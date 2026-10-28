"""Detached volumes kept at the copies they ask for.

nas-backup sat detached with one of its two copies for hours: Longhorn
rebuilds only while a volume is attached, and its offline rebuilding was off.
A reboot of the host holding that copy then stalled in its drain."""
import itertools
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
import homestead_kube as K
import homestead_lhrebuild as R
import homestead_changes as CHANGES
import homestead_host_access as HOSTACCESS
import homestead_operations as OPS
from fakes.kube import Kube

LH = R.LH
SERIAL = itertools.count(1)


def replica(volume, node, state="stopped", healthy=True, failed=False):
    return {"apiVersion": "longhorn.io/v1beta2", "kind": "Replica",
            "metadata": {"namespace": "longhorn-system", "name": f"{volume}-r-{next(SERIAL)}"},
            "spec": {"volumeName": volume, "nodeID": node, **({"healthyAt": "2026-10-01T00:00:00Z"} if healthy else {}),
                     **({"failedAt": "2026-10-02T00:00:00Z"} if failed else {})},
            "status": {"currentState": state}}


def volume(name, wanted=2, state="detached", robustness="unknown", claim="nas-backup"):
    return {"apiVersion": "longhorn.io/v1beta2", "kind": "Volume",
            "metadata": {"namespace": "longhorn-system", "name": name}, "spec": {"numberOfReplicas": wanted},
            "status": {"state": state, "robustness": robustness,
                       "kubernetesStatus": {"namespace": "lab", "pvcName": claim}}}


class Cluster:
    """Longhorn's volumes, replicas, attachments and offline-rebuilding setting
    on the shared fake API (tests/fakes/kube.py)."""

    def __init__(self, case, setting="false", annotations=None):
        # The shared fake runs the real write guards; the storage guard takes the
        # job store's lock, which lives under its DATA_DIR.
        case.enterContext(mock.patch.object(OPS, "DATA_DIR", case.enterContext(tempfile.TemporaryDirectory())))
        # No signed-in person on this thread, whatever an earlier test left behind: the write guards read who is asking.
        case.enterContext(mock.patch.object(HOSTACCESS, "role", lambda: None))
        case.enterContext(mock.patch.object(CHANGES.REQUEST, "user", None, create=True))
        self.kube = case.enterContext(Kube())
        if setting is not None:
            self.kube.add({"apiVersion": "longhorn.io/v1beta2", "kind": "Setting",
                           "metadata": {"namespace": "longhorn-system", "name": R.SETTING,
                                        "annotations": annotations or {}},
                           "value": setting})

    def volumes(self, *volumes):
        """Each volume with the attachment Longhorn keeps for it (no tickets yet)."""
        for each in volumes:
            self.kube.add(each)
            self.attachment(each["metadata"]["name"])

    def replicas(self, *replicas):
        for each in replicas:
            self.kube.add(each)

    def attachment(self, name, tickets=None):
        self.kube.add({"apiVersion": "longhorn.io/v1beta2", "kind": "VolumeAttachment",
                       "metadata": {"namespace": "longhorn-system", "name": name},
                       "spec": {"attachmentTickets": tickets or {}}})

    def status(self, name, **status):
        path = f"{LH}/volumes/{name}"
        found = self.kube.object(path)
        found["status"].update(status)
        self.kube.add(found)

    @property
    def sent(self):
        return [(method, path, body) for method, path, body, _ in self.kube.writes]


class RebuildCase(unittest.TestCase):
    def bind(self, enabled=True):
        for name, value in (("kget", R.kget), ("ksend", R.ksend), ("_enabled", R._enabled)):
            self.addCleanup(setattr, R, name, value)
        R.bind(K.kget, K.ksend, lambda: enabled)


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


class DefaultTests(RebuildCase):
    def test_offline_rebuilding_is_turned_on_once(self):
        cluster = Cluster(self, "false")
        self.bind()
        R.tick()
        method, path, body = cluster.sent[0]
        self.assertEqual((f"{LH}/settings/{R.SETTING}", "true"), (path, body["value"]))
        self.assertIn(R.DEFAULTED, body["metadata"]["annotations"], "marked, so it is never forced back on")
        self.assertEqual("true", cluster.kube.object(path)["value"])

    def test_a_setting_turned_off_after_that_is_left_alone(self):
        cluster = Cluster(self, "false", {R.DEFAULTED: "set"})
        self.bind()
        R.tick()
        self.assertEqual([], cluster.sent)

    def test_rebuild_now_uses_longhorns_offline_rebuild_where_it_has_one(self):
        cluster = Cluster(self, "false", {R.DEFAULTED: "set"})
        cluster.volumes(volume("v1"))
        cluster.replicas(replica("v1", "k1"))
        self.bind()
        R.rebuild_now("v1")
        self.assertEqual([("PATCH", f"{LH}/volumes/v1", {"spec": {"offlineRebuilding": "enabled"}})], cluster.sent)


class FallbackTests(RebuildCase):
    """Longhorn without offline rebuilding: Homestead holds a short volume
    attached, with no frontend, until it is whole."""
    def setUp(self):
        R._gave_up.clear()
        self.cluster = Cluster(self, setting=None)
        self.cluster.volumes(volume("v1"), volume("v2", claim="other"))
        self.cluster.replicas(replica("v1", "k1"), replica("v2", "k2"))
        self.bind()

    def held(self, name, since=None, others=()):
        tickets = {R.TICKET: {"id": R.TICKET, "parameters": {"since": str(int(since or time.time()))}}}
        tickets.update({o: {"id": o} for o in others})
        self.cluster.attachment(name, tickets)

    def test_one_short_volume_at_a_time_is_held_without_a_frontend(self):
        R.tick()
        self.assertEqual(1, len(self.cluster.sent))
        method, path, body = self.cluster.sent[0]
        ticket = body["spec"]["attachmentTickets"][R.TICKET]
        self.assertEqual((f"{LH}/volumeattachments/v1", "k1", "true"),
                         (path, ticket["nodeID"], ticket["parameters"]["disableFrontend"]))
        self.assertIn(R.TICKET, self.cluster.kube.object(path)["spec"]["attachmentTickets"])

    def test_it_lets_go_when_whole_when_asked_for_or_after_six_hours(self):
        for setup in (lambda: self.cluster.status("v1", robustness="healthy"),
                      lambda: self.held("v1", others=["csi-abc"]),
                      lambda: self.held("v1", since=time.time() - R.TICKET_LIMIT - 1)):
            self.held("v1")
            setup()
            self.cluster.kube.writes.clear()
            R.tick()
            self.assertIn(("PATCH", f"{LH}/volumeattachments/v1", {"spec": {"attachmentTickets": {R.TICKET: None}}}),
                          self.cluster.sent)

    def test_a_null_volume_list_lets_go_of_nothing(self):
        """A held volume missing from the list is let go: an answer of null
        is not an empty list."""
        self.held("v1")

        def kget(path, *args, **kwargs):
            # The API server never answers null; a proxy or a bug could.
            return None if path == f"{LH}/volumes" else K.kget(path, *args, **kwargs)
        self.enterContext(mock.patch.object(R, "kget", kget))
        with self.assertRaises(AttributeError):
            R.tick()
        self.assertEqual([], self.cluster.sent)

    def test_a_volume_still_rebuilding_is_kept_and_no_second_one_taken(self):
        self.held("v1")
        self.cluster.status("v1", state="attached", robustness="degraded")
        R.tick()
        self.assertEqual([], self.cluster.sent)

    def test_turned_off_it_holds_nothing_new(self):
        self.bind(enabled=False)
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
