import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "server"))

import server


class WorkloadGroupTests(unittest.TestCase):
    def test_a_group_is_an_annotation_on_each_workload(self):
        sent = []
        with mock.patch.object(server, "ksend", side_effect=lambda *a, **k: sent.append((a, k))):
            result = server.set_workload_groups({"group": "  Media   apps ", "items": [
                {"ns": "lab", "name": "plex"}, {"ns": "lab", "name": "sonarr"}]})
        self.assertEqual("Media apps", result["group"])
        self.assertEqual(2, len(sent))
        (method, path, body), kwargs = sent[0]
        self.assertEqual(("PATCH", "/apis/apps/v1/namespaces/lab/deployments/plex"), (method, path))
        self.assertEqual({"homestead.io/group": "Media apps"}, body["metadata"]["annotations"])
        self.assertEqual("application/merge-patch+json", kwargs["ctype"])

    def test_a_blank_group_removes_the_annotation(self):
        sent = []
        with mock.patch.object(server, "ksend", side_effect=lambda *a, **k: sent.append(a)):
            result = server.set_workload_groups({"group": "", "items": [{"ns": "lab", "name": "plex"}]})
        self.assertIsNone(sent[0][2]["metadata"]["annotations"]["homestead.io/group"])
        self.assertIn("ungrouped", result["detail"])

    def test_names_and_targets_are_checked_before_anything_is_written(self):
        with mock.patch.object(server, "ksend") as send:
            with self.assertRaises(ValueError):
                server.set_workload_groups({"group": "x" * 41, "items": [{"ns": "lab", "name": "plex"}]})
            with self.assertRaises(ValueError):
                server.set_workload_groups({"group": "Media", "items": []})
            with self.assertRaises(ValueError):
                server.set_workload_groups({"group": "Media", "items": [{"ns": "lab", "name": "../x"}]})
        send.assert_not_called()


if __name__ == "__main__":
    unittest.main()
