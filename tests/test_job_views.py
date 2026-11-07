"""What the job tray is sent for each kind of job, before and after the kind
branches of homestead_operations._public moved into per-kind views (#622).

_public used to hold one `if item.get("kind") == ...` branch per kind. Each
kind's part is now a function in OPS.VIEWS, and the registry in
homestead_job_kinds names it. reference_view below is _public as it stood
before the move, plus helm's branch, marked where it was added: helm had no view
then. Every case of the matrix must give the same answer from both. Once each kind's view has tests of its own, the reference and the
matrix can go."""
import itertools
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "server"))

import homestead_job_kinds as KINDS
import homestead_operations as OPS
from homestead_operations import CANCELLING, TERMINAL, _cancel_stale, _cleanable, _dismissible

NOW = 2_000_000_000.0


def reference_view(item):
    """homestead_operations._public before its kind branches moved (#622)."""
    out = {key: value for key, value in item.items()
           if key not in ("ref", "cancel_started", "previous_status", "history", "_legacy_store")}
    out["cancellable"] = item.get("kind") != "self-data-handoff" and item.get("status") not in TERMINAL and (
        item.get("status") != CANCELLING or _cancel_stale(item))
    if item.get("ref", {}).get("cdi_cleanup") and item.get("kind") not in OPS.CDI_CANCELLABLE:
        out["cancellable"] = False
    out["cleanable"] = _cleanable(item)
    out["dismissible"] = _dismissible(item)
    if item.get("kind") == "disk-v2-convert":
        out["disk_v2_preparation"] = True
        out["cancellable"] = out["cancellable"] and item.get("ref", {}).get("phase") in ("evacuating", "awaiting-erase", "cancelled")
        out["cleanable"] = False
    if item.get("kind") == "disk-allocation":
        out.update(cancellable=False, cleanable=False, allocation=True)
    if item.get("kind") == "longhorn-runtime":
        out.update(cancellable=False, cleanable=False, runtime_maintenance=True)
    if item.get("kind") == "helm" and out["cleanable"]:
        # Added with the put-back on a failed upgrade: only that, only with its record.
        ref = item.get("ref", {})
        out["cleanable"] = ref.get("action") == "upgrade" and bool(ref.get("chart_uid")) and not ref.get("chart_gone")
    if item.get("kind") == "k3s-cluster":
        out["tracking_only"] = True
    if item.get("kind") == "auto-update":
        ref = item.get("ref") or {}
        out["auto_update"] = {"namespace": ref.get("namespace", ""), "name": ref.get("name", ""),
                              "phase": ref.get("phase", ""), "snapshots": ref.get("snapshots") or []}
    if item.get("kind") == "node-power":
        ref = item.get("ref") or {}
        out["power"] = {"phase": ref.get("phase", ""), "action": ref.get("action", ""), "node": ref.get("node", ""),
                        "direct": bool(ref.get("planned_outage") or ref.get("forced")),
                        "planned_outage": bool(ref.get("planned_outage")),
                        "handoff": bool(ref.get("handoff") and ref.get("helper_uid")),
                        "failed_phase": ref.get("failed_phase", ""),
                        "holds": bool(ref.get("held")) or any(
                            choice == "wait" or (choice == "move" and key.startswith("VirtualMachine/"))
                            for key, choice in (ref.get("choices") or {}).items()),
                        "held": len(ref.get("held") or []),
                        "waiting": bool(ref.get("held")) and ref.get("restored") is None,
                        "restored": ref.get("restored")}
    if item.get("kind") == "set-restart":
        ref = item.get("ref") or {}
        out["cancellable"] = False
        out["set_restart"] = {"node": ref.get("node", ""), "set": ref.get("name", ""),
                              "started_by": ref.get("started_by", ""), "stage": ref.get("stage", "")}
    if item.get("kind") == "vm-power":
        out["cancellable"] = out["cancellable"] and item.get("ref", {}).get("phase") in ("prepared", "accepted")
        out["power_recovery"] = item.get("status") not in TERMINAL and item.get("ref", {}).get("phase") in ("uncertain", "dispatching")
    if item.get("kind") in ("vm-create", "vm-edit", "import-create"):
        out["cancellable"] = False
        out["mutation_recovery"] = bool(item.get("ref", {}).get("retain_resources"))
    if item.get("kind") == "k3s-cluster" and item.get("ref", {}).get("dispatch_protocol") == 2:
        out["cancellable"] = out["cleanable"] = False
        out["mutation_recovery"] = bool(item.get("ref", {}).get("retain_resources"))
        out["batch_name"] = item["ref"]["name"]
    if item.get("kind") == "snapshot-delete":
        out["cancellable"] = False
    if item.get("kind") in ("self-data-prepare", "host-console", "cluster-shutdown", "longhorn-v2-prepare"):
        out["cancellable"] = out["cleanable"] = False
    if item.get("kind") == "self-data-prepare":
        from homestead_self_data_prepare import can_archive
        out["preparation_archivable"] = can_archive(item)
    if item.get("kind") == "workload-rename":
        out["tracking_only"] = True
        out["rename_recovery"] = True
    if item.get("kind") in ("workload-copy", "restructure"):
        out["tracking_only"] = True
        out["copy_recovery"] = True
    if item.get("kind") == "k3s-cluster" and item.get("ref", {}).get("retain_resources") and item["ref"].get("phase") == "provisioning":
        out["cancellable"] = False
    check = OPS.RESUMABLE.get(item.get("kind"))
    if item.get("status") == "failed" and check:
        try:
            out["resumable"] = not check(item)
        except Exception:
            out["resumable"] = False
    if item.get("kind") == "reclass" and "storage_protocol" in item.get("ref", {}):
        out.update(cancellable=False, cleanable=False, resumable=False,
                   storage_recovery=item["ref"]["storage_protocol"] == 1 and item["ref"].get("handoff_phase") != "done")
    # Added after #622, for the three disk job kinds a removed host can strand (homestead_host_gone).
    if item.get("kind") in ("disk-v2-convert", "disk-allocation", "longhorn-runtime"):
        out["host_gone_endable"] = OPS.STORAGE_CONFLICTS.unresolved(item)
    return out


BRANCHED = ["disk-v2-convert", "disk-allocation", "longhorn-runtime", "k3s-cluster", "auto-update", "node-power", "set-restart", "vm-power",
            "vm-create", "vm-edit", "import-create", "self-data-prepare", "host-console",
            "cluster-shutdown", "longhorn-v2-prepare", "workload-rename", "workload-copy", "restructure",
            "reclass", "helm"]
# snapshot-delete's branch only said "not cancellable"; its refusal in CANCEL_BLOCKS
# says that now, for the tray as for cancelling.
KIND_NAMES = BRANCHED + ["snapshot-delete", "self-data-handoff", "vm-disk-import", "workload-edit", "no-such-kind"]
STATUSES = ["queued", "running", CANCELLING, "failed", "succeeded", "cancelled"]
REFS = [
    {},
    {"phase": "evacuating"}, {"phase": "awaiting-erase"}, {"phase": "cancelled"},
    {"phase": "prepared"}, {"phase": "accepted"}, {"phase": "uncertain"}, {"phase": "dispatching"},
    {"phase": "provisioning", "retain_resources": True}, {"retain_resources": True},
    {"dispatch_protocol": 2, "name": "batch-a", "retain_resources": True},
    {"dispatch_protocol": 2, "name": "batch-b", "phase": "provisioning"},
    {"cdi_cleanup": True}, {"storage_protocol": 1, "handoff_phase": "copying"},
    {"storage_protocol": 1, "handoff_phase": "done"}, {"storage_protocol": 2},
    {"held": ["Deployment/web"], "restored": None, "action": "reboot", "node": "node-a",
     "planned_outage": True, "failed_phase": "drain",
     "choices": {"VirtualMachine/vm-a": "move", "Deployment/web": "wait"}},
    {"held": ["Deployment/web"], "restored": 1, "forced": True, "choices": {"Deployment/db": "move"}},
    {"namespace": "apps", "name": "web", "snapshots": ["snap-1"], "phase": "snapshot"},
    {"node": "node-a", "name": "db", "started_by": "admin", "stage": "delete"},
    {"prepared": True, "retain_resources": False},
    {"action": "upgrade", "chart_uid": "uid-a"}, {"action": "upgrade", "chart_uid": "uid-a", "chart_gone": True},
    {"action": "upgrade"}, {"action": "uninstall", "chart_uid": "uid-a"},
]
EXTRAS = [{}, {"cancel_started": NOW}, {"cleaned": True, "tracking_stopped": True}]


def cases():
    for number, (kind, status, ref, extra) in enumerate(itertools.product(KIND_NAMES, STATUSES, REFS, EXTRAS)):
        yield {"id": f"job-{number}", "kind": kind, "title": kind, "status": status, "progress": 10,
               "message": "", "resource": {"kind": "Volume", "name": "v", "namespace": "apps"},
               "href": "/", "ref": dict(ref), "history": [], "started_at": "", "updated_at": "",
               "finished_at": "", **extra}


def unresumable(item):
    return "it moved on" if item.get("ref", {}).get("phase") else ""


def broken(item):
    raise RuntimeError("check failed")


class SameViewTests(unittest.TestCase):
    def setUp(self):
        for patch in (mock.patch.object(OPS.time, "time", return_value=NOW),
                      mock.patch.object(OPS, "RESUMABLE", {"reclass": unresumable, "vm-power": unresumable,
                                                           "k3s-cluster": lambda item: "",
                                                           "node-power": broken}),
                      mock.patch.object(OPS, "CLEANUPS", {"k3s-cluster", "workload-copy", "restructure",
                                                          "workload-rename", "vm-create", "reclass",
                                                          "disk-v2-convert", "host-console",
                                                          # so longhorn-v2-prepare's "never cleanable" shows
                                                          "longhorn-v2-prepare", "helm"}),
                      mock.patch.object(OPS, "CDI_CANCELLABLE", {"vm-disk-import"})):
            patch.start()
            self.addCleanup(patch.stop)

    def test_every_kind_status_and_ref_gives_the_view_it_gave_before(self):
        count = 0
        for item in cases():
            with self.subTest(kind=item["kind"], status=item["status"], ref=item["ref"],
                              cancel_started="cancel_started" in item, cleaned="cleaned" in item):
                self.assertEqual(reference_view(item), OPS._public(item))
            count += 1
        self.assertGreater(count, 2000)

    def test_the_matrix_reaches_every_kind_branch(self):
        """Each kind with a view of its own changes at least one case's answer
        from the shared part alone, so the matrix would see a lost branch."""
        for kind in BRANCHED:
            with self.subTest(kind=kind), mock.patch.dict(OPS.VIEWS, {kind: lambda item, out: None}):
                self.assertTrue(any(reference_view(item) != OPS._public(item)
                                    for item in cases() if item["kind"] == kind))


class RegistryTests(unittest.TestCase):
    def test_every_kind_that_had_a_branch_has_a_view_of_its_own(self):
        self.assertEqual(set(BRANCHED), set(OPS.VIEWS))

    def test_a_kind_names_its_own_view(self):
        import server  # noqa: F401  (fills the registries, so every kind is there)
        found = KINDS.kinds()
        self.assertEqual(set(), set(OPS.VIEWS) - set(found))
        for name, fields in OPS.VIEWS.items():
            with self.subTest(kind=name):
                self.assertIs(fields, found[name].fields)

    def test_a_kind_without_fields_of_its_own_has_none(self):
        import types
        ops = types.SimpleNamespace(RESOLVERS={"scan": lambda item: None}, CANCELLERS={}, RESUMABLE={},
                                    LOGGERS={}, CLEANUPS=set(), CDI_CANCELLABLE=set(), VIEWS={}, CANCEL_BLOCKS={}, CONFLICTS={},
                                    _public=lambda item: item)
        self.assertIsNone(KINDS.kind("scan", ops).fields)

    def test_a_view_registered_later_is_used(self):
        with mock.patch.dict(OPS.VIEWS, {"late-kind": lambda item, out: out.update(late=True)}):
            self.assertTrue(OPS._public({"id": "x", "kind": "late-kind", "status": "running"})["late"])


if __name__ == "__main__":
    unittest.main()
