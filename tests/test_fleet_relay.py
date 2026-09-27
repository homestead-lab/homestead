"""A request relayed through one real Homestead server to another, over HTTP.

The far Homestead runs in a process of its own, so each side has its own
view of the links, as two clusters would.
"""
import base64
import json
import subprocess
import sys
import textwrap
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "server"))
import server

KEY = base64.b64encode(b"k" * 32).decode()

FAR = textwrap.dedent("""
    import base64, json, sys
    sys.path.insert(0, sys.argv[1])
    import server
    from http.server import ThreadingHTTPServer
    state = {"self": "shed1", "revision": 2, "members": [
        {"id": "loft1", "handle": "loft", "name": "Loft", "url": "http://127.0.0.1:1"},
        {"id": "shed1", "handle": "shed", "name": "Shed", "url": "http://127.0.0.1:1"}]}
    server.FLEET._load = lambda fresh=False: (state, base64.b64decode(sys.argv[2]))
    # The one route this test posts to: it says what arrived, and from whom.
    server.FLEET.set_address = lambda url: {"ok": True, "url": url}
    # No Kubernetes here: its name and its accounts, as a cluster would say.
    server.FLEET._site = lambda: "Shed"
    server.AUTH.needs_setup = lambda: True
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.H)
    print(httpd.server_address[1], flush=True)
    httpd.serve_forever()
""")


class RelayOverHttpTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.far = subprocess.Popen([sys.executable, "-c", FAR, str(ROOT / "server"), KEY],
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        cls.far_port = int(cls.far.stdout.readline().strip())
        cls.state = {"self": "loft1", "revision": 2, "members": [
            {"id": "loft1", "handle": "loft", "name": "Loft", "url": "http://127.0.0.1:1"},
            {"id": "shed1", "handle": "shed", "name": "Shed", "url": f"http://127.0.0.1:{cls.far_port}"}]}
        cls.patches = [mock.patch.object(server.FLEET, "_load",
                                         lambda fresh=False: (cls.state, base64.b64decode(KEY)))]
        for patch in cls.patches:
            patch.start()
        cls.near = ThreadingHTTPServer(("127.0.0.1", 0), server.H)
        threading.Thread(target=cls.near.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.near.shutdown()
        for patch in cls.patches:
            patch.stop()
        cls.far.terminate()
        cls.far.wait(5)

    def ask(self, path, role="operator", body=None):
        request = urllib.request.Request(f"http://127.0.0.1:{self.near.server_address[1]}{path}",
                                         data=json.dumps(body).encode() if body is not None else None,
                                         method="POST" if body is not None else "GET")
        request.add_header("Cookie", "homestead_cluster=shed1; homestead_session=tok")
        if body is not None:
            request.add_header("Content-Type", "application/json")
            request.add_header("X-Homestead-Auth", "1")
        with mock.patch.object(server.AUTH, "verify_token", return_value={"user": "james", "role": role}):
            try:
                with urllib.request.urlopen(request, timeout=10) as response:
                    return response.status, json.loads(response.read() or b"{}"), response.headers
            except urllib.error.HTTPError as error:
                return error.code, json.loads(error.read() or b"{}"), error.headers

    def test_the_far_cluster_sees_the_person_signed_in_here(self):
        status, state, headers = self.ask("/api/auth/state")
        self.assertEqual(200, status)
        self.assertEqual("james@loft", state["user"])
        self.assertEqual("operator", state["role"])
        self.assertEqual("Loft", state["via"])
        self.assertEqual("close", headers.get("Connection"))

    def test_the_far_cluster_applies_its_own_rules_to_the_role(self):
        status, answer, _ = self.ask("/api/fleet/address", body={"url": "http://10.0.0.2:8088"})
        self.assertEqual(403, status)
        self.assertIn("admin required", answer["error"])

    def test_a_body_arrives_as_it_was_sent_and_signed(self):
        status, answer, _ = self.ask("/api/fleet/address", role="admin", body={"url": "http://10.0.0.2:8088"})
        self.assertEqual(200, status)
        self.assertEqual("http://10.0.0.2:8088", answer["url"])

    def test_the_far_cluster_says_who_it_is(self):
        status, hello, _ = self.ask("/api/fleet/hello")
        self.assertEqual(200, status)
        self.assertEqual("shed1", hello["id"])


if __name__ == "__main__":
    unittest.main()
