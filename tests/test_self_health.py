import sys
import time
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
import server


class SambaSwitchTests(unittest.TestCase):
    def test_off_stops_serving_and_keeps_everything(self):
        sent = []
        dep = {"spec": {"replicas": 1, "template": {"spec": {"containers": [{"image": "dperson/samba"}]}}}, "status": {"readyReplicas": 1}}
        with mock.patch.object(server, "kget", lambda path, **k: dep if path.endswith("/deployments/samba") else {}), \
                mock.patch.object(server, "ksend", lambda *a, **k: sent.append(a)), \
                mock.patch.object(server.SHARES, "list_shares", lambda: [1, 2]):
            result = server.set_samba(False)
        self.assertEqual(("PATCH", f"/apis/apps/v1/namespaces/{server.SMB_NAMESPACE}/deployments/samba",
                          {"spec": {"replicas": 0}}), sent[0])
        self.assertIn("kept", result["detail"])

    def test_on_installs_samba_with_the_shares_already_defined(self):
        def missing(path, **k):
            raise urllib.error.HTTPError(path, 404, "missing", None, None)
        applied = []
        with mock.patch.object(server, "kget", missing), \
                mock.patch.object(server, "install_samba", lambda address="": {"metadata": {"name": "samba"}}), \
                mock.patch.object(server.SHARES, "list_shares", lambda: []), \
                mock.patch.object(server.SHARES, "_state", lambda: ([{"name": "media"}], {"media": {}}, None, None, None)), \
                mock.patch.object(server.SHARES, "apply_samba", lambda rows, creds, dep=None: applied.append(rows)):
            result = server.set_samba(True)
        self.assertEqual([[{"name": "media"}]], applied)
        self.assertIn("1 share", result["detail"])


class HeartbeatTests(unittest.TestCase):
    def setUp(self):
        self.saved = dict(server.HEART)
        server.HEART.clear()

    def tearDown(self):
        server.HEART.clear()
        server.HEART.update(self.saved)

    def test_each_task_says_whether_it_is_working(self):
        server.beat("sampler", 30)
        server.beat("alerts", 20, leader_only=True)
        server.beat("hardware", 30, RuntimeError("probe timed out"), leader_only=True)
        server.HEART["history"] = {"every": 30, "leader_only": True, "last_ok": time.time() - 3600, "error": "", "error_at": 0}
        with mock.patch.object(server, "kget", lambda path, **k: {}), \
                mock.patch.object(server.LEADER, "is_leader", lambda: True), \
                mock.patch.object(server, "homestead_replicas", lambda: {"desired": 1, "pods": []}), \
                mock.patch.object(server, "node_temps", lambda: {}), \
                mock.patch.object(server, "samba_state", lambda: {"installed": False}), \
                mock.patch.object(server.OBJECTS, "status", lambda: {}):
            health = server.self_health()
        states = {row["name"]: row["state"] for row in health["loops"]}
        self.assertEqual({"sampler": "ok", "alerts": "ok", "hardware": "failing", "history": "late", "moves": "starting"}, states)
        self.assertEqual("probe timed out", next(r for r in health["loops"] if r["name"] == "hardware")["error"])
        with mock.patch.object(server, "kget", lambda path, **k: {}), \
                mock.patch.object(server.LEADER, "is_leader", lambda: False), \
                mock.patch.object(server, "homestead_replicas", lambda: {}), \
                mock.patch.object(server, "node_temps", lambda: {}), \
                mock.patch.object(server, "samba_state", lambda: {}), \
                mock.patch.object(server.OBJECTS, "status", lambda: {}):
            standby = {r["name"]: r["state"] for r in server.self_health()["loops"]}
        self.assertEqual("standby", standby["alerts"], "the leader's work is not this copy's to do")


if __name__ == "__main__":
    unittest.main()
