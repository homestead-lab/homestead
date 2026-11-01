"""Every reader of homestead_fetch, over a real socket and TLS (#1077).

The capped reader's own tests hand it fakes with no socket, and the update
checks' tests replace the registry altogether, so 2.8.333 shipped a reader
that failed every image check, the App Store and linked clusters with
"Bad file descriptor": http.client closes an answer's socket once its body is
in, and the reader set a timeout on it after that. Here each module that reads
through homestead_fetch talks to a local HTTPS server through urllib, as it
does on the cluster, and a new module that reads through it must be added.
"""
import http.server
import json
import os
import re
import shutil
import ssl
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

SERVER = Path(os.environ.get("HOMESTEAD_TEST_SERVER_DIR", str(Path(__file__).resolve().parents[1] / "server")))
sys.path.insert(0, str(SERVER))
import homestead_deploy_appstore as APPSTORE
import homestead_fleet as FLEET
import homestead_helm as HELM
import homestead_update_checks as CHECKS
import homestead_update_registry as REGISTRY
import homestead_updates as UPDATES

# What each reader is tested with below. A module that starts reading through
# homestead_fetch belongs here, with a test that reads a real answer.
COVERED = {
    "homestead_update_registry": "RegistryTests",
    "homestead_helm": "OtherReaderTests.test_artifact_hub",
    "homestead_deploy_appstore": "OtherReaderTests.test_app_feed",
    "homestead_fleet": "OtherReaderTests.test_linked_cluster",
}

TOKEN = "t0ken"
OLD, NEW, CHILD = ("sha256:" + c * 64 for c in "abc")
# Past one CHUNK of homestead_fetch, so the body arrives in several reads.
PADDING = "x" * (200 << 10)


def _certificate(folder):
    """A throwaway certificate for 127.0.0.1: from HOMESTEAD_TEST_TLS_DIR when
    set (the built image has no openssl), else made with openssl."""
    given = os.environ.get("HOMESTEAD_TEST_TLS_DIR")
    if given:
        return os.path.join(given, "cert.pem"), os.path.join(given, "key.pem")
    if not shutil.which("openssl"):
        if os.environ.get("CI"):
            raise RuntimeError("openssl or HOMESTEAD_TEST_TLS_DIR is needed: these reads are not to be skipped in CI")
        raise unittest.SkipTest("openssl is needed to make a throwaway certificate")
    key, cert = os.path.join(folder, "key.pem"), os.path.join(folder, "cert.pem")
    subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", key, "-out", cert,
                    "-days", "1", "-subj", "/CN=127.0.0.1", "-addext", "subjectAltName=IP:127.0.0.1"],
                   check=True, capture_output=True)
    return cert, key


class Handler(http.server.BaseHTTPRequestHandler):
    """A registry with a token server, Artifact Hub, an app feed and a linked
    cluster: as they answer, with a length, keep-alive, or chunked."""
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def answer(self, body, headers=(), status=200, chunked=False):
        body = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        for name, value in headers:
            self.send_header(name, value)
        if chunked:
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            for start in range(0, len(body), 50000):
                piece = body[start:start + 50000]
                self.wfile.write(b"%x\r\n%s\r\n" % (len(piece), piece))
            self.wfile.write(b"0\r\n\r\n")
        else:
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    def do_GET(self):
        host = "%s:%d" % self.server.server_address
        path = self.path
        if path.startswith("/token"):
            return self.answer({"token": TOKEN})
        if path.startswith("/v2/"):
            if self.headers.get("Authorization") != "Bearer " + TOKEN:
                return self.answer({"errors": [{"code": "UNAUTHORIZED"}]}, status=401, headers=[
                    ("WWW-Authenticate", f'Bearer realm="https://{host}/token",service="{host}",scope="repository:lab/app:pull"')])
            if path == "/v2/lab/app/tags/list?n=1000":
                return self.answer({"name": "lab/app", "tags": ["1.1.0", "1.2.0", "latest"]}, chunked=True)
            if path == "/v2/lab/app/manifests/1.2.0":
                return self.answer({"schemaVersion": 2, "mediaType": "application/vnd.oci.image.index.v1+json",
                                    "manifests": [{"digest": CHILD, "platform": {"os": "linux", "architecture": "amd64"}}],
                                    "annotations": {"padding": PADDING}}, headers=[("Docker-Content-Digest", NEW)])
            if path == "/v2/lab/app/manifests/" + CHILD:
                return self.answer({"schemaVersion": 2, "layers": [{"digest": "sha256:" + "d" * 64, "size": 1234}]})
        if path == "/hub/packages":
            return self.answer({"packages": [{"name": "demo"}], "padding": PADDING})
        if path == "/feed.json":
            return self.answer({"applist": [{"Name": "Demo", "Repository": "lab/demo:latest", "Overview": PADDING}]},
                               chunked=True)
        if path == "/api/fleet/hello":
            return self.answer({"ok": True, "padding": PADDING})
        self.answer({"error": "not found"}, status=404)


class Served(unittest.TestCase):
    """A local HTTPS server that urllib's default contexts trust."""

    @classmethod
    def setUpClass(cls):
        cls.folder = tempfile.TemporaryDirectory()
        cert, key = _certificate(cls.folder.name)
        cls.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(cert, key)
        cls.server.socket = context.wrap_socket(cls.server.socket, server_side=True)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.host = "%s:%d" % cls.server.server_address
        # Trusted as a public CA would be: the readers' own TLS setup is kept.
        made = ssl.create_default_context
        trusting = lambda *args, **kwargs: made(cafile=cert)
        cls.patches = [mock.patch.object(ssl, "create_default_context", trusting),
                       mock.patch.object(ssl, "_create_default_https_context", trusting)]
        for patch in cls.patches:
            patch.start()

    @classmethod
    def tearDownClass(cls):
        for patch in cls.patches:
            patch.stop()
        cls.server.shutdown()
        cls.server.server_close()
        cls.folder.cleanup()

    def setUp(self):
        REGISTRY._CACHE.clear()


class RegistryTests(Served):
    def test_tags_are_read_through_the_token_exchange(self):
        self.assertEqual(["1.1.0", "1.2.0", "latest"], REGISTRY.registry_tags(f"{self.host}/lab/app:1.1.0", force=True))

    def test_a_manifest_of_many_pieces_is_read_whole(self):
        info = REGISTRY.manifest_info(f"{self.host}/lab/app:1.2.0", force=True)
        self.assertEqual({"digest": NEW, "children": [CHILD]}, info)

    def test_an_index_leads_to_its_layers(self):
        self.assertEqual([{"digest": "sha256:" + "d" * 64, "size": 1234}],
                         REGISTRY.image_layers(f"{self.host}/lab/app:1.2.0"))

    def test_an_update_check_finds_the_newer_release(self):
        """The check the Updates dialog shows, with nothing but the cluster replaced."""
        image = f"{self.host}/lab/app:1.1.0"
        dep = {"metadata": {"namespace": "lab", "name": "app", "annotations": {}},
               "spec": {"replicas": 1, "selector": {"matchLabels": {"app": "app"}},
                        "template": {"spec": {"containers": [{"name": "app", "image": image}]}}}}
        pod = {"metadata": {"namespace": "lab", "labels": {"app": "app"}},
               "status": {"phase": "Running",
                          "containerStatuses": [{"name": "app", "imageID": f"{self.host}/lab/app@{OLD}"}]}}
        with tempfile.TemporaryDirectory() as data:
            UPDATES.bind(lambda path: {"imagePullSecrets": []}, lambda *a, **k: {}, "lab", data)
            report = CHECKS._check_deployment(dep, [pod], force=True, persist=False)
        image_report = report["images"][0]
        self.assertEqual("", image_report["error"])
        self.assertTrue(image_report["available"])
        self.assertEqual("1.2.0", image_report["candidate_tag"])
        self.assertEqual(NEW, image_report["remote_digest"])


class OtherReaderTests(Served):
    def test_artifact_hub(self):
        self.assertEqual([{"name": "demo"}], HELM._hub_get(f"https://{self.host}/hub/packages")["packages"])

    def test_app_feed(self):
        apps = APPSTORE.load_feed_now(f"https://{self.host}/feed.json")
        self.assertEqual(["Demo"], [app.get("name") or app.get("Name") for app in apps])

    def test_linked_cluster(self):
        answer, _ = FLEET._json(f"https://{self.host}", "GET", "/api/fleet/hello")
        self.assertTrue(answer["ok"])


class CoverageTests(unittest.TestCase):
    def test_every_reader_of_the_capped_fetch_is_tested_here(self):
        readers = {path.stem for path in SERVER.glob("homestead_*.py")
                   if re.search(r"^import homestead_fetch\b", path.read_text(encoding="utf-8"), re.M)}
        self.assertEqual(set(COVERED), readers,
                         "a module reading through homestead_fetch needs a test here that reads a real answer")


if __name__ == "__main__":
    unittest.main()
