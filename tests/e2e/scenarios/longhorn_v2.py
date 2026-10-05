"""Longhorn's V2 data engine (SPDK) set up the way Homestead offers it: each
host prepared by Homestead's own task (nvme-cli, kernel modules, hugepages
kept across reboots), V2 enabled, a blank disk on each host given to it, and
a V2 storage class - then an app on a V2 volume keeps its data when its host
reboots and it starts on the other.

The hosts reserve their hugepages at boot, before Kubernetes starts, as a
host already prepared does: Kubernetes counts hugepages only when it starts,
so reserving them later needs a reboot of every host first."""
import json
import secrets
import time
from pathlib import Path

from harness import log
from scenarios.power import _plan, _send

CLASS = "e2e-v2"


def _v2_plan(ctx):
    return ctx.api.get("/api/longhorn/v2/plan")


def run(ctx):
    hosts = {n.name for n in ctx.lab.nodes}
    # Longhorn lists a host once its manager runs there: wait for all of them.
    plan = ctx.kube.wait("every host in the V2 plan", lambda: (lambda p: {r["node"] for r in p["nodes"]} >= hosts and p)(_v2_plan(ctx)),
                         timeout=600, every=10)
    assert not plan["enabled"], "V2 is on before Homestead turned it on"
    deadline, prepared = time.time() + 900, set()
    while True:
        plan = _v2_plan(ctx)
        if plan["can_enable"]:
            break
        waiting = [r for r in plan["nodes"] if r["problems"]]
        ready = [r for r in waiting if r["can_prepare"] and r["node"] not in prepared]
        assert time.time() < deadline, f"V2 cannot be enabled: {plan['blockers']}"
        for row in ready:
            started = ctx.api.post("/api/longhorn/v2/prepare", {"node": row["node"], "review_token": row["review_token"],
                                                                "request_id": secrets.token_hex(12), "confirm": True})
            ctx.api.wait_job(started["operation"]["id"], timeout=900)
            prepared.add(row["node"])
            log.info(f"{row['node']}: prepared for V2 by Homestead")
        if not ready:
            time.sleep(10)
    ctx.api.post("/api/longhorn/v2/enable", {"confirm": True, "review_token": plan["review_token"]})
    # V2's instance managers can start and fail within seconds, and Longhorn
    # replaces them: their output is kept as it happens, for the diagnostics.
    seen = Path(ctx.artifacts) / "v2-instance-managers.txt"

    def engine_ready():
        for pod in ctx.kube.items("pods", "-n", "longhorn-system", "-l", "longhorn.io/data-engine=v2"):
            name = pod["metadata"]["name"]
            states = [{"name": c.get("name"), "state": c.get("state"), "last": c.get("lastState")}
                      for c in (pod.get("status") or {}).get("containerStatuses") or []]
            logs = ctx.kube.run("logs", "-n", "longhorn-system", name, "--all-containers", "--tail=60", check=False, timeout=20)
            with open(seen, "a", encoding="utf-8") as out:
                out.write(f"===== {time.strftime('%H:%M:%S')} {name} on {pod['spec'].get('nodeName')} "
                          f"{(pod.get('status') or {}).get('phase')}\n{json.dumps(states)}\n{logs}\n")
        return _v2_plan(ctx).get("engine_ready")
    ctx.kube.wait("V2 instance managers running on every host", engine_ready, timeout=600, every=10)
    log.info("V2 data engine on")

    for node in ctx.lab.nodes:
        device = node.ssh("readlink -f /dev/disk/by-id/virtio-e2e-data", quiet=True).strip()
        assert device.startswith("/dev/vd"), f"{node.name} has no data disk: {device!r}"
        ctx.api.post("/api/disks/add", {"node": node.name, "engine": "v2", "path": device})
        log.info(f"{node.name}: {device} given to Longhorn V2")

    def disks_ready():
        ready = 0
        for lh in ctx.kube.items("nodes.longhorn.io", "-n", "longhorn-system"):
            for name, disk in ((lh.get("status") or {}).get("diskStatus") or {}).items():
                spec = ((lh.get("spec") or {}).get("disks") or {}).get(name) or {}
                if spec.get("diskType") == "block" and any(c.get("type") == "Schedulable" and c.get("status") == "True"
                                                           for c in disk.get("conditions") or []):
                    ready += 1
        return ready >= len(ctx.lab.nodes)
    ctx.kube.wait("the V2 disks schedulable", disks_ready, timeout=600, every=10)

    ctx.api.post("/api/storage/classes", {"name": CLASS, "engine": "v2", "replicas": 2, "reclaim_policy": "Delete"})
    first = ctx.lab.nodes[0].name
    ctx.app("e2e-v2", node=first, storage_class=CLASS)
    mark = ctx.mark("e2e-v2")
    volume = ctx.kube.volume("lab", "e2e-v2-data")
    assert volume["spec"].get("dataEngine") == "v2", f"the volume is on {volume['spec'].get('dataEngine')}"
    here = ctx.app_node("e2e-v2")
    log.info(f"e2e-v2 on a V2 volume, running on {here}")

    # Its host reboots; the app moves to the other host with its V2 volume.
    node = ctx.node(here)
    boot = node.boot_id()
    power = _plan(ctx, here, "reboot")
    assert power["ready"], f"reboot review blocked: {power['blockers']}"
    job = _send(ctx, power, {"Deployment/lab/e2e-v2": "move"})
    started = time.time()
    ctx.api.wait_job(job, timeout=1800)
    assert node.boot_id() != boot, f"{here} did not reboot"
    ctx.kube.deployment_ready("lab", "e2e-v2")
    assert ctx.has_mark("e2e-v2", mark), "the app lost its data on V2 across the reboot"
    log.info(f"e2e-v2 back on {ctx.app_node('e2e-v2')} with its data, {int(time.time() - started)}s after the reboot began")
    robustness = ctx.kube.volume("lab", "e2e-v2-data")["status"].get("robustness")
    assert robustness in ("healthy", "degraded"), f"the V2 volume is {robustness}"
