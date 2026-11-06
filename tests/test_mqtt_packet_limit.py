"""MQTT packets have a size limit: a packet whose declared length is over
64 KiB is refused before its body is read, the listener disconnects, the status
says what size came in, and the publisher reconnects with its backoff."""
import socket
import sys
import threading
import types
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "server"))

import homestead_mqtt as MQTT

HUNDRED_MB = 100 * 1024 * 1024
TOPIC = "harvester/control/app/lab/frigate/power"


def feeder(data, piece=None):
    """recv(n) over data, piece bytes at most per call; counts the calls."""
    data = bytes(data)
    state = {"pos": 0, "calls": 0}

    def recv(n):
        state["calls"] += 1
        step = min(n, piece or n)
        out = data[state["pos"]:state["pos"] + step]
        state["pos"] += len(out)
        return out
    return recv, state


def header(first, length):
    return bytes([first]) + MQTT._remaining(length)


class ReadPacketTests(unittest.TestCase):
    def test_a_declared_length_over_the_limit_is_refused_before_its_body_is_read(self):
        head = header(0x30, HUNDRED_MB)
        recv, state = feeder(head + b"\x00" * 4096)
        with self.assertRaises(ConnectionError) as caught:
            MQTT.read_packet(recv)
        self.assertEqual(len(head), state["pos"], "no byte of the body is read")
        self.assertIn(str(HUNDRED_MB), str(caught.exception))
        self.assertIn("64 KiB", str(caught.exception))

    def test_one_byte_over_the_limit_is_refused(self):
        recv, state = feeder(header(0x30, 64 * 1024 + 1) + b"\x00" * (64 * 1024 + 1))
        with self.assertRaises(ConnectionError):
            MQTT.read_packet(recv)
        self.assertLessEqual(state["pos"], 5)

    def test_a_packet_just_under_the_limit_is_read(self):
        topic = TOPIC.encode()
        payload = b"x" * (64 * 1024 - 1 - 2 - len(topic))
        packet = MQTT.publish_packet(TOPIC, payload, retain=False)
        self.assertEqual(64 * 1024 - 1 + 4, len(packet))   # 3 length bytes + first byte
        recv, _ = feeder(packet, piece=1500)
        first, body = MQTT.read_packet(recv)
        self.assertIsInstance(body, bytes)
        self.assertEqual((TOPIC, payload, False), MQTT.parse_publish(first, body))

    def test_a_packet_of_exactly_the_limit_is_read(self):
        topic = TOPIC.encode()
        payload = b"z" * (64 * 1024 - 2 - len(topic))
        packet = MQTT.publish_packet(TOPIC, payload, retain=False)
        self.assertEqual(header(0x30, 64 * 1024), packet[:4])
        recv, _ = feeder(packet, piece=1500)
        first, body = MQTT.read_packet(recv)
        self.assertEqual((TOPIC, payload, False), MQTT.parse_publish(first, body))

    def test_a_large_packet_from_small_pieces_copies_each_byte_a_bounded_number_of_times(self):
        copied = [0]

        class Piece(bytes):
            # Concatenating onto an immutable bytes copies the whole packet so
            # far; a bytearray appends in place (NotImplemented hands it back).
            def __radd__(self, other):
                if isinstance(other, bytearray):
                    return NotImplemented
                copied[0] += len(other)
                return bytes(other) + bytes(self)
        size = 60 * 1024
        data = header(0x30, size) + b"y" * size
        pos = [0]

        def recv(n):
            out = Piece(data[pos[0]:pos[0] + min(n, 16)])
            pos[0] += len(out)
            return out
        first, body = MQTT.read_packet(recv)
        self.assertEqual(b"y" * size, body)
        self.assertLess(copied[0], 4 * size, "the packet is not rebuilt on every piece")

    def test_a_malformed_remaining_length_is_still_refused(self):
        recv, _ = feeder(b"\x30\xff\xff\xff\xff\x01")
        with self.assertRaises(ConnectionError) as caught:
            MQTT.read_packet(recv)
        self.assertIn("malformed", str(caught.exception))


class Broker:
    """A fake broker on loopback. Each connection gets a CONNACK; after the
    SUBSCRIBE it sends the header of a PUBLISH declaring 100 MB and some of its
    body, then keeps everything the client sends until the client closes."""

    def __init__(self, connections):
        self.server = socket.socket()
        self.server.bind(("127.0.0.1", 0))
        self.server.listen(4)
        self.server.settimeout(10)
        self.port = self.server.getsockname()[1]
        self.received, self.closed = [], []
        self.thread = threading.Thread(target=self._serve, args=(connections,), daemon=True)
        self.thread.start()

    def _serve(self, connections):
        for _ in range(connections):
            try:
                conn, _ = self.server.accept()
            except OSError:
                return
            conn.settimeout(10)
            got = bytearray()
            try:
                got += conn.recv(4096)                      # CONNECT
                conn.sendall(b"\x20\x02\x00\x00")
                while b"\x82" not in got[len(got) - 4096:] and len(got) < 65536:
                    piece = conn.recv(4096)                 # SUBSCRIBE
                    if not piece:
                        break
                    got += piece
                conn.sendall(header(0x31, HUNDRED_MB) + b"\x00" * 2048)   # retained, oversized
                while True:
                    piece = conn.recv(4096)
                    if not piece:
                        break
                    got += piece
                self.closed.append(True)
            except (ConnectionResetError, BrokenPipeError):
                self.closed.append(True)
            except OSError:
                self.closed.append(False)
            finally:
                self.received.append(bytes(got))
                conn.close()

    def stop(self):
        self.server.close()
        self.thread.join(10)


class ListenerTests(unittest.TestCase):
    def setUp(self):
        MQTT._restart.clear()
        self.addCleanup(MQTT._restart.clear)

    def test_an_oversized_packet_ends_the_listener_and_says_its_size(self):
        broker = Broker(1)
        self.addCleanup(broker.stop)
        got = []
        client = MQTT.Client({"host": "127.0.0.1", "port": broker.port}, "", "t").connect(timeout=5)
        self.addCleanup(client.close)
        client.listen(TOPIC.rsplit("/", 1)[0] + "/+", lambda *message: got.append(message))
        self.assertTrue(client.lost.wait(10), "the listener stops")
        self.assertTrue(MQTT._restart.is_set(), "the publisher is told to reconnect")
        self.assertEqual([], got, "the oversized message is never handed on")
        self.assertIn(str(HUNDRED_MB), client.refused)
        client.publish("homestead/after", "still works", retain=False)


class Stop(BaseException):
    pass


class PauseTests(unittest.TestCase):
    def setUp(self):
        MQTT._restart.clear()
        self.addCleanup(MQTT._restart.clear)

    def test_a_settings_change_ends_the_pause_early(self):
        done = threading.Event()
        pauser = threading.Thread(target=lambda: (MQTT._pause(300), done.set()), daemon=True)
        pauser.start()
        # As save() does; again until it is seen, since the pause starts by
        # dropping a wake-up left from before it.
        while not done.wait(0.01):
            MQTT._restart.set()
            if not pauser.is_alive():
                break
        pauser.join(10)
        self.assertTrue(done.is_set(), "the pause ended when settings changed")


def run_patches(test, cfg, extra=()):
    """run() against the fake broker: settings from cfg (read each time), no
    command worker (so no thread is left to take a later test's command)."""
    patches = [mock.patch.object(MQTT, "load", side_effect=lambda: dict(cfg)),
               mock.patch.object(MQTT, "is_leader", return_value=True),
               mock.patch.object(MQTT, "snapshot", return_value={"nodes": []}),
               mock.patch.object(MQTT, "_password", return_value=""),
               mock.patch.object(MQTT, "_share"),
               mock.patch.object(MQTT, "_command_worker", lambda: None),
               mock.patch.dict(MQTT.STATUS, {"state": "off", "error": "", "published": 0}),
               mock.patch.dict(MQTT._live, {}), *extra]
    for patch in patches:
        patch.start()
        test.addCleanup(patch.stop)


def run_until_stopped():
    try:
        MQTT.run()
    except Stop:
        pass


class RunTests(unittest.TestCase):
    def setUp(self):
        MQTT._restart.clear()
        self.addCleanup(MQTT._restart.clear)

    def broker_cfg(self, broker):
        return {**MQTT.DEFAULTS, "enabled": True, "host": "127.0.0.1", "port": broker.port,
                "control": True, "base": "harvester", "interval": 60}

    def test_turning_mqtt_off_during_the_backoff_takes_effect_at_once(self):
        broker = Broker(1)
        self.addCleanup(broker.stop)
        cfg = self.broker_cfg(broker)
        blocked = threading.Event()

        def stop_when_off(state, detail="", error=""):
            if state == "off":
                raise Stop()
        # time.sleep never returns here: the backoff must be a wait that a
        # settings change ends.
        clock = types.SimpleNamespace(time=MQTT.time.time, monotonic=MQTT.time.monotonic,
                                      sleep=lambda seconds: blocked.wait())
        run_patches(self, cfg, [mock.patch.object(MQTT, "publish_round", return_value=(1, [])),
                                mock.patch.object(MQTT, "time", clock),
                                mock.patch.object(MQTT, "_set", side_effect=stop_when_off)])
        self.addCleanup(blocked.set)
        runner = threading.Thread(target=run_until_stopped, daemon=True)
        runner.start()
        for _ in range(1000):
            if broker.closed:
                break
            runner.join(0.01)
        self.assertEqual([True], broker.closed, "the oversized packet ended the first connection")
        cfg["enabled"] = False
        for _ in range(1000):               # up to about 10 s
            if not runner.is_alive():
                break
            MQTT._restart.set()             # as save() does
            runner.join(0.01)
        self.assertFalse(runner.is_alive(), "switching off ended the backoff")

    def test_an_oversized_retained_message_is_a_visible_reconnect_loop_with_backoff(self):
        broker = Broker(2)
        self.addCleanup(broker.stop)
        cfg = self.broker_cfg(broker)
        rounds, pauses, seen = [], [], []

        def publish_round(cfg, client, snap, announced):
            client.publish("homestead/round", str(len(rounds)), retain=False)
            rounds.append(client)
            return 1, []

        def pause(seconds):
            with MQTT._lock:
                seen.append(dict(MQTT.STATUS))
            pauses.append(seconds)
            if len(pauses) == 2:
                raise Stop()
        run_patches(self, cfg, [mock.patch.object(MQTT, "publish_round", side_effect=publish_round),
                                mock.patch.object(MQTT, "_pause", side_effect=pause)])
        runner = threading.Thread(target=run_until_stopped, daemon=True)
        runner.start()
        runner.join(20)
        self.assertFalse(runner.is_alive(), "run() reached its backoff twice")
        broker.stop()
        self.assertEqual([5, 10], pauses, "it reconnects with the backoff, which grows")
        for status in seen:
            self.assertEqual("error", status["state"])
            self.assertIn(str(HUNDRED_MB), status["error"])
        self.assertEqual(2, len(rounds), "it publishes on every connection")
        self.assertEqual([True, True], broker.closed, "each connection is closed by Homestead")
        for data in broker.received:
            self.assertIn(b"homestead/round", data)


if __name__ == "__main__":
    unittest.main()
