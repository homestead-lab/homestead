import re
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "server"))

import server


class SpaRouteTests(unittest.TestCase):
    def test_known_routes_and_trailing_slashes_serve_the_app(self):
        for path in server.SPA_ROUTES:
            self.assertTrue(server.is_spa_route(path), path)
            if path != "/":
                self.assertTrue(server.is_spa_route(path + "/"), path)

    def test_api_and_unknown_paths_are_not_spa_routes(self):
        for path in ("/api/overview", "/api/not-found", "/unknown", "/style.css"):
            self.assertFalse(server.is_spa_route(path), path)

    def test_a_mistyped_page_address_gets_the_app_to_say_so(self):
        """A typo used to answer with raw JSON; the app now lands on the dashboard."""
        for path in ("/protection", "/containers/extra", "/Volumes"):
            self.assertTrue(server.is_page_path(path), path)

    def test_api_and_file_paths_keep_their_plain_not_found(self):
        for path in ("/api/not-found", "/api", "/missing.js", "/js/nope.js", "/favicon.ico", "/../etc/passwd"):
            self.assertFalse(server.is_page_path(path), path)

    def test_an_unknown_page_serves_the_app_before_authentication(self):
        handler = object.__new__(server.H)
        handler.path = "/protection"
        handler.command = "GET"
        handler.headers = {}
        served = []
        handler._file = lambda path, content_type: served.append((path, content_type))
        handler.do_GET()
        self.assertEqual([(f"{server.WEBROOT}/index.html", "text/html; charset=utf-8")], served)

    def test_browser_and_server_route_allowlists_match(self):
        source = (ROOT / "web" / "js" / "router.js").read_text(encoding="utf-8")
        browser_paths = set(re.findall(r'path:\s*"([^"]+)"', source))
        self.assertEqual(set(server.SPA_ROUTES), browser_paths)

    def test_sidebar_links_use_canonical_routes(self):
        html = (ROOT / "web" / "index.html").read_text(encoding="utf-8")
        hrefs = set(re.findall(r'<a href="([^"]+)" data-view=', html))
        self.assertEqual(set(server.SPA_ROUTES), hrefs)
        self.assertRegex(html, r'<script src="/js/router\.js\?v=\d+\.\d+\.\d+"></script>')

    def test_bundled_svg_assets_are_public_without_allowing_traversal(self):
        self.assertTrue(server.is_asset_path("/assets/homestead-mark.svg"))
        self.assertTrue(server.is_public_path("/assets/homestead-mark.svg"))
        for path in ("/assets/../server.py", "/assets/nested/mark.svg", "/assets/mark.png"):
            self.assertFalse(server.is_asset_path(path), path)
            self.assertFalse(server.is_public_path(path), path)

    def test_bundled_svg_asset_is_served_with_svg_mime_type(self):
        handler = object.__new__(server.H)
        handler.path = "/assets/homestead-mark.svg?v=2.1.0"
        handler.command = "GET"
        handler.headers = {}
        served = []
        handler._file = lambda path, content_type: served.append((path, content_type))
        handler.do_GET()
        self.assertEqual(
            [(f"{server.WEBROOT}/assets/homestead-mark.svg", "image/svg+xml")],
            served,
        )

    def test_browser_script_path_matches_source_and_container_layout(self):
        handler = object.__new__(server.H)
        handler.path = "/js/router.js"
        handler.command = "GET"
        handler.headers = {}
        served = []
        handler._file = lambda path, content_type: served.append((path, content_type))
        handler.do_GET()
        self.assertEqual(
            [(f"{server.WEBROOT}/js/router.js", "application/javascript")],
            served,
        )

    def test_direct_route_returns_app_shell_before_authentication(self):
        handler = object.__new__(server.H)
        handler.path = "/containers?from=bookmark"
        handler.command = "GET"
        handler.headers = {}
        served = []
        handler._file = lambda path, content_type: served.append((path, content_type))
        handler.do_GET()
        self.assertEqual([(f"{server.WEBROOT}/index.html", "text/html; charset=utf-8")], served)


if __name__ == "__main__":
    unittest.main()
