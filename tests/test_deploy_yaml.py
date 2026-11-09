"""Deploy's review offers the app as YAML for git (#920).

A new app is the whole app: its Deployment, Service and each volume claim
Deploy creates, as one multi-document file. Adding a container to an app is
the patch, as Edit's YAML is. Server fields, status and Homestead's own
bookkeeping are left out; settings a person chose and the claims' managed
label stay; secret values never appear, only the Secret's name and keys.
"""
import copy
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
import server
import homestead_deploy_manifests as MANIFESTS
import homestead_gitops as GITOPS
import homestead_names as NAMES
import homestead_priority as PRIORITY
import homestead_network_services as SVC
import test_deploy_capacity as CAPACITY

TYPED_SECRET = "typed-s3cr3t-value"
GENERATED = "GENERATED-s3cr3t-value"
SERVER_FIELDS = ("status:", "managedFields", "uid:", "resourceVersion", "creationTimestamp", "generation:",
                 "selfLink", "ownerReferences")


def documents(text):
    """Each document of a multi-document YAML file, and its kind."""
    docs = text.split("\n---\n")
    return docs, [next((line.split(": ", 1)[1] for line in doc.splitlines() if line.startswith("kind: ")), "")
                  for doc in docs]


class DeployYaml(unittest.TestCase):
    setUp, get, volume = (CAPACITY.DeployCapacityTests.setUp, CAPACITY.DeployCapacityTests.get,
                          CAPACITY.DeployCapacityTests.volume)

    def preview(self, cfg):
        handler = object.__new__(server.H)
        handler.path, handler.headers = "/api/preview", {}
        handler._guard = lambda path: False
        handler._body = lambda: copy.deepcopy(cfg)
        handler._client_ip = lambda: "127.0.0.1"
        handler._send = mock.Mock()
        with mock.patch.object(SVC, "prepare_deploy", side_effect=lambda value: value), \
             mock.patch("homestead_vip_registry.registered", return_value=[]), \
             mock.patch.object(server.KUBE_HELPERS, "kget", side_effect=self.get), \
             mock.patch("secrets.token_urlsafe", return_value=GENERATED):
            handler.do_POST()
        code, body = handler._send.call_args.args[:2]
        self.assertEqual(200, code, body)
        return body

    def with_server_fields(self, build):
        """The builder's objects, as if read back from the cluster: what a
        person must not commit, and one setting they chose."""
        def built(cfg, *args):
            dep, svc = build(cfg, *args)
            for obj in (dep, svc):
                if obj:
                    obj["status"] = {"replicas": 1}
                    meta = obj["metadata"]
                    meta.update(uid="1234-abcd", resourceVersion="42", creationTimestamp="2026-01-01T00:00:00Z",
                                generation=3, managedFields=[{"manager": "homestead"}])
                    meta.setdefault("annotations", {})[GITOPS.UPDATES.TRACKED] = "bookkeeping"
                    meta["annotations"]["example.com/team"] = "media"
            return dep, svc
        return built

    def new_app(self):
        self.volume()
        self.cfg.update(ports=[{"container": 8080, "host": 8080, "protocol": "TCP", "expose": True}],
                        env={"DB_PASSWORD": TYPED_SECRET, "ADMIN_TOKEN": "", "TZ": "UTC"},
                        env_meta=[{"key": "DB_PASSWORD", "masked": True},
                                  {"key": "ADMIN_TOKEN", "masked": True, "generate": True}])
        with mock.patch.object(MANIFESTS, "build_deployment", self.with_server_fields(MANIFESTS.build_deployment)):
            return self.preview(self.cfg)

    def test_a_new_app_is_its_deployment_service_and_claims_in_one_file(self):
        body = self.new_app()
        self.assertEqual("demo.yaml", body["yaml_file"])
        docs, kinds = documents(body["yaml"])
        self.assertEqual(["Deployment", "Service", "PersistentVolumeClaim"], kinds)
        claim = docs[2]
        self.assertIn("name: data", claim)
        self.assertIn("namespace: lab", claim)
        self.assertIn("storageClassName: test-class", claim)
        self.assertIn("storage: 10Gi", claim)
        self.assertIn(f"{NAMES.key('managed')}: \"true\"", claim)   # Homestead still knows its volumes

    def test_server_fields_status_and_bookkeeping_are_left_out_and_chosen_settings_kept(self):
        text = self.new_app()["yaml"]
        for field in SERVER_FIELDS:
            self.assertNotIn(field, text)
        self.assertNotIn(GITOPS.UPDATES.TRACKED, text)
        self.assertIn("example.com/team: media", text)
        self.assertIn("value: UTC", text)

    def test_secret_values_never_appear_and_their_keys_are_named(self):
        body = self.new_app()
        self.assertNotIn(TYPED_SECRET, body["yaml"])
        self.assertNotIn(GENERATED, body["yaml"])
        self.assertNotIn(TYPED_SECRET, str(body["deployment"]))
        self.assertEqual({"name": "demo-env", "keys": ["demo.ADMIN_TOKEN", "demo.DB_PASSWORD"]}, body["secret"])
        self.assertIn("secretKeyRef", body["yaml"])
        self.assertIn("key: demo.DB_PASSWORD", body["yaml"])

    # What Deploy adds when it runs that the file cannot hold, only when it applies.
    def test_an_app_with_neither_a_new_volume_nor_a_lan_address_adds_nothing_at_deploy_time(self):
        self.assertEqual([], self.preview(self.cfg)["deploy_adds"])

    def test_a_new_volume_and_a_named_user_add_a_volume_owner_init_container(self):
        self.volume()
        self.cfg["run_as_user"] = 1000
        self.assertEqual([{"what": "a volume-owner init container", "certain": True}], self.preview(self.cfg)["deploy_adds"])

    def test_a_new_volume_without_a_named_user_may_add_one_without_reading_the_registry(self):
        self.volume()
        with mock.patch("homestead_volowner.image_owners", side_effect=AssertionError("registry read in a preview")):
            adds = self.preview(self.cfg)["deploy_adds"]
        self.assertEqual([{"what": "a volume-owner init container", "certain": False}], adds)

    def test_a_volume_whose_image_takes_puid_adds_no_init_container(self):
        self.volume()
        self.cfg["env"] = {"PUID": "1000"}
        self.assertEqual([], self.preview(self.cfg)["deploy_adds"])

    def test_a_privileged_container_or_an_explicit_root_user_adds_no_init_container(self):
        for setting in ({"privileged": True}, {"run_as_user": 0}):
            with self.subTest(setting):
                self.setUp(); self.volume(); self.cfg.update(setting)
                self.assertEqual([], self.preview(self.cfg)["deploy_adds"])

    def test_a_tier_whose_class_the_cluster_lacks_names_the_class_deploy_makes(self):
        self.cfg["priority"] = "critical"
        real, classes = PRIORITY.ladder, []
        ours = {"metadata": {"name": "homestead-critical"}, "value": 1000, "preemptionPolicy": "PreemptLowerPriority"}
        with mock.patch.object(PRIORITY, "ladder", side_effect=lambda *a, **k: real(items=list(classes))):
            adds = self.preview(self.cfg)["deploy_adds"]
            self.assertEqual([{"what": "a PriorityClass homestead-critical if the cluster has none", "certain": True}], adds)
            classes.append(ours)                          # the cluster has one to use
            self.assertEqual([], self.preview(self.cfg)["deploy_adds"])

    def test_when_the_classes_cannot_be_read_the_line_says_what_deploy_then_does(self):
        self.cfg["priority"] = "critical"
        # Listing forbidden: Deploy makes Homestead's own class by name, as before #429.
        with mock.patch.object(PRIORITY, "ladder", return_value=PRIORITY._fallback(forbidden=True)):
            self.assertEqual([{"what": "a PriorityClass homestead-critical if the cluster has none", "certain": True}],
                             self.preview(self.cfg)["deploy_adds"])
        # The read failed: Deploy refuses and asks to try again, so nothing is sure yet.
        with mock.patch.object(PRIORITY, "ladder", return_value=PRIORITY._fallback(forbidden=False)):
            self.assertEqual([{"what": "a PriorityClass homestead-critical if the cluster has none "
                                       "(its classes could not be read just now, so Deploy waits until they can)",
                               "certain": False}], self.preview(self.cfg)["deploy_adds"])

    def test_a_one_word_password_typed_into_deploy_is_a_secret_key_in_the_yaml(self):
        self.cfg["env"] = {"DBPASS": TYPED_SECRET, "BYPASS": "1"}
        body = self.preview(self.cfg)
        self.assertNotIn(TYPED_SECRET, body["yaml"])
        self.assertEqual({"name": "demo-env", "keys": ["demo.DBPASS"]}, body["secret"])
        self.assertIn("key: demo.DBPASS", body["yaml"])
        self.assertIn("value: \"1\"", body["yaml"])               # an ordinary word stays a value

    def test_server_fields_in_the_pod_template_and_the_last_applied_copy_are_left_out(self):
        def built(cfg, *args):
            dep, svc = real(cfg, *args)
            template = dep["spec"]["template"].setdefault("metadata", {})
            template.update(uid="pod-uid", resourceVersion="9", creationTimestamp="2026-01-01T00:00:00Z",
                            managedFields=[{"manager": "kubelet"}])
            template.setdefault("annotations", {})["kubectl.kubernetes.io/restartedAt"] = "2026-01-01T00:00:00Z"
            dep["metadata"].setdefault("annotations", {})["kubectl.kubernetes.io/last-applied-configuration"] = (
                '{"spec":{"template":{"spec":{"containers":[{"env":[{"name":"DB_PASSWORD","value":"%s"}]}]}}}}' % TYPED_SECRET)
            return dep, svc
        real = MANIFESTS.build_deployment
        with mock.patch.object(MANIFESTS, "build_deployment", built):
            text = self.preview(self.cfg)["yaml"]
        for field in SERVER_FIELDS + ("restartedAt", "last-applied-configuration", TYPED_SECRET):
            self.assertNotIn(field, text)

    def test_a_lan_address_in_the_yaml_is_the_cleaned_one_deploy_saves(self):
        self.cfg.update(network_mode="lan", lan={"network": "lab/lan-a", "address": " 192.0.2.40 ", "prefix": "24"})
        # The preview checks no address: that is IPAM, the inventory and a probe of five ports.
        with mock.patch("homestead_vm_server.vm_address_problem", side_effect=AssertionError("probed")) as probe:
            body = self.preview(self.cfg)
        probe.assert_not_called()
        self.assertIn('{\\"network\\": \\"lab/lan-a\\", \\"address\\": \\"192.0.2.40\\", \\"prefix\\": 24, \\"gateway\\": \\"\\"}',
                      body["yaml"])
        self.assertIn({"what": "a LAN network attachment", "certain": True}, body["deploy_adds"])

    def test_an_app_with_a_lan_address_adds_its_network_attachment(self):
        body = server.DEPLOY.for_git({**self.cfg, "network_mode": "lan", "lan": {"address": "192.0.2.40"}},
                                     {"metadata": {"name": "demo", "namespace": "lab"}, "spec": {}}, None)
        self.assertEqual([{"what": "a LAN network attachment", "certain": True}], body["deploy_adds"])

    def test_adding_a_container_to_an_app_is_a_patch_and_its_new_claim(self):
        current = {"apiVersion": "apps/v1", "kind": "Deployment",
                   "metadata": {"name": "web", "namespace": "lab", "uid": "dep-uid", "resourceVersion": "7",
                                "labels": {"app": "web"}, "managedFields": [{"manager": "kubectl"}]},
                   "spec": {"replicas": 1, "selector": {"matchLabels": {"app": "web"}},
                            "template": {"metadata": {"labels": {"app": "web"}},
                                         "spec": {"containers": [{"name": "web", "image": "example/web:2",
                                                                  "env": [{"name": "MODE", "value": "full"}]}]}}},
                   "status": {"replicas": 1, "readyReplicas": 1}}
        self.objects["/apis/apps/v1/namespaces/lab/deployments/web"] = current
        self.objects["/apis/apps/v1/namespaces/lab/replicasets"] = {"items": []}
        self.objects["/api/v1/namespaces/lab/pods"] = {"items": []}
        self.volume(source="sidecar-data")
        self.cfg.update(target_mode="existing", target_workload="web", container_name="sidecar")
        body = self.preview(self.cfg)
        self.assertEqual("web-patch.yaml", body["yaml_file"])
        docs, kinds = documents(body["yaml"])
        self.assertEqual(["Deployment", "PersistentVolumeClaim"], kinds)
        patch = docs[0]
        self.assertIn("name: web", patch)
        self.assertIn("name: sidecar", patch)
        self.assertNotIn("example/web:2", patch)     # only what adding the container changes
        self.assertNotIn("MODE", patch)
        for field in SERVER_FIELDS:
            self.assertNotIn(field, body["yaml"])
        self.assertIn("name: sidecar-data", docs[1])
        self.assertEqual([], body["deploy_adds"])     # Deploy adds no owner step to an app it joins


if __name__ == "__main__":
    unittest.main()
