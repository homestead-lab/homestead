import sys
import unittest
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))

import homestead_hardware as hardware
import homestead_place as place


CORAL = {
    "id": "coral_usb", "name": "Google Coral USB",
    "label": "hardware/coral-usb", "host_path": "/dev/bus/usb",
    "container_path": "/dev/bus/usb", "path_type": "Directory",
    "usb_ids": ["1a6e:089a"], "builtin": False,
}


class HardwarePlacementTests(unittest.TestCase):
    def setUp(self):
        place.hardware_features = lambda: [CORAL]
        self.nodes = [
            {"name": "node1", "status": "Ready", "schedulable": True,
             "hardware": {"coral_usb": True}, "labels": {}, "allocatable": {},
             "temps": {"devices": {"usb": [{"vid": "1a6e", "pid": "089a"}]}}},
            {"name": "node2", "status": "Ready", "schedulable": True,
             "hardware": {"coral_usb": False}, "labels": {}, "allocatable": {},
             "temps": {"devices": {"usb": []}}},
        ]
        place.get_nodes = lambda: self.nodes
        self.dep = {
            "spec": {"template": {"spec": {
                "containers": [{"name": "frigate"}],
                "nodeSelector": {"hardware/coral-usb": "true"},
                "volumes": [{"name": "coral", "hostPath": {"path": "/dev/bus/usb"}}],
            }}}
        }

        def kget(path):
            if path == "/api/v1/pods":
                return {"items": [{"metadata": {"namespace": "lab", "labels": {"app": "frigate"}},
                                    "spec": {"nodeName": "node1"}}]}
            if path == "/apis/apps/v1/namespaces/lab/deployments/frigate":
                return self.dep
            raise AssertionError(path)
        place.kget = kget

    def test_usb_inventory_matches_vid_pid(self):
        hardware.features = lambda: [CORAL]
        result = hardware.inventory({}, {"usb": [{"vid": "1a6e", "pid": "089a"}]})
        self.assertTrue(result[0]["detected"])
        self.assertTrue(result[0]["available"])

    def test_node_impact_marks_workload_stranded(self):
        result = place.impact("node1")
        self.assertFalse(result["safe"])
        self.assertEqual(["frigate"], [x["name"] for x in result["stranded"]])

    def test_node_impact_lists_compatible_destination(self):
        self.nodes[1]["hardware"]["coral_usb"] = True
        result = place.impact("node1")
        self.assertTrue(result["safe"])
        self.assertEqual(["node2"], result["movable"][0]["eligible"])


if __name__ == "__main__":
    unittest.main()
