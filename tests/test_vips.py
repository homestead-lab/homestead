import json
import sys
import unittest
import urllib.error
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
import homestead_networking as NET

MAP = "/api/v1/namespaces/lab/configmaps/homestead-vips"


class Cluster:
    def __init__(self, vips=None, services=None):
        self.map = {"data": {"vips.json": json.dumps(vips)}} if vips is not None else None
        self.services = services or []
        self.sent = []
        NET.bind(self.get, self.send, {"kube-system"}, "lab", "192.0.2.242")

    def get(self, path):
        if path == MAP:
            if self.map is None:
                raise urllib.error.HTTPError(path, 404, "missing", None, None)
            return self.map
        if path == "/api/v1/nodes":
            return {"items": [{"status": {"addresses": [{"type": "InternalIP", "address": "192.0.2.201"}]}}]}
        if path == "/api/v1/services":
            return {"items": self.services}
        return {"items": []}

    def send(self, method, path, body=None, **kw):
        self.sent.append((method, path, body))
        if path.endswith("/configmaps") or path == MAP:
            self.map = body
        return body


def service(ip, name="plex", port=32400):
    return {"metadata": {"namespace": "lab", "name": name, "annotations": {NET.VIP_ANNOTATION: ip}},
            "spec": {"type": "LoadBalancer", "clusterIP": "10.43.0.9", "ports": [{"port": port, "protocol": "TCP"}]},
            "status": {"loadBalancer": {"ingress": [{"ip": ip}]}}}


class VipTests(unittest.TestCase):
    def test_a_range_is_kept_with_its_label_skipping_what_cannot_be_a_vip(self):
        c = Cluster()
        result = NET.add_vips({"start": "192.0.2.199", "end": "192.0.2.203", "label": "Media"},
                              {"192.0.2.203": {"name": "printer", "category": "printer"}})
        self.assertEqual(["192.0.2.199", "192.0.2.200", "192.0.2.202"], result["added"])
        self.assertEqual(2, len(result["skipped"]))
        self.assertTrue(any("node's own address" in s for s in result["skipped"]))
        self.assertTrue(any("printer" in s for s in result["skipped"]))
        stored = json.loads(c.map["data"]["vips.json"])
        self.assertEqual({"Media"}, {row["label"] for row in stored})
        with self.assertRaisesRegex(ValueError, "at most"):
            NET.add_vips({"start": "192.0.2.1", "end": "192.0.2.100"})

    def test_automatic_addresses_come_from_your_vips_first(self):
        Cluster(vips=[{"ip": "192.0.2.230", "label": "Shares"}, {"ip": "192.0.2.214", "label": "Frigate"}],
                services=[service("192.0.2.214")])
        state = NET.inventory()
        self.assertEqual(["192.0.2.230"], state["available_vips"][:1])
        self.assertEqual({"192.0.2.230": True, "192.0.2.214": False},
                         {row["ip"]: row["free"] for row in state["registered_vips"]})
        self.assertEqual(["lab/plex"], next(r for r in state["registered_vips"] if r["ip"] == "192.0.2.214")["used_by"])
        plan = NET.service_plan({"namespace": "lab", "name": "samba", "workload": "samba", "type": "LoadBalancer",
                                 "vip_mode": "automatic", "ports": [{"port": 445, "target_port": 445}]},
                                require_workload=False)
        self.assertEqual("192.0.2.230", plan["vip"])

    def test_no_free_address_says_where_to_add_one(self):
        Cluster()
        with self.assertRaisesRegex(ValueError, "Add VIPs"):
            NET.service_plan({"namespace": "lab", "name": "samba", "workload": "samba", "type": "LoadBalancer",
                              "vip_mode": "automatic", "ports": [{"port": 445, "target_port": 445}]},
                             require_workload=False)

    def test_an_address_in_use_is_not_released(self):
        Cluster(vips=[{"ip": "192.0.2.214", "label": ""}], services=[service("192.0.2.214")])
        with self.assertRaisesRegex(ValueError, "in use by lab/plex"):
            NET.remove_vip("192.0.2.214")


if __name__ == "__main__":
    unittest.main()
