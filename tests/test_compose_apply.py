"""Creating a Compose file's services: in order, all checked, stopping on trouble."""
import sys
import unittest
from pathlib import Path
from unittest import mock


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
import server

STACK = """services:
  web:
    image: web
    ports: ["8080:80"]
    depends_on: [db]
    environment:
      DB_HOST: db
  db:
    image: postgres:16
"""


class ComposeApplyTests(unittest.TestCase):
    def setUp(self):
        self.created = []
        get = mock.patch.object(server, "kget", return_value={"items": []})
        features = mock.patch.object(server.HW, "features", return_value=[])
        deploy = mock.patch.object(server, "run_deploy", side_effect=self._deploy)
        for patcher in (get, features, deploy):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.fail_on = None

    def _deploy(self, cfg):
        if cfg["workload_name"] == self.fail_on:
            raise ValueError("the cluster said no")
        self.created.append(cfg["workload_name"])
        return {"ok": True, "name": cfg["workload_name"]}

    def test_dependencies_are_created_first(self):
        result = server.compose_apply({"text": STACK, "namespace": "lab"})

        self.assertEqual({"ok": True, "created": ["db", "web"]}, result)
        self.assertEqual(["db", "web"], self.created)

    def test_it_stops_at_the_first_failure_and_says_what_was_made(self):
        self.fail_on = "web"

        result = server.compose_apply({"text": STACK, "namespace": "lab"})

        self.assertEqual((False, ["db"], "web"), (result["ok"], result["created"], result["failed"]))
        self.assertIn("said no", result["error"])

    def test_a_file_with_errors_creates_nothing(self):
        with self.assertRaisesRegex(ValueError, "fix the file first"):
            server.compose_apply({"text": "services:\n  a:\n    build: .\n", "namespace": "lab"})
        self.assertEqual([], self.created)

    def test_only_the_chosen_services(self):
        result = server.compose_apply({"text": STACK, "namespace": "lab", "services": ["db"]})

        self.assertEqual(["db"], result["created"])

    def test_a_namespace_must_be_a_name(self):
        with self.assertRaisesRegex(ValueError, "namespace"):
            server.compose_report({"text": STACK, "namespace": "../etc"})

    def test_what_was_checked_is_what_is_created(self):
        """The page's copy of the report is never trusted; the text is read again."""
        server.compose_apply({"text": STACK, "namespace": "lab",
                              "report": {"services": [{"name": "evil", "config": {"image": "x"}}]}})

        self.assertNotIn("evil", self.created)


if __name__ == "__main__":
    unittest.main()
