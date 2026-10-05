"""The whole cluster shut down from Homestead and brought back: apps and
Homestead stop in order, every volume detaches, the hosts power themselves
off, the runner powers them on again, and recovery restores scheduling and
the apps - with their data - by itself, with nobody at a console."""
import time

from harness import log


def run(ctx):
    ctx.app("e2e-shutdown")
    mark = ctx.mark("e2e-shutdown")
    boots = {n.name: n.boot_id() for n in ctx.lab.nodes}

    plan = ctx.api.get("/api/cluster/shutdown/plan")
    assert plan["ready"], f"shutdown review blocked: {plan['blockers']}"
    ctx.api.post("/api/cluster/shutdown", {"confirm": plan["confirm"], "review_token": plan["review_token"]})
    log.info("Shutdown started; waiting for every host to power itself off")
    for node in ctx.lab.nodes:
        node.wait_off(timeout=2400)

    time.sleep(30)
    for node in ctx.lab.nodes:          # a person at the power buttons
        node.power_on()
    for node in ctx.lab.nodes:
        node.wait_ssh()
        assert node.boot_id() != boots[node.name], f"{node.name} did not boot again"
    # Nobody at a console: Homestead brings the cluster back by itself once
    # every host is Ready - no uncordon, no waiting out the helpers' deadline.
    started = time.time()
    ctx.kube.nodes_ready(len(ctx.lab.nodes))
    state = ctx.api.get("/api/cluster/shutdown", wait=900)["state"]
    assert state and state["run"], "the shutdown's record did not survive"
    ctx.kube.wait("the shutdown recovered by itself", lambda: (ctx.api.get("/api/cluster/shutdown", wait=60)["state"] or {}).get("phase") == "released",
                  timeout=600, every=10)
    log.info(f"Recovered by itself {int(time.time() - started)}s after the hosts were powered on")
    nodes = ctx.kube.items("nodes")
    assert not any(n["spec"].get("unschedulable") for n in nodes), "a host is still cordoned after recovery"
    ctx.kube.wait("the recovery helper gone", lambda: not ctx.kube.items("deployments", "-n", "lab", "-l", "homestead.io/task=cluster-shutdown"),
                  timeout=120)
    ctx.kube.deployment_ready("lab", "e2e-shutdown")
    assert ctx.has_mark("e2e-shutdown", mark), "the app lost data across the shutdown"

