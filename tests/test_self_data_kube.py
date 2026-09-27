import io
import json
import os
import ssl
import sys
import tempfile
import unittest
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.environ.get("HOMESTEAD_TEST_SERVER_DIR", str(Path(__file__).resolve().parents[1] / "server")))
import homestead_self_data_kube as K
import homestead_self_data_worker as W
from homestead_storage_journal import Held
from test_self_data_coordinator import Cluster, OP


def scope():
    return K.Scope("lab", "homestead", OP, ["source", "target"], ["old-pv", "new-pv"], ["node1"])


class Response(io.BytesIO):
    def __init__(self, value, url, code=200):
        super().__init__(value if isinstance(value, bytes) else json.dumps(value).encode())
        self.url, self.status = url, code

    def geturl(self):
        return self.url


class ClientTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        self.token_path = Path(temp.name) / "token"
        self.token_path.write_text("fixture.token.one", encoding="ascii")
        self.scope = scope()
        with mock.patch.object(K.ssl, "create_default_context", return_value=ssl.create_default_context()):
            self.client = K.Client(self.scope, token_path=str(self.token_path), ca_path="test-ca")
        self.open = mock.Mock(side_effect=lambda req, **kwargs: Response({"ok": True}, req.full_url))
        self.client.opener.open = self.open

    def test_requests_use_fixed_origin_verified_tls_no_proxy_or_redirect(self):
        context = ssl.create_default_context()
        with mock.patch.object(K.ssl, "create_default_context", return_value=context) as tls, \
                mock.patch.object(K.urllib.request, "build_opener") as build:
            K.Client(self.scope, ca_path="trusted-cluster-ca")
        tls.assert_called_once_with(cafile="trusted-cluster-ca")
        self.assertEqual(ssl.CERT_REQUIRED, context.verify_mode)
        self.assertTrue(context.check_hostname)
        proxy, https, redirect = build.call_args.args
        self.assertEqual({}, proxy.proxies)
        self.assertIsInstance(https, urllib.request.HTTPSHandler)
        self.assertIsNone(redirect.redirect_request(None, None, 302, "", {}, "https://elsewhere.invalid/"))
        self.client.read("/api/v1/nodes")
        request = self.open.call_args.args[0]
        self.assertEqual(K.ORIGIN + "/api/v1/nodes", request.full_url)
        self.assertEqual(15, self.open.call_args.kwargs["timeout"])

    def test_missing_ca_never_falls_back_to_unverified_connection(self):
        with mock.patch.object(K.ssl, "create_default_context", side_effect=FileNotFoundError()), \
                mock.patch.object(K.urllib.request, "build_opener") as build:
            with self.assertRaises(FileNotFoundError): K.Client(self.scope)
        build.assert_not_called()

    def test_rotated_token_is_reopened_for_each_request(self):
        self.client.read("/api/v1/nodes")
        first = self.open.call_args.args[0].get_header("Authorization")
        self.token_path.write_text("fixture.token.two", encoding="ascii")
        self.client.read("/api/v1/nodes")
        second = self.open.call_args.args[0].get_header("Authorization")
        self.assertEqual("Bearer fixture.token.one", first)
        self.assertEqual("Bearer fixture.token.two", second)

    def test_out_of_scope_paths_are_rejected_before_reading_token(self):
        paths = ["https://elsewhere.invalid/", "//elsewhere.invalid/", "/api/v1/secrets",
                 "/api/v1/namespaces/lab/secrets/auth", "/api/v1/nodes?watch=1",
                 "/api/v1/nodes/%2e%2e/secrets", "/api/v1/namespaces/lab/pods/x/exec",
                 "/api/v1/namespaces/other/pods/x", "/api/v1/persistentvolumes/unreviewed",
                 "/apis/coordination.k8s.io/v1/namespaces/kube-node-lease/leases/unreviewed"]
        with mock.patch("builtins.open", side_effect=AssertionError("must validate first")):
            for path in paths:
                with self.subTest(path=path), self.assertRaises(Held): self.client.read(path)
        self.open.assert_not_called()

    def test_missing_invalid_or_oversized_token_never_sends(self):
        for token in ("", "header\r\ninjection", "x" * 16385):
            self.token_path.write_text(token, encoding="ascii")
            with self.assertRaises(Held): self.client.read("/api/v1/nodes")
        self.token_path.unlink()
        with self.assertRaises(Held): self.client.read("/api/v1/nodes")
        self.open.assert_not_called()

    def test_http_error_keeps_code_but_discards_body_credentials_and_headers(self):
        for code in (301, 401, 403, 404, 409, 422, 500):
            error = urllib.error.HTTPError("https://private.invalid/", code, "private diagnostic", {"Secret": "private"}, io.BytesIO(b"private body"))
            self.open.side_effect = error
            with self.assertRaises(urllib.error.HTTPError) as caught:
                self.client.read("/api/v1/nodes")
            self.assertEqual(code, caught.exception.code)
            self.assertNotIn("private", str(caught.exception))
            self.assertEqual({}, caught.exception.headers)

    def test_transport_and_parse_failures_are_sanitized_without_retry(self):
        for failure in (TimeoutError("private endpoint"), ValueError("private JSON")):
            self.open.reset_mock(); self.open.side_effect = failure
            with self.assertRaises(Held) as caught: self.client.read("/api/v1/nodes")
            self.assertNotIn("private", str(caught.exception))
            self.open.assert_called_once()
        self.open.side_effect = lambda req, **kw: Response([], req.full_url)
        with self.assertRaises(Held): self.client.read("/api/v1/nodes")

    def test_response_from_another_origin_is_never_trusted(self):
        self.open.side_effect = lambda *_args, **_kw: Response({}, "https://elsewhere.invalid/")
        with self.assertRaises(Held): self.client.read("/api/v1/nodes")

    def test_copy_logs_are_bounded_and_cannot_read_other_containers(self):
        path = "/api/v1/namespaces/lab/pods/copy-pod/log?container=copy&tailLines=20"
        self.open.side_effect = lambda req, **kw: Response(b"x" * 65537, req.full_url)
        with self.assertRaisesRegex(Held, "limit"): self.client.logs(path)
        self.open.reset_mock()
        with self.assertRaises(Held): self.client.logs(path.replace("container=copy", "container=homestead"))
        with self.assertRaises(Held): self.client.read(path)
        self.open.assert_not_called()

    def test_delete_requires_exact_job_preconditions_and_foreground_without_force(self):
        good = {"preconditions": {"uid": "copy-uid", "resourceVersion": "12"}, "propagationPolicy": "Foreground"}
        self.client.send("DELETE", self.scope.job, good)
        for path, body in ((self.scope.job, {}), (self.scope.job, {**good, "gracePeriodSeconds": 0}),
                           (self.scope.job, {**good, "propagationPolicy": "Background"}),
                           (self.scope.core + "/pods/old-0", good), (self.scope.core + "/persistentvolumeclaims/source", good)):
            self.open.reset_mock()
            with self.assertRaises(Held): self.client.send("DELETE", path, body)
            self.open.assert_not_called()

    def test_mutations_cannot_change_target_identity_or_use_overrides(self):
        original = {"kind": "Deployment", "apiVersion": "apps/v1", "metadata": {
            "namespace": "lab", "name": "homestead", "uid": "deployment-uid", "resourceVersion": "1"}}
        self.client.send("PUT", self.scope.dep, original)
        for field, value in (("namespace", "other"), ("name", "other"), ("uid", ""), ("resourceVersion", "")):
            body = {**original, "metadata": {**original["metadata"], field: value}}
            self.open.reset_mock()
            with self.assertRaises(Held): self.client.send("PUT", self.scope.dep, body)
            self.open.assert_not_called()
        with self.assertRaises(Held): self.client.send("PATCH", self.scope.dep, original)
        with self.assertRaises(Held): self.client.send("PUT", self.scope.dep, original, timeout=999)
        with self.assertRaises(Held): self.client.send("POST", self.scope.jobs, [])

    def test_namespace_selector_reads_have_matching_rbac_without_secret_access(self):
        path = "/api/v1/namespaces/another-workload-namespace"
        self.client.read(path)
        self.assertTrue(granted(K.access_resources(self.scope), "GET", path))

    def test_full_phase_engine_uses_transport_and_generated_rbac_without_data_access(self):
        c = Cluster()
        calls = []
        def dispatch(req, **kw):
            path = urllib.parse.urlsplit(req.full_url).path
            method = req.get_method()
            calls.append((method, path))
            if path.endswith("/log"):
                result = c.logs_text.encode()
            elif method == "GET":
                result = c.read(path)
            else:
                result = c.send(method, path, json.loads(req.data))
            return Response(result, req.full_url)
        self.open.side_effect = dispatch
        runner = W.Runner(self.client.read, self.client.send, self.client.logs, c.admit,
            namespace="lab", deployment="homestead", operation=OP, anchor_uid=c.handle["uid"], worker_uid="coordinator-uid", clock=lambda: 1000)
        self.assertEqual("quiesce", runner.tick()["phase"])
        c.settle_stop(); runner.tick(); runner.tick(); c.finish_copy(); runner.tick(); runner.tick()
        c.objects.pop("/api/v1/namespaces/lab/pods/copy-pod")
        runner.tick(); runner.tick(); c.settle_stop(); runner.tick(); c.settle_start()
        self.assertEqual("done", runner.tick()["status"])
        for method, path in calls:
            with self.subTest(method=method, path=path):
                self.assertTrue(granted(K.access_resources(self.scope), method, path))


def granted(resources, method, path):
    parts = path.strip("/").split("/")
    if parts[0] == "api":
        group, parts = "", parts[2:]
    else:
        group, parts = parts[1], parts[3:]
    ns = None
    if len(parts) >= 3 and parts[0] == "namespaces":
        ns, parts = parts[1], parts[2:]
    plural = parts[0]
    name = parts[1] if len(parts) > 1 else None
    if len(parts) > 2: plural += "/" + parts[2]
    verb = {"GET": "get" if name else "list", "POST": "create", "PUT": "update", "DELETE": "delete"}[method]
    for role in resources:
        if role["kind"] not in ("Role", "ClusterRole"): continue
        if role["kind"] == "Role" and role["metadata"].get("namespace") != ns: continue
        for rule in role["rules"]:
            if (group in rule["apiGroups"] and plural in rule["resources"] and verb in rule["verbs"]
                    and ("resourceNames" not in rule or name in rule["resourceNames"])):
                return True
    return False


class AccessTests(unittest.TestCase):
    def test_no_secret_exec_pvc_delete_or_cluster_write_authority(self):
        resources = K.access_resources(scope())
        for method, path in (("GET", "/api/v1/namespaces/lab/secrets/auth"), ("GET", "/api/v1/secrets"),
            ("POST", "/api/v1/namespaces/lab/pods/x/exec"), ("DELETE", "/api/v1/namespaces/lab/persistentvolumeclaims/source"),
            ("PUT", "/api/v1/nodes/node1"), ("PUT", "/apis/apps/v1/namespaces/lab/deployments/other"),
            ("POST", "/apis/batch/v1/namespaces/other/jobs"), ("PUT", "/api/v1/namespaces/lab/configmaps/other")):
            self.assertFalse(granted(resources, method, path), path)
        for role in resources:
            self.assertNotIn("ownerReferences", role["metadata"])
            for rule in role.get("rules", []):
                self.assertNotIn("*", json.dumps(rule))

    def test_bindings_only_target_dedicated_operation_account(self):
        resources = K.access_resources(scope())
        account = resources[0]
        self.assertFalse(account["automountServiceAccountToken"])
        for resource in resources:
            if resource["kind"].endswith("Binding"):
                self.assertEqual([{"kind": "ServiceAccount", "namespace": "lab", "name": account["metadata"]["name"]}], resource["subjects"])
                expected = account["metadata"]["name"] + ("-leases" if resource["metadata"].get("namespace") == "kube-node-lease" else "")
                self.assertEqual(expected, resource["roleRef"]["name"])
        other = K.Scope("other", "homestead", OP, ["source", "target"], ["old-pv", "new-pv"], ["node1"])
        self.assertNotEqual(account["metadata"]["name"], K.access_resources(other)[0]["metadata"]["name"])

    def test_lease_and_application_roles_never_collide(self):
        unusual = K.Scope("kube-node-lease", "homestead", OP, ["source", "target"], ["old-pv", "new-pv"], ["node1"])
        resources = K.access_resources(unusual)
        identities = [(r["kind"], r["metadata"].get("namespace"), r["metadata"]["name"]) for r in resources]
        self.assertEqual(len(identities), len(set(identities)))

    def test_job_create_limit_is_honest_and_deletes_are_name_bound(self):
        resources = K.access_resources(scope())
        self.assertTrue(granted(resources, "POST", "/apis/batch/v1/namespaces/lab/jobs"))
        self.assertFalse(granted(resources, "DELETE", "/apis/batch/v1/namespaces/lab/jobs/other"))
        create = next(rule for role in resources for rule in role.get("rules", []) if "create" in rule["verbs"])
        self.assertNotIn("resourceNames", create)
        with self.assertRaises(Held):
            scope().check("POST", scope().jobs, {"kind": "Job", "apiVersion": "batch/v1", "metadata": {"namespace": "lab", "name": "other"}})

    def test_projection_has_no_application_secret_or_data_mount(self):
        volume = K.token_volume()
        self.assertNotIn("secret", json.dumps(volume).lower())
        self.assertNotIn("persistentVolumeClaim", volume)
        token = volume["projected"]["sources"][0]["serviceAccountToken"]
        self.assertEqual(600, token["expirationSeconds"])
        self.assertNotIn("audience", token)
        self.assertEqual(0o440, volume["projected"]["defaultMode"])


if __name__ == "__main__":
    unittest.main()
