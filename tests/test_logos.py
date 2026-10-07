import sys
import unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
import homestead_logos as LOGOS


def app(name, repo, icon=None, downloads=0):
    return {"name": name, "repo": repo, "icon": icon if icon is not None else f"https://example.com/{name}.png",
            "downloads": downloads}


CATALOGUE = [
    app("Sonarr", "lscr.io/linuxserver/sonarr", downloads=900),
    app("sonarr-hotio", "ghcr.io/hotio/sonarr:release", downloads=300),
    app("Sonarr4K", "lscr.io/linuxserver/sonarr", icon="https://example.com/Sonarr.png", downloads=10),
    app("Jellyfin", "jellyfin/jellyfin"),
    app("Paperless-ngx", "ghcr.io/paperless-ngx/paperless-ngx"),
    app("paperless", "example/paperless", downloads=5),
    app("No picture", "example/none", icon=""),
    app("Unsafe", "example/unsafe", icon="javascript:alert(1)"),
]


def search(apps, term):
    return [a for a in apps if term.lower() in a["name"].lower()]


class ImageRepoTests(unittest.TestCase):
    def test_registry_tag_digest_and_library_are_dropped(self):
        cases = {
            "lscr.io/linuxserver/sonarr:latest": "linuxserver/sonarr",
            "linuxserver/sonarr": "linuxserver/sonarr",
            "ghcr.io/hotio/sonarr:release": "hotio/sonarr",
            "nginx": "nginx",
            "docker.io/library/nginx@sha256:" + "a" * 64: "nginx",
            "registry.example.com:5000/team/app:1.2": "team/app",
            "localhost/app": "app",
            "Jellyfin/Jellyfin": "jellyfin/jellyfin",
            "": "",
            "bad image": "",
        }
        for ref, repo in cases.items():
            with self.subTest(ref=ref):
                self.assertEqual(repo, LOGOS.image_repo(ref))


class SuggestTests(unittest.TestCase):
    def test_the_image_match_comes_first_and_each_picture_once(self):
        tiles = LOGOS.suggest(CATALOGUE, ["lscr.io/linuxserver/sonarr:latest"], "sonarr", search)
        self.assertEqual(("Sonarr", "image"), (tiles[0]["name"], tiles[0]["match"]))
        self.assertEqual(["Sonarr", "sonarr-hotio"], [t["name"] for t in tiles], "Sonarr4K shares Sonarr's picture")
        self.assertEqual("name", tiles[1]["match"])

    def test_no_image_match_is_just_the_search(self):
        tiles = LOGOS.suggest(CATALOGUE, ["registry.example.com/beamng:1"], "jelly", search)
        self.assertEqual([("Jellyfin", "name")], [(t["name"], t["match"]) for t in tiles])

    def test_apps_without_a_web_logo_are_never_offered(self):
        names = [t["name"] for t in LOGOS.suggest(CATALOGUE, [], "u", search)]
        self.assertNotIn("Unsafe", names)
        self.assertNotIn("No picture", names)

    def test_the_limit_holds(self):
        many = [app(f"app{i}", f"example/app{i}") for i in range(40)]
        self.assertEqual(5, len(LOGOS.suggest(many, [], "app", search, limit=5)))


class MissingTests(unittest.TestCase):
    def test_only_apps_that_can_have_a_logo_and_have_none(self):
        workloads = [
            {"ns": "lab", "name": "sonarr", "images": ["lscr.io/linuxserver/sonarr:latest"]},
            {"ns": "lab", "name": "paperless", "images": ["registry.example.com/paperless:2"]},
            {"ns": "lab", "name": "beamng", "images": ["registry.example.com/beamng:1"]},
            {"ns": "lab", "name": "jellyfin", "images": ["jellyfin/jellyfin"], "icon": "data:image/png;base64,AA"},
            {"ns": "lab", "name": "healing", "images": ["jellyfin/jellyfin"], "has_logo": True},
            {"ns": "lab", "name": "plain", "images": ["jellyfin/jellyfin"], "logo_skipped": True},
            {"ns": "kube-system", "name": "coredns", "images": ["coredns/coredns"], "platform": True},
            {"ns": "lab", "name": "homestead", "images": ["x"], "self": True},
            {"ns": "lab", "name": "smb", "images": ["x"], "managed_smb": True},
        ]
        rows = LOGOS.missing(workloads, CATALOGUE)
        self.assertEqual(["sonarr", "paperless", "beamng"], [r["name"] for r in rows], "image, name, then none")
        self.assertEqual("image", rows[0]["suggestion"]["match"])
        self.assertEqual(("paperless", "name"), (rows[1]["suggestion"]["name"], rows[1]["suggestion"]["match"]))
        self.assertIsNone(rows[2]["suggestion"])



class SaveLogoTests(unittest.TestCase):
    def setUp(self):
        from unittest import mock
        import server
        self.server, self.mock, self.sent = server, mock, []
        self.patches = [mock.patch.object(server, "ksend", side_effect=lambda *a, **k: self.sent.append((a, k))),
                        mock.patch.object(server.ICONS, "persist", side_effect=lambda src, d: "/api/icons/abc.png")]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()

    def annotations(self, i=0):
        return self.sent[i][0][2]["metadata"]["annotations"]

    def test_a_logo_is_cached_and_only_the_deployments_annotations_change(self):
        result = self.server.set_workload_logos({"items": [{"ns": "lab", "name": "sonarr", "icon": "https://example.com/s.png"}]})
        (method, path, body), kwargs = self.sent[0]
        self.assertEqual(("PATCH", "/apis/apps/v1/namespaces/lab/deployments/sonarr"), (method, path))
        self.assertEqual(["metadata"], list(body), "the pod template is untouched, so nothing restarts")
        self.assertEqual({"homestead.io/icon": "/api/icons/abc.png", "homestead.io/icon-source": "https://example.com/s.png",
                          "homestead.io/logo-skipped": None}, self.annotations())
        self.assertEqual("application/merge-patch+json", kwargs["ctype"])
        self.assertEqual("1 logo saved", result["detail"])

    def test_blank_removes_and_skip_marks(self):
        self.server.set_workload_logos({"items": [{"ns": "lab", "name": "a", "icon": ""}, {"ns": "lab", "name": "b", "skip": True}]})
        self.assertIsNone(self.annotations(0)["homestead.io/icon"])
        self.assertEqual({"homestead.io/logo-skipped": "true"}, self.annotations(1))

    def test_inputs_are_checked_before_anything_is_written(self):
        for items in ([], [{"ns": "lab", "name": "../x", "icon": ""}],
                      [{"ns": "lab", "name": "a", "icon": "javascript:alert(1)"}],
                      [{"ns": "lab", "name": "a", "icon": "file:///etc/passwd"}]):
            with self.subTest(items=items), self.assertRaises(ValueError):
                self.server.set_workload_logos({"items": items})
        self.assertEqual([], self.sent)

    def test_one_failed_fetch_does_not_stop_the_rest(self):
        with self.mock.patch.object(self.server.ICONS, "persist", side_effect=[ValueError("not an image"), "/api/icons/b.png"]):
            result = self.server.set_workload_logos({"items": [{"ns": "lab", "name": "a", "icon": "https://example.com/a"},
                                                               {"ns": "lab", "name": "b", "icon": "https://example.com/b"}]})
        self.assertFalse(result["ok"])
        self.assertEqual(1, result["done"])
        self.assertIn("a: not an image", result["detail"])
        self.assertTrue(result["detail"].startswith("1 logo saved;"), result["detail"])


if __name__ == "__main__":
    unittest.main()
