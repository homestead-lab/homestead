"""List reads through homestead_kube.items (#619 step 4): each module lists
through the reader it was given, an incomplete list (no items, cut short, rows
that are not objects) is an error in that module's own words, and a failed
read raises rather than passing for an empty list."""
import io
import os
import sys
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.environ.get("HOMESTEAD_TEST_SERVER_DIR", str(Path(__file__).resolve().parents[1] / "server")))
import homestead_csi_restore as CSI_RESTORE
import homestead_kube as KUBE
import homestead_maintenance as MAINTENANCE
import homestead_reclass as RECLASS
import homestead_rollout_capacity as ROLLOUT_CAPACITY
import homestead_smb_recovery as SMB_RECOVERY


def http_error(code):
    return urllib.error.HTTPError("/x", code, "error", {}, io.BytesIO(b""))


class Reads:
    """A reader answering from a dict of path -> object or exception."""
    def __init__(self, answers):
        self.answers, self.paths = answers, []

    def __call__(self, path, **_):
        self.paths.append(path)
        answer = self.answers[path]
        if isinstance(answer, Exception):
            raise answer
        return answer


PODS = "/api/v1/namespaces/lab/pods"
VMS = "/apis/kubevirt.io/v1/namespaces/lab/virtualmachines"
ROW = {"metadata": {"name": "a"}}
CUT_SHORT = {"items": [ROW], "metadata": {"continue": "token"}}


class ReclassTests(unittest.TestCase):
    def listing(self, answers, path=PODS, **options):
        with mock.patch.object(RECLASS, "kget", Reads(answers)), mock.patch.object(KUBE, "kget", Reads({})):
            return RECLASS._items(path, **options)

    def test_rows_come_through_the_module_reader(self):
        self.assertEqual(self.listing({PODS: {"items": [ROW]}}), [ROW])

    def test_an_incomplete_list_is_an_error_in_its_own_words(self):
        for answer in ({}, {"items": None}, CUT_SHORT, [], {"items": [ROW, "x"]}):
            with self.subTest(answer=answer):
                with self.assertRaisesRegex(ValueError, "^The volume list"):
                    self.listing({PODS: answer})

    def test_a_failed_read_raises(self):
        for code in (403, 404, 500):
            with self.subTest(code=code):
                with self.assertRaises(urllib.error.HTTPError):
                    self.listing({PODS: http_error(code)})

    def test_an_absent_optional_group_is_an_empty_list(self):
        answers = {VMS: http_error(404), "/apis": {"groups": [{"name": "apps"}]}}
        self.assertEqual(self.listing(answers, VMS, optional_group="kubevirt.io"), [])

    def test_a_404_from_an_installed_group_raises(self):
        answers = {VMS: http_error(404), "/apis": {"groups": [{"name": "kubevirt.io"}]}}
        with self.assertRaises(urllib.error.HTTPError):
            self.listing(answers, VMS, optional_group="kubevirt.io")

    def test_unreadable_discovery_is_an_error(self):
        answers = {VMS: http_error(404), "/apis": {"groups": None}}
        with self.assertRaises(ValueError):
            self.listing(answers, VMS, optional_group="kubevirt.io")


class CsiRestoreTests(unittest.TestCase):
    def listing(self, answer):
        with mock.patch.object(CSI_RESTORE, "kget", Reads({PODS: answer})), mock.patch.object(KUBE, "kget", Reads({})):
            return CSI_RESTORE._items(PODS)

    def test_rows_come_through_the_module_reader(self):
        self.assertEqual(self.listing({"items": [ROW]}), [ROW])

    def test_an_incomplete_list_is_an_error_in_its_own_words(self):
        # Before #619 step 4 only a missing "items" or a cut-short list was
        # refused here; items(None) and rows that are not objects now are too.
        for answer in ({}, CUT_SHORT, {"items": None}, {"items": [ROW, "x"]}):
            with self.subTest(answer=answer):
                with self.assertRaisesRegex(ValueError, "^Snapshot support could not be checked"):
                    self.listing(answer)

    def test_a_failed_read_raises(self):
        with self.assertRaises(urllib.error.HTTPError):
            self.listing(http_error(500))


class GivenReaderTests(unittest.TestCase):
    """maintenance.items and rollout_capacity.items list through the reader
    their caller passes."""
    def test_rows_come_through_the_given_reader(self):
        for module in (MAINTENANCE, ROLLOUT_CAPACITY):
            with self.subTest(module=module.__name__), mock.patch.object(KUBE, "kget", Reads({})):
                read = Reads({PODS: {"items": [ROW]}})
                self.assertEqual(module.items(read, PODS), [ROW])
                self.assertEqual(read.paths, [PODS])

    def test_an_incomplete_list_is_an_error_in_its_own_words(self):
        for module, words in ((MAINTENANCE, "^incomplete inventory: " + PODS), (ROLLOUT_CAPACITY, "^incomplete inventory")):
            for answer in ({}, {"items": None}, CUT_SHORT):
                with self.subTest(module=module.__name__, answer=answer):
                    with self.assertRaisesRegex(ValueError, words):
                        module.items(Reads({PODS: answer}), PODS)

    def test_a_failed_read_raises(self):
        for module in (MAINTENANCE, ROLLOUT_CAPACITY):
            with self.subTest(module=module.__name__):
                with self.assertRaises(urllib.error.HTTPError):
                    module.items(Reads({PODS: http_error(500)}), PODS)


class OtherErrorsTests(unittest.TestCase):
    """Only an incomplete list takes the module's words: any other ValueError
    (an invalid name in the path, an answer that was not JSON) keeps its own."""
    INVALID = ValueError("'Bad_Name' is not a valid Kubernetes name")

    def test_other_errors_keep_their_own_message(self):
        reads = Reads({PODS: self.INVALID})
        calls = (
            ("reclass", lambda: self.through(RECLASS, lambda: RECLASS._items(PODS), reads)),
            ("csi_restore", lambda: self.through(CSI_RESTORE, lambda: CSI_RESTORE._items(PODS), reads)),
            ("maintenance", lambda: MAINTENANCE.items(reads, PODS)),
            ("rollout_capacity", lambda: ROLLOUT_CAPACITY.items(reads, PODS)),
        )
        for name, call in calls:
            with self.subTest(module=name):
                with self.assertRaisesRegex(ValueError, "not a valid Kubernetes name"):
                    call()

    @staticmethod
    def through(module, call, reads):
        with mock.patch.object(module, "kget", reads):
            return call()


class SmbRecoveryTests(unittest.TestCase):
    def test_an_incomplete_inventory_keeps_shares_as_they_are(self):
        nodes = "/api/v1/nodes"
        for answer in ({}, {"items": None}, CUT_SHORT, http_error(500)):
            with self.subTest(answer=answer):
                result = SMB_RECOVERY.observe([{"pvc": "data"}], Reads({nodes: answer}), "lab")
                self.assertEqual(result[0], {})
                self.assertIn("could not confirm", result[1])


if __name__ == "__main__":
    unittest.main()
