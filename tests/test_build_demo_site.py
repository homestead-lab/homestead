"""scripts/build_demo_site.py: the demo site's scripts must not ask for files at the site root."""
import re
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import build_demo_site as BUILD

BASE = "/homestead/"


class SmallSite(unittest.TestCase):
    """build() on a one-script site, so the rewrite is checked on known text."""

    def built(self, source):
        with tempfile.TemporaryDirectory() as tmp:
            web = Path(tmp) / "web"
            (web / "js").mkdir(parents=True)
            (web / "index.html").write_text('<meta charset="utf-8">\n', encoding="utf-8")
            (web / "js" / "x.js").write_text(source, encoding="utf-8")
            old, BUILD.WEB = BUILD.WEB, web
            try:
                BUILD.build(BASE, Path(tmp) / "_site")
            finally:
                BUILD.WEB = old
            return (Path(tmp) / "_site" / "js" / "x.js").read_text(encoding="utf-8")

    def test_vendor_is_moved_under_the_base_bare_or_with_a_file(self):
        out = self.built('const a = "/vendor"; const b = `/vendor/xterm/xterm.js${v}`; const c = \'/vendor/monaco\';')
        self.assertEqual(out, 'const a = "/homestead/vendor"; const b = `/homestead/vendor/xterm/xterm.js${v}`; '
                              "const c = '/homestead/vendor/monaco';")

    def test_assets_and_icons_are_still_moved(self):
        self.assertEqual(self.built('"/assets/a.png" "/icons/b.svg"'), '"/homestead/assets/a.png" "/homestead/icons/b.svg"')

    def test_other_paths_are_left_alone(self):
        text = '"/vendors/a" "/vendorx" "/api/nodes" "vendor/x"'
        self.assertEqual(self.built(text), text)


class BuiltScriptPaths(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.out = Path(cls.tmp.name) / "_site"
        BUILD.build(BASE, cls.out)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def scripts(self):
        return {p.name: p.read_text(encoding="utf-8") for p in (self.out / "js").glob("*.js")}

    def test_no_script_asks_for_vendor_at_the_site_root(self):
        # The editor's library (Monaco) is asked for through vendorBase(), which on the demo
        # is the bare string "/vendor"; the terminal's loadXterm() builds "/vendor/xterm/...".
        root_vendor = re.compile(r"""(["'`])/vendor(?=/|\1)""")
        left = {name: len(root_vendor.findall(text)) for name, text in self.scripts().items()
                if root_vendor.search(text)}
        self.assertEqual(left, {})

    def test_vendor_paths_sit_under_the_base(self):
        text = self.scripts()["views-overview.js"]
        self.assertIn(f'"{BASE}vendor"', text)  # what vendorBase() returns on the demo
        self.assertIn(f"`{BASE}vendor/xterm/xterm.js", text)

    def test_assets_and_icons_still_sit_under_the_base(self):
        for name, text in self.scripts().items():
            self.assertIsNone(re.search(r"""(["'`])/(assets|icons)/""", text), name)


if __name__ == "__main__":
    unittest.main()
