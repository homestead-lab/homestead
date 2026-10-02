import contextlib
import io
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import bump_version


class VersionBumpTests(unittest.TestCase):
    def bump(self, old, new, text):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "server").mkdir()
            (root / "server/server.py").write_text(f'os.environ.get("HOMESTEAD_VERSION", "{old}")', encoding="utf-8")
            (root / "fixture.txt").write_text(text, encoding="utf-8")
            with patch.object(bump_version, "ROOT", root), patch.object(bump_version, "FILES", ("server/server.py", "fixture.txt")), \
                    patch.object(bump_version, "GENERATED", ()), contextlib.redirect_stdout(io.StringIO()):
                bump_version.main(new)
                self.assertEqual(bump_version.current(), new)
            return (root / "fixture.txt").read_text(encoding="utf-8")

    def test_stable_to_dev_preserves_historical_previews_and_longer_patch_numbers(self):
        text = 'image:2.8.294 /style.css?v=2.8.294 "2.8.294-dev.3" "2.8.2940"'
        self.assertEqual(self.bump("2.8.294", "2.8.295-dev.1", text),
                         'image:2.8.295-dev.1 /style.css?v=2.8.295-dev.1 "2.8.294-dev.3" "2.8.2940"')

    def test_preview_bump_preserves_other_preview_numbers_and_stable_references(self):
        text = '"2.8.295-dev.1" "2.8.295-dev.10" "2.8.295"'
        self.assertEqual(self.bump("2.8.295-dev.1", "2.8.295-dev.2", text),
                         '"2.8.295-dev.2" "2.8.295-dev.10" "2.8.295"')


if __name__ == "__main__":
    unittest.main()
