"""Stop, Start, /api/scale, API v1 and power schedules: what one scale write
does, through the real write guards and a fake API server (#621).

The outcome tests pin what a person sees - the copies, the refusals, the
storage-move and self-data fences - so they hold whichever path the write
takes. The write tests pin how it is sent: one merge patch on the Deployment
itself, carrying who made it, and never a second try through /scale."""
import contextlib
import json
import re
import sys
import tempfile
import time
import types
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import server
import homestead_api_v1 as API_V1
import homestead_host_access as HOSTACCESS
import homestead_changes as CH
import homestead_kube as K
import homestead_names as NAMES
import homestead_operations as OPS
import homestead_schedules as SCHEDULES
import homestead_self as SELF
import homestead_workloads as WORKLOADS
from fakes.job_store import put
from fakes.kube import Kube

OV = server.OVERVIEW_ROUTES
DEPLOYMENTS = "/apis/apps/v1/namespaces/lab/deployments"


def deployment(name="web", replicas=1):
    return {"apiVersion": "apps/v1", "kind": "Deployment",
            "metadata": {"namespace": "lab", "name": name, "annotations": {"deployment.kubernetes.io/revision": "1"}},
            "spec": {"replicas": replicas, "selector": {"matchLabels": {"app": name}},
                     "template": {"metadata": {"labels": {"app": name}},
                                  "spec": {"containers": [{"name": "app", "image": "example/web:1"}]}}}}


def scale_subresource(kube, name):
    """/scale as the API server answers it: only the count reaches the app."""
    path = f"{DEPLOYMENTS}/{name}"

    def answer(method, _path, body):
        kube("PATCH", path, {"spec": {"replicas": body["spec"]["replicas"]}}, "application/merge-patch+json")
        return {"kind": "Scale", "spec": {"replicas": body["spec"]["replicas"]}}
    kube.react("PATCH", path + "/scale", answer)


def plan(blocked=False, confirm=False):
    return {"blocked": blocked, "requires_confirmation": confirm, "warnings": ["tight"] if confirm else [],
            "blockers": ["no room"] if blocked else []}


class ScaleTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        CH.bind(directory.name)
        CH._known.clear()
        self.addCleanup(CH._known.clear)
        self.addCleanup(setattr, CH.REQUEST, "user", None)
        for target, name, value in ((OPS, "DATA_DIR", directory.name),
                                    (CH, "_author_key", lambda: b"test-key"),
                                    (CH, "_authors", lambda: {"users": ["ada"], "api_keys": ["ci"]}),
                                    (OV, "workload_start_plan", lambda ns, name, n: plan())):
            patcher = mock.patch.object(target, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    @contextlib.contextmanager
    def cluster(self, *objects):
        objects = objects or (deployment(),)
        with Kube(*objects) as kube:
            for obj in objects:
                scale_subresource(kube, obj["metadata"]["name"])
            yield kube

    def replicas(self, kube, name="web"):
        return kube.object(f"{DEPLOYMENTS}/{name}")["spec"]["replicas"]

    def scale_writes(self, kube):
        return [w for w in kube.writes if w[0] != "DELETE" and w[1].startswith(DEPLOYMENTS)]

    # What a person sees: pinned before the write changed path ---------------

    def test_stop_and_start_set_the_copies(self):
        with self.cluster() as kube:
            self.assertEqual({"ok": True, "plan": None}, OV.scale_workload("lab", "web", 0))
            self.assertEqual(0, self.replicas(kube))
            self.assertEqual(plan(), OV.scale_workload("lab", "web", 3)["plan"])
            self.assertEqual(3, self.replicas(kube))

    def test_a_count_out_of_range_is_refused_before_anything_is_sent(self):
        with self.cluster() as kube:
            for n in (-1, 101):
                with self.assertRaisesRegex(ValueError, "between 0 and 100"):
                    OV.scale_workload("lab", "web", n)
            self.assertEqual([], kube.writes)

    def test_a_start_the_capacity_check_refuses_sends_nothing(self):
        with self.cluster(deployment(replicas=0)) as kube:
            with mock.patch.object(OV, "workload_start_plan", lambda *a: plan(blocked=True)):
                with self.assertRaises(WORKLOADS.WorkloadRefused):
                    OV.scale_workload("lab", "web", 1, confirm_capacity=True)
            with mock.patch.object(OV, "workload_start_plan", lambda *a: plan(confirm=True)):
                with self.assertRaisesRegex(WORKLOADS.WorkloadRefused, "review node memory"):
                    OV.scale_workload("lab", "web", 1)
                self.assertEqual([], kube.writes)
                OV.scale_workload("lab", "web", 1, confirm_capacity=True)
            self.assertEqual(1, self.replicas(kube))

    def test_homestead_stops_itself_only_when_confirmed(self):
        with self.cluster(deployment(NAMES.BRAND)) as kube, mock.patch.object(SELF, "NS", "lab"):
            with self.assertRaisesRegex(ValueError, "Stopping Homestead"):
                OV.scale_workload("lab", NAMES.BRAND, 0)
            self.assertEqual([], kube.writes)
            OV.scale_workload("lab", NAMES.BRAND, 0, confirm_self=True)
            self.assertEqual(0, self.replicas(kube, NAMES.BRAND))

    def test_homestead_is_not_stopped_during_a_storage_move(self):
        put(OPS, [{"id": "move", "kind": "reclass", "status": "running", "ref": {"namespace": "lab", "claim": "data"}}])
        with self.cluster(deployment(NAMES.BRAND)) as kube, mock.patch.object(SELF, "NS", "lab"):
            with self.assertRaisesRegex(ValueError, "Finish or recover the storage move"):
                OV.scale_workload("lab", NAMES.BRAND, 0, confirm_self=True)
            self.assertEqual([], self.scale_writes(kube))
            self.assertEqual(1, self.replicas(kube, NAMES.BRAND))

    def test_nothing_is_scaled_while_homesteads_own_data_moves(self):
        with self.cluster() as kube, \
                mock.patch.object(K, "require_self_data_write", side_effect=ValueError("Homestead's data is moving")):
            with self.assertRaisesRegex(ValueError, "data is moving"):
                OV.scale_workload("lab", "web", 0)
            self.assertEqual([], kube.writes)
            self.assertEqual(1, self.replicas(kube))

    def test_homesteads_own_smb_server_is_not_scaled_from_here(self):
        with self.cluster(deployment("samba")) as kube:
            with self.assertRaisesRegex(ValueError, "manages SMB"):
                OV.scale_workload("lab", "samba", 0)
            self.assertEqual([], kube.writes)

    def test_an_operator_cannot_scale_a_management_app_on_the_write_itself(self):
        # The write guard's own access check, on the path the write now takes,
        # even when the check before it is skipped.
        HOSTACCESS.set_role("operator")
        self.addCleanup(HOSTACCESS.set_role, None)
        with self.cluster(deployment("homestead-helper")) as kube, \
                mock.patch.object(OV, "require_workload_target", lambda ns, name: None):
            with self.assertRaisesRegex(PermissionError, "only an admin"):
                OV.scale_workload("lab", "homestead-helper", 0)
            self.assertEqual([], self.scale_writes(kube))
            self.assertEqual(1, self.replicas(kube, "homestead-helper"))
        with self.cluster() as kube:
            OV.scale_workload("lab", "web", 0)
            self.assertEqual(0, self.replicas(kube), "an ordinary app is still the operator's to stop")

    def test_a_refused_write_is_the_apis_answer_and_is_sent_once(self):
        with self.cluster() as kube:
            kube.fail(f"{DEPLOYMENTS}/web", 409, method="PATCH")
            kube.fail(f"{DEPLOYMENTS}/web/scale", 409, method="PATCH")
            with self.assertRaises(urllib.error.HTTPError) as caught:
                OV.scale_workload("lab", "web", 0)
            self.assertEqual(409, caught.exception.code)
            self.assertEqual(1, len(self.scale_writes(kube)), "no second try")
            self.assertEqual(1, self.replicas(kube))

    def test_a_scale_after_someone_else_changed_the_app_still_applies(self):
        # The write pins no resourceVersion, so an edit in between is no conflict.
        with self.cluster() as kube:
            kube("PATCH", f"{DEPLOYMENTS}/web", {"metadata": {"labels": {"tier": "web"}}}, "application/merge-patch+json")
            OV.scale_workload("lab", "web", 0)
            self.assertEqual(0, self.replicas(kube))
            self.assertEqual({"tier": "web"}, kube.object(f"{DEPLOYMENTS}/web")["metadata"]["labels"])

    def test_the_scale_route_and_api_v1_answer_as_before(self):
        with self.cluster() as kube:
            self.assertEqual({"ok": True}, WORKLOADS.scale(types.SimpleNamespace(body={"ns": "lab", "name": "web", "replicas": "0"})))
            self.assertEqual(0, self.replicas(kube))
            self.assertEqual({"warnings": [], "job": None}, OV.api_scale("lab", "web", 2))
            self.assertEqual(2, self.replicas(kube))
            with mock.patch.object(OV, "workload_start_plan", lambda *a: plan(blocked=True)):
                reply = WORKLOADS.scale(types.SimpleNamespace(body={"ns": "lab", "name": "web", "replicas": 4}))
                self.assertEqual(409, reply.code)
                with self.assertRaises(API_V1.ApiError) as caught:
                    OV.api_scale("lab", "web", 4)
            self.assertEqual((409, ["no room"]), (caught.exception.code, caught.exception.extra["blockers"]))
            self.assertEqual(2, self.replicas(kube))

    def test_a_schedule_stops_with_the_copies_remembered_and_starts_them_again(self):
        CH.REQUEST.user = CH.BY_SCHEDULE
        with self.cluster(deployment(replicas=2)) as kube:
            self.assertEqual("stopped web", OV.schedule_act({"kind": "app", "ns": "lab", "name": "web"}, "stop"))
            stored = kube.object(f"{DEPLOYMENTS}/web")
            self.assertEqual(0, stored["spec"]["replicas"])
            self.assertEqual("2", stored["metadata"]["annotations"][NAMES.key(SCHEDULES.REPLICAS_KEY)])
            self.assertEqual("web was already stopped", OV.schedule_act({"kind": "app", "ns": "lab", "name": "web"}, "stop"))
            self.assertIn("2 copies", OV.schedule_act({"kind": "app", "ns": "lab", "name": "web"}, "start"))
            self.assertEqual(2, self.replicas(kube))

    # How it is sent ----------------------------------------------------------

    def test_a_scale_is_one_merge_patch_on_the_app_that_carries_who_made_it(self):
        CH.REQUEST.user = "ada"
        with self.cluster() as kube:
            OV.scale_workload("lab", "web", 0)
            writes = self.scale_writes(kube)
            self.assertEqual(1, len(writes), writes)
            method, path, body, ctype = writes[0]
            self.assertEqual(("PATCH", f"{DEPLOYMENTS}/web", "application/merge-patch+json"), (method, path, ctype))
            self.assertEqual({"replicas": 0}, body["spec"])
            self.assertEqual({CH.AUTHOR}, set(body["metadata"]["annotations"]))
            stored = kube.object(f"{DEPLOYMENTS}/web")
            self.assertIn(CH.AUTHOR, stored["metadata"]["annotations"])
            self.assertEqual("1", stored["metadata"]["annotations"]["deployment.kubernetes.io/revision"],
                             "the app's other annotations are kept")

    def test_a_policy_refusal_is_shown_and_never_retried_through_scale(self):
        # An admission policy that matches Deployments may refuse what /scale
        # would have let through: the person sees that refusal (#621, PR body).
        with self.cluster() as kube:
            kube.fail(f"{DEPLOYMENTS}/web", 403, method="PATCH")
            with self.assertRaises(urllib.error.HTTPError) as caught:
                OV.scale_workload("lab", "web", 0)
            self.assertEqual(403, caught.exception.code)
            self.assertEqual([], [w for w in kube.writes if w[1].endswith("/scale")], "no fallback to /scale")
            self.assertEqual(1, self.replicas(kube))

    def test_the_mark_says_who_on_every_way_in(self):
        for user, call in (("ada", lambda: OV.scale_workload("lab", "web", 0)),
                           ("api-key:ci", lambda: OV.api_scale("lab", "web", 0)),
                           (CH.BY_SCHEDULE, lambda: OV.schedule_act({"kind": "app", "ns": "lab", "name": "web"}, "stop"))):
            with self.subTest(user=user), self.cluster() as kube:
                CH.REQUEST.user = user
                call()
                mark = json.loads(kube.object(f"{DEPLOYMENTS}/web")["metadata"]["annotations"][CH.AUTHOR])
                self.assertEqual(CH._signed(b"test-key", "lab", "web", mark["at"], user), mark["by"])
                self.assertLessEqual(abs(mark["at"] - time.time()), 5)


class ScalePermissionTests(unittest.TestCase):
    """Nothing scales through /scale any more, so Homestead stops asking for it."""
    ROOT = Path(__file__).resolve().parents[1]

    def test_no_code_writes_a_deployments_scale(self):
        # The change history and the storage guard still recognise the path.
        found = [f"{source.name}:{number}" for source in sorted((self.ROOT / "server").glob("*.py"))
                 for number, line in enumerate(source.read_text(encoding="utf-8").splitlines(), 1)
                 if re.search(r"deployments/\S*/scale\b|[\"'}]/scale\b", line) and not line.lstrip().startswith("#")
                 and "re.compile" not in line and '"/scale" in path' not in line]
        self.assertEqual([], found, "a write to deployments/.../scale needs the permission back")

    def test_homestead_does_not_ask_for_the_scale_permission(self):
        grants = {(group, resource) for rule in SELF.desired_rules(str(self.ROOT / "deploy" / "deploy.yaml"))
                  for group in rule.get("apiGroups") or [""] for resource in rule.get("resources") or []}
        self.assertIn(("apps", "deployments"), grants)
        self.assertNotIn(("apps", "deployments/scale"), grants)
        for generated in ("deploy/rbac.yaml", "charts/homestead/templates/rbac.yaml"):
            self.assertNotIn("deployments/scale", (self.ROOT / generated).read_text(encoding="utf-8"), generated)


if __name__ == "__main__":
    unittest.main()
