"""The last local "read it, or nothing on 404" helpers (#619 steps 3 and 4):
shares, disk preparation, the Longhorn V2 upgrade, priority tiers, the
Longhorn rebalance, power, volume deletion and the network picture read an
optional object or list through homestead_kube with the reader their bind()
gave them. Each keeps what it did for every answer: only a 404 is "not
there", a null answer is not a 404, and anything else fails as before.

Left out on purpose: helpers that take every failure as nothing there
(homestead_components, _node_parity, _passthrough and _platform's _get or
_items, and homestead_vips' _items unless strict). No homestead_kube read
does that, and making them strict changes what they do."""
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
import source_scan as SS
import homestead_disk_v2 as DISK_V2
import homestead_kube as KUBE
import homestead_lhv2_upgrade as LHV2_UPGRADE
import homestead_network_state as NET_STATE
import homestead_power as POWER
import homestead_priority as PRIORITY
import homestead_rebalance as REBALANCE
import homestead_shares as SHARES
import homestead_volumes as VOLUMES

# Functions in these modules that may still compare a status with 404
# themselves, and why.
OWN_404 = {
    "homestead_disk_v2": set(),
    "homestead_lhv2_upgrade": set(),
    # A 404 on the update of a ConfigMap read a moment ago: deleted meanwhile,
    # so the save is refused as changed, not created again. _not_found: a
    # borrowed claim that is not there, refused in the share's words.
    "homestead_shares": {"_not_found", "_save"},
    "homestead_network_state": set(),
    # A power helper pod that is gone has finished: the host is watched on.
    "homestead_power": {"_status"},
    # Creating a missing class (403 is its own refusal); "is gone" in the
    # tier change's words; a skipped app among 403, 404, 409 and 422.
    "homestead_priority": {"_ensure_by_name", "check", "status"},
    # A volume that went away while a rebalance waited is skipped.
    "homestead_rebalance": {"status"},
    # The bound volume or Longhorn volume already gone, in deletion's words.
    "homestead_volumes": {"deletion_plan"},
}


def functions_comparing_404(source):
    """The functions that compare anything with 404 (also in a tuple)."""
    found = set()

    def visit(node, inside):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            inside = inside + (node.name,)
        if isinstance(node, ast.Compare) and any(
                isinstance(part, ast.Constant) and part.value == 404 for part in ast.walk(node)):
            found.update(inside or ("<module>",))
        for child in ast.iter_child_nodes(node):
            visit(child, inside)
    visit(SS.tree(SS.code(source)), ())
    return found


def http_error(code, headers=None):
    return urllib.error.HTTPError("/x", code, "error", headers or {}, io.BytesIO(b""))


class Answers:
    """A bound reader that gives these answers in turn (the last one repeats)."""
    def __init__(self, *answers):
        self.answers, self.paths = list(answers), []

    def __call__(self, path, **_):
        self.paths.append(path)
        answer = self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]
        if isinstance(answer, Exception):
            raise answer
        return answer


def never(path, **_):
    raise AssertionError(f"read through the shared client, not the module's own: {path}")


class NoOwn404CopiesTests(unittest.TestCase):
    def test_only_the_listed_functions_compare_a_status_with_404(self):
        for module, allowed in OWN_404.items():
            with self.subTest(module=module):
                self.assertEqual(allowed, functions_comparing_404(SS.text(SERVER / f"{module}.py")))

    def test_each_helper_reads_through_homestead_kube_with_its_own_reader(self):
        helpers = {
            SHARES: lambda: SHARES._longhorn_volume({"spec": {"volumeName": "pvc-1"}}),
            DISK_V2: lambda: DISK_V2._get("/x", True),
            LHV2_UPGRADE: lambda: LHV2_UPGRADE._get("/x", True),
            PRIORITY: lambda: PRIORITY._items("/x"),
            REBALANCE: lambda: REBALANCE._items("/x"),
            POWER: lambda: POWER._items("/x", absent_ok=True),
            VOLUMES: lambda: VOLUMES._items("/x", "virtual machine", [], optional=True),
            NET_STATE: lambda: NET_STATE._items("/x"),
        }
        for module, helper in helpers.items():
            read = Answers({"items": []})
            with self.subTest(module=module.__name__), mock.patch.object(module, "kget", read), \
                    mock.patch.object(KUBE, "kget", never), \
                    mock.patch.object(KUBE, "get_optional", wraps=KUBE.get_optional) as shared:
                helper()
                shared.assert_called_once()
                self.assertEqual(1, len(read.paths))


class LastHelpersTests(unittest.TestCase):
    def setUp(self):
        patch = mock.patch.object(KUBE, "kget", never)
        patch.start()
        self.addCleanup(patch.stop)

    def reading(self, module, *answers):
        reader = Answers(*answers)
        patch = mock.patch.object(module, "kget", reader)
        patch.start()
        self.addCleanup(patch.stop)
        return reader

    def test_shares_take_only_a_404_as_no_longhorn_volume(self):
        pvc = {"spec": {"volumeName": "pvc-1"}}
        read = self.reading(SHARES, {"metadata": {"name": "pvc-1"}})
        self.assertEqual({"metadata": {"name": "pvc-1"}}, SHARES._longhorn_volume(pvc))
        self.assertIn("/volumes/pvc-1", read.paths[0])
        self.reading(SHARES, http_error(404))
        self.assertIsNone(SHARES._longhorn_volume(pvc))
        self.reading(SHARES, None)
        self.assertIsNone(SHARES._longhorn_volume(pvc))
        for failure in (http_error(403), http_error(500), OSError("timed out")):
            self.reading(SHARES, failure)
            with self.subTest(failure=failure), self.assertRaises(type(failure)):
                SHARES._longhorn_volume(pvc)

    def test_shares_find_the_samba_host_past_a_missing_pod_list(self):
        self.reading(SHARES, http_error(404), {"items": [{"spec": {"nodeName": "node-2"}}]})
        self.assertEqual("node-2", SHARES._samba_node())
        self.reading(SHARES, http_error(500))
        with self.assertRaises(urllib.error.HTTPError):
            SHARES._samba_node()

    def check_inventory_get(self, module, unavailable, incomplete):
        self.reading(module, {"value": "1"})
        self.assertEqual({"value": "1"}, module._get("/x"))
        self.reading(module, http_error(404))
        self.assertIsNone(module._get("/x", True))
        with self.assertRaisesRegex(ValueError, unavailable.format(code=404)):
            module._get("/x")
        self.reading(module, http_error(500))
        with self.assertRaisesRegex(ValueError, unavailable.format(code=500)) as caught:
            module._get("/x", True)
        self.assertEqual(500, caught.exception.__cause__.code)
        # A null answer is not a 404: it is an incomplete inventory, optional or not.
        for optional in (False, True):
            self.reading(module, None)
            with self.subTest(optional=optional), self.assertRaisesRegex(ValueError, incomplete):
                module._get("/x", optional)
        self.reading(module, OSError("timed out"))
        with self.assertRaises(OSError):
            module._get("/x", True)

    def test_disk_preparation_takes_only_a_404_of_an_optional_object_as_missing(self):
        self.check_inventory_get(DISK_V2, r"Disk preparation inventory is unavailable \(HTTP {code}\)",
                                 "^Disk preparation inventory is incomplete$")

    def test_the_v2_upgrade_takes_only_a_404_of_an_optional_object_as_missing(self):
        self.check_inventory_get(LHV2_UPGRADE, r"Longhorn upgrade inventory is unavailable \(HTTP {code}\)",
                                 "Longhorn upgrade inventory is incomplete")
        self.reading(LHV2_UPGRADE, http_error(404))
        self.assertEqual([], LHV2_UPGRADE._items("/x", True))
        with self.assertRaisesRegex(ValueError, r"\(HTTP 404\)"):
            LHV2_UPGRADE._items("/x")

    def check_list_404_is_empty(self, module, *args):
        self.reading(module, {"items": [{"a": 1}]})
        self.assertEqual([{"a": 1}], module._items("/x", *args))
        self.reading(module, {})
        self.assertEqual([], module._items("/x", *args))
        self.reading(module, http_error(404))
        self.assertEqual([], module._items("/x", *args))
        for failure in (http_error(403), http_error(500), OSError("timed out")):
            self.reading(module, failure)
            with self.subTest(failure=failure), self.assertRaises(type(failure)):
                module._items("/x", *args)
        # A null answer is not a 404: it fails, so nothing acts on it as an empty list.
        self.reading(module, None)
        with self.assertRaises(AttributeError):
            module._items("/x", *args)

    def test_priority_tiers_take_only_a_404_as_an_empty_list(self):
        self.check_list_404_is_empty(PRIORITY)

    def test_the_rebalance_takes_only_a_404_as_an_empty_list(self):
        self.check_list_404_is_empty(REBALANCE)

    def test_power_takes_a_404_as_absent_only_where_absence_is_allowed(self):
        self.reading(POWER, {"items": [{"a": 1}]})
        self.assertEqual([{"a": 1}], POWER._items("/x", absent_ok=True))
        self.reading(POWER, http_error(404))
        self.assertIsNone(POWER._items("/x", absent_ok=True))
        with self.assertRaises(urllib.error.HTTPError):
            POWER._items("/x")
        for failure in (http_error(403), http_error(500)):
            self.reading(POWER, failure)
            with self.subTest(failure=failure), self.assertRaises(urllib.error.HTTPError):
                POWER._items("/x", absent_ok=True)
        # A list cut short or a null answer is incomplete, absent allowed or not.
        for answer in ({"items": [], "metadata": {"continue": "more"}}, None):
            self.reading(POWER, answer)
            with self.subTest(answer=answer), self.assertRaisesRegex(ValueError, "incomplete inventory"):
                POWER._items("/x", absent_ok=True)

    def test_volume_deletion_takes_a_404_as_empty_only_for_an_optional_list(self):
        warnings = []
        self.reading(VOLUMES, http_error(404))
        self.assertEqual([], VOLUMES._items("/x", "virtual machine", warnings, optional=True))
        self.assertEqual([], warnings)
        self.assertEqual([], VOLUMES._items("/x", "pod", warnings))
        self.assertEqual(["pod inventory unavailable (HTTP 404)"], warnings)
        warnings = []
        self.reading(VOLUMES, None)
        self.assertEqual([], VOLUMES._items("/x", "virtual machine", warnings, optional=True))
        self.assertEqual(1, len(warnings))
        self.assertTrue(warnings[0].startswith("virtual machine inventory unavailable: "), warnings)
        warnings = []
        self.reading(VOLUMES, {"items": [], "metadata": {"continue": "more"}})
        self.assertEqual([], VOLUMES._items("/x", "virtual machine", warnings, optional=True))
        self.assertEqual(["virtual machine inventory unavailable: incomplete inventory"], warnings)
        warnings = []
        read = self.reading(VOLUMES, http_error(429, {"Retry-After": "0"}), http_error(404))
        with mock.patch.object(VOLUMES.time, "sleep"):
            self.assertEqual([], VOLUMES._items("/x", "virtual machine", warnings, optional=True))
        self.assertEqual([[], 2], [warnings, len(read.paths)])
        read = self.reading(VOLUMES, http_error(429, {"Retry-After": "0"}), {"items": [{"a": 1}]})
        with mock.patch.object(VOLUMES.time, "sleep"):
            self.assertEqual([{"a": 1}], VOLUMES._items("/x", "pod", warnings))
        self.assertEqual(2, len(read.paths))

    def test_the_network_picture_takes_403_and_404_as_empty_only_when_not_strict(self):
        for code in (403, 404):
            self.reading(NET_STATE, http_error(code))
            self.assertEqual([], NET_STATE._items("/x"))
            with self.subTest(code=code), self.assertRaises(urllib.error.HTTPError):
                NET_STATE._items("/x", strict=True)
        self.reading(NET_STATE, http_error(500))
        with self.assertRaises(urllib.error.HTTPError):
            NET_STATE._items("/x")
        read = self.reading(NET_STATE, http_error(429, {"Retry-After": "0"}), {"items": [{"a": 1}]})
        with mock.patch.object(NET_STATE.time, "sleep"):
            self.assertEqual([{"a": 1}], NET_STATE._items("/x", strict=True))
        self.assertEqual(2, len(read.paths))
        self.reading(NET_STATE, None)
        self.assertEqual([], NET_STATE._items("/x"))
        with self.assertRaises(AttributeError):
            NET_STATE._items("/x", strict=True)


if __name__ == "__main__":
    unittest.main()
