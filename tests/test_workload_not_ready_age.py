"""An app moved by Balance hosts showed "not ready for" its whole age while
its new pod started. A move switches the Deployment to Recreate, so for a few
seconds it has no pod at all; with no pod to time, the not-ready age fell back
to the Deployment's creationTimestamp, days ago, and Cluster Health raised it
as degraded instead of starting.

With no pod, the age is the youngest of the rollout annotations (a move's
movedAt among them) and the time the Deployment's Available condition turned
False; only with neither, its creationTimestamp. The stamps are written in
UTC, as their "Z" says, whatever the server's time zone."""
import calendar
import os
import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import server
import homestead_cluster as CLUSTER
import homestead_names as NAMES
from fakes.kube import Kube

DAY = 86400


def stamp(seconds_ago):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - seconds_ago))


def deployment(created_ago=25 * DAY, template_annotations=None, conditions=None):
    return {"apiVersion": "apps/v1", "kind": "Deployment",
            "metadata": {"namespace": "lab", "name": "web", "annotations": {}, "labels": {},
                         "creationTimestamp": stamp(created_ago), "generation": 2},
            "spec": {"replicas": 1, "strategy": {"type": "Recreate"},
                     "selector": {"matchLabels": {"app": "web"}},
                     "template": {"metadata": {"labels": {"app": "web"},
                                               "annotations": dict(template_annotations or {})},
                                  "spec": {"containers": [{"name": "web", "image": "example.com/web:1"}]}}},
            "status": {"replicas": 0, "updatedReplicas": 0, "readyReplicas": 0, "availableReplicas": 0,
                       "unavailableReplicas": 1, "observedGeneration": 2,
                       "conditions": list(conditions or [])}}


def condition(kind, status, since, reason=""):
    return {"type": kind, "status": status, "reason": reason,
            "lastTransitionTime": stamp(since), "lastUpdateTime": stamp(since)}


def row(dep):
    """The Apps-list row for one Deployment that has no pod at all."""
    with Kube(dep):
        rows = server.WORKLOADS.get_workloads()
    return next(w for w in rows if (w["ns"], w["name"]) == ("lab", "web"))


def verdict(workload):
    return CLUSTER.classify_cluster_health([{"name": "node-a", "status": "Ready"}], [workload], [])


class NoPodWhileAMoveRecreatesTests(unittest.TestCase):

    def test_a_move_twenty_seconds_ago_is_starting_not_degraded(self):
        dep = deployment(template_annotations={NAMES.key("movedAt"): stamp(20)},
                         # Available flipped False when the old pod went: also recent.
                         conditions=[condition("Available", "False", 20, "MinimumReplicasUnavailable"),
                                     condition("Progressing", "True", 20 * DAY, "ReplicaSetUpdated")])
        workload = row(dep)
        self.assertEqual([], workload["pods"])
        self.assertAlmostEqual(20, workload["transition_age"], delta=10)
        result = verdict(workload)
        self.assertEqual("healthy", result["health"])
        self.assertEqual("starting", result["health_state"])
        self.assertFalse(result["health_issues"])

    def test_a_fresh_move_beats_an_old_restart_stamp(self):
        workload = row(deployment(template_annotations={NAMES.key("restartedAt"): stamp(20 * DAY),
                                                        NAMES.key("movedAt"): stamp(20)}))
        self.assertAlmostEqual(20, workload["transition_age"], delta=10)
        self.assertEqual("starting", verdict(workload)["health_state"])

    def test_movedat_alone_counts_even_with_no_conditions(self):
        workload = row(deployment(template_annotations={NAMES.key("movedAt"): stamp(20)}))
        self.assertAlmostEqual(20, workload["transition_age"], delta=10)
        self.assertEqual("starting", verdict(workload)["health_state"])


class AvailableConditionTests(unittest.TestCase):

    def unavailable_for(self, seconds):
        return row(deployment(conditions=[
            condition("Available", "False", seconds, "MinimumReplicasUnavailable"),
            # Progressing stays True through a rollout: its time is weeks old.
            condition("Progressing", "True", 20 * DAY, "NewReplicaSetAvailable")]))

    def test_unavailable_for_five_minutes_is_five_minutes_old(self):
        self.assertAlmostEqual(5 * 60, self.unavailable_for(5 * 60)["transition_age"], delta=10)

    def test_past_the_grace_it_says_how_long_it_has_been_unavailable(self):
        result = verdict(self.unavailable_for(6 * 60))
        self.assertEqual("degraded", result["health"])
        reason = result["health_issues"][0]["reason"]
        self.assertIn("not ready for 6m", reason)
        self.assertNotIn("36000m", reason)

    def test_an_available_condition_that_is_true_is_not_a_not_ready_time(self):
        dep = deployment(conditions=[condition("Available", "True", 60)])
        self.assertGreater(row(dep)["transition_age"], 24 * DAY)


class OldStampTests(unittest.TestCase):

    def test_an_old_stamp_does_not_hide_a_fresh_unavailable(self):
        # An edit or image change (no stamp of its own) after a restart 20 days ago.
        dep = deployment(template_annotations={NAMES.key("restartedAt"): stamp(20 * DAY)},
                         conditions=[condition("Available", "False", 30, "MinimumReplicasUnavailable")])
        workload = row(dep)
        self.assertAlmostEqual(30, workload["transition_age"], delta=10)
        self.assertEqual("starting", verdict(workload)["health_state"])

    def test_a_fresh_stamp_beats_an_old_unavailable(self):
        dep = deployment(template_annotations={NAMES.key("movedAt"): stamp(20)},
                         conditions=[condition("Available", "False", 2 * DAY, "MinimumReplicasUnavailable")])
        self.assertAlmostEqual(20, row(dep)["transition_age"], delta=10)


class UtcStampTests(unittest.TestCase):
    """movedAt and restartedAt end in "Z": they are UTC, not local time."""

    def setUp(self):
        self.old_tz = os.environ.get("TZ")
        os.environ["TZ"] = "XXX-14"   # 14 h ahead of UTC, no tzdata needed
        time.tzset()

    def tearDown(self):
        if self.old_tz is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = self.old_tz
        time.tzset()

    def assertNow(self, value):
        written = calendar.timegm(time.strptime(value, "%Y-%m-%dT%H:%M:%SZ"))
        self.assertLess(abs(time.time() - written), 60, value)

    def template_stamp(self, kube, key):
        dep = kube.object("/apis/apps/v1/namespaces/lab/deployments/web")
        return dep["spec"]["template"]["metadata"]["annotations"][NAMES.key(key)]

    def test_placement_writes_moved_at_in_utc(self):
        dep = deployment()
        server.PLACE.apply_node_placement(dep, "node-a")
        self.assertNow(dep["spec"]["template"]["metadata"]["annotations"][NAMES.key("movedAt")])

    def test_move_to_host_writes_moved_at_in_utc(self):
        with Kube(deployment()) as kube:
            server.LC.move_workload("lab", "web", "node-a")
            self.assertNow(self.template_stamp(kube, "movedAt"))

    def test_restart_writes_restarted_at_in_utc(self):
        with Kube(deployment()) as kube:
            server.OVERVIEW_ROUTES.restart_workload("lab", "web")
            self.assertNow(self.template_stamp(kube, "restartedAt"))


class NeverReadyTests(unittest.TestCase):

    def test_a_deployment_never_ready_counts_from_its_creation(self):
        workload = row(deployment(created_ago=2 * DAY))
        self.assertAlmostEqual(2 * DAY, workload["transition_age"], delta=10)
        self.assertEqual("degraded", verdict(workload)["health"])

    def test_never_available_since_creation_is_the_same_age(self):
        dep = deployment(created_ago=2 * DAY,
                         conditions=[condition("Available", "False", 2 * DAY, "MinimumReplicasUnavailable")])
        self.assertAlmostEqual(2 * DAY, row(dep)["transition_age"], delta=10)


if __name__ == "__main__":
    unittest.main()
