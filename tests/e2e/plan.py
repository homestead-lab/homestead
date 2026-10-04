"""Which release tests to run, written as GitHub Actions outputs.

A published release runs them by itself when it is x.y.0 - the large ones -
and on demand, any published version and any suites and distributions."""
import json
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from suites import SUITES  # noqa: E402

DISTROS = ("k3s", "rke2")


def newest_release():
    import subprocess
    tags = subprocess.run(["git", "tag", "-l", "v*", "--sort=-v:refname"], capture_output=True, text=True).stdout.split()
    return next((t.lstrip("v") for t in tags if re.fullmatch(r"v\d+\.\d+\.\d+(?:-dev\.\d+)?", t)), "")


def plan(tag="", version="", suites="", distros="", branch=""):
    if branch:
        # e2e/k3s-single, e2e/rke2-all, e2e/all: that much, on the newest release.
        what = branch.split("/", 1)[1] if "/" in branch else ""
        distro, _, suite = what.partition("-") if what.split("-", 1)[0] in DISTROS else ("", "", what)
        distros, suites = distro or ",".join(DISTROS), suite or "all"
        version = version or newest_release()
    if tag:
        version = tag.lstrip("v")
        run = bool(re.fullmatch(r"\d+\.\d+\.0", version))
    else:
        version = version.strip().lstrip("v")
        run = bool(re.fullmatch(r"\d+\.\d+\.\d+(?:-dev\.\d+)?", version))
    chosen = sorted(SUITES) if suites.strip() in ("", "all") else [s.strip() for s in suites.split(",") if s.strip()]
    unknown = [s for s in chosen if s not in SUITES]
    if unknown:
        raise SystemExit(f"unknown suites: {', '.join(unknown)} (have {', '.join(sorted(SUITES))})")
    dists = [d.strip() for d in (distros or ",".join(DISTROS)).split(",") if d.strip()]
    if any(d not in DISTROS for d in dists):
        raise SystemExit(f"distributions are {', '.join(DISTROS)}")
    matrix = {"include": [{"distro": d, "suite": s} for d in dists for s in chosen]}
    return {"run": "true" if run else "false", "version": version, "matrix": json.dumps(matrix)}


if __name__ == "__main__":
    out = plan(os.environ.get("TAG", ""), os.environ.get("VERSION", ""), os.environ.get("SUITES", ""), os.environ.get("DISTROS", ""),
               os.environ.get("BRANCH", ""))
    for key, value in out.items():
        print(f"{key}={value}")
