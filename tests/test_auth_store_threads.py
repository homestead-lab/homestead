"""The accounts Secret read shared by requests (#978), with real threads, the
real homestead_kube.kget and each request's own read deadline
(homestead_read_deadline), over the shared fake's transport. A held read
behaves as a hung connection does: it answers with the Secret as it was when
it began, or times out on the timeout kget gave it."""
import base64
import json
import socket
import sys
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import homestead_auth as AUTH
import homestead_kube as K
import homestead_read_deadline as DEADLINE
from fakes.kube import Kube

STORE = {"users": {"robin": {"salt": "s", "hash": "h", "ver": 1, "role": "admin"}}, "signing_key": "k" * 40}
PATH = f"/api/v1/namespaces/{AUTH.NS}/secrets/homestead-auth"


def store_secret(store):
    return {"apiVersion": "v1", "kind": "Secret", "type": "Opaque",
            "metadata": {"name": "homestead-auth", "namespace": AUTH.NS},
            "data": {"store.json": base64.b64encode(json.dumps(store).encode()).decode()}}


class Wire:
    """The transport between kget and the fake: counts Secret reads and can
    hold the next one."""
    def __init__(self, kube):
        self.kube, self.reads, self.lock = kube, 0, threading.Lock()
        self.hold_next = False
        self.held = threading.Event()        # a held read has begun
        self.release = threading.Event()     # let it answer
        self.answer = "data"                 # or "timeout" when released

    def __call__(self, method, path, body=None, ctype=None, timeout=15):
        if path.partition("?")[0] != PATH or method != "GET":
            return self.kube(method, path, body, ctype, timeout)
        with self.lock:
            self.reads += 1
            hold, self.hold_next = self.hold_next, False
        snapshot = self.kube(method, path, body, ctype, timeout)   # the Secret as it is now
        if not hold:
            return snapshot
        self.held.set()
        if not self.release.wait(timeout) or self.answer == "timeout":
            raise socket.timeout("timed out")
        return snapshot


class Request(threading.Thread):
    """One request on its own thread, with deadline seconds for its reads
    (None: no deadline, as a background loop)."""
    def __init__(self, call, deadline=15):
        super().__init__(daemon=True)
        self.call, self.deadline = call, deadline
        self.answer = self.took = self.cuts = None

    def run(self):
        if self.deadline is not None:
            DEADLINE.begin(self.deadline)
        started = time.monotonic()
        try:
            self.answer = self.call()
        except Exception as e:              # the answer, whatever it is
            self.answer = e
        finally:
            self.took = time.monotonic() - started
            self.cuts = DEADLINE.cuts()
            DEADLINE.end()


def user(answer):
    return answer["user"] if isinstance(answer, dict) else answer


class SharedReadThreadTests(unittest.TestCase):
    def setUp(self):
        self.saved = (AUTH.kget, AUTH.ksend, AUTH.SECRET_NAME)
        AUTH._store_cache.clear()
        AUTH._store_cache.update(at=0, data=None)
        AUTH._store_read = None
        self.kube = self.enterContext(Kube(store_secret(STORE)))
        self.wire = Wire(self.kube)
        self.enterContext(mock.patch.object(K, "transport", self.wire))
        # Short timeouts, so a held read gives up in test time.
        for module, name in ((K, "OBJECT_TIMEOUT"), (AUTH, "WAIT_IN_REQUEST")):
            self.enterContext(mock.patch.object(module, name, 2))
        for module, name in ((K, "LIST_TIMEOUT"), (AUTH, "WAIT_OTHERWISE")):
            self.enterContext(mock.patch.object(module, name, 3))
        self.enterContext(mock.patch.object(AUTH, "RETRY", 0.3))
        AUTH.kget, AUTH.ksend = K.kget, K.ksend
        AUTH.SECRET_NAME = lambda: "homestead-auth"
        self.token = AUTH.issue_token("robin")
        self.wire.reads = 0
        self.parked = 0
        real_wait = DEADLINE.event_wait

        def counted_wait(event, seconds):
            with self.wire.lock:
                self.parked += 1
            return real_wait(event, seconds)
        self.enterContext(mock.patch.object(DEADLINE, "event_wait", counted_wait))

    def tearDown(self):
        self.wire.release.set()
        AUTH.kget, AUTH.ksend, AUTH.SECRET_NAME = self.saved
        AUTH._store_cache.clear()
        AUTH._store_cache.update(at=0, data=None)
        AUTH._store_read = None

    def cold(self):
        AUTH._store_cache.update(at=0, data=None)

    def expired(self):
        AUTH._store_cache["at"] = time.time() - 11

    def sign_out_elsewhere(self):
        store = json.loads(json.dumps(STORE))
        store["users"]["robin"]["ver"] = 2
        self.kube.add(store_secret(store))

    def start(self, *requests):
        for r in requests:
            r.start()
        return requests

    def wait_parked(self, n):
        end = time.monotonic() + 2
        while self.parked < n and time.monotonic() < end:
            time.sleep(0.005)
        self.assertGreaterEqual(self.parked, n, "the requests never started waiting")

    def held_read(self, deadline=15):
        """A request whose Secret read hangs; returned once the read has begun."""
        self.wire.hold_next = True
        leader, = self.start(Request(lambda: AUTH.verify_token(self.token), deadline))
        self.assertTrue(self.wire.held.wait(2))
        return leader

    def finish(self, *requests):
        for r in requests:
            r.join(10)
            self.assertFalse(r.is_alive(), "a request never answered")

    def test_a_request_after_the_api_recovers_does_not_wait_on_the_hung_read(self):
        self.cold()
        leader = self.held_read()
        waiters = self.start(*(Request(lambda: AUTH.verify_token(self.token)) for _ in range(3)))
        self.wait_parked(3)
        time.sleep(AUTH.RETRY + 0.05)            # the API server is back; the old read still hangs
        late = Request(lambda: AUTH.verify_token(self.token))
        late.run()
        self.assertEqual("robin", user(late.answer))
        self.assertLess(late.took, 0.5)
        self.wire.answer = "timeout"
        self.wire.release.set()
        self.finish(leader, *waiters)
        # The copy the late request read stands in for the hung one.
        self.assertEqual(["robin"] * 4, [user(r.answer) for r in (leader, *waiters)])
        self.assertEqual(2, self.wire.reads)

    def test_a_cold_burst_behind_a_failing_read_is_refused_together_and_in_time(self):
        self.cold()
        leader = self.held_read()
        waiters = self.start(*(Request(lambda: AUTH.verify_token(self.token)) for _ in range(5)))
        self.wait_parked(5)
        self.wire.answer = "timeout"
        self.wire.release.set()
        self.finish(leader, *waiters)
        for r in (leader, *waiters):
            self.assertIsInstance(r.answer, AUTH.StoreUnavailable)
            self.assertLess(r.took, AUTH.WAIT_IN_REQUEST + 0.5)
        self.assertEqual(1, self.wire.reads)

    def test_a_leader_out_of_its_own_time_does_not_fail_the_requests_waiting_on_it(self):
        self.cold()
        leader = self.held_read(deadline=1)       # a page with little time left asks first
        waiters = self.start(*(Request(lambda: AUTH.verify_token(self.token)) for _ in range(4)))
        self.wait_parked(4)
        self.wire.answer = "timeout"              # its 1 s ran out; the API server is fine
        self.wire.release.set()
        self.finish(leader, *waiters)
        self.assertIsInstance(leader.answer, AUTH.StoreUnavailable)
        self.assertEqual(1, leader.cuts)
        self.assertEqual(["robin"] * 4, [user(r.answer) for r in waiters])
        self.assertEqual(2, self.wire.reads)      # the first waiter read again, the rest shared it

    def test_a_leader_out_of_its_own_time_does_not_hold_off_the_next_read(self):
        self.expired()
        self.wire.answer = "timeout"
        leader = self.held_read(deadline=0.1)
        self.finish(leader)
        self.assertEqual("robin", user(leader.answer))   # its own answer: the copy, as before
        self.sign_out_elsewhere()
        self.wire.answer = "data"
        nxt = Request(lambda: AUTH.verify_token(self.token))
        nxt.run()
        self.assertIsNone(nxt.answer, "the next request must read, not take the copy")
        self.assertEqual(2, self.wire.reads)

    def test_a_request_with_little_time_left_stops_waiting_in_time(self):
        self.cold()
        leader = self.held_read(deadline=None)    # a background loop asked first, with 3 s
        short = Request(lambda: AUTH.verify_token(self.token), deadline=0.2)
        short.start()
        short.join(2)
        self.assertFalse(short.is_alive(), "the request waited past its own time")
        self.assertIsInstance(short.answer, AUTH.StoreUnavailable)
        self.assertLess(short.took, 0.6)
        self.assertEqual(1, short.cuts)           # counted as cut short, as its own read would be
        self.wire.release.set()
        self.finish(leader)
        self.assertEqual(1, self.wire.reads)

    def test_a_waiter_stops_at_its_own_bound_behind_a_read_with_no_deadline(self):
        self.cold()
        self.enterContext(mock.patch.object(AUTH, "WAIT_IN_REQUEST", 0.3))
        leader = self.held_read(deadline=None)    # a background loop asked first, with 3 s
        waiter = Request(lambda: AUTH.verify_token(self.token))   # a page request with its full 15 s
        waiter.start()
        waiter.join(2)
        self.assertFalse(waiter.is_alive(), "the request waited past what its own read would get")
        self.assertIsInstance(waiter.answer, AUTH.StoreUnavailable)
        self.assertIn("did not answer in time", waiter.answer.cause)
        self.assertEqual(0, waiter.cuts)          # its deadline did not stop it
        self.assertLess(waiter.took, 1.0)
        self.wire.release.set()
        self.finish(leader)

    def test_a_hung_read_failing_after_a_newer_one_does_not_hold_off_the_next_read(self):
        self.expired()
        leader = self.held_read()
        time.sleep(AUTH.RETRY + 0.05)
        late = Request(lambda: AUTH.verify_token(self.token))
        late.run()                                # the API server is back: a newer read succeeds
        self.assertEqual("robin", user(late.answer))
        self.wire.answer = "timeout"
        self.wire.release.set()                   # the old read fails only now
        self.finish(leader)
        self.expired()
        self.sign_out_elsewhere()
        nxt = Request(lambda: AUTH.verify_token(self.token))
        nxt.run()
        self.assertIsNone(nxt.answer, "the old failure must not stand in for a read")
        self.assertEqual(3, self.wire.reads)

    def test_a_read_begun_before_a_sign_out_here_does_not_bring_the_session_back(self):
        self.expired()
        leader = self.held_read(deadline=None)    # answers with the Secret from before
        AUTH.logout_everywhere("robin")
        self.wire.release.set()
        self.finish(leader)
        self.assertIsNone(AUTH.verify_token(self.token))

    def test_a_read_begun_before_a_sign_out_stays_out_after_a_conflict_empties_the_copy(self):
        # A later save refused as a conflict empties the copy; the older read
        # finishing after that must still not bring the signed-out session back.
        self.expired()
        leader = self.held_read(deadline=None)    # answers with the Secret from before
        AUTH.logout_everywhere("robin")
        self.kube.fail(PATH, 409, method="PUT")
        with self.assertRaises(AUTH.StoreConflict):
            AUTH._save(json.loads(json.dumps(AUTH._load())))
        self.wire.release.set()
        self.finish(leader)
        self.assertIsNone(AUTH.verify_token(self.token))

    def test_forgetting_the_copy_also_forgets_a_read_under_way(self):
        # A restored configuration is read afresh (homestead_settings_helpers).
        self.expired()
        leader = self.held_read(deadline=None)
        self.sign_out_elsewhere()
        AUTH.invalidate()
        now = Request(lambda: AUTH.verify_token(self.token))
        now.run()
        self.assertIsNone(now.answer)
        self.assertLess(now.took, 0.5)
        self.wire.release.set()
        self.finish(leader)
        self.assertIsNone(AUTH.verify_token(self.token))


if __name__ == "__main__":
    unittest.main()
