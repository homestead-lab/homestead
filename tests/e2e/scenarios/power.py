"""Host reboots and power-offs through Host actions, on several hosts and on
a single one - what moves, what waits for the host, the host coming back
with a new boot, scheduling allowed again, and every app's data intact."""
import time

from harness import log


def _plan(ctx, node, action):
    plan = ctx.api.get(f"/api/node/power/plan?node={node}&action={action}")
    assert plan.get("review_token"), f"no review for {node}: {plan}"
    return plan


def _send(ctx, plan, choices=None):
    body = {"node": plan["node"], "action": plan["action"], "confirm": plan["node"], "review_token": plan["review_token"],
            "force": False, "choices": choices or {}, "allow_cluster_outage": bool(plan.get("planned_outage")),
            "allow_stranded": True, "allow_data_risk": True}
    answer = ctx.api.post("/api/node/power", body)
    return answer["operation"]["id"]


def _volumes_available(ctx, apps):
    for name in apps:
        volume = ctx.kube.volume("lab", f"{name}-data")
        robustness = volume["status"].get("robustness")
        assert robustness in ("healthy", "degraded"), f"{name}'s volume is {robustness} after the host came back"


def multi(ctx):
    """Three hosts: reboot node-2 with one app that moves and one pinned to it
    that stops and waits, then check both, the host and the data."""
    ctx.app("e2e-moves", node="node-2")
    ctx.app("e2e-waits", node="node-2", pinned=True)
    marks = {name: ctx.mark(name) for name in ("e2e-moves", "e2e-waits")}
    node = ctx.node("node-2")
    boot = node.boot_id()

    plan = _plan(ctx, "node-2", "reboot")
    assert plan["ready"], f"review blocked: {plan['blockers']}"
    hold = {h["id"]: h for h in plan.get("hold") or []}
    assert hold["Deployment/lab/e2e-waits"]["options"] == ["wait"], hold.get("Deployment/lab/e2e-waits")
    assert "move" in hold["Deployment/lab/e2e-moves"]["options"], hold.get("Deployment/lab/e2e-moves")
    job = _send(ctx, plan, {"Deployment/lab/e2e-moves": "move", "Deployment/lab/e2e-waits": "wait"})
    done = ctx.api.wait_job(job, timeout=3600)
    log.info(f"Reboot job: {done['message']}")

    assert node.boot_id() != boot, "node-2 did not boot again"
    k8s = ctx.kube.get("node", "node-2")
    assert not k8s["spec"].get("unschedulable"), "node-2 is still cordoned after coming back"
    waits = ctx.kube.deployment_ready("lab", "e2e-waits")
    assert waits["spec"]["replicas"] == 1, "the app that waited was not started again"
    assert ctx.app_node("e2e-waits") == "node-2"
    for name, mark in marks.items():
        assert ctx.has_mark(name, mark), f"{name} lost data across the reboot"
    _volumes_available(ctx, marks)


def single(ctx):
    """One host: Homestead stops the apps, hands the last step to a helper so
    its own volume detaches, reboots, and brings everything back."""
    ctx.app("e2e-single")
    mark = ctx.mark("e2e-single")
    node = ctx.lab.nodes[0]
    boot = node.boot_id()

    plan = _plan(ctx, node.name, "reboot")
    assert plan["planned_outage"], "a single host's reboot is a planned outage"
    assert plan["ready"], f"review blocked: {plan['blockers']}"
    job = _send(ctx, plan)
    done = ctx.api.wait_job(job, timeout=3600)
    log.info(f"Reboot job: {done['message']}")
    assert node.boot_id() != boot, "the host did not boot again"
    assert ctx.has_mark("e2e-single", mark), "the app lost data across the reboot"
    # Homestead's own volume stopped cleanly: no I/O errors under a mounted filesystem.
    errors = node.ssh("sudo journalctl -k -b -1 --no-pager | grep -c -E 'Buffer I/O error|JBD2: I/O error' || true", quiet=True).strip()
    assert errors in ("0", ""), f"{errors} storage I/O errors while the host went down"
    for claim in [c["metadata"]["name"] for c in ctx.kube.items("pvc", "-n", "lab") if c["metadata"]["name"].startswith("homestead-data")]:
        robustness = ctx.kube.volume("lab", claim)["status"].get("robustness")
        assert robustness != "faulted", f"Homestead's data volume came back {robustness}"


def single_poweroff(ctx):
    """One host powered off: the runner powers it on again, as a person would,
    and the job finishes with everything back."""
    ctx.app("e2e-off")
    mark = ctx.mark("e2e-off")
    node = ctx.lab.nodes[0]
    plan = _plan(ctx, node.name, "poweroff")
    job = _send(ctx, plan)
    node.wait_off(timeout=1200)
    time.sleep(20)
    node.power_on()
    node.wait_ssh()
    done = ctx.api.wait_job(job, timeout=3600)
    assert "powered off and has started again" in done["message"], done["message"]
    assert ctx.has_mark("e2e-off", mark), "the app lost data across the power-off"
