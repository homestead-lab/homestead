"""The volume a storage copy copies from, while the copy is not confirmed (#1525).

Moving an app's data saves the edit with the app stopped and its settings
already naming the new volume, then copies. Until the copy is confirmed (or
undone, or its hold released), the volume it copies from is the only whole
copy of the app's data, and undoing the copy needs that volume. The Deployment
carries the copy's mark and its settings from before the edit are in a Secret
beside it: the delete review refuses that volume and the Volumes page shows the
storage copy needs it. Only claim names are read from the Secret.
On the shared fake API (tests/fakes/kube.py)."""
import base64
import contextlib
import io
import json
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import homestead_auth as AUTH
import homestead_changes as CHANGES
import homestead_copy_job as flow
import homestead_kube as K
import homestead_restructure as restructure
import homestead_storage as STORAGE
import homestead_volumes as VOLUMES
import test_copy_job as base
from fakes.kube import Kube
from rebind import rebind

LH = "/apis/longhorn.io/v1beta2/namespaces/longhorn-system/volumes"
DEP = "/apis/apps/v1/namespaces/lab/deployments/app"
# A value from the app's settings before the edit: never shown anywhere.
PASSWORD = "pa" + "ss-" + "from-before-the-edit"


def claim(name, ns="lab"):
    return [{"apiVersion": "v1", "kind": "PersistentVolumeClaim",
             "metadata": {"name": name, "namespace": ns},
             "spec": {"volumeName": f"pv-{ns}-{name}", "storageClassName": "longhorn",
                      "accessModes": ["ReadWriteOnce"], "resources": {"requests": {"storage": "1Gi"}}},
             "status": {"phase": "Bound"}},
            {"apiVersion": "v1", "kind": "PersistentVolume", "metadata": {"name": f"pv-{ns}-{name}"},
             "spec": {"persistentVolumeReclaimPolicy": "Delete",
                      "csi": {"driver": "driver.longhorn.io", "volumeHandle": f"pv-{ns}-{name}"}}},
            {"apiVersion": "longhorn.io/v1beta2", "kind": "Volume",
             "metadata": {"name": f"pv-{ns}-{name}", "namespace": "longhorn-system"},
             "spec": {"numberOfReplicas": 1, "size": str(1024 ** 3)},
             "status": {"state": "detached", "robustness": "unknown", "actualSize": 1024,
                        "kubernetesStatus": {"namespace": ns, "pvcName": name, "pvStatus": "Bound"}}}]


def template(*claims, env=None):
    return {"metadata": {"labels": {"app": "app"}}, "spec": {
        "volumes": [{"name": f"v{n}", "persistentVolumeClaim": {"claimName": c}} for n, c in enumerate(claims)],
        "containers": [{"name": "main", "image": "example/app", "env": env or [],
                        "volumeMounts": [{"name": f"v{n}", "mountPath": f"/d{n}"} for n in range(len(claims))]}]}}


class Scene(unittest.TestCase):
    """App `app` in lab, held by a storage copy: its settings now name `new`
    and `keep`; before the edit they named `old` and `keep`."""

    def setUp(self):
        # The account store and change-author key the tests fill go back when each ends (CacheRatchet).
        self.enterContext(mock.patch.dict(AUTH._store_cache, {"at": 0, "data": None}, clear=True))
        self.enterContext(mock.patch.dict(CHANGES._known, clear=True))
        objects = [obj for name in ("old", "new", "keep") for obj in claim(name)]
        self.kube = Kube(*objects, absent_groups=("kubevirt.io",))
        self.kube.__enter__()
        self.addCleanup(self.kube.__exit__)
        self.kube.add({"apiVersion": "apps/v1", "kind": "Deployment",
                       "metadata": {"name": "app", "namespace": "lab", "annotations": {flow.MARKER: "copy-1", restructure.HELD: "1"}},
                       "spec": {"replicas": 0, "selector": {"matchLabels": {"app": "app"}},
                                "template": template("new", "keep")}})
        self.uid = self.kube.object(DEP)["metadata"]["uid"]
        before = {"apiVersion": "apps/v1", "kind": "Deployment", "metadata": {"name": "app", "namespace": "lab"},
                  "spec": {"replicas": 1, "template": template("old", "keep", env=[{"name": "DB_PASSWORD", "value": PASSWORD}])}}
        self.kube.add(flow.earlier_secret("lab", "app", self.uid, flow.earlier(before)))
        self.record = f"/api/v1/namespaces/lab/secrets/{flow.earlier_name(self.uid)}"
        rebind(self, VOLUMES, K.kget, K.ksend, lambda name: [], lambda name: [], {},
               {"kube-system", "longhorn-system"}, "lab")
        rebind(self, STORAGE, K.kget, K.ksend, lambda: [], None, lambda stamp: 0, {}, set(), "lab")

    def plan(self, name, ns="lab"):
        return VOLUMES.deletion_plan(ns, name)

    def used_by(self):
        return {(row["namespace"], row["pvc_name"]): row["used_by"] for row in STORAGE.get_volumes()}

    def unmark(self):
        dep = self.kube.object(DEP)
        dep["metadata"]["annotations"].pop(flow.MARKER)
        self.kube.add(dep)

    def secret_reads(self):
        return [path for path, _ in self.kube.reads if "/secrets" in path]


class DeleteReviewTests(Scene):
    def test_the_review_and_the_delete_refuse_the_volume_a_held_copy_copies_from(self):
        plan = self.plan("old")
        self.assertTrue(plan["blocked"])
        held = [r for r in plan["blocking_reasons"] if "storage copy" in r]
        self.assertEqual(1, len(held), plan["blocking_reasons"])
        for words in ("app", "has not finished", "data from before the edit", "Move app's data", "Jobs"):
            self.assertIn(words, held[0])
        self.assertFalse(plan["actions"]["delete_claim"]["enabled"])
        self.assertFalse(plan["actions"]["delete_data"]["enabled"])
        for action in ("delete_claim", "delete_data"):
            with self.assertRaisesRegex(PermissionError, "storage copy of app has not finished"):
                VOLUMES.delete({"namespace": "lab", "name": "old", "action": action,
                                "uid": plan["uid"], "confirmation": "old"})
        self.assertEqual([], self.kube.writes)
        self.assertIsNotNone(self.kube.object("/api/v1/namespaces/lab/persistentvolumeclaims/old"))

    def test_a_volume_the_app_still_names_gets_no_storage_copy_reason(self):
        reasons = self.plan("keep")["blocking_reasons"]
        self.assertTrue(reasons)    # its stopped Deployment names it, as before
        self.assertFalse([r for r in reasons if "storage copy" in r], reasons)
        self.assertFalse([r for r in self.plan("new")["blocking_reasons"] if "storage copy" in r])

    def test_once_the_mark_is_gone_it_is_an_ordinary_unused_volume(self):
        self.unmark()       # copy confirmed, undone or its hold released
        plan = self.plan("old")
        self.assertFalse(plan["blocked"], plan["blocking_reasons"])
        self.assertEqual([], self.secret_reads())   # no mark, no read of the record

    def test_a_mark_added_after_the_review_refuses_the_delete(self):
        self.unmark()
        plan = self.plan("old")
        self.assertFalse(plan["blocked"], plan["blocking_reasons"])
        dep = self.kube.object(DEP)
        dep["metadata"]["annotations"][flow.MARKER] = "copy-1"
        self.kube.add(dep)
        with self.assertRaisesRegex(PermissionError, "storage copy of app has not finished"):
            VOLUMES.delete({"namespace": "lab", "name": "old", "action": "delete_claim",
                            "uid": plan["uid"], "confirmation": "old"})
        self.assertEqual([], self.kube.writes)

    def test_a_running_copy_still_blocks_through_its_job(self):
        self.kube.add({"apiVersion": "batch/v1", "kind": "Job", "metadata": {"name": "homestead-copy-app", "namespace": "lab"},
                       "spec": {"template": {"spec": template("old", "new")["spec"]}}, "status": {"active": 1}})
        reasons = self.plan("old")["blocking_reasons"]
        self.assertTrue([r for r in reasons if "Kubernetes object reference" in r], reasons)
        self.assertTrue([r for r in reasons if "storage copy" in r], reasons)

    def test_a_held_copy_in_another_namespace_does_not_block_a_same_name_claim(self):
        for obj in claim("old", ns="media"):
            self.kube.add(obj)
        plan = self.plan("old", ns="media")
        self.assertFalse(plan["blocked"], plan["blocking_reasons"])
        self.assertEqual([], self.used_by()[("media", "old")])

    def test_a_storage_class_move_that_holds_an_app_is_not_read_as_a_copy(self):
        # A storage class move (reclass) marks the apps it stops with the same
        # annotation, with no hold count and no record of before: they still
        # name their volume, so nothing is missing.
        self.kube.discard(self.record)
        dep = self.kube.object(DEP)
        dep["metadata"]["annotations"].pop(restructure.HELD)
        self.kube.add(dep)
        for obj in claim("spare"):
            self.kube.add(obj)
        plan = self.plan("spare")
        self.assertFalse(plan["blocked"], plan["blocking_reasons"])
        self.assertEqual([], plan["warnings"])
        self.assertEqual([], self.used_by()[("lab", "spare")])
        self.assertEqual([], self.secret_reads())

    def test_a_statefulset_with_the_mark_is_not_read_as_a_copy(self):
        self.unmark()
        self.kube.add({"apiVersion": "apps/v1", "kind": "StatefulSet",
                       "metadata": {"name": "db", "namespace": "lab", "annotations": {flow.MARKER: "copy-2"}},
                       "spec": {"replicas": 0, "template": template("new")}})
        uid = self.kube.object("/apis/apps/v1/namespaces/lab/statefulsets/db")["metadata"]["uid"]
        self.kube.add(flow.earlier_secret("lab", "db", uid, flow.earlier({"spec": {"replicas": 1, "template": template("old")}})))
        self.assertFalse(self.plan("old")["blocked"])
        self.assertEqual([], self.used_by()[("lab", "old")])
        self.assertEqual([], self.secret_reads())


class RecordUnreadTests(Scene):
    def test_a_missing_record_blocks_and_says_what_could_not_be_read(self):
        self.kube.discard(self.record)
        plan = self.plan("old")
        self.assertTrue(plan["blocked"])
        self.assertIn("the app dependency list is incomplete", plan["blocking_reasons"])
        self.assertTrue([w for w in plan["warnings"] if "storage copy of app" in w and "is missing" in w], plan["warnings"])
        self.assertFalse(plan["actions"]["delete_data"]["enabled"])

    def test_the_warning_says_how_to_release_the_hold(self):
        self.kube.discard(self.record)
        warning = [w for w in self.plan("old")["warnings"] if "storage copy of app" in w][0]
        for words in ("Move app's data", "Check outcome", "Release hold and stop tracking",
                      "app was replaced", "homestead.io/storage-copy-job", "homestead.io/restructure-replicas",
                      "stays stopped until it is scaled up"):
            self.assertIn(words, warning)

    def test_an_app_created_again_with_the_mark_on_it_is_blocked_and_told_to_remove_the_mark(self):
        # Applied again from a saved manifest: a new uid, the mark still on it,
        # and no record for that uid (the old one went with the old app).
        dep = self.kube.object(DEP)
        self.kube.discard(DEP)
        self.assertIsNone(self.kube.object(self.record))     # owned by the old app
        for field in ("uid", "resourceVersion", "creationTimestamp"):
            dep["metadata"].pop(field, None)
        self.kube.add(dep)
        self.assertNotEqual(self.uid, self.kube.object(DEP)["metadata"]["uid"])
        plan = self.plan("old")
        self.assertTrue(plan["blocked"])
        warning = [w for w in plan["warnings"] if "storage copy of app" in w][0]
        self.assertIn("is missing", warning)
        self.assertIn("remove the annotations homestead.io/storage-copy-job and homestead.io/restructure-replicas "
                      "from the app in Kubernetes", warning)

    def test_a_record_not_owned_by_the_app_is_not_trusted(self):
        secret = self.kube.object(self.record)
        secret["metadata"]["ownerReferences"][0]["uid"] = "another-uid"
        self.kube.add(secret)
        plan = self.plan("old")
        self.assertTrue(plan["blocked"])
        self.assertIn("the app dependency list is incomplete", plan["blocking_reasons"])
        self.assertFalse([r for r in plan["blocking_reasons"] if "storage copy" in r])   # its names are not used
        self.assertTrue([w for w in plan["warnings"] if "belongs to another app" in w], plan["warnings"])
        self.assertIsNone(self.used_by()[("lab", "old")])

    def test_an_install_does_not_take_over_a_volume_there_meanwhile(self):
        import server
        for obj in claim("spare"):
            self.kube.add(obj)
        self.kube.discard(self.record)
        with mock.patch.object(server.STORAGE_ROUTES, "kget", K.kget):
            with self.assertRaisesRegex(ValueError, "could not check what uses it"):
                server.STORAGE_ROUTES.ensure_claim("lab", "spare", 1)

    def test_an_unreadable_record_blocks_with_its_http_status(self):
        self.kube.fail(self.record, 403, times=5)
        plan = self.plan("old")
        self.assertTrue(plan["blocked"])
        self.assertTrue([w for w in plan["warnings"] if "storage copy of app" in w and "HTTP 403" in w], plan["warnings"])

    def test_the_page_does_not_call_its_namespaces_volumes_unused_meanwhile(self):
        for obj in claim("spare", ns="media"):
            self.kube.add(obj)
        self.kube.discard(self.record)
        used = self.used_by()
        self.assertIsNone(used[("lab", "old")])            # "could not check", not "orphaned"
        self.assertEqual(["Deployment/app"], used[("lab", "new")])
        self.assertEqual([], used[("media", "spare")])     # another namespace is still known

    def test_a_damaged_record_shows_none_of_its_values(self):
        secret = self.kube.object(self.record)
        secret["data"]["earlier"] = base64.b64encode(("{not json " + PASSWORD).encode()).decode()
        self.kube.add(secret)
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            plan = self.plan("old")
            page = STORAGE.get_volumes()
        self.assertTrue(plan["blocked"])
        self.assertTrue([w for w in plan["warnings"] if "could not be read" in w], plan["warnings"])
        for shown in (json.dumps(plan), json.dumps(page), out.getvalue()):
            self.assertNotIn(PASSWORD, shown)
            self.assertNotIn("not json", shown)


class VolumesPageTests(Scene):
    def test_the_page_shows_the_volume_needed_by_the_apps_storage_copy(self):
        used = self.used_by()
        self.assertEqual(["Storage copy/app"], used[("lab", "old")])
        self.assertEqual(["Deployment/app"], used[("lab", "new")])
        self.assertEqual(["Deployment/app"], used[("lab", "keep")])
        self.assertEqual([self.record], self.secret_reads())    # one read per held app, not per volume

    def test_the_page_and_review_show_claim_names_only(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            shown = [json.dumps(self.plan("old")), json.dumps(STORAGE.get_volumes()), out.getvalue()]
        for text in shown:
            self.assertNotIn(PASSWORD, text)
            self.assertNotIn("DB_PASSWORD", text)

    def test_once_the_mark_is_gone_the_page_shows_it_unused_again(self):
        self.unmark()
        self.assertEqual([], self.used_by()[("lab", "old")])


class RealCopyTests(unittest.TestCase):
    """With the real copy code: what it records is what the review reads."""
    DEP = base.CopyDeadlineTests.DEP

    def setUp(self):
        # The account store and change-author key the tests fill go back when each ends (CacheRatchet).
        self.enterContext(mock.patch.dict(AUTH._store_cache, {"at": 0, "data": None}, clear=True))
        self.enterContext(mock.patch.dict(CHANGES._known, clear=True))
        base.CopyDeadlineTests.setUp(self)

    edit = base.CopyDeadlineTests.edit
    controller = base.CopyDeadlineTests.controller
    apply = base.CopyDeadlineTests.apply
    poll = base.CopyDeadlineTests.poll
    job = base.CopyDeadlineTests.job
    started = base.CopyDeadlineTests.started

    def test_a_copy_whose_job_is_gone_keeps_its_source_volume(self):
        job = self.started()
        self.kube.discard(f"/apis/batch/v1/namespaces/lab/jobs/{job['metadata']['name']}")
        self.kube.absent_groups.add("kubevirt.io")
        dep = self.kube.object(self.DEP)
        self.assertIn(flow.MARKER, dep["metadata"]["annotations"])
        self.assertIn(restructure.HELD, dep["metadata"]["annotations"])
        with mock.patch.object(STORAGE, "kget", K.kget), mock.patch.object(VOLUMES, "kget", K.kget), \
                mock.patch.object(VOLUMES, "snapshots", lambda name: []), mock.patch.object(VOLUMES, "backups", lambda name: []):
            refs = STORAGE.claim_references()
            plan = VOLUMES.deletion_plan("lab", "old")
        self.assertEqual(["Storage copy/app"], STORAGE._references_for(refs, "lab", "old"))
        self.assertTrue([r for r in plan["blocking_reasons"] if "storage copy of app has not finished" in r],
                        plan["blocking_reasons"])
        self.assertNotIn("secret-not-in-journal", json.dumps(plan) + json.dumps(STORAGE._references_for(refs, "lab", "old")))


    def test_an_app_created_again_with_the_mark_cannot_be_released_from_jobs(self):
        import homestead_operations as ops
        self.started()
        dep = self.kube.object(self.DEP)
        self.kube.discard(self.DEP)
        for field in ("uid", "resourceVersion", "creationTimestamp"):
            dep["metadata"].pop(field, None)
        self.kube.add(dep)
        self.kube.absent_groups.add("kubevirt.io")
        plan = ops.cancel_plan(ops.stored()[0]["id"])
        self.assertFalse(plan["can"])
        self.assertIn("The app was replaced", plan["why_not"])     # so the warning names the mark
        with mock.patch.object(VOLUMES, "kget", K.kget), mock.patch.object(VOLUMES, "snapshots", lambda name: []), \
                mock.patch.object(VOLUMES, "backups", lambda name: []):
            review = VOLUMES.deletion_plan("lab", "old")
        self.assertTrue(review["blocked"])
        self.assertTrue([w for w in review["warnings"] if "homestead.io/storage-copy-job" in w
                         and "homestead.io/restructure-replicas" in w], review["warnings"])
        # Removing the mark alone would leave the app refused by the start guard.
        import homestead_import_guard as guard
        dep = self.kube.object(self.DEP)
        dep["metadata"]["annotations"].pop(flow.MARKER)
        self.assertIn("older storage copy hold", guard.pending(dep, "lab", K.kget))


if __name__ == "__main__":
    unittest.main()
