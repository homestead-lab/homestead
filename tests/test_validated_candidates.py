"""The daily workflow's candidates and how their results are recorded (#1348, 5.5)."""
import gzip
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import validated_candidates as CANDIDATES

PACKAGES = gzip.compress(b"""Package: linux-image-7.0.0-43-generic
Version: 7.0.0-43.43

Package: linux-image-7.0.0-44-generic
Version: 7.0.0-44.44

Package: linux-image-unsigned-7.0.0-44-generic
Version: 7.0.0-44.44

Package: curl
Version: 8.0
""")


def fetch(url, headers=None):
    if "archive.ubuntu.com" in url:
        return PACKAGES
    if "k3s-io/k3s" in url:
        return json.dumps([{"tag_name": "v1.37.1+k3s1"}, {"tag_name": "v1.38.0-rc1+k3s1", "prerelease": True}]).encode()
    return json.dumps([{"tag_name": "v1.9.3"}, {"tag_name": "v1.9.2"}]).encode()


class FindTests(unittest.TestCase):
    def test_the_newest_versions_the_list_does_not_name(self):
        document = json.loads((ROOT / "data" / "validated-versions.json").read_text())
        found = CANDIDATES.find(limit=20, fetch=fetch, document=document)
        kernels = [c["kernel"] for c in found if c["kind"] == "host"]
        self.assertEqual(["linux-image-7.0.0-44-generic=7.0.0-44.44"], kernels, "7.0.0-43 is listed (bad) already")
        platform = {c["part"]: c["version"] for c in found if c["kind"] == "platform"}
        self.assertEqual("v1.37.1+k3s1", platform["k3s"], "no release candidates")
        self.assertEqual("v1.9.3", platform["kubevirt"])
        self.assertEqual(3, len(CANDIDATES.find(limit=3, fetch=fetch, document=document)))


class RecordTests(unittest.TestCase):
    def test_passed_is_validated_and_failed_is_bad_with_what_it_affects(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        data = Path(folder.name, "list.json")
        data.write_text((ROOT / "data" / "validated-versions.json").read_text())
        good = Path(folder.name, "good.json")
        good.write_text(json.dumps({"run": "https://example.com/run/1", "checks": [{"check": "vm", "passed": True}],
                                    "candidate": {"kind": "host", "key": "ubuntu-26.04-amd64", "title": "kernel 7.0.0-44.44",
                                                  "package": "linux-image-7.0.0-44-generic", "version": "7.0.0-44.44"}}))
        bad = Path(folder.name, "bad.json")
        bad.write_text(json.dumps({"run": "https://example.com/run/2",
                                   "checks": [{"check": "vm", "passed": False, "affects": ["kubevirt"]}],
                                   "candidate": {"kind": "platform", "part": "kubevirt", "version": "v1.9.3", "title": "kubevirt v1.9.3"}}))
        with mock.patch.object(CANDIDATES, "DATA", data):
            summary = CANDIDATES.record([good, bad], now=1_900_000_000)
        self.assertEqual(["kernel 7.0.0-44.44: validated", "kubevirt v1.9.3: bad"], summary)
        document = json.loads(data.read_text())
        kernel = next(e for e in document["host"]["ubuntu-26.04-amd64"] if e["version"] == "7.0.0-44.44")
        self.assertEqual(("validated", "https://example.com/run/1"), (kernel["status"], kernel["evidence"]))
        self.assertEqual(["gpu-passthrough", "real-disks"], kernel["not_covered"])
        broken = document["platform"]["kubevirt"][-1]
        self.assertEqual(("bad", ["kubevirt"], "vm failed"), (broken["status"], broken["affects"], broken["reason"]))
        self.assertEqual("2030-03-17T17:46:40Z", document["generated"])


if __name__ == "__main__":
    unittest.main()
