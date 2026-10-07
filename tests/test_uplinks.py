import sys, unittest, urllib.error
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
import homestead_uplinks as UP

API = UP.API


def node(name):
    return {"metadata": {"name": name, "labels": {"kubernetes.io/hostname": name}}}


def vlanconfig(name, cn, nics, selector=None, mode="active-backup"):
    return {"metadata": {"name": name, "resourceVersion": "7", "labels": {}},
            "spec": {"clusterNetwork": cn, "nodeSelector": selector or {},
                     "uplink": {"nics": nics, "bondOptions": {"mode": mode, "miimon": 100}}}}


def vlanstatus(config, node, ready=True, message=""):
    return {"status": {"vlanConfig": config, "node": node, "clusterNetwork": "data",
                       "conditions": [{"type": "ready", "status": "True" if ready else "False", "message": message}]}}


def nic(name, carrier=True, speed=1000, master=""):
    return {"name": name, "kind": "nic", "carrier": carrier, "speed_mbps": speed if carrier else None, "master": master}


class Fake:
    def __init__(self):
        self.nodes = [node("h1"), node("h2")]
        self.cns = [{"metadata": {"name": "mgmt"}}, {"metadata": {"name": "data"}}]
        self.vcs, self.vss, self.nads, self.vmis = [], [], [], []
        self.sent = []

    def kget(self, path):
        base = path.split("?", 1)[0]
        table = {"/api/v1/nodes": self.nodes, f"{API}/clusternetworks": self.cns, f"{API}/vlanconfigs": self.vcs,
                 f"{API}/vlanstatuses": self.vss, UP.NADS: self.nads,
                 "/apis/kubevirt.io/v1/virtualmachineinstances": self.vmis}
        if base not in table:
            raise urllib.error.HTTPError(path, 404, "not found", {}, None)
        return {"items": table[base]}

    def ksend(self, verb, path, body=None, ctype=None):
        self.sent.append((verb, path, body))
        return {}


class Ops:
    def __init__(self):
        self.started = []

    def start(self, kind, title, resource, href, ref, message=""):
        self.started.append((kind, title, ref))
        return {"id": "op1", "kind": kind, "title": title}


PROBES = {"h1": {"interfaces": [nic("eno1", master="mgmt-bo"), nic("enp2s0"), nic("enp3s0"), nic("enp4s0", carrier=False)]},
          "h2": {"interfaces": [nic("eno1", master="mgmt-bo"), nic("enp2s0"), nic("enp3s0", speed=2500)]}}


class UplinkTests(unittest.TestCase):
    def setUp(self):
        self.fake = Fake()
        UP.bind(self.fake.kget, self.fake.ksend)

    def test_inventory_lists_networks_hosts_and_who_holds_each_nic(self):
        self.fake.vcs = [vlanconfig("data-h1", "data", ["enp2s0"], {"kubernetes.io/hostname": "h1"})]
        self.fake.vss = [vlanstatus("data-h1", "h1")]
        inv = UP.inventory(PROBES)
        self.assertEqual(["mgmt", "data"], [n["name"] for n in inv["networks"]])
        data = inv["networks"][1]
        self.assertEqual(["h1"], data["configs"][0]["nodes"])
        self.assertTrue(data["configs"][0]["status"]["h1"]["ready"])
        self.assertEqual(["h2"], data["uncovered"])
        held = {n["name"]: n["used_by"] for n in inv["hosts"]["h1"]}
        self.assertEqual(("mgmt", "data", ""), (held["eno1"], held["enp2s0"], held["enp3s0"]))

    def test_not_harvester(self):
        self.fake.kget = lambda path: (_ for _ in ()).throw(urllib.error.HTTPError(path, 404, "", {}, None))
        UP.bind(self.fake.kget, self.fake.ksend)
        self.assertFalse(UP.inventory(PROBES)["applies"])
        with self.assertRaisesRegex(ValueError, "not a Harvester cluster"):
            UP.preview({"cluster_network": "data"}, PROBES)

    def test_a_new_bond_per_host(self):
        plan = UP.preview({"cluster_network": "data", "nodes": ["h1", "h2"], "nics": ["enp2s0", "enp3s0"]}, PROBES)
        self.assertEqual([], plan["refusals"])
        self.assertEqual(["POST", "POST"], [w["verb"] for w in plan["writes"]])
        body = plan["writes"][0]["body"]
        self.assertEqual(("data-h1", {"kubernetes.io/hostname": "h1"}), (body["metadata"]["name"], body["spec"]["nodeSelector"]))
        self.assertEqual({"nics": ["enp2s0", "enp3s0"], "bondOptions": {"mode": "active-backup", "miimon": 100}}, body["spec"]["uplink"])
        self.assertTrue(any("different speeds" in w for w in plan["warnings"]))

    def test_a_new_cluster_network_is_made_first(self):
        plan = UP.preview({"cluster_network": "storage", "new_network": True, "nodes": ["h1"], "nics": ["enp2s0"], "mtu": 9000}, PROBES)
        self.assertEqual([], plan["refusals"])
        self.assertEqual("ClusterNetwork", plan["writes"][0]["body"]["kind"])
        self.assertEqual({"mtu": 9000}, plan["writes"][1]["body"]["spec"]["uplink"]["linkAttributes"])

    def test_refusals(self):
        cases = [
            ({"cluster_network": "mgmt", "nodes": ["h1"], "nics": ["enp2s0"]}, "set when Harvester installs"),
            ({"cluster_network": "data", "nodes": ["h1"], "nics": ["eno1"]}, "eno1 on h1 already carries mgmt"),
            ({"cluster_network": "data", "nodes": ["h1"], "nics": ["enp4s0"]}, "has no link"),
            ({"cluster_network": "data", "nodes": ["h2"], "nics": ["enp9s0"]}, "h2 has no NIC enp9s0"),
            ({"cluster_network": "data", "nodes": ["h1"], "nics": ["enp2s0", "enp3s0"], "mode": "802.3ad"}, "LACP group"),
            ({"cluster_network": "data", "nodes": ["h1"], "nics": ["enp2s0"], "mtu": 20000}, "outside 576 to 9000"),
            ({"cluster_network": "toolongname-x", "new_network": True, "nodes": ["h1"], "nics": ["enp2s0"]}, "15-character"),
            ({"cluster_network": "data", "new_network": True, "nodes": ["h1"], "nics": ["enp2s0"]}, "already has a cluster network"),
            ({"cluster_network": "data", "nodes": [], "nics": ["enp2s0"]}, "at least one host"),
            ({"cluster_network": "data", "nodes": ["h1"], "nics": ["<img src=x>"]}, "is not a NIC name"),
        ]
        for cfg, words in cases:
            with self.subTest(words=words):
                self.assertIn(words, " ".join(UP.preview(cfg, PROBES)["refusals"]))
        # Overridden on purpose: no link is a warning, LACP confirmed is fine.
        plan = UP.preview({"cluster_network": "data", "nodes": ["h1"], "nics": ["enp2s0", "enp4s0"], "allow_down": True,
                           "mode": "802.3ad", "lacp_confirmed": True}, PROBES)
        self.assertEqual([], plan["refusals"])

    def test_a_host_with_an_uplink_already_is_changed_not_added(self):
        self.fake.vcs = [vlanconfig("data-all", "data", ["enp2s0"])]
        plan = UP.preview({"cluster_network": "data", "nodes": ["h1"], "nics": ["enp3s0"]}, PROBES)
        self.assertIn("change that one instead", " ".join(plan["refusals"]))

    def test_change_names_every_host_and_the_vms_in_the_way(self):
        self.fake.vcs = [vlanconfig("data-all", "data", ["enp2s0"])]
        plan = UP.preview({"action": "change", "config": "data-all", "nics": ["enp2s0", "enp3s0"]}, PROBES)
        self.assertEqual([], plan["refusals"])
        self.assertEqual(["h1", "h2"], plan["nodes"])
        self.assertIn("they all change", " ".join(plan["warnings"]))
        self.assertEqual(("PATCH", {"metadata": {"resourceVersion": "7"}, "spec": {"uplink": {
            "nics": ["enp2s0", "enp3s0"], "bondOptions": {"mode": "active-backup", "miimon": 100}}}}),
            (plan["writes"][0]["verb"], plan["writes"][0]["body"]))
        self.fake.nads = [{"metadata": {"namespace": "lab", "name": "vlan20"}}]
        self.fake.vmis = [{"metadata": {"namespace": "lab", "name": "nas"}, "status": {"nodeName": "h2"},
                           "spec": {"networks": [{"name": "lan", "multus": {"networkName": "vlan20"}}]}}]
        plan = UP.preview({"action": "change", "config": "data-all", "nics": ["enp2s0", "enp3s0"]}, PROBES)
        self.assertIn("lab/nas", " ".join(plan["refusals"]))
        plan = UP.preview({"action": "remove", "config": "data-all"}, PROBES)
        self.assertIn("Stop or move them first", " ".join(plan["refusals"]))

    def test_apply_writes_what_was_reviewed_and_starts_a_job(self):
        cfg = {"cluster_network": "data", "nodes": ["h1"], "nics": ["enp2s0", "enp3s0"]}
        plan = UP.preview(cfg, PROBES)
        ops = Ops()
        UP.apply({**cfg, "digest": plan["digest"]}, ops, PROBES)
        self.assertEqual([("POST", f"{API}/vlanconfigs")], [(v, p) for v, p, _ in self.fake.sent])
        kind, title, ref = ops.started[0]
        self.assertEqual((UP.KIND, ["data-h1"], ["h1"]), (kind, ref["configs"], ref["nodes"]))
        self.assertIn("enp2s0 + enp3s0 (active-backup)", title)

    def test_apply_refuses_a_stale_review_and_a_refused_plan(self):
        with self.assertRaisesRegex(ValueError, "review it again"):
            UP.apply({"cluster_network": "data", "nodes": ["h1"], "nics": ["enp2s0"], "digest": "stale"}, Ops(), PROBES)
        with self.assertRaisesRegex(ValueError, "already carries mgmt"):
            UP.apply({"cluster_network": "data", "nodes": ["h1"], "nics": ["eno1"]}, Ops(), PROBES)
        self.assertEqual([], self.fake.sent)

    def test_the_job_follows_each_hosts_vlanstatus(self):
        item = {"ref": {"action": "create", "cluster_network": "data", "configs": ["data-h1", "data-h2"],
                        "nodes": ["h1", "h2"], "since": 1000}}
        self.fake.vss = [vlanstatus("data-h1", "h1"), vlanstatus("data-h2", "h2", False, "nic enp3s0 not found")]
        status, progress, message = UP.status(item, now=1060)
        self.assertEqual("running", status)
        self.assertIn("h2: nic enp3s0 not found", message)
        status, _, message = UP.status(item, now=1400)
        self.assertEqual("failed", status)
        self.fake.vss[1] = vlanstatus("data-h2", "h2")
        self.assertEqual("succeeded", UP.status(item, now=1100)[0])

    def test_removal_is_done_when_the_statuses_are_gone(self):
        item = {"ref": {"action": "remove", "cluster_network": "data", "configs": ["data-h1"], "nodes": ["h1"], "since": 1000}}
        self.fake.vss = [vlanstatus("data-h1", "h1")]
        self.assertEqual("running", UP.status(item, now=1010)[0])
        self.fake.vss = []
        self.assertEqual("succeeded", UP.status(item, now=1020)[0])


if __name__ == "__main__":
    unittest.main()
