import sys, time, unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import homestead_components as C
import homestead_kube as K
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


class Cluster:
    """The cluster on the shared fake API (tests/fakes/kube.py). The upgrade
    controller's API is there only after upgrade_api()."""
    def __init__(self, test, platform, *objects):
        self.platform, self.helm_calls = platform, []
        self.kube = test.enterContext(Kube(*objects))
        C._releases.clear()
        test.addCleanup(C.bind, *(getattr(C, name, None) for name in (
            "kget", "ksend", "platform", "helm_upgrade", "addons", "fetch_json", "ktext")))
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


def node(name, version="v1.31.12+k3s1", labels=None, unschedulable=False):
    obj = {"apiVersion": "v1", "kind": "Node", "metadata": {"name": name},
           "status": {"nodeInfo": {"kubeletVersion": version}}}
    if labels:
        obj["metadata"]["labels"] = labels
    if unschedulable:
        obj["spec"] = {"unschedulable": True}
    return obj


def job(name, plan, node_name=None, **status):
    labels = {"upgrade.cattle.io/plan": plan, **({"upgrade.cattle.io/node": node_name} if node_name else {})}
    return {"apiVersion": "batch/v1", "kind": "Job",
            "metadata": {"namespace": C.SUC_NS, "name": name, "labels": labels}, "status": status}


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
        self.assertIn("0 of 2 nodes", message)
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
        item = {"ref": {"component": "cluster", "to": "v1.32.8+k3s1", "phase": "nodes", "held": ["node-2"]}}
        status, _, message = C.status(item)
        self.assertEqual("running", status)
        self.assertIn("job on node-1", message)
        self.assertFalse(self.c.sent)
        running["status"] = {"succeeded": 1}
        self.c.kube.add(running)
        status, _, message = C.status(item)
        self.assertEqual("succeeded", status)
        patched = [p for m, p, _ in self.c.sent if m == "PATCH"]
        self.assertEqual(["/api/v1/nodes/node-1"], patched, "node-2 was cordoned before the upgrade and stays so")
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
        with self.assertRaisesRegex(ValueError, "tick"):
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
