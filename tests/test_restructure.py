import copy
import subprocess
import sys
import unittest
import urllib.error
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "server"))

import homestead_restructure as RESTRUCTURE
from test_container_storage import WorkloadEditFixture


def row(path, source, sub_path="", kind="existing", claim="", folder=""):
    """A mount now at source/sub_path; claim/folder is where it was."""
    out = {"path": path, "kind": kind, "source": source, "sub_path": sub_path}
    if claim:
        out["copy_from"] = {"claim": claim, "sub_path": folder}
    return out


def edit(*rows):
    return {"ns": "lab", "name": "app", "containers": [{"name": "app", "volumes": list(rows)}]}


class CopiesTests(unittest.TestCase):
    def test_combining_two_volumes_into_folders_of_one(self):
        moves = RESTRUCTURE.copies(edit(
            row("/config", "app-appdata", "config", kind="new-rwo", claim="app-config"),
            row("/data", "app-appdata", "data", kind="new-rwo", claim="app-data"),
        ))
        self.assertEqual([("app-config", "", "app-appdata", "config"), ("app-data", "", "app-appdata", "data")],
                         [(m["from"], m["from_folder"], m["to"], m["to_folder"]) for m in moves])

    def test_splitting_a_folder_out_to_a_volume_of_its_own(self):
        moves = RESTRUCTURE.copies(edit(row("/data", "app-data", kind="new-rwx", claim="app-appdata", folder="data")))
        self.assertEqual(("app-appdata", "data", "app-data", ""),
                         (moves[0]["from"], moves[0]["from_folder"], moves[0]["to"], moves[0]["to_folder"]))

    def test_an_unmoved_mount_or_one_without_the_option_copies_nothing(self):
        self.assertEqual([], RESTRUCTURE.copies(edit(
            row("/config", "app-appdata", "config", claim="app-appdata", folder="/config/"),
            row("/data", "elsewhere"),
        )))

    def test_a_whole_volume_can_become_a_folder_of_itself(self):
        moves = RESTRUCTURE.copies(edit(row("/config", "app", "config", claim="app")))
        self.assertEqual(("app", "", "app", "config"),
                         (moves[0]["from"], moves[0]["from_folder"], moves[0]["to"], moves[0]["to_folder"]))

    def test_two_sources_cannot_land_in_one_place(self):
        with self.assertRaisesRegex(ValueError, "two paths"):
            RESTRUCTURE.copies(edit(row("/a", "new", "x", claim="one"), row("/b", "new", "x", claim="two")))

    def test_folder_names_and_targets_are_checked(self):
        with self.assertRaises(ValueError):
            RESTRUCTURE.copies(edit(row("/a", "new", "../escape", claim="one")))
        with self.assertRaises(ValueError):
            RESTRUCTURE.copies(edit(row("/a", "/mnt/disk", kind="host", claim="one")))


class ScriptTests(unittest.TestCase):
    def test_the_copy_script_runs_and_keeps_what_it_moves(self):
        moves = [{"path": "/config", "from": "old", "from_folder": "", "to": "new", "to_folder": "my config"},
                 {"path": "/db", "from": "old2", "from_folder": "never", "to": "new", "to_folder": "db"}]
        with __import__("tempfile").TemporaryDirectory() as tmp:
            base = Path(tmp)
            (base / "old" / "sub").mkdir(parents=True)
            (base / "old" / "sub" / "a.txt").write_text("hello")
            (base / "old" / ".hidden").write_text("dot")
            (base / "new").mkdir()
            (base / "old2").mkdir()
            mount_of = {name: (base / name).as_posix() for name in ("old", "old2", "new")}
            try:
                result = subprocess.run(["sh", "-c", RESTRUCTURE.script(moves, mount_of)],
                                        capture_output=True, text=True, timeout=30)
            except FileNotFoundError:
                self.skipTest("no sh here")
            self.assertEqual(0, result.returncode, result.stderr)
            self.assertEqual("hello", (base / "new" / "my config" / "sub" / "a.txt").read_text())
            self.assertEqual("dot", (base / "new" / "my config" / ".hidden").read_text())
            self.assertIn("skipped", result.stdout)
            self.assertTrue((base / "old" / "sub" / "a.txt").exists())

    def test_a_volume_moved_into_a_folder_of_itself_skips_that_folder(self):
        moves = [{"path": "/config", "from": "app", "from_folder": "", "to": "app", "to_folder": "config/app"}]
        with __import__("tempfile").TemporaryDirectory() as tmp:
            base = Path(tmp)
            (base / "app" / "db").mkdir(parents=True)
            (base / "app" / "db" / "x").write_text("x")
            (base / "app" / ".env").write_text("e")
            try:
                result = subprocess.run(["sh", "-c", RESTRUCTURE.script(moves, {"app": (base / "app").as_posix()})],
                                        capture_output=True, text=True, timeout=30)
            except FileNotFoundError:
                self.skipTest("no sh here")
            self.assertEqual(0, result.returncode, result.stderr)
            self.assertEqual("x", (base / "app" / "config" / "app" / "db" / "x").read_text())
            self.assertEqual("e", (base / "app" / "config" / "app" / ".env").read_text())
            self.assertFalse((base / "app" / "config" / "app" / "config").exists())

    def test_the_job_mounts_each_volume_once(self):
        name, body = RESTRUCTURE.job("lab", "app", [
            {"path": "/a", "from": "one", "from_folder": "", "to": "all", "to_folder": "a"},
            {"path": "/b", "from": "two", "from_folder": "", "to": "all", "to_folder": "b"}])
        spec = body["spec"]["template"]["spec"]
        self.assertEqual(["all", "one", "two"], [v["persistentVolumeClaim"]["claimName"] for v in spec["volumes"]])
        self.assertTrue(name.startswith("app-restructure-"))
        self.assertLessEqual(len(name), 63)


class ResolveTests(unittest.TestCase):
    def setUp(self):
        self.deployment = {"metadata": {"name": "app", "annotations": {RESTRUCTURE.HELD: "2"}},
                           "spec": {"replicas": 0, "selector": {"matchLabels": {"app": "app"}}}}
        self.pods, self.jobs, self.sent = [{"metadata": {"name": "app-1"}}], {}, []

        def get(path):
            if path.endswith("/deployments/app"):
                return copy.deepcopy(self.deployment)
            if "/pods?" in path:
                return {"items": self.pods}
            if "/jobs/" in path:
                name = path.rsplit("/", 1)[-1]
                if name not in self.jobs:
                    raise urllib.error.HTTPError(path, 404, "gone", None, None)
                return self.jobs[name]
            raise AssertionError(path)

        def send(method, path, body):
            self.sent.append((method, path, body))
            if path.endswith("/jobs"):
                self.jobs[body["metadata"]["name"]] = {"status": {"active": 1}}
            return body

        RESTRUCTURE.bind(get, send, lambda path: "cp: No space left on device\n")
        self.item = {"progress": 0, "ref": {"namespace": "lab", "name": "app", "replicas": 2, "phase": "stopping",
                     "moves": [{"path": "/a", "from": "one", "from_folder": "", "to": "all", "to_folder": "a"}]}}

    def test_waits_for_the_pods_then_copies_then_starts_again(self):
        self.assertEqual("running", RESTRUCTURE.resolve(self.item)[0])
        self.assertEqual([], self.sent)
        self.pods = []
        status, _, message = RESTRUCTURE.resolve(self.item)
        self.assertEqual(("running", "copying"), (status, self.item["ref"]["phase"]))
        self.assertIn("Copying 1 location", message)
        self.assertEqual("running", RESTRUCTURE.resolve(self.item)[0])
        self.jobs[self.item["ref"]["job"]] = {"status": {"succeeded": 1}}
        status, progress, message = RESTRUCTURE.resolve(self.item)
        self.assertEqual(("succeeded", 100), (status, progress))
        saved = self.sent[-1][2]
        self.assertEqual(2, saved["spec"]["replicas"])
        self.assertNotIn(RESTRUCTURE.HELD, saved["metadata"]["annotations"])

    def test_a_failed_copy_leaves_the_workload_stopped_and_says_why(self):
        self.pods = []
        RESTRUCTURE.resolve(self.item)
        self.jobs[self.item["ref"]["job"]] = {"status": {"failed": 1}}
        self.pods = [{"metadata": {"name": "copy-pod"}}]
        status, _, message = RESTRUCTURE.resolve(self.item)
        self.assertEqual("failed", status)
        self.assertIn("No space left", message)
        self.assertEqual(1, len(self.sent))  # only the job; the workload was not started


class HeldEditTests(WorkloadEditFixture, unittest.TestCase):
    def test_a_held_edit_saves_stopped_and_parks_the_count(self):
        self.deployment["spec"]["replicas"] = 3
        result = self.edit([{"path": "/config", "kind": "existing", "source": "media"}])
        self.assertNotIn("held_replicas", result)
        import homestead_lifecycle as lifecycle
        result = lifecycle.edit_workload({"ns": "lab", "name": "frigate", "containers": [
            {"original_name": "frigate", "name": "frigate", "image": "frigate:test",
             "volumes": [{"path": "/config", "kind": "existing", "source": "media"}]}]}, hold=True)
        self.assertEqual(3, result["held_replicas"])
        saved = self.sent[-1][2]
        self.assertEqual(0, saved["spec"]["replicas"])
        self.assertEqual("3", saved["metadata"]["annotations"][RESTRUCTURE.HELD])


if __name__ == "__main__":
    unittest.main()
