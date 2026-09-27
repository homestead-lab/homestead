import base64
import copy
import json
import tempfile
import unittest
from unittest import mock

from test_guest_readiness import NOW, condition, lease, obj
import homestead_k3s_health as health
import homestead_k3scluster as cluster
import homestead_operations as ops


class HostGuestVerificationTests(unittest.TestCase):
    def setUp(self):
        self.cfg = {"name": "check", "namespace": "lab", "servers": 1, "agents": 1,
                    "network": "default/lan", "addresses": ["192.0.2.20", "192.0.2.21"],
                    "setup": "k3s", "password": "a-test-password", "review_id": "f" * 32}
        with mock.patch.object(cluster, "check", return_value=""):
            self.batch = cluster.prepare(self.cfg, token="private-test-token", guest_checks=True)
        self.contract = self.batch["guest_health"]
        self.ref = {"namespace": "lab", "nodes": self.batch["plan"]["nodes"], "guest_health": self.contract,
                    "created": [], "writes": [], "started": NOW, "retain_resources": True,
                    "dispatch_protocol": 2, "phase": "awaiting-ready"}
        self.item = {"ref": self.ref, "progress": 10}
        self.objects, self.vms, self.instances, self.seeds = {}, [], [], []
        for config in self.batch["configs"]:
            name = config["name"]
            seed_name = name + "-cloudinit"
            seed = obj("Secret", seed_name, "lab", data={"userdata": base64.b64encode(config["cloud_init"].encode()).decode(), "networkdata": "bGFu"})
            spec = {"domain": {}, "volumes": [{"name": "cloudinit", "cloudInitNoCloud": {
                "secretRef": {"name": seed_name}, "networkDataSecretRef": {"name": seed_name}}}]}
            vm = obj("VirtualMachine", name, "lab", spec={"template": {"spec": spec}})
            prepared = {"name": name, "vm": vm, "secrets": [seed], "secret_name": seed_name}
            health.pin(prepared, self.contract)
            instance = obj("VirtualMachineInstance", name, "lab", spec=copy.deepcopy(spec), status={
                "phase": "Running", "nodeName": "host1", "conditions": [condition("Ready"), condition("AgentConnected")]})
            instance["metadata"]["uid"] = name + "-instance"
            instance["metadata"]["ownerReferences"] = [{"controller": True, "apiVersion": "kubevirt.io/v1",
                "kind": "VirtualMachine", "name": name, "uid": vm["metadata"]["uid"]}]
            self.ref["created"].append({"name": name, "identity": {"uid": vm["metadata"]["uid"], "resourceVersion": "1"}})
            self.ref["writes"].append({"resource": {"apiVersion": "v1", "kind": "Secret", "namespace": "lab", "name": seed_name},
                                      "phase": "accepted", "identity": {"uid": seed["metadata"]["uid"], "resourceVersion": "1"}})
            self.objects["/apis/kubevirt.io/v1/namespaces/lab/virtualmachines/" + name] = vm
            self.objects["/apis/kubevirt.io/v1/namespaces/lab/virtualmachineinstances/" + name] = instance
            self.objects["/api/v1/namespaces/lab/secrets/" + seed_name] = seed
            self.vms.append(vm)
            self.instances.append(instance)
            self.seeds.append(seed)
        self.objects["/api/v1/nodes/host1"] = obj("Node", "host1", status={"conditions": [condition("Ready")]})
        self.objects["/apis/coordination.k8s.io/v1/namespaces/kube-node-lease/leases/host1"] = lease("host1")

    def status(self, now=NOW):
        return health.status(self.item, self.objects.__getitem__, now=now)

    def test_ready_owned_instances_complete_with_only_public_observation_receipts(self):
        self.assertEqual("succeeded", self.status()[0])
        self.assertFalse(self.ref["retain_resources"])
        self.assertEqual("verified-ready", self.ref["phase"])
        self.assertEqual(2, len(self.ref["guest_observation"]["instances"]))
        journal = json.dumps(self.ref)
        for private in (self.cfg["password"], "private-test-token", "cloud_init", "client-admin.key", self.seeds[0]["data"]["userdata"]):
            self.assertNotIn(private, journal)

    def test_operation_refresh_persists_verified_receipt_without_recovery_or_cleanup_controls(self):
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(ops, "DATA_DIR", directory), \
                mock.patch.object(ops, "RESOLVERS", {"k3s-cluster": lambda item: health.status(item, self.objects.__getitem__, now=NOW)}):
            ref = {**copy.deepcopy(self.ref), "name": "check", "review_digest": "a" * 64, "review_expires": NOW}
            operation = ops.start("k3s-cluster", "check", {}, "/vms", ref)
            result = ops.list_operations()[0]
            self.assertEqual(operation["id"], result["id"])
            self.assertEqual("succeeded", result["status"])
            self.assertFalse(result["mutation_recovery"])
            self.assertFalse(result["cancellable"])
            self.assertFalse(result["cleanable"])
            self.assertEqual("verified-ready", ops._read()[0]["ref"]["phase"])

    def test_deterministic_cloud_init_pins_probe_source_config_hash_and_uuid(self):
        with mock.patch.object(cluster, "check", return_value=""):
            repeated = cluster.prepare(self.cfg, token="private-test-token", guest_checks=True)
            changed = cluster.prepare({**self.cfg, "review_id": "e" * 32}, token="private-test-token", guest_checks=True)
        self.assertEqual(repeated["configs"], self.batch["configs"])
        self.assertNotEqual(changed["configs"], self.batch["configs"])
        self.assertNotEqual(self.contract["nodes"][0]["uuid"], self.contract["nodes"][1]["uuid"])
        for node, config in zip(self.contract["nodes"], self.batch["configs"]):
            line = next(line for line in config["cloud_init"].splitlines() if line.startswith("write_files: "))
            files = json.loads(line.removeprefix("write_files: "))
            raw = base64.b64decode(files[1]["content"])
            self.assertEqual(health.digest(json.loads(raw)), node["probe"]["exec"]["command"][-1])
            compile(base64.b64decode(files[0]["content"]), health.SCRIPT, "exec")
            self.assertIn("python3", config["cloud_init"])
            self.assertTrue(all(row["owner"] == "root:root" and row["permissions"] == "0600" for row in files))

    def test_vm_replacement_deletion_or_probe_change_is_terminal_and_retained(self):
        for mutation in (lambda: self.vms[0]["metadata"].update(uid="replacement"),
                         lambda: self.vms[0]["metadata"].update(deletionTimestamp="now"),
                         lambda: self.vms[0]["spec"]["template"]["spec"]["readinessProbe"]["exec"].update(command=["true"]),
                         lambda: self.vms[0]["spec"]["template"]["spec"]["domain"]["firmware"].update(uuid="other")):
            self.setUp()
            mutation()
            self.assertEqual("failed", self.status()[0])
            self.assertTrue(self.ref["retain_resources"])

    def test_cloud_seed_replacement_content_change_or_uncertain_write_cannot_verify(self):
        for mutation in (lambda: self.seeds[0]["metadata"].update(uid="replacement"),
                         lambda: self.seeds[0]["data"].update(userdata="other"),
                         lambda: self.ref["writes"][0].update(phase="uncertain"),
                         lambda: self.ref["writes"].clear()):
            self.setUp()
            mutation()
            self.assertEqual("failed", self.status()[0])
            self.assertTrue(self.ref["retain_resources"])

    def test_unowned_paused_unready_or_changed_instance_never_completes(self):
        for mutation in (lambda: self.instances[0]["metadata"].update(ownerReferences=[]),
                         lambda: self.instances[0]["metadata"].update(deletionTimestamp="now"),
                         lambda: self.instances[0]["status"].update(conditions=[condition("Ready")]),
                         lambda: self.instances[0]["status"]["conditions"].append(condition("Paused")),
                         lambda: self.instances[0]["status"]["conditions"].append(condition("Paused", "Unknown")),
                         lambda: self.instances[0]["spec"]["readinessProbe"]["exec"].update(command=["true"]),
                         lambda: self.instances[0]["spec"]["volumes"][0]["cloudInitNoCloud"].update(userData="other")):
            self.setUp()
            mutation()
            self.assertEqual("running", self.status()[0])
            self.assertTrue(self.ref["retain_resources"])

    def test_stale_or_missing_host_heartbeat_cannot_reuse_stale_guest_ready(self):
        self.assertEqual("running", self.status(now=NOW + 91)[0])
        self.objects.pop("/api/v1/nodes/host1")
        self.assertEqual("running", self.status()[0])

    def test_timeout_and_api_failure_are_safe_without_raw_seed_errors(self):
        self.instances[0]["status"]["conditions"] = []
        self.assertEqual("failed", self.status(now=NOW + 2701)[0])
        read = mock.Mock(side_effect=ValueError("private-bootstrap-value"))
        status = health.status(self.item, read, now=NOW)
        self.assertEqual("running", status[0])
        self.assertNotIn("private-bootstrap-value", status[2])
        self.assertTrue(self.ref["retain_resources"])

    def test_authenticated_branch_does_not_probe_an_ip_or_port(self):
        with mock.patch.object(cluster, "kget", side_effect=self.objects.__getitem__), mock.patch.object(cluster, "_answers") as tcp:
            with mock.patch.object(health.time, "time", return_value=NOW):
                self.assertEqual("succeeded", cluster.status(self.item)[0])
            tcp.assert_not_called()

    def test_invalid_contract_never_succeeds(self):
        self.contract["version"] = 999
        self.assertEqual("failed", self.status()[0])
        self.assertTrue(self.ref["retain_resources"])


if __name__ == "__main__":
    unittest.main()
