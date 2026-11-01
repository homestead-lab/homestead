"""What each kind of job is, read in one place, and a guard that no kind is
half-registered: one the store can cancel but not advance, or a branch, a
page or a start for a kind nothing resolves.

Today a kind is spread over OPS.RESOLVERS/CANCELLERS/RESUMABLE/LOGGERS/
CLEANUPS/CDI_CANCELLABLE - seeded by homestead_operations, filled by
server.py, homestead_cancel.register, homestead_joblogs.register and a few
modules at import or in bind() - and over the kind branches in
homestead_operations (#622). The registry reads those as they are."""
import ast
import dataclasses
import importlib
import re
import sys
import types
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "server"))

import homestead_job_kinds as KINDS
import source_scan as SS


def stand_in(**dicts):
    ops = types.SimpleNamespace(RESOLVERS={}, CANCELLERS={}, RESUMABLE={}, LOGGERS={}, CLEANUPS=set(),
                                CDI_CANCELLABLE=set(), VIEWS={}, CANCEL_BLOCKS={}, CONFLICTS={},
                                _public=lambda item: {"id": item["id"], "shown": True})
    for name, value in dicts.items():
        getattr(ops, name).update(value)
    return ops


class RegistryTests(unittest.TestCase):
    def test_a_kind_gathers_what_each_registry_says_about_it(self):
        resolve, plan, run, check, reader = (lambda *a: None for _ in range(5))
        ops = stand_in(RESOLVERS={"copy": resolve}, CANCELLERS={"copy": (plan, run)}, RESUMABLE={"copy": check},
                       LOGGERS={"copy": reader}, CLEANUPS={"copy"}, CDI_CANCELLABLE={"copy"})
        found = KINDS.kind("copy", ops)
        self.assertEqual(("copy", resolve, (plan, run), check, reader, True, True),
                         (found.name, found.resolve, found.cancel, found.resumable, found.log,
                          found.cleanup, found.cdi_cancellable))

    def test_a_kind_registered_only_to_resolve_has_nothing_else(self):
        found = KINDS.kind("scan", stand_in(RESOLVERS={"scan": lambda item: None}))
        self.assertEqual((None, None, None, False, False),
                         (found.cancel, found.resumable, found.log, found.cleanup, found.cdi_cancellable))

    def test_a_kind_cancellable_without_a_resolver_shows_with_no_resolver(self):
        found = KINDS.kinds(stand_in(CANCELLERS={"orphan": (None, None)}))
        self.assertIn("orphan", found)
        self.assertIsNone(found["orphan"].resolve)

    def test_it_reads_registrations_made_after_import(self):
        ops = stand_in()
        self.assertEqual({}, KINDS.kinds(ops))
        ops.RESOLVERS["late"] = lambda item: None
        self.assertEqual(["late"], list(KINDS.kinds(ops)))

    def test_a_kind_is_read_only(self):
        found = KINDS.kind("scan", stand_in(RESOLVERS={"scan": lambda item: None}))
        with self.assertRaises(dataclasses.FrozenInstanceError):
            found.resolve = None

    def test_an_unknown_kind_is_a_key_error(self):
        with self.assertRaises(KeyError):
            KINDS.kind("nothing", stand_in())

    def test_its_view_is_the_shared_public_view_today(self):
        found = KINDS.kind("scan", stand_in(RESOLVERS={"scan": lambda item: None}))
        self.assertEqual({"id": "a", "shown": True}, found.view({"id": "a", "kind": "scan"}))

    def test_it_adds_no_start_rules_of_its_own_yet(self):
        found = KINDS.kind("scan", stand_in(RESOLVERS={"scan": lambda item: None}))
        self.assertEqual("", found.conflicts([{"kind": "scan", "status": "running"}], "scan", {}, {}))

    def test_without_a_stand_in_it_reads_the_job_store(self):
        import homestead_operations as OPS
        saved = dict(OPS.RESOLVERS)
        try:
            OPS.RESOLVERS["guard-probe"] = lambda item: None
            self.assertIs(OPS.RESOLVERS["guard-probe"], KINDS.kind("guard-probe").resolve)
        finally:
            OPS.RESOLVERS.clear(); OPS.RESOLVERS.update(saved)


# Kinds named in a branch that no job has, each left for a follow-up because
# removing or renaming it changes behaviour. None today: the one #622's step 2
# found (a "longhorn-v2-upgrade" rule) now names the Longhorn platform-upgrade.
KNOWN_STRAYS = set()
# The modules that branch on a job's kind.
BRANCHING = ("homestead_operations.py", "homestead_storage_conflicts.py", "homestead_vm_power_receipts.py")
# Words a branch compares a `kind` against that are not jobs, by module.
NOT_JOB_BRANCHES = {("homestead_operations.py", "power"),     # _recover: which recovery review a route opens
                    ("homestead_operations.py", "storage")}
# Starts whose kind is a variable, so it cannot be read here: what each passes.
DYNAMIC_STARTS = {"homestead_cdi_cleanup.py": {"vm-disk-import", "unraid-vm-import"},   # dispatch(kind)
                  "homestead_share_servers.py": {"deployment", "share-remove"},          # _restart(kind)
                  "homestead_updates.py": {"image-update", "image-rollback"},            # "image-" + action
                  "homestead_vm_mutation_job.py": {"vm-create", "vm-edit"}}              # dispatch(kind)
# A `kind` the page compares that belongs to something other than a job.
NOT_JOBS = {("views-lifecycle.js", "chown")}  # an import's rows, from /api/imports
JOB_NAME = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)+|[a-z0-9]+")
JOB_VAR = r"\b(?:op|operation|job|o|j)\??\.kind"
PAGE_BRANCH = re.compile(JOB_VAR + r"""\s*[!=]==?\s*["']([a-z0-9-]+)["']""")
PAGE_INCLUDES = re.compile(r"\[([^\]]*)\]\.includes\(" + JOB_VAR + r"\)")
QUOTED = re.compile(r"""["']([a-z0-9-]+)["']""")
DEMO_JOB = re.compile(r'id: *"(?:op\d+|demo-)[^,]*, *kind: *"([a-z0-9-]+)"')


def _names_kind(node):
    """item.get("kind"), item["kind"] or a bare `kind`."""
    if isinstance(node, ast.Name):
        return node.id == "kind"
    if isinstance(node, ast.Subscript):
        return isinstance(node.slice, ast.Constant) and node.slice.value == "kind"
    return (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "get"
            and bool(node.args) and isinstance(node.args[0], ast.Constant) and node.args[0].value == "kind")


def _strings(node):
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return [node.value]
    if isinstance(node, (ast.Tuple, ast.Set, ast.List)):
        return [text for element in node.elts for text in _strings(element)]
    return []


def branch_kinds(source):
    """Job kinds a module compares a kind against. Kubernetes kinds
    ("Deployment") are capitalised and are not jobs."""
    found = set()
    for node in ast.walk(SS.tree(source)):
        if isinstance(node, ast.Compare) and any(_names_kind(part) for part in [node.left, *node.comparators]):
            found |= {text for part in [node.left, *node.comparators] for text in _strings(part)
                      if JOB_NAME.fullmatch(text)}
        if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "KINDS" for t in node.targets):
            found |= set(_strings(node.value))
    return found


def _start_kind(module, node):
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return [node.value]
    if isinstance(node, ast.IfExp):
        body, orelse = _start_kind(module, node.body), _start_kind(module, node.orelse)
        return None if body is None or orelse is None else body + orelse
    if isinstance(node, ast.Name) and node.id.isupper():
        return [getattr(module, node.id)]
    if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.attr.isupper():
        return [getattr(getattr(module, node.value.id), node.attr)]
    return None


STARTS = ("OPS.start", "ops.start", "operations.start")


def start_calls(source, module):
    """The kinds the starts in one module's source pass, and how many starts
    pass a kind that cannot be read. A start is OPS.start(...) under the
    names modules give the job store, the same reached through another module
    (homestead_move_plan's STATE.OPS.start), or a module-level alias of it
    (homestead_promote's OPS_START = OPS.start)."""
    tree = SS.tree(source)
    aliases = {target.id for node in tree.body if isinstance(node, ast.Assign) and SS.dotted(node.value) in STARTS
               for target in node.targets if isinstance(target, ast.Name)}
    through = {name for name in (SS.dotted(node.func) for node in ast.walk(tree) if isinstance(node, ast.Call))
               if name and name.endswith(tuple("." + start for start in STARTS))}
    found, dynamic = set(), 0
    for node in SS.calls(tree, *STARTS, *aliases, *through):
        if node.args:
            kinds = _start_kind(module, node.args[0])
            if kinds is None:
                dynamic += 1
            else:
                found |= set(kinds)
    return found, dynamic


def started_kinds():
    """(kind, file) for every start whose kind can be read, and the files
    with a start whose kind is a variable."""
    found, dynamic = set(), set()
    for path in sorted(SS.SERVER.glob("*.py")):
        kinds, unread = start_calls(path, importlib.import_module(path.stem))
        found |= {(kind, path.name) for kind in kinds}
        if unread:
            dynamic.add(path.name)
    return found, dynamic


def page_job_kinds(text):
    """Job kinds a page compares a job's kind with, either quote, or lists
    with .includes(op.kind); and the kinds of demo.js's jobs."""
    found = set(PAGE_BRANCH.findall(text)) | set(DEMO_JOB.findall(text))
    for listed in PAGE_INCLUDES.findall(text):
        found |= set(QUOTED.findall(listed))
    return found


def page_kinds():
    for path in sorted((ROOT / "web" / "js").glob("*.js")):
        text = SS.text(path)
        for kind in page_job_kinds(text):
            if (path.name, kind) not in NOT_JOBS:
                yield path.name, kind


class ScanTests(unittest.TestCase):
    """The guard's scans read the forms the code uses."""

    def test_a_start_through_a_module_alias_is_read(self):
        source = "OPS_START = OPS.start\n\ndef go():\n    OPS_START('host-promote', 'Promote', {}, '/', {})\n"
        self.assertEqual(({"host-promote"}, 0), start_calls(SS.code(source), types.SimpleNamespace()))

    def test_a_start_through_another_module_s_job_store_is_read(self):
        # homestead_move_plan starts a move through STATE.OPS (#623).
        source = "def go():\n    STATE.OPS.start('move', 'Move', {}, '/', {})\n"
        self.assertEqual(({"move"}, 0), start_calls(SS.code(source), types.SimpleNamespace()))

    def test_a_start_with_a_variable_kind_is_counted_not_guessed(self):
        source = "def go(kind):\n    ops.start(kind, 'x', {}, '/', {})\n    OPS.start(KIND, 'y', {}, '/', {})\n"
        self.assertEqual(({"copy"}, 1), start_calls(SS.code(source), types.SimpleNamespace(KIND="copy")))

    def test_the_page_is_read_in_either_quote_and_in_includes_lists(self):
        text = """operation.kind === 'cluster-shutdown'; op?.kind==="vm-power";
                  ["reclass", 'image-update'].includes(operation?.kind); row.kind === "vip";"""
        self.assertEqual({"cluster-shutdown", "vm-power", "reclass", "image-update"}, page_job_kinds(text))


class GuardTests(unittest.TestCase):
    """Every kind referenced anywhere is a whole kind. server is imported so
    every registration it, its modules' bind()s and register(ops) make is in."""

    @classmethod
    def setUpClass(cls):
        import server  # noqa: F401  (fills the registries)
        import homestead_operations as OPS
        cls.ops, cls.kinds = OPS, KINDS.kinds()

    def test_every_kind_the_store_knows_has_a_resolver(self):
        half = sorted(name for name, kind in self.kinds.items() if kind.resolve is None)
        self.assertEqual([], half, "registered to cancel, resume, log or clean up, but nothing advances it")

    def test_a_kind_cleaned_up_by_cancelling_has_a_canceller(self):
        self.assertEqual([], sorted(name for name, kind in self.kinds.items()
                                    if (kind.cleanup or kind.cdi_cancellable) and not kind.cancel))

    def branches(self):
        return {(name, kind) for name in BRANCHING
                for kind in branch_kinds(SS.SERVER / name)}

    def test_every_kind_a_branch_names_exists(self):
        named = {(name, kind) for name, kind in self.branches() - NOT_JOB_BRANCHES if kind not in KNOWN_STRAYS}
        self.assertEqual([], sorted(f"{kind} ({name})" for name, kind in named if kind not in self.kinds))

    def test_every_listed_exception_is_still_needed(self):
        named = self.branches()
        self.assertEqual(set(), KNOWN_STRAYS - {kind for _, kind in named}, "fixed: take it out of KNOWN_STRAYS")
        self.assertEqual(set(), KNOWN_STRAYS & set(self.kinds), "now a real kind: take it out of KNOWN_STRAYS")
        self.assertEqual(set(), NOT_JOB_BRANCHES - named, "no longer compared: take it out of NOT_JOB_BRANCHES")

    def test_every_kind_with_a_view_of_its_own_exists(self):
        """The kind branches of _public became keys of OPS.VIEWS (#622), where
        branch_kinds() no longer sees them: a mistyped key would never run."""
        self.assertGreater(len(self.ops.VIEWS), 10)
        self.assertEqual([], sorted(set(self.ops.VIEWS) - set(self.kinds)))

    def test_every_kind_with_a_refusal_of_its_own_exists(self):
        """The kind branches of _plan_for became keys of OPS.CANCEL_BLOCKS
        (#622), where branch_kinds() no longer sees them either."""
        self.assertGreaterEqual(len(self.ops.CANCEL_BLOCKS), 4)
        self.assertEqual([], sorted(set(self.ops.CANCEL_BLOCKS) - set(self.kinds)))

    def test_every_kind_with_a_start_rule_of_its_own_exists(self):
        """The kind branches of start() became keys of OPS.CONFLICTS (#622),
        where branch_kinds() no longer sees them either."""
        self.assertGreaterEqual(len(self.ops.CONFLICTS), 6)
        self.assertEqual([], sorted(set(self.ops.CONFLICTS) - set(self.kinds)))

    def test_every_kind_started_has_a_resolver(self):
        found, dynamic = started_kinds()
        self.assertGreater(len(found), 30, "the start scan found too little to trust")
        self.assertIn(("move", "homestead_move_plan.py"), found, "the start scan missed the move start")
        self.assertEqual([], sorted(f"{kind} ({where})" for kind, where in found if kind not in self.kinds))
        self.assertEqual(set(DYNAMIC_STARTS), dynamic,
                         "a start whose kind is a variable: check what it passes, then list it in DYNAMIC_STARTS")
        self.assertEqual([], sorted(f"{kind} ({where})" for where, kinds in DYNAMIC_STARTS.items()
                                    for kind in kinds if kind not in self.kinds))

    def test_every_job_kind_the_page_names_exists(self):
        named = list(page_kinds())
        self.assertGreater(len(named), 10, "the page scan found too little to trust")
        self.assertEqual([], sorted(f"{kind} ({where})" for where, kind in named if kind not in self.kinds))

    def test_host_cordon_kinds_are_real_kinds(self):
        self.assertEqual([], sorted(k for k in KINDS.HOST_CORDON_KINDS if k not in self.kinds))

    def test_every_module_that_cordons_a_host_has_its_kind_listed(self):
        cordons = re.compile(r"""set_cordon\([^)]*,\s*True\)|unschedulable['"],\s*['"]value['"]:\s*True""")
        # Modules with no KIND of their own: the kind whose job runs the cordon.
        # node_actions' power_node is the node-power job's cordon step.
        kinds = {"homestead_components.py": "platform-upgrade", "homestead_node_actions.py": "node-power"}
        # The Hosts page's Cordon is a person's own; homestead_cordon is the shared helper
        # that jobs call (each job's own module is the one that must name its kind).
        people = {"homestead_cordon.py", "homestead_lifecycle.py"}
        found_any = 0
        for path in SS.server_files():
            text = path.read_text()
            if path.name in people or not cordons.search(text):
                continue
            found_any += 1
            found = re.search(r"""^KIND = ['"]([^'"]+)['"]""", text, re.M)
            kind = kinds.get(path.name) or (found and found.group(1))
            self.assertTrue(kind, f"{path.name} cordons a host: name its job kind here")
            self.assertIn(kind, KINDS.HOST_CORDON_KINDS, f"{path.name} cordons hosts as {kind}")
        self.assertGreater(found_any, 3, "the cordon scan found too little to trust")


if __name__ == "__main__":
    unittest.main()
