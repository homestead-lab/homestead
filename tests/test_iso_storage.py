import sys
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
import homestead_isos as ISOS
import server

DAY = 86400
NOW = 1_800_000_000


class Cluster:
    """ISO volumes in lab, and the VMs that have them in a drive."""

    def __init__(self, keep_days="7"):
        self.objects, self.sent = {}, []
        self.objects["/api/v1/namespaces/lab/configmaps/homestead-iso-library"] = {
            "metadata": {"name": "homestead-iso-library"}, "data": {"folders": "[]", "keep_days": keep_days}}
        ISOS.bind(self.get, self.send, lambda: [], lambda *a: {"entries": []}, lambda: ("homestead-isos", True),
                  lambda: "", "lab", "lab", "alpine:3.20")

    def volume(self, name, unused_since=None, ready=True):
        annotations = {ISOS.FILE: f"{name}.iso", ISOS.SOURCE: f"media/{name}.iso", ISOS.SIZE: "100"}
        if ready:
            annotations[ISOS.READY] = "true"
        if unused_since is not None:
            annotations[ISOS.UNUSED] = str(unused_since)
        self.objects[f"/api/v1/namespaces/lab/persistentvolumeclaims/{name}"] = {
            "metadata": {"name": name, "labels": {ISOS.LABEL: "true"}, "annotations": annotations}, "spec": {}}
        if not ready:
            self.objects[f"/apis/batch/v1/namespaces/lab/jobs/{name}"] = {"metadata": {"name": name}, "status": {"active": 1}}

    def in_drive(self, vm, claim):
        self.objects[f"/apis/kubevirt.io/v1/namespaces/lab/virtualmachines/{vm}"] = {
            "metadata": {"name": vm}, "spec": {"template": {"spec": {"volumes": [
                {"name": "cdrom-0", "persistentVolumeClaim": {"claimName": claim, "readOnly": True}}]}}}}

    def get(self, path):
        base = path.split("?")[0]
        if base in self.objects:
            return self.objects[base]
        if base.endswith(("/persistentvolumeclaims", "/jobs", "/virtualmachines")):
            return {"items": [o for p, o in self.objects.items() if p.startswith(base + "/")]}
        raise urllib.error.HTTPError(path, 404, "missing", {}, None)

    def send(self, method, path, body=None, **kw):
        self.sent.append((method, path, body))
        if method == "DELETE":
            self.objects.pop(path.split("?")[0], None)
        elif method == "PATCH":
            meta = self.objects[path]["metadata"]
            for key, value in (body["metadata"].get("annotations") or {}).items():
                if value is None:
                    meta["annotations"].pop(key, None)
                else:
                    meta["annotations"][key] = value
        elif method in ("PUT", "POST") and body:
            self.objects[path if method == "PUT" else f"{path}/{body['metadata']['name']}"] = body
        return body


class TidyTests(unittest.TestCase):
    def test_a_copy_no_vm_uses_is_marked_then_removed_after_keep_days(self):
        c = Cluster()
        c.volume("iso-a")
        self.assertEqual([], ISOS.tidy(now=NOW))
        self.assertEqual(str(NOW), c.objects["/api/v1/namespaces/lab/persistentvolumeclaims/iso-a"]["metadata"]["annotations"][ISOS.UNUSED])
        self.assertEqual([], ISOS.tidy(now=NOW + 6 * DAY))
        self.assertEqual(["iso-a"], ISOS.tidy(now=NOW + 7 * DAY))
        self.assertNotIn("/api/v1/namespaces/lab/persistentvolumeclaims/iso-a", c.objects)

    def test_a_copy_in_a_drive_is_kept_and_its_mark_cleared(self):
        c = Cluster()
        c.volume("iso-a", unused_since=NOW - 30 * DAY)
        c.in_drive("win", "iso-a")
        self.assertEqual([], ISOS.tidy(now=NOW))
        self.assertNotIn(ISOS.UNUSED, c.objects["/api/v1/namespaces/lab/persistentvolumeclaims/iso-a"]["metadata"]["annotations"])

    def test_a_copy_being_made_is_left_alone(self):
        c = Cluster()
        c.volume("iso-a", unused_since=NOW - 30 * DAY, ready=False)
        self.assertEqual([], ISOS.tidy(now=NOW))

    def test_zero_keeps_them(self):
        c = Cluster(keep_days="0")
        c.volume("iso-a", unused_since=NOW - 300 * DAY)
        self.assertEqual([], ISOS.tidy(now=NOW))

    def test_keep_days_is_saved_beside_the_folders(self):
        c = Cluster()
        c.objects["/api/v1/namespaces/lab/configmaps/homestead-iso-library"]["data"]["folders"] = '[{"share": "media", "path": "isos"}]'
        self.assertEqual(14, ISOS.set_keep_days(14)["keep_days"])
        data = c.objects["/api/v1/namespaces/lab/configmaps/homestead-iso-library"]["data"]
        self.assertEqual(("14", '[{"share": "media", "path": "isos"}]'), (data["keep_days"], data["folders"]))
        for bad in (-1, 366, "soon"):
            with self.assertRaises(ValueError):
                ISOS.set_keep_days(bad)


LONGHORN = {"name": "longhorn-r2", "provisioner": "driver.longhorn.io", "migratable": False, "internal": False, "made_for": "",
            "default": True, "parameters": {"numberOfReplicas": "2", "dataLocality": "best-effort", "staleReplicaTimeout": "30",
                                            "recurringJobSelector": "[]"}}


class IsoClassTests(unittest.TestCase):
    def test_on_longhorn_a_single_replica_class_is_made_once_and_hidden_from_pickers(self):
        sent = []
        with mock.patch.object(server, "storage_classes", return_value=[dict(LONGHORN)]), \
                mock.patch.object(server, "STORAGE_CLASS", "longhorn-r2"), \
                mock.patch.object(server, "ksend", lambda method, path, body=None, **kw: sent.append((method, path, body))):
            self.assertEqual((server.ISO_CLASS, True), server.iso_storage_class())
        method, path, body = sent[0]
        self.assertEqual(("POST", "homestead-isos"), (method, body["metadata"]["name"]))
        self.assertEqual({"numberOfReplicas": "1", "dataLocality": "disabled", "staleReplicaTimeout": "30"}, body["parameters"])
        self.assertEqual("iso", server._class_made_for("homestead-isos", {}))
        made = dict(LONGHORN, name="homestead-isos", made_for="iso")
        sent.clear()
        with mock.patch.object(server, "storage_classes", return_value=[dict(LONGHORN), made]), \
                mock.patch.object(server, "STORAGE_CLASS", "longhorn-r2"), \
                mock.patch.object(server, "ksend", lambda *a, **k: sent.append(a)):
            self.assertEqual((server.ISO_CLASS, True), server.iso_storage_class())
        self.assertEqual([], sent, "made once")

    def test_without_longhorn_a_shared_class_is_used_as_it_is(self):
        nfs = dict(LONGHORN, name="nfs-client", provisioner="nfs.csi.k8s.io")
        with mock.patch.object(server, "storage_classes", return_value=[nfs]), mock.patch.object(server, "STORAGE_CLASS", "nfs-client"), \
                mock.patch.object(server, "ksend", lambda *a, **k: (_ for _ in ()).throw(AssertionError("no class made"))):
            self.assertEqual(("nfs-client", True), server.iso_storage_class())


if __name__ == "__main__":
    unittest.main()
