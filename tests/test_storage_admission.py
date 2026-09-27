import copy
import unittest
from unittest import mock

import test_deploy_capacity as fixtures
import test_vm_resources as vm_fixtures
import homestead_storage_admission as admission
import homestead_storage_journal as journal
import homestead_reclass_handoff as handoff


class StorageAdmissionTests(unittest.TestCase):
    get = fixtures.DeployCapacityTests.get

    def setUp(self):
        fixtures.DeployCapacityTests.setUp(self)
        self.item = {"id": "move", "ref": {"namespace": "lab", "claim": "data", "temp": "data-copy", "consumers": [], "storage_writes": []}}
        self.proposals = []
        self.objects["/apis/apps/v1/namespaces/lab/replicasets"] = {"items": []}
        self.objects["/apis/batch/v1/namespaces/lab/jobs"] = {"items": []}
        self.pvc("data", "pv-old")
        self.pvc("data-copy", "pv-copy")

    def pvc(self, name, pv):
        self.objects[f"/api/v1/namespaces/lab/persistentvolumeclaims/{name}"] = {
            "apiVersion": "v1", "kind": "PersistentVolumeClaim",
            "metadata": {"name": name, "namespace": "lab", "uid": name + "-uid", "resourceVersion": "1"},
            "spec": {"volumeName": pv, "storageClassName": "test-class", "accessModes": ["ReadWriteMany"], "resources": {"requests": {"storage": "10Gi"}}},
            "status": {"phase": "Bound"}}
        self.objects["/api/v1/persistentvolumes/" + pv] = {"apiVersion": "v1", "kind": "PersistentVolume",
            "metadata": {"name": pv, "uid": pv + "-uid", "resourceVersion": "1"},
            "spec": {"accessModes": ["ReadWriteMany"], "storageClassName": "test-class"}}

    def template(self, memory="1Gi", claim="data"):
        return {"metadata": {"labels": {"app": "fixture"}}, "spec": {
            "containers": [{"name": "app", "image": "example/app:1", "resources": {"requests": {"memory": memory}, "limits": {"memory": memory}}}],
            "volumes": [{"name": "data", "persistentVolumeClaim": {"claimName": claim}}]}}

    def controller(self, kind, name, memory="1Gi", count=1):
        path = handoff._path("lab", kind, name)
        spec = {"replicas": 0, "template": self.template(memory)}
        consumer = {"kind": kind, "name": name, "replicas": count}
        if kind == "CronJob":
            spec = {"suspend": True, "concurrencyPolicy": "Forbid", "jobTemplate": {"spec": {"parallelism": count, "template": self.template(memory)}}}
            consumer = {"kind": kind, "name": name, "suspend": False}
        obj = {"apiVersion": "batch/v1" if kind == "CronJob" else "apps/v1", "kind": kind,
               "metadata": {"name": name, "namespace": "lab", "uid": name + "-uid", "resourceVersion": "1"}, "spec": spec}
        self.objects[path] = obj
        proposed = copy.deepcopy(obj)
        proposed["spec"]["suspend" if kind == "CronJob" else "replicas"] = False if kind == "CronJob" else count
        self.proposals.append({"kind": kind, "path": path, "object": proposed})
        self.item["ref"]["consumers"].append(consumer)
        return path

    def restarted(self, path):
        proposal = next(p for p in self.proposals if p["path"] == path)
        self.objects[path] = copy.deepcopy(proposal["object"])
        self.objects[path]["metadata"]["resourceVersion"] = "2"
        obj = self.objects[path]
        self.item["ref"]["storage_writes"].append({"step": "restart:" + path, "state": "accepted", "method": "PUT",
            "target": journal.target("PUT", path, obj, "lab"), "after": journal.identity(obj), "shape": journal.shape(obj)})
        self.proposals.remove(proposal)
        self.item["ref"]["cutover_complete"] = True

    def pod(self, name, controller_name, kind="Deployment", host=None, memory="1Gi", terminating=False):
        owner_kind, owner_uid = kind, controller_name + "-uid"
        if kind == "Deployment":
            owner_kind, owner_uid = "ReplicaSet", controller_name + "-rs-uid"
            self.objects["/apis/apps/v1/namespaces/lab/replicasets"]["items"].append({
                "metadata": {"name": controller_name + "-rs", "namespace": "lab", "uid": owner_uid,
                             "ownerReferences": [{"kind": "Deployment", "uid": controller_name + "-uid", "controller": True}]}})
        pod = {"metadata": {"name": name, "namespace": "lab", "uid": name + "-uid",
                           "ownerReferences": [{"kind": owner_kind, "uid": owner_uid, "controller": True}]},
               "spec": self.template(memory)["spec"], "status": {"phase": "Running" if host else "Pending"}}
        if host: pod["spec"]["nodeName"] = host
        if terminating: pod["metadata"]["deletionTimestamp"] = "2026-09-27T00:00:00Z"
        self.objects["/api/v1/pods"]["items"].append(pod)
        return pod

    def plan(self):
        result = admission.plan(self.item, self.proposals, self.get, self.nodes, 88)
        self.send.assert_not_called()
        return result

    def vm(self, name="guest", memory="4Gi", claim=None):
        self.nodes[0]["labels"]["kubevirt.io/schedulable"] = "true"
        self.nodes[0]["allocatable"].update({"devices.kubevirt.io/kvm": "100",
            "devices.kubevirt.io/tun": "100", "devices.kubevirt.io/vhost-net": "100"})
        self.objects["/apis/kubevirt.io/v1/kubevirts"] = {"items": [{
            "metadata": {"name": "kubevirt", "namespace": "kubevirt", "uid": "kv-uid", "resourceVersion": "1"},
            "spec": {"configuration": {}}, "status": {"observedKubeVirtVersion": "v1.9.0"}}]}
        obj = vm_fixtures.vm()
        obj.update(apiVersion="kubevirt.io/v1", kind="VirtualMachine")
        obj["metadata"].update(name=name, uid=name + "-uid")
        obj["spec"]["runStrategy"] = "Halted"
        spec = obj["spec"]["template"]["spec"]
        spec["domain"]["memory"]["guest"] = memory
        if claim:
            spec["volumes"] = [{"name": "data", "persistentVolumeClaim": {"claimName": claim}}]
            spec["domain"]["devices"] = {"disks": [{"name": "data", "disk": {"bus": "virtio"}}]}
        path = handoff._path("lab", "VirtualMachine", name)
        self.objects[path] = obj
        proposed = copy.deepcopy(obj)
        proposed["spec"]["runStrategy"] = "Always"
        self.proposals.append({"kind": "VirtualMachine", "path": path, "object": proposed})
        self.item["ref"]["consumers"].append({"kind": "VirtualMachine", "name": name, "run_strategy": "Always"})
        return path

    def test_vm_and_container_compete_for_one_memory_budget(self):
        self.vm(memory="4Gi")
        self.controller("Deployment", "app", "4Gi")
        result = self.plan()
        self.assertTrue(result["blocked"], result)
        self.assertEqual(2, result["pods"])
        self.assertEqual(1, result["vm_count"])
        self.proposals[1]["object"]["spec"]["template"]["spec"]["containers"][0]["resources"] = {
            "requests": {"memory": "1Gi"}, "limits": {"memory": "1Gi"}}
        self.assertFalse(self.plan()["blocked"])

    def test_restarted_vm_without_launcher_still_reserves_memory(self):
        path = self.vm(memory="4Gi")
        self.controller("Deployment", "app", "4Gi")
        self.restarted(path)
        result = self.plan()
        self.assertTrue(result["blocked"], result)
        self.assertEqual(2, result["pods"])
        self.assertEqual(1, result["created_count"])

    def test_shared_rwx_does_not_authorize_guest_disk_and_container_writers(self):
        self.vm(memory="1Gi", claim="data")
        self.controller("Deployment", "app", "1Gi")
        result = self.plan()
        self.assertTrue(result["blocked"], result)
        self.assertIn("concurrent guest-disk writers", " ".join(result["blockers"]))

    def test_restarted_vm_receipt_does_not_bypass_changed_disk_readiness(self):
        path = self.vm(memory="1Gi", claim="data")
        self.restarted(path)
        self.objects["/api/v1/namespaces/lab/persistentvolumeclaims/data"]["status"]["phase"] = "Lost"
        result = self.plan()
        self.assertTrue(result["blocked"])
        self.assertIn("not Bound", " ".join(result["blockers"]))

    def test_mixed_restart_keeps_numa_allocation_requirements(self):
        self.vm(memory="1Gi")
        self.controller("Deployment", "app", "1Gi")
        self.proposals[0]["object"]["spec"]["template"]["spec"]["domain"]["cpu"].update(
            numa={"guestMappingPassthrough": {}}, dedicatedCpuPlacement=True)
        result = self.plan()
        self.assertTrue(result["blocked"], result)
        self.assertIn("verified local CPU", " ".join(result["blockers"]))

    def test_two_individually_fitting_workloads_cannot_double_book_capacity(self):
        self.controller("Deployment", "one", "5Gi"); self.controller("Deployment", "two", "5Gi")
        self.assertTrue(self.plan()["blocked"])

    def test_restart_receipt_without_pods_still_reserves_its_entire_demand(self):
        first = self.controller("Deployment", "one", "5Gi")
        self.controller("Deployment", "two", "5Gi"); self.restarted(first)
        result = self.plan()
        self.assertTrue(result["blocked"])
        self.assertEqual(2, result["pods"])

    def test_pending_owned_pod_is_replaced_not_double_counted(self):
        first = self.controller("Deployment", "one", "4Gi")
        self.controller("Deployment", "two", "4Gi"); self.restarted(first)
        self.pod("one-pending", "one", memory="4Gi")
        result = self.plan()
        self.assertFalse(result["blocked"], result)
        self.assertEqual(2, result["pods"])

    def test_terminating_pod_is_not_credited_as_freed_capacity(self):
        first = self.controller("Deployment", "one", "4Gi")
        self.controller("Deployment", "two", "4Gi"); self.restarted(first)
        self.pod("one-old", "one", host="a", memory="4Gi", terminating=True)
        self.assertTrue(self.plan()["blocked"])

    def test_before_cutover_uses_copied_pv_node_affinity_not_old_pv(self):
        self.controller("Deployment", "one")
        self.objects["/api/v1/persistentvolumes/pv-copy"]["spec"]["nodeAffinity"] = {"required": {"nodeSelectorTerms": [
            {"matchExpressions": [{"key": "kubernetes.io/hostname", "operator": "In", "values": ["other-host"]}]}]}}
        self.assertTrue(self.plan()["blocked"])
        self.item["ref"]["cutover_complete"] = True
        self.assertFalse(self.plan()["blocked"])

    def test_statefulset_ordinals_project_real_claim_names(self):
        path = self.controller("StatefulSet", "database", count=2)
        template = {"metadata": {"name": "data"}}
        for obj in (self.objects[path], self.proposals[0]["object"]):
            obj["spec"].update(volumeClaimTemplates=[template], ordinals={"start": 10})
        self.pvc("data-database-10", "pv-ten"); self.pvc("data-database-11", "pv-eleven")
        self.item["ref"].update(claim="data-database-10", temp="data-copy")
        result = self.plan()
        self.assertFalse(result["blocked"], result)
        self.assertEqual(["StatefulSet/database-10", "StatefulSet/database-11"], [s["name"] for s in result["services"]])

    def test_statefulset_auto_delete_retention_is_not_silently_accepted(self):
        self.controller("StatefulSet", "database")
        self.proposals[0]["object"]["spec"]["persistentVolumeClaimRetentionPolicy"] = {"whenScaled": "Delete"}
        with self.assertRaisesRegex(ValueError, "delete its claims"): self.plan()

    def test_cronjob_models_parallelism_and_warns_about_future_executions(self):
        self.controller("CronJob", "backup", "3Gi", count=3)
        result = self.plan()
        self.assertTrue(result["blocked"])
        self.assertEqual(3, result["pods"])
        self.assertIn("one next execution", " ".join(result["warnings"]))

    def test_missing_or_replaced_controller_ownership_blocks_admission(self):
        first = self.controller("Deployment", "one"); self.restarted(first)
        self.objects[first]["metadata"]["uid"] = "replacement"
        with self.assertRaises(journal.Held): self.plan()

    def test_proposals_outside_the_recorded_workloads_are_rejected(self):
        self.controller("Deployment", "one")
        self.proposals.append({"path": "/not/a/workload", "kind": "Deployment", "object": {}})
        with self.assertRaisesRegex(ValueError, "outside"): self.plan()
