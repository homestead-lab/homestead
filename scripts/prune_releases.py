#!/usr/bin/env python3
"""Keep the GitHub releases page short, run after each release is published.

Kept: the newest 5 releases (patches), the newest release of each of the 3
newest minor lines (features), and the newest of each of the 2 newest major
lines - plus the one just published and the one marked Latest. Drafts,
pre-releases and anything not named vX.Y.Z are left alone.

Only the release pages go. Their git tags stay: an installed Homestead
fetches its permissions file (deploy/rbac.yaml) by its own version's tag, and
the images stay in the registry, so any version can still be installed.

    python scripts/prune_releases.py            # lists what would go
    python scripts/prune_releases.py --apply    # deletes those release pages
"""
import argparse
import json
import re
import subprocess
import sys

PATCHES, FEATURES, MAJORS = 5, 3, 2
TAG = re.compile(r"v(\d+)\.(\d+)\.(\d+)")


def keep(releases, patches=PATCHES, features=FEATURES, majors=MAJORS, also=()):
    """The tags to keep, from rows of {tagName, isLatest, isDraft, isPrerelease}."""
    versions = {}
    for row in releases:
        match = TAG.fullmatch(row.get("tagName") or "")
        if match and not row.get("isDraft") and not row.get("isPrerelease"):
            versions[row["tagName"]] = tuple(int(x) for x in match.groups())
    newest = sorted(versions, key=versions.get, reverse=True)
    kept = set(newest[:patches])
    for width, count in ((2, features), (1, majors)):
        lines = []
        for tag in newest:              # newest first: a line's first tag is its newest
            line = versions[tag][:width]
            if line not in lines:
                lines.append(line)
                if len(lines) <= count:
                    kept.add(tag)
    kept |= {row["tagName"] for row in releases if row.get("isLatest")}
    kept |= set(also)
    # Anything not a plain vX.Y.Z release is not this script's to remove.
    kept |= {row["tagName"] for row in releases if row["tagName"] not in versions}
    return kept


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--apply", action="store_true", help="delete the release pages (the default only lists them)")
    parser.add_argument("--keep", action="append", default=[], help="a tag to keep as well (the one just published)")
    args = parser.parse_args()
    rows = json.loads(subprocess.run(
        ["gh", "release", "list", "--limit", "1000", "--json", "tagName,isLatest,isDraft,isPrerelease"],
        check=True, capture_output=True, text=True).stdout)
    kept = keep(rows, also=args.keep)
    gone = sorted((r["tagName"] for r in rows if r["tagName"] not in kept),
                  key=lambda t: tuple(int(x) for x in TAG.fullmatch(t).groups()))
    print(f"keeping {len(kept)}: {', '.join(sorted(kept, key=lambda t: (TAG.fullmatch(t) is None, t)))}")
    print(f"{'removing' if args.apply else 'would remove'} {len(gone)} release page(s), keeping their tags"
          + (f": {', '.join(gone)}" if gone else ""))
    failed = 0
    for tag in gone if args.apply else []:
        # No --cleanup-tag: the tag stays.
        result = subprocess.run(["gh", "release", "delete", tag, "--yes"], capture_output=True, text=True)
        if result.returncode:
            failed += 1
            print(f"could not remove {tag}: {result.stderr.strip()[:200]}", file=sys.stderr)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
