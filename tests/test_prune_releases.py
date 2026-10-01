"""The releases page kept short: 5 patches, 3 feature lines, 2 majors."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import prune_releases as PRUNE


def rows(*tags, latest=None, **extra):
    return [{"tagName": t, "isLatest": t == latest, "isDraft": False, "isPrerelease": False} for t in tags] + \
        [dict({"tagName": t, "isLatest": False, "isDraft": False, "isPrerelease": False}, **flags)
         for t, flags in extra.get("other", [])]


class PruneTests(unittest.TestCase):
    def test_five_patches_three_feature_lines_two_majors(self):
        tags = [f"v2.8.{n}" for n in range(230, 251)] + ["v2.7.9", "v2.7.3", "v2.6.4", "v2.5.1",
                                                       "v1.9.0", "v1.2.0", "v0.9.0"]
        kept = PRUNE.keep(rows(*tags, latest="v2.8.250"))
        self.assertEqual({"v2.8.250", "v2.8.249", "v2.8.248", "v2.8.247", "v2.8.246",   # patches
                          "v2.7.9", "v2.6.4",                                            # newest of 2.7, 2.6
                          "v1.9.0"}, kept)                                               # newest of 1.x

    def test_the_latest_the_one_just_published_and_anything_unusual_stay(self):
        tags = [f"v2.8.{n}" for n in range(1, 12)]
        kept = PRUNE.keep(rows(*tags, latest="v2.8.3", other=[("v2.9.0-rc1", {}), ("v2.8.12", {"isDraft": True})]),
                          also=["v2.8.2"])
        self.assertTrue({"v2.8.3", "v2.8.2", "v2.9.0-rc1", "v2.8.12"} <= kept)
        self.assertNotIn("v2.8.1", kept)

    def test_versions_sort_as_numbers_not_text(self):
        kept = PRUNE.keep(rows("v2.8.9", "v2.8.10", "v2.8.100", "v2.8.99", "v2.8.11", "v2.8.2"), patches=2,
                          features=0, majors=0)
        self.assertEqual({"v2.8.100", "v2.8.99"}, kept)

    def test_dev_releases_are_kept_separately_from_prod(self):
        stable = [f"v2.8.{n}" for n in range(1, 8)]
        dev = [(f"v2.8.8-dev.{n}", {"isPrerelease": True}) for n in range(1, 12)]
        kept = PRUNE.keep(rows(*stable, latest="v2.8.7", other=dev))
        self.assertTrue(set(stable[-5:]) <= kept)
        self.assertTrue({f"v2.8.8-dev.{n}" for n in range(7, 12)} <= kept)
        self.assertNotIn("v2.8.8-dev.6", kept)


if __name__ == "__main__":
    unittest.main()
