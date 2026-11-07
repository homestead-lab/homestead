"""tests/bound_guard.py's checks, on made-up runs, and its recorder on a real leak."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import bound_guard as GUARD

LISTED = {("homestead_addons.kget", "test_addons"), ("homestead_uplinks.node_probes", "test_uplinks")}


class Checks(unittest.TestCase):
    def test_a_listed_name_left_changed_by_another_file_is_a_new_leak(self):
        found = {("homestead_addons.kget", "test_addons"): "a", ("homestead_addons.kget", "test_new_file"): "b"}
        self.assertEqual([("homestead_addons.kget", "test_new_file")], GUARD.new_leaks(found, LISTED))

    def test_a_listed_pair_whose_file_ran_and_no_longer_leaks_is_stale(self):
        found = {("homestead_addons.kget", "test_addons"): "a"}
        self.assertEqual([("homestead_uplinks.node_probes", "test_uplinks")],
                         GUARD.stale(found, LISTED, {"test_addons", "test_uplinks"}, set(),
                                     {"homestead_addons", "homestead_uplinks"}))

    def test_a_pair_is_not_stale_when_the_name_held_a_fake_after_that_files_test(self):
        # The same shared fake, left first by another file: no change to see, but still a leak there.
        held = {("homestead_uplinks.node_probes", "test_uplinks")}
        self.assertEqual([], GUARD.stale({("homestead_addons.kget", "test_addons"): "a"}, LISTED,
                                         {"test_addons", "test_uplinks"}, set(),
                                         {"homestead_addons", "homestead_uplinks"}, held))

    def test_a_pair_is_not_stale_when_its_file_did_not_run_or_was_not_checked(self):
        found = {}
        covered = {"homestead_addons", "homestead_uplinks"}
        self.assertEqual([], GUARD.stale(found, LISTED, set(), set(), covered))
        self.assertEqual([], GUARD.stale(found, LISTED, {"test_addons", "test_uplinks"}, {"test_addons", "test_uplinks"}, covered))
        self.assertEqual([], GUARD.stale(found, LISTED, {"test_addons", "test_uplinks"}, set(), set()))

    def test_a_module_this_run_did_not_load_is_not_unknown(self):
        # A small run (one file and the guards) loads few modules: their listed names are not checked.
        self.assertEqual([], GUARD.unknown(LISTED, {}, set()))
        self.assertEqual(["homestead_uplinks.node_probes"],
                         GUARD.unknown(LISTED, {"homestead_addons.kget": None}, {"homestead_addons", "homestead_uplinks"}))


class Ratchet(unittest.TestCase):
    """What BindRatchet counts as a file that binds, and as a restore."""

    def judge(self, body, imports="import homestead_addons as A\n"):
        return GUARD.binds_without_restoring(imports + body)

    def test_a_module_bound_with_no_restore_is_found(self):
        self.assertTrue(self.judge("def setUp(self):\n    A.bind(1)\n"))
        self.assertTrue(self.judge("def setUp(self):\n    server.LC.bind(1)\n", "import server\n"))

    def test_a_socket_bind_is_not_a_module_bind(self):
        self.assertFalse(self.judge('def setUp(self):\n    sock.bind(("127.0.0.1", 0))\n'))

    def test_the_word_in_a_comment_or_a_string_is_no_restore(self):
        self.assertTrue(self.judge('def setUp(self):\n    # restore it\n    """rebind"""\n    A.bind(1)\n'))

    def test_code_that_puts_it_back_is_a_restore(self):
        for line in ("self.addCleanup(f)", "keep_bound(self, A)", "self.restore_it()"):
            with self.subTest(line=line):
                self.assertFalse(self.judge(f"def setUp(self):\n    {line}\n    A.bind(1)\n"))
class FromOpenPrs(unittest.TestCase):
    """A line for a leak that comes with an open PR of ours (its test file is not on dev
    yet, or not in that form): allowed, never counted against the list, never stale."""

    def test_a_line_from_an_open_pr_is_allowed_and_known_as_pending(self):
        import tempfile
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "leaks.txt"
            path.write_text("# note\n"
                            "homestead_addons.kget\ttest_addons\tfake\n"
                            "homestead_revert.lh_call\ttest_revert_stop\tfake\t"
                            "from #1100, not on dev yet: remove once it merges with a keep_bound there\n",
                            encoding="utf-8")
            self.assertEqual({("homestead_addons.kget", "test_addons", "fake"),
                              ("homestead_revert.lh_call", "test_revert_stop", "fake")}, GUARD.allowed(path))
            self.assertEqual({("homestead_revert.lh_call", "test_revert_stop")}, GUARD.pending(path))
            self.assertEqual([], GUARD.bad_notes(path))

    def test_a_note_that_names_no_pr_number_is_refused(self):
        import tempfile
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "leaks.txt"
            path.write_text("homestead_a.x\ttest_a\tfake\tfrom #, not on dev yet\n"
                            "homestead_b.y\ttest_b\tfake\tfrom #12a, not on dev yet\n"
                            "homestead_c.z\ttest_c\tfake\tsee later\n"
                            "homestead_d.w\ttest_d\tfake\tfrom #1100, not on dev yet\n", encoding="utf-8")
            self.assertEqual(["homestead_a.x\ttest_a", "homestead_b.y\ttest_b", "homestead_c.z\ttest_c"],
                             GUARD.bad_notes(path))
            self.assertEqual({("homestead_d.w", "test_d")}, GUARD.pending(path))

    def test_each_check_reads_its_own_part_of_the_list(self):
        listed = {("homestead_a.x", "test_a", "fake"), ("homestead_a.y", "test_a", "other"),
                  ("homestead_b.z", "test_b", "fake")}
        sides = GUARD.sides(listed, {("homestead_b.z", "test_b")})
        # The fake check allows every listed fake, the open PR's too.
        self.assertEqual({("homestead_a.x", "test_a"), ("homestead_b.z", "test_b")}, sides.fakes)
        # CEILING, the stale check and the still-bound check see only what is on dev.
        self.assertEqual({("homestead_a.x", "test_a", "fake"), ("homestead_a.y", "test_a", "other")}, sides.on_dev)
        self.assertEqual({("homestead_a.x", "test_a")}, sides.stale_fakes)
        self.assertEqual({("homestead_b.z", "test_b", "fake")}, sides.pending)


class Kinds(unittest.TestCase):
    """What a test leaves is a fake (enforced) or another value (printed only)."""

    def test_every_fake_is_credited_to_the_test_file_that_left_it(self):
        from unittest import mock
        import rebind as REBIND
        self.assertEqual("test_running", GUARD.fake_origin(lambda: None, "test_running"))   # this file's own code
        self.assertEqual("test_running", GUARD.fake_origin(mock.Mock(), "test_running"))
        self.assertEqual("test_running", GUARD.fake_origin(REBIND.keep, "test_running"))   # a shared helper's

    def test_a_path_under_the_temp_folder_is_a_fake(self):
        import os, tempfile
        self.assertEqual("test_running", GUARD.fake_origin(os.path.join(tempfile.gettempdir(), "x"), "test_running"))
        self.assertIsNone(GUARD.fake_origin("/data", "test_running"))

    def test_a_server_function_or_a_plain_value_is_no_fake(self):
        import homestead_push as PUSH
        self.assertIsNone(GUARD.fake_origin(PUSH.bind, "test_running"))
        self.assertIsNone(GUARD.fake_origin("/data", "test_running"))
        self.assertIsNone(GUARD.fake_origin({}, "test_running"))

    def test_a_listed_name_left_with_a_new_files_fake_fails_but_a_reset_to_default_does_not(self):
        listed = {("homestead_addons.kget", "test_addons")}
        fakes = {("homestead_addons.kget", "test_new_file"): "t"}
        others = {("homestead_addons.ksend", "test_new_file"): "t"}
        self.assertEqual([("homestead_addons.kget", "test_new_file")], GUARD.new_leaks(fakes, listed))
        self.assertEqual([], GUARD.new_leaks({}, listed), "a reset to default (an other change) is not checked here")
        self.assertTrue(others)   # printed by the guard, not enforced

    def test_the_stale_check_needs_the_whole_suite_in_one_process(self):
        self.assertTrue(GUARD.full_run({"test_a", "test_b"}, {"test_a", "test_b"}))
        self.assertFalse(GUARD.full_run({"test_a"}, {"test_a", "test_b"}))

    def test_a_discover_shaped_run_reaches_the_stale_check(self):
        # The guard runs before the files that sort after it (test_zz_guard_state_clean and
        # the like), so those cannot have run yet: they are not part of "the whole suite" here.
        every = ["test_a", "test_b", "test_zz_bound_state_clean", "test_zz_guard_state_clean", "test_zz_network_refused"]
        before = GUARD.files_before(every, "test_zz_bound_state_clean")
        self.assertEqual({"test_a", "test_b"}, before)
        self.assertTrue(GUARD.full_run({"test_a", "test_b", "test_zz_bound_state_clean"}, before))


class Recorder(unittest.TestCase):
    def test_a_fake_is_credited_to_the_file_that_left_it_and_another_change_only_noted(self):
        import homestead_push as PUSH
        import homestead_uplinks as UPLINKS
        if not GUARD._installed:
            GUARD.install()
            self.addCleanup(lambda: (setattr(unittest.TestCase, "run", GUARD._installed.pop("run"))))
        saved = UPLINKS.node_probes, PUSH.CONTACT

        class Leaky(unittest.TestCase):
            def test_leaks(self):
                UPLINKS.node_probes = lambda: {"made": "up"}     # a fake (its code is under tests/)
                PUSH.CONTACT = "https://leak.example.com"        # a plain value

        Leaky.__module__ = "test_made_up_leaker"
        unittest.TextTestRunner(stream=open("/dev/null", "w")).run(unittest.defaultTestLoader.loadTestsFromTestCase(Leaky))
        fake = ("homestead_uplinks.node_probes", "test_made_up_leaker")   # the file that left it
        other = ("homestead_push.CONTACT", "test_made_up_leaker")
        self.assertIn(fake, GUARD.left)
        self.assertIn(other, GUARD.others)
        GUARD.left.pop(fake)
        GUARD.others.pop(other)
        GUARD.ran.discard("test_made_up_leaker")
        UPLINKS.node_probes, PUSH.CONTACT = saved   # the recorder only records: put the made-up leaks back here

    def test_a_fake_that_was_already_bound_is_reported_not_enforced(self):
        # A restore that puts back an object another test made (the cascade through
        # SHARES.bind -> NAMES.bind) is not this file's fake.
        import homestead_networking as NETWORKING
        import homestead_uplinks as UPLINKS
        if not GUARD._installed:
            GUARD.install()
            self.addCleanup(lambda: (setattr(unittest.TestCase, "run", GUARD._installed.pop("run"))))
        saved = UPLINKS.node_probes, NETWORKING.node_probes
        other_files_fake = lambda: {"made": "elsewhere"}
        NETWORKING.node_probes = other_files_fake      # bound already when the test starts

        class Restorer(unittest.TestCase):
            def test_moves(self):
                UPLINKS.node_probes = other_files_fake   # a change, to a fake it did not make

        Restorer.__module__ = "test_made_up_restorer"
        unittest.TextTestRunner(stream=open("/dev/null", "w")).run(
            unittest.defaultTestLoader.loadTestsFromTestCase(Restorer))
        pair = ("homestead_uplinks.node_probes", "test_made_up_restorer")
        self.assertNotIn(pair, GUARD.left, "not enforced")
        self.assertIn(pair, GUARD.others, "but reported")
        GUARD.others.pop(pair, None)
        GUARD.ran.discard("test_made_up_restorer")
        UPLINKS.node_probes, NETWORKING.node_probes = saved

    def test_bound_targets_include_what_bind_sets_on_another_object(self):
        import homestead_mcp as MCP
        import homestead_networking as NETWORK
        import rebind as REBIND
        self.assertLessEqual({"STATE.kget", "STATE.ksend", "STATE.SHARED_VIP"}, set(REBIND.bound_targets(NETWORK)))
        self.assertLessEqual({"ALERTS.AGENTS[0]", "_settings[0]"}, set(REBIND.bound_targets(MCP)))


MADE_UP_MODULE = '''
KIND = "made-up-job"
NEEDS = ("reader", "writer")
RESOLVERS = {}
_bound = {"kget": None, "ksend": None}
_extra = {"plan": None}
_given = {}


def bind(kget=None, ksend=None, plan=None, helpers=None, **given):
    for key, value in (("kget", kget), ("ksend", ksend)):
        if value:
            _bound[key] = value
    _extra.update(plan=plan)
    RESOLVERS[KIND] = plan
    _given.update(given)
    if helpers:
        globals().update({name: helpers[name] for name in NEEDS})
'''

# A module whose bind() sets nothing but entries of a dict that starts empty
# (homestead_api_v1's ctx): before the test, the guard sees no name of it at all.
ONLY_A_DICT = '''
_given = {}


def bind(**given):
    _given.update(given)
'''


# A module whose bind_routes() sets the names its VALUES lists with globals().update(values)
# (homestead_kube's API, CTX and TOKEN); whose bind() sets an entry under a key it is given
# (neither a constant nor a loop variable), and resets a runtime cache to constants
# (homestead_uptime's _state): keep_bound puts that back, the guard does not report it.
VALUES_MODULE = '''
VALUES = ("API", "TOKEN")
API = "https://example.com"
TOKEN = "made-up"
_table = {}
_named = {}
_state = {"loaded": True, "apps": {"a": 1}}
_seen = {"at": 0.0}
_cache = {"at": 1.0}


def bind(name="first", handler=None, at=0.0):
    _table[name] = handler
    _named[name.upper()] = handler
    _state.update(loaded=False, apps={})
    _seen.update({"at": 0.0, "hosts": []})
    _cache.update(at=at)


def load():
    _state["loaded"], _state["apps"] = True, {"b": 2}


def bind_routes(helpers, **values):
    if set(values) != set(VALUES):
        raise TypeError("bound with " + ", ".join(sorted(set(values) ^ set(VALUES))))
    globals().update(values)
'''


class DictEntries(unittest.TestCase):
    """What a bind() sets in a dict without naming the key on that line: in a loop over
    a fixed tuple, with .update(), under a module constant, or with globals().update()."""

    def setUp(self):
        import importlib
        import tempfile
        folder = self.enterContext(tempfile.TemporaryDirectory())
        for name, source in (("homestead_made_up_dicts", MADE_UP_MODULE), ("homestead_made_up_only_dict", ONLY_A_DICT),
                             ("homestead_made_up_values", VALUES_MODULE)):
            (Path(folder) / f"{name}.py").write_text(source, encoding="utf-8")
            self.addCleanup(sys.modules.pop, name, None)
            self.addCleanup(GUARD._targets.pop, name, None)
        sys.path.insert(0, folder)
        self.addCleanup(sys.path.remove, folder)
        importlib.invalidate_caches()
        import homestead_made_up_dicts as MADE_UP
        import homestead_made_up_only_dict as ONLY
        import homestead_made_up_values as VALUES
        self.module, self.only, self.values = MADE_UP, ONLY, VALUES
        MADE_UP.bind(kget=len, ksend=print, plan=min, helpers={"reader": max, "writer": sum})

    def record(self, leak):
        """The (name, test file) pairs the recorder credits as fakes to a made-up test file
        whose one test runs leak(); the recorder's state is put back as it was."""
        if not GUARD._installed:
            GUARD.install()
            self.addCleanup(lambda: (setattr(unittest.TestCase, "run", GUARD._installed.pop("run"))))

        class Leaky(unittest.TestCase):
            def test_leaks(self):
                leak()

        Leaky.__module__ = "test_made_up_dict_leaker"
        unittest.TextTestRunner(stream=open("/dev/null", "w")).run(
            unittest.defaultTestLoader.loadTestsFromTestCase(Leaky))
        found = {pair for pair in GUARD.left if pair[1] == "test_made_up_dict_leaker"}
        # The recorder only records: take the made-up pairs out again.
        for record in (GUARD.left, GUARD.others):
            for key in [key for key in record if key[1] == "test_made_up_dict_leaker"]:
                record.pop(key)
        GUARD.ran.discard("test_made_up_dict_leaker")
        GUARD.covered.difference_update({"homestead_made_up_dicts", "homestead_made_up_only_dict",
                                         "homestead_made_up_values"})
        return found

    def test_bound_targets_name_each_entry(self):
        import rebind as REBIND
        self.assertLessEqual({"_bound['kget']", "_bound['ksend']", "_extra['plan']", "RESOLVERS['made-up-job']",
                              "reader", "writer", "_given[*]"}, set(REBIND.bound_targets(self.module)))

    def test_keep_bound_puts_back_an_entry_set_in_a_loop_and_one_set_by_update(self):
        import rebind as REBIND
        module = self.module
        given = module._given

        class Leaky(unittest.TestCase):
            def test_leaks(self):
                REBIND.keep_bound(self, module)
                module.bind(kget=lambda path: {}, plan=lambda: "fake", helpers={"reader": id, "writer": id}, more=id)

        result = unittest.TextTestRunner(stream=open("/dev/null", "w")).run(
            unittest.defaultTestLoader.loadTestsFromTestCase(Leaky))
        self.assertTrue(result.wasSuccessful())
        self.assertEqual({"kget": len, "ksend": print}, module._bound)
        self.assertEqual({"plan": min}, module._extra)
        self.assertEqual({"made-up-job": min}, module.RESOLVERS)
        self.assertEqual({}, module._given, "an entry the test added is taken out again")
        self.assertIs(given, module._given, "the same dict, which other modules may hold, is put back in place")
        self.assertEqual((max, sum), (module.reader, module.writer))

    def test_the_recorder_sees_a_fake_left_in_a_loop_set_entry_and_in_an_added_one(self):
        module = self.module
        saved = dict(module._bound)

        def leak():
            module.bind(kget=lambda path: {"made": "up"}, more=lambda: None)   # fakes, never put back
        found = self.record(leak)
        module._bound.update(saved)
        module._given.clear()
        self.assertEqual({("homestead_made_up_dicts._bound['kget']", "test_made_up_dict_leaker"),
                          ("homestead_made_up_dicts._given['more']", "test_made_up_dict_leaker")}, found,
                         "an entry set in a loop, and one the test added, are seen")

    def test_the_recorder_sees_an_entry_added_to_a_modules_only_dict_while_it_was_empty(self):
        only = self.only

        def leak():
            only.bind(kget=lambda path: {"made": "up"})   # a fake, never put back
        found = self.record(leak)
        only._given.clear()
        self.assertEqual({("homestead_made_up_only_dict._given['kget']", "test_made_up_dict_leaker")}, found)

    def test_bound_targets_name_what_globals_update_values_sets_from_VALUES(self):
        import rebind as REBIND
        self.assertLessEqual({"API", "TOKEN"}, set(REBIND.bound_targets(self.values)))

    def test_keep_bound_puts_back_a_name_bind_routes_set_from_VALUES(self):
        import rebind as REBIND
        module = self.values

        class Leaky(unittest.TestCase):
            def test_leaks(self):
                REBIND.keep_bound(self, module)
                module.bind_routes({}, API="https://fake.example.com", TOKEN="fake")

        result = unittest.TextTestRunner(stream=open("/dev/null", "w")).run(
            unittest.defaultTestLoader.loadTestsFromTestCase(Leaky))
        self.assertTrue(result.wasSuccessful())
        self.assertEqual(("https://example.com", "made-up"), (module.API, module.TOKEN))

    def test_the_recorder_sees_a_fake_left_in_a_name_set_from_VALUES(self):
        module = self.values

        def leak():
            module.bind_routes({}, API=lambda: "fake", TOKEN="made-up")   # a fake, never put back
        found = self.record(leak)
        module.API = "https://example.com"
        self.assertEqual({("homestead_made_up_values.API", "test_made_up_dict_leaker")}, found)

    def test_an_entry_under_a_key_bind_is_given_is_kept_as_the_whole_dict(self):
        import rebind as REBIND
        self.assertLessEqual({"_table[*]", "_named[*]"}, set(REBIND.bound_targets(self.values)))
        module = self.values

        class Leaky(unittest.TestCase):
            def test_leaks(self):
                REBIND.keep_bound(self, module)
                module.bind(name="second", handler=id)

        unittest.TextTestRunner(stream=open("/dev/null", "w")).run(
            unittest.defaultTestLoader.loadTestsFromTestCase(Leaky))
        self.assertEqual({}, module._table, "the entry the test added is taken out again")
        self.assertEqual({}, module._named)

    def test_an_update_that_resets_a_cache_to_constants_is_kept_but_not_reported(self):
        import rebind as REBIND
        kept = set(REBIND.bound_targets(self.values))
        reported = set(REBIND.bound_targets(self.values, resets=False))
        resets = {"_state['loaded']", "_state['apps']", "_seen['at']", "_seen['hosts']"}
        self.assertLessEqual(resets, kept, "keep_bound puts a reset cache back: it goes with what bind() was given")
        self.assertEqual(set(), resets & reported, "the guard does not report a reset to constants (keywords or a dict)")
        self.assertIn("_cache['at']", reported, "an .update() with a value from bind()'s arguments is reported")

    def test_keep_bound_puts_back_a_cache_that_bind_reset_and_a_load_filled(self):
        import rebind as REBIND
        module = self.values
        apps = module._state["apps"]

        class Leaky(unittest.TestCase):
            def test_leaks(self):
                REBIND.keep_bound(self, module)
                module.bind()
                module.load()

        unittest.TextTestRunner(stream=open("/dev/null", "w")).run(
            unittest.defaultTestLoader.loadTestsFromTestCase(Leaky))
        self.assertEqual({"loaded": True, "apps": {"a": 1}}, module._state)
        self.assertIs(apps, module._state["apps"])


class RealResets(unittest.TestCase):
    def test_keep_bound_puts_back_uptimes_state_after_a_test_that_binds_and_loads(self):
        """homestead_uptime.bind() resets _state; _load() fills it from DATA_DIR. A test that
        binds a temp folder and loads must not leave that folder's apps for later tests."""
        import json
        import tempfile
        import rebind as REBIND
        import homestead_uptime as UP
        before = dict(UP._state)

        class Loads(unittest.TestCase):
            def test_loads(self):
                folder = self.enterContext(tempfile.TemporaryDirectory())
                REBIND.keep_bound(self, UP)
                UP.bind(folder)
                Path(folder, "uptime.json").write_text(json.dumps({"format": 1, "apps": {"made-up": {}}}),
                                                       encoding="utf-8")
                self.assertIn("made-up", UP._load())

        result = unittest.TextTestRunner(stream=open("/dev/null", "w")).run(
            unittest.defaultTestLoader.loadTestsFromTestCase(Loads))
        self.assertTrue(result.wasSuccessful())
        self.assertEqual(before, UP._state)
        self.assertIs(before["apps"], UP._state["apps"])


class ValuesRatchet(unittest.TestCase):
    def test_each_globals_update_of_bind_routes_values_resolves_to_values_names(self):
        """globals().update(values) in a bind()/bind_routes() is seen only through the module
        tuple the function compares values with (VALUES): a new way of checking the keys
        must not make rebind drop the names silently."""
        import ast
        import importlib
        import inspect
        import textwrap
        import rebind as REBIND
        server = Path(__file__).resolve().parents[1] / "server"
        seen, unresolved = [], []
        for path in sorted(server.glob("homestead_*.py")):
            if "globals().update(" not in path.read_text(encoding="utf-8"):
                continue
            module = importlib.import_module(path.stem)
            for fn in ("bind", "bind_routes"):
                function = getattr(module, fn, None)
                if not (inspect.isfunction(function) and function.__module__ == module.__name__):
                    continue
                tree = ast.parse(textwrap.dedent(inspect.getsource(function)))
                calls = [node for node in ast.walk(tree) if isinstance(node, ast.Call)
                         and isinstance(node.func, ast.Attribute) and node.func.attr == "update"
                         and isinstance(node.func.value, ast.Call) and getattr(node.func.value.func, "id", "") == "globals"
                         and any(isinstance(arg, ast.Name) for arg in node.args)]
                if not calls:
                    continue
                seen.append(f"{path.stem}.{fn}")
                listed = [getattr(module, name.split(":", 1)[1], None) for name in REBIND._attributes_of(function)
                          if name.startswith("@globals:")]
                values = {n for names in listed if isinstance(names, (tuple, list, frozenset))
                          for n in names if isinstance(n, str)}
                if not values or not values <= set(REBIND.bound_targets(module)):
                    unresolved.append(f"{path.stem}.{fn}")
        self.assertGreaterEqual(len(seen), 11, "the eleven bind_routes() with globals().update(values) are found")
        self.assertEqual([], unresolved, "check values' keys with `if set(values) != set(VALUES)`, as the others do")


if __name__ == "__main__":
    unittest.main()
