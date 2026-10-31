"""The audit log on the data volume (#738): /data/audit.jsonl, 0600, two files
of 10 MB at most, and written only through the self-data write fence - while a
data move holds /data, lines go to stdout alone and nothing raises."""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "server"))
import homestead_audit as AUDIT_LOG
import homestead_self_data_fence as FENCE
import homestead_shared as SHARED
from homestead_storage_journal import Held


class OnTheDataVolume(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.dir = folder.name
        self.path = os.path.join(self.dir, "audit.jsonl")
        self.enterContext(mock.patch.object(SHARED, "DIR", self.dir))
        self.enterContext(mock.patch.object(SHARED, "WRITE_GUARD", None))
        self.enterContext(mock.patch.object(SHARED, "WRITE_SCOPE", None))
        self.enterContext(mock.patch.object(AUDIT_LOG, "_file_on", return_value=True))
        self.stdout = self.enterContext(mock.patch.object(AUDIT_LOG, "_stdout"))
        AUDIT_LOG._file_dropped[0] = 0
        self.addCleanup(AUDIT_LOG._file_dropped.__setitem__, 0, 0)

    def emit(self, n=1):
        for i in range(n):
            AUDIT_LOG.emit({"v": 1, "path": f"/api/x/{i}"})

    def kept(self):
        return Path(self.path).read_text(encoding="utf-8").splitlines() if os.path.exists(self.path) else []

    def test_lines_append_with_mode_0600(self):
        self.emit(2)
        self.assertEqual(["/api/x/0", "/api/x/1"], [json.loads(line)["path"] for line in self.kept()])
        self.assertEqual(0o600, os.stat(self.path).st_mode & 0o777)
        self.assertEqual(2, self.stdout.call_count, "stdout always")

    def test_rotation_keeps_exactly_two_files(self):
        with mock.patch.object(AUDIT_LOG, "MAX_BYTES", 200):
            self.emit(20)
        self.assertEqual(["audit.jsonl", "audit.jsonl.1", "audit.lock"], sorted(os.listdir(self.dir)))
        for name in ("audit.jsonl", "audit.jsonl.1"):
            self.assertLessEqual(os.path.getsize(os.path.join(self.dir, name)), 200)
        self.assertEqual("/api/x/19", json.loads(self.kept()[-1])["path"])

    def test_rotation_rechecks_under_the_lock(self):
        """Two replicas that both saw a full file rotate it once, not twice."""
        Path(self.path).write_text("x" * 300, encoding="utf-8")
        with mock.patch.object(AUDIT_LOG, "MAX_BYTES", 200):
            AUDIT_LOG._rotate(self.path, 10)
            Path(self.path).write_text("y\n", encoding="utf-8")       # the first replica's next line
            AUDIT_LOG._rotate(self.path, 10)                           # the second, late
        self.assertEqual("x" * 300, Path(self.path + ".1").read_text(encoding="utf-8"))
        self.assertEqual("y\n", Path(self.path).read_text(encoding="utf-8"))

    def test_a_held_fence_means_stdout_only(self):
        Path(self.path).write_text("old\n", encoding="utf-8")
        for name, held in (("WRITE_SCOPE", mock.Mock(side_effect=Held("held"))),
                           ("WRITE_GUARD", mock.Mock(side_effect=Held("held")))):
            with self.subTest(name), mock.patch.object(SHARED, name, held):
                self.emit()
                held.assert_called_with(self.path)
        self.assertEqual(["old"], self.kept())
        self.assertEqual(["audit.jsonl"], os.listdir(self.dir))
        self.assertEqual(2, self.stdout.call_count)

    @unittest.skipUnless(FENCE.fcntl is not None, "Linux shared locking")
    def test_a_frozen_barrier_means_stdout_only_until_it_thaws(self):
        """A data move's freeze drains activity; an audit append is activity."""
        locks = tempfile.TemporaryDirectory()
        self.addCleanup(locks.cleanup)
        mine, mover = FENCE.WriteBarrier(locks.name, lambda: None), FENCE.WriteBarrier(locks.name, lambda: None)
        with mock.patch.object(SHARED, "WRITE_SCOPE", lambda path: mine.activity()):
            with mover.freeze(timeout=.05):
                self.emit()
                self.assertEqual([], os.listdir(self.dir))
            self.emit()
        self.assertEqual(1, len(self.kept()), "after the freeze it writes again")
        self.assertEqual(2, self.stdout.call_count)

    def test_rotating_twice_within_an_hour_says_so(self):
        """A flood that fills 10 MB twice in an hour is worth a look: one line
        on stdout, and one at the top of the new file."""
        clock = [5000.0]
        self.enterContext(mock.patch.object(AUDIT_LOG.time, "time", side_effect=lambda: clock[0]))
        AUDIT_LOG._rotated.clear()
        self.addCleanup(AUDIT_LOG._rotated.clear)
        def full_then_emit():
            Path(self.path).write_text("x" * 300, encoding="utf-8")
            self.emit()                                                # exactly one rotation
        with mock.patch.object(AUDIT_LOG, "MAX_BYTES", 200):
            full_then_emit()                                           # the first: nothing to say
            self.assertFalse([c for c in self.stdout.call_args_list if "rotated" in c.args[0]])
            clock[0] += 60
            full_then_emit()                                           # again within the hour
            clock[0] += AUDIT_LOG.ROTATION_ALARM_SECONDS + 1
            full_then_emit()                                           # an hour later: quiet again
        alarms = [json.loads(c.args[0]) for c in self.stdout.call_args_list if "rotated" in c.args[0]]
        self.assertEqual(1, len(alarms))
        self.assertEqual("audit-file-rotated-quickly", alarms[0]["event"])
        self.assertIn("the pod log keeps every line", alarms[0]["detail"])

    def test_the_fence_sees_the_audit_path(self):
        guard = mock.Mock()
        with mock.patch.object(SHARED, "WRITE_GUARD", guard):
            self.emit()
        guard.assert_called_once_with(self.path)

    def test_no_data_dir_no_file(self):
        missing = os.path.join(self.dir, "missing")
        with mock.patch.object(SHARED, "DIR", missing):
            self.emit()
        self.assertFalse(os.path.exists(missing))
        self.assertEqual(1, self.stdout.call_count)

    def test_a_full_disk_changes_nothing(self):
        with mock.patch.object(AUDIT_LOG.os, "write", side_effect=OSError(28, "No space left on device")):
            self.emit()
        self.assertEqual(1, self.stdout.call_count)

    def test_a_short_write_is_finished_with_a_marker_and_nothing_is_cut(self):
        """Never truncate (another replica may have appended since): a half line
        is ended, and a marker line says the one before it is broken."""
        self.emit()
        real, calls = os.write, []

        def half_once(fd, data):
            calls.append(data)
            return real(fd, data[:len(data) // 2] if len(calls) == 1 else data)
        with mock.patch.object(AUDIT_LOG.os, "write", side_effect=half_once), \
                mock.patch.object(AUDIT_LOG.os, "ftruncate", side_effect=AssertionError("never truncate")):
            self.emit()
        self.emit()
        lines = self.kept()
        self.assertEqual("/api/x/0", json.loads(lines[0])["path"], "earlier lines are intact")
        with self.assertRaises(ValueError):
            json.loads(lines[1])                                    # the broken one, ended
        self.assertEqual("audit-line-truncated", json.loads(lines[2])["event"])
        self.assertEqual("/api/x/0", json.loads(lines[3])["path"])
        self.assertEqual(4, len(lines))

    def test_a_failed_write_is_counted_and_reported_by_the_next_line(self):
        self.emit()
        with mock.patch.object(AUDIT_LOG.os, "write", side_effect=OSError(28, "No space left on device")), \
                mock.patch.object(AUDIT_LOG.os, "ftruncate", side_effect=AssertionError("never truncate")):
            self.emit(2)
        self.assertEqual(1, len(self.kept()))
        self.emit()
        lines = [json.loads(line) for line in self.kept()]
        self.assertEqual(("audit-file-lines-dropped", 2), (lines[1]["event"], lines[1]["dropped"]))
        self.assertEqual("/api/x/0", lines[2]["path"])

    def test_a_link_in_its_place_is_not_followed(self):
        target = os.path.join(self.dir, "elsewhere")
        Path(target).write_text("", encoding="utf-8")
        os.symlink(target, self.path)
        self.emit()
        self.assertEqual("", Path(target).read_text(encoding="utf-8"))


class FillingFastAlert(unittest.TestCase):
    """Andreas's decision 3: audit.jsonl filling fast is a bell alert too. It is
    read from the files, so it works on whichever copy leads the alerts: raised
    while audit.jsonl.1 was filled in under ROTATION_ALARM_SECONDS, and that
    fill ended within the last ROTATION_ALERT_SECONDS; it clears once a fill is
    slow again, or a day after the fast one."""

    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.dir = folder.name
        self.enterContext(mock.patch.object(SHARED, "DIR", self.dir))
        self.t0 = 1792574043.0

    def old_file(self, first, last, broken_tail=False):
        lines = [AUDIT_LOG.text({"v": 1, "at": AUDIT_LOG._now_text(first), "path": "/api/x"})]
        lines += [AUDIT_LOG.text({"v": 1, "at": AUDIT_LOG._now_text(first + 1), "path": "/api/x"})] * 50
        lines.append(AUDIT_LOG.text({"v": 1, "at": AUDIT_LOG._now_text(last), "path": "/api/x"}))
        text = "\n".join(lines) + "\n" + ('{"v":1,"at":"20' if broken_tail else "")
        Path(self.dir, "audit.jsonl.1").write_text(text, encoding="utf-8")

    def facts(self, now):
        return AUDIT_LOG.alert_facts(now=now)

    def test_a_fast_fill_raises_one_alert(self):
        self.old_file(self.t0, self.t0 + 600, broken_tail=True)
        (fact,) = self.facts(self.t0 + 700)
        self.assertEqual(("audit:filling-fast", "degraded", "degraded"),
                         (fact["key"], fact["severity"], fact["category"]))
        self.assertIn("10 minutes", fact["body"])
        self.assertIn("pod log", fact["body"])
        self.assertTrue(fact["resolved"])
        self.assertEqual("/settings?tab=about", fact["href"])

    def test_a_slow_fill_raises_nothing(self):
        self.old_file(self.t0, self.t0 + AUDIT_LOG.ROTATION_ALARM_SECONDS + 1)
        self.assertEqual([], self.facts(self.t0 + 2 * AUDIT_LOG.ROTATION_ALARM_SECONDS))

    def test_it_clears_a_day_after_the_fast_fill(self):
        self.old_file(self.t0, self.t0 + 600)
        self.assertEqual([], self.facts(self.t0 + 600 + AUDIT_LOG.ROTATION_ALERT_SECONDS + 1))

    def test_no_file_or_an_unreadable_one_raises_nothing(self):
        self.assertEqual([], self.facts(self.t0))
        Path(self.dir, "audit.jsonl.1").write_text("not json\n", encoding="utf-8")
        self.assertEqual([], self.facts(self.t0))

    def test_a_file_that_cannot_be_read_keeps_the_alert_as_it_was(self):
        """EIO on the data volume is not "fills at its usual pace again" (sweep unknown-as-fine, row 14)."""
        self.old_file(self.t0, self.t0 + 600)
        real = open

        def failing(path, *args, **kwargs):
            if str(path).endswith("audit.jsonl.1"):
                raise OSError(5, "Input/output error")
            return real(path, *args, **kwargs)
        with mock.patch("builtins.open", failing):
            self.assertEqual([{"unknown_key": "audit:filling-fast"}], self.facts(self.t0 + 700))

    def test_the_leader_asks_for_it_with_the_other_sources(self):
        import homestead_overview_server as OVERVIEW
        source = Path(OVERVIEW.__file__).read_text(encoding="utf-8")
        self.assertIn('take("audit", AUDIT_LOG.alert_facts)', source)


class TheSetting(unittest.TestCase):
    """Settings › About › Audit log: the file can be turned off; stdout cannot."""

    def setUp(self):
        AUDIT_LOG._setting.update(value=True, read=None)
        self.addCleanup(AUDIT_LOG._setting.update, value=True, read=None)
        self.clock = [1000.0]
        self.enterContext(mock.patch.object(AUDIT_LOG.time, "monotonic", side_effect=lambda: self.clock[0]))

    def settings(self, **kwargs):
        import homestead_settings_helpers as SETTINGS
        return self.enterContext(mock.patch.object(SETTINGS, "get_app_settings", **kwargs))

    def test_off_means_stdout_only(self):
        self.settings(return_value={"audit": {"file": False}})
        with mock.patch.object(AUDIT_LOG, "_stdout") as out, mock.patch.object(AUDIT_LOG, "_append") as append:
            AUDIT_LOG.emit({"v": 1})
        out.assert_called_once()
        append.assert_not_called()

    def test_it_is_read_at_most_every_30_s(self):
        read = self.settings(return_value={"audit": {"file": True}})
        for _ in range(5):
            self.assertTrue(AUDIT_LOG._file_on())
            self.clock[0] += 1
        self.assertEqual(1, read.call_count)
        self.clock[0] += AUDIT_LOG.SETTING_SECONDS
        AUDIT_LOG._file_on()
        self.assertEqual(2, read.call_count)

    def test_a_read_error_keeps_the_last_value(self):
        self.settings(return_value={"audit": {"file": False}})
        self.assertFalse(AUDIT_LOG._file_on())
        self.clock[0] += AUDIT_LOG.SETTING_SECONDS + 1
        self.settings(side_effect=OSError("the cluster did not answer"))
        self.assertFalse(AUDIT_LOG._file_on())
        AUDIT_LOG._setting.update(value=True, read=None)
        self.assertTrue(AUDIT_LOG._file_on(), "before any read: on")


if __name__ == "__main__":
    unittest.main()
