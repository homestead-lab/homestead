#!/usr/bin/env python3
"""The live demo for GitHub Pages: web/, running on demo.js's made-up data.

    python scripts/build_demo_site.py [--base /homestead/] [--out _site]

A project site lives under /<repo>/, not at the root Homestead serves from, so
the page gets a <base>, its own references become relative, and the router is
told where it sits. 404.html is the same page: Pages answers a deep link such
as /homestead/nodes with it, and the router takes it from there. There is no
service worker and no web app manifest - the demo is a page, not an app to
install - and search engines are asked to leave it out."""
import argparse
import re
import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / "web"

# Absolute paths the scripts build themselves, which a <base> does not reach.
SCRIPT_PATHS = re.compile(r"""(["'`])/(assets|icons)/""")


def build(base, out):
    base = "/" + base.strip("/") + "/" if base.strip("/") else "/"
    if out.exists():
        shutil.rmtree(out)
    shutil.copytree(WEB, out, ignore=shutil.ignore_patterns("sw.js", "manifest.webmanifest"))

    page = (out / "index.html").read_text(encoding="utf-8")
    page = re.sub(r'<link rel="manifest"[^>]*>\n?', "", page)
    # src="/js/app.js" -> src="js/app.js", href="/nodes" -> href="nodes", href="/" -> href="".
    page = re.sub(r'\b(src|href)="/(?!/)', r'\1="', page)
    head = (f'<base href="{base}">\n<meta name="robots" content="noindex">\n'
            f'<script>window.HOMESTEAD_BASE = "{base}"; window.HOMESTEAD_DEMO = true;</script>\n')
    page, found = re.subn(r"(<meta charset=[^>]*>\n?)", lambda m: m.group(1) + head, page, count=1)
    if not found:
        raise SystemExit("index.html has no <meta charset> to follow")
    (out / "index.html").write_text(page, encoding="utf-8")
    shutil.copyfile(out / "index.html", out / "404.html")

    for script in (out / "js").glob("*.js"):
        text = script.read_text(encoding="utf-8")
        changed = SCRIPT_PATHS.sub(lambda m: f"{m.group(1)}{base}{m.group(2)}/", text)
        if changed != text:
            script.write_text(changed, encoding="utf-8")
    # Pages would otherwise run the site through Jekyll.
    (out / ".nojekyll").write_text("", encoding="utf-8")
    return base


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--base", default="/homestead/", help="where the site is served, e.g. /homestead/")
    parser.add_argument("--out", default=str(ROOT / "_site"))
    args = parser.parse_args()
    base = build(args.base, Path(args.out))
    print(f"demo site for {base} in {args.out}")


if __name__ == "__main__":
    main()
