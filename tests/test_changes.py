import copy
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
        CH.observe([dep()], now=1000)
        for i in range(CH.KEEP + 5):
            CH.observe([dep(image=f"example/sonarr:{i}")], now=1060 + i * 60)
        self.assertEqual(CH.KEEP, len(CH.history("lab", "sonarr")))
        self.assertEqual(CH.KEEP + 1, len(list(Path(self.dir.name, CH.TEMPLATES).iterdir())), "the 50 entries' and the app's own")
        for e in CH.history("lab", "sonarr"):
            CH.kept_before("lab", "sonarr", e["id"])

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
        with mock.patch.object(SHARED, "read_json", wraps=SHARED.read_json) as read:
            for i in range(3):
                CH.observe([dep()], now=1060 + i * 60)
                CH.history("lab", "sonarr")
            self.assertEqual(0, read.call_count, "the minute's look and the page answer from what was read")
            CH.observe([dep(image="example/sonarr:5.0")], now=1300)
            CH.history("lab", "sonarr")
            self.assertEqual(0, read.call_count, "nor after its own write")
            other = self.index()                          # another copy of Homestead leads now, and writes
            other["apps"]["lab/sonarr"]["entries"][0]["by"] = "someone"
            self.write_old(other)
            self.assertEqual("someone", CH.history("lab", "sonarr")[0]["by"])
            self.assertEqual(1, read.call_count)

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
