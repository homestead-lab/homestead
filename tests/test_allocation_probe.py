import base64
import copy
import http.client
import json
import sys
import threading
import time
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "server"))
sys.path.insert(0, str(ROOT / "server/probe"))
import homestead_allocation_auth as auth
import homestead_allocation_probe as probe
import homestead_probe
import allocation_http as helper

KEY = "ab" * 32
BOOT = "12345678-1234-1234-1234-123456789abc"


class AllocationAuthenticationTests(unittest.TestCase):
    def test_request_response_and_nonce_domains_are_distinct(self):
        request = auth.encode(auth.request("node", "pod"))
        response = b'{"complete":true}'
        signature = auth.signature(KEY, response, reply_to=request)
        self.assertTrue(auth.verify(KEY, response, signature, reply_to=request))
        self.assertFalse(auth.verify(KEY, response, signature))
        self.assertFalse(auth.verify(KEY, response + b" ", signature, reply_to=request))
        self.assertFalse(auth.verify(KEY, response, signature, reply_to=request + b" "))
        self.assertFalse(auth.verify("bad", response, signature, reply_to=request))

    def test_identity_age_shapes_and_exact_nonce(self):
        valid = auth.request("node", "pod", now=100)
        self.assertTrue(auth.validate(valid, "node", "pod", now=100))
        for changes in ({"node": "elsewhere"}, {"pod_uid": "replaced"}, {"time": 69},
                        {"time": 131}, {"time": True}, {"nonce": "short"}, {"extra": 1}):
            self.assertFalse(auth.validate(dict(valid, **changes), "node", "pod", now=100))


class AllocationHTTPTests(unittest.TestCase):
    def setUp(self):
        for name, value in (("NODE", "node"), ("POD_UID", "pod")):
            patch = mock.patch.object(helper, name, value)
            patch.start(); self.addCleanup(patch.stop)
        for name, value in (("read_key", KEY), ("boot_id", BOOT)):
            patch = mock.patch.object(helper, name, return_value=value)
            setattr(self, name, patch.start()); self.addCleanup(patch.stop)
        patch = mock.patch.object(helper.allocation, "snapshot", return_value={"schema": 1, "complete": True, "sampled_at": time.time()})
        self.snapshot = patch.start(); self.addCleanup(patch.stop)
        self.server = helper.Server(("127.0.0.1", 0))
        thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": .01}, daemon=True)
        thread.start()
        def cleanup():
            self.server.shutdown(); self.server.server_close(); thread.join(2)
        self.addCleanup(cleanup)

    def call(self, value=None, *, raw=None, supplied=None, path="/snapshot", method="POST", headers=None):
        raw = raw if raw is not None else auth.encode(value or auth.request("node", "pod"))
        connection = http.client.HTTPConnection(*self.server.server_address, timeout=3)
        try:
            connection.request(method, path, body=raw,
                headers=headers or {"X-Homestead-Allocation": supplied or auth.signature(KEY, raw)})
            reply = connection.getresponse()
            return reply.status, reply.read(), reply.getheader("X-Homestead-Allocation"), raw
        finally:
            connection.close()

    def test_signed_request_and_response_bind_current_pod_and_boot(self):
        code, body, signature, raw = self.call()
        self.assertEqual(200, code)
        self.assertTrue(auth.verify(KEY, body, signature, reply_to=raw))
        self.assertEqual(BOOT, json.loads(body)["boot_id"])
        self.assertEqual("pod", json.loads(body)["pod_uid"])
        self.snapshot.assert_called_once_with()

    def test_wrong_signature_identity_age_and_replay_never_collect(self):
        request = auth.request("node", "pod")
        for kwargs in ({"supplied": "no"}, {"value": dict(request, pod_uid="other")},
                       {"value": dict(request, time=0)}, {"raw": b"x" * 1025},
                       {"raw": b"not json"}):
            self.assertEqual(403, self.call(**kwargs)[0])
        self.snapshot.assert_not_called()
        self.assertEqual(200, self.call(request)[0])
        self.assertEqual(403, self.call(request)[0])
        self.assertEqual(1, self.snapshot.call_count)

    def test_failed_collection_returns_signed_unknown_without_private_error(self):
        self.snapshot.side_effect = RuntimeError("secret/private-path")
        code, body, signature, raw = self.call()
        self.assertEqual(200, code)
        self.assertFalse(json.loads(body)["complete"])
        self.assertNotIn(b"private-path", body)
        self.assertTrue(auth.verify(KEY, body, signature, reply_to=raw))

    def test_boot_change_discards_allocation(self):
        self.boot_id.side_effect = [BOOT, "another"]
        self.assertFalse(json.loads(self.call()[1])["complete"])

    def test_other_paths_and_health_do_not_expose_allocations(self):
        self.assertEqual(404, self.call(method="GET", path="/snapshot")[0])
        self.assertEqual(200, self.call(method="GET", path="/healthz")[0])
        self.assertEqual(404, self.call(path="/snapshot?socket=/other")[0])
        self.snapshot.assert_not_called()

    def test_transfer_encoding_and_replay_cache_limit_refuse_work(self):
        self.assertEqual(403, self.call(headers={"Transfer-Encoding": "chunked"})[0])
        self.server.nonces = {str(i): time.time() for i in range(256)}
        self.assertEqual(403, self.call()[0])
        self.snapshot.assert_not_called()


class AllocationConfigurationTests(unittest.TestCase):
    def setUp(self):
        self.obj = homestead_probe.manifest("test")[1]
        self.obj["metadata"].update(uid="probe-uid", resourceVersion="10")
        self.secret = None
        self.sent = []
        self.capacity = mock.Mock(return_value={"blocked": False, "blockers": [], "warnings": [], "fingerprint": "capacity-1"})
        capacity_patch = mock.patch.object(probe, "capacity_check", self.capacity)
        capacity_patch.start(); self.addCleanup(capacity_patch.stop)
        patch_read = mock.patch.object(probe, "read", side_effect=self.read)
        patch_write = mock.patch.object(probe, "write", side_effect=self.write)
        patch_ns = mock.patch.object(probe, "NS", "lab")
        for patch in (patch_read, patch_write, patch_ns):
            patch.start(); self.addCleanup(patch.stop)
        self.body = {"enabled": True, "uid": "probe-uid", "resource_version": "10",
                     "directory": "/var/lib/kubelet/pod-resources", "acknowledge_host_access": True}

    def read(self, path):
        if "/daemonsets/" in path:
            return copy.deepcopy(self.obj)
        if "/secrets/" in path and self.secret:
            return copy.deepcopy(self.secret)
        raise urllib.error.HTTPError(path, 404, "missing", {}, None)

    def write(self, method, path, body, **kwargs):
        self.sent.append((method, path, copy.deepcopy(body), kwargs))
        if method == "POST":
            self.secret = copy.deepcopy(body)
            self.secret["metadata"].update(uid="key-uid", resourceVersion="1")
            return copy.deepcopy(self.secret)
        self.assertEqual("application/json-patch+json", kwargs["ctype"])
        self.assertEqual(self.obj["metadata"]["uid"], body[0]["value"])
        self.assertEqual(self.obj["metadata"]["resourceVersion"], body[1]["value"])
        self.obj["spec"]["template"] = body[2]["value"]
        self.obj["metadata"]["resourceVersion"] = str(int(self.obj["metadata"]["resourceVersion"]) + 1)
        return copy.deepcopy(self.obj)

    def test_disabled_by_default_and_enable_is_minimal_explicit_and_fenced(self):
        before = copy.deepcopy(self.obj["spec"]["template"]["spec"])
        self.assertFalse(probe.status()["enabled"])
        probe.configure(self.body, "9.9.9")
        spec = self.obj["spec"]["template"]["spec"]
        self.assertEqual(before["containers"], spec["containers"][:-1])
        self.assertEqual(before["volumes"], spec["volumes"][:-4])
        sidecar = spec["containers"][-1]
        self.assertEqual("ghcr.io/wjcloudy/homestead:9.9.9", sidecar["image"])
        self.assertNotIn("privileged", sidecar["securityContext"])
        self.assertFalse(sidecar["securityContext"]["allowPrivilegeEscalation"])
        self.assertEqual(["ALL"], sidecar["securityContext"]["capabilities"]["drop"])
        self.assertTrue(all(row["readOnly"] for row in sidecar["volumeMounts"]))
        self.assertEqual("Directory", spec["volumes"][-4]["hostPath"]["type"])
        self.assertEqual([{"key": "key", "path": "key"}], spec["volumes"][-3]["secret"]["items"])
        self.assertEqual("allocation-no-token", sidecar["volumeMounts"][-1]["name"])
        self.assertEqual("/var/run/secrets/kubernetes.io/serviceaccount", sidecar["volumeMounts"][-1]["mountPath"])
        self.assertEqual({"medium": "Memory", "sizeLimit": "64Ki"}, spec["volumes"][-1]["emptyDir"])
        self.assertTrue(probe.status()["enabled"])
        self.assertNotIn(base64.b64decode(self.secret["data"]["key"]).decode(), str(probe.status()))

    def test_disable_keeps_other_mounts_and_never_deletes_secret_or_volumes(self):
        self.obj["spec"]["template"]["spec"]["volumes"].append({"name": "user-pvc", "persistentVolumeClaim": {"claimName": "keep"}})
        before = copy.deepcopy(self.obj["spec"]["template"]["spec"])
        probe.configure(self.body, "1")
        probe.configure({"enabled": False, "uid": "probe-uid", "resource_version": "11"}, "1")
        self.assertEqual(before, self.obj["spec"]["template"]["spec"])
        self.assertIsNotNone(self.secret)
        self.assertFalse(any(row[0] == "DELETE" for row in self.sent))

    def test_stale_identity_or_missing_consent_never_writes(self):
        for changes in ({"uid": "old"}, {"resource_version": "9"}, {"acknowledge_host_access": False}, {"enabled": "true"}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                probe.configure(dict(self.body, **changes), "1")
        self.assertEqual([], self.sent)

    def test_only_dedicated_socket_directory_is_accepted(self):
        for value in ("/", "/var/lib/kubelet", "/pod-resources", "/etc/../pod-resources", "relative/pod-resources", "/a/pod-resources/", "/a//pod-resources"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                probe.configure(dict(self.body, directory=value), "1")
        self.assertEqual([], self.sent)
        self.assertEqual("/custom/k3s/pod-resources", probe.socket_directory("/custom/k3s/pod-resources"))

    def test_unmanaged_sidecar_or_foreign_auth_secret_is_not_replaced(self):
        self.obj["spec"]["template"]["spec"]["containers"].append({"name": "allocation"})
        with self.assertRaisesRegex(ValueError, "Unmanaged"):
            probe.configure(self.body, "1")
        self.obj["spec"]["template"]["spec"]["containers"].pop()
        self.secret = {"metadata": {"uid": "private"}, "data": {"key": "private"}}
        with self.assertRaisesRegex(ValueError, "another probe"):
            probe.configure(self.body, "1")
        self.assertEqual([], self.sent)

    def test_shared_mount_is_not_removed(self):
        probe.configure(self.body, "1")
        self.sent.clear()
        self.obj["spec"]["template"]["spec"]["containers"][0]["volumeMounts"].append({"name": "allocation-socket", "mountPath": "/extra"})
        with self.assertRaisesRegex(ValueError, "Another container"):
            probe.configure({"enabled": False, "uid": "probe-uid", "resource_version": "11"}, "1")
        self.assertEqual([], self.sent)

    def test_uncertain_write_is_not_retried_or_cleaned_up(self):
        with mock.patch.object(probe, "write", side_effect=TimeoutError("private-url")) as send:
            with self.assertRaisesRegex(ValueError, "not confirmed"):
                probe.configure(self.body, "1")
        self.assertEqual(1, send.call_count)

    def test_api_is_admin_only_and_calls_the_same_identity_fenced_configuration(self):
        import server
        for method in ("GET", "POST"):
            self.assertEqual("admin", server.needed_role("/api/node/probe/allocation", method))
            self.assertEqual("admin", server.needed_role("/api/node/probe/allocation/check", method))
        handler = object.__new__(server.H)
        handler.path, handler.headers = "/api/node/probe/allocation", {}
        handler._client_ip = lambda: "127.0.0.1"
        handler._guard = lambda path: False
        handler._body = lambda: copy.deepcopy(self.body)
        handler._send = mock.Mock()
        with mock.patch.object(probe, "read", side_effect=self.read), mock.patch.object(probe, "write", side_effect=self.write), mock.patch.object(probe, "capacity_check", self.capacity):
            handler.do_POST()
        self.assertEqual(200, handler._send.call_args.args[0], handler._send.call_args.args)
        self.assertEqual(2, len(self.sent))

    def test_capacity_preview_never_creates_key_or_changes_probe(self):
        result = probe.configure({**self.body, "acknowledge_host_access": False}, "9.9.9", preview=True)
        self.assertEqual("capacity-1", result["fingerprint"])
        self.assertEqual([], self.sent)

    def test_capacity_warning_override_is_explicit_and_bound_to_review(self):
        self.capacity.return_value.update(warnings=["RAM high"])
        for body in (self.body, {**self.body, "confirm_capacity": True, "capacity_review": "old"}):
            with self.assertRaisesRegex(ValueError, "fresh review"):
                probe.configure(body, "9.9.9")
            self.assertEqual([], self.sent)
        probe.configure({**self.body, "confirm_capacity": True, "capacity_review": "capacity-1"}, "9.9.9")
        self.assertEqual(2, len(self.sent))

    def test_hard_capacity_failure_is_not_overridable_and_has_zero_writes(self):
        self.capacity.return_value.update(blocked=True, blockers=["Host request capacity exceeded"])
        with self.assertRaisesRegex(ValueError, "capacity exceeded"):
            probe.configure({**self.body, "confirm_capacity": True, "capacity_review": "capacity-1"}, "9.9.9")
        self.assertEqual([], self.sent)

    def test_upgrade_never_enables_checks_or_ignores_capacity_warning(self):
        self.assertEqual("absent", probe.reconcile("9.9.9")["state"])
        self.assertEqual([], self.sent)
        probe.configure(self.body, "9.9.8")
        self.sent.clear()
        self.capacity.return_value.update(warnings=["RAM high"])
        self.assertEqual("review", probe.reconcile("9.9.9")["state"])
        self.assertEqual([], self.sent)
        self.capacity.return_value.update(warnings=[])
        with mock.patch.object(probe, "write") as write:
            self.assertEqual("updated", probe.reconcile("9.9.9")["state"])
        operations = write.call_args.args[2]
        self.assertEqual(["test", "test", "replace"], [op["op"] for op in operations])
        self.assertTrue(operations[-1]["path"].endswith("/image"))
        self.assertEqual("ghcr.io/wjcloudy/homestead:9.9.9", operations[-1]["value"])
