"""The fence on every Kubernetes write, tested through a real write (#595).

Other tests patch authorize_workload_write and the self-data fence out, or
check only the order the guards run in. These send writes through K.send on
the shared fake with the guards in place, as the app does, and look at what
reached the API server."""
import contextlib
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.environ.get("HOMESTEAD_TEST_SERVER_DIR", str(Path(__file__).resolve().parents[1] / "server")))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import homestead_host_access as HOST
import homestead_kube as K
from fakes.kube import Kube

DEPLOYMENTS = "/apis/apps/v1/namespaces/{ns}/deployments"
CONFIGMAPS = "/api/v1/namespaces/lab/configmaps"
CRONJOBS = "/apis/batch/v1/namespaces/lab/cronjobs"


def deployment(name, ns="lab", **pod):
    return {"apiVersion": "apps/v1", "kind": "Deployment", "metadata": {"name": name, "namespace": ns},
            "spec": {"selector": {"matchLabels": {"app": name}},
                     "template": {"metadata": {"labels": {"app": name}},
                                  "spec": {"containers": [{"name": name, "image": "example.com/app:1"}], **pod}}}}


def cronjob(name, **pod):
    return {"apiVersion": "batch/v1", "kind": "CronJob", "metadata": {"name": name, "namespace": "lab"},
            "spec": {"schedule": "0 3 * * *", "jobTemplate": {"spec": {"template": {
                "metadata": {"labels": {"app": name}},
                "spec": {"restartPolicy": "Never",
                         "containers": [{"name": "task", "image": "example.com/task:1"}], **pod}}}}}}


class WriteGateTests(unittest.TestCase):
    def setUp(self):
        self.addCleanup(HOST.bind_targets, HOST._system_namespaces)
        HOST.bind_targets({"kube-system"})
        self.addCleanup(HOST.set_role, None)

    def written(self, kube, method):
        return [(m, path) for m, path, *_ in kube.writes if m == method]

    def test_an_operator_cannot_change_a_host_privileged_workload(self):
        with Kube(deployment("router", hostNetwork=True)) as kube:
            HOST.set_role("operator")
            with self.assertRaisesRegex(PermissionError, "only an admin"):
                K.send("PATCH", DEPLOYMENTS.format(ns="lab") + "/router", {"spec": {"replicas": 0}},
                       ctype="application/merge-patch+json")
            self.assertEqual([], kube.writes, "nothing reached the API server")
            self.assertNotIn("replicas", kube.object(DEPLOYMENTS.format(ns="lab") + "/router")["spec"])

    def test_an_operator_cannot_create_a_privileged_workload(self):
        privileged = deployment("tools")
        privileged["spec"]["template"]["spec"]["containers"][0]["securityContext"] = {"privileged": True}
        with Kube() as kube:
            HOST.set_role("operator")
            with self.assertRaises(PermissionError):
                K.send("POST", DEPLOYMENTS.format(ns="lab"), privileged)
            self.assertEqual([], kube.writes)

    def test_an_operator_cannot_operate_a_workload_in_a_system_namespace(self):
        with Kube(deployment("coredns", ns="kube-system")) as kube:
            HOST.set_role("operator")
            with self.assertRaises(PermissionError):
                K.send("DELETE", DEPLOYMENTS.format(ns="kube-system") + "/coredns")
            self.assertEqual([], kube.writes)
            self.assertIsNotNone(kube.object(DEPLOYMENTS.format(ns="kube-system") + "/coredns"))

    def test_an_operator_can_change_an_ordinary_workload(self):
        with Kube(deployment("frigate")) as kube:
            HOST.set_role("operator")
            K.send("PATCH", DEPLOYMENTS.format(ns="lab") + "/frigate", {"spec": {"replicas": 2}},
                   ctype="application/merge-patch+json")
            self.assertEqual([("PATCH", DEPLOYMENTS.format(ns="lab") + "/frigate")], self.written(kube, "PATCH"))
            self.assertEqual(2, kube.object(DEPLOYMENTS.format(ns="lab") + "/frigate")["spec"]["replicas"])

    def test_an_admin_and_homestead_itself_can_change_a_host_privileged_workload(self):
        for role in ("admin", None):
            with self.subTest(role=role), Kube(deployment("router", hostNetwork=True)) as kube:
                HOST.set_role(role)
                K.send("PATCH", DEPLOYMENTS.format(ns="lab") + "/router", {"spec": {"replicas": 0}},
                       ctype="application/merge-patch+json")
                self.assertEqual(1, len(self.written(kube, "PATCH")))

    def test_a_job_started_by_an_operator_is_checked_as_that_operator(self):
        with Kube(deployment("router", hostNetwork=True)) as kube:
            with HOST.acting_as("operator"), self.assertRaises(PermissionError):
                K.send("DELETE", DEPLOYMENTS.format(ns="lab") + "/router")
            self.assertEqual([], kube.writes)

    def test_writes_that_are_not_to_workloads_pass_this_fence(self):
        with Kube() as kube:
            HOST.set_role("operator")
            K.send("POST", CONFIGMAPS, {"apiVersion": "v1", "kind": "ConfigMap",
                                        "metadata": {"name": "notes", "namespace": "lab"}, "data": {"a": "1"}})
            self.assertEqual(1, len(self.written(kube, "POST")))

    def test_an_operator_cannot_change_a_cronjob_that_runs_with_host_access(self):
        for pod in ({"serviceAccountName": "backup-runner"},
                    {"volumes": [{"name": "root", "hostPath": {"path": "/"}}]},
                    {"hostPID": True}):
            with self.subTest(pod=pod), Kube(cronjob("nightly", **pod)) as kube:
                edited = cronjob("nightly", **pod)
                edited["spec"]["jobTemplate"]["spec"]["template"]["spec"]["containers"][0]["command"] = ["sh", "-c", "id"]
                HOST.set_role("operator")
                with self.assertRaisesRegex(PermissionError, "only an admin"):
                    K.send("PUT", CRONJOBS + "/nightly", edited)
                with self.assertRaises(PermissionError):
                    K.send("DELETE", CRONJOBS + "/nightly")
                self.assertEqual([], kube.writes, "nothing reached the API server")

    def test_an_operator_cannot_give_a_cronjob_host_access(self):
        privileged = cronjob("nightly")
        privileged["spec"]["jobTemplate"]["spec"]["template"]["spec"]["containers"][0]["securityContext"] = {"privileged": True}
        for method, path in (("POST", CRONJOBS), ("PUT", CRONJOBS + "/nightly")):
            with self.subTest(method=method), Kube(cronjob("nightly")) as kube:
                HOST.set_role("operator")
                with self.assertRaises(PermissionError):
                    K.send(method, path, privileged)
                self.assertEqual([], kube.writes)

    def test_an_operator_can_change_an_ordinary_cronjob(self):
        with Kube(cronjob("nightly")) as kube:
            edited = cronjob("nightly")
            edited["spec"]["schedule"] = "0 4 * * *"
            HOST.set_role("operator")
            K.send("PUT", CRONJOBS + "/nightly", edited)
            self.assertEqual("0 4 * * *", kube.object(CRONJOBS + "/nightly")["spec"]["schedule"])

    def test_an_admin_can_change_a_cronjob_that_runs_with_host_access(self):
        with Kube(cronjob("nightly", hostPID=True)) as kube:
            HOST.set_role("admin")
            K.send("PUT", CRONJOBS + "/nightly", cronjob("nightly", hostPID=True))
            self.assertEqual(1, len(self.written(kube, "PUT")))

    def test_a_deployment_is_checked_on_the_pod_template_it_runs(self):
        body = deployment("tools")
        body["spec"]["template"]["spec"]["containers"][0]["securityContext"] = {"privileged": True}
        body["spec"]["jobTemplate"] = cronjob("decoy")["spec"]["jobTemplate"]
        self.assertIn("privileged mode", HOST.grants(body))
        with Kube() as kube:
            HOST.set_role("operator")
            with self.assertRaises((PermissionError, ValueError)):
                K.send("POST", DEPLOYMENTS.format(ns="lab"), body)
            self.assertEqual([], kube.writes)

    def test_a_cronjob_is_checked_on_its_job_template(self):
        body = cronjob("nightly", hostPID=True)
        body["spec"]["template"] = deployment("decoy")["spec"]["template"]
        self.assertIn("the host's processes", HOST.grants(body))
        with Kube() as kube:
            HOST.set_role("operator")
            with self.assertRaises((PermissionError, ValueError)):
                K.send("POST", CRONJOBS, body)
            self.assertEqual([], kube.writes)

    def test_a_pod_is_checked_on_its_own_spec(self):
        pods = "/api/v1/namespaces/lab/pods"
        plain = {"apiVersion": "v1", "kind": "Pod", "metadata": {"name": "tools", "namespace": "lab"},
                 "spec": {"hostNetwork": True, "containers": [{"name": "tools", "image": "example.com/t:1"}]}}
        decoyed = {**plain, "spec": {**plain["spec"], "template": deployment("decoy")["spec"]["template"]}}
        for body in (plain, decoyed):
            with self.subTest(decoy="template" in body["spec"]), Kube() as kube:
                self.assertIn("the host's network", HOST.grants(body))
                HOST.set_role("operator")
                with self.assertRaises((PermissionError, ValueError)):
                    K.send("POST", pods, body)
                self.assertEqual([], kube.writes)

    def test_a_template_that_does_not_belong_to_the_kind_is_refused(self):
        cases = (("POST", DEPLOYMENTS.format(ns="lab"), {**deployment("web"), "spec": {
                     **deployment("web")["spec"], "jobTemplate": cronjob("web")["spec"]["jobTemplate"]}}),
                 ("POST", CRONJOBS, {**cronjob("web"), "spec": {
                     **cronjob("web")["spec"], "template": deployment("web")["spec"]["template"]}}))
        for method, path, body in cases:
            with self.subTest(path=path), Kube() as kube:
                HOST.set_role("operator")
                with self.assertRaisesRegex(ValueError, "has no"):
                    K.send(method, path, body)
                self.assertEqual([], kube.writes)

    def test_either_service_account_field_needs_an_admin(self):
        for field in ("serviceAccountName", "serviceAccount"):
            with self.subTest(field=field), Kube() as kube:
                HOST.set_role("operator")
                with self.assertRaisesRegex(PermissionError, "only an admin"):
                    K.send("POST", DEPLOYMENTS.format(ns="lab"), deployment("tools", **{field: "builder"}))
                with self.assertRaisesRegex(PermissionError, "only an admin"):
                    K.send("POST", CRONJOBS, cronjob("nightly", **{field: "builder"}))
                self.assertEqual([], kube.writes)

    def test_a_json_patch_adding_a_privileged_container_is_refused(self):
        path = DEPLOYMENTS.format(ns="lab") + "/frigate"
        patch = [{"op": "add", "path": "/spec/template/spec/containers/-",
                  "value": {"name": "x", "image": "example.com/x:1", "securityContext": {"privileged": True}}}]
        with Kube(deployment("frigate")) as kube:
            HOST.set_role("operator")
            with self.assertRaisesRegex(PermissionError, "only an admin"):
                K.send("PATCH", path, patch, ctype="application/json-patch+json")
            self.assertEqual([], kube.writes)

    def test_a_merge_patch_setting_a_service_account_is_refused(self):
        path = DEPLOYMENTS.format(ns="lab") + "/frigate"
        for patch in ({"spec": {"template": {"spec": {"serviceAccount": "builder"}}}},
                      {"spec": {"template": {"spec": {"serviceAccountName": "builder"}}}}):
            with self.subTest(patch=patch), Kube(deployment("frigate")) as kube:
                HOST.set_role("operator")
                with self.assertRaisesRegex(PermissionError, "only an admin"):
                    K.send("PATCH", path, patch, ctype="application/merge-patch+json")
                self.assertEqual([], kube.writes)

    def test_a_patch_adding_a_privileged_debug_container_is_refused(self):
        path = "/api/v1/namespaces/lab/pods/tools"
        pod = {"apiVersion": "v1", "kind": "Pod", "metadata": {"name": "tools", "namespace": "lab"},
               "spec": {"containers": [{"name": "tools", "image": "example.com/t:1"}]}}
        patch = {"spec": {"ephemeralContainers": [{"name": "debug", "image": "example.com/d:1",
                                                   "securityContext": {"privileged": True}}]}}
        with Kube(pod) as kube:
            HOST.set_role("operator")
            with self.assertRaises(PermissionError):
                K.send("PATCH", path, patch, ctype="application/strategic-merge-patch+json")
            self.assertEqual([], kube.writes)

    def test_a_patch_that_cannot_be_applied_is_refused(self):
        path = DEPLOYMENTS.format(ns="lab") + "/frigate"
        for patch in ([{"op": "replace", "path": "/spec/nothing/here", "value": 1}],
                      [{"op": "unknown", "path": "/spec"}], "not a patch"):
            with self.subTest(patch=patch), Kube(deployment("frigate")) as kube:
                HOST.set_role("operator")
                with self.assertRaises(ValueError):
                    K.send("PATCH", path, patch, ctype="application/json-patch+json")
                self.assertEqual([], kube.writes)

    def test_homesteads_own_patches_still_pass_for_an_operator(self):
        path = DEPLOYMENTS.format(ns="lab") + "/frigate"
        patches = (({"spec": {"replicas": 0}}, "application/merge-patch+json"),
                   ({"metadata": {"annotations": {"example.com/primary-port": "8080"}}}, "application/merge-patch+json"),
                   ({"spec": {"template": {"spec": {"priorityClassName": "low"}}}}, "application/strategic-merge-patch+json"),
                   ([{"op": "test", "path": "/metadata/name", "value": "frigate"},
                     {"op": "add", "path": "/metadata/labels", "value": {"tier": "low"}},
                     {"op": "replace", "path": "/spec/template/spec/containers/0/image", "value": "example.com/app:2"}],
                    "application/json-patch+json"))
        for patch, ctype in patches:
            with self.subTest(patch=patch), Kube(deployment("frigate")) as kube:
                HOST.set_role("operator")
                K.send("PATCH", path, patch, ctype=ctype)
                self.assertEqual(1, len(self.written(kube, "PATCH")))

    def test_the_self_data_fence_stops_a_write_while_homesteads_data_is_moved(self):
        held = []

        @contextlib.contextmanager
        def activity():
            held.append("held")
            try:
                yield
            finally:
                held.append("released")

        def moving():
            raise RuntimeError("Homestead's data is being moved")

        with Kube() as kube, mock.patch.object(K, "self_data_activity", activity), \
                mock.patch.object(K, "require_self_data_write", moving):
            with self.assertRaisesRegex(RuntimeError, "being moved"):
                K.send("POST", CONFIGMAPS, {"apiVersion": "v1", "kind": "ConfigMap",
                                            "metadata": {"name": "notes", "namespace": "lab"}})
            self.assertEqual([], kube.writes)
        self.assertEqual(["held", "released"], held, "the fence is let go after a refusal")

    def test_the_self_data_fence_holds_while_the_write_runs(self):
        seen = []

        @contextlib.contextmanager
        def activity():
            seen.append("held")
            yield
            seen.append("released")

        with Kube() as kube, mock.patch.object(K, "self_data_activity", activity):
            kube.react("POST", CONFIGMAPS, lambda *a, **k: seen.append("written") or None)
            K.send("POST", CONFIGMAPS, {"apiVersion": "v1", "kind": "ConfigMap",
                                        "metadata": {"name": "notes", "namespace": "lab"}})
        self.assertEqual(["held", "written", "released"], seen)


if __name__ == "__main__":
    unittest.main()
