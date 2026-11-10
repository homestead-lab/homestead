"""After a host restart: whether the cluster is ready for the next one (#1290).

The Hosts page says "Ready for the next host restart" once every app the last
host restart moved runs again and no volume is short of copies; the restart
review warns while that is not true, as a warning, never a block. The apps and
volumes settle by themselves after the restart: a small simulated controller
(Settling) brings them up a step at a time, as the Deployment and StatefulSet
controllers, KubeVirt and Longhorn's rebuild do."""
import sys
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import homestead_kube as K
import homestead_nodes as NODES
import homestead_operations as OPS
import homestead_power as POWER
from fakes.kube import Kube
from rebind import keep_bound

LH = "/apis/longhorn.io/v1beta2/namespaces/longhorn-system"
ROUTE = ("GET", "/api/nodes/restart-ready")


def stamp(ago):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - ago))


def node(name):
    return {"apiVersion": "v1", "kind": "Node", "metadata": {"name": name, "uid": f"uid-{name}"},
            "status": {"conditions": [{"type": "Ready", "status": "True"}], "nodeInfo": {"bootID": f"boot-{name}"}}}


def restart_job(host="node-a", ended_ago=600, status="succeeded", action="reboot", **ref):
    """A host restart job as send_reviewed_power stores it, ended."""
    plan = {"node": host, "action": action, "workloads": [{"ns": "lab", "name": "web", "stranded": False}],
            "vms": ["lab/vm1"]}
    return {"id": f"op-{host}-{ended_ago}", "kind": "node-power", "title": f"{action} {host}", "status": status,
            "finished_at": stamp(ended_ago) if status in OPS.TERMINAL else "",
            "ref": {"node": host, "action": action, "plan": plan,
                    "held": [{"id": "StatefulSet/lab/db", "kind": "StatefulSet", "ns": "lab", "name": "db", "was": 1}],
                    "restored": {"started": ["lab/db"], "left": [], "at": time.time()}, **ref}}


class Settling:
    """What runs after a host restart, by itself: each tick() the Deployment
    and StatefulSet controllers bring one more copy up, KubeVirt starts the
    VM, and Longhorn rebuilds the volume's missing copy over two ticks."""

    def __init__(self, kube):
        self.kube, self.ticks = kube, 0
        kube.add({"apiVersion": "apps/v1", "kind": "Deployment", "metadata": {"namespace": "lab", "name": "web"},
                  "spec": {"replicas": 2}, "status": {"replicas": 2, "readyReplicas": 0}})
        kube.add({"apiVersion": "apps/v1", "kind": "StatefulSet", "metadata": {"namespace": "lab", "name": "db"},
                  "spec": {"replicas": 1}, "status": {"replicas": 1, "readyReplicas": 0}})
        kube.add({"apiVersion": "kubevirt.io/v1", "kind": "VirtualMachine", "metadata": {"namespace": "lab", "name": "vm1"},
                  "spec": {"runStrategy": "Always"}})
        kube.add({"apiVersion": "kubevirt.io/v1", "kind": "VirtualMachineInstance",
                  "metadata": {"namespace": "lab", "name": "vm1"}, "status": {"phase": "Scheduling", "nodeName": "node-b"}})
        kube.add(self._volume("degraded"))

    @staticmethod
    def _volume(robustness):
        return {"apiVersion": "longhorn.io/v1beta2", "kind": "Volume",
                "metadata": {"namespace": "longhorn-system", "name": "pvc-1"},
                "spec": {"numberOfReplicas": 2},
                "status": {"state": "attached", "robustness": robustness,
                           "kubernetesStatus": {"namespace": "lab", "pvcName": "data"}}}

    def tick(self):
        self.ticks += 1
        for plural in ("deployments", "statefulsets"):
            for name in ("web", "db"):
                path = f"/apis/apps/v1/namespaces/lab/{plural}/{name}"
                obj = self.kube.object(path)
                if obj:
                    want = obj["spec"]["replicas"]
                    obj["status"]["readyReplicas"] = min(want, (obj["status"].get("readyReplicas") or 0) + 1)
                    self.kube.add(obj)
        vmi = self.kube.object("/apis/kubevirt.io/v1/namespaces/lab/virtualmachineinstances/vm1")
        vmi["status"]["phase"] = "Running"
        self.kube.add(vmi)
        if self.ticks >= 2:
            self.kube.add(self._volume("healthy"))


class RestartReadyTests(unittest.TestCase):
    def setUp(self):
        self.kube = self.enterContext(Kube(node("node-a"), node("node-b")))
        self.enterContext(mock.patch.object(NODES, "kget", K.kget))
        # The route keeps its answer 15 s; each read here is a fresh one.
        self.enterContext(mock.patch.object(NODES.ROUTER, "cached", lambda _key, _seconds, fn: fn()))
        self.jobs = [restart_job()]
        self.enterContext(mock.patch.object(OPS, "stored", lambda: list(self.jobs)))
        self.cluster = Settling(self.kube)

    def ready(self):
        return NODES.ROUTES[ROUTE][1](None)

    def test_hosts_says_ready_only_once_the_moved_apps_run_and_no_volume_is_short(self):
        state = self.ready()
        self.assertEqual("node-a", state["node"])
        self.assertIs(False, state["ready"])
        self.assertEqual(["lab/db", "lab/vm1", "lab/web"], state["not_running"])
        self.assertEqual(["lab/data"], state["short_of_copies"])
        self.cluster.tick()                # one copy of web, db and the VM up; the volume still rebuilding
        state = self.ready()
        self.assertEqual(["lab/web"], state["not_running"])
        self.assertIs(False, state["ready"])
        self.cluster.tick()
        state = self.ready()
        self.assertEqual(([], []), (state["not_running"], state["short_of_copies"]))
        self.assertIs(True, state["ready"])

    def test_a_volume_short_of_copies_alone_keeps_it_not_ready(self):
        self.cluster.tick(); self.cluster.tick()
        self.kube.add(Settling._volume("degraded"))
        state = self.ready()
        self.assertEqual([], state["not_running"])
        self.assertIs(False, state["ready"])

    def test_an_app_removed_or_stopped_on_purpose_since_is_not_waited_for(self):
        self.kube.discard("/apis/apps/v1/namespaces/lab/deployments/web")
        db = self.kube.object("/apis/apps/v1/namespaces/lab/statefulsets/db")
        db["spec"]["replicas"] = 0
        self.kube.add(db)
        vm = self.kube.object("/apis/kubevirt.io/v1/namespaces/lab/virtualmachines/vm1")
        vm["spec"]["runStrategy"] = "Halted"
        self.kube.add(vm)
        self.assertEqual([], self.ready()["not_running"])

    def test_the_newest_ended_restart_counts(self):
        self.jobs[:] = [restart_job("node-b", ended_ago=3000), restart_job("node-a", ended_ago=600),
                        restart_job("node-c", status="running")]
        self.assertEqual("node-a", self.ready()["node"])

    def test_nothing_is_said_without_a_recent_host_restart(self):
        for jobs in ([], [restart_job(ended_ago=2 * 86400)], [restart_job(status="running")],
                     [restart_job(action="poweroff")], [{"id": "x", "kind": "os-rollout", "status": "succeeded",
                                                          "finished_at": stamp(60), "ref": {}}]):
            with self.subTest(jobs=[j.get("status") for j in jobs]):
                self.jobs[:] = jobs
                self.assertEqual({"node": None}, self.ready())


class RestartReviewWarnsTests(unittest.TestCase):
    """The restart review of the next host warns while the last one has not
    settled - a warning, never a block."""

    def setUp(self):
        self.kube = self.enterContext(Kube(node("node-a"), node("node-b"), node("node-c")))
        keep_bound(self, POWER)
        POWER.bind(K.kget, lambda host: {"workloads": [], "stranded": []},
                   lambda: {"members": ["node-a", "node-b", "node-c"], "can_lose": 1}, lambda: True)
        self.jobs = [restart_job()]
        self.enterContext(mock.patch.object(OPS, "stored", lambda: list(self.jobs)))
        self.cluster = Settling(self.kube)

    def warning(self, plan):
        return [w for w in plan["warnings"] if w.startswith("The cluster is not ready for the next host restart.")]

    def test_the_review_warns_until_the_last_restart_has_settled(self):
        plan = POWER.plan("node-b", "reboot")
        [said] = self.warning(plan)
        self.assertIn("3 apps moved by the restart of node-a are not running yet: lab/db, lab/vm1, lab/web.", said)
        self.assertIn("1 volume is short of copies: lab/data.", said)
        blocked = list(plan["blockers"])
        self.cluster.tick(); self.cluster.tick()
        settled = POWER.plan("node-b", "reboot")
        self.assertEqual([], self.warning(settled))
        self.assertEqual(blocked, settled["blockers"], "the warning never blocks")

    def test_the_first_sentence_stands_alone_under_90_characters(self):
        [said] = self.warning(POWER.plan("node-b", "reboot"))
        self.assertLess(len(said.split(". ")[0]), 90)


if __name__ == "__main__":
    unittest.main()
