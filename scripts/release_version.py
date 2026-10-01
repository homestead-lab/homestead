"""Validate a release tag, its source version and its publishing branch."""
import re
import subprocess
import sys

from bump_version import current


def resolve(tag, source):
    if not re.fullmatch(r"v\d+\.\d+\.\d+(?:-dev\.[1-9]\d*)?", tag):
        raise ValueError("tag must be vMAJOR.MINOR.PATCH or vMAJOR.MINOR.PATCH-dev.N")
    version = tag[1:]
    if version != source:
        raise ValueError(f"tag version {version} does not match source version {source}; run bump_version.py first")
    return version, "dev" if "-dev." in version else "prod"


if __name__ == "__main__":
    version, channel = resolve(sys.argv[1], current())
    branch = "dev" if channel == "dev" else "main"
    subprocess.run(["git", "merge-base", "--is-ancestor", "HEAD", f"origin/{branch}"], check=True)
    print(f"version={version}\nchannel={channel}")
