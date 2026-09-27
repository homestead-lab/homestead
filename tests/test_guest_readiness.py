import datetime
import hashlib
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
import homestead_guest_readiness as guest
import homestead_k3s_health as health

NOW = 1800000000


def condition(kind, status="True"):
    return {"type": kind, "status": status}


def obj(kind, name, namespace="", **fields):
    return {"kind": kind, "metadata": {"name": name, "namespace": namespace, "uid": name + "-uid", "generation": 1}, **fields}


def lease(name, uid=None):
    value = obj("Lease", name, "kube-node-lease", spec={"holderIdentity": name,
        "renewTime": datetime.datetime.fromtimestamp(NOW, datetime.timezone.utc).isoformat()})
    value["metadata"]["ownerReferences"] = [{"kind": "Node", "uid": uid or name + "-uid"}]
    return value


class GuestReadinessTests(unittest.TestCase):
    def setUp(self):
        built = {"nodes": [{"name": "server-1", "role": "server", "address": "192.0.2.20"},
                           {"name": "agent-1", "role": "agent", "address": "192.0.2.21"}], "setup": "k3s", "kubevirt": False}
        self.bundle = health.prepare("lab", built, "a" * 32)
        self.config = self.bundle["nodes"][0]["config"]
        nodes, leases = [], []
        for row in self.config["members"]:
            node = obj("Node", row["name"], status={"conditions": [condition("Ready")],
                "nodeInfo": {"systemUUID": row["uuid"]}, "addresses": [{"type": "InternalIP", "address": row["address"]}]})
            node["metadata"]["labels"] = {"node-role.kubernetes.io/control-plane": "true"} if row["role"] == "server" else {}
            nodes.append(node)
            leases.append(lease(row["name"]))
        self.responses = {"/readyz": True, "/api/v1/nodes?limit=100": {"kind": "NodeList", "items": nodes},
                          "/apis/coordination.k8s.io/v1/namespaces/kube-node-lease/leases?limit=100": {"kind": "LeaseList", "items": leases}}
        self.nodes, self.leases = nodes, leases
        self.uid = self.bundle["nodes"][0]["uuid"]
        self.active = mock.Mock(return_value=True)

    def check(self, **kwargs):
        guest.check(self.config, self.responses.__getitem__, self.uid, self.active, now=NOW, **kwargs)

    def test_complete_guest_topology_and_active_local_service(self):
        self.check()
        self.active.assert_called_once_with("k3s")

    def test_typed_lists_allow_omitted_item_typemeta_but_not_contradictory_types(self):
        for row in self.nodes + self.leases:
            row.pop("kind")
        self.check()
        self.nodes[0]["kind"] = "Pod"
        with self.assertRaises(guest.NotReady):
            self.check()

    def test_wrong_uuid_ip_role_readiness_and_identity_are_rejected(self):
        for mutation in (
            lambda: self.nodes[1]["status"]["nodeInfo"].update(systemUUID=self.uid),
            lambda: self.nodes[1]["status"].update(addresses=[]),
            lambda: self.nodes[1]["metadata"].update(labels={"node-role.kubernetes.io/master": ""}),
            lambda: self.nodes[1]["status"].update(conditions=[condition("Ready", "False")]),
            lambda: self.nodes[1]["metadata"].update(uid=""),
            lambda: self.nodes[1]["metadata"].update(deletionTimestamp="now"),
        ):
            with self.subTest(mutation=mutation):
                self.setUp()
                mutation()
                with self.assertRaises(guest.NotReady):
                    self.check()

    def test_missing_duplicate_unexpected_or_paginated_nodes_fail(self):
        for nodes, metadata in ((self.nodes[:1], {}), (self.nodes + self.nodes[:1], {}),
                               (self.nodes + [obj("Node", "stranger")], {}), (self.nodes, {"continue": "more"})):
            with self.subTest(nodes=nodes, metadata=metadata):
                self.responses["/api/v1/nodes?limit=100"] = {"kind": "NodeList", "items": nodes, "metadata": metadata}
                with self.assertRaises(guest.NotReady):
                    self.check()

    def test_missing_stale_future_wrong_owner_or_holder_lease_fail(self):
        for delta in (-91, 31):
            self.leases[1]["spec"]["renewTime"] = datetime.datetime.fromtimestamp(NOW + delta, datetime.timezone.utc).isoformat()
            with self.assertRaises(guest.NotReady):
                self.check()
        for field, value in (("holderIdentity", "wrong"), ("renewTime", "2027-01-01T00:00:00")):
            self.setUp()
            self.leases[1]["spec"][field] = value
            with self.assertRaises(guest.NotReady):
                self.check()
        self.setUp()
        self.leases[1]["metadata"]["ownerReferences"][0]["uid"] = "replacement"
        with self.assertRaises(guest.NotReady):
            self.check()

    def test_agent_requires_own_uuid_and_service_but_no_exported_credentials(self):
        cfg = self.bundle["nodes"][1]["config"]
        guest.check(cfg, None, cfg["members"][1]["uuid"], self.active)
        self.active.assert_called_once_with("k3s-agent")
        with self.assertRaises(guest.NotReady):
            guest.check(cfg, None, self.uid, self.active)

    def test_requested_components_need_observed_current_generation_and_rollout(self):
        deployment = obj("Deployment", "homestead", "lab", spec={"replicas": 1},
                         status={"observedGeneration": 1, "updatedReplicas": 1, "availableReplicas": 1})
        self.config["setup"] = "local"
        self.responses["/apis/apps/v1/namespaces/lab/deployments/homestead"] = deployment
        self.check()
        for field, bad in (("observedGeneration", 0), ("updatedReplicas", 0), ("availableReplicas", 0), ("unavailableReplicas", 1)):
            with self.subTest(field=field):
                previous = deployment["status"].get(field)
                deployment["status"][field] = bad
                with self.assertRaises(guest.NotReady):
                    self.check()
                if previous is None:
                    deployment["status"].pop(field)
                else:
                    deployment["status"][field] = previous

    def test_longhorn_requires_manager_on_all_expected_nodes_and_deployer_ready(self):
        self.config["setup"] = "homestead"
        for ns, name in (("lab", "homestead"), ("longhorn-system", "longhorn-driver-deployer")):
            self.responses[f"/apis/apps/v1/namespaces/{ns}/deployments/{name}"] = obj("Deployment", name, ns,
                spec={"replicas": 1}, status={"observedGeneration": 1, "updatedReplicas": 1, "availableReplicas": 1})
        manager = obj("DaemonSet", "longhorn-manager", "longhorn-system", status={"observedGeneration": 1,
            "desiredNumberScheduled": 2, "updatedNumberScheduled": 2, "numberReady": 2})
        self.responses["/apis/apps/v1/namespaces/longhorn-system/daemonsets/longhorn-manager"] = manager
        self.check()
        manager["status"]["numberReady"] = 1
        with self.assertRaises(guest.NotReady):
            self.check()

    def test_kubevirt_and_cdi_use_their_actual_distinct_operator_status_fields(self):
        kv = obj("KubeVirt", "kubevirt", "kubevirt", status={"phase": "Deployed", "conditions": [condition("Available")],
            "observedGeneration": 1, "targetDeploymentID": "id", "observedDeploymentID": "id"})
        cdi = obj("CDI", "cdi", status={"phase": "Deployed", "conditions": [condition("Available")],
            "observedVersion": "v1", "targetVersion": "v1"})
        guest.operator(kv, "KubeVirt", "kubevirt")
        guest.operator(cdi, "CDI", "cdi")
        for resource, kind, key in ((kv, "KubeVirt", "observedDeploymentID"), (cdi, "CDI", "observedVersion")):
            resource["status"][key] = "old"
            with self.assertRaises(guest.NotReady):
                guest.operator(resource, kind, resource["metadata"]["name"])

    def test_bootstrap_receipt_does_not_freeze_future_cluster_topology(self):
        with tempfile.TemporaryDirectory() as directory:
            marker = Path(directory) / "verified"
            run = lambda: guest.verify(self.config, self.responses.__getitem__, self.uid, self.active, marker, now=NOW)
            run()
            self.assertEqual(hashlib.sha256(guest.canonical(self.config)).hexdigest(), marker.read_text())
            self.responses["/api/v1/nodes?limit=100"]["items"] = self.nodes[:1] + [obj("Node", "later-worker")]
            run()  # installation was verified; now check this server/API/lease
            self.responses["/readyz"] = False
            with self.assertRaises(guest.NotReady):
                run()
            self.responses["/readyz"] = True
            self.config["setup"] = "local"  # changed contract cannot reuse marker
            with self.assertRaises(guest.NotReady):
                run()

    def test_failed_bootstrap_never_leaves_completion_receipt(self):
        self.responses["/readyz"] = False
        with tempfile.TemporaryDirectory() as directory:
            marker = Path(directory) / "verified"
            with self.assertRaises(guest.NotReady):
                guest.verify(self.config, self.responses.__getitem__, self.uid, self.active, marker, now=NOW)
            self.assertFalse(marker.exists())

    def test_completion_marker_does_not_bypass_service_or_own_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            marker = Path(directory) / "verified"
            guest.verify(self.config, self.responses.__getitem__, self.uid, self.active, marker, now=NOW)
            self.active.return_value = False
            with self.assertRaises(guest.NotReady):
                guest.verify(self.config, self.responses.__getitem__, self.uid, self.active, marker, now=NOW)


class GuestTransportTests(unittest.TestCase):
    def test_tls_uses_local_ca_client_cert_disables_proxies_and_redirects(self):
        response = mock.MagicMock(status=200)
        response.__enter__.return_value = response
        response.geturl.return_value = "https://127.0.0.1:6443/readyz"
        response.read.return_value = b"ok\n"
        with mock.patch.object(guest.ssl, "create_default_context") as tls, mock.patch.object(guest.urllib.request, "build_opener") as opener:
            opener.return_value.open.return_value = response
            read = guest.reader()
            self.assertTrue(read("/readyz"))
            tls.assert_called_once_with(cafile=guest.TLS + "server-ca.crt")
            tls.return_value.load_cert_chain.assert_called_once_with(guest.TLS + "client-admin.crt", guest.TLS + "client-admin.key")
            proxy, redirect, https = opener.call_args.args
            self.assertEqual({}, proxy.proxies)
            self.assertIsInstance(redirect, guest.NoRedirect)
            self.assertIs(https._context, tls.return_value)
            opener.return_value.open.assert_called_once_with("https://127.0.0.1:6443/readyz", timeout=2)
            response.read.assert_called_once_with(guest.MAX_RESPONSE + 1)
            with self.assertRaises(guest.NotReady):
                redirect.redirect_request(None, None, None, None, None, None)
            with self.assertRaises(guest.NotReady):
                read("//elsewhere/")

    def test_changed_endpoint_oversized_response_or_tls_error_never_pass(self):
        response = mock.MagicMock(status=200)
        response.__enter__.return_value = response
        response.geturl.return_value = "https://wrong/readyz"
        with mock.patch.object(guest.ssl, "create_default_context"), mock.patch.object(guest.urllib.request, "build_opener") as opener:
            opener.return_value.open.return_value = response
            read = guest.reader()
            with self.assertRaises(guest.NotReady):
                read("/readyz")
            response.geturl.return_value = "https://127.0.0.1:6443/readyz"
            response.read.return_value = b"x" * (guest.MAX_RESPONSE + 1)
            with self.assertRaises(guest.NotReady):
                read("/readyz")
            opener.return_value.open.side_effect = guest.ssl.SSLError("untrusted")
            with self.assertRaises(guest.ssl.SSLError):
                read("/readyz")

    def test_main_errors_are_redacted_and_deadline_cleared(self):
        with mock.patch.object(guest.signal, "SIGALRM", 14, create=True), mock.patch.object(guest.signal, "signal"), \
                mock.patch.object(guest.signal, "alarm", create=True) as alarm, \
                mock.patch.object(guest.Path, "read_bytes", side_effect=ValueError("private-credential")), \
                mock.patch("sys.stdout", new_callable=io.StringIO) as out:
            self.assertEqual(1, guest.main(["a" * 64]))
            self.assertNotIn("private-credential", out.getvalue())
            self.assertEqual([mock.call(23), mock.call(0)], alarm.call_args_list)


if __name__ == "__main__":
    unittest.main()
