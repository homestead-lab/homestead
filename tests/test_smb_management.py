"""Managed SMB rename and mutation guards, without a live cluster."""
import copy
import sys
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
import server


class SmbMigrationTests(unittest.TestCase):
    def setUp(self):
        self.namespace = server.SMB_NAMESPACE
        self.old_dep = server._smb_path("deployments", "samba")
        self.new_dep = server._smb_path("deployments", server.SMB_NAME)
        self.old_svc = server._smb_path("services", "samba")
        self.new_svc = server._smb_path("services", server.SMB_NAME)
        self.objects = {
            self.old_dep: {
                "apiVersion": "apps/v1", "kind": "Deployment",
                "metadata": {"name": "samba", "namespace": self.namespace, "generation": 1},
                "spec": {"replicas": 1, "selector": {"matchLabels": {"app": "samba"}},
                         "template": {"metadata": {"labels": {"app": "samba"}}, "spec": {
                             "containers": [{"name": "samba", "image": "dperson/samba:latest",
                                             "args": ["-p", "-s", "media;/shares/media;yes;no;no;lab"],
                                             "volumeMounts": [{"name": "manual", "mountPath": "/shares/media"}]}],
                             "volumes": [{"name": "manual", "persistentVolumeClaim": {"claimName": "share-media"}}]}}},
                "status": {"readyReplicas": 1, "updatedReplicas": 1, "observedGeneration": 1}},
            self.old_svc: {
                "apiVersion": "v1", "kind": "Service",
                "metadata": {"name": "samba", "namespace": self.namespace},
                "spec": {"type": "LoadBalancer", "selector": {"app": "samba"},
                         "clusterIP": "10.43.1.2", "ports": [{"name": "smb", "port": 445,
                                                               "targetPort": 445, "nodePort": 30445}]},
                "status": {"loadBalancer": {"ingress": [{"ip": "192.0.2.245"}]}}}}
        self.sent = []

    def get(self, path, **_kwargs):
        if path not in self.objects:
            raise urllib.error.HTTPError(path, 404, "not found", {}, None)
        return copy.deepcopy(self.objects[path])

    def send(self, method, path, body=None, **_kwargs):
        self.sent.append((method, path, copy.deepcopy(body)))
        if method == "POST":
            path += "/" + body["metadata"]["name"]
        if method == "DELETE":
            self.objects.pop(path, None)
            return {}
        if method == "PATCH":
            obj = self.objects[path]
            obj["spec"].update(body["spec"])
            if path == self.new_dep and obj["spec"]["replicas"]:
                obj["status"] = {"readyReplicas": 1, "updatedReplicas": 1,
                                 "observedGeneration": 1}
            return copy.deepcopy(obj)
        self.objects[path] = copy.deepcopy(body)
        return copy.deepcopy(body)

    def test_legacy_workload_is_recreated_with_same_vip_and_canonical_mounts(self):
        rows = [{"name": "media", "pvc": "share-media", "path": "/shares/media",
                 "user": "lab", "public": False}]
        creds = {"media": {"user": "lab", "password": "pw"}}
        with mock.patch.object(server, "kget", self.get), \
                mock.patch.object(server, "ksend", self.send), \
                mock.patch.object(server.SHARES, "_state", return_value=(rows, creds, None, None, None)):
            server.install_samba()
        self.assertNotIn(self.old_dep, self.objects)
        self.assertNotIn(self.old_svc, self.objects)
        dep = self.objects[self.new_dep]
        self.assertEqual(server.SMB_NAME, dep["spec"]["template"]["spec"]["containers"][0]["name"])
        self.assertEqual(["/shares/media"], [m["mountPath"] for m in
                          dep["spec"]["template"]["spec"]["containers"][0]["volumeMounts"]])
        self.assertEqual("192.0.2.245", self.objects[self.new_svc]["metadata"]["annotations"]
                         .get("kube-vip.io/loadbalancerIPs"))
        self.assertNotIn("clusterIP", self.objects[self.new_svc]["spec"])

    def test_failed_cutover_restores_the_legacy_service(self):
        def fail_new_service(method, path, body=None, **kwargs):
            if method == "POST" and path.endswith("/services") and body["metadata"]["name"] == server.SMB_NAME:
                raise ValueError("service rejected")
            return self.send(method, path, body, **kwargs)

        with mock.patch.object(server, "kget", self.get), \
                mock.patch.object(server, "ksend", fail_new_service), \
                mock.patch.object(server.SHARES, "_state", return_value=([], {}, None, None, None)):
            with self.assertRaisesRegex(ValueError, "service rejected"):
                server.install_samba()
        self.assertIn(self.old_dep, self.objects)
        self.assertIn(self.old_svc, self.objects)
        self.assertNotIn(self.new_dep, self.objects)
        self.assertNotIn(self.new_svc, self.objects)

    def test_status_detects_a_duplicate_mount_even_when_share_names_match(self):
        rows = [{"name": "media", "pvc": "share-media", "path": "/shares/media",
                 "user": "lab", "public": False}]
        creds = {"media": {"user": "lab", "password": "pw"}}
        dep = server._smb_new_object(self.objects[self.old_dep], server.SMB_NAME)
        dep = server.SHARES.configured_deployment(dep, rows, creds)
        container = dep["spec"]["template"]["spec"]["containers"][0]
        container["volumeMounts"].append({"name": "manual", "mountPath": "/shares/media"})
        dep["spec"]["template"]["spec"]["volumes"].append(
            {"name": "manual", "persistentVolumeClaim": {"claimName": "share-media"}})
        self.objects[self.new_dep] = dep
        with mock.patch.object(server, "kget", self.get), \
                mock.patch.object(server.SHARES, "_state", return_value=(rows, creds, None, None, dep)):
            state = server.samba_state()
        self.assertEqual(["media"], state["served_shares"])
        self.assertFalse(state["in_sync"])

    def test_general_workload_mutations_reject_managed_names(self):
        for name in ("samba", server.SMB_NAME):
            with self.assertRaisesRegex(ValueError, "Network Shares"):
                server.guard_managed_smb(self.namespace, name)
            with self.assertRaisesRegex(ValueError, "Network Shares"):
                server.guard_smb_object("Deployment", self.namespace, name)
            with self.assertRaisesRegex(ValueError, "Network Shares"):
                server.guard_smb_object("Service", self.namespace, name)
        for kind, name in (("ConfigMap", server.SHARES.CONFIGMAP()),
                           ("Secret", server.SHARES.SECRET())):
            with self.assertRaisesRegex(ValueError, "Network Shares"):
                server.guard_smb_object(kind, self.namespace, name)

    def test_disabling_and_removing_smb_never_delete_share_volumes(self):
        pvc_path = f"/api/v1/namespaces/{self.namespace}/persistentvolumeclaims/share-media"
        config_path = f"/api/v1/namespaces/{self.namespace}/configmaps/{server.SHARES.CONFIGMAP()}"
        secret_path = f"/api/v1/namespaces/{self.namespace}/secrets/{server.SHARES.SECRET()}"
        self.objects[self.new_dep] = server._smb_new_object(self.objects[self.old_dep], server.SMB_NAME)
        self.objects[self.new_svc] = server._smb_new_object(self.objects[self.old_svc], server.SMB_NAME)
        for path in (pvc_path, config_path, secret_path):
            self.objects[path] = {"metadata": {"name": path.rsplit("/", 1)[1]}}
        with mock.patch.object(server, "kget", self.get), \
                mock.patch.object(server, "ksend", self.send), \
                mock.patch.object(server, "samba_state", return_value={
                    "installed": True, "enabled": True, "name": server.SMB_NAME}):
            server.set_samba(False)
            self.assertEqual(0, self.objects[self.new_dep]["spec"]["replicas"])
            server.remove_samba()
        self.assertNotIn(self.new_dep, self.objects)
        self.assertNotIn(self.new_svc, self.objects)
        for path in (pvc_path, config_path, secret_path):
            self.assertIn(path, self.objects)
        self.assertFalse(any(method == "DELETE" and
                             ("/persistentvolumeclaims/" in path or "/configmaps/" in path or "/secrets/" in path)
                             for method, path, _ in self.sent))


if __name__ == "__main__":
    unittest.main()
