"""Moves every place that names Homestead's release to a new version.

    python scripts/bump_version.py 2.8.80

The release is named in the server's default, the manifests, the page's cache
busters, the service worker and a test; this changes all of them, then
regenerates the files made from the manifest.
"""
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FILES = ("README.md", "deploy/deploy.yaml", "server/server.py", "tests/test_deploy.py", "web/index.html",
         "web/js/auth.js", "web/js/core.js", "web/js/demo.js", "web/sw.js")
GENERATED = ("scripts/render_nodeprobe.py", "scripts/render_rbac.py")


def current():
    source = (ROOT / "server" / "server.py").read_text(encoding="utf-8")
    return re.search(r'os\.environ\.get\("HOMESTEAD_VERSION", "(\d+\.\d+\.\d+)"\)', source).group(1)


def main(new):
    if not re.fullmatch(r"\d+\.\d+\.\d+", new):
        sys.exit("give the new version as MAJOR.MINOR.PATCH")
    old = current()
    pattern = re.compile(re.escape(old) + r"(?![\d])")
    for name in FILES:
        path = ROOT / name
        text = path.read_text(encoding="utf-8")
        path.write_text(pattern.sub(new, text), encoding="utf-8", newline="")
    for script in GENERATED:
        subprocess.run([sys.executable, str(ROOT / script)], check=True, stdout=subprocess.DEVNULL)
    print(f"{old} -> {new}")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "")
