import sys, unittest
from pathlib import Path
from unittest import mock
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
import server


def deployment(ns, name, image):
    return {"metadata": {"namespace": ns, "name": name, "annotations": {}, "labels": {}},
            "spec": {"replicas": 1, "selector": {"matchLabels": {"app": name}},
                     "template": {"metadata": {"labels": {"app": name}},
                                  "spec": {"containers": [{"name": name, "image": image}]}}},
            "status": {"readyReplicas": 1}}


class PlatformWorkloadTests(unittest.TestCase):
    """On k3s, KubeVirt's parts showed as apps of their own, each offering an
    image update its operator would only put back."""

    def test_add_on_parts_are_platform_in_homesteads_group_and_harvesters_stay_out(self):
        items = {"/apis/apps/v1/deployments": {"items": [
                     deployment("kubevirt", "virt-api", "quay.io/kubevirt/virt-api:v1.6.0"),
                     deployment("harvester-system", "virt-api", "registry.suse.com/virt-api:v1.4.0"),
                     deployment("lab", "plex", "lscr.io/linuxserver/plex:latest")]},
                 "/api/v1/pods": {"items": []}}
        with mock.patch.object(server, "kget", side_effect=lambda path, **kw: items.get(path, {"items": []})):
            rows = {(w["ns"], w["name"]): w for w in server.get_workloads()}
        self.assertNotIn(("harvester-system", "virt-api"), rows)
        virt = rows[("kubevirt", "virt-api")]
        self.assertEqual(("KubeVirt", server.OWN_GROUP), (virt["platform"], virt["group"]))
        self.assertEqual("", rows[("lab", "plex")]["platform"])
        # The image checker leaves them to KubeVirt's own upgrade.
        self.assertIn("kubevirt", server.SYS_NS)


if __name__ == "__main__":
    unittest.main()
