"""homestead_longhorn split by responsibility (#623): recurring jobs and the
weekly trim live in homestead_longhorn_jobs, volumes, groups and snapshots in
homestead_longhorn_volumes, the backup target in homestead_longhorn_target,
backups in homestead_longhorn_backups and restores in
homestead_longhorn_restore. homestead_longhorn keeps what bind() sets, the
helpers the parts share, the overview and the routes, and offers only the
moved names the move source and the move engine read through the LH their
bind() is given (test_split_reexports)."""
import ast
import re
import subprocess
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SERVER = ROOT / "server"
sys.path.insert(0, str(SERVER))

import homestead_csi_restore as CSI_RESTORE
import homestead_longhorn as LH
import homestead_longhorn_backups as BACKUPS
import homestead_longhorn_jobs as JOBS
import homestead_longhorn_restore as RESTORE
import homestead_longhorn_target as BACKUP_TARGET
import homestead_longhorn_volumes as VOLUMES
import homestead_operations as OPS

PARTS = (JOBS, VOLUMES, BACKUP_TARGET, BACKUPS, RESTORE)
NAMES = ("homestead_longhorn_jobs", "homestead_longhorn_volumes", "homestead_longhorn_target",
         "homestead_longhorn_backups", "homestead_longhorn_restore", "homestead_longhorn")
# What bind() sets. It stays on homestead_longhorn, where server.py also sets
# STORAGE_CLASS as it finds the cluster's default class, and the parts read it
# there: a copy in a part would go stale.
BOUND = ("kget", "ksend", "_cache", "STORAGE_CLASS", "DEFAULT_NS")
# What homestead_longhorn defines itself.
OWN = BOUND + ("LHNS", "API", "TASKS", "KEEPING", "JOB_LABEL", "GROUP_LABEL", "SAFE", "bind", "_bust",
               "JOB_CHANGED", "_Changed", "_plain", "_update", "PROTECTING", "overview", "run_job_now",
               "backup_now", "restore_reviewed", "ROUTES", "register")


def moved(part):
    """Every name a part defines: each part imports whole modules only."""
    return [name for name, value in vars(part).items() if not name.startswith("__")
            and not isinstance(value, type(sys))]


def bound_holders():
    """Server modules whose LH is set by their bind(): server.py hands them
    homestead_longhorn (the move source, the move engine's state)."""
    return {path.stem for path in SERVER.glob("*.py")
            if re.search(r"^LH\s*=(?:\s*\w+\s*=)*\s*None\s*$", path.read_text(), re.M)}


def read_elsewhere():
    """Names other server modules read through homestead_longhorn, imported or bound."""
    found = set()
    holders = bound_holders()
    for path in SERVER.glob("*.py"):
        if path.stem in NAMES:
            continue
        text = path.read_text()
        for alias in re.findall(r"homestead_longhorn as (\w+)", text):
            found |= set(re.findall(rf"\b{alias}\.(\w+)", text))
        if path.stem in holders:
            found |= set(re.findall(r"(?<![\w.])LH\.(\w+)", text))
        for holder in holders:
            for alias in re.findall(rf"\b{holder} as (\w+)", text):
                found |= set(re.findall(rf"\b{alias}\.LH\.(\w+)", text))
        for names in re.findall(r"from homestead_longhorn import ([\w, ]+)", text):
            found |= {name.strip() for name in names.split(",")}
    return found


def read_inside():
    """Moved names the six modules read: as ALIAS.name, or bare in the part that defines them."""
    found = set()
    for module in PARTS + (LH,):
        text = Path(module.__file__).read_text().split("# What the move source and the move engine read")[0]
        found |= set(re.findall(r"\b(?:JOBS|VOLUMES|BACKUP_TARGET|BACKUPS|RESTORE)\.(\w+)", text))
        if module is not LH:
            found |= {name for name in moved(module) if len(re.findall(rf"\b{name}\b", text)) > 1}
    return found


class LonghornModuleTests(unittest.TestCase):
    def test_each_moved_name_is_defined_once(self):
        seen = set()
        for part in PARTS:
            names = moved(part)
            self.assertTrue(names, part.__name__)
            for name in names:
                self.assertNotIn(name, seen, f"{name} is defined twice")
                self.assertNotIn(name, OWN, f"{part.__name__}.{name} is homestead_longhorn's")
                seen.add(name)
        self.assertEqual(62, len(seen))
        self.assertIn("TRIM_JOB", seen)
        self.assertIn("K8S_LABEL", seen)

    def test_what_other_modules_read_is_on_homestead_longhorn_as_the_same_object(self):
        self.assertIn("STORAGE_CLASS", read_elsewhere())
        self.assertNotIn("move_snapshot_error", read_elsewhere())
        for name in sorted(read_elsewhere()):
            self.assertTrue(hasattr(LH, name), name)
            for part in PARTS:
                if name in vars(part):
                    self.assertIs(getattr(part, name), getattr(LH, name), f"{part.__name__}.{name}")

    def test_what_bind_sets_lives_only_on_homestead_longhorn(self):
        for name in BOUND:
            self.assertIn(name, vars(LH), name)
            for part in PARTS:
                self.assertFalse(name in vars(part), f"{part.__name__}.{name} would be a stale copy")

    def test_each_module_can_be_imported_first(self):
        for name in NAMES:
            done = subprocess.run([sys.executable, "-c", f"import {name}, homestead_longhorn as L, "
                                   "homestead_longhorn_jobs as J, homestead_longhorn_volumes as V, "
                                   "homestead_longhorn_restore as R; "
                                   "J.list_jobs; V.save_group; L.backup_target; L.backups; R.restore_plan; L.ROUTES"],
                                  cwd=SERVER, capture_output=True, text=True)
            self.assertEqual(0, done.returncode, f"importing {name} first: {done.stderr}")

    def test_the_routes_and_the_job_store_entry_stay_on_homestead_longhorn(self):
        self.assertEqual(17, len(LH.ROUTES))
        self.assertTrue(all(path.startswith("/api/lh/") for _, path in LH.ROUTES))
        for part in PARTS:
            self.assertFalse(hasattr(part, "ROUTES"), part.__name__)
            self.assertFalse(hasattr(part, "register"), part.__name__)
        ops = SimpleNamespace(RESOLVERS={})
        LH.register(ops)
        self.assertIs(JOBS.run_status, ops.RESOLVERS["protect-run"])

    def test_what_bind_sets_reaches_the_moved_code(self):
        saved = {name: getattr(LH, name) for name in BOUND}
        self.addCleanup(lambda: [setattr(LH, name, value) for name, value in saved.items()])
        # bind() also binds homestead_csi_restore; put its clients back too.
        self.addCleanup(setattr, CSI_RESTORE, "ksend", CSI_RESTORE.ksend)
        self.addCleanup(setattr, CSI_RESTORE, "kget", CSI_RESTORE.kget)
        backup = {"metadata": {"name": "nightly-1"},
                  "status": {"state": "Completed", "url": "s3://example", "volumeSize": "1073741824",
                             "volumeName": "pvc-data"}}
        asked, sent = [], []

        def kget(path, **_):
            asked.append(path)
            if path.endswith("/backups/nightly-1"):
                return backup
            if path == "/api/v1/namespaces/apps":
                return {}
            return {"items": []}

        cache = {"lhjobs": 1, "other": 2}
        LH.bind(kget, lambda *args, **_: sent.append(args) or {}, cache, "example-class", "apps")
        self.assertEqual((kget, cache, "example-class", "apps"),
                         (LH.kget, LH._cache, LH.STORAGE_CLASS, LH.DEFAULT_NS))
        # The parts read the clients and the cache bind() was given.
        self.assertEqual([], JOBS.list_jobs())
        self.assertIn(f"{LH.API}/namespaces/{LH.LHNS}/recurringjobs", asked)
        JOBS.delete_job("daily")
        self.assertEqual(("DELETE", f"{LH.API}/namespaces/{LH.LHNS}/recurringjobs/daily"), sent[-1])
        self.assertEqual({"other": 2}, cache)
        # A restore is planned in the default namespace, on the storage class
        # bind() was given, and then on the one server.py sets later.
        plan_route = LH.ROUTES[("GET", "/api/lh/restore/plan")][1]
        plan = plan_route(SimpleNamespace(query={"backup": ["nightly-1"], "name": ["data"]}))
        self.assertEqual(("apps", "example-class"), (plan["namespace"], plan["storage_class"]))
        LH.STORAGE_CLASS = "example-default"
        self.assertEqual("example-default", RESTORE.restore_plan("nightly-1", "apps", "data")["storage_class"])

    def test_the_overview_and_the_route_handlers_read_the_parts(self):
        with mock.patch.object(JOBS, "list_jobs", return_value=[
                {"name": "daily", "task": "backup", "volumes": ["pvc-a"], "groups": ["default"]}]), \
                mock.patch.object(VOLUMES, "_volumes", return_value=[{"name": "pvc-a", "pvc": "data", "groups": ["default"]}]), \
                mock.patch.object(VOLUMES, "groups", return_value=["default"]), \
                mock.patch.object(BACKUP_TARGET, "backup_target", return_value={"configured": True}):
            overview = LH.overview()
        self.assertEqual((1, 1, {"configured": True}), (overview["protected"], overview["backed_up"], overview["target"]))
        with mock.patch.object(RESTORE, "restore_plan", return_value={"conflict": {"message": "taken"}}), \
                mock.patch.object(RESTORE, "restore_backup") as restore, \
                mock.patch.object(OPS, "transaction", mock.MagicMock()):
            reply = LH.restore_reviewed({"backup": "nightly-1", "name": "data"})
        self.assertEqual(409, reply.code)
        restore.assert_not_called()

    def test_no_test_replaces_a_moved_name_on_homestead_longhorn(self):
        # A replacement there would not reach the moved code, so it must go to the
        # module that defines the name. The exceptions: what homestead_longhorn
        # defines itself (bind()'s clients, the shared helpers, the routes), and a
        # name the move engine or move source reads
        # through the LH their bind() is given: replacing it there reaches those
        # reads, and only those. Read: assignments (tuple targets too), setattr,
        # mock.patch("homestead_longhorn.…") and mock.patch("server.LH.…"),
        # patch.object (target and name positional or as keywords), patch.multiple
        # and (module, name, value) rows, on homestead_longhorn as a test imported
        # it, took it from another test module, or reached it through a module
        # that imports or is bound to it (server.LH, store.LH, STATE.LH). Also: a
        # bound value assigned on a part (JOBS.kget = x). Not read: a helper that
        # is handed the module, or a name held in a variable.
        allowed = set(OWN) | read_elsewhere()
        # Of those, a name the moved code, the overview or the routes also read: a
        # replacement on homestead_longhorn reaches none of those reads, so only
        # the tests that drive another module's read of it may make one.
        inside = read_inside() & read_elsewhere()
        drives_another_module = {
            "test_move_backup.py": {"backups"},                   # homestead_move_source, as SOURCE.LH
        }
        found = []
        trees = {path: ast.parse(path.read_text()) for path in sorted((ROOT / "tests").glob("test_*.py"))}
        imported = {path.stem: {a.asname or a.name for node in ast.walk(tree) if isinstance(node, ast.Import)
                                for a in node.names if a.name == "homestead_longhorn"}
                    for path, tree in trees.items()}
        # Which attribute of a server module is homestead_longhorn.
        holds = {}
        for path in SERVER.glob("*.py"):
            holds[path.stem] = set(re.findall(r"homestead_longhorn as (\w+)", path.read_text()))
        for stem in bound_holders():
            holds[stem].add("LH")
        strings = ("homestead_longhorn.",) + tuple(f"{stem}.{alias}." for stem, aliases in holds.items()
                                                   for alias in aliases)
        for path, tree in trees.items():
            aliases = imported[path.stem] | {a.asname or a.name for node in ast.walk(tree)
                                             if isinstance(node, ast.ImportFrom) and node.module in imported
                                             for a in node.names if a.name in imported[node.module]}
            modules = {a.asname or a.name: holds[a.name] for node in ast.walk(tree) if isinstance(node, ast.Import)
                       for a in node.names if holds.get(a.name)}
            parts = {a.asname or a.name for node in ast.walk(tree) if isinstance(node, ast.Import)
                     for a in node.names if a.name in NAMES[:-1]}

            def longhorn(node):
                return (isinstance(node, ast.Name) and node.id in aliases) or \
                    (isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name)
                     and node.attr in modules.get(node.value.id, ()))

            for node in ast.walk(tree):
                name = None
                if isinstance(node, (ast.Assign, ast.AugAssign)):
                    for target in getattr(node, "targets", [getattr(node, "target", None)]):
                        for one in (target.elts if isinstance(target, ast.Tuple) else [target]):
                            if isinstance(one, ast.Attribute) and longhorn(one.value):
                                name = ",".join(filter(None, [name, one.attr]))
                            # A bound value set on a part: an attribute nothing reads.
                            elif isinstance(one, ast.Attribute) and isinstance(one.value, ast.Name) \
                                    and one.value.id in parts and one.attr in BOUND:
                                found.append(f"{path.name}:{node.lineno} {one.value.id}.{one.attr} (lives on homestead_longhorn)")
                elif isinstance(node, ast.Call) and isinstance(node.func, (ast.Attribute, ast.Name)):
                    called = node.func.attr if isinstance(node.func, ast.Attribute) else node.func.id
                    keywords = {k.arg: k.value for k in node.keywords}
                    first = node.args[0] if node.args else keywords.get("target")
                    second = node.args[1] if len(node.args) > 1 else keywords.get("attribute")
                    if called in ("object", "setattr") and first is not None and longhorn(first) \
                            and isinstance(second, ast.Constant):
                        name = second.value
                    elif called == "multiple" and first is not None and longhorn(first):
                        name = ",".join(k.arg for k in node.keywords)
                    elif called == "patch" and isinstance(first, ast.Constant) and isinstance(first.value, str):
                        prefix = next((s for s in strings if first.value.startswith(s)), None)
                        if prefix:
                            name = first.value[len(prefix):].split(".")[0]
                # (obj, name, value) rows a test patches in a loop, as test_setup does.
                elif isinstance(node, ast.Tuple) and len(node.elts) == 3 and longhorn(node.elts[0]) \
                        and isinstance(node.elts[1], ast.Constant) and isinstance(node.elts[1].value, str):
                    name = node.elts[1].value
                if name and any(part not in allowed or (part in inside and part not in
                                drives_another_module.get(path.name, ())) for part in name.split(",")):
                    found.append(f"{path.name}:{node.lineno} {name}")
        self.assertEqual([], found)

    def test_only_what_other_modules_read_may_be_replaced_on_homestead_longhorn(self):
        self.assertIn("restore_backup", read_elsewhere())
        # Read through a module bound to homestead_longhorn: the move source's
        # LH, and the move engine's STATE.LH.
        for name in ("ensure_move_backup", "validate_restore_class", "backup_target_state",
                     "replace_backup_target_state", "transfer_target_state", "restore_support",
                     "ensure_restore_support"):
            self.assertIn(name, read_elsewhere())
        self.assertNotIn("restore_plan", read_elsewhere())
        self.assertNotIn("_backend", read_elsewhere())
        # Read by the restore route too, so a replacement on homestead_longhorn
        # misses it: allowed only where the test drives another module's read.
        self.assertIn("restore_backup", read_inside())


if __name__ == "__main__":
    unittest.main()
