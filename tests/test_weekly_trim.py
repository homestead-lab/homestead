"""A trim of one Longhorn volume on demand (Trim now). The weekly trim is
homestead_weekly_trim's, tested in test_weekly_trim_run."""
import copy
import sys
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
import homestead_longhorn as LH
import homestead_longhorn_jobs as LH_JOBS

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


def volume(name, groups=(), state="attached"):
    return {"metadata": {"name": name, "labels": {LH.GROUP_LABEL + g: "enabled" for g in groups}},
            "spec": {"size": str(10 * 1024 ** 3)}, "status": {"state": state}}


class WeeklyTrimTests(unittest.TestCase):
    def bind(self, cluster):
        saved = (LH.kget, LH.ksend, LH._cache, LH.STORAGE_CLASS, LH.DEFAULT_NS)
        self.addCleanup(lambda: LH.bind(*saved))   # put back what server.py bound
        LH.bind(cluster.get, cluster.send, {}, "longhorn")
        return cluster

    def test_a_volume_is_trimmed_now_only_while_attached(self):
        self.bind(Longhorn([], [volume("pvc-11111111-2222-4333-8444-555555555555"),
                                volume("pvc-66666666-7777-4888-8999-aaaaaaaaaaaa", state="detached")]))
        with mock.patch.object(LH_JOBS, "_backend") as backend, \
                mock.patch.object(LH_JOBS.ACCESS, "reachable", return_value=""):
            result = LH_JOBS.trim_volume("pvc-11111111-2222-4333-8444-555555555555")
            backend.assert_called_once_with("POST", "/volumes/pvc-11111111-2222-4333-8444-555555555555?action=trimFilesystem", {})
            self.assertIn("being trimmed", result["detail"])
            with self.assertRaisesRegex(ValueError, "only while it is in use"):
                LH_JOBS.trim_volume("pvc-66666666-7777-4888-8999-aaaaaaaaaaaa")
            for bad in ("../x", "pvc a", ""):
                with self.subTest(bad=bad), self.assertRaises(ValueError):
                    LH_JOBS.trim_volume(bad)
            self.assertEqual(1, backend.call_count)

    def test_trim_now_is_refused_while_longhorns_api_cannot_be_reached(self):
        self.bind(Longhorn([], [volume("pvc-11111111-2222-4333-8444-555555555555")]))
        with mock.patch.object(LH_JOBS, "_backend") as backend, \
                mock.patch.object(LH_JOBS.ACCESS, "reachable", return_value="Connection refused"):
            with self.assertRaisesRegex(ValueError, "cannot reach Longhorn's API.*Connection refused"):
                LH_JOBS.trim_volume("pvc-11111111-2222-4333-8444-555555555555")
        backend.assert_not_called()


if __name__ == "__main__":
    unittest.main()
