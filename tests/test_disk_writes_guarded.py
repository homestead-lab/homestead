"""Every write to a Longhorn disk passes the disk guards (#1127's registry),
except the writes named in EXEMPT, each with why (#TBD-disk-writes-guarded).

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

A second scan covers every server/ module: a function that writes a Longhorn
node (where Longhorn keeps a host's disks) or a Harvester block device is in
homestead_disks under the guards, or named in EXEMPT.

What the scans see, and do not: a call to _patch, ksend, send or patch whose
path argument - or a local variable holding it, assigned in the same function
- names LH/BD, longhorn.io nodes or blockdevices. A write inside a nested
function or lambda counts for the function around it. They miss a path passed
in as a parameter, a module-level path constant, and a write through any other
client call.
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
    ("homestead_root_guard", "_schedule"): "stops placement on a root-filesystem folder disk that is nearly full, "
                                          "and allows it again only on one it stopped itself",
    ("homestead_disk_v2", "_patch_disk"): "the V2 preparation job's own steps, which its saved task (the disk_v2 guard) protects",
    ("homestead_disk_v2", "_register"): "the V2 preparation job's own steps, which its saved task (the disk_v2 guard) protects",
    ("homestead_onboard", "remove_node"): "removing a host: stops Longhorn scheduling to the whole host, under its own reviewed plan",
    ("homestead_onboard", "cleanup"): "deletes Longhorn's record of a host that is gone",
}
# Writes that are not to a Longhorn disk: a Kubernetes Node annotation (a disk's name).
NOT_DISKS = ("/api/v1/nodes",)


def _paths(fn, wanted):
    """The paths fn writes that wanted(text) picks: the path argument of each
    _patch/ksend/send/patch call that is not a GET, or the local variable's value."""
    local = {n.targets[0].id: ast.unparse(n.value) for n in ast.walk(fn)
             if isinstance(n, ast.Assign) and len(n.targets) == 1 and isinstance(n.targets[0], ast.Name)}
    found = []
    for node in ast.walk(fn):
        if not isinstance(node, ast.Call):
            continue
        name = node.func.attr if isinstance(node.func, ast.Attribute) else getattr(node.func, "id", "")
        if name not in ("_patch", "ksend", "send", "patch") or not node.args:
            continue
        args = [ast.unparse(a) for a in node.args[:2]]
        if name in ("ksend", "send") and len(args) > 1:
            if args[0].strip("'\"") == "GET":
                continue
            path = args[1]
        else:
            path = args[0]
        text = local.get(path, path)
        if not any(p in text for p in NOT_DISKS) and wanted(text):
            found.append(text)
    return found


def _writes(fn):
    """Whether fn writes a Longhorn object or a block device (homestead_disks, homestead_cancel)."""
    return bool(_paths(fn, lambda text: "LH" in text or "BD" in text))


def _writes_disks(fn):
    """Whether fn writes a Longhorn node or a Harvester block device (any module)."""
    return bool(_paths(fn, lambda text: ("/nodes" in text and ("LH" in text or "longhorn.io" in text))
                       or "{BD}" in text or "blockdevices" in text))


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

    def test_no_other_module_writes_a_longhorn_disk_past_the_guards(self):
        found = []
        for path in sorted((ROOT / "server").glob("*.py")):
            module = path.stem
            if module in ("homestead_disks",):
                continue                     # the first test: under @_guarded, or a guarded helper
            tree = ast.parse(path.read_text())
            for fn in tree.body:
                if isinstance(fn, ast.FunctionDef) and (module, fn.name) not in EXEMPT and _writes_disks(fn):
                    found.append(f"{module}.{fn.name}")
        self.assertEqual([], found, "a Longhorn node or block-device write outside homestead_disks' guards: "
                                    "route it through homestead_disks, or name it in EXEMPT with why")

    def test_the_exempt_writes_are_still_there(self):
        # A name that no longer exists, or no longer writes, is dropped from EXEMPT.
        for module, name in EXEMPT:
            tree = ast.parse((ROOT / "server" / f"{module}.py").read_text())
            fn = next((f for f in tree.body if isinstance(f, ast.FunctionDef) and f.name == name), None)
            self.assertIsNotNone(fn, name)
            self.assertTrue(_writes(fn) or _writes_disks(fn), name)

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
