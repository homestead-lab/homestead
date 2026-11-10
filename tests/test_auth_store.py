"""The account store, when the cluster is slow or the Secret is missing.
Runs on the shared fake API (tests/fakes/kube.py)."""
import base64
import json
import socket
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import homestead_auth as AUTH
import homestead_api_keys as API_KEYS
import homestead_kube as K
from fakes.kube import Kube
from rebind import keep_bound


def store_secret(store):
    return {"apiVersion": "v1", "kind": "Secret", "type": "Opaque",
            "metadata": {"name": "homestead-auth", "namespace": AUTH.NS},
            "data": {"store.json": base64.b64encode(json.dumps(store).encode()).decode()}}


class AccountStoreTests(unittest.TestCase):
    STORE = {"users": {"robin": {"salt": "s", "hash": "h", "ver": 1, "role": "admin"}}, "signing_key": "k" * 40}

    def setUp(self):
        AUTH._store_cache.update(at=0, data=None)
        self.saved_io = (AUTH.kget, AUTH.ksend, AUTH.SECRET_NAME)
        AUTH.kget, AUTH.ksend = K.kget, K.ksend
        AUTH.SECRET_NAME = lambda: "homestead-auth"
        self.path = f"/api/v1/namespaces/{AUTH.NS}/secrets/homestead-auth"
        self.slow = False

    def tearDown(self):
        AUTH.kget, AUTH.ksend, AUTH.SECRET_NAME = self.saved_io
        AUTH._store_cache.update(at=0, data=None)

    def cluster(self, *objects):
        kube = self.enterContext(Kube(*objects))

        def slow_read(method, path, body):
            # An API server that can be slow: reading the accounts Secret times out while slow.
            if self.slow:
                raise socket.timeout("timed out")
        kube.react("GET", self.path, slow_read)
        return kube

    def test_a_slow_cluster_is_never_mistaken_for_a_fresh_install(self):
        kube = self.cluster(store_secret(self.STORE))
        self.slow = True
        with self.assertRaises(AUTH.StoreUnavailable):
            AUTH.needs_setup()
        with self.assertRaises(AUTH.StoreUnavailable):
            AUTH.create_user("intruder", "a-long-password", first_only=True)
        self.assertEqual([], kube.writes, "nothing may be written over the real accounts")

    def test_the_last_good_read_stands_in_while_the_cluster_is_slow(self):
        self.cluster(store_secret(self.STORE))
        self.assertFalse(AUTH.needs_setup())
        token = AUTH.issue_token("robin")
        self.slow = True
        self.assertFalse(AUTH.needs_setup())
        self.assertEqual("robin", AUTH.verify_token(token)["user"])
        with self.assertRaises(PermissionError):
            AUTH.create_user("intruder", "a-long-password", first_only=True)

    def test_a_missing_secret_is_still_a_fresh_install(self):
        self.cluster()
        self.assertTrue(AUTH.needs_setup())


class Clock:
    """The module's clocks, moved by hand; everything else is the real time
    module. time() and monotonic() move together, except when the wall clock
    is stepped (an NTP correction), which monotonic() never sees. They start
    apart (apart = time() - monotonic()), as on a real host, so ordering by
    the wrong one shows: a wall-clock as_of stays ahead of every read's start
    when apart > 0, a wall-clock start stays behind as_of when apart < 0."""
    def __init__(self, now=1_000_000.0, apart=0.0):
        self.now, self.stepped = now, apart

    def time(self):
        return self.now

    def monotonic(self):
        return self.now - self.stepped

    def step_wall_clock(self, seconds):
        self.now += seconds
        self.stepped += seconds

    def __getattr__(self, name):
        return getattr(time, name)


KEY_ID, KEY_SECRET = "0123456789ab", "s" * 43


def with_key(store):
    """The store with one API key of robin's (built here, never a real key)."""
    store = json.loads(json.dumps(store))
    store["api_keys"] = {KEY_ID: {"name": "Home Assistant", "owner": "robin", "scopes": ["read"], "networks": [],
                                  "created": 0, "expires": int(time.time()) + 86400,
                                  "hash": API_KEYS._digest(KEY_SECRET), "mcp": False}}
    return store


def reset_store():
    AUTH._store_cache.clear()
    AUTH._store_cache.update(at=0, data=None)
    AUTH._store_read = None


class StoreReadTests(unittest.TestCase):
    APART = 995_000.0                   # the wall clock far ahead, as on a real host
    """How often the accounts Secret is read when the copy has expired (#978):
    once for a burst of requests, and while the cluster is failing at most once
    per AUTH.RETRY seconds - never more refusals than before, never fewer."""
    STORE = with_key(AccountStoreTests.STORE)
    KEY = API_KEYS.PREFIX + KEY_ID + "_" + KEY_SECRET

    def setUp(self):
        reset_store()
        keep_bound(self, API_KEYS)
        API_KEYS.bind(self.enterContext(tempfile.TemporaryDirectory()))
        self.saved = (AUTH.kget, AUTH.ksend, AUTH.SECRET_NAME, AUTH.time)
        self.kube = self.enterContext(Kube(store_secret(self.STORE)))
        self.path = f"/api/v1/namespaces/{AUTH.NS}/secrets/homestead-auth"
        self.reads, self.failing, self.gate = [], False, None
        self.reads_lock = threading.Lock()

        def kget(path, timeout=None):
            if path == self.path:
                with self.reads_lock:
                    self.reads.append(self.clock.now)
                if self.gate:
                    self.gate()
                if self.failing:
                    raise socket.timeout("timed out")
            return K.kget(path, timeout)
        AUTH.kget, AUTH.ksend = kget, K.ksend
        AUTH.SECRET_NAME = lambda: "homestead-auth"
        self.clock = Clock(apart=self.APART)
        AUTH.time = self.clock
        self.token = AUTH.issue_token("robin")      # the first read, at the start
        self.reads.clear()

    def tearDown(self):
        AUTH.kget, AUTH.ksend, AUTH.SECRET_NAME, AUTH.time = self.saved
        reset_store()

    def burst(self, n, call):
        """n requests at once; the first read is held until n reads have
        started or half a second has passed, so requests that would each read
        the Secret are all seen doing it."""
        started = threading.Event()

        def gate():
            with self.reads_lock:
                if len(self.reads) >= n:
                    started.set()
            started.wait(0.5)
        self.gate = gate
        answers = [None] * n

        def one(i):
            try:
                answers[i] = call()
            except Exception as e:      # the answer, whatever it is
                answers[i] = e
        threads = [threading.Thread(target=one, args=(i,)) for i in range(n)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(5)
        self.gate = None
        return answers

    def bump_version_elsewhere(self):
        """Another replica signs robin out everywhere."""
        store = json.loads(json.dumps(self.STORE))
        store["users"]["robin"]["ver"] = 2
        self.kube.add(store_secret(store))

    def revoke_key_elsewhere(self):
        store = json.loads(json.dumps(self.STORE))
        store["api_keys"] = {}
        self.kube.add(store_secret(store))

    def test_a_burst_after_expiry_reads_the_secret_once(self):
        self.clock.now += 11
        answers = self.burst(20, lambda: AUTH.verify_token(self.token))
        self.assertEqual(1, len(self.reads))
        self.assertEqual(["robin"] * 20, [a and a["user"] for a in answers])

    def test_a_burst_while_the_cluster_fails_reads_once_and_uses_the_last_good_copy(self):
        self.clock.now += 11
        self.failing = True
        answers = self.burst(20, lambda: AUTH.verify_token(self.token))
        self.assertEqual(1, len(self.reads))
        self.assertEqual(["robin"] * 20, [a and a["user"] for a in answers])

    def test_a_failing_cluster_is_asked_once_per_retry_interval(self):
        self.clock.now += 11
        self.failing = True
        for _ in range(20):
            self.assertEqual("robin", AUTH.verify_token(self.token)["user"])
            self.clock.now += 0.1
        self.assertEqual(1, len(self.reads))
        self.clock.now += AUTH.RETRY
        self.assertEqual("robin", AUTH.verify_token(self.token)["user"])
        self.assertEqual(2, len(self.reads))

    def test_the_cluster_is_asked_again_as_soon_as_the_copy_is_too_old_to_use(self):
        # Nobody is turned away without the cluster being asked: the pause
        # after a failure only stands in for a read while the copy may be used.
        self.clock.now += AUTH.MAX_STALE_SECONDS - 1
        self.failing = True
        self.assertEqual("robin", AUTH.verify_token(self.token)["user"])
        self.clock.now += 2                     # copy now too old; still inside RETRY
        self.failing = False
        self.assertEqual("robin", AUTH.verify_token(self.token)["user"])
        self.assertEqual(2, len(self.reads))

    def test_a_copy_too_old_to_use_is_refused_as_before(self):
        self.clock.now += AUTH.MAX_STALE_SECONDS + 1
        self.failing = True
        with self.assertRaises(AUTH.StoreUnavailable):
            AUTH.verify_token(self.token)

    def test_a_sign_out_elsewhere_still_stops_the_session_within_ten_seconds(self):
        self.bump_version_elsewhere()
        self.clock.now += 10
        self.assertIsNone(AUTH.verify_token(self.token))

    def test_a_sign_out_elsewhere_is_seen_once_the_cluster_answers_again(self):
        self.clock.now += 11
        self.failing = True
        self.assertEqual("robin", AUTH.verify_token(self.token)["user"])
        self.bump_version_elsewhere()
        self.failing = False
        self.clock.now += AUTH.RETRY
        self.assertIsNone(AUTH.verify_token(self.token))

    def test_a_forced_read_is_never_skipped_after_a_failure(self):
        self.clock.now += 11
        self.failing = True
        AUTH.verify_token(self.token)
        with self.assertRaises(AUTH.StoreUnavailable):
            AUTH.verify_token(self.token, force=True)   # a write: fresh or nothing
        self.assertEqual(2, len(self.reads))
        self.failing = False
        self.bump_version_elsewhere()
        self.assertIsNone(AUTH.verify_token(self.token, force=True))

    def test_a_forced_read_does_not_wait_on_a_read_already_under_way(self):
        # A write must see the Secret as it is after it asked, not as a read
        # that began earlier found it.
        self.clock.now += 11
        held, release = threading.Event(), threading.Event()

        def gate():
            if not held.is_set():
                held.set()
                release.wait(5)
        self.gate = gate
        first = threading.Thread(target=AUTH.verify_token, args=(self.token,))
        first.start()
        self.assertTrue(held.wait(5))
        self.bump_version_elsewhere()
        self.assertIsNone(AUTH.verify_token(self.token, force=True))
        release.set()
        first.join(5)
        self.assertEqual(2, len(self.reads))

    def test_an_api_key_check_does_not_take_a_read_that_began_too_long_ago(self):
        # API keys ask for a copy at most 2 s old (homestead_api_keys.FRESH_FOR).
        self.clock.now += 11
        held, release = threading.Event(), threading.Event()

        def gate():
            if not held.is_set():
                held.set()
                release.wait(5)
        self.gate = gate
        first = threading.Thread(target=AUTH.verify_token, args=(self.token,))
        first.start()
        self.assertTrue(held.wait(5))
        self.clock.now += API_KEYS.FRESH_FOR + 1
        self.revoke_key_elsewhere()
        with self.assertRaisesRegex(PermissionError, "not valid"):
            API_KEYS.verify(self.KEY, "192.0.2.5")
        release.set()
        first.join(5)
        self.assertEqual(2, len(self.reads))

    def test_an_api_key_check_asks_the_cluster_even_right_after_a_failed_read(self):
        # A failed read lets ordinary requests use the copy for RETRY; an API key
        # wants its copy within 2 s, so it asks the cluster as before.
        self.clock.now += 11
        self.failing = True
        self.assertEqual("robin", AUTH.verify_token(self.token)["user"])
        self.failing = False
        self.revoke_key_elsewhere()
        self.clock.now += 0.5
        with self.assertRaisesRegex(PermissionError, "not valid"):
            API_KEYS.verify(self.KEY, "192.0.2.5")
        self.assertEqual(2, len(self.reads))

    def test_a_wall_clock_stepped_back_does_not_stop_reads_filling_the_copy(self):
        # Reads and forgetting are ordered on a clock that never goes back
        # (#1302): after an NTP correction, one read fills the copy again
        # rather than every request reading the Secret itself.
        AUTH.invalidate()                       # a restored configuration
        self.clock.step_wall_clock(-600)
        for _ in range(5):
            self.assertEqual("robin", AUTH.verify_token(self.token)["user"])
        self.assertEqual(1, len(self.reads))

    def test_a_wall_clock_stepped_back_does_not_hold_off_a_conflict_reread(self):
        # A save refused because another replica changed the accounts empties
        # the copy; the reads after it fill it again, clock step or not.
        self.kube.fail(self.path, 409, method="PUT")
        with self.assertRaises(AUTH.StoreConflict):
            AUTH.logout_everywhere("robin")
        self.reads.clear()
        self.clock.step_wall_clock(-600)
        for _ in range(5):
            self.assertEqual("robin", AUTH.verify_token(self.token)["user"])
        self.assertEqual(1, len(self.reads))


    def test_reads_after_a_save_here_fill_the_copy_again(self):
        # A save here is put in order on the same clock as the reads after it.
        AUTH.logout_everywhere("robin")
        self.token = AUTH.issue_token("robin")
        self.clock.now += 11                    # the saved copy has expired
        self.reads.clear()
        for _ in range(5):
            self.assertEqual("robin", AUTH.verify_token(self.token)["user"])
        self.assertEqual(1, len(self.reads))


class StoreReadTestsMonotonicAhead(StoreReadTests):
    """Every StoreReadTests case again with the monotonic clock ahead of the
    wall clock, so a read's start taken from the wall clock shows too."""
    APART = -995_000.0


if __name__ == "__main__":
    unittest.main()
