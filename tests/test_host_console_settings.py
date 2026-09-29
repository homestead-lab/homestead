"""Console management uses this release, preserves logins and identifies hosts."""
import copy
import hashlib
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
import homestead_host_console as CONSOLE
import homestead_operations as OPS


class ConsoleSettingsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.node = {"metadata": {"name": "node1", "uid": "original"},
                     "status": {"conditions": [{"type": "Ready", "status": "True"}]}}
        self.runner = mock.Mock()
        def get(path):
            return {"items": [copy.deepcopy(self.node)]} if path == "/api/v1/nodes" else copy.deepcopy(self.node)
        OPS.bind(get, self.tmp.name, lambda *a: {})
        CONSOLE.bind(get, self.runner, OPS, "2.8.244", self.tmp.name)
        self.payload = self.root / "payload"
        self.payload.mkdir()
        source = Path(__file__).resolve().parents[1] / "scripts"
        for name in ("host-console.py", "install-console.sh"):
            (self.payload / name).write_bytes((source / name).read_bytes())
        self.enterContext(mock.patch.object(CONSOLE, "PAYLOAD", self.payload))
        self.enterContext(mock.patch.dict(OPS.RESOLVERS, {"host-console": CONSOLE.status}))

    def report(self, enabled="yes", native="no"):
        digest = hashlib.sha256((self.payload / "host-console.py").read_bytes()).hexdigest()
        return f"HSCONSOLE enabled={enabled} version=2.8.244 digest={digest} native={native}\n", ""

    def test_install_is_durable_and_new_container_marks_previous_console_outdated(self):
        self.runner.run.return_value = self.report()
        started = CONSOLE.start("node1", "enable")
        self.assertFalse(started["cancellable"])
        result = OPS.list_operations()[0]
        self.assertEqual("succeeded", result["status"])
        self.assertIn("logout or reboot", result["message"])
        current = CONSOLE.inventory()["nodes"][0]
        self.assertTrue(current["current"])
        (self.payload / "host-console.py").write_text("# next release\n")
        CONSOLE.VERSION = "2.8.245"
        updated = CONSOLE.inventory()["nodes"][0]
        self.assertFalse(updated["current"])
        self.assertIn("update available", updated["detail"])
        self.assertEqual(1, self.runner.run.call_count, "inventory never changes hosts")

    def test_replaced_host_or_changed_release_cannot_execute_a_queued_install(self):
        for replace in (False, True):
            item = {"ref": {"node": "node1", "uid": "original", "action": "enable", "version": "2.8.244"}}
            if replace:
                self.node["metadata"]["uid"] = "replacement"
                CONSOLE.VERSION = "2.8.244"
            else:
                CONSOLE.VERSION = "2.8.245"
            self.assertEqual("failed", CONSOLE.status(item)[0])
        self.runner.run.assert_not_called()
        self.assertNotIn("enabled", CONSOLE.inventory()["nodes"][0])

    def test_ready_required_and_install_failure_is_reported(self):
        self.node["status"]["conditions"][0]["status"] = "False"
        with self.assertRaisesRegex(ValueError, "Ready"):
            CONSOLE.start("node1", "enable")
        self.node["status"]["conditions"][0]["status"] = "True"
        self.runner.run.return_value = ("", "python curses unavailable")
        CONSOLE.start("node1", "enable")
        result = OPS.list_operations()[0]
        self.assertEqual("failed", result["status"])
        self.assertIn("curses unavailable", result["message"])

    @unittest.skipIf(os.name == "nt", "exercise the installer on Linux")
    def test_real_install_and_disable_keep_other_overrides_and_never_restart_getty(self):
        dest = self.root / "lib"
        dropin = self.root / "getty" / "50-homestead-console.conf"
        dropin.parent.mkdir()
        other = dropin.parent / "10-existing.conf"
        other.write_text("existing override\n")
        helper = self.payload / "install-console.sh"
        helper.write_text(helper.read_text().replace(CONSOLE.DEST, str(dest)).replace(CONSOLE.DROPIN, str(dropin)))
        commands = self.root / "bin"
        commands.mkdir()
        log = self.root / "systemctl.log"
        for name, body in {"systemctl": f"printf '%s\\n' \"$*\" >> '{log}'\n",
                           "agetty": "exit 0\n", "id": "echo 0\n"}.items():
            path = commands / name
            path.write_text("#!/bin/sh\n" + body)
            path.chmod(0o755)
        env = dict(os.environ, PATH=str(commands) + ":" + os.environ["PATH"])
        with mock.patch.object(CONSOLE, "DEST", str(dest)), mock.patch.object(CONSOLE, "DROPIN", str(dropin)):
            install = subprocess.run(["sh", "-c", CONSOLE.script("enable")], env=env, capture_output=True, text=True)
            self.assertEqual(0, install.returncode, install.stderr)
            self.assertTrue(dropin.exists())
            self.assertEqual("2.8.244\n", (dest / "host-console.version").read_text())
            self.assertEqual((self.payload / "host-console.py").read_bytes(), (dest / "host-console.py").read_bytes())
            disabled = subprocess.run(["sh", "-c", CONSOLE.script("disable")], env=env, capture_output=True, text=True)
            self.assertEqual(0, disabled.returncode, disabled.stderr)
            self.assertFalse(dropin.exists())
            self.assertEqual("existing override\n", other.read_text())
            self.assertIn("enabled=no", disabled.stdout)
        self.assertNotIn("restart", log.read_text())
        self.assertNotIn("stop", log.read_text())
