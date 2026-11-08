"""Longhorn's policy for a down node (homestead_lhcapacity): set once so a
failed host lets go of its volumes, and an alert when a down host still holds
some - the outage that left Home Assistant waiting for seven hours."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
import homestead_lhcapacity as LHCAP

PATH = f"{LHCAP.LH}/settings/{LHCAP.NODE_DOWN}"


class Settle:
    def __init__(self, item=None, error=None):
        self.item, self.error, self.sent = item, error, []

    def read(self, path):
        assert path == PATH
        if self.error:
            raise self.error
        return self.item

    def send(self, method, path, body, ctype=None):
        self.sent.append((method, path, body, ctype))


class EnsureTests(unittest.TestCase):
    def run_it(self, item=None, error=None):
        s = Settle(item, error)
        return LHCAP.ensure_node_down(s.read, s.send), s.sent

    def test_longhorns_default_becomes_release_and_is_marked(self):
        for item in ({"value": "do-nothing"}, {"value": "", "default": "do-nothing"}, {}):
            changed, sent = self.run_it(item)
            self.assertTrue(changed, item)
            self.assertEqual([("PATCH", PATH, {"metadata": {"annotations": {LHCAP.NODE_DOWN_MARK: "1"}},
                                               "value": LHCAP.NODE_DOWN_RELEASE}, "application/merge-patch+json")], sent)

    def test_a_policy_someone_chose_is_kept_and_only_marked(self):
        changed, sent = self.run_it({"value": "delete-statefulset-pod"})
        self.assertFalse(changed)
        self.assertNotIn("value", sent[0][2])

    def test_once_marked_it_is_never_touched_again_even_at_do_nothing(self):
        changed, sent = self.run_it({"metadata": {"annotations": {LHCAP.NODE_DOWN_MARK: "1"}}, "value": "do-nothing"})
        self.assertEqual((False, []), (changed, sent))

    def test_an_unread_setting_changes_nothing_and_is_tried_again(self):
        self.assertEqual((None, []), self.run_it(error=OSError("no Longhorn")))


def node(name, ready, since="2026-11-08T01:32:32Z"):
    return {"metadata": {"name": name}, "status": {"conditions": [
        {"type": "Ready", "status": "True" if ready else "Unknown", "lastTransitionTime": since}]}}


def pv(name, claim, modes=("ReadWriteOnce",)):
    return {"metadata": {"name": name}, "spec": {"accessModes": list(modes), "claimRef": {"namespace": "lab", "name": claim}}}


def attached(pv_name, node_name, attacher="driver.longhorn.io", is_attached=True):
    return {"spec": {"nodeName": node_name, "attacher": attacher, "source": {"persistentVolumeName": pv_name}},
            "status": {"attached": is_attached}}


DOWN_AT = 1794101552.0      # 2026-11-08T01:32:32Z
PARSE = lambda text: DOWN_AT if text == "2026-11-08T01:32:32Z" else float(text)


class HeldTests(unittest.TestCase):
    PVS = [pv("pvc-ha", "home-assistant-config"), pv("pvc-mq", "mosquitto-data"), pv("pvc-nas", "nas-media", ("ReadWriteMany",))]

    def held(self, attachments, nodes, now=DOWN_AT + 3600, policy="do-nothing"):
        return LHCAP.held_volumes(attachments, nodes, self.PVS, policy, now, PARSE)

    def test_a_down_host_holding_single_host_volumes_is_named_with_them(self):
        held = self.held([attached("pvc-ha", "node2"), attached("pvc-mq", "node2"), attached("pvc-nas", "node2")],
                         [node("node1", True), node("node2", False)])
        self.assertEqual({"node2": {"claims": ["lab/home-assistant-config", "lab/mosquitto-data"], "down_minutes": 60,
                                    "policy": "do-nothing"}}, held)

    def test_within_the_grace_nothing_is_said(self):
        self.assertEqual({}, self.held([attached("pvc-ha", "node2")], [node("node2", False)], now=DOWN_AT + 60))

    def test_ready_hosts_other_drivers_and_detached_volumes_are_left_out(self):
        self.assertEqual({}, self.held([attached("pvc-ha", "node1"), attached("pvc-mq", "node2", attacher="nfs.csi"),
                                        attached("pvc-ha", "node2", is_attached=False)],
                                       [node("node1", True), node("node2", False)]))

    def test_the_alert_says_what_waits_and_the_way_out(self):
        fact, = LHCAP.held_alert_facts(self.held([attached("pvc-ha", "node2")], [node("node2", False)]))
        self.assertEqual(("held-volumes:node2", "critical", "outage"), (fact["key"], fact["severity"], fact["category"]))
        self.assertEqual("node2 is down; 1 volume is still attached to it", fact["title"])
        self.assertIn("lab/home-assistant-config", fact["body"])
        self.assertIn("Allow apps with storage to move off a failed host", fact["body"])
        self.assertLessEqual(len(fact["body"].split(". ")[0]), 140)

    def test_with_the_policy_set_it_points_at_longhorn_instead(self):
        fact, = LHCAP.held_alert_facts(self.held([attached("pvc-ha", "node2")], [node("node2", False)],
                                                 policy=LHCAP.NODE_DOWN_RELEASE))
        self.assertIn("longhorn-manager", fact["body"])
        self.assertNotIn("Allow apps with storage", fact["body"])


if __name__ == "__main__":
    unittest.main()
