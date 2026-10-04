"""A host that fails with no warning - the runner pulls its plug - and a
container that crashes: what was set to move comes back elsewhere with its
data, what was set to wait comes back on its host when that returns,
Homestead says the host is down meanwhile, and nothing is left faulted."""
import time

from harness import log


def _workload(ctx, name):
    rows = ctx.api.get("/api/workloads")
    return next((w for w in rows if w.get("ns") == "lab" and w.get("name") == name), {})


def container_restart(ctx):
    """A container that crashes is restarted in place, with its data."""
    ctx.app("e2e-crash")
    mark = ctx.mark("e2e-crash")
    # A container's PID 1 ignores signals it has no handler for, from inside;
    # its host kills it as a crash would - found by the pod's UID in its cgroup.
    pod = next(p for p in ctx.kube.items("pods", "-n", "lab", "-l", "app=e2e-crash") if p["status"].get("phase") == "Running")
    uid = pod["metadata"]["uid"]
    host = ctx.node(pod["spec"]["nodeName"])
    killed = host.ssh(f"for p in $(pgrep -f /data/starts); do grep -qE '{uid}|{uid.replace('-', '_')}' /proc/$p/cgroup 2>/dev/null "
                      f"&& sudo kill -9 $p && echo $p; done", check=False).split()
    assert killed, f"no process of e2e-crash found on {host.name}"
    def restarted():
        pods = ctx.kube.items("pods", "-n", "lab", "-l", "app=e2e-crash")
        status = (pods[0]["status"].get("containerStatuses") or [{}])[0] if pods else {}
        return status.get("restartCount", 0) >= 1 and status.get("ready")
    ctx.kube.wait("e2e-crash restarted and ready", restarted, timeout=300)
    assert ctx.has_mark("e2e-crash", mark), "the container lost its data when it restarted"
    restarts = _workload(ctx, "e2e-crash").get("restarts")
    assert restarts is None or int(restarts) >= 1, f"Homestead reports {restarts} restarts"


def host_outage(ctx):
    """node-2 loses power: an app set to move starts elsewhere; one set to
    wait does not, and runs there again once the host is back."""
    # Longhorn lets go of a dead host's volumes, so a container can move with its own.
    ctx.api.post("/api/longhorn/settings", {"node_down": "delete-both-statefulset-and-deployment-pod"})
    ctx.app("e2e-mover", node="node-2")
    ctx.app("e2e-waiter", node="node-2", pinned=True)
    ctx.api.post("/api/workloads/failover", {"items": [{"ns": "lab", "name": "e2e-mover", "mode": "move"},
                                                        {"ns": "lab", "name": "e2e-waiter", "mode": "wait"}]})
    ctx.kube.deployment_ready("lab", "e2e-mover")
    ctx.kube.deployment_ready("lab", "e2e-waiter")
    marks = {name: ctx.mark(name) for name in ("e2e-mover", "e2e-waiter")}
    node = ctx.node("node-2")
    assert ctx.app_node("e2e-mover") == "node-2", "the app to move did not start on node-2"

    started = time.time()
    node.pull_plug()
    ctx.kube.wait("Homestead to report node-2 down", lambda: any(
        n.get("name") == "node-2" and str(n.get("status", "")).lower() != "ready" for n in ctx.api.get("/api/nodes", wait=120)),
        timeout=300)
    ctx.kube.wait("e2e-mover running on another host", lambda: (
        ctx.app_node("e2e-mover") not in (None, "node-2") and ctx.kube.deployment_ready("lab", "e2e-mover", timeout=10)), timeout=900)
    log.info(f"e2e-mover back on {ctx.app_node('e2e-mover')} {int(time.time() - started)}s after the host failed")
    assert ctx.has_mark("e2e-mover", marks["e2e-mover"]), "the app that moved lost its data"
    assert ctx.app_node("e2e-waiter") in (None, "node-2"), "the app set to wait ran somewhere else"

    node.power_on()
    node.wait_ssh()
    ctx.kube.nodes_ready(len(ctx.lab.nodes))
    ctx.kube.wait("e2e-waiter running on node-2 again", lambda: (
        ctx.app_node("e2e-waiter") == "node-2" and ctx.kube.deployment_ready("lab", "e2e-waiter", timeout=10)), timeout=900)
    assert ctx.has_mark("e2e-waiter", marks["e2e-waiter"]), "the app that waited lost its data"
    for name in marks:
        ctx.kube.wait(f"{name}'s volume available", lambda name=name: ctx.kube.volume("lab", f"{name}-data")["status"].get(
            "robustness") in ("healthy", "degraded"), timeout=900)
