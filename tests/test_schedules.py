import datetime
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
import homestead_names as NAMES
import homestead_schedules as S

UTC = datetime.timezone.utc


def at(y, mo, d, h, mi, tz="UTC"):
    return datetime.datetime(y, mo, d, h, mi, tzinfo=S.zone(tz)).timestamp()


# Wednesday 7 October 2026.
WED_0100 = at(2026, 10, 7, 1, 0)
NIGHTS = {"stop": "01:00", "start": "07:00", "days": [0, 1, 2, 3, 4], "tz": "UTC", "since": 0}


class CleanTests(unittest.TestCase):
    def test_a_schedule_is_checked(self):
        got = S.clean({"stop": "01:00", "start": "07:00", "days": [4, 0, 0], "tz": "Europe/London"}, now=5)
        self.assertEqual({"stop": "01:00", "start": "07:00", "days": [0, 4], "tz": "Europe/London", "since": 5}, got)
        self.assertEqual("", S.clean({"start": "07:00", "days": [0]})["stop"], "either time may be left out")
        self.assertIsNone(S.clean(None))
        for bad in ({"days": [0]}, {"stop": "25:00", "days": [0]}, {"stop": "1:00", "days": [0]},
                    {"stop": "01:00", "days": []}, {"stop": "01:00", "days": [7]},
                    {"stop": "01:00", "start": "01:00", "days": [0]}, {"stop": "01:00", "days": [0], "tz": "Mars/Base"}):
            with self.assertRaises(ValueError, msg=bad):
                S.clean(bad)

    def test_read_from_an_annotation(self):
        ann = {NAMES.key(S.KEY): json.dumps(NIGHTS)}
        self.assertEqual("01:00", S.read(ann, NAMES)["stop"])
        self.assertIsNone(S.read({NAMES.key(S.KEY): "not json"}, NAMES))
        self.assertIsNone(S.read({}, NAMES))

    def test_described_in_words(self):
        self.assertEqual("Stops 01:00, starts 07:00, weekdays", S.describe(NIGHTS))
        self.assertEqual("Starts 07:00, every day", S.describe({"start": "07:00", "days": list(range(7))}))
        self.assertEqual("Stops 23:00, Mon, Sat", S.describe({"stop": "23:00", "days": [0, 5]}))


class DueTests(unittest.TestCase):
    def test_due_within_the_grace_and_only_once(self):
        self.assertIsNone(S.due(NIGHTS, WED_0100 - 60))
        self.assertEqual((WED_0100, "stop"), S.due(NIGHTS, WED_0100 + 120))
        self.assertIsNone(S.due(NIGHTS, WED_0100 + 120, done=f"stop@{int(WED_0100)}"))
        self.assertIsNone(S.due(NIGHTS, WED_0100 + S.GRACE + 60), "missed by more than the grace: skipped, not late")

    def test_only_on_the_chosen_days(self):
        sat = at(2026, 10, 10, 1, 1)
        self.assertIsNone(S.due(NIGHTS, sat))

    def test_not_for_times_before_it_was_set(self):
        self.assertIsNone(S.due({**NIGHTS, "since": WED_0100 + 60}, WED_0100 + 120))

    def test_in_the_time_zone_it_was_chosen_in(self):
        ny = {**NIGHTS, "tz": "America/New_York"}
        local = at(2026, 10, 7, 1, 0, "America/New_York")
        self.assertEqual(4 * 3600, local - WED_0100, "01:00 in New York is 05:00 UTC in October")
        self.assertEqual((local, "stop"), S.due(ny, local + 60))
        self.assertIsNone(S.due(ny, WED_0100 + 60))

    def test_the_next_few_times(self):
        nxt = S.upcoming(NIGHTS, WED_0100 + 60)
        self.assertEqual(["start", "stop", "start"], [a for _, a in nxt])
        self.assertEqual(at(2026, 10, 7, 7, 0), nxt[0][0])
        fri_night = S.upcoming(NIGHTS, at(2026, 10, 9, 8, 0))
        self.assertEqual(at(2026, 10, 12, 1, 0), fri_night[0][0], "after Friday the next is Monday")


class TickTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        S.bind(self.dir.name)

    def tearDown(self):
        self.dir.cleanup()

    def test_does_what_is_due_once_and_keeps_the_result(self):
        items = [{"kind": "app", "ns": "lab", "name": "frigate", "schedule": NIGHTS, "running": True}]
        calls = []
        act = lambda item, action: calls.append((item["name"], action)) or f"{action}ped"
        did = S.tick(items, act, now=WED_0100 + 30)
        self.assertEqual([("frigate", "stop")], calls)
        self.assertEqual("app:lab/frigate", did[0]["key"])
        self.assertEqual([], S.tick(items, act, now=WED_0100 + 90), "not twice")
        self.assertTrue(S.results()["app:lab/frigate"]["ok"])

    def test_a_failure_alerts_until_one_succeeds(self):
        items = [{"kind": "vm", "ns": "default", "name": "win11", "schedule": NIGHTS, "running": False}]

        def refuse(item, action):
            raise ValueError("it cannot start: not enough memory")
        S.tick(items, refuse, now=at(2026, 10, 7, 7, 1))
        facts = S.alert_facts()
        self.assertEqual("schedule:vm:default/win11", facts[0]["key"])
        self.assertIn("not enough memory", facts[0]["body"])
        self.assertIn("/vms?panel=schedule", facts[0]["href"])
        S.tick(items, lambda i, a: "stopped", now=at(2026, 10, 8, 1, 1))
        self.assertEqual([], S.alert_facts())

    def test_unscheduled_items_are_forgotten(self):
        items = [{"kind": "app", "ns": "lab", "name": "x", "schedule": NIGHTS, "running": True}]
        S.tick(items, lambda i, a: (_ for _ in ()).throw(ValueError("no")), now=WED_0100 + 30)
        self.assertEqual(1, len(S.alert_facts()))
        S.tick([], lambda i, a: "", now=WED_0100 + 90)
        self.assertEqual([], S.alert_facts())

    def test_nothing_due_writes_nothing(self):
        S.tick([{"kind": "app", "ns": "lab", "name": "x", "schedule": NIGHTS, "running": True}], lambda i, a: "", now=WED_0100 - 600)
        self.assertFalse((Path(self.dir.name) / "schedules.json").exists())

    def test_items_from_rows(self):
        got = S.items([{"ns": "lab", "name": "a", "desired": 0, "schedule": NIGHTS}, {"ns": "lab", "name": "b"}],
                      [{"ns": "default", "name": "v", "status": "Running", "schedule": NIGHTS}])
        self.assertEqual([("app", "a", False), ("vm", "v", True)], [(i["kind"], i["name"], i["running"]) for i in got])


class ServerTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        S.bind(self.dir.name)

    def tearDown(self):
        self.dir.cleanup()

    def test_a_scheduled_stop_remembers_the_copies_and_the_start_restores_them(self):
        import server
        dep = {"metadata": {"annotations": {}}, "spec": {"replicas": 2}}
        sent, scaled = [], []
        with mock.patch.object(server, "kget", return_value=dep), \
             mock.patch.object(server, "ksend", side_effect=lambda *a, **k: sent.append(a)), \
             mock.patch.object(server, "api_scale", side_effect=lambda ns, name, n: scaled.append(n)):
            self.assertEqual("stopped frigate", server.schedule_act({"kind": "app", "ns": "lab", "name": "frigate"}, "stop"))
            self.assertEqual("2", sent[0][2]["metadata"]["annotations"][NAMES.key(S.REPLICAS_KEY)])
            dep.update(spec={"replicas": 0}, metadata={"annotations": {NAMES.key(S.REPLICAS_KEY): "2"}})
            self.assertIn("2 copies", server.schedule_act({"kind": "app", "ns": "lab", "name": "frigate"}, "start"))
        self.assertEqual([0, 2], scaled)

    def test_homestead_itself_cannot_be_scheduled(self):
        import server
        with mock.patch.object(server, "guard_managed_smb"), mock.patch.object(server, "require_workload_target"), \
             mock.patch.object(server, "is_self", return_value=True):
            with self.assertRaises(ValueError):
                server.set_schedule({"kind": "app", "ns": "homestead", "name": "homestead",
                                     "schedule": {"stop": "01:00", "days": [0]}})


class HistoryTests(unittest.TestCase):
    def setUp(self):
        import homestead_changes as CHANGES
        self.C, self.dir = CHANGES, tempfile.TemporaryDirectory()
        CHANGES.bind(self.dir.name)
        CHANGES._notes.clear()

    def tearDown(self):
        self.C.REQUEST.user = None
        self.dir.cleanup()

    def dep(self, replicas, image="app:1"):
        return {"metadata": {"namespace": "lab", "name": "paperless"},
                "spec": {"replicas": replicas, "template": {"spec": {"containers": [{"name": "app", "image": image}]}}}}

    def test_a_scheduled_stop_is_not_a_change_but_an_edit_still_is(self):
        C = self.C
        C.observe([self.dep(1)], now=1000)
        C.REQUEST.user = C.BY_SCHEDULE
        C.note_write("PATCH", "/apis/apps/v1/namespaces/lab/deployments/paperless/scale", now=1030)
        C.observe([self.dep(0)], now=1060)
        self.assertEqual([], C.history("lab", "paperless"))
        C.REQUEST.user = "admin"
        C.note_write("PATCH", "/apis/apps/v1/namespaces/lab/deployments/paperless", now=1090)
        C.observe([self.dep(1, "app:2")], now=1120)
        rows = C.history("lab", "paperless")[0]["changes"]
        self.assertEqual({"Copies", "app: image"}, {r["field"] for r in rows}, "a person's change is recorded in full")

    def test_stop_and_start_from_homestead_are_homesteads(self):
        C = self.C
        C.observe([self.dep(1)], now=1000)
        C.REQUEST.user = "admin"
        C.note_write("PATCH", "/apis/apps/v1/namespaces/lab/deployments/paperless/scale", now=1030)
        C.observe([self.dep(0)], now=1060)
        entry = C.history("lab", "paperless")[0]
        self.assertEqual(("homestead", "admin"), (entry["source"], entry["by"]), "not outside Homestead")


class PolicyTests(unittest.TestCase):
    def test_routes_apart_from_scheduled_jobs(self):
        import homestead_route_policy as POLICY
        self.assertEqual("viewer", POLICY.role("/api/power-schedules", "GET"))
        self.assertEqual("operator", POLICY.role("/api/power-schedules/set", "POST"))
        self.assertEqual("operator", POLICY.POLICY[("POST", "/api/schedules")], "scheduled jobs keep theirs")

if __name__ == "__main__":
    unittest.main()
