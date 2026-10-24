import json
import socket
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "server"))

import homestead_agent_audit as AUDIT
import homestead_kube as K
import homestead_mqtt as MQTT
import homestead_operations as OPS
from fakes.kube import Kube

DEFAULT_ITEMS, DEFAULT_ACT = MQTT.control_items, MQTT.control_act

ITEMS = [
    {"id": "vm:vms/win11", "kind": "vm", "ns": "vms", "name": "win11", "on": True, "refused": ""},
    {"id": "app:lab/frigate", "kind": "app", "ns": "lab", "name": "frigate", "on": False, "refused": ""},
    {"id": "app:lab/homestead", "kind": "app", "ns": "lab", "name": "homestead", "on": True,
     "refused": "it is part of Homestead"},
]


class FakeClient:
    def __init__(self):
        self.sent = []

    def publish(self, topic, message, retain=True):
        self.sent.append((topic, message))


class Store:
    """The settings ConfigMap on the shared fake API (tests/fakes/kube.py); no Secret."""
    def __init__(self, test, stored=None):
        self.path = f"/api/v1/namespaces/default/configmaps/{MQTT._map()}"
        self.kube = test.enterContext(Kube())
        self.set_cfg(stored or {})
        # The shared fake runs the real write guards; the storage guard takes the job
        # store's lock, which lives under its DATA_DIR.
        test.enterContext(mock.patch.object(OPS, "DATA_DIR", test.enterContext(tempfile.TemporaryDirectory())))
        saved = MQTT.kget, MQTT.ksend, MQTT.NAMESPACE, MQTT.snapshot, MQTT.is_leader
        test.addCleanup(lambda: MQTT.bind(*saved))
        MQTT.bind(K.kget, K.ksend, "default", lambda: None, lambda: True)

    def set_cfg(self, cfg):
        self.kube.add({"apiVersion": "v1", "kind": "ConfigMap",
                       "metadata": {"name": MQTT._map(), "namespace": "default"},
                       "data": {"mqtt.json": json.dumps(cfg)}})

    def cfg(self):
        return json.loads(self.kube.object(self.path)["data"]["mqtt.json"])


class ControlTests(unittest.TestCase):
    def setUp(self):
        self.store = Store(self, {"enabled": True, "host": "192.0.2.177", "control": True,
                                  "controlled": ["vm:vms/win11", "app:lab/frigate"]})
        self.calls = []
        MQTT.bind_control(lambda: [dict(i) for i in ITEMS], lambda *call: self.calls.append(call) or "")
        MQTT._recent.clear()
        MQTT._minute.clear()
        self.audit_dir = tempfile.TemporaryDirectory()
        AUDIT.bind(self.audit_dir.name)

    def tearDown(self):
        self.audit_dir.cleanup()

    def audited(self):
        return AUDIT._read()

    def test_commands_are_read_from_their_topics(self):
        cfg = MQTT.load()
        self.assertEqual(("vm", "vms", "win11", "start"), MQTT.parse_command(cfg, "harvester/control/vm/vms/win11/power", b"ON"))
        self.assertEqual(("app", "lab", "frigate", "stop"), MQTT.parse_command(cfg, "harvester/control/app/lab/frigate/power", b"off"))
        self.assertEqual(("vm", "vms", "win11", "force-off"), MQTT.parse_command(cfg, "harvester/control/vm/vms/win11/force_off", b"PRESS"))
        self.assertIsNone(MQTT.parse_command(cfg, "harvester/control/vm/vms/win11/state", b"ON"))
        for topic, payload in (("harvester/control/app/lab/frigate/reset", b"PRESS"),     # not for a container
                               ("harvester/control/vm/vms/win11/power", b"PRESS"),
                               ("harvester/control/vm/vms/win11/restart", b"ON"),
                               ("harvester/control/pod/lab/x/power", b"ON"),
                               ("harvester/control/vm/Bad_NS/x/power", b"ON"),
                               ("harvester/control/vm/vms/power", b"ON"),
                               ("other/control/vm/vms/win11/power", b"ON")):
            with self.subTest(topic=topic), self.assertRaises(ValueError):
                MQTT.parse_command(cfg, topic, payload)

    def test_a_vm_gets_a_switch_and_three_buttons_a_container_one(self):
        cfg = MQTT.load()
        topics = dict(MQTT.control_discovery(cfg, ITEMS[:2]))
        power = topics["homeassistant/switch/hs_vm_vms_win11/config"]
        self.assertEqual(("harvester/control/vm/vms/win11/power", "harvester/control/vm/vms/win11/state", "harvester_cluster"),
                         (power["command_topic"], power["state_topic"], power["device"]["via_device"]))
        self.assertIn("homeassistant/button/hs_vm_vms_win11_reset/config", topics)
        self.assertIn("homeassistant/button/hs_vm_vms_win11_force_off/config", topics)
        self.assertIn("homeassistant/button/hs_app_lab_frigate_restart/config", topics)
        self.assertNotIn("homeassistant/button/hs_app_lab_frigate_reset/config", topics)
        self.assertEqual(4 + 2, len(topics))
        self.assertEqual([("harvester/control/vm/vms/win11/state", "ON"), ("harvester/control/app/lab/frigate/state", "OFF")],
                         MQTT.control_states(cfg, ITEMS[:2]))

    def test_only_items_picked_and_allowed_are_covered(self):
        self.store.set_cfg({**self.store.cfg(), "controlled": ["vm:vms/win11", "app:lab/homestead"]})
        self.assertEqual(["vm:vms/win11"], [i["id"] for i in MQTT.controlled(MQTT.load())])
        self.store.set_cfg({**self.store.cfg(), "control": False})
        self.assertEqual([], MQTT.controlled(MQTT.load()))

    def test_saving_control_keeps_the_broker_and_retires_what_was_dropped(self):
        result = MQTT.save_control({"control": True, "controlled": ["vm:vms/win11"]})
        self.assertEqual(["vm:vms/win11"], result["controlled"])
        cfg = self.store.cfg()
        self.assertEqual(("192.0.2.177", ["app:lab/frigate"]), (cfg["host"], cfg["retire"]))
        MQTT.save_control({"control": True, "controlled": ["vm:vms/win11", "app:lab/frigate"]})
        self.assertEqual([], self.store.cfg()["retire"])   # back again: not retired
        MQTT.save_control({"control": False, "controlled": ["vm:vms/win11", "app:lab/frigate"]})
        self.assertEqual(["app:lab/frigate", "vm:vms/win11"], sorted(self.store.cfg()["retire"]))
        with self.assertRaisesRegex(ValueError, "part of Homestead"):
            MQTT.save_control({"control": True, "controlled": ["app:lab/homestead"]})
        with self.assertRaises(ValueError):
            MQTT.save_control({"control": True, "controlled": ["lab/frigate"]})

    def test_saving_the_broker_leaves_control_as_it_was(self):
        MQTT.save({"enabled": True, "host": "192.0.2.178"})
        cfg = self.store.cfg()
        self.assertEqual(("192.0.2.178", True, ["vm:vms/win11", "app:lab/frigate"]), (cfg["host"], cfg["control"], cfg["controlled"]))

    def test_a_command_is_carried_out_and_written_down(self):
        self.assertEqual("ok", MQTT.handle(MQTT.load(), "harvester/control/vm/vms/win11/reset", b"PRESS", now=1000))
        self.assertEqual([("vm", "vms", "win11", "reset")], self.calls)
        entry = self.audited()[-1]
        self.assertEqual(("mqtt", "mqtt", "mqtt_vm_reset", "ok", {"namespace": "vms", "name": "win11"}),
                         (entry["key"], entry["via"], entry["action"], entry["outcome"], entry["args"]))

    def test_commands_it_does_not_cover_are_refused(self):
        cfg = MQTT.load()
        self.assertEqual("refused", MQTT.handle(cfg, "harvester/control/app/lab/homestead/power", b"OFF", now=1000))
        self.assertEqual("refused", MQTT.handle(cfg, "harvester/control/app/lab/plex/power", b"ON", now=1000))
        self.assertEqual("refused", MQTT.handle({**cfg, "control": False}, "harvester/control/vm/vms/win11/power", b"ON", now=1000))
        self.assertEqual("refused", MQTT.handle(cfg, "harvester/control/vm/vms/win11/power", b"maybe", now=1000))
        self.assertEqual([], self.calls)
        self.assertEqual(4, len(self.audited()))

    def test_commands_are_rate_limited(self):
        cfg = MQTT.load()
        self.assertEqual("ok", MQTT.handle(cfg, "harvester/control/vm/vms/win11/power", b"ON", now=1000))
        self.assertEqual("refused", MQTT.handle(cfg, "harvester/control/vm/vms/win11/power", b"OFF", now=1005))
        self.assertEqual("ok", MQTT.handle(cfg, "harvester/control/vm/vms/win11/power", b"OFF", now=1011))
        MQTT._minute[:] = [1011] * MQTT.COMMANDS_PER_MINUTE
        self.assertEqual("refused", MQTT.handle(cfg, "harvester/control/app/lab/frigate/power", b"ON", now=1012))
        self.assertEqual(2, len(self.calls))

    def test_a_failed_command_says_why(self):
        def fails(*_):
            raise ValueError("it cannot start: not enough room")
        MQTT.bind_control(lambda: [dict(i) for i in ITEMS], fails)
        self.assertEqual("error", MQTT.handle(MQTT.load(), "harvester/control/vm/vms/win11/power", b"ON", now=1000))
        self.assertIn("not enough room", self.audited()[-1]["detail"])

    def test_a_retained_command_is_never_acted_on(self):
        while not MQTT._commands.empty():
            MQTT._commands.get_nowait()
        MQTT._on_message("harvester/control/vm/vms/win11/power", b"ON", True)
        self.assertTrue(MQTT._commands.empty())
        MQTT._on_message("harvester/control/vm/vms/win11/power", b"ON", False)
        self.assertEqual(1, MQTT._commands.qsize())
        MQTT._commands.get_nowait()

    def test_publishing_retires_announces_and_states_the_items(self):
        cfg = {**MQTT.load(), "retire": ["app:lab/plex", "vm:vms/win11"]}
        client, announced = FakeClient(), {}
        snap = {"cluster": {}, "nodes": []}
        MQTT.publish_once(cfg, client, snap, announced, MQTT.controlled(cfg))
        sent = dict(client.sent)
        self.assertEqual("", sent["homeassistant/switch/hs_app_lab_plex/config"])
        self.assertEqual("", sent["harvester/control/app/lab/plex/state"])
        # win11 is covered again, so it is announced, not retired
        self.assertTrue(sent["homeassistant/switch/hs_vm_vms_win11/config"])
        self.assertEqual("ON", sent["harvester/control/vm/vms/win11/state"])
        client.sent.clear()
        MQTT.publish_once(cfg, client, snap, announced, MQTT.controlled(cfg))
        self.assertEqual({"harvester/cluster/availability", "harvester/cluster/state",
                          "harvester/control/vm/vms/win11/state", "harvester/control/app/lab/frigate/state"},
                         {t for t, _ in client.sent})


class ApiBindingTests(unittest.TestCase):
    """Without a test's own, control reads and acts through what /api/v1 is bound to."""
    def test_items_and_commands_go_through_the_api(self):
        import homestead_api_v1 as API_V1
        import homestead_host_access as HOSTACCESS
        import homestead_workloads as WORKLOADS
        calls, roles = [], []
        saved, saved_sys = dict(API_V1.ctx), WORKLOADS.SYS_NS
        API_V1.ctx.update(
            workloads=lambda: [{"ns": "lab", "name": "frigate", "desired": 1}, {"ns": "lab", "name": "homestead", "desired": 1, "self": True},
                               {"ns": "kubevirt", "name": "virt-api", "desired": 2}],
            vms=lambda: [{"ns": "vms", "name": "win11", "running": False}],
            scale=lambda *a: calls.append(("scale",) + a) or roles.append(HOSTACCESS.role()) or {"warnings": ["tight on memory"]},
            restart=lambda *a: calls.append(("restart",) + a),
            vm_power=lambda *a: calls.append(("vm",) + a) or roles.append(HOSTACCESS.role()) or {"warnings": []})
        WORKLOADS.SYS_NS = frozenset({"kubevirt"})
        try:
            items = {i["id"]: i for i in DEFAULT_ITEMS()}
            self.assertEqual((True, "", False), (items["app:lab/frigate"]["on"], items["app:lab/frigate"]["refused"], items["vm:vms/win11"]["on"]))
            self.assertEqual("it is part of Homestead", items["app:lab/homestead"]["refused"])
            self.assertEqual("it runs in a system namespace", items["app:kubevirt/virt-api"]["refused"])
            self.assertEqual("tight on memory", DEFAULT_ACT("app", "lab", "frigate", "stop"))
            DEFAULT_ACT("vm", "vms", "win11", "force-off")
            DEFAULT_ACT("app", "lab", "frigate", "restart")
            with self.assertRaises(ValueError):
                DEFAULT_ACT("app", "lab", "frigate", "reset")
        finally:
            API_V1.ctx.clear()
            API_V1.ctx.update(saved)
            WORKLOADS.SYS_NS = saved_sys
        self.assertEqual([("scale", "lab", "frigate", 0), ("vm", "vms", "win11", "force-off"), ("restart", "lab", "frigate")], calls)
        self.assertEqual(["operator", "operator"], roles)


class VmReadFailsTests(unittest.TestCase):
    """#620: with power control on, a VM list that cannot be read is not "no
    VMs". The sensors and containers go on publishing, the VMs' entities stay
    as last published, and a VM command is refused, not acted on."""
    SNAP = {"cluster": {"health": "ok"},
            "nodes": [{"name": "node1", "cpu_pct": 5, "mem_pct": 9, "mem_gb": 3.2, "rx_mbps": 1, "tx_mbps": 2,
                       "pods": 20, "vms": 1, "wl": "frigate", "status": "Ready"}]}

    def setUp(self):
        import homestead_api_v1 as API_V1
        self.store = Store(self, {"enabled": True, "host": "192.0.2.177", "control": True,
                                  "controlled": ["vm:vms/win11", "app:lab/frigate"]})
        saved = dict(API_V1.ctx)
        self.addCleanup(lambda: (API_V1.ctx.clear(), API_V1.ctx.update(saved)))
        self.calls, self.vms_fail = [], False

        def vms():
            if self.vms_fail:
                raise RuntimeError("the VM list timed out")
            return [{"ns": "vms", "name": "win11", "running": True}]
        API_V1.ctx.update(workloads=lambda: [{"ns": "lab", "name": "frigate", "desired": 0}], vms=vms,
                          vm_power=lambda *a: self.calls.append(("vm",) + a) or {"warnings": []},
                          scale=lambda *a: self.calls.append(("scale",) + a) or {"warnings": []})
        MQTT.bind_control(DEFAULT_ITEMS, DEFAULT_ACT)
        MQTT._recent.clear()
        MQTT._minute.clear()
        audit_dir = self.enterContext(tempfile.TemporaryDirectory())
        AUDIT.bind(audit_dir)

    def test_sensors_and_containers_publish_while_the_vms_cannot_be_read(self):
        cfg, client, announced = MQTT.load(), FakeClient(), {}
        MQTT.publish_round(cfg, client, self.SNAP, announced)
        client.sent.clear()
        self.vms_fail = True
        count, unread = MQTT.publish_round(cfg, client, self.SNAP, announced)   # does not raise
        sent = dict(client.sent)
        self.assertEqual("online", sent["harvester/cluster/availability"])
        self.assertIn("harvester/cluster/state", sent)
        self.assertIn("harvester/node/node1/state", sent)
        self.assertEqual("OFF", sent["harvester/control/app/lab/frigate/state"])
        # The VM's switch and buttons are left as they were: not stated off, not retired.
        self.assertEqual([], [t for t, _ in client.sent if "vm" in t.split("/")[2:4] or "hs_vm_" in t])
        self.assertIn("timed out", unread["vm"])
        self.assertEqual(3, count)   # the cluster, node and container states
        # Read again: its state comes back, with no new announcement.
        client.sent.clear()
        self.vms_fail = False
        MQTT.publish_round(cfg, client, self.SNAP, announced)
        sent = dict(client.sent)
        self.assertEqual("ON", sent["harvester/control/vm/vms/win11/state"])
        self.assertNotIn("homeassistant/switch/hs_vm_vms_win11/config", sent)

    def test_a_vm_command_while_the_vms_cannot_be_read_is_refused_and_written_down(self):
        self.vms_fail = True
        cfg = MQTT.load()
        self.assertEqual("refused", MQTT.handle(cfg, "harvester/control/vm/vms/win11/power", b"OFF", now=1000))
        self.assertEqual([], self.calls)
        entry = AUDIT._read()[-1]
        self.assertEqual(("mqtt_vm_stop", "refused"), (entry["action"], entry["outcome"]))
        self.assertRegex(entry["detail"], "could not read the virtual machines.*timed out")
        # A container's command does not need the VM list.
        self.assertEqual("ok", MQTT.handle(cfg, "harvester/control/app/lab/frigate/power", b"ON", now=1000))
        self.assertEqual([("scale", "lab", "frigate", 1)], self.calls)


class ProtocolTests(unittest.TestCase):
    def test_packets_read_back(self):
        packet = MQTT.publish_packet("harvester/control/vm/vms/win11/power", "ON", retain=False)
        data = bytearray(packet)

        def recv(n):
            out = bytes(data[:n])
            del data[:n]
            return out
        first, body = MQTT.read_packet(recv)
        self.assertEqual(("harvester/control/vm/vms/win11/power", b"ON", False), MQTT.parse_publish(first, body))
        sub = MQTT.subscribe_packet(1, "harvester/control/+/+/+/+")
        self.assertEqual(0x82, sub[0])
        self.assertEqual(b"\x00", sub[-1:])

    def test_it_listens_on_the_connection_it_publishes_on(self):
        server = socket.socket()
        server.bind(("127.0.0.1", 0))
        server.listen(1)
        port = server.getsockname()[1]
        subscribed = []

        def broker():
            conn, _ = server.accept()
            conn.recv(1024)
            conn.sendall(b"\x20\x02\x00\x00")
            subscribed.append(conn.recv(1024))
            conn.sendall(b"\x90\x03\x00\x01\x00")      # SUBACK
            conn.sendall(MQTT.publish_packet("harvester/control/app/lab/frigate/power", "ON", retain=True))
            conn.sendall(MQTT.publish_packet("harvester/control/app/lab/frigate/power", "OFF", retain=False))
            conn.recv(1024)
            conn.close()
        thread = threading.Thread(target=broker, daemon=True)
        thread.start()
        got, done = [], threading.Event()

        def on_message(topic, payload, retained):
            got.append((topic, payload, retained))
            if len(got) == 2:
                done.set()
        client = MQTT.Client({"host": "127.0.0.1", "port": port}, "", "t").connect(timeout=5)
        client.listen("harvester/control/+/+/+/+", on_message)
        self.assertTrue(done.wait(5))
        client.close()
        thread.join(5)
        server.close()
        self.assertIn(b"harvester/control/+/+/+/+", subscribed[0])
        self.assertEqual([(b"ON", True), (b"OFF", False)], [(p, r) for _, p, r in got])
        self.assertFalse(client.lost.is_set())


if __name__ == "__main__":
    unittest.main()


class LinkedClusterTests(unittest.TestCase):
    def test_two_clusters_on_one_broker_keep_their_entities_apart_by_base_topic(self):
        item = [{"id": "vm:vms/win11", "kind": "vm", "ns": "vms", "name": "win11", "on": True}]
        home = dict(MQTT.control_discovery(MQTT.clean({"host": "b"}), item))
        lab = dict(MQTT.control_discovery(MQTT.clean({"host": "b", "base": "lab-cluster"}), item))
        self.assertIn("homeassistant/switch/hs_vm_vms_win11/config", home)          # the default keeps its ids
        self.assertIn("homeassistant/switch/hs_lab_cluster_vm_vms_win11/config", lab)
        self.assertEqual("lab-cluster/control/vm/vms/win11/power", lab["homeassistant/switch/hs_lab_cluster_vm_vms_win11/config"]["command_topic"])
        self.assertFalse(set(home) & set(lab))
        self.assertIn("homeassistant/button/hs_lab_cluster_vm_vms_win11_reset/config",
                      MQTT.retire_topics(MQTT.clean({"host": "b", "base": "lab-cluster"}), ["vm:vms/win11"]))
