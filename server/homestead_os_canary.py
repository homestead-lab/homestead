"""The first host of an OS update, as a canary (issue #1348, section 5.3).

On a cluster of more than one host, an OS update (homestead_os_rollout) goes
first to one host, the canary: the least loaded one that is not running
Homestead's leader, so a bad update hurts the fewest apps and never Homestead
first. Once it is updated and restarted the run waits, checking it at each
step, and goes on to the other hosts only after SOAK seconds (a day) in which
the checks kept passing and no new degraded or critical alert was raised. A
check that fails stops the run, so the other hosts stay on what they run.

The checks need nothing from CI, as the spec says: the host is Ready, the
pods Homestead depends on there (Longhorn's manager, KubeVirt's handler, the
node probe) are Ready, and nothing on it is crash-looping.
"""
import time

kget = None
alerts = None          # () -> active alert rows (homestead_alerts.active)
SOAK = 24 * 3600
NEEDED = {"longhorn-manager": "Longhorn's manager", "virt-handler": "KubeVirt's handler",
          "homestead-nodeprobe": "the node probe"}


def bind(_kget, _alerts):
    global kget, alerts
    kget, alerts = _kget, _alerts


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


def _active():
    if alerts:
        return alerts()
    import homestead_alerts as ALERTS
    return ALERTS.active()


def new_alerts(since):
    """Degraded or critical alerts raised since the soak began."""
    return [row for row in _active()
            if row.get("severity") in ("degraded", "critical") and float(row.get("since") or 0) >= since]


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
    raised = new_alerts(since)
    if raised:
        return "stop", (f"an alert was raised while the canary soaked: {raised[0].get('title', 'an alert')}. "
                        "The other hosts were left as they are")
    left = SOAK - (now - since)
    if left > 0:
        return "wait", f"{node} is the canary: checks pass; the other hosts follow in {int(left // 3600)} h {int(left % 3600 // 60)} min"
    return "go", f"{node} passed a day as the canary"
