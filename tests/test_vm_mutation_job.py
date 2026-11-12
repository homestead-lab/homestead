import copy
import hashlib
import json
import multiprocessing
import tempfile
import time
import unittest
import urllib.error
from unittest import mock

import test_vm_resources  # server module path
import homestead_operations as ops
from fakes.job_store import put, store_writes
import homestead_vm_mutation_job as job
import homestead_vm_mutation_recovery as recovery
import homestead_vm_power_job as power
import server
import test_vm_power_recovery as power_recovery_tests
import homestead_imports as IMPORTS
import homestead_kube as KUBE
import homestead_route_policy as POLICY
import homestead_vm_create as VM_CREATE
from fakes.kube import Kube

PATH = "/apis/kubevirt.io/v1/namespaces/lab/virtualmachines/guest"
VM = {"apiVersion": "kubevirt.io/v1", "kind": "VirtualMachine",
      "metadata": {"namespace": "lab", "name": "guest", "uid": "vm-uid", "resourceVersion": "v1"},
      "spec": {"private": "test-secret-value"}}


def hold_worker(directory, ident, ready, finish):
    ops.bind(None, directory, None)
    with power.worker_lock(ident, ops):
        ready.set()
        finish.wait(10)


class VMMutationJobTests(unittest.TestCase):
    api = power_recovery_tests.VMPowerRecoveryTests.api
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        for patch in (mock.patch.object(ops, "DATA_DIR", temporary.name),
                      mock.patch.object(recovery.REVIEW, "_key", return_value=b"mutation-tests")):
            patch.start()
            self.addCleanup(patch.stop)
        self.vm = copy.deepcopy(VM)
        self.body = {"capacity_token": f"{int(time.time()) + 600}.mutation-test"}
        self.send = mock.Mock(side_effect=lambda *a, **kw: copy.deepcopy(self.vm))

    def dispatch(self, commit=None, kind="vm-edit", body=None):
        def save(writer):
            writer("PUT", PATH, VM)
            return {"ok": True, "vm_identity": copy.deepcopy(VM["metadata"])}
        return job.dispatch(kind, body or self.body, "lab", "guest", VM["metadata"], ops, self.send, commit or save)

    def failed(self):
        self.send.side_effect = TimeoutError("test-secret-value")
        with self.assertRaisesRegex(ValueError, "Check job") as error:
            self.dispatch()
        self.assertNotIn("test-secret-value", str(error.exception))
        return ops.stored()[0]["id"]

    def read(self, path):
        self.assertEqual(PATH, path)
        return copy.deepcopy(self.vm)

    def approved(self, ident):
        result = recovery.preview(ident, ops, self.read, "admin")
        return {"id": ident, "capacity_token": result["capacity_token"], "confirm_capacity": True,
                "confirm": "guest", "acknowledge_unknown": True}

    def test_success_means_acknowledged_config_not_guest_health_and_cannot_replay(self):
        result = self.dispatch()
        self.assertEqual("succeeded", result["operation"]["status"])
        self.assertIn("healthy is not checked", result["operation"]["message"])
        item = ops.stored()[0]
        self.assertEqual("accepted", item["ref"]["writes"][0]["phase"])
        self.assertFalse(item["ref"]["retain_resources"])
        self.assertNotIn("test-secret-value", str(item))
        self.assertNotIn("writes", result["operation"])
        with self.assertRaisesRegex(ValueError, "already has job"):
            self.dispatch()
        self.assertEqual(1, self.send.call_count)

    def test_clock_rollback_and_clear_history_do_not_resurrect_create_or_edit_approval(self):
        for kind in ("vm-create", "vm-edit"):
            body = {"capacity_token": f"{int(time.time()) - 1}.{kind}"}
            self.dispatch(kind=kind, body=body)
            ops.dismiss_finished()
            with mock.patch.object(time, "time", return_value=1), self.assertRaisesRegex(ValueError, "already has job"):
                self.dispatch(kind=kind, body=body)
        self.assertEqual([], ops.stored())
        self.assertEqual(2, self.send.call_count)

    def test_failure_retains_receipts_and_blocks_new_create_edit_and_power(self):
        ident = self.failed()
        item = ops.stored()[0]
        self.assertEqual("uncertain", item["ref"]["writes"][0]["phase"])
        self.assertTrue(ops._public(item)["mutation_recovery"])
        self.assertFalse(ops._public(item)["cancellable"])
        self.assertEqual(0, ops.dismiss_finished()["dismissed"])
        for kind in ("vm-create", "vm-edit", "vm-power"):
            with self.assertRaisesRegex(ValueError, "needs recovery"):
                ops.start(kind, "new", {}, "/vms", {"namespace": "lab", "name": "guest", "review_digest": hashlib.sha256(kind.encode()).hexdigest()})
        self.assertEqual(ident, ops.stored()[0]["id"])

    def test_partial_failure_and_swallowed_warning_never_become_success(self):
        def commit(writer):
            writer("PUT", PATH, VM)
            self.send.side_effect = urllib.error.HTTPError(PATH, 422, "test-secret-value", {}, None)
            try:
                writer("PUT", PATH, VM)
            except ValueError:
                pass
            return {"ok": True, "warning": "ignored write failure"}
        with self.assertRaises(ValueError):
            self.dispatch(commit)
        item = ops.stored()[0]
        self.assertEqual("failed", item["status"])
        self.assertEqual(["accepted", "refused"], [row["phase"] for row in item["ref"]["writes"]])
        self.assertTrue(item["ref"]["retain_resources"])

    def test_prewrite_admission_failure_sends_nothing_but_consumes_approval(self):
        def commit(writer):
            raise ValueError("fresh admission changed")
        with self.assertRaisesRegex(ValueError, "fresh admission"):
            self.dispatch(commit)
        self.send.assert_not_called()
        self.assertFalse(ops.stored()[0]["ref"]["retain_resources"])
        with self.assertRaisesRegex(ValueError, "already has job"):
            self.dispatch()

    def test_history_write_failure_never_sends(self):
        with store_writes(ops, fail=OSError("disk")), self.assertRaises(OSError):
            self.dispatch()
        self.send.assert_not_called()

    def test_unverified_completion_retains_receipts_instead_of_claiming_success(self):
        def commit(writer):
            writer("PUT", PATH, VM)
            return {"ok": True, "vm_identity": {**VM["metadata"], "uid": "wrong-vm"}}
        with self.assertRaises(ValueError):
            self.dispatch(commit)
        self.assertTrue(ops.stored()[0]["ref"]["retain_resources"])
        self.assertEqual("failed", ops.stored()[0]["status"])

    def test_resolution_is_read_only_audited_unknown_and_consumes_old_token(self):
        ident = self.failed()
        good = self.approved(ident)
        result = recovery.resolve(good, ops, self.read, "admin")
        self.assertEqual(1, self.send.call_count)
        self.assertEqual("failed", result["operation"]["status"])
        self.assertFalse(result["operation"]["mutation_recovery"])
        ref = ops.stored()[0]["ref"]
        self.assertEqual(("admin", "unknown"), (ref["recovery"]["by"], ref["recovery"]["outcome"]))
        self.assertFalse(ref["retain_resources"])
        self.assertNotIn("test-secret-value", str(ref))
        with self.assertRaisesRegex(ValueError, "already has job"):
            self.dispatch()
        with self.assertRaises(ValueError):
            recovery.resolve(good, ops, self.read, "admin")

    def test_fresh_identity_actor_and_explicit_acknowledgement_required(self):
        ident = self.failed()
        good = self.approved(ident)
        for changed in ({"confirm": "wrong"}, {"acknowledge_unknown": False}, {"capacity_token": ""}):
            with self.assertRaises(ValueError):
                recovery.resolve({**good, **changed}, ops, self.read, "admin")
        with self.assertRaises(recovery.REVIEW.Rejected):
            recovery.resolve(good, ops, self.read, "other-admin")
        self.vm["metadata"]["resourceVersion"] = "v2"
        with self.assertRaises(recovery.REVIEW.Rejected):
            recovery.resolve(good, ops, self.read, "admin")
        self.assertTrue(ops.stored()[0]["ref"]["retain_resources"])

    def test_missing_replacement_and_deleting_resources_are_not_adopted(self):
        ident = self.failed()
        self.vm["metadata"]["uid"] = "replacement"
        plan = recovery.preview(ident, ops, self.read, "admin")["plan"]
        self.assertEqual("replacement; not adopted", plan["resources"][0]["relationship"])
        self.vm["metadata"]["deletionTimestamp"] = "now"
        self.assertTrue(recovery.preview(ident, ops, self.read, "admin")["plan"]["blocked"])
        missing = mock.Mock(side_effect=urllib.error.HTTPError(PATH, 404, "not found", {}, None))
        plan = recovery.preview(ident, ops, missing, "admin")["plan"]
        self.assertEqual("not found", plan["resources"][0]["relationship"])
        self.assertIsNone(plan["resources"][0]["current"])

    def test_unavailable_inventory_or_store_is_never_successful_recovery(self):
        ident = self.failed()
        for value in ({}, [], TimeoutError("test-secret-value"), urllib.error.HTTPError(PATH, 403, "test-secret-value", {}, None)):
            read = mock.Mock(side_effect=value) if isinstance(value, Exception) else mock.Mock(return_value=value)
            with self.assertRaises(ValueError) as error:
                recovery.preview(ident, ops, read, "admin")
            self.assertNotIn("test-secret-value", str(error.exception))
        good = self.approved(ident)
        with store_writes(ops, fail=OSError("disk")), self.assertRaises(OSError):
            recovery.resolve(good, ops, self.read, "admin")
        self.assertTrue(ops.stored()[0]["ref"]["retain_resources"])

    def test_active_dispatcher_in_another_process_blocks_inspection_and_resolution(self):
        ident = self.failed()
        good = self.approved(ident)
        ctx = multiprocessing.get_context("spawn")
        ready, finish = ctx.Event(), ctx.Event()
        child = ctx.Process(target=hold_worker, args=(ops.DATA_DIR, ident, ready, finish))
        child.start()
        try:
            self.assertTrue(ready.wait(10))
            result = recovery.preview(ident, ops, self.read, "admin")
            self.assertTrue(result["plan"]["blocked"])
            self.assertIsNone(result["capacity_token"])
            with self.assertRaisesRegex(ValueError, "dispatcher is running"):
                recovery.resolve(good, ops, self.read, "admin")
        finally:
            finish.set()
            child.join(10)
            if child.is_alive():
                child.terminate()
                child.join(5)

    def test_orphan_prepared_job_can_be_resolved_without_replaying_and_is_fenced(self):
        ident = self.failed()
        items = ops.stored()
        items[0].update(status="queued")
        items[0]["ref"].update(phase="prepared", writes=[])
        put(ops, items)
        recovery.resolve(self.approved(ident), ops, self.read, "admin")
        with self.assertRaisesRegex(ValueError, "job has ended"):
            ops.record_phase(ident, "writing", 0, "late worker")

    def test_a_create_stamps_its_vm_with_the_job_id_and_an_edit_does_not(self):
        vms = "/apis/kubevirt.io/v1/namespaces/lab/virtualmachines"
        claim = {"apiVersion": "v1", "kind": "PersistentVolumeClaim", "metadata": {"name": "guest-disk", "namespace": "lab"}}
        sent = {}

        def answer(method, path, body=None, **kwargs):
            sent[path] = copy.deepcopy(body)
            return {**copy.deepcopy(body), "metadata": {**body["metadata"], "uid": "u-" + body["metadata"]["name"],
                                                        "resourceVersion": "1"}}
        self.send.side_effect = answer
        vm = {"apiVersion": "kubevirt.io/v1", "kind": "VirtualMachine",
              "metadata": {"name": "guest", "namespace": "lab", "labels": {"app": "guest"}}}

        def commit(writer):
            writer("POST", "/api/v1/namespaces/lab/persistentvolumeclaims", claim)
            made = writer("POST", vms, vm)
            return {"ok": True, "vm_identity": {key: made["metadata"][key] for key in ("namespace", "name", "uid", "resourceVersion")}}
        self.dispatch(commit, kind="vm-create")
        ident = ops.stored()[0]["id"]
        self.assertEqual({"app": "guest", job.CREATE_JOB_LABEL: ident}, sent[vms]["metadata"]["labels"])
        self.assertNotIn("labels", sent["/api/v1/namespaces/lab/persistentvolumeclaims"]["metadata"], "only the VM is stamped")
        self.assertEqual({"app": "guest"}, vm["metadata"]["labels"], "the caller's manifest is left as it was")
        self.send.side_effect = lambda method, path, body=None, **kw: sent.update({path: copy.deepcopy(body)}) or copy.deepcopy(self.vm)
        self.dispatch(kind="vm-edit", body={"capacity_token": f"{int(time.time()) + 600}.edit"})
        self.assertNotIn(job.CREATE_JOB_LABEL, str(sent[PATH]))

    def test_a_vm_is_this_attempts_by_its_receipt_uid_or_when_the_answer_was_lost_by_the_stamp(self):
        vm = {"resource": {"kind": "VirtualMachine"}}
        stamped = {"metadata": {"uid": "a", "labels": {job.CREATE_JOB_LABEL: "job-1"}}}
        cases = [
            ({**vm, "identity": {"uid": "a"}}, {"metadata": {"uid": "a"}}, True),
            # A known uid decides: another uid is another VM, whatever its stamp says.
            ({**vm, "identity": {"uid": "b"}}, stamped, False),
            ({**vm, "phase": "uncertain"}, stamped, True),
            ({**vm, "phase": "uncertain"}, {"metadata": {"uid": "a", "labels": {job.CREATE_JOB_LABEL: "job-2"}}}, False),
            ({**vm, "phase": "uncertain"}, {"metadata": {"uid": "a"}}, False),
            # Only a VM carries the stamp: a claim with no receipt is unproven.
            ({"resource": {"kind": "PersistentVolumeClaim"}, "phase": "uncertain"}, stamped, False),
        ]
        for entry, found, expected in cases:
            with self.subTest(entry=entry, found=found):
                self.assertIs(expected, job.made_by_attempt(found, entry, "job-1"))

    def test_recovery_routes_are_admin_only_and_monitor_does_not_write(self):
        for suffix in ("preview", "resolve"):
            self.assertIn("/api/operations/vm-recovery/" + suffix, server.ADMIN_ROUTES)
        item = {"progress": 20, "ref": {"phase": "writing"}}
        self.assertEqual("running", job.status(item)[0])
        self.assertFalse(job.cancel_plan(item)["can"])
        self.send.assert_not_called()

    def test_actual_recovery_routes_enforce_roles_and_never_mutate_cluster(self):
        ident = self.failed()
        for role in ("operator", "viewer"):
            for suffix in ("preview", "resolve"):
                self.assertEqual(403, self.api("/api/operations/vm-recovery/" + suffix, {"id": ident}, role)[0])
        preview = self.api("/api/operations/vm-recovery/preview", {"id": ident})
        self.assertEqual(200, preview[0], preview)
        result = self.api("/api/operations/vm-recovery/resolve", {
            "id": ident, "capacity_token": preview[1]["capacity_token"], "confirm_capacity": True,
            "confirm": "guest", "acknowledge_unknown": True})
        self.assertEqual(200, result[0], result)
        self.assertFalse(result[1]["operation"]["mutation_recovery"])


VMS = "/apis/kubevirt.io/v1/namespaces/lab/virtualmachines"
WEB = f"{VMS}/web"
CLAIM = "/api/v1/namespaces/lab/persistentvolumeclaims/web-disk"
ROM = "/api/v1/namespaces/lab/configmaps/web-vbios"
SECRET = "/api/v1/namespaces/lab/secrets/web-cloudinit"


class UncertainCreateRemovalTests(unittest.TestCase):
    """A Create VM whose VirtualMachine POST got no definite answer keeps
    what it made; an admin's reviewed removal takes away only what this
    attempt provably made, each delete fenced on what the review showed."""

    def setUp(self):
        saved = (IMPORTS.kget, IMPORTS.ksend, IMPORTS.NS, IMPORTS._cache)
        self.addCleanup(lambda: setattr(IMPORTS, "kget", saved[0]) or setattr(IMPORTS, "ksend", saved[1])
                        or setattr(IMPORTS, "NS", saved[2]) or setattr(IMPORTS, "_cache", saved[3]))
        IMPORTS.kget, IMPORTS.ksend, IMPORTS.NS, IMPORTS._cache = KUBE.kget, KUBE.ksend, "lab", {}
        self.enterContext(mock.patch.object(ops, "DATA_DIR", self.enterContext(tempfile.TemporaryDirectory())))
        self.enterContext(mock.patch.object(recovery.REVIEW, "_key", return_value=b"removal-tests"))
        self.kube = self.enterContext(Kube())
        self.paths = [WEB, ROM, CLAIM, SECRET]

    def prepared(self):
        prepared = VM_CREATE.prepare_vm({"name": "web", "password": "a-long-password"}, {"harvester": False, "cdi": False}, "")
        prepared["effects"].append({"kind": "configmap", "path": ROM, "body": {
            "apiVersion": "v1", "kind": "ConfigMap", "metadata": {"name": "web-vbios", "namespace": "lab"},
            "binaryData": {"rom": "AA=="}}})
        return prepared

    def create(self):
        self.reviews = getattr(self, "reviews", 0) + 1
        body = {"capacity_token": f"{int(time.time()) + 600}.removal-test-{self.reviews}"}
        prepared = self.prepared()
        return job.dispatch("vm-create", body, "lab", "web", None, ops, KUBE.ksend,
                            lambda send: VM_CREATE.commit_vm(prepared, send=send))

    def uncertain(self, applied):
        """The VM's POST times out; applied: the VM was made and its reply lost."""
        self.kube.fail(VMS, TimeoutError("timed out"), method="POST", applied=applied)
        with self.assertRaisesRegex(ValueError, "uncertain"):
            self.create()
        self.kube.writes.clear()
        return ops.stored()[-1]["id"]

    def left(self):
        return [path for path in self.paths if self.kube.object(path) is not None]

    def review(self, ident):
        return recovery.removal_preview(ident, ops, KUBE.kget, "admin")

    def remove(self, ident, review, actor="admin", **changes):
        return recovery.remove({"id": ident, "capacity_token": review["capacity_token"], "confirm": "web", **changes},
                               ops, KUBE.kget, actor, send=KUBE.ksend)

    def deletes(self):
        return [path for method, path, *_ in self.kube.writes if method == "DELETE"]

    def replace(self, path, **labels):
        """Somebody else's object of the same name in place of the one there."""
        again = self.kube.object(path)
        self.kube.discard(path)
        again["metadata"].pop("uid")
        again["metadata"]["labels"] = {**(again["metadata"].get("labels") or {}), **labels}
        self.kube.add(again)
        return self.kube.object(path)["metadata"]["uid"]

    def job(self, ident):
        return next(row for row in ops.stored() if row["id"] == ident)

    def test_the_vm_a_lost_reply_made_is_removed_with_what_it_needs_and_the_name_is_free(self):
        ident = self.uncertain(applied=True)
        self.assertEqual(ident, self.kube.object(WEB)["metadata"]["labels"][job.CREATE_JOB_LABEL])
        review = self.review(ident)
        plan = review["plan"]
        self.assertFalse(plan["blocked"], plan)
        self.assertEqual("web", plan["confirm"])
        self.assertEqual(["VirtualMachine", "ConfigMap", "PersistentVolumeClaim", "Secret"],
                         [row["resource"]["kind"] for row in plan["resources"]], "the VM goes first")
        self.assertTrue(all(row["remove"] and row["relationship"] == "Created by this attempt" for row in plan["resources"]))
        self.assertIn("The VM web was created by this attempt", plan["warnings"][0])
        self.assertTrue(any("templates" in warning for warning in plan["warnings"]))
        result = self.remove(ident, review)
        self.assertEqual([], self.left())
        self.assertEqual(self.paths, self.deletes())
        item = self.job(ident)
        self.assertFalse(item["ref"]["retain_resources"], item["message"])
        self.assertEqual("admin removed what this attempt created. Nothing else was changed.", result["detail"])
        self.assertFalse(result["operation"]["mutation_recovery"])
        self.assertEqual("succeeded", self.create()["operation"]["status"], "the name can be used again")

    def test_with_no_vm_made_its_volume_secret_and_rom_go(self):
        ident = self.uncertain(applied=False)
        plan = self.review(ident)["plan"]
        self.assertEqual(("VirtualMachine", "Not found", False),
                         (plan["resources"][0]["resource"]["kind"], plan["resources"][0]["relationship"], plan["resources"][0]["remove"]))
        self.remove(ident, self.review(ident))
        self.assertEqual([], self.left())
        self.assertFalse(self.job(ident)["ref"]["retain_resources"])

    def test_a_vm_of_that_name_without_this_jobs_id_is_left_and_so_is_what_it_may_use(self):
        ident = self.uncertain(applied=False)
        self.kube.add({"apiVersion": "kubevirt.io/v1", "kind": "VirtualMachine", "metadata": {"name": "web", "namespace": "lab"}})
        review = self.review(ident)
        plan = review["plan"]
        self.assertTrue(plan["blocked"])
        self.assertIsNone(review["capacity_token"])
        self.assertIn("nothing on it shows that this attempt created it. It is left as it is", plan["blockers"][0])
        self.assertFalse(any(row["remove"] for row in plan["resources"]))
        self.assertEqual("Not shown to be this attempt's", plan["resources"][0]["relationship"])
        with self.assertRaisesRegex(ValueError, "Nothing was removed"):
            self.remove(ident, {"capacity_token": f"{int(time.time()) + 600}.forged"})
        self.assertEqual(self.paths, self.left())
        self.assertEqual([], self.deletes())
        self.assertTrue(self.job(ident)["ref"]["retain_resources"])

    def test_each_object_replaced_after_the_review_survives_its_fenced_delete(self):
        for path in self.paths:
            with self.subTest(path=path):
                for leftover in self.paths:
                    self.kube.discard(leftover)
                ident = self.uncertain(applied=True)
                review = self.review(ident)
                swapped = {}

                def swap(method, target, body, path=path):
                    # Between the token's check and this delete, the object
                    # was replaced by somebody else's of the same name (with
                    # this job's id on it, even: the uid decides).
                    if "uid" not in swapped:
                        swapped["uid"] = self.replace(path, **{job.CREATE_JOB_LABEL: ident})
                    return None
                self.kube.react("DELETE", path, swap)
                self.remove(ident, review)
                self.kube._reactions.clear()
                self.assertEqual(swapped["uid"], self.kube.object(path)["metadata"]["uid"], "the new one survives")
                item = self.job(ident)
                self.assertTrue(item["ref"]["retain_resources"])
                self.assertIn(path.rsplit("/", 1)[-1], item["message"])
                if path == WEB:
                    # The VM stays, so what it may use stays with it, unsent.
                    self.assertEqual([WEB], self.deletes())
                    self.assertEqual(self.paths, self.left())
                else:
                    self.assertEqual([path], self.left())
                with ops.transaction() as t:  # free the name for the next case
                    items = t.items
                    next(row for row in items if row["id"] == ident)["ref"]["retain_resources"] = False
                    t.save(items)

    def test_each_delete_sends_the_uid_and_version_the_review_showed(self):
        ident = self.uncertain(applied=True)
        review = self.review(ident)
        # A VM on its uid (its status moves); a claim, Secret or ConfigMap on its uid and version.
        shown = {row["resource"]["name"]: {key: row["current"][key] for key in
                                           (("uid",) if row["resource"]["kind"] == "VirtualMachine" else ("uid", "resourceVersion"))}
                 for row in review["plan"]["resources"]}
        with mock.patch.object(KUBE, "delete_if_present", wraps=KUBE.delete_if_present) as delete:
            self.remove(ident, review)
        fences = {call.args[0].rsplit("/", 1)[-1]: call.args[1]["preconditions"] for call in delete.call_args_list}
        self.assertEqual(shown, fences)

    def test_a_change_between_review_and_removal_is_refused_with_nothing_removed(self):
        ident = self.uncertain(applied=True)
        review = self.review(ident)
        claim = self.kube.object(CLAIM)
        claim["metadata"]["labels"] = {**(claim["metadata"].get("labels") or {}), "changed": "yes"}
        KUBE.ksend("PUT", CLAIM, claim)
        self.kube.writes.clear()
        with self.assertRaisesRegex(ValueError, "changed since the review"):
            self.remove(ident, review)
        self.assertEqual([], self.deletes())
        self.assertEqual(self.paths, self.left())

    def test_the_typed_name_and_a_fresh_review_are_needed_and_a_review_is_used_once(self):
        ident = self.uncertain(applied=False)
        review = self.review(ident)
        for changed in ({"confirm": "other"}, {"capacity_token": ""}, {"actor": "other-admin"}):
            with self.subTest(changed=changed), self.assertRaisesRegex(ValueError, "Nothing was removed"):
                self.remove(ident, review, **changed)
        self.assertEqual([], self.deletes())
        self.remove(ident, review)
        with self.assertRaisesRegex(ValueError, "nothing to remove"):
            self.remove(ident, review)

    def test_a_restart_part_way_finishes_only_what_was_reviewed(self):
        ident = self.uncertain(applied=True)
        review = self.review(ident)
        self.kube.fail(CLAIM, SystemExit("stopped"), method="DELETE")
        with self.assertRaises(SystemExit):
            self.remove(ident, review)
        self.assertEqual([CLAIM, SECRET], self.left())
        self.assertTrue(self.job(ident)["ref"]["retain_resources"], "half done holds the name")
        theirs = self.replace(SECRET)  # made again by somebody else while Homestead was down
        self.kube.add({"apiVersion": "v1", "kind": "Secret", "metadata": {"name": "web-other", "namespace": "lab"}})
        job.resume_cleanups(ops, KUBE.ksend)
        self.assertEqual([SECRET], self.left())
        self.assertEqual(theirs, self.kube.object(SECRET)["metadata"]["uid"])
        self.assertIsNotNone(self.kube.object("/api/v1/namespaces/lab/secrets/web-other"))
        item = self.job(ident)
        self.assertEqual("done", item["ref"]["cleanup"]["state"])
        self.assertIn("Kept: Secret web-cloudinit", item["message"])

    def test_only_a_failed_create_that_kept_what_it_made_can_be_reviewed(self):
        self.create()
        with self.assertRaisesRegex(ValueError, "nothing to remove"):
            self.review(ops.stored()[-1]["id"])

    def test_the_removal_routes_are_admins_and_kept_from_agents(self):
        for suffix in ("removal-preview", "remove"):
            self.assertIn("/api/operations/vm-recovery/" + suffix, server.ADMIN_ROUTES)
            self.assertEqual("admin", POLICY.role("/api/operations/vm-recovery/" + suffix, "POST"))

    def write_record_without_the_vm(self):
        """The Secret's POST times out, so the VM's is never sent."""
        self.kube.fail("/api/v1/namespaces/lab/secrets", TimeoutError("timed out"), method="POST")
        with self.assertRaisesRegex(ValueError, "uncertain"):
            self.create()
        self.kube.writes.clear()
        ident = ops.stored()[-1]["id"]
        self.assertNotIn("VirtualMachine", [w["resource"]["kind"] for w in self.job(ident)["ref"]["writes"]])
        return ident

    def vm(self, name, claim="web-disk", **meta):
        return {"apiVersion": "kubevirt.io/v1", "kind": "VirtualMachine", "metadata": {"name": name, "namespace": "lab", **meta},
                "spec": {"template": {"spec": {"volumes": [{"name": "disk", "persistentVolumeClaim": {"claimName": claim}}]}}}}

    def test_a_vm_of_the_jobs_name_made_by_other_means_blocks_the_removal_when_the_record_has_no_vm(self):
        ident = self.write_record_without_the_vm()
        self.kube.add(self.vm("web", labels={job.CREATE_JOB_LABEL: ident}))  # even with this job's id: it was never sent
        review = self.review(ident)
        self.assertTrue(review["plan"]["blocked"])
        self.assertIsNone(review["capacity_token"])
        self.assertIn("A VM named web exists", review["plan"]["blockers"][0])
        vm = next(row for row in review["plan"]["resources"] if row["resource"]["kind"] == "VirtualMachine")
        self.assertEqual(("Not made by this attempt", False), (vm["relationship"], vm["remove"]))
        with self.assertRaisesRegex(ValueError, "Nothing was removed"):
            self.remove(ident, {"capacity_token": f"{int(time.time()) + 600}.forged"})
        self.assertEqual([], self.deletes())
        self.assertIsNotNone(self.kube.object(CLAIM))

    def test_a_claim_another_vm_uses_is_kept_and_the_rest_goes(self):
        ident = self.uncertain(applied=False)
        self.kube.add(self.vm("media"))
        review = self.review(ident)
        claim = next(row for row in review["plan"]["resources"] if row["resource"]["name"] == "web-disk")
        self.assertEqual(("Used by VM media", False, True), (claim["relationship"], claim["remove"], claim["holds"]))
        result = self.remove(ident, review)
        self.assertEqual([CLAIM], self.left())
        self.assertNotIn(CLAIM, self.deletes())
        self.assertFalse(result["ok"])
        self.assertEqual("admin removed part of what this attempt created. Kept: Volume web-disk. Check them, then review again.",
                         result["detail"])
        self.assertTrue(self.job(ident)["ref"]["retain_resources"])

    def test_vms_that_cannot_be_read_keep_everything(self):
        ident = self.uncertain(applied=False)
        self.kube.fail(VMS, 500, method="GET")
        review = self.review(ident)
        self.assertTrue(review["plan"]["blocked"])
        self.assertIn("The VMs and pods in lab could not be read", review["plan"]["blockers"][0])
        self.assertEqual([], self.deletes())

    def test_a_status_write_on_the_vm_after_the_review_does_not_refuse_its_removal(self):
        ident = self.uncertain(applied=True)
        review = self.review(ident)
        vm = self.kube.object(WEB)
        vm["status"] = {"printableStatus": "Starting"}
        KUBE.ksend("PUT", WEB + "/status", vm)
        self.assertNotEqual(review["plan"]["resources"][0]["current"]["resourceVersion"], self.kube.object(WEB)["metadata"]["resourceVersion"])
        self.remove(ident, review)
        self.assertEqual([], self.left())

    def test_a_vm_spec_change_after_the_review_refuses_it(self):
        ident = self.uncertain(applied=True)
        review = self.review(ident)
        vm = self.kube.object(WEB)
        vm["spec"]["runStrategy"] = "Halted"
        KUBE.ksend("PUT", WEB, vm)
        self.kube.writes.clear()
        with self.assertRaisesRegex(ValueError, "changed since the review"):
            self.remove(ident, review)
        self.assertEqual([], self.deletes())

    def test_disks_the_vm_owns_through_its_templates_are_shown_as_going_with_it(self):
        ident = self.uncertain(applied=True)
        vm = self.kube.object(WEB)
        vm["spec"]["dataVolumeTemplates"] = [{"metadata": {"name": "web-rootdisk"}, "spec": {}}]
        KUBE.ksend("PUT", WEB, vm)
        plan = self.review(ident)["plan"]
        row = next(row for row in plan["resources"] if row["resource"]["name"] == "web-rootdisk")
        self.assertEqual(("Goes with the VM", False, True), (row["relationship"], row["remove"], row["with_vm"]))
        self.assertTrue(any(w.startswith("Disks made from the VM's own templates are not in this job's record. KubeVirt removes")
                            for w in plan["warnings"]))

    def test_a_removal_that_removed_nothing_says_so(self):
        ident = self.uncertain(applied=True)
        review = self.review(ident)
        self.kube.fail(WEB, 500, method="DELETE")
        result = self.remove(ident, review)
        self.assertEqual((False, 0), (result["ok"], result["removed"]))
        self.assertTrue(result["detail"].startswith("Nothing was removed: what the review showed changed since"), result["detail"])
        self.assertIn("Kept: VM web, ConfigMap web-vbios, Volume web-disk, Secret web-cloudinit", result["detail"])

    def test_a_removal_being_finished_is_said_and_not_reviewed_again(self):
        ident = self.uncertain(applied=True)
        review = self.review(ident)
        self.kube.fail(CLAIM, SystemExit("stopped"), method="DELETE")
        with self.assertRaises(SystemExit):
            self.remove(ident, review)
        with self.assertRaisesRegex(ValueError, "A removal of this job is being finished"):
            self.review(ident)

    def test_an_object_being_removed_blocks_the_review(self):
        ident = self.uncertain(applied=True)
        claim = self.kube.object(CLAIM)
        claim["metadata"]["finalizers"] = ["kubernetes.io/pvc-protection"]
        KUBE.ksend("PUT", CLAIM, claim)
        KUBE.ksend("DELETE", CLAIM)
        review = self.review(ident)
        self.assertTrue(review["plan"]["blocked"])
        self.assertIn("Volume web-disk is being removed", review["plan"]["blockers"][0])

    def test_a_record_naming_another_namespace_or_an_older_job_is_refused(self):
        ident = self.uncertain(applied=False)
        with ops.transaction() as t:
            items = t.items
            row = next(r for r in items if r["id"] == ident)
            saved = copy.deepcopy(row["ref"])
            row["ref"]["writes"][0]["resource"]["namespace"] = "other"
            t.save(items)
        with self.assertRaisesRegex(ValueError, "malformed"):
            self.review(ident)
        with ops.transaction() as t:
            items = t.items
            row = next(r for r in items if r["id"] == ident)
            row["ref"] = {**saved, "dispatch_protocol": 0}
            t.save(items)
        with self.assertRaisesRegex(ValueError, "nothing to remove"):
            self.review(ident)

    def kept_by(self, ident, path, user):
        """The object at path is kept, said as used by user, and the rest goes."""
        review = self.review(ident)
        row = next(row for row in review["plan"]["resources"] if row["resource"]["name"] == path.rsplit("/", 1)[-1])
        self.assertEqual((f"Used by {user}", False, True), (row["relationship"], row["remove"], row["holds"]))
        self.remove(ident, review)
        self.assertNotIn(path, self.deletes())
        self.assertIsNotNone(self.kube.object(path))

    def test_every_form_a_vm_uses_a_claim_secret_or_configmap_by_keeps_it(self):
        sidecar = json.dumps([{"args": ["--version", "v1alpha2"], "configMap": {"name": "web-vbios", "key": "k", "hookPath": "/h"}}])
        forms = [
            (CLAIM, {"volumes": [{"name": "d", "persistentVolumeClaim": {"claimName": "web-disk"}}]}),
            (CLAIM, {"volumes": [{"name": "d", "dataVolume": {"name": "web-disk"}}]}),
            (CLAIM, {"volumes": [{"name": "d", "ephemeral": {"persistentVolumeClaim": {"claimName": "web-disk"}}}]}),
            (CLAIM, {"volumes": [{"name": "d", "memoryDump": {"claimName": "web-disk"}}]}),
            (CLAIM, {"dataVolumeTemplates": [{"metadata": {"name": "copy"}, "spec": {"source": {"pvc": {"name": "web-disk", "namespace": "lab"}}}}]}),
            (CLAIM, {"status": {"volumeRequests": [{"addVolumeOptions": {"name": "d", "volumeSource": {"persistentVolumeClaim": {"claimName": "web-disk"}}}}]}}),
            (SECRET, {"volumes": [{"name": "c", "cloudInitNoCloud": {"secretRef": {"name": "web-cloudinit"}}}]}),
            (SECRET, {"volumes": [{"name": "c", "cloudInitConfigDrive": {"networkDataSecretRef": {"name": "web-cloudinit"}}}]}),
            (SECRET, {"volumes": [{"name": "c", "secret": {"secretName": "web-cloudinit"}}]}),
            (SECRET, {"volumes": [{"name": "c", "sysprep": {"secret": {"name": "web-cloudinit"}}}]}),
            (SECRET, {"accessCredentials": [{"sshPublicKey": {"source": {"secret": {"secretName": "web-cloudinit"}}}}]}),
            (ROM, {"volumes": [{"name": "r", "configMap": {"name": "web-vbios"}}]}),
            (ROM, {"annotations": {"hooks.kubevirt.io/hookSidecars": sidecar}}),
            # A sidecar list that cannot be read keeps every ConfigMap.
            (ROM, {"annotations": {"hooks.kubevirt.io/hookSidecars": "not json"}}),
        ]
        for path, form in forms:
            with self.subTest(path=path, form=form):
                for leftover in self.paths + [f"{VMS}/media"]:
                    self.kube.discard(leftover)
                ident = self.uncertain(applied=False)
                template = {"spec": {k: v for k, v in form.items() if k in ("volumes", "accessCredentials")}}
                if "annotations" in form:
                    template["metadata"] = {"annotations": form["annotations"]}
                vm = {"apiVersion": "kubevirt.io/v1", "kind": "VirtualMachine", "metadata": {"name": "media", "namespace": "lab"},
                      "spec": {"template": template, **({"dataVolumeTemplates": form["dataVolumeTemplates"]} if "dataVolumeTemplates" in form else {})},
                      **({"status": form["status"]} if "status" in form else {})}
                self.kube.add(vm)
                self.kept_by(ident, path, "VM media")
                with ops.transaction() as t:  # free the name for the next case
                    items = t.items
                    next(row for row in items if row["id"] == ident)["ref"]["retain_resources"] = False
                    t.save(items)

    def test_every_form_a_pod_uses_an_object_by_keeps_it(self):
        forms = [
            (CLAIM, {"volumes": [{"name": "d", "persistentVolumeClaim": {"claimName": "web-disk"}}]}),
            (SECRET, {"volumes": [{"name": "p", "projected": {"sources": [{"secret": {"name": "web-cloudinit"}}]}}]}),
            (SECRET, {"containers": [{"name": "c", "env": [{"name": "X", "valueFrom": {"secretKeyRef": {"name": "web-cloudinit", "key": "k"}}}]}]}),
            (ROM, {"containers": [{"name": "c", "envFrom": [{"configMapRef": {"name": "web-vbios"}}]}]}),
            (ROM, {"initContainers": [{"name": "c", "env": [{"name": "X", "valueFrom": {"configMapKeyRef": {"name": "web-vbios", "key": "k"}}}]}]}),
        ]
        for path, spec in forms:
            with self.subTest(path=path, spec=spec):
                for leftover in self.paths + ["/api/v1/namespaces/lab/pods/app"]:
                    self.kube.discard(leftover)
                ident = self.uncertain(applied=False)
                self.kube.add({"apiVersion": "v1", "kind": "Pod", "metadata": {"name": "app", "namespace": "lab"},
                               "spec": {"containers": [{"name": "c", "image": "x"}], **spec}})
                self.kept_by(ident, path, "pod app")
                with ops.transaction() as t:
                    items = t.items
                    next(row for row in items if row["id"] == ident)["ref"]["retain_resources"] = False
                    t.save(items)

    def test_a_running_vm_with_no_vm_object_keeps_the_claim_it_uses(self):
        ident = self.uncertain(applied=False)
        self.kube.add({"apiVersion": "kubevirt.io/v1", "kind": "VirtualMachineInstance", "metadata": {"name": "bare", "namespace": "lab"},
                       "spec": {"volumes": [{"name": "d", "persistentVolumeClaim": {"claimName": "web-disk"}}]}})
        self.kept_by(ident, CLAIM, "running VM bare")

    def test_the_going_vms_own_instance_and_pods_do_not_keep_what_it_uses(self):
        ident = self.uncertain(applied=True)
        uid = self.kube.object(WEB)["metadata"]["uid"]
        uses = {"volumes": [{"name": "d", "persistentVolumeClaim": {"claimName": "web-disk"}}]}
        self.kube.add({"apiVersion": "kubevirt.io/v1", "kind": "VirtualMachineInstance",
                       "metadata": {"name": "web", "namespace": "lab", "uid": "vmi-1", "ownerReferences": [{"kind": "VirtualMachine", "name": "web", "uid": uid}]},
                       "spec": uses})
        self.kube.add({"apiVersion": "v1", "kind": "Pod", "metadata": {"name": "virt-launcher-web", "namespace": "lab", "uid": "pod-1",
                       "ownerReferences": [{"kind": "VirtualMachineInstance", "name": "web", "uid": "vmi-1"}]},
                       "spec": {"containers": [{"name": "c", "image": "x"}], **uses}})
        self.kube.add({"apiVersion": "v1", "kind": "Pod", "metadata": {"name": "hp-volume", "namespace": "lab", "uid": "pod-2",
                       "ownerReferences": [{"kind": "Pod", "name": "virt-launcher-web", "uid": "pod-1"}]},
                       "spec": {"containers": [{"name": "c", "image": "x"}], **uses}})
        review = self.review(ident)
        self.assertFalse(review["plan"]["blocked"], review["plan"])
        self.assertTrue(all(row["remove"] for row in review["plan"]["resources"]))

    def test_a_disk_going_with_the_vm_that_something_else_uses_keeps_the_vm(self):
        ident = self.uncertain(applied=True)
        vm = self.kube.object(WEB)
        vm["spec"]["dataVolumeTemplates"] = [{"metadata": {"name": "web-rootdisk"}, "spec": {}}]
        KUBE.ksend("PUT", WEB, vm)
        self.kube.add(self.vm("media", claim="web-rootdisk"))
        review = self.review(ident)
        self.assertTrue(review["plan"]["blocked"])
        self.assertIn("The disk web-rootdisk goes with the VM web, and VM media uses it. Nothing is removed.", review["plan"]["blockers"])
        with self.assertRaisesRegex(ValueError, "Nothing was removed"):
            self.remove(ident, {"capacity_token": f"{int(time.time()) + 600}.forged"})
        self.assertEqual([], self.deletes())

    def test_a_list_cut_short_or_pods_that_cannot_be_read_keep_everything(self):
        for path, answer in ((VMS, {"items": [], "metadata": {"continue": "more"}}),
                             ("/apis/kubevirt.io/v1/namespaces/lab/virtualmachineinstances", {"items": [], "metadata": {"continue": "more"}}),
                             ("/api/v1/namespaces/lab/pods", None)):
            with self.subTest(path=path):
                for leftover in self.paths:
                    self.kube.discard(leftover)
                ident = self.uncertain(applied=False)
                if answer is None:
                    self.kube.fail(path, 503, method="GET")
                else:
                    self.kube.react("GET", path, lambda method, target, body, answer=answer: answer)
                review = self.review(ident)
                self.kube._reactions.clear()
                self.kube.stop_failing()
                self.assertTrue(review["plan"]["blocked"])
                self.assertIn("could not be read", review["plan"]["blockers"][0])
                with ops.transaction() as t:
                    items = t.items
                    next(row for row in items if row["id"] == ident)["ref"]["retain_resources"] = False
                    t.save(items)

    def test_a_removal_with_nothing_left_says_so(self):
        ident = self.uncertain(applied=False)
        for path in self.paths:
            self.kube.discard(path)
        result = self.remove(ident, self.review(ident))
        self.assertEqual("Nothing was left to remove. The name can be used again.", result["detail"])
        self.assertFalse(self.job(ident)["ref"]["retain_resources"])
