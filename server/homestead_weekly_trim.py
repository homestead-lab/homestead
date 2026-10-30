"""
Homestead's weekly trim (#591): once a week Homestead trims every attached
Longhorn volume that has a filesystem, itself, as a job that says what it
trimmed and what it skipped.

Deleted files leave their blocks allocated in a Longhorn volume until its
filesystem is trimmed. Longhorn's own filesystem-trim RecurringJob tries
every volume in its groups and fails on each detached volume and each VM
disk (a Block volume has no filesystem to trim), so it failed every week
and said nothing useful. Homestead now picks the volumes itself: attached
ones with a filesystem are trimmed, one per step of the job; the others are
skipped and named. The result is kept in DATA_DIR/weekly-trim.json under
"last".
"""
import os
import socket
import time
import urllib.error

import homestead_longhorn as LH
import homestead_longhorn_jobs as LH_JOBS
import homestead_names as NAMES
import homestead_operations as OPS
import homestead_shared as SHARED

KIND = "weekly-trim"
STATE = "weekly-trim.json"
# The default slot, Saturdays 04:00 UTC: Longhorn's TRIM_CRON ("0 4 * * 6"),
# which the RecurringJob Homestead used to make ran on. The weekday (Monday 0)
# and hour can be moved under Housekeeping, saved as "weekday" and "hour" (#927).
SLOT_WEEKDAY, SLOT_HOUR = 5, 4
DAY_NAMES = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
# How the trim is named among the schedules (This week, the clashes).
SCHEDULE_NAME = "Homestead weekly trim"
WEEK = 7 * 86400
VM_DISK = "no filesystem (VM disk)"
DETACHED_ANSWER = "Action [trimFilesystem] not available"
BLOCK_ANSWER = "with Block VolumeMode"
DATA_DIR = None


def _whole(value, top):
    return isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= top


def slot_of(state):
    """(weekday, hour) the trim runs on: the saved ones, else Saturday 04:00."""
    weekday, hour = state.get("weekday"), state.get("hour")
    return (weekday, hour) if _whole(weekday, 6) and _whole(hour, 23) else (SLOT_WEEKDAY, SLOT_HOUR)


def slot_words(state):
    weekday, hour = slot_of(state)
    return f"{DAY_NAMES[weekday]} at {hour:02d}:00 UTC"


def cron(state):
    """The slot as a five-field cron (UTC), for the schedule clashes."""
    weekday, hour = slot_of(state)
    return f"0 {hour} * * {(weekday + 1) % 7}"


def last_slot(now, weekday=SLOT_WEEKDAY, hour=SLOT_HOUR):
    """The most recent slot (weekday, Monday 0, at hour UTC) at or before now."""
    day = int(now) // 86400
    today = (day + 3) % 7                   # 1 January 1970 was a Thursday
    slot = (day - (today - weekday) % 7) * 86400 + hour * 3600
    return slot if slot <= now else slot - WEEK


def _week(moment):
    """The Monday-to-Sunday week (UTC) a moment falls in, counted from 1970."""
    return (int(moment) // 86400 + 3) // 7


def due(state, now):
    """Switched on and not yet run, nor tried, for this week's slot. A slot
    missed while Homestead was down runs once, late. At most one run a week:
    when the last run's slot fell in the same Monday-to-Sunday week (UTC) as
    this slot, the trim has had its week, so moving it to a later day does not
    run it twice. Moving it earlier, or any gap of a week or more, still runs."""
    if state.get("enabled") is False:
        return False
    slot = last_slot(now, *slot_of(state))
    ran = (state.get("last") or {}).get("slot")
    if isinstance(ran, (int, float)) and _week(ran) == _week(slot):
        return False
    return state.get("tried") != slot


def _claim(volume):
    """The volume's claim as people know it, namespace/name; "" without one."""
    k8s = (volume.get("status") or {}).get("kubernetesStatus") or {}
    pvc = k8s.get("pvcName") or ""
    return f"{k8s['namespace']}/{pvc}" if pvc and k8s.get("namespace") else pvc


def _row(volume, why):
    return {"volume": volume["metadata"]["name"], "pvc": _claim(volume), "why": why}


def plan(volumes, pvs):
    """Which volumes a run trims, and which it skips and why. volumes are
    Longhorn's volume items; pvs maps a PV's name to its volumeMode. A volume
    with no PV (no claim) counts as a filesystem."""
    trim, skipped = [], []
    for volume in sorted(volumes, key=lambda v: v["metadata"]["name"]):
        status = volume.get("status") or {}
        state = status.get("state") or ""
        pv = (status.get("kubernetesStatus") or {}).get("pvName") or volume["metadata"]["name"]
        if state != "attached":
            skipped.append(_row(volume, state or "not attached"))
        elif pvs.get(pv) == "Block":
            skipped.append(_row(volume, VM_DISK))
        else:
            trim.append(volume["metadata"]["name"])
    return {"trim": trim, "skipped": skipped}


def _timed_out(error):
    reason = getattr(error, "reason", None)
    return isinstance(error, (socket.timeout, TimeoutError)) or isinstance(reason, (socket.timeout, TimeoutError))


def classify(error):
    """What Longhorn's answer to one trim means: (outcome, why). A volume
    that detached since the run read the list is skipped, as is a VM disk;
    no answer within the backend call's 60 s is not confirmed, not failed."""
    if error is None:
        return "trimmed", ""
    cause = error if isinstance(error, urllib.error.HTTPError) else error.__cause__
    if isinstance(cause, urllib.error.HTTPError) and cause.code == 404:
        return "skipped", "deleted"             # removed since the run read the list
    message = str(error)
    if DETACHED_ANSWER in message:
        return "skipped", "detached"
    if BLOCK_ANSWER in message:
        return "skipped", VM_DISK
    if _timed_out(error):
        return "unconfirmed", "Longhorn did not answer within 60 s"
    return "failed", message[:300]


def bind(data_dir):
    global DATA_DIR
    DATA_DIR = data_dir


def _path():
    return os.path.join(DATA_DIR, STATE)


def _update(change):
    """Change the saved state in place and write it back; the rest of it
    (created, groups, enabled, retired) is kept. An unreadable file raises,
    and nothing is written over it."""
    state = SHARED.read_json(_path(), {})
    change(state)
    SHARED.write_json(_path(), state, durable=True)
    return state


def start(why="weekly"):
    """Start one weekly trim as a job; one runs at a time."""
    slot = last_slot(time.time(), *slot_of(last_state()))
    operation = OPS.start(KIND, "Weekly trim", {"kind": "Longhorn", "name": "weekly trim"},
                          "/data-protection#housekeeping", {"phase": "plan", "why": why, "slot": slot},
                          "Finding attached volumes", unless=(KIND,))
    _update(lambda state: state.update(tried=slot))
    return operation


def _name(ref, volume):
    return (ref.get("pvcs") or {}).get(volume) or volume


def _volumes_and_modes():
    """Longhorn's volumes, read directly so that an error is one (LH's own
    volume list answers [] when it cannot read them), and each PV's
    volumeMode. Without the PVs a VM disk still reaches Longhorn, whose
    answer classify() reads as a skip."""
    volumes = LH.kget(f"{LH.API}/namespaces/{LH.LHNS}/volumes").get("items", [])
    try:
        pvs = LH.kget("/api/v1/persistentvolumes").get("items", [])
    except Exception:
        pvs = []
    return volumes, {p["metadata"]["name"]: (p.get("spec") or {}).get("volumeMode") or "Filesystem" for p in pvs}


def resolve(item, checkpoint):
    """Move the weekly trim on one step: (status, progress, message). The
    plan step reads the volumes; each later step trims one, records what
    Longhorn said and checkpoints, so a run that a restart or a new leader
    picks up carries on from the next volume (a trim done twice is harmless)."""
    ref = item["ref"]
    if ref.get("phase", "plan") == "plan":
        try:
            volumes, modes = _volumes_and_modes()
        except Exception as error:
            return "failed", 0, f"Longhorn's volumes could not be read: {str(error)[:200]}"
        chosen = plan(volumes, modes)
        pvcs = {v["metadata"]["name"]: _claim(v) for v in volumes}
        ref.update(phase="trim", todo=chosen["trim"], done=[], skipped=chosen["skipped"],
                   pvcs={name: pvc for name, pvc in pvcs.items() if pvc})
        checkpoint(item)
        return "running", 1, f"Trimming {len(chosen['trim'])} attached volumes, one at a time"
    todo, done = ref.get("todo") or [], ref.setdefault("done", [])
    if todo:
        volume = todo[0]
        try:
            LH_JOBS._backend("POST", f"/volumes/{volume}?action=trimFilesystem", {})
            error = None
        except Exception as caught:
            error = caught
        outcome, why = classify(error)
        done.append({"volume": volume, "outcome": outcome, "why": why})
        ref["todo"] = todo[1:]
        checkpoint(item)
        total = len(done) + len(ref["todo"])
        if ref["todo"]:
            return "running", max(1, min(99, round(100 * len(done) / total))), \
                f"Trimmed {_name(ref, volume)} ({len(done)} of {total})" if outcome == "trimmed" else \
                f"{_name(ref, volume)}: {outcome} ({len(done)} of {total})"
    return _finish(ref)


def _count(n, word):
    return f"{n} {word}{'' if n == 1 else 's'}"


def _finish(ref):
    """The run's result: saved as state["last"], and said in one message."""
    row = lambda d: {"volume": d["volume"], "pvc": (ref.get("pvcs") or {}).get(d["volume"], "")}
    done = ref.get("done") or []
    last = {"slot": ref.get("slot"), "at": round(time.time()), "why": ref.get("why", ""),
            "trimmed": [d["volume"] for d in done if d["outcome"] == "trimmed"],
            "skipped": list(ref.get("skipped") or []) + [{**row(d), "why": d["why"]} for d in done
                                                          if d["outcome"] == "skipped"],
            "unconfirmed": [{**row(d), "why": d["why"]} for d in done if d["outcome"] == "unconfirmed"],
            "failed": [{**row(d), "message": d["why"]} for d in done if d["outcome"] == "failed"]}
    _update(lambda state: state.update(last=last))
    name = lambda r: r["pvc"] or r["volume"]
    whys = {}
    for s in last["skipped"]:
        whys[s["why"]] = whys.get(s["why"], 0) + 1
    said = ", ".join(_count(n, "VM disk") if why == VM_DISK else f"{n} {why}" for why, n in whys.items())
    message = (f"Trimmed {_count(len(last['trimmed']), 'volume')}, skipped {len(last['skipped'])}"
               + (f" ({said})" if said else ""))
    # Failures first: the job's message is cut at 500 characters, the saved result is not.
    if last["failed"]:
        message += ". Failed: " + "; ".join(f"{name(f)}: {f['message']}" for f in last["failed"])
    if last["unconfirmed"]:
        message += ". Not confirmed: " + ", ".join(f"{name(u)} ({u['why']})" for u in last["unconfirmed"])
    if last["skipped"]:
        message += ". Skipped: " + ", ".join(name(s) for s in last["skipped"])
    return ("failed" if last["failed"] else "succeeded"), 100, message


def _as_homestead_left_it(job, state):
    """Homestead's old trim job, exactly as Homestead last wrote it: its
    schedule, its groups and its label. Changed in any of these, it is the
    user's (state["groups"] is None once someone chose its groups)."""
    spec, labels = job.get("spec") or {}, (job.get("metadata") or {}).get("labels") or {}
    if labels.get(NAMES.key("managed")) != "true" or spec.get("task") != "filesystem-trim" \
            or spec.get("cron") != LH_JOBS.TRIM_CRON:
        return False
    if "groups" not in state:
        return True
    return state["groups"] is not None and sorted(spec.get("groups") or []) == sorted(state["groups"])


def _retire(jobs, state):
    """Once: remove the Longhorn trim job Homestead made, unless someone
    changed it (theirs, so Homestead's own run stays off) or deleted it
    (they did not want a trim, so it stays off too). Returns (what changed,
    whether it was removed)."""
    ours = next((j for j in jobs if j["metadata"]["name"] == LH_JOBS.TRIM_JOB), None)
    removed = f"Longhorn trim job {LH_JOBS.TRIM_JOB} removed: Homestead trims attached volumes itself"
    if ours is None and state.get("retiring"):
        state.update(retired=True, enabled=True)    # deleted by an earlier check that did not finish
        return removed, True
    if ours is not None and _as_homestead_left_it(ours, state):
        # Said before the delete: if Homestead stops before it is saved, the
        # job missing next time is still ours to have removed, not theirs.
        _update(lambda saved: saved.update(retiring=True))
        try:
            LH_JOBS.delete_job(LH_JOBS.TRIM_JOB)
        except urllib.error.HTTPError as error:
            if error.code != 404:
                raise
        state.update(retired=True, enabled=True)
        return removed, True
    if ours is not None:
        state.update(retired=True, enabled=False)
        return f"Longhorn trim job {LH_JOBS.TRIM_JOB} was changed, so it is kept as yours; Homestead's own trim is off", False
    state.update(retired=True, enabled=False if state.get("created") else state.get("enabled", True))
    return "", False


def theirs(jobs):
    """A trim job of the user's that covers the default group: while one is
    there Homestead does not trim as well."""
    return next((j["metadata"]["name"] for j in jobs if (j.get("spec") or {}).get("task") == "filesystem-trim"
                 and "default" in ((j.get("spec") or {}).get("groups") or [])), "")


def tick(now, operations):
    """On the leader, every ten minutes: retire the old trim job once, then
    start this week's run when it is due. Returns what changed, or ""."""
    try:
        jobs = LH.kget(f"{LH.API}/namespaces/{LH.LHNS}/recurringjobs").get("items", [])
        state = SHARED.read_json(_path(), {})
    except Exception as error:
        return f"weekly trim not checked: {str(error)[:160]}"
    said = []
    # Nothing here may stop the rest of the leader's ten-minute checks.
    try:
        if not state.get("retired"):
            change, removed = _retire(jobs, state)
            state.pop("retiring", None)
            # The first run is the next slot's, not one on the day of the update.
            state.setdefault("tried", last_slot(now, *slot_of(state)))
            SHARED.write_json(_path(), state, durable=True)
            said.append(change)
            if removed:
                jobs = [j for j in jobs if j["metadata"]["name"] != LH_JOBS.TRIM_JOB]
        busy = any(o.get("kind") == KIND and o.get("status") in ("queued", "running") for o in operations)
        if due(state, now) and not busy and not theirs(jobs):
            start("weekly")
            said.append("weekly trim started")
    except Exception as error:
        return "; ".join([s for s in said if s] + [f"weekly trim not checked: {str(error)[:160]}"])
    return "; ".join(s for s in said if s)


def _running():
    return any(o.get("kind") == KIND and o.get("status") in ("queued", "running") for o in OPS.snapshot())


def view():
    """Data protection's Housekeeping card: on or off, the next run, the
    last result, and a trim job of the user's that keeps ours from running."""
    state = SHARED.read_json(_path(), {})
    enabled = state.get("enabled", True) is not False
    try:
        mine = theirs(LH.kget(f"{LH.API}/namespaces/{LH.LHNS}/recurringjobs").get("items", []))
    except Exception:
        mine = ""
    weekday, hour = slot_of(state)
    upcoming = last_slot(time.time(), weekday, hour) + WEEK
    ran = (state.get("last") or {}).get("slot")
    if isinstance(ran, (int, float)) and _week(ran) == _week(upcoming):
        upcoming += WEEK                    # that week has had its run (due())
    return {"enabled": enabled, "next": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(upcoming)) if enabled else "",
            "last": state.get("last"), "running": _running(), "theirs": mine, "weekday": weekday, "hour": hour}


def run_now():
    """Trim every attached volume now; one run at a time."""
    if _running():
        raise ValueError("a weekly trim is already running; one runs at a time")
    return {"ok": True, "operation": start("Run now")}


def settings(body):
    """Homestead's weekly trim on or off ("enabled"), and its slot moved
    ("weekday", Monday 0 to Sunday 6, with "hour", 0 to 23, both UTC). A
    moved slot that has already passed this week runs next week, not at once."""
    body = body if isinstance(body, dict) else {}
    moving = "weekday" in body or "hour" in body
    if not moving and "enabled" not in body:
        raise ValueError("nothing to change: send enabled, or weekday and hour")
    if "enabled" in body and not isinstance(body["enabled"], bool):
        raise ValueError("enabled must be true or false")
    weekday, hour = body.get("weekday"), body.get("hour")
    if moving and not (_whole(weekday, 6) and _whole(hour, 23)):
        raise ValueError("weekday must be 0 (Monday) to 6 (Sunday) and hour 0 to 23, both in UTC")

    def change(state):
        if "enabled" in body:
            state["enabled"] = body["enabled"]
        if moving:
            if (weekday, hour) != slot_of(state):
                state["tried"] = last_slot(time.time(), weekday, hour)
            state.update(weekday=weekday, hour=hour)
    state = _update(change)
    enabled = state.get("enabled", True) is not False
    weekday, hour = slot_of(state)
    return {"ok": True, "enabled": enabled, "weekday": weekday, "hour": hour,
            "detail": f"Attached volumes are trimmed every {slot_words(state)}" if enabled
            else "The weekly trim is off; Run now still trims every attached volume"}


def switch(enabled):
    """Homestead's weekly trim on or off."""
    if not isinstance(enabled, bool):
        raise ValueError("enabled must be true or false")
    return settings({"enabled": enabled})


def schedule(volumes, jobs):
    """The weekly trim as a job among the schedules (This week, the clashes,
    #927), or None while it does not run: switched off, or a trim job of the
    user's covers the default group. volumes are Longhorn's volume rows,
    jobs its recurring job rows. It trims the volumes attached now that have
    a filesystem (a PV is matched by the volume's name, as Longhorn names the
    PVs it provisions); PVs that cannot be read count every attached one, as a
    run does. A state file that cannot be read reads as the default: on, at
    Saturday 04:00, as last_state() answers for the Dashboard."""
    state = last_state()
    if state.get("enabled") is False or any(j.get("task") == "filesystem-trim" and "default" in (j.get("groups") or [])
                                            for j in jobs):
        return None
    try:
        pvs = LH.kget("/api/v1/persistentvolumes").get("items", [])
    except Exception:
        pvs = []
    block = {p["metadata"]["name"] for p in pvs if (p.get("spec") or {}).get("volumeMode") == "Block"}
    return {"name": SCHEDULE_NAME, "task": "filesystem-trim", "cron": cron(state), "homestead": True,
            "volumes": sorted(v["name"] for v in volumes if v.get("state") == "attached" and v["name"] not in block)}


# Its routes and who may use them (homestead_routes.py): trimming is an
# operator's, as Trim now is; switching it off or moving it is an admin's.
ROUTES = {
    ("GET", "/api/weekly-trim"): ("viewer", lambda request: view()),
    ("POST", "/api/weekly-trim/run"): ("operator", lambda request: run_now()),
    ("POST", "/api/weekly-trim/settings"): ("admin", lambda request: settings(request.body)),
}


def last_state(strict=False):
    """The saved state: {} before the first run. strict, for the Dashboard's
    health: a file that cannot be read raises SHARED.Unreadable, so a failed
    trim is not taken for one that worked. Otherwise that is {} too."""
    try:
        return SHARED.read_json(_path(), {})
    except Exception:
        if strict:
            raise
        return {}


def issues(state):
    """A health issue for each attached volume the last run failed to trim
    (#749). Skipped and unconfirmed volumes raise nothing, and nothing is
    raised while the weekly trim is off. The next run that trims the volume
    clears it."""
    if state.get("enabled") is False:
        return []
    out = []
    for failed in (state.get("last") or {}).get("failed") or []:
        name = failed.get("pvc") or failed.get("volume") or "a volume"
        out.append({"severity": "degraded", "kind": "Trim", "name": name,
                    "reason": f"The weekly trim of {name} failed: {failed.get('message') or 'no reason given'}. "
                              f"It is tried again next {DAY_NAMES[slot_of(state)[0]]}; "
                              "Trim now on its snapshot panel tries it now."})
    return out


def register(ops):
    """The job store's entry for the weekly trim (#622)."""
    ops.RESOLVERS[KIND] = lambda item: resolve(item, ops.checkpoint)
