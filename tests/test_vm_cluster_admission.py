import copy
import json
import tempfile
import unittest
import urllib.error
from unittest import mock

import test_vm_capacity as fixtures
import test_vm_resources as resource_fixtures
import server
import homestead_capacity_review as review


class VMClusterAdmissionTests(unittest.TestCase):
    read = fixtures.VMCapacityTests.read

    def setUp(self):
        fixtures.VMCapacityTests.setUp(self)
        self.body = {"name": "cluster", "namespace": "lab", "servers": 1, "agents": 1,
                     "network": "default/lan", "addresses": ["192.0.2.20", "192.0.2.21"],
                     "password": "private-test-password", "memory": "4Gi", "disk_gb": 20}
        self.platform = {"harvester": False, "cdi": True}
        self.objects["/apis/storage.k8s.io/v1/storageclasses/storage"] = {
            "metadata": {"name": "storage", "uid": "sc-uid", "resourceVersion": "1"},
            "provisioner": "test.storage", "volumeBindingMode": "WaitForFirstConsumer"}
        self.objects["/apis/cdi.kubevirt.io/v1beta1/storageprofiles/storage"] = {
            "metadata": {"name": "storage", "uid": "profile-uid", "resourceVersion": "1"},
            "status": {"claimPropertySets": [{"accessModes": ["ReadWriteOnce"], "volumeMode": "Filesystem"}]}}
        self.objects["/apis/k8s.cni.cncf.io/v1/namespaces/default/network-attachment-definitions/lan"] = {
            "metadata": {"name": "lan", "namespace": "default", "uid": "nad-uid", "resourceVersion": "1"},
            "spec": {"config": '{"type":"bridge","bridge":"br0"}'}}
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.sent = []
        self.after_write = lambda *args: None
        for patch in (mock.patch.object(server, "kget", side_effect=lambda path: copy.deepcopy(self.read(path))),
                      mock.patch.object(server.IMP, "kget", side_effect=lambda path: copy.deepcopy(self.read(path))),
                      mock.patch.object(server.K3SC, "check", return_value=""),
                      mock.patch.object(server, "vm_address_problem", return_value=""),
                      mock.patch.object(server.PLATFORM, "detect", side_effect=lambda: self.platform),
                      mock.patch.object(server, "vm_default_class", return_value="storage"),
                      mock.patch.object(server.PLACE, "get_nodes", side_effect=lambda: copy.deepcopy(self.nodes)),
                      mock.patch.object(server.PLACE, "hardware_features", return_value=[]),
                      mock.patch.object(server, "get_app_settings", return_value=server.DEFAULT_APP_SETTINGS),
                      mock.patch.object(server.OPS, "DATA_DIR", self.tmp.name),
                      mock.patch.object(server.IPAM, "save_record"),
                      mock.patch.object(review, "_key", return_value=b"cluster-admission-tests")):
            patch.start()
            self.addCleanup(patch.stop)

    def send(self, method, path, body=None, **kwargs):
        self.sent.append((method, path, copy.deepcopy(body)))
        value = copy.deepcopy(body or {})
        if method == "POST":
            meta = value["metadata"]
            meta.update(uid=meta["name"] + "-uid", resourceVersion="1")
            self.objects[path + "/" + meta["name"]] = copy.deepcopy(value)
        if method == "PATCH":
            value = {**copy.deepcopy(self.objects[path]), **value,
                     "metadata": {**self.objects[path]["metadata"], **value["metadata"], "resourceVersion": "2"}}
            self.objects[path] = copy.deepcopy(value)
        self.after_write(method, path, value)
        return value

    def call(self, path, body):
        handler = object.__new__(server.H)
        handler.path, handler.headers = path, {}
        handler._guard = lambda path: False
        handler._body = lambda: copy.deepcopy(body)
        handler._client_ip = lambda: "127.0.0.1"
        handler._send = mock.Mock()
        with mock.patch.object(server.IMP, "ksend", side_effect=self.send), mock.patch.object(server, "ksend") as other:
            handler.do_POST()
        other.assert_not_called()
        return handler._send.call_args.args

    def reviewed(self):
        result = self.call("/api/vm/k3s-cluster/plan", self.body)
        self.assertEqual(200, result[0], result)
        self.assertFalse(result[1]["capacity"]["blocked"], result)
        self.assertEqual([], self.sent)
        self.assertEqual([], server.OPS._read())
        return {**result[1]["config"], "capacity_token": result[1]["capacity_token"], "confirm_capacity": True}

    def test_preview_freezes_ids_but_never_exposes_join_token_or_secret_manifests(self):
        result = self.call("/api/vm/k3s-cluster/plan", self.body)
        self.assertEqual(200, result[0], result)
        self.assertEqual(2, len(result[1]["config"]["macs"]))
        self.assertNotIn("K3S_TOKEN", json.dumps(result))
        self.assertNotIn("cloud_init", json.dumps(result))
        self.assertEqual([], self.sent)

    def test_unreviewed_batch_is_zero_write_and_zero_job(self):
        result = self.call("/api/vm/k3s-cluster", self.body)
        self.assertEqual(400, result[0], result)
        self.assertEqual([], self.sent)
        self.assertEqual([], server.OPS._read())

    def test_reviewed_api_records_every_resource_and_supports_read_only_batch_inspection(self):
        body = self.reviewed()
        result = self.call("/api/vm/k3s-cluster", body)
        self.assertEqual(200, result[0], result)
        item = server.OPS._read()[0]
        self.assertEqual(2, item["ref"]["dispatch_protocol"])
        self.assertEqual(len(self.sent), len(item["ref"]["writes"]))
        self.assertTrue(all(row["phase"] == "accepted" for row in item["ref"]["writes"]))
        self.assertTrue(result[1]["operation"]["mutation_recovery"])
        before = len(self.sent)
        inspection = server.VM_MUTATION_RECOVERY.preview(item["id"], server.OPS, self.read, "admin")
        self.assertEqual("cluster", inspection["plan"]["confirm"])
        self.assertEqual(4, len(inspection["plan"]["resources"]))
        server.VM_MUTATION_RECOVERY.resolve({"id": item["id"], "capacity_token": inspection["capacity_token"],
            "confirm_capacity": True, "confirm": "cluster", "acknowledge_unknown": True}, server.OPS, self.read, "admin")
        self.assertEqual(before, len(self.sent))
        self.assertEqual("resolved-unknown", server.OPS._read()[0]["ref"]["phase"])

    def test_whole_batch_cannot_double_book_individually_available_memory(self):
        self.nodes[0]["allocatable"]["memory"] = "6Gi"
        result = self.call("/api/vm/k3s-cluster/plan", self.body)
        self.assertEqual(200, result[0], result)
        self.assertTrue(result[1]["capacity"]["blocked"])
        cfg = {**result[1]["config"], "capacity_token": result[1]["capacity_token"], "confirm_capacity": True}
        self.assertEqual(409, self.call("/api/vm/k3s-cluster", cfg)[0])
        self.assertEqual([], self.sent)

    def test_reviewed_batch_pins_claim_modes_and_tracks_both_missing_launchers(self):
        body = self.reviewed()
        result = self.call("/api/vm/k3s-cluster", body)
        self.assertEqual(200, result[0], result)
        vms = [obj for method, path, obj in self.sent if path.endswith("/virtualmachines")]
        self.assertEqual(2, len(vms))
        for vm in vms:
            storage = vm["spec"]["dataVolumeTemplates"][0]["spec"]["storage"]
            self.assertEqual(["ReadWriteOnce"], storage["accessModes"])
            self.assertEqual("Filesystem", storage["volumeMode"])
        item = server.OPS._read()[-1]
        self.assertEqual("awaiting-ready", item["ref"]["phase"])
        self.assertEqual(2, len(item["ref"]["created"]))
        health = item["ref"]["guest_health"]
        for vm, node in zip(vms, health["nodes"]):
            spec = vm["spec"]["template"]["spec"]
            self.assertEqual(node["uuid"], spec["domain"]["firmware"]["uuid"])
            self.assertEqual(node["probe"], spec["readinessProbe"])
            self.assertNotIn("livenessProbe", spec)
        self.assertNotIn(self.body["password"], json.dumps(item))
        self.assertNotIn("K3S_TOKEN", json.dumps(item))

    def test_changed_input_or_dependency_cannot_reuse_approval(self):
        body = self.reviewed()
        for changed in ({"memory": "8Gi"}, {"password": "different-private-password"}, {"agents": 0, "addresses": ["192.0.2.20"]}):
            result = self.call("/api/vm/k3s-cluster", {**body, **changed})
            self.assertIn(result[0], (400, 409), result)
            self.assertEqual([], self.sent)
        self.objects["/apis/cdi.kubevirt.io/v1beta1/storageprofiles/storage"]["metadata"]["resourceVersion"] = "2"
        self.assertEqual(409, self.call("/api/vm/k3s-cluster", body)[0])
        self.assertEqual([], self.sent)

    def test_capacity_loss_after_first_created_vm_halts_without_deleting(self):
        body = self.reviewed()
        def changed(method, path, value):
            if path.endswith("/virtualmachines"):
                self.nodes[0]["allocatable"]["memory"] = "6Gi"
        self.after_write = changed
        result = self.call("/api/vm/k3s-cluster", body)
        self.assertEqual(400, result[0], result)
        self.assertEqual(1, len([path for _, path, _ in self.sent if path.endswith("/virtualmachines")]))
        self.assertNotIn("DELETE", [method for method, _, _ in self.sent])
        self.assertEqual("failed", server.OPS._read()[-1]["status"])

    def test_new_capacity_consumer_after_first_vm_is_accounted_before_next(self):
        body = self.reviewed()
        def changed(method, path, value):
            if path.endswith("/virtualmachines"):
                self.pods.append({"metadata": {"name": "other", "namespace": "lab", "uid": "other", "resourceVersion": "1"},
                                  "spec": {"nodeName": "node1", "containers": [{"name": "app", "resources": {"requests": {"memory": "10Gi"}}}]},
                                  "status": {"phase": "Running"}})
        self.after_write = changed
        result = self.call("/api/vm/k3s-cluster", body)
        self.assertEqual(400, result[0], result)
        self.assertEqual(1, len([path for _, path, _ in self.sent if path.endswith("/virtualmachines")]))

    def test_created_vm_replaced_before_next_node_is_not_credited(self):
        body = self.reviewed()
        def changed(method, path, value):
            if path.endswith("/virtualmachines"):
                self.objects[path + "/" + value["metadata"]["name"]]["metadata"]["uid"] = "replacement"
                # The receipt returned by POST still identifies the original.
                value["metadata"]["uid"] = "original"
        self.after_write = changed
        result = self.call("/api/vm/k3s-cluster", body)
        self.assertEqual(400, result[0], result)
        self.assertEqual(1, len([path for _, path, _ in self.sent if path.endswith("/virtualmachines")]))

    def test_post_image_capacity_check_precedes_secret_vm_writes(self):
        self.platform = {"harvester": True, "cdi": False}
        body = self.reviewed()
        def download(*args):
            self.nodes[0]["allocatable"]["memory"] = "1Gi"
            return {"namespace": "lab", "name": "image", "storage_class": "storage"}
        with mock.patch.object(server.IMP.HVIMAGE, "download", side_effect=download):
            result = self.call("/api/vm/k3s-cluster", body)
        self.assertEqual(400, result[0], result)
        self.assertEqual([], self.sent)

    def test_observed_owned_launcher_is_counted_once_not_as_another_future_vm(self):
        self.nodes[0]["allocatable"]["memory"] = "8.5Gi"  # includes guest probe + console helper
        body = self.reviewed()
        def changed(method, path, vm):
            if path.endswith("/virtualmachines"):
                name = vm["metadata"]["name"]
                vmi = resource_fixtures.child(vm, "VirtualMachine", name, name + "-vmi")
                pod = resource_fixtures.child(vmi, "VirtualMachineInstance", name + "-launcher", name + "-pod")
                pod["spec"]["containers"] = [{"name": "compute", "resources": {"requests": {"memory": "4Gi", "cpu": "20m"}}}]
                self.objects[f"/apis/kubevirt.io/v1/namespaces/lab/virtualmachineinstances/{name}"] = vmi
                self.pods.append(pod)
        self.after_write = changed
        result = self.call("/api/vm/k3s-cluster", body)
        self.assertEqual(200, result[0], result)

    def test_unassigned_owned_launcher_is_replaced_by_exactly_one_future_demand(self):
        self.nodes[0]["allocatable"]["memory"] = "8.5Gi"  # includes guest probe + console helper
        body = self.reviewed()
        def changed(method, path, vm):
            if path.endswith("/virtualmachines"):
                name = vm["metadata"]["name"]
                vmi = resource_fixtures.child(vm, "VirtualMachine", name, name + "-vmi")
                pod = resource_fixtures.child(vmi, "VirtualMachineInstance", name + "-launcher", name + "-pod")
                pod["spec"] = {"containers": [{"name": "compute", "resources": {"requests": {"memory": "4Gi"}}}]}
                pod["status"] = {"phase": "Pending"}
                self.objects[f"/apis/kubevirt.io/v1/namespaces/lab/virtualmachineinstances/{name}"] = vmi
                self.pods.append(pod)
        self.after_write = changed
        result = self.call("/api/vm/k3s-cluster", body)
        self.assertEqual(200, result[0], result)

    def test_batch_ram_warning_is_overridable(self):
        self.nodes[0]["mem_used_gb"] = 15
        body = self.reviewed()
        result = self.call("/api/vm/k3s-cluster", body)
        self.assertEqual(200, result[0], result)

    def test_created_numa_controller_without_launcher_still_needs_allocation_evidence(self):
        cfg = server.VM_REVIEW.vm_cluster_configuration(self.body, preview=True)
        _, prepared, _, _ = server.VM_REVIEW.prepare_vm_cluster(cfg)
        item = prepared[0]
        domain = item["vm"]["spec"]["template"]["spec"]["domain"]
        domain["cpu"].update(numa={"guestMappingPassthrough": {}}, dedicatedCpuPlacement=True)
        domain["memory"] = {"guest": "4Gi", "hugepages": {"pageSize": "2Mi"}}
        for bucket in ("requests", "limits"):
            domain.get("resources", {}).get(bucket, {}).pop("cpu", None)
        self.config["status"] = {"observedKubeVirtVersion": "v1.9.0"}
        self.nodes[0]["labels"]["cpumanager"] = "true"
        self.nodes[0]["allocatable"]["hugepages-2Mi"] = "8Gi"
        current = copy.deepcopy(item["vm"])
        current["metadata"].update(uid="created-uid", resourceVersion="1")
        self.objects[f"/apis/kubevirt.io/v1/namespaces/lab/virtualmachines/{item['name']}"] = current
        result = server.VM_BATCH.plan(prepared, self.read, self.nodes, created={item["name"]: {"uid": "created-uid"}})
        self.assertTrue(result["blocked"])
        self.assertIn("physical topology alone cannot authorize", str(result["blockers"]))
        self.assertEqual([], self.sent)

        # Once an actual UID-owned launcher is assigned, the batch accounts
        # for its existing reservation rather than allocating a second VM.
        vmi = resource_fixtures.child(current, "VirtualMachine", item["name"], "running-vmi")
        pod = resource_fixtures.child(vmi, "VirtualMachineInstance", "launcher", "running-pod")
        pod["spec"]["containers"] = [{"name": "compute", "resources": {"requests": {"memory": "4Gi", "cpu": "2", "hugepages-2Mi": "4Gi"}}}]
        self.objects[f"/apis/kubevirt.io/v1/namespaces/lab/virtualmachineinstances/{item['name']}"] = vmi
        self.pods.append(pod)
        result = server.VM_BATCH.plan(prepared, self.read, self.nodes, created={item["name"]: {"uid": "created-uid"}})
        self.assertNotIn("physical topology alone cannot authorize", str(result["blockers"]))
        self.assertEqual([], self.sent)

    def test_joint_network_devices_cannot_be_double_booked_across_guest_vms(self):
        nad = self.objects["/apis/k8s.cni.cncf.io/v1/namespaces/default/network-attachment-definitions/lan"]
        nad["metadata"]["annotations"] = {"k8s.v1.cni.cncf.io/resourceName": "vendor/lan"}
        self.nodes[0]["allocatable"]["vendor/lan"] = "1"
        result = self.call("/api/vm/k3s-cluster/plan", self.body)
        self.assertEqual(200, result[0], result)
        self.assertTrue(result[1]["capacity"]["blocked"])
        self.assertIn("vendor/lan", str(result[1]["capacity"]["reasons"]))
        self.assertEqual([], self.sent)
        self.nodes[0]["allocatable"]["vendor/lan"] = "2"
        self.reviewed()

    def test_missing_kvm_cannot_be_overridden(self):
        del self.nodes[0]["allocatable"]["devices.kubevirt.io/kvm"]
        result = self.call("/api/vm/k3s-cluster/plan", self.body)
        self.assertEqual(200, result[0], result)
        self.assertTrue(result[1]["capacity"]["blocked"])
        body = {**result[1]["config"], "capacity_token": result[1]["capacity_token"], "confirm_capacity": True}
        self.assertEqual(409, self.call("/api/vm/k3s-cluster", body)[0])
        self.assertEqual([], self.sent)

    def persistent_batch(self):
        original = server.IMP.prepare_vm
        def prepare(*args, **kwargs):
            value = original(*args, **kwargs)
            value["vm"]["spec"]["template"]["spec"]["domain"].setdefault("devices", {})["tpm"] = {"persistent": True}
            return value
        patch = mock.patch.object(server.IMP, "prepare_vm", side_effect=prepare)
        patch.start()
        self.addCleanup(patch.stop)
        self.config["spec"]["configuration"]["vmStateStorageClass"] = "state"
        self.objects["/api/v1/namespaces/lab/persistentvolumeclaims"] = {"items": []}
        sc = copy.deepcopy(self.objects["/apis/storage.k8s.io/v1/storageclasses/storage"])
        sc["metadata"].update(name="state", uid="state-class-uid")
        self.objects["/apis/storage.k8s.io/v1/storageclasses/state"] = sc
        return sc

    def test_backend_state_topology_participates_in_joint_batch_placement(self):
        sc = self.persistent_batch()
        sc["allowedTopologies"] = [{"matchLabelExpressions": [{"key": "zone", "values": ["unavailable"]}]}]
        result = self.call("/api/vm/k3s-cluster/plan", self.body)
        self.assertEqual(200, result[0], result)
        self.assertTrue(result[1]["capacity"]["blocked"])
        self.assertIn("storage class topology", str(result))
        self.assertEqual([], self.sent)

    def test_missing_backend_profile_appearing_mid_batch_stops_further_writes(self):
        self.persistent_batch()
        body = self.reviewed()
        def changed(method, path, value):
            if path.endswith("/virtualmachines"):
                self.objects["/apis/cdi.kubevirt.io/v1beta1/storageprofiles/state"] = {
                    "metadata": {"name": "state", "uid": "new-profile", "resourceVersion": "1"},
                    "status": {"claimPropertySets": [{"volumeMode": "Filesystem", "accessModes": ["ReadWriteOnce"]}]}}
        self.after_write = changed
        result = self.call("/api/vm/k3s-cluster", body)
        self.assertEqual(400, result[0], result)
        self.assertEqual(1, sum(path.endswith("/virtualmachines") for _, path, _ in self.sent))
        self.assertFalse(any(obj.get("metadata", {}).get("name", "").startswith("persistent-state-for") for _, _, obj in self.sent))
        self.assertNotIn("DELETE", [method for method, _, _ in self.sent])

    def test_backup_namespace_change_mid_batch_retains_first_vm_and_stops(self):
        self.persistent_batch()
        self.config["spec"]["configuration"].update(developerConfiguration={"featureGates": ["IncrementalBackup"]},
            changedBlockTrackingLabelSelectors={"namespaceLabelSelector": {"matchLabels": {"backup": "yes"}}})
        namespace = {"metadata": {"name": "lab", "uid": "ns-uid", "resourceVersion": "1", "labels": {"backup": "yes"}}}
        self.objects["/api/v1/namespaces/lab"] = namespace
        body = self.reviewed()
        def changed(method, path, value):
            if path.endswith("/virtualmachines"):
                namespace["metadata"]["resourceVersion"] = "2"
        self.after_write = changed
        result = self.call("/api/vm/k3s-cluster", body)
        self.assertEqual(400, result[0], result)
        self.assertEqual(1, sum(path.endswith("/virtualmachines") for _, path, _ in self.sent))
        self.assertNotIn("DELETE", [method for method, _, _ in self.sent])


if __name__ == "__main__":
    unittest.main()
