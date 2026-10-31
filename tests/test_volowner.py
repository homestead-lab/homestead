"""A new volume gets the owner its image gives the path, as Docker's would."""
import gzip
import http.server
import io
import json
import os
import subprocess
import sys
import tarfile
import tempfile
import threading
import unittest
import urllib.request
from pathlib import Path
from unittest import mock


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
import homestead_update_registry as REGISTRY
import homestead_volowner as VOLOWNER
import server
import homestead_deploy_manifests as MANIFESTS


def layer(*entries):
    """A gzipped tar layer: (name, uid, gid, mode, text or None for a folder)."""
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w") as tar:
        for name, uid, gid, mode, text in entries:
            info = tarfile.TarInfo(name)
            info.uid, info.gid, info.mode = uid, gid, mode
            if text is None:
                info.type = tarfile.DIRTYPE
                tar.addfile(info)
            else:
                data = text.encode()
                info.size = len(data)
                tar.addfile(info, io.BytesIO(data))
    blob = gzip.compress(raw.getvalue())
    return lambda: io.BytesIO(blob)


PASSWD = "root:x:0:0:root:/root:/bin/bash\nsambee:x:1000:1000::/home/sambee:/bin/sh\n"


class ScanTests(unittest.TestCase):
    def test_the_newest_layer_to_mention_a_path_decides_it(self):
        newest = layer(("app/static/index.html", 0, 0, 0o644, "hi"))
        middle = layer(("app/data/", 1000, 1000, 0o755, None))
        oldest = layer(("./etc/passwd", 0, 0, 0o644, PASSWD), ("app/data", 0, 0, 0o755, None))

        found, passwd, _ = VOLOWNER.scan_layers([newest, middle, oldest], ["/app/data/"])

        self.assertEqual({"/app/data": (1000, 1000, 0o755)}, found)
        self.assertIn("sambee", passwd)

    def test_a_named_user_is_resolved_through_the_images_passwd(self):
        self.assertEqual((1000, 1000), VOLOWNER.parse_user("sambee", PASSWD))
        self.assertEqual((1000, 5), VOLOWNER.parse_user("1000:5"))
        self.assertEqual((1000, 1000), VOLOWNER.parse_user("1000"))
        self.assertEqual((1000, 44), VOLOWNER.parse_user("sambee:video", PASSWD, "video:x:44:\n"))
        self.assertIsNone(VOLOWNER.parse_user("nobody-here", PASSWD))
        self.assertTrue(VOLOWNER.is_root("root:root") and VOLOWNER.is_root("") and VOLOWNER.is_root("0"))

    def test_a_path_the_image_lacks_belongs_to_the_user_it_runs_as(self):
        layers = [layer(("etc/passwd", 0, 0, 0o644, PASSWD)), layer(("app/data", 1000, 1000, 0o750, None))]
        manifest = {"layers": [{"digest": "sha256:b", "mediaType": "tar+gzip"}, {"digest": "sha256:a"}]}
        config = {"config": {"User": "sambee"}}
        with mock.patch.object(VOLOWNER, "_registry", return_value=({}, manifest, config)), \
                mock.patch.object(VOLOWNER, "_blob", side_effect=lambda _p, digest: layers[digest == "sha256:b"]):
            owners = VOLOWNER.image_owners("sambee:test", ["/app/data", "/srv/extra"])

        self.assertEqual({"/app/data": (1000, 1000, 0o750), "/srv/extra": (1000, 1000, 0o755)}, owners)

    def test_an_image_that_runs_as_root_or_cannot_be_read_needs_nothing(self):
        with mock.patch.object(VOLOWNER, "_registry", return_value=({}, {"layers": []}, {"config": {}})):
            self.assertEqual({}, VOLOWNER.image_owners("root:test", ["/data"]))
        with mock.patch.object(VOLOWNER, "_registry", side_effect=OSError("offline")):
            self.assertEqual({}, VOLOWNER.image_owners("offline:test", ["/data"]))


class PrepareTests(unittest.TestCase):
    def cfg(self, **extra):
        return {"name": "sambee", "image": "ghcr.io/helgeklein/sambee:stable",
                "volumes": [{"type": "pvc", "source": "sambee-data", "path": "/app/data", "create": True},
                            {"type": "pvc", "source": "media", "path": "/media"}], **extra}

    def test_only_new_volumes_are_looked_up(self):
        asked = []
        cfg = VOLOWNER.prepare(self.cfg(), lambda image, paths: asked.append(paths) or {"/app/data": (1000, 1000, 0o755)})

        self.assertEqual([["/app/data"]], asked)
        self.assertEqual({"/app/data": [1000, 1000, 0o755]}, cfg["volume_owners"])

    def test_the_deploys_own_user_wins_and_root_or_puid_need_nothing(self):
        never = lambda *_: self.fail("the image should not be read")
        self.assertEqual({"/app/data": [568, 568, 0o755]},
                         VOLOWNER.prepare(self.cfg(run_as_user=568), never)["volume_owners"])
        self.assertEqual({"/app/data": [568, 100, 0o755]},
                         VOLOWNER.prepare(self.cfg(run_as_user=568, fs_group=100), never)["volume_owners"])
        self.assertNotIn("volume_owners", VOLOWNER.prepare(self.cfg(run_as_user=0), never))
        self.assertNotIn("volume_owners", VOLOWNER.prepare(self.cfg(env={"PUID": "99"}), never))


class DeploymentTests(unittest.TestCase):
    def test_an_init_container_owns_each_new_volume_at_its_folder(self):
        cfg = {"name": "sambee", "image": "ghcr.io/helgeklein/sambee:stable", "namespace": "lab",
               "volumes": [{"type": "pvc", "source": "appdata", "path": "/app/data", "create": True,
                            "sub_path": "sambee"},
                           {"type": "pvc", "source": "old", "path": "/old"}],
               "volume_owners": {"/app/data": [1000, 1000, 0o755]}}
        with mock.patch.object(server.HW, "features", return_value=[]):
            dep, _ = MANIFESTS.build_deployment(cfg)

        spec = dep["spec"]["template"]["spec"]
        init = spec["initContainers"][0]
        self.assertEqual(VOLOWNER.INIT, init["name"])
        self.assertEqual([{"name": "vol0", "mountPath": "/v/0"}], init["volumeMounts"])
        self.assertIn("d='/v/0/sambee'", init["command"][2])
        self.assertIn("chown 1000:1000", init["command"][2])
        self.assertEqual({"runAsUser": 0}, init["securityContext"])

    def test_no_owners_no_init_container(self):
        cfg = {"name": "web", "image": "nginx", "namespace": "lab",
               "volumes": [{"type": "pvc", "source": "web", "path": "/data", "create": True}]}
        with mock.patch.object(server.HW, "features", return_value=[]):
            dep, _ = MANIFESTS.build_deployment(cfg)
        self.assertNotIn("initContainers", dep["spec"]["template"]["spec"])


@unittest.skipUnless(hasattr(os, "geteuid") and os.geteuid() == 0, "chown needs root")
class ScriptTests(unittest.TestCase):
    def run_script(self, root, sub_path=""):
        script = VOLOWNER.init_container([("vol0", sub_path, 1000, 1000, 0o750)])["command"][2]
        subprocess.run(["sh", "-c", script.replace("/v/0", root)], check=True, capture_output=True)

    def test_an_empty_volume_is_given_its_owner(self):
        with tempfile.TemporaryDirectory() as root:
            os.mkdir(os.path.join(root, "lost+found"))
            self.run_script(root)
            info = os.stat(root)
            self.assertEqual((1000, 1000, 0o750), (info.st_uid, info.st_gid, info.st_mode & 0o7777))

    def test_a_folder_is_made_and_data_already_there_is_left_alone(self):
        with tempfile.TemporaryDirectory() as root:
            self.run_script(root, "app")
            self.assertEqual(1000, os.stat(os.path.join(root, "app")).st_uid)
            self.assertEqual(0, os.stat(root).st_uid)
        with tempfile.TemporaryDirectory() as root:
            Path(root, "db.sqlite").write_text("data")
            self.run_script(root)
            self.assertEqual(0, os.stat(root).st_uid)


def fake_registry(test, blobs, manifests=None):
    """A registry that wants a bearer token and sends blobs on to storage on
    another host, as Docker Hub and GHCR do. Storage, like their CDNs, refuses
    a request whose Host names the registry (421). Returns (registry port, seen)."""
    seen = {"storage": []}

    class Storage(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            seen["storage"].append({"host": self.headers.get("Host"),
                                    "authorization": self.headers.get("Authorization")})
            if self.headers.get("Host") != f"127.0.0.1:{self.server.server_port}":
                self.send_response(421)
                self.end_headers()
                return
            body = blobs.get(self.path.lstrip("/"))
            self.send_response(200 if body is not None else 404)
            self.end_headers()
            self.wfile.write(body or b"")

        def log_message(self, *_):
            pass

    storage = http.server.HTTPServer(("127.0.0.1", 0), Storage)

    class Registry(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path.startswith("/token"):
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b'{"token": "secret"}')
            elif self.headers.get("Authorization") != "Bearer secret":
                self.send_response(401)
                self.send_header("WWW-Authenticate",
                                 f'Bearer realm="http://127.0.0.1:{self.server.server_port}/token"')
                self.end_headers()
            elif "/manifests/" in self.path:
                body = (manifests or {}).get(self.path.rsplit("/", 1)[-1])
                self.send_response(200 if body is not None else 404)
                self.end_headers()
                self.wfile.write(body or b"")
            else:
                self.send_response(307)
                self.send_header("Location", f"http://127.0.0.1:{storage.server_port}/{self.path.rsplit('/', 1)[-1]}")
                self.end_headers()

        def log_message(self, *_):
            pass

    registry = http.server.HTTPServer(("127.0.0.1", 0), Registry)
    for srv in (storage, registry):
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        test.addCleanup(srv.server_close)
        test.addCleanup(srv.shutdown)
    # The transport fixture is HTTP loopback; realm trust/TLS policy has
    # separate tests. These checks concern what the retry carries on a redirect.
    for patch in (mock.patch.dict(os.environ, {"NO_PROXY": "127.0.0.1", "no_proxy": "127.0.0.1"}),
                  mock.patch.object(REGISTRY, "_auth_realm")):
        patch.start()
        test.addCleanup(patch.stop)
    return registry.server_port, seen


class RedirectTests(unittest.TestCase):
    def test_the_registry_token_is_not_sent_on_to_blob_storage(self):
        port, seen = fake_registry(self, {"sha256:a": b"blob"})
        url = f"http://127.0.0.1:{port}/v2/x/blobs/sha256:a"
        with REGISTRY._open(urllib.request.Request(url)) as response:
            self.assertEqual(b"blob", response.read())
        self.assertIsNone(seen["storage"][-1]["authorization"])

    def test_the_retry_does_not_carry_the_registrys_host_to_blob_storage(self):
        # Docker Hub's CDN answers 403 and GHCR's 421 when it does (wrap-up row 91).
        port, seen = fake_registry(self, {"sha256:a": b"blob"})
        url = f"http://127.0.0.1:{port}/v2/x/blobs/sha256:a"
        request = urllib.request.Request(url, headers={"User-Agent": "Homestead/2.0", "Accept": "application/json"})
        with REGISTRY._open(request) as response:
            self.assertEqual(b"blob", response.read())
        self.assertEqual(1, len(seen["storage"]))
        self.assertNotEqual(f"127.0.0.1:{port}", seen["storage"][0]["host"])

    def test_image_owners_reads_the_config_and_layers_behind_a_token(self):
        config = json.dumps({"config": {"User": "sambee"}}).encode()
        layers = {"sha256:l1": layer(("etc/passwd", 0, 0, 0o644, PASSWD))().read(),
                  "sha256:l2": layer(("app/data", 1000, 1000, 0o750, None))().read()}
        manifest = json.dumps({"config": {"digest": "sha256:cfg"},
                               "layers": [{"digest": "sha256:l1"}, {"digest": "sha256:l2"}]}).encode()
        port, seen = fake_registry(self, {"sha256:cfg": config, **layers}, {"retry": manifest})
        real_open = REGISTRY._open

        def plain(req, *args, **kwargs):
            # The registry is always asked over HTTPS; the fixture speaks HTTP.
            req.full_url = req.full_url.replace("https://", "http://", 1)
            return real_open(req, *args, **kwargs)

        with mock.patch.object(REGISTRY, "_open", side_effect=plain):
            owners = VOLOWNER.image_owners(f"127.0.0.1:{port}/sambee:retry", ["/app/data", "/srv/extra"])

        self.assertEqual({"/app/data": (1000, 1000, 0o750), "/srv/extra": (1000, 1000, 0o755)}, owners)
        self.assertEqual(3, len(seen["storage"]))


if __name__ == "__main__":
    unittest.main()
