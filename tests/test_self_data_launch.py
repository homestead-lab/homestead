import hashlib
import http.client
import io
import json
import os
import signal
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

SERVER = os.environ.get("HOMESTEAD_TEST_SERVER_DIR", str(Path(__file__).resolve().parents[1] / "server"))
sys.path.insert(0, SERVER)
import homestead_self_data_launch as L
import homestead_self_data_kube as K
import homestead_self_data_worker as W
from homestead_storage_journal import Held
from test_self_data_coordinator import OP, IMAGE


TOKEN = "d" * 64
DIGEST = hashlib.sha256(TOKEN.encode()).hexdigest()


def scope():
    return K.Scope("lab", "homestead", OP, ["source", "target"], ["old-pv", "new-pv"], ["node1"])


def config():
    return L.configuration(scope(), "anchor-uid", DIGEST)


class ManifestTests(unittest.TestCase):
    def resources(self, **overrides):
        args = dict(anchor_uid="anchor-uid", status_digest=DIGEST, image=IMAGE, node="node1")
        args.update(overrides)
        return L.resources(scope(), **args)

    def test_worker_is_non_restarting_non_root_and_never_mounts_either_data_claim(self):
        resources = self.resources()
        pod = next(r for r in resources if r["kind"] == "Pod")
        spec = pod["spec"]; container = spec["containers"][0]
        self.assertNotIn("ownerReferences", pod["metadata"])
        self.assertEqual("Never", spec["restartPolicy"])
        self.assertFalse(spec["automountServiceAccountToken"])
        self.assertEqual([K.token_volume()], spec["volumes"])
        self.assertEqual(10001, spec["securityContext"]["fsGroup"])
        self.assertTrue(spec["securityContext"]["runAsNonRoot"])
        self.assertTrue(container["securityContext"]["readOnlyRootFilesystem"])
        self.assertFalse(container["securityContext"]["allowPrivilegeEscalation"])
        self.assertEqual({"drop": ["ALL"]}, container["securityContext"]["capabilities"])
        self.assertEqual(IMAGE, container["image"])
        self.assertEqual(["python3", "/srv/homestead_self_data_launch.py"], container["command"])
        self.assertNotIn("livenessProbe", container)
        self.assertNotIn("hostNetwork", spec)
        self.assertNotIn("hostPID", spec)
        self.assertNotIn("nodeName", spec)
        self.assertEqual("node1", spec["affinity"]["nodeAffinity"]["requiredDuringSchedulingIgnoredDuringExecution"]["nodeSelectorTerms"][0]["matchFields"][0]["values"][0])
        self.assertEqual({"cpu": "50m", "memory": "64Mi"}, container["resources"]["requests"])
        self.assertEqual("/healthz", container["readinessProbe"]["httpGet"]["path"])

    def test_identity_comes_from_downward_api_and_only_digest_is_stored(self):
        resources = self.resources()
        pod = next(r for r in resources if r["kind"] == "Pod")
        env = {e["name"]: e for e in pod["spec"]["containers"][0]["env"]}
        self.assertEqual("metadata.uid", env[L.UID_ENV]["valueFrom"]["fieldRef"]["fieldPath"])
        value, actual_scope = L.parse_configuration(env[L.CONFIG_ENV]["value"])
        self.assertEqual(DIGEST, value["status_digest"])
        self.assertEqual(scope().claims, actual_scope.claims)
        self.assertNotIn(TOKEN, json.dumps(resources))
        self.assertEqual(resources[0]["metadata"]["name"], pod["spec"]["serviceAccountName"])

    def test_progress_service_is_internal_and_selects_only_this_worker(self):
        pod, service = self.resources()[-2:]
        self.assertEqual("ClusterIP", service["spec"]["type"])
        self.assertNotIn("externalIPs", service["spec"])
        self.assertNotIn("loadBalancerIP", service["spec"])
        self.assertEqual(8081, service["spec"]["ports"][0]["port"])
        self.assertTrue(all(pod["metadata"]["labels"][k] == v for k, v in service["spec"]["selector"].items()))
        self.assertNotIn("app", service["spec"]["selector"])

    def test_unpinned_image_or_unreviewed_node_refused(self):
        for kwargs in ({"image": "homestead:latest"}, {"node": "other"}, {"anchor_uid": ""}, {"status_digest": TOKEN + "extra"}):
            with self.assertRaises(Held): self.resources(**kwargs)

    def test_config_cannot_override_origin_credentials_identity_or_unknown_fields(self):
        baseline = json.loads(config())
        for extra in ({"api_url": "http://evil"}, {"token": TOKEN}, {"worker_uid": "invented"}, {"protocol": True},
                      {"protocol": 2}, {"nodes": ["node1", "node1"]}, {"claims": []}, {"anchor_uid": "x\nprivate"}):
            with self.assertRaises(Held): L.parse_configuration(json.dumps({**baseline, **extra}))
        for raw in (None, "", "[1]", "{" * 10000, " " * 32769, config()[:-1] + ',"protocol":1}'):
            with self.assertRaises(Held): L.parse_configuration(raw)


class RuntimeTests(unittest.TestCase):
    def test_real_executable_fails_closed_without_config_and_does_not_create_data_files(self):
        with tempfile.TemporaryDirectory() as directory:
            env = {**os.environ, "DATA_DIR": directory, "PYTHONDONTWRITEBYTECODE": "1"}
            env.pop(L.CONFIG_ENV, None); env.pop(L.UID_ENV, None)
            result = subprocess.run([sys.executable, str(Path(SERVER) / "homestead_self_data_launch.py")],
                                    env=env, capture_output=True, text=True, timeout=10)
            self.assertEqual(1, result.returncode)
            self.assertEqual("", result.stdout)
            self.assertNotIn("Traceback", result.stderr)
            self.assertIn("no retry was requested", result.stderr)
            self.assertEqual([], list(Path(directory).iterdir()))

    def test_main_uses_scoped_client_default_admission_and_restores_signals(self):
        before = {s: signal.getsignal(s) for s in (signal.SIGTERM, signal.SIGINT)}
        env = {L.CONFIG_ENV: config(), L.UID_ENV: "worker-uid"}
        with mock.patch.dict(os.environ, env), mock.patch.object(sys, "argv", ["worker"]), \
                mock.patch.object(L.K, "Client") as client, mock.patch.object(L.W, "Runner") as runner, \
                mock.patch.object(L, "serve") as serve:
            def interrupt(_runner, _digest, stop):
                signal.getsignal(signal.SIGTERM)(signal.SIGTERM, None)
                self.assertTrue(stop.is_set())
            serve.side_effect = interrupt
            self.assertEqual(0, L.main())
            args, kwargs = runner.call_args
            self.assertEqual((client.return_value.read, client.return_value.send, client.return_value.logs), args)
            self.assertNotIn("admit", kwargs)
            self.assertTrue(kwargs["require_setup_receipts"])
            self.assertEqual("worker-uid", kwargs["worker_uid"])
            self.assertEqual("anchor-uid", kwargs["anchor_uid"])
            self.assertEqual(DIGEST, serve.call_args.args[1])
        self.assertEqual(before, {s: signal.getsignal(s) for s in before})

    def test_main_never_logs_raw_startup_exception(self):
        with mock.patch.dict(os.environ, {L.CONFIG_ENV: config(), L.UID_ENV: "worker-uid"}), \
                mock.patch.object(sys, "argv", ["worker"]), mock.patch.object(L.K, "Client", side_effect=RuntimeError("private credential")), \
                mock.patch.object(sys, "stderr", new_callable=io.StringIO) as output:
            self.assertEqual(1, L.main())
            self.assertNotIn("private credential", output.getvalue())

    def test_invalid_cli_or_missing_uid_never_builds_an_api_client(self):
        for args, uid in ((["worker", "--unsafe"], "worker-uid"), (["worker"], "")):
            with mock.patch.dict(os.environ, {L.CONFIG_ENV: config(), L.UID_ENV: uid}), \
                    mock.patch.object(sys, "argv", args), mock.patch.object(L.K, "Client") as client, \
                    mock.patch.object(sys, "stderr", new_callable=io.StringIO):
                self.assertEqual(1, L.main())
                client.assert_not_called()

    def test_bind_failure_cannot_start_mutations(self):
        runner = mock.Mock()
        with mock.patch.object(W, "status_server", side_effect=OSError("port in use")):
            with self.assertRaises(OSError): L.serve(runner, DIGEST, threading.Event())
        runner.run.assert_not_called()

    def test_serving_thread_failure_stops_worker_and_is_not_success(self):
        runner, stop = mock.Mock(), threading.Event()
        runner.run.side_effect = lambda event: self.assertTrue(event.wait(2))
        httpd = mock.Mock()
        httpd.serve_forever.side_effect = RuntimeError("unsafe detail")
        with mock.patch.object(W, "status_server", return_value=httpd):
            with self.assertRaisesRegex(Held, "progress server stopped"):
                L.serve(runner, DIGEST, stop)
        self.assertTrue(stop.is_set())
        httpd.server_close.assert_called_once()

    def test_thread_start_failure_releases_listener_without_starting_worker(self):
        runner, httpd = mock.Mock(), mock.Mock()
        with mock.patch.object(W, "status_server", return_value=httpd), \
                mock.patch.object(threading.Thread, "start", side_effect=RuntimeError("thread unavailable")):
            with self.assertRaises(RuntimeError): L.serve(runner, DIGEST, threading.Event())
        runner.run.assert_not_called()
        httpd.server_close.assert_called_once()

    def test_worker_exception_stops_and_closes_progress(self):
        runner, stop = mock.Mock(), threading.Event()
        runner.run.side_effect = RuntimeError("worker unavailable")
        real = W.status_server
        observed = []
        def make(*args):
            server = real(*args); observed.append(server)
            return server
        with mock.patch.object(W, "status_server", side_effect=make):
            with self.assertRaises(RuntimeError):
                L.serve(runner, DIGEST, stop, address=("127.0.0.1", 0))
        self.assertTrue(stop.is_set())
        self.assertEqual(-1, observed[0].socket.fileno())

    def test_stop_signal_closes_progress_without_another_worker_tick(self):
        stop = threading.Event()
        runner = mock.Mock(operation=OP)
        runner.snapshot.return_value = {"status": "held", "phase": "copy"}
        real = W.status_server
        with mock.patch.object(W, "status_server", wraps=real) as factory:
            def running(event):
                server = factory.spy_server
                conn = http.client.HTTPConnection(*server.server_address, timeout=2)
                try:
                    conn.request("GET", "/api/self/data/handoff/" + OP, headers={"Authorization": "Bearer " + TOKEN})
                    response = conn.getresponse()
                    self.assertEqual(200, response.status)
                    self.assertEqual("held", json.loads(response.read())["status"])
                finally:
                    conn.close()
                event.set()
            def make(*args):
                server = real(*args); factory.spy_server = server
                return server
            factory.side_effect = make
            runner.run.side_effect = running
            L.serve(runner, DIGEST, stop, address=("127.0.0.1", 0))
            self.assertEqual(-1, factory.spy_server.socket.fileno())
        runner.run.assert_called_once()
        runner.tick.assert_not_called()


if __name__ == "__main__":
    unittest.main()
