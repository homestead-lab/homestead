"""Longhorn's recurring jobs, backup target and the target's Secret are changed
on the object as stored when they are saved: what someone else put there
meanwhile, or what Homestead does not edit, stays (#621). The job editor names
the version it opened, so a job changed since is not saved over."""
import base64
import json
import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, os.environ.get("HOMESTEAD_TEST_SERVER_DIR", str(Path(__file__).resolve().parents[1] / "server")))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import homestead_kube as K
import homestead_longhorn as LH
import homestead_longhorn_jobs as JOBS
import homestead_longhorn_volumes as VOLUMES
import homestead_longhorn_target as BACKUP_TARGET
from fakes.kube import Kube

NS = "/apis/longhorn.io/v1beta2/namespaces/longhorn-system"
SECRET = "/api/v1/namespaces/longhorn-system/secrets/homestead-backup-target"
TARGET = f"{NS}/backuptargets/default"


def job(name="daily", groups=("default",), **extra):
    return {"apiVersion": "longhorn.io/v1beta2", "kind": "RecurringJob",
            "metadata": {"name": name, "namespace": "longhorn-system", "generation": 1,
                         "labels": {"homestead.io/managed": "true"}},
            "spec": dict({"name": name, "task": "snapshot", "cron": "0 2 * * *", "retain": 7,
                          "concurrency": 1, "groups": list(groups), "labels": {}}, **extra)}


def form(**changes):
    return dict({"name": "daily", "task": "snapshot", "cron": "0 3 * * *", "retain": 5,
                 "concurrency": 1, "groups": ["default"]}, **changes)


class LonghornWriteTests(unittest.TestCase):
    def setUp(self):
        saved = (LH.kget, LH.ksend, LH._cache, LH.DEFAULT_NS)
        self.addCleanup(lambda: LH.bind(*saved))
        self.bind()

    def bind(self, send=None):
        LH.bind(K.kget, send or K.send, {}, "lab")

    def meanwhile(self, kube, path, change):
        """A send that lets another writer change the object at path just
        before this one saves it, once."""
        raced = []

        def send(method, target, body=None, **options):
            if method == "PUT" and target == path and not raced:
                raced.append(1)
                other = kube.object(path)
                change(other)
                K.send("PUT", path, other)
            return K.send(method, target, body, **options)
        return send

    # recurring jobs ------------------------------------------------------

    def test_saving_a_job_keeps_labels_annotations_and_fields_it_does_not_edit(self):
        stored = job(parameters={"full-backup-interval": "4"})
        stored["metadata"]["labels"]["team"] = "media"
        stored["metadata"]["annotations"] = {"note": "set by hand"}
        with Kube(stored) as kube:
            JOBS.save_job(form())
            saved = kube.object(f"{NS}/recurringjobs/daily")
        self.assertEqual("0 3 * * *", saved["spec"]["cron"])
        self.assertEqual(5, saved["spec"]["retain"])
        self.assertEqual({"full-backup-interval": "4"}, saved["spec"]["parameters"])
        self.assertEqual("media", saved["metadata"]["labels"]["team"])
        self.assertEqual("true", saved["metadata"]["labels"]["homestead.io/managed"])
        self.assertEqual("set by hand", saved["metadata"]["annotations"]["note"])

    def test_a_job_saved_meanwhile_is_saved_again_on_what_is_there_now(self):
        with Kube(job()) as kube:
            path = f"{NS}/recurringjobs/daily"
            self.bind(self.meanwhile(kube, path, lambda other: other["metadata"].setdefault(
                "annotations", {}).update({"note": "added meanwhile"})))
            JOBS.save_job(form())
            saved = kube.object(path)
        self.assertEqual("0 3 * * *", saved["spec"]["cron"])
        self.assertEqual("added meanwhile", saved["metadata"]["annotations"]["note"])

    def opened(self):
        """The generation of daily as the editor gets it from the list."""
        return next(row["generation"] for row in JOBS.list_jobs() if row["name"] == "daily")

    def test_the_editor_does_not_save_over_a_job_changed_since_it_opened(self):
        with Kube(job()) as kube:
            path = f"{NS}/recurringjobs/daily"
            opened = self.opened()
            other = kube.object(path)
            other["spec"]["groups"] = ["default", "media"]
            K.send("PUT", path, other)
            reply = JOBS.save_job(form(generation=opened))
            saved = kube.object(path)
        self.assertEqual(409, reply.code)
        self.assertIn("changed or deleted", reply.body["error"])
        self.assertEqual(["default", "media"], saved["spec"]["groups"])
        self.assertEqual("0 2 * * *", saved["spec"]["cron"])

    def test_the_editor_saves_a_job_that_has_only_run_since_it_opened(self):
        # Longhorn counts each run in the job's status: that moves its
        # resourceVersion, not its generation, and is no reason to refuse.
        with Kube(job()) as kube:
            path = f"{NS}/recurringjobs/daily"
            opened = self.opened()
            ran = kube.object(path)
            ran["status"] = {"executionCount": 3}
            K.send("PUT", path + "/status", ran)
            reply = JOBS.save_job(form(generation=opened))
            saved = kube.object(path)
        self.assertEqual({"ok": True, "name": "daily"}, reply)
        self.assertEqual("0 3 * * *", saved["spec"]["cron"])

    def test_the_editor_does_not_make_again_a_job_deleted_since_it_opened(self):
        with Kube(job()) as kube:
            path = f"{NS}/recurringjobs/daily"
            opened = self.opened()
            K.send("DELETE", path)
            reply = JOBS.save_job(form(generation=opened))
            saved = kube.object(path)
        self.assertEqual(409, reply.code)
        self.assertIn("changed or deleted", reply.body["error"])
        self.assertIsNone(saved)

    def test_the_editor_saves_a_job_unchanged_since_it_opened(self):
        with Kube(job()) as kube:
            JOBS.save_job(form(generation=self.opened()))
            saved = kube.object(f"{NS}/recurringjobs/daily")
        self.assertEqual("0 3 * * *", saved["spec"]["cron"])

    def test_saving_or_deleting_a_job_refreshes_the_overview_the_editor_reads(self):
        with Kube(job()):
            for act in (lambda: JOBS.save_job(form()), lambda: JOBS.delete_job("daily")):
                cache = {"lhov": {"jobs": []}, "lhjobs": [], "other": 1}
                LH.bind(K.kget, K.send, cache, "lab")
                act()
                self.assertEqual({"other"}, set(cache))

    def test_a_new_job_from_the_editor_does_not_replace_one_of_the_same_name(self):
        with Kube(job()) as kube:
            reply = JOBS.save_job(form(generation=""))
            saved = kube.object(f"{NS}/recurringjobs/daily")
        self.assertEqual(409, reply.code)
        self.assertIn("already", reply.body["error"])
        self.assertEqual("0 2 * * *", saved["spec"]["cron"])

    def test_a_new_job_is_made(self):
        with Kube() as kube:
            JOBS.save_job(form(name="weekly", generation=""))
            saved = kube.object(f"{NS}/recurringjobs/weekly")
        self.assertEqual("weekly", saved["spec"]["name"])
        self.assertEqual("true", saved["metadata"]["labels"]["homestead.io/managed"])

    def test_joining_a_group_keeps_a_group_the_job_joined_meanwhile(self):
        with Kube(job()) as kube:
            path = f"{NS}/recurringjobs/daily"
            self.bind(self.meanwhile(kube, path, lambda other: other["spec"]["groups"].append("media")))
            VOLUMES.save_group({"name": "photos", "jobs": ["daily"], "volumes": []})
            saved = kube.object(path)
        self.assertEqual(["default", "media", "photos"], saved["spec"]["groups"])

    # backup target -------------------------------------------------------

    def test_typed_keys_keep_other_keys_in_the_targets_secret(self):
        secret = {"apiVersion": "v1", "kind": "Secret",
                  "metadata": {"name": "homestead-backup-target", "namespace": "longhorn-system",
                               "labels": {"team": "ops"}},
                  "data": {"AWS_ACCESS_KEY_ID": base64.b64encode(b"old").decode(),
                           "AWS_CERT": base64.b64encode(b"-----BEGIN CERTIFICATE-----").decode()}}
        with Kube(secret) as kube:
            BACKUP_TARGET.set_backup_target("s3://bucket@eu-west-1/", keys={"access_key": "new", "secret_key": "s3cret"})
            saved = kube.object(SECRET)
        data = {key: base64.b64decode(value).decode() for key, value in (saved.get("data") or {}).items()}
        data.update(saved.get("stringData") or {})
        self.assertEqual("new", data["AWS_ACCESS_KEY_ID"])
        self.assertEqual("s3cret", data["AWS_SECRET_ACCESS_KEY"])
        self.assertEqual("-----BEGIN CERTIFICATE-----", data["AWS_CERT"])
        self.assertEqual("ops", saved["metadata"]["labels"]["team"])

    def test_the_target_keeps_its_labels_and_a_change_made_meanwhile_is_saved_again(self):
        target = {"apiVersion": "longhorn.io/v1beta2", "kind": "BackupTarget",
                  "metadata": {"name": "default", "namespace": "longhorn-system",
                               "labels": {"team": "ops"}},
                  "spec": {"backupTargetURL": "", "credentialSecret": "", "pollInterval": "5m"}}
        with Kube(target) as kube:
            self.bind(self.meanwhile(kube, TARGET, lambda other: other["metadata"].setdefault(
                "annotations", {}).update({"note": "added meanwhile"})))
            BACKUP_TARGET.set_backup_target("nfs://192.0.2.20:/backups", poll="10m")
            saved = kube.object(TARGET)
        self.assertEqual("nfs://192.0.2.20:/backups", saved["spec"]["backupTargetURL"])
        self.assertEqual("10m", saved["spec"]["pollInterval"])
        self.assertEqual("ops", saved["metadata"]["labels"]["team"])
        self.assertEqual("added meanwhile", saved["metadata"]["annotations"]["note"])


if __name__ == "__main__":
    unittest.main()
