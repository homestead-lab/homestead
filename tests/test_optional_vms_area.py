"""Optional reads and deletes in the remaining VM-area modules (#619 step 3):
each reads through the reader its bind() gave it, a missing object (404) is
None (or "already gone" for a delete), and any other failure raises. The
404 handling lives in homestead_kube, not in a copy per module."""
import ast
import io
import os
import sys
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

SERVER = Path(os.environ.get("HOMESTEAD_TEST_SERVER_DIR", str(Path(__file__).resolve().parents[1] / "server")))
sys.path.insert(0, str(SERVER))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import homestead_kube as KUBE
import homestead_imports as IMPORTS
import homestead_container_import as CONTAINER_IMPORT
import homestead_vm_capacity as VM_CAPACITY
import homestead_vm_create as VM_CREATE
import homestead_vms as VMS
import test_vms as VM_FIXTURES

# Functions that may still compare a status with 404 themselves, and why.
OWN_404 = {
    # Turns any failure into its own refusal ("could not be checked, so
    # nothing was deleted"); 404 alone is "not there".
    "homestead_vms": {"_owners"},
    # Lists only the pods it deleted itself. delete_if_present tells "deleted"
    # from "already gone" only by its return value, and a sender that answers
    # None would hide a deleted pod, so this stays explicit.
    "homestead_container_import": {"_stop_job_pods"},
    "homestead_vm_create": set(),
    "homestead_vm_capacity": set(),
    "homestead_passthrough": set(),
}


def functions_comparing_404(source):
    """Where the source compares anything with 404, also inside a tuple
    (code in (404, 410)): the names of the functions it is in, a nested
    function counting for the one around it as well, or "<module>" for a
    check outside any function."""
    found = set()

    def visit(node, inside):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            inside = inside + (node.name,)
        if isinstance(node, ast.Compare) and any(
                isinstance(part, ast.Constant) and part.value == 404 for part in ast.walk(node)):
            found.update(inside or ("<module>",))
        for child in ast.iter_child_nodes(node):
            visit(child, inside)
    visit(ast.parse(source), ())
    return found


def http_error(code):
    return urllib.error.HTTPError("/x", code, "error", {}, io.BytesIO(b""))


class Paths:
    """A bound reader or sender: answers by path, the default for any other path."""
    def __init__(self, default, **by_part):
        self.default, self.by_part, self.calls = default, by_part, []

    def __call__(self, *args, **_):
        path = args[1] if len(args) > 1 and args[0] in ("GET", "POST", "PUT", "PATCH", "DELETE") else args[0]
        self.calls.append(args)
        answer = next((value for part, value in self.by_part.items() if part in path), self.default)
        if isinstance(answer, Exception):
            raise answer
        return answer


def never(*args, **_):
    raise AssertionError(f"went through the shared client, not the module's own: {args}")


class NoOwn404CopiesTests(unittest.TestCase):
    def test_only_the_listed_functions_compare_a_status_with_404(self):
        for module, allowed in OWN_404.items():
            with self.subTest(module=module):
                self.assertEqual(allowed, functions_comparing_404((SERVER / f"{module}.py").read_text()))

    def test_the_check_sees_tuples_nested_functions_and_module_level_checks(self):
        self.assertEqual({"outer", "inner"}, functions_comparing_404(
            "def outer():\n    def inner(error):\n        return error.code in (404, 410)\n"))
        self.assertEqual({"<module>"}, functions_comparing_404("GONE = code == 404\n"))
        self.assertEqual(set(), functions_comparing_404("def f(error):\n    return error.code == 409\n"))


class VmAreaReadTests(unittest.TestCase):
    def setUp(self):
        for name in ("kget", "ksend"):
            patch = mock.patch.object(KUBE, name, never)
            patch.start()
            self.addCleanup(patch.stop)

    def bound(self, module, **values):
        for name, value in values.items():
            patch = mock.patch.object(module, name, value)
            patch.start()
            self.addCleanup(patch.stop)

    def test_vm_create_reads_an_optional_object_through_the_imports_reader(self):
        read = Paths(http_error(404), present={"metadata": {"name": "present"}})
        self.bound(IMPORTS, kget=read)
        self.assertIsNone(VM_CREATE._get_or_none("/api/v1/namespaces/lab/secrets/missing"))
        self.assertEqual("present", VM_CREATE._get_or_none("/api/v1/namespaces/lab/secrets/present")["metadata"]["name"])
        self.assertEqual(2, len(read.calls))
        self.bound(IMPORTS, kget=Paths(http_error(500)))
        with self.assertRaises(urllib.error.HTTPError):
            VM_CREATE._get_or_none("/api/v1/namespaces/lab/secrets/missing")

    def test_vm_capacity_optional_dependency(self):
        read = Paths(http_error(404), named={"metadata": {"name": "disk"}}, nameless={"metadata": {}})
        self.assertIsNone(VM_CAPACITY._optional(read, "/missing"))
        self.assertEqual("disk", VM_CAPACITY._optional(read, "/named")["metadata"]["name"])
        with self.assertRaisesRegex(ValueError, "incomplete VM dependency response"):
            VM_CAPACITY._optional(read, "/nameless")
        with self.assertRaises(urllib.error.HTTPError):
            VM_CAPACITY._optional(Paths(http_error(500)), "/broken")

    def test_import_inventory_takes_404_as_free_and_anything_else_as_an_error(self):
        prepared = {"namespace": "lab", "deployment": {"metadata": {"name": "app"}}, "service": None, "job": None,
                    "volumes": [{"name": "app-data", "create": True}]}
        pods = {"items": [], "metadata": {}}
        inventory, rows = CONTAINER_IMPORT.import_inventory(prepared, Paths(http_error(404), **{"/pods": pods}))
        self.assertEqual(({"claims": {"app-data": None}}, []), (inventory, rows))
        with self.assertRaisesRegex(ValueError, "deployment app already exists"):
            CONTAINER_IMPORT.import_inventory(prepared, Paths(http_error(404), deployments={"metadata": {"uid": "u1"}}, **{"/pods": pods}))
        with self.assertRaisesRegex(ValueError, "Import inventory is incomplete"):
            CONTAINER_IMPORT.import_inventory(prepared, Paths(http_error(404), deployments={"metadata": {}}, **{"/pods": pods}))
        with self.assertRaisesRegex(ValueError, "Import inventory is incomplete"):
            CONTAINER_IMPORT.import_inventory(prepared, Paths(http_error(404), deployments=None, **{"/pods": pods}))
        with self.assertRaises(urllib.error.HTTPError):
            CONTAINER_IMPORT.import_inventory(prepared, Paths(http_error(500), **{"/pods": pods}))

    def test_chown_claim_refuses_a_job_it_cannot_read(self):
        self.bound(IMPORTS, kget=Paths(http_error(500)), ksend=never)
        with self.assertRaises(urllib.error.HTTPError):
            CONTAINER_IMPORT.chown_claim("lab", "app-data", 1000, 1000)

    def test_delete_import_passes_over_what_is_already_gone(self):
        job = {"metadata": {"name": "homestead-import-app", "uid": "j1",
                            "ownerReferences": [{"kind": "Deployment", "name": "app", "uid": "d1"}]},
               "status": {"conditions": [{"type": "Complete", "status": "True"}]}}
        reads = Paths({"items": []}, **{"/jobs/": job, "/deployments/app": http_error(404), "/daemonsets/": http_error(404)})
        sends = Paths({}, **{"/jobs/": http_error(404), "/secrets/": http_error(404)})
        self.bound(IMPORTS, kget=reads, ksend=sends)
        self.bound(CONTAINER_IMPORT.IMPORT_JOB, journalled=lambda *a: False)
        self.bound(IMPORTS, _bust=lambda *a: None)
        result = CONTAINER_IMPORT.delete_import("homestead-import-app", ns="apps")
        self.assertTrue(result["ok"])
        self.assertEqual(["DELETE", "DELETE"], [call[0] for call in sends.calls])
        self.assertFalse(any(call[0] == "PUT" for call in sends.calls), "a Deployment that is gone is not written")
        # A failure other than 404 stops it.
        self.bound(IMPORTS, ksend=Paths({}, **{"/jobs/": http_error(500)}))
        with self.assertRaises(urllib.error.HTTPError):
            CONTAINER_IMPORT.delete_import("homestead-import-app", ns="apps")
        self.bound(IMPORTS, ksend=Paths({}, **{"/secrets/": http_error(403)}))
        with self.assertRaises(urllib.error.HTTPError):
            CONTAINER_IMPORT.delete_import("homestead-import-app", ns="apps")
        # A missing Job is read as no Job; any other read failure raises.
        self.bound(IMPORTS, ksend=Paths({}), kget=Paths({"items": []}, **{"/jobs/": http_error(404), "/daemonsets/": http_error(404)}))
        self.assertTrue(CONTAINER_IMPORT.delete_import("homestead-import-app", ns="apps")["ok"])
        self.bound(IMPORTS, kget=Paths({"items": []}, **{"/jobs/": http_error(500)}))
        with self.assertRaises(urllib.error.HTTPError):
            CONTAINER_IMPORT.delete_import("homestead-import-app", ns="apps")


class VmsReadTests(unittest.TestCase):
    """homestead_vms reads the VM's instance through its own bound reader: a
    404 is no instance, any other failure is not proof of one being absent."""
    VMI = "/virtualmachineinstances/win11"

    def setUp(self):
        for name in ("kget", "ksend"):
            patch = mock.patch.object(KUBE, name, never)
            patch.start()
            self.addCleanup(patch.stop)
        for name in ("kget", "ksend", "events_for"):
            patch = mock.patch.object(VMS, name, getattr(VMS, name))
            patch.start()
            self.addCleanup(patch.stop)

    def crashed(self, instance, strategy="RerunOnFailure"):
        """A VM in a boot crash loop; instance is the answer for its VMI.
        Writes go to the shared fake (tests/fakes/kube.py), which keeps the record."""
        cluster = VM_FIXTURES.Cluster(self)
        cluster.change(lambda vm: (vm["spec"].update(runStrategy=strategy), vm.update(status={
            "printableStatus": "CrashLoopBackOff", "startFailure": {"consecutiveFailCount": 3},
            "conditions": [{"type": "Ready", "status": "False", "reason": "", "message": "not running"}]})))
        self.reads = []

        def read(path):
            if path.endswith(self.VMI):
                self.reads.append(path)
                if isinstance(instance, Exception):
                    raise instance
                return instance
            return cluster.get(path)
        VMS.bind(read, cluster.send, lambda *a: [])
        return cluster

    def test_detail_takes_only_404_as_no_instance(self):
        self.crashed(http_error(404), "Halted")
        self.assertEqual("Stopped", VMS.detail("default", "win11")["status"])
        self.assertEqual(1, len(self.reads), "the instance is read through the module's own reader")
        for code in (403, 500):
            with self.subTest(code=code):
                self.crashed(http_error(code), "Halted")
                self.assertEqual("CrashLoopBackOff", VMS.detail("default", "win11")["status"])
                self.assertEqual(1, len(self.reads))

    def test_halting_crash_retries_needs_a_404_not_any_failure(self):
        cluster = self.crashed(http_error(404))
        self.assertTrue(VMS._halt_crash_retries("default", "win11")["ok"])
        self.assertEqual(1, len(self.reads), "the instance is read through the module's own reader")
        self.assertEqual(["PATCH"], [sent[0] for sent in cluster.sent])
        for code in (403, 500):
            with self.subTest(code=code):
                cluster = self.crashed(http_error(code))
                with self.assertRaises(urllib.error.HTTPError) as caught:
                    VMS._halt_crash_retries("default", "win11")
                self.assertEqual(code, caught.exception.code)
                self.assertEqual([], cluster.sent)


if __name__ == "__main__":
    unittest.main()
