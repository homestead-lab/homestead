import json
import socket
import sys
import tempfile
import threading
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "server"))

import homestead_history as HISTORY
import homestead_mqtt as MQTT
import homestead_shared as SHARED

SNAP = {"cluster": {"nodes_ready": 2, "nodes_total": 3, "nodes_notready": 1, "vol_total": 4, "vol_degraded": 0,
                    "vol_faulted": 0, "pods_system": 50, "pods_workload": 6, "pods_sys_bad": 0, "pods_wl_bad": 1,
                    "vms_running": 0, "health": "critical", "wl_summary": "lab:6", "cpu_pct": 10.0, "mem_pct": 20.0},
        "nodes": [{"name": "harvester-node1", "cpu_pct": 5, "mem_pct": 9, "mem_gb": 3.2, "rx_mbps": 1, "tx_mbps": 2,
                   "pods": 20, "vms": 0, "wl": "frigate", "status": "Ready"}]}


class FakeClient:
    def __init__(self):
        self.sent = []

    def publish(self, topic, message, retain=True):
        self.sent.append((topic, message))


class MqttTests(unittest.TestCase):
    def test_packets_are_mqtt_3_1_1(self):
        packet = MQTT.connect_packet("hs", "user", "pw", "harvester/cluster/availability", "offline", keepalive=90)
        self.assertEqual(0x10, packet[0])
        self.assertEqual(b"\x00\x04MQTT\x04", packet[2:9])
        # clean session, will, will retain, username, password
        self.assertEqual(0x02 | 0x04 | 0x20 | 0x80 | 0x40, packet[9])
        big = MQTT.publish_packet("t", "x" * 300)
        self.assertEqual(0x31, big[0])
        self.assertEqual(bytes([(303 % 128) | 0x80, 303 // 128]), big[1:3])

    def test_the_same_entities_as_hv_exporter(self):
        cfg = MQTT.clean({"host": "broker"})
        topics = dict(MQTT.discovery(cfg, SNAP))
        health = topics["homeassistant/sensor/harvester_health/config"]
        self.assertEqual(("hv_health", "harvester/cluster/state", "harvester/cluster/availability"),
                         (health["unique_id"], health["state_topic"], health["availability_topic"]))
        cpu = topics["homeassistant/sensor/hv_harvester_node1_cpu/config"]
        self.assertEqual(("hv_harvester_node1_cpu", "harvester/node/harvester_node1/state", "{{ value_json.cpu_pct }}"),
                         (cpu["unique_id"], cpu["state_topic"], cpu["value_template"]))
        self.assertEqual(15 + 9, len(topics))

    def test_discovery_is_sent_again_only_when_nodes_change(self):
        cfg = MQTT.clean({"host": "broker"})
        client, announced = FakeClient(), {}
        MQTT.publish_once(cfg, client, SNAP, announced)
        first = len(client.sent)
        MQTT.publish_once(cfg, client, SNAP, announced)
        self.assertEqual(first + 3, len(client.sent))   # availability and two states
        state = json.loads(dict(client.sent)["harvester/cluster/state"])
        self.assertEqual("critical", state["health"])

    def test_settings_are_checked(self):
        for bad in ({"enabled": True, "host": "bad host!"}, {"host": "b", "port": 70000},
                    {"host": "b", "base": "a b"}, {"host": "b", "interval": 1}):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                MQTT.clean(bad)
        self.assertEqual(8883, MQTT.clean({"host": "b", "tls": True})["port"])

    def test_a_refused_login_says_why(self):
        server = socket.socket()
        server.bind(("127.0.0.1", 0))
        server.listen(1)
        port = server.getsockname()[1]

        def broker():
            conn, _ = server.accept()
            conn.recv(1024)
            conn.sendall(b"\x20\x02\x00\x05")
            conn.close()
        threading.Thread(target=broker, daemon=True).start()
        try:
            with self.assertRaisesRegex(ConnectionError, "username and password"):
                MQTT.Client(MQTT.clean({"host": "127.0.0.1", "port": port}), "", "x").connect(timeout=5)
        finally:
            server.close()


def overview(cpu, ready=3, node2_ready=True):
    return {"cpu_pct": cpu, "mem_pct": 40, "workload_pods": 10, "vol_degraded": 0, "vol_faulted": 0,
            "nodes_ready": ready, "nodes_total": 3,
            "nodes": [{"name": "n1", "cpu_pct": cpu, "mem_pct": 30, "status": "Ready", "rx_mbps": 1, "tx_mbps": 1},
                      {"name": "n2", "cpu_pct": cpu, "mem_pct": 30, "status": "Ready" if node2_ready else "NotReady",
                       "rx_mbps": 1, "tx_mbps": 1}]}


class HistoryTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        HISTORY.bind(self.dir.name)
        SHARED.bind(self.dir.name)

    def tearDown(self):
        self.dir.cleanup()

    def test_samples_roll_up_into_hours_and_old_ones_go(self):
        start = 1_800_000_000 - 1_800_000_000 % 3600
        for i in range(24):   # two hours of samples; node2 down for three of them
            HISTORY.record(overview(10 + i, node2_ready=not (3 <= i < 6)), now=start + i * 300)
        HISTORY.record(overview(50), now=start + 3 * 3600)   # the hours before are complete now
        data = json.loads(Path(self.dir.name, "history.json").read_text())
        self.assertEqual([start, start + 3600], [c["t"] for c in data["coarse"]])
        first = data["coarse"][0]
        self.assertEqual((12, 15.5, 21), (first["n"], first["cpu"], first["cpu_max"]))
        self.assertAlmostEqual(0.75, first["nodes"]["n2"][2])
        day = HISTORY.series("24h", now=start + 3 * 3600)
        self.assertEqual(25, day["samples"])
        n2 = next(n for n in day["nodes"] if n["name"] == "n2")
        self.assertEqual(88.0, n2["availability"])
        week = HISTORY.series("7d", now=start + 3 * 3600)
        self.assertEqual(2, week["samples"])
        self.assertEqual(87.5, next(n for n in week["nodes"] if n["name"] == "n2")["availability"])
        # two days later the fine samples are gone, the hours stay
        HISTORY.record(overview(5), now=start + 3 * 86400)
        data = json.loads(Path(self.dir.name, "history.json").read_text())
        self.assertEqual(1, len(data["fine"]))
        self.assertGreaterEqual(len(data["coarse"]), 3)

    def test_an_empty_history_is_empty(self):
        self.assertEqual(0, HISTORY.series("30d")["samples"])


if __name__ == "__main__":
    unittest.main()
