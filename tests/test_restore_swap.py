"""Restore into this app (#743): a backup restored into a new claim, swapped in
for the app's own, and swapped back on its own when the app does not come up.
The old claim is kept as a previous volume, which expires."""
import contextlib
import copy
import json
import sys
import types
import unittest
import urllib.error
import urllib.parse
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "server"))

import homestead_restore_swap as RS
from unittest import mock

STRICT = RS.settings_strict          # the real reader, before any test replaces it
import homestead_routes as MODULE_ROUTES

NS = "lab"
DEP = f"/apis/apps/v1/namespaces/{NS}/deployments/app-1"
OLD = f"/api/v1/namespaces/{NS}/persistentvolumeclaims/app-1-data"
NEW = f"/api/v1/namespaces/{NS}/persistentvolumeclaims/app-1-data-restore"
LISTS = {"deployments", "statefulsets", "daemonsets", "cronjobs", "pods", "persistentvolumeclaims"}
NOW = 1_800_000_000.0


def missing(path):
    return urllib.error.HTTPError(path, 404, "not found", {}, None)


def merge(target, patch):
    for key, value in patch.items():
        if value is None:
            target.pop(key, None)
        elif isinstance(value, dict) and isinstance(target.get(key), dict):
            merge(target[key], value)
        else:
            target[key] = copy.deepcopy(value)


def pointer(obj, path):
    parts = [p for p in path.split("/") if p]
    for part in parts[:-1]:
        obj = obj[int(part)] if isinstance(obj, list) else obj[part]
    last = parts[-1]
    return obj, int(last) if isinstance(obj, list) else last


class Kube:
    """Objects by path; lists read from them; pods follow the Deployments."""

    def __init__(self):
        self.objs, self.sent, self.loose, self.ready = {}, [], [], True

    def add(self, path, obj):
        self.objs[path] = obj
        return obj

    def _pods(self, ns):
        pods = []
        for path, dep in self.objs.items():
            if f"/namespaces/{ns}/deployments/" in path and self.ready and dep["spec"].get("replicas", 1):
                pods.append({"metadata": {"name": dep["metadata"]["name"] + "-5d8f-x1", "ownerReferences": [
                                 {"kind": "ReplicaSet", "name": dep["metadata"]["name"] + "-5d8f"}]},
                             "spec": copy.deepcopy(dep["spec"]["template"]["spec"]), "status": {"phase": "Running"}})
        return pods + copy.deepcopy(self.loose)

    def get(self, path, **_):
        base, _, query = path.partition("?")
        if base in self.objs:
            return copy.deepcopy(self.objs[base])
        if base.rsplit("/", 1)[-1] in LISTS:
            if base.endswith("/pods"):
                return {"items": self._pods(base.split("/")[4])}
            rows = [copy.deepcopy(o) for p, o in self.objs.items() if p.rsplit("/", 1)[0] == base or (
                base == "/api/v1/persistentvolumeclaims" and "/persistentvolumeclaims/" in p)]
            if "labelSelector=" in query:
                key, value = urllib.parse.unquote(query.split("labelSelector=", 1)[1]).split("=")
                rows = [o for o in rows if (o["metadata"].get("labels") or {}).get(key) == value]
            return {"items": rows}
        raise missing(path)

    def send(self, method, path, body=None, ctype="application/json", **_):
        self.sent.append((method, path, copy.deepcopy(body), ctype))
        obj = self.objs.get(path)
        if obj is None:
            raise missing(path)
        before = json.dumps(obj.get("spec"), sort_keys=True)
        if ctype == "application/json-patch+json":
            trial = copy.deepcopy(obj)
            for op in body:
                parent, key = pointer(trial, op["path"])
                if op["op"] == "test" and parent[key] != op["value"]:
                    raise urllib.error.HTTPError(path, 422, "test failed", {}, None)
                if op["op"] == "replace":
                    parent[key] = op["value"]
            obj.clear(); obj.update(trial)
        else:
            merge(obj, body)
        if "/deployments/" in path:
            if json.dumps(obj.get("spec"), sort_keys=True) != before:
                obj["metadata"]["generation"] += 1
            self.settle(obj)
        return copy.deepcopy(obj)

    def settle(self, dep):
        want = int(dep["spec"].get("replicas", 1))
        n = want if self.ready else 0
        dep["status"] = {"observedGeneration": dep["metadata"]["generation"], "updatedReplicas": n,
                         "readyReplicas": n, "availableReplicas": n, "replicas": want}

    def writes(self, path=None, ctype=None):
        return [s for s in self.sent if (path is None or s[1] == path) and (ctype is None or s[3] == ctype)]


def deployment(name="app-1", claim="app-1-data", kind="Deployment", replicas=1, labels=None):
    obj = {"metadata": {"name": name, "namespace": NS, "generation": 3, "resourceVersion": "100", "labels": labels or {}},
           "spec": {"replicas": replicas, "template": {"spec": {
               "containers": [{"name": "app", "volumeMounts": [{"name": "data", "mountPath": "/config"}]}],
               "volumes": [{"name": "cfg", "configMap": {"name": "app-1"}},
                           {"name": "data", "persistentVolumeClaim": {"claimName": claim}}]}}}}
    obj["kind"] = kind
    return obj


def claim(name, uid, annotations=None, labels=None):
    return {"metadata": {"name": name, "namespace": NS, "uid": uid, "annotations": dict(annotations or {}),
                         "labels": dict(labels or {})},
            "spec": {"volumeName": "pv-" + uid, "storageClassName": "longhorn", "accessModes": ["ReadWriteOnce"],
                     "resources": {"requests": {"storage": "10Gi"}}}}


class FakeOps:
    TERMINAL = {"succeeded", "failed", "cancelled"}

    def __init__(self):
        self.started, self.items, self.depth, self.refuse = [], [], 0, None

    @contextlib.contextmanager
    def transaction(self):
        self.depth += 1
        try:
            yield
        finally:
            self.depth -= 1

    def start(self, kind, title, resource, href, ref, message="", unless=()):
        if self.refuse:
            raise ValueError(self.refuse)
        self.started.append({"kind": kind, "title": title, "resource": resource, "ref": copy.deepcopy(ref)})
        return {"id": "op1", "kind": kind}

    def snapshot(self):
        return copy.deepcopy(self.items)

    def stored(self):
        return copy.deepcopy(self.items)


class Base(unittest.TestCase):
    def setUp(self):
        self.kube = kube = Kube()
        kube.add(DEP, deployment())
        kube.settle(kube.objs[DEP])
        kube.add(OLD, claim("app-1-data", "u-old"))
        kube.add("/apis/storage.k8s.io/v1/storageclasses/longhorn", {"metadata": {"name": "longhorn"}, "allowVolumeExpansion": True})
        self.ops = FakeOps()
        self.backup = {"metadata": {"name": "backup-0123abcd"},
                       "status": {"state": "Completed", "url": "s3://example.com/b", "volumeSize": str(10 * 1024 ** 3),
                                  "backupCreatedAt": "2026-10-20T03:00:00Z",
                                  "labels": {"KubernetesStatus": json.dumps({"namespace": NS, "pvcName": "app-1-data"})}}}
        self.restored, self.restore_result, self.cancelled = [], ("succeeded", 100, "Restored"), []
        self.refused_classes = {}
        self.uptime, self.settings, self.notes, self.deleted = {}, {"restore_swap": {"keep_days": 7}}, [], []
        self.leader, self.self_app = True, False
        self.blocking, self.reclaim = [], "Delete"
        saved = {name: getattr(RS, name) for name in RS.HOOKS}
        self.addCleanup(lambda: [setattr(RS, name, value) for name, value in saved.items()])
        RS.kget, RS.ksend = kube.get, kube.send
        RS.OPS = self.ops
        RS.fence = lambda handle: contextlib.nullcontext()
        RS.backup_object = lambda name: copy.deepcopy(self.backup)
        RS.restore_plan = self.fake_restore_plan
        RS.restore_backup = self.fake_restore
        RS.restore_resolver = self.fake_restore_resolver
        RS.restore_cancel = lambda item: self.cancelled.append(copy.deepcopy(item["ref"])) or "Restore stopped"
        RS.uptime_report = lambda: self.uptime
        RS.settings = lambda: self.settings
        RS.settings_strict = lambda: self.settings
        RS.is_self = lambda ns, name: self.self_app
        RS.system_namespaces = lambda: {"kube-system", "longhorn-system"}
        RS.is_leader = lambda: self.leader
        RS.note = self.notes.append
        RS.delete_volume = lambda cfg: self.deleted.append(cfg) or {"ok": True}
        RS.deletion_plan = self.fake_deletion_plan
        self.records = {}
        RS.read_records = lambda: copy.deepcopy(self.records)
        RS.update_records = lambda fn: fn(self.records)

    def fake_restore_plan(self, backup, ns, name):
        taken = f"/api/v1/namespaces/{ns}/persistentvolumeclaims/{name}" in self.kube.objs
        return {"minimum_size_gb": 10, "storage_class": "longhorn", "storage_classes": ["longhorn"],
                "refused_classes": self.refused_classes,
                "conflict": {"message": f"PVC {ns}/{name} already exists; choose a new name"} if taken else None}

    def fake_restore_resolver(self, item):
        ref = item["ref"]
        if not ref.get("restore_started"):
            RS.restore_backup(ref["restore_config"])
            ref["restore_started"] = True
        return self.restore_result

    def fake_restore(self, cfg):
        self.restored.append(copy.deepcopy(cfg))
        made = claim(cfg["name"], "u-new", annotations=cfg["annotations"])
        self.kube.add(f"/api/v1/namespaces/{cfg['namespace']}/persistentvolumeclaims/{cfg['name']}", made)
        return {"created": True, "name": cfg["name"], "uid": "u-new", "source_size_bytes": 10 * 1024 ** 3, "message": "Restore started"}

    def fake_deletion_plan(self, ns, name):
        pods = [p for p in self.kube._pods(ns) if any((v.get("persistentVolumeClaim") or {}).get("claimName") == name
                                                      for v in p["spec"].get("volumes") or [])]
        return {"uid": self.kube.objs[f"/api/v1/namespaces/{ns}/persistentvolumeclaims/{name}"]["metadata"]["uid"],
                "consumers": [{"kind": "Pod", "name": p["metadata"]["name"], "active": True} for p in pods],
                "blocking_reasons": list(self.blocking), "pv": {"name": "pv-x", "reclaim_policy": self.reclaim}}

    def plan(self, **extra):
        return RS.plan("backup-0123abcd", **extra)

    def started(self, **extra):
        p = self.plan()
        body = {"backup": "backup-0123abcd", "namespace": NS, "app": "app-1", "claim": "app-1-data",
                "digest": p["digest"], **extra}
        result = RS.start(body)
        ref = self.ops.started[-1]["ref"]
        self.checkpoints = []
        return result, {"id": "op1", "status": "running", "ref": ref}

    def step(self, item, now):
        def checkpoint(it):
            self.checkpoints.append(copy.deepcopy(it))
        return RS.resolve(item, checkpoint, now=now)

    def run_until(self, item, phase, now=NOW, limit=20):
        result = None
        for _ in range(limit):
            if item["ref"].get("phase") == phase:
                return result
            result = self.step(item, now)
            if result[0] != "running":
                return result
        self.fail(f"never reached {phase}: {item['ref'].get('phase')}")

    def template_claim(self):
        return self.kube.objs[DEP]["spec"]["template"]["spec"]["volumes"][1]["persistentVolumeClaim"]["claimName"]

    def marks(self, path):
        meta = self.kube.objs[path]["metadata"]
        return meta.get("labels") or {}, meta.get("annotations") or {}


class PlanTests(Base):
    def test_a_deployment_with_one_claim_is_ready_with_five_steps(self):
        p = self.plan()
        self.assertTrue(p["ready"], p["blockers"])
        self.assertEqual(["restore", "stop", "swap", "start", "watch"], p["steps"])
        self.assertEqual(({"namespace": NS, "name": "app-1", "kind": "Deployment", "replicas": 1}, "app-1-data"),
                         (p["app"], p["claim"]))
        self.assertEqual(("app-1-data-restore", 7, "data"), (p["new_claim"], p["keep_days"], p["mount"]["volume"]))
        self.assertEqual([{"name": "app", "path": "/config"}], p["mount"]["containers"])
        self.assertTrue(p["digest"])

    def blocked(self, needle, **extra):
        p = self.plan(**extra)
        self.assertFalse(p["ready"])
        self.assertTrue(any(needle in b for b in p["blockers"]), p["blockers"])

    def test_a_statefulset_waits(self):
        del self.kube.objs[DEP]
        self.kube.add(f"/apis/apps/v1/namespaces/{NS}/statefulsets/app-1", deployment(kind="StatefulSet"))
        self.blocked("StatefulSet")

    def test_a_claim_shared_with_another_workload(self):
        self.kube.add(f"/apis/apps/v1/namespaces/{NS}/deployments/app-2", deployment("app-2"))
        self.blocked("app-2")

    def test_a_loose_pod(self):
        self.kube.loose.append({"metadata": {"name": "debug"}, "status": {"phase": "Running"},
                                "spec": {"volumes": [{"persistentVolumeClaim": {"claimName": "app-1-data"}}]}})
        self.blocked("debug")

    def test_homesteads_own_data(self):
        self.self_app = True
        self.blocked("Homestead's own data")

    def test_a_backup_that_is_not_completed(self):
        self.backup["status"]["state"] = "InProgress"
        self.blocked("not ready to restore")

    def test_an_active_job_on_the_claim(self):
        self.ops.items.append({"kind": "reclass", "status": "running", "resource": {"namespace": NS, "name": "app-1-data"},
                               "ref": {"namespace": NS, "claim": "app-1-data"}})
        self.blocked("already has a job")

    def test_a_new_name_that_is_taken(self):
        self.kube.add(f"/api/v1/namespaces/{NS}/persistentvolumeclaims/taken", claim("taken", "u-t"))
        self.blocked("already exists", new_name="taken")

    def test_a_free_name_is_chosen_when_the_usual_one_is_taken(self):
        self.kube.add(NEW, claim("app-1-data-restore", "u-x"))
        self.assertEqual("app-1-data-restore-2", self.plan()["new_claim"])

    def test_a_flux_app_needs_the_notice_accepted(self):
        self.kube.objs[DEP]["metadata"]["labels"] = {"kustomize.toolkit.fluxcd.io/name": "apps",
                                                     "kustomize.toolkit.fluxcd.io/namespace": "flux-system"}
        p = self.plan()
        self.assertEqual("Flux", p["managed"]["by"])
        self.assertTrue(p["ready"])
        with self.assertRaisesRegex(ValueError, "Flux"):
            self.started()
        self.started(accept_gitops=True)
        self.assertEqual("restore-swap", self.ops.started[-1]["kind"])


class RefusedClassTests(Base):
    """A class a restore cannot use: the review says why, and another class can
    be chosen rather than one being picked in silence. Ready means it can work."""
    WHY = "late makes a volume only once an app mounts it"

    def on_a_refused_class(self):
        self.refused_classes = {"late": self.WHY}
        self.kube.objs[f"/api/v1/namespaces/{NS}/persistentvolumeclaims/app-1-data"]["spec"]["storageClassName"] = "late"

    def add_class(self, name, **fields):
        self.kube.add(f"/apis/storage.k8s.io/v1/storageclasses/{name}", {"metadata": {"name": name}, **fields})

    def offering(self, *names):
        """The classes the backup's restore plan offers, as restore_plan answers."""
        plan = self.fake_restore_plan
        RS.restore_plan = lambda *args: {**plan(*args), "storage_classes": list(names)}

    def test_the_review_says_why_it_cannot_start_and_asks_for_another(self):
        self.on_a_refused_class()
        p = self.plan()
        self.assertFalse(p["ready"])
        self.assertIn(f"{self.WHY}; choose another storage class", p["blockers"])
        self.assertEqual("late", p["storage_class"])
        self.assertEqual({"late": self.WHY}, p["refused_classes"])

    def test_it_says_so_when_there_is_no_other_class_to_choose(self):
        self.on_a_refused_class()
        self.offering()
        p = self.plan()
        self.assertFalse(p["ready"])
        self.assertEqual([], p["storage_classes"])
        self.assertFalse([b for b in p["blockers"] if "choose another" in b], p["blockers"])
        self.assertTrue([b for b in p["blockers"] if self.WHY in b and "no other class" in b], p["blockers"])

    def test_it_is_refused_when_asked_for_by_name_too(self):
        self.refused_classes = {"late": self.WHY}
        self.assertFalse(self.plan(storage_class="late")["ready"])

    def test_with_another_class_chosen_the_restore_goes_on_that_class(self):
        self.on_a_refused_class()
        p = self.plan(storage_class="longhorn")
        self.assertTrue(p["ready"], p["blockers"])
        RS.start({"backup": "backup-0123abcd", "namespace": NS, "app": "app-1", "claim": "app-1-data",
                  "digest": p["digest"], "storage_class": "longhorn"})
        self.assertEqual("longhorn", self.ops.started[-1]["ref"]["restore"]["restore_config"]["storage_class"])

    def test_the_review_page_can_ask_for_another_class(self):
        self.on_a_refused_class()
        plain = RS._plan_route(types.SimpleNamespace(query={"backup": ["backup-0123abcd"]}))
        chosen = RS._plan_route(types.SimpleNamespace(query={"backup": ["backup-0123abcd"], "storage_class": ["longhorn"]}))
        self.assertFalse(plain["ready"])
        self.assertTrue(chosen["ready"], chosen["blockers"])

    def test_a_migratable_class_is_not_ready_for_a_filesystem_volume_and_not_offered(self):
        self.add_class("rwx", parameters={"migratable": "true"})
        self.offering("longhorn", "rwx")
        p = self.plan(storage_class="rwx")
        self.assertFalse(p["ready"])
        self.assertEqual(["longhorn"], p["storage_classes"])
        self.assertIn("migratable disabled", p["refused_classes"]["rwx"])

    def test_a_migratable_class_stays_on_offer_for_a_block_volume(self):
        self.add_class("rwx", parameters={"migratable": "true"})
        self.offering("longhorn", "rwx")
        self.kube.objs[f"/api/v1/namespaces/{NS}/persistentvolumeclaims/app-1-data"]["spec"]["volumeMode"] = "Block"
        p = self.plan(storage_class="rwx")
        self.assertTrue(p["ready"], p["blockers"])
        self.assertEqual(["longhorn", "rwx"], p["storage_classes"])

    def test_a_class_on_neither_list_blocks(self):
        p = self.plan(storage_class="local-path")
        self.assertFalse(p["ready"])
        self.assertTrue([b for b in p["blockers"] if "local-path" in b and "choose another" in b], p["blockers"])

    def test_a_bad_class_name_on_the_route_is_refused_before_it_is_read(self):
        before = len(self.kube.sent)
        with self.assertRaisesRegex(ValueError, "storage class must be a valid Kubernetes name"):
            RS._plan_route(types.SimpleNamespace(query={"backup": ["backup-0123abcd"], "storage_class": ["../secrets/x"]}))
        self.assertEqual(before, len(self.kube.sent))


class StartTests(Base):
    def test_a_stale_review_is_a_409_with_a_fresh_plan(self):
        p = self.plan()
        self.kube.objs[DEP]["metadata"]["generation"] = 9
        reply = RS.start({"backup": "backup-0123abcd", "namespace": NS, "app": "app-1", "claim": "app-1-data",
                          "digest": p["digest"]})
        self.assertIsInstance(reply, MODULE_ROUTES.Reply)
        self.assertEqual(409, reply.code)
        self.assertNotEqual(p["digest"], reply.body["plan"]["digest"])
        self.assertEqual([], self.restored)

    def test_start_restores_with_the_restore_id_and_records_the_job(self):
        result, item = self.started()
        self.assertEqual([], self.restored)           # the job makes the claim, not the request
        recorded = copy.deepcopy(item["ref"])
        self.step(item, NOW)
        cfg = self.restored[0]
        self.assertEqual(("app-1-data-restore", NS), (cfg["name"], cfg["namespace"]))
        self.assertEqual(cfg["restore_id"], cfg["annotations"]["homestead.io/restore-id"])
        self.assertEqual("lab/app-1", cfg["annotations"]["homestead.io/restored-into"])
        ref = recorded
        self.assertEqual(("restoring", "restore", "app-1-data", "app-1-data-restore", "u-old"),
                         (ref["phase"], ref["direction"], ref["old_claim"], ref["new_claim"], ref["old_uid"]))
        self.assertEqual("app-1-data-restore", result["name"])

    def test_the_routes_and_their_roles(self):
        import homestead_route_policy as POLICY
        self.assertIn("homestead_restore_swap", MODULE_ROUTES.MODULES)
        for method, path, role in (("GET", "/api/restore-swap/plan", "viewer"), ("POST", "/api/restore-swap", "admin"),
):
            with self.subTest(path=path):
                self.assertEqual(role, POLICY.role(path, method))


class ResolveTests(Base):
    def test_the_happy_path(self):
        _, item = self.started()
        self.run_until(item, "watching")
        ref = item["ref"]
        self.assertEqual(1, ref["replicas_before"])
        scales = [s[2]["spec"]["replicas"] for s in self.kube.writes(DEP, "application/merge-patch+json")]
        self.assertEqual([0, 1], scales)
        swaps = self.kube.writes(DEP, "application/json-patch+json")
        self.assertEqual(1, len(swaps))
        self.assertEqual([{"op": "test", "path": "/metadata/generation", "value": 4},
                          {"op": "test", "path": "/spec/template/spec/volumes/1/persistentVolumeClaim/claimName", "value": "app-1-data"},
                          {"op": "replace", "path": "/spec/template/spec/volumes/1/persistentVolumeClaim/claimName", "value": "app-1-data-restore"}],
                         swaps[0][2])
        labels, notes = self.marks(OLD)
        self.assertEqual("true", labels["homestead.io/previous-volume"])
        self.assertEqual(("lab/app-1", "app-1-data-restore", str(int(NOW)), "op1", "u-old", "restored"),
                         tuple(notes["homestead.io/" + k] for k in ("previous-of", "replaced-by", "replaced-at",
                                                                     "replaced-job", "replaced-uid", "replaced-reason")))
        self.assertEqual("lab/app-1", self.marks(NEW)[1]["homestead.io/restored-into"])
        self.assertEqual(("running", ), self.step(item, NOW + 5)[:1])         # rolled out: waiting for an answer
        self.uptime = {"lab/app-1": {"state": "up", "last": {"at": NOW + 10}}}
        status, _, message = self.step(item, NOW + 20)
        self.assertEqual("succeeded", status)
        self.assertIn("app-1 runs on app-1-data-restore", message)
        self.assertIn("previous volume app-1-data is kept until", message)

    def test_an_app_with_no_check_passes_once_it_stays_ready(self):
        _, item = self.started()
        self.run_until(item, "watching")
        self.step(item, NOW)
        self.assertEqual("running", self.step(item, NOW + 60)[0])
        self.assertEqual("succeeded", self.step(item, NOW + RS.AUTOUPDATE.STEADY + 1)[0])

    def test_resuming_a_step_never_scales_or_swaps_twice(self):
        _, item = self.started()
        self.run_until(item, "watching")
        # A replica that restarted right after the scale to 0, before it was
        # recorded, carries on from the checkpoint written before it.
        stop = next(c for c in self.checkpoints if c["ref"]["phase"] == "stopping" and "replicas_before" in c["ref"])
        again = copy.deepcopy(stop)
        self.run_until(again, "watching")
        self.assertEqual(1, again["ref"]["replicas_before"])
        # One that restarted after the swap was sent, before it was recorded.
        swap = copy.deepcopy(next(c for c in self.checkpoints if c["ref"]["phase"] == "swapping"))
        self.run_until(swap, "watching")
        self.assertEqual(1, len(self.kube.writes(DEP, "application/json-patch+json")))
        self.assertEqual(1, self.kube.objs[DEP]["spec"]["replicas"])
        self.assertEqual(str(int(NOW)), self.marks(OLD)[1]["homestead.io/replaced-at"])

    def test_a_swap_refused_because_the_app_was_edited_changes_nothing(self):
        _, item = self.started()
        send = self.kube.send

        def edited_meanwhile(method, path, body=None, ctype="application/json", **options):
            out = send(method, path, body, ctype)
            if path == DEP and ctype == "application/merge-patch+json" and body["spec"]["replicas"] == 0:
                self.kube.objs[DEP]["metadata"]["generation"] += 1      # someone's edit right after the stop
            return out
        RS.ksend = edited_meanwhile
        status, _, message = self.step(item, NOW)
        self.assertEqual("failed", status)
        self.assertIn("Nothing was changed", message)
        self.assertEqual("app-1-data", self.template_claim())
        self.assertEqual(1, self.kube.objs[DEP]["spec"]["replicas"])
        self.assertEqual("not-used", self.marks(NEW)[1]["homestead.io/replaced-reason"])


class SwapBackTests(Base):
    def swapped_back(self, item, why, now):
        status, _, message = self.step(item, now)
        self.assertEqual("running", status, message)
        self.assertEqual("back-starting", item["ref"]["phase"])
        self.assertEqual("app-1-data", self.template_claim())
        self.assertEqual(1, self.kube.objs[DEP]["spec"]["replicas"])
        old_labels, old_notes = self.marks(OLD)
        self.assertNotIn("homestead.io/previous-volume", old_labels)
        self.assertFalse([k for k in old_notes if k.startswith("homestead.io/replaced")])
        new_labels, new_notes = self.marks(NEW)
        self.assertEqual("true", new_labels["homestead.io/previous-volume"])
        self.assertEqual(("not-started", "u-new"), (new_notes["homestead.io/replaced-reason"], new_notes["homestead.io/replaced-uid"]))
        status, _, message = self.step(item, now + 1)
        self.assertEqual("failed", status)
        self.assertIn("Swapped back: on the restored volume app-1 " + why, message)
        self.assertIn("app-1-data-restore is kept until", message)

    def test_rollout_not_ready_in_time(self):
        _, item = self.started()
        self.kube.ready = False
        self.run_until(item, "watching")
        self.assertEqual("running", self.step(item, NOW + 60)[0])
        self.kube.ready = True
        self.swapped_back(item, "was not ready after 10 minutes", NOW + RS.AUTOUPDATE.ROLLOUT_WAIT + 1)

    def test_pods_stop_being_ready(self):
        _, item = self.started()
        self.run_until(item, "watching")
        self.step(item, NOW)
        self.kube.objs[DEP]["status"]["readyReplicas"] = 0
        self.assertEqual("running", self.step(item, NOW + 30)[0])     # one bad poll is not enough
        self.assertEqual("watching", item["ref"]["phase"])
        self.swapped_back(item, "stopped being ready", NOW + 30 + RS.FLAP_WAIT)

    def test_one_bad_poll_that_recovers_does_not_swap_back(self):
        _, item = self.started()
        self.run_until(item, "watching")
        self.step(item, NOW)
        self.kube.objs[DEP]["status"]["readyReplicas"] = 0
        self.assertEqual("running", self.step(item, NOW + 30)[0])
        self.kube.settle(self.kube.objs[DEP])
        self.uptime = {"lab/app-1": {"state": "up", "last": {"at": NOW + 40}}}
        self.assertEqual("succeeded", self.step(item, NOW + 30 + RS.FLAP_WAIT)[0])
        self.assertEqual("app-1-data-restore", self.template_claim())

    def test_the_uptime_check_says_down(self):
        _, item = self.started()
        self.run_until(item, "watching")
        self.step(item, NOW)
        self.uptime = {"lab/app-1": {"state": "down", "last": {"at": NOW + 5, "error": "connection refused"}}}
        self.assertEqual("running", self.step(item, NOW + 10)[0])
        self.swapped_back(item, "stopped answering (connection refused)", NOW + 10 + RS.FLAP_WAIT)

    def test_no_answer_in_time(self):
        _, item = self.started()
        self.run_until(item, "watching")
        self.step(item, NOW)
        self.uptime = {"lab/app-1": {"state": "up", "last": {"at": NOW - 100}}}
        self.swapped_back(item, "did not answer within 5 minutes", NOW + RS.AUTOUPDATE.ANSWER_WAIT + 1)


class BeforeTheSwapTests(Base):
    def test_a_failed_restore_changes_nothing_and_marks_the_claim_it_made(self):
        _, item = self.started()
        self.restore_result = ("failed", 20, "The restored Longhorn volume is faulted")
        status, _, message = self.step(item, NOW)
        self.assertEqual("failed", status)
        self.assertIn("Nothing was changed", message)
        self.assertEqual([], self.kube.writes(DEP))
        self.assertEqual("not-used", self.marks(NEW)[1]["homestead.io/replaced-reason"])

    def test_a_stop_that_times_out_starts_the_app_again(self):
        _, item = self.started()
        self.kube.loose.append({"metadata": {"name": "stuck"}, "status": {"phase": "Running"},
                                "spec": {"volumes": [{"persistentVolumeClaim": {"claimName": "app-1-data"}}]}})
        self.run_until(item, "stopping")
        self.assertEqual("running", self.step(item, NOW)[0])
        status, _, message = self.step(item, NOW + RS.STOP_WAIT + 1)
        self.assertEqual("failed", status)
        self.assertIn("Nothing was changed", message)
        self.assertEqual(1, self.kube.objs[DEP]["spec"]["replicas"])
        self.assertEqual("app-1-data", self.template_claim())


class CancelTests(Base):
    def test_before_the_swap_the_restore_stops_and_the_app_starts(self):
        _, item = self.started()
        self.kube.loose.append({"metadata": {"name": "stuck"}, "status": {"phase": "Running"},
                                "spec": {"volumes": [{"persistentVolumeClaim": {"claimName": "app-1-data"}}]}})
        self.assertEqual("running", self.step(item, NOW)[0])
        self.assertEqual(0, self.kube.objs[DEP]["spec"]["replicas"])
        plan = RS.cancel_plan(item)
        self.assertEqual("rollback", plan["mode"])
        message = RS.cancel_run(item, {})
        self.assertEqual("app-1-data-restore", self.cancelled[0]["name"])
        self.assertEqual(1, self.kube.objs[DEP]["spec"]["replicas"])
        self.assertIn("Nothing was changed", message)

    def test_after_the_swap_cancelling_swaps_back(self):
        _, item = self.started()
        self.run_until(item, "watching")
        message = RS.cancel_run(item, {})
        self.assertEqual("app-1-data", self.template_claim())
        self.assertEqual(1, self.kube.objs[DEP]["spec"]["replicas"])
        self.assertEqual("not-started", self.marks(NEW)[1]["homestead.io/replaced-reason"])
        self.assertEqual([], self.cancelled)
        self.assertIn("Swapped back", message)


class RegisterTests(Base):
    def test_the_storage_module_registers_the_kind(self):
        import homestead_storage_server as STORAGE_SERVER
        ops = types.SimpleNamespace(RESOLVERS={"volume-restore": lambda item: None}, RESUMABLE={}, CANCELLERS={},
                                    CLEANUPS=set(), checkpoint=lambda item: None)
        STORAGE_SERVER.register(ops)
        self.assertIn("restore-swap", ops.RESOLVERS)
        self.assertIn("restore-swap", ops.CANCELLERS)

    def test_a_second_restore_into_the_same_app_is_refused(self):
        self.ops.items.append({"kind": "restore-swap", "status": "running", "title": "Restore app-1 from a backup",
                               "resource": {"kind": "Deployment", "namespace": NS, "name": "app-1"},
                               "ref": {"namespace": NS, "app": "app-1", "old_claim": "a", "new_claim": "b"}})
        p = self.plan()
        self.assertFalse(p["ready"])
        self.assertTrue(any("already has a job" in b for b in p["blockers"]), p["blockers"])


KEPT = f"/api/v1/namespaces/{NS}/persistentvolumeclaims/app-1-data-old"


def previous(uid="u-prev", at=NOW - 10 * 86400, reminded=None, kept=False, reason="restored"):
    notes = {"homestead.io/previous-of": "lab/app-1", "homestead.io/replaced-by": "app-1-data",
             "homestead.io/replaced-at": str(int(at)), "homestead.io/replaced-uid": "u-prev",
             "homestead.io/replaced-job": "op0", "homestead.io/replaced-reason": reason}
    if reminded is not None:
        notes["homestead.io/reminded-at"] = str(int(reminded))
    if kept:
        notes["homestead.io/keep-previous"] = "true"
    return claim("app-1-data-old", uid, annotations=notes, labels={"homestead.io/previous-volume": "true"})


def record(uid="u-prev", at=NOW - 10 * 86400, reminded=None, kept=False, reason="restored", app="app-1"):
    return {"uid": uid, "app": app, "replaced_by": "app-1-data", "replaced_at": int(at), "job": "op0",
            "reason": reason, "reminded_at": int(reminded) if reminded else None, "kept": kept}


def put_previous(test, uid="u-prev", **kw):
    """A previous volume as Homestead keeps one: its own record, and the
    marks on the claim that mirror it."""
    test.kube.objs[KEPT] = previous(uid=uid, **{k: v for k, v in kw.items() if k != "app"})
    test.records["lab/app-1-data-old"] = record(**kw)


class ExpireTests(Base):
    def setUp(self):
        super().setUp()
        put_previous(self)

    def tick(self, now=NOW):
        return RS.expire_tick(now)

    def test_not_the_leader_reads_nothing(self):
        self.leader = False
        RS.kget = lambda *a, **k: self.fail("read on a replica that is not the leader")
        self.assertEqual([], self.tick())

    def test_keep_days_0_keeps_everything(self):
        self.settings = {"restore_swap": {"keep_days": 0}}
        put_previous(self, reminded=NOW - 3 * 86400)
        self.tick()
        self.assertEqual(([], []), (self.deleted, self.kube.sent))

    def test_a_kept_volume_is_left_alone(self):
        put_previous(self, reminded=NOW - 3 * 86400, kept=True)
        self.tick()
        self.assertEqual(([], []), (self.deleted, self.kube.sent))

    def test_a_day_before_its_expiry_it_is_reminded_not_deleted(self):
        put_previous(self, at=NOW - 6.5 * 86400)
        self.tick()
        self.assertEqual(str(int(NOW)), self.marks(KEPT)[1]["homestead.io/reminded-at"])
        self.assertEqual([], self.deleted)

    def test_past_its_expiry_without_a_reminder_it_is_reminded_first(self):
        self.tick()
        self.assertEqual(str(int(NOW)), self.marks(KEPT)[1]["homestead.io/reminded-at"])
        self.assertEqual([], self.deleted)

    def test_nothing_goes_less_than_a_day_after_its_reminder(self):
        put_previous(self, reminded=NOW - 20 * 3600)
        self.tick()
        self.assertEqual([], self.deleted)

    def test_past_expiry_and_reminded_a_day_ago_it_is_deleted_the_way_a_person_would(self):
        put_previous(self, reminded=NOW - 86400)
        self.tick()
        self.assertEqual([{"namespace": NS, "name": "app-1-data-old", "action": "delete_data", "uid": "u-prev",
                           "confirmation": "app-1-data-old"}], self.deleted)
        self.assertIn("Deleted app-1's previous volume app-1-data-old", self.notes[0]["title"])

    def test_a_volume_mounted_again_is_no_longer_kept_and_never_deleted(self):
        put_previous(self, reminded=NOW - 3 * 86400)
        self.kube.objs[DEP]["spec"]["template"]["spec"]["volumes"][1]["persistentVolumeClaim"]["claimName"] = "app-1-data-old"
        self.tick()
        self.assertEqual([], self.deleted)
        labels, notes = self.marks(KEPT)
        self.assertNotIn("homestead.io/previous-volume", labels)
        self.assertFalse([k for k in notes if k.startswith("homestead.io/replaced")])
        self.assertIn("app-1 uses app-1-data-old again", self.notes[0]["title"])

    def test_a_claim_made_again_under_the_same_name_is_never_taken(self):
        put_previous(self, reminded=NOW - 3 * 86400)
        self.kube.objs[KEPT]["metadata"]["uid"] = "u-someone-else"
        self.tick()
        self.assertEqual([], self.deleted)

    def test_a_volume_a_job_holds_is_skipped(self):
        put_previous(self, reminded=NOW - 3 * 86400)
        self.ops.items.append({"kind": "reclass", "status": "running", "resource": {"namespace": NS, "name": "app-1-data-old"},
                               "ref": {"namespace": NS, "claim": "app-1-data-old"}})
        self.tick()
        self.assertEqual([], self.deleted)

    def test_an_error_on_one_claim_leaves_the_others_to_carry_on(self):
        other = f"/api/v1/namespaces/{NS}/persistentvolumeclaims/b-old"
        put_previous(self, reminded=NOW - 3 * 86400)
        second = previous(reminded=NOW - 3 * 86400)
        second["metadata"]["name"] = "b-old"
        self.kube.add(other, second)
        self.records["lab/b-old"] = record(reminded=NOW - 3 * 86400)
        plan = RS.deletion_plan
        RS.deletion_plan = lambda ns, name: (_ for _ in ()).throw(ValueError("Longhorn did not answer")) \
            if name == "app-1-data-old" else plan(ns, name)
        self.tick()
        self.assertEqual(["b-old"], [d["name"] for d in self.deleted])


class AlertFactTests(Base):
    def test_a_reminded_volume_that_is_not_kept_is_one_info_alert_offering_keep(self):
        put_previous(self, at=NOW - 6.5 * 86400, reminded=NOW - 3600)
        facts = RS.alert_facts(NOW)
        self.assertEqual(1, len(facts))
        self.assertEqual(("info", "previous-volume:lab/app-1-data-old"), (facts[0]["severity"], facts[0]["key"]))
        self.assertEqual("app-1's previous volume app-1-data-old will be deleted tomorrow", facts[0]["title"])
        self.assertIn("Keep", facts[0]["body"])
        self.assertTrue(facts[0]["href"].startswith("/data-protection"))

    def test_kept_or_not_yet_reminded_raises_nothing(self):
        put_previous(self, at=NOW - 6.5 * 86400, reminded=NOW - 3600, kept=True)
        self.assertEqual([], RS.alert_facts(NOW))
        put_previous(self, at=NOW - 86400)
        self.assertEqual([], RS.alert_facts(NOW))


class PreviousTests(Base):
    def setUp(self):
        super().setUp()
        put_previous(self, at=NOW - 86400)

    def test_the_list(self):
        listing = RS.previous_volumes(NOW)
        self.assertEqual(7, listing["keep_days"])
        row = listing["volumes"][0]
        self.assertEqual({"namespace": NS, "name": "app-1-data-old", "app": "app-1", "replaced_by": "app-1-data",
                          "replaced_at": int(NOW - 86400), "expires_at": int(NOW + 6 * 86400), "reminded_at": None,
                          "kept": False, "reason": "restored", "in_use": False},
                         {k: row[k] for k in ("namespace", "name", "app", "replaced_by", "replaced_at", "expires_at",
                                              "reminded_at", "kept", "reason", "in_use")})
        self.assertTrue(row["digest"])
        self.assertEqual((True, ""), (row["can_swap_back"], row["managed"]))

    def test_keep_is_for_an_operator_and_un_keep_for_an_admin(self):
        put_previous(self, at=NOW - 86400, reminded=NOW - 60)
        row = RS.keep({"namespace": NS, "name": "app-1-data-old", "keep": True}, "operator")
        self.assertTrue(row["kept"])
        notes = self.marks(KEPT)[1]
        self.assertEqual("true", notes["homestead.io/keep-previous"])
        self.assertNotIn("homestead.io/reminded-at", notes)
        self.assertEqual((True, None), (self.records["lab/app-1-data-old"]["kept"], self.records["lab/app-1-data-old"]["reminded_at"]))
        with self.assertRaises(PermissionError):
            RS.keep({"namespace": NS, "name": "app-1-data-old", "keep": False}, "operator")
        self.assertFalse(RS.keep({"namespace": NS, "name": "app-1-data-old", "keep": False}, "admin")["kept"])
        self.assertNotIn("homestead.io/keep-previous", self.marks(KEPT)[1])

    def test_keep_refuses_a_claim_that_is_not_a_previous_volume(self):
        with self.assertRaisesRegex(ValueError, "not a previous volume"):
            RS.keep({"namespace": NS, "name": "app-1-data", "keep": True}, "admin")

    def test_swap_back_starts_the_same_job_the_other_way_round(self):
        row = RS.previous_volumes(NOW)["volumes"][0]
        stale = RS.swap_back({"namespace": NS, "name": "app-1-data-old", "digest": "old"})
        self.assertEqual(409, stale.code)
        RS.swap_back({"namespace": NS, "name": "app-1-data-old", "digest": row["digest"]})
        job = self.ops.started[-1]
        ref = job["ref"]
        self.assertEqual(("restore-swap", "back", "stopping", "app-1-data", "app-1-data-old"),
                         (job["kind"], ref["direction"], ref["phase"], ref["old_claim"], ref["new_claim"]))
        item = {"id": "op2", "status": "running", "ref": ref}
        self.checkpoints = []
        self.run_until(item, "watching")
        self.assertEqual("app-1-data-old", self.template_claim())
        self.assertEqual("swapped-back", self.marks(OLD)[1]["homestead.io/replaced-reason"])
        self.assertNotIn("homestead.io/previous-volume", self.marks(KEPT)[0])
        self.assertEqual(["lab/app-1-data"], list(self.records))
        self.assertEqual(("u-old", "swapped-back"), (self.records["lab/app-1-data"]["uid"], self.records["lab/app-1-data"]["reason"]))

    def test_swap_back_refuses_when_the_app_no_longer_uses_what_replaced_it(self):
        self.kube.objs[DEP]["spec"]["template"]["spec"]["volumes"][1]["persistentVolumeClaim"]["claimName"] = "other"
        with self.assertRaisesRegex(ValueError, "no longer uses app-1-data"):
            RS.swap_back({"namespace": NS, "name": "app-1-data-old", "digest": "x"})

    def test_the_setting_is_saved_and_checked(self):
        saved = []
        RS.save_settings = saved.append
        self.assertEqual(14, RS.set_keep_days(14)["keep_days"])
        self.assertEqual([{"restore_swap": {"keep_days": 14}}], saved)
        for bad in (-1, 91, "x"):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                RS.set_keep_days(bad)

    def test_the_routes_and_their_roles(self):
        import homestead_route_policy as POLICY
        for method, path, role in (("GET", "/api/volumes/previous", "viewer"),
                                   ("POST", "/api/volumes/previous/keep", "operator"),
                                   ("POST", "/api/volumes/previous/swap-back", "admin"),
                                   ("POST", "/api/volumes/previous/settings", "admin")):
            with self.subTest(path=path):
                self.assertEqual(role, POLICY.role(path, method))


class ProvenanceTests(Base):
    """Only Homestead's own record makes a claim a previous volume: labels and
    annotations anyone who may edit claims can write prove nothing (security
    review of the first draft)."""

    def foreign(self):
        foreign = previous(uid="u-foreign", reminded=NOW - 30 * 86400)
        foreign["metadata"]["name"] = "someones-data"
        foreign["metadata"]["annotations"]["homestead.io/replaced-uid"] = "u-foreign"
        self.kube.add(f"/api/v1/namespaces/{NS}/persistentvolumeclaims/someones-data", foreign)

    def test_a_hand_labelled_claim_is_never_deleted_listed_or_alerted(self):
        self.foreign()
        RS.expire_tick(NOW)
        self.assertEqual([], self.deleted)
        self.assertEqual([], [s for s in self.kube.sent if "someones-data" in s[1]])
        self.assertEqual([], RS.previous_volumes(NOW)["volumes"])
        self.assertEqual([], RS.alert_facts(NOW))
        for call in (lambda: RS.keep({"namespace": NS, "name": "someones-data", "keep": False}, "admin"),
                     lambda: RS.swap_back({"namespace": NS, "name": "someones-data", "digest": "x"})):
            with self.assertRaisesRegex(ValueError, "not a previous volume"):
                call()

    def test_a_forged_reminder_on_the_claim_does_not_hasten_the_deletion(self):
        put_previous(self)                                  # Homestead has not reminded anyone yet
        self.kube.objs[KEPT]["metadata"]["annotations"]["homestead.io/reminded-at"] = str(int(NOW - 30 * 86400))
        RS.expire_tick(NOW)
        self.assertEqual([], self.deleted)
        self.assertEqual(int(NOW), self.records["lab/app-1-data-old"]["reminded_at"])

    def test_a_record_whose_claim_was_made_again_is_dropped_not_deleted(self):
        put_previous(self, reminded=NOW - 3 * 86400)
        self.kube.objs[KEPT]["metadata"]["uid"] = "u-new-owner"
        RS.expire_tick(NOW)
        self.assertEqual([], self.deleted)
        self.assertEqual({}, self.records)

    def test_a_claim_made_again_under_the_same_name_is_never_recorded_or_deleted(self):
        _, item = self.started()
        self.restore_result = ("running", 50, "Restoring")
        self.step(item, NOW)
        self.kube.objs[OLD]["metadata"]["uid"] = "u-recreated"      # deleted and made again meanwhile
        self.restore_result = ("succeeded", 100, "Restored")
        status, _, message = self.run_until(item, "watching")
        self.assertEqual("failed", status)                            # not swapped onto someone else's claim
        self.assertEqual("app-1-data", self.template_claim())
        self.assertNotIn("lab/app-1-data", self.records)
        RS.expire_tick(NOW + 60 * 86400)
        RS.expire_tick(NOW + 62 * 86400)
        self.assertEqual([], [d for d in self.deleted if d["name"] == "app-1-data"])

    def test_a_restored_claim_made_again_is_not_recorded_when_swapping_back(self):
        _, item = self.started()
        self.run_until(item, "watching")
        self.kube.objs[NEW]["metadata"]["uid"] = "u-recreated"
        RS.cancel_run(item, {})
        self.assertNotIn("lab/app-1-data-restore", self.records)
        self.assertEqual("app-1-data", self.template_claim())

    def test_an_app_name_that_is_not_a_kubernetes_name_is_not_shown(self):
        put_previous(self, at=NOW - 6.5 * 86400, reminded=NOW - 3600, app="<b>x</b>")
        self.assertEqual("An app's previous volume app-1-data-old will be deleted tomorrow", RS.alert_facts(NOW)[0]["title"])


class SwapBackBlockerTests(Base):
    def setUp(self):
        super().setUp()
        put_previous(self, at=NOW - 86400)

    def go(self, **extra):
        row = RS.previous_volumes(NOW)["volumes"][0]
        return RS.swap_back({"namespace": NS, "name": "app-1-data-old", "digest": row["digest"], **extra})

    def refused(self, needle, **extra):
        with self.assertRaisesRegex(ValueError, needle):
            self.go(**extra)
        self.assertEqual([], self.ops.started)

    def test_homesteads_own_app(self):
        self.self_app = True
        self.refused("Homestead's own data")

    def test_a_system_namespace(self):
        RS.system_namespaces = lambda: {NS}
        self.refused("system namespaces")

    def test_another_workload_on_the_claim_it_would_leave(self):
        self.kube.add(f"/apis/apps/v1/namespaces/{NS}/deployments/app-2", deployment("app-2"))
        self.refused("app-2")

    def test_a_loose_pod_on_the_previous_volume(self):
        self.kube.loose.append({"metadata": {"name": "debug"}, "status": {"phase": "Running"},
                                "spec": {"volumes": [{"persistentVolumeClaim": {"claimName": "app-1-data-old"}}]}})
        self.refused("debug")

    def test_a_flux_app_needs_the_notice_accepted(self):
        self.kube.objs[DEP]["metadata"]["labels"] = {"kustomize.toolkit.fluxcd.io/name": "apps"}
        self.refused("Flux")
        self.go(accept_gitops=True)
        self.assertEqual("back", self.ops.started[-1]["ref"]["direction"])

    def test_the_app_and_claim_come_from_the_record_not_the_annotations(self):
        self.kube.objs[KEPT]["metadata"]["annotations"]["homestead.io/previous-of"] = "lab/app-2"
        self.kube.add(f"/apis/apps/v1/namespaces/{NS}/deployments/app-2", deployment("app-2", claim="other"))
        self.go()
        self.assertEqual("app-1", self.ops.started[-1]["ref"]["app"])


class RecordStoreTests(unittest.TestCase):
    def test_records_live_on_homesteads_data_volume(self):
        import tempfile
        import homestead_shared as SHARED
        saved = SHARED.DIR
        self.addCleanup(lambda: setattr(SHARED, "DIR", saved))
        with tempfile.TemporaryDirectory() as folder:
            SHARED.DIR = folder
            RS.update_records(lambda records: records.update({"lab/a": {"uid": "u1"}}))
            self.assertEqual({"lab/a": {"uid": "u1"}}, RS.read_records())
            self.assertTrue(Path(folder, "previous-volumes.json").exists())


class ReviewFixTests(Base):
    """The fresh review's Important findings, each red first."""

    def test_keep_pressed_while_the_expiry_runs_stops_the_delete(self):
        put_previous(self, reminded=NOW - 3 * 86400)
        plan = RS.deletion_plan

        def keep_meanwhile(ns, name):
            self.records["lab/app-1-data-old"]["kept"] = True        # Keep, between the read and the delete
            return plan(ns, name)
        RS.deletion_plan = keep_meanwhile
        RS.expire_tick(NOW)
        self.assertEqual([], self.deleted)

    def test_a_swap_back_started_while_the_expiry_runs_stops_the_delete(self):
        put_previous(self, reminded=NOW - 3 * 86400)
        plan = RS.deletion_plan

        def swap_back_meanwhile(ns, name):
            self.ops.items.append({"kind": "restore-swap", "status": "queued", "resource": {"kind": "Deployment", "namespace": NS, "name": "app-1"},
                                   "ref": {"namespace": NS, "app": "app-1", "old_claim": "app-1-data", "new_claim": "app-1-data-old"}})
            return plan(ns, name)
        RS.deletion_plan = swap_back_meanwhile
        RS.expire_tick(NOW)
        self.assertEqual([], self.deleted)

    def back_job(self):
        row = RS.previous_volumes(NOW)["volumes"][0]
        RS.swap_back({"namespace": NS, "name": "app-1-data-old", "digest": row["digest"]})
        self.checkpoints = []
        return {"id": "op2", "status": "running", "ref": self.ops.started[-1]["ref"]}

    def test_a_failed_swap_back_keeps_a_kept_volume_kept(self):
        put_previous(self, at=NOW - 86400, kept=True)
        item = self.back_job()
        self.run_until(item, "watching")
        self.step(item, NOW)
        self.uptime = {"lab/app-1": {"state": "down", "last": {"at": NOW + 5, "error": "refused"}}}
        self.step(item, NOW + 10)
        self.step(item, NOW + 10 + RS.FLAP_WAIT)                      # swapped back to app-1-data
        self.assertEqual("app-1-data", self.template_claim())
        self.assertTrue(self.records["lab/app-1-data-old"]["kept"])

    def test_swap_back_refuses_a_previous_volume_made_again_under_its_name(self):
        put_previous(self, at=NOW - 86400)
        item = self.back_job()
        self.kube.objs[KEPT]["metadata"]["uid"] = "u-someone-else"      # deleted and made again meanwhile
        status, _, message = self.run_until(item, "watching")
        self.assertEqual("failed", status)
        self.assertIn("replaced by another claim", message)
        self.assertEqual("app-1-data", self.template_claim())
        self.assertEqual(1, self.kube.objs[DEP]["spec"]["replicas"])

    def failing_swap_back(self, code, times):
        _, item = self.started()
        self.run_until(item, "watching")
        self.step(item, NOW)
        send, left = self.kube.send, [times]

        def flaky(method, path, body=None, ctype="application/json", **options):
            if ctype == "application/json-patch+json" and left[0]:
                left[0] -= 1
                raise urllib.error.HTTPError(path, code, "error", {}, None)
            return send(method, path, body, ctype)
        RS.ksend = flaky
        self.uptime = {"lab/app-1": {"state": "down", "last": {"at": NOW + 5, "error": "refused"}}}
        self.step(item, NOW + 10)
        return item

    def test_a_passing_error_while_swapping_back_is_tried_again(self):
        item = self.failing_swap_back(500, 1)
        with self.assertRaises(urllib.error.HTTPError):
            self.step(item, NOW + 10 + RS.FLAP_WAIT)
        self.assertEqual("swapping-back", item["ref"]["phase"])
        self.assertEqual("running", self.step(item, NOW + 20 + RS.FLAP_WAIT)[0])
        self.assertEqual(("back-starting", "app-1-data"), (item["ref"]["phase"], self.template_claim()))

    def test_a_swap_back_that_cannot_be_done_never_leaves_the_app_stopped(self):
        item = self.failing_swap_back(422, 99)
        status, _, message = self.step(item, NOW + 10 + RS.FLAP_WAIT)
        self.assertEqual("failed", status)
        self.assertIn("did not work either", message)
        self.assertEqual(1, self.kube.objs[DEP]["spec"]["replicas"])
        self.assertIn("app-1 was started again", message)

    def test_a_swap_back_that_keeps_failing_gives_up_and_starts_the_app(self):
        item = self.failing_swap_back(500, 99)
        with self.assertRaises(urllib.error.HTTPError):
            self.step(item, NOW + 10 + RS.FLAP_WAIT)
        status, _, message = self.step(item, NOW + 10 + RS.FLAP_WAIT + RS.STOP_WAIT + 1)
        self.assertEqual("failed", status)
        self.assertEqual(1, self.kube.objs[DEP]["spec"]["replicas"])

    def test_settings_that_cannot_be_read_delete_nothing(self):
        put_previous(self, reminded=NOW - 3 * 86400)
        RS.settings_strict = lambda: (_ for _ in ()).throw(ValueError("settings.json is not valid JSON"))
        RS.expire_tick(NOW)
        self.assertEqual([], self.deleted)

    def test_other_storage_jobs_refuse_a_claim_a_restore_into_an_app_holds(self):
        import homestead_storage_conflicts as STORAGE_CONFLICTS
        import homestead_storage_guard as STORAGE_GUARD
        running = {"kind": "restore-swap", "status": "running", "resource": {"kind": "Deployment", "namespace": NS, "name": "app-1"},
                   "ref": {"namespace": NS, "app": "app-1", "old_claim": "app-1-data", "new_claim": "app-1-data-restore",
                           "old_volume": "pv-u-old"}}
        for kind, ref, resource in (("reclass", {"namespace": NS, "claim": "app-1-data"}, {"namespace": NS, "name": "app-1-data"}),
                                    ("snapshot-revert", {"namespace": NS, "claim": "app-1-data", "volume": "pv-u-old"}, {}),
                                    ("volume-restore", {"namespace": NS, "name": "app-1-data-restore"}, {})):
            with self.subTest(kind=kind):
                with self.assertRaises(ValueError):
                    STORAGE_CONFLICTS.require_clear([running], kind, ref, resource)
                STORAGE_CONFLICTS.require_clear([{**running, "status": "failed"}], kind, ref, resource)
        ops = types.SimpleNamespace(transaction=contextlib.nullcontext, stored=lambda: [running])
        plan = STORAGE_GUARD.review({"namespace": NS, "name": "app-1-data-restore", "pv": {}, "longhorn": {}}, ops, None)
        self.assertTrue(plan["blocked"])
        free = STORAGE_GUARD.review({"namespace": NS, "name": "other", "pv": {}, "longhorn": {}}, ops, None)
        self.assertFalse(free.get("blocked"))

    def test_the_review_says_when_the_app_has_other_volumes(self):
        dep = self.kube.objs[DEP]
        dep["spec"]["template"]["spec"]["volumes"].append({"name": "db", "persistentVolumeClaim": {"claimName": "app-1-db"}})
        notices = self.plan()["notices"]
        self.assertTrue(any("app-1-db" in n for n in notices), notices)


class SettingsFallbackTests(Base):
    """keep_days for the expiry (Andreas, 2026-10-21): settings never saved
    mean the 7-day default; settings that cannot be read right now mean
    nothing is deleted this hour."""

    def setUp(self):
        super().setUp()
        RS.settings_strict = STRICT
        put_previous(self, reminded=NOW - 3 * 86400)
        import homestead_settings_helpers as SETTINGS_HELPERS
        self.helpers = SETTINGS_HELPERS
        for name, value in (("DEFAULT_NS", "homestead"), ("_settings_map", lambda: "homestead-settings")):
            patcher = mock.patch.object(SETTINGS_HELPERS, name, value, create=True)
            patcher.start()
            self.addCleanup(patcher.stop)

    def read(self, answer):
        def kget(path, **_):
            if isinstance(answer, BaseException):
                raise answer
            return answer
        patcher = mock.patch.object(self.helpers, "kget", kget, create=True)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_settings_never_saved_use_the_7_day_default(self):
        self.read(urllib.error.HTTPError("/configmaps/homestead-settings", 404, "not found", {}, None))
        RS.expire_tick(NOW)
        self.assertEqual(["app-1-data-old"], [d["name"] for d in self.deleted])

    def test_saved_settings_without_the_key_use_the_7_day_default(self):
        self.read({"data": {"settings.json": json.dumps({"site_name": "Main site"})}})
        RS.expire_tick(NOW)
        self.assertEqual(["app-1-data-old"], [d["name"] for d in self.deleted])

    def test_settings_that_cannot_be_read_right_now_delete_nothing(self):
        for failure in (urllib.error.HTTPError("/configmaps/homestead-settings", 500, "error", {}, None),
                        TimeoutError("timed out"), urllib.error.URLError("connection refused")):
            with self.subTest(failure=type(failure).__name__):
                self.read(failure)
                self.assertEqual([], RS.expire_tick(NOW))
                self.assertEqual([], self.deleted)

    def test_0_still_keeps_until_deleted(self):
        self.read({"data": {"settings.json": json.dumps({"restore_swap": {"keep_days": 0}})}})
        self.assertEqual([], RS.expire_tick(NOW))
        self.assertEqual([], self.deleted)


class WrapUpTests(Base):
    """Wrap-up rows 48-55 (#773 fix pass), each red first."""

    # 48
    def test_the_review_and_the_restore_run_outside_the_jobs_lock(self):
        depths, plan = [], RS.restore_plan
        RS.restore_plan = lambda *a: depths.append(self.ops.depth) or plan(*a)
        self.started()
        self.assertTrue(depths)
        self.assertEqual([0], sorted(set(depths)))
        self.assertEqual([], self.restored)

    def test_a_start_the_job_store_refuses_leaves_no_claim(self):
        self.ops.refuse = "Another job holds this volume"
        with self.assertRaisesRegex(ValueError, "Another job"):
            self.started()
        self.assertEqual([], self.restored)
        self.assertNotIn(NEW, self.kube.objs)

    # 49
    def test_a_block_claim_is_restored_as_block(self):
        self.kube.objs[OLD]["spec"]["volumeMode"] = "Block"
        _, item = self.started()
        self.step(item, NOW)
        self.assertEqual("Block", self.restored[0]["volume_mode"])

    def test_a_claim_grown_since_the_backup_comes_back_at_its_size(self):
        self.kube.objs[OLD]["spec"]["resources"]["requests"]["storage"] = "30Gi"
        self.assertEqual(30, self.plan()["size_gb"])
        self.kube.objs["/apis/storage.k8s.io/v1/storageclasses/longhorn"]["allowVolumeExpansion"] = False
        p = self.plan()
        self.assertFalse(p["ready"])
        self.assertTrue(any("30 GiB" in b for b in p["blockers"]), p["blockers"])

    def test_an_access_mode_a_restore_cannot_make_is_refused_in_the_review(self):
        self.kube.objs[OLD]["spec"]["accessModes"] = ["ReadWriteOncePod"]
        p = self.plan()
        self.assertFalse(p["ready"])
        self.assertTrue(any("ReadWriteOncePod" in b for b in p["blockers"]), p["blockers"])

    # 50
    def test_a_claim_under_the_new_name_that_is_not_this_restores_is_not_taken(self):
        _, item = self.started()
        self.restore_result = ("running", 10, "Restoring")
        self.step(item, NOW)
        self.kube.objs[NEW]["metadata"]["annotations"]["homestead.io/restore-id"] = "someone-elses"
        self.restore_result = ("succeeded", 100, "Restored")
        status, _, message = self.step(item, NOW)
        self.assertEqual("failed", status)
        self.assertNotIn("lab/app-1-data-restore", self.records)
        self.assertEqual([], self.kube.writes(DEP))

    def test_a_restore_that_fails_after_making_its_claim_keeps_it_recorded(self):
        _, item = self.started()
        self.restore_result = ("failed", 20, "faulted")
        self.step(item, NOW)
        self.assertEqual("u-new", self.records["lab/app-1-data-restore"]["uid"])

    def test_cancel_deletes_only_the_claim_this_restore_made(self):
        _, item = self.started()
        self.restore_result = ("running", 10, "Restoring")
        self.step(item, NOW)
        self.kube.objs[NEW]["metadata"]["annotations"]["homestead.io/restore-id"] = "someone-elses"
        RS.cancel_run(item, {})
        self.assertEqual([], self.cancelled)
        self.kube.objs[NEW]["metadata"]["annotations"]["homestead.io/restore-id"] = item["ref"]["restore"]["restore_config"]["restore_id"]
        RS.cancel_run(item, {})
        self.assertEqual(["app-1-data-restore"], [c["name"] for c in self.cancelled])

    # 51
    def test_a_kept_volume_mounted_again_is_no_longer_recorded(self):
        put_previous(self, kept=True)
        self.kube.objs[DEP]["spec"]["template"]["spec"]["volumes"][1]["persistentVolumeClaim"]["claimName"] = "app-1-data-old"
        RS.expire_tick(NOW)
        self.assertEqual({}, self.records)
        self.assertIn("uses app-1-data-old again", self.notes[0]["title"])

    def test_keep_on_a_record_dropped_meanwhile_is_refused_not_a_500(self):
        put_previous(self, at=NOW - 86400)
        listed = copy.deepcopy(self.records)
        self.records.clear()                                   # the hourly tick dropped it meanwhile
        RS.read_records = lambda: copy.deepcopy(listed)
        with self.assertRaisesRegex(ValueError, "not a previous volume"):
            RS.keep({"namespace": NS, "name": "app-1-data-old", "keep": True}, "operator")

    def test_marks_that_failed_to_reach_the_claim_are_sent_again(self):
        _, item = self.started()
        send, failed = self.kube.send, []

        def once(method, path, body=None, ctype="application/json", **options):
            if path == OLD and not failed:
                failed.append(1)
                raise urllib.error.HTTPError(path, 500, "error", {}, None)
            return send(method, path, body, ctype)
        RS.ksend = once
        with self.assertRaises(urllib.error.HTTPError):
            self.run_until(item, "watching")
        self.run_until(item, "watching")
        self.assertEqual("true", self.marks(OLD)[0].get("homestead.io/previous-volume"))

    # 52
    def test_a_put_back_whose_start_fails_is_finished_not_swapped(self):
        _, item = self.started()
        self.kube.loose.append({"metadata": {"name": "stuck"}, "status": {"phase": "Running"},
                                "spec": {"volumes": [{"persistentVolumeClaim": {"claimName": "app-1-data"}}]}})
        self.step(item, NOW)
        send = self.kube.send

        def scale_fails(method, path, body=None, ctype="application/json", **options):
            if path == DEP and (body or {}).get("spec", {}).get("replicas") == 1:
                raise urllib.error.HTTPError(path, 500, "error", {}, None)
            return send(method, path, body, ctype)
        RS.ksend = scale_fails
        with self.assertRaises(urllib.error.HTTPError):
            self.step(item, NOW + RS.STOP_WAIT + 1)
        self.assertEqual("put-back", item["ref"]["phase"])
        self.kube.loose.clear()                                  # the pod lets go later
        RS.ksend = send
        status, _, message = self.step(item, NOW + RS.STOP_WAIT + 2)
        self.assertEqual("failed", status)
        self.assertEqual(("app-1-data", 1), (self.template_claim(), self.kube.objs[DEP]["spec"]["replicas"]))

    def test_a_pod_named_like_the_app_but_not_its_own_is_loose(self):
        self.kube.loose.append({"metadata": {"name": "app-1-debug"}, "status": {"phase": "Running"},
                                "spec": {"volumes": [{"persistentVolumeClaim": {"claimName": "app-1-data"}}]}})
        p = self.plan()
        self.assertTrue(any("app-1-debug" in b for b in p["blockers"]), p["blockers"])

    # 53
    def test_a_volume_that_cannot_be_deleted_says_why(self):
        put_previous(self, reminded=NOW - 3 * 86400)
        self.blocking = ["Longhorn still reports the volume attached to node-1"]
        RS.expire_tick(NOW)
        self.assertEqual([], self.deleted)
        facts = RS.alert_facts(NOW)
        self.assertEqual(1, len(facts))
        self.assertIn("could not be deleted", facts[0]["title"])
        self.assertIn("attached", facts[0]["body"])

    def test_keep_days_is_a_whole_number_and_its_section_an_object(self):
        import homestead_app_settings as APP_SETTINGS
        with self.assertRaises(ValueError):
            RS.set_keep_days(7.9)
        for bad in ({"keep_days": 7.5}, [7], "7"):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                APP_SETTINGS.validate({"restore_swap": bad})

    # 54
    def test_a_volume_set_to_retain_is_never_deleted_and_the_alert_says_so(self):
        put_previous(self, reminded=NOW - 3 * 86400)
        self.reclaim = "Retain"
        RS.expire_tick(NOW)
        self.assertEqual([], self.deleted)
        facts = RS.alert_facts(NOW)
        self.assertIn("Retain", facts[0]["body"])

    def test_after_flux_puts_the_old_claim_back_the_restored_one_is_kept_as_previous(self):
        _, item = self.started()
        self.run_until(item, "watching")
        self.step(item, NOW)
        self.assertEqual("succeeded", self.step(item, NOW + RS.AUTOUPDATE.STEADY + 1)[0])
        # Flux applies the old manifest again.
        self.kube.objs[DEP]["spec"]["template"]["spec"]["volumes"][1]["persistentVolumeClaim"]["claimName"] = "app-1-data"
        RS.expire_tick(NOW + 3600)
        self.assertNotIn("lab/app-1-data", self.records)
        self.assertEqual(("u-new", "not-used"), (self.records["lab/app-1-data-restore"]["uid"],
                                                 self.records["lab/app-1-data-restore"]["reason"]))
        self.assertIn("app-1-data-restore", self.notes[-1]["body"])


class SettingsTests(unittest.TestCase):
    def test_keep_days_defaults_to_7_and_is_0_or_1_to_90(self):
        import homestead_app_settings as APP_SETTINGS
        self.assertEqual({"keep_days": 7}, APP_SETTINGS.validate({})["restore_swap"])
        self.assertEqual(0, APP_SETTINGS.validate({"restore_swap": {"keep_days": 0}})["restore_swap"]["keep_days"])
        self.assertEqual(90, APP_SETTINGS.validate({"restore_swap": {"keep_days": 90}})["restore_swap"]["keep_days"])
        for bad in (-1, 91, "soon"):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                APP_SETTINGS.validate({"restore_swap": {"keep_days": bad}})


class LeaderLoopTests(unittest.TestCase):
    def test_the_hourly_data_protection_tick_runs_both_even_when_one_fails(self):
        import homestead_storage_server as STORAGE_SERVER
        ran = []
        saved = STORAGE_SERVER.restore_tests_tick, RS.expire_tick
        self.addCleanup(lambda: (setattr(STORAGE_SERVER, "restore_tests_tick", saved[0]), setattr(RS, "expire_tick", saved[1])))
        STORAGE_SERVER.restore_tests_tick = lambda: ran.append("tests") or (_ for _ in ()).throw(ValueError("no backups"))
        RS.expire_tick = lambda now=None: ran.append("expiry") or []
        with self.assertRaisesRegex(ValueError, "no backups"):
            STORAGE_SERVER.data_protection_tick()
        self.assertEqual(["tests", "expiry"], ran)


if __name__ == "__main__":
    unittest.main()
