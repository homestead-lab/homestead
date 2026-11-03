"""The server's three disk guards, for tests that change disks (homestead_disks).

A disk change is refused while any of them is missing, so a test that drives
one installs them, as server.py does, over an empty job store:

    def setUp(self):
        disk_guards(self)

The store is homestead_operations itself, with its DATA_DIR (where the job
store's lock lives) in a temporary folder, so the guards take the real lock.
The registry is put back as it was when the test ends.
"""
import tempfile
from unittest import mock

import homestead_disk_allocation as DISK_ALLOCATION
import homestead_disk_v2 as DISK_V2
import homestead_disks as DISKS
import homestead_longhorn_runtime as LH_RUNTIME
import homestead_operations as OPS


def disk_guards(test, ops=OPS):
    if ops is OPS:
        test.enterContext(mock.patch.object(OPS, "DATA_DIR", test.enterContext(tempfile.TemporaryDirectory())))
    test.enterContext(mock.patch.dict(DISKS.GUARDS))
    test.enterContext(mock.patch.object(DISKS, "v2_tasks", DISKS.v2_tasks))
    for module in (DISK_V2, DISK_ALLOCATION, LH_RUNTIME):
        test.enterContext(mock.patch.object(module, "ops", ops))
        module.protect_mutations(DISKS)
