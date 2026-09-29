"""Host console data, API failures and escape to authenticated getty."""
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("host_console", ROOT / "scripts/host-console.py")
console = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(console)


class ConsoleDataTests(unittest.TestCase):
    def test_cpu_excludes_double_counted_guest_time(self):
        self.assertEqual((100, 60), console.cpu_ticks("cpu  20 0 20 50 10 0 0 0 8 0\n"))
        self.assertIsNone(console.cpu_ticks(""))

    def test_memory_uses_available_including_reclaimable_cache(self):
        self.assertEqual((614400, 1024000), console.memory(
            "MemTotal: 1000 kB\nMemFree: 100 kB\nMemAvailable: 400 kB\n"))

    def test_unknown_readiness_and_cordons_are_visible(self):
        status, rows = console.node_rows({"items": [
            {"metadata": {"name": "node-a", "labels": {"node-role.kubernetes.io/control-plane": ""}},
             "status": {"conditions": [{"type": "Ready", "status": "True"}]}},
            {"metadata": {"name": "node-b"}, "spec": {"unschedulable": True},
             "status": {"conditions": [{"type": "Ready", "status": "Unknown"}]}}]})
        self.assertEqual("1/2 nodes Ready", status)
        self.assertIn("server", rows[0])
        self.assertIn("NotReady/cordoned", rows[1])

    def test_cluster_unavailable_never_shows_raw_errors(self):
        with patch.object(console, "kube_command", return_value=["kubectl"]), \
             patch.object(console, "command", return_value=None):
            self.assertIn("API unavailable", console.cluster_stats()[0])
        with patch.object(console, "kube_command", return_value=None):
            self.assertIn("worker", console.cluster_stats()[0])

    def test_homestead_uses_service_port_and_vip(self):
        service = {"status": {"loadBalancer": {"ingress": [{"ip": "192.0.2.100"}]}},
                   "spec": {"ports": [{"port": 8088, "targetPort": 8080}]}}
        with patch.object(console, "kube_command", return_value=["kubectl"]), \
             patch.object(console, "command", side_effect=['{"items": []}', json.dumps(service)]):
            self.assertIn("Homestead: http://192.0.2.100:8088", console.cluster_stats())

    def test_subprocesses_are_bounded_and_cannot_read_console_input(self):
        with patch.object(console.subprocess, "run", side_effect=subprocess.TimeoutExpired("kubectl", 5)) as run:
            self.assertIsNone(console.command(["kubectl", "get", "nodes"]))
            self.assertEqual(subprocess.DEVNULL, run.call_args.kwargs["stdin"])
            self.assertEqual(5, run.call_args.kwargs["timeout"])

    def test_no_terminal_control_characters_in_data(self):
        self.assertEqual("node[2Jname", console.clean("node\x1b[2J\nname\x00"))

    def test_disk_mounts_exclude_pod_mounts_and_duplicate_filesystems(self):
        mounts = "/dev/a / ext4 rw 0 0\n/dev/a /bind ext4 rw 0 0\nserver /nfs nfs rw 0 0\n/dev/b /var/lib/kubelet/pods/id ext4 rw 0 0\n"
        with patch.object(console, "read", return_value=mounts), \
             patch.object(console.os, "stat") as stat, patch.object(console.shutil, "disk_usage") as usage:
            stat.return_value.st_dev = 1
            usage.return_value = shutil._ntuple_diskusage(100, 40, 60)
            self.assertEqual(1, len(console.disk_stats()))
            self.assertEqual(1, usage.call_count)


@unittest.skipIf(sys.platform == "win32", "requires a Linux pseudo-terminal")
class ConsoleTerminalTests(unittest.TestCase):
    def test_exit_does_not_wait_for_a_stuck_cluster_collector(self):
        import pty
        import select
        master, slave = pty.openpty()
        program = """
import importlib.util, sys, time
spec = importlib.util.spec_from_file_location('console', sys.argv[1])
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
module.cluster_stats = lambda: time.sleep(60)
sys.argv = ['host-console.py']
module.main()
"""
        process = subprocess.Popen([sys.executable, "-c", program, str(ROOT / "scripts/host-console.py")],
                                   stdin=slave, stdout=slave, stderr=slave,
                                   env=dict(os.environ, TERM="xterm"))
        try:
            output = b""
            deadline = time.monotonic() + 5
            while b"HOMESTEAD" not in output and time.monotonic() < deadline:
                if select.select([master], [], [], .2)[0]:
                    output += os.read(master, 65536)
            self.assertIn(b"HOMESTEAD", output)
            os.write(master, b"q")
            self.assertEqual(0, process.wait(timeout=3))
        finally:
            if process.poll() is None:
                process.kill()
                process.wait()
            os.close(master)
            os.close(slave)

    def test_all_exit_keys_and_resize_leave_the_console_promptly(self):
        import fcntl
        import pty
        import select
        import struct
        import termios
        for key in (b"q", b"\r", b"\x1b", b"\x03"):
            with self.subTest(key=key):
                master, slave = pty.openpty()
                fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 24, 80, 0, 0))
                process = subprocess.Popen([sys.executable, str(ROOT / "scripts/host-console.py"), "--demo"],
                                           stdin=slave, stdout=slave, stderr=slave,
                                           env=dict(os.environ, TERM="xterm"))
                try:
                    output = b""
                    deadline = time.monotonic() + 5
                    while b"3/3 nodes Ready" not in output and time.monotonic() < deadline:
                        if select.select([master], [], [], .2)[0]:
                            output += os.read(master, 65536)
                    self.assertIn(b"3/3 nodes Ready", output)
                    fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 8, 30, 0, 0))
                    # PTYs without a controlling session do not deliver Ctrl-C as
                    # SIGINT; signal it explicitly in that case.
                    if key == b"\x03":
                        import signal
                        process.send_signal(signal.SIGINT)
                    else:
                        os.write(master, key)
                    self.assertEqual(0, process.wait(timeout=3))
                    self.assertTrue(termios.tcgetattr(slave)[3] & termios.ECHO)
                finally:
                    if process.poll() is None:
                        process.kill()
                        process.wait()
                    os.close(master)
                    os.close(slave)

    def test_install_disable_and_failed_screen_handoff(self):
        # Redirect only the installation paths into a private sandbox. Execute
        # the real helper and generated getty wrapper, with systemctl/agetty
        # stand-ins so the test can never alter the host's login services.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            dest = root / "lib"
            dropin = root / "getty.d/50-homestead-console.conf"
            source = (ROOT / "scripts/install-console.sh").read_text().replace("\r\n", "\n")
            source = source.replace("DROPIN=/etc/systemd/system/getty@tty1.service.d/50-homestead-console.conf",
                                    f"DROPIN={dropin}").replace("DEST=/usr/local/lib/homestead", f"DEST={dest}")
            script = root / "install-console.sh"
            script.write_text(source)
            for name, text in {
                "id": "echo 0",
                "systemctl": 'printf "%s\\n" "$*" >> "$TEST_CALLS"',
                "agetty": 'printf "LOGIN %s\\n" "$*"',
            }.items():
                file = root / name
                file.write_text("#!/bin/sh\n" + text + "\n")
                file.chmod(0o755)
            payload = root / "host-console.py"
            payload.write_text("raise RuntimeError('failed screen')\n")
            env = dict(os.environ, PATH=str(root) + os.pathsep + os.environ["PATH"],
                       TEST_CALLS=str(root / "calls"))
            result = subprocess.run(["sh", str(script), "enable", str(payload)],
                                    env=env, capture_output=True, text=True, timeout=10)
            self.assertEqual(0, result.returncode, result.stderr)
            self.assertIn("Type=idle", dropin.read_text())
            self.assertNotIn("ExecStartPre", dropin.read_text())
            result = subprocess.run(["sh", str(dest / "console-getty")],
                                    env=env, capture_output=True, text=True, timeout=10)
            self.assertEqual(0, result.returncode)
            self.assertIn("LOGIN --noclear - linux", result.stdout)
            # Disabling preserves unrelated getty customisations.
            other = dropin.parent / "local.conf"
            other.write_text("# site customisation\n")
            result = subprocess.run(["sh", str(script), "disable"],
                                    env=env, capture_output=True, text=True, timeout=10)
            self.assertEqual(0, result.returncode, result.stderr)
            self.assertFalse(dropin.exists())
            self.assertTrue(other.exists())
            calls = (root / "calls").read_text()
            self.assertNotIn("restart", calls)
            self.assertNotIn("stop", calls)
