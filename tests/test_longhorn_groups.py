"""Data protection groups, coverage, backups of gone volumes and target keys."""
import copy
import json
import sys
import unittest
import urllib.error
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))

import homestead_longhorn as LH

G, J = LH.GROUP_LABEL, LH.JOB_LABEL
API = "/apis/longhorn.io/v1beta2/namespaces/longhorn-system"


def volume(name, pvc, labels):
    return {"metadata": {"name": name, "labels": dict(labels)},
            "spec": {"size": str(5 * 1024 ** 3)},
            "status": {"kubernetesStatus": {"pvcName": pvc, "namespace": "lab"}, "robustness": "healthy"}}


def job(name, task, groups):
    return {"metadata": {"name": name, "resourceVersion": "1"},
            "spec": {"name": name, "task": task, "cron": "0 2 * * *", "retain": 7, "concurrency": 1,
                     "groups": list(groups)}}


class FakeLonghorn:
    """Longhorn's volumes, jobs and PVCs, patched as the API would."""

    def __init__(self):
        self.volumes = {
            "pvc-a": volume("pvc-a", "frigate", {G + "default": "enabled"}),
            "pvc-b": volume("pvc-b", "plex", {G + "default": "enabled"}),
            "pvc-c": volume("pvc-c", "paperless", {G + "media": "enabled"}),
        }
        self.jobs = {"daily": job("daily", "snapshot", ["default"]),
                     "trim": job("trim", "filesystem-trim", ["default", "media"])}
        self.pvcs = {"frigate": {"metadata": {"labels": {}}}, "plex": {"metadata": {"labels": {LH.SOURCE_LABEL: "enabled"}}},
                     "paperless": {"metadata": {"labels": {}}}}
        self.secrets = {}
        self.sent = []

    def get(self, path):
        return copy.deepcopy(self._get(path))

    def _get(self, path):
        if "/recurringjobs/" in path:
            if path.rsplit("/", 1)[-1] in self.jobs:
                return self.jobs[path.rsplit("/", 1)[-1]]
            raise urllib.error.HTTPError(path, 404, "missing", None, None)
        if path.endswith("/volumes"):
            return {"items": list(self.volumes.values())}
        if "/volumes/" in path:
            return self.volumes[path.rsplit("/", 1)[-1]]
        if path.endswith("/recurringjobs"):
            return {"items": list(self.jobs.values())}
        if path.endswith("/cronjobs"):
            return {"items": []}
        if "/persistentvolumeclaims/" in path:
            return self.pvcs[path.rsplit("/", 1)[-1]]
        if "/secrets/" in path:
            name = path.rsplit("/", 1)[-1]
            if name in self.secrets:
                return self.secrets[name]
            raise urllib.error.HTTPError(path, 404, "missing", None, None)
        if path.endswith("/backuptargets"):
            return {"items": []}
        if "/backuptargets/" in path:
            raise urllib.error.HTTPError(path, 404, "missing", None, None)
        if path.endswith("/backupvolumes"):
            return {"items": [
                {"metadata": {"name": "pvc-a"}, "status": {"lastBackupAt": "2026-09-20T02:00:00Z", "size": "1048576"}},
                {"metadata": {"name": "pvc-gone"}, "status": {
                    "lastBackupAt": "2026-09-01T02:00:00Z",
                    "labels": {"KubernetesStatus": json.dumps({"pvcName": "old-app", "namespace": "lab"})}}}]}
        if path.endswith("/backups"):
            return {"items": [{"metadata": {"name": "b1", "labels": {"backup-volume": "pvc-gone"}}, "status": {}},
                              {"metadata": {"name": "b2", "labels": {"backup-volume": "pvc-gone"}}, "status": {}}]}
        raise AssertionError(path)

    def send(self, method, path, body=None, **kwargs):
        self.sent.append((method, path, copy.deepcopy(body)))
        if method == "PATCH" and "/volumes/" in path:
            labels = self.volumes[path.rsplit("/", 1)[-1]]["metadata"]["labels"]
            for key, value in body["metadata"]["labels"].items():
                if value is None:
                    labels.pop(key, None)
                else:
                    labels[key] = value
            # Longhorn puts a volume with no job or group label in default.
            if not any(k.startswith((G, J)) for k in labels):
                labels[G + "default"] = "enabled"
        elif method == "PUT" and "/recurringjobs/" in path:
            self.jobs[body["metadata"]["name"]] = body
        elif method == "POST" and path.endswith("/secrets"):
            self.secrets[body["metadata"]["name"]] = body
        return body or {}


class GroupTests(unittest.TestCase):
    def setUp(self):
        self.lh = FakeLonghorn()
        LH.bind(self.lh.get, self.lh.send, {})

    def labels(self, name):
        return self.lh.volumes[name]["metadata"]["labels"]

    def test_a_new_group_takes_its_volumes_out_of_default_and_names_its_jobs(self):
        result = LH.save_group({"name": "cameras", "volumes": ["pvc-a"], "jobs": ["daily"]})

        self.assertEqual({G + "cameras": "enabled"}, self.labels("pvc-a"))
        self.assertEqual(["pvc-a"], result["left_default"])
        self.assertEqual(["default", "cameras"], self.lh.jobs["daily"]["spec"]["groups"])
        self.assertEqual({G + "default": "enabled"}, self.labels("pvc-b"))

    def test_joining_can_keep_default_too(self):
        LH.save_group({"name": "cameras", "volumes": ["pvc-a"], "leave_default": False})
        self.assertEqual({G + "default": "enabled", G + "cameras": "enabled"}, self.labels("pvc-a"))

    def test_a_group_with_nothing_in_it_is_refused(self):
        with self.assertRaisesRegex(ValueError, "not kept anywhere"):
            LH.save_group({"name": "empty"})
        with self.assertRaisesRegex(ValueError, "lowercase"):
            LH.save_group({"name": "Bad Name", "volumes": ["pvc-a"]})

    def test_renaming_moves_members_and_jobs(self):
        LH.save_group({"original": "media", "name": "films", "volumes": ["pvc-c"], "jobs": ["trim"]})

        self.assertEqual({G + "films": "enabled"}, self.labels("pvc-c"))
        self.assertEqual(["default", "films"], self.lh.jobs["trim"]["spec"]["groups"])

    def test_a_volume_leaving_its_only_group_goes_back_to_default(self):
        result = LH.save_group({"name": "media", "volumes": [], "jobs": ["trim"]})

        self.assertEqual(["pvc-c"], result["back_to_default"])
        self.assertEqual({G + "default": "enabled"}, self.labels("pvc-c"))

    def test_default_keeps_a_volume_that_has_nowhere_else_to_go(self):
        result = LH.save_group({"name": "default", "volumes": [], "jobs": ["daily"]})
        self.assertEqual(["pvc-a", "pvc-b"], result["kept_in_default"])

    def test_the_pvc_is_labelled_when_longhorn_reads_labels_from_it(self):
        LH.assign("pvc-b", "cameras")
        pvc_patches = [path for method, path, _ in self.lh.sent if method == "PATCH" and "persistentvolumeclaims" in path]
        self.assertEqual(["/api/v1/namespaces/lab/persistentvolumeclaims/plex"], pvc_patches)
        LH.assign("pvc-a", "cameras")
        self.assertEqual(1, sum("persistentvolumeclaims" in path for method, path, _ in self.lh.sent if method == "PATCH"))

    def test_deleting_says_what_it_leaves_behind(self):
        result = LH.delete_group("media")

        self.assertEqual(["paperless"], result["back_to_default"])
        self.assertEqual([], result["idle_jobs"])
        self.assertEqual(["default"], self.lh.jobs["trim"]["spec"]["groups"])
        with self.assertRaisesRegex(ValueError, "Longhorn's own"):
            LH.delete_group("default")


class CoverageTests(unittest.TestCase):
    def setUp(self):
        self.lh = FakeLonghorn()
        LH.bind(self.lh.get, self.lh.send, {})

    def test_a_trim_alone_protects_nothing(self):
        view = LH.overview()

        by_name = {v["name"]: v for v in view["volumes"]}
        self.assertEqual(["daily"], by_name["pvc-a"]["protected_by"])
        self.assertEqual([], by_name["pvc-c"]["protected_by"])
        self.assertEqual((2, 0, ["paperless"]), (view["protected"], view["backed_up"], view["unprotected"]))
        self.assertEqual({"name": "media", "volumes": ["pvc-c"], "jobs": ["trim"]},
                         next(g for g in view["group_rows"] if g["name"] == "media"))

    def test_cleanups_and_trims_keep_nothing(self):
        LH.save_job({"name": "weekly-cleanup", "task": "snapshot-cleanup", "cron": "30 4 * * 6", "retain": 0})
        self.assertEqual(0, self.lh.sent[-1][2]["spec"]["retain"])
        with self.assertRaisesRegex(ValueError, "at least 1"):
            LH.save_job({"name": "snap", "task": "snapshot", "cron": "0 2 * * *", "retain": 0})


class BackupTests(unittest.TestCase):
    def setUp(self):
        self.lh = FakeLonghorn()
        LH.bind(self.lh.get, self.lh.send, {})

    def test_backups_of_a_deleted_volume_are_listed(self):
        rows = {row["name"]: row for row in LH.backup_volumes()}

        self.assertFalse(rows["pvc-gone"]["exists"])
        self.assertEqual("old-app", rows["pvc-gone"]["pvc"])
        self.assertEqual(2, rows["pvc-gone"]["count"])
        self.assertTrue(rows["pvc-a"]["exists"])
        self.assertEqual("frigate", rows["pvc-a"]["pvc"])

    def test_typed_s3_keys_become_the_targets_secret(self):
        LH.set_backup_target("s3://backups@us-east-1/", "", "5m",
                             {"access_key": "AK", "secret_key": "SK", "endpoint": "http://192.0.2.20:9000"})

        secret = self.lh.secrets[LH.TARGET_SECRET]
        self.assertEqual({"AWS_ACCESS_KEY_ID": "AK", "AWS_SECRET_ACCESS_KEY": "SK",
                          "AWS_ENDPOINTS": "http://192.0.2.20:9000"}, secret["stringData"])
        target = [body for method, path, body in self.lh.sent if path.endswith("/backuptargets")][0]
        self.assertEqual(LH.TARGET_SECRET, target["spec"]["credentialSecret"])

    def test_a_target_that_cannot_work_is_refused(self):
        with self.assertRaisesRegex(ValueError, "keys"):
            LH.set_backup_target("s3://backups@us-east-1/")
        with self.assertRaisesRegex(ValueError, "starts with"):
            LH.set_backup_target("backups@us-east-1/")
        with self.assertRaisesRegex(ValueError, "both"):
            LH.set_backup_target("s3://b@r/", keys={"access_key": "AK"})


if __name__ == "__main__":
    unittest.main()
