"""Apps that git applies: Flux or Argo CD, or an administrator's mark that
something else does (homestead_updates.managed_by).

A change Homestead makes to such an app lasts until that tool next applies
it, usually within minutes. A review that writes an app's Deployment says so
first (#734), and a rename, which that tool would turn into two copies,
needs its own acknowledgement.
"""
import copy
import datetime
import json
import re
import time
import urllib.error

import homestead_kube as KUBE
import homestead_names as NAMES
import homestead_routes as ROUTER
import homestead_updates as UPDATES

# Every review that writes a Deployment's spec. Each answers with "managed",
# so the page and the MCP tools both see who applies the app
# (tests/test_gitops_notice.py keeps this list complete).
REVIEWS = (("POST", "/api/edit/preview"), ("POST", "/api/move/preview"))


def notice(dep):
    """Who applies this app instead of Homestead: {} when nobody does."""
    return UPDATES.managed_by(dep or {})


def require_rename_ack(dep, body):
    """A rename makes the app under its new name and removes the old one;
    Flux or Argo CD then brings the old one back. Refused until the person
    (or the tool acting for them) says they know."""
    managed = notice(dep)
    if managed and body.get("confirm_managed") is not True:
        where = f" ({managed['source']})" if managed.get("source") else ""
        raise ValueError(f"{managed['by']} applies this app{where} and will bring back the old name, so there "
                         "would be two copies. Rename it in git instead, or confirm with confirm_managed: true")


# ------------------------------------------------- the change, as YAML (#735)
# What Homestead keeps for itself on an app - update history, the digests it
# last ran, restart stamps - is not a setting a person commits; the settings
# they chose (group, logo, monitoring) are, and stay in.
BOOKKEEPING = frozenset((UPDATES.TRACKED, UPDATES.PREVIOUS, UPDATES.LAST_ACTION, UPDATES.RAN, UPDATES.ROLLOUT_AT,
                         NAMES.key("editedAt"), "kubectl.kubernetes.io/restartedAt", "deployment.kubernetes.io/revision"))
_UNCHANGED = object()
# Anything a YAML 1.1 reader (Kubernetes, Flux) or a 1.2 one could take for
# something other than a string: numbers in any base, sexagesimal, bools and
# their yes/no/on/off cousins, nulls, infinities.
_TYPED = re.compile(r"[-+]?(\.?[0-9][0-9_.:eE+-]*|0[xXoObB][0-9a-fA-F_]+|\.(inf|Inf|INF|nan|NaN|NAN))"
                    r"|y|Y|yes|Yes|YES|n|N|no|No|NO|true|True|TRUE|false|False|FALSE|on|On|ON|off|Off|OFF"
                    r"|null|Null|NULL|~|<<|=")


def _scalar(value):
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return json.dumps(value)
    text = str(value)
    plain = (text and text == text.strip() and not _TYPED.fullmatch(text) and text[0] not in "-?:,[]{}#&*!|>'\"%@`"
             and ": " not in text and " #" not in text and not text.endswith(":")
             and all(ch.isprintable() for ch in text))
    return text if plain else json.dumps(text)


def _lines(value, indent):
    pad = " " * indent
    if isinstance(value, dict):
        out = []
        for key, item in value.items():
            head = f"{pad}{_scalar(key)}:"
            if isinstance(item, (dict, list)) and item:
                out.append(head)
                out += _lines(item, indent + 2 if isinstance(item, dict) else indent)
            else:
                out.append(f"{head} {'{}' if item == {} else '[]' if item == [] else _scalar(item)}")
        return out
    out = []
    for item in value:
        if isinstance(item, (dict, list)) and item:
            inner = _lines(item, indent + 2)
            out.append(f"{pad}- {inner[0].lstrip()}")
            out += inner[1:]
        else:
            out.append(f"{pad}- {'{}' if item == {} else '[]' if item == [] else _scalar(item)}")
    return out


def dump(value):
    """Block YAML for plain data, with no library: Homestead runs on the
    standard library alone. A string is quoted whenever a reader could type
    it as anything else."""
    return "\n".join(_lines(value, 0))


def dump_all(objects):
    """Several objects as one multi-document YAML file, each after ---; a
    document already written (document()'s patch) is taken as it is."""
    return "\n---\n".join(value if isinstance(value, str) else dump(value) for value in objects if value) + "\n"


# What the API server writes on an object, never a setting to commit.
SERVER_FIELDS = ("managedFields", "uid", "resourceVersion", "creationTimestamp", "generation", "selfLink",
                 "deletionTimestamp", "deletionGracePeriodSeconds", "ownerReferences")
# kubectl's copy of the last object it applied: on a live object it holds
# every value, env included, so it never reaches the YAML.
LAST_APPLIED = "kubectl.kubernetes.io/last-applied-configuration"


def stripped(obj):
    """A whole object to commit (#920): no status or server fields, and of
    its labels and annotations only the settings (_settings), in the pod
    template too. A copy; obj is left as it was."""
    out = {key: copy.deepcopy(value) for key, value in obj.items() if key != "status"}
    for holder in (out, (out.get("spec") or {}).get("template") or {}):
        meta = holder.get("metadata")
        if not isinstance(meta, dict):
            continue
        for key in SERVER_FIELDS:
            meta.pop(key, None)
        (meta.get("annotations") or {}).pop(LAST_APPLIED, None)
        for key, value in _settings(meta).items():
            if value:
                meta[key] = value
            else:
                meta.pop(key, None)
    return out


def _named(rows):
    return isinstance(rows, list) and rows and all(isinstance(r, dict) and isinstance(r.get("name"), str) for r in rows)


def _diff(old, new):
    if old == new:
        return _UNCHANGED
    if isinstance(old, dict) and isinstance(new, dict):
        out = {}
        for key, value in new.items():
            change = _diff(old.get(key, _UNCHANGED), value) if key in old else value
            if change is not _UNCHANGED:
                out[key] = change
        out.update({key: None for key in old if key not in new})
        return out or _UNCHANGED
    if _named(old) and _named(new):
        # Containers, env, volumes: merged by name, as a strategic merge patch
        # (and a kustomize patch) does. One gone is deleted by name.
        before = {row["name"]: row for row in old}
        out = []
        for row in new:
            change = _diff(before[row["name"]], row) if row["name"] in before else row
            if change is not _UNCHANGED:
                out.append({"name": row["name"], **change})
        names = {row["name"] for row in new}
        out += [{"name": row["name"], "$patch": "delete"} for row in old if row["name"] not in names]
        return out or _UNCHANGED
    return copy.deepcopy(new)


def _settings(meta):
    return {key: dict((value for value in (meta.get(key) or {}).items() if value[0] not in BOOKKEEPING))
            for key in ("labels", "annotations")}


def patch(current, proposed):
    """Only what changed from current to proposed: spec, and the labels and
    annotations that are settings. {} when nothing a person commits did."""
    out = {}
    meta = _diff(_settings(current.get("metadata") or {}), _settings(proposed.get("metadata") or {}))
    if meta is not _UNCHANGED:
        out["metadata"] = meta
    old_spec, new_spec = copy.deepcopy(current.get("spec") or {}), copy.deepcopy(proposed.get("spec") or {})
    for spec in (old_spec, new_spec):
        template = (spec.get("template") or {}).get("metadata")
        if isinstance(template, dict):
            template.update(_settings(template))
    spec = _diff(old_spec, new_spec)
    if spec is not _UNCHANGED:
        out["spec"] = spec
    return out


def document(dep, body):
    """body under the object's kind, name and namespace: a patch to commit
    where Flux or Argo CD reads the app from - a kustomize patch, or the same
    fields in the Deployment itself."""
    meta = dep.get("metadata") or {}
    head = {"apiVersion": dep.get("apiVersion") or "apps/v1", "kind": dep.get("kind") or "Deployment",
            "metadata": {"name": meta.get("name", ""), "namespace": meta.get("namespace", ""),
                         **((body.get("metadata") or {}))}}
    return dump({**head, **{key: value for key, value in body.items() if key != "metadata"}})


def git_image(item):
    """An update check's image line for git: the new tag, or - when only what
    the tag points at moved - the tag pinned to the new digest. An image
    pinned in git (tag@digest) stays pinned: the new tag at its digest (#874)."""
    pinned = "@" in (item.get("deployed") or "")
    if (item.get("candidate") != item.get("deployed") and not pinned) or not item.get("remote_digest"):
        return item["candidate"]
    return f"{item['candidate'].split('@', 1)[0]}@{item['remote_digest']}"


def image_yaml(dep, images):
    """An image bump to commit: the image: line of each container that
    changes ({container: image}), init containers under their own key."""
    pod = ((dep.get("spec") or {}).get("template") or {}).get("spec") or {}
    spec = {}
    for key in ("initContainers", "containers"):
        rows = [{"name": c["name"], "image": images[c["name"]]} for c in pod.get(key) or [] if c.get("name") in images]
        if rows:
            spec[key] = rows
    return document(dep, {"spec": {"template": {"spec": spec}}})


# ------------------------------------------------- is Flux applying it? (#742)
# Each Kustomization and HelmRelease, whether it is Ready, what it last
# applied and Flux's message when it is not. Homestead's read permission
# covers every kind (deploy/rbac.yaml); Reconcile now, below, is the one write.
# Argo CD's Application has another shape and is not read here.
FLUX = (("Kustomization", "kustomize.toolkit.fluxcd.io", ("v1", "v1beta2"), "kustomizations"),
        ("HelmRelease", "helm.toolkit.fluxcd.io", ("v2", "v2beta2", "v2beta1"), "helmreleases"))
EVERY = 60          # seconds: one read a minute, shared by every page and the alerts
ALERT_AFTER = 600   # seconds not Ready before it is news: one Flux interval is often 10 minutes


def _list(group, versions, plural):
    """Every object of a Flux kind; [] when Flux is not installed. An older
    Flux serves an older version: tried in turn."""
    for version in versions:
        try:
            return KUBE.items(f"/apis/{group}/{version}/{plural}", optional_group=group)
        except urllib.error.HTTPError as error:
            if error.code != 404 or version == versions[-1]:
                raise
            error.close()
    return []


def _revision(kind, status):
    if kind == "HelmRelease":
        last = (status.get("history") or [{}])[0]
        if last.get("chartVersion"):
            return " ".join(x for x in (last.get("chartName", ""), last["chartVersion"]) if x)
        return str(status.get("lastAttemptedRevision") or "")
    # main@sha1:<40 hex> reads as main@a1b2c3d, as git shows it.
    match = re.fullmatch(r"(.*)@sha(?:1|256):([0-9a-f]{7})[0-9a-f]*", str(status.get("lastAppliedRevision") or ""))
    return f"{match.group(1)}@{match.group(2)}" if match else str(status.get("lastAppliedRevision") or "")


def _row(kind, obj):
    meta, spec, status = obj.get("metadata") or {}, obj.get("spec") or {}, obj.get("status") or {}
    cond = next((c for c in status.get("conditions") or [] if c.get("type") == "Ready"), {})
    last = (status.get("history") or [{}])[0]
    ns, name = meta.get("namespace", ""), meta.get("name", "")
    return {"id": f"{kind} {ns}/{name}", "kind": kind, "namespace": ns, "name": name,
            "ready": {"True": True, "False": False}.get(cond.get("status")),
            "suspended": spec.get("suspend") is True, "revision": _revision(kind, status),
            "tried": last.get("lastReconciled") or last.get("lastDeployed") or cond.get("lastTransitionTime", ""),
            "since": cond.get("lastTransitionTime", ""), "interval": spec.get("interval", ""),
            "message": str(cond.get("message") or "")[:400],
            # Reconcile now: when one was last asked for, and the request Flux
            # last handled; the same value once Flux has taken it.
            "requested": str((meta.get("annotations") or {}).get(REQUESTED) or ""),
            "handled": str(status.get("lastHandledReconcileAt") or "")}


def _plural(count, word):
    return f"{count} {word}{'' if count == 1 else 's'}"


def _read():
    objects = [_row(kind, obj) for kind, group, versions, plural in FLUX for obj in _list(group, versions, plural)]
    if not objects:
        return {"objects": [], "apps": {}, "summary": ""}
    ids = {row["id"] for row in objects}
    apps = {}
    for dep in KUBE.items("/apis/apps/v1/deployments"):
        managed = notice(dep)
        source = managed.get("source", "") if managed.get("by") == "Flux" else ""
        if source in ids:
            meta = dep.get("metadata") or {}
            apps[f"{meta.get('namespace', '')}/{meta.get('name', '')}"] = source
    counts = ", ".join(_plural(sum(r["kind"] == kind for r in objects), kind) for kind, *_ in FLUX
                       if any(r["kind"] == kind for r in objects))
    failing = sum(r["ready"] is False and not r["suspended"] for r in objects)
    suspended = sum(r["suspended"] for r in objects)
    state = ", ".join(part for part in (failing and f"{failing} not ready", suspended and f"{suspended} suspended") if part)
    return {"objects": objects, "apps": apps, "summary": f"{counts}: {state or 'all ready'}"}


def status():
    """Flux's objects, the apps each applies (ns/name -> id) and one line.
    Read every FOLLOW_EVERY seconds for a while after Reconcile now, so the
    page sees Flux take the request."""
    return ROUTER.cached("gitops", FOLLOW_EVERY if time.time() < _following[0] else EVERY, _read)


# ------------------------------------------------- Reconcile now (#742 part 2)
# The standard request Flux answers, as `flux reconcile` makes it: the
# annotation, set to a new value, on the Kustomization or HelmRelease that
# applies the app. Flux applies what git has and copies the value to
# status.lastHandledReconcileAt. Nothing else on the object is touched.
REQUESTED = "reconcile.fluxcd.io/requestedAt"
FOLLOW_EVERY = 5     # seconds between reads while following a request
FOLLOW_FOR = 120     # seconds a request is followed
_following = [0.0]


def _object(row):
    """The live object and its path, at the version this cluster serves."""
    _, group, versions, plural = next(entry for entry in FLUX if entry[0] == row["kind"])
    for version in versions:
        path = KUBE.api_path(f"/apis/{group}/{version}/namespaces/{row['namespace']}/{plural}/{row['name']}")
        found = KUBE.get_optional(path)
        if found is not None:
            return path, found
    raise ValueError(f"{row['id']} is no longer there")


def reconcile(request):
    body = request.body if isinstance(request.body, dict) else {}
    ns, name = body.get("ns"), body.get("name")
    if not (isinstance(ns, str) and ns and isinstance(name, str) and name):
        raise ValueError("say which app: ns and name")
    report = status()
    source = report["apps"].get(f"{ns}/{name}")
    row = next((r for r in report["objects"] if r["id"] == source), None)
    if row is None:
        raise ValueError(f"Flux does not apply {ns}/{name}")
    path, live = _object(row)
    if (live.get("spec") or {}).get("suspend") is True:
        raise ValueError(f"{row['id']} is suspended, so Flux would not apply it: resume it in git first")
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    KUBE.ksend("PATCH", path, {"metadata": {"annotations": {REQUESTED: stamp}}}, ctype="application/merge-patch+json")
    _following[0] = time.time() + FOLLOW_FOR
    ROUTER.forget("gitops")
    return {"ok": True, "id": row["id"], "requested": stamp}


def alert_facts():
    """One per Kustomization or HelmRelease not Ready for ALERT_AFTER, until it
    is again. A suspended one is left alone: someone paused it. A failed read
    raises, so the alerts keep what they had rather than clearing."""
    return [{"key": f"gitops:{row['kind']}:{row['namespace']}/{row['name']}", "category": "degraded",
             "severity": "degraded", "hold": ALERT_AFTER,
             "title": f"Flux has not applied {row['id']}",
             # Flux's own message can name repositories, paths and Secrets: it
             # stays on the Cluster page, not in alerts sent to every device.
             "body": "Changes made in git are not reaching the cluster. System › Cluster shows Flux's message.",
             "resolved": f"Flux applies {row['id']} again", "href": "/system/cluster"}
            for row in status()["objects"] if row["ready"] is False and not row["suspended"]]


ROUTES = {
    ("GET", "/api/gitops"): ("viewer", lambda request: status()),
    ("POST", "/api/gitops/reconcile"): ("admin", reconcile),
}
