"""Change history: who changed each app, when, and what - and undo.

Every minute the leading replica reads each app's Deployment and boils it down
to what a person changes: images, environment, CPU and memory, ports, volumes,
copies, command, host network, LAN attachment and placement. When that differs
from what it saw last, the difference is a change, recorded with the settings
from before it so it can be undone.

Who made it: every write Homestead sends to a Deployment is noted with the
person whose request sent it (a job of Homestead's own has none). A change
with no such note in the minute before it was made outside Homestead -
kubectl, Helm, GitOps - and says so.

Values that may be secret are never kept in the readable summary: an
environment variable from a Secret is "from Secret x", and a plain one whose
name looks like a password or token is "(hidden)". The settings kept for undo
are the pod template as it was, which is what Kubernetes itself already holds.

The history is a file on the data volume, written only when something changed,
KEEP entries per app for KEEP_DAYS.
"""
import copy
import hashlib
import json
import os
import re
import threading
import time

import homestead_routes
import homestead_shared as SHARED

DATA_DIR = "/data"
KEEP = 50
KEEP_DAYS = 180
NOTE_WINDOW = 150           # a write noted this long before a change made it
SENSITIVE = re.compile(r"pass|secret|token|key|credential|auth|cookie|private|salt", re.I)
_lock = threading.Lock()
_notes = []                 # (at, ns, name, who) of Homestead's own writes
_notes_lock = threading.Lock()
REQUEST = threading.local() # who the request on this thread is for
BY_SCHEDULE = "schedule"    # a power schedule's stop or start: not a change to record


# server.py's review of an undo, and its writer (bind).
_bound = {"plan": None, "ksend": None}


def bind(data_dir="/data", plan=None, ksend=None):
    global DATA_DIR
    DATA_DIR = data_dir
    if plan:
        _bound.update(plan=plan, ksend=ksend)


def _path():
    return os.path.join(DATA_DIR, "change-history.json")


def _load():
    try:
        with open(_path(), encoding="utf-8") as handle:
            data = json.load(handle)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _save(data):
    SHARED.write_json(_path(), data, separators=(",", ":"), sort_keys=True, mode=0o600)


# A Deployment, or its scale - how Stop, Start and schedules change copies.
_DEPLOYMENT = re.compile(r"^/apis/apps/v1/namespaces/([a-z0-9-]+)/deployments(?:/([a-z0-9.-]+)(?:/scale)?)?(?:\?.*)?$")


def note_write(method, path, body=None, now=None):
    """Homestead is about to write a Deployment: remember who for."""
    if method not in ("POST", "PUT", "PATCH"):
        return
    m = _DEPLOYMENT.match(path or "")
    if not m:
        return
    name = m.group(2) or ((body or {}).get("metadata") or {}).get("name") or ""
    who = getattr(REQUEST, "user", None) or ""
    now = now or time.time()
    with _notes_lock:
        _notes.append((now, m.group(1), name, who))
        cutoff = now - NOTE_WINDOW * 4
        while _notes and _notes[0][0] < cutoff:
            _notes.pop(0)


def _attribution(ns, name, since, now):
    """Who made a change seen now: the latest note for this app since the last look."""
    with _notes_lock:
        found = [n for n in _notes if n[1] == ns and n[2] == name and since - 5 <= n[0] <= now]
    if not found:
        return "outside", ""
    return "homestead", found[-1][3]


def _env(container):
    out = {}
    for e in container.get("env") or []:
        name = e.get("name", "")
        if "value" in e:
            out[name] = "(hidden)" if SENSITIVE.search(name) else str(e.get("value"))
        elif (e.get("valueFrom") or {}).get("secretKeyRef"):
            ref = e["valueFrom"]["secretKeyRef"]
            out[name] = f"from Secret {ref.get('name')} ({ref.get('key')})"
        elif (e.get("valueFrom") or {}).get("configMapKeyRef"):
            ref = e["valueFrom"]["configMapKeyRef"]
            out[name] = f"from ConfigMap {ref.get('name')} ({ref.get('key')})"
        elif e.get("valueFrom"):
            out[name] = "from the pod's own fields"
    for source in container.get("envFrom") or []:
        ref = source.get("secretRef") or source.get("configMapRef") or {}
        out[f"(all of {'Secret' if 'secretRef' in source else 'ConfigMap'} {ref.get('name')})"] = "included"
    return out


def _volume(v):
    if v.get("persistentVolumeClaim"):
        return f"volume {v['persistentVolumeClaim'].get('claimName')}"
    for kind, label in (("nfs", "NFS"), ("hostPath", "host path"), ("emptyDir", "temporary"), ("configMap", "ConfigMap"),
                        ("secret", "Secret")):
        if kind in v:
            detail = v[kind] or {}
            what = detail.get("path") or detail.get("name") or detail.get("secretName") or ""
            if kind == "nfs":
                what = f"{detail.get('server')}:{detail.get('path')}"
            return f"{label} {what}".strip()
    return "other"


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()[:10]


def summary(dep):
    """What a person changes in an app, flat: {field: text}."""
    spec = (dep.get("spec") or {})
    pod = ((spec.get("template") or {}).get("spec") or {})
    template_meta = (spec.get("template") or {}).get("metadata") or {}
    volumes = {v.get("name"): _volume(v) for v in pod.get("volumes") or []}
    out = {"Copies": str(spec.get("replicas", 1))}
    for c in pod.get("containers") or []:
        n = c.get("name", "")
        out[f"{n}: image"] = c.get("image", "")
        res = c.get("resources") or {}
        for part, word in (("requests", "reserved"), ("limits", "max")):
            for k in ("cpu", "memory"):
                if (res.get(part) or {}).get(k):
                    out[f"{n}: {'CPU' if k == 'cpu' else 'memory'} {word}"] = str(res[part][k])
        if c.get("command") or c.get("args"):
            out[f"{n}: command"] = " ".join(str(x) for x in (c.get("command") or []) + (c.get("args") or []))
        ports = sorted(f"{p.get('containerPort')}/{p.get('protocol', 'TCP')}" for p in c.get("ports") or [])
        if ports:
            out[f"{n}: ports"] = ", ".join(ports)
        for key, value in _env(c).items():
            out[f"{n}: env {key}"] = value
        for m in c.get("volumeMounts") or []:
            out[f"{n}: mount {m.get('mountPath')}"] = volumes.get(m.get("name"), "?") + (" (read-only)" if m.get("readOnly") else "")
    for c in pod.get("initContainers") or []:
        out[f"init {c.get('name')}: image"] = c.get("image", "")
    if pod.get("hostNetwork"):
        out["Host network"] = "on"
    networks = (template_meta.get("annotations") or {}).get("k8s.v1.cni.cncf.io/networks")
    if networks:
        out["LAN attachment"] = str(networks)
    if pod.get("nodeSelector") or pod.get("affinity"):
        out["Placement"] = "rules " + _digest([pod.get("nodeSelector"), pod.get("affinity")])
    return out


def diff(before, after):
    """[{field, before, after}] between two summaries; secret-ish values say only that they changed."""
    rows = []
    for field in sorted(set(before) | set(after)):
        b, a = before.get(field), after.get(field)
        if b == a:
            continue
        if b == "(hidden)" or a == "(hidden)":
            b, a = ("(hidden)" if b is not None else None), ("(hidden, changed)" if a is not None else None)
        rows.append({"field": field, "before": b, "after": a})
    return rows


def _template(dep):
    """The settings kept for undo: the pod template and copies, as they were."""
    spec = dep.get("spec") or {}
    template = copy.deepcopy(spec.get("template") or {})
    (template.get("metadata") or {}).get("annotations", {}).pop("homestead.io/editedAt", None)
    return {"template": template, "replicas": spec.get("replicas", 1)}


def observe(deployments, now=None, since=None):
    """One look at every app: record what changed since the last."""
    now = now or time.time()
    since = since if since is not None else now - 70
    with _lock:
        data = _load()
        apps = data.setdefault("apps", {})
        changed = False
        seen = set()
        for dep in deployments or []:
            meta = dep.get("metadata") or {}
            key = f"{meta.get('namespace')}/{meta.get('name')}"
            seen.add(key)
            now_summary = summary(dep)
            app = apps.get(key)
            if app is None:                      # first sight: what it is, not a change
                apps[key] = {"last": now_summary, "kept": _template(dep), "entries": [], "seen": round(now)}
                changed = True
                continue
            rows = diff(app.get("last") or {}, now_summary)
            if rows:
                source, who = _attribution(meta.get("namespace"), meta.get("name"), since, now)
                if who == BY_SCHEDULE and all(r["field"] == "Copies" for r in rows):
                    # Stopped or started on its schedule, twice a day: kept as
                    # the app now is, but not an entry crowding out real changes.
                    app["last"], app["kept"], changed = now_summary, _template(dep), True
                    continue
                entry = {"id": hashlib.sha256(f"{key}{now}".encode()).hexdigest()[:12], "at": round(now),
                         "source": source, "by": who, "changes": rows, "before": app.get("kept")}
                app["entries"] = ([entry] + app.get("entries", []))[:KEEP]
                app["last"], app["kept"] = now_summary, _template(dep)
                changed = True
            app["seen"] = app.get("seen") if now - app.get("seen", 0) < 86400 else round(now)
        oldest = now - KEEP_DAYS * 86400
        for key in list(apps):
            entries = [e for e in apps[key].get("entries", []) if e["at"] >= oldest]
            if entries != apps[key].get("entries", []):
                apps[key]["entries"], changed = entries, True
            if key not in seen and apps[key].get("seen", now) < oldest:
                apps.pop(key)
                changed = True
        if changed:
            _save(data)
        return apps


def history(ns=None, name=None, limit=200):
    """Entries newest first, for one app or all; without the kept settings."""
    with _lock:
        apps = _load().get("apps", {})
    out = []
    for key, app in apps.items():
        a_ns, a_name = key.split("/", 1)
        if (ns and a_ns != ns) or (name and a_name != name):
            continue
        for e in app.get("entries", []):
            out.append({**{k: v for k, v in e.items() if k != "before"}, "namespace": a_ns, "name": a_name,
                        "can_undo": bool(e.get("before"))})
    return sorted(out, key=lambda e: -e["at"])[:limit]


def kept_before(ns, name, entry_id):
    """The settings from just before one change, for undo."""
    with _lock:
        app = _load().get("apps", {}).get(f"{ns}/{name}") or {}
    entry = next((e for e in app.get("entries", []) if e.get("id") == entry_id), None)
    if not entry or not entry.get("before"):
        raise ValueError("that change is no longer in the history")
    return entry


def undo_plan(current, entry):
    """current with the settings from before the change put back: (proposed, rows)."""
    proposed = copy.deepcopy(current)
    proposed["spec"]["template"] = copy.deepcopy(entry["before"]["template"])
    proposed["spec"]["replicas"] = entry["before"].get("replicas", 1)
    rows = diff(summary(current), summary(proposed))
    if not rows:
        raise ValueError("the app already has these settings")
    return proposed, rows


def undo_preview(body):
    """What undoing a change would put back, and the capacity it needs."""
    import homestead_capacity_review as CAPACITY_REVIEW
    current, proposed, rows, capacity, context = _bound["plan"](body)
    return {"changes": rows, "capacity": capacity, "capacity_token": CAPACITY_REVIEW.issue(body, context)}


def undo(body):
    """Put an app's settings back as they were before a change, reviewed like any rollout."""
    import homestead_capacity_review as CAPACITY_REVIEW
    import homestead_names as NAMES
    import homestead_updates as UPDATES
    current, proposed, rows, capacity, context = _bound["plan"](body)
    CAPACITY_REVIEW.enforce(body, capacity, context)
    ns, name = proposed["metadata"]["namespace"], proposed["metadata"]["name"]
    proposed["spec"]["template"].setdefault("metadata", {}).setdefault("annotations", {})[NAMES.key("editedAt")] = \
        time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    _bound["ksend"]("PUT", f"/apis/apps/v1/namespaces/{ns}/deployments/{name}", proposed)
    homestead_routes.forget("wl")
    UPDATES.refresh_soon(ns, name)
    return {"ok": True, "changes": rows,
            "detail": f"{name} is back to its settings from before that change; its pods are being replaced"}


# Its routes and who may use them (homestead_routes.py).
ROUTES = {
    ("POST", "/api/changes/undo/preview"): ("operator", lambda request: undo_preview(request.body)),
    ("POST", "/api/changes/undo"): ("operator", lambda request: undo(request.body)),
    ("GET", "/api/changes"): ("viewer", lambda request: {"entries": history((request.query.get("ns") or [""])[0] or None, (request.query.get("name") or [""])[0] or None)}),
}
