"""A second bind() that leaves an optional argument out puts that name back to the
module's own default, not to what the last bind() set (#TBD-bind-resets-names).

A name that kept its last value let one test's fake outlive the test: the next
file's bind() left the argument out and read the fake (#1073, #1092). In
production server.py binds each of these modules once, at import, so the value
before that bind() is the module default anyway: nothing changes there.

Each row binds once with a sentinel for one optional argument, then again
without it, and checks that every name that argument sets is back to the
module's default. Everything bind() touches is put back after each case.
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import rebind as REBIND


def f(*args, **kwargs):
    return None


def S(label):
    """A truthy sentinel that is also callable, so any bind() takes it."""
    def sentinel(*args, **kwargs):
        return None
    sentinel.__qualname__ = f"sentinel<{label}>"
    return sentinel


TEXT = "sentinel-value"
# module, required positional arguments, {optional keyword: (value, [names it sets])}
CASES = [
    ("homestead_cluster", (f, (), f), {"_overview": (S("overview"), ["overview"])}),
    ("homestead_container_rebalance", (f, f, f, f), {"_is_leader": (S("is_leader"), ["is_leader"])}),
    ("homestead_host_bonds", (f, f, f), {"_platform": (S("platform"), ["platform"])}),
    ("homestead_imports", (f, f, f, f, "lab", {}), {"_hardware_features": (S("hw"), ["hardware_features"])}),
    ("homestead_lhrebuild", (f, f), {"enabled": (S("enabled"), ["_enabled"]),
                                     "settings": (S("settings"), ["app_settings", "save_app_settings"])}),
    ("homestead_lifecycle", (f, f, set(), {}), {"_hardware_features": (S("hw"), ["hardware_features"]),
                                                "_create_pvc": (S("pvc"), ["create_pvc"]),
                                                "_storage_class": (TEXT, ["STORAGE_CLASS"])}),
    ("homestead_longhorn", (f, f, {}), {"default_namespace": (TEXT, ["DEFAULT_NS"])}),
    ("homestead_move_engine", (f, f, f, f, f, f, "/tmp", "lab"), {"guard": (S("guard"), ["guard_managed_smb"])}),
    ("homestead_networking", (f, f, (), "lab", ""), {"_guard_managed": (S("guard"), ["guard_managed"]),
                                                     "_node_probes": (S("probes"), ["node_probes"])}),
    ("homestead_place", (f, f, f, {}), {"_hardware_features": (S("hw"), ["hardware_features"])}),
    ("homestead_placement_mode", (f, f), {"_settings": (S("settings"), ["settings", "system_ns"])}),
    ("homestead_priority", (f, f, f, (), ()), {"_is_leader": (S("leader"), ["is_leader"]),
                                               "_guard": (S("guard"), ["guard"])}),
    ("homestead_pullwatch", (f, f, f, f), {"data_dir": (TEXT, ["DATA_DIR"])}),
    ("homestead_push", ("/tmp",), {"contact": (TEXT, ["CONTACT"])}),
    ("homestead_rebalance", (f, f), {"_is_leader": (S("leader"), ["is_leader"])}),
    ("homestead_recovery", ("/tmp",), {"_read": (S("read"), ["read", "settings", "operations", "plan", "start", "WAITS_FOR"]),
                                       "_is_leader": (S("leader"), ["is_leader", "beat"]),
                                       "_managed_by": (S("managed"), ["managed_by"])}),
    ("homestead_requests", (f, f, f, f, (), ()), {"_is_leader": (S("leader"), ["is_leader"]),
                                                  "_guard": (S("guard"), ["guard"])}),
    ("homestead_restore_test", (f, f, f, f, f, f, f, f, f, f), {"_workloads": (S("workloads"), ["workloads", "app_settings", "save_app_settings", "dns_name"])}),
    ("homestead_revert", (f, f, f), {"_is_self": (S("self"), ["is_self"])}),
    ("homestead_schedules", ("/tmp",), {"items": (S("items"), ["scheduled", "save"])}),
    ("homestead_unraid_vms", (f, f, f, f), {"_platform": (S("platform"), ["platform"]),
                                            "storage": ((S("classes"), S("capacity"), S("default")),
                                                        ["storage_classes", "lh_capacity", "default_class"])}),
    ("homestead_uplinks", (f, f), {"_node_probes": (S("probes"), ["node_probes"])}),
    ("homestead_uptime", ("/tmp",), {"setting": (S("setting"), ["save_setting"])}),
    ("homestead_usage", ("/tmp", 4, 1 << 30), {"is_leader": (S("leader"), ["_is_leader"]),
                                               "beat": (S("beat"), ["_beat"])}),
    ("homestead_vms", (f, f, f), {"_display_icon": (S("icon"), ["display_icon"]),
                                  "default_ns": (TEXT, ["DEFAULT_NS"]),
                                  "_set_logos": (S("logos"), ["set_logos"]),
                                  "_set_monitoring": (S("monitoring"), ["set_monitoring"])}),
]


class EachBindResetsWhatItLeavesOut(unittest.TestCase):
    def keep_everything(self, module):
        """What bind() sets here and in the modules it binds in turn."""
        REBIND.keep_bound(self, module)
        for other in ("NAMES", "CSI_RESTORE"):
            if hasattr(module, other):
                REBIND.keep_bound(self, getattr(module, other))
        state = getattr(module, "STATE", None)
        if state is not None:
            REBIND.keep(self, state, [n for n in vars(state) if n.isupper() or n in ("kget", "ksend")])
        if hasattr(module, "_state") and isinstance(module._state, dict):
            saved = dict(module._state)
            self.addCleanup(lambda: (module._state.clear(), module._state.update(saved)))

    def test_a_left_out_argument_goes_back_to_the_module_default(self):
        import importlib
        for name, required, optional in CASES:
            module = importlib.import_module(name)
            for keyword, (value, names) in optional.items():
                with self.subTest(module=name, argument=keyword):
                    self.keep_everything(module)
                    defaults = module._UNBOUND if hasattr(module, "_UNBOUND") else {}
                    module.bind(*required, **{keyword: value})
                    module.bind(*required)
                    for global_name in names:
                        now = getattr(module, global_name)
                        self.assertFalse(now is value or (isinstance(value, tuple) and now in value),
                                         f"{name}.{global_name} kept the last bind's value")
                        self.assertIn(global_name, defaults, f"{name} has no _UNBOUND default for {global_name}")
                        self.assertIs(now, defaults[global_name], f"{name}.{global_name} is not its module default")

    def test_every_name_a_bind_keeps_unless_given_is_covered_here(self):
        """The scan the review used: x = _x or x, if _x: x = _x, and the tuple forms."""
        import ast
        covered = {(n, g) for n, _, optional in CASES for _, names in optional.values() for g in names}
        found = set()
        for path in sorted((Path(__file__).resolve().parents[1] / "server").glob("homestead_*.py")):
            tree = ast.parse(path.read_text())
            for fn in [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "bind"]:
                globs = {g for node in ast.walk(fn) if isinstance(node, ast.Global) for g in node.names}
                for node in ast.walk(fn):
                    pairs = []
                    if isinstance(node, ast.Assign) and len(node.targets) == 1:
                        target, value = node.targets[0], node.value
                        if isinstance(target, ast.Tuple) and isinstance(value, ast.Tuple):
                            pairs = list(zip(target.elts, value.elts))
                        else:
                            pairs = [(target, value)]
                    for target, value in pairs:
                        if isinstance(target, ast.Name) and target.id in globs and isinstance(value, ast.BoolOp) \
                                and isinstance(value.op, ast.Or) \
                                and any(isinstance(v, ast.Name) and v.id == target.id for v in value.values[1:]):
                            found.add((path.stem, target.id))
                    if isinstance(node, ast.If) and not node.orelse:
                        for statement in node.body:
                            if isinstance(statement, ast.Assign):
                                for target in statement.targets:
                                    for t in ([target] if isinstance(target, ast.Name) else getattr(target, "elts", [])):
                                        if isinstance(t, ast.Name) and t.id in globs:
                                            found.add((path.stem, t.id))
        self.assertEqual(set(), found - covered, "a bind() keeps these when they are left out; reset them and add a row")


if __name__ == "__main__":
    unittest.main()
