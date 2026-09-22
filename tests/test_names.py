"""Homestead was harvUI. Both names have to work, in the right directions."""
import sys
import unittest
import urllib.error
import urllib.parse
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
import homestead_names as names


class NameTests(unittest.TestCase):
    def setUp(self):
        self.existing = set()
        names.bind(self._get)

    def _get(self, path):
        name = path.rsplit("/", 1)[-1].split("?")[0]
        if name not in self.existing:
            raise urllib.error.HTTPError(path, 404, "missing", {}, None)
        return {"metadata": {"name": name}}

    def test_a_new_install_is_given_the_new_name(self):
        self.assertEqual("homestead-settings", names.object_name("settings", "lab"))

    def test_an_install_that_already_has_the_old_object_keeps_using_it(self):
        """Writing to the new name instead would fork the install's state into
        two objects, one of them silently ignored."""
        self.existing.add("harvui-settings")

        self.assertEqual("harvui-settings", names.object_name("settings", "lab"))

    def test_the_new_name_wins_once_both_somehow_exist(self):
        self.existing.update({"harvui-settings", "homestead-settings"})

        self.assertEqual("homestead-settings", names.object_name("settings", "lab"))

    def test_an_annotation_is_read_under_either_domain(self):
        self.assertEqual("1", names.read({"homestead.io/replicas": "1"}, "replicas"))
        self.assertEqual("2", names.read({"harvui.io/replicas": "2"}, "replicas"))
        self.assertEqual("", names.read({}, "replicas"))

    def test_the_new_domain_answers_first(self):
        both = {"homestead.io/icon": "new", "harvui.io/icon": "old"}

        self.assertEqual("new", names.read(both, "icon"))

    def test_writing_an_annotation_clears_the_old_key(self):
        meta = {"annotations": {"harvui.io/icon": "old", "keep": "me"}}

        names.write_annotation(meta, "icon", "new")

        self.assertEqual({"homestead.io/icon": "new", "keep": "me"}, meta["annotations"])

    def test_only_the_new_domain_is_ever_written(self):
        self.assertEqual("homestead.io/task", names.key("task"))
        self.assertEqual({"homestead.io/task": "import", "homestead.io/app": "frigate"},
                         names.labels("import", app="frigate"))


class LabelSearchTests(unittest.TestCase):
    """A label selector cannot say "or", so both domains are asked."""

    def setUp(self):
        self.answers = {}
        def get(path):
            wanted = urllib.parse.unquote(path.split("labelSelector=", 1)[-1])
            return {"items": self.answers.get(wanted.split("=", 1)[-1], [])}

        names.bind(get)

    def test_objects_under_either_domain_come_back_once(self):
        shared = {"metadata": {"uid": "u1", "name": "both"}}
        self.answers["import"] = [shared, {"metadata": {"uid": "u2", "name": "new"}}]

        found = names.find("/apis/batch/v1/namespaces/lab/jobs", "task", "import")

        self.assertEqual(["both", "new"], [item["metadata"]["name"] for item in found])

    def test_a_cluster_that_will_not_answer_is_not_an_error(self):
        def refuse(_path):
            raise urllib.error.URLError("no route to host")

        names.bind(refuse)

        self.assertEqual([], names.find("/apis/batch/v1/namespaces/lab/jobs", "task"))


if __name__ == "__main__":
    unittest.main()
