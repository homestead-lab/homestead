"""Is this Longhorn replica a whole copy? Every place that asks, over the same
replica shapes (#1126), so where they disagree is in one table.

The shapes start from objects recorded read-only on a Longhorn 1.13 cluster
and generalised (tests/fixtures/longhorn_v1_13/): an attached volume with two
running RW replicas, and a detached volume with two stopped ones. Every other
shape is built from those by changing only the fields Longhorn changes for it
(see SHAPES); none of them was live when recorded.

The replica asked about is always the copy on node-b. The volume's other copy,
on node-a, stays whole, and node-a is the host each check is about (power off
node-a: does a whole copy survive on node-b?).

This pins what each place answers today, next to what the one rule
(homestead_whole_copy) says in the last column. The commits that move each
place onto the rule change only that place's column.
"""
import copy
import json
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
import homestead_app_copies as APP_COPIES
import homestead_data_copies as DATA_COPIES
import homestead_disk_v2 as DISK_V2
import homestead_disks as DISKS
import homestead_lhv2_upgrade as LHV2_UPGRADE
import homestead_power as POWER
import homestead_rebalance as REBALANCE
import homestead_whole_copy as WHOLE
import test_lhv2_upgrade as UPGRADE_FIXTURE
import test_node_power_plan as POWER_FIXTURE

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "longhorn_v1_13"


def recorded(name):
    return json.loads((FIXTURES / name).read_text())["items"]


def by_name(items):
    return {item["metadata"]["name"]: item for item in items}


def attached():
    """vol-a as recorded: two running RW replicas, vol-a-r-1 on node-b."""
    return {"volume": by_name(recorded("volumes.json"))["vol-a"], "engine": by_name(recorded("engines.json"))["vol-a-e-0"],
            "replicas": [r for r in recorded("replicas.json") if r["spec"]["volumeName"] == "vol-a"],
            "asked": "vol-a-r-1", "nodes": recorded("nodes.json"), "lh_nodes": recorded("lh-nodes.json")}


def detached():
    """vol-b as recorded: detached, both replicas stopped, its engine stopped."""
    return {"volume": by_name(recorded("volumes.json"))["vol-b"], "engine": by_name(recorded("engines.json"))["vol-b-e-0"],
            "replicas": [r for r in recorded("replicas.json") if r["spec"]["volumeName"] == "vol-b"],
            "asked": "vol-b-r-2", "nodes": recorded("nodes.json"), "lh_nodes": recorded("lh-nodes.json")}


def asked(shape):
    return next(r for r in shape["replicas"] if r["metadata"]["name"] == shape["asked"])


def built(change):
    def make():
        shape = attached()
        change(shape, asked(shape))
        return shape
    return make


def _rebuilding(shape, replica, healthy_at=""):
    # Longhorn adds the copy to the engine as WO, rebuilds it, and sets
    # healthyAt only when the engine has made it RW.
    replica["spec"]["healthyAt"] = healthy_at
    shape["engine"]["status"]["replicaModeMap"][replica["metadata"]["name"]] = "WO"
    shape["engine"]["status"]["rebuildStatus"] = {"tcp://192.0.2.10:10000": {"isRebuilding": True, "progress": 40,
                                                                            "state": "in_progress", "fromReplicaAddress": ""}}
    shape["volume"]["status"]["robustness"] = "degraded"


def _failed(shape, replica):
    # The controller marks it: failedAt set, healthyAt cleared, the engine drops it.
    replica["spec"].update(failedAt="2026-11-01T10:00:00Z", healthyAt="", desireState="stopped")
    replica["status"]["currentState"] = "stopped"
    shape["engine"]["status"]["replicaModeMap"].pop(replica["metadata"]["name"])
    shape["volume"]["status"]["robustness"] = "degraded"


def _err(shape, replica):
    # The engine has seen the error; the controller has not yet marked failedAt (status lags).
    shape["engine"]["status"]["replicaModeMap"][replica["metadata"]["name"]] = "ERR"
    shape["volume"]["status"]["robustness"] = "degraded"


def _not_in_engine(shape, replica):
    shape["engine"]["status"]["replicaModeMap"].pop(replica["metadata"]["name"])


def _stopped_attached(shape, replica):
    # Its instance manager went away: stopped, still healthyAt, not in the engine.
    replica["status"]["currentState"] = "stopped"
    shape["engine"]["status"]["replicaModeMap"].pop(replica["metadata"]["name"])
    shape["volume"]["status"]["robustness"] = "degraded"


def _engine_down(keep_map):
    def change(shape, replica):
        # The volume still says attached, but its engine is not running and its
        # copies have stopped (healthyAt set): Longhorn is not serving it.
        shape["engine"]["status"]["currentState"] = "stopped"
        if not keep_map:
            shape["engine"]["status"]["replicaModeMap"] = {}
        for r in shape["replicas"]:
            r["status"]["currentState"] = "stopped"
        shape["volume"]["status"]["robustness"] = "unknown"
    return change


def _host_not_ready(shape, replica):
    # The replica's status still says running RW: Ready goes False ~40 s before Longhorn reacts.
    for node in shape["nodes"] + shape["lh_nodes"]:
        if node["metadata"]["name"] == "node-b":
            for c in node["status"]["conditions"]:
                if c["type"] == "Ready":
                    c["status"] = "False"


def _host_cordoned(shape, replica):
    next(n for n in shape["nodes"] if n["metadata"]["name"] == "node-b")["spec"]["unschedulable"] = True


def _deleting(shape, replica):
    replica["metadata"]["deletionTimestamp"] = "2026-11-01T10:00:00Z"


# name: (how it is made, recorded or built)
SHAPES = {
    "healthy, attached": (attached, "recorded"),
    "stopped, detached volume": (detached, "recorded"),
    "rebuilding (WO), new copy": (built(_rebuilding), "built"),
    "rebuilding (WO), healthyAt still set": (built(lambda s, r: _rebuilding(s, r, r["spec"]["healthyAt"])), "built"),
    "failed (failedAt)": (built(_failed), "built"),
    "running, engine says ERR": (built(_err), "built"),
    "running, not in the engine": (built(_not_in_engine), "built"),
    "stopped, attached volume": (built(_stopped_attached), "built"),
    "running RW, host NotReady": (built(_host_not_ready), "built"),
    "running RW, host cordoned": (built(_host_cordoned), "built"),
    "running RW, being deleted": (built(_deleting), "built"),
    "attached, engine stopped, map cleared": (built(_engine_down(False)), "built"),
    "attached, engine stopped, map kept": (built(_engine_down(True)), "built"),
}

# Each place's answer for the copy on node-b: "whole", "not", or "-" where it
# does not ask about that shape. app_copies and data_copies ask about the
# volume ("a copy is rebuilding"): "rebuilding" or "-".
PLACES = ("power", "rebalance._whole", "rebalance._judge (was _built)", "disk_v2._rw", "disks._whole_copies (was _healthy)", "lhv2_upgrade",
          "app_copies", "data_copies", "the rule")
# Where a place disagrees with the rule it says "whole" and the rule "not":
# moving that place onto the rule can only refuse or delay a power-off, move
# or retire that it allows today. disk_v2 says "not" for a detached volume's
# stopped copy on purpose (the conversion needs it running); lhv2_upgrade
# reads the cordon as "not eligible" on its own.
EXPECTED = {
    #                                        power    _whole   _built   disk_v2  disks    upgrade  app_copies    data_copies   rule
    'healthy, attached': ('whole', 'whole', 'whole', 'whole', 'whole', 'whole', '-', '-', 'whole'),
    'stopped, detached volume': ('whole', 'whole', 'whole', 'not', 'whole', '-', '-', '-', 'whole'),
    'rebuilding (WO), new copy': ('not', 'not', 'not', 'not', 'not', 'not', 'rebuilding', 'rebuilding', 'not'),
    'rebuilding (WO), healthyAt still set': ('not', 'not', 'not', 'not', 'not', 'not', 'rebuilding', 'rebuilding', 'not'),
    'failed (failedAt)': ('not', 'not', 'not', 'not', 'not', 'not', '-', '-', 'not'),
    'running, engine says ERR': ('not', 'not', 'not', 'not', 'not', 'not', '-', '-', 'not'),
    'running, not in the engine': ('not', 'not', 'not', 'not', 'not', 'not', '-', '-', 'not'),
    'stopped, attached volume': ('not', 'not', 'not', 'not', 'not', 'not', '-', '-', 'not'),
    'running RW, host NotReady': ('not', 'not', 'not', 'not', 'not', 'not', '-', '-', 'not'),
    'running RW, host cordoned': ('whole', 'whole', 'whole', 'whole', 'whole', 'not', '-', '-', 'whole'),
    'running RW, being deleted': ('not', 'not', 'not', 'not', 'not', 'not', '-', '-', 'not'),
    # Attached, but no engine running: Longhorn is not serving it.
    'attached, engine stopped, map cleared': ('not', 'not', 'not', 'not', 'not', 'not', '-', '-', 'not'),
    'attached, engine stopped, map kept': ('not', 'not', 'not', 'not', 'not', 'not', '-', '-', 'not'),
}


class Places:
    """Each place, asked about one shape, through its own code."""

    def __init__(self, test, shape):
        self.test, self.shape = test, shape
        self.volume_name = shape["volume"]["metadata"]["name"]

    def power(self):
        t = self.test
        POWER_FIXTURE.PowerPlanTests.setUp(t)
        s = self.shape
        t.objects["/apis/kubevirt.io/v1/virtualmachineinstances"]["items"] = []
        t.objects["/api/v1/pods"]["items"] = []
        t.objects["/api/v1/nodes"] = {"items": copy.deepcopy(s["nodes"])}
        t.objects["/api/v1/nodes/node-a"] = dict(next(n for n in s["nodes"] if n["metadata"]["name"] == "node-a"),
                                                 metadata={"name": "node-a", "uid": "node-a-uid"})
        t.objects[f"{POWER.LH}/replicas"] = {"items": copy.deepcopy(s["replicas"])}
        t.objects[f"{POWER.LH}/engines"] = {"items": [copy.deepcopy(s["engine"])]}
        t.objects[f"{POWER.LH}/volumes"] = {"items": [copy.deepcopy(s["volume"])]}
        row = next(v for v in POWER.plan("node-a", "reboot")["volumes"] if v["name"] == self.volume_name)
        return "whole" if row["healthy_elsewhere"] == 1 else "not"

    def _cluster(self, path):
        s = self.shape
        return copy.deepcopy({f"{REBALANCE.LH}/engines": [s["engine"]], "/api/v1/nodes": s["nodes"],
                              f"{REBALANCE.LH}/nodes": s["lh_nodes"], f"{REBALANCE.LH}/volumes": [s["volume"]]}[path])

    def rebalance_whole(self):
        """Its review (inventory) and its steps ask the same judge."""
        with mock.patch.object(REBALANCE, "_items", self._cluster):
            return "whole" if REBALANCE._whole(asked(self.shape)) else "not"

    def rebalance_built(self):
        """A step's "is the new copy built": the judge _built used to be, over one read."""
        with mock.patch.object(REBALANCE, "_items", self._cluster):
            return "whole" if REBALANCE._judge()(asked(self.shape)) else "not"

    def disk_v2_rw(self):
        s = self.shape
        inv = {"engines": [s["engine"]], "replicas": s["replicas"],
               "kube": by_name(s["nodes"]), "lhs": by_name(s["lh_nodes"])}
        names = [r["metadata"]["name"] for r in DISK_V2._rw(inv, self.volume_name)]
        return "whole" if s["asked"] in names else "not"

    def disks_healthy(self):
        s = self.shape
        reads = {f"{DISKS.LH}/engines": {"items": [s["engine"]]}, "/api/v1/nodes": {"items": s["nodes"]},
                 f"{DISKS.LH}/volumes": {"items": [s["volume"]]}}
        with mock.patch.object(DISKS, "kget", lambda path: copy.deepcopy(reads[path])), \
                mock.patch.object(DISKS, "_lh_nodes", lambda: by_name(copy.deepcopy(s["lh_nodes"]))):
            return "whole" if DISKS._whole_copies()(asked(self.shape)) else "not"

    def lhv2_upgrade(self):
        """The live V2 upgrade: two whole copies on eligible hosts. Its fixture's
        test-disk takes this shape: replica-1 (node-1) whole, replica-2 (node-2)
        the copy asked about, no third. The volume is kept attached and healthy
        here, so only the copy decides."""
        s = self.shape
        if s["volume"]["status"]["state"] != "attached":
            return "-"                       # it skips detached volumes
        cluster = UPGRADE_FIXTURE.Cluster(self.test, installed="v1.12.2")
        mine = asked(s)
        cluster.edit(cluster.replica(2), lambda r: (r["spec"].update(
            {k: mine["spec"].get(k, "") for k in ("healthyAt", "failedAt")}), r["status"].update(currentState=mine["status"]["currentState"]),
            r["metadata"].update({k: v for k, v in mine["metadata"].items() if k == "deletionTimestamp"})))
        cluster.remove(cluster.replica(3))
        cluster.edit(cluster.replica(1), lambda r: r["spec"].update(healthyAt="2026-11-01T08:00:00Z"))
        mode = s["engine"]["status"]["replicaModeMap"].get(s["asked"])
        cluster.edit(cluster.engine(), lambda e: e["status"].update(
            replicaModeMap={"replica-1": "RW", **({"replica-2": mode} if mode else {})}))
        b = next(n for n in s["nodes"] if n["metadata"]["name"] == "node-b")
        cluster.edit(cluster.node(2), lambda n: (n["status"].update(conditions=b["status"]["conditions"]),
                                                n.setdefault("spec", {}).update(b.get("spec") or {})))
        lb = next(n for n in s["lh_nodes"] if n["metadata"]["name"] == "node-b")
        ready = next(c["status"] for c in lb["status"]["conditions"] if c["type"] == "Ready")
        cluster.edit(cluster.lh_node(2), lambda n: [c.update(status=ready) for c in n["status"]["conditions"] if c["type"] == "Ready"])
        blockers = LHV2_UPGRADE.plan("v1.13.0")["live_blockers"]
        return "not" if any("RW replicas on at least two eligible hosts" in b for b in blockers) else "whole"

    def _engines(self, path):
        return {"items": [copy.deepcopy(self.shape["engine"])]}

    def app_copies(self):
        with mock.patch.object(APP_COPIES.NODE_PARITY, "kget", self._engines):
            return "rebuilding" if self.volume_name in APP_COPIES._rebuilding() else "-"

    def data_copies(self):
        with mock.patch.object(DATA_COPIES.NODE_PARITY, "kget", self._engines):
            return "rebuilding" if DATA_COPIES._rebuilding(self.volume_name) else "-"

    def rule(self):
        """homestead_whole_copy as power, rebalance and disk retire will ask it:
        a detached volume's stopped copies count, and the hosts are read."""
        s = self.shape
        whole = WHOLE.judge([s["engine"]], detached_ok=True, volumes=[s["volume"]],
                            nodes=by_name(s["nodes"]), lh_nodes=by_name(s["lh_nodes"]))
        return "whole" if whole(asked(s)) else "not"

    def answers(self):
        calls = (self.power, self.rebalance_whole, self.rebalance_built, self.disk_v2_rw, self.disks_healthy,
                 self.lhv2_upgrade, self.app_copies, self.data_copies, self.rule)
        return tuple(call() for call in calls)


class WholeReplicaTableTests(unittest.TestCase):
    get = POWER_FIXTURE.PowerPlanTests.get

    def test_every_place_over_every_shape(self):
        for name, (make, _) in SHAPES.items():
            with self.subTest(shape=name):
                self.assertEqual(EXPECTED[name], Places(self, make()).answers())

    def test_the_recorded_objects_carry_the_fields_the_rules_read(self):
        replicas, engines = recorded("replicas.json"), recorded("engines.json")
        self.assertTrue(all(r["spec"].get("healthyAt") for r in replicas))
        self.assertTrue(all("failedAt" in r["spec"] for r in replicas))
        self.assertEqual({"vol-a-r-1": "RW", "vol-a-r-2": "RW"}, by_name(engines)["vol-a-e-0"]["status"]["replicaModeMap"])


if __name__ == "__main__":
    unittest.main()
