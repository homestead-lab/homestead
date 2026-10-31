"""Each module registers its own kinds of job with register(ops), and
server.py only calls it (#622 step 3)."""
import os
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, os.environ.get("HOMESTEAD_TEST_SERVER_DIR", str(Path(__file__).resolve().parents[1] / "server")))
import homestead_host_bonds as HOST_BONDS
import homestead_host_bridge as HOST_BRIDGE
import homestead_host_console as HOST_CONSOLE
import homestead_host_os as HOST_OS
import homestead_k3scluster as K3SC
import homestead_os_rollout as OS_ROLLOUT
import homestead_power as POWER
import homestead_uplinks as UPLINKS
import homestead_components as COMPONENTS
import homestead_container_rebalance as CREBALANCE
import homestead_lhv2_setup as LHV2_SETUP
import homestead_longhorn as LH
import homestead_longhorn_jobs as JOBS
import homestead_move_engine as MOVE_ENGINE
import homestead_move_plan as PLAN
import homestead_rebalance as REBALANCE
import homestead_copy_move as COPY_MOVE
import homestead_host_move as HOST_MOVE
import homestead_shares as SHARES
import homestead_unraid_vms as UNRAID_VMS
import homestead_vm_mutation_job as VM_MUTATION_JOB
import homestead_addons as ADDONS
import homestead_autoupdate as AUTOUPDATE
import homestead_disk_v2 as DISK_V2
import homestead_disks as DISKS
import homestead_helm as HELM
import homestead_import_job as IMPORT_JOB
import homestead_kube as KUBE
import homestead_rename as RENAME
import homestead_restore_test as RESTORE_TEST
import homestead_weekly_trim as WEEKLY_TRIM
import homestead_vm_power_job as VM_POWER_JOB
import source_scan as SS
from unittest import mock

# module, the kinds it resolves, the kinds it cancels
HOSTS = [
    (HOST_OS, {"host-os": HOST_OS.status}, {"host-os"}),
    (OS_ROLLOUT, {"os-rollout": OS_ROLLOUT.status}, {}),
    (HOST_BRIDGE, {"host-bridge": HOST_BRIDGE.status}, {"host-bridge"}),
    (HOST_BONDS, {HOST_BONDS.KIND: HOST_BONDS.status}, {HOST_BONDS.KIND}),
    (UPLINKS, {UPLINKS.KIND: UPLINKS.status}, {}),
    (HOST_CONSOLE, {"host-console": HOST_CONSOLE.status}, {}),
    (K3SC, {"k3s-cluster": K3SC.status}, {}),
    (POWER, {"node-power": POWER.status}, {"node-power"}),
]
# Storage and app jobs (step 3, part 2)
STORAGE_APPS = [
    (REBALANCE, {REBALANCE.KIND: REBALANCE.status}, {REBALANCE.KIND}),
    (COPY_MOVE, {COPY_MOVE.KIND: COPY_MOVE.status}, {COPY_MOVE.KIND}),
    (HOST_MOVE, {HOST_MOVE.KIND: HOST_MOVE.status}, set()),
    (CREBALANCE, {CREBALANCE.KIND: CREBALANCE.status}, {CREBALANCE.KIND}),
    (COMPONENTS, {"platform-upgrade": COMPONENTS.status}, {"platform-upgrade"}),
    (VM_MUTATION_JOB, {"vm-create": VM_MUTATION_JOB.status, "vm-edit": VM_MUTATION_JOB.status}, {"vm-create", "vm-edit"}),
    (LH, {"protect-run": JOBS.run_status}, {}),
    (MOVE_ENGINE, {"move": PLAN.op_state}, {}),
    (LHV2_SETUP, {"longhorn-v2-prepare": LHV2_SETUP.progress, "longhorn-v2-cleanup": LHV2_SETUP.CLEANUP.progress}, {}),
    (SHARES, {"share-remove": SHARES.removal_progress}, {}),
    (UNRAID_VMS, {UNRAID_VMS.KIND: UNRAID_VMS.status}, {UNRAID_VMS.KIND}),
]
# The rest that need only ops or the Kubernetes client (step 3, part 3):
# module, the kinds it resolves, the kinds it cancels. Some resolvers are
# made in register(), so they are compared by where they were made.
REST = [
    (VM_POWER_JOB, {"vm-power"}, {"vm-power"}),
    (RENAME, {RENAME.KIND}, {RENAME.KIND}),
    (IMPORT_JOB, {IMPORT_JOB.KIND}, {IMPORT_JOB.KIND}),
    (DISK_V2, {DISK_V2.KIND}, {DISK_V2.KIND}),
    (DISKS, {"disk-retire"}, set()),
    (HELM, {"helm"}, set()),
    (ADDONS, {"multus"}, set()),
    (AUTOUPDATE, {AUTOUPDATE.KIND}, set()),
    (RESTORE_TEST, {RESTORE_TEST.KIND}, set()),
    (WEEKLY_TRIM, {WEEKLY_TRIM.KIND}, set()),
]
REST_ALIASES = {VM_POWER_JOB: "VM_POWER_JOB", RENAME: "RENAME", IMPORT_JOB: "IMPORT_JOB", DISK_V2: "DISK_V2",
                DISKS: "DISKS", HELM: "HELM", ADDONS: "ADDONS", AUTOUPDATE: "AUTOUPDATE", RESTORE_TEST: "RESTORE_TEST",
                WEEKLY_TRIM: "WEEKLY_TRIM"}
same = lambda functions: [(f.__module__, f.__qualname__) for f in functions]
ALIASES = {"homestead_host_os": "HOST_OS", "homestead_os_rollout": "OS_ROLLOUT", "homestead_host_bridge": "HOST_BRIDGE",
           "homestead_host_bonds": "HOST_BONDS", "homestead_uplinks": "UPLINKS", "homestead_host_console": "HOST_CONSOLE",
           "homestead_k3scluster": "K3SC", "homestead_power": "POWER", "homestead_rebalance": "REBALANCE", "homestead_copy_move": "COPY_MOVE", "homestead_host_move": "HOST_MOVE",
           "homestead_container_rebalance": "CREBALANCE", "homestead_components": "COMPONENTS",
           "homestead_vm_mutation_job": "VM_MUTATION_JOB", "homestead_longhorn": "LH", "homestead_move_engine": "MOVE_ENGINE",
           "homestead_lhv2_setup": "LHV2_SETUP", "homestead_shares": "SHARES", "homestead_unraid_vms": "UNRAID_VMS"}


def fake_ops():
    return SimpleNamespace(RESOLVERS={}, CANCELLERS={}, CANCEL_BLOCKS={}, RESUMABLE={}, LOGGERS={}, CLEANUPS=set(), CDI_CANCELLABLE=set())


class HostKindTests(unittest.TestCase):
    def test_v2_cleanup_cannot_discard_a_pending_memory_change(self):
        ops = fake_ops()
        LHV2_SETUP.register(ops)
        self.assertIn('restart', ops.CANCEL_BLOCKS['longhorn-v2-cleanup']({}))

    def test_each_host_module_registers_its_kinds(self):
        for module, resolves, cancels in HOSTS + STORAGE_APPS:
            with self.subTest(module=module.__name__):
                ops = fake_ops()
                module.register(ops)
                self.assertEqual(resolves, ops.RESOLVERS)
                self.assertEqual(set(cancels), set(ops.CANCELLERS))

    def test_a_node_power_command_cannot_be_cancelled_once_sent(self):
        ops = fake_ops()
        POWER.register(ops)
        plan, run = ops.CANCELLERS["node-power"]
        self.assertEqual({"can": False, "why_not": "A host power command cannot be cancelled after it has been sent"}, plan({}))
        self.assertEqual("", run({}, {}))

    def test_the_server_has_them_registered(self):
        import server
        for module, resolves, cancels in HOSTS + STORAGE_APPS:
            for kind, resolve in resolves.items():
                self.assertIs(resolve, server.OPS.RESOLVERS[kind], kind)
            for kind in cancels:
                ops = fake_ops()
                module.register(ops)
                # The same functions: by name, since a lambda is made afresh each time.
                same = lambda pair: [(f.__module__, f.__qualname__) for f in pair]
                self.assertEqual(same(ops.CANCELLERS[kind]), same(server.OPS.CANCELLERS[kind]), kind)

    def test_server_py_no_longer_registers_them_itself(self):
        text = SS.text(SS.SERVER_PY)
        for module, resolves, _ in HOSTS + STORAGE_APPS:
            alias = ALIASES[module.__name__]
            for resolve in set(resolves.values()):
                self.assertFalse(f"= {alias}.{resolve.__name__}\n" in text, alias)
            self.assertFalse(f"= ({alias}.cancel_plan, {alias}.cancel_run)" in text, alias)

    def test_an_unraid_import_can_be_cancelled_with_its_cdi_cleanup(self):
        ops = fake_ops()
        UNRAID_VMS.register(ops)
        self.assertEqual({UNRAID_VMS.KIND}, ops.CDI_CANCELLABLE)



class RestKindTests(unittest.TestCase):
    def test_each_module_registers_its_kinds(self):
        for module, resolves, cancels in REST:
            with self.subTest(module=module.__name__):
                ops = fake_ops()
                module.register(ops)
                self.assertEqual(resolves, set(ops.RESOLVERS))
                self.assertEqual(cancels, set(ops.CANCELLERS))

    def test_what_else_they_register(self):
        ops = fake_ops()
        for module in (RENAME, DISK_V2, DISKS):
            module.register(ops)
        self.assertEqual({RENAME.KIND}, ops.CLEANUPS)
        self.assertEqual({DISK_V2.KIND: DISK_V2.logs}, ops.LOGGERS)
        self.assertEqual({"disk-retire": DISKS.retire_resumable}, ops.RESUMABLE)
        self.assertIs(DISK_V2.progress, ops.RESOLVERS[DISK_V2.KIND])
        self.assertIs(DISKS.retire_step, ops.RESOLVERS["disk-retire"])

    def test_they_pass_what_server_py_passed(self):
        ops = fake_ops()
        ops.checkpoint = object()
        for module, _, _ in REST:
            module.register(ops)
        item, read = {"id": "j1", "ref": {}}, object()
        with mock.patch.object(KUBE, "kget", read):
            for module, name, kind, args in [
                    (VM_POWER_JOB, "status", "vm-power", (item, read)),
                    (IMPORT_JOB, "status", IMPORT_JOB.KIND, (item, read)),
                    (RENAME, "status", RENAME.KIND, (item, ops)),
                    (AUTOUPDATE, "resolve", AUTOUPDATE.KIND, (item, ops.checkpoint)),
                    (RESTORE_TEST, "resolve", RESTORE_TEST.KIND, (item, ops.checkpoint)),
                    (WEEKLY_TRIM, "resolve", WEEKLY_TRIM.KIND, (item, ops.checkpoint))]:
                with self.subTest(kind=kind), mock.patch.object(module, name) as called:
                    ops.RESOLVERS[kind](item)
                    called.assert_called_once_with(*args)
            with mock.patch.object(RENAME, "recovery_plan") as plan, mock.patch.object(RENAME, "recovery_run") as run:
                ops.CANCELLERS[RENAME.KIND][0](item)
                ops.CANCELLERS[RENAME.KIND][1](item, {"x": 1})
            plan.assert_called_once_with(item, read, ops)
            run.assert_called_once_with(item, {"x": 1}, read, ops)

    def test_the_server_has_them_registered(self):
        import server
        for module, resolves, cancels in REST:
            ops = fake_ops()
            module.register(ops)
            for kind in resolves:
                self.assertEqual(same([ops.RESOLVERS[kind]]), same([server.OPS.RESOLVERS[kind]]), kind)
            for kind in cancels:
                self.assertEqual(same(ops.CANCELLERS[kind]), same(server.OPS.CANCELLERS[kind]), kind)
        self.assertIn(RENAME.KIND, server.OPS.CLEANUPS)
        self.assertIs(DISK_V2.logs, server.OPS.LOGGERS[DISK_V2.KIND])
        self.assertIs(DISKS.retire_resumable, server.OPS.RESUMABLE["disk-retire"])

    def test_server_py_no_longer_registers_them_itself(self):
        lines = SS.text(SS.SERVER_PY).splitlines()
        for module, alias in REST_ALIASES.items():
            self.assertEqual([], [line for line in lines if line.startswith("OPS.") and f"{alias}." in line], alias)


class LastKindTests(unittest.TestCase):
    """The kinds whose resolvers live with server.py's feature code (step 3,
    part 4): storage, cluster and self-data each register their own."""

    @classmethod
    def setUpClass(cls):
        import server
        cls.server = server
        cls.modules = {"storage": server.STORAGE_ROUTES, "cluster": server.CLUSTER_ROUTES,
                       "self-data": server.SELF_DATA_ROUTES}

    def registered(self, *names):
        ops = fake_ops()
        for name in names:
            self.modules[name].register(ops)
        return ops

    def test_each_module_registers_its_kinds(self):
        copy = self.server.COPY_JOB.KIND
        prepare, execute = self.server.SELF_DATA_PREPARE.KIND, self.server.SELF_DATA_EXECUTE.KIND
        for name, resolves, cancels, resumable in [
                ("storage", {"volume-restore", "restructure", copy, "reclass", "snapshot-revert", "snapshot-delete", "restore-swap"},
                 {copy, "reclass", "snapshot-revert", "restore-swap"}, {"volume-restore", "reclass", "snapshot-delete"}),
                ("cluster", {"cluster-shutdown", "harvester-upgrade", "onboard", "cluster-cleanup"}, set(), set()),
                ("self-data", {prepare, execute, "self-data-move"}, {prepare}, set())]:
            with self.subTest(module=name):
                ops = self.registered(name)
                self.assertEqual(resolves, set(ops.RESOLVERS))
                self.assertEqual(cancels, set(ops.CANCELLERS))
                self.assertEqual(resumable, set(ops.RESUMABLE))
                self.assertEqual({copy, "restructure"} if name == "storage" else set(), ops.CLEANUPS)

    def test_they_register_the_functions_server_py_did(self):
        s, ops = self.server, self.registered("storage", "cluster", "self-data")
        self.assertIs(s.STORAGE_ROUTES._restore_then_tidy, ops.RESOLVERS["volume-restore"])
        self.assertIs(s.COPY_JOB.legacy_status, ops.RESOLVERS["restructure"])
        self.assertIs(s.STORAGE_ROUTES.storage_move_progress, ops.RESOLVERS["reclass"])
        self.assertIs(s.RECLASS.resumable, ops.RESUMABLE["reclass"])
        self.assertIs(s.SNAPSHOT_DELETE.resumable, ops.RESUMABLE["snapshot-delete"])
        self.assertEqual((s.CANCEL.reclass_plan, s.STORAGE_ROUTES.storage_legacy_cancel), ops.CANCELLERS["reclass"])
        self.assertIs(s.REVERT.cancel_plan, ops.CANCELLERS["snapshot-revert"][0])
        self.assertIs(s.CLUSTER_ROUTES._harvester_upgrade_status, ops.RESOLVERS["harvester-upgrade"])
        prepare = s.SELF_DATA_PREPARE
        self.assertIs(s.SELF_DATA_ROUTES.self_data_preparation_progress, ops.RESOLVERS[prepare.KIND])
        self.assertEqual((prepare.cancel_plan, prepare.cancel_run), ops.CANCELLERS[prepare.KIND])
        self.assertIs(s.SELF_DATA_ROUTES._data_move_status, ops.RESOLVERS["self-data-move"])
        restore = ops.RESUMABLE["volume-restore"]
        self.assertTrue(restore({"ref": {}}))
        self.assertFalse(restore({"ref": {"restore_config": {"x": 1}}}))

    def test_they_pass_what_server_py_passed(self):
        s, ops = self.server, self.registered("storage", "cluster", "self-data")
        item, read, send = {"id": "j1", "kind": "x", "ref": {}}, mock.Mock(), mock.Mock()
        copy = s.COPY_JOB
        with mock.patch.object(KUBE, "kget", read), mock.patch.object(KUBE, "ksend", send):
            with mock.patch.object(copy, "resolve") as resolve, \
                    mock.patch.object(s, "copy_admission", return_value="admitted") as admission:
                ops.RESOLVERS[copy.KIND](item)
                (got_item, got_read, got_send, got_ops, admit), = [c.args for c in resolve.call_args_list]
                self.assertEqual((item, read, send, ops), (got_item, got_read, got_send, got_ops))
                self.assertEqual("admitted", admit({"manifest": 1}))
                admission.assert_called_once_with({"manifest": 1})
            with mock.patch.object(copy, "recovery_plan") as plan, mock.patch.object(copy, "recovery_run") as run:
                ops.CANCELLERS[copy.KIND][0](item)
                ops.CANCELLERS[copy.KIND][1](item, {"o": 1})
            plan.assert_called_once_with(item, read, ops)
            run.assert_called_once_with(item, {"o": 1}, read, send, ops)
            with mock.patch.object(s.SELF_DATA_EXECUTE, "resolve") as resolve:
                ops.RESOLVERS[s.SELF_DATA_EXECUTE.KIND](item)
            resolve.assert_called_once_with(item, read, directory=s.DATA_DIR)
        shutdown = mock.Mock()
        with mock.patch.object(s.CLUSTER_ROUTES, "cluster_shutdown", return_value=shutdown):
            ops.RESOLVERS["cluster-shutdown"](item)
        shutdown.progress.assert_called_once_with(item)
        self.assertEqual(("cancelled", 40, "Join plans were replaced by the install guide"),
                         ops.RESOLVERS["onboard"]({"progress": 40}))
        self.assertEqual(("succeeded", 100, "Removed n1"), ops.RESOLVERS["cluster-cleanup"]({"message": "Removed n1"}))

    def test_the_server_has_them_registered(self):
        s, ops = self.server, self.registered("storage", "cluster", "self-data")
        for kind in ops.RESOLVERS:
            self.assertEqual(same([ops.RESOLVERS[kind]]), same([s.OPS.RESOLVERS[kind]]), kind)
        for kind in ops.CANCELLERS:
            self.assertEqual(same(ops.CANCELLERS[kind]), same(s.OPS.CANCELLERS[kind]), kind)
        for kind in ops.RESUMABLE:
            self.assertEqual(same([ops.RESUMABLE[kind]]), same([s.OPS.RESUMABLE[kind]]), kind)

    def test_server_py_registers_no_kind_itself_any_more(self):
        lines = SS.text(SS.SERVER_PY).splitlines()
        self.assertEqual([], [line for line in lines
                              if line.startswith(("OPS.RESOLVERS", "OPS.CANCELLERS", "OPS.RESUMABLE",
                                                  "OPS.LOGGERS", "OPS.CLEANUPS", "OPS.CDI_CANCELLABLE"))])


if __name__ == "__main__":
    unittest.main()
