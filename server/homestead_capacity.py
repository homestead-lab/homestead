"""If a host fails, is there room for what it runs? For every host at once.

"If this host goes down" (homestead_impact) says what moves and where it may
go; it does not say whether the hosts left have the room (#347). This takes
each host in turn as failed and places its apps on the others the way the
scheduler would, by what they reserve:

* Apps and VMs that would stop anyway (pinned, on host-local storage,
  hardware only that host has) or that wait for the host to come back are
  left to the impact preview; DaemonSet pods do not move, and nor does a bare
  pod no controller owns. Kubernetes never starts a StatefulSet's pod
  elsewhere while the old one might still run: the preview says whether each
  moves (Longhorn deletes its pod), waits until its pod is deleted, or stops,
  and only those that move need room. The sentence says how many wait (#440).
  A pod the preview does not list - a platform app's - is counted as needing
  room on a host its own node name, selectors, required affinity and its
  taints allow, should it be started elsewhere; one a StatefulSet owns is not.
* The rest are placed in tier order - critical, then normal, then low
  (homestead_priority) - largest first. Like the scheduler, each goes to an
  allowed host with room for it beside everything already there, the one with
  most memory left; only when none has room may it evict pods of a lower tier
  - critical evicts normal and low ones, normal evicts low ones, low evicts
  nothing - on the host where the fewest apps must go, the lowest tier first,
  keeping any that still fit. An evicted pod is counted as waiting, though it
  may find room elsewhere.

Only reservations count. An app that reserves nothing fits anywhere on paper
and uses memory all the same, so the forecast says how many of those move -
the figures are a floor, not a promise. It is a preview, not the scheduler:
it ignores affinity between apps and the order pods actually restart in.
Pods already Pending, with no host, are counted apart: they wait whatever fails.
Counts are of apps, not of their copies.
"""
import homestead_impact as IMPACT
import homestead_place as PLACE
import homestead_pod_resources as RESOURCES
import homestead_priority as PRIORITY
import homestead_routes

ORDER = {"critical": 0, "normal": 1, "low": 2}
TIGHT = 0.9              # a host this full after the move is said to be tight
GIB = 2 ** 30


def _takes_work(node):
    ready = any(c.get("type") == "Ready" and c.get("status") == "True"
                for c in (node.get("status") or {}).get("conditions") or [])
    return ready and not (node.get("spec") or {}).get("unschedulable")


def _live(pod):
    # A terminating pod's room is given back as it goes, and its replacement
    # is a pod of its own.
    return (pod.get("status") or {}).get("phase") not in ("Succeeded", "Failed") \
        and not (pod.get("metadata") or {}).get("deletionTimestamp")


def _daemon(pod):
    return any(o.get("kind") in ("DaemonSet", "Node") for o in (pod.get("metadata") or {}).get("ownerReferences") or [])


def _need(pod, lad=None):
    """What a pod asks for, its tier (for the words), and its priority number
    and whether it may preempt (for the ranking, as the scheduler ranks, #429)."""
    spec = pod.get("spec") or {}
    lad = lad if lad is not None else PRIORITY.ladder()
    number, preempts = PRIORITY.priority_of(spec, lad)
    tier = PRIORITY.tier_of(spec, lad)[0]
    return {"cpu": RESOURCES.pod_request(spec, "cpu"), "mem": RESOURCES.pod_request(spec, "memory"),
            "tier": tier if tier in ORDER else "normal",
            "prio": number if number is not None else lad["default"]["value"], "preempts": preempts}


def _apps(rows):
    """How many apps, not copies: a three-copy app is one."""
    return len({r["id"] for r in rows})


def forecast(failed, nodes, pods, preview, owner):
    """What happens to the room on the other hosts if failed fails.
    preview: homestead_impact.preview for that host; owner(pod) names the
    workload a pod runs for."""
    survivors, objects = {}, {}
    lad = PRIORITY.ladder()
    for node in nodes:
        name = (node.get("metadata") or {}).get("name")
        if name == failed or not _takes_work(node):
            continue
        alloc = (node.get("status") or {}).get("allocatable") or {}
        objects[name] = node
        survivors[name] = {"name": name, "cpu_alloc": RESOURCES.quantity(alloc.get("cpu"), "cpu"),
                           "mem_alloc": RESOURCES.quantity(alloc.get("memory")), "pods": [], "got": False}
    # Where each app, VM and StatefulSet may go, from the impact preview. Rows
    # that stop, wait (for this host, or for a StatefulSet's pod to be
    # deleted) or couldn't be checked need no room elsewhere.
    # Keyed by kind as well, so a Deployment and a StatefulSet of one name
    # each find their own row.
    listed = {(r.get("kind") == "set", r.get("ns"), r.get("name")): r for r in preview.get("apps") or []
              if r.get("kind") in ("app", "vm", "set")}
    moving, pending = [], set()
    for pod in pods:
        if not _live(pod):
            continue
        meta, spec = pod.get("metadata") or {}, pod.get("spec") or {}
        where = spec.get("nodeName")
        app_id = f"{meta.get('namespace')}/{owner(pod) or meta.get('name')}"
        if not where:
            if (pod.get("status") or {}).get("phase") == "Pending":
                pending.add(app_id)
            continue
        need = {"id": app_id, **_need(pod, lad)}
        if where in survivors:
            survivors[where]["pods"].append(need)
        elif where == failed and not _daemon(pod):
            of_set = any(o.get("kind") == "StatefulSet" for o in meta.get("ownerReferences") or [])
            row = listed.get((of_set, meta.get("namespace"), owner(pod) or meta.get("name")))
            if row is None:
                # A bare pod is not started again anywhere.
                if not meta.get("ownerReferences"):
                    continue
                # Nor is a StatefulSet's, while the old one might still run.
                if of_set:
                    continue
                # As its controller would start it again: the host it ran on is
                # the scheduler's choice, not part of the pod it asks for.
                again = {k: v for k, v in spec.items() if k != "nodeName"}
                moving.append({**need, "to": [h for h in survivors if PLACE.may_run_on(again, objects[h])]})
            elif row.get("outcome") == "moves":
                moving.append({**need, "to": row.get("to") or []})
    moving.sort(key=lambda p: (-p["prio"], -p["mem"], p["id"]))
    before = {h["name"]: sum(p["mem"] for p in h["pods"]) for h in survivors.values()}

    def used(host, pods=None):
        pods = host["pods"] if pods is None else pods
        return sum(p["cpu"] for p in pods), sum(p["mem"] for p in pods)

    def fits(host, pod, pods=None):
        cpu, mem = used(host, pods)
        return cpu + pod["cpu"] <= host["cpu_alloc"] and mem + pod["mem"] <= host["mem_alloc"]

    def preempt(host, pod):
        """The pods pod would evict from host to fit, or None: those with a
        lower priority number, lowest first, keeping back any that still fit after."""
        lower = [p for p in host["pods"] if p["prio"] < pod["prio"]]
        kept = [p for p in host["pods"] if p["prio"] >= pod["prio"]]
        if not fits(host, pod, kept):
            return None
        victims = []
        for other in sorted(lower, key=lambda p: (-p["prio"], -p["mem"])):
            if fits(host, pod, kept + [other]):
                kept.append(other)
            else:
                victims.append(other)
        return victims

    def place(host, pod):
        host["pods"].append(pod)
        host["got"] = True

    waits, evicted = [], []
    for pod in moving:
        allowed = [survivors[h] for h in pod["to"] if h in survivors]
        room = sorted((h for h in allowed if fits(h, pod)), key=lambda h: h["mem_alloc"] - used(h)[1], reverse=True)
        if room:
            place(room[0], pod)
            continue
        options = [] if not pod["preempts"] else [(h, v) for h in allowed for v in [preempt(h, pod)] if v is not None]
        if options:
            # The host where the fewest apps must go, then the least important of them.
            host, victims = min(options, key=lambda o: (_apps(o[1]), max(v["prio"] for v in o[1]) if o[1] else 0,
                                                        -(o[0]["mem_alloc"] - used(o[0])[1])))
            for victim in victims:
                host["pods"].remove(victim)
                evicted.append({**victim, "on": host["name"], "why": f"evicted from {host['name']} to make room for {pod['id']}"})
            place(host, pod)
            continue
        waits.append({**pod, "why": "low tier: it waits for room and evicts nothing" if pod["tier"] == "low" and allowed
                      else "no host it may run on has room, even with lower-tier apps evicted" if allowed
                      else "no host it may run on is taking work"})
    hosts = []
    for h in sorted(survivors.values(), key=lambda h: h["name"]):
        cpu, mem = used(h)
        hosts.append({"name": h["name"], "mem_gib": round(mem / GIB, 1), "mem_alloc_gib": round(h["mem_alloc"] / GIB, 1),
                      "mem_before_gib": round(before[h["name"]] / GIB, 1), "cpu_m": cpu, "cpu_alloc_m": h["cpu_alloc"],
                      "received": h["got"],
                      "full": round(max(mem / h["mem_alloc"] if h["mem_alloc"] else 1, cpu / h["cpu_alloc"] if h["cpu_alloc"] else 1), 3)})
    stuck = waits + evicted
    unreserved = _apps([p for p in moving if not p["mem"] and not p["cpu"]])
    # Tight only where something landed: a small host already full before
    # anything moved is not this failure's doing.
    verdict = ("fits" if not moving else "no-host" if not survivors else "waits" if stuck
               else "tight" if any(h["received"] and h["full"] >= TIGHT for h in hosts) else "fits")
    sets = [r for r in preview.get("apps") or [] if r.get("kind") == "set"]
    # One set to wait for its host waits as a Deployment does: not for deletion.
    waiting = sum(1 for r in sets if r.get("outcome") == "waits" and r.get("why") != IMPACT.FAILOVER_WORDS["wait"][1])
    # Rows that couldn't be checked need no room elsewhere: the sentence says
    # so, never a bare "It fits" (an app's volumes or a VM's devices unread).
    unchecked = sum(1 for r in preview.get("apps") or [] if r.get("outcome") == "unknown" and r.get("kind") in ("set", "app"))
    vms_unchecked = sum(1 for r in preview.get("apps") or [] if r.get("outcome") == "unknown" and r.get("kind") == "vm")
    return {"node": failed, "verdict": verdict, "moving": _apps(moving), "hosts": hosts, "pending": len(pending),
            "waits": _rows(waits), "evicted": _rows(evicted, "on"), "unreserved": unreserved,
            "waiting_for_deletion": waiting,
            "sentence": sentence(failed, verdict, hosts, stuck, len(moving)) + _deletion_clause(waiting, unchecked, "StatefulSets" in (preview.get("unread") or []), vms_unchecked)}


def _rows(pods, *extra):
    """One row per app, with all its copies' memory."""
    out = {}
    for p in pods:
        row = out.setdefault(p["id"], {"id": p["id"], "tier": p["tier"], "why": p["why"], "mem_gib": 0,
                                        **{k: p[k] for k in extra}})
        row["mem_gib"] = round(row["mem_gib"] + p["mem"] / GIB, 2)
    return list(out.values())


def _gib(value):
    return f"{value:g}"


def sentence(failed, verdict, hosts, stuck, moving=1):
    """The forecast in one line, the fullest host first."""
    if not moving:
        return f"If {failed} fails, nothing on it needs room on another host."
    if verdict == "no-host":
        return f"If {failed} fails, no other host takes work: what runs on it waits."
    # The fullest host that something moved to - one already full before is
    # not this failure's doing - or the fullest of all if none did.
    worst = max([h for h in hosts if h.get("received")] or hosts, key=lambda h: h["full"])
    by_cpu = worst["cpu_alloc_m"] and worst["cpu_m"] / worst["cpu_alloc_m"] > (worst["mem_gib"] / worst["mem_alloc_gib"] if worst["mem_alloc_gib"] else 1)
    need = (f"{worst['name']} would have {round(100 * worst['cpu_m'] / worst['cpu_alloc_m'])}% of its CPU reserved" if by_cpu
            else f"{worst['name']} would have {_gib(worst['mem_gib'])} of {_gib(worst['mem_alloc_gib'])} GiB of memory reserved")
    if not stuck:
        return f"If {failed} fails, {need}." + (" Tight, but it fits." if verdict == "tight" else " It fits.")
    tiers = {p["tier"] for p in stuck}
    tier = f"{tiers.pop()}-tier " if len(tiers) == 1 else ""
    evicted = any(p.get("on") for p in stuck)
    count = _apps(stuck)
    return (f"If {failed} fails, {need}; {count} {tier}app{'' if count == 1 else 's'} "
            f"would {'wait for room or be evicted' if evicted else 'wait for room'}.")


def _deletion_clause(waiting, unchecked, unread=False, vms_unchecked=0):
    """What "fits" would otherwise hide: StatefulSets that stay down until
    their pod is deleted, those that couldn't be checked, and all of them
    when they couldn't be read (#440)."""
    parts = []
    if waiting:
        parts.append("1 app waits until its pod is deleted" if waiting == 1
                     else f"{waiting} apps wait until their pod is deleted")
    if unchecked:
        parts.append(f"{unchecked} app{'' if unchecked == 1 else 's'} couldn't be checked")
    if vms_unchecked:
        parts.append(f"{vms_unchecked} VM{'' if vms_unchecked == 1 else 's'} couldn't be checked")
    clause = f" {' and '.join(parts)}; If this host goes down says why." if parts else ""
    return clause + (" StatefulSets couldn't be read, so they aren't counted." if unread else "")


# What every_host reads, bound by server.py: the cluster, a host's impact
# preview (homestead_impact, given the sources it shares), those sources, the
# workload a pod runs for, and the pods that are not apps (Homestead's helpers).
read = impact = sources = owner = None
helper = lambda pod: False


def bind(_read, _impact, _sources, _owner, _helper=None):
    global read, impact, sources, owner, helper
    read, impact, sources, owner = _read, _impact, _sources, _owner
    helper = _helper or (lambda pod: False)


def _ready(node):
    return any(c.get("type") == "Ready" and c.get("status") == "True"
               for c in (node.get("status") or {}).get("conditions") or [])


def every_host():
    """Each ready host taken in turn as failed. The Deployments and VMs every
    preview reads are read once; one host whose preview fails is "unknown"
    and leaves the others standing."""
    nodes = read("/api/v1/nodes").get("items", [])
    pods = [p for p in read("/api/v1/pods").get("items", []) if not helper(p)]
    shared = sources()
    out = []
    for node in nodes:
        name = node["metadata"]["name"]
        if not _ready(node):
            continue
        try:
            out.append(forecast(name, nodes, pods, impact(name, shared), owner))
        except Exception as error:
            out.append({"node": name, "verdict": "unknown", "moving": 0, "hosts": [], "waits": [], "evicted": [],
                        "unreserved": 0, "pending": 0, "waiting_for_deletion": 0,
                        "sentence": f"Could not work out what happens if {name} fails: {str(error)[:120]}"})
    # The cluster's own figures, not the largest of any one host's: every
    # app that reserves nothing, and every app with a pod waiting for a host.
    apps = {}
    for pod in pods:
        meta = pod.get("metadata") or {}
        if not _live(pod) or _daemon(pod) or not (pod.get("spec") or {}).get("nodeName"):
            continue
        need = _need(pod)
        apps.setdefault(f"{meta.get('namespace')}/{owner(pod) or meta.get('name')}", []).append(need["cpu"] or need["mem"])
    return {"hosts": out, "unreserved": sum(1 for reserved in apps.values() if not any(reserved)),
            "pending": max([h.get("pending", 0) for h in out] or [0])}


ROUTES = {
    ("GET", "/api/capacity/forecast"): ("viewer", lambda request: homestead_routes.cached("capacity-forecast", 60, every_host)),
}
