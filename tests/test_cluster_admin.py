import sys
import unittest
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
import harvui_cluster as cluster


def node(name, roles, ready=True, pressure=None, os_image="Harvester v1.6.0"):
    labels = {f"node-role.kubernetes.io/{role}": "true" for role in roles}
    conditions = [{"type": "Ready", "status": "True" if ready else "False"}]
    for item in pressure or []:
        conditions.append({"type": item, "status": "True"})
    return {"metadata": {"name": name, "labels": labels},
            "spec": {}, "status": {"conditions": conditions,
            "nodeInfo": {"kubeletVersion": "v1.34.1+rke2r1", "osImage": os_image}}}


def pod(name, namespace="kube-system", ready=True):
    return {"metadata": {"name": name, "namespace": namespace, "labels": {"app": name}},
            "status": {"phase": "Running" if ready else "Pending",
                       "containerStatuses": [{"ready": ready}]}}


class ClusterAdministrationTests(unittest.TestCase):
    def setUp(self):
        cluster.SYS_NS = {"kube-system", "harvester-system", "longhorn-system"}

    def report(self, nodes, pods=None, events=None, csrs=None, now=1_800_000_000):
        summaries = [{"name": row["metadata"]["name"], "cpu_pct": 20,
                      "mem_pct": 30, "fs_pct": 40, "pods": 20} for row in nodes]
        return cluster.build_report("v1.34.1+rke2r1", nodes, summaries, pods or [],
                                    events or [], csrs or [], now=now)

    def test_two_member_etcd_cluster_is_online_but_has_no_failure_margin(self):
        nodes = [node("one", ["control-plane", "etcd"]),
                 node("two", ["control-plane", "etcd"]), node("three", ["worker"])]
        report = self.report(nodes, [pod("etcd-one"), pod("kube-apiserver-one")])
        self.assertEqual("attention", report["state"])
        self.assertEqual(2, report["control_plane"]["quorum_needed"])
        self.assertEqual(0, report["control_plane"]["quorum_margin"])
        self.assertEqual("Control plane + etcd", report["onboarding"]["recommended_role"])
        self.assertEqual("1.6.0", report["versions"]["harvester"])

    def test_three_member_cluster_has_one_member_failure_margin(self):
        nodes = [node(str(i), ["control-plane", "etcd"]) for i in range(3)]
        report = self.report(nodes)
        self.assertEqual(1, report["control_plane"]["quorum_margin"])
        self.assertEqual("Worker", report["onboarding"]["recommended_role"])

    def test_unready_node_and_failed_required_service_are_critical(self):
        nodes = [node("one", ["control-plane", "etcd"]),
                 node("two", ["control-plane", "etcd"], ready=False)]
        report = self.report(nodes, [pod("etcd-one"), pod("kube-apiserver-one", ready=False)])
        self.assertEqual("critical", report["state"])
        self.assertEqual(["two"], report["capacity"]["unready"])
        api = next(row for row in report["services"] if row["id"] == "api")
        self.assertEqual("critical", api["state"])

    def test_replaced_and_completed_system_pods_do_not_reduce_current_readiness(self):
        current = pod("rke2-coredns-current")
        completed = pod("rke2-coredns-old", ready=False)
        completed["status"]["phase"] = "Succeeded"
        terminating = pod("rke2-coredns-replaced", ready=False)
        terminating["metadata"]["deletionTimestamp"] = "2027-01-15T08:00:00Z"
        report = self.report([node("one", ["control-plane", "etcd"])],
                             [current, completed, terminating])
        dns = next(row for row in report["services"] if row["id"] == "dns")
        self.assertEqual({"pods": 1, "ready": 1, "state": "healthy"},
                         {key: dns[key] for key in ("pods", "ready", "state")})

    def test_certificate_and_recent_system_warnings_are_summarized_without_bodies(self):
        now = 1_800_000_000
        created = "2027-01-15T07:46:40Z"  # 20 minutes before `now`
        csr = {"metadata": {"name": "node-csr", "creationTimestamp": created},
               "spec": {"signerName": "kubernetes.io/kubelet-serving", "request": "secret-body"},
               "status": {}}
        event = {"type": "Warning", "reason": "Unhealthy", "message": "probe failed",
                 "metadata": {"namespace": "kube-system", "creationTimestamp": created},
                 "involvedObject": {"kind": "Pod", "name": "coredns"}}
        report = self.report([node("one", ["control-plane", "etcd"])],
                             events=[event], csrs=[csr], now=now)
        self.assertEqual("attention", report["certificates"]["state"])
        self.assertEqual("node-csr", report["certificates"]["entries"][0]["name"])
        self.assertNotIn("request", report["certificates"]["entries"][0])
        self.assertEqual("Unhealthy", report["warnings"][0]["reason"])

    def test_old_warning_events_do_not_keep_platform_in_attention(self):
        event = {"type": "Warning", "reason": "Old", "message": "resolved",
                 "metadata": {"namespace": "kube-system", "creationTimestamp": "2026-01-01T00:00:00Z"},
                 "involvedObject": {"kind": "Pod", "name": "old"}}
        report = self.report([node("one", ["control-plane", "etcd"])], events=[event])
        self.assertEqual([], report["warnings"])


if __name__ == "__main__":
    unittest.main()
