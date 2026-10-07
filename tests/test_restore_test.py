import copy
import json
import sys
import unittest
import urllib.error
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "server"))

import homestead_names as NAMES
import homestead_restore_test as RT


def deployment():
    return {"metadata": {"name": "paperless", "namespace": "lab", "labels": {"app": "paperless"},
                         "annotations": {}},
            "spec": {"template": {"metadata": {"labels": {"app": "paperless"},
                                               "annotations": {"k8s.v1.cni.cncf.io/networks": "default/lan"}},
                                  "spec": {
                "hostNetwork": True, "dnsPolicy": "ClusterFirstWithHostNet", "nodeName": "node2",
                "nodeSelector": {"gpu": "yes"}, "serviceAccountName": "paperless",
                "containers": [{"name": "app", "image": "example/paperless:2",
                                "ports": [{"containerPort": 8000, "hostPort": 8000}, {"containerPort": 53, "protocol": "UDP"}],
                                "resources": {"requests": {"cpu": "100m", "memory": "512Mi", "gpu.intel.com/i915": "1"},
                                              "limits": {"memory": "1Gi", "squat.ai/coral": "1"}}}],
                "volumes": [{"name": "data", "persistentVolumeClaim": {"claimName": "paperless-data"}},
                            {"name": "cache", "persistentVolumeClaim": {"claimName": "paperless-cache"}},
                            {"name": "media", "nfs": {"server": "192.0.2.5", "path": "/media"}},
                            {"name": "dev", "hostPath": {"path": "/dev/dri"}},
                            {"name": "conf", "configMap": {"name": "paperless-conf"}},
                            {"name": "env", "secret": {"secretName": "paperless-env"}}]}}}}


class Cluster:
    """A fake of what a restore test reads and writes."""
    def __init__(self):
        self.objects = {
            "/apis/apps/v1/namespaces/lab/deployments/paperless": deployment(),
            "/api/v1/namespaces/lab/persistentvolumeclaims/paperless-data": {"spec": {"volumeName": "pvc-data", "storageClassName": "longhorn"}},
            "/api/v1/namespaces/lab/persistentvolumeclaims/paperless-cache": {"spec": {"volumeName": "pvc-cache", "storageClassName": "longhorn"}},
            "/api/v1/persistentvolumes/pvc-data": {"spec": {"csi": {"driver": "driver.longhorn.io", "volumeHandle": "pvc-data"}}},
            "/api/v1/persistentvolumes/pvc-cache": {"spec": {"csi": {"driver": "driver.longhorn.io", "volumeHandle": "pvc-cache"}}},
        }
        self.sent, self.restored, self.restore_state, self.pod = [], [], "succeeded", None
        self.answer = {"ok": True, "port": 8000}

    def get(self, path):
        if "/pods?labelSelector=" in path:
            return {"items": [self.pod] if self.pod else []}
        if path in self.objects:
            return copy.deepcopy(self.objects[path])
        raise urllib.error.HTTPError(path, 404, "not found", {}, None)

    def send(self, method, path, body=None, ctype=None):
        self.sent.append((method, path, body))
        if method == "POST":
            self.objects[path.rstrip("s") + "/" + body["metadata"]["name"] if False else f"{path}/{body['metadata']['name']}"] = body
        if method == "DELETE":
            self.objects.pop(path, None)
        return {}


class Base(unittest.TestCase):
    def setUp(self):
        self.c = Cluster()
        backups = {"pvc-data": [{"name": "backup-data-2", "restorable": True, "created": "2026-10-06", "volume_size_gb": 20},
                                {"name": "backup-data-1", "restorable": True, "created": "2026-10-01", "volume_size_gb": 20}],
                   "pvc-cache": [{"name": "backup-cache-1", "restorable": False}]}

        def restore(cfg):
            self.c.restored.append(cfg)
            path = f"/api/v1/namespaces/{cfg['namespace']}/persistentvolumeclaims/{cfg['name']}"
            self.c.objects[path] = {"metadata": {"name": cfg["name"]}}
            return {"ok": True}
        RT.bind(self.c.get, self.c.send, NAMES, lambda volume: backups.get(volume, []), restore,
                lambda item: (self.c.restore_state, 100 if self.c.restore_state == "succeeded" else 40, "restoring"),
                lambda: [], lambda: {"largest": {"1": 500, "2": 300, "3": 200}}, lambda target: self.c.answer, lambda: "lab")
        self.item = {"id": "abcdef0123456789abcdef", "progress": 0,
                     "ref": {"namespace": "lab", "name": "paperless", "phase": "plan"}}
        self.saves = 0

    def step(self, now):
        def checkpoint(item):
            self.saves += 1
        return RT.resolve(self.item, checkpoint, now=now)

    def ready_pod(self, ip="10.42.1.9"):
        self.c.pod = {"status": {"podIP": ip, "conditions": [{"type": "Ready", "status": "True"}]}}


class PlanTests(Base):
    def test_newest_completed_backup_per_longhorn_volume(self):
        rows, without = RT.plan("lab", deployment())
        self.assertEqual([("paperless-data", "backup-data-2", 20)], [(r["claim"], r["backup"], r["size_gb"]) for r in rows])
        self.assertEqual([{"claim": "paperless-cache", "why": "no completed backup"}], without)

    def test_room_at_each_volumes_copies(self):
        rows = [{"size_gb": 100, "storage_class": "longhorn"}]
        classes = {"longhorn": {"numberOfReplicas": "3"}}
        self.assertTrue(RT.room_for(rows, {"largest": {"1": 500, "3": 150}}, classes))
        self.assertFalse(RT.room_for(rows, {"largest": {"1": 500, "3": 100}}, classes), "120 GB needed at three copies")
        self.assertFalse(RT.room_for(rows * 5, {"largest": {"1": 500, "3": 150}}, classes), "and the whole test must fit")


class PickTests(Base):
    def w(me, **extra):
        row = {"ns": "lab", "name": "paperless", "claims": ["paperless-data"], "restore_tested": None}
        row.update(extra)
        return row

    def test_off_in_the_window_and_one_at_a_time(self):
        self.assertIsNone(RT.pick([self.w()], [], False, False, 10**9, {}))
        self.assertIsNone(RT.pick([self.w()], [], True, True, 10**9, {}))
        self.assertIsNone(RT.pick([self.w()], [{"kind": RT.KIND, "status": "running"}], True, False, 10**9, {}))
        self.assertEqual("paperless", RT.pick([self.w()], [], True, False, 10**9, {})["name"])

    def test_tested_this_month_waits(self):
        now = 10**9
        self.assertIsNone(RT.pick([self.w(restore_tested={"at": now - 86400})], [], True, False, now, {}))
        self.assertIsNotNone(RT.pick([self.w(restore_tested={"at": now - RT.EVERY - 1})], [], True, False, now, {}))

    def test_homesteads_own_and_shares_are_never_tested(self):
        for extra in ({"self": True}, {"homestead": "self"}, {"managed_smb": True}, {"platform": True}, {"claims": []}):
            with self.subTest(extra=extra):
                self.assertIsNone(RT.pick([self.w(**extra)], [], True, False, 10**9, {}))


class CopyTests(Base):
    def copy(self):
        rows = [{"claim": "paperless-data", "restored": "restore-test-abcdef0123-0"}]
        return RT.copy_of(deployment(), self.item, rows)

    def test_the_copy_cannot_touch_anything_real(self):
        dep = self.copy()
        spec = dep["spec"]["template"]["spec"]
        volumes = {v["name"]: v for v in spec["volumes"]}
        self.assertEqual({"claimName": "restore-test-abcdef0123-0"}, volumes["data"]["persistentVolumeClaim"])
        for name in ("cache", "media", "dev"):
            self.assertEqual({"name": name, "emptyDir": {}}, volumes[name], f"{name} is replaced by an empty volume")
        self.assertIn("configMap", volumes["conf"])
        self.assertIn("secret", volumes["env"])
        for key in ("hostNetwork", "nodeName", "nodeSelector"):
            self.assertNotIn(key, spec)
        self.assertEqual("ClusterFirst", spec["dnsPolicy"])
        self.assertIs(False, spec["automountServiceAccountToken"])
        container = spec["containers"][0]
        self.assertNotIn("hostPort", container["ports"][0])
        self.assertEqual({"cpu": "100m", "memory": "512Mi"}, container["resources"]["requests"])
        self.assertEqual({"memory": "1Gi"}, container["resources"]["limits"])

    def test_no_projected_service_account_token(self):
        dep = deployment()
        dep["spec"]["template"]["spec"]["volumes"] += [
            {"name": "token", "projected": {"sources": [{"serviceAccountToken": {"path": "token"}}]}},
            {"name": "mixed", "projected": {"sources": [{"serviceAccountToken": {"path": "t"}}, {"configMap": {"name": "c"}}]}}]
        volumes = {v["name"]: v for v in RT.copy_of(dep, self.item, [])["spec"]["template"]["spec"]["volumes"]}
        self.assertEqual({"name": "token", "emptyDir": {}}, volumes["token"])
        self.assertEqual([{"configMap": {"name": "c"}}], volumes["mixed"]["projected"]["sources"])
        self.assertFalse(RT.privileged(dep))
        dep["spec"]["template"]["spec"]["containers"][0]["securityContext"] = {"privileged": True}
        self.assertTrue(RT.privileged(dep))

    def test_no_service_selects_it_and_no_lan_attachment(self):
        dep = self.copy()
        labels = dep["spec"]["template"]["metadata"]["labels"]
        self.assertEqual({"homestead.io/restore-test": self.item["id"]}, labels, "not app=paperless")
        self.assertNotIn("annotations", dep["spec"]["template"]["metadata"])

    def test_the_fence_lets_in_only_homestead_and_out_only_dns(self):
        policy = RT.fence(self.item, "lab")["spec"]
        self.assertEqual(["Ingress", "Egress"], policy["policyTypes"])
        self.assertEqual({"app": "homestead"}, policy["ingress"][0]["from"][0]["podSelector"]["matchLabels"])
        self.assertEqual({53}, {p["port"] for p in policy["egress"][0]["ports"]})
        self.assertNotIn("to", policy["egress"][0])


class JobTests(Base):
    def test_a_passing_test_cleans_up_and_records_on_the_app(self):
        self.assertEqual("running", self.step(1000)[0])
        self.assertEqual(["backup-data-2"], [r["backup"] for r in self.c.restored])
        self.assertEqual("restore-test-abcdef0123-0", self.c.restored[0]["name"])
        self.assertEqual(("running", 70), self.step(1100)[:2])
        created = [p for m, p, _ in self.c.sent if m == "POST"]
        self.assertEqual(["/apis/networking.k8s.io/v1/namespaces/lab/networkpolicies", "/apis/apps/v1/namespaces/lab/deployments"], created,
                         "the fence goes up before the copy")
        self.assertEqual("running", self.step(1110)[0])
        self.ready_pod()
        status, _, message = self.step(1200)
        self.assertEqual("succeeded", status)
        self.assertIn("answered on port 8000", message)
        self.assertIn("paperless-cache (no completed backup)", message)
        deleted = {p.rsplit("/", 1)[-1] for m, p, _ in self.c.sent if m == "DELETE"}
        self.assertEqual({"restore-test-abcdef0123", "restore-test-abcdef0123-0"}, deleted)
        patch = [b for m, p, b in self.c.sent if m == "PATCH"][-1]
        result = json.loads(patch["metadata"]["annotations"]["homestead.io/restore-tested"])
        self.assertTrue(result["ok"])

    def test_a_restore_that_fails_cleans_up_and_fails(self):
        self.c.restore_state = "failed"
        status, _, message = self.step(1000)
        self.assertEqual("failed", status)
        self.assertIn("did not restore", message)
        self.assertIn(("DELETE", "/api/v1/namespaces/lab/persistentvolumeclaims/restore-test-abcdef0123-0", None), self.c.sent)

    def test_a_copy_that_crashes_on_the_restored_data_fails(self):
        self.step(1000)
        self.step(1100)
        self.c.pod = {"status": {"containerStatuses": [{"name": "app", "state": {"waiting": {"reason": "CrashLoopBackOff"}}}]}}
        self.assertEqual("running", self.step(1150)[0], "a crash loop gets a little time")
        status, _, message = self.step(1100 + 181)
        self.assertEqual("failed", status)
        self.assertIn("CrashLoopBackOff", message)
        result = json.loads([b for m, p, b in self.c.sent if m == "PATCH"][-1]["metadata"]["annotations"]["homestead.io/restore-tested"])
        self.assertFalse(result["ok"])

    def test_ready_but_silent_fails_after_a_while(self):
        self.step(1000)
        self.step(1100)
        self.ready_pod()
        self.c.answer = {"ok": False, "error": "connection refused"}
        self.assertEqual("running", self.step(1200)[0])
        status, _, message = self.step(1200 + RT.ANSWER_WAIT + 1)
        self.assertEqual("failed", status)
        self.assertIn("did not answer on 8000", message)

    def test_a_restart_before_the_copy_was_made_makes_it(self):
        self.step(1000)
        self.item["ref"].update(phase="checking", copy_at=1100, ports=[8000])
        self.step(1110)
        self.assertIn("/apis/apps/v1/namespaces/lab/deployments", [p for m, p, _ in self.c.sent if m == "POST"])

    def test_an_app_with_nothing_to_restore_is_not_a_failure(self):
        self.c.objects["/apis/apps/v1/namespaces/lab/deployments/paperless"]["spec"]["template"]["spec"]["volumes"] = []
        self.assertEqual("succeeded", self.step(1000)[0])
        self.assertEqual([], self.c.restored)



class SettingTests(unittest.TestCase):
    def test_off_by_default_and_only_true_or_false(self):
        import server
        self.assertEqual({"enabled": False}, server.validate_app_settings({})["restore_tests"])
        self.assertEqual({"enabled": True}, server.validate_app_settings({"restore_tests": {"enabled": True}})["restore_tests"])
        with self.assertRaises(ValueError):
            server.validate_app_settings({"restore_tests": {"enabled": "yes"}})


if __name__ == "__main__":
    unittest.main()
