"""Longhorn that could not be read is not Longhorn with nothing in it (#1002).

A failed volume, replica or Longhorn node read used to answer as an empty
cluster: the faulted-volume and disk-not-ready alerts resolved with an "all
clear" push, the Volumes page and list_volumes listed no volumes, Hosts >
Storage showed Longhorn drives as unused, and a host's impact preview said no
volume would lose its only copy. Everything here runs on the shared fake."""
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import homestead_alerts as ALERTS
import homestead_impact as IMPACT
from test_page_snapshot import Cluster, clean_cache, clean_snapshot, setUpModule  # noqa: F401 (restart record off)

VOLUMES = "/apis/longhorn.io/v1beta2/volumes"
REPLICAS = "/apis/longhorn.io/v1beta2/namespaces/longhorn-system/replicas"
LH_NODES = "/apis/longhorn.io/v1beta2/namespaces/longhorn-system/nodes"


def lh_disk_not_ready(cluster, node="node-a"):
    """node-a's Longhorn disk reports itself not ready."""
    path = f"{LH_NODES}/{node}"
    obj = cluster.kube.object(path)
    obj["status"]["diskStatus"]["d1"]["conditions"] = [{"type": "Ready", "status": "False", "message": "gone"}]
    cluster.kube.add(obj)


class LonghornFixture(unittest.TestCase):
    def setUp(self):
        clean_cache(self)
        self.PAGES = clean_snapshot(self)
        self.cluster = Cluster(self)
        self.OV = self.cluster.server.OVERVIEW_ROUTES
        self.STORAGE = self.cluster.server.STORAGE
        self.enterContext(mock.patch.object(self.STORAGE.VOLUME_USAGE, "collect", lambda get, claims: {}))
        # V2 disk preparations live in the data dir, which these tests have none of.
        self.enterContext(mock.patch.object(self.cluster.server.DISKS, "v2_tasks", lambda: []))
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        old = ALERTS.DATA_DIR
        ALERTS.bind(directory.name)
        self.addCleanup(ALERTS.bind, old)

    def break_read(self, path):
        self.cluster.kube.fail(path, 500, times=999)


class VolumesTests(LonghornFixture):
    def test_a_failed_volume_read_raises_and_says_longhorn_could_not_be_read(self):
        self.break_read(VOLUMES)
        with self.assertRaisesRegex(RuntimeError, "Longhorn"):
            self.STORAGE.get_volumes()

    def test_the_volumes_route_and_list_volumes_say_longhorn_could_not_be_read(self):
        self.break_read(VOLUMES)
        with self.assertRaisesRegex(RuntimeError, "Longhorn's volumes could not be read"):
            self.cluster.route("vol")

    def test_a_cluster_without_longhorn_still_has_no_volumes(self):
        self.cluster.kube.absent_groups.add("longhorn.io")
        self.assertEqual([], self.STORAGE.get_volumes())

    def test_a_normal_read_lists_the_volume_with_its_copies_known(self):
        rows = self.STORAGE.get_volumes()
        self.assertEqual(["pvc-1"], [r["name"] for r in rows])
        self.assertEqual([], rows[0]["copies"])   # the fake cluster has no replicas: known to be none

    def test_a_failed_replica_read_leaves_copies_unknown_not_none(self):
        self.break_read(REPLICAS)
        rows = self.STORAGE.get_volumes()
        self.assertEqual(["pvc-1"], [r["name"] for r in rows])
        self.assertIsNone(rows[0]["copies"])
        self.assertIsNone(rows[0]["copies_short"])


class SnapshotAndOverviewTests(LonghornFixture):
    def test_the_snapshot_does_not_keep_a_failed_volume_read_as_fresh(self):
        self.break_read(VOLUMES)
        done = self.PAGES.cycle(self.OV.page_inputs(), self.OV.page_builders())
        self.assertFalse(done["vol"])
        self.assertIsNone(self.PAGES.read("vol"))
        # The Dashboard still draws, and says the volumes were not read.
        self.assertTrue(done["ov"])
        self.assertTrue(self.PAGES.read("ov")[1]["volumes_unread"])

    def test_a_normal_overview_has_its_volumes_read(self):
        self.assertFalse(self.OV.get_overview()["volumes_unread"])

    def test_open_volume_alerts_stay_open_and_no_cleared_push_is_sent(self):
        key = "health:Volume:data"
        overview = self.OV.get_overview()
        faulted = {"key": key, "category": "outage", "severity": "critical", "title": "Volume data is faulted",
                   "resolved": "Volume warning cleared: data", "body": "", "signals": {"condition": 1}, "href": "/volumes"}
        ALERTS.observe({"health": ALERTS.health_facts(overview) + [faulted]}, now=1000)
        ALERTS.observe({"health": ALERTS.health_facts(overview) + [faulted]}, now=1000 + ALERTS.HOLD)
        self.assertIn(key, ALERTS._load()["active"])
        self.break_read(VOLUMES)
        unread = self.OV.get_overview()
        self.assertTrue(unread["volumes_unread"])
        facts = ALERTS.health_facts(unread)
        self.assertIn({"unknown_prefix": "health:Volume:"}, facts)
        fresh = []
        for step in range(1, 5):
            fresh += ALERTS.observe({"health": facts}, now=1000 + ALERTS.HOLD * (step + 1))
        self.assertIn(key, ALERTS._load()["active"])
        self.assertEqual([], [e for e in fresh if e["phase"] == "resolved" and e["key"].startswith(("health:Volume:", "disks:"))])

    def test_with_volumes_read_a_volume_alert_that_went_away_still_clears(self):
        key = "health:Volume:data"
        faulted = {"key": key, "category": "outage", "severity": "critical", "title": "Volume data is faulted",
                   "resolved": "Volume warning cleared: data", "body": "", "signals": {"condition": 1}, "href": "/volumes"}
        ALERTS.observe({"health": [faulted]}, now=1000)
        ALERTS.observe({"health": [faulted]}, now=1000 + ALERTS.HOLD)
        facts = ALERTS.health_facts(self.OV.get_overview())
        self.assertNotIn({"unknown_prefix": "health:Volume:"}, facts)
        fresh = ALERTS.observe({"health": facts}, now=2000) + ALERTS.observe({"health": facts}, now=2000 + ALERTS.HOLD)
        self.assertEqual(["resolved"], [e["phase"] for e in fresh if e["key"] == key])


class DisksTests(LonghornFixture):
    def disks_source(self):
        self.cluster.server.MODULE_ROUTES.forget("disks")
        return self.OV._alert_sources()["disks"]

    def test_a_failed_longhorn_node_read_raises_and_says_longhorn_could_not_be_read(self):
        self.break_read(LH_NODES)
        with self.assertRaisesRegex(RuntimeError, "Longhorn's disks could not be read"):
            self.cluster.server.DISKS.inventory()

    def test_a_cluster_without_longhorn_has_no_longhorn_disks(self):
        self.cluster.kube.absent_groups.add("longhorn.io")
        self.assertEqual({}, self.cluster.server.DISKS._lh_nodes())

    def test_open_disk_alerts_stay_open_when_longhorn_nodes_cannot_be_read(self):
        lh_disk_not_ready(self.cluster)
        facts = self.disks_source()
        self.assertEqual(["disks:node-a:d1"], [f["key"] for f in facts])
        ALERTS.observe({"disks": facts}, now=1000)
        ALERTS.observe({"disks": facts}, now=1000 + ALERTS.HOLD)
        self.break_read(LH_NODES)
        unread = self.disks_source()
        self.assertIsNone(unread)
        fresh = []
        for step in range(1, 5):
            fresh += ALERTS.observe({"disks": unread}, now=1000 + ALERTS.HOLD * (step + 1))
        self.assertIn("disks:node-a:d1", ALERTS._load()["active"])
        self.assertEqual([], [e for e in fresh if e["phase"] == "resolved" and e["key"].startswith(("health:Volume:", "disks:"))])


class ImpactTests(unittest.TestCase):
    NODES = [{"name": "node-a", "status": "Ready"}, {"name": "node-b", "status": "Ready"}]
    APP = {"name": "web", "ns": "lab", "nodes": ["node-a"], "desired": 1, "claims": ["data"]}

    def volume(self, copies):
        return {"name": "pvc-1", "pvc_name": "data", "namespace": "lab", "replicas": 2, "copies": copies}

    def preview(self, volumes):
        return IMPACT.preview("node-a", [self.APP], [], [], [], volumes, [], self.NODES)

    def test_known_copies_still_report_a_volume_at_risk(self):
        p = self.preview([self.volume([{"node": "node-a", "healthy": True}])])
        self.assertEqual(1, p["counts"]["at_risk"])
        self.assertEqual("stops", p["apps"][0]["outcome"])

    def test_unread_copies_make_the_risk_unknown_never_zero(self):
        p = self.preview([self.volume(None)])
        self.assertIsNone(p["counts"]["at_risk"])
        self.assertIsNone(p["counts"]["fewer_copies"])
        self.assertIn("Longhorn's volume copies", p["unread"])
        self.assertEqual("unknown", p["apps"][0]["outcome"])

    def test_unread_volumes_make_the_risk_unknown_never_zero(self):
        p = self.preview(None)
        self.assertIsNone(p["counts"]["at_risk"])
        self.assertIn("Longhorn's volume copies", p["unread"])
        self.assertEqual("unknown", p["apps"][0]["outcome"])


class NodeImpactTests(LonghornFixture):
    def test_a_host_preview_with_volumes_unread_says_the_risk_is_unknown(self):
        self.break_read(VOLUMES)
        p = self.cluster.server.NODES.node_impact("node-a")
        self.assertIsNone(p["counts"]["at_risk"])
        self.assertIn("Longhorn's volume copies", p["unread"])

    def test_a_host_preview_with_replicas_unread_says_the_risk_is_unknown(self):
        self.break_read(REPLICAS)
        p = self.cluster.server.NODES.node_impact("node-a")
        self.assertIsNone(p["counts"]["at_risk"])


if __name__ == "__main__":
    unittest.main()
