"""An app moved from one cluster to another, as Linked clusters does it: the
destination links the source, sets up backup storage on it, plans and starts
the move, and finishes it - the app runs on the destination with its data,
and is gone from the source."""
import time

from harness import log
from harness.vms import NET

SOURCE = "source"


def run(ctx):
    target, source = ctx, ctx.others[0]        # two single-host clusters
    source.app("e2e-migrate")
    mark = source.mark("e2e-migrate")

    target.api.post("/api/move/clusters/add", {"name": SOURCE, "url": source.homestead_urls()[0],
                                                "user": source.api.username, "password": source.api.password})
    target.api.post("/api/move/clusters/check", {"name": SOURCE})
    target.api.post("/api/move/clusters/storage", {"name": SOURCE, "size_gb": 20, "lb_ip": f"{NET}.110", "vip_mode": "", "port": 0},
                    wait=600)

    def ready():
        answer = target.api.post("/api/move/clusters/readiness", {"name": SOURCE})
        return answer.get("ready") and answer
    target.kube.wait("the source's backup storage ready for moves", ready, timeout=1800, every=20)

    body = {"cluster": SOURCE, "kind": "container", "name": "e2e-migrate", "namespace": "lab", "address_mode": "shared"}
    plan = target.api.post("/api/move/plan", body)
    log.info(f"Move plan: {str(plan)[:300]}")
    longhorn = plan.get("storage_classes") or []
    # The class offered first is Longhorn's: a moved volume is restored from
    # a Longhorn backup. Checked at the end, so the move itself is still tested.
    offered = plan.get("storage_class")
    body["storage_class"] = offered if offered in longhorn else (longhorn or [""])[0]
    if body["storage_class"] != offered:
        plan = target.api.post("/api/move/plan", body)
    assert not plan.get("blockers"), f"the move is blocked: {plan['blockers']}"
    target.api.post("/api/move/start", body)

    deadline, move = time.time() + 3600, None
    while time.time() < deadline:
        move = next((m for m in target.api.get("/api/move/moves") if m.get("name") == "e2e-migrate"), None)
        if move:
            log.info(f"  move {move.get('status')} {move.get('phase')} {move.get('progress')}% · {str(move.get('message', ''))[:140]}")
            if move.get("status") in ("succeeded", "failed", "cancelled"):
                break
        time.sleep(20)
    assert move and move["status"] == "succeeded", f"the move ended {move and move.get('status')}: {move and move.get('message')}"

    target.kube.deployment_ready("lab", "e2e-migrate")
    assert target.has_mark("e2e-migrate", mark), "the app arrived without its data"
    target.api.post("/api/move/moves/finish", {"id": move["id"], "volumes": True})
    assert offered in longhorn, f"the move offered {offered!r} for the volume, not a Longhorn class of {longhorn}"
    source.kube.wait("the app gone from the source", lambda: not source.kube.items("deployments", "-n", "lab", "-l", "app=e2e-migrate"),
                     timeout=600)
