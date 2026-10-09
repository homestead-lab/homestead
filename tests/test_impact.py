import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
import homestead_impact as IMPACT

NODES = [{"name": "node1", "status": "Ready", "schedulable": True, "hardware": {"igpu": True}},
         {"name": "node2", "status": "Ready", "schedulable": True, "hardware": {"igpu": True, "coral_usb": True}},
         {"name": "node3", "status": "Ready", "schedulable": True, "hardware": {}}]


def app(name, nodes=("node2",), failover="move", hardware=(), claims=(), desired=1, **extra):
    row = {"ns": "lab", "name": name, "nodes": list(nodes), "failover": failover, "hardware": list(hardware),
           "claims": list(claims), "desired": desired}
    row.update(extra)
    return row


def dep(name, podspec=None):
    return {"metadata": {"namespace": "lab", "name": name}, "spec": {"template": {"spec": podspec or {}}}}


def vol(claim, copies):
    return {"name": f"pvc-{claim}", "pvc_name": claim, "namespace": "lab", "replicas": len(copies),
            "copies": [{"node": n, "healthy": h} for n, h in copies]}


class AppTests(unittest.TestCase):
    def run_preview(self, workloads, deployments=(), volumes=(), vms=(), raw_vms=(), addresses=()):
        return IMPACT.preview("node2", list(workloads), list(deployments), list(vms), list(raw_vms), list(volumes), list(addresses), NODES)

    def outcome(self, result, name):
        return next(r for r in result["apps"] if r["name"] == name)

    def test_a_movable_app_moves_after_its_failover_delay(self):
        r = self.outcome(self.run_preview([app("web")], volumes=[vol("web-data", [("node2", True), ("node1", True)])]), "web")
        self.assertEqual(("moves", ["node1", "node3"]), (r["outcome"], r["to"]))
        self.assertIn("15 seconds", r["why"])
        r = self.outcome(self.run_preview([app("web", failover="default")]), "web")
        self.assertIn("5 minutes", r["why"])

    def test_an_app_set_to_wait_waits(self):
        self.assertEqual("waits", self.outcome(self.run_preview([app("db", failover="wait")]), "db")["outcome"])

    def test_hardware_only_on_this_host_stops_it(self):
        r = self.outcome(self.run_preview([app("frigate", hardware=["coral_usb"])]), "frigate")
        self.assertEqual("stops", r["outcome"])
        self.assertIn("coral_usb", r["why"])
        r = self.outcome(self.run_preview([app("plex", hardware=["igpu"])]), "plex")
        self.assertEqual(["node1"], r["to"], "only hosts with its iGPU")

    def test_a_volume_whose_only_copy_is_here_stops_it(self):
        r = self.outcome(self.run_preview([app("ha", claims=["ha-config"])], volumes=[vol("ha-config", [("node2", True), ("node1", False)])]), "ha")
        self.assertEqual("stops", r["outcome"])
        self.assertIn("only healthy copy on node2", r["why"])

    def test_pinned_to_this_host_stops_it(self):
        pinned = {"nodeSelector": {"kubernetes.io/hostname": "node2"}}
        r = self.outcome(self.run_preview([app("pinned")], deployments=[dep("pinned", pinned)]), "pinned")
        self.assertEqual("stops", r["outcome"])
        self.assertIn("may only run on node2", r["why"])

    def test_required_affinity_narrows_where_it_goes(self):
        spec = {"affinity": {"nodeAffinity": {"requiredDuringSchedulingIgnoredDuringExecution": {"nodeSelectorTerms": [
            {"matchExpressions": [{"key": "kubernetes.io/hostname", "operator": "In", "values": ["node2", "node3"]}]}]}}}}
        self.assertEqual(["node3"], self.outcome(self.run_preview([app("a")], deployments=[dep("a", spec)]), "a")["to"])

    def test_copies_elsewhere_keep_answering(self):
        r = self.outcome(self.run_preview([app("web", nodes=["node1", "node2"], desired=2)]), "web")
        self.assertTrue(r["keeps_answering"])

    def test_apps_elsewhere_and_stopped_apps_are_not_listed(self):
        res = self.run_preview([app("elsewhere", nodes=["node1"]), app("stopped", desired=0)])
        self.assertEqual([], res["apps"])

    def test_the_placement_planners_answer_decides_where(self):
        placement = {"workloads": [{"ns": "lab", "name": "a", "eligible": ["node3"], "blocked": [{"name": "node1", "why": ["lacks label zone=b"]}]},
                                   {"ns": "lab", "name": "b", "eligible": [], "blocked": [{"name": "node1", "why": ["no /dev/ttyUSB0"]},
                                                                                            {"name": "node3", "why": ["no /dev/ttyUSB0"]}]}]}
        res = IMPACT.preview("node2", [app("a"), app("b")], [], [], [], [], [], NODES, placement)
        self.assertEqual(["node3"], self.outcome(res, "a")["to"])
        b = self.outcome(res, "b")
        self.assertEqual("stops", b["outcome"])
        self.assertIn("no /dev/ttyUSB0", b["why"])

    def test_a_volume_not_on_longhorn_is_noted(self):
        r = self.outcome(self.run_preview([app("nas", claims=["nfs-media"])]), "nas")
        self.assertEqual("moves", r["outcome"])
        self.assertIn("not a Longhorn volume", r["notes"][0])


class VmTests(AppTests):
    def vm(self, name="ha-os", **extra):
        row = {"ns": "lab", "name": name, "node": "node2", "running": True, "migratable": True, "disks": [{"claim": f"{name}-disk"}]}
        row.update(extra)
        return row

    def raw(self, name="ha-os", devices=None):
        return {"metadata": {"namespace": "lab", "name": name},
                "spec": {"template": {"spec": {"domain": {"devices": devices or {}}}}}}

    def test_a_vm_moves_and_says_it_moves_live_on_a_drain(self):
        r = self.outcome(self.run_preview([], vms=[self.vm()], raw_vms=[self.raw()],
                                          volumes=[vol("ha-os-disk", [("node2", True), ("node3", True)])]), "ha-os")
        self.assertEqual("moves", r["outcome"])
        self.assertIn("moves live", r["why"])

    def test_a_passed_through_device_keeps_the_vm_here(self):
        raw = self.raw(devices={"gpus": [{"name": "gpu1", "deviceName": "nvidia.com/GA102"}]})
        r = self.outcome(self.run_preview([], vms=[self.vm()], raw_vms=[raw]), "ha-os")
        self.assertEqual("stops", r["outcome"])
        self.assertIn("nvidia.com/GA102", r["why"])

    def test_stopped_vms_and_vms_elsewhere_are_not_listed(self):
        res = self.run_preview([], vms=[self.vm(running=False), self.vm("other", node="node1")])
        self.assertEqual([], res["apps"])


class StorageAndAddressTests(AppTests):
    def test_volumes_at_risk_and_with_fewer_copies(self):
        res = self.run_preview([], volumes=[vol("only-here", [("node2", True)]), vol("two", [("node2", True), ("node1", True)]),
                                            vol("elsewhere", [("node1", True), ("node3", True)])])
        self.assertEqual(["only-here"], [v["name"] for v in res["at_risk"]])
        self.assertEqual([("two", 1, 2)], [(v["name"], v["left"], v["wanted"]) for v in res["fewer_copies"]])

    def test_addresses_this_host_answers_for_move(self):
        res = self.run_preview([], addresses=[{"ip": "192.0.2.240", "kind": "vip", "node": "node2", "services": ["lab/web"]},
                                              {"ip": "192.0.2.12", "kind": "node", "node": "node2"},
                                              {"ip": "192.0.2.241", "kind": "vip", "node": "node1"}])
        self.assertEqual(["192.0.2.240"], [a["ip"] for a in res["addresses"]], "its own address goes down with it")

    def test_counts_and_order(self):
        res = self.run_preview([app("moves"), app("stops", hardware=["coral_usb"]), app("waits", failover="wait")])
        self.assertEqual(["stops", "waits", "moves"], [r["name"] for r in res["apps"]])
        self.assertEqual({"stops": 1, "waits": 1, "moves": 1}, {k: res["counts"][k] for k in ("stops", "waits", "moves")})


class NodeImpactTests(unittest.TestCase):
    """node_impact() as the API calls it, with the network inventory's real
    shape: its "addresses" is the address map, nodes and addresses together."""

    def test_the_preview_reads_the_address_list_inside_the_network_inventory(self):
        import server
        inventory = {"addresses": {"nodes": [{"name": "node2", "ips": ["192.0.2.12"], "ready": True}],
                                   "addresses": [{"ip": "192.0.2.240", "kind": "vip", "node": "node2", "services": ["lab/web"]},
                                                 {"ip": "192.0.2.12", "kind": "node", "node": "node2"},
                                                 {"ip": "192.0.2.241", "kind": "vip", "node": "node1"}]}}
        with mock.patch.dict(server._cache, clear=True), \
                mock.patch.object(server, "get_nodes", lambda: NODES), \
                mock.patch.object(server, "kget", lambda path: {"items": []}), \
                mock.patch.object(server.NETWORK, "inventory", lambda: inventory), \
                mock.patch.object(server.PLACE, "impact", lambda node: {"workloads": []}), \
                mock.patch.object(server, "get_workloads", lambda: []), \
                mock.patch.object(server.VMS, "list_vms", lambda: []), \
                mock.patch.object(server, "get_volumes", lambda: []):
            res = server.node_impact("node2")
        self.assertEqual(["192.0.2.240"], [a["ip"] for a in res["addresses"]])


if __name__ == "__main__":
    unittest.main()
