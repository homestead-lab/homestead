import json
import socket
import sys
import unittest
import urllib.error
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "server"))

import homestead_portal as PORTAL

WORKLOADS = [
    {"ns": "lab", "name": "frigate", "icon": "data:image/png;base64,AAA", "group": "Home",
     "ports": [{"port": 5000, "ip": "192.0.2.214", "name": "http"}, {"port": 8443, "ip": "192.0.2.214"}]},
    {"ns": "lab", "name": "web", "icon": "", "ports": [{"port": 80, "ip": "192.0.2.9"}]},
    {"ns": "lab", "name": "quiet", "icon": "", "ports": []},
]


class PortalTests(unittest.TestCase):
    def setUp(self):
        self.map = None
        self.persisted = []

        def get(path):
            if self.map is None:
                raise urllib.error.HTTPError(path, 404, "missing", None, None)
            return {"metadata": {"resourceVersion": "7"}, "data": {"links.json": self.map}}

        def send(method, path, body=None, **kw):
            self.map = body["data"]["links.json"]
            self.last = (method, path, body)
            return body

        def persist(source):
            self.persisted.append(source)
            return "/api/icons/" + "a" * 64 + ".png"

        PORTAL.bind(get, send, "lab", lambda: WORKLOADS, persist, lambda ref: "data:image/png;base64,CACHED")

    def test_links_are_checked_and_stored(self):
        result = PORTAL.save([
            {"title": "  Gateway  ", "url": "https://192.0.2.1", "section": "Network", "icon": "builtin:router"},
            {"title": "Frigate", "url": "http://192.0.2.214:5000", "icon": "workload:lab/frigate"},
            {"title": "NAS", "url": "http://tower.lan", "icon": "https://example.com/logo.png", "note": "Unraid"},
        ])
        self.assertEqual("POST", self.last[0])
        rows = json.loads(self.map)
        self.assertEqual("Gateway", rows[0]["title"])
        self.assertEqual(3, len({row["id"] for row in rows}))
        self.assertEqual(["https://example.com/logo.png"], self.persisted)
        shown = [link["shown"] for link in result["links"]]
        self.assertEqual({"kind": "builtin", "src": "router"}, shown[0])
        self.assertEqual({"kind": "image", "src": "data:image/png;base64,AAA"}, shown[1])
        self.assertEqual({"kind": "image", "src": "data:image/png;base64,CACHED"}, shown[2])
        # A second save updates the same map and keeps the ids it was given.
        PORTAL.save(rows)
        self.assertEqual("PUT", self.last[0])
        self.assertEqual([row["id"] for row in rows], [row["id"] for row in json.loads(self.map)])

    def test_bad_links_are_refused_before_anything_is_written(self):
        for row in ({"title": "", "url": "http://a"},
                    {"title": "x", "url": "ftp://a"},
                    {"title": "x", "url": "http://admin:secret@192.0.2.1"},
                    {"title": "x", "url": "http://a", "icon": "builtin:toaster"},
                    {"title": "x", "url": "http://a", "icon": "workload:../etc"},
                    {"title": "x" * 61, "url": "http://a"}):
            with self.subTest(row=row), self.assertRaises(ValueError):
                PORTAL.save([row])
        self.assertIsNone(self.map)
        with self.assertRaises(ValueError):
            PORTAL.save("not a list")

    def test_containers_offer_each_exposed_port(self):
        rows = PORTAL.candidates()
        self.assertEqual(["http://192.0.2.214:5000", "https://192.0.2.214:8443", "http://192.0.2.9"],
                         [row["url"] for row in rows])
        self.assertEqual("workload:lab/frigate", rows[0]["icon"])
        self.assertTrue(rows[0]["has_logo"])
        self.assertFalse(rows[2]["has_logo"])

    def test_a_link_answers_when_its_port_accepts_a_connection(self):
        server = socket.socket()
        server.bind(("127.0.0.1", 0))
        server.listen(1)
        port = server.getsockname()[1]
        closed = socket.socket()
        closed.bind(("127.0.0.1", 0))
        dead = closed.getsockname()[1]
        closed.close()
        try:
            self.map = json.dumps([{"id": "aaaaaaaa", "title": "up", "url": f"http://127.0.0.1:{port}/admin"},
                                   {"id": "bbbbbbbb", "title": "down", "url": f"http://127.0.0.1:{dead}"}])
            result = PORTAL.status(force=True)
        finally:
            server.close()
        self.assertTrue(result["aaaaaaaa"]["up"])
        self.assertFalse(result["bbbbbbbb"]["up"])


if __name__ == "__main__":
    unittest.main()
