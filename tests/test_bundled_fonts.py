import hashlib, json, re, sys, unittest
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "server"))
sys.path.insert(0, str(ROOT / "scripts"))
import server
import homestead_static as STATIC
import subset_fonts

WEB = ROOT / "web"
FONTS = WEB / "vendor" / "fonts"


class BundledFontTests(unittest.TestCase):
    """The app's first screen asks no other host for anything: its fonts ship with it (#705)."""

    def test_the_page_names_no_other_host(self):
        # Links, scripts and stylesheets in index.html, and url()/@import in style.css, stay on this host.
        html = (WEB / "index.html").read_text()
        css = (WEB / "style.css").read_text()
        outside = re.findall(r'(?:href|src)="(https?:)?//[^"]+"', html)
        outside += re.findall(r'(?:url\(|@import\s+)["\']?(https?:)?//', css)
        self.assertEqual([], outside)

    def test_the_policy_allows_fonts_and_styles_from_here_only(self):
        policy = dict(part.split(" ", 1) for part in server.CSP.split("; "))
        self.assertEqual("'self' data:", policy["font-src"])
        self.assertEqual("'self' 'unsafe-inline'", policy["style-src"])

    def test_every_face_style_css_names_is_bundled_and_servable(self):
        faces = re.findall(r'url\(([^)]*vendor/fonts/[^)]+\.woff2)\)', (WEB / "style.css").read_text())
        self.assertTrue(faces)
        self.assertEqual("font/woff2", server.VENDOR_TYPES[".woff2"])
        for path in faces:
            with self.subTest(path=path):
                # Relative to the stylesheet: the live demo serves the app under /homestead/,
                # where a root-absolute /vendor/... would miss.
                self.assertFalse(path.startswith("/"), f"{path} is root-absolute")
                self.assertTrue((WEB / path).is_file(), f"{path} is missing")
                self.assertTrue(server.WEB_PATHS.is_vendor_path("/" + path))

    def test_each_font_family_ships_its_licence(self):
        for family, holder in (("inter-tight", "The Inter Project Authors"),
                               ("jetbrains-mono", "The JetBrains Mono Project Authors")):
            with self.subTest(family=family):
                licence = (FONTS / family / "OFL.txt").read_text()
                self.assertIn(holder, licence)
                self.assertIn("SIL OPEN FONT LICENSE Version 1.1", licence)
        notices = (ROOT / "THIRD_PARTY_NOTICES.md").read_text()
        self.assertIn("web/vendor/fonts/inter-tight/OFL.txt", notices)
        self.assertIn("web/vendor/fonts/jetbrains-mono/OFL.txt", notices)



class SubsetFontTests(unittest.TestCase):
    """The fonts are cut to what a Latin page needs (#982); the rest loads only for text that needs it.
    The files are built by scripts/subset_fonts.py: rerun it when one of these fails."""

    def setUp(self):
        self.manifest = json.loads((FONTS / "fonts.json").read_text())
        self.files = [dict(f, family=fam["family"]) for fam in self.manifest["families"] for f in fam["files"]]
        self.css = (WEB / "style.css").read_text()

    def part(self, family, name):
        return next(f for f in self.files if f["family"] == family and f["part"] == name)

    def test_a_latin_page_downloads_little_and_all_fonts_stay_small(self):
        latin = sum((WEB / f["path"]).stat().st_size for f in self.files if f["part"] == "latin")
        everything = sum(p.stat().st_size for p in FONTS.rglob("*.woff2"))
        self.assertLessEqual(latin, 160_000, "the fonts a Latin page loads (was 1.1 MB before #982)")
        self.assertLessEqual(everything, 450_000)

    def test_each_family_is_one_variable_face_in_two_parts(self):
        faces = re.findall(r"@font-face\{([^}]*)\}", self.css)
        self.assertEqual(4, len(faces))
        for face in faces:
            with self.subTest(face=face[:60]):
                self.assertRegex(face, r"font-weight:\d00 \d00;")
                self.assertRegex(face, r"unicode-range:U\+[0-9A-F]")

    def test_the_files_on_disk_are_the_ones_built(self):
        on_disk = sorted(str(p.relative_to(WEB)) for p in FONTS.rglob("*.woff2"))
        self.assertEqual(sorted(f["path"] for f in self.files), on_disk)
        for f in self.files:
            with self.subTest(path=f["path"]):
                digest = hashlib.sha256((WEB / f["path"]).read_bytes()).hexdigest()
                self.assertEqual(f["sha256"], digest, "edited by hand? rerun scripts/subset_fonts.py")
                self.assertTrue(f["path"].endswith(f".{digest[:10]}.woff2"))

    def test_the_stylesheet_names_each_part_with_its_range(self):
        for f in self.files:
            with self.subTest(path=f["path"]):
                self.assertIn(f"src:url({f['path']}) format(\"woff2\");unicode-range:{f['unicode_range']}}}", self.css)

    def test_the_parts_never_overlap_and_latin_holds_latin(self):
        for family in {f["family"] for f in self.files}:
            latin, rest = (subset_fonts.parse_range(self.part(family, part)["unicode_range"]) for part in ("latin", "rest"))
            with self.subTest(family=family):
                self.assertFalse([p for a, b in latin for p in range(a, b + 1) if subset_fonts.covers(rest, p)])
                for point in list(range(0x20, 0x7F)) + list(range(0xA0, 0x100)):
                    self.assertTrue(subset_fonts.covers(latin, point), f"U+{point:04X}")

    def test_every_character_the_ui_writes_is_in_the_first_part(self):
        # Otherwise a page with that character downloads the rest part too.
        for f in self.files:
            if f["part"] != "rest":
                continue
            rest = subset_fonts.parse_range(f["unicode_range"])
            missing = sorted(f"U+{p:04X} {chr(p)}" for p in subset_fonts.ui_characters() if subset_fonts.covers(rest, p))
            with self.subTest(family=f["family"]):
                self.assertEqual([], missing, "rerun scripts/subset_fonts.py")

    def test_the_page_preloads_the_latin_body_font(self):
        html = (WEB / "index.html").read_text()
        preloads = re.findall(r'<link rel="preload" href="([^"]+)" as="font"', html)
        self.assertEqual(["/" + self.part("Inter Tight", "latin")["path"]], preloads)

    def test_every_font_is_named_so_the_server_lets_the_browser_keep_it(self):
        for f in self.files:
            with self.subTest(path=f["path"]):
                found = STATIC.FINGERPRINT.search(f["path"].rsplit("/", 1)[-1])
                self.assertEqual(f["sha256"][:10], found and found.group(1))


if __name__ == "__main__":
    unittest.main()
