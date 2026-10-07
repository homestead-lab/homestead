import copy
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "server"))

import homestead_autoupdate as AU
import homestead_names as NAMES


def workload(name="sonarr", mode="auto", **extra):
    row = {"ns": "lab", "name": name, "update_mode": mode, "desired": 1, "ready": 1}
    row.update(extra)
    return row


def entry(name="sonarr", candidate="lscr.io/linuxserver/sonarr:latest", source="lscr.io/linuxserver/sonarr:latest", **extra):
    row = {"ns": "lab", "name": name, "available": True, "managed": "",
           "images": [{"container": "app", "available": True, "candidate": candidate, "source": source, "error": ""}]}
    row.update(extra)
    return row


class PickTests(unittest.TestCase):
    def setUp(self):
        AU.names = NAMES

    def pick(self, workloads, entries, answers=None, ops=(), window=True, policy="maintenance_window"):
        return AU.pick(workloads, {"workloads": entries}, answers or {}, list(ops), window, policy)

    def test_an_auto_app_with_a_same_tag_update_inside_the_window(self):
        self.assertEqual("sonarr", self.pick([workload()], [entry()])["name"])

    def test_never_outside_the_window_or_when_policy_is_notify_only(self):
        self.assertIsNone(self.pick([workload()], [entry()], window=False))
        self.assertIsNone(self.pick([workload()], [entry()], policy="notify_only"))

    def test_a_new_version_waits_for_someone(self):
        self.assertIsNone(self.pick([workload()], [entry(candidate="lscr.io/linuxserver/sonarr:5.0", source="lscr.io/linuxserver/sonarr:4.0")]))

    def test_what_is_never_updated_on_its_own(self):
        cases = [
            ([workload(mode="manual")], [entry()], {}),
            ([workload()], [entry(managed="flux")], {}),
            ([workload(ready=0)], [entry()], {}),
            ([workload(desired=0, ready=0)], [entry()], {}),
            ([workload(self=True)], [entry()], {}),
            ([workload()], [entry()], {"lab/sonarr": {"state": "down"}}),
            ([workload()], [entry()], {"lab/sonarr": {"state": "unknown"}}),
        ]
        for workloads, entries, answers in cases:
            with self.subTest(case=(workloads, entries, answers)):
                self.assertIsNone(self.pick(workloads, entries, answers))

    def test_an_app_without_a_check_is_judged_by_readiness(self):
        self.assertIsNotNone(self.pick([workload()], [entry()], {"lab/sonarr": {"state": "off", "why": "no address to ask"}}))
        self.assertIsNone(self.pick([workload()], [entry()], {"lab/sonarr": {"state": "off", "why": "stopped"}}))

    def test_one_at_a_time(self):
        self.assertIsNone(self.pick([workload()], [entry()], ops=[{"kind": AU.KIND, "status": "running"}]))
        self.assertIsNotNone(self.pick([workload()], [entry()], ops=[{"kind": AU.KIND, "status": "failed"}]))

    def test_an_image_error_holds_it_back(self):
        bad = entry()
        bad["images"].append({"container": "side", "available": False, "error": "registry authentication required"})
        self.assertIsNone(self.pick([workload()], [bad]))


class FakeUpdates:
    TRACKED = "homestead.io/update-sources"

    def __init__(self, test):
        self.test, self.committed, self.rolled_back, self.available = test, 0, 0, True

    def _pod_containers(self, dep):
        return dep["spec"]["template"]["spec"]["containers"]

    def _annotation_json(self, dep, key):
        import json
        return json.loads((dep["metadata"].get("annotations") or {}).get(key) or "{}")

    def prepare_update(self, ns, name):
        if not self.available:
            raise ValueError("no image update is currently available")
        import json
        current = copy.deepcopy(self.test.dep)
        proposed = copy.deepcopy(current)
        proposed["spec"]["template"]["spec"]["containers"][0]["image"] = "lscr.io/linuxserver/sonarr@sha256:" + "b" * 64
        proposed["metadata"]["annotations"][self.TRACKED] = json.dumps({"app": self.test.candidate})
        return {"current": current, "proposed": proposed, "before": {"app": "lscr.io/linuxserver/sonarr@sha256:" + "a" * 64}}

    def commit_prepared(self, prepared):
        self.committed += 1
        self.available = False
        self.test.dep["metadata"]["generation"] += 1

    def rollback(self, ns, name):
        self.rolled_back += 1


class JobTests(unittest.TestCase):
    def setUp(self):
        import json
        self.candidate = "lscr.io/linuxserver/sonarr:latest"
        self.dep = {"metadata": {"name": "sonarr", "namespace": "lab", "generation": 4,
                                 "annotations": {"homestead.io/update-sources": json.dumps({"app": "lscr.io/linuxserver/sonarr:latest"})}},
                    "spec": {"replicas": 1, "template": {"spec": {
                        "containers": [{"name": "app", "image": "lscr.io/linuxserver/sonarr@sha256:" + "a" * 64}],
                        "volumes": [{"name": "config", "persistentVolumeClaim": {"claimName": "sonarr-config"}},
                                    {"name": "media", "persistentVolumeClaim": {"claimName": "media-nfs"}}]}}},
                    "status": {"observedGeneration": 4, "replicas": 1, "updatedReplicas": 1, "readyReplicas": 1, "availableReplicas": 1}}
        self.snapshots, self.answer, self.checkpoints = [], {}, 0
        self.updates = FakeUpdates(self)

        def kget(path):
            if path.endswith("/deployments/sonarr"):
                return copy.deepcopy(self.dep)
            if path.endswith("/persistentvolumeclaims/sonarr-config"):
                return {"spec": {"volumeName": "pvc-1"}}
            if path.endswith("/persistentvolumeclaims/media-nfs"):
                return {"spec": {"volumeName": "pv-nfs"}}
            if path.endswith("/persistentvolumes/pvc-1"):
                return {"spec": {"csi": {"driver": "driver.longhorn.io", "volumeHandle": "pvc-1"}}}
            if path.endswith("/persistentvolumes/pv-nfs"):
                return {"spec": {"nfs": {"server": "192.0.2.5"}}}
            raise AssertionError(path)
        AU.bind(kget, None, self.updates, lambda: self.answer,
                lambda volume, name, annotations: self.snapshots.append((volume, name, annotations)),
                lambda volume: [], lambda volume, name: None, NAMES)
        self.item = {"id": "0123456789abcdef0123", "progress": 0,
                     "ref": {"namespace": "lab", "name": "sonarr", "phase": "snapshot"}}

    def step(self, now):
        def checkpoint(item):
            self.checkpoints += 1
        return AU.resolve(self.item, checkpoint, now=now)

    def roll_out(self):
        self.dep["status"]["observedGeneration"] = self.dep["metadata"]["generation"]

    def test_snapshot_update_and_answering_again(self):
        self.assertEqual("running", self.step(1000)[0])
        self.assertEqual([("pvc-1", "before-update-0123456789ab-0", {"homestead.io/before-update": "lab/sonarr"})], self.snapshots,
                         "Longhorn volumes only; the NFS share is noted, not snapshotted")
        self.assertEqual(["media-nfs"], self.item["ref"]["skipped"])
        self.assertEqual(("running", 50), self.step(1010)[:2])
        self.assertEqual(1, self.updates.committed)
        self.dep["status"]["observedGeneration"] = 4           # the new pods are not there yet
        self.assertEqual(55, self.step(1020)[1])
        self.roll_out()
        self.assertEqual(70, self.step(1100)[1])
        self.answer = {"lab/sonarr": {"state": "up", "last": {"at": 1050, "ok": True}}}
        self.assertEqual("running", self.step(1110)[0], "an answer from before the new pods does not count")
        self.answer = {"lab/sonarr": {"state": "up", "last": {"at": 1160, "ok": True}}}
        status, progress, message = self.step(1170)
        self.assertEqual("succeeded", status)
        self.assertIn("answering again", message)
        self.assertIn("sonarr-config (before-update-0123456789ab-0)", message)
        self.assertEqual(0, self.updates.rolled_back)

    def test_it_stops_answering_so_it_is_rolled_back(self):
        for now in (1000, 1010):
            self.step(now)
        self.roll_out()
        self.step(1100)
        self.answer = {"lab/sonarr": {"state": "down", "last": {"at": 1280, "ok": False, "error": "HTTP 502"}}}
        status, _, message = self.step(1290)
        self.assertEqual("failed", status)
        self.assertEqual(1, self.updates.rolled_back)
        self.assertIn("stopped answering (HTTP 502)", message)
        self.assertIn("Restore them from Data Protection", message)

    def test_new_pods_that_never_become_ready_are_rolled_back(self):
        for now in (1000, 1010):
            self.step(now)
        self.dep["status"]["observedGeneration"] = 4
        self.assertEqual("running", self.step(1300)[0])
        status, _, message = self.step(1010 + AU.ROLLOUT_WAIT + 1)
        self.assertEqual(("failed", 1), (status, self.updates.rolled_back))
        self.assertIn("not ready after 10 minutes", message)

    def test_silence_after_starting_is_rolled_back(self):
        for now in (1000, 1010):
            self.step(now)
        self.roll_out()
        self.step(1100)
        self.answer = {"lab/sonarr": {"state": "up", "last": {"at": 1090, "ok": True}}}
        self.assertEqual("failed", self.step(1100 + AU.ANSWER_WAIT + 1)[0])

    def test_an_app_without_a_check_only_has_to_stay_ready(self):
        for now in (1000, 1010):
            self.step(now)
        self.roll_out()
        self.step(1100)
        self.answer = {"lab/sonarr": {"state": "off", "why": "no address to ask"}}
        self.assertEqual("running", self.step(1150)[0])
        self.assertEqual("succeeded", self.step(1100 + AU.STEADY)[0])

    def test_a_version_jump_found_at_the_last_moment_is_not_installed(self):
        self.candidate = "lscr.io/linuxserver/sonarr:5.0"
        self.step(1000)
        status, _, message = self.step(1010)
        self.assertEqual(("failed", 0), (status, self.updates.committed))
        self.assertIn("new version", message)

    def test_a_restart_after_the_update_was_written_carries_on(self):
        self.step(1000)
        self.step(1010)
        self.item["ref"]["phase"] = "updating"           # as if it stopped just after the write
        self.assertEqual(("running", 50), self.step(1020)[:2])
        self.assertEqual(1, self.updates.committed, "not installed twice")
        self.assertEqual("watch", self.item["ref"]["phase"])

    def test_nothing_left_to_install_is_not_a_failure(self):
        self.updates.available = False
        self.step(1000)
        self.assertEqual("succeeded", self.step(1010)[0])

    def test_every_step_is_saved_before_it_writes(self):
        self.step(1000)
        before = self.checkpoints
        self.step(1010)
        self.assertGreaterEqual(self.checkpoints - before, 2, "updating is saved before the write, watch after")


class TidyTests(unittest.TestCase):
    def test_keeps_the_newest_two_before_update_snapshots(self):
        removed = []
        AU.bind(None, None, None, lambda: {}, None,
                lambda volume: [{"name": f"before-update-x-{i}", "created": f"2026-10-0{i}T00:00:00Z"} for i in range(1, 5)]
                + [{"name": "homestead-manual", "created": "2026-09-01T00:00:00Z"}],
                lambda volume, name: removed.append(name), NAMES)
        AU.tidy([{"kind": AU.KIND, "status": "succeeded", "auto_update": {"snapshots": [{"volume": "pvc-1", "snapshot": "before-update-x-4"}]}},
                 {"kind": AU.KIND, "status": "failed", "auto_update": {"snapshots": [{"volume": "pvc-2", "snapshot": "y"}]}}])
        self.assertEqual(["before-update-x-2", "before-update-x-1"], removed, "manual snapshots and failed updates' are left alone")


if __name__ == "__main__":
    unittest.main()
