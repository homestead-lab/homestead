"""A host job keeps a cordon a person put on while it ran.

Restart, Shut down and Maintenance cordon the host and take that cordon off
at their end. A person who presses Cordon on the Hosts page while the job
runs (Evacuate does too, on a host already cordoned), or Uncordon and then
Cordon, makes the cordon theirs: the job's end leaves the host as they set
it. The job tells its own cordon by its mark on the Node (homestead_cordon),
which the person's Cordon and Uncordon clear, as #1087 set out.

Through the routes, the job store, the job's resolver and End, on the fake
API server; the person's Cordon is the Hosts page's own route."""
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import homestead_cordon as CORDON
import homestead_history as HISTORY
import homestead_host_maintenance as HM
import homestead_lifecycle as LC
import homestead_nodes as NODES
import homestead_node_actions as NODE_ACTIONS
import homestead_operations as OPS
import homestead_power as POWER
import homestead_power_jobs as JOBS
import test_host_maintenance as MAINTENANCE_TESTS
from test_power_stop_uncordon import NODE, Case, refused, req

DRAINED = {"evicted": [], "skipped": []}


class PersonsCordonTests(Case):
    def setUp(self):
        super().setUp()
        # What the drain and the recheck read is not what these tests are about.
        self.enterContext(mock.patch.object(POWER, "recheck_after_drain", lambda plan: None))

    def person_cordons(self, cordon=True):
        """The Hosts page's Cordon (or Uncordon): the person's own route."""
        handler = LC.ROUTES[("POST", "/api/node/cordon")][1]
        handler(req({"node": "node-a", "cordon": cordon}))

    def during_drain(self, then=DRAINED, *steps):
        def drain(*args, **kwargs):
            for step in steps or (self.person_cordons,):
                step()
            return then() if callable(then) else then
        return drain

    def back(self, item):
        """The host comes back from its restart, and the jobs loop looks."""
        found = self.kube.object(NODE)
        found["status"]["nodeInfo"]["bootID"] = "boot-2"
        self.kube.add(found)
        for _ in range(3):
            OPS.list_operations(wait=False)
        return next(i for i in OPS.stored() if i["id"] == item["id"])

    def test_a_restart_that_nobody_touched_still_uncordons_when_the_host_is_back(self):
        item = self.send(drain=lambda *a, **k: DRAINED)
        self.assertEqual("observing", item["ref"]["phase"])
        self.assertTrue(self.cordoned())
        self.assertEqual({f"node-power:{item['id']}"}, CORDON.holders(self.kube.object(NODE)),
                         "the job's cordon carries its mark")
        item = self.back(item)
        self.assertEqual("succeeded", item["status"], item["message"])
        self.assertFalse(self.cordoned())
        self.assertIn("Scheduling is allowed on it again", item["message"])

    def test_a_restart_keeps_a_cordon_a_person_put_on_while_it_ran(self):
        item = self.send(drain=self.during_drain())
        self.assertEqual("observing", item["ref"]["phase"])
        item = self.back(item)
        self.assertEqual("succeeded", item["status"], item["message"])
        self.assertTrue(self.cordoned(), "the person's cordon stays")
        self.assertIn("left as someone set it", item["message"])
        self.assertNotIn("Scheduling is allowed on it again", item["message"])

    def test_a_restart_keeps_a_cordon_a_person_took_off_and_put_on_again(self):
        item = self.send(drain=self.during_drain(DRAINED, lambda: self.person_cordons(False), self.person_cordons))
        item = self.back(item)
        self.assertTrue(self.cordoned())
        self.assertIn("left as someone set it", item["message"])

    def test_a_restart_stopped_at_its_drain_keeps_a_cordon_a_person_put_on(self):
        item = self.send(drain=self.during_drain(refused))
        self.assertEqual("failed", item["status"])
        self.assertTrue(self.cordoned(), "the person's cordon stays")
        self.assertTrue(item["message"].startswith("Nothing was sent. The host was left as someone set it"), item["message"])
        self.assertNotIn("uncordoned", item["message"])

    def test_maintenance_end_keeps_a_cordon_a_person_put_on_during_it(self):
        item = self.send(action="maintenance", drain=lambda *a, **k: DRAINED)
        OPS.list_operations(wait=False)
        self.assertEqual("succeeded", next(i for i in OPS.stored() if i["id"] == item["id"])["status"])
        self.assertIsNotNone(HISTORY.declared_for("node-a"))
        self.person_cordons()
        out = HM._end(req({"node": "node-a"}))
        self.assertTrue(out["ok"], out)
        self.assertTrue(self.cordoned(), "the person's cordon stays")
        self.assertIn("left as someone set it", out["detail"])
        self.assertIsNone(HISTORY.declared_for("node-a"), "the maintenance still ends")

    def test_maintenance_end_that_nobody_touched_still_uncordons(self):
        self.send(action="maintenance", drain=lambda *a, **k: DRAINED)
        out = HM._end(req({"node": "node-a"}))
        self.assertTrue(out["ok"], out)
        self.assertFalse(self.cordoned())
        self.assertIn("Scheduling is allowed on it again", out["detail"])
        self.assertIsNone((self.kube.object(NODE)["metadata"].get("annotations") or {}).get(CORDON.MARK),
                          "no mark is left behind")


    def test_the_hosts_page_names_no_ended_job_while_its_maintenance_is_open(self):
        """The maintenance job ends when the maintenance opens; its mark must
        not read as a cordon left by a job that has ended (whose advice is to
        uncordon the host), while the maintenance is still open."""
        self.send(action="maintenance", drain=lambda *a, **k: DRAINED)
        OPS.list_operations(wait=False)
        self.assertIsNotNone(HISTORY.declared_for("node-a"))
        self.assertIsNone(NODES.cordon_holder(self.kube.object(NODE), OPS.stored()),
                          "the page shows the maintenance, not an ended job to check and uncordon")
        # What the page shows instead: cordoned, with no job named, and the maintenance as open.
        declared = HM._get(req(node="node-a"))["declared"]
        self.assertEqual(("open", "Drive swap"), (declared["state"], declared["reason"]))

    def evicted_mid_drain(self):
        class Evicted(BaseException):
            """The replica running the job goes mid-drain: nothing is recorded as failed."""

        def evicted(*args, **kwargs):
            raise Evicted()
        with self.assertRaises(Evicted):
            self.send(drain=evicted)
        return next(i for i in OPS.stored() if i["ref"].get("node") == "node-a")

    def resume(self, item, drain):
        with mock.patch.object(NODE_ACTIONS, "drain", drain):
            try:
                JOBS.run_power_job(item["id"], item["ref"]["plan"], resumed=True)
            except JOBS.PowerNotSent:
                pass
        return next(i for i in OPS.stored() if i["id"] == item["id"])

    def test_a_job_that_died_before_recording_its_mark_still_knows_its_cordon(self):
        item = self.evicted_mid_drain()
        with OPS.transaction() as t:      # it died between the mark and its record
            for i in t.items:
                if i["id"] == item["id"]:
                    i["ref"].pop("own_cordon", None)
                    i["ref"].pop("cordon_held", None)
            t.save(t.items)
        item = self.resume(item, refused)
        self.assertFalse(self.cordoned(), "the cordon with its own mark is its own")
        self.assertEqual(set(), CORDON.holders(self.kube.object(NODE)), "and no mark is left behind")

    def test_a_resumed_job_marks_again_the_cordon_it_puts_back_after_a_persons_uncordon(self):
        item = self.evicted_mid_drain()
        self.person_cordons(False)
        item = self.resume(item, lambda *a, **k: DRAINED)
        self.assertEqual({f"node-power:{item['id']}"}, CORDON.holders(self.kube.object(NODE)))
        item = self.back(item)
        self.assertFalse(self.cordoned(), "the cordon the job put back is its own")
        self.assertIn("Scheduling is allowed on it again", item["message"])

    def test_a_node_that_cannot_be_read_at_the_cordon_step_is_still_uncordoned_after_an_ordinary_restart(self):
        self.before_job.append(lambda: self.kube.fail(NODE, 500, method="GET", times=1))
        item = self.send(drain=lambda *a, **k: DRAINED)
        self.assertEqual("observing", item["ref"]["phase"], item["message"])
        item = self.back(item)
        self.assertFalse(self.cordoned(), "a brief read failure does not leave the host cordoned")
        self.assertIn("Scheduling is allowed on it again", item["message"])

    def test_a_persons_cordon_found_when_the_job_carries_on_stays(self):
        item = self.evicted_mid_drain()
        self.person_cordons()
        item = self.resume(item, lambda *a, **k: DRAINED)
        self.assertTrue(item["ref"].get("cordon_taken"))
        item = self.back(item)
        self.assertTrue(self.cordoned())
        self.assertIn("left as someone set it", item["message"])

    def test_an_uncordon_after_a_lost_mark_is_said_when_the_cordon_step_then_fails(self):
        # The mark applies and its reply is lost; release() takes it off; the cordon step's own write fails.
        self.kube.fail(NODE, 500, method="PATCH", times=1, applied=True)
        self.kube.fail(NODE, 500, method="PATCH", times=1, skip=1)
        item = self.send(drain=lambda *a, **k: DRAINED)
        self.assertEqual("failed", item["status"])
        self.assertFalse(self.cordoned())
        self.assertIn("The host is not cordoned", item["message"])
        self.assertNotIn("stays cordoned", item["message"])

    def test_a_job_from_before_the_mark_takes_the_old_path(self):
        item = self.send(drain=lambda *a, **k: DRAINED)
        with OPS.transaction() as t:      # what an older Homestead recorded, and its unmarked cordon
            for i in t.items:
                if i["id"] == item["id"]:
                    i["ref"].pop("cordon_held", None)
            t.save(t.items)
        self.person_cordons()
        item = self.back(item)
        self.assertFalse(self.cordoned(), "uncordoned by uid, as on dev")
        self.assertIn("Scheduling is allowed on it again", item["message"])

    def test_a_maintenance_from_before_the_mark_ends_by_the_old_path(self):
        HISTORY.maintenance_declare("node-a", "Drive swap", "op-old", 2 ** 31, cordoned_before=False, node_uid="uid-node-a")
        self.cordon_by_hand()
        out = HM._end(req({"node": "node-a"}))
        self.assertTrue(out["ok"], out)
        self.assertFalse(self.cordoned())
        self.assertIn("Scheduling is allowed on it again", out["detail"])

    def test_a_mark_whose_reply_was_lost_is_released_and_the_host_stays_cordoned(self):
        # The mark's write applies and its answer is lost; the job's own cordon step then cordons.
        self.kube.fail(NODE, 500, method="PATCH", times=1, applied=True)
        item = self.send(drain=lambda *a, **k: DRAINED)
        self.assertTrue(self.cordoned())
        self.assertEqual(set(), CORDON.holders(self.kube.object(NODE)), "no mark is left to turn stale")
        item = self.back(item)
        self.assertTrue(self.cordoned(), "a cordon the job cannot tell is its own stays")
        self.assertIn("could not mark its cordon", item["message"])
        self.assertIn("Uncordon it on the Hosts page", item["message"])

    def test_a_cordon_another_job_still_holds_is_named_by_its_title(self):
        def another_job_joins():
            with OPS.transaction() as t:
                t.items.append({"id": "j9", "kind": "host-os", "title": "OS update on node-a", "status": "running",
                                "resource": {"kind": "Node", "name": "node-a"}, "ref": {"node": "node-a"}})
                t.save(t.items)
            self.assertTrue(CORDON.cordon("node-a", "host-os", "j9").done)
        item = self.send(drain=self.during_drain(DRAINED, another_job_joins))
        item = self.back(item)
        self.assertTrue(self.cordoned())
        self.assertIn("OS update on node-a", item["message"])
        self.assertNotIn("host-os:j9", item["message"])
        self.assertIn("Hosts page", item["message"])


class HeldRestartTests(MAINTENANCE_TESTS.Case):
    """A restart that stopped an app to wait for the host: when the host is
    back it is uncordoned before what waited starts, unless a person cordoned it."""
    objects = MAINTENANCE_TESTS.ExtendAndEndTests.objects

    def setUp(self):
        super().setUp()
        for target, name, value in ((POWER, "power_enabled", lambda: True), (LC, "NODE_POWER_ENABLED", True),
                                    (POWER, "recheck_after_drain", lambda plan: None)):
            self.enterContext(mock.patch.object(target, name, value))
        self.stops_for_real()

    def restart(self, during=None):
        pick = {i["id"]: "wait" for i in self.plan(action="reboot")["hold"]}
        review = self.plan(action="reboot")
        real = NODE_ACTIONS.drain

        def drain(*args, **kwargs):
            if during:
                during()
            return real(*args, **kwargs)
        body = {"node": "node-a", "confirm": "node-a", "action": "reboot", "choices": pick,
                "review_token": review["review_token"], "allow_drain_blockers": []}
        with mock.patch.object(NODE_ACTIONS, "drain", drain):
            reply = JOBS._send_power(MAINTENANCE_TESTS.req(body))
        item = self.job(reply)
        self.assertTrue(item["ref"].get("held"), "the app waits for the host")
        self.edit(lambda n: n["status"]["nodeInfo"].update(bootID="boot-2"))
        for _ in range(3):
            self.advance()
        return next(i for i in OPS.stored() if i["id"] == item["id"])

    def person_cordons(self):
        LC.ROUTES[("POST", "/api/node/cordon")][1](req({"node": "node-a", "cordon": True}))

    def test_nobody_touched_it_so_it_is_uncordoned_and_what_waited_starts(self):
        item = self.restart()
        self.assertEqual("succeeded", item["status"], item["message"])
        self.assertFalse(self.cordoned())
        self.assertEqual(1, self.web_replicas())

    def test_a_person_cordoned_it_so_it_stays_cordoned_and_the_job_says_what_waits(self):
        item = self.restart(during=self.person_cordons)
        self.assertEqual("succeeded", item["status"], item["message"])
        self.assertTrue(self.cordoned(), "the person's cordon stays")
        self.assertEqual(1, self.web_replicas(), "what waited is started")
        self.assertIn("left as someone set it", item["message"])
        self.assertIn("once it is uncordoned", item["message"])

    def test_a_person_uncordoned_it_so_the_job_says_nothing_of_waiting_for_an_uncordon(self):
        item = self.restart(during=lambda: (self.person_cordons(), LC.ROUTES[("POST", "/api/node/cordon")][1](
            req({"node": "node-a", "cordon": False}))))
        self.assertFalse(self.cordoned())
        self.assertIn("left as someone set it", item["message"])
        self.assertNotIn("once it is uncordoned", item["message"])

    def test_a_give_back_that_failed_is_tried_again_on_the_next_look(self):
        pick = {i["id"]: "wait" for i in self.plan(action="reboot")["hold"]}
        review = self.plan(action="reboot")
        body = {"node": "node-a", "confirm": "node-a", "action": "reboot", "choices": pick,
                "review_token": review["review_token"], "allow_drain_blockers": []}
        item = self.job(JOBS._send_power(MAINTENANCE_TESTS.req(body)))
        self.edit(lambda n: n["status"]["nodeInfo"].update(bootID="boot-2"))
        self.kube.fail(MAINTENANCE_TESTS.NODES + "/node-a", 500, method="PATCH", times=1)
        self.advance()
        item = next(i for i in OPS.stored() if i["id"] == item["id"])
        self.assertEqual("running", item["status"])
        self.assertTrue(self.cordoned())
        self.assertEqual(0, self.web_replicas(), "nothing started before the cordon is given back")
        for _ in range(3):
            self.advance()
        item = next(i for i in OPS.stored() if i["id"] == item["id"])
        self.assertEqual("succeeded", item["status"], item["message"])
        self.assertFalse(self.cordoned())
        self.assertEqual(1, self.web_replicas())

    def web_replicas(self):
        return self.kube.object("/apis/apps/v1/namespaces/apps/deployments/web")["spec"]["replicas"]


class HolderWordsTests(unittest.TestCase):
    """A cordon held by another job is named as the Hosts page names it:
    by the job's title, never a raw kind or <kind>:<id>."""
    def words(self, reason, jobs=()):
        with mock.patch.object(CORDON, "release", lambda *a, **k: CORDON.Outcome(False, "node-a", reason)), \
             mock.patch.object(OPS, "stored", lambda: list(jobs)):
            what, words, _ = JOBS.give_back("node-a", "op-1", "uid-node-a")
        self.assertEqual("kept", what)
        return words

    def test_a_mark_of_a_listed_job_is_its_title(self):
        self.assertIn("OS update on node-a holds it",
                      self.words("still held by host-os:j9", [{"id": "j9", "kind": "host-os", "title": "OS update on node-a"}]))

    def test_a_mark_or_a_kind_with_no_job_listed_is_another_job(self):
        for reason in ("still held by host-os:j9", "still held by host-os"):
            words = self.words(reason)
            self.assertIn("another job holds it", words, reason)
            self.assertNotIn("host-os", words)

    def test_a_title_with_a_colon_is_kept_as_it_is(self):
        self.assertIn("Restart: node-a holds it", self.words("still held by Restart: node-a"))


if __name__ == "__main__":
    unittest.main()
