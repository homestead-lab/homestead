"""The cluster health verdict turned amber or red for routine things (#941):
a restart in an old pod, a Longhorn rebuild, a host in planned maintenance,
and a 30 s blip. Alerts already wait for all of them; the verdict now does too."""
import sys
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
import server
import homestead_alerts as ALERTS
import homestead_cluster as CLUSTER

DAY = 86400


def stamp(seconds_ago):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - seconds_ago))


def classify(*args, **kwargs):
    return CLUSTER.classify_cluster_health(*args, **kwargs)


def node(name="node-1", status="Ready"):
    return {"name": name, "status": status}


class RestartInAnOldPodTests(unittest.TestCase):
    """S1: a pod's not-ready time counts from its Ready condition's last
    change, not from when the pod was made."""

    def workload(self, not_ready_for, pod_age=3 * DAY):
        dep = {"metadata": {"namespace": "lab", "name": "web", "annotations": {}, "labels": {},
                            "creationTimestamp": stamp(10 * DAY), "generation": 1},
               "spec": {"replicas": 1, "selector": {"matchLabels": {"app": "web"}},
                        "template": {"metadata": {"labels": {"app": "web"}},
                                     "spec": {"containers": [{"name": "web", "image": "example.com/web:1"}]}}},
               "status": {"replicas": 1, "updatedReplicas": 1, "readyReplicas": 0, "availableReplicas": 0,
                          "unavailableReplicas": 1, "observedGeneration": 1}}
        pod = {"metadata": {"namespace": "lab", "name": "web-5d-abcde", "labels": {"app": "web"},
                            "creationTimestamp": stamp(pod_age)},
               "spec": {"nodeName": "node-1", "containers": [{"name": "web"}]},
               "status": {"phase": "Running", "startTime": stamp(pod_age),
                          "conditions": [{"type": "PodScheduled", "status": "True"},
                                         {"type": "Ready", "status": "False",
                                          "lastTransitionTime": stamp(not_ready_for)}],
                          "containerStatuses": [{"name": "web", "restartCount": 4, "ready": False,
                                                 "state": {"running": {"startedAt": stamp(not_ready_for)}}}]}}
        items = {"/apis/apps/v1/deployments": {"items": [dep]}, "/api/v1/pods": {"items": [pod]}}
        with mock.patch.object(server.KUBE_HELPERS, "kget",
                               side_effect=lambda path, **kw: items.get(path, {"items": []})):
            return server.WORKLOADS.get_workloads()[0]

    def test_a_restart_in_a_days_old_pod_is_within_its_grace(self):
        workload = self.workload(not_ready_for=40)
        self.assertLess(workload["transition_age"], 300)
        result = classify([node()], [workload], [])
        self.assertEqual("healthy", result["health"])
        self.assertEqual("starting", result["health_state"])
        self.assertFalse(result["health_issues"])

    def test_a_long_not_ready_says_how_long_it_has_been_not_ready(self):
        workload = self.workload(not_ready_for=10 * 60)
        result = classify([node()], [workload], [])
        self.assertEqual("degraded", result["health"])
        reason = result["health_issues"][0]["reason"]
        self.assertIn("not ready for 10m", reason)
        self.assertNotIn("4320m", reason)

    def test_a_pod_with_no_ready_condition_yet_counts_from_its_creation(self):
        workload = self.workload(not_ready_for=0, pod_age=20)
        self.assertLess(workload["transition_age"], 300)


class RebuildTests(unittest.TestCase):
    """S2: a degraded volume rebuilding a copy with no error is repairing, not
    a fault, for REPAIR_GRACE; then it is degraded again."""
    NOW = 1_800_000_000

    def volume(self, degraded_for, **changes):
        value = {"name": "pvc-a", "pvc_name": "data", "robustness": "degraded",
                 "degraded_since": self.NOW - degraded_for, "rebuild": {"pct": 40, "replicas": 1, "error": ""}}
        value.update(changes)
        return value

    def test_the_grace_is_thirty_minutes(self):
        self.assertEqual(30 * 60, CLUSTER.REPAIR_GRACE)

    def test_a_rebuild_in_progress_is_repairing(self):
        result = classify([node()], [], [self.volume(5 * 60)], now=self.NOW)
        self.assertEqual("healthy", result["health"])
        self.assertEqual("repairing", result["health_state"])
        self.assertFalse(result["health_issues"])
        self.assertIn("40%", result["health_summary"])

    def test_a_rebuild_past_the_grace_is_degraded(self):
        result = classify([node()], [], [self.volume(CLUSTER.REPAIR_GRACE + 1)], now=self.NOW)
        self.assertEqual("degraded", result["health_state"])
        self.assertIn("after 30m", result["health_issues"][0]["reason"])
        self.assertEqual("repairing", classify([node()], [], [self.volume(CLUSTER.REPAIR_GRACE)],
                                               now=self.NOW)["health_state"])

    def test_a_rebuild_with_an_error_is_degraded_at_once(self):
        volume = self.volume(60, rebuild={"pct": 10, "replicas": 1, "error": "replica disk full"})
        result = classify([node()], [], [volume], now=self.NOW)
        self.assertEqual("degraded", result["health_state"])

    def test_degraded_with_no_rebuild_or_no_clock_stays_degraded(self):
        for volume in (self.volume(60, rebuild=None), self.volume(60, degraded_since=None)):
            self.assertEqual("degraded", classify([node()], [], [volume], now=self.NOW)["health_state"])


class MaintenanceTests(unittest.TestCase):
    """S3: a host Homestead put in maintenance is an activity, as alerts leave
    it out (#535); one whose maintenance overran its limit is not."""

    def test_a_host_in_maintenance_is_an_activity(self):
        result = classify([node("node-1", "NotReady"), node("node-2")], [], [], maintenance={"node-1"})
        self.assertEqual("healthy", result["health"])
        self.assertEqual("maintenance", result["health_state"])
        self.assertFalse(result["health_issues"])
        self.assertEqual([("maintenance", "node-1")],
                         [(a["state"], a["name"]) for a in result["activities"]])

    def test_another_host_not_ready_is_still_critical(self):
        result = classify([node("node-1", "NotReady"), node("node-2", "NotReady")], [], [],
                          maintenance={"node-1"})
        self.assertEqual("critical", result["health_state"])
        self.assertEqual(["node-2"], [i["name"] for i in result["health_issues"]])

    def test_an_overran_host_is_urgent(self):
        result = classify([node("node-1", "NotReady")], [], [], overran={"node-1"})
        self.assertTrue(result["health_issues"][0].get("urgent"))

    def test_a_ready_host_in_maintenance_still_says_so(self):
        result = classify([node("node-1")], [], [], maintenance={"node-1"})
        self.assertEqual("maintenance", result["health_state"])

    def test_the_overview_reads_homesteads_maintenance(self):
        import homestead_overview_server as OV

        class Seen(Exception):
            pass

        class Hold:
            # The overview's verdict as it goes into the hold; the rest of
            # the overview (addresses, totals) is not what this is about.
            def apply(self, report, now=None):
                raise Seen(report)

        with mock.patch.object(OV.HISTORY, "maintenance_view", return_value=({"node-1"}, set())), \
                mock.patch.object(CLUSTER, "HOLD", Hold()), \
                self.assertRaises(Seen) as seen:
            OV.get_overview(nodes=[{**node("node-1", "NotReady"), "cpu_cap": 4, "cpu_used": 1,
                                    "mem_cap_gb": 8, "mem_used_gb": 2}],
                            wl=[], vols=[], pods={"items": []})
        self.assertEqual("maintenance", seen.exception.args[0]["health_state"])


class HoldTests(unittest.TestCase):
    """S4: an issue turns the verdict amber or red only once it has lasted
    ALERTS.HOLD (60 s), and clears HOLD after it is gone, as alerts do."""
    VOLUME = {"name": "pvc-a", "pvc_name": "data", "robustness": "faulted"}
    NODE_DOWN = node("node-1", "NotReady")

    def setUp(self):
        self.hold = CLUSTER.HealthHold()

    def cycle(self, at, *, volume=False, node_down=False, overran=()):
        report = classify([self.NODE_DOWN if node_down else node()], [], [self.VOLUME] if volume else [],
                          overran=overran)
        return self.hold.apply(report, now=at)

    def test_the_hold_is_the_alerts_hold(self):
        self.assertEqual(ALERTS.HOLD, self.hold.hold)
        self.assertEqual(60, ALERTS.HOLD)

    def test_a_30_second_blip_never_turns_the_verdict(self):
        states = [self.cycle(0, volume=True), self.cycle(20, volume=True),
                  self.cycle(40), self.cycle(60), self.cycle(80)]
        self.assertEqual(["healthy"] * 5, [s["health_state"] for s in states])
        self.assertTrue(all(not s["health_issues"] for s in states))
        # What alerts read is this cycle's, so their own hold is not doubled.
        self.assertEqual(1, len(states[0]["current_issues"]))

    def test_a_lasting_fault_shows_after_60_seconds_and_clears_60_seconds_after(self):
        held = [self.cycle(t, volume=True)["health_state"] for t in (0, 20, 40, 60, 80)]
        self.assertEqual(["healthy", "healthy", "healthy", "critical", "critical"], held)
        after = [self.cycle(t) for t in (100, 140, 159, 160)]
        self.assertEqual(["critical", "critical", "critical", "healthy"], [s["health_state"] for s in after])
        self.assertEqual([], after[0]["current_issues"])

    def test_a_fault_that_comes_back_before_it_is_counted_starts_over(self):
        self.cycle(0, volume=True)
        self.cycle(40)
        self.assertEqual("healthy", self.cycle(60, volume=True)["health_state"])
        self.assertEqual("critical", self.cycle(120, volume=True)["health_state"])

    def test_a_host_not_ready_is_held_as_alerts_hold_it(self):
        self.assertEqual("healthy", self.cycle(0, node_down=True)["health_state"])
        self.assertEqual("critical", self.cycle(60, node_down=True)["health_state"])

    def test_an_overran_maintenance_is_shown_at_once(self):
        self.assertEqual("critical", self.cycle(0, node_down=True, overran={"node-1"})["health_state"])

    def test_an_activity_still_shows_while_an_issue_is_held(self):
        report = classify([node()], [{"ns": "lab", "name": "web", "desired": 1, "ready": 0, "available": 0,
                                      "updated": 1, "generation": 1, "observed_generation": 1,
                                      "transition_age": 10, "problems": []}], [self.VOLUME])
        held = self.hold.apply(report, now=0)
        self.assertEqual("starting", held["health_state"])
        self.assertIn("Workload lab/web", held["health_summary"])

    def test_alerts_read_this_cycles_issues(self):
        issue = {"severity": "critical", "kind": "Volume", "name": "data", "reason": "faulted"}
        facts = ALERTS.health_facts({"health_issues": [], "current_issues": [issue]})
        self.assertEqual(["health:Volume:data"], [f["key"] for f in facts if "key" in f])

    def test_a_repairing_volume_rings_no_bell(self):
        """S2 changes the issues themselves, so the bell follows the pill."""
        volume = {"name": "pvc-a", "pvc_name": "data", "robustness": "degraded", "degraded_since": 0,
                  "rebuild": {"pct": 40, "replicas": 1, "error": ""}}
        report = self.hold.apply(classify([node()], [], [volume], now=300), now=300)
        self.assertEqual([], [f for f in ALERTS.health_facts(report) if "key" in f])

    def test_a_counted_fault_that_flickers_stays_counted(self):
        for at in (0, 20, 40, 60):
            self.cycle(at, volume=True)
        self.cycle(80)
        self.assertEqual("critical", self.cycle(100, volume=True)["health_state"])
        self.assertEqual("critical", self.cycle(150)["health_state"])

    def test_a_worse_severity_on_a_counted_issue_shows_at_once(self):
        disk = lambda severity: node() | {"disk_issues": [{"disk": "sda", "severity": severity, "reason": "SMART"}]}
        for at in (0, 60):
            state = self.hold.apply(classify([disk("degraded")], [], []), now=at)["health_state"]
        self.assertEqual("degraded", state)
        self.assertEqual("critical", self.hold.apply(classify([disk("critical")], [], []), now=80)["health_state"])

    def test_a_row_not_looked_at_for_longer_than_the_hold_starts_over(self):
        """A replica that builds the overview rarely: a blip it saw an hour
        ago and one it sees now are not one fault."""
        self.cycle(0, volume=True)
        self.assertEqual("healthy", self.cycle(3600, volume=True)["health_state"])

    def test_a_fault_with_its_own_clock_is_not_held_again(self):
        """A workload past its grace, or a rebuild past 30 min, has been a
        fault for that long already: a rarely-run overview, or a new
        leader, counts it at once."""
        stalled = {"ns": "lab", "name": "web", "desired": 1, "ready": 0, "available": 0, "updated": 1,
                   "generation": 1, "observed_generation": 1, "transition_age": 300 + 90, "problems": []}
        self.assertEqual("degraded", self.hold.apply(classify([node()], [stalled], []), now=0)["health_state"])
        just = dict(stalled, name="api", transition_age=300 + 20)
        self.assertEqual("degraded", self.hold.apply(classify([node()], [just], []), now=0)["health_state"])
        fresh = CLUSTER.HealthHold()
        self.assertEqual("healthy", fresh.apply(classify([node()], [just], []), now=0)["health_state"])
        self.assertEqual("degraded", fresh.apply(classify([node()], [dict(just, transition_age=360)], []),
                                                 now=40)["health_state"])
        volume = {"name": "pvc-a", "pvc_name": "data", "robustness": "degraded", "degraded_since": 0,
                  "rebuild": {"pct": 40, "replicas": 1, "error": ""}}
        late = CLUSTER.REPAIR_GRACE + 120
        self.assertEqual("degraded", CLUSTER.HealthHold().apply(classify([node()], [], [volume], now=late),
                                                                 now=late)["health_state"])


class CrashLoopTests(unittest.TestCase):
    """S1 must not hide a crash loop: its back-off is a problem, which skips
    the start-up grace whatever the Ready clock says."""

    def test_a_crash_loop_in_a_fresh_ready_transition_is_still_degraded(self):
        workload = {"ns": "lab", "name": "web", "desired": 1, "ready": 0, "available": 0, "updated": 1,
                    "generation": 1, "observed_generation": 1, "transition_age": 5,
                    "problems": ["web-5d-abcde: CrashLoopBackOff"]}
        result = classify([node()], [workload], [])
        self.assertEqual("degraded", result["health_state"])
        self.assertIn("CrashLoopBackOff", result["health_summary"])


class SetupHealthStepTests(unittest.TestCase):
    def test_the_setup_guide_reads_this_cycles_issues(self):
        """The setup guide's health step is a checklist, not the pill: a
        fault the verdict still holds is not "done"."""
        import homestead_setup as SETUP
        source = Path(SETUP.__file__).read_text(encoding="utf-8")
        self.assertIn('ov.get("current_issues"', source)


if __name__ == "__main__":
    unittest.main()
