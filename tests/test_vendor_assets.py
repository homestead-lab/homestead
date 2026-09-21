import sys, unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
import server


class VendorAssetTests(unittest.TestCase):
    """Vendored libraries are served by path; nothing else is."""

    def test_monaco_files_are_servable(self):
        for path in ("/vendor/monaco/vs/loader.js", "/vendor/monaco/vs/editor/editor.main.css",
                     "/vendor/monaco/vs/base/browser/ui/codicons/codicon/codicon.ttf",
                     "/vendor/monaco/vs/language/json/jsonWorker.js"):
            with self.subTest(path=path):
                self.assertTrue(server.is_vendor_path(path))

    def test_traversal_and_odd_types_are_refused(self):
        for path in ("/vendor/../server/server.py", "/vendor/monaco/../../etc/passwd",
                     "/vendor/monaco/vs/loader.py", "/vendor/monaco/secrets.env",
                     "/vendors/monaco/vs/loader.js", "/vendor/", "/vendor/monaco/vs/loader"):
            with self.subTest(path=path):
                self.assertFalse(server.is_vendor_path(path))

    def test_every_vendored_file_the_editor_needs_is_present(self):
        root = Path(__file__).resolve().parents[1] / "web" / "vendor" / "monaco"
        for relative in ("vs/loader.js", "vs/editor/editor.main.js", "vs/editor/editor.main.css",
                         "vs/base/worker/workerMain.js", "vs/language/json/jsonMode.js",
                         "vs/language/json/jsonWorker.js", "vs/basic-languages/yaml/yaml.js",
                         "vs/base/browser/ui/codicons/codicon/codicon.ttf", "LICENSE"):
            with self.subTest(relative=relative):
                self.assertTrue((root / relative).is_file(), f"{relative} is missing")


if __name__ == "__main__":
    unittest.main()
