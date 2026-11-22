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
be raised.

The checks need nothing from CI, as the spec says: the host is Ready, the
pods Homestead depends on there (Longhorn's manager, KubeVirt's handler, the
node probe) are Ready, and nothing on it is crash-looping.
"""
import time

kget = None
alerts = None          # () -> the alert log's entries, oldest first (homestead_alerts.log)
held = None            # () -> the alert store's rows (homestead_alerts.active)
SOAK = 24 * 3600
SERIOUS = ("degraded", "critical")
# The OS update's own alerts (homestead_os_rollout._alert) never stop it.
OWN = "os-update-"
# The newest log entry this process has read, for the soak it read it for:
# whether entries since fell off the log's end unread. Not bound: a new
# process (a restart, a leader change) starts without it.
_seen = {}
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


def _active():
    if held:
        return held()
    import homestead_alerts as ALERTS
    return ALERTS.active()


def new_alerts(since, entries=None):
    """Degraded or critical alerts raised, or grown worse, since the soak
    began, from the alert log: also one that began before the soak, and one
    raised and cleared between two looks."""
    return [e for e in (_log() if entries is None else entries)
            if e.get("phase") in ("raised", "worsened") and e.get("severity") in SERIOUS
            and not str(e.get("key") or "").startswith(OWN) and float(e.get("at") or 0) >= since]


def _complete(canary, entries):
    """Whether the log still holds every entry since the soak began. With
    what this process read last for this soak: that entry is still there
    unchanged, or the log now starts right after it. Without (a restart, a
    leader change): the log's oldest entry is from before the soak, or is
    its first ever. Not so when entries fell off its end (LOG_SIZE) before
    anyone read them, or the log was lost and begun again."""
    seen = _seen.get((canary["name"], float(canary["since"])))
    if seen:
        same = [e for e in entries if e.get("id") == seen[0]]
        if same:
            return same[0].get("at") == seen[1]
        return bool(entries) and entries[0].get("id") == seen[0] + 1 and float(entries[0].get("at") or 0) >= seen[1]
    return not entries or float(entries[0].get("at") or 0) <= float(canary["since"]) or entries[0].get("id") == 1


def _remember(canary, entries):
    _seen.clear()
    if entries and isinstance(entries[-1].get("id"), int):
        _seen[(canary["name"], float(canary["since"]))] = (entries[-1].get("id"), entries[-1].get("at"))


def pending(since):
    """Degraded or critical conditions that began in the soak and are still
    in their hold: not news yet, but the canary does not pass while one may
    become news."""
    return [row for row in _active()
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
        return "stop", (f"{what} while the canary soaked: {title} (see All alerts, under the bell). "
                        "The other hosts were left as they are")
    if not _complete(canary, entries):
        return "stop", ("the alert log no longer goes back to when the canary's day began, so Homestead "
                        "cannot tell whether an alert was raised in it. The other hosts were left as they are")
    _remember(canary, entries)
    left = SOAK - (now - since)
    if left > 0:
        return "wait", f"{node} is the canary: checks pass; the other hosts follow in {int(left // 3600)} h {int(left % 3600 // 60)} min"
    waiting = pending(since)
    if waiting:
        return "wait", (f"{node} is the canary: a day passed; waiting to see whether "
                        f"{waiting[0].get('title', 'a condition')} clears or is raised as an alert")
    return "go", f"{node} passed a day as the canary"
