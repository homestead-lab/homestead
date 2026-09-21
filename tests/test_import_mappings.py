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
                           "mount_path": "/config", "folder": "", "bytes": 0}], rows)

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


class SourceMeasurementTests(unittest.TestCase):
    """du before the copy, so the volume is sized from facts not a default."""

    def setUp(self):
        self.script = ""
        imports.SOURCES = None

        def probe(tag, script, src, timeout=70):
            self.script = script
            return self.lines

        self.probe = imports.run_probe
        imports.run_probe = probe
        imports._source = lambda name: dict(SOURCE)

    def tearDown(self):
        imports.run_probe = self.probe

    def test_sizes_come_back_per_folder_with_a_suggested_volume(self):
        self.lines = ["### /mnt/user/appdata/plex/config", "1024	/mnt/user/appdata/plex/config",
                      "### /mnt/user/appdata/plex/media", "10485760	/mnt/user/appdata/plex/media"]

        result = imports.measure_source_paths("tower", ["/mnt/user/appdata/plex/config",
                                                        "/mnt/user/appdata/plex/media"])

        self.assertEqual([1048576, 10737418240], [row["bytes"] for row in result["paths"]])
        self.assertTrue(result["complete"])
        self.assertEqual(13, result["suggested_gb"], "measured size plus headroom")

    def test_a_folder_that_times_out_is_unknown_rather_than_zero(self):
        self.lines = ["### /mnt/user/appdata/plex/config", "1024	/mnt/user/appdata/plex/config",
                      "### /mnt/user/media", "UNKNOWN"]

        result = imports.measure_source_paths("tower", ["/mnt/user/appdata/plex/config",
                                                        "/mnt/user/media"])

        self.assertFalse(result["paths"][1]["measured"])
        self.assertIsNone(result["paths"][1]["bytes"])
        self.assertFalse(result["complete"], "a partial measurement says so")

    def test_every_folder_is_measured_under_its_own_timeout(self):
        self.lines = []
        imports.measure_source_paths("tower", ["/a", "/b"], seconds=30)

        self.assertEqual(2, self.script.count("timeout 30 du -sk"))

    def test_paths_must_be_absolute(self):
        self.lines = []
        with self.assertRaisesRegex(ValueError, "must be absolute"):
            imports.measure_source_paths("tower", ["relative/path"])


class ImportCapacityTests(unittest.TestCase):
    """A measured source and a known claim size settle this before copying."""

    def setUp(self):
        self.created, self.sent = [], []
        self.claim_capacity = "10Gi"
        self.written = 9.95 * 1024 ** 3

        def get(path):
            if "persistentvolumeclaims/" in path:
                return {"metadata": {"name": "ha-appdata"},
                        "spec": {"accessModes": ["ReadWriteOnce"], "storageClassName": "longhorn"},
                        "status": {"phase": "Bound", "capacity": {"storage": self.claim_capacity}}}
            if "longhorn.io" in path:
                return {"items": [{"status": {"actualSize": int(self.written),
                                              "kubernetesStatus": {"namespace": "lab",
                                                                   "pvcName": "ha-appdata"}}}]}
            raise AssertionError(path)

        imports.bind(get, lambda method, path, body=None, **kw: self.sent.append((method, path)) or {},
                     lambda *a, **k: self.created.append(a) or {},
                     lambda cfg: ({"metadata": {"name": "x"}}, None), "lab", {})
        imports._source = lambda name: dict(SOURCE)

    def _import(self, **extra):
        cfg = {"source": "tower", "name": "ha", "image": "ha:1", "pvc_name": "ha-appdata",
               "reuse_existing": True, "create_workload": False,
               "mappings": [{"remote_path": "/mnt/user/appdata/ha", "mount_path": "/config",
                             "bytes": int(11.8 * 1024 ** 3)}]}
        cfg.update(extra)
        return imports.import_container(cfg)

    def test_an_import_that_cannot_fit_is_refused_before_it_starts(self):
        with self.assertRaisesRegex(ValueError, "11.8 GiB but ha-appdata has"):
            self._import()
        self.assertEqual([], self.sent, "no job is created for a copy that must fail")

    def test_the_refusal_names_what_is_already_written(self):
        with self.assertRaisesRegex(ValueError, "9.9 GiB already written"):
            self._import()

    def test_a_claim_with_room_proceeds(self):
        self.claim_capacity = "50Gi"
        self.written = 0

        self._import()

        self.assertTrue(any("jobs" in path for _, path in self.sent))

    def test_an_unmeasured_import_is_never_second_guessed(self):
        self._import(mappings=[{"remote_path": "/mnt/user/appdata/ha", "mount_path": "/config"}])

        self.assertTrue(any("jobs" in path for _, path in self.sent))


class OwnershipTests(unittest.TestCase):
    """Copied appdata has to belong to whoever the container runs as."""

    def setUp(self):
        self.sent, self.bodies = [], []

        def send(method, path, body=None, **kw):
            self.sent.append((method, path))
            self.bodies.append(body)
            return body or {}

        imports.bind(lambda path: {"items": []}, send, lambda *a, **k: {},
                     lambda cfg: ({"metadata": {"name": "x"}}, None), "lab", {})
        imports._source = lambda name: dict(SOURCE)

    def _script(self):
        job = next(body for body in self.bodies if body and body.get("kind") == "Job")
        return job["spec"]["template"]["spec"]["containers"][0]["command"][-1]

    def test_the_copy_keeps_the_ownership_the_source_had(self):
        """A fresh deployment writes its own files; an import must not undo that."""
        imports.import_container({"source": "tower", "name": "z2m", "image": "z2m:1",
                                  "create_workload": False, "reuse_existing": True,
                                  "pvc_name": "z2m-appdata", "remote_path": "/mnt/user/appdata/z2m"})

        script = self._script()
        self.assertIn("--numeric-ids", script)
        for dropped in ("--no-owner", "--no-group", "--no-perms"):
            self.assertNotIn(dropped, script)
        self.assertNotIn("chown", script, "nothing to correct when ownership survives the copy")

    def test_an_override_still_hands_the_files_over(self):
        imports.import_container({"source": "tower", "name": "ha", "image": "ha:1",
                                  "create_workload": False, "reuse_existing": True,
                                  "pvc_name": "ha-appdata", "remote_path": "/mnt/user/appdata/ha",
                                  "env": {"PUID": "1000", "PGID": "1000"}})

        self.assertIn("chown -R 1000:1000 /appdata", self._script())

    def test_an_explicit_owner_beats_the_environment(self):
        imports.import_container({"source": "tower", "name": "mq", "image": "mosquitto:2",
                                  "create_workload": False, "reuse_existing": True,
                                  "pvc_name": "mq-appdata", "remote_path": "/mnt/user/appdata/mq",
                                  "env": {"PUID": "1000"}, "uid": "1883"})

        self.assertIn("chown -R 1883:1883 /appdata", self._script())

    def test_without_an_owner_nothing_is_chowned(self):
        imports.import_container({"source": "tower", "name": "plain", "image": "app:1",
                                  "create_workload": False, "reuse_existing": True,
                                  "pvc_name": "plain-appdata", "remote_path": "/mnt/user/appdata/plain"})

        self.assertNotIn("chown", self._script())

    def test_jobs_are_named_after_homestead(self):
        imports.import_container({"source": "tower", "name": "plex", "image": "plex:1",
                                  "create_workload": False, "reuse_existing": True,
                                  "pvc_name": "plex-appdata", "remote_path": "/mnt/user/appdata/plex"})

        created = [body for body in self.bodies if body and body.get("kind") == "Job"]
        self.assertEqual("homestead-import-plex", created[-1]["metadata"]["name"])
        cleared = [path for method, path in self.sent if method == "DELETE"]
        self.assertTrue(any("harvui-import-plex" in path for path in cleared),
                        "a job from before the rename is cleared too")

    def test_a_claim_can_be_handed_over_after_the_fact(self):
        result = imports.chown_claim("lab", "mosquitto-appdata", "1883", "")

        job = self.bodies[-1]
        self.assertEqual("homestead-chown-mosquitto-appdata", job["metadata"]["name"])
        self.assertEqual("chown", job["metadata"]["labels"]["harvui.io/task"])
        self.assertIn("chown -R 1883:1883 /data",
                      job["spec"]["template"]["spec"]["containers"][0]["command"][-1])
        self.assertEqual("mosquitto-appdata",
                         job["spec"]["template"]["spec"]["volumes"][0]["persistentVolumeClaim"]["claimName"])
        self.assertIn("1883:1883", result["message"])

    def test_nonsense_ids_are_refused(self):
        for uid in ("root", "-5", "99999999"):
            with self.subTest(uid=uid):
                with self.assertRaises(ValueError):
                    imports.chown_claim("lab", "media", uid, "")


class OwnershipHintTests(unittest.TestCase):
    """Nobody knows a container's uid by heart; the workload already says."""

    def _deployment(self, container):
        return {"items": [{
            "metadata": {"name": "app", "namespace": "lab"},
            "spec": {"template": {"spec": {
                "volumes": [{"name": "data", "persistentVolumeClaim": {"claimName": "app-appdata"}}],
                "containers": [dict(container, volumeMounts=[{"name": "data", "mountPath": "/data"}])],
                **self.pod_security,
            }}},
        }]}

    def _bind(self, container, **pod):
        self.pod_security = pod
        imports.bind(lambda path: self._deployment(container), lambda *a, **k: {},
                     lambda *a, **k: {}, lambda cfg: ({}, None), "lab", {})

    def test_puid_and_pgid_are_the_first_answer(self):
        self._bind({"name": "app", "image": "lscr.io/linuxserver/app",
                    "env": [{"name": "PUID", "value": "1000"}, {"name": "PGID", "value": "1000"}]})

        hint = imports.ownership_hint("lab", "app-appdata")

        self.assertEqual((1000, 1000, True), (hint["uid"], hint["gid"], hint["known"]))
        self.assertIn("PUID/PGID", hint["source"])

    def test_a_security_context_answers_when_the_environment_does_not(self):
        self._bind({"name": "app", "image": "mosquitto:2",
                    "securityContext": {"runAsUser": 1883, "runAsGroup": 1883}})

        hint = imports.ownership_hint("lab", "app-appdata")

        self.assertEqual(1883, hint["uid"])
        self.assertIn("security context", hint["source"])

    def test_an_fs_group_alone_still_names_a_group(self):
        self._bind({"name": "app", "image": "app:1"}, securityContext={"fsGroup": 568})

        hint = imports.ownership_hint("lab", "app-appdata")

        self.assertEqual(568, hint["gid"])
        self.assertTrue(hint["known"])

    def test_a_workload_that_says_nothing_says_so_and_names_the_image(self):
        self._bind({"name": "app", "image": "eclipse-mosquitto:2"})

        hint = imports.ownership_hint("lab", "app-appdata")

        self.assertFalse(hint["known"])
        self.assertIsNone(hint["uid"])
        self.assertIn("eclipse-mosquitto:2", hint["source"])

    def test_an_unused_volume_is_not_second_guessed(self):
        self._bind({"name": "app", "image": "app:1"})

        hint = imports.ownership_hint("lab", "some-other-claim")

        self.assertFalse(hint["known"])
        self.assertIn("nothing mounts this volume", hint["source"])


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

    def test_a_measured_import_counts_bytes_not_folders(self):
        """A 1 MiB config folder is not half a copy that also moves 100 MiB."""
        log = "\n".join([
            "==> total 2 folders 110100480B",
            "==> step 1/2 config (1048576B) :: a -> /config",
            "==> step 1/2 config complete",
            "==> step 2/2 media (109051904B) :: b -> /media",
            "  50,000  50%  9MB/s  0:00:30",
        ])

        progress = imports.import_progress(log)

        self.assertTrue(progress["weighted"])
        self.assertEqual(50.5, progress["percent"])
        self.assertEqual(110100480, progress["total_bytes"])

    def test_without_measurements_it_falls_back_to_counting_folders(self):
        log = "\n".join([
            "==> step 1/2 config :: a -> /config",
            "==> step 1/2 config complete",
            "==> step 2/2 media :: b -> /media",
            "  50,000  50%  9MB/s  0:00:30",
        ])

        progress = imports.import_progress(log)

        self.assertFalse(progress["weighted"])
        self.assertEqual(75.0, progress["percent"])

    def test_running_out_of_space_is_named_not_left_in_the_log(self):
        log = "\n".join([
            '==> step 1/1 appdata :: 192.0.2.177:/mnt/user/appdata/HomeAsssistantCore -> /config',
            'rsync: [receiver] write failed on "/appdata/home-assistant_v2.db": No space left on device (28)',
            "rsync error: error in file IO (code 11) at receiver.c(401) [receiver=3.4.3]",
        ])

        progress = imports.import_progress(log)

        self.assertEqual("ran out of space on the volume", progress["error"])
        self.assertIn("No space left on device", progress["error_detail"])

    def test_a_refused_connection_is_named_too(self):
        progress = imports.import_progress(
            "==> step 1/1 appdata :: a -> /config\nssh: connect to host 10.0.0.9 port 22: Connection refused")

        self.assertEqual("the source host refused the connection", progress["error"])

    def test_output_without_markers_reports_nothing_rather_than_guessing(self):
        self.assertEqual({}, imports.import_progress("connecting...\nsome noise"))


if __name__ == "__main__":
    unittest.main()


class FsGroupTests(unittest.TestCase):
    """fsGroup keeps the volume writable for a container that is not root."""

    def setUp(self):
        self.features = server.HW.features
        server.HW.features = lambda: []

    def tearDown(self):
        server.HW.features = self.features

    def test_an_owner_group_becomes_the_pod_fs_group(self):
        deployment, _ = server.build_deployment({
            "name": "mq", "workload_name": "mq", "container_name": "mq", "image": "mosquitto:2",
            "namespace": "lab", "fs_group": 1883,
            "volumes": [{"path": "/mosquitto/data", "source": "mq-appdata", "type": "pvc"}]})

        spec = deployment["spec"]["template"]["spec"]
        self.assertEqual(1883, spec["securityContext"]["fsGroup"])

    def test_no_owner_leaves_the_pod_security_context_alone(self):
        deployment, _ = server.build_deployment({
            "name": "app", "workload_name": "app", "container_name": "app", "image": "app:1",
            "namespace": "lab", "volumes": []})

        self.assertNotIn("securityContext", deployment["spec"]["template"]["spec"])
