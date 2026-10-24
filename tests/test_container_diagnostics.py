"""Crash-loop evidence and commands approved for one exact workload and pod."""
import copy
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
import homestead_container_diagnostics as D
import homestead_routes as ROUTER


class DiagnosticsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.dep = {"metadata": {"name": "game", "uid": "app-uid"}, "spec": {"replicas": 1,
            "selector": {"matchLabels": {"app": "game"}}, "template": {"spec": {"containers": [{"name": "server", "image": "example/server:1"}]}}}}
        self.rs = {"metadata": {"uid": "rs-uid", "ownerReferences": [{"uid": "app-uid", "controller": True}]}}
        self.pod = {"metadata": {"name": "game-abc-123", "uid": "pod-uid", "ownerReferences": [{"uid": "rs-uid", "controller": True}]},
                    "spec": {"containers": [{"name": "server", "image": "example/server:1"}]},
                    "status": {"phase": "Running", "containerStatuses": [{"name": "server", "restartCount": 8,
                        "state": {"running": {"startedAt": "2026-01-01T00:00:00Z"}},
                        "lastState": {"terminated": {"reason": "Error", "exitCode": 127}}}]}}
        self.pods = [self.pod]
        def get(path):
            if "/deployments/" in path:
                return copy.deepcopy(self.dep)
            return {"items": copy.deepcopy([self.rs] if "/replicasets?" in path else self.pods)}
        patches = [mock.patch.object(D.WORKLOADS, "kget", get, create=True),
                   mock.patch.object(D.WORKLOADS, "homestead_part", return_value="", create=True),
                   mock.patch.object(D.WORKLOADS, "guard_self", create=True),
                   mock.patch.object(D.WORKLOADS, "guard_managed_smb", create=True),
                   mock.patch.object(D.WORKLOADS, "DATA_DIR", self.temp.name),
                   mock.patch.object(D.REVIEW, "_key", lambda: b"test-key"),
                   mock.patch.object(D.CONSOLE, "audit")]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)
        self.cfg = {"ns": "apps", "name": "game", "container": "server", "argv": ["sh", "-c", "uname -a"], "timeout_seconds": 10}

    def approved(self):
        r = D.preview(self.cfg, "ada")
        return {**r["settings"], "review_token": r["review_token"], "confirm": True}

    def test_crash_loop_keeps_exit_reason_and_previous_logs_when_current_logs_fail(self):
        self.pod["status"]["containerStatuses"][0]["state"] = {"waiting": {"reason": "CrashLoopBackOff"}}
        with mock.patch.object(D, "log", side_effect=[{"error": "not started"}, {"text": "missing shared library"}]) as logs:
            answer = D.report(self.cfg)
        self.assertEqual(127, answer["pods"][0]["containers"][0]["last_state"]["terminated"]["exitCode"])
        self.assertEqual(8, answer["pods"][0]["containers"][0]["restarts"])
        self.assertEqual("missing shared library", answer["logs"]["previous"]["text"])
        self.assertTrue(logs.call_args.args[3])

    def test_the_restart_record_comes_with_the_diagnostics(self):
        kept = {"since": 1790000000, "records": [{"at": "2026-10-22T09:00:00Z", "kind": "restart", "reason": "Ran out of memory"}]}
        with mock.patch.object(D, "log", return_value={"text": ""}), \
                mock.patch.object(D.RESTARTS, "for_app", lambda ns, name, kind, uid: kept if (ns, name, kind, uid) == ("apps", "game", "Deployment", "app-uid") else None):
            self.assertEqual(kept, D.report(self.cfg)["restart_history"])

    def test_a_restart_record_that_cannot_be_read_says_so_never_none(self):
        def unreadable(ns, name, kind, uid):
            raise D.SHARED.Unreadable("Homestead could not read restarts.json on its data volume")
        with mock.patch.object(D, "log", return_value={"text": ""}), mock.patch.object(D.RESTARTS, "for_app", unreadable):
            history = D.report(self.cfg)["restart_history"]
        self.assertNotIn("records", history)
        self.assertIn("could not read restarts.json", history["error"])

    def test_with_no_pod_the_restart_record_is_still_there(self):
        self.pods = []
        with mock.patch.object(D.RESTARTS, "for_app", lambda ns, name, kind, uid: {"since": 1, "records": [{"reason": "Evicted"}]}):
            self.assertEqual("Evicted", D.report(self.cfg)["restart_history"]["records"][0]["reason"])

    def test_same_labels_do_not_allow_unowned_pods_or_explicit_foreign_selection(self):
        foreign = copy.deepcopy(self.pod)
        foreign["metadata"].update(name="other-123", ownerReferences=[{"uid": "unrelated", "controller": True}])
        self.pods = [foreign]
        self.assertEqual([], D.report(self.cfg)["pods"])
        with self.assertRaisesRegex(ValueError, "no longer belongs"):
            D.report({**self.cfg, "pod": "other-123"})

    def test_no_pod_does_not_start_or_execute_anything(self):
        self.pods = []
        with mock.patch.object(D, "capture") as capture:
            self.assertIn("No pod", D.report(self.cfg)["message"])
            with self.assertRaisesRegex(ValueError, "no current pod"):
                D.preview(self.cfg, "ada")
        capture.assert_not_called()

    def test_changed_command_target_or_user_cannot_reuse_approval(self):
        cfg = self.approved()
        with mock.patch.object(D, "capture") as capture:
            for change in ({"argv": ["rm", "-rf", "/data"]}, {"timeout_seconds": 30}):
                with self.assertRaisesRegex(ValueError, "review"):
                    D.execute({**cfg, **change}, "ada")
            with self.assertRaisesRegex(ValueError, "review"):
                D.execute(cfg, "eve")
            self.pod["metadata"]["uid"] = "replacement"
            with self.assertRaisesRegex(ValueError, "review"):
                D.execute(cfg, "ada")
        capture.assert_not_called()

    def test_waiting_container_is_not_executed(self):
        cfg = self.approved()
        self.pod["status"]["containerStatuses"][0]["state"] = {"waiting": {"reason": "CrashLoopBackOff"}}
        with self.assertRaisesRegex(ValueError, "not running"):
            D.execute(cfg, "ada")

    def test_homestead_workloads_are_excluded_even_for_admins(self):
        for part in ("self", "smb", "nfs", "objectstore"):
            D.WORKLOADS.homestead_part.return_value = part
            with self.assertRaisesRegex(ValueError, "Homestead's own workloads"):
                D.preview(self.cfg, "admin")

    def test_explicit_confirmation_and_single_use_are_required(self):
        cfg = self.approved()
        with self.assertRaisesRegex(ValueError, "review"):
            D.execute({**cfg, "confirm": False}, "ada")
        result = {"exit_code": 0, "timed_out": False, "stdout": "Linux", "stderr": ""}
        with mock.patch.object(D, "capture", return_value=result) as capture:
            self.assertEqual("Linux", D.execute(cfg, "ada")["stdout"])
            with self.assertRaisesRegex(ValueError, "already been used"):
                D.execute(cfg, "ada")
        capture.assert_called_once()

    def test_malformed_arguments_and_limits_are_rejected(self):
        for change in ({"argv": []}, {"argv": "uname -a"}, {"argv": ["x\x00"]},
                       {"argv": ["x" * 8193]}, {"timeout_seconds": True}, {"timeout_seconds": 31}):
            with self.assertRaises(ValueError):
                D.preview({**self.cfg, **change}, "ada")

    def test_execution_route_is_admin_only_and_evidence_is_viewer_readable(self):
        self.assertEqual("admin", ROUTER.role("/api/container/command", "POST"))
        self.assertEqual("viewer", ROUTER.role("/api/container/diagnostics", "GET"))

    def test_capture_preserves_arguments_caps_output_and_reports_nonzero_exit(self):
        sock = mock.Mock()
        frames = [(True, 2, b"\x01" + b"x" * (D.LIMIT + 10)), (True, 2, b"\x02error"),
                  (True, 2, b"\x03" + json.dumps({"status": "Failure", "message": "exit 127", "details": {"causes": [{"reason": "ExitCode", "message": "127"}]}}).encode())]
        with mock.patch.object(D.CONSOLE.ConsoleProxy, "connect_upstream", return_value=sock) as connect, \
                mock.patch.object(D.CONSOLE, "read_frame", side_effect=frames):
            result = D.capture("apps", "game-abc-123", "server", ["printf", "a;b $(x)"], 10)
        self.assertEqual(127, result["exit_code"])
        self.assertTrue(result["truncated"])
        self.assertEqual(D.LIMIT, len(result["stdout"]))
        query = D.urllib.parse.parse_qs(connect.call_args.args[0].split("?", 1)[1])
        self.assertEqual(["timeout", "-s", "TERM", "-k", "2", "10", "printf", "a;b $(x)"], query["command"])
        self.assertNotIn("stdin", query)
        sock.close.assert_called_once()

    def test_disconnected_command_is_not_reported_as_success(self):
        with mock.patch.object(D.CONSOLE.ConsoleProxy, "connect_upstream", return_value=mock.Mock()), \
                mock.patch.object(D.CONSOLE, "read_frame", side_effect=EOFError("disconnected")):
            result = D.capture("apps", "game-abc-123", "server", ["uname"], 1)
        self.assertIsNone(result["exit_code"])
        self.assertIn("disconnected", result["error"])

    def test_log_fetch_bounds_bytes_and_requests_previous_instance(self):
        response = io.BytesIO(b"x" * (D.LIMIT + 50))
        with mock.patch.object(D.urllib.request, "urlopen", return_value=response) as fetch, \
                mock.patch.object(D.WORKLOADS, "API", "https://example.com"):
            result = D.log("apps", "pod", "server", True, 100)
        self.assertEqual(D.LIMIT, len(result["text"]))
        self.assertTrue(result["truncated"])
        self.assertIn("previous=true", fetch.call_args.args[0].full_url)


if __name__ == "__main__":
    unittest.main()
