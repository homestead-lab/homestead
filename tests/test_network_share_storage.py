"""A network share is not data kept on the host (#1545).

A pod that mounts an NFS export, or a volume on an NFS or SMB server, keeps its
data on that server. The host restart review lists it with the host's own
storage, but it asks no data acceptance for it, an OS-update restart with nobody
there is not held by it, and Make server does not call it "kept on this host".
A host path, a local volume and an emptyDir are counted as before.

Reads go through the real kget over the shared fake API server
(tests/fakes/kube.py)."""
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import server
import homestead_kube as K
import homestead_maintenance as MAINTENANCE
import homestead_power as power
import homestead_promote as PROMOTE
from fakes.kube import Kube
import own_pod
from rebind import keep_bound
from test_power_drain_blockers import host
from test_promote import PlanBase

REAL_INVENTORY = MAINTENANCE.inventory
NAS = "192.0.2.20"


def app_pod(name, volumes, node="node-a"):
    return {"apiVersion": "v1", "kind": "Pod",
            "metadata": {"namespace": "lab", "name": name, "uid": f"uid-{name}",
                         "ownerReferences": [{"apiVersion": "apps/v1", "kind": "ReplicaSet", "name": name,
                                              "uid": f"rs-{name}", "controller": True}]},
            "spec": {"nodeName": node, "containers": [{"name": "c"}], "volumes": volumes},
            "status": {"phase": "Running", "conditions": [{"type": "Ready", "status": "True"}]}}


def bound(claim, pv_spec):
    """A volume claim in lab bound to a PersistentVolume with this spec."""
    return [{"apiVersion": "v1", "kind": "PersistentVolumeClaim", "metadata": {"namespace": "lab", "name": claim},
             "spec": {"volumeName": f"pv-{claim}"}, "status": {"phase": "Bound"}},
            {"apiVersion": "v1", "kind": "PersistentVolume", "metadata": {"name": f"pv-{claim}"}, "spec": pv_spec}]


def claim_volume(claim):
    return {"name": claim, "persistentVolumeClaim": {"claimName": claim}}


NFS = {"name": "media", "nfs": {"server": NAS, "path": "/export/media"}}
# The shapes the csi-driver-nfs and csi-driver-smb provisioners write, and an
# NFS PersistentVolume written by hand.
SHARES = {
    "nfs-csi": {"csi": {"driver": "nfs.csi.k8s.io", "volumeHandle": NAS + "#export#media##",
                        "volumeAttributes": {"server": NAS, "share": "/export"}}},
    "smb-csi": {"csi": {"driver": "smb.csi.k8s.io", "volumeHandle": NAS + "/share#media##",
                        "volumeAttributes": {"source": "//" + NAS + "/share"}}},
    "nfs-pv": {"nfs": {"server": NAS, "path": "/export/media"}},
}
ON_HOST = {
    "local-pv": {"local": {"path": "/srv/data"}},
    "hostpath-pv": {"hostPath": {"path": "/srv/data"}},
}


class Cluster(unittest.TestCase):
    def setUp(self):
        keep_bound(self, power)
        self.kube = Kube(host("node-a", "uid-a"), host("node-b", "uid-b"))
        self.kube.__enter__()
        self.addCleanup(self.kube.__exit__, None, None, None)
        power.bind(K.kget, lambda node: {"workloads": [], "stranded": []},
                   lambda: {"members": ["node-a", "node-b", "node-c"], "can_lose": 1}, lambda: True)
        self.kube.add(own_pod.runs_as(self, K.kget))

    def run_with(self, volumes, *objects):
        for obj in objects:
            self.kube.add(obj)
        self.kube.add(app_pod("app", volumes))


class RestartReviewTests(Cluster):
    def review(self, volumes, *objects):
        self.run_with(volumes, *objects)
        plan = power.plan("node-a", "reboot")
        self.assertFalse(plan["storage_unknown"])
        self.assertEqual([], plan["blockers"])
        return plan

    def test_a_network_share_asks_no_data_acceptance_and_is_still_listed(self):
        plan = self.review([NFS])
        self.assertFalse(plan["requires_data_ack"])
        self.assertEqual([("lab/app", NAS)], [(r["pod"], r["source"]) for r in plan["maintenance"]["local_storage"]])

    def test_a_volume_on_an_nfs_or_smb_server_asks_no_data_acceptance(self):
        for claim, spec in SHARES.items():
            with self.subTest(claim):
                self.setUp()
                plan = self.review([claim_volume(claim)], *bound(claim, spec))
                self.assertFalse(plan["requires_data_ack"])
                self.assertEqual(["lab/app"], [r["pod"] for r in plan["maintenance"]["local_storage"]])

    def test_data_on_the_host_still_asks_for_it(self):
        cases = {"hostPath": ([{"name": "files", "hostPath": {"path": "/srv/files"}}], []),
                 "emptyDir": ([{"name": "cache", "emptyDir": {}}], []),
                 **{c: ([claim_volume(c)], bound(c, spec)) for c, spec in ON_HOST.items()}}
        for case, (volumes, objects) in cases.items():
            with self.subTest(case):
                self.setUp()
                self.assertTrue(self.review(volumes, *objects)["requires_data_ack"])
                self.setUp()
                # Next to a network share, too.
                self.assertTrue(self.review(volumes + [NFS], *objects)["requires_data_ack"])

    def test_another_kind_of_volume_server_is_counted_as_before(self):
        # Only NFS and SMB are known to keep the data on another server.
        plan = self.review([claim_volume("other")], *bound("other", {"csi": {"driver": "csi.example.com",
                                                                               "volumeHandle": "v1"}}))
        self.assertTrue(plan["requires_data_ack"])


class UnattendedRestartTests(Cluster):
    def check(self, volumes, *objects):
        self.run_with(volumes, *objects)
        return server.POWER_JOBS.rollout_check("node-a")

    def test_a_network_share_does_not_hold_an_os_update_restart(self):
        self.assertEqual(("", False), self.check([NFS]))
        self.setUp()
        claim, spec = "nfs-csi", SHARES["nfs-csi"]
        self.assertEqual(("", False), self.check([claim_volume(claim)], *bound(claim, spec)))

    def test_nor_next_to_the_hosts_own_plumbing(self):
        # The plumbing alone asks the review for acceptance; the share must
        # not be what is left to refuse with.
        plumbing = {"name": "sockets", "hostPath": {"path": "/var/lib/kubelet/plugins"}}
        self.assertEqual(("", False), self.check([plumbing, NFS]))

    def test_data_on_the_host_still_holds_it_until_a_person_acts(self):
        refusal, may_clear = self.check([{"name": "files", "hostPath": {"path": "/srv/files"}}, NFS])
        self.assertRegex(refusal, r"pods keep data on this host itself: lab/app \(/srv/files\)")
        self.assertNotIn(NAS, refusal)
        self.assertFalse(may_clear)


class MakeServerTests(PlanBase):
    def warnings(self, *pods):
        for pod in pods:
            self.cluster.kube.add(pod)
        with mock.patch.object(MAINTENANCE, "inventory", REAL_INVENTORY):
            return PROMOTE.plan("app-1")["warnings"]

    def test_a_network_share_is_not_kept_on_this_host(self):
        warnings = self.warnings(app_pod("player", [NFS], node="app-1"),
                                 app_pod("db", [{"name": "files", "hostPath": {"path": "/srv/db"}}], node="app-1"))
        kept = [w for w in warnings if w.startswith("Kept on this host")]
        self.assertEqual(["Kept on this host, waiting for it: lab/db"], kept)
        share = [w for w in warnings if "lab/player" in w]
        self.assertEqual(1, len(share), warnings)
        self.assertIn("shared folder on another server", share[0])
        self.assertIn(NAS, share[0])


if __name__ == "__main__":
    unittest.main()
