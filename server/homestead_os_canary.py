"""The first host of an OS update, as a canary (issue #1348, section 5.3).

On a cluster of more than one host, an OS update (homestead_os_rollout) goes
first to one host, the canary: the least loaded one that is not running
Homestead's leader, so a bad update hurts the fewest apps and never Homestead
first. Once it is updated and restarted the run waits, checking it at each
step, and goes on to the other hosts only after SOAK seconds (a day) in which
the checks kept passing and no degraded or critical alert was raised or got
worse. A check that fails stops the run, so the other hosts stay on what they
run.

"Raised" is what the bell, push and the alert webhook were told: the alert
log (#1526). A condition still inside its hold was told to nobody, and one
Homestead puts right within it (a Service address it records again) is not
news; at the end of the day the run waits for such a condition to clear or
be raised, for at most SETTLE.

The checks need nothing from CI, as the spec says: the host is Ready, the
pods Homestead depends on there (Longhorn's manager, KubeVirt's handler, the
node probe) are Ready, and nothing on it is crash-looping.
"""
import time

kget = None
alerts = None          # () -> the alert log's entries, oldest first (homestead_alerts.log)
held = None            # () -> the alert store's rows (homestead_alerts.active)
SOAK = 24 * 3600
# How long after the day the run waits for a held condition: twice the
# longest hold a condition asks for (homestead_alerts.JOB_STALLED, 30 min).
# One that has neither cleared nor been raised by then never settles: its
# source could not be read, or it keeps coming back within the store's grace.
SETTLE = 3600
SERIOUS = ("degraded", "critical")
# The OS update's own alerts (homestead_os_rollout._alert) never stop it.
OWN = "os-update-"
NEEDED = {"longhorn-manager": "Longhorn's manager", "virt-handler": "KubeVirt's handler",
          "homestead-nodeprobe": "the node probe"}


def bind(_kget, _alerts, _held=None):
    global kget, alerts, held
    kget, alerts, held = _kget, _alerts, _held


def _pods(node):
    path = f"/api/v1/pods?fieldSelector=spec.nodeName%3D{node}"
    return (kget(path) or {}).get("items", [])


def pick(names, own):
    """The canary: the host with the fewest pods that is not `own` (the
    leader's), or None when there is no second host to keep it from."""
    candidates = [name for name in names if name != own]
    if len(names) < 2 or not candidates:
        return None
    try:
        load = {name: len(_pods(name)) for name in candidates}
    except Exception:
        return candidates[0]
    return min(candidates, key=lambda name: (load[name], name))


def checks(node):
    """What is wrong with the canary now, or "" when the checks pass."""
    item = kget(f"/api/v1/nodes/{node}") or {}
    if not any(c.get("type") == "Ready" and c.get("status") == "True"
               for c in (item.get("status") or {}).get("conditions") or []):
        return f"{node} is not Ready"
    pods = _pods(node)
    for prefix, what in NEEDED.items():
        found = [p for p in pods if p["metadata"]["name"].startswith(prefix)]
        if found and not any(c.get("type") == "Ready" and c.get("status") == "True"
                             for p in found for c in (p.get("status") or {}).get("conditions") or []):
            return f"{what} on {node} is not Ready"
    looping = sorted(p["metadata"]["name"] for p in pods for s in (p.get("status") or {}).get("containerStatuses") or []
                     if ((s.get("state") or {}).get("waiting") or {}).get("reason") == "CrashLoopBackOff")
    if looping:
        return f"{', '.join(looping[:3])} on {node} keep{'s' if len(looping) == 1 else ''} crashing"
    return ""


def _log():
    if alerts:
        return alerts()
    import homestead_alerts as ALERTS
    return ALERTS.log(limit=ALERTS.LOG_SIZE, strict=True)["alerts"]


def _active(now):
    if held:
        return held()
    import homestead_alerts as ALERTS
    return ALERTS.active(now=now)


def new_alerts(since, entries=None):
    """Degraded or critical conditions raised, or grown worse, since the
    soak began, from the alert log: also one that began before the soak, and
    one raised and cleared between two looks. Events (a failed job, the OS
    update's own notices) are not counted, as they were not before #1526.
    The log keeps whole seconds."""
    return [e for e in (_log() if entries is None else entries)
            if e.get("phase") in ("raised", "worsened") and e.get("severity") in SERIOUS
            and not e.get("event") and not str(e.get("key") or "").startswith(OWN)
            and int(e.get("at") or 0) >= int(since)]


def _complete(canary, entries):
    """Whether the log still holds every entry since the soak began. The
    soak keeps the last entry the canary read ("log_read", saved with the OS
    update's state, so a restart or a leader change keeps it too): that
    entry is still there unchanged, or the log now starts right after it.
    Before the first read (also a soak a previous release began) the log's
    oldest entry must be from before the soak. Not so when entries fell off
    its end (LOG_SIZE) unread, or the log was lost and begun again."""
    read = canary.get("log_read")
    if isinstance(read, list) and len(read) == 2:
        same = [e for e in entries if e.get("id") == read[0]]
        if same:
            return same[0].get("at") == read[1]
        if not entries:
            return read[0] == 0       # empty then and now; once it had entries, a lost log
        return entries[0].get("id") == read[0] + 1 and int(entries[0].get("at") or 0) >= int(read[1] or 0)
    return not entries or int(entries[0].get("at") or 0) < int(canary["since"])


def _remember(canary, entries):
    last = entries[-1] if entries else {}
    if isinstance(last.get("id"), int) or not entries:
        canary["log_read"] = [last.get("id", 0), last.get("at")]


def pending(since, now=None):
    """Degraded or critical conditions that began in the soak and are still
    in their hold: not news yet, but the canary does not pass while one may
    become news."""
    return [row for row in _active(time.time() if now is None else now)
            if row.get("announced", 0) == 0 and not row.get("event")
            and row.get("severity") in SERIOUS and float(row.get("since") or 0) >= since]


def watch(canary, now=None):
    """One look at the soaking canary: ("wait", message), ("go", message) or
    ("stop", why)."""
    now = time.time() if now is None else now
    node, since = canary["name"], float(canary["since"])
    try:
        wrong = checks(node)
    except Exception as error:
        return "wait", f"{node}: checking the canary again ({str(error)[:80]})"
    if wrong:
        return "stop", f"the canary failed its checks: {wrong}. The other hosts were left as they are"
    try:
        entries = _log()
    except Exception as error:
        return "wait", f"{node}: reading the alert log again ({str(error)[:80]})"
    raised = new_alerts(since, entries)
    if raised:
        first = raised[0]
        title = str(first.get("title") or "an alert")
        what = "an alert was raised"
        if first.get("phase") == "worsened":
            what, title = "an alert got worse", title.removeprefix("Worsened: ")
        return "stop", (f"{what} while the canary soaked: {title} (see the Timeline). "
                        "The other hosts were left as they are")
    if not _complete(canary, entries):
        return "stop", ("the alert log no longer goes back to when the canary's day began, so Homestead "
                        "cannot tell whether an alert was raised in it. The other hosts were left as they are")
    _remember(canary, entries)
    left = SOAK - (now - since)
    if left > 0:
        return "wait", f"{node} is the canary: checks pass; the other hosts follow in {int(left // 3600)} h {int(left % 3600 // 60)} min"
    waiting = pending(since, now)
    if waiting:
        row = waiting[0]
        title = row.get("title") or "a condition"
        if -left >= SETTLE:
            unread = " Homestead could not check it for over an hour." if row.get("stale") else ""
            return "stop", (f"{title} neither cleared nor was raised as an alert within {SETTLE // 3600} h "
                            f"after the canary's day.{unread} The other hosts were left as they are")
        return "wait", (f"{node} is the canary: a day passed. Waiting up to {SETTLE // 3600} h for "
                        f"{title} to clear or to be raised as an alert")
    return "go", f"{node} passed a day as the canary"
