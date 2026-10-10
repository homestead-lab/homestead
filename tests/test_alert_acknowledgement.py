"""Acknowledging known conditions must not hide deterioration or other users' alerts."""
import json
import sys
import tempfile
import threading
import unittest
from http.client import HTTPConnection
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
import homestead_alerts as A
import homestead_push as P
import homestead_http as HTTP
import server


def disk(count=24, pending=0):
    report={"available":True,"reallocated":count,"pending":pending}
    issues=server.smart_disk_issues(report)
    return A.health_facts({"health_issues":[dict(i,kind="Disk",name="node/sda") for i in issues]})

class AcknowledgementTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        A.bind(self.temp.name);P.bind(self.temp.name)
        self.now=1000
        self.tick(disk());self.tick(disk(),60)
    def tick(self,facts,seconds=20):
        self.now+=seconds
        return A.observe({"health":facts},self.now)
    def ack(self,user="alice"):
        row=A.active(user=user)[0]
        A.acknowledge(user,row["key"],row["version"],now=self.now)
        return row
    def test_unchanged_disk_stays_acknowledged_across_reload_and_devices(self):
        self.ack();A.bind(self.temp.name)
        for _ in range(5):self.assertEqual([],self.tick(disk(),120))
        self.assertTrue(A.active(user="alice")[0]["acknowledged"])
        self.assertFalse(A.active(user="bob")[0]["acknowledged"])
        self.assertEqual([],A.for_user(A.log()["alerts"],"alice"))
        for endpoint in ("phone","tablet"):
            P.subscribe("alice",{"endpoint":"https://fcm.googleapis.com/"+endpoint},["degraded"])
            answer=P.pending("alice","https://fcm.googleapis.com/"+endpoint)
            self.assertEqual([],answer["alerts"]);self.assertEqual(0,answer["active"])
    def test_a_single_sector_increase_rearms_after_hold_once(self):
        self.ack();self.assertEqual([],self.tick(disk(25)))
        fresh=self.tick(disk(25),60)
        self.assertEqual(["worsened"],[e["phase"] for e in fresh])
        self.assertEqual([],self.tick(disk(25),120))
        self.assertFalse(A.active(user="alice")[0]["acknowledged"])
        self.ack();self.assertEqual([],self.tick(disk(25),120))
    def test_new_problem_and_severity_increase_are_not_hidden_by_old_counter(self):
        facts=disk(24,1);self.assertEqual(1,len(facts));self.assertEqual("critical",facts[0]["severity"])
        self.assertEqual({"reallocated":24,"pending":1},facts[0]["signals"])
        self.ack();self.tick(facts);self.assertEqual("worsened",self.tick(facts,60)[0]["phase"])
    def test_brief_worsening_does_not_notify_and_does_not_remove_ack(self):
        self.ack();self.tick(disk(25));self.tick(disk(24));self.assertEqual([],self.tick(disk(24),120))
        self.assertTrue(A.active(user="alice")[0]["acknowledged"])
    def test_lower_acknowledged_level_can_rearm_below_previous_peak(self):
        self.tick(disk(10));self.ack();self.tick(disk(11))
        self.assertEqual("worsened",self.tick(disk(11),60)[0]["phase"])
    def test_recovery_then_recurrence_is_a_new_unacknowledged_incident(self):
        self.ack();self.tick([]);self.assertEqual("resolved",self.tick([],60)[0]["phase"])
        self.tick(disk());self.assertEqual("raised",self.tick(disk(),60)[0]["phase"])
        self.assertFalse(A.active(user="alice")[0]["acknowledged"])
    def test_ack_stale_review_is_rejected_and_undo_is_personal(self):
        row=self.ack();self.tick(disk(25))
        with self.assertRaises(A.AlertChanged):A.acknowledge("alice",row["key"],row["version"])
        row=A.active(user="alice")[0];A.acknowledge("alice",row["key"],row["version"],undo=True)
        self.assertFalse(A.active(user="alice")[0]["acknowledged"])
    def test_unknown_observations_do_not_count_as_recovery_time(self):
        self.ack();self.tick([]);self.tick(None,120);self.assertEqual([],self.tick([],20))
        self.assertEqual("resolved",self.tick([],60)[0]["phase"])
    def test_missing_disk_probe_retains_its_warning_but_other_resources_resolve(self):
        unknown=A.health_facts({"nodes":[{"name":"node","temps":None}]})
        self.tick(unknown);self.assertEqual([],self.tick(unknown,120));self.assertEqual(1,len(A.active()))
    def test_addresses_source_failure_preserves_legacy_singular_key(self):
        fact={"key":"address:192.0.2.1","category":"degraded","severity":"degraded","title":"No route"}
        A.observe({"addresses":[fact]},100);A.observe({"addresses":[fact]},200)
        with A._lock:
            state=A._load();state["active"][fact["key"]].pop("source");A._save(state)
        A.observe({"addresses":None},300);self.assertEqual([],A.observe({"addresses":None},400))
        self.assertTrue(any(r["key"]==fact["key"] for r in A.active()))
    def test_new_worker_advances_only_after_confirmed_display_and_backlogs_coalesce(self):
        endpoint="https://fcm.googleapis.com/a";P.subscribe("alice",{"endpoint":endpoint},["degraded"])
        first=P.pending("alice",endpoint,True)
        self.assertEqual(first,P.pending("alice",endpoint,True))
        self.assertEqual(0,P.mine("alice",endpoint)["cursor"])
        P.advance("alice",endpoint,first["latest"])
        self.assertEqual([],P.pending("alice",endpoint,True)["alerts"])
        self.tick([]);self.tick([],60)
        answer=P.pending("alice",endpoint,True)
        self.assertEqual(["resolved"],[e["phase"] for e in answer["alerts"]])
    def test_legacy_history_gains_metrics_without_a_false_worsening(self):
        with A._lock:
            state=A._load();row=next(iter(state["active"].values()))
            row.pop("signals");row.pop("notice");A._save(state)
        self.assertEqual([],self.tick(disk()))
        self.assertEqual([],self.tick(disk(),120))
        self.tick(disk(25));self.assertEqual("worsened",self.tick(disk(25),60)[0]["phase"])
    def test_replacement_disk_does_not_inherit_acknowledgement(self):
        def device(serial):
            nodes=[{"name":"node","status":"Ready","disk_issues":[dict(i,disk="sda",device_identity=serial) for i in server.smart_disk_issues({"available":True,"reallocated":24})]}]
            report=server.classify_cluster_health(nodes,[],[])
            return A.health_facts({"health_issues":report["health_issues"]})
        self.tick(device("drive-a"));self.tick(device("drive-a"),60);self.ack()
        self.tick(device("drive-b"));fresh=self.tick(device("drive-b"),60)
        self.assertEqual("worsened",fresh[0]["phase"])
        self.assertFalse(A.active(user="alice")[0]["acknowledged"])
    def test_deleting_account_forgets_its_acknowledgements(self):
        self.ack();A.forget_user("alice")
        self.assertFalse(A.active(user="alice")[0]["acknowledged"])
    def test_badge_counts_active_conditions_independently_of_device_categories(self):
        endpoint="https://fcm.googleapis.com/jobs-only"
        P.subscribe("alice",{"endpoint":endpoint},["jobs"])
        answer=P.pending("alice",endpoint,True)
        self.assertEqual([],answer["alerts"]);self.assertEqual(1,answer["active"])
    def test_http_identity_csrf_conflicts_and_delivery_ownership(self):
        listener=HTTP.BoundedHTTPServer(("127.0.0.1",0),server.H,max_connections=2)
        threading.Thread(target=listener.serve_forever,daemon=True).start()
        self.addCleanup(listener.server_close);self.addCleanup(listener.shutdown)
        identity=self.enterContext(patch.object(server.H,"_who",return_value={"user":"alice","role":"viewer","stale":False}))
        def post(path,body,csrf=True):
            client=HTTPConnection(*listener.server_address,timeout=3)
            try:
                headers={"Content-Type":"application/json"}
                if csrf:headers["X-Homestead-Auth"]="1"
                client.request("POST",path,json.dumps(body),headers);r=client.getresponse();r.read();return r.status
            finally:client.close()
        row=A.active(user="alice")[0];body={"key":row["key"],"version":row["version"],"user":"bob"}
        self.assertEqual(403,post("/api/alerts/acknowledge",body,False))
        self.assertEqual(200,post("/api/alerts/acknowledge",body))
        self.assertFalse(A.active(user="bob")[0]["acknowledged"])
        self.assertTrue(A.active(user="alice")[0]["acknowledged"])
        self.assertEqual(409,post("/api/alerts/acknowledge",dict(body,version="stale")))
        P.subscribe("bob",{"endpoint":"https://fcm.googleapis.com/bob"},["degraded"])
        self.assertEqual(404,post("/api/alerts/delivered",{"endpoint":"https://fcm.googleapis.com/bob","latest":1}))
        identity.return_value=None;self.assertEqual(401,post("/api/alerts/acknowledge",body))

if __name__=="__main__":unittest.main()


class HoldTests(unittest.TestCase):
    """A condition can ask to be held longer than the usual minute: a
    service address after the cluster starts settles by itself in minutes."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        A.bind(self.temp.name)

    def test_a_longer_hold_is_waited_out_and_one_that_clears_inside_it_is_never_raised(self):
        import homestead_vips as VIPS
        fact = VIPS.alert_facts({"addresses": [{"ip": "192.0.2.108", "state": "unrouted", "reason": "r"}]})
        self.assertEqual([], A.observe({"addresses": fact}, 1000))
        self.assertEqual([], A.observe({"addresses": fact}, 1000 + A.HOLD + 5), "past the usual minute, still held")
        self.assertEqual([], A.observe({"addresses": []}, 1200), "it cleared: never news")
        self.assertEqual([], A.observe({"addresses": []}, 1400))
        A.observe({"addresses": fact}, 2000)
        raised = A.observe({"addresses": fact}, 2000 + VIPS.ADDRESS_HOLD)
        self.assertEqual(["raised"], [e["phase"] for e in raised], "still there after its hold: raised")


class JobAlertTests(unittest.TestCase):
    def test_a_failed_jobs_alert_opens_the_job_not_the_page_it_was_about(self):
        # Seen on a phone: "Balance containers failed" opened Containers, with nothing there saying why.
        fact = A.job_facts([{"id": "7df5", "status": "failed", "title": "Balance containers", "href": "/containers"}])[0]
        self.assertEqual("/?job=7df5", fact["href"])
