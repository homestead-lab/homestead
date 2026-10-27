"""homestead_imports split by feature (#623): importing a container lives in
homestead_container_import, the image cache in homestead_image_cache, schedules
in homestead_scheduled_jobs and VM disks, images and creation in
homestead_vm_create. homestead_imports keeps the sources, discovery, the bound
state, the routes and a re-export of every name, so other modules, server.py and
tests keep working."""
import ast
import subprocess
import sys
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SERVER = ROOT / "server"
sys.path.insert(0, str(SERVER))

import source_scan as SS
import homestead_container_import as CONTAINER_IMPORT
import homestead_image_cache as IMAGE_CACHE
import homestead_imports as IMPORTS
import homestead_operations as OPS
import homestead_scheduled_jobs as SCHEDULED_JOBS
import homestead_vm_create as VM_CREATE

PARTS = (CONTAINER_IMPORT, IMAGE_CACHE, SCHEDULED_JOBS, VM_CREATE)

# Names server.py binds on homestead_imports or tests replace there that the
# moved code uses: it must read each one through homestead_imports (I.name).
REPLACED = {"NS", "SCAN_DIR", "_SCAN_STARTED", "_bust", "_save_scans", "kget", "ksend", "_vm_mac",
            "commit_vm", "prepare_vm", "iso_ready", "_daemonset_exists", "_pod_logs", "_source",
            "_stop_job_pods", "build_deployment", "create_pvc", "run_probe", "source_secret",
            "hardware_features", "_cache", "wait_for_pods_gone", "import_cleanup_plan", "delete_import",
            "_sources_map", "SOURCES"}


class ImportsModuleTests(unittest.TestCase):
    def test_every_moved_name_is_the_same_object_on_homestead_imports(self):
        for part in PARTS:
            moved = [name for name in vars(part) if not name.startswith("__")
                     and getattr(vars(part)[name], "__module__", part.__name__) == part.__name__
                     and name != "I" and not isinstance(vars(part)[name], type(sys))]
            self.assertTrue(moved, part.__name__)
            for name in moved:
                self.assertIs(getattr(IMPORTS, name), getattr(part, name), f"{part.__name__}.{name}")

    def test_each_module_can_be_imported_first(self):
        for name in [part.__name__ for part in PARTS] + ["homestead_imports"]:
            done = subprocess.run([sys.executable, "-c", f"import {name}, homestead_imports as I; "
                                   "I.import_container; I.image_cache; I.list_jobs; I.create_vm; I.ROUTES"],
                                  cwd=SERVER, capture_output=True, text=True)
            self.assertEqual(0, done.returncode, f"importing {name} first: {done.stderr}")

    def test_the_routes_stay_on_homestead_imports(self):
        self.assertIn(("POST", "/api/import"), IMPORTS.ROUTES)
        for part in PARTS:
            self.assertFalse(hasattr(part, "ROUTES"), part.__name__)

    def test_the_moved_code_reads_every_replaced_name_through_homestead_imports(self):
        for part in PARTS:
            tree = SS.tree(Path(part.__file__))
            bare = sorted({node.id for fn in ast.walk(tree) if isinstance(fn, (ast.FunctionDef, ast.Lambda))
                           for node in ast.walk(fn) if isinstance(node, ast.Name)
                           and isinstance(node.ctx, ast.Load) and node.id in REPLACED})
            self.assertEqual([], bare, f"{part.__name__} reads these directly, so replacing them on homestead_imports misses it")

    def test_create_vm_uses_the_prepare_and_commit_set_on_homestead_imports(self):
        with mock.patch.object(IMPORTS, "prepare_vm", return_value="prepared") as prepare, \
                mock.patch.object(IMPORTS, "commit_vm", return_value={"ok": True}) as commit:
            self.assertEqual({"ok": True}, IMPORTS.create_vm({"name": "vm"}, {"kubevirt": True}, "fast"))
        prepare.assert_called_once_with({"name": "vm"}, {"kubevirt": True}, "fast")
        commit.assert_called_once_with("prepared")

    def test_an_image_scan_records_its_start_in_the_state_on_homestead_imports(self):
        started = [0.0]
        with mock.patch.object(IMPORTS, "kget", return_value={"items": []}), \
                mock.patch.object(IMPORTS, "ksend"), mock.patch.object(IMPORTS, "_bust"), \
                mock.patch.object(IMPORTS, "_SCAN_STARTED", started), \
                mock.patch.object(IMPORTS, "_save_scans") as save:
            IMPORTS.start_image_scan()
        self.assertGreater(started[0], 0)
        save.assert_called_once()

    def test_deleting_an_import_uses_the_job_helpers_set_on_homestead_imports(self):
        def missing(path, *args, **kwargs):
            raise urllib.error.HTTPError(path, 404, "not found", {}, None)

        data = self.enterContext(tempfile.TemporaryDirectory())
        with mock.patch.object(OPS, "DATA_DIR", data), \
                mock.patch.object(IMPORTS, "kget", side_effect=missing), \
                mock.patch.object(IMPORTS, "ksend", side_effect=missing), \
                mock.patch.object(IMPORTS, "_bust"), \
                mock.patch.object(IMPORTS, "_stop_job_pods", return_value=["homestead-import-app-x"]) as stop, \
                mock.patch.object(IMPORTS, "_daemonset_exists", return_value=False) as exists:
            result = IMPORTS.delete_import("homestead-import-app")
        stop.assert_called_once()
        exists.assert_called()
        self.assertEqual(["homestead-import-app-x"], result["pods_removed"])


if __name__ == "__main__":
    unittest.main()
