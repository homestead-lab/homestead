import copy
import sys
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
import homestead_updates as updates


class ChannelTests(unittest.TestCase):
    def test_prod_excludes_previews_and_sorts_versions_numerically(self):
        self.assertEqual("2.8.100", updates.channel_release("2.8.9", ["2.8.10", "2.8.100", "2.8.101-dev.1", "3.0.0"], "prod"))

    def test_dev_sorts_preview_numbers_and_excludes_stable_and_other_majors(self):
        self.assertEqual("2.9.0-dev.10", updates.channel_release("2.8.289", ["2.9.0-dev.2", "2.9.0-dev.10", "2.9.0", "3.0.0-dev.1"], "dev"))

    def test_returning_to_prod_can_offer_an_older_stable_version(self):
        self.assertEqual("2.8.289", updates.channel_release("2.8.290-dev.1", ["2.8.288", "2.8.289", "2.8.290-dev.1"], "prod"))

    def test_a_missing_dev_release_is_an_error_and_never_falls_back_to_prod(self):
        with self.assertRaisesRegex(ValueError, "No dev release"):
            updates.channel_release("2.8.289", ["2.8.289", "3.0.0-dev.1"], "dev")

    def test_an_unknown_version_cannot_select_a_different_major(self):
        with self.assertRaisesRegex(ValueError, "No release version"):
            updates.channel_release("latest", ["2.8.289", "3.0.0"], "prod")

    def test_channel_changes_invalidate_cached_reports_even_without_a_forced_check(self):
        prod = {"channel": "prod", "workloads": []}
        dev = {"channel": "dev", "workloads": []}
        with mock.patch.object(updates, "CHANNEL", return_value="dev"), \
             mock.patch.object(updates, "_LATEST", {"report": prod, "number": 1, "finished": time.time()}), \
             mock.patch.object(updates, "scan", return_value=dev) as scan:
            self.assertEqual(dev, updates.report())
            scan.assert_called_once_with(False)

    def check(self, part, channel, current="2.8.289", digest="sha256:" + "a" * 64, tags=None):
        source = "ghcr.io/wjcloudy/homestead:" + current
        dep = {"metadata": {"namespace": "lab", "name": "homestead", "annotations": {
            updates.TRACKED: '{"homestead": "' + source + '"}'}}, "spec": {"replicas": 0,
            "selector": {"matchLabels": {}}, "template": {"spec": {"containers": [
                {"name": "homestead", "image": "ghcr.io/wjcloudy/homestead@" + digest if digest else source}]}}}}
        with mock.patch.object(updates, "PART", return_value=part), \
             mock.patch.object(updates, "CHANNEL", return_value=channel), \
             mock.patch.object(updates, "_secret_credentials", return_value={}), \
             mock.patch.object(updates, "registry_tags", return_value=tags or ["2.8.289", "2.8.290-dev.1"]), \
             mock.patch.object(updates, "manifest_info", return_value={"digest": "sha256:" + "b" * 64, "children": []}):
            return updates._check_deployment(copy.deepcopy(dep), [], persist=False)

    def test_only_homestead_itself_uses_the_preview_channel(self):
        self.assertEqual("2.8.290-dev.1", self.check("self", "dev")["images"][0]["candidate_tag"])
        self.assertEqual("2.8.289", self.check("", "dev")["images"][0]["candidate_tag"])
        self.assertEqual("2.8.289", self.check("nfs", "dev")["images"][0]["candidate_tag"])

    def test_same_digest_is_current_even_when_switching_channel(self):
        self.assertFalse(self.check("self", "dev", digest="sha256:" + "b" * 64)["available"])

    def test_never_run_current_release_is_unchecked(self):
        checked = self.check("self", "prod", digest="")
        self.assertFalse(checked["available"])
        self.assertTrue(checked["unchecked"])

    def test_missing_dev_release_is_reported_without_an_install_candidate(self):
        checked = self.check("self", "dev", tags=["2.8.289"])
        self.assertFalse(checked["available"])
        self.assertIn("No dev release", checked["images"][0]["error"])


if __name__ == "__main__":
    unittest.main()
