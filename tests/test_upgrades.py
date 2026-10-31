import sys
import tempfile
import urllib.error
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "server"))

import homestead_alerts as ALERTS
import homestead_upgrades as UPGRADES

RELEASES = [
    {"tag_name": "v1.5.1", "prerelease": False, "published_at": "2026-08-01T00:00:00Z", "html_url": "u151"},
    {"tag_name": "v1.6.0-rc2", "prerelease": True, "published_at": "2026-09-01T00:00:00Z", "html_url": "u160rc2"},
    {"tag_name": "v1.5.0", "prerelease": False, "published_at": "2026-05-01T00:00:00Z", "html_url": "u150"},
    {"tag_name": "v1.4.3", "prerelease": False, "published_at": "2026-04-01T00:00:00Z", "html_url": "u143"},
    {"tag_name": "v1.6.0-draft", "draft": True},
    {"tag_name": "v1.5.1-rc1", "prerelease": True, "published_at": "2026-07-01T00:00:00Z", "html_url": "u151rc1"},
]

UPGRADE = {"metadata": {"name": "hvst-upgrade-x", "creationTimestamp": "2026-09-20T10:00:00Z",
                        "labels": {"harvesterhci.io/latestUpgrade": "true"}},
           "spec": {"version": "v1.5.1"},
           "status": {"previousVersion": "v1.5.0",
                      "conditions": [{"type": "ImageReady", "status": "True"}, {"type": "RepoReady", "status": "True"},
                                     {"type": "NodesPrepared", "status": "Unknown"}, {"type": "Completed", "status": "Unknown"}],
                      "nodeStatuses": {"node2": {"state": "Images preloading"}, "node1": {"state": "Images preloaded"}}}}


class UpgradeTests(unittest.TestCase):
    def setUp(self):
        self.upgrades = [UPGRADE]
        self.versions = [{"metadata": {"name": "v1.5.1"}, "spec": {"releaseDate": "20260801"}}]
        self.calls = 0

        def fetch(url):
            self.calls += 1
            return RELEASES

        def get(path):
            return {"items": self.upgrades if path.endswith("/upgrades") else self.versions}

        UPGRADES._releases.update(at=0.0, value=[], error="")
        UPGRADES.bind(get, fetch)

    def test_versions_order_releases_above_their_candidates(self):
        tags = ["v1.5.1-rc1", "v1.5.1", "v1.4.10", "v1.5.0", "junk"]
        self.assertEqual(["v1.5.1", "v1.5.1-rc1", "v1.5.0", "v1.4.10", "junk"],
                         sorted(tags, key=UPGRADES.version_key, reverse=True))
        self.assertEqual(["stable", "rc", "dev", "test"],
                         [UPGRADES.channel(t, p) for t, p in (("v1.5.1", False), ("v1.6.0-rc2", True),
                                                               ("v1.7-head", True), ("v1.6.0", True))])

    def test_the_report_names_the_newest_stable_and_test_builds(self):
        report = UPGRADES.report("1.5.0")
        self.assertEqual("v1.5.1", report["stable"]["tag"])
        self.assertTrue(report["stable"]["offered"])
        self.assertEqual("v1.6.0-rc2", report["test"]["tag"])
        self.assertFalse(report["test"]["offered"])
        self.assertNotIn("v1.6.0-draft", [row["tag"] for row in report["recent"]])
        # An older candidate than the newest stable release is not news.
        self.assertNotEqual("v1.5.1-rc1", report["test"]["tag"])
        UPGRADES.report("1.5.0")
        self.assertEqual(1, self.calls, "GitHub is asked once an hour, not every page load")

    def test_an_up_to_date_cluster_has_no_stable_upgrade(self):
        report = UPGRADES.report("1.5.1")
        self.assertIsNone(report["stable"])
        self.assertEqual("v1.6.0-rc2", report["test"]["tag"])

    def test_a_running_upgrade_shows_its_step_and_nodes(self):
        up = UPGRADES.report("1.5.0")["active"]
        self.assertEqual(("running", 40), (up["state"], up["progress"]))
        self.assertEqual(["done", "done", "running", "waiting", "waiting"], [s["state"] for s in up["steps"]])
        self.assertEqual(["node1", "node2"], [n["name"] for n in up["nodes"]])

    def test_finished_and_failed_upgrades(self):
        done = {**UPGRADE, "status": {"conditions": [{"type": "Completed", "status": "True"}]}}
        failed = {**UPGRADE, "status": {"conditions": [
            {"type": "ImageReady", "status": "False", "reason": "Failed", "message": "image pull failed"},
            {"type": "Completed", "status": "False", "reason": "Failed", "message": "upgrade image not ready"}]}}
        self.upgrades = [done]
        report = UPGRADES.report("1.5.1")
        self.assertIsNone(report["active"])
        self.assertEqual(("succeeded", 100), (report["last"]["state"], report["last"]["progress"]))
        self.upgrades = [failed]
        last = UPGRADES.report("1.5.0")["last"]
        self.assertEqual("failed", last["state"])
        self.assertEqual("failed", last["steps"][0]["state"])
        self.assertIn("not ready", last["message"])

    def test_a_github_outage_keeps_what_was_known(self):
        UPGRADES.report("1.5.0")

        def broken(url):
            raise OSError("no route")

        UPGRADES.bind(UPGRADES.kget, broken)
        report = UPGRADES.report("1.5.0", force=True)
        self.assertEqual("v1.5.1", report["stable"]["tag"])
        self.assertIn("no route", report["error"])

    def test_alerts_follow_an_upgrade_and_a_new_release(self):
        facts = ALERTS.upgrade_facts(UPGRADES.report("1.5.0"))
        keys = {fact["key"] for fact in facts}
        self.assertIn("platform:hvst-upgrade-x:started", keys)
        self.assertIn("platform:release:v1.5.1", keys)
        self.assertFalse(any(key.endswith(":failed") for key in keys))
        self.assertTrue(all(fact["event"] for fact in facts))


def http_error(code):
    return urllib.error.HTTPError("https://192.0.2.1/x", code, "failed", {}, None)


class UnreadUpgradesTests(unittest.TestCase):
    """Harvester's versions and upgrades that cannot be read are not "none" (sweep unknown-as-fine, row 16)."""

    def setUp(self):
        self.failing = True
        self.groups = [{"name": "harvesterhci.io"}]

        def get(path):
            if path == "/apis":
                return {"groups": self.groups}
            if self.failing:
                raise http_error(500)
            return {"items": [UPGRADE] if path.endswith("/upgrades") else
                    [{"metadata": {"name": "v1.5.1"}, "spec": {"releaseDate": "20260801"}}]}

        UPGRADES._releases.update(at=0.0, value=[], error="")
        UPGRADES.bind(get, lambda url: RELEASES)

    def test_a_failed_read_raises_and_the_report_says_it_could_not_read(self):
        for fn in (UPGRADES.offered, UPGRADES.upgrades):
            with self.assertRaises(urllib.error.HTTPError):
                fn()
        report = UPGRADES.report("1.5.0")
        self.assertEqual((None, None, None, None), (report["history"], report["offered"], report["active"], report["last"]))
        self.assertIn("could not read", report["unread"])
        self.assertEqual("v1.5.1", report["stable"]["tag"], "the releases from GitHub still show")
        self.assertIsNone(report["stable"]["offered"], "not known whether Harvester offers it")

    def test_a_cluster_without_harvester_has_no_upgrades_and_no_error(self):
        self.groups = [{"name": "apps"}]

        def get(path):
            if path == "/apis":
                return {"groups": self.groups}
            raise http_error(404)
        UPGRADES.bind(get, lambda url: RELEASES)
        report = UPGRADES.report("")
        self.assertEqual(([], [], ""), (report["history"], report["offered"], report["unread"]))
        self.assertEqual([], ALERTS.upgrade_facts(report))

    def test_its_alerts_stay_in_the_bell_and_are_not_pushed_again(self):
        before = ALERTS.DATA_DIR
        self.addCleanup(ALERTS.bind, before)
        ALERTS.bind(tempfile.mkdtemp())
        self.failing = False
        ALERTS.observe({"platform": []}, now=900)       # a source already watched: its events are news
        first = ALERTS.observe({"platform": ALERTS.upgrade_facts(UPGRADES.report("1.5.0"))}, now=1000)
        self.assertIn("platform:hvst-upgrade-x:started", {e["key"] for e in first})
        self.failing = True
        for later in (1100, 1300, 5000):
            facts = ALERTS.upgrade_facts(UPGRADES.report("1.5.0"))
            self.assertIsNone(facts, "the platform source could not look")
            self.assertEqual([], ALERTS.observe({"platform": facts}, now=later))
        self.failing = False
        self.assertEqual([], ALERTS.observe({"platform": ALERTS.upgrade_facts(UPGRADES.report("1.5.0"))}, now=6000),
                         "nothing is announced twice when the read works again")


if __name__ == "__main__":
    unittest.main()
