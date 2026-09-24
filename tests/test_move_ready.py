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

        def remote(name, path, body=None):
            calls.append((name, path, body))
            if path == "/api/move/target":
                raise ValueError("oldcluster: this cluster has no Longhorn backup target")
            if path == "/api/objectstore":
                return {"backup_url": "s3://homestead-backups@us-east-1/"}
            return {"endpoint": "http://192.0.2.244:9000", "longhorn": {"secret": "homestead-backup-credentials"}}
        with mock.patch.object(move, "remote", remote),                 mock.patch.object(move, "check_cluster", lambda name: {"version": "2.8.111"}):
            r = move.setup_storage("oldcluster", 50, "192.0.2.244")
        self.assertEqual(("oldcluster", "/api/objectstore/deploy",
                          {"size_gb": 50, "lb_ip": "192.0.2.244", "point_longhorn": True}), calls[0])
        # An older Homestead over there never set its target; this side does.
        self.assertEqual(("oldcluster", "/api/lh/target", {"url": "s3://homestead-backups@us-east-1/",
                          "secret": "homestead-backup-credentials", "poll": "5m"}), calls[-1])
        self.assertIn("192.0.2.244:9000", r["detail"])

    def test_an_older_homestead_is_asked_to_update_before_it_makes_storage_that_cannot_start(self):
        calls = []
        with mock.patch.object(move, "remote", lambda *a, **k: calls.append(a)),                 mock.patch.object(move, "check_cluster", lambda name: {"version": "2.8.100"}):
            with self.assertRaisesRegex(ValueError, "MinIO's images can no longer be downloaded"):
                move.setup_storage("oldcluster")
        self.assertEqual([], calls, "nothing is deployed that could not start")

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
