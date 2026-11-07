"""Each share's recycle bin: the image's own bin off, and each share's as its settings say."""
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "server"))
import homestead_names as NAMES
import homestead_operations as JOBS
import homestead_shares as shares
import homestead_share_recycle as RECYCLE
import homestead_share_servers as servers
import test_shares

DEPLOYMENT = "/apis/apps/v1/namespaces/lab/deployments/homestead-smb"
CONFIG = "/api/v1/namespaces/lab/configmaps/homestead-shares"


def set_up(self):
    """The share tests' own, and afterwards put back what shares.bind() and servers.bind()
    set. A restart's job goes in this test's own folder, and a resize asks no real cluster."""
    for module, names in ((shares, ("kget", "ksend", "create_pvc", "NAMESPACE", "CACHE")), (NAMES, ("kget",)),
                          (servers, ("kget", "ksend", "build_deployment", "SMB_NAMESPACE", "_cache",
                                     "shared_classes", "default_class", "storage_options"))):
        for name in names:
            self.addCleanup(setattr, module, name, getattr(module, name, None))
    self.enterContext(mock.patch.object(JOBS, "DATA_DIR", self.enterContext(tempfile.TemporaryDirectory())))
    self.enterContext(mock.patch.object(shares.RESIZE, "upgrade_guard", None))
    test_shares.ShareTests.setUp(self)


class RecycleBinTests(unittest.TestCase):
    setUp = set_up

    def podspec(self):
        return self.objects[DEPLOYMENT]["spec"]["template"]["spec"]

    def args(self):
        return self.podspec()["containers"][0]["args"]

    def bin_options(self, name):
        args = self.args()
        return [args[i + 1].split(";", 1)[1] for i, flag in enumerate(args[:-1])
                if flag == "-G" and args[i + 1].startswith(name + ";")]

    def stored(self):
        return json.loads(self.objects[CONFIG]["data"]["shares.json"])

    def test_the_image_bin_is_turned_off_before_any_share_gets_its_own(self):
        rows = [{"name": "media", "pvc": "claim", "public": True, "recycle": False}]
        dep = shares.configured_deployment(self.objects[DEPLOYMENT], rows, {})
        args = dep["spec"]["template"]["spec"]["containers"][0]["args"]
        self.assertEqual("-r", args[0])
        self.assertNotIn("-G", args)
        self.assertEqual(["homestead-smb"], [c["name"] for c in dep["spec"]["template"]["spec"]["containers"]])

    def test_a_share_saved_before_the_setting_keeps_its_bin_without_a_limit(self):
        self.assertEqual((True, 0), RECYCLE.setting({"name": "old"}))
        rows, credentials, *_ = shares._state()
        self.objects[DEPLOYMENT] = shares.configured_deployment(self.objects[DEPLOYMENT], rows, credentials)
        options = self.bin_options("secure")
        self.assertIn("recycle:repository = .deleted", options)
        self.assertIn("vfs objects = catia fruit recycle streams_xattr", options)
        self.assertIn("recycle:touch_mtime = yes", options)
        self.assertEqual(1, len(self.podspec()["containers"]), "a bin with no limit needs no cleaner")
        self.assertLess(self.args().index("-s"), self.args().index("-G"), "a share's options follow the share")

    def test_a_limit_runs_a_cleaner_on_that_share_only(self):
        result = shares.edit_share("secure", 10, "lab", "", False, recycle=True, recycle_days=14)
        self.assertTrue(result["deployment_updated"], "a bin change restarts Samba")
        self.assertEqual(14, self.stored()[0]["recycle_days"])
        spec = self.podspec()
        cleaner = spec["containers"][1]
        self.assertEqual(RECYCLE.CLEANER, cleaner["name"])
        self.assertIn('find "/bins/0/.deleted" -mindepth 1 -type f -mmin +20160 -delete', cleaner["command"][2])
        samba_mount = spec["containers"][0]["volumeMounts"][0]
        self.assertEqual([{**samba_mount, "mountPath": "/bins/0"}], cleaner["volumeMounts"])
        again = shares.configured_deployment(self.objects[DEPLOYMENT], shares._state()[0], shares._state()[1])
        self.assertEqual(spec, again["spec"]["template"]["spec"], "applying it again changes nothing")

    def test_turning_the_bin_off_removes_its_options_and_cleaner(self):
        shares.edit_share("secure", 10, "lab", "", False, recycle=True, recycle_days=7)
        shares.edit_share("secure", 10, "lab", "", False, recycle=False)
        self.assertEqual([], self.bin_options("secure"))
        self.assertEqual(["homestead-smb"], [c["name"] for c in self.podspec()["containers"]])
        self.assertEqual(False, self.stored()[0]["recycle"])
        self.assertEqual(7, self.stored()[0]["recycle_days"], "the limit is kept for when it is turned back on")

    def test_leaving_the_bin_out_of_an_edit_keeps_it(self):
        shares.edit_share("secure", 10, "lab", "", False, recycle=True, recycle_days=9)
        result = shares.edit_share("secure", 20, "lab", "", False)
        self.assertFalse(result["deployment_updated"])
        self.assertEqual((True, 9), RECYCLE.setting(self.stored()[0]))

    def test_read_only_shares_get_no_bin(self):
        shares.edit_share("secure", 10, "lab", "", False, True, recycle=True, recycle_days=3)
        self.assertEqual([], self.bin_options("secure"))
        self.assertEqual(1, len(self.podspec()["containers"]))

    def test_new_shares_keep_deleted_files_thirty_days_unless_told(self):
        shares.create_share("media", 10, "lab", "pw", False)
        row = next(r for r in self.stored() if r["name"] == "media")
        self.assertEqual((True, 30), RECYCLE.setting(row))
        shares.create_share("scratch", 10, "lab", "", False, recycle=False)
        row = next(r for r in self.stored() if r["name"] == "scratch")
        self.assertFalse(RECYCLE.setting(row)[0])
        self.assertEqual([], self.bin_options("scratch"))

    def test_days_must_be_a_whole_number_in_range(self):
        for bad in (-1, 3651, "ten", 2.5):
            with self.subTest(days=bad), self.assertRaises(ValueError):
                shares.edit_share("secure", 10, "lab", "", False, recycle=True, recycle_days=bad)
        self.assertEqual(0, RECYCLE.days_value("0"))
        self.assertEqual(30, RECYCLE.days_value(None))

    def test_the_public_view_says_what_the_bin_does(self):
        listed = shares.list_shares()[0]
        self.assertEqual((True, 0), (listed["recycle"], listed["recycle_days"]))


class EmptyingTests(unittest.TestCase):
    setUp = set_up

    def pod(self, ready=True):
        self.objects["/api/v1/namespaces/lab/pods?labelSelector=app%3Dhomestead-smb"] = {"items": [{
            "metadata": {"name": "homestead-smb-abc"},
            "status": {"phase": "Running", "containerStatuses": [{"name": "homestead-smb", "ready": ready}]}}]}
        servers.bind(shares.kget, shares.ksend, None, "lab", {})

    def run_in_pod(self, kib=2 * 1024 ** 2, files=3):
        calls = []
        def run(namespace, pod, argv, timeout=30, container="files", **_):
            calls.append((namespace, pod, container, argv[2]))
            out = f"{kib}\n{files}\n".encode() if "du -sk" in argv[2] else b""
            return mock.Mock(stdout=out, stderr="", exit_code=0)
        patch = mock.patch.object(RECYCLE.POD_EXEC, "run", side_effect=run)
        patch.start(); self.addCleanup(patch.stop)
        return calls

    def test_size_reads_the_share_s_own_bin_in_samba(self):
        self.pod()
        calls = self.run_in_pod()
        bin_ = servers.recycle_bin("secure")
        self.assertEqual((2.0, 3, "/shares/secure/.deleted"), (bin_["gb"], bin_["files"], bin_["folder"]))
        self.assertEqual(("lab", "homestead-smb-abc", "homestead-smb"), calls[0][:3])

    def test_empty_deletes_only_inside_the_bin(self):
        self.pod()
        calls = self.run_in_pod()
        result = servers.empty_recycle({"name": "secure"})
        self.assertIn("2 GiB in 3 files", result["detail"])
        self.assertIn('find "/shares/secure/.deleted" -mindepth 1 -delete', calls[-1][3])

    def test_turning_off_with_empty_empties_before_samba_restarts(self):
        self.pod()
        self.run_in_pod()
        order = []
        edit = shares.edit_share
        with mock.patch.object(shares, "edit_share", side_effect=lambda *a, **k: order.append("edit") or edit(*a, **k)):
            with mock.patch.object(RECYCLE, "empty", side_effect=lambda *a: order.append("empty") or
                                   {"gb": 1, "files": 1, "bytes": 1, "folder": "x"}):
                result = servers.edit_share({"name": "secure", "size_gb": 10, "user": "lab", "public": False,
                                             "recycle": False, "empty_recycle": True})
        self.assertEqual(["empty", "edit"], order)
        self.assertIn("emptied the recycle bin of secure", result["message"])

    def test_no_running_samba_refuses_plainly(self):
        self.pod(ready=False)
        with self.assertRaisesRegex(ValueError, "not running"):
            servers.recycle_bin("secure")

    def test_a_path_outside_the_shares_is_refused(self):
        with self.assertRaises(ValueError):
            RECYCLE.folder({"name": "x", "path": "/etc"})
        with self.assertRaises(ValueError):
            RECYCLE.folder({"name": "x", "path": "/shares/../etc"})


if __name__ == "__main__":
    unittest.main()
