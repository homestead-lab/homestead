import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
import server

CORAL = {"id": "coral_usb", "name": "Google Coral USB", "label": "hardware/coral-usb", "host_path": "/dev/bus/usb",
         "container_path": "/dev/bus/usb", "path_type": "Directory", "usb_ids": ["18d1:9302", "1a6e:089a"]}


class HardwareRescanTests(unittest.TestCase):
    def test_a_coral_plugged_in_later_is_labelled_without_anyone_looking(self):
        nodes = {"items": [{"metadata": {"name": "node1", "labels": {}, "annotations": {}}},
                           {"metadata": {"name": "node2", "labels": {}, "annotations": {}}}]}
        probe = {"node1": {"devices": {"usb": [{"vid": "1a6e", "pid": "089a"}], "dri": [], "paths": []}}}
        sent = []
        with mock.patch.object(server, "kget", lambda path, **k: nodes), \
                mock.patch.object(server, "node_temps", lambda: probe), \
                mock.patch.object(server.HW, "features", lambda: [CORAL]), \
                mock.patch.object(server.HW, "ksend", lambda *a, **k: sent.append(a)):
            found = server.reconcile_hardware(fresh=True)
        # node2 has no probe, so nothing is said about it either way.
        self.assertEqual({"node1": ["coral_usb"]}, found)
        method, path, body = sent[0]
        self.assertEqual(("PATCH", "/api/v1/nodes/node1"), (method, path))
        self.assertEqual({"hardware/coral-usb": "true"}, body["metadata"]["labels"])


if __name__ == "__main__":
    unittest.main()
