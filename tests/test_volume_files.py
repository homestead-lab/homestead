import sys
import unittest
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))

import homestead_files as files


class PathSafetyTests(unittest.TestCase):
    """The volume is the whole world; nothing may address outside it."""

    def test_a_path_is_normalised_inside_the_volume(self):
        self.assertEqual("config/app.yaml", files.safe_path("/config/app.yaml"))
        self.assertEqual("config/app.yaml", files.safe_path("config//./app.yaml"))
        self.assertEqual("", files.safe_path("/"))
        self.assertEqual("", files.safe_path(""))

    def test_climbing_out_is_refused(self):
        for attempt in ("../etc/passwd", "config/../../etc", "/../..", "a/b/../../../x"):
            with self.subTest(attempt=attempt):
                with self.assertRaisesRegex(ValueError, "climb out"):
                    files.safe_path(attempt)

    def test_a_backslash_cannot_smuggle_a_traversal(self):
        with self.assertRaisesRegex(ValueError, "climb out"):
            files.safe_path("config\\..\\..\\etc")

    def test_an_absurd_path_is_refused(self):
        with self.assertRaisesRegex(ValueError, "too long"):
            files.safe_path("/" + "a" * 2000)


class SyntaxCheckTests(unittest.TestCase):
    """Warn about what is certainly broken; never block on taste."""

    def test_invalid_json_is_named_with_its_line(self):
        message = files.check_syntax("config.json", '{"a": 1,}')
        self.assertIn("JSON is invalid", message)
        self.assertIn("line 1", message)

    def test_valid_json_passes(self):
        self.assertEqual("", files.check_syntax("config.json", '{"a": [1, 2]}'))

    def test_yaml_indented_with_tabs_is_caught(self):
        message = files.check_syntax("config.yaml", "root:\n\tchild: 1\n")
        self.assertIn("tabs", message)
        self.assertIn("line 2", message)

    def test_a_tab_inside_a_value_is_not_a_problem(self):
        self.assertEqual("", files.check_syntax("config.yml", 'greeting: "a\tb"\n'))

    def test_an_unknown_extension_is_left_alone(self):
        self.assertEqual("", files.check_syntax("notes.txt", "{not json at all"))


class SessionPodTests(unittest.TestCase):
    def setUp(self):
        self.sent = []
        self.pods = {}

        def get(path):
            name = path.rsplit("/", 1)[-1]
            if name in self.pods:
                return self.pods[name]
            raise ValueError("not found")

        def send(method, path, body=None, **kw):
            self.sent.append((method, path, body))
            if method == "POST" and body:
                self.pods[body["metadata"]["name"]] = {
                    "metadata": {"name": body["metadata"]["name"]},
                    "status": {"phase": "Running"}}
            return body or {}

        files.bind(get, send, None, "", None, {"kube-system"})

    def test_the_helper_pod_mounts_the_claim_and_expires_itself(self):
        files.open_session("lab", "frigate-config")

        body = self.sent[-1][2]
        self.assertEqual("homestead-files-frigate-config", body["metadata"]["name"])
        spec = body["spec"]
        self.assertEqual("frigate-config",
                         spec["volumes"][0]["persistentVolumeClaim"]["claimName"])
        self.assertEqual("/data", spec["containers"][0]["volumeMounts"][0]["mountPath"])
        self.assertTrue(spec["activeDeadlineSeconds"] > 0, "a held RWO claim must not be forgotten")

    def test_a_running_session_is_reused_rather_than_recreated(self):
        files.open_session("lab", "frigate-config")
        self.sent.clear()

        files.open_session("lab", "frigate-config")

        self.assertEqual([], self.sent, "the pod already mounts the volume")

    def test_system_namespaces_are_refused(self):
        with self.assertRaises(PermissionError):
            files.open_session("kube-system", "anything")


if __name__ == "__main__":
    unittest.main()
