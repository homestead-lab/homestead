"""What's in an image update: the two images' configs, read once and checked
against their digests, and what changed between them (names only)."""
import hashlib
import json
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
import homestead_updates as UPDATES
import homestead_update_notes as NOTES


def digest(body):
    return "sha256:" + hashlib.sha256(body).hexdigest()


def blob(obj):
    body = json.dumps(obj, sort_keys=True).encode()
    return body, digest(body)


class Registry:
    """A registry by path: manifests/<ref> and blobs/<digest>, per repo."""
    def __init__(self):
        self.paths, self.calls = {}, []

    def image(self, repo, config, tag=None, index=True, size=None):
        cfg_body, cfg_digest = blob({"architecture": "amd64", "config": config})
        self.paths[(repo, "blobs/" + cfg_digest)] = cfg_body
        manifest, man_digest = blob({"schemaVersion": 2, "config": {
            "digest": cfg_digest, "size": len(cfg_body) if size is None else size}, "layers": []})
        self.paths[(repo, "manifests/" + man_digest)] = manifest
        top, top_digest = manifest, man_digest
        if index:
            top, top_digest = blob({"schemaVersion": 2, "manifests": [
                {"digest": digest(b"arm"), "platform": {"os": "linux", "architecture": "arm64"}},
                {"digest": man_digest, "platform": {"os": "linux", "architecture": "amd64"}}]})
            self.paths[(repo, "manifests/" + top_digest)] = top
        if tag:
            self.paths[(repo, "manifests/" + tag)] = top
        return {"top": top_digest, "manifest": man_digest, "config": cfg_digest}

    def __call__(self, parsed, path, auths, accept="application/json"):
        self.calls.append((parsed["repo"], path, dict(auths)))
        body = self.paths.get((parsed["repo"], path))
        if body is None:
            raise UPDATES.urllib.error.HTTPError("https://registry.example.com", 404, "not found", {}, None)
        return body, {}


class ConfigTests(unittest.TestCase):
    def setUp(self):
        NOTES._CONFIGS.clear()
        self.registry = Registry()
        patch = mock.patch.object(UPDATES, "_registry_json", self.registry)
        patch.start()
        self.addCleanup(patch.stop)

    def test_an_index_is_followed_to_this_architecture_and_its_config_read(self):
        d = self.registry.image("example/app", {"User": "1000", "Env": ["A=1"]})
        config = NOTES.image_config("registry.example.com/example/app@" + d["top"], {})
        self.assertEqual({"User": "1000", "Env": ["A=1"]}, config)
        self.assertEqual(["manifests/" + d["top"], "manifests/" + d["manifest"], "blobs/" + d["config"]],
                         [path for _, path, _ in self.registry.calls])

    def test_a_second_look_at_the_same_digest_asks_the_registry_nothing(self):
        d = self.registry.image("example/app", {"User": "1000"})
        ref = "registry.example.com/example/app@" + d["top"]
        NOTES.image_config(ref, {})
        self.registry.calls.clear()
        self.assertEqual({"User": "1000"}, NOTES.image_config(ref, {}))
        self.assertEqual([], self.registry.calls)

    def test_the_cache_is_kept_per_registry_and_repository(self):
        d = self.registry.image("example/app", {"User": "1000"})
        NOTES.image_config("registry.example.com/example/app@" + d["top"], {})
        with self.assertRaises(Exception):
            NOTES.image_config("registry.example.com/example/other@" + d["top"], {})
        self.registry.calls.clear()
        NOTES.image_config("other.example.com/example/app@" + d["top"], {})
        self.assertEqual(3, len(self.registry.calls))

    def test_a_tag_is_read_each_time_and_cached_only_by_the_digest_it_named(self):
        d = self.registry.image("example/app", {"User": "1000"}, tag="1.2.0")
        NOTES.image_config("registry.example.com/example/app:1.2.0", {})
        self.assertNotIn("1.2.0", json.dumps([list(k) for k in NOTES._CONFIGS]))
        self.registry.calls.clear()
        NOTES.image_config("registry.example.com/example/app:1.2.0", {})
        self.assertEqual(["manifests/1.2.0"], [path for _, path, _ in self.registry.calls])
        self.registry.calls.clear()
        NOTES.image_config("registry.example.com/example/app@" + d["top"], {})
        self.assertEqual([], self.registry.calls)

    def test_a_config_blob_that_does_not_match_its_digest_is_refused_and_not_kept(self):
        d = self.registry.image("example/app", {"User": "1000"})
        self.registry.paths[("example/app", "blobs/" + d["config"])] = b'{"config": {"User": "0"}}'
        with self.assertRaisesRegex(ValueError, "digest"):
            NOTES.image_config("registry.example.com/example/app@" + d["top"], {})
        self.assertEqual({}, NOTES._CONFIGS)

    def test_a_manifest_that_does_not_match_its_digest_is_refused(self):
        d = self.registry.image("example/app", {"User": "1000"})
        self.registry.paths[("example/app", "manifests/" + d["manifest"])] = json.dumps(
            {"config": {"digest": d["config"], "size": 10}}).encode() + b" "
        with self.assertRaisesRegex(ValueError, "digest"):
            NOTES.image_config("registry.example.com/example/app@" + d["top"], {})
        self.assertEqual({}, NOTES._CONFIGS)

    def test_a_manifest_without_a_sha256_config_digest_is_refused(self):
        for config in ({}, {"digest": ""}, {"digest": "md5:abc", "size": 2}):
            body, man_digest = blob({"schemaVersion": 2, "config": config})
            self.registry.paths[("example/app", "manifests/" + man_digest)] = body
            self.registry.calls.clear()
            with self.assertRaisesRegex(ValueError, "config"):
                NOTES.image_config("registry.example.com/example/app@" + man_digest, {})
            self.assertFalse(any(path.startswith("blobs/") for _, path, _ in self.registry.calls))
        self.assertEqual({}, NOTES._CONFIGS)

    def test_a_reference_by_another_kind_of_digest_is_refused(self):
        with self.assertRaisesRegex(ValueError, "sha256"):
            NOTES.image_config("registry.example.com/example/app@sha512:" + "a" * 128, {})
        self.assertEqual([], self.registry.calls)

    def test_a_config_larger_than_a_mebibyte_is_not_read(self):
        d = self.registry.image("example/app", {"User": "1000"}, size=2 << 20)
        with self.assertRaisesRegex(ValueError, "large"):
            NOTES.image_config("registry.example.com/example/app@" + d["top"], {})
        self.assertNotIn("blobs/" + d["config"], [path for _, path, _ in self.registry.calls])

    def test_the_workloads_pull_credentials_go_with_every_request(self):
        d = self.registry.image("example/app", {})
        auths = {"registry.example.com": ("puller", "pw")}
        NOTES.image_config("registry.example.com/example/app@" + d["top"], auths)
        self.assertTrue(all(a == auths for _, _, a in self.registry.calls))

    def test_the_cache_is_bounded(self):
        with mock.patch.object(NOTES, "_CONFIGS_MAX", 2):
            for n in range(3):
                d = self.registry.image("example/app", {"Env": [f"N={n}"]})
                NOTES.image_config("registry.example.com/example/app@" + d["top"], {})
            self.assertEqual(2, len(NOTES._CONFIGS))


if __name__ == "__main__":
    unittest.main()
