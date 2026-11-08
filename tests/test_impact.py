import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
import homestead_impact as IMPACT

NODES = [{"name": "node1", "status": "Ready", "schedulable": True, "hardware": {"igpu": True}},
         {"name": "node2", "status": "Ready", "schedulable": True, "hardware": {"igpu": True, "coral_usb": True}},
         {"name": "node3", "status": "Ready", "schedulable": True, "hardware": {}}]


def app(name, nodes=("node2",), failover="move", hardware=(), claims=(), desired=1, **extra):
    row = {"ns": "lab", "name": name, "nodes": list(nodes), "failover": failover, "hardware": list(hardware),
           "claims": list(claims), "desired": desired}
    row.update(extra)
    return row


def dep(name, podspec=None):
    return {"metadata": {"namespace": "lab", "name": name}, "spec": {"template": {"spec": podspec or {}}}}


def vol(claim, copies):
    return {"name": f"pvc-{claim}", "pvc_name": claim, "namespace": "lab", "replicas": len(copies),
            "copies": [{"node": n, "healthy": h} for n, h in copies]}


def sts(name="postgres", pods=(("node2", True),), replicas=None, spec=None, claims=True):
    """A StatefulSet as homestead_nodes._impact_sources gives it: pod i on
    host, ready or not, with its own claim data-<name>-<i>."""
    rows = [{"name": f"{name}-{i}", "node": host, "ready": ready, "claims": [f"data-{name}-{i}"] if claims else []}
            for i, (host, ready) in enumerate(pods)]
    return {"ns": "lab", "name": name, "replicas": len(rows) if replicas is None else replicas, "spec": spec or {}, "pods": rows}


class AppTests(unittest.TestCase):
    def run_preview(self, workloads, deployments=(), volumes=(), vms=(), raw_vms=(), addresses=()):
        return IMPACT.preview("node2", list(workloads), list(deployments), list(vms), list(raw_vms), list(volumes), list(addresses), NODES)

    def outcome(self, result, name):
        return next(r for r in result["apps"] if r["name"] == name)

    def test_a_movable_app_moves_after_its_failover_delay(self):
        r = self.outcome(self.run_preview([app("web")], volumes=[vol("web-data", [("node2", True), ("node1", True)])]), "web")
        self.assertEqual(("moves", ["node1", "node3"]), (r["outcome"], r["to"]))
        self.assertIn("15 seconds", r["why"])
        r = self.outcome(self.run_preview([app("web", failover="default")]), "web")
        self.assertIn("5 minutes", r["why"])

    def test_an_app_set_to_wait_waits(self):
        self.assertEqual("waits", self.outcome(self.run_preview([app("db", failover="wait")]), "db")["outcome"])

    def test_hardware_only_on_this_host_stops_it(self):
        r = self.outcome(self.run_preview([app("frigate", hardware=["coral_usb"])]), "frigate")
        self.assertEqual("stops", r["outcome"])
        self.assertIn("coral_usb", r["why"])
        r = self.outcome(self.run_preview([app("plex", hardware=["igpu"])]), "plex")
        self.assertEqual(["node1"], r["to"], "only hosts with its iGPU")

    def test_a_volume_whose_only_copy_is_here_stops_it(self):
        r = self.outcome(self.run_preview([app("ha", claims=["ha-config"])], volumes=[vol("ha-config", [("node2", True), ("node1", False)])]), "ha")
        self.assertEqual("stops", r["outcome"])
        self.assertIn("only healthy copy on node2", r["why"])

    def test_pinned_to_this_host_stops_it(self):
        pinned = {"nodeSelector": {"kubernetes.io/hostname": "node2"}}
        r = self.outcome(self.run_preview([app("pinned")], deployments=[dep("pinned", pinned)]), "pinned")
        self.assertEqual("stops", r["outcome"])
        self.assertIn("may only run on node2", r["why"])

    def test_required_affinity_narrows_where_it_goes(self):
        spec = {"affinity": {"nodeAffinity": {"requiredDuringSchedulingIgnoredDuringExecution": {"nodeSelectorTerms": [
            {"matchExpressions": [{"key": "kubernetes.io/hostname", "operator": "In", "values": ["node2", "node3"]}]}]}}}}
        self.assertEqual(["node3"], self.outcome(self.run_preview([app("a")], deployments=[dep("a", spec)]), "a")["to"])

    def test_copies_elsewhere_keep_answering(self):
        r = self.outcome(self.run_preview([app("web", nodes=["node1", "node2"], desired=2)]), "web")
        self.assertTrue(r["keeps_answering"])

    def test_apps_elsewhere_and_stopped_apps_are_not_listed(self):
        res = self.run_preview([app("elsewhere", nodes=["node1"]), app("stopped", desired=0)])
        self.assertEqual([], res["apps"])

    def test_the_placement_planners_answer_decides_where(self):
        placement = {"workloads": [{"ns": "lab", "name": "a", "eligible": ["node3"], "blocked": [{"name": "node1", "why": ["lacks label zone=b"]}]},
                                   {"ns": "lab", "name": "b", "eligible": [], "blocked": [{"name": "node1", "why": ["no /dev/ttyUSB0"]},
                                                                                            {"name": "node3", "why": ["no /dev/ttyUSB0"]}]}]}
        res = IMPACT.preview("node2", [app("a"), app("b")], [], [], [], [], [], NODES, placement)
        self.assertEqual(["node3"], self.outcome(res, "a")["to"])
        b = self.outcome(res, "b")
        self.assertEqual("stops", b["outcome"])
        self.assertIn("no /dev/ttyUSB0", b["why"])

    def test_a_volume_not_on_longhorn_is_noted(self):
        r = self.outcome(self.run_preview([app("nas", claims=["nfs-media"])]), "nas")
        self.assertEqual("moves", r["outcome"])
        self.assertIn("not a Longhorn volume", r["notes"][0])


class VmTests(AppTests):
    def vm(self, name="ha-os", **extra):
        row = {"ns": "lab", "name": name, "node": "node2", "running": True, "migratable": True, "disks": [{"claim": f"{name}-disk"}]}
        row.update(extra)
        return row

    def raw(self, name="ha-os", devices=None):
        return {"metadata": {"namespace": "lab", "name": name},
                "spec": {"template": {"spec": {"domain": {"devices": devices or {}}}}}}

    def test_a_vm_is_never_sent_to_a_tiebreaker(self):
        nodes = NODES + [{"name": "pi", "status": "Ready", "schedulable": True, "hardware": {},
                          "labels": {"node-role/tiebreaker": "true"},
                          "taints": [{"key": "node-role/tiebreaker", "value": "true", "effect": "NoSchedule"}]}]
        found = IMPACT.preview("node2", [], [], [self.vm(disks=[])], [self.raw()], [], [], nodes)
        self.assertEqual(["node1", "node3"], self.outcome(found, "ha-os")["to"])
        self.assertNotIn("pi", found["others_ready"])

    def test_a_vm_moves_and_says_it_moves_live_on_a_drain(self):
        r = self.outcome(self.run_preview([], vms=[self.vm()], raw_vms=[self.raw()],
                                          volumes=[vol("ha-os-disk", [("node2", True), ("node3", True)])]), "ha-os")
        self.assertEqual("moves", r["outcome"])
        self.assertIn("moves live", r["why"])

    def test_a_passed_through_device_keeps_the_vm_here(self):
        raw = self.raw(devices={"gpus": [{"name": "gpu1", "deviceName": "nvidia.com/GA102"}]})
        r = self.outcome(self.run_preview([], vms=[self.vm()], raw_vms=[raw]), "ha-os")
        self.assertEqual("stops", r["outcome"])
        self.assertIn("nvidia.com/GA102", r["why"])

    def test_stopped_vms_and_vms_elsewhere_are_not_listed(self):
        res = self.run_preview([], vms=[self.vm(running=False), self.vm("other", node="node1")])
        self.assertEqual([], res["apps"])


class StorageAndAddressTests(AppTests):
    def test_volumes_at_risk_and_with_fewer_copies(self):
        res = self.run_preview([], volumes=[vol("only-here", [("node2", True)]), vol("two", [("node2", True), ("node1", True)]),
                                            vol("elsewhere", [("node1", True), ("node3", True)])])
        self.assertEqual(["only-here"], [v["name"] for v in res["at_risk"]])
        self.assertEqual([("two", 1, 2)], [(v["name"], v["left"], v["wanted"]) for v in res["fewer_copies"]])

    def test_addresses_this_host_answers_for_move(self):
        res = self.run_preview([], addresses=[{"ip": "192.0.2.240", "kind": "vip", "node": "node2", "services": ["lab/web"]},
                                              {"ip": "192.0.2.12", "kind": "node", "node": "node2"},
                                              {"ip": "192.0.2.241", "kind": "vip", "node": "node1"}])
        self.assertEqual(["192.0.2.240"], [a["ip"] for a in res["addresses"]], "its own address goes down with it")

    def test_counts_and_order(self):
        res = self.run_preview([app("moves"), app("stops", hardware=["coral_usb"]), app("waits", failover="wait")])
        self.assertEqual(["stops", "waits", "moves"], [r["name"] for r in res["apps"]])
        self.assertEqual({"stops": 1, "waits": 1, "moves": 1}, {k: res["counts"][k] for k in ("stops", "waits", "moves")})


class HostLocalTests(unittest.TestCase):
    """#440: a claim whose PersistentVolume is pinned to this host alone
    (local-path) can never move with its app."""

    def preview(self, workloads=(), vms=(), raw_vms=(), volumes=(), pvs=None):
        return IMPACT.preview("node2", list(workloads), [], list(vms), list(raw_vms), list(volumes), [], NODES, pvs=pvs)

    def row(self, result, name):
        return next(r for r in result["apps"] if r["name"] == name)

    def test_a_deployment_on_host_local_storage_stops(self):
        r = self.row(self.preview([app("web", claims=["web-data"])], pvs={("lab", "web-data"): ["node2"]}), "web")
        self.assertEqual(("stops", "its volume web-data is stored on node2", []), (r["outcome"], r["why"], r["notes"]))

    def test_a_deployment_on_longhorn_is_unchanged(self):
        result = IMPACT.preview("node2", [app("web", claims=["web-data"])], [], [], [], [vol("web-data", [("node2", True), ("node1", True)])],
                                [], NODES, pvs={("lab", "web-data"): []}, node_down="delete-both-statefulset-and-deployment-pod")
        r = self.row(result, "web")
        self.assertEqual(("moves", ["node1", "node3"], []), (r["outcome"], r["to"], r["notes"]))

    def test_a_volume_pinned_to_several_hosts_is_not_host_local(self):
        r = self.row(self.preview([app("web", claims=["web-data"])], pvs={("lab", "web-data"): ["node1", "node2"]}), "web")
        self.assertEqual("moves", r["outcome"])
        self.assertIn("not a Longhorn volume", r["notes"][0])

    def test_unread_volumes_leave_todays_answer(self):
        r = self.row(self.preview([app("web", claims=["web-data"])], pvs=None), "web")
        self.assertEqual("moves", r["outcome"])
        self.assertIn("whether other hosts can reach it is not known", r["notes"][0])

    def test_a_vm_on_host_local_storage_stops(self):
        vm = {"ns": "lab", "name": "x-db", "node": "node2", "running": True, "migratable": False, "disks": [{"claim": "x-db-disk"}]}
        raw = {"metadata": {"namespace": "lab", "name": "x-db"}, "spec": {"template": {"spec": {"domain": {"devices": {}}}}}}
        r = self.row(self.preview(vms=[vm], raw_vms=[raw], pvs={("lab", "x-db-disk"): ["node2"]}), "x-db")
        self.assertEqual(("stops", "its volume x-db-disk is stored on node2"), (r["outcome"], r["why"]))


class StatefulSetTests(unittest.TestCase):
    """#440: Kubernetes never starts a StatefulSet's pod elsewhere while the
    old one might still run, so it moves only once something deletes it."""
    LONGHORN_TWO = [vol("data-postgres-0", [("node2", True), ("node1", True)])]

    def preview(self, sets, volumes=(), node_down="do-nothing", nodes=NODES, **kw):
        kw.setdefault("pvs", {})
        return IMPACT.preview("node2", [], [], [], [], list(volumes), [], nodes, sets=list(sets), node_down=node_down, **kw)

    def row(self, result, name="postgres"):
        return next(r for r in result["apps"] if r["name"] == name)

    def test_on_host_local_storage_it_stops(self):
        r = self.row(self.preview([sts()], pvs={("lab", "data-postgres-0"): ["node2"]}))
        self.assertEqual(("set", "stops", "its volume data-postgres-0 is stored on node2"), (r["kind"], r["outcome"], r["why"]))

    def test_on_longhorn_left_at_do_nothing_it_waits_with_the_pointer(self):
        r = self.row(self.preview([sts()], volumes=self.LONGHORN_TWO))
        self.assertEqual("waits", r["outcome"])
        self.assertTrue(r["why"].startswith("waits until its pod is deleted: Kubernetes doesn't start"), r["why"])
        self.assertIn("To have Longhorn do this", r["why"])
        self.assertEqual("node_down", r["setting"])

    def test_with_longhorns_deletion_policy_and_a_copy_elsewhere_it_moves(self):
        for policy in ("delete-statefulset-pod", "delete-both-statefulset-and-deployment-pod"):
            r = self.row(self.preview([sts()], volumes=self.LONGHORN_TWO, node_down=policy))
            self.assertEqual(("moves", ["node1", "node3"]), (r["outcome"], r["to"]), policy)
            self.assertEqual("moves once Longhorn deletes its pod, after about 5 minutes, Kubernetes' default", r["why"])
        r = self.row(self.preview([sts()], volumes=self.LONGHORN_TWO, node_down="delete-deployment-pod"))
        self.assertEqual("waits", r["outcome"], "deleting Deployments' pods leaves StatefulSets' alone")

    UNREACHABLE = "node.kubernetes.io/unreachable"

    def test_a_set_whose_pod_tolerates_the_host_being_down_for_ever_waits(self):
        # Never evicted, so never terminating: Longhorn has no pod to delete.
        forever = {"tolerations": [{"key": self.UNREACHABLE, "operator": "Exists", "effect": "NoExecute"}]}
        r = self.row(self.preview([sts(spec=forever)], volumes=self.LONGHORN_TWO, node_down="delete-statefulset-pod"))
        self.assertEqual(("waits", "set to wait for its host to come back"), (r["outcome"], r["why"]))
        self.assertNotIn("setting", r)
        self.assertNotIn("to", r)

    def test_set_row_carries_no_wait_mode_flag(self):
        # The review reads the set's own setting (the plan's set.wait_mode);
        # the impact row carries no flag for it.
        forever = {"tolerations": [{"key": self.UNREACHABLE, "operator": "Exists", "effect": "NoExecute"}]}
        chosen = self.row(self.preview([sts(spec=forever)], volumes=self.LONGHORN_TWO, node_down="delete-statefulset-pod"))
        self.assertEqual("waits", chosen["outcome"])
        self.assertNotIn("mode", chosen)

    def test_a_set_set_to_move_moves_after_its_own_delay(self):
        fast = {"tolerations": [{"key": self.UNREACHABLE, "operator": "Exists", "effect": "NoExecute", "tolerationSeconds": 15}]}
        r = self.row(self.preview([sts(spec=fast)], volumes=self.LONGHORN_TWO, node_down="delete-statefulset-pod"))
        self.assertEqual(("moves", "moves once Longhorn deletes its pod, about 15 seconds after the host stops answering"),
                         (r["outcome"], r["why"]))

    def test_its_only_healthy_copy_here_stops_it(self):
        r = self.row(self.preview([sts()], volumes=[vol("data-postgres-0", [("node2", True), ("node1", False)])],
                                  node_down="delete-statefulset-pod"))
        self.assertEqual(("stops", "its volume data-postgres-0 has its only healthy copy on node2"), (r["outcome"], r["why"]))

    def test_pinned_by_node_selector_it_stops(self):
        r = self.row(self.preview([sts(spec={"nodeSelector": {"kubernetes.io/hostname": "node2"}})], volumes=self.LONGHORN_TWO,
                                  node_down="delete-statefulset-pod"))
        self.assertEqual(("stops", "it may only run on node2"), (r["outcome"], r["why"]))

    def test_with_no_other_ready_host_it_stops(self):
        alone = [NODES[0] | {"status": "NotReady"}, NODES[1], NODES[2] | {"schedulable": False}]
        r = self.row(self.preview([sts()], volumes=self.LONGHORN_TWO, node_down="delete-statefulset-pod", nodes=alone))
        self.assertEqual("stops", r["outcome"])
        self.assertTrue(r["why"].startswith("no other host can run it: "), r["why"])

    def test_on_nfs_it_waits_with_the_note_and_no_pointer(self):
        r = self.row(self.preview([sts()], pvs={("lab", "data-postgres-0"): []}, node_down="delete-statefulset-pod"))
        self.assertEqual("waits", r["outcome"])
        self.assertNotIn("Longhorn can do this", r["why"])
        self.assertNotIn("setting", r)
        self.assertIn("data-postgres-0 is not a Longhorn volume", r["notes"][0])

    def test_without_longhorn_a_set_on_nfs_waits_not_unknown(self):
        r = self.row(self.preview([sts()], pvs={("lab", "data-postgres-0"): []}, node_down=None))
        self.assertEqual("waits", r["outcome"])

    def test_a_set_with_no_volumes_waits_without_the_pointer(self):
        r = self.row(self.preview([sts(claims=False)], node_down="delete-statefulset-pod"))
        self.assertEqual("waits", r["outcome"])
        self.assertNotIn("setting", r)

    def test_one_of_three_copies_here_keeps_answering(self):
        r = self.row(self.preview([sts(pods=(("node1", True), ("node2", True), ("node3", False)))]))
        self.assertEqual((1, 3, True), (r["here"], r["copies"], r["keeps_answering"]))
        r = self.row(self.preview([sts(pods=(("node1", False), ("node2", True)))]))
        self.assertFalse(r["keeps_answering"], "its other copy isn't ready")

    def test_unread_volumes_make_it_unknown(self):
        res = self.preview([sts()], pvs=None, node_down="delete-statefulset-pod")
        r = self.row(res)
        self.assertEqual(("unknown", "couldn't be checked: where its volumes are stored"), (r["outcome"], r["why"]))
        self.assertEqual(1, res["counts"]["unknown"])

    def test_an_unread_policy_makes_it_unknown(self):
        r = self.row(self.preview([sts()], volumes=self.LONGHORN_TWO, node_down=None))
        self.assertEqual(("unknown", "couldn't be checked: whether apps with storage can move off a failed host"), (r["outcome"], r["why"]))

    def test_a_set_with_no_pod_here_is_not_listed(self):
        self.assertEqual([], self.preview([sts(pods=(("node1", True),))])["apps"])

    def test_unread_statefulsets_are_said_not_left_empty(self):
        res = IMPACT.preview("node2", [], [], [], [], [], [], NODES, sets=None, pvs={}, node_down="do-nothing")
        self.assertEqual((["StatefulSets"], []), (res["unread"], res["apps"]))
        self.assertEqual([], self.preview([])["unread"])

    def test_the_order_is_stops_waits_unknown_moves(self):
        # One preview with all four: PVs unread, the policy set.
        # a-moves: its only claim is on Longhorn with a copy on node1, so the unread PVs don't matter;
        # b-unknown: its claim isn't Longhorn and the PVs are unread;
        # c-waits: no volumes at all; d-stops: pinned here.
        sets = [sts("a-moves"), sts("b-unknown"), sts("c-waits", claims=False),
                sts("d-stops", spec={"nodeSelector": {"kubernetes.io/hostname": "node2"}})]
        volumes = [vol("data-a-moves-0", [("node2", True), ("node1", True)])]
        res = IMPACT.preview("node2", [], [], [], [], volumes, [], NODES, sets=sets, pvs=None, node_down="delete-statefulset-pod")
        self.assertEqual([("d-stops", "stops"), ("c-waits", "waits"), ("b-unknown", "unknown"), ("a-moves", "moves")],
                         [(r["name"], r["outcome"]) for r in res["apps"]])
        self.assertEqual({"stops": 1, "waits": 1, "unknown": 1, "moves": 1},
                         {k: res["counts"][k] for k in ("stops", "waits", "unknown", "moves")})


if __name__ == "__main__":
    unittest.main()


class DeploymentVolumeTests(unittest.TestCase):
    """A Deployment's replacement starts elsewhere, but a volume one host mounts
    at a time stays attached to the down host until its old pod is deleted:
    the forecast says "moves" only when Longhorn's policy deletes it."""
    def row(self, node_down, modes=("ReadWriteOnce",), failover="move"):
        volume = {**vol("ha-config", [("node2", True), ("node1", True)]), "access_modes": list(modes)}
        result = IMPACT.preview("node2", [app("ha", claims=["ha-config"], failover=failover)], [], [], [], [volume], [], NODES,
                                pvs={("lab", "ha-config"): []}, node_down=node_down)
        return next(r for r in result["apps"] if r["name"] == "ha")

    def test_left_at_do_nothing_it_waits_and_names_the_volume_and_the_setting(self):
        r = self.row("do-nothing")
        self.assertEqual(("waits", "node_down"), (r["outcome"], r.get("setting")))
        self.assertIn("ha-config is still attached to the failed host", r["why"])
        self.assertIn("Allow apps with storage to move off a failed host", r["why"])

    def test_a_policy_for_statefulsets_alone_still_leaves_it_waiting(self):
        self.assertEqual("waits", self.row("delete-statefulset-pod")["outcome"])

    def test_with_a_policy_that_deletes_deployment_pods_it_moves(self):
        for policy in ("delete-deployment-pod", "delete-both-statefulset-and-deployment-pod"):
            r = self.row(policy)
            self.assertEqual(("moves", ["node1", "node3"]), (r["outcome"], r["to"]), policy)
            self.assertIn("once Longhorn deletes its pod", r["why"])

    def test_an_unread_policy_is_said_not_guessed(self):
        r = self.row(None)
        self.assertEqual("unknown", r["outcome"])
        self.assertIn("apps with storage can move off a failed host", r["why"])

    def test_a_volume_many_hosts_mount_moves_whatever_the_policy(self):
        self.assertEqual("moves", self.row("do-nothing", modes=("ReadWriteMany",))["outcome"])

    def test_an_app_set_to_wait_still_waits_for_its_host(self):
        r = self.row("do-nothing", failover="wait")
        self.assertEqual("waits", r["outcome"])
        self.assertNotIn("setting", r)
