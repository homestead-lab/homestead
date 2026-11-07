"""Deleting a VM and keeping its disks keeps them: a disk is let go of before
the VM goes, and if that can't be done or checked, nothing is deleted (#600).
Runs on the shared fake API (tests/fakes/kube.py), which deletes what a
deleted VM still owns, as Kubernetes' garbage collector does."""
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import homestead_kube as K
import homestead_operations as OPS
import homestead_vms as VMS
from fakes.kube import Kube
from rebind import keep_bound

OWNED = [{"apiVersion": "kubevirt.io/v1", "kind": "VirtualMachine", "name": "web", "uid": "u1"}]
VM = {"apiVersion": "kubevirt.io/v1", "kind": "VirtualMachine",
      "metadata": {"name": "web", "namespace": "lab", "uid": "u1", "resourceVersion": "10"},
      "spec": {"runStrategy": "Halted",
               "dataVolumeTemplates": [{"metadata": {"name": "web-disk"},
                                        "spec": {"source": {"blank": {}},
                                                 "pvc": {"resources": {"requests": {"storage": "20Gi"}}}}}],
               "template": {"spec": {
                   "domain": {"cpu": {"cores": 2}, "memory": {"guest": "2Gi"},
                              "devices": {"disks": [{"name": "root", "disk": {"bus": "virtio"}, "bootOrder": 1}]}},
                   "volumes": [{"name": "root", "dataVolume": {"name": "web-disk"}}]}}},
      "status": {"printableStatus": "Stopped"}}
# The VM owns both the DataVolume and its claim, as dataVolumeTemplates leave them.
DATAVOLUME = {"apiVersion": "cdi.kubevirt.io/v1beta1", "kind": "DataVolume",
              "metadata": {"name": "web-disk", "namespace": "lab", "ownerReferences": OWNED},
              "status": {"phase": "Succeeded"}}
CLAIM = {"apiVersion": "v1", "kind": "PersistentVolumeClaim",
         "metadata": {"name": "web-disk", "namespace": "lab", "ownerReferences": OWNED}}
VM_PATH = "/apis/kubevirt.io/v1/namespaces/lab/virtualmachines/web"
DV = "/apis/cdi.kubevirt.io/v1beta1/namespaces/lab/datavolumes/web-disk"
PVC = "/api/v1/namespaces/lab/persistentvolumeclaims/web-disk"


class KeepDisksTests(unittest.TestCase):
    def setUp(self):
        # The shared fake runs the real write guards; the storage guard takes the job
        # store's lock, which lives under OPS.DATA_DIR (/data, not writable as non-root).
        data = self.enterContext(tempfile.TemporaryDirectory())
        self.enterContext(mock.patch.object(OPS, "DATA_DIR", data))
        keep_bound(self, VMS)   # put back what VMS.bind() sets
        VMS.bind(K.kget, K.ksend, lambda *a, **k: [])
        platform = mock.patch.object(VMS, "platform", lambda: {"harvester": False, "cdi": True})
        platform.start()
        self.addCleanup(platform.stop)

    def cluster(self, *objects):
        """A stopped VM whose one disk finished importing."""
        return Kube(VM, *(objects or (DATAVOLUME, CLAIM)))

    def owners(self, kube, path):
        disk = kube.object(path)
        self.assertIsNotNone(disk, f"{path} was deleted with the VM")
        return disk["metadata"].get("ownerReferences")

    def test_kept_disks_are_let_go_before_the_vm_is_deleted(self):
        with self.cluster() as kube:
            VMS.delete("lab", "web")
        self.assertIsNone(kube.object(VM_PATH))
        self.assertEqual((None, None), (self.owners(kube, DV), self.owners(kube, PVC)),
                         "the kept disk outlives the VM: nothing left for the garbage collector to take")
        writes = [(method, path) for method, path, _, _ in kube.writes]
        self.assertLess(writes.index(("PATCH", PVC)), writes.index(("DELETE", VM_PATH)))

    def test_a_failed_read_of_a_kept_disk_stops_the_delete(self):
        for path in (DV, PVC):
            with self.subTest(path), self.cluster() as kube:
                kube.fail(path, 500, method="GET")
                with self.assertRaisesRegex(ValueError, "web-disk could not be checked, so nothing was deleted"):
                    VMS.delete("lab", "web")
                self.assertIsNotNone(kube.object(VM_PATH))
                self.assertNotIn("DELETE", [method for method, *_ in kube.writes])

    def test_a_disk_without_a_datavolume_still_has_its_claim_let_go(self):
        # A DataVolume CDI already cleaned up is not found; the claim is what keeps the data.
        with self.cluster(CLAIM) as kube:
            VMS.delete("lab", "web")
        self.assertIsNone(kube.object(VM_PATH))
        self.assertIsNone(self.owners(kube, PVC))

    def test_a_disk_that_still_names_the_vm_stops_the_delete(self):
        with self.cluster() as kube:
            for path in (DV, PVC):  # the API answers the patch but the owner stays
                kube.react("PATCH", path, lambda method, path, body: kube.object(path))
            with self.assertRaisesRegex(ValueError, "web-disk still belongs to web, so nothing was deleted"):
                VMS.delete("lab", "web")
            self.assertIsNotNone(kube.object(VM_PATH))
            self.assertEqual(OWNED, self.owners(kube, PVC))


if __name__ == "__main__":
    unittest.main()
