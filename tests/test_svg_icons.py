import hashlib
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "server"))

import homestead_icons as icons

LOGO = b'''<?xml version="1.0" encoding="UTF-8"?>
<!-- made in an editor -->
<svg xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink"
     xmlns:inkscape="http://www.inkscape.org/namespaces/inkscape" viewBox="0 0 64 64" inkscape:version="1.3">
  <defs><linearGradient id="g"><stop offset="0" stop-color="#2f6fb0"/><stop offset="1" stop-color="#1b3a5e"/></linearGradient></defs>
  <style>.mark{fill:url(#g)}</style>
  <rect width="64" height="64" rx="14" fill="url(#g)"/>
  <path class="mark" d="M16 44 32 16l16 28z" stroke="#fff" stroke-width="2"/>
  <use xlink:href="#g"/>
  <text x="32" y="58" text-anchor="middle">S<tspan>r</tspan></text>
</svg>'''


def clean(svg):
    return icons.clean_svg(svg).decode()


class CleanSvgTests(unittest.TestCase):
    def test_a_real_logo_keeps_its_drawing(self):
        out = clean(LOGO)
        for kept in ('viewBox="0 0 64 64"', "<linearGradient", 'stop-color="#2f6fb0"', 'fill="url(#g)"',
                     ".mark{fill:url(#g)}", 'd="M16 44 32 16l16 28z"', 'href="#g"', ">S<", ">r</"):
            self.assertIn(kept, out)
        self.assertNotIn("inkscape", out)
        self.assertNotIn("<!--", out)
        self.assertTrue(out.startswith('<svg xmlns="http://www.w3.org/2000/svg"'))

    def test_anything_that_runs_or_reaches_out_is_removed(self):
        attack = b'''<svg xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink" onload="alert(1)">
          <script>alert(2)</script>
          <rect width="10" height="10" onclick="alert(3)" fill="url(https://example.com/track.svg#x)"/>
          <a href="javascript:alert(4)"><rect width="1" height="1"/></a>
          <foreignObject><iframe xmlns="http://www.w3.org/1999/xhtml" src="https://example.com"/></foreignObject>
          <image href="https://example.com/pixel.png"/>
          <use xlink:href="https://example.com/sprite.svg#icon"/>
          <set attributeName="href" to="javascript:alert(5)"/>
          <animate attributeName="href" values="javascript:alert(6)"/>
          <style>@import url(https://example.com/x.css); .a{fill:red}</style>
          <circle r="4" style="fill:url('https://example.com/a');stroke:red"/>
          <path d="M0 0" filter="javascript:alert(7)"/>
        </svg>'''
        out = clean(attack).lower()
        for gone in ("alert", "script", "onload", "onclick", "example.com", "foreignobject", "iframe",
                     "<image", "<a ", "<set", "<animate", "@import", "javascript"):
            self.assertNotIn(gone, out, gone)
        self.assertIn("fill:none;stroke:red", out.replace(" ", ""), "an outside url() becomes none")

    def test_doctype_entities_and_non_svg_are_refused(self):
        bomb = b'<?xml version="1.0"?><!DOCTYPE svg [<!ENTITY a "aaaa"><!ENTITY b "&a;&a;">]><svg xmlns="http://www.w3.org/2000/svg">&b;</svg>'
        with self.assertRaisesRegex(ValueError, "DOCTYPE"):
            icons.clean_svg(bomb)
        with self.assertRaisesRegex(ValueError, "not an SVG"):
            icons.clean_svg(b"<html><body/></html>")
        with self.assertRaisesRegex(ValueError, "could not be read"):
            icons.clean_svg(b"<svg><rect></svg>")

    def test_cleaning_twice_gives_the_same_bytes(self):
        once = icons.clean_svg(LOGO)
        self.assertEqual(once, icons.clean_svg(once), "a moved logo keeps its content-addressed name")

    def test_svg_is_sniffed_with_or_without_a_declaration(self):
        self.assertEqual("image/svg+xml", icons._sniff_mime(LOGO))
        self.assertEqual("image/svg+xml", icons._sniff_mime(b'\xef\xbb\xbf  <svg xmlns="http://www.w3.org/2000/svg"/>'))
        with self.assertRaises(ValueError):
            icons._sniff_mime(b"<?xml version='1.0'?><html/>")


class StoredSvgTests(unittest.TestCase):
    def test_the_cleaned_svg_is_what_is_kept_and_served(self):
        with tempfile.TemporaryDirectory() as data_dir, \
                mock.patch.object(icons, "_download", return_value=(LOGO, "image/svg+xml")):
            url = icons.persist("https://example.com/logo.svg", data_dir)
            cleaned = icons.clean_svg(LOGO)
            self.assertEqual(f"/api/icons/{hashlib.sha256(cleaned).hexdigest()}.svg", url)
            path, mime = icons.resolve(url, data_dir)
            self.assertEqual(("image/svg+xml", cleaned), (mime, Path(path).read_bytes()))
            self.assertTrue(icons.data_url(url, data_dir).startswith("data:image/svg+xml;base64,"))

    def test_bytes_from_another_homestead_are_cleaned_too(self):
        with tempfile.TemporaryDirectory() as data_dir:
            url = icons.store(b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script><rect width="1"/></svg>', data_dir)
            path, _ = icons.resolve(url, data_dir)
            self.assertNotIn(b"script", Path(path).read_bytes())

    def test_served_svg_carries_a_sandbox_policy(self):
        import server
        with tempfile.TemporaryDirectory() as data_dir:
            url = icons.store(LOGO, data_dir)
            sent = []
            handler = mock.Mock()
            handler.send_header = lambda k, v: sent.append((k, v))
            handler.wfile = mock.Mock()
            with mock.patch.object(server, "DATA_DIR", data_dir):
                server.H._icon(handler, url)
            policies = [v for k, v in sent if k == "Content-Security-Policy"]
            self.assertIn(icons.SVG_POLICY, policies)
            self.assertIn(("Content-Type", "image/svg+xml"), sent)


if __name__ == "__main__":
    unittest.main()
