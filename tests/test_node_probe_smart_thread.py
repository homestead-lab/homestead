"""SMART is read on its own thread, one pass at a time, so a slow SMART
helper never holds up the temperature rounds (#974 part 3)."""
import http.server
import json
import socket
import threading
import time
import unittest
import urllib.request
from unittest import mock

from test_node_probes import Base, PROBES

REAL_URLOPEN = urllib.request.urlopen        # before any test patches it


def smart_reads(cluster, ip="192.0.2.1"):
    return len([url for url in cluster.calls if f"{ip}:9100" in url])


class SmartThreadTests(Base):
    def test_a_round_returns_without_waiting_for_a_slow_smart_helper(self):
        self.cluster.smart_delay = 1.0          # stands for a helper waiting out SMART_TIMEOUT
        started = time.monotonic()
        PROBES.run_round(now=1000)
        took = time.monotonic() - started
        self.assertLess(took, PROBES.TIMEOUT + 0.5, "the round waited for SMART")
        self.assertLess(took, 0.5)
        self.assertEqual({"node1", "node2", "node3"}, set(PROBES.node_temps(wait=False)))
        self.assertTrue(PROBES.smart_settled(5))
        disk = PROBES.node_temps(wait=False)["node1"]["disks"][0]
        self.assertTrue(disk["smart"]["passed"], "the SMART pass still lands, after the round")

    def test_the_next_round_is_not_held_by_a_smart_pass_still_running(self):
        self.cluster.smart_delay = 1.0
        first = threading.Thread(target=PROBES.run_round, kwargs={"now": 1000})
        first.start()
        self.addCleanup(first.join)
        self.assertTrue(PROBES._round["done"].wait(0.6))
        self.cluster.down = {"192.0.2.2"}
        started = time.monotonic()
        first.join()                            # the first round itself is over...
        PROBES.run_round(now=1015)              # ...so this one runs, while its SMART pass still does
        self.assertLess(time.monotonic() - started, 0.5, "the second round waited on the first round's SMART")
        self.assertEqual(1, self.misses("node2"))
        self.assertTrue(PROBES.smart_settled(5))

    def test_one_smart_pass_at_a_time(self):
        self.cluster.smart_delay = 0.6
        PROBES.run_round(now=1000)
        # SMART for every host is still due here (nothing has landed yet), but
        # a pass is running: this round does not start a second one.
        PROBES.run_round(now=1000 + PROBES.SMART_EVERY)
        self.assertTrue(PROBES.smart_settled(5))
        self.assertEqual(1, smart_reads(self.cluster))
        self.assertTrue(PROBES.node_temps(wait=False)["node1"]["smart_helper"]["available"])

    def test_a_smart_test_started_during_a_pass_is_read_on_the_next_round(self):
        self.cluster.smart_delay = 0.4
        PROBES.run_round(now=1000)
        PROBES.read_smart_again()               # a SMART test started while that pass ran
        self.assertTrue(PROBES.smart_settled(5))
        PROBES.run_round(now=1015)
        self.assertTrue(PROBES.smart_settled(5))
        self.assertEqual(2, smart_reads(self.cluster), "the reading taken before the test started stood in for it")

    def test_a_failed_smart_pass_still_retries_sooner(self):
        self.cluster.smart_down = {"192.0.2.1"}
        PROBES.run_round(now=1000)
        self.assertTrue(PROBES.smart_settled(5))
        PROBES.run_round(now=1000 + PROBES.SMART_RETRY)
        self.assertTrue(PROBES.smart_settled(5))
        self.assertEqual(2, smart_reads(self.cluster))
        self.assertEqual(1, smart_reads(self.cluster, "192.0.2.2"))

    def test_reset_waits_for_a_running_pass_so_it_cannot_land_afterwards(self):
        self.cluster.smart_delay = 0.3
        PROBES.run_round(now=1000)
        PROBES.reset()
        self.assertEqual({}, PROBES.status())
        self.assertFalse(any(thread.name == "node-probes-smart" and thread.is_alive()
                             for thread in threading.enumerate()))


class _Probe(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        body = json.dumps({"cpu_c": 41, "disks": [{"name": "sda"}]}).encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


class RealSocketTests(Base):
    """The fakes above sleep; here the temperatures come from a real HTTP
    server and the SMART helper is a real socket that accepts and never
    answers, so the read ends on urllib's own timeout, as on a host."""

    def setUp(self):
        super().setUp()
        probe = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Probe)
        threading.Thread(target=probe.serve_forever, daemon=True).start()
        self.addCleanup(probe.server_close)
        self.addCleanup(probe.shutdown)
        silent = socket.create_server(("127.0.0.1", 0))
        self.addCleanup(silent.close)
        held = []
        def hold():
            while True:
                try:
                    held.append(silent.accept()[0])
                except OSError:
                    return
        threading.Thread(target=hold, daemon=True).start()
        self.addCleanup(lambda: [conn.close() for conn in held])
        ports = {"9099": probe.server_address[1], "9100": silent.getsockname()[1]}

        def to_local(url, timeout=None):
            # Each host's probe pod address and port, pointed at the local servers.
            request = url if isinstance(url, urllib.request.Request) else urllib.request.Request(url)
            host, port = request.full_url.split("//")[1].rstrip("/").split(":")
            self.reads.append(port)
            request.full_url = f"http://127.0.0.1:{ports[port]}/"
            return REAL_URLOPEN(request, timeout=timeout)
        self.reads = []
        for patch in (mock.patch.object(PROBES.urllib.request, "urlopen", to_local),
                      mock.patch.object(PROBES, "SMART_TIMEOUT", 1.0)):
            patch.start()
            self.addCleanup(patch.stop)
        self.addCleanup(PROBES.smart_settled, 5)     # before the patches above are undone

    def test_a_helper_that_never_answers_holds_up_no_round(self):
        started = time.monotonic()
        PROBES.run_round(now=1000)
        PROBES.run_round(now=1015)
        took = time.monotonic() - started
        self.assertLess(took, 0.5, "the rounds waited on a SMART helper that never answers")
        self.assertEqual(41, PROBES.node_temps(wait=False)["node1"]["cpu_c"])
        self.assertEqual(3, self.reads.count("9100"), "one SMART pass at a time")
        self.assertTrue(PROBES.smart_settled(5), "the pass ends on the socket timeout")
        self.assertEqual({"available": False, "reason": PROBES.SMART_UNAVAILABLE},
                         PROBES.node_temps(wait=False)["node1"]["smart_helper"])
        PROBES.run_round(now=1000 + PROBES.SMART_RETRY)
        self.assertTrue(PROBES.smart_settled(5))
        self.assertEqual(6, self.reads.count("9100"), "tried again after SMART_RETRY")


if __name__ == "__main__":
    unittest.main()
