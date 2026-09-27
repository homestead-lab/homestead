"""scripts/install.sh: the one-line installer and node doctor, run with
--dry-run and answers given ahead, so what it would do can be checked
without a machine to install on. The doctor runs against stand-ins for
systemctl, k3s and the rest."""
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "install.sh"
SH = shutil.which("dash") or shutil.which("sh")


def run(args, env=None, path_extra=None):
    text = SCRIPT.read_text(encoding="utf-8").replace("\r\n", "\n")
    with tempfile.TemporaryDirectory() as tmp:
        script = Path(tmp, "install.sh")
        script.write_text(text, encoding="utf-8", newline="\n")
        full = dict(os.environ, **(env or {}))
        full.pop("KUBECONFIG", None)
        if path_extra:
            full["PATH"] = path_extra + os.pathsep + full["PATH"]
        result = subprocess.run([SH, str(script), *args], capture_output=True, text=True, env=full,
                                stdin=subprocess.DEVNULL, timeout=120)
        return result.returncode, result.stdout + result.stderr


@unittest.skipUnless(SH, "no POSIX shell here")
class InstallerTests(unittest.TestCase):
    def test_it_is_plain_posix_shell(self):
        text = SCRIPT.read_text(encoding="utf-8").replace("\r\n", "\n")
        with tempfile.NamedTemporaryFile("w", suffix=".sh", delete=False, newline="\n", encoding="utf-8") as f:
            f.write(text)
        try:
            self.assertEqual(0, subprocess.run([SH, "-n", f.name]).returncode)
        finally:
            os.unlink(f.name)

    def test_a_new_cluster_runs_bootstrap_with_the_answers(self):
        code, out = run(["--dry-run", "--skip-checks"], {"HS_ROLE": "new", "HS_NODE_IP": "10.0.0.5",
                                                          "HS_LONGHORN": "no", "HS_KUBEVIRT": "yes", "HS_YES": "1"})
        self.assertIn("+ sh /tmp/homestead-bootstrap-k3s.sh server --node-ip 10.0.0.5 --no-longhorn --kubevirt", out)
        self.assertIn("http://10.0.0.5:8088", out)

    def test_joining_as_a_worker(self):
        code, out = run(["--dry-run", "--skip-checks"], {"HS_ROLE": "agent", "HS_NODE_IP": "10.0.0.6",
                                                          "HS_SERVER": "10.0.0.5", "HS_TOKEN": "tok", "HS_YES": "1"})
        self.assertIn("+ sh /tmp/homestead-bootstrap-k3s.sh agent https://10.0.0.5:6443 tok --node-ip 10.0.0.6", out)

    def test_a_new_rke2_cluster_always_has_longhorn(self):
        code, out = run(["--dry-run", "--skip-checks"], {"HS_ROLE": "new", "HS_DIST": "rke2", "HS_NODE_IP": "10.0.0.5",
                                                          "HS_LONGHORN": "no", "HS_KUBEVIRT": "no", "HS_YES": "1"})
        self.assertIn("+ sh /tmp/homestead-bootstrap-k3s.sh server --node-ip 10.0.0.5 --rke2", out)
        self.assertNotIn("--no-longhorn", out, "RKE2 has no storage of its own")

    def test_joining_an_rke2_cluster_uses_its_supervisor_port(self):
        code, out = run(["--dry-run", "--skip-checks"], {"HS_ROLE": "server", "HS_DIST": "rke2", "HS_NODE_IP": "10.0.0.6",
                                                          "HS_SERVER": "10.0.0.5", "HS_TOKEN": "tok", "HS_YES": "1"})
        self.assertIn("+ sh /tmp/homestead-bootstrap-k3s.sh join https://10.0.0.5:9345 tok --node-ip 10.0.0.6 --rke2", out)

    def test_an_unknown_kubernetes_is_refused(self):
        code, out = run(["--dry-run", "--skip-checks"], {"HS_ROLE": "new", "HS_DIST": "k0s", "HS_NODE_IP": "10.0.0.5", "HS_YES": "1"})
        self.assertNotEqual(0, code)
        self.assertIn("Set HS_DIST to k3s or rke2", out)

    def test_versions_set_ahead_are_pinned(self):
        code, out = run(["--dry-run", "--skip-checks"], {"HS_ROLE": "new", "HS_DIST": "rke2", "HS_NODE_IP": "10.0.0.5",
                                                          "HS_KUBEVIRT": "yes", "HS_K8S_VERSION": "v1.33.4+rke2r1",
                                                          "HS_LONGHORN_VERSION": "v1.9.1", "HS_KUBEVIRT_VERSION": "v1.6.0",
                                                          "HS_CDI_VERSION": "v1.62.0", "HS_VERSION": "2.8.180", "HS_YES": "1"})
        self.assertIn("server --node-ip 10.0.0.5 --kubevirt --rke2 --rke2-version v1.33.4+rke2r1 --longhorn-version v1.9.1"
                      " --kubevirt-version v1.6.0 --cdi-version v1.62.0 --homestead-version 2.8.180", out)

    def test_a_joining_node_takes_the_version_given(self):
        code, out = run(["--dry-run", "--skip-checks"], {"HS_ROLE": "agent", "HS_NODE_IP": "10.0.0.6", "HS_SERVER": "10.0.0.5",
                                                          "HS_TOKEN": "tok", "HS_K8S_VERSION": "v1.32.8+k3s1", "HS_YES": "1"})
        self.assertIn("agent https://10.0.0.5:6443 tok --node-ip 10.0.0.6 --k3s-version v1.32.8+k3s1", out)

    def test_no_versions_given_means_no_pins_and_no_lookups(self):
        code, out = run(["--dry-run", "--skip-checks"], {"HS_ROLE": "new", "HS_NODE_IP": "10.0.0.5", "HS_LONGHORN": "yes",
                                                          "HS_KUBEVIRT": "no", "HS_YES": "1"})
        self.assertIn("+ sh /tmp/homestead-bootstrap-k3s.sh server --node-ip 10.0.0.5\n", out)
        self.assertNotIn("-version", out)
        self.assertNotIn("Retrieving release information", out)

    def test_summary_declined_changes_nothing(self):
        code, out = run(["--dry-run", "--skip-checks"], {"HS_ROLE": "new", "HS_NODE_IP": "10.0.0.5", "HS_LONGHORN": "yes",
                                                          "HS_KUBEVIRT": "no", "HS_YES": "no"})
        self.assertNotEqual(0, code)
        self.assertIn("Installation cancelled. No changes were made.", out)
        self.assertNotIn("bootstrap-k3s.sh server", out)

    def test_harvester_gets_the_manifest_with_its_address_and_class(self):
        code, out = run(["--dry-run", "--skip-checks"], {"HS_ROLE": "harvester", "HS_VIP": "192.0.2.250",
                                                          "HS_CLASS": "harvester-longhorn", "HS_YES": "1"})
        self.assertIn("s/192\\.0\\.2\\.242/192.0.2.250/g", out)
        self.assertIn("s/longhorn-r2/harvester-longhorn/g", out)
        self.assertIn("apply -f /tmp/homestead-deploy.yaml", out)

    def test_a_question_with_no_terminal_and_no_answer_says_which_to_give(self):
        code, out = run(["--dry-run", "--skip-checks"], {"HS_ROLE": "agent", "HS_NODE_IP": "10.0.0.6", "HS_YES": "1"})
        self.assertNotEqual(0, code)
        self.assertIn("HS_SERVER", out)


@unittest.skipUnless(SH and sys.platform != "win32", "package-manager stand-ins need POSIX")
class MenuBootstrapTests(unittest.TestCase):
    """Run the actual startup over stdin, without root or real package changes."""

    def startup(self, manager="apt-get", behavior="success", args=(), extra_env=None):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            pkg = root / manager
            pkg.write_text("""#!/bin/sh
printf '%s %s\\n' "$(basename "$0")" "$*" >> "$TEST_CALLS"
printf 'repository progress\\n'
printf 'package diagnostic\\n' >&2
case "$TEST_BEHAVIOR" in
  fail) exit 42 ;;
  retry)
    if [ ! -e "$TEST_RETRIED" ]; then
      touch "$TEST_RETRIED"
      exit 100
    fi ;;
  hang) trap '' TERM; sleep 10 ;;
esac
# A package hook trying to read input must see EOF, not installer source.
cat > "$TEST_CONSUMED"
touch "$TEST_INSTALLED"
""", encoding="utf-8")
            pkg.chmod(0o755)
            real_timeout = shutil.which("timeout")
            if not real_timeout:
                self.skipTest("timeout is unavailable")
            timer = root / "timeout"
            timer.write_text(f"""#!/bin/sh
[ "$1 $2 $3" = '-k 10 120' ] || exit 99
shift 3
exec '{real_timeout}' -k 0.1 0.2 "$@"
""", encoding="utf-8")
            timer.chmod(0o755)
            script = SCRIPT.read_text(encoding="utf-8").split("# Boxes are as tall", 1)[0]
            script = script.replace("TTY=/dev/tty", 'TTY="$TEST_TTY"')
            # A regular file stands in for the terminal; preserve successive writes.
            script = script.replace('> "$TTY"', '>> "$TTY"')
            # Control discovery, never the bootstrap under test. Other host
            # package managers must not become an accidental fallback.
            script = script.replace("# HS_UI=", """interactive() { return 0; }
id() { echo 0; }
have() {
  case "$1" in
    whiptail) [ -f "$TEST_INSTALLED" ] ;;
    dialog) [ "${TEST_DIALOG:-0}" = 1 ] ;;
    timeout) [ "${TEST_NO_TIMEOUT:-0}" = 0 ] ;;
    apt-get|dnf|yum|zypper|apk) [ "$1" = "$TEST_MANAGER" ] ;;
    *) command -v "$1" >/dev/null 2>&1 ;;
  esac
}
# HS_UI=""")
            # More than a shell input buffer remains when the package command
            # runs. Reading stdin here used to eat the remainder of curl | sh.
            script += "\n" + "# remaining installer source\n" * 4096
            script += "printf 'INSTALLER_CONTINUES:%s\\n' \"$UI\"\n"
            env = dict(os.environ, PATH=tmp + os.pathsep + os.environ["PATH"], TERM="xterm",
                       HS_UI="", SUDO_USER="", TEST_MANAGER=manager, TEST_BEHAVIOR=behavior,
                       TEST_TTY=str(root / "terminal"), TEST_CALLS=str(root / "calls"),
                       TEST_INSTALLED=str(root / "installed"), TEST_CONSUMED=str(root / "consumed"),
                       TEST_RETRIED=str(root / "retried"))
            env.update(extra_env or {})
            if behavior == "installed":
                (root / "installed").touch()
            result = subprocess.run([SH, "-s", "--", *args], input=script, text=True,
                                    capture_output=True, env=env, timeout=5)
            def read(name):
                path = root / name
                return path.read_text() if path.exists() else ""
            return result, read("terminal"), read("calls"), read("consumed")

    def test_package_output_is_visible_and_hooks_cannot_eat_the_piped_installer(self):
        for manager in ("apt-get", "dnf", "yum", "zypper", "apk"):
            with self.subTest(manager=manager):
                result, terminal, calls, consumed = self.startup(manager)
                self.assertEqual(0, result.returncode, result.stderr)
                self.assertIn("INSTALLER_CONTINUES:whiptail", result.stdout)
                self.assertIn("repository progress", terminal)
                self.assertIn("package diagnostic", terminal)
                self.assertIn("opening the installer", terminal)
                self.assertIn(manager, calls)
                self.assertEqual("", consumed)

    def test_failed_package_install_continues_in_text_mode_with_diagnostics(self):
        result, terminal, calls, _ = self.startup(behavior="fail")
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn("INSTALLER_CONTINUES:text", result.stdout)
        self.assertIn("failed (exit 42)", terminal)
        self.assertIn("package diagnostic", terminal)
        self.assertNotIn("opening the installer", terminal)

    def test_stalled_package_does_not_block_installer_and_falls_back_to_text(self):
        result, terminal, calls, _ = self.startup(behavior="hang")
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn("INSTALLER_CONTINUES:text", result.stdout)
        self.assertIn("timed out", terminal)
        self.assertEqual(1, len(calls.splitlines()), "do not retry after a timeout")

    def test_apt_can_refresh_stale_indexes_and_retry(self):
        result, terminal, calls, _ = self.startup(behavior="retry")
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn("INSTALLER_CONTINUES:whiptail", result.stdout)
        commands = calls.splitlines()
        self.assertEqual(3, len(commands))
        self.assertTrue(commands[0].endswith("install -y whiptail"))
        self.assertTrue(commands[1].endswith("update"))
        self.assertTrue(commands[2].endswith("install -y whiptail"))

    def test_text_report_dry_run_and_existing_menus_do_not_install_packages(self):
        cases = [(("--text",), {}, "success", "text"),
                 (("--report",), {}, "success", "text"),
                 (("--fix-safe",), {}, "success", "text"),
                 (("--dry-run",), {}, "success", "text"),
                 ((), {"HS_UI": "text"}, "success", "text"),
                 ((), {}, "installed", "whiptail"),
                 ((), {"TEST_DIALOG": "1"}, "success", "dialog")]
        for args, env, behavior, ui in cases:
            with self.subTest(args=args, env=env, behavior=behavior):
                result, terminal, calls, _ = self.startup(args=args, extra_env=env, behavior=behavior)
                self.assertEqual(0, result.returncode, result.stderr)
                self.assertIn("INSTALLER_CONTINUES:" + ui, result.stdout)
                self.assertEqual("", calls)

    def test_missing_timeout_or_package_manager_uses_text_without_installing(self):
        for env in ({"TEST_NO_TIMEOUT": "1"}, {"TEST_MANAGER": "none"}):
            with self.subTest(env=env):
                result, terminal, calls, _ = self.startup(extra_env=env)
                self.assertEqual(0, result.returncode, result.stderr)
                self.assertIn("INSTALLER_CONTINUES:text", result.stdout)
                self.assertIn("text prompts", terminal)
                self.assertEqual("", calls)


@unittest.skipUnless(SH and sys.platform != "win32", "terminal stand-ins need POSIX")
class TerminalSizeTests(unittest.TestCase):
    def test_nested_piped_menu_queries_size_without_changing_terminal_settings(self):
        self.check_rows("40", 0, "40")

    def test_missing_tput_failed_query_or_invalid_dimensions_use_24_rows(self):
        for value, status in (("", 127), ("99", 1), ("0", 0), ("-1", 0), ("bad", 0)):
            with self.subTest(value=value, status=status):
                self.check_rows(value, status, "24")

    def check_rows(self, value, status, expected):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for name, content in {
                "tput": '#!/bin/sh\n[ "$1" = lines ] || exit 1\ncat >/dev/null\nprintf "%s\\n" "$TEST_ROWS"\nexit "$TEST_STATUS"\n',
                "stty": '#!/bin/sh\ntouch "$TEST_STTY_CALLED"\nexit 1\n',
            }.items():
                path = root / name
                path.write_text(content, encoding="utf-8")
                path.chmod(0o755)
            source = SCRIPT.read_text(encoding="utf-8")
            function = source[source.index("term_rows() {"):source.index("text_lines() {")]
            script = 'TTY=/dev/null\n' + function
            script += 'menu() { rows=$(term_rows); printf "ROWS:%s\\n" "$rows"; }\npick=$(menu)\n'
            script += '# remaining source\n' * 4096 + 'printf "%s\\n" "$pick"\n'
            env = dict(os.environ, PATH=tmp + os.pathsep + os.environ["PATH"],
                       TEST_ROWS=value, TEST_STATUS=str(status), TEST_STTY_CALLED=str(root / "called"))
            result = subprocess.run([SH, "-s"], input=script, capture_output=True,
                                    text=True, env=env, timeout=5)
            self.assertEqual(0, result.returncode, result.stderr)
            self.assertEqual("ROWS:" + expected + "\n", result.stdout)
            self.assertFalse((root / "called").exists(), "size queries must not change terminal settings")


@unittest.skipUnless(SH and sys.platform != "win32", "PTY handoff needs POSIX")
class TerminalHandoffTests(unittest.TestCase):
    def startup(self, args=(), env=None, prompt=True):
        import pty
        import select
        import time
        master, slave = pty.openpty()
        process = None
        try:
            source = SCRIPT.read_text(encoding="utf-8").split("# Boxes are as tall", 1)[0]
            source = source.replace("TTY=/dev/tty", 'TTY="$TEST_TTY"')
            source += "\nprintf 'STARTUP_COMPLETE\\n'\n"
            full = dict(os.environ, SUDO_USER="rancher", HS_ROLE="", HS_UI="text",
                        TERM="xterm", TEST_TTY=os.ttyname(slave))
            full.update(env or {})
            process = subprocess.Popen([SH, "-s", "--", *args], stdin=subprocess.PIPE,
                                       stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=full)
            process.stdin.write(source)
            process.stdin.close()
            process.stdin = None
            seen = b""
            if prompt:
                deadline = time.monotonic() + 3
                while b"Press Enter" not in seen and time.monotonic() < deadline:
                    if select.select([master], [], [], 0.1)[0]:
                        seen += os.read(master, 4096)
                self.assertIn(b"Press Enter to open Homestead setup", seen)
                self.assertIsNone(process.poll(), "must wait for the terminal, not read script input")
                os.write(master, b"\n")
            out, err = process.communicate(timeout=5)
            self.assertEqual(0, process.returncode, err)
            self.assertIn("STARTUP_COMPLETE", out)
            if not prompt:
                self.assertFalse(select.select([master], [], [], 0)[0], "unattended mode must not prompt")
        finally:
            if process is not None and process.poll() is None:
                process.kill()
                process.communicate()
            os.close(master)
            os.close(slave)

    def test_piped_sudo_reads_the_terminal_before_opening_the_interface(self):
        self.startup()

    def test_reports_and_unattended_installs_do_not_wait_for_enter(self):
        for args, env in [(("--report",), {}), (("--fix-safe",), {}),
                          ((), {"HS_ROLE": "agent"}), ((), {"SUDO_USER": ""})]:
            with self.subTest(args=args, env=env):
                self.startup(args=args, env=env, prompt=False)


@unittest.skipUnless(SH and sys.platform != "win32", "cluster fixtures need POSIX")
class ClusterOverviewTests(unittest.TestCase):
    def overview(self, mode="running", again=False):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            kubectl = root / "kubectl"
            kubectl.write_text("""#!/bin/sh
printf '%s\\n' "$*" >> "$TEST_CALLS"
[ "$1" = --request-timeout=3s ] || exit 98
shift
case "$*" in
  'config view '*) echo 'https://127.0.0.1:6443' ;;
  'get nodes '*)
    [ "$TEST_MODE" != unreachable ] || exit 1
    if [ "$TEST_MODE" = many ]; then
      for n in 1 2 3 4 5 6 7 8; do printf 'node%s|192.0.2.%s|True|v1.34.1+k3s1\\n' "$n" "$n"; done
      exit 0
    fi
    printf 'node1|192.0.2.108|True|v1.34.1+k3s1\\nnode2|192.0.2.109|False|v1.33.5+k3s1\\n' ;;
  '-n longhorn-system get daemonset '*)
    [ "$TEST_MODE" != denied ] || exit 1
    [ "$TEST_MODE" != absent ] || exit 0
    echo 'longhorn-manager|longhornio/longhorn-manager:v1.9.2|1|2' ;;
  '-n lab get deployment '*)
    [ "$TEST_MODE" != denied ] || exit 1
    [ "$TEST_MODE" != absent ] || exit 0
    if [ "$TEST_MODE" = digest ]; then
      echo 'homestead|ghcr.io/wjcloudy/homestead@sha256:1234567890abcdef1234567890abcdef1234567890abcdef1234567890abcdef1234|1|1'
    else echo 'homestead|ghcr.io/wjcloudy/homestead:2.8.199|1|1'; fi ;;
  'get services '*)
    [ "$TEST_MODE" != denied ] || exit 1
    [ "$TEST_MODE" != absent ] || exit 0
    printf 'lab/homestead|192.0.2.242 | \\nlab/nas-data||192.0.2.243 \\nlab/pending|| \\n' ;;
  *) exit 99 ;;
esac
""", encoding="utf-8")
            kubectl.chmod(0o755)
            script = SCRIPT.read_text(encoding="utf-8")
            script = script.split("# Main-menu discovery", 1)[1].split("# ------------------------------------------------------------------ findings", 1)[0]
            script = "# Main-menu discovery" + script
            script += """
have() { command -v "$1" >/dev/null 2>&1; }
hostname() { echo node1; }
default_ip() { echo 192.0.2.108; }
term_rows() { echo 24; }
os_name() { echo Ubuntu; }
msg() { printf '%s\\n' "$2"; }
KIND=k3s-server; KC=kubectl; UI=text; here='k3s server node'
[ "$TEST_MODE" != worker ] || { KIND=k3s-agent; KC=''; }
cluster_overview
"""
            if again:
                script += "TEST_MODE=unreachable; export TEST_MODE; cluster_overview\n"
            if mode == "many":
                script += 'prepare_overview; n=1; while [ "$n" -le "$CLUSTER_PAGES" ]; do CLUSTER_PAGE=$n; echo PAGE; main_overview; n=$((n+1)); done\n'
            script += 'main_overview; echo DETAILS; cluster_details; printf "PRESENT=%s\\n" "$HOMESTEAD_PRESENT"\n'
            env = dict(os.environ, PATH=tmp + os.pathsep + os.environ["PATH"],
                       TEST_MODE=mode, TEST_CALLS=str(root / "calls"))
            result = subprocess.run([SH, "-s"], input=script, text=True, env=env,
                                    capture_output=True, timeout=10)
            self.assertEqual(0, result.returncode, result.stderr)
            calls = (root / "calls").read_text() if (root / "calls").exists() else ""
            return result.stdout, calls

    def test_main_menu_shows_live_members_versions_component_readiness_and_vips(self):
        out, calls = self.overview()
        summary, details = out.split("DETAILS", 1)
        for expected in ("192.0.2.108", "node2", "v1.34.1+k3s1", "v1.33.5+k3s1",
                         "not ready (1/2); longhorn-manager:v1.9.2", "ready (1/1); homestead:2.8.199",
                         "192.0.2.242", "127.0.0.1:6443"):
            self.assertIn(expected, summary)
        self.assertTrue(all(len(line) <= 80 for line in summary.splitlines()))
        self.assertIn("node2  192.0.2.109  NotReady", details)
        self.assertIn("192.0.2.243 (requested; pending)", details)
        self.assertIn("lab/pending  pending address", details)
        self.assertIn("PRESENT=yes", details)
        self.assertEqual(5, len(calls.splitlines()))
        self.assertNotIn("--raw", calls)

    def test_long_member_lists_can_be_paged_without_losing_addresses(self):
        out, _ = self.overview("many")
        summary = out.split("DETAILS", 1)[0]
        self.assertGreaterEqual(summary.count("PAGE"), 2)
        for n in range(1, 9):
            self.assertIn(f"192.0.2.{n}", summary)
        self.assertNotIn("...", summary)
        self.assertTrue(all(len(line) <= 80 for line in summary.splitlines()))

    def test_addresses_wrap_and_remain_available_in_full(self):
        out, _ = self.overview()
        summary = out.split("DETAILS", 1)[0]
        self.assertIn("192.0.2.243", summary)
        self.assertIn("pending address", summary)
        self.assertNotIn("...", summary)
        self.assertTrue(all(len(line) <= 80 for line in summary.splitlines()))

    def test_digest_pinned_image_keeps_readiness_visible_on_the_main_menu(self):
        out, _ = self.overview("digest")
        summary = out.split("DETAILS", 1)[0]
        self.assertIn("Homestead   ready (1/1); homestead@sha256:1234567890ab", summary)
        self.assertIn("digest pinned", summary)
        self.assertNotIn("1234567890abcdef1234567890abcdef", summary)

    def test_absent_components_are_distinct_from_forbidden_queries(self):
        absent, _ = self.overview("absent")
        self.assertIn("Longhorn    not installed", absent)
        self.assertIn("Homestead   not installed", absent)
        self.assertIn("PRESENT=no", absent)
        self.assertIn("no LoadBalancer services", absent)
        denied, _ = self.overview("denied")
        self.assertIn("Longhorn    unavailable", denied)
        self.assertIn("Homestead   unavailable", denied)
        self.assertIn("VIPs        unavailable", denied)
        self.assertIn("PRESENT=unknown", denied)
        self.assertNotIn("not installed", denied)

    def test_failed_api_stops_followup_queries_and_clears_previous_status(self):
        out, calls = self.overview("unreachable")
        self.assertEqual(2, len(calls.splitlines()))
        self.assertIn("Cluster API unavailable or access denied", out)
        self.assertIn("PRESENT=unknown", out)
        out, _ = self.overview(again=True)
        self.assertNotIn("node2", out)
        self.assertNotIn("2.8.199", out)
        self.assertIn("Homestead   unavailable", out)

    def test_worker_without_credentials_keeps_a_useful_menu_without_querying(self):
        out, calls = self.overview("worker")
        self.assertEqual("", calls)
        self.assertIn("192.0.2.108", out)
        self.assertIn("require server-node credentials", out)
        self.assertIn("PRESENT=unknown", out)


STUBS = {
    "systemctl": """#!/bin/sh
case "$1" in
  is-active) [ "$3" = k3s ] || [ "$2" = k3s ] || [ "$3" = iscsid ] ;;
  list-unit-files) [ "$2" = k3s.service ] && echo "k3s.service enabled enabled" ;;
  *) exit 0 ;;
esac""",
    "timedatectl": "#!/bin/sh\necho yes",
    "hostname": "#!/bin/sh\necho node1",
    "df": """#!/bin/sh
echo "Filesystem 1024-blocks Used Available Capacity Mounted"
echo "/dev/sda1 100 50 50 50% /" """,
    "journalctl": "#!/bin/sh\nexit 0",
    "k3s": """#!/bin/sh
[ "$1" = kubectl ] && shift
case "$*" in
  "get --raw /readyz"|"get --raw /readyz/etcd") echo ok ;;
  "get node node1 -o jsonpath"*) echo "True true" ;;
  "get nodes --no-headers") echo "node1 Ready control-plane 1d v1.31" ;;
  "get pods -A --no-headers")
    echo "lab plex-1 0/1 CrashLoopBackOff 5 1h"
    echo "lab web-1 1/1 Running 0 1h" ;;
  "get pods -A --field-selector=status.phase=Failed --no-headers")
    echo "lab old-1 0/1 Evicted 0 1d"; echo "lab old-2 0/1 Evicted 0 1d" ;;
  *"-n lab get deploy homestead"*) case "$*" in *jsonpath*) echo 1 ;; *) echo found ;; esac ;;
  *"get crd"*) exit 1 ;;
  *"coredns"*) case "$*" in *jsonpath*) echo 1 ;; *) echo found ;; esac ;;
  *) exit 0 ;;
esac""",
}


@unittest.skipUnless(SH and sys.platform != "win32", "the stand-ins are shell scripts on PATH")
class DoctorTests(unittest.TestCase):
    def setUp(self):
        self.bin = tempfile.mkdtemp()
        for name, text in STUBS.items():
            path = Path(self.bin, name)
            path.write_text(text + "\n", encoding="utf-8", newline="\n")
            path.chmod(path.stat().st_mode | stat.S_IEXEC)

    def tearDown(self):
        shutil.rmtree(self.bin, ignore_errors=True)

    def test_the_report_names_what_is_wrong_and_exits_to_match(self):
        code, out = run(["--report", "--dry-run"], path_extra=self.bin)
        self.assertIn("node1 (k3s-server)", out)
        self.assertIn("[WARN] Node node1 is cordoned (fix available)", out)
        self.assertIn("[WARN] Failing pods: 1", out)
        self.assertIn("[WARN] Failed pods: 2 (fix available)", out)
        self.assertIn("[ OK ] k3s service is running", out)
        self.assertIn("[ OK ] Homestead is running", out)
        self.assertEqual(1, code, "warnings, no failures")

    def test_fix_safe_uncordons_and_clears_failed_pods_but_leaves_the_rest(self):
        code, out = run(["--fix-safe", "--dry-run"], path_extra=self.bin)
        self.assertIn("+ k3s kubectl uncordon node1", out)
        self.assertIn("+ k3s kubectl delete pods -A --field-selector=status.phase=Failed", out)
        self.assertNotIn("delete pod plex-1", out, "restarting failing pods is asked, never automatic")


if __name__ == "__main__":
    unittest.main()
