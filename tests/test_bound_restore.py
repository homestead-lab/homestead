"""tests/bound_restore.py: what a test file leaves bound is put back before the next file runs."""
import sys
import time
import types
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import bound_guard as GUARD
import bound_restore as RESTORE

import homestead_auth as AUTH
import homestead_changes as CHANGES
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


class CachesEmptied(unittest.TestCase):
    """The account store cache and the change-author key, after each file."""

    def setUp(self):
        if not RESTORE.installed():
            RESTORE.install()
            self.addCleanup(RESTORE.uninstall)
        self.enterContext(mock.patch.dict(AUTH._store_cache, {"at": 0, "data": None}, clear=True))
        self.enterContext(mock.patch.dict(CHANGES._known, clear=True))

        def forget():
            # The made-up files' records, so the guards at the end of the run do not count them.
            for name in [name for name in RESTORE.caches_left if name.startswith("test_made_up_cache_")]:
                RESTORE.caches_left.pop(name)
            RESTORE.cache_files.difference_update({f"test_made_up_cache_{x}" for x in "ab"})
            for files in (GUARD.ran, GUARD.unchecked):
                files.difference_update({f"test_made_up_cache_{x}" for x in "ab"})
        self.addCleanup(forget)

    def two_files(self, leave):
        """A file whose test runs leave(), then a file that reads both caches."""
        seen = []

        class Leaky(unittest.TestCase):
            def test_leaves(self):
                leave()

        class Next(unittest.TestCase):
            def test_reads(self):
                seen.append((AUTH._store_cache.get("data"), dict(CHANGES._known)))

        Leaky.__module__, Next.__module__ = "test_made_up_cache_a", "test_made_up_cache_b"
        self.assertTrue(run(Leaky, Next).wasSuccessful())
        return seen

    def test_a_keyed_store_one_file_leaves_is_gone_in_the_next_file_and_the_file_is_named(self):
        # What CI's share 4 had before test_revert_fenced (23 Nov): a store read moments ago, with a key.
        seen = self.two_files(lambda: AUTH._store_cache.update(
            at=time.time(), data={"users": {}, "signing_key": "example-key"}))
        self.assertEqual([(None, {})], seen)
        self.assertEqual({"test_made_up_cache_a": {"homestead_auth._store_cache"}},
                         {f: n for f, n in RESTORE.caches_left.items() if f.startswith("test_made_up_cache_")})
        self.assertEqual(["test_made_up_cache_a: homestead_auth._store_cache"],
                         RESTORE.new_cache_leaks({"test_made_up_cache_a": {"homestead_auth._store_cache"}}, frozenset()))

    def test_a_key_left_in_the_change_author_cache_is_gone_too(self):
        seen = self.two_files(lambda: CHANGES._known.update(key=b"k" * 32, users=[], api_keys=[]))
        self.assertEqual([(None, {})], seen)
        self.assertIn("homestead_changes._known", RESTORE.caches_left["test_made_up_cache_a"])

    def test_with_no_key_left_the_next_file_writes_no_mark(self):
        # The clock race of test_revert_fenced needs a key from an earlier file: with none, a
        # Deployment write through the write guard carries no mark at all.
        bodies = []

        class Leaky(unittest.TestCase):
            def test_leaves(self):
                AUTH._store_cache.update(at=time.time(), data={"users": {}, "signing_key": "example-key"})
                CHANGES._accounts()

        class Next(unittest.TestCase):
            def test_writes(self):
                body = {"spec": {"replicas": 3}}
                with mock.patch.object(AUTH, "kget", side_effect=OSError("no cluster here")):
                    CHANGES.note_write("PATCH", "/apis/apps/v1/namespaces/lab/deployments/app-a", body)
                bodies.append(body)

        Leaky.__module__, Next.__module__ = "test_made_up_cache_a", "test_made_up_cache_b"
        self.assertTrue(run(Leaky, Next).wasSuccessful())
        self.assertNotIn(CHANGES.AUTHOR, (bodies[0].get("metadata") or {}).get("annotations") or {})

    def test_a_listed_file_is_tolerated_and_one_that_no_longer_leaks_is_named(self):
        left = {"test_made_up_cache_a": {"homestead_auth._store_cache"}}
        listed = frozenset({"test_made_up_cache_a", "test_made_up_cache_b", "test_made_up_cache_c"})
        self.assertEqual([], RESTORE.new_cache_leaks(left, listed))
        checked = {"test_made_up_cache_a", "test_made_up_cache_b"}
        self.assertEqual(["test_made_up_cache_b"], RESTORE.stale_cache_leaks(left, checked, listed),
                         "b ran and left nothing; c did not run here, so it says nothing")

    def test_a_pending_file_is_not_stale_and_pending_entries_have_a_ceiling_of_their_own(self):
        left, checked = {}, {"test_made_up_cache_a"}
        listed = frozenset({"test_made_up_cache_a", "test_made_up_cache_b"})
        self.assertEqual([], RESTORE.stale_cache_leaks(left, checked, listed, frozenset({"test_made_up_cache_a"})),
                         "an open PR of ours fixes a: it ran clean, and is not stale while pending")
        self.assertEqual(["test_made_up_cache_a"], RESTORE.stale_cache_leaks(left, checked, listed),
                         "once off the pending list (its PR merged), the same run names it")
        pending = frozenset({"test_made_up_cache_a"})
        self.assertEqual([], RESTORE.cache_ceiling_faults(listed, pending, 2, 1), "every listed file counts")
        self.assertEqual(["CACHE_LEAKS has 2, ceiling 1: it may only shrink, fix the leak instead"],
                         RESTORE.cache_ceiling_faults(listed, pending, 1, 1))
        self.assertEqual(["pending entries has 0, ceiling 1: lower the ceiling to match"],
                         RESTORE.cache_ceiling_faults(listed, frozenset(), 2, 1))
        self.assertEqual(["pending entries has 1, ceiling 0: it may only shrink, fix the leak instead"],
                         RESTORE.cache_ceiling_faults(listed, pending, 2, 0))
        self.assertEqual(["test_made_up_cache_c is pending but not on CACHE_LEAKS"],
                         RESTORE.cache_ceiling_faults(listed, frozenset({"test_made_up_cache_c"}), 2, 1))

    def test_a_listed_file_not_in_the_tree_yet_is_neither_stale_nor_named(self):
        # An open PR of ours adds test_made_up_cache_b: listed before it arrives. It never ran here.
        listed = frozenset({"test_made_up_cache_a", "test_made_up_cache_b"})
        left, checked = {"test_made_up_cache_a": {"homestead_auth._store_cache"}}, {"test_made_up_cache_a"}
        self.assertEqual([], RESTORE.stale_cache_leaks(left, checked, listed))
        self.assertEqual([], RESTORE.new_cache_leaks(left, listed))
        self.assertEqual([], RESTORE.cache_ceiling_faults(listed, frozenset({"test_made_up_cache_b"}), 2, 1),
                         "counted from the start, so its merge changes no count")

    def test_a_listed_file_that_arrives_and_leaks_is_tolerated_and_an_unlisted_one_still_fails(self):
        listed = frozenset({"test_made_up_cache_a"})   # a arrives with its PR; b is listed nowhere
        self.two_files(lambda: AUTH._store_cache.update(at=time.time(), data={"users": {}}))
        left = {f: n for f, n in RESTORE.caches_left.items() if f.startswith("test_made_up_cache_")}
        self.assertEqual([], RESTORE.new_cache_leaks(left, listed))
        self.assertEqual([], RESTORE.stale_cache_leaks(left, RESTORE.cache_files, listed))
        self.assertEqual(["test_made_up_cache_a: homestead_auth._store_cache"], RESTORE.new_cache_leaks(left, frozenset()),
                         "not listed: it fails, named")

    def test_a_file_that_leaves_nothing_is_not_named(self):
        self.two_files(lambda: None)
        self.assertNotIn("test_made_up_cache_a", RESTORE.caches_left)
        self.assertIn("test_made_up_cache_a", RESTORE.cache_files)


if __name__ == "__main__":
    unittest.main()
