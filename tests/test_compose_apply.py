"""Creating a Compose file's services: in order, all checked, stopping on trouble."""
import sys
import copy
import urllib.error
import unittest
from pathlib import Path
from unittest import mock


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
import server

STACK = """services:
  web:
    image: web
    ports: ["8080:80"]
    depends_on: [db]
    environment:
      DB_HOST: db
  db:
    image: postgres:16
"""


class ComposeApplyTests(unittest.TestCase):
    def setUp(self):
        self.created = []
        self.controllers = {}
        self.pods = []
        self.sets = []
        get = mock.patch.object(server, "kget", side_effect=self.get)
        features = mock.patch.object(server.HW, "features", return_value=[])
        deploy = mock.patch.object(server, "run_deploy", side_effect=self._deploy)
        nodes = [{"name": "host", "status": "Ready", "schedulable": True, "hardware": {},
                  "labels": {"kubernetes.io/hostname": "host"}, "allocatable": {"cpu": "8", "memory": "16Gi", "pods": "100"},
                  "mem_cap_gb": 16, "mem_used_gb": 1, "mem_metrics_available": True}]
        self.nodes = nodes
        for patcher in (get, features, deploy, mock.patch.object(server.PLACE, "get_nodes", return_value=nodes),
                        mock.patch.object(server.PLACE, "hardware_features", return_value=[]),
                        mock.patch.object(server.CAPACITY_REVIEW, "_key", return_value=b"test-compose-review")):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.fail_on = None

    def get(self, path):
        if path == "/api/v1/pods":
            return {"items": copy.deepcopy(self.pods)}
        if path.endswith("/replicasets"):
            return {"items": copy.deepcopy(self.sets)}
        if path == "/apis/apps/v1/namespaces/lab/deployments":
            return {"items": copy.deepcopy(list(self.controllers.values()))}
        if path.split("/")[-1] in self.controllers and "/deployments/" in path:
            return copy.deepcopy(self.controllers[path.split("/")[-1]])
        if path.endswith(("pods", "replicasets", "persistentvolumeclaims")):
            return {"items": []}
        raise urllib.error.HTTPError(path, 404, "missing", {}, None)

    def apply(self, body):
        token = server.compose_preview(body)["capacity_token"]
        return server.compose_apply({**body, "capacity_token": token, "confirm_capacity": True})

    def _deploy(self, cfg):
        if cfg["workload_name"] == self.fail_on:
            raise ValueError("the cluster said no")
        self.created.append(cfg["workload_name"])
        dep, _ = server.build_deployment(cfg)
        dep["metadata"]["uid"] = cfg["workload_name"] + "-uid"
        self.controllers[cfg["workload_name"]] = dep
        return {"ok": True, "name": cfg["workload_name"]}

    def test_dependencies_are_created_first(self):
        result = self.apply({"text": STACK, "namespace": "lab"})

        self.assertEqual({"ok": True, "created": ["db", "web"]}, result)
        self.assertEqual(["db", "web"], self.created)

    def test_it_stops_at_the_first_failure_and_says_what_was_made(self):
        self.fail_on = "web"

        result = self.apply({"text": STACK, "namespace": "lab"})

        self.assertEqual((False, ["db"], "web"), (result["ok"], result["created"], result["failed"]))
        self.assertIn("said no", result["error"])

    def test_a_file_with_errors_creates_nothing(self):
        with self.assertRaisesRegex(ValueError, "fix the file first"):
            self.apply({"text": "services:\n  a:\n    build: .\n", "namespace": "lab"})
        self.assertEqual([], self.created)

    def test_only_the_chosen_services(self):
        result = self.apply({"text": STACK, "namespace": "lab", "services": ["db"]})

        self.assertEqual(["db"], result["created"])

    def test_a_namespace_must_be_a_name(self):
        with self.assertRaisesRegex(ValueError, "namespace"):
            server.compose_report({"text": STACK, "namespace": "../etc"})

    def test_what_was_checked_is_what_is_created(self):
        """The page's copy of the report is never trusted; the text is read again."""
        self.apply({"text": STACK, "namespace": "lab",
                              "report": {"services": [{"name": "evil", "config": {"image": "x"}}]}})

        self.assertNotIn("evil", self.created)

    def test_apply_requires_review_token_before_any_creation(self):
        with self.assertRaises(server.CAPACITY_REVIEW.Rejected):
            server.compose_apply({"text": STACK, "namespace": "lab", "confirm_capacity": True})
        self.assertEqual([], self.created)

    def test_changed_file_or_selection_invalidates_review(self):
        body = {"text": STACK, "namespace": "lab"}
        token = server.compose_preview(body)["capacity_token"]
        for change in ({"text": STACK.replace("image: web", "image: changed")}, {"services": ["db"]}):
            with self.subTest(change=change), self.assertRaises(server.CAPACITY_REVIEW.Rejected):
                server.compose_apply({**body, **change, "capacity_token": token, "confirm_capacity": True})
        self.assertEqual([], self.created)

    def test_joint_shortfall_writes_nothing_even_with_signed_ack(self):
        text = "services:\n  one:\n    image: app\n    mem_reservation: 10Gi\n    mem_limit: 10Gi\n  two:\n    image: app\n    mem_reservation: 10Gi\n    mem_limit: 10Gi\n"
        with self.assertRaises(server.CAPACITY_REVIEW.Rejected):
            self.apply({"text": text, "namespace": "lab"})
        self.assertEqual([], self.created)

    def test_parser_refuses_missing_or_paginated_inventory(self):
        for result in ({}, {"items": [], "metadata": {"continue": "next"}}):
            with mock.patch.object(server, "kget", return_value=result), self.assertRaises(ValueError):
                server.compose_report({"text": STACK, "namespace": "lab"})

    def test_unknown_selected_service_does_not_silently_create_subset(self):
        with self.assertRaisesRegex(ValueError, "no longer exists"):
            self.apply({"text": STACK, "namespace": "lab", "services": ["db", "missing"]})
        self.assertEqual([], self.created)

    def test_controller_without_pods_remains_reserved_before_next_service(self):
        text = "services:\n  one:\n    image: app\n    mem_reservation: 6Gi\n    mem_limit: 6Gi\n  two:\n    image: app\n    mem_reservation: 6Gi\n    mem_limit: 6Gi\n"
        def deploy(cfg):
            result = self._deploy(cfg)
            self.pods.append({"metadata": {"namespace": "other", "name": "competitor"},
                              "spec": {"nodeName": "host", "containers": [{"name": "other", "resources": {"requests": {"memory": "5Gi"}}}]}})
            return result
        with mock.patch.object(server, "run_deploy", side_effect=deploy):
            result = self.apply({"text": text, "namespace": "lab"})
        self.assertFalse(result["ok"])
        self.assertEqual(["one"], result["created"])
        self.assertEqual(["one"], self.created)
        self.assertIn("one", self.controllers, "no automatic rollback/delete")

    def test_observed_pods_are_not_double_counted(self):
        text = "services:\n  one:\n    image: app\n    mem_reservation: 6Gi\n    mem_limit: 6Gi\n  two:\n    image: app\n    mem_reservation: 6Gi\n    mem_limit: 6Gi\n"
        def deploy(cfg):
            result = self._deploy(cfg)
            name = result["name"]
            self.sets.append({"metadata": {"uid": name + "-rs", "namespace": "lab", "ownerReferences": [
                {"kind": "Deployment", "controller": True, "uid": name + "-uid"}]}})
            self.pods.append({"metadata": {"namespace": "lab", "name": name + "-pod", "ownerReferences": [
                {"kind": "ReplicaSet", "controller": True, "uid": name + "-rs"}]},
                "spec": {**copy.deepcopy(self.controllers[name]["spec"]["template"]["spec"]), "nodeName": "host"}})
            return result
        with mock.patch.object(server, "run_deploy", side_effect=deploy):
            result = self.apply({"text": text, "namespace": "lab"})
        self.assertTrue(result["ok"], result)
        self.assertEqual(["one", "two"], result["created"])

    def test_unknown_created_controller_identity_stops_with_truthful_partial_result(self):
        def deploy(cfg):
            result = self._deploy(cfg)
            self.controllers[result["name"]]["metadata"].pop("uid")
            return result
        with mock.patch.object(server, "run_deploy", side_effect=deploy):
            result = self.apply({"text": STACK, "namespace": "lab"})
        self.assertFalse(result["ok"])
        self.assertEqual(["db"], result["created"])

    def test_preview_api_returns_only_read_only_review(self):
        handler = object.__new__(server.H)
        handler.path, handler.headers = "/api/compose/preview", {}
        handler._guard = lambda path: False
        handler._body = lambda: {"text": STACK, "namespace": "lab"}
        handler._client_ip = lambda: "127.0.0.1"
        handler._send = mock.Mock()
        with mock.patch.object(server, "ksend") as send:
            handler.do_POST()
        send.assert_not_called()
        self.assertEqual([], self.created)
        self.assertEqual(200, handler._send.call_args.args[0])
        self.assertIn("capacity_token", handler._send.call_args.args[1])


if __name__ == "__main__":
    unittest.main()
