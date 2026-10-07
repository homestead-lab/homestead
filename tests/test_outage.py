import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
import homestead_names as NAMES
import homestead_outage as O

APP = {"kind": "app", "ns": "lab", "name": "frigate", "key": "lab/frigate",
       "actions": {"restart": {"after_min": 5, "max": 2}, "webhook": "https://hooks.example.com/x"}}


def down(since):
    return {"lab/frigate": {"state": "down", "since": since, "last": {"error": "HTTP 502"}}}


UP = {"lab/frigate": {"state": "up", "since": 0, "last": {}}}


class Base(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        O.bind(self.dir.name)
        self.restarts, self.sent = [], []

    def tearDown(self):
        self.dir.cleanup()

    def tick(self, report, now, item=APP, busy=False, ops=(), restart=None):
        return O.tick([item], report, restart or (lambda i: self.restarts.append(now)), lambda i: busy, ops,
                      send=lambda url, body: self.sent.append(body["event"]), now=now)


class CleanTests(unittest.TestCase):
    def test_nothing_unless_asked(self):
        self.assertIsNone(O.clean(None))
        self.assertIsNone(O.clean({}))
        self.assertIsNone(O.clean({"restart": None, "webhook": ""}))

    def test_checked(self):
        self.assertEqual({"restart": {"after_min": 5, "max": 3}, "webhook": "http://192.0.2.5:8123/api/webhook/x"},
                         O.clean({"restart": {"after_min": "5", "max": 3}, "webhook": " http://192.0.2.5:8123/api/webhook/x "}))
        for bad in ({"restart": {"after_min": 0, "max": 1}}, {"restart": {"after_min": 5, "max": 11}},
                    {"restart": {"after_min": "x"}}, {"webhook": "ftp://x"}, {"webhook": "https://a b"}):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                O.clean(bad)

    def test_a_vm_restarts_only_when_monitored_on_its_own_port(self):
        with self.assertRaises(ValueError):
            O.clean({"restart": {"after_min": 5, "max": 1}}, "vm", monitored_on_port=False)
        self.assertEqual({"webhook": "https://h.example.com"}, O.clean({"webhook": "https://h.example.com"}, "vm", False))

    def test_read_from_an_annotation(self):
        self.assertEqual(2, O.read({NAMES.key(O.KEY): json.dumps(APP["actions"])}, NAMES)["restart"]["max"])
        self.assertIsNone(O.read({NAMES.key(O.KEY): "{}"}, NAMES))


class TickTests(Base):
    def test_restarts_after_the_wait_then_every_wait_at_most_max_then_gives_up(self):
        t0 = 10_000
        self.tick(down(t0), t0 + 60)
        self.assertEqual([], self.restarts, "not before five minutes down")
        self.assertEqual(["down"], self.sent)
        self.tick(down(t0), t0 + 300)
        self.tick(down(t0), t0 + 360)
        self.assertEqual([t0 + 300], self.restarts, "and not again a minute later")
        self.tick(down(t0), t0 + 600)
        self.tick(down(t0), t0 + 900)
        self.assertEqual([t0 + 300, t0 + 600], self.restarts, "at most twice")
        self.assertEqual(["down", "restarted", "restarted", "gave-up"], self.sent)
        fact = O.alert_facts()[0]
        self.assertEqual(("outage:app:lab/frigate", "critical"), (fact["key"], fact["severity"]))
        self.assertIn("still down after 2 restarts", fact["title"])

    def test_up_again_tells_resolves_and_later_resets_the_count(self):
        t0 = 10_000
        for t in (t0 + 300, t0 + 600, t0 + 900):
            self.tick(down(t0), t)
        self.tick(UP, t0 + 1000)
        self.assertEqual("up", self.sent[-1])
        self.assertEqual([], O.alert_facts(), "answering again resolves the alert")
        self.assertEqual(2, O.report()["app:lab/frigate"]["restarts"], "the count waits")
        self.tick(UP, t0 + 1000 + O.UP_RESET)
        self.assertEqual(0, O.report()["app:lab/frigate"]["restarts"])

    def test_nothing_while_a_job_works_on_it(self):
        self.tick(down(10_000), 10_000 + 600, busy=True)
        self.assertEqual([], self.restarts, "an update's own watch decides, and may roll back")
        self.assertEqual(["down"], self.sent, "the webhook still hears it is down")

    def test_a_refused_restart_is_said(self):
        def refuse(item):
            raise ValueError("it cannot start: not enough memory")
        self.tick(down(10_000), 10_300, restart=refuse)
        fact = O.alert_facts()[0]
        self.assertEqual("degraded", fact["severity"])
        self.assertIn("not enough memory", fact["body"])

    def test_webhook_only_and_restart_only(self):
        self.tick(down(10_000), 10_600, item={**APP, "actions": {"webhook": "https://h.example.com"}})
        self.assertEqual(([], ["down"]), (self.restarts, self.sent))
        self.sent.clear()
        O.bind(tempfile.mkdtemp(dir=self.dir.name))
        self.tick(down(10_000), 10_600, item={**APP, "actions": {"restart": {"after_min": 1, "max": 1}}})
        self.assertEqual(([10_600], []), (self.restarts, self.sent))

    def test_an_automatic_update_result_is_told_once(self):
        ops = [{"id": "u1", "kind": "auto-update", "status": "failed", "resource": {"namespace": "lab", "name": "frigate"},
                "message": "Rolled back: after the update it stopped answering"},
               {"id": "u2", "kind": "auto-update", "status": "running", "resource": {"namespace": "lab", "name": "frigate"}}]
        self.tick(UP, 10_000, ops=ops)
        self.tick(UP, 10_060, ops=ops)
        self.assertEqual(["update"], self.sent)

    def test_nothing_happening_writes_nothing(self):
        self.tick({"lab/frigate": {"state": "off"}}, 10_000)
        self.assertFalse((Path(self.dir.name) / "outage-actions.json").exists())


class PayloadTests(unittest.TestCase):
    def test_chat_services_get_a_sentence(self):
        body = O.payload(APP, "down", down(5)["lab/frigate"], now=10)
        self.assertEqual("frigate is down: HTTP 502", body["text"])
        self.assertEqual(body["text"], body["content"])
        self.assertEqual(("down", "lab", "frigate", 5), (body["event"], body["namespace"], body["name"], body["since"]))


class ServerTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        O.bind(self.dir.name)

    def tearDown(self):
        self.dir.cleanup()

    def test_only_the_annotation_changes(self):
        import server
        sent = []
        with mock.patch.object(server, "ksend", side_effect=lambda *a, **k: sent.append(a)), \
             mock.patch.object(server, "guard_managed_smb"):
            r = server.set_outage_actions({"kind": "app", "ns": "lab", "name": "frigate",
                                           "actions": {"restart": {"after_min": 5, "max": 3}}})
            server.set_outage_actions({"kind": "app", "ns": "lab", "name": "frigate", "actions": None})
        self.assertEqual({"metadata": {"annotations": {"homestead.io/outage-actions": '{"restart":{"after_min":5,"max":3}}'}}}, sent[0][2])
        self.assertIsNone(sent[1][2]["metadata"]["annotations"]["homestead.io/outage-actions"])
        self.assertIn("at most 3 times", r["detail"])

    def test_a_vm_on_automatic_monitoring_cannot_be_restarted(self):
        import server
        vm = {"metadata": {"annotations": {}}}
        with mock.patch.object(server, "kget", return_value=vm), mock.patch.object(server, "ksend"):
            with self.assertRaises(ValueError):
                server.set_outage_actions({"kind": "vm", "ns": "lab", "name": "ha", "actions": {"restart": {"after_min": 5, "max": 1}}})
            vm["metadata"]["annotations"]["homestead.io/uptime"] = "tcp:22"
            server.set_outage_actions({"kind": "vm", "ns": "lab", "name": "ha", "actions": {"restart": {"after_min": 5, "max": 1}}})

    def test_busy_while_a_job_runs_on_it(self):
        import server
        restarted = []
        with mock.patch.object(server, "cached", side_effect=lambda key, ttl, fn: [{"ns": "lab", "name": "frigate", "outage_actions": APP["actions"]}] if key == "wl" else []), \
             mock.patch.object(server.OPS, "list_operations", return_value=[{"status": "running", "resource": {"namespace": "lab", "name": "frigate"}}]), \
             mock.patch.object(server.UPTIME, "report", return_value=down(0)), \
             mock.patch.object(server, "restart_workload", side_effect=lambda ns, n: restarted.append(n)), \
             mock.patch.object(O, "_send_later"):
            server.outage_tick()
        self.assertEqual([], restarted)


if __name__ == "__main__":
    unittest.main()
