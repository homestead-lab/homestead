"""Moving containers so hosts carry similar CPU and memory.

A host can end up running most of the busy apps - after a reboot drained it
and everything came back elsewhere, or as apps were added. Moving a
container restarts it, so the plan makes the fewest moves that bring the
busiest host down, each one worth its restart, and each can be vetoed.

What can move is what the scheduler could already place elsewhere: a
Deployment with one copy, not Homestead itself, not pinned to a host, not
on host-local storage, and only to hosts its hardware allows (the same
check a manual move uses). Its data stays near it: an app whose volume
keeps its only copy on its host (strict-local) stays there, and one that
reads its volumes locally now - or whose volumes follow it, best-effort,
which would build a whole new copy - moves only to a host that already
holds a copy of each. Its placement is kept too: an app set to run
with others, or one others run with, is not moved alone - the scheduler
would keep it beside them and the move would never come up - and nothing
goes to a host it is kept apart from. A host that already holds a copy of its volumes
is preferred, so its reads stay local. When the one thing in the way is an
app kept apart from it, that one may move first, to another host it may run
on, if the two moves together are worth their restarts; and every app that
could move but does not is listed with why.

Standby hosts (avoid) are moved off first and never moved onto: an app on
one is there because a main host was away, and a main host with room is a
better place for it. Room is the point: a move off a standby host is made
only while its new host stays under SPARE_ROOM, so emptying the spare never
overloads the others - what does not fit stays on it.

The job moves one container at a time with the same capacity check and
placement as a manual move, waits until it runs on its new host, and stops
- rather than carrying on - if one does not come up, so a problem is looked
at before more restarts follow. Cancelling stops after the current one.
"""
import hashlib
import json
import time
import urllib.error
import urllib.parse

import homestead_capacity_review as CAPACITY_REVIEW
import homestead_host_access as HOSTACCESS
import homestead_kube as KUBE
import homestead_kube_metrics as KUBE_METRICS
import homestead_operations as OPS
import homestead_pod_resources as RESOURCES
import homestead_routes as ROUTER

KIND = "container-rebalance"
MAX_MOVES = 10
GAIN = 3.0          # a move must even the hosts out by this many points (spread, below)
SYSTEM = ("kube-", "cattle-", "harvester-", "longhorn-", "fleet-")
SYSTEM_NS = {"kube-system", "longhorn-system", "kubevirt", "cdi", "system-upgrade", "local"}
LH = "/apis/longhorn.io/v1beta2/namespaces/longhorn-system"

kget = None
own = ("", "")                  # Homestead's namespace and Deployment
requirements = satisfies = None # homestead_place: what a workload needs, whether a host has it
summaries = None                # homestead_place.get_nodes: the host summaries satisfies() reads
apply = None                    # (ns, name, node) -> moves it, after the manual move's capacity check
guard = None                    # (deployment, ns, role) -> raises PermissionError if that role may not change it
operations = None               # homestead_operations: the job is one of its jobs
standby = lambda: ()            # homestead_placement_mode.standby_hosts: never moved onto, emptied first
is_leader = lambda: True


def bind(_kget, _requirements, _satisfies, _apply, _own=("", ""), _is_leader=None, _summaries=None, _guard=None,
         _operations=None, _standby=None):
    global kget, requirements, satisfies, apply, own, is_leader, summaries, guard, operations, standby
    kget, requirements, satisfies, apply, own = _kget, _requirements, _satisfies, _apply, tuple(_own)
    summaries, guard, operations = _summaries, _guard, _operations
    standby = _standby or (lambda: ())
    if _is_leader:
        is_leader = _is_leader


def cpu(value):
    """Kubernetes CPU quantity in millicores."""
    text = str(value or "0")
    if text.endswith("n"):
        return int(text[:-1]) / 1e6
    if text.endswith("u"):
        return int(text[:-1]) / 1e3
    if text.endswith("m"):
        return float(text[:-1])
    return float(text) * 1000


def mem(value):
    """Kubernetes memory quantity in bytes."""
    text = str(value or "0")
    units = {"Ki": 1024, "Mi": 1024 ** 2, "Gi": 1024 ** 3, "Ti": 1024 ** 4, "k": 1e3, "M": 1e6, "G": 1e9, "T": 1e12}
    for unit, factor in units.items():
        if text.endswith(unit):
            return float(text[:-len(unit)]) * factor
    return float(text)


def _items(path):
    """The rows, or none when the list is not there (404) or has no "items"
    key. Tolerant on purpose: making it strict is #620's."""
    answered = []   # what kget answered: still empty after a 404

    def read(where):
        answered.append(kget(where))
        return answered[0]
    KUBE.get_optional(path, read=read)
    # A null answer is not a 404: .get fails on it, as before, so nothing
    # acts on it as an empty list.
    return answered[0].get("items", []) if answered else []


def _deployment_of(pod, sets):
    """The Deployment a pod's ReplicaSet belongs to, by owner kind."""
    meta = pod.get("metadata") or {}
    rs = next((o for o in meta.get("ownerReferences") or [] if o.get("kind") == "ReplicaSet"), None)
    owner = ((sets.get((meta.get("namespace"), (rs or {}).get("name"))) or {}).get("metadata") or {}).get("ownerReferences") or []
    return next((o.get("name") for o in owner if o.get("kind") == "Deployment"), None)


def _selector(dep):
    labels = ((dep.get("spec") or {}).get("selector") or {}).get("matchLabels") or {}
    return ",".join(f"{k}={v}" for k, v in sorted(labels.items()))


DEVICE_PATHS = ("/dev", "/sys", "/proc", "/run", "/var/run", "/etc/localtime")


def _host_storage(volume):
    """A host path holding data. Devices - /dev/dri, a Coral, /dev/net/tun -
    are hardware, which the placement check already matches to hosts."""
    path = (volume.get("hostPath") or {}).get("path") or ""
    return bool(path) and not any(path == p or path.startswith(p + "/") for p in DEVICE_PATHS)


def _ready(node):
    return any(c.get("type") == "Ready" and c.get("status") == "True" for c in (node.get("status") or {}).get("conditions") or [])


ADMIN_ONLY = "only an admin can move it"


def _allowed(dep, ns, role):
    """Whether role may change dep, as a manual move would ask; no guard or
    no role (an admin, or Homestead itself) may change anything here."""
    if guard is None or role is None:
        return True
    try:
        guard(dep, ns, role)
        return True
    except PermissionError:
        return False


def _budget_why(ns, labels, budgets):
    """A disruption budget that allows no restart of this app now. A move
    rewrites the Deployment, which does not ask the Eviction API, so the
    budget is checked here or not at all."""
    for budget in budgets:
        meta, spec = budget.get("metadata") or {}, budget.get("spec") or {}
        selector = spec.get("selector")
        # An empty selector covers every pod in its namespace (policy/v1);
        # no selector at all covers none.
        everything = selector == {} and meta.get("namespace") == ns
        if (everything or selector and _term_matches({"labelSelector": selector}, meta.get("namespace"), ns, labels)) \
                and int((budget.get("status") or {}).get("disruptionsAllowed") or 0) < 1:
            return f"its disruption budget {meta.get('namespace')}/{meta.get('name')} allows no restart now"
    return ""


# metrics-server's states (homestead_kube_metrics), from best to worst.
_WORSE = ("ok", "stale", "waiting", "absent", "unavailable")


def inventory(role=None):
    """Hosts and apps; role, when given, is who the moves are for - an app
    they may not change is listed with why, as one that cannot move is."""
    nodes = _items("/api/v1/nodes")
    # The background reading, with its state, never metrics-server itself (#928).
    node_metrics, node_state = KUBE_METRICS.read("nodes", kget)
    pod_metrics, pod_state = KUBE_METRICS.read("pods", kget)
    # The plan rests on both lists: it says the worse state of the two, and
    # without current or recent app figures every app would read as idle.
    state = max(node_state, pod_state, key=lambda s: _WORSE.index(s["state"]) if s["state"] in _WORSE else 0)
    apps_read = pod_state["state"] in ("ok", "stale")
    usage = {(m.get("metadata") or {}).get("name"): m.get("usage") or {} for m in node_metrics}
    hosts = {}
    for node in nodes:
        name = (node.get("metadata") or {}).get("name")
        alloc = (node.get("status") or {}).get("allocatable") or {}
        hosts[name] = {"name": name, "cpu": cpu((usage.get(name) or {}).get("cpu")), "mem": mem((usage.get(name) or {}).get("memory")),
                       "cpu_total": cpu(alloc.get("cpu")) or 1, "mem_total": mem(alloc.get("memory")) or 1,
                       "takes": _ready(node) and not (node.get("spec") or {}).get("unschedulable"), "node": node,
                       "metrics": name in usage and apps_read, "metrics_state": state}
    pods = _items("/api/v1/pods")
    budgets = _items("/apis/policy/v1/poddisruptionbudgets")
    pod_usage = {}
    for m in pod_metrics:
        meta = m.get("metadata") or {}
        pod_usage[(meta.get("namespace"), meta.get("name"))] = (
            sum(cpu((c.get("usage") or {}).get("cpu")) for c in m.get("containers") or []),
            sum(mem((c.get("usage") or {}).get("memory")) for c in m.get("containers") or []))
    sets = {((r.get("metadata") or {}).get("namespace"), (r.get("metadata") or {}).get("name")): r for r in _items("/apis/apps/v1/replicasets")}
    # Where each Longhorn volume has a copy: a host holding one keeps reads local.
    claims, locality = {}, {}
    for volume in _items(f"{LH}/volumes"):
        k8s = (volume.get("status") or {}).get("kubernetesStatus") or {}
        claims[(k8s.get("namespace"), k8s.get("pvcName"))] = (volume.get("metadata") or {}).get("name")
        locality[(volume.get("metadata") or {}).get("name")] = (volume.get("spec") or {}).get("dataLocality") or "disabled"
    copies = {}
    for replica in _items(f"{LH}/replicas"):
        spec = replica.get("spec") or {}
        if not spec.get("failedAt"):
            copies.setdefault(spec.get("volumeName"), set()).add(spec.get("nodeID"))
    apps = []
    for dep in _items("/apis/apps/v1/deployments"):
        meta, spec = dep.get("metadata") or {}, dep.get("spec") or {}
        ns, name = meta.get("namespace", ""), meta.get("name", "")
        if ns in SYSTEM_NS or ns.startswith(SYSTEM) or (ns, name) == own:
            continue
        mine = [p for p in pods if (p.get("metadata") or {}).get("namespace") == ns
                and (p.get("status") or {}).get("phase") == "Running" and _deployment_of(p, sets) == name]
        if not mine:
            continue
        template = (spec.get("template") or {}).get("spec") or {}
        where = sorted({(p.get("spec") or {}).get("nodeName") for p in mine})
        use = [pod_usage.get((ns, (p.get("metadata") or {}).get("name")), (0, 0)) for p in mine]
        why = ""
        if int(spec.get("replicas") or 0) != 1 or len(mine) != 1:
            why = "it runs more than one copy, which the scheduler spreads itself"
        elif "kubernetes.io/hostname" in (template.get("nodeSelector") or {}):
            why = "it is pinned to its host"
        elif any(_host_storage(v) for v in template.get("volumes") or []):
            why = "it uses storage on its host"
        volumes = [claims.get((ns, (v.get("persistentVolumeClaim") or {}).get("claimName")))
                   for v in template.get("volumes") or [] if v.get("persistentVolumeClaim")]
        modes = {locality.get(v) for v in volumes if v}
        if not why and "strict-local" in modes:
            why = "its volume keeps its only copy on its host (strict-local), so it runs there"
        if not why and not _allowed(dep, ns, role):
            why = ADMIN_ONLY
        if not why:
            why = _budget_why(ns, ((spec.get("template") or {}).get("metadata") or {}).get("labels") or {}, budgets)
        apps.append({"ns": ns, "name": name, "id": f"{ns}/{name}", "host": where[0], "dep": dep,
                     "cpu": sum(u[0] for u in use), "mem": sum(u[1] for u in use), "why": why,
                     "near": set.intersection(*[copies.get(v, set()) for v in volumes]) if volumes and all(volumes) else None,
                     "follows": "best-effort" in modes})
    _placement(apps)
    return hosts, apps


def _pod_terms(template, kind):
    """A pod template's terms of one kind (podAffinity, podAntiAffinity),
    required and preferred alike, that place it by host."""
    affinity = (template.get("affinity") or {}).get(kind) or {}
    terms = list(affinity.get("requiredDuringSchedulingIgnoredDuringExecution") or [])
    terms += [weighted.get("podAffinityTerm") or {} for weighted in affinity.get("preferredDuringSchedulingIgnoredDuringExecution") or []]
    return [term for term in terms if term.get("topologyKey") == "kubernetes.io/hostname"]


def _term_matches(term, term_ns, ns, labels):
    """Whether a pod in ns with labels is one the term (written in term_ns) names."""
    if ns not in (term.get("namespaces") or [term_ns]):
        return False
    selector = term.get("labelSelector") or {}
    match, expressions = selector.get("matchLabels") or {}, selector.get("matchExpressions") or []
    if not match and not expressions:
        return False
    if any(labels.get(key) != value for key, value in match.items()):
        return False
    for expression in expressions:
        key, op, values = expression.get("key"), expression.get("operator"), expression.get("values") or []
        if (op == "In" and labels.get(key) not in values) or (op == "NotIn" and labels.get(key) in values) or \
                (op == "Exists" and key not in labels) or (op == "DoesNotExist" and key in labels):
            return False
    return True


def _placement(apps):
    """Keep each app's placement in the plan: one set to run with others,
    or one others run with, stays where they are; each remembers what it is
    kept apart from, so no move takes it there."""
    for app in apps:
        template = (((app["dep"].get("spec") or {}).get("template") or {}))
        app["labels"] = (template.get("metadata") or {}).get("labels") or {}
        app["with"], app["apart"] = _pod_terms(template.get("spec") or {}, "podAffinity"), _pod_terms(template.get("spec") or {}, "podAntiAffinity")
    followers = {}
    for app in apps:
        partners = [other["id"] for other in apps if other is not app and
                    any(_term_matches(term, app["ns"], other["ns"], other["labels"]) for term in app["with"])]
        if partners and not app["why"]:
            app["why"] = f"it runs with {', '.join(partners[:3])} (its placement), so it is not moved away from them alone"
        for partner in partners:
            followers.setdefault(partner, []).append(app["id"])
    for app in apps:
        if followers.get(app["id"]) and not app["why"]:
            who = followers[app["id"]]
            app["why"] = f"{', '.join(who[:3])} {'runs' if len(who) == 1 else 'run'} with it (their placement), so it is not moved away from {'it' if len(who) == 1 else 'them'}"


def _kept_apart(app, other):
    return (any(_term_matches(term, app["ns"], other["ns"], other["labels"]) for term in app.get("apart") or ())
            or any(_term_matches(term, other["ns"], app["ns"], app["labels"]) for term in other.get("apart") or ()))


def _keeps_local(app):
    """Moves only where a copy of each of its volumes already is: it reads
    them on its host now, or Longhorn would build a new copy wherever it
    went (best-effort)."""
    near = app.get("near")
    return near is not None and (app["host"] in near or app.get("follows"))


def _averages(hosts, load):
    """The cluster's CPU and memory in use, as a share of all it has."""
    return (100 * sum(l[0] for l in load.values()) / max(1, sum(h["cpu_total"] for h in hosts.values())),
            100 * sum(l[1] for l in load.values()) / max(1, sum(h["mem_total"] for h in hosts.values())))


def _spread(hosts, load):
    """How uneven the hosts are, in points: how far each is from the
    cluster's average, CPU and memory each counted on their own (#381).
    Comparing only each host's higher share hid a memory gap behind a CPU
    share on another host, and no move could pass."""
    cpu, mem = _averages(hosts, load)
    return sum((100 * l[0] / hosts[n]["cpu_total"] - cpu) ** 2 + (100 * l[1] / hosts[n]["mem_total"] - mem) ** 2
               for n, l in load.items()) ** 0.5


def _over(hosts, load, n):
    """How far a host is above the cluster's average, on whichever of CPU
    and memory it is furthest - the order hosts are balanced in."""
    cpu, mem = _averages(hosts, load)
    return max(100 * load[n][0] / hosts[n]["cpu_total"] - cpu, 100 * load[n][1] / hosts[n]["mem_total"] - mem)


def _plain(reasons):
    return ", ".join(reasons) or "it cannot run there"


def _blockers(app, n, apps, leaving=()):
    """What runs on host n now that app is kept apart from - apps that are
    leaving it do not count."""
    return [other for other in apps if other is not app and other not in leaving
            and other.get("at", other["host"]) == n and _kept_apart(app, other)]


def _moved_load(load, *steps):
    after = {n: list(l) for n, l in load.items()}
    for app, target in steps:
        at = app.get("at", app["host"])
        after[at][0] -= app["cpu"]; after[at][1] -= app["mem"]
        after[target][0] += app["cpu"]; after[target][1] += app["mem"]
    return after


def _take(app, target, load, moves, moved, note=""):
    at = app.get("at", app["host"])
    load[at][0] -= app["cpu"]; load[at][1] -= app["mem"]
    load[target][0] += app["cpu"]; load[target][1] += app["mem"]
    near = bool(app["near"] and target in app["near"])
    moves.append({"ns": app["ns"], "name": app["name"], "id": app["id"], "from": app["host"], "to": target,
                  "cpu_m": round(app["cpu"]), "mem_gb": round(app["mem"] / 1024 ** 3, 2), "near": near,
                  **({"note": note} if note else {})})
    moved.add(app["id"])
    app["at"] = target


def _free(app, exclude, moved):
    return not app["why"] and app["id"] not in exclude and app["id"] not in moved


def _chained(apps, load, hosts, avoid, exclude, moved, reachable, now, spread):
    """Two moves that are worth it together when neither is alone: an app
    kept apart from the one thing on the host that would even things out
    (Frigate and Plex, say), and that one moved to another host it may run
    on first. Only one app in the way - more would be a reshuffle, not a
    balance."""
    best = None
    for app in apps:
        if not _free(app, exclude, moved) or app.get("at", app["host"]) in avoid:
            continue
        for target in reachable.get(app["id"], ()):
            ahead = _blockers(app, target, apps)
            if len(ahead) != 1 or not _free(ahead[0], exclude, moved):
                continue
            blocker = ahead[0]
            for room in reachable.get(blocker["id"], ()):
                # Its new host: not the one it makes room on, nothing there
                # it is kept apart from once app has left for target.
                if room == target or _blockers(blocker, room, apps, leaving=(app,)) or (
                        room == app.get("at", app["host"]) and _kept_apart(blocker, app)):
                    continue
                gain = now - spread(_moved_load(load, (blocker, room), (app, target)))
                near = sum(bool(a["near"] and t in a["near"]) for a, t in ((blocker, room), (app, target)))
                key = (gain - 2 * (2 - near), near)
                if gain >= GAIN and (best is None or key > best[0]):
                    best = (key, app, target, blocker, room)
    if not best:
        return []
    _, app, target, blocker, room = best
    return [(blocker, room, f"makes room on {target} for {app['id']}, which is kept apart from it"),
            (app, target, f"after {blocker['id']} leaves {target}")]


TINY_CPU, TINY_MEM = 50, 256 * 1024 ** 2   # too small to shift a host's share: listed together


def _not_moved(apps, load, hosts, refused, reachable, exclude, moved, now, spread, full):
    """Why each app that could move does not: no host it may run on, kept
    apart from what is there, or no move of it worth a restart - with the
    numbers, so a person can see how close it came."""
    out, tiny = [], []
    for app in apps:
        if not _free(app, exclude, moved):
            continue
        here = refused.get(app["id"]) or {}
        if not reachable.get(app["id"]):
            hosts_why = "; ".join(f"{n}: {w}" for n, w in sorted(here.items()))
            out.append({"id": app["id"], "why": f"no other host it may run on ({hosts_why})" if hosts_why
                        else "there is no other host"})
            continue
        apart = {n: _blockers(app, n, apps) for n in reachable[app["id"]]}
        open_ = [n for n, b in apart.items() if not b]
        if not open_:
            parts = [f"{n}: kept apart from {', '.join(o['id'] for o in b[:2])} there" for n, b in sorted(apart.items())]
            parts += [f"{n}: {w}" for n, w in sorted(here.items())]
            out.append({"id": app["id"], "why": "; ".join(parts)})
            continue
        if app["cpu"] < TINY_CPU and app["mem"] < TINY_MEM:
            tiny.append(app["id"])
            continue
        if full:
            out.append({"id": app["id"], "why": f"the plan stops at {MAX_MOVES} moves"})
            continue
        n, after = min(((n, spread(_moved_load(load, (app, n)))) for n in open_), key=lambda x: x[1])
        out.append({"id": app["id"], "why": (f"to {n} the hosts would be {after:.0f} points apart, now {now:.0f}"
                                             if after < now else f"any move makes the hosts more uneven ({now:.0f} points apart now)")
                    + f" - a restart needs {GAIN:.0f}"})
    if tiny:
        out.append({"id": f"{len(tiny)} small app{'s' if len(tiny) != 1 else ''}",
                    "why": "each uses too little CPU and memory to shift a host: " + ", ".join(sorted(tiny))})
    return out


SPARE_ROOM = 90.0  # how full a main host may get from apps moved off a standby host


def plan(exclude=(), avoid=(), role=None):
    hosts, apps = inventory(role)
    exclude, avoid = set(exclude or ()), set(avoid or ())
    load = {n: [h["cpu"], h["mem"]] for n, h in hosts.items()}
    # How even the main hosts are, without the standby ones: an empty spare
    # would pull the average down and make every move between the others
    # look worth less than it is.
    main = {n: h for n, h in hosts.items() if n not in avoid} or hosts

    def main_spread(l):
        return _spread(main, {n: l[n] for n in main})
    spread_before = main_spread(load) if hosts else 0
    # satisfies() reads Homestead's own host summary (status, labels,
    # devices), not the Kubernetes Node.
    summary = {n.get("name"): n for n in (summaries() if summaries else [])}
    # Where each app could run but for what runs there now: whether
    # something there is kept apart from it is checked move by move, as
    # apps move - and a chained move can make room.
    reachable, refused = {}, {}
    for app in apps:
        if app["why"]:
            continue
        reqs = requirements(app["dep"])
        refused[app["id"]] = {}
        for n, h in hosts.items():
            if n == app["host"]:
                continue
            ok, reasons = satisfies(summary.get(n, h["node"]), reqs)
            why = ("takes no new containers" if not h["takes"] else "is a standby host" if n in avoid
                   else _plain(reasons) if not ok
                   else "has no copy of its volumes" if _keeps_local(app) and n not in app["near"] else "")
            if why:
                refused[app["id"]][n] = why
        reachable[app["id"]] = {n for n in hosts if n != app["host"] and n not in refused[app["id"]]}
    moves, moved = [], set()
    while len(moves) < MAX_MOVES:
        best = None
        # Standby hosts first, then the busiest; when nothing of one can move, the next.
        now = main_spread(load)
        for busiest in sorted(load, key=lambda n: (n in avoid, _over(main, {k: load[k] for k in main}, n) if n in main else 0),
                              reverse=True):
            for app in apps:
                if app.get("at", app["host"]) != busiest or not _free(app, exclude, moved):
                    continue
                for target in reachable.get(app["id"], ()):
                    if _blockers(app, target, apps):
                        continue
                    after = _moved_load(load, (app, target))
                    if busiest in avoid:
                        # Off a standby host, any move that leaves the new
                        # one room is worth it; the most room left first.
                        full = max(100 * after[target][0] / hosts[target]["cpu_total"],
                                   100 * after[target][1] / hosts[target]["mem_total"])
                        gain, worth = SPARE_ROOM - full, full <= SPARE_ROOM
                    else:
                        gain = now - main_spread(after)
                        worth = gain >= GAIN
                    near = bool(app["near"] and target in app["near"])
                    key = (gain - (0 if near else 2), near)      # a host with its volumes is worth two points
                    if worth and (best is None or key > best[0]):
                        best = (key, app, target, near)
            if best:
                break
        if not best:
            chain = _chained(apps, load, hosts, avoid, exclude, moved, reachable, now, main_spread)                 if len(moves) + 2 <= MAX_MOVES else []
            if not chain:
                break
            # The one in the way first, to a host it may run on; then the app
            # into the room it leaves.
            for app, target, note in chain:
                _take(app, target, load, moves, moved, note)
            continue
        _, app, target, _near = best
        _take(app, target, load, moves, moved)
    shown = {m["id"] for m in moves} | (exclude & {a["id"] for a in apps})
    movable = [a for a in apps if not a["why"] and a["id"] not in exclude and reachable.get(a["id"])]
    # Why nothing moves, when nothing does (#381).
    quiet = ("" if moves else "even" if spread_before < GAIN * 2 else "nothing-movable" if not movable else "small")
    token = review_token(moves, exclude)
    return {"hosts": [{"name": n, "takes": hosts[n]["takes"], "metrics": hosts[n]["metrics"],
                       "cpu_before": round(100 * hosts[n]["cpu"] / hosts[n]["cpu_total"]),
                       "cpu_after": round(100 * load[n][0] / hosts[n]["cpu_total"]),
                       "mem_before": round(100 * hosts[n]["mem"] / hosts[n]["mem_total"]),
                       "mem_after": round(100 * load[n][1] / hosts[n]["mem_total"])} for n in sorted(hosts)],
            "moves": moves, "apps": sorted(shown), "excluded": sorted(exclude), "quiet": quiet,
            "spread_before": round(spread_before, 1), "spread_after": round(main_spread(load), 1) if hosts else 0,
            "skipped": [{"id": a["id"], "why": a["why"]} for a in apps if a["why"]]
                       + _not_moved(apps, load, hosts, refused, reachable, exclude, moved, main_spread(load) if hosts else 0,
                                    main_spread, len(moves) >= MAX_MOVES),
            "metrics": all(h["metrics"] for h in hosts.values()),
            "metrics_state": next((h["metrics_state"] for h in hosts.values()), None), "review_token": token}


def _versions(moves):
    """Each app's uid and generation, as the review binds them: generation
    changes when someone edits the app, not when its status does, so a
    move made minutes into the job is checked against an edit, not noise."""
    out = []
    for m in moves:
        try:
            meta = kget(f"/apis/apps/v1/namespaces/{m['ns']}/deployments/{m['name']}").get("metadata") or {}
        except urllib.error.HTTPError as error:
            if error.code != 404:
                raise
            meta = {}
        out.append([m["id"], meta.get("uid", ""), int(meta.get("generation") or 0)])
    return sorted(out)


def _review_body(moves, exclude):
    return {"moves": [[m["id"], m["from"], m["to"]] for m in moves], "exclude": sorted(exclude or ())}


def review_token(moves, exclude):
    """What was reviewed - which container goes from where to where, what
    was vetoed, and each app as it stood - signed for ten minutes. Not the
    live load figures, which move by the second."""
    return CAPACITY_REVIEW.issue(_review_body(moves, exclude), {"action": "container-moves", "apps": _versions(moves)})


def _token_ok(moves, exclude, token):
    return CAPACITY_REVIEW.valid({**_review_body(moves, exclude), "capacity_token": token},
                                 {"action": "container-moves", "apps": _versions(moves)})


def reviewed(moves, exclude, token, role=None):
    """The moves a person reviewed, checked against the cluster now: each
    container still on the host it was to leave, still movable, and its new
    host still able to take it. Load that changed since does not matter -
    each move gets the manual move's capacity check as it is made."""
    if not isinstance(moves, list) or not moves or len(moves) > MAX_MOVES:
        raise ValueError("Review the containers to move again")
    keys = ("ns", "name", "id", "from", "to")
    if any(not isinstance(m, dict) or not all(isinstance(m.get(k), str) and m.get(k) for k in keys) for m in moves):
        raise ValueError("Review the containers to move again")
    if not _token_ok(moves, exclude, token):
        raise ValueError("The moves differ from the review, it is more than ten minutes old, "
                         "or a container was edited since; review again")
    hosts, apps = inventory(role)
    by_id = {a["id"]: a for a in apps}
    summary = {n.get("name"): n for n in (summaries() if summaries else [])}
    out = []
    for m in moves:
        app, host = by_id.get(m["id"]), hosts.get(m["to"])
        if app and app["why"] == ADMIN_ONLY:
            raise PermissionError(f"{m['id']}: {ADMIN_ONLY}")
        if not app or app["why"] or app["host"] != m["from"] or m["id"] in set(exclude or ()):
            raise ValueError(f"{m['id']} changed since the review; review again")
        if not host or not host["takes"] or not satisfies(summary.get(m["to"], host["node"]), requirements(app["dep"]))[0]:
            raise ValueError(f"{m['to']} cannot take {m['id']} now; review again")
        out.append({**{k: m[k] for k in keys}, "cpu_m": round(app["cpu"]), "mem_gb": round(app["mem"] / 1024 ** 3, 2),
                    "near": bool(app["near"] and m["to"] in app["near"])})
    generations = {row[0]: row[2] for row in _versions(moves)}
    for move in out:
        move["generation"] = generations.get(move["id"], 0)
    return out


GI = 1024 ** 3
GI_MB = 1024 ** 2


def _requests(template):
    """CPU (millicores) and memory (bytes) one copy of the app asks for."""
    return float(RESOURCES.pod_request(template, "cpu")), float(RESOURCES.pod_request(template, "memory"))


def picked(picks, role=None):
    """Moves a person chose - [{id, to}] - each marked with why it can't go,
    and the destinations' requests counted together: what each already
    runs, plus every pick sent there."""
    if not isinstance(picks, list) or not picks or \
            any(not isinstance(p, dict) or not isinstance(p.get("id"), str) or not isinstance(p.get("to"), str) for p in picks):
        raise ValueError("Choose the containers to move, and where to")
    hosts, apps = inventory(role)
    by_id = {a["id"]: a for a in apps}
    summary = {n.get("name"): n for n in (summaries() if summaries else [])}
    used = {n: [0.0, 0.0] for n in hosts}
    for p in _items("/api/v1/pods"):
        where = (p.get("spec") or {}).get("nodeName")
        if where in used and (p.get("status") or {}).get("phase") in ("Running", "Pending"):
            cpu_r, mem_r = _requests(p.get("spec") or {})
            used[where][0] += cpu_r; used[where][1] += mem_r
    rows, seen, going = [], set(), {}
    for pick in picks:
        if pick["id"] in seen:
            continue
        seen.add(pick["id"])
        app, to = by_id.get(pick["id"]), pick["to"]
        if not app:
            rows.append({"id": pick["id"], "ns": "", "name": "", "from": "", "to": to, "why": "it is not running as a single-copy Deployment Homestead can move",
                         "cpu_m": 0, "mem_mb": 0, "reserves": False})
            continue
        template = ((app["dep"].get("spec") or {}).get("template") or {}).get("spec") or {}
        cpu_r, mem_r = _requests(template)
        host = hosts.get(to)
        why = app["why"]
        if not why and (app["dep"].get("spec") or {}).get("paused"):
            why = "its rollouts are paused, so a move would not take effect"
        if not why and template.get("nodeName"):
            why = "it names its host directly (nodeName)"
        if not why and template.get("topologySpreadConstraints"):
            why = "it has spread rules, which decide where it runs"
        if not why and app["host"] == to:
            why = f"it already runs on {to}"
        if not why and (not host or not host["takes"]):
            why = f"{to} takes no new containers"
        if not why:
            fits, reasons = satisfies(summary.get(to, host["node"]), requirements(app["dep"]))
            if not fits:
                why = f"{to} can't run it: {'; '.join(reasons) or 'its hardware or labels'}"
        if not why and any(other["host"] == to and _kept_apart(app, other) for other in apps if other is not app):
            why = f"it is kept apart from an app on {to}"
        if not why and any(_kept_apart(app, by_id[other]) for other in going.get(to, ())):
            why = f"it is kept apart from another container going to {to}"
        if not why and sum(len(v) for v in going.values()) >= MAX_MOVES:
            why = f"at most {MAX_MOVES} at a time"
        if not why:
            going.setdefault(to, []).append(app["id"])
        rows.append({"id": app["id"], "ns": app["ns"], "name": app["name"], "from": app["host"], "to": to, "why": why,
                     "cpu_m": round(cpu_r), "mem_mb": round(mem_r / GI_MB), "reserves": bool(cpu_r or mem_r),
                     "near": bool(app["near"] and to in app["near"])})
    # The destinations, with every pick sent there counted together.
    out_hosts = []
    for name in sorted(going):
        cpu_in = sum(r["cpu_m"] for r in rows if r["to"] == name and not r["why"])
        mem_in = sum(r["mem_mb"] * GI_MB for r in rows if r["to"] == name and not r["why"])
        h = hosts[name]
        cpu_after, mem_after = used[name][0] + cpu_in, used[name][1] + mem_in
        over = cpu_after > h["cpu_total"] or mem_after > h["mem_total"]
        out_hosts.append({"name": name, "eligible": True, "metrics_available": True,
                          "used_gb": round(used[name][1] / GI, 1), "capacity_gb": round(h["mem_total"] / GI, 1),
                          "projected_gb": round(mem_after / GI, 1), "projected_percent": round(100 * mem_after / h["mem_total"]),
                          "cpu_before_m": round(used[name][0]), "cpu_after_m": round(cpu_after), "cpu_total_m": round(h["cpu_total"]),
                          "over": over})
        if over:
            for r in rows:
                if r["to"] == name and not r["why"]:
                    r["why"] = f"no room on {name} with the others (requests {round(cpu_after)}m of {round(h['cpu_total'])}m CPU, " \
                               f"{round(mem_after / GI, 1)} of {round(h['mem_total'] / GI, 1)} GiB)"
    moves = [{"ns": r["ns"], "name": r["name"], "id": r["id"], "from": r["from"], "to": r["to"],
              "cpu_m": r["cpu_m"], "mem_gb": round(r["mem_mb"] / 1024, 2), "near": r["near"]} for r in rows if not r["why"]]
    return {"rows": rows, "moves": moves, "hosts": out_hosts, "blocked": not moves,
            "review_token": review_token(moves, []) if moves else ""}


def _note(ref, move, result, detail=""):
    ref.setdefault("results", []).append({"id": move["id"], "to": move["to"], "result": result, "detail": detail})


def _summary(ref):
    """Each app's result for the job's message - failures first, six named."""
    results = ref.get("results") or []
    order = {"failed": 0, "not reached": 1, "skipped": 2, "moved": 3}
    words = {"moved": "Moved {id} to {to}", "skipped": "Skipped {id}: {detail}", "failed": "{id} failed: {detail}",
             "not reached": "{id} not reached"}
    named = [words[r["result"]].format(**r) + (f" ({r['detail']})" if r["result"] == "moved" and r.get("detail") else "")
             for r in sorted(results, key=lambda r: order[r["result"]])]
    return "; ".join(named[:6]) + (f"; and {len(named) - 6} more" if len(named) > 6 else "")


def _halt(ref, i, move, detail):
    """Moves a person chose stop at the first that does not go: it fails,
    and the rest are not reached."""
    _note(ref, move, "failed", detail)
    for rest in ref["moves"][i + 1:]:
        _note(ref, rest, "not reached")
    return "failed", int(100 * i / max(1, len(ref["moves"]))), _summary(ref)


# --------------------------------------------------------------- the job
def start(plan, why="", role=None, unless=()):
    """The job for a reviewed plan - or one Homestead made itself, in the
    maintenance window after a host came back. role is who started it: each
    move is checked for them again as it is made; None for Homestead's own.
    unless: kinds of job that, while one runs, refuse this one (OPS.start)."""
    moves = plan["moves"]
    verb = "Move" if plan.get("stop_on_failure") else "Rebalance"
    return operations.start(KIND, f"{verb} {len(moves)} container{'' if len(moves) == 1 else 's'}{why}",
                            {"kind": "Deployment", "name": "rebalance"}, "/containers",
                            {"moves": moves, "index": 0, "moved": 0, "stage": "move", "excluded": plan["excluded"], "role": role,
                             "stop_on_failure": bool(plan.get("stop_on_failure")), "results": []},
                            "Starting with the first container", unless=unless)


def _running_on(ns, name, keep=None):
    """(settled, hosts); keep, when given, receives the Deployment read, so
    the checks before a move need not read it again."""
    dep = kget(f"/apis/apps/v1/namespaces/{ns}/deployments/{name}")
    if keep is not None:
        keep["dep"] = dep
    status, spec, meta = dep.get("status") or {}, dep.get("spec") or {}, dep.get("metadata") or {}
    settled = (int(status.get("observedGeneration") or 0) >= int(meta.get("generation") or 0)
               and int(status.get("updatedReplicas") or 0) == int(spec.get("replicas") or 0)
               and int(status.get("readyReplicas") or 0) == int(spec.get("replicas") or 0)
               and int(status.get("replicas") or 0) == int(spec.get("replicas") or 0))
    selector = urllib.parse.quote(_selector(dep), safe="")
    hosts = {(p.get("spec") or {}).get("nodeName") for p in _items(f"/api/v1/namespaces/{ns}/pods?labelSelector={selector}")
             if (p.get("status") or {}).get("phase") == "Running" and not (p.get("metadata") or {}).get("deletionTimestamp")}
    return settled, hosts


def _pull_first(dep, node):
    """Start pulling the app's images the host lacks; ([pulls], [images]).
    A pull that cannot start is not a reason to stop: it moves at once, as
    it always did."""
    import homestead_host_move as HOST_MOVE
    import homestead_image_cache as IMAGES
    try:
        need = HOST_MOVE.missing(dep, node) if dep else []
        return [IMAGES.prepull(image, [node])["daemonset"] for image in need], need
    except Exception:
        return [], []


def status(item):
    ref = item["ref"]
    moves, i, done = ref["moves"], ref.get("index", 0), ref.get("moved", 0)
    if i >= len(moves):
        if ref.get("stop_on_failure"):
            return "succeeded", 100, _summary(ref)
        skipped = ref.get("skipped") or []
        return "succeeded", 100, (f"Moved {done} container{'' if done == 1 else 's'}"
                                  + (f"; left {len(skipped)}: " + "; ".join(skipped[:4]) if skipped else ""))
    move = moves[i]
    progress = int(100 * i / max(1, len(moves)))
    if not is_leader():
        return "running", progress, item.get("message") or f"Moving {move['id']}"
    stage, now = ref.get("stage", "move"), time.time()

    def skip(reason):
        """A move that does not go: a person's own choice stops here, the
        rest not reached; Balance's own plan skips it and carries on."""
        if ref.get("stop_on_failure"):
            return _halt(ref, i, move, reason)
        _note(ref, move, "skipped", reason)
        ref.setdefault("skipped", []).append(f"{move['id']}: {reason}")
        ref.update(index=i + 1)
        return "running", progress, f"Skipped {move['id']}: {reason}"
    if stage == "move":
        seen = {}
        try:
            _, hosts = _running_on(move["ns"], move["name"], seen)
        except urllib.error.HTTPError as error:
            if error.code != 404:
                raise
            hosts = set()
        if hosts != {move["from"]}:
            return skip("it moved or changed since the review")
        dep = seen.get("dep") or {}
        if "generation" in move:
            now_generation = int((dep.get("metadata") or {}).get("generation") or 0) if dep else -1
            if now_generation != move["generation"]:
                return skip("it changed since the review")
        # A budget made or tightened since the review, or used up by an
        # earlier move: a Deployment rewrite never asks the Eviction API.
        labels = (((dep.get("spec") or {}).get("template") or {}).get("metadata") or {}).get("labels") or {}
        budget = _budget_why(move["ns"], labels, [b for b in _items("/apis/policy/v1/poddisruptionbudgets")
                                                  if (b.get("metadata") or {}).get("namespace") == move["ns"]])
        if budget:
            return skip(budget)
        # Its image onto the new host first: a host already holding it
        # scores for that, and can outweigh the preferred placement (#880).
        if ref.get("pulled") != i:
            pulls, need = _pull_first(dep, move["to"])
            ref["pulled"] = i
            if pulls:
                ref.update(stage="pulling", pulls=pulls, since=now)
                return "running", progress, f"Pulling {', '.join(need)} onto {move['to']} first, so {move['id']} lands there"
        # The job runs with no request behind it: asked again for whoever
        # started it, since the app may have changed since the review, and
        # the move written as them, so the fence on Kubernetes writes checks
        # the very object it changes, as it does for a manual move.
        role = ref.get("role")
        try:
            if role not in (None, "admin") and not _allowed(
                    kget(f"/apis/apps/v1/namespaces/{move['ns']}/deployments/{move['name']}"), move["ns"], role):
                raise ValueError(ADMIN_ONLY)
            with HOSTACCESS.acting_as(role):
                apply(move["ns"], move["name"], move["to"])
        except PermissionError:
            return skip(ADMIN_ONLY)
        except ValueError as error:
            return skip(str(error)[:160])
        ref.update(stage="starting", started=now)
        if ref.get("stop_on_failure"):
            moved = [r["id"] for r in ref.get("results") or [] if r["result"] == "moved"]
            return "running", progress, (f"{i + 1} of {len(moves)}: " + (f"moved {', '.join(moved)}; " if moved else "")
                                         + f"moving {move['id']} to {move['to']}")
        return "running", progress, f"Moving {move['id']} to {move['to']} ({i + 1} of {len(moves)}); it restarts there"
    if stage == "pulling":
        import homestead_host_move as HOST_MOVE
        pulls = ref.get("pulls") or []
        if not HOST_MOVE._pulled(pulls) and now - ref.get("since", now) < HOST_MOVE.PULL_WAIT:
            return "running", progress, f"Pulling {move['id']}'s image onto {move['to']} first, so it lands there"
        HOST_MOVE._clear(pulls)
        ref.pop("pulls", None)
        # The checks again, then the move: the pull took a while.
        ref["stage"] = "move"
        return "running", progress, f"Image on {move['to']}; moving {move['id']}"
    if stage == "starting":
        settled, hosts = _running_on(move["ns"], move["name"])
        if settled and hosts and move["from"] not in hosts:
            # Where it landed, when not where it was sent.
            _note(ref, move, "moved", "" if move["to"] in hosts else f"it landed on {', '.join(sorted(hosts))}, not {move['to']}")
            ref.update(index=i + 1, stage="move", moved=done + 1)
            return "running", int(100 * (i + 1) / len(moves)), f"{move['id']} runs on {', '.join(sorted(hosts))}"
        if now - ref.get("started", now) > 600:
            # Stop rather than restart more: one that did not come up is looked at first.
            if ref.get("stop_on_failure"):
                return _halt(ref, i, move, f"did not start on {move['to']} within 10 minutes")
            if settled and hosts == {move["from"]}:
                where = f"{move['id']} restarted on {move['from']}, not {move['to']}"
                if ref.get("stop_on_failure"):
                    return _halt(ref, i, move, where.split(" ", 1)[1])
                return "failed", progress, f"{where}; the rest were not moved. Moved {done} before it"
            return "failed", progress, (f"{move['id']} did not start on {move['to']} within 10 minutes; the rest were not moved. "
                                        f"Moved {done} before it. Check its events in Containers")
        return "running", progress, f"Waiting for {move['id']} to start on {move['to']} ({i + 1} of {len(moves)})"
    return "failed", progress, "Unknown step; the rest were not moved"


def cancel_plan(item):
    return {"mode": "stop", "action": "Stop rebalancing",
            "undo": ["No more containers are moved"],
            "keeps": ["A container already moving finishes its restart on its new host",
                      "Containers already moved stay where they are"], "needs": "operator"}


def cancel_run(work, chosen):
    ref = work.get("ref") or {}
    if ref.get("stop_on_failure"):
        # The one moving now finishes its restart; the rest are not reached.
        first = ref.get("index", 0) + (1 if ref.get("stage") == "starting" else 0)
        for move in ref.get("moves", [])[first:]:
            _note(ref, move, "not reached")
        return "Stopped: " + _summary(ref)
    moved = ref.get("moved", 0)
    return f"Stopped after moving {moved} container{'' if moved == 1 else 's'}"


def start_run(request):
    """The reviewed moves, started as a job: 202 while it runs, 409 when the
    review no longer holds."""
    b = request.body
    running = next((i for i in OPS.snapshot() if i.get("kind") == KIND
                    and i.get("status") not in OPS.TERMINAL), None)
    if running:
        return ROUTER.Reply(409, {"error": "A container rebalance is already running; follow it in Jobs", "operation": running})
    if b.get("restart") is not True:
        return ROUTER.Reply(409, {"error": "Moving a container restarts it; confirm the restarts"})
    role = request.role
    if b.get("picks") is not None:
        try:
            now = picked(b["picks"], role=role)
        except PermissionError as error:
            return ROUTER.Reply(403, {"error": str(error)})
        except ValueError as error:
            return ROUTER.Reply(409, {"error": str(error)})
        if not now["moves"] or not _token_ok(now["moves"], [], b.get("review_token")):
            return ROUTER.Reply(409, {"error": "The containers or hosts changed since the review, or it is more than ten "
                                               "minutes old; review again", "plan": now})
        generations = {row[0]: row[2] for row in _versions(now["moves"])}
        moves = [{**m, "generation": generations.get(m["id"], 0)} for m in now["moves"]]
        return ROUTER.Reply(202, {"operation": start({"moves": moves, "excluded": [], "stop_on_failure": True}, role=role)})
    if b.get("moves") is not None:
        # The moves reviewed, checked against the cluster now -
        # not a plan made again from load that moves by the second.
        try:
            moves = reviewed(b["moves"], b.get("exclude") or [], b.get("review_token"), role=role)
        except PermissionError as error:
            return ROUTER.Reply(403, {"error": str(error)})
        except ValueError as error:
            return ROUTER.Reply(409, {"error": str(error), "plan": plan(b.get("exclude") or [], avoid=standby(), role=role)})
        chosen = {"moves": moves, "excluded": sorted(b.get("exclude") or [])}
    else:
        chosen = plan(b.get("exclude") or [], avoid=standby(), role=role)
        if chosen["review_token"] != b.get("review_token"):
            return ROUTER.Reply(409, {"error": "Container load changed since the review; review again", "plan": chosen})
    if not chosen["moves"]:
        return ROUTER.Reply(409, {"error": "Nothing worth moving: no move brings the busiest host down enough", "plan": chosen})
    return ROUTER.Reply(202, {"operation": start(chosen, role=role)})


# Its routes and who may use them (homestead_routes.py).
ROUTES = {
    ("GET", "/api/workloads/rebalance/plan"): ("viewer", lambda request: plan([a for a in (request.query.get("exclude") or [""])[0].split(",") if a],
                                                                            avoid=standby(), role=request.role)),
    ("POST", "/api/workloads/rebalance"): ("operator", start_run),
    ("POST", "/api/workloads/rebalance/review"): ("viewer", lambda request: picked((request.body or {}).get("picks"), role=request.role)),
}


def register(ops):
    """The job store's entry for a container rebalance (#622)."""
    ops.RESOLVERS[KIND] = status
    ops.CANCELLERS[KIND] = (cancel_plan, cancel_run)
