"""Server mutation hooks must enforce the independent startup/write fence."""
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.environ.get("HOMESTEAD_TEST_SERVER_DIR", str(Path(__file__).resolve().parents[1] / "server")))
import server
import homestead_operations as OPS
import homestead_shared as SHARED
from homestead_storage_journal import Held


class MutationHooksTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        for patch in (mock.patch.object(OPS, "DATA_DIR", self.temp.name),
                      mock.patch.object(server, "DATA_DIR", self.temp.name),
                      mock.patch.object(server, "_self_data_fence", None)):
            patch.start(); self.addCleanup(patch.stop)

    def held(self):
        fence = mock.Mock()
        fence.require_write.side_effect = Held("Data handoff requires review")
        patch = mock.patch.object(server, "_self_data_fence", fence)
        patch.start(); self.addCleanup(patch.stop)
        return fence

    def test_kubernetes_mutation_is_blocked_before_any_dispatch(self):
        self.held()
        with mock.patch.object(server.STORAGE_GUARD, "send") as guarded, mock.patch.object(server, "_ksend") as raw:
            with self.assertRaises(Held): server.ksend("DELETE", "/api/v1/namespaces/lab/persistentvolumeclaims/source")
            guarded.assert_not_called(); raw.assert_not_called()

    def test_unfenced_install_keeps_existing_storage_guard(self):
        with mock.patch.object(server.STORAGE_GUARD, "send", return_value={"ok": True}) as guarded:
            self.assertEqual({"ok": True}, server.ksend("POST", "/api/v1/namespaces/lab/pods", {}))
            guarded.assert_called_once()

    def test_job_start_cannot_persist_after_freeze(self):
        self.held()
        with self.assertRaises(Held):
            OPS.start("image-pull", "Pull image", {}, "/", {"namespace": "lab", "name": "pull"})
        self.assertFalse((Path(self.temp.name) / OPS.STORE).exists())

    def test_job_poll_returns_snapshot_without_running_resolver_or_rewriting_history(self):
        job = OPS.start("image-pull", "Pull image", {}, "/", {"namespace": "lab", "name": "pull"})
        before = (Path(self.temp.name) / OPS.STORE).read_bytes()
        self.held()
        with mock.patch.dict(OPS.RESOLVERS, {"image-pull": mock.Mock(side_effect=AssertionError("must not run"))}):
            current = OPS.list_operations()
        self.assertEqual(job["id"], current[0]["id"])
        self.assertEqual(before, (Path(self.temp.name) / OPS.STORE).read_bytes())

    def test_direct_refresh_cannot_bypass_poll_guard(self):
        self.held()
        with mock.patch.dict(OPS.RESOLVERS, {"test": mock.Mock()}):
            with self.assertRaises(Held): OPS._refresh({"status": "running", "kind": "test"})
            OPS.RESOLVERS["test"].assert_not_called()

    def test_shared_json_writer_preserves_existing_config_when_held(self):
        path = Path(self.temp.name) / "settings.json"
        SHARED.write_json(path, {"old": True}, durable=True)
        self.held()
        with self.assertRaises(Held): SHARED.write_json(path, {"new": True}, durable=True)
        self.assertEqual('{"old": true}', path.read_text(encoding="utf-8"))
        self.assertFalse(list(Path(self.temp.name).glob("*.tmp")))

    def test_file_hook_does_not_block_unrelated_runtime_files(self):
        fence = self.held()
        server.self_data_file_write(str(Path(self.temp.name).parent / "unrelated-file"))
        fence.require_write.assert_not_called()

    def test_http_mutations_and_console_upgrades_are_held(self):
        self.held()
        for method, path in (("POST", "/api/auth/login"), ("POST", "/api/settings"),
                             ("DELETE", "/api/workload/lab/app"), ("GET", "/api/console"),
                             ("GET", "/api/node/shell"), ("GET", "/api/vm/console")):
            handler = object.__new__(server.H)
            handler.command = method; handler._send = mock.Mock()
            with mock.patch.object(server.CFACCESS, "enabled", return_value=False):
                self.assertTrue(handler._guard(path))
            self.assertEqual(503, handler._send.call_args.args[0])
            self.assertTrue(handler._send.call_args.args[1]["data_handoff"])

    def test_startup_gate_precedes_feature_bindings_and_requires_fresh_api_proof(self):
        with mock.patch.object(server, "TOKEN", "fixture"), mock.patch.dict(os.environ, {"HOSTNAME": "pod1"}), \
                mock.patch("builtins.open", mock.mock_open(read_data="lab\n")), \
                mock.patch.object(server.SELF_DATA_FENCE, "Fence") as factory:
            factory.return_value.inspect.side_effect = Held("Startup held")
            with self.assertRaises(Held): server.initialize_self_data_fence()
            factory.return_value.inspect.assert_called_once()
            self.assertIs(factory.return_value, server._self_data_fence)
        text = Path(server.__file__).read_text(encoding="utf-8")
        self.assertLess(text.index("    initialize_self_data_fence()"), text.index("NAMES.bind(kget)"))

    def test_demo_startup_does_not_contact_a_cluster(self):
        with mock.patch.object(server, "TOKEN", ""), mock.patch.object(server.SELF_DATA_FENCE, "Fence") as factory:
            server.initialize_self_data_fence()
            factory.assert_not_called()

    def test_real_entrypoint_blocks_before_creating_data_or_starting_services(self):
        script = r'''
import builtins, io, os, pathlib, runpy, sys, urllib.request
from unittest import mock
sys.path.insert(0, sys.argv[1])
from homestead_storage_journal import Held
root = pathlib.Path(sys.argv[2]) / "must-not-be-created"
os.environ.update(DATA_DIR=str(root), HOSTNAME="pod1", PORT="0")
opened, exists = builtins.open, os.path.exists
def file_open(path, *args, **kwargs):
    if str(path) == "/var/run/secrets/kubernetes.io/serviceaccount/token":
        return io.StringIO("fixture-token")
    if str(path) == "/var/run/secrets/kubernetes.io/serviceaccount/namespace":
        return io.StringIO("lab")
    return opened(path, *args, **kwargs)
def present(path):
    if str(path) == "/var/run/secrets/kubernetes.io/serviceaccount/token":
        return True
    return exists(path)
with mock.patch("builtins.open", side_effect=file_open), mock.patch("os.path.exists", side_effect=present), \
     mock.patch.object(urllib.request, "urlopen", side_effect=OSError("private upstream diagnostic")):
    try:
        runpy.run_path(str(pathlib.Path(sys.argv[1]) / "server.py"), run_name="__main__")
    except Held as error:
        assert "private" not in str(error)
        assert not root.exists(), "persistent state was touched before the startup fence"
        print("startup-held-before-persistence")
    else:
        raise AssertionError("startup unexpectedly passed")
'''
        result = subprocess.run([sys.executable, "-B", "-c", script, str(Path(server.__file__).parent), self.temp.name],
                                capture_output=True, text=True, timeout=20,
                                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn("startup-held-before-persistence", result.stdout)


if __name__ == "__main__":
    unittest.main()
