"""Jobs that only wait look at the host at most every so often (#975).

While a disk preparation waits for its erase review, a Longhorn runtime move
waits for a reboot, a host OS update installs, or a SMART self-test runs, the
jobs loop asks each of them every 3 s. Each ask that reaches the host starts a
helper pod there. They now look at most once per their kind's interval and
answer from their last look in between, as host promotion already does."""
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "server"))

import homestead_job_cadence as CADENCE


class Clock:
    def __init__(self, start=1000.0):
        self.now = start

    def __call__(self):
        return self.now


class CadenceHelperTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.enterContext(mock.patch.object(CADENCE.time, "time", self.clock))

    def ticks(self, ref, kind, look, rounds=20, step=3):
        """Ask like the jobs loop: every `step` s, `rounds` times."""
        item = {"ref": ref}
        answers = []
        for _ in range(rounds):
            answers.append(CADENCE.look(item, kind, look))
            self.clock.now += step
        return answers

    def test_a_waiting_kind_looks_at_most_once_per_interval(self):
        for kind, every in CADENCE.INTERVALS.items():
            with self.subTest(kind=kind):
                calls = []
                self.ticks({}, kind, lambda: calls.append(1) or ("running", 50, "waiting"))
                # 20 asks 3 s apart span 57 s.
                self.assertLessEqual(len(calls), 57 // every + 1)
                self.assertGreaterEqual(len(calls), 1)

    def test_between_looks_the_last_answer_is_given(self):
        answers = self.ticks({}, "longhorn-runtime", lambda: ("running", 20, "Reboot to move runtime"), rounds=5)
        self.assertEqual([("running", 20, "Reboot to move runtime")] * 5, answers)

    def test_a_change_on_the_host_shows_within_one_interval(self):
        state = {"phase": "staged"}
        item = {"ref": {}}
        look = lambda: ("running", 20 if state["phase"] == "staged" else 85, state["phase"])
        CADENCE.look(item, "longhorn-runtime", look)
        state["phase"] = "relocated"
        seen_at = None
        start = self.clock.now
        for _ in range(30):
            self.clock.now += 3
            if CADENCE.look(item, "longhorn-runtime", look)[2] == "relocated":
                seen_at = self.clock.now
                break
        self.assertIsNotNone(seen_at)
        self.assertLessEqual(seen_at - start, CADENCE.INTERVALS["longhorn-runtime"] + 3)

    def test_a_finished_state_is_seen_at_the_next_look(self):
        """A job that finished on the host shows as finished at the next look."""
        item = {"ref": {}}
        CADENCE.look(item, "host-os", lambda: ("running", 50, "installing"))
        self.clock.now += 3
        # Not due yet: the last answer comes back, the look is not made.
        self.assertEqual(("running", 50, "installing"), CADENCE.look(item, "host-os", lambda: ("succeeded", 100, "done")))
        self.clock.now += CADENCE.INTERVALS["host-os"]
        self.assertEqual(("succeeded", 100, "done"), CADENCE.look(item, "host-os", lambda: ("succeeded", 100, "done")))

    def test_a_look_that_fails_reaches_the_resolver_and_keeps_no_answer(self):
        """Errors are the resolver's to handle, as before: nothing is kept."""
        item = {"ref": {}}
        def broken():
            raise ValueError("helper did not answer")
        with self.assertRaises(ValueError):
            CADENCE.look(item, "smart-test", broken)
        self.assertNotIn("answer", item.get(CADENCE.KEY, {}))
        self.clock.now += 3
        self.assertEqual(("running", 1, "ok"), CADENCE.look(item, "smart-test", lambda: ("running", 1, "ok")))

    def test_a_long_self_test_looks_less_often(self):
        ref = {"expected_seconds": 50 * 120}  # a 100-minute test: every 120 s
        self.assertEqual(120, CADENCE.interval(ref, "smart-test"))
        # A 20-hour test still looks at least every SMART_MAX, so its end shows soon.
        self.assertEqual(CADENCE.SMART_MAX, CADENCE.interval({"expected_seconds": 72000}, "smart-test"))
        self.assertEqual(CADENCE.INTERVALS["smart-test"], CADENCE.interval({}, "smart-test"))



    def test_a_look_never_writes_into_the_jobs_ref(self):
        """ref is hashed into reviews and compared with saved host plans."""
        item = {"ref": {"node": "node-a", "phase": "relocated"}}
        before = dict(item["ref"])
        CADENCE.look(item, "longhorn-runtime", lambda: ("running", 70, "moved"))
        self.assertEqual(before, item["ref"])

    def test_a_phase_change_made_elsewhere_looks_again_at_once(self):
        """A Recheck or Retire changes ref['phase']: the next ask looks fresh."""
        item, calls = {"ref": {"phase": "relocated"}}, []
        look = lambda: calls.append(1) or ("running", 70, item["ref"]["phase"])
        CADENCE.look(item, "longhorn-runtime", look)
        item["ref"]["phase"] = "retiring"
        self.clock.now += 3
        self.assertEqual("retiring", CADENCE.look(item, "longhorn-runtime", look)[2])
        self.assertEqual(2, len(calls))

    def test_a_forced_look_is_made_even_inside_the_interval(self):
        item, calls = {"ref": {}}, []
        look = lambda: calls.append(1) or ("running", 50, "installing")
        CADENCE.look(item, "host-os", look)
        self.clock.now += 3
        CADENCE.look(item, "host-os", look, force=True)
        self.assertEqual(2, len(calls))

class ResolverTests(unittest.TestCase):
    """The real resolvers, ticked 20 times 3 s apart against a counting host."""
    def setUp(self):
        self.clock = Clock()
        self.enterContext(mock.patch.object(CADENCE.time, "time", self.clock))

    def test_a_host_os_update_asks_its_host_at_most_once_per_interval(self):
        import homestead_host_os as HOSTOS
        calls = []
        self.enterContext(mock.patch.object(HOSTOS, "upgrade_state",
                                            lambda node: calls.append(node) or {"code": None, "last": "unpacking"}))
        item = {"ref": {"node": "node-a", "since": self.clock.now - 60}}
        for _ in range(20):
            self.assertEqual("running", HOSTOS._step(item, now=self.clock.now)[0])
            self.clock.now += 3
        self.assertLessEqual(len(calls), 57 // CADENCE.INTERVALS["host-os"] + 1)

    def test_a_smart_self_test_asks_its_host_at_most_once_per_interval(self):
        import homestead_operations as OPS
        calls = []
        self.enterContext(mock.patch.object(OPS, "smart_progress",
                                            lambda ref: calls.append(1) or ("running", 40, "Self-test in progress")))
        item = {"ref": {"node": "node-a", "disk": "sda"}}
        for _ in range(20):
            OPS._smart_test(item)
            self.clock.now += 3
        self.assertLessEqual(len(calls), 57 // CADENCE.INTERVALS["smart-test"] + 1)


    def test_a_disk_preparation_at_its_erase_review_asks_its_host_at_most_once_per_interval(self):
        import homestead_disk_v2 as V2
        calls = []
        self.enterContext(mock.patch.object(V2, "_inventory", lambda node, disk: {"spec": {}}))
        self.enterContext(mock.patch.object(V2, "_verify_original", lambda inv, ref: None))
        self.enterContext(mock.patch.object(V2, "_evacuated",
                                            lambda inv, ref: calls.append(1) or (True, "Ready for the erase review")))
        item = {"ref": {"phase": "awaiting-erase", "node": "node-a", "disk": "disk-a",
                        "eviction_started": True, "volumes": [], "remaining": 0}}
        for _ in range(20):
            self.assertEqual("running", V2.progress(item)[0])
            self.clock.now += 3
        self.assertLessEqual(len(calls), 57 // CADENCE.INTERVALS["disk-v2-convert"] + 1)

    def test_a_runtime_move_waiting_for_a_reboot_asks_its_host_at_most_once_per_interval(self):
        import homestead_longhorn_runtime as RUNTIME
        calls = []
        self.enterContext(mock.patch.object(RUNTIME.V2, "_ready", lambda node: True))
        self.enterContext(mock.patch.object(RUNTIME.V2, "_get", lambda path: {}))
        self.enterContext(mock.patch.object(RUNTIME, "_folder", lambda ref: "folder"))
        self.enterContext(mock.patch.object(RUNTIME, "_call",
                                            lambda node, code, write=False: calls.append(code) or {"phase": "staged"}))
        item = {"ref": {"phase": "staged", "node": "node-a"}}
        for _ in range(20):
            self.assertEqual("running", RUNTIME.progress(item)[0])
            self.clock.now += 3
        self.assertLessEqual(len(calls), 57 // CADENCE.INTERVALS["longhorn-runtime"] + 1)

    def test_a_look_saved_by_a_clock_ahead_of_this_one_is_not_trusted(self):
        """The leader can move to a pod whose clock is behind the saved time."""
        item, calls = {"ref": {}}, []
        look = lambda: calls.append(1) or ("running", 50, "installing")
        CADENCE.look(item, "host-os", look)
        self.clock.now -= 600
        CADENCE.look(item, "host-os", look)
        self.assertEqual(2, len(calls))


class PublicTests(unittest.TestCase):
    def test_the_cadence_state_is_not_shown_in_the_jobs_list(self):
        import homestead_operations as OPS
        item = {"id": "a", "kind": "host-os", "status": "running", "ref": {},
                CADENCE.KEY: {"at": 1, "phase": None, "answer": ["running", 50, "installing"]}}
        self.assertNotIn(CADENCE.KEY, OPS._public(item))


class RatchetTests(unittest.TestCase):
    def test_the_kinds_known_to_wait_on_their_host_declare_an_interval(self):
        for kind in ("disk-v2-convert", "longhorn-runtime", "host-os", "smart-test"):
            with self.subTest(kind=kind):
                self.assertIn(kind, CADENCE.INTERVALS)
                self.assertGreaterEqual(CADENCE.INTERVALS[kind], 30)


if __name__ == "__main__":
    unittest.main()
