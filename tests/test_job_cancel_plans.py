"""What cancelling each kind of job would do, before and after the kind
branches of homestead_operations._plan_for moved into per-kind refusals (#622).

_plan_for used to hold one `if item.get("kind") == ...` branch for each kind
whose jobs cannot be cancelled at some step. Each of those is now a function in
OPS.CANCEL_BLOCKS that says why, and the registry in homestead_job_kinds names
it. reference_plan below is _plan_for exactly as it stood before the move;
every case of the matrix must give the same plan from both. Once each kind's
refusal has tests of its own, the reference and the matrix can go."""
import itertools
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "server"))

import homestead_job_kinds as KINDS
import homestead_operations as OPS


def reference_plan(item):
    """homestead_operations._plan_for before its kind branches moved (#622)."""
    plan = {"mode": "forget", "can": True, "why_not": "", "undo": [], "severity": "low",
            "keeps": ["Homestead has no way to stop this kind of job: it carries on in Kubernetes "
                      "and only stops showing here."],
            "confirm": "", "needs": "operator", "options": []}
    entry = OPS.CANCELLERS.get(item.get("kind"))
    if item.get("ref", {}).get("cdi_cleanup") and item.get("kind") not in OPS.CDI_CANCELLABLE:
        plan.update(can=False, why_not="The import must finish reclaiming its temporary storage before its job can be dismissed.")
        return plan
    if item.get("kind") == "self-data-handoff":
        plan.update(can=False, why_not="A data move cannot be safely cancelled after handoff. Both volumes and its progress record are retained.")
        return plan
    if item.get("kind") == "reclass" and "storage_protocol" in item.get("ref", {}):
        plan.update(can=False, why_not="Use the storage move review to pause safely. Cancelling must not roll back or delete kept data.")
        return plan
    if item.get("kind") == "longhorn-v2-prepare":
        plan.update(can=False, why_not="Stopping tracking does not undo host package and memory changes. Check the preparation log.")
        return plan
    if item.get("kind") == "longhorn-v2-cleanup":
        plan.update(can=False, why_not="Inspect saved V2 cleanup. A host may have memory changes awaiting restart.")
        return plan
    if item.get("kind") == "snapshot-delete":
        plan.update(can=False, why_not="Longhorn snapshot removal cannot be undone or safely cancelled")
        return plan
    if item.get("kind") == "disk-allocation":
        plan.update(can=False, why_not="Storage growth cannot be interrupted safely. Let the helper finish, then recheck its saved outcome.")
        return plan
    if item.get("kind") == "longhorn-runtime":
        plan.update(can=False, why_not='Runtime maintenance keeps a boot gate and retained source. Recheck its saved state; do not discard it.')
        return plan
    if entry:
        plan.update(entry[0](item) or {})
    plan["mode"] = plan["mode"] if plan["mode"] in OPS.MODES else "stop"
    plan.setdefault("action", OPS.MODES[plan["mode"]])
    plan["undo"] = [str(line) for line in plan.get("undo") or []]
    plan["keeps"] = [str(line) for line in plan.get("keeps") or []]
    return plan


BLOCKED = ["self-data-handoff", "reclass", "longhorn-v2-prepare", "longhorn-v2-cleanup", "snapshot-delete", "disk-allocation", "longhorn-runtime"]
# Every blocked kind, a kind cancellable while its CDI receipt is held, kinds
# with and without a canceller of their own, and one no registry knows.
KIND_NAMES = BLOCKED + ["vm-disk-import", "workload-copy", "vm-power", "no-such-kind"]
REFS = [
    {}, {"cdi_cleanup": True}, {"storage_protocol": 1}, {"storage_protocol": 2, "cdi_cleanup": True},
    {"plan": {"mode": "rollback", "undo": [1, "the claim"], "keeps": ["the data"], "severity": "high"}},
    {"plan": {"mode": "no-such-mode"}}, {"plan": {"mode": "stop", "action": "Stop copying"}},
    {"plan": {"can": False, "why_not": "the swap takes seconds", "undo": None}},
    {"storage_protocol": 1, "plan": {"mode": "rollback"}},
]


def cases():
    for number, (kind, ref) in enumerate(itertools.product(KIND_NAMES, REFS)):
        yield {"id": f"job-{number}", "kind": kind, "status": "running", "ref": dict(ref)}


def planned(item):
    return item["ref"].get("plan")


# A canceller for every kind but vm-power and the unknown one, so the plan a
# kind's own canceller gives is merged in wherever no refusal comes first.
CANCELLERS = {kind: (planned, lambda item, options: "") for kind in KIND_NAMES
              if kind not in ("vm-power", "no-such-kind")}


class SamePlanTests(unittest.TestCase):
    def setUp(self):
        for patch in (mock.patch.object(OPS, "CANCELLERS", CANCELLERS),
                      mock.patch.object(OPS, "CDI_CANCELLABLE", {"vm-disk-import"})):
            patch.start()
            self.addCleanup(patch.stop)

    def test_every_kind_and_ref_gives_the_plan_it_gave_before(self):
        count = 0
        for item in cases():
            with self.subTest(kind=item["kind"], ref=item["ref"]):
                self.assertEqual(reference_plan(item), OPS._plan_for(item))
            count += 1
        self.assertEqual(len(KIND_NAMES) * len(REFS), count)

    def test_the_matrix_reaches_every_kind_refusal(self):
        """Each kind with a refusal of its own changes at least one case's plan
        when its refusal is taken away, so the matrix would see a lost branch."""
        for kind in BLOCKED:
            with self.subTest(kind=kind), mock.patch.dict(OPS.CANCEL_BLOCKS, {kind: lambda item: ""}):
                self.assertTrue(any(reference_plan(item) != OPS._plan_for(item)
                                    for item in cases() if item["kind"] == kind))

    def test_a_refusal_comes_before_the_kinds_own_plan(self):
        item = {"id": "x", "kind": "reclass", "ref": {"storage_protocol": 1, "plan": {"mode": "rollback"}}}
        plan = OPS._plan_for(item)
        self.assertFalse(plan["can"])
        self.assertEqual("forget", plan["mode"])

    def test_the_import_receipt_comes_before_a_kinds_refusal(self):
        plan = OPS._plan_for({"id": "x", "kind": "snapshot-delete", "ref": {"cdi_cleanup": True}})
        self.assertIn("reclaiming its temporary storage", plan["why_not"])


class RegistryTests(unittest.TestCase):
    def test_every_kind_that_had_a_refusal_has_one_of_its_own(self):
        self.assertEqual(set(BLOCKED), set(OPS.CANCEL_BLOCKS))

    def test_a_kind_names_its_own_refusal(self):
        import server  # noqa: F401  (fills the registries, so every kind is there)
        found = KINDS.kinds()
        self.assertEqual(set(), set(OPS.CANCEL_BLOCKS) - set(found))
        for name, block in OPS.CANCEL_BLOCKS.items():
            with self.subTest(kind=name):
                self.assertIs(block, found[name].cancel_block)

    def test_a_kind_without_a_refusal_of_its_own_has_none(self):
        import types
        ops = types.SimpleNamespace(RESOLVERS={"scan": lambda item: None}, CANCELLERS={}, RESUMABLE={},
                                    LOGGERS={}, CLEANUPS=set(), CDI_CANCELLABLE=set(), VIEWS={},
                                    CANCEL_BLOCKS={}, CONFLICTS={}, _public=lambda item: item)
        self.assertIsNone(KINDS.kind("scan", ops).cancel_block)

    def test_a_refusal_registered_later_is_used(self):
        with mock.patch.dict(OPS.CANCEL_BLOCKS, {"late-kind": lambda item: "not now"}):
            plan = OPS._plan_for({"id": "x", "kind": "late-kind", "ref": {}})
        self.assertEqual((False, "not now"), (plan["can"], plan["why_not"]))


if __name__ == "__main__":
    unittest.main()
