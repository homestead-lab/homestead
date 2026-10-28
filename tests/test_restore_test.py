import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "server"))

import homestead_kube as K
import homestead_names as NAMES
import homestead_changes as CHANGES
import homestead_host_access as HOSTACCESS
import homestead_operations as OPS
import homestead_restore_test as RT
from fakes.kube import Kube

DEPLOYMENT = "/apis/apps/v1/namespaces/lab/deployments/paperless"


def deployment():
    return {"apiVersion": "apps/v1", "kind": "Deployment",
            "metadata": {"name": "paperless", "namespace": "lab", "labels": {"app": "paperless"},
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



def claim(name, volume):
    return {"apiVersion": "v1", "kind": "PersistentVolumeClaim", "metadata": {"name": name, "namespace": "lab"},
            "spec": {"volumeName": volume, "storageClassName": "longhorn"}}


def volume(name):
    return {"apiVersion": "v1", "kind": "PersistentVolume", "metadata": {"name": name},
            "spec": {"csi": {"driver": "driver.longhorn.io", "volumeHandle": name}}}


class Cluster:
    """What a restore test reads and writes, on the shared fake API (tests/fakes/kube.py)."""
    def __init__(self, case):
        # The shared fake runs the real write guards; the storage guard takes the
        # job store's lock, which lives under its DATA_DIR.
        case.enterContext(mock.patch.object(OPS, "DATA_DIR", case.enterContext(tempfile.TemporaryDirectory())))
        # No signed-in person on this thread, whatever an earlier test left behind: the write guards read who is asking.
        case.enterContext(mock.patch.object(HOSTACCESS, "role", lambda: None))
        case.enterContext(mock.patch.object(CHANGES.REQUEST, "user", None, create=True))
        # And no signing key for the change note, so no Deployment write carries a changedBy mark: with
        # the key bound by an earlier test it would, and the bodies these tests compare would differ.
        case.enterContext(mock.patch.object(CHANGES, "_author_key", mock.Mock(side_effect=OSError("no key"))))
        self.kube = case.enterContext(Kube(
            deployment(), claim("paperless-data", "pvc-data"), claim("paperless-cache", "pvc-cache"),
            volume("pvc-data"), volume("pvc-cache")))
        self.restored, self.restore_state = [], "succeeded"
        self.answer = {"ok": True, "port": 8000}

    @property
    def sent(self):
        return [(method, path, body) for method, path, body, _ in self.kube.writes]

    def set_pod(self, item, status):
        """The copy's pod, as Kubernetes keeps it: found by the label the test puts on it."""
        self.kube.add({"apiVersion": "v1", "kind": "Pod",
                       "metadata": {"name": "restore-test-pod", "namespace": "lab",
                                    "labels": {NAMES.key(RT.LABEL): item["id"]}},
                       "status": status})

    def change_deployment(self, change):
        found = self.kube.object(DEPLOYMENT)
        change(found)
        self.kube.add(found)


class Base(unittest.TestCase):
    def setUp(self):
        self.c = Cluster(self)
        backups = {"pvc-data": [{"name": "backup-data-2", "restorable": True, "created": "2026-10-06", "volume_size_gb": 20},
                                {"name": "backup-data-1", "restorable": True, "created": "2026-10-01", "volume_size_gb": 20}],
                   "pvc-cache": [{"name": "backup-cache-1", "restorable": False}]}

        def restore(cfg):
            self.c.restored.append(cfg)
            self.c.kube.add({"apiVersion": "v1", "kind": "PersistentVolumeClaim",
                             "metadata": {"namespace": cfg["namespace"], "name": cfg["name"]}})
            return {"ok": True}
        for name in ("kget", "ksend"):
            self.addCleanup(setattr, RT, name, getattr(RT, name))
        RT.bind(K.kget, K.ksend, NAMES, lambda volume: backups.get(volume, []), restore,
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
        self.c.set_pod(self.item, {"podIP": ip, "conditions": [{"type": "Ready", "status": "True"}]})


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
        self.c.set_pod(self.item, {"containerStatuses": [{"name": "app", "state": {"waiting": {"reason": "CrashLoopBackOff"}}}]})
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
        self.c.change_deployment(lambda dep: dep["spec"]["template"]["spec"].update(volumes=[]))
        self.assertEqual("succeeded", self.step(1000)[0])
        self.assertEqual([], self.c.restored)



class SettingTests(unittest.TestCase):
    def test_off_by_default_and_only_true_or_false(self):
        import server
        self.assertEqual({"enabled": False}, server.APP_SETTINGS.validate({})["restore_tests"])
        self.assertEqual({"enabled": True}, server.APP_SETTINGS.validate({"restore_tests": {"enabled": True}})["restore_tests"])
        with self.assertRaises(ValueError):
            server.APP_SETTINGS.validate({"restore_tests": {"enabled": "yes"}})


if __name__ == "__main__":
    unittest.main()
