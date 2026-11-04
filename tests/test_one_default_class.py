"""Which storage class Homestead uses when none is named, at every place that
asks (#1125): a new claim, a VM disk, the class a move restores an image with,
and the object store's one-copy class. Over the cluster shapes Homestead meets,
on the shared fake API (tests/fakes/kube.py).

The answer is taken twice, before and after the storage classes are listed
(as the Volumes page does): a listing must not change it.
"""
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
import server
import homestead_kube as K
import homestead_move_phases as PHASE
import homestead_move_state as STATE
import homestead_objectstore as STORE
import homestead_storage_classes as STORAGE_CLASSES
from fakes.kube import Kube

LONGHORN, LOCAL = "driver.longhorn.io", "rancher.io/local-path"


def klass(name, provisioner=LONGHORN, default=False, **parameters):
    return {"apiVersion": "storage.k8s.io/v1", "kind": "StorageClass",
            "metadata": {"name": name, "annotations": (
                {"storageclass.kubernetes.io/is-default-class": "true"} if default else {})},
            "provisioner": provisioner, "parameters": parameters}


# name: (the install class, the cluster's classes)
SHAPES = {
    "only local-path, the default": ("longhorn-r2", [klass("local-path", LOCAL, default=True)]),
    "local-path and Longhorn both default": ("longhorn-r2", [klass("local-path", LOCAL, default=True),
                                                             klass("longhorn", default=True)]),
    "install class present, another the default": ("longhorn-r2", [klass("longhorn-r2"), klass("fast", default=True)]),
    "install class absent, local-path the default": ("longhorn-r2", [klass("local-path", LOCAL, default=True),
                                                                     klass("longhorn")]),
    "no install class (chart), both default": ("", [klass("local-path", LOCAL, default=True),
                                                    klass("longhorn", default=True)]),
    "Harvester": ("longhorn-r2", [klass("harvester-longhorn", default=True, migratable="true"),
                                 klass("longhorn-static"), klass("longhorn-image-x", backingImage="x")]),
    "every class unusable": ("longhorn-r2", [klass("longhorn-static"), klass("lh-1", backingImage="x"),
                                             klass("homestead-restore-1", fromBackup="s3://b/v")]),
    "an empty cluster": ("longhorn-r2", []),
}

# What each place answers: (new claim before a listing, new claim after it,
# VM disk, a move's image class, the object store's base class). One rule
# (homestead_storage_classes.default_class): the install class if it is here
# and usable, else the class the cluster marks as default (Longhorn's first),
# else the first usable of longhorn-r2, harvester-longhorn, longhorn. A move's
# image and the object store need a Longhorn class; "-" is none. A claim
# with no usable class at all still names the install class (it cannot name
# none); a VM disk then names none, and the API server picks.
EXPECTED = {
    "only local-path, the default": ("local-path", "local-path", "local-path", "-", "-"),
    "local-path and Longhorn both default": ("longhorn", "longhorn", "longhorn", "longhorn", "longhorn"),
    "install class present, another the default": ("longhorn-r2", "longhorn-r2", "longhorn-r2", "longhorn-r2", "longhorn-r2"),
    "install class absent, local-path the default": ("local-path", "local-path", "local-path", "longhorn", "-"),
    "no install class (chart), both default": ("longhorn", "longhorn", "longhorn", "longhorn", "longhorn"),
    "Harvester": ("harvester-longhorn", "harvester-longhorn", "harvester-longhorn", "harvester-longhorn", "harvester-longhorn"),
    "every class unusable": ("longhorn-r2", "longhorn-r2", "", "-", "-"),
    "an empty cluster": ("longhorn-r2", "longhorn-r2", "", "-", "-"),
}


class OneDefaultClassTests(unittest.TestCase):
    def setUp(self):
        for name in ("kget", "ksend"):
            self.enterContext(mock.patch.object(STORAGE_CLASSES, name, getattr(K, name)))
        self.enterContext(mock.patch.object(STATE, "kget", K.kget))
        self.enterContext(mock.patch.object(STORAGE_CLASSES, "INSTALL_CLASS", STORAGE_CLASSES.INSTALL_CLASS))

    def start(self, install):
        """Homestead as it starts with install as its STORAGE_CLASS."""
        STORAGE_CLASSES.INSTALL_CLASS = install

    def claim(self):
        return server.new_claims([{"type": "pvc", "create": True, "source": "data"}])[0]["storage_class"]

    def image_class(self):
        try:
            return PHASE._image_class()
        except ValueError:
            return "-"

    def store_base(self):
        sent = []
        with mock.patch.object(STORE, "ksend", lambda method, path, body=None, **k: sent.append(body)):
            made = STORE.single_copy_class()
        return sent[0]["metadata"]["annotations"]["homestead.io/base-storage-class"] if made and sent else "-"

    def answers(self, install, classes):
        with Kube(*classes):
            self.start(install)
            before = self.claim()
            server.storage_classes()                    # a listing: the Volumes page, say
            return (before, self.claim(), server.VM_ROUTES.vm_default_class(), self.image_class(), self.store_base())

    def test_every_place_over_every_cluster_shape(self):
        for shape, (install, classes) in SHAPES.items():
            with self.subTest(shape=shape):
                self.assertEqual(EXPECTED[shape], self.answers(install, classes))

    def test_listing_classes_changes_no_answer(self):
        """The cluster marks another class default than the install class."""
        with Kube(klass("longhorn-r2"), klass("fast", default=True), klass("local-path", LOCAL, default=True)):
            self.start("longhorn-r2")
            first = (self.claim(), server.VM_ROUTES.vm_default_class(), self.image_class(), self.store_base())
            server.storage_classes()
            server.selectable_storage_classes()
            self.assertEqual(first, (self.claim(), server.VM_ROUTES.vm_default_class(), self.image_class(), self.store_base()))
            self.assertEqual(("longhorn-r2",) * 4, first)


if __name__ == "__main__":
    unittest.main()
