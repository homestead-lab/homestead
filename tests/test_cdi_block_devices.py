"""CDI block imports need non-root device ownership on every host role."""
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "server"))
import homestead_vms as VMS

SCRIPT = ROOT / "scripts" / "bootstrap-k3s.sh"
SH = shutil.which("dash") or shutil.which("sh")


def function(name):
    text = SCRIPT.read_text(encoding="utf-8")
    start = text.index(name + "() {")
    return text[start:text.index("\n}", start) + 2]


HELPERS ='fail() { echo "$*" >&2; exit 1; }\n' + function("channel_release") + "\n" + function("attempts") + "\n"


@unittest.skipUnless(SH and os.name != "nt", "requires a POSIX shell")
class ChannelReleaseTests(unittest.TestCase):
    def release(self, answers):
        """channel_release with curl answering each URL as `answers` says
        (an empty answer is a failed request)."""
        cases = " ".join(f'*{key}*) {"echo " + value if value else "return 22"} ;;' for key, value in answers.items())
        stub = 'curl() { for a; do last=$a; done; case "$last" in ' + cases + ' *) return 22 ;; esac; }\n'
        result = subprocess.run([SH], input="set -eu\n" + stub + HELPERS + "channel_release rke2 stable || true\n",
                                text=True, capture_output=True, timeout=10)
        return result.stdout.strip()

    def test_the_channel_server_names_the_release(self):
        self.assertEqual("v1.36.5+rke2r1", self.release({"update.rke2.io": "https://github.com/rancher/rke2/releases/tag/v1.36.5+rke2r1"}))

    def test_github_stands_in_while_the_channel_server_is_down(self):
        # update.rke2.io answering 404: never "stable" taken as a version.
        self.assertEqual("v1.37.1+rke2r1", self.release({"update.rke2.io": "",
                                                         "releases/latest": "https://github.com/rancher/rke2/releases/tag/v1.37.1+rke2r1"}))

    def test_nothing_when_neither_answers(self):
        self.assertEqual("", self.release({}))


@unittest.skipUnless(SH and os.name != "nt", "requires a POSIX shell")
class BootstrapDeviceTests(unittest.TestCase):
    def test_k3s_servers_and_workers_keep_options_and_enable_device_ownership(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp, "arguments")
            curl = Path(tmp, "curl")
            curl.write_text("#!/bin/sh\ncat <<'INSTALLER'\n"
                            'printf "%s\\n" "$@" > "$TEST_ARGS"\nINSTALLER\n')
            curl.chmod(0o755)
            env = dict(os.environ, PATH=tmp + os.pathsep + os.environ["PATH"], TEST_ARGS=str(output))
            for args in ("server --cluster-init", "server --server https://192.0.2.10:6443", "agent"):
                script = "set -eu\nsay() { :; }\nK3S_VERSION=v1.34.6+k3s1\nNODE_IP=192.0.2.11\n"
                script += HELPERS + function("install_k3s") + "\n" + function("get_k3s") + "\ninstall_k3s " + args + "\n"
                result = subprocess.run([SH], input=script, text=True, capture_output=True, env=env, timeout=10)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(output.read_text().splitlines(), args.split() +
                                 ["--node-ip", "192.0.2.11", "--nonroot-devices"])

    def test_rke2_servers_and_workers_enable_device_ownership_and_keep_join_settings(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp, "rke2")
            # Only the extracted function is run, with host paths redirected to a fixture.
            body = (HELPERS + function("install_rke2") + "\n" + function("get_rke2")).replace("/etc/rancher/rke2", str(config))
            # The release is looked up (a redirect to its tag); the installer itself is a no-op.
            prelude = ('set -eu\nsay() { :; }\nsystemctl() { :; }\n'
                       'curl() { case "$*" in *url_effective*) echo https://github.com/rancher/rke2/releases/tag/v1.34.6+rke2r1 ;; *) echo ":" ;; esac; }\n')
            prelude += 'NODE_IP=192.0.2.11\nJOIN_TAINT=homestead.io/storage-pending=longhorn:PreferNoSchedule\nRKE2_VERSION=""\n'
            for role in ("server", "agent"):
                result = subprocess.run([SH], input=prelude + body +
                    f"\ninstall_rke2 {role} https://192.0.2.10:9345 fixture-token\n",
                    text=True, capture_output=True, timeout=10)
                self.assertEqual(result.returncode, 0, result.stderr)
                settings = (config / "config.yaml").read_text()
                self.assertIn("nonroot-devices: true\n", settings)
                self.assertIn("server: https://192.0.2.10:9345\n", settings)
                self.assertIn("node-ip: 192.0.2.11\n", settings)
                self.assertIn("token: fixture-token\n", settings)
                self.assertEqual("enable-servicelb: true" in settings, role == "server")


class BlockImportDiagnosticsTests(unittest.TestCase):
    def reasons(self, message):
        def get(path):
            if path.endswith("/pods"):
                return {"items": [{"metadata": {"name": "importer-prime-claim-uid"}, "status": {
                    "containerStatuses": [{"state": {"waiting": {"reason": "CrashLoopBackOff"}}}]}}]}
            return {"metadata": {"uid": "claim-uid"}}
        dv = {"metadata": {"name": "test-disk"}, "status": {"conditions": [
            {"type": "Bound", "status": "False", "message": "target PVC test-disk Pending"}]}}
        with mock.patch.object(VMS, "kget", get), mock.patch.object(VMS, "events_for",
                lambda ns, name, uid="": [{"type": "Warning", "message": message}] if name == "prime-claim-uid" else []):
            return VMS._why_not_filling("lab", dv)

    def test_permission_error_is_not_lost_after_long_combined_event_prefix(self):
        message = "Starting importer exit status 1; " * 20 + "blockdev: cannot open /dev/cdi-block-volume: Permission denied"
        reasons = self.reasons(message)
        self.assertIn("CDI cannot access its block device", reasons[0])
        self.assertIn("nonroot-devices", reasons[0])
        self.assertIn("Keep the disk PVC and DataVolume", reasons[0])
        self.assertIn("target PVC test-disk Pending", reasons)

    def test_unrelated_importer_errors_do_not_suggest_node_configuration_changes(self):
        for message in ("Downloading image: HTTP 403 Permission denied", "blockdev: cannot open /dev/cdi-block-volume: No such file"):
            with self.subTest(message=message):
                reasons = self.reasons(message)
                self.assertFalse(any("nonroot-devices" in text for text in reasons))
                self.assertEqual(reasons[0], "target PVC test-disk Pending")


if __name__ == "__main__":
    unittest.main()
