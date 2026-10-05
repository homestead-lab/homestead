"""Host reboots and power-offs through Host actions, on several hosts and on
a single one - what moves, what waits for the host, the host coming back
with a new boot, scheduling allowed again, and every app's data intact."""
import time

from harness import log
from harness.api import HomesteadError

BUSY = "host command may still be changing this host"


def _plan(ctx, node, action, wait=180):
    deadline = time.time() + wait
    while True:
        plan = ctx.api.get(f"/api/node/power/plan?node={node}&action={action}")
        assert plan.get("review_token"), f"no review for {node}: {plan}"
        busy = [b for b in plan.get("blockers") or [] if BUSY in str(b)]
        if not busy or time.time() > deadline:
            return plan
        log.info(f"{node}: a host command is still running; reviewing again")
        time.sleep(10)


def _send(ctx, plan, choices=None, wait=180):
    """Send a reviewed power action. Homestead's own short host commands
    (reading a host's OS, holding its updates) run for a moment after
    install, and the review rightly waits for them: so does this, reviewing
    again until they end."""
    deadline = time.time() + wait
    while True:
        body = {"node": plan["node"], "action": plan["action"], "confirm": plan["node"], "review_token": plan["review_token"],
                "force": False, "choices": choices or {}, "allow_cluster_outage": bool(plan.get("planned_outage")),
                "allow_stranded": True, "allow_data_risk": True}
        try:
            answer = ctx.api.post("/api/node/power", body)
            return answer["operation"]["id"]
        except HomesteadError as error:
            if error.status != 409 or BUSY not in str(error) or time.time() > deadline:
                raise
            log.info(f"{plan['node']}: a host command is still running; reviewing again")
            time.sleep(10)
            plan = _plan(ctx, plan["node"], plan["action"])


def _volumes_available(ctx, apps, wait=120):
    """Each app's volume healthy or degraded. Longhorn reports "unknown" for a
    while after the host it is attached on comes back, even once the app
    reads its data again - so it is given a couple of minutes to settle."""
    for name in apps:
        deadline = time.time() + wait
        while True:
            robustness = ctx.kube.volume("lab", f"{name}-data")["status"].get("robustness")
            if robustness in ("healthy", "degraded"):
                break
            assert time.time() < deadline, f"{name}'s volume is {robustness} {wait}s after the host came back"
            time.sleep(5)


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
    assert "wait" in hold["Deployment/lab/e2e-waits"]["options"], hold.get("Deployment/lab/e2e-waits")
    assert "move" in hold["Deployment/lab/e2e-moves"]["options"], hold.get("Deployment/lab/e2e-moves")
    job = _send(ctx, plan, {"Deployment/lab/e2e-moves": "move", "Deployment/lab/e2e-waits": "wait"})
    done = ctx.api.wait_job(job, timeout=3600)
    log.info(f"Reboot job: {done['message']}")

    assert node.boot_id() != boot, "node-2 did not boot again"
    for name in marks:              # started again, now running
        ctx.kube.deployment_ready("lab", name)
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
    ctx.kube.deployment_ready("lab", "e2e-single")
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
    ctx.kube.deployment_ready("lab", "e2e-off")
    assert ctx.has_mark("e2e-off", mark), "the app lost data across the power-off"
