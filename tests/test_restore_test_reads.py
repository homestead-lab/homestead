"""Restore tests on Data protection: what the page's list reads from the cluster.

The page asks for /api/restore-tests on every refresh. Its answer is kept a
little longer than the refresh, worked out once for callers that ask at the
same time, read the Longhorn backups list once (not once per claim) and is
forgotten when a test starts or finishes or a backup is made or deleted."""
import sys
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "server"))

import homestead_cache as CACHE
import homestead_kube as K
import homestead_longhorn as LH
import homestead_longhorn_backups as LHB
import homestead_names as NAMES
import homestead_restore_test as RT
from fakes.kube import Kube

BACKUPS = "/apis/longhorn.io/v1beta2/namespaces/longhorn-system/backups"
APPS = ("paperless", "immich", "jellyfin")


def objects():
    out = []
    for app in APPS:
        claims = [f"{app}-data", f"{app}-config"]
        out.append({"apiVersion": "apps/v1", "kind": "Deployment", "metadata": {"name": app, "namespace": "lab"},
                    "spec": {"template": {"spec": {"containers": [{"name": "app", "image": "example/app:1"}],
                             "volumes": [{"name": c, "persistentVolumeClaim": {"claimName": c}} for c in claims]}}}})
        for claim in claims:
            volume = f"pvc-{claim}"
            out.append({"apiVersion": "v1", "kind": "PersistentVolumeClaim", "metadata": {"name": claim, "namespace": "lab"},
                        "spec": {"volumeName": volume, "storageClassName": "longhorn"}})
            out.append({"apiVersion": "v1", "kind": "PersistentVolume", "metadata": {"name": volume},
                        "spec": {"csi": {"driver": "driver.longhorn.io", "volumeHandle": volume}}})
            for day in (1, 6):
                out.append(backup(f"{claim}-{day}", volume, f"2026-10-0{day}T03:00:00Z"))
    return out


def backup(name, volume, created, state="Completed"):
    return {"apiVersion": "longhorn.io/v1beta2", "kind": "Backup",
            "metadata": {"name": name, "namespace": "longhorn-system", "labels": {"backup-volume": volume}},
            "status": {"state": state, "url": f"s3://example/{name}", "backupCreatedAt": created,
                       "volumeSize": str(20 * 1073741824), "size": "1048576"}}


class Reads(unittest.TestCase):
    def setUp(self):
        CACHE.forget_where(lambda key: True)
        self.kube = Kube(*objects())
        self.kube.__enter__()
        self.addCleanup(self.kube.__exit__, None, None, None)
        self.reads = []

        def kget(path, timeout=10):
            self.reads.append(path)
            return K.kget(path, timeout)
        LH.bind(kget, K.ksend, CACHE._cache)
        self.operations = []
        RT.bind(kget, K.ksend, NAMES, LHB.backups, lambda cfg: {}, lambda item: ("running", 0, ""),
                lambda: [], lambda: {}, lambda target: {"ok": False}, lambda: "lab",
                lambda: [{"ns": "lab", "name": app, "claims": [f"{app}-data", f"{app}-config"]} for app in APPS],
                lambda: {}, lambda settings: None, lambda value, what: value)
        patch = mock.patch.object(RT.OPS, "snapshot", lambda: list(self.operations))
        patch.start()
        self.addCleanup(patch.stop)
        self.addCleanup(CACHE.forget_where, lambda key: True)

    def backup_lists(self):
        return [p for p in self.reads if p.split("?")[0] == BACKUPS]

    def test_every_app_and_claim_reads_the_backups_list_once(self):
        answer = RT.view()
        self.assertEqual(len(APPS), len(answer["apps"]))
        self.assertEqual(1, len(self.backup_lists()), self.backup_lists())

    def test_the_answer_is_the_same_as_asking_claim_by_claim(self):
        want = []
        for app in APPS:
            dep = K.kget(f"/apis/apps/v1/namespaces/lab/deployments/{app}")
            want.append(RT.plan("lab", dep))
        got = [(a["restores"], a["without"]) for a in RT.view()["apps"]]
        self.assertEqual(sorted(want, key=repr), sorted([tuple(g) for g in got], key=repr))
        self.assertEqual("immich-config-6", RT.view()["apps"][0]["restores"][0]["backup"])

    def test_two_requests_within_the_refresh_read_once(self):
        RT.view()
        first = len(self.reads)
        RT.view()
        self.assertEqual(first, len(self.reads))

    def test_it_is_kept_at_least_as_long_as_the_page_refresh(self):
        self.assertGreaterEqual(RT.PLANS_SECONDS, 15)
        RT.view()
        first = len(self.reads)
        later = time.time() + RT.PLANS_SECONDS + 1
        with mock.patch.object(CACHE.time, "time", lambda: later):
            RT.view()
        self.assertGreater(len(self.reads), first)

    def test_a_started_test_is_seen_at_once(self):
        RT.view()
        self.operations.append({"kind": RT.KIND, "status": "running",
                                "resource": {"namespace": "lab", "name": "immich"}})
        rows = {a["name"]: a for a in RT.view()["apps"]}
        self.assertTrue(rows["immich"]["running"])
        self.assertFalse(rows["paperless"]["running"])

    def test_starting_a_test_forgets_the_answer(self):
        RT.view()
        first = len(self.reads)
        with mock.patch.object(RT.OPS, "start", lambda *a, **k: {"id": "x"}):
            RT.start_test("lab", "immich")
        RT.view()
        self.assertGreater(len(self.reads), first)

    def test_finishing_a_test_forgets_the_answer(self):
        RT.view()
        first = len(self.reads)
        item = {"id": "abcdef0123456789", "ref": {"namespace": "lab", "name": "immich", "phase": "cleanup",
                                                  "ok": True, "why": "it answered", "rows": []}}
        RT._finish(item, lambda *a, **k: None, True, "it answered")
        RT.view()
        self.assertGreater(len(self.reads), first)

    def test_a_failed_backups_read_is_not_kept(self):
        self.kube.fail(BACKUPS, 500)
        with self.assertRaises(Exception):
            RT.view()
        first = len(self.backup_lists())
        rows = {a["name"]: a for a in RT.view()["apps"]}
        self.assertEqual(first + 1, len(self.backup_lists()))
        self.assertTrue(rows["immich"]["restores"])

    def test_a_failed_backups_read_shows_the_last_good_answer(self):
        good = RT.view()["apps"]
        self.kube.fail(BACKUPS, 500)
        later = time.time() + RT.PLANS_SECONDS + 1
        with mock.patch.object(CACHE.time, "time", lambda: later):
            self.assertEqual(good, RT.view()["apps"])
        self.assertEqual(2, len(self.backup_lists()))

    def test_a_new_backup_is_seen_at_once(self):
        RT.view()
        self.kube.add(backup("immich-data-9", "pvc-immich-data", "2026-10-09T03:00:00Z"))
        LH._bust("lhbackups")
        rows = {a["name"]: a for a in RT.view()["apps"]}
        self.assertIn("immich-data-9", [r["backup"] for r in rows["immich"]["restores"]])

    def test_a_new_app_is_seen_at_once(self):
        RT.view()
        RT.workloads = lambda: [{"ns": "lab", "name": app, "claims": [f"{app}-data"]} for app in APPS + ("gitea",)]
        self.kube.add({"apiVersion": "apps/v1", "kind": "Deployment", "metadata": {"name": "gitea", "namespace": "lab"},
                       "spec": {"template": {"spec": {"containers": [{"name": "app", "image": "example/app:1"}],
                                "volumes": [{"name": "d", "persistentVolumeClaim": {"claimName": "paperless-data"}}]}}}})
        self.assertIn("gitea", [a["name"] for a in RT.view()["apps"]])


class OneVolume(unittest.TestCase):
    def setUp(self):
        self.kube = Kube(*[o for o in objects() if o["kind"] == "Backup"])
        self.kube.__enter__()
        self.addCleanup(self.kube.__exit__, None, None, None)
        self.reads = []

        def kget(path, timeout=10):
            self.reads.append(path)
            return K.kget(path, timeout)
        LH.bind(kget, K.ksend, {})

    def test_one_volumes_backups_are_asked_for_by_label(self):
        rows = LHB.backups("pvc-immich-data")
        self.assertEqual(["immich-data-6", "immich-data-1"], [r["name"] for r in rows])
        self.assertEqual([BACKUPS + "?labelSelector=backup-volume%3Dpvc-immich-data"], self.reads)

    def test_every_backup_is_one_plain_list(self):
        self.assertEqual(len(APPS) * 4, len(LHB.backups()))
        self.assertEqual([BACKUPS], self.reads)


if __name__ == "__main__":
    unittest.main()
