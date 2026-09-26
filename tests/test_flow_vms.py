"""VMs in the architecture view: their disks, ports and use, running or not."""
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
import server


def pod(name, labels, node="node1", claims=()):
    return {"metadata": {"name": name, "namespace": "lab", "labels": labels},
            "spec": {"nodeName": node, "containers": [{"name": "c", "image": "x"}],
                     "volumes": [{"name": c, "persistentVolumeClaim": {"claimName": c}} for c in claims]},
            "status": {"phase": "Running", "startTime": "2026-09-25T10:00:00Z"}}


def vm(name, claim, labels=None):
    return {"metadata": {"name": name, "namespace": "lab"},
            "spec": {"template": {"metadata": {"labels": labels or {}},
                                  "spec": {"volumes": [{"name": "disk", "persistentVolumeClaim": {"claimName": claim}},
                                                       {"name": "ci", "cloudInitNoCloud": {}}]}}},
            "status": {"printableStatus": "Running" if name == "ubuntu" else "Stopped"}}


API = {
    "/api/v1/pods": {"items": [
        pod("web-5d9c-abcde", {"app": "web"}, claims=["web-data"]),
        pod("homestead-files-web-data", {"homestead.io/task": "files", "homestead.io/app": "web-data"},
            claims=["web-data"]),
        pod("virt-launcher-ubuntu-x1y2z", {"kubevirt.io": "virt-launcher", "vm.kubevirt.io/name": "ubuntu",
                                           "harvesterhci.io/vmName": "ubuntu"}, node="node2", claims=["ubuntu-disk"]),
        pod("virt-launcher-other-q9w8e", {"kubevirt.io": "virt-launcher", "vm.kubevirt.io/name": "other"}),
    ]},
    "/api/v1/services": {"items": [
        {"metadata": {"name": "web", "namespace": "lab"}, "spec": {"selector": {"app": "web"}, "ports": [{"port": 80}]},
         "status": {"loadBalancer": {"ingress": [{"ip": "192.0.2.214"}]}}},
        {"metadata": {"name": "ubuntu-ssh", "namespace": "lab"},
         "spec": {"selector": {"harvesterhci.io/vmName": "ubuntu"}, "ports": [{"port": 22, "name": "ssh"}]},
         "status": {"loadBalancer": {"ingress": [{"ip": "192.0.2.217"}]}}},
    ]},
    "/apis/longhorn.io/v1beta2/volumes": {"items": [
        {"metadata": {"name": f"pvc-{c}"}, "spec": {"numberOfReplicas": 2, "size": str(2**30)},
         "status": {"kubernetesStatus": {"pvcName": c}, "robustness": "healthy"}}
        for c in ("web-data", "ubuntu-disk", "router-disk")]},
    "/apis/longhorn.io/v1beta2/replicas": {"items": []},
    "/apis/kubevirt.io/v1/virtualmachineinstances": {"items": [
        {"metadata": {"name": "ubuntu", "namespace": "lab"},
         "spec": {"volumes": [{"name": "disk", "persistentVolumeClaim": {"claimName": "ubuntu-disk"}}]},
         "status": {"phase": "Running", "nodeName": "node2",
                    "interfaces": [{"name": "default", "ipAddress": "192.0.2.61", "ipAddresses": ["192.0.2.61", "fe80::1"]}]}}]},
    "/apis/kubevirt.io/v1/virtualmachines": {"items": [vm("ubuntu", "ubuntu-disk", {"harvesterhci.io/vmName": "ubuntu"}),
                                                       vm("router", "router-disk")]},
    "/apis/apps/v1/deployments": {"items": []},
    "/apis/metrics.k8s.io/v1beta1/pods": {"items": [
        {"metadata": {"name": "virt-launcher-ubuntu-x1y2z", "namespace": "lab"},
         "containers": [{"usage": {"cpu": "250m", "memory": "1024Mi"}}]}]},
}


class FlowVmTests(unittest.TestCase):
    def setUp(self):
        patches = [mock.patch.object(server, "kget", side_effect=lambda path: API[path]),
                   mock.patch.object(server.HW, "workload_features", return_value=[])]
        for patcher in patches:
            patcher.start()
            self.addCleanup(patcher.stop)
        self.flow = server.get_flow2()
        self.by = {w["id"]: w for w in self.flow["workloads"]}

    def test_launcher_pods_are_not_containers(self):
        self.assertEqual({"w:web", "w:vm-ubuntu", "w:vm-router"}, set(self.by))

    def test_homestead_helper_pods_are_not_workloads(self):
        self.assertNotIn("w:homestead-files-web-data", self.by)

    def test_a_running_vm_has_its_disk_ports_address_and_use(self):
        ubuntu = self.by["w:vm-ubuntu"]
        self.assertEqual([{"pvc": "ubuntu-disk", "vid": "v:pvc-ubuntu-disk"}], ubuntu["claims"])
        self.assertEqual([{"port": 22, "name": "ssh", "vip": "192.0.2.217"}], ubuntu["ports"])
        self.assertEqual(("192.0.2.61", "node2", True), (ubuntu["ip"], ubuntu["node"], ubuntu["running"]))
        self.assertEqual((0.25, 1024.0), (ubuntu["cpu"], ubuntu["mem_mb"]))
        self.assertIn({"id": "i:192.0.2.217", "ip": "192.0.2.217", "ports": [{"port": 22, "app": "ubuntu"}]},
                      self.flow["vips"])

    def test_a_stopped_vm_still_shows_its_disks(self):
        router = self.by["w:vm-router"]
        self.assertEqual(([{"pvc": "router-disk", "vid": "v:pvc-router-disk"}], False, "Stopped"),
                         (router["claims"], router["running"], router["state"]))


if __name__ == "__main__":
    unittest.main()
