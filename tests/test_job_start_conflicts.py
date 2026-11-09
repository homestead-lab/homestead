"""Which running jobs refuse a new one, before and after the kind branches of
homestead_operations.start moved into per-kind conflict rules (#622).

start() used to hold one `if kind == ...` branch for each kind whose new job is
refused while a matching one is running. Each of those is now a function in
OPS.CONFLICTS, and the registry in homestead_job_kinds names it. The checks that
belong to no one kind stay in start(): `unless`, an import's disks, approval
receipts and the storage conflicts. reference_conflict below is that part of
start() exactly as it stood before the move; every case of the matrix must give
the same answer from both. Once each kind's rule has tests of its own, the
reference and the matrix can go."""
import itertools
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "server"))
sys.path.insert(0, str(ROOT / "tests"))

import homestead_job_kinds as KINDS
import homestead_operations as OPS
from fakes.job_store import put

TERMINAL = OPS.TERMINAL


def reference_conflict(items, kind, ref, resource):
    """The kind branches of homestead_operations.start before they moved
    (#622), returning the refusal instead of raising it."""
    RESUMABLE, POWER_RECEIPTS, _vm_targets = OPS.RESUMABLE, OPS.POWER_RECEIPTS, OPS._vm_targets
    if kind == "node-power" and any(i.get("kind") == kind and i.get("status") not in TERMINAL and
                                    i.get("ref", {}).get("node") == ref.get("node") for i in items):
        return "Host maintenance is already running. Check its job before trying again."
    if kind == "set-restart" and any(i.get("kind") == kind and i.get("status") not in TERMINAL and
                                     (i.get("ref", {}).get("ns"), i.get("ref", {}).get("name")) ==
                                     (ref.get("ns"), ref.get("name")) for i in items):
        return "A restart of this StatefulSet is already running. Check its job before trying again."
    if kind == "k3s-cluster" and any(i.get("kind") == kind and i.get("status") not in TERMINAL and
                                    (i.get("ref", {}).get("namespace"), i.get("ref", {}).get("name")) ==
                                    (ref.get("namespace"), ref.get("name")) for i in items):
        return "This batch of k3s VMs is already running. Check its job before trying again."
    if kind in ("snapshot-delete", "snapshot-revert") and any(
            i.get("kind") in ("snapshot-delete", "snapshot-revert") and i.get("status") not in TERMINAL
            and i.get("ref", {}).get("volume") == ref.get("volume") for i in items):
        return "A snapshot task is already running on this volume. Check its job first."
    if kind == "reclass" and any(i.get("kind") == "reclass" and
            i.get("resource", {}).get("name") == resource.get("name") and
            i.get("resource", {}).get("namespace") == resource.get("namespace") and
            (i.get("status") not in TERMINAL or (i.get("status") == "failed" and not RESUMABLE["reclass"](i)))
            for i in items):
        return "This volume is already moving to another storage class. Check its job first."
    if kind == "reclass" and any(
            _vm_targets(kind, ref) & _vm_targets(i.get("kind"), i.get("ref") or {}) and
            (i.get("status") not in TERMINAL or i.get("ref", {}).get("retain_resources"))
            for i in items if i.get("kind") in POWER_RECEIPTS.KINDS | {"k3s-cluster"}):
        return "An affected app or volume has another job or recovery hold. Check it before moving storage."
    return ""


def conflict(items, kind, ref, resource):
    rule = OPS.CONFLICTS.get(kind)
    return rule(items, kind, ref, resource) if rule else ""


RULED = ["node-power", "set-restart", "k3s-cluster", "snapshot-delete", "snapshot-revert", "reclass"]
# Every kind with a rule, receipt kinds a reclass is checked against, and one no registry knows.
KIND_NAMES = RULED + ["vm-power", "workload-copy", "workload-rename", "import-create", "no-such-kind"]
ONE = {"node": "node-a", "ns": "lab", "namespace": "lab", "name": "web", "volume": "vol-a", "claim": "data",
       "consumers": [{"kind": "Deployment", "name": "web"}, {"kind": "VirtualMachine", "name": "vm-a"}],
       "nodes": [{"name": "vm-a"}]}  # a k3s batch holding the VM a reclass of ONE touches
TWO = {"node": "node-b", "ns": "lab", "namespace": "lab", "name": "db", "volume": "vol-b", "claim": "logs",
       "consumers": [{"kind": "Deployment", "name": "db"}], "nodes": [{"name": "db"}]}
REFS = [{}, ONE, TWO, dict(ONE, retain_resources=True), dict(ONE, resumable=True),
        dict(ONE, namespace="other", ns="other")]
RESOURCES = [{"name": "pvc-a", "namespace": "lab"}, {"name": "pvc-b", "namespace": "lab"}, {},
             {"name": "pvc-a", "namespace": "other"}]
STATUSES = ["running", "queued", "succeeded", "failed"]


def existing():
    """No job at all, then one job of every kind, status, ref and resource."""
    yield []
    for kind, status, ref, resource in itertools.product(KIND_NAMES, STATUSES, REFS, RESOURCES):
        yield [{"id": "old", "kind": kind, "status": status, "ref": dict(ref), "resource": dict(resource)}]


def cases():
    for items in existing():
        for kind, ref, resource in itertools.product(KIND_NAMES, REFS[:3], RESOURCES[:2]):
            yield items, kind, dict(ref), dict(resource)


def resumable(item):
    return bool(item.get("ref", {}).get("resumable"))


class SameAnswerTests(unittest.TestCase):
    def setUp(self):
        patch = mock.patch.dict(OPS.RESUMABLE, {"reclass": resumable})
        patch.start()
        self.addCleanup(patch.stop)

    def test_every_case_gives_the_answer_it_gave_before(self):
        count = 0
        for items, kind, ref, resource in cases():
            with self.subTest(kind=kind, ref=ref, resource=resource, items=items):
                self.assertEqual(reference_conflict(items, kind, ref, resource), conflict(items, kind, ref, resource))
            count += 1
        self.assertEqual((1 + len(KIND_NAMES) * len(STATUSES) * len(REFS) * len(RESOURCES))
                         * len(KIND_NAMES) * 3 * 2, count)

    def test_the_matrix_reaches_every_rule(self):
        """Every refusal of the old code comes out of at least one case, so a
        rule lost from the table would show."""
        said = {reference_conflict(*case) for case in cases()} - {""}
        self.assertEqual(6, len(said))

    def test_the_matrix_reaches_every_kinds_rule(self):
        for kind in RULED:
            with self.subTest(kind=kind), mock.patch.dict(OPS.CONFLICTS, {kind: lambda items, kind, ref, resource: ""}):
                self.assertTrue(any(reference_conflict(*case) != conflict(*case)
                                    for case in cases() if case[1] == kind))

    def test_a_reclass_names_its_own_volume_before_the_workloads_it_touches(self):
        items = [{"id": "a", "kind": "reclass", "status": "running", "ref": {},
                  "resource": {"name": "pvc-a", "namespace": "lab"}},
                 {"id": "b", "kind": "workload-copy", "status": "running", "ref": dict(ONE), "resource": {}}]
        self.assertEqual(reference_conflict(items, "reclass", ONE, RESOURCES[0]),
                         conflict(items, "reclass", ONE, RESOURCES[0]))
        self.assertIn("moving to another storage class", conflict(items, "reclass", ONE, RESOURCES[0]))


class StartTests(unittest.TestCase):
    """The rules are still checked by start(), at the same point as before:
    after an approval's receipt, before the storage conflicts."""

    def setUp(self):
        folder = self.enterContext(tempfile.TemporaryDirectory())
        for patch in (mock.patch.object(OPS, "DATA_DIR", folder),
                      mock.patch.dict(OPS.RESUMABLE, {"reclass": resumable})):
            patch.start()
            self.addCleanup(patch.stop)

    def test_each_rule_refuses_through_start(self):
        for kind in RULED:
            with self.subTest(kind=kind):
                old = {"id": "a" * 24, "kind": kind, "status": "running", "ref": dict(ONE),
                       "resource": dict(RESOURCES[0]), "title": "old"}
                if kind == "reclass":  # its second rule: a workload job it would touch
                    old = dict(old, kind="workload-copy", ref=dict(ONE, review_digest="b" * 64))
                put(OPS, [old])
                with self.assertRaises(ValueError) as caught:
                    OPS.start(kind, "new", RESOURCES[0], "/", dict(ONE))
                self.assertEqual(reference_conflict([old], kind, ONE, RESOURCES[0]), str(caught.exception))

    def test_a_rule_comes_before_the_storage_conflicts(self):
        put(OPS, [{"id": "a" * 24, "kind": "node-power", "status": "running", "ref": dict(ONE),
                   "resource": {}, "title": "old"}])
        with mock.patch.object(OPS.STORAGE_CONFLICTS, "require_clear",
                               side_effect=ValueError("storage conflict")):
            with self.assertRaisesRegex(ValueError, "Host maintenance"):
                OPS.start("node-power", "new", {}, "/", dict(ONE))

    def test_the_storage_conflicts_still_run_when_no_rule_refuses(self):
        put(OPS, [])
        with mock.patch.object(OPS.STORAGE_CONFLICTS, "require_clear",
                               side_effect=ValueError("storage conflict")):
            with self.assertRaisesRegex(ValueError, "storage conflict"):
                OPS.start("node-power", "new", {}, "/", dict(ONE))

    def test_unless_comes_before_a_rule(self):
        put(OPS, [{"id": "a" * 24, "kind": "node-power", "status": "running", "ref": dict(ONE),
                   "resource": {}, "title": "Host upkeep"}])
        with self.assertRaisesRegex(ValueError, "Host upkeep is running"):
            OPS.start("node-power", "new", {}, "/", dict(ONE), unless=("node-power",))


class RegistryTests(unittest.TestCase):
    def test_every_kind_that_had_a_rule_has_one_of_its_own(self):
        self.assertEqual(set(RULED), set(OPS.CONFLICTS))

    def test_both_snapshot_kinds_share_one_rule(self):
        self.assertIs(OPS.CONFLICTS["snapshot-delete"], OPS.CONFLICTS["snapshot-revert"])

    def test_a_kind_names_its_own_rule(self):
        import server  # noqa: F401  (fills the registries, so every kind is there)
        found = KINDS.kinds()
        self.assertEqual(set(), set(OPS.CONFLICTS) - set(found))
        for name, rule in OPS.CONFLICTS.items():
            with self.subTest(kind=name):
                self.assertIs(rule, found[name].conflicts)

    def test_a_kind_without_a_rule_of_its_own_has_none(self):
        import types
        ops = types.SimpleNamespace(RESOLVERS={"scan": lambda item: None}, CANCELLERS={}, RESUMABLE={},
                                    LOGGERS={}, CLEANUPS=set(), CDI_CANCELLABLE=set(), VIEWS={},
                                    CANCEL_BLOCKS={}, CONFLICTS={}, _public=lambda item: item)
        rules = KINDS.kind("scan", ops).conflicts
        self.assertEqual("", rules([{"kind": "scan", "status": "running"}], "scan", {}, {}))

    def test_a_rule_registered_later_is_used(self):
        with tempfile.TemporaryDirectory() as folder, mock.patch.object(OPS, "DATA_DIR", folder), \
                mock.patch.dict(OPS.CONFLICTS, {"late-kind": lambda items, kind, ref, resource: "not now"}):
            put(OPS, [])
            with self.assertRaisesRegex(ValueError, "not now"):
                OPS.start("late-kind", "new", {}, "/", {})


if __name__ == "__main__":
    unittest.main()
