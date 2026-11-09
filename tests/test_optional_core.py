"""Optional reads in the core modules (#619 step 3): each module reads through
the reader its bind() gave it, a missing object (404) is None, and any other
failure raises rather than passing for a missing object."""
import io
import os
import sys
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.environ.get("HOMESTEAD_TEST_SERVER_DIR", str(Path(__file__).resolve().parents[1] / "server")))
import homestead_cancel as CANCEL
import homestead_csi_restore as CSI_RESTORE
import homestead_hostrun as HOSTRUN
import homestead_isos as ISOS
import homestead_move_source as MOVE_SOURCE
import homestead_nodeshell as NODESHELL
import homestead_onboard as ONBOARD
import homestead_operations as OPS
import homestead_revert as REVERT
import homestead_storage_pending as STORAGE_PENDING


def http_error(code):
    return urllib.error.HTTPError("/x", code, "error", {}, io.BytesIO(b""))


class Every:
    """A bound reader that gives the same answer for every path."""
    def __init__(self, answer):
        self.answer, self.paths = answer, []

    def __call__(self, path, **_):
        self.paths.append(path)
        if isinstance(self.answer, Exception):
            raise self.answer
        return self.answer


class Sends:
    def __init__(self):
        self.calls = []

    def __call__(self, *args, **_):
        self.calls.append(args)


class OptionalReadTests(unittest.TestCase):
    def reading(self, module, answer):
        read = Every(answer)
        return read, mock.patch.object(module, "kget", read)

    def assert_raises_on_500(self, module, call):
        read, patch = self.reading(module, http_error(500))
        with patch, self.assertRaises(urllib.error.HTTPError):
            call()
        self.assertTrue(read.paths)

    def test_cancel_helm_chart(self):
        item = {"resource": {"name": "app"}}
        read, patch = self.reading(CANCEL, http_error(404))
        with patch:
            self.assertEqual(CANCEL._helmchart(item), ("app", None))
        self.assertEqual(len(read.paths), 1)
        self.assert_raises_on_500(CANCEL, lambda: CANCEL._helmchart(item))

    def test_csi_restore_problem(self):
        pvc = {"metadata": {"namespace": "lab"}, "status": {"phase": "Pending"},
               "spec": {"dataSource": {"apiGroup": "snapshot.storage.k8s.io", "kind": "VolumeSnapshot", "name": "s"}}}
        _, patch = self.reading(CSI_RESTORE, http_error(404))
        with patch:
            self.assertEqual(CSI_RESTORE.problem(pvc), "The restore snapshot no longer exists")
        self.assert_raises_on_500(CSI_RESTORE, lambda: CSI_RESTORE.problem(pvc))

    def test_hostrun_let_go_leaves_a_missing_lease_alone(self):
        send = Sends()
        read, patch = self.reading(HOSTRUN, http_error(404))
        with patch, mock.patch.object(HOSTRUN, "ksend", send):
            HOSTRUN.let_go("node-a", "me")
        self.assertEqual(len(read.paths), 1)
        self.assertEqual(send.calls, [])

    def test_isos_settings(self):
        _, patch = self.reading(ISOS, http_error(404))
        with patch:
            self.assertEqual(ISOS._settings(), {})
        self.assert_raises_on_500(ISOS, ISOS._settings)

    def test_move_source_object(self):
        _, patch = self.reading(MOVE_SOURCE, http_error(404))
        with patch, self.assertRaisesRegex(ValueError, "does not exist"):
            MOVE_SOURCE._object("vm", "web")
        self.assert_raises_on_500(MOVE_SOURCE, lambda: MOVE_SOURCE._object("vm", "web"))

    def test_nodeshell_session_end_with_its_helper_gone(self):
        send = Sends()
        read, patch = self.reading(NODESHELL, http_error(404))
        NODESHELL._sessions["node-a"] = 1
        self.addCleanup(NODESHELL._sessions.pop, "node-a", None)
        with patch, mock.patch.object(NODESHELL, "ksend", send):
            NODESHELL.session_ended("node-a")
        self.assertEqual(len(read.paths), 1)
        self.assertEqual(send.calls, [])

    def test_onboard_password_secret(self):
        read, patch = self.reading(ONBOARD, http_error(404))
        with patch:
            self.assertEqual(ONBOARD._password_secret("node-a"), "")
        self.assertEqual(len(read.paths), 2)
        self.assert_raises_on_500(ONBOARD, lambda: ONBOARD._password_secret("node-a"))

    def test_operations_volume_delete(self):
        item = {"ref": {"namespace": "lab", "name": "data", "action": "delete_claim"}}
        _, patch = self.reading(OPS, http_error(404))
        with patch:
            self.assertEqual(OPS._volume_delete(item), ("succeeded", 100, "Volume claim deleted. The data behind it is kept."))
        self.assert_raises_on_500(OPS, lambda: OPS._volume_delete(item))

    def test_revert_volume(self):
        _, patch = self.reading(REVERT, http_error(404))
        with patch, self.assertRaisesRegex(ValueError, "there is no Longhorn volume"):
            REVERT._volume("vol")
        self.assert_raises_on_500(REVERT, lambda: REVERT._volume("vol"))

    def test_storage_pending_registered(self):
        _, patch = self.reading(STORAGE_PENDING, http_error(404))
        with patch:
            self.assertFalse(STORAGE_PENDING.registered("node-a"))
        self.assert_raises_on_500(STORAGE_PENDING, lambda: STORAGE_PENDING.registered("node-a"))

    def test_a_module_whose_bind_never_ran_does_not_read_the_cluster(self):
        with mock.patch.object(STORAGE_PENDING, "kget", None), self.assertRaises(TypeError):
            STORAGE_PENDING.registered("node-a")


if __name__ == "__main__":
    unittest.main()
