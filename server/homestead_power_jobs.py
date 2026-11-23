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
import re
import threading
import time
import urllib.error
import urllib.parse

import homestead_cordon as CORDON
import homestead_history as HISTORY
import homestead_drain_blockers as DRAIN_BLOCKERS
import homestead_leader as LEADER
import homestead_maintenance as MAINTENANCE
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


# The job kind whose mark a host job's cordon carries (homestead_cordon).
CORDON_KIND = "node-power"


def _cordon_found(operation_id, power_plan, phase):
    """At the job's cordon step, just before it cordons: whether the host has
    no cordon yet, so that the cordon there after it is this job's own (#1289),
    and then that cordon, with this job's mark on the Node (homestead_cordon).
    A person's Cordon or Uncordon on the Hosts page clears the mark, and the
    job's end then leaves the host as they set it (cordon_held, or its mark
    could not be written: the host stays cordoned at the end).
    own_cordon is recorded once: a resumed job keeps its first answer, and
    one that died before recording it finds its own mark on the host. A
    resumed job whose cordon was its own marks again what its cordon step
    cordons again (a person may have uncordoned the host since). A host that
    is not the reviewed host counts as cordoned already, so its cordon is
    never taken off. One that cannot be read is still marked: cordon() reads
    it again and tells a person's cordon apart; if that fails too, the host
    stays cordoned at the end, saying so."""
    if phase != "cordoning":
        return {}
    try:
        ref = _job_ref(operation_id)
        if "own_cordon" in ref:
            return _mark(power_plan, operation_id) if ref.get("cordon_held") else {}
        own = False
        if power_plan.get("node_uid"):
            found = kget(f"/api/v1/nodes/{urllib.parse.quote(power_plan['node'], safe='')}")
            own = ((found.get("metadata") or {}).get("uid") == power_plan["node_uid"]
                   and (not (found.get("spec") or {}).get("unschedulable")
                        or f"{CORDON_KIND}:{operation_id}" in CORDON.holders(found)))
        return {"own_cordon": own, **(_mark(power_plan, operation_id) if own else {"cordon_held": False})}
    except Exception:
        pass
    try:
        marked = _mark(power_plan, operation_id)
    except Exception:
        marked = {"cordon_held": False}
    # Not its own only when cordon() found someone else's cordon.
    return {"own_cordon": not marked.get("cordon_taken"), **marked}


def _mark(power_plan, operation_id):
    """Cordon the host with this job's mark. A host a person cordoned since
    stays theirs (cordon_taken). Any other failure may follow a write whose
    reply was lost, so the mark is released (homestead_cordon's rule) and the
    host stays cordoned at the end, as the job then says."""
    node = power_plan["node"]
    outcome = CORDON.cordon(node, CORDON_KIND, operation_id)
    if outcome.done:
        return {"cordon_held": True}
    if outcome.reason in CORDON.BEFORE:
        return {"cordon_held": False, "cordon_taken": True}
    CORDON.release(node, CORDON_KIND, operation_id, power_plan.get("node_uid") or "")
    return {"cordon_held": False}


# What a job tells a person to do with a host it leaves cordoned.
READY_AGAIN = "Uncordon it on the Hosts page when it is ready for work again"


def _title(holder):
    """A cordon holder as the Hosts page names it: release() gives a mark
    (<kind>:<job id>) or a job's title, or its kind when it has none. A mark
    names its job's title; a kind, or a job not listed, is "another job"."""
    kind, colon, ident = holder.partition(":")
    if colon and re.fullmatch(r"[a-z0-9-]+", kind) and re.fullmatch(r"[A-Za-z0-9._-]+", ident):
        try:
            item = next((i for i in OPS.stored() if str(i.get("id")) == ident), None)
        except Exception:
            item = None
        return (item or {}).get("title") or "another job"
    return "another job" if re.fullmatch(r"[a-z0-9-]+", holder) else holder


def give_back(node, operation_id, uid):
    """Release a host job's marked cordon (homestead_cordon.release) and say
    what became of it: (what, words, the helper's reason). what is "uncordoned";
    "free" (it was not cordoned); "gone" (another host by now); "changed" (a
    person's Cordon or Uncordon took the cordon over: left as they set it);
    "kept" (held for another job or left for a person); "retry" (it could not
    be read or written: it stays cordoned for now). words: for "kept" and
    "retry", what follows "The host" or "It"."""
    outcome = CORDON.release(node, CORDON_KIND, operation_id, uid or "")
    reason = outcome.reason
    what = {"uncordoned": "uncordoned", "released": "free", CORDON.GONE: "gone",
            "not held by this job": "changed"}.get(reason)
    if what:
        return what, "", reason
    if reason == CORDON.FOR_PERSON:
        return "kept", ("stays cordoned, because a job left it cordoned for a person to check. "
                        "Uncordon it on the Hosts page once it has been checked"), reason
    if reason.startswith("still held by "):
        return "kept", (f"stays cordoned while {_title(reason[len('still held by '):])} holds it. "
                        "If it is still cordoned once that has ended, uncordon it on the Hosts page"), reason
    return "retry", f"stays cordoned, because uncordoning it failed: {reason}. {READY_AGAIN}", reason


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
    if ref.get("cordoned_before") is not False:
        return f"{opening}. The host stays cordoned, as it was before this job. {cause}", {}
    if not ref["own_cordon"]:
        return f"{opening}. The host stays cordoned, as it was before this job's cordon step. {cause}", {}
    if "cordon_held" in ref:
        if not ref["cordon_held"]:
            if not ref.get("cordon_taken") and _cordoned(power_plan["node"], power_plan.get("node_uid")) is False:
                # Its lost mark was released and its own cordon step then failed.
                return f"{opening}. The host is not cordoned. {cause}", {}
            return f"{opening}. The host {_unheld(ref, 'this job')}. {cause}", {}
        what, words, _ = give_back(power_plan["node"], operation_id, power_plan.get("node_uid"))
        return (f"{opening}. {_STOP_WORDS.get(what) or f'The host {words}.'} {cause}",
                {"uncordoned": True} if what == "uncordoned" else {})
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


# What a stopped job says of its marked cordon, by what give_back() found.
_STOP_WORDS = {"uncordoned": "The host was uncordoned, because this job had cordoned it.",
               "free": "The host is not cordoned.",
               "gone": "The host was replaced during this job, so its cordon was left alone.",
               "changed": "The host was left as someone set it during this job."}
# What a job that ran says of a marked cordon it did not take off.
_KEPT_WORDS = {"gone": "The host was replaced, so its cordon was left alone",
               "changed": "It was left as someone set it during this job"}


def _cordoned(node, uid):
    """Whether the host (by uid) is cordoned now; None when that is not known."""
    try:
        found = kget(f"/api/v1/nodes/{urllib.parse.quote(node, safe='')}")
    except Exception:
        return None
    if (found.get("metadata") or {}).get("uid") != uid:
        return None
    return bool((found.get("spec") or {}).get("unschedulable"))


def _unheld(ref, who):
    """Words, after "The host" or "It", for a cordon whose mark is not this
    job's: one a person put on, or one it could not mark."""
    if ref.get("cordon_taken"):
        return f"was left as someone set it during {who}"
    return f"stays cordoned, because {who} could not mark its cordon as its own. {READY_AGAIN}"


def _give_back_job(item):
    """A job whose cordon carries its mark, its host back: take its cordon
    off, or record on the job why it stays. False while it could not be done."""
    ref = item.get("ref") or {}
    if not ref.get("cordon_held"):
        ref["cordon_kept"] = ("It stays cordoned, as it was before this job's cordon step" if not ref.get("own_cordon")
                              else "It " + _unheld(ref, "this job"))
        return True
    what, words, _ = give_back(ref["node"], item["id"], ref.get("node_uid"))
    if what == "retry":
        return False
    if what in ("uncordoned", "free"):
        ref["uncordoned"] = True
    else:
        ref["cordon_kept"] = _KEPT_WORDS.get(what) or "It " + words
    return True


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
    ref = _job_ref(operation_id)
    started = ref.get("started_epoch")
    # Whether End finds the cordon still this job's by its mark; an event without it is from before.
    mark = {"cordon_held": ref["cordon_held"]} if "cordon_held" in ref else {}
    try:
        return HISTORY.maintenance_declare(
            power_plan["node"], declared["reason"], operation_id, now + declared["hours"] * 3600,
            # Nothing was cordoned on a single host, so End has nothing to take off.
            # A host cordoned before the job's cordon step (own_cordon False) stays so too.
            cordoned_before=bool(power_plan.get("cordoned")) or planned_outage or ref.get("own_cordon") is False,
            node_uid=power_plan["node_uid"],
            held=_held_by(operation_id), apps=apps, started=started, now=now, **mark)
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
    if uncordon and not ref.get("planned_outage") and not ref.get("cordoned_before") and "cordon_held" in ref:
        # What waited is for this host; it cannot start here while cordoned.
        if not ref.get("uncordoned") and not ref.get("cordon_kept"):
            if not _give_back_job(item):
                return False
            if ref.get("cordon_kept") and _cordoned(ref["node"], ref.get("node_uid")):
                ref["cordon_kept"] += ". What waited is started; what can run only on this host starts once it is uncordoned"
    elif uncordon and not ref.get("planned_outage") and not ref.get("cordoned_before"):
        # A job from before its cordon carried its mark.
        node = kget(f"/api/v1/nodes/{urllib.parse.quote(ref['node'], safe='')}")
        if ref.get("node_uid") and (node.get("metadata") or {}).get("uid") == ref["node_uid"]:
            NODE_ACTIONS.set_cordon(ref["node"], False)
            ref["uncordoned"] = True
    started, left = HOLD.restore(ref.get("held") or [], item["id"])
    ref["restored"] = {"started": started, "left": left, "at": time.time()}
    return True


def allow_scheduling(item):
    """Uncordon a host its power job cordoned, now it is back. Leader only,
    once; only the host the job was for, by identity, and only while the
    cordon is still the job's own (its mark)."""
    ref = item.get("ref") or {}
    if not LEADER.is_leader():
        return False
    if ref.get("uncordoned") or ref.get("cordon_kept"):
        return True
    if "cordon_held" in ref:
        return _give_back_job(item)
    # A job from before its cordon carried its mark.
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
    """What rollout_reboot would refuse with today, or "", whether that may
    clear by itself (the rollout then waits first), and the same reasons as
    parts, one each. Read only. A read that fails raises: it is not an answer
    either way."""
    try:
        _unattended_plan(node, allow_single_copy)
    except DRAIN_BLOCKERS.MayClear as error:
        return str(error), True, getattr(error, "reasons", [])
    except ValueError as error:
        return str(error), False, getattr(error, "reasons", [])
    return "", False, []


# What a person reviewing an update reads, one line per reason (#1494): each
# reason as {kind, text, count, names, clears}. The refusal's own words stay as
# they were: the rollout's notes and its wait are said in them.
SINGLE_COPY_SETTING = ('"Restart a host even when a volume has its only healthy copy there" '
                       "in the Host updates settings")
IN_REVIEW = "Only a person can accept that, in this host's Restart host… review."
BY_ITSELF = "This may clear by itself within minutes."
DRAIN_TICKED = "Tick or remove them in this host's Restart host… review. An automatic restart cannot."
# The drain blockers, per kind of row: what is said, given how many rows (None:
# not counted by rows). Engine orphans are counted by their records instead.
DRAIN_SAID = {
    "other-orphans": lambda n: "Other Longhorn orphan records keep this host from draining.",
    "removing-orphans": lambda n: "Longhorn is still removing orphan records on this host.",
    "engines-elsewhere": lambda n: "Longhorn on this host still lists volumes that have moved to another host.",
    "stuck-pod": lambda n: f"{_counted(n, 'pod is', 'pods are')} still stopping long after {'it' if n == 1 else 'they'} should have, "
                           "which keeps this host from draining.",
    "stuck-attachment": lambda n: f"{_counted(n, 'volume is', 'volumes are')} still attached to this host with no pod using "
                                  f"{'it' if n == 1 else 'them'}.",
    "unread": lambda n: f"{n} of the drain checks could not be read.",
}
COUNTED_BY_ROWS = ("stuck-pod", "stuck-attachment", "unread")


def _counted(n, one, many):
    return f"{n} {one if n == 1 else many}"


def _part(kind, text, clears, names=(), count=None):
    return {"kind": kind, "text": text, "count": count, "names": list(names), "clears": clears}


def _drain_parts(rows):
    """One part per kind of drain blocker row, in the order the rows come, with
    each row as the review's token says it."""
    said = dict(DRAIN_BLOCKERS.review(rows))
    kinds = {}
    for row in rows:
        kinds.setdefault(row["kind"], []).append(row)
    parts = []
    for kind, these in kinds.items():
        count = len(these) if kind in COUNTED_BY_ROWS else None
        if kind == "engine-orphans":
            count = sum(len(row.get("records") or []) for row in these)
            text = f"{_counted(count, 'Longhorn orphan record keeps', 'Longhorn orphan records keep')} this host from draining."
        else:
            text = DRAIN_SAID.get(kind, lambda n: "Something would keep this host from draining.")(count)
        parts.append(_part("drain-" + kind, text, BY_ITSELF if kind in DRAIN_BLOCKERS.CLEARS_BY_ITSELF else DRAIN_TICKED,
                           [said[row["id"]] for row in these], count))
    return parts


# How homestead_power.plan starts its stops for Longhorn's drain rule, after the claim.
LONGHORN_HOLDS = (": this host holds its only complete copy", " is detached with its only copy on this host",
                  " keeps one copy, on this host")


def _copy_parts(power_plan, blockers):
    """The volumes a restart leaves with too few copies, as parts: those with no
    healthy copy elsewhere, and those left with one. A volume a review stop
    names already (Longhorn's drain rule: neither the setting nor waiting
    clears that) is said there only. Only Longhorn's own phrasings for a claim
    count: a maintenance stop for a pod of the same name ("<ns>/<pod>: ...") is
    no word on the volume. One copy elsewhere is refused too (requires_data_ack);
    two healthy copies on other hosts make the risk "resync", which is not."""
    def held(v):
        claim = v["claim"]
        return v.get("last_copy_here") and any(str(b).startswith(tuple(claim + said for said in LONGHORN_HOLDS)) for b in blockers)
    rows = [v for v in power_plan.get("volumes") or [] if v.get("risk") in ("unavailable", "single-copy")]
    parts = []
    here = [v["claim"] for v in rows if v["risk"] == "unavailable" and not held(v)]
    if here:
        parts.append(_part("single-copy", _counted(len(here), "volume has its", "volumes have their") + " only healthy copy on this host.",
                           f"Turn on {SINGLE_COPY_SETTING}, or wait until Longhorn has rebuilt copies on two other hosts.", here[:4], len(here)))
    left = [v["claim"] for v in rows if v["risk"] == "single-copy"]
    if left:
        parts.append(_part("one-copy-left", _counted(len(left), "volume", "volumes")
                           + " would have one healthy copy left, on another host, while this host restarts.",
                           f"Turn on {SINGLE_COPY_SETTING}, or wait until Longhorn has rebuilt more copies on other hosts.",
                           left[:4], len(left)))
    return parts


def _sentence(text):
    text = str(text).strip()
    return text if text.endswith((".", "!", "?")) else text + "."


def _unattended_plan(node, allow_single_copy):
    """The plan a restart with nobody there may send; ValueError says what stops
    it, all of it, and carries each reason as a part (reasons). MayClear only
    when nothing in it stays until a person acts."""
    power_plan = POWER.plan(node, "reboot")
    if power_plan.get("planned_outage"):
        error = ValueError("A single-host cluster outage needs a manual review and acknowledgement in Host actions")
        error.reasons = [_part("planned-outage", "This is the only host, so restarting it stops the whole cluster.", IN_REVIEW)]
        raise error
    blockers = list(power_plan["blockers"]) if not power_plan["ready"] else []
    stays, may_clear = list(blockers), []
    stay_parts = [_part("not-ready", _sentence(b), "This has to be dealt with before any restart. The Restart host… review shows it too.")
                  for b in blockers]
    clear_parts = []
    if power_plan.get("requires_blocker_ack"):
        # A person ticks each of these; with nobody there the drain would stall on
        # them, so the host waits. Without the time a pod has been stuck: the same
        # pod later is the same answer.
        rows = power_plan["drain_blockers"]
        said = "Something would keep this host from draining: " + "; ".join(what for _, what in DRAIN_BLOCKERS.review(rows))
        clears = all(row["kind"] in DRAIN_BLOCKERS.CLEARS_BY_ITSELF for row in rows)
        (may_clear if clears else stays).append(said)
        (clear_parts if clears else stay_parts).extend(_drain_parts(rows))
    if power_plan.get("stranded"):
        stays.append("some workloads have no other host to run on")
        stranded = power_plan["stranded"]
        stay_parts.append(_part("stranded", _counted(len(stranded), "workload has", "workloads have") + " no other host to run on.",
                                f"Only a person can accept stopping {'it' if len(stranded) == 1 else 'them'}, "
                                "in this host's Restart host… review.",
                                [f"{w.get('ns', '')}/{w.get('name', '')}" for w in stranded[:6]], len(stranded)))
    hold = power_plan.get("hold") or []
    vms = [i for i in hold if i["kind"] == "VirtualMachine"]
    if vms:
        stays.append("Running VMs are on this host. Migrate or stop them and review again.")
        stay_parts.append(_part("vms", _counted(len(vms), "virtual machine is", "virtual machines are") + " running on this host.",
                                f"Migrate or stop {'it' if len(vms) == 1 else 'them'} first.", [f"{i['ns']}/{i['name']}" for i in vms[:6]], len(vms)))
    waits = [f"{i['ns']}/{i['name']}" for i in hold if i["default"] != "move"]
    if waits:
        stays.append("These would have to stop and wait for the host: " + ", ".join(waits[:6]))
        stay_parts.append(_part("would-wait", _counted(len(waits), "workload", "workloads") + " would have to stop and wait for this host.",
                                IN_REVIEW, waits[:6], len(waits)))
    if power_plan.get("requires_data_ack") and not allow_single_copy:
        # What a person would have to accept, said as it is. A pod's emptyDir
        # is scratch space every drain deletes - on a k3s host metrics-server
        # and Traefik have one - and is no reason to leave a host unrestarted.
        reasons, parts = [], []
        single = [v["claim"] for v in power_plan.get("volumes") or [] if v.get("risk") in ("unavailable", "single-copy")]
        if single:
            reasons.append("a volume has its only healthy copy on this host: " + ", ".join(single[:4]))
            parts += _copy_parts(power_plan, blockers)
        # Nor are the host's own sockets and devices data: Longhorn's CSI
        # attacher mounts /var/lib/kubelet/plugins/driver.longhorn.io.
        kept = [f"{row['pod']} ({row['source']})" for row in (power_plan.get("maintenance") or {}).get("local_storage") or []
                if MAINTENANCE.on_host(row) and not str(row.get("kind", "")).startswith("emptyDir") and not _system_host_path(row)]
        if kept:
            reasons.append("pods keep data on this host itself: " + ", ".join(kept[:4]))
            parts.append(_part("host-data", _counted(len(kept), "pod keeps", "pods keep") + " data on this host itself.",
                               f"Turning on {SINGLE_COPY_SETTING} accepts this.", kept[:4], len(kept)))
        if power_plan.get("storage_unknown"):
            reasons.append("Longhorn's volumes could not be read")
            # The review stops on the same fact (homestead_power.plan: "The list of
            # volume copies is unavailable"), whatever the setting: said once, there.
            if not any("volume copies is unavailable" in str(b) for b in blockers):
                parts.append(_part("storage-unread", "Longhorn's volumes could not be read.",
                                   "This may clear by itself on the next read."))
        if reasons:
            # Copies can rebuild and a read can answer; data kept on the host won't go by itself.
            (stays if kept else may_clear).append("; ".join(reasons) + " (the settings do not accept that)")
            (stay_parts if kept else clear_parts).extend(parts)
    if stays or may_clear:
        error = (ValueError if stays else DRAIN_BLOCKERS.MayClear)("; ".join(stays + may_clear))
        error.reasons = stay_parts + clear_parts
        raise error
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
