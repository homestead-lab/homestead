"""A key change is two writes: the member list naming the new key (keyId),
then the key's Secret. When the second does not happen, a change made here
(unlinking a cluster, leaving, standing alone) is finished later with a new
key; one taken from another member is shown on the Fleet page."""
import base64
import contextlib
import hashlib
import hmac
import json
import secrets
import threading
import time
import unittest
from unittest import mock

from test_fleet import Cluster, wire
from test_fleet_join_lock import second_copy

SECRET_PATH = "/api/v1/namespaces/lab/secrets/homestead-fleet-key"
CONFIG_PATH = "/api/v1/namespaces/lab/configmaps/homestead-fleet"


def fail_key_write(cluster, method="PUT", times=1):
    """The next write(s) of this cluster's key Secret fail with a 500."""
    cluster.kube.kube.fail(SECRET_PATH, 500, method=method, times=times)


def agreed(cluster):
    """The list names the key stored."""
    state, key = cluster.fleet._load(fresh=True)
    return state.get(cluster.fleet.KEY_ID) == cluster.fleet._key_id(key)


def signed_as(fleet, key, sender, target="/api/fleet/hello"):
    """Headers signed with key, for sender."""
    stamp, nonce = str(int(time.time())), secrets.token_hex(12)
    mac = hmac.new(key, fleet._digest(sender, stamp, nonce, "GET", target, "", "admin", b""),
                   hashlib.sha256).digest()
    return {fleet.H_FROM: sender, fleet.H_TIME: stamp, fleet.H_NONCE: nonce, fleet.H_USER: "",
            fleet.H_ROLE: "admin", fleet.H_SIG: base64.urlsafe_b64encode(mac).decode().rstrip("=")}


def stored_list(cluster):
    return json.loads(cluster.kube.get(CONFIG_PATH)["data"]["fleet.json"])


def rewrite_list(cluster, change):
    """Change the stored list as another writer would."""
    found = cluster.kube.get(CONFIG_PATH)
    state = json.loads(found["data"]["fleet.json"])
    change(state)
    found["data"]["fleet.json"] = json.dumps(state)
    cluster.kube.send("PUT", CONFIG_PATH, found)


def aged(cluster, seconds=3600):
    """The list as if saved that long ago."""
    rewrite_list(cluster, lambda s: s.update(keyAt=s["keyAt"] - seconds) if "keyAt" in s else None)


def finish(cluster):
    return getattr(cluster.fleet, "finish_key_change", lambda: "")()


def notice(cluster):
    cluster.fleet._load(fresh=True)
    return cluster.fleet.summary().get("notice")


class KeyChangeResumesTests(unittest.TestCase):
    def setUp(self):
        self.a = Cluster("loft", "https://192.0.2.1:8088", {})
        self.b = Cluster("shed", "https://192.0.2.2:8088", {})
        self.c = Cluster("garage", "https://192.0.2.3:8088", {})
        wire([self.a, self.b, self.c])

    def link_all(self):
        self.a.fleet.join(self.b.url, "admin", "secret")
        self.a.fleet.join(self.c.url, "admin", "secret")
        return self.a.fleet._load(fresh=True)[1]

    def restarted(self, cluster):
        """The same Homestead started again: new module state, the same stored objects."""
        again = second_copy(cluster)
        wire([again, *(c for c in (self.a, self.b, self.c) if c.url != cluster.url)])
        return again

    def unlink_garage_without_its_key_stored(self):
        """The unlink saved, its key not stored. What it raised is checked by
        the test of its message; the others go on to what happens next."""
        group_key = self.link_all()
        fail_key_write(self.a)
        with self.assertRaises(Exception) as caught:
            self.a.fleet.remove("garage")
        self.raised = caught.exception
        return group_key, str(caught.exception)

    def assert_one_key(self, *clusters):
        """Every cluster given holds the key stored on the first."""
        key = clusters[0].fleet._load(fresh=True)[1]
        for cluster in clusters:
            self.assertTrue(agreed(cluster), cluster.name)
            self.assertEqual(key, cluster.fleet._load(fresh=True)[1], cluster.name)
        return key

    # --- a change made here: finished ------------------------------------

    def test_an_unlink_whose_key_was_not_stored_says_it_is_finished_on_its_own(self):
        _, said = self.unlink_garage_without_its_key_stored()
        self.assertIsInstance(self.raised, ValueError)
        self.assertNotIn("Try again", said)
        self.assertNotIn(".).", said)
        self.assertIn("Garage is unlinked", said)
        self.assertIn("on its own within", said)
        self.assertNotIn("garage", {m["handle"] for m in self.a.fleet.members()})

    def test_an_unlink_is_finished_with_a_new_key_the_rest_hold(self):
        group_key, _ = self.unlink_garage_without_its_key_stored()
        again = self.restarted(self.a)
        aged(self.a)
        self.assertTrue(finish(again))
        new_key = self.assert_one_key(self.a, self.b)
        self.assertNotEqual(group_key, new_key)
        self.assertEqual("fleet:shed", self.a.fleet.verify(self.b.fleet.sign("GET", "/api/fleet/hello"),
                                                           "GET", "/api/fleet/hello")["user"])
        # Only the new key is the clusters' key, here and on shed.
        b_id, a_id = self.b.fleet.self_id(), self.a.fleet.self_id()
        with self.assertRaises(PermissionError):
            self.a.fleet.verify(signed_as(self.a.fleet, group_key, b_id), "GET", "/api/fleet/hello")
        with self.assertRaises(PermissionError):
            self.b.fleet.verify(signed_as(self.b.fleet, group_key, a_id), "GET", "/api/fleet/hello")
        # Finished: nothing more to do, and nothing to show.
        aged(self.a)
        self.assertFalse(finish(again))
        self.assertIsNone(notice(again))

    def test_a_change_still_within_its_settle_time_is_left_to_its_own_writer(self):
        group_key, _ = self.unlink_garage_without_its_key_stored()
        self.assertFalse(finish(self.a))
        self.assertEqual(group_key, self.a.fleet._load(fresh=True)[1])
        self.assertIsNone(notice(self.a))
        # The same change, once past its settle time, is finished.
        aged(self.a)
        self.assertTrue(finish(self.a))
        self.assertNotEqual(group_key, self.assert_one_key(self.a, self.b))

    def test_two_copies_finishing_at_once_store_one_key_and_send_it(self):
        group_key, _ = self.unlink_garage_without_its_key_stored()
        copies = [self.restarted(self.a), self.restarted(self.a)]
        aged(self.a)
        start, results = threading.Barrier(2), []

        def run(copy):
            start.wait(5)
            results.append(finish(copy))
        threads = [threading.Thread(target=run, args=(c,)) for c in copies]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(10)
        self.assertEqual(1, sum(1 for r in results if r), results)
        self.assertNotEqual(group_key, self.assert_one_key(self.a, self.b))

    def test_a_key_that_is_stored_meanwhile_is_not_replaced(self):
        self.link_all()
        kept = []

        def fails(key, attempts=4, **_):
            kept.append(key)
            raise RuntimeError("the Secret could not be written")
        real_store = self.a.fleet._store_key
        self.a.fleet._store_key = fails
        with self.assertRaises(Exception):
            self.a.fleet.remove("garage")
        self.a.fleet._store_key = real_store
        again = self.restarted(self.a)
        aged(self.a)
        real = again.fleet._read

        def stored_meanwhile():
            # The first writer's key lands just after the finish read the list.
            found = real()
            self.a.kube.send("PUT", SECRET_PATH, dict(self.a.kube.get(SECRET_PATH), data={
                "key": base64.b64encode(kept[0]).decode()}))
            return found
        again.fleet._read = stored_meanwhile
        self.assertFalse(finish(again))
        self.assertEqual(kept[0], self.a.fleet._load(fresh=True)[1])
        self.assertTrue(agreed(self.a))
        self.assertIn(self.a.fleet._key_id(kept[0]), json.dumps(stored_list(self.a)))

    def test_a_key_another_writer_stores_during_a_finish_is_kept_and_nothing_is_sent(self):
        group_key, _ = self.unlink_garage_without_its_key_stored()
        again = self.restarted(self.a)
        aged(self.a)
        other = secrets.token_bytes(32)
        send, done = again.fleet.ksend, []

        def ksend(method, path, body=None, *args, **kwargs):
            answer = send(method, path, body, *args, **kwargs)
            if path == CONFIG_PATH and method == "PUT" and not done:
                done.append(1)
                send("PUT", SECRET_PATH, dict(self.a.kube.get(SECRET_PATH), data={
                    "key": base64.b64encode(other).decode()}))
            return answer
        again.fleet.ksend = ksend
        outcome = []
        try:
            outcome.append(finish(again))
        except Exception as error:      # noqa: BLE001 - the test reads what came out
            outcome.append(error)
        self.assertIsInstance(outcome[0], RuntimeError)
        self.assertEqual(other, self.a.fleet._load(fresh=True)[1])
        self.assertEqual(group_key, self.b.fleet._load(fresh=True)[1], "a key was sent")

    def test_a_finish_that_keeps_failing_is_shown_and_moves_the_revision_once(self):
        group_key, _ = self.unlink_garage_without_its_key_stored()
        again = self.restarted(self.a)
        revisions = []
        for _ in range(2):
            aged(self.a)
            fail_key_write(self.a)
            outcome = []
            try:
                outcome.append(finish(again))
            except Exception as error:      # noqa: BLE001 - the test reads what came out
                outcome.append(error)
            self.assertIsInstance(outcome[0], RuntimeError)
            revisions.append(stored_list(self.a)["revision"])
            self.assertEqual(group_key, self.a.fleet._load(fresh=True)[1])
        self.assertEqual(revisions[0], revisions[1])
        self.assertEqual(2, stored_list(self.a)["keyMade"]["tries"])
        aged(self.a)
        shown = notice(again)
        self.assertIsNotNone(shown)
        self.assertIn("not finished", shown["title"])
        # When the key can be stored again, it is finished.
        self.assertTrue(finish(again))
        self.assertNotEqual(group_key, self.assert_one_key(self.a, self.b))
        self.assertIsNone(notice(again))

    def test_a_member_not_reached_is_shown_and_sent_the_key_on_a_later_run(self):
        group_key, _ = self.unlink_garage_without_its_key_stored()
        again = self.restarted(self.a)
        aged(self.a)
        self.b.down = True
        self.assertTrue(finish(again))
        shown = notice(again)
        self.assertIsNotNone(shown)
        self.assertIn("Shed", shown["detail"])
        self.assertEqual(group_key, self.b.fleet._load(fresh=True)[1])
        self.assertFalse(finish(again), "a member that does not answer is not counted as told")
        self.b.down = False
        self.assertTrue(finish(again))
        self.assertNotEqual(group_key, self.assert_one_key(self.a, self.b))
        self.assertIsNone(notice(again))
        self.assertNotIn("previous", self.a.kube.get(SECRET_PATH)["data"])
        with self.assertRaises(PermissionError):
            self.b.fleet.verify(signed_as(self.b.fleet, group_key, self.a.fleet.self_id()),
                                "GET", "/api/fleet/hello")

    def untold_shed_moved_to(self, url):
        """Shed was not reached by the finish, and is listed at url since."""
        group_key, _ = self.unlink_garage_without_its_key_stored()
        again = self.restarted(self.a)
        aged(self.a)
        self.b.down = True
        self.assertTrue(finish(again))
        self.b.down = False
        shed = self.b.fleet.self_id()
        rewrite_list(self.a, lambda s: [m.update(url=url) for m in s["members"] if m["id"] == shed])
        sent, wired = [], again.fleet._json

        def json_call(to, method, path, body=None, **kwargs):
            sent.append((to, path))
            return wired(self.b.url if to == url else to, method, path, body, **kwargs)
        again.fleet._json = json_call
        self.assertFalse(finish(again))
        self.assertEqual([], [s for s in sent if s[1] == "/api/fleet/sync"])
        self.assertEqual(group_key, self.b.fleet._load(fresh=True)[1])
        shown = notice(again)
        self.assertIsNotNone(shown)
        self.assertIn("Shed", shown["detail"])
        self.assertNotIn("tries again", shown["detail"])

    def test_a_member_not_reached_and_at_another_address_since_is_not_sent_the_key(self):
        self.untold_shed_moved_to("https://192.0.2.8:8088")

    def test_a_member_not_reached_and_at_a_plain_address_since_is_not_sent_the_key(self):
        self.untold_shed_moved_to("http://192.0.2.2:8088")

    def test_a_member_that_holds_a_newer_list_is_not_counted_as_sent_the_key(self):
        group_key, _ = self.unlink_garage_without_its_key_stored()
        again = self.restarted(self.a)
        aged(self.a)
        rewrite_list(self.b, lambda s: s.update(revision=999))
        self.assertTrue(finish(again))
        shown = notice(again)
        self.assertIsNotNone(shown)
        self.assertIn("Shed", shown["detail"])
        self.assertEqual(group_key, self.b.fleet._load(fresh=True)[1])
        self.assertIn("previous", self.a.kube.get(SECRET_PATH)["data"])

    def test_a_member_that_moved_past_the_list_while_not_reached_stays_shown(self):
        group_key, _ = self.unlink_garage_without_its_key_stored()
        again = self.restarted(self.a)
        aged(self.a)
        self.b.down = True
        self.assertTrue(finish(again))
        self.b.down = False
        rewrite_list(self.b, lambda s: s.update(revision=999))
        self.assertFalse(finish(again))
        self.assertEqual(group_key, self.b.fleet._load(fresh=True)[1])
        self.assertIn("Shed", notice(again)["detail"])
        self.assertIn("previous", self.a.kube.get(SECRET_PATH)["data"])

    def test_a_member_not_reached_is_told_again_with_the_promise_shown(self):
        self.unlink_garage_without_its_key_stored()
        again = self.restarted(self.a)
        aged(self.a)
        self.b.down = True
        self.assertTrue(finish(again))
        self.assertIn("Homestead tries again every ten minutes for Shed.", notice(again)["detail"])

    def test_a_member_whose_answer_was_lost_is_counted_as_told_on_a_later_run(self):
        group_key, _ = self.unlink_garage_without_its_key_stored()
        again = self.restarted(self.a)
        aged(self.a)
        wired = again.fleet._json

        def lost(url, method, path, body=None, **kwargs):
            answer = wired(url, method, path, body, **kwargs)
            if url == self.b.url and path == "/api/fleet/sync":
                raise again.fleet.Unreachable("no answer within 8 seconds")
            return answer
        again.fleet._json = lost
        self.assertTrue(finish(again))
        self.assertIsNotNone(notice(again))
        again.fleet._json = wired
        self.assertTrue(finish(again))
        self.assertNotEqual(group_key, self.assert_one_key(self.a, self.b))
        self.assertIsNone(notice(again))
        self.assertNotIn("previous", self.a.kube.get(SECRET_PATH)["data"])

    def test_a_member_not_reached_and_no_longer_listed_is_dropped(self):
        self.unlink_garage_without_its_key_stored()
        again = self.restarted(self.a)
        aged(self.a)
        self.b.down = True
        self.assertTrue(finish(again))
        shed = self.b.fleet.self_id()
        rewrite_list(self.a, lambda s: s.update(members=[m for m in s["members"] if m["id"] != shed]))
        finish(again)
        self.assertIsNone(notice(again))
        self.assertNotIn("keyUntold", stored_list(self.a))
        self.assertNotIn("previous", self.a.kube.get(SECRET_PATH)["data"])

    def test_a_member_not_reached_is_not_promised_a_retry_when_the_earlier_key_was_not_kept(self):
        self.unlink_garage_without_its_key_stored()
        again = self.restarted(self.a)
        aged(self.a)
        self.b.down = True
        self.a.kube.kube.fail(SECRET_PATH, 500, method="PUT", skip=1)    # the new key stored, the earlier not kept
        with self.assertRaises(Exception):
            again.fleet.finish_key_change()
        shown = notice(again)
        self.assertIsNotNone(shown)
        self.assertIn("Shed", shown["detail"])
        self.assertNotIn("tries again", shown["detail"])

    def test_a_key_taken_from_another_member_is_stored_even_if_another_write_landed_meanwhile(self):
        self.link_all()
        incoming, other = secrets.token_bytes(32), secrets.token_bytes(32)
        send, done = self.b.fleet.ksend, []

        def ksend(method, path, body=None, *args, **kwargs):
            answer = send(method, path, body, *args, **kwargs)
            if path == CONFIG_PATH and method == "PUT" and not done:
                done.append(1)
                send("PUT", SECRET_PATH, dict(self.b.kube.get(SECRET_PATH), data={
                    "key": base64.b64encode(other).decode()}))
            return answer
        self.b.fleet.ksend = ksend
        there = self.b.fleet._load(fresh=True)[0]
        self.b.fleet.adopt({"revision": there["revision"] + 1, "updated": time.time(), "members": there["members"],
                            "key": base64.b64encode(incoming).decode()})
        self.assertEqual(incoming, self.b.fleet._load(fresh=True)[1])
        self.assertTrue(agreed(self.b))

    def test_a_leave_whose_key_was_not_stored_is_finished_and_the_rest_are_told(self):
        group_key = self.link_all()
        fail_key_write(self.a)
        with self.assertRaises(Exception) as caught:
            self.a.fleet.leave()
        self.assertNotIn("Try again", str(caught.exception))
        again = self.restarted(self.a)
        aged(self.a)
        self.assertTrue(finish(again))
        self.assertTrue(agreed(self.a))
        own = self.a.fleet._load(fresh=True)[1]
        self.assertNotEqual(group_key, own)
        for other in (self.b, self.c):
            self.assertEqual({"shed", "garage"}, {m["handle"] for m in other.fleet._load(fresh=True)[0]["members"]})
            self.assertNotIn(other.fleet._load(fresh=True)[1], (group_key, own))
        self.assertEqual(self.b.fleet._load(fresh=True)[1], self.c.fleet._load(fresh=True)[1])

    def test_a_leave_finished_while_a_member_does_not_answer_names_it(self):
        self.link_all()
        fail_key_write(self.a)
        with self.assertRaises(Exception):
            self.a.fleet.leave()
        again = self.restarted(self.a)
        aged(self.a)
        self.c.down = True
        self.assertTrue(finish(again))
        shown = notice(again)
        self.assertIsNotNone(shown)
        self.assertIn("Garage", shown["detail"])
        self.assertNotIn("Shed", shown["detail"])
        self.assertIn("stays until the clusters' key next changes here", shown["detail"])
        # Linking a cluster from here does not change the key: the notice stays.
        yard = Cluster("yard", "https://192.0.2.9:8088", {})
        wire([again, self.b, self.c, yard])
        self.assertTrue(again.fleet.join(yard.url, "admin", "secret")["ok"])
        self.assertIsNotNone(notice(again))

    def test_standing_alone_without_the_key_stored_is_finished_with_a_key_of_its_own(self):
        group_key = self.link_all()
        fail_key_write(self.c)
        self.a.fleet.remove("garage")
        again = self.restarted(self.c)
        aged(self.c)
        self.assertTrue(finish(again))
        self.assertTrue(agreed(self.c))
        self.assertEqual(["garage"], [m["handle"] for m in self.c.fleet.members()])
        self.assertNotEqual(group_key, self.c.fleet._load(fresh=True)[1])

    # --- not finished: shown -------------------------------------------------

    def assert_shown(self, shown, cluster):
        self.assertIsNotNone(shown)
        words = shown["title"] + " " + shown["detail"]
        self.assertIn("Unlink this cluster", words)
        self.assertLess(len(shown["title"]), 90)
        state, key = cluster.fleet._load(fresh=True)
        for secret in (base64.b64encode(key).decode(), key.hex(), state[cluster.fleet.KEY_ID]):
            self.assertNotIn(secret, words)

    def shown_not_finished(self, cluster):
        """What the Fleet page shows once the settle time is past, after a finish that did nothing."""
        aged(cluster)
        with mock.patch.object(cluster.fleet, "KEY_RESUME_AFTER", 0, create=True):
            key = cluster.fleet._load(fresh=True)[1]
            self.assertFalse(finish(cluster))
            self.assertEqual(key, cluster.fleet._load(fresh=True)[1], "no key is made up for it")
            return notice(cluster)

    def test_a_key_change_taken_from_another_member_and_not_stored_is_shown(self):
        old_key = self.link_all()
        fail_key_write(self.b)
        self.a.fleet.remove("garage")
        self.assertEqual(old_key, self.b.fleet._load(fresh=True)[1])
        self.assert_shown(self.shown_not_finished(self.b), self.b)

    def test_a_link_accepted_without_its_key_stored_is_shown(self):
        self.a.fleet._ensure()
        self.b.fleet._ensure()
        fail_key_write(self.b)
        with self.assertRaises(Exception):
            self.a.fleet.join(self.b.url, "admin", "secret")
        self.assert_shown(self.shown_not_finished(self.b), self.b)

    def test_a_list_an_older_release_saved_over_an_unfinished_change_is_shown(self):
        self.unlink_garage_without_its_key_stored()
        # An older release keeps keyId on a change and drops what it does not know.
        rewrite_list(self.a, lambda s: [s.pop(k) for k in [k for k in s if k not in
                                                            ("self", "revision", "updated", "members", "keyId")]])
        self.assert_shown(self.shown_not_finished(self.restarted(self.a)), self.a)

    def test_an_unlink_whose_cluster_is_listed_again_is_shown_not_finished(self):
        group_key, _ = self.unlink_garage_without_its_key_stored()
        # Shed never heard of the unlink: a change of its own brings Garage back here.
        there = self.b.fleet._load(fresh=True)[0]
        self.a.fleet.adopt({"revision": stored_list(self.a)["revision"], "updated": time.time(),
                            "members": [dict(m, name=m["name"] + " 2") if m["handle"] == "shed" else m
                                        for m in there["members"]]})
        self.assertIn("garage", {m["handle"] for m in self.a.fleet._load(fresh=True)[0]["members"]})
        shown = self.shown_not_finished(self.a)
        self.assertIsNotNone(shown)
        self.assertEqual("Garage is listed again after it was unlinked.", shown["title"])
        self.assertIn("Unlink Garage again.", shown["detail"])

    def assert_changed_since(self, shown, name):
        self.assertIsNotNone(shown)
        self.assertEqual("An unlink is not finished, because the linked clusters changed since.", shown["title"])
        self.assertIn(f"Unlink {name} here, then link it again.", shown["detail"])

    def test_a_member_linked_since_the_unlink_started_is_not_sent_the_key(self):
        self.unlink_garage_without_its_key_stored()
        rewrite_list(self.a, lambda s: s["members"].append(
            {"id": "f00d", "handle": "yard", "name": "Yard", "url": "https://192.0.2.9:8088"}))
        self.assert_changed_since(self.shown_not_finished(self.a), "Yard")

    def test_a_member_at_another_address_since_the_unlink_started_is_not_sent_the_key(self):
        group_key, _ = self.unlink_garage_without_its_key_stored()
        shed = self.b.fleet.self_id()
        rewrite_list(self.a, lambda s: [m.update(url="https://192.0.2.8:8088") for m in s["members"] if m["id"] == shed])
        self.assert_changed_since(self.shown_not_finished(self.a), "Shed")
        self.assertEqual(group_key, self.b.fleet._load(fresh=True)[1])

    def test_a_plain_http_member_nobody_agreed_to_is_not_sent_the_key(self):
        self.unlink_garage_without_its_key_stored()
        rewrite_list(self.a, lambda s: s["members"].append(
            {"id": "f00d", "handle": "yard", "name": "Yard", "url": "http://192.0.2.9:8088"}))
        self.assert_changed_since(self.shown_not_finished(self.a), "Yard")

    def test_a_record_of_the_change_that_does_not_match_is_not_acted_on(self):
        self.unlink_garage_without_its_key_stored()
        rewrite_list(self.a, lambda s: s["keyMade"].update(change="leave", rest=[]) if "keyMade" in s
                     else s.update(keyMade={"change": "remove"}))
        self.assert_shown(self.shown_not_finished(self.a), self.a)

    def test_a_list_from_another_member_cannot_bring_its_own_record_of_a_change(self):
        self.link_all()
        there = self.b.fleet._load(fresh=True)[0]
        self.b.fleet.adopt({"revision": 99, "updated": 1, "members": there["members"],
                            "keyMade": {"change": "remove", "removed": "x"}, "keyAt": 1, "keyUntold": ["x"]})
        kept = stored_list(self.b)
        self.assertEqual(99, kept["revision"])
        for field in ("keyMade", "keyUntold"):
            self.assertNotIn(field, kept)
        self.assertNotEqual(1, kept.get("keyAt"))

    def test_a_list_and_key_that_agree_show_nothing(self):
        self.link_all()
        aged(self.a)
        self.assertIsNone(notice(self.a))
        # The same list naming a key that is not stored is shown.
        rewrite_list(self.a, lambda s: s.update(keyId="0" * 16))
        self.assertIsNotNone(notice(self.a))

    def test_a_first_key_not_stored_is_created_again(self):
        self.a.kube.kube.fail(SECRET_PATH.rsplit("/", 1)[0], 500, method="POST")
        with self.assertRaises(Exception):
            self.a.fleet._ensure()
        self.a.fleet._ensure()
        self.assertTrue(agreed(self.a))


class LeaderLoopTests(unittest.TestCase):
    def test_the_platform_loop_finishes_a_key_change_when_an_earlier_tick_fails(self):
        import homestead_storage_server as STORAGE

        class Stop(BaseException):
            pass
        naps = []

        def sleep(seconds):
            naps.append(seconds)
            if len(naps) > 1:
                raise Stop
        clock = mock.Mock(wraps=time)
        clock.sleep = sleep
        finished = mock.Mock(return_value="")
        with mock.patch.object(STORAGE, "time", clock), \
                mock.patch.object(STORAGE.LEADER, "is_leader", return_value=True), \
                mock.patch.object(STORAGE.HOST_LIMITS, "tick", side_effect=RuntimeError("no hosts")), \
                mock.patch.object(STORAGE.FLEET, "finish_key_change", finished, create=True), \
                mock.patch.object(STORAGE, "beat", mock.Mock(), create=True), \
                mock.patch.object(STORAGE, "self_data_activity", contextlib.nullcontext, create=True), \
                self.assertRaises(Stop):
            STORAGE._host_fix_loop()
        finished.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
