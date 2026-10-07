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
        CH.bind(self.dir.name)
        CH._notes.clear()

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
        CH.REQUEST.user = "james"
        CH.note_write("PUT", "/apis/apps/v1/namespaces/lab/deployments/sonarr", now=1030)
        CH.REQUEST.user = None
        CH.observe([dep(image="example/sonarr:4.1", memory="1Gi")], now=1060, since=1000)
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


class RouteTests(Base):
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
        with mock.patch.object(server, "kget", side_effect=kget), mock.patch.object(server, "guard_managed_smb"), \
                mock.patch.object(server, "CHANGES", CH):
            with self.assertRaisesRegex(ValueError, "sonarr-config, which no longer exists"):
                server.change_undo_plan({"ns": "lab", "name": "sonarr", "id": entry_id})

    def test_writes_to_deployments_are_noted_with_the_requester(self):
        CH.REQUEST.user = "james"
        CH.note_write("PATCH", "/apis/apps/v1/namespaces/lab/deployments/sonarr", now=500)
        CH.note_write("GET", "/apis/apps/v1/namespaces/lab/deployments/sonarr", now=501)
        CH.note_write("PUT", "/api/v1/namespaces/lab/configmaps/x", now=502)
        CH.REQUEST.user = None
        CH.note_write("POST", "/apis/apps/v1/namespaces/lab/deployments", {"metadata": {"name": "new"}}, now=503)
        self.assertEqual([(500, "lab", "sonarr", "james"), (503, "lab", "new", "")], CH._notes)


if __name__ == "__main__":
    unittest.main()
