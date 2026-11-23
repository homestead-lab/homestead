"""CI's built-image steps run some test files against the image's copy of the server
code: they set HOMESTEAD_TEST_SERVER_DIR=/srv and pick the files with -p. Each file a
step picks must put that folder first on the path, or it tests the repo's copy instead.

Limits, read by text: a step is what follows a "- name:" line in ci.yml; a step that
names the variable must pick its files with -p '...' or -p "..." (one without fails
this test, it is not skipped); the variable set in a job-level env: is not seen. A file
fails when it never reads the variable, or when one line inserts a path ending in a
"server" or 'server' literal without naming it; a repo path built over several lines
is not seen."""
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HONOURS = re.compile(r"""os\.environ\.get\(\s*["']HOMESTEAD_TEST_SERVER_DIR["']""")
REPO_SERVER = re.compile(r"""sys\.path\.insert\(.*["']server["']""")


def image_run_patterns(text):
    """(patterns, steps without one): the -p patterns of the ci.yml steps that name
    HOMESTEAD_TEST_SERVER_DIR, and the first line of each such step with no -p."""
    patterns, without = [], []
    for step in re.split(r"\n\s*- name:", text):
        if "HOMESTEAD_TEST_SERVER_DIR" in step:
            found = re.findall(r"""-p\s+(['"])([^'"]+)\1""", step)
            patterns += [pattern for _, pattern in found]
            if not found:
                without.append(step.strip().splitlines()[0])
    return patterns, without


def ignores_the_variable(path):
    """Why a test file would test the repo's server code in an image run, or None."""
    text = path.read_text()
    if not HONOURS.search(text):
        return "never reads HOMESTEAD_TEST_SERVER_DIR"
    for line in text.splitlines():
        if REPO_SERVER.search(line) and "HOMESTEAD_TEST_SERVER_DIR" not in line:
            return "puts the repo's server/ on the path: " + line.strip()
    return None


class ImageRunServerDirTests(unittest.TestCase):
    def ci(self):
        return image_run_patterns((ROOT / ".github" / "workflows" / "ci.yml").read_text())

    def test_the_image_steps_are_found_and_each_names_its_files(self):
        patterns, without = self.ci()
        self.assertIn("test_self_data*.py", patterns)
        self.assertEqual([], without)

    def test_every_file_an_image_step_runs_honours_the_variable(self):
        files = sorted({path for pattern in self.ci()[0] for path in (ROOT / "tests").glob(pattern)})
        self.assertTrue(files)
        wrong = {path.name: why for path in files if (why := ignores_the_variable(path))}
        self.assertEqual({}, wrong)

    def test_the_parser_reads_both_quotes_and_names_a_step_without_a_pattern(self):
        text = ("jobs:\n  steps:\n"
                "      - name: Single\n        run: x --env HOMESTEAD_TEST_SERVER_DIR=/srv -p 'test_a*.py'\n"
                '      - name: Double\n        run: x --env HOMESTEAD_TEST_SERVER_DIR=/srv -p "test_b.py"\n'
                "      - name: No pattern\n        run: x --env HOMESTEAD_TEST_SERVER_DIR=/srv discover\n"
                "      - name: Unrelated\n        run: x -p 'test_c.py'\n")
        self.assertEqual((["test_a*.py", "test_b.py"], ["No pattern"]), image_run_patterns(text))

    def test_a_file_that_puts_the_repo_copy_first_is_found(self):
        import tempfile
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "test_x.py"
            path.write_text('sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))\n')
            self.assertEqual("never reads HOMESTEAD_TEST_SERVER_DIR", ignores_the_variable(path))
            for quote in ('"', "'"):
                with self.subTest(quote=quote):
                    path.write_text(f'X = os.environ.get({quote}HOMESTEAD_TEST_SERVER_DIR{quote}, "")\n'
                                    f'sys.path.insert(0, str(ROOT / {quote}server{quote}))\n')
                    self.assertIn("repo's server/", ignores_the_variable(path))
            path.write_text('sys.path.insert(0, os.environ.get("HOMESTEAD_TEST_SERVER_DIR", str(ROOT / "server")))\n')
            self.assertIsNone(ignores_the_variable(path))


if __name__ == "__main__":
    unittest.main()
