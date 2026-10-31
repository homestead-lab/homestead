"""Every job in the Activity tray can be cancelled, says first what that does,
and puts back what it changed where it can."""
import copy
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
import homestead_cancel as CANCEL
import homestead_kube as K
import homestead_disks as DISKS
import homestead_helm as HELM
import homestead_container_import as CONTAINER_IMPORT
import homestead_ipam as IPAM
import homestead_k3scluster as K3SC
import homestead_operations as OPS
import homestead_reclass as RECLASS
import homestead_restructure as RESTRUCTURE
import homestead_smart as SMART
import homestead_vms as VMS
from fakes.job_store import put, store_writes
from fakes.kube import Kube


def vm(name, labels=None, **meta):
    return {"apiVersion": "kubevirt.io/v1", "kind": "VirtualMachine",
            "metadata": {"namespace": "lab", "name": name, "labels": labels or {}, **meta}}


class Store(unittest.TestCase):
    """The job store in a folder of its own, over the shared fake API
    (tests/fakes/kube.py): self.kube starts empty, and a test adds its objects."""
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.saved = OPS.DATA_DIR, dict(OPS.RESOLVERS), dict(OPS.CANCELLERS), set(OPS.CLEANUPS)
        OPS.DATA_DIR = self.tmp.name
        self.kube = self.enterContext(Kube())
        self.addCleanup(CANCEL.bind, *(getattr(CANCEL, name, None) for name in (
            "kget", "ksend", "remove_workload", "tidy_restore")))
        CANCEL.bind(K.kget, K.ksend)

    def sent(self, method):
        """The bodies written with method, by path."""
        return [(path.split("?")[0], body) for verb, path, body, _ in self.kube.writes if verb == method]

    def tearDown(self):
        OPS.DATA_DIR = self.saved[0]
        OPS.RESOLVERS.clear(); OPS.RESOLVERS.update(self.saved[1])
        OPS.CANCELLERS.clear(); OPS.CANCELLERS.update(self.saved[2])
        OPS.CLEANUPS.clear(); OPS.CLEANUPS.update(self.saved[3])
        self.tmp.cleanup()

    def job(self, kind, ref, title="A job", resource=None):
        return OPS.start(kind, title, resource or {"kind": "Thing", "name": "thing", "namespace": "lab"},
                         "/", ref)["id"]

    def stored(self, op):
        return next(item for item in OPS.stored() if item["id"] == op)


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
        items = OPS.stored(); items[-1]["status"] = "succeeded"; put(OPS, items)
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
        items = OPS.stored()
        items[-1].update(status=OPS.CANCELLING, cancel_started=time.time())
        put(OPS, items)
        listed = next(o for o in OPS.list_operations() if o["id"] == op)
        self.assertEqual(OPS.CANCELLING, listed["status"])
        self.assertFalse(listed["cancellable"])
        with self.assertRaisesRegex(ValueError, "being cancelled already"):
            OPS.cancel(op)

    def test_a_cancel_interrupted_long_ago_can_be_asked_again(self):
        op = self.job("thing", {})
        items = OPS.stored()
        items[-1].update(status=OPS.CANCELLING, previous_status="running",
                         cancel_started=time.time() - OPS.CANCEL_RETRY_AFTER - 1)
        put(OPS, items)
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
            self.kube.add(vm(node["name"], {K3SC.LABEL: "k3s-lab"}))
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

    def test_legacy_plan_explains_missing_identity_and_confirms_tracking_only(self):
        plan = OPS.cancel_plan(self.op())
        self.assertEqual(("forget", "k3s-lab", "admin", "low"),
                         (plan["mode"], plan["confirm"], plan["needs"], plan["severity"]))
        self.assertTrue(plan["tracking_only"])
        self.assertEqual([], plan["undo"])
        self.assertIn("no creation identity receipts", plan["keeps"][0])
        self.assertIn("IP-address records remain", " ".join(plan["keeps"]))

    def test_cancelling_retains_every_vm_disk_and_address(self):
        op = self.op()
        before = copy.deepcopy(self.records)
        a, b = self.patches()
        with a, b:
            OPS.cancel(op, confirm="k3s-lab")
        self.assertEqual([], self.deleted)
        self.assertEqual(before, self.records)
        self.assertEqual([], self.kube.writes)
        self.assertEqual("cancelled", self.stored(op)["status"])
        self.assertTrue(self.stored(op)["tracking_stopped"])
        self.assertTrue(self.stored(op)["ref"]["retain_resources"])

    def test_a_vm_that_only_shares_a_name_is_never_deleted(self):
        self.kube.add(vm("k3s-lab-agent-1"))
        a, b = self.patches()
        with a, b:
            OPS.cancel(self.op(), confirm="k3s-lab")
        self.assertEqual([], self.deleted)

    def test_failed_build_requires_retention_review_and_never_claims_cleanup(self):
        op = self.op()
        items = OPS.stored()
        items[-1].update(status="failed", finished_at=OPS._now(), message="After 45 minutes the cluster is not up")
        put(OPS, items)
        listed = next(o for o in OPS.list_operations() if o["id"] == op)
        self.assertEqual((False, True), (listed["cancellable"], listed["cleanable"]))
        self.assertTrue(listed["tracking_only"])
        self.assertFalse(listed["dismissible"])
        self.assertEqual(0, OPS.dismiss_finished()["dismissed"])
        with self.assertRaisesRegex(ValueError, "recovery details"):
            OPS.dismiss(op)
        self.assertTrue(OPS.cancel_plan(op)["cleanup"])
        a, b = self.patches()
        with a, b:
            OPS.cancel(op, confirm="k3s-lab", by="test-admin")
        item = self.stored(op)
        self.assertEqual(("failed", True), (item["status"], item["tracking_stopped"]))
        self.assertFalse(item.get("cleaned"))
        self.assertEqual("test-admin", item["cancelled_by"])
        self.assertIn("Tracking stopped after failure", item["message"])
        self.assertEqual([], self.deleted)
        self.assertIn("192.0.2.60", self.records)
        with self.assertRaisesRegex(ValueError, "failed already"):
            OPS.cancel(op, confirm="k3s-lab")
        self.assertTrue(OPS._public(item)["dismissible"])
        self.assertEqual(1, OPS.dismiss_finished()["dismissed"])

    def test_other_failed_jobs_are_not_offered_a_clean_up(self):
        op = self.job("image-pull", {"namespace": "lab", "name": "x"})
        items = OPS.stored(); items[-1]["status"] = "failed"; put(OPS, items)
        self.assertFalse(next(o for o in OPS.list_operations() if o["id"] == op)["cleanable"])
        with self.assertRaisesRegex(ValueError, "failed already"):
            OPS.cancel(op)

    def test_same_name_and_label_replacement_is_retained_without_any_cluster_call(self):
        op = self.op()
        for node in self.NODES:
            self.kube.add(vm(node["name"], {K3SC.LABEL: "k3s-lab"}, uid="replacement-never-created-by-job"))
        with mock.patch.object(CANCEL, "kget", side_effect=AssertionError("no ownership lookup needed")), \
             mock.patch.object(VMS, "delete") as delete, mock.patch.object(IPAM, "update") as addresses:
            OPS.cancel_plan(op)
            OPS.cancel(op, confirm="k3s-lab")
        delete.assert_not_called()
        addresses.assert_not_called()
        self.assertEqual([], self.kube.writes)

    def test_failed_new_and_legacy_recovery_records_survive_clear_and_pruning(self):
        for retained in (False, True):
            op = self.op()
            items = OPS.stored()
            item = next(row for row in items if row["id"] == op)
            item.update(status="failed")
            item["ref"].update(retain_resources=retained, phase="failed")
            put(OPS, items)
            with mock.patch.object(OPS, "MAX_OPERATIONS", 0):
                put(OPS)
            self.assertEqual(0, OPS.dismiss_finished()["dismissed"])
            with self.assertRaisesRegex(ValueError, "recovery details"):
                OPS.dismiss(op)
            with self.assertRaises(PermissionError):
                OPS.cancel(op, confirm="k3s-lab", allowed=lambda role: role != "admin")
            with self.assertRaisesRegex(ValueError, "type k3s-lab"):
                OPS.cancel(op, confirm="wrong", allowed=lambda role: True)
            OPS.cancel(op, confirm="k3s-lab", allowed=lambda role: True)
            OPS.dismiss(op)

    def test_tracking_result_write_failure_keeps_recovery_evidence(self):
        op = self.op()
        items = OPS.stored()
        items[-1]["status"] = "failed"
        put(OPS, items)
        def write(rows):
            if any(row.get("tracking_stopped") for row in rows):
                raise OSError("lost storage")
        with store_writes(OPS, fail=write), mock.patch.object(VMS, "delete") as delete:
            with self.assertRaises(OSError):
                OPS.cancel(op, confirm="k3s-lab")
        delete.assert_not_called()
        self.assertEqual("cancelling", self.stored(op)["status"])
        self.assertFalse(self.stored(op).get("tracking_stopped"))
        self.assertEqual(0, OPS.dismiss_finished()["dismissed"])

    def test_previously_completed_legacy_cleanup_remains_dismissible(self):
        op = self.op()
        items = OPS.stored()
        items[-1].update(status="failed", cleaned=True)
        put(OPS, items)
        self.assertTrue(OPS._public(self.stored(op))["dismissible"])
        OPS.dismiss(op)
        self.assertEqual([], OPS.stored())

    def test_a_build_that_fails_part_way_retains_vms_and_records_the_uncertain_target(self):
        removed, made = [], []

        def create(vm):
            if vm["name"].endswith("agent-1"):
                raise ValueError("no room")
            made.append(vm["name"])
            return {"vm_identity": {"namespace": "lab", "name": vm["name"], "uid": "made-uid", "resourceVersion": "1"}}
        K3SC.bind(lambda path: {}, create, lambda ip: "", lambda ns, node: removed.append(node["name"]))
        try:
            with self.assertRaisesRegex(ValueError, "Stopped at k3s-lab-agent-1. 1 creation receipt"):
                K3SC.start({"name": "k3s-lab", "servers": 1, "agents": 1, "network": "default/lan",
                            "addresses": ["192.0.2.60", "192.0.2.61"], "password": "a-long-password"}, OPS)
        finally:
            K3SC.bind(None, None, None)
        self.assertEqual([], removed)
        item = OPS.stored()[-1]
        self.assertEqual("failed", item["status"])
        self.assertEqual("k3s-lab-agent-1", item["ref"]["attempted"])
        self.assertEqual("made-uid", item["ref"]["created"][0]["identity"]["uid"])
        self.assertTrue(OPS._public(item)["cleanable"])
        self.assertTrue(OPS._public(item)["tracking_only"])


class DeploymentTests(Store):
    DEP = "/apis/apps/v1/namespaces/lab/deployments/frigate"

    def setUp(self):
        super().setUp()
        CANCEL.register(OPS)

    def replicasets(self, *sets):
        for rev, made, image in sets:
            self.kube.add({"apiVersion": "apps/v1", "kind": "ReplicaSet",
                           "metadata": {"namespace": "lab", "name": f"frigate-{rev}", "creationTimestamp": made,
                                        "labels": {"app": "frigate", "pod-template-hash": f"h{rev}"},
                                        "annotations": {CANCEL.REVISION: str(rev)}, "ownerReferences": [{"uid": "d1"}]},
                           "spec": {"template": {"metadata": {"labels": {"app": "frigate", "pod-template-hash": f"h{rev}"}},
                                                 "spec": {"containers": [{"name": "frigate", "image": image}]}}}})

    def deployment(self, revision, volumes=None):
        self.kube.add({
            "apiVersion": "apps/v1", "kind": "Deployment",
            "metadata": {"namespace": "lab", "name": "frigate", "uid": "d1", "annotations": {CANCEL.REVISION: str(revision)}},
            "spec": {"replicas": 1, "selector": {"matchLabels": {"app": "frigate"}},
                     "template": {"spec": {"containers": [{"name": "frigate", "image": "frigate:0.15"}],
                                           **({"volumes": volumes} if volumes else {})}}}})

    def test_a_new_deploy_is_removed_and_its_volumes_kept(self):
        self.deployment(1, volumes=[{"name": "c", "persistentVolumeClaim": {"claimName": "frigate-config"}}])
        removed = []
        CANCEL.bind(K.kget, K.ksend, lambda ns, name: removed.append((ns, name)))
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
        template = self.kube.object(self.DEP)["spec"]["template"]
        self.assertEqual("frigate:0.14", template["spec"]["containers"][0]["image"])
        self.assertNotIn("pod-template-hash", template["metadata"]["labels"])

    def test_a_version_the_job_did_not_make_is_never_rolled_back(self):
        op = self.job("deployment", {"namespace": "lab", "name": "frigate", "undo": "rollout"}, "Move frigate")
        self.deployment(2)
        self.replicasets((1, "2026-01-01T00:00:00Z", "frigate:0.14"), (2, "2026-02-01T00:00:00Z", "frigate:0.15"))
        plan = OPS.cancel_plan(op)
        self.assertEqual("forget", plan["mode"])
        OPS.cancel(op)
        self.assertEqual([], self.sent("PUT"))

    def test_a_share_change_is_saved_already_and_only_stops_being_followed(self):
        op = self.job("deployment", {"namespace": "samba", "name": "samba"}, "Create share media")
        self.assertEqual("forget", OPS.cancel_plan(op)["mode"])


class RestructureTests(Store):
    DEP = "/apis/apps/v1/namespaces/lab/deployments/app"

    def setUp(self):
        super().setUp()
        CANCEL.register(OPS)

    def test_legacy_copy_recovery_never_deletes_or_rolls_back_or_restarts(self):
        op = self.job("restructure", {"namespace": "lab", "name": "app", "moves": [], "replicas": 2,
                                      "phase": "copying", "job": "app-restructure-abc"})
        self.kube.add({"apiVersion": "apps/v1", "kind": "Deployment",
                       "metadata": {"namespace": "lab", "name": "app", "uid": "u"},
                       "spec": {"replicas": 0, "template": {"spec": {"volumes": ["new"]}}}})
        copier = self.kube.add({"apiVersion": "batch/v1", "kind": "Job",
                                "metadata": {"namespace": "lab", "name": "app-restructure-abc"}})
        before = [self.kube.object(path) for path in (self.DEP, copier)]
        plan = OPS.cancel_plan(op)
        self.assertEqual("forget", plan["mode"])
        with self.assertRaises(ValueError):
            OPS.cancel(op, confirm="app")
        OPS.cancel(op, {"ack": True}, confirm="app")
        self.assertEqual(before, [self.kube.object(path) for path in (self.DEP, copier)])
        self.assertEqual([], self.kube.writes)
        self.assertTrue(self.stored(op)["tracking_stopped"])


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
        CANCEL.bind(K.kget, K.ksend, lambda ns, name: None)
        plan = {"job": "homestead-import-frigate", "namespace": "lab", "workload": "frigate",
                "volumes": [{"name": "frigate-appdata", "created": True}, {"name": "media", "created": False}]}
        for claim in ("frigate-appdata", "media"):
            self.kube.add({"apiVersion": "v1", "kind": "PersistentVolumeClaim",
                           "metadata": {"namespace": "lab", "name": claim}})
        op = self.job("import", {"namespace": "lab", "name": "homestead-import-frigate"})
        with mock.patch.object(CONTAINER_IMPORT, "import_cleanup_plan", lambda name, ns=None: plan), \
                mock.patch.object(CONTAINER_IMPORT, "delete_import", lambda name, ns=None: {}), \
                mock.patch.object(CONTAINER_IMPORT, "wait_for_pods_gone", lambda *a: True):
            shown = OPS.cancel_plan(op)
            self.assertIn("frigate-appdata", shown["options"][0]["label"])
            OPS.cancel(op, {"volumes": True})
        self.assertIsNone(self.kube.object("/api/v1/namespaces/lab/persistentvolumeclaims/frigate-appdata"))
        self.assertIsNotNone(self.kube.object("/api/v1/namespaces/lab/persistentvolumeclaims/media"))

    def test_a_helm_upgrade_cancelled_puts_the_chart_back_as_it_was(self):
        path = f"/apis/helm.cattle.io/v1/namespaces/{HELM.CONTROLLER_NS}/helmcharts/grafana"
        self.kube.add({"apiVersion": "helm.cattle.io/v1", "kind": "HelmChart",
                       "metadata": {"name": "grafana", "namespace": HELM.CONTROLLER_NS},
                       "spec": {"targetNamespace": "mon", "version": "7.0.0", "valuesContent": "a: 1"}})
        self.addCleanup(HELM.bind, *(getattr(HELM, name, None) for name in ("kget", "ksend")))
        HELM.bind(K.kget, K.ksend)
        HELM.upgrade({"namespace": "mon", "name": "grafana", "version": "8.0.0", "values": "a: 2"})
        op = self.job("helm", {"namespace": HELM.CONTROLLER_NS, "name": "helm-install-grafana", "action": "upgrade"},
                      "Helm upgrade grafana", {"kind": "HelmChart", "name": "grafana", "namespace": HELM.CONTROLLER_NS})
        self.assertEqual("rollback", OPS.cancel_plan(op)["mode"])
        OPS.cancel(op)
        chart = self.kube.object(path)
        self.assertEqual(("7.0.0", "a: 1"), (chart["spec"]["version"], chart["spec"]["valuesContent"]))
        self.assertNotIn(HELM.PREVIOUS_SPEC, chart["metadata"]["annotations"])

    def test_a_helm_install_cancelled_deletes_the_chart_so_the_controller_uninstalls_it(self):
        path = f"/apis/helm.cattle.io/v1/namespaces/{HELM.CONTROLLER_NS}/helmcharts/grafana"
        self.kube.add({"apiVersion": "helm.cattle.io/v1", "kind": "HelmChart",
                       "metadata": {"namespace": HELM.CONTROLLER_NS, "name": "grafana"}, "spec": {}})
        op = self.job("helm", {"namespace": HELM.CONTROLLER_NS, "name": "helm-install-grafana", "action": "install"},
                      "Helm install grafana", {"kind": "HelmChart", "name": "grafana", "namespace": HELM.CONTROLLER_NS})
        OPS.cancel(op)
        self.assertIsNone(self.kube.object(path))

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
        self.kube.add({"apiVersion": "apps/v1", "kind": "Deployment", "metadata": {"namespace": "hs", "name": "homestead"},
                       "spec": {"template": {"spec": {
                           "volumes": [{"name": "data", "persistentVolumeClaim": {"claimName": "homestead-data-shared"}}]}}}})
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
