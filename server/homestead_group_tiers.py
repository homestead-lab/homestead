"""Priority tiers for a group of apps, and where each app's tier comes from (#430).

A group is an annotation on each Deployment (homestead.io/group), so it needs
no list of its own: a group lasts while something is in it. A group's tier is
an entry in one ConfigMap of Homestead's, homestead-group-tiers, key
group-tiers.json: {"<group>": "critical" | "normal" | "low"}. A group with no
tier has no entry. Like the portal's links, it is kept apart from the
settings, since it is a map that grows. It is not in git: a cluster rebuilt
from git keeps each app's class, but its group tiers must be set again.

An app follows its group when it carries homestead.io/tier: group. Its tier is
then its group's, or the cluster default (normal, no class) when the group has
none; homestead.io/tier-class records the class Homestead wrote for it, so a
class someone changed afterwards is told apart from a group that changed. An
app without the marker has a tier of its own, read from its class. So the
order is the app's own tier, its group's, then the default, and a class alone
never means "from the group". Apps Flux or Argo CD apply never get the
marker, but their tier is compared with their group's. VMs have no groups.

Setting a group's tier writes only the map: nothing restarts. Priority tiers
then lists the apps that need a restart, and its reviewed job makes them.
"""
import json
import urllib.error

import homestead_names as NAMES
import homestead_routes as ROUTER
# homestead_priority imports this module too; each reads the other's names
# only when called, so either may be imported first.
import homestead_priority as PRIORITY

DATA_KEY = "group-tiers.json"
TIERS = ("critical", "normal", "low")
GROUP = NAMES.key("group")        # homestead.io/group: the group an app is in
FOLLOW = NAMES.key("tier")        # homestead.io/tier: group - it follows its group
WROTE = NAMES.key("tier-class")   # homestead.io/tier-class: the class Homestead wrote for it
DEPLOYMENTS = "/apis/apps/v1/deployments"

# server.py's helpers, bound by name (bind), so a test that replaces one in
# server.py replaces it here too; and its values.
NEEDS = ("kget", "ksend", "guard_managed_smb", "_dns_name", "is_self")
kget = ksend = guard_managed_smb = _dns_name = None
is_self = lambda ns, name: False
managed_by = lambda dep: {}
NAMESPACE = "default"
OWN_GROUP = "Homestead"


def bind(helpers, *, namespace, own_group, managed):
    global NAMESPACE, OWN_GROUP, managed_by
    missing = set(NEEDS) - set(helpers)
    if missing:
        raise TypeError("homestead_group_tiers needs " + ", ".join(sorted(missing)))
    globals().update({name: helpers[name] for name in NEEDS})
    NAMESPACE, OWN_GROUP, managed_by = namespace, own_group, managed


def _map():
    return NAMES.object_name("group-tiers")


def _path():
    return f"/api/v1/namespaces/{NAMESPACE}/configmaps/{_map()}"


def _meta(obj):
    return (obj or {}).get("metadata") or {}


def _annotations(dep):
    return _meta(dep).get("annotations") or {}


def _spec(dep):
    return (((dep or {}).get("spec") or {}).get("template") or {}).get("spec") or {}


def group_of(dep):
    """The group an app is in, or ""."""
    return _annotations(dep).get(GROUP) or ""


def follows(dep):
    """Whether an app carries the marker: it follows its group."""
    return _annotations(dep).get(FOLLOW) == "group"


def read(get=None):
    """(tiers, resourceVersion). No map yet is ({}, None). Raises when the map
    can't be read; tiers is None when what it holds isn't a map of tiers."""
    try:
        cm = (get or kget)(_path())
    except urllib.error.HTTPError as error:
        if error.code == 404:
            return {}, None
        raise
    version = _meta(cm).get("resourceVersion")
    try:
        raw = json.loads(((cm or {}).get("data") or {}).get(DATA_KEY) or "{}")
    except ValueError:
        return None, version
    if not isinstance(raw, dict):
        return None, version
    return {str(group): tier for group, tier in raw.items() if tier in TIERS}, version


def stored(get=None):
    """The group tiers, or None when they can't be read just now - never an
    empty map in its place. get: the reader to use; the plan and Edit pass
    their own module's."""
    try:
        return read(get)[0]
    except Exception:
        return None


def _class(tier, lad):
    try:
        return PRIORITY.class_for(tier, lad)
    except ValueError:
        return None   # no class can rank there; Priority tiers' warnings say so


def resolve(dep, tiers, ladder, *, kind="app", managed=False):
    """Where an app's tier comes from: {tier, class, source, runs}.

    tier and class: what it should run with (class None for no class).
    source: "own" without the marker; "group" with it, when its group has a
    tier; "default" with it, when it has no group or the group has no tier.
    runs: the tier its template has now. tiers is None when the map can't be
    read: an app that follows a group then has tier None. managed: Flux or
    Argo CD applies it - it never follows, but its group's tier, if there is
    one, is what it should run with. VMs and StatefulSets are always their own."""
    runs, cls = PRIORITY.tier_of(_spec(dep), ladder)
    own = {"tier": runs, "class": cls or None, "source": "own", "runs": runs}
    if kind in ("vm", "sts"):
        return own
    group = group_of(dep)
    following = follows(dep) and not managed
    if not following and not (managed and group):
        return own
    if group and tiers is None:
        return {"tier": None, "class": None, "source": "group", "runs": runs}
    tier = tiers.get(group) if group else None
    if tier:
        return {"tier": tier, "class": _class(tier, ladder), "source": "group", "runs": runs}
    if managed:
        return own
    return {"tier": "normal", "class": None, "source": "default", "runs": runs}


CHANGED = "Group tiers changed elsewhere; try again"


def workload_group(value):
    """A group's name as stored: its own words, trimmed, one line."""
    group = " ".join(str(value or "").split())
    if len(group) > 40:
        raise ValueError("a group name is at most 40 characters")
    return group


def _counts(get=None):
    """How many apps each group has."""
    counts = {}
    for dep in (get or kget)(DEPLOYMENTS).get("items", []):
        group = group_of(dep)
        if group:
            counts[group] = counts.get(group, 0) + 1
    return counts


def _write(tiers, version):
    """The map, saved over the version read. A save that crosses another is
    the API server's 409, passed on - never merged blindly."""
    body = {"apiVersion": "v1", "kind": "ConfigMap",
            "metadata": {"name": _map(), "namespace": NAMESPACE, "labels": {NAMES.key("managed"): "true"}},
            "data": {DATA_KEY: json.dumps(dict(sorted(tiers.items())), indent=1)}}
    if version is None:
        ksend("POST", f"/api/v1/namespaces/{NAMESPACE}/configmaps", body)
    else:
        body["metadata"]["resourceVersion"] = version
        ksend("PUT", _path(), body)


def _joins(dep):
    """Whether an app just put in a group follows it - joining means
    following - unless it has a class of its own, Flux or Argo CD applies it,
    it is Homestead (protect() keeps its tier), or it follows already, as an
    app moved from another group does. dep is the API server's answer to the
    move; without one, it doesn't."""
    if not isinstance(dep, dict) or follows(dep) or _spec(dep).get("priorityClassName"):
        return False
    meta = _meta(dep)
    return not is_self(meta.get("namespace", ""), meta.get("name", "")) and not managed_by(dep)


def _tidy(group, renamed_from):
    """After a change of groups: a renamed group's tier moves to its new name,
    unless that name has a tier already, and a group nothing is in any more
    loses its tier. "" when done; else what to check. Never fails the change
    of groups, which is made by then."""
    for attempt in (1, 2):
        try:
            tiers, version = read()
            if not tiers:
                return ""
            counts = _counts()
            kept = dict(tiers)
            if renamed_from and group and renamed_from != group and renamed_from in kept and not counts.get(renamed_from):
                kept.setdefault(group, kept[renamed_from])
            kept = {name: tier for name, tier in kept.items() if counts.get(name)}
            if kept != tiers:
                _write(kept, version)
            return ""
        except urllib.error.HTTPError as error:
            if error.code == 409 and attempt == 1:
                continue      # changed meanwhile: read it again and redo this on what is there now
            break
        except (OSError, ValueError):
            break
    return "the group tiers couldn't be brought up to date: check them in Priority tiers…"


def set_workload_groups(b):
    """Puts workloads in a group, or out of every group with a blank name.

    The group is an annotation on each Deployment, so it travels with the
    workload and needs no list of its own: a group exists while something is
    in it. An app that joins a group follows its tier (the marker), unless it
    has a tier of its own; one that leaves every group stops following; one
    moved between groups follows its new group. renamed_from: the group's old
    name, when the Manage groups dialog renames it, so its tier moves too; a
    rename moves its apps, it doesn't join them, so none starts following.
    Only annotations change, so nothing restarts."""
    group = workload_group(b.get("group"))
    renamed_from = workload_group(b.get("renamed_from"))
    items = b.get("items") or []
    if not items:
        raise ValueError("choose at least one workload")
    targets = [(_dns_name(item.get("ns"), "namespace"), _dns_name(item.get("name"), "workload name"))
               for item in items]
    annotations = {GROUP: group or None}
    if not group:
        annotations.update({FOLLOW: None, WROTE: None})
    following = 0
    for ns, name in targets:
        guard_managed_smb(ns, name)
        path = f"/apis/apps/v1/namespaces/{ns}/deployments/{name}"
        dep = ksend("PATCH", path, {"metadata": {"annotations": annotations}}, ctype="application/merge-patch+json")
        if group and not renamed_from and _joins(dep):
            ksend("PATCH", path, {"metadata": {"annotations": {FOLLOW: "group"}}}, ctype="application/merge-patch+json")
            following += 1
    ROUTER.forget("wl")
    count = len(items)
    detail = f"{count} workload{'s' if count != 1 else ''} " + (f"moved to {group}" if group else "ungrouped")
    note = _tidy(group, renamed_from)
    return {"ok": True, "group": group, "following": following, "detail": detail + (f"; {note}" if note else "")}


def effect(deps, tiers, lad=None):
    """What a group's tiers mean for these apps now: how many follow their
    group, how many of those need a restart to run with its tier, and how many
    Flux or Argo CD apply that differ from it (to change in git)."""
    lad = lad if lad is not None else PRIORITY.ladder()
    out = {"follow": 0, "restart": 0, "git": 0}
    for dep in deps:
        owner = bool(managed_by(dep))
        where = resolve(dep, tiers, lad, managed=owner)
        if owner:
            out["git"] += where["tier"] != where["runs"]
        elif follows(dep):
            out["follow"] += 1
            out["restart"] += where["tier"] != where["runs"]
    return out


def _said(group, tier, counts):
    head = f"{group} has no tier now" if tier == "none" else f"{group} is {tier} now"
    n, restart, git = counts["follow"], counts["restart"], counts["git"]
    parts = [f"{n} app{' follows' if n == 1 else 's follow'} it" if n else "no app follows it yet"]
    if restart:
        parts.append(f"{restart} need{'s' if restart == 1 else ''} a restart. Nothing restarts now: "
                     f"start {'it' if restart == 1 else 'them'} from Priority tiers…")
    if git:
        parts.append(f"{git} applied by Flux or Argo CD must change in git")
    return f"{head}: " + "; ".join(parts)


def set_group_tier(b):
    """A group's tier: critical, normal, low, or none to remove it. Only the
    map is written - nothing restarts; Priority tiers then lists the apps that
    need a restart. Homestead's own group is refused (it looks after its own
    tier), as is a group no app is in."""
    group = workload_group(b.get("group"))
    tier = str(b.get("tier") or "")
    if not group:
        raise ValueError("choose a group")
    if tier not in TIERS + ("none",):
        raise ValueError("the tier must be critical, normal, low or none")
    if group == OWN_GROUP:
        raise ValueError(f"{OWN_GROUP} looks after its own tier: it ranks at least as high as any app")
    members = [dep for dep in kget(DEPLOYMENTS).get("items", []) if group_of(dep) == group]
    if not members:
        raise ValueError(f"{group} has no apps; put apps in it first")
    tiers, version = read()
    new = dict(tiers or {})   # a map that isn't one is replaced
    if tier == "none":
        new.pop(group, None)
    else:
        new[group] = tier
    if new != tiers:
        try:
            _write(new, version)
        except urllib.error.HTTPError as error:
            if error.code == 409:
                return ROUTER.Reply(409, {"error": CHANGED})
            raise
    counts = effect(members, new)
    return {"ok": True, "group": group, "tier": "" if tier == "none" else tier, **counts,
            "detail": _said(group, tier, counts)}


ROUTES = {
    ("POST", "/api/workloads/group"): ("operator", lambda request: set_workload_groups(request.body)),
    ("POST", "/api/workloads/group-tier"): ("operator", lambda request: set_group_tier(request.body)),
}


STATUSES = ("in_line", "restart", "git", "outside", "cant_set", "unknown_group")


def status(dep, where, owner):
    """A plan row's status, from resolve()'s answer (where) and who applies
    it (owner, managed_by's answer). cant_set is the plan's skipped list.
    in_line: it runs with its tier. unknown_group: its group's tier can't be
    read. git: Flux or Argo CD applies it, and it differs. restart: Homestead
    applies it, it differs, and its class is the one Homestead wrote (or none
    yet). outside: it follows its group, but its class isn't the one
    Homestead wrote - someone changed it, and the job won't quietly undo it."""
    if where["tier"] is None:
        return "unknown_group"
    if where["tier"] == where["runs"]:
        return "in_line"
    if owner:
        return "git"
    if (_spec(dep).get("priorityClassName") or "") != (_annotations(dep).get(WROTE) or ""):
        return "outside"
    return "restart"


def git_line(owner, cls):
    """The line to commit for an app Flux or Argo CD applies: its manifest's
    field, or for a HelmRelease the chart's value (usually priorityClassName)."""
    helm = str(owner.get("source") or "").startswith("HelmRelease")
    field = "priorityClassName" if helm else "spec.template.spec.priorityClassName"
    if cls:
        return f"{field}: {cls}"
    return f"remove {field}" + (" from the chart's values" if helm else "")


def edit_tier(dep, choice, get=None):
    """The tier Edit's choice means: a tier as it is, or "group" - its group's
    tier, normal when the group has none. Refused for an app in no group (or
    in Homestead's), and while the group tiers can't be read."""
    if choice != "group":
        return choice
    group = group_of(dep)
    if not group or group == OWN_GROUP:
        raise ValueError(f"{_meta(dep).get('name', 'it')} isn't in a group; choose a tier")
    tiers = stored(get)
    if tiers is None:
        raise ValueError("Homestead could not read the group tiers just now. Try again, or choose a tier.")
    return tiers.get(group) or "normal"


def edit_fields(dep, tier, get=None):
    """What Edit shows for an app's tier: priority - "group" for an app that
    follows its group, else the tier given (as tier_of reads it, or system,
    missing, unknown); its group, "" for none or Homestead's own; and that
    group's tier, "" for none or None when it can't be read. Sent back
    unchanged, "group" keeps the app following. An app Flux or Argo CD
    applies shows its own tier, marker or not: it never follows."""
    group = group_of(dep)
    group = "" if group == OWN_GROUP else group
    tiers = stored(get) if group else {}
    following = group and follows(dep) and not managed_by(dep)   # Flux's or Argo CD's: shown as its own, read-only
    return {"priority": "group" if following and tier in TIERS else tier,
            "priority_group": group, "priority_group_tier": None if tiers is None else tiers.get(group, "")}
