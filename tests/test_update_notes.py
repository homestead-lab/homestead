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


SRC = "org.opencontainers.image.source"
VER = "org.opencontainers.image.version"
REV = "org.opencontainers.image.revision"
URL = "org.opencontainers.image.url"


class LinkTests(unittest.TestCase):
    def test_a_github_source_links_its_releases_and_the_code_between_two_revisions(self):
        out = NOTES.release_links({REV: "4be1d2c09a7e11"}, {SRC: "https://github.com/example/app.git", VER: "1.2.0",
                                                           REV: "9c0f31aa52d8ff"}, "1.1.0", "1.2.0")
        self.assertEqual({"release": "https://github.com/example/app/releases?q=1.2.0&expanded=true",
                          "compare": "https://github.com/example/app/compare/4be1d2c09a7e...9c0f31aa52d8"},
                         out["links"])
        self.assertEqual(("1.1.0", "1.2.0"), (out["from_version"], out["to_version"]))

    def test_no_code_link_without_two_different_revisions(self):
        for old in ({}, {REV: "9c0f31aa52d8"}, {REV: "not-a-sha"}):
            out = NOTES.release_links(old, {SRC: "https://github.com/example/app", REV: "9c0f31aa52d8"}, "a", "b")
            self.assertNotIn("compare", out["links"])

    def test_gitlab_and_other_hosts(self):
        self.assertEqual("https://gitlab.example.com/group/app/-/releases",
                         NOTES.release_links({}, {SRC: "https://gitlab.example.com/group/app"}, "", "")["links"]["release"])
        self.assertEqual("https://code.example.com/group/app",
                         NOTES.release_links({}, {SRC: "https://code.example.com/group/app/"}, "", "")["links"]["release"])

    def test_plain_http_is_a_link_too(self):
        out = NOTES.release_links({}, {SRC: "http://code.example.com/group/app", URL: "http://app.example.com/"}, "", "")
        self.assertEqual({"release": "http://code.example.com/group/app", "project": "http://app.example.com/"}, out["links"])

    def test_only_http_and_https_without_credentials_become_links(self):
        for bad in ("javascript:alert(1)", "JaVaScRiPt://github.com/a/b", "data:text/html,x", "ftp://example.com/a/b",
                    "git+https://github.com/example/app", "https://user:pw@github.com/example/app",
                    "https://token@example.com/a/b", "//github.com/example/app", "https://", "https://exa mple.com/a/b",
                    "https://example.com/a/b\nx", "https://example.com/" + "a" * 3000):
            out = NOTES.release_links({}, {SRC: bad, URL: bad}, "", "")
            self.assertEqual({}, out["links"], bad)
            self.assertEqual("", out["source"], bad)

    def test_the_project_site_is_not_repeated_when_it_is_the_release_link(self):
        out = NOTES.release_links({}, {SRC: "https://code.example.com/a/b", URL: "https://code.example.com/a/b"}, "", "")
        self.assertEqual({"release": "https://code.example.com/a/b"}, out["links"])

    def test_no_labels_no_links_and_the_tags_stand_in_for_versions(self):
        out = NOTES.release_links({}, {}, "1.0", "1.1")
        self.assertEqual(({}, "1.0", "1.1"), (out["links"], out["from_version"], out["to_version"]))

    def test_a_version_is_quoted_into_the_link_and_kept_short(self):
        out = NOTES.release_links({}, {SRC: "https://github.com/example/app", VER: "1.0&x=<b>" + "9" * 100}, "", "")
        self.assertEqual(64, len(out["to_version"]))
        self.assertIn("q=1.0%26x%3D%3Cb%3E", out["links"]["release"])


class ChangeTests(unittest.TestCase):
    def test_ports_and_volumes_added_and_removed(self):
        out = NOTES.config_changes({"ExposedPorts": {"80/tcp": {}, "81/tcp": {}}, "Volumes": {"/data": {}}},
                                   {"ExposedPorts": {"80/tcp": {}, "8971/tcp": {}}, "Volumes": {"/data": {}, "/media": {}}})
        self.assertEqual({"added": ["8971/tcp"], "removed": ["81/tcp"]}, out["ports"])
        self.assertEqual({"added": ["/media"], "removed": []}, out["volumes"])
        self.assertFalse(out["none"])

    def test_env_is_reported_by_name_only(self):
        old = {"Env": ["TZ=UTC", "LEGACY=1", "DATABASE_URL=postgres://app:hunter2@db.example.com/app", "SAME=x"]}
        new = {"Env": ["TZ=Etc/UTC", "AUTH=on-with-s3cret", "DATABASE_URL=postgres://app:n3wpass@db.example.com/app",
                       "SAME=x", "EMPTY="]}
        out = NOTES.config_changes(old, new)
        self.assertEqual({"added": ["AUTH", "EMPTY"], "removed": ["LEGACY"], "changed": ["DATABASE_URL", "TZ"],
                          "set_by_app": []}, out["env"])
        text = json.dumps(out)
        for value in ("UTC", "hunter2", "n3wpass", "s3cret", "postgres", "db.example.com", '"1"', '"x"'):
            self.assertNotIn(value, text)

    def test_build_details_are_counted_not_listed(self):
        out = NOTES.config_changes({"Env": ["REDIS_VERSION=7.2", "REDIS_DOWNLOAD_SHA=aa", "PYTHON_GET_PIP_URL=a", "GPG_KEYS=1"]},
                                   {"Env": ["REDIS_VERSION=7.4", "REDIS_DOWNLOAD_SHA=bb", "PYTHON_GET_PIP_URL=a", "NODE_SHA256=c"]})
        self.assertEqual({"added": [], "removed": [], "changed": [], "set_by_app": []}, out["env"])
        self.assertEqual(4, out["build_env_changed"])
        self.assertTrue(out["none"])

    def test_a_change_of_user(self):
        self.assertEqual({"was": "root", "now": "1000"}, NOTES.config_changes({}, {"User": "1000"})["user"])
        self.assertIsNone(NOTES.config_changes({"User": ""}, {"User": "root"})["user"])
        self.assertIsNone(NOTES.config_changes({"User": "app"}, {"User": "app"})["user"])

    def test_the_apps_own_user_and_env_win_over_the_images(self):
        out = NOTES.config_changes({"User": "0", "Env": ["TZ=UTC", "A=1"]}, {"User": "1000", "Env": ["TZ=Etc/UTC", "A=2"]},
                                   {"user": "568", "env": {"TZ"}})
        self.assertEqual({"was": "root", "now": "1000", "overridden_by": "568"}, out["user"])
        self.assertEqual(["TZ"], out["env"]["set_by_app"])
        self.assertEqual(["A", "TZ"], out["env"]["changed"])

    def test_the_same_image_config_changes_nothing(self):
        same = {"ExposedPorts": {"80/tcp": {}}, "Env": ["A=1"], "User": "1000", "Volumes": {"/data": {}}}
        out = NOTES.config_changes(same, dict(same))
        self.assertTrue(out["none"])
        self.assertIsNone(out["user"])

    def test_garbled_fields_are_read_as_empty(self):
        out = NOTES.config_changes({"Env": "A=1", "ExposedPorts": ["80"], "Volumes": None, "User": 7},
                                   {"Env": [None, 5, "B"]})
        self.assertEqual(["B"], out["env"]["added"])
        self.assertEqual({"added": [], "removed": []}, out["ports"])


if __name__ == "__main__":
    unittest.main()
