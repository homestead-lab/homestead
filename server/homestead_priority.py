"""Priority tiers: which apps fit first when the cluster is short of room.

When a host fails, its apps start on the others; if those cannot hold them
all, the scheduler fits them by priority, and the home automation can wait
behind a media scanner (#347). A pod's priority is the value of its
PriorityClass, fixed when the pod is made; one naming no class gets the
cluster's globalDefault class, or 0 without one. So a tier is a number
relative to that default (#429), whatever the class is called:

* critical - above the default: fits first, and may evict pods of lower
  priority to fit. For what the house depends on.
* normal - the default.
* low - below it: waits, and is evicted first. Batch work and CI runners.

The ladder (ladder()) reads the cluster's classes once and says which class
each tier uses: the cluster's own where one fits - critical the highest
class above the default that may preempt, low the highest below it,
preferring one that never preempts - and otherwise one Homestead makes, just
above or below the default (homestead-critical, homestead-low, or with the
value in the name when that one is taken at another value). Kubernetes' own
classes, and the platform's (Longhorn's sits at 1e9), are never a tier's.
Homestead never updates or deletes a class, and never makes one the default.
When the classes can't be read it does as it did before #429, and protect()
waits rather than guess.

VMs have tiers too, set in their template: a VM takes its tier when it next
starts, so setting one restarts nothing. StatefulSets have tiers of their own,
never a group's: one rolls its pods one at a time, and one that doesn't restart
itself (OnDelete, or a partition at or above its replicas) takes its tier
when its pods are next restarted - Homestead never deletes them.

Changing a tier changes the pod template, so the app restarts once. Like the
reservations (homestead_requests), the tiers are reviewed as a list and
applied by a job one app at a time; Flux- and Argo-applied apps get the line
to commit instead. Homestead's own tier is not offered: protect() keeps it at
least as high as any app, and never below the default.
"""
import hashlib
import json
import re
import time
import urllib.error

# homestead_group_tiers imports this module too; each reads the other's names
# only when called, so either may be imported first.
import homestead_group_tiers as GROUPS
import homestead_kube as KUBE

KIND = "set-priority"
MAX_APPS = 50
SYSTEM = ("kube-", "cattle-", "harvester-", "longhorn-", "fleet-")
VMS = "/apis/kubevirt.io/v1/virtualmachines"
STATEFULSETS = "/apis/apps/v1/statefulsets"
PER_POD = 600                    # seconds a restart may take: per pod for a StatefulSet
TIERS = {
    "critical": {"name": "homestead-critical", "value": 1000, "preemptionPolicy": "PreemptLowerPriority",
                 "description": "Homestead: fits first, and may evict lower-priority pods to fit."},
    "normal": {"name": None},
    "low": {"name": "homestead-low", "value": -1000, "preemptionPolicy": "Never",
            "description": "Homestead: waits for room, and never evicts anything."},
}
BY_CLASS = {"homestead-critical": "critical", "homestead-normal": "normal", "homestead-low": "low"}
SYSTEM_VALUE = 1_000_000_000     # above this, Kubernetes' own classes (system-*)
INT32_MIN = -2_147_483_648
STEP = 1000                      # how far above or below the default a class Homestead makes sits
OURS = re.compile(r"homestead-(critical|low|normal)(-\d+)?")
LADDER_TTL = 5
_LADDER = {"at": 0.0, "value": None}


def _rows(items):
    rows = []
    for c in items:
        name = (c.get("metadata") or {}).get("name", "")
        value = int(c.get("value") or 0)
        rows.append({"name": name, "value": value, "default": bool(c.get("globalDefault")),
                     "preempts": (c.get("preemptionPolicy") or "PreemptLowerPriority") != "Never",
                     # Kubernetes' own (above 1e9) and the platform's - Longhorn's
                     # longhorn-critical sits at exactly 1e9 - are never a tier's class.
                     "system": value >= SYSTEM_VALUE or name.startswith(SYSTEM) or name.startswith("system-"),
                     "homestead": bool(OURS.fullmatch(name))})
    return rows


def _made(tier, value, rows):
    """The class Homestead would make for a tier at this value: its plain
    name if free, else one carrying the value, as restore classes carry a
    digest of what they are. None if even that name is taken otherwise."""
    by_name = {r["name"]: r for r in rows}
    for name in (f"homestead-{tier}", f"homestead-{tier}-{value}"):
        row = by_name.get(name)
        if row is None:
            return {"name": name, "value": value, "create": True}
        if row["value"] == value and row["preempts"] == (tier == "critical"):
            return {"name": name, "value": value, "create": False}
    return None


def ladder(items=None, now=None):
    """The cluster's ranking, read from its PriorityClasses: the default
    (what a pod naming no class gets), the class critical and low use - one
    of the cluster's own where one fits, else one to make - and what is
    wrong. A class is placed by its value, never its name."""
    now = now or time.time()
    if items is None:
        if _LADDER["value"] is not None and now - _LADDER["at"] < LADDER_TTL:
            return _LADDER["value"]
        try:
            items = kget("/apis/scheduling.k8s.io/v1/priorityclasses").get("items", [])
        except Exception as error:
            return _fallback(forbidden=isinstance(error, urllib.error.HTTPError) and error.code == 403)
        out = ladder(items)
        _LADDER.update(at=now, value=out)
        return out
    rows = _rows(items)
    defaults = sorted((r for r in rows if r["default"]), key=lambda r: r["value"])
    default = {"name": defaults[0]["name"], "value": defaults[0]["value"]} if defaults else {"name": None, "value": 0}
    d, warnings = default["value"], []
    # A Homestead class serves only the tier its name says: a stale
    # homestead-critical below the default is never taken for low.
    def ours_for(r, tier):
        m = OURS.fullmatch(r["name"])
        return not m or m.group(1) == tier
    above = sorted((r for r in rows if not r["system"] and r["value"] > d and r["preempts"] and ours_for(r, "critical")),
                   key=lambda r: (-r["value"], not r["homestead"], r["name"]))
    if above:
        critical = {"name": above[0]["name"], "value": above[0]["value"], "create": False}
    else:
        value = min(d + STEP, SYSTEM_VALUE - 1)   # 1e9 itself is the platform's (Longhorn's)
        critical = _made("critical", value, rows) if value > d else None
        if critical is None:
            warnings.append(f"Critical can't rank above the cluster default ({default['name'] or 'none'}, {d}): "
                            "there is no room, or its name is taken")
    below = sorted((r for r in rows if not r["system"] and r["value"] < d and ours_for(r, "low")),
                   key=lambda r: (r["preempts"], -r["value"], not r["homestead"], r["name"]))
    if below:
        low = {"name": below[0]["name"], "value": below[0]["value"], "create": False}
    else:
        low = _made("low", max(d - STEP, INT32_MIN), rows)
    return {"default": default, "classes": rows, "critical": critical, "low": low,
            "normal": default["name"], "warnings": warnings}


def _fallback(forbidden=True):
    """What Homestead assumed before it read the classes: its own two (and the
    legacy homestead-normal, at the default), and no default."""
    return dict(fallback=True, forbidden=forbidden, **ladder([{"metadata": {"name": TIERS[t]["name"]}, "value": TIERS[t]["value"],
                    "preemptionPolicy": TIERS[t]["preemptionPolicy"]} for t in ("critical", "low")]
                  + [{"metadata": {"name": "homestead-normal"}, "value": 0}]))


def priority_of(spec, lad):
    """(the number a pod has or would get, whether it may preempt); None for a class that isn't there."""
    spec = spec or {}
    name = spec.get("priorityClassName") or ""
    row = next((r for r in lad["classes"] if r["name"] == name), None) if name else None
    if spec.get("priority") is not None:
        never = spec.get("preemptionPolicy") == "Never" or (row is not None and not row["preempts"])
        return int(spec["priority"]), not never
    if name:
        return (None, False) if row is None else (row["value"], row["preempts"])
    d = next((r for r in lad["classes"] if r["name"] == lad["default"]["name"]), None)
    return lad["default"]["value"], d["preempts"] if d else True

# What an app is, from its name or image, when it is likely critical or low.
# A suggestion only: it is ticked in the review, never applied unseen.
HINTS = [
    ("critical", re.compile(r"home-?assistant|zigbee2mqtt|z-?wave|mosquitto|vaultwarden|bitwarden|"
                            r"postgres|mariadb|mysql|cloudflared|tailscale|wireguard|pi-?hole|adguard")),
    ("low", re.compile(r"actions-runner|gitlab-runner|renovate|\brunner\b")),
]
# A helper beside one of those - its exporter, backup or sync - is not it.
HELPER = re.compile(r"(exporter|backup|sync|metrics|dashboard|\bui|admin)\b")
WHY = {"critical": "looks like home automation, a password manager, a database, DNS or a tunnel",
       "low": "looks like batch or CI work"}

kget = ksend = None
managed_by = lambda dep: {}
own = ("", "")
system_ns = frozenset()
is_leader = lambda: True
guard = lambda dep, ns, role: None   # as in homestead_requests: may this role change it
operations = None                    # homestead_operations: the job is one of its jobs


def bind(_kget, _ksend, _managed_by, _own, _system_ns, _is_leader=None, _guard=None, _operations=None):
    global kget, ksend, managed_by, own, system_ns, is_leader, guard, operations
    kget, ksend, managed_by, own, system_ns = _kget, _ksend, _managed_by, tuple(_own), frozenset(_system_ns)
    operations = _operations
    if operations:
        # Its jobs resume and cancel through the job list like every other.
        operations.RESOLVERS[KIND] = status
        operations.CANCELLERS[KIND] = (cancel_plan, cancel_run)
    if _is_leader:
        is_leader = _is_leader
    if _guard:
        guard = _guard


def tier_of(spec, lad=None):
    """(tier, class name), by the number the class or pod carries relative to
    the cluster default: above is critical, equal normal, below low; above
    1e9 Kubernetes' own (system); a class that isn't there, missing."""
    lad = lad if lad is not None else ladder()
    name = (spec or {}).get("priorityClassName") or ""
    number, _ = priority_of(spec, lad)
    row = next((r for r in lad["classes"] if r["name"] == name), None) if name else None
    if number is None:
        # Not listed: missing - unless the list itself couldn't be read.
        return ("unknown" if lad.get("fallback") else "missing"), name
    if number >= SYSTEM_VALUE or (row is not None and row["system"]):
        return "system", name
    d = lad["default"]["value"]
    return ("critical" if number > d else "low" if number < d else "normal"), name


def suggestion(dep):
    """The tier an app's name or image suggests, and why; None for neither."""
    meta = dep.get("metadata") or {}
    images = " ".join(c.get("image", "") for c in (((dep.get("spec") or {}).get("template") or {}).get("spec") or {}).get("containers") or [])
    text = f"{meta.get('name', '')} {images}".lower()
    if HELPER.search(meta.get("name", "").lower()):
        return None, ""
    for tier, pattern in HINTS:
        if pattern.search(text):
            return tier, WHY[tier]
    return None, ""


NO_PERMISSION = ("Homestead may not make priority classes yet: its permissions predate this version. "
                 "Settings → Homestead → About this installation → Permissions says how to bring them up to date.")


def class_for(tier, lad):
    """The class a tier is written as: None for normal (the default)."""
    if tier == "normal":
        return None
    step = lad.get(tier)
    if not step:
        raise ValueError(lad["warnings"][0] if lad["warnings"] else f"no class for {tier}")
    return step["name"]


def _ensure_by_name(tiers):
    """Before the classes can be listed - permissions from before #429 - as
    Homestead always did: its own classes, by name, made where missing. One that exists with
    another value or policy is kept as it is - someone made it - and said so."""
    notes = []
    for tier in tiers:
        spec = TIERS[tier]
        if not spec["name"]:
            continue
        try:
            found = kget(f"/apis/scheduling.k8s.io/v1/priorityclasses/{spec['name']}")
            if (found.get("value"), found.get("preemptionPolicy", "PreemptLowerPriority")) != (spec["value"], spec["preemptionPolicy"]):
                notes.append(f"{spec['name']} exists with value {found.get('value')} and "
                             f"{found.get('preemptionPolicy', 'PreemptLowerPriority')}; it is kept")
            continue
        except urllib.error.HTTPError as error:
            if error.code == 403:
                raise PermissionError(NO_PERMISSION)
            if error.code != 404:
                raise
        try:
            ksend("POST", "/apis/scheduling.k8s.io/v1/priorityclasses", {
                "apiVersion": "scheduling.k8s.io/v1", "kind": "PriorityClass",
                "metadata": {"name": spec["name"], "labels": {"app.kubernetes.io/managed-by": "homestead"}},
                "value": spec["value"], "preemptionPolicy": spec["preemptionPolicy"],
                "globalDefault": False, "description": spec["description"]})
        except urllib.error.HTTPError as error:
            if error.code == 403:
                raise PermissionError(NO_PERMISSION)
            # Made meanwhile by an edit or another job: there now, as wanted.
            if error.code != 409:
                raise
    return notes


def ensure(tiers=("critical", "low"), lad=None):
    """The classes these tiers use, made only where the ladder has none to
    use. A class is never updated or deleted: its value cannot change, and
    one in use must stay."""
    lad = lad if lad is not None else ladder()
    if lad.get("fallback"):
        if not lad.get("forbidden"):
            raise ValueError("Homestead couldn't read the cluster's priority classes just now; nothing was made. Try again")
        return _ensure_by_name(tiers)
    notes = []
    for tier in tiers:
        step = lad.get(tier) if tier != "normal" else None
        if tier != "normal" and not step:
            raise ValueError(lad["warnings"][0] if lad["warnings"] else f"no class for {tier}")
        if not step or not step["create"]:
            continue
        try:
            ksend("POST", "/apis/scheduling.k8s.io/v1/priorityclasses", {
                "apiVersion": "scheduling.k8s.io/v1", "kind": "PriorityClass",
                "metadata": {"name": step["name"], "labels": {"app.kubernetes.io/managed-by": "homestead",
                                                               "homestead.io/task": "priority"}},
                "value": step["value"], "preemptionPolicy": TIERS[tier]["preemptionPolicy"], "globalDefault": False,
                "description": (f"Homestead's {tier} tier: {'above' if tier == 'critical' else 'below'} the cluster default "
                                f"{lad['default']['name'] or '(none)'} ({lad['default']['value']}). Made by Homestead; "
                                "apps name it through their tier.")})
            notes.append(f"made {step['name']} ({step['value']})")
        except urllib.error.HTTPError as error:
            if error.code == 403:
                raise PermissionError(NO_PERMISSION)
            if error.code != 409:
                raise
    _LADDER.update(at=0, value=None)
    return notes


def _items(path):
    """A list's items; none where the API isn't there (404). A null answer
    is not a 404: it fails, as before."""
    listing = KUBE.get_optional(path, read=kget, missing=KUBE.GONE)
    return [] if listing is KUBE.GONE else listing.get("items", [])


def _workloads():
    """("app", Deployment), ("vm", VirtualMachine) and ("sts", StatefulSet)
    outside the platform's namespaces; no VMs where KubeVirt is not installed."""
    vms, stss = _items(VMS), _items(STATEFULSETS)
    for kind, obj in ([("app", d) for d in kget("/apis/apps/v1/deployments").get("items", [])] + [("vm", v) for v in vms]
                      + [("sts", s) for s in stss]):
        ns = (obj.get("metadata") or {}).get("namespace", "")
        if ns not in system_ns and not ns.startswith(SYSTEM):
            yield kind, obj


PREFIX = {"vm": "vm:", "sts": "sts:"}     # an app's id has none


def _row_id(kind, ns, name):
    return f"{PREFIX.get(kind, '')}{ns}/{name}"


def _split(row_id):
    """(kind, ns, name) from a row's id: a VM's starts vm:, a StatefulSet's sts:."""
    kind = next((k for k, prefix in PREFIX.items() if row_id.startswith(prefix)), "app")
    ns, name = row_id[len(PREFIX.get(kind, "")):].split("/", 1)
    return kind, ns, name


def _path(kind, ns, name):
    return {"vm": f"/apis/kubevirt.io/v1/namespaces/{ns}/virtualmachines/{name}",
            "sts": f"/apis/apps/v1/namespaces/{ns}/statefulsets/{name}"}.get(
        kind, f"/apis/apps/v1/namespaces/{ns}/deployments/{name}")


def _partition(sts):
    strategy = (sts.get("spec") or {}).get("updateStrategy") or {}
    return int((strategy.get("rollingUpdate") or {}).get("partition") or 0)


def _rolls(sts):
    """Whether a StatefulSet restarts pods itself when its template changes.
    Not with OnDelete, nor with a partition at or above its replicas: someone
    (often a database operator) restarts those, and Homestead leaves that to
    them. A lower partition restarts the pods at or above it at once."""
    strategy = (sts.get("spec") or {}).get("updateStrategy") or {}
    partition = _partition(sts)
    return strategy.get("type") != "OnDelete" and not (partition > 0 and partition >= _replicas(sts))


def _replicas(obj):
    replicas = (obj.get("spec") or {}).get("replicas")
    return 1 if replicas is None else int(replicas)


def _pod_spec(dep):
    return ((dep.get("spec") or {}).get("template") or {}).get("spec") or {}


def plan(chosen=None, role=None):
    """Every app with its tier now ("current"), the tier it should have
    ("wanted") and where that comes from ("source": its own, its group's or
    the default, #430), its status, and the tier it is set to in the review:
    for an app that needs a restart, the tier it should have; for one still at
    normal with a tier of its own, the suggestion; else what it has. chosen
    maps an app id to a tier. role: who asks; apps they may not change are
    left out, saying why. groups: each group's tier and how many apps it has."""
    apps, skipped, own_seen, values, members = [], [], None, [], {}
    lad = ladder()
    tiers = GROUPS.stored(kget)
    for kind, dep in _workloads():
        meta = dep.get("metadata") or {}
        ns, name = meta.get("namespace", ""), meta.get("name", "")
        app_id = _row_id(kind, ns, name)
        group = GROUPS.group_of(dep) if kind == "app" else ""
        if group and group != GROUPS.OWN_GROUP and (ns, name) != own:
            members[group] = members.get(group, 0) + 1
        current, cls = tier_of(_pod_spec(dep), lad)
        suggested, why = suggestion(dep)
        if not (kind == "app" and (ns, name) == own):
            values.append((priority_of(_pod_spec(dep), lad)[0], app_id))
        if kind == "app" and (ns, name) == own:
            own_seen = (cls, (meta.get("annotations") or {}).get(f"homestead.io/{MARK}") == cls and bool(cls),
                        priority_of(_pod_spec(dep), lad)[0])
            skipped.append({"id": app_id, "why": "Homestead's own tier is critical once any app's is, so none can evict it"})
            continue
        if current == "unknown":
            skipped.append({"id": app_id, "class": cls, "why": f"it names {cls}, which Homestead couldn't check just now; it is left alone"})
            continue
        if current in ("system", "missing"):
            skipped.append({"id": app_id, "class": cls, "why": (f"it names {cls}, which isn't a class here: its new pods are refused"
                                                                if current == "missing" else f"it uses Kubernetes' own class {cls}")})
            continue
        try:
            guard(dep, ns, role)
        except PermissionError as error:
            skipped.append({"id": app_id, "why": str(error)})
            continue
        owner = managed_by(dep)
        row = {"id": app_id, "kind": kind, "ns": ns, "name": name, "current": current, "suggested": suggested or "", "why": why,
               "managed": owner.get("by", ""), "class": cls}
        if owner:
            where = f" ({owner['source']})" if owner.get("source") else ""
            row["note"] = (f"{owner['by']} applies it{where}; set priorityClassName there" if owner.get("detected")
                           else f"marked as updated by {owner['by']}; set priorityClassName there")
        found = GROUPS.resolve(dep, tiers, lad, kind=kind, managed=bool(owner))
        row.update(group=group, follows=kind == "app" and not owner and GROUPS.follows(dep),
                   wanted=found["tier"], source=found["source"],
                   status=GROUPS.status(dep, found, owner),
                   restarts=kind == "app" or (kind == "sts" and _rolls(dep)))
        if row["status"] == "git":
            row["line"] = GROUPS.git_line(owner, found["class"])
        if row["status"] == "restart":
            default = found["tier"]
        elif suggested and current == "normal" and found["source"] == "own" and not owner:
            default = suggested
        else:
            default = current     # in line, outside or unknown: never ticked unseen
        row["tier"] = (chosen or {}).get(app_id, default) if not owner and row["status"] != "unknown_group" else current
        if row["tier"] not in TIERS:
            raise ValueError(f"{app_id}: the tier must be critical, normal or low")
        # Whether the job keeps it following its group (else the tier set
        # becomes its own); the review token binds it.
        row["follow"] = row["follows"] and found["tier"] == row["tier"]
        apps.append(row)
    # What isn't in line first; then by the tier each has now, so a row stays
    # where it is as a tier is picked.
    apps.sort(key=lambda a: (a["status"] == "in_line", {"critical": 0, "normal": 1, "low": 2}[a["current"]], a["id"]))
    changes = [a for a in apps if a["tier"] != a["current"]]
    # The suggestions never start more than one run may change.
    if chosen is None and len(changes) > MAX_APPS:
        for app in changes[MAX_APPS:]:
            app["tier"] = app["current"]
        changes = changes[:MAX_APPS]
    warnings = list(lad["warnings"])
    missing = sorted((s["class"], s["id"]) for s in skipped if "isn't a class here" in s["why"])
    for cls_name in sorted({c for c, _ in missing}):
        ids = [i for c, i in missing if c == cls_name]
        warnings.append(f"{', '.join(ids[:6])} name {cls_name}, which isn't a class on the cluster: their new pods are refused")
    used = {}
    for a in apps:
        named = OURS.fullmatch(a.get("class") or "")
        if named and a["class"] != lad["default"]["name"] and named.group(1) != tier_of({"priorityClassName": a["class"]}, lad)[0]:
            used.setdefault(a["class"], []).append(a["id"])
    for cls_name, ids in sorted(used.items()):
        value = next((r["value"] for r in lad["classes"] if r["name"] == cls_name), None)
        warnings.append(f"{cls_name} ({value}) no longer ranks as its name says; {', '.join(ids[:6])} still use it "
                        "and rank by its value until their tier is set again")
    # Homestead below an app, on a class a person gave it (protect() leaves that alone).
    if own_seen and own_seen[0] and not own_seen[1] and own_seen[2] is not None:
        above = sorted((v, i) for v, i in values if v is not None and v > own_seen[2] and v < SYSTEM_VALUE)
        if above:
            warnings.append(f"Homestead ({own_seen[0]}, {own_seen[2]}) ranks below {above[-1][1]} ({above[-1][0]}), which can "
                            "evict it: give it a class at least as high, or remove its class so Homestead sets its own")
    groups = [{"name": g, "tier": None if tiers is None else tiers.get(g, ""), "apps": n} for g, n in sorted(members.items())]
    return {"apps": apps, "skipped": sorted(skipped, key=lambda s: s["id"]), "max": MAX_APPS,
            "changes": len(changes), "review_token": review_token(changes), "warnings": warnings, "groups": groups}


def review_token(changes):
    rows = sorted([c["id"], c["current"], c["tier"], bool(c.get("follow"))] for c in changes)
    return hashlib.sha256(json.dumps(rows).encode()).hexdigest()[:20]


def reviewed(changes, token, role=None):
    """The tier changes a person reviewed, each app checked as it is now."""
    if not isinstance(changes, list) or not changes:
        raise ValueError("Change at least one app's tier")
    if len(changes) > MAX_APPS:
        raise ValueError(f"At most {MAX_APPS} apps at a time")
    if any(not isinstance(c, dict) or not isinstance(c.get("id"), str) or "/" not in c["id"]
           or c.get("current") not in TIERS or c.get("tier") not in TIERS or c["current"] == c["tier"] for c in changes):
        raise ValueError("Review the tiers again")
    if review_token(changes) != token:
        raise ValueError("The tiers differ from the review; review again")
    tiers = GROUPS.stored(kget)
    out = []
    for c in changes:
        kind, ns, name = _split(c["id"])
        check(ns, name, c["current"], role, kind=kind)
        following = _following(kind, ns, name, c["tier"], tiers)
        # Whether it keeps following its group is part of what was reviewed.
        if not following.get("unreadable") and bool(following.get("follow")) != bool(c.get("follow")):
            raise ValueError(f"{c['id']}'s group tier changed since the review; review again")
        out.append({"id": c["id"], "kind": kind, "ns": ns, "name": name, "current": c["current"], "tier": c["tier"],
                    **following})
    return out


def _following(kind, ns, name, tier, tiers):
    """For an app that follows its group (#430): follow when the tier set is
    its group's - the job writes the marker with the class - else the tier
    becomes the app's own and the marker goes (marked). While the group tiers
    can't be read it is marked unreadable, and start() leaves it out of the
    job, saying so, whatever was reviewed (the plan shows it as unknown_group
    and never ticks it). {} for a VM or a StatefulSet; an app's group and marker
    as they are now go in seen, which check() compares at the app's turn in
    the job."""
    if kind != "app":
        return {}
    dep = kget(_path(kind, ns, name))
    seen = {"seen": _membership(dep)}
    if not GROUPS.follows(dep):
        return seen
    wanted = GROUPS.resolve(dep, tiers, ladder())["tier"]
    if wanted is None:
        return {"unreadable": True, **seen}
    return {"marked": True, "follow": wanted == tier, **seen}


def _membership(dep):
    return {"group": GROUPS.group_of(dep), "marker": GROUPS.follows(dep)}


def check(ns, name, current, role=None, tier=None, kind="app", seen=None):
    """One app as it is now: still one Homestead - and the person who asked -
    may change, and still in the tier it was reviewed in. Returns "applied"
    when it already has the new tier: a change whose answer was lost is waited
    for, not counted as skipped. seen: its group and marker as reviewed
    (#430); one that has changed since is left alone."""
    app_id = _row_id(kind, ns, name)
    try:
        dep = kget(_path(kind, ns, name))
    except urllib.error.HTTPError as error:
        if error.code == 404:
            raise ValueError(f"{app_id} is gone")
        raise
    if (kind == "app" and (ns, name) == own) or ns in system_ns or ns.startswith(SYSTEM) or managed_by(dep):
        raise ValueError(f"{app_id} is not one Homestead changes")
    guard(dep, ns, role)
    now = tier_of(_pod_spec(dep), ladder())[0]
    if tier and now == tier:
        return "applied"
    if seen is not None and kind == "app" and _membership(dep) != seen:
        raise ValueError("it changed since the review, so it was left alone")
    if now != current:
        raise ValueError(f"{app_id}'s tier changed since the review")
    return "ready"


def patch(app):
    """Normal is no class at all: the class is removed rather than set. What
    a template copied from a pod carries (priority, preemptionPolicy) goes
    too: admission refuses a pod whose values are not its class's."""
    cls = class_for(app["tier"], ladder())
    if app.get("kind") == "vm":
        # A VM's pod is made from its template when it starts: no restart now.
        ksend("PATCH", _path("vm", app["ns"], app["name"]), {"spec": {"template": {"spec": {"priorityClassName": cls}}}},
              ctype="application/merge-patch+json")
        return
    kind = app.get("kind", "app")
    body = {"spec": {"template": {"spec": {"priorityClassName": cls, "priority": None, "preemptionPolicy": None}}}}
    # #430: an app that follows its group keeps the marker, with the class
    # Homestead wrote; one given a tier of its own loses both. Never a
    # StatefulSet's: it has no group.
    if kind == "app" and app.get("follow"):
        body["metadata"] = {"annotations": {GROUPS.FOLLOW: "group", GROUPS.WROTE: cls}}
    elif kind == "app" and app.get("marked"):
        body["metadata"] = {"annotations": {GROUPS.FOLLOW: None, GROUPS.WROTE: None}}
    ksend("PATCH", _path(kind, app["ns"], app["name"]), body, ctype="application/strategic-merge-patch+json")


# --------------------------------------------------------------- the job
def _settled(ns, name, kind="app"):
    """Whether its restart is over: every replica updated and ready - for a
    StatefulSet also on its new revision (replicas default to 1 there). A
    partitioned one never fully is: the pods at or above its partition are
    updated, and every pod is ready."""
    obj = kget(_path(kind, ns, name))
    status, spec, meta = obj.get("status") or {}, obj.get("spec") or {}, obj.get("metadata") or {}
    if int(status.get("observedGeneration") or 0) < int(meta.get("generation") or 0):
        return False
    if kind == "sts" and _partition(obj) > 0:
        want = _replicas(obj)
        return (int(status.get("updatedReplicas") or 0) >= want - _partition(obj)
                and int(status.get("readyReplicas") or 0) == int(status.get("replicas") or 0) == want)
    if kind == "sts" and status.get("currentRevision") != status.get("updateRevision"):
        return False
    want = _replicas(obj) if kind == "sts" else int(spec.get("replicas") or 0)
    return int(status.get("updatedReplicas") or 0) == int(status.get("readyReplicas") or 0) == int(status.get("replicas") or 0) == want


def status(item):
    ref = item["ref"]
    apps, i, done = ref["apps"], ref.get("index", 0), ref.get("done", 0)
    if not ref.get("classes"):
        if not is_leader():
            return "running", 0, item.get("message") or "Making the priority classes"
        try:
            ref["notes"] = ensure(sorted({a["tier"] for a in apps}), ladder())
        except (PermissionError, ValueError) as error:
            return "failed", 0, str(error)
        ref["classes"] = True
        return "running", 0, "Priority classes ready" + (f"; {'; '.join(ref['notes'])}" if ref["notes"] else "")
    if i >= len(apps):
        skipped = ref.get("skipped") or []
        return "succeeded", 100, (f"Set the tier of {_named(done - ref.get('done_sts', 0), ref.get('done_sts', 0))}"
                                  + (f"; left {len(skipped)}: " + "; ".join(skipped[:4]) if skipped else ""))
    app = apps[i]
    progress = int(100 * i / max(1, len(apps)))
    if not is_leader():
        return "running", progress, item.get("message") or f"Setting {app['id']}"
    stage, now = ref.get("stage", "apply"), time.time()
    if stage == "apply":
        try:
            if check(app["ns"], app["name"], app["current"], ref.get("role"), app["tier"], app.get("kind", "app"),
                     app.get("seen")) == "ready":
                patch(app)
        except (ValueError, PermissionError, urllib.error.HTTPError) as error:
            if isinstance(error, urllib.error.HTTPError) and error.code not in (403, 404, 409, 422):
                raise
            why = f"Kubernetes refused it ({error.code})" if isinstance(error, urllib.error.HTTPError) else str(error)
            ref.setdefault("skipped", []).append(f"{app['id']}: {why[:160]}")
            ref.update(index=i + 1)
            return "running", progress, f"Skipped {app['id']}: {why[:160]}"
        kind, limit = app.get("kind", "app"), PER_POD
        if kind == "vm":
            ref.update(index=i + 1, done=done + 1)
            return "running", int(100 * (i + 1) / len(apps)), f"{app['name']} takes {app['tier']} when it next starts"
        if kind == "sts":
            # As it is now: its pods are never deleted to apply a tier.
            sts = kget(_path(kind, app["ns"], app["name"]))
            if not _rolls(sts):
                ref.update(index=i + 1, done=done + 1, done_sts=ref.get("done_sts", 0) + 1)
                return ("running", int(100 * (i + 1) / len(apps)),
                        f"{app['name']} takes {app['tier']} when its pods are next restarted")
            limit = PER_POD * max(1, _replicas(sts))   # it restarts its pods in turn
        ref.update(stage="rolling", started=now, limit=limit)
        return "running", progress, f"Setting {app['id']} to {app['tier']} ({i + 1} of {len(apps)}); it restarts"
    if stage == "rolling":
        if _settled(app["ns"], app["name"], app.get("kind", "app")):
            ref.update(index=i + 1, stage="apply", done=done + 1)
            if app.get("kind") == "sts":
                ref["done_sts"] = ref.get("done_sts", 0) + 1
            return "running", int(100 * (i + 1) / len(apps)), f"{app['id']} runs as {app['tier']}"
        limit = ref.get("limit", PER_POD)
        if now - ref.get("started", now) > limit:
            return "failed", progress, (f"{app['id']} did not come back within {limit // 60} minutes; the rest were not changed. "
                                        f"Set {done} before it. Check its events in Containers")
        return "running", progress, f"Waiting for {app['id']} to restart ({i + 1} of {len(apps)})"
    return "failed", progress, "Unknown step; the rest were not changed"


def cancel_plan(item):
    return {"mode": "stop", "action": "Stop setting tiers",
            "undo": ["No more apps are changed"],
            "keeps": ["An app already restarting finishes in its new tier",
                      "Apps already changed keep their new tier; the classes stay"], "needs": "operator"}


def cancel_run(work, chosen):
    ref = work.get("ref") or {}
    done, sets = ref.get("done", 0), ref.get("done_sts", 0)
    return f"Stopped after setting {_named(done - sets, sets)}"


def _named(apps, sets):
    """"2 apps and 1 StatefulSet"; apps alone (VMs among them) as before."""
    words = [f"{apps} app{'' if apps == 1 else 's'}" if apps or not sets else "",
             f"{sets} StatefulSet{'' if sets == 1 else 's'}" if sets else ""]
    return " and ".join(w for w in words if w)


def start(body, role=None):
    """The job for the tiers a person reviewed, checked against the cluster
    now; role is theirs, and each app is checked for them again as it is
    changed. Refused while another such job runs, or without the restarts
    confirmed."""
    running = next((i for i in operations.snapshot() if i.get("kind") == KIND
                    and i.get("status") not in operations.TERMINAL), None)
    if running:
        raise ValueError("Tiers are already being set; follow it in Jobs")
    if (body or {}).get("restart") is not True:
        raise ValueError("A new tier restarts each app; confirm the restarts")
    apps = reviewed(body.get("changes"), body.get("review_token"), role)
    # An app whose group's tier can't be read is never acted on: it is left
    # out and said so, and the rest of the run goes ahead.
    left = [f"{a['id']}: its group's tier couldn't be read, so it was left alone" for a in apps if a.get("unreadable")]
    apps = [a for a in apps if not a.get("unreadable")]
    if not apps:
        raise ValueError("The group tiers couldn't be read just now, so no app was changed; try again shortly")
    sets = sum(1 for a in apps if a.get("kind") == "sts")
    return {"operation": operations.start(KIND, f"Set the priority tier of {_named(len(apps) - sets, sets)}",
                                          {"kind": "Deployment", "name": "priority"}, "/containers",
                                          {"apps": apps, "index": 0, "done": 0, "stage": "apply", "role": role, "skipped": left},
                                          "Making the priority classes")}


# ------------------------------------------------- Homestead's own tier
MARK = "priority-protect"   # homestead.io/priority-protect: the class protect() set itself


def protect():
    """Homestead at least as high as any app, and never below the default, so
    no app can evict it. It marks the class it set itself, and moves itself up
    when an app comes to rank higher; a class a person set is left alone.
    Never while a job runs, which its restart would interrupt, and never on a
    guess: when the classes can't be read, it waits for the next minute."""
    ns, name = own
    if not ns or not name:
        return None
    lad = ladder()
    if lad.get("fallback"):
        return None
    dep = kget(f"/apis/apps/v1/namespaces/{ns}/deployments/{name}")
    meta, spec = dep.get("metadata") or {}, _pod_spec(dep)
    current = spec.get("priorityClassName") or ""
    marked = (meta.get("annotations") or {}).get(f"homestead.io/{MARK}")
    # Its own doing before #429: homestead-critical at 1000, which protect() set unmarked.
    adopted = current == "homestead-critical" and any(r["name"] == current and r["value"] == 1000 for r in lad["classes"])
    if current and not (marked == current or adopted):
        return None
    d = lad["default"]["value"]
    tops = [priority_of(p.get("spec") or {}, lad)[0] for p in kget("/api/v1/pods").get("items", [])
            if (p.get("metadata") or {}).get("namespace") not in system_ns]
    highest = max([t for t in tops if t is not None and t < SYSTEM_VALUE] or [d])
    now_value = priority_of(spec, lad)[0] if current else d

    def busy():
        return operations and any(i.get("status") not in operations.TERMINAL for i in operations.snapshot())

    def send(cls):
        ksend("PATCH", f"/apis/apps/v1/namespaces/{ns}/deployments/{name}",
              {"metadata": {"annotations": {f"homestead.io/{MARK}": cls}},
               "spec": {"template": {"spec": {"priorityClassName": cls, "priority": None, "preemptionPolicy": None}}}},
              ctype="application/strategic-merge-patch+json")
    if highest <= d:
        # Nothing ranks above the default; a class of its own below it goes.
        if current and now_value is not None and now_value < d and not busy():
            send(None)
            return f"Homestead runs at the cluster default again, not {current} below it; it restarts once"
        return None
    candidates = [r for r in lad["classes"] if not r["system"] and r["preempts"] and r["value"] >= highest]
    try:
        target = min(candidates, key=lambda r: (r["value"], r["name"]))["name"] if candidates else class_for("critical", lad)
    except ValueError:
        return None   # no class can rank above the default; Priority tiers says so
    target_value = next((r["value"] for r in lad["classes"] if r["name"] == target), highest)
    if current == target or (now_value is not None and now_value >= target_value):
        return None
    if busy():
        return None
    if not candidates:
        ensure(("critical",), lad)
    send(target)
    return f"Homestead runs as {target} now, so no app can evict it; it restarts once"


def run(sleep=time.sleep):
    """Every minute on the leader: Homestead's own tier (protect)."""
    while True:
        if is_leader():
            try:
                said = protect()
                if said:
                    print(f"priority: {said}", flush=True)
            except Exception as error:
                print(f"priority: {str(error)[:160]}", flush=True)
        sleep(60)


def _picks(query):
    """?chosen=ns/app=tier,...: the tiers asked for, or None for the defaults."""
    chosen = query.get("chosen")
    return dict(item.split("=", 1) for item in chosen[0].split(",") if "=" in item) if chosen else None


ROUTES = {
    ("GET", "/api/workloads/priority/plan"): ("viewer", lambda request: plan(_picks(request.query), request.role)),
    ("POST", "/api/workloads/priority"): ("operator", lambda request: start(request.body, request.role)),
}
