"""No unused imports or unused local variables in server/ and tests/ (#747).

The repo has no linter: the server and its tests use the standard library
only. #402 removed what pyflakes found by hand, and the unused names came
back. An unused import keeps a module "used" after its last caller moved;
an unused local is often a typo, or a value computed and never returned.

This is pyflakes' F401 (an import never used) and F841 (a local variable
assigned and never used), found with ast in server/*.py and tests/*.py
(not scripts/ or tests/helpers/):
- An import is used when its file reads its name, or lists it in __all__,
  or when the import statement is marked `# noqa: F401` (or a bare
  `# noqa`), as pyflakes does: that is how a split module marks its
  re-exports, and a test the import it keeps for its side effect.
  It is also used when another file reaches it through the module: X.name
  after `import homestead_x as X`, `from homestead_x import name`,
  patch.object(X, "name"), or a patch target "homestead_x.name". In a file
  that calls globals(), a name some file writes as a string is used too
  (server.py hands names to modules that way). Reads are counted per file,
  so a local that shadows an import keeps the import "used".
- A local is a name a function assigns with = (or :=, or `except ... as`).
  It is used when the function, or a function, class or lambda inside it,
  reads it, or adds to it (x += 1). A name starting with _ is meant to be
  unused, and unpacking (a, b = ...) is not counted, as in pyflakes.

What is there today is listed below. The list may only shrink: an entry
that is fixed fails the test until it is removed, and a new finding is
fixed rather than listed.
"""
import ast
import re
import unittest
from collections import namedtuple

import source_scan as SS

FILES = sorted([*SS.SERVER.glob("*.py"), *(SS.ROOT / "tests").glob("*.py")])

SIDE_EFFECT = "may be imported for its side effect (what importing it binds or sets up): mark it # noqa: F401 or remove it"
UNUSED = "unused when this check was added: remove it"

# What is there today, "path: import name" or "path: local function.name",
# with why it stays. Only remove entries: a re-export or an import kept for
# its side effect is marked # noqa: F401, and anything else is removed.
ALLOWED = {
    "server/homestead_cancel.py: import LH": UNUSED,
    "server/homestead_cancel.py: import NAMES": UNUSED,
    "server/homestead_container_diagnostics.py: import ROUTER": UNUSED,
    "server/homestead_container_rebalance.py: import hashlib": UNUSED,
    "server/homestead_container_rebalance.py: import json": UNUSED,
    "server/homestead_imports.py: import COPY_CHECKS": UNUSED,
    "server/homestead_imports.py: import PASSTHROUGH": UNUSED,
    "server/homestead_imports.py: import RESOURCES": UNUSED,
    "server/homestead_imports.py: import SELF": UNUSED,
    "server/homestead_imports.py: import VMNETWORK": UNUSED,
    "server/homestead_imports.py: import base64": UNUSED,
    "server/homestead_imports.py: import hashlib": UNUSED,
    "server/homestead_place.py: import json": UNUSED,
    "server/homestead_placement_mode.py: import urllib": UNUSED,
    "server/homestead_push.py: import threading": UNUSED,
    "server/homestead_self_data_execute.py: import copy": UNUSED,
    "server/homestead_self_data_review.py: import digest": UNUSED,
    "server/server.py: import BATCH_CAPACITY": UNUSED,
    "server/server.py: import BaseHTTPRequestHandler": UNUSED,
    "server/server.py: import LOGSEARCH": UNUSED,
    "server/server.py: import PRIV": UNUSED,
    "server/server.py: import STORAGE_ADMISSION": UNUSED,
    "server/server.py: import STORAGE_RECOVERY": UNUSED,
    "server/server.py: import VM_CLAIMS": UNUSED,
    "server/server.py: import VM_POWER_RECOVERY": UNUSED,
    "server/server.py: import VM_PROFILES": UNUSED,
    "server/server.py: import _guarded_ksend": UNUSED,
    "server/server.py: import protection_issues": UNUSED,
    "tests/test_api_v1.py: import json": UNUSED,
    "tests/test_batch_capacity.py: import mock": UNUSED,
    "tests/test_dashboard_preferences.py: local DashboardPreferencesTests.test_node_width_and_display_settings_persist_with_the_account.saved": UNUSED,
    "tests/test_disk_v2.py: import json": UNUSED,
    "tests/test_edit_capacity.py: import review": UNUSED,
    "tests/test_file_manager.py: import time": UNUSED,
    "tests/test_fleet_relay.py: import time": UNUSED,
    "tests/test_forecast.py: import time": UNUSED,
    "tests/test_guest_readiness.py: import json": UNUSED,
    "tests/test_helm.py: import urllib": UNUSED,
    "tests/test_host_fixes.py: import urllib": UNUSED,
    "tests/test_hostrun_pods.py: import urllib": UNUSED,
    "tests/test_housekeeping.py: import server": SIDE_EFFECT,
    "tests/test_icons.py: import os": UNUSED,
    "tests/test_import_capacity.py: import urllib": UNUSED,
    "tests/test_job_cancel.py: import RESTRUCTURE": UNUSED,
    "tests/test_longhorn_writes.py: import json": UNUSED,
    "tests/test_node_removal_k3s.py: local K3sCluster.get.names": UNUSED,
    "tests/test_ports.py: import os": UNUSED,
    "tests/test_probe_sensors.py: import tempfile": UNUSED,
    "tests/test_reclass_review.py: import CLASSES": UNUSED,
    "tests/test_restructure.py: import copy": UNUSED,
    "tests/test_restructure.py: import urllib": UNUSED,
    "tests/test_seed_config.py: import urllib": UNUSED,
    "tests/test_self_address.py: import json": UNUSED,
    "tests/test_self_data_drain.py: import threading": UNUSED,
    "tests/test_self_data_recovery.py: local RecoveryTests.restored.before": UNUSED,
    "tests/test_smb_recovery.py: import server": SIDE_EFFECT,
    "tests/test_snapshot_files.py: import ast": UNUSED,
    "tests/test_storage_admission.py: import mock": UNUSED,
    "tests/test_storage_conflicts.py: import test_deploy_capacity": SIDE_EFFECT,
    "tests/test_storage_guard.py: import test_deploy_capacity": SIDE_EFFECT,
    "tests/test_storage_runtime.py: import test_deploy_capacity": SIDE_EFFECT,
    "tests/test_tunnel.py: local AccessTokenTests.test_everything_else_is_refused.other": UNUSED,
    "tests/test_vm_batch_journal.py: import test_vm_resources": SIDE_EFFECT,
    "tests/test_vm_cluster_admission.py: import urllib": UNUSED,
    "tests/test_vm_device_usage.py: import mock": UNUSED,
    "tests/test_vm_device_usage.py: import usage": UNUSED,
    "tests/test_vm_device_usage.py: local DeviceAdmissionTests.test_terminating_launcher_keeps_the_device_after_instance_completion.instance": UNUSED,
    "tests/test_vm_logos.py: import os": UNUSED,
    "tests/test_vm_mutation_job.py: import test_vm_resources": SIDE_EFFECT,
    "tests/test_vmstore.py: local StoreTests.test_variants_minimal_among_them_and_alpine_resolved.c": UNUSED,
}

Scan = namedtuple("Scan", "imports locals reads aliases through strings uses_globals")
PATCH_TARGET = re.compile(r"(\w+)\.(\w+)")
NOQA = re.compile(r"#\s*noqa(?::\s*(?P<codes>[A-Z][0-9]+(?:\s*,\s*[A-Z][0-9]+)*))?", re.I)


def _marked(lines, node):
    """Whether an import statement carries # noqa: F401, or a bare # noqa,
    on any of its lines."""
    for line in lines[node.lineno - 1:node.end_lineno]:
        for mark in NOQA.finditer(line):
            if mark["codes"] is None or "F401" in mark["codes"].upper().replace(" ", "").split(","):
                return True
    return False


class _Frame:
    def __init__(self, name):
        self.name, self.assigned, self.read, self.kept = name, {}, set(), set()


def scan(tree, lines=()):
    """One pass over a module: its imports (line, name), its unused locals
    (line, "function.name"), the names it reads, its module aliases, the
    (alias, name) pairs it reaches through a module, its strings, and
    whether it calls globals()."""
    imports, unused, reads, aliases, through, strings = [], [], set(), {}, set(), set()
    uses_globals = False
    frames, owners = [], []  # every scope entered; the functions among them
    stack = [(tree, "", False)]
    while stack:
        node, prefix, leaving = stack.pop()
        if leaving:
            frame = frames.pop()
            if frame:
                owners.pop()
                if owners:  # an inner function's reads are its outer function's too
                    owners[-1].read |= frame.read
            if frame and "locals" not in frame.read:
                unused += [(line, f"{frame.name}.{name}") for name, line in frame.assigned.items()
                           if name not in frame.read and name not in frame.kept and not name.startswith("_")]
            continue
        inner = frames[-1] if frames else None
        if isinstance(node, ast.Name):
            if not isinstance(node.ctx, ast.Store):
                reads.add(node.id)
                if owners:
                    owners[-1].read.add(node.id)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                bound = alias.asname or alias.name.split(".")[0]
                if not _marked(lines, node):
                    imports.append((node.lineno, bound))
                aliases[bound] = alias.name if alias.asname else bound
        elif isinstance(node, ast.ImportFrom):
            if node.module and node.level == 0:
                through |= {(node.module, alias.name) for alias in node.names}
                aliases.setdefault(node.module, node.module)
            if node.module != "__future__" and not _marked(lines, node):
                imports += [(node.lineno, alias.asname or alias.name) for alias in node.names if alias.name != "*"]
        elif isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
            through.add((node.value.id, node.attr))
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            strings.add(node.value)
        elif isinstance(node, ast.Call):
            if isinstance(node.func, ast.Name) and node.func.id == "globals":
                uses_globals = True
            if len(node.args) >= 2 and isinstance(node.args[0], ast.Name) \
                    and isinstance(node.args[1], ast.Constant) and isinstance(node.args[1].value, str):
                through.add((node.args[0].id, node.args[1].value))
        elif isinstance(node, ast.AugAssign) and isinstance(node.target, ast.Name):
            reads.add(node.target.id)
            if owners:
                owners[-1].read.add(node.target.id)
        elif isinstance(node, (ast.Global, ast.Nonlocal)) and inner:
            inner.kept |= set(node.names)
        elif isinstance(node, ast.Assign) and inner:
            for target in node.targets:
                if isinstance(target, ast.Name):
                    inner.assigned.setdefault(target.id, target.lineno)
        elif isinstance(node, (ast.AnnAssign, ast.NamedExpr)) and inner and isinstance(node.target, ast.Name) \
                and node.value is not None:
            inner.assigned.setdefault(node.target.id, node.target.lineno)
        elif isinstance(node, ast.ExceptHandler) and node.name and inner:
            inner.assigned.setdefault(node.name, node.lineno)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)):
            name = getattr(node, "name", "<lambda>")
            frames.append(None if isinstance(node, (ast.ClassDef, ast.Lambda)) else _Frame(prefix + name))
            if frames[-1]:
                owners.append(frames[-1])
            stack.append((node, prefix, True))
            prefix = prefix + name + "."
        stack.extend((child, prefix, False) for child in reversed(list(ast.iter_child_nodes(node))))
    exported = set()
    for node in tree.body:
        targets = node.targets if isinstance(node, ast.Assign) else [getattr(node, "target", None)]
        if any(isinstance(t, ast.Name) and t.id == "__all__" for t in targets) and getattr(node, "value", None):
            exported |= {c.value for c in ast.walk(node.value) if isinstance(c, ast.Constant)}
    return Scan(sorted(imports), sorted(unused), reads | exported, aliases, through, strings, uses_globals)


def findings(files=FILES):
    """{"path: import name" / "path: local function.name": "path:line"}.
    Each file is read through source_scan, but parsed by report(), not
    through source_scan's tree cache, so its ~660 trees are freed when the
    test is done with them (its text stays cached)."""
    return report((path.relative_to(SS.ROOT).as_posix(), path.stem, SS.text(path))
                  for path in files)


def report(modules):
    """findings() for (where, module name, source text) triples."""
    scans = {(where, module): scan(ast.parse(text), text.splitlines()) for where, module, text in modules}
    reached, strings = set(), set()
    for found in scans.values():
        reached |= {(found.aliases[alias], name) for alias, name in found.through if alias in found.aliases}
        reached |= {match.groups() for text in found.strings if (match := PATCH_TARGET.fullmatch(text))}
        strings |= found.strings
    result = {}
    for (where, module), found in scans.items():
        for line, name in found.imports:
            if name in found.reads or (module, name) in reached or found.uses_globals and name in strings:
                continue
            result.setdefault(f"{where}: import {name}", f"{where}:{line}")
        for line, name in found.locals:
            result.setdefault(f"{where}: local {name}", f"{where}:{line}")
    return result


def found_in(*sources):
    """(unused imports, unused locals) in the given modules, named m0, m1, …"""
    found = report((f"m{index}", f"m{index}", source) for index, source in enumerate(sources))
    return ([name.split("import ", 1)[1] for name in found if ": import " in name],
            [name.split("local ", 1)[1] for name in found if ": local " in name])


class Finder(unittest.TestCase):
    """What counts as unused."""

    def test_an_import_never_read_is_unused(self):
        imports, _ = found_in("import os\nimport json as j\nfrom a.b import c, d\nimport x.y\nprint(d)\n")
        self.assertEqual(["os", "j", "c", "x"], imports)

    def test_an_import_read_anywhere_or_listed_in_all_is_used(self):
        imports, _ = found_in("import os\nimport re\nimport sys\n__all__ = ['re']\n"
                              "def go():\n    return os.path\nclass K:\n    x = sys\n")
        self.assertEqual([], imports)

    def test_an_import_another_module_reads_through_this_one_is_used(self):
        imports, _ = found_in("import os\nimport re\nimport sys\nimport json\nfrom glob import glob\n",
                              "import m0 as M\nM.os.sep\npatch.object(M, 're')\npatch('m0.sys')\n")
        self.assertEqual(["json", "glob"], imports)

    def test_in_a_file_that_calls_globals_a_name_written_as_a_string_is_used(self):
        imports, _ = found_in("import os\nimport re\nfor name in NEEDS:\n    print(globals()[name])\n",
                              "NEEDS = ('os',)\n")
        self.assertEqual(["re"], imports)

    def test_an_import_marked_noqa_f401_or_bare_noqa_is_not_a_finding(self):
        imports, _ = found_in("import os  # noqa: F401  (tests reach it through here)\nimport re  # noqa\n"
                              "from json import (  # noqa: E402,F401\n    dumps,\n    loads,\n)\n"
                              "import sys  # noqa: E402\nimport glob  # noqa: E731\n")
        self.assertEqual(["sys", "glob"], imports)

    def test_a_name_another_module_imports_from_this_one_is_used(self):
        imports, _ = found_in("import os\nimport re\n", "from m0 import os\nprint(os)\n")
        self.assertEqual(["re"], imports)

    def test_a_future_import_is_not_a_finding(self):
        self.assertEqual([], found_in("from __future__ import annotations\n")[0])

    def test_an_import_inside_a_function_counts_too(self):
        self.assertEqual(["json"], found_in("def go():\n    import json\n    return 1\n")[0])

    def test_a_local_assigned_and_never_read_is_unused(self):
        _, local = found_in("def go(a):\n    total = a + 1\n    return a\n")
        self.assertEqual(["go.total"], local)

    def test_an_exception_name_never_read_is_unused(self):
        _, local = found_in("def go():\n    try:\n        f()\n    except ValueError as error:\n        pass\n")
        self.assertEqual(["go.error"], local)

    def test_a_local_read_later_or_in_an_inner_function_is_used(self):
        _, local = found_in("def go():\n    a = 1\n    b = 2\n    c = 3\n    c += 1\n"
                            "    def inner():\n        return a\n    return inner, (lambda: b)\n")
        self.assertEqual([], local)

    def test_underscore_names_unpacking_globals_and_locals_are_not_findings(self):
        _, local = found_in("X = 0\ndef go():\n    global X\n    X = 1\n    _ignored = 2\n    a, b = 3, 4\n\n"
                            "def keep():\n    kept = 1\n    return locals()\n")
        self.assertEqual([], local)

    def test_a_method_s_local_is_named_with_its_class(self):
        _, local = found_in("class H:\n    def get(self):\n        body = 1\n        return None\n")
        self.assertEqual(["H.get.body"], local)

    def test_an_inner_function_s_local_is_its_own(self):
        _, local = found_in("def outer():\n    def inner():\n        unused = 1\n    return inner\n")
        self.assertEqual(["outer.inner.unused"], local)


class Tree(unittest.TestCase):
    def test_server_and_tests_are_read(self):
        names = {path.name for path in FILES}
        self.assertIn("server.py", names)
        self.assertIn("test_unused_names.py", names)
        self.assertGreater(len(FILES), 500)

    @classmethod
    def setUpClass(cls):
        cls.found = findings()

    def test_no_new_unused_imports_or_locals(self):
        found = self.found
        new = sorted(set(found) - set(ALLOWED))
        self.assertEqual([], [f"{found[name]}: {name.split(': ', 1)[1]} is never used. Remove it; if it is a "
                              "re-export or imported for its side effect, mark it # noqa: F401." for name in new])

    def test_the_list_only_shrinks(self):
        stale = sorted(set(ALLOWED) - set(self.found))
        self.assertEqual([], [f"{name} is fixed: remove it from ALLOWED." for name in stale])


if __name__ == "__main__":
    unittest.main()
