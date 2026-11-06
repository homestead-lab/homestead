"""An Unraid or Proxmox VM import that CDI never moves on ends, and lets go of the
VM's name: the "disks" and "convert" waits had no deadline and raise nothing, so
the job stayed "running" and its disk targets refused a retry. On the shared fake
API and the real job list."""
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import homestead_kube as K
import homestead_operations as JOBS
import homestead_unraid_vms as UVMS
import test_unraid_vms as fixture
from fakes.kube import Kube

DISKS_LIMIT = 30 * 60
CONVERT_LIMIT = 6 * 60 * 60
DVS = ("win11-disk", "win11-disk-2")


class ImportWaitTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(mock.patch.object(JOBS, "DATA_DIR", self.enterContext(tempfile.TemporaryDirectory())))
        self.kube = self.enterContext(Kube(
            {"apiVersion": "v1", "kind": "Service", "metadata": {"namespace": "cdi", "name": "cdi-uploadproxy",
                                                                 "labels": {"cdi.kubevirt.io": "cdi-uploadproxy"}}},
            groups=("cdi.kubevirt.io", "kubevirt.io")))
        for name in ("IMP", "kget", "ksend", "OPS"):
            self.addCleanup(setattr, UVMS, name, getattr(UVMS, name, None))
        self.imp = fixture.FakeImports(fixture.listing_lines())
        UVMS.bind(self.imp, K.kget, K.ksend, JOBS)
        self.enterContext(mock.patch.dict(JOBS.RESOLVERS, {}))
        UVMS.register(JOBS)
        self.now = 1000.0
        self.enterContext(mock.patch.object(UVMS, "_clock", lambda: self.now, create=True))
        self.ident = UVMS.start({"source": "nas", "vm": "Windows 11", "name": "win11"})["id"]

    def path(self, name):
        return f"{UVMS.CDI_API}/namespaces/lab/datavolumes/{name}"

    def phase(self, phase):
        for name in DVS:
            dv = self.kube.object(self.path(name))
            dv["status"] = {"phase": phase}
            self.kube.add(dv)

    def look(self, times=1):
        for _ in range(times):
            job = next(job for job in JOBS.list_operations() if job["id"] == self.ident)
        return job

    def set_ref(self, **values):
        with JOBS.transaction():
            items = JOBS._read()
            next(item for item in items if item["id"] == self.ident)["ref"].update(values)
            JOBS._write(items)

    def test_cdi_never_ready_ends_the_import_and_clears_what_it_made(self):
        self.phase("UploadScheduled")
        self.assertEqual("running", self.look()["status"])
        self.now += DISKS_LIMIT - 1
        self.assertEqual("running", self.look()["status"])
        self.now += 1
        job = self.look(3)
        self.assertEqual("failed", job["status"])
        self.assertIn("30 minutes", job["message"])
        self.assertIsNone(self.kube.object(self.path(DVS[0])), "what it made goes, so a retry can run")
        UVMS.start({"source": "nas", "vm": "Windows 11", "name": "win11"})

    def claims(self):
        # CDI's own claims: the copy wrote to them, so they are bound by this phase.
        for name in DVS:
            uid = self.kube.object(self.path(name))["metadata"]["uid"]
            self.kube.add({"apiVersion": "v1", "kind": "PersistentVolumeClaim",
                           "metadata": {"namespace": "lab", "name": name, "annotations": {},
                                        "ownerReferences": [{"kind": "DataVolume", "name": name, "uid": uid, "controller": True}]},
                           "spec": {"volumeName": f"pv-{name}"}, "status": {"phase": "Bound"}})

    def test_cdi_never_finishing_ends_the_import_and_keeps_the_copied_disks(self):
        self.phase("Pending")
        self.claims()
        self.set_ref(phase="convert")
        self.assertEqual("CDI is finishing the disk", self.look()["message"])
        self.now += CONVERT_LIMIT - 1
        self.assertEqual("running", self.look()["status"], "a big disk takes its time")
        self.now += 1
        job = self.look(3)
        self.assertEqual("failed", job["status"], job["message"])
        self.assertIn("6 hours", job["message"])
        self.assertIn(DVS[0], job["message"])
        self.assertTrue(self.kube.object(self.path(DVS[0])), "hours of copying are not thrown away on a guess")
        self.assertFalse(next(i for i in JOBS._read() if i["id"] == self.ident)["ref"].get("retain_resources"))

    def test_a_disk_with_no_bound_claim_is_held_and_says_why_and_can_be_cancelled(self):
        # Keeping disks needs a bound claim. With none the clean-up holds, in words,
        # as it does after a refused VM: this job is not "ended", but it says so.
        self.phase("Pending")
        self.set_ref(phase="convert")
        self.look()
        self.now += CONVERT_LIMIT
        job = self.look(2)
        self.assertEqual("running", job["status"])
        self.assertIn("not bound", job["message"])
        self.assertIn("6 hours", job["message"])
        self.assertTrue(job["cancellable"])

    def test_an_older_import_with_no_receipt_ends_too_and_removes_nothing(self):
        self.phase("UploadScheduled")
        self.set_ref(cdi_cleanup=None)
        self.look()
        self.now += DISKS_LIMIT
        job = self.look()
        self.assertEqual("failed", job["status"])
        self.assertTrue(self.kube.object(self.path(DVS[0])))

    def test_disks_that_become_ready_in_time_are_not_failed(self):
        self.phase("UploadScheduled")
        self.look()
        self.now += DISKS_LIMIT - 1
        self.phase("Succeeded")
        self.set_ref(phase="convert")
        self.now += 1
        self.assertNotEqual("failed", self.look()["status"])


if __name__ == "__main__":
    unittest.main()
