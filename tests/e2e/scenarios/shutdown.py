"""The whole cluster shut down from Homestead and brought back: apps and
Homestead stop in order, every volume detaches, the hosts power themselves
off, the runner powers them on again, and recovery restores scheduling and
the apps - with their data."""
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

    state = ctx.api.get("/api/cluster/shutdown", wait=1800)["state"]
    assert state and state["run"], "the shutdown's record did not survive"
    # Recovery is offered once the helpers' deadline has passed.
    deadline = state["deadline"] + 200
    while time.time() < deadline:
        log.info(f"Waiting {int(deadline - time.time())}s for the shutdown helpers' deadline before recovering")
        time.sleep(min(60, max(1, deadline - time.time())))
    ctx.api.post("/api/cluster/shutdown/recover", {"run": state["run"]}, wait=600)
    nodes = ctx.kube.items("nodes")
    assert not any(n["spec"].get("unschedulable") for n in nodes), "a host is still cordoned after recovery"
    ctx.kube.deployment_ready("lab", "e2e-shutdown")
    assert ctx.has_mark("e2e-shutdown", mark), "the app lost data across the shutdown"
    after = ctx.api.get("/api/cluster/shutdown")["state"]
    assert after["phase"] == "released", f"shutdown is {after['phase']} after recovery"
