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


if __name__ == "__main__":
    unittest.main()
