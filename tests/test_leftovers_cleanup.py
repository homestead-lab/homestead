"""What Homestead leaves behind is cleaned up, and only what it made: a
Longhorn "Run now" Job ends with a TTL, a finished import probe pod is swept,
a disk read that is cut off unmounts, "Back up now" removes its own local
snapshot."""
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
import homestead_disk_setup as DISK_SETUP
import homestead_helper_sweep as SWEEP
import homestead_kube as KUBE
import homestead_longhorn as LH
import homestead_longhorn_backups as BACKUPS
import homestead_longhorn_jobs as JOBS
import homestead_longhorn_target as TARGET
import homestead_operations as OPS
from fakes.kube import Kube

NOW = 1_800_000_000
HOUR, DAY = 3600, 86400
LHNS = "longhorn-system"
SNAPS = f"/apis/longhorn.io/v1beta2/namespaces/{LHNS}/snapshots"
BACKUP_LIST = f"/apis/longhorn.io/v1beta2/namespaces/{LHNS}/backups"


def iso(t):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t))


class BoundToFake(unittest.TestCase):
    """Longhorn's module reads and writes through the shared fake; what
    server.py bound is put back after."""

    def setUp(self):
        # Put back by name whatever this version of Longhorn's bind() sets,
        # including the CSI restore part it binds too (STORAGE_CLASS is gone
        # once the one default-storage-class rule lands).
        for module, names in ((LH, ("kget", "ksend", "_cache", "STORAGE_CLASS", "DEFAULT_NS")),
                              (LH.CSI_RESTORE, ("kget", "ksend"))):
            for name in names:
                if hasattr(module, name):
                    self.enterContext(mock.patch.object(module, name, getattr(module, name)))
        self.enterContext(mock.patch.object(OPS, "DATA_DIR", self.enterContext(tempfile.TemporaryDirectory())))

    def fake(self, *objects):
        self.kube = self.enterContext(Kube(*objects))
        LH.bind(KUBE.kget, KUBE.ksend, {})
        return self.kube


# ----------------------------------------------------------- Longhorn Run now
def cron(name="backup-daily", ttl=None):
    spec = {"template": {"spec": {"containers": [{"name": "run"}], "restartPolicy": "OnFailure"}}}
    if ttl is not None:
        spec["ttlSecondsAfterFinished"] = ttl
    return {"apiVersion": "batch/v1", "kind": "CronJob", "metadata": {"name": name, "namespace": LHNS},
            "spec": {"schedule": "0 3 * * *", "jobTemplate": {"spec": spec}}}


class RunNowTtlTests(BoundToFake):
    def posted_job(self, result):
        return self.kube.object(f"/apis/batch/v1/namespaces/{LHNS}/jobs/{result['job']}")

    def test_a_run_now_job_goes_a_day_after_it_ends(self):
        # The CronJob owns it without being its controller, so its history
        # limits never trim it: only a TTL removes it.
        self.fake(cron())
        job = self.posted_job(JOBS.run_job("backup-daily"))
        self.assertEqual(86400, job["spec"].get("ttlSecondsAfterFinished"))
        self.assertFalse(job["metadata"]["ownerReferences"][0]["controller"])

    def test_a_ttl_the_schedule_sets_itself_is_kept(self):
        self.fake(cron(ttl=600))
        job = self.posted_job(JOBS.run_job("backup-daily"))
        self.assertEqual(600, job["spec"]["ttlSecondsAfterFinished"])


# ------------------------------------------------------------ probe pod sweep
def pod(name, task, phase="Failed", finished=NOW - DAY, labels=None, ns="lab"):
    status = {"phase": phase, "startTime": iso(finished - 60)}
    if phase in ("Succeeded", "Failed"):
        status["containerStatuses"] = [{"name": "probe", "state": {"terminated": {
            "exitCode": 0 if phase == "Succeeded" else 1, "finishedAt": iso(finished)}}}]
    meta = {"name": name, "namespace": ns, "creationTimestamp": iso(finished - 70),
            "labels": {**({"homestead.io/task": task} if task else {}), **(labels or {})}}
    return {"apiVersion": "v1", "kind": "Pod", "metadata": meta, "spec": {"containers": [{"name": "probe"}]},
            "status": status}


class ProbeSweepTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(mock.patch.object(SWEEP.OPS, "DATA_DIR", self.enterContext(tempfile.TemporaryDirectory())))

    def sweep(self, *objects):
        self.kube = Kube(*objects)
        with self.kube, mock.patch.object(SWEEP.OPS, "stored", return_value=[]):
            return SWEEP.sweep(now=NOW)

    def left(self, name):
        return self.kube.object(f"/api/v1/namespaces/lab/pods/{name}") is not None

    def test_a_probe_left_by_a_restart_goes_an_hour_after_it_ended(self):
        # run_probe deletes its pod in finally; a restart mid-probe skips
        # that, and the pod ends at its deadline and stays.
        self.sweep(pod("homestead-probe-inspect-source-a-0123456789", "probe", finished=NOW - 2 * HOUR),
                   pod("homestead-probe-key-source-a-abcdef0123", "probe", phase="Succeeded", finished=NOW - 2 * HOUR),
                   pod("homestead-probe-vms-source-a-0a1b2c3d4e", "probe", finished=NOW - 10 * 60),
                   pod("homestead-probe-browse-source-a-9876543210", "probe", phase="Running"))
        self.assertFalse(self.left("homestead-probe-inspect-source-a-0123456789"))
        self.assertFalse(self.left("homestead-probe-key-source-a-abcdef0123"))
        self.assertTrue(self.left("homestead-probe-vms-source-a-0a1b2c3d4e"), "ended minutes ago")
        self.assertTrue(self.left("homestead-probe-browse-source-a-9876543210"), "never a running one")

    def test_the_node_probe_and_pods_not_named_as_a_probe_are_left(self):
        # The node probe DaemonSet's pods sit in the same namespace with no
        # task label; a user's pod may carry the label by copying it.
        self.sweep(pod("homestead-nodeprobe-x7k2p", None, labels={"app": "homestead-nodeprobe"}, finished=NOW - 9 * DAY),
                   pod("my-probe", "probe", finished=NOW - 9 * DAY),
                   pod("homestead-probe-inspect-source-a-0123456789", "probe", finished=NOW - 9 * DAY, ns="apps"))
        self.assertTrue(self.left("homestead-nodeprobe-x7k2p"))
        self.assertTrue(self.left("my-probe"))
        self.assertIsNotNone(self.kube.object("/api/v1/namespaces/apps/pods/homestead-probe-inspect-source-a-0123456789"))

    def test_a_probe_replaced_since_it_was_read_is_left(self):
        name = "homestead-probe-inspect-source-a-0123456789"
        path = f"/api/v1/namespaces/lab/pods/{name}"
        kube = Kube(pod(name, "probe", finished=NOW - 2 * HOUR))

        def replaced(_method, _path, _body):
            # Removed and made again between the sweep's read and its delete.
            kube.discard(path)
            kube.add(pod(name, "probe", finished=NOW - 2 * HOUR))
            return None
        kube.react("DELETE", path, replaced)
        self.kube = kube
        with kube, mock.patch.object(SWEEP.OPS, "stored", return_value=[]):
            SWEEP.sweep(now=NOW)
        self.assertTrue(self.left(name), "the new one's uid is not the one read")


# ------------------------------------------------------------ disk read mount
FAKES = {
    "blockdev": "echo 0",
    "lsblk": "exit 0",
    "blkid": 'case "$*" in *TYPE*) echo ext4;; *) echo 0000-1111;; esac',
    # The read is cut off while the disk is mounted: the shell running the
    # script is told to stop, as when the exec is ended under it.
    "mount": 'echo "mount ${4}" >> "$LOG"; [ -n "$CUT" ] && kill -"$CUT" $PPID; exit 0',
    "umount": 'echo "umount ${1}" >> "$LOG"; exit 0',
}


class DiskReadUnmountTests(unittest.TestCase):
    def read(self, shell, cut):
        """cut: the signal the script's shell gets while the disk is mounted
        (TERM when the helper is stopped, PIPE when the exec's stream that
        it writes to is gone), or None."""
        work = Path(self.enterContext(tempfile.TemporaryDirectory()))
        (work / "bin").mkdir()
        for name, body in FAKES.items():
            tool = work / "bin" / name
            tool.write_text(f"#!/bin/sh\n{body}\n")
            tool.chmod(0o755)
        log = work / "log"
        log.touch()
        # Only the block-device test is answered here: the test has no disk.
        script = DISK_SETUP.inspect_script("/dev/sdz").replace('[ -b "$D" ] ||', ': ||', 1)
        env = {"PATH": f"{work / 'bin'}:/usr/bin:/bin", "LOG": str(log), "CUT": cut or "",
               "TMPDIR": str(work)}
        self.code = subprocess.run([shell, "-c", script], env=env, capture_output=True, timeout=30).returncode
        lines = log.read_text().split()
        mounted = [lines[i + 1] for i, word in enumerate(lines) if word == "mount"]
        unmounted = [lines[i + 1] for i, word in enumerate(lines) if word == "umount"]
        return mounted, unmounted, [p for p in work.iterdir() if p.name.startswith("tmp")]

    def shells(self):
        return [s for s in ("sh", "bash") if shutil.which(s)]

    def test_a_disk_read_cut_off_while_mounted_unmounts_and_removes_its_folder(self):
        for shell in self.shells():
            with self.subTest(shell=shell):
                mounted, unmounted, folders = self.read(shell, cut="TERM")
                self.assertEqual(1, len(mounted))
                self.assertEqual(mounted, unmounted, "the host keeps the mount until a reboot")
                self.assertEqual([], folders)

    def test_a_read_whose_output_is_gone_unmounts_too(self):
        # SIGPIPE: the exec's stream closed and the script writes on (dash
        # is the sh of the test image, as of many hosts).
        for shell in self.shells():
            with self.subTest(shell=shell):
                mounted, unmounted, folders = self.read(shell, cut="PIPE")
                self.assertEqual(1, len(mounted))
                self.assertEqual(mounted, unmounted)
                self.assertEqual([], folders)

    def test_the_read_exits_with_the_signals_usual_status(self):
        # 128 + the signal, as the shell itself would have exited.
        for cut, code in (("INT", 130), ("TERM", 143), ("PIPE", 141)):
            with self.subTest(cut=cut):
                mounted, unmounted, _ = self.read("sh", cut=cut)
                self.assertEqual((mounted, code), (unmounted, self.code))

    def test_a_read_that_runs_through_unmounts_once(self):
        for shell in self.shells():
            with self.subTest(shell=shell):
                mounted, unmounted, folders = self.read(shell, cut=None)
                self.assertEqual((1, mounted), (len(mounted), unmounted))
                self.assertEqual([], folders)


# --------------------------------------------------------------- Back up now
def target_configured():
    return mock.patch.object(TARGET, "backup_target", return_value={"configured": True})


class BackUpNowTests(BoundToFake):
    def setUp(self):
        super().setUp()
        self.enterContext(target_configured())
        saved = OPS.kget
        self.addCleanup(setattr, OPS, "kget", saved)
        OPS.kget = KUBE.kget

    def snapshots(self):
        return [row["metadata"]["name"] for row in self.kube("GET", SNAPS)["items"]]

    def test_a_refused_backup_removes_the_snapshot_it_took(self):
        kube = self.fake()
        kube.fail(BACKUP_LIST, 422, method="POST")
        with self.assertRaises(urllib.error.HTTPError):
            BACKUPS.create_backup("vol-a")
        self.assertEqual([], self.snapshots())

    def test_a_backup_whose_answer_was_lost_keeps_its_snapshot(self):
        # A 5xx or a time-out does not say the Backup was not made: it may be
        # reading that snapshot now.
        # A 408 may still be made, a 409 is one of that name already there.
        for failure, applied in ((500, False), (503, True), (TimeoutError("timed out"), True), (408, False), (409, False)):
            with self.subTest(failure=failure):
                kube = self.fake()
                kube.fail(BACKUP_LIST, failure, method="POST", applied=applied)
                with self.assertRaises(Exception):
                    BACKUPS.create_backup("vol-a")
                self.assertEqual(1, len(self.snapshots()))
                self.assertFalse(any(m == "DELETE" for m, *_ in kube.writes))

    def start(self):
        result = LH.backup_now({"volume": "vol-a"})
        item = next(i for i in OPS.stored() if i.get("kind") == "backup")
        return result, item

    def finish(self, result, state="Completed"):
        backup = self.kube.object(f"{BACKUP_LIST}/{result['backup']}")
        backup["status"] = {"state": state, "progress": 100 if state == "Completed" else 40}
        self.kube.discard(f"{BACKUP_LIST}/{result['backup']}")
        self.kube.add(backup)

    def resolve(self, ident):
        """One round of the jobs' poll for this job, saved as the poll does."""
        with OPS.transaction() as t:
            items = t.items
            item = next(i for i in items if i["id"] == ident)
            state = OPS.RESOLVERS["backup"](item)
            OPS._finish(item, *state)
            t.save(items)
        return state[0]

    def sweep(self):
        SWEEP.sweep(now=NOW)

    def job(self, ident):
        return next(i for i in OPS.stored() if i["id"] == ident)

    def other(self, kind, ref, status="running"):
        """Another job on the volume: a revert, a browse session, a restore."""
        job = OPS.start(kind, f"{kind} vol-a", {"kind": "Volume", "name": "vol-a", "namespace": LHNS}, "/storage", ref)
        if status != "running":
            self.end(job["id"], status)
        return job["id"]

    def end(self, ident, status="succeeded"):
        with OPS.transaction() as t:
            items = t.items
            OPS._finish(next(i for i in items if i["id"] == ident), status, 100, "done")
            t.save(items)

    def test_a_completed_backup_removes_its_local_snapshot(self):
        self.fake()
        result, item = self.start()
        self.finish(result)
        self.assertEqual("succeeded", self.resolve(item["id"]))
        self.sweep()
        self.assertEqual([], self.snapshots())
        self.assertFalse(self.job(item["id"])["ref"].get("snapshot_pending"), "removed once, not every minute")

    def revert_ref(self, snapshot):
        """A snapshot revert's ref, as homestead_revert.start writes it."""
        return {"volume": "vol-a", "snapshot": snapshot, "namespace": "apps", "claim": "data",
                "consumers": [], "phase": "stopping", "since": NOW}

    def browse(self, snapshot, volume="vol-a"):
        """A snapshot browse session's record, as homestead_snapshot_files.start
        writes it: not a job, a labelled VolumeSnapshotContent."""
        name = "homestead-snapshot-files-0123456789abcdef01234567"
        self.kube.add({"apiVersion": "snapshot.storage.k8s.io/v1", "kind": "VolumeSnapshotContent",
                       "metadata": {"name": name, "labels": {"homestead.io/snapshot-files": "true",
                                                             "app.kubernetes.io/managed-by": "homestead"},
                                    "annotations": {"homestead.io/expires": str(NOW + 1800)}},
                       "spec": {"driver": "driver.longhorn.io", "deletionPolicy": "Retain", "sourceVolumeMode": "Filesystem",
                                "source": {"snapshotHandle": f"snap://{volume}/{snapshot}"},
                                "volumeSnapshotRef": {"name": name, "namespace": "apps"}}})
        return f"/apis/snapshot.storage.k8s.io/v1/volumesnapshotcontents/{name}"

    def test_the_snapshot_waits_while_a_revert_runs(self):
        self.fake()
        result, item = self.start()
        self.finish(result)
        self.assertEqual("succeeded", self.resolve(item["id"]), "the backup itself is done")
        using = self.other("snapshot-revert", self.revert_ref(result["snapshot"]))
        self.sweep()
        self.assertEqual([result["snapshot"]], self.snapshots(), "the revert is still using it")
        self.assertTrue(self.job(item["id"])["ref"]["snapshot_pending"])
        self.end(using)
        self.sweep()
        self.assertEqual([], self.snapshots(), "the next round, once nothing uses it")

    def test_the_snapshot_waits_while_a_failed_revert_awaits_recovery(self):
        self.fake()
        result, item = self.start()
        self.finish(result)
        using = self.other("snapshot-revert", self.revert_ref(result["snapshot"]), status="failed")
        self.resolve(item["id"])
        self.sweep()
        self.assertEqual([result["snapshot"]], self.snapshots(), "an unresolved revert still holds the volume")
        with OPS.transaction() as t:
            items = t.items
            next(i for i in items if i["id"] == using)["cleaned"] = True
            t.save(items)
        self.sweep()
        self.assertEqual([], self.snapshots())

    def test_the_snapshot_waits_while_it_is_open_in_the_file_browser(self):
        self.fake()
        result, item = self.start()
        self.finish(result)
        record = self.browse(result["snapshot"])
        self.resolve(item["id"])
        self.sweep()
        self.assertEqual([result["snapshot"]], self.snapshots(), "a browse session reads a clone of it")
        self.kube.discard(record)
        self.sweep()
        self.assertEqual([], self.snapshots())

    def test_another_snapshots_browse_session_does_not_hold_it(self):
        self.fake()
        result, item = self.start()
        self.finish(result)
        self.browse("homestead-1-aaaaaa")
        self.resolve(item["id"])
        self.sweep()
        self.assertEqual([], self.snapshots())

    def test_when_the_browse_sessions_cannot_be_read_the_snapshot_is_kept(self):
        kube = self.fake()
        result, item = self.start()
        self.finish(result)
        self.resolve(item["id"])
        kube.fail("/apis/snapshot.storage.k8s.io/v1/volumesnapshotcontents", 500, times=10)
        self.sweep()
        self.assertEqual([result["snapshot"]], self.snapshots())
        self.assertTrue(self.job(item["id"])["ref"]["snapshot_pending"], "asked again next round")

    def test_a_browse_opened_during_the_round_is_seen_before_the_next_delete(self):
        # The round reads the jobs and browse sessions again just before each
        # delete, not only once at its start.
        kube = self.fake()
        first, first_item = self.start()
        self.finish(first)
        second = LH.backup_now({"volume": "vol-b"})
        second_item = next(i for i in OPS.stored() if i.get("kind") == "backup" and i["id"] != first_item["id"])
        self.finish(second)
        self.resolve(first_item["id"])
        self.resolve(second_item["id"])

        def open_browse(method, path, body):
            self.browse(second["snapshot"], volume="vol-b")
            return None
        kube.react("DELETE", f"{SNAPS}/{first['snapshot']}", open_browse)
        self.sweep()
        self.assertEqual([second["snapshot"]], self.snapshots(), "opened while the first was being removed")

    def test_a_finished_job_on_the_volume_does_not_hold_it(self):
        self.fake()
        result, item = self.start()
        self.finish(result)
        self.other("snapshot-revert", self.revert_ref(result["snapshot"]), status="succeeded")
        self.resolve(item["id"])
        self.sweep()
        self.assertEqual([], self.snapshots())

    def test_a_job_store_row_that_is_not_an_object_is_skipped(self):
        self.fake()
        result, item = self.start()
        self.finish(result)
        self.resolve(item["id"])
        rows = ["not a job", None] + OPS.stored()
        # The snapshot step alone, given the rows: the rest of the round (and
        # the write guards, which read the store) have their own rules.
        SWEEP._backup_snapshots(rows, 50)
        self.assertEqual([], self.snapshots())

    def test_when_the_jobs_cannot_be_read_the_snapshot_is_kept(self):
        self.fake()
        result, item = self.start()
        self.finish(result)
        self.resolve(item["id"])
        with mock.patch.object(SWEEP.OPS, "stored", side_effect=OSError("store unreadable")):
            self.sweep()
        self.assertEqual([result["snapshot"]], self.snapshots())

    def test_a_running_backup_keeps_its_snapshot(self):
        self.fake()
        result, item = self.start()
        self.finish(result, state="InProgress")
        self.assertEqual("running", self.resolve(item["id"]))
        self.sweep()
        self.assertEqual([result["snapshot"]], self.snapshots())

    def test_a_snapshot_made_again_under_the_name_is_kept(self):
        kube = self.fake()
        result, item = self.start()
        self.finish(result)
        path = f"{SNAPS}/{result['snapshot']}"
        again = kube.object(path)
        kube.discard(path)
        again["metadata"].pop("uid")
        kube.add(again)
        self.resolve(item["id"])
        self.sweep()
        self.assertEqual([result["snapshot"]], self.snapshots(), "not the snapshot this backup took")
        self.assertFalse(self.job(item["id"])["ref"].get("snapshot_pending"))

    def test_a_backup_job_from_before_names_no_snapshot_and_removes_none(self):
        kube = self.fake()
        result, item = self.start()
        self.finish(result)
        with OPS.transaction() as t:
            items = t.items
            next(i for i in items if i["id"] == item["id"])["ref"] = {"namespace": LHNS, "name": result["backup"]}
            t.save(items)
        self.assertEqual("succeeded", self.resolve(item["id"]))
        self.sweep()
        self.assertEqual([result["snapshot"]], self.snapshots())
        self.assertFalse(any(m == "DELETE" and "/snapshots/" in p for m, p, *_ in kube.writes))

    def test_a_snapshot_whose_delete_fails_is_left_and_not_asked_again(self):
        kube = self.fake()
        result, item = self.start()
        self.finish(result)
        kube.fail(f"{SNAPS}/{result['snapshot']}", 500, method="DELETE")
        self.assertEqual("succeeded", self.resolve(item["id"]))
        self.sweep()
        self.sweep()
        self.assertEqual([result["snapshot"]], self.snapshots())
        self.assertEqual(1, sum(1 for m, p, *_ in kube.writes if m == "DELETE" and "/snapshots/" in p))
        self.assertEqual("succeeded", self.job(item["id"])["status"])


if __name__ == "__main__":
    unittest.main()
