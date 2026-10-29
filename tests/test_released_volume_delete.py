"""#932 follow-up: deleting a Released volume whose claim name is in use again.

A Retain class keeps a volume after its claim is deleted. When an app is
deployed again with the same claim name, a new claim and a new volume take
the name, and the old one stays Released. The reviewed delete found the claim
by name, so it reviewed the new claim and refused. Asked for by its PV, the
review now shows the Released volume beside the one the app uses now, and the
delete changes only that PV, pinned by name, UID and version. The current
claim, its volume and its Longhorn volume are never written."""
import sys
import urllib.error
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import homestead_kube as K
import homestead_operations as OPS
import homestead_route_policy as ROUTE_POLICY
import homestead_volumes as VOLUMES
from fakes.kube import Kube

LH = "/apis/longhorn.io/v1beta2/namespaces/longhorn-system"
GROUPS = ("apps", "batch", "longhorn.io", "storage.k8s.io")


def pv(name, uid, phase, claim_uid, handle=None):
    return {"apiVersion": "v1", "kind": "PersistentVolume",
            "metadata": {"name": name, "uid": uid, "resourceVersion": "7"},
            "spec": {"persistentVolumeReclaimPolicy": "Retain", "storageClassName": "longhorn",
                     "capacity": {"storage": "20Gi"},
                     "claimRef": {"namespace": "lab", "name": "notes-data", "uid": claim_uid},
                     "csi": {"driver": "driver.longhorn.io", "volumeHandle": handle or name}},
            "status": {"phase": phase}}


def longhorn(name, state, pv_name):
    return {"apiVersion": "longhorn.io/v1beta2", "kind": "Volume",
            "metadata": {"name": name, "namespace": "longhorn-system", "uid": "lh-" + name, "resourceVersion": "5"},
            "spec": {"numberOfReplicas": 2},
            "status": {"state": state, "currentNodeID": "node-1" if state == "attached" else "",
                       "kubernetesStatus": {"namespace": "lab", "pvcName": "notes-data", "pvName": pv_name}}}


def claim(volume, uid="claim-new", phase="Bound"):
    return {"apiVersion": "v1", "kind": "PersistentVolumeClaim",
            "metadata": {"name": "notes-data", "namespace": "lab", "uid": uid, "resourceVersion": "9"},
            "spec": {"volumeName": volume, "storageClassName": "longhorn"}, "status": {"phase": phase}}


APP = {"apiVersion": "v1", "kind": "Pod",
       "metadata": {"name": "notes-7d8f", "namespace": "lab"},
       "spec": {"nodeName": "node-1", "containers": [{"name": "notes", "volumeMounts": [{"name": "data", "mountPath": "/data"}]}],
                "volumes": [{"name": "data", "persistentVolumeClaim": {"claimName": "notes-data"}}]},
       "status": {"phase": "Running"}}


def cluster(*changes):
    """The old Released volume pvc-old and the app's current claim notes-data,
    bound to pvc-new, both once named notes-data."""
    objects = {"old": pv("pvc-old", "pv-old-uid", "Released", "claim-old"),
               "new": pv("pvc-new", "pv-new-uid", "Bound", "claim-new"),
               "lh-old": longhorn("pvc-old", "detached", "pvc-old"),
               "lh-new": longhorn("pvc-new", "attached", "pvc-new"),
               "claim": claim("pvc-new"), "app": APP}
    for change in changes:
        change(objects)
    return Kube(*[o for o in objects.values() if o], groups=GROUPS)


def bind():
    VOLUMES.bind(K.kget, K.ksend, lambda _: [], lambda _: [], {}, {"kube-system"}, "lab")


def plan():
    return VOLUMES.deletion_plan("lab", "notes-data", "pvc-old", "pvc-old")


CFG = dict(namespace="lab", name="notes-data", volume="pvc-old", pv="pvc-old", uid="lh-pvc-old",
           pv_uid="pv-old-uid", confirmation="pvc-old", action="delete_data")


class Case(unittest.TestCase):
    def setUp(self):
        # Writes take the jobs' lock (the storage guard), kept in a folder of its own.
        self.enterContext(mock.patch.object(OPS, "DATA_DIR", self.enterContext(tempfile.TemporaryDirectory())))


class ReusedClaimNameReview(Case):
    def test_the_review_sets_the_released_volume_beside_the_one_the_app_uses_now(self):
        with cluster():
            bind()
            p = plan()
        self.assertFalse(p["blocked"], p["blocking_reasons"])
        self.assertTrue(p["released"])
        self.assertEqual({"name": "pvc-old", "uid": "pv-old-uid", "phase": "Released", "claim_uid": "claim-old",
                          "longhorn": "pvc-old"},
                         {k: p["pv"][k] for k in ("name", "uid", "phase", "claim_uid", "longhorn")})
        self.assertEqual({"claim_uid": "claim-new", "pv": "pvc-new", "pv_uid": "pv-new-uid", "phase": "Bound",
                          "longhorn": "pvc-new"},
                         {k: p["current"][k] for k in ("claim_uid", "pv", "pv_uid", "phase", "longhorn")})
        # The app mounts the claim name, which is the current volume: shown, not blocking.
        self.assertEqual([], p["consumers"])
        self.assertEqual(["notes-7d8f"], [c["name"] for c in p["current"]["consumers"]])
        self.assertEqual("pvc-old", p["confirm"])
        self.assertFalse(p["actions"]["delete_claim"]["enabled"])
        self.assertTrue(p["actions"]["delete_data"]["enabled"])

    def test_the_delete_changes_only_that_pv_pinned_by_name_uid_and_version(self):
        with cluster() as kube:
            bind()
            VOLUMES.delete(CFG)
            writes = [(m, p) for m, p, *_ in kube.writes]
            self.assertEqual([("PATCH", "/api/v1/persistentvolumes/pvc-old")], writes)
            body = kube.writes[0][2]
            tests = {op["path"]: op["value"] for op in body if op["op"] == "test"}
            self.assertEqual({"/metadata/uid": "pv-old-uid", "/metadata/resourceVersion": "7", "/status/phase": "Released",
                              "/spec/claimRef/uid": "claim-old", "/spec/csi/volumeHandle": "pvc-old"}, tests)
            self.assertEqual("Delete", kube.object("/api/v1/persistentvolumes/pvc-old")["spec"]["persistentVolumeReclaimPolicy"])
            self.assertEqual("Retain", kube.object("/api/v1/persistentvolumes/pvc-new")["spec"]["persistentVolumeReclaimPolicy"])
            self.assertIsNotNone(kube.object("/api/v1/namespaces/lab/persistentvolumeclaims/notes-data"))

    def test_the_confirmation_is_the_released_volume_name_not_the_claim_name(self):
        with cluster() as kube:
            bind()
            with self.assertRaises(ValueError):
                VOLUMES.delete({**CFG, "confirmation": "notes-data"})
            self.assertEqual([], kube.writes)

    def test_a_pv_changed_since_the_review_is_not_written(self):
        with cluster() as kube:
            bind()
            for cfg in ({**CFG, "pv_uid": "other"}, {**CFG, "uid": "other"}):
                with self.subTest(cfg=cfg), self.assertRaises(ValueError):
                    VOLUMES.delete(cfg)
            self.assertEqual([], kube.writes)


class TheCurrentVolumeIsNeverDeleted(Case):
    def assertRefused(self, *changes, why, volume="pvc-old", pv_name="pvc-old"):
        """Refused for the reason named (a blocker in the review, or the review
        itself refusing), and nothing written."""
        with cluster(*changes) as kube:
            bind()
            try:
                p = VOLUMES.deletion_plan("lab", "notes-data", volume, pv_name)
                self.assertTrue(p["blocked"], p)
                self.assertFalse(p["actions"]["delete_data"]["enabled"])
                self.assertTrue(any(why in reason for reason in p["blocking_reasons"]), p["blocking_reasons"])
            except ValueError as error:
                self.assertIn(why, str(error))
            with self.assertRaises((ValueError, PermissionError)):
                VOLUMES.delete({**CFG, "volume": volume, "pv": pv_name, "confirmation": pv_name,
                                "uid": "lh-" + volume, "pv_uid": "pv-new-uid" if pv_name == "pvc-new" else "pv-old-uid"})
            self.assertEqual([], kube.writes)

    def test_asking_for_the_pv_the_app_uses_now_is_refused(self):
        self.assertRefused(volume="pvc-new", pv_name="pvc-new", why="This is the volume the claim uses now")

    def test_a_pv_that_is_not_released_is_refused(self):
        self.assertRefused(lambda o: o["old"]["status"].update(phase="Bound"), why="not Released")

    def test_a_current_claim_bound_to_that_pv_is_refused(self):
        self.assertRefused(lambda o: o.update(claim=claim("pvc-old")), why="This is the volume the claim uses now")

    def test_a_current_claim_with_the_old_claim_uid_is_refused(self):
        self.assertRefused(lambda o: o.update(claim=claim("pvc-new", uid="claim-old")), why="claim reference is the current claim's")

    def test_a_current_claim_not_bound_yet_is_refused(self):
        self.assertRefused(lambda o: o.update(claim=claim("", phase="Pending")), why="not bound yet")

    def test_a_current_volume_on_the_same_longhorn_volume_is_refused(self):
        self.assertRefused(lambda o: o["new"]["spec"]["csi"].update(volumeHandle="pvc-old"), why="uses this Longhorn volume now")

    def test_a_current_volume_that_cannot_be_read_is_refused(self):
        self.assertRefused(lambda o: o.update(new=None), why="Impact inventory is incomplete")

    def test_a_longhorn_volume_still_attached_is_refused(self):
        self.assertRefused(lambda o: o["lh-old"]["status"].update(state="attached", currentNodeID="node-2"), why="fully detached")

    def test_a_pv_of_another_claim_is_refused(self):
        self.assertRefused(lambda o: o["old"]["spec"]["claimRef"].update(name="other"), why="was not that claim's")

    def test_a_pv_on_another_longhorn_volume_is_refused(self):
        self.assertRefused(lambda o: o["old"]["spec"]["csi"].update(volumeHandle="pvc-other"), why="not that Longhorn volume's")

    def test_a_longhorn_volume_last_bound_to_another_pv_is_refused(self):
        self.assertRefused(lambda o: o["lh-old"]["status"]["kubernetesStatus"].update(pvName="pvc-new"), why="another persistent volume")

    def test_a_longhorn_volume_last_bound_to_another_claim_is_refused(self):
        self.assertRefused(lambda o: o["lh-old"]["status"]["kubernetesStatus"].update(pvcName="other"), why="another claim")

    def test_a_longhorn_volume_that_is_gone_is_refused_plainly(self):
        self.assertRefused(lambda o: o.update(**{"lh-old": None}), why="refresh the list")

    def test_a_forged_plan_naming_the_current_pv_is_never_written(self):
        """delete_orphan checks again, whatever the plan says."""
        with cluster() as kube:
            bind()
            p = plan()
            for forged in ({**p, "pv": {**p["pv"], "name": "pvc-new"}}, {**p, "pv": {**p["pv"], "uid": "pv-new-uid"}},
                           {**p, "longhorn": {**p["longhorn"], "name": "pvc-new"}}, {**p, "pv": {**p["pv"], "claim_uid": ""}}):
                with self.subTest(forged=forged["pv"]), self.assertRaises(PermissionError):
                    VOLUMES.delete_orphan(forged, {**CFG, "pv_uid": forged["pv"]["uid"]})
            self.assertEqual([], kube.writes)

    def test_the_released_pv_changing_under_the_delete_stops_the_write(self):
        """The patch carries its own tests: the API server refuses it if the PV
        was replaced, rebound or changed after the review read it."""
        with cluster() as kube:
            bind()
            original = K.kget

            def changing(path, *a, **k):
                found = original(path, *a, **k)
                if path == "/api/v1/persistentvolumes/pvc-old":
                    kube.add({**pv("pvc-old", "pv-old-uid", "Released", "claim-old"),
                              "metadata": {"name": "pvc-old", "uid": "pv-old-uid", "resourceVersion": "8"}})
                return found
            VOLUMES.bind(changing, K.ksend, lambda _: [], lambda _: [], {}, {"kube-system"}, "lab")
            with self.assertRaises(urllib.error.HTTPError) as refused:
                VOLUMES.delete(CFG)
            self.assertEqual(422, refused.exception.code)
            self.assertEqual([("PATCH", "/api/v1/persistentvolumes/pvc-old")], [(m, p) for m, p, *_ in kube.writes])
            self.assertEqual("Retain", kube.object("/api/v1/persistentvolumes/pvc-old")["spec"]["persistentVolumeReclaimPolicy"])


class TheJobIsTheReleasedVolumes(Case):
    """The job recorded for the delete, and the storage guard, are the Released
    PV's: the claim name is the volume in use's, and must not be fenced or named."""

    def test_the_job_names_the_released_pv_and_does_not_hold_the_claim_in_use(self):
        import homestead_storage_conflicts as CONFLICTS
        with cluster():
            bind()
            VOLUMES.delete_reviewed(CFG)
        job = OPS.stored()[-1]
        self.assertEqual("Delete Released volume pvc-old", job["title"])
        self.assertEqual(("PersistentVolume", "pvc-old"), (job["resource"]["kind"], job["resource"]["name"]))
        targets = CONFLICTS.targets(job["kind"], job["ref"], job.get("resource"))
        self.assertNotIn(("claim", "lab", "notes-data"), targets)
        self.assertIn(("pv", "pvc-old"), targets)

    def test_a_storage_move_of_the_claim_name_still_holds_the_released_volume_until_it_ends(self):
        """Left conservative on purpose: the guard matches a PV by its live
        claimRef, which a Released volume shares with the claim in use, so a
        move of that claim holds the old volume's delete until the move ends."""
        import homestead_storage_guard as GUARD
        from fakes.job_store import Store
        for ref in ({"namespace": "lab", "claim": "notes-data"}, {"namespace": "lab", "old_pv": "pvc-old"}):
            move = {"id": "m1", "kind": "reclass", "status": "running", "ref": ref}
            with self.subTest(ref=ref), cluster():
                bind()
                self.assertTrue(GUARD.review(plan(), Store(read=lambda: [move]), K.kget)["blocked"])


class WithoutAReusedName(Case):
    def test_with_no_claim_of_that_name_the_existing_orphan_review_answers_for_that_pv(self):
        with cluster(lambda o: o.update(claim=None, new=None, app=None)):
            bind()
            p = plan()
        self.assertTrue(p["orphan"])
        self.assertFalse(p["blocked"], p["blocking_reasons"])
        self.assertEqual("pvc-old", p["pv"]["name"])

    def test_with_no_claim_of_that_name_another_pv_of_that_volume_is_refused(self):
        with cluster(lambda o: o.update(claim=None, new=None, app=None)):
            bind()
            with self.assertRaises(ValueError):
                VOLUMES.deletion_plan("lab", "notes-data", "pvc-old", "pvc-elsewhere")


class WhoMay(unittest.TestCase):
    def test_only_an_admin_may_delete_and_anyone_signed_in_may_review(self):
        self.assertEqual("admin", ROUTE_POLICY.role("/api/volumes/delete", "POST"))
        self.assertEqual("viewer", ROUTE_POLICY.role("/api/volumes/delete-plan", "GET"))

    def test_the_review_route_passes_the_pv_it_was_asked_for(self):
        seen = []
        handler = VOLUMES.ROUTES[("GET", "/api/volumes/delete-plan")][1]
        saved = VOLUMES.deletion_plan, VOLUMES._review
        VOLUMES.deletion_plan, VOLUMES._review = (lambda *a: seen.append(a) or {}), (lambda p: p)
        try:
            handler(type("R", (), {"query": {"ns": ["lab"], "name": ["notes-data"], "volume": ["pvc-old"], "pv": ["pvc-old"]}})())
        finally:
            VOLUMES.deletion_plan, VOLUMES._review = saved
        self.assertEqual([("lab", "notes-data", "pvc-old", "pvc-old")], seen)


if __name__ == "__main__":
    unittest.main()
