"""tests/fakes/kube.py, the one fake Kubernetes API the tests share (#619):
it answers like the API server and plugs in at homestead_kube.transport, so
reads go through kget and writes through WRITE_GUARDS like the real ones."""
import json
import os
import sys
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.environ.get("HOMESTEAD_TEST_SERVER_DIR", str(Path(__file__).resolve().parents[1] / "server")))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import homestead_kube as K
from fakes.kube import Kube

PODS = "/api/v1/namespaces/lab/pods"
NODES = "/api/v1/nodes"
DEPLOYS = "/apis/apps/v1/namespaces/lab/deployments"
MERGE = "application/merge-patch+json"
JSON_PATCH = "application/json-patch+json"
STRATEGIC = "application/strategic-merge-patch+json"


def pod(name, ns="lab", labels=None, node="host-1", **meta):
    return {"apiVersion": "v1", "kind": "Pod",
            "metadata": {"name": name, "namespace": ns, "labels": labels or {}, **meta},
            "spec": {"nodeName": node}, "status": {"phase": "Running"}}


def names(listing):
    return [row["metadata"]["name"] for row in listing["items"]]


class Recorder:
    def __init__(self, *answers):
        self.calls, self.answers = [], list(answers)

    def __call__(self, method, path, body=None, ctype=None, timeout=15):
        self.calls.append((method, path, body, ctype))
        answer = self.answers.pop(0) if self.answers else {}
        if isinstance(answer, Exception):
            raise answer
        return answer


def http_error(code):
    return urllib.error.HTTPError("/x", code, "error", {}, None)


class TransportSeamTests(unittest.TestCase):
    """transport is the one place a request leaves Homestead."""

    def test_a_read_goes_through_transport(self):
        transport = Recorder({"kind": "Pod"})
        with mock.patch.object(K, "transport", transport):
            self.assertEqual(K.kget(PODS + "/a"), {"kind": "Pod"})
        self.assertEqual(transport.calls, [("GET", PODS + "/a", None, None)])

    def test_a_write_reaches_transport_with_its_body_and_type(self):
        transport = Recorder({"ok": 1})
        with mock.patch.object(K, "transport", transport), mock.patch.object(K, "FENCE_READY", False):
            self.assertEqual(K._ksend("PATCH", PODS + "/a", {"x": 1}, "application/merge-patch+json"), {"ok": 1})
        self.assertEqual(transport.calls, [("PATCH", PODS + "/a", {"x": 1}, "application/merge-patch+json")])

    def test_kget_still_retries_a_throttled_read(self):
        transport = Recorder(http_error(429), http_error(429), {"kind": "Pod"})
        with mock.patch.object(K, "transport", transport), mock.patch.object(K.time, "sleep"):
            self.assertEqual(K.kget(PODS + "/a"), {"kind": "Pod"})
        self.assertEqual(len(transport.calls), 3)

    def test_a_path_that_steps_out_never_reaches_transport(self):
        transport = Recorder()
        with mock.patch.object(K, "transport", transport):
            for call in (lambda: K.kget(PODS + "/.."), lambda: K._ksend("DELETE", PODS + "/..")):
                with self.assertRaises(ValueError):
                    call()
        self.assertEqual(transport.calls, [])


class ReadTests(unittest.TestCase):
    def test_an_object_is_read_back_at_its_api_path(self):
        with Kube(pod("a")) as kube:
            got = K.kget(PODS + "/a")
        self.assertEqual(got["metadata"]["name"], "a")
        self.assertEqual(kube.path_of(pod("a")), PODS + "/a")

    def test_a_missing_object_is_a_404_like_the_api_servers(self):
        with Kube():
            with self.assertRaises(urllib.error.HTTPError) as caught:
                K.kget(PODS + "/a")
        error = caught.exception
        self.assertEqual(error.code, 404)
        status = json.loads(error.read())
        self.assertEqual((status["kind"], status["reason"], status["code"]), ("Status", "NotFound", 404))
        self.assertEqual(status["message"], 'pods "a" not found')
        error.close()

    def test_get_optional_and_items_answer_from_the_fake(self):
        with Kube(pod("a"), pod("b")):
            self.assertIsNone(K.get_optional(PODS + "/c"))
            self.assertEqual(sorted(r["metadata"]["name"] for r in K.items(PODS)), ["a", "b"])

    def test_what_is_read_is_a_copy(self):
        with Kube(pod("a")) as kube:
            K.kget(PODS + "/a")["metadata"]["name"] = "changed"
            self.assertEqual(kube.object(PODS + "/a")["metadata"]["name"], "a")

    def test_cluster_scoped_and_api_group_objects_have_their_own_paths(self):
        node = {"apiVersion": "v1", "kind": "Node", "metadata": {"name": "host-1"}}
        deploy = {"apiVersion": "apps/v1", "kind": "Deployment", "metadata": {"name": "web", "namespace": "lab"}}
        claim = {"apiVersion": "v1", "kind": "PersistentVolumeClaim", "metadata": {"name": "d", "namespace": "lab"}}
        with Kube(node, deploy, claim) as kube:
            self.assertEqual(K.kget(NODES + "/host-1")["kind"], "Node")
            self.assertEqual(K.kget(DEPLOYS + "/web")["kind"], "Deployment")
            self.assertEqual(kube.path_of(claim), "/api/v1/namespaces/lab/persistentvolumeclaims/d")
            self.assertEqual(names(K.kget("/apis/apps/v1/deployments")), ["web"])

    def test_a_resource_whose_path_name_is_not_its_kind_made_plural(self):
        nad = {"apiVersion": "k8s.cni.cncf.io/v1", "kind": "NetworkAttachmentDefinition",
               "metadata": {"name": "vlan1", "namespace": "default"}}
        path = "/apis/k8s.cni.cncf.io/v1/namespaces/default/network-attachment-definitions/vlan1"
        with Kube(nad) as kube:
            self.assertEqual(kube.path_of(nad), path)
            self.assertEqual(K.kget(path)["metadata"]["name"], "vlan1")

    def test_an_object_is_served_at_every_version_of_its_group(self):
        dv = {"apiVersion": "cdi.kubevirt.io/v1beta1", "kind": "DataVolume", "metadata": {"name": "d", "namespace": "lab"}}
        with Kube(dv):
            self.assertEqual(K.kget("/apis/cdi.kubevirt.io/v1/namespaces/lab/datavolumes/d")["metadata"]["name"], "d")

    def test_a_list_is_one_namespace_or_all_of_them(self):
        with Kube(pod("a"), pod("b", ns="other")):
            listing = K.kget(PODS)
            self.assertEqual(names(listing), ["a"])
            self.assertEqual(listing["kind"], "PodList")
            self.assertTrue(listing["metadata"]["resourceVersion"])
            self.assertEqual(names(K.kget("/api/v1/pods")), ["a", "b"])

    def test_label_selectors(self):
        with Kube(pod("a", labels={"app": "web", "tier": "front"}), pod("b", labels={"app": "db"}), pod("c")):
            for selector, expected in (("app=web", ["a"]), ("app%3D%3Dweb", ["a"]), ("app!=web", ["b", "c"]),
                                       ("app", ["a", "b"]), ("!app", ["c"]), ("app in (web,db)", ["a", "b"]),
                                       ("app notin (web)", ["b", "c"]), ("app=web,tier=front", ["a"]),
                                       ("app=web,tier=back", [])):
                with self.subTest(selector=selector):
                    self.assertEqual(names(K.kget(PODS + "?labelSelector=" + selector.replace(" ", "%20"))), expected)

    def test_field_selectors(self):
        with Kube(pod("a", node="host-1"), pod("b", node="host-2")):
            self.assertEqual(names(K.kget(PODS + "?fieldSelector=spec.nodeName%3Dhost-2")), ["b"])
            self.assertEqual(names(K.kget(PODS + "?fieldSelector=spec.nodeName!=host-2")), ["a"])
            self.assertEqual(names(K.kget(PODS + "?fieldSelector=metadata.name=a,status.phase=Running")), ["a"])

    def test_a_limited_list_pages_with_continue(self):
        with Kube(pod("a"), pod("b"), pod("c")):
            first = K.kget(PODS + "?limit=2")
            self.assertEqual(names(first), ["a", "b"])
            token = first["metadata"]["continue"]
            second = K.kget(PODS + "?limit=2&continue=" + token)
            self.assertEqual(names(second), ["c"])
            self.assertNotIn("continue", second["metadata"])
            with self.assertRaises(ValueError):
                K.items(PODS + "?limit=2")  # a list cut short is never taken as whole

    def test_discovery_lists_the_groups_held_and_an_absent_group_is_not_found(self):
        deploy = {"apiVersion": "apps/v1", "kind": "Deployment", "metadata": {"name": "web", "namespace": "lab"}}
        snaps = "/apis/snapshot.storage.k8s.io/v1/volumesnapshots"
        with Kube(deploy, groups=("longhorn.io",), absent_groups=("snapshot.storage.k8s.io",)):
            groups = [g["name"] for g in K.kget("/apis")["groups"]]
            self.assertIn("apps", groups)
            self.assertIn("longhorn.io", groups)
            self.assertNotIn("snapshot.storage.k8s.io", groups)
            with self.assertRaises(urllib.error.HTTPError) as caught:
                K.kget(snaps)
            self.assertEqual(caught.exception.code, 404)
            self.assertEqual(K.items(snaps, optional_group="snapshot.storage.k8s.io"), [])
        with Kube():
            self.assertEqual(K.items(snaps), [], "a group that is not declared absent is installed and empty")


class WriteTests(unittest.TestCase):
    def test_create_fills_in_what_the_api_server_does(self):
        with Kube() as kube:
            made = K.ksend("POST", PODS, pod("a"))
            meta = made["metadata"]
            self.assertTrue(meta["uid"] and meta["resourceVersion"] and meta["creationTimestamp"])
            self.assertEqual(meta["namespace"], "lab")
            self.assertEqual(kube.object(PODS + "/a")["metadata"]["uid"], meta["uid"])
            named = K.ksend("POST", PODS, {"apiVersion": "v1", "kind": "Pod", "metadata": {"generateName": "job-"}})
            self.assertTrue(named["metadata"]["name"].startswith("job-"))

    def test_create_of_a_name_that_is_taken_is_a_409(self):
        with Kube(pod("a")) as kube:
            with self.assertRaises(urllib.error.HTTPError) as caught:
                K.ksend("POST", PODS, pod("a"))
            self.assertEqual(caught.exception.code, 409)
            self.assertEqual(json.loads(caught.exception.read())["reason"], "AlreadyExists")

    def test_replace_needs_the_current_resource_version(self):
        with Kube(pod("a")) as kube:
            current = K.kget(PODS + "/a")
            current["spec"]["nodeName"] = "host-2"
            replaced = K.ksend("PUT", PODS + "/a", current)
            self.assertNotEqual(replaced["metadata"]["resourceVersion"], current["metadata"]["resourceVersion"])
            self.assertEqual(replaced["metadata"]["uid"], current["metadata"]["uid"])
            with self.assertRaises(urllib.error.HTTPError) as caught:
                K.ksend("PUT", PODS + "/a", current)  # stale now
            self.assertEqual(caught.exception.code, 409)
            self.assertEqual(json.loads(caught.exception.read())["reason"], "Conflict")
            self.assertEqual(kube.object(PODS + "/a")["spec"]["nodeName"], "host-2")
            with self.assertRaises(urllib.error.HTTPError) as caught:
                K.ksend("PUT", PODS + "/missing", pod("missing"))
            self.assertEqual(caught.exception.code, 404)

    def test_merge_patch_merges_and_null_removes(self):
        with Kube(pod("a", labels={"app": "web", "old": "x"}, ownerReferences=[{"uid": "u1"}])) as kube:
            K.ksend("PATCH", PODS + "/a", {"metadata": {"labels": {"old": None, "new": "y"}, "ownerReferences": None}},
                    ctype=MERGE)
            meta = kube.object(PODS + "/a")["metadata"]
            self.assertEqual(meta["labels"], {"app": "web", "new": "y"})
            self.assertNotIn("ownerReferences", meta)

    def test_a_patch_carrying_a_stale_resource_version_is_a_409(self):
        with Kube(pod("a")):
            rv = K.kget(PODS + "/a")["metadata"]["resourceVersion"]
            K.ksend("PATCH", PODS + "/a", {"metadata": {"labels": {"x": "1"}}}, ctype=MERGE)
            with self.assertRaises(urllib.error.HTTPError) as caught:
                K.ksend("PATCH", PODS + "/a", {"metadata": {"resourceVersion": rv, "labels": {"x": "2"}}}, ctype=MERGE)
            self.assertEqual(caught.exception.code, 409)

    def test_json_patch_with_a_failing_test_changes_nothing(self):
        with Kube(pod("a")) as kube:
            rv = K.kget(PODS + "/a")["metadata"]["resourceVersion"]
            K.ksend("PATCH", PODS + "/a", [{"op": "test", "path": "/metadata/resourceVersion", "value": rv},
                                           {"op": "add", "path": "/metadata/labels/app", "value": "web"},
                                           {"op": "replace", "path": "/spec/nodeName", "value": "host-2"}], ctype=JSON_PATCH)
            self.assertEqual(kube.object(PODS + "/a")["metadata"]["labels"], {"app": "web"})
            with self.assertRaises(urllib.error.HTTPError) as caught:
                K.ksend("PATCH", PODS + "/a", [{"op": "test", "path": "/metadata/resourceVersion", "value": rv},
                                               {"op": "remove", "path": "/metadata/labels/app"}], ctype=JSON_PATCH)
            self.assertEqual(caught.exception.code, 422)
            self.assertEqual(kube.object(PODS + "/a")["metadata"]["labels"], {"app": "web"})
            K.ksend("PATCH", PODS + "/a", [{"op": "add", "path": "/spec/tolerations", "value": []},
                                           {"op": "add", "path": "/spec/tolerations/-", "value": {"key": "k"}},
                                           {"op": "remove", "path": "/metadata/labels/app"}], ctype=JSON_PATCH)
            obj = kube.object(PODS + "/a")
            self.assertEqual((obj["spec"]["tolerations"], obj["metadata"]["labels"]), ([{"key": "k"}], {}))

    def test_strategic_merge_merges_named_lists(self):
        deploy = {"apiVersion": "apps/v1", "kind": "Deployment", "metadata": {"name": "web", "namespace": "lab"},
                  "spec": {"template": {"spec": {
                      "containers": [{"name": "app", "image": "a:1"}, {"name": "side", "image": "s:1"}],
                      "volumes": [{"name": "data", "emptyDir": {}}, {"name": "old", "emptyDir": {}}]}}}}
        with Kube(deploy) as kube:
            K.ksend("PATCH", DEPLOYS + "/web", {"spec": {"template": {"spec": {
                "containers": [{"name": "app", "image": "a:2"}],
                "volumes": [{"name": "auth", "secret": {"secretName": "key"}}, {"name": "old", "$patch": "delete"}]}}}},
                ctype=STRATEGIC)
            spec = kube.object(DEPLOYS + "/web")["spec"]["template"]["spec"]
            self.assertEqual(spec["containers"], [{"name": "app", "image": "a:2"}, {"name": "side", "image": "s:1"}])
            self.assertEqual([v["name"] for v in spec["volumes"]], ["data", "auth"])

    def test_a_status_write_lands_on_the_object(self):
        with Kube(pod("a")) as kube:
            K.ksend("PATCH", PODS + "/a/status", {"status": {"phase": "Failed"}}, ctype=MERGE)
            self.assertEqual(kube.object(PODS + "/a")["status"]["phase"], "Failed")

    def test_delete_checks_its_preconditions(self):
        with Kube(pod("a")) as kube:
            with self.assertRaises(urllib.error.HTTPError) as caught:
                K.ksend("DELETE", PODS + "/a", {"preconditions": {"uid": "someone-else"}})
            self.assertEqual(caught.exception.code, 409)
            uid = kube.object(PODS + "/a")["metadata"]["uid"]
            K.ksend("DELETE", PODS + "/a", {"preconditions": {"uid": uid}})
            self.assertIsNone(kube.object(PODS + "/a"))
            self.assertIsNone(K.delete_if_present(PODS + "/a"))

    def test_deleting_an_owner_takes_what_it_alone_owns(self):
        owner = pod("owner", uid="u1")
        owned = pod("owned", ownerReferences=[{"uid": "u1"}])
        shared = pod("shared", ownerReferences=[{"uid": "u1"}, {"uid": "u2"}])
        other = pod("other", uid="u2")
        with Kube(owner, owned, shared, other) as kube:
            K.ksend("DELETE", PODS + "/owner", {"propagationPolicy": "Foreground"})
            self.assertIsNone(kube.object(PODS + "/owned"))
            self.assertIsNotNone(kube.object(PODS + "/shared"), "a dependent with an owner left stays")
        with Kube(pod("owner", uid="u1"), pod("owned", ownerReferences=[{"uid": "u1"}])) as kube:
            K.ksend("DELETE", PODS + "/owner", {"propagationPolicy": "Orphan"})
            self.assertIsNotNone(kube.object(PODS + "/owned"))

    def test_a_finalizer_holds_a_deleted_object_until_it_is_cleared(self):
        with Kube(pod("a", finalizers=["example.com/hold"])) as kube:
            K.ksend("DELETE", PODS + "/a")
            self.assertTrue(kube.object(PODS + "/a")["metadata"]["deletionTimestamp"])
            K.ksend("PATCH", PODS + "/a", {"metadata": {"finalizers": None}}, ctype=MERGE)
            self.assertIsNone(kube.object(PODS + "/a"))

    def test_every_write_is_logged(self):
        with Kube(pod("a")) as kube:
            K.ksend("PATCH", PODS + "/a", {"metadata": {"labels": {"x": "1"}}}, ctype=MERGE)
            K.ksend("DELETE", PODS + "/a")
            K.kget(PODS)
        self.assertEqual([(m, p) for m, p, _, _ in kube.writes], [("PATCH", PODS + "/a"), ("DELETE", PODS + "/a")])
        self.assertEqual(kube.writes[0][3], MERGE)

    def test_writes_go_round_the_write_guards(self):
        def refuse(method, path, body, proceed):
            raise ValueError("refused by a guard")
        with Kube(pod("a")) as kube, mock.patch.object(K, "WRITE_GUARDS", [refuse] + K.WRITE_GUARDS):
            with self.assertRaisesRegex(ValueError, "refused by a guard"):
                K.ksend("DELETE", PODS + "/a")
            self.assertIsNotNone(kube.object(PODS + "/a"))
            self.assertEqual(kube.writes, [])

    def test_writes_pass_the_shutdown_fence_once_server_py_has_armed_it(self):
        with Kube() as kube, mock.patch.object(K, "FENCE_READY", True):
            K.ksend("POST", PODS, pod("a"))
            self.assertIsNotNone(kube.object(PODS + "/a"))

    def test_the_route_needs_are_given_back_afterwards(self):
        before = {name: getattr(K, name, None) for name in K.ROUTE_NEEDS}
        with Kube():
            K.ksend("POST", PODS, pod("a"))
        self.assertEqual({name: getattr(K, name, None) for name in K.ROUTE_NEEDS}, before)
        self.assertIsNot(K.transport, None)
        self.assertEqual(K.transport.__name__, "transport")


class FailureTests(unittest.TestCase):
    def test_a_path_fails_the_times_it_is_told_to(self):
        with Kube(pod("a")) as kube:
            kube.fail(PODS + "/a", 500, times=2)
            for _ in range(2):
                with self.assertRaises(urllib.error.HTTPError) as caught:
                    K.kget(PODS + "/a")
                self.assertEqual(caught.exception.code, 500)
            self.assertEqual(K.kget(PODS + "/a")["metadata"]["name"], "a")

    def test_a_throttled_read_is_retried_by_kget(self):
        with Kube(pod("a")) as kube, mock.patch.object(K.time, "sleep") as slept:
            kube.fail(PODS + "/a", 429, times=2)
            self.assertEqual(K.kget(PODS + "/a")["metadata"]["name"], "a")
        self.assertEqual(slept.call_count, 2)

    def test_a_failure_can_be_for_one_method(self):
        with Kube(pod("a")) as kube:
            kube.fail(PODS + "/a", 403, method="DELETE")
            self.assertEqual(K.kget(PODS + "/a")["metadata"]["name"], "a")
            with self.assertRaises(urllib.error.HTTPError) as caught:
                K.ksend("DELETE", PODS + "/a")
            self.assertEqual(caught.exception.code, 403)
            self.assertIsNotNone(kube.object(PODS + "/a"))

    def test_a_reaction_answers_in_place_of_the_fake(self):
        with Kube(pod("a")) as kube:
            kube.react("PATCH", PODS + "/a", lambda method, path, body: {"ignored": True})
            self.assertEqual(K.ksend("PATCH", PODS + "/a", {"metadata": {"labels": {"x": "1"}}}, ctype=MERGE),
                             {"ignored": True})
            self.assertEqual(kube.object(PODS + "/a")["metadata"]["labels"], {})
            kube.react("GET", PODS + "/a/log", lambda *_: "line one")
            self.assertEqual(K.kget(PODS + "/a/log"), "line one")


    def test_an_object_can_be_taken_away_without_a_write_of_the_apps(self):
        owner = {"apiVersion": "batch/v1", "kind": "Job", "metadata": {"name": "j", "namespace": "lab", "uid": "job-uid"}}
        owned = pod("p", ownerReferences=[{"kind": "Job", "name": "j", "uid": "job-uid"}])
        with Kube(owner, owned, pod("other")) as kube:
            kube.discard("/apis/batch/v1/namespaces/lab/jobs/j")
            self.assertIsNone(kube.object("/apis/batch/v1/namespaces/lab/jobs/j"))
            self.assertEqual(names(K.kget(PODS)), ["other"], "what it owned is collected, as after a delete")
            self.assertEqual(kube.writes, [], "it is the test changing the cluster, not a write of the app's")
            with self.assertRaises(urllib.error.HTTPError) as caught:
                K.kget("/apis/batch/v1/namespaces/lab/jobs/j")
            self.assertEqual(caught.exception.code, 404)
            kube.discard(PODS + "/not-there")  # already gone is no error


    def test_a_secrets_string_data_is_kept_as_base64_data(self):
        secrets = "/api/v1/namespaces/lab/secrets"
        with Kube() as kube:
            K.ksend("POST", secrets, {"apiVersion": "v1", "kind": "Secret", "metadata": {"name": "s", "namespace": "lab"},
                                     "stringData": {"password": "hunter2"}})
            self.assertEqual(K.kget(secrets + "/s")["data"], {"password": "aHVudGVyMg=="})
            self.assertNotIn("stringData", K.kget(secrets + "/s"))
            K.ksend("PATCH", secrets + "/s", {"stringData": {"user": "ada"}}, ctype=MERGE)
            self.assertEqual(K.kget(secrets + "/s")["data"], {"password": "aHVudGVyMg==", "user": "YWRh"})
            K.ksend("PUT", secrets + "/s", {"apiVersion": "v1", "kind": "Secret", "metadata": {"name": "s", "namespace": "lab"},
                                           "stringData": {"password": "x"}})
            self.assertEqual(K.kget(secrets + "/s")["data"], {"password": "eA=="})
            self.assertNotIn("stringData", kube.object(secrets + "/s"))


if __name__ == "__main__":
    unittest.main()
