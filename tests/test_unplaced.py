"""A pod the scheduler cannot place said only "Pending · unscheduled"."""
import sys, unittest
from pathlib import Path
from unittest import mock
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
import server
import homestead_updates as UPDATES

PORTS = "0/1 nodes are available: 1 node(s) didn't have free ports for the requested pod ports. preemption: 0/1 nodes are available: 1 No preemption victims found for incoming pod."


class ExplainTests(unittest.TestCase):
    def test_the_schedulers_reasons_said_plainly(self):
        self.assertIn("ServiceLB, publishing this app's own Service", UPDATES.explain_unplaced(PORTS, [32400]))
        self.assertIn("port it needs on the host is taken", UPDATES.explain_unplaced(PORTS))
        self.assertIn("enough free memory", UPDATES.explain_unplaced("0/1 nodes are available: 1 Insufficient memory."))
        self.assertIn("pinned", UPDATES.explain_unplaced("0/2 nodes are available: 2 node(s) didn't match Pod's node affinity/selector."))


class PullTests(unittest.TestCase):
    """openspeedtest's pull error was cut at its start, before the cause."""
    HEAD = ('failed to pull and unpack image "docker.io/openspeedtest/latest:latest": failed to resolve reference '
            '"docker.io/openspeedtest/latest:latest": failed to do request: Head '
            '"https://registry-1.docker.io/v2/openspeedtest/latest/manifests/latest": ')

    def test_the_cause_comes_first(self):
        self.assertIn("DNS", UPDATES.explain_pull(self.HEAD + "dial tcp: lookup registry-1.docker.io on 10.43.0.10:53: server misbehaving"))
        self.assertIn("IPv6", UPDATES.explain_pull(self.HEAD + "dial tcp [2600:1f18::1]:443: connect: network is unreachable"))
        self.assertIn("pull limit", UPDATES.explain_pull("toomanyrequests: You have reached your pull rate limit"))
        self.assertIn("in time", UPDATES.explain_pull(self.HEAD + "dial tcp 54.1.2.3:443: i/o timeout"))
        self.assertEqual("", UPDATES.explain_pull("Back-off restarting failed container"))


class PlexOnK3sTests(unittest.TestCase):
    """Plex, moved to k3s on the host network, kept its LoadBalancer Service:
    ServiceLB held 32400 on the node for it, so Plex itself never fitted."""

    def test_the_card_says_what_holds_the_port_and_what_to_do(self):
        dep = {"metadata": {"namespace": "lab", "name": "plex", "annotations": {}, "labels": {}},
               "spec": {"replicas": 1, "selector": {"matchLabels": {"app": "plex"}},
                        "template": {"metadata": {"labels": {"app": "plex"}}, "spec": {
                            "hostNetwork": True,
                            "containers": [{"name": "plex", "image": "lscr.io/linuxserver/plex:latest",
                                            "ports": [{"containerPort": 32400}]}]}}},
               "status": {}}
        pod = {"metadata": {"namespace": "lab", "name": "plex-69d-rldkx", "labels": {"app": "plex"},
                            "creationTimestamp": "2026-09-25T10:00:00Z"},
               "spec": {"containers": [{"name": "plex"}]},
               "status": {"phase": "Pending", "conditions": [
                   {"type": "PodScheduled", "status": "False", "reason": "Unschedulable", "message": PORTS}]}}
        svc = {"metadata": {"namespace": "lab", "name": "plex"},
               "spec": {"type": "LoadBalancer", "selector": {"app": "plex"}, "ports": [{"port": 32400}]}}
        items = {"/apis/apps/v1/deployments": {"items": [dep]}, "/api/v1/pods": {"items": [pod]},
                 "/api/v1/services": {"items": [svc]}}
        with mock.patch.object(server, "kget", side_effect=lambda path, **kw: items.get(path, {"items": []})), \
                mock.patch.object(server.NETWORK, "servicelb_present", return_value=True):
            plex = server.get_workloads()[0]
        self.assertIn("ServiceLB, publishing this app's own Service", plex["pods"][0]["unplaced"])
        self.assertTrue(any("cannot be placed" in p for p in plex["problems"]))


if __name__ == "__main__":
    unittest.main()
