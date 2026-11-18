#!/usr/bin/env python3
"""New versions to validate, and their results recorded (issue #1348, 5.5).

    python3 scripts/validated_candidates.py find [--limit 6]
        Reads data/validated-versions.json and what is published upstream:
        Ubuntu 26.04's kernel images (the archive's Packages index for
        resolute, -updates and -security) and the stable GitHub releases of
        k3s, RKE2, Longhorn, KubeVirt and CDI. Prints, as JSON, the newest
        versions the list does not name yet, newest first, at most --limit:
        the matrix the daily workflow runs the harness on.

    python3 scripts/validated_candidates.py record RESULTS.json...
        Writes each harness result into data/validated-versions.json:
        validated when every check passed, else bad, with what failed, the
        features it affects and the run as evidence.
"""
import argparse
import gzip
import json
import pathlib
import re
import sys
import time
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parents[1]
DATA = ROOT / "data" / "validated-versions.json"
HOST_KEY = "ubuntu-26.04-amd64"
ARCHIVE = "http://archive.ubuntu.com/ubuntu/dists/{suite}/main/binary-amd64/Packages.gz"
SUITES = ("resolute", "resolute-updates", "resolute-security")
REPOS = {"k3s": "k3s-io/k3s", "rke2": "rancher/rke2", "longhorn": "longhorn/longhorn",
         "kubevirt": "kubevirt/kubevirt", "cdi": "kubevirt/containerized-data-importer"}
STABLE = re.compile(r"v\d+\.\d+\.\d+(\+(k3s|rke2r)\d+)?")
KERNEL = re.compile(r"linux-image-(\d+\.\d+\.\d+-\d+)-generic")


def _get(url, headers=None):
    request = urllib.request.Request(url, headers={"User-Agent": "homestead-validation", **(headers or {})})
    with urllib.request.urlopen(request, timeout=60) as answer:
        return answer.read()


def _numbers(version):
    return tuple(int(part) for part in re.findall(r"\d+", version))


def kernels(fetch=_get):
    """{package: version} of the generic kernel images Ubuntu 26.04 publishes."""
    found = {}
    for suite in SUITES:
        text = gzip.decompress(fetch(ARCHIVE.format(suite=suite))).decode("utf-8", "replace")
        for block in text.split("\n\n"):
            package = re.search(r"^Package: (\S+)$", block, re.M)
            version = re.search(r"^Version: (\S+)$", block, re.M)
            if package and version and KERNEL.fullmatch(package.group(1)):
                old = found.get(package.group(1))
                if not old or _numbers(version.group(1)) > _numbers(old):
                    found[package.group(1)] = version.group(1)
    return found


def releases(part, fetch=_get, token=""):
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    rows = json.loads(fetch(f"https://api.github.com/repos/{REPOS[part]}/releases?per_page=30", headers))
    return [row["tag_name"] for row in rows if not row.get("prerelease") and not row.get("draft")
            and STABLE.fullmatch(row.get("tag_name", ""))]


def find(limit=6, fetch=_get, token="", document=None):
    document = document or json.loads(DATA.read_text(encoding="utf-8"))
    named = {(e.get("package"), e["version"]) for e in (document.get("host") or {}).get(HOST_KEY, [])}
    out = []
    host = sorted(kernels(fetch).items(), key=lambda item: _numbers(item[1]), reverse=True)
    out += [{"kind": "host", "key": HOST_KEY, "package": package, "version": version, "kernel": f"{package}={version}",
             "title": f"kernel {version}"}
            for package, version in host[:2] if (package, version) not in named]
    for part in REPOS:
        listed = {e["version"] for e in (document.get("platform") or {}).get(part, [])}
        newest = sorted(releases(part, fetch, token), key=_numbers, reverse=True)[:1]
        out += [{"kind": "platform", "part": part, "version": v, "title": f"{part} {v}"} for v in newest if v not in listed]
    return out[:limit]


def record(paths, now=None):
    document = json.loads(DATA.read_text(encoding="utf-8"))
    changed = []
    for path in paths:
        result = json.loads(pathlib.Path(path).read_text(encoding="utf-8"))
        candidate = result.get("candidate") or {}
        failed = [c for c in result.get("checks", []) if not c.get("passed")]
        entry = {"version": candidate["version"], "status": "bad" if failed else "validated",
                 "tested": time.strftime("%Y-%m-%d", time.gmtime(now)), "evidence": result.get("run", ""),
                 "not_covered": ["gpu-passthrough", "real-disks"]}
        if failed:
            entry["affects"] = sorted({feature for check in failed for feature in check.get("affects", [])})
            entry["reason"] = "; ".join(f"{c['check']} failed" for c in failed)
        if candidate["kind"] == "host":
            entry["package"] = candidate["package"]
            rows = document.setdefault("host", {}).setdefault(candidate["key"], [])
            same = lambda e: e.get("package") == entry["package"] and e["version"] == entry["version"]
        else:
            rows = document.setdefault("platform", {}).setdefault(candidate["part"], [])
            same = lambda e: e["version"] == entry["version"]
        rows[:] = [e for e in rows if not same(e)] + [entry]
        changed.append(f"{candidate.get('title', entry['version'])}: {entry['status']}")
    document["generated"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now))
    DATA.write_bytes((json.dumps(document, indent=1, sort_keys=True) + "\n").encode("utf-8"))
    return changed


def main(argv):
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    found = sub.add_parser("find")
    found.add_argument("--limit", type=int, default=6)
    found.add_argument("--token", default="")
    recorded = sub.add_parser("record")
    recorded.add_argument("results", nargs="+")
    args = parser.parse_args(argv)
    if args.command == "find":
        print(json.dumps(find(args.limit, token=args.token)))
    else:
        print("\n".join(record(args.results)))


if __name__ == "__main__":
    main(sys.argv[1:])
