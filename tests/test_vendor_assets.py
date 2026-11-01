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
                self.assertTrue(server.WEB_PATHS.is_vendor_path(path))

    def test_a_path_under_a_version_is_a_vendor_path_too(self):
        self.assertTrue(server.WEB_PATHS.is_vendor_path("/vendor/@2.8.334/monaco/vs/loader.js"))
        self.assertEqual(("/vendor/monaco/vs/loader.js", "c0a1"),
                         server.WEB_PATHS.split_vendor_version("/vendor/@c0a1/monaco/vs/loader.js"))
        self.assertEqual(("/vendor/monaco/vs/loader.js", ""),
                         server.WEB_PATHS.split_vendor_version("/vendor/monaco/vs/loader.js"))
        for path in ("/vendor/@1/../server/server.py", "/vendor/@/monaco/vs/loader.js", "/vendor/@1/monaco/vs/loader.py"):
            self.assertFalse(server.WEB_PATHS.is_vendor_path(path), path)

    def test_traversal_and_odd_types_are_refused(self):
        for path in ("/vendor/../server/server.py", "/vendor/monaco/../../etc/passwd",
                     "/vendor/monaco/vs/loader.py", "/vendor/monaco/secrets.env",
                     "/vendors/monaco/vs/loader.js", "/vendor/", "/vendor/monaco/vs/loader"):
            with self.subTest(path=path):
                self.assertFalse(server.WEB_PATHS.is_vendor_path(path))

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
