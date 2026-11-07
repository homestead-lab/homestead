"""The cancellers the audit had to change (plans: homestead_cancel_rerun): each
cut off part-way through, then finished by the leader's next round, on the shared
fake API. A fake reply lost with the write applied is what a process going away
just after its write looks like from the cluster's side."""
import http.client
import os
import secrets
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
import homestead_cancel as CANCEL
import homestead_kube as K
import homestead_move_state as MOVE_STATE
import homestead_hostrun as HOSTRUN
import homestead_image_cache as IMAGES
import json
import homestead_operations as OPS
import homestead_revert as REVERT
import homestead_cdi_cleanup as CDI_CLEANUP
import homestead_components as COMPONENTS
import homestead_lhv2_upgrade as V2
import homestead_vm_power_job as LOCKS
import homestead_copy_job as COPY_JOB
import homestead_auth as AUTH
import homestead_deploy_server as DEPLOY_SERVER
import homestead_helm as HELM
import homestead_host_bonds as HOST_BONDS
import homestead_host_bridge as HOST_BRIDGE
import homestead_host_os as HOST_OS
import homestead_imports as IMPORTS
import homestead_lan as LAN
import homestead_network_state as NETWORK_STATE
import homestead_reclass as RECLASS
import homestead_restore_test as RESTORE_TEST
import homestead_unraid_vms as UNRAID
import server
from fakes.kube import Kube


def rerun():
    """The audited list. Imported here so that on code without it, the behaviour
    tests in this file still run and fail on behaviour, not on this import."""
    import homestead_cancel_rerun
    return homestead_cancel_rerun


DEP = "/apis/apps/v1/namespaces/lab/deployments/web"
SVC = "/api/v1/namespaces/lab/services/web"
SECRET = "/api/v1/namespaces/lab/secrets/web-env"
CLAIM = "/api/v1/namespaces/lab/persistentvolumeclaims/web-data"
REVISION = "deployment.kubernetes.io/revision"
LH = "/apis/longhorn.io/v1beta2/namespaces/longhorn-system"


class Died(BaseException):
    """The process going away: no handler of the job store's sees it."""


def stamp(seconds_ago):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - seconds_ago))


def deployment(**notes):
    return {"apiVersion": "apps/v1", "kind": "Deployment",
            "metadata": {"namespace": "lab", "name": "web", "annotations": dict(notes)},
            "spec": {"replicas": 1, "selector": {"matchLabels": {"app": "web"}},
                     "template": {"metadata": {"labels": {"app": "web"}},
                                  "spec": {"containers": [{"name": "web", "image": "web:new"}]}}}}


def service(name="web"):
    return {"apiVersion": "v1", "kind": "Service", "metadata": {"namespace": "lab", "name": name},
            "spec": {"selector": {"app": "web"}, "ports": [{"port": 80}]}}

# The modules these cancellers write through, each bound to the API when server.py runs.
BOUND_TO_THE_API = (DEPLOY_SERVER, HELM, IMPORTS, HOSTRUN, RECLASS, RESTORE_TEST, REVERT, UNRAID)


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        was = OPS.DATA_DIR
        OPS.DATA_DIR = self.tmp.name
        self.addCleanup(setattr, OPS, "DATA_DIR", was)
        self.addCleanup(CANCEL.bind, *(getattr(CANCEL, name, None) for name in (
            "kget", "ksend", "remove_workload", "tidy_restore")))
        self.kube = self.enterContext(Kube())
        CANCEL.bind(K.kget, K.ksend, server.delete_workload)
        self.bind_as_the_server_does()

    def bind_as_the_server_does(self):
        """What these cancellers read through their modules' bound names, as server.py binds it.
        Another test file may have left its own stand-ins there, and then a write never
        reaches the fake (so a cut-off never happens). Put back after each test."""
        names = [(module, name, value) for module in BOUND_TO_THE_API
                 for name, value in (("kget", K.kget), ("ksend", K.ksend))]
        # These only read here; AUTH's ksend is a wrapper of its own, and stays.
        names += [(module, "kget", K.kget) for module in (AUTH, LAN, NETWORK_STATE)]
        names += [(module, "hostrun", HOSTRUN) for module in (HOST_OS, HOST_BONDS, HOST_BRIDGE)]
        names += [(HOSTRUN, "NS", "lab"), (UNRAID, "OPS", OPS), (REVERT, "reclass", RECLASS)]
        for module, name, value in names:
            self.enterContext(mock.patch.object(module, name, value))

    def job(self, kind, ref, name="web"):
        return OPS.start(kind, "A job", {"kind": "Thing", "name": name, "namespace": "lab"}, "/", ref)["id"]

    def stored(self, op):
        return next(item for item in OPS.stored() if item["id"] == op)

    def cut_off(self, op, options=None, confirm=""):
        with self.assertRaises(Died):
            OPS.cancel(op, options, confirm=confirm, by="alice")
        self.assertEqual(OPS.CANCELLING, self.stored(op)["status"])

    def cut_off_in_the_loop(self, op, options=None, confirm=""):
        """A kind whose cancel the jobs loop applies, dying in that step."""
        OPS.cancel(op, options, confirm=confirm, by="alice")
        self.leader_round()
        OPS.STEPS.take_errors()
        self.assertEqual(OPS.CANCELLING, self.stored(op)["status"])

    def again(self, obj):
        """The same name made again: a new object, with a new uid."""
        before = self.kube.object(self.kube.path_of(obj))
        made = dict(obj, metadata={k: v for k, v in obj["metadata"].items() if k not in ("uid", "resourceVersion")})
        path = self.kube.add(made)
        self.assertNotEqual((before or {}).get("metadata", {}).get("uid"), self.kube.object(path)["metadata"]["uid"])
        return path, self.kube.object(path)["metadata"]["uid"]

    def survives(self, path, uid, looked_again=True):
        # The leader did look again, or surviving would prove nothing.
        said = [row.get("message", "") for row in OPS.stored()] + [
            entry.get("m", "") for row in OPS.stored() for entry in row.get("history") or []]
        self.assertTrue(not looked_again or any("Homestead restarted during this cancel" in words for words in said), said)
        found = self.kube.object(path)
        self.assertIsNotNone(found, f"{path} was deleted")
        self.assertEqual(uid, found["metadata"]["uid"])

    def leader_round(self):
        OPS.tick(lambda: True)
        OPS.STEPS.wait(5)

    def finished(self, op):
        item = self.stored(op)
        self.assertEqual("cancelled", item["status"], item["message"])
        return item["message"]


class DeployRemoved(Base):
    """A deploy undone by deleting it: the Deployment goes first."""
    def started(self):
        self.kube.add(deployment(**{"homestead.io/env-secret": "web-env"}))
        self.kube.add(service())
        self.kube.add({"apiVersion": "v1", "kind": "Secret",
                       "metadata": {"namespace": "lab", "name": "web-env", "labels": {"homestead.io/env-for": "web"}}})
        return self.job("deployment", {"namespace": "lab", "name": "web", "undo": "delete"})

    def test_a_cut_off_removal_still_removes_the_services_it_would_have(self):
        op = self.started()
        self.kube.fail(DEP, Died(), method="DELETE", applied=True)
        self.cut_off(op)
        self.assertIsNone(self.kube.object(DEP))
        self.assertIsNotNone(self.kube.object(SVC))
        self.leader_round()
        self.finished(op)
        self.assertIsNone(self.kube.object(SVC))

    def test_it_says_it_removed_the_app_not_that_it_was_already_gone(self):
        op = self.started()
        self.kube.fail(DEP, Died(), method="DELETE", applied=True)
        self.cut_off(op)
        self.leader_round()
        self.assertNotIn("already gone", self.finished(op))

    def test_a_new_app_of_that_name_made_since_is_not_deleted(self):
        op = self.started()
        self.kube.fail(DEP, Died(), method="DELETE", applied=True)
        self.cut_off(op)
        self.kube.add(deployment())
        self.leader_round()
        self.assertIn("kept", self.finished(op))
        self.assertIsNotNone(self.kube.object(DEP))

    def test_a_new_service_of_that_name_made_since_is_not_deleted(self):
        op = self.started()
        self.kube.fail(DEP, Died(), method="DELETE", applied=True)
        self.cut_off(op)
        self.kube.discard(SVC)
        self.kube.add(service())
        self.leader_round()
        self.finished(op)
        self.assertIsNotNone(self.kube.object(SVC))


class RolledBack(Base):
    """A rollout undone. Kubernetes gives the ReplicaSet gone back to the top
    revision, so 'one back' is then the job's own new version."""
    def rs(self, name, revision, image, age):
        uid = self.kube.object(DEP)["metadata"]["uid"]
        return {"apiVersion": "apps/v1", "kind": "ReplicaSet",
                "metadata": {"namespace": "lab", "name": name, "labels": {"app": "web"},
                             "creationTimestamp": stamp(age), "annotations": {REVISION: str(revision)},
                             "ownerReferences": [{"kind": "Deployment", "name": "web", "uid": uid}]},
                "spec": {"template": {"metadata": {"labels": {"app": "web", "pod-template-hash": name}},
                                      "spec": {"containers": [{"name": "web", "image": image}]}}}}

    def started(self):
        """web:old was itself rolled out a minute before this job, within CLOCK_SLACK."""
        self.kube.add(deployment(**{REVISION: "2"}))
        self.kube.add(self.rs("web-old", 1, "web:old", 60))
        self.kube.add(self.rs("web-new", 2, "web:new", 5))
        return self.job("deployment", {"namespace": "lab", "name": "web", "undo": "rollout"})

    def image(self):
        return self.kube.object(DEP)["spec"]["template"]["spec"]["containers"][0]["image"]

    def controller_moves_on(self):
        """What the Deployment controller does once the template matches web-old."""
        dep = self.kube.object(DEP)
        dep["metadata"]["annotations"][REVISION] = "3"
        self.kube.add(dep)
        old = self.kube.object("/apis/apps/v1/namespaces/lab/replicasets/web-old")
        old["metadata"]["annotations"][REVISION] = "3"
        self.kube.add(old)

    def test_a_cut_off_rollback_is_not_rolled_forward_again(self):
        op = self.started()
        self.kube.fail(DEP, Died(), method="PUT", applied=True)
        self.cut_off(op)
        self.assertEqual("web:old", self.image())
        self.controller_moves_on()
        self.leader_round()
        self.finished(op)
        self.assertEqual("web:old", self.image())

    def test_it_says_it_went_back_not_that_the_change_stays(self):
        op = self.started()
        self.kube.fail(DEP, Died(), method="PUT", applied=True)
        self.cut_off(op)
        self.controller_moves_on()
        self.leader_round()
        message = self.finished(op)
        self.assertIn("going back", message)
        self.assertNotIn("the change stays", message)

    def test_a_rollback_that_never_landed_is_sent_when_it_runs_again(self):
        op = self.started()
        self.kube.fail(DEP, Died(), method="PUT")
        self.cut_off(op)
        self.assertEqual("web:new", self.image())
        self.leader_round()
        self.finished(op)
        self.assertEqual("web:old", self.image())

    def test_a_person_changing_it_after_the_rollback_keeps_their_change(self):
        op = self.started()
        self.kube.fail(DEP, Died(), method="PUT", applied=True)
        self.cut_off(op)
        self.controller_moves_on()
        dep = self.kube.object(DEP)
        dep["spec"]["template"]["spec"]["containers"][0]["image"] = "web:theirs"
        self.kube.add(dep)
        self.leader_round()
        self.finished(op)
        self.assertEqual("web:theirs", self.image())


class Backup(Base):
    PATH = f"{LH}/backups/backup-1"
    SNAP = f"{LH}/snapshots/snap-1"

    def test_a_cut_off_cancel_still_removes_the_snapshot_named_by_the_backup_it_deleted(self):
        self.kube.add({"apiVersion": "longhorn.io/v1beta2", "kind": "Backup",
                       "metadata": {"namespace": "longhorn-system", "name": "backup-1"},
                       "spec": {"snapshotName": "snap-1"}})
        self.kube.add({"apiVersion": "longhorn.io/v1beta2", "kind": "Snapshot",
                       "metadata": {"namespace": "longhorn-system", "name": "snap-1"}, "spec": {}})
        op = self.job("backup", {"name": "backup-1"})
        self.kube.fail(self.PATH, Died(), method="DELETE", applied=True)
        self.cut_off(op)
        self.assertIsNone(self.kube.object(self.PATH))
        self.leader_round()
        self.finished(op)
        self.assertIsNone(self.kube.object(self.SNAP))


class OlderImport(Base):
    JOB = "/apis/batch/v1/namespaces/lab/jobs/homestead-import-web"

    def started(self):
        self.kube.add(deployment())
        self.kube.add({"apiVersion": "v1", "kind": "PersistentVolumeClaim",
                       "metadata": {"namespace": "lab", "name": "web-data"}, "spec": {}})
        self.kube.add({"apiVersion": "batch/v1", "kind": "Job",
                       "metadata": {"namespace": "lab", "name": "homestead-import-web", "labels": {"app": "web"},
                                    "annotations": {"homestead.io/import-workload": "web",
                                                    "homestead.io/import-volumes": "web-data",
                                                    "homestead.io/import-volumes-created": "web-data"}},
                       "spec": {}})
        return self.job("import", {"namespace": "lab", "name": "homestead-import-web"})

    def test_a_cut_off_cancel_still_removes_what_the_deleted_job_named(self):
        op = self.started()
        self.kube.fail(self.JOB, Died(), method="DELETE", applied=True)
        self.cut_off(op, {"volumes": True})
        self.assertIsNone(self.kube.object(self.JOB))
        self.leader_round()
        message = self.finished(op)
        self.assertIsNone(self.kube.object(DEP))
        self.assertIsNone(self.kube.object(CLAIM))
        self.assertIn("web removed", message)

    def test_a_person_who_unticked_the_volumes_keeps_them_when_it_runs_again(self):
        op = self.started()
        self.kube.fail(self.JOB, Died(), method="DELETE", applied=True)
        self.cut_off(op, {"volumes": False})
        self.leader_round()
        self.finished(op)
        self.assertIsNotNone(self.kube.object(CLAIM))

    def test_a_workload_already_deleted_does_not_fail_the_cancel(self):
        op = self.started()
        self.kube.fail(DEP, Died(), method="DELETE", applied=True)
        self.cut_off(op, {"volumes": True})
        self.leader_round()
        self.finished(op)
        self.assertIsNone(self.kube.object(CLAIM))


class SameNameSince(Base):
    def test_a_service_made_again_since_is_not_deleted(self):
        self.kube.add(service())
        op = self.job("network-service", {"namespace": "lab", "name": "web"})
        self.kube.fail(SVC, Died(), method="DELETE", applied=True)
        self.cut_off(op)
        self.kube.add(service())
        self.leader_round()
        self.finished(op)
        self.assertIsNotNone(self.kube.object(SVC))

    def test_a_disk_made_again_since_is_not_deleted(self):
        volume = "/apis/cdi.kubevirt.io/v1beta1/namespaces/lab/datavolumes/web-data"
        made = {"apiVersion": "cdi.kubevirt.io/v1beta1", "kind": "DataVolume",
                "metadata": {"namespace": "lab", "name": "web-data"}, "spec": {}}
        self.kube.add(made)
        claim = {"apiVersion": "v1", "kind": "PersistentVolumeClaim",
                 "metadata": {"namespace": "lab", "name": "web-data"}, "spec": {}}
        self.kube.add(claim)
        op = self.job("vm-disk-import", {"namespace": "lab", "name": "web-data"})
        self.kube.fail(volume, Died(), method="DELETE", applied=True)
        self.cut_off(op)
        self.kube.add(made)
        self.leader_round()
        self.finished(op)
        self.assertIsNotNone(self.kube.object(volume))
        self.assertIsNone(self.kube.object(CLAIM))


class Helm(Base):
    def test_a_cut_off_upgrade_rollback_says_it_went_back(self):
        path = "/apis/helm.cattle.io/v1/namespaces/kube-system/helmcharts/app"
        self.kube.add({"apiVersion": "helm.cattle.io/v1", "kind": "HelmChart",
                       "metadata": {"namespace": "kube-system", "name": "app",
                                    "annotations": {"homestead.io/helm-previous": '{"version": "1.0.0"}'}},
                       "spec": {"version": "2.0.0"}})
        op = self.job("helm", {"action": "upgrade", "name": "helm-install-app"}, name="app")
        self.kube.fail(path, Died(), method="PUT", applied=True)
        self.cut_off(op)
        self.assertEqual("1.0.0", self.kube.object(path)["spec"]["version"])
        self.leader_round()
        message = self.finished(op)
        self.assertNotIn("carries on", message)
        self.assertIn("settings from before the upgrade", message)


class Move(Base):
    def test_a_move_whose_record_is_gone_ends_as_its_plan_says(self):
        op = self.job("move", {"move": "0123456789ab"})
        with mock.patch.object(MOVE_STATE, "DATA_DIR", self.tmp.name):
            self.assertEqual("forget", OPS.cancel_plan(op)["mode"])
            OPS.cancel(op)
        self.finished(op)


class NeverStuck(Base):
    """A plan that has come to refuse must not leave a job cancelling for good."""
    def test_a_copy_whose_workload_was_replaced_leaves_cancelling(self):
        path = self.kube.add(deployment(**{COPY_JOB.MARKER: "placeholder"}))
        first = self.kube.object(path)["metadata"]["uid"]
        op = self.job("workload-copy", {"namespace": "lab", "name": "web", "deployment": {"uid": first},
                                        "review_digest": "b" * 64})
        marked = self.kube.object(path)
        marked["metadata"]["annotations"][COPY_JOB.MARKER] = op
        self.kube.add(marked)
        self.kube.fail(path, Died(), method="PATCH", applied=True)
        self.cut_off_in_the_loop(op, {"ack": True}, confirm="web")
        self.again(deployment(**{COPY_JOB.MARKER: op}))
        self.leader_round()
        item = self.stored(op)
        self.assertNotEqual(OPS.CANCELLING, item["status"])
        self.assertTrue(any("replaced" in row.get("m", "") for row in item["history"]), item["history"])

    def test_an_image_update_whose_rollback_has_appeared_leaves_cancelling(self):
        self.kube.add(deployment())
        op = self.job("image-update", {"namespace": "lab", "name": "web"})
        self.kube.fail(DEP, Died(), method="GET", skip=1)
        self.cut_off(op)
        dep = self.kube.object(DEP)
        dep["metadata"]["annotations"][CANCEL.UPDATES.PREVIOUS] = json.dumps({"images": {"web": "web:old"}})
        self.kube.add(dep)
        self.leader_round()
        item = self.stored(op)
        self.assertNotEqual(OPS.CANCELLING, item["status"])
        self.assertTrue(any("fresh image and capacity review" in row.get("m", "") for row in item["history"]), item["history"])


class ABlipIsNotARefusal(Base):
    """"A plan that can't be read waits": an API error is not the plan saying no."""
    def held_copy(self):
        path = self.kube.add(deployment(**{COPY_JOB.MARKER: "placeholder"}))
        first = self.kube.object(path)["metadata"]["uid"]
        op = self.job("workload-copy", {"namespace": "lab", "name": "web", "deployment": {"uid": first},
                                        "review_digest": secrets.token_hex(32)})
        marked = self.kube.object(path)
        marked["metadata"]["annotations"][COPY_JOB.MARKER] = op
        self.kube.add(marked)
        return op, path

    def test_a_copy_waits_out_an_api_error_and_a_timeout(self):
        for blip in (503, TimeoutError("timed out")):
            with self.subTest(blip=blip):
                op, path = self.held_copy()
                self.kube.fail(path, Died(), method="PATCH", applied=True)
                self.cut_off_in_the_loop(op, {"ack": True}, confirm="web")
                reruns = self.stored(op)["cancel_record"]["reruns"]
                self.kube.fail(path, blip, method="GET")
                self.leader_round()
                OPS.STEPS.take_errors()
                item = self.stored(op)
                self.assertEqual(OPS.CANCELLING, item["status"], item["message"])
                self.assertEqual(reruns, item["cancel_record"]["reruns"])
                self.leader_round()
                self.assertEqual("cancelled", self.stored(op)["status"], self.stored(op)["message"])
                self.kube.discard(path)

    def test_a_rename_waits_out_an_api_error(self):
        self.kube.add(deployment())
        real = OPS.CANCELLERS["workload-rename"]
        died = []

        def once(item, options):
            if not died:
                died.append(True)
                raise Died()
            return real[1](item, options)
        self.enterContext(mock.patch.dict(OPS.CANCELLERS, {"workload-rename": (real[0], once)}))
        op = self.job("workload-rename", {"namespace": "lab", "name": "web", "new_name": "web2", "review_digest": "c" * 64})
        self.cut_off(op, {"ack": True}, confirm="web")
        self.kube.fail(DEP, 503, method="GET")
        self.leader_round()
        OPS.STEPS.take_errors()
        self.assertEqual(OPS.CANCELLING, self.stored(op)["status"], self.stored(op)["message"])
        self.assertFalse(any("did not finish" in row.get("m", "") for row in self.stored(op)["history"]))
        self.leader_round()
        self.assertEqual("cancelled", self.stored(op)["status"], self.stored(op)["message"])

    def test_a_person_pressing_cancel_during_a_blip_changes_nothing(self):
        op, path = self.held_copy()
        self.kube.add({**self.kube.object(path)})
        self.kube.fail(path, 503, method="GET")
        with self.assertRaises(OSError):
            OPS.cancel_plan(op)
        self.kube.fail(path, 503, method="GET")
        with self.assertRaises(OSError):
            OPS.cancel(op, {"ack": True}, confirm="web")
        item = self.stored(op)
        self.assertNotEqual(OPS.CANCELLING, item["status"])
        self.assertNotIn("cancel_request", item)
        self.assertFalse(any("did not finish" in row.get("m", "") for row in item.get("history") or []))

    def test_a_workload_really_gone_is_still_a_refusal(self):
        op, path = self.held_copy()
        self.kube.fail(path, Died(), method="PATCH", applied=True)
        self.cut_off_in_the_loop(op, {"ack": True}, confirm="web")
        self.kube.discard(path)
        self.leader_round()
        item = self.stored(op)
        self.assertNotEqual(OPS.CANCELLING, item["status"])
        self.assertTrue(any("did not finish" in row.get("m", "") for row in item["history"]))


class AFreshReviewLooksAfresh(Base):
    """What a cancel saved fences a run of that same cancel. A person who has reviewed
    it again, after it failed or parked, has reviewed what is there now."""
    def test_a_deployment_replaced_after_a_failed_first_cancel_is_what_they_reviewed(self):
        self.kube.add(deployment())
        op = self.job("deployment", {"namespace": "lab", "name": "web", "undo": "delete"})
        self.kube.fail(DEP, 500, method="DELETE")
        with self.assertRaises(ValueError):
            OPS.cancel(op, by="alice")
        self.assertIn("cancel_removing", self.stored(op)["ref"])
        self.again(deployment())
        result = OPS.cancel(op, by="alice")
        self.assertIsNone(self.kube.object(DEP))
        self.assertIn("web was removed", result["detail"])

    def test_a_service_replaced_after_a_parked_cancel_is_what_they_reviewed(self):
        self.kube.add(service())
        op = self.job("network-service", {"namespace": "lab", "name": "web"})
        self.kube.fail(SVC, Died(), method="DELETE", applied=True)
        self.cut_off(op)
        items = OPS.stored()
        items[-1]["cancel_record"]["at"] = time.time() - OPS.CANCEL_REVIEW_KEEPS - 1
        with OPS.transaction() as tx:
            tx.save(items)
        self.leader_round()
        self.assertIn("was not run again", self.stored(op)["message"])
        self.kube.add(service())
        OPS.cancel(op, by="alice")
        self.assertIsNone(self.kube.object(SVC))

    def test_an_import_job_replaced_after_a_failed_first_cancel_is_what_they_reviewed(self):
        op = OlderImport.started(self)
        self.kube.fail(OlderImport.JOB, 500, method="DELETE")
        with self.assertRaises(ValueError):
            OPS.cancel(op, {"volumes": True}, by="alice")
        new = {"apiVersion": "batch/v1", "kind": "Job",
               "metadata": {"namespace": "lab", "name": "homestead-import-web", "labels": {"app": "web"}}, "spec": {}}
        self.again(new)
        OPS.cancel(op, {}, by="alice")
        self.assertIsNone(self.kube.object(OlderImport.JOB))

    def test_a_run_of_the_same_cancel_still_keeps_what_is_new(self):
        self.kube.add(deployment())
        op = self.job("deployment", {"namespace": "lab", "name": "web", "undo": "delete"})
        self.kube.fail(DEP, Died(), method="DELETE", applied=True)
        self.cut_off(op)
        path, uid = self.again(deployment())
        self.leader_round()
        self.survives(path, uid)

    def test_after_a_failed_first_cancel_what_it_had_removed_still_gets_finished(self):
        self.kube.add(deployment())
        self.kube.add(service())
        op = self.job("deployment", {"namespace": "lab", "name": "web", "undo": "delete"})
        self.kube.fail(DEP, 500, method="DELETE", applied=True)
        with self.assertRaises(ValueError):
            OPS.cancel(op, by="alice")
        self.assertIsNone(self.kube.object(DEP))
        OPS.cancel(op, by="alice")
        self.assertIsNone(self.kube.object(SVC))


class SecondReviewRound(Base):
    def v2_job(self):
        return self.job("platform-upgrade", {"component": "longhorn", "v2_mode": "live"})

    def v2_mocks(self, **set_kwargs):
        patches = (mock.patch.object(COMPONENTS, "longhorn_version", return_value="1.13.0"),
                   mock.patch.object(V2, "_setting", return_value=({}, {"v2": "true"})),
                   mock.patch.object(V2, "_set", **set_kwargs))
        return [self.enterContext(one) for one in patches][-1]

    def test_a_pause_that_failed_is_sent_again_by_a_persons_retry(self):
        op = self.v2_job()
        failed = self.v2_mocks(side_effect=RuntimeError("API 500"))
        with self.assertRaises(ValueError):
            OPS.cancel(op, by="alice")
        self.assertEqual(1, failed.call_count)
        self.assertNotIn("v2_pause_sent", self.stored(op)["ref"])
        failed.side_effect = None
        result = OPS.cancel(op, by="alice")
        self.assertEqual(2, failed.call_count)
        self.assertNotIn("before it restarted", result["detail"])
        self.assertIn("Paused", result["detail"])

    def test_a_person_reviewing_a_parked_pause_sends_it_again(self):
        op = self.v2_job()
        sent = self.v2_mocks(side_effect=Died())
        self.cut_off(op)
        items = OPS.stored()
        items[-1]["cancel_record"]["at"] = time.time() - OPS.CANCEL_REVIEW_KEEPS - 1
        with OPS.transaction() as tx:
            tx.save(items)
        self.leader_round()
        self.assertIn("was not run again", self.stored(op)["message"])
        sent.side_effect = None
        OPS.cancel(op, by="alice")
        self.assertEqual(2, sent.call_count)

    def test_the_leaders_own_rerun_still_does_not_ask_again(self):
        op = self.v2_job()
        sent = self.v2_mocks(side_effect=Died())
        self.cut_off(op)
        sent.side_effect = None
        self.leader_round()
        self.assertEqual(1, sent.call_count)
        self.assertIn("check that setting", self.finished(op))

    def pulls(self):
        paths = []
        for name in ("homestead-pull-a", "homestead-pull-b"):
            paths.append(self.kube.add({"apiVersion": "apps/v1", "kind": "DaemonSet",
                                        "metadata": {"namespace": IMAGES.I.NS, "name": name}, "spec": {}}))
        return self.job("container-rebalance", {"pulls": ["homestead-pull-a", "homestead-pull-b"], "moves": []}), paths

    def test_one_pull_failing_leaves_the_others_cleared_and_is_named(self):
        op, paths = self.pulls()
        self.kube.fail(paths[0], 500, method="DELETE")
        OPS.cancel(op, by="alice")
        self.assertIsNone(self.kube.object(paths[1]))
        message = self.finished(op)
        self.assertIn("homestead-pull-a", message)
        self.assertNotIn("homestead-pull-b", message)

    def test_all_pulls_are_saved_before_any_goes(self):
        op, paths = self.pulls()
        self.kube.fail(paths[0], Died(), method="DELETE", applied=True)
        self.cut_off(op)
        new = {"apiVersion": "apps/v1", "kind": "DaemonSet", "metadata": {"namespace": IMAGES.I.NS, "name": "homestead-pull-b"}, "spec": {}}
        path, uid = self.again(new)
        self.leader_round()
        self.survives(path, uid)

    def test_the_words_read_right_for_one_pull_and_for_several(self):
        op, paths = self.pulls()
        self.kube.fail(paths[0], 500, method="DELETE")
        self.kube.fail(paths[1], 500, method="DELETE")
        OPS.cancel(op, by="alice")
        message = self.finished(op)
        self.assertIn("homestead-pull-a and homestead-pull-b could not be stopped: delete them", message)
        self.kube.discard(paths[0])
        op2, paths2 = self.pulls()
        self.kube.fail(paths2[0], 500, method="DELETE")
        OPS.cancel(op2, by="alice")
        self.assertIn("homestead-pull-a could not be stopped: delete it", self.finished(op2))

    def test_a_read_that_fails_some_other_way_after_the_deletes_still_ends(self):
        self.kube.add(service())
        op = self.job("network-service", {"namespace": "lab", "name": "web"})
        self.kube.fail(SVC, http.client.IncompleteRead(b""), method="GET", skip=2)
        OPS.cancel(op, by="alice")
        self.assertIsNone(self.kube.object(SVC))
        self.assertIn("could not be read", self.finished(op))

    def test_an_import_read_that_fails_some_other_way_still_ends(self):
        op = OlderImport.started(self)
        self.kube.fail(CLAIM, http.client.IncompleteRead(b""), method="GET", skip=1)
        OPS.cancel(op, {"volumes": True}, by="alice")
        self.assertIn("could not be read", self.finished(op))

    def test_a_pull_that_cannot_be_read_does_not_stop_the_others(self):
        op, paths = self.pulls()
        self.kube.fail(paths[0], 503, method="GET")
        OPS.cancel(op, by="alice")
        self.assertIsNone(self.kube.object(paths[1]))
        self.finished(op)

    def test_an_import_says_a_claim_it_kept_and_only_deleted_what_went(self):
        op = OlderImport.started(self)
        self.kube.add(claim("web-more"))
        job = self.kube.object(OlderImport.JOB)
        job["metadata"]["annotations"].update({"homestead.io/import-volumes": "web-data,web-more",
                                                "homestead.io/import-volumes-created": "web-data,web-more"})
        self.kube.add(job)
        self.kube.fail(OlderImport.JOB, Died(), method="DELETE", applied=True)
        self.cut_off(op, {"volumes": True})
        self.kube.discard(CLAIM)
        self.again(claim())
        self.leader_round()
        message = self.finished(op)
        self.assertIn(", web-more deleted", message)
        self.assertNotIn("web-data and web-more deleted", message)
        self.assertIn("web-data was made again", message)
        self.assertIsNotNone(self.kube.object(CLAIM))

    def test_an_import_whose_claim_cannot_be_read_after_its_delete_still_ends(self):
        op = OlderImport.started(self)
        self.kube.fail(CLAIM, 503, method="GET", skip=1)     # past the save's read
        OPS.cancel(op, {"volumes": True}, by="alice")
        self.assertIsNone(self.kube.object(CLAIM))
        self.assertIn("could not be read", self.finished(op))

    def test_a_read_failing_after_the_deletes_does_not_undo_a_finished_cancel(self):
        self.kube.add(service())
        op = self.job("network-service", {"namespace": "lab", "name": "web"})
        self.kube.fail(SVC, 503, method="GET", skip=2)     # past the review's read and the save's
        OPS.cancel(op, by="alice")
        self.assertIsNone(self.kube.object(SVC))
        message = self.finished(op)
        self.assertIn("could not be read", message)


class ABusyWorkerLockWaits(Base):
    """Its own dispatcher holding the lock says nothing about the job: wait, not refuse."""
    def copy(self):
        path = self.kube.add(deployment(**{COPY_JOB.MARKER: "placeholder"}))
        first = self.kube.object(path)["metadata"]["uid"]
        op = self.job("workload-copy", {"namespace": "lab", "name": "web", "deployment": {"uid": first},
                                        "review_digest": secrets.token_hex(32)})
        marked = self.kube.object(path)
        marked["metadata"]["annotations"][COPY_JOB.MARKER] = op
        self.kube.add(marked)
        self.kube.fail(path, Died(), method="PATCH", applied=True)
        self.cut_off_in_the_loop(op, {"ack": True}, confirm="web")
        return op

    def rename(self):
        self.kube.add(deployment())
        real = OPS.CANCELLERS["workload-rename"]
        died = []

        def once(item, options):
            if not died:
                died.append(True)
                raise Died()
            return real[1](item, options)
        self.enterContext(mock.patch.dict(OPS.CANCELLERS, {"workload-rename": (real[0], once)}))
        op = self.job("workload-rename", {"namespace": "lab", "name": "web", "new_name": "web2", "review_digest": secrets.token_hex(32)})
        self.cut_off(op, {"ack": True}, confirm="web")
        return op

    def check_waits(self, op):
        with LOCKS.worker_lock(op, OPS):
            self.leader_round()
            OPS.STEPS.take_errors()
            item = self.stored(op)
            self.assertEqual(OPS.CANCELLING, item["status"], item["message"])
            self.assertFalse(item["cancel_record"].get("parked"))
            self.assertFalse(any("did not finish" in row.get("m", "") for row in item["history"]))
        self.leader_round()
        self.assertEqual("cancelled", self.stored(op)["status"], self.stored(op)["message"])

    def test_a_copy_waits(self):
        self.check_waits(self.copy())

    def test_a_rename_waits(self):
        self.check_waits(self.rename())

    def test_a_person_pressing_cancel_meanwhile_is_told_and_the_job_stays(self):
        op = self.rename()
        items = OPS.stored()
        items[-1]["cancel_started"] = time.time() - OPS.CANCEL_RETRY_AFTER - 1
        with OPS.transaction() as tx:
            tx.save(items)
        with LOCKS.worker_lock(op, OPS):
            with self.assertRaises(ValueError):
                OPS.cancel(op, {"ack": True}, confirm="web")
        self.assertEqual(OPS.CANCELLING, self.stored(op)["status"])
        self.assertFalse(any("did not finish" in row.get("m", "") for row in self.stored(op)["history"]))

    def lock_that_cannot_open(self, op):
        """Whoever opens it, open() raises IsADirectoryError, as a real error on the volume would."""
        path = os.path.join(OPS.DATA_DIR, ".locks", f"vm-power-{op}.lock")
        if os.path.exists(path):
            os.remove(path)
        os.makedirs(path)
        return path

    def later(self, seconds):
        real = time.time
        return mock.patch.object(OPS.time, "time", lambda: real() + seconds)

    def check_gives_back(self, op):
        self.lock_that_cannot_open(op)
        self.leader_round()
        OPS.STEPS.take_errors()
        self.assertEqual(OPS.CANCELLING, self.stored(op)["status"], "a passing error is waited out")
        with self.later(OPS.CANCEL_SLOW_AFTER - 60):
            self.leader_round()
            self.assertEqual(OPS.CANCELLING, self.stored(op)["status"])
            with self.assertRaises(ValueError):
                OPS.cancel(op, {"ack": True}, confirm="web")
            self.assertEqual(OPS.CANCELLING, self.stored(op)["status"], "told, and it stays")
        with self.later(OPS.CANCEL_SLOW_AFTER + 60):
            self.leader_round()
        item = self.stored(op)
        self.assertNotEqual(OPS.CANCELLING, item["status"])
        self.assertTrue(any("could not be opened" in row.get("m", "") and "did not finish" in row.get("m", "")
                            for row in item["history"]), item["history"])

    def test_a_copy_whose_lock_cannot_be_opened_goes_back_after_ten_minutes(self):
        self.check_gives_back(self.copy())

    def test_a_rename_whose_lock_cannot_be_opened_goes_back_after_ten_minutes(self):
        self.check_gives_back(self.rename())

    def check_clears(self, op):
        path = self.lock_that_cannot_open(op)
        self.leader_round()
        self.assertEqual(OPS.CANCELLING, self.stored(op)["status"])
        os.rmdir(path)
        with self.later(OPS.CANCEL_SLOW_AFTER - 60):
            self.leader_round()
        item = self.stored(op)
        self.assertEqual("cancelled", item["status"], item["message"])
        self.assertFalse(any("did not finish" in row.get("m", "") for row in item["history"]))

    def test_a_copy_whose_lock_error_clears_carries_on(self):
        self.check_clears(self.copy())

    def test_a_rename_whose_lock_error_clears_carries_on(self):
        self.check_clears(self.rename())

    def test_a_new_error_after_a_rerun_gets_its_own_ten_minutes(self):
        op = self.rename()
        path = self.lock_that_cannot_open(op)
        self.leader_round()
        os.rmdir(path)
        real = OPS.CANCELLERS["workload-rename"]

        def dies(item, options):
            raise Died()
        with mock.patch.dict(OPS.CANCELLERS, {"workload-rename": (real[0], dies)}):
            self.leader_round()
            OPS.STEPS.take_errors()
        self.assertEqual(1, self.stored(op)["cancel_record"]["reruns"])
        with self.later(OPS.CANCEL_SLOW_AFTER + 60):
            self.lock_that_cannot_open(op)
            self.leader_round()
            self.assertEqual(OPS.CANCELLING, self.stored(op)["status"])

    def test_the_ten_minutes_count_from_the_error_not_from_the_mark(self):
        op = self.rename()
        items = OPS.stored()
        items[-1]["cancel_started"] = time.time() - OPS.CANCEL_SLOW_AFTER - 600
        with OPS.transaction() as tx:
            tx.save(items)
        self.lock_that_cannot_open(op)
        self.leader_round()
        self.assertEqual(OPS.CANCELLING, self.stored(op)["status"])

    def test_a_person_pressing_cancel_after_the_ten_minutes_frees_it(self):
        op = self.rename()
        self.lock_that_cannot_open(op)
        self.leader_round()
        with self.later(OPS.CANCEL_SLOW_AFTER + 60):
            with self.assertRaises(ValueError):
                OPS.cancel(op, {"ack": True}, confirm="web")
        self.assertNotEqual(OPS.CANCELLING, self.stored(op)["status"])

    def hold_for_ever(self, op):
        got, done = threading.Event(), threading.Event()

        def hold():
            with LOCKS.worker_lock(op, OPS):
                got.set()
                done.wait(20)
        thread = threading.Thread(target=hold)
        thread.start()
        self.addCleanup(thread.join)
        self.addCleanup(done.set)
        self.assertTrue(got.wait(5))
        return done, thread

    def test_a_lock_busy_for_ever_waits_and_says_a_restart_frees_it(self):
        op = self.rename()
        done, thread = self.hold_for_ever(op)
        with self.later(30 * 86400):
            self.leader_round()
            item = self.stored(op)
            self.assertEqual(OPS.CANCELLING, item["status"])
            self.assertIn("restart", item["message"].lower())
            with self.assertRaises(ValueError) as refused:
                OPS.cancel(op, {"ack": True}, confirm="web")
            self.assertIn("restart", str(refused.exception).lower())
            self.assertEqual(OPS.CANCELLING, self.stored(op)["status"])
        done.set()
        thread.join(5)
        self.leader_round()
        self.assertEqual("cancelled", self.stored(op)["status"], self.stored(op)["message"])

    def test_its_words_on_a_restart_are_true_past_the_review_limit(self):
        op = self.rename()
        items = OPS.stored()
        items[-1]["cancel_record"]["at"] = time.time() - OPS.CANCEL_REVIEW_KEEPS - 1
        items[-1]["cancel_started"] = time.time() - OPS.CANCEL_RETRY_AFTER - 1
        with OPS.transaction() as tx:
            tx.save(items)
        self.hold_for_ever(op)
        with self.assertRaises(ValueError) as refused:
            OPS.cancel(op, {"ack": True}, confirm="web")
        self.assertIn("waits for you", str(refused.exception))

    def test_it_says_so_once_it_has_waited_long(self):
        op = self.rename()
        items = OPS.stored()
        items[-1]["cancel_started"] = time.time() - OPS.CANCEL_SLOW_AFTER - 1
        with OPS.transaction() as tx:
            tx.save(items)
        with LOCKS.worker_lock(op, OPS):
            self.leader_round()
        self.assertIn("Rename is still running", self.stored(op)["message"])


class ReviewedTargetMoved(RolledBack):
    """Someone's own rollout, after the cancel was reviewed."""
    def test_a_later_rollout_is_not_rolled_back_to_the_cancelled_version(self):
        op = self.started()
        self.kube.fail(DEP, Died(), method="PUT")     # never landed
        self.cut_off(op)
        dep = self.kube.object(DEP)
        dep["metadata"]["annotations"][REVISION] = "3"
        dep["spec"]["template"]["spec"]["containers"][0]["image"] = "web:theirs"
        self.kube.add(dep)
        self.kube.add(self.rs("web-theirs", 3, "web:theirs", 1))
        self.leader_round()
        self.assertEqual("web:theirs", self.image())
        self.assertEqual(OPS.CANCELLING, self.stored(op)["status"])
        self.assertIn("was not run again", self.stored(op)["message"])

    def test_an_older_earlier_version_still_finishes_by_its_marker(self):
        """web:old from long before the job: past CLOCK_SLACK its current version reads
        as not this job's, so only the marker says the rollback was its own."""
        self.kube.add(deployment(**{REVISION: "2"}))
        self.kube.add(self.rs("web-old", 1, "web:old", 3600))
        self.kube.add(self.rs("web-new", 2, "web:new", 5))
        op = self.job("deployment", {"namespace": "lab", "name": "web", "undo": "rollout"})
        self.kube.fail(DEP, Died(), method="PUT", applied=True)
        self.cut_off(op)
        self.controller_moves_on()
        self.leader_round()
        self.assertIn("going back", self.finished(op))
        self.assertEqual("web:old", self.image())


class HelmLaterUpgrade(Base):
    PATH = "/apis/helm.cattle.io/v1/namespaces/kube-system/helmcharts/app"

    def chart(self):
        self.kube.add({"apiVersion": "helm.cattle.io/v1", "kind": "HelmChart",
                       "metadata": {"namespace": "kube-system", "name": "app",
                                    "annotations": {"homestead.io/helm-previous": '{"version": "1.0.0"}'}},
                       "spec": {"version": "2.0.0"}})
        return self.job("helm", {"action": "upgrade", "name": "helm-install-app"}, name="app")

    def test_a_new_upgrade_since_the_rollback_landed_is_not_undone(self):
        op = self.chart()
        self.kube.fail(self.PATH, Died(), method="PUT", applied=True)
        self.cut_off(op)
        chart = self.kube.object(self.PATH)
        self.assertEqual("1.0.0", chart["spec"]["version"])
        chart["spec"]["version"] = "3.0.0"
        chart["metadata"]["annotations"]["homestead.io/helm-previous"] = '{"version": "1.0.0"}'
        self.kube.add(chart)
        self.leader_round()
        self.assertEqual("3.0.0", self.kube.object(self.PATH)["spec"]["version"])
        self.assertIn("was not run again", self.stored(op)["message"])

    def test_a_new_upgrade_before_the_rollback_landed_is_not_undone(self):
        op = self.chart()
        self.kube.fail(self.PATH, Died(), method="PUT")
        self.cut_off(op)
        chart = self.kube.object(self.PATH)
        chart["spec"]["version"] = "3.0.0"
        self.kube.add(chart)
        self.leader_round()
        self.assertEqual("3.0.0", self.kube.object(self.PATH)["spec"]["version"])
        self.assertEqual(OPS.CANCELLING, self.stored(op)["status"])

    def test_nothing_changed_and_the_rollback_not_landed_runs_again(self):
        op = self.chart()
        self.kube.fail(self.PATH, Died(), method="PUT")
        self.cut_off(op)
        self.leader_round()
        self.finished(op)
        self.assertEqual("1.0.0", self.kube.object(self.PATH)["spec"]["version"])


class HelmRunningUpgradeWithItsRecord(Base):
    """An upgrade from this version records the chart it wrote: uid and generation."""
    PATH = "/apis/helm.cattle.io/v1/namespaces/kube-system/helmcharts/app"

    def chart(self, **ref):
        self.kube.add({"apiVersion": "helm.cattle.io/v1", "kind": "HelmChart",
                       "metadata": {"namespace": "kube-system", "name": "app", "generation": 5,
                                    "annotations": {"homestead.io/helm-previous": '{"version": "1.0.0"}'}},
                       "spec": {"version": "2.0.0"}})
        uid = self.kube.object(self.PATH)["metadata"]["uid"]
        return self.job("helm", {"action": "upgrade", "name": "helm-install-app", "namespace": "kube-system",
                                 "chart_uid": uid, "chart_generation": 5, **ref}, name="app")

    def given_back(self, op):
        """The job is what it was before the cancel, and may have been polled since, and no
        cancel is left waiting on a person, who would be refused. Returns what it said."""
        item = self.stored(op)
        self.assertIn(item["status"], ("queued", "running"))
        self.assertNotIn("cancel_record", item)
        said = [row.get("m", "") for row in item["history"] if "has changed since" in row.get("m", "")]
        self.assertTrue(said, item["history"])
        return said

    def test_a_rollback_that_landed_says_it_went_back(self):
        op = self.chart()
        self.kube.fail(self.PATH, Died(), method="PUT", applied=True)
        self.cut_off(op)
        self.assertEqual(6, self.kube.object(self.PATH)["metadata"]["generation"])
        self.leader_round()
        self.assertIn("went back", self.finished(op))
        self.assertEqual("1.0.0", self.kube.object(self.PATH)["spec"]["version"])

    def test_a_chart_moved_on_before_the_run_again_is_given_back(self):
        """A person's review would only be refused now, so it does not wait for one: it follows
        its Job again and says why."""
        op = self.chart()
        self.kube.fail(self.PATH, Died(), method="PUT")
        self.cut_off(op)
        chart = self.kube.object(self.PATH)
        chart["metadata"]["generation"] = 7
        self.kube.add(chart)
        self.leader_round()
        self.assertEqual("2.0.0", self.kube.object(self.PATH)["spec"]["version"])
        self.given_back(op)

    def test_a_later_upgrade_after_the_rollback_landed_is_not_undone(self):
        op = self.chart()
        self.kube.fail(self.PATH, Died(), method="PUT", applied=True)
        self.cut_off(op)
        chart = self.kube.object(self.PATH)
        chart["spec"]["version"] = "3.0.0"
        chart["metadata"]["generation"] = 7
        chart["metadata"]["annotations"]["homestead.io/helm-previous"] = '{"version": "1.0.0"}'
        self.kube.add(chart)
        self.leader_round()
        self.assertEqual("3.0.0", self.kube.object(self.PATH)["spec"]["version"])
        self.assertNotIn("nothing was changed", " ".join(self.given_back(op)), "its own write landed")

    def test_someone_else_changing_the_chart_after_it_landed_is_not_called_going_back(self):
        op = self.chart()
        self.kube.fail(self.PATH, Died(), method="PUT", applied=True)
        self.cut_off(op)
        chart = self.kube.object(self.PATH)
        chart["spec"]["version"] = "4.0.0"
        chart["metadata"]["generation"] = 7
        self.kube.add(chart)
        self.leader_round()
        self.assertEqual("4.0.0", self.kube.object(self.PATH)["spec"]["version"])
        self.assertIn("was not run again", self.stored(op)["message"])

    def test_an_upgrade_that_changed_nothing_runs_again_with_no_write(self):
        op = self.chart(no_change=True)
        chart = self.kube.object(self.PATH)     # it wrote nothing, so it kept nothing
        chart["metadata"]["annotations"] = {}
        self.kube.add(chart)
        real = OPS.CANCELLERS["helm"]
        died = []

        def once(item, options):
            if not died:
                died.append(True)
                raise Died()
            return real[1](item, options)
        self.enterContext(mock.patch.dict(OPS.CANCELLERS, {"helm": (real[0], once)}))
        self.cut_off(op)
        self.kube.writes.clear()
        self.leader_round()
        self.assertIn("changed nothing", self.finished(op))
        self.assertEqual([], [row for row in self.kube.writes if row[0] != "GET"])


class AFailedUpgradeCutOff(Base):
    PATH = "/apis/helm.cattle.io/v1/namespaces/kube-system/helmcharts/app"

    def test_a_put_back_on_a_chart_moved_on_stays_with_a_person(self):
        """Only a running upgrade's cancel is given back: this one's job has already failed."""
        self.kube.add({"apiVersion": "helm.cattle.io/v1", "kind": "HelmChart",
                       "metadata": {"namespace": "kube-system", "name": "app", "generation": 1,
                                    "annotations": {"homestead.io/helm-previous": '{"version": "1.0.0"}'}},
                       "spec": {"version": "2.0.0"}})
        chart = self.kube.object(self.PATH)
        op = self.job("helm", {"action": "upgrade", "name": "helm-upgrade-app", "namespace": "kube-system",
                               "chart_uid": chart["metadata"]["uid"], "chart_generation": 1}, name="app")
        items = OPS.stored()
        items[-1].update(status="failed", message="failed")
        with OPS.transaction() as tx:
            tx.save(items)
        self.kube.fail(self.PATH, Died(), method="PUT")
        self.cut_off(op)
        chart = self.kube.object(self.PATH)
        chart["spec"]["version"] = "3.0.0"
        chart["metadata"]["generation"] = 2
        self.kube.add(chart)
        self.leader_round()
        self.assertEqual("3.0.0", self.kube.object(self.PATH)["spec"]["version"])
        self.assertIn("was not run again", self.stored(op)["message"])

    def test_putting_it_back_says_it_went_back(self):
        self.kube.add({"apiVersion": "helm.cattle.io/v1", "kind": "HelmChart",
                       "metadata": {"namespace": "kube-system", "name": "app", "generation": 1,
                                    "annotations": {"homestead.io/helm-previous": '{"version": "1.0.0"}'}},
                       "spec": {"version": "2.0.0"}})
        chart = self.kube.object(self.PATH)
        op = self.job("helm", {"action": "upgrade", "name": "helm-upgrade-app", "namespace": "kube-system",
                               "chart_uid": chart["metadata"]["uid"], "chart_generation": 1}, name="app")
        items = OPS.stored()
        items[-1].update(status="failed", message="failed")
        with OPS.transaction() as tx:
            tx.save(items)
        self.kube.fail(self.PATH, Died(), method="PUT", applied=True)
        self.cut_off(op)
        self.leader_round()
        item = self.stored(op)
        self.assertEqual(("failed", True), (item["status"], item.get("cleaned")), item["message"])
        self.assertEqual("1.0.0", self.kube.object(self.PATH)["spec"]["version"])
        self.assertNotIn("did not finish", item["message"])


class Words(Base):
    def test_a_service_kept_is_not_called_removed(self):
        self.kube.add(service())
        op = self.job("network-service", {"namespace": "lab", "name": "web"})
        self.kube.fail(SVC, Died(), method="DELETE", applied=True)
        self.cut_off(op)
        self.kube.add(service())
        self.leader_round()
        message = self.finished(op)
        self.assertNotIn("removed", message)
        self.assertIn("kept", message)

    def test_a_second_cancel_after_a_failed_first_looks_afresh(self):
        self.kube.add(service())
        op = self.job("network-service", {"namespace": "lab", "name": "web"})
        self.kube.fail(SVC, 500, method="DELETE")
        with self.assertRaises(ValueError):
            OPS.cancel(op, by="alice")
        self.assertNotIn("cancel_uids", self.stored(op)["ref"])
        self.kube.add(service())
        result = OPS.cancel(op, by="alice")
        self.assertIsNone(self.kube.object(SVC), "a person reviewed this one")
        self.assertIn("removed", result["detail"])


class NothingThereAtFirst(Base):
    def test_something_made_where_nothing_was_at_first_is_not_deleted(self):
        volume = "/apis/cdi.kubevirt.io/v1beta1/namespaces/lab/datavolumes/web-data"
        self.kube.add({"apiVersion": "cdi.kubevirt.io/v1beta1", "kind": "DataVolume",
                       "metadata": {"namespace": "lab", "name": "web-data"}, "spec": {}})
        op = self.job("vm-disk-import", {"namespace": "lab", "name": "web-data"})
        self.kube.fail(volume, Died(), method="DELETE", applied=True)
        self.cut_off(op)
        path, uid = self.again(claim())
        self.leader_round()
        self.survives(path, uid)


def lh(kind, name, **fields):
    return {"apiVersion": "longhorn.io/v1beta2", "kind": kind,
            "metadata": {"namespace": "longhorn-system", "name": name}, **fields}


def claim(name="web-data", **notes):
    return {"apiVersion": "v1", "kind": "PersistentVolumeClaim",
            "metadata": {"namespace": "lab", "name": name, "annotations": dict(notes)}, "spec": {}}


class MadeAgainSurvives(Base):
    """Each kind that deletes by name, cut off just after its delete; the name is
    then used again. The new object has a new uid and must be left alone."""
    def test_deployment(self):
        self.kube.add(deployment())
        op = self.job("deployment", {"namespace": "lab", "name": "web", "undo": "delete"})
        self.kube.fail(DEP, Died(), method="DELETE", applied=True)
        self.cut_off(op)
        path, uid = self.again(deployment())
        self.leader_round()
        self.survives(path, uid)

    def test_backup_and_its_snapshot(self):
        backup, snapshot = lh("Backup", "backup-1", spec={"snapshotName": "snap-1"}), lh("Snapshot", "snap-1", spec={})
        self.kube.add(backup)
        self.kube.add(snapshot)
        op = self.job("backup", {"name": "backup-1"})
        self.kube.fail(Backup.PATH, Died(), method="DELETE", applied=True)
        self.cut_off(op)
        made = [self.again(backup), self.again(snapshot)]
        self.leader_round()
        for path, uid in made:
            self.survives(path, uid)

    def test_protect_run(self):
        job = {"apiVersion": "batch/v1", "kind": "Job", "metadata": {"namespace": "lab", "name": "run-1"}, "spec": {}}
        path = self.kube.add(job)
        op = self.job("protect-run", {"namespace": "lab", "name": "run-1"})
        self.kube.fail(path, Died(), method="DELETE", applied=True)
        self.cut_off(op)
        path, uid = self.again(job)
        self.leader_round()
        self.survives(path, uid)

    def test_a_job_is_still_deleted_in_the_background_with_its_uid_named(self):
        """The API server reads a delete's options from its body when it has one: a
        propagation policy left in the query would be ignored, and a Job's pods orphaned."""
        path = self.kube.add({"apiVersion": "batch/v1", "kind": "Job", "metadata": {"namespace": "lab", "name": "run-1"}, "spec": {}})
        uid = self.kube.object(path)["metadata"]["uid"]
        OPS.cancel(self.job("protect-run", {"namespace": "lab", "name": "run-1"}))
        sent = [row for row in self.kube.writes if row[0] == "DELETE"]
        self.assertEqual(path, sent[0][1])
        self.assertEqual(("Background", {"uid": uid}), (sent[0][2]["propagationPolicy"], sent[0][2]["preconditions"]))

    def test_vm_migration(self):
        made = {"apiVersion": "kubevirt.io/v1", "kind": "VirtualMachineInstanceMigration",
                "metadata": {"namespace": "lab", "name": "mig-1"}, "spec": {}}
        path = self.kube.add(made)
        op = self.job("vm-migration", {"namespace": "lab", "name": "mig-1"})
        self.kube.fail(path, Died(), method="DELETE", applied=True)
        self.cut_off(op)
        path, uid = self.again(made)
        self.leader_round()
        self.survives(path, uid)

    def test_image_cleanup(self):
        pod = {"apiVersion": "v1", "kind": "Pod", "metadata": {"namespace": "lab", "name": "clean-1"},
               "status": {"phase": "Running"}}
        path = self.kube.add(pod)
        op = self.job("image-cleanup", {"namespace": "lab", "pods": ["clean-1"]})
        self.kube.fail(path, Died(), method="DELETE", applied=True)
        self.cut_off(op)
        path, uid = self.again(pod)
        self.leader_round()
        self.survives(path, uid)

    def test_image_pull(self):
        pull = {"apiVersion": "apps/v1", "kind": "DaemonSet",
                "metadata": {"namespace": IMAGES.I.NS, "name": "homestead-pull-web"}, "spec": {}}
        path = self.kube.add(pull)
        op = self.job("image-pull", {"name": "homestead-pull-web"})
        self.kube.fail(path, Died(), method="DELETE", applied=True)
        self.cut_off(op)
        path, uid = self.again(pull)
        self.leader_round()
        self.survives(path, uid)

    def test_helm_install(self):
        chart = {"apiVersion": "helm.cattle.io/v1", "kind": "HelmChart",
                 "metadata": {"namespace": "kube-system", "name": "app"}, "spec": {"version": "1.0.0"}}
        path = self.kube.add(chart)
        op = self.job("helm", {"action": "install", "name": "helm-install-app"}, name="app")
        self.kube.fail(path, Died(), method="DELETE", applied=True)
        self.cut_off(op)
        path, uid = self.again(chart)
        self.leader_round()
        self.survives(path, uid)
        self.assertIn("kept", self.stored(op)["message"])

    def test_volume_restore(self):
        note = {"homestead.io/restored-from-backup": "backup-1"}
        self.kube.add(claim(**note))
        op = self.job("volume-restore", {"namespace": "lab", "name": "web-data", "backup": "backup-1"})
        self.kube.fail(CLAIM, Died(), method="DELETE", applied=True)
        self.cut_off(op)
        path, uid = self.again(claim(**note))
        self.leader_round()
        self.survives(path, uid)

    def test_a_new_import_of_that_name(self):
        op = OlderImport.started(self)
        self.kube.fail(OlderImport.JOB, Died(), method="DELETE", applied=True)
        self.cut_off(op, {"volumes": True})
        path, uid = self.again({"apiVersion": "batch/v1", "kind": "Job",
                                "metadata": {"namespace": "lab", "name": "homestead-import-web", "labels": {"app": "web"}}, "spec": {}})
        self.leader_round()
        self.survives(path, uid)

    def test_container_rebalance_pulls(self):
        made = {"apiVersion": "apps/v1", "kind": "DaemonSet",
                "metadata": {"namespace": IMAGES.I.NS, "name": "homestead-pull-web"}, "spec": {}}
        path = self.kube.add(made)
        op = self.job("container-rebalance", {"pulls": ["homestead-pull-web"], "moves": []})
        self.kube.fail(path, Died(), method="DELETE", applied=True)
        self.cut_off(op)
        path, uid = self.again(made)
        self.leader_round()
        self.survives(path, uid)

    def test_import_workload_and_volume(self):
        older = OlderImport.started
        op = older(self)
        self.kube.fail(OlderImport.JOB, Died(), method="DELETE", applied=True)
        self.cut_off(op, {"volumes": True})
        self.kube.discard(DEP)
        self.kube.discard(CLAIM)
        made = [self.again(deployment()), self.again(claim())]
        self.leader_round()
        for path, uid in made:
            self.survives(path, uid)

    def test_restore_test(self):
        op = self.job("restore-test", {"namespace": "lab", "rows": [{"restored": "web-data"}]})
        name = f"restore-test-{op[:10]}"
        copy_of = deployment()
        copy_of["metadata"]["name"] = name
        path = self.kube.add(copy_of)
        self.kube.add(claim())
        self.kube.fail(path, Died(), method="DELETE", applied=True)
        self.cut_off(op)
        made = [self.again(copy_of)]
        self.kube.discard(CLAIM)
        made.append(self.again(claim()))
        self.leader_round()
        for path, uid in made:
            self.survives(path, uid)

    def test_unraid_vm_import(self):
        volume = {"apiVersion": "cdi.kubevirt.io/v1beta1", "kind": "DataVolume",
                  "metadata": {"namespace": "lab", "name": "disk-1", "annotations": {CDI_CLEANUP.STAMP: "stamp-1"}}, "spec": {}}
        path = self.kube.add(volume)
        op = self.job("unraid-vm-import", {"namespace": "lab", "cdi_cleanup": {"id": "stamp-1"}, "disks": [{"dv": "disk-1"}],
                                           "source": {"kind": "unraid", "name": "source-a"}})
        self.kube.fail(path, Died(), method="DELETE", applied=True)
        self.cut_off(op)
        volume["metadata"]["annotations"] = {}
        path, uid = self.again(volume)
        self.leader_round()
        self.survives(path, uid)

    def test_import_create(self):
        job = {"apiVersion": "batch/v1", "kind": "Job", "metadata": {"namespace": "lab", "name": "copy-1"}, "spec": {}, "status": {}}
        path = self.kube.add(job)
        first = self.kube.object(path)["metadata"]["uid"]
        op = self.job("import-create", {"namespace": "lab", "name": "web", "phase": "copying", "job": "copy-1", "job_uid": first,
                                             "review_digest": "a" * 64})
        self.kube.fail(path, Died(), method="DELETE", applied=True)
        self.cut_off(op)
        path, uid = self.again(job)
        self.leader_round()
        self.survives(path, uid)

    def test_workload_copy(self):
        held = deployment(**{COPY_JOB.MARKER: "placeholder"})
        path = self.kube.add(held)
        first = self.kube.object(path)["metadata"]["uid"]
        op = self.job("workload-copy", {"namespace": "lab", "name": "web", "deployment": {"uid": first},
                                            "review_digest": "b" * 64})
        marked = self.kube.object(path)
        marked["metadata"]["annotations"][COPY_JOB.MARKER] = op
        self.kube.add(marked)
        self.kube.fail(path, Died(), method="PATCH", applied=True)
        self.cut_off_in_the_loop(op, {"ack": True}, confirm="web")
        replaced = deployment(**{COPY_JOB.MARKER: op})
        path, uid = self.again(replaced)
        self.leader_round()
        # Its plan refuses now, so the job goes back to what it was, with the reason.
        self.survives(path, uid, looked_again=False)
        self.assertTrue(any("replaced" in row.get("m", "") for row in self.stored(op)["history"]))
        self.assertIn(COPY_JOB.MARKER, self.kube.object(path)["metadata"]["annotations"])

    def test_snapshot_revert(self):
        """The new app is otherwise one it would start: stopped, on the same claim, with
        room. Only its uid says it is not what this job stopped."""
        self.enterContext(mock.patch.object(REVERT, "_admission", return_value={"blocked": False}))
        self.kube.add({**claim(), "spec": {"volumeName": "pvc-1"}})
        stopped = deployment()
        stopped["spec"]["replicas"] = 0
        stopped["spec"]["template"]["spec"]["volumes"] = [{"name": "data", "persistentVolumeClaim": {"claimName": "web-data"}}]
        path = self.kube.add(stopped)
        first = self.kube.object(path)["metadata"]["uid"]
        op = self.job("snapshot-revert", {"namespace": "lab", "volume": "pvc-1", "claim": "web-data", "attached": False,
                                          "consumers": [{"kind": "Deployment", "name": "web", "uid": first, "stopped": True, "replicas": 2}]})
        self.kube.fail(path, Died(), method="PATCH", applied=True)
        self.cut_off(op)
        self.assertEqual(2, self.kube.object(path)["spec"]["replicas"], "the first run started what it stopped")
        path, uid = self.again(stopped)
        self.leader_round()
        self.survives(path, uid)
        self.assertEqual(0, self.kube.object(path)["spec"]["replicas"])
        self.assertIn("replaced", self.stored(op)["message"])


class LeaseSomeoneElseHolds(Base):
    """Host jobs delete nothing: they give back a lease, only if it is still theirs."""
    def check(self, kind):
        path = HOSTRUN._lease_path("node-a")
        lease = {"apiVersion": "coordination.k8s.io/v1", "kind": "Lease",
                 "metadata": {"namespace": "lab", "name": path.rsplit("/", 1)[-1]},
                 "spec": {"holderIdentity": "this-job"}}
        self.kube.add(lease)
        op = self.job(kind, {"node": "node-a", "lease": "this-job"})
        self.kube.fail(path, Died(), method="PUT", applied=True)
        self.cut_off_in_the_loop(op)
        lease["spec"]["holderIdentity"] = "another-job"
        path, uid = self.again(lease)
        self.leader_round()
        self.survives(path, uid)
        self.assertEqual("another-job", self.kube.object(path)["spec"]["holderIdentity"])

    def test_host_os(self):
        self.check("host-os")

    def test_host_bond(self):
        self.check("host-bond")

    def test_host_bridge(self):
        self.check("host-bridge")


# What each kind's real canceller is given when it writes nothing to the cluster.
NO_WRITE = {
    "k3s-cluster": ({"name": "lab", "phase": "booting"}, {}),
    "restructure": ({"name": "web"}, {"ack": True}),
    "set-priority": ({}, {}),
    "set-requests": ({}, {}),
    "vm-power": ({"phase": "accepted", "name": "vm-1"}, {}),
    "volume-delete": ({"name": "web-data"}, {}),
    "workload-rename": ({"namespace": "lab", "name": "web", "new_name": "web2"}, {"ack": True}),
    "image-update": ({"namespace": "lab", "name": "web"}, {}),
    "image-rollback": ({"namespace": "lab", "name": "web"}, {}),
}
NEVER_MARKED = ("node-power", "self-data-move", "self-data-prepare", "vm-create", "vm-edit")


class NothingToTake(Base):
    def test_a_kind_that_writes_nothing_writes_nothing_however_often_it_runs(self):
        for kind, (ref, options) in NO_WRITE.items():
            with self.subTest(kind):
                self.kube.writes.clear()
                item = {"id": "0123456789abcdef01234567", "kind": kind, "ref": dict(ref), "resource": {"name": "web"}}
                run = OPS.CANCELLERS[kind][1]
                run(item, options)
                run({**item, "ref": dict(ref)}, options)
                self.assertEqual([], [row for row in self.kube.writes if row[0] != "GET"])

    def test_a_kind_whose_plan_always_refuses_is_never_marked_so_never_runs_again(self):
        for number, kind in enumerate(NEVER_MARKED):
            with self.subTest(kind):
                op = self.job(kind, {"phase": "x", "name": f"web-{number}", "namespace": "lab", "node": "node-a",
                                     "new_name": f"new-{number}", "review_digest": f"{number:064x}"})
                with self.assertRaises(ValueError):
                    OPS.cancel(op)
                self.assertNotEqual(OPS.CANCELLING, self.stored(op)["status"])
                self.assertNotIn(OPS.CANCELLING, [row.get("s") for row in self.stored(op).get("history") or []])


# Every safe kind and the test that proves it safe against a name used again.
PROOF = {
    "backup": "test_backup_and_its_snapshot",
    "container-rebalance": "test_container_rebalance_pulls",
    "deployment": "test_deployment",
    "disk-v2-convert": "test_a_replaced_host_is_not_touched_when_the_cancel_runs_again",
    "helm": "test_helm_install",
    "host-bond": "test_host_bond",
    "host-bridge": "test_host_bridge",
    "host-os": "test_host_os",
    "image-cleanup": "test_image_cleanup",
    "image-pull": "test_image_pull",
    "import": "test_import_workload_and_volume",
    "import-create": "test_import_create",
    "move": "test_abandoning_again_leaves_a_new_object_of_that_name",
    "network-service": "test_a_service_made_again_since_is_not_deleted",
    "platform-upgrade": "test_a_replaced_node_keeps_its_cordon_when_the_cancel_runs_again",
    "protect-run": "test_protect_run",
    "restore-test": "test_restore_test",
    "snapshot-revert": "test_snapshot_revert",
    "unraid-vm-import": "test_unraid_vm_import",
    "vm-disk-import": "test_a_disk_made_again_since_is_not_deleted",
    "vm-migration": "test_vm_migration",
    "volume-restore": "test_volume_restore",
    "workload-copy": "test_workload_copy",
    **{kind: "test_a_kind_that_writes_nothing_writes_nothing_however_often_it_runs" for kind in NO_WRITE},
    **{kind: "test_a_kind_whose_plan_always_refuses_is_never_marked_so_never_runs_again" for kind in NEVER_MARKED},
}


class EverySafeKindHasItsProof(unittest.TestCase):
    def test_each_safe_kind_names_a_test_that_exists(self):
        self.assertEqual(set(rerun().SAFE), set(PROOF))
        tests = Path(__file__).parent
        text = "".join((tests / name).read_text() for name in (
            "test_cancel_rerun_kinds.py", "test_disk_v2.py", "test_components.py", "test_cluster_copy.py"))
        for kind, name in PROOF.items():
            self.assertIn(f"def {name}(", text, kind)

    def test_the_kinds_that_patch_unfenced_stay_with_a_person(self):
        for kind in ("reclass", "volume-rebalance", "volume-copy-move", "disk-retire", "restore-swap", "host-promote", "smart-test"):
            self.assertIn(kind, rerun().PERSON)


if __name__ == "__main__":
    unittest.main()
