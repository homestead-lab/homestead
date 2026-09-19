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

    def test_browser_and_server_route_allowlists_match(self):
        source = (ROOT / "web" / "js" / "router.js").read_text(encoding="utf-8")
        browser_paths = set(re.findall(r'path:\s*"([^"]+)"', source))
        self.assertEqual(set(server.SPA_ROUTES), browser_paths)

    def test_sidebar_links_use_canonical_routes(self):
        html = (ROOT / "web" / "index.html").read_text(encoding="utf-8")
        hrefs = set(re.findall(r'<a href="([^"]+)" data-view=', html))
        self.assertEqual(set(server.SPA_ROUTES), hrefs)
        self.assertIn('<script src="/js/router.js"></script>', html)

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
