"""The checks every disk change passes first (homestead_disks).

Three saved jobs hold a host's disks: a V2 preparation (homestead_disk_v2), a
storage allocation (homestead_disk_allocation) and Longhorn runtime maintenance
(homestead_longhorn_runtime). While one is saved, the nine disk changes are
refused on that host. These tests drive the real disk module's public
functions with the three real guards installed, a job store stand-in, and a
Kubernetes read that only says it was reached.
"""
import sys
import threading
import types
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
import homestead_disk_allocation as ALLOC
import homestead_disk_v2 as V2
import homestead_disks as DISKS
import homestead_longhorn_runtime as RUNTIME
from fakes.job_store import JobStore
from rebind import keep_bound

GUARDED = ("set_disk_tags", "set_scheduling", "evict", "remove", "set_node_tags",
           "add", "set_up", "use_os_space", "retire_start")


class Reached(Exception):
    """The change got past its guard and asked the cluster for something."""


class Operations(JobStore):
    def __init__(self):
        self._lock, self.items = threading.RLock(), []

    def _read(self):
        return self.items


def actions(ops):
    """Each guarded change, on disk "data" (device /dev/sdb, mounted at /mnt/data) of node-1."""
    return {
        "set_disk_tags": lambda: DISKS.set_disk_tags("node-1", "data", ["fast"]),
        "set_scheduling": lambda: DISKS.set_scheduling("node-1", "data", False),
        "evict": lambda: DISKS.evict("node-1", "data", True),
        "remove": lambda: DISKS.remove("node-1", "data"),
        "set_node_tags": lambda: DISKS.set_node_tags("node-1", ["fast"]),
        "add": lambda: DISKS.add({"node": "node-1", "path": "/mnt/data"}),
        "set_up": lambda: DISKS.set_up({"node": "node-1", "device": "/dev/sdb"}),
        "use_os_space": lambda: DISKS.use_os_space({"node": "node-1"}),
        "retire_start": lambda: DISKS.retire_start({"node": "node-1", "disk": "data"}, ops),
    }


V2_TASK = {"id": "v2", "kind": V2.KIND, "ref": {"node": "node-1", "disk": "data", "phase": "awaiting-approval",
                                                "hardware": {"device": "/dev/sdb", "mountpoint": "/mnt/data"}}}
ALLOCATION = {"id": "alloc", "kind": ALLOC.KIND, "ref": {"node": "node-1", "phase": "queued"}}
MAINTENANCE = {"id": "rt", "kind": RUNTIME.KIND, "ref": {"node": "node-1", "phase": "staged"}}


class Guarded(unittest.TestCase):
    """The disk module bound and its guards installed in server.py's order."""

    def setUp(self):
        keep_bound(self, DISKS)
        # Everything installing the guards may change, put back after the test.
        # Written so it also runs against older disk modules, which kept the
        # guards in mutation_scope and wrapped the nine functions in place.
        for name in ("kget", "ksend", "temps", "setup_module", "v2_tasks", "mutation_scope", *GUARDED):
            if hasattr(DISKS, name):
                self.enterContext(mock.patch.object(DISKS, name, getattr(DISKS, name)))
        self.enterContext(mock.patch.dict(getattr(DISKS, "GUARDS", {})))
        self.ops = Operations()
        for module in (V2, ALLOC, RUNTIME):
            self.enterContext(mock.patch.object(module, "ops", self.ops))
        self.bind()
        # server.py: DISKS.bind, then each guard.
        V2.protect_mutations(DISKS)
        ALLOC.protect_mutations(DISKS)
        RUNTIME.protect_mutations(DISKS)

    def bind(self):
        self.reached = []
        def reached(*args, **kwargs):
            self.reached.append(self.ops._lock._is_owned())
            raise Reached(args)
        DISKS.bind(reached, reached, lambda: {})
        DISKS.setup_module = types.SimpleNamespace(inspect=reached, use_os_space=reached, refusal=reached)

    def attempt(self, apply):
        """What the change did: "refused: <message>", or "reached" the cluster."""
        self.reached.clear()
        try:
            apply()
        except ValueError as e:
            if not self.reached:
                return "refused: " + str(e)
        except Exception:
            pass     # the read that only says it was reached, or what the change made of it
        return "reached" if self.reached else "nothing"

    def assertAllRefused(self, pattern):
        for name, apply in actions(self.ops).items():
            with self.subTest(action=name):
                self.assertRegex(self.attempt(apply), "^refused: .*" + pattern)

    def assertAllThrough(self):
        for name, apply in actions(self.ops).items():
            with self.subTest(action=name):
                self.assertEqual(self.attempt(apply), "reached")
                # The guard holds the job store's lock for the whole change.
                self.assertTrue(all(self.reached), self.reached)


class NormalOrderTests(Guarded):
    def test_every_change_goes_through_when_no_job_holds_the_host(self):
        self.assertAllThrough()

    def test_a_saved_v2_preparation_refuses_every_change_on_its_disk(self):
        self.ops.items.append(V2_TASK)
        self.assertAllRefused("saved V2 preparation")

    def test_a_storage_allocation_job_refuses_every_change_on_its_host(self):
        self.ops.items.append(ALLOCATION)
        self.assertAllRefused("storage allocation job")

    def test_runtime_maintenance_refuses_every_change_on_its_host(self):
        self.ops.items.append(MAINTENANCE)
        self.assertAllRefused("Longhorn runtime maintenance")

    def test_maintenance_is_checked_first_then_allocation_then_v2(self):
        self.ops.items.extend([V2_TASK, ALLOCATION, MAINTENANCE])
        with self.assertRaisesRegex(ValueError, "runtime maintenance"):
            DISKS.evict("node-1", "data", True)
        self.ops.items.remove(MAINTENANCE)
        with self.assertRaisesRegex(ValueError, "storage allocation"):
            DISKS.evict("node-1", "data", True)

    def test_a_job_on_another_host_refuses_nothing(self):
        for item in (V2_TASK, ALLOCATION, MAINTENANCE):
            self.ops.items.append({**item, "ref": {**item["ref"], "node": "node-2"}})
        self.assertAllThrough()

    def test_the_inventory_shows_the_saved_preparation(self):
        self.ops.items.append(V2_TASK)
        self.assertEqual([t["id"] for t in DISKS.v2_tasks()], ["v2"])


class KeptGuardsTests(Guarded):
    def test_binding_the_disk_module_again_keeps_every_guard(self):
        self.ops.items.extend([V2_TASK, ALLOCATION, MAINTENANCE])
        self.bind()
        self.assertAllRefused("runtime maintenance")
        self.ops.items.remove(MAINTENANCE)
        self.assertAllRefused("storage allocation")
        self.ops.items.remove(ALLOCATION)
        self.assertAllRefused("saved V2 preparation")
        self.assertEqual([t["id"] for t in DISKS.v2_tasks()], ["v2"])

    def test_installing_a_guard_twice_checks_it_once(self):
        V2.protect_mutations(DISKS)
        RUNTIME.protect_mutations(DISKS)
        calls = []
        real = V2.mutation_guard
        with mock.patch.object(V2, "mutation_guard", lambda *a: (calls.append(a), real(*a))):
            self.assertEqual(self.attempt(lambda: DISKS.evict("node-1", "data", True)), "reached")
        self.assertEqual(len(calls), 1)


class MissingGuardTests(Guarded):
    def test_a_change_is_refused_while_no_guard_is_installed(self):
        self.enterContext(mock.patch.dict(getattr(DISKS, "GUARDS", {}), clear=True))
        self.bind()
        self.assertAllRefused("safety checks")

    def test_a_change_is_refused_while_one_guard_is_missing(self):
        # The server's order, one guard short: freshly bound, and never installed.
        self.enterContext(mock.patch.dict(getattr(DISKS, "GUARDS", {}), clear=True))
        self.bind()
        V2.protect_mutations(DISKS)
        ALLOC.protect_mutations(DISKS)
        self.assertAllRefused("safety checks are not set up \\(longhorn_runtime\\)")

    def test_only_the_known_guards_can_be_installed(self):
        with self.assertRaisesRegex(ValueError, "unknown disk guard"):
            DISKS.add_guard("disk-v2", lambda *a: None)

if __name__ == "__main__":
    unittest.main()
