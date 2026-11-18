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
