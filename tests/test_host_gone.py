"""Ending a disk job whose host was removed: the shared
"settled" rule, the proof that the host is gone, the review and the end."""
import contextlib
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
import homestead_disk_allocation as ALLOC
import homestead_disk_v2 as V2
import homestead_host_gone as HOST_GONE
import homestead_kube as K
import homestead_longhorn_runtime as RUNTIME
import homestead_operations as OPS
import homestead_storage_conflicts as CONFLICTS
import server  # binds and registers the job kinds, as in the app
from fakes.kube import Kube

NODES = "/api/v1/nodes"
LH = "/apis/longhorn.io/v1beta2/namespaces/longhorn-system/nodes"
JOBS = "/apis/batch/v1/namespaces/lab/jobs"

# The phases each kind can be stranded in when its host goes.
STUCK = {
    "disk-v2-convert": ("evacuating", "awaiting-erase", "removing", "dispatching", "preparing", "registering", "verifying"),
    "disk-allocation": ("queued", "dispatching", "growing"),
    "longhorn-runtime": ("queued", "staging", "staged", "relocated", "retire-staged"),
}


def stuck(kind, phase, node="node-a", status="running", job_id=None, **extra):
    """A job as its module saves it, with the identities it pins."""
    ref = {"node": node, "phase": phase, "node_uid": "uid-" + node, "boot_id": "boot-1", "request_id": "a" * 24}
    if kind in ("disk-v2-convert", "longhorn-runtime"):
        ref.update(lh_uid="lh-uid-" + node, disk="disk-1", disk_uuid="du-1",
                   hardware={"device": "/dev/sdb", "by_id": "/dev/disk/by-id/ata-DEMO", "serial": "SER1",
                             "size_bytes": 500 * 1024 ** 3})
    if kind == "disk-v2-convert":
        ref["volumes"] = [{"volume": "pvc-moved"}]
        if phase in ("dispatching", "preparing", "registering", "verifying"):
            ref.update(job="homestead-disk-v2-" + "a" * 24, job_uid="helper-uid")
    if kind == "disk-allocation":
        ref.update(area={"name": "longhorn", "lv_name": "longhorn", "size_bytes": 100 * 1024 ** 3}, role="longhorn",
                   target_bytes=200 * 1024 ** 3, job="homestead-allocation-" + "a" * 24, namespace="lab")
        if phase != "queued":
            ref["job_uid"] = "helper-uid"
    if kind == "longhorn-runtime":
        ref.update(retained="/mnt/homestead-retired-" + "a" * 24, service="k3s-agent")
    ref.update(extra)
    return {"id": job_id or f"{kind}-{phase}", "kind": kind, "title": f"A {kind} job on {node}", "status": status,
            "progress": 40, "started_at": "2026-11-04T10:00:00Z", "updated_at": "2026-11-04T10:00:00Z",
            "resource": {"kind": "Node", "name": node}, "href": "/nodes", "ref": ref}


def host(name="node-a", uid=None, ready=True):
    return {"apiVersion": "v1", "kind": "Node", "metadata": {"name": name, "uid": uid or "uid-" + name},
            "status": {"conditions": [{"type": "Ready", "status": "True" if ready else "Unknown"}]}}


def longhorn(name="node-a", uid=None):
    return {"apiVersion": "longhorn.io/v1beta2", "kind": "Node",
            "metadata": {"name": name, "namespace": "longhorn-system", "uid": uid or "lh-uid-" + name}}


def helper(name, uid="helper-uid"):
    return {"apiVersion": "batch/v1", "kind": "Job", "metadata": {"name": name, "namespace": "lab", "uid": uid}}


class Store(unittest.TestCase):
    """The real job store in a temp DATA_DIR; the disk modules read it."""

    def setUp(self):
        self.kube = self.enterContext(Kube())   # never the real client
        tmp = self.enterContext(tempfile.TemporaryDirectory())
        self.enterContext(mock.patch.object(OPS, "DATA_DIR", tmp))
        self.enterContext(mock.patch.multiple(V2, ops=OPS, kget=K.kget, ksend=K.ksend, NS="lab"))
        self.enterContext(mock.patch.multiple(ALLOC, ops=OPS, kget=K.kget, ksend=K.ksend, NS="lab"))
        self.enterContext(mock.patch.object(RUNTIME, "ops", OPS))

    def save(self, *jobs):
        with OPS.transaction() as tx:
            tx.save(list(jobs))

    def saved(self, ident):
        return next(i for i in OPS.stored() if i["id"] == ident)


def guarded(module):
    """A disks module stand-in whose mutation scope is the job module's own check:
    it wraps mutation_scope, or installs it with add_guard (fix/disk-guards-kept)."""
    disks = SimpleNamespace(mutation_scope=lambda node, disk, device: contextlib.nullcontext())
    disks.add_guard = lambda name, scope: setattr(disks, "mutation_scope", scope)
    module.protect_mutations(disks)
    return disks


class SettledTests(Store):
    def holds(self, kind):
        """Every rule that keeps a stuck job of this kind from being forgotten."""
        items = OPS.stored()
        checks = [lambda: CONFLICTS.require_clear(items, "cluster-shutdown", {}),
                  lambda: CONFLICTS.require_clear(items, "platform-upgrade", {"component": "longhorn"}),
                  lambda: CONFLICTS.require_clear(items, "os-rollout", {}),
                  lambda: CONFLICTS.require_clear(items, "node-power", {"node": "node-a", "action": "shutdown"})]
        if kind == "disk-v2-convert":
            checks.append(lambda: V2.mutation_guard("node-a"))
        if kind == "disk-allocation":
            checks.append(lambda: guarded(ALLOC).mutation_scope("node-a", "disk-1", "/dev/sdb").__enter__())
        if kind == "longhorn-runtime":
            checks += [lambda: guarded(RUNTIME).mutation_scope("node-a", "disk-1", "/dev/sdb").__enter__(),
                       lambda: RUNTIME._conflicts("node-b")]
        refused = 0
        for check in checks:
            try:
                check()
            except ValueError:
                refused += 1
        return refused

    def test_a_stuck_job_holds_and_an_ended_one_is_settled_everywhere(self):
        for kind, phases in STUCK.items():
            with self.subTest(kind):
                self.save(stuck(kind, phases[-1]))
                self.assertTrue(CONFLICTS.unresolved(OPS.stored()[0]))
                self.assertGreater(self.holds(kind), 0, "a stuck job is never forgotten")

                self.save(stuck(kind, "host-gone", status="failed"))
                self.assertFalse(CONFLICTS.unresolved(OPS.stored()[0]))
                self.assertTrue(CONFLICTS.settled(OPS.stored()[0]))
                self.assertEqual(0, self.holds(kind), "an ended job blocks nothing")
                self.assertEqual([], V2.tasks(), "the Disks page lists no saved preparation")
                self.assertIsNone(V2._active_for("node-a", "disk-1"))
                self.assertIsNone(ALLOC._pending("node-a"))
                self.assertIsNone(RUNTIME._pending("node-a"))

    def test_the_evacuated_volumes_are_released(self):
        reclass = {"namespace": "lab", "claim": "data", "copy_claims": {
            "data": {"pv": "pv-x", "csi_driver": "driver.longhorn.io", "csi_handle": "pvc-moved"}}}
        self.save(stuck("disk-v2-convert", "removing"))
        with self.assertRaisesRegex(ValueError, "another storage job"):
            CONFLICTS.require_clear(OPS.stored(), "reclass", reclass)
        self.save(stuck("disk-v2-convert", "host-gone", status="failed"))
        CONFLICTS.require_clear(OPS.stored(), "reclass", reclass)

    def test_the_jobs_tray_offers_the_end_only_for_stuck_jobs_of_the_three_kinds(self):
        for kind, phases in STUCK.items():
            with self.subTest(kind):
                self.assertIs(True, OPS._public(stuck(kind, phases[0]))["host_gone_endable"])
                self.assertIs(True, OPS._public(stuck(kind, phases[0], status="failed"))["host_gone_endable"])
                self.assertIs(False, OPS._public(stuck(kind, "host-gone", status="failed"))["host_gone_endable"])
        self.assertIs(False, OPS._public(stuck("disk-v2-convert", "complete", status="succeeded"))["host_gone_endable"])
        for other in ("disk-retire", "longhorn-v2-cleanup", "node-power"):
            self.assertNotIn("host_gone_endable", OPS._public(stuck(other, "stop")))

    def test_an_ended_job_is_dismissible(self):
        self.save(stuck("disk-allocation", "growing", status="failed"))
        self.assertFalse(OPS._public(OPS.stored()[0])["dismissible"])
        self.save(stuck("disk-allocation", "host-gone", status="failed"))
        self.assertTrue(OPS._public(OPS.stored()[0])["dismissible"])
        OPS.dismiss("disk-allocation-host-gone")
        self.assertEqual([], [i for i in OPS.snapshot() if i["id"] == "disk-allocation-host-gone"])

    def test_recheck_never_reopens_an_ended_job(self):
        self.save(stuck("disk-allocation", "host-gone", status="failed", job_id="alloc"),
                  stuck("longhorn-runtime", "host-gone", status="failed", job_id="runtime"))
        with self.assertRaisesRegex(ValueError, "ended because its host was removed"):
            ALLOC.recheck("alloc")
        with self.assertRaisesRegex(ValueError, "ended because its host was removed"):
            RUNTIME.recheck("runtime")
        self.assertEqual({"host-gone"}, {i["ref"]["phase"] for i in OPS.stored()})



class Review(Store):
    def setUp(self):
        super().setUp()
        self.enterContext(mock.patch.object(HOST_GONE.REVIEW, "_key", return_value=b"host-gone-tests-only"))

    def preview(self, ident):
        return HOST_GONE.preview(ident, OPS, K.kget, "admin", "lab")

    def approved(self, ident):
        review = self.preview(ident)
        self.assertFalse(review["plan"]["blocked"], review)
        return {"id": ident, "capacity_token": review["capacity_token"], "confirm_capacity": True,
                "confirm": review["plan"]["confirm"], "acknowledge_host_gone": True}

    def end(self, ident):
        return HOST_GONE.resolve(self.approved(ident), OPS, K.kget, K.ksend, "admin", "lab")


class ProofTests(Review):
    """Only identity says a host is gone: 404 or another uid, read fresh."""

    def test_a_removed_host_can_be_ended(self):
        self.save(stuck("disk-v2-convert", "removing", job_id="v2"))
        before = OPS.stored()
        review = self.preview("v2")
        plan = review["plan"]
        self.assertFalse(plan["blocked"])
        self.assertTrue(review["capacity_token"])
        self.assertEqual("node-a", plan["confirm"])
        self.assertEqual({"node": "missing", "longhorn": "missing"}, plan["evidence"])
        self.assertEqual([], self.kube.writes, "a review changes nothing")
        self.assertEqual(before, OPS.stored())

    def test_a_host_rebuilt_with_a_new_identity_counts_as_gone(self):
        self.kube.add(host(uid="uid-rebuilt"))
        self.kube.add(longhorn(uid="lh-uid-rebuilt"))
        self.save(stuck("longhorn-runtime", "staged", job_id="rt"))
        plan = self.preview("rt")["plan"]
        self.assertFalse(plan["blocked"])
        self.assertEqual({"node": "replaced", "longhorn": "replaced"}, plan["evidence"])
        self.assertIn("new identity", " ".join(plan["warnings"]))

    def test_the_same_host_still_there_refuses_even_when_not_ready(self):
        self.kube.add(host(ready=False))
        self.save(stuck("disk-allocation", "growing", job_id="alloc"))
        review = self.preview("alloc")
        self.assertTrue(review["plan"]["blocked"])
        self.assertIsNone(review["capacity_token"])
        self.assertIn("still in the cluster with the same identity", " ".join(review["plan"]["blockers"]))

    def test_a_read_that_failed_is_not_proof(self):
        self.save(stuck("disk-v2-convert", "removing", job_id="v2"))
        for failure in (500, 403, 503, TimeoutError("slow"), OSError("refused")):
            with self.subTest(failure=failure):
                self.kube.fail(NODES + "/node-a", failure, times=5)
                review = self.preview("v2")
                self.assertTrue(review["plan"]["blocked"])
                self.assertIsNone(review["capacity_token"])
                self.assertIn("could not be read", " ".join(review["plan"]["blockers"]))
                self.kube._failures.clear()
        self.kube.fail(LH + "/node-a", 500, times=5)
        self.assertTrue(self.preview("v2")["plan"]["blocked"], "a failed Longhorn read refuses too")

    def test_an_answer_without_an_identity_is_not_proof(self):
        self.kube.react("GET", NODES + "/node-a", lambda method, path, body: {"kind": "Node", "metadata": {"name": "node-a"}})
        self.save(stuck("disk-allocation", "growing", job_id="alloc"))
        plan = self.preview("alloc")["plan"]
        self.assertTrue(plan["blocked"])
        self.assertIn("could not be read (no identity in the answer)", " ".join(plan["blockers"]))

    def test_longhorn_still_holding_the_pinned_identity_refuses(self):
        self.kube.add(longhorn())
        for kind, phase in (("disk-v2-convert", "verifying"), ("longhorn-runtime", "relocated")):
            with self.subTest(kind):
                self.save(stuck(kind, phase, job_id="job"))
                plan = self.preview("job")["plan"]
                self.assertTrue(plan["blocked"])
                self.assertIn("Longhorn still has", " ".join(plan["blockers"]))
                # Longhorn kept its record after the removal: the Cluster page's leftovers delete it.
                self.assertIn('Hosts coming and going, "Longhorn still lists node-a"', " ".join(plan["blockers"]))
                self.assertNotIn("Remove the host from Cluster first", " ".join(plan["blockers"]))

    def test_allocation_pins_no_longhorn_identity(self):
        self.kube.add(longhorn())
        self.save(stuck("disk-allocation", "growing", job_id="alloc"))
        plan = self.preview("alloc")["plan"]
        self.assertFalse(plan["blocked"])
        self.assertEqual({"node": "missing"}, plan["evidence"])

    def test_a_job_without_a_pinned_uid_cannot_be_ended(self):
        job = stuck("disk-v2-convert", "removing", job_id="v2")
        del job["ref"]["node_uid"]
        self.save(job)
        plan = self.preview("v2")["plan"]
        self.assertTrue(plan["blocked"])
        self.assertIn("did not record", " ".join(plan["blockers"]))

    def test_only_a_stuck_job_of_the_three_kinds_has_a_review(self):
        self.save(stuck("disk-v2-convert", "complete", status="succeeded", job_id="done"),
                  stuck("disk-retire", "stop", job_id="retire"), stuck("longhorn-v2-cleanup", "staging", job_id="cleanup"))
        for ident in ("done", "retire", "cleanup", "missing"):
            with self.subTest(ident), self.assertRaisesRegex(ValueError, "no disk job left by a removed host"):
                self.preview(ident)

    def test_the_review_says_what_is_left_and_what_it_releases(self):
        self.save(stuck("longhorn-runtime", "staged", job_id="rt"),
                  stuck("disk-v2-convert", "preparing", job_id="v2", node="node-b"),
                  stuck("disk-allocation", "growing", job_id="alloc", node="node-c"))
        runtime = " ".join(self.preview("rt")["plan"]["left_on_host"])
        for text in ("/etc/systemd/system/homestead-longhorn-runtime.service", "k3s-agent.service.d/homestead-runtime.conf",
                     "/var/lib/homestead/longhorn-runtime/" + "a" * 24, "/mnt/homestead-retired-" + "a" * 24, "before k3s"):
            self.assertIn(text, runtime)
        v2 = self.preview("v2")["plan"]
        self.assertIn("/dev/sdb", " ".join(v2["left_on_host"]))
        self.assertIn("partly erased", " ".join(v2["left_on_host"]))
        self.assertIn("pvc-moved", " ".join(v2["released"]))
        self.assertIn("Longhorn upgrades, cluster shutdown and OS rollouts", " ".join(v2["released"]))
        alloc = self.preview("alloc")["plan"]
        self.assertIn("partly grown", " ".join(alloc["left_on_host"]))
        self.assertIn("deletes the helper Job below", " ".join(alloc["warnings"]))

    def test_queued_work_left_nothing_on_the_host(self):
        self.save(stuck("disk-allocation", "queued", job_id="alloc"))
        self.assertIn("Nothing was started on the host", " ".join(self.preview("alloc")["plan"]["left_on_host"]))


class EndTests(Review):
    def test_each_kind_ends_from_each_stuck_phase(self):
        for kind, phases in STUCK.items():
            for phase in phases:
                for status in ("running", "failed"):
                    with self.subTest(kind=kind, phase=phase, status=status):
                        job = stuck(kind, phase, status=status, job_id="job")
                        self.save(job)
                        result = self.end("job")
                        self.assertTrue(result["ok"])
                        item = self.saved("job")
                        self.assertEqual(("failed", "host-gone"), (item["status"], item["ref"]["phase"]))
                        record = item["ref"]["host_gone"]
                        self.assertEqual((phase, status, "admin"), (record["previous_phase"], record["previous_status"], record["by"]))
                        self.assertEqual({k: v for k, v in job["ref"].items() if k != "phase"},
                                         {k: v for k, v in item["ref"].items() if k not in ("phase", "host_gone")})
                        self.assertTrue(item["message"].startswith("Ended: its host node-a was removed"))
                        self.assertFalse(CONFLICTS.unresolved(item))
                        public = result["operation"]
                        self.assertTrue(public["dismissible"])
                        self.assertFalse(public["host_gone_endable"])

    def test_the_confirmation_must_be_given_in_full(self):
        self.save(stuck("disk-v2-convert", "removing", job_id="v2"))
        good = self.approved("v2")
        before = OPS.stored()
        for change in ({"confirm": "node-b"}, {"confirm": ""}, {"acknowledge_host_gone": False}, {"capacity_token": "1.x"},
                       {"confirm_capacity": False}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                HOST_GONE.resolve({**good, **change}, OPS, K.kget, K.ksend, "admin", "lab")
            self.assertEqual(before, OPS.stored())

    def test_evidence_that_changed_after_the_review_refuses(self):
        self.save(stuck("disk-v2-convert", "removing", job_id="v2"))
        good = self.approved("v2")
        self.kube.add(host(uid="uid-rebuilt"))       # gone still, but not as reviewed
        with self.assertRaisesRegex(ValueError, "Review again"):
            HOST_GONE.resolve(good, OPS, K.kget, K.ksend, "admin", "lab")
        self.kube.add(host())                        # back with the pinned identity
        with self.assertRaisesRegex(ValueError, "same identity"):
            HOST_GONE.resolve(good, OPS, K.kget, K.ksend, "admin", "lab")
        self.assertEqual("removing", self.saved("v2")["ref"]["phase"])

    def test_a_helper_job_that_vanished_after_the_review_refuses(self):
        name = "homestead-disk-v2-" + "a" * 24
        self.kube.add(helper(name))
        self.save(stuck("disk-v2-convert", "preparing", job_id="v2"))
        good = self.approved("v2")
        self.kube.discard(f"{JOBS}/{name}")
        with self.assertRaisesRegex(ValueError, "Review again"):
            HOST_GONE.resolve(good, OPS, K.kget, K.ksend, "admin", "lab")
        self.assertEqual("preparing", self.saved("v2")["ref"]["phase"])
        self.assertEqual([], self.kube.writes)

    def test_one_admins_review_does_not_end_the_job_for_another(self):
        self.save(stuck("disk-v2-convert", "removing", job_id="v2"))
        good = self.approved("v2")    # reviewed by "admin"
        with self.assertRaisesRegex(ValueError, "Review again"):
            HOST_GONE.resolve(good, OPS, K.kget, K.ksend, "other-admin", "lab")
        self.assertEqual("removing", self.saved("v2")["ref"]["phase"])

    def test_the_end_releases_upgrades_shutdown_and_rollouts(self):
        for kind, phases in STUCK.items():
            with self.subTest(kind):
                self.save(stuck(kind, phases[1], job_id="job"))
                for blocked in (("platform-upgrade", {"component": "longhorn"}), ("cluster-shutdown", {}), ("os-rollout", {})):
                    with self.assertRaises(ValueError):
                        CONFLICTS.require_clear(OPS.stored(), *blocked)
                self.end("job")
                for allowed in (("platform-upgrade", {"component": "longhorn"}), ("cluster-shutdown", {}), ("os-rollout", {})):
                    CONFLICTS.require_clear(OPS.stored(), *allowed)

    def test_the_hosts_came_back_run_no_longer_waits(self):
        """The scheduled placement run starts with unless= its waited-for kinds."""
        self.save(stuck("disk-v2-convert", "removing", job_id="v2"))
        start = lambda: OPS.start("container-rebalance", "Balance hosts", {}, "/", {}, unless=("disk-v2-convert",))
        with self.assertRaises(ValueError):
            start()
        self.end("v2")
        self.assertEqual("container-rebalance", start()["kind"])

    def test_cancel_rules_are_unchanged(self):
        self.save(stuck("disk-v2-convert", "removing", job_id="v2"), stuck("disk-allocation", "growing", job_id="alloc"),
                  stuck("longhorn-runtime", "staged", job_id="rt"))
        for item in OPS.stored():
            self.assertFalse(OPS._public(item)["cancellable"], item["kind"])


class HelperJobTests(Review):
    """The helper Job a stranded erase or growth left in Homestead's namespace."""
    NAME = "homestead-disk-v2-" + "a" * 24

    def test_the_helper_job_goes_with_a_uid_precondition(self):
        self.kube.add(helper(self.NAME))
        self.save(stuck("disk-v2-convert", "preparing", job_id="v2"))
        self.assertIn(self.NAME, " ".join(self.preview("v2")["plan"]["left_in_cluster"]))
        self.end("v2")
        self.assertEqual(1, len(self.kube.writes))
        method, path, body, _ = self.kube.writes[0]
        self.assertEqual(("DELETE", f"{JOBS}/{self.NAME}"), (method, path))
        self.assertEqual({"uid": "helper-uid"}, body["preconditions"])
        self.assertEqual("Background", body["propagationPolicy"])
        self.assertIsNone(self.kube.object(f"{JOBS}/{self.NAME}"))
        self.assertEqual("deleted", self.saved("v2")["ref"]["host_gone"]["helper_job"])

    def test_a_job_of_the_same_name_with_another_identity_is_left_alone(self):
        self.kube.add(helper(self.NAME, uid="someone-else"))
        self.save(stuck("disk-v2-convert", "preparing", job_id="v2"))
        self.assertIn("left alone", " ".join(self.preview("v2")["plan"]["left_in_cluster"]))
        self.end("v2")
        self.assertEqual([], self.kube.writes)
        self.assertIsNotNone(self.kube.object(f"{JOBS}/{self.NAME}"))

    def test_no_helper_job_means_no_write(self):
        for kind, phase in (("disk-v2-convert", "preparing"), ("disk-allocation", "growing"), ("disk-allocation", "queued")):
            with self.subTest(kind=kind, phase=phase):
                self.save(stuck(kind, phase, job_id="job"))
                self.end("job")
                self.assertEqual([], self.kube.writes)

    def test_a_helper_whose_identity_was_never_saved_is_only_listed(self):
        self.kube.add(helper("homestead-allocation-" + "a" * 24))
        job = stuck("disk-allocation", "dispatching", job_id="alloc")
        del job["ref"]["job_uid"]
        self.save(job)
        self.assertIn("delete it by hand", " ".join(self.preview("alloc")["plan"]["left_in_cluster"]))
        self.end("alloc")
        self.assertEqual([], self.kube.writes)

    def test_a_refused_delete_does_not_undo_the_end(self):
        self.kube.add(helper(self.NAME))
        self.save(stuck("disk-v2-convert", "preparing", job_id="v2"))
        self.kube.fail(f"{JOBS}/{self.NAME}", 409, method="DELETE")
        result = self.end("v2")
        item = self.saved("v2")
        self.assertEqual("host-gone", item["ref"]["phase"])
        self.assertIn("HTTP 409", item["ref"]["host_gone"]["helper_job"])
        self.assertIn(self.NAME, result["detail"])


class RouteTests(Review):
    def api(self, path, body, role):
        """Through server.py's handler: authentication and the route policy are real."""
        handler = object.__new__(server.H)
        handler.path, handler.command = path, "POST"
        handler.headers = {"X-Homestead-Auth": "1"}
        handler._who = lambda: {"user": role, "role": role}
        handler._body = lambda: dict(body)
        handler._client_ip = lambda: "127.0.0.1"
        handler._send = mock.Mock()
        with mock.patch.object(server.CFACCESS, "enabled", return_value=False):
            handler.do_POST()
        return handler._send.call_args.args

    def test_both_routes_are_for_admins_only(self):
        import homestead_routes as ROUTES
        found = {path: role for (method, path), (role, _) in HOST_GONE.ROUTES.items()}
        self.assertEqual({"/api/operations/host-gone/preview": "admin", "/api/operations/host-gone/resolve": "admin"}, found)
        self.assertIn("homestead_host_gone", ROUTES.MODULES)

    def test_viewers_and_operators_are_refused(self):
        self.save(stuck("disk-v2-convert", "removing", job_id="v2"))
        for role in ("viewer", "operator"):
            for step in ("preview", "resolve"):
                with self.subTest(role=role, step=step):
                    self.assertEqual(403, self.api("/api/operations/host-gone/" + step, {"id": "v2"}, role)[0])
        self.assertEqual("removing", self.saved("v2")["ref"]["phase"])

    def test_the_routes_review_and_end_through_the_bound_disk_reader(self):
        self.save(stuck("disk-v2-convert", "removing", job_id="v2"))
        (_, preview), (_, resolve) = (HOST_GONE.ROUTES[("POST", "/api/operations/host-gone/preview")],
                                      HOST_GONE.ROUTES[("POST", "/api/operations/host-gone/resolve")])
        review = preview(SimpleNamespace(body={"id": "v2"}, user="admin"))
        body = {"id": "v2", "capacity_token": review["capacity_token"], "confirm_capacity": True, "confirm": "node-a",
                "acknowledge_host_gone": True}
        with mock.patch("homestead_routes.forget") as forget:
            self.assertTrue(resolve(SimpleNamespace(body=body, user="admin"))["ok"])
        forget.assert_called_once_with("disks", "lhcap")
        self.assertEqual("admin", self.saved("v2")["ref"]["host_gone"]["by"])


if __name__ == "__main__":
    unittest.main()
