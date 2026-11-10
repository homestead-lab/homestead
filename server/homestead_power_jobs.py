"""Host power jobs: a reviewed reboot or shutdown, run as a job.

Host actions review a host's power change (homestead_power.plan); once the
review is accepted the job cordons and drains the host, stops what waits for
it, rechecks and sends the command, recording each phase so another replica
can carry it on if the drain evicts this one. An OS update of the hosts
(homestead_os_rollout) restarts each host the same way, with nobody there to
accept a warning.

server.py binds the cluster reads and writes and Homestead's own pod; its
background loop asks the job tray, whose resolver resumes a job here.
"""
import os
import threading
import time
import urllib.error
import urllib.parse

import homestead_history as HISTORY
import homestead_drain_blockers as DRAIN_BLOCKERS
import homestead_leader as LEADER
import homestead_node_actions as NODE_ACTIONS
import homestead_names as NAMES
import homestead_operations as OPS
import homestead_power as POWER
import homestead_power_hold as HOLD
import homestead_routes as ROUTER
import homestead_self as SELF

kget = ksend = None
_self_data_helper_image = homestead_running_on = None
# The Homestead replica a host power job is running in. A drain can evict
# that very replica; the job then says which pod to look for, and the leader
# carries it on from another replica (resume_power_job).
POD_NAME = os.environ.get("HOSTNAME", "")
_POWER_RESUMING = set()
_POWER_RESUME_LOCK = threading.Lock()


def bind(_kget, _ksend, helper_image, running_on, pod_name):
    """The cluster, the image and data claim Homestead runs with, and its pod."""
    global kget, ksend, _self_data_helper_image, homestead_running_on, POD_NAME
    kget, ksend, POD_NAME = _kget, _ksend, pod_name
    _self_data_helper_image, homestead_running_on = helper_image, running_on


class PowerNotSent(Exception):
    """A reviewed power request that stopped; the job it made says how far it got."""

    def __init__(self, message, operation):
        super().__init__(message)
        self.operation = operation


def active_power_job(node):
    """A reboot or shutdown job for this host that has not finished."""
    return next((item for item in OPS.snapshot()
                 if item.get("kind") == "node-power" and item.get("resource", {}).get("name") == node
                 and item.get("status") not in ("succeeded", "failed", "cancelled")), None)


def power_plan_with_job(power_plan):
    """A host whose power job is still going cannot be reviewed again: a
    second review during its drain is how the same drain started twice."""
    running = active_power_job(power_plan.get("node"))
    if running:
        power_plan = {**power_plan, "ready": False, "operation": running,
                      "blockers": ["A power job for this host is still running. Follow it in its progress view."]
                                  + list(power_plan.get("blockers") or [])}
    return power_plan


def run_power_job(operation_id, power_plan, force=False, resumed=False):
    """Cordon, drain, recheck and send a reviewed reboot or shutdown, recording
    each phase on the job. Cordon and drain may be repeated safely; the send is
    guarded by the job's own receipts.

    While it runs the job is marked as running here, however it was started
    (in the background, or in the leader's own OS-update tick), so the leader
    never resumes it a second time in this process."""
    with _POWER_RESUME_LOCK:
        mine = operation_id not in _POWER_RESUMING      # a background runner's mark stays its own to free
        _POWER_RESUMING.add(operation_id)
    try:
        return _run_power_job(operation_id, power_plan, force, resumed)
    finally:
        if mine:
            with _POWER_RESUME_LOCK:
                _POWER_RESUMING.discard(operation_id)


def _run_power_job(operation_id, power_plan, force=False, resumed=False):
    node, action = power_plan["node"], power_plan["action"]
    if action == "maintenance":
        return _run_maintenance(operation_id, power_plan, resumed)
    planned_outage = bool(power_plan.get("planned_outage")) and not force
    phase_state = {"phase": "reviewed"}

    def power_progress(phase, percent, message, **details):
        details.update(_cordon_found(operation_id, power_plan, phase))
        updated = OPS.record_phase(operation_id, phase, percent, message, owner=POD_NAME or None, **details)
        phase_state["phase"] = phase
        if phase == "sending":
            # From here the host's downtime is maintenance this job caused (#535).
            # Bookkeeping: a history that can't be written never stops the job.
            try:
                HISTORY.maintenance_open(node, power_plan.get("reason") or
                                         {"reboot": "host reboot", "poweroff": "host shutdown"}[action], operation_id)
            except Exception:
                pass
        return updated

    def hold():
        return hold_for_power(operation_id, power_plan, power_progress)

    def handoff(node, action, steps, rep, report):
        return send_handoff(operation_id, power_plan, steps, rep, report)
    try:
        result = NODE_ACTIONS.node_power(node, action, True,
                               before_send=(lambda: POWER.recheck_planned_outage(power_plan)) if planned_outage else
                               (lambda: POWER.recheck_forced(power_plan)) if force
                               else (lambda: POWER.recheck_after_drain(power_plan)),
                               reviewed_pods=power_plan["drain_pods"], progress=power_progress, force=force, planned_outage=planned_outage,
                               resumed=resumed, hold=None if force else hold,
                               send=handoff if planned_outage else None)
        result["operation"] = {"id": operation_id}
        return result
    except OPS.Superseded:
        raise  # another replica owns the job; nothing to record or send here
    except Exception as e:
        uncertain = phase_state["phase"] in ("sending", "observing")
        if uncertain:
            message = "Whether the restart or shutdown was sent is uncertain. Check the existing job and helper before trying again."
            power_progress("observing", 20, message)
        else:
            # Power was not sent: the cordon this job put on comes off first,
            # so what was stopped to wait for the host can start there again
            # now. A cordon from before the job stays.
            message, cordon = _after_stop(operation_id, power_plan, "Nothing was sent", str(e))
            message, restored = _start_what_was_held(operation_id, message)
            power_progress("failed", 10, message, failed_phase=phase_state["phase"], **cordon, **restored)
        raise PowerNotSent(message, {"id": operation_id}) from e


def _job_ref(operation_id):
    return next(((i.get("ref") or {}) for i in OPS.stored() if i.get("id") == operation_id), {})


def _cordon_found(operation_id, power_plan, phase):
    """At the job's cordon step, just before it cordons: whether the host has
    no cordon yet, so that the cordon there after it is this job's own (#1289).
    Recorded once: a resumed job finds its own cordon from its first run and
    keeps that first answer. A host that cannot be read, or is not the reviewed
    host, counts as cordoned already, so its cordon is never taken off."""
    if phase != "cordoning":
        return {}
    try:
        if "own_cordon" in _job_ref(operation_id):
            return {}
        own = False
        if power_plan.get("node_uid"):
            found = kget(f"/api/v1/nodes/{urllib.parse.quote(power_plan['node'], safe='')}")
            own = ((found.get("metadata") or {}).get("uid") == power_plan["node_uid"]
                   and not (found.get("spec") or {}).get("unschedulable"))
    except Exception:
        own = False
    return {"own_cordon": own}


# The drain's and the recheck's own words for the cordon, which the job says
# itself once it knows what became of it.
_CORDON_WORDS = (("The host stays cordoned, and nothing was sent: ", ""), ("The host stays cordoned. ", ""),
                 ("The host stays cordoned; ", ""), (" Nothing was sent.", ""), (", so nothing was sent", ""))


def _after_stop(operation_id, power_plan, opening, cause):
    """A job that stopped before power (or before maintenance opened): the
    cordon it put on comes off; a cordon from before it stays. Returns the
    job's message, saying what is true of the cordon now, and what its record
    gets. Only this job's cordon, on the reviewed host (by uid): a job from
    before own_cordon was recorded, or stopped before its cordon step, leaves
    the host as it is and says so as before."""
    ref = _job_ref(operation_id)
    if "own_cordon" not in ref or ref.get("planned_outage") or ref.get("forced"):
        return f"{opening}. Check whether the host is still cordoned: {cause}", {}
    for old, new in _CORDON_WORDS:
        cause = cause.replace(old, new)
    cause = (cause[:1].upper() + cause[1:]).rstrip(" .")    # what follows starts with ". "
    if ref.get("cordoned_before") is not False or not ref["own_cordon"]:
        return f"{opening}. The host stays cordoned, as it was before this job. {cause}", {}
    try:
        found = kget(f"/api/v1/nodes/{urllib.parse.quote(power_plan['node'], safe='')}")
    except Exception as error:
        return f"{opening}. The host stays cordoned, because it could not be read: {error}. {cause}", {}
    if (found.get("metadata") or {}).get("uid") != power_plan.get("node_uid"):
        return f"{opening}. The host was replaced during this job, so its cordon was left alone. {cause}", {}
    if not (found.get("spec") or {}).get("unschedulable"):
        return f"{opening}. The host is not cordoned. {cause}", {}
    try:
        NODE_ACTIONS.set_cordon(power_plan["node"], False)
    except Exception as error:
        return f"{opening}. The host stays cordoned, because uncordoning it failed: {error}. {cause}", {}
    return f"{opening}. The host was uncordoned, because this job had cordoned it. {cause}", {"uncordoned": True}


def _key(item):
    return (item.get("kind"), item.get("ns"), item.get("name"))


def _held_by(operation_id):
    return next((((i.get("ref") or {}).get("held") or []) for i in OPS.stored() if i.get("id") == operation_id), [])


def _start_what_was_held(operation_id, message):
    """What this job stopped starts again. The message says so, and the second
    answer is what the job's record gets. The message ends with one full stop."""
    restored = {}
    message = message.rstrip(" .")
    held = _held_by(operation_id)
    if held:
        try:
            started, left = HOLD.restore(held, operation_id)
            restored = {"restored": {"started": started, "left": left, "at": time.time()}}
            message += (". Started again: " + ", ".join(started)) if started else ""
            message += (". Left as they are: " + ", ".join(left)) if left else ""
        except Exception as error:
            message += f". What was stopped could not be started again ({str(error)[:120]}). Start it manually"
    return message + ".", restored


# The words of the steps it shares with a reboot, for a job that sends no power.
_POWER_WORDS = ((". Nothing has been sent to the host yet", ""), ("Nothing was sent", "Maintenance was not started"),
                ("nothing was sent", "maintenance was not started"),
                (" before the restart or shutdown", ""))


def _maintenance_words(text):
    for old, new in _POWER_WORDS:
        text = text.replace(old, new)
    return text


def _run_maintenance(operation_id, power_plan, resumed=False):
    """Cordon, drain and recheck as for a reboot, then open the event instead
    of sending power. The job ends there: a running one would count as an
    active power job for the whole maintenance, and End isn't one of its steps.
    A copy evicted by the drain is carried on as any power job is, and a declared
    event is one per job, so opening it twice opens it once."""
    node, declared = power_plan["node"], power_plan["declared"]
    planned_outage = bool(power_plan.get("planned_outage"))

    def progress(phase, percent, message, **details):
        details.update(_cordon_found(operation_id, power_plan, phase))
        return OPS.record_phase(operation_id, phase, percent, _maintenance_words(message), owner=POD_NAME or None, **details)

    def before_open():
        (POWER.recheck_planned_outage if planned_outage else POWER.recheck_after_drain)(power_plan)

    opening = False
    try:
        steps = []
        if not _opened_by(node, operation_id):
            # The review finds an event already open, so a copy carrying on after one was opened must not repeat it.
            steps = NODE_ACTIONS.node_maintain(
                node, before_open, power_plan["drain_pods"], progress, planned_outage=planned_outage, resumed=resumed,
                hold=None if planned_outage else (lambda: hold_for_power(operation_id, power_plan, progress)))["steps"]
            opening = True
            _open_maintenance(operation_id, power_plan, declared, planned_outage)
        progress("maintained", 100, f"In maintenance: {declared['reason']}")
        OPS.advance_soon()     # the job is done: until the loop sees that, it blocks a Reboot
        return {"ok": True, "node": node, "action": "maintenance", "steps": steps, "operation": {"id": operation_id}}
    except OPS.Superseded:
        raise  # another replica owns the job
    except Exception as e:
        if _opened_by(node, operation_id):
            # Only saying so failed. The event is open and End is what lets it go,
            # so what waits goes on waiting.
            message = f"In maintenance: {declared['reason']}. Its job could not record that ({str(e)[:120]})"
            progress("failed", 100, message)
            raise PowerNotSent(message, {"id": operation_id}) from e
        if opening:
            # Whether the event was written is not known: the host stays
            # cordoned, as the error says.
            message, cordon = "Maintenance was not started. Check whether the host is still cordoned: " + str(e), {}
        else:
            message, cordon = _after_stop(operation_id, power_plan, "Maintenance was not started", str(e))
        message, restored = _start_what_was_held(operation_id, _maintenance_words(message))
        progress("failed", 10, message, **cordon, **restored)
        raise PowerNotSent(message, {"id": operation_id}) from e


def _opened_by(node, operation_id):
    try:
        return (HISTORY.declared_for(node, strict=True) or {}).get("op") == operation_id
    except Exception:
        return False


def _moved_apps(power_plan):
    """App uptime's keys for what a maintenance's drain moves: nothing on a
    single host, where nothing is drained."""
    if power_plan.get("planned_outage"):
        return []
    return ([f"{w.get('ns')}/{w.get('name')}" for w in power_plan.get("workloads") or []]
            + [f"vm:{vm}" for vm in power_plan.get("vms") or []])




def _open_maintenance(operation_id, power_plan, declared, planned_outage):
    """The event is the state here, unlike a reboot's: a history that can't be
    written stops the job, and says so. Its time counts from now, so a long
    drain doesn't eat the time asked for."""
    now = int(time.time())
    apps = _moved_apps(power_plan)
    started = next(((i.get("ref") or {}).get("started_epoch") for i in OPS.stored() if i.get("id") == operation_id), None)
    try:
        return HISTORY.maintenance_declare(
            power_plan["node"], declared["reason"], operation_id, now + declared["hours"] * 3600,
            # Nothing was cordoned on a single host, so End has nothing to take off.
            cordoned_before=bool(power_plan.get("cordoned")) or planned_outage, node_uid=power_plan["node_uid"],
            held=_held_by(operation_id), apps=apps, started=started, now=now)
    except OPS.Superseded:
        raise
    except Exception as error:
        raise ValueError(f"The host is cordoned, but its maintenance could not be recorded ({str(error)[:120]}). "
                         "Uncordon it manually when the work is done.") from error


def send_handoff(operation_id, power_plan, steps, rep, report):
    """The last step on a cluster's only host. Homestead's data volume is on
    that host: going down with Homestead still writing to it left the volume
    faulted. So a helper on the host takes over - Homestead's own image and
    account - and Homestead stops itself; the helper waits for its volume to
    detach, then asks systemd for the reboot or power-off, and starts
    Homestead again on the new boot (homestead_power_handoff)."""
    node, action = power_plan["node"], power_plan["action"]
    path = f"/apis/apps/v1/namespaces/{SELF.NS}/deployments/{NAMES.BRAND}"
    try:
        own, image = _self_data_helper_image(kget, SELF.NS)
        claim, _ = homestead_running_on()
        replicas = int((kget(path).get("spec") or {}).get("replicas", 1) or 1)
    except Exception as error:
        report("verifying", 15, f"Homestead cannot hand the last step to the host ({str(error)[:160]}). Sending the {action} directly.")
        return NODE_ACTIONS._send_power(node, action, steps, rep, report)
    spec = own.get("spec") or {}
    pod_name = f"homestead-handoff-{action}-{int(time.time()) % 100000}"
    body = {"apiVersion": "v1", "kind": "Pod",
            "metadata": {"name": pod_name, "namespace": SELF.NS, "labels": NAMES.labels("node-power")},
            "spec": {"nodeName": node, "hostPID": True, "restartPolicy": "OnFailure",
                     "serviceAccountName": spec.get("serviceAccountName") or "default",
                     "imagePullSecrets": spec.get("imagePullSecrets") or [],
                     "tolerations": [{"operator": "Exists"}],
                     "terminationGracePeriodSeconds": 1,
                     "containers": [{"name": "handoff", "image": image,
                                     "command": ["python3", "/srv/homestead_power_handoff.py", SELF.NS, NAMES.BRAND,
                                                 operation_id, action, power_plan["boot_id"], claim],
                                     "securityContext": {"privileged": True, "runAsUser": 0, "runAsGroup": 0},
                                     "resources": {"requests": {"cpu": "10m", "memory": "32Mi"}, "limits": {"memory": "128Mi"}}}]}}
    # The helper's identity is on the job before it exists, as for any power helper.
    report("sending", 20, "Handing the last step to a helper on the host. Nothing has been sent to the host yet",
           helper_pod=pod_name, helper_namespace=SELF.NS, handoff=True, started_epoch=time.time())
    receipt = ksend("POST", f"/api/v1/namespaces/{SELF.NS}/pods", body)
    meta = (receipt or {}).get("metadata") or {}
    if not meta.get("uid") or meta.get("name") != pod_name:
        raise ValueError("The host helper was not confirmed. Check it before trying again.")
    report("observing", 20, f"Homestead is stopping so that its data volume is released. The helper on the host then sends the {action}. "
           "This page is offline until the host is back.", helper_uid=meta["uid"])
    # Homestead stops last, marked like anything else held for the host, so
    # the helper starts it again - and only while that mark is the job's.
    ksend("PATCH", path, {"metadata": {"annotations": {HOLD.HELD_BY: operation_id, HOLD.HELD_AS: str(replicas)}},
                          "spec": {"replicas": 0}}, ctype="application/merge-patch+json")
    steps.append(f"handed the {action} to {pod_name}, and Homestead stopped so that its data volume is released")
    return {"ok": True, "node": node, "action": action, "steps": steps, "helper_pod": pod_name, "quorum": rep}


def hold_for_power(operation_id, power_plan, progress):
    """Stop what waits for the host and live-migrate the VMs that move, then
    wait for them to leave it. Safe to repeat: a resumed job finds its own
    marks. Returns whether there was anything to do."""
    node, picks = power_plan["node"], power_plan.get("choices") or {}
    items = power_plan.get("hold") or []
    waiting = [i for i in items if picks.get(i["id"]) == "wait"]
    moving = [i for i in items if i["kind"] == "VirtualMachine" and picks.get(i["id"]) == "move"]
    if not waiting and not moving:
        return False
    stopping = (f"Stopping {len(waiting)} app(s) and VM(s) to wait for the host"
                + (f", moving {len(moving)} VM{'' if len(moving) == 1 else 's'}" if moving else "") + ". Nothing has been sent to the host yet")
    progress("holding", 8, stopping)
    # The whole list goes on the job once, before the first app is touched,
    # and joins what an earlier pass of this job already recorded, never
    # replacing it: a pass that dies part way must not drop an app an earlier
    # one stopped (#1321). Restore reads how each ran from its own mark, so
    # "was" waits until the stop says.
    held = list(_held_by(operation_id))
    before = {_key(i) for i in held}
    held += [{**item, "was": None} for item in waiting if _key(item) not in before]
    progress("holding", 8, stopping, held=held)
    done = set()
    for item in waiting:
        try:
            result = HOLD.stop(item, operation_id)
        except ValueError:
            # A refusal comes before any write (another job's mark, a
            # StatefulSet that would lose its claims): what this pass listed
            # but has not stopped was never touched, so it goes off the list
            # again. What an earlier pass stopped stays: that hold is real.
            held = [i for i in held if _key(i) in before or _key(i) in done]
            progress("holding", 8, stopping, held=held)
            raise
        done.add(_key(item))
        held = [result if _key(i) == _key(item) else i for i in held]
    progress("holding", 8, "Waiting for them to stop. Nothing has been sent to the host yet", held=held)
    if moving:
        # A resumed job finds some already moved: only what is still here moves.
        here = {((v.get("metadata") or {}).get("namespace"), (v.get("metadata") or {}).get("name"))
                for v in kget("/apis/kubevirt.io/v1/virtualmachineinstances").get("items", [])
                if (v.get("status") or {}).get("nodeName") == node}
        for vm in moving:
            if (vm["ns"], vm["name"]) in here:
                HOLD.migrate(vm)
    deadline = time.monotonic() + 600
    while True:
        pods = [p for p in kget("/api/v1/pods").get("items", []) if (p.get("spec") or {}).get("nodeName") == node]
        vmis = [v for v in (kget("/apis/kubevirt.io/v1/virtualmachineinstances").get("items", []) if items and any(
            i["kind"] == "VirtualMachine" for i in items) else []) if (v.get("status") or {}).get("nodeName") == node]
        pending = [f"{i['ns']}/{i['name']}" for i in waiting + moving if not HOLD.gone(i, node, pods, vmis)]
        if not pending:
            return True
        if time.monotonic() >= deadline:
            raise ValueError("These did not stop or move within 10 minutes: " + ", ".join(pending[:6])
                             + ". What was stopped stays stopped until this job is released or the host is back")
        progress("holding", 8, "Waiting to stop or move: " + ", ".join(pending[:6]) + ". Nothing has been sent to the host yet")
        time.sleep(3)


def restore_held(item, uncordon=True):
    """Start again what a power job stopped to wait for its host. Leader only,
    once; the job's resolver calls it and records what happened."""
    ref = item.get("ref") or {}
    if not LEADER.is_leader() or ref.get("restored") is not None:
        return ref.get("restored") is not None
    if uncordon and not ref.get("planned_outage") and not ref.get("cordoned_before"):
        # What waited is for this host; it cannot start here while cordoned.
        node = kget(f"/api/v1/nodes/{urllib.parse.quote(ref['node'], safe='')}")
        if ref.get("node_uid") and (node.get("metadata") or {}).get("uid") == ref["node_uid"]:
            NODE_ACTIONS.set_cordon(ref["node"], False)
            ref["uncordoned"] = True
    started, left = HOLD.restore(ref.get("held") or [], item["id"])
    ref["restored"] = {"started": started, "left": left, "at": time.time()}
    return True


def allow_scheduling(item):
    """Uncordon a host its power job cordoned, now it is back. Leader only,
    once; only the host the job was for, by identity."""
    ref = item.get("ref") or {}
    if not LEADER.is_leader():
        return False
    if ref.get("uncordoned"):
        return True
    node = kget(f"/api/v1/nodes/{urllib.parse.quote(ref['node'], safe='')}")
    if ref.get("node_uid") and (node.get("metadata") or {}).get("uid") != ref["node_uid"]:
        return False
    if (node.get("spec") or {}).get("unschedulable"):
        NODE_ACTIONS.set_cordon(ref["node"], False)
    ref["uncordoned"] = True
    return True


def release_held_power(operation_id):
    """Start what waits for a host on other hosts now, without waiting for it."""
    with OPS.transaction() as t:
        items = t.items
        item = next((i for i in items if i.get("id") == operation_id), None)
        if not item or item.get("kind") != "node-power":
            raise ValueError("host power job not found")
        ref = item.get("ref") or {}
        if not ref.get("held") or ref.get("restored") is not None:
            raise ValueError("nothing is waiting for this host")
        if ref.get("phase") not in ("observing",):
            raise ValueError("wait until the power command has been sent")
        restore_held(item, uncordon=False)
        t.save(items)
    return {"ok": True, "detail": "Started again: " + (", ".join(ref["restored"]["started"]) or "nothing")}


def _power_in_background(operation_id, power_plan, force, resumed=False):
    def run():
        try:
            run_power_job(operation_id, power_plan, force, resumed)
        except (PowerNotSent, OPS.Superseded):
            pass  # recorded on the job, or carried on by another replica
        except Exception as error:  # never leave the job looking busy
            try:
                OPS.record_phase(operation_id, "failed", 10, f"Host power job stopped: {error}"[:400])
            except Exception:
                pass
        finally:
            with _POWER_RESUME_LOCK:
                _POWER_RESUMING.discard(operation_id)
    threading.Thread(target=run, name=f"node-power-{power_plan['node']}", daemon=True).start()


def send_reviewed_power(power_plan, force=False, background=False):
    """Cordon, drain and send a reviewed reboot or shutdown, as a job - what
    Host actions does once its review is accepted, and what an OS update of
    every host does for each host that needs a restart. background returns
    the job at once and leaves the work, and its outcome, to the job."""
    node, action = power_plan["node"], power_plan["action"]
    planned_outage = bool(power_plan.get("planned_outage")) and not force
    operation = OPS.start(
        "node-power", f"{action} {node}", {"kind": "Node", "name": node},
        "/nodes?node=" + urllib.parse.quote(node),
        {"node": node, "node_uid": power_plan["node_uid"], "action": action, "boot_id": power_plan["boot_id"],
         "choices": power_plan.get("choices") or {},
         # A host cordoned before the review stays so after it; one this job
         # cordoned is allowed scheduling again when it is back.
         "cordoned_before": bool(power_plan.get("cordoned")),
         "volumes": [v["name"] for v in power_plan["volumes"]],
         "planned_outage": planned_outage, "forced": bool(force),
         # What a resumed job needs: the reviewed plan, and which replica runs it.
         "plan": power_plan, "worker": POD_NAME, "worker_restarts": own_restarts(),
         "phase": "reviewed", "phase_at": time.time(), "started_epoch": time.time()},
        "Planned whole-cluster outage. Recording it without a cordon or drain." if planned_outage and action == "maintenance" else
        "Planned whole-cluster outage. Sending without a cordon or drain." if planned_outage else
        "Forced by an admin. Sending without a cordon or drain." if force else "Effect on the host reviewed. Preparing the cordon and drain.")
    if not background:
        result = run_power_job(operation["id"], power_plan, force)
        result["operation"] = operation
        return result
    with _POWER_RESUME_LOCK:
        _POWER_RESUMING.add(operation["id"])
    _power_in_background(operation["id"], power_plan, force)
    return {"operation": operation, "steps": [], "background": True}


def _restarts(pod):
    """Homestead's own container's restart count in pod, or None without one:
    a sidecar restarting is not the worker going."""
    for status in ((pod or {}).get("status") or {}).get("containerStatuses") or []:
        if status.get("name") == NAMES.BRAND:
            return int(status.get("restartCount") or 0)
    return None


def own_restarts():
    """How often this replica's Homestead container has restarted, recorded on a power
    job when its worker starts it: a higher count later means the process
    running the job died with its container, though the pod lives on. None
    when it can't be read, and the job keeps the pod-only check."""
    try:
        return _restarts(kget(f"/api/v1/namespaces/{SELF.NS}/pods/{urllib.parse.quote(POD_NAME, safe='')}"))
    except Exception:
        return None


def power_worker_gone(pod, restarts=None):
    """Whether the replica a power job named has gone - evicted by the drain,
    or its container restarted since the job recorded restarts."""
    try:
        found = kget(f"/api/v1/namespaces/{SELF.NS}/pods/{urllib.parse.quote(pod, safe='')}")
    except urllib.error.HTTPError as error:
        return error.code == 404
    except Exception:
        return False  # unknown is not gone
    if (found.get("metadata") or {}).get("deletionTimestamp") or \
            (found.get("status") or {}).get("phase") in ("Succeeded", "Failed"):
        return True
    now = _restarts(found)
    if restarts is not None and now is not None and now > restarts:
        return True
    # A host shut down or lost leaves its pods listed for minutes; one whose
    # node is not Ready is not running anything.
    node = (found.get("spec") or {}).get("nodeName")
    if not node:
        return False
    try:
        conditions = (kget(f"/api/v1/nodes/{urllib.parse.quote(node, safe='')}").get("status") or {}).get("conditions") or []
    except Exception:
        return False
    return not any(c.get("type") == "Ready" and c.get("status") == "True" for c in conditions)


def resume_power_job(item):
    """Carry on a host power job whose replica was evicted before it sent the
    command. Only the leader does, once; called from the job's resolver, so it
    writes nothing here - the thread claims the job, then cordons and drains
    again (both safe to repeat), rechecks and sends."""
    ref = item.get("ref") or {}
    if not LEADER.is_leader() or not isinstance(ref.get("plan"), dict):
        return False
    with _POWER_RESUME_LOCK:
        if item["id"] in _POWER_RESUMING:
            return True
        _POWER_RESUMING.add(item["id"])

    def claim_then_run():
        try:
            OPS.record_phase(item["id"], ref.get("phase", "draining"), item.get("progress", 10),
                             "Homestead restarted or moved while preparing this host. Continuing.", worker=POD_NAME,
                             worker_restarts=own_restarts(), expect_worker=ref.get("worker"), expect_phase=POWER.BEFORE_SEND)
        except Exception:
            with _POWER_RESUME_LOCK:
                _POWER_RESUMING.discard(item["id"])
            return
        _power_in_background(item["id"], ref["plan"], bool(ref.get("forced")), resumed=True)
    threading.Thread(target=claim_then_run, name=f"node-power-resume-{ref.get('node', '')}", daemon=True).start()
    return True


SYSTEM_HOST_PATHS = ("/var/lib/kubelet", "/run", "/var/run", "/dev", "/sys", "/proc", "/lib/modules",
                     "/etc/localtime", "/var/log", "/var/lib/rancher", "/etc/rancher")


SYSTEM_POD_NAMESPACES = ("kube-system", "longhorn-system", "kubevirt", "cdi", "cattle-system", "system-upgrade",
                         "cattle-fleet-system", "harvester-system")


def _system_host_path(row):
    """A host path that is the host's own plumbing - kubelet's plugin and pod
    directories, sockets, devices, logs - not data a pod keeps there. Nor
    is anything Kubernetes or Longhorn itself mounts: an instance manager
    mounts / and /var/lib/longhorn, and the volumes it serves are judged by
    their copies, not by its mounts."""
    if not str(row.get("kind", "")).startswith("host-local path"):
        return False
    if str(row.get("pod") or "").split("/", 1)[0] in SYSTEM_POD_NAMESPACES:
        return True
    path = "/" + str(row.get("source") or "").strip("/")
    return any(path == p or path.startswith(p + "/") for p in SYSTEM_HOST_PATHS)


def rollout_reboot(node, allow_single_copy=False):
    """A restart for an OS update: the same review Host actions shows, with
    nobody to accept its warnings - so what a person would have to accept
    stops it, except a volume's only copy when the settings accept that."""
    power_plan = _unattended_plan(node, allow_single_copy)
    power_plan["reason"] = "OS update"
    try:
        return send_reviewed_power(power_plan)["operation"]["id"]
    except PowerNotSent as e:
        raise ValueError(str(e)) from e


def rollout_check(node, allow_single_copy=False):
    """What rollout_reboot would refuse with today, or "", and whether that may
    clear by itself (the rollout then waits first). Read only. A read that fails
    raises: it is not an answer either way."""
    try:
        _unattended_plan(node, allow_single_copy)
    except DRAIN_BLOCKERS.MayClear as error:
        return str(error), True
    except ValueError as error:
        return str(error), False
    return "", False


def _unattended_plan(node, allow_single_copy):
    """The plan a restart with nobody there may send; ValueError says what stops
    it, all of it. MayClear only when nothing in it stays until a person acts."""
    power_plan = POWER.plan(node, "reboot")
    if power_plan.get("planned_outage"):
        raise ValueError("A single-host cluster outage needs a manual review and acknowledgement in Host actions")
    stays, may_clear = list(power_plan["blockers"]) if not power_plan["ready"] else [], []
    if power_plan.get("requires_blocker_ack"):
        # A person ticks each of these; with nobody there the drain would stall on
        # them, so the host waits. Without the time a pod has been stuck: the same
        # pod later is the same answer.
        rows = power_plan["drain_blockers"]
        said = "Something would keep this host from draining: " + "; ".join(what for _, what in DRAIN_BLOCKERS.review(rows))
        (may_clear if all(row["kind"] in DRAIN_BLOCKERS.CLEARS_BY_ITSELF for row in rows) else stays).append(said)
    if power_plan.get("stranded"):
        stays.append("some workloads have no other host to run on")
    hold = power_plan.get("hold") or []
    if any(i["kind"] == "VirtualMachine" for i in hold):
        stays.append("Running VMs are on this host. Migrate or stop them and review again.")
    waits = [f"{i['ns']}/{i['name']}" for i in hold if i["default"] != "move"]
    if waits:
        stays.append("These would have to stop and wait for the host: " + ", ".join(waits[:6]))
    if power_plan.get("requires_data_ack") and not allow_single_copy:
        # What a person would have to accept, said as it is. A pod's emptyDir
        # is scratch space every drain deletes - on a k3s host metrics-server
        # and Traefik have one - and is no reason to leave a host unrestarted.
        reasons = []
        single = [v["claim"] for v in power_plan.get("volumes") or [] if v.get("risk") in ("unavailable", "single-copy")]
        if single:
            reasons.append("a volume has its only healthy copy on this host: " + ", ".join(single[:4]))
        # Nor are the host's own sockets and devices data: Longhorn's CSI
        # attacher mounts /var/lib/kubelet/plugins/driver.longhorn.io.
        kept = [f"{row['pod']} ({row['source']})" for row in (power_plan.get("maintenance") or {}).get("local_storage") or []
                if not str(row.get("kind", "")).startswith("emptyDir") and not _system_host_path(row)]
        if kept:
            reasons.append("pods keep data on this host itself: " + ", ".join(kept[:4]))
        if power_plan.get("storage_unknown"):
            reasons.append("Longhorn's volumes could not be read")
        if reasons:
            # Copies can rebuild and a read can answer; data kept on the host won't go by itself.
            (stays if kept else may_clear).append("; ".join(reasons) + " (the settings do not accept that)")
    if stays or may_clear:
        raise (ValueError if stays else DRAIN_BLOCKERS.MayClear)("; ".join(stays + may_clear))
    power_plan["choices"] = HOLD.choose(hold, None)
    return power_plan


def rollout_power_job(node, since):
    """A restart of this host started since the rollout began and not failed:
    the one a drained leader left running."""
    # The stored records: the public list leaves each job's ref out.
    for item in OPS.stored():
        ref = item.get("ref") or {}
        if (item.get("kind") == "node-power" and ref.get("node") == node and ref.get("action") == "reboot"
                and float(ref.get("started_epoch") or 0) >= float(since or 0)
                and item.get("status") not in ("failed", "cancelled")):
            return item["id"]
    return ""


def _plan(request):
    q = request.query
    return power_plan_with_job(POWER.plan((q.get("node") or [""])[0], (q.get("action") or [""])[0],
                                          force=(q.get("force") or [""])[0] == "1"))


DECLARED_HOURS = (1, 2, 4, 8, 24)


def declared_hours(value):
    """Hours a person may ask for: DECLARED_HOURS, the last being "until I end it"
    under the agreed ceiling. ValueError for anything else."""
    if isinstance(value, bool) or value not in DECLARED_HOURS or not isinstance(value, int):
        raise ValueError("hours must be one of " + ", ".join(map(str, DECLARED_HOURS)))
    return value


def _declared_request(body):
    """The typed reason and the hours, or ValueError. A reserved reason is the
    words Homestead itself writes for its own maintenance."""
    reason = body.get("reason")
    reason = reason.strip() if isinstance(reason, str) else ""
    if not 1 <= len(reason) <= 120 or not reason.isprintable():
        raise ValueError("Give a reason of 1 to 120 printable characters")
    if reason.casefold() in {word.casefold() for word in HISTORY.RESERVED_REASONS}:
        raise ValueError(f"\"{reason}\" is the name Homestead uses for its own maintenance. Give another reason.")
    return {"reason": reason, "hours": declared_hours(body.get("hours"))}


def _send_power(request):
    """Host actions' send: refused (409) whenever the review it carries no
    longer holds, so the browser shows the plan again."""
    b = request.body
    if b.get("confirm") != b.get("node"):
        raise ValueError("confirmation must repeat the node name")
    force = b.get("force") is True
    declared = None
    if b.get("action") == "maintenance":
        if b.get("force"):
            raise ValueError("Maintenance has no force: resolve what stops it, or cordon the host by hand")
        declared = _declared_request(b)
    running = active_power_job(b.get("node"))
    if running:
        return ROUTER.Reply(409, {"error": "A restart or shutdown of this host is still running. Follow it instead of sending another.",
                                  "operation": running})
    power_plan = POWER.plan(b["node"], b["action"], force=force)
    if not power_plan["ready"]:
        return ROUTER.Reply(409, {"error": "; ".join(power_plan["blockers"]), "plan": power_plan})
    if b.get("review_token") != power_plan["review_token"]:
        return ROUTER.Reply(409, {"error": "The effect on the host changed. Review the plan again.", "plan": power_plan})
    if power_plan.get("planned_outage") and b.get("allow_cluster_outage") is not True:
        return ROUTER.Reply(409, {"error": "Confirm the whole-cluster outage before restarting or shutting down the host",
                                  "plan": power_plan})
    # Forced, nothing is stopped or moved first (hold=None): what has no option is not asked to choose.
    power_plan["choices"] = HOLD.choose([i for i in power_plan.get("hold") or [] if i["options"] or not force],
                                        b.get("choices") or {})
    stranded = {(w["ns"], w["name"]) for w in power_plan["stranded"]}
    if any((i["ns"], i["name"]) in stranded and power_plan["choices"].get(i["id"]) == "move"
           for i in power_plan.get("hold") or []) and not b.get("allow_stranded"):
        return ROUTER.Reply(409, {"error": "some workloads have no eligible failover host", "plan": power_plan})
    if power_plan["requires_data_ack"] and not b.get("allow_data_risk"):
        return ROUTER.Reply(409, {"error": "Confirm the volume risk before restarting or shutting down the host", "plan": power_plan})
    # Each drain blocker the review named, acknowledged by its id: no more, no fewer.
    given = b.get("allow_drain_blockers") or []
    if (not isinstance(given, list) or
            set(map(str, given)) != {row["id"] for row in power_plan.get("drain_blockers") or []}):
        return ROUTER.Reply(409, {"error": "Confirm each drain blocker, or remove the orphaned records, before restarting or shutting down the host",
                                  "plan": power_plan})
    if declared:
        power_plan["declared"] = declared
    # Cordon and drain can take many minutes: the request returns the job at
    # once and the browser follows its phases.
    return ROUTER.Reply(202, send_reviewed_power(power_plan, force, background=True))


ROUTES = {
    ("GET", "/api/node/power/plan"): ("viewer", _plan),
    ("POST", "/api/node/power"): ("admin", _send_power),
    ("POST", "/api/node/power/release"): ("operator", lambda request: release_held_power(str(request.body.get("id") or ""))),
}
