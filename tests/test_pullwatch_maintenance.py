import copy
import sys
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
import homestead_lifecycle as lifecycle
import homestead_maintenance as maintenance
import homestead_names as names
import homestead_pullwatch as pull
import homestead_runtime as runtime
import homestead_imports as imports
from test_cluster_shutdown import Fake
import test_node_power_plan as power_fixtures


def watcher(node="node1", distribution="k3s"):
    with mock.patch.object(runtime.PLATFORM, "detect", return_value={"distribution": distribution}):
        pod = runtime.pod(pull._pod_name(node, "image:1"), "lab", node,
                          pull.script(["sha256:" + "a" * 64]), pull.TASK,
                          {names.key("image"): "image:1"}, deadline=pull.LIMIT + 60)
    pod["metadata"]["uid"] = "watch-uid"
    pod["status"] = {"phase": "Running"}
    return pod


class PullWatchDrainTests(unittest.TestCase):
    def setUp(self):
        self.patch = mock.patch.object(pull, "NS", "lab")
        self.patch.start()
        self.addCleanup(self.patch.stop)

    def test_old_and_new_helpers_are_safe_but_still_reviewed_and_evicted(self):
        for distribution in ("k3s", "rke2"):
            pod = watcher(distribution=distribution)
            # API defaulting and legacy service-account admission are supported.
            pod["spec"].update(dnsPolicy="ClusterFirst", schedulerName="default-scheduler", securityContext={})
            pod["spec"]["volumes"].append({"name": "kube-api-access-x", "projected": {"sources": []}})
            pod["spec"]["containers"][0]["volumeMounts"].append({"name": "kube-api-access-x",
                "readOnly": True, "mountPath": "/var/run/secrets/kubernetes.io/serviceaccount"})
            inventory = maintenance.inventory(lambda _: {"items": []}, [pod])
            self.assertEqual([], inventory["blockers"])
            self.assertEqual([], inventory["local_storage"])
            self.assertIn("only its report is lost", " ".join(inventory["waiting"]))
            snapshot = maintenance.pod_snapshot([pod])
            self.assertEqual("watch-uid", snapshot[0][2])
            live = [pod]
            def send(method, path, body):
                self.assertEqual("POST", method)
                self.assertTrue(path.endswith("/eviction"))
                self.assertEqual({"uid": "watch-uid"}, body["deleteOptions"]["preconditions"])
                live.clear()
            with mock.patch.object(lifecycle, "kget", side_effect=lambda _: {"items": copy.deepcopy(live)}), \
                    mock.patch.object(lifecycle, "ksend", side_effect=send) as evict:
                result = lifecycle.drain("node1", include_system=True, reviewed_pods=snapshot, wait=True)
            evict.assert_called_once()
            self.assertEqual(["lab/" + pod["metadata"]["name"]], result["evicted"])

    def test_name_or_label_cannot_exempt_a_workload_or_writable_data(self):
        mutations = [
            lambda p: p["metadata"].update(namespace="other"),
            lambda p: p["metadata"].pop("uid"),
            lambda p: p["metadata"]["labels"].update(app="other"),
            lambda p: p["metadata"]["labels"].pop(names.key("task")),
            lambda p: p["spec"].pop("activeDeadlineSeconds"),
            lambda p: p["spec"].update(restartPolicy="Always"),
            lambda p: p["spec"]["containers"].append({"name": "app", "image": "app"}),
            lambda p: p["spec"].update(initContainers=[{"name": "app", "image": "app"}]),
            lambda p: p["spec"].update(ephemeralContainers=[{"name": "app", "image": "app"}]),
            lambda p: p["spec"]["containers"][0].update(command=["sh", "-c", "sleep 1800"]),
            lambda p: p["spec"]["containers"][0]["command"].__setitem__(2, p["spec"]["containers"][0]["command"][2] + "\nrm -rf /host"),
            lambda p: p["spec"]["containers"][0].update(env=[{"name": "ENV", "value": "other"}]),
            lambda p: p["spec"]["containers"][0]["volumeMounts"][0].update(readOnly=False),
            lambda p: p["spec"]["containers"][0]["securityContext"].update(privileged=True),
            lambda p: p["spec"]["volumes"][0]["hostPath"].update(path="/data"),
            lambda p: p["spec"]["volumes"].append({"name": "data", "persistentVolumeClaim": {"claimName": "data"}}),
        ]
        for mutate in mutations:
            pod = watcher()
            mutate(pod)
            with self.subTest(mutation=mutate):
                self.assertFalse(pull.disposable(pod))
                self.assertIn("unmanaged pod", " ".join(maintenance.inventory(lambda _: {"items": []}, [pod])["blockers"]))

    def test_watcher_still_obeys_disruption_budgets(self):
        pod = watcher()
        budget = {"metadata": {"namespace": "lab", "name": "all", "generation": 1},
                  "spec": {"selector": {}}, "status": {"observedGeneration": 1, "disruptionsAllowed": 0}}
        self.assertIn("permits no verified eviction", " ".join(
            maintenance.inventory(lambda _: {"items": [budget]}, [pod])["blockers"]))

    def test_replacement_watcher_invalidates_review_before_eviction(self):
        pod = watcher()
        snapshot = maintenance.pod_snapshot([pod])
        pod["metadata"]["uid"] = "replacement"
        with mock.patch.object(lifecycle, "kget", return_value={"items": [pod]}), \
                mock.patch.object(lifecycle, "ksend") as send:
            with self.assertRaisesRegex(ValueError, "changed after review"):
                lifecycle.drain("node1", include_system=True, reviewed_pods=snapshot, wait=True)
        send.assert_not_called()


class AllMaintenanceHelpersTests(unittest.TestCase):
    def setUp(self):
        patch = mock.patch.object(pull, "NS", "lab")
        patch.start()
        self.addCleanup(patch.stop)

    def scan(self):
        with mock.patch.object(runtime.PLATFORM, "detect", return_value={"distribution": "k3s"}):
            pod = runtime.pod("homestead-image-scan-1234567890-abc123", "lab", "b",
                              f"{runtime.CRICTL} images -o json", "image-scan", deadline=120)
        pod["metadata"]["uid"] = "scan-uid"
        pod["status"] = {"phase": "Running"}
        return pod

    def test_cluster_shutdown_evicts_both_observers_before_power_handoff(self):
        cluster = Fake()
        cluster.pods.extend([watcher(node="b"), self.scan()])
        review = cluster.s.review()
        self.assertTrue(review["ready"], review["blockers"])
        self.assertEqual([], review["local_storage"])
        self.assertEqual(2, len(review["warnings"]))
        coordinator = cluster.start()
        coordinator.execute()
        self.assertEqual("handoff", cluster.s.state()["phase"])
        evicted = [body["deleteOptions"]["preconditions"]["uid"]
                   for _, path, body in cluster.calls if path.endswith("/eviction")]
        self.assertIn("watch-uid", evicted)
        self.assertIn("scan-uid", evicted)

    def test_host_power_review_accepts_both_observers(self):
        fixture = power_fixtures.PowerPlanTests()
        fixture.setUp()
        fixture.objects["/apis/kubevirt.io/v1/virtualmachineinstances"]["items"] = []
        scan = self.scan()
        scan["spec"]["nodeName"] = "node1"
        fixture.objects["/api/v1/pods"]["items"].extend([watcher(), scan])
        review = __import__("homestead_power").plan("node1", "reboot")
        self.assertTrue(review["ready"], review["blockers"])
        self.assertEqual(3, len(review["drain_pods"]))
        self.assertEqual([], review["maintenance"]["local_storage"])

    def test_cluster_shutdown_uses_configured_helper_namespace(self):
        pod = watcher()
        pod["metadata"]["namespace"] = "custom"
        self.assertEqual([], maintenance.inventory(lambda _: {"items": []}, [pod], namespace="custom")["blockers"])
        self.assertTrue(maintenance.inventory(lambda _: {"items": []}, [pod])["blockers"])

    def test_changed_scan_command_does_not_gain_a_drain_exception(self):
        pod = self.scan()
        pod["spec"]["containers"][0]["command"][2] += "; rm -rf /host"
        self.assertIn("unmanaged pod", " ".join(maintenance.inventory(lambda _: {"items": []}, [pod])["blockers"]))

    def test_active_sessions_and_mutations_block_even_with_a_job_controller(self):
        for task, phrase in (("host-run", "host command"), ("node-shell", "close its sessions"),
                ("files", "close the volume browser"), ("probe", "source inspection"),
                ("image-cleanup", "cleanup"), ("import", "import/copy"), ("chown", "ownership"),
                ("reclass", "migration"), ("restructure", "restructuring"), ("vm-import", "VM import"), ("iso-copy", "ISO copy"),
                ("node-power", "power helper"), ("cluster-shutdown", "saved progress")):
            pod = watcher()
            pod["metadata"]["labels"] = names.labels(task)
            pod["metadata"]["ownerReferences"] = [{"kind": "Job", "controller": True, "uid": "owner"}]
            with self.subTest(task=task):
                self.assertIn(phrase, " ".join(maintenance.inventory(lambda _: {"items": []}, [pod])["blockers"]))
                pod["status"]["phase"] = "Succeeded"
                self.assertEqual([], maintenance.inventory(lambda _: {"items": []}, [pod])["blockers"])
        for label, phrase in (("self-data-copy", "data handoff"), ("data-preparation", "data handoff"),
                             ("handoff-worker", "data handoff"), ("longhorn-v2-setup", "host preparation"),
                             ("snapshot-files", "temporary mounts")):
            pod = watcher()
            pod["metadata"]["labels"] = {names.key(label): "operation"}
            self.assertIn(phrase, " ".join(maintenance.inventory(lambda _: {"items": []}, [pod])["blockers"]))

    def test_cluster_shutdown_blocks_copy_jobs_before_any_writes(self):
        cluster = Fake()
        pod = watcher(node="b")
        pod["metadata"]["labels"] = names.labels("import")
        pod["metadata"]["ownerReferences"] = [{"kind": "Job", "controller": True, "uid": "owner"}]
        cluster.pods.append(pod)
        review = cluster.s.review()
        self.assertFalse(review["ready"])
        self.assertIn("import/copy", " ".join(review["blockers"]))
        self.assertEqual([], cluster.calls)

    def test_image_scans_skip_cordoned_hosts(self):
        nodes = [{"metadata": {"name": "open"}, "status": {"conditions": [{"type": "Ready", "status": "True"}]}},
                 {"metadata": {"name": "closed"}, "spec": {"unschedulable": True},
                  "status": {"conditions": [{"type": "Ready", "status": "True"}]}}]
        with mock.patch.object(imports, "kget", return_value={"items": nodes}), \
                mock.patch.object(imports, "ksend") as send, mock.patch.object(imports, "_save_scans"), \
                mock.patch.object(imports, "_bust"), mock.patch.object(imports, "_SCAN_STARTED", [0]):
            self.assertEqual(["open"], imports.start_image_scan()["nodes"])
        send.assert_called_once()
        self.assertIs(False, send.call_args.args[2]["spec"]["automountServiceAccountToken"])


class PullWatchLifecycleTests(unittest.TestCase):
    def setUp(self):
        for field, value in (("NS", "lab"), ("_ASKED", {}), ("_LAYERS", {})):
            patch = mock.patch.object(pull, field, value)
            patch.start()
            self.addCleanup(patch.stop)

    def test_cordoned_or_unreadable_node_never_starts_or_renews_watcher(self):
        for host in ({"spec": {"unschedulable": True}}, None):
            with mock.patch.object(pull, "kget", return_value=host), \
                    mock.patch.object(pull, "ksend") as send, mock.patch.object(pull, "layers_of") as layers:
                self.assertEqual({}, pull.progress("node1", "image:1"))
                self.assertEqual({}, pull._ASKED)
                layers.assert_not_called()
                send.assert_not_called()

    def test_new_watcher_does_not_need_a_service_account_token(self):
        def get(path):
            if path.startswith("/api/v1/nodes/"):
                return {}
            raise urllib.error.HTTPError(path, 404, "missing", {}, None)
        with mock.patch.object(pull, "kget", side_effect=get), mock.patch.object(pull, "ksend") as send, \
                mock.patch.object(pull, "layers_of", return_value=[{"digest": "sha256:" + "a" * 64, "size": 20}]):
            self.assertEqual(20, pull.progress("node1", "image:1")["total_bytes"])
        body = send.call_args.args[2]
        self.assertIs(False, body["spec"]["automountServiceAccountToken"])
        body["metadata"]["uid"] = "new-uid"
        self.assertTrue(pull.disposable(body))

    def test_unknown_running_watcher_is_collected_after_restart(self):
        pod = watcher()
        with mock.patch.object(names, "find", return_value=[pod]), mock.patch.object(pull, "ksend") as send:
            pull.sweep(now=100)
            pull.sweep(now=100 + pull.IDLE - 1)
            send.assert_not_called()
            pull.sweep(now=100 + pull.IDLE)
        self.assertEqual("DELETE", send.call_args.args[0])
        self.assertEqual({"uid": "watch-uid"}, send.call_args.args[2]["preconditions"])
        self.assertEqual({}, pull._ASKED)

    def test_recently_requested_watcher_stays_but_finished_one_is_collected(self):
        pod = watcher()
        pull._ASKED[pod["metadata"]["name"]] = 170
        with mock.patch.object(names, "find", return_value=[pod]), mock.patch.object(pull, "ksend") as send:
            pull.sweep(now=200)
            send.assert_not_called()
            pod["status"]["phase"] = "Succeeded"
            pull.sweep(now=201)
            send.assert_called_once()

    def test_cleanup_cannot_delete_a_workload_with_watcher_label(self):
        pod = watcher()
        pod["spec"]["containers"][0]["command"] = ["sh", "-c", "sleep 1800"]
        pod["status"]["phase"] = "Succeeded"
        with mock.patch.object(names, "find", return_value=[pod]), mock.patch.object(pull, "ksend") as send:
            pull.sweep(now=500)
        send.assert_not_called()


if __name__ == "__main__":
    unittest.main()
