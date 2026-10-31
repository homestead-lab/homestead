"""Change history: who changed each app, when, and what - and undo.

Every minute the leading replica reads each app's Deployment and boils it down
to what a person changes: images, environment, CPU and memory, ports, volumes,
copies, command, host network, LAN attachment and placement. When that differs
from what it saw last, the difference is a change, recorded with the settings
from before it so it can be undone.

Who made it: every write Homestead sends to a Deployment carries the person
whose request sent it (a job of Homestead's own has none), as an annotation on
the app's own metadata (never its pod template, which would restart it), so the
leader credits a change the same whichever copy of Homestead served it. The
write is the only record: no copy keeps a note of its own in memory (#621).
Every write means every one: a whole object or a merge patch takes it as a
field, a JSON patch as one more op of its own, and
Stop, Start and schedules write the Deployment itself rather than its scale,
which has no metadata to carry it. The annotation names nobody: it holds a keyed
digest of the app, the moment and the person, which the leader checks against
the accounts and API keys. A mark that does not check out for that app -
copied from another app, or altered - is ignored. A change
with no such mark from the minute before it was made outside Homestead
- kubectl, Helm, GitOps - and says so.

Values that may be secret are never kept in the readable summary: an
environment variable from a Secret is "from Secret x", and a plain one whose
name looks like a password or token is "(hidden)". The settings kept for undo
are the pod template as it was, which is what Kubernetes itself already holds.

The history is a file on the data volume, written only when something changed,
KEEP entries per app for KEEP_DAYS. It holds each change's rows and, for undo,
only the digest of the settings from before it: those are kept beside it in
TEMPLATES, one file each, so the file the Changes tab, the Timeline and the
minute's look read stays a few kilobytes an app (#981). Entries written before
that held the settings themselves; they are read as they are and moved out on
the leader's next look. What was read is kept until the file changes.
"""
import copy
import hashlib
import hmac
import json
import os
import re
import threading
import time

import homestead_names as NAMES
import homestead_routes
import homestead_shared as SHARED

DATA_DIR = "/data"
TEMPLATES = "change-templates"      # the settings kept for undo, a file each, named by digest
_DIGEST = re.compile(r"^[0-9a-f]{32}$")
# A template no entry points at is removed only once it is this old: a new
# leader may have written it for an index it has not saved yet (two leaders
# overlap for a moment at a handover).
TEMPLATE_GRACE = 3600
KEEP = 50
KEEP_DAYS = 180
SENSITIVE = re.compile(r"pass|secret|token|key|credential|auth|cookie|private|salt", re.I)
_lock = threading.Lock()
REQUEST = threading.local() # who the request on this thread is for
BY_SCHEDULE = "schedule"    # a power schedule's stop or start: not a change to record
AUTHOR = NAMES.key("changedBy")   # on an app's own metadata: who last wrote it, and when
MARK_MAX = 256                    # a longer mark is not one Homestead wrote
_AUTHOR_POINTER = "/metadata/annotations/" + AUTHOR.replace("~", "~0").replace("/", "~1")
_known = {}                       # the author key and names as last read (_accounts)
_known_lock = threading.Lock()


# server.py's review of an undo, and its writer (bind); for the loop, the
# apps' Deployments, whether this replica leads, and the heartbeat.
_bound = {"plan": None, "ksend": None, "deployments": None,
          "is_leader": lambda: False, "beat": lambda *args, **kwargs: None}


def bind(data_dir="/data", plan=None, ksend=None, deployments=None, is_leader=None, beat=None):
    global DATA_DIR
    DATA_DIR = data_dir
    _memo.clear()
    if plan:
        _bound.update(plan=plan, ksend=ksend)
    if deployments:
        _bound.update(deployments=deployments, is_leader=is_leader, beat=beat)


def _path():
    return os.path.join(DATA_DIR, "change-history.json")


_memo = {}      # the index as last read or written here, and the file it came from


def _stat():
    try:
        st = os.stat(_path())
    except OSError:
        return None
    return st.st_ino, st.st_mtime_ns, st.st_size


def _load():
    """The index, under _lock. It is parsed again only when the file has changed
    (#981); only observe() changes what this returns, and it saves what it changes."""
    stat = _stat()            # before reading: a file replaced meanwhile is read again next time
    if stat is not None and _memo.get("stat") == stat:
        return _memo["data"]
    _memo.clear()
    data = SHARED.read_json(_path(), {})
    if stat is not None:
        _memo.update(stat=stat, data=data)
    return data


def _save(data):
    try:
        SHARED.write_json(_path(), data, separators=(",", ":"), sort_keys=True, mode=0o600)
    except BaseException:
        _memo.clear()
        raise
    _memo.update(stat=_stat(), data=data)


def _template_path(digest):
    return os.path.join(DATA_DIR, TEMPLATES, f"{digest}.json")


def _keep(settings):
    """Keep the settings for undo once, under their digest, and return it.

    Written durably (only on a change, so rarely): the index that points at it
    is saved after, and must never outlive it. A file there already is used
    only if it reads back; one a crash cut short is written again."""
    text = json.dumps(settings, sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256(text.encode()).hexdigest()[:32]
    if _kept(digest) is None:
        SHARED.write_json(_template_path(digest), settings, durable=True, separators=(",", ":"), sort_keys=True,
                          mode=0o600)
    return digest


def _kept(ref):
    """The settings an entry points at: kept in the entry itself before #981, or by digest."""
    if isinstance(ref, dict):
        return ref
    if not isinstance(ref, str) or not _DIGEST.match(ref):
        return None
    try:
        settings = SHARED.read_json(_template_path(ref), {})
    except ValueError:          # cut short or altered: as good as gone
        return None
    return settings if isinstance(settings.get("template"), dict) else None


def _can_undo(ref):
    """Whether an entry's settings are there, from the entry or a look at the
    folder: the template itself is not opened for a listing."""
    if isinstance(ref, dict):
        return True
    return isinstance(ref, str) and bool(_DIGEST.match(ref)) and os.path.isfile(_template_path(ref))


def _compact(app):
    """Move settings an entry or the app still holds itself out to TEMPLATES; True if any moved."""
    moved = False
    if isinstance(app.get("kept"), dict):
        app["kept"], moved = _keep(app["kept"]), True
    for entry in app.get("entries", []):
        if isinstance(entry.get("before"), dict):
            entry["before"], moved = _keep(entry["before"]), True
    return moved


def _tidy_templates(apps):
    """Remove the kept settings that neither an app nor any of its entries points at."""
    wanted = {ref for app in apps.values() for ref in [app.get("kept")] + [e.get("before") for e in app.get("entries", [])]
              if isinstance(ref, str)}
    try:
        names = os.listdir(os.path.join(DATA_DIR, TEMPLATES))
    except OSError:
        return
    now = time.time()
    for file in names:
        digest = file[:-5] if file.endswith(".json") else ""
        if _DIGEST.match(digest) and digest not in wanted:
            path = _template_path(digest)
            try:
                if now - os.path.getmtime(path) < TEMPLATE_GRACE:
                    continue
            except OSError:
                continue
            with SHARED.write_scope(path):
                if SHARED.WRITE_GUARD is not None:
                    SHARED.WRITE_GUARD(path)
                try:
                    os.remove(path)
                except FileNotFoundError:
                    pass


# A Deployment, or its scale: Homestead itself now scales the Deployment (#621),
# but a write to /scale is still a write to the app (one that cannot carry the mark).
_DEPLOYMENT = re.compile(r"^/apis/apps/v1/namespaces/([a-z0-9-]+)/deployments(?:/([a-z0-9.-]+)(?:/scale)?)?(?:\?.*)?$")


def _accounts():
    """The author key and the names a write can be for, as the account store last said.

    Every read that answers replaces them - a factory reset's new key, or no
    key at all before setup. A read that fails answers from the last one, so a
    copy that has read them once marks and checks marks through an outage of
    the account Secret. Read only: looking must never make a key."""
    import homestead_auth as AUTH
    try:
        data = AUTH._load()
    except Exception:
        with _known_lock:
            if _known:
                return dict(_known)
        raise
    key = data.get("signing_key")
    fresh = {"key": hmac.new(key.encode(), b"homestead-change-author-v1", hashlib.sha256).digest() if key else None,
             "users": list(data.get("users") or {}),
             "api_keys": [entry.get("name", "") for entry in (data.get("api_keys") or {}).values()]}
    with _known_lock:
        _known.clear()
        if key:
            _known.update(fresh)
    return fresh


def _author_key():
    """A key every copy of Homestead shares, so a mark can be checked without naming anyone.

    Before setup there is no key: it raises, so the write goes unmarked and a
    mark reads as outside."""
    key = _accounts()["key"]
    if not key:
        raise LookupError("no signing key yet")
    return key


def _authors():
    """Everyone a write can be for: the accounts and the API keys, by name."""
    known = _accounts()
    return {"users": known["users"], "api_keys": known["api_keys"]}


def _label(who):
    """Who a write was for, with its kind, so names of different kinds never match."""
    if not who:
        return "job:"
    if who == BY_SCHEDULE:
        return "schedule:"
    if who.startswith("api-key:"):
        return who
    return "user:" + who


def _signed(key, ns, name, at, who):
    """The digest a mark holds: this app, this moment, this person."""
    text = "\n".join(("Deployment", ns, name, str(int(at)), _label(who)))
    return hmac.new(key, text.encode(), hashlib.sha256).hexdigest()[:32]


_METADATA = ("", "/metadata", "/metadata/annotations")   # paths whose value can hold the mark


def _strip(body):
    """Drop any mark the request brought, in a body or a JSON patch: only this
    write guard puts one on, so nobody can carry another person's through Homestead.

    In a JSON patch an op on the mark itself is dropped, a value set at the
    whole object, its metadata or its annotations is cleaned, and a copy or move
    into one of those is refused: what it copies cannot be cleaned here."""
    if isinstance(body, list):
        keep = []
        for op in body:
            if isinstance(op, dict):
                if _AUTHOR_POINTER in (op.get("path"), op.get("from")):
                    continue
                if op.get("op") in ("copy", "move") and op.get("path") in _METADATA:
                    raise ValueError("a JSON patch cannot copy or move into an app's metadata")
                value = op.get("value")
                if op.get("path") in _METADATA and isinstance(value, dict):
                    _strip_object(value, op["path"])
            keep.append(op)
        body[:] = keep
    elif isinstance(body, dict):
        _strip_object(body, "")


def _strip_object(value, path):
    """Remove the mark from an object, its metadata or its annotations, by where it was set."""
    if path == "":
        value = value.get("metadata")
    if path in ("", "/metadata") and isinstance(value, dict):
        value = value.get("annotations")
    if isinstance(value, dict):
        value.pop(AUTHOR, None)


def _mark(ns, name, who, now):
    """The mark itself; raises when the key cannot be read."""
    return json.dumps({"at": int(now), "by": _signed(_author_key(), ns, name, now, who)}, separators=(",", ":"))


def _carry(path, body, ns, name, who, now):
    """Put the mark on the app's own metadata, when this body can hold it.

    A Scale has no app metadata to put it on (Homestead no longer sends one),
    so one is credited as made outside Homestead."""
    if "/scale" in path.split("?")[0]:
        return
    if isinstance(body, list):
        return _carry_patch(body, ns, name, who, now)
    if not isinstance(body, dict):
        return
    meta = body.setdefault("metadata", {})
    if not isinstance(meta, dict):
        return
    if meta.get("annotations") is None:
        meta["annotations"] = {}
    if isinstance(meta["annotations"], dict):
        try:
            meta["annotations"][AUTHOR] = _mark(ns, name, who, now)
        except Exception:
            pass    # the credentials could not be read: marking never refuses a write


def _carry_patch(ops, ns, name, who, now):
    """Add the mark to a JSON patch as one more op, after the caller's own.

    The app is read once to choose its form: at its own key when the app has
    annotations, or as the whole map when it has none - but only under the
    patch's own test of the version just read, which proves none appeared
    since, so the map overwrites nobody's. Otherwise, or when the app or the
    key cannot be read, or the patch sets the app's metadata wholesale, the
    patch goes as it came. The patch still applies whole or not at all, its
    own tests included. Marking cannot refuse a patch that tests its
    resourceVersion, as rename's and the copy job's do: if the app changed
    since the read, that test fails it anyway. One without such a test fails
    only if every annotation went between the read and the write."""
    if not name or any(isinstance(op, dict) and op.get("op") != "test" and op.get("path") in _METADATA for op in ops):
        return
    try:
        mark = _mark(ns, name, who, now)
        import homestead_kube as KUBE
        meta = (KUBE.kget(f"/apis/apps/v1/namespaces/{ns}/deployments/{name}") or {}).get("metadata") or {}
    except Exception:
        return
    if isinstance(meta.get("annotations"), dict):
        ops.append({"op": "add", "path": _AUTHOR_POINTER, "value": mark})
    elif meta.get("resourceVersion") and {"op": "test", "path": "/metadata/resourceVersion",
                                          "value": meta["resourceVersion"]} in ops:
        ops.append({"op": "add", "path": "/metadata/annotations", "value": {AUTHOR: mark}})


def _marked_by(dep, ns, name, since, now):
    """Who the app's mark says wrote it since the last look, or None when there is
    no such mark or it does not check out for this app.

    The mark's time is the writing copy's clock and the window the leader's; with
    the clocks set by NTP the few seconds of slack cover the difference. A mark
    kept on this app over another edit within one look is credited."""
    raw = (((dep or {}).get("metadata") or {}).get("annotations") or {}).get(AUTHOR)
    if not isinstance(raw, str) or len(raw) > MARK_MAX:
        return None
    try:
        mark = json.loads(raw)
        at, tag = int(mark["at"]), str(mark["by"])
        if not since - 5 <= at <= now:
            return None
        key, known = _author_key(), _authors()
        candidates = ["", BY_SCHEDULE, *(u for u in known["users"] if _label(u) == "user:" + u),
                      *(f"api-key:{n}" for n in known["api_keys"])]
        return next((who for who in candidates if hmac.compare_digest(_signed(key, ns, name, at, who), tag)), None)
    except Exception:
        return None


def note_write(method, path, body=None, now=None):
    """Homestead is about to write a Deployment: say who for, on the write itself.
    A write to a Longhorn Setting or Volume goes to _note_object instead."""
    if method not in ("POST", "PUT", "PATCH"):
        return
    m = _DEPLOYMENT.match(path or "")
    if not m:
        _note_object(path, body)
        return
    _strip(body)            # before anything else: a refused patch carries nobody's mark
    name = m.group(2) or ((body or {}).get("metadata") or {}).get("name") or ""
    who = getattr(REQUEST, "user", None) or ""
    _carry(path, body, m.group(1), name, who, now or time.time())


def _attribution(ns, name, since, now, dep=None):
    """Who made a change seen now: the mark the write left on the app, if it is
    from since the last look and checks out; otherwise it was made outside."""
    who = _marked_by(dep, ns, name, since, now)
    return ("outside", "") if who is None else ("homestead", who)


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


def _observe_one(apps, seen, dep, now, since):
    """Record one app's change since the last look; True when the history changed."""
    meta = dep.get("metadata") or {}
    key = f"{meta.get('namespace')}/{meta.get('name')}"
    seen.add(key)
    now_summary = summary(dep)
    app = apps.get(key)
    if app is None:                      # first sight: what it is, not a change
        apps[key] = {"last": now_summary, "kept": _keep(_template(dep)), "entries": [], "seen": round(now)}
        return True
    changed = False
    rows = diff(app.get("last") or {}, now_summary)
    if rows:
        source, who = _attribution(meta.get("namespace"), meta.get("name"), since, now, dep)
        if who == BY_SCHEDULE and all(r["field"] == "Copies" for r in rows):
            # Stopped or started on its schedule, twice a day: kept as
            # the app now is, but not an entry crowding out real changes.
            app["last"], app["kept"] = now_summary, _keep(_template(dep))
            return True
        kept = _keep(_template(dep))
        entry = {"id": hashlib.sha256(f"{key}{now}".encode()).hexdigest()[:12], "at": round(now),
                 "source": source, "by": who, "changes": rows, "before": app.get("kept")}
        app["entries"] = ([entry] + app.get("entries", []))[:KEEP]
        app["last"], app["kept"] = now_summary, kept
        changed = True
    if now - app.get("seen", 0) >= 86400:
        # Saved, not only noted: what was read is kept between looks, so it
        # must be what the file says.
        app["seen"], changed = round(now), True
    return changed


def observe(deployments, now=None, since=None):
    """One look at every app: record what changed since the last."""
    now = now or time.time()
    since = since if since is not None else now - 70
    with _lock:
        try:
            return _observe(deployments, now, since)
        except BaseException:
            _memo.clear()           # what was read may have been changed but not saved
            raise


def _observe(deployments, now, since):
    data = _load()
    apps = data.setdefault("apps", {})
    changed = False
    for app in apps.values():
        changed = _compact(app) or changed
    seen = set()
    for dep in deployments or []:
        try:
            changed = _observe_one(apps, seen, dep, now, since) or changed
        except Exception:
            continue    # one app that cannot be read stops no other from being recorded
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
        _tidy_templates(apps)
    return apps


def history(ns=None, name=None, limit=200):
    """Entries newest first, for one app or all; without the kept settings."""
    out = []
    with _lock:             # what was read is shared with the minute's look: read it under the lock
        for key, app in _load().get("apps", {}).items():
            a_ns, a_name = key.split("/", 1)
            if (ns and a_ns != ns) or (name and a_name != name):
                continue
            for e in app.get("entries", []):
                out.append({**{k: v for k, v in e.items() if k != "before"}, "namespace": a_ns, "name": a_name,
                            "can_undo": _can_undo(e.get("before"))})
    return sorted(out, key=lambda e: -e["at"])[:limit]


def kept_before(ns, name, entry_id):
    """The entry for one change, with the settings from just before it, for undo."""
    with _lock:
        app = _load().get("apps", {}).get(f"{ns}/{name}") or {}
        entry = next((dict(e) for e in app.get("entries", []) if e.get("id") == entry_id), None)
    before = _kept(entry.get("before")) if entry else None
    if not before:
        raise ValueError("that change is no longer in the history")
    return {**entry, "before": before}


def undo_plan(current, entry):
    """current with the settings from before the change put back: (proposed, rows)."""
    if not isinstance(entry.get("before"), dict) or not isinstance(entry["before"].get("template"), dict):
        raise ValueError("the settings from before that change are no longer kept, so it cannot be undone")
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
    import homestead_update_checks as CHECKS
    current, proposed, rows, capacity, context = _bound["plan"](body)
    CAPACITY_REVIEW.enforce(body, capacity, context)
    ns, name = proposed["metadata"]["namespace"], proposed["metadata"]["name"]
    proposed["spec"]["template"].setdefault("metadata", {}).setdefault("annotations", {})[NAMES.key("editedAt")] = \
        time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    _bound["ksend"]("PUT", f"/apis/apps/v1/namespaces/{ns}/deployments/{name}", proposed)
    homestead_routes.forget("wl")
    CHECKS.refresh_soon(ns, name)
    return {"ok": True, "changes": rows,
            "detail": f"{name} is back to its settings from before that change; its pods are being replaced"}


# Its routes and who may use them (homestead_routes.py).
ROUTES = {
    ("POST", "/api/changes/undo/preview"): ("operator", lambda request: undo_preview(request.body)),
    ("POST", "/api/changes/undo"): ("operator", lambda request: undo(request.body)),
    ("GET", "/api/changes"): ("viewer", lambda request: {"entries": history((request.query.get("ns") or [""])[0] or None, (request.query.get("name") or [""])[0] or None)}),
}


def run(sleep=time.sleep):
    """Every minute on the leader: what changed in each app since the last look."""
    last = time.time()
    while True:
        started = time.time()
        if _bound["is_leader"]():
            try:
                observe(_bound["deployments"](), now=started, since=last)
                _bound["beat"]("changes", 60, leader_only=True)
            except Exception as error:
                _bound["beat"]("changes", 60, error, leader_only=True)
                print(f"changes: {str(error)[:160]}", flush=True)
        last = started
        sleep(max(5, 60 - (time.time() - started)))


# The same mark on other kinds of object Homestead changes for someone: a
# Longhorn Setting or Volume. It is signed over the kind, the object and the
# value written, so a mark copied from an app or another object, or left
# behind by a later change made elsewhere (kubectl, Longhorn's chart), never
# checks out. Only the write guard puts it on, for a write made inside
# marking(value); a mark a request brings is dropped, as on apps.
_LONGHORN_OBJECT = re.compile(
    r"^/apis/longhorn\.io/v1beta2/namespaces/([a-z0-9-]+)/(settings|volumes)(?:/([a-z0-9.-]+))?(?:\?.*)?$")
_OBJECT_KIND = {"settings": "Setting", "volumes": "Volume"}


def _signed_object(key, kind, ns, name, value, at, who):
    text = "\n".join(("object", kind, ns, name, str(value), str(int(at)), _label(who)))
    return hmac.new(key, text.encode(), hashlib.sha256).hexdigest()[:32]


def object_mark(kind, ns, name, value, now=None):
    """The changedBy value for writing value to this object, for whoever this
    request is for; None before setup, when there is no key to sign with."""
    now = int(now if now is not None else time.time())
    try:
        tag = _signed_object(_author_key(), kind, ns, name, value, now, getattr(REQUEST, "user", None) or "")
    except Exception:
        return None
    return json.dumps({"at": now, "by": tag}, separators=(",", ":"))


def object_author(kind, ns, name, obj, value):
    """{"who", "at"} when the object's mark checks out for it holding value
    ("" is a job of Homestead's own), else None."""
    raw = (((obj or {}).get("metadata") or {}).get("annotations") or {}).get(AUTHOR)
    if not isinstance(raw, str) or len(raw) > MARK_MAX:
        return None
    try:
        mark = json.loads(raw)
        at, tag = int(mark["at"]), str(mark["by"])
        key, known = _author_key(), _authors()
    except Exception:
        return None
    candidates = ["", *(f"api-key:{n}" for n in known["api_keys"]),
                  *(u for u in known["users"] if _label(u) == "user:" + u)]
    who = next((w for w in candidates
                if hmac.compare_digest(_signed_object(key, kind, ns, name, value, at, w), tag)), None)
    return None if who is None else {"who": who, "at": at}


class marking:
    """Writes to a Longhorn Setting or Volume on this thread, inside it, are
    marked as writing value."""

    def __init__(self, value):
        self.value = value

    def __enter__(self):
        REQUEST.object_value = self.value
        return self

    def __exit__(self, *exc):
        REQUEST.object_value = None
        return False


def _note_object(path, body):
    """A write to a Longhorn Setting or Volume: drop any mark it brought, and
    mark it when it is written inside marking()."""
    m = _LONGHORN_OBJECT.match(path or "")
    if not m:
        return False
    _strip(body)
    value = getattr(REQUEST, "object_value", None)
    name = m.group(3) or (((body or {}).get("metadata") or {}).get("name") if isinstance(body, dict) else "") or ""
    if value is not None and isinstance(body, dict) and name:
        mark = object_mark(_OBJECT_KIND[m.group(2)], m.group(1), name, value)
        if mark:
            meta = body.setdefault("metadata", {})
            if isinstance(meta, dict):
                meta.setdefault("annotations", {})
                if isinstance(meta["annotations"], dict):
                    meta["annotations"][AUTHOR] = mark
    return True
