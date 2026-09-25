import sys, unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
import server


class SelfGuardTests(unittest.TestCase):
    """Stop on Homestead's own card took the page down with one click, and
    nothing on the page could bring it back."""

    def setUp(self):
        self.saved = server.SELF.NS
        server.SELF.NS = "homestead"

    def tearDown(self):
        server.SELF.NS = self.saved

    def test_stopping_homestead_says_what_it_does_and_how_to_undo_it(self):
        with self.assertRaisesRegex(ValueError, r"takes this page down.*kubectl -n homestead scale deployment/homestead --replicas=1"):
            server.guard_self("homestead", "homestead", stopping=True)

    def test_a_confirmed_stop_goes_ahead(self):
        server.guard_self("homestead", "homestead", stopping=True, confirmed=True)

    def test_restarting_is_fine(self):
        server.guard_self("homestead", "homestead")

    def test_delete_rename_and_storage_moves_are_refused(self):
        for kind, pattern in (("deleting", "cannot delete itself"), ("renaming", "cannot rename itself"),
                              ("moving", "Redundancy")):
            with self.subTest(kind), self.assertRaisesRegex(ValueError, pattern):
                server.guard_self("homestead", "homestead", confirmed=True, **{kind: True})

    def test_other_workloads_are_untouched(self):
        server.guard_self("lab", "homestead", stopping=True, deleting=True)
        server.guard_self("homestead", "samba", stopping=True, renaming=True)

    def test_the_workload_list_marks_homestead_itself(self):
        self.assertTrue(server.is_self("homestead", "homestead"))
        self.assertFalse(server.is_self("lab", "plex"))


if __name__ == "__main__":
    unittest.main()
