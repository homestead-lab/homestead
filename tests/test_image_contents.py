"""What the Dockerfile copies has to survive .dockerignore.

A path excluded there fails the build rather than the tests, so it only
shows up in CI after a tag has already been pushed.
"""
import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def ignore_rules():
    rules = []
    for line in (ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            rules.append(line)
    return rules


def excluded(path, rules):
    """Docker's last-match-wins, for the plain prefix patterns this repo uses."""
    verdict = False
    for rule in rules:
        negate = rule.startswith("!")
        pattern = rule.lstrip("!")
        if path == pattern or path.startswith(pattern.rstrip("/") + "/"):
            verdict = not negate
    return verdict


class ImageContentTests(unittest.TestCase):
    def test_every_copied_path_reaches_the_build(self):
        rules = ignore_rules()
        dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
        sources = []
        for line in dockerfile.splitlines():
            match = re.match(r"COPY (?:--\S+ )*(.+)", line.strip())
            if not match:
                continue
            parts = match.group(1).split()
            sources.extend(parts[:-1])          # the last argument is the destination
        self.assertTrue(sources, "the Dockerfile copies nothing?")
        for source in sources:
            with self.subTest(source=source):
                self.assertFalse(excluded(source, rules),
                                 f"{source} is copied by the Dockerfile but "
                                 ".dockerignore keeps it out of the build")

    def test_the_probe_scripts_are_in_the_image(self):
        """Without them an upgrade cannot carry the probe forward, and the
        install button has nothing to install."""
        dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")

        self.assertIn("server/probe/*.py", dockerfile)
        self.assertFalse(excluded("server/probe/*.py", ignore_rules()))
        self.assertTrue((ROOT / "server" / "probe" / "probe.py").exists())
        self.assertTrue((ROOT / "server" / "probe" / "smart.py").exists())


if __name__ == "__main__":
    unittest.main()
