import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
import homestead_move as move
import homestead_move_engine as engine


class MoveReadinessTests(unittest.TestCase):
    def test_a_cluster_without_backup_storage_says_what_is_missing(self):
        def remote(name, path, body=None):
            if path == "/api/objectstore":
                return {"deployed": False, "ready": False}
            raise ValueError("oldcluster: this cluster has no Longhorn backup target; set up backup storage first")
        with mock.patch.object(move, "remote", remote), \
                mock.patch.object(move, "check_cluster", lambda name: {"compatible": True, "state": "same"}):
            r = move.readiness("oldcluster")
        self.assertFalse(r["ready"])
        self.assertFalse(r["target"]["configured"])
        self.assertEqual({"deployed": False, "ready": False, "endpoint": None, "reachable_off_cluster": None}, r["storage"])

    def test_a_ready_cluster_keeps_its_keys_to_itself(self):
        def remote(name, path, body=None):
            if path == "/api/objectstore":
                return {"deployed": True, "ready": True, "endpoint": "http://192.0.2.244:9000", "reachable_off_cluster": True}
            return {"url": "s3://b@us-east-1/", "reachable_off_cluster": True, "credentials": {"AWS_SECRET_ACCESS_KEY": "x"}}
        with mock.patch.object(move, "remote", remote), \
                mock.patch.object(move, "check_cluster", lambda name: {"compatible": True}):
            r = move.readiness("oldcluster")
        self.assertTrue(r["ready"])
        self.assertNotIn("credentials", r["target"])

    def test_storage_is_set_up_over_there_with_the_stored_account(self):
        calls = []
        with mock.patch.object(move, "remote", lambda name, path, body=None: calls.append((name, path, body)) or
                               {"endpoint": "http://192.0.2.244:9000"}):
            r = move.setup_storage("oldcluster", 50, "192.0.2.244")
        self.assertEqual([("oldcluster", "/api/objectstore/deploy",
                           {"size_gb": 50, "lb_ip": "192.0.2.244", "point_longhorn": True})], calls)
        self.assertIn("192.0.2.244:9000", r["detail"])

    def test_the_move_review_offers_the_fix(self):
        def remote(name, path, body=None):
            if path.startswith("/api/move/definition"):
                return {"claims": []}
            raise ValueError("oldcluster: this cluster has no Longhorn backup target; set up backup storage first")
        with mock.patch.object(engine.CLIENT, "check_cluster", lambda name: {"compatible": True, "state": "same"}), \
                mock.patch.object(engine.CLIENT, "remote", remote):
            plan = engine.plan("oldcluster", "container", "ha-screenshot")
        self.assertFalse(plan["ok"])
        self.assertEqual([{"kind": "source-storage", "cluster": "oldcluster"}], plan["fixes"])


if __name__ == "__main__":
    unittest.main()
