"""Homestead's own data moved to a new volume, twice, through its API - as
Settings › Homestead data does - on real Longhorn across hosts: the copy made
on one host while Homestead runs on another, so its volume really detaches
and attaches elsewhere, which the PR check's one-node local-path cluster
cannot show. The account, a job record and the old volumes survive each
move, and the move helpers are gone afterwards."""
import json
import secrets
import time

from harness import log
from harness.api import HomesteadError

TARGET = "e2e-self-target"


def _until(what, seconds, attempt, retry=()):
    """attempt() until truthy, through Homestead restarting mid-move, and
    through refusals `retry` names as temporary."""
    deadline, last = time.time() + seconds, ""
    while time.time() < deadline:
        try:
            result = attempt()
            if result:
                return result
        except TimeoutError as error:           # Homestead away: the move working
            last = str(error)[:200]
        except HomesteadError as error:
            if not any(phrase in str(error) for phrase in retry):
                raise
            last = str(error)[:200]
        time.sleep(3)
    raise AssertionError(f"{what}: not done in {seconds}s; last: {last or 'no answer'}")


def _move(ctx, storage_class, node, copy_node):
    api = ctx.api
    operation = secrets.token_hex(12)
    cfg = {"operation": operation, "storage_class": storage_class, "node": node}
    preview = _until("preparation review", 300, lambda: api.post("/api/self/data/prepare/preview", cfg, wait=60),
                     retry=("earlier data move record exists", "only reports progress", "Wait for every Homestead replica"))
    prepared = api.post("/api/self/data/prepare", {**cfg, "capacity_token": preview["capacity_token"], "confirm_capacity": True})
    log.info(f"Preparing {prepared['destination']} on {storage_class}, {node}")

    def prepared_now():
        api.get("/api/operations", wait=30)             # advances the job
        row = next(p for p in api.get("/api/self/data/prepare", wait=30)["preparations"] if p["operation"] == operation)
        if row["status"] in ("failed", "cancelled"):
            raise AssertionError(f"preparation {row['status']}: {row['message']}")
        return row["prepared"]
    _until("preparation", 900, prepared_now)

    move = {"operation": operation, "destination": prepared["destination"], "worker_node": node, "copy_node": copy_node}
    preview = _until("move review", 300, lambda: api.post("/api/self/data/move/preview", move, wait=60),
                     retry=("report current data-move support",))
    api.post("/api/self/data/move", {**move, "capacity_token": preview["capacity_token"], "confirm_capacity": True, "confirm_move": True})
    log.info(f"Moving Homestead's data to {prepared['destination']} (copy on {copy_node}, Homestead on {node})")

    def moved():
        view = api.request("GET", f"/api/self/data/handoff/{operation}", ok=(200, 202), wait=15, timeout=20)
        log.info(f"  move {view.get('phase')} {view['status']}: {str(view.get('message', ''))[:140]}")
        if view["status"] in ("held", "failed", "cancelled"):
            raise AssertionError(f"move {view['status']} at {view.get('phase')}: {view['message']}")
        return view["status"] == "done"
    _until("move", 1800, moved, retry=(": HTTP 404",))
    _until("new source reported", 300, lambda: api.get("/api/self/data/prepare", wait=30)["source"] == prepared["destination"],
           retry=(": HTTP 404",))
    _until("handoff finished", 300, lambda: not api.get("/healthz", wait=30).get("data_handoff"))
    return prepared["destination"]


def run(ctx):
    nodes = [n.name for n in ctx.lab.nodes]
    assert len(nodes) >= 3, "the moves go across three hosts"
    ctx.kube.apply(json.dumps({"apiVersion": "storage.k8s.io/v1", "kind": "StorageClass", "metadata": {"name": TARGET},
                               "provisioner": "driver.longhorn.io", "allowVolumeExpansion": True, "reclaimPolicy": "Retain",
                               "volumeBindingMode": "Immediate", "parameters": {"numberOfReplicas": "2", "staleReplicaTimeout": "30"}}))
    here = next(p["spec"]["nodeName"] for p in ctx.kube.items("pods", "-n", "lab", "-l", "app=homestead")
                if p["status"].get("phase") == "Running")
    away = [n for n in nodes if n != here]
    claims_before = {c["metadata"]["name"] for c in ctx.kube.items("pvc", "-n", "lab")}
    jobs_before = len(ctx.api.get("/api/operations") or [])

    # A move's handoff is followed on the address that started it: its cookie
    # belongs there, as it does in the browser that started the move.
    urls, ctx.api.urls = ctx.api.urls, ctx.api.urls[:1]
    try:
        first = _move(ctx, TARGET, away[0], away[1])
        second = _move(ctx, "longhorn", away[1], away[0])
    finally:
        ctx.api.urls = urls
    ctx.kube.deployment_ready("lab", "homestead")
    log.info(f"Homestead's data moved to {first}, then {second}")

    # Signed in with the same account, its jobs still listed.
    ctx.api.sign_in()
    assert len(ctx.api.get("/api/operations") or []) >= jobs_before, "the job history did not survive the moves"
    claims = {c["metadata"]["name"]: c for c in ctx.kube.items("pvc", "-n", "lab")}
    assert claims_before <= set(claims), f"an old data volume was removed: {sorted(claims_before - set(claims))}"
    assert {first, second} <= set(claims), f"the moved-to volumes are missing: {sorted(claims)}"
    assert all(claims[c]["status"]["phase"] == "Bound" for c in claims_before | {first, second})
    used = {v["persistentVolumeClaim"]["claimName"] for p in ctx.kube.items("pods", "-n", "lab", "-l", "app=homestead")
            for v in p["spec"].get("volumes") or [] if v.get("persistentVolumeClaim")}
    assert second in used, f"Homestead runs on {sorted(used)}, not the volume it moved to"
    ctx.kube.wait("the move helpers retired", lambda: not ctx.kube.items("pods", "-n", "lab", "-l", "homestead.io/self-data-handoff"),
                  timeout=120)
    # The one in use is whole; the first, kept after the second move, rests detached.
    robustness = ctx.kube.volume("lab", second)["status"].get("robustness")
    assert robustness in ("healthy", "degraded"), f"{second} is {robustness}"
    assert ctx.kube.volume("lab", first)["status"].get("state") == "detached", f"{first} is still attached"
