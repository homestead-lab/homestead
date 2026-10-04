"""Detached volumes kept at the copies they ask for.

nas-backup sat detached with one of its two copies for hours: Longhorn
rebuilds only while a volume is attached, and its offline rebuilding was off.
A reboot of the host holding that copy then stalled in its drain."""
import copy
import sys
import time
import unittest
import urllib.error
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
import homestead_lhrebuild as R

LH = R.LH


def replica(volume, node, state="stopped", healthy=True, failed=False):
    return {"spec": {"volumeName": volume, "nodeID": node, **({"healthyAt": "2026-10-01T00:00:00Z"} if healthy else {}),
                     **({"failedAt": "2026-10-02T00:00:00Z"} if failed else {})},
            "status": {"currentState": state}}


def volume(name, wanted=2, state="detached", robustness="unknown", claim="nas-backup"):
    return {"metadata": {"name": name}, "spec": {"numberOfReplicas": wanted},
            "status": {"state": state, "robustness": robustness,
                       "kubernetesStatus": {"namespace": "lab", "pvcName": claim}}}


class Cluster:
    def __init__(self, setting="false", annotations=None):
        self.objects = {f"{LH}/volumes": {"items": []}, f"{LH}/replicas": {"items": []},
                        f"{LH}/volumeattachments": {"items": []}}
        if setting is not None:
            self.objects[f"{LH}/settings/{R.SETTING}"] = {"metadata": {"annotations": annotations or {}}, "value": setting}
        self.sent = []

    def get(self, path):
        if path.startswith(f"{LH}/volumeattachments/"):
            name = path.rsplit("/", 1)[1]
            found = next((a for a in self.objects[f"{LH}/volumeattachments"]["items"] if a["metadata"]["name"] == name), None)
            if found:
                return copy.deepcopy(found)
            return {"metadata": {"name": name, "resourceVersion": "1"}, "spec": {"attachmentTickets": {}}}
        if path not in self.objects:
            raise urllib.error.HTTPError(path, 404, "not found", {}, None)
        return copy.deepcopy(self.objects[path])

    def send(self, method, path, body=None, ctype=None):
        self.sent.append((method, path, body))
        return {}


class ShortTests(unittest.TestCase):
    def test_a_stopped_copy_of_a_detached_volume_counts_and_a_failed_one_does_not(self):
        rows = R.short([volume("v1"), volume("v2", claim="archive"), volume("v3", claim="whole")],
                       [replica("v1", "k1"),
                        replica("v2", "k1"), replica("v2", "k2", failed=True),
                        replica("v3", "k1"), replica("v3", "k2")])
        self.assertEqual([("lab/nas-backup", 1, 2, ["k1"]), ("lab/archive", 1, 2, ["k1"])],
                         [(r["claim"], r["whole"], r["wanted"], r["hosts"]) for r in rows])

    def test_two_copies_on_one_host_are_one(self):
        rows = R.short([volume("v1")], [replica("v1", "k1"), replica("v1", "k1")])
        self.assertEqual(1, rows[0]["whole"])

    def test_attached_volumes_are_longhorns_to_rebuild(self):
        self.assertEqual([], R.short([volume("v1", state="attached", robustness="degraded")], [replica("v1", "k1", "running")]))

    def test_no_more_copies_are_wanted_than_there_are_hosts(self):
        rows = R.short([volume("v1", wanted=3)], [replica("v1", "k1"), replica("v1", "k2")], hosts_available=2)
        self.assertEqual([], rows)
        self.assertEqual(1, len(R.short([volume("v1", wanted=3)], [replica("v1", "k1")], hosts_available=2)))

    def test_a_copy_that_never_became_healthy_is_not_one(self):
        self.assertEqual(0, R.short([volume("v1")], [replica("v1", "k1", healthy=False)])[0]["whole"])


class DefaultTests(unittest.TestCase):
    def bind(self, cluster, enabled=True):
        R.bind(cluster.get, cluster.send, lambda: enabled)

    def test_offline_rebuilding_is_turned_on_once(self):
        cluster = Cluster("false")
        self.bind(cluster)
        R.tick()
        method, path, body = cluster.sent[0]
        self.assertEqual((f"{LH}/settings/{R.SETTING}", "true"), (path, body["value"]))
        self.assertIn(R.DEFAULTED, body["metadata"]["annotations"], "marked, so it is never forced back on")

    def test_a_setting_turned_off_after_that_is_left_alone(self):
        cluster = Cluster("false", {R.DEFAULTED: "set"})
        self.bind(cluster)
        R.tick()
        self.assertEqual([], cluster.sent)

    def test_rebuild_now_uses_longhorns_offline_rebuild_where_it_has_one(self):
        cluster = Cluster("false", {R.DEFAULTED: "set"})
        cluster.objects[f"{LH}/volumes"]["items"] = [volume("v1")]
        cluster.objects[f"{LH}/replicas"]["items"] = [replica("v1", "k1")]
        self.bind(cluster)
        R.rebuild_now("v1")
        self.assertEqual([("PATCH", f"{LH}/volumes/v1", {"spec": {"offlineRebuilding": "enabled"}})], cluster.sent)


class FallbackTests(unittest.TestCase):
    """Longhorn without offline rebuilding: Homestead holds a short volume
    attached, with no frontend, until it is whole."""
    def setUp(self):
        R._gave_up.clear()
        self.cluster = Cluster(setting=None)
        self.cluster.objects[f"{LH}/volumes"]["items"] = [volume("v1"), volume("v2", claim="other")]
        self.cluster.objects[f"{LH}/replicas"]["items"] = [replica("v1", "k1"), replica("v2", "k2")]
        R.bind(self.cluster.get, self.cluster.send, lambda: True)

    def held(self, name, since=None, others=()):
        tickets = {R.TICKET: {"id": R.TICKET, "parameters": {"since": str(int(since or time.time()))}}}
        tickets.update({o: {"id": o} for o in others})
        self.cluster.objects[f"{LH}/volumeattachments"]["items"] = [
            {"metadata": {"name": name, "resourceVersion": "2"}, "spec": {"attachmentTickets": tickets}}]

    def test_one_short_volume_at_a_time_is_held_without_a_frontend(self):
        R.tick()
        self.assertEqual(1, len(self.cluster.sent))
        method, path, body = self.cluster.sent[0]
        ticket = body["spec"]["attachmentTickets"][R.TICKET]
        self.assertEqual((f"{LH}/volumeattachments/v1", "k1", "true"),
                         (path, ticket["nodeID"], ticket["parameters"]["disableFrontend"]))

    def test_it_lets_go_when_whole_when_asked_for_or_after_six_hours(self):
        for setup in (lambda: self.cluster.objects[f"{LH}/volumes"]["items"][0]["status"].update(robustness="healthy"),
                      lambda: self.held("v1", others=["csi-abc"]),
                      lambda: self.held("v1", since=time.time() - R.TICKET_LIMIT - 1)):
            self.held("v1")
            setup()
            self.cluster.sent = []
            R.tick()
            self.assertIn(("PATCH", f"{LH}/volumeattachments/v1", {"spec": {"attachmentTickets": {R.TICKET: None}}}),
                          self.cluster.sent)

    def test_a_volume_still_rebuilding_is_kept_and_no_second_one_taken(self):
        self.held("v1")
        self.cluster.objects[f"{LH}/volumes"]["items"][0]["status"].update(state="attached", robustness="degraded")
        R.tick()
        self.assertEqual([], self.cluster.sent)

    def test_turned_off_it_holds_nothing_new(self):
        R.bind(self.cluster.get, self.cluster.send, lambda: False)
        R.tick()
        self.assertEqual([], self.cluster.sent)


class FactTests(unittest.TestCase):
    def test_the_finding_says_whether_anything_will_repair_it(self):
        row = {"name": "v1", "claim": "lab/nas-backup", "whole": 1, "wanted": 2, "hosts": ["k1"], "offline": "ignored"}
        off = R.alert_facts({"enabled": False, "short": [row]})[0]
        self.assertIn("Rebuild it now", off["body"].replace("rebuild it now", "Rebuild it now"))
        on = R.alert_facts({"enabled": True, "short": [row]})[0]
        self.assertIn("offline", on["body"])
        self.assertEqual("critical", R.alert_facts({"enabled": True, "short": [{**row, "whole": 0, "hosts": []}]})[0]["severity"])


if __name__ == "__main__":
    unittest.main()
