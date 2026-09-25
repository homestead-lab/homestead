"""Every job in the Activity tray can be cancelled, says first what that does,
and puts back what it changed where it can."""
import copy
import sys
import tempfile
import time
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
import homestead_cancel as CANCEL
import homestead_disks as DISKS
import homestead_helm as HELM
import homestead_imports as IMP
import homestead_ipam as IPAM
import homestead_k3scluster as K3SC
import homestead_operations as OPS
import homestead_reclass as RECLASS
import homestead_restructure as RESTRUCTURE
import homestead_smart as SMART
import homestead_vms as VMS


def missing(path):
    return urllib.error.HTTPError(path, 404, "not found", {}, None)


class Cluster:
    """Objects by path; lists answer at their path with the query attached."""

    def __init__(self):
        self.objects, self.calls = {}, []

    def get(self, path):
        value = self.objects.get(path, self.objects.get(path.split("?")[0]))
        if value is None:
            raise missing(path)
        return copy.deepcopy(value)

    def send(self, method, path, body=None, ctype=None):
        base = path.split("?")[0]
        self.calls.append((method, base, copy.deepcopy(body)))
        if method == "DELETE":
            if base not in self.objects:
                raise missing(path)
            self.objects.pop(base)
        elif method == "PUT":
            self.objects[base] = copy.deepcopy(body)
        return body or {}

    def sent(self, method):
        return [(path, body) for verb, path, body in self.calls if verb == method]


class Store(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.saved = OPS.DATA_DIR, dict(OPS.RESOLVERS), dict(OPS.CANCELLERS)
        OPS.DATA_DIR = self.tmp.name
        self.cluster = Cluster()
        CANCEL.bind(self.cluster.get, self.cluster.send)

    def tearDown(self):
        OPS.DATA_DIR = self.saved[0]
        OPS.RESOLVERS.clear(); OPS.RESOLVERS.update(self.saved[1])
        OPS.CANCELLERS.clear(); OPS.CANCELLERS.update(self.saved[2])
        self.tmp.cleanup()

    def job(self, kind, ref, title="A job", resource=None):
        return OPS.start(kind, title, resource or {"kind": "Thing", "name": "thing", "namespace": "lab"},
                         "/", ref)["id"]

    def stored(self, op):
        return next(item for item in OPS._read() if item["id"] == op)


class FrameworkTests(Store):
    def setUp(self):
        super().setUp()
        self.ran = []
        OPS.CANCELLERS["thing"] = (
            lambda item: {"mode": "rollback", "undo": ["puts the thing back"], "keeps": ["the rest"],
                          "options": [{"id": "wipe", "label": "wipe it", "default": True}]},
            lambda item, options: self.ran.append((item["ref"], options)) or "Put back")

    def test_the_plan_says_what_a_cancel_would_do_before_anything_changes(self):
        op = self.job("thing", {"n": 1})
        plan = OPS.cancel_plan(op)
        self.assertEqual(("rollback", "Cancel and put back", ["puts the thing back"], ["the rest"]),
                         (plan["mode"], plan["action"], plan["undo"], plan["keeps"]))
        self.assertEqual([], self.ran)
        self.assertEqual("queued", self.stored(op)["status"])

    def test_cancelling_runs_the_rollback_and_leaves_the_job_cancelled(self):
        op = self.job("thing", {"n": 1})
        result = OPS.cancel(op, {"wipe": False}, by="alice")
        self.assertEqual([({"n": 1}, {"wipe": False})], self.ran)
        item = self.stored(op)
        self.assertEqual(("cancelled", "Put back", "alice"), (item["status"], item["message"], item["cancelled_by"]))
        self.assertTrue(item["finished_at"])
        self.assertFalse(result["operation"]["cancellable"])

    def test_an_option_left_out_takes_its_default(self):
        OPS.cancel(self.job("thing", {}))
        self.assertEqual({"wipe": True}, self.ran[0][1])

    def test_a_finished_job_has_nothing_to_cancel(self):
        op = self.job("thing", {})
        items = OPS._read(); items[-1]["status"] = "succeeded"; OPS._write(items)
        with self.assertRaisesRegex(ValueError, "succeeded already"):
            OPS.cancel(op)

    def test_a_step_that_must_not_be_interrupted_is_refused_with_the_reason(self):
        OPS.CANCELLERS["thing"] = (lambda item: {"can": False, "why_not": "swapping now"}, lambda *a: "")
        with self.assertRaisesRegex(ValueError, "swapping now"):
            OPS.cancel(self.job("thing", {}))

    def test_a_cancel_that_deletes_data_asks_for_the_name(self):
        OPS.CANCELLERS["thing"] = (lambda item: {"confirm": "k3s-lab"}, lambda *a: "gone")
        op = self.job("thing", {})
        with self.assertRaisesRegex(ValueError, "type k3s-lab"):
            OPS.cancel(op, confirm="k3s")
        OPS.cancel(op, confirm="k3s-lab")
        self.assertEqual("cancelled", self.stored(op)["status"])

    def test_a_cancel_needing_an_admin_is_refused_to_an_operator(self):
        OPS.CANCELLERS["thing"] = (lambda item: {"needs": "admin"}, lambda *a: "")
        op = self.job("thing", {})
        with self.assertRaises(PermissionError):
            OPS.cancel(op, allowed=lambda need: need == "operator")
        self.assertEqual("queued", self.stored(op)["status"])

    def test_a_rollback_that_fails_leaves_the_job_running_and_says_why(self):
        def broken(item, options):
            item["ref"]["step"] = "half"
            raise RuntimeError("the API server went away")
        OPS.CANCELLERS["thing"] = (lambda item: {}, broken)
        op = self.job("thing", {})
        with self.assertRaisesRegex(ValueError, "went away"):
            OPS.cancel(op)
        item = self.stored(op)
        self.assertEqual("queued", item["status"])
        self.assertIn("did not finish", item["message"])
        self.assertEqual("half", item["ref"]["step"])

    def test_the_poll_does_not_move_a_job_on_while_it_is_being_cancelled(self):
        OPS.RESOLVERS["thing"] = lambda item: ("succeeded", 100, "done")
        op = self.job("thing", {})
        items = OPS._read()
        items[-1].update(status=OPS.CANCELLING, cancel_started=time.time())
        OPS._write(items)
        listed = next(o for o in OPS.list_operations() if o["id"] == op)
        self.assertEqual(OPS.CANCELLING, listed["status"])
        self.assertFalse(listed["cancellable"])
        with self.assertRaisesRegex(ValueError, "being cancelled already"):
            OPS.cancel(op)

    def test_a_cancel_interrupted_long_ago_can_be_asked_again(self):
        op = self.job("thing", {})
        items = OPS._read()
        items[-1].update(status=OPS.CANCELLING, previous_status="running",
                         cancel_started=time.time() - OPS.CANCEL_RETRY_AFTER - 1)
        OPS._write(items)
        self.assertTrue(next(o for o in OPS.list_operations() if o["id"] == op)["cancellable"])
        OPS.cancel(op)
        self.assertEqual("cancelled", self.stored(op)["status"])

    def test_a_kind_homestead_cannot_stop_only_stops_being_tracked(self):
        op = self.job("mystery", {})
        plan = OPS.cancel_plan(op)
        self.assertEqual(("forget", "Stop tracking it"), (plan["mode"], plan["action"]))
        OPS.cancel(op)
        self.assertIn("carries on", self.stored(op)["message"])

    def test_every_kind_of_job_homestead_starts_says_what_cancelling_does(self):
        CANCEL.register(OPS)
        for kind in ("deployment", "image-update", "image-rollback", "image-pull", "image-cleanup",
                     "volume-delete", "volume-restore", "backup", "protect-run", "import", "vm-disk-import",
                     "vm-migration", "k3s-cluster", "restructure", "reclass", "self-data-move",
                     "disk-retire", "helm", "smart-test", "network-service", "move"):
            self.assertIn(kind, OPS.CANCELLERS)


class K3sClusterTests(Store):
    NODES = [{"name": "k3s-lab-server-1", "role": "server", "address": "192.0.2.60"},
             {"name": "k3s-lab-agent-1", "role": "agent", "address": "192.0.2.61"}]

    def setUp(self):
        super().setUp()
        CANCEL.register(OPS)
        for node in self.NODES:
            self.cluster.objects[f"/apis/kubevirt.io/v1/namespaces/lab/virtualmachines/{node['name']}"] = {
                "metadata": {"name": node["name"], "labels": {K3SC.LABEL: "k3s-lab"}}}
        self.records = {"192.0.2.60": {"name": "k3s-lab-server-1"},
                        "192.0.2.61": {"name": "printer"}}
        self.deleted = []

    def op(self):
        return self.job("k3s-cluster", {"namespace": "lab", "name": "k3s-lab", "nodes": self.NODES,
                                        "first": "192.0.2.60", "setup": "homestead", "started": time.time()},
                        "k3s cluster k3s-lab")

    def patches(self):
        def update(change):
            data = {"records": self.records}
            return change(data)
        return (mock.patch.object(VMS, "delete", lambda ns, name, with_disks=False: self.deleted.append((ns, name, with_disks))),
                mock.patch.object(IPAM, "update", update))

    def test_the_plan_names_the_vms_and_addresses_and_asks_for_the_cluster_name(self):
        plan = OPS.cancel_plan(self.op())
        self.assertEqual(("rollback", "k3s-lab", "admin", "high"),
                         (plan["mode"], plan["confirm"], plan["needs"], plan["severity"]))
        self.assertIn("k3s-lab-server-1 and k3s-lab-agent-1", plan["undo"][0])
        self.assertIn("192.0.2.60 and 192.0.2.61", plan["undo"][1])

    def test_cancelling_deletes_its_vms_with_their_disks_and_frees_their_addresses(self):
        op = self.op()
        a, b = self.patches()
        with a, b:
            OPS.cancel(op, confirm="k3s-lab")
        self.assertEqual([("lab", "k3s-lab-server-1", True), ("lab", "k3s-lab-agent-1", True)], self.deleted)
        # Only a record still under the VM's name: .61 has been taken since.
        self.assertEqual({"192.0.2.61": {"name": "printer"}}, self.records)
        self.assertEqual("cancelled", self.stored(op)["status"])

    def test_a_vm_that_only_shares_a_name_is_never_deleted(self):
        self.cluster.objects["/apis/kubevirt.io/v1/namespaces/lab/virtualmachines/k3s-lab-agent-1"]["metadata"][
            "labels"] = {}
        a, b = self.patches()
        with a, b:
            OPS.cancel(self.op(), confirm="k3s-lab")
        self.assertEqual([("lab", "k3s-lab-server-1", True)], self.deleted)

    def test_a_build_that_fails_part_way_removes_the_vms_it_made(self):
        removed, made = [], []

        def create(vm):
            if vm["name"].endswith("agent-1"):
                raise ValueError("no room")
            made.append(vm["name"])
        K3SC.bind(lambda path: {}, create, lambda ip: "", lambda ns, node: removed.append(node["name"]))
        try:
            with self.assertRaisesRegex(ValueError, "agent-1 could not be made: no room. The 1 VM made before it"):
                K3SC.start({"name": "k3s-lab", "servers": 1, "agents": 1, "network": "default/lan",
                            "addresses": ["192.0.2.60", "192.0.2.61"], "password": "a-long-password"}, OPS)
        finally:
            K3SC.bind(None, None, None)
        self.assertEqual(["k3s-lab-server-1"], removed)


class DeploymentTests(Store):
    DEP = "/apis/apps/v1/namespaces/lab/deployments/frigate"

    def setUp(self):
        super().setUp()
        CANCEL.register(OPS)

    def replicasets(self, *sets):
        self.cluster.objects["/apis/apps/v1/namespaces/lab/replicasets"] = {"items": [
            {"metadata": {"name": f"frigate-{rev}", "creationTimestamp": made,
                          "annotations": {CANCEL.REVISION: str(rev)}, "ownerReferences": [{"uid": "d1"}]},
             "spec": {"template": {"metadata": {"labels": {"app": "frigate", "pod-template-hash": f"h{rev}"}},
                                   "spec": {"containers": [{"name": "frigate", "image": image}]}}}}
            for rev, made, image in sets]}

    def deployment(self, revision):
        self.cluster.objects[self.DEP] = {
            "metadata": {"name": "frigate", "uid": "d1", "annotations": {CANCEL.REVISION: str(revision)}},
            "spec": {"replicas": 1, "selector": {"matchLabels": {"app": "frigate"}},
                     "template": {"spec": {"containers": [{"name": "frigate", "image": "frigate:0.15"}]}}}}

    def test_a_new_deploy_is_removed_and_its_volumes_kept(self):
        self.deployment(1)
        self.cluster.objects[self.DEP]["spec"]["template"]["spec"]["volumes"] = [
            {"name": "c", "persistentVolumeClaim": {"claimName": "frigate-config"}}]
        removed = []
        CANCEL.bind(self.cluster.get, self.cluster.send, lambda ns, name: removed.append((ns, name)))
        op = self.job("deployment", {"namespace": "lab", "name": "frigate", "undo": "delete"}, "Deploy frigate")
        plan = OPS.cancel_plan(op)
        self.assertIn("frigate-config is kept", plan["keeps"][0])
        OPS.cancel(op)
        self.assertEqual([("lab", "frigate")], removed)

    def test_a_changed_deployment_goes_back_to_the_version_before_the_job(self):
        op = self.job("deployment", {"namespace": "lab", "name": "frigate", "undo": "rollout"}, "Move frigate")
        started = self.stored(op)["started_at"]
        self.deployment(3)
        self.replicasets((1, "2026-01-01T00:00:00Z", "frigate:0.13"), (2, "2026-02-01T00:00:00Z", "frigate:0.14"),
                         (3, started, "frigate:0.15"))
        plan = OPS.cancel_plan(op)
        self.assertIn("revision 2: frigate:0.14", plan["undo"][0])
        OPS.cancel(op)
        template = self.cluster.objects[self.DEP]["spec"]["template"]
        self.assertEqual("frigate:0.14", template["spec"]["containers"][0]["image"])
        self.assertNotIn("pod-template-hash", template["metadata"]["labels"])

    def test_a_version_the_job_did_not_make_is_never_rolled_back(self):
        op = self.job("deployment", {"namespace": "lab", "name": "frigate", "undo": "rollout"}, "Move frigate")
        self.deployment(2)
        self.replicasets((1, "2026-01-01T00:00:00Z", "frigate:0.14"), (2, "2026-02-01T00:00:00Z", "frigate:0.15"))
        plan = OPS.cancel_plan(op)
        self.assertEqual("forget", plan["mode"])
        OPS.cancel(op)
        self.assertEqual([], self.cluster.sent("PUT"))

    def test_a_share_change_is_saved_already_and_only_stops_being_followed(self):
        op = self.job("deployment", {"namespace": "samba", "name": "samba"}, "Create share media")
        self.assertEqual("forget", OPS.cancel_plan(op)["mode"])


class RestructureTests(Store):
    DEP = "/apis/apps/v1/namespaces/lab/deployments/app"

    def setUp(self):
        super().setUp()
        CANCEL.register(OPS)

    def test_a_copy_cancelled_puts_the_storage_back_and_starts_the_app_as_it_ran(self):
        op = self.job("restructure", {"namespace": "lab", "name": "app", "moves": [], "replicas": 2,
                                      "phase": "copying", "job": "app-restructure-abc"})
        started = self.stored(op)["started_at"]
        self.cluster.objects[self.DEP] = {
            "metadata": {"name": "app", "uid": "u", "annotations": {CANCEL.REVISION: "2", RESTRUCTURE.HELD: "2"}},
            "spec": {"replicas": 0, "selector": {"matchLabels": {"app": "app"}}, "template": {"spec": {"volumes": ["new"]}}}}
        self.cluster.objects["/apis/apps/v1/namespaces/lab/replicasets"] = {"items": [
            {"metadata": {"annotations": {CANCEL.REVISION: str(rev)}, "ownerReferences": [{"uid": "u"}],
                          "creationTimestamp": made},
             "spec": {"template": {"metadata": {"labels": {"app": "app"}}, "spec": {"volumes": [vols]}}}}
            for rev, made, vols in ((1, "2026-01-01T00:00:00Z", "old"), (2, started, "new"))]}
        self.cluster.objects["/apis/batch/v1/namespaces/lab/jobs/app-restructure-abc"] = {}
        plan = OPS.cancel_plan(op)
        self.assertEqual("rollback", plan["mode"])
        self.assertIn("starts again (2 replicas)", plan["undo"][1])
        OPS.cancel(op)
        dep = self.cluster.objects[self.DEP]
        self.assertEqual((2, ["old"]), (dep["spec"]["replicas"], dep["spec"]["template"]["spec"]["volumes"]))
        self.assertNotIn(RESTRUCTURE.HELD, dep["metadata"]["annotations"])
        self.assertNotIn("/apis/batch/v1/namespaces/lab/jobs/app-restructure-abc", self.cluster.objects)
        self.assertEqual("cancelled", self.stored(op)["ref"]["phase"])


class ReclassTests(Store):
    def setUp(self):
        super().setUp()
        CANCEL.register(OPS)

    def op(self, phase):
        return self.job("reclass", {"namespace": "lab", "claim": "data", "target": "fast", "phase": phase,
                                    "temp": "data-reclass", "job": "data-reclass-1", "old_pv": "pv-old",
                                    "consumers": [{"kind": "Deployment", "name": "paperless", "replicas": 1}]})

    def test_before_the_swap_it_puts_everything_back_on_the_original(self):
        op = self.op("copy")
        with mock.patch.object(RECLASS, "_rollback") as rollback:
            OPS.cancel(op)
        self.assertEqual("data-reclass-1", rollback.call_args[0][1]["job"])
        self.assertEqual("cancelled", self.stored(op)["status"])

    def test_the_swap_itself_cannot_be_interrupted(self):
        op = self.op("swap")
        self.assertFalse(OPS.cancel_plan(op)["can"])
        with self.assertRaisesRegex(ValueError, "must not stop half-way"):
            OPS.cancel(op)


class OtherKindTests(Store):
    def setUp(self):
        super().setUp()
        CANCEL.register(OPS)

    def test_a_disk_retirement_cancelled_early_lets_replicas_onto_the_disk_as_before(self):
        op = self.job("disk-retire", {"node": "n1", "disk": "d1", "phase": "replicas", "removed": ["r1"],
                                      "was_scheduling": True})
        with mock.patch.object(DISKS, "_patch") as patch:
            OPS.cancel(op)
        self.assertEqual({"spec": {"disks": {"d1": {"allowScheduling": True}}}}, patch.call_args[0][1])

    def test_a_disk_being_taken_out_cannot_be_stopped(self):
        self.assertFalse(OPS.cancel_plan(self.job("disk-retire", {"node": "n1", "disk": "d1", "phase": "remove"}))["can"])

    def test_an_import_cancelled_removes_its_workload_and_only_the_volumes_it_made(self):
        CANCEL.bind(self.cluster.get, self.cluster.send, lambda ns, name: None)
        plan = {"job": "homestead-import-frigate", "namespace": "lab", "workload": "frigate",
                "volumes": [{"name": "frigate-appdata", "created": True}, {"name": "media", "created": False}]}
        for claim in ("frigate-appdata", "media"):
            self.cluster.objects[f"/api/v1/namespaces/lab/persistentvolumeclaims/{claim}"] = {}
        op = self.job("import", {"namespace": "lab", "name": "homestead-import-frigate"})
        with mock.patch.object(IMP, "import_cleanup_plan", lambda name: plan), \
                mock.patch.object(IMP, "delete_import", lambda name: {}), \
                mock.patch.object(IMP, "wait_for_pods_gone", lambda *a: True):
            shown = OPS.cancel_plan(op)
            self.assertIn("frigate-appdata", shown["options"][0]["label"])
            OPS.cancel(op, {"volumes": True})
        self.assertNotIn("/api/v1/namespaces/lab/persistentvolumeclaims/frigate-appdata", self.cluster.objects)
        self.assertIn("/api/v1/namespaces/lab/persistentvolumeclaims/media", self.cluster.objects)

    def test_a_helm_upgrade_cancelled_puts_the_chart_back_as_it_was(self):
        path = f"/apis/helm.cattle.io/v1/namespaces/{HELM.CONTROLLER_NS}/helmcharts/grafana"
        chart = {"metadata": {"name": "grafana", "namespace": HELM.CONTROLLER_NS},
                 "spec": {"targetNamespace": "mon", "version": "7.0.0", "valuesContent": "a: 1"}}
        sent = []
        HELM.bind(lambda p: {"items": [copy.deepcopy(chart)]},
                  lambda method, p, body=None, **kw: sent.append(copy.deepcopy(body)))
        HELM.upgrade({"namespace": "mon", "name": "grafana", "version": "8.0.0", "values": "a: 2"})
        self.cluster.objects[path] = sent[-1]
        op = self.job("helm", {"namespace": HELM.CONTROLLER_NS, "name": "helm-install-grafana", "action": "upgrade"},
                      "Helm upgrade grafana", {"kind": "HelmChart", "name": "grafana", "namespace": HELM.CONTROLLER_NS})
        HELM.bind(self.cluster.get, self.cluster.send)
        self.assertEqual("rollback", OPS.cancel_plan(op)["mode"])
        OPS.cancel(op)
        spec = self.cluster.objects[path]["spec"]
        self.assertEqual(("7.0.0", "a: 1"), (spec["version"], spec["valuesContent"]))
        self.assertNotIn(HELM.PREVIOUS_SPEC, self.cluster.objects[path]["metadata"]["annotations"])

    def test_a_helm_install_cancelled_deletes_the_chart_so_the_controller_uninstalls_it(self):
        path = f"/apis/helm.cattle.io/v1/namespaces/{HELM.CONTROLLER_NS}/helmcharts/grafana"
        self.cluster.objects[path] = {"metadata": {"name": "grafana"}, "spec": {}}
        op = self.job("helm", {"namespace": HELM.CONTROLLER_NS, "name": "helm-install-grafana", "action": "install"},
                      "Helm install grafana", {"kind": "HelmChart", "name": "grafana", "namespace": HELM.CONTROLLER_NS})
        OPS.cancel(op)
        self.assertNotIn(path, self.cluster.objects)

    def test_a_smart_test_is_aborted_on_its_node(self):
        op = self.job("smart-test", {"node": "n1", "disk": "sda", "test": "long"})
        with mock.patch.object(SMART, "abort_test") as abort:
            OPS.cancel(op)
        abort.assert_called_once_with("n1", "sda")

    def test_an_older_smart_helper_says_it_cannot_abort(self):
        with mock.patch.object(SMART, "_request", side_effect=ValueError("test must be short or long")):
            with self.assertRaisesRegex(ValueError, "too old to abort"):
                SMART.abort_test("n1", "sda")

    def test_homestead_switching_onto_its_moved_data_cannot_be_stopped(self):
        self.cluster.objects["/apis/apps/v1/namespaces/hs/deployments/homestead"] = {"spec": {"template": {"spec": {
            "volumes": [{"name": "data", "persistentVolumeClaim": {"claimName": "homestead-data-shared"}}]}}}}
        op = self.job("self-data-move", {"namespace": "hs", "job": "j", "old": "homestead-data",
                                         "new": "homestead-data-shared"})
        self.assertFalse(OPS.cancel_plan(op)["can"])

    def test_a_volume_delete_cannot_be_taken_back_and_says_so(self):
        op = self.job("volume-delete", {"namespace": "lab", "name": "old", "action": "delete"})
        plan = OPS.cancel_plan(op)
        self.assertEqual("forget", plan["mode"])
        self.assertIn("cannot take a delete back", plan["keeps"][0])


if __name__ == "__main__":
    unittest.main()
