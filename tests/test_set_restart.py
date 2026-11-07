import copy
import sys
import tempfile
import unittest
from unittest import mock
import urllib.error
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
import homestead_operations as OPS
import homestead_route_policy as POLICY
import homestead_routes as ROUTER
import homestead_set_restart as sr
sys.path.insert(0, str(Path(__file__).resolve().parent))
from rebind import keep_bound

LH = sr.LH
PODS = "/api/v1/namespaces/lab/pods?labelSelector=app%3Dweb"


def pod(name, uid, node="host-1", phase="Running", deleting=False, owner="web", claim="data-web-0"):
    meta = {"name": name, "uid": uid, "ownerReferences": [{"kind": "StatefulSet", "name": owner}]}
    if deleting:
        meta["deletionTimestamp"] = "2026-10-17T10:00:00Z"
    return {"metadata": meta, "status": {"phase": phase}, "spec": {
        "nodeName": node, "volumes": [{"name": "d", "persistentVolumeClaim": {"claimName": claim}}]}}


def ready(status, since="2026-10-17T09:00:00Z"):
    return [{"type": "Ready", "status": status, "lastTransitionTime": since}]


class SetRestartReadTests(unittest.TestCase):
    def setUp(self):
        self.objects = {
            "/api/v1/nodes": {"items": [
                {"metadata": {"name": "host-1"}, "status": {"conditions": ready("Unknown")}},
                {"metadata": {"name": "host-2"}, "status": {"conditions": ready("True")}}]},
            "/apis/coordination.k8s.io/v1/namespaces/kube-node-lease/leases/host-1": {
                "spec": {"renewTime": "2026-10-17T08:59:00.000000Z"}},
            "/apis": {"groups": [{"name": "apps"}, {"name": "longhorn.io"}]},
            "/apis/apps/v1/namespaces/lab/statefulsets/web": {
                "spec": {"selector": {"matchLabels": {"app": "web"}}, "template": {"spec": {}}}},
            PODS: {"items": [pod("web-0", "u0"), pod("web-1", "u1", node="host-2"),
                             pod("other-0", "u9", owner="other")]},
            "/api/v1/namespaces/lab/persistentvolumeclaims/data-web-0": {
                "spec": {"accessModes": ["ReadWriteOnce"], "volumeName": "pv-a"}},
            f"{LH}/nodes": {"items": [
                {"metadata": {"name": "host-1"}, "status": {"conditions": ready("False")}},
                {"metadata": {"name": "host-2"}, "status": {"conditions": ready("True")}}]},
            f"{LH}/replicas": {"items": [
                {"spec": {"nodeID": "host-1", "volumeName": "pv-a", "failedAt": "", "healthyAt": "x"}},
                {"spec": {"nodeID": "host-2", "volumeName": "pv-a", "failedAt": "t", "healthyAt": ""}}]},
            f"{LH}/engines": {"items": [
                {"spec": {"nodeID": "host-1", "volumeName": "pv-a"}, "status": {"currentState": "running"}}]},
            "/apis/longhorn.io/v1beta2/volumes": {"items": [
                {"metadata": {"name": "pv-a"}, "status": {"robustness": "degraded"}}]}}
        keep_bound(self, sr)
        sr.bind(self.get, None)

    def get(self, path, *a, **k):
        if path not in self.objects:
            raise urllib.error.HTTPError(path, 404, "not found", {}, None)
        return copy.deepcopy(self.objects[path])

    def refuses(self, path, words):
        del self.objects[path]
        with self.assertRaises(sr.Unread) as ctx:
            sr.read_state("lab", "web", "host-1")
        self.assertIn(words, str(ctx.exception))

    def test_reads_the_whole_state(self):
        st = sr.read_state("lab", "web", "host-1")
        self.assertEqual({"ready": False, "status": "Unknown", "since": "2026-10-17T09:00:00Z",
                          "lease": "2026-10-17T08:59:00.000000Z"}, st["node"])
        self.assertFalse(st["harvester"])
        self.assertEqual({"ns": "lab", "name": "web", "wait_mode": False}, st["set"])
        self.assertEqual({"host-1": False, "host-2": True}, st["nodes_ready"])
        self.assertEqual({"host-1": False, "host-2": True}, st["lh_nodes"])
        self.assertEqual({"data-web-0": {"access": ["ReadWriteOnce"], "volume": "pv-a", "longhorn": True}},
                         st["claims"])
        self.assertEqual([{"node": "host-1", "failed": False, "healthy_at": True},
                          {"node": "host-2", "failed": True, "healthy_at": False}], st["replicas"]["pv-a"])
        self.assertEqual({"pv-a": [{"node": "host-1", "state": "running"}]}, st["engines"])
        self.assertEqual({"pv-a": "degraded"}, st["robustness"])

    def test_missing_lease_is_none_not_unread(self):
        del self.objects["/apis/coordination.k8s.io/v1/namespaces/kube-node-lease/leases/host-1"]
        self.assertIsNone(sr.read_state("lab", "web", "host-1")["node"]["lease"])

    def test_harvester_group_is_seen(self):
        self.objects["/apis"]["groups"].append({"name": "harvesterhci.io"})
        self.assertTrue(sr.read_state("lab", "web", "host-1")["harvester"])

    def test_unreadable_longhorn_replicas_refuses(self):
        self.refuses(f"{LH}/replicas", "replicas")

    def test_unreadable_engines_refuses(self):
        self.refuses(f"{LH}/engines", "engines")

    def test_unreadable_longhorn_node_refuses(self):
        self.refuses(f"{LH}/nodes", "Longhorn nodes")

    def test_unreadable_api_groups_refuses(self):
        self.refuses("/apis", "API groups")

    def test_unreadable_pvc_refuses(self):
        self.refuses("/api/v1/namespaces/lab/persistentvolumeclaims/data-web-0", "data-web-0")

    def test_reads_terminating_and_failed_pods_with_uid(self):
        self.objects[PODS]["items"] = [pod("web-0", "u0", deleting=True),
                                       pod("web-2", "u2", phase="Failed"), pod("web-1", "u1", node="host-2")]
        pods = sr.read_state("lab", "web", "host-1")["pods"]
        self.assertEqual([("web-0", "u0", "host-1", True, ["data-web-0"]),
                          ("web-2", "u2", "host-1", False, ["data-web-0"])],
                         [(p["name"], p["uid"], p["node"], p["terminating"], p["claims"]) for p in pods])

    def test_malformed_object_is_unread_not_keyerror(self):
        self.objects[f"{LH}/replicas"]["items"].append(None)
        with self.assertRaises(sr.Unread) as ctx:
            sr.read_state("lab", "web", "host-1")
        self.assertIn("can't read", str(ctx.exception))

    def unreadable(self, kind, item):
        self.objects[f"{LH}/{kind}"]["items"].append(item)
        with self.assertRaises(sr.Unread) as ctx:
            sr.read_state("lab", "web", "host-1")
        self.assertIn("can't read", str(ctx.exception))


    def test_unscheduled_replica_is_skipped(self):
        self.objects[f"{LH}/replicas"]["items"].append({"spec": {"volumeName": "pv-a", "nodeID": "", "healthyAt": ""}})
        self.objects[f"{LH}/replicas"]["items"].append({"spec": {"volumeName": "pv-a"}})
        reps = sr.read_state("lab", "web", "host-1")["replicas"]["pv-a"]
        self.assertEqual(["host-1", "host-2"], [r["node"] for r in reps])

    def test_engine_without_volume_name_is_unread(self):
        self.unreadable("engines", {"spec": {"nodeID": "host-1"}, "status": {"currentState": "running"}})

    def test_reviewed_engine_without_node_id_is_skipped(self):
        # A detached engine has no host: it can't be running on the down one.
        self.objects[f"{LH}/engines"]["items"] += [
            {"metadata": {"name": "pv-a-e-1"}, "spec": {"volumeName": "pv-a", "nodeID": ""}, "status": {"currentState": "stopped"}},
            {"metadata": {"name": "pv-a-e-2"}, "spec": {"volumeName": "pv-a"}, "status": {"currentState": "stopped"}}]
        self.assertEqual({"pv-a": [{"node": "host-1", "state": "running"}]}, sr.read_state("lab", "web", "host-1")["engines"])

    def test_reviewed_engine_without_node_id_not_stopped_blocks(self):
        # Without a host and not stopped, Longhorn can't say it isn't running on the down host.
        for state in ("running", "unknown", None):
            with self.subTest(state=state):
                self.setUp()
                self.objects[f"{LH}/engines"]["items"][0]["status"]["currentState"] = "stopped"
                status = {} if state is None else {"currentState": state}
                self.objects[f"{LH}/engines"]["items"].append(
                    {"metadata": {"name": "pv-a-e-1"}, "spec": {"volumeName": "pv-a", "nodeID": ""}, "status": status})
                st = sr.read_state("lab", "web", "host-1")
                self.assertIn({"node": None, "state": state}, st["engines"]["pv-a"])
                self.assertIn(f"data-web-0: Longhorn can't say where the engine of pv-a runs (state: {state}).",
                              sr.plan(st, NOW)["blockers"])

    def test_other_volumes_are_ignored_even_when_malformed(self):
        # Detached volumes elsewhere in the cluster (empty nodeID, odd status)
        # must not block a set that doesn't use them.
        self.objects[f"{LH}/engines"]["items"] += [
            {"metadata": {"name": "pv-z-e-0"}, "spec": {"volumeName": "pv-z", "nodeID": ""}, "status": {"currentState": "stopped"}},
            {"metadata": {"name": "pv-y-e-0"}, "spec": {"volumeName": "pv-y"}, "status": "garbage"}]
        self.objects[f"{LH}/replicas"]["items"].append(
            {"metadata": {"name": "pv-z-r-0"}, "spec": {"volumeName": "pv-z", "nodeID": "host-2", "failedAt": None}, "status": "x"})
        self.objects["/apis/longhorn.io/v1beta2/volumes"]["items"] += [
            {"metadata": {"name": "pv-z"}, "status": "garbage"}, {"metadata": {"name": "pv-y"}}]
        st = sr.read_state("lab", "web", "host-1")
        self.assertEqual(["pv-a"], sorted(st["engines"]))
        self.assertEqual(["pv-a"], sorted(st["replicas"]))
        self.assertEqual({"pv-a": "degraded"}, st["robustness"])

    def test_engine_without_volume_name_names_the_engine(self):
        self.objects[f"{LH}/engines"]["items"].append(
            {"metadata": {"name": "pvc-x-e-0"}, "spec": {"nodeID": "host-1"}, "status": {"currentState": "running"}})
        with self.assertRaises(sr.Unread) as ctx:
            sr.read_state("lab", "web", "host-1")
        self.assertIn("Longhorn engine pvc-x-e-0 has no spec.volumeName", str(ctx.exception))

    def test_replica_without_spec_names_the_replica(self):
        self.objects[f"{LH}/replicas"]["items"].append({"metadata": {"name": "pvc-x-r-0"}})
        with self.assertRaises(sr.Unread) as ctx:
            sr.read_state("lab", "web", "host-1")
        self.assertIn("Longhorn replica pvc-x-r-0 has no spec", str(ctx.exception))
        self.assertNotIn("KeyError", str(ctx.exception))

    def test_malformed_reviewed_volume_names_it(self):
        self.objects["/apis/longhorn.io/v1beta2/volumes"]["items"][0]["status"] = "garbage"
        with self.assertRaises(sr.Unread) as ctx:
            sr.read_state("lab", "web", "host-1")
        self.assertIn("Longhorn volume pv-a", str(ctx.exception))
        self.assertNotIn("AttributeError", str(ctx.exception))

    def test_pod_without_uid_is_unread(self):
        del self.objects[PODS]["items"][0]["metadata"]["uid"]
        with self.assertRaises(sr.Unread) as ctx:
            sr.read_state("lab", "web", "host-1")
        self.assertIn("web-0", str(ctx.exception))
        self.assertIn("uid", str(ctx.exception))

    def test_lease_read_error_other_than_404_is_unread(self):
        get = self.get
        def failing(path, *a, **k):
            if "/leases/" in path:
                raise urllib.error.HTTPError(path, 500, "boom", {}, None)
            return get(path)
        sr.bind(failing, None)
        with self.assertRaises(sr.Unread) as ctx:
            sr.read_state("lab", "web", "host-1")
        self.assertIn("lease of host host-1", str(ctx.exception))

    def test_host_missing_from_the_host_list_is_unread(self):
        self.objects["/api/v1/nodes"]["items"].pop(0)
        with self.assertRaises(sr.Unread) as ctx:
            sr.read_state("lab", "web", "host-1")
        self.assertIn("not in the cluster's host list", str(ctx.exception))

    def test_statefulset_without_selector_is_unread(self):
        self.objects["/apis/apps/v1/namespaces/lab/statefulsets/web"]["spec"]["selector"] = {}
        with self.assertRaises(sr.Unread) as ctx:
            sr.read_state("lab", "web", "host-1")
        self.assertIn("no label selector", str(ctx.exception))

    def test_unreadable_statefulset_pods_hosts_and_volumes_refuse(self):
        for path, words in (("/apis/apps/v1/namespaces/lab/statefulsets/web", "StatefulSet lab/web"),
                            (PODS, "the pods of lab/web"), ("/api/v1/nodes", "the hosts"),
                            ("/apis/longhorn.io/v1beta2/volumes", "the Longhorn volumes")):
            with self.subTest(path=path):
                self.setUp()
                self.refuses(path, words)

    def pod_volumes(self, *volumes):
        self.objects[PODS]["items"][0]["spec"]["volumes"] += list(volumes)

    def test_volumes_with_nothing_to_protect_pass(self):
        self.pod_volumes({"name": "c", "configMap": {"name": "x"}}, {"name": "s", "secret": {"secretName": "x"}},
                         {"name": "t", "emptyDir": {}}, {"name": "p", "projected": {"sources": []}},
                         {"name": "w", "downwardAPI": {"items": []}})
        st = sr.read_state("lab", "web", "host-1")
        self.assertEqual([], st["pods"][0]["unchecked"])
        self.assertEqual(["data-web-0"], st["pods"][0]["claims"])

    def test_host_path_and_inline_csi_volumes_block(self):
        self.pod_volumes({"name": "logs", "hostPath": {"path": "/var/log"}}, {"name": "nfs", "csi": {"driver": "x"}})
        st = sr.read_state("lab", "web", "host-1")
        self.assertEqual([{"name": "logs", "type": "hostPath"}, {"name": "nfs", "type": "csi"}], st["pods"][0]["unchecked"])
        blockers = sr.plan(st, NOW)["blockers"]
        self.assertIn("web-0: volume logs is hostPath, which Homestead can't check", blockers)
        self.assertIn("web-0: volume nfs is csi, which Homestead can't check", blockers)

    def test_generic_ephemeral_volume_is_checked_as_its_claim(self):
        self.pod_volumes({"name": "scratch", "ephemeral": {"volumeClaimTemplate": {"spec": {}}}})
        self.objects["/api/v1/namespaces/lab/persistentvolumeclaims/web-0-scratch"] = {
            "spec": {"accessModes": ["ReadWriteOnce"], "volumeName": "pv-e"}}
        st = sr.read_state("lab", "web", "host-1")
        self.assertEqual(["data-web-0", "web-0-scratch"], st["pods"][0]["claims"])
        self.assertEqual({"access": ["ReadWriteOnce"], "volume": "pv-e", "longhorn": False}, st["claims"]["web-0-scratch"])
        self.assertTrue(any(b.startswith("web-0-scratch: it is not a bound Longhorn volume") for b in sr.plan(st, NOW)["blockers"]))

    def test_claim_on_a_local_volume_blocks_through_its_volume(self):
        # A `local` volume is a PersistentVolume, so it arrives as a claim whose
        # volume Longhorn doesn't know.
        self.objects["/api/v1/namespaces/lab/persistentvolumeclaims/data-web-0"]["spec"]["volumeName"] = "local-pv-1"
        st = sr.read_state("lab", "web", "host-1")
        self.assertFalse(st["claims"]["data-web-0"]["longhorn"])
        self.assertTrue(any(b.startswith("data-web-0: it is not a bound Longhorn volume") for b in sr.plan(st, NOW)["blockers"]))

    def test_longhorn_node_without_ready_condition_is_none(self):
        self.objects[f"{LH}/nodes"]["items"][0]["status"] = {"conditions": []}
        self.assertIsNone(sr.read_state("lab", "web", "host-1")["lh_nodes"]["host-1"])

    def test_wait_mode_from_tolerations(self):
        spec = self.objects["/apis/apps/v1/namespaces/lab/statefulsets/web"]["spec"]["template"]["spec"]
        spec["tolerations"] = [{"key": "node.kubernetes.io/unreachable", "operator": "Exists", "effect": "NoExecute"}]
        self.assertTrue(sr.read_state("lab", "web", "host-1")["set"]["wait_mode"])


NOW = 1792231200.0  # 2026-10-17T10:00:00Z
DOWN_SINCE = "2026-10-17T09:30:00Z"  # 1800 s before NOW


def good_state():
    return {"node": {"ready": False, "status": "Unknown", "since": DOWN_SINCE, "lease": "2026-10-17T09:29:00.000000Z"},
            "harvester": False, "set": {"ns": "lab", "name": "web", "wait_mode": False},
            "pods": [{"name": "web-0", "uid": "u0", "node": "host-1", "terminating": False, "claims": ["data-web-0"], "unchecked": []}],
            "claims": {"data-web-0": {"access": ["ReadWriteOnce"], "volume": "pv-a", "longhorn": True}},
            "lh_nodes": {"host-1": False, "host-2": True, "host-3": True},
            "replicas": {"pv-a": [{"node": "host-1", "failed": False, "healthy_at": True},
                                  {"node": "host-2", "failed": False, "healthy_at": True}]},
            "engines": {"pv-a": [{"node": "host-1", "state": "stopped"}]},
            "robustness": {"pv-a": "degraded"},
            "nodes_ready": {"host-1": False, "host-2": True, "host-3": True}}


class SetRestartPlanTests(unittest.TestCase):
    def refused(self, st, words, now=NOW):
        p = sr.plan(st, now)
        self.assertFalse(p["ready"])
        self.assertTrue(any(words in b for b in p["blockers"]), p["blockers"])
        return p

    def test_good_state_is_ready(self):
        p = sr.plan(good_state(), NOW)
        self.assertTrue(p["ready"], p["blockers"])
        self.assertEqual([], p["blockers"])
        self.assertEqual("host-1", p["node"])
        self.assertEqual(DOWN_SINCE, p["down_since"])
        self.assertEqual(1800, p["down_seconds"])
        self.assertEqual("2026-10-17T09:29:00.000000Z", p["last_heard"])

    def test_harvester_refuses(self):
        st = good_state(); st["harvester"] = True
        self.refused(st, "Harvester")

    def test_host_ready_refuses(self):
        st = good_state(); st["node"].update(ready=True, since=None)
        self.refused(st, "is Ready")

    def test_down_under_ten_minutes_refuses(self):
        self.refused(good_state(), "10 minutes", now=NOW - 1800 + 599)
        self.assertTrue(sr.plan(good_state(), NOW - 1800 + 600)["ready"])

    def test_unknown_down_time_refuses(self):
        st = good_state(); st["node"]["since"] = None
        self.refused(st, "went down")

    def test_no_pod_on_host_refuses(self):
        st = good_state(); st["pods"] = []
        self.refused(st, "no pod")

    def test_not_longhorn_refuses(self):
        st = good_state(); st["claims"]["data-web-0"]["longhorn"] = False
        p = self.refused(st, "data-web-0")
        self.assertTrue(any("Longhorn" in b for b in p["blockers"]))

    def test_unbound_claim_refuses(self):
        st = good_state(); st["claims"]["data-web-0"].update(volume=None, longhorn=False)
        self.refused(st, "data-web-0")

    def test_rwx_refuses(self):
        st = good_state(); st["claims"]["data-web-0"]["access"] = ["ReadWriteMany"]
        self.refused(st, "ReadWriteMany")

    def test_longhorn_still_sees_host_ready_refuses(self):
        st = good_state(); st["lh_nodes"]["host-1"] = True
        self.refused(st, "Longhorn")

    def test_host_missing_from_longhorn_refuses(self):
        st = good_state(); del st["lh_nodes"]["host-1"]
        self.refused(st, "Longhorn")

    def test_engine_running_on_host_refuses(self):
        st = good_state(); st["engines"]["pv-a"][0]["state"] = "running"
        self.refused(st, "engine")

    def test_only_copy_elsewhere_failed_refuses(self):
        st = good_state(); st["replicas"]["pv-a"][1].update(failed=True, healthy_at=False)
        p = self.refused(st, "pv-a")
        self.assertTrue(any("data-web-0" in b for b in p["blockers"]))

    def test_unsynced_copy_refuses(self):
        st = good_state(); st["replicas"]["pv-a"][1]["healthy_at"] = False
        self.refused(st, "data-web-0")

    def test_copy_on_another_down_host_refuses(self):
        st = good_state()
        st["replicas"]["pv-a"][1]["node"] = "host-3"
        st["nodes_ready"]["host-3"] = False
        self.refused(st, "host-3, which is not Ready either")

    def test_faulted_volume_refuses(self):
        st = good_state(); st["robustness"]["pv-a"] = "faulted"
        self.refused(st, "faulted")

    def test_volume_missing_from_robustness_refuses(self):
        st = good_state(); st["robustness"] = {}
        self.refused(st, "pv-a")

    def test_ready_plan_lists_every_pod_and_volume(self):
        st = good_state()
        st["pods"].append({"name": "web-1", "uid": "u1", "node": "host-1", "terminating": True, "claims": ["data-web-1"], "unchecked": []})
        st["claims"]["data-web-1"] = {"access": ["ReadWriteOnce"], "volume": "pv-b", "longhorn": True}
        st["replicas"]["pv-b"] = [{"node": "host-1", "failed": False, "healthy_at": True},
                                  {"node": "host-3", "failed": False, "healthy_at": True}]
        st["engines"]["pv-b"] = []
        st["robustness"]["pv-b"] = "degraded"
        p = sr.plan(st, NOW)
        self.assertTrue(p["ready"], p["blockers"])
        self.assertEqual([("web-0", "u0", False), ("web-1", "u1", True)],
                         [(x["name"], x["uid"], x["terminating"]) for x in p["pods"]])
        self.assertEqual([{"pvc": "data-web-0", "volume": "pv-a", "copy_on": "host-2"},
                          {"pvc": "data-web-1", "volume": "pv-b", "copy_on": "host-3"}], p["volumes"])

    def test_unscheduled_replica_is_skipped_not_a_copy(self):
        st = good_state()
        st["replicas"]["pv-a"] = [{"node": "host-1", "failed": False, "healthy_at": True}]
        p = self.refused(st, "no healthy copy")
        self.assertIsNone(p["volumes"][0]["copy_on"])

    def test_second_pods_bad_volume_blocks_whole_plan(self):
        st = good_state()
        st["pods"].append({"name": "web-1", "uid": "u1", "node": "host-1", "terminating": False, "claims": ["data-web-1"], "unchecked": []})
        st["claims"]["data-web-1"] = {"access": ["ReadWriteOnce"], "volume": "pv-b", "longhorn": False}
        self.refused(st, "data-web-1")

    def test_token_changes_with_pod_uid(self):
        st = good_state(); a = sr.plan(st, NOW)["review_token"]
        st["pods"][0]["uid"] = "u0-new"
        self.assertNotEqual(a, sr.plan(st, NOW)["review_token"])

    def test_token_changes_with_chosen_copy(self):
        st = good_state(); a = sr.plan(st, NOW)["review_token"]
        st["replicas"]["pv-a"][1]["node"] = "host-3"
        self.assertNotEqual(a, sr.plan(st, NOW)["review_token"])

    def test_token_stable_for_same_state(self):
        a = sr.plan(good_state(), NOW)["review_token"]
        self.assertEqual(a, sr.plan(good_state(), NOW + 60)["review_token"])
        self.assertEqual(20, len(a))

    def test_longhorn_node_none_or_missing_blocks_false_passes(self):
        for v in (None, True):
            st = good_state(); st["lh_nodes"]["host-1"] = v
            self.refused(st, "Longhorn")
        st = good_state(); del st["lh_nodes"]["host-1"]
        self.refused(st, "Longhorn")
        self.assertTrue(sr.plan(good_state(), NOW)["ready"])

    def test_engine_without_host_blocks_unless_stopped(self):
        st = good_state(); st["engines"]["pv-a"].append({"node": None, "state": "running"})
        self.refused(st, "data-web-0: Longhorn can't say where the engine of pv-a runs (state: running)")

    def test_engine_must_be_stopped(self):
        for state in ("running", "error", None, "starting"):
            st = good_state(); st["engines"]["pv-a"][0]["state"] = state
            self.refused(st, "stopped")
        # "unknown" passes only on a host that is down by every measure (SetRestartDownHostTests).
        st = good_state(); st["engines"]["pv-a"][0]["state"] = "unknown"
        self.assertTrue(sr.plan(st, NOW)["ready"])
        st = good_state(); st["engines"]["pv-a"].append({"node": "host-2", "state": "running"})
        self.assertTrue(sr.plan(st, NOW)["ready"])

    def test_robustness_allowlist(self):
        for ok in ("healthy", "degraded"):
            st = good_state(); st["robustness"]["pv-a"] = ok
            self.assertTrue(sr.plan(st, NOW)["ready"], ok)
        for bad in ("faulted", None, "other"):
            st = good_state(); st["robustness"]["pv-a"] = bad
            self.refused(st, "pv-a")
        # "unknown" passes only with the engine on a host that is down by every measure (SetRestartDownHostTests).
        st = good_state(); st["robustness"]["pv-a"] = "unknown"
        self.assertTrue(sr.plan(st, NOW)["ready"])

    def test_kubelet_still_answering_blocks(self):
        st = good_state(); st["node"]["status"] = "False"
        self.refused(st, "still answering")

    def test_fresh_lease_blocks(self):
        st = good_state(); st["node"]["lease"] = "2026-10-17T09:55:00.000000Z"
        self.refused(st, "heard from")

    def test_missing_lease_blocks(self):
        st = good_state(); st["node"]["lease"] = None
        self.refused(st, "last heard")

    def test_malformed_timestamps_block_not_raise(self):
        st = good_state(); st["node"]["since"] = "garbage"
        self.refused(st, "went down")
        st = good_state(); st["node"]["lease"] = "garbage"
        self.refused(st, "last heard")

    def test_set_without_claims_is_allowed(self):
        # Nothing on the host holds data Homestead must protect: no blocker for it.
        st = good_state(); st["pods"][0]["claims"] = []; st["claims"] = {}
        p = sr.plan(st, NOW)
        self.assertTrue(p["ready"], p["blockers"])
        self.assertEqual([], p["volumes"])

    def test_unchecked_volume_blocks(self):
        st = good_state(); st["pods"][0]["unchecked"] = [{"name": "logs", "type": "hostPath"}]
        self.refused(st, "web-0: volume logs is hostPath, which Homestead can't check")

    def test_wait_mode_carried(self):
        st = good_state(); st["set"]["wait_mode"] = True
        p = sr.plan(st, NOW)
        self.assertTrue(p["ready"])
        self.assertEqual({"ns": "lab", "name": "web", "wait_mode": True}, p["set"])


def down_host_state():
    """What Longhorn and Kubernetes reported for a genuinely down host (#441's
    real host-loss test): K8s Ready Unknown, Longhorn node Ready False, the
    volume's robustness and the engine on the down host both "unknown", the
    down host's replica "unknown" (still healthyAt, no failedAt) and the copy
    on the other host running with healthyAt."""
    st = good_state()
    st["engines"]["pv-a"] = [{"node": "host-1", "state": "unknown"}]
    st["robustness"]["pv-a"] = "unknown"
    return st


class SetRestartDownHostTests(unittest.TestCase):
    def blocked(self, st, words, now=NOW):
        p = sr.plan(st, now)
        self.assertFalse(p["ready"])
        self.assertTrue(any(words in b for b in p["blockers"]), p["blockers"])
        return p

    def test_recorded_down_host_states_are_ready(self):
        p = sr.plan(down_host_state(), NOW)
        self.assertTrue(p["ready"], p["blockers"])
        self.assertEqual([{"pvc": "data-web-0", "volume": "pv-a", "copy_on": "host-2"}], p["volumes"])
        relied = " ".join(p["relied_on"])
        self.assertIn("Longhorn reports the engine of pv-a on host-1 as unknown, as it does for a host that is down", relied)
        self.assertIn("Longhorn reports pv-a as unknown", relied)

    def test_normal_states_rely_on_nothing_extra(self):
        self.assertEqual([], sr.plan(good_state(), NOW)["relied_on"])

    def test_longhorn_node_true_blocks(self):
        st = down_host_state(); st["lh_nodes"]["host-1"] = True
        p = self.blocked(st, "state: unknown")
        self.blocked(st, "pv-a as unknown")
        self.assertEqual([], p["relied_on"])

    def test_longhorn_node_none_blocks(self):
        st = down_host_state(); del st["lh_nodes"]["host-1"]
        self.blocked(st, "state: unknown")
        self.blocked(st, "pv-a as unknown")

    def test_kubernetes_ready_false_blocks(self):
        st = down_host_state(); st["node"]["status"] = "False"
        self.blocked(st, "state: unknown")
        self.blocked(st, "pv-a as unknown")

    def test_fresh_lease_blocks(self):
        st = down_host_state(); st["node"]["lease"] = "2026-10-17T09:55:00Z"
        self.blocked(st, "state: unknown")
        self.blocked(st, "pv-a as unknown")

    def test_missing_lease_blocks(self):
        st = down_host_state(); st["node"]["lease"] = None
        self.blocked(st, "state: unknown")
        self.blocked(st, "pv-a as unknown")

    def test_unknown_down_time_blocks(self):
        st = down_host_state(); st["node"]["since"] = None
        p = self.blocked(st, "state: unknown")
        self.blocked(st, "pv-a as unknown")
        self.assertEqual([], p["relied_on"])

    def test_token_covers_what_was_relied_on(self):
        stopped = down_host_state(); stopped["engines"]["pv-a"][0]["state"] = "stopped"
        a, b = sr.plan(stopped, NOW), sr.plan(down_host_state(), NOW)
        self.assertTrue(a["ready"] and b["ready"])
        self.assertNotEqual(a["relied_on"], b["relied_on"])
        self.assertNotEqual(a["review_token"], b["review_token"])
        self.assertEqual(b["review_token"], sr.plan(down_host_state(), NOW + 60)["review_token"])

    def test_relied_on_lines_are_not_repeated(self):
        st = down_host_state(); st["engines"]["pv-a"].append({"node": "host-1", "state": "unknown"})
        p = sr.plan(st, NOW)
        self.assertTrue(p["ready"], p["blockers"])
        self.assertEqual(2, len(p["relied_on"]), p["relied_on"])
        self.assertEqual(len(p["relied_on"]), len(set(p["relied_on"])))

    def test_relaxation_is_per_volume(self):
        st = down_host_state()
        st["pods"][0]["claims"].append("data-web-0b")
        st["claims"]["data-web-0b"] = {"access": ["ReadWriteOnce"], "volume": "pv-b", "longhorn": True}
        # pv-b's only other copy is on a host that is not Ready: its "unknown"s are not accepted.
        st["replicas"]["pv-b"] = [{"node": "host-1", "failed": False, "healthy_at": True},
                                  {"node": "host-3", "failed": False, "healthy_at": True}]
        st["nodes_ready"]["host-3"] = False
        st["engines"]["pv-b"] = [{"node": "host-1", "state": "unknown"}]
        st["robustness"]["pv-b"] = "unknown"
        p = self.blocked(st, "engine has stopped. It is pv-b on host-1 (state: unknown)")
        self.blocked(st, "pv-b as unknown")
        self.assertFalse(any("pv-a" in b for b in p["blockers"]), p["blockers"])
        relied = " ".join(p["relied_on"])
        self.assertIn("engine of pv-a on host-1 as unknown", relied)
        self.assertNotIn("pv-b", relied)

    def test_down_under_ten_minutes_blocks(self):
        st = down_host_state(); st["node"]["since"] = "2026-10-17T09:50:01Z"
        self.blocked(st, "state: unknown")
        self.blocked(st, "pv-a as unknown")

    def test_no_healthy_copy_elsewhere_blocks(self):
        st = down_host_state(); st["replicas"]["pv-a"][1].update(failed=True)
        self.blocked(st, "state: unknown")
        self.blocked(st, "pv-a as unknown")

    def test_copy_elsewhere_on_a_host_not_ready_blocks(self):
        st = down_host_state(); st["nodes_ready"]["host-2"] = False
        self.blocked(st, "state: unknown")
        self.blocked(st, "pv-a as unknown")

    def test_engine_running_on_down_host_still_blocks(self):
        st = down_host_state(); st["engines"]["pv-a"][0]["state"] = "running"
        self.blocked(st, "state: running")

    def test_other_engine_states_on_down_host_block(self):
        for state in ("error", "starting", None, "stopping"):
            st = down_host_state(); st["engines"]["pv-a"][0]["state"] = state
            self.blocked(st, f"state: {state}")

    def test_unknown_robustness_with_engine_on_another_host_blocks(self):
        st = down_host_state(); st["engines"]["pv-a"] = [{"node": "host-2", "state": "unknown"}]
        self.blocked(st, "pv-a as unknown")

    def test_unknown_robustness_with_no_engine_blocks(self):
        st = down_host_state(); st["engines"]["pv-a"] = []
        self.blocked(st, "pv-a as unknown")

    def test_unknown_robustness_with_an_engine_also_elsewhere_blocks(self):
        st = down_host_state(); st["engines"]["pv-a"].append({"node": "host-2", "state": "running"})
        self.blocked(st, "pv-a as unknown")

    def test_faulted_robustness_blocks(self):
        st = down_host_state(); st["robustness"]["pv-a"] = "faulted"
        self.blocked(st, "pv-a as faulted")

    def test_missing_robustness_blocks(self):
        st = down_host_state(); st["robustness"] = {}
        self.blocked(st, "pv-a as None")

    def test_stopped_engine_with_unknown_robustness_on_down_host_is_ready(self):
        st = down_host_state(); st["engines"]["pv-a"][0]["state"] = "stopped"
        self.assertTrue(sr.plan(st, NOW)["ready"])


def req(method="POST", user="alice", **body):
    q = {k: [v] for k, v in body.items()}
    return ROUTER.Request(method, "/x", q if method == "GET" else {}, body if method != "GET" else {}, user, "admin", "")


class SetRestartRouteTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.objects, self.sent, self.err, self.errs, self.get_err = {}, [], None, {}, None
        keep_bound(self, OPS, sr)
        OPS.bind(lambda path: self.objects[path], self.tmp.name, lambda ns, name: None)
        self.state = good_state()
        self.now = NOW
        self.enterContext(mock.patch.object(sr, "read_state", lambda ns, name, node: copy.deepcopy(self.state)))
        self.enterContext(mock.patch.object(sr, "clock", lambda: self.now))
        sr.bind(self.get, self.send)
        self.token = sr.plan(good_state(), NOW)["review_token"]

    def get(self, path):
        if self.get_err:
            raise urllib.error.HTTPError(path, self.get_err, "unavailable", {}, None)
        if path not in self.objects:
            raise urllib.error.HTTPError(path, 404, "missing", {}, None)
        return self.objects[path]

    def send(self, method, path, body=None, *a, **k):
        self.sent.append((method, path, body))
        code = self.errs.get(path.rsplit("/", 1)[-1], self.err)
        if code:
            raise urllib.error.HTTPError(path, code, "boom", {}, None)
        return {}

    def run_req(self, **over):
        b = {"ns": "lab", "name": "web", "node": "host-1", "confirm": "web", "review_token": self.token}
        b.update(over)
        return sr._run(req(user=b.pop("user", "alice"), **b))

    def test_run_needs_typed_name(self):
        with self.assertRaises(ValueError):
            self.run_req(confirm="wrong")
        self.assertEqual([], self.sent)

    def test_plan_route_reports_unread_as_blocker(self):
        with mock.patch.object(sr, "read_state", side_effect=sr.Unread("no lease")):
            p = sr._plan(req("GET", ns="lab", name="web", node="host-1"))
        self.assertFalse(p["ready"])
        self.assertEqual(["no lease"], p["blockers"])
        self.assertTrue(sr._plan(req("GET", ns="lab", name="web", node="host-1"))["ready"])

    def test_run_refuses_while_one_runs_for_same_set(self):
        first = self.run_req()
        self.assertEqual(202, first.code)
        again = self.run_req()
        self.assertEqual(409, again.code)
        self.assertEqual({"id": first.body["id"]}, again.body["operation"])

    def test_run_replans_and_refuses_blockers(self):
        self.state["node"]["ready"] = True
        r = self.run_req()
        self.assertEqual(409, r.code)
        self.assertIn("plan", r.body)
        self.assertEqual([], self.sent)

    def test_run_refuses_unread(self):
        with mock.patch.object(sr, "read_state", side_effect=sr.Unread("no lease")):
            r = self.run_req()
        self.assertEqual(409, r.code)
        self.assertEqual(["no lease"], r.body["plan"]["blockers"])

    def test_run_refuses_changed_review(self):
        r = self.run_req(review_token="stale")
        self.assertEqual(409, r.code)
        self.assertIn("review the plan again", r.body["error"])
        self.assertIn("plan", r.body)
        self.assertEqual([], self.sent)

    def test_run_deletes_each_reviewed_pod_with_uid_precondition(self):
        self.state["pods"].append({"name": "web-1", "uid": "u1", "node": "host-1", "terminating": False, "claims": [], "unchecked": []})
        self.token = sr.plan(self.state, NOW)["review_token"]
        r = self.run_req()
        self.assertEqual(202, r.code)
        body = lambda uid: {"apiVersion": "v1", "kind": "DeleteOptions", "gracePeriodSeconds": 0, "preconditions": {"uid": uid}}
        self.assertEqual([("DELETE", "/api/v1/namespaces/lab/pods/web-0", body("u0")),
                          ("DELETE", "/api/v1/namespaces/lab/pods/web-1", body("u1"))], self.sent)

    def test_run_records_who(self):
        for user in ("alice", "api-key:ops"):
            self.tmp.cleanup(); self.tmp = tempfile.TemporaryDirectory()
            OPS.bind(lambda path: self.objects[path], self.tmp.name, lambda ns, name: None)
            r = self.run_req(user=user)
            self.assertEqual(user, r.body["set_restart"]["started_by"])
            self.assertEqual({"node": "host-1", "set": "web", "started_by": user, "stage": "deleted"}, r.body["set_restart"])
            self.assertNotIn("ref", r.body)

    def test_host_back_at_send_time_refuses(self):
        self.state["node"].update(ready=True, status="True", since=None)
        r = self.run_req()
        self.assertEqual(409, r.code)
        self.assertEqual([], self.sent)

    def test_pod_already_replaced(self):
        for code in (404, 409):
            self.sent.clear(); self.err = code
            self.tmp.cleanup(); self.tmp = tempfile.TemporaryDirectory()
            OPS.bind(lambda path: self.objects[path], self.tmp.name, lambda ns, name: None)
            r = self.run_req()
            self.assertEqual(202, r.code)
            self.assertEqual(1, len(self.sent), "no retry")
            self.assertEqual("already replaced", r.body["set_restart"]["stage"])
            item = OPS.stored()[0]
            self.assertEqual("web-0 was already replaced; nothing was deleted; waiting for it to be Ready on another host",
                             item["message"])

    def two_pods(self):
        self.state["pods"].append({"name": "web-1", "uid": "u1", "node": "host-1", "terminating": False, "claims": [], "unchecked": []})
        self.token = sr.plan(self.state, NOW)["review_token"]

    def test_all_deletes_failed_starts_no_job(self):
        self.two_pods(); self.err = 500
        r = self.run_req()
        self.assertEqual(502, r.code)
        self.assertIn("could not delete any of the pods", r.body["error"])
        self.assertEqual(["web-0", "web-1"], [x["name"] for x in r.body["not_deleted"]])
        self.assertIn("boom", r.body["not_deleted"][0]["error"])
        self.assertEqual([], OPS.stored())

    def test_mixed_deleted_and_failed_follows_only_the_deleted(self):
        self.two_pods(); self.errs = {"web-1": 500}
        r = self.run_req()
        self.assertEqual(202, r.code)
        self.assertEqual(["web-1"], [x["name"] for x in r.body["not_deleted"]])
        item = OPS.stored()[0]
        self.assertEqual(["web-0"], [p["name"] for p in item["ref"]["pods"]])
        self.assertEqual("Deleted web-0; could not delete web-1: HTTP Error 500: boom; nothing was changed for it; "
                         "waiting for a replacement on another host", item["message"])
        self.now = NOW + 60
        self.objects["/api/v1/namespaces/lab/pods/web-0"] = self.new_pod("host-2", True)
        self.assertEqual("succeeded", sr.status(item)[0])

    def test_replaced_and_deleted_are_both_followed(self):
        self.two_pods(); self.errs = {"web-0": 404}
        r = self.run_req()
        self.assertEqual(202, r.code)
        item = OPS.stored()[0]
        self.assertEqual(["web-0", "web-1"], [p["name"] for p in item["ref"]["pods"]])
        self.assertEqual("Deleted web-1; web-0 was already replaced; waiting for a replacement on another host", item["message"])
        self.assertEqual("deleted", r.body["set_restart"]["stage"])

    def test_start_refused_after_a_delete_says_pods_were_deleted(self):
        self.two_pods(); self.errs = {"web-1": 500}
        with mock.patch.object(OPS, "start", side_effect=ValueError("already active")), \
                mock.patch.object(sr, "_active", side_effect=["", "op-other"]):
            r = self.run_req()
        self.assertEqual(409, r.code)
        self.assertIn("Pods were deleted", r.body["error"])
        self.assertEqual(["web-0"], [x["name"] for x in r.body["deleted"]])
        self.assertEqual({"id": "op-other"}, r.body["operation"])
        self.assertEqual(["web-1"], [x["name"] for x in r.body["not_deleted"]])

    def item(self):
        r = self.run_req()
        return next(i for i in OPS.stored() if i["id"] == r.body["id"])

    def new_pod(self, node, ready, uid="u9"):
        conds = [{"type": "Ready", "status": "True" if ready else "False"}]
        return {"metadata": {"name": "web-0", "uid": uid}, "spec": {"nodeName": node}, "status": {"conditions": conds}}

    def test_status_follows_stages(self):
        item = self.item()
        path = "/api/v1/namespaces/lab/pods/web-0"
        self.now = NOW + 30
        self.assertEqual("running", sr.status(item)[0])  # no replacement yet: the read is a 404
        self.objects[path] = self.new_pod("", False)
        self.assertEqual("running", sr.status(item)[0])
        self.assertEqual("deleted", item["ref"]["stage"])
        self.objects[path] = self.new_pod("host-2", False)
        self.assertEqual("running", sr.status(item)[0])
        self.assertEqual("scheduled on host-2", item["ref"]["stage"])
        self.objects[path]["status"]["containerStatuses"] = [{"state": {"waiting": {"reason": "ContainerCreating"}}}]
        sr.status(item)
        self.assertEqual("attaching volume", item["ref"]["stage"])

    def test_status_ready_elsewhere_succeeds(self):
        item = self.item()
        self.objects["/api/v1/namespaces/lab/pods/web-0"] = self.new_pod("host-2", True)
        self.assertEqual("succeeded", sr.status(item)[0])
        self.assertEqual("ready", item["ref"]["stage"])

    def test_status_old_pod_never_succeeds(self):
        item = self.item()
        path = "/api/v1/namespaces/lab/pods/web-0"
        for node in ("host-2", "host-1"):
            self.objects[path] = self.new_pod(node, True, uid="u0")  # the pod that was deleted
            self.assertEqual("running", sr.status(item)[0])

    def test_status_new_pod_ready_on_the_host_that_came_back_succeeds(self):
        item = self.item()
        self.objects["/api/v1/namespaces/lab/pods/web-0"] = self.new_pod("host-1", True)
        status, progress, message = sr.status(item)
        self.assertEqual(("succeeded", 100), (status, progress))
        self.assertEqual("web-0 is Ready on host-1, which came back", message)

    def test_status_new_pod_not_ready_on_the_host_that_came_back_is_scheduled(self):
        item = self.item()
        self.objects["/api/v1/namespaces/lab/pods/web-0"] = self.new_pod("host-1", False)
        self.assertEqual("running", sr.status(item)[0])
        self.assertEqual("scheduled on host-1", item["ref"]["stage"])

    def test_status_mixed_hosts_names_each(self):
        self.two_pods()
        item = self.item()
        self.objects["/api/v1/namespaces/lab/pods/web-0"] = self.new_pod("host-1", True)
        self.objects["/api/v1/namespaces/lab/pods/web-1"] = dict(self.new_pod("host-2", True), metadata={"name": "web-1", "uid": "u8"})
        status, _, message = sr.status(item)
        self.assertEqual("succeeded", status)
        self.assertEqual("Ready on another host: web-1; web-0 is Ready on host-1, which came back", message)

    def test_status_gives_the_attach_note_while_deleted_or_attaching(self):
        item = self.item()
        note = "Kubernetes can take about 6 minutes to release the volume from host-1"
        _, _, message = sr.status(item)
        self.assertEqual(f"web-0: deleted; {note}", message)
        pod = self.new_pod("host-2", False)
        self.objects["/api/v1/namespaces/lab/pods/web-0"] = pod
        self.assertNotIn("6 minutes", sr.status(item)[2])
        pod["status"]["containerStatuses"] = [{"state": {"waiting": {"reason": "ContainerCreating"}}}]
        self.assertEqual(f"web-0: attaching volume; {note}", sr.status(item)[2])

    def test_status_read_error_keeps_running_until_limit(self):
        item = self.item()
        self.get_err = 503  # not a 404: the pod could not be read at all
        self.now = NOW + 60
        status, _, message = sr.status(item)
        self.assertEqual("running", status)
        self.assertIn("Waiting to read the pods again", message)
        self.now = NOW + 901
        self.assertEqual("failed", sr.status(item)[0])

    def test_status_times_out_after_900s(self):
        item = self.item()
        self.objects["/api/v1/namespaces/lab/pods/web-0"] = self.new_pod("host-2", False)
        self.now = NOW + 899
        self.assertEqual("running", sr.status(item)[0])
        self.now = NOW + 901
        status, _, message = sr.status(item)
        self.assertEqual("failed", status)
        self.assertIn("within 15 minutes", message)
        self.assertIn("Ready on another host", message)
        self.assertIn("scheduled on host-2", message)

    def test_routes_roles(self):
        for key, role in ((("GET", "/api/statefulset/restart-elsewhere/plan"), "viewer"),
                          (("POST", "/api/statefulset/restart-elsewhere"), "admin")):
            self.assertEqual(role, sr.ROUTES[key][0])
            self.assertEqual(role, POLICY.role(key[1], key[0]))
        self.assertIn("homestead_set_restart", ROUTER.MODULES)
        self.assertIs(sr.status, OPS.RESOLVERS[sr.KIND])


if __name__ == "__main__":
    unittest.main()
