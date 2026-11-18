"""#731 item 3: deploy/deploy.yaml asks for Longhorn's own class, `longhorn`,
and the installers that rewrite it (bootstrap-k3s.sh, install.sh's Harvester
branch) rewrite exactly the claim's class and STORAGE_CLASS - both in the
current manifest and in older release manifests that still say longhorn-r2,
which a pinned version (HS_RELEASE, --homestead-version) downloads."""
import re
import shutil
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "deploy" / "deploy.yaml"
SH = shutil.which("dash") or shutil.which("sh")
SED = shutil.which("sed")


def claim_class(text):
    """The homestead-data claim's storageClassName in a manifest's text."""
    for doc in re.split(r"(?m)^---\s*$", text):
        if re.search(r"(?m)^kind: PersistentVolumeClaim$", doc) and re.search(r"(?m)^  name: homestead-data$", doc):
            return re.search(r"(?m)^  storageClassName: (.*)$", doc).group(1)
    raise AssertionError("no homestead-data PersistentVolumeClaim")


def env_class(text):
    """The Deployment's STORAGE_CLASS value in a manifest's text."""
    return re.search(r'(?m)^ *- name: STORAGE_CLASS\n *value: "([^"]*)"$', text).group(1)


def old_manifest(text):
    """The same manifest as an older release wrote it: longhorn-r2 in both fields."""
    text = re.sub(r"(?m)^(  storageClassName: )longhorn$", r"\1longhorn-r2", text)
    return re.sub(r'(?m)^( *- name: STORAGE_CLASS\n *value: )"longhorn"$', r'\1"longhorn-r2"', text)


def class_expressions(script, variable, target):
    """The sed expressions the script passes for the class, as its shell would
    expand them with `variable` set to `target`."""
    text = (ROOT / "scripts" / script).read_text(encoding="utf-8")
    words = re.findall(r'-e ("s/(?:storageClassName|value): (?:[^"\\]|\\.)*")', text)
    if len(words) != 2:
        raise AssertionError(f"{script}: expected the two class expressions, found {words}")
    # bootstrap-k3s.sh gives Homestead's own claim its own class (DATA_CLASS:
    # local-path on k3s) and apps $CLASS; here both are the target.
    out = subprocess.run([SH, "-c", f'{variable}={target}; DATA_CLASS={target}; for a in {" ".join(words)}; do printf "%s\\n" "$a"; done'],
                         capture_output=True, text=True, check=True).stdout
    return out.splitlines()


@unittest.skipUnless(SH and SED, "no POSIX shell or sed here")
class ManifestClassTests(unittest.TestCase):
    def test_manifest_asks_for_longhorn(self):
        text = MANIFEST.read_text(encoding="utf-8")
        self.assertEqual("longhorn", claim_class(text))
        self.assertEqual("longhorn", env_class(text))

    def test_installer_rewrite_handles_new_and_old_manifests(self):
        current = MANIFEST.read_text(encoding="utf-8")
        for script, variable in (("bootstrap-k3s.sh", "CLASS"), ("install.sh", "class")):
            for target in ("local-path", "harvester-longhorn", "longhorn"):
                expressions = class_expressions(script, variable, target)
                for label, text in (("current", current), ("old", old_manifest(current))):
                    with self.subTest(script=script, target=target, manifest=label):
                        args = [SED]
                        for e in expressions:
                            args += ["-e", e]
                        out = subprocess.run(args, input=text, capture_output=True, text=True, check=True).stdout
                        self.assertEqual(target, claim_class(out))
                        self.assertEqual(target, env_class(out))
                        for kept in ("longhorn.io", "longhorn-system"):
                            self.assertEqual(text.count(kept), out.count(kept), kept)
                        before, after = text.splitlines(), out.splitlines()
                        self.assertEqual(len(before), len(after))
                        changed = [b for b, a in zip(before, after) if b != a]
                        expected = 0 if target in (claim_class(text),) else 2
                        self.assertEqual(expected, len(changed), changed)

    def test_bootstrap_puts_homesteads_claim_on_data_class_and_apps_on_class(self):
        text = (ROOT / "scripts" / "bootstrap-k3s.sh").read_text(encoding="utf-8")
        self.assertIn('storageClassName: $DATA_CLASS/"', text)
        self.assertIn('value: \\"$CLASS\\"/"', text)
        self.assertIn('[ "$DIST" = k3s ] && DATA_CLASS=local-path', text)

    def test_old_manifest_helper_really_says_longhorn_r2(self):
        old = old_manifest(MANIFEST.read_text(encoding="utf-8"))
        self.assertEqual("longhorn-r2", claim_class(old))
        self.assertEqual("longhorn-r2", env_class(old))

    def test_harvester_wiki_one_liner_uses_the_same_expressions(self):
        wiki = (ROOT / "docs" / "wiki" / "Installing-on-Harvester.md").read_text(encoding="utf-8")
        for e in class_expressions("install.sh", "class", "harvester-longhorn"):
            self.assertIn(f"-e '{e}'", wiki)


if __name__ == "__main__":
    unittest.main()
