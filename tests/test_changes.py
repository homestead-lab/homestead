import copy
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
import homestead_changes as CH


def dep(image="example/sonarr:4.0", memory="512Mi", env=None, replicas=1, ns="lab", name="sonarr"):
    return {"metadata": {"namespace": ns, "name": name, "annotations": {"homestead.io/icon": "x"}},
            "spec": {"replicas": replicas, "template": {"metadata": {"labels": {"app": name}}, "spec": {
                "containers": [{"name": "app", "image": image, "ports": [{"containerPort": 8989}],
                                "resources": {"requests": {"memory": memory}},
                                "env": env if env is not None else [{"name": "TZ", "value": "Europe/London"},
                                                                    {"name": "API_KEY", "value": "abc123"},
                                                                    {"name": "DB_PASSWORD", "valueFrom": {"secretKeyRef": {"name": "db", "key": "pw"}}}],
                                "volumeMounts": [{"name": "config", "mountPath": "/config"}]}],
                "volumes": [{"name": "config", "persistentVolumeClaim": {"claimName": "sonarr-config"}}]}}}}


class Base(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(CH.bind, CH.DATA_DIR)  # leave the history where it was found
        CH.bind(self.dir.name)

    def tearDown(self):
        self.dir.cleanup()


class SummaryTests(Base):
    def test_what_a_person_changes_and_never_a_secret(self):
        s = CH.summary(dep())
        self.assertEqual("example/sonarr:4.0", s["app: image"])
        self.assertEqual("512Mi", s["app: memory reserved"])
        self.assertEqual("8989/TCP", s["app: ports"])
        self.assertEqual("volume sonarr-config", s["app: mount /config"])
        self.assertEqual("Europe/London", s["app: env TZ"])
        self.assertEqual("(hidden)", s["app: env API_KEY"], "a name that looks like a key hides its value")
        self.assertEqual("from Secret db (pw)", s["app: env DB_PASSWORD"])
        self.assertNotIn("abc123", str(s))

    def test_a_changed_secret_says_only_that_it_changed(self):
        before = CH.summary(dep())
        after = CH.summary(dep(env=[{"name": "TZ", "value": "Europe/London"}, {"name": "API_KEY", "value": "zzz999"},
                                    {"name": "DB_PASSWORD", "valueFrom": {"secretKeyRef": {"name": "db", "key": "pw"}}}]))
        self.assertEqual([], CH.diff(before, after), "same name, value hidden both times: nothing to show")
        rows = CH.diff(before, CH.summary(dep(env=[])))
        api = next(r for r in rows if r["field"] == "app: env API_KEY")
        self.assertEqual(("(hidden)", None), (api["before"], api["after"]))


class ObserveTests(Base):
    def test_first_sight_is_not_a_change(self):
        apps = CH.observe([dep()], now=1000)
        self.assertEqual([], apps["lab/sonarr"]["entries"])

    def test_a_change_through_homestead_is_attributed_to_the_person(self):
        CH.observe([dep()], now=1000)
        written = dep(image="example/sonarr:4.1", memory="1Gi")
        with mock.patch.object(CH, "_author_key", lambda: b"test-key"), \
                mock.patch.object(CH, "_authors", lambda: {"users": ["james"], "api_keys": []}):
            CH.REQUEST.user = "james"
            CH.note_write("PUT", "/apis/apps/v1/namespaces/lab/deployments/sonarr", written, now=1030)
            CH.REQUEST.user = None
            CH.observe([written], now=1060, since=1000)    # the write itself says who
        entry = CH.history("lab", "sonarr")[0]
        self.assertEqual(("homestead", "james"), (entry["source"], entry["by"]))
        self.assertEqual({"app: image", "app: memory reserved"}, {r["field"] for r in entry["changes"]})
        self.assertTrue(entry["can_undo"])
        self.assertNotIn("before", entry, "the kept settings are not sent to the page")

    def test_a_change_nobody_in_homestead_made_is_outside(self):
        CH.observe([dep()], now=1000)
        CH.note_write("PUT", "/apis/apps/v1/namespaces/lab/deployments/radarr", now=1030)   # another app
        CH.observe([dep(replicas=2)], now=1060, since=1000)
        entry = CH.history("lab", "sonarr")[0]
        self.assertEqual(("outside", ""), (entry["source"], entry["by"]))

    def test_homesteads_own_labels_are_not_changes(self):
        CH.observe([dep()], now=1000)
        noisy = dep()
        noisy["metadata"]["annotations"].update({"homestead.io/uptime": "/health", "homestead.io/ran-digests": "{}"})
        noisy["metadata"]["resourceVersion"] = "999"
        CH.observe([noisy], now=1060, since=1000)
        self.assertEqual([], CH.history("lab", "sonarr"))

    def test_writes_happen_only_on_change(self):
        with mock.patch.object(CH, "_save", wraps=CH._save) as save:
            CH.observe([dep()], now=1000)
            CH.observe([dep()], now=1060)
            CH.observe([dep()], now=1120)
            self.assertEqual(1, save.call_count)

    def test_history_is_capped(self):
        CH.observe([dep()], now=1000)
        for i in range(CH.KEEP + 5):
            CH.observe([dep(replicas=i % 2 + 1)], now=1060 + i * 60)
        self.assertEqual(CH.KEEP, len(CH.history("lab", "sonarr")))


class UndoTests(Base):
    def test_undo_puts_the_settings_from_before_back(self):
        CH.observe([dep()], now=1000)
        CH.observe([dep(image="example/sonarr:5.0")], now=1060)
        entry = CH.kept_before("lab", "sonarr", CH.history("lab", "sonarr")[0]["id"])
        current = dep(image="example/sonarr:5.0")
        current["metadata"]["resourceVersion"] = "7"
        proposed, rows = CH.undo_plan(current, entry)
        self.assertEqual("example/sonarr:4.0", proposed["spec"]["template"]["spec"]["containers"][0]["image"])
        self.assertEqual("7", proposed["metadata"]["resourceVersion"], "the write is fenced on the version reviewed")
        self.assertEqual([{"field": "app: image", "before": "example/sonarr:5.0", "after": "example/sonarr:4.0"}], rows)

    def test_nothing_to_undo_is_said(self):
        CH.observe([dep()], now=1000)
        CH.observe([dep(image="example/sonarr:5.0")], now=1060)
        entry = CH.kept_before("lab", "sonarr", CH.history("lab", "sonarr")[0]["id"])
        with self.assertRaisesRegex(ValueError, "already has these settings"):
            CH.undo_plan(dep(), entry)
        with self.assertRaisesRegex(ValueError, "no longer in the history"):
            CH.kept_before("lab", "sonarr", "nope")



def helm_dep(name, version):
    """An app as a Helm chart writes it: about 3 KB of pod template."""
    env = [{"name": f"SETTING_{i}", "value": f"value-{i}-{'x' * 20}"} for i in range(25)]
    probe = {"httpGet": {"path": "/health", "port": 8080}, "periodSeconds": 10, "failureThreshold": 3}
    return {"metadata": {"namespace": "apps", "name": name},
            "spec": {"replicas": 1, "template": {
                "metadata": {"labels": {"app.kubernetes.io/name": name}, "annotations": {"checksum/config": "0" * 64}},
                "spec": {"containers": [{"name": name, "image": f"example/{name}:1.{version}.0", "env": env,
                                         "ports": [{"containerPort": 8080}], "livenessProbe": probe, "readinessProbe": probe,
                                         "resources": {"requests": {"cpu": "100m", "memory": "256Mi"}},
                                         "volumeMounts": [{"name": "config", "mountPath": "/config"}]}],
                         "volumes": [{"name": "config", "persistentVolumeClaim": {"claimName": f"{name}-config"}}]}}}}


def old_format(apps, entries):
    """change-history.json as it was written before #981: a whole template in every entry."""
    data = {"apps": {}}
    for a in range(apps):
        name = f"app-{a}"
        rows = [{"id": f"{a:04x}{v:08x}", "at": 10_000_000 - v * 3600, "source": "homestead", "by": "james",
                 "changes": CH.diff(CH.summary(helm_dep(name, v)), CH.summary(helm_dep(name, v + 1))),
                 "before": CH._template(helm_dep(name, v))} for v in range(entries, 0, -1)]
        data["apps"][f"apps/{name}"] = {"last": CH.summary(helm_dep(name, entries + 1)),
                                        "kept": CH._template(helm_dep(name, entries + 1)), "entries": rows, "seen": 10_000_000}
    return data


class CompactHistoryTests(Base):
    """#981: the index keeps each change's rows and only a digest of the
    template from before it; templates are kept once each, beside it."""

    def index(self):
        import json
        return json.loads(Path(self.dir.name, "change-history.json").read_text())

    def write_old(self, data):
        import json
        Path(self.dir.name, "change-history.json").write_text(json.dumps(data, sort_keys=True))

    def test_the_index_holds_no_templates(self):
        CH.observe([dep()], now=1000)
        CH.observe([dep(image="example/sonarr:5.0")], now=1060)
        CH.observe([dep(image="example/sonarr:6.0")], now=1120)
        app = self.index()["apps"]["lab/sonarr"]
        self.assertIsInstance(app["kept"], str)
        self.assertTrue(all(isinstance(e["before"], str) for e in app["entries"]))
        self.assertFalse("containers" in Path(self.dir.name, "change-history.json").read_text(), "no template in the index")
        self.assertEqual(3, len(list(Path(self.dir.name, CH.TEMPLATES).iterdir())), "4.0, 5.0 and 6.0, once each")

    def test_thirty_apps_of_fifty_changes_keep_the_index_small(self):
        self.write_old(old_format(30, CH.KEEP))
        before = Path(self.dir.name, "change-history.json").stat().st_size
        CH.observe([helm_dep(f"app-{a}", CH.KEEP + 1) for a in range(30)], now=10_000_100)
        after = Path(self.dir.name, "change-history.json").stat().st_size
        self.assertGreater(before, 3_000_000)
        self.assertLess(after, 500_000, f"{after} bytes, from {before}")
        self.assertEqual(30 * CH.KEEP, len(CH.history(limit=None)))

    def test_history_opens_no_template(self):
        CH.observe([dep()], now=1000)
        CH.observe([dep(image="example/sonarr:5.0")], now=1060)
        real = open

        def guarded(path, *args, **kwargs):
            if CH.TEMPLATES in str(path):
                raise AssertionError(f"history opened {path}")
            return real(path, *args, **kwargs)
        CH._memo.clear()                                   # a copy that has not read the index yet
        with mock.patch("builtins.open", guarded):
            self.assertTrue(CH.history("lab", "sonarr")[0]["can_undo"])

    def test_entries_from_before_are_undone_before_and_after_they_move(self):
        data = old_format(1, 3)
        self.write_old(data)
        old = data["apps"]["apps/app-0"]["entries"][1]
        listed = CH.history("apps", "app-0")
        self.assertEqual(old["before"], CH.kept_before("apps", "app-0", old["id"])["before"], "read as it is")
        CH.observe([helm_dep("app-0", 4)], now=10_000_100)
        self.assertFalse("containers" in Path(self.dir.name, "change-history.json").read_text(), "moved out on the first look")
        self.assertEqual(listed, CH.history("apps", "app-0"))
        entry = CH.kept_before("apps", "app-0", old["id"])
        self.assertEqual(old["before"], entry["before"])
        proposed, rows = CH.undo_plan(helm_dep("app-0", 4), entry)
        self.assertEqual("example/app-0:1.2.0", proposed["spec"]["template"]["spec"]["containers"][0]["image"])
        CH.observe([helm_dep("app-0", 5)], now=10_000_200)
        self.assertEqual(old["before"], CH.kept_before("apps", "app-0", old["id"])["before"])
        newest = CH.history("apps", "app-0")[0]
        self.assertEqual("example/app-0:1.4.0", CH.kept_before("apps", "app-0", newest["id"])["before"]["template"]["spec"]["containers"][0]["image"],
                         "the template kept before the move is the one the next change undoes to")

    def test_a_template_no_entry_needs_is_removed(self):
        import os
        CH.observe([dep()], now=1000)
        for i in range(CH.KEEP + 5):
            CH.observe([dep(image=f"example/sonarr:{i}")], now=1060 + i * 60)
        self.assertEqual(CH.KEEP, len(CH.history("lab", "sonarr")))
        self.assertEqual(CH.KEEP + 6, len(list(Path(self.dir.name, CH.TEMPLATES).iterdir())), "none removed within the hour")
        for f in Path(self.dir.name, CH.TEMPLATES).iterdir():
            os.utime(f, (f.stat().st_mtime - CH.TEMPLATE_GRACE - 1,) * 2)
        CH.observe([dep(image="example/sonarr:last")], now=1060 + (CH.KEEP + 6) * 60)
        self.assertEqual(CH.KEEP + 1, len(list(Path(self.dir.name, CH.TEMPLATES).iterdir())), "the 50 entries' and the app's own")
        for e in CH.history("lab", "sonarr"):
            CH.kept_before("lab", "sonarr", e["id"])

    def test_a_template_another_leader_just_wrote_is_not_removed(self):
        CH.observe([dep()], now=1000)
        CH._keep(CH._template(dep(image="example/sonarr:9.9")))     # the new leader's, its index not saved yet
        CH.observe([dep(image="example/sonarr:5.0")], now=1060)       # the old leader saves and tidies
        self.assertEqual(3, len(list(Path(self.dir.name, CH.TEMPLATES).iterdir())))

    def test_a_template_cut_short_is_written_again(self):
        CH.observe([dep()], now=1000)
        for f in Path(self.dir.name, CH.TEMPLATES).iterdir():
            f.write_text("")                                          # a power cut after the index was saved
        CH.observe([dep(image="example/sonarr:5.0")], now=1060)
        CH.observe([dep()], now=1120)                                 # back to 4.0: the same settings again
        newest = CH.history("lab", "sonarr")[0]
        self.assertEqual("example/sonarr:5.0", CH.kept_before("lab", "sonarr", newest["id"])["before"]["template"]["spec"]["containers"][0]["image"])
        older = CH.history("lab", "sonarr")[1]
        self.assertTrue(older["can_undo"], "the 4.0 settings were written again when kept again")
        self.assertEqual("example/sonarr:4.0", CH.kept_before("lab", "sonarr", older["id"])["before"]["template"]["spec"]["containers"][0]["image"])

    def test_templates_are_written_to_last(self):
        with mock.patch.object(CH.SHARED, "write_json", wraps=CH.SHARED.write_json) as write:
            CH.observe([dep()], now=1000)
        durable = [c.kwargs.get("durable") for c in write.call_args_list if CH.TEMPLATES in c.args[0]]
        self.assertEqual([True], durable)

    def test_undo_without_its_template_is_said_and_not_offered(self):
        CH.observe([dep()], now=1000)
        CH.observe([dep(image="example/sonarr:5.0")], now=1060)
        for f in Path(self.dir.name, CH.TEMPLATES).iterdir():
            f.unlink()
        self.assertFalse(CH.history("lab", "sonarr")[0]["can_undo"])
        with self.assertRaisesRegex(ValueError, "no longer kept"):
            CH.undo_plan(dep(image="example/sonarr:5.0"), {"id": "x", "before": "0" * 32})

    def test_a_missing_template_is_no_longer_in_the_history(self):
        CH.observe([dep()], now=1000)
        CH.observe([dep(image="example/sonarr:5.0")], now=1060)
        for f in Path(self.dir.name, CH.TEMPLATES).iterdir():
            f.unlink()
        with self.assertRaisesRegex(ValueError, "no longer in the history"):
            CH.kept_before("lab", "sonarr", CH.history("lab", "sonarr")[0]["id"])

    def test_an_unchanged_index_is_not_read_again(self):
        import homestead_shared as SHARED
        CH.observe([dep()], now=1000)
        with mock.patch.object(SHARED, "parse_json", wraps=SHARED.parse_json) as read:
            for i in range(3):
                CH.observe([dep()], now=1060 + i * 60)
                CH.history("lab", "sonarr")
            self.assertEqual(0, sum(1 for c in read.call_args_list if c.args[1].endswith("change-history.json")), "the minute's look and the page answer from what was read")
            CH.observe([dep(image="example/sonarr:5.0")], now=1300)
            CH.history("lab", "sonarr")
            self.assertEqual(0, sum(1 for c in read.call_args_list if c.args[1].endswith("change-history.json")), "nor after its own write")
            other = self.index()                          # another copy of Homestead leads now, and writes
            other["apps"]["lab/sonarr"]["entries"][0]["by"] = "someone"
            self.write_old(other)
            self.assertEqual("someone", CH.history("lab", "sonarr")[0]["by"])
            self.assertEqual(1, sum(1 for c in read.call_args_list if c.args[1].endswith("change-history.json")))

    def test_a_failed_write_is_not_remembered_as_written(self):
        CH.observe([dep()], now=1000)
        real = CH.SHARED.write_json

        def write(path, *args, **kwargs):
            if path.endswith("change-history.json"):
                raise OSError("disk full")
            return real(path, *args, **kwargs)
        with mock.patch.object(CH.SHARED, "write_json", side_effect=write):
            with self.assertRaises(OSError):
                CH.observe([dep(image="example/sonarr:5.0")], now=1060)
        self.assertEqual([], CH.history("lab", "sonarr"))
        CH.observe([dep(image="example/sonarr:5.0")], now=1120)
        self.assertEqual(1, len(CH.history("lab", "sonarr")))

    def test_the_last_sight_is_saved_once_a_day(self):
        CH.observe([dep()], now=1000)
        with mock.patch.object(CH, "_save", wraps=CH._save) as save:
            for minute in range(1, 3 * 1440, 30):
                CH.observe([dep()], now=1000 + minute * 60)
            self.assertEqual(2, save.call_count, "a day and two days on")
        self.assertGreater(self.index()["apps"]["lab/sonarr"]["seen"], 1000 + 2 * 86400)


class SharedVolumeTests(Base):
    """#1081: two replicas share the data volume over NFS, where stat() of a
    path is answered from the client's attribute cache for up to a minute;
    an open revalidates it. Another copy's save must be seen at once."""

    def path(self):
        return str(Path(self.dir.name, "change-history.json"))

    def written_by_another_copy(self):
        """The leader's look, then a save by another copy of Homestead (a whole
        new file, as homestead_shared.write_json writes), and a stat() that
        still answers with the attributes from before it."""
        import json
        CH.observe([dep()], now=1000)
        CH.observe([dep(image="example/sonarr:5.0")], now=1060)
        self.assertEqual(1, len(CH.history("lab", "sonarr")))
        cached = os.stat(self.path())
        other = json.loads(Path(self.path()).read_text())
        other["apps"]["lab/sonarr"]["entries"][0]["by"] = "someone"
        CH.SHARED.write_json(self.path(), other, separators=(",", ":"), sort_keys=True, mode=0o600)
        real = os.stat

        def stat(path, *args, **kwargs):
            return cached if str(path) == self.path() else real(path, *args, **kwargs)
        return stat

    def test_another_copys_save_is_seen_while_stat_answers_from_its_cache(self):
        stale = self.written_by_another_copy()
        with mock.patch.object(CH.os, "stat", side_effect=stale):
            self.assertEqual("someone", CH.history("lab", "sonarr")[0]["by"])

    def test_the_next_leader_does_not_save_over_another_copys_save(self):
        stale = self.written_by_another_copy()
        with mock.patch.object(CH.os, "stat", side_effect=stale):
            CH.observe([dep(image="example/sonarr:6.0")], now=1120)
        rows = CH.history("lab", "sonarr")
        self.assertEqual(2, len(rows))
        self.assertEqual("someone", rows[1]["by"], "the other copy's entry is kept, not saved over")

    def test_a_rewrite_that_keeps_inode_time_and_size_is_seen(self):
        """A real file, nothing patched: rewritten in place to the same size
        and its modification time set back. Only st_ctime_ns tells."""
        CH.observe([dep()], now=1000)
        CH.observe([dep(image="example/sonarr:5.0")], now=1060)
        self.assertEqual(1060, CH.history("lab", "sonarr")[0]["at"])
        before = os.stat(self.path())
        text = Path(self.path()).read_text()
        self.assertEqual(1, text.count('"at":1060'))
        with open(self.path(), "r+", encoding="utf-8") as handle:
            handle.write(text.replace('"at":1060', '"at":1061'))
        os.utime(self.path(), ns=(before.st_atime_ns, before.st_mtime_ns))
        after = os.stat(self.path())
        self.assertEqual((before.st_ino, before.st_mtime_ns, before.st_size),
                         (after.st_ino, after.st_mtime_ns, after.st_size))
        self.assertEqual(1061, CH.history("lab", "sonarr")[0]["at"])

    def test_a_quiet_look_opens_the_file_once_and_parses_nothing(self):
        import builtins
        import json
        CH.observe([dep()], now=1000)
        CH.observe([dep(image="example/sonarr:5.0")], now=1060)
        CH.history("lab", "sonarr")
        opened, parsed = [], []
        real_open, real_load = builtins.open, json.load

        def counting_open(file, *args, **kwargs):
            if str(file) == self.path():
                opened.append(file)
            return real_open(file, *args, **kwargs)

        def counting_load(handle, *args, **kwargs):
            if getattr(handle, "name", "") == self.path():
                parsed.append(handle)
            return real_load(handle, *args, **kwargs)
        with mock.patch("builtins.open", counting_open), mock.patch("json.load", counting_load):
            for i in range(3):
                CH.observe([dep(image="example/sonarr:5.0")], now=1120 + i * 60)
                CH.history("lab", "sonarr")
            self.assertEqual([], parsed, "nothing changed, so nothing is parsed again")
            self.assertEqual(6, len(opened), "one open each: three looks and three page answers")
            CH.SHARED.write_json(self.path(), {"apps": {}}, separators=(",", ":"), sort_keys=True, mode=0o600)
            self.assertEqual([], CH.history("lab", "sonarr"))
            self.assertEqual(1, len(parsed), "another copy's save is parsed once")
            CH.history("lab", "sonarr")
            self.assertEqual(1, len(parsed))


    def test_a_save_replaced_before_it_is_looked_at_again_is_read_again(self):
        """A handover: another leader's save lands between this copy's write
        and its look at what it wrote. That file is not kept as this copy's."""
        import json
        CH.observe([dep()], now=1000)
        real = CH.SHARED.write_json
        other = {}

        def write_then_replaced(path, value, **kwargs):
            written = real(path, value, **kwargs)
            if path == self.path() and not other:
                other.update(json.loads(json.dumps(value)))
                other["apps"]["lab/sonarr"]["entries"][0]["by"] = "someone"
                real(path, other, separators=(",", ":"), sort_keys=True, mode=0o600)
            return written
        with mock.patch.object(CH.SHARED, "write_json", side_effect=write_then_replaced):
            CH.observe([dep(image="example/sonarr:5.0")], now=1060)
        self.assertTrue(other, "the save was replaced")
        self.assertEqual("someone", CH.history("lab", "sonarr")[0]["by"])

    def test_a_save_that_was_not_replaced_is_still_not_read_again(self):
        import json
        CH.observe([dep()], now=1000)
        with mock.patch("json.load", wraps=json.load) as load:
            CH.observe([dep(image="example/sonarr:5.0")], now=1060)
            CH.history("lab", "sonarr")
            self.assertEqual(0, sum(1 for c in load.call_args_list if getattr(c.args[0], "name", "") == self.path()))

    def test_an_fstat_that_fails_says_the_history_cannot_be_read(self):
        import errno
        CH.observe([dep()], now=1000)
        CH.observe([dep(image="example/sonarr:5.0")], now=1060)
        stale = OSError(errno.ESTALE, "Stale file handle")
        with mock.patch.object(CH.os, "fstat", side_effect=stale):
            with self.assertRaisesRegex(CH.SHARED.Unreadable, "change-history.json.*Stale file handle"):
                CH.history("lab", "sonarr")
        self.assertEqual(1, len(CH.history("lab", "sonarr")), "read again once it can be")

class RouteTests(Base):
    def setUp(self):
        # Importing server binds the history to /data the first time; bind it
        # back to the test's own folder after that (a non-root runner can't write /data).
        import server  # noqa: F401
        super().setUp()

    def test_undo_refuses_settings_whose_volume_is_gone(self):
        import server
        CH.observe([dep()], now=1000)
        CH.observe([dep(image="example/sonarr:5.0")], now=1060)
        entry_id = CH.history("lab", "sonarr")[0]["id"]
        current = dep(image="example/sonarr:5.0")
        current["metadata"].update(uid="u1", resourceVersion="7")

        def kget(path):
            if path.endswith("/deployments/sonarr"):
                return copy.deepcopy(current)
            if path.endswith("/persistentvolumeclaims"):
                return {"items": []}
            raise AssertionError(path)
        with mock.patch.object(server.KUBE_HELPERS, "kget", side_effect=kget), mock.patch.object(server.OVERVIEW_ROUTES, "guard_managed_smb"), \
                mock.patch.object(server, "CHANGES", CH):
            with self.assertRaisesRegex(ValueError, "sonarr-config, which no longer exists"):
                server.OVERVIEW_ROUTES.change_undo_plan({"ns": "lab", "name": "sonarr", "id": entry_id})

    def test_writes_to_deployments_carry_the_requester(self):
        self.addCleanup(setattr, CH.REQUEST, "user", None)
        written = {"patch": {}, "get": {}, "configmap": {}, "post": {"metadata": {"name": "new"}}}
        with mock.patch.object(CH, "_author_key", lambda: b"test-key"), \
                mock.patch.object(CH, "_authors", lambda: {"users": ["james"], "api_keys": []}):
            CH.REQUEST.user = "james"
            CH.note_write("PATCH", "/apis/apps/v1/namespaces/lab/deployments/sonarr", written["patch"], now=500)
            CH.note_write("GET", "/apis/apps/v1/namespaces/lab/deployments/sonarr", written["get"], now=501)
            CH.note_write("PUT", "/api/v1/namespaces/lab/configmaps/x", written["configmap"], now=502)
            CH.REQUEST.user = None
            CH.note_write("POST", "/apis/apps/v1/namespaces/lab/deployments", written["post"], now=503)
            self.assertEqual("james", CH._marked_by(written["patch"], "lab", "sonarr", 500, 560))
            self.assertEqual("", CH._marked_by(written["post"], "lab", "new", 503, 560), "a job of Homestead's own")
        self.assertEqual(({}, {}), (written["get"], written["configmap"]), "a read, or another object, is not marked")



class LoopTests(Base):
    """The minutely look runs from here, so server.py only starts it."""

    def test_the_leader_observes_the_apps_deployments_and_beats(self):
        beats, saved = [], dict(CH._bound)
        self.addCleanup(CH._bound.update, saved)
        CH.bind(self.dir.name, deployments=lambda: ["web"], is_leader=lambda: True,
                beat=lambda *args, **kwargs: beats.append(args))

        class Stop(Exception):
            pass
        with mock.patch.object(CH, "observe") as observe, self.assertRaises(Stop):
            CH.run(sleep=mock.Mock(side_effect=Stop))
        self.assertEqual(["web"], observe.call_args.args[0])
        self.assertEqual([("changes", 60)], beats)

if __name__ == "__main__":
    unittest.main()
