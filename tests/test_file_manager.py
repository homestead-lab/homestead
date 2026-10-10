"""The volume file manager (homestead_files.py, filemanager.js): what a
folder shows, the operations, downloads as zips, uploads in pieces, copies
between volumes, and helpers that go a minute after they were last used."""
import base64
import io
import sys
import tarfile
import time
import unittest
import zipfile
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
import homestead_files as files
import homestead_route_policy as POLICY


class Helper:
    """The claim's helper: records each script and answers as the shell would."""
    def __init__(self, answers=None):
        self.scripts, self.stdin, self.answers = [], [], list(answers or [])

    def sh(self, namespace, pod, script, stdin=b"", timeout=30):
        self.scripts.append(script)
        self.stdin.append(stdin)
        out = self.answers.pop(0) if self.answers else b""
        status = b"1" if isinstance(out, Exception) else b"0"
        if isinstance(out, Exception):
            return b"\nrc=" + status, str(out)
        return out + b"\nrc=" + status, ""


class Base(unittest.TestCase):
    def setUp(self):
        self.helper = Helper()
        patches = [mock.patch.object(files, "_sh", side_effect=self.helper.sh),
                   mock.patch.object(files, "open_session", return_value={"namespace": "lab", "pod": "homestead-files-data"}),
                   mock.patch.object(files, "_touch", return_value=True)]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)


class Listing(Base):
    def test_a_folder_says_what_each_entry_is_and_who_owns_it(self):
        self.helper.answers = [b"directory|4096|755|1000|1000|app|app|1791600000|config\n"
                               b"regular file|12|644|0|0|root|root|1791600001|notes.txt\n"
                               b"symbolic link|9|777|0|0|root|root|1791600002|latest\n"
                               b"regular file|3|600|1883|1883|UNKNOWN|UNKNOWN|1791600003|.hidden\n"]
        listing = files.list_entries("lab", "data", "media")
        self.assertEqual(["config", ".hidden", "latest", "notes.txt"], [e["name"] for e in listing["entries"]])
        config = listing["entries"][0]
        self.assertEqual(("dir", "755", "app", 1000), (config["kind"], config["mode"], config["user"], config["uid"]))
        hidden = listing["entries"][1]
        self.assertEqual(("1883", "1883"), (hidden["user"], hidden["group"]), "an id with no name shows the id")
        self.assertEqual("link", listing["entries"][2]["kind"])
        self.assertIn("cd '/data/media'", self.helper.scripts[0])

    def test_a_missing_folder_says_so(self):
        self.helper.answers = [ValueError("no such folder")]
        with self.assertRaisesRegex(ValueError, "no such folder"):
            files.list_entries("lab", "data", "gone")


class Operations(Base):
    def test_names_cannot_leave_their_folder(self):
        for name in ("", ".", "..", "a/b", "a\\b", "x" * 256):
            with self.assertRaisesRegex(ValueError, "without slashes"):
                files.make_folder("lab", "data", "", name)
        self.assertEqual([], self.helper.scripts)

    def test_the_volume_itself_cannot_be_deleted_renamed_or_changed(self):
        for call in (lambda: files.delete("lab", "data", [""]), lambda: files.delete("lab", "data", ["/"]),
                     lambda: files.rename("lab", "data", "", "x"), lambda: files.set_mode("lab", "data", [""], "755")):
            with self.assertRaises(ValueError):
                call()
        with self.assertRaisesRegex(ValueError, "climb"):
            files.delete("lab", "data", ["../other"])
        self.assertEqual([], self.helper.scripts)

    def test_quoting_keeps_odd_names_as_names(self):
        files.rename("lab", "data", "media/it's here", "new; rm -rf ~")
        script = self.helper.scripts[0]
        self.assertIn("'/data/media/it'\\''s here'", script)
        self.assertIn("'/data/media/new; rm -rf ~'", script)

    def test_permissions_and_owners(self):
        files.set_mode("lab", "data", ["a", "b"], "0750", recursive=True)
        self.assertIn("chmod -R 0750 -- '/data/a' '/data/b'", self.helper.scripts[-1])
        files.set_owner("lab", "data", ["a"], 1000, 100)
        self.assertIn("chown 1000:100 -- '/data/a'", self.helper.scripts[-1])
        for bad in ("777x", "8", "rwx"):
            with self.assertRaisesRegex(ValueError, "octal"):
                files.set_mode("lab", "data", ["a"], bad)
        with self.assertRaisesRegex(ValueError, "number"):
            files.set_owner("lab", "data", ["a"], "root")

    def test_a_folder_cannot_be_copied_into_itself(self):
        with self.assertRaisesRegex(ValueError, "inside itself"):
            files.copy_within("lab", "data", ["media"], "media/sub")
        files.copy_within("lab", "data", ["media/a.txt"], "backup", move=True)
        self.assertIn("mv -- '/data/media/a.txt' '/data/backup'/", self.helper.scripts[-1])

    def test_a_failed_command_says_what_the_shell_said(self):
        self.helper.answers = [ValueError("that name is taken")]
        with self.assertRaisesRegex(ValueError, "that name is taken"):
            files.rename("lab", "data", "a", "b")


class Uploads(Base):
    def test_pieces_land_beside_the_file_and_the_last_puts_it_in_place(self):
        first = files.upload("lab", "data", "media", "film.mkv", base64.b64encode(b"abc").decode(), 0, False)
        self.assertEqual(3, first["received"])
        self.assertNotIn("mv -f", self.helper.scripts[-1])
        self.assertIn(": > '/data/media/.film.mkv.homestead-upload-", self.helper.scripts[-1])
        self.assertEqual(b"abc", self.helper.stdin[-1])
        last = files.upload("lab", "data", "media", "film.mkv", base64.b64encode(b"def").decode(), 3, True, first["token"])
        self.assertTrue(last["done"])
        self.assertIn("= 3 ]", self.helper.scripts[-1], "a piece is added only where the last one ended")
        self.assertIn("mv -f -- '/data/media/.film.mkv.homestead-upload-", self.helper.scripts[-1])

    def test_a_piece_that_is_not_whole_or_too_large_is_refused(self):
        with self.assertRaisesRegex(ValueError, "whole"):
            files.upload("lab", "data", "", "a", "not base64!")
        with mock.patch.object(files, "MAX_CHUNK", 2):
            with self.assertRaisesRegex(ValueError, "4 MiB"):
                files.upload("lab", "data", "", "a", base64.b64encode(b"abc").decode())
        with self.assertRaisesRegex(ValueError, "unknown upload"):
            files.upload("lab", "data", "", "a", "", 3, True, "../../x")


class Zips(unittest.TestCase):
    def test_a_folder_downloads_as_a_zip_made_while_it_streams(self):
        tar_bytes = io.BytesIO()
        with tarfile.open(fileobj=tar_bytes, mode="w") as archive:
            folder = tarfile.TarInfo("config")
            folder.type, folder.mode = tarfile.DIRTYPE, 0o755
            archive.addfile(folder)
            for name, data in (("config/app.yaml", b"key: value\n"), ("config/big.bin", b"x" * 300000)):
                info = tarfile.TarInfo(name)
                info.size, info.mode, info.mtime = len(data), 0o640, 1791600000
                archive.addfile(info, io.BytesIO(data))
        raw = tar_bytes.getvalue()
        pieces = [raw[i:i + 7000] for i in range(0, len(raw), 7000)]
        with mock.patch.object(files, "_stdout", side_effect=lambda sock, errors: iter(pieces)), \
                mock.patch.object(files, "_close_exec") as closed:
            body = b"".join(files._zip_of_tar(object()))
        closed.assert_called_once()
        with zipfile.ZipFile(io.BytesIO(body)) as zipped:
            self.assertEqual(["config/", "config/app.yaml", "config/big.bin"], zipped.namelist())
            self.assertEqual(b"key: value\n", zipped.read("config/app.yaml"))
            self.assertEqual(300000, len(zipped.read("config/big.bin")))
            self.assertEqual(0o640, (zipped.getinfo("config/app.yaml").external_attr >> 16) & 0o7777)

    def test_entries_for_one_zip_come_from_one_folder(self):
        with self.assertRaisesRegex(ValueError, "one folder"):
            files._tar_argv(["a/x", "b/y"])
        argv = files._tar_argv(["media/a b", "media/c"])
        self.assertEqual("cd '/data/media' && tar -cf - -- 'a b' 'c'", argv[2])

    def test_the_websocket_mask_matches_byte_by_byte(self):
        key, payload = b"\x01\x02\x03\x04", bytes(range(256)) * 3
        self.assertEqual(bytes(b ^ key[i % 4] for i, b in enumerate(payload)), files._mask(payload, key))


class Transfers(Base):
    def test_within_one_volume_it_is_a_copy_there(self):
        answer = files.transfer({"namespace": "lab", "pvc": "data", "path": ""},
                                {"namespace": "lab", "pvc": "data", "path": "backup"}, ["a"], move=False)
        self.assertTrue(answer["done"])
        self.assertIn("cp -a -- '/data/a' '/data/backup'/", self.helper.scripts[-1])

    def test_between_volumes_it_runs_in_the_background_and_refuses_a_clash(self):
        self.helper.answers = [b"a\n"]
        with mock.patch.object(files, "_sh", side_effect=lambda ns, pod, script, **k: (b"a\n", "") if "for n in" in script else (b"yes", "")):
            with self.assertRaisesRegex(ValueError, "a is already there"):
                files.transfer({"namespace": "lab", "pvc": "one"}, {"namespace": "lab", "pvc": "two", "path": ""}, ["a"])
        started = []
        with mock.patch.object(files, "_sh", side_effect=lambda ns, pod, script, **k: (b"", "") if "for n in" in script else (b"yes", "")), \
                mock.patch.object(files.threading, "Thread", side_effect=lambda **k: mock.Mock(start=lambda: started.append(k))):
            answer = files.transfer({"namespace": "lab", "pvc": "one"}, {"namespace": "media", "pvc": "two", "path": ""}, ["a"], move=True)
        self.assertEqual("running", answer["job"]["status"])
        self.assertEqual(1, len(started))
        self.assertEqual(answer["job"]["id"], files.transfer_status(answer["job"]["id"])["id"])


class Sessions(unittest.TestCase):
    def setUp(self):
        self.sent, self.pods = [], {}
        files.bind(lambda path: self.pods.get(path.rsplit("/", 1)[-1]) or (_ for _ in ()).throw(ValueError("gone")),
                   lambda method, path, body=None, **kw: self.sent.append((method, path, body)) or {},
                   None, "", None, {"kube-system"})

    def test_a_helper_is_made_saying_when_it_was_last_used(self):
        files.open_session.__wrapped__ if hasattr(files.open_session, "__wrapped__") else None
        with mock.patch.object(files, "_wait_ready", return_value={"pod": "homestead-files-data", "namespace": "lab"}):
            files.open_session("lab", "data")
        body = self.sent[-1][2]
        self.assertTrue(int(body["metadata"]["annotations"][files.SEEN]) > 0)

    def test_closing_waits_for_the_helper_to_go(self):
        self.pods["homestead-files-data"] = {"status": {"phase": "Running"}}
        checks = []

        def sleep(_):
            checks.append(1)
            if len(checks) == 2:
                del self.pods["homestead-files-data"]
        answer = files.close_session("lab", "data", wait=10, sleep=sleep)
        self.assertEqual({"ok": True, "stopped": True}, answer)
        self.assertEqual("DELETE", self.sent[0][0])
        self.pods["homestead-files-data"] = {"status": {"phase": "Running"}}
        self.assertFalse(files.close_session("lab", "data", wait=2, sleep=lambda _: None)["stopped"])

    def test_keepalive_says_whether_the_helper_is_still_there(self):
        self.assertFalse(files.keepalive("lab", "data")["running"])
        self.pods["homestead-files-data"] = {"status": {"phase": "Running"}}
        answer = files.keepalive("lab", "data")
        self.assertTrue(answer["running"])
        self.assertEqual(60, answer["idle_seconds"])
        self.assertEqual("PATCH", self.sent[-1][0])


class IdleCleanup(unittest.TestCase):
    def pod(self, seen):
        return {"metadata": {"name": "homestead-files-data", "namespace": "lab", "uid": "u", "resourceVersion": "1",
                             "creationTimestamp": "2026-10-02T00:00:00Z", "labels": {"homestead.io/task": "files"},
                             "annotations": {files.SEEN: str(seen)} if seen else {}},
                "spec": {"activeDeadlineSeconds": 14400,
                         "volumes": [{"name": "data", "persistentVolumeClaim": {"claimName": "data"}}]},
                "status": {"phase": "Running"}}

    def test_a_helper_goes_a_minute_after_it_was_last_used(self):
        created = 1790899200
        for seen, now, gone in ((created + 100, created + 159, False), (created + 100, created + 160, True),
                                (None, created + 600, False)):
            sent = []
            files.bind(lambda path, p=self.pod(seen): {"items": [p]}, lambda m, path, body: sent.append(m),
                       None, "", None, {"kube-system"})
            self.assertEqual(gone, bool(files.cleanup(now)), (seen, now))


class Routes(unittest.TestCase):
    def test_every_file_route_is_an_admins(self):
        for method, action in (("GET", "entries"), ("GET", "download"), ("GET", "transfer"), ("GET", "list"),
                               ("GET", "read"), ("POST", "keepalive"), ("POST", "folder"), ("POST", "rename"),
                               ("POST", "delete"), ("POST", "mode"), ("POST", "owner"), ("POST", "upload"),
                               ("POST", "transfer"), ("POST", "write"), ("POST", "close")):
            self.assertEqual("admin", POLICY.role("/api/files/" + action, method), action)


if __name__ == "__main__":
    unittest.main()
