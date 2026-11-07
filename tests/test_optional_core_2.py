"""Optional reads in the core modules, second batch (#619 step 3): each module
reads through the reader it was given, a missing object (404) is None, and any
other failure raises rather than passing for a missing object."""
import io
import os
import sys
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.environ.get("HOMESTEAD_TEST_SERVER_DIR", str(Path(__file__).resolve().parents[1] / "server")))
import homestead_cluster_shutdown as SHUTDOWN
import homestead_config_backup as BACKUP
import homestead_reclass as RECLASS
import homestead_share_servers as SHARE_SERVERS
sys.path.insert(0, str(Path(__file__).resolve().parent))
from fakes.kube import no_cluster


def setUpModule():
    # No cluster here: a read on the way to what a test checks fails at once, as on CI,
    # instead of looking the API server's name up (tests/fakes/kube.py no_cluster).
    unittest.enterModuleContext(no_cluster())


def http_error(code):
    return urllib.error.HTTPError("/x", code, "error", {}, io.BytesIO(b""))


class Every:
    """A reader that gives the same answer for every path."""
    def __init__(self, answer):
        self.answer, self.paths = answer, []

    def __call__(self, path, **_):
        self.paths.append(path)
        if isinstance(self.answer, Exception):
            raise self.answer
        return self.answer


class OptionalReadTests(unittest.TestCase):
    def reading(self, module, answer):
        read = Every(answer)
        return read, mock.patch.object(module, "kget", read)

    def assert_raises_on_500(self, module, call):
        read, patch = self.reading(module, http_error(500))
        with patch, self.assertRaises(urllib.error.HTTPError):
            call()
        self.assertTrue(read.paths)

    def test_reclass_plan_of_a_missing_volume(self):
        read, patch = self.reading(RECLASS, http_error(404))
        with patch, self.assertRaisesRegex(ValueError, "does not exist"):
            RECLASS.plan("lab", "data", "fast")
        self.assertEqual(read.paths, ["/api/v1/namespaces/lab/persistentvolumeclaims/data"])
        self.assert_raises_on_500(RECLASS, lambda: RECLASS.plan("lab", "data", "fast"))

    def test_reclass_old_copy_that_is_gone(self):
        read, patch = self.reading(RECLASS, http_error(404))
        with patch, self.assertRaisesRegex(ValueError, "is not an old copy"):
            RECLASS._remove_old_copy("pv-1")
        self.assertEqual(read.paths, ["/api/v1/persistentvolumes/pv-1"])
        self.assert_raises_on_500(RECLASS, lambda: RECLASS._remove_old_copy("pv-1"))

    def test_share_servers_nfs_state_without_the_gateway(self):
        read, patch = self.reading(SHARE_SERVERS, http_error(404))
        with patch, mock.patch.object(SHARE_SERVERS.SHARES, "_state", return_value=([],)):
            state = SHARE_SERVERS.nfs_state()
        self.assertFalse(state["installed"])
        self.assertEqual(state["address"], "")
        self.assertIn(SHARE_SERVERS._smb_path("deployments", SHARE_SERVERS.NFS.NAME), read.paths)
        self.assertIn(SHARE_SERVERS._smb_path("services", SHARE_SERVERS.NFS.NAME), read.paths)
        with mock.patch.object(SHARE_SERVERS.SHARES, "_state", return_value=([],)):
            self.assert_raises_on_500(SHARE_SERVERS, SHARE_SERVERS.nfs_state)

    def test_cluster_shutdown_inventory_of_an_optional_api(self):
        read = Every(http_error(404))
        self.assertEqual(SHUTDOWN.inventory(read, "/apis/kubevirt.io/v1/virtualmachines", optional_api=True), [])
        self.assertEqual(read.paths, ["/apis/kubevirt.io/v1/virtualmachines"])
        with self.assertRaises(urllib.error.HTTPError):
            SHUTDOWN.inventory(Every(http_error(500)), "/apis/kubevirt.io/v1/virtualmachines", optional_api=True)
        with self.assertRaises(urllib.error.HTTPError):
            SHUTDOWN.inventory(Every(http_error(404)), "/api/v1/nodes")

    def test_config_backup_reads_a_missing_object_as_none(self):
        read, patch = self.reading(BACKUP, http_error(404))
        with patch:
            self.assertIsNone(BACKUP._read("configmaps", "homestead", "settings"))
        self.assertEqual(read.paths, ["/api/v1/namespaces/homestead/configmaps/settings"])
        read, patch = self.reading(BACKUP, {"data": {"a": "1"}})
        with patch:
            self.assertEqual(BACKUP._read("configmaps", "homestead", "settings"), {"data": {"a": "1"}, "binaryData": {}})
        self.assert_raises_on_500(BACKUP, lambda: BACKUP._read("configmaps", "homestead", "settings"))


if __name__ == "__main__":
    unittest.main()
