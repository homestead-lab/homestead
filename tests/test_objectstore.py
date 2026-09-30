"""Backups need somewhere to go, and that somewhere has to be reachable."""
import base64
import json
import sys
import unittest
import urllib.error
from pathlib import Path
from unittest import mock


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
import homestead_objectstore as store


class ObjectStoreTests(unittest.TestCase):
    def setUp(self):
        self.objects = {}
        self.sent = []
        self.claims = []
        store.bind(self._get, self._send, self._create_pvc, "lab")
        store.LH.bind(self._get, self._send, {}, "longhorn-r2")
        # Homestead's shared address, as Networking would plan it.
        self.plans = []
        plan = mock.patch.object(store.NETWORK, "service_plan",
                                 side_effect=lambda cfg, **kw: self.plans.append(cfg) or {"vip": "192.0.2.242"})
        plan.start()
        self.addCleanup(plan.stop)

    def _get(self, path):
        key = path.split("?")[0]
        if key not in self.objects:
            raise urllib.error.HTTPError(path, 404, "missing", {}, None)
        return self.objects[key]

    def _send(self, method, path, body=None, **kwargs):
        self.sent.append((method, path.split("?")[0], body))
        return body or {}

    def _create_pvc(self, ns, name, size, storage_class=None, access_mode="ReadWriteOnce"):
        self.claims.append((ns, name, size, storage_class, access_mode))

    def _service(self, ip=None, annotation=None):
        self.objects["/api/v1/namespaces/lab/services/homestead-objectstore"] = {
            "metadata": {"name": "homestead-objectstore", "resourceVersion": "1",
                         "annotations": {"kube-vip.io/loadbalancerIPs": annotation}
                         if annotation else {}},
            "status": {"loadBalancer": {"ingress": [{"ip": ip}] if ip else []}},
        }

    def store_running(self, image, replicas=1):
        self.objects["/apis/apps/v1/namespaces/lab/deployments/homestead-objectstore"] = {
            "spec": {"replicas": replicas, "template": {"spec": {"containers": [{"name": "s3", "image": image}]}}}}

    def test_an_older_rustfs_moves_on_to_the_pinned_release_and_nothing_else_is_touched(self):
        self.assertEqual("rustfs/rustfs:1.0.0", store.IMAGE)
        self.store_running("rustfs/rustfs:1.0.0-rc.6")
        self.assertEqual(store.IMAGE, store.keep_in_step())
        method, path, body = self.sent[-1]
        self.assertEqual(("PATCH", "/apis/apps/v1/namespaces/lab/deployments/homestead-objectstore"), (method, path))
        self.assertEqual({"spec": {"template": {"spec": {"containers": [{"name": "s3", "image": "rustfs/rustfs:1.0.0"}]}}}}, body,
                         "only the image: keys, volume and address stay")
        for image in ("rustfs/rustfs:1.0.0", "rustfs/rustfs:1.0.1", "minio/minio:RELEASE.2025-04-22T22-12-26Z",
                      "rustfs/rustfs:latest", "rustfs/rustfs@sha256:" + "a" * 64):
            with self.subTest(image=image):
                self.sent.clear()
                self.store_running(image)
                self.assertEqual("", store.keep_in_step())
                self.assertEqual([], self.sent)
        self.store_running("rustfs/rustfs:1.0.0-rc.6", replicas=0)
        self.assertEqual("", store.keep_in_step(), "a store turned off stays off")
        self.assertLess(store._rustfs_version("rustfs/rustfs:1.0.0-rc.6"), store._rustfs_version("rustfs/rustfs:1.0.0"))

    def test_nothing_is_deployed_to_start_with(self):
        state = store.status()

        self.assertFalse(state["deployed"])
        self.assertFalse(state["ready"])

    def test_deploy_creates_a_volume_a_workload_and_an_address(self):
        store.deploy({"size_gb": 200, "lb_ip": "192.0.2.243", "point_longhorn": False})

        self.assertEqual([("lab", "homestead-objectstore", 200, None, "ReadWriteOnce")],
                         self.claims)
        created = [p for _, p, _ in self.sent]
        self.assertIn("/apis/apps/v1/namespaces/lab/deployments", created)
        self.assertIn("/api/v1/namespaces/lab/services", created)
        service = next(b for _, p, b in self.sent if p.endswith("/services"))
        self.assertEqual("LoadBalancer", service["spec"]["type"])
        self.assertEqual("192.0.2.243",
                         service["metadata"]["annotations"]["kube-vip.io/loadbalancerIPs"])

    def test_the_store_keeps_one_copy_on_a_class_of_its_own(self):
        self.objects["/apis/storage.k8s.io/v1/storageclasses/longhorn-r2"] = {
            "provisioner": "driver.longhorn.io",
            "parameters": {"numberOfReplicas": "3", "migratable": "true", "diskSelector": "ssd"}}
        store.deploy({"size_gb": 100, "lb_ip": "192.0.2.243", "point_longhorn": False})
        made = next(b for m, p, b in self.sent if p == "/apis/storage.k8s.io/v1/storageclasses")
        self.assertEqual("homestead-single-copy", made["metadata"]["name"])
        self.assertEqual({"numberOfReplicas": "1", "migratable": "false", "diskSelector": "ssd"}, made["parameters"])
        self.assertEqual([("lab", "homestead-objectstore", 100, "homestead-single-copy", "ReadWriteOnce")], self.claims)

    def test_a_volume_longhorn_cannot_place_is_explained(self):
        self._deployment(1)
        self.objects["/apis/apps/v1/namespaces/lab/deployments/homestead-objectstore"]["status"] = {"readyReplicas": 0}
        self.objects["/api/v1/namespaces/lab/persistentvolumeclaims/homestead-objectstore"] = {
            "spec": {"volumeName": "pvc-3a8d", "resources": {"requests": {"storage": "500Gi"}}}}
        self.objects["/apis/longhorn.io/v1beta2/namespaces/longhorn-system/volumes/pvc-3a8d"] = {"status": {"conditions": [
            {"type": "Scheduled", "status": "False", "reason": "ReplicaSchedulingFailure",
             "message": "precheck new replica failed: insufficient storage"}]}}
        self.assertEqual("precheck new replica failed: insufficient storage", store.transfers()["volume_problem"])

    def test_a_class_of_that_name_homestead_did_not_make_is_not_used(self):
        self.objects["/apis/storage.k8s.io/v1/storageclasses/longhorn-r2"] = {"provisioner": "driver.longhorn.io", "parameters": {}}
        self.objects["/apis/storage.k8s.io/v1/storageclasses/homestead-single-copy"] = {
            "metadata": {"name": "homestead-single-copy"}, "provisioner": "driver.longhorn.io"}
        store.deploy({"lb_ip": "192.0.2.243", "point_longhorn": False})
        self.assertEqual(None, self.claims[0][3], "the cluster's default instead")
        self.assertFalse(any(p.endswith("/storageclasses") for _, p, _ in self.sent))

    def _sent_service(self):
        return [b for _, p, b in self.sent if "/services" in p][-1]

    def test_with_no_address_asked_for_it_shares_homesteads(self):
        store.deploy({"point_longhorn": False})
        self.assertEqual("192.0.2.242", self._sent_service()["metadata"]["annotations"]["kube-vip.io/loadbalancerIPs"])
        # Checked as an app sharing the address would be: both its ports, on the shared address.
        self.assertEqual("shared", self.plans[0]["vip_mode"])
        self.assertEqual([9000, 9001], [p["port"] for p in self.plans[0]["ports"]])

    def test_where_services_go_on_the_nodes_it_goes_there_too(self):
        store.NETWORK.service_plan.side_effect = lambda cfg, **kw: {"vip": ""}
        store.deploy({"point_longhorn": False})
        self.assertEqual({}, self._sent_service()["metadata"]["annotations"])

    def test_a_port_taken_on_the_shared_address_says_so(self):
        store.NETWORK.service_plan.side_effect = ValueError("192.0.2.242:9000/TCP is already used by lab/minio")
        with self.assertRaises(ValueError) as caught:
            store.deploy({"point_longhorn": False})
        self.assertIn("lab/minio", str(caught.exception))
        self.assertIn("address of its own", str(caught.exception))

    def test_a_chosen_port_is_where_it_answers(self):
        store.deploy({"point_longhorn": False, "port": 9100})
        service = self._sent_service()
        self.assertEqual({"s3": 9100, "console": 9101}, {p["name"]: p["port"] for p in service["spec"]["ports"]})
        self.assertEqual([9100, 9101], [p["port"] for p in self.plans[0]["ports"]])
        self.objects["/api/v1/namespaces/lab/services/homestead-objectstore"] = dict(
            service, status={"loadBalancer": {"ingress": [{"ip": "192.0.2.242"}]}})
        self.assertEqual("http://192.0.2.242:9100", store.endpoint())

    def test_a_taken_port_names_a_free_one(self):
        def plan(cfg, **kw):
            if cfg["ports"][0]["port"] == 9000:
                raise ValueError("192.0.2.20:9000/TCP is already used by lab/home-assistant-core")
            return {"vip": "192.0.2.20"}
        store.NETWORK.service_plan.side_effect = plan
        with self.assertRaises(ValueError) as caught:
            store.deploy({"point_longhorn": False})
        self.assertIn("home-assistant-core", str(caught.exception))
        self.assertIn("Choose another port", str(caught.exception))
        self.assertIn("Port 9010 is free there", str(caught.exception))
        store.deploy({"point_longhorn": False, "port": 9010})
        self.assertEqual("192.0.2.20", self._sent_service()["metadata"]["annotations"]["kube-vip.io/loadbalancerIPs"])

    def test_an_address_chosen_for_the_store_beats_its_older_copy_on_the_vip(self):
        # A chosen store address supersedes a legacy Service on another address.
        self.objects["/api/v1/namespaces/lab/services/homestead-objectstore"] = {
            "metadata": {"name": "homestead-objectstore", "annotations": {"kube-vip.io/loadbalancerIPs": "192.0.2.20"}},
            "spec": {"ports": [{"name": "s3", "port": 9060}]},
            "status": {"loadBalancer": {"ingress": [{"ip": "192.0.2.20"}]}}}
        self.objects["/api/v1/namespaces/lab/services/homestead-objectstore-vip"] = {
            "metadata": {"name": "homestead-objectstore-vip", "annotations": {"kube-vip.io/loadbalancerIPs": "192.0.2.10"}},
            "spec": {"ports": [{"name": "s3", "port": 9000}]},
            "status": {"loadBalancer": {"ingress": [{"ip": "192.0.2.10"}]}}}
        self.assertEqual("http://192.0.2.20:9060", store.endpoint())
        # On the nodes' own addresses (k3s ServiceLB) the VIP copy is the one to use.
        self.objects["/api/v1/namespaces/lab/services/homestead-objectstore"]["metadata"]["annotations"] = {}
        self.objects["/api/v1/namespaces/lab/services/homestead-objectstore"]["status"]["loadBalancer"]["ingress"] = [{"ip": "192.0.2.203"}]
        self.assertEqual("http://192.0.2.10:9000", store.endpoint())

    def test_a_store_already_running_keeps_its_port(self):
        self._service("192.0.2.244", "192.0.2.244")
        self.objects["/api/v1/namespaces/lab/services/homestead-objectstore"]["spec"] = {
            "ports": [{"name": "s3", "port": 9100}, {"name": "console", "port": 9101}]}
        store.deploy({"point_longhorn": False})
        self.assertEqual(9100, self._sent_service()["spec"]["ports"][0]["port"])
        self.assertEqual("192.0.2.244", self._sent_service()["metadata"]["annotations"]["kube-vip.io/loadbalancerIPs"])

    def test_a_silly_port_is_refused(self):
        for port in (70000, -1, "x"):
            with self.subTest(port=port), self.assertRaises(ValueError):
                store.deploy({"point_longhorn": False, "port": port})

    def test_a_store_already_running_keeps_its_address(self):
        self._service("192.0.2.244", "192.0.2.244")
        store.deploy({"point_longhorn": False})
        self.assertEqual("192.0.2.244", self._sent_service()["metadata"]["annotations"]["kube-vip.io/loadbalancerIPs"])
        self.assertEqual([], self.plans)

    def test_the_requested_vip_wins_over_a_stale_node_address(self):
        self._service("192.0.2.10", "192.0.2.20")
        self.objects["/api/v1/namespaces/lab/services/homestead-objectstore"]["spec"] = {
            "ports": [{"name": "s3", "port": 9070, "targetPort": 9000}]}
        self.assertEqual("http://192.0.2.20:9070", store.endpoint())
        self.assertEqual("http://192.0.2.20:9070", store.status()["endpoint"])

    def test_status_and_target_use_the_same_vip_service_and_its_published_port(self):
        self._service("192.0.2.10")
        self.objects["/api/v1/namespaces/lab/services/homestead-objectstore-vip"] = {
            "metadata": {"annotations": {"kube-vip.io/loadbalancerIPs": "192.0.2.20"}},
            "spec": {"ports": [{"name": "s3", "port": 9070, "targetPort": 9000}]}}
        self.assertEqual("http://192.0.2.20:9070", store.endpoint())
        self.assertEqual(store.endpoint(), store.status()["endpoint"])
        self.assertEqual(9070, store.status()["port"])

    def test_a_store_can_be_moved_to_the_shared_address_on_purpose(self):
        self._service()
        store.deploy({"point_longhorn": False, "vip_mode": "shared"})
        self.assertEqual("192.0.2.242", self._sent_service()["metadata"]["annotations"]["kube-vip.io/loadbalancerIPs"])

    def test_the_writer_replaces_rather_than_surges(self):
        """One pod, one ReadWriteOnce volume: a surge would deadlock on it."""
        store.deploy({"point_longhorn": False})

        deployment = next(b for _, p, b in self.sent if p.endswith("/deployments"))
        self.assertEqual("Recreate", deployment["spec"]["strategy"]["type"])

    def test_keys_are_generated_once_and_then_reused(self):
        store.deploy({"point_longhorn": False})
        written = next(b for _, p, b in self.sent if p.endswith("/secrets"))
        first = base64.b64decode(written["data"]["secretkey"]).decode()

        self.objects["/api/v1/namespaces/lab/secrets/homestead-objectstore-keys"] = written
        self.assertEqual(first, store.credentials()["secret_key"],
                         "regenerating the key would orphan every existing backup")

    def test_a_generated_key_is_not_a_guessable_one(self):
        self.assertGreaterEqual(len(store.credentials()["secret_key"]), 32)

    def test_longhorn_is_given_the_lan_address_not_the_cluster_one(self):
        """A backup only readable from inside this cluster cannot be restored
        onto the cluster you are moving to."""
        self._service(ip="192.0.2.243")

        result = store.point_longhorn()

        self.assertEqual("http://192.0.2.243:9000", result["endpoint"])
        secret = next(b for _, p, b in self.sent
                      if p == "/api/v1/namespaces/longhorn-system/secrets")
        self.assertEqual("http://192.0.2.243:9000",
                         base64.b64decode(secret["data"]["AWS_ENDPOINTS"]).decode())
        self.assertEqual("s3://homestead-backups@us-east-1/", result["url"])

    def _deployed(self):
        return next(b for m, p, b in self.sent if p.endswith("/deployments") and m == "POST")

    def test_a_fresh_store_runs_rustfs_as_its_own_user(self):
        """MinIO's images are no longer published; RustFS is a drop-in."""
        store.deploy({"point_longhorn": False})
        spec = self._deployed()["spec"]["template"]["spec"]
        container = spec["containers"][0]
        self.assertTrue(container["image"].startswith("rustfs/rustfs:"))
        self.assertEqual({"RUSTFS_ACCESS_KEY", "RUSTFS_SECRET_KEY", "RUSTFS_VOLUMES"}, {e["name"] for e in container["env"]})
        self.assertEqual(10001, spec["securityContext"]["fsGroup"])

    def test_a_minio_already_serving_backups_is_left_as_it_is(self):
        self.objects["/apis/apps/v1/namespaces/lab/deployments/homestead-objectstore"] = {
            "metadata": {"resourceVersion": "5"}, "status": {"readyReplicas": 1},
            "spec": {"template": {"spec": {"containers": [{"image": "quay.io/minio/minio:RELEASE.2024-09-22T00-33-43Z"}]}}}}
        store.deploy({"point_longhorn": False})
        put = next(b for m, p, b in self.sent if m == "PUT" and p.endswith("/deployments/homestead-objectstore"))
        container = put["spec"]["template"]["spec"]["containers"][0]
        self.assertIn("minio/minio", container["image"])
        self.assertIn("MINIO_ROOT_USER", {e["name"] for e in container["env"]})

    def test_a_minio_that_cannot_start_is_replaced(self):
        self.objects["/apis/apps/v1/namespaces/lab/deployments/homestead-objectstore"] = {
            "metadata": {"resourceVersion": "5"}, "status": {},
            "spec": {"template": {"spec": {"containers": [{"image": "quay.io/minio/minio:RELEASE.2024-09-22T00-33-43Z"}]}}}}
        store.deploy({"point_longhorn": False})
        put = next(b for m, p, b in self.sent if m == "PUT" and p.endswith("/deployments/homestead-objectstore"))
        self.assertTrue(put["spec"]["template"]["spec"]["containers"][0]["image"].startswith("rustfs/rustfs:"))

    def test_requests_are_signed_the_way_s3_checks(self):
        headers = store._sign("PUT", "http://store.lab.svc:9000/homestead-backups", "AKID", "SECRET")
        self.assertTrue(headers["Authorization"].startswith("AWS4-HMAC-SHA256 Credential=AKID/"))
        self.assertIn("/us-east-1/s3/aws4_request, SignedHeaders=host;x-amz-content-sha256;x-amz-date, Signature=",
                      headers["Authorization"])
        self.assertEqual("e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855", headers["x-amz-content-sha256"])

    def test_longhorn_is_pointed_at_the_bucket_not_just_given_its_keys(self):
        """Before 2.8.110 only the keys were written, so a move still found no target."""
        self._service(ip="192.0.2.243")
        store.point_longhorn()
        target = next(b for m, p, b in self.sent if p.endswith("/backuptargets"))
        self.assertEqual(("s3://homestead-backups@us-east-1/", "homestead-backup-credentials"),
                         (target["spec"]["backupTargetURL"], target["spec"]["credentialSecret"]))

    def test_harvester_waits_for_the_store_before_changing_the_target(self):
        self._service("192.0.2.20", "192.0.2.20")
        self._deployment(0)
        with mock.patch.object(store.LH, "on_harvester", return_value=True), \
                mock.patch.object(store, "ensure_bucket") as bucket:
            with self.assertRaisesRegex(ValueError, "still starting.*waiting"):
                store.point_longhorn(replace=True)
        bucket.assert_not_called()
        self.assertEqual([], self.sent, "no credentials or target changed while starting")

    def test_harvester_gets_a_readable_bucket_at_the_chosen_port_before_its_target(self):
        self._service("192.0.2.20", "192.0.2.20")
        self.objects["/api/v1/namespaces/lab/services/homestead-objectstore"]["spec"] = {
            "ports": [{"name": "s3", "port": 9070}]}
        self._deployment(1)
        events = []
        with mock.patch.object(store.LH, "on_harvester", return_value=True), \
                mock.patch.object(store, "ensure_bucket", side_effect=lambda **kw: events.append(("bucket", kw)) or True), \
                mock.patch.object(store.LH, "set_backup_target", side_effect=lambda *args: events.append(("target", args))):
            result = store.point_longhorn(replace=True)
        self.assertEqual(("bucket", {"base": "http://192.0.2.20:9070", "refresh": True}), events[0])
        self.assertEqual("target", events[1][0])
        self.assertEqual("http://192.0.2.20:9070", result["endpoint"])

    def test_an_unreachable_or_unusable_bucket_keeps_the_old_target_and_credentials(self):
        self._service("192.0.2.20", "192.0.2.20")
        self._deployment(1)
        for failure, expected in ((urllib.error.URLError("connection refused"), "not reachable yet"),
                                  (False, "bucket.*not ready")):
            with self.subTest(failure=failure), \
                    mock.patch.object(store.LH, "on_harvester", return_value=True), \
                    mock.patch.object(store, "ensure_bucket", side_effect=failure if isinstance(failure, Exception) else None,
                                      return_value=failure):
                with self.assertRaisesRegex(ValueError, expected):
                    store.point_longhorn(replace=True)
            self.assertEqual([], self.sent)

    def test_a_cached_bucket_does_not_skip_validation_at_a_new_address(self):
        old = dict(store._bucket)
        self.addCleanup(store._bucket.update, old)
        store._bucket["ok"] = True
        with mock.patch.object(store, "_s3", side_effect=[404, 200]) as s3:
            self.assertTrue(store.ensure_bucket("http://192.0.2.20:9070", refresh=True))
        self.assertEqual(["HEAD", "PUT"], [call.args[0] for call in s3.call_args_list])
        self.assertTrue(all(call.args[1] == "http://192.0.2.20:9070/homestead-backups"
                            for call in s3.call_args_list))

    def test_a_target_already_elsewhere_is_left_alone_unless_asked(self):
        self._service(ip="192.0.2.243")
        self.objects["/apis/longhorn.io/v1beta2/namespaces/longhorn-system/backuptargets"] = {"items": [
            {"metadata": {"name": "default", "resourceVersion": "3"},
             "spec": {"backupTargetURL": "nfs://192.0.2.177:/mnt/user/backups"}, "status": {}}]}
        result = store.point_longhorn()
        self.assertIn("nfs://", result["kept_target"])
        self.assertFalse([p for m, p, b in self.sent if "backuptargets" in p])
        self.objects["/apis/longhorn.io/v1beta2/namespaces/longhorn-system/backuptargets/default"] =             self.objects["/apis/longhorn.io/v1beta2/namespaces/longhorn-system/backuptargets"]["items"][0]
        store.point_longhorn(replace=True)
        self.assertTrue([p for m, p, b in self.sent if "backuptargets" in p])

    def test_without_a_lan_address_backups_still_work_and_say_what_they_cannot(self):
        """Refusing would block someone who only wants backups working today."""
        self._service()

        result = store.point_longhorn()

        self.assertEqual("http://homestead-objectstore.lab.svc:9000", result["endpoint"])
        self.assertFalse(result["reachable_off_cluster"])
        self.assertIn("only this cluster can read it", result["detail"])

    def test_pointing_longhorn_at_a_store_that_does_not_exist_is_refused(self):
        with self.assertRaisesRegex(ValueError, "not deployed"):
            store.point_longhorn()

    def test_status_says_when_only_this_cluster_can_reach_it(self):
        self._service()
        self.objects["/apis/apps/v1/namespaces/lab/deployments/homestead-objectstore"] = {
            "metadata": {"name": "homestead-objectstore"}, "status": {"readyReplicas": 1}}

        state = store.status()

        self.assertTrue(state["ready"])
        self.assertFalse(state["reachable_off_cluster"],
                         "without a LAN address no other cluster can restore from it")

    def test_removing_keeps_the_backups_unless_told_otherwise(self):
        self.objects["/apis/apps/v1/namespaces/lab/deployments/homestead-objectstore"] = {}
        self.objects["/api/v1/namespaces/lab/services/homestead-objectstore"] = {}

        store.remove()

        deleted = [p for m, p, _ in self.sent if m == "DELETE"]
        self.assertNotIn("/api/v1/namespaces/lab/persistentvolumeclaims/homestead-objectstore",
                         deleted)

    def test_releasing_the_volume_is_possible_but_explicit(self):
        store.remove(keep_data=False)

        deleted = [p for m, p, _ in self.sent if m == "DELETE"]
        self.assertIn("/api/v1/namespaces/lab/persistentvolumeclaims/homestead-objectstore",
                      deleted)

    def _deployment(self, replicas):
        self.objects["/apis/apps/v1/namespaces/lab/deployments/homestead-objectstore"] = {
            "metadata": {"name": "homestead-objectstore"}, "spec": {"replicas": replicas, "template": {"spec": {"containers": [{}]}}},
            "status": {"readyReplicas": replicas}}

    def test_moves_out_are_off_until_the_store_exists(self):
        self.assertFalse(store.transfers()["allowed"])

    def test_turning_moves_out_on_sets_the_store_up_the_first_time(self):
        with mock.patch.object(store.NETWORK, "service_plan", return_value={"vip": "192.0.2.242"}),                 mock.patch.object(store, "request_target", return_value={}) as point:
            store.set_transfers(True, 50)
        point.assert_called_once()
        self.assertIn("/apis/apps/v1/namespaces/lab/deployments", [p for _, p, _ in self.sent])
        self.assertEqual([("lab", "homestead-objectstore", 50, None, "ReadWriteOnce")], self.claims)

    def test_turning_moves_out_off_stops_the_store_and_keeps_its_volume(self):
        self._deployment(1)
        result = store.set_transfers(False)
        self.assertIn(("PATCH", "/apis/apps/v1/namespaces/lab/deployments/homestead-objectstore", {"spec": {"replicas": 0}}), self.sent)
        self.assertFalse(result["allowed"])
        self.assertFalse(any(m == "DELETE" for m, _, _ in self.sent))

    def test_a_stopped_store_says_so_and_starts_again(self):
        self._deployment(0)
        self.assertTrue(store.status()["stopped"])
        self.assertFalse(store.transfers()["allowed"])
        result = store.set_transfers(True)
        self.assertIn(("PATCH", "/apis/apps/v1/namespaces/lab/deployments/homestead-objectstore", {"spec": {"replicas": 1}}), self.sent)
        self.assertTrue(result["allowed"])
        self.assertEqual([], self.claims)

    def test_enabling_again_points_longhorn_at_the_store(self):
        # The store was created, but pointing Longhorn failed, so
        # its target stayed on the old address until enabled again.
        self._deployment(1)
        self._service("192.0.2.20", "192.0.2.20")
        with mock.patch.object(store, "request_target", return_value={"pending": True, "detail": "Waiting for backup storage"}) as point:
            result = store.set_transfers(True)
        point.assert_called_once_with()
        self.assertIn("Waiting for backup storage", result["detail"])
        with mock.patch.object(store, "request_target", side_effect=ValueError("an S3 target needs its access key and secret key")):
            with self.assertRaisesRegex(ValueError, "Longhorn was not pointed at it"):
                store.set_transfers(True)

    def test_a_silly_size_is_refused(self):
        for size in (1, 99999):
            with self.subTest(size=size):
                with self.assertRaisesRegex(ValueError, "between 5 and"):
                    store.deploy({"size_gb": size})

    def _target_fixture(self):
        self._service("192.0.2.20", "192.0.2.20")
        self._deployment(1)
        deployment = self.objects["/apis/apps/v1/namespaces/lab/deployments/homestead-objectstore"]
        actual = {"url": store.backup_url(), "secret": "harvester-backup-target-secret",
                  "endpoint": "http://192.0.2.10:9000"}
        def save(dep, request):
            dep["metadata"]["annotations"] = {store.TARGET_REQUEST: json.dumps(request)} if request else {}
        for patch in (mock.patch.object(store, "_save_target_request", side_effect=save),
                      mock.patch.object(store, "_target_location", side_effect=lambda: dict(actual))):
            patch.start()
            self.addCleanup(patch.stop)
        return deployment, actual

    def test_a_pending_switch_survives_startup_and_waits_for_the_effective_endpoint(self):
        deployment, actual = self._target_fixture()
        with mock.patch.object(store, "point_longhorn", side_effect=ValueError("storage is still starting")) as point:
            self.assertTrue(store.request_target()["pending"])
            point.assert_not_called()
            store.reconcile_target()
        self.assertEqual("pending", store.target_status(deployment)["state"])
        self.assertFalse(store.target_status(deployment)["pointed"], "the same bucket URL on another server is not this store")
        # No in-memory queue: reload the serialized Kubernetes annotation.
        deployment["metadata"]["annotations"] = json.loads(json.dumps(deployment["metadata"]["annotations"]))
        with mock.patch.object(store, "point_longhorn") as point:
            store.reconcile_target()
            point.assert_called_once_with(replace=False)
        self.assertEqual("applying", store.target_status(deployment)["state"])
        actual["endpoint"] = store.endpoint()
        with mock.patch.object(store, "point_longhorn") as point:
            store.reconcile_target()
            point.assert_not_called()
        self.assertEqual("complete", store.target_status(deployment)["state"])
        self.assertTrue(store.target_status(deployment)["pointed"])

    def test_a_failed_switch_can_be_retried_without_redeploying_storage(self):
        deployment, _ = self._target_fixture()
        store.request_target(replace=True)
        with mock.patch.object(store.time, "time", return_value=store.time.time() + 601), \
                mock.patch.object(store, "point_longhorn") as point:
            store.reconcile_target()
            point.assert_not_called()
        self.assertEqual("failed", store.target_status(deployment)["state"])
        store.request_target(replace=True)
        self.assertEqual("pending", store.target_status(deployment)["state"])
        self.assertEqual([], self.claims)
        self.assertEqual([], self.sent, "only the annotation is changed by a retry")

    def test_a_pending_switch_does_not_overwrite_a_later_target_choice(self):
        deployment, actual = self._target_fixture()
        store.request_target(replace=True)
        actual["endpoint"] = "http://another-store:9000"
        with mock.patch.object(store, "point_longhorn") as point:
            store.reconcile_target()
            point.assert_not_called()
        self.assertEqual("failed", store.target_status(deployment)["state"])
        self.assertIn("changed after", store.target_status(deployment)["detail"])

    def test_a_changed_store_address_requires_a_new_request(self):
        deployment, _ = self._target_fixture()
        store.request_target()
        self._service("192.0.2.21", "192.0.2.21")
        with mock.patch.object(store, "point_longhorn") as point:
            store.reconcile_target()
            point.assert_not_called()
        self.assertEqual("failed", store.target_status(deployment)["state"])

    def test_a_store_without_a_request_does_not_change_longhorn(self):
        self._target_fixture()
        with mock.patch.object(store, "point_longhorn") as point:
            store.reconcile_target()
            point.assert_not_called()

    def test_stopping_moves_clears_a_pending_switch(self):
        deployment, _ = self._target_fixture()
        store.request_target()
        store.set_transfers(False)
        self.assertEqual({}, store._target_request(deployment))

    def test_an_external_target_needs_explicit_replacement(self):
        deployment, actual = self._target_fixture()
        actual.update(url="nfs://nas:/backups", endpoint="")
        self.assertEqual(actual["url"], store.request_target()["kept_target"])
        self.assertEqual({}, store._target_request(deployment))
        self.assertTrue(store.request_target(replace=True)["pending"])


if __name__ == "__main__":
    unittest.main()
