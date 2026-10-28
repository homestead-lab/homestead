"""homestead_networking split by responsibility (#623): the bound clients and
shared readers live in homestead_network_state, the network picture in
homestead_network_inventory, the Services that publish an app in
homestead_network_services, the VIP registry in homestead_vip_registry and the
VM networks in homestead_network_vms. homestead_networking keeps bind(), the
guard and node probes server.py hands it, and the routes, and offers only the
moved names the move engine and the self address read through it
(test_split_reexports)."""
import ast
import subprocess
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SERVER = ROOT / "server"
sys.path.insert(0, str(SERVER))

import source_scan as SS
import homestead_network_inventory as INV
import homestead_network_services as SVC
import homestead_network_state as STATE
import homestead_network_vms as VMNET
import homestead_networking as NETWORKING
import homestead_vip_registry as REG

PARTS = (STATE, INV, SVC, REG, VMNET)
NAMES = ("homestead_network_state", "homestead_network_inventory", "homestead_network_services",
         "homestead_vip_registry", "homestead_network_vms", "homestead_networking")
# What server.py binds: only homestead_network_state holds it, so that a test
# setting one of them on homestead_networking is not silently ignored.
BOUND = ("kget", "ksend", "SYSTEM_NAMESPACES", "DEFAULT_NAMESPACE", "SHARED_VIP")
# What homestead_networking defines itself.
OWN = ("guard_managed", "node_probes", "bind", "_create_service_route", "_delete_service_route",
       "_default_vip_route", "_change_vip_route", "_fresh", "ROUTES")


def moved(part):
    """Every name a part defines: each part imports whole modules only."""
    return [name for name, value in vars(part).items() if not name.startswith("__")
            and not isinstance(value, type(sys)) and name not in BOUND]


def read_elsewhere():
    """Names other server modules read through homestead_networking."""
    found = set()
    for path in SS.server_files():
        if path.stem in NAMES:
            continue
        for alias in SS.import_aliases(path, "homestead_networking"):
            found |= SS.attribute_reads(path, alias)
    return found


def read_inside():
    """Names the six modules read: as ALIAS.name, or bare in the module that defines them."""
    found = set()
    for part in PARTS + (NETWORKING,):
        text = SS.text(part.__file__).split("# What the move engine and the self address read")[0]
        above = SS.tree(SS.code(text))
        for alias in ("STATE", "INV", "SVC", "REG", "VMNET"):
            found |= SS.attribute_reads(above, alias)
        if part is not NETWORKING:
            used = SS.name_counts(above)
            found |= {name for name in moved(part) if used[name] > 1}
    return found


class NetworkingModuleTests(unittest.TestCase):
    def test_each_moved_name_is_defined_once(self):
        seen = set()
        for part in PARTS:
            names = moved(part)
            self.assertTrue(names, part.__name__)
            for name in names:
                self.assertNotIn(name, seen, f"{name} is defined twice")
                seen.add(name)
        self.assertIn("DNS_NAME", seen)
        self.assertIn("_NothingAdded", seen)

    def test_what_other_modules_read_is_on_homestead_networking(self):
        for name in sorted(read_elsewhere()):
            self.assertTrue(hasattr(NETWORKING, name), name)

    def test_the_bound_clients_live_only_on_homestead_network_state(self):
        for name in BOUND:
            self.assertTrue(hasattr(STATE, name), name)
            for module in PARTS[1:] + (NETWORKING,):
                self.assertNotIn(name, vars(module), f"{module.__name__}.{name} would be a stale copy")

    def test_each_module_can_be_imported_first(self):
        for name in NAMES:
            done = subprocess.run([sys.executable, "-c", f"import {name}, homestead_networking as N, "
                                   "homestead_vip_registry as R, homestead_network_vms as V; "
                                   "N.inventory; N.service_plan; R.registered; V.create_vm_network; N.ROUTES"],
                                  cwd=SERVER, capture_output=True, text=True)
            self.assertEqual(0, done.returncode, f"importing {name} first: {done.stderr}")

    def test_the_routes_stay_on_homestead_networking(self):
        self.assertEqual({("POST", "/api/network/plan"), ("GET", "/api/network"),
                          ("POST", "/api/network/services"), ("POST", "/api/network/service/delete"),
                          ("POST", "/api/network/vips/add"), ("POST", "/api/network/vips/default"),
                          ("POST", "/api/network/vips/remove"), ("POST", "/api/network/vips/change"),
                          ("POST", "/api/network/vips/label"), ("POST", "/api/network/vm-networks")},
                         set(NETWORKING.ROUTES))
        for part in PARTS:
            self.assertFalse(hasattr(part, "ROUTES"), part.__name__)

    def test_bind_reaches_the_moved_code(self):
        saved = {name: getattr(STATE, name) for name in BOUND}
        self.addCleanup(lambda: [setattr(STATE, name, value) for name, value in saved.items()])
        for name in ("guard_managed", "node_probes"):
            self.addCleanup(setattr, NETWORKING, name, getattr(NETWORKING, name))
        asked = []
        kget = lambda path, **_: asked.append(path) or {}
        ksend = lambda *args, **_: None
        guarded = []
        NETWORKING.bind(kget, ksend, ["kube-system"], "apps", "192.0.2.10",
                        lambda ns, name: guarded.append((ns, name)), lambda: {"node-1": {}})
        self.assertEqual((kget, ksend, {"kube-system"}, "apps", "192.0.2.10"),
                         tuple(getattr(STATE, name) for name in BOUND))
        # The registry is read in the default namespace; with nothing in it the
        # shared address is the one bind() was given.
        self.assertEqual("192.0.2.10", REG.shared_vip())
        self.assertEqual(["/api/v1/namespaces/apps/configmaps/homestead-vips"], asked)
        # An ingress in one of the system namespaces bind() was given is marked so.
        ingress = lambda ns: {"metadata": {"namespace": ns, "name": "web"}, "spec": {}, "status": {}}
        self.assertEqual([("apps", False), ("kube-system", True)], [(row["namespace"], row["system"]) for row in
                         INV._ingress_inventory([ingress("kube-system"), ingress("apps")])])
        # The routes use the guard and the node probes bind() was given.
        request = SimpleNamespace(body={"namespace": "apps", "name": "web"})
        with mock.patch.object(SVC, "delete_service", return_value={"ok": True}) as delete:
            NETWORKING._delete_service_route(request)
        self.assertEqual([("apps", "web")], guarded)
        delete.assert_called_once_with("apps", "web", None)
        with mock.patch.object(VMNET, "create_vm_network", return_value={"ok": True}) as create:
            NETWORKING.ROUTES[("POST", "/api/network/vm-networks")][1](SimpleNamespace(body={"name": "lan"}))
        self.assertEqual({"name": "lan", "_probes": {"node-1": {}}}, create.call_args.args[0])

    def test_no_test_replaces_a_moved_name_on_homestead_networking(self):
        # A replacement there would not reach the moved code, so it must go to the
        # module that defines the name. The exception would be a name another
        # server module reads through homestead_networking: replacing it there
        # reaches that module's reads, and only those.
        # Read: assignments, setattr, mock.patch(...), patch.object (name given
        # positionally or as attribute=) and patch.multiple, on homestead_networking
        # as a test imported it, took it from another test module, or reached it
        # through a module that imports it (server.NETWORK, store.NETWORK). Not
        # read: a helper that is handed the module and replaces a name on it.
        allowed = set(OWN) | read_elsewhere()
        # Of those, a name the moved code or the routes also read: a replacement on
        # homestead_networking reaches none of those reads, so only the tests that
        # drive another module's read of it may make one.
        inside = read_inside() & read_elsewhere()
        drives_another_module = {}
        found = []
        trees = {path: SS.tree(path) for path in SS.test_files()}
        imported = {path.stem: SS.import_aliases(tree, "homestead_networking") for path, tree in trees.items()}
        # Which attribute of a server module is homestead_networking.
        holds = {path.stem: SS.import_aliases(path, "homestead_networking") for path in SS.server_files()}
        for path, tree in trees.items():
            aliases = imported[path.stem] | {a.asname or a.name for node in ast.walk(tree)
                                             if isinstance(node, ast.ImportFrom) and node.module in imported
                                             for a in node.names if a.name in imported[node.module]}
            modules = {a.asname or a.name: holds[a.name] for node in ast.walk(tree) if isinstance(node, ast.Import)
                       for a in node.names if holds.get(a.name)}

            def networking(node):
                return (isinstance(node, ast.Name) and node.id in aliases) or \
                    (isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name)
                     and node.attr in modules.get(node.value.id, ()))

            for node in ast.walk(tree):
                name = None
                if isinstance(node, (ast.Assign, ast.AugAssign)):
                    for target in getattr(node, "targets", [getattr(node, "target", None)]):
                        for one in (target.elts if isinstance(target, ast.Tuple) else [target]):
                            if isinstance(one, ast.Attribute) and networking(one.value):
                                name = ",".join(filter(None, [name, one.attr]))
                elif isinstance(node, ast.Call) and isinstance(node.func, (ast.Attribute, ast.Name)):
                    called = node.func.attr if isinstance(node.func, ast.Attribute) else node.func.id
                    first = node.args[0] if node.args else None
                    keywords = {k.arg: k.value for k in node.keywords}
                    second = node.args[1] if len(node.args) > 1 else keywords.get("attribute")
                    if called in ("object", "setattr") and first is not None and networking(first) \
                            and isinstance(second, ast.Constant):
                        name = second.value
                    elif called == "multiple" and first is not None and networking(first):
                        name = ",".join(k.arg for k in node.keywords)
                    elif called == "patch" and isinstance(first, ast.Constant) \
                            and str(first.value).startswith("homestead_networking."):
                        name = first.value.split(".")[1]
                    # (obj, name, value) rows a test patches in a loop, as test_setup does.
                elif isinstance(node, ast.Tuple) and len(node.elts) == 3 and networking(node.elts[0]) \
                        and isinstance(node.elts[1], ast.Constant) and isinstance(node.elts[1].value, str):
                    name = node.elts[1].value
                if name and any(part not in allowed or (part in inside and part not in
                                drives_another_module.get(path.name, ())) for part in name.split(",")):
                    found.append(f"{path.name}:{node.lineno} {name}")
        self.assertEqual([], found)

    def test_only_what_other_modules_read_may_be_replaced_on_homestead_networking(self):
        self.assertNotIn("prepare_deploy", read_elsewhere())
        self.assertNotIn("kget", read_elsewhere())
        self.assertNotIn("_update_registered", read_elsewhere())


if __name__ == "__main__":
    unittest.main()
