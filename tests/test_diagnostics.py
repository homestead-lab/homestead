"""Diagnostic exports preserve evidence without sharing private originals."""
import io
import json
import sys
import tempfile
import time
import unittest
import zipfile
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
import homestead_diagnostics as D
import homestead_route_policy as POLICY


class DiagnosticsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.reads = []
        self.jobs = Mock()
        self.jobs.diagnostic_summaries.return_value = [{"id": "job-1", "kind": "restart", "title": "Restart photos", "status": "failed", "message": "password=job-secret"}]
        D.bind(self.temp.name, self.read, self.jobs, "2.8.293", "lab", "homestead-abc")
        self.id = D.create("alice")["id"]

    def read(self, path, timeout=5):
        self.reads.append(path)
        if "/nodes?" in path:
            return {"items": [{"metadata": {"name": "private-node"}, "status": {"conditions": [{"type": "Ready", "status": "True"}]}}]}
        if "previous=true" in path:
            raise OSError("no previous pod")
        if "/log?" in path:
            return "node=private-node password=secret-value Authorization: Bearer original-token\naddress=192.0.2.10 alice@example.com\n"
        if "/events?" in path:
            return {"items": [{"message": "private-node could not start photos", "involvedObject": {"name": "photos", "namespace": "lab", "kind": "Pod"}}]}
        raise AssertionError(path)

    def ready(self):
        D.append(self.id, "alice", 1, [{"kind": "click", "at": 100, "action": "restart", "operation": "job-1", "value": "never-collect"}])
        D.stop(self.id, "alice")
        D.prepare(self.id, "alice", "private-node failed", "password=comment-secret on private-node", ["service", "cluster", "jobs"])

    def test_anonymised_and_full_packages_preserve_same_timeline(self):
        self.ready()
        name, mime, raw = D.export(self.id, "alice", "full", True)
        self.assertTrue(name.endswith("-full.zip"))
        self.assertEqual("application/zip", mime)
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            full = archive.read("report.log").decode()
            self.assertIn("secret-value", full)
            self.assertIn("private-node", full)
            self.assertNotIn("never-collect", full)
            self.assertNotIn("not-collected", full)
        _, _, raw = D.export(self.id, "alice", "anonymised", True)
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            all_text = "\n".join(archive.read(name).decode() for name in archive.namelist())
            for secret in ("secret-value", "original-token", "private-node", "192.0.2.10", "alice@example.com", "comment-secret", "job-secret"):
                self.assertNotIn(secret, all_text)
            self.assertIn('"action": "restart"', all_text)
            for name in archive.namelist():
                if name.endswith(".json"):
                    json.loads(archive.read(name))
        self.assertTrue(all("secrets" not in path for path in self.reads))

    def test_every_route_is_admin_only(self):
        for (method, path), role in POLICY.POLICY.items():
            if path.startswith("/api/diagnostics"):
                self.assertEqual("admin", role, (method, path))

    def test_reports_cannot_be_read_changed_or_deleted_by_another_admin(self):
        self.assertEqual([], D.listing("bob"))
        for call in (lambda: D.snapshot(self.id, "bob"), lambda: D.append(self.id, "bob", 1, []),
                     lambda: D.stop(self.id, "bob"), lambda: D.delete(self.id, "bob"), lambda: D.issue(self.id, "bob")):
            with self.assertRaises(PermissionError):
                call()

    def test_expiry_removes_originals_and_disallows_download(self):
        with patch.object(D.time, "time", return_value=time.time() + D.TTL + 1):
            with self.assertRaises(ValueError):
                D.export(self.id, "alice", "full")
            D.cleanup()
        self.assertFalse(Path(D._path(self.id)).exists())

    def test_retrying_an_acknowledged_batch_does_not_duplicate_events(self):
        event = {"kind": "click", "at": 10}
        D.append(self.id, "alice", 1, [event])
        D.append(self.id, "alice", 1, [event])
        self.assertEqual(1, len(D.snapshot(self.id, "alice")["events"]))
        with self.assertRaises(ValueError):
            D.append(self.id, "alice", 3, [])

    def test_event_limit_is_reported_and_stop_still_works(self):
        with patch.object(D, "MAX_EVENTS", 1):
            D.append(self.id, "alice", 1, [{"kind": "click"}, {"kind": "click"}])
        row = D.stop(self.id, "alice")
        self.assertTrue(row["truncated"])
        self.assertEqual(1, row["events"])

    def test_query_and_unrecognised_fields_are_never_stored(self):
        D.append(self.id, "alice", 1, [{"kind": "request", "path": "/api/nodes?token=secret#extra", "body": "secret", "headers": {"password": "secret"}}])
        text = json.dumps(D.snapshot(self.id, "alice", "full"))
        self.assertNotIn("secret", text)
        self.assertIn("/api/nodes", text)

    def test_interrupted_recording_is_recoverable_after_server_restart(self):
        with patch.object(D.time, "time", return_value=time.time() + 120):
            self.assertEqual("interrupted", D.listing("alice")[0]["status"])
        self.assertEqual("draft", D.stop(self.id, "alice")["status"])

    def test_late_delivery_saves_already_captured_events_without_extending_limit(self):
        with patch.object(D.time, "time", return_value=time.time() + 900):
            D.append(self.id, "alice", 1, [{"kind": "click", "at": 100}, {"kind": "click", "at": 700000}])
            row = D.stop(self.id, "alice")
        self.assertEqual(1, row["events"])

    def test_collection_failure_is_listed_without_discarding_the_recording(self):
        self.ready()
        row = D.snapshot(self.id, "alice")
        self.assertIn({"source": "homestead-previous.log", "state": "unavailable"}, row["manifest"])
        self.assertEqual(1, len(row["events"]))

    def test_anonymiser_failure_cannot_fall_back_to_full_export_or_issue(self):
        self.ready()
        with patch.object(D.Anonymiser, "value", side_effect=ValueError("cannot sanitise")):
            with self.assertRaises(ValueError):
                D.export(self.id, "alice", "anonymised")
            with self.assertRaises(ValueError):
                D.issue(self.id, "alice")
            self.assertIn(b"secret-value", D.export(self.id, "alice", "full")[2])

    def test_issue_is_independently_anonymised_and_contains_exact_preview(self):
        self.ready()
        issue = D.issue(self.id, "alice")
        self.assertNotIn("private-node", json.dumps(issue))
        self.assertNotIn("comment-secret", json.dumps(issue))
        parsed = D.urllib.parse.parse_qs(D.urllib.parse.urlparse(issue["url"]).query)
        self.assertEqual(issue["title"], parsed["title"][0])
        self.assertEqual(issue["body"], parsed["body"][0])
        self.assertLess(len(issue["url"]), 1801)

    def test_draft_survives_without_collecting_logs(self):
        D.stop(self.id, "alice")
        D.draft(self.id, "alice", "UI freezes", "Expected the dialog to close")
        self.assertEqual("UI freezes", D.snapshot(self.id, "alice")["title"])
        self.assertEqual([], self.reads)

    def test_traversal_and_unsupported_formats_are_rejected(self):
        for report in ("../other", "", None):
            with self.assertRaises(ValueError):
                D.snapshot(report, "alice")
        with self.assertRaises(ValueError):
            D.export(self.id, "alice", "raw")

    def test_storage_has_a_global_limit(self):
        with patch.object(D, "MAX_RECORDS", 1):
            with self.assertRaises(ValueError):
                D.create("bob")

    def test_addresses_are_anonymised_without_losing_log_timestamps(self):
        text = D.Anonymiser([]).text("2026-10-02T12:30:40Z 2001:db8::1 192.0.2.10 aa:bb:cc:dd:ee:ff")
        self.assertIn("2026-10-02T12:30:40Z", text)
        for address in ("2001:db8::1", "192.0.2.10", "aa:bb:cc:dd:ee:ff"):
            self.assertNotIn(address, text)

    def test_job_evidence_never_refreshes_jobs_or_reads_console_output(self):
        import homestead_operations as OPS
        row = {"id": "job-1", "history": [{"message": "Started"}], "ref": {"password": "never-copy"}, "execution": "never-copy"}
        with patch.object(OPS, "DATA_DIR", self.temp.name), patch.object(OPS, "_read", return_value=[row]), patch.object(OPS, "_refresh", side_effect=AssertionError("must not refresh")):
            result = OPS.diagnostic_summaries({"job-1"})
        self.assertEqual([{"message": "Started"}], result[0]["history"])
        self.assertNotIn("never-copy", json.dumps(result))

    def request(self, path, method="GET", body=None, role="admin", owner="alice", csrf=True):
        import server
        D.bind(self.temp.name, self.read, self.jobs, "2.8.293", "lab", "homestead-abc")
        handler = object.__new__(server.H)
        handler.path, handler.command = path, method
        handler.headers = {"X-Homestead-Auth": "1"} if csrf else {}
        handler._who = Mock(return_value={"user": owner, "role": role})
        handler._fleet_target = Mock(return_value="")
        handler._body = Mock(return_value=body or {})
        handler._client_ip = Mock(return_value="127.0.0.1")
        handler._send = Mock()
        with patch.object(server.CFACCESS, "enabled", return_value=False), patch.object(server, "require_self_data_write"):
            (handler.do_POST if method == "POST" else handler.do_GET)()
        return handler._send.call_args.args, handler

    def test_http_routes_enforce_role_csrf_and_ownership(self):
        for role in ("viewer", "operator"):
            self.assertEqual(403, self.request("/api/diagnostics", role=role)[0][0])
            self.assertEqual(403, self.request("/api/diagnostics/start", "POST", role=role)[0][0])
        self.assertEqual(403, self.request("/api/diagnostics/start", "POST", csrf=False)[0][0])
        self.assertEqual(403, self.request(f"/api/diagnostics/report?id={self.id}", owner="bob")[0][0])
        self.assertEqual(403, self.request(f"/api/diagnostics/download?id={self.id}&format=full", owner="bob")[0][0])

    def test_http_download_returns_an_attachment_and_real_zip(self):
        self.ready()
        response, handler = self.request(f"/api/diagnostics/download?id={self.id}&format=full&package=1")
        self.assertEqual((200, "application/zip"), (response[0], response[2]))
        self.assertTrue(zipfile.is_zipfile(io.BytesIO(response[1])))
        self.assertTrue(any(key == "Content-Disposition" and "-full.zip" in value for key, value in handler._extra_headers))

    def test_invalid_format_and_id_are_client_errors(self):
        self.assertEqual(400, self.request(f"/api/diagnostics/download?id={self.id}&format=unsafe")[0][0])
        self.assertEqual(400, self.request("/api/diagnostics/report?id=../bad")[0][0])

    def test_batches_and_server_observations_are_exported_in_time_order(self):
        D.server_event(self.id, "alice", {"kind": "server", "request": "request-1", "status": 200})
        D.append(self.id, "alice", 1, [{"kind": "click", "at": 0}, {"kind": "request", "at": 1000, "request": "request-1"}])
        events = D.snapshot(self.id, "alice")["events"]
        self.assertEqual(sorted(event["at"] for event in events), [event["at"] for event in events])

    def test_malformed_event_times_cannot_poison_saved_reports(self):
        for event in ({"kind": []}, {"kind": "click", "at": "secret"}, {"kind": "click", "at": float("nan")}):
            with self.assertRaises(ValueError):
                D.append(self.id, "alice", 1, [event])


if __name__ == "__main__":
    unittest.main()
