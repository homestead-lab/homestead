import json, sys, time, unittest, urllib.error
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
import homestead_longhorn as LH
import homestead_revert as REVERT

API = REVERT.API


class FakeReclass:
    def __init__(self, cluster):
        self.cluster = cluster
        self.started = []

    def consumers(self, ns, claim):
        return [{"kind": "Deployment", "name": "paperless", "replicas": 1, "running": True}]

    def _using_pods(self, ns, claim, helpers=True):
        return [{"metadata": {"name": "paperless-abc"}}] if self.cluster.pod_running else []

    def _start(self, ns, ref):
        self.started.append([c["name"] for c in ref["consumers"]])

    def _started(self, ns, ref):
        return []


class RevertTests(unittest.TestCase):
    """Snapshots could be taken and deleted but never rolled back to."""

    def setUp(self):
        self.state, self.pod_running, self.sent, self.calls = "attached", True, [], []
        self.objects = {
            f"{API}/volumes/pvc-1": {"status": {"kubernetesStatus": {"namespace": "lab", "pvcName": "paperless-data"}}},
            f"{API}/snapshots/snap-a": {"spec": {"volume": "pvc-1"}, "status": {"creationTime": "2026-09-24T02:00:00Z",
                                                                                  "readyToUse": True}},
            f"{API}/replicas": {"items": [{"spec": {"volumeName": "pvc-1", "nodeID": "node2"},
                                           "status": {"currentState": "stopped"}}]},
        }
        self.reclass = FakeReclass(self)
        REVERT.bind(self.get, self.send, self.reclass, lambda ns, name: name == "homestead",
                    lambda method, path, body=None: self.calls.append((path, body)) or {})

    def get(self, path):
        if path == f"{API}/volumes/pvc-1":
            self.objects[path]["status"]["state"] = self.state
        if path in self.objects:
            return self.objects[path]
        raise urllib.error.HTTPError(path, 404, "missing", {}, None)

    def send(self, method, path, body=None, **kw):
        self.sent.append((method, path, body))

    def test_the_plan_names_what_stops(self):
        plan = REVERT.plan("pvc-1", "snap-a")
        self.assertTrue(plan["ready"])
        self.assertEqual(("lab", "paperless-data", ["paperless"]),
                         (plan["namespace"], plan["claim"], [c["name"] for c in plan["consumers"]]))

    def test_it_stops_waits_attaches_keeps_the_present_reverts_and_starts_again(self):
        item = {"ref": {"volume": "pvc-1", "snapshot": "snap-a", "namespace": "lab", "claim": "paperless-data",
                        "consumers": REVERT.plan("pvc-1", "snap-a")["consumers"], "phase": "stopping",
                        "since": time.time()}}
        status, _, message = REVERT.resolve(item)
        self.assertEqual("running", status)
        self.assertIn(("PATCH", "/apis/apps/v1/namespaces/lab/deployments/paperless", {"spec": {"replicas": 0}}),
                      self.sent)
        self.assertEqual([], self.calls, "nothing is attached while the app still has it")
        self.pod_running, self.state = False, "detached"
        REVERT.resolve(item)
        self.assertEqual("/volumes/pvc-1?action=attach", self.calls[0][0])
        self.assertTrue(self.calls[0][1]["disableFrontend"])
        self.assertEqual("node2", self.calls[0][1]["hostId"])
        self.state = "attached"
        REVERT.resolve(item)
        actions = [path.split("action=")[1] for path, _ in self.calls]
        self.assertEqual(["attach", "snapshotCreate", "snapshotRevert", "detach"], actions)
        self.assertEqual("snap-a", self.calls[2][1]["name"])
        self.state = "detached"
        REVERT.resolve(item)
        self.assertEqual([["paperless"]], self.reclass.started)
        status, _, message = REVERT.resolve(item)
        self.assertEqual("succeeded", status)
        self.assertIn(item["ref"]["kept"], message)

    def test_a_volume_that_will_not_detach_is_put_back_untouched(self):
        item = {"ref": {"volume": "pvc-1", "snapshot": "snap-a", "namespace": "lab", "claim": "paperless-data",
                        "consumers": REVERT.plan("pvc-1", "snap-a")["consumers"], "phase": "stopping",
                        "since": time.time() - 3600}}
        status, _, message = REVERT.resolve(item)
        self.assertEqual("failed", status)
        self.assertIn("Nothing was changed", message)
        self.assertEqual([["paperless"]], self.reclass.started)
        self.assertEqual([], self.calls)

    def test_homesteads_own_data_is_refused(self):
        self.reclass.consumers = lambda ns, claim: [{"kind": "Deployment", "name": "homestead", "replicas": 1}]
        self.pod_running = False
        with self.assertRaisesRegex(ValueError, "Homestead's own data"):
            REVERT.start("pvc-1", "snap-a", None)


class ProtectionHealthTests(unittest.TestCase):
    """A failing backup job said so only on its own page."""

    def test_failed_jobs_and_an_unreachable_target_are_issues(self):
        import server
        jobs = [{"name": "daily-backup", "task": "backup", "covers": 3, "last_failed": True},
                {"name": "weekly-trim", "task": "filesystem-trim", "covers": 3, "last_failed": True},
                {"name": "daily-snapshot", "task": "snapshot", "covers": 0, "last_failed": True}]
        issues = server.protection_issues(jobs, {"configured": True, "available": False, "url": "nfs://nas:/b",
                                                 "reason": "connection refused"})
        self.assertEqual(["daily-backup", "backup target"], [i["name"] for i in issues])
        self.assertIn("connection refused", issues[1]["reason"])
        self.assertIn("no backup target", server.protection_issues(jobs[:1], {"configured": False})[1]["reason"])
        result = server.classify_cluster_health([], [], [], protection=issues)
        self.assertEqual("degraded", result["health"])


class HarvesterTargetTests(unittest.TestCase):
    """On Harvester the target is Harvester's setting, or Harvester resets it."""

    def setUp(self):
        self.sent = []
        self.setting = {"metadata": {"name": "backup-target"}, "value": ""}

        def get(path):
            if path == LH.HARVESTER_TARGET:
                return self.setting
            raise urllib.error.HTTPError(path, 404, "missing", {}, None)
        LH.bind(get, lambda m, p, b=None, **k: self.sent.append((m, p, b)), {})

    def test_nfs_and_s3_are_written_to_harvesters_setting(self):
        LH.set_backup_target("nfs://192.168.1.177:/mnt/user/backups", poll="5m")
        method, path, body = self.sent[-1]
        self.assertEqual(("PUT", LH.HARVESTER_TARGET), (method, path))
        self.assertEqual({"type": "nfs", "endpoint": "nfs://192.168.1.177:/mnt/user/backups",
                          "refreshIntervalInSeconds": 300}, json.loads(body["value"]))
        LH.set_backup_target("s3://homelab@us-east-1", keys={"access_key": "AK", "secret_key": "SK",
                                                             "endpoint": "http://192.168.1.20:9000"})
        value = json.loads(self.sent[-1][2]["value"])
        self.assertEqual(("s3", "homelab", "us-east-1", "http://192.168.1.20:9000", "AK"),
                         (value["type"], value["bucketName"], value["bucketRegion"], value["endpoint"], value["accessKeyId"]))
        self.assertFalse(any("backuptargets" in p for _, p, _ in self.sent), "Longhorn's own is left to Harvester")

    def test_keys_are_kept_when_none_are_typed_and_a_path_is_refused(self):
        self.setting["value"] = json.dumps({"type": "s3", "accessKeyId": "AK", "secretAccessKey": "SK", "endpoint": "http://x:9000"})
        LH.set_backup_target("s3://homelab@us-east-1", secret="whatever")
        value = json.loads(self.sent[-1][2]["value"])
        self.assertEqual(("AK", "SK", "http://x:9000"), (value["accessKeyId"], value["secretAccessKey"], value["endpoint"]))
        with self.assertRaisesRegex(ValueError, "leave any path off"):
            LH.set_backup_target("s3://homelab@us-east-1/longhorn", keys={"access_key": "A", "secret_key": "S"})


    def test_a_secret_named_alone_gives_its_keys_to_harvesters_setting(self):
        # Backup storage for a move names the Secret it wrote; Harvester's
        # setting has nowhere to put a name, so the keys are read from it.
        import base64
        enc = lambda v: base64.b64encode(v.encode()).decode()
        secret = {"data": {"AWS_ACCESS_KEY_ID": enc("homestead"), "AWS_SECRET_ACCESS_KEY": enc("s3cret"),
                           "AWS_ENDPOINTS": enc("http://192.168.1.211:9010")}}

        def get(path):
            if path == LH.HARVESTER_TARGET:
                return self.setting
            if path.endswith("/secrets/homestead-backup-credentials"):
                return secret
            raise urllib.error.HTTPError(path, 404, "missing", {}, None)
        LH.bind(get, lambda m, p, b=None, **k: self.sent.append((m, p, b)), {})
        LH.set_backup_target("s3://homestead-backups@us-east-1/", secret="homestead-backup-credentials")
        value = json.loads(self.sent[-1][2]["value"])
        self.assertEqual(("homestead", "s3cret", "http://192.168.1.211:9010", "homestead-backups"),
                         (value["accessKeyId"], value["secretAccessKey"], value["endpoint"], value["bucketName"]))


if __name__ == "__main__":
    unittest.main()
