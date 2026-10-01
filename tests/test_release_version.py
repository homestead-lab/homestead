import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from release_version import resolve


class ReleaseVersionTests(unittest.TestCase):
    def test_prod_and_dev_are_named_and_match_the_embedded_version(self):
        self.assertEqual(("2.8.290", "prod"), resolve("v2.8.290", "2.8.290"))
        self.assertEqual(("2.8.291-dev.10", "dev"), resolve("v2.8.291-dev.10", "2.8.291-dev.10"))

    def test_a_mismatched_source_version_cannot_be_published(self):
        with self.assertRaisesRegex(ValueError, "does not match"):
            resolve("v2.8.291-dev.1", "2.8.290")

    def test_unnumbered_previews_and_other_tag_formats_cannot_be_published(self):
        for tag in ("dev", "v2.8.290-rc.1", "v2.8.290-dev.0", "v2.8.290-dev", "v2.8"):
            with self.assertRaisesRegex(ValueError, "tag must"):
                resolve(tag, "2.8.290")


if __name__ == "__main__":
    unittest.main()
