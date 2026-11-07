"""A host preference says whose it is, and shows where a person looks.

Balance hosts leaves a weighted preference on each app it moves, on Flux apps
too. Nothing told a person: Edit said "any host", the row said nothing, and
nothing could tell Balance's own preference from one a person chose. Balance now
marks its own on the Deployment's metadata, which restarts nothing; a person's
Move or Edit that writes a preference takes the mark off."""
import copy
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))

import homestead_container_rebalance as C
import homestead_deploy_server as DEPLOY_SERVER
import homestead_kube as K
import homestead_names as NAMES
import homestead_place as PLACE
import homestead_workload_edit as WORKLOAD_EDIT
import server
from fakes.kube import Kube
from rebind import keep_bound

WORKLOADS = server.WORKLOADS
MARK = NAMES.key("balance-preference")


def preference(*hosts, weight=100, key="kubernetes.io/hostname"):
    return {"weight": weight, "preference": {"matchExpressions": [{"key": key, "operator": "In", "values": list(hosts)}]}}


def deployment(name="app-a", prefers=(), mark=None, selector=None):
    spec = {"containers": [{"name": name, "image": "example.com/app:1"}]}
    if prefers:
        spec["affinity"] = {"nodeAffinity": {"preferredDuringSchedulingIgnoredDuringExecution": list(prefers)}}
    if selector:
        spec["nodeSelector"] = {"kubernetes.io/hostname": selector}
    return {"apiVersion": "apps/v1", "kind": "Deployment",
            "metadata": {"name": name, "namespace": "lab", "labels": {}, "annotations": {MARK: mark} if mark else {}},
            "spec": {"replicas": 1, "selector": {"matchLabels": {"app": name}},
                     "template": {"metadata": {"labels": {"app": name}}, "spec": spec}},
            "status": {"readyReplicas": 1}}


class WhichHost(unittest.TestCase):
    def test_a_host_preference_is_read_from_the_template(self):
        spec = deployment(prefers=[preference("host-a")])["spec"]["template"]["spec"]
        self.assertEqual("host-a", PLACE.preferred_host(spec))

    def test_the_heaviest_host_preference_wins(self):
        terms = [preference("host-a", weight=10), preference("host-b", weight=100)]
        for prefers in (terms, terms[::-1]):
            spec = deployment(prefers=prefers)["spec"]["template"]["spec"]
            self.assertEqual("host-b", PLACE.preferred_host(spec))

    def test_a_preference_for_several_hosts_or_a_label_is_not_one_host(self):
        for prefers in ([preference("host-a", "host-b")], [preference("yes", key="hardware/igpu")], []):
            spec = deployment(prefers=prefers)["spec"]["template"]["spec"]
            self.assertEqual("", PLACE.preferred_host(spec), prefers)


class WhoseItIs(unittest.TestCase):
    def test_balance_hosts_marks_its_own_on_the_metadata_alone(self):
        current = deployment()
        proposed = copy.deepcopy(current)
        PLACE.apply_node_placement(proposed, "host-b")
        sent = []
        with mock.patch.object(WORKLOADS, "move_capacity_plan", lambda body: (copy.deepcopy(proposed), {"blocked": False}, None)), \
                mock.patch.object(DEPLOY_SERVER, "ksend", lambda *args: sent.append(args), create=True), \
                mock.patch.object(PLACE, "_bust", lambda *args: None):
            DEPLOY_SERVER.rebalance_move("lab", "app-a", "host-b")
        self.assertEqual("PUT", sent[0][0])
        put = sent[0][2]
        self.assertEqual("host-b", put["metadata"]["annotations"][MARK])
        self.assertNotIn(MARK, put["spec"]["template"]["metadata"].get("annotations", {}),
                         "on the pod template the mark would restart the app")
        self.assertEqual("host-b", PLACE.balance_preference(put))

    def test_a_persons_bulk_move_takes_the_mark_off_and_leaves_none(self):
        # The Move dialog runs the same job: what it writes is a person's choice.
        proposed = deployment(prefers=[preference("host-a")], mark="host-a")
        PLACE.apply_node_placement(proposed, "host-b")
        sent = []
        with mock.patch.object(WORKLOADS, "move_capacity_plan", lambda body: (copy.deepcopy(proposed), {"blocked": False}, None)), \
                mock.patch.object(DEPLOY_SERVER, "ksend", lambda *args: sent.append(args), create=True), \
                mock.patch.object(PLACE, "_bust", lambda *args: None):
            DEPLOY_SERVER.rebalance_move("lab", "app-a", "host-b", mark=False)
        self.assertNotIn(MARK, sent[0][2]["metadata"]["annotations"])
        self.assertEqual("", PLACE.balance_preference(sent[0][2]))

    def test_only_balances_own_plan_asks_for_a_mark(self):
        # The job, as the Move dialog and Balance hosts start it.
        made = []

        def apply(ns, name, to, mark=True):
            made.append(mark)
            raise ValueError("stop here")
        keep_bound(self, C)
        with Kube(deployment("app-a"), {"apiVersion": "v1", "kind": "Pod", "metadata": {"namespace": "lab", "name": "p", "labels": {"app": "app-a"}},
                                        "spec": {"nodeName": "host-a"}, "status": {"phase": "Running"}}):
            C.bind(K.kget, lambda dep: {}, lambda n, r: (True, []), apply, ("lab", "homestead"), lambda: True)
            for stop_on_failure in (True, False):
                item = {"id": "job", "ref": {"moves": [{"ns": "lab", "name": "app-a", "id": "lab/app-a", "from": "host-a", "to": "host-b"}],
                                             "index": 0, "moved": 0, "stage": "move", "results": [], "pulled": 0,
                                             "stop_on_failure": stop_on_failure}}
                C.status(item)
        self.assertEqual([False, True], made)

    def test_an_unmarked_preference_is_a_persons(self):
        self.assertEqual("", PLACE.balance_preference(deployment(prefers=[preference("host-a")])))

    def test_a_mark_naming_another_host_is_not_believed(self):
        # Something else changed the preference and left the annotation.
        dep = deployment(prefers=[preference("host-b")], mark="host-a")
        self.assertEqual("", PLACE.balance_preference(dep))

    def test_a_mark_with_no_preference_left_is_nothing(self):
        self.assertEqual("", PLACE.balance_preference(deployment(mark="host-a")))

    def test_a_persons_move_takes_the_mark_off(self):
        for node, pin in (("host-b", False), ("host-a", False), ("host-b", True), (None, False)):
            dep = deployment(prefers=[preference("host-a")], mark="host-a")
            PLACE.apply_node_placement(dep, node, pin)
            self.assertNotIn(MARK, dep["metadata"]["annotations"], (node, pin))

    def test_a_persons_edit_of_the_host_takes_the_mark_off(self):
        dep = deployment(prefers=[preference("host-a")], mark="host-a")
        WORKLOAD_EDIT.apply_placement(dep, {"node": "host-b"})
        self.assertNotIn(MARK, dep["metadata"]["annotations"])

    def test_an_edit_that_leaves_the_host_alone_keeps_the_mark(self):
        # Edit sends node only when a person changed it.
        dep = deployment(prefers=[preference("host-a")], mark="host-a")
        WORKLOAD_EDIT.apply_placement(dep, {})
        self.assertEqual("host-a", PLACE.balance_preference(dep))


class WhereItShows(unittest.TestCase):
    def edit_node(self, dep):
        with Kube():
            return WORKLOADS.workload_edit_payload("lab", dep["metadata"]["name"], dep, [], services=[])["node"]

    def test_edit_shows_the_preferred_host(self):
        self.assertEqual("host-a", self.edit_node(deployment(prefers=[preference("host-a")])))

    def test_edit_shows_a_pin_over_a_preference(self):
        self.assertEqual("host-b", self.edit_node(deployment(prefers=[preference("host-a")], selector="host-b")))

    def test_edit_shows_any_host_with_neither(self):
        self.assertEqual("", self.edit_node(deployment()))

    def rows(self, *deployments):
        with Kube(*deployments):
            return {row["name"]: row for row in WORKLOADS.get_workloads()}

    def test_the_row_says_which_host_it_prefers_and_whether_balance_chose_it(self):
        rows = self.rows(deployment("app-a", prefers=[preference("host-a")], mark="host-a"),
                         deployment("app-b", prefers=[preference("host-b")]), deployment("app-c"))
        self.assertEqual(("host-a", True), (rows["app-a"]["prefers"], rows["app-a"]["prefers_balance"]))
        self.assertEqual(("host-b", False), (rows["app-b"]["prefers"], rows["app-b"]["prefers_balance"]))
        self.assertEqual(("", False), (rows["app-c"]["prefers"], rows["app-c"]["prefers_balance"]))


if __name__ == "__main__":
    unittest.main()
