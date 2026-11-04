"""Near the end of a run: no test reached the real network (#TBD-tests-refuse-real-network).

tests/network_guard.py makes every lookup and connect to anything but this machine
fail at once, as a failed lookup does, and records the test and the call. On a
runner whose resolver is slow, each such call used to wait seconds and could fail
a test timed against it (#1123, #1135). scripts/run_tests.py installs the guard and
reports the tests that reached the network (with network_guard.ENFORCE, a file not
in network_offenders.txt fails the run);
this file installs it too, for a plain `unittest discover` run, and checks the same.

network_offenders.txt is today's list, with what each file reaches: it may only
shrink (CEILING). To take a file off it, fake the client the code really uses: the
shared Kubernetes fake (tests/fakes/kube.py), no_cluster() from it for a test that needs
no cluster, or a patch of the module's own client.
"""
import re
import socket
import sys
import threading
import time
import types
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import network_guard as NETWORK

NETWORK.install()

CEILING = 0
# The list's files, digested: a swap (one off, another on) keeps the count but not this.
# "A listed file no longer offends" is not checked: which tests reach the network depends
# on what ran before them, so a file can look clean in one order and offend in another.
DIGEST = "e3b0c44298fc1c14"


class NetworkRefused(unittest.TestCase):
    def test_no_test_outside_the_list_reached_the_network(self):
        NETWORK.settle_once()        # a leaked thread's late call lands before the check, not after it
        bad = NETWORK.offenders()
        if not NETWORK.ENFORCE:
            self.skipTest(f"advisory: {len(bad)} test(s) outside network_offenders.txt reached the network "
                          "(the run's 'network:' lines name them)")
        self.assertEqual({}, {test: calls[:2] for test, calls in bad.items()},
                         "these reached the real network: fake the client they use (tests/network_guard.py)")

    def test_the_list_only_shrinks(self):
        listed = NETWORK.allowed_files()
        self.assertLessEqual(len(listed), CEILING, "network_offenders.txt may only shrink: fix the test, do not add it")
        if len(listed) < CEILING:
            self.fail(f"network_offenders.txt has {len(listed)} files: lower CEILING to match")
        here = Path(__file__).resolve().parent
        self.assertEqual([], sorted(name for name in listed if not (here / f"{name}.py").exists()),
                         "these are not test files any more: take them off network_offenders.txt")

    def test_the_list_is_the_one_counted(self):
        import hashlib
        digest = hashlib.sha256("\n".join(sorted(NETWORK.allowed_files())).encode()).hexdigest()[:16]
        self.assertEqual(DIGEST, digest, "network_offenders.txt changed: only removals are allowed; "
                                         "after one, set DIGEST to this and lower CEILING")

    def test_an_ip_literal_is_not_looked_up_and_its_connect_is_refused(self):
        me = unittest.TestCase.id(self)
        self.assertTrue(socket.getaddrinfo("192.0.2.7", 443))       # a literal: no lookup needed
        with self.assertRaises(OSError) as caught:
            socket.create_connection(("192.0.2.7", 443), timeout=1)
        self.assertNotIsInstance(caught.exception, socket.gaierror)
        self.assertIn("connect to", str(caught.exception))
        NETWORK.seen.pop(me, None)

    def test_the_other_lookups_are_refused_too(self):
        me = unittest.TestCase.id(self)
        with self.assertRaises(socket.gaierror):
            socket.gethostbyname("cluster.example.com")
        with self.assertRaises(socket.herror):
            socket.gethostbyaddr("192.0.2.7")
        self.assertEqual("cluster.example.com", socket.getfqdn("cluster.example.com"))
        self.assertEqual(3, len(NETWORK.seen.get(me, [])))
        NETWORK.seen.pop(me, None)

    def test_this_machines_own_name_is_local(self):
        me = unittest.TestCase.id(self)
        NETWORK.seen.pop(me, None)
        socket.getfqdn()                                  # no argument: this machine's own name
        socket.getfqdn(socket.gethostname())
        self.assertTrue(NETWORK._local(socket.gethostname()))
        self.assertTrue(NETWORK._local(socket.gethostname().upper() + "."))
        self.assertEqual([], NETWORK.seen.get(me, []), "this machine's own name is not the network")

    def test_local_names_in_any_spelling_are_this_machine(self):
        for name in ("LOCALHOST", "localhost.", "ip6-localhost", "127.0.0.1", "::1"):
            with self.subTest(name=name):
                self.assertTrue(NETWORK._local(name))
        self.assertFalse(NETWORK._local("cluster.example.com"))

    def test_a_real_lookup_is_refused_at_once_and_named(self):
        me = unittest.TestCase.id(self)
        started = time.monotonic()
        with self.assertRaises(socket.gaierror) as caught:
            socket.getaddrinfo("cluster.example.com", 443)
        self.assertLess(time.monotonic() - started, 0.5)
        self.assertIn("cluster.example.com", str(caught.exception))
        self.assertIn(me, str(caught.exception))
        self.assertIn("tests/test_zz_network_refused.py", str(caught.exception))
        NETWORK.seen.pop(me, None)      # this test's own lookup is not an offence

    def test_this_machine_is_still_reachable(self):
        with socket.socket() as server:
            server.bind(("127.0.0.1", 0))
            server.listen(1)
            with socket.create_connection(server.getsockname(), timeout=2):
                pass
        self.assertTrue(socket.getaddrinfo("localhost", 80))


def _inner_test(body):
    """A test of its own, to run inside one of these: what runs while another test is current."""
    class Other(unittest.TestCase):
        def test_other(self):
            body()
    return Other("test_other")


def _made_by(host, what):
    """Whether a record is the lookup of exactly this host (not one that merely contains it)."""
    return re.search(rf"\bof {re.escape(host)}(?![\w.-])", what) is not None


def _forget(host):
    """Take out only the records a check made itself (each uses its own made-up host),
    never a whole bucket: "(no test running)" or a file's records may hold real calls."""
    with NETWORK._lock:
        for name in list(NETWORK.seen):
            kept = [call for call in NETWORK.seen[name] if not _made_by(host, call[0])]
            if kept:
                NETWORK.seen[name] = kept
            else:
                del NETWORK.seen[name]


def _lookup(host):
    try:
        socket.getaddrinfo(host, 443)
    except socket.gaierror:
        pass


class BlameTests(unittest.TestCase):
    """A refused call is blamed on the test that started its thread, whichever test runs
    when the call lands: a loop a test leaves running is pinned on that test's file."""

    def lookup_later(self, host, go, done, caught):
        def call():
            go.wait(5)
            try:
                socket.getaddrinfo(host, 443)
            except socket.gaierror as error:
                caught.append(str(error))
            done.set()
        return call

    def test_a_thread_is_blamed_on_the_test_that_started_it_even_through_another_thread(self):
        me = unittest.TestCase.id(self)
        go, done, caught = threading.Event(), threading.Event(), []
        call = self.lookup_later("starter.example.com", go, done, caught)

        def loop():                                  # a loop the test starts, which starts its own worker
            worker = threading.Thread(target=call, daemon=True)
            worker.start()
            worker.join(5)
        threading.Thread(target=loop, daemon=True).start()
        other = _inner_test(lambda: (go.set(), done.wait(5)))
        other.run(unittest.TestResult())             # the call lands while this other test runs
        try:
            self.assertTrue(done.is_set())
            self.assertEqual(["lookup of starter.example.com"], [what for what, _ in NETWORK.seen.get(me, [])])
            self.assertNotIn(unittest.TestCase.id(other), NETWORK.seen)
            self.assertIn(me, caught[0])
            self.assertEqual(me, NETWORK._current["test"], "the outer test is current again after the inner one")
        finally:
            _forget("starter.example.com")

    def test_a_thread_started_in_setupclass_is_blamed_on_its_file(self):
        go, done, caught = threading.Event(), threading.Event(), []
        call = self.lookup_later("fixture.example.com", go, done, caught)

        class Fixture(unittest.TestCase):
            @classmethod
            def setUpClass(cls):
                threading.Thread(target=call, daemon=True).start()

            def test_it(self):
                go.set()
                done.wait(5)
        unittest.TestSuite([Fixture("test_it")]).run(unittest.TestResult())
        try:
            self.assertTrue(done.is_set())
            self.assertEqual([__name__], [name for name, calls in NETWORK.seen.items()
                                          if any("fixture.example.com" in what for what, _ in calls)])
            self.assertIn(f"during {__name__}", caught[0])
        finally:
            _forget("fixture.example.com")

    def test_a_thread_started_in_teardownmodule_is_blamed_on_its_file_not_the_next(self):
        name = "network_guard_teardown_probe"
        module = types.ModuleType(name)

        # Note: the bound-state ratchet (test_zz_bound_state_clean) reads "tearDown" in this
        # file as a restore, so a real module bind() added here would need its own restore.
        def tearDownModule():
            worker = threading.Thread(target=_lookup, args=("teardown.example.com",), daemon=True)
            worker.start()
            worker.join(5)
        module.tearDownModule = tearDownModule
        probe = type("Probe", (unittest.TestCase,), {"__module__": name, "test_it": lambda self: None})
        sys.modules[name] = module
        try:
            unittest.TestSuite([probe("test_it")]).run(unittest.TestResult())
            self.assertEqual([name], [key for key, calls in NETWORK.seen.items()
                                      if any("teardown.example.com" in what for what, _ in calls)])
        finally:
            sys.modules.pop(name, None)
            _forget("teardown.example.com")

    def test_a_call_after_its_test_ended_is_late_and_names_the_starter(self):
        go, done, caught = threading.Event(), threading.Event(), []
        call = self.lookup_later("late.example.com", go, done, caught)
        other = _inner_test(lambda: threading.Thread(target=call, daemon=True).start())
        other.run(unittest.TestResult())             # starts the thread and ends without stopping it
        starter = unittest.TestCase.id(other)
        go.set()
        done.wait(5)
        try:
            calls = NETWORK.seen.get(starter, [])
            self.assertEqual(1, len(calls))
            self.assertIn("late", calls[0][0])
            self.assertIn("lookup of late.example.com", calls[0][0])
            self.assertIn(f"late: {starter} had ended", caught[0])
        finally:
            _forget("late.example.com")

    def test_a_thread_started_outside_any_test_is_named_with_where_it_started_and_never_listed(self):
        worker = threading.Thread(target=_lookup, args=("outside.example.com",), name="import-time-loop", daemon=True)
        with mock.patch.dict(NETWORK._current, {"test": None}):     # as at import, or between files
            worker.start()
        worker.join(5)
        try:
            bad = NETWORK.offenders(allowed={NETWORK.NO_TEST})        # listing it changes nothing
            calls = [what for what, _ in bad.get(NETWORK.NO_TEST, []) if "outside.example.com" in what]
            self.assertEqual(1, len(calls))
            self.assertIn("thread import-time-loop", calls[0])
            self.assertIn("started at tests/test_zz_network_refused.py:", calls[0])
        finally:
            _forget("outside.example.com")

    def test_the_final_check_waits_for_a_leaked_thread_and_names_its_starter_as_late(self):
        def call():
            time.sleep(0.5)                          # lands well after its test has ended
            _lookup("leaked.example.com")
        started = []
        other = _inner_test(lambda: started.append(threading.Thread(target=call, daemon=True)) or started[0].start())
        other.run(unittest.TestResult())
        starter = unittest.TestCase.id(other)
        try:
            self.assertIn(started[0], NETWORK.leftover_threads())
            NETWORK.settle([started[0]])
            calls = [what for what, _ in NETWORK.offenders().get(starter, []) if "leaked.example.com" in what]
            self.assertEqual(1, len(calls), "the final check sees the late call")
            self.assertIn("late", calls[0])
        finally:
            started[0].join(5)
            _forget("leaked.example.com")

    def test_the_wait_is_one_short_window_for_all_threads_and_still_sees_a_late_call(self):
        # Threads a test leaves on purpose (servers, idle pool workers) come first and
        # never end within the window; a leaked loop's call 0.5 s after its test must
        # still be seen, and the whole wait must stay one short window, not one per thread.
        release = threading.Event()

        def call():
            time.sleep(0.5)
            _lookup("windowed.example.com")
        blockers, leaked = [], []

        def body():
            for _ in range(3):
                blockers.append(threading.Thread(target=release.wait, args=(30,), daemon=True))
                blockers[-1].start()
            leaked.append(threading.Thread(target=call, daemon=True))
            leaked[0].start()
        other = _inner_test(body)
        other.run(unittest.TestResult())
        starter = unittest.TestCase.id(other)
        try:
            began = time.monotonic()
            still = NETWORK.settle(blockers + leaked)
            waited = time.monotonic() - began
            self.assertLess(waited, 2.5, "one 2 s window for all threads, not one per thread")
            self.assertEqual(blockers, still, "the threads that did not end are named")
            calls = [what for what, _ in NETWORK.offenders().get(starter, []) if "windowed.example.com" in what]
            self.assertEqual(1, len(calls), "the late call is seen within the window")
        finally:
            release.set()
            leaked[0].join(5)
            _forget("windowed.example.com")

    def test_a_process_wide_thread_is_not_waited_for_and_its_late_call_does_not_say_join_it(self):
        go, done, caught = threading.Event(), threading.Event(), []
        call = self.lookup_later("singleton.example.com", go, done, caught)
        started = []
        other = _inner_test(lambda: started.append(threading.Thread(target=call, name="after-request", daemon=True))
                            or started[0].start())
        other.run(unittest.TestResult())
        try:
            self.assertNotIn(started[0], NETWORK.leftover_threads())
            go.set()
            done.wait(5)
            self.assertIn("process-wide thread after-request", caught[0])
            self.assertNotIn("join", caught[0])
        finally:
            _forget("singleton.example.com")


class SingletonNamesTests(unittest.TestCase):
    """The process-wide threads the final wait skips are named in the guard by their
    thread names: a rename in server/ must fail here, not quietly start being waited for."""

    def test_each_process_wide_thread_name_is_one_server_code_starts(self):
        self.assertEqual([], _unknown_thread_names(NETWORK.SINGLETONS))

    def test_a_name_no_server_code_uses_is_reported(self):
        self.assertEqual(["no-such-thread"], _unknown_thread_names({"no-such-thread", "after-request"}))

    def test_a_record_is_matched_by_its_exact_host(self):
        self.assertTrue(_made_by("a.example.com", "late (thread x outlived its test): lookup of a.example.com"))
        self.assertTrue(_made_by("a.example.com", "lookup of a.example.com [thread t, ident 1, started at x]"))
        self.assertFalse(_made_by("a.example.com", "lookup of data.example.com"))
        self.assertFalse(_made_by("a.example.com", "lookup of a.example.com.cluster.local"))


def _unknown_thread_names(names):
    server = Path(__file__).resolve().parents[1] / "server"
    text = "\n".join(path.read_text(encoding="utf-8") for path in server.glob("*.py"))
    return sorted(name for name in names if f'name="{name}"' not in text)


class OtherRecordsKeptTests(unittest.TestCase):
    """The checks above take out only what they made: a real record (a thread started
    at import, say) must survive them to the final check."""

    def test_blame_tests_leave_other_records_alone(self):
        marker = ("lookup of planted.example.com", "tests/test_zz_network_refused.py (planted)")
        with NETWORK._lock:
            NETWORK.seen.setdefault("(no test running)", []).append(marker)
        try:
            result = unittest.TestResult()
            unittest.defaultTestLoader.loadTestsFromTestCase(BlameTests).run(result)
            self.assertEqual([], [trace for _, trace in result.failures + result.errors])
            self.assertIn(marker, NETWORK.seen.get("(no test running)", []))
        finally:
            _forget("planted.example.com")


if __name__ == "__main__":
    unittest.main()
