import copy
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
import homestead_restarts as R
import homestead_shared as SHARED

T0 = 1_790_000_000
UID_PREFIX = {"ReplicaSet": "rs", "StatefulSet": "sts", "DaemonSet": "ds", "Deployment": "dep"}


def pod(name="game-6d4f-abcde", uid="pod-1", ns="apps", rs="game-6d4f", hash_="6d4f", restarts=0, last=None,
        images=None, ann=None, node="node-a", created="2026-10-22T08:00:00Z", phase="Running", reason="",
        limit="512Mi", owner_kind="ReplicaSet"):
    images = images or {"server": "example/server:1"}
    status = {"phase": phase, "containerStatuses": [
        {"name": c, "restartCount": restarts, "image": img, "state": {"running": {}},
         "lastState": {"terminated": last} if last else {}} for c, img in images.items()]}
    if reason:
        status["reason"], status["message"] = reason, "The node was low on resource: memory."
    return {"metadata": {"name": name, "uid": uid, "namespace": ns, "creationTimestamp": created,
                         "labels": {"pod-template-hash": hash_}, "annotations": ann or {},
                         "ownerReferences": [{"kind": owner_kind, "name": rs, "controller": True,
                                              "uid": f"{UID_PREFIX[owner_kind]}-{rs}"}]},
            "spec": {"nodeName": node, "containers": [
                {"name": c, "image": img, "resources": {"limits": {"memory": limit}} if limit else {}}
                for c, img in images.items()]},
            "status": status}


def oom(code=137):
    return {"reason": "OOMKilled", "exitCode": code, "finishedAt": "2026-10-22T09:00:00Z"}


class RestartRecordTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.dir = temp.name
        self.enterContext(mock.patch.object(SHARED, "DIR", self.dir))
        self.events, self.nodes, self.outage = [], [{"metadata": {"name": "node-a"},
                                                      "status": {"conditions": [{"type": "Ready", "status": "True"}]}}], {}
        self.reads, self.tails = [], []
        # The cluster's ReplicaSets, made from the pods each look lists: each
        # one controlled by the Deployment its name says, unless it is bare.
        self.bare, self.dep_uids, self.rs_items, self.rs_fail = set(), {}, [], False
        # What "now" is for reads too (records older than KEEP_DAYS are not shown).
        self.clock = [T0]
        self.enterContext(mock.patch.object(R.time, "time", lambda: self.clock[0]))

    def read(self, path):
        self.reads.append(path)
        if "/events" in path:
            return {"items": self.events}
        if path == "/api/v1/nodes":
            return {"items": self.nodes}
        if path == "/apis/apps/v1/replicasets":
            if self.rs_fail:
                raise OSError("connection refused")
            return {"items": self.rs_items}
        raise AssertionError(path)

    def tail(self, ns, pod_name, container):
        self.tails.append((ns, pod_name, container))
        return {"text": "\n".join(f"line {i}" for i in range(80))}

    def look(self, *pods, now=T0, outage=None):
        self.clock[0] = now
        names = {(p["metadata"]["namespace"], r["name"]) for p in pods for r in p["metadata"]["ownerReferences"]
                 if r["kind"] == "ReplicaSet"}
        self.rs_items = [{"metadata": {"name": rs, "namespace": ns, "uid": f"rs-{rs}", "ownerReferences": [] if rs in self.bare else [
            {"kind": "Deployment", "name": rs.rsplit("-", 1)[0], "controller": True,
             "uid": self.dep_uids.get(rs.rsplit("-", 1)[0], f"dep-{rs.rsplit('-', 1)[0]}")}]}} for ns, rs in names]
        return R.observe({"items": [copy.deepcopy(p) for p in pods]}, now=now, read=self.read, tail=self.tail,
                         outage=outage or (lambda: self.outage), sys_ns=frozenset({"kube-system"}))

    def records(self, ns="apps", name="game", kind="Deployment", uid=None):
        return R.for_app(ns, name, kind, uid or self.dep_uids.get(name) or f"{UID_PREFIX[kind]}-{name}")["records"]

    def test_the_first_look_only_takes_a_baseline(self):
        self.look(pod(restarts=8, last=oom()))
        self.assertEqual([], self.records())
        self.assertEqual(T0, R.for_app("apps", "game", "Deployment", "dep-game")["since"])

    def test_a_restart_in_place_is_recorded_with_its_reason_limit_and_log(self):
        self.look(pod(restarts=1))
        self.assertEqual(1, self.look(pod(restarts=2, last=oom()), now=T0 + 20))
        [rec] = self.records()
        self.assertEqual(("restart", "Ran out of memory", "limit 512 MiB"), (rec["kind"], rec["reason"], rec["detail"]))
        self.assertEqual(("server", "game-6d4f-abcde", 1, 137), (rec["container"], rec["pod"], rec["count"], rec["exit_code"]))
        self.assertEqual(50, len(rec["log_tail"].splitlines()), "the last 50 lines of the previous log")
        self.assertEqual("line 79", rec["log_tail"].splitlines()[-1])

    def test_exit_codes_in_words(self):
        for code, words in ((139, "Crashed (segmentation fault)"), (143, "Stopped by a signal (TERM)"),
                            (137, "Killed (signal 9)"), (1, "Exited with code 1"), (0, "Exited")):
            with self.subTest(code=code):
                self.assertEqual(words, R.reason({"reason": "Error" if code else "Completed", "exitCode": code}, []))

    def test_a_failed_liveness_check(self):
        self.events = [{"reason": "Killing", "message": "Container server failed liveness probe, will be restarted",
                        "involvedObject": {"name": "game-6d4f-abcde"}}]
        self.look(pod(restarts=0))
        self.look(pod(restarts=1, last={"reason": "Error", "exitCode": 137}), now=T0 + 20)
        self.assertEqual("Failed its liveness check", self.records()[0]["reason"])

    def test_the_host_restarting(self):
        self.look(pod(restarts=0))
        self.look(pod(restarts=1, last={"reason": "Unknown", "exitCode": 255}), now=T0 + 20)
        self.assertEqual("Its host restarted", self.records()[0]["reason"])

    def test_several_restarts_between_looks_are_one_record_that_says_so(self):
        self.look(pod(restarts=1))
        self.look(pod(restarts=4, last=oom()), now=T0 + 20)
        [rec] = self.records()
        self.assertEqual(3, rec["count"])
        self.assertIn("3 restarts", rec["note"])

    def test_the_same_counts_record_nothing_again(self):
        """A new leader reads the same store: nothing is recorded twice."""
        self.look(pod(restarts=1))
        self.look(pod(restarts=2, last=oom()), now=T0 + 20)
        self.assertEqual(0, self.look(pod(restarts=2, last=oom()), now=T0 + 40))
        self.assertEqual(1, len(self.records()))

    def test_an_eviction(self):
        old = pod(restarts=0)
        self.look(old)
        evicted = pod(phase="Failed", reason="Evicted")
        new = pod(name="game-6d4f-xyz", uid="pod-2", created="2026-10-22T09:00:00Z")
        self.look(evicted, new, now=T0 + 20)
        [rec] = self.records()
        self.assertEqual(("replaced", "Evicted"), (rec["kind"], rec["reason"]))
        self.assertIn("low on resource", rec["detail"])
        self.look(evicted, new, now=T0 + 40)
        self.assertEqual(1, len(self.records()), "said once")

    def test_a_host_going_away(self):
        self.look(pod(node="node-b"))
        self.nodes = []
        self.look(pod(name="game-6d4f-xyz", uid="pod-2", created="2026-10-22T09:00:00Z"), now=T0 + 20)
        self.assertEqual("Its host went away (node-b)", self.records()[0]["reason"])

    def test_a_pod_deleted_and_replaced(self):
        self.look(pod())
        self.look(pod(name="game-6d4f-xyz", uid="pod-2", created="2026-10-22T09:00:00Z"), now=T0 + 20)
        self.assertEqual("Pod replaced", self.records()[0]["reason"])

    def test_stopping_records_nothing(self):
        self.look(pod())
        self.look(now=T0 + 20)
        self.assertEqual([], self.records())

    def test_an_image_update(self):
        self.look(pod())
        self.look(pod(name="game-7a1b-xyz", uid="pod-2", rs="game-7a1b", hash_="7a1b", images={"server": "example/server:2"},
                      created="2026-10-22T09:00:00Z"), now=T0 + 20)
        [rec] = self.records()
        self.assertEqual(("rollout", "Updated to example/server:2"), (rec["kind"], rec["reason"]))

    def test_restarted_from_homestead_and_by_its_outage_action(self):
        self.look(pod())
        stamp = {"homestead.io/restartedAt": "2026-10-22T09:00:00Z"}
        self.look(pod(name="game-7a1b-xyz", uid="pod-2", rs="game-7a1b", hash_="7a1b", ann=stamp), now=T0 + 20)
        self.assertEqual("Restarted from Homestead", self.records()[0]["reason"])
        self.outage = {"app:apps/game": {"last_restart": 1792659660}}     # 2026-10-22T09:01:00Z
        self.look(pod(name="game-8c2d-xyz", uid="pod-3", rs="game-8c2d", hash_="8c2d",
                      ann={"homestead.io/restartedAt": "2026-10-22T09:01:00Z"}), now=T0 + 40)
        self.assertEqual("Restarted by its outage action", self.records()[0]["reason"])

    def test_a_kubectl_rollout_restart_and_other_changes(self):
        self.look(pod())
        self.look(pod(name="game-7a1b-x", uid="pod-2", rs="game-7a1b", hash_="7a1b",
                      ann={"kubectl.kubernetes.io/restartedAt": "2026-10-22T09:00:00Z"}), now=T0 + 20)
        self.assertEqual("Restarted (rollout restart)", self.records()[0]["reason"])
        self.look(pod(name="game-9e3f-x", uid="pod-3", rs="game-9e3f", hash_="9e3f",
                      ann={"kubectl.kubernetes.io/restartedAt": "2026-10-22T09:00:00Z"}), now=T0 + 40)
        self.assertEqual("Its settings changed", self.records()[0]["reason"])

    def test_a_rolling_update_is_one_record_not_one_per_pod(self):
        self.look(pod(), pod(name="game-6d4f-b", uid="pod-b"))
        new = dict(rs="game-7a1b", hash_="7a1b", images={"server": "example/server:2"}, created="2026-10-22T09:00:00Z")
        self.look(pod(name="game-7a1b-a", uid="pod-2", **new), pod(name="game-7a1b-b", uid="pod-3", **new), now=T0 + 20)
        self.assertEqual(1, len(self.records()))

    def test_statefulsets_and_system_pods(self):
        self.look(pod(name="db-0", uid="db", rs="db", owner_kind="StatefulSet"),
                  pod(name="csi-0", uid="csi", ns="kube-system", rs="csi", owner_kind="DaemonSet"))
        self.look(pod(name="db-0", uid="db", rs="db", owner_kind="StatefulSet", restarts=1, last=oom()),
                  pod(name="csi-0", uid="csi", ns="kube-system", rs="csi", owner_kind="DaemonSet", restarts=1, last=oom()),
                  now=T0 + 20)
        self.assertEqual(1, len(self.records("apps", "db", "StatefulSet")))
        self.assertEqual([], self.records("kube-system", "csi", "DaemonSet"), "system pods are not apps")

    def test_at_most_three_logs_read_per_look(self):
        many = [pod(name=f"app{i}-6d4f-x", uid=f"u{i}", rs=f"app{i}-6d4f") for i in range(5)]
        self.look(*many)
        self.look(*[pod(name=f"app{i}-6d4f-x", uid=f"u{i}", rs=f"app{i}-6d4f", restarts=1, last=oom()) for i in range(5)],
                  now=T0 + 20)
        self.assertEqual(3, len(self.tails))
        skipped = [r for i in range(5) for r in self.records("apps", f"app{i}") if r.get("log_error")]
        self.assertEqual(2, len(skipped))

    def test_kept_short(self):
        self.look(pod())
        for i in range(1, 26):
            self.look(pod(restarts=i, last=oom()), now=T0 + 20 * i)
        self.assertEqual(R.KEEP, len(self.records()))
        self.assertEqual(25, self.records()[0]["total"], "newest first")
        later = T0 + R.KEEP_DAYS * 86400 + 600
        self.look(pod(restarts=25, last=oom()), now=later)
        self.assertEqual([], self.records(), "older than KEEP_DAYS")

    def test_at_most_max_apps_the_longest_quiet_dropped_first(self):
        apps = [pod(name=f"app{i}-6d4f-x", uid=f"u{i}", rs=f"app{i}-6d4f") for i in range(3)]
        self.look(*apps)
        with mock.patch.object(R, "MAX_APPS", 2):
            for i in range(3):
                self.look(*[pod(name=f"app{j}-6d4f-x", uid=f"u{j}", rs=f"app{j}-6d4f", restarts=int(j <= i), last=oom())
                            for j in range(3)], now=T0 + 20 * (i + 1))
        self.assertEqual([], self.records("apps", "app0"))
        self.assertEqual(1, len(self.records("apps", "app2")))

    def test_a_store_that_cannot_be_read_is_not_written_over(self):
        path = os.path.join(self.dir, R.FILE)
        with open(path, "w") as handle:
            handle.write("{not json")
        self.assertEqual(0, self.look(pod(restarts=1, last=oom())))
        with open(path) as handle:
            self.assertEqual("{not json", handle.read())
        with self.assertRaises(SHARED.Unreadable):
            R.for_app("apps", "game")

    def test_nothing_new_writes_nothing(self):
        self.look(pod())
        path = os.path.join(self.dir, R.FILE)
        before = os.stat(path).st_mtime_ns
        self.look(pod(), now=T0 + 20)
        self.assertEqual(before, os.stat(path).st_mtime_ns)

    def test_recent_across_apps(self):
        self.look(pod(), pod(name="db-0", uid="db", rs="db", owner_kind="StatefulSet"))
        self.look(pod(restarts=1, last=oom()), pod(name="db-0", uid="db", rs="db", owner_kind="StatefulSet"), now=T0 + 20)
        self.look(pod(restarts=1, last=oom()), pod(name="db-0", uid="db", rs="db", owner_kind="StatefulSet",
                                                   restarts=1, last=oom(139)), now=T0 + 40)
        got = R.recent(T0 + 30)
        self.assertEqual([("apps", "db")], [(r["ns"], r["name"]) for r in got])
        self.assertEqual(2, len(R.recent(T0)))

    # Review findings: real rollouts overlap, scale-downs share timestamps,
    # replacements come a look late, and nothing slow runs under the lock.
    def test_old_and_new_pods_in_one_look_are_one_rollout(self):
        new = dict(rs="game-7a1b", hash_="7a1b", images={"server": "example/server:2"}, created="2026-10-22T09:00:00Z")
        self.look(pod(), pod(name="game-6d4f-b", uid="pod-b"))
        self.look(pod(), pod(name="game-6d4f-b", uid="pod-b"), pod(name="game-7a1b-a", uid="n1", **new), now=T0 + 20)
        self.look(pod(name="game-6d4f-b", uid="pod-b"), pod(name="game-7a1b-a", uid="n1", **new),
                  pod(name="game-7a1b-b", uid="n2", **new), now=T0 + 40)
        self.look(pod(name="game-7a1b-a", uid="n1", **new), pod(name="game-7a1b-b", uid="n2", **new), now=T0 + 60)
        self.assertEqual([("rollout", "Updated to example/server:2")], [(r["kind"], r["reason"]) for r in self.records()])

    def test_scaling_down_pods_made_in_the_same_second_records_nothing(self):
        three = [pod(name=f"game-6d4f-{i}", uid=f"p{i}") for i in range(3)]
        self.look(*three)
        self.look(*three[:2], now=T0 + 20)
        self.look(*three[:2], now=T0 + 40)
        self.assertEqual([], self.records())

    def test_a_replacement_that_turns_up_a_look_later_still_counts(self):
        self.look(pod())
        evicted = pod(phase="Failed", reason="Evicted")
        self.look(evicted, now=T0 + 20)
        self.look(evicted, pod(name="game-6d4f-xyz", uid="pod-2", created="2026-10-22T09:00:00Z"), now=T0 + 40)
        self.assertEqual(["Evicted"], [r["reason"] for r in self.records()])

    def test_a_pod_being_deleted_and_its_replacement(self):
        self.look(pod())
        going = pod()
        going["metadata"]["deletionTimestamp"] = "2026-10-22T09:00:00Z"
        self.look(going, pod(name="game-6d4f-xyz", uid="pod-2", created="2026-10-22T09:00:00Z"), now=T0 + 20)
        self.look(pod(name="game-6d4f-xyz", uid="pod-2", created="2026-10-22T09:00:00Z"), now=T0 + 40)
        self.assertEqual(["Pod replaced"], [r["reason"] for r in self.records()])

    def test_a_statefulset_rollout_by_its_revision(self):
        def sts(rev, image, uid):
            p = pod(name="db-0", uid=uid, rs="db", owner_kind="StatefulSet", images={"db": image})
            p["metadata"]["labels"] = {"controller-revision-hash": rev}
            return p
        self.look(sts("db-1", "example/db:1", "a"))
        self.look(sts("db-2", "example/db:2", "b"), now=T0 + 20)
        self.assertEqual(["Updated to example/db:2"], [r["reason"] for r in self.records("apps", "db", "StatefulSet")])

    def test_an_unreadable_outage_store_does_not_cost_the_look(self):
        self.look(pod())
        def broken():
            raise SHARED.Unreadable("outage-actions.json")
        self.outage = None
        self.look(pod(name="game-7a1b-x", uid="pod-2", rs="game-7a1b", hash_="7a1b",
                      ann={"homestead.io/restartedAt": "2026-10-22T09:00:00Z"}), now=T0 + 20, outage=broken)
        self.assertEqual(["Restarted from Homestead"], [r["reason"] for r in self.records()])

    def test_logs_and_events_are_read_outside_the_lock(self):
        held = []
        def tail(ns, name, container):
            held.append(R._lock._depth)
            return {"text": "bye"}
        def read(path):
            held.append(R._lock._depth)
            return self.read(path)
        self.look(pod())
        R.observe({"items": [pod(restarts=1, last={"reason": "Error", "exitCode": 137})]}, now=T0 + 20, read=read, tail=tail,
                  outage=lambda: {}, sys_ns=frozenset())
        self.assertTrue(held)
        self.assertEqual({0}, set(held))
        self.assertEqual("bye", self.records()[0]["log_tail"])

    # Second review: template choice, stale events, needless reads, page waits.
    def test_old_and_new_pods_made_in_the_same_second_are_one_rollout(self):
        new = dict(rs="game-7a1b", hash_="7a1b", images={"server": "example/server:2"})
        self.look(pod())
        for i in range(1, 4):
            self.look(pod(), pod(name="game-7a1b-a", uid="n1", **new), now=T0 + 20 * i)
        self.assertEqual(["Updated to example/server:2"], [r["reason"] for r in self.records()])

    def test_an_old_pod_replaced_during_a_rollout_is_not_another_rollout(self):
        new = dict(rs="game-7a1b", hash_="7a1b", images={"server": "example/server:2"}, created="2026-10-22T09:00:00Z")
        self.look(pod(), pod(name="game-6d4f-b", uid="pod-b"))
        self.look(pod(), pod(name="game-6d4f-b", uid="pod-b"), pod(name="game-7a1b-a", uid="n1", **new), now=T0 + 20)
        # pod-b drained mid-rollout: a fresh old-template pod, newest of all.
        self.look(pod(), pod(name="game-6d4f-c", uid="pod-c", created="2026-10-22T09:05:00Z"),
                  pod(name="game-7a1b-a", uid="n1", **new), now=T0 + 40)
        self.look(pod(name="game-7a1b-a", uid="n1", **new), pod(name="game-7a1b-b", uid="n2", **new), now=T0 + 60)
        self.assertEqual(["rollout"], [r["kind"] for r in self.records() if r["kind"] == "rollout"])

    def test_a_rollback_to_the_old_template_is_recorded(self):
        new = dict(rs="game-7a1b", hash_="7a1b", images={"server": "example/server:2"}, created="2026-10-22T09:00:00Z")
        self.look(pod())
        self.look(pod(name="game-7a1b-a", uid="n1", **new), now=T0 + 20)
        self.look(pod(name="game-6d4f-z", uid="r1", created="2026-10-22T10:00:00Z"), now=T0 + 40)
        self.assertEqual(["Updated to example/server:1", "Updated to example/server:2"],
                         [r["reason"] for r in self.records() if r["kind"] == "rollout"])

    def test_a_liveness_event_long_before_the_exit_is_not_its_reason(self):
        self.events = [{"reason": "Killing", "message": "Container server failed liveness probe, will be restarted",
                        "lastTimestamp": "2026-10-22T08:00:00Z", "involvedObject": {"name": "game-6d4f-abcde"}}]
        self.look(pod())
        self.look(pod(restarts=1, last={"reason": "Error", "exitCode": 137, "finishedAt": "2026-10-22T08:50:00Z"}), now=T0 + 20)
        self.assertEqual("Killed (signal 9)", self.records()[0]["reason"])

    def test_finished_evictions_left_in_the_list_do_not_read_the_hosts_every_look(self):
        self.look(pod())
        evicted = pod(phase="Failed", reason="Evicted")
        new = pod(name="game-6d4f-xyz", uid="pod-2", created="2026-10-22T09:00:00Z")
        self.look(evicted, new, now=T0 + 20)
        self.reads.clear()
        self.look(evicted, new, now=T0 + 40)
        self.assertNotIn("/api/v1/nodes", self.reads)

    def test_a_replaced_record_names_the_pod_even_when_it_is_gone(self):
        self.look(pod())
        self.look(pod(name="game-6d4f-xyz", uid="pod-2", created="2026-10-22T09:00:00Z"), now=T0 + 20)
        self.assertEqual("game-6d4f-abcde", self.records()[0]["pod"])

    # Final review: two new templates in one look; a former leader's late look.
    def test_two_new_templates_in_one_look_are_one_rollout_and_no_rollback(self):
        b = dict(rs="game-7a1b", hash_="7a1b", images={"server": "example/server:2"}, created="2026-10-22T09:00:00Z")
        c = dict(rs="game-8c2d", hash_="8c2d", images={"server": "example/server:3"}, created="2026-10-22T09:05:00Z")
        self.look(pod())
        self.look(pod(name="game-7a1b-a", uid="b1", **b), pod(name="game-8c2d-a", uid="c1", **c), now=T0 + 20)
        self.look(pod(name="game-7a1b-a", uid="b1", **b), pod(name="game-8c2d-a", uid="c1", **c), now=T0 + 40)
        self.look(pod(name="game-8c2d-a", uid="c1", **c), now=T0 + 60)
        self.assertEqual(["Updated to example/server:3"], [r["reason"] for r in self.records() if r["kind"] == "rollout"])

    def test_a_look_older_than_the_last_written_one_changes_nothing(self):
        self.look(pod())
        self.look(pod(restarts=1, last=oom()), now=T0 + 40)
        # A former leader's thread, with a pod list read before that look.
        self.assertEqual(0, self.look(pod(), now=T0 + 20))
        self.look(pod(restarts=1, last=oom()), now=T0 + 60)
        self.assertEqual(1, len(self.records()), "the restart is not recorded twice")

    # Security review: a record belongs to the workload that controls the pod,
    # by kind and uid, never to whatever shares its name.
    def test_a_statefulset_of_the_same_name_keeps_its_own_record(self):
        sts = dict(name="game-0", uid="s0", rs="game", owner_kind="StatefulSet")
        self.look(pod(), pod(**sts))
        self.look(pod(), pod(**sts, restarts=1, last=oom()), now=T0 + 20)
        self.assertEqual([], self.records())
        self.assertEqual(1, len(self.records("apps", "game", "StatefulSet")))

    def test_a_bare_replicaset_named_like_a_deployments_is_no_app(self):
        self.bare = {"game-6d4f"}
        self.look(pod())
        self.look(pod(restarts=1, last=oom()), now=T0 + 20)
        self.assertEqual([], self.records())
        self.assertEqual([], R.recent(0))

    def test_an_app_deleted_and_made_again_starts_with_no_history(self):
        self.look(pod())
        self.look(pod(restarts=1, last=oom()), now=T0 + 20)
        self.assertEqual(1, len(self.records()))
        self.dep_uids["game"] = "dep-game-2"
        # The new Deployment makes a ReplicaSet of its own (a new uid).
        self.look(pod(name="game-7a1b-new", uid="pod-9", rs="game-7a1b", hash_="7a1b",
                      images={"server": "example/server:9"}), now=T0 + 40)
        self.assertEqual([], self.records())
        self.assertEqual(1, len(self.records(uid="dep-game")), "the old app's record ages out")

    def test_replicasets_that_could_not_be_read_change_nothing(self):
        self.look(pod())
        self.rs_fail = True
        # Whose each ReplicaSet is is kept a while (#980): this look reads them again.
        later = T0 + R.OWNERS_FOR
        self.assertEqual(0, self.look(pod(restarts=1, last=oom()), now=later + 20))
        self.rs_fail = False
        self.look(pod(restarts=1, last=oom()), now=later + 40)
        self.assertEqual(1, len(self.records()), "recorded once, when it could be told whose pod it is")

    # #871: the timeline filters by host, so a record names the host it happened on.
    def test_a_restart_names_the_host_its_pod_ran_on(self):
        self.look(pod(node="node-b"))
        self.look(pod(node="node-b", restarts=1, last=oom()), now=T0 + 20)
        self.assertEqual("node-b", self.records()[0]["node"])
        self.assertEqual("node-b", R.recent(T0)[0]["node"])

    def test_a_replaced_pod_names_the_host_it_ran_on_not_the_new_one(self):
        self.look(pod(node="node-b"))
        self.look(pod(name="game-6d4f-xyz", uid="pod-2", node="node-c", created="2026-10-22T09:00:00Z"), now=T0 + 20)
        self.assertEqual(("replaced", "node-b"), (self.records()[0]["kind"], self.records()[0]["node"]))

    def test_a_rollout_names_no_host(self):
        self.look(pod())
        self.look(pod(name="game-7a1b-xyz", uid="pod-2", rs="game-7a1b", hash_="7a1b", images={"server": "example/server:2"},
                      created="2026-10-22T09:00:00Z"), now=T0 + 20)
        [rec] = self.records()
        self.assertEqual("rollout", rec["kind"])
        self.assertNotIn("node", rec, "a new template is the app's, not one host's")


class LaterTests(unittest.TestCase):
    def test_the_pages_never_wait_for_the_record_and_one_look_runs_at_a_time(self):
        import threading
        started, release, calls = threading.Event(), threading.Event(), []
        def slow(pods, now=None):
            calls.append(pods)
            started.set()
            release.wait(5)
        with mock.patch.object(R, "observe", slow):
            self.assertTrue(R.observe_later({"items": [1]}))
            self.assertTrue(started.wait(5))
            self.assertFalse(R.observe_later({"items": [2]}), "busy: this look is skipped, the next catches up")
            release.set()
            for _ in range(250):
                if not R._busy.locked():
                    break
                __import__("time").sleep(0.02)
            self.assertTrue(R.observe_later({"items": [3]}))
            for _ in range(250):          # still under the patch: never the real observe
                if len(calls) == 2 and not R._busy.locked():
                    break
                __import__("time").sleep(0.02)
        self.assertEqual([{"items": [1]}, {"items": [3]}], calls)

    def test_a_thread_that_cannot_start_does_not_stop_every_later_look(self):
        with mock.patch.object(R.threading, "Thread", side_effect=RuntimeError("can't start new thread")):
            with self.assertRaises(RuntimeError):
                R.observe_later({"items": []})
        self.assertFalse(R._busy.locked())


if __name__ == "__main__":
    unittest.main()
