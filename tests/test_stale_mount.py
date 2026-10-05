import errno
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
import homestead_stale_mount as STALE


def answers(*results):
    """os.listdir that answers each look in turn: True is stale, an errno
    another error, anything else a healthy listing."""
    def listdir(path):
        result = results[min(listdir.calls, len(results) - 1)]
        listdir.calls += 1
        if result is True:
            raise OSError(errno.ESTALE, "Stale file handle")
        if isinstance(result, int):
            raise OSError(result, "other")
        return []
    listdir.calls = 0
    return listdir


class StaleMountTests(unittest.TestCase):
    def watch(self, *results, replace=None, looks=10):
        replace = replace or mock.Mock()
        with mock.patch.object(STALE.os, "listdir", side_effect=answers(*results)):
            done = STALE.watch("/data", replace, sleep=lambda s: None, log=lambda *a, **k: None, looks=looks)
        return done, replace

    def test_a_mount_stale_three_looks_running_replaces_the_pod(self):
        done, replace = self.watch(True, True, True)
        self.assertTrue(done)
        replace.assert_called_once_with()

    def test_a_passing_blip_is_not_enough(self):
        done, replace = self.watch(True, True, False, True, True, False, looks=6)
        self.assertFalse(done)
        replace.assert_not_called()

    def test_other_errors_are_not_staleness(self):
        # A missing directory (demo mode) or a permission error is not this.
        done, replace = self.watch(errno.ENOENT, errno.EACCES, errno.EIO, looks=6)
        self.assertFalse(done)
        replace.assert_not_called()

    def test_a_failed_replacement_is_tried_again(self):
        replace = mock.Mock(side_effect=[RuntimeError("API busy"), None])
        done, _ = self.watch(True, replace=replace, looks=10)
        self.assertTrue(done)
        self.assertEqual(2, replace.call_count)


if __name__ == "__main__":
    unittest.main()
