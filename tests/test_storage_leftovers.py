"""#932: what storage leaves behind that Cluster Health did not name.

Released volumes: a claim was deleted, its class keeps the data (Retain),
and the volume keeps its space allocated on the hosts until someone deletes
it. Stale iSCSI sessions: a host lost while a Longhorn engine ran on it keeps
a session to the old instance-manager's address once it comes back, and a
later attach there fails in a loop. homestead_storage_leftovers reads both;
it changes nothing."""
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import homestead_node_probes as NODE_PROBES
import homestead_routes
import homestead_storage_leftovers as LEFTOVERS
from fakes.kube import Kube

GiB = 1024 ** 3
LH = "/apis/longhorn.io/v1beta2/namespaces/longhorn-system"
PODS = "/api/v1/namespaces/longhorn-system/pods?labelSelector=longhorn.io%2Fcomponent%3Dinstance-manager"
OLD_COPY = "homestead.io/reclass-old"


def pv(name, phase="Released", policy="Retain", size="40Gi", claim=("media", "photos"), driver="driver.longhorn.io",
       klass="longhorn", released="2026-10-16T02:39:48Z", created="2026-10-01T00:00:00Z", annotations=None):
    spec = {"capacity": {"storage": size}, "persistentVolumeReclaimPolicy": policy, "storageClassName": klass,
            "claimRef": {"namespace": claim[0], "name": claim[1], "uid": "u-" + name}}
    if driver:
        spec["csi"] = {"driver": driver, "volumeHandle": name}
    status = {"phase": phase}
    if released:
        status["lastPhaseTransitionTime"] = released
    return {"metadata": {"name": name, "creationTimestamp": created, "annotations": annotations or {}},
            "spec": spec, "status": status}


def replica(volume, node):
    return {"metadata": {"name": f"{volume}-r-{node}"}, "spec": {"volumeName": volume, "nodeID": node}}


def im(node, ip, phase="Running"):
    return {"metadata": {"name": "instance-manager-" + node, "labels": {"longhorn.io/component": "instance-manager"}},
            "spec": {"nodeName": node}, "status": {"phase": phase, "podIP": ip, "podIPs": [{"ip": ip}]}}


def session(sid, address, volume="pvc-a", state="LOGGED_IN", disks=("sdj",)):
    return {"sid": sid, "target": "iqn.2019-10.io.longhorn:" + volume, "state": state, "address": address,
            "port": 3260, "disks": list(disks)}


class Cluster:
    """The volumes, copies, instance managers and claims above on the shared fake API server
    (tests/fakes/kube.py); fail names list paths the API server answers 403."""

    def __init__(self, pvs=(), replicas=(), ims=(), probes=None, fail=(), claims=()):
        def made(kind, version, objects, namespace=None):
            for obj in objects:
                obj = {**obj, "apiVersion": version, "kind": kind}
                if namespace:
                    obj["metadata"] = {**obj["metadata"], "namespace": namespace}
                yield obj
        self.kube = Kube(*made("PersistentVolume", "v1", pvs),
                         *made("Replica", "longhorn.io/v1beta2", replicas, "longhorn-system"),
                         *made("Pod", "v1", ims, "longhorn-system"),
                         *made("PersistentVolumeClaim", "v1", claims))
        for path in fail:
            self.kube.fail(path.partition("?")[0], 403, times=100)
        self.probes = probes or {}

    def __enter__(self):
        self.kube.__enter__()
        self.patches = [mock.patch.object(NODE_PROBES, "node_temps", lambda *a, **k: self.probes),
                        mock.patch.object(homestead_routes, "cached", lambda key, seconds, fn: fn())]
        for p in self.patches:
            p.start()
        return self

    def __exit__(self, *exc):
        for p in self.patches:
            p.stop()
        return self.kube.__exit__(*exc)


class ReleasedVolumes(unittest.TestCase):
    def test_a_released_retained_volume_is_named_with_its_size_age_class_claim_and_hosts(self):
        with Cluster([pv("pvc-a", size="40Gi"), pv("pvc-bound", phase="Bound")],
                     [replica("pvc-a", "node-1"), replica("pvc-a", "node-2")]):
            found = LEFTOVERS.facts()["released"]
        self.assertEqual("ok", found["state"])
        self.assertEqual([{
            "pv": "pvc-a", "namespace": "media", "claim": "photos", "class": "longhorn", "policy": "Retain",
            "size": "40Gi", "size_bytes": 40 * GiB, "released_at": "2026-10-16T02:39:48Z", "driver": "driver.longhorn.io",
            "longhorn": "pvc-a", "hosts": ["node-1", "node-2"], "allocated_bytes": 80 * GiB, "old_copy": False, "claim_reused": False}],
            found["volumes"])

    def test_a_volume_whose_class_deletes_it_is_not_one_that_is_kept(self):
        with Cluster([pv("pvc-a", policy="Delete"), pv("pvc-b", phase="Available")]):
            self.assertEqual([], LEFTOVERS.facts()["released"]["volumes"])

    def test_without_a_release_time_the_age_is_the_volume_s_own(self):
        with Cluster([pv("pvc-a", released=None)], [replica("pvc-a", "node-1")]):
            self.assertEqual("2026-10-01T00:00:00Z", LEFTOVERS.facts()["released"]["volumes"][0]["released_at"])

    def test_a_volume_of_another_driver_is_named_at_its_size_with_no_longhorn_volume(self):
        with Cluster([pv("pv-nfs", driver="", klass="nfs", size="10Gi")]):
            row = LEFTOVERS.facts()["released"]["volumes"][0]
        self.assertEqual(("", [], 10 * GiB), (row["longhorn"], row["hosts"], row["allocated_bytes"]))

    def test_an_old_copy_a_class_change_kept_says_so(self):
        with Cluster([pv("pvc-a", annotations={OLD_COPY: "media/photos"})], [replica("pvc-a", "node-1")]):
            self.assertTrue(LEFTOVERS.facts()["released"]["volumes"][0]["old_copy"])

    def test_unread_copies_leave_the_hosts_unknown_not_the_volume_unnamed(self):
        with Cluster([pv("pvc-a", size="40Gi")], fail={f"{LH}/replicas"}):
            row = LEFTOVERS.facts()["released"]["volumes"][0]
        self.assertEqual((None, 40 * GiB), (row["hosts"], row["allocated_bytes"]))

    def test_a_claim_name_in_use_again_is_said_so_the_delete_is_not_offered_for_it(self):
        # The reviewed delete looks the claim up by name: a new claim of the
        # same name is not this volume's, and that review refuses it.
        again = {"metadata": {"namespace": "media", "name": "photos"}, "spec": {"volumeName": "pvc-new"}}
        with Cluster([pv("pvc-a")], [replica("pvc-a", "node-1")], claims=[again]):
            self.assertTrue(LEFTOVERS.facts()["released"]["volumes"][0]["claim_reused"])

    def test_unread_claims_leave_reuse_unknown(self):
        with Cluster([pv("pvc-a")], [replica("pvc-a", "node-1")], fail={"/api/v1/persistentvolumeclaims"}):
            self.assertIsNone(LEFTOVERS.facts()["released"]["volumes"][0]["claim_reused"])

    def test_unread_volumes_are_unknown_not_none(self):
        with Cluster(fail={"/api/v1/persistentvolumes"}):
            found = LEFTOVERS.facts()["released"]
        self.assertEqual(("unknown", []), (found["state"], found["volumes"]))
        self.assertIn("403", found["error"])


class StaleSessions(unittest.TestCase):
    def test_a_session_to_an_instance_manager_address_that_is_gone_is_named_on_its_host(self):
        probes = {"node-2": {"iscsi": [session(67, "192.0.2.40", "pvc-a"), session(70, "192.0.2.41", "pvc-b")]}}
        with Cluster([pv("pvc-a", phase="Bound")], ims=[im("node-2", "192.0.2.41")], probes=probes):
            found = LEFTOVERS.facts()["iscsi"]
        self.assertEqual("ok", found["state"])
        self.assertEqual([{"node": "node-2", "sessions": [{
            "sid": 67, "target": "iqn.2019-10.io.longhorn:pvc-a", "volume": "pvc-a", "namespace": "media",
            "claim": "photos", "state": "LOGGED_IN", "address": "192.0.2.40", "port": 3260, "disks": ["sdj"]}]}],
            found["hosts"])

    def test_a_session_to_another_target_is_not_longhorn_s_to_judge(self):
        other = dict(session(3, "198.51.100.7"), target="iqn.2005-10.org.example:nas")
        with Cluster(ims=[im("node-1", "192.0.2.41")], probes={"node-1": {"iscsi": [other]}}):
            self.assertEqual([], LEFTOVERS.facts()["iscsi"]["hosts"])

    def test_an_address_any_instance_manager_has_now_is_current(self):
        probes = {"node-1": {"iscsi": [session(1, "192.0.2.42")]}}
        with Cluster(ims=[im("node-1", "192.0.2.41"), im("node-2", "192.0.2.42")], probes=probes):
            self.assertEqual([], LEFTOVERS.facts()["iscsi"]["hosts"])

    def test_an_address_on_a_storage_network_counts_as_the_instance_manager_s(self):
        # Longhorn's storage network gives instance-managers a second address
        # (Multus); a session to it is current, not stale.
        probes = {"node-1": {"iscsi": [session(1, "198.51.100.21")]}}
        pod = im("node-1", "192.0.2.41")
        pod["metadata"]["annotations"] = {"k8s.v1.cni.cncf.io/network-status": (
            '[{"name":"cbr0","ips":["192.0.2.41"]},{"name":"longhorn-system/storage","ips":["198.51.100.21"]}]')}
        with Cluster(ims=[pod], probes=probes):
            self.assertEqual([], LEFTOVERS.facts()["iscsi"]["hosts"])

    def test_the_address_of_an_instance_manager_that_has_ended_is_not_current(self):
        probes = {"node-1": {"iscsi": [session(1, "192.0.2.40")]}}
        # A pod still terminating may still serve its engines, so it counts.
        for phase in ("Failed", "Succeeded"):
            with Cluster(ims=[im("node-1", "192.0.2.41"), im("node-2", "192.0.2.40", phase=phase)], probes=probes):
                self.assertEqual(1, len(LEFTOVERS.facts()["iscsi"]["hosts"]), phase)

    def test_a_host_whose_instance_manager_is_not_running_yet_is_not_judged(self):
        probes = {"node-1": {"iscsi": [session(1, "192.0.2.40")]}}
        with Cluster(ims=[im("node-1", "", phase="Pending"), im("node-2", "192.0.2.42")], probes=probes):
            self.assertEqual([], LEFTOVERS.facts()["iscsi"]["hosts"])

    def test_a_probe_that_reports_no_sessions_says_nothing(self):
        with Cluster(ims=[im("node-1", "192.0.2.41")], probes={"node-1": {"iscsi": []}}):
            found = LEFTOVERS.facts()["iscsi"]
        self.assertEqual(("ok", []), (found["state"], found["hosts"]))

    def test_unread_instance_managers_judge_no_session(self):
        probes = {"node-1": {"iscsi": [session(1, "192.0.2.40")]}}
        with Cluster(probes=probes, fail={PODS}):
            found = LEFTOVERS.facts()["iscsi"]
        self.assertEqual(("unknown", []), (found["state"], found["hosts"]))

    def test_no_probe_reading_is_no_verdict(self):
        with Cluster(ims=[im("node-1", "192.0.2.41")]):
            self.assertEqual("n/a", LEFTOVERS.facts()["iscsi"]["state"])


class Route(unittest.TestCase):
    def test_viewers_read_it(self):
        self.assertEqual("viewer", LEFTOVERS.ROUTES[("GET", "/api/storage/leftovers")][0])
        self.assertIn("homestead_storage_leftovers", homestead_routes.MODULES)


if __name__ == "__main__":
    unittest.main()
