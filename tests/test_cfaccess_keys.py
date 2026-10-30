"""Cloudflare Access signing keys: refreshed one fetch at a time, never in a request's way."""
import sys
import threading
import unittest
from pathlib import Path
from unittest import mock


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
import homestead_cfaccess as cfaccess


OLD = (101, 3)
NEW = (202, 3)
T0 = 1_000_000.0


class Clock:
    def __init__(self, at):
        self.at = at

    def __call__(self):
        return self.at


class FakeFetch:
    """Stands in for the HTTPS fetch: counts calls, can fail, can hold until released."""

    def __init__(self, keys=None, fail=False, hold=False):
        self.keys, self.fail, self.calls = keys or {}, fail, 0
        self.started, self.release = threading.Event(), threading.Event()
        if not hold:
            self.release.set()

    def __call__(self):
        self.calls += 1
        self.started.set()
        self.release.wait(5)
        if self.fail:
            raise OSError("the key endpoint did not answer")
        return dict(self.keys)


class KeyRefreshTests(unittest.TestCase):
    def setUp(self):
        saved = dict(cfaccess._certs)
        self.assertIsNone(cfaccess._fetching, "no fetch is left running from an earlier test")
        self.fakes = []
        self.addCleanup(self.restore, saved)
        self.clock = Clock(T0)
        patcher = mock.patch.object(cfaccess, "_now", self.clock)
        patcher.start()
        self.addCleanup(patcher.stop)

    def restore(self, saved):
        for fake in self.fakes:
            fake.release.set()
        self.settle()
        cfaccess._fetching = None
        cfaccess._certs.clear()
        cfaccess._certs.update(saved)

    def settle(self):
        """Wait for a fetch started in the background to finish."""
        running = cfaccess._fetching
        if running is not None:
            self.assertTrue(running.wait(5), "the background fetch finished")

    def use(self, fetch):
        if isinstance(fetch, FakeFetch):
            self.fakes.append(fetch)
        patcher = mock.patch.object(cfaccess, "_fetch_keys", fetch)
        patcher.start()
        self.addCleanup(patcher.stop)
        return fetch

    def keys(self, keys, fetched_ago):
        cfaccess._certs.update(keys=dict(keys), at=T0 - fetched_ago, tried=T0 - fetched_ago)

    def in_thread(self, kid, out):
        thread = threading.Thread(target=lambda: out.append(cfaccess._key(kid)), daemon=True)
        thread.start()
        return thread

    def test_a_fresh_cache_answers_without_a_fetch(self):
        fetch = self.use(FakeFetch({"k1": NEW}))
        self.keys({"k1": OLD}, fetched_ago=10)
        self.assertEqual(OLD, cfaccess._key("k1"))
        self.assertEqual(0, fetch.calls)

    def test_a_due_refresh_replaces_the_keys(self):
        fetch = self.use(FakeFetch({"k1": NEW}))
        self.keys({"k1": OLD}, fetched_ago=cfaccess.CERT_TTL + 1)
        self.assertIn(cfaccess._key("k1"), (OLD, NEW))
        self.settle()
        self.assertEqual(NEW, cfaccess._key("k1"))
        self.assertEqual(1, fetch.calls)
        self.clock.at += 61
        self.assertEqual(NEW, cfaccess._key("k1"))
        self.assertEqual(1, fetch.calls, "the next refresh is an hour away")

    def test_after_a_failed_refresh_requests_use_the_keys_they_have_and_do_not_refetch(self):
        fetch = self.use(FakeFetch(fail=True))
        self.keys({"k1": OLD}, fetched_ago=cfaccess.CERT_TTL + 1)
        for _ in range(20):
            self.assertEqual(OLD, cfaccess._key("k1"))
        self.settle()
        self.assertEqual(1, fetch.calls)
        self.clock.at += cfaccess.RETRY_AFTER + 1
        for _ in range(20):
            self.assertEqual(OLD, cfaccess._key("k1"))
        self.settle()
        self.assertEqual(2, fetch.calls, "one retry per interval")

    def test_an_unknown_key_id_refetches_at_most_once_per_interval(self):
        fetch = self.use(FakeFetch({"k1": OLD}))
        self.keys({"k1": OLD}, fetched_ago=cfaccess.RETRY_AFTER + 1)
        for _ in range(20):
            self.assertIsNone(cfaccess._key("junk"))
        self.assertEqual(1, fetch.calls)
        self.clock.at += cfaccess.RETRY_AFTER + 1
        for _ in range(20):
            self.assertIsNone(cfaccess._key("junk"))
        self.assertEqual(2, fetch.calls)

    def test_a_new_key_is_picked_up_by_the_refetch_it_triggers(self):
        fetch = self.use(FakeFetch({"k1": OLD, "k2": NEW}))
        self.keys({"k1": OLD}, fetched_ago=cfaccess.RETRY_AFTER + 1)
        self.assertEqual(NEW, cfaccess._key("k2"))
        self.assertEqual(1, fetch.calls)

    def test_a_known_key_never_waits_on_a_running_fetch(self):
        fetch = self.use(FakeFetch(fail=True, hold=True))
        self.keys({"k1": OLD}, fetched_ago=cfaccess.CERT_TTL + 1)
        first = []
        self.in_thread("k1", first).join(1)
        self.assertEqual([OLD], first, "the request that starts the refresh answers from the cache at once")
        self.assertTrue(fetch.started.wait(2), "the refresh still runs")
        others = []
        threads = [self.in_thread("k1", others) for _ in range(8)]
        for thread in threads:
            thread.join(1)
        self.assertEqual([OLD] * 8, others, "known keys answer while the fetch is still running")
        fetch.release.set()
        self.settle()
        self.assertEqual(1, fetch.calls, "one fetch at a time")

    def test_an_unknown_key_waits_for_the_running_fetch_only_up_to_a_bound(self):
        fetch = self.use(FakeFetch({"k1": OLD, "k2": NEW}, hold=True))
        self.keys({"k1": OLD}, fetched_ago=cfaccess.RETRY_AFTER + 1)
        with mock.patch.object(cfaccess, "FETCH_WAIT", 0.01):
            self.assertIsNone(cfaccess._key("k2"), "gives up waiting and answers from the keys it has")
        self.assertTrue(fetch.started.wait(2))
        fetch.release.set()
        self.settle()
        self.assertEqual(NEW, cfaccess._key("k2"))
        self.assertEqual(1, fetch.calls)

    def test_an_unknown_key_gets_the_result_of_the_fetch_it_waited_for(self):
        fetch = self.use(FakeFetch({"k1": OLD, "k2": NEW}, hold=True))
        self.keys({"k1": OLD}, fetched_ago=cfaccess.RETRY_AFTER + 1)
        first, second = [], []
        fetcher = self.in_thread("k2", first)
        self.assertTrue(fetch.started.wait(2))
        waiter = self.in_thread("k2", second)
        waiter.join(0.2)
        self.assertTrue(waiter.is_alive(), "the second request waits for the running fetch")
        self.assertEqual([], second)
        fetch.release.set()
        fetcher.join(2)
        waiter.join(2)
        self.assertEqual(([NEW], [NEW]), (first, second))
        self.assertEqual(1, fetch.calls)

    def test_a_fetch_that_raises_anything_does_not_block_later_fetches(self):
        fetch = self.use(mock.Mock(side_effect=[SystemExit, {"k1": NEW}]))
        self.keys({"k1": OLD}, fetched_ago=cfaccess.CERT_TTL + 1)
        try:
            cfaccess._key("k1")
        except SystemExit:
            pass
        self.settle()
        self.assertIsNone(cfaccess._fetching)
        self.clock.at += cfaccess.RETRY_AFTER + 1
        cfaccess._key("k1")
        self.settle()
        self.assertEqual(NEW, cfaccess._key("k1"))
        self.assertEqual(2, fetch.call_count)

    def test_a_fetch_with_no_rsa_keys_keeps_the_keys_there_were_and_backs_off(self):
        fetch = self.use(FakeFetch({}))
        self.keys({"k1": OLD}, fetched_ago=cfaccess.CERT_TTL + 1)
        cfaccess._key("k1")
        self.settle()
        for _ in range(20):
            self.assertEqual(OLD, cfaccess._key("k1"))
        self.settle()
        self.assertEqual(1, fetch.calls, "an empty answer counts as a failed fetch")

    def test_with_no_keys_at_all_a_failed_fetch_refuses_and_backs_off(self):
        fetch = self.use(FakeFetch(fail=True))
        cfaccess._certs.update(keys={}, at=0.0, tried=0.0)
        for _ in range(5):
            with self.assertRaises(ValueError):
                cfaccess._key("k1")
        self.assertEqual(1, fetch.calls)
        self.clock.at += cfaccess.RETRY_EMPTY + 1
        with self.assertRaises(ValueError):
            cfaccess._key("k1")
        self.assertEqual(2, fetch.calls)


if __name__ == "__main__":
    unittest.main()
