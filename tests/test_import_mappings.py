import copy
import sys
import unittest
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))

import homestead_imports as imports
import server


SOURCE = {"name": "tower", "host": "192.168.1.10", "user": "root",
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

        self.assertEqual([{"remote_path": "/mnt/user/appdata/plex", "mount_path": "/config",
                           "folder": "", "bytes": 0, "pvc": "app-appdata",
                           "medium": "", "size_mb": 0, "copy": True, "exclude": []}], rows)

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


class MultipleVolumeTests(unittest.TestCase):
    """Frigate's recordings do not belong on the same claim as its appdata."""

    FRIGATE = {
        "name": "frigate",
        "volumes": [{"name": "frigate-appdata", "create": True, "size_gb": 10},
                    {"name": "frigate-recordings", "create": True, "size_gb": 500,
                     "access_mode": "ReadWriteMany"}],
        "mappings": [
            {"remote_path": "/mnt/user/appdata/frigate", "mount_path": "/config"},
            {"remote_path": "/mnt/user/appdata/frigate/clips", "mount_path": "/clips",
             "pvc": "frigate-appdata"},
            {"remote_path": "/mnt/user/cctv", "mount_path": "/media/frigate/recordings",
             "pvc": "frigate-recordings"}],
    }

    def test_each_folder_lands_in_the_volume_it_was_given(self):
        rows = imports.import_mappings(self.FRIGATE)

        self.assertEqual(["frigate-appdata", "frigate-appdata", "frigate-recordings"],
                         [row["pvc"] for row in rows])

    def test_folders_sharing_a_volume_get_subdirectories_and_a_lone_one_does_not(self):
        rows = imports.import_mappings(self.FRIGATE)

        self.assertEqual(["frigate", "clips"], [row["folder"] for row in rows[:2]])
        self.assertEqual("", rows[2]["folder"],
                         "the only folder on a volume takes its root")

    def test_the_same_folder_name_on_two_volumes_is_not_a_collision(self):
        rows = imports.import_mappings({
            "name": "app",
            "volumes": [{"name": "app-one"}, {"name": "app-two"}],
            "mappings": [{"remote_path": "/a/data", "mount_path": "/one", "pvc": "app-one"},
                         {"remote_path": "/b/data", "mount_path": "/two", "pvc": "app-two"}]})

        self.assertEqual(["", ""], [row["folder"] for row in rows])

    def test_a_mapping_cannot_name_a_volume_the_import_does_not_have(self):
        with self.assertRaisesRegex(ValueError, "which this import does not create"):
            imports.import_mappings({"name": "app", "volumes": [{"name": "app-data"}],
                                     "mappings": [{"remote_path": "/a", "mount_path": "/x",
                                                   "pvc": "somewhere-else"}]})

    def test_duplicate_and_malformed_volumes_are_refused(self):
        with self.assertRaisesRegex(ValueError, "listed twice"):
            imports.import_volumes({"volumes": [{"name": "a"}, {"name": "a"}]})
        with self.assertRaisesRegex(ValueError, "lowercase letters"):
            imports.import_volumes({"volumes": [{"name": "Bad Name"}]})


class ScratchMountTests(unittest.TestCase):
    """Frigate's /tmp/cache is tmpfs on Unraid: RAM, not something to copy."""

    FRIGATE = {
        "name": "frigate",
        "volumes": [{"name": "frigate-appdata"}],
        "mappings": [{"remote_path": "/mnt/user/appdata/frigate", "mount_path": "/config"},
                     {"mount_path": "/tmp/cache", "medium": "memory", "size_mb": 1000}],
    }

    def test_a_scratch_mount_has_no_source_and_is_never_copied(self):
        rows = imports.import_mappings(self.FRIGATE)

        scratch = rows[1]
        self.assertEqual(("", "memory", 1000), (scratch["remote_path"], scratch["medium"],
                                                scratch["size_mb"]))
        self.assertEqual(0, scratch["bytes"], "there is nothing to measure")

    def test_scratch_does_not_push_the_real_folder_into_a_subdirectory(self):
        rows = imports.import_mappings(self.FRIGATE)

        self.assertEqual("", rows[0]["folder"],
                         "one copied folder still owns the volume root")

    def _scratch(self, **extra):
        return imports.import_mappings({"name": "app", "volumes": [{"name": "app-data"}],
                                        "mappings": [dict({"mount_path": "/tmp/cache",
                                                           "medium": "memory"}, **extra)]})[0]

    def test_an_unstated_size_defaults_to_a_gibibyte(self):
        self.assertEqual(1024, self._scratch()["size_mb"])
        self.assertEqual(1024, self._scratch(size_mb=0)["size_mb"],
                         "blank arrives as zero and means unstated")

    def test_a_scratch_larger_than_any_node_is_refused(self):
        with self.assertRaisesRegex(ValueError, "scratch size"):
            self._scratch(size_mb=99999999)

    def test_docker_tmpfs_options_become_a_size(self):
        self.assertEqual(953, imports._tmpfs_mb("rw,size=1000000000"))
        self.assertEqual(1024, imports._tmpfs_mb("rw,size=1g"))
        self.assertEqual(512, imports._tmpfs_mb("size=512m"))
        self.assertEqual(0, imports._tmpfs_mb("rw,noexec"))


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

    def test_a_folder_that_is_not_there_says_so_rather_than_timing_out(self):
        """The two look identical in a log and mean very different things."""
        self.lines = ["### /mnt/user/appdata/frigate", "MISSING",
                      "### /mnt/user/appdata/plex", "2048	/mnt/user/appdata/plex"]

        result = imports.measure_source_paths("tower", ["/mnt/user/appdata/frigate",
                                                        "/mnt/user/appdata/plex"])

        self.assertEqual(["/mnt/user/appdata/frigate"], result["missing"])
        self.assertFalse(result["paths"][0]["exists"])
        self.assertFalse(result["paths"][0]["timed_out"], "absent is not slow")
        self.assertTrue(result["paths"][1]["exists"])

    def test_a_folder_that_times_out_is_unknown_rather_than_zero(self):
        self.lines = ["### /mnt/user/appdata/plex/config", "1024	/mnt/user/appdata/plex/config",
                      "### /mnt/user/media", "TIMEOUT"]

        result = imports.measure_source_paths("tower", ["/mnt/user/appdata/plex/config",
                                                        "/mnt/user/media"])

        self.assertFalse(result["paths"][1]["measured"])
        self.assertIsNone(result["paths"][1]["bytes"])
        self.assertTrue(result["paths"][1]["timed_out"])
        self.assertTrue(result["paths"][1]["exists"], "slow is not absent")
        self.assertEqual([], result["missing"])
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

    def test_snapshot_allocation_is_not_treated_as_filesystem_usage(self):
        self.written = 20 * 1024 ** 3  # snapshots can exceed logical capacity
        self._import(mappings=[{"remote_path": "/data", "mount_path": "/config", "bytes": 1024 ** 3}])
        self.assertTrue(any("jobs" in path for _, path in self.sent))

    def test_a_claim_with_room_proceeds(self):
        self.claim_capacity = "50Gi"
        self.written = 0

        self._import()

        self.assertTrue(any("jobs" in path for _, path in self.sent))

    def test_an_unmeasured_import_is_never_second_guessed(self):
        self._import(mappings=[{"remote_path": "/mnt/user/appdata/ha", "mount_path": "/config"}])

        self.assertTrue(any("jobs" in path for _, path in self.sent))


class MountEmptyTests(unittest.TestCase):
    """A folder can be mounted without being copied: new recordings on a new
    volume, the old ones left on the source."""

    def setUp(self):
        self.bodies, self.built = [], []
        imports.bind(lambda path: {"items": []},
                     lambda method, path, body=None, **kw: self.bodies.append(body) or
                     {**(body or {}), "metadata": {**(body or {}).get("metadata", {}), "uid": "created-object"}},
                     lambda *a, **k: {},
                     lambda cfg: self.built.append(cfg) or ({"metadata": {"name": "frigate"}}, None), "lab", {})
        imports._source = lambda name: dict(SOURCE)

    def test_a_volume_mounted_empty_is_mapped_but_never_copied(self):
        imports.import_container({
            "source": "tower", "name": "frigate", "image": "frigate:1",
            "volumes": [{"name": "frigate-appdata", "size_gb": 5}, {"name": "frigate-media", "size_gb": 500}],
            "mappings": [{"remote_path": "/mnt/user/appdata/frigate", "mount_path": "/config", "pvc": "frigate-appdata"},
                         {"remote_path": "/mnt/user/media/frigate", "mount_path": "/media/frigate",
                          "pvc": "frigate-media", "copy": False}]})
        mounts = {v["path"]: v["source"] for v in self.built[0]["volumes"]}
        self.assertEqual({"/config": "frigate-appdata", "/media/frigate": "frigate-media"}, mounts)
        job = next(b for b in self.bodies if b and b.get("kind") == "Job")
        script = job["spec"]["template"]["spec"]["containers"][0]["command"][-1]
        self.assertIn("appdata/frigate", script)
        self.assertNotIn("media/frigate", script)
        self.assertIn("step 1/1", script)

    def test_mounted_empty_needs_no_source_folder(self):
        rows = imports.import_mappings({"mappings": [{"remote_path": "", "mount_path": "/media", "copy": False}]})
        self.assertEqual((False, 0), (rows[0]["copy"], rows[0]["bytes"]))


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

    def test_a_claim_can_be_handed_over_after_the_fact(self):
        result = imports.chown_claim("lab", "mosquitto-appdata", "1883", "")

        job = self.bodies[-1]
        self.assertEqual("homestead-chown-mosquitto-appdata", job["metadata"]["name"])
        self.assertEqual("chown", job["metadata"]["labels"]["homestead.io/task"])
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
            '==> step 1/1 appdata :: 192.168.1.177:/mnt/user/appdata/HomeAsssistantCore -> /config',
            'rsync: [receiver] write failed on "/appdata/home-assistant_v2.db": No space left on device (28)',
            "rsync error: error in file IO (code 11) at receiver.c(401) [receiver=3.4.3]",
        ])

        progress = imports.import_progress(log)

        self.assertEqual("ran out of space on the volume", progress["error"])
        self.assertIn("No space left on device", progress["error_detail"])

    def test_a_copy_that_never_starts_still_reports_why(self):
        progress = imports.import_progress(
            "==> checking the source folders exist\n==> missing /mnt/user/appdata/frigate")

        self.assertEqual("a source folder does not exist on the host", progress["error"])
        self.assertEqual("/mnt/user/appdata/frigate", progress["error_detail"])

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


class NestedFolderTests(unittest.TestCase):
    """Frigate keeps its recordings inside the folder holding its config."""

    FRIGATE = {
        "name": "frigate",
        "volumes": [{"name": "frigate-appdata"}, {"name": "frigate-recordings"}],
        "mappings": [
            {"remote_path": "/mnt/user/cctv/Frigate", "mount_path": "/config",
             "pvc": "frigate-appdata"},
            {"remote_path": "/mnt/user/cctv/Frigate/recordings", "mount_path": "/media/frigate",
             "pvc": "frigate-recordings"}],
    }

    def test_a_folder_mapped_on_its_own_is_kept_out_of_its_parent(self):
        rows = imports.import_mappings(self.FRIGATE)

        self.assertEqual(["/recordings"], rows[0]["exclude"],
                         "the recordings must not also ride along into appdata")
        self.assertEqual([], rows[1]["exclude"])

    def test_a_folder_left_unticked_is_excluded_too(self):
        """The client names what it did not select, so it stays behind."""
        cfg = copy.deepcopy(self.FRIGATE)
        cfg["mappings"] = [dict(cfg["mappings"][0], exclude=["clips/", "/recordings"])]

        rows = imports.import_mappings(cfg)

        self.assertEqual(["/clips", "/recordings"], rows[0]["exclude"])

    def test_an_exclude_cannot_climb_out_of_the_folder(self):
        cfg = copy.deepcopy(self.FRIGATE)
        cfg["mappings"] = [dict(cfg["mappings"][0], exclude=["../../etc", "", "/"])]

        self.assertEqual([], imports.import_mappings(cfg)[0]["exclude"])

    def test_a_sibling_with_a_similar_name_is_not_excluded(self):
        cfg = copy.deepcopy(self.FRIGATE)
        cfg["mappings"][1]["remote_path"] = "/mnt/user/cctv/Frigate-recordings"

        self.assertEqual([], imports.import_mappings(cfg)[0]["exclude"],
                         "Frigate-recordings is beside Frigate, not inside it")

    def test_the_copy_tells_rsync_to_leave_the_nested_folder_alone(self):
        sent = []
        imports.bind(lambda path: (_ for _ in ()).throw(AssertionError(path)),
                     lambda method, path, body=None, **kw: sent.append(body) or {},
                     lambda *a, **k: {}, lambda cfg: ({"metadata": {"name": "x"}}, None),
                     "lab", {})
        imports._source = lambda name: dict(SOURCE)
        imports.import_container(dict(self.FRIGATE, source="tower", image="frigate:1",
                                      create_workload=False))

        script = sent[-1]["spec"]["template"]["spec"]["containers"][0]["command"][-1]
        appdata, recordings = [line for line in script.splitlines() if line.startswith("sshpass -p \"$SRC_PASS\" rsync")]
        self.assertIn("--exclude=/recordings/ ", appdata)
        self.assertNotIn("--exclude", recordings, "the recordings copy excludes nothing")

    def test_a_parent_is_measured_without_what_the_copy_will_skip(self):
        rows = [{"path": "/mnt/user/cctv/Frigate", "bytes": 5 * 1024 ** 3, "measured": True},
                {"path": "/mnt/user/cctv/Frigate/recordings", "bytes": 4 * 1024 ** 3,
                 "measured": True}]

        imports._deduct_nested(rows)

        self.assertEqual(1024 ** 3, rows[0]["bytes"], "du counted the recordings twice")
        self.assertEqual(5 * 1024 ** 3, rows[0]["gross_bytes"])
        self.assertEqual(4 * 1024 ** 3, rows[1]["bytes"])

    def test_a_three_deep_tree_does_not_deduct_the_same_bytes_twice(self):
        rows = [{"path": "/a", "bytes": 100, "measured": True},
                {"path": "/a/b", "bytes": 60, "measured": True},
                {"path": "/a/b/c", "bytes": 20, "measured": True}]

        imports._deduct_nested(rows)

        self.assertEqual([40, 40, 20], [row["bytes"] for row in rows])


class SharedMemoryTests(unittest.TestCase):
    """--shm-size has no Kubernetes equivalent, so it has to be carried over."""

    def test_more_than_the_default_is_reported(self):
        self.assertEqual(512, imports._shm_mb(512 * 1024 ** 2))

    def test_the_default_and_below_is_not_worth_carrying(self):
        self.assertEqual(0, imports._shm_mb(64 * 1024 ** 2), "a pod gets this anyway")
        self.assertEqual(0, imports._shm_mb(0))
        self.assertEqual(0, imports._shm_mb(None))

    def test_a_shm_volume_is_memory_backed_and_copies_nothing(self):
        rows = imports.import_mappings({
            "name": "frigate", "volumes": [{"name": "frigate-appdata"}],
            "mappings": [{"remote_path": "/mnt/user/cctv/Frigate", "mount_path": "/config"},
                         {"mount_path": "/dev/shm", "medium": "memory", "size_mb": 512}]})

        self.assertEqual({"remote_path": "", "mount_path": "/dev/shm", "medium": "memory",
                          "size_mb": 512, "bytes": 0},
                         {key: rows[1][key] for key in
                          ("remote_path", "mount_path", "medium", "size_mb", "bytes")})


class NoStorageTests(unittest.TestCase):
    """Plenty of containers keep nothing: a relay, a bridge, a snapshot helper."""

    CFG = {"source": "tower", "name": "ha-snapshot", "image": "ha-snapshot:1",
           "mappings": [], "volumes": []}

    def test_no_claim_is_invented_for_a_container_that_keeps_nothing(self):
        self.assertEqual([], imports.import_volumes(self.CFG))

    def test_no_mappings_means_no_mappings(self):
        self.assertEqual([], imports.import_mappings(self.CFG))

    def test_the_older_single_folder_shape_still_gets_its_default(self):
        """A client that sends no mappings key at all predates the list."""
        rows = imports.import_mappings({"name": "plex", "remote_path": "/mnt/user/appdata/plex"})

        self.assertEqual("/mnt/user/appdata/plex", rows[0]["remote_path"])
        self.assertEqual(["plex-appdata"],
                         [v["name"] for v in imports.import_volumes({"name": "plex"})])

    def test_a_ram_only_container_still_gets_its_scratch_but_no_claim(self):
        cfg = dict(self.CFG, mappings=[{"mount_path": "/tmp/cache", "medium": "memory",
                                        "size_mb": 512}])

        self.assertEqual([], imports.import_volumes(cfg))
        self.assertEqual("memory", imports.import_mappings(cfg)[0]["medium"])


class ConfigMountTests(unittest.TestCase):
    """Which folder an imported container keeps its configuration in."""

    def test_appdata_wins_when_the_container_mounts_it(self):
        mounts = [{"source": "/mnt/user/cctv", "path": "/media"},
                  {"source": "/mnt/user/appdata/frigate", "path": "/config"}]

        chosen = imports._config_mount(mounts, "/mnt/user/appdata")

        self.assertEqual("/mnt/user/appdata/frigate", chosen["source"])

    def test_a_config_path_outside_appdata_is_used_rather_than_invented(self):
        """Frigate on Unraid often keeps its config beside the recordings."""
        mounts = [{"source": "/mnt/user/cctv/Frigate/recordings", "path": "/media/frigate"},
                  {"source": "/mnt/user/cctv/Frigate", "path": "/config"}]

        chosen = imports._config_mount(mounts, "/mnt/user/appdata")

        self.assertEqual("/mnt/user/cctv/Frigate", chosen["source"],
                         "the real path on the host, not /mnt/user/appdata/frigate")

    def test_nothing_recognisable_means_nothing_is_guessed(self):
        mounts = [{"source": "/mnt/user/films", "path": "/films"},
                  {"source": "", "path": "/tmp/cache", "type": "tmpfs"}]

        self.assertIsNone(imports._config_mount(mounts, "/mnt/user/appdata"))
