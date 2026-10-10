"""A logo too large to keep - an App Store picture can be - is drawn smaller
by the browser and that copy kept (homestead_icons.py, core.js logoReady)."""
import base64
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "server"))

import homestead_icons as icons
import homestead_route_policy as POLICY
import server

PNG = b"\x89PNG\r\n\x1a\n" + b"homestead-small-logo"
BIG_PNG = b"\x89PNG\r\n\x1a\n" + b"x" * (icons.MAX_ICON_BYTES + 10)
SVG = b'<svg xmlns="http://www.w3.org/2000/svg"></svg>'


class Keep(unittest.TestCase):
    def test_a_logo_that_fits_is_kept_as_it_is(self):
        with tempfile.TemporaryDirectory() as data_dir, \
                mock.patch.object(icons, "_download", return_value=(PNG, "image/png")):
            answer = icons.keep("https://example.com/logo.png", data_dir)
        self.assertTrue(answer["icon"].startswith("/api/icons/"))

    def test_a_logo_too_large_asks_for_a_smaller_copy_instead_of_failing(self):
        with tempfile.TemporaryDirectory() as data_dir, \
                mock.patch.object(icons, "_download", side_effect=ValueError("logo is too large (maximum 256 KiB)")):
            self.assertEqual({"too_large": True}, icons.keep("https://example.com/huge.png", data_dir))

    def test_other_failures_still_say_what_went_wrong(self):
        with tempfile.TemporaryDirectory() as data_dir, \
                mock.patch.object(icons, "_download", side_effect=ValueError("logo server did not return an image")):
            with self.assertRaisesRegex(ValueError, "did not return an image"):
                icons.keep("https://example.com/page", data_dir)


class Source(unittest.TestCase):
    def test_a_large_picture_comes_back_whole_for_the_browser(self):
        with mock.patch.object(icons, "_fetch", return_value=(BIG_PNG, "image/png", "https://example.com/huge.png")) as fetch:
            data, mime = icons.large_source("https://example.com/huge.png")
        self.assertEqual((BIG_PNG, "image/png"), (data, mime))
        self.assertEqual(icons.MAX_SOURCE_BYTES, fetch.call_args.args[2])

    def test_only_pictures_are_passed_through(self):
        # An SVG is a document: never handed back from Homestead's own origin.
        with mock.patch.object(icons, "_fetch", return_value=(SVG, "image/svg+xml", "https://example.com/a.svg")):
            with self.assertRaisesRegex(ValueError, "only a picture"):
                icons.large_source("https://example.com/a.svg")
        for bad in ("", "file:///etc/passwd", "/api/icons/x.png", "https://example.com/" + "a" * 2100):
            with self.assertRaisesRegex(ValueError, "http"):
                icons.large_source(bad)

    def test_it_is_fetched_from_public_addresses_only(self):
        with mock.patch.object(icons.socket, "getaddrinfo", return_value=[
                (icons.socket.AF_INET, icons.socket.SOCK_STREAM, 6, "", ("192.0.2.2", 443))]):
            with self.assertRaisesRegex(ValueError, "public addresses"):
                icons.large_source("https://internal.example/logo.png")


class Smaller(unittest.TestCase):
    def test_the_smaller_copy_is_kept(self):
        with tempfile.TemporaryDirectory() as data_dir:
            reference = icons.keep_smaller(base64.b64encode(PNG).decode(), data_dir)
            self.assertEqual(PNG, Path(icons.resolve(reference, data_dir)[0]).read_bytes())

    def test_anything_but_a_small_picture_is_refused(self):
        with tempfile.TemporaryDirectory() as data_dir:
            with self.assertRaisesRegex(ValueError, "whole"):
                icons.keep_smaller("not base64!", data_dir)
            with self.assertRaisesRegex(ValueError, "PNG, WebP or JPEG"):
                icons.keep_smaller(base64.b64encode(SVG).decode(), data_dir)
            with self.assertRaisesRegex(ValueError, "too large"):
                icons.keep_smaller(base64.b64encode(BIG_PNG).decode(), data_dir)


class Routes(unittest.TestCase):
    def test_operators_only_and_not_under_the_public_icon_path(self):
        for method, path in (("POST", "/api/logo-fit/keep"), ("GET", "/api/logo-fit/source"),
                             ("POST", "/api/logo-fit/resized")):
            self.assertEqual("operator", POLICY.role(path, method))
            self.assertFalse(server.is_public_path(path), path)

    def test_the_picture_is_sent_as_itself_not_as_json(self):
        handler = object.__new__(server.H)
        handler.path, handler.headers, handler.command = "/api/logo-fit/source?url=https%3A%2F%2Fexample.com%2Fhuge.png", {}, "GET"
        handler._guard = lambda path: False
        handler._send = mock.Mock()
        handler.user, handler.role = "ada", "operator"
        with mock.patch.object(server.ICONS, "_fetch", return_value=(BIG_PNG, "image/png", "")):
            handler.do_GET()
        self.assertEqual((200, BIG_PNG, "image/png"), handler._send.call_args.args)
        self.assertIn(("X-Content-Type-Options", "nosniff"), handler._extra_headers)


if __name__ == "__main__":
    unittest.main()
