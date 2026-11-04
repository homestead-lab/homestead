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
import socket
import sys
import threading
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import network_guard as NETWORK

NETWORK.install()

CEILING = 7
# The list's files, digested: a swap (one off, another on) keeps the count but not this.
# "A listed file no longer offends" is not checked: which tests reach the network depends
# on what ran before them, so a file can look clean in one order and offend in another.
DIGEST = "360c17ef5880507f"


class NetworkRefused(unittest.TestCase):
    def test_no_test_outside_the_list_reached_the_network(self):
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

    def forget(self, *names):
        for name in names:
            NETWORK.seen.pop(name, None)

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
            self.forget(me, unittest.TestCase.id(other), "(no test running)")

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
        inner = Fixture("test_it")
        before = set(NETWORK.seen)
        unittest.TestSuite([inner]).run(unittest.TestResult())
        new = set(NETWORK.seen) - before
        try:
            self.assertTrue(done.is_set())
            self.assertEqual({__name__}, new)
            self.assertIn(f"during {__name__}", caught[0])
        finally:
            self.forget(*new)

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
            self.forget(starter, unittest.TestCase.id(self), "(no test running)")


if __name__ == "__main__":
    unittest.main()
