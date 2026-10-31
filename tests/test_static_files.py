"""The app shell is cached and compressed (#705): a versioned file is kept by the
browser for good, everything else is revalidated with an ETag, text is sent
gzipped when the browser accepts it, and demo.js reaches only a demo visit."""
import gzip
import hashlib
import os
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


class StaticFileTests(unittest.TestCase):
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


class RealPageTests(unittest.TestCase):
    """The shipped index.html: its demo.js line is the one taken out."""

    def test_the_real_page_loses_exactly_its_demo_script_line(self):
        page = (ROOT / "web" / "index.html").read_bytes()
        plain = STATIC._without_demo(page)
        self.assertIn(b"/js/demo.js", page)
        self.assertNotIn(b"/js/demo.js", plain)
        self.assertEqual(len(page.splitlines()) - 1, len(plain.splitlines()))
