import json
import unittest
from unittest.mock import patch
import test_networking
import homestead_networking as NET
import homestead_platform as PLATFORM


class DefaultsTests(unittest.TestCase):
    def setUp(self):
        base = test_networking.NetworkingTests("setUp")
        base.setUp()
        self.objects, self.sent = base.objects, base.sent
        self.ip = "192.168.1.80"
        self.map = "/api/v1/namespaces/lab/configmaps/homestead-vips"
        self.objects[self.map] = {"data": {"vips.json": json.dumps([{"ip": self.ip, "default": True}])}}
        self.platform = {"load_balancer": "kube-vip", "servicelb": True, "vip_service_election": True,
                         "vip_shared_lease": True, "vip_class": PLATFORM.VIP_CLASS}
        p = patch.object(PLATFORM, "detect", return_value=self.platform)
        p.start()
        self.addCleanup(p.stop)

    def plan(self, **extra):
        return NET.service_plan({"name": "media", "namespace": "lab", "vip_mode": "shared",
                                 "ports": [{"port": 8080, "target_port": 80}], **extra}, require_workload=False)

    def test_default_overrides_legacy_access_ip_without_changing_services(self):
        self.assertEqual(self.ip, NET.shared_vip())
        self.assertEqual(self.ip, self.plan()["vip"])
        NET.set_default_vip(self.ip)
        self.assertTrue(all("configmaps" in path for _, path, _ in self.sent))

    def test_default_cannot_be_removed_or_set_to_node(self):
        with self.assertRaisesRegex(ValueError, "another default"):
            NET.remove_vip(self.ip)
        with self.assertRaisesRegex(ValueError, "node address"):
            NET.set_default_vip("192.168.1.210")

    def test_default_requires_a_vip_provider_and_reserved_address(self):
        with self.assertRaisesRegex(ValueError, "Add this reserved"):
            NET.set_default_vip("192.168.1.89")
        self.platform["load_balancer"] = "servicelb"
        with self.assertRaisesRegex(ValueError, "Install kube-vip"):
            NET.set_default_vip(self.ip)

    def test_common_lease_is_per_ip_not_per_service(self):
        self.assertEqual("homestead-vip-192-168-1-80", PLATFORM.vip_annotations(self.ip)["kube-vip.io/leaseName"])
        self.assertNotEqual(PLATFORM.vip_annotations(self.ip), PLATFORM.vip_annotations("192.168.1.81"))

    def test_unsafe_shared_elections_are_refused(self):
        svc = self.objects["/api/v1/services"]["items"][0]
        svc["metadata"]["annotations"]["kube-vip.io/loadbalancerIPs"] = self.ip
        svc["status"]["loadBalancer"]["ingress"] = [{"ip": self.ip}]
        with self.assertRaisesRegex(ValueError, "common kube-vip lease"):
            self.plan()
        svc["metadata"]["annotations"].update(PLATFORM.vip_annotations(self.ip))
        self.assertEqual(self.ip, self.plan()["vip"])
        with self.assertRaisesRegex(ValueError, "same namespace"):
            self.plan(namespace="other")

    def test_pod_network_vm_uses_its_persistent_template_labels(self):
        vm = {"metadata": {"name": "guest", "namespace": "lab"}, "spec": {"template": {
            "metadata": {"labels": {"kubevirt.io/domain": "guest"}}, "spec": {
                "networks": [{"name": "default", "pod": {}}],
                "domain": {"devices": {"interfaces": [{"name": "default", "masquerade": {}}]}}}}}}
        self.objects["/apis/kubevirt.io/v1/virtualmachines"] = {"items": [vm]}
        cfg = {"name": "guest-web", "namespace": "lab", "workload": "guest", "workload_kind": "VirtualMachine",
               "vip_mode": "shared", "ports": [{"port": 8080, "target_port": 80}]}
        result = NET.create_service(cfg)
        self.assertEqual("VirtualMachine/guest", result["path"]["workload"])
        body = self.sent[-1][2]
        self.assertEqual({"kubevirt.io/domain": "guest"}, body["spec"]["selector"])
        self.assertEqual(PLATFORM.VIP_CLASS, body["spec"]["loadBalancerClass"])
        vm["spec"]["template"]["spec"]["networks"][0] = {"name": "default", "multus": {"networkName": "lan"}}
        with self.assertRaisesRegex(ValueError, "supported pod network"):
            NET.service_plan(cfg)


if __name__ == "__main__":
    unittest.main()
