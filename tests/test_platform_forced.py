"""Platform detection is forced only where Homestead is about to change the
cluster (#985 §3). Pages and background checks take the cached answer, or one
at most a few seconds old; concurrent reads share one detection."""
import ast
import sys
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

SERVER = Path(__file__).resolve().parents[1] / "server"
sys.path.insert(0, str(SERVER))
import homestead_platform as PLATFORM

# (module, function) that may force a fresh detection: each decides a write
# (an install, an upgrade, a host or disk change) from what the cluster has.
ALLOWED = {
    ("homestead_addons", "_can_install"), ("homestead_addons", "set_kubevirt_emulation"),
    ("homestead_baseline", "install"), ("homestead_components", "upgrade"),
    ("homestead_components", "_manage_operator_route"), ("homestead_csi_restore", "support"),
    ("homestead_disk_allocation", "_host"), ("homestead_disk_v2", "_inventory"),
    ("homestead_lhv2_upgrade", "plan"), ("homestead_lhv2_upgrade", "settings_review"),
    ("homestead_longhorn_runtime", "_inventory"), ("homestead_macvtap", "install"),
    ("homestead_macvtap", "upgrade"), ("homestead_os_rollout", "start"), ("homestead_promote", "plan"),
}

# Forced refreshes a viewer asks for ("Check now", ?force=1): each is shared
# and rate-limited by homestead_forced (#991), so it may force detection.
# Listed here before #991 merges, so the stale-row check leaves them out.
ASKED = {("homestead_platform", "_platform_route")}


def forced_calls():
    """(module, function) of every call that forces platform detection."""
    found = set()
    for path in sorted(SERVER.glob("homestead_*.py")):
        tree = ast.parse(path.read_text())
        for fn in ast.walk(tree):
            if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for call in ast.walk(fn):
                if not isinstance(call, ast.Call):
                    continue
                name = call.func.attr if isinstance(call.func, ast.Attribute) else getattr(call.func, "id", "")
                if name not in ("platform", "detect"):
                    continue
                args = [a for a in call.args] + [k.value for k in call.keywords if k.arg == "force"]
                if any(isinstance(a, ast.Constant) and a.value is True for a in args):
                    found.add((path.stem, fn.name))
    return found


class ForcedDetectionTests(unittest.TestCase):
    def test_only_write_paths_force_a_detection(self):
        extra = forced_calls() - ALLOWED - ASKED
        self.assertEqual(set(), extra, "a page or background check forces platform detection: use the "
                         "cached detect(), or platform(RECENT) on a page that must show a change soon")

    def test_the_allow_list_has_no_stale_rows(self):
        self.assertEqual(set(), ALLOWED - forced_calls())


class DetectAgeTests(unittest.TestCase):
    def setUp(self):
        saved = dict(PLATFORM._cached)
        self.addCleanup(lambda: (PLATFORM._cached.clear(), PLATFORM._cached.update(saved)))
        PLATFORM._cached.update(at=0.0, value=None, failed_at=0.0)
        self.reads = []
        def read():
            self.reads.append(1)
            time.sleep(0.05)
            return {"distribution": "k3s", "n": len(self.reads)}
        patch = mock.patch.object(PLATFORM, "_read", side_effect=read)
        patch.start()
        self.addCleanup(patch.stop)

    def test_a_number_is_the_oldest_answer_taken(self):
        PLATFORM.detect()
        PLATFORM._cached["at"] = time.time() - 10
        self.assertEqual(1, PLATFORM.detect(30)["n"], "10 s old is young enough for 30")
        PLATFORM._cached["at"] = time.time() - 40
        self.assertEqual(2, PLATFORM.detect(30)["n"], "40 s old is read again")
        self.assertEqual(2, PLATFORM.detect()["n"], "the cache stays five minutes")

    def test_concurrent_reads_share_one_detection(self):
        threads = [threading.Thread(target=PLATFORM.detect) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(5)
        self.assertEqual(1, len(self.reads))

    def test_a_forced_read_still_reads(self):
        PLATFORM.detect()
        PLATFORM.detect(True)
        self.assertEqual(2, len(self.reads))


if __name__ == "__main__":
    unittest.main()
