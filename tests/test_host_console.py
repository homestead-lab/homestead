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
        self.assertEqual("server", rows[0]["role"])
        self.assertEqual("ok", rows[0]["health"])
        self.assertEqual("NotReady/cordoned", rows[1]["state"])
        self.assertEqual("bad", rows[1]["health"])

    def test_cluster_unavailable_never_shows_raw_errors(self):
        with patch.object(console, "kube_command", return_value=["kubectl"]), \
             patch.object(console, "command", return_value=None):
            data = console.cluster_stats()
            self.assertIn("API unavailable", data["summary"])
            self.assertEqual("bad", data["health"])
        with patch.object(console, "kube_command", return_value=None):
            data = console.cluster_stats()
            self.assertIn("worker", data["summary"])
            self.assertEqual("muted", data["health"])

    def test_homestead_uses_service_port_and_vip(self):
        service = {"status": {"loadBalancer": {"ingress": [{"ip": "192.0.2.100"}]}},
                   "spec": {"ports": [{"port": 8088, "targetPort": 8080}]}}
        with patch.object(console, "kube_command", return_value=["kubectl"]), \
             patch.object(console, "command", side_effect=['{"items": []}', json.dumps(service)]):
            self.assertEqual("http://192.0.2.100:8088", console.cluster_stats()["url"])

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

    def test_cpu_hotplug_and_counter_resets_are_unknown_not_busy(self):
        samples = console.cpu_samples("cpu 20 0 20 50 10 0 0 0\ncpu0 5 0 5 20 0 0 0 0\n")
        self.assertEqual({"cpu": (100, 60), "cpu0": (30, 20)}, samples)
        self.assertIsNone(console.cpu_percent((1, 1), (100, 60)))
        self.assertIsNone(console.cpu_percent((100, 60), None))
        self.assertEqual(40, console.cpu_percent((200, 120), (100, 60)))

    def test_ready_cordoned_node_is_a_warning(self):
        _, rows = console.node_rows({"items": [{"metadata": {"name": "node-a"},
            "spec": {"unschedulable": True}, "status": {"conditions": [{"type": "Ready", "status": "True"}]}}]})
        self.assertEqual("warn", rows[0]["health"])
        self.assertEqual("Ready/cordoned", rows[0]["state"])


class DashboardTests(unittest.TestCase):
    def test_braille_dot_order_matches_unicode(self):
        # Unicode numbers the bottom pair 7/8, rather than row-major order.
        for x, y, bit in ((0, 0, 1), (0, 1, 2), (0, 2, 4), (1, 0, 8),
                          (1, 1, 16), (1, 2, 32), (0, 3, 64), (1, 3, 128)):
            canvas = console.DotCanvas(1, 1)
            canvas.dot(x, y, "brand")
            self.assertEqual(chr(0x2800 + bit), canvas.cells()[0][0][0])

    def test_logo_is_symmetric_with_two_separated_white_layers(self):
        for large, width, height in ((False, 12, 5), (True, 16, 7)):
            cells = console.logo_cells(large)
            self.assertEqual(height, len(cells))
            dots = []
            for row in cells:
                self.assertEqual(width, len(row))
                for dy in range(4):
                    dots.append([bool((ord(char) - 0x2800 if char != " " else 0) & console.DOT_BITS[dy][dx])
                                 for char, _ in row for dx in range(2)])
            self.assertTrue(all(row == row[::-1] for row in dots), "roof and walls must be symmetric")
            centre = [row[width] for row in dots]
            # A roof peak, two layer strokes, and the bottom of the house.
            self.assertEqual(4, sum(on and (y == 0 or not centre[y - 1]) for y, on in enumerate(centre)))
            self.assertTrue(any(style == "text" and char != " " for row in cells for char, style in row))
            frame = console.dashboard(console.demo_data(), 99 if large else 79, 32 if large else 24)
            self.assertIn("CPU / history", frame.text()[height])
            for y, row in enumerate(cells):
                self.assertEqual(row, frame.rows[y][1:1 + width])

    def test_graph_has_subcell_resolution_and_correct_endpoints(self):
        frame = console.Frame(4, 2)
        frame.graph(0, 0, 4, 2, [0, 0, 12.5, 25, 50, 75, 100, 100])
        self.assertEqual(" ", frame.rows[1][0][0], "zero usage must leave an empty column")
        self.assertEqual("\u28e0", frame.rows[1][1][0], "one/two dots high in adjacent samples")
        self.assertEqual("\u28ff", frame.rows[0][3][0])
        self.assertEqual("\u28ff", frame.rows[1][3][0])
        self.assertEqual("bad", frame.rows[0][3][1])

    def test_plain_character_fallback_preserves_the_dashboard(self):
        frame = console.dashboard(console.demo_data(), 80, 24, braille=False)
        self.assertTrue(all(ord(c) < 128 for row in frame.text() for c in row))
        self.assertIn("node-3", "\n".join(frame.text()))
        self.assertIn("#", "\n".join(frame.text()))

    def test_standard_host_console_shows_metrics_and_all_three_nodes(self):
        frame = console.dashboard(console.demo_data(), 80, 24)
        text = "\n".join(frame.text())
        self.assertEqual(23, len(frame.rows), "one row is reserved for the login key")
        for label in ("CPU / history", "MEMORY", "DISKS / used", "CLUSTER / readiness",
                      "3/3 nodes Ready", "node-1", "node-2", "node-3", "360/960G 38%"):
            self.assertIn(label, text)

    def test_failed_nodes_and_usage_bars_have_semantic_red_styles(self):
        data = console.demo_data(True)
        frame = console.dashboard(data, 80, 24)
        red_text = "".join(c for row in frame.rows for c, style in row if style == "bad")
        green_text = "".join(c for row in frame.rows for c, style in row if style == "ok")
        self.assertIn("NotReady", red_text)
        self.assertIn("node-3", red_text)
        self.assertIn("node-1", green_text)
        self.assertIn("High memory usage", red_text)
        self.assertIn("\u28ff", red_text, "high usage colours the filled bar")

    def test_unknown_metrics_have_no_green_health_claim(self):
        frame = console.dashboard(console.Monitor().snapshot(), 80, 24)
        text = "\n".join(frame.text())
        green_text = "".join(c for row in frame.rows for c, style in row if style == "ok")
        self.assertIn("sampling", text)
        self.assertIn("--%", text)
        self.assertIn("Waiting for metrics", text)
        self.assertEqual("", green_text)

    def test_long_cluster_and_disk_lists_remain_in_scrollable_frame(self):
        data = console.demo_data()
        data["cluster"]["nodes"] *= 20
        data["local"]["disks"] = [dict(data["local"]["disks"][0], path=f"/data/{i}") for i in range(20)]
        frame = console.dashboard(data, 80, 24)
        self.assertGreater(len(frame.rows), 24)
        self.assertEqual(20, sum("/data/" in row for row in frame.text()))
        self.assertEqual(20, sum("node-3" in row for row in frame.text()))

    def test_narrow_terminal_stacks_panels_without_losing_data(self):
        frame = console.dashboard(console.demo_data(), 40, 20)
        text = "\n".join(frame.text())
        for label in ("MEMORY", "DISKS", "CLUSTER", "/var/lib/longhorn", "node-3"):
            self.assertIn(label, text)
        self.assertGreater(len(frame.rows), 20)
        for row in frame.rows:
            self.assertEqual(40, len(row))

    def test_bars_clamp_values_and_zero_capacity_is_unknown(self):
        frame = console.Frame(20, 3)
        frame.bar(0, 0, 10, -20)
        frame.bar(1, 0, 10, 120)
        frame.bar(2, 0, 10, None)
        self.assertEqual("[        ]", frame.text()[0][:10])
        self.assertEqual("[" + "\u28ff" * 8 + "]", frame.text()[1][:10])
        self.assertIsNone(console.percent(10, 0))
        self.assertEqual("muted", console.usage_style(None))


@unittest.skipIf(sys.platform == "win32", "requires a Linux pseudo-terminal")
class ConsoleTerminalTests(unittest.TestCase):
    def test_unicode_ascii_switch_and_non_unicode_locale(self):
        import fcntl
        import pty
        import select
        import struct
        import termios

        def collect(master, marker):
            output = b""
            deadline = time.monotonic() + 5
            while marker not in output and time.monotonic() < deadline:
                if select.select([master], [], [], .2)[0]:
                    output += os.read(master, 65536)
            self.assertIn(marker, output)
            return output

        for ascii_only, unicode_locale in ((False, True), (True, True), (False, False)):
            with self.subTest(ascii_only=ascii_only, unicode_locale=unicode_locale):
                master, slave = pty.openpty()
                fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 24, 80, 0, 0))
                args = [sys.executable, str(ROOT / "scripts/host-console.py"), "--demo"]
                if ascii_only:
                    args.append("--ascii")
                process = subprocess.Popen(args, stdin=slave, stdout=slave, stderr=slave,
                    env=dict(os.environ, TERM="xterm", LC_ALL="C.UTF-8" if unicode_locale else "C",
                             PYTHONUTF8="0", PYTHONCOERCECLOCALE="0"))
                try:
                    output = collect(master, b"A: glyphs")
                    dotted = "\u28ff".encode("utf-8")
                    if unicode_locale and not ascii_only:
                        self.assertIn(dotted, output)
                    else:
                        self.assertNotIn(dotted, output)
                        self.assertIn(b"#", output)
                    if unicode_locale:
                        os.write(master, b"a")
                        output = collect(master, dotted if ascii_only else b"#")
                        if ascii_only:
                            self.assertIn(dotted, output)
                        else:
                            self.assertNotIn(dotted, output)
                            self.assertIn(b"#", output)
                    os.write(master, b"q")
                    self.assertEqual(0, process.wait(timeout=3))
                    self.assertTrue(termios.tcgetattr(slave)[3] & termios.ECHO)
                finally:
                    if process.poll() is None:
                        process.kill()
                        process.wait()
                    os.close(master)
                    os.close(slave)

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
        for key, terminal in ((b"q", "xterm"), (b"\r", "xterm"), (b"\x1b", "xterm"),
                              (b"\x03", "xterm"), (b"q", "vt100")):
            with self.subTest(key=key, terminal=terminal):
                master, slave = pty.openpty()
                fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 24, 80, 0, 0))
                process = subprocess.Popen([sys.executable, str(ROOT / "scripts/host-console.py"), "--demo"],
                                           stdin=slave, stdout=slave, stderr=slave,
                                           env=dict(os.environ, TERM=terminal))
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
                # Someone signed in on the screen unless the test says not.
                "who": '[ -n "$TEST_TTY1_FREE" ] || echo "admin    tty1         2026-09-30 21:00"',
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
            # A session signed in on the screen is never ended.
            calls = (root / "calls").read_text()
            self.assertNotIn("restart", calls)
            self.assertNotIn("stop", calls)
            # With nobody there, the change shows at once.
            env["TEST_TTY1_FREE"] = "1"
            result = subprocess.run(["sh", str(script), "enable", str(payload)],
                                    env=env, capture_output=True, text=True, timeout=10)
            self.assertEqual(0, result.returncode, result.stderr)
            self.assertIn("restart getty@tty1.service", (root / "calls").read_text())
            self.assertIn("showing on tty1 now", result.stdout)


def psf1_font(height=16):
    """A stand-in 256-glyph console font: glyph i is rows of byte i, mapped to U+00i."""
    glyphs = b"".join(bytes([i]) * height for i in range(256))
    table = b"".join(i.to_bytes(2, "little") + b"\xff\xff" for i in range(256))
    return b"\x36\x04" + bytes([2, height]) + glyphs + table


class ConsoleFontTests(unittest.TestCase):
    """Braille on a machine's own screen: a font of our own while it shows."""

    def test_our_font_keeps_their_ascii_and_adds_every_pattern_the_dashboard_draws(self):
        width, height, glyphs, table = console.read_psf(console.console_font(psf1_font()))
        self.assertEqual((8, 16, 256), (width, height, len(glyphs)), "256 glyphs keep the bright colours")
        index = {char: i for i, chars in enumerate(table) for char in chars}
        self.assertEqual(bytes([ord("A")]) * 16, glyphs[index["A"]], "their own letters, unchanged")
        for mask in console.dashboard_masks():
            self.assertIn(chr(0x2800 + mask), index)
        full = glyphs[index["\u28ff"]]
        self.assertEqual(8, sum(bin(b).count("1") for b in full) // 4, "eight dots of four pixels")
        self.assertIn(console.OURS, index)

    def test_each_ascii_glyph_sits_at_its_own_code_so_a_cleared_cell_is_blank(self):
        # The console clears a cell with glyph 32 directly, not through the
        # table: packed from 1, every empty cell showed "@".
        _, _, glyphs, table = console.read_psf(console.console_font(psf1_font()))
        self.assertEqual(bytes(16), glyphs[0x20])
        for char in "A@z~!":
            self.assertEqual(bytes([ord(char)]) * 16, glyphs[ord(char)])
            self.assertIn(char, table[ord(char)])
        braille = [i for i, chars in enumerate(table) if any("⠁" <= c <= "⣿" for c in chars)]
        self.assertTrue(braille and min(braille) >= 0x80, "Braille in the top half, clear of ASCII")

    def test_every_graph_and_bar_pattern_is_in_it(self):
        frame = console.Frame(40, 6)
        frame.graph(0, 0, 20, 3, [i * 5 for i in range(40)])
        frame.bar(4, 0, 30, 67)
        drawn = {ord(c) - 0x2800 for row in frame.rows for c, _ in row if "\u2801" <= c <= "\u28ff"}
        self.assertTrue(drawn)
        self.assertLessEqual(drawn, set(console.dashboard_masks()))

    def test_a_font_left_behind_by_a_crash_is_never_kept_as_theirs(self):
        with self.assertRaises(ValueError):
            console.console_font(console.console_font(psf1_font()))

    def test_without_setfont_the_screen_keeps_plain_characters(self):
        font = console.ConsoleFont()
        font.tty = "/dev/tty1"
        with patch.object(console.shutil, "which", return_value=None):
            self.assertFalse(font.load())
        font.restore()


class LeftoverFontTests(unittest.TestCase):
    """A console stopped before it put their font back left ours loaded."""

    def test_a_leftover_font_is_reset_to_the_systems_then_ours_built_on_that(self):
        system = psf1_font()
        loaded = {"font": console.console_font(system)}       # ours, left behind
        calls = []

        def run(args, **kwargs):
            calls.append(args[0].rsplit("/", 1)[-1] if "/" in args[0] else args[0])
            if args[0] == "setupcon":
                loaded["font"] = system
            elif "-O" in args:
                Path(args[args.index("-O") + 1]).write_bytes(loaded["font"])
            elif len(args) == 4:
                loaded["font"] = Path(args[3]).read_bytes()
            return subprocess.CompletedProcess(args, 0)

        font = console.ConsoleFont()
        font.tty = "/dev/tty1"
        with patch.object(console.shutil, "which", side_effect=lambda name: name), \
                patch.object(console.subprocess, "run", side_effect=run):
            self.assertTrue(font.load())
            _, _, glyphs, table = console.read_psf(loaded["font"])
            self.assertEqual(bytes(16), glyphs[0x20], "no \"@\" in blank cells")
            self.assertEqual(bytes([ord("A")]) * 16, glyphs[ord("A")], "built on the system's font")
            self.assertEqual(["setfont", "setupcon", "setfont", "setfont"], calls)
            font.restore()
        self.assertEqual(system, loaded["font"], "the system's font goes back, never the leftover")

    def test_a_stop_signal_still_puts_their_font_back(self):
        source = (ROOT / "scripts/host-console.py").read_text()
        self.assertIn("signal.SIGTERM, signal.SIGHUP", source)
        self.assertIn("font.restore()", source)
