import json, sys, time, unittest
from unittest import mock
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import homestead_components as C
import homestead_kube as K
import homestead_lhv2_upgrade as V2
from rebind import keep_bound
from fakes.kube import Kube

K3S_CHANNELS = {"data": [{"name": "stable", "latest": "v1.33.4+k3s1"}, {"name": "latest", "latest": "v1.34.1+k3s1"},
                         {"name": "v1.31", "latest": "v1.31.12+k3s1"}, {"name": "v1.32", "latest": "v1.32.8+k3s1"},
                         {"name": "v1.33", "latest": "v1.33.4+k3s1"}, {"name": "testing", "latest": "v1.34.2-rc1+k3s1"}]}
LONGHORN = [{"tag_name": "v1.10.0-rc1", "prerelease": True}, {"tag_name": "v1.9.1"}, {"tag_name": "v1.9.0"},
            {"tag_name": "v1.8.2"}, {"tag_name": "v1.8.1"}, {"tag_name": "v1.7.3"}]


K3S_GITHUB = [{"tag_name": "v1.34.2-rc1+k3s1", "prerelease": True}, {"tag_name": "v1.34.1+k3s1"},
              {"tag_name": "v1.33.5+k3s1"}, {"tag_name": "v1.33.4+k3s2"}, {"tag_name": "v1.33.5+k3s2"},
              {"tag_name": "v1.32.9+k3s1"}, {"tag_name": "v1.32.9"}]


class ChannelFallbackTests(unittest.TestCase):
    def setUp(self):
        keep_bound(self, C, V2)

    def releases(self, answer):
        C._releases.clear()
        C.bind(None, None, None, None, None, answer)
        try:
            return C.releases("k3s", force=True)
        finally:
            C._releases.clear()

    def test_the_channel_server_names_the_newest_of_each_minor(self):
        value, error = self.releases(lambda url: K3S_CHANNELS)
        self.assertEqual(["v1.33.4+k3s1", "v1.32.8+k3s1", "v1.31.12+k3s1"], value)
        self.assertEqual("", error)

    def test_github_stands_in_when_the_channel_server_takes_no_connection(self):
        def answer(url):
            if "update.k3s.io" in url:
                raise TimeoutError("timed out")
            self.assertIn("k3s-io/k3s", url)
            return K3S_GITHUB
        value, error = self.releases(answer)
        # The newest k3s build of each minor; no test build, no bare tag.
        self.assertEqual(["v1.34.1+k3s1", "v1.33.5+k3s2", "v1.32.9+k3s1"], value)
        self.assertEqual("", error)

    def test_github_stands_in_when_the_channel_server_answers_nothing_usable(self):
        value, _ = self.releases(lambda url: {"data": []} if "update.k3s.io" in url else K3S_GITHUB)
        self.assertEqual("v1.34.1+k3s1", value[0])

    def test_neither_answering_is_said(self):
        def answer(url):
            raise TimeoutError("timed out")
        value, error = self.releases(answer)
        self.assertEqual([], value)
        self.assertIn("timed out", error)


class VersionTests(unittest.TestCase):
    def test_one_minor_at_a_time(self):
        available = ["v1.9.1", "v1.9.0", "v1.8.2", "v1.8.1", "v1.7.3"]
        self.assertEqual("v1.7.3", C.next_step("v1.7.1", available), "the newest patch of this minor first")
        self.assertEqual("v1.8.2", C.next_step("v1.7.3", available), "then the next minor, never two")
        self.assertIsNone(C.next_step("v1.9.1", available))
        self.assertEqual("v1.32.8+k3s1", C.next_step("v1.31.12+k3s1", ["v1.33.4+k3s1", "v1.32.8+k3s1"]))
        self.assertEqual("v1.31.12+k3s2", C.next_step("v1.31.12+k3s1", ["v1.31.12+k3s2"]))

    def test_test_builds_are_not_releases(self):
        self.assertIsNone(C.parse("v1.10.0-rc1"))
        self.assertEqual((1, 31, 4, 1), C.parse("v1.31.4+k3s1"))
        self.assertEqual((1, 31, 4, 2), C.parse("v1.31.4+rke2r2"))


def json_copy(value):
    return json.loads(json.dumps(value))


class Cluster:
    """The cluster on the shared fake API (tests/fakes/kube.py). The upgrade
    controller's API is there only after upgrade_api()."""
    def __init__(self, test, platform, *objects):
        self.platform, self.helm_calls = platform, []
        self.kube = test.enterContext(Kube(*objects))
        # The job store: no other job, and saving the job before a write is recorded.
        self.jobs, self.saved = [], []
        test.enterContext(mock.patch.object(C.OPS, "stored", lambda: list(self.jobs)))
        test.enterContext(mock.patch.object(C.OPS, "keep_cancel_ref", lambda item: None))
        test.enterContext(mock.patch.object(C, "_save", lambda item: self.saved.append(
            (json_copy(item["ref"]), len(self.kube.writes)))))
        C._releases.clear()
        keep_bound(test, C, V2)   # C.bind() also binds the Longhorn v2 upgrade module
        C.bind(K.kget, K.ksend, lambda force=False: self.platform, self.helm, Addons(),
               lambda url: K3S_CHANNELS if "k3s.io" in url else LONGHORN)

    @property
    def sent(self):
        return [(method, path, body) for method, path, body, _ in self.kube.writes]

    def upgrade_api(self):
        self.kube.react("GET", "/apis/upgrade.cattle.io/v1", lambda *_: {
            "kind": "APIResourceList", "groupVersion": "upgrade.cattle.io/v1", "resources": []})

    def helm(self, cfg):
        self.helm_calls.append(cfg)


class Addons:
    KUBEVIRT, CDI = "https://kv", "https://cdi"

    @staticmethod
    def fetch(url):
        if url.endswith("/latest"):
            return "", "https://github.com/rancher/system-upgrade-controller/releases/tag/v0.16.0"
        return f"kind: Thing\n# {url}\n", url

    @staticmethod
    def chart_archive(name, version, manifests, extra):
        return f"archive:{name}:{version}"

    kubevirt_cr = staticmethod(lambda emulation: f"kubevirt emulation={emulation}")
    cdi_cr = staticmethod(lambda: "cdi")


def stamp(ago=0):
    """An API timestamp this many seconds ago."""
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - ago))


def cordon(cordoned_ago=300):
    """A node's spec when cordoned: the node lifecycle controller adds the
    cordon's taint, with when (SwapNodeControllerTaint sets TimeAdded on every
    taint it adds, pkg/controller/util/node/controller_utils.go)."""
    return {"unschedulable": True, "taints": [
        {"key": "node.kubernetes.io/unschedulable", "effect": "NoSchedule", "timeAdded": stamp(cordoned_ago)}]}


def node(name, version="v1.31.12+k3s1", labels=None, unschedulable=False, ready=True, annotations=None,
         cordoned_ago=300):
    obj = {"apiVersion": "v1", "kind": "Node", "metadata": {"name": name},
           "status": {"nodeInfo": {"kubeletVersion": version},
                      "conditions": [{"type": "Ready", "status": "True" if ready else "Unknown"}]}}
    if annotations:
        obj["metadata"]["annotations"] = annotations
    if labels:
        obj["metadata"]["labels"] = labels
    if unschedulable:
        obj["spec"] = cordon(cordoned_ago)
    return obj


def job(name, plan, node_name=None, **status):
    labels = {"upgrade.cattle.io/plan": plan, **({"upgrade.cattle.io/node": node_name} if node_name else {})}
    # The controller makes each job owned by its Plan: removing the Plan removes it.
    owner = [{"apiVersion": "upgrade.cattle.io/v1", "kind": "Plan", "name": plan, "uid": f"uid-plan-{plan}"}]
    # Started before the node's cordon (300 s ago); a finished one ended after it.
    status = {"startTime": stamp(600), **({"completionTime": stamp(60)} if status.get("succeeded") or status.get("failed") else {}),
              **status}
    return {"apiVersion": "batch/v1", "kind": "Job",
            "metadata": {"namespace": C.SUC_NS, "name": name, "labels": labels, "ownerReferences": owner},
            "status": status}


def plan(name):
    return {"apiVersion": "upgrade.cattle.io/v1", "kind": "Plan",
            "metadata": {"namespace": C.SUC_NS, "name": name, "uid": f"uid-plan-{name}",
                         "labels": {"homestead.io/managed": "true"}}}


def helmchart(name, **meta):
    return {"apiVersion": "helm.cattle.io/v1", "kind": "HelmChart",
            "metadata": {"namespace": "kube-system", "name": name, **meta}, "spec": {"chart": name}}


LH_VERSION_SETTING = {"apiVersion": "longhorn.io/v1beta2", "kind": "Setting",
                      "metadata": {"namespace": "longhorn-system", "name": "current-longhorn-version"}, "value": "v1.8.1"}
KUBEVIRT_CR = {"apiVersion": "kubevirt.io/v1", "kind": "KubeVirt", "metadata": {"namespace": "kubevirt", "name": "kubevirt"}}


class K3sTests(unittest.TestCase):
    def cluster(self, nodes=None, *objects, chart=True):
        nodes = [node("node-1"), node("node-2")] if nodes is None else nodes
        self.c = Cluster(self, {"distribution": "k3s", "longhorn": True, "version": "1.31.12+k3s1"},
                         *nodes, LH_VERSION_SETTING, *([helmchart("longhorn")] if chart else []), *objects)
        return self.c

    def row(self, component):
        return next(r for r in C.report()["components"] if r["id"] == component)

    def test_the_report_names_versions_and_the_next_step(self):
        self.cluster()
        cluster, longhorn = self.row("cluster"), self.row("longhorn")
        self.assertEqual(("v1.31.12+k3s1", "v1.32.8+k3s1", "v1.33.4+k3s1", "suc", True),
                         (cluster["installed"], cluster["next"], cluster["newest"], cluster["how"], cluster["steps_left"]))
        self.assertEqual(("v1.8.1", "v1.8.2", "v1.9.1", "helmchart"),
                         (longhorn["installed"], longhorn["next"], longhorn["newest"], longhorn["how"]))

    def test_longhorn_installed_elsewhere_is_shown_not_upgraded(self):
        self.cluster(chart=False)
        self.assertEqual(("manual", ""), (self.row("longhorn")["how"], self.row("longhorn")["next"]))
        with self.assertRaisesRegex(ValueError, "installed outside Homestead"):
            C.upgrade("longhorn", "v1.8.2")

    def test_skipping_a_step_is_refused(self):
        self.cluster()
        with self.assertRaisesRegex(ValueError, "v1.9.1 would skip a step"):
            C.upgrade("longhorn", "v1.9.1")
        C.upgrade("longhorn", "v1.8.2")
        self.assertEqual([{"namespace": "longhorn-system", "name": "longhorn", "version": "1.8.2"}], self.c.helm_calls)

    def test_the_cluster_installs_the_controller_then_writes_plans_and_follows_the_nodes(self):
        self.cluster()
        result = C.upgrade("cluster", "v1.32.8+k3s1")
        self.assertIn("system-upgrade-controller", result["detail"])
        method, path, chart = self.c.sent[0]
        self.assertEqual(("POST", "homestead-system-upgrade"), (method, chart["metadata"]["name"]))
        item = {"ref": {"component": "cluster", "to": "v1.32.8+k3s1", "phase": "controller", "started": time.time()}}
        self.assertEqual("running", C.status(item)[0])
        # The controller's chart is in the cluster now (the POST made it); its API comes up.
        self.c.upgrade_api()
        status, _, message = C.status(item)
        plans = [body for m, p, body in self.c.sent if p == C.PLANS]
        self.assertEqual(["homestead-server", "homestead-agent"], [p["metadata"]["name"] for p in plans])
        self.assertEqual("rancher/k3s-upgrade", plans[0]["spec"]["upgrade"]["image"])
        self.assertEqual(["prepare", "homestead-server"], plans[1]["spec"]["prepare"]["args"])
        self.assertEqual(("running", "nodes"), (status, item["ref"]["phase"]))
        self.assertIn("0 of 2 hosts", message)
        for name in ("node-1", "node-2"):
            self.c.kube.add(node(name, "v1.32.8+k3s1"))
        self.assertEqual("succeeded", C.status(item)[0])
        self.assertIn(("DELETE", f"{C.PLANS}/homestead-server", None), self.c.sent)

    def test_the_plans_stay_until_each_job_ends_and_no_node_is_left_cordoned(self):
        # A node reports its new version when k3s restarts, before its job
        # ends; removing the Plan then removed the job, and the node stayed
        # cordoned with nothing left to uncordon it.
        labels = {"plan.upgrade.cattle.io/homestead-server": "h"}
        self.cluster([node("node-1", "v1.32.8+k3s1", labels, True), node("node-2", "v1.32.8+k3s1", labels, True)])
        running = job("apply-homestead-server-on-node-1", "homestead-server", "node-1", active=1)
        self.c.kube.add(running)
        item = {"id": "op-1", "ref": {"component": "cluster", "to": "v1.32.8+k3s1", "phase": "nodes", "held": ["node-2"]}}
        status, _, message = C.status(item)
        self.assertEqual("running", status)
        self.assertIn("job on node-1", message)
        self.assertFalse(self.c.sent)
        running["status"] = {"startTime": running["status"]["startTime"], "succeeded": 1, "completionTime": stamp(0)}
        self.c.kube.add(running)
        status, _, message = C.status(item)
        self.assertEqual("succeeded", status)
        patched = {p for m, p, _ in self.c.sent if m == "PATCH"}
        self.assertEqual({"/api/v1/nodes/node-1"}, patched, "node-2 was cordoned before the upgrade and stays so")
        self.assertIn("uncordoned node-1", message)

    def test_a_job_that_never_ends_does_not_hold_the_upgrade_forever(self):
        self.cluster([node("node-1", "v1.32.8+k3s1")], job("j", "homestead-agent", active=1))
        item = {"ref": {"component": "cluster", "to": "v1.32.8+k3s1", "phase": "nodes",
                        "versions_at": time.time() - C.JOB_FINISH_WAIT - 1}}
        self.assertEqual("succeeded", C.status(item)[0])

    def node_job(self, **status):
        self.c.upgrade_api()
        self.c.kube.add(job("apply-homestead-server-on-node-1", "homestead-server", "node-1", **status))
        return C.status({"ref": {"component": "cluster", "to": "v1.32.8+k3s1", "phase": "nodes"}})

    def test_a_server_stopping_its_own_upgrade_pod_is_retried_not_a_failure(self):
        # Seen on a real cluster: k3s-upgrade replaces the binary and stops
        # k3s, its own pod with it; the Job's next pod completed the upgrade.
        self.cluster()
        status, _, message = self.node_job(failed=1)
        self.assertEqual("running", status)
        self.assertNotIn("failed", message)

    def test_a_node_job_kubernetes_gave_up_on_fails_the_upgrade_with_what_it_said(self):
        LOG = "\n".join(["+ set -e", "[INFO]  Comparing old and new binaries", "[ERROR] no space left on device", "+ exit 1", ""])
        self.cluster()
        for name, created in (("apply-1-a", "2026-10-06T12:47:00Z"), ("apply-1-b", "2026-10-06T12:48:00Z")):
            self.c.kube.add({"apiVersion": "v1", "kind": "Pod", "metadata": {
                "namespace": C.SUC_NS, "name": name, "creationTimestamp": created,
                "labels": {"job-name": "apply-homestead-server-on-node-1"}}})
        read = []
        C.ktext = lambda path: read.append(path) or LOG
        try:
            status, _, message = self.node_job(failed=6, conditions=[
                {"type": "Failed", "status": "True", "reason": "BackoffLimitExceeded", "message": "Job has reached the specified backoff limit"}])
        finally:
            C.ktext = None
        self.assertEqual("failed", status)
        self.assertIn("node-1 failed (Job has reached the specified backoff limit)", message)
        self.assertIn("[ERROR] no space left on device", message, "what it said, not where to look")
        self.assertNotIn("+ set -e", message, "the script's trace is left out")
        self.assertIn("/pods/apply-1-b/log", read[0], "its newest pod")

    def test_a_server_restarting_mid_upgrade_is_not_a_step_of_its_own(self):
        self.cluster([])
        item = {"progress": 35, "message": "1 of 3 nodes on v1.32.8+k3s1; next k3s-1, k3s-2",
                "ref": {"component": "cluster", "to": "v1.32.8+k3s1", "phase": "nodes"}}
        self.assertEqual(("running", 35, "1 of 3 nodes on v1.32.8+k3s1; next k3s-1, k3s-2"), C.status(item))

    def test_external_upgrade_controller_is_not_taken_over(self):
        self.cluster().upgrade_api()
        with self.assertRaisesRegex(ValueError, "managed outside Homestead"):
            C.upgrade("cluster", "v1.32.8+k3s1")
        self.assertFalse(self.c.sent)

    def test_external_plan_is_neither_overwritten_nor_deleted(self):
        self.cluster(None, helmchart(C.SUC_CHART),
                     {"apiVersion": "upgrade.cattle.io/v1", "kind": "Plan",
                      "metadata": {"namespace": C.SUC_NS, "name": C.PLAN_NAMES[1]}}).upgrade_api()
        with self.assertRaisesRegex(ValueError, "not owned by Homestead"):
            C.upgrade("cluster", "v1.32.8+k3s1")
        self.assertFalse(self.c.sent, "ownership of both Plans must be checked before writing either")
        C.remove_plans()
        self.assertFalse(self.c.sent)


class ClusterUpgradeEndTests(unittest.TestCase):
    """A cluster upgrade that is cancelled, fails or runs out of time removes
    its Plans and uncordons the hosts its own upgrade jobs cordoned - only
    those, and only once a fresh read shows it is safe."""
    TARGET = "v1.32.8+k3s1"
    MARK = "homestead.io/upgrade-cordon"

    def cluster(self, nodes, *objects, jobs=()):
        self.c = Cluster(self, {"distribution": "k3s", "longhorn": True, "version": "1.31.12+k3s1"},
                         *nodes, LH_VERSION_SETTING, helmchart("longhorn"), helmchart(C.SUC_CHART),
                         plan("homestead-server"), plan("homestead-agent"), *objects)
        self.c.upgrade_api()
        self.c.jobs[:] = jobs  # the other Homestead jobs, as stored
        self.saved = self.c.saved
        return self.c

    def item(self, **ref):
        return {"id": "op-7", "ref": {"component": "cluster", "to": self.TARGET, "phase": "nodes",
                                      "started": time.time(), **ref}}

    def node(self, name):
        return self.c.kube.object(f"/api/v1/nodes/{name}")

    def cordoned(self, name):
        return bool((self.node(name).get("spec") or {}).get("unschedulable"))

    def plans_left(self):
        return [name for name in C.PLAN_NAMES if self.c.kube.object(f"{C.PLANS}/{name}")]

    def test_cancel_uncordons_the_host_its_upgrade_cordoned_and_no_other(self):
        self.cluster([node("node-1", unschedulable=True), node("node-2", unschedulable=True),
                      node("node-3", unschedulable=True)],
                     job("apply-homestead-server-on-node-1", "homestead-server", "node-1", active=1))
        # node-2 was cordoned before the upgrade; node-3 by someone, with no upgrade job there.
        item = self.item(held=["node-2"])
        message = C.cancel_run(item, {})
        self.assertEqual([], self.plans_left())
        self.assertFalse(self.cordoned("node-1"))
        self.assertNotIn(self.MARK, self.node("node-1")["metadata"].get("annotations") or {})
        self.assertTrue(self.cordoned("node-2"))
        self.assertTrue(self.cordoned("node-3"))
        self.assertIn("uncordoned node-1", message)

    def test_the_cordon_is_marked_and_recorded_before_any_node_write(self):
        self.cluster([node("node-1", unschedulable=True)],
                     job("apply-homestead-server-on-node-1", "homestead-server", "node-1",
                         failed=6, conditions=[{"type": "Failed", "status": "True", "reason": "BackoffLimitExceeded"}]))
        before = len(self.c.kube.writes)
        C.status(self.item())
        node_writes = [i for i, (m, p, _, _) in enumerate(self.c.kube.writes) if p == "/api/v1/nodes/node-1"]
        self.assertTrue(node_writes)
        first = node_writes[0]
        recorded = [ref for ref, at in self.saved if at <= first and at >= before]
        self.assertTrue(any("node-1" in (ref.get("cordoned") or {}) for ref in recorded),
                        "the job records which host it marks before it marks it")
        mark = self.c.kube.writes[first][2]
        self.assertIn({"op": "test", "path": "/spec/unschedulable", "value": True}, mark)
        self.assertTrue(any(op["op"] == "test" and op["path"] == "/metadata/uid" for op in mark))
        self.assertTrue(any(op["op"] == "test" and op["path"] == "/metadata/resourceVersion" for op in mark))

    def test_a_failed_upgrade_removes_its_plans_and_uncordons_its_host(self):
        self.cluster([node("node-1", unschedulable=True), node("node-2")],
                     job("apply-homestead-server-on-node-1", "homestead-server", "node-1",
                         failed=6, conditions=[{"type": "Failed", "status": "True", "reason": "BackoffLimitExceeded"}]))
        status, _, message = C.status(self.item())
        self.assertEqual("failed", status)
        self.assertIn("node-1 failed", message)
        self.assertIn("uncordoned node-1", message)
        self.assertEqual([], self.plans_left(), "no Plan is left to resume the upgrade with no job following it")
        self.assertFalse(self.cordoned("node-1"))

    def test_the_node_wait_has_a_deadline(self):
        self.cluster([node("node-1", self.TARGET), node("node-2", unschedulable=True)],
                     job("apply-homestead-agent-on-node-2", "homestead-agent", "node-2", active=1))
        item = self.item()
        self.assertEqual("running", C.status(item)[0])
        item["ref"]["done_at"] = time.time() - C.NODE_WAIT - 1
        status, _, message = C.status(item)
        self.assertEqual("failed", status)
        self.assertIn("node-2", message)
        self.assertEqual([], self.plans_left())
        self.assertFalse(self.cordoned("node-2"))

    def test_a_host_still_restarting_stays_cordoned_until_a_fresh_read_shows_it_ready(self):
        self.cluster([node("node-1", unschedulable=True, ready=False)],
                     job("apply-homestead-server-on-node-1", "homestead-server", "node-1",
                         failed=6, conditions=[{"type": "Failed", "status": "True", "reason": "BackoffLimitExceeded"}]))
        item = self.item()
        status, _, message = C.status(item)
        self.assertEqual("running", status, "it waits for the host instead of ending with it cordoned")
        self.assertIn("node-1", message)
        self.assertIn("not Ready", message)
        self.assertTrue(self.cordoned("node-1"))
        self.assertEqual([], self.plans_left())
        # The host comes back: the mark on it, not the deleted job, says it is ours.
        self.c.kube.add({**self.node("node-1"), "status": {"conditions": [{"type": "Ready", "status": "True"}]}})
        status, _, message = C.status(item)
        self.assertEqual("failed", status)
        self.assertIn("uncordoned node-1", message)
        self.assertFalse(self.cordoned("node-1"))

    def test_a_host_that_never_comes_back_ends_the_job_and_says_what_to_do(self):
        self.cluster([node("node-1", unschedulable=True, ready=False)],
                     job("apply-homestead-server-on-node-1", "homestead-server", "node-1",
                         failed=6, conditions=[{"type": "Failed", "status": "True", "reason": "BackoffLimitExceeded"}]))
        item = self.item()
        self.assertEqual("running", C.status(item)[0])
        item["ref"]["released_at"] = time.time() - C.RELEASE_WAIT - 1
        status, _, message = C.status(item)
        self.assertEqual("failed", status)
        self.assertIn("node-1 stays cordoned", message)
        self.assertIn("Uncordon", message)
        self.assertTrue(self.cordoned("node-1"))

    def test_cancel_leaves_a_restarting_host_cordoned_and_says_so(self):
        self.cluster([node("node-1", unschedulable=True, ready=False)],
                     job("apply-homestead-server-on-node-1", "homestead-server", "node-1", active=1))
        message = C.cancel_run(self.item(), {})
        self.assertTrue(self.cordoned("node-1"))
        self.assertIn("node-1 stays cordoned", message)
        self.assertIn("not Ready", message)
        self.assertIn("Uncordon", message)

    def test_a_host_another_maintenance_job_is_working_on_stays_cordoned(self):
        self.cluster([node("node-1", unschedulable=True)],
                     job("apply-homestead-server-on-node-1", "homestead-server", "node-1", active=1),
                     jobs=[{"id": "op-9", "kind": "host-os", "status": "running", "title": "Update node-1",
                            "ref": {"node": "node-1"}}])
        message = C.cancel_run(self.item(), {})
        self.assertTrue(self.cordoned("node-1"))
        self.assertIn("node-1 stays cordoned", message)
        self.assertIn("Update node-1", message)

    def test_a_host_it_would_not_take_is_still_named_after_a_restart_during_the_cancel(self):
        self.cluster([node("node-1", unschedulable=True)],
                     job("apply-homestead-server-on-node-1", "homestead-server", "node-1", active=1),
                     jobs=[{"id": "op-9", "kind": "host-os", "status": "running", "title": "Update node-1",
                            "ref": {"node": "node-1"}}])
        died = type("Died", (BaseException,), {})
        self.c.kube.fail(f"{C.PLANS}/{C.PLAN_NAMES[-1]}", died(), method="DELETE", applied=True)
        kept = []
        with mock.patch.object(C.OPS, "keep_cancel_ref",
                               side_effect=lambda item: kept.append(json.loads(json.dumps(item["ref"])))):
            with self.assertRaises(died):
                C.cancel_run(self.item(), {})
        # A new run, from what was saved: the Plans have gone, and their jobs with them.
        self.assertEqual([], self.plans_left())
        for name in ("apply-homestead-server-on-node-1",):
            self.c.kube.discard(f"/apis/batch/v1/namespaces/{C.SUC_NS}/jobs/{name}")
        message = C.cancel_run(self.item(**kept[-1]), {})
        self.assertTrue(self.cordoned("node-1"))
        self.assertIn("node-1 stays cordoned", message)
        self.assertIn("Update node-1", message)

    def test_a_replaced_node_keeps_its_cordon_when_the_cancel_runs_again(self):
        self.cluster([node("node-1", unschedulable=True)],
                     job("apply-homestead-server-on-node-1", "homestead-server", "node-1", active=1))
        item = self.item()
        C.cancel_run(item, {})
        # The host was rebuilt under the same name, and someone cordoned the new one.
        self.c.kube.add({**self.node("node-1"), "metadata": {"name": "node-1", "uid": "a-new-node"}, "spec": cordon(5)})
        C.cancel_run(self.item(**item["ref"]), {})
        self.assertTrue(self.cordoned("node-1"))

    def test_a_plan_made_again_since_is_not_removed_when_the_cancel_runs_again(self):
        self.cluster([node("node-1")])
        died = type("Died", (BaseException,), {})
        last = f"{C.PLANS}/{C.PLAN_NAMES[-1]}"
        self.c.kube.fail(last, died(), method="DELETE", applied=True)
        kept = []
        with mock.patch.object(C.OPS, "keep_cancel_ref", side_effect=lambda item: kept.append(json.loads(json.dumps(item["ref"])))):
            with self.assertRaises(died):
                C.cancel_run(self.item(), {})
        self.c.kube.add({**plan(C.PLAN_NAMES[-1]), "metadata": {**plan(C.PLAN_NAMES[-1])["metadata"], "uid": "a-later-upgrade"}})
        C.cancel_run(self.item(**kept[-1]), {})
        self.assertEqual("a-later-upgrade", self.c.kube.object(last)["metadata"]["uid"])

    def test_a_plan_not_ours_is_left(self):
        self.cluster([node("node-1")])
        first = f"{C.PLANS}/{C.PLAN_NAMES[0]}"
        found = self.c.kube.object(first)
        found["metadata"]["labels"] = {}
        self.c.kube.add(found)
        C.cancel_run(self.item(), {})
        self.assertIsNotNone(self.c.kube.object(first))

    def test_pausing_v2_again_does_not_undo_someone_turning_it_back_on(self):
        import homestead_lhv2_upgrade as V2
        self.cluster([node("node-1")])
        died = type("Died", (BaseException,), {})
        kept = []
        ref = {"component": "longhorn", "v2_mode": "live"}
        with mock.patch.object(C, "longhorn_version", return_value="1.13.0"), \
                mock.patch.object(V2, "_setting", return_value=({}, {"v2": "true"})), \
                mock.patch.object(V2, "_set", side_effect=died()) as first, \
                mock.patch.object(C.OPS, "keep_cancel_ref", side_effect=lambda item: kept.append(json.loads(json.dumps(item["ref"])))):
            with self.assertRaises(died):
                C.cancel_run({"id": "op-7", "ref": dict(ref)}, {})
        self.assertEqual(1, first.call_count)
        with mock.patch.object(C, "longhorn_version", return_value="1.13.0"), \
                mock.patch.object(V2, "_setting", return_value=({}, {"v2": "true"})), \
                mock.patch.object(V2, "_set") as second:
            message = C.cancel_run({"id": "op-7", "ref": kept[-1]}, {})
        second.assert_not_called()
        self.assertIn("on", message)
        self.assertIn("check", message.lower())

    def test_a_conflict_is_retried_from_a_fresh_read(self):
        self.cluster([node("node-1", unschedulable=True)],
                     job("apply-homestead-server-on-node-1", "homestead-server", "node-1", active=1))
        self.c.kube.fail("/api/v1/nodes/node-1", 409, times=2, method="PATCH")
        message = C.cancel_run(self.item(), {})
        self.assertFalse(self.cordoned("node-1"))
        self.assertIn("uncordoned node-1", message)

    def test_a_cordon_changed_between_read_and_write_is_not_undone(self):
        self.cluster([node("node-1", unschedulable=True)],
                     job("apply-homestead-server-on-node-1", "homestead-server", "node-1", active=1))
        item = self.item()
        C.cancel_run(item, {})  # marks and releases node-1
        # Someone cordons it again and another job marks it: ours no longer.
        self.c.kube.add({**self.node("node-1"), "spec": cordon(5)})
        current = self.node("node-1")
        current["metadata"]["annotations"] = {self.MARK: "op-8"}
        self.c.kube.add(current)
        C.cancel_run(item, {})
        self.assertTrue(self.cordoned("node-1"))

    def test_a_node_that_no_longer_exists_counts_as_released(self):
        self.cluster([node("node-1")])
        item = self.item(phase="releasing", outcome=["failed", 40, "the upgrade job on node-9 failed"],
                         released_at=time.time(), cordoned={"node-9": "uid-gone"})
        status, _, message = C.status(item)
        self.assertEqual("failed", status)
        self.assertNotIn("stays cordoned", message)

    def test_a_mark_left_by_an_ended_job_is_stale_and_taken_over(self):
        self.cluster([node("node-1", unschedulable=True, annotations={self.MARK: "op-3"})],
                     job("apply-homestead-server-on-node-1", "homestead-server", "node-1", active=1),
                     jobs=[{"id": "op-3", "kind": "platform-upgrade", "status": "cancelled",
                            "ref": {"component": "cluster"}}])
        message = C.cancel_run(self.item(), {})
        self.assertFalse(self.cordoned("node-1"), "op-3 has ended: its mark holds nothing")
        self.assertIn("uncordoned node-1", message)

    def test_a_mark_of_a_job_still_running_is_not_taken(self):
        self.cluster([node("node-1", unschedulable=True, annotations={self.MARK: "op-3"})],
                     job("apply-homestead-server-on-node-1", "homestead-server", "node-1", active=1),
                     jobs=[{"id": "op-3", "kind": "platform-upgrade", "status": "running",
                            "ref": {"component": "kubevirt"}}])
        C.cancel_run(self.item(), {})
        self.assertTrue(self.cordoned("node-1"))

    def test_a_host_cordoned_after_the_first_claim_is_marked_and_released(self):
        # The controller cordons in its job's pod; that pod can outlive the Plan.
        pod = {"apiVersion": "v1", "kind": "Pod", "metadata": {
            "namespace": C.SUC_NS, "name": "apply-node-1-x",
            "labels": {"upgrade.cattle.io/plan": "homestead-server", "upgrade.cattle.io/node": "node-1"}},
            "spec": {"nodeName": "node-1"}, "status": {"phase": "Running", "startTime": stamp(60)}}
        self.cluster([node("node-1"), node("node-2", unschedulable=True)],
                     job("apply-homestead-agent-on-node-2", "homestead-agent", "node-2",
                         failed=6, conditions=[{"type": "Failed", "status": "True", "reason": "BackoffLimitExceeded"}]),
                     pod)
        item = self.item()
        status, _, message = C.status(item)
        self.assertEqual("running", status, "an upgrade pod is still going: the job waits for it")
        self.assertIn("node-1", message)
        self.c.kube.add({**self.node("node-1"), "spec": cordon(5)})
        self.assertEqual("running", C.status(item)[0])
        self.assertEqual("op-7", self.node("node-1")["metadata"]["annotations"][self.MARK])
        self.c.kube.discard(f"/api/v1/namespaces/{C.SUC_NS}/pods/apply-node-1-x")
        status, _, message = C.status(item)
        self.assertEqual("failed", status)
        self.assertFalse(self.cordoned("node-1"))
        self.assertFalse(self.cordoned("node-2"))

    def test_a_recently_started_upgrade_pod_counts_as_progress(self):
        self.cluster([node("node-1", self.TARGET), node("node-2", unschedulable=True)],
                     job("apply-homestead-agent-on-node-2", "homestead-agent", "node-2", active=1),
                     {"apiVersion": "v1", "kind": "Pod", "metadata": {
                         "namespace": C.SUC_NS, "name": "apply-node-2-y",
                         "labels": {"upgrade.cattle.io/plan": "homestead-agent", "upgrade.cattle.io/node": "node-2"}},
                      "spec": {"nodeName": "node-2"}, "status": {"phase": "Running"}})
        item = self.item(done=1, done_at=time.time() - C.NODE_WAIT - 1)
        self.assertEqual("running", C.status(item)[0], "a pod started just now is progress")
        self.assertEqual("nodes", item["ref"]["phase"])
        self.assertEqual(["homestead-server", "homestead-agent"], self.plans_left())

    def test_the_stall_names_the_host_mid_upgrade(self):
        self.cluster([node("node-1", self.TARGET), node("node-2", unschedulable=True)],
                     job("apply-homestead-agent-on-node-2", "homestead-agent", "node-2", active=1))
        item = self.item(done=1, done_at=time.time() - C.NODE_WAIT - 1)
        status, _, message = C.status(item)
        self.assertEqual("failed", status)
        self.assertIn("node-2 was mid-upgrade", message)

    def test_a_host_cordoned_before_the_upgrade_pod_started_is_never_claimed(self):
        # A person or another job cordoned it an hour ago; the upgrade job came later.
        self.cluster([node("node-1", unschedulable=True, cordoned_ago=3600)],
                     job("apply-homestead-server-on-node-1", "homestead-server", "node-1", active=1))
        message = C.cancel_run(self.item(), {})
        self.assertTrue(self.cordoned("node-1"))
        self.assertNotIn(self.MARK, self.node("node-1")["metadata"].get("annotations") or {})
        self.assertIn("node-1 stays cordoned", message)

    def test_a_cordon_with_no_time_to_go_by_is_not_claimed(self):
        bare = node("node-1")
        bare["spec"] = {"unschedulable": True}
        self.cluster([bare], job("apply-homestead-server-on-node-1", "homestead-server", "node-1", active=1))
        C.cancel_run(self.item(), {})
        self.assertTrue(self.cordoned("node-1"))

    @staticmethod
    def upgrade_pod(node_name, exit_code, phase="Running"):
        """An upgrade pod as the system-upgrade-controller makes it for a Plan
        with cordon: true - its init container "cordon" runs kubectl cordon on
        the host - shaped like a real Pod's status."""
        return {"apiVersion": "v1", "kind": "Pod", "metadata": {
            "namespace": C.SUC_NS, "name": f"apply-homestead-server-on-{node_name}-x1",
            "creationTimestamp": stamp(400),
            "labels": {"upgrade.cattle.io/plan": "homestead-server", "upgrade.cattle.io/node": node_name,
                       "job-name": f"apply-homestead-server-on-{node_name}"}},
            "spec": {"nodeName": node_name,
                     "initContainers": [{"name": "cordon", "args": ["cordon", node_name]}],
                     "containers": [{"name": "upgrade"}]},
            "status": {"phase": phase, "startTime": stamp(400),
                       "initContainerStatuses": [{"name": "cordon", "ready": exit_code == 0, "restartCount": 0,
                                                  "state": {"terminated": {
                                                      "exitCode": exit_code, "reason": "Completed" if exit_code == 0 else "Error",
                                                      "startedAt": stamp(395), "finishedAt": stamp(390)}}}],
                       "containerStatuses": [{"name": "upgrade", "ready": phase == "Running", "restartCount": 0,
                                              "state": {"running": {"startedAt": stamp(385)}} if phase == "Running" else
                                              {"terminated": {"exitCode": 0 if phase == "Succeeded" else 1,
                                                              "startedAt": stamp(385), "finishedAt": stamp(120)}}}]}}

    @staticmethod
    def timeless(name):
        """A cordoned node whose taint carries no timeAdded."""
        bare = node(name)
        bare["spec"] = {"unschedulable": True, "taints": [{"key": "node.kubernetes.io/unschedulable", "effect": "NoSchedule"}]}
        return bare

    def test_a_succeeded_upgrade_pods_cordon_step_is_no_evidence(self):
        # The controller uncordons a host itself once its job completes, so a
        # cordon there now - here a person's, its taint not yet stamped - is not ours.
        self.cluster([self.timeless("node-1")],
                     job("apply-homestead-server-on-node-1", "homestead-server", "node-1", succeeded=1),
                     self.upgrade_pod("node-1", 0, phase="Succeeded"))
        message = C.cancel_run(self.item(), {})
        self.assertTrue(self.cordoned("node-1"))
        self.assertNotIn(self.MARK, self.node("node-1")["metadata"].get("annotations") or {})
        self.assertIn("node-1 stays cordoned: it was not cordoned by this upgrade's own job", message)

    def test_a_failed_upgrade_pods_cordon_step_still_counts(self):
        # A failed job's cordon is left in place by the controller: it is ours to undo.
        self.cluster([self.timeless("node-1")],
                     job("apply-homestead-server-on-node-1", "homestead-server", "node-1",
                         failed=6, conditions=[{"type": "Failed", "status": "True", "reason": "BackoffLimitExceeded"}]),
                     self.upgrade_pod("node-1", 0, phase="Failed"))
        self.assertEqual({}, C._claim(self.item(), lambda _item: None))
        self.assertEqual("op-7", self.node("node-1")["metadata"]["annotations"][self.MARK])

    def test_the_plans_cordon_and_never_drain(self):
        # The fallback looks for the init container "cordon"; with drain: set
        # the controller runs one named "drain" instead (job.go), and it would miss.
        for distribution in ("k3s", "rke2"):
            for body in C.plan_bodies(distribution, self.TARGET):
                self.assertIs(True, body["spec"]["cordon"])
                self.assertNotIn("drain", body["spec"])

    def test_a_cordon_taint_without_its_time_is_claimed_by_the_upgrade_pods_cordon_step(self):
        # The taint has no timeAdded (added by hand, or by the kubelet when the
        # node registered cordoned): the pod's own cordon step is the evidence.
        bare = node("node-1")
        bare["spec"] = {"unschedulable": True, "taints": [{"key": "node.kubernetes.io/unschedulable", "effect": "NoSchedule"}]}
        self.cluster([bare], job("apply-homestead-server-on-node-1", "homestead-server", "node-1", active=1),
                     self.upgrade_pod("node-1", 0))
        item = self.item()
        self.assertEqual({}, C._claim(item, lambda _item: None))
        self.assertEqual("op-7", self.node("node-1")["metadata"]["annotations"][self.MARK])
        # Its pod goes with its job once the Plans are removed.
        self.c.kube.discard(f"/api/v1/namespaces/{C.SUC_NS}/pods/apply-homestead-server-on-node-1-x1")
        message = C.cancel_run(item, {})
        self.assertFalse(self.cordoned("node-1"))
        self.assertIn("uncordoned node-1", message)

    def test_a_cordon_step_that_failed_is_no_evidence(self):
        bare = node("node-1")
        bare["spec"] = {"unschedulable": True, "taints": [{"key": "node.kubernetes.io/unschedulable", "effect": "NoSchedule"}]}
        self.cluster([bare], job("apply-homestead-server-on-node-1", "homestead-server", "node-1", active=1),
                     self.upgrade_pod("node-1", 1))
        message = C.cancel_run(self.item(), {})
        self.assertTrue(self.cordoned("node-1"))
        self.assertIn("node-1 stays cordoned: it was not cordoned by this upgrade's own job", message)

    def test_a_host_whose_cordoning_job_was_removed_after_its_ttl_stays_cordoned(self):
        # The job that cordoned node-1 20 minutes ago failed and the controller
        # removed it 900 s after it ended (ttlSecondsAfterFinished); the job it
        # made again started after the cordon, so nothing left ties the cordon to us.
        again = job("apply-homestead-server-on-node-1", "homestead-server", "node-1", active=1)
        again["status"]["startTime"] = stamp(30)
        self.cluster([node("node-1", unschedulable=True, cordoned_ago=1200)], again)
        message = C.cancel_run(self.item(), {})
        self.assertTrue(self.cordoned("node-1"))
        self.assertNotIn(self.MARK, self.node("node-1")["metadata"].get("annotations") or {})
        self.assertIn("node-1 stays cordoned: it was not cordoned by this upgrade's own job", message)

    def test_a_finished_job_justifies_only_a_cordon_made_while_it_ran(self):
        late = job("apply-homestead-server-on-node-1", "homestead-server", "node-1", succeeded=1)
        late["status"]["completionTime"] = stamp(900)  # ended before the cordon (300 s ago)
        late["status"]["startTime"] = stamp(1200)
        self.cluster([node("node-1", unschedulable=True)], late)
        C.cancel_run(self.item(), {})
        self.assertTrue(self.cordoned("node-1"))

    def test_a_host_cordoned_by_hand_while_the_upgrade_prepares_stays_cordoned(self):
        # #1330: the agent Plan's prepare step holds its pod for minutes before
        # the cordon step; a person cordons the host then; the job fails later.
        suc = UpgradeController(self, "node-1", "homestead-agent")
        suc.start()                        # prepare running, cordon waiting
        self.c.kube.add({**self.node("node-1"), "spec": cordon(300)})  # a person, 300 s ago
        suc.cordon_step(ago=200)           # "already cordoned": no new taint
        suc.fail(ago=60)
        message = C.cancel_run(self.item(), {})
        self.assertTrue(self.cordoned("node-1"))
        self.assertNotIn(self.MARK, self.node("node-1")["metadata"].get("annotations") or {})
        self.assertIn("node-1 stays cordoned: it was not cordoned by this upgrade's own job", message)

    def test_a_host_cordoned_by_hand_before_the_cordon_step_starts_is_not_claimed(self):
        # The upgrade ends while its pod still prepares: its cordon step never ran.
        suc = UpgradeController(self, "node-1", "homestead-agent")
        suc.start()
        self.c.kube.add({**self.node("node-1"), "spec": cordon(300)})
        self.assertEqual({"node-1": "it was not cordoned by this upgrade's own job"},
                         C._claim(self.item(), lambda _item: None))
        self.assertNotIn(self.MARK, self.node("node-1")["metadata"].get("annotations") or {})

    def test_a_host_the_cordon_step_cordoned_is_released(self):
        suc = UpgradeController(self, "node-1", "homestead-agent")
        suc.start()
        suc.cordon_step(ago=200)           # the controller's own cordon
        suc.fail(ago=60)
        message = C.cancel_run(self.item(), {})
        self.assertFalse(self.cordoned("node-1"))
        self.assertIn("uncordoned node-1", message)

    def test_a_cordon_a_moment_before_the_step_start_by_another_clock_still_counts(self):
        # The taint's time comes from the control plane, the step's from the
        # host's kubelet: a clock a little behind is still the step's own cordon.
        suc = UpgradeController(self, "node-1", "homestead-agent")
        suc.start()
        suc.cordon_step(ago=200, taint_ago=200 + C.CORDON_CLOCK_SKEW - 1)
        suc.fail(ago=60)
        C.cancel_run(self.item(), {})
        self.assertFalse(self.cordoned("node-1"))

    def test_a_v2_clean_up_on_the_host_blocks_its_release(self):
        self.cluster([node("node-1", unschedulable=True)],
                     job("apply-homestead-server-on-node-1", "homestead-server", "node-1", active=1))
        item = self.item()
        C._claim(item, lambda _item: None)  # marked while the upgrade ran
        self.c.jobs[:] = [{"id": "op-5", "kind": "longhorn-v2-cleanup", "status": "running",
                           "title": "Clean up Longhorn V2", "ref": {}}]
        message = C.cancel_run(item, {})
        self.assertTrue(self.cordoned("node-1"))
        self.assertIn("Clean up Longhorn V2", message)

    def test_cancel_says_it_uncordons(self):
        self.cluster([node("node-1")])
        plan_ = C.cancel_plan(self.item())
        self.assertTrue(any("uncordon" in line.lower() for line in plan_["undo"]))


class UpgradeController:
    """A small system-upgrade-controller for one host and Plan: it makes the
    job and its pod, runs the pod's init containers in order (prepare, then
    cordon - kubectl cordon, which adds the cordon only when the host is not
    cordoned already) and fails the job, writing each step to the fake API
    the way the controller and the kubelet do. It also builds the cluster."""

    def __init__(self, test, node_name, plan_name):
        self.node_name, self.plan_name = node_name, plan_name
        self.job_name = f"apply-{plan_name}-on-{node_name}"
        self.pod_name, self.ran_cordon = f"{self.job_name}-x1", None
        test.cluster([node(node_name)])
        self.kube = test.c.kube

    def _pod(self, phase, prepare, cordon_state):
        return {"apiVersion": "v1", "kind": "Pod", "metadata": {
            "namespace": C.SUC_NS, "name": self.pod_name, "creationTimestamp": stamp(600),
            "labels": {"upgrade.cattle.io/plan": self.plan_name, "upgrade.cattle.io/node": self.node_name,
                       "job-name": self.job_name}},
            "spec": {"nodeName": self.node_name,
                     "initContainers": [{"name": "prepare"}, {"name": "cordon", "args": ["cordon", self.node_name]}],
                     "containers": [{"name": "upgrade"}]},
            "status": {"phase": phase, "startTime": stamp(600), "initContainerStatuses": [
                {"name": "prepare", "ready": False, "restartCount": 0, "state": prepare},
                {"name": "cordon", "ready": False, "restartCount": 0, "state": cordon_state}]}}

    def _prepared(self):
        return {"terminated": {"exitCode": 0, "reason": "Completed", "startedAt": stamp(595),
                               "finishedAt": stamp(self.ran_cordon + 1)}}

    def start(self):
        self.kube.add(job(self.job_name, self.plan_name, self.node_name, active=1))
        self.kube.add(self._pod("Pending", {"running": {"startedAt": stamp(595)}},
                                {"waiting": {"reason": "PodInitializing"}}))

    def cordon_step(self, ago, taint_ago=None):
        self.ran_cordon = ago
        self.kube.add(self._pod("Pending", self._prepared(), {"running": {"startedAt": stamp(ago)}}))
        host = self.kube.object(f"/api/v1/nodes/{self.node_name}")
        if not (host.get("spec") or {}).get("unschedulable"):
            self.kube.add({**host, "spec": cordon(ago - 1 if taint_ago is None else taint_ago)})

    def fail(self, ago):
        """The cordon step done, the upgrade container failed, and the job with it."""
        cordoned = {"terminated": {"exitCode": 0, "reason": "Completed", "startedAt": stamp(self.ran_cordon),
                                   "finishedAt": stamp(self.ran_cordon - 1)}}
        self.kube.add(self._pod("Failed", self._prepared(), cordoned))
        failed = job(self.job_name, self.plan_name, self.node_name, failed=1,
                     conditions=[{"type": "Failed", "status": "True", "reason": "BackoffLimitExceeded",
                                  "lastTransitionTime": stamp(ago)}])
        failed["status"].pop("completionTime", None)  # a failed Job has none
        self.kube.add(failed)


class UpgradeCheckWiringTests(unittest.TestCase):
    """The report carries each upgrade's check and the pairs running now; an
    upgrade the check blocks needs the review's tick (homestead_upgrade_check)."""

    def setUp(self):
        self.c = Cluster(self, {"distribution": "k3s", "kubevirt": True, "version": "1.36.4+k3s1"},
                         node("node-1", "v1.36.4+k3s1"), helmchart("homestead-kubevirt", labels={"homestead.io/managed": "true"}),
                         dict(KUBEVIRT_CR, status={"observedKubeVirtVersion": "v1.9.1", "phase": "Deployed"}))
        channels = {"data": [{"name": "v1.36", "latest": "v1.36.4+k3s1"}, {"name": "v1.37", "latest": "v1.37.1+k3s1"}]}
        C.bind(K.kget, K.ksend, lambda force=False: self.c.platform, self.c.helm, Addons(),
               lambda url: channels if "k3s.io" in url else [{"tag_name": "v1.9.1"}])

    def test_the_report_carries_the_check_and_the_running_pair(self):
        report = C.report()
        cluster = next(r for r in report["components"] if r["id"] == "cluster")
        self.assertEqual("v1.37.1+k3s1", cluster["next"])
        self.assertEqual("blocked", cluster["check"]["verdict"])
        self.assertEqual([("KubeVirt 1.9 on Kubernetes 1.36", "supported")],
                         [(p["title"], p["status"]) for p in report["pairs"]])

    def test_a_blocked_upgrade_needs_the_tick(self):
        with self.assertRaisesRegex(ValueError, "confirm that what it breaks"):
            C.upgrade("cluster", "v1.37.1+k3s1")
        self.assertEqual([], self.c.sent, "nothing was written")
        self.assertEqual("v1.37.1+k3s1", C.upgrade("cluster", "v1.37.1+k3s1", {"accept_check": True})["to"])


class HarvesterTests(unittest.TestCase):
    def test_harvesters_own_parts_are_shown_and_left_to_harvester(self):
        Cluster(self, {"distribution": "harvester", "harvester": True, "longhorn": True, "kubevirt": True},
                LH_VERSION_SETTING, dict(KUBEVIRT_CR, status={"observedKubeVirtVersion": "v1.4.0", "phase": "Deployed"}))
        rows = {r["id"]: r for r in C.report()["components"]}
        self.assertEqual(("harvester", "v1.8.1", ""), (rows["longhorn"]["how"], rows["longhorn"]["installed"],
                                                       rows["longhorn"]["next"]))
        self.assertEqual("v1.4.0", rows["kubevirt"]["installed"])
        with self.assertRaisesRegex(ValueError, "upgraded with it"):
            C.upgrade("longhorn", "v1.8.2")

    def test_only_an_offered_version_is_started(self):
        c = Cluster(self, {"harvester": True})
        with self.assertRaisesRegex(ValueError, "does not offer v1.6.0"):
            C.start_harvester("v1.6.0", [{"version": "v1.5.1"}])
        C.start_harvester("v1.5.1", [{"version": "v1.5.1"}])
        method, path, body = c.sent[0]
        self.assertEqual(("POST", {"version": "v1.5.1"}), (method, body["spec"]))


if __name__ == "__main__":
    unittest.main()
