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
        self.enterContext(mock.patch.object(ICONS, "_UNCACHEABLE", {}, create=True))
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

    def test_a_logo_annotation_edited_by_hand_to_point_elsewhere_is_no_logo(self):
        # Anyone who may edit the app may write its annotation (kubectl): the
        # page fetches what a row names, so a path or scheme that is neither an
        # exact cached logo, an http(s) address nor a picture is no logo.
        good = self.logo(png("good", 300))
        name = good[len("/api/icons/"):]
        bad = {"dots": "/api/icons/../auth/keys", "dots-hash": f"/api/icons/../icons/{name}",
               "query": f"{good}?x=1", "fragment": f"{good}#x", "host": f"//example.com{good}",
               "deeper": f"/api/icons/x/{name}", "upper": good.upper(), "script": "javascript:alert(1)",
               "page": "data:text/html;base64,PGI+", "spaced": "https://example.com/a b.png"}
        kept = {"picture": "data:image/png;base64,iVBORw0KGgo="}
        apps = [deployment(key, {NAMES.key("icon"): value}) for key, value in {**bad, **kept}.items()]
        with mock.patch.object(server.NAMES_HELPERS, "_heal_soon"), \
                Kube(deployment("good", {NAMES.key("icon"): good}), *apps):
            rows = {row["name"]: row["icon"] for row in json.loads(self.ask("/api/workloads")[1])}
        self.assertEqual({"good": good, **{key: "" for key in bad}, **kept}, rows)
        self.assertFalse(set(bad.values()) & set(server.NAMES_HELPERS._ICON_TRIED), "no repair is asked for them")

    OLD = "https://example.com/old.png"     # v1.9.0 to v1.14.1 wrote the App Store's address

    def read_with_old_address(self):
        """One /api/workloads read of an app whose annotation is an old
        address: (its row, the whole answer, the repairs asked for)."""
        asked = []
        with mock.patch.object(server.NAMES_HELPERS, "_heal_soon", lambda ref, ann: asked.append((ref, dict(ann)))), \
                Kube(deployment("frigate", {NAMES.key("icon"): self.OLD})):
            body = self.ask("/api/workloads")[1]
        return json.loads(body)[0], body, asked

    def test_an_old_logo_address_is_never_given_to_the_page_and_is_kept_as_a_cached_logo(self):
        row, body, asked = self.read_with_old_address()
        self.assertNotIn(b"old.png", body, "the page is never told the address")
        self.assertEqual(("", True), (row["icon"], row["has_logo"]), "initials while it is copied; not offered to Find logos")
        self.assertEqual([self.OLD], [ref for ref, _ in asked], "its copy into the cache is asked for")
        picture = png("old", 2000)
        with mock.patch.object(ICONS, "_download", return_value=(picture, "image/png")) as fetched:
            server.NAMES_HELPERS._heal_icon(*asked[0])
        fetched.assert_called_once_with(self.OLD)        # the icon code's own checked fetch
        row, body, _ = self.read_with_old_address()
        self.assertTrue(ICONS.is_reference(row["icon"]), row["icon"])
        self.assertNotIn(b"old.png", body)
        self.assertEqual(picture, Path(ICONS.resolve(row["icon"], self.data)[0]).read_bytes())

    def test_an_old_logo_address_that_cannot_be_copied_is_offered_to_find_logos_again(self):
        row, _, asked = self.read_with_old_address()
        for refusal in (ValueError("logo host must resolve only to public addresses"),
                        ValueError("logo is too large (maximum 256 KiB)")):
            with self.subTest(refusal=str(refusal)), mock.patch.object(ICONS, "_download", side_effect=refusal):
                server.NAMES_HELPERS._heal_icon(*asked[0])
                row, body, _ = self.read_with_old_address()
                self.assertEqual(("", False), (row["icon"], row["has_logo"]))
                self.assertNotIn(b"old.png", body)

    def test_an_old_logo_address_is_copied_at_most_once_in_a_while_not_on_every_read(self):
        started = []
        annotations = {NAMES.key("icon"): self.OLD}
        with mock.patch.object(server.NAMES_HELPERS.threading, "Thread",
                               lambda **kw: type("T", (), {"start": lambda self: started.append(kw)})()):
            for _ in range(4):
                self.assertEqual("", server.NAMES_HELPERS.display_icon(annotations))
            self.assertEqual(1, len(started), "four reads, one copy")
            server.NAMES_HELPERS._ICON_TRIED[self.OLD] -= server.NAMES_HELPERS.ICON_RETRY_SECONDS + 1
            server.NAMES_HELPERS.display_icon(annotations)
        self.assertEqual(2, len(started), "tried again after ICON_RETRY_SECONDS")

    def test_only_an_exact_logo_path_is_taken_as_a_cached_logo(self):
        good = self.logo(png("exact", 300))
        name = good[len("/api/icons/"):]
        for value in (f"/api/icons/../icons/{name}", f"/api/icons/x/{name}", f"/x/{name}", name,
                      f"{good}?x=1", f"//example.com{good}"):
            with self.subTest(value=value):
                self.assertFalse(ICONS.is_reference(value))
                with self.assertRaises(FileNotFoundError):
                    ICONS.resolve(value, self.data)
                with self.assertRaises((FileNotFoundError, ValueError)), \
                        mock.patch.object(ICONS, "_download", side_effect=ValueError("not fetched")):
                    ICONS.persist(value, self.data)
        self.assertTrue(ICONS.is_reference(good))
        self.assertEqual(good, ICONS.persist(good, self.data))

    def test_the_page_fetches_logos_of_exactly_the_names_the_server_allows(self):
        import re
        core = (Path(__file__).resolve().parents[1] / "web" / "js" / "core.js").read_text(encoding="utf-8")
        page = re.search(r"const ICON_PATH = /\^\\/api\\/icons\\/\[0-9a-f\]\{64\}\\\.\(([a-z|]+)\)", core)
        self.assertIsNotNone(page, "core.js names the logo path it fetches")
        self.assertEqual(ICONS.ICON_NAME.pattern, r"[0-9a-f]{64}\.(" + page.group(1) + ")")

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
                              ("shed 1", "y"): "", ("shed 1", "z"): ""}, icons, what)

    def test_a_linked_clusters_architecture_logos_name_that_cluster(self):
        theirs = "/api/icons/" + "c" * 64 + ".png"
        graph = self.gather("flow", {"workloads": [{"id": "w:lab/x", "icon": theirs}], "volumes": [], "nodes": []},
                            {"workloads": [], "volumes": [], "nodes": []})
        shed = next(c for c in graph["clusters"] if c["site"]["id"] == "shed 1")
        self.assertEqual(theirs + "?hs_cluster=shed%201", shed["workloads"][0]["icon"])

    def test_an_answer_from_an_older_homestead_keeps_its_picture(self):
        kept = ["data:image/png;base64,iVBORw0KGgo=", "data:image/svg+xml;base64,PHN2Zy8+",
                "data:image/jpeg;base64,/9j/", "data:image/x-icon;base64,AAABAA=="]
        rows = self.gather("workloads", [{"name": f"x{i}", "icon": v} for i, v in enumerate(kept)], [])
        self.assertEqual(kept, [row["icon"] for row in rows])

    def test_any_other_logo_a_linked_cluster_sends_is_no_logo(self):
        # On dev every value passed through, and the page loaded it as an <img>.
        refused = ["https://example.com/y.png", "http://192.0.2.9/y.png", "//example.com/y.png",
                   "/api/icons/../auth/keys", "/api/icons/" + "b" * 64 + ".png?x=1", "/assets/os-debian.svg",
                   "javascript:alert(1)", "data:text/html;base64,PGI+", "data:image/svg+xml,<svg onload=x>",
                   "data:image/png;base64,AA AA", "data:image/bmp;base64,Qk0=", 7]
        for what in ("workloads", "vms"):
            rows = self.gather(what, [{"name": f"x{i}", "icon": v} for i, v in enumerate(refused)], [])
            self.assertEqual([""] * len(refused), [row["icon"] for row in rows], what)
        graph = self.gather("flow", {"workloads": [{"id": "w:lab/x", "icon": refused[0]}], "volumes": [], "nodes": []},
                            {"workloads": [], "volumes": [], "nodes": []})
        shed = next(c for c in graph["clusters"] if c["site"]["id"] == "shed 1")
        self.assertEqual("", shed["workloads"][0]["icon"])


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
