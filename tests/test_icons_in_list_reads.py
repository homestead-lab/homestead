"""App logos leave the lists that are read again every 15 seconds (#1211).

/api/workloads carried each app's logo as a whole data: URI, so the Apps page
downloaded every logo again on each refresh: on a four-host cluster with 60
apps, 1,088 of the 1,181 KiB of each read. A row now carries the logo's
content-addressed /api/icons/<sha256>.<ext> reference, which the browser
fetches once and keeps; what it shows does not change."""
import hashlib
import json
import sys
import tempfile
import threading
import unittest
from http.client import HTTPConnection
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
import homestead_auth as AUTH
import homestead_fleet as FLEET
import homestead_http as HTTP
import homestead_icons as ICONS
import homestead_routes as ROUTER
import server
sys.path.insert(0, str(Path(__file__).resolve().parent))
from fakes.kube import Kube

NAMES = server.NAMES
WORKLOADS = server.WORKLOADS
DEPLOY_SERVER = sys.modules["homestead_deploy_server"]
REFERENCE = r"/api/icons/[0-9a-f]{64}\.(png|jpg|gif|webp|ico|svg)"


def png(seed, size):
    """A PNG-signed picture of size bytes, the same for the same seed."""
    body, block = b"", hashlib.sha256(str(seed).encode()).digest()
    while len(body) < size:
        block = hashlib.sha256(block).digest()
        body += block
    return b"\x89PNG\r\n\x1a\n" + body[:size - 8]


def deployment(name, annotations=None):
    return {"apiVersion": "apps/v1", "kind": "Deployment",
            "metadata": {"name": name, "namespace": "lab", "annotations": dict(annotations or {}), "labels": {}},
            "spec": {"replicas": 1, "selector": {"matchLabels": {"app": name}},
                     "template": {"metadata": {"labels": {"app": name}},
                                  "spec": {"containers": [{"name": name, "image": "example.com/app:1"}]}}},
            "status": {"readyReplicas": 1}}


class Served(unittest.TestCase):
    """The real HTTP server, a fake cluster, and a data folder of its own."""

    @classmethod
    def setUpClass(cls):
        cls.listener = HTTP.BoundedHTTPServer(("127.0.0.1", 0), server.H)
        threading.Thread(target=cls.listener.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.listener.shutdown()
        cls.listener.server_close()

    def setUp(self):
        self.data = self.enterContext(tempfile.TemporaryDirectory())
        for module in (server, server.NAMES_HELPERS, DEPLOY_SERVER):
            self.enterContext(mock.patch.object(module, "DATA_DIR", self.data))
        self.role = "viewer"
        self.enterContext(mock.patch.object(AUTH, "verify_token", side_effect=lambda token, force=False:
                                            {"user": "ada", "role": self.role, "remember": False,
                                             "started": 1, "expires": 2} if token == "tok" else None))
        # Each read builds its answer: nothing kept from another test, no leader snapshot.
        self.enterContext(mock.patch.object(ROUTER, "_cached", [lambda key, seconds, fn: fn()]))
        self.enterContext(mock.patch.object(ROUTER, "_snapshot", [lambda key: None]))
        server.NAMES_HELPERS._ICON_HEALED.clear()
        server.NAMES_HELPERS._ICON_TRIED.clear()
        self.addCleanup(server.NAMES_HELPERS._ICON_HEALED.clear)
        self.addCleanup(server.NAMES_HELPERS._ICON_TRIED.clear)

    def ask(self, path, session=True, headers=None):
        sent = {"Cookie": "homestead_session=tok"} if session else {}
        sent.update(headers or {})
        conn = HTTPConnection("127.0.0.1", self.listener.server_address[1], timeout=20)
        try:
            conn.request("GET", path, headers=sent)
            reply = conn.getresponse()
            headers = {}
            for name, value in reply.getheaders():        # a header sent twice: both values
                headers[name.lower()] = f"{headers[name.lower()]}, {value}" if name.lower() in headers else value
            return reply.status, reply.read(), headers
        finally:
            conn.close()

    def logo(self, picture):
        return ICONS.store(picture, self.data)


class ListReadTests(Served):
    def test_the_apps_list_carries_each_logo_as_a_short_reference_not_its_bytes(self):
        # The measured cluster's shape: 60 apps, 38 with a logo, 33 different
        # logos, about 1,088 KiB of them once written as data: URIs.
        pictures = [png(i, 21 * 1024) for i in range(33)]
        references = [self.logo(p) for p in pictures]
        apps = [deployment(f"app-{i:02}", {NAMES.key("icon"): references[i % 33]} if i < 38 else {})
                for i in range(60)]
        with Kube(*apps):
            status, body, _ = self.ask("/api/workloads")
        self.assertEqual(200, status)
        rows = {row["name"]: row for row in json.loads(body)}
        self.assertEqual(60, len(rows))
        for i in range(60):
            row = rows[f"app-{i:02}"]
            if i < 38:
                self.assertEqual(references[i % 33], row["icon"], f"app-{i:02} shows its own logo")
                self.assertTrue(row["has_logo"])
            else:
                self.assertEqual("", row["icon"])
        logos = sum(len(row["icon"]) for row in rows.values())
        print(f"\n/api/workloads, 60 apps, 38 logos: {len(body) / 1024:.0f} KiB, logos {logos / 1024:.1f} KiB",
              file=sys.stderr)
        self.assertLess(logos, 38 * 100, f"the logos take {logos / 1024:.0f} KiB of the list")
        self.assertLess(len(body), 128 * 1024, f"the list is {len(body) / 1024:.0f} KiB: logos are still inside it")

    def test_each_reference_answers_the_picture_the_row_used_to_carry(self):
        picture = png("frigate", 5000)
        reference = self.logo(picture)
        with Kube(deployment("frigate", {NAMES.key("icon"): reference})):
            row = json.loads(self.ask("/api/workloads")[1])[0]
        status, body, headers = self.ask(row["icon"])
        self.assertEqual((200, picture, "image/png"), (status, body, headers["content-type"]))

    def test_a_logo_whose_file_is_missing_is_no_logo_as_before(self):
        missing = "/api/icons/" + "e" * 64 + ".png"
        server.NAMES_HELPERS._ICON_TRIED[missing] = server.time.time()   # its repair already asked for
        with Kube(deployment("frigate", {NAMES.key("icon"): missing})):
            row = json.loads(self.ask("/api/workloads")[1])[0]
        self.assertEqual(("", True), (row["icon"], row["has_logo"]))

    def test_a_logo_address_that_is_not_cached_here_is_passed_on_as_it_is(self):
        with Kube(deployment("frigate", {NAMES.key("icon"): "https://example.com/logo.png"})):
            row = json.loads(self.ask("/api/workloads")[1])[0]
        self.assertEqual("https://example.com/logo.png", row["icon"])

    def test_the_list_is_still_refused_to_someone_signed_out(self):
        with Kube(deployment("frigate", {NAMES.key("icon"): self.logo(png("f", 100))})):
            status, body, _ = self.ask("/api/workloads", session=False)
        self.assertEqual(401, status)
        self.assertNotIn(b"/api/icons/", body)


class IconRouteTests(Served):
    """The route the references name is dev's own, unchanged; these pin what
    the lists now rely on."""

    def test_a_logo_is_kept_by_the_browser_and_never_asked_for_again(self):
        reference = self.logo(png("a", 300))
        status, _, headers = self.ask(reference)
        self.assertEqual(200, status)
        self.assertEqual("private, max-age=31536000, immutable", headers["cache-control"])

    def test_an_unknown_logo_is_refused_and_the_refusal_is_not_kept(self):
        for path in ("/api/icons/" + "0" * 64 + ".png", "/api/icons/" + "0" * 64 + ".exe",
                     "/api/icons/../server.py", "/api/icons/notahash.png"):
            status, _, headers = self.ask(path)
            self.assertEqual(404, status, path)
            self.assertEqual("no-store", headers.get("cache-control"), path)

    def test_a_logo_answers_without_signing_in_as_on_dev(self):
        reference = self.logo(png("b", 300))
        self.assertEqual(200, self.ask(reference, session=False)[0])

    def test_an_svg_logo_cannot_run_anything_when_opened_on_its_own(self):
        reference = self.logo(b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script>'
                              b'<rect width="4" height="4"/></svg>')
        status, body, headers = self.ask(reference)
        self.assertEqual((200, "image/svg+xml"), (status, headers["content-type"]))
        self.assertNotIn(b"script", body)
        self.assertIn("sandbox", headers["content-security-policy"])

    def test_a_cluster_named_on_this_homestead_itself_serves_the_logo_here(self):
        reference = self.logo(png("c", 300))
        with mock.patch.object(FLEET, "self_id", return_value="me"):
            status, _, _ = self.ask(reference + "?hs_cluster=me")
        self.assertEqual(200, status)


class EditTests(Served):
    def test_the_edit_form_still_gets_the_logo_address_that_was_given(self):
        reference = self.logo(png("d", 300))
        annotations = {NAMES.key("icon"): reference, NAMES.key("icon-source"): "https://example.com/d.png"}
        with Kube(deployment("frigate", annotations)):
            status, body, _ = self.ask("/api/workload?ns=lab&name=frigate")
        self.assertEqual(200, status, body)
        self.assertEqual("https://example.com/d.png", json.loads(body)["icon"])

    def test_the_logo_a_list_row_carries_saved_back_keeps_the_same_picture(self):
        # A form that copies the row's logo into a save keeps the real image:
        # the reference is taken as it is, never fetched or re-made.
        picture = png("e", 30 * 1024)
        reference = self.logo(picture)
        with Kube(deployment("frigate", {NAMES.key("icon"): reference})):
            row = json.loads(self.ask("/api/workloads")[1])[0]
        with mock.patch.object(ICONS, "_download", side_effect=AssertionError("nothing is fetched")):
            saved = DEPLOY_SERVER.persist_icon_config({"icon": row["icon"]})
        self.assertEqual(reference, saved["icon"])
        self.assertEqual(picture, Path(ICONS.resolve(saved["icon"], self.data)[0]).read_bytes())


class LinkedClusterTests(unittest.TestCase):
    """The view of every cluster: a linked cluster's logo is fetched from it."""

    def gather(self, what, theirs, ours):
        members = [{"id": "me", "name": "Home", "handle": "home", "self": True, "reachable": True},
                   {"id": "shed 1", "name": "Shed", "handle": "shed", "self": False, "reachable": True}]

        def ask(entry, done, member, what_, user, role):
            entry.update(result=theirs, at=FLEET.time.time(), misses=0, error="", pending=False)
            done.set()
        with mock.patch.object(FLEET, "summary", return_value={"self": "me", "members": members}), \
                mock.patch.object(FLEET, "_ask", ask), mock.patch.dict(FLEET._answers, clear=True), \
                mock.patch.object(FLEET.threading, "Thread",
                                  lambda target, args, **kw: type("T", (), {"start": lambda self: target(*args)})()):
            return FLEET.gather(what, lambda: ours, "ada", "viewer")[0]

    def test_a_linked_clusters_logo_names_that_cluster_and_this_ones_does_not(self):
        mine, theirs = "/api/icons/" + "a" * 64 + ".png", "/api/icons/" + "b" * 64 + ".svg"
        for what in ("workloads", "vms"):
            rows = self.gather(what, [{"name": "x", "icon": theirs}, {"name": "y", "icon": "https://example.com/y.png"},
                                      {"name": "z", "icon": ""}],
                               [{"name": "w", "icon": mine}])
            icons = {(row["site"]["id"], row["name"]): row["icon"] for row in rows}
            self.assertEqual({("me", "w"): mine, ("shed 1", "x"): theirs + "?hs_cluster=shed%201",
                              ("shed 1", "y"): "https://example.com/y.png", ("shed 1", "z"): ""}, icons, what)

    def test_a_linked_clusters_architecture_logos_name_that_cluster(self):
        theirs = "/api/icons/" + "c" * 64 + ".png"
        graph = self.gather("flow", {"workloads": [{"id": "w:lab/x", "icon": theirs}], "volumes": [], "nodes": []},
                            {"workloads": [], "volumes": [], "nodes": []})
        shed = next(c for c in graph["clusters"] if c["site"]["id"] == "shed 1")
        self.assertEqual(theirs + "?hs_cluster=shed%201", shed["workloads"][0]["icon"])

    def test_an_answer_from_an_older_homestead_keeps_its_data_uri(self):
        old = "data:image/png;base64,iVBORw0KGgo="
        rows = self.gather("workloads", [{"name": "x", "icon": old}], [])
        self.assertEqual(old, rows[0]["icon"])


class RelayTests(Served):
    def test_a_logo_named_for_a_linked_cluster_is_asked_of_that_cluster(self):
        relayed = []

        def forward(handler, ref, body, user="", role="", cookies=()):
            relayed.append((ref, handler.path))
            handler.send_response(200)
            handler.send_header("Content-Length", "0")
            handler.end_headers()
        path = "/api/icons/" + "b" * 64 + ".png?hs_cluster=shed1"
        with mock.patch.object(FLEET, "self_id", return_value="me"), \
                mock.patch.object(FLEET, "member", return_value={"id": "shed1", "name": "Shed"}), \
                mock.patch.object(FLEET, "forward", forward):
            status, _, _ = self.ask(path)
        self.assertEqual(200, status)
        self.assertEqual([("shed1", path)], relayed)


if __name__ == "__main__":
    unittest.main()
