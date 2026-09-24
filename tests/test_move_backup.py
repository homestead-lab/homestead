import json
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
import homestead_longhorn as LH
import homestead_move_source as SOURCE


class SnapshotNameTests(unittest.TestCase):
    def test_two_volumes_snapshotted_in_the_same_second_get_different_names(self):
        sent = []
        with mock.patch.object(LH, "ksend", lambda m, p, b=None, **k: sent.append(b) or b), \
                mock.patch.object(LH.time, "time", lambda: 1790287418):
            first = LH.create_snapshot("pvc-a")["snapshot"]
            second = LH.create_snapshot("pvc-b")["snapshot"]
        self.assertNotEqual(first, second)
        self.assertTrue(first.startswith("homestead-1790287418-"))


class BackupProgressTests(unittest.TestCase):
    def test_a_backup_failing_part_way_keeps_what_it_made(self):
        merged = []
        made = iter([{"backup": "b1"}, RuntimeError("longhorn refused")])

        def create_backup(volume):
            result = next(made)
            if isinstance(result, Exception):
                raise result
            return result
        with mock.patch.object(SOURCE, "_kind", lambda k: k), \
                mock.patch.object(SOURCE, "_object", lambda kind, name: {"metadata": {"annotations": {}}}), \
                mock.patch.object(SOURCE, "_origin", lambda obj: {"replicas": 1}), \
                mock.patch.object(SOURCE, "_remaining", lambda kind, obj: 0), \
                mock.patch.object(SOURCE, "_recorded_backups", lambda obj: []), \
                mock.patch.object(SOURCE, "_claims_of", lambda kind, obj: ["data", "data2"]), \
                mock.patch.object(SOURCE, "_claim_row", lambda c: {"volume": f"pvc-{c}", "backing_image": ""}), \
                mock.patch.object(SOURCE, "_merge", lambda kind, name, patch: merged.append(patch)), \
                mock.patch.object(SOURCE.LH, "create_backup", create_backup):
            with self.assertRaises(RuntimeError):
                SOURCE.backup("container", "birdnet-go")
        recorded = json.loads(next(iter(merged[-1]["metadata"]["annotations"].values())))
        self.assertEqual([("data", "b1")], [(r["claim"], r["backup"]) for r in recorded])


if __name__ == "__main__":
    unittest.main()
