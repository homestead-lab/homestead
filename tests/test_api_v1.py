"""/api/v1: one declaration per endpoint, enforced and described alike."""
import io
import json
import sys
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "server"))
sys.path.insert(0, str(ROOT / "scripts"))
import homestead_api_v1 as API

READ = {"name": "ha", "kind": "key", "scopes": ["read"], "expires": 1}
CONTROL = {"name": "agent", "kind": "key", "scopes": ["read", "containers:control"], "expires": 1}
WORKLOADS = [{"ns": "lab", "name": "photos", "desired": 1, "ready": 1, "images": ["x"], "nodes": ["k3s-1"],
              "cpu": 0.1, "mem_mb": 120.0, "uptime": 50, "problems": []},
             {"ns": "lab", "name": "wiki", "desired": 0, "ready": 0, "problems": []},
             {"ns": "media", "name": "jelly", "desired": 1, "ready": 0, "problems": ["CrashLoopBackOff"]}]


class DispatchTests(unittest.TestCase):
    def setUp(self):
        self.calls = []
        API.bind(nodes=lambda: [{"name": "k3s-1", "status": "Ready", "roles": ["control-plane"], "cpu_pct": 7.5,
                                 "mem_pct": 20.0, "mem_used_gb": 3.0, "mem_cap_gb": 15.0, "pods": 30, "vms": 1}],
                 workloads=lambda: WORKLOADS,
                 vms=lambda: [{"ns": "lab", "name": "win11", "status": "Running", "running": True, "cores": 4,
                               "memory": "8Gi", "node": "k3s-1", "ip": "192.0.2.40", "os": "windows"}],
                 alerts=lambda: [{"key": "disk:k3s-1", "severity": "critical", "category": "storage",
                                  "title": "A disk is failing", "body": "sdb", "first": 1700000000}],
                 jobs=lambda: [{"id": "abc", "kind": "vm-power", "title": "Start VM win11", "status": "running",
                                "progress": 25, "message": "", "started_at": "t", "finished_at": ""}],
                 scale=lambda ns, name, n: self.calls.append(("scale", ns, name, n)) or {"warnings": ["tight"], "job": None},
                 restart=lambda ns, name: self.calls.append(("restart", ns, name)),
                 vm_power=lambda ns, name, action: self.calls.append(("vm", ns, name, action)) or {"warnings": [], "job": "j1"},
                 version=lambda: "2.8.273")

    def call(self, method, path, auth=READ, query=None, body=None):
        return API.handle(method, path, query or {}, body, auth)

    def test_reading_the_cluster(self):
        code, status = self.call("GET", "/api/v1/status")
        self.assertEqual(200, code)
        self.assertEqual({"total": 3, "running": 1, "stopped": 1, "failing": 1}, status["containers"])
        self.assertEqual({"active": 1, "critical": 1}, status["alerts"])
        code, rows = self.call("GET", "/api/v1/containers", query={"namespace": ["lab"]})
        self.assertEqual(["photos", "wiki"], [r["name"] for r in rows])
        self.assertEqual(["running", "stopped"], [r["state"] for r in rows])
        self.assertEqual("failing", self.call("GET", "/api/v1/containers/media/jelly")[1]["state"])
        self.assertTrue(self.call("GET", "/api/v1/vms/lab/win11")[1]["running"])
        self.assertEqual(25, self.call("GET", "/api/v1/jobs/abc")[1]["progress"])

    def test_every_answer_has_exactly_its_documented_fields(self):
        for path, schema in (("/api/v1/status", "Status"), ("/api/v1/nodes", "Node"), ("/api/v1/containers", "Container"),
                             ("/api/v1/vms", "VM"), ("/api/v1/alerts", "Alert"), ("/api/v1/jobs/abc", "Job"),
                             ("/api/v1/whoami", "WhoAmI")):
            code, answer = self.call("GET", path)
            row = answer[0] if isinstance(answer, list) else answer
            self.assertEqual(200, code, path)
            self.assertEqual(set(API.SCHEMAS[schema]["properties"]), set(row), path)

    def test_control_needs_its_scope(self):
        code, answer = self.call("POST", "/api/v1/containers/lab/photos/stop")
        self.assertEqual((403, "this needs the containers:control scope"), (code, answer["error"]))
        self.assertEqual([], self.calls)
        code, answer = self.call("POST", "/api/v1/containers/lab/wiki/start", auth=CONTROL)
        self.assertEqual(200, code)
        self.assertEqual({"ok": True, "action": "start", "warnings": ["tight"], "job": None}, answer)
        self.assertEqual(403, self.call("POST", "/api/v1/vms/lab/win11/start", auth=CONTROL)[0], "containers is not VMs")
        self.assertEqual([("scale", "lab", "wiki", 1)], self.calls)

    def test_bad_paths_names_and_methods(self):
        self.assertEqual(404, self.call("GET", "/api/v1/nothing")[0])
        self.assertEqual(405, self.call("GET", "/api/v1/containers/lab/photos/stop")[0])
        self.assertEqual(400, self.call("GET", "/api/v1/containers/LAB/x")[0])
        self.assertEqual(404, self.call("GET", "/api/v1/containers/lab/missing")[0])
        self.assertEqual(404, self.call("POST", "/api/v1/containers/lab/missing/stop", auth=CONTROL)[0])
        self.assertEqual([], self.calls)

    def test_refusals_from_homestead_are_409s_with_the_reason(self):
        API.ctx["scale"] = mock.Mock(side_effect=PermissionError("Homestead cannot stop itself here"))
        code, answer = self.call("POST", "/api/v1/containers/lab/photos/stop", auth=CONTROL)
        self.assertEqual((409, "Homestead cannot stop itself here"), (code, answer["error"]))
        API.ctx["scale"] = mock.Mock(side_effect=API.ApiError(409, "it cannot start", blockers=["no room"]))
        self.assertEqual(["no room"], self.call("POST", "/api/v1/containers/lab/wiki/start", auth=CONTROL)[1]["blockers"])
        API.ctx["scale"] = mock.Mock(side_effect=urllib.error.HTTPError("u", 404, "gone", {}, None))
        self.assertEqual(404, self.call("POST", "/api/v1/containers/lab/wiki/start", auth=CONTROL)[0])


class DescriptionTests(unittest.TestCase):
    def test_every_endpoint_is_described_with_its_scope_and_errors(self):
        doc = API.openapi("2.8.273", {"read": "r", "containers:control": "c", "vms:control": "v"})
        self.assertEqual("3.1.0", doc["openapi"])
        for e in API.ENDPOINTS:
            op = doc["paths"][e.path][e.method.lower()]
            self.assertEqual(e.scope, op.get("x-homestead-scope"), e.path)
            self.assertIn("401", op["responses"])
            self.assertEqual(len(e.params), len([p for p in op.get("parameters", []) if p["in"] == "path"]))
        ops = [op["operationId"] for path in doc["paths"].values() for op in path.values()]
        self.assertEqual(len(ops), len(set(ops)), "operation ids are unique")

    def test_the_wiki_guide_lists_every_endpoint_with_its_scope(self):
        guide = (ROOT / "docs" / "wiki" / "API.md").read_text(encoding="utf-8")
        for e in API.ENDPOINTS:
            row = f"| {e.method} | `{e.path}` | " + (f"`{e.scope}`" if e.scope else "any key") + " |"
            self.assertIn(row, guide, f"docs/wiki/API.md is missing {e.method} {e.path}")

    def test_the_documented_copy_in_the_repository_is_current(self):
        import build_api_docs
        committed = (ROOT / "docs" / "api" / "openapi.json").read_text(encoding="utf-8")
        self.assertEqual(build_api_docs.document(), committed,
                         "docs/api/openapi.json is stale: run python scripts/build_api_docs.py")


class DoorTests(unittest.TestCase):
    """The server: a key opens /api/v1 and nothing else."""

    def handler(self, path, method="GET", headers=None, body=b""):
        import server
        h = object.__new__(server.H)
        h.path, h.command = path, method
        h.headers = {**(headers or {}), "Content-Length": str(len(body))}
        h.rfile, h.wfile = io.BytesIO(body), io.BytesIO()
        h.client_address, h.connection = ("192.0.2.30", 1), None
        h._begin()
        h._send = mock.Mock()
        h._signin = mock.Mock()
        return h, server

    def test_a_key_is_refused_everywhere_outside_api_v1(self):
        for path in ("/api/nodes", "/api/auth/keys", "/api/auth/users", "/api/node/shell", "/api/settings"):
            h, server = self.handler(path, headers={"Authorization": "Bearer hsk_000000000000_" + "a" * 43})
            with mock.patch.object(server.API_KEYS, "verify") as verify:
                self.assertTrue(h._guard(path))
            verify.assert_not_called()
            self.assertEqual(401, h._send.call_args[0][0], path)

    def test_a_good_key_reaches_api_v1_without_a_session_or_csrf_header(self):
        h, server = self.handler("/api/v1/containers/lab/x/stop", "POST",
                                 headers={"Authorization": "Bearer hsk_x"})
        key = {"id": "1", "name": "ha", "owner": "ada", "scopes": ["read"], "expires": 9}
        with mock.patch.object(server.API_KEYS, "verify", return_value=key), \
                mock.patch.object(server, "require_self_data_write", return_value=None):
            self.assertIsNone(h._guard("/api/v1/containers/lab/x/stop"))
        self.assertEqual(("api-key:ha", None), (h.user, h.role))
        self.assertEqual(["read"], h.api_key["scopes"])

    def test_a_refused_key_is_a_401_and_too_many_a_429(self):
        import server
        for message, code in (("that API key is not valid", 401), ("too many wrong API keys from this address", 429)):
            h, _ = self.handler("/api/v1/status", headers={"Authorization": "Bearer nope"})
            with mock.patch.object(server.API_KEYS, "verify", side_effect=PermissionError(message)):
                self.assertTrue(h._guard("/api/v1/status"))
            self.assertEqual(code, h._send.call_args[0][0])

    def test_api_v1_never_travels_to_a_linked_cluster(self):
        h, server = self.handler("/api/v1/status", headers={"X-Homestead-Cluster": "shed1",
                                                            "Authorization": "Bearer hsk_x"})
        with mock.patch.object(server.FLEET, "self_id", return_value="me"):
            self.assertEqual("", h._fleet_target("/api/v1/status"))


if __name__ == "__main__":
    unittest.main()
