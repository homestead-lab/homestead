import hashlib
import hmac
import json
import sys
import time
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "server"))
import harvui_smart as smart
import server


class _Response:
    def __init__(self, value):
        self.value = json.dumps(value).encode()
    def __enter__(self):
        return self
    def __exit__(self, *_):
        return False
    def read(self):
        return self.value


class SmartTests(unittest.TestCase):
    def setUp(self):
        def get(_path):
            return {"items": [{
                "spec": {"nodeName": "node-1"},
                "status": {"phase": "Running", "podIP": "10.42.0.9",
                           "containerStatuses": [{"name": "smart", "ready": True}]},
            }]}
        smart.bind(get, "lab", lambda: b"test-signing-key")

    def test_mutating_helper_request_is_timestamped_and_signed(self):
        captured = {}
        def open_request(request, timeout=0):
            captured.update(headers=dict(request.header_items()), body=request.data, timeout=timeout)
            return _Response({"ok": True})
        with mock.patch.object(smart.urllib.request, "urlopen", open_request):
            smart._request("node-1", body={"node": "node-1", "disk": "sda", "test": "short"})
        stamp = captured["headers"]["X-homestead-time"]
        expected = hmac.new(b"test-signing-key", stamp.encode() + b"." + captured["body"],
                            hashlib.sha256).hexdigest()
        self.assertEqual(expected, captured["headers"]["X-homestead-signature"])
        self.assertLess(abs(time.time() - int(stamp)), 3)

    def test_start_revalidates_support_and_returns_durable_operation_reference(self):
        report = {"name": "sda", "available": True, "supported_tests": ["short"],
                  "test": {"active": False}}
        with mock.patch.object(smart, "disk", return_value=report), \
             mock.patch.object(smart, "_request", return_value={
                 "expected_seconds": 120, "baseline": "old", "started_epoch": 100,
                 "message": "test started"}):
            result = smart.start_test("node-1", "sda", "short")
        self.assertEqual(120, result["expected_seconds"])
        self.assertEqual("old", result["baseline"])
        with mock.patch.object(smart, "disk", return_value=report):
            with self.assertRaisesRegex(ValueError, "does not advertise"):
                smart.start_test("node-1", "sda", "long")

    def test_progress_uses_drive_remaining_percentage_and_new_log_result(self):
        active = {"test": {"active": True, "remaining_percent": 60,
                            "status": "Self-test in progress"}, "self_tests": []}
        with mock.patch.object(smart, "disk", return_value=active):
            state = smart.progress({"node": "node-1", "disk": "sda", "test": "short",
                                    "started_epoch": int(time.time()) - 30,
                                    "expected_seconds": 120, "baseline": "old"})
        self.assertEqual(("running", 40, "Self-test in progress"), state)
        complete = {"test": {"active": False}, "self_tests": [{
            "signature": "new", "status": "Completed without error"}]}
        with mock.patch.object(smart, "disk", return_value=complete):
            state = smart.progress({"node": "node-1", "disk": "sda", "test": "short",
                                    "started_epoch": int(time.time()) - 90,
                                    "expected_seconds": 120, "baseline": "old"})
        self.assertEqual("succeeded", state[0])
        failed = {"test": {"active": False}, "self_tests": [{
            "signature": "new", "status": "Completed: read failure"}]}
        with mock.patch.object(smart, "disk", return_value=failed):
            self.assertEqual("failed", smart.progress({
                "node": "node-1", "disk": "sda", "test": "short",
                "started_epoch": int(time.time()) - 90,
                "expected_seconds": 120, "baseline": "old"})[0])

    def test_drive_findings_use_configured_thresholds(self):
        report = {"available": True, "health": "passed", "temperature_c": 58,
                  "reallocated": 2, "pending": 0, "uncorrectable": 0}
        issues = server.smart_disk_issues(report, {
            "temperature": {"warning": 55, "critical": 65},
            "reallocated_warning": 1, "pending_critical": 1,
            "uncorrectable_critical": 1})
        self.assertEqual(2, len(issues))
        self.assertTrue(all(row["severity"] == "degraded" for row in issues))
        critical = server.smart_disk_issues({**report, "health": "failed", "pending": 1}, {
            "temperature": {"warning": 55, "critical": 65},
            "reallocated_warning": 5, "pending_critical": 1,
            "uncorrectable_critical": 1})
        self.assertTrue(any(row["severity"] == "critical" for row in critical))

    def test_nodeprobe_keeps_privileged_smart_access_isolated(self):
        manifest = (ROOT / "deploy" / "nodeprobe.yaml").read_text(encoding="utf-8")
        self.assertIn("- name: smart", manifest)
        self.assertIn("privileged: true", manifest)
        probe_section = manifest.split("- name: probe", 1)[1].split("- name: smart", 1)[0]
        self.assertNotIn("privileged: true", probe_section)
        self.assertIn("X-Homestead-Signature", manifest)
        self.assertIn("secretName: harvui-auth", manifest)
        self.assertIn('["-d", device_type(name), "-a", "-j", path]', manifest)
        self.assertIn('["-d", device_type(name), "-t", test, "-j", path]', manifest)
        script = manifest.split("  smart.py: |\n", 1)[1].split("\n---", 1)[0]
        source = "\n".join(line[4:] if line.startswith("    ") else line
                           for line in script.splitlines())
        compile(source, "smart.py", "exec")


if __name__ == "__main__":
    unittest.main()
