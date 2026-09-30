"""A weekly trim for every Longhorn volume, by default, and a trim on demand."""
import copy
import sys
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
import homestead_longhorn as LH

JOBS = f"{LH.API}/namespaces/{LH.LHNS}/recurringjobs"
VOLS = f"{LH.API}/namespaces/{LH.LHNS}/volumes"


class Longhorn:
    def __init__(self, jobs=(), volumes=()):
        self.jobs = {j["metadata"]["name"]: copy.deepcopy(j) for j in jobs}
        self.volumes = list(volumes)
        self.sent = []

    def get(self, path):
        if path == JOBS:
            return {"items": list(self.jobs.values())}
        if path.startswith(JOBS + "/"):
            name = path.rsplit("/", 1)[1]
            if name in self.jobs:
                return self.jobs[name]
            raise urllib.error.HTTPError(path, 404, "missing", {}, None)
        if path == VOLS:
            return {"items": self.volumes}
        if path.startswith(VOLS + "/"):
            found = next((v for v in self.volumes if v["metadata"]["name"] == path.rsplit("/", 1)[1]), None)
            if found:
                return found
            raise urllib.error.HTTPError(path, 404, "missing", {}, None)
        if path.endswith("/cronjobs"):
            return {"items": []}
        raise AssertionError(path)

    def send(self, method, path, body=None, **kw):
        self.sent.append((method, path, body))
        if method in ("POST", "PUT"):
            body = copy.deepcopy(body)
            body["metadata"]["resourceVersion"] = str(len(self.sent))   # as the API server would
            self.jobs[body["metadata"]["name"]] = body
        return body


def job(name, task, groups):
    return {"metadata": {"name": name, "resourceVersion": "1"},
            "spec": {"name": name, "task": task, "cron": "0 1 * * *", "groups": groups}}


def volume(name, groups=(), state="attached"):
    return {"metadata": {"name": name, "labels": {LH.GROUP_LABEL + g: "enabled" for g in groups}},
            "spec": {"size": str(10 * 1024 ** 3)}, "status": {"state": state}}


class WeeklyTrimTests(unittest.TestCase):
    def bind(self, cluster):
        LH.bind(cluster.get, cluster.send, {}, "longhorn")
        return cluster

    def test_every_volume_gets_a_weekly_trim_its_groups_included(self):
        lh = self.bind(Longhorn([job("nightly", "snapshot", ["media"])], [volume("pvc-a", ["media"]), volume("pvc-b")]))
        made, change, written = LH.ensure_weekly_trim(False)
        self.assertTrue(made)
        self.assertEqual(["default", "media"], written)
        spec = lh.jobs[LH.TRIM_JOB]["spec"]
        self.assertEqual(("filesystem-trim", "0 4 * * 6", 0), (spec["task"], spec["cron"], spec["retain"]))
        self.assertEqual(["default", "media"], sorted(spec["groups"]))
        self.assertEqual((False, "", ["default", "media"]), LH.ensure_weekly_trim(True, written), "already right: nothing to do")
        # A new group appears: the trim follows it, and a schedule someone changed stays.
        lh.jobs[LH.TRIM_JOB]["spec"]["cron"] = "0 2 * * 0"
        lh.volumes.append(volume("pvc-c", ["cameras"]))
        made, change, written = LH.ensure_weekly_trim(True, written)
        self.assertEqual((False, "weekly trim now covers cameras, default, media"), (made, change))
        self.assertEqual("0 2 * * 0", lh.jobs[LH.TRIM_JOB]["spec"]["cron"])
        # Someone takes a group out: theirs from now on, not put back.
        lh.jobs[LH.TRIM_JOB]["spec"]["groups"] = ["default"]
        lh.volumes.append(volume("pvc-d", ["databases"]))
        self.assertEqual((False, "", None), LH.ensure_weekly_trim(True, written))
        self.assertEqual(["default"], lh.jobs[LH.TRIM_JOB]["spec"]["groups"])

    def test_one_someone_deleted_is_not_made_again_nor_a_second_trim(self):
        self.bind(Longhorn([], [volume("pvc-a")]))
        self.assertEqual((False, "", None), LH.ensure_weekly_trim(True))
        lh = self.bind(Longhorn([job("weekly-trim", "filesystem-trim", ["default"])], [volume("pvc-a")]))
        self.assertEqual((False, "", None), LH.ensure_weekly_trim(False), "a trim of their own already covers everything")
        self.assertNotIn(LH.TRIM_JOB, lh.jobs)

    def test_nothing_is_done_when_the_jobs_cannot_be_read(self):
        lh = Longhorn()
        LH.bind(lambda path: (_ for _ in ()).throw(OSError("api down")), lh.send, {}, "longhorn")
        self.assertIsNone(LH.ensure_weekly_trim(False))
        self.assertEqual([], lh.sent)

    def test_a_volume_is_trimmed_now_only_while_attached(self):
        self.bind(Longhorn([], [volume("pvc-11111111-2222-4333-8444-555555555555"),
                                volume("pvc-66666666-7777-4888-8999-aaaaaaaaaaaa", state="detached")]))
        with mock.patch.object(LH, "_backend") as backend:
            result = LH.trim_volume("pvc-11111111-2222-4333-8444-555555555555")
            backend.assert_called_once_with("POST", "/volumes/pvc-11111111-2222-4333-8444-555555555555?action=trimFilesystem", {})
            self.assertIn("being trimmed", result["detail"])
            with self.assertRaisesRegex(ValueError, "only while it is in use"):
                LH.trim_volume("pvc-66666666-7777-4888-8999-aaaaaaaaaaaa")
            for bad in ("../x", "pvc a", ""):
                with self.subTest(bad=bad), self.assertRaises(ValueError):
                    LH.trim_volume(bad)
            self.assertEqual(1, backend.call_count)


if __name__ == "__main__":
    unittest.main()
