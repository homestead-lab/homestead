"""Maintenance a person declared: reading it, Extend and End (#535 part 2).

The review and the drain are a node-power job (homestead_power_jobs); by the
time anything here runs that job has finished and the event holds what End
needs. These run once, in the copy that took the request, so unlike a job's
resolver they don't need the leader: End claims the release under the history
lock, then acts.
"""
import time
import urllib.error
import urllib.parse

import homestead_history as HISTORY
import homestead_node_actions as NODE_ACTIONS
import homestead_operations as OPS
import homestead_power as POWER
import homestead_power_hold as HOLD
import homestead_power_jobs as POWER_JOBS
import homestead_routes as ROUTER


def _node(source):
    node = source.get("node")
    if not isinstance(node, str) or not node:
        raise ValueError("node is required")
    return node


def _no_maintenance():
    return ROUTER.Reply(409, {"error": "This host has no maintenance to change"})


def _get(request):
    """Read fresh: what a person just changed is what Host actions shows."""
    now = int(time.time())
    event = HISTORY.declared_for(_node({"node": (request.query.get("node") or [""])[0]}), now=now)
    if not event:
        return {"declared": None}
    return {"declared": {"reason": event["reason"], "until": event["until"], "start": event["start"],
                         "state": "ending" if event.get("end_asked") else "open" if event["end"] is None else "ran_out",
                         "left_s": max(0, event["until"] - now) if event["end"] is None else 0}}


def _extend(request):
    node, hours = _node(request.body), POWER_JOBS.declared_hours(request.body.get("hours"))
    now = int(time.time())
    event = HISTORY.declared_for(node, now=now)
    if not event:
        return _no_maintenance()
    try:
        moved = HISTORY.maintenance_extend(event["op"], now + hours * 3600, now=now)
    except ValueError as error:
        return ROUTER.Reply(409, {"error": str(error)})
    ROUTER.forget("uptime")
    return {"ok": True, "until": moved["until"]}


def _draining(node):
    """A power job on this host that hasn't sent yet: taking the cordon off
    under its drain would bring back what it is moving away."""
    return any(i.get("kind") == "node-power" and (i.get("ref") or {}).get("node") == node
               and i.get("status") not in OPS.TERMINAL and (i.get("ref") or {}).get("phase") in POWER.BEFORE_SEND
               for i in OPS.stored())


def _allow_scheduling(node, event):
    """Take off the cordon this maintenance put on, only from the host it was
    for, saying what is true of it now: it may never have been cordoned, or
    someone may have uncordoned it. Returns those words, and whether all went well."""
    try:
        found = POWER_JOBS.kget(f"/api/v1/nodes/{urllib.parse.quote(node, safe='')}")
    except urllib.error.HTTPError as error:
        if error.code == 404:
            return "This host has been replaced or removed since, so its cordon was left alone", True
        return _unread(event, error)
    except Exception as error:
        return _unread(event, error)
    if (found.get("metadata") or {}).get("uid") != event.get("node_uid") and not event.get("cordoned_before"):
        return "This host has been replaced or removed since, so its cordon was left alone", True
    if not (found.get("spec") or {}).get("unschedulable"):
        return "Scheduling was already allowed on it", True
    if event.get("cordoned_before"):
        return "It stays cordoned, as it was before", True
    try:
        NODE_ACTIONS.set_cordon(node, False)
    except Exception as error:
        return f"It is still cordoned: that could not be changed ({str(error)[:120]})", False
    return "Scheduling is allowed on it again", True


def _unread(event, error):
    if event.get("cordoned_before"):
        return "Its cordon was left as it was", True
    return f"It is still cordoned: it could not be read ({str(error)[:120]})", False


def _end(request):
    """Fix the end, then uncordon and start what waited, and only then let the
    maintenance go. All of that is safe to repeat (the cordon is a plain write,
    and restoring checks its marks), so whatever stops it half way, a failed
    write or this copy dying, End stays offered and is asked again. Two at once
    both act, harmlessly."""
    node = _node(request.body)
    event = HISTORY.declared_for(node)
    if not event:
        return _no_maintenance()
    if _draining(node):
        return ROUTER.Reply(409, {"error": "A power job is still draining this host; wait for it, then end the maintenance"})
    event, first = HISTORY.maintenance_end(event["op"])
    ROUTER.forget("uptime")
    detail, ok = _allow_scheduling(node, event)
    started, left, held = [], [], event.get("held") or []
    if held:
        try:
            started, left = HOLD.restore(held, event["op"])
        except Exception as error:
            ok, left = False, [f"{i.get('ns')}/{i.get('name')}" for i in held]
            detail += f". What waited could not be started ({str(error)[:120]}): " + ", ".join(left)
        else:
            # An End before this one may have started them; their marks can't tell that from a change.
            left = left if first else [row.replace("(changed since)", "(already started, or changed since)") for row in left]
            detail += (". Started again: " + ", ".join(started)) if started else ""
            detail += (". Left as they are: " + ", ".join(left)) if left else ""
    if ok:
        try:
            HISTORY.maintenance_release(event["op"])
        except Exception as error:
            ok, detail = False, detail + f". That could not be recorded ({str(error)[:120]})"
    if not ok:
        detail += ". Its maintenance is still not finished: end it again"
    ROUTER.forget("uptime")
    return {"ok": ok, "detail": detail, "started": started, "left": left}


# Its routes and who may use them (homestead_routes.py).
ROUTES = {
    ("GET", "/api/node/maintenance"): ("viewer", _get),
    ("POST", "/api/node/maintenance/extend"): ("admin", _extend),
    ("POST", "/api/node/maintenance/end"): ("admin", _end),
}
