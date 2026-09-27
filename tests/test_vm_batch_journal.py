import copy
from contextlib import contextmanager
import hashlib
import multiprocessing
import tempfile
import time
import unittest
import urllib.error
from unittest import mock

import test_vm_resources  # server import path
import homestead_operations as ops
import homestead_k3scluster as cluster
import homestead_cancel as cancel
import homestead_vm_mutation_recovery as recovery
import homestead_vm_power_job as power


def prepared():
    nodes = [{"name": "batch-server-1", "address": "192.0.2.10"}, {"name": "batch-agent-1", "address": "192.0.2.11"}]
    return {"namespace": "lab", "plan": {"name": "batch", "nodes": nodes, "first": "192.0.2.10", "setup": "k3s"},
            "configs": [{"name": row["name"], "namespace": "lab", "password": "test-private"} for row in nodes]}


def create_one(cfg, send):
    base = "/api/v1/namespaces/lab/secrets"
    send("POST", base, {"apiVersion": "v1", "kind": "Secret", "metadata": {"namespace": "lab", "name": cfg["name"] + "-login"},
                        "stringData": {"password": cfg["password"]}})
    vm = send("POST", "/apis/kubevirt.io/v1/namespaces/lab/virtualmachines", {
        "apiVersion": "kubevirt.io/v1", "kind": "VirtualMachine", "metadata": {"namespace": "lab", "name": cfg["name"]}})
    return {"vm_identity": vm["metadata"]}


def response(method, path, body, **kw):
    value = copy.deepcopy(body)
    value["metadata"].update(uid=value["metadata"]["name"] + "-uid", resourceVersion="1")
    return value


def race(directory, token, ready, go, results, writes):
    ops.bind(None, directory, None)
    ready.put(True)
    if not go.wait(10):
        return
    def send(*args, **kw):
        with writes.get_lock():
            writes.value += 1
        return response(*args, **kw)
    try:
        cluster.commit(prepared(), ops, create_one=create_one, review=token, send=send)
        results.put("accepted")
    except ValueError as error:
        results.put("duplicate" if "already has job" in str(error) else str(error))


def in_flight(directory, token, sending, finish):
    ops.bind(None, directory, None)
    def send(*args, **kw):
        sending.set()
        finish.wait(10)
        raise TimeoutError("test-private")
    try:
        cluster.commit(prepared(), ops, create_one=create_one, review=token, send=send)
    except ValueError:
        pass


class VMBatchJournalTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.body = {"capacity_token": f"{int(time.time()) + 600}.test-batch-approval"}
        self.objects, self.sent = {}, []
        self.after_send = lambda value: None
        for patch in (mock.patch.object(ops, "DATA_DIR", temporary.name),
                      mock.patch.object(recovery.REVIEW, "_key", return_value=b"batch-recovery-test"),
                      mock.patch.dict(ops.RESOLVERS, {"k3s-cluster": cluster.status}),
                      mock.patch.dict(ops.CANCELLERS, {"k3s-cluster": (cancel.k3s_plan, cancel.k3s_cancel)})):
            patch.start()
            self.addCleanup(patch.stop)

    def send(self, method, path, body, **kw):
        item = ops._read()[0]
        self.assertEqual("intent", item["ref"]["writes"][-1]["phase"])
        self.assertEqual("provisioning", item["ref"]["phase"])
        self.sent.append((method, path))
        value = response(method, path, body, **kw)
        self.objects[path + "/" + value["metadata"]["name"]] = copy.deepcopy(value)
        self.after_send(value)
        return value

    def dispatch(self, **kw):
        return cluster.commit(prepared(), ops, create_one=kw.pop("create_one", create_one), review=self.body, send=self.send, **kw)

    def read(self, path):
        if path not in self.objects:
            raise urllib.error.HTTPError(path, 404, "missing", {}, None)
        return copy.deepcopy(self.objects[path])

    def fail_second(self):
        def fail(value):
            if value["kind"] == "VirtualMachine" and value["metadata"]["name"].endswith("agent-1"):
                raise TimeoutError("test-private")
        self.after_send = fail
        with self.assertRaisesRegex(ValueError, "Resources are retained") as error:
            self.dispatch()
        self.assertNotIn("test-private", str(error.exception))
        return ops._read()[0]["id"]

    def approved(self, ident):
        result = recovery.preview(ident, ops, self.read, "admin")
        return {"id": ident, "capacity_token": result["capacity_token"], "confirm_capacity": True,
                "confirm": "batch", "acknowledge_unknown": True}

    def test_whole_batch_has_one_journal_and_per_resource_receipts(self):
        result = self.dispatch()
        self.assertEqual("running", result["status"])
        self.assertTrue(result["mutation_recovery"])
        self.assertFalse(result["cancellable"])
        self.assertFalse(result["cleanable"])
        item = ops._read()[0]
        self.assertEqual("awaiting-ready", item["ref"]["phase"])
        self.assertEqual([1, 2, 3, 4], [entry["sequence"] for entry in item["ref"]["writes"]])
        self.assertTrue(all(entry["phase"] == "accepted" for entry in item["ref"]["writes"]))
        self.assertEqual(2, len(item["ref"]["created"]))
        self.assertNotIn("test-private", str(item))
        self.assertNotIn(self.body["capacity_token"], str(item))
        with self.assertRaisesRegex(ValueError, "already has job"):
            self.dispatch()
        self.assertEqual(4, len(self.sent))

    def test_partial_and_uncertain_resources_remain_visible_without_automatic_cleanup(self):
        ident = self.fail_second()
        item = ops._read()[0]
        self.assertEqual("failed", item["status"])
        self.assertEqual(["accepted", "accepted", "accepted", "uncertain"], [entry["phase"] for entry in item["ref"]["writes"]])
        self.assertEqual(1, len(item["ref"]["created"]))
        plan = recovery.preview(ident, ops, self.read, "admin")["plan"]
        self.assertEqual(4, len(plan["resources"]))
        self.assertEqual("identity unproven", plan["resources"][1]["relationship"])
        self.assertNotIn("test-private", str(plan))
        self.assertEqual(0, ops.dismiss_finished()["dismissed"])
        self.assertFalse(cancel.k3s_plan(item)["can"])
        with self.assertRaises(ValueError):
            cancel.k3s_cancel(item, {})

    def test_failed_first_write_shows_all_planned_vms_including_unsent(self):
        self.after_send = mock.Mock(side_effect=TimeoutError("test-private"))
        with self.assertRaises(ValueError):
            self.dispatch()
        plan = recovery.preview(ops._read()[0]["id"], ops, self.read, "admin")["plan"]
        self.assertEqual(3, len(plan["resources"]))
        self.assertEqual(["not dispatched", "not dispatched"], [row["last_write"] for row in plan["resources"][:2]])
        self.assertEqual(1, len(self.sent))

    def test_recovery_retains_resources_and_keeps_approval_consumed_after_clear_and_clock_rollback(self):
        ident = self.fail_second()
        before = copy.deepcopy(self.objects)
        good = self.approved(ident)
        recovery.resolve(good, ops, self.read, "admin")
        item = ops._read()[0]
        self.assertEqual("failed", item["status"])
        self.assertEqual("unknown", item["ref"]["recovery"]["outcome"])
        self.assertTrue(item["tracking_stopped"])
        self.assertFalse(ops._public(item)["mutation_recovery"])
        self.assertEqual(before, self.objects)
        with mock.patch.object(time, "time", return_value=time.time() + 10000):
            self.assertEqual(1, ops.dismiss_finished()["dismissed"])
        with mock.patch.object(time, "time", return_value=1), self.assertRaisesRegex(ValueError, "already has job"):
            self.dispatch()
        self.assertEqual(4, len(self.sent))

    def test_changed_resource_or_actor_invalidates_recovery_approval(self):
        ident = self.fail_second()
        good = self.approved(ident)
        with self.assertRaises(recovery.REVIEW.Rejected):
            recovery.resolve(good, ops, self.read, "other-admin")
        next(iter(self.objects.values()))["metadata"]["resourceVersion"] = "new-version"
        with self.assertRaises(recovery.REVIEW.Rejected):
            recovery.resolve(good, ops, self.read, "admin")

    def test_batch_and_individual_vm_actions_block_each_other_on_shared_targets(self):
        self.fail_second()
        for kind in ("vm-power", "vm-create", "vm-edit"):
            with self.assertRaisesRegex(ValueError, "needs recovery"):
                ops.start(kind, "new", {}, "/vms", {"name": "batch-server-1", "namespace": "lab", "review_digest": hashlib.sha256(kind.encode()).hexdigest()})
        self.assertEqual(1, len(ops._read()))

    def test_individual_action_prevents_a_conflicting_batch_before_any_write(self):
        ops.start("vm-edit", "edit", {}, "/vms", {"name": "batch-agent-1", "namespace": "lab", "review_digest": "a" * 64})
        with self.assertRaisesRegex(ValueError, "needs recovery"):
            self.dispatch()
        self.assertEqual([], self.sent)

    def test_batch_name_alone_does_not_miss_differently_named_batch_target_collision(self):
        self.fail_second()
        other = prepared()
        other["plan"]["name"] = "other-batch"
        with self.assertRaisesRegex(ValueError, "needs recovery"):
            cluster.commit(other, ops, create_one=create_one, review={"capacity_token": "100.new"}, send=self.send)

    def test_receipt_mismatch_or_swallowed_write_failure_never_advances_to_next_vm(self):
        def create(cfg, send):
            result = create_one(cfg, send)
            result["vm_identity"]["uid"] = "wrong-vm"
            return result
        with self.assertRaises(ValueError):
            self.dispatch(create_one=create)
        self.assertEqual(2, len(self.sent))
        self.assertEqual([], ops._read()[0]["ref"]["created"])

    def test_missing_approval_or_store_failure_sends_nothing(self):
        with self.assertRaises(ValueError):
            cluster.commit(prepared(), ops, create_one=create_one, review={}, send=self.send)
        with mock.patch.object(ops, "_write", side_effect=OSError("disk")), self.assertRaises(OSError):
            self.dispatch()
        self.assertEqual([], self.sent)

    def test_recovery_winning_pre_dispatch_lock_fences_late_worker(self):
        @contextmanager
        def resolve_first(ident, _ops):
            recovery.resolve(self.approved(ident), ops, self.read, "admin")
            yield
        # Recovery uses the same module's worker_lock, so save/restore it while
        # making the competing inspection, then give the resolved job to worker.
        original = power.worker_lock
        @contextmanager
        def lock(ident, _ops):
            with mock.patch.object(power, "worker_lock", original):
                with resolve_first(ident, _ops):
                    yield
        with mock.patch.object(power, "worker_lock", lock), self.assertRaises(ValueError):
            self.dispatch()
        self.assertEqual([], self.sent)
        self.assertEqual("resolved-unknown", ops._read()[0]["ref"]["phase"])

    def test_actual_two_process_duplicate_approval_dispatches_one_batch(self):
        ctx = multiprocessing.get_context("spawn")
        ready, results, go, writes = ctx.Queue(), ctx.Queue(), ctx.Event(), ctx.Value("i", 0)
        children = [ctx.Process(target=race, args=(ops.DATA_DIR, self.body, ready, go, results, writes)) for _ in range(2)]
        for child in children:
            child.start()
        try:
            for _ in children:
                ready.get(timeout=10)
            go.set()
            self.assertEqual(["accepted", "duplicate"], sorted(results.get(timeout=10) for _ in children))
            self.assertEqual(4, writes.value)
        finally:
            go.set()
            for child in children:
                child.join(10)
                if child.is_alive():
                    child.terminate()
                    child.join(5)

    def test_active_dispatcher_in_another_process_blocks_recovery(self):
        ctx = multiprocessing.get_context("spawn")
        sending, finish = ctx.Event(), ctx.Event()
        child = ctx.Process(target=in_flight, args=(ops.DATA_DIR, self.body, sending, finish))
        child.start()
        try:
            self.assertTrue(sending.wait(10))
            ident = ops._read()[0]["id"]
            result = recovery.preview(ident, ops, self.read, "admin")
            self.assertTrue(result["plan"]["blocked"])
            self.assertIsNone(result["capacity_token"])
            self.assertFalse(cancel.k3s_plan(ops._read()[0])["can"])
        finally:
            finish.set()
            child.join(10)
            if child.is_alive():
                child.terminate()
                child.join(5)
        self.assertFalse(recovery.preview(ident, ops, self.read, "admin")["plan"]["blocked"])

    def test_open_guest_ports_do_not_prove_authenticated_health(self):
        self.dispatch()
        item = ops._read()[0]
        def read(path):
            if "/virtualmachines/" in path:
                return self.read(path)
            name = path.rsplit("/", 1)[-1]
            return {"metadata": {"ownerReferences": [{"controller": True, "kind": "VirtualMachine", "apiVersion": "kubevirt.io/v1", "name": name, "uid": name + "-uid"}]},
                    "status": {"phase": "Running"}}
        with mock.patch.object(cluster, "kget", side_effect=read), mock.patch.object(cluster, "_answers", return_value=True):
            status, _, message = cluster.status(item)
        self.assertEqual("running", status)
        self.assertIn("quorum are not verified", message)
