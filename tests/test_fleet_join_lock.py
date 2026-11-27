"""Linking a cluster asks it to accept over the network (#1546). That wait
held the lock every Fleet read and change takes, so one slow cluster held
them all. Asked outside the lock, what was decided before asking is checked
again when the link is saved; when it no longer holds, the cluster that
accepted is told to stand alone again."""
import base64
import http.server
import json
import secrets
import threading
import time
import unittest
from unittest import mock

from test_fleet import Cluster, wire

# Generous: the gate runs eight shards at once.
WAIT = 10


class SlowAcceptTests(unittest.TestCase):
    """The new cluster answers the accept over a real socket, only when the test says."""

    def setUp(self):
        self.a = Cluster("loft", "https://192.0.2.1:8088", {})
        self.b = Cluster("shed", "https://192.0.2.2:8088", {})
        self.c = Cluster("garage", "https://192.0.2.3:8088", {})
        real_json = self.a.fleet._json
        wire([self.a, self.b, self.c])
        self.arrived, self.release = threading.Event(), threading.Event()
        self.addCleanup(self.release.set)
        peer, arrived, release = self.b, self.arrived, self.release

        class Accept(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                arrived.set()
                release.wait(WAIT * 3)
                token = self.headers.get("Cookie", "").split("homestead_session=", 1)[-1]
                out = json.dumps(peer.serve("POST", self.path, body, token, {})).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(out)))
                self.end_headers()
                self.wfile.write(out)

            def log_message(self, *args):
                pass
        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Accept)
        server.daemon_threads = True
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        slow = f"http://127.0.0.1:{server.server_address[1]}"
        wired = self.a.fleet._json

        def json_call(url, method, path, body=None, token="", headers=None, timeout=15):
            if path == "/api/fleet/accept" and url == peer.url:
                return real_json(slow, method, path, body, token=token, headers=headers, timeout=timeout)
            return wired(url, method, path, body, token=token, headers=headers, timeout=timeout)
        self.a.fleet._json = json_call

    def link_slowly(self, outcome):
        def link():
            try:
                outcome["join"] = self.a.fleet.join(self.b.url, "admin", "secret")
            except Exception as error:  # reported by the assertions
                outcome["join"] = error
        linking = threading.Thread(target=link, daemon=True)
        linking.start()
        self.assertTrue(self.arrived.wait(WAIT), "the accept never reached the other cluster")
        return linking

    def test_a_fleet_read_does_not_wait_for_a_slow_cluster_to_accept(self):
        outcome = {}
        linking = self.link_slowly(outcome)
        read = threading.Event()
        threading.Thread(target=lambda: (self.a.fleet.hello(), self.a.fleet.members(), read.set()),
                         daemon=True).start()
        waited = not read.wait(WAIT)
        self.release.set()
        linking.join(WAIT)
        self.assertFalse(waited, "a Fleet read waited for the other cluster's answer to the accept")
        self.assertTrue(outcome["join"]["ok"], outcome["join"])
        self.assertEqual({"loft", "shed"}, {m["handle"] for m in self.a.fleet.members()})
        self.assertEqual({"loft", "shed"}, {m["handle"] for m in self.b.fleet.members()})

    def test_another_link_goes_ahead_while_a_slow_cluster_is_asked_and_both_stay(self):
        outcome = {}
        linking = self.link_slowly(outcome)
        done = threading.Event()
        threading.Thread(target=lambda: (outcome.update(other=self.a.fleet.join(self.c.url, "admin", "secret")),
                                         done.set()), daemon=True).start()
        waited = not done.wait(WAIT)
        self.release.set()
        linking.join(WAIT)
        done.wait(WAIT)
        self.assertFalse(waited, "the second link waited for the first cluster's answer")
        self.assertTrue(outcome["join"]["ok"], outcome["join"])
        everyone = {"loft", "shed", "garage"}
        for cluster in (self.a, self.b, self.c):
            self.assertEqual(everyone, {m["handle"] for m in cluster.fleet._load(fresh=True)[0]["members"]}, cluster.name)
        self.assertEqual(self.a.fleet._load()[1], self.b.fleet._load()[1])
        self.assertEqual(self.a.fleet._load()[1], self.c.fleet._load()[1])


def second_copy(cluster):
    """Another copy of the same Homestead: same Kubernetes API, its own lock and cache."""
    other = Cluster(cluster.name, cluster.url, {})
    other.kube = cluster.kube
    other.fleet.bind(cluster.kube.get, cluster.kube.send, "lab", "2.8.300",
                     site=lambda: cluster.name.title(), address=lambda: cluster.url)
    other.fleet._json = cluster.fleet._json
    return other


class JoinRaceTests(unittest.TestCase):
    """A change saved while the new cluster was asked."""

    def setUp(self):
        self.a = Cluster("loft", "https://192.0.2.1:8088", {})
        self.b = Cluster("shed", "https://192.0.2.2:8088", {})
        self.c = Cluster("garage", "https://192.0.2.3:8088", {})
        wire([self.a, self.b, self.c])
        self.a2 = second_copy(self.a)

    def meanwhile(self, change):
        """Run change() on the other copy once b has accepted, before the link is saved here."""
        wired = self.a.fleet._json

        def json_call(url, method, path, body=None, token="", headers=None, timeout=15):
            answer = wired(url, method, path, body, token=token, headers=headers, timeout=timeout)
            if path == "/api/fleet/accept":
                change()
            return answer
        self.a.fleet._json = json_call

    def stored_handles(self):
        return {m["handle"] for m in self.a.fleet._load(fresh=True)[0]["members"]}

    def test_a_leave_meanwhile_undoes_the_link_on_both_sides(self):
        self.a.fleet.join(self.c.url, "admin", "secret")
        self.meanwhile(self.a2.fleet.leave)
        with self.assertRaises(ValueError) as caught:
            self.a.fleet.join(self.b.url, "admin", "secret")
        self.assertIn("told to stand alone again", str(caught.exception))
        self.assertEqual({"loft"}, self.stored_handles())
        self.assertEqual({"shed"}, {m["handle"] for m in self.b.fleet.members()})
        self.assertNotEqual(self.a.fleet._load(fresh=True)[1], self.b.fleet._load(fresh=True)[1])

    def test_a_member_removed_meanwhile_undoes_the_link(self):
        self.a.fleet.join(self.c.url, "admin", "secret")
        self.meanwhile(lambda: self.a2.fleet.remove("garage"))
        with self.assertRaises(ValueError) as caught:
            self.a.fleet.join(self.b.url, "admin", "secret")
        self.assertIn("key changed", str(caught.exception))
        self.assertEqual({"loft"}, self.stored_handles())
        self.assertEqual({"shed"}, {m["handle"] for m in self.b.fleet.members()})

    def test_a_member_linked_meanwhile_keeps_both_links(self):
        # The same key: the new member hears the list as saved (as tests/test_fleet_writes.py pins).
        self.meanwhile(lambda: self.a2.fleet.join(self.c.url, "admin", "secret"))
        self.assertTrue(self.a.fleet.join(self.b.url, "admin", "secret")["ok"])
        both = {"loft", "garage", "shed"}
        self.assertEqual(both, self.stored_handles())
        self.assertEqual(both, {m["handle"] for m in self.b.fleet.members()})
        self.assertEqual(both, {m["handle"] for m in self.c.fleet.members()})
        self.assertEqual(self.a.fleet._load(fresh=True)[1], self.b.fleet._load(fresh=True)[1])

    def test_a_cluster_that_cannot_be_told_is_named_with_what_to_do(self):
        self.a.fleet.join(self.c.url, "admin", "secret")

        def leave_and_lose_b():
            self.a2.fleet.leave()
            self.b.down = True
        self.meanwhile(leave_and_lose_b)
        with self.assertRaises(ValueError) as caught:
            self.a.fleet.join(self.b.url, "admin", "secret")
        said = str(caught.exception)
        self.assertIn("Shed", said)
        self.assertIn("could not be told", said)
        self.assertIn("unlink it there", said)
        self.assertEqual({"loft"}, self.stored_handles())

    def linked_from_elsewhere(self, store_key=True):
        """A third Homestead links this one (it stands alone) on the other copy.
        store_key False: the list is saved, its key not yet (the second of the two writes)."""
        def accept():
            if not store_key:
                self.a2.fleet._store_key = lambda key, attempts=4, **_: True
            self.a2.fleet.accept({"key": base64.b64encode(secrets.token_bytes(32)).decode(), "revision": 7,
                                  "url": self.a.url, "members": [{"id": "f00d", "handle": "yard", "name": "Yard",
                                                                  "url": "https://192.0.2.9:8088"}]})
        return accept

    def test_a_key_saved_meanwhile_but_not_yet_stored_undoes_the_link(self):
        self.meanwhile(self.linked_from_elsewhere(store_key=False))
        with self.assertRaises(ValueError) as caught:
            self.a.fleet.join(self.b.url, "admin", "secret")
        self.assertIn("key changed", str(caught.exception))
        self.assertIn("told to stand alone again", str(caught.exception))
        self.assertEqual({"loft", "yard"}, self.stored_handles())
        self.assertEqual({"shed"}, {m["handle"] for m in self.b.fleet.members()})

    def test_an_accept_from_a_third_cluster_meanwhile_undoes_the_link(self):
        self.meanwhile(self.linked_from_elsewhere())
        with self.assertRaises(ValueError) as caught:
            self.a.fleet.join(self.b.url, "admin", "secret")
        self.assertIn("key changed", str(caught.exception))
        self.assertEqual({"loft", "yard"}, self.stored_handles())
        self.assertEqual({"shed"}, {m["handle"] for m in self.b.fleet.members()})

    def test_the_address_linked_meanwhile_undoes_the_link(self):
        self.a.fleet.join(self.c.url, "admin", "secret")
        moved = lambda state, _key: (dict(state, revision=state["revision"] + 1, members=[
            dict(m, url=self.b.url) if m["handle"] == "garage" else m for m in state["members"]]), None)
        self.meanwhile(lambda: self.a2.fleet._change(moved))     # garage's new address, saved on the other copy
        with self.assertRaises(ValueError) as caught:
            self.a.fleet.join(self.b.url, "admin", "secret")
        self.assertIn("that address was linked", str(caught.exception))
        self.assertEqual({"loft", "garage"}, self.stored_handles())
        self.assertEqual({"shed"}, {m["handle"] for m in self.b.fleet.members()})

    def test_the_list_removed_meanwhile_saves_nothing_and_says_to_unlink_there(self):
        path = "/api/v1/namespaces/lab/configmaps/" + self.a.fleet.CONFIG
        self.meanwhile(lambda: self.a.kube.send("DELETE", path))
        with self.assertRaises(ValueError) as caught:
            self.a.fleet.join(self.b.url, "admin", "secret")
        said = str(caught.exception)
        self.assertIn("the list of linked clusters was removed", said)
        self.assertIn("unlink it there", said)
        self.assertEqual({}, self.a.fleet._read()[0], "a list was saved after it was removed")

    def test_a_key_change_that_never_stored_its_key_still_links_with_the_key_the_members_hold(self):
        # A remove saved its list (naming a new key) and then failed to store
        # that key: the list and the Secret disagree for good, and the members
        # still hold the stored key.
        self.a.fleet.join(self.c.url, "admin", "secret")
        held = self.a.fleet._load(fresh=True)[1]

        def fails(key, attempts=4):
            raise RuntimeError("the Secret could not be written")
        self.a2.fleet._store_key = fails
        with self.assertRaises(ValueError):
            self.a2.fleet.remove("garage")
        state, key = self.a.fleet._load(fresh=True)
        self.assertEqual(held, key)
        self.assertNotEqual(self.a.fleet._key_id(key), state[self.a.fleet.KEY_ID])
        with mock.patch.object(self.a.fleet, "KEY_SETTLE", 0.3):
            result = self.a.fleet.join(self.b.url, "admin", "secret")
        self.assertTrue(result["ok"])
        self.assertEqual({"loft", "shed"}, self.stored_handles())
        self.assertEqual(held, self.b.fleet._load(fresh=True)[1])
        headers = self.b.fleet.sign("GET", "/api/fleet/hello")
        self.assertEqual("fleet:shed", self.a.fleet.verify(headers, "GET", "/api/fleet/hello")["user"])

    def test_a_key_change_whose_key_is_stored_while_the_link_starts_links_with_the_new_key(self):
        # The other copy saved its list (naming its new key) just before this
        # join read it, and stores the key a moment later.
        self.a.fleet._ensure()      # this Homestead's own list and key, stored
        deferred = []
        self.a2.fleet._store_key = lambda key, attempts=4, **_: deferred.append(key) or True
        self.linked_from_elsewhere()()
        written = second_copy(self.a).fleet._store_key
        clock = mock.Mock(wraps=time)

        def pause(seconds):
            while deferred:
                written(deferred.pop())     # the other copy's second write lands
            time.sleep(seconds)
        clock.sleep = pause
        with mock.patch.object(self.a.fleet, "time", clock):
            result = self.a.fleet.join(self.b.url, "admin", "secret")
        self.assertTrue(result["ok"])
        self.assertEqual({"loft", "yard", "shed"}, self.stored_handles())
        state, key = self.a.fleet._load(fresh=True)
        self.assertEqual(self.a.fleet._key_id(key), state[self.a.fleet.KEY_ID])
        self.assertEqual(key, self.b.fleet._load(fresh=True)[1], "the new member holds another key")

    def test_a_cluster_already_past_the_undo_is_told_to_be_unlinked_there(self):
        self.a.fleet.join(self.c.url, "admin", "secret")

        def leave_while_b_moves_on():
            self.a2.fleet.leave()
            there = self.b.fleet._load(fresh=True)[0]
            self.b.fleet.adopt({"revision": 50, "updated": 1, "members": there["members"]})
        self.meanwhile(leave_while_b_moves_on)
        with self.assertRaises(ValueError) as caught:
            self.a.fleet.join(self.b.url, "admin", "secret")
        said = str(caught.exception)
        self.assertIn("did not stand alone", said)
        self.assertIn("unlink it there", said)
        self.assertEqual({"loft"}, self.stored_handles())

    def test_a_rename_meanwhile_keeps_the_link(self):
        self.a.fleet.join(self.c.url, "admin", "secret")
        renamed = lambda: "Loft Two"
        self.a2.fleet._site = renamed

        def rename():
            self.a2.fleet.refresh_name()
        self.meanwhile(rename)
        result = self.a.fleet.join(self.b.url, "admin", "secret")
        self.assertTrue(result["ok"])
        self.assertEqual({"loft", "garage", "shed"}, self.stored_handles())
        self.assertEqual({"loft", "garage", "shed"}, {m["handle"] for m in self.b.fleet.members()})
        self.assertEqual(self.a.fleet._load(fresh=True)[1], self.b.fleet._load(fresh=True)[1])


if __name__ == "__main__":
    unittest.main()
