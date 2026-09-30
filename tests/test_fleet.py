"""Linked clusters: signing, linking, unlinking, and relaying a request."""
import base64
import importlib.util
import io
import json
import socket
import sys
import threading
import time
import unittest
import urllib.error
import urllib.parse
from unittest import mock
from pathlib import Path

SERVER = Path(__file__).resolve().parents[1] / "server"
sys.path.insert(0, str(SERVER))


def fresh_module(name):
    """A separate copy of homestead_fleet: one per pretend cluster."""
    spec = importlib.util.spec_from_file_location(name, SERVER / "homestead_fleet.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class Kube:
    """Just enough of a Kubernetes API for ConfigMaps and Secrets."""

    def __init__(self):
        self.objects = {}

    def get(self, path):
        if path not in self.objects:
            raise urllib.error.HTTPError(path, 404, "missing", {}, None)
        return json.loads(json.dumps(self.objects[path]))

    def send(self, method, path, body=None, **_):
        if method == "DELETE":
            self.objects.pop(path, None)
            return {}
        if method == "POST":
            path = f"{path}/{body['metadata']['name']}"
        body = json.loads(json.dumps(body))
        if "stringData" in body:
            body["data"] = {k: base64.b64encode(v.encode()).decode() for k, v in body.pop("stringData").items()}
        self.objects[path] = body
        return body


class Cluster:
    def __init__(self, name, url, network):
        self.name, self.url, self.kube = name, url, Kube()
        self.fleet = fresh_module(f"fleet_{name}")
        self.fleet.bind(self.kube.get, self.kube.send, "lab", "2.8.300",
                        site=lambda: name.title(), address=lambda: url)
        self.admin_token = f"token-{name}"
        network[url] = self

    # What the Homestead HTTP server would do with each fleet call.
    def serve(self, method, path, body, token, headers):
        target = urllib.parse.urlparse(path)
        raw = json.dumps(body).encode() if body is not None else b""
        who = None
        if headers.get(self.fleet.H_FROM):
            who = self.fleet.verify(headers, method, target.path + (f"?{target.query}" if target.query else ""), raw)
        elif token != self.admin_token:
            raise ValueError("not signed in")
        if target.path == "/api/fleet":
            return self.fleet.summary()
        if target.path == "/api/fleet/hello":
            return self.fleet.hello()
        if target.path == "/api/fleet/state":
            return self.fleet.shared_state()
        if target.path == "/api/fleet/accept":
            return self.fleet.accept(body)
        if target.path == "/api/fleet/sync":
            assert who, "sync must be signed"
            return self.fleet.adopt(body, who["sender"])
        if target.path == "/api/whoami":
            return who
        raise ValueError(f"no route {path}")


def wire(clusters):
    """Every cluster's HTTP calls reach the others' serve()."""
    network = {c.url: c for c in clusters}
    for cluster in clusters:
        def fake_json(url, method, path, body=None, token="", headers=None, timeout=15, _net=network,
                      _fleet=cluster.fleet):
            there = _net.get(url.rstrip("/"))
            if there is None or getattr(there, "down", False):
                raise _fleet.Unreachable(f"no answer from {url}")
            if path == "/api/auth/login":
                if body["password"] != "secret":
                    raise ValueError("wrong password")
                response = type("R", (), {"headers": {"Set-Cookie": f"homestead_session={there.admin_token}; Path=/"}})()
                return {"ok": True}, response
            try:
                return there.serve(method, path, body, token, headers or {}), None
            except PermissionError as error:
                raise ValueError(str(error)) from error
        cluster.fleet._json = fake_json


class SigningTests(unittest.TestCase):
    def setUp(self):
        self.a = Cluster("loft", "http://10.0.0.1:8088", {})
        self.b = Cluster("shed", "http://10.0.0.2:8088", {})
        wire([self.a, self.b])
        self.a.fleet.join(self.b.url, "admin", "secret")

    def test_a_signed_request_says_who_it_is_for(self):
        headers = self.a.fleet.sign("POST", "/api/workloads/restart", b'{"name":"x"}', "robin", "operator")
        who = self.b.fleet.verify(headers, "POST", "/api/workloads/restart", b'{"name":"x"}')
        self.assertEqual("robin@loft", who["user"])
        self.assertEqual("operator", who["role"])

    def test_homestead_itself_is_admin(self):
        headers = self.a.fleet.sign("GET", "/api/move/inventory")
        who = self.b.fleet.verify(headers, "GET", "/api/move/inventory")
        self.assertEqual(("homestead@loft", "admin"), (who["user"], who["role"]))

    def test_an_altered_body_is_refused(self):
        headers = self.a.fleet.sign("POST", "/api/vm/delete", b'{"name":"a"}')
        with self.assertRaises(PermissionError):
            self.b.fleet.verify(headers, "POST", "/api/vm/delete", b'{"name":"b"}')

    def test_another_path_is_refused(self):
        headers = self.a.fleet.sign("POST", "/api/vm/start", b"")
        with self.assertRaises(PermissionError):
            self.b.fleet.verify(headers, "POST", "/api/vm/delete", b"")

    def test_a_request_cannot_be_used_twice(self):
        headers = self.a.fleet.sign("POST", "/api/vm/start", b"")
        self.b.fleet.verify(headers, "POST", "/api/vm/start", b"")
        with self.assertRaises(PermissionError):
            self.b.fleet.verify(headers, "POST", "/api/vm/start", b"")

    def test_an_old_request_is_refused(self):
        headers = self.a.fleet.sign("GET", "/api/nodes")
        headers[self.a.fleet.H_TIME] = str(int(time.time()) - 900)
        with self.assertRaises(PermissionError):
            self.b.fleet.verify(headers, "GET", "/api/nodes")

    def test_an_unknown_role_is_refused(self):
        headers = self.a.fleet.sign("GET", "/api/nodes", b"", "robin", "root")
        with self.assertRaises(PermissionError):
            self.b.fleet.verify(headers, "GET", "/api/nodes")

    def test_an_unlinked_homestead_is_refused(self):
        stranger = Cluster("barn", "http://10.0.0.9:8088", {})
        stranger.fleet._ensure()
        headers = stranger.fleet.sign("GET", "/api/nodes")
        with self.assertRaises(PermissionError):
            self.b.fleet.verify(headers, "GET", "/api/nodes")


class LinkingTests(unittest.TestCase):
    def setUp(self):
        self.a = Cluster("loft", "http://10.0.0.1:8088", {})
        self.b = Cluster("shed", "http://10.0.0.2:8088", {})
        self.c = Cluster("garage", "http://10.0.0.3:8088", {})
        wire([self.a, self.b, self.c])

    def ids(self, cluster):
        return sorted(m["id"] for m in cluster.fleet.members())

    def test_linking_gives_both_the_same_members_and_key(self):
        result = self.a.fleet.join(self.b.url, "admin", "secret")
        self.assertEqual("shed", result["member"]["handle"])
        self.assertEqual(self.ids(self.a), self.ids(self.b))
        self.assertEqual(self.a.fleet._load()[1], self.b.fleet._load()[1])
        self.assertEqual(self.b.url, self.a.fleet.member("shed")["url"])
        self.assertEqual(self.a.url, self.b.fleet.member("loft")["url"])

    def test_the_password_is_not_kept(self):
        self.a.fleet.join(self.b.url, "admin", "secret")
        stored = json.dumps(self.a.kube.objects) + json.dumps(self.b.kube.objects)
        self.assertNotIn("secret", stored.replace("secrets", ""))

    def test_a_wrong_password_links_nothing(self):
        with self.assertRaises(ValueError):
            self.a.fleet.join(self.b.url, "admin", "wrong")
        self.assertEqual(1, len(self.a.fleet.members()))

    def test_a_third_cluster_joins_everyone(self):
        self.a.fleet.join(self.b.url, "admin", "secret")
        self.b.fleet.join(self.c.url, "admin", "secret")
        self.assertEqual(3, len(self.ids(self.a)))
        self.assertEqual(self.ids(self.a), self.ids(self.b))
        self.assertEqual(self.ids(self.a), self.ids(self.c))
        headers = self.c.fleet.sign("GET", "/api/nodes")
        self.assertEqual("homestead@garage", self.a.fleet.verify(headers, "GET", "/api/nodes")["user"])

    def test_an_already_linked_cluster_cannot_be_pulled_into_another_group(self):
        self.b.fleet.join(self.c.url, "admin", "secret")
        with self.assertRaises(ValueError) as caught:
            self.a.fleet.join(self.b.url, "admin", "secret")
        self.assertIn("already linked", str(caught.exception))

    def test_unlinking_changes_the_key_and_the_one_leaving_stands_alone(self):
        self.a.fleet.join(self.b.url, "admin", "secret")
        self.a.fleet.join(self.c.url, "admin", "secret")
        old = self.a.fleet._load()[1]
        result = self.a.fleet.remove(self.c.fleet.self_id())
        self.assertTrue(result["told"])
        self.assertEqual([], result["missed"])
        self.assertEqual(2, len(self.a.fleet.members()))
        self.assertEqual(self.ids(self.a), self.ids(self.b))
        self.assertEqual(1, len(self.c.fleet.members()))
        self.assertNotEqual(old, self.a.fleet._load()[1])
        self.assertEqual(self.a.fleet._load()[1], self.b.fleet._load()[1])
        headers = self.c.fleet.sign("GET", "/api/nodes")
        with self.assertRaises(PermissionError):
            self.a.fleet.verify(headers, "GET", "/api/nodes")

    def test_leaving_keeps_the_others_linked_under_a_key_the_leaver_lacks(self):
        self.a.fleet.join(self.b.url, "admin", "secret")
        self.a.fleet.join(self.c.url, "admin", "secret")
        self.c.fleet.leave()
        self.assertEqual(self.ids(self.a), self.ids(self.b))
        self.assertEqual(2, len(self.ids(self.a)))
        self.assertEqual(self.a.fleet._load()[1], self.b.fleet._load()[1])
        self.assertNotEqual(self.a.fleet._load()[1], self.c.fleet._load()[1])

    def test_a_member_that_missed_a_change_catches_up(self):
        self.a.fleet.join(self.b.url, "admin", "secret")
        self.b.down = True
        self.a.fleet.join(self.c.url, "admin", "secret")
        self.b.down = False
        self.assertEqual(2, len(self.b.fleet.members()))
        self.b.fleet._status.clear()
        view = self.b.fleet.summary()
        self.assertEqual(3, len(view["members"]))
        self.assertTrue(all(m["reachable"] for m in view["members"]))

    def test_the_address_follows_homestead_onto_its_vip(self):
        where = {"url": self.a.url}
        self.a.fleet.bind(self.a.kube.get, self.a.kube.send, "lab", "2.8.300", site=lambda: "Loft",
                          address=lambda: where["url"], earlier=lambda: ("http://10.0.0.1:8088",))
        self.a.fleet.join(self.b.url, "admin", "secret")
        self.assertEqual("http://10.0.0.1:8088", self.b.fleet.member("loft")["url"])
        # Homestead moved onto its VIP: the node's address it was given gives way.
        where["url"] = "http://10.0.0.200:8088"

        self.a.fleet.summary()
        self.assertEqual("http://10.0.0.1:8088", self.b.fleet.member("loft")["url"],
                         "looking at the list changes nothing; the leader's loop does")
        self.assertEqual("http://10.0.0.200:8088", self.a.fleet.follow_address())
        self.assertEqual("http://10.0.0.200:8088", self.a.fleet.summary()["address"])
        self.assertEqual("http://10.0.0.200:8088", self.b.fleet.member("loft")["url"], "the others are told")
        # An address an admin typed is theirs.
        self.a.fleet.set_address("http://loft.lan:8088")
        where["url"] = "http://10.0.0.201:8088"
        self.assertEqual("", self.a.fleet.follow_address())
        self.assertEqual("http://loft.lan:8088", self.a.fleet.summary()["address"])
        # A VIP changed under it: the old VIP gives way to the new one.
        self.a.fleet.set_address("http://10.0.0.200:8088")
        self.assertEqual("http://10.0.0.201:8088", self.a.fleet.follow_address(("http://10.0.0.200:8088",)))

    def test_the_summary_says_who_answers(self):
        self.a.fleet.join(self.b.url, "admin", "secret")
        self.b.down = True
        view = self.a.fleet.summary()
        mine = next(m for m in view["members"] if m["self"])
        theirs = next(m for m in view["members"] if not m["self"])
        self.assertTrue(view["linked"])
        self.assertEqual("Loft", mine["name"])
        self.assertFalse(theirs["reachable"])
        self.assertIn("no answer", theirs["error"])


class RelayTests(unittest.TestCase):
    def setUp(self):
        self.a = Cluster("loft", "http://127.0.0.1:1", {})
        self.b = Cluster("shed", "http://127.0.0.1:2", {})
        wire([self.a, self.b])
        self.a.fleet.join(self.b.url, "admin", "secret")

    def test_the_request_loses_cookies_and_gains_a_signature(self):
        head = self.a.fleet.request_head(
            "POST", "/api/vm/start", "10.0.0.2:8088",
            {"Cookie": "homestead_session=abc", "Cf-Ray": "1", "X-Homestead-Auth": "1",
             "Content-Type": "application/json", "Connection": "keep-alive"},
            b"{}", self.a.fleet.sign("POST", "/api/vm/start", b"{}", "robin", "operator")).decode()
        self.assertIn("POST /api/vm/start HTTP/1.1", head)
        self.assertIn("Host: 10.0.0.2:8088", head)
        self.assertNotIn("homestead_session", head)
        self.assertNotIn("Cf-Ray", head)
        self.assertIn("X-Homestead-Auth: 1", head)
        self.assertIn("Connection: close", head)
        self.assertIn("Content-Length: 2", head)
        self.assertIn("X-Homestead-Fleet-User: robin", head)

    def test_a_console_keeps_its_upgrade(self):
        head = self.a.fleet.request_head("GET", "/api/console?pod=x", "h", {"Upgrade": "websocket",
                                         "Sec-WebSocket-Key": "k"}, b"", {}, websocket=True).decode()
        self.assertIn("Upgrade: websocket", head)
        self.assertIn("Connection: Upgrade", head)
        self.assertIn("Sec-WebSocket-Key: k", head)

    def test_the_answer_cannot_set_this_origins_cookies(self):
        out, upgrade = self.a.fleet.response_head(
            b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nSet-Cookie: homestead_session=theirs\r\n"
            b"Connection: keep-alive", ["homestead_session=ours; Path=/"])
        text = out.decode()
        self.assertFalse(upgrade)
        self.assertNotIn("theirs", text)
        self.assertIn("Set-Cookie: homestead_session=ours; Path=/", text)
        self.assertIn("Connection: close", text)
        self.assertNotIn("keep-alive", text)

    def test_a_request_is_relayed_and_answered(self):
        received = {}
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        port = listener.getsockname()[1]

        def serve():
            conn, _ = listener.accept()
            data = b""
            while b"\r\n\r\n" not in data:
                data += conn.recv(4096)
            head, _, body = data.partition(b"\r\n\r\n")
            received["head"] = head.decode()
            conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: 11\r\n\r\n{\"ok\":true}")
            conn.close()
        thread = threading.Thread(target=serve, daemon=True)
        thread.start()
        self.a.fleet.member("shed")["url"]  # present
        state, key = self.a.fleet._load()
        for m in state["members"]:
            if m["handle"] == "shed":
                m["url"] = f"http://127.0.0.1:{port}"

        class Handler:
            command, path = "GET", "/api/nodes?x=1"
            headers = {"Accept": "application/json", "Cookie": "homestead_session=abc"}
            wfile = io.BytesIO()
            close_connection = False
        handler = Handler()
        self.a.fleet.forward(handler, "shed", b"", "robin", "viewer", ["homestead_session=new; Path=/"])
        thread.join(2)
        listener.close()
        answer = handler.wfile.getvalue().decode()
        self.assertTrue(answer.startswith("HTTP/1.1 200 OK"))
        self.assertTrue(answer.endswith('{"ok":true}'))
        self.assertIn("Set-Cookie: homestead_session=new", answer)
        self.assertTrue(handler.close_connection)
        self.assertIn("GET /api/nodes?x=1 HTTP/1.1", received["head"])
        self.assertIn("X-Homestead-Fleet-Role: viewer", received["head"])
        self.assertNotIn("homestead_session", received["head"])

    def test_a_console_is_piped_both_ways(self):
        received = {}
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        port = listener.getsockname()[1]

        def serve():
            conn, _ = listener.accept()
            data = b""
            while b"\r\n\r\n" not in data:
                data += conn.recv(4096)
            received["head"] = data.decode()
            conn.sendall(b"HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n\r\n")
            typed = b""
            while True:
                chunk = conn.recv(4096)
                if not chunk:
                    break
                typed += chunk
            conn.sendall(b"echo:" + typed)
            conn.close()
        thread = threading.Thread(target=serve, daemon=True)
        thread.start()
        for m in self.a.fleet._load()[0]["members"]:
            if m["handle"] == "shed":
                m["url"] = f"http://127.0.0.1:{port}"

        class Handler:
            command, path = "GET", "/api/vm/console?vm=x&hs_cluster=b"
            headers = {"Upgrade": "websocket", "Connection": "Upgrade", "Sec-WebSocket-Key": "k",
                       "Origin": "https://homestead.example", "Host": "homestead.example"}
            rfile = io.BufferedReader(io.BytesIO(b"ls -la"))
            wfile = io.BytesIO()
        handler = Handler()
        self.a.fleet.forward(handler, "shed", b"", "robin", "operator")
        thread.join(3)
        listener.close()
        answer = handler.wfile.getvalue().decode()
        self.assertIn("101 Switching Protocols", answer)
        self.assertIn("Connection: Upgrade", answer)
        self.assertTrue(answer.endswith("echo:ls -la"))
        self.assertIn("Sec-WebSocket-Key: k", received["head"])
        # The member checks its own origin; the browser's was checked here.
        self.assertIn(f"Origin: http://127.0.0.1:{port}", received["head"])
        self.assertNotIn("homestead.example", received["head"].split("Host:")[0])

    def test_a_console_from_another_site_is_refused_before_relaying(self):
        class Handler:
            command, path = "GET", "/api/console?pod=x"
            headers = {"Upgrade": "websocket", "Origin": "https://evil.example", "Host": "homestead.example"}
            wfile = io.BytesIO()
        with self.assertRaises(PermissionError):
            self.a.fleet.forward(Handler(), "shed", b"")

    def test_a_cluster_that_does_not_answer_raises_before_anything_is_sent(self):
        class Handler:
            command, path, headers = "GET", "/", {}
            wfile = io.BytesIO()
        handler = Handler()
        with self.assertRaises(self.a.fleet.Unreachable):
            self.a.fleet.forward(handler, "shed", b"")
        self.assertEqual(b"", handler.wfile.getvalue())


class MoveClientTests(unittest.TestCase):
    def test_linked_clusters_are_move_sources_without_a_password(self):
        import homestead_move as move
        a = Cluster("loft", "http://10.0.0.1:8088", {})
        b = Cluster("shed", "http://10.0.0.2:8088", {})
        wire([a, b])
        a.fleet.join(b.url, "admin", "secret")
        move.bind(a.kube.get, a.kube.send, "lab", "2.8.300")
        move.FLEET = a.fleet
        try:
            rows = move.list_clusters()
            self.assertEqual(["shed"], [row["name"] for row in rows])
            self.assertTrue(rows[0]["fleet"])
            with self.assertRaises(ValueError):
                move.remove_cluster("shed")
            self.assertNotIn(f"/api/v1/namespaces/lab/configmaps/homestead-clusters", a.kube.objects)
        finally:
            move.FLEET = None


if __name__ == "__main__":
    unittest.main()


class LegacyClusterTests(unittest.TestCase):
    """Clusters added for moves, with a stored account, before linking existed."""

    def setUp(self):
        import homestead_move as move
        self.move = move
        self.a = Cluster("loft", "http://10.0.0.1:8088", {})
        self.b = Cluster("shed", "http://10.0.0.2:8088", {})
        wire([self.a, self.b])
        move.bind(self.a.kube.get, self.a.kube.send, "lab", "2.8.300")
        move.FLEET = self.a.fleet
        self.addCleanup(setattr, move, "FLEET", None)
        move.add_cluster("oldshed", self.b.url, "admin", "secret")

    def test_an_old_cluster_is_listed_for_linking(self):
        rows = self.move.legacy_clusters()
        self.assertEqual(["oldshed"], [row["name"] for row in rows])
        self.assertIsNone(rows[0]["linked_as"])

    def test_linking_it_uses_the_stored_account_then_forgets_the_password(self):
        result = self.move.link_legacy("oldshed")
        self.assertEqual("shed", result["member"]["handle"])
        self.assertEqual(2, len(self.b.fleet.members()))
        self.assertNotIn("/api/v1/namespaces/lab/secrets/homestead-cluster-oldshed", self.a.kube.objects)
        self.assertEqual([], self.move.legacy_clusters())
        # Listed once, as linked; the old name still reaches it for moves that recorded it.
        self.assertEqual(["shed"], [row["name"] for row in self.move.list_clusters()])
        row = self.move._cluster("oldshed")
        self.assertTrue(row["fleet"])
        self.assertEqual(self.b.url, row["url"])

    def test_one_already_linked_under_another_name_is_not_linked_twice(self):
        self.a.fleet.join(self.b.url, "admin", "secret")
        self.assertEqual("shed", self.move.legacy_clusters()[0]["linked_as"]["name"].lower())
        with mock.patch.object(self.a.fleet, "join") as join:
            self.move.link_legacy("oldshed")
        join.assert_not_called()
        self.assertEqual(["shed"], [row["name"] for row in self.move.list_clusters()])
