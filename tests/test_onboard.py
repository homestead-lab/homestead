"""The guide to adding a host, and removing a dead one."""
import contextlib
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
import homestead_kube as K
import homestead_onboard as onboard
import homestead_operations as OPS
from fakes.kube import Kube

NODES = "/api/v1/nodes"
MACHINES = "/apis/cluster.x-k8s.io/v1beta1/namespaces/fleet-local/machines"
LONGHORN = "/apis/longhorn.io/v1beta2/namespaces/longhorn-system"


def node(name, ready=True, roles=(), machine=""):
    labels = {f"node-role.kubernetes.io/{r}": "true" for r in roles}
    annotations = {"cluster.x-k8s.io/machine": machine, "cluster.x-k8s.io/cluster-namespace": "fleet-local"} if machine else {}
    return {"apiVersion": "v1", "kind": "Node", "metadata": {"name": name, "labels": labels, "annotations": annotations},
            "status": {"conditions": [{"type": "Ready", "status": "True" if ready else "Unknown",
                                       "lastTransitionTime": "2026-09-20T10:00:00Z"}],
                       "addresses": [{"type": "InternalIP", "address": "192.0.2.51"}]}}


def machine(name, node_name, phase="Running", **meta):
    return {"apiVersion": "cluster.x-k8s.io/v1beta1", "kind": "Machine",
            "metadata": {"name": name, "namespace": "fleet-local", **meta},
            "status": {"nodeRef": {"name": node_name}, "phase": phase}}


def longhorn_node(name):
    return {"apiVersion": "longhorn.io/v1beta2", "kind": "Node", "metadata": {"name": name, "namespace": "longhorn-system"}}


def replica(name, node_name, volume, state="running"):
    return {"apiVersion": "longhorn.io/v1beta2", "kind": "Replica", "metadata": {"name": name, "namespace": "longhorn-system"},
            "spec": {"nodeID": node_name, "volumeName": volume}, "status": {"currentState": state}}


def setting(name, value):
    return {"apiVersion": "harvesterhci.io/v1beta1", "kind": "Setting", "metadata": {"name": name}, "value": value}


class Cluster:
    """Three control-plane hosts, their machines and Longhorn nodes, on the
    shared fake API (tests/fakes/kube.py)."""

    def __init__(self, test):
        self.kube = test.enterContext(Kube(
            node("node1", roles=("control-plane", "etcd"), machine="custom-1"),
            node("node2", roles=("control-plane", "etcd"), machine="custom-2"),
            node("node3", roles=("control-plane", "etcd"), machine="custom-3"),
            {"apiVersion": "v1", "kind": "ConfigMap", "metadata": {"name": "vip", "namespace": "harvester-system"},
             "data": {"ip": "192.0.2.240"}},
            setting("server-version", "v1.4.1"),
            *[machine(f"custom-{i}", f"node{i}") for i in (1, 2, 3)],
            *[longhorn_node(f"node{i}") for i in (1, 2, 3)]))
        # The shared fake runs the real write guards; the storage guard takes the job store's lock under DATA_DIR.
        test.enterContext(mock.patch.object(OPS, "DATA_DIR", test.enterContext(tempfile.TemporaryDirectory())))

    @property
    def sent(self):
        return [(method, path) for method, path, _, _ in self.kube.writes]

    def edit(self, path, change):
        """Change an object as it stands, as something outside Homestead would."""
        obj = self.kube.object(path)
        change(obj)
        self.kube.add(obj)

    def names(self, path):
        return [item["metadata"]["name"] for item in K.kget(path)["items"]]

    @property
    def machines(self):
        return self.names(MACHINES)

    @property
    def lh_nodes(self):
        return self.names(f"{LONGHORN}/nodes")


class Ops:
    TERMINAL = OPS.TERMINAL

    def __init__(self):
        self.started = []

    def stored(self):
        return []   # no saved jobs

    def transaction(self):
        return contextlib.nullcontext()

    def start(self, kind, title, resource, href, ref, message=""):
        self.started.append((kind, title))
        return {"id": "op1"}


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cluster = Cluster(self)
        self.ops = Ops()
        # onboard reads the cluster through the shared client here, whatever another test bound.
        self.enterContext(mock.patch.multiple(onboard, kget=onboard.kget, ksend=onboard.ksend, NS=onboard.NS, OPS=onboard.OPS))
        onboard.bind(K.kget, K.ksend, "lab", self.ops)


class GuideTests(Base):
    def test_the_guide_answers_from_the_cluster(self):
        self.cluster.kube.add(setting("ntp-servers", '{"ntpServers":["0.uk.pool.ntp.org","1.uk.pool.ntp.org"]}'))
        g = onboard.guide()

        self.assertEqual("1.4.1", g["version"])
        self.assertEqual("https://releases.rancher.com/harvester/v1.4.1/harvester-v1.4.1-amd64.iso", g["iso"])
        self.assertEqual("https://releases.rancher.com/harvester/v1.4.1/harvester-v1.4.1-amd64.sha512", g["checksums"])
        self.assertEqual("192.0.2.240", g["vip"])
        self.assertEqual(["0.uk.pool.ntp.org", "1.uk.pool.ntp.org"], g["ntp"])
        self.assertEqual("node4", g["hostname"])
        self.assertEqual(3, g["management_count"])
        self.assertEqual("192.0.2.51", g["token_host"])
        self.assertEqual("sudo grep '^token:' /etc/rancher/rancherd/config.yaml", g["token_command"])

    def test_the_guide_never_reads_the_token(self):
        onboard.guide()
        self.assertFalse(any("secret" in path for _, path in self.cluster.sent))
        self.assertNotIn("token", {k for k in onboard.guide() if not k.startswith("token_")})

    def test_the_next_name_follows_the_pattern(self):
        self.assertEqual("harvester-04", onboard.next_hostname(["harvester-01", "harvester-02", "harvester-03"]))
        self.assertEqual("node3", onboard.next_hostname(["node1", "node2", "witness"]))
        self.assertEqual("", onboard.next_hostname(["alpha"]))

    def test_join_plans_left_by_an_older_release_are_deleted_with_their_tokens(self):
        for name in ("homestead-onboard-abc", "homestead-auth"):
            self.cluster.kube.add({"apiVersion": "v1", "kind": "Secret", "metadata": {"name": name, "namespace": "lab"}})

        self.assertEqual(1, onboard.tidy_old_plans())
        self.assertEqual(["homestead-auth"], self.cluster.names("/api/v1/namespaces/lab/secrets"))


class RemovalTests(Base):
    def kill(self, name):
        self.cluster.edit(f"{NODES}/{name}", lambda n: n["status"]["conditions"][0].update(status="Unknown"))

    def test_a_ready_node_is_not_removed_from_here(self):
        report = onboard.removal_plan("node3")

        self.assertFalse(report["ok"])
        self.assertIn("rke2-uninstall.sh", report["blockers"][0])

    def test_a_ready_k3s_node_gets_k3s_steps(self):
        self.cluster.edit(f"{NODES}/node3", lambda n: n["status"].setdefault("nodeInfo", {}).update(kubeletVersion="v1.33.4+k3s1"))

        blocker = onboard.removal_plan("node3")["blockers"][0]

        self.assertIn("k3s-agent-uninstall.sh", blocker)
        self.assertNotIn("Harvester", blocker)

    def test_removing_a_dead_control_plane_node_keeps_quorum_and_says_the_margin(self):
        self.kill("node3")

        report = onboard.removal_plan("node3")

        self.assertTrue(report["ok"], report["blockers"])
        self.assertTrue(any("one more failure" in w for w in report["warnings"]))

    def test_losing_quorum_is_refused(self):
        self.kill("node2")
        self.kill("node3")

        report = onboard.removal_plan("node3")

        self.assertFalse(report["ok"])
        self.assertIn("quorum", report["blockers"][0])

    def test_the_last_copy_of_a_volume_is_called_out(self):
        self.kill("node3")
        self.cluster.kube.add(replica("only-here-r-3", "node3", "only-here"))
        self.cluster.kube.add(replica("mirrored-r-3", "node3", "mirrored"))
        self.cluster.kube.add(replica("mirrored-r-1", "node1", "mirrored"))

        report = onboard.removal_plan("node3")
        self.assertEqual((["only-here"], ["mirrored"]), (report["lost_volumes"], report["rebuilt_volumes"]))
        with self.assertRaisesRegex(ValueError, "only copy"):
            onboard.remove_node("node3")

    def test_a_dead_node_is_removed_in_harvesters_order(self):
        self.kill("node3")

        result = onboard.remove_node("node3")

        order = [(m, p.split("/")[-1]) for m, p in self.cluster.sent]
        self.assertEqual([("PATCH", "node3"), ("DELETE", "node3"), ("DELETE", "custom-3"), ("DELETE", "node3")], order)
        self.assertEqual(4, len(result["log"]))
        self.assertNotIn("custom-3", self.cluster.machines)

    def hold_things_on(self, name):
        """A host that died holding a pod, a VM, a volume attachment and a replica."""
        self.kill(name)
        self.cluster.kube.add({"apiVersion": "v1", "kind": "Pod", "metadata": {"name": "app-1", "namespace": "lab"},
                               "spec": {"nodeName": name}})
        self.cluster.kube.add({"apiVersion": "kubevirt.io/v1", "kind": "VirtualMachineInstance",
                               "metadata": {"name": "haos", "namespace": "lab"}, "status": {"nodeName": name}})
        self.cluster.kube.add({"apiVersion": "storage.k8s.io/v1", "kind": "VolumeAttachment", "metadata": {"name": "csi-abc"},
                               "spec": {"nodeName": name}})
        self.cluster.kube.add(replica("vol-r-3", name, "vol"))
        self.cluster.kube.add(replica("vol-r-1", "node1", "vol"))
        self.cluster.edit(f"{MACHINES}/custom-3", lambda m: m["metadata"].update(finalizers=["machine.cluster.x-k8s.io"]))

    def test_the_plan_says_what_a_dead_host_still_holds(self):
        self.hold_things_on("node3")

        report = onboard.removal_plan("node3")

        self.assertEqual({"pods": 1, "vms": ["haos"], "attachments": 1, "replicas": 1}, report["stuck"])
        self.assertTrue(any("haos" in step for step in report["gone_steps"]))

    def test_a_host_gone_for_good_lets_go_of_everything_it_held(self):
        self.hold_things_on("node3")

        result = onboard.remove_node("node3", gone=True)

        deleted = [p for m, p in self.cluster.sent if m == "DELETE"]
        self.assertIn("/apis/kubevirt.io/v1/namespaces/lab/virtualmachineinstances/haos", deleted)
        self.assertIn("/api/v1/namespaces/lab/pods/app-1", deleted)
        self.assertIn("/apis/storage.k8s.io/v1/volumeattachments/csi-abc", deleted)
        self.assertIn("/apis/longhorn.io/v1beta2/namespaces/longhorn-system/replicas/vol-r-3", deleted)
        self.assertNotIn("custom-3", self.cluster.machines, "the machine held by its finalizer is let go")
        self.assertNotIn("node3", self.cluster.lh_nodes, "no replicas left, so Longhorn's record goes too")
        self.assertTrue(any("finalizers" in line for line in result["log"]))

    def test_the_standard_removal_leaves_what_it_held_for_the_controllers(self):
        self.hold_things_on("node3")

        onboard.remove_node("node3")

        deleted = [p for m, p in self.cluster.sent if m == "DELETE"]
        self.assertNotIn("/api/v1/namespaces/lab/pods/app-1", deleted)
        self.assertIn("custom-3", self.cluster.machines, "still deleting")

    def test_a_node_that_only_just_went_down_gets_a_warning(self):
        self.kill("node3")
        self.cluster.edit(f"{NODES}/node3", lambda n: n["status"]["conditions"][0].update(
            lastTransitionTime=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())))

        report = onboard.removal_plan("node3")

        self.assertTrue(any("restarting" in w for w in report["warnings"]))

    def test_a_stuck_machine_or_longhorn_node_needs_force(self):
        self.cluster.kube.add(machine("custom-9", "gone", phase="Deleting",
                                      deletionTimestamp="2026-09-23T10:00:00Z", finalizers=["x"]))
        self.cluster.kube.add(longhorn_node("gone"))
        self.cluster.kube.add(replica("r-gone", "gone", "v", state="error"))

        with self.assertRaisesRegex(ValueError, "force"):
            onboard.cleanup("machine", "custom-9")
        with self.assertRaisesRegex(ValueError, "force"):
            onboard.cleanup("longhorn", "gone")
        onboard.cleanup("machine", "custom-9", force=True)
        onboard.cleanup("longhorn", "gone", force=True)

        self.assertNotIn("custom-9", self.cluster.machines)
        self.assertNotIn("gone", self.cluster.lh_nodes)

    def test_the_cleanup_report_finds_leftovers(self):
        self.kill("node2")
        self.cluster.kube.add(machine("custom-9", "gone"))
        self.cluster.kube.add(longhorn_node("gone"))

        report = onboard.cleanup_report()

        self.assertEqual(["node2"], [n["name"] for n in report["dead_nodes"]])
        self.assertEqual(["custom-9"], [m["name"] for m in report["stale_machines"]])
        self.assertEqual(["gone"], [n["name"] for n in report["stale_longhorn"]])

    def test_leftovers_are_cleaned_one_at_a_time_and_only_if_leftover(self):
        self.cluster.kube.add(longhorn_node("gone"))

        onboard.cleanup("longhorn", "gone")

        self.assertNotIn("gone", self.cluster.lh_nodes)
        self.assertIn(("PATCH", "/apis/longhorn.io/v1beta2/namespaces/longhorn-system/nodes/gone"), self.cluster.sent)
        with self.assertRaisesRegex(ValueError, "still has"):
            onboard.cleanup("longhorn", "node1")
        with self.assertRaisesRegex(ValueError, "not left over"):
            onboard.cleanup("machine", "custom-1")


def saved_job(kind, node="node3", status="running", job_id=None, **ref):
    """A job as the store keeps it, for the host `node` (V2 cleanup: a row for it)."""
    ref = {"nodes": [{"node": node, "uid": "u-" + node, "phase": "pending"}]} | ref if kind == "longhorn-v2-cleanup" \
        else {"node": node} | ref
    return {"id": job_id or f"{kind}-{node}", "kind": kind, "title": f"A {kind} job on {node}", "status": status,
            "started_at": "2026-11-04T10:00:00Z", "updated_at": "2026-11-04T10:00:00Z",
            "resource": {"kind": "Node", "name": node}, "href": f"/nodes/{node}", "ref": ref}


# The kinds that strand a removed host's disk work, each still going.
PENDING = {
    "V2 disk preparation": saved_job("disk-v2-convert", phase="awaiting-erase"),
    "storage allocation": saved_job("disk-allocation", phase="queued"),
    "Longhorn runtime maintenance": saved_job("longhorn-runtime", phase="staged"),
    "disk retire": saved_job("disk-retire", phase="stop"),
    "V2 cleanup": saved_job("longhorn-v2-cleanup", phase="restart-required"),
}


class DiskJobRemovalTests(Base):
    """A saved disk job pins the host's identity and most cannot be cancelled
    once started, so removing the host under it strands the job - and the job then blocks
    Longhorn upgrades, shutdowns and OS rollouts cluster-wide."""

    def setUp(self):
        super().setUp()
        onboard.bind(K.kget, K.ksend, "lab", OPS)   # the real job store, in the test's DATA_DIR
        self.cluster.edit(f"{NODES}/node3", lambda n: n["status"]["conditions"][0].update(status="Unknown"))

    def save(self, *jobs):
        with OPS.transaction() as tx:
            tx.items.extend(jobs)
            tx.save()

    def test_a_saved_disk_job_on_the_host_blocks_a_standard_removal(self):
        self.save(PENDING["V2 disk preparation"])

        report = onboard.removal_plan("node3")

        self.assertEqual(["disk-v2-convert-node3"], [job["id"] for job in report["disk_jobs"]])
        self.assertIn("node3 has a saved V2 disk preparation job", report["disk_jobs"][0]["text"])
        with self.assertRaisesRegex(ValueError, "saved V2 disk preparation job"):
            onboard.remove_node("node3")
        self.assertEqual([], self.cluster.sent, "refused before anything changed: no Longhorn PATCH, no DELETE")

    def test_each_disk_job_kind_counts(self):
        for label, job in PENDING.items():
            with self.subTest(label):
                with OPS.transaction() as tx:
                    tx.save([job])

                self.assertEqual([job["id"]], [j["id"] for j in onboard.removal_plan("node3")["disk_jobs"]])
                with self.assertRaisesRegex(ValueError, f"saved {label} job"):
                    onboard.remove_node("node3")
                self.assertEqual([], self.cluster.sent)

    def test_finished_or_other_host_jobs_do_not_block(self):
        self.save(saved_job("disk-v2-convert", phase="complete", job_id="v2-done"),
                  saved_job("disk-allocation", phase="cancelled", job_id="alloc-cancelled"),
                  saved_job("longhorn-runtime", status="succeeded", phase="retired", job_id="runtime-retired"),
                  saved_job("disk-retire", status="succeeded", phase="done", job_id="retire-done"),
                  saved_job("longhorn-v2-cleanup", status="succeeded", phase="complete", job_id="cleanup-done"),
                  saved_job("disk-v2-convert", node="node2", phase="awaiting-erase", job_id="v2-elsewhere"),
                  saved_job("longhorn-v2-cleanup", node="node2", phase="restart-required", job_id="cleanup-elsewhere"),
                  saved_job("node-power", phase="drain", job_id="not-a-disk-job"))

        self.assertEqual([], onboard.removal_plan("node3")["disk_jobs"])
        onboard.remove_node("node3")

        order = [(m, p.split("/")[-1]) for m, p in self.cluster.sent]
        self.assertEqual([("PATCH", "node3"), ("DELETE", "node3"), ("DELETE", "custom-3"), ("DELETE", "node3")], order)

    def test_gone_for_good_with_a_saved_disk_job_needs_acknowledgement(self):
        self.save(PENDING["storage allocation"])

        with self.assertRaisesRegex(ValueError, "saved storage allocation job"):
            onboard.remove_node("node3", gone=True)
        self.assertEqual([], self.cluster.sent)

        result = onboard.remove_node("node3", gone=True, accept_jobs=["disk-allocation-node3"])

        self.assertIn(("DELETE", f"{NODES}/node3"), self.cluster.sent)
        self.assertIn("The saved storage allocation job disk-allocation-node3 (A disk-allocation job on node3) stays, "
                      "and keeps blocking Longhorn upgrades, shutdown and OS rollouts until it is ended from Jobs once "
                      "the host is removed", result["log"])
        self.assertEqual(["disk-allocation-node3"], [i["id"] for i in OPS.stored() if i["kind"] == "disk-allocation"],
                         "the job stays saved; ending it is a separate step")

    def test_the_refusal_says_what_leaving_the_jobs_means(self):
        self.save(PENDING["storage allocation"])
        with self.assertRaisesRegex(ValueError, "To remove it anyway, confirm that the saved job stays, and keeps "
                                    "blocking Longhorn upgrades, shutdown and OS rollouts until it is ended from Jobs once "
                                    "the host is removed."):
            onboard.remove_node("node3", gone=True)
        self.save(PENDING["V2 disk preparation"])   # beside the allocation
        with self.assertRaisesRegex(ValueError, "To remove it anyway, confirm that the saved jobs stay, and keep "
                                    "blocking Longhorn upgrades, shutdown and OS rollouts until they are ended from Jobs once "
                                    "the host is removed."):
            onboard.remove_node("node3", gone=True)

    # What leaving a job means depends on its kind: only three can be ended from Jobs.
    RETIRE = ("until it stops. It fails by itself once Longhorn's record of the host is deleted")
    CLEANUP = ("until it ends. Its next round refuses once the cluster's hosts change, and uncordons the hosts "
               "it cordoned. If it prepared a RAM release on another host, it waits for that host to restart before it fails")

    def left_behind(self, job, until):
        self.save(job)
        stay = ("The saved job stays, and keeps blocking Longhorn upgrades, shutdown and OS rollouts " + until + ".")
        self.assertEqual(stay, onboard.removal_plan("node3")["disk_jobs_stay"])
        with self.assertRaises(ValueError) as refused:
            onboard.remove_node("node3", gone=True)
        self.assertTrue(str(refused.exception).endswith(" To remove it anyway, confirm that t" + stay[1:]), str(refused.exception))
        result = onboard.remove_node("node3", gone=True, accept_jobs=[job["id"]])
        self.assertIn(f"The saved {onboard.DISK_JOBS[job['kind']]} job {job['id']} ({job['title']}) stays, and keeps "
                      f"blocking Longhorn upgrades, shutdown and OS rollouts {until}", result["log"])

    def test_a_disk_retire_left_behind_fails_by_itself(self):
        self.left_behind(PENDING["disk retire"], self.RETIRE)

    def test_a_v2_cleanup_left_behind_ends_on_its_next_round_or_after_a_staged_restart(self):
        self.left_behind(PENDING["V2 cleanup"], self.CLEANUP)

    def test_mixed_kinds_name_each_case(self):
        self.save(PENDING["storage allocation"], PENDING["V2 disk preparation"], PENDING["Longhorn runtime maintenance"],
                  PENDING["disk retire"], PENDING["V2 cleanup"])
        stay = ("The saved jobs stay, and keep blocking Longhorn upgrades, shutdown and OS rollouts. "
                "The storage allocation, V2 disk preparation and Longhorn runtime maintenance jobs do so "
                "until they are ended from Jobs once the host is removed. "
                "The disk retire job does so " + self.RETIRE + ". The V2 cleanup job does so " + self.CLEANUP + ".")
        self.assertEqual(stay, onboard.removal_plan("node3")["disk_jobs_stay"])
        with self.assertRaises(ValueError) as refused:
            onboard.remove_node("node3", gone=True)
        self.assertTrue(str(refused.exception).endswith(" To remove it anyway, confirm that t" + stay[1:]), str(refused.exception))
        result = onboard.remove_node("node3", gone=True, accept_jobs=[j["id"] for j in PENDING.values()])
        self.assertEqual(5, sum("stays, and keeps blocking" in line for line in result["log"]))
        self.assertTrue(any("disk retire job" in line and line.endswith(self.RETIRE) for line in result["log"]))
        self.assertTrue(any("V2 cleanup job" in line and line.endswith(self.CLEANUP) for line in result["log"]))
        self.assertTrue(any("storage allocation job" in line and line.endswith("until it is ended from Jobs once the host is removed")
                            for line in result["log"]))

    def test_two_kinds_of_one_case_are_joined_with_and(self):
        text = onboard.stays([{"kind": "disk-allocation"}, {"kind": "disk-v2-convert"}, {"kind": "disk-retire"}])
        self.assertIn(". The storage allocation and V2 disk preparation jobs do so until they are ended", text)

    def test_no_saved_job_means_nothing_to_say(self):
        self.assertEqual("", onboard.removal_plan("node3")["disk_jobs_stay"])

    def test_an_acknowledgement_covers_only_the_jobs_it_names(self):
        self.save(PENDING["storage allocation"], PENDING["disk retire"])

        with self.assertRaisesRegex(ValueError, "saved disk retire job"):
            onboard.remove_node("node3", gone=True, accept_jobs=["disk-allocation-node3"])
        self.assertEqual([], self.cluster.sent)

    def test_the_standard_removal_is_refused_even_with_an_acknowledgement(self):
        self.save(PENDING["V2 disk preparation"])

        with self.assertRaisesRegex(ValueError, "saved V2 disk preparation job"):
            onboard.remove_node("node3", accept_jobs=["disk-v2-convert-node3"])
        self.assertEqual([], self.cluster.sent)

    def test_the_check_runs_under_the_job_lock(self):
        """A job started after the review, before the removal, still stops it:
        the store is read again while the job lock is held."""
        job = PENDING["Longhorn runtime maintenance"]

        class LockedOps:
            TERMINAL = OPS.TERMINAL
            held = False

            @contextlib.contextmanager
            def transaction(self):
                LockedOps.held = True
                try:
                    yield None
                finally:
                    LockedOps.held = False

            def stored(self):
                return [job] if LockedOps.held else []   # the job starts once the review has read the store

            def start(self, *args, **kwargs):
                raise AssertionError("nothing is recorded for a refused removal")

        onboard.bind(K.kget, K.ksend, "lab", LockedOps())

        with self.assertRaisesRegex(ValueError, "saved Longhorn runtime maintenance job"):
            onboard.remove_node("node3")
        self.assertEqual([], self.cluster.sent)

    def starter(self):
        """Another request starting a disk job the way allocation and V2 cleanup
        do (homestead_disk_allocation.start, homestead_lhv2_cleanup.start): it
        takes the job lock, then inspects the host before saving anything."""
        saw = []

        def start():
            with OPS.transaction():
                saw.append(K.get_optional(f"{NODES}/node3", read=K.kget) is not None)
        return threading.Thread(target=start, daemon=True), saw

    def remove_with_a_start_at(self, write, wait=0.2):
        """Removes node3; when the removal sends `write`, another request starts
        a disk job. Each write after it records whether that start still waits
        after `wait` seconds (long enough to finish when nothing holds it)."""
        thread, saw = self.starter()
        waiting = []

        def send(method, path, *args):
            if (method, path) == write:
                thread.start()
            if thread.ident:
                thread.join(wait)
                waiting.append(((method, path.split("/")[-1]), thread.is_alive()))
            return K.ksend(method, path, *args)

        onboard.bind(K.kget, send, "lab", OPS)
        onboard.remove_node("node3")
        thread.join(10)
        return waiting, saw

    def test_a_disk_job_start_during_the_removal_waits_and_finds_the_host_gone(self):
        """From the recheck until the node DELETE is sent, a start that checks
        the host under the job lock waits; once it gets the lock, the host is
        gone, so it cannot save a job for it."""
        waiting, saw = self.remove_with_a_start_at(("PATCH", f"{LONGHORN}/nodes/node3"))

        self.assertEqual([(("PATCH", "node3"), True), (("DELETE", "node3"), True)], waiting[:2],
                         "the Longhorn PATCH and the node DELETE are both sent while the start waits")
        self.assertEqual([False], saw, "the start sees no node3 once it has the lock")

    def test_a_job_start_after_the_node_delete_is_not_held(self):
        """The lock is let go once the node DELETE is sent: the machine and
        Longhorn record deletes do not hold up other job starts."""
        waiting, saw = self.remove_with_a_start_at(("DELETE", f"{MACHINES}/custom-3"), wait=5)

        self.assertEqual([(("DELETE", "custom-3"), False), (("DELETE", "node3"), False)], waiting)
        self.assertEqual([False], saw)

    def test_a_disk_retire_can_be_rolled_back_says_so(self):
        early = saved_job("disk-retire", phase="replicas", job_id="early")
        late = saved_job("disk-retire", node="node3", phase="remove", job_id="late")
        for job, rollback in ((early, True), (late, False)):
            with self.subTest(job["ref"]["phase"]):
                with OPS.transaction() as tx:
                    tx.save([job])
                text = onboard.removal_plan("node3")["disk_jobs"][0]["text"]
                self.assertEqual(rollback, "roll it back" in text, text)


class RemoveRouteTests(unittest.TestCase):
    def removed_with(self, body):
        import homestead_cluster_server as CLUSTER
        calls = []
        with mock.patch.object(CLUSTER.ONBOARD, "remove_node", lambda *a, **k: calls.append((a, k)) or {"ok": True}):
            CLUSTER._remove_node(SimpleNamespace(body=body))
        return calls

    def test_the_route_passes_the_acknowledged_job_ids_on(self):
        (args, kwargs), = self.removed_with({"node": "node3", "gone": True, "accept_jobs": ["j1", "j2"]})

        self.assertEqual(("node3", False, True), args)
        self.assertEqual(["j1", "j2"], kwargs["accept_jobs"])

    def test_an_acknowledgement_that_is_not_a_list_of_ids_names_no_job(self):
        for sent in ("j1", {"j1": True}, [1, None], True):
            with self.subTest(sent=sent):
                (_, kwargs), = self.removed_with({"node": "node3", "gone": True, "accept_jobs": sent})
                self.assertEqual([], kwargs["accept_jobs"])


if __name__ == "__main__":
    unittest.main()
