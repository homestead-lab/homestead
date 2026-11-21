"""A key that acts without approval (Nov 2026).

An admin can make an MCP key whose requests are approved the moment they
are filed, as the key's maker with their role then. It is off by default,
needs MCP on the key, and goes through the same approval as a person's:
planned again, audited, and listed on the Agents page."""
import sys
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_mcp as M
from fakes.kube import no_cluster

import homestead_agent_audit as AUDIT
import homestead_agent_requests as REQUESTS
import homestead_api_keys as KEYS
import homestead_mcp as MCP
import homestead_route_policy as ROUTE_POLICY


def setUpModule():
    unittest.enterModuleContext(no_cluster())


def unattended(owner_role="admin", **changes):
    return {**M.key(["read", "containers:manage"], role="operator"), "unattended": True, "owner_role": owner_role, **changes}


class UnattendedKeyTests(M.Base):
    def setUp(self):
        super().setUp()
        self.objects["/apis/apps/v1/namespaces/lab/deployments/web"] = {
            "metadata": {"uid": "u1"}, "spec": {"template": {"spec": {"containers": [{"image": "nginx"}]}}}}

    def delete(self, k):
        with mock.patch.object(ROUTE_POLICY, "role", return_value="operator"):
            return self.call("delete_container", {"namespace": "lab", "name": "web", "reason": "unused"}, k)

    def test_its_delete_runs_at_once_as_the_keys_maker(self):
        result = self.delete(unattended())
        self.assertFalse(result["isError"], result)
        self.assertEqual([("lab", "web")], self.deleted)
        self.assertEqual(["approving:claude:ada"], self.deleted_by)
        row = next(iter(REQUESTS._load().values()))
        self.assertEqual(("done", "ada"), (row["status"], row["decided_by"]))
        self.assertTrue(row["detail"].startswith("Run without approval"), row["detail"])
        self.assertEqual("approved", AUDIT.recent()[0]["outcome"])
        self.assertEqual([], MCP.alert_facts(), "nothing waits in the bell")

    def test_a_key_without_it_still_waits_for_a_person(self):
        result = self.delete(M.key(["read", "containers:manage"], role="operator"))
        self.assertEqual("pending_approval", result["structuredContent"]["status"])
        self.assertEqual([], self.deleted)

    def test_a_request_its_makers_role_cannot_approve_still_waits(self):
        # Its maker was demoted to viewer since: they could not approve a delete.
        result = self.delete(unattended(owner_role="viewer"))
        self.assertEqual("pending_approval", result["structuredContent"]["status"])
        self.assertEqual([], self.deleted)

    def test_a_plan_that_fails_says_so_to_the_agent(self):
        with mock.patch.object(REQUESTS.TOOLS, "digest", side_effect=["planned", "changed"]):
            result = self.delete(unattended())
        self.assertTrue(result["isError"])
        self.assertIn("what it would do has changed", result["content"][0]["text"])
        self.assertEqual([], self.deleted)

    def test_the_agent_is_told_it_acts_without_approval(self):
        said = self.ask(M.rpc("initialize"), unattended())[1]["result"]["instructions"]
        self.assertIn("without a person's approval", said)
        self.assertNotIn("become a request", said)
        usual = self.ask(M.rpc("initialize"))[1]["result"]["instructions"]
        self.assertIn(MCP.REQUESTS_SAID, usual, "the sentence replaced for such a key is still in the instructions")


class KeyRecordTests(unittest.TestCase):
    def setUp(self):
        self.data = {"users": {"ada": {"role": "admin"}}, "api_keys": {}}
        for name, value in (("_load", lambda **_: self.data), ("_save", lambda data: None),
                            ("role_of", lambda user: self.data["users"].get(user, {}).get("role"))):
            patch = mock.patch.object(KEYS.AUTH, name, value)
            patch.start()
            self.addCleanup(patch.stop)

    def make(self, **options):
        return KEYS.create("agent", ["read"], 3600, [], "ada", **options)

    def test_off_by_default(self):
        self.assertFalse(self.make(mcp=True)["key"]["unattended"])

    def test_it_needs_mcp_on_the_key(self):
        with self.assertRaisesRegex(ValueError, "only a key that allows AI agents"):
            self.make(unattended=True)

    def test_turning_mcp_off_turns_it_off(self):
        kid = self.make(mcp=True, unattended=True)["key"]["id"]
        done = KEYS.edit(kid, {"mcp": False}, "ada")
        self.assertEqual((False, False), (done["key"]["mcp"], done["key"]["unattended"]))
        self.assertEqual(["MCP", "approval"], done["changed"])

    def test_a_key_made_before_it_existed_reads_as_off(self):
        kid = self.make(mcp=True)["key"]["id"]
        self.data["api_keys"][kid].pop("unattended")
        self.assertEqual([], KEYS.edit(kid, {"unattended": False}, "ada")["changed"])

    def test_the_verified_key_carries_it_and_its_makers_role(self):
        made = self.make(mcp=True, unattended=True)
        with mock.patch.object(KEYS, "_note_use"):
            key = KEYS.verify(made["token"], "192.0.2.40", now=time.time())
        self.assertEqual((True, "admin"), (key["unattended"], key["owner_role"]))


if __name__ == "__main__":
    unittest.main()
