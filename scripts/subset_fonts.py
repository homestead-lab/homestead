"""Builds the fonts Homestead ships (web/vendor/fonts) from their upstream sources (#982).

Each family is one variable font (Inter Tight 400-800, JetBrains Mono 400-700) cut
in two: "latin" holds Basic Latin, Latin-1 and every other character the UI's own
text uses, and is all a page in a Latin script downloads; "rest" holds every other
character the font has (Cyrillic, Greek, Vietnamese, box drawing, ...), behind a
unicode-range, so the browser fetches it only for text that needs it. Characters
neither part has fall back to the next font in the stack, as before.

Files are named by their content (Name-part.<sha256[:10]>.woff2), so the server
lets the browser keep them for good (homestead_static.serve). This writes the
fonts, fonts.json beside them, the @font-face block at the top of web/style.css
and the preload in web/index.html; tests/test_bundled_fonts.py checks they agree.

It needs fontTools and brotli, so run it in a container:

    podman run --rm -v "$PWD":/repo:z -w /repo docker.io/library/python:3.12 sh -c \\
      'pip install -q fonttools==4.59.0 brotli==1.1.0 && python scripts/subset_fonts.py'

--check rebuilds into memory and exits 1 if anything committed differs.
--sources DIR reads <family>.ttf from DIR instead of downloading the pinned files.
"""
import argparse
import hashlib
import io
import json
import re
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / "web"
FONTS = WEB / "vendor" / "fonts"
MANIFEST = FONTS / "fonts.json"
FONTTOOLS = "4.59.0"

FAMILIES = (
    {"family": "Inter Tight", "dir": "inter-tight", "stem": "InterTight", "weights": (400, 800),
     "url": "https://raw.githubusercontent.com/googlefonts/inter-gf-tight/"
            "c194f94c60b569b47876811321f5ef1f0c2614a2/fonts/variable/InterTight%5Bwght%5D.ttf",
     "sha256": "b81b73dcb64df3c230cabade7df6c5773bf863233f24c9ee51087519f1f88b6f"},
    {"family": "JetBrains Mono", "dir": "jetbrains-mono", "stem": "JetBrainsMono", "weights": (400, 700),
     "url": "https://raw.githubusercontent.com/JetBrains/JetBrainsMono/"
            "cd5227bd1f61dff3bbd6c814ceaf7ffd95e947d9/fonts/variable/JetBrainsMono%5Bwght%5D.ttf",
     "sha256": "662a196d58f1183bf2d77428b6d5283fe3f45161ab021bea4036bc98e5cac016"},
)

# What Google Fonts calls "latin": Basic Latin, Latin-1 and the common punctuation,
# currency and math signs that Latin text uses.
LATIN = (set(range(0x00, 0x100)) | {0x131, 0x152, 0x153, 0x2BB, 0x2BC, 0x2C6, 0x2DA, 0x2DC, 0x304, 0x308,
         0x329, 0x20AC, 0x2122, 0x2191, 0x2193, 0x2212, 0x2215, 0xFEFF, 0xFFFD} | set(range(0x2000, 0x2070)))
# Text the UI writes itself: its own characters go in the first file too.
UI_TEXT = ("web/index.html", "web/style.css", "web/js/*.js", "server/*.py")

CSS_START = "/* Fonts: rendered by scripts/subset_fonts.py"
CSS_END = "/* End of the rendered fonts. */"
PRELOAD = re.compile(r'<link rel="preload" href="[^"]*" as="font" type="font/woff2" crossorigin>')


def ui_characters():
    """Every character above Latin-1 in the UI's own text."""
    found = set()
    for pattern in UI_TEXT:
        for path in sorted(ROOT.glob(pattern)):
            found.update(ord(ch) for ch in path.read_text(encoding="utf-8") if ord(ch) > 0xFF)
    return found


def ranges(points):
    """Code points as merged [first, last] pairs."""
    out = []
    for point in sorted(points):
        if out and point == out[-1][1] + 1:
            out[-1][1] = point
        else:
            out.append([point, point])
    return out


def unicode_range(pairs):
    return ",".join(f"U+{a:X}" if a == b else f"U+{a:X}-{b:X}" for a, b in pairs)


def parse_range(text):
    """unicode_range() read back as [first, last] pairs."""
    pairs = []
    for piece in text.split(","):
        first, _, last = piece.strip()[2:].partition("-")
        pairs.append([int(first, 16), int(last or first, 16)])
    return pairs


def covers(pairs, point):
    return any(a <= point <= b for a, b in pairs)


def _source(spec, folder):
    data = (Path(folder) / f"{spec['stem']}.ttf").read_bytes() if folder else \
        urllib.request.urlopen(spec["url"], timeout=60).read()
    if hashlib.sha256(data).hexdigest() != spec["sha256"]:
        sys.exit(f"{spec['stem']}: source does not match its pinned sha256")
    return data


def _cut(data, weights, points):
    """The source instanced to weights, kept to points, as woff2 bytes."""
    from fontTools import subset
    from fontTools.ttLib import TTFont
    from fontTools.varLib import instancer
    font = instancer.instantiateVariableFont(TTFont(io.BytesIO(data), recalcTimestamp=False, lazy=False),
                                             {"wght": tuple(weights)})
    narrowed = io.BytesIO()
    font.save(narrowed)  # read back whole: the subsetter trips over a lazily read gvar
    font = TTFont(io.BytesIO(narrowed.getvalue()), recalcTimestamp=False, lazy=False)
    options = subset.Options()
    options.flavor = "woff2"
    options.layout_features = ["*"]
    options.name_IDs = ["*"]
    options.name_languages = ["*"]
    options.notdef_outline = True
    subsetter = subset.Subsetter(options)
    subsetter.populate(unicodes=points)
    subsetter.subset(font)
    out = io.BytesIO()
    font.flavor = "woff2"
    font.save(out)
    return out.getvalue()


def build(folder=None):
    """{relative path: bytes} for every file this writes, fonts.json and the two page edits included."""
    from fontTools.ttLib import TTFont
    ui = ui_characters()
    files, faces, manifest = {}, [], {"fonttools": FONTTOOLS, "families": []}
    for spec in FAMILIES:
        data = _source(spec, folder)
        cmap = set(TTFont(io.BytesIO(data)).getBestCmap())
        parts = {"latin": cmap & (LATIN | ui), "rest": cmap - LATIN - ui}
        entry = {"family": spec["family"], "weights": list(spec["weights"]), "source": spec["url"],
                 "source_sha256": spec["sha256"], "files": []}
        # rest first: of two faces that both cover a character the later one wins,
        # and the two never overlap anyway.
        for part in ("rest", "latin"):
            body = _cut(data, spec["weights"], parts[part])
            digest = hashlib.sha256(body).hexdigest()
            name = f"vendor/fonts/{spec['dir']}/{spec['stem']}-{part}.{digest[:10]}.woff2"
            files["web/" + name] = body
            pairs = ranges(parts[part])
            entry["files"].append({"part": part, "path": name, "bytes": len(body), "sha256": digest,
                                   "unicode_range": unicode_range(pairs)})
            faces.append(f'@font-face{{font-family:"{spec["family"]}";font-style:normal;'
                         f'font-weight:{spec["weights"][0]} {spec["weights"][1]};font-display:swap;'
                         f'src:url({name}) format("woff2");unicode-range:{unicode_range(pairs)}}}')
        manifest["families"].append(entry)
    files["web/vendor/fonts/fonts.json"] = (json.dumps(manifest, indent=1) + "\n").encode()
    block = (f"{CSS_START}; edit that, not this. They ship with Homestead (web/vendor/fonts,\n"
             "   SIL OFL 1.1; see THIRD_PARTY_NOTICES.md), so the first screen never waits on another host. A page\n"
             "   in a Latin script downloads only the latin part; swap: text shows in the fallback until it arrives. */\n"
             + "\n".join(faces) + "\n" + CSS_END + "\n")
    css = (WEB / "style.css").read_text(encoding="utf-8")
    start, end = css.index(CSS_START), css.index(CSS_END) + len(CSS_END) + 1
    files["web/style.css"] = (css[:start] + block + css[end:]).encode()
    latin = next(f["path"] for f in manifest["families"][0]["files"] if f["part"] == "latin")
    page = (WEB / "index.html").read_text(encoding="utf-8")
    files["web/index.html"] = PRELOAD.sub(
        f'<link rel="preload" href="/{latin}" as="font" type="font/woff2" crossorigin>', page, count=1).encode()
    return files


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--sources")
    args = parser.parse_args()
    import fontTools
    if fontTools.version != FONTTOOLS:
        sys.exit(f"fontTools {fontTools.version} found; the committed fonts are built with {FONTTOOLS}")
    files = build(args.sources)
    stale = sorted(str(p.relative_to(ROOT)) for d in FONTS.iterdir() if d.is_dir()
                   for p in d.glob("*.woff2") if str(p.relative_to(ROOT)) not in files)
    if args.check:
        changed = [name for name, body in files.items()
                   if not (ROOT / name).is_file() or (ROOT / name).read_bytes() != body]
        for name in changed + stale:
            print(f"differs: {name}")
        sys.exit(1 if changed or stale else 0)
    for name in stale:
        (ROOT / name).unlink()
    for name, body in files.items():
        (ROOT / name).write_bytes(body)
        print(f"{name} {len(body)}")


if __name__ == "__main__":
    main()
