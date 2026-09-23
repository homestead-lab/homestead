"""The names Homestead gives its objects, and the keys it writes on them."""
import re
import sys
import unittest
import urllib.error
import urllib.parse
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
import homestead_names as names

ROOT = Path(__file__).resolve().parents[1]


class NameTests(unittest.TestCase):
    def test_objects_are_named_homestead(self):
        self.assertEqual("homestead-settings", names.object_name("settings", "lab"))
        self.assertEqual("homestead-nodeprobe", names.NODEPROBE)

    def test_keys_are_in_the_homestead_domain(self):
        self.assertEqual("homestead.io/task", names.key("task"))
        self.assertEqual({"homestead.io/task": "import", "homestead.io/app": "frigate"},
                         names.labels("import", app="frigate"))
        self.assertEqual("1", names.read({"homestead.io/replicas": "1"}, "replicas"))
        self.assertEqual("", names.read({}, "replicas"))

    def test_writing_and_clearing_an_annotation(self):
        meta = {"annotations": {"keep": "me"}}
        names.write_annotation(meta, "icon", "new")
        self.assertEqual({"homestead.io/icon": "new", "keep": "me"}, meta["annotations"])
        names.write_annotation(meta, "icon", None)
        self.assertEqual({"keep": "me"}, meta["annotations"])


class LabelSearchTests(unittest.TestCase):
    def test_objects_are_found_by_their_label(self):
        asked = []

        def get(path):
            asked.append(urllib.parse.unquote(path.split("labelSelector=", 1)[-1]))
            return {"items": [{"metadata": {"name": "job"}}]}

        names.bind(get)
        found = names.find("/apis/batch/v1/namespaces/lab/jobs", "task", "import")

        self.assertEqual(["homestead.io/task=import"], asked)
        self.assertEqual(["job"], [item["metadata"]["name"] for item in found])

    def test_a_cluster_that_will_not_answer_is_not_an_error(self):
        def refuse(_path):
            raise urllib.error.URLError("no route to host")

        names.bind(refuse)
        self.assertEqual([], names.find("/apis/batch/v1/namespaces/lab/jobs", "task"))


class NoOldNameTests(unittest.TestCase):
    """Homestead was once harvUI. Only the one-time key rename may still say so."""

    def test_nothing_else_mentions_the_old_name(self):
        offenders = []
        paths = [*(ROOT / "server").glob("*.py"), *(ROOT / "web" / "js").glob("*.js"), ROOT / "web" / "index.html",
                 ROOT / "web" / "sw.js", *(ROOT / "deploy").glob("*.yaml"), *(ROOT / "scripts").glob("*")]
        for path in sorted(paths):
            if path.name == "homestead_self.py" or not path.is_file():
                continue
            for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                if re.search(r"harv(ui|UI)", line):
                    offenders.append(f"{path.name}:{number}")
        self.assertEqual([], offenders)

    def test_the_browser_globals_carry_the_current_name(self):
        for name, wanted in (("router.js", "HomesteadRouter"),
                             ("update-state.js", "HomesteadUpdateState")):
            with self.subTest(file=name):
                source = (ROOT / "web" / "js" / name).read_text(encoding="utf-8")
                self.assertIn(wanted, source)
                self.assertNotIn("Harv", source.replace("Harvester", ""))


if __name__ == "__main__":
    unittest.main()
