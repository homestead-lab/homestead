import json
import struct
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "server"))

import homestead_icons as icons


def png(size):
    return b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\rIHDR" + struct.pack(">II", size, size) + b"\x08\x06\x00\x00\x00" + b"x" * 40


def ico(*sizes):
    head = b"\x00\x00\x01\x00" + struct.pack("<H", len(sizes))
    return head + b"".join(bytes([s % 256, s % 256]) + b"\x00" * 14 for s in sizes)


SVG = b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 10 10"><rect width="10" height="10"/></svg>'


class Site:
    """A fake web: path -> (bytes, declared type). _fetch is patched to read it."""
    def __init__(self, pages):
        self.pages, self.asked = pages, []

    def fetch(self, url, accept, limit=icons.MAX_ICON_BYTES, deadline=None):
        self.asked.append(url)
        if url not in self.pages:
            raise ValueError("logo server did not return a successful response")
        data, declared = self.pages[url]
        return data, declared, url


def page(head):
    return (f"<!doctype html><html><head>{head}</head><body>hello</body></html>".encode(), "text/html")


class SiteIconTests(unittest.TestCase):
    def keep(self, site, url="https://example.com/"):
        with tempfile.TemporaryDirectory() as data_dir, mock.patch.object(icons, "_fetch", site.fetch):
            ref = icons.persist(url, data_dir)
            path, mime = icons.resolve(ref, data_dir)
            return Path(path).read_bytes(), mime

    def test_an_svg_icon_is_taken_first(self):
        site = Site({"https://example.com/": page('<link rel="icon" href="/fav.ico"><link rel="icon" type="image/svg+xml" href="/logo.svg">'),
                     "https://example.com/logo.svg": (SVG, "image/svg+xml"), "https://example.com/fav.ico": (ico(16), "image/x-icon")})
        data, mime = self.keep(site)
        self.assertEqual("image/svg+xml", mime)
        self.assertNotIn("https://example.com/fav.ico", site.asked, "an SVG is as good as it gets")

    def test_the_largest_declared_size_and_a_relative_base(self):
        site = Site({"https://example.com/app/": page('<base href="/static/"><link rel="icon" sizes="32x32" href="i32.png">'
                                                      '<link rel="apple-touch-icon" href="touch.png">'),
                     "https://example.com/static/i32.png": (png(32), "image/png"),
                     "https://example.com/static/touch.png": (png(180), "image/png")})
        data, _ = self.keep(site, "https://example.com/app/")
        self.assertEqual(png(180), data)

    def test_the_manifest_is_read_for_larger_icons(self):
        manifest = {"icons": [{"src": "/m/192.png", "sizes": "192x192", "type": "image/png"},
                              {"src": "/m/mono.png", "sizes": "512x512", "purpose": "monochrome"}]}
        site = Site({"https://example.com/": page('<link rel="manifest" href="/site.webmanifest"><link rel="icon" href="/favicon.ico">'),
                     "https://example.com/site.webmanifest": (json.dumps(manifest).encode(), "application/manifest+json"),
                     "https://example.com/m/192.png": (png(192), "image/png"),
                     "https://example.com/favicon.ico": (ico(16, 32), "image/x-icon")})
        data, _ = self.keep(site)
        self.assertEqual(png(192), data)
        self.assertNotIn("https://example.com/m/mono.png", site.asked, "a monochrome mask is not a logo")

    def test_a_big_favicon_ico_will_do(self):
        site = Site({"https://example.com/": page("<title>no icons declared</title>"),
                     "https://example.com/favicon.ico": (ico(16, 32, 128), "image/x-icon")})
        self.assertEqual("image/x-icon", self.keep(site)[1])

    def test_only_small_icons_is_refused_with_why(self):
        site = Site({"https://example.com/": page('<link rel="icon" sizes="16x16" href="/s.png">'),
                     "https://example.com/s.png": (png(16), "image/png"),
                     "https://example.com/favicon.ico": (ico(16, 32), "image/x-icon")})
        with self.assertRaisesRegex(ValueError, "no logo of at least 64 px \\(its largest is 32 px\\)"):
            self.keep(site)

    def test_a_declared_size_is_checked_against_the_picture(self):
        site = Site({"https://example.com/": page('<link rel="icon" sizes="192x192" href="/liar.png">'),
                     "https://example.com/liar.png": (png(16), "image/png"),
                     "https://example.com/favicon.ico": (ico(16), "image/x-icon")})
        with self.assertRaisesRegex(ValueError, "64 px"):
            self.keep(site)

    def test_an_image_url_is_kept_as_before(self):
        site = Site({"https://example.com/logo.png": (png(24), "image/png")})
        self.assertEqual(png(24), self.keep(site, "https://example.com/logo.png")[0], "an image asked for by address is not judged")

    def test_every_icon_fetch_goes_through_the_public_address_check(self):
        with mock.patch.object(icons, "_public_target", side_effect=ValueError("logo host must resolve only to public addresses")):
            with self.assertRaisesRegex(ValueError, "public addresses"):
                icons._fetch("https://example.com/", icons.IMAGE_ACCEPT)

    def test_a_page_pointing_at_private_addresses_finds_nothing(self):
        site = Site({"https://example.com/": page('<link rel="icon" href="http://192.168.1.1/x.svg">')})
        real = site.fetch

        def guarded(url, *a, **k):
            if "192.168." in url:
                raise ValueError("logo host must resolve only to public addresses")
            return real(url, *a, **k)
        site.fetch = guarded
        with self.assertRaisesRegex(ValueError, "no logo"):
            self.keep(site)


if __name__ == "__main__":
    unittest.main()
