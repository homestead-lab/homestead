import copy
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.environ.get("HOMESTEAD_TEST_SERVER_DIR", str(Path(__file__).resolve().parents[1] / "server")))
import homestead_self_data_admission as D
import homestead_self_data_anchor as A
import homestead_self_data_copy as COPY
import homestead_place as PLACE
from homestead_storage_journal import Held
import homestead_self_data_worker as W
import homestead_self_data_kube as K
from homestead_storage_journal import identity
from test_self_data_kube import granted
from test_self_data_coordinator import Cluster, IMAGE, OP, obj


class AdmissionTests(unittest.TestCase):
    def setUp(self):
        self.cluster = Cluster()
        self.pin = copy.deepcopy(self.cluster.anchor.state["plan"]["nodes"])
        self.nodes = [copy.deepcopy(self.cluster.objects["/api/v1/nodes/node1"])]
        self.nodes[0]["metadata"]["labels"] = {"kubernetes.io/hostname": "node1"}
        self.nodes[0]["status"].update(allocatable={"memory": "8Gi", "cpu": "4", "pods": "100"}, capacity={"memory": "8Gi"})
        self.pods = []
        self.metrics = [{"metadata": {"name": "node1"}, "timestamp": "1970-01-01T00:16:40Z", "usage": {"memory": "1Gi"}}]
        self.dep = copy.deepcopy(self.cluster.dep)
        self.dep["spec"]["replicas"] = 1
        self.dep["spec"]["template"]["spec"]["volumes"][0]["persistentVolumeClaim"]["claimName"] = "target"
        self.dep["spec"]["template"]["spec"]["containers"][0]["resources"] = {"requests": {"memory": "1Gi", "cpu": "100m"}, "limits": {"memory": "1Gi"}}
        self.job = COPY.job("lab", "homestead-data-copy-" + OP, "source", "target", IMAGE, OP, "node1")
        self.paths = []

    def read(self, path):
        self.paths.append(path)
        if path == "/api/v1/nodes": return {"items": copy.deepcopy(self.nodes)}
        if path == "/api/v1/pods": return {"items": copy.deepcopy(self.pods)}
        if path == "/apis/metrics.k8s.io/v1beta1/nodes": return {"items": copy.deepcopy(self.metrics)}
        return self.cluster.read(path)

    def review(self, stage="restart", proposal=None, **kwargs):
        return D.review(kwargs.pop("read", self.read), "lab", stage, proposal or (self.job if stage == "copy" else self.dep),
                        self.pin, 88, clock=kwargs.pop("clock", lambda: 1000), **kwargs)

    def policy(self):
        return {"threshold": 88, "reviews": {stage: self.review(stage)["receipt"] for stage in ("copy", "restart")}}

    def admit(self, policy=None):
        return D.Admitter(self.read, "lab", self.pin, policy or self.policy(), clock=lambda: 1000)

    def test_real_planner_checks_copy_and_restart_without_server_or_global_bindings(self):
        writes = copy.deepcopy(self.cluster.sent)
        with mock.patch.object(PLACE, "hardware_features", side_effect=AssertionError("must not use app globals")), \
                mock.patch.object(PLACE, "get_nodes", side_effect=AssertionError("must not reconcile node hardware")):
            admit = self.admit()
            self.assertTrue(admit("copy", self.job))
            self.assertTrue(admit("switch", self.dep))
            self.assertTrue(admit("start", self.dep))
        self.assertEqual(writes, self.cluster.sent)

    def test_every_check_refreshes_pods_and_metrics_and_never_subtracts_old_pods(self):
        admit = self.admit()
        self.assertTrue(admit("copy", self.job))
        old = obj("Pod", "old", {"nodeName": "node1", "containers": [{"name": "old", "resources": {"requests": {"memory": "8Gi"}}}]})
        old["metadata"]["deletionTimestamp"] = "1970-01-01T00:16:39Z"
        old["status"] = {"phase": "Running"}
        self.pods.append(old)
        with self.assertRaises(Held): admit("copy", self.job)
        with self.assertRaises(Held): admit("start", self.dep)
        self.assertGreaterEqual(self.paths.count("/api/v1/pods"), 5)

    def test_override_is_explicit_and_new_pressure_requires_new_review(self):
        admit = self.admit()
        self.metrics[0]["usage"]["memory"] = "7Gi"
        with self.assertRaisesRegex(Held, "new warnings"): admit("start", self.dep)
        approved = self.admit()  # Simulate the user approving the new exact review.
        self.assertTrue(approved("start", self.dep))
        self.metrics[0]["usage"]["memory"] = "7.2Gi"
        with self.assertRaisesRegex(Held, "new warnings"): approved("start", self.dep)
        self.metrics[0]["usage"]["memory"] = "1Gi"
        self.assertTrue(approved("start", self.dep))

    def test_override_never_ignores_hard_resources(self):
        self.metrics[0]["usage"]["memory"] = "7Gi"
        admit = self.admit()
        self.nodes[0]["status"]["allocatable"]["memory"] = "512Mi"
        with self.assertRaises(Held): admit("start", self.dep)

    def test_missing_and_stale_metrics_need_explicit_acknowledgement(self):
        admit = self.admit()
        self.metrics[0]["timestamp"] = "1970-01-01T00:10:00Z"
        with self.assertRaisesRegex(Held, "new warnings"): admit("start", self.dep)
        report = self.review()
        self.assertIn("live memory usage is unavailable", report["warnings"])
        self.assertFalse(report["nodes"][0]["metrics_available"])
        self.assertTrue(self.admit()("start", self.dep))

    def test_changed_template_or_replica_count_invalidates_approval(self):
        admit = self.admit()
        for change in (lambda d: d["spec"].update(replicas=2),
                       lambda d: d["spec"]["template"]["spec"]["containers"][0].update(image=IMAGE + "changed")):
            changed = copy.deepcopy(self.dep); change(changed)
            with self.assertRaisesRegex(Held, "workload changed"): admit("start", changed)

    def test_new_deleted_rebooted_or_replaced_nodes_hold(self):
        admit = self.admit()
        for change in (lambda: self.nodes[0]["metadata"].update(uid="replacement"),
                       lambda: self.nodes[0]["status"]["nodeInfo"].update(bootID="other"),
                       lambda: self.nodes.append(obj("Node", "new", ns=None)), lambda: self.nodes.clear()):
            saved = copy.deepcopy(self.nodes); change()
            with self.assertRaises(Held): admit("start", self.dep)
            self.nodes = saved

    def test_taints_affinity_and_cordon_are_hard_blockers(self):
        for change in (lambda: self.nodes[0]["spec"].update(unschedulable=True),
                       lambda: self.nodes[0]["spec"].update(taints=[{"key": "maintenance", "effect": "NoSchedule"}]),
                       lambda: self.dep["spec"]["template"]["spec"].update(nodeSelector={"hardware/igpu": "true"})):
            self.setUp(); change()
            with self.assertRaises(Held): self.review()

    def test_unknown_allocatable_or_incomplete_pods_is_not_overridable(self):
        self.nodes[0]["status"]["allocatable"].pop("pods")
        with self.assertRaises(Held): self.review()
        self.setUp()
        def paginated(path):
            data = self.read(path)
            if path == "/api/v1/pods": data["metadata"] = {"continue": "next"}
            return data
        with self.assertRaises(Held): self.review(read=paginated)

    def test_binding_unknown_or_changed_is_never_a_memory_warning(self):
        for change in (lambda: self.cluster.objects["/api/v1/namespaces/lab/persistentvolumeclaims/target"]["status"].update(phase="Pending"),
                       lambda: self.cluster.objects["/api/v1/persistentvolumes/new-pv"]["spec"]["claimRef"].update(uid="wrong"),
                       lambda: self.cluster.objects["/api/v1/persistentvolumes/new-pv"]["spec"].update(nodeAffinity={"required": {"nodeSelectorTerms": [{"matchExpressions": [{"key": "zone", "operator": "In", "values": ["other"]}]}]}})):
            self.setUp(); change()
            with self.assertRaises(Held): self.review()

    def test_unmodelled_local_devices_or_scheduler_cannot_be_approved(self):
        for extra in ({"volumes": [{"name": "usb", "hostPath": {"path": "/dev/bus/usb"}}]},
                      {"resourceClaims": [{"name": "device"}]}, {"runtimeClassName": "special"},
                      {"schedulerName": "other"}, {"schedulingGates": [{"name": "wait"}]}):
            changed = copy.deepcopy(self.dep); changed["spec"]["template"]["spec"].update(extra)
            with self.assertRaises(Held): self.review(proposal=changed)

    def test_pod_and_init_resources_are_both_counted(self):
        self.dep["spec"]["template"]["spec"]["initContainers"] = [{"name": "init", "resources": {"requests": {"memory": "9Gi"}}}]
        with self.assertRaises(Held): self.review()

    def test_joint_replicas_cannot_overbook_requests_or_host_ports(self):
        self.dep["spec"]["replicas"] = 9
        with self.assertRaises(Held): self.review()
        self.dep["spec"]["replicas"] = 2
        self.dep["spec"]["template"]["spec"]["containers"][0]["ports"] = [{"hostPort": 8088, "containerPort": 8088}]
        with self.assertRaises(Held): self.review()

    def test_checks_that_take_too_long_hold(self):
        with self.assertRaisesRegex(Held, "too long"):
            self.review(clock=mock.Mock(side_effect=[1000, 1031]))

    def test_receipts_contain_no_template_secrets_and_anchor_validates_them(self):
        self.dep["spec"]["template"]["spec"]["containers"][0]["env"] = [{"name": "PRIVATE", "value": "do-not-persist-this"}]
        policy = self.policy()
        self.assertNotIn("do-not-persist-this", str(policy))
        state = copy.deepcopy(self.cluster.anchor.state)
        state["plan"]["admission"] = policy
        A._validate(state, "lab")
        state["plan"]["admission"]["reviews"]["restart"]["warnings"].append("approve everything")
        with self.assertRaises(Held): A._validate(state, "lab")

    def test_unknown_constraint_warning_is_not_treated_as_capacity_override(self):
        self.dep["spec"]["template"]["spec"]["hostNetwork"] = True
        with self.assertRaisesRegex(Held, "memory override"): self.review()

    def test_warning_approval_cannot_be_reused_with_another_threshold(self):
        policy = self.policy()
        policy["threshold"] = 99
        with self.assertRaises(Held): self.admit(policy)("start", self.dep)

    def test_default_worker_holds_before_stop_without_persisted_approval(self):
        c = self.cluster
        runner = W.Runner(self.read, c.send, lambda _: c.logs_text, namespace="lab", deployment="homestead",
            operation=OP, anchor_uid=c.handle["uid"], worker_uid="coordinator-uid", clock=lambda: 1000)
        self.assertEqual("held", runner.tick()["status"])
        self.assertEqual(2, c.objects[c.dep_path]["spec"]["replicas"])
        self.assertFalse(any(path == c.dep_path for _, path, _ in c.sent))

    def prepared_runner(self):
        c = self.cluster
        # Simulate reviewed setup before pointer publication, not a mutation of
        # an existing production approval. The phase engine itself stays real.
        self.dep = copy.deepcopy(c.dep)
        self.dep["spec"]["template"]["spec"]["volumes"][0]["persistentVolumeClaim"]["claimName"] = "target"
        plan = copy.deepcopy(c.anchor.state["plan"])
        plan["admission"] = self.policy()
        c.objects.pop(c.anchor.path)
        c.anchor = c.fresh()
        c.handle = c.anchor.create(operation=OP, deployment={"name": "homestead", **identity(c.dep)},
            source={"name": "source", **identity(c.source)}, destination="target", replicas=2)
        c.anchor.configure(plan)
        c.anchor.pointer_published(A.pointer_digest("lab", c.anchor.state, c.handle["uid"]))
        scope = K.Scope("lab", "homestead", OP, ["source", "target"], ["old-pv", "new-pv"], ["node1"])
        def read(path):
            scope.check("GET", path)
            self.assertTrue(granted(K.access_resources(scope), "GET", path))
            if path == "/api/v1/pods":
                return {"items": copy.deepcopy(self.pods) + [copy.deepcopy(v) for v in c.objects.values() if v.get("kind") == "Pod"]}
            return self.read(path)
        return W.Runner(read, c.send, lambda _: c.logs_text, namespace="lab", deployment="homestead",
            operation=OP, anchor_uid=c.handle["uid"], worker_uid="coordinator-uid", clock=lambda: 1000)

    def finish_worker_copy(self, runner):
        c = self.cluster
        self.assertEqual("quiesce", runner.tick()["phase"])
        c.settle_stop()
        self.assertEqual("copy", runner.tick()["phase"])
        self.assertNotEqual("held", runner.tick()["status"])
        c.finish_copy()
        self.assertEqual("verify", runner.tick()["phase"])
        runner.tick()
        c.objects.pop("/api/v1/namespaces/lab/pods/copy-pod")
        self.assertEqual("switch", runner.tick()["phase"])

    def test_default_worker_complete_handoff_uses_real_admission_and_scoped_reads(self):
        runner = self.prepared_runner()
        self.finish_worker_copy(runner)
        self.assertEqual("start", runner.tick()["phase"])
        self.cluster.settle_stop()  # Controller observes the stopped cutover template.
        self.assertNotEqual("held", runner.tick()["status"])
        self.cluster.settle_start()
        self.assertEqual("done", runner.tick()["status"])

    def test_mismatched_copy_or_restart_receipt_holds_before_downtime(self):
        for stage in ("copy", "restart"):
            self.setUp()
            runner = self.prepared_runner()
            anchor = self.cluster.fresh().load(**self.cluster.handle)
            policy = copy.deepcopy(anchor.state["plan"]["admission"])
            policy["reviews"][stage]["proposal"] = "0" * 64
            runner.admit = D.Admitter(self.read, "lab", self.pin, policy, handoff=anchor.state, clock=lambda: 1000)
            self.assertEqual("held", runner.tick()["status"])
            self.assertEqual(2, self.cluster.objects[self.cluster.dep_path]["spec"]["replicas"])

    def test_capacity_lost_after_copy_prevents_cutover_and_persists_hold(self):
        runner = self.prepared_runner()
        self.finish_worker_copy(runner)
        other = obj("Pod", "other", {"nodeName": "node1", "containers": [{"name": "busy", "resources": {"requests": {"memory": "8Gi"}}}]})
        other["status"] = {"phase": "Running"}
        self.pods.append(other)
        # Homestead's fixture has no requests; exhaust pod slots too, so this
        # tests a hard scheduling failure rather than only a RAM warning.
        self.nodes[0]["status"]["allocatable"]["pods"] = "1"
        self.assertEqual("held", runner.tick()["status"])
        c = self.cluster
        self.assertEqual("source", c.objects[c.dep_path]["spec"]["template"]["spec"]["volumes"][0]["persistentVolumeClaim"]["claimName"])
        self.assertEqual(0, c.objects[c.dep_path]["spec"]["replicas"])
        self.assertEqual("held", c.fresh().load(**c.handle).state["runtime"]["state"])
        self.pods.clear(); self.nodes[0]["status"]["allocatable"]["pods"] = "100"
        self.assertEqual("held", runner.tick()["status"])

    def test_capacity_is_rechecked_again_between_switch_and_start(self):
        runner = self.prepared_runner()
        self.finish_worker_copy(runner)
        self.assertEqual("start", runner.tick()["phase"])
        self.cluster.settle_stop()
        self.nodes[0]["status"]["allocatable"]["pods"] = "1"
        self.assertEqual("held", runner.tick()["status"])
        dep = self.cluster.objects[self.cluster.dep_path]
        self.assertEqual(0, dep["spec"]["replicas"])
        self.assertEqual("target", dep["spec"]["template"]["spec"]["volumes"][0]["persistentVolumeClaim"]["claimName"])
        self.assertIn("/api/v1/namespaces/lab/persistentvolumeclaims/source", self.cluster.objects)
        self.assertIn("/api/v1/namespaces/lab/persistentvolumeclaims/target", self.cluster.objects)

    def test_capacity_lost_before_copy_never_launches_a_copy_job(self):
        runner = self.prepared_runner()
        self.assertEqual("quiesce", runner.tick()["phase"])
        self.cluster.settle_stop()
        self.assertEqual("copy", runner.tick()["phase"])
        self.nodes[0]["status"]["allocatable"]["pods"] = "1"
        self.assertEqual("held", runner.tick()["status"])
        self.assertNotIn(self.cluster.job_path, self.cluster.objects)


if __name__ == "__main__":
    unittest.main()
