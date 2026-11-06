"""What keeps a host from draining, named before the drain starts.

A host restart's drain can stall on things Homestead never looked at: Longhorn
orphan records whose leftover engine process keeps the host's
instance-manager (and its disruption budget at 0) alive, an instance-manager
that still lists engines which now run on another host, and a pod stuck
terminating that keeps its volume attached there. homestead_drain_blockers
reads them for one host and says each one; it changes nothing.

The Longhorn objects come from tests/fixtures/longhorn_v1_13/, each built from
Longhorn v1.13.0's own source (cited in the file's "_source")."""
import calendar
import copy
import json
import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import homestead_drain_blockers as BLOCKERS
from fakes.kube import Kube

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "longhorn_v1_13"
LH = "/apis/longhorn.io/v1beta2/namespaces/longhorn-system"
NOW = calendar.timegm((2026, 11, 1, 12, 0, 0))


def stamp(seconds):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(seconds))


def fixture(name, **changes):
    """A Longhorn v1.13.0 object from the fixtures, with dotted fields changed."""
    obj = copy.deepcopy(json.loads((FIXTURES / f"{name}.json").read_text())["object"])
    for dotted, value in changes.items():
        *path, last = dotted.split("__")
        place = obj
        for part in path:
            place = place.setdefault(part, {})
        place[last] = value
    return obj


def engine_orphan(volume, n):
    engine = f"{volume}-e-0"
    orphan = fixture("orphan_engine_instance")
    orphan["metadata"].update(name=f"orphan-{n:064x}", uid=f"orphan-uid-{n}", resourceVersion=str(6000 + n))
    orphan["metadata"]["labels"]["longhornengine"] = engine
    orphan["spec"]["parameters"]["InstanceName"] = engine
    return orphan


def engine(volume, node, state="running"):
    obj = fixture("engine", spec__volumeName=volume, spec__nodeID=node, status__ownerID=node,
                  status__currentState=state)
    obj["metadata"].update(name=f"{volume}-e-0", uid=f"engine-uid-{volume}")
    return obj


def instance_manager(node="node-a", engines=()):
    im = fixture("instancemanager", spec__nodeID=node)
    template = im["status"]["instanceEngines"]["vol-e-0"]
    im["status"]["instanceEngines"] = {}
    for name in engines:
        process = copy.deepcopy(template)
        process["spec"]["name"] = name
        im["status"]["instanceEngines"][name] = process
    return im


def pod(name="app-0", ns="apps", node="node-a", phase="Succeeded", deleting_at=None, claim="data-app-0",
        grace=30):
    meta = {"name": name, "namespace": ns, "uid": f"pod-uid-{name}"}
    if deleting_at is not None:
        meta.update(deletionTimestamp=stamp(deleting_at), deletionGracePeriodSeconds=grace,
                    finalizers=["example.com/hold"])
    return {"apiVersion": "v1", "kind": "Pod", "metadata": meta,
            "spec": {"nodeName": node, "terminationGracePeriodSeconds": grace,
                     "volumes": [{"name": "data", "persistentVolumeClaim": {"claimName": claim}}]},
            "status": {"phase": phase}}


def claim(name="data-app-0", ns="apps", volume="pv-1"):
    return {"apiVersion": "v1", "kind": "PersistentVolumeClaim", "metadata": {"name": name, "namespace": ns},
            "spec": {"volumeName": volume}, "status": {"phase": "Bound"}}


def attachment(node="node-a", volume="pv-1"):
    return {"apiVersion": "storage.k8s.io/v1", "kind": "VolumeAttachment", "metadata": {"name": f"csi-{volume}"},
            "spec": {"attacher": "driver.longhorn.io", "nodeName": node,
                     "source": {"persistentVolumeName": volume}},
            "status": {"attached": True}}


class DrainBlockerTests(unittest.TestCase):
    def kinds(self, rows):
        return sorted(row["kind"] for row in rows)

    def test_engine_orphans_on_this_host_whose_engine_runs_elsewhere_are_one_removable_row(self):
        volumes = ["vol-a", "vol-b", "vol-c"]
        objects = [engine_orphan(volume, n) for n, volume in enumerate(volumes, 1)]
        objects += [engine(volume, "node-b") for volume in volumes]
        # Today's case: the leftover processes are still on node-a's
        # instance-manager list too. They are the same blocker, said once.
        objects.append(instance_manager("node-a", engines=[f"{v}-e-0" for v in volumes]))
        with Kube(*objects):
            rows = BLOCKERS.for_host("node-a", now=NOW)
        self.assertEqual(["engine-orphans"], self.kinds(rows))
        row = rows[0]
        self.assertEqual("engine-orphans:node-a", row["id"])
        self.assertTrue(row["can_remove"])
        for volume in volumes:
            self.assertIn(volume, row["what"])
        self.assertIn("node-b", row["what"])
        self.assertEqual(3, len(row["records"]))
        self.assertEqual({"name": f"orphan-{1:064x}", "uid": "orphan-uid-1", "resourceVersion": "6001",
                          "engine": "vol-a-e-0", "engine_node": "node-b"}, row["records"][0])

    def test_an_engine_orphan_whose_engine_is_not_confirmed_elsewhere_is_not_removable(self):
        missing = engine_orphan("vol-a", 1)  # no Engine record at all
        here = engine_orphan("vol-b", 2)  # its engine runs on this very host
        stopped = engine_orphan("vol-c", 3)  # its engine on node-b is not running
        with Kube(missing, here, stopped, engine("vol-b", "node-a"), engine("vol-c", "node-b", state="stopped")):
            rows = BLOCKERS.for_host("node-a", now=NOW)
        self.assertEqual(["other-orphans"], self.kinds(rows))
        self.assertFalse(rows[0]["can_remove"])

    def test_an_orphan_record_being_removed_is_explained_not_removable(self):
        # Longhorn's orphans carry its finalizer: a DELETE only sets
        # deletionTimestamp, and the record stays until Longhorn has stopped
        # the process. The leftover is still listed on the instance-manager.
        removing = engine_orphan("vol-a", 1)
        removing["metadata"]["deletionTimestamp"] = stamp(NOW - 30)
        with Kube(removing, engine("vol-a", "node-b"), instance_manager("node-a", engines=["vol-a-e-0"])):
            rows = BLOCKERS.for_host("node-a", now=NOW)
        self.assertEqual(["removing-orphans"], self.kinds(rows), "said once, not again as an engine elsewhere")
        row = rows[0]
        self.assertFalse(row["can_remove"])
        self.assertEqual([], row["records"])
        self.assertIn("being removed", row["what"])
        self.assertIn("vol-a-e-0", row["what"])
        self.assertIn("review", row["fix"].lower())

    def test_replica_orphans_are_explained_not_removable(self):
        with Kube(fixture("orphan_replica_instance"), fixture("orphan_replica_data")):
            rows = BLOCKERS.for_host("node-a", now=NOW)
        self.assertEqual(["other-orphans"], self.kinds(rows))
        row = rows[0]
        self.assertEqual("other-orphans:node-a", row["id"])
        self.assertFalse(row["can_remove"])
        self.assertEqual([], row["records"])
        self.assertIn("vol-c-r-1a2b3c4d", row["what"])
        self.assertIn("vol-d-9f8e7d6c", row["what"])
        self.assertIn("replica-data", row["fix"])
        self.assertIn("deletes", row["fix"])
        self.assertIn("on-disk copy", row["fix"])

    def test_orphans_on_another_host_are_not_this_hosts(self):
        data = fixture("orphan_replica_data", spec__nodeID="node-b")
        instance = engine_orphan("vol-a", 1)
        instance["spec"]["nodeID"] = "node-b"
        with Kube(data, instance, engine("vol-a", "node-c")):
            self.assertEqual([], BLOCKERS.for_host("node-a", now=NOW))

    def test_instance_manager_engines_that_run_elsewhere_are_named(self):
        with Kube(instance_manager("node-a", engines=["vol-e-0"]), fixture("engine")):
            rows = BLOCKERS.for_host("node-a", now=NOW)
        self.assertEqual(["engines-elsewhere"], self.kinds(rows))
        row = rows[0]
        self.assertEqual("engines-elsewhere:node-a", row["id"])
        self.assertFalse(row["can_remove"])
        self.assertIn("vol-e-0", row["what"])
        self.assertIn("node-b", row["what"])

    def test_an_engine_listed_and_running_here_is_a_real_attachment_not_a_blocker(self):
        with Kube(instance_manager("node-a", engines=["vol-e-0"]), fixture("engine", spec__nodeID="node-a",
                                                                          status__ownerID="node-a")):
            self.assertEqual([], BLOCKERS.for_host("node-a", now=NOW))

    def test_a_pod_stuck_terminating_and_its_attachment_are_named(self):
        with Kube(pod(deleting_at=NOW - 20 * 60), claim(), attachment()):
            rows = BLOCKERS.for_host("node-a", now=NOW)
        self.assertEqual(["stuck-pod"], self.kinds(rows))
        row = rows[0]
        self.assertEqual("stuck-pod:apps/app-0", row["id"])
        self.assertFalse(row["can_remove"])
        self.assertIn("apps/app-0", row["what"])
        self.assertIn("20 min", row["what"])
        self.assertIn("pv-1", row["what"])

    def test_a_pod_inside_its_grace_period_is_not_stuck(self):
        # The API server sets deletionTimestamp to the time the grace period
        # ends: a pod deleted with 3600 s of grace carries one in the future.
        with Kube(pod(phase="Running", deleting_at=NOW + 10 * 60, grace=3600), claim(), attachment()):
            self.assertEqual([], BLOCKERS.for_host("node-a", now=NOW))

    def test_a_pod_only_just_past_its_grace_period_is_not_stuck_yet(self):
        with Kube(pod(deleting_at=NOW - (BLOCKERS.STUCK_AFTER - 60)), claim(), attachment()):
            self.assertEqual([], BLOCKERS.for_host("node-a", now=NOW))
        self.assertEqual(900, BLOCKERS.STUCK_AFTER)

    def test_a_stuck_pod_on_another_host_is_not_this_hosts(self):
        with Kube(pod(node="node-b", deleting_at=NOW - 20 * 60), claim(), attachment(node="node-b")):
            self.assertEqual([], BLOCKERS.for_host("node-a", now=NOW))

    def test_unreadable_longhorn_lists_are_said_not_skipped(self):
        with Kube(engine_orphan("vol-a", 1), engine("vol-a", "node-b"),
                  instance_manager("node-a", engines=["vol-x-e-0"]), engine("vol-x", "node-b"),
                  pod(deleting_at=NOW - 20 * 60), claim(), attachment()) as kube:
            kube.fail(LH + "/orphans", 500)
            rows = BLOCKERS.for_host("node-a", now=NOW)
        self.assertEqual(["engines-elsewhere", "stuck-pod", "unread"], self.kinds(rows))
        unread = [row for row in rows if row["kind"] == "unread"]
        self.assertEqual(1, len(unread))
        self.assertIn("couldn't check Longhorn orphan records", unread[0]["what"])
        self.assertEqual("unread:orphans", unread[0]["id"])
        self.assertFalse(unread[0]["can_remove"])

    def test_a_forbidden_list_is_unread_too(self):
        # RBAC refusing a list answers 403, as a real API server does.
        with Kube(instance_manager("node-a", engines=["vol-e-0"]), fixture("engine")) as kube:
            kube.fail(LH + "/engines", 403)
            rows = BLOCKERS.for_host("node-a", now=NOW)
        self.assertEqual(["unread"], self.kinds(rows))
        self.assertIn("engine", rows[0]["what"].lower())

    def test_no_longhorn_at_all_is_not_a_blocker(self):
        with Kube(absent_groups=("longhorn.io",)):
            self.assertEqual([], BLOCKERS.for_host("node-a", now=NOW))

    def test_an_engine_listed_here_with_no_engine_record_is_named(self):
        # A process the instance-manager lists whose Engine CR is gone keeps
        # its budget at 0 as well, until Longhorn records it as an orphan.
        with Kube(instance_manager("node-a", engines=["vol-gone-e-0"]), engine("vol", "node-a")):
            rows = BLOCKERS.for_host("node-a", now=NOW)
        self.assertEqual(["engines-elsewhere"], self.kinds(rows))
        self.assertFalse(rows[0]["can_remove"])
        self.assertIn("vol-gone-e-0 (no engine record)", rows[0]["what"])

    def test_a_404_on_pods_claims_or_attachments_is_unread_not_absent(self):
        for key, path in (("pods", "/api/v1/pods"), ("claims", "/api/v1/persistentvolumeclaims"),
                          ("attachments", "/apis/storage.k8s.io/v1/volumeattachments")):
            with self.subTest(key), Kube(pod(phase="Running"), claim(), attachment()) as kube:
                kube.fail(path, 404)
                rows = BLOCKERS.for_host("node-a", now=NOW)
                self.assertEqual([f"unread:{key}"], [row["id"] for row in rows])

    def test_an_attachment_with_a_null_source_does_not_break_the_check(self):
        bare = attachment()
        bare["spec"]["source"] = None
        with Kube(pod(deleting_at=NOW - 20 * 60), claim(), bare):
            rows = BLOCKERS.for_host("node-a", now=NOW)
        self.assertEqual(["stuck-pod"], self.kinds(rows))

    def test_an_attachment_here_whose_pod_is_gone_is_named(self):
        with Kube(claim(), attachment(), pod(name="other", phase="Running", claim="something-else")):
            rows = BLOCKERS.for_host("node-a", now=NOW)
        self.assertEqual(["stuck-attachment"], self.kinds(rows))
        row = rows[0]
        self.assertEqual("stuck-attachment:csi-pv-1", row["id"])
        self.assertFalse(row["can_remove"])
        self.assertIn("pv-1", row["what"])
        self.assertIn("no pod", row["what"])

    def test_an_attachment_a_generic_ephemeral_volume_uses_is_not_stuck(self):
        # A pod's ephemeral volume is never listed under persistentVolumeClaim:
        # its claim is "<pod name>-<volume name>".
        running = pod(phase="Running")
        running["spec"]["volumes"] = [{"name": "scratch", "ephemeral": {"volumeClaimTemplate": {
            "spec": {"accessModes": ["ReadWriteOnce"], "storageClassName": "longhorn",
                     "resources": {"requests": {"storage": "1Gi"}}}}}}]
        with Kube(running, claim(name="app-0-scratch", volume="pv-2"), attachment(volume="pv-2")):
            self.assertEqual([], BLOCKERS.for_host("node-a", now=NOW))

    def test_an_attachment_already_detaching_is_not_stuck(self):
        # A normal pod move: the attachment carries deletionTimestamp while
        # the CSI driver detaches.
        detaching = attachment()
        detaching["metadata"].update(deletionTimestamp=stamp(NOW - 5), finalizers=["external-attacher/driver-longhorn-io"])
        with Kube(claim(), detaching):
            self.assertEqual([], BLOCKERS.for_host("node-a", now=NOW))

    def test_an_attachment_a_running_pod_uses_is_not_stuck(self):
        with Kube(pod(phase="Running"), claim(), attachment()):
            self.assertEqual([], BLOCKERS.for_host("node-a", now=NOW))

    def test_a_stuck_pod_says_when_its_volumes_could_not_be_checked(self):
        for path in ("/api/v1/persistentvolumeclaims", "/apis/storage.k8s.io/v1/volumeattachments"):
            with self.subTest(path), Kube(pod(deleting_at=NOW - 20 * 60), claim(), attachment()) as kube:
                kube.fail(path, 500)
                rows = BLOCKERS.for_host("node-a", now=NOW)
                stuck = [row for row in rows if row["kind"] == "stuck-pod"]
                self.assertEqual(1, len(stuck))
                self.assertIn("its volumes couldn't be checked", stuck[0]["what"])

    def test_a_clean_host_has_no_rows(self):
        with Kube(instance_manager("node-a", engines=[]), engine("vol", "node-a"),
                  pod(phase="Running"), claim(), attachment()):
            self.assertEqual([], BLOCKERS.for_host("node-a", now=NOW))

    def test_rows_have_the_promised_shape(self):
        with Kube(engine_orphan("vol-a", 1), engine("vol-a", "node-b"), fixture("orphan_replica_data"),
                  pod(deleting_at=NOW - 20 * 60), claim(), attachment()):
            rows = BLOCKERS.for_host("node-a", now=NOW)
        for row in rows:
            self.assertEqual({"id", "kind", "what", "why", "fix", "can_remove", "records"}, set(row))
            self.assertTrue(row["what"] and row["why"] and row["fix"])
        self.assertEqual(["engine-orphans", "other-orphans", "stuck-pod"], [row["kind"] for row in rows])


if __name__ == "__main__":
    unittest.main()
