"""/api/settings asks the API server for its version at most every ten
minutes (#985 §6); a failed read is not kept."""
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
import homestead_settings_helpers as SETTINGS


class KubeVersionTests(unittest.TestCase):
    def setUp(self):
        SETTINGS.forget_kube_version()
        self.addCleanup(SETTINGS.forget_kube_version)
        self.now = [100.0]
        patch = mock.patch.object(SETTINGS, "version_clock", lambda: self.now[0])
        patch.start()
        self.addCleanup(patch.stop)

    def test_one_read_in_ten_minutes(self):
        reads = []
        read = lambda path: reads.append(path) or {"gitVersion": "v1.30.4+k3s1"}
        for _ in range(30):
            self.assertEqual("v1.30.4+k3s1", SETTINGS.kube_version(read))
            self.now[0] += 15
        self.assertEqual(["/version"] * 1, reads[:1])
        self.assertEqual(1, len(reads), "450 s of 15 s ticks")
        self.now[0] += SETTINGS.KUBE_VERSION_TTL
        SETTINGS.kube_version(read)
        self.assertEqual(2, len(reads))

    def test_a_failed_read_answers_blank_and_is_asked_again(self):
        def broken(path):
            raise OSError("down")
        self.assertEqual("", SETTINGS.kube_version(broken))
        self.assertEqual("v1", SETTINGS.kube_version(lambda path: {"gitVersion": "v1"}))


if __name__ == "__main__":
    unittest.main()
