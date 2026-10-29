"""#841: on a one-host cluster, the warnings about app volumes say why.

A volume that asks for more copies than the one host can hold reads
degraded. The banner, the alert, MQTT and the installer's check used to say
"Longhorn is rebuilding or missing a replica", which sends a person waiting
for a rebuild that never comes. They now say the cluster has one host, and
point to Match copies to hosts in Cluster Health."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
import homestead_alerts as ALERTS
import homestead_cluster as CLUSTER
import homestead_mqtt as MQTT
import homestead_network_server as NET

UNPLACEABLE = {"type": "Scheduled", "status": "False", "reason": "ReplicaSchedulingFailure", "message": "replica scheduling failed"}


def row(name="pvc-a", claim="photos", replicas=3, robustness="degraded", conditions=(UNPLACEABLE,), rebuild=None):
    return {"name": name, "pvc_name": claim, "robustness": robustness, "replicas": replicas,
            "conditions": list(conditions), "rebuild": rebuild}


def nodes(count):
    return [{"name": f"node-{i}", "status": "Ready"} for i in range(1, count + 1)]


class ReasonTests(unittest.TestCase):
    """The warning says "one host" only for a volume the offer lists
    (homestead_app_copies), so the two can never disagree."""
    def issue(self, volume, offered=frozenset({"pvc-a"})):
        issues = CLUSTER.classify_cluster_health(nodes(1), [], [volume], short_of_hosts=offered)["health_issues"]
        self.assertEqual(1, len(issues))
        return issues[0]

    def test_one_host_says_why_and_points_to_the_offer(self):
        issue = self.issue(row())
        self.assertEqual("degraded", issue["severity"])
        self.assertTrue(issue["one_host"])
        self.assertIn("one host", issue["reason"])
        self.assertIn("3 copies", issue["reason"])
        self.assertIn("Match copies to hosts", issue["reason"])
        self.assertIn("Cluster Health", issue["reason"])

    def test_a_volume_the_offer_does_not_list_reads_as_before(self):
        for volume, offered in ((row(), frozenset()), (row("pvc-b"), frozenset({"pvc-a"}))):
            with self.subTest(volume=volume["name"], offered=offered):
                issue = self.issue(volume, offered)
                self.assertNotIn("one_host", issue)
                self.assertEqual("Longhorn is rebuilding or missing a replica", issue["reason"])

    def test_without_the_offer_nothing_is_said(self):
        issue = CLUSTER.classify_cluster_health(nodes(1), [], [row()])["health_issues"][0]
        self.assertNotIn("one_host", issue)


class AlertTests(unittest.TestCase):
    def test_the_alert_carries_the_reason_and_opens_cluster_health(self):
        issues = CLUSTER.classify_cluster_health(nodes(1), [], [row()], short_of_hosts={"pvc-a"})["health_issues"]
        fact = next(f for f in ALERTS.health_facts({"health_issues": issues}) if f.get("key"))
        self.assertIn("one host", fact["body"])
        self.assertEqual("/system/cluster?section=health", fact["href"])

    def test_other_volume_alerts_still_open_volumes(self):
        issues = CLUSTER.classify_cluster_health(nodes(2), [], [row()])["health_issues"]
        fact = next(f for f in ALERTS.health_facts({"health_issues": issues}) if f.get("key"))
        self.assertEqual("/volumes", fact["href"])


class MqttTests(unittest.TestCase):
    def setUp(self):
        self.saved = {name: getattr(NET, name, None) for name in ("cached", "kget", "get_overview", "SYS_NS")}

    def tearDown(self):
        for name, value in self.saved.items():
            setattr(NET, name, value)

    def test_a_sensor_counts_the_volumes_short_of_hosts(self):
        self.assertIn("vol_one_host", [key for key, *_ in MQTT.CLUSTER_SENSORS])
        overview = {"nodes": [], "nodes_ready": 1, "nodes_total": 1, "volumes": 3, "vol_degraded": 2,
                    "vol_faulted": 0, "vol_one_host": 2, "cpu_pct": 1, "mem_pct": 1}
        NET.cached = lambda key, ttl, fn: overview
        NET.kget = lambda path: {"items": []}
        NET.SYS_NS = set()
        self.assertEqual(2, NET.mqtt_snapshot()["cluster"]["vol_one_host"])


class OverviewTests(unittest.TestCase):
    NODE = {"status": "Ready", "cpu_cap": 4, "cpu_used": 1, "mem_cap_gb": 8, "mem_used_gb": 2}

    def overview(self, facts, hosts=1):
        import homestead_overview_server as OV
        asked = []

        def offer():
            asked.append(1)
            if isinstance(facts, Exception):
                raise facts
            return facts
        saved = {name: getattr(OV, name, None) for name in ("cached", "NETWORK")}
        saved_offer = OV.APP_COPIES.cached_facts
        try:
            OV.cached = lambda key, ttl, fn: []
            OV.NETWORK = type("N", (), {"shared_vip": staticmethod(lambda: "")})
            OV.APP_COPIES.cached_facts = offer
            out = OV.get_overview(nodes=[dict(self.NODE, name=f"node-{i}") for i in range(hosts)], wl=[],
                                  vols=[row(), row("pvc-b", claim="notes")], pods={"items": []})
        finally:
            for name, value in saved.items():
                setattr(OV, name, value)
            OV.APP_COPIES.cached_facts = saved_offer
        return out, asked

    def test_the_overview_counts_the_volumes_the_offer_lists(self):
        out, _ = self.overview({"state": "lower", "hosts": 1, "volumes": [{"volume": "pvc-a"}]})
        self.assertEqual(1, out["vol_one_host"])
        reasons = {i["name"]: i["reason"] for i in out["health_issues"]}
        self.assertIn("one host", reasons["photos"])
        self.assertNotIn("one host", reasons["notes"])

    def test_no_offer_harvester_or_a_failed_read_says_nothing_new(self):
        for facts in ({"state": "n/a", "hosts": 0, "volumes": []}, {"state": "unknown", "hosts": 0, "volumes": [], "error": "x"},
                      RuntimeError("unreadable")):
            with self.subTest(facts=facts):
                out, _ = self.overview(facts)
                self.assertEqual(0, out["vol_one_host"])
                self.assertFalse([i for i in out["health_issues"] if i.get("one_host")])

    def test_a_bigger_cluster_does_not_read_the_offer(self):
        out, asked = self.overview({"state": "lower", "hosts": 1, "volumes": [{"volume": "pvc-a"}]}, hosts=2)
        self.assertEqual([], asked)
        self.assertEqual(0, out["vol_one_host"])


if __name__ == "__main__":
    unittest.main()
