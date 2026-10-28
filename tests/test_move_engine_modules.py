"""homestead_move_engine split by responsibility (#623): the bound clients,
records and shared helpers live in homestead_move_state, the backup target in
homestead_move_target, reviewing and starting a move in homestead_move_plan, the
phases and the tick in homestead_move_phases, and retry, abandon, dismiss and
finish in homestead_move_actions. homestead_move_engine keeps bind(),
after_finish, the routes and the Activity tray registration. Its one-release
re-exports are gone: test_split_reexports checks that."""
import ast
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SERVER = ROOT / "server"
sys.path.insert(0, str(SERVER))

import homestead_move_actions as ACT
import homestead_move_engine as ENGINE
import homestead_move_phases as PHASE
import homestead_move_plan as PLAN
import homestead_move_state as STATE
import homestead_move_target as TARGET

PARTS = (STATE, TARGET, PLAN, PHASE, ACT)
NAMES = ("homestead_move_state", "homestead_move_target", "homestead_move_plan",
         "homestead_move_phases", "homestead_move_actions", "homestead_move_engine")
# What server.py binds: only homestead_move_state holds it, so that a test or a
# module setting one of them on homestead_move_engine is not silently ignored.
BOUND = ("kget", "ksend", "LH", "CLIENT", "NETWORK", "OPS", "DATA_DIR", "NS")
# What homestead_move_engine defines itself.
OWN = ("guard_managed_smb", "bind", "after_finish", "_plan_or_start", "ROUTES", "register")


def engine_holders():
    """Which attribute of a server module is homestead_move_engine (server.MOVE_ENGINE)."""
    holds = {}
    for path in SERVER.glob("*.py"):
        tree = ast.parse(path.read_text())
        holds[path.stem] = {a.asname or a.name for node in ast.walk(tree) if isinstance(node, ast.Import)
                            for a in node.names if a.name == "homestead_move_engine"}
    return holds


def _replacements(node):
    """(module, name) for each replacement this node makes: assignments (tuple
    targets too), setattr, patch.object (target and name positional or as
    keywords), patch.multiple, and (module, name, value) rows a test patches in
    a loop. The name is a str or the expression that gives it."""
    if isinstance(node, (ast.Assign, ast.AugAssign)):
        return [(one.value, one.attr) for target in getattr(node, "targets", [getattr(node, "target", None)])
                for one in (target.elts if isinstance(target, ast.Tuple) else [target])
                if isinstance(one, ast.Attribute)]
    if isinstance(node, ast.Call) and isinstance(node.func, (ast.Attribute, ast.Name)):
        called = node.func.attr if isinstance(node.func, ast.Attribute) else node.func.id
        keywords = {k.arg: k.value for k in node.keywords}
        first = node.args[0] if node.args else keywords.get("target")
        second = node.args[1] if len(node.args) > 1 else keywords.get("attribute")
        if called in ("object", "setattr") and first is not None and second is not None:
            return [(first, second)]
        if called == "multiple" and first is not None:
            return [(first, ",".join(k.arg or "?" for k in node.keywords))]
    if isinstance(node, ast.Tuple) and len(node.elts) == 3 and isinstance(node.elts[1], ast.Constant) \
            and isinstance(node.elts[1].value, str):
        return [(node.elts[0], node.elts[1])]
    return []


def _calls(node, helpers):
    """(module, name) a call to one of helpers hands it."""
    if not isinstance(node, ast.Call) or not isinstance(node.func, (ast.Attribute, ast.Name)):
        return []
    called = node.func.attr if isinstance(node.func, ast.Attribute) else node.func.id
    keywords = {k.arg: k.value for k in node.keywords}
    rows = []
    for names, position, name in helpers.get(called, ()):
        # A method called on self or cls: its first parameter is not in the call.
        skip = 1 if isinstance(node.func, ast.Attribute) and names and names[0] in ("self", "cls") else 0

        def argument(index):
            if 0 <= index - skip < len(node.args):
                return node.args[index - skip]
            return keywords.get(names[index])

        module = argument(position)
        if module is not None:
            rows.append((module, argument(name) if isinstance(name, int) else name))
    return rows


def _helpers(tree):
    """Functions that replace a name on a module they are handed, directly or by
    handing it on: {function: {(parameters, module position, name)}}, the name a
    str, a parameter's position, or None when the helper does not spell it out."""
    functions = [node for node in ast.walk(tree) if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))]
    params = {fn: [a.arg for a in fn.args.posonlyargs + fn.args.args + fn.args.kwonlyargs] for fn in functions}

    def entry(fn, module, name):
        if not (isinstance(module, ast.Name) and module.id in params[fn]):
            return None
        if isinstance(name, ast.Constant) and isinstance(name.value, str):
            name = name.value
        elif isinstance(name, ast.Name) and name.id in params[fn]:
            name = params[fn].index(name.id)
        elif not isinstance(name, str):
            name = None
        return (tuple(params[fn]), params[fn].index(module.id), name)

    found = {}
    for fn in functions:
        for node in ast.walk(fn):
            for module, name in _replacements(node):
                if entry(fn, module, name):
                    found.setdefault(fn.name, set()).add(entry(fn, module, name))
    # A helper that hands its module on to one of these is one too.
    grown = True
    while grown:
        grown = False
        for fn in functions:
            for node in ast.walk(fn):
                for module, name in _calls(node, found):
                    row = entry(fn, module, name)
                    if row and row not in found.get(fn.name, set()):
                        found.setdefault(fn.name, set()).add(row)
                        grown = True
    return found


def replaced(sources, holds):
    """Each place a test replaces a name on homestead_move_engine other than
    the ones it defines, as "file:line name". Read: what _replacements reads,
    and mock.patch("homestead_move_engine.…") or through a server module that
    imports it (server.MOVE_ENGINE.…); on the engine as a test imported it, as
    it took it from another test module (from test_move_flow import engine,
    fixtures.engine), or as a server module names it (server.MOVE_ENGINE). Also
    a helper that is handed the engine and replaces a name on it, or hands it on
    to one that does (#623's wrap-up), when the helper is defined in the same
    test file. A name the code does not spell out reads as "?". Not read: a
    helper from another file, functools.partial, or the module held in a
    local variable inside the helper."""
    found = []
    trees = {name: ast.parse(text) for name, text in sources.items()}
    imported = {Path(name).stem: {a.asname or a.name for node in ast.walk(tree) if isinstance(node, ast.Import)
                                  for a in node.names if a.name == "homestead_move_engine"}
                for name, tree in trees.items()}
    anywhere = set().union(*imported.values(), *holds.values())
    strings = ("homestead_move_engine.",) + tuple(f"{stem}.{alias}." for stem, aliases in holds.items()
                                                  for alias in aliases)
    for path, tree in trees.items():
        stem = Path(path).stem
        aliases = imported[stem] | {a.asname or a.name for node in ast.walk(tree)
                                    if isinstance(node, ast.ImportFrom) and node.module in imported
                                    for a in node.names if a.name in imported[node.module]}

        def engine(node):
            return (isinstance(node, ast.Name) and node.id in aliases) or \
                (isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.attr in anywhere)

        helpers = _helpers(tree)
        for node in ast.walk(tree):
            rows = [(module, name, "") for module, name in _replacements(node)]
            if isinstance(node, ast.Call) and isinstance(node.func, (ast.Attribute, ast.Name)):
                called = node.func.attr if isinstance(node.func, ast.Attribute) else node.func.id
                rows += [(module, name, f" (through {called})") for module, name in _calls(node, helpers)]
                first = node.args[0] if node.args else None
                if called == "patch" and isinstance(first, ast.Constant) and isinstance(first.value, str):
                    prefix = next((s for s in strings if first.value.startswith(s)), None)
                    if prefix and first.value[len(prefix):].split(".")[0] not in OWN:
                        found.append(f"{path}:{node.lineno} {first.value[len(prefix):].split('.')[0]}")
            for module, name, through in rows:
                if not engine(module):
                    continue
                if isinstance(name, ast.Constant) and isinstance(name.value, str):
                    name = name.value
                if not isinstance(name, str):
                    name = "?"
                if any(part not in OWN for part in name.split(",")):
                    found.append(f"{path}:{node.lineno} {name}{through}")
    return found


# The guard's own example: helpers handed the engine, as test_optional_storage_hosts'
# reading(module, reader) is handed the module whose kget it replaces.
HELPERS = '''
import homestead_move_engine as ENGINE
import server
from unittest import mock


def keyword(value, *, module):
    module.dismiss = value


def naming(module, name, value):
    setattr(module, name, value)


class Example:
    def reading(self, module, reader):
        patch = mock.patch.object(module, "abandon", reader)
        patch.start()

    def passing(self, target):
        naming(target, "tick_all", None)

    def test(self, name):
        self.reading(ENGINE, None)
        naming(module=ENGINE, name="carry_icon", value=None)
        self.passing(server.MOVE_ENGINE)
        naming(ENGINE, name, None)
        naming(ENGINE, "after_finish", None)
        self.reading(STATE, None)
        for module, name, value in ((ENGINE, "_find", None),):
            setattr(module, name, value)
        mock.patch("server.MOVE_ENGINE._retry").start()
        ENGINE.TICK_SECONDS = 1
        keyword(None, module=ENGINE)
        mock.patch.multiple(ENGINE, **{"retry": None})
'''


class MoveEngineModuleTests(unittest.TestCase):
    def test_the_bound_clients_live_only_on_homestead_move_state(self):
        for name in BOUND:
            self.assertTrue(hasattr(STATE, name), name)
            self.assertFalse(hasattr(ENGINE, name), f"homestead_move_engine.{name} would be a stale copy")

    def test_each_module_can_be_imported_first(self):
        # The modules that read the moved names too (wrap-up row 40).
        for name in NAMES + ("homestead_cancel", "homestead_deploy_server", "homestead_names_helpers"):
            done = subprocess.run([sys.executable, "-c", f"import {name}, homestead_move_engine as E, "
                                   "homestead_move_phases as P, homestead_move_actions as A, homestead_move_state as S; "
                                   "P.tick_all; P.carry_icon; A.abandon; S._find; E.ROUTES"],
                                  cwd=SERVER, capture_output=True, text=True)
            self.assertEqual(0, done.returncode, f"importing {name} first: {done.stderr}")

    def test_the_routes_stay_on_homestead_move_engine(self):
        self.assertEqual({("GET", "/api/move/moves"), ("POST", "/api/move/moves/dismiss"),
                          ("POST", "/api/move/plan"), ("POST", "/api/move/start"),
                          ("POST", "/api/move/moves/retry"), ("POST", "/api/move/moves/abandon"),
                          ("POST", "/api/move/moves/finish")}, set(ENGINE.ROUTES))
        for part in PARTS:
            self.assertFalse(hasattr(part, "ROUTES"), part.__name__)

    def test_the_unused_run_loop_is_gone(self):
        # The leader's loop in homestead_deploy_server calls tick_all; nothing called run().
        for module in PARTS + (ENGINE,):
            self.assertFalse(hasattr(module, "run"), module.__name__)

    def test_bind_reaches_the_moved_code(self):
        saved = {name: getattr(STATE, name) for name in BOUND}
        self.addCleanup(lambda: [setattr(STATE, name, value) for name, value in saved.items()])
        saved_guard = ENGINE.guard_managed_smb
        self.addCleanup(setattr, ENGINE, "guard_managed_smb", saved_guard)
        asked, sent = [], []
        kget = lambda path: asked.append(path) or {}
        ksend = lambda *args, **_: sent.append(args)
        with tempfile.TemporaryDirectory() as directory, mock.patch("homestead_names.bind"):
            ENGINE.bind(kget, ksend, "longhorn", "client", "network", "operations", directory, "apps")
            self.assertEqual((kget, ksend, "longhorn", "client", "network", "operations", directory, "apps"),
                             tuple(getattr(STATE, name) for name in BOUND))
            PHASE._ensure_namespace("apps")
            self.assertEqual(["/api/v1/namespaces/apps"], asked)
            self.assertEqual([], STATE.moves())
            self.assertEqual({}, TARGET._target_journal())

    def test_a_guard_given_to_bind_runs_before_a_container_move_starts(self):
        saved = {name: getattr(STATE, name) for name in BOUND}
        self.addCleanup(lambda: [setattr(STATE, name, value) for name, value in saved.items()])
        self.addCleanup(setattr, ENGINE, "guard_managed_smb", ENGINE.guard_managed_smb)
        guarded = []
        with mock.patch("homestead_names.bind"):
            ENGINE.bind(None, None, None, None, None, None, "/data", "apps",
                        guard=lambda ns, name: guarded.append((ns, name)))
        request = SimpleNamespace(body={"cluster": "other", "name": "web"})
        with mock.patch.object(PLAN, "start", return_value={"id": "m"}) as start:
            ENGINE._plan_or_start(request, True)
        self.assertEqual([("apps", "web")], guarded)
        self.assertEqual(("other", "container", "web", "apps"), start.call_args.args[:4])

    def test_after_finish_set_on_homestead_move_engine_runs_when_a_move_finishes(self):
        # server.py sets after_finish on homestead_move_engine.
        finished = []
        with mock.patch.object(ENGINE, "after_finish", lambda: finished.append(True)), \
                mock.patch.object(STATE, "_store"), mock.patch.object(PHASE, "_cleanup_resources"), \
                mock.patch.object(TARGET, "_cleanup_target"):
            PHASE._finish({"id": "0123456789ab"}, "failed", "stopped")
        self.assertEqual([True], finished)

    def test_no_test_replaces_a_moved_name_on_homestead_move_engine(self):
        # A replacement there would not reach the moved code, so it must go to the
        # module that defines the name. What replaced() reads is in its docstring.
        sources = {path.name: path.read_text() for path in sorted((ROOT / "tests").glob("test_*.py"))}
        self.assertEqual([], replaced(sources, engine_holders()))

    def test_the_guard_sees_a_helper_that_is_handed_the_engine(self):
        # As test_optional_storage_hosts' reading(module, reader) does with kget:
        # the helper replaces the name, the test only hands it the module.
        found = replaced({"test_example.py": HELPERS}, {"server": {"MOVE_ENGINE"}})
        self.assertEqual(sorted(["abandon (through reading)", "carry_icon (through naming)",
                                 "tick_all (through passing)", "? (through naming)", "_find", "_retry",
                                 "TICK_SECONDS", "dismiss (through keyword)", "?"]),
                         sorted(row.split(" ", 1)[1] for row in found))

    def test_no_server_module_reads_a_moved_name_through_homestead_move_engine(self):
        # A test replacing a name where it is defined (ACT.abandon, PHASE.tick_all)
        # would not reach a read of the re-export, so the readers use the part.
        found = []
        for path in sorted(SERVER.glob("*.py")):
            if path.stem == "homestead_move_engine":
                continue
            tree = ast.parse(path.read_text())
            aliases = {a.asname or a.name for node in ast.walk(tree) if isinstance(node, ast.Import)
                       for a in node.names if a.name == "homestead_move_engine"}
            found += [f"{path.name}:{node.lineno} {node.attr}" for node in ast.walk(tree)
                      if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name)
                      and node.value.id in aliases and node.attr not in OWN]
        self.assertEqual([], found)


class ReadWhereDefinedTests(unittest.TestCase):
    """Cancel, the leader's loop and the icon heal read the moved names from
    the module that defines them, so replacing a name there reaches them."""

    def test_cancelling_a_move_reads_the_record_and_abandons_through_the_parts(self):
        import homestead_cancel as CANCEL
        item = {"ref": {"move": "0123456789ab"}}
        with mock.patch.object(STATE, "_find", return_value={"name": "web", "cluster": "other"}):
            self.assertEqual("rollback", CANCEL.move_plan(item)["mode"])
        with mock.patch.object(ACT, "abandon", return_value={"message": "Put back web"}) as abandon:
            self.assertEqual("Put back web", CANCEL.move_cancel(item, {}))
        abandon.assert_called_once_with("0123456789ab")

    def test_the_leaders_loop_ticks_through_homestead_move_phases(self):
        import homestead_deploy_server as DEPLOY_SERVER

        class Stop(Exception):
            pass

        beats = []
        with mock.patch.object(DEPLOY_SERVER.LEADER, "is_leader", return_value=True), \
                mock.patch.object(PHASE, "tick_all") as tick_all, \
                mock.patch.object(STATE, "TICK_SECONDS", 7), \
                mock.patch.object(DEPLOY_SERVER, "beat", lambda *args, **kw: beats.append((args, kw)), create=True), \
                mock.patch.object(DEPLOY_SERVER.time, "sleep", side_effect=Stop) as sleep:
            with self.assertRaises(Stop):
                DEPLOY_SERVER._moves_loop()
        tick_all.assert_called_once_with()
        self.assertEqual([(("moves", 7), {"leader_only": True})], beats)
        sleep.assert_called_once_with(7)

    def test_the_icon_heal_carries_the_icon_through_homestead_move_phases(self):
        import homestead_names as NAMES_MODULE
        import homestead_names_helpers as NAMES_HELPERS
        reference = "/api/icons/" + "a" * 64 + ".png"
        self.addCleanup(NAMES_HELPERS._ICON_HEALED.pop, reference, None)
        annotations = {NAMES_MODULE.key("moved-from"): "oldcluster/web"}
        with mock.patch.object(PHASE, "carry_icon", return_value="/api/icons/carried.png") as carry:
            NAMES_HELPERS._heal_icon(reference, annotations)
        self.assertEqual("oldcluster", carry.call_args.args[0])
        self.assertEqual("/api/icons/carried.png", NAMES_HELPERS._ICON_HEALED[reference])


if __name__ == "__main__":
    unittest.main()
