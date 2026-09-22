import io
import json
import sys
import tempfile
import unittest
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
import homestead_console as console


class ConsoleTests(unittest.TestCase):
    def test_websocket_frames_round_trip_masked_and_unmasked(self):
        for masked in (False, True):
            encoded = console.encode_frame(b"hello", opcode=2, masked=masked)
            final, opcode, payload = console.read_frame(io.BytesIO(encoded), require_mask=masked)
            self.assertTrue(final)
            self.assertEqual(2, opcode)
            self.assertEqual(b"hello", payload)

    def test_browser_messages_map_to_kubernetes_channels(self):
        self.assertEqual(b"\x00ls\n", console.browser_message(
            json.dumps({"type": "input", "data": "ls\n"}).encode()))
        resize = console.browser_message(
            json.dumps({"type": "resize", "cols": 120, "rows": 35}).encode())
        self.assertEqual(4, resize[0])
        self.assertEqual({"Width": 120, "Height": 35}, json.loads(resize[1:]))

    def test_kubernetes_output_channels_stay_distinct(self):
        self.assertEqual({"type": "output", "stream": "stdout", "data": "ok"},
                         console.kubernetes_message(b"\x01ok"))
        self.assertEqual("stderr", console.kubernetes_message(b"\x02bad")["stream"])
        self.assertEqual("error", console.kubernetes_message(b"\x03denied")["type"])

    def test_target_validation_blocks_system_namespaces_and_arbitrary_commands(self):
        with self.assertRaises(PermissionError):
            console.validate_target("kube-system", "pod-1", "app", "/bin/sh", {"kube-system"})
        with self.assertRaises(ValueError):
            console.validate_target("lab", "pod-1", "app", "rm -rf /", set())
        with self.assertRaises(PermissionError):
            console.validate_target("other", "pod-1", "app", "/bin/sh", set(), {"lab"})
        console.validate_target("lab", "pod-1", "app", "/bin/bash", set())

    def test_exec_path_selects_exact_container_and_interactive_tty(self):
        path = console.exec_path("lab", "frigate-abc", "frigate", "/bin/sh")
        self.assertIn("/namespaces/lab/pods/frigate-abc/exec?", path)
        self.assertIn("container=frigate", path)
        self.assertIn("command=%2Fbin%2Fsh", path)
        self.assertIn("stdin=true", path)
        self.assertIn("tty=true", path)

    def test_audit_records_session_metadata_but_not_terminal_input(self):
        with tempfile.TemporaryDirectory() as root:
            console.audit(root, {"event": "start", "user": "admin", "pod": "demo"})
            record = json.loads(Path(root, "console-audit.jsonl").read_text())
        self.assertEqual("start", record["event"])
        self.assertEqual("admin", record["user"])
        self.assertNotIn("input", record)


if __name__ == "__main__":
    unittest.main()
