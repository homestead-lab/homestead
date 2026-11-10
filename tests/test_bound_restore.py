"""tests/bound_restore.py: what a test file leaves bound is put back before the next file runs."""
import sys
import types
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import bound_guard as GUARD
import bound_restore as RESTORE

import homestead_push as PUSH
import homestead_uplinks as UPLINKS

FAKE = ("homestead_uplinks.node_probes", "test_made_up_restore_a")


def run(*cases):
    suite = unittest.TestSuite()
    for case in cases:
        suite.addTests(unittest.defaultTestLoader.loadTestsFromTestCase(case))
    with open("/dev/null", "w") as quiet:
        return unittest.TextTestRunner(stream=quiet).run(suite)


class PutBack(unittest.TestCase):
    def setUp(self):
        if not RESTORE.installed():
            RESTORE.install()
            self.addCleanup(RESTORE.uninstall)
        saved = UPLINKS.node_probes, PUSH.CONTACT

        def forget():
            # The made-up files' records, so the guards at the end of the run do not count them.
            UPLINKS.node_probes, PUSH.CONTACT = saved
            for found in (GUARD.left, GUARD.others):
                for pair in [pair for pair in found if pair[1].startswith("test_made_up_restore_")]:
                    found.pop(pair)
            for files in (GUARD.ran, GUARD.unchecked):
                files.difference_update({f"test_made_up_restore_{x}" for x in "abcd"})
        self.addCleanup(forget)

    def test_a_fake_one_file_leaves_is_gone_in_the_next_file(self):
        original, seen = UPLINKS.node_probes, []

        class Leaky(unittest.TestCase):
            def test_leaks(self):
                UPLINKS.node_probes = lambda: {"made": "up"}
                PUSH.CONTACT = "https://leak.example.com"

        class Next(unittest.TestCase):
            def test_sees(self):
                seen.append((UPLINKS.node_probes, PUSH.CONTACT))

        Leaky.__module__, Next.__module__ = "test_made_up_restore_a", "test_made_up_restore_b"
        contact = PUSH.CONTACT
        self.assertTrue(run(Leaky, Next).wasSuccessful())
        self.assertEqual([(original, contact)], seen)
        self.assertIs(original, UPLINKS.node_probes, "the last file is put back too")

    def test_a_leftover_is_seen_by_the_next_test_of_the_same_file(self):
        # Within a file nothing changes: the restore runs when the file is done.
        seen = []

        class Leaky(unittest.TestCase):
            def test_1_leaks(self):
                UPLINKS.node_probes = fake

            def test_2_sees(self):
                seen.append(UPLINKS.node_probes)

        fake = lambda: {"made": "up"}
        Leaky.__module__ = "test_made_up_restore_a"
        run(Leaky)
        self.assertEqual([fake], seen)

    def test_what_setUpModule_binds_is_put_back_after_the_file(self):
        module = types.ModuleType("test_made_up_restore_c")
        module.setUpModule = lambda: setattr(PUSH, "CONTACT", "https://module.example.com")
        sys.modules[module.__name__] = module
        self.addCleanup(sys.modules.pop, module.__name__, None)
        contact, seen = PUSH.CONTACT, []

        class InModule(unittest.TestCase):
            def test_sees_it(self):
                seen.append(PUSH.CONTACT)

        class Next(unittest.TestCase):
            def test_sees(self):
                seen.append(PUSH.CONTACT)

        InModule.__module__, Next.__module__ = module.__name__, "test_made_up_restore_d"
        self.assertTrue(run(InModule, Next).wasSuccessful())
        self.assertEqual(["https://module.example.com", contact], seen)

    def test_a_file_that_imports_server_first_keeps_what_server_bound(self):
        # server binds every module when it is first imported: that is the real wiring, not a leak.
        server = sys.modules.pop("server", None)
        self.addCleanup(lambda: sys.modules.update(server=server) if server else sys.modules.pop("server", None))
        fake, seen = (lambda: {"made": "by server"}), []

        class ImportsServer(unittest.TestCase):
            def test_imports(self):
                sys.modules["server"] = server or types.ModuleType("server")
                UPLINKS.node_probes = fake

        class Next(unittest.TestCase):
            def test_sees(self):
                seen.append(UPLINKS.node_probes)

        ImportsServer.__module__, Next.__module__ = "test_made_up_restore_a", "test_made_up_restore_b"
        run(ImportsServer, Next)
        self.assertEqual([fake], seen)


    def test_a_fake_left_within_a_file_still_fails_the_bound_state_guard(self):
        # The restore only stops a leftover reaching the next file: within a file the next
        # test would still see it, so the guard fails it as before (keep_bound stays required).
        import test_zz_bound_state_clean as CLEAN

        class Leaky(unittest.TestCase):
            def test_1_leaks(self):
                UPLINKS.node_probes = lambda: {"made": "up"}

            def test_2_leans_on_it(self):
                self.assertEqual({"made": "up"}, UPLINKS.node_probes())

        Leaky.__module__ = "test_made_up_restore_a"
        self.assertTrue(run(Leaky).wasSuccessful())
        self.assertIn(FAKE, GUARD.left)
        check = run(CLEAN.BoundStateClean)
        self.assertEqual(["test_no_test_left_a_fake_that_is_not_listed"],
                         [test._testMethodName for test, _ in check.failures])


if __name__ == "__main__":
    unittest.main()
