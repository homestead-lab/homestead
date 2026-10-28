"""homestead_vms split by feature (#623): listing VMs lives in homestead_vm_list
and editing one in homestead_vm_edit, and homestead_vms keeps the bound state,
status, detail, power, delete and the routes. Other modules, server.py and
tests read and replace each name where it is defined (test_split_reexports)."""
import copy
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SERVER = ROOT / "server"
sys.path.insert(0, str(SERVER))

import homestead_vms as VMS
import homestead_vm_edit as VM_EDIT
import homestead_vm_list as VM_LIST
sys.path.insert(0, str(ROOT / "tests"))
from test_vms import VM

PARTS = (VM_LIST, VM_EDIT)


class VmModuleTests(unittest.TestCase):
    def test_each_module_can_be_imported_first(self):
        for name in ("homestead_vm_list", "homestead_vm_edit", "homestead_vms"):
            done = subprocess.run([sys.executable, "-c", f"import {name}, homestead_vms as V, homestead_vm_list as VL, "
                                   "homestead_vm_edit as VE; VL.list_vms; VE.edit; V.ROUTES"],
                                  cwd=SERVER, capture_output=True, text=True)
            self.assertEqual(0, done.returncode, f"importing {name} first: {done.stderr}")

    def test_the_routes_are_unchanged(self):
        self.assertEqual({("POST", "/api/vm/delete"), ("POST", "/api/vm/migrate"), ("POST", "/api/vms/logo"),
                          ("POST", "/api/vms/monitoring"), ("GET", "/api/vms"), ("GET", "/api/vm")}, set(VMS.ROUTES))
        for part in PARTS:
            self.assertFalse(hasattr(part, "ROUTES"), part.__name__)

    def test_a_client_set_on_homestead_vms_reaches_the_moved_code(self):
        # server.py binds kget on homestead_vms, and tests replace it there.
        asked = []
        with mock.patch.object(VMS, "kget", side_effect=lambda path: asked.append(path) or {"items": []}):
            VM_LIST._datavolumes("lab")
            VM_EDIT._secret_values("lab", "vm-cloud-init")
        self.assertEqual(["/apis/cdi.kubevirt.io/v1beta1/namespaces/lab/datavolumes",
                          "/api/v1/namespaces/lab/secrets/vm-cloud-init"], asked)

    def test_what_server_py_binds_reaches_the_moved_code(self):
        # server.py calls homestead_vms.bind() after import; the moved code must use
        # what it binds, not what was there when the modules were loaded.
        class Stop(Exception):
            pass

        sent = []

        def ksend(method, path, body, ctype=None):
            sent.append((method, path))
            raise Stop

        prepared = {"namespace": "lab", "name": "vm", "vm": {}, "to_create": [], "resize": [], "dropped": [],
                    "changed_hardware": False,
                    "effects": [{"kind": "secret", "path": "/api/v1/namespaces/lab/secrets/vm-ci", "identity": {}, "data": {}}]}
        with mock.patch.object(VMS, "kget", VMS.kget), mock.patch.object(VMS, "ksend", VMS.ksend), \
                mock.patch.object(VMS, "events_for", VMS.events_for), \
                mock.patch.object(VMS, "display_icon", VMS.display_icon), \
                mock.patch.object(VM_EDIT, "_recheck_edit"):
            VMS.bind(lambda path: {"items": []}, ksend, lambda *a: [], _display_icon=lambda annotations: "bound icon")
            self.assertEqual("bound icon", VM_LIST._row(copy.deepcopy(VM), {})["icon"])
            with self.assertRaises(Stop):
                VM_EDIT.commit_edit(prepared)
        self.assertEqual([("PATCH", "/api/v1/namespaces/lab/secrets/vm-ci")], sent)

    def test_a_name_replaced_where_it_is_defined_reaches_the_edit(self):
        # Tests replace commit_edit on homestead_vm_edit; editing reads it there.
        with mock.patch.object(VM_EDIT, "prepare_edit", return_value="prepared"), \
                mock.patch.object(VM_EDIT, "commit_edit", return_value="saved") as commit:
            self.assertEqual("saved", VM_EDIT.edit("lab", "vm", {}))
        commit.assert_called_once_with("prepared")


if __name__ == "__main__":
    unittest.main()
