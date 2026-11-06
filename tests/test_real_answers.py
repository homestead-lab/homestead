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
import time
import unittest
import urllib.request
from pathlib import Path
from unittest import mock

SERVER = Path(os.environ.get("HOMESTEAD_TEST_SERVER_DIR", str(Path(__file__).resolve().parents[1] / "server")))
sys.path.insert(0, str(SERVER))
import homestead_addons as ADDONS
import homestead_cfaccess as CFACCESS
import homestead_components as COMPONENTS
import homestead_deploy_appstore as APPSTORE
import homestead_fetch as FETCH
import homestead_fleet as FLEET
import homestead_helm as HELM
import homestead_ipam as IPAM
import homestead_move as MOVE
import homestead_upgrades as UPGRADES
import homestead_vmstore as VMSTORE
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
    "homestead_cfaccess": "OutboundFetchTests.test_cloudflare_access_keys",
    "homestead_addons": "OutboundFetchTests.test_addon_downloads",
    "homestead_components": "OutboundFetchTests.test_platform_release_lists",
    "homestead_upgrades": "OutboundFetchTests.test_harvester_releases",
    "homestead_vmstore": "OutboundFetchTests.test_vm_store_addresses",
    "homestead_ipam": "OutboundFetchTests.test_unifi_console",
    "homestead_move": "OutboundFetchTests.test_another_homestead",
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

    def trickle(self):
        """An answer that comes a byte at a time, slower than any deadline here."""
        self.send_response(200)
        self.send_header("Content-Length", "400")
        self.end_headers()
        try:
            for _ in range(400):
                self.wfile.write(b"[")
                self.wfile.flush()
                time.sleep(0.05)
        except OSError:
            pass

    def outbound(self, path):
        """The public services and consoles Homestead asks (OutboundFetchTests)."""
        if "/slow" in path:
            self.close_connection = True
            self.trickle()
            return True
        if "/huge" in path:
            self.answer(b"[" + b" " * (300 << 10) + b"]")
            return True
        route, _, query = path.partition("?")
        if route == "/moved":
            self.send_response(302)
            self.send_header("Location", query)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return True
        if route == "/echo-headers":
            self.answer({name.lower(): value for name, value in self.headers.items()})
            return True
        if route == "/cf/cdn-cgi/access/certs":
            self.answer({"keys": [{"kid": "k1", "kty": "RSA", "n": "AQAB", "e": "AQAB"}]})
            return True
        if route == "/gh/releases":
            self.answer([{"tag_name": "v1.2.3", "published_at": "2026-01-01T00:00:00Z", "padding": PADDING},
                         {"tag_name": "v1.2.4-rc1", "prerelease": True}], chunked=True)
            return True
        if route == "/releases/latest":
            self.send_response(302)
            self.send_header("Location", "/releases/tag/v1.2.3")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return True
        if route == "/releases/tag/v1.2.3":
            self.answer(b"release page")
            return True
        if route == "/releases.json":
            self.answer([{"variant": "Cloud", "subvariant": "Cloud_Base", "version": "43", "arch": "x86_64",
                          "link": "https://example.com/Fedora-Cloud-Base-Generic-43.x86_64.qcow2"}])
            return True
        if route == "/alpine/":
            self.answer(b'<a href="alpine-3.22.1-x86_64-cloudinit-r0.qcow2">x</a>' + PADDING.encode())
            return True
        if route.startswith("/unifi/"):
            if self.headers.get("X-API-KEY") != UNIFI_KEY:
                self.answer({"error": "no key"}, status=401)
            else:
                self.answer({"data": [{"id": "s1", "name": "Default"}], "padding": PADDING})
            return True
        if route == "/api/fleet/hello":
            return False
        if route.startswith("/api/"):
            self.answer({"ok": True, "padding": PADDING}, chunked=True)
            return True
        return False

    def do_POST(self):
        self.rfile.read(int(self.headers.get("Content-Length") or 0))
        if self.path == "/api/auth/login":
            return self.answer({"ok": True}, headers=[("Set-Cookie", "homestead_session=s3ss; HttpOnly")])
        self.answer({"error": "not found"}, status=404)

    def do_GET(self):
        host = "%s:%d" % self.server.server_address
        path = self.path
        if self.outbound(path):
            return
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
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(cert, key)
        # A second server is another origin (another port), for redirects.
        cls.server, cls.elsewhere = (http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler) for _ in range(2))
        for server in (cls.server, cls.elsewhere):
            server.daemon_threads = True
            server.socket = context.wrap_socket(server.socket, server_side=True)
            threading.Thread(target=server.serve_forever, daemon=True).start()
        cls.host = "%s:%d" % cls.server.server_address
        cls.other = "%s:%d" % cls.elsewhere.server_address
        # Trusted as a public CA would be: the readers' own TLS setup is kept.
        made = ssl.create_default_context
        trusting = lambda *args, **kwargs: made(cafile=cert)
        # urllib keeps one opener, with the TLS context of the first urlopen in
        # this process: dropped, so the readers that pass none get this one.
        cls.patches = [mock.patch.object(ssl, "create_default_context", trusting),
                       mock.patch.object(ssl, "_create_default_https_context", trusting),
                       mock.patch.object(urllib.request, "_opener", None)]
        for patch in cls.patches:
            patch.start()

    @classmethod
    def tearDownClass(cls):
        for patch in cls.patches:
            patch.stop()
        for server in (cls.server, cls.elsewhere):
            server.shutdown()
            server.server_close()
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


UNIFI_KEY = "un" + "ifi-" + "key-" + "0123"


class OutboundFetchTests(Served):
    """Each caller of FETCH.get, over TLS: a normal answer, and one that trickles
    or is too large ends in the caller's own error path, soon."""

    def quick(self, module, name, value=0.5):
        patch = mock.patch.object(module, name, value)
        patch.start()
        self.addCleanup(patch.stop)

    def ends_soon(self, call, error=(TimeoutError, FETCH.TooLarge)):
        started = time.monotonic()
        with self.assertRaises(error) as caught:
            call()
        self.assertLess(time.monotonic() - started, 4, "the fetch ends at its deadline, not the far end's pace")
        return caught.exception

    def test_cloudflare_access_keys(self):
        self.addCleanup(CFACCESS.configure, CFACCESS.TEAM, ",".join(CFACCESS.AUDIENCES))
        CFACCESS.configure(f"https://{self.host}/cf", "aud")
        self.assertEqual({"k1": (65537, 65537)}, CFACCESS._fetch_keys())
        self.quick(CFACCESS, "KEYS_DEADLINE")
        CFACCESS.configure(f"https://{self.host}/slow", "aud")
        self.assertIn("Cloudflare Access's signing keys took too long", str(self.ends_soon(CFACCESS._fetch_keys)))
        CFACCESS.configure(f"https://{self.host}/huge", "aud")
        self.ends_soon(CFACCESS._fetch_keys, FETCH.TooLarge)

    def test_cloudflare_access_keys_a_failed_refresh_keeps_the_keys_there_were(self):
        self.addCleanup(CFACCESS.configure, CFACCESS.TEAM, ",".join(CFACCESS.AUDIENCES))
        saved = dict(CFACCESS._certs)
        self.addCleanup(lambda: (CFACCESS._certs.clear(), CFACCESS._certs.update(saved)))
        CFACCESS._certs.update(keys={"old": (3, 5)}, at=1.0)
        self.quick(CFACCESS, "KEYS_DEADLINE")
        CFACCESS.configure(f"https://{self.host}/slow", "aud")
        running = threading.Event()
        CFACCESS._fetching = running
        CFACCESS._refresh(2.0, running)
        self.assertTrue(running.is_set())
        self.assertIsNone(CFACCESS._fetching)
        self.assertEqual({"old": (3, 5)}, CFACCESS._certs["keys"])

    def test_addon_downloads(self):
        text, where = ADDONS._fetch(f"https://{self.host}/releases/latest")
        self.assertEqual(("release page", f"https://{self.host}/releases/tag/v1.2.3"), (text, where))
        self.quick(ADDONS, "FETCH_DEADLINE")
        self.ends_soon(lambda: ADDONS._fetch(f"https://{self.host}/slow"), TimeoutError)
        self.quick(ADDONS, "FETCH_MAX_BYTES", 1000)
        self.ends_soon(lambda: ADDONS._fetch(f"https://{self.host}/huge"), FETCH.TooLarge)

    def test_platform_release_lists(self):
        self.quick(COMPONENTS, "fetch_json", COMPONENTS._get_json)
        self.quick(COMPONENTS, "REPOS", {"longhorn": "x"})
        self.addCleanup(COMPONENTS._releases.pop, "longhorn", None)
        self.quick(COMPONENTS, "GITHUB", f"https://{self.host}/gh/releases?{{}}")
        self.assertEqual((["v1.2.3"], ""), COMPONENTS.releases("longhorn", force=True))
        self.quick(COMPONENTS, "RELEASES_DEADLINE")
        self.quick(COMPONENTS, "GITHUB", f"https://{self.host}/slow?{{}}")
        started = time.monotonic()
        value, error = COMPONENTS.releases("longhorn", force=True)
        self.assertLess(time.monotonic() - started, 4)
        self.assertEqual((["v1.2.3"], True), (value, "took too long" in error), "the versions known are kept")

    def test_harvester_releases(self):
        self.quick(UPGRADES, "fetch", UPGRADES._github)
        self.quick(UPGRADES, "_releases", {"at": 0.0, "value": [], "error": ""})
        self.quick(UPGRADES, "RELEASES_URL", f"https://{self.host}/gh/releases")
        rows, error = UPGRADES.releases(force=True)
        self.assertEqual((["v1.2.3", "v1.2.4-rc1"], ""), (sorted(r["tag"] for r in rows), error))
        self.quick(UPGRADES, "RELEASES_DEADLINE")
        self.quick(UPGRADES, "RELEASES_URL", f"https://{self.host}/slow")
        started = time.monotonic()
        rows, error = UPGRADES.releases(force=True)
        self.assertLess(time.monotonic() - started, 4, "the alerts loop is not held up")
        self.assertIn("took too long", error)
        self.assertEqual(2, len(rows), "the releases known are kept")

    def test_vm_store_addresses(self):
        self.quick(VMSTORE, "fetch_json", VMSTORE._get_json)
        self.quick(VMSTORE, "fetch_text", VMSTORE._get_text)
        self.quick(VMSTORE, "_fedora", {"at": 0.0, "value": {}})
        self.quick(VMSTORE, "_alpine", {"at": 0.0, "value": {}})
        self.quick(VMSTORE, "FEDORA", f"https://{self.host}/releases.json")
        self.quick(VMSTORE, "ALPINE", f"https://{self.host}/alpine/")
        self.assertTrue(VMSTORE.url_for({"resolve": "fedora"}, "amd64").endswith("43.x86_64.qcow2"))
        self.assertTrue(VMSTORE.url_for({"resolve": "alpine"}, "amd64").endswith("cloudinit-r0.qcow2"))
        self.quick(VMSTORE, "LIST_DEADLINE")
        for name in ("FEDORA", "ALPINE"):
            self.quick(VMSTORE, name, f"https://{self.host}/slow")
        self.quick(VMSTORE, "_fedora", {"at": 0.0, "value": {}})
        self.quick(VMSTORE, "_alpine", {"at": 0.0, "value": {}})
        started = time.monotonic()
        self.assertEqual(("", ""), (VMSTORE.url_for({"resolve": "fedora"}, "amd64"),
                                    VMSTORE.url_for({"resolve": "alpine"}, "amd64")))
        self.assertLess(time.monotonic() - started, 4)
        self.ends_soon(lambda: VMSTORE._get_text(f"https://{self.host}/slow"), TimeoutError)

    def test_unifi_console(self):
        sites = IPAM._unifi_get(f"https://{self.host}/unifi/sites", UNIFI_KEY, True)
        self.assertEqual("Default", sites["data"][0]["name"])
        self.quick(IPAM, "UNIFI_DEADLINE")
        self.ends_soon(lambda: IPAM._unifi_get(f"https://{self.host}/slow", UNIFI_KEY, True), TimeoutError)
        self.quick(IPAM, "UNIFI_MAX_BYTES", 1000)
        self.ends_soon(lambda: IPAM._unifi_get(f"https://{self.host}/huge", UNIFI_KEY, True), FETCH.TooLarge)

    def test_unifi_console_key_stays_on_its_host(self):
        seen = IPAM._unifi_get(f"https://{self.host}/moved?https://{self.other}/echo-headers", UNIFI_KEY, True)
        self.assertNotIn(UNIFI_KEY, json.dumps(seen))
        seen = IPAM._unifi_get(f"https://{self.host}/moved?https://{self.host}/echo-headers", UNIFI_KEY, True)
        self.assertEqual(UNIFI_KEY, seen.get("x-api-key"), "a redirect on the console itself keeps it")

    def test_another_homestead(self):
        row = {"url": f"https://{self.host}"}
        payload, _ = MOVE._call(row, "/api/move/inventory", token="t")
        self.assertTrue(payload["ok"])
        _, answer = MOVE._call(row, "/api/auth/login", body={"username": "u", "password": "p"})
        self.assertIn("homestead_session=s3ss", answer.headers.get("Set-Cookie", ""))
        self.quick(MOVE, "ANSWER_DEADLINE")
        self.ends_soon(lambda: MOVE._call({"url": f"https://{self.host}/slow"}, "/api/x"), TimeoutError)
        self.quick(MOVE, "ANSWER_MAX_BYTES", 1000)
        self.ends_soon(lambda: MOVE._call({"url": f"https://{self.host}/huge"}, "/api/x"), FETCH.TooLarge)

    def test_another_homestead_icon(self):
        icon = "/api/icons/" + "a" * 64 + ".png"
        self.quick(MOVE, "_cluster", lambda name: {"url": f"https://{self.host}{name}"})
        self.assertTrue(MOVE.icon_bytes("", icon).startswith(b"{"))
        self.quick(MOVE, "ICON_DEADLINE")
        started = time.monotonic()
        with self.assertRaises(TimeoutError):
            MOVE.icon_bytes("/slow", icon)
        self.assertLess(time.monotonic() - started, 4, "a trickling icon is cut at the deadline")
        with self.assertRaisesRegex(ValueError, "icon is too large"):
            MOVE.icon_bytes("/huge", icon)

    def test_another_homestead_session_stays_on_its_host(self):
        session = "s" + "ess-" + "789"
        seen, _ = MOVE._call({"url": f"https://{self.host}"}, f"/moved?https://{self.other}/echo-headers",
                             token=session)
        self.assertNotIn(session, json.dumps(seen))


class CoverageTests(unittest.TestCase):
    def test_every_reader_of_the_capped_fetch_is_tested_here(self):
        readers = {path.stem for path in SERVER.glob("homestead_*.py")
                   if re.search(r"^import homestead_fetch\b", path.read_text(encoding="utf-8"), re.M)}
        self.assertEqual(set(COVERED), readers,
                         "a module reading through homestead_fetch needs a test here that reads a real answer")


if __name__ == "__main__":
    unittest.main()
