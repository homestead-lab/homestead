"""homestead_app_hold.hold / stop / restart / how_to: a job records how an app
ran before stopping it, stops it with one fenced write, and starts it again
only once that is provably safe - Wait for what can pass, KeptStopped (with
how a person starts it) for what can't."""
import base64
import json
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import homestead_app_hold as HOLD
import homestead_deploy_server as DEPLOY
from fakes.kube import Kube

NS = "apps"
PODS = f"/api/v1/namespaces/{NS}/pods"
WEB = f"/apis/apps/v1/namespaces/{NS}/deployments/web"
START = f"/apis/subresources.kubevirt.io/v1/namespaces/{NS}/virtualmachines/vm1/start"
SECRET = "very-secret-password-value"
FITS = {"blocked": False}


def obj(kind, name, uid, api="apps/v1", owner=None, **extra):
    meta = {"name": name, "namespace": NS, "uid": uid, **extra.pop("meta", {})}
    if owner:
        meta["ownerReferences"] = [{"apiVersion": owner["apiVersion"], "kind": owner["kind"],
                                    "name": owner["metadata"]["name"], "uid": owner["metadata"]["uid"],
                                    "controller": True}]
    return {"apiVersion": api, "kind": kind, "metadata": meta, **extra}


def template(claim=None):
    spec = {"containers": [{"name": "app", "image": "example.com/web:1",
                            "env": [{"name": "PASSWORD", "value": SECRET}]}]}
    if claim:
        spec["volumes"] = [{"name": "data", "persistentVolumeClaim": {"claimName": claim}}]
    return {"metadata": {"labels": {"app": "web"}}, "spec": spec}


def web(replicas=3, uid="d-web", **meta):
    return obj("Deployment", "web", uid, spec={"replicas": replicas, "template": template("data")}, meta=meta)


def pod(name, owner=None, phase="Running", claim=None, **meta):
    spec = {"volumes": [{"name": "d", "persistentVolumeClaim": {"claimName": claim}}]} if claim else {}
    return obj("Pod", name, f"uid-{name}", api="v1", owner=owner, status={"phase": phase}, spec=spec, meta=meta)


class AppHoldTest(unittest.TestCase):
    def setUp(self):
        self.admit = mock.Mock(return_value=FITS)
        self.enterContext(mock.patch.object(DEPLOY, "copy_admission", self.admit))

    def kube(self, *objects):
        return self.enterContext(Kube(*objects))

    def held(self, app, kind="node-power", job="j1", **options):
        """An app this job has recorded and stopped; -> (kube, record)."""
        kube = self.kube(app)
        record = HOLD.hold(kube.object(kube.path_of(app)), kind, job, **options)
        HOLD.stop(record, kind, job)
        return kube, record

    def patches(self, kube):
        return [w for w in kube.writes if w[0] == "PATCH"]

    # hold and stop ---------------------------------------------------------

    def test_hold_records_before_it_stops(self):
        kube = self.kube(web())
        record = HOLD.hold(kube.object(WEB), "node-power", "j1")
        self.assertEqual(kube.writes, [])
        self.assertEqual(kube.object(WEB)["spec"]["replicas"], 3)
        self.assertEqual((record.kind, record.namespace, record.name, record.uid),
                         ("Deployment", NS, "web", "d-web"))
        self.assertEqual(record.earlier, {"replicas": 3})
        self.assertEqual(record.resource_version, kube.object(WEB)["metadata"]["resourceVersion"])
        self.assertEqual(record.template_secret, "")
        HOLD.stop(record, "node-power", "j1")
        now = kube.object(WEB)
        self.assertEqual(now["spec"]["replicas"], 0)
        self.assertEqual(now["metadata"]["annotations"][HOLD.HELD_BY], "node-power:j1")
        self.assertEqual(now["metadata"]["annotations"][HOLD.HELD_AS], "3")

    def test_hold_refuses_an_app_held_by_another_job(self):
        for mark in ("cluster-shutdown:run9", "run9", "node-power:j2"):
            with self.subTest(mark), Kube(web(0, annotations={HOLD.HELD_BY: mark, HOLD.HELD_AS: "2"})) as kube:
                with self.assertRaisesRegex(HOLD.Refused, f"held by {mark}"):
                    HOLD.hold(kube.object(WEB), "node-power", "j1")
                self.assertEqual(kube.writes, [])

    def test_hold_reads_its_own_mark_old_and_new(self):
        # Asked again after its own stop (a job resumed): what it ran before
        # comes from the mark, not from the 0 it now has. A bare mark equal to
        # the job id is the old format's.
        for mark in ("node-power:j1", "j1"):
            with self.subTest(mark), Kube(web(0, annotations={HOLD.HELD_BY: mark, HOLD.HELD_AS: "2"})) as kube:
                self.assertEqual(HOLD.hold(kube.object(WEB), "node-power", "j1").earlier, {"replicas": 2})

    def test_hold_keeps_the_template_in_an_owned_secret_never_an_annotation(self):
        kube, record = self.held(web(), keep_template=True)
        self.assertEqual(record.template_secret, "homestead-held-d-web")
        secret = kube.object(f"/api/v1/namespaces/{NS}/secrets/homestead-held-d-web")
        refs = secret["metadata"]["ownerReferences"]
        self.assertEqual([(r["kind"], r["name"], r["uid"]) for r in refs], [("Deployment", "web", "d-web")])
        self.assertIn("homestead.io/held", secret["metadata"]["labels"])
        kept = json.loads(base64.b64decode(secret["data"]["template"]))
        self.assertEqual(kept, template("data"))
        for path in (WEB, kube.path_of(secret)):
            self.assertNotIn(SECRET, json.dumps(kube.object(path)["metadata"].get("annotations") or {}))
        for _, _, body, _ in self.patches(kube):
            self.assertNotIn(SECRET, json.dumps(body))
        self.assertEqual(secret["metadata"]["annotations"][HOLD.HELD_BY], "node-power:j1")

    def test_the_same_job_holding_again_keeps_its_template(self):
        kube = self.kube(web())
        first = HOLD.hold(kube.object(WEB), "node-power", "j1", keep_template=True)
        again = HOLD.hold(kube.object(WEB), "node-power", "j1", keep_template=True)
        self.assertEqual(again.template_secret, first.template_secret)

    def test_a_template_kept_by_another_job_is_never_taken(self):
        kube = self.kube(web())
        HOLD.hold(kube.object(WEB), "node-power", "j1", keep_template=True)
        path = f"/api/v1/namespaces/{NS}/secrets/homestead-held-d-web"
        before = kube.object(path)
        kube("PATCH", WEB, {"spec": {"template": {"metadata": {"labels": {"app": "web", "v": "2"}}}}},
             "application/merge-patch+json")
        with self.assertRaisesRegex(HOLD.Refused, "node-power:j1"):
            HOLD.hold(kube.object(WEB), "cluster-shutdown", "run9", keep_template=True)
        self.assertEqual(kube.object(path)["data"], before["data"])

    def test_the_kept_template_goes_once_started(self):
        kube, record = self.held(web(), keep_template=True)
        path = f"/api/v1/namespaces/{NS}/secrets/{record.template_secret}"
        self.assertIsNotNone(kube.object(path))
        self.assertEqual(HOLD.restart(record, "node-power", "j1", wait_seconds=0), HOLD.Started("3"))
        self.assertIsNone(kube.object(path))
        [(_, _, options, _)] = [w for w in kube.writes if w[0] == "DELETE"]
        self.assertEqual(options["preconditions"]["uid"][:4], "uid-")

    def test_a_kept_template_that_cant_be_removed_waits_then_goes(self):
        kube, record = self.held(web(), keep_template=True)
        path = f"/api/v1/namespaces/{NS}/secrets/{record.template_secret}"
        kube.fail(path, 500, method="DELETE")
        result = HOLD.restart(record, "node-power", "j1", wait_seconds=0)
        self.assertIsInstance(result, HOLD.Wait)
        self.assertIn("kept template", result.reason)
        self.assertEqual(kube.object(WEB)["spec"]["replicas"], 3)  # started all the same
        self.assertEqual(HOLD.restart(record, "node-power", "j1", wait_seconds=0), HOLD.Started("3"))
        self.assertIsNone(kube.object(path))

    def test_the_template_secrets_owner_doesnt_need_the_rows_api_version(self):
        # A row from a list has no apiVersion (nor kind, which the caller sets).
        kube = self.kube(web())
        row = kube.object(WEB)
        row.pop("apiVersion")
        record = HOLD.hold(row, "node-power", "j1", keep_template=True)
        secret = kube.object(f"/api/v1/namespaces/{NS}/secrets/{record.template_secret}")
        self.assertEqual(secret["metadata"]["ownerReferences"][0]["apiVersion"], "apps/v1")

    def test_stop_is_one_fenced_write(self):
        kube = self.kube(web())
        held_rv = kube.object(WEB)["metadata"]["resourceVersion"]
        record = HOLD.hold(kube.object(WEB), "node-power", "j1")
        HOLD.stop(record, "node-power", "j1")
        [(method, path, body, ctype)] = kube.writes
        self.assertEqual((method, path, ctype), ("PATCH", WEB, "application/json-patch+json"))
        self.assertEqual(body[:2], [{"op": "test", "path": "/metadata/uid", "value": "d-web"},
                                    {"op": "test", "path": "/metadata/resourceVersion", "value": held_rv}])
        self.assertIn({"op": "test", "path": "/metadata/uid", "value": "d-web"}, body)
        self.assertIn({"op": "test", "path": "/metadata/resourceVersion", "value": record.resource_version}, body)

    def test_stop_refuses_an_app_changed_since_the_hold(self):
        kube = self.kube(web())
        record = HOLD.hold(kube.object(WEB), "node-power", "j1")
        kube("PATCH", WEB, {"spec": {"replicas": 5}}, "application/merge-patch+json")
        with self.assertRaises(HOLD.Refused):
            HOLD.stop(record, "node-power", "j1")
        self.assertEqual(kube.object(WEB)["spec"]["replicas"], 5)

    # restart ---------------------------------------------------------------

    def test_restart_starts_at_the_earlier_count(self):
        kube, record = self.held(web())
        stopped_rv = kube.object(WEB)["metadata"]["resourceVersion"]
        self.assertNotEqual(stopped_rv, record.resource_version)
        self.assertEqual(HOLD.restart(record, "node-power", "j1", wait_seconds=0), HOLD.Started("3"))
        now = kube.object(WEB)
        self.assertEqual(now["spec"]["replicas"], 3)
        self.assertNotIn(HOLD.HELD_BY, now["metadata"].get("annotations") or {})
        self.assertNotIn(HOLD.HELD_AS, now["metadata"].get("annotations") or {})
        self.assertEqual(self.admit.call_args.args[0]["spec"]["replicas"], 3)
        restart = self.patches(kube)[-1][2]
        self.assertEqual(restart[:2], [{"op": "test", "path": "/metadata/uid", "value": "d-web"},
                                       {"op": "test", "path": "/metadata/resourceVersion", "value": stopped_rv}])

    def test_a_statefulset_restarts_at_its_earlier_count(self):
        db = obj("StatefulSet", "db", "s-db", spec={"replicas": 2, "template": template()})
        kube, record = self.held(db)
        path = kube.path_of(db)
        self.assertEqual(kube.object(path)["spec"]["replicas"], 0)
        self.assertEqual(HOLD.restart(record, "node-power", "j1", wait_seconds=0), HOLD.Started("2"))
        self.assertEqual(kube.object(path)["spec"]["replicas"], 2)
        self.assertEqual(self.admit.call_args.args[0]["kind"], "StatefulSet")

    def test_a_helm_managed_app_restarts_by_default(self):
        kube, record = self.held(web(annotations={"meta.helm.sh/release-name": "web"},
                                     labels={"app.kubernetes.io/managed-by": "Helm"}))
        self.assertEqual(HOLD.restart(record, "node-power", "j1", wait_seconds=0), HOLD.Started("3"))
        self.assertEqual(kube.object(WEB)["spec"]["replicas"], 3)

    def test_a_guard_passed_in_that_refuses_keeps_it_stopped(self):
        kube, record = self.held(web())
        result = HOLD.restart(record, "node-power", "j1", wait_seconds=0,
                              guard=lambda app: f"{app['metadata']['name']} is managed elsewhere")
        self.assertEqual(result, HOLD.KeptStopped("web is managed elsewhere", "Containers → web → Start with 3 replicas"))
        self.assertEqual(kube.object(WEB)["spec"]["replicas"], 0)
        self.assertEqual(HOLD.restart(record, "node-power", "j1", wait_seconds=0, guard=lambda app: None),
                         HOLD.Started("3"))

    def test_a_copy_hold_keeps_it_stopped(self):
        kube, record = self.held(web())
        kube("PATCH", WEB, {"metadata": {"annotations": {"homestead.io/storage-copy-job": "copy-1"}}},
             "application/merge-patch+json")
        result = HOLD.restart(record, "node-power", "j1", wait_seconds=0)
        self.assertIsInstance(result, HOLD.KeptStopped)
        self.assertIn("storage copy", result.reason)

    def test_an_import_job_that_cant_be_read_waits(self):
        kube, record = self.held(web())
        kube("PATCH", WEB, {"metadata": {"annotations": {"homestead.io/import-job": "import-1"}}},
             "application/merge-patch+json")
        kube.fail(f"/apis/batch/v1/namespaces/{NS}/jobs/import-1", 500, times=20)
        self.assertIsInstance(HOLD.restart(record, "node-power", "j1", wait_seconds=0), HOLD.Wait)
        self.assertEqual(kube.object(WEB)["spec"]["replicas"], 0)

    def test_an_app_at_zero_is_never_started(self):
        kube, record = self.held(web(0))
        self.assertEqual(record.earlier, {"replicas": 0})
        self.assertEqual(HOLD.restart(record, "node-power", "j1", wait_seconds=0), HOLD.Started("0"))
        self.assertEqual(kube.writes, [])
        self.assertEqual(kube.object(WEB)["spec"]["replicas"], 0)

    def test_restart_waits_while_old_pods_remain(self):
        kube, record = self.held(web())
        rs = obj("ReplicaSet", "web-abc12", "rs-web", owner=kube.object(WEB))
        kube.add(rs)
        kube.add(pod("web-abc12-x", rs, deletionTimestamp="2026-11-01T00:00:00Z"))
        result = HOLD.restart(record, "node-power", "j1", wait_seconds=0)
        self.assertIsInstance(result, HOLD.Wait)
        self.assertIn("web-abc12-x", result.reason)
        self.assertEqual(kube.object(WEB)["spec"]["replicas"], 0)

    def test_restart_waits_when_pods_cant_be_read(self):
        kube, record = self.held(web())
        kube.fail(PODS, 500, times=20)
        self.assertIsInstance(HOLD.restart(record, "node-power", "j1", wait_seconds=0), HOLD.Wait)
        self.assertEqual(kube.object(WEB)["spec"]["replicas"], 0)

    def test_restart_waits_when_the_app_cant_be_read(self):
        kube, record = self.held(web())
        kube.fail(WEB, 500, times=20)
        self.assertIsInstance(HOLD.restart(record, "node-power", "j1", wait_seconds=0), HOLD.Wait)

    def test_restart_waits_when_it_doesnt_fit(self):
        kube, record = self.held(web())
        self.admit.return_value = {"blocked": True, "candidates": [{"reasons": ["not enough memory on any host"]}]}
        result = HOLD.restart(record, "node-power", "j1", wait_seconds=0)
        self.assertIsInstance(result, HOLD.Wait)
        self.assertIn("not enough memory on any host", result.reason)
        self.assertEqual(kube.object(WEB)["spec"]["replicas"], 0)

    def test_a_conflict_waits(self):
        kube, record = self.held(web())
        kube.fail(WEB, 409, method="PATCH")
        self.assertIsInstance(HOLD.restart(record, "node-power", "j1", wait_seconds=0), HOLD.Wait)
        self.assertEqual(HOLD.restart(record, "node-power", "j1", wait_seconds=0), HOLD.Started("3"))

    def test_a_different_uid_is_kept_stopped_with_how_to(self):
        kube, record = self.held(web())
        kube.discard(WEB)
        kube.add(web(0, uid="d-web-2"))
        result = HOLD.restart(record, "node-power", "j1", wait_seconds=0)
        self.assertIsInstance(result, HOLD.KeptStopped)
        self.assertEqual(result.how_to, "Containers → web → Start with 3 replicas")
        self.assertEqual(self.patches(kube)[1:], [])

    def test_a_gone_app_is_kept_stopped(self):
        kube, record = self.held(web())
        kube.discard(WEB)
        result = HOLD.restart(record, "node-power", "j1", wait_seconds=0)
        self.assertEqual(result.reason, "web is gone")

    def test_a_mark_not_this_jobs_is_kept_stopped(self):
        kube, record = self.held(web())
        kube("PATCH", WEB, {"metadata": {"annotations": {HOLD.HELD_BY: "cluster-shutdown:run9"}}},
             "application/merge-patch+json")
        self.assertIsInstance(HOLD.restart(record, "node-power", "j1", wait_seconds=0), HOLD.KeptStopped)
        self.assertEqual(kube.object(WEB)["spec"]["replicas"], 0)

    def test_an_app_changed_while_stopped_is_kept_stopped(self):
        kube, record = self.held(web())
        kube("PATCH", WEB, {"spec": {"replicas": 1}}, "application/merge-patch+json")
        self.assertIsInstance(HOLD.restart(record, "node-power", "j1", wait_seconds=0), HOLD.KeptStopped)
        self.assertEqual(kube.object(WEB)["spec"]["replicas"], 1)

    def test_an_import_hold_keeps_it_stopped(self):
        kube, record = self.held(web())
        kube("PATCH", WEB, {"metadata": {"annotations": {"homestead.io/import-dispatch": "x"}}},
             "application/merge-patch+json")
        result = HOLD.restart(record, "node-power", "j1", wait_seconds=0)
        self.assertIsInstance(result, HOLD.KeptStopped)
        self.assertIn("Import", result.reason)

    def test_a_claim_used_elsewhere_keeps_it_stopped(self):
        other = obj("StatefulSet", "other", "s-other")
        claim = obj("PersistentVolumeClaim", "data", "pvc-data", api="v1", spec={"volumeName": "pv-data"})
        kube = self.kube(web(), claim, other, pod("other-0", other, claim="data"))
        record = HOLD.hold(kube.object(WEB), "node-power", "j1")
        HOLD.stop(record, "node-power", "j1")
        result = HOLD.restart(record, "node-power", "j1", wait_seconds=0, claims=("data",))
        self.assertIsInstance(result, HOLD.KeptStopped)
        self.assertIn("other-0", result.reason)
        self.assertEqual(kube.object(WEB)["spec"]["replicas"], 0)

    def test_a_claim_attached_or_still_detaching_waits(self):
        claim = obj("PersistentVolumeClaim", "data", "pvc-data", api="v1", spec={"volumeName": "pv-data"})
        for what, meta in (("attached", {"name": "csi-1"}),
                           ("detaching", {"name": "csi-1", "deletionTimestamp": "2026-11-01T00:00:00Z",
                                          "finalizers": ["external-attacher/driver-example-com"]})):
            attachment = {"apiVersion": "storage.k8s.io/v1", "kind": "VolumeAttachment", "metadata": meta,
                          "spec": {"attacher": "driver.example.com", "nodeName": "host-b",
                                   "source": {"persistentVolumeName": "pv-data"}},
                          "status": {"attached": True}}
            with self.subTest(what), Kube(web(), claim, attachment) as kube:
                record = HOLD.hold(kube.object(WEB), "node-power", "j1")
                HOLD.stop(record, "node-power", "j1")
                result = HOLD.restart(record, "node-power", "j1", wait_seconds=0, claims=("data",))
                self.assertIsInstance(result, HOLD.Wait)
                self.assertIn("host-b", result.reason)
                self.assertEqual(kube.object(WEB)["spec"]["replicas"], 0)

    def test_a_claim_gone_keeps_it_stopped(self):
        kube, record = self.held(web())
        result = HOLD.restart(record, "node-power", "j1", wait_seconds=0, claims=("data",))
        self.assertIsInstance(result, HOLD.KeptStopped)
        self.assertIn("data is gone", result.reason)
        self.assertEqual(kube.object(WEB)["spec"]["replicas"], 0)

    def test_a_claim_with_no_spec_never_raises(self):
        claim = obj("PersistentVolumeClaim", "data", "pvc-data", api="v1")
        kube = self.kube(web(), claim)
        record = HOLD.hold(kube.object(WEB), "node-power", "j1")
        HOLD.stop(record, "node-power", "j1")
        self.assertEqual(HOLD.restart(record, "node-power", "j1", wait_seconds=0, claims=("data",)),
                         HOLD.Started("3"))

    def test_rows_it_cant_make_sense_of_wait_never_raise(self):
        kube, record = self.held(web())
        kube.react("GET", PODS, lambda method, path, body: {"kind": "PodList", "metadata": {}, "items": [{"metadata": "junk"}]})
        self.assertIsInstance(HOLD.restart(record, "node-power", "j1", wait_seconds=0), HOLD.Wait)
        self.assertEqual(kube.object(WEB)["spec"]["replicas"], 0)

    def test_a_claim_that_cant_be_read_waits(self):
        claim = obj("PersistentVolumeClaim", "data", "pvc-data", api="v1", spec={"volumeName": "pv-data"})
        kube = self.kube(web(), claim)
        record = HOLD.hold(kube.object(WEB), "node-power", "j1")
        HOLD.stop(record, "node-power", "j1")
        kube.fail(kube.path_of(claim), 500, times=20)
        self.assertIsInstance(HOLD.restart(record, "node-power", "j1", wait_seconds=0, claims=("data",)), HOLD.Wait)
        self.assertEqual(kube.object(WEB)["spec"]["replicas"], 0)

    def old_pod(self, kube):
        rs = obj("ReplicaSet", "web-abc12", "rs-web", owner=kube.object(WEB))
        kube.add(rs)
        return kube.add(pod("web-abc12-x", rs, deletionTimestamp="2026-11-01T00:00:00Z"))

    def test_restart_waits_a_while_for_old_pods_to_go(self):
        self.enterContext(mock.patch.object(HOLD, "POLL_SECONDS", 0.01))
        kube, record = self.held(web())
        old = self.old_pod(kube)
        looks = []

        def pods(method, path, body):
            looks.append(path)
            if len(looks) == 2:
                kube.discard(old)  # gone while restart waits
        kube.react("GET", PODS, pods)
        self.assertEqual(HOLD.restart(record, "node-power", "j1", wait_seconds=30), HOLD.Started("3"))
        self.assertGreaterEqual(len(looks), 2)

    def test_restart_waits_only_so_long(self):
        self.enterContext(mock.patch.object(HOLD, "POLL_SECONDS", 0.01))
        kube, record = self.held(web())
        self.old_pod(kube)
        looks = []
        kube.react("GET", PODS, lambda method, path, body: looks.append(path))
        self.assertIsInstance(HOLD.restart(record, "node-power", "j1", wait_seconds=0.1), HOLD.Wait)
        self.assertGreater(len(looks), 1)
        self.assertEqual(kube.object(WEB)["spec"]["replicas"], 0)

    def test_a_refused_stop_gives_the_real_reason(self):
        kube = self.kube(web())
        record = HOLD.hold(kube.object(WEB), "node-power", "j1")
        kube("PATCH", WEB, {"spec": {"replicas": 5}}, "application/merge-patch+json")
        with self.assertRaises(HOLD.Refused):
            HOLD.stop(record, "node-power", "j1")
        result = HOLD.restart(record, "node-power", "j1", wait_seconds=0)
        self.assertIsInstance(result, HOLD.KeptStopped)
        self.assertNotIn("taken away", result.reason)
        self.assertIn("is not held by this job", result.reason)
        self.assertIn("5 rather than 3", result.reason)
        self.assertEqual(kube.object(WEB)["spec"]["replicas"], 5)

    def test_a_free_claim_lets_it_start(self):
        claim = obj("PersistentVolumeClaim", "data", "pvc-data", api="v1", spec={"volumeName": "pv-data"})
        kube = self.kube(web(), claim)
        record = HOLD.hold(kube.object(WEB), "node-power", "j1")
        HOLD.stop(record, "node-power", "j1")
        self.assertEqual(HOLD.restart(record, "node-power", "j1", wait_seconds=0, claims=("data",)),
                         HOLD.Started("3"))

    def test_lost_reply_reads_before_writing_again(self):
        kube, record = self.held(web())
        kube.fail(WEB, TimeoutError("timed out"), method="PATCH", applied=True)
        self.assertIsInstance(HOLD.restart(record, "node-power", "j1", wait_seconds=0), HOLD.Wait)
        self.assertEqual(HOLD.restart(record, "node-power", "j1", wait_seconds=0), HOLD.Started("3"))
        self.assertEqual(len(self.patches(kube)), 2)  # the stop, and one restart
        self.assertEqual(kube.object(WEB)["spec"]["replicas"], 3)

    def test_a_cronjob_is_suspended_and_resumed(self):
        cron = obj("CronJob", "backup", "c-backup", api="batch/v1",
                   spec={"schedule": "0 3 * * *", "suspend": False, "jobTemplate": {"spec": {"template": template()}}})
        kube, record = self.held(cron, kind="cluster-shutdown", job="run9")
        path = kube.path_of(cron)
        self.assertIs(kube.object(path)["spec"]["suspend"], True)
        job = obj("Job", "backup-1", "j-1", api="batch/v1", owner=kube.object(path))
        kube.add(job)
        kube.add(pod("backup-1-x", job))
        result = HOLD.restart(record, "cluster-shutdown", "run9", wait_seconds=0)
        self.assertIsInstance(result, HOLD.Wait)
        self.assertIn("a run of backup is still going", result.reason)
        self.assertIsNotNone(kube.object(kube.path_of(job)))  # never deleted
        kube.discard(kube.path_of(job))
        self.assertIsInstance(HOLD.restart(record, "cluster-shutdown", "run9", wait_seconds=0), HOLD.Started)
        self.assertIs(kube.object(path)["spec"]["suspend"], False)
        self.admit.assert_not_called()

    def test_a_vm_halts_and_starts_with_its_run_strategy(self):
        vm = obj("VirtualMachine", "vm1", "vm-1", api="kubevirt.io/v1",
                 spec={"runStrategy": "Always", "template": template()})
        kube, record = self.held(vm)
        path = kube.path_of(vm)
        self.assertEqual(kube.object(path)["spec"]["runStrategy"], "Halted")
        self.assertEqual(record.earlier, {"runStrategy": "Always"})
        self.assertEqual(HOLD.restart(record, "node-power", "j1", wait_seconds=0), HOLD.Started("Always"))
        self.assertEqual(kube.object(path)["spec"]["runStrategy"], "Always")

    def manual_vm(self, running=True):
        vm = obj("VirtualMachine", "vm1", "vm-1", api="kubevirt.io/v1",
                 spec={"runStrategy": "Manual", "template": template()})
        objects = [vm]
        if running:
            objects.append(obj("VirtualMachineInstance", "vm1", "vmi-1", api="kubevirt.io/v1", owner=vm,
                               status={"phase": "Running"}))
        return vm, objects

    def test_a_manual_vm_is_started_again_through_kubevirt(self):
        vm, objects = self.manual_vm()
        kube = self.kube(*objects)
        path, vmi = kube.path_of(vm), kube.path_of(objects[1])
        record = HOLD.hold(kube.object(path), "node-power", "j1")
        HOLD.stop(record, "node-power", "j1")
        self.assertEqual(kube.object(path)["spec"]["runStrategy"], "Halted")
        kube.discard(vmi)  # KubeVirt stops it
        starts = []

        def start(method, path_, body):
            starts.append(path_)
            kube.add(obj("VirtualMachineInstance", "vm1", "vmi-2", api="kubevirt.io/v1", owner=kube.object(path)))
            return {}
        kube.react("PUT", START, start)
        kube.fail(START, 500, method="PUT")
        # Set to Manual, but the start failed: not Started, still held.
        self.assertIsInstance(HOLD.restart(record, "node-power", "j1", wait_seconds=0), HOLD.Wait)
        self.assertEqual(kube.object(path)["spec"]["runStrategy"], "Manual")
        self.assertEqual(kube.object(path)["metadata"]["annotations"][HOLD.HELD_BY], "node-power:j1")
        self.assertEqual(starts, [])
        self.assertEqual(HOLD.restart(record, "node-power", "j1", wait_seconds=0), HOLD.Started("Manual"))
        now = kube.object(path)
        self.assertEqual(now["spec"]["runStrategy"], "Manual")
        self.assertNotIn(HOLD.HELD_BY, now["metadata"].get("annotations") or {})
        self.assertEqual(starts, [START])
        self.assertIsNotNone(kube.object(vmi))
        # Asked again once done: nothing more is sent.
        self.assertEqual(HOLD.restart(record, "node-power", "j1", wait_seconds=0), HOLD.Started("Manual"))
        self.assertEqual(starts, [START])

    def test_a_manual_vm_that_was_off_is_never_started(self):
        vm, objects = self.manual_vm(running=False)
        kube = self.kube(*objects)
        record = HOLD.hold(kube.object(kube.path_of(vm)), "node-power", "j1")
        HOLD.stop(record, "node-power", "j1")
        kube.react("PUT", START, lambda *a: self.fail("started a VM that was off"))
        self.assertIsInstance(HOLD.restart(record, "node-power", "j1", wait_seconds=0), HOLD.Started)
        self.assertEqual(kube.writes, [])
        self.assertEqual(kube.object(kube.path_of(vm))["spec"]["runStrategy"], "Manual")

    # how_to ----------------------------------------------------------------

    # release: a job ending with the app not started ----------------------------

    def test_release_takes_this_jobs_marks_off_and_leaves_the_app_stopped(self):
        kube, record = self.held(web())
        self.assertEqual("", HOLD.release(record, "node-power", "j1"))
        notes = kube.object(WEB)["metadata"].get("annotations") or {}
        self.assertNotIn(HOLD.HELD_BY, notes)
        self.assertNotIn(HOLD.HELD_AS, notes)
        self.assertEqual(0, kube.object(WEB)["spec"]["replicas"])

    def test_release_leaves_another_jobs_mark(self):
        kube = self.kube(web(annotations={HOLD.HELD_BY: "other:j2", HOLD.HELD_AS: "3"}))
        record = HOLD.Record("Deployment", NS, "web", "d-web", "1", {"replicas": 3}, "")
        self.assertEqual("", HOLD.release(record, "node-power", "j1"))
        self.assertEqual("other:j2", kube.object(WEB)["metadata"]["annotations"][HOLD.HELD_BY])
        self.assertEqual([], self.patches(kube))

    def test_release_leaves_another_object_of_that_name(self):
        kube = self.kube(web(uid="d-new", annotations={HOLD.HELD_BY: "node-power:j1", HOLD.HELD_AS: "3"}))
        record = HOLD.Record("Deployment", NS, "web", "d-web", "1", {"replicas": 3}, "")
        self.assertEqual("", HOLD.release(record, "node-power", "j1"))
        self.assertEqual([], self.patches(kube))

    def test_release_of_an_app_that_has_gone_is_done(self):
        kube, record = self.held(web())
        kube.discard(WEB)
        self.assertEqual("", HOLD.release(record, "node-power", "j1"))

    def test_release_reads_again_when_a_controller_wrote_first(self):
        kube, record = self.held(web())
        kube.fail(WEB, 422, method="PATCH")
        self.assertEqual("", HOLD.release(record, "node-power", "j1"))
        self.assertNotIn(HOLD.HELD_BY, kube.object(WEB)["metadata"].get("annotations") or {})

    def test_release_that_lands_with_its_reply_lost_says_why_and_a_second_one_is_harmless(self):
        kube, record = self.held(web())
        kube.fail(WEB, OSError("timed out"), method="PATCH", applied=True)
        self.assertIn("timed out", HOLD.release(record, "node-power", "j1"))
        sent = len(self.patches(kube))
        self.assertEqual("", HOLD.release(record, "node-power", "j1"))
        self.assertEqual(sent, len(self.patches(kube)))

    def test_release_is_fenced_so_a_mark_another_job_put_in_the_instant_before_stays(self):
        kube, record = self.held(web())

        def beat_it(method, path, body):
            kube._reactions.clear()
            taken = kube.object(WEB)
            taken["metadata"]["annotations"][HOLD.HELD_BY] = "other:j2"
            taken["metadata"]["resourceVersion"] = "9000"
            kube.add(taken)
        kube.react("PATCH", WEB, beat_it)
        self.assertEqual("", HOLD.release(record, "node-power", "j1"))
        self.assertEqual("other:j2", kube.object(WEB)["metadata"]["annotations"][HOLD.HELD_BY])

    def test_a_mark_gone_in_the_instant_before_release_reads_again_and_is_done(self):
        """A remove of a mark already gone answers 422 on a real cluster."""
        kube, record = self.held(web())

        def gone(method, path, body):
            kube._reactions.clear()
            taken = kube.object(WEB)
            del taken["metadata"]["annotations"][HOLD.HELD_BY]
            kube.add(taken)
        kube.react("PATCH", WEB, gone)
        self.assertEqual("", HOLD.release(record, "node-power", "j1"))

    def test_a_mark_gone_in_the_instant_before_a_restart_waits_then_keeps_it_stopped(self):
        kube, record = self.held(web())

        def gone(method, path, body):
            kube._reactions.clear()
            taken = kube.object(WEB)
            del taken["metadata"]["annotations"][HOLD.HELD_BY]
            kube.add(taken)
        kube.react("PATCH", WEB, gone)
        self.assertIsInstance(HOLD.restart(record, "node-power", "j1", wait_seconds=0), HOLD.Wait)
        self.assertEqual(0, kube.object(WEB)["spec"]["replicas"])
        self.assertIsInstance(HOLD.restart(record, "node-power", "j1", wait_seconds=0), HOLD.KeptStopped)

    def test_release_says_why_when_it_can_not_write(self):
        kube, record = self.held(web())
        kube.fail(WEB, 403, method="PATCH")
        self.assertIn("403", HOLD.release(record, "node-power", "j1"))
        self.assertEqual("node-power:j1", kube.object(WEB)["metadata"]["annotations"][HOLD.HELD_BY])

    def test_how_to_words_per_kind(self):
        def record(kind, earlier):
            return HOLD.Record(kind, NS, "x", "u", "1", earlier, "")
        self.assertEqual(HOLD.how_to(record("Deployment", {"replicas": 3})), "Containers → x → Start with 3 replicas")
        self.assertEqual(HOLD.how_to(record("StatefulSet", {"replicas": 1})), "Containers → x → Start with 1 replica")
        self.assertEqual(HOLD.how_to(record("VirtualMachine", {"runStrategy": "Always"})), "Virtual machines → x → Start")
        self.assertEqual(HOLD.how_to(record("CronJob", {"suspend": False})), ("Scheduled jobs shows x as paused. Resume it by setting its spec.suspend back to false "
                          "(kubectl, or the Git source it comes from)"))


if __name__ == "__main__":
    unittest.main()
