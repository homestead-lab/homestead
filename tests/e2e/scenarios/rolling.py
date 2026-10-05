"""OS updates rolled across the hosts with a restart each: one host down at a
time, each back with a new boot and uncordoned, and apps and data intact."""
import threading
import time

from harness import log


def run(ctx):
    ctx.app("e2e-rolling")
    mark = ctx.mark("e2e-rolling")
    boots = {n.name: n.boot_id() for n in ctx.lab.nodes}
    for node in ctx.lab.nodes:          # as an update that needs a restart leaves it
        node.ssh("echo '*** System restart required ***' | sudo tee /var/run/reboot-required >/dev/null")

    # As a person would: every volume whole before restarting hosts - copies
    # Homestead added as the hosts joined may still be building.
    ctx.kube.wait("every volume healthy", lambda: all(
        (v.get("status") or {}).get("robustness") == "healthy" for v in ctx.kube.items("volumes.longhorn.io", "-n", "longhorn-system")
        if (v.get("status") or {}).get("state") == "attached"), timeout=900, every=15)
    ctx.api.post("/api/os-updates/settings", {"reboot": "when-needed", "manage": "ubuntu", "schedule": {"enabled": False}})
    started = ctx.api.post("/api/os-updates/start")
    job = (started.get("operation") or {}).get("id") or started.get("operation")
    assert job, f"no rollout job: {started}"

    # Never more than one host down at once.
    worst, stop = [0], threading.Event()
    def watch():
        while not stop.is_set():
            try:
                down = sum(1 for n in ctx.kube.items("nodes")
                           if not any(c["type"] == "Ready" and c["status"] == "True" for c in n["status"]["conditions"]))
                worst[0] = max(worst[0], down)
            except Exception:
                pass
            stop.wait(10)
    watcher = threading.Thread(target=watch, daemon=True)
    watcher.start()
    try:
        done = ctx.api.wait_job(job, timeout=2 * 3600)
    finally:
        stop.set()
    log.info(f"Rollout: {done['message']}; most hosts down at once: {worst[0]}")
    assert worst[0] <= 1, f"{worst[0]} hosts were down at once"
    rollout = ctx.api.get("/api/os-updates").get("rollout") or {}
    results = {r.get("node"): r.get("note") for r in rollout.get("results") or []}
    for node in ctx.lab.nodes:
        assert node.boot_id() != boots[node.name], f"{node.name} was not restarted: {results.get(node.name) or done['message']}"
    assert not any(n["spec"].get("unschedulable") for n in ctx.kube.items("nodes")), "a host is still cordoned"
    ctx.kube.deployment_ready("lab", "e2e-rolling")
    assert ctx.has_mark("e2e-rolling", mark), "the app lost data across the rollout"
