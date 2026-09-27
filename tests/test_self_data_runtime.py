"""Source review needs reports from the actual mounted, current processes."""
import copy
import io
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, os.environ.get("HOMESTEAD_TEST_SERVER_DIR", str(Path(__file__).resolve().parents[1] / "server")))
import homestead_self_data_fence as F
import homestead_storage_runtime as R
import homestead_shared as S
import homestead_operations as OPS
from homestead_storage_journal import Held


MOUNT = {"mount_id": "50", "device": "8:1", "inode": 12, "root": "/pvc-root", "filesystem": "ext4"}


def pod(name):
    return {"metadata": {"name": name, "namespace": "lab", "uid": name + "-uid"},
            "spec": {"volumes": [{"name": "data", "persistentVolumeClaim": {"claimName": "source"}}],
                     "containers": [{"name": "homestead", "image": "homestead:release",
                        "readinessProbe": {"httpGet": {"path": "/healthz", "port": 8080}},
                        "volumeMounts": [{"name": "data", "mountPath": "/data"}]}]},
            "status": {"phase": "Running", "containerStatuses": [{"name": "homestead",
                "containerID": "containerd://" + name, "imageID": "sha256:" + "a" * 64,
                "state": {"running": {"startedAt": "now"}}}]}}


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(); self.addCleanup(directory.cleanup)
        for patch in (mock.patch.object(OPS, "DATA_DIR", directory.name),
                      mock.patch.object(F, "mounted_data", return_value=copy.deepcopy(MOUNT))):
            patch.start(); self.addCleanup(patch.stop)
        self.pods = {name: pod(name) for name in ("one", "two")}
        self.clock = lambda: 1000

    def read(self, path):
        if path.endswith("/pods"): return {"items": copy.deepcopy(list(self.pods.values()))}
        return copy.deepcopy(self.pods[path.rsplit("/", 1)[1]])

    def report(self, name, **kwargs):
        return R.report(OPS, self.read, "lab", name, "homestead", "new", self_data=True, clock=self.clock, **kwargs)

    def check(self):
        return R.require_self_data(OPS, list(self.pods.values()), "lab", "homestead", "new", "source",
                                   own_uid="one-uid", clock=self.clock)

    def ready(self):
        self.report("one"); self.report("two")

    def test_reports_are_compatible_with_existing_storage_protocol(self):
        self.ready()
        self.assertEqual(2, R.require(OPS, self.read, "lab", "one", "homestead", "new")["replicas"])
        result = self.check()
        self.assertEqual(2, len(result)); self.assertNotIn("checked_at", result[0])

    def test_missing_failed_or_expired_report_blocks_self_move(self):
        self.report("one")
        with self.assertRaises(Held): self.check()
        self.report("two")
        self.clock = lambda: 1061
        with self.assertRaises(Held): self.check()
        self.clock = lambda: 999
        with self.assertRaises(Held): self.check()

    def test_replaced_container_needs_its_own_report(self):
        self.ready()
        self.pods["two"]["status"]["containerStatuses"][0]["containerID"] = "new-container"
        with self.assertRaises(Held): self.check()
        self.report("two"); self.check()

    def test_bad_mount_does_not_claim_self_move_support_or_disable_normal_protocol(self):
        self.ready()
        with mock.patch.object(F, "mounted_data", side_effect=Held("not a volume")):
            row = self.report("one")
        self.assertNotIn("self_data", row)
        self.assertEqual(2, R.require(OPS, self.read, "lab", "one", "homestead", "new")["replicas"])
        with self.assertRaises(Held): self.check()

    def test_preview_does_not_create_locks_register_or_rewrite_heartbeats(self):
        self.ready()
        before = Path(OPS.DATA_DIR, R.FILE).read_bytes()
        with mock.patch.object(OPS, "_lock", side_effect=AssertionError("no locking")), \
             mock.patch.object(S, "write_json", side_effect=AssertionError("no writes")):
            self.check()
        self.assertEqual(before, Path(OPS.DATA_DIR, R.FILE).read_bytes())

    def test_local_mount_change_invalidates_report(self):
        self.ready()
        with mock.patch.object(F, "mounted_data", return_value={**MOUNT, "mount_id": "99"}):
            with self.assertRaisesRegex(Held, "mount changed"): self.check()

    def test_sidecar_and_ephemeral_writers_block(self):
        self.ready()
        for key in ("containers", "ephemeralContainers"):
            original = copy.deepcopy(self.pods["two"]["spec"].get(key, []))
            writer = {"name": "other", "volumeMounts": [{"name": "data", "mountPath": "/files"}]}
            self.pods["two"]["spec"][key] = original + [writer]
            with self.assertRaisesRegex(Held, "Another container"): self.check()
            writer["volumeMounts"][0]["readOnly"] = True
            self.check()
            self.pods["two"]["spec"][key] = original

    def test_known_completed_initializer_only_and_pinned_on_restart(self):
        self.ready()
        init = {"name": "data-permissions", "image": "homestead:release",
            "command": ["sh", "-c", "chown 10001:10001 /data && chmod 0770 /data"],
            "volumeMounts": [{"name": "data", "mountPath": "/data"}]}
        current = self.pods["two"]
        current["spec"]["initContainers"] = [init]
        current["status"]["initContainerStatuses"] = [{"name": "data-permissions", "state": {"terminated": {"exitCode": 0}}}]
        self.check()
        for key, value in (("restartPolicy", "Always"), ("command", ["custom"]), ("env", [{"name": "PATH", "value": "/other"}])):
            with mock.patch.dict(init, {key: value}):
                with self.assertRaises(Held): self.check()
        current["status"]["initContainerStatuses"][0]["state"] = {"running": {}}
        with self.assertRaises(Held): self.check()
        dep = {"metadata": {"name": "homestead"}, "spec": {"template": {"spec": current["spec"]}}}
        F.pin_app_image(dep, "repo@sha256:" + "a" * 64)
        self.assertEqual("repo@sha256:" + "a" * 64, init["image"])

    def test_subdirectory_readonly_and_missing_own_replica_are_not_accepted(self):
        self.ready()
        mount = self.pods["two"]["spec"]["containers"][0]["volumeMounts"][0]
        for key, value in (("subPath", "config"), ("subPathExpr", "$(POD)"), ("readOnly", True)):
            with mock.patch.dict(mount, {key: value}):
                with self.assertRaises(Held): self.check()
        self.pods.pop("one")
        with self.assertRaises(Held): self.check()


class MountTests(unittest.TestCase):
    def setUp(self):
        self.info = "50 1 8:1 /pvc-root /data rw,relatime - ext4 /dev/test rw\n"
        self.stat = SimpleNamespace(st_dev=99, st_ino=12)
        def opened(path, **_):
            return io.StringIO("mnt_id:\t50\n" if "/fdinfo/" in path else self.info)
        for patch in (mock.patch.object(F.os, "name", "posix"),
                      mock.patch.object(F.os, "O_DIRECTORY", 0, create=True),
                      mock.patch.object(F.os, "O_NOFOLLOW", 0, create=True),
                      mock.patch.object(F.os.path, "isabs", return_value=True),
                      mock.patch.object(F.os.path, "normpath", side_effect=lambda p: p),
                      mock.patch.object(F.os.path, "realpath", side_effect=lambda p: p),
                      mock.patch.object(F.os, "open", return_value=81), mock.patch.object(F.os, "close"),
                      mock.patch.object(F.os, "fstat", return_value=self.stat),
                      mock.patch.object(F.os, "stat", return_value=self.stat),
                      mock.patch.object(F.os, "major", return_value=8, create=True),
                      mock.patch.object(F.os, "minor", return_value=1, create=True),
                      mock.patch("builtins.open", side_effect=opened)):
            patch.start(); self.addCleanup(patch.stop)

    def test_opened_mount_id_device_and_path_are_verified(self):
        self.assertEqual(MOUNT, F.mounted_data("/data"))
        F.os.close.assert_called_once_with(81)

    def test_root_subdirectory_memory_readonly_mismatch_and_bad_proc_are_rejected(self):
        original = self.info
        for value in (original.replace("/data", "/"), original.replace("ext4", "tmpfs"),
                      original.replace("rw,relatime", "ro,relatime"), original.replace("test rw", "test ro"),
                      original.replace("8:1", "8:2"), original.replace("50 1", "51 1"), "invalid"):
            self.info = value
            with self.assertRaises(Held): F.mounted_data("/data")
        with self.assertRaises(Held): F.mounted_data("/")

    def test_mount_replacement_and_symlink_are_rejected(self):
        with mock.patch.object(F.os, "stat", return_value=SimpleNamespace(st_dev=99, st_ino=15)):
            with self.assertRaisesRegex(Held, "changed"): F.mounted_data("/data")
        with mock.patch.object(F.os.path, "realpath", return_value="/actual"):
            with self.assertRaisesRegex(Held, "symbolic link"): F.mounted_data("/data")


if __name__ == "__main__": unittest.main()
