import pathlib
import sys
import tempfile
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "scripts"))
import run_tests as RUN


class ShardTests(unittest.TestCase):
    def test_every_test_file_runs_in_exactly_one_share(self):
        files = RUN.test_files()
        for count in (1, 3, 4):
            shares = [RUN.shard(files, k, count) for k in range(1, count + 1)]
            flat = [path for share in shares for path in share]
            self.assertEqual(sorted(files), sorted(flat), count)

    def test_shares_are_the_same_on_every_run(self):
        files = RUN.test_files()
        self.assertEqual(RUN.shard(files, 2, 4), RUN.shard(list(reversed(files)), 2, 4))


class TimedShardTests(unittest.TestCase):
    """Shares balanced by the seconds in tests/durations.json, with the file
    size standing in for a file that has no recorded time."""

    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = pathlib.Path(folder.name)

    def make(self, sizes):
        files = []
        for name, size in sizes.items():
            (self.root / name).write_text("x" * size)
            files.append(self.root / name)
        return files

    def shares(self, files, count, durations):
        return [[p.name for p in RUN.shard(files, k, count, durations)] for k in range(1, count + 1)]

    def test_recorded_seconds_decide_the_shares_not_the_size(self):
        # By size the big file is heavy; by time the small one is.
        files = self.make({"test_big.py": 9000, "test_slow.py": 100, "test_a.py": 500, "test_b.py": 500})
        durations = {"test_big.py": 1.0, "test_slow.py": 40.0, "test_a.py": 1.0, "test_b.py": 1.0}
        self.assertEqual([["test_slow.py"], ["test_a.py", "test_b.py", "test_big.py"]], self.shares(files, 2, durations))

    def test_a_file_with_no_recorded_time_is_weighed_by_its_size(self):
        # test_new.py has no time. At the recorded rate (30 s over 300 bytes)
        # its 150 bytes weigh 15 s, so it comes after test_a.py's 20 s; as raw
        # bytes (150) it would outweigh every recorded file and go first.
        files = self.make({"test_a.py": 100, "test_b.py": 100, "test_c.py": 100, "test_new.py": 150})
        durations = {"test_a.py": 20.0, "test_b.py": 0.0, "test_c.py": 10.0}
        self.assertEqual([["test_a.py", "test_b.py"], ["test_c.py", "test_new.py"]], self.shares(files, 2, durations))

    def test_with_no_recorded_times_the_shares_go_by_size(self):
        files = self.make({"test_a.py": 900, "test_b.py": 500, "test_c.py": 400})
        self.assertEqual([["test_a.py"], ["test_b.py", "test_c.py"]], self.shares(files, 2, {}))

    def test_unknown_and_stale_names_still_land_in_exactly_one_share(self):
        files = self.make({f"test_{n}.py": 100 + n for n in range(12)})
        durations = {"test_1.py": 5.0, "test_2.py": 7.5, "test_gone.py": 99.0}
        for count in (1, 3, 5, 13):
            flat = sorted(name for share in self.shares(files, count, durations) for name in share)
            self.assertEqual(sorted(p.name for p in files), flat, count)
            self.assertEqual(self.shares(files, count, durations),
                             self.shares(list(reversed(files)), count, durations), count)

    def test_no_share_is_heavier_than_the_mean_plus_the_largest_file(self):
        seconds = [41.0, 30.0, 22.0, 9.5, 9.0, 8.0, 3.0, 2.5, 1.0, 1.0, 0.5, 0.2, 0.1, 0.0]
        files = self.make({f"test_{n}.py": 100 for n in range(len(seconds))})
        durations = {f"test_{n}.py": s for n, s in enumerate(seconds)}
        for count in (2, 4, 6):
            loads = [sum(durations[name] for name in share) for share in self.shares(files, count, durations)]
            self.assertLessEqual(max(loads), max(max(seconds), sum(seconds) / count + max(seconds)), count)
            if count >= 4:  # the largest file alone sets the floor
                self.assertEqual(max(seconds), max(loads), count)

    def test_the_committed_file_gives_seconds_by_test_file_name(self):
        durations = RUN.load_durations()
        self.assertTrue(durations)
        for name, seconds in durations.items():
            self.assertRegex(name, r"^test_\w+\.py$")
            self.assertIsInstance(seconds, (int, float))
            self.assertGreaterEqual(seconds, 0)

    def test_a_missing_file_means_no_recorded_times(self):
        self.assertEqual({}, RUN.load_durations(self.root / "durations.json"))

    def test_a_file_that_does_not_read_means_no_recorded_times(self):
        """Conflict markers or a cut-short write: shares fall back to sizes."""
        path = self.root / "durations.json"
        for text in ("<<<<<<< HEAD\n{", '["test_a.py"]', "", "{\"test_a.py\": 1"):
            with self.subTest(text=text):
                path.write_text(text)
                self.assertEqual({}, RUN.load_durations(path))

    def test_only_seconds_are_kept(self):
        path = self.root / "durations.json"
        path.write_text('{"test_a.py": 1.5, "test_b.py": null, "test_c.py": "2", "test_d.py": true, "test_e.py": -1}')
        self.assertEqual({"test_a.py": 1.5}, RUN.load_durations(path))


class RecordTests(unittest.TestCase):
    def test_each_file_is_timed_once_and_rounded(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        root = pathlib.Path(folder.name)
        ran = []
        files = []
        for name in ("test_zz_recorded_one", "test_zz_recorded_two"):
            (root / f"{name}.py").write_text(
                "import unittest\n"
                "class T(unittest.TestCase):\n"
                f"    def test_it(self): RAN.append({name!r})\n")
            files.append(root / f"{name}.py")
        sys.path.insert(0, str(root))
        self.addCleanup(sys.path.remove, str(root))
        for name in ("test_zz_recorded_one", "test_zz_recorded_two"):
            __import__(name).RAN = ran
            self.addCleanup(sys.modules.pop, name, None)
        ticks = iter([0.0, 1.234, 10.0, 10.004])
        self.assertEqual({"test_zz_recorded_one.py": 1.23, "test_zz_recorded_two.py": 0.0},
                         RUN.record_durations(files, clock=lambda: next(ticks)))
        self.assertEqual(["test_zz_recorded_one", "test_zz_recorded_two"], ran)

    def test_a_failing_file_keeps_its_last_time_or_has_none(self):
        """A file that fails is often cut short: its time would be too low."""
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        root = pathlib.Path(folder.name)
        files = []
        for name in ("test_zz_failing_one", "test_zz_failing_two", "test_zz_passing"):
            body = "self.assertTrue(True)" if name == "test_zz_passing" else "self.fail('no')"
            (root / f"{name}.py").write_text(
                "import unittest\n"
                "class T(unittest.TestCase):\n"
                f"    def test_it(self): {body}\n")
            files.append(root / f"{name}.py")
            self.addCleanup(sys.modules.pop, name, None)
        sys.path.insert(0, str(root))
        self.addCleanup(sys.path.remove, str(root))
        ticks = iter([0.0, 0.01, 1.0, 1.01, 2.0, 2.5])
        seconds = RUN.record_durations(files, clock=lambda: next(ticks),
                                       previous={"test_zz_failing_one.py": 7.5})
        self.assertEqual({"test_zz_failing_one.py": 7.5, "test_zz_passing.py": 0.5}, seconds)


class ChangedTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        root = pathlib.Path(folder.name)
        self.files = []
        for name, text in {"test_widgets.py": "import homestead_widgets as W\n",
                           "test_gadgets.py": "import homestead_gadgets_extra\n",
                           "test_scan.py": "for p in ROOT.rglob('*.py'): pass\n",
                           "test_page.py": "read('web/js/views-vms.js')\n"}.items():
            (root / name).write_text(text)
            self.files.append(root / name)

        (root / "test_routes.py").write_text("server.GADGET_ROUTES.handle()\n")
        self.files.append(root / "test_routes.py")

    def names(self, changed):
        picked = RUN.affected(self.files, set(changed), {"homestead_gadgets": {"GADGET_ROUTES"}})
        return None if picked is None else sorted(p.name for p in picked)

    def test_a_module_change_runs_its_tests_and_the_tree_scans_only(self):
        self.assertEqual(["test_scan.py", "test_widgets.py"], self.names(["server/homestead_widgets.py"]))

    def test_a_module_reached_through_its_server_alias_runs_those_tests(self):
        self.assertEqual(["test_routes.py", "test_scan.py"], self.names(["server/homestead_gadgets.py"]))

    def test_a_web_file_change_runs_the_tests_that_read_it(self):
        self.assertEqual(["test_page.py", "test_scan.py"], self.names(["web/js/views-vms.js"]))

    def test_a_changed_test_file_runs_itself(self):
        self.assertEqual(["test_gadgets.py", "test_scan.py"], self.names(["tests/test_gadgets.py"]))

    def test_shared_files_and_test_helpers_run_everything(self):
        for changed in (["server/server.py"], ["tests/source_scan.py"], [".gitattributes"]):
            self.assertIsNone(self.names(changed), changed)


if __name__ == "__main__":
    unittest.main()
