import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
import homestead_kube as K
import homestead_networking as NET
import homestead_network_state as STATE
import homestead_network_inventory as INV
import homestead_network_services as SVC
import homestead_operations as OPS
import homestead_vip_registry as REG
from fakes.kube import Kube
from fakes.platform import plain_platform

MAP = "/api/v1/namespaces/lab/configmaps/homestead-vips"
NODE = {"apiVersion": "v1", "kind": "Node", "metadata": {"name": "node1"},
        "status": {"addresses": [{"type": "InternalIP", "address": "192.0.2.201"}]}}


def cluster(test, vips=None, services=()):
    """The VIP registry and the Services on the shared fake API (tests/fakes/kube.py)."""
    objects = [NODE, *services]
    if vips is not None:
        objects.append({"apiVersion": "v1", "kind": "ConfigMap", "metadata": {"namespace": "lab", "name": "homestead-vips"},
                        "data": {"vips.json": json.dumps(vips)}})
    kube = test.enterContext(Kube(*objects))
    # The shared fake runs the real write guards; the storage guard takes the job
    # store's lock, which lives under its DATA_DIR.
    test.enterContext(mock.patch.object(OPS, "DATA_DIR", test.enterContext(tempfile.TemporaryDirectory())))
    saved = (STATE.kget, STATE.ksend, STATE.SYSTEM_NAMESPACES, STATE.DEFAULT_NAMESPACE, STATE.SHARED_VIP,
             NET.guard_managed, NET.node_probes)
    test.addCleanup(lambda: NET.bind(*saved))
    NET.bind(K.kget, K.ksend, {"kube-system"}, "lab", "192.0.2.242")
    return kube


def service(ip, name="plex", port=32400):
    return {"apiVersion": "v1", "kind": "Service",
            "metadata": {"namespace": "lab", "name": name, "annotations": {STATE.VIP_ANNOTATION: ip}},
            "spec": {"type": "LoadBalancer", "clusterIP": "10.43.0.9", "ports": [{"port": port, "protocol": "TCP"}]},
            "status": {"loadBalancer": {"ingress": [{"ip": ip}]}}}


class VipTests(unittest.TestCase):
    def setUp(self):
        plain_platform(self)

    def test_a_range_is_kept_with_its_label_skipping_what_cannot_be_a_vip(self):
        kube = cluster(self)
        result = REG.add_vips({"start": "192.0.2.199", "end": "192.0.2.203", "label": "Media"},
                              {"192.0.2.203": {"name": "printer", "category": "printer"}})
        self.assertEqual(["192.0.2.199", "192.0.2.200", "192.0.2.202"], result["added"])
        self.assertEqual(2, len(result["skipped"]))
        self.assertTrue(any("node's own address" in s for s in result["skipped"]))
        self.assertTrue(any("printer" in s for s in result["skipped"]))
        stored = json.loads(kube.object(MAP)["data"]["vips.json"])
        self.assertEqual({"Media"}, {row["label"] for row in stored})
        with self.assertRaisesRegex(ValueError, "at most"):
            REG.add_vips({"start": "192.0.2.1", "end": "192.0.2.100"})

    def test_automatic_addresses_come_from_your_vips_first(self):
        cluster(self, vips=[{"ip": "192.0.2.230", "label": "Shares"}, {"ip": "192.0.2.214", "label": "Frigate"}],
                services=[service("192.0.2.214")])
        state = INV.inventory()
        self.assertEqual(["192.0.2.230"], state["available_vips"][:1])
        self.assertEqual({"192.0.2.230": True, "192.0.2.214": False},
                         {row["ip"]: row["free"] for row in state["registered_vips"]})
        self.assertEqual(["lab/plex"], next(r for r in state["registered_vips"] if r["ip"] == "192.0.2.214")["used_by"])
        plan = SVC.service_plan({"namespace": "lab", "name": "samba", "workload": "samba", "type": "LoadBalancer",
                                 "vip_mode": "automatic", "ports": [{"port": 445, "target_port": 445}]},
                                require_workload=False)
        self.assertEqual("192.0.2.230", plan["vip"])

    def test_no_free_address_says_where_to_add_one(self):
        cluster(self)
        with self.assertRaisesRegex(ValueError, "Add VIPs"):
            SVC.service_plan({"namespace": "lab", "name": "samba", "workload": "samba", "type": "LoadBalancer",
                              "vip_mode": "automatic", "ports": [{"port": 445, "target_port": 445}]},
                             require_workload=False)

    def test_an_address_in_use_is_not_released(self):
        cluster(self, vips=[{"ip": "192.0.2.214", "label": ""}], services=[service("192.0.2.214")])
        with self.assertRaisesRegex(ValueError, "in use by lab/plex"):
            REG.remove_vip("192.0.2.214")

    def test_local_nfs_cannot_share_an_independently_routed_smb_address(self):
        cluster(self, services=[service("192.0.2.245", "homestead-smb", 445)])
        with self.assertRaisesRegex(ValueError, "independently placed pods"):
            SVC.service_plan({"namespace": "lab", "name": "homestead-nfs", "vip_mode": "manual",
                              "vip": "192.0.2.245", "exclusive_vip": True,
                              "ports": [{"port": 2049, "target_port": 2049}]}, require_workload=False)

    def test_other_services_cannot_take_a_port_on_the_nfs_vip(self):
        nfs = service("192.0.2.245", "homestead-nfs", 2049)
        nfs["spec"]["externalTrafficPolicy"] = "Local"
        cluster(self, services=[nfs])
        with self.assertRaisesRegex(ValueError, "independently placed pods"):
            SVC.service_plan({"namespace": "lab", "name": "homestead-smb", "vip_mode": "manual",
                              "vip": "192.0.2.245", "ports": [{"port": 445, "target_port": 445}]},
                             require_workload=False)


if __name__ == "__main__":
    unittest.main()
