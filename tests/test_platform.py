import sys
import unittest
import urllib.error
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "server"))

import homestead_platform as PLATFORM


def fake(groups, kubelet, daemonsets=(), extra=None):
    extra = extra or {}

    def get(path):
        if path == "/apis":
            return {"groups": [{"name": g} for g in groups]}
        if path == "/api/v1/nodes":
            return {"items": [{"metadata": {"name": "n1", "labels": {"node-role.kubernetes.io/control-plane": "true"}},
                               "status": {"nodeInfo": {"kubeletVersion": kubelet, "architecture": "amd64"},
                                          "addresses": [{"type": "InternalIP", "address": "10.0.0.5"}]}}]}
        if path == "/apis/apps/v1/namespaces/kube-system/daemonsets":
            return {"items": [{"metadata": {"name": d}} for d in daemonsets]}
        if path in extra:
            return extra[path]
        raise urllib.error.HTTPError(path, 404, "missing", None, None)
    return get


class PlatformTests(unittest.TestCase):
    def tearDown(self):
        PLATFORM.bind(None)
        PLATFORM._cached.update(at=0.0, value=None)

    def use(self, *args, **kw):
        PLATFORM.bind(fake(*args, **kw))
        PLATFORM._cached.update(at=0.0, value=None)
        return PLATFORM.detect()

    def test_harvester_keeps_kube_vip_whatever_else_is_installed(self):
        p = self.use({"harvesterhci.io", "longhorn.io", "kubevirt.io", "helm.cattle.io", "metallb.io"}, "v1.31.4+rke2r1")
        self.assertEqual(("harvester", "kube-vip", True, True), (p["distribution"], p["load_balancer"], p["longhorn"], p["kubevirt"]))
        self.assertEqual({"kube-vip.io/loadbalancerIPs": "192.0.2.242"}, PLATFORM.vip_annotations("192.0.2.242"))

    def test_a_plain_k3s_cluster(self):
        p = self.use({"helm.cattle.io"}, "v1.31.4+k3s1", daemonsets=("svclb-traefik-abc",))
        self.assertEqual(("k3s", "servicelb", False, False, ["10.0.0.5"]),
                         (p["distribution"], p["load_balancer"], p["longhorn"], p["kubevirt"], p["control_plane"]))
        guide = PLATFORM.join_guide()
        self.assertIn("K3S_URL=https://10.0.0.5:6443", guide["agent"])
        self.assertIn('INSTALL_K3S_VERSION="v1.31.4+k3s1"', guide["agent"])
        self.assertEqual("/var/lib/rancher/k3s/server/node-token", guide["token_file"])

    def test_metallb_gets_its_own_annotations_and_pools(self):
        self.use({"metallb.io"}, "v1.30.2", extra={"/apis/metallb.io/v1beta1/ipaddresspools": {"items": [
            {"metadata": {"name": "lan"}, "spec": {"addresses": ["192.0.2.240-192.0.2.250", "10.9.0.0/28"]}}]}})
        self.assertEqual({"metallb.universe.tf/loadBalancerIPs": "192.0.2.241", "metallb.universe.tf/allow-shared-ip": "homestead"},
                         PLATFORM.vip_annotations("192.0.2.241"))
        pools = PLATFORM.metallb_pools()
        self.assertEqual([{"rangeStart": "192.0.2.240", "rangeEnd": "192.0.2.250"}, {"subnet": "10.9.0.0/28"}],
                         pools[0]["spec"]["ranges"])
        self.assertEqual({}, PLATFORM.vip_annotations(""))

    def test_rke2_join_uses_the_supervisor_port(self):
        self.use(set(), "v1.31.4+rke2r1")
        guide = PLATFORM.join_guide()
        self.assertIn("server: https://10.0.0.5:9345", guide["config"])


if __name__ == "__main__":
    unittest.main()
