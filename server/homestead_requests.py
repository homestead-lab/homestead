"""Setting what apps reserve, many at once, from what they have used.

Edit offers one container's reservation from its week of use (homestead_usage).
On a cluster where most apps reserve nothing - 28 of 48 on the one in #347 -
that is an afternoon of dialogs, so this offers them together: a plan of each
container whose use suggests a different reservation, reviewed as a list,
then applied by a job one app at a time.

What is offered is what Homestead may change: a Deployment outside the system
namespaces, not Homestead itself (its manifest sets its own on every
upgrade), and not one Flux or Argo CD applies - their next sync would undo the
change, so those get the YAML to commit instead. An app with less than a day
of readings is left out with the reason.

Containers with no reservation at all are ticked; ones whose reservation only
differs from their use are offered unticked, since someone may have set them
on purpose. Each app restarts once, with all its containers' changes in one
rollout, and the next waits until it is running again. One that does not
come back within ten minutes stops the job, so a problem is looked at before
more restarts follow.
"""
import copy
import hashlib
import json
import time
import urllib.error

import homestead_gitops as GITOPS
import homestead_place as PLACE
import homestead_pod_resources as RESOURCES
import homestead_priority as PRIORITY

KIND = "set-requests"
MAX_APPS = 50
SYSTEM = ("kube-", "cattle-", "harvester-", "longhorn-", "fleet-")

kget = ksend = None
usage = None                     # homestead_usage.summary
managed_by = lambda dep: {}      # homestead_updates.managed_by
own = ("", "")                   # Homestead's namespace and Deployment
system_ns = frozenset()
is_leader = lambda: True
# guard(deployment, namespace, role): raises PermissionError for an app that
# role may not change - management, service-account or host-privileged ones,
# as Edit refuses them (homestead_host_access). The job runs with no request
# behind it, so it checks as the person who started it.
guard = lambda dep, ns, role: None
operations = None                # homestead_operations: the job is one of its jobs


# What bind() puts back for an argument it is not given: the module's own default,
# not the last bind's (a test's fake must not outlive the test).
_UNBOUND = {"is_leader": is_leader, "guard": guard}


def bind(_kget, _ksend, _usage, _managed_by, _own, _system_ns, _is_leader=None, _guard=None, _operations=None):
    global kget, ksend, usage, managed_by, own, system_ns, is_leader, guard, operations
    kget, ksend, usage, managed_by, own, system_ns = _kget, _ksend, _usage, _managed_by, tuple(_own), frozenset(_system_ns)
    operations = _operations
    if operations:
        # Its jobs resume and cancel through the job list like every other.
        operations.RESOLVERS[KIND] = status
        operations.CANCELLERS[KIND] = (cancel_plan, cancel_run)
    is_leader = _is_leader if _is_leader else _UNBOUND["is_leader"]
    guard = _guard if _guard else _UNBOUND["guard"]


def _deployments():
    for dep in kget("/apis/apps/v1/deployments").get("items", []):
        meta = dep.get("metadata") or {}
        ns = meta.get("namespace", "")
        if ns in system_ns or ns.startswith(SYSTEM):
            continue
        yield dep


def _resources(container):
    res = container.get("resources") or {}
    return res.get("requests") or {}, res.get("limits") or {}


def _yaml(dep, changes):
    """The change as a patch to commit where Flux or Argo CD reads the app
    from. Containers are named, so it merges into the ones there."""
    return GITOPS.document(dep, {"spec": {"template": {"spec": {"containers": [
        {"name": c["container"], "resources": {"requests": {"cpu": c["cpu"], "memory": c["memory"]}}} for c in changes]}}}})


def _changes(dep):
    """Each container of a Deployment whose use suggests another reservation,
    and the reason when none can be offered."""
    meta = dep.get("metadata") or {}
    ns, name = meta.get("namespace", ""), meta.get("name", "")
    out, waiting = [], []
    pod_spec = ((dep.get("spec") or {}).get("template") or {}).get("spec") or {}
    tier = PRIORITY.tier_of(pod_spec)[0]
    for container in pod_spec.get("containers") or []:
        requests, limits = _resources(container)
        found = usage(ns, name, container.get("name", ""), requests, limits, tier=tier)
        suggest = found.get("suggest")
        if not suggest:
            waiting.append(found.get("why") or "no readings yet")
            continue
        if not suggest.get("differs"):
            continue
        out.append({"container": container.get("name", ""),
                    "cpu_now": str(requests.get("cpu", "")), "memory_now": str(requests.get("memory", "")),
                    # What it reserves in effect: Kubernetes takes a limit as
                    # the request where none is set.
                    "cpu_now_m": suggest.get("cpu_now_m") or 0, "memory_now_mi": suggest.get("memory_now_mi") or 0,
                    "from_limit": [r for r in ("cpu", "memory") if not requests.get(r) and limits.get(r)],
                    "cpu": suggest["cpu"], "memory": suggest["memory"], "capped": suggest.get("capped") or [],
                    # Kubernetes takes a limit as the request where none is set.
                    "missing": not (requests.get("cpu") or limits.get("cpu")) and not (requests.get("memory") or limits.get("memory")),
                    "lowers": bool(suggest.get("lowers")),
                    "cpu_p95_m": found.get("cpu_p95_m"), "mem_p95_mi": found.get("mem_p95_mi"),
                    "mem_max_mi": found.get("mem_max_mi"), "days": found.get("days"), "hours": found.get("hours")})
    return out, waiting


def plan(chosen=None, role=None):
    """Every app whose reservations its use would change, and every one left
    out with why. chosen names the apps ticked - by default those reserving
    nothing, at most MAX_APPS - and the review token covers exactly those.
    role: who asks; apps they may not change are left out, saying why."""
    apps, skipped = [], []
    for dep in _deployments():
        meta = dep.get("metadata") or {}
        ns, name = meta.get("namespace", ""), meta.get("name", "")
        app_id = f"{ns}/{name}"
        changes, waiting = _changes(dep)
        if (ns, name) == own:
            if changes:
                skipped.append({"id": app_id, "why": "Homestead's own reservations come from its manifest, which every upgrade applies again"})
            continue
        owner = managed_by(dep)
        if changes and owner:
            where = f" ({owner['source']})" if owner.get("source") else ""
            why = (f"{owner['by']} applies it{where} and would undo the change at its next sync; commit this instead"
                   if owner.get("detected") else f"marked as updated by {owner['by']}; set it there - this is the change")
            skipped.append({"id": app_id, "why": why, "yaml": _yaml(dep, changes)})
            continue
        if changes:
            try:
                guard(dep, ns, role)
            except PermissionError as error:
                skipped.append({"id": app_id, "why": str(error)})
                continue
        if changes:
            apps.append({"id": app_id, "ns": ns, "name": name, "changes": changes,
                         "missing": any(c["missing"] for c in changes),
                         "replicas": int((dep.get("spec") or {}).get("replicas") or 0)})
        elif waiting and len(waiting) == len((((dep.get("spec") or {}).get("template") or {}).get("spec") or {}).get("containers") or []):
            skipped.append({"id": app_id, "why": waiting[0]})
    apps.sort(key=lambda a: (not a["missing"], a["id"]))
    # The default never ticks more than one run may change.
    chosen = set([a["id"] for a in apps if a["missing"]][:MAX_APPS] if chosen is None else chosen)
    for app in apps:
        app["chosen"] = app["id"] in chosen
    return {"apps": apps, "skipped": sorted(skipped, key=lambda s: s["id"]), "max": MAX_APPS,
            "review_token": review_token([a for a in apps if a["chosen"]])}


def review_token(apps):
    """What was reviewed: which containers get which reservation, and what
    each had then - so a reservation changed since by hand is not overwritten."""
    rows = sorted([a["id"], c["container"], c["cpu_now"], c["memory_now"], c["cpu"], c["memory"]]
                  for a in apps for c in a["changes"])
    return hashlib.sha256(json.dumps(rows).encode()).hexdigest()[:20]


FIELDS = ("container", "cpu_now", "memory_now", "cpu", "memory")


def reviewed(apps, token, role=None):
    """The changes a person reviewed, checked against the cluster now."""
    if not isinstance(apps, list) or not apps:
        raise ValueError("Choose at least one app")
    if len(apps) > MAX_APPS:
        raise ValueError(f"At most {MAX_APPS} apps at a time")
    for app in apps:
        if not isinstance(app, dict) or not isinstance(app.get("id"), str) or "/" not in app["id"] \
                or not isinstance(app.get("changes"), list) or not app["changes"] \
                or any(not isinstance(c, dict) or not all(isinstance(c.get(k), str) for k in FIELDS) for c in app["changes"]):
            raise ValueError("Review the reservations again")
        # The token only proves the list is the one reviewed; what is sent
        # must still be a reservation Kubernetes can read.
        for c in app["changes"]:
            if not _quantity(c["cpu"], "cpu") or not _quantity(c["memory"], "memory"):
                raise ValueError(f"{app['id']}: {c['container']} has no readable reservation; review again")
    if review_token(apps) != token:
        raise ValueError("The reservations differ from the review; review again")
    out = []
    for app in apps:
        ns, name = app["id"].split("/", 1)
        changes = [{k: c[k] for k in FIELDS} for c in app["changes"]]
        # Room is weighed when its turn comes: the cluster moves meanwhile,
        # and one app without room should not hold back the rest.
        check(ns, name, changes, role, fit=False)
        out.append({"id": app["id"], "ns": ns, "name": name, "changes": changes})
    return out


def check(ns, name, changes, role=None, fit=True):
    """One app as it is now: still one Homestead - and the person who asked -
    may change, and each container still reserving what it did when reviewed,
    so a reservation set by hand since is not overwritten. Returns "applied"
    when every container already has the new reservation: a change sent
    whose answer was lost is waited for, not counted as skipped. A limit
    lowered since below the new reservation is refused here, saying so,
    rather than sent for Kubernetes to refuse."""
    app_id = f"{ns}/{name}"
    try:
        dep = kget(f"/apis/apps/v1/namespaces/{ns}/deployments/{name}")
    except urllib.error.HTTPError as error:
        if error.code == 404:
            raise ValueError(f"{app_id} is gone")
        raise
    if (ns, name) == own or ns in system_ns or ns.startswith(SYSTEM) or managed_by(dep):
        raise ValueError(f"{app_id} is not one Homestead changes")
    guard(dep, ns, role)
    containers = {c.get("name"): c for c in (((dep.get("spec") or {}).get("template") or {}).get("spec") or {}).get("containers") or []}
    now, limits = [], []
    for change in changes:
        container = containers.get(change["container"])
        if not container:
            raise ValueError(f"{app_id} has no container {change['container']} now")
        requests, limit = _resources(container)
        now.append((str(requests.get("cpu", "")), str(requests.get("memory", ""))))
        limits.append(limit)
    if all(_same(n[0], c["cpu"], "cpu") and _same(n[1], c["memory"], "memory") for n, c in zip(now, changes)):
        return "applied"
    if not all(_same(n[0], c["cpu_now"], "cpu") and _same(n[1], c["memory_now"], "memory") for n, c in zip(now, changes)):
        raise ValueError(f"{app_id}'s reservations changed since the review")
    for change, limit in zip(changes, limits):
        for r in ("cpu", "memory"):
            if limit.get(r) and _quantity(change[r], r) > _quantity(limit[r], r):
                raise ValueError(f"{app_id}: {change['container']}'s {'CPU' if r == 'cpu' else 'memory'} limit is now "
                                 f"{limit[r]}, below the new reservation {change[r]}; review again")
    if fit:
        spec = dep.get("spec") or {}
        # One already rolling out, failing or paused would not settle, and the
        # new reservation would be blamed for it: it is left, saying why.
        if spec.get("paused"):
            raise ValueError(f"{app_id} is paused; resume it, then review again")
        if not _settled(ns, name):
            raise ValueError(f"{app_id} is not running as it should now (rolling out, or not ready); left alone")
        # A stopped app takes its new reservation when it next starts; it
        # needs no room now.
        if int(spec.get("replicas") or 0):
            room(dep, changes)
    return "ready"


def _quantity(text, resource):
    try:
        return RESOURCES.quantity(text, resource) if str(text or "").strip() else 0
    except (TypeError, ValueError):
        return 0


def _same(a, b, resource):
    """Two quantities the same however written: the API gives back 1Gi for
    1024Mi, and 1 for 1000m."""
    return (not str(a or "").strip() and not str(b or "").strip()) or (
        bool(str(a or "").strip()) and bool(str(b or "").strip()) and _quantity(a, resource) == _quantity(b, resource))


def room(dep, changes):
    """Whether a host the app may run on can take its pod with the new
    reservation: raising one where none has the room leaves the new pod
    Pending - and a Recreate app down. Only the hosts its node name,
    nodeSelector, required affinity and their taints allow are weighed - an
    app pinned to one full host has no room, however empty the others are -
    and reservations only, as the scheduler counts them."""
    spec = copy.deepcopy(((dep.get("spec") or {}).get("template") or {}).get("spec") or {})
    for container in spec.get("containers") or []:
        for change in changes:
            if container.get("name") == change["container"]:
                container.setdefault("resources", {})["requests"] = {
                    **((container.get("resources") or {}).get("requests") or {}), "cpu": change["cpu"], "memory": change["memory"]}
    need = {r: RESOURCES.pod_request(spec, r) for r in ("cpu", "memory")}
    meta = dep.get("metadata") or {}
    recreate = ((dep.get("spec") or {}).get("strategy") or {}).get("type") == "Recreate"
    booked, mine = {}, {}
    for pod in kget("/api/v1/pods").get("items", []):
        node, pmeta = (pod.get("spec") or {}).get("nodeName"), pod.get("metadata") or {}
        if not node or (pod.get("status") or {}).get("phase") in ("Succeeded", "Failed"):
            continue
        used = {r: RESOURCES.pod_request(pod.get("spec") or {}, r) for r in need}
        # A Recreate rollout removes the old pod first, giving its room back:
        # a pod of this Deployment's ReplicaSet, named after it and the pod
        # template's hash - not app-worker's beside app.
        hash_ = (pmeta.get("labels") or {}).get("pod-template-hash")
        if recreate and hash_ and pmeta.get("namespace") == meta.get("namespace") and any(
                o.get("kind") == "ReplicaSet" and o.get("name") == f"{meta.get('name', '')}-{hash_}"
                for o in pmeta.get("ownerReferences") or []):
            mine[node] = {r: mine.get(node, {}).get(r, 0) + used[r] for r in need}
        booked[node] = {r: booked.get(node, {}).get(r, 0) + used[r] for r in need}
    best, allowed = 0, 0
    for node in kget("/api/v1/nodes").get("items", []):
        name, status = node["metadata"]["name"], node.get("status") or {}
        ready = any(c.get("type") == "Ready" and c.get("status") == "True" for c in status.get("conditions") or [])
        if not ready or (node.get("spec") or {}).get("unschedulable") or not PLACE.may_run_on(spec, node):
            continue
        allowed += 1
        alloc = status.get("allocatable") or {}
        free = {r: RESOURCES.quantity(alloc.get(r), r) - booked.get(name, {}).get(r, 0) + mine.get(name, {}).get(r, 0) for r in need}
        if all(free[r] >= need[r] for r in need):
            return
        best = max(best, free["memory"])
    if not allowed:
        raise ValueError("no host it may run on takes apps now (its pins, selectors or the hosts' taints rule them out)")
    raise ValueError(f"no host it may run on has room for its new reservation ({need['memory'] // 2 ** 20} MiB; "
                     f"the most any of them has free is {max(0, best) // 2 ** 20} MiB)")


# --------------------------------------------------------------- the job
def _settled(ns, name):
    dep = kget(f"/apis/apps/v1/namespaces/{ns}/deployments/{name}")
    status, spec, meta = dep.get("status") or {}, dep.get("spec") or {}, dep.get("metadata") or {}
    want = int(spec.get("replicas") or 0)
    return (int(status.get("observedGeneration") or 0) >= int(meta.get("generation") or 0)
            and int(status.get("updatedReplicas") or 0) == want
            and int(status.get("readyReplicas") or 0) == want
            and int(status.get("replicas") or 0) == want)


def patch(app):
    """All of one app's containers in one change, so it restarts once."""
    containers = [{"name": c["container"], "resources": {"requests": {"cpu": c["cpu"], "memory": c["memory"]}}}
                  for c in app["changes"]]
    ksend("PATCH", f"/apis/apps/v1/namespaces/{app['ns']}/deployments/{app['name']}",
          {"spec": {"template": {"spec": {"containers": containers}}}},
          ctype="application/strategic-merge-patch+json")


def status(item):
    ref = item["ref"]
    apps, i, done = ref["apps"], ref.get("index", 0), ref.get("done", 0)
    if i >= len(apps):
        skipped = ref.get("skipped") or []
        head = f"Set reservations on {done} app{'' if done == 1 else 's'}" if done else "Nothing was changed"
        return "succeeded", 100, head + (f"; left {len(skipped)}: " + "; ".join(skipped[:4]) if skipped else "")
    app = apps[i]
    progress = int(100 * i / max(1, len(apps)))
    if not is_leader():
        return "running", progress, item.get("message") or f"Setting {app['id']}"
    stage, now = ref.get("stage", "apply"), time.time()
    if stage == "apply":
        try:
            if check(app["ns"], app["name"], app["changes"], ref.get("role")) == "ready":
                patch(app)
        except (ValueError, PermissionError, urllib.error.HTTPError) as error:
            # Refused - a limit lowered below the new reservation, no room,
            # or changed or gone since the review: this app is left, the
            # rest go on.
            if isinstance(error, urllib.error.HTTPError) and error.code not in (403, 404, 409, 422):
                raise
            why = _refused(error) if isinstance(error, urllib.error.HTTPError) else str(error)
            ref.setdefault("skipped", []).append(f"{app['id']}: {why[:160]}")
            ref.update(index=i + 1)
            return "running", progress, f"Skipped {app['id']}: {why[:160]}"
        ref.update(stage="rolling", started=now)
        return "running", progress, f"Setting {app['id']} ({i + 1} of {len(apps)}); it restarts"
    if stage == "rolling":
        if _settled(app["ns"], app["name"]):
            ref.update(index=i + 1, stage="apply", done=done + 1)
            return "running", int(100 * (i + 1) / len(apps)), f"{app['id']} is running with its new reservations"
        if now - ref.get("started", now) > 600:
            # Stop rather than restart more: one that did not come back is
            # looked at first. It keeps its new reservation, so what it had
            # is said, to set back in Edit if that is the cause.
            return "failed", progress, (f"{app['id']} did not come back within 10 minutes; the rest were not changed. "
                                        f"Set {done} before it. It reserved {_before(app)} before; check its events "
                                        f"in Containers, and set that back in Edit if the new reservation is why")
        return "running", progress, f"Waiting for {app['id']} to restart ({i + 1} of {len(apps)})"
    return "failed", progress, "Unknown step; the rest were not changed"


def _refused(error):
    """Kubernetes' own reason for refusing a change, where it gave one."""
    try:
        message = json.loads(error.read() or b"{}").get("message") or ""
    except (ValueError, OSError, AttributeError):
        message = ""
    return f"Kubernetes refused it ({error.code}{': ' + message if message else ''})"


def _before(app):
    """What each container reserved before the change, in Edit's words."""
    return "; ".join(f"{c['container']}: CPU {c['cpu_now'] or 'none'}, memory {c['memory_now'] or 'none'}"
                     for c in app["changes"])


def cancel_plan(item):
    return {"mode": "stop", "action": "Stop setting reservations",
            "undo": ["No more apps are changed"],
            "keeps": ["An app already restarting finishes with its new reservations",
                      "Apps already changed keep their new reservations"], "needs": "operator"}


def cancel_run(work, chosen):
    done = (work.get("ref") or {}).get("done", 0)
    return f"Stopped after setting {done} app{'' if done == 1 else 's'}"


def start(body, role=None):
    """The job for the apps a person reviewed, checked against the cluster
    now; role is theirs, and each app is checked for them again as it is
    changed. Refused while another such job runs, or without the restarts
    confirmed."""
    running = next((i for i in operations.snapshot() if i.get("kind") == KIND
                    and i.get("status") not in operations.TERMINAL), None)
    if running:
        raise ValueError("Reservations are already being set; follow it in Jobs")
    if (body or {}).get("restart") is not True:
        raise ValueError("A new reservation restarts each app; confirm the restarts")
    apps = reviewed(body.get("apps"), body.get("review_token"), role)
    return {"operation": operations.start(KIND, f"Set reservations on {len(apps)} app{'' if len(apps) == 1 else 's'}",
                                          {"kind": "Deployment", "name": "reservations"}, "/containers",
                                          {"apps": apps, "index": 0, "done": 0, "stage": "apply", "role": role},
                                          "Starting with the first app")}


ROUTES = {
    ("GET", "/api/workloads/requests/plan"): ("viewer", lambda request: plan(
        [a for a in request.query["chosen"][0].split(",") if a] if request.query.get("chosen") else None, request.role)),
    ("POST", "/api/workloads/requests"): ("operator", lambda request: start(request.body, request.role)),
}
