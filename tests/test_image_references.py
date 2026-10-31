"""The image cache lists every image a node holds, and never calls one unused
while a stopped container or a scheduled job still starts from it."""
import json
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
import urllib.error
import homestead_imports as imports
import homestead_image_cache as IMAGES

VMS = "/apis/kubevirt.io/v1/virtualmachines"
POOLS = "/apis/pool.kubevirt.io/v1alpha1/virtualmachinepools"
VMIRS = "/apis/kubevirt.io/v1/virtualmachineinstancereplicasets"
TEMPLATES = "/apis/harvesterhci.io/v1beta1/virtualmachinetemplateversions"
CONTENTS = "/apis/snapshot.kubevirt.io/v1beta1/virtualmachinesnapshotcontents"
DVS = "/apis/cdi.kubevirt.io/v1beta1/datavolumes"
CRONS = "/apis/cdi.kubevirt.io/v1beta1/dataimportcrons"
# Groups that may not be installed; each is read at the version discovery prefers.
OPTIONAL = {"pool.kubevirt.io": "v1alpha1", "snapshot.kubevirt.io": "v1beta1",
            "harvesterhci.io": "v1beta1", "cdi.kubevirt.io": "v1beta1"}
SERVED = {"pool.kubevirt.io": ["virtualmachinepools", "virtualmachinepools/status"],
          "snapshot.kubevirt.io": ["virtualmachinesnapshots", "virtualmachinesnapshotcontents"],
          "harvesterhci.io": ["virtualmachinetemplateversions"],
          "cdi.kubevirt.io": ["datavolumes", "dataimportcrons"]}
ESPHOME = "sha256:" + "e" * 64
BACKUP = "sha256:" + "f" * 64


def ready(name, images):
    return {"metadata": {"name": name},
            "status": {"conditions": [{"type": "Ready", "status": "True"}], "images": images}}


def api_group(name, version):
    """GET /apis/<group>, as the API server answers it."""
    served = {"groupVersion": f"{name}/{version}", "version": version}
    return {"kind": "APIGroup", "apiVersion": "v1", "name": name, "versions": [served], "preferredVersion": served}


class Fixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.saved = imports.SCAN_DIR, dict(IMAGES._SCANS), IMAGES._SCAN_STARTED[0]
        imports.SCAN_DIR = self.tmp.name
        IMAGES._SCANS.clear()
        IMAGES._SCAN_STARTED[0] = 0.0
        self.sent = []
        self.nodes = [ready("node1", [
            {"names": ["ghcr.io/esphome/esphome:2025.9.0", "ghcr.io/esphome/esphome@" + ESPHOME], "sizeBytes": 600 << 20},
            {"names": ["docker.io/restic/restic:0.17", "docker.io/restic/restic@" + BACKUP], "sizeBytes": 30 << 20},
            {"names": ["docker.io/library/unused:1"], "sizeBytes": 10 << 20},
            {"names": ["quay.io/containerdisks/fedora:41"], "sizeBytes": 500 << 20},
            {"names": ["quay.io/containerdisks/debian:12"], "sizeBytes": 400 << 20}])]
        self.objects = {
            "/api/v1/pods": {"items": []},
            **{f"/apis/{group}": api_group(group, version) for group, version in OPTIONAL.items()},
            **{f"/apis/{group}/{version}": {"resources": [{"name": name} for name in SERVED[group]]}
               for group, version in OPTIONAL.items()},
            TEMPLATES: {"items": []}, CONTENTS: {"items": []}, DVS: {"items": []}, CRONS: {"items": []},
            POOLS: {"items": []},
            VMIRS: {"items": []},
            "/apis/apps/v1/deployments": {"items": [
                {"metadata": {"name": "esphome", "namespace": "lab"},
                 "spec": {"replicas": 0, "template": {"spec": {"containers": [
                     {"name": "esphome", "image": "ghcr.io/esphome/esphome:2025.9.0"}]}}}},
                {"metadata": {"name": "web", "namespace": "lab"},
                 "spec": {"replicas": 1, "template": {"spec": {"containers": [
                     {"name": "web", "image": "docker.io/library/unused:1"}]}}},
                 # Its pod is running: the status says every replica it wants is there.
                 "status": {"replicas": 1, "updatedReplicas": 1}}]},
            "/apis/apps/v1/statefulsets": {"items": []},
            "/apis/apps/v1/daemonsets": {"items": []},
            "/apis/batch/v1/jobs": {"items": []},
            "/apis/batch/v1/cronjobs": {"items": [
                {"metadata": {"name": "backup", "namespace": "lab"},
                 "spec": {"jobTemplate": {"spec": {"template": {"spec": {"containers": [
                     {"name": "restic", "image": "restic/restic:0.17"}]}}}}}}]},
            VMS: {"items": [
                {"metadata": {"name": "lab-vm", "namespace": "lab"},
                 "spec": {"runStrategy": "Halted", "template": {"spec": {"volumes": [
                     {"name": "rootdisk", "containerDisk": {"image": "quay.io/containerdisks/fedora:41"}},
                     {"name": "cloudinit", "cloudInitNoCloud": {"userData": "#cloud-config"}}]}}}},
                {"metadata": {"name": "web-vm", "namespace": "lab"},
                 "spec": {"runStrategy": "Always", "template": {"spec": {"volumes": [
                     {"name": "rootdisk", "containerDisk": {"image": "quay.io/containerdisks/debian:12"}}]}}},
                 "status": {"printableStatus": "Running", "ready": True}}]},
        }

        self.asked = []

        def get(path):
            self.asked.append(path)
            if path == "/api/v1/nodes":
                return {"items": self.nodes}
            if "/pods?" in path or "/daemonsets?" in path:
                return {"items": []}
            return self.objects[path]

        def send(method, path, body=None, **kwargs):
            self.sent.append((method, path, body))
            return body or {}
        self.restore = imports.kget, imports.ksend
        imports.kget, imports.ksend = get, send
        imports.NAMES.bind(get)

    def tearDown(self):
        imports.kget, imports.ksend = self.restore
        imports.SCAN_DIR = self.saved[0]
        IMAGES._SCANS.clear(); IMAGES._SCANS.update(self.saved[1])
        IMAGES._SCAN_STARTED[0] = self.saved[2]
        self.tmp.cleanup()

    def image(self, report, name):
        return next(row for row in report["images"] if name in row["name"])


class StoppedReferenceTests(Fixture):
    def test_a_stopped_containers_image_is_kept_and_says_why(self):
        esphome = self.image(IMAGES.image_cache(), "esphome")
        self.assertTrue(esphome["protected"])
        self.assertEqual([("stopped", "esphome")], [(r["reason"], r["workload"]) for r in esphome["retained_by"]])

    def test_it_cannot_be_cleaned_up_while_the_container_is_there(self):
        with self.assertRaisesRegex(PermissionError, "stopped:lab/esphome"):
            IMAGES.cleanup_image(ESPHOME, ["node1"])

    def test_a_scheduled_jobs_image_is_kept_between_runs(self):
        restic = self.image(IMAGES.image_cache(), "restic")
        self.assertEqual(["scheduled"], [r["reason"] for r in restic["retained_by"]])

    def test_a_running_workload_is_named_by_its_pods_not_as_stopped(self):
        self.assertFalse(self.image(IMAGES.image_cache(), "unused")["protected"])

    def test_a_stopped_vms_container_disk_is_kept_and_says_why(self):
        fedora = self.image(IMAGES.image_cache(), "fedora")
        self.assertTrue(fedora["protected"])
        self.assertEqual([("stopped", "lab", "lab-vm", "rootdisk")],
                         [(r["reason"], r["namespace"], r["workload"], r["container"]) for r in fedora["retained_by"]])

    def test_a_stopped_vms_kernel_boot_container_is_kept(self):
        vm = self.objects[VMS]["items"][0]
        vm["spec"]["template"]["spec"]["domain"] = {"firmware": {"kernelBoot": {"container": {
            "image": "docker.io/library/unused:1", "kernelPath": "/boot/vmlinuz"}}}}
        unused = self.image(IMAGES.image_cache(), "unused")
        self.assertTrue(unused["protected"])
        self.assertIn(("stopped", "lab-vm", "kernel-boot"),
                      [(r["reason"], r["workload"], r["container"]) for r in unused["retained_by"]])

    def test_a_running_vm_is_named_by_its_pod_not_as_stopped(self):
        self.assertFalse(self.image(IMAGES.image_cache(), "debian")["protected"])

    def test_the_digest_it_last_ran_on_holds_the_image_whatever_its_tag(self):
        dep = self.objects["/apis/apps/v1/deployments"]["items"][0]
        dep["spec"]["template"]["spec"]["containers"][0]["image"] = "ghcr.io/esphome/esphome:stable"
        dep["metadata"]["annotations"] = {"homestead.io/ran-digests": '{"esphome": "%s"}' % ESPHOME}
        self.assertTrue(self.image(IMAGES.image_cache(), "esphome")["protected"])


class ScanTests(Fixture):
    LISTING = [{"repoTags": ["ghcr.io/esphome/esphome:2025.9.0"], "repoDigests": ["ghcr.io/esphome/esphome@" + ESPHOME],
                "size": str(600 << 20)},
               {"repoTags": ["docker.io/library/small:1"], "repoDigests": [], "size": "1024"}]

    def scanned(self, at):
        IMAGES._SCANS["node1"] = {"at": at, "images": self.LISTING}
        IMAGES._save_scans()

    def test_a_scan_is_seen_by_another_replica_and_after_a_restart(self):
        self.scanned(time.time())
        IMAGES._SCANS.clear()
        report = IMAGES.image_cache()
        self.assertTrue(report["complete"])
        self.assertIn("docker.io/library/small:1", {row["name"] for row in report["images"]})

    def test_an_old_scan_is_still_used_with_what_kubernetes_lists_added(self):
        self.scanned(time.time() - IMAGES.SCAN_FRESH - 60)
        names = {row["name"] for row in IMAGES.image_cache()["images"]}
        # small:1 only the scan knows; restic only Kubernetes lists (pulled since).
        self.assertTrue({"docker.io/library/small:1", "docker.io/restic/restic:0.17"} <= names)

    def test_an_old_scan_is_asked_again_but_not_over_and_over(self):
        self.scanned(time.time() - IMAGES.SCAN_FRESH - 60)
        with mock.patch.object(imports.RUNTIME, "pod", lambda *a, **k: {"spec": {}}):
            IMAGES.image_cache()
            self.assertEqual([], [p for m, p, _ in self.sent if m == "POST"], "looking starts nothing (#622)")
            IMAGES.tick()
            IMAGES.tick()
        posts = [path for method, path, _ in self.sent if method == "POST" and path.endswith("/pods")]
        self.assertEqual(1, len(posts), "the leader's tick asks once, not every minute")

    def test_a_cleaned_image_leaves_the_saved_scan(self):
        self.scanned(time.time())
        dep = self.objects["/apis/apps/v1/deployments"]["items"][0]
        # Running, with its pod on another node: nothing here holds it.
        dep["spec"]["replicas"], dep["status"] = 1, {"replicas": 1, "updatedReplicas": 1}
        with mock.patch.object(imports.RUNTIME, "pod", lambda *a, **k: {"spec": {}}):
            IMAGES.cleanup_image(ESPHOME, ["node1"])
        IMAGES._SCANS.clear()
        IMAGES._load_scans()
        self.assertEqual([["docker.io/library/small:1"]], [i["repoTags"] for i in IMAGES._SCANS["node1"]["images"]])


class RemovalFixture(Fixture):
    """The fixture, with the unused image removable by its digest."""
    UNUSED = "sha256:" + "a" * 64

    def setUp(self):
        super().setUp()
        self.nodes[0]["status"]["images"][2]["names"].append("docker.io/library/unused@" + self.UNUSED)
        # A removal pod needs the runtime's paths, which come from the platform;
        # since #662 an unread platform is an error, not a guess, so give it one.
        platform = mock.patch("homestead_platform.detect", lambda *_: {"distribution": "k3s"})
        platform.start()
        self.addCleanup(platform.stop)

    def posted(self):
        """Removal pods started (a scan pod may start too; it removes nothing)."""
        return [body["metadata"]["name"] for method, path, body in self.sent if method == "POST"
                and body["metadata"]["name"].startswith("homestead-image-clean-")]

    def kept(self, path=None, item=None):
        """(reason, workload, container) of each row keeping the unused image,
        after adding `item` to the list at `path`."""
        if path:
            self.objects[path]["items"].append(item)
        rows = self.image(IMAGES.image_cache(), "unused")["retained_by"]
        return [(r["reason"], r["workload"], r["container"]) for r in rows]


class UnreadInventoryTests(RemovalFixture):
    """Every source of "still needed" is read in full, or nothing is removed (#620)."""
    LISTS = ("/api/v1/pods", "/apis/apps/v1/deployments", "/apis/apps/v1/statefulsets", "/apis/batch/v1/cronjobs",
             "/apis/apps/v1/daemonsets", "/apis/batch/v1/jobs", VMS, POOLS, VMIRS,
             TEMPLATES, CONTENTS, DVS, CRONS, *(f"/apis/{group}" for group in OPTIONAL),
             *(f"/apis/{group}/{version}" for group, version in OPTIONAL.items()))

    def test_an_image_nothing_needs_is_cleaned_up(self):
        IMAGES.cleanup_image(self.UNUSED, ["node1"])
        self.assertEqual(1, len(self.posted()))

    def test_nothing_is_removed_when_a_list_cannot_be_read(self):
        read = imports.kget
        for failed in self.LISTS:
            for how, answer in (("timed out", OSError("timed out")),
                                ("cut short", {"items": [], "metadata": {"continue": "next"}})):
                def get(path, failed=failed, answer=answer):
                    if path == failed:
                        if isinstance(answer, Exception):
                            raise answer
                        return answer
                    return read(path)
                with self.subTest(failed=failed, how=how), mock.patch.object(imports, "kget", get):
                    with self.assertRaisesRegex(ValueError, "no image is cleaned up"):
                        IMAGES.cleanup_image(self.UNUSED, ["node1"])
                    self.assertEqual([], self.posted())

    def kubevirt_answers_404(self, served):
        """The VM list answers 404; discovery answers with these groups, or
        raises when given an exception."""
        read = imports.kget

        def get(path):
            if path == VMS:
                raise urllib.error.HTTPError(path, 404, "Not Found", {}, None)
            if path == "/apis":
                if isinstance(served, Exception):
                    raise served
                return {"groups": [{"name": name} for name in served]}
            return read(path)
        return mock.patch.object(imports, "kget", get)

    # Today's behaviour, pinned before the unreadable-state changes: what can
    # be read still decides alone, and only an unreadable record keeps more.
    def test_a_rollback_annotation_keeps_only_the_image_it_names(self):
        web = self.objects["/apis/apps/v1/deployments"]["items"][1]
        web["metadata"]["annotations"] = {"homestead.io/update-previous": json.dumps(
            {"images": {"web": "docker.io/library/unused@" + self.UNUSED}, "at": "2026-10-01T00:00:00Z"})}
        report = IMAGES.image_cache()
        self.assertEqual([("rollback", "web")],
                         [(r["reason"], r["workload"]) for r in self.image(report, "unused")["retained_by"]])
        self.assertFalse(self.image(report, "debian")["protected"])

    def test_a_rollback_annotation_naming_no_images_keeps_nothing(self):
        web = self.objects["/apis/apps/v1/deployments"]["items"][1]
        web["metadata"]["annotations"] = {"homestead.io/update-previous": json.dumps({"images": {}})}
        IMAGES.cleanup_image(self.UNUSED, ["node1"])
        self.assertEqual(1, len(self.posted()))

    def test_an_unreadable_ran_digests_note_still_keeps_the_image_by_name(self):
        dep = self.objects["/apis/apps/v1/deployments"]["items"][0]
        dep["metadata"]["annotations"] = {"homestead.io/ran-digests": "not json"}
        self.assertTrue(self.image(IMAGES.image_cache(), "esphome")["protected"])

    # Gap 11: a record Homestead cannot read keeps every image until it can,
    # and says whose it is, rather than reading as "this keeps nothing".
    def assert_everything_kept_by(self, workload, what):
        report = IMAGES.image_cache()
        for name in ("unused", "debian"):
            rows = self.image(report, name)["retained_by"]
            self.assertIn(("unreadable", "lab", workload, what),
                          [(r["reason"], r["namespace"], r["workload"], r["container"]) for r in rows])
        with self.assertRaisesRegex(PermissionError, f"unreadable:lab/{workload}"):
            IMAGES.cleanup_image(self.UNUSED, ["node1"])
        self.assertEqual([], self.posted())
        import homestead_image_cleanup as cleanup
        self.assertEqual([], cleanup.unused(report))

    def test_an_unreadable_rollback_annotation_keeps_every_image(self):
        web = self.objects["/apis/apps/v1/deployments"]["items"][1]
        for value in ("{not json", "[]", '"text"', json.dumps({"images": ["nginx"]}), json.dumps({"images": []}),
                      json.dumps({"images": None}), json.dumps({"images": ""}),
                      json.dumps({"images": {"web": 7}})):
            with self.subTest(value=value):
                web["metadata"]["annotations"] = {"homestead.io/update-previous": value}
                self.assert_everything_kept_by("web", "update-previous")

    def test_a_stopped_workload_with_an_unreadable_template_keeps_every_image(self):
        for kind, path, index, broken in (
                ("Deployment", "/apis/apps/v1/deployments", 0, lambda o: o["spec"].pop("template")),
                ("Deployment", "/apis/apps/v1/deployments", 0, lambda o: o["spec"]["template"]["spec"].update(containers="x")),
                ("Deployment", "/apis/apps/v1/deployments", 0, lambda o: o["spec"]["template"]["spec"].update(containers=["x"])),
                ("CronJob", "/apis/batch/v1/cronjobs", 0, lambda o: o["spec"]["jobTemplate"].update(spec=None)),
                ("VM", VMS, 0, lambda o: o["spec"].update(template="x")),
                ("VM", VMS, 0, lambda o: o["spec"]["template"]["spec"].update(volumes={"rootdisk": {}}))):
            with self.subTest(kind=kind):
                obj = self.objects[path]["items"][index]
                saved = json.loads(json.dumps(obj))
                broken(obj)
                try:
                    self.assert_everything_kept_by(obj["metadata"]["name"], "template")
                finally:
                    self.objects[path]["items"][index] = saved

    # Gap 13: a VM pool or VM replica set scaled to zero still starts from its
    # template's container disks; with no VM or pod to name them they were unused.
    def test_a_vm_pool_or_replica_set_at_zero_keeps_its_container_disks(self):
        disk = {"name": "rootdisk", "containerDisk": {"image": "docker.io/library/unused:1"}}
        for path, item in (
                (POOLS, {"metadata": {"name": "workers", "namespace": "lab"}, "spec": {"replicas": 0,
                 "virtualMachineTemplate": {"spec": {"template": {"spec": {"volumes": [disk]}}}}}}),
                (VMIRS, {"metadata": {"name": "workers", "namespace": "lab"}, "spec": {"replicas": 0,
                 "template": {"spec": {"volumes": [disk]}}}})):
            with self.subTest(path=path):
                self.objects[path] = {"items": [item]}
                try:
                    rows = self.image(IMAGES.image_cache(), "unused")["retained_by"]
                    self.assertEqual([("stopped", "lab", "workers", "rootdisk")],
                                     [(r["reason"], r["namespace"], r["workload"], r["container"]) for r in rows])
                    with self.assertRaisesRegex(PermissionError, "stopped:lab/workers"):
                        IMAGES.cleanup_image(self.UNUSED, ["node1"])
                    self.assertEqual([], self.posted())
                    item["spec"]["replicas"] = 2       # running ones are named by their VMs and pods
                    self.asked.clear()
                    self.assertFalse(self.image(IMAGES.image_cache(), "unused")["protected"])
                    self.assertIn(path, self.asked)
                finally:
                    self.objects[path] = {"items": []}

    def test_no_permission_to_list_vm_pools_or_replica_sets_keeps_every_image(self):
        # An older Homestead role may not let it list these; the page still
        # opens, every image is kept, and the row says which list and why.
        read = imports.kget
        for path, kind in ((POOLS, "VirtualMachinePools"), (VMIRS, "VirtualMachineInstanceReplicaSets")):
            def get(asked, path=path):
                if asked == path:
                    raise urllib.error.HTTPError(asked, 403, "Forbidden", {}, None)
                return read(asked)
            with self.subTest(path=path), mock.patch.object(imports, "kget", get):
                report = IMAGES.image_cache()
                for name in ("unused", "debian"):
                    self.assertIn(("unreadable", "", kind, "no permission to list"),
                                  [(r["reason"], r["namespace"], r["workload"], r["container"])
                                   for r in self.image(report, name)["retained_by"]])
                with self.assertRaisesRegex(PermissionError, f"unreadable:/{kind}"):
                    IMAGES.cleanup_image(self.UNUSED, ["node1"])
                self.assertEqual([], self.posted())

    def test_other_errors_on_those_lists_still_stop_the_page(self):
        read = imports.kget

        def get(asked):
            if asked == POOLS:
                raise urllib.error.HTTPError(asked, 500, "Server Error", {}, None)
            return read(asked)
        with mock.patch.object(imports, "kget", get):
            with self.assertRaisesRegex(ValueError, "no image is cleaned up"):
                IMAGES.image_cache()

    def test_replicas_count_as_zero_unless_a_positive_number(self):
        for replicas, zero in ((0, True), (2, False), (-1, True), ("2", True), ("x", True),
                               (True, True), (2.0, True)):
            with self.subTest(replicas=replicas):
                self.assertIs(zero, IMAGES._scaled_to_zero({"replicas": replicas}))
        self.assertFalse(IMAGES._scaled_to_zero({}), "missing means one, the Kubernetes default")
        self.assertFalse(IMAGES._scaled_to_zero({"replicas": None}))

    def test_without_vm_pools_installed_clean_up_still_works(self):
        read = imports.kget

        asked = []

        def get(path):
            asked.append(path)
            if path == POOLS:
                raise urllib.error.HTTPError(path, 404, "Not Found", {}, None)
            if path == "/apis":
                return {"groups": [{"name": "apps"}, {"name": "kubevirt.io"}]}
            return read(path)
        with mock.patch.object(imports, "kget", get):
            IMAGES.cleanup_image(self.UNUSED, ["node1"])
        self.assertIn(POOLS, asked, "the pool list is asked for, and its 404 then checked with discovery")
        self.assertIn("/apis", asked)
        self.assertEqual(1, len(self.posted()))

    def test_a_404_for_vm_pools_while_their_group_is_served_removes_nothing(self):
        read = imports.kget

        def get(path):
            if path == POOLS:
                raise urllib.error.HTTPError(path, 404, "Not Found", {}, None)
            if path == "/apis":
                return {"groups": [{"name": "apps"}, {"name": "kubevirt.io"}, {"name": "pool.kubevirt.io"}]}
            return read(path)
        with mock.patch.object(imports, "kget", get):
            with self.assertRaisesRegex(ValueError, "no image is cleaned up"):
                IMAGES.cleanup_image(self.UNUSED, ["node1"])
        self.assertEqual([], self.posted())

    def test_without_kubevirt_installed_clean_up_still_works(self):
        with self.kubevirt_answers_404(["apps", "batch"]):
            IMAGES.cleanup_image(self.UNUSED, ["node1"])
        self.assertEqual(1, len(self.posted()))

    def test_a_404_for_vms_while_kubevirt_is_served_removes_nothing(self):
        with self.kubevirt_answers_404(["apps", "batch", "kubevirt.io"]):
            with self.assertRaisesRegex(ValueError, "no image is cleaned up"):
                IMAGES.cleanup_image(self.UNUSED, ["node1"])
        self.assertEqual([], self.posted())

    def test_an_empty_discovery_answer_removes_nothing(self):
        with self.kubevirt_answers_404([]):
            with self.assertRaisesRegex(ValueError, "no image is cleaned up"):
                IMAGES.cleanup_image(self.UNUSED, ["node1"])
        self.assertEqual([], self.posted())

    def test_a_discovery_that_fails_after_a_404_removes_nothing(self):
        with self.kubevirt_answers_404(OSError("timed out")):
            with self.assertRaisesRegex(ValueError, "no image is cleaned up"):
                IMAGES.cleanup_image(self.UNUSED, ["node1"])
        self.assertEqual([], self.posted())


class WaitingWorkloadTests(RemovalFixture):
    """Wrap-up rows 76 and 77: a workload whose pods are not there - a
    suspended Job, one between retries, a DaemonSet that matches no node, a
    Deployment that cannot make its pods - has no pod to name its image. Its
    image is kept unless its status shows every pod it wants is there."""
    JOBS, DAEMONSETS = "/apis/batch/v1/jobs", "/apis/apps/v1/daemonsets"

    def test_a_job_on_its_way_to_finishing_keeps_its_image_until_it_has(self):
        # SuccessCriteriaMet / FailureTarget come before Complete / Failed: no
        # more pods start, but until the end the image is kept, as a waiting one.
        for kind in ("SuccessCriteriaMet", "FailureTarget"):
            with self.subTest(kind=kind):
                self.objects[self.JOBS]["items"] = []
                self.assertEqual([("scheduled", "migrate", "migrate")], self.kept(self.JOBS, self.job(
                    status={"active": 0, "conditions": [{"type": kind, "status": "True"}]})))

    def test_a_record_that_is_not_an_object_removes_nothing(self):
        for path, item in ((self.JOBS, "x"), (self.JOBS, {"metadata": "x", "spec": {}}),
                           (self.JOBS, {**self.job(), "status": "x"}), (self.DAEMONSETS, {**self.daemonset(), "spec": []}),
                           (DVS, {"metadata": {"name": "d"}, "spec": {}, "status": 7})):
            with self.subTest(path=path, item=item):
                self.objects[path]["items"] = [item]
                try:
                    with self.assertRaisesRegex(ValueError, "no image is cleaned up"):
                        IMAGES.cleanup_image(self.UNUSED, ["node1"])
                finally:
                    self.objects[path]["items"] = []
        self.assertEqual([], self.posted())

    @staticmethod
    def job(spec=None, status=None):
        return {"metadata": {"name": "migrate", "namespace": "lab"},
                "spec": {**(spec or {}), "template": {"spec": {"containers": [
                    {"name": "migrate", "image": "docker.io/library/unused:1"}]}}},
                "status": status or {}}

    @staticmethod
    def daemonset(status=None, generation=None):
        meta = {"name": "agent", "namespace": "lab", **({"generation": generation} if generation else {})}
        return {"metadata": meta, "spec": {"template": {"spec": {"containers": [
                    {"name": "agent", "image": "docker.io/library/unused:1"}]}}},
                "status": status or {}}

    def test_a_suspended_job_keeps_its_image(self):
        self.assertEqual([("stopped", "migrate", "migrate")], self.kept(self.JOBS, self.job({"suspend": True})))
        with self.assertRaisesRegex(PermissionError, "stopped:lab/migrate"):
            IMAGES.cleanup_image(self.UNUSED, ["node1"])
        self.assertEqual([], self.posted())

    def test_a_job_between_retries_keeps_its_image(self):
        self.assertEqual([("scheduled", "migrate", "migrate")],
                         self.kept(self.JOBS, self.job(status={"failed": 1, "active": 0})))

    def test_a_job_whose_conditions_cannot_be_read_keeps_its_image(self):
        self.assertEqual([("scheduled", "migrate", "migrate")],
                         self.kept(self.JOBS, self.job(status={"conditions": "Complete"})))

    def test_a_finished_job_keeps_nothing(self):
        for kind in ("Complete", "Failed"):
            with self.subTest(kind=kind):
                self.objects[self.JOBS]["items"] = []
                self.assertEqual([], self.kept(self.JOBS, self.job(status={"conditions": [
                    {"type": "Suspended", "status": "False"}, {"type": kind, "status": "True"}]})))
        self.objects[self.JOBS]["items"] = [self.job(status={"conditions": [{"type": "Complete", "status": "False"}]})]
        self.assertEqual([("scheduled", "migrate", "migrate")], self.kept())

    def test_a_running_job_is_named_by_its_pods(self):
        self.assertEqual([], self.kept(self.JOBS, self.job(status={"active": 1})))
        # ...but a suspended one is waiting, whatever its count says.
        self.objects[self.JOBS]["items"] = [self.job({"suspend": True}, {"active": 1})]
        self.assertEqual([("stopped", "migrate", "migrate")], self.kept())

    def test_a_job_with_an_unreadable_template_keeps_every_image(self):
        item = self.job({"suspend": True})
        item["spec"]["template"] = "x"
        self.assertEqual([("unreadable", "migrate", "template")], self.kept(self.JOBS, item))

    def test_a_daemonset_on_no_node_keeps_its_image(self):
        for status in ({}, {"desiredNumberScheduled": 0}, {"desiredNumberScheduled": "2", "updatedNumberScheduled": 2}):
            with self.subTest(status=status):
                self.objects[self.DAEMONSETS]["items"] = []
                self.assertEqual([("stopped", "agent", "agent")], self.kept(self.DAEMONSETS, self.daemonset(status)))

    def test_a_daemonset_with_every_pod_it_wants_is_named_by_them(self):
        self.assertEqual([], self.kept(self.DAEMONSETS, self.daemonset(
            {"desiredNumberScheduled": 2, "updatedNumberScheduled": 2, "observedGeneration": 3}, generation=3)))

    def test_a_daemonset_part_way_or_with_an_old_status_keeps_its_image(self):
        for status, generation in (({"desiredNumberScheduled": 2, "updatedNumberScheduled": 1}, None),
                                   ({"desiredNumberScheduled": 2, "updatedNumberScheduled": 2, "observedGeneration": 3}, 4),
                                   ({"desiredNumberScheduled": 2, "updatedNumberScheduled": 2}, 4)):
            with self.subTest(status=status, generation=generation):
                self.objects[self.DAEMONSETS]["items"] = []
                self.assertEqual([("stopped", "agent", "agent")],
                                 self.kept(self.DAEMONSETS, self.daemonset(status, generation)))

    def test_a_deployment_or_statefulset_that_cannot_make_its_pods_keeps_its_image(self):
        web = self.objects["/apis/apps/v1/deployments"]["items"][1]
        for status in ({}, {"replicas": 0, "updatedReplicas": 0}, {"replicas": 1}, {"updatedReplicas": "1"}):
            with self.subTest(status=status):
                web["status"] = status
                self.assertEqual([("stopped", "web", "web")], self.kept())
        web["spec"]["replicas"], web["status"] = 2, {"replicas": 2, "updatedReplicas": 1}   # part way through an update
        self.assertEqual([("stopped", "web", "web")], self.kept())
        web["spec"]["replicas"] = 1
        web["status"] = {"replicas": 1, "updatedReplicas": 1, "observedGeneration": 1}
        web["metadata"]["generation"] = 2      # the status is from before the last change
        self.assertEqual([("stopped", "web", "web")], self.kept())
        web["metadata"]["generation"] = 1
        self.assertEqual([], self.kept())
        sts = json.loads(json.dumps(web))
        sts["status"] = {"replicas": 1}
        self.assertEqual([("stopped", "web", "web")], self.kept("/apis/apps/v1/statefulsets", sts))

    def test_a_stuck_deployment_with_an_unreadable_template_keeps_every_image(self):
        web = self.objects["/apis/apps/v1/deployments"]["items"][1]
        web["status"], web["spec"]["template"] = {}, None
        self.assertEqual([("unreadable", "web", "template")], self.kept())


class OtherRecordsTests(RemovalFixture):
    """Wrap-up rows 78 and 79: Harvester VM template versions, KubeVirt VM
    snapshot contents and CDI imports that pull through the node's runtime
    name images too; VM pools are read at the version discovery prefers."""
    DISK = {"name": "rootdisk", "containerDisk": {"image": "docker.io/library/unused:1"}}
    NODE_PULL = {"registry": {"url": "docker://docker.io/library/unused:1", "pullMethod": "node"}}

    def answers(self, path, error=None, groups=None):
        """kget with one path raising HTTP error `error`, and /apis listing `groups`."""
        read = imports.kget

        def get(asked):
            if asked == path:
                raise urllib.error.HTTPError(asked, error, "error", {}, None)
            if asked == "/apis" and groups is not None:
                return {"groups": [{"name": name} for name in groups]}
            return read(asked)
        return mock.patch.object(imports, "kget", get)

    def test_vm_pools_are_read_at_the_version_discovery_prefers(self):
        beta = "/apis/pool.kubevirt.io/v1beta1/virtualmachinepools"
        self.objects["/apis/pool.kubevirt.io"] = api_group("pool.kubevirt.io", "v1beta1")
        del self.objects[POOLS]          # v1alpha1 no longer served
        self.objects["/apis/pool.kubevirt.io/v1beta1"] = {"resources": [{"name": "virtualmachinepools"}]}
        self.objects[beta] = {"items": [{"metadata": {"name": "workers", "namespace": "lab"}, "spec": {
            "replicas": 0, "virtualMachineTemplate": {"spec": {"template": {"spec": {"volumes": [self.DISK]}}}}}}]}
        self.assertEqual([("stopped", "workers", "rootdisk")], self.kept())
        self.assertNotIn(POOLS, self.asked)

    def test_a_resource_the_preferred_version_does_not_serve_is_read_at_one_that_does(self):
        # Review: a group can move some kinds to a new version before others.
        self.objects["/apis/snapshot.kubevirt.io"] = api_group("snapshot.kubevirt.io", "v1")
        self.objects["/apis/snapshot.kubevirt.io"]["versions"].append(
            {"groupVersion": "snapshot.kubevirt.io/v1beta1", "version": "v1beta1"})
        self.objects["/apis/snapshot.kubevirt.io/v1"] = {"resources": [{"name": "virtualmachinesnapshots"}]}
        item = {"metadata": {"name": "vmsnapshot-content-1", "namespace": "lab"}, "spec": {"source": {
            "virtualMachine": {"spec": {"template": {"spec": {"volumes": [self.DISK]}}}}}}}
        self.assertEqual([("stopped", "vmsnapshot-content-1", "rootdisk")], self.kept(CONTENTS, item))
        # A group that serves the kind at no version has none of it.
        self.objects["/apis/snapshot.kubevirt.io/v1beta1"] = {"resources": [{"name": "virtualmachinerestores"}]}
        self.assertEqual([], self.kept())
        # A version's resource list that cannot be read keeps everything.
        self.objects["/apis/snapshot.kubevirt.io/v1"] = {"resources": "x"}
        with self.assertRaisesRegex(ValueError, "no image is cleaned up"):
            IMAGES.cleanup_image(self.UNUSED, ["node1"])
        self.assertEqual([], self.posted())

    def test_no_permission_to_read_a_groups_discovery_keeps_every_image(self):
        for where in ("/apis/cdi.kubevirt.io", "/apis/cdi.kubevirt.io/v1beta1"):
            with self.subTest(where=where), self.answers(where, 403):
                self.assertIn(("unreadable", "DataVolumes", "no permission to list"), self.kept())
        self.assertEqual([], self.posted())

    def test_a_scheduled_import_keeps_the_digests_it_imported(self):
        # CDI imports a DataImportCron's tag by digest; a node pull by digest
        # has no tag to match by name.
        cron = {"metadata": {"name": "fedora-daily", "namespace": "lab"},
                "spec": {"template": {"spec": {"source": {"registry": {
                    "url": "docker://quay.io/containerdisks/other:latest", "pullMethod": "node"}}}}},
                "status": {"currentImports": [{"dataVolumeName": "fedora-abc", "digest": self.UNUSED}]}}
        self.assertEqual([("scheduled", "fedora-daily", "registry")], self.kept(CRONS, cron))
        cron["status"]["currentImports"] = "x"
        self.assertEqual([("unreadable", "fedora-daily", "source")], self.kept())

    def test_a_stopped_vms_disk_templates_keep_what_they_import_through_the_node(self):
        vm = self.objects[VMS]["items"][0]
        vm["spec"]["dataVolumeTemplates"] = [{"metadata": {"name": "root"}, "spec": {"source": self.NODE_PULL}}]
        self.assertEqual([("stopped", "lab-vm", "root")], self.kept())
        running = self.objects[VMS]["items"][1]          # a running VM's disk is imported already
        vm["spec"].pop("dataVolumeTemplates")
        running["spec"]["dataVolumeTemplates"] = [{"metadata": {"name": "root"}, "spec": {"source": self.NODE_PULL}}]
        self.assertEqual([], self.kept())

    def test_a_group_that_discovery_says_is_absent_is_not_read(self):
        for group in OPTIONAL:
            with self.subTest(group=group):
                self.asked.clear()
                with self.answers(f"/apis/{group}", 404, groups=["apps", "batch", "kubevirt.io"]):
                    IMAGES.cleanup_image(self.UNUSED, ["node1"])
                self.assertFalse([path for path in self.asked if path.startswith(f"/apis/{group}/")])
        self.assertEqual(len(OPTIONAL), len(self.posted()))

    def test_a_404_for_a_group_discovery_still_lists_removes_nothing(self):
        for group in OPTIONAL:
            with self.subTest(group=group), self.answers(f"/apis/{group}", 404, groups=["apps", group]):
                with self.assertRaisesRegex(ValueError, "no image is cleaned up"):
                    IMAGES.cleanup_image(self.UNUSED, ["node1"])
        self.assertEqual([], self.posted())

    def test_an_odd_discovery_answer_removes_nothing(self):
        for answer in ({}, {"preferredVersion": None}, {"preferredVersion": {"groupVersion": 7}},
                       {"preferredVersion": {"groupVersion": "other.io/v1"}},
                       {"preferredVersion": {"groupVersion": "cdi.kubevirt.io/v1/x"}}, []):
            with self.subTest(answer=answer):
                self.objects["/apis/cdi.kubevirt.io"] = answer
                with self.assertRaisesRegex(ValueError, "no image is cleaned up"):
                    IMAGES.cleanup_image(self.UNUSED, ["node1"])
        self.assertEqual([], self.posted())

    def test_a_harvester_vm_template_keeps_the_images_it_boots_from(self):
        item = {"metadata": {"name": "fedora-template-v1", "namespace": "lab"}, "spec": {"vm": {"spec": {
            "template": {"spec": {"volumes": [self.DISK]}}}}}}
        self.assertEqual([("stopped", "fedora-template-v1", "rootdisk")], self.kept(TEMPLATES, item))
        with self.assertRaisesRegex(PermissionError, "stopped:lab/fedora-template-v1"):
            IMAGES.cleanup_image(self.UNUSED, ["node1"])
        # ...and the image its disk templates import through the node's runtime.
        item["spec"]["vm"]["spec"]["template"]["spec"]["volumes"] = []
        item["spec"]["vm"]["spec"]["dataVolumeTemplates"] = [{"metadata": {"name": "root"}, "spec": {"source": self.NODE_PULL}}]
        self.assertEqual([("stopped", "fedora-template-v1", "root")], self.kept())
        self.assertEqual([], self.posted())

    def test_a_vm_snapshot_keeps_the_container_disks_its_vm_boots_from(self):
        item = {"metadata": {"name": "vmsnapshot-content-1", "namespace": "lab"}, "spec": {"source": {
            "virtualMachine": {"metadata": {"name": "lab-vm"}, "spec": {"template": {"spec": {"volumes": [self.DISK]}}}}}}}
        self.assertEqual([("stopped", "vmsnapshot-content-1", "rootdisk")], self.kept(CONTENTS, item))

    def test_a_cdi_import_that_pulls_through_the_node_keeps_its_image(self):
        dv = {"metadata": {"name": "fedora-root", "namespace": "lab"}, "spec": {"source": self.NODE_PULL}}
        self.assertEqual([("stopped", "fedora-root", "registry")], self.kept(DVS, dv))
        self.objects[DVS]["items"] = []
        cron = {"metadata": {"name": "fedora-daily", "namespace": "lab"},
                "spec": {"template": {"spec": {"source": self.NODE_PULL}}}}
        self.assertEqual([("scheduled", "fedora-daily", "registry")], self.kept(CRONS, cron))

    def test_a_cdi_import_its_own_pod_pulls_keeps_nothing(self):
        # The importer pod downloads the image itself: containerd never holds it.
        for source in ({"registry": {"url": "docker://docker.io/library/unused:1"}},
                       {"registry": {"url": "docker://docker.io/library/unused:1", "pullMethod": "pod"}},
                       {"http": {"url": "https://example.com/disk.img"}}, None):
            with self.subTest(source=source):
                self.objects[DVS]["items"] = [{"metadata": {"name": "disk", "namespace": "lab"},
                                               "spec": {"source": source} if source else {"sourceRef": {}}}]
                self.assertEqual([], self.kept())

    def test_one_of_these_records_that_cannot_be_read_keeps_every_image(self):
        for path, item, what in (
                (TEMPLATES, {"spec": {"vm": "x"}}, "template"),
                (TEMPLATES, {"spec": {"vm": {"spec": {"template": {"spec": {}}, "dataVolumeTemplates": "x"}}}}, "template"),
                (TEMPLATES, {"spec": {"vm": {"spec": {"template": {"spec": {}}, "dataVolumeTemplates": [
                    {"spec": {"source": {"registry": "x"}}}]}}}}, "source"),
                (CONTENTS, {"spec": {"source": {}}}, "template"),
                (DVS, {"spec": {"source": "x"}}, "source"),
                (DVS, {"spec": {"source": {"registry": "x"}}}, "source"),
                (DVS, {"spec": {"source": {"registry": {"pullMethod": "node", "url": 7}}}}, "source"),
                (CRONS, {"spec": {"template": "x"}}, "source")):
            with self.subTest(path=path, item=item):
                self.objects[path]["items"] = [{"metadata": {"name": "broken", "namespace": "lab"}, **item}]
                try:
                    self.assertEqual([("unreadable", "broken", what)], self.kept())
                finally:
                    self.objects[path]["items"] = []

    def test_no_permission_to_list_one_of_these_keeps_every_image(self):
        for path, kind in ((TEMPLATES, "VirtualMachineTemplateVersions"), (CONTENTS, "VirtualMachineSnapshotContents"),
                           (DVS, "DataVolumes"), (CRONS, "DataImportCrons")):
            with self.subTest(path=path), self.answers(path, 403):
                self.assertEqual([("unreadable", kind, "no permission to list")], self.kept())
                with self.assertRaisesRegex(PermissionError, f"unreadable:/{kind}"):
                    IMAGES.cleanup_image(self.UNUSED, ["node1"])
        self.assertEqual([], self.posted())



class KeptReasonTests(RemovalFixture):
    """#937: each image kept for a record Homestead reads says why in plain
    words (a label, a sentence and the kind of record), not only the coarse
    "stopped" or "scheduled" cleanup sorts by."""
    DISK = OtherRecordsTests.DISK
    NODE_PULL = OtherRecordsTests.NODE_PULL
    job = staticmethod(WaitingWorkloadTests.job)
    daemonset = staticmethod(WaitingWorkloadTests.daemonset)

    def why(self, path=None, item=None):
        """(kind, label) of each row keeping the unused image."""
        if path:
            self.objects[path]["items"].append(item)
        rows = self.image(IMAGES.image_cache(), "unused")["retained_by"]
        return [(r["kind"], r["label"]) for r in rows]

    def test_a_suspended_job_and_a_waiting_job_read_differently(self):
        self.assertEqual([("Job", "Job paused")], self.why("/apis/batch/v1/jobs", self.job({"suspend": True})))
        self.objects["/apis/batch/v1/jobs"]["items"] = []
        self.assertEqual([("Job", "Job waiting")], self.why("/apis/batch/v1/jobs", self.job(status={"failed": 1})))

    def test_a_workload_without_pods_says_so(self):
        self.assertEqual([("DaemonSet", "no pods yet")],
                         self.why("/apis/apps/v1/daemonsets", self.daemonset({"desiredNumberScheduled": 0})))
        web = self.objects["/apis/apps/v1/deployments"]["items"][1]
        self.objects["/apis/apps/v1/daemonsets"]["items"] = []
        web["status"] = {}
        self.assertEqual([("Deployment", "no pods yet")], self.why())
        web["spec"]["replicas"] = 0
        self.assertEqual([("Deployment", "scaled to zero")], self.why())

    def test_a_cronjob_is_a_scheduled_job(self):
        cron = {"metadata": {"name": "nightly", "namespace": "lab"}, "spec": {"jobTemplate": {"spec": {"template": {
            "spec": {"containers": [{"name": "run", "image": "docker.io/library/unused:1"}]}}}}}}
        self.assertEqual([("CronJob", "scheduled job")], self.why("/apis/batch/v1/cronjobs", cron))

    def test_templates_snapshots_and_imports_say_what_they_are(self):
        template = {"metadata": {"name": "t-v1", "namespace": "lab"}, "spec": {"vm": {"spec": {
            "template": {"spec": {"volumes": [self.DISK]}}}}}}
        self.assertEqual([("VirtualMachineTemplateVersion", "VM template")], self.why(TEMPLATES, template))
        self.objects[TEMPLATES]["items"] = []
        snapshot = {"metadata": {"name": "snap-1", "namespace": "lab"}, "spec": {"source": {"virtualMachine": {
            "spec": {"template": {"spec": {"volumes": [self.DISK]}}}}}}}
        self.assertEqual([("VirtualMachineSnapshotContent", "VM snapshot")], self.why(CONTENTS, snapshot))
        self.objects[CONTENTS]["items"] = []
        dv = {"metadata": {"name": "root", "namespace": "lab"}, "spec": {"source": self.NODE_PULL}}
        self.assertEqual([("DataVolume", "disk import")], self.why(DVS, dv))
        self.objects[DVS]["items"] = []
        cron = {"metadata": {"name": "daily", "namespace": "lab"}, "spec": {"template": {"spec": {"source": self.NODE_PULL}}}}
        self.assertEqual([("DataImportCron", "scheduled import")], self.why(CRONS, cron))

    def test_a_template_versions_disk_import_is_the_templates(self):
        template = {"metadata": {"name": "t-v1", "namespace": "lab"}, "spec": {"vm": {"spec": {
            "template": {"spec": {}}, "dataVolumeTemplates": [{"metadata": {"name": "root"}, "spec": {"source": self.NODE_PULL}}]}}}}
        self.assertEqual([("VirtualMachineTemplateVersion", "VM template")], self.why(TEMPLATES, template))

    def test_every_row_has_a_label_and_a_sentence(self):
        rows = IMAGES.image_retention_inventory()
        self.assertTrue(rows)
        for row in rows:
            with self.subTest(row=row):
                self.assertTrue(row["label"] and row["detail"] and "kind" in row)

    def test_an_image_stream_import_is_named_and_still_keeps_every_image(self):
        # OpenShift only: its image stream is not read, so which image it means is unknown.
        stream = {"registry": {"imageStream": "fedora", "pullMethod": "node"}}
        dv = {"metadata": {"name": "root", "namespace": "lab"}, "spec": {"source": stream}}
        self.assertEqual([("DataVolume", "image stream not read")], self.why(DVS, dv))
        with self.assertRaisesRegex(PermissionError, "unreadable:lab/root"):
            IMAGES.cleanup_image(self.UNUSED, ["node1"])
        self.assertEqual([], self.posted())

    def test_a_dataimportcron_image_stream_is_named_too(self):
        cron = {"metadata": {"name": "daily", "namespace": "lab"},
                "spec": {"template": {"spec": {"source": {"registry": {"imageStream": "fedora", "pullMethod": "node"}}}}}}
        self.assertEqual([("DataImportCron", "image stream not read")], self.why(CRONS, cron))

    def test_a_pod_pulled_image_stream_import_keeps_nothing(self):
        # No pullMethod node: containerd never holds the image, so nothing is kept.
        dv = {"metadata": {"name": "root", "namespace": "lab"}, "spec": {"source": {"registry": {"imageStream": "fedora"}}}}
        self.assertEqual([], self.why(DVS, dv))

    def test_two_keepers_with_one_name_both_show(self):
        # A Deployment and a StatefulSet of one name keep the image for the same reason; neither hides the other.
        web = self.objects["/apis/apps/v1/deployments"]["items"][1]
        web["status"] = {}
        sts = json.loads(json.dumps(web))
        self.assertEqual([("Deployment", "no pods yet"), ("StatefulSet", "no pods yet")],
                         self.why("/apis/apps/v1/statefulsets", sts))


if __name__ == "__main__":
    unittest.main()
