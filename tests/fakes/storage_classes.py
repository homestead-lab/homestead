"""The cluster's storage classes, for tests whose code asks which class a new
volume goes on (homestead_storage_classes.default_class, #1125) but whose fake
cluster serves no StorageClass objects.

That answer used to be a copy bound into each module, so these tests never
needed the classes; it is read from the cluster now. A test states the classes
it assumes:

    def setUp(self):
        storage_classes(self, "longhorn-r2")

Each name is a usable Longhorn class; pass a row (as server.storage_classes()
returns) for anything else. The install class is set to the first one.
"""
from unittest import mock

import homestead_storage_classes as STORAGE_CLASSES


def storage_classes(test, *classes, install=None):
    rows = [{"name": c, "provisioner": STORAGE_CLASSES.LONGHORN_PROVISIONER, "default": False,
             "internal": False, "made_for": ""} if isinstance(c, str) else dict(c) for c in classes]
    test.enterContext(mock.patch.object(STORAGE_CLASSES, "storage_classes", lambda: [dict(r) for r in rows]))
    first = rows[0]["name"] if rows else ""
    test.enterContext(mock.patch.object(STORAGE_CLASSES, "INSTALL_CLASS", first if install is None else install))
    return rows
