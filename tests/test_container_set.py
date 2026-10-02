import copy
import sys
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
import server
import homestead_lifecycle as lifecycle
import homestead_host_access as host_access


class ContainerSetTests(unittest.TestCase):
    def setUp(self):
        self.current = {"apiVersion": "apps/v1", "kind": "Deployment",
            "metadata": {"name": "media", "namespace": "lab", "resourceVersion": "7"},
            "spec": {"replicas": 2, "selector": {"matchLabels": {"app": "media"}},
                "template": {"metadata": {}, "spec": {"containers": [
                    {"name": "main", "image": "example/main:1", "env": [
                        {"name": "TOKEN", "valueFrom": {"secretKeyRef": {"name": "private", "key": "token"}}}],
                     "volumeMounts": [{"name": "data", "mountPath": "/data"}]},
                    {"name": "old-helper", "image": "example/helper:1"}],
                    "volumes": [{"name": "data", "persistentVolumeClaim": {"claimName": "media-data"}}]}}}}
        self.read = mock.Mock(side_effect=urllib.error.HTTPError("fixture", 404, "missing", {}, None))
        for patch in (mock.patch.object(lifecycle, "kget", self.read),
                      mock.patch.object(lifecycle, "hardware_features", return_value=[]),
                      mock.patch.object(server.HW, "features", return_value=[])):
            patch.start(); self.addCleanup(patch.stop)

    def prepare(self, **changes):
        return lifecycle.prepare_edit({"ns": "lab", "name": "media", **changes}, current=self.current)

    def test_add_and_remove_are_explicit_and_leave_source_and_data_untouched(self):
        before = copy.deepcopy(self.current)
        prepared = self.prepare(remove_containers=["old-helper"], containers=[
            {"original_name": "main", "name": "main", "env": {"VISIBLE": "changed"}},
            {"new": True, "name": "metrics", "image": "example/metrics", "memory": "128Mi",
             "volumes": [{"kind": "pod", "source": "data", "path": "/shared", "read_only": True}]}])
        spec = prepared["deployment"]["spec"]["template"]["spec"]
        self.assertEqual(["main", "metrics"], [c["name"] for c in spec["containers"]])
        self.assertEqual("IfNotPresent", spec["containers"][1]["imagePullPolicy"])
        self.assertEqual("example/metrics:latest", spec["containers"][1]["image"])
        self.assertTrue(spec["containers"][1]["volumeMounts"][0]["readOnly"])
        self.assertEqual(before, self.current)
        self.assertEqual("7", prepared["deployment"]["metadata"]["resourceVersion"])
        self.assertEqual(before["spec"]["template"]["spec"]["containers"][0]["env"][0], spec["containers"][0]["env"][0])
        self.assertEqual([], prepared["claims"])

    def test_old_clients_do_not_remove_omitted_containers(self):
        prepared = self.prepare(containers=[{"original_name": "main", "memory": "256Mi"}])
        self.assertEqual(2, len(prepared["deployment"]["spec"]["template"]["spec"]["containers"]))

    def test_removing_container_does_not_delete_its_persistent_claim(self):
        prepared = self.prepare(remove_containers=["main"], containers=[])
        self.assertEqual([], prepared["claims"])
        self.assertEqual(self.current["spec"]["template"]["spec"]["volumes"], prepared["deployment"]["spec"]["template"]["spec"]["volumes"])
        self.read.assert_not_called()

    def test_last_container_cannot_be_removed(self):
        with self.assertRaisesRegex(ValueError, "at least one"):
            self.prepare(remove_containers=["main", "old-helper"], containers=[])

    def test_stale_or_duplicate_removal_fails_before_any_write(self):
        for removed in (["gone"], ["main", "main"]):
            with self.subTest(removed=removed), self.assertRaises(ValueError):
                self.prepare(remove_containers=removed)

    def test_new_container_cannot_overwrite_regular_or_init_container(self):
        for name in ("main", "init"):
            self.current["spec"]["template"]["spec"]["initContainers"] = [{"name": "init", "image": "example/init"}]
            with self.subTest(name=name), self.assertRaisesRegex(ValueError, "unique"):
                self.prepare(containers=[{"new": True, "name": name, "image": "example/new"}])

    def test_add_requires_explicit_flag_and_an_image(self):
        for change in ({"name": "new", "image": "example/new"}, {"new": True, "name": "new", "image": ""},
                       {"new": True, "name": "new", "image": "example/new", "original_name": "main"}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                self.prepare(containers=[change])

    def test_add_and_edit_cannot_duplicate_or_remove_the_same_original(self):
        for changes in ([{"original_name": "main"}, {"original_name": "main"}], [{"original_name": "old-helper"}]):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                self.prepare(remove_containers=["old-helper"], containers=changes)

    def config(self):
        return {"name": "media", "workload_name": "media", "container_name": "main", "image": "example/main:1",
                "namespace": "lab", "network_mode": "internal", "replicas": 2,
                "ports": [{"container": 8080, "expose": True}],
                "additional_containers": [{"name": "metrics", "image": "example/metrics:1", "memory": "128Mi",
                    "ports": [{"container": 9090, "expose": True}], "env": {"API_TOKEN": "private"},
                    "volumes": [{"kind": "new-rwo", "type": "pvc", "source": "metrics-data", "create": True,
                                 "size_gb": 3, "path": "/config"}]}]}

    def test_new_workload_manifest_and_service_include_every_container(self):
        config = self.config(); before = copy.deepcopy(config)
        dep, svc = server.build_deployment(config)
        self.assertEqual(["main", "metrics"], [c["name"] for c in dep["spec"]["template"]["spec"]["containers"]])
        self.assertEqual([8080, 9090], [p["targetPort"] for p in svc["spec"]["ports"]])
        self.assertEqual(2, dep["spec"]["replicas"])
        self.assertEqual(before, config)
        self.assertEqual({"app": "media"}, svc["spec"]["selector"])
        safe = server.redact_deployment_preview(dep, config)
        self.assertEqual("••••••", safe["spec"]["template"]["spec"]["containers"][1]["env"][0]["value"])

    def test_capacity_manifest_plans_additional_claims_before_provisioning(self):
        _, dep, claims, _ = server.capacity_manifest(self.config())
        self.assertEqual(3, claims["metrics-data"]["size_gb"])
        self.assertEqual(2, len(dep["spec"]["template"]["spec"]["containers"]))
        self.assertTrue(dep["spec"]["template"]["spec"]["initContainers"])

    def test_additional_host_access_cannot_bypass_deploy_guard(self):
        config = self.config()
        config["additional_containers"][0]["privileges"] = {"privileged": True}
        with mock.patch.object(host_access, "_allowed", return_value=False), self.assertRaises(host_access.Refused):
            server.build_deployment(config)

    def test_additional_container_cannot_override_shared_pod_or_privilege_settings(self):
        for changes in ({"node": "elsewhere"}, {"privileges": {"namespace": "system"}}, {"args": ["unsupported"]}):
            config = self.config(); config["additional_containers"][0].update(changes)
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                server.build_deployment(config)

    def test_batch_of_additional_containers_cannot_silently_join_existing_workload(self):
        config = self.config(); config["target_mode"] = "existing"
        with self.assertRaisesRegex(ValueError, "editor"):
            server.additional_container_configs(config)

    def test_network_preparation_checks_additional_listeners_but_keeps_primary_ports(self):
        config = self.config()
        with mock.patch.object(server.NETWORK, "prepare_deploy", side_effect=lambda cfg: dict(cfg, lb_ip="192.0.2.1")) as network:
            prepared = server.prepare_deploy_network(config)
        self.assertEqual([8080, 9090], [p["container"] for p in network.call_args.args[0]["ports"]])
        self.assertEqual(config["ports"], prepared["ports"])
        self.assertEqual("192.0.2.1", prepared["lb_ip"])

    def test_execution_provisions_additional_claims_and_saves_one_complete_pod_template(self):
        config = self.config()
        with mock.patch.object(server, "guard_managed_smb"), mock.patch.object(server, "persist_icon_config"), \
                mock.patch.object(server.NETWORK, "prepare_deploy", side_effect=lambda cfg: cfg), \
                mock.patch.object(server, "prepare_lan", side_effect=lambda cfg: cfg), \
                mock.patch.object(server.VOLOWNER, "prepare", side_effect=lambda cfg: cfg), \
                mock.patch.object(server, "ensure_claim", return_value=False) as claim, \
                mock.patch.object(server, "ksend") as send, \
                mock.patch.object(server.OPS, "start", return_value={"id": "created"}):
            server.run_deploy(config)
        claim.assert_called_once_with("lab", "metrics-data", 3, server.STORAGE_CLASS, "ReadWriteOnce")
        deployment = next(call.args[2] for call in send.call_args_list if call.args[1].endswith("/deployments"))
        self.assertEqual(["main", "metrics"], [c["name"] for c in deployment["spec"]["template"]["spec"]["containers"]])
        service = next(call.args[2] for call in send.call_args_list if call.args[1].endswith("/services"))
        self.assertEqual([8080, 9090], [p["targetPort"] for p in service["spec"]["ports"]])

    def test_owner_helper_maps_additional_container_to_its_actual_volume(self):
        config = self.config()
        config["additional_containers"][0]["volume_owners"] = {"/config": [1000, 1000, 493]}
        dep, _ = server.build_deployment(config)
        spec = dep["spec"]["template"]["spec"]
        self.assertEqual(spec["containers"][1]["volumeMounts"][0]["name"], spec["initContainers"][0]["volumeMounts"][0]["name"])
        self.assertEqual("hs-owner-metrics", spec["initContainers"][0]["name"])


if __name__ == "__main__":
    unittest.main()
