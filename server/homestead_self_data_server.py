"""The code for Homestead's own data and pod that lived in server.py (track 1,
part 7): the shared-data write scope, where Homestead runs and its own node
and address, its data volume's strategy, preparing and previewing a data
move, its progress and state, permissions, replacing its own pod, and the
data-move routes server.py's handler answered. What reads or writes the
fence state server.py holds (_self_data_fence and the boot flags) stays in
server.py.

server.py binds what it still owns through bind_routes(): each helper in
ROUTE_NEEDS is looked up in server.py when it is called, or, once moved
(server.MOVED), in the module it moved to, so a test that patches it there
reaches it here too; each name in VALUES is server.py's own value."""
from contextlib import nullcontext
import copy
import os
import re
import time
import urllib.request, urllib.parse, urllib.error
import homestead_capacity_review as CAPACITY_REVIEW
import homestead_kube as KUBE
import homestead_names as NAMES
import homestead_network_inventory as INV
import homestead_operations as OPS
import homestead_routes as MODULE_ROUTES
import homestead_self as SELF
import homestead_self_address as SELF_ADDRESS
import homestead_self_data_execute as SELF_DATA_EXECUTE
import homestead_self_data_fence as SELF_DATA_FENCE
import homestead_self_data_prepare as SELF_DATA_PREPARE
import homestead_self_data_review as SELF_DATA_REVIEW
import homestead_self_data_route as SELF_DATA_ROUTE
import homestead_self_data_worker as SELF_DATA_WORKER
import homestead_self_health as SELF_HEALTH
import homestead_storage_runtime as STORAGE_RUNTIME
import homestead_update_rollouts as ROLLOUTS

ROUTE_NEEDS = ("abandon_self_data_preparation", "cached", "class_selectable", "_dns_name",
               "get_app_settings", "kget", "_ksend", "ksend", "require_self_data_write",
               "self_data_activity", "self_data_handoff_status", "shared_storage_classes",
               "start_self_data_move", "storage_classes")
VALUES = ("DATA_DIR", "HOMESTEAD_VERSION", "LB_IP", "POD_NAME")


def bind_routes(helpers, **values):
    missing = set(ROUTE_NEEDS) - set(helpers)
    if missing:
        raise TypeError("homestead_self_data_server needs " + ", ".join(sorted(missing)))
    if set(values) != set(VALUES):
        raise TypeError("homestead_self_data_server is bound with " + ", ".join(sorted(set(values) ^ set(VALUES))))
    globals().update({name: helpers[name] for name in ROUTE_NEEDS})
    globals().update(values)


def self_data_file_scope(path):
    root, candidate = os.path.realpath(DATA_DIR), os.path.realpath(path)
    try:
        within = os.path.commonpath((root, candidate)) == root
    except ValueError:
        within = False
    if within:
        # Also preserve the standalone check used by test/demo integrations
        # which bind a fence without creating a Linux activity barrier.
        require_self_data_write()
        return self_data_activity()
    return nullcontext()


def self_stop_warning():
    return (f"Stopping Homestead takes this page down with it, and nothing here can start it again: "
            f"it stays down until someone runs  kubectl -n {SELF.NS} scale deployment/{NAMES.BRAND} --replicas=1  "
            "on the cluster. Restart it instead if it needs a fresh start.")


def homestead_running_on():
    """The claim Homestead's own Deployment mounts for its data, and whether
    it is ready on it now - read live, never from a cache."""
    dep = kget(f"/apis/apps/v1/namespaces/{SELF.NS}/deployments/{NAMES.BRAND}")
    spec, status = dep.get("spec") or {}, dep.get("status") or {}
    volumes = (spec.get("template") or {}).get("spec", {}).get("volumes") or []
    data = next((v for v in volumes if v.get("name") == "data"), None) or next(
        (v for v in volumes if v.get("persistentVolumeClaim")), {})
    claim = (data.get("persistentVolumeClaim") or {}).get("claimName", "")
    meta = dep.get("metadata") or {}
    ready = (int(status.get("readyReplicas") or 0) >= 1
             and int(status.get("observedGeneration") or 0) >= int(meta.get("generation") or 0))
    return claim, ready


def own_node():
    """The node this Homestead pod runs on."""
    try:
        return kget(f"/api/v1/namespaces/{SELF.NS}/pods/{os.environ.get('HOSTNAME', '')}")["spec"]["nodeName"]
    except Exception:
        return ""


# Linked clusters: each knows the others by the address they reach it at,
# which is its own VIP unless an admin says otherwise.
def fleet_address():
    """Homestead's web page on its VIP where it has one; else the address
    it started with (on k3s, a node's own)."""
    try:
        url = SELF_ADDRESS.report(cached("network", 5, INV.inventory)).get("url") or ""
    except Exception:
        url = ""
    return url or (f"http://{LB_IP}:8088" if LB_IP else "")


def fit_own_strategy():
    """An update from this page changes only the image, so a Deployment that
    was once set to roll keeps rolling. Put right at start-up what the data
    volume can take. The strategy is not part of the pod template, so this
    starts no rollout."""
    try:
        ns, name = SELF.NS, NAMES.BRAND
        dep = kget(f"/apis/apps/v1/namespaces/{ns}/deployments/{name}")
        want = SELF_HEALTH.own_strategy(homestead_data_volume(dep)["shareable"])
        if (dep["spec"].get("strategy") or {}).get("type") != want["type"]:
            ksend("PATCH", f"/apis/apps/v1/namespaces/{ns}/deployments/{name}",
                  {"spec": {"strategy": {"type": want["type"], "rollingUpdate": want.get("rollingUpdate")}}},
                  ctype="application/merge-patch+json")
            print(f"own update strategy set to {want['type']}", flush=True)
    except Exception as error:
        print(f"could not check Homestead's own update strategy: {error}", flush=True)


def self_data_preparation(body, actor, *, start=False):
    """Review or enqueue destination preparation; never switches the app PVC."""
    try:
        # No resolver polling in review. Execution recomputes under the shared
        # operations lock, before recording any preparation intent.
        def reviewed():
            _require_no_data_handoff(kget, SELF.NS)
            info = homestead_data_volume()
            row = next((r for r in info.get("classes", []) if r["name"] == body.get("storage_class")), None)
            if row is None: raise SELF_DATA_FENCE.Held("Choose an available destination storage class")
            _, image = _self_data_helper_image(kget, SELF.NS)
            return SELF_DATA_PREPARE.review(kget, SELF.NS, NAMES.BRAND, body, actor=actor, image=image,
                access_mode="ReadWriteMany" if row["shareable"] else "ReadWriteOnce",
                threshold=get_app_settings()["thresholds"]["memory"]["critical"], clock=time.time)
        if not start:
            cfg, public, context, _ = reviewed()
            return {**public, "capacity_token": CAPACITY_REVIEW.issue(cfg, context)}
        with OPS.transaction():
            result = reviewed()
            # Cleanup may have finished before Jobs' next refresh. Reconcile
            # only the verified completed handoff; do not advance other jobs.
            SELF_DATA_EXECUTE.reconcile_completed(OPS, DATA_DIR, SELF.NS, NAMES.BRAND)
            operation = SELF_DATA_PREPARE.start(body, result, OPS)
            return {"ok": True, "operation": operation, "destination": result[3]["destination"],
                    "detail": "Preparing the new volume. Homestead stays on its original data until you confirm the final move."}
    except SELF_DATA_FENCE.Held:
        raise
    except Exception:
        raise SELF_DATA_FENCE.Held("Destination preparation is unavailable. Inspect jobs before retrying; no data volume was deleted") from None


def self_data_preparation_progress(item):
    try:
        return SELF_DATA_PREPARE.resolve(item, kget, ksend, OPS.checkpoint, clock=time.time)
    except SELF_DATA_FENCE.Held as error:
        item["ref"]["retain_resources"] = True
        return "failed", item.get("progress", 0), str(error)


def self_data_preparation_state():
    """Settings can reopen preparation without advancing any job or exposing refs."""
    try:
        info = homestead_data_volume()
        nodes = SELF_DATA_PREPARE.D._inventory(kget, "/api/v1/nodes")
        jobs = []
        for item in OPS.stored():
            ref = item.get("ref", {})
            if (item.get("kind") != SELF_DATA_PREPARE.KIND or ref.get("namespace") != SELF.NS
                    or OPS._archived_preparation(item)):
                continue
            reusable = (item["status"] == "succeeded" and bool(ref.get("prepared")) and
                        ref["source"]["name"] == info.get("pvc") and ref["destination"] != info.get("pvc"))
            message = item.get("message", "")
            if item["status"] == "succeeded" and ref.get("prepared") and not reusable:
                message = ("Homestead currently uses this volume. The preparation is complete." if ref["destination"] == info.get("pvc")
                           else "Prepared for an earlier source volume. It cannot move Homestead's current data; the volume is retained.")
            jobs.append({"id": item["id"], "operation": ref["operation"], "destination": ref["destination"],
                "source": ref["source"]["name"], "storage_class": ref["storage_class"], "node": ref["node"],
                "status": item["status"], "progress": item.get("progress", 0), "message": message, "prepared": reusable,
                "archivable": SELF_DATA_PREPARE.can_archive(item)})
        return {"source": info.get("pvc"), "classes": info.get("classes", []), "preparations": jobs,
            "execution_ready": True, "blocking_jobs": SELF_DATA_PREPARE.blocking_jobs(OPS),
            "nodes": [{"name": n["metadata"]["name"], "ready": not n["metadata"].get("deletionTimestamp")
                and not n.get("spec", {}).get("unschedulable") and any(c.get("type") == "Ready" and c.get("status") == "True"
                    for c in n.get("status", {}).get("conditions", []))} for n in nodes]}
    except Exception:
        raise SELF_DATA_FENCE.Held("Data preparation status is unavailable. Existing jobs and volumes have not been changed") from None


def preview_self_data_move(body, actor):
    """Read-only final review for a prepared, bound destination.

    Provisioning and execution are connected separately. Never accept a worker
    image, capacity receipt, actor or cluster identity from the request body.
    """
    try:
        # Read the saved job snapshot, not list_operations(): polling resolvers
        # can change workloads or write job history during an alleged preview.
        blockers = SELF_DATA_PREPARE.blocking_jobs(OPS)
        if blockers:
            raise SELF_DATA_FENCE.Held(SELF_DATA_PREPARE.blocked_message("Finish running jobs and review retained recovery jobs before moving Homestead's data", blockers))
        ns = SELF.NS
        cache = {}
        def read(path):
            if path not in cache: cache[path] = copy.deepcopy(kget(path))
            return copy.deepcopy(cache[path])
        _require_no_data_handoff(read, ns)
        pod, image = _self_data_helper_image(read, ns)
        def runtime_check(pods, claim):
            return STORAGE_RUNTIME.require_self_data(OPS, pods, ns, NAMES.BRAND, HOMESTEAD_VERSION, claim,
                own_uid=pod["metadata"]["uid"], data_mount=DATA_DIR)
        result = SELF_DATA_REVIEW.Review(read, ns, NAMES.BRAND, actor=actor, image=image,
            threshold=get_app_settings()["thresholds"]["memory"]["critical"], source_pod=pod, data_dir=DATA_DIR,
            runtime_check=runtime_check, clock=time.time, route_check=SELF_DATA_ROUTE.review).preview(body)
        return result
    except SELF_DATA_FENCE.Held:
        raise
    except Exception:
        raise SELF_DATA_FENCE.Held("The data move review is unavailable. Nothing was changed; check cluster access and volume readiness") from None


def move_homestead_data(storage_class):
    raise SELF_DATA_FENCE.Held("The live-copy mover was retired. Open Settings to prepare and confirm a verified data move.")


def _data_move_status(item):
    # Old jobs lack pre-write receipts and a quiesced-copy proof. Reading one
    # after an upgrade must not silently switch claims or report safe success.
    item["ref"]["retain_resources"] = True
    return "failed", 0, "This older data move needs manual review. Both volumes are retained; no copy, restart or claim switch was requested."


def _reconcile_permissions():
    """Bring Homestead's own ClusterRole up to this release, before anything needs it."""
    try:
        with self_data_activity():
            result = SELF.reconcile()
    except Exception as error:
        print(f"permissions: not checked ({str(error)[:120]})", flush=True)
        return
    print(f"permissions: {result['state']} - {result['detail']}", flush=True)
    try:
        with self_data_activity():
            adopted = SELF.adopt_old_keys()
        if adopted.get("changed"):
            print(f"moved {adopted['changed']} objects' keys to {NAMES.DOMAIN}", flush=True)
    except Exception as error:
        print(f"old keys: not moved ({str(error)[:120]})", flush=True)


def _replace_own_pod():
    """Delete this pod so the Deployment makes one with a fresh data mount.
    Straight to the API: the usual guards read the data that is stale."""
    if not POD_NAME:
        raise ValueError("this pod's name is unknown")
    _ksend("DELETE", f"/api/v1/namespaces/{SELF.NS}/pods/{urllib.parse.quote(POD_NAME, safe='')}", shutdown_bypass=True)


# The routes server.py's handler answered for this code.
def _preparation_state(request):
    try:
        return self_data_preparation_state()
    except SELF_DATA_FENCE.Held as error:
        return MODULE_ROUTES.Reply(409, {"error": str(error)})


def _abandon(request):
    try:
        return abandon_self_data_preparation(request.body)
    except SELF_DATA_FENCE.Held as error:
        return MODULE_ROUTES.Reply(409, {"error": str(error)})


def _held_for_review(answer):
    """A data-move step the fence holds: 409, and the person reviews again."""
    try:
        return answer()
    except SELF_DATA_FENCE.Held as error:
        return MODULE_ROUTES.Reply(409, {"error": str(error), "review_required": True})


def _move_preview(request):
    return _held_for_review(lambda: preview_self_data_move(request.body, request.user))


def _handoff_close(request):
    return _held_for_review(lambda: SELF_DATA_PREPARE.close_recovery(
        request.body, request.user, SELF.NS, NAMES.BRAND, OPS, homestead_running_on,
        start=request.path == "/api/self/data/handoff/close"))


def _archive(request):
    return _held_for_review(lambda: SELF_DATA_PREPARE.archive(
        request.body, request.user, SELF.NS, NAMES.BRAND, OPS, start=request.path == "/api/self/data/prepare/archive"))


def _prepare(request):
    return _held_for_review(lambda: self_data_preparation(request.body, request.user,
                                                          start=request.path == "/api/self/data/prepare"))


def _move(request):
    """The move started: its job, and the cookie that lets this browser read
    its progress from the move's own status server while Homestead is away."""
    try:
        result, token = start_self_data_move(request.body, request.user)
    except SELF_DATA_FENCE.Held as error:
        return MODULE_ROUTES.Reply(409, {"error": str(error), "review_required": True})
    return MODULE_ROUTES.Reply(202, result, cookies=(MODULE_ROUTES.Cookie(
        "homestead-data-move", token, path="/api/self/data/handoff/", max_age=86400),))


def _handoff(request):
    """A recorded move's progress, or the page that shows it (/view)."""
    found = re.fullmatch(r"/api/self/data/handoff/([a-f0-9]{24})(/view)?", request.path)
    if not found:   # do_GET answers PermissionError 403, in the guard's words for a non-route
        raise PermissionError("this route has no declared authorization policy")
    if found.group(2):
        return MODULE_ROUTES.Reply(200, SELF_DATA_WORKER.maintenance_page(found.group(1)), "text/html; charset=utf-8")
    status = self_data_handoff_status(found.group(1))
    if status is None:
        return MODULE_ROUTES.Reply(404, {"error": "No recorded data move with this identity"})
    return MODULE_ROUTES.Reply(503 if status["status"] == "unknown" else 200, status)


ROUTES = {
    ("GET", "/api/self/data/prepare"): ("viewer", _preparation_state),
    ("GET", "/api/self/data/handoff/*"): ("viewer", _handoff),
    ("POST", "/api/self/data/move"): ("admin", _move),
    ("POST", "/api/self/data/abandon"): ("admin", _abandon),
    ("POST", "/api/self/data/move/preview"): ("admin", _move_preview),
    ("POST", "/api/self/data/handoff/close"): ("admin", _handoff_close),
    ("POST", "/api/self/data/handoff/close/preview"): ("admin", _handoff_close),
    ("POST", "/api/self/data/prepare/archive"): ("admin", _archive),
    ("POST", "/api/self/data/prepare/archive/preview"): ("admin", _archive),
    ("POST", "/api/self/data/prepare"): ("admin", _prepare),
    ("POST", "/api/self/data/prepare/preview"): ("admin", _prepare),
}


def homestead_data_volume(dep=None):
    """Homestead's data claim, and whether pods on several nodes can mount it.

    Copies of Homestead on different nodes all mount this one claim, so it has
    to be ReadWriteMany on a class Longhorn serves through its share manager.
    A migratable class - Harvester's own, or a longhorn-r2 someone made
    migratable (Homestead makes its "Longhorn, two copies" class without
    migration, homestead_data_copies) - hands out a VM-disk volume that one
    node attaches, and a pod on a second node waits forever."""
    ns, name = SELF.NS, NAMES.BRAND
    dep = dep or kget(f"/apis/apps/v1/namespaces/{ns}/deployments/{name}")
    volumes = (dep["spec"]["template"]["spec"].get("volumes") or [])
    claim = next(((v.get("persistentVolumeClaim") or {}).get("claimName") for v in volumes
                  if v.get("name") == "data" and v.get("persistentVolumeClaim")), "")
    if not claim:
        return {"pvc": "", "shareable": False, "reason": "Homestead keeps no data claim", "candidates": []}
    pvc = kget(f"/api/v1/namespaces/{ns}/persistentvolumeclaims/{claim}")
    spec = pvc.get("spec") or {}
    klass = spec.get("storageClassName") or ""
    modes = spec.get("accessModes") or []
    rows = storage_classes()
    row = next((r for r in rows if r["name"] == klass), None)
    size = str(((pvc.get("status") or {}).get("capacity") or {}).get("storage")
               or ((spec.get("resources") or {}).get("requests") or {}).get("storage") or "2Gi")
    if "ReadWriteMany" not in modes:
        reason = f"{claim} is ReadWriteOnce: one node at a time can mount it"
    elif row and row.get("migratable"):
        reason = (f"{claim} is on {klass}, a migratable class: Longhorn gives it a VM-disk volume that "
                  "only one node can mount, so a copy on a second node would never start")
    else:
        reason = ""
    # Do not request RWX merely because a non-Longhorn class has no
    # migratable flag. k3s local-path (and many block CSI drivers) cannot
    # provision it. Unknown drivers get the conservative single-node mode.
    shared = shared_storage_classes(rows)
    # Every class it could move to, and whether copies on several nodes could
    # then share it: the move is not only for redundancy.
    classes = [{"name": r["name"], "shareable": r["name"] in shared} for r in rows
               if class_selectable(r) and r["name"] != klass]
    # Data volumes an earlier move left behind: kept until deleted, so they
    # are said out loud rather than found later as a mystery.
    try:
        kept = [p["metadata"]["name"] for p in kget(f"/api/v1/namespaces/{ns}/persistentvolumeclaims").get("items", [])
                if p["metadata"]["name"].startswith(f"{name}-data") and p["metadata"]["name"] != claim]
    except Exception:
        kept = []
    return {"pvc": claim, "storage_class": klass, "access_modes": modes, "size": size,
            "shareable": not reason, "reason": reason, "candidates": shared, "classes": classes, "kept": kept}


def _self_data_helper_image(read, ns):
    pod_name = _dns_name(SELF.POD, "Homestead pod")
    pod = read(f"/api/v1/namespaces/{ns}/pods/{pod_name}")
    containers = [c for c in pod.get("spec", {}).get("containers", []) if c.get("name") == NAMES.BRAND]
    statuses = [c for c in pod.get("status", {}).get("containerStatuses", []) if c.get("name") == NAMES.BRAND]
    if len(containers) != 1 or len(statuses) != 1 or not statuses[0].get("ready") or not statuses[0].get("state", {}).get("running"):
        raise SELF_DATA_FENCE.Held("The running Homestead image is not ready to be used for this move")
    digest_match = re.search(r"(?:^|@|://)(sha256:[a-f0-9]{64})$", statuses[0].get("imageID", ""))
    if not digest_match:
        raise SELF_DATA_FENCE.Held("The running Homestead image digest is unavailable; wait for it before reviewing")
    return pod, ROLLOUTS._immutable(containers[0]["image"], digest_match.group(1))


def _require_no_data_handoff(read, ns):
    try:
        read(f"/api/v1/namespaces/{ns}/configmaps/{NAMES.BRAND}-data-handoff")
    except urllib.error.HTTPError as error:
        if error.code == 404: return
        raise
    raise SELF_DATA_FENCE.Held("An earlier data move record exists; review it before starting another move")


def register(ops):
    """The self-data jobs (#622). The Kubernetes client is looked up when a
    job is asked about, as server.py did."""
    ops.RESOLVERS[SELF_DATA_PREPARE.KIND] = self_data_preparation_progress
    ops.CANCELLERS[SELF_DATA_PREPARE.KIND] = (SELF_DATA_PREPARE.cancel_plan, SELF_DATA_PREPARE.cancel_run)
    ops.RESOLVERS[SELF_DATA_EXECUTE.KIND] = lambda item: SELF_DATA_EXECUTE.resolve(item, KUBE.kget, directory=DATA_DIR)
    ops.RESOLVERS["self-data-move"] = _data_move_status
