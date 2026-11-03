"""Every write to a Longhorn disk passes the disk guards (#1127's registry),
except the retire job's own steps and its cancel (#TBD-disk-writes-guarded).

The rule: a function in homestead_disks or homestead_cancel that writes a
Longhorn node, disk or Harvester block device runs under @_guarded, or is a
helper only guarded functions call, or is one of EXEMPT. The retire job's
steps and cancel are exempt on purpose: retire_start passed the guards, and
while a retire runs none of the guarded jobs (a V2 preparation, a storage
allocation, Longhorn runtime maintenance) can start on that host
(homestead_storage_conflicts and homestead_longhorn_runtime refuse), so a
guard could only fail a half-done retire or stop its cancel from putting
scheduling back. The second test pins that refusal, which the exemption
relies on.
"""
import ast
import sys
import types
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "server"))
import homestead_disks as DISKS
import homestead_longhorn_runtime as RUNTIME
import homestead_storage_conflicts as CONFLICTS

# name: why it may write without the guards
EXEMPT = {
    ("homestead_disks", "retire_step"): "the retire job's own steps; its start was guarded and it holds the host",
    ("homestead_cancel", "disk_retire_cancel"): "puts scheduling back on the disk the retire job holds",
}
# Writes that are not to a Longhorn disk: a Kubernetes Node annotation (a disk's name).
NOT_DISKS = ("/api/v1/nodes",)


def _writes(fn):
    """Whether fn calls _patch/ksend on a Longhorn or block-device path."""
    for node in ast.walk(fn):
        if not isinstance(node, ast.Call):
            continue
        name = node.func.attr if isinstance(node.func, ast.Attribute) else getattr(node.func, "id", "")
        if name not in ("_patch", "ksend") or not node.args:
            continue
        text = ast.unparse(node.args[0] if name == "_patch" else (node.args[1] if len(node.args) > 1 else node.args[0]))
        if any(path in text for path in NOT_DISKS):
            continue
        if "LH" in text or "BD" in text:
            return True
    return False


def _guarded(fn):
    return any(isinstance(d, ast.Call) and getattr(d.func, "id", "") == "_guarded" for d in fn.decorator_list)


class DiskWritesGuardedTests(unittest.TestCase):
    def test_every_longhorn_disk_write_is_guarded_or_named_exempt(self):
        found = []
        for module in ("homestead_disks", "homestead_cancel"):
            tree = ast.parse((ROOT / "server" / f"{module}.py").read_text())
            functions = {f.name: f for f in tree.body if isinstance(f, ast.FunctionDef)}
            callers = {name: {other for other, f in functions.items() for n in ast.walk(f)
                              if isinstance(n, ast.Call) and getattr(n.func, "id", "") == name}
                       for name in functions}
            for name, fn in functions.items():
                if name == "_patch" or not _writes(fn) or _guarded(fn) or (module, name) in EXEMPT:
                    continue
                # A helper is fine when every function that calls it is guarded.
                if callers[name] and all(_guarded(functions[c]) for c in callers[name]):
                    continue
                found.append(f"{module}.{name}")
        self.assertEqual([], found, "a Longhorn disk write that skips the disk guards: decorate it with "
                                    "@_guarded, or name it in EXEMPT with why")

    def test_the_exempt_writes_are_still_there(self):
        # A name that no longer exists, or no longer writes, is dropped from EXEMPT.
        for module, name in EXEMPT:
            tree = ast.parse((ROOT / "server" / f"{module}.py").read_text())
            fn = next((f for f in tree.body if isinstance(f, ast.FunctionDef) and f.name == name), None)
            self.assertIsNotNone(fn, name)
            self.assertTrue(_writes(fn), name)

    def test_while_a_retire_runs_no_guarded_job_can_start_on_its_host(self):
        retire = {"id": "r", "kind": "disk-retire", "status": "running", "ref": {"node": "node-a", "disk": "data"}}
        for kind in ("disk-v2-convert", "disk-allocation"):
            with self.subTest(kind=kind), self.assertRaises(ValueError):
                CONFLICTS.require_clear([retire], kind, {"node": "node-a"})
        with mock.patch.object(RUNTIME, "ops", types.SimpleNamespace(stored=lambda: [retire], TERMINAL=("succeeded",))), \
                self.assertRaises(ValueError):
            RUNTIME._conflicts("node-a")

    def test_and_a_retire_cannot_start_past_the_guards(self):
        self.assertTrue(getattr(DISKS.retire_start, "__wrapped__", None), "retire_start runs under @_guarded")


if __name__ == "__main__":
    unittest.main()
