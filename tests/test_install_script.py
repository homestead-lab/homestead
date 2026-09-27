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
