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
            return {"url": "s3://b@us-east-1/", "reachable_off_cluster": True, "endpoint": "http://192.0.2.244:9000",
                    "credentials": {"AWS_SECRET_ACCESS_KEY": "x"}}
        with mock.patch.object(move, "remote", remote), \
                mock.patch.object(move, "answers", lambda endpoint, timeout=3: endpoint == "http://192.0.2.244:9000"), \
                mock.patch.object(move, "check_cluster", lambda name: {"compatible": True}):
            r = move.readiness("oldcluster")
        self.assertTrue(r["ready"])
        self.assertNotIn("credentials", r["target"])

    def test_storage_is_set_up_over_there_with_the_stored_account(self):
        calls = []

        def remote(name, path, body=None):
            calls.append((name, path, body))
            if path == "/api/move/target":
                if not any(c[1] == "/api/lh/target" for c in calls):
                    raise ValueError("oldcluster: this cluster has no Longhorn backup target")
                return {"endpoint": "http://192.0.2.244:9000"}
            if path == "/api/objectstore":
                return {"backup_url": "s3://homestead-backups@us-east-1/"}
            return {"endpoint": "http://192.0.2.244:9000", "longhorn": {"secret": "homestead-backup-credentials"}}
        with mock.patch.object(move, "remote", remote), \
                mock.patch.object(move, "answers", lambda endpoint, timeout=3: True), \
                mock.patch.object(move, "check_cluster", lambda name: {"version": "2.8.111"}):
            r = move.setup_storage("oldcluster", 50, "192.0.2.244")
        self.assertIn(("oldcluster", "/api/objectstore/deploy",
                       {"size_gb": 50, "lb_ip": "192.0.2.244", "point_longhorn": True}), calls)
        # An older Homestead over there never set its target; this side does.
        self.assertIn(("oldcluster", "/api/lh/target", {"url": "s3://homestead-backups@us-east-1/",
                       "secret": "homestead-backup-credentials", "poll": "5m"}), calls)
        self.assertIn("192.0.2.244:9000", r["detail"])

    def test_an_older_homestead_is_asked_to_update_before_it_makes_storage_that_cannot_start(self):
        calls = []
        with mock.patch.object(move, "remote", lambda *a, **k: calls.append(a)), \
                mock.patch.object(move, "check_cluster", lambda name: {"version": "2.8.100"}):
            with self.assertRaisesRegex(ValueError, "MinIO's images can no longer be downloaded"):
                move.setup_storage("oldcluster")
        self.assertFalse([c for c in calls if c[1] == "/api/objectstore/deploy"], "nothing is deployed that could not start")

    def test_a_running_store_on_an_older_homestead_can_still_be_given_its_address(self):
        calls = []

        def remote(name, path, body=None):
            calls.append((name, path, body))
            if path == "/api/objectstore":
                return {"ready": True, "backup_url": "s3://homestead-backups@us-east-1/"}
            return {"endpoint": "http://192.0.2.243:9000"}
        with mock.patch.object(move, "remote", remote), \
                mock.patch.object(move, "answers", lambda endpoint, timeout=3: True), \
                mock.patch.object(move, "check_cluster", lambda name: {"version": "2.8.100"}):
            move.setup_storage("oldcluster", 100, "192.0.2.243")
        self.assertIn(("oldcluster", "/api/objectstore/deploy",
                       {"size_gb": 100, "lb_ip": "192.0.2.243", "point_longhorn": True}), calls)

    def test_an_address_that_did_not_take_is_said_not_assumed(self):
        def remote(name, path, body=None):
            if path == "/api/objectstore":
                return {"ready": True}
            if path == "/api/move/target":
                return {"endpoint": "http://homestead-objectstore.lab.svc:9000"}
            return {"endpoint": ""}
        with mock.patch.object(move, "remote", remote), \
                mock.patch.object(move, "check_cluster", lambda name: {"version": "2.8.113"}):
            with self.assertRaisesRegex(ValueError, "did not take the address"):
                move.setup_storage("oldcluster", 100, "192.0.2.243")

    def test_an_address_this_cluster_cannot_reach_is_reported(self):
        def remote(name, path, body=None):
            if path == "/api/objectstore":
                return {"ready": True}
            return {"endpoint": "http://192.0.2.243:9000"}
        with mock.patch.object(move, "remote", remote), \
                mock.patch.object(move, "answers", lambda endpoint, timeout=3: False), \
                mock.patch.object(move.time, "sleep", lambda s: None), \
                mock.patch.object(move, "check_cluster", lambda name: {"version": "2.8.113"}):
            result = move.setup_storage("oldcluster", 100, "192.0.2.243")
        self.assertFalse(result["answers"])
        self.assertIn("cannot reach it yet", result["detail"])

    def test_an_in_cluster_address_never_counts_as_reachable(self):
        self.assertFalse(move.answers("http://homestead-objectstore.lab.svc:9000"))
        self.assertFalse(move.answers(""))

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
