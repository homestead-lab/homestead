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
import homestead_cluster as CLUSTER
import homestead_impact as IMPACT
import homestead_kube as K
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
        # Other test files bind homestead_disks to fakes of their own and leave
        # it so (as Cluster says of homestead_lhcapacity): read through this one.
        DISKS = self.cluster.server.DISKS
        for name, value in (("kget", K.kget), ("ksend", K.ksend), ("temps", lambda: {})):
            self.enterContext(mock.patch.object(DISKS, name, value))
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

    def no_workloads(self):
        """The fake's one workload is past its start-up grace, which makes the
        verdict degraded: with none, and a hold that has seen no earlier test's
        issues, it is healthy."""
        self.enterContext(mock.patch.object(self.OV.WORKLOADS, "get_workloads", lambda **_: []))
        self.enterContext(mock.patch.object(self.OV.CLUSTER, "HOLD", CLUSTER.HealthHold()))

    def test_a_normal_overview_has_its_volumes_read(self):
        self.no_workloads()
        overview = self.OV.get_overview()
        self.assertFalse(overview["volumes_unread"])
        self.assertEqual("healthy", overview["health_state"])

    def test_the_dashboard_and_every_page_s_pill_say_unknown_with_the_reason_while_volumes_are_unread(self):
        # The verdict is set where the overview is built, so the pill that
        # every page paints from /api/alerts (#949) says it too, not only the
        # Dashboard.
        self.no_workloads()
        self.break_read(VOLUMES)
        self.PAGES.cycle(self.OV.page_inputs(), self.OV.page_builders())
        overview = self.PAGES.read("ov")[1]
        self.assertEqual("unknown", overview["health_state"])
        self.assertRegex(overview["health_summary"], r"^Longhorn's volumes could not be read, so their health is not known\. ")
        brief = CLUSTER.health_brief()
        self.assertEqual("unknown", brief["state"])
        self.assertIn("Longhorn's volumes could not be read", brief["summary"])

    def test_known_bad_news_and_activities_stay_while_a_part_is_unread(self):
        unread = ["Longhorn's volumes could not be read, so their health is not known"]
        for state in ("critical", "degraded", "updating"):
            report = {"health": "degraded", "health_state": state, "health_summary": "Node a: down"}
            self.assertEqual(report, CLUSTER.unknown_when_unread(report, unread))
        healthy = {"health": "healthy", "health_state": "healthy", "health_summary": "All good"}
        self.assertEqual(healthy, CLUSTER.unknown_when_unread(healthy, []))
        self.assertEqual({**healthy, "health_state": "unknown", "health_summary": f"{unread[0]}. All good"},
                         CLUSTER.unknown_when_unread(healthy, unread))

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


class ElsewhereTests(LonghornFixture):
    """What else reads the overview's volume counts, and the other callers
    of the Longhorn node read."""

    def test_a_non_volume_health_alert_still_clears_while_volumes_are_unread(self):
        key = "health:Workload:lab/gone"
        gone = {"key": key, "category": "degraded", "severity": "degraded", "title": "gone is not ready",
                "resolved": "Workload readiness warning cleared: gone", "body": "", "signals": {"condition": 1},
                "href": "/containers"}
        ALERTS.observe({"health": [gone]}, now=1000)
        ALERTS.observe({"health": [gone]}, now=1000 + ALERTS.HOLD)
        self.break_read(VOLUMES)
        facts = ALERTS.health_facts(self.OV.get_overview())
        fresh = ALERTS.observe({"health": facts}, now=2000) + ALERTS.observe({"health": facts}, now=2000 + ALERTS.HOLD)
        self.assertEqual(["resolved"], [e["phase"] for e in fresh if e["key"] == key])

    def mqtt(self):
        return self.cluster.server.NETWORK_ROUTES.mqtt_snapshot()["cluster"]

    def test_mqtt_publishes_no_volume_counts_and_an_unknown_health_while_volumes_are_unread(self):
        self.assertEqual(("healthy", 1, 0), (self.mqtt()["health"], self.mqtt()["vol_total"], self.mqtt()["vol_faulted"]))
        self.cluster.server.MODULE_ROUTES.forget("ov")
        self.break_read(VOLUMES)
        cluster = self.mqtt()
        self.assertEqual("unknown", cluster["health"])
        for field in ("vol_total", "vol_degraded", "vol_faulted"):
            self.assertIsNone(cluster[field], field)

    def test_a_host_not_ready_is_still_critical_while_volumes_are_unread(self):
        node = self.cluster.kube.object("/api/v1/nodes/node-b")
        node["status"]["conditions"] = [{"type": "Ready", "status": "False"}]
        self.cluster.kube.add(node)
        self.break_read(VOLUMES)
        self.assertEqual("critical", self.mqtt()["health"])

    def test_history_records_no_volume_count_for_a_cycle_that_did_not_read_them(self):
        HISTORY = self.cluster.server.HISTORY
        self.assertIsNone(HISTORY.sample_from({"volumes_unread": True, "vol_degraded": 0, "vol_faulted": 0})["vol_bad"])
        self.assertEqual(2, HISTORY.sample_from({"vol_degraded": 1, "vol_faulted": 1})["vol_bad"])
        hour = HISTORY._rollup([{"vol_bad": None}, {"vol_bad": 2}, {"vol_bad": 4}], 0)
        self.assertEqual((3, 4), (hour["vol_bad"], hour["vol_bad_max"]))
        self.assertEqual((None, None), (lambda h: (h["vol_bad"], h["vol_bad_max"]))(HISTORY._rollup([{"vol_bad": None}], 0)))

    def test_a_tagged_storage_class_says_longhorn_could_not_be_read_not_that_no_node_has_the_tag(self):
        self.break_read(LH_NODES)
        result = self.cluster.server.STORAGE_CLASSES.create_storage_class({"name": "tagged", "disk_tags": ["ssd"]})
        self.assertIn("could not be read from Longhorn", str(result))
        self.assertNotIn("no node has", str(result))

    def test_retiring_a_disk_is_refused_when_longhorn_s_nodes_cannot_be_read(self):
        lh_disk_not_ready(self.cluster)
        self.break_read(LH_NODES)
        with self.assertRaisesRegex(RuntimeError, "Longhorn's disks could not be read"):
            self.cluster.server.DISKS.retire_plan("node-a", "d1")

    def test_disk_auto_tagging_that_fails_does_not_stop_the_host_fixes_after_it(self):
        STORAGE_ROUTES = self.cluster.server.STORAGE_ROUTES
        with mock.patch.object(STORAGE_ROUTES.DISKS, "auto_tag", side_effect=RuntimeError("Longhorn's disks could not be read")), \
                mock.patch("builtins.print") as printed:
            STORAGE_ROUTES._auto_tag()
        self.assertIn("disks not tagged", str(printed.call_args))


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


class NotAnsweringTests(LonghornFixture):
    """While Longhorn cannot be read its alerts are held. After ten minutes
    an alert says Longhorn is not answering; after an hour the held alerts
    say they are the last known state, not the current one."""
    KEY = "disks:node-a:d1"

    def sources(self):
        self.cluster.server.MODULE_ROUTES.forget("ov")
        self.cluster.server.MODULE_ROUTES.forget("disks")
        found = self.OV._alert_sources()
        return {name: found[name] for name in ("health", "disks", "longhorn")}

    def mend(self):
        self.cluster.kube._failures.clear()

    def longhorn(self, fresh):
        return [(e["phase"], e["title"]) for e in fresh if e["key"] == "longhorn:unread"]

    def row(self, at):
        return next(r for r in ALERTS.active(now=at) if r["key"] == self.KEY)

    def open_disk_alert(self):
        lh_disk_not_ready(self.cluster)
        read = self.sources()
        ALERTS.observe(read, now=1000)
        ALERTS.observe(read, now=1000 + ALERTS.HOLD)
        self.assertIn(self.KEY, ALERTS._load()["active"])
        return read

    def test_longhorn_not_answering_is_raised_after_ten_minutes_not_before(self):
        self.assertEqual(600, ALERTS.LONGHORN_QUIET)
        self.break_read(VOLUMES)
        unread = self.sources()
        self.assertEqual(["longhorn:unread"], [f["key"] for f in unread["longhorn"]])
        fresh = []
        for at in (1000, 1000 + ALERTS.HOLD, 1000 + ALERTS.LONGHORN_QUIET - 1):
            fresh += ALERTS.observe(unread, now=at)
        self.assertEqual([], self.longhorn(fresh))
        self.assertEqual([("raised", "Longhorn is not answering")],
                         self.longhorn(ALERTS.observe(unread, now=1000 + ALERTS.LONGHORN_QUIET)))

    def test_longhorn_answering_again_clears_the_alert(self):
        self.break_read(VOLUMES)
        unread = self.sources()
        ALERTS.observe(unread, now=1000)
        ALERTS.observe(unread, now=1000 + ALERTS.LONGHORN_QUIET)
        self.mend()
        read = self.sources()
        self.assertEqual([], read["longhorn"])
        fresh = ALERTS.observe(read, now=2000) + ALERTS.observe(read, now=2000 + ALERTS.HOLD)
        self.assertEqual([("resolved", "Longhorn is answering again")], self.longhorn(fresh))

    def test_a_cluster_without_longhorn_has_no_longhorn_alert(self):
        self.cluster.kube.absent_groups.add("longhorn.io")
        self.assertEqual([], self.sources()["longhorn"])

    def test_held_alerts_say_last_known_after_an_hour_and_stay_open(self):
        self.assertEqual(3600, ALERTS.STALE)
        self.open_disk_alert()
        body = ALERTS._load()["active"][self.KEY]["body"]
        self.break_read(VOLUMES)
        self.break_read(LH_NODES)
        unread = self.sources()
        self.assertIsNone(unread["disks"])
        start = 2000
        ALERTS.observe(unread, now=start)
        ALERTS.observe(unread, now=start + ALERTS.STALE - 1)
        early = self.row(start + ALERTS.STALE - 1)
        self.assertEqual((body, False), (early["body"], early["stale"]))
        fresh = ALERTS.observe(unread, now=start + ALERTS.STALE)
        held = self.row(start + ALERTS.STALE)
        self.assertTrue(held["stale"])
        self.assertTrue(held["body"].startswith("Last known, not current:"), held["body"])
        self.assertTrue(held["body"].endswith(body), held["body"])
        self.assertEqual([], [e for e in fresh if e["phase"] == "resolved"])

    def test_the_last_known_mark_goes_once_the_alert_is_read_again(self):
        read = self.open_disk_alert()
        ALERTS.observe({"disks": None}, now=2000)
        ALERTS.observe({"disks": None}, now=2000 + ALERTS.STALE)
        self.assertTrue(self.row(2000 + ALERTS.STALE)["stale"])
        ALERTS.observe({"disks": read["disks"]}, now=2000 + ALERTS.STALE + 1)
        row = self.row(9000 + ALERTS.STALE)
        self.assertFalse(row["stale"])
        self.assertFalse(row["body"].startswith("Last known"))


if __name__ == "__main__":
    unittest.main()
