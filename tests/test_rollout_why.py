import sys
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "server"))

import homestead_kube as KUBE
import homestead_node_probes as NODE_PROBES
import homestead_operations as OPS
import homestead_routes
import homestead_updates as UPDATES
import homestead_update_rollouts as ROLLOUTS
sys.path.insert(0, str(ROOT / "tests"))
from fakes.kube import Kube
from rebind import keep_bound


def pod(age):
    stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - age))
    return {"metadata": {"name": "homestead-7cd4b47d4-hwlnr", "creationTimestamp": stamp}}


EVENTS = {"items": [
    {"reason": "Scheduled", "type": "Warning", "message": "old", "lastTimestamp": "2026-09-24T10:00:00Z"},
    {"reason": "FailedAttachVolume", "type": "Warning", "lastTimestamp": "2026-09-24T10:05:00Z",
     "message": "AttachVolume.Attach failed for volume \"pvc-1\" : rpc error: invalid controller count 2"}]}


class RolloutWhyTests(unittest.TestCase):
    def test_init_image_pull_failure_is_reported_even_when_app_is_initializing(self):
        waiting = pod(10)
        waiting["metadata"]["namespace"] = "lab"
        waiting["metadata"]["labels"] = {"app": "homestead"}
        waiting["status"] = {"initContainerStatuses": [{"name": "data-permissions", "state": {"waiting": {
            "reason": "ImagePullBackOff", "message": "failed to pull: dial tcp: lookup ghcr.io: Try again"}}}],
            "containerStatuses": [{"name": "homestead", "state": {"waiting": {"reason": "PodInitializing"}}}]}
        dep = {"metadata": {"name": "homestead", "namespace": "lab", "generation": 2}, "spec": {
            "replicas": 1, "selector": {"matchLabels": {"app": "homestead"}}}, "status": {
                "replicas": 2, "updatedReplicas": 1, "availableReplicas": 1, "readyReplicas": 1}}
        with mock.patch.object(UPDATES, "kget", return_value={"items": [waiting]}), \
                mock.patch.object(ROLLOUTS, "pull_state", return_value={}):
            result = ROLLOUTS.progress("lab", "homestead", dep)
        self.assertEqual("failed", result["phase"])
        self.assertTrue(result["pods"][0]["waiting"][0]["init"])
        self.assertIn("init data-permissions", result["problems"][0])
        self.assertIn("DNS", result["problems"][0])

    def test_a_young_pod_is_given_time(self):
        with mock.patch.object(UPDATES, "kget", lambda path: EVENTS):
            self.assertIsNone(ROLLOUTS._why_waiting("lab", pod(20)))

    def test_a_pod_waiting_on_its_volume_says_so_and_is_stuck_after_three_minutes(self):
        with mock.patch.object(UPDATES, "kget", lambda path: EVENTS):
            waiting = ROLLOUTS._why_waiting("lab", pod(90))
            self.assertIn("invalid controller count 2", waiting["message"])
            self.assertFalse(waiting["stuck"])
            self.assertIn("Scale the workload to 0", waiting["hint"])
            self.assertTrue(ROLLOUTS._why_waiting("lab", pod(400))["stuck"])

    def test_no_warnings_no_reason(self):
        with mock.patch.object(UPDATES, "kget", lambda path: {"items": []}):
            self.assertIsNone(ROLLOUTS._why_waiting("lab", pod(400)))


# A host lost while a Longhorn engine ran on it keeps its iSCSI session to the
# old instance-manager's address (192.0.2.40) once it is back; the one there now
# is 192.0.2.41. Kubernetes' attach failure names the volume (the PV) in the form
# the attach/detach controller writes on the pod: 'AttachVolume.Attach failed for
# volume "<pv>" : <the driver's error>'.
STALE = "192.0.2.40"
ATTACH_TIMEOUT = ('AttachVolume.Attach failed for volume "pvc-7d1e" : timed out waiting for external-attacher '
                  'of driver.longhorn.io CSI driver to attach volume pvc-7d1e')


def stale_cluster(age=300, node="node-2", reason="FailedAttachVolume", message=ATTACH_TIMEOUT,
                  sessions=(("node-2", "pvc-7d1e"),), handle="pvc-7d1e", im_pods=True):
    """The kube fake with a Longhorn volume, its claim, the instance-manager
    of each host, the app's waiting pod and its newest warning; and the node
    probes' iSCSI reading."""
    stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - age))
    pod = {"apiVersion": "v1", "kind": "Pod",
           "metadata": {"name": "notes-6c9f7-x2k7p", "namespace": "lab", "labels": {"app": "notes"}, "creationTimestamp": stamp},
           "spec": {"nodeName": node, "containers": [{"name": "notes", "image": "registry.example.com/notes:1"}],
                    "volumes": [{"name": "data", "persistentVolumeClaim": {"claimName": "notes-data"}}]},
           "status": {"phase": "Pending", "conditions": [{"type": "PodScheduled", "status": "True"},
                                                         {"type": "Ready", "status": "False"}],
                      "containerStatuses": [{"name": "notes", "ready": False, "image": "registry.example.com/notes:1",
                                             "state": {"waiting": {"reason": "ContainerCreating"}}}]}}
    pv = {"apiVersion": "v1", "kind": "PersistentVolume", "metadata": {"name": "pvc-7d1e"},
          "spec": {"csi": {"driver": "driver.longhorn.io", "volumeHandle": handle},
                   "claimRef": {"namespace": "lab", "name": "notes-data"}}, "status": {"phase": "Bound"}}
    event = {"apiVersion": "v1", "kind": "Event", "metadata": {"name": "notes.1", "namespace": "lab"},
             "involvedObject": {"kind": "Pod", "name": pod["metadata"]["name"], "namespace": "lab"},
             "type": "Warning", "reason": reason, "message": message, "lastTimestamp": stamp}
    ims = [{"apiVersion": "v1", "kind": "Pod",
            "metadata": {"name": f"instance-manager-{n}", "namespace": "longhorn-system",
                         "labels": {"longhorn.io/component": "instance-manager"}},
            "spec": {"nodeName": n}, "status": {"phase": "Running", "podIP": ip, "podIPs": [{"ip": ip}]}}
           for n, ip in (("node-1", "192.0.2.31"), ("node-2", "192.0.2.41"))] if im_pods else []
    probes = {}
    for host, volume in sessions:
        probes.setdefault(host, {"iscsi": []})["iscsi"].append(
            {"sid": 67, "target": "iqn.2019-10.io.longhorn:" + volume, "state": "LOGGED_IN",
             "address": STALE, "port": 3260, "disks": ["sdj"]})
    return Kube(pod, pv, event, *ims), pod, probes


class StaleSessionTests(unittest.TestCase):
    """An app whose volume cannot attach because its host keeps a stale iSCSI
    session says so, cause first, wherever Homestead says why a pod waits."""

    def setUp(self):
        keep_bound(self, UPDATES)
        UPDATES.kget = KUBE.kget

    def why(self, **cluster):
        kube, pod, probes = stale_cluster(**cluster)
        with kube, mock.patch.object(NODE_PROBES, "node_temps", lambda *a, **k: probes), \
                mock.patch.object(homestead_routes, "cached", lambda key, seconds, fn: fn()):
            return ROLLOUTS._why_waiting("lab", kube.object("/api/v1/namespaces/lab/pods/" + pod["metadata"]["name"]))

    def test_a_stale_session_on_the_pod_s_host_to_its_volume_is_named_cause_first(self):
        found = self.why()
        self.assertEqual("Volume notes-data cannot attach, because node-2 still holds an old connection to it from "
                         "before an outage. End that connection on node-2 (Cluster Health lists the steps), or "
                         "restart node-2.", found["message"])
        self.assertTrue(found["stale_session"])
        self.assertTrue(found["stuck"], "stuck after three minutes, as any volume that cannot attach")
        self.assertNotIn("hint", found, "a hint fails a rollout at once; this waits its three minutes")
        self.assertFalse(self.why(age=90)["stuck"])

    def test_the_volume_is_matched_by_its_pv_name_not_by_longhorn_s_volume_name(self):
        # A volume made by hand keeps its own Longhorn name under another PV name.
        found = self.why(handle="notes-by-hand", sessions=(("node-2", "notes-by-hand"),))
        self.assertTrue(found.get("stale_session"))
        self.assertIn("Volume notes-data cannot attach", found["message"])

    def test_without_the_session_the_kubernetes_text_stays(self):
        for case, cluster in {
                "a session on another host": {"sessions": (("node-1", "pvc-7d1e"),)},
                "a session to another volume": {"sessions": (("node-2", "pvc-other"),)},
                "no probe reading": {"sessions": ()},
                "no instance-manager running": {"im_pods": False},
                "a mount failure, not an attach failure": {
                    "reason": "FailedMount",
                    "message": 'MountVolume.MountDevice failed for volume "pvc-7d1e" : rpc error: code = Internal'}}.items():
            with self.subTest(case):
                found = self.why(**cluster)
                self.assertFalse(found.get("stale_session"))
                self.assertEqual(cluster.get("message", ATTACH_TIMEOUT), found["message"])

    def test_the_rollout_and_its_job_say_it_without_failing_early(self):
        kube, pod, probes = stale_cluster(age=90)
        dep = {"metadata": {"name": "notes", "namespace": "lab", "generation": 2},
               "spec": {"replicas": 1, "selector": {"matchLabels": {"app": "notes"}}},
               "status": {"replicas": 1, "observedGeneration": 2}}
        with kube, mock.patch.object(NODE_PROBES, "node_temps", lambda *a, **k: probes), \
                mock.patch.object(homestead_routes, "cached", lambda key, seconds, fn: fn()), \
                mock.patch.object(ROLLOUTS, "pull_state", return_value={}):
            state = ROLLOUTS.progress("lab", "notes", dep)
        self.assertEqual("progressing", state["phase"])
        self.assertIn("node-2 still holds an old connection", state["pods"][0]["blocked"])
        self.addCleanup(setattr, OPS, "deployment_progress", OPS.deployment_progress)
        OPS.deployment_progress = lambda ns, name: state
        _, _, message = OPS._deployment({"ref": {"namespace": "lab", "name": "notes"}})
        self.assertTrue(message.startswith("New pod waiting on node-2: Volume notes-data cannot attach, because"), message)

    def app_page(self, **cluster):
        import server  # binds the modules the way the app does
        kube, pod, probes = stale_cluster(**cluster)
        kube.add({"apiVersion": "apps/v1", "kind": "Deployment",
                  "metadata": {"name": "notes", "namespace": "lab", "generation": 1},
                  "spec": {"replicas": 1, "selector": {"matchLabels": {"app": "notes"}},
                           "template": {"metadata": {"labels": {"app": "notes"}}, "spec": pod["spec"]}},
                  "status": {"replicas": 1, "observedGeneration": 1}})
        with kube, mock.patch.object(NODE_PROBES, "node_temps", lambda *a, **k: probes), \
                mock.patch.object(homestead_routes, "cached", lambda key, seconds, fn: fn()), \
                mock.patch.object(ROLLOUTS, "pull_state", return_value={}):
            app = next(w for w in server.WORKLOADS.get_workloads() if w["name"] == "notes")
        return app["pods"][0], kube

    def test_the_app_s_own_page_says_it_too(self):
        row, _ = self.app_page()
        self.assertEqual(self.why()["message"], row["stale_session"], "one reason, worked out in one place")

    def test_the_app_s_page_says_nothing_new_without_the_session_and_reads_no_events_for_it(self):
        row, kube = self.app_page(sessions=(("node-1", "pvc-7d1e"),))
        self.assertEqual("", row["stale_session"])
        self.assertFalse([path for path, _ in kube.reads if "/events" in path],
                         "no stale session on the pod's host: its events are not read for one")


if __name__ == "__main__":
    unittest.main()
