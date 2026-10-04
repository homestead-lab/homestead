"""Moving containers so hosts carry similar CPU and memory.

A host can end up running most of the busy apps - after a reboot drained it
and everything came back elsewhere, or as apps were added. Moving a
container restarts it, so the plan makes the fewest moves that bring the
busiest host down, each one worth its restart, and each can be vetoed.

What can move is what the scheduler could already place elsewhere: a
Deployment with one copy, not Homestead itself, not pinned to a host, not
on host-local storage, and only to hosts its hardware allows (the same
check a manual move uses). A host that already holds a copy of its volumes
is preferred, so its reads stay local.

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

KIND = "container-rebalance"
MAX_MOVES = 10
GAIN = 3.0          # a move must bring its host's load down by this many points
SYSTEM = ("kube-", "cattle-", "harvester-", "longhorn-", "fleet-")
SYSTEM_NS = {"kube-system", "longhorn-system", "kubevirt", "cdi", "system-upgrade", "local"}
LH = "/apis/longhorn.io/v1beta2/namespaces/longhorn-system"

kget = None
own = ("", "")                  # Homestead's namespace and Deployment
requirements = satisfies = None # homestead_place: what a workload needs, whether a host has it
summaries = None                # homestead_place.get_nodes: the host summaries satisfies() reads
apply = None                    # (ns, name, node) -> moves it, after the manual move's capacity check
is_leader = lambda: True


def bind(_kget, _requirements, _satisfies, _apply, _own=("", ""), _is_leader=None, _summaries=None):
    global kget, requirements, satisfies, apply, own, is_leader, summaries
    kget, requirements, satisfies, apply, own = _kget, _requirements, _satisfies, _apply, tuple(_own)
    summaries = _summaries
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
    try:
        return kget(path).get("items", [])
    except urllib.error.HTTPError as error:
        if error.code == 404:
            return []
        raise


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


def inventory():
    nodes = _items("/api/v1/nodes")
    usage = {(m.get("metadata") or {}).get("name"): m.get("usage") or {} for m in _items("/apis/metrics.k8s.io/v1beta1/nodes")}
    hosts = {}
    for node in nodes:
        name = (node.get("metadata") or {}).get("name")
        alloc = (node.get("status") or {}).get("allocatable") or {}
        hosts[name] = {"name": name, "cpu": cpu((usage.get(name) or {}).get("cpu")), "mem": mem((usage.get(name) or {}).get("memory")),
                       "cpu_total": cpu(alloc.get("cpu")) or 1, "mem_total": mem(alloc.get("memory")) or 1,
                       "takes": _ready(node) and not (node.get("spec") or {}).get("unschedulable"), "node": node,
                       "metrics": name in usage}
    pods = _items("/api/v1/pods")
    pod_usage = {}
    for m in _items("/apis/metrics.k8s.io/v1beta1/pods"):
        meta = m.get("metadata") or {}
        pod_usage[(meta.get("namespace"), meta.get("name"))] = (
            sum(cpu((c.get("usage") or {}).get("cpu")) for c in m.get("containers") or []),
            sum(mem((c.get("usage") or {}).get("memory")) for c in m.get("containers") or []))
    sets = {((r.get("metadata") or {}).get("namespace"), (r.get("metadata") or {}).get("name")): r for r in _items("/apis/apps/v1/replicasets")}
    # Where each Longhorn volume has a copy: a host holding one keeps reads local.
    claims = {}
    for volume in _items(f"{LH}/volumes"):
        k8s = (volume.get("status") or {}).get("kubernetesStatus") or {}
        claims[(k8s.get("namespace"), k8s.get("pvcName"))] = (volume.get("metadata") or {}).get("name")
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
        apps.append({"ns": ns, "name": name, "id": f"{ns}/{name}", "host": where[0], "dep": dep,
                     "cpu": sum(u[0] for u in use), "mem": sum(u[1] for u in use), "why": why,
                     "near": set.intersection(*[copies.get(v, set()) for v in volumes]) if volumes and all(volumes) else None})
    return hosts, apps


def _score(host, cpu_m, mem_b):
    return max(100 * cpu_m / host["cpu_total"], 100 * mem_b / host["mem_total"])


def plan(exclude=()):
    hosts, apps = inventory()
    exclude = set(exclude or ())
    load = {n: [h["cpu"], h["mem"]] for n, h in hosts.items()}
    score = lambda n: _score(hosts[n], *load[n])
    # satisfies() reads Homestead's own host summary (status, labels,
    # devices), not the Kubernetes Node.
    summary = {n.get("name"): n for n in (summaries() if summaries else [])}
    eligible = {}
    for app in apps:
        if app["why"]:
            continue
        reqs = requirements(app["dep"])
        eligible[app["id"]] = {n for n, h in hosts.items() if n != app["host"] and h["takes"]
                               and satisfies(summary.get(n, h["node"]), reqs)[0]}
    moves, moved = [], set()
    while len(moves) < MAX_MOVES:
        best = None
        # The busiest host first; when nothing of its can move, the next one.
        for busiest in sorted(load, key=score, reverse=True):
            for app in apps:
                if app["host"] != busiest or app["why"] or app["id"] in exclude or app["id"] in moved:
                    continue
                for target in eligible.get(app["id"], ()):
                    after_from = _score(hosts[busiest], load[busiest][0] - app["cpu"], load[busiest][1] - app["mem"])
                    after_to = _score(hosts[target], load[target][0] + app["cpu"], load[target][1] + app["mem"])
                    peak = max(after_from, after_to)
                    gain = score(busiest) - peak
                    near = bool(app["near"] and target in app["near"])
                    key = (gain - (0 if near else 2), near)      # a host with its volumes is worth two points
                    if gain >= GAIN and (best is None or key > best[0]):
                        best = (key, app, target, near)
            if best:
                break
        if not best:
            break
        _, app, target, near = best
        load[app["host"]][0] -= app["cpu"]; load[app["host"]][1] -= app["mem"]
        load[target][0] += app["cpu"]; load[target][1] += app["mem"]
        moves.append({"ns": app["ns"], "name": app["name"], "id": app["id"], "from": app["host"], "to": target,
                      "cpu_m": round(app["cpu"]), "mem_gb": round(app["mem"] / 1024 ** 3, 2), "near": near})
        moved.add(app["id"])
    shown = {m["id"] for m in moves} | (exclude & {a["id"] for a in apps})
    token = hashlib.sha256(json.dumps([moves, sorted(exclude)], sort_keys=True).encode()).hexdigest()[:20]
    return {"hosts": [{"name": n, "takes": hosts[n]["takes"], "metrics": hosts[n]["metrics"],
                       "cpu_before": round(100 * hosts[n]["cpu"] / hosts[n]["cpu_total"]),
                       "cpu_after": round(100 * load[n][0] / hosts[n]["cpu_total"]),
                       "mem_before": round(100 * hosts[n]["mem"] / hosts[n]["mem_total"]),
                       "mem_after": round(100 * load[n][1] / hosts[n]["mem_total"])} for n in sorted(hosts)],
            "moves": moves, "apps": sorted(shown), "excluded": sorted(exclude),
            "skipped": [{"id": a["id"], "why": a["why"]} for a in apps if a["why"]],
            "metrics": all(h["metrics"] for h in hosts.values()), "review_token": token}


# --------------------------------------------------------------- the job
def _running_on(ns, name):
    dep = kget(f"/apis/apps/v1/namespaces/{ns}/deployments/{name}")
    status, spec, meta = dep.get("status") or {}, dep.get("spec") or {}, dep.get("metadata") or {}
    settled = (int(status.get("observedGeneration") or 0) >= int(meta.get("generation") or 0)
               and int(status.get("updatedReplicas") or 0) == int(spec.get("replicas") or 0)
               and int(status.get("readyReplicas") or 0) == int(spec.get("replicas") or 0)
               and int(status.get("replicas") or 0) == int(spec.get("replicas") or 0))
    selector = urllib.parse.quote(_selector(dep), safe="")
    hosts = {(p.get("spec") or {}).get("nodeName") for p in _items(f"/api/v1/namespaces/{ns}/pods?labelSelector={selector}")
             if (p.get("status") or {}).get("phase") == "Running" and not (p.get("metadata") or {}).get("deletionTimestamp")}
    return settled, hosts


def status(item):
    ref = item["ref"]
    moves, i, done = ref["moves"], ref.get("index", 0), ref.get("moved", 0)
    if i >= len(moves):
        skipped = ref.get("skipped") or []
        return "succeeded", 100, (f"Moved {done} container{'' if done == 1 else 's'}"
                                  + (f"; left {len(skipped)}: " + "; ".join(skipped[:4]) if skipped else ""))
    move = moves[i]
    progress = int(100 * i / max(1, len(moves)))
    if not is_leader():
        return "running", progress, item.get("message") or f"Moving {move['id']}"
    stage, now = ref.get("stage", "move"), time.time()
    if stage == "move":
        try:
            _, hosts = _running_on(move["ns"], move["name"])
        except urllib.error.HTTPError as error:
            if error.code != 404:
                raise
            hosts = set()
        if hosts != {move["from"]}:
            ref.setdefault("skipped", []).append(f"{move['id']}: it moved or changed since the review")
            ref.update(index=i + 1)
            return "running", progress, f"Skipped {move['id']}: it moved or changed since the review"
        try:
            apply(move["ns"], move["name"], move["to"])
        except ValueError as error:
            ref.setdefault("skipped", []).append(f"{move['id']}: {str(error)[:160]}")
            ref.update(index=i + 1)
            return "running", progress, f"Skipped {move['id']}: {str(error)[:160]}"
        ref.update(stage="starting", started=now)
        return "running", progress, f"Moving {move['id']} to {move['to']} ({i + 1} of {len(moves)}); it restarts there"
    if stage == "starting":
        settled, hosts = _running_on(move["ns"], move["name"])
        if settled and hosts and move["from"] not in hosts:
            ref.update(index=i + 1, stage="move", moved=done + 1)
            return "running", int(100 * (i + 1) / len(moves)), f"{move['id']} runs on {', '.join(sorted(hosts))}"
        if now - ref.get("started", now) > 600:
            # Stop rather than restart more: one that did not come up is looked at first.
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
    moved = (work.get("ref") or {}).get("moved", 0)
    return f"Stopped after moving {moved} container{'' if moved == 1 else 's'}"
