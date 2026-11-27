"""An agent asks for a restore test; an admin approves it in Homestead.

Test now on Data protection is an admin route (POST /api/restore-tests/run). The
tool run_restore_test reaches it only through an approval, and only for an app
the page's own list offers a test for. Here the tool runs through the MCP door,
the real routes and the restore-test module, on the shared Kubernetes fake; only
the job store is stood in for (what starts, what runs), except in RunNowTests,
which use the real job store."""
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "server"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import homestead_agent_audit as AUDIT
import homestead_agent_requests as REQUESTS
import homestead_agent_tools as TOOLS
import homestead_cache as CACHE
import homestead_kube as K
import homestead_longhorn as LH
import homestead_longhorn_backups as LHB
import homestead_mcp as MCP
import homestead_names as NAMES
import homestead_operations as OPS
import homestead_restore_test as RT
import homestead_route_policy as ROUTE_POLICY
from fakes.kube import Kube
from rebind import keep_bound

RT_BOUND = ("kget", "ksend", "names", "backups", "restore", "restore_status", "csi_cleanup", "capacity", "ask",
            "self_namespace", "workloads", "app_settings", "save_app_settings", "dns_name")


def deployment(name, claims, privileged=False):
    container = {"name": "app", "image": "example/app:1"}
    if privileged:
        container["securityContext"] = {"privileged": True}
    return {"apiVersion": "apps/v1", "kind": "Deployment", "metadata": {"name": name, "namespace": "lab"},
            "spec": {"template": {"spec": {"containers": [container],
                     "volumes": [{"name": c, "persistentVolumeClaim": {"claimName": c}} for c in claims]}}}}


def backup(claim, day=6):
    return {"apiVersion": "longhorn.io/v1beta2", "kind": "Backup",
            "metadata": {"name": f"{claim}-{day}", "namespace": "longhorn-system",
                         "labels": {"backup-volume": f"pvc-{claim}"}},
            "status": {"state": "Completed", "url": f"s3://example/{claim}-{day}",
                       "backupCreatedAt": f"2026-10-{day:02d}T03:00:00Z",
                       "volumeSize": str(20 * 1073741824), "size": "1048576"}}


def volume(claim, backed_up=True):
    out = [{"apiVersion": "v1", "kind": "PersistentVolumeClaim", "metadata": {"name": claim, "namespace": "lab"},
            "spec": {"volumeName": f"pvc-{claim}", "storageClassName": "longhorn"}},
           {"apiVersion": "v1", "kind": "PersistentVolume", "metadata": {"name": f"pvc-{claim}"},
            "spec": {"csi": {"driver": "driver.longhorn.io", "volumeHandle": f"pvc-{claim}"}}}]
    return out + ([backup(claim)] if backed_up else [])


def app(name, claims, backed_up=True, privileged=False):
    return [deployment(name, claims, privileged)] + [o for claim in claims for o in volume(claim, backed_up)]


def key(scopes, role="operator", **more):
    return {"id": "k1", "name": "claude", "owner": "ada", "scopes": list(scopes), "expires": time.time() + 3600,
            "mcp": True, "role": role, "kind": "key", **more}


class RestoreTestToolTests(unittest.TestCase):
    def setUp(self):
        CACHE.forget_where(lambda key: True)
        self.addCleanup(CACHE.forget_where, lambda key: True)
        self.kube = Kube(*app("paperless", ["paperless-data", "paperless-media"]),
                         *app("scratch", ["scratch-data"], backed_up=False),
                         *app("homestead", ["homestead-data"]), *app("camera", ["camera-data"], privileged=True))
        self.kube.__enter__()
        self.addCleanup(self.kube.__exit__, None, None, None)
        saved = (LH.kget, LH.ksend, LH._cache, LH.DEFAULT_NS)
        self.addCleanup(lambda: LH.bind(*saved))
        LH.bind(K.kget, K.ksend, CACHE._cache)
        self.addCleanup(vars(RT).update, {name: getattr(RT, name, None) for name in RT_BOUND})
        workloads = [{"ns": "lab", "name": "paperless", "claims": ["paperless-data", "paperless-media"]},
                     {"ns": "lab", "name": "scratch", "claims": ["scratch-data"]},
                     {"ns": "lab", "name": "camera", "claims": ["camera-data"]},
                     {"ns": "lab", "name": "homestead", "claims": ["homestead-data"], "self": True}]
        RT.bind(K.kget, K.ksend, NAMES, LHB.backups, lambda cfg: {}, lambda item: ("running", 0, ""),
                lambda: [], lambda: {}, lambda target: {"ok": False}, lambda: "lab",
                lambda: workloads, lambda: {}, lambda settings: None, lambda value, what: value)
        self.operations, self.started = [], []

        def start(kind, title, resource, href, item, message, unless=()):
            self.started.append((kind, resource["namespace"], resource["name"]))
            return {"id": "op1", "kind": kind}
        for name, fake in (("snapshot", lambda: list(self.operations)), ("start", start)):
            patch = mock.patch.object(RT.OPS, name, fake)
            patch.start()
            self.addCleanup(patch.stop)
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        keep_bound(self, MCP, AUDIT, REQUESTS, TOOLS)
        MCP.bind(self.folder.name, lambda: {"mcp": {"enabled": True}}, "9.9.9", ROUTE_POLICY.role,
                 lambda m, p, b: None, K.kget, lambda ns, name: [])
        MCP._calls.clear()
        self.k = key(["read", "storage:manage"])

    def ask(self, args, k=None):
        MCP._calls.clear()     # not the one-minute limit on asking
        status, answer = MCP.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                     "params": {"name": "run_restore_test", "arguments": args}},
                                    k or self.k, "192.0.2.40", "", "homestead.example.com")
        self.assertEqual(200, status)
        return answer["result"]

    def request(self, name="paperless"):
        return self.ask({"namespace": "lab", "name": name})["structuredContent"]["request"]

    def later(self):
        """What the page's list keeps for 30 s has gone: approval reads the cluster as it is."""
        CACHE.forget_where(lambda key: True)

    def test_it_is_a_destructive_tool_an_admin_approves_shown_only_with_storage_manage(self):
        tool = TOOLS.BY_NAME["run_restore_test"]
        self.assertEqual((TOOLS.DESTRUCTIVE, "admin", "storage:manage"), (tool.kind, tool.role, tool.scope))
        self.assertEqual(["name", "namespace"], sorted(tool.schema["required"]))
        self.assertIn("new volumes of the same size in the app's namespace", tool.description)
        self.assertNotIn(tool, TOOLS.offered(["read"]))
        self.assertIn(tool, TOOLS.offered(["read", "storage:manage"]))

    def test_asking_starts_nothing_and_files_a_request_for_an_admin(self):
        answer = self.ask({"namespace": "lab", "name": "paperless", "reason": "check the backups restore"})
        self.assertEqual("pending_approval", answer["structuredContent"]["status"])
        self.assertEqual([], self.started)
        row = REQUESTS.get(answer["structuredContent"]["request"])
        self.assertEqual(("admin", "pending"), (row["role"], row["status"]))
        self.assertEqual("Run a restore test of lab/paperless", row["summary"]["action"])
        self.assertEqual(["paperless-data", "paperless-media"], row["summary"]["restores"])
        self.assertEqual(40.0, row["summary"]["new_volumes_gb"], "two 20 GB volumes are made")
        self.assertNotIn("privileged", row["summary"])

    def test_the_approver_is_told_a_privileged_apps_copy_runs_privileged(self):
        rid = self.request("camera")
        self.assertIn("privileged", REQUESTS.get(rid)["summary"]["privileged"])
        self.later()
        self.assertEqual("done", REQUESTS.approve(rid, "ada", "admin")["status"], "the summary holds still")

    def test_an_operator_cannot_approve_it_and_an_admin_starts_the_test_once(self):
        rid = self.request()
        with self.assertRaises(PermissionError):
            REQUESTS.approve(rid, "olly", "operator")
        self.assertEqual([], self.started)
        done = REQUESTS.approve(rid, "ada", "admin")
        self.assertEqual("done", done["status"], done.get("detail"))
        self.assertEqual([(RT.KIND, "lab", "paperless")], self.started)
        self.assertEqual("op1", done["result"]["operation"]["id"])
        self.assertEqual("ada", AUDIT.recent()[0]["approved_by"])

    def test_a_backup_made_between_asking_and_approving_still_approves(self):
        rid = self.request()
        self.kube.add(backup("paperless-data", day=7))
        self.later()
        self.assertEqual("done", REQUESTS.approve(rid, "ada", "admin")["status"])
        self.assertEqual([(RT.KIND, "lab", "paperless")], self.started)

    def test_a_volume_added_or_removed_between_asking_and_approving_fails_the_approval(self):
        for claims in (["paperless-data", "paperless-media", "paperless-extra"], ["paperless-data"]):
            with self.subTest(claims=claims):
                self.kube.add(deployment("paperless", ["paperless-data", "paperless-media"]))
                self.later()
                rid = self.request()
                for obj in volume("paperless-extra"):
                    self.kube.add(obj)
                self.kube.add(deployment("paperless", claims))
                self.later()
                done = REQUESTS.approve(rid, "ada", "admin")
                self.assertEqual("failed", done["status"])
                self.assertIn("changed since", done["detail"])
        self.assertEqual([], self.started)

    def test_a_key_an_admin_set_to_act_without_approval_starts_it_at_once_as_that_admin(self):
        answer = self.ask({"namespace": "lab", "name": "paperless"},
                          key(["read", "storage:manage"], unattended=True, owner_role="admin"))
        self.assertEqual("op1", answer["structuredContent"]["operation"]["id"])
        self.assertEqual([(RT.KIND, "lab", "paperless")], self.started)
        self.assertEqual("ada", AUDIT.recent()[0]["approved_by"])
        # Its maker is an operator now: it waits for an admin.
        answer = self.ask({"namespace": "lab", "name": "paperless"},
                          key(["read", "storage:manage"], unattended=True, owner_role="operator"))
        self.assertEqual("pending_approval", answer["structuredContent"]["status"])
        self.assertEqual(1, len(self.started))

    def test_an_app_the_page_offers_no_test_for_is_refused_before_anyone_is_asked(self):
        for name, why in (("scratch", "no completed backup"), ("homestead", "not an app"), ("ghost", "not an app")):
            with self.subTest(name=name):
                result = self.ask({"namespace": "lab", "name": name})
                self.assertTrue(result["isError"])
                self.assertIn(why, result["content"][0]["text"])
        self.assertEqual(0, REQUESTS.pending_count())
        self.assertEqual([], self.started)

    def test_while_a_test_runs_or_cancels_asking_is_refused_and_an_approval_fails(self):
        for status in ("running", "cancelling"):
            with self.subTest(status=status):
                self.operations[:] = []
                rid = self.request()
                self.operations.append({"kind": RT.KIND, "status": status,
                                        "resource": {"namespace": "lab", "name": "other"}})
                done = REQUESTS.approve(rid, "ada", "admin")
                self.assertEqual("failed", done["status"])
                self.assertIn("Only one runs at a time", done["detail"])
                result = self.ask({"namespace": "lab", "name": "paperless"})
                self.assertTrue(result["isError"])
                self.assertIn("Only one runs at a time", result["content"][0]["text"])
        self.assertEqual([], self.started)

    def test_the_route_still_refuses_when_the_plans_read_of_the_jobs_missed_one(self):
        rid = self.request()
        self.operations.append({"kind": RT.KIND, "status": "running", "resource": {"namespace": "lab", "name": "other"}})
        real, sent = TOOLS.call, []

        def call(method, path, *args, **kwargs):
            sent.append((method, path))
            return [] if path == "/api/operations" else real(method, path, *args, **kwargs)
        with mock.patch.object(TOOLS, "call", call):
            done = REQUESTS.approve(rid, "ada", "admin")
        self.assertIn(("POST", "/api/restore-tests/run"), sent, "the approval reached run_now")
        self.assertEqual("failed", done["status"])
        self.assertIn("Only one runs at a time", done["detail"])
        self.assertEqual([], self.started)


class RunNowTests(unittest.TestCase):
    """Test now on the real job store: one restore test at a time, checked as it starts."""

    def setUp(self):
        tmp = self.enterContext(tempfile.TemporaryDirectory())
        self.enterContext(Kube())
        keep_bound(self, OPS)
        OPS.bind(K.kget, tmp, lambda namespace, name: {})
        self.enterContext(mock.patch.object(RT, "dns_name", lambda value, what: value))

    def jobs(self):
        return [(j["resource"]["name"], j["status"]) for j in OPS._read() if j["kind"] == RT.KIND]

    def test_a_test_still_cancelling_refuses_another(self):
        RT.run_now({"ns": "lab", "name": "paperless"})
        with OPS.transaction() as tx:
            tx.items[0]["status"] = OPS.CANCELLING
            tx.save()
        with self.assertRaisesRegex(ValueError, "is running"):
            RT.run_now({"ns": "lab", "name": "immich"})
        self.assertEqual([("paperless", "cancelling")], self.jobs())

    def test_a_test_started_after_the_check_refuses_another(self):
        # Another replica's Test now starts between run_now's look at the jobs and its own start.
        RT.start_test("lab", "paperless")
        with mock.patch.object(RT.OPS, "snapshot", lambda: []):
            with self.assertRaisesRegex(ValueError, "is running"):
                RT.run_now({"ns": "lab", "name": "immich"})
        self.assertEqual([("paperless", "queued")], self.jobs())


if __name__ == "__main__":
    unittest.main()
