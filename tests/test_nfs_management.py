"""NFS export safety and independent lifecycle, without a live cluster."""
import copy
import sys
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
import homestead_nfs as NFS
import server


class NfsExportTests(unittest.TestCase):
    def test_existing_export_handles_survive_reordering_removal_and_reinstall(self):
        rows = [{"name": "media", "pvc": "media", "nfs_clients": "192.0.2.0/24"},
                {"name": "photos", "pvc": "photos", "nfs_clients": "192.0.2.0/24"}]
        old = {"spec": {"template": {"spec": {"containers": [{"env": [
            {"name": "NFS_EXPORT_1", "value": "/exports/media 192.0.2.0/24(ro,fsid=1,root_squash)"},
            {"name": "NFS_EXPORT_2", "value": "/exports/photos 192.0.2.0/24(ro,fsid=2,root_squash)"}]}]}}}}
        saved = NFS.export_ids(rows, old)
        self.assertEqual(["1", "2"], [r["nfs_fsid"] for r in saved])
        saved = [saved[1], {"name": "archive", "pvc": "archive", "nfs_clients": "192.0.2.0/24"}]
        saved = NFS.export_ids(saved)
        self.assertEqual("2", saved[0]["nfs_fsid"])
        self.assertNotIn(saved[1]["nfs_fsid"], ("0", "1", "2"))
        self.assertEqual(saved, NFS.export_ids(saved))
        pvc = {"spec": {"accessModes": ["ReadWriteMany"]}, "status": {"phase": "Bound"}}
        rebuilt = NFS.configure(old, NFS.exports(saved, lambda _: pvc))
        env = rebuilt["spec"]["template"]["spec"]["containers"][0]["env"]
        self.assertTrue(any("/exports/photos " in item["value"] and "fsid=2," in item["value"] for item in env))

    def test_client_network_is_explicit(self):
        self.assertEqual("192.0.2.0/24", NFS.client_network("192.0.2.52/24"))
        self.assertEqual("192.0.2.52/32", NFS.client_network("192.0.2.52"))
        for bad in ("", "*", "0.0.0.0/0", "::1", "127.0.0.1", "224.0.0.1/24"):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                NFS.client_network(bad)

    def test_only_explicit_bound_rwx_shares_are_exported(self):
        rows = [{"name": "media", "pvc": "share-media", "nfs_clients": "192.0.2.0/24"},
                {"name": "private", "pvc": "private"}]
        pvc = {"spec": {"accessModes": ["ReadWriteMany"]}, "status": {"phase": "Bound"}}
        self.assertEqual(["media"], [item["name"] for item in NFS.exports(rows, lambda _name: pvc)])
        for access, phase in ((["ReadWriteOnce"], "Bound"), (["ReadWriteMany"], "Pending")):
            with self.subTest(access=access, phase=phase), self.assertRaises(ValueError):
                NFS.exports(rows, lambda _name: {"spec": {"accessModes": access}, "status": {"phase": phase}})

    def test_container_exports_just_selected_claims_with_client_restrictions(self):
        base = {"spec": {"template": {"spec": {"containers": [{"name": NFS.NAME, "image": "old"}]}}}}
        result = NFS.configure(base, [{"name": "media", "pvc": "share-media", "sub_path": "movies",
                                       "clients": "192.0.2.0/24", "read_only": True}])
        self.assertEqual("old", base["spec"]["template"]["spec"]["containers"][0]["image"])
        pod = result["spec"]["template"]["spec"]
        container = pod["containers"][0]
        self.assertEqual({"capabilities": {"add": ["SYS_ADMIN"]}}, container["securityContext"])
        self.assertNotIn("privileged", container["securityContext"])
        self.assertEqual(["nfs-root", "export-1"], [item["name"] for item in pod["volumes"]])
        self.assertEqual("share-media", pod["volumes"][1]["persistentVolumeClaim"]["claimName"])
        self.assertEqual("movies", container["volumeMounts"][1]["subPath"])
        self.assertTrue(container["volumeMounts"][1]["readOnly"])
        self.assertIn("root_squash", container["env"][2]["value"])
        self.assertIn("192.0.2.0/24(ro", container["env"][2]["value"])
        self.assertEqual("NFS_DISABLE_VERSION_3", container["env"][0]["name"])
        self.assertEqual({"port": 2049}, container["readinessProbe"]["tcpSocket"])
        self.assertEqual("true", pod["nodeSelector"][NFS.HOST_LABEL])
        self.assertEqual("move", server.FAILOVER.mode_of(pod))
        self.assertEqual("Recreate", result["spec"]["strategy"]["type"])


class NfsLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.dep = server._smb_path("deployments", NFS.NAME)
        self.svc = server._smb_path("services", NFS.NAME)
        self.pvc = f"/api/v1/namespaces/{server.SMB_NAMESPACE}/persistentvolumeclaims/share-media"
        self.cm = f"/api/v1/namespaces/{server.SMB_NAMESPACE}/configmaps/{server.SHARES.CONFIGMAP()}"
        self.secret = f"/api/v1/namespaces/{server.SMB_NAMESPACE}/secrets/{server.SHARES.SECRET()}"
        self.objects = {
            self.dep: {"metadata": {"name": NFS.NAME}, "spec": {"replicas": 1,
                       "template": {"spec": {"containers": [{"name": NFS.NAME}], "volumes": []}}}},
            self.svc: {"metadata": {"name": NFS.NAME}, "spec": {}},
            self.pvc: {"metadata": {"name": "share-media"}},
            self.cm: {"metadata": {"name": server.SHARES.CONFIGMAP()}},
            self.secret: {"metadata": {"name": server.SHARES.SECRET()}},
        }
        self.sent = []

    def get(self, path, **_kwargs):
        if path not in self.objects:
            raise urllib.error.HTTPError(path, 404, "not found", {}, None)
        return copy.deepcopy(self.objects[path])

    def send(self, method, path, body=None, **_kwargs):
        self.sent.append((method, path, copy.deepcopy(body)))
        if method == "DELETE":
            self.objects.pop(path, None)
            return {}
        if method == "PATCH":
            self.objects[path]["spec"].update(body["spec"])
            return copy.deepcopy(self.objects[path])
        self.objects[path] = copy.deepcopy(body)
        return copy.deepcopy(body)

    def test_disabling_and_removing_nfs_never_delete_claims_or_smb_config(self):
        with mock.patch.object(server, "kget", self.get), mock.patch.object(server, "ksend", self.send), \
                mock.patch.object(server.SHARES, "_state", return_value=([], {}, None, None, None)):
            server.set_nfs(False)
            self.assertEqual(0, self.objects[self.dep]["spec"]["replicas"])
            server.remove_nfs()
        self.assertNotIn(self.dep, self.objects)
        self.assertNotIn(self.svc, self.objects)
        for path in (self.pvc, self.cm, self.secret):
            self.assertIn(path, self.objects)
        self.assertEqual([self.dep, self.svc], [path for method, path, _ in self.sent if method == "DELETE"])

    def test_general_workload_mutations_reject_managed_nfs(self):
        with self.assertRaisesRegex(ValueError, "Network Shares"):
            server.guard_managed_smb(server.SMB_NAMESPACE, NFS.NAME)
        for kind in ("Deployment", "Service"):
            with self.assertRaisesRegex(ValueError, "Network Shares"):
                server.guard_smb_object(kind, server.SMB_NAMESPACE, NFS.NAME)

    def test_install_manifest_is_separate_from_smb_and_preserves_client_ip(self):
        rows = [{"name": "media", "pvc": "share-media", "nfs_clients": "192.0.2.0/24"}]
        pvc = {"spec": {"accessModes": ["ReadWriteMany"]}, "status": {"phase": "Bound"}}
        with mock.patch.object(server.SHARES, "_pvc", return_value=pvc), \
                mock.patch.object(server.PLATFORM, "detect", return_value={"load_balancer": "kube-vip"}), \
                mock.patch.object(server.NETWORK, "prepare_deploy", side_effect=lambda cfg: cfg):
            dep, svc = server._nfs_deployment(rows, "192.0.2.246")
        self.assertEqual(NFS.NAME, dep["metadata"]["name"])
        self.assertEqual("Local", svc["spec"]["externalTrafficPolicy"])
        self.assertEqual("true", svc["metadata"]["annotations"]["homestead.io/exclusive-vip"])
        self.assertEqual("192.0.2.246", svc["metadata"]["annotations"]["kube-vip.io/loadbalancerIPs"])
        self.assertEqual([2049], [port["port"] for port in svc["spec"]["ports"]])
        self.assertEqual("share-media", dep["spec"]["template"]["spec"]["volumes"][1]["persistentVolumeClaim"]["claimName"])

    def test_plain_servicelb_cannot_expose_restricted_nfs(self):
        rows = [{"name": "media", "pvc": "share-media", "nfs_clients": "192.0.2.0/24"}]
        pvc = {"spec": {"accessModes": ["ReadWriteMany"]}, "status": {"phase": "Bound"}}
        with mock.patch.object(server.SHARES, "_pvc", return_value=pvc), \
                mock.patch.object(server.PLATFORM, "detect", return_value={"load_balancer": "servicelb"}):
            with self.assertRaisesRegex(ValueError, "dedicated VIP"):
                server._nfs_deployment(rows, "")


class NfsRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.nodes = [{"metadata": {"name": f"node{i}", "labels": {
            "node-role.kubernetes.io/control-plane": "true", "node-role.kubernetes.io/etcd": "true",
            NFS.HOST_LABEL: "true"}}, "status": {"conditions": [{"type": "Ready", "status": "True"}]}}
            for i in range(1, 4)]
        self.volumes = [{"pvc": "media", "volume": {"metadata": {"name": "v1"},
                          "spec": {"numberOfReplicas": 2}}}]
        self.replicas = [{"spec": {"volumeName": "v1", "nodeID": f"node{i}", "healthyAt": "2026-09-26"},
                          "status": {"currentState": "running"}} for i in (1, 2)]
        self.platform = {"load_balancer": "kube-vip", "vip_service_election": True}

    def report(self):
        return NFS.recovery_report(self.nodes, self.volumes, self.replicas, self.platform, "delete-deployment-pod")

    def test_two_real_replica_hosts_and_quorum_still_do_not_claim_lock_recovery(self):
        report = self.report()
        self.assertEqual([], report["blockers"])
        self.assertEqual("limited", report["level"])
        self.assertFalse(report["lock_recovery"])

    def test_desired_two_replicas_are_not_evidence_of_two_healthy_copies(self):
        self.replicas.pop()
        self.assertTrue(any("healthy replicas are on 1 host" in b for b in self.report()["blockers"]))

    def test_down_host_is_not_counted_as_a_healthy_replica_or_standby(self):
        self.nodes[0]["status"]["conditions"][0]["status"] = "False"
        report = self.report()
        self.assertNotIn("node1", report["eligible_hosts"])
        self.assertEqual(["node2"], report["volumes"][0]["healthy_replica_nodes"])
        self.assertTrue(any("quorum" in issue for issue in report["blockers"]))

    def test_single_control_plane_cannot_be_reported_as_failover_ready(self):
        for node in self.nodes[1:]:
            node["metadata"]["labels"] = {NFS.HOST_LABEL: "true"}
        report = self.report()
        self.assertTrue(any("control-plane" in issue for issue in report["blockers"]))
        self.assertTrue(any("quorum" in issue for issue in report["blockers"]))

    def test_kernel_support_and_taints_are_required_on_replacement_hosts(self):
        self.nodes[1]["metadata"]["labels"].pop(NFS.HOST_LABEL)
        self.nodes[2]["spec"] = {"taints": [{"key": "dedicated", "effect": "NoSchedule"}]}
        report = self.report()
        self.assertEqual(["node1"], report["eligible_hosts"])
        self.assertTrue(any("replacement host" in issue for issue in report["blockers"]))


if __name__ == "__main__":
    unittest.main()
