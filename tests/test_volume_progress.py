import sys, unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
import server
import homestead_storage as STORAGE


def engine(volume, rebuild=None, restore=None):
    return {"metadata": {"name": f"{volume}-e-0", "labels": {"longhornvolume": volume}},
            "spec": {"volumeName": volume},
            "status": {"rebuildStatus": rebuild or {}, "restoreStatus": restore or {}}}


def lh_volume(name, **status):
    return {"metadata": {"name": name},
            "spec": {"size": str(10 * 1024**3), "numberOfReplicas": 3},
            "status": {"state": "attached", "robustness": "degraded",
                       "kubernetesStatus": {"namespace": "lab", "pvcName": name}, **status}}


class EngineProgressTests(unittest.TestCase):
    """How far a rebuild or restore has got lives on Longhorn's engine, not
    the volume, so Volumes showed "degraded" with no idea of when it ends."""

    def test_a_rebuild_reports_its_slowest_replica(self):
        progress = STORAGE._engine_progress([engine("pvc-a", rebuild={
            "tcp://10.0.0.1:10000": {"isRebuilding": True, "progress": 72},
            "tcp://10.0.0.2:10000": {"isRebuilding": True, "progress": 35},
            "tcp://10.0.0.3:10000": {"isRebuilding": False, "progress": 0}})])

        self.assertEqual({"pct": 35, "replicas": 2, "error": ""}, progress["pvc-a"]["rebuild"])

    def test_a_restore_reports_its_progress(self):
        progress = STORAGE._engine_progress([engine("pvc-b", restore={
            "tcp://10.0.0.1:10000": {"isRestoring": True, "progress": 41},
            "tcp://10.0.0.2:10000": {"isRestoring": True, "progress": 44}})])

        self.assertEqual({"pct": 41, "error": ""}, progress["pvc-b"]["restore"])

    def test_a_failed_restore_says_why(self):
        progress = STORAGE._engine_progress([engine("pvc-c", restore={
            "tcp://10.0.0.1:10000": {"isRestoring": False, "progress": 12,
                                     "error": "backup target unreachable"}})])

        self.assertEqual("backup target unreachable", progress["pvc-c"]["restore"]["error"])

    def test_finished_work_leaves_nothing_behind(self):
        progress = STORAGE._engine_progress([engine("pvc-d",
            rebuild={"tcp://10.0.0.1:10000": {"isRebuilding": False, "progress": 100}},
            restore={"tcp://10.0.0.1:10000": {"isRestoring": False, "progress": 100,
                                              "lastRestored": "backup-1"}})])

        self.assertEqual({}, progress)


class VolumesListTests(unittest.TestCase):
    def setUp(self):
        # STORAGE reads the cluster through server.py's client in these tests.
        for name in ("kget", "ksend"):
            self.addCleanup(setattr, STORAGE, name, getattr(STORAGE, name))
            setattr(STORAGE, name, lambda *a, _name=name, **k: getattr(server.KUBE_HELPERS, _name)(*a, **k))
        self._kget = server.KUBE_HELPERS.kget

    def tearDown(self):
        server.KUBE_HELPERS.kget = self._kget

    def serve(self, volumes, engines):
        def kget(path):
            if path.endswith("/volumes"):
                return {"items": volumes}
            if path.endswith("/engines"):
                return {"items": engines}
            return {"items": []}
        server.KUBE_HELPERS.kget = kget
        return {row["name"]: row for row in STORAGE.get_volumes()}

    def test_the_list_carries_rebuild_and_restore_progress(self):
        rows = self.serve(
            [lh_volume("pvc-a"), lh_volume("pvc-b", restoreRequired=True), lh_volume("pvc-c")],
            [engine("pvc-a", rebuild={"r1": {"isRebuilding": True, "progress": 63}}),
             engine("pvc-b", restore={"r1": {"isRestoring": True, "progress": 41}})])

        self.assertEqual(63, rows["pvc-a"]["rebuild"]["pct"])
        self.assertEqual(41, rows["pvc-b"]["restore"]["pct"])
        self.assertIsNone(rows["pvc-c"]["rebuild"])
        self.assertIsNone(rows["pvc-c"]["restore"])

    def test_a_restore_not_yet_on_the_engine_still_shows(self):
        rows = self.serve([lh_volume("pvc-b", restoreRequired=True)], [])

        self.assertEqual({"pct": 0, "error": ""}, rows["pvc-b"]["restore"])

    def test_a_standby_volume_is_not_a_restore_in_progress(self):
        """A disaster-recovery standby keeps restoreRequired for good."""
        rows = self.serve([lh_volume("pvc-dr", restoreRequired=True, isStandby=True)], [])

        self.assertIsNone(rows["pvc-dr"]["restore"])

    def test_a_degraded_volume_says_since_when(self):
        """Longhorn's lastDegradedAt is the repairing grace's clock (#941):
        it survives a Homestead restart, which a first sighting would not."""
        rows = self.serve([lh_volume("pvc-a", lastDegradedAt="2026-10-28T03:00:00Z"),
                           lh_volume("pvc-b", robustness="healthy", lastDegradedAt="")], [])

        self.assertEqual(1793156400, rows["pvc-a"]["degraded_since"])
        self.assertIsNone(rows["pvc-b"]["degraded_since"])

    def test_volumes_are_listed_when_engines_cannot_be_read(self):
        def kget(path):
            if path.endswith("/engines"):
                raise PermissionError("forbidden")
            return {"items": [lh_volume("pvc-a")] if path.endswith("/volumes") else []}
        server.KUBE_HELPERS.kget = kget

        rows = STORAGE.get_volumes()

        self.assertEqual(["pvc-a"], [row["name"] for row in rows])
        self.assertIsNone(rows[0]["rebuild"])


if __name__ == "__main__":
    unittest.main()
