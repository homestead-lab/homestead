"""What the Agents page is given about a request, by role: a person who can
decide a request sees its full settings; anyone else who may see the list
sees that it exists, who asked, which tool and which app, and none of its
settings. The server leaves them out; the page only shows what it is given."""
import json
import sys
import tempfile
import time
import types
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "server"))
import homestead_agent_audit as AUDIT
import homestead_agent_requests as REQUESTS
import homestead_agent_tools as TOOLS
import homestead_host_access as HOSTACCESS
import homestead_mcp as MCP
import homestead_route_policy as ROUTE_POLICY
from rebind import keep_bound
sys.path.insert(0, str(Path(__file__).resolve().parent))
from fakes.kube import no_cluster

# A stand-in for a value kept for an app; plain words, not shaped like any key.
VALUE = "example-" + "value-" + "7731"
LISTING = REQUESTS.ROUTES[("GET", "/api/agents/requests")]


def setUpModule():
    unittest.enterModuleContext(no_cluster())


def key(scopes, role):
    return {"id": "k1", "name": "claude", "owner": "ada", "scopes": list(scopes), "expires": time.time() + 3600,
            "mcp": True, "role": role, "kind": "key"}


def listed(role):
    """The answer of GET /api/agents/requests for a person with this role."""
    needed, handler = LISTING
    assert needed == "viewer"
    return handler(types.SimpleNamespace(role=role, user="someone", body={}))


class Base(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        keep_bound(self, MCP, AUDIT, REQUESTS, TOOLS)
        MCP.bind(self.folder.name, lambda: {"mcp": {"enabled": True}}, "9.9.9", ROUTE_POLICY.role,
                 lambda m, p, b: None, self.kget, lambda ns, name: [name])
        MCP._calls.clear()

    def kget(self, path):
        if path != "/apis/apps/v1/namespaces/lab/deployments/old-wiki":
            raise KeyError(path)
        return {"metadata": {"uid": "u-1"}, "spec": {"template": {"spec": {"containers": [{"image": "busybox"}]}}}}

    def call(self, name, args, k):
        status, answer = MCP.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                     "params": {"name": name, "arguments": args}}, k, "192.0.2.40", "",
                                    "homestead.example.com")
        self.assertEqual(200, status)
        return answer["result"]

    def escalated(self):
        """An operator's agent asks to deploy lab/web with a kept value and
        host access: a request only an admin can decide."""
        k = key(["read", "containers:manage"], "operator")
        settings = {"name": "web", "namespace": "lab", "image": "busybox", "privileged": True,
                    "env": {"EXAMPLE_TOKEN": VALUE}}
        with mock.patch.object(TOOLS, "call", side_effect=HOSTACCESS.Refused("privileged mode is for admins to give")):
            answer = self.call("deploy_container", {"settings": settings}, k)["structuredContent"]
        self.assertEqual("pending_approval", answer["status"])
        self.assertNotIn(VALUE, json.dumps(answer), "the agent's own answer holds no value")
        return answer["request"], k

    def only(self, role):
        rows = listed(role)["requests"]
        self.assertEqual(1, len(rows))
        return rows[0]


class SettingsByRoleTests(Base):
    def test_a_person_who_cannot_decide_sees_the_request_without_its_settings(self):
        rid, _ = self.escalated()
        for role in ("viewer", "operator"):
            with self.subTest(role=role):
                answer = listed(role)
                self.assertNotIn(VALUE, json.dumps(answer))
                row = answer["requests"][0]
                self.assertEqual((rid, "deploy_container", "claude", "ada", "lab/web"),
                                 (row["id"], row["tool"], row["key"], row["owner"], row["app"]))
                self.assertFalse(row["can_decide"])
                self.assertTrue(row["details_hidden"])
                for field in ("summary", "args", "result"):
                    self.assertNotIn(field, row)
                self.assertEqual(1, answer["pending"])

    def test_a_person_who_can_decide_still_sees_every_setting(self):
        self.escalated()
        row = self.only("admin")
        self.assertTrue(row["can_decide"])
        self.assertNotIn("details_hidden", row)
        self.assertEqual(VALUE, row["summary"]["settings"]["settings"]["env"]["EXAMPLE_TOKEN"], "the approver reviews what will run")
        self.assertIn("args", row)

    def test_a_planned_request_shows_its_plan_only_to_people_who_can_decide_it(self):
        k = key(["read", "containers:manage"], "operator")
        self.call("delete_container", {"namespace": "lab", "name": "old-wiki"}, k)
        hidden, shown = self.only("viewer"), self.only("operator")
        self.assertEqual("lab/old-wiki", hidden["app"])
        self.assertTrue(hidden["details_hidden"])
        self.assertNotIn("summary", hidden)
        self.assertNotIn("args", hidden)
        self.assertTrue(shown["can_decide"])
        self.assertIn("summary", shown)
        self.assertEqual({"namespace": "lab", "name": "old-wiki"}, shown["args"])

    def test_an_app_name_that_is_not_a_plain_name_is_left_out(self):
        k = key(["read", "containers:manage"], "operator")
        settings = {"name": "web " + VALUE, "namespace": "lab", "image": "busybox", "privileged": True}
        with mock.patch.object(TOOLS, "call", side_effect=HOSTACCESS.Refused("privileged mode is for admins to give")):
            self.call("deploy_container", {"settings": settings}, k)
        row = self.only("operator")
        self.assertEqual("", row["app"])
        self.assertNotIn(VALUE, json.dumps(row))


class DecidingTests(Base):
    def test_the_person_who_can_decide_still_approves_it_and_its_result_stays_with_them(self):
        rid, _ = self.escalated()
        with self.assertRaises(PermissionError):
            REQUESTS.approve(rid, "sam", "operator")

        def route(method, path, query=None, body=None, *, user, role):
            return {"capacity_token": "fresh", "capacity": {}} if path == "/api/preview" else {"ok": True, "echo": VALUE}
        with mock.patch.object(TOOLS, "call", side_effect=route):
            self.assertEqual("done", REQUESTS.approve(rid, "ada", "admin")["status"])
        self.assertEqual(VALUE, self.only("admin")["result"]["echo"])
        hidden = self.only("operator")
        self.assertEqual("done", hidden["status"])
        self.assertNotIn(VALUE, json.dumps(hidden))

    def test_the_agent_following_its_request_is_given_no_settings(self):
        rid, k = self.escalated()
        followed = self.call("get_agent_request", {"id": rid}, k)["structuredContent"]
        self.assertEqual("pending", followed["status"])
        self.assertNotIn(VALUE, json.dumps(followed))

    def test_the_bell_entry_names_the_request_without_settings(self):
        self.escalated()
        facts = REQUESTS.alert_facts()
        self.assertEqual(1, len(facts))
        self.assertNotIn(VALUE, json.dumps(facts))


if __name__ == "__main__":
    unittest.main()
