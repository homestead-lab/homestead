"""What a returned host must pass before containers move back by themselves
(#588). Fixtures keep the shape of real Longhorn objects: spec.nodeID,
spec.failedAt and status.replicaModeMap, read from a cluster on 6 Nov. All were
healthy then, so the WO and failed values follow homestead_whole_copy.rebuilding
and haven't been captured from a live rebuild."""
import calendar
import sys
import time
import unittest
import urllib.error
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import homestead_data_copies as DATA_COPIES
import homestead_priority as PRIORITY
import homestead_recovery_auto as AUTO

T = calendar.timegm((2026, 10, 7, 12, 0, 0))


def stamp(t):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t))


def node(name, ready_at=None, ready=True, cordoned=False, taints=()):
    condition = {"type": "Ready", "status": "True" if ready else "False"}
    if ready_at is not None:
        condition["lastTransitionTime"] = ready_at
    return {"metadata": {"name": name}, "spec": {"unschedulable": cordoned, "taints": list(taints)}, "status": {"conditions": [condition]}}


def volume(name, state="attached", robustness="healthy"):
    return {"metadata": {"name": name}, "status": {"state": state, "robustness": robustness}}


def replica(name, vol, host, failed=""):
    return {"metadata": {"name": name}, "spec": {"volumeName": vol, "nodeID": host, "failedAt": failed}}


def engine(vol, **modes):
    return {"metadata": {"name": f"{vol}-e-0"}, "spec": {"volumeName": vol}, "status": {"replicaModeMap": modes}}


class Steady(unittest.TestCase):
    def test_steady_counts_from_the_later_of_return_and_ready(self):
        self.assertEqual(T + 100, AUTO.steady_since(node("a", stamp(T + 100)), T))
        self.assertEqual(T, AUTO.steady_since(node("a", stamp(T - 100)), T))

    def test_a_missing_ready_time_uses_the_return(self):
        self.assertEqual(T, AUTO.steady_since(node("a"), T))
        self.assertEqual(T, AUTO.steady_since(node("a", "not a time"), T))

    def test_steady_left_counts_down_to_nothing(self):
        self.assertEqual(900, AUTO.steady_left(node("a"), T, T))
        self.assertEqual(300, AUTO.steady_left(node("a"), T, T + 600))
        self.assertEqual(0, AUTO.steady_left(node("a"), T, T + 900))
        self.assertEqual(0, AUTO.steady_left(node("a"), T, T + 5000))

    def test_a_ready_time_in_the_future_waits_in_full(self):
        self.assertEqual(900, AUTO.steady_left(node("a", stamp(T + 3600)), T, T + 1000))


class Copies(unittest.TestCase):
    def test_a_wo_copy_on_the_host_counts(self):
        left = AUTO.copies_left("a", [volume("v1")], [replica("r1", "v1", "a"), replica("r2", "v1", "b")], [engine("v1", r1="WO", r2="RW")])
        self.assertEqual(["v1"], left)

    def test_a_wo_copy_elsewhere_does_not(self):
        left = AUTO.copies_left("a", [volume("v1")], [replica("r1", "v1", "a"), replica("r2", "v1", "b")], [engine("v1", r1="RW", r2="WO")])
        self.assertEqual([], left)

    def test_a_replica_name_shared_with_another_volumes_engine_does_not_count(self):
        self.assertEqual([], AUTO.copies_left("a", [volume("v1"), volume("v2")], [replica("r1", "v1", "a")], [engine("v2", r1="WO")]))

    def test_a_failed_copy_on_a_degraded_attached_volume_counts(self):
        left = AUTO.copies_left("a", [volume("v1", robustness="degraded")], [replica("r1", "v1", "a", failed="2026-10-07T11:00:00Z")], [])
        self.assertEqual(["v1"], left)

    def test_a_failed_copy_on_a_healthy_or_detached_volume_does_not(self):
        for vol in (volume("v1", robustness="healthy"), volume("v1", state="detached", robustness="degraded")):
            self.assertEqual([], AUTO.copies_left("a", [vol], [replica("r1", "v1", "a", failed="2026-10-07T11:00:00Z")], []))

    def test_a_failed_copy_on_another_host_does_not(self):
        self.assertEqual([], AUTO.copies_left("a", [volume("v1", robustness="degraded")], [replica("r1", "v1", "b", failed="x")], []))

    def test_a_copy_rebuilding_in_any_engine_counts_in_either_list_order(self):
        engines = [engine("v1", r1="WO"), {**engine("v1", r1="RW"), "metadata": {"name": "v1-e-1"}}]
        for order in (engines, engines[::-1]):
            self.assertEqual(["v1"], AUTO.copies_left("a", [volume("v1")], [replica("r1", "v1", "a")], order))

    def test_any_copy_on_the_host_not_rw_counts(self):
        for mode in ("ERR", "", "WO"):
            self.assertEqual(["v1"], AUTO.copies_left("a", [volume("v1")], [replica("r1", "v1", "a")], [engine("v1", r1=mode)]), mode)

    def test_a_copy_not_in_the_map_of_a_degraded_attached_volume_counts(self):
        self.assertEqual(["v1"], AUTO.copies_left("a", [volume("v1", robustness="degraded")], [replica("r1", "v1", "a")], [engine("v1", r2="RW")]))

    def test_a_copy_not_in_the_map_of_a_healthy_or_detached_volume_does_not(self):
        for vol in (volume("v1"), volume("v1", state="detached", robustness="degraded")):
            self.assertEqual([], AUTO.copies_left("a", [vol], [replica("r1", "v1", "a")], [engine("v1", r2="RW")]))

    def test_a_volume_counts_once(self):
        left = AUTO.copies_left("a", [volume("v1", robustness="degraded")],
                                [replica("r1", "v1", "a", failed="x"), replica("r2", "v1", "a")], [engine("v1", r2="WO")])
        self.assertEqual(["v1"], left)

    def test_no_longhorn_means_no_wait(self):
        def absent(path):
            raise urllib.error.HTTPError(path, 404, "no", {}, None)
        self.assertEqual(([], [], []), AUTO.read_copies(absent))

    def test_one_list_answering_404_while_another_answers_raises(self):
        paths = (AUTO.VOLUMES, AUTO.REPLICAS, DATA_COPIES.ENGINES)
        for missing in paths:
            def partly(path, missing=missing):
                if path == missing:
                    raise urllib.error.HTTPError(path, 404, "no", {}, None)
                return {"items": []}
            with self.assertRaises(ValueError, msg=missing):
                AUTO.read_copies(partly)

    def test_a_404_with_another_list_failing_raises_that_failure(self):
        def mixed(path):
            raise urllib.error.HTTPError(path, 404 if path == AUTO.VOLUMES else 503, "no", {}, None)
        with self.assertRaises(urllib.error.HTTPError) as caught:
            AUTO.read_copies(mixed)
        self.assertEqual(503, caught.exception.code)

    def test_longhorn_unreadable_raises(self):
        for code in (500, 503, 403):
            def broken(path, code=code):
                raise urllib.error.HTTPError(path, code, "no", {}, None)
            with self.assertRaises(urllib.error.HTTPError):
                AUTO.read_copies(broken)

    def test_a_list_failing_after_the_first_read_raises(self):
        def half(path):
            if path == DATA_COPIES.ENGINES:
                raise TimeoutError("engines")
            return {"items": []}
        with self.assertRaises(TimeoutError):
            AUTO.read_copies(half)

    def test_a_reply_with_no_items_is_not_read_as_none_rebuilding(self):
        with self.assertRaises(ValueError):
            AUTO.read_copies(lambda path: {})

    def test_all_three_lists_are_read(self):
        lists = {AUTO.VOLUMES: {"items": [1]}, AUTO.REPLICAS: {"items": [2]}, DATA_COPIES.ENGINES: {"items": [3]}}
        self.assertEqual(([1], [2], [3]), AUTO.read_copies(lists.__getitem__))


class Away(unittest.TestCase):
    def test_a_host_that_has_worked_and_takes_no_work_is_away(self):
        nodes = [node("a"), node("b", ready=False), node("c", cordoned=True)]
        self.assertEqual(["b", "c"], AUTO.away(nodes, ["a", "b", "c"], (), ()))

    def test_a_tiebreaker_is_not_away(self):
        tiebreak = node("t", taints=[{"key": "etcd", "effect": "NoSchedule"}])
        self.assertEqual([], AUTO.away([tiebreak], ["a"], (), ()))

    def test_a_standby_host_is_not_away(self):
        self.assertEqual([], AUTO.away([node("s", ready=False)], ["s"], ["s"], ()))

    def test_a_returned_host_is_not_away(self):
        self.assertEqual([], AUTO.away([node("b", cordoned=True)], ["b"], (), ["b"]))


class Tier(unittest.TestCase):
    LADDER = PRIORITY.ladder([{"metadata": {"name": "important"}, "value": 1000},
                              {"metadata": {"name": "everyday"}, "value": 0, "globalDefault": True},
                              {"metadata": {"name": "last"}, "value": -10}])

    def dep(self, name=""):
        return {"spec": {"template": {"spec": {"priorityClassName": name} if name else {}}}}

    def test_critical_waits_and_normal_and_low_do_not(self):
        self.assertTrue(AUTO.waits_for_window(self.dep("important"), self.LADDER))
        self.assertFalse(AUTO.waits_for_window(self.dep("everyday"), self.LADDER))
        self.assertFalse(AUTO.waits_for_window(self.dep("last"), self.LADDER))
        self.assertFalse(AUTO.waits_for_window(self.dep(), self.LADDER))

    def test_an_unreadable_tier_waits_for_the_window(self):
        self.assertTrue(AUTO.waits_for_window(self.dep("gone"), self.LADDER), "missing")
        self.assertTrue(AUTO.waits_for_window(self.dep("gone"), {**self.LADDER, "fallback": True}), "unknown")


if __name__ == "__main__":
    unittest.main()
