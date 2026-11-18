"""Host kernels on validated versions only (issue #1348, section 5.3)."""
import base64
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import homestead_ed25519 as ED25519
import homestead_host_os as HOST_OS
import homestead_kernel_hold as KERNEL
import homestead_validated as VALIDATED
from rebind import keep_bound

SECRET = bytes(range(32))
T = 1_800_000_000
FACTS = {"id": "ubuntu", "version": "26.04", "arch": "x86_64", "package_manager": "apt", "at": T,
         "kernel": "7.0.0-43-generic", "kernel_package_version": "7.0.0-43.43",
         "kernels": ["linux-image-7.0.0-38-generic=7.0.0-38.38", "linux-image-7.0.0-43-generic=7.0.0-43.43",
                     "linux-headers-7.0.0-43-generic=7.0.0-43.43"],
         "held": []}
VALIDATED_38 = {"package": "linux-image-7.0.0-38-generic", "version": "7.0.0-38.38", "status": "validated"}
VALIDATED_45 = {"package": "linux-image-7.0.0-45-generic", "version": "7.0.0-45.45", "status": "validated"}


class Host:
    def __init__(self, answer="HELD linux-generic linux-image-generic apparmor"):
        self.answer, self.scripts = answer, []

    def run(self, node, script, timeout=60, change=""):
        self.scripts.append(script)
        return self.answer, ""


class Base(unittest.TestCase):
    def setUp(self):
        keep_bound(self, VALIDATED)
        keep_bound(self, KERNEL)
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        VALIDATED.DATA_DIR = KERNEL.DATA_DIR = folder.name
        public = ED25519.public_key(SECRET).hex()
        self.enterContext(mock.patch.object(VALIDATED, "PUBLIC_KEY", public))
        self.host = Host()
        KERNEL.hostrun = self.host

    def listing(self, *entries):
        document = {"generated": "2030-01-01T00:00:00Z", "platform": {}, "images": {},
                    "host": {"ubuntu-26.04-amd64": list(entries)}}
        document["signature"] = base64.b64encode(ED25519.sign(SECRET, VALIDATED.canonical(document))).decode()
        VALIDATED.fetch = lambda url, **options: json.dumps(document)
        VALIDATED.tick(now=T)


class InstallTests(Base):
    def test_an_update_installs_the_newest_validated_kernel_by_its_exact_version(self):
        self.listing(VALIDATED_38, VALIDATED_45)
        command = KERNEL.install_command(FACTS)
        self.assertIn("install linux-image-7.0.0-45-generic=7.0.0-45.45 linux-modules-7.0.0-45-generic=7.0.0-45.45", command)
        self.assertIn("linux-headers-7.0.0-45-generic=7.0.0-45.45", command, "headers too, as the host has some")

    def test_nothing_extra_when_installed_off_the_tier_or_on_any_version(self):
        self.listing(VALIDATED_45)
        self.assertEqual("", KERNEL.install_command({**FACTS, "version": "24.04"}))
        installed = {**FACTS, "kernels": FACTS["kernels"] + ["linux-image-7.0.0-45-generic=7.0.0-45.45"]}
        self.assertEqual("", KERNEL.install_command(installed), "7.0.0-45 is installed already")
        VALIDATED.save_settings({"platform": "validated", "host": "any"})
        self.assertEqual("", KERNEL.install_command(FACTS))

    def test_the_fallback_is_the_newest_validated_kernel_installed(self):
        self.listing(VALIDATED_38, VALIDATED_45)
        self.assertEqual("7.0.0-38-generic", KERNEL.fallback(FACTS))

    def test_no_fallback_without_a_validated_kernel_installed(self):
        self.listing(VALIDATED_45)
        self.assertEqual("", KERNEL.fallback(FACTS))


class UpgradeLaunchTests(Base):
    def test_the_host_s_update_installs_the_kernel_after_refreshing_and_before_upgrading(self):
        self.listing(VALIDATED_45)
        sent = []

        class Runner:
            def hold(self, node, what, seconds):
                return "lease"

            def let_go(self, node, lease):
                pass

            def run(self, node, script, **options):
                sent.append(script)
                return "STARTED", ""
        keep_bound(self, HOST_OS)
        HOST_OS.hostrun = Runner()
        HOST_OS.upgrade_begin("node-a", facts={**FACTS, "updates": [{"name": "curl"}]})
        launched = sent[-1]
        refresh, kernel, upgrade = (launched.index(part) for part in
                                    ("apt-get -o DPkg::Lock::Timeout=300 update", "linux-image-7.0.0-45-generic=7.0.0-45.45",
                                     "upgrade --with-new-pkgs"))
        self.assertLess(refresh, kernel)
        self.assertLess(kernel, upgrade)


class HoldTests(Base):
    def test_held_once_then_left_until_a_newer_read_shows_a_hold_missing(self):
        self.listing(VALIDATED_38)
        self.assertEqual({"node-a": "held linux-generic, linux-image-generic, apparmor"},
                         KERNEL.tick({"node-a": FACTS}, now=T + 10))
        self.assertIn("apt-mark hold", self.host.scripts[-1])
        KERNEL.tick({"node-a": FACTS}, now=T + 20)
        self.assertEqual(1, len(self.host.scripts), "no newer read: no command")
        read = {**FACTS, "at": T + 30, "held": ["linux-generic", "linux-image-generic", "apparmor"]}
        KERNEL.tick({"node-a": read}, now=T + 40)
        self.assertEqual(1, len(self.host.scripts), "all still held")
        read = {**FACTS, "at": T + 50, "held": ["apparmor"]}
        KERNEL.tick({"node-a": read}, now=T + 60)
        self.assertEqual(2, len(self.host.scripts), "a hold went missing: held again")

    def test_nothing_is_held_until_the_list_has_validated_a_kernel_here(self):
        self.listing()
        self.assertEqual({}, KERNEL.tick({"node-a": FACTS}, now=T + 10))
        self.assertEqual([], self.host.scripts)

    def test_any_version_lets_go_of_only_homestead_s_holds(self):
        self.listing(VALIDATED_38)
        before = {**FACTS, "held": ["linux-firmware"]}       # a person's own hold
        self.host.answer = "HELD linux-generic linux-firmware"
        KERNEL.tick({"node-a": before}, now=T + 10)
        VALIDATED.save_settings({"platform": "validated", "host": "any"})
        self.host.answer = "RELEASED"
        self.assertEqual({"node-a": "released linux-generic"}, KERNEL.tick({"node-a": before}, now=T + 20))
        self.assertIn("apt-mark unhold linux-generic", self.host.scripts[-1])
        self.assertNotIn("linux-firmware", self.host.scripts[-1])

    def test_off_the_tier_and_off_apt_nothing_happens(self):
        self.listing(VALIDATED_38)
        KERNEL.tick({"a": {**FACTS, "version": "24.04"}, "b": {**FACTS, "package_manager": "dnf"}}, now=T + 10)
        self.assertEqual([], self.host.scripts)

    def test_the_scripts_are_valid_shell(self):
        for script in (KERNEL._hold_script(), KERNEL._release_script(["linux-generic"]),
                       KERNEL.boot_script("7.0.0-38-generic")):
            with self.subTest(script=script[:30]):
                done = subprocess.run(["sh", "-n"], input=script, text=True, capture_output=True)
                self.assertEqual(0, done.returncode, done.stderr)
        with self.assertRaises(ValueError):
            KERNEL.boot_script("7.0.0-38-generic; reboot")


class BootTests(Base):
    def test_boot_points_grub_at_the_fallback_and_says_what_failed(self):
        self.listing(VALIDATED_38)
        with mock.patch.object(HOST_OS, "stored", lambda node: FACTS):
            self.host.answer = "SET gnulinux-7.0.0-38-generic-advanced-1234"
            self.assertEqual("7.0.0-38-generic", KERNEL.boot_validated({"node": "node-a"})["kernel"])
            self.assertIn("gnulinux-7.0.0-38-generic-advanced-", self.host.scripts[-1])
            for answer, words in (("NOGRUB", "does not boot with GRUB"), ("NOENTRY", "no entry"), ("", "could not set")):
                self.host.answer = answer
                with self.subTest(answer=answer), self.assertRaisesRegex(ValueError, words):
                    KERNEL.boot_validated({"node": "node-a"})
        with mock.patch.object(HOST_OS, "stored", lambda node: {**FACTS, "kernel": "7.0.0-38-generic"}):
            with self.assertRaisesRegex(ValueError, "already runs"):
                KERNEL.boot_validated({"node": "node-a"})

    def test_the_host_read_lists_installed_kernels_and_holds(self):
        facts = HOST_OS.parse("KINST linux-image-7.0.0-38-generic=7.0.0-38.38 linux-headers-7.0.0-38-generic=7.0.0-38.38 \n"
                              "HOLDS linux-generic apparmor \nEND\n")
        self.assertEqual(["linux-image-7.0.0-38-generic=7.0.0-38.38", "linux-headers-7.0.0-38-generic=7.0.0-38.38"], facts["kernels"])
        self.assertEqual(["linux-generic", "apparmor"], facts["held"])


if __name__ == "__main__":
    unittest.main()
