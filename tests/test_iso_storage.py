import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
import homestead_isos as ISOS
import homestead_kube as K
import homestead_operations as OPS
import server
from fakes.kube import Kube

DAY = 86400
NOW = 1_800_000_000


LIBRARY = "/api/v1/namespaces/lab/configmaps/homestead-iso-library"
PVCS = "/api/v1/namespaces/lab/persistentvolumeclaims"


class Cluster:
    """ISO volumes in lab, and the VMs that have them in a drive, on the shared
    fake API (tests/fakes/kube.py)."""

    def __init__(self, test, keep_days="7"):
        self.kube = test.enterContext(Kube(
            {"apiVersion": "v1", "kind": "ConfigMap", "metadata": {"name": "homestead-iso-library", "namespace": "lab"},
             "data": {"folders": "[]", "keep_days": keep_days}}))
        # The shared fake runs the real write guards; the storage guard takes the job store's lock under DATA_DIR.
        test.enterContext(mock.patch.object(OPS, "DATA_DIR", test.enterContext(tempfile.TemporaryDirectory())))
        test.enterContext(mock.patch.multiple(ISOS, kget=ISOS.kget, ksend=ISOS.ksend, shares=ISOS.shares,
                                              list_files=ISOS.list_files, pick_class=ISOS.pick_class,
                                              share_node=ISOS.share_node, SHARE_NS=ISOS.SHARE_NS,
                                              DEFAULT_NS=ISOS.DEFAULT_NS, IMAGE=ISOS.IMAGE))
        ISOS.bind(K.kget, K.ksend, lambda: [], lambda *a: {"entries": []}, lambda: ("homestead-isos", True),
                  lambda: "", "lab", "lab", "alpine:3.20")

    def volume(self, name, unused_since=None, ready=True):
        annotations = {ISOS.FILE: f"{name}.iso", ISOS.SOURCE: f"media/{name}.iso", ISOS.SIZE: "100"}
        if ready:
            annotations[ISOS.READY] = "true"
        if unused_since is not None:
            annotations[ISOS.UNUSED] = str(unused_since)
        self.kube.add({"apiVersion": "v1", "kind": "PersistentVolumeClaim",
                       "metadata": {"name": name, "namespace": "lab", "labels": {ISOS.LABEL: "true"}, "annotations": annotations},
                       "spec": {}})
        if not ready:
            self.kube.add({"apiVersion": "batch/v1", "kind": "Job", "metadata": {"name": name, "namespace": "lab", "labels": {ISOS.LABEL: "true"}},
                           "status": {"active": 1}})

    def in_drive(self, vm, claim):
        self.kube.add({"apiVersion": "kubevirt.io/v1", "kind": "VirtualMachine", "metadata": {"name": vm, "namespace": "lab"},
                       "spec": {"template": {"spec": {"volumes": [
                           {"name": "cdrom-0", "persistentVolumeClaim": {"claimName": claim, "readOnly": True}}]}}}})

    def object(self, path):
        return self.kube.object(path)

    def library_data(self, **values):
        """The library's data as it is; values are set first, as kubectl would."""
        if values:
            obj = self.kube.object(LIBRARY)
            obj["data"].update(values)
            self.kube.add(obj)
        return self.kube.object(LIBRARY)["data"]


class VersionedCluster(Cluster):
    """Cluster that can let another request save the ISO library ConfigMap just
    after a read of it (the fake checks the resourceVersion on PUT, as Kubernetes
    does: a PUT naming no version is not checked)."""

    def __init__(self, test):
        super().__init__(test)
        self.after_read = None
        self.kube.react("GET", LIBRARY, self.read)

    def read(self, method, path, body):
        if not self.after_read:
            return None
        hook, self.after_read = self.after_read, None
        found = self.kube.object(path)
        hook()                  # another request saves between this read and its write
        return found


class SettingsTests(unittest.TestCase):
    def test_two_settings_saved_at_once_both_stay(self):
        c = VersionedCluster(self)
        c.after_read = lambda: ISOS.set_keep_days(14)
        with mock.patch.object(ISOS, "shares", lambda: [{"name": "media"}]):
            ISOS.set_folders([{"share": "media", "path": "isos"}])
        self.assertEqual(14, ISOS.keep_days())
        self.assertEqual([{"share": "media", "path": "isos"}], ISOS.folders())


    def test_a_hand_edited_value_is_fixed_by_saving_it_from_the_page(self):
        c = Cluster(self)
        c.library_data(folders="", keep_days="07")       # left by kubectl: not JSON
        with mock.patch.object(ISOS, "shares", lambda: [{"name": "media"}]):
            ISOS.set_folders([{"share": "media", "path": "isos"}])
        ISOS.set_keep_days(14)
        self.assertEqual(([{"share": "media", "path": "isos"}], 14), (ISOS.folders(), ISOS.keep_days()))


class TidyTests(unittest.TestCase):
    def test_a_copy_no_vm_uses_is_marked_then_removed_after_keep_days(self):
        c = Cluster(self)
        c.volume("iso-a")
        self.assertEqual([], ISOS.tidy(now=NOW))
        self.assertEqual(str(NOW), c.object(f"{PVCS}/iso-a")["metadata"]["annotations"][ISOS.UNUSED])
        self.assertEqual([], ISOS.tidy(now=NOW + 6 * DAY))
        self.assertEqual(["iso-a"], ISOS.tidy(now=NOW + 7 * DAY))
        self.assertIsNone(c.object(f"{PVCS}/iso-a"))

    def test_a_copy_in_a_drive_is_kept_and_its_mark_cleared(self):
        c = Cluster(self)
        c.volume("iso-a", unused_since=NOW - 30 * DAY)
        c.in_drive("win", "iso-a")
        self.assertEqual([], ISOS.tidy(now=NOW))
        self.assertNotIn(ISOS.UNUSED, c.object(f"{PVCS}/iso-a")["metadata"]["annotations"])

    def test_a_copy_being_made_is_left_alone(self):
        c = Cluster(self)
        c.volume("iso-a", unused_since=NOW - 30 * DAY, ready=False)
        self.assertEqual([], ISOS.tidy(now=NOW))

    def test_zero_keeps_them(self):
        c = Cluster(self, keep_days="0")
        c.volume("iso-a", unused_since=NOW - 300 * DAY)
        self.assertEqual([], ISOS.tidy(now=NOW))

    def test_keep_days_is_saved_beside_the_folders(self):
        c = Cluster(self)
        c.library_data(folders='[{"share": "media", "path": "isos"}]')
        self.assertEqual(14, ISOS.set_keep_days(14)["keep_days"])
        data = c.library_data()
        self.assertEqual(("14", '[{"share": "media", "path": "isos"}]'), (data["keep_days"], data["folders"]))
        for bad in (-1, 366, "soon"):
            with self.assertRaises(ValueError):
                ISOS.set_keep_days(bad)


LONGHORN = {"name": "longhorn-r2", "provisioner": "driver.longhorn.io", "migratable": False, "internal": False, "made_for": "",
            "default": True, "parameters": {"numberOfReplicas": "2", "dataLocality": "best-effort", "staleReplicaTimeout": "30",
                                            "recurringJobSelector": "[]"}}


class IsoClassTests(unittest.TestCase):
    def cluster(self):
        # The class is made through server.py's client, which is the shared fake's here.
        self.enterContext(mock.patch.object(OPS, "DATA_DIR", self.enterContext(tempfile.TemporaryDirectory())))
        return self.enterContext(Kube())

    def test_on_longhorn_a_single_replica_class_is_made_once_and_hidden_from_pickers(self):
        kube = self.cluster()
        with mock.patch.object(server, "storage_classes", return_value=[dict(LONGHORN)]), \
                mock.patch.object(server, "STORAGE_CLASS", "longhorn-r2"):
            self.assertEqual((server.ISO_CLASS, True), server.iso_storage_class())
        method, path, body, _ = kube.writes[0]
        self.assertEqual(("POST", "homestead-isos"), (method, body["metadata"]["name"]))
        self.assertEqual({"numberOfReplicas": "1", "dataLocality": "disabled", "staleReplicaTimeout": "30"}, body["parameters"])
        self.assertEqual("iso", server.STORAGE_ROUTES._class_made_for("homestead-isos", {}))
        made = dict(LONGHORN, name="homestead-isos", made_for="iso")
        kube.writes.clear()
        with mock.patch.object(server, "storage_classes", return_value=[dict(LONGHORN), made]), \
                mock.patch.object(server, "STORAGE_CLASS", "longhorn-r2"):
            self.assertEqual((server.ISO_CLASS, True), server.iso_storage_class())
        self.assertEqual([], kube.writes, "made once")

    def test_without_longhorn_a_shared_class_is_used_as_it_is(self):
        kube = self.cluster()
        nfs = dict(LONGHORN, name="nfs-client", provisioner="nfs.csi.k8s.io")
        with mock.patch.object(server, "storage_classes", return_value=[nfs]), mock.patch.object(server, "STORAGE_CLASS", "nfs-client"):
            self.assertEqual(("nfs-client", True), server.iso_storage_class())
        self.assertEqual([], kube.writes, "no class made")


if __name__ == "__main__":
    unittest.main()
