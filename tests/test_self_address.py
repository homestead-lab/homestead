import copy
import json
import sys
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
import homestead_baseline as BASELINE
import homestead_networking as NETWORK
import homestead_self_address as SELF_ADDRESS


def service(name, workload, ports, ips, requested=()):
    return {"namespace": "lab", "name": name, "type": "LoadBalancer", "targets": [workload], "ports": ports,
            "external_ips": list(ips), "requested_ips": list(requested), "system": False,
            "uid": f"uid-{name}", "resource_version": "1"}


def state():
    return {"node_ips": ["192.0.2.21", "192.0.2.22"],
            "workloads": [{"namespace": "lab", "name": "homestead"}, {"namespace": "lab", "name": "homestead-objectstore"}],
            "services": [service("homestead", "homestead", [{"name": "http", "port": 8088, "target_port": 8080, "protocol": "TCP"}],
                                 ["192.0.2.21", "192.0.2.22"]),
                         service("homestead-objectstore", "homestead-objectstore",
                                 [{"name": "s3", "port": 9000, "target_port": "s3", "protocol": "TCP"},
                                  {"name": "console", "port": 9001, "target_port": "console", "protocol": "TCP"}],
                                 ["192.0.2.21"])],
            "shared_vip": {"ip": ""}}


class Network:
    def __init__(self, refuse=None):
        self.state, self.created, self.refuse = state(), [], refuse or set()

    def inventory(self):
        return copy.deepcopy(self.state)

    def service_plan(self, cfg):
        if cfg["workload"] in self.refuse:
            raise ValueError("192.0.2.200:9000/TCP is already used by lab/minio")
        return {"vip": cfg["vip"], "warnings": []}

    def create_service(self, cfg):
        self.created.append(cfg)
        return {"name": cfg["name"]}


class Objects:
    NS, NAME = "lab", "homestead-objectstore"

    def __init__(self):
        self.pointed = 0

    def request_target(self):
        self.pointed += 1
        return {"detail": "Longhorn's backup target switch is queued"}


class SelfAddressTests(unittest.TestCase):
    def bind(self, network, objects=None):
        self.objects = objects or Objects()
        SELF_ADDRESS.bind(None, network, self.objects, "lab", "8080", "lab", "homestead-smb")

    def test_homestead_on_node_addresses_is_reported_as_such(self):
        self.bind(Network())
        report = SELF_ADDRESS.report()
        web = next(c for c in report["components"] if c["id"] == "web")
        self.assertEqual(("", ["192.0.2.21", "192.0.2.22"]), (web["vip"], web["node_addresses"]))
        self.assertFalse(report["on_vip"])
        self.assertFalse(next(c for c in report["components"] if c["id"] == "smb")["present"], "no shares, nothing to move")

    def test_each_service_gets_a_second_connection_on_the_vip_with_its_ports(self):
        network = Network()
        self.bind(network)
        moved = SELF_ADDRESS.move("192.0.2.200")
        names = {c["name"]: c for c in network.created}
        self.assertEqual({"homestead-vip", "homestead-objectstore-vip"}, set(names))
        web = names["homestead-vip"]
        self.assertEqual(("192.0.2.200", "manual", [8088], [8080]),
                         (web["vip"], web["vip_mode"], [p["port"] for p in web["ports"]], [p["target_port"] for p in web["ports"]]))
        self.assertEqual([9000, 9001], [p["port"] for p in names["homestead-objectstore-vip"]["ports"]])
        self.assertEqual(1, self.objects.pointed, "backups follow the store onto the VIP")
        self.assertTrue(all(s["action"] == "added" for s in moved["steps"]))

    def test_a_service_that_cannot_go_there_is_reported_and_the_rest_still_move(self):
        network = Network(refuse={"homestead-objectstore"})
        self.bind(network)
        moved = SELF_ADDRESS.move("192.0.2.200")
        actions = {s["id"]: s["action"] for s in moved["steps"]}
        self.assertEqual({"web": "added", "objectstore": "refused"}, actions)
        self.assertEqual(0, self.objects.pointed)

    def test_one_already_on_the_vip_is_left_as_it_is(self):
        network = Network()
        network.state["services"].append(service("homestead-vip", "homestead", [{"name": "http", "port": 8088, "target_port": 8080,
                                                                                  "protocol": "TCP"}], ["192.0.2.200"], ["192.0.2.200"]))
        self.bind(network)
        steps = {s["id"]: s["action"] for s in SELF_ADDRESS.plan("192.0.2.200")["steps"]}
        self.assertEqual("kept", steps["web"])


class ChangeVipTests(unittest.TestCase):
    def setUp(self):
        self.rows = [{"ip": "192.0.2.200", "label": "Homestead and apps", "default": True}]
        self.services = {"homestead-vip": {"metadata": {"name": "homestead-vip", "annotations": {
            "kube-vip.io/loadbalancerIPs": "192.0.2.200", "homestead.io/vip-mode": "manual"}}, "spec": {"type": "LoadBalancer"}}}
        self.sent, self.saved = [], []
        inventory = {"node_ips": ["192.0.2.21"], "services": [
            service("homestead-vip", "homestead", [{"port": 8088, "protocol": "TCP"}], ["192.0.2.200"], ["192.0.2.200"]),
            service("frigate", "frigate", [{"port": 5000, "protocol": "TCP"}], ["192.0.2.200"], ["192.0.2.200"]),
            service("other", "other", [{"port": 80, "protocol": "TCP"}], ["192.0.2.201"])]}
        self.services["frigate"] = copy.deepcopy(self.services["homestead-vip"])
        patches = [mock.patch.object(NETWORK, "inventory", lambda: copy.deepcopy(inventory)),
                   mock.patch.object(NETWORK, "registered", lambda: copy.deepcopy(self.rows)),
                   mock.patch.object(NETWORK, "check_address", lambda ip, state=None: None),
                   mock.patch.object(NETWORK, "_save_registered", lambda rows: self.saved.append(rows)),
                   mock.patch.object(NETWORK, "kget", lambda path: copy.deepcopy(self.services[path.rsplit("/", 1)[-1]])),
                   mock.patch.object(NETWORK, "ksend", lambda method, path, body=None, **k: self.sent.append((method, path, body))),
                   mock.patch.object(NETWORK.PLATFORM, "vip_annotations", lambda vip: {"kube-vip.io/loadbalancerIPs": vip})]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)

    def test_the_review_lists_everything_on_the_old_address_and_changes_nothing(self):
        plan = NETWORK.change_vip("192.0.2.200", "192.0.2.210")
        self.assertEqual(["homestead-vip", "frigate"], [s["name"] for s in plan["services"]])
        self.assertTrue(plan["default"])
        self.assertEqual(([], []), (self.sent, self.saved))

    def test_applied_every_service_moves_and_the_vip_keeps_its_label_and_default(self):
        NETWORK.change_vip("192.0.2.200", "192.0.2.210", apply=True)
        self.assertEqual(2, len(self.sent))
        for method, path, body in self.sent:
            self.assertEqual("PUT", method)
            self.assertEqual("192.0.2.210", body["metadata"]["annotations"]["kube-vip.io/loadbalancerIPs"])
            self.assertEqual("manual", body["metadata"]["annotations"]["homestead.io/vip-mode"], "other annotations kept")
        row = self.saved[-1][0]
        self.assertEqual(("192.0.2.210", "Homestead and apps", True, "192.0.2.200"),
                         (row["ip"], row["label"], row["default"], row["previous"]))

    def test_a_new_address_in_use_or_a_node_address_is_refused(self):
        for new, words in (("192.0.2.21", "node's own"), ("192.0.2.201", "already used by a Service")):
            with self.subTest(words=words), self.assertRaisesRegex(ValueError, words):
                NETWORK.change_vip("192.0.2.200", new)


class InstallerVipTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.platform = {"distribution": "k3s", "helm_controller": True, "load_balancer": "servicelb"}
        self.request = {"data": {"vip": "192.0.2.200"}}
        self.calls = []
        BASELINE.bind(self.get, type("A", (), {"status": staticmethod(lambda: {})}), lambda force=False: self.platform,
                      "lab", self.tmp.name)
        BASELINE.vip_setup = lambda vip: self.calls.append(vip) or "moved"
        self.addCleanup(setattr, BASELINE, "vip_setup", None)

    def tearDown(self):
        self.tmp.cleanup()

    def get(self, path):
        if path.endswith("/configmaps/homestead-install"):
            return self.request
        raise urllib.error.HTTPError(path, 404, "missing", {}, None)

    def test_the_installers_vip_waits_for_kube_vip_then_is_set_up_once(self):
        self.assertIsNone(BASELINE.vip_tick(), "ServiceLB alone cannot answer on a VIP")
        self.platform["load_balancer"] = "kube-vip"
        self.assertEqual({"id": "vip", "ok": True, "detail": "moved"}, BASELINE.vip_tick())
        self.assertIsNone(BASELINE.vip_tick())
        self.assertEqual(["192.0.2.200"], self.calls)


if __name__ == "__main__":
    unittest.main()
