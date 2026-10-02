import copy
from contextlib import nullcontext
import sys
import unittest
from pathlib import Path
from unittest.mock import patch


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


class CleanupTests(unittest.TestCase):
    def setUp(self):
        self.sent = []
        self.pod = {
            "metadata": {"name": "homestead-files-data", "namespace": "lab", "uid": "old-pod",
                         "resourceVersion": "17", "creationTimestamp": "2026-10-02T00:00:00Z",
                         "labels": {"homestead.io/task": "files"}},
            "spec": {"activeDeadlineSeconds": 1800,
                     "volumes": [{"name": "data", "persistentVolumeClaim": {"claimName": "data"}}]},
            "status": {"phase": "Running"}}
        self.pods = [self.pod]
        files.bind(lambda path: {"items": self.pods},
                   lambda method, path, body: self.sent.append((method, path, body)),
                   None, "", None, {"kube-system"})
        self.created = 1790899200  # 2026-10-02 00:00 UTC

    def test_running_helpers_are_kept_until_their_deadline(self):
        self.assertEqual([], files.cleanup(self.created + 1799))
        self.assertEqual([], self.sent)
        self.assertEqual(["homestead-files-data"], files.cleanup(self.created + 1800))
        method, path, body = self.sent[0]
        self.assertEqual("DELETE", method)
        self.assertEqual("/api/v1/namespaces/lab/pods/homestead-files-data", path)
        self.assertEqual({"uid": "old-pod", "resourceVersion": "17"}, body["preconditions"])
        self.assertEqual(0, body["gracePeriodSeconds"])

    def test_finished_and_failed_helpers_are_removed_before_the_deadline(self):
        for phase in ("Succeeded", "Failed"):
            self.pod["status"]["phase"] = phase
            self.assertEqual(["homestead-files-data"], files.cleanup(self.created + 10))

    def test_an_unscheduled_helper_also_expires(self):
        self.pod["status"] = {"phase": "Pending"}
        self.assertEqual(["homestead-files-data"], files.cleanup(self.created + 1801))

    def test_only_identifiable_managed_helpers_are_deleted(self):
        mutations = [
            ("metadata", "labels", {}), ("metadata", "uid", ""),
            ("metadata", "name", "homestead-snapshot-files-data"),
            ("metadata", "namespace", "kube-system"),
            ("metadata", "creationTimestamp", "bad timestamp"),
            ("metadata", "deletionTimestamp", "already closing"),
            ("spec", "volumes", [{"name": "data", "persistentVolumeClaim": {"claimName": "other"}}])]
        for section, key, value in mutations:
            with self.subTest(key=key):
                pod = copy.deepcopy(self.pod)
                pod[section][key] = value
                self.pods = [pod]
                self.assertEqual([], files.cleanup(self.created + 1900))
        self.assertEqual([], self.sent)

    def test_api_or_delete_failures_are_reported_for_a_later_retry(self):
        with patch.object(files, "kget", side_effect=ConnectionError("unavailable")):
            with self.assertRaises(ConnectionError):
                files.cleanup(self.created + 1900)
        with patch.object(files, "ksend", side_effect=ValueError("replacement UID conflict")):
            with self.assertRaisesRegex(ValueError, "UID conflict"):
                files.cleanup(self.created + 1900)


class CleanupLoopTests(unittest.TestCase):
    def test_sweep_runs_only_on_the_writable_leader(self):
        import server

        class StopLoop(BaseException):
            pass

        for leader, writable in ((False, True), (True, True), (True, False)):
            with self.subTest(leader=leader, writable=writable), \
                    patch.object(server.LEADER, "is_leader", return_value=leader), \
                    patch.object(server, "self_data_activity", side_effect=lambda: nullcontext()), \
                    patch.object(server, "require_self_data_write", side_effect=None if writable else RuntimeError("held")), \
                    patch.object(server.FILES, "cleanup") as cleanup, \
                    patch.object(server, "beat") as beat, \
                    patch.object(server.time, "sleep", side_effect=StopLoop):
                with self.assertRaises(StopLoop):
                    server._files_loop()
                self.assertEqual(int(leader and writable), cleanup.call_count)
                self.assertEqual(int(leader), beat.call_count)


if __name__ == "__main__":
    unittest.main()
