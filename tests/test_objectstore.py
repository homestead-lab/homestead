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
                                 side_effect=lambda cfg, **kw: self.plans.append(cfg) or {"vip": "192.168.1.242"})
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
        store.deploy({"size_gb": 200, "lb_ip": "192.168.1.243", "point_longhorn": False})

        self.assertEqual([("lab", "homestead-objectstore", 200, None, "ReadWriteOnce")],
                         self.claims)
        created = [p for _, p, _ in self.sent]
        self.assertIn("/apis/apps/v1/namespaces/lab/deployments", created)
        self.assertIn("/api/v1/namespaces/lab/services", created)
        service = next(b for _, p, b in self.sent if p.endswith("/services"))
        self.assertEqual("LoadBalancer", service["spec"]["type"])
        self.assertEqual("192.168.1.243",
                         service["metadata"]["annotations"]["kube-vip.io/loadbalancerIPs"])

    def _sent_service(self):
        return [b for _, p, b in self.sent if "/services" in p][-1]

    def test_with_no_address_asked_for_it_shares_homesteads(self):
        store.deploy({"point_longhorn": False})
        self.assertEqual("192.168.1.242", self._sent_service()["metadata"]["annotations"]["kube-vip.io/loadbalancerIPs"])
        # Checked as an app sharing the address would be: both its ports, on the shared address.
        self.assertEqual("shared", self.plans[0]["vip_mode"])
        self.assertEqual([9000, 9001], [p["port"] for p in self.plans[0]["ports"]])

    def test_where_services_go_on_the_nodes_it_goes_there_too(self):
        store.NETWORK.service_plan.side_effect = lambda cfg, **kw: {"vip": ""}
        store.deploy({"point_longhorn": False})
        self.assertEqual({}, self._sent_service()["metadata"]["annotations"])

    def test_a_port_taken_on_the_shared_address_says_so(self):
        store.NETWORK.service_plan.side_effect = ValueError("192.168.1.242:9000/TCP is already used by lab/minio")
        with self.assertRaises(ValueError) as caught:
            store.deploy({"point_longhorn": False})
        self.assertIn("lab/minio", str(caught.exception))
        self.assertIn("address of its own", str(caught.exception))

    def test_a_store_already_running_keeps_its_address(self):
        self._service("192.168.1.244", "192.168.1.244")
        store.deploy({"point_longhorn": False})
        self.assertEqual("192.168.1.244", self._sent_service()["metadata"]["annotations"]["kube-vip.io/loadbalancerIPs"])
        self.assertEqual([], self.plans)

    def test_a_store_can_be_moved_to_the_shared_address_on_purpose(self):
        self._service()
        store.deploy({"point_longhorn": False, "vip_mode": "shared"})
        self.assertEqual("192.168.1.242", self._sent_service()["metadata"]["annotations"]["kube-vip.io/loadbalancerIPs"])

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
        self._service(ip="192.168.1.243")

        result = store.point_longhorn()

        self.assertEqual("http://192.168.1.243:9000", result["endpoint"])
        secret = next(b for _, p, b in self.sent
                      if p == "/api/v1/namespaces/longhorn-system/secrets")
        self.assertEqual("http://192.168.1.243:9000",
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
        self._service(ip="192.168.1.243")
        store.point_longhorn()
        target = next(b for m, p, b in self.sent if p.endswith("/backuptargets"))
        self.assertEqual(("s3://homestead-backups@us-east-1/", "homestead-backup-credentials"),
                         (target["spec"]["backupTargetURL"], target["spec"]["credentialSecret"]))

    def test_a_target_already_elsewhere_is_left_alone_unless_asked(self):
        self._service(ip="192.168.1.243")
        self.objects["/apis/longhorn.io/v1beta2/namespaces/longhorn-system/backuptargets"] = {"items": [
            {"metadata": {"name": "default", "resourceVersion": "3"},
             "spec": {"backupTargetURL": "nfs://192.168.1.177:/mnt/user/backups"}, "status": {}}]}
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
        with mock.patch.object(store.NETWORK, "service_plan", return_value={"vip": "192.168.1.242"}),                 mock.patch.object(store, "point_longhorn", return_value={}) as point:
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

    def test_a_silly_size_is_refused(self):
        for size in (1, 99999):
            with self.subTest(size=size):
                with self.assertRaisesRegex(ValueError, "between 5 and"):
                    store.deploy({"size_gb": size})


if __name__ == "__main__":
    unittest.main()
