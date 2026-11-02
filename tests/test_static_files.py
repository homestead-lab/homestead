"""The app shell is cached and compressed (#705): a versioned file is kept by the
browser for good, everything else is revalidated with an ETag, text is sent
gzipped when the browser accepts it, and demo.js reaches only a demo visit."""
import gzip
import hashlib
import os
import re
import sys
import tempfile
import threading
import unittest
from http.client import HTTPConnection
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "server"))

import server
import homestead_http as HTTP
import homestead_static as STATIC

INDEX = """<!DOCTYPE html>
<html><head><title>Homestead</title></head><body>
<script src="/js/router.js?v=9.9.9"></script>
<script src="/js/demo.js?v=9.9.9"></script>
<script src="/js/core.js?v=9.9.9"></script>
</body></html>
"""
SCRIPT = "/* core */\n" + "const answer = 42;\n" * 200


class Served(unittest.TestCase):
    """A real listener on a temporary web folder; no tests of its own."""
    def setUp(self):
        web = self.enterContext(tempfile.TemporaryDirectory())
        os.makedirs(os.path.join(web, "js"))
        os.makedirs(os.path.join(web, "vendor", "xterm"))
        Path(web, "index.html").write_text(INDEX)
        Path(web, "js", "core.js").write_text(SCRIPT)
        Path(web, "js", "tiny.js").write_text("x")
        Path(web, "vendor", "xterm", "xterm.js").write_text(SCRIPT)
        self.enterContext(mock.patch.object(server, "WEBROOT", web))
        self.enterContext(mock.patch.object(server, "HOMESTEAD_VERSION", "9.9.9"))
        STATIC.forget()
        self.addCleanup(STATIC.forget)
        listener = HTTP.BoundedHTTPServer(("127.0.0.1", 0), server.H)
        threading.Thread(target=listener.serve_forever, daemon=True).start()
        self.addCleanup(listener.server_close)
        self.addCleanup(listener.shutdown)
        self.address = listener.server_address

    def get(self, path, **headers):
        client = HTTPConnection(*self.address, timeout=5)
        try:
            client.request("GET", path, headers=headers)
            response = client.getresponse()
            return response.status, {k.lower(): v for k, v in response.getheaders()}, response.read()
        finally:
            client.close()


class StaticFileTests(Served):
    def test_a_versioned_file_is_kept_for_good(self):
        status, headers, _ = self.get("/js/core.js?v=9.9.9")
        self.assertEqual(200, status)
        self.assertEqual("public, max-age=31536000, immutable", headers["cache-control"])

    def test_an_unversioned_file_is_revalidated_with_an_etag(self):
        status, headers, _ = self.get("/js/core.js")
        self.assertEqual(200, status)
        self.assertEqual("no-cache", headers["cache-control"])
        self.assertTrue(headers.get("etag"))

    def test_the_page_itself_is_revalidated_never_stored_for_good(self):
        status, headers, _ = self.get("/")
        self.assertEqual(200, status)
        self.assertEqual("no-cache", headers["cache-control"])
        self.assertTrue(headers.get("etag"))

    def test_a_matching_etag_answers_304_with_no_body(self):
        _, headers, _ = self.get("/js/core.js")
        status, again, body = self.get("/js/core.js", **{"If-None-Match": headers["etag"]})
        self.assertEqual(304, status)
        self.assertEqual(b"", body)
        self.assertEqual(headers["etag"], again["etag"])

    def test_a_changed_file_gets_a_new_etag(self):
        _, first, _ = self.get("/js/core.js")
        Path(server.WEBROOT, "js", "core.js").write_text(SCRIPT + "// changed\n")
        status, second, body = self.get("/js/core.js", **{"If-None-Match": first["etag"]})
        self.assertEqual(200, status)
        self.assertNotEqual(first["etag"], second["etag"])
        self.assertTrue(body.endswith(b"// changed\n"))

    def test_text_is_gzipped_when_the_browser_accepts_it(self):
        status, headers, body = self.get("/js/core.js", **{"Accept-Encoding": "gzip, deflate, br"})
        self.assertEqual(200, status)
        self.assertEqual("gzip", headers.get("content-encoding"))
        self.assertEqual("Accept-Encoding", headers.get("vary"))
        self.assertEqual(SCRIPT.encode(), gzip.decompress(body))
        self.assertEqual(str(len(body)), headers["content-length"])

    def test_text_is_sent_plain_to_a_browser_that_does_not_accept_gzip(self):
        _, headers, body = self.get("/js/core.js")
        self.assertNotIn("content-encoding", headers)
        self.assertEqual(SCRIPT.encode(), body)

    def test_a_tiny_file_is_not_worth_compressing(self):
        _, headers, body = self.get("/js/tiny.js", **{"Accept-Encoding": "gzip"})
        self.assertNotIn("content-encoding", headers)
        self.assertEqual(b"x", body)

    def test_vendor_files_are_revalidated(self):
        _, headers, _ = self.get("/vendor/xterm/xterm.js")
        self.assertEqual("no-cache", headers["cache-control"])
        self.assertTrue(headers.get("etag"))

    def vendor_font(self, name, data):
        os.makedirs(os.path.join(server.WEBROOT, "vendor", "fonts"), exist_ok=True)
        Path(server.WEBROOT, "vendor", "fonts", name).write_bytes(data)
        return "/vendor/fonts/" + name

    def test_a_vendor_file_named_by_its_content_is_kept_for_good(self):
        data = b"wOF2" + bytes(range(256)) * 8
        path = self.vendor_font(f"Face-latin.{hashlib.sha256(data).hexdigest()[:10]}.woff2", data)
        status, headers, body = self.get(path, **{"Accept-Encoding": "gzip"})
        self.assertEqual(200, status)
        self.assertEqual("public, max-age=31536000, immutable", headers["cache-control"])
        self.assertNotIn("content-encoding", headers)  # woff2 is compressed already
        self.assertEqual(data, body)

    def test_a_content_name_the_bytes_no_longer_match_is_only_revalidated(self):
        data = b"wOF2" + bytes(range(256)) * 8
        path = self.vendor_font(f"Face-latin.{hashlib.sha256(b'other').hexdigest()[:10]}.woff2", data)
        _, headers, _ = self.get(path)
        self.assertEqual("no-cache", headers["cache-control"])

    def test_a_missing_file_is_still_404(self):
        status, _, _ = self.get("/js/missing.js")
        self.assertEqual(404, status)

    def test_demo_js_is_left_out_of_a_plain_visit(self):
        _, _, body = self.get("/")
        self.assertNotIn(b"demo.js", body)
        self.assertIn(b"/js/core.js?v=9.9.9", body)

    def test_demo_js_is_kept_for_a_demo_visit(self):
        _, _, body = self.get("/?demo=1")
        self.assertIn(b'<script src="/js/demo.js?v=9.9.9"></script>', body)

    def test_the_demo_and_plain_page_have_different_etags(self):
        _, plain, _ = self.get("/")
        _, demo, _ = self.get("/?demo=1")
        self.assertNotEqual(plain["etag"], demo["etag"])

    def test_api_replies_are_still_never_stored(self):
        _, headers, _ = self.get("/healthz")
        self.assertEqual("no-store", headers["cache-control"])


if __name__ == "__main__":
    unittest.main()


# Only a script or stylesheet naming the running version is kept for good; the
# page never is, and a weak or repeated validator still counts.
class VersionedCacheTests(StaticFileTests):

    def test_the_page_is_never_kept_for_good_whatever_its_query(self):
        for path in ("/?v=9.9.9", "/?v=1", "/storage?v=x"):
            with self.subTest(path=path):
                _, headers, _ = self.get(path)
                self.assertEqual("no-cache", headers["cache-control"])

    def test_a_script_naming_another_version_is_only_revalidated(self):
        _, headers, _ = self.get("/js/core.js?v=1.0.0")
        self.assertEqual("no-cache", headers["cache-control"])

    def test_a_weak_validator_still_gets_a_304(self):
        _, headers, _ = self.get("/js/core.js")
        status, _, _ = self.get("/js/core.js", **{"If-None-Match": "W/" + headers["etag"]})
        self.assertEqual(304, status)

    def test_a_304_carries_the_cache_policy_vary_and_security_headers(self):
        _, headers, _ = self.get("/js/core.js")
        status, again, _ = self.get("/js/core.js", **{"If-None-Match": headers["etag"]})
        self.assertEqual(304, status)
        self.assertEqual("no-cache", again["cache-control"])
        self.assertEqual("Accept-Encoding", again["vary"])
        self.assertIn("content-security-policy", again)

    def test_demo_given_twice_still_counts_as_a_demo_visit(self):
        _, _, body = self.get("/?demo=1&demo=1")
        self.assertIn(b"demo.js", body)


class VendorVersionTests(Served):
    """noVNC and Monaco load many files by relative path, so they are named under the
    page's version: /vendor/@<version>/... (#982)."""

    def test_a_vendor_file_under_the_running_version_is_kept_for_good(self):
        status, headers, body = self.get("/vendor/@9.9.9/xterm/xterm.js")
        self.assertEqual(200, status)
        self.assertEqual("public, max-age=31536000, immutable", headers["cache-control"])
        self.assertEqual(SCRIPT.encode(), body)

    def test_a_vendor_file_under_another_version_is_only_revalidated(self):
        status, headers, _ = self.get("/vendor/@1.0.0/xterm/xterm.js")
        self.assertEqual(200, status)
        self.assertEqual("no-cache", headers["cache-control"])

    def test_the_unversioned_vendor_path_is_still_revalidated(self):
        self.assertEqual("no-cache", self.get("/vendor/xterm/xterm.js")[1]["cache-control"])

    def test_a_versioned_vendor_path_cannot_climb_out(self):
        for path in ("/vendor/@9.9.9/../../server/server.py", "/vendor/@9.9.9/xterm/x.py", "/vendor/@/xterm/xterm.js"):
            with self.subTest(path=path):
                # refused exactly as an unversioned odd path is, never served
                self.assertEqual(self.get("/vendor/xterm/x.py")[0], self.get(path)[0])
                self.assertNotEqual(200, self.get(path)[0])


class BuildThatIsNotARelease(Served):
    """A CI or plain docker build says "ci" or "dev" for every build: its files are
    named by a hash of the web folder, so they are kept for good and a changed file
    gets a new name (#982)."""

    def setUp(self):
        super().setUp()
        self.enterContext(mock.patch.object(server, "HOMESTEAD_VERSION", "ci"))
        STATIC.forget()

    def name(self):
        _, _, page = self.get("/")
        match = re.search(rb"/js/core\.js\?v=([^\"]+)", page)
        return match.group(1).decode()

    def test_the_page_names_its_files_by_a_hash_not_the_source_version(self):
        name = self.name()
        self.assertRegex(name, r"^c[0-9a-f]{12}$")
        self.assertNotIn(b"9.9.9", self.get("/")[2])

    def test_the_scripts_it_names_are_kept_for_good(self):
        _, headers, _ = self.get(f"/js/core.js?v={self.name()}")
        self.assertEqual("public, max-age=31536000, immutable", headers["cache-control"])

    def test_vendor_files_under_that_name_are_kept_for_good(self):
        _, headers, _ = self.get(f"/vendor/@{self.name()}/xterm/xterm.js")
        self.assertEqual("public, max-age=31536000, immutable", headers["cache-control"])

    def test_a_changed_file_is_never_served_under_the_old_name(self):
        old = self.name()
        Path(server.WEBROOT, "js", "core.js").write_text(SCRIPT + "const changed = 1;\n")
        new = self.name()
        self.assertNotEqual(old, new)
        # a page still naming the old build gets revalidated, not kept
        _, headers, _ = self.get(f"/js/core.js?v={old}")
        self.assertEqual("no-cache", headers["cache-control"])
        self.assertEqual("public, max-age=31536000, immutable", self.get(f"/js/core.js?v={new}")[1]["cache-control"])

    def test_a_file_edited_between_page_loads_is_not_kept_under_the_old_name(self):
        old = self.name()
        self.get(f"/js/core.js?v={old}")
        Path(server.WEBROOT, "js", "core.js").write_text(SCRIPT + "const edited = 2;\n")
        # no page load in between: the old name must not be kept for the new bytes
        _, headers, _ = self.get(f"/js/core.js?v={old}")
        self.assertEqual("no-cache", headers["cache-control"])

    def test_a_file_that_vanishes_or_cannot_be_read_does_not_break_the_name(self):
        Path(server.WEBROOT, "js", "locked.js").write_text("x")
        real_open = open
        def refuse(path, *args, **kwargs):
            if str(path).endswith("locked.js"):
                raise PermissionError(path)
            return real_open(path, *args, **kwargs)
        with mock.patch("homestead_static.open", refuse, create=True):
            STATIC.forget()
            self.assertRegex(self.name(), r"^c[0-9a-f]{12}$")

    def test_an_unchanged_folder_keeps_its_name(self):
        self.assertEqual(self.name(), self.name())

    def test_a_release_page_keeps_its_own_version(self):
        with mock.patch.object(server, "HOMESTEAD_VERSION", "2.8.1"):
            STATIC.forget()
            self.assertIn(b"/js/core.js?v=2.8.1", self.get("/")[2])


class RealPageTests(unittest.TestCase):
    """The shipped index.html: its demo.js line is the one taken out."""

    def test_the_real_page_loses_exactly_its_demo_script_line(self):
        page = (ROOT / "web" / "index.html").read_bytes()
        plain = STATIC._without_demo(page)
        self.assertIn(b"/js/demo.js", page)
        self.assertNotIn(b"/js/demo.js", plain)
        self.assertEqual(len(page.splitlines()) - 1, len(plain.splitlines()))


class RealVendorTests(unittest.TestCase):
    """The shipped noVNC and Monaco: every file their entry points import by a relative
    path exists, so the versioned prefix reaches all of it."""

    def test_novnc_relative_imports_all_resolve(self):
        base = ROOT / "web" / "vendor" / "novnc"
        seen, todo = set(), [base / "core" / "rfb.js"]
        while todo:
            file = todo.pop()
            if file in seen:
                continue
            self.assertTrue(file.is_file(), f"{file} is missing")
            seen.add(file)
            for spec in re.findall(r"""(?:from|import)\s*\(?\s*['"](\.[^'"]+)['"]""", file.read_text()):
                todo.append((file.parent / spec).resolve())
        self.assertGreater(len(seen), 20)
        self.assertTrue(all(str(f).startswith(str(base.resolve())) for f in seen))

    def test_the_page_loads_no_library_by_an_unversioned_path(self):
        storage = sorted(p.name for p in (ROOT / "web" / "js").glob("views-storage-*.js"))
        self.assertTrue(storage)
        for name in ("vm-console.js", *storage, "views-overview.js"):
            text = (ROOT / "web" / "js" / name).read_text()
            with self.subTest(name=name):
                self.assertNotRegex(text, r"""["'`]/vendor/(?:novnc|monaco)""")
