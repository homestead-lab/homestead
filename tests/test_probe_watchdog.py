"""The node probe's watchdog salvages Homestead's data volume, and nothing
else, when Longhorn's auto-salvage will not (2026-11-10: an undrained restart,
and the only copy on a disk below Longhorn's free-space threshold)."""
import copy
import json
import sys
import tempfile
import unittest
import urllib.error
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "server" / "probe"))
import homestead_watchdog as W

GiB = 1024 ** 3
CONF = {"namespace": "lab", "deployment": "homestead", "claims": ["homestead-data"]}


def volume(name="pvc-data", claim="homestead-data", users=None, state="detached", robustness="faulted", replicas=None):
    return {"name": name, "state": state, "robustness": robustness,
            "kubernetesStatus": {"namespace": "lab", "pvcName": claim, "workloadsStatus": users if users is not None else [
                {"podName": "homestead-7d9f8-abcde", "podStatus": "Pending", "workloadName": "homestead-7d9f8", "workloadType": "ReplicaSet"}]},
            "replicas": replicas if replicas is not None else [
                {"name": "pvc-data-r-1", "hostId": "node1", "diskID": "u-sata", "diskPath": "/mnt/sata", "failedAt": "2026-11-10T08:00:00Z"}]}


def node(name="node1", ready=True, allow=True, disks=None):
    return {"name": name, "allowScheduling": allow,
            "conditions": {"Ready": {"type": "Ready", "status": "True" if ready else "False"}},
            "disks": disks if disks is not None else {
                "sata": {"path": "/mnt/sata", "diskUUID": "u-sata", "allowScheduling": True, "storageAvailable": 20 * GiB,
                         "storageMaximum": 100 * GiB, "conditions": {"Schedulable": {"status": "False", "reason": "DiskPressure"}}},
                "nvme": {"path": "/mnt/nvme", "diskUUID": "u-nvme", "allowScheduling": True, "storageAvailable": 300 * GiB,
                         "storageMaximum": 400 * GiB, "conditions": {"Schedulable": {"status": "True"}}}}}


class Longhorn:
    def __init__(self):
        self.volumes, self.nodes, self.posts, self.refuse = [volume()], [node()], [], None

    def get(self, path):
        if path == "/volumes":
            return {"data": copy.deepcopy(self.volumes)}
        if path == "/nodes":
            return {"data": copy.deepcopy(self.nodes)}
        if path.startswith("/settings/"):
            return {"value": "25"}
        raise AssertionError(path)

    def post(self, path, body):
        self.posts.append((path, body))
        if self.refuse:
            raise self.refuse
        return {}


class WatchdogTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        (self.dir / "conf.json").write_text(json.dumps(CONF))
        (self.dir / "boot_id").write_text("boot-1\n")
        self.lh, self.now = Longhorn(), 1000.0
        self.dog = self.make("node1")

    def make(self, me):
        return W.Watchdog(me, get=self.lh.get, post=self.lh.post, clock=lambda: self.now,
                          conf=str(self.dir / "conf.json"), state=str(self.dir / "state.json"), boot_id=str(self.dir / "boot_id"))

    def wait_out(self, dog=None):
        dog = dog or self.dog
        self.assertEqual("waiting", dog.step())
        self.now += W.PATIENCE
        return dog.step()

    def record(self):
        return json.loads((self.dir / "state.json").read_text())["salvaged"]

    def test_the_stuck_volume_is_salvaged_after_longhorn_had_its_turn_and_recorded(self):
        self.assertEqual("salvaged", self.wait_out())
        self.assertEqual([("/volumes/pvc-data?action=salvage", {"names": ["pvc-data-r-1"]})], self.lh.posts)
        (entry,) = self.record()
        self.assertEqual(("boot-1", "node1", "pvc-data", "lab/homestead-data", "/mnt/sata", 20 * GiB, 100 * GiB, 25, "accepted"),
                         tuple(entry[k] for k in ("boot_id", "node", "volume", "claim", "path", "available", "maximum", "minimal", "result")))
        self.assertEqual("salvaged", self.dog.snapshot()["state"])
        self.assertEqual(1, len(self.dog.snapshot()["salvaged"]))

    def test_once_per_boot_even_if_it_faults_again_and_again_after_a_restart(self):
        self.wait_out()
        self.now += 3600
        self.assertEqual("done", self.dog.step())
        self.assertEqual("done", self.make("node1").step(), "a restarted probe reads the record on the host")
        self.assertEqual(1, len(self.lh.posts))
        (self.dir / "boot_id").write_text("boot-2\n")
        dog = self.make("node1")
        self.assertEqual("salvaged", self.wait_out(dog))
        self.assertEqual(2, len(self.lh.posts))

    def test_a_lost_answer_is_never_sent_again_and_a_refusal_is_said(self):
        self.lh.refuse = TimeoutError("timed out")
        self.assertEqual("failed", self.wait_out())
        self.assertTrue(self.record()[0]["result"].startswith("uncertain"))
        self.now += 600
        self.assertEqual("done", self.dog.step())
        self.assertEqual(1, len(self.lh.posts))
        (self.dir / "boot_id").write_text("boot-2\n")
        self.lh.refuse = urllib.error.HTTPError("/volumes", 409, "conflict", {}, None)
        self.assertEqual("failed", self.wait_out(self.make("node1")))
        self.assertEqual("refused (HTTP 409)", self.record()[-1]["result"])

    def test_no_record_folder_means_no_salvage(self):
        self.dog.state = str(self.dir / "missing" / "state.json")
        self.dog.step()
        self.now += W.PATIENCE
        with self.assertRaises(OSError):
            self.dog.step()
        self.assertEqual([], self.lh.posts)

    def test_only_the_probe_on_the_copys_host_acts(self):
        self.lh.nodes.append(node("node0"))
        other = self.make("node0")
        self.assertEqual("watching", other.step())
        self.now += W.PATIENCE
        self.assertEqual("watching", other.step())
        self.assertEqual([], self.lh.posts)

    def test_it_waits_while_any_condition_is_missing(self):
        cases = {
            "longhorn salvages it itself": lambda: self.lh.volumes[0]["replicas"].append(
                {"name": "r-2", "hostId": "node1", "diskID": "u-nvme", "failedAt": "x"}),
            "attached": lambda: self.lh.volumes[0].update(state="attached"),
            "healthy": lambda: self.lh.volumes[0].update(robustness="healthy"),
            "homestead is running": lambda: self.lh.volumes[0]["kubernetesStatus"]["workloadsStatus"][0].update(podStatus="Running"),
            "a data move's copy job uses it": lambda: self.lh.volumes[0]["kubernetesStatus"]["workloadsStatus"].append(
                {"podName": "homestead-data-copy-1", "podStatus": "Pending", "workloadName": "homestead-data-copy-1", "workloadType": "Job"}),
            "nothing uses it": lambda: self.lh.volumes[0]["kubernetesStatus"].update(workloadsStatus=[]),
            "another app's volume": lambda: self.lh.volumes[0]["kubernetesStatus"].update(pvcName="jellyfin-config"),
            "its host is down": lambda: self.lh.nodes[0]["conditions"]["Ready"].update(status="False"),
            "homestead named nothing": lambda: (self.dir / "conf.json").write_text("{}"),
        }
        for label, change in cases.items():
            with self.subTest(label):
                self.lh, self.now = Longhorn(), 1000.0
                (self.dir / "conf.json").write_text(json.dumps(CONF))
                change()
                dog = self.make("node1")
                dog.step()
                self.now += W.PATIENCE
                self.assertIn(dog.step(), ("watching", "idle"))
                self.assertEqual([], self.lh.posts)

    def test_crd_style_conditions_and_a_host_that_takes_no_copies_count_too(self):
        self.lh.nodes = [node(allow=False, disks={"sata": {"path": "/mnt/sata", "diskUUID": "u-sata", "allowScheduling": True,
                                                            "conditions": [{"type": "Schedulable", "status": "True"}]}})]
        self.lh.nodes[0]["conditions"] = [{"type": "Ready", "status": "True"}]
        self.assertEqual("salvaged", self.wait_out())

    def test_longhorn_not_answering_is_said_and_resets_the_wait(self):
        self.assertEqual("waiting", self.dog.step())
        self.dog.get = lambda path: (_ for _ in ()).throw(OSError("no route"))
        self.assertEqual("unavailable", self.dog.step())
        self.assertIn("no route", self.dog.snapshot()["detail"])
        self.lh = Longhorn()
        self.dog.get, self.dog.post = self.lh.get, self.lh.post
        self.now += W.PATIENCE
        self.assertEqual("waiting", self.dog.step(), "the wait starts again")


class AlertTests(unittest.TestCase):
    """Once Homestead is back, it says what the watchdog did (design 3.3)."""
    def setUp(self):
        sys.path.insert(0, str(ROOT / "server"))
        import homestead_probe
        self.probe = homestead_probe
        self.entry = {"boot_id": "b", "at": 1_800_000_000.0, "node": "node1", "volume": "pvc-data", "claim": "lab/homestead-data",
                      "replicas": ["r-1"], "path": "/mnt/sata", "available": 20 * GiB, "maximum": 100 * GiB, "minimal": 25,
                      "result": "accepted"}

    def facts(self, entry, now=1_800_000_600.0):
        return self.probe.watchdog_alert_facts({"node1": {"watchdog": {"salvaged": [entry]}}, "node2": {}}, now)

    def test_a_salvage_is_said_with_the_disk_and_the_fix(self):
        (fact,) = self.facts(self.entry)
        self.assertEqual("Homestead's data volume was salvaged after a restart", fact["title"])
        self.assertTrue(fact["body"].startswith("Disk /mnt/sata on node1 is below Longhorn's free space threshold: "
                                                "20.0 GiB free of 100.0 GiB, and Longhorn keeps 25% free"))
        self.assertIn("The node probe on node1 salvaged it at 2027-01-15 08:00 UTC.", fact["body"])
        self.assertIn("Move copies on Volumes", fact["body"])
        self.assertEqual(("watchdog-salvage:pvc-data:1800000000", "degraded"), (fact["key"], fact["severity"]))

    def test_an_unconfirmed_salvage_says_so_and_a_week_old_one_is_gone(self):
        (fact,) = self.facts(dict(self.entry, result="uncertain (timed out)", available=80 * GiB))
        self.assertIn("was not confirmed", fact["title"])
        self.assertIn("the request was uncertain (timed out). It is not tried again until that host restarts.", fact["body"])
        self.assertTrue(fact["body"].startswith("Longhorn does not place copies on disk /mnt/sata on node1, "))
        self.assertEqual([], self.facts(self.entry, now=self.entry["at"] + 8 * 86400))
        self.assertEqual([], self.facts({"at": "garbage"}))


if __name__ == "__main__":
    unittest.main()
