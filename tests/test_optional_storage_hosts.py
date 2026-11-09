"""Optional and list reads in the storage, host and workload modules (#619
steps 3 and 4): each module reads through the reader its bind() gave it, a
missing object (404) is None, any other failure raises, and a list that came
back incomplete is an error in the module's own words."""
import io
import os
import sys
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.environ.get("HOMESTEAD_TEST_SERVER_DIR", str(Path(__file__).resolve().parents[1] / "server")))
import homestead_kube as KUBE
import homestead_lhv2_setup as LHV2
import homestead_longhorn as LH
import homestead_longhorn_restore as RESTORE
import homestead_move_state as STATE
import homestead_move_phases as PHASE
import homestead_platform as PLATFORM
import homestead_rename as RENAME
import homestead_restore_test as RESTORE_TEST
import homestead_snapshot_files as SNAPSHOT_FILES
import homestead_vm_capacity as VM_CAPACITY

CUT_SHORT = {"items": [], "metadata": {"continue": "more"}}


def http_error(code):
    return urllib.error.HTTPError("/x", code, "error", {}, io.BytesIO(b""))


class Paths:
    """A bound reader: answers by path, the default for any other path."""
    def __init__(self, default, **by_part):
        self.default, self.by_part, self.paths = default, by_part, []

    def __call__(self, path, **_):
        self.paths.append(path)
        answer = next((value for part, value in self.by_part.items() if part in path), self.default)
        if isinstance(answer, Exception):
            raise answer
        return answer


def never(path, **_):
    raise AssertionError(f"read through the shared client, not the module's own: {path}")


class StorageHostReadTests(unittest.TestCase):
    def setUp(self):
        # Every read must go through the module's own reader.
        patch = mock.patch.object(KUBE, "kget", never)
        patch.start()
        self.addCleanup(patch.stop)

    def reading(self, module, reader):
        patch = mock.patch.object(module, "kget", reader)
        patch.start()
        self.addCleanup(patch.stop)
        return reader

    def test_longhorn_backup_that_is_missing_is_not_found(self):
        read = self.reading(LH, Paths(http_error(404)))
        with self.assertRaisesRegex(ValueError, "backup was not found"):
            RESTORE._backup("nightly-1")
        self.assertEqual(len(read.paths), 1)
        self.reading(LH, Paths(http_error(500)))
        with self.assertRaises(urllib.error.HTTPError):
            RESTORE._backup("nightly-1")

    def test_move_engine_makes_a_namespace_only_when_it_is_missing(self):
        sent = []
        patch = mock.patch.object(STATE, "ksend", lambda *args, **_: sent.append(args))
        patch.start()
        self.addCleanup(patch.stop)
        self.reading(STATE, Paths(http_error(404)))
        PHASE._ensure_namespace("lab")
        self.assertEqual([("POST", "/api/v1/namespaces")], [call[:2] for call in sent])
        self.reading(STATE, Paths({"metadata": {"name": "lab"}}))
        PHASE._ensure_namespace("lab")
        self.assertEqual(1, len(sent))
        self.reading(STATE, Paths(http_error(500)))
        with self.assertRaises(urllib.error.HTTPError):
            PHASE._ensure_namespace("lab")

    def lhv2_plan(self, reader):
        self.reading(LHV2, reader)
        for name, value in (("status", lambda: {"nodes": [], "distribution": ""}), ("memory_mib", lambda: 1024)):
            patch = mock.patch.object(LHV2, name, value)
            patch.start()
            self.addCleanup(patch.stop)
        return LHV2.plan()

    def test_lhv2_setup_reads_harvesters_setting_as_optional_and_its_lists_strictly(self):
        reader = Paths(CUT_SHORT, **{"/settings/v2-data-engine": {"value": "true"}, LHV2.CAP.HARVESTER_V2: http_error(404)})
        with self.assertRaisesRegex(ValueError, r"^The V2 setup information is incomplete. Check again.$"):
            self.lhv2_plan(reader)
        self.assertIn(LHV2.CAP.HARVESTER_V2, reader.paths)
        reader = Paths(CUT_SHORT, **{"/settings/v2-data-engine": {"value": "true"}, LHV2.CAP.HARVESTER_V2: http_error(500)})
        with self.assertRaises(urllib.error.HTTPError):
            self.lhv2_plan(reader)

    def deployment(self):
        return {"metadata": {"name": "web", "namespace": "lab", "uid": "u1", "resourceVersion": "7"}, "spec": {}}

    def test_rename_falls_back_to_v1_autoscalers_only_when_v2_is_missing(self):
        read = Paths({"items": []}, **{"autoscaling/v2": http_error(404)})
        RENAME.guards(self.deployment(), "web-2", read)
        self.assertTrue(any("autoscaling/v1" in path for path in read.paths))
        with self.assertRaises(urllib.error.HTTPError):
            RENAME.guards(self.deployment(), "web-2", Paths({"items": []}, **{"autoscaling/v2": http_error(500)}))

    def test_restore_test_plan_counts_a_missing_claim_as_not_longhorn(self):
        dep = {"spec": {"template": {"spec": {"volumes": [{"persistentVolumeClaim": {"claimName": "data"}}]}}}}
        self.reading(RESTORE_TEST, Paths(http_error(404)))
        self.assertEqual(([], [{"claim": "data", "why": "not a Longhorn volume"}]), RESTORE_TEST.plan("lab", dep))
        self.reading(RESTORE_TEST, Paths(http_error(500)))
        with self.assertRaises(urllib.error.HTTPError):
            RESTORE_TEST.plan("lab", dep)

    def test_snapshot_review_refuses_a_cut_short_volume_list(self):
        patch = mock.patch.object(SNAPSHOT_FILES.CSI, "support", lambda: {"ready": True})
        patch.start()
        self.addCleanup(patch.stop)
        snapshot = {"metadata": {}, "spec": {"volume": "vol-1"}, "status": {"readyToUse": True}}
        self.reading(SNAPSHOT_FILES, Paths(CUT_SHORT, **{"/volumes/vol-1": {}, "/snapshots/snap-1": snapshot}))
        with self.assertRaisesRegex(ValueError, r"^Snapshot browser inventory is incomplete$"):
            SNAPSHOT_FILES.review("vol-1", "snap-1")

    def test_vm_admission_lists_through_the_reader_it_is_given(self):
        read = Paths({"items": [{"metadata": {"name": "kubevirt"}}]})
        self.assertEqual([{"metadata": {"name": "kubevirt"}}], VM_CAPACITY._items(read, "/apis/kubevirt.io/v1/kubevirts"))
        self.assertEqual(["/apis/kubevirt.io/v1/kubevirts"], read.paths)
        for answer in (CUT_SHORT, {"items": "not a list"}, "not an object"):
            with self.subTest(answer=answer), self.assertRaisesRegex(ValueError, r"^incomplete VM admission inventory$"):
                VM_CAPACITY._items(Paths(answer), "/api/v1/pods")
        with self.assertRaises(urllib.error.HTTPError):
            VM_CAPACITY._items(Paths(http_error(500)), "/api/v1/pods")

    def test_platform_reads_daemonsets_strictly(self):
        self.reading(PLATFORM, Paths({"items": [{"metadata": {"name": "svclb-web"}}]}))
        self.assertTrue(PLATFORM._servicelb_running())
        self.reading(PLATFORM, Paths({"items": "not a list"}))
        with self.assertRaisesRegex(ValueError, r"^Kubernetes returned an incomplete list for /apis/apps/v1/namespaces/kube-system/daemonsets$"):
            PLATFORM._servicelb_running()


if __name__ == "__main__":
    unittest.main()
