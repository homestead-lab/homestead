"""What Homestead logs when it writes back an address a kube-vip Service lost
from its status: the Service and the address, and no cause - keep() repairs
the same way whether ServiceLB emptied it or something else did (#1557)."""
import contextlib
import io
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "server"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import homestead_kube as K
import homestead_vips as VIPS
from fakes.kube import Kube
from rebind import keep_bound

CLASS = "kube-vip.io/kube-vip-class"
K3S = {"load_balancer": "kube-vip", "vip_class": CLASS, "servicelb": True, "vip_service_election": True}
LEASE = "homestead-vip-192-0-2-108"


def objects(renewed):
    service = {"apiVersion": "v1", "kind": "Service",
               "metadata": {"namespace": "lab", "name": "plex",
                            "annotations": {"kube-vip.io/loadbalancerIPs": "192.0.2.108", "kube-vip.io/leaseName": LEASE}},
               "spec": {"type": "LoadBalancer", "loadBalancerClass": CLASS, "ports": [{"port": 32400, "protocol": "TCP"}]},
               "status": {"loadBalancer": {}}}
    lease = {"apiVersion": "coordination.k8s.io/v1", "kind": "Lease", "metadata": {"namespace": "lab", "name": LEASE},
             "spec": {"holderIdentity": "node-a", "renewTime": renewed, "leaseDurationSeconds": 15}}
    slices = {"apiVersion": "discovery.k8s.io/v1", "kind": "EndpointSlice",
              "metadata": {"namespace": "lab", "name": "plex-abcde", "labels": {"kubernetes.io/service-name": "plex"}},
              "endpoints": [{"addresses": ["10.42.0.9"], "conditions": {"ready": True}}]}
    return service, lease, slices


class RepairLog(unittest.TestCase):
    def setUp(self):
        keep_bound(self, VIPS)

    def test_the_log_line_names_the_service_and_address_and_no_cause(self):
        import time
        renewed = time.strftime("%Y-%m-%dT%H:%M:%S.000000Z", time.gmtime())
        with Kube(*objects(renewed)) as kube:
            VIPS.bind(K.kget, K.ksend)
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                done = VIPS.keep(K3S)
            status = kube.object("/api/v1/namespaces/lab/services/plex")["status"]
        self.assertEqual([r["name"] for r in done], ["plex"])
        self.assertEqual(status["loadBalancer"]["ingress"][0]["ip"], "192.0.2.108")
        line = next(line for line in out.getvalue().splitlines() if "192.0.2.108" in line)
        self.assertTrue(line.startswith("VIPs: lab/plex had lost 192.0.2.108 from its status"), line)
        self.assertIn("node-a answers for it; recorded it again", line)
        # keep() repairs a lost address whatever emptied it: the line blames no one.
        self.assertNotIn("ServiceLB", line)
        self.assertNotIn("pods change", line)


if __name__ == "__main__":
    unittest.main()
