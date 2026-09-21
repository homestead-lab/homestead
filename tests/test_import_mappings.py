import copy
import sys
import unittest
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))

import harvui_imports as imports
import server


SOURCE = {"name": "tower", "host": "192.0.2.10", "user": "root",
          "kind": "unraid", "base_path": "/mnt/user/appdata"}


class ImportMappingTests(unittest.TestCase):
    """An Unraid app with several appdata folders becomes one volume."""

    def test_each_mapping_gets_its_own_folder_in_one_volume(self):
        rows = imports.import_mappings({"mappings": [
            {"remote_path": "/mnt/user/appdata/plex/config", "mount_path": "/config"},
            {"remote_path": "/mnt/user/appdata/plex/transcode", "mount_path": "/transcode"},
        ]})

        self.assertEqual(["config", "transcode"], [row["folder"] for row in rows])
        self.assertEqual(["/config", "/transcode"], [row["mount_path"] for row in rows])

    def test_colliding_folder_names_are_kept_apart(self):
        rows = imports.import_mappings({"mappings": [
            {"remote_path": "/mnt/user/appdata/a/data", "mount_path": "/one"},
            {"remote_path": "/mnt/user/appdata/b/data", "mount_path": "/two"},
        ]})

        self.assertEqual(["data", "data-2"], [row["folder"] for row in rows])

    def test_a_single_mapping_still_lands_at_the_volume_root(self):
        rows = imports.import_mappings({"remote_path": "/mnt/user/appdata/plex",
                                        "mount_path": "/config"})

        self.assertEqual([{"remote_path": "/mnt/user/appdata/plex",
                           "mount_path": "/config", "folder": ""}], rows)

    def test_bad_mappings_are_refused(self):
        with self.assertRaisesRegex(ValueError, "remote path must be absolute"):
            imports.import_mappings({"mappings": [{"remote_path": "relative", "mount_path": "/x"}]})
        with self.assertRaisesRegex(ValueError, "mapped twice"):
            imports.import_mappings({"mappings": [
                {"remote_path": "/a", "mount_path": "/config"},
                {"remote_path": "/b", "mount_path": "/config"}]})
        with self.assertRaisesRegex(ValueError, "climb out"):
            imports.import_mappings({"mappings": [
                {"remote_path": "/a", "mount_path": "/x", "folder": "../etc"}]})


class ImportManifestTests(unittest.TestCase):
    def setUp(self):
        # build_deployment asks the cluster for hardware features otherwise.
        self.features = server.HW.features
        server.HW.features = lambda: []

    def tearDown(self):
        server.HW.features = self.features

    def test_one_claim_backs_every_folder_of_the_workload(self):
        deployment, _ = server.build_deployment({
            "name": "plex", "workload_name": "plex", "container_name": "plex",
            "image": "plex:1", "namespace": "lab",
            "volumes": [
                {"path": "/config", "source": "plex-appdata", "type": "pvc", "sub_path": "config"},
                {"path": "/transcode", "source": "plex-appdata", "type": "pvc", "sub_path": "transcode"},
            ]})

        spec = deployment["spec"]["template"]["spec"]
        self.assertEqual(1, len(spec["volumes"]), "one claim, not one volume per folder")
        self.assertEqual("plex-appdata", spec["volumes"][0]["persistentVolumeClaim"]["claimName"])
        mounts = spec["containers"][0]["volumeMounts"]
        self.assertEqual([("/config", "config"), ("/transcode", "transcode")],
                         [(m["mountPath"], m["subPath"]) for m in mounts])
        self.assertEqual({spec["volumes"][0]["name"]}, {m["name"] for m in mounts})

    def test_a_mapping_without_a_folder_has_no_sub_path(self):
        deployment, _ = server.build_deployment({
            "name": "app", "workload_name": "app", "container_name": "app",
            "image": "app:1", "namespace": "lab",
            "volumes": [{"path": "/config", "source": "app-appdata", "type": "pvc", "sub_path": ""}]})

        mount = deployment["spec"]["template"]["spec"]["containers"][0]["volumeMounts"][0]
        self.assertNotIn("subPath", mount)


class ImportProgressTests(unittest.TestCase):
    """The log replays 0-100% per folder; the UI needs one honest number."""

    def test_progress_counts_finished_folders_not_just_the_running_one(self):
        log = "\n".join([
            "==> step 1/4 config :: tower:/mnt/user/appdata/plex/config -> /config",
            "  1,234,567  57%  11.83MB/s  0:00:04",
            "==> step 1/4 config complete",
            "==> step 2/4 data :: tower:/mnt/user/appdata/plex/data -> /data",
            "  9,000,000  50%  22.10MB/s  0:00:20",
        ])

        progress = imports.import_progress(log)

        self.assertEqual((2, 4, "data"), (progress["step"], progress["steps"], progress["folder"]))
        self.assertEqual(50, progress["step_percent"])
        self.assertEqual(37.5, progress["percent"], "one of four done plus half of the second")
        self.assertEqual("22.10MB/s", progress["rate"])

    def test_a_carriage_returned_bar_is_read_as_lines(self):
        progress = imports.import_progress(
            "==> step 1/1 appdata :: a -> /config\r  10  9%  1MB/s  0:00:01\r  99  93%  2MB/s  0:00:00")

        self.assertEqual(93, progress["step_percent"])

    def test_a_finished_job_reads_as_complete(self):
        progress = imports.import_progress(
            "==> step 1/2 a :: x -> /a\n==> step 1/2 a complete\n"
            "==> step 2/2 b :: y -> /b\n==> step 2/2 b complete\n==> done\n1.2G\t/appdata")

        self.assertEqual(100.0, progress["percent"])

    def test_output_without_markers_reports_nothing_rather_than_guessing(self):
        self.assertEqual({}, imports.import_progress("connecting...\nsome noise"))


if __name__ == "__main__":
    unittest.main()
