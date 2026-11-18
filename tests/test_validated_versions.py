"""The validated-versions list: its shape, its signature, fetching it and
looking a version up (issue #1348, section 5)."""
import base64
import copy
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "server"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import homestead_ed25519 as ED25519
import homestead_validated as VALIDATED
import homestead_validated_bundled as BUNDLED
from rebind import keep_bound

SECRET = bytes(range(32))
T = 1_800_000_000          # a time well after the last try (0)
PUBLIC = ED25519.public_key(SECRET).hex()


def signed(document, secret=SECRET):
    document = copy.deepcopy(document)
    document["signature"] = base64.b64encode(ED25519.sign(secret, VALIDATED.canonical(document))).decode()
    return document


def newer(generated="2026-12-01T00:00:00Z", **sections):
    document = {"generated": generated, "host": {}, "platform": {}, "images": {}}
    document.update(sections)
    return document


class Store(unittest.TestCase):
    def setUp(self):
        keep_bound(self, VALIDATED)
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        VALIDATED.DATA_DIR = folder.name
        self.enterContext(mock.patch.object(VALIDATED, "PUBLIC_KEY", PUBLIC))
        self.fetched = []
        self.answer = None

        def fake(url, **options):
            self.fetched.append(url)
            if isinstance(self.answer, Exception):
                raise self.answer
            return json.dumps(self.answer)
        VALIDATED.fetch = fake


class ShapeTests(unittest.TestCase):
    def test_the_built_in_list_is_well_formed_and_names_the_bad_kernel(self):
        VALIDATED.check(BUNDLED.LIST)
        entry = next(e for e in BUNDLED.LIST["host"]["ubuntu-26.04-amd64"] if e["version"] == "7.0.0-43.43")
        self.assertEqual(("bad", ["kubevirt"]), (entry["status"], entry["affects"]))
        self.assertEqual({"kernel.apparmor_restrict_unprivileged_unconfined": "0"}, entry["workaround"]["sysctl"])

    def test_what_is_refused(self):
        for document, words in ((newer(generated="yesterday"), "generated time"),
                                (newer(host={"ubuntu": []}), "distribution, release and architecture"),
                                (newer(platform={"minikube": []}), "platform part"),
                                (newer(platform={"k3s": [{"version": "v1", "status": "fine"}]}), "status"),
                                (newer(host={"ubuntu-26.04-amd64": [{"version": "1", "status": "bad"}]}), "package"),
                                ([], "JSON object")):
            with self.subTest(words=words), self.assertRaisesRegex(ValueError, words):
                VALIDATED.check(document)

    def test_the_signature_covers_everything_but_itself(self):
        document = signed(newer())
        self.assertTrue(VALIDATED.verify(document, PUBLIC))
        changed = copy.deepcopy(document)
        changed["platform"]["k3s"] = [{"version": "v1.37.1+k3s1", "status": "validated"}]
        self.assertFalse(VALIDATED.verify(changed, PUBLIC))
        self.assertFalse(VALIDATED.verify(signed(newer(), bytes(32)), PUBLIC), "another key")
        self.assertFalse(VALIDATED.verify(document, ""), "no key: nothing verifies")
        self.assertFalse(VALIDATED.verify({**document, "signature": "not base64!"}, PUBLIC))


class FetchTests(Store):
    def test_a_newer_signed_list_is_kept_and_used(self):
        self.answer = signed(newer(platform={"k3s": [{"version": "v1.37.1+k3s1", "status": "validated"}]}))
        self.assertIn("downloaded", VALIDATED.tick(now=T))
        self.assertEqual([VALIDATED.URL], self.fetched)
        document, source = VALIDATED.current()
        self.assertEqual(("downloaded", "2026-12-01T00:00:00Z"), (source, document["generated"]))
        self.assertEqual("validated", VALIDATED.status("platform", "k3s", "v1.37.1+k3s1")["status"])

    def test_twice_a_day_at_most(self):
        self.answer = signed(newer())
        VALIDATED.tick(now=T)
        VALIDATED.tick(now=T + VALIDATED.EVERY - 1)
        self.assertEqual(1, len(self.fetched))
        VALIDATED.tick(now=T + VALIDATED.EVERY)
        self.assertEqual(2, len(self.fetched))

    def test_an_unsigned_or_older_list_is_not_used(self):
        self.answer = newer()
        self.assertIn("signature does not match", VALIDATED.tick(now=T))
        self.assertEqual("built-in", VALIDATED.current()[1])
        self.assertIn("signature", VALIDATED.summary()["error"])
        self.answer = signed(newer(generated="2026-01-01T00:00:00Z"))
        self.assertEqual("", VALIDATED.tick(now=T + VALIDATED.EVERY))
        self.assertEqual("built-in", VALIDATED.current()[1], "older than the built-in list")
        self.assertEqual("", VALIDATED.summary()["error"])

    def test_a_cached_copy_that_no_longer_verifies_is_dropped(self):
        self.answer = signed(newer())
        VALIDATED.tick(now=T)
        path = Path(VALIDATED.DATA_DIR, "validated-versions.json")
        tampered = json.loads(path.read_text())
        tampered["platform"]["k3s"] = [{"version": "v9", "status": "validated"}]
        path.write_text(json.dumps(tampered))
        self.assertEqual("built-in", VALIDATED.current()[1])

    def test_offline_the_built_in_list_is_used_and_no_key_means_no_fetch(self):
        self.answer = OSError("no route to host")
        self.assertIn("could not be downloaded", VALIDATED.tick(now=T))
        self.assertEqual("built-in", VALIDATED.current()[1])
        with mock.patch.object(VALIDATED, "PUBLIC_KEY", ""):
            self.fetched.clear()
            self.assertEqual("", VALIDATED.tick(now=10 ** 9))
            self.assertEqual([], self.fetched)

    def test_a_list_older_than_thirty_days_is_stale(self):
        generated = VALIDATED._when(BUNDLED.LIST["generated"])
        self.assertFalse(VALIDATED.summary(now=generated + 29 * 86400)["stale"])
        self.assertTrue(VALIDATED.summary(now=generated + 31 * 86400)["stale"])


class StatusTests(Store):
    def test_a_version_the_list_does_not_name_is_untested_never_validated(self):
        self.assertEqual("bad", VALIDATED.status("host", "ubuntu-26.04-amd64", "7.0.0-43.43",
                                                 "linux-image-7.0.0-43-generic")["status"])
        self.assertEqual("untested", VALIDATED.status("host", "ubuntu-26.04-amd64", "7.0.0-44.44",
                                                      "linux-image-7.0.0-44-generic")["status"])
        self.assertEqual("untested", VALIDATED.status("platform", "longhorn", "v1.13.0")["status"])
        self.assertTrue(VALIDATED.validated_tier("ubuntu-26.04-amd64"))
        self.assertFalse(VALIDATED.validated_tier("debian-13-amd64"))


class SettingsTests(Store):
    def test_a_new_install_starts_on_validated_versions_and_an_earlier_one_keeps_any(self):
        self.assertEqual({"platform": "validated", "host": "validated"}, VALIDATED.settings())
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        VALIDATED.DATA_DIR = folder.name
        Path(folder.name, "history.json").write_text("{}")
        self.assertEqual({"platform": "any", "host": "any"}, VALIDATED.settings())
        Path(folder.name, "history.json").unlink()
        self.assertEqual({"platform": "any", "host": "any"}, VALIDATED.settings(), "recorded on first read, so it holds")

    def test_saved_choices_are_checked(self):
        VALIDATED.save_settings({"platform": "any", "host": "validated"})
        self.assertEqual({"platform": "any", "host": "validated"}, VALIDATED.settings())
        with self.assertRaisesRegex(ValueError, "validated or any"):
            VALIDATED.save_settings({"platform": "newest", "host": "any"})


def _parse(version):
    import homestead_components as COMPONENTS
    return COMPONENTS.parse(version)


def _next(current, available):
    import homestead_components as COMPONENTS
    return COMPONENTS.next_step(current, available)


class PlatformOfferTests(Store):
    AVAILABLE = ["v1.10.2", "v1.10.1", "v1.9.3", "v1.9.2", "v1.9.1"]

    def listing(self, **platform):
        self.answer = signed(newer(platform=platform))
        VALIDATED.tick(now=T)

    def test_validated_only_offers_the_newest_validated_step_and_names_the_rest(self):
        self.listing(kubevirt=[{"version": "v1.9.2", "status": "validated"},
                               {"version": "v1.9.3", "status": "bad", "reason": "VMs do not start"}])
        target, held, note = VALIDATED.platform_offer("kubevirt", "v1.9.1", self.AVAILABLE, _next, _parse)
        self.assertEqual("v1.9.2", target)
        self.assertEqual({"v1.9.3": "bad", "v1.10.1": "untested", "v1.10.2": "untested"}, {h["version"]: h["status"] for h in held})
        self.assertIn("3 newer versions not yet validated", note)

    def test_a_part_the_list_has_not_covered_is_offered_as_before_with_a_note(self):
        self.listing(kubevirt=[])
        target, held, note = VALIDATED.platform_offer("kubevirt", "v1.9.1", self.AVAILABLE, _next, _parse)
        self.assertEqual(("v1.9.3", []), (target, held))
        self.assertIn("has not validated any version", note)

    def test_any_version_offers_the_next_step_but_says_when_it_is_bad(self):
        self.listing(kubevirt=[{"version": "v1.9.2", "status": "validated"},
                               {"version": "v1.9.3", "status": "bad", "reason": "VMs do not start"}])
        VALIDATED.save_settings({"platform": "any", "host": "any"})
        target, held, note = VALIDATED.platform_offer("kubevirt", "v1.9.1", self.AVAILABLE, _next, _parse)
        self.assertEqual("v1.9.3", target)
        self.assertEqual(["v1.9.3"], [h["version"] for h in held])
        self.assertIn("known to break something: VMs do not start", note)

    def test_the_components_report_offers_only_what_the_list_allows(self):
        import homestead_components as COMPONENTS
        self.listing(longhorn=[{"version": "v1.12.1", "status": "validated"}])
        with mock.patch.object(COMPONENTS, "releases", lambda kind, force=False: (["v1.12.2", "v1.12.1"], "")):
            row = COMPONENTS._row("longhorn", "Longhorn", "v1.12.0", "longhorn", "helmchart")
        self.assertEqual(("v1.12.1", ["v1.12.2"]), (row["next"], [h["version"] for h in row["held"]]))


class KernelTests(Store):
    FACTS = {"id": "ubuntu", "version": "26.04", "arch": "x86_64", "kernel": "7.0.0-43-generic",
             "kernel_package_version": "7.0.0-43.43"}

    def test_the_running_kernel_s_status_and_its_alert(self):
        found = VALIDATED.kernel_status(self.FACTS)
        self.assertEqual(("bad", "ubuntu-26.04-amd64"), (found["status"], found["key"]))
        alert = VALIDATED.kernel_alert("node-a", self.FACTS, "/nodes")
        self.assertEqual("node-a runs kernel 7.0.0-43-generic, which breaks KubeVirt VMs", alert["title"])
        self.assertEqual("degraded", alert["severity"])
        good = {**self.FACTS, "kernel": "7.0.0-44-generic", "kernel_package_version": "7.0.0-44.44"}
        self.assertEqual("untested", VALIDATED.kernel_status(good)["status"])
        self.assertIsNone(VALIDATED.kernel_alert("node-a", good, "/nodes"))

    def test_off_the_validated_tier_kernels_are_not_covered(self):
        other = {**self.FACTS, "version": "24.04"}
        self.assertEqual("not-covered", VALIDATED.kernel_status(other)["status"])
        self.assertIsNone(VALIDATED.kernel_alert("node-a", other, "/nodes"))
        self.assertEqual("unknown", VALIDATED.kernel_status({**self.FACTS, "kernel_package_version": ""})["status"])

    def test_the_host_read_carries_the_architecture_and_kernel_package(self):
        import homestead_host_os as HOST_OS
        facts = HOST_OS.parse("OSID ubuntu 26.04\nKERNEL 7.0.0-43-generic\nARCH x86_64\nPKG apt\nKPKG 7.0.0-43.43\nEND\n")
        self.assertEqual(("x86_64", "7.0.0-43.43"), (facts["arch"], facts["kernel_package_version"]))
        self.assertIn("""dpkg-query -W -f='${Version}' "linux-image-$(uname -r)\"""", HOST_OS.SCRIPT)


class ScriptTests(unittest.TestCase):
    def test_sign_then_verify_and_keygen_keeps_the_private_key_off_disk(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        path = Path(folder.name, "validated-versions.json")
        path.write_text(json.dumps(newer()))
        script = str(ROOT / "scripts" / "validated_versions.py")
        env = {**os.environ, "VALIDATED_VERSIONS_KEY": SECRET.hex()}
        subprocess.run([sys.executable, script, "sign", str(path)], check=True, env=env, capture_output=True)
        done = subprocess.run([sys.executable, script, "verify", str(path), PUBLIC], capture_output=True, text=True)
        self.assertEqual(0, done.returncode, done.stderr)
        made = subprocess.run([sys.executable, script, "keygen"], capture_output=True, text=True, check=True)
        secret = made.stdout.strip()
        self.assertEqual(64, len(secret))
        self.assertIn(ED25519.public_key(bytes.fromhex(secret)).hex(), made.stderr)
        self.assertNotIn(secret, made.stderr)


if __name__ == "__main__":
    unittest.main()
