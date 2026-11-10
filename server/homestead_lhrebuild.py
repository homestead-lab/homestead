"""Keeping detached Longhorn volumes at the copies they ask for.

Longhorn rebuilds a missing replica only while its volume is attached. A
volume nothing uses can sit with one copy for good - nas-backup did, until a
reboot found its only copy on the host being drained.

Homestead attaches such a volume itself, with no frontend - nothing can mount
it - until its copies are whole, and releases it as soon as anything else asks
for it. Longhorn 1.9 and later can do this too (offline replica rebuilding),
but its rebuild keeps the volume until the rebuild finishes, even when an app
on another host is waiting for it: after a host failure, Home Assistant waited
for a 15 GB copy before it could start. So Homestead turns Longhorn's own off,
once (OWN marks the setting), and uses its own on every version.
"""
import time
import urllib.error
import homestead_kube as KUBE
import homestead_routes

LH = "/apis/longhorn.io/v1beta2/namespaces/longhorn-system"
SETTING = "offline-replica-rebuilding"
# Marks the setting as one Homestead has set once; after that it is yours.
DEFAULTED = "homestead.io/offline-rebuilding-default"
# Marks Longhorn's own offline rebuilding as switched off in favour of Homestead's.
OWN = "homestead.io/offline-rebuilding-own"
TICKET = "homestead-rebuild"
# A fallback attachment that has not made the volume whole by then is let go.
TICKET_LIMIT = 6 * 3600
_gave_up = {}           # volume -> when a hold on it ran out; tried again a day later

kget = ksend = None
_enabled = lambda: True     # Homestead's own preference, for Longhorn without the setting
app_settings = save_app_settings = None     # server.py's settings, where that preference is kept


# What bind() puts back for an argument it is not given: the module's own default,
# not the last bind's (a test's fake must not outlive the test).
_UNBOUND = {"_enabled": _enabled, "app_settings": app_settings, "save_app_settings": save_app_settings}


def bind(_kget, _ksend, enabled=None, settings=None, save_settings=None):
    global kget, ksend, _enabled, app_settings, save_app_settings
    kget, ksend = _kget, _ksend
    _enabled = enabled if enabled is not None else _UNBOUND["_enabled"]
    if settings is not None:
        app_settings, save_app_settings = settings, save_settings
    else:
        app_settings, save_app_settings = _UNBOUND["app_settings"], _UNBOUND["save_app_settings"]


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


def whole(replica):
    """A copy Longhorn can rebuild from: running, or stopped with the volume
    detached after it was last healthy - and never failed."""
    spec, status = replica.get("spec") or {}, replica.get("status") or {}
    if spec.get("failedAt"):
        return False
    return status.get("currentState") == "running" or bool(spec.get("healthyAt"))


def short(volumes, replicas, hosts_available=None):
    """Detached volumes holding fewer whole copies, on separate hosts, than
    they ask for - the ones Longhorn will not repair by itself. A volume
    asking for more copies than there are hosts is short only of what the
    hosts can hold."""
    hosts = {}
    for replica in replicas:
        spec = replica.get("spec") or {}
        if spec.get("volumeName") and whole(replica):
            hosts.setdefault(spec["volumeName"], set()).add(spec.get("nodeID"))
    out = []
    for volume in volumes:
        meta, spec, status = volume.get("metadata") or {}, volume.get("spec") or {}, volume.get("status") or {}
        wanted = int(spec.get("numberOfReplicas") or 0)
        if hosts_available:
            wanted = min(wanted, hosts_available)
        name = meta.get("name", "")
        have = hosts.get(name, set())
        if status.get("state") != "detached" or not wanted or len(have) >= wanted:
            continue
        k8s = status.get("kubernetesStatus") or {}
        out.append({"name": name, "claim": "/".join(x for x in (k8s.get("namespace"), k8s.get("pvcName")) if x) or name,
                    "whole": len(have), "wanted": wanted, "hosts": sorted(h for h in have if h),
                    "offline": spec.get("offlineRebuilding") or "ignored"})
    return out


def setting():
    """Longhorn's offline rebuilding setting: True/False, or None where this
    Longhorn has none."""
    try:
        item = kget(f"{LH}/settings/{SETTING}")
    except urllib.error.HTTPError as error:
        if error.code == 404:
            return None
        raise
    return str(item.get("value") or "").lower() == "true", item


def _hosts():
    """Longhorn hosts that can take a replica."""
    return len([n for n in _items(f"{LH}/nodes") if (n.get("spec") or {}).get("allowScheduling", True)]) or None


def status():
    volumes, replicas = _items(f"{LH}/volumes"), _items(f"{LH}/replicas")
    rows = short(volumes, replicas, _hosts())
    held = _tickets()
    for row in rows:
        row["rebuilding"] = row["name"] in held
    return {"supported": True, "enabled": bool(_enabled()), "short": rows}


def _tickets():
    """Volumes Homestead itself is holding attached to rebuild, with when."""
    out = {}
    for attachment in _items(f"{LH}/volumeattachments"):
        tickets = (attachment.get("spec") or {}).get("attachmentTickets") or {}
        if TICKET in tickets:
            name = (attachment.get("metadata") or {}).get("name", "")
            out[name] = {"tickets": tickets, "since": (tickets[TICKET].get("parameters") or {}).get("since", "")}
    return out


def save(enabled):
    """Homestead's offline rebuilding on or off. Longhorn's own stays off
    (_use_own), so the answer is Homestead's preference alone."""
    return {"ok": True, "enabled": bool(enabled), "supported": True}


def _use_own(found):
    """Once: Longhorn's own offline rebuilding off, and any volume an earlier
    Homestead set to use it back to the cluster's setting, so only Homestead's
    holds - which give way to apps - rebuild detached volumes."""
    value, item = found
    if ((item.get("metadata") or {}).get("annotations") or {}).get(OWN):
        return
    for each in _items(f"{LH}/volumes"):
        if (each.get("spec") or {}).get("offlineRebuilding") == "enabled":
            ksend("PATCH", f"{LH}/volumes/{each['metadata']['name']}", {"spec": {"offlineRebuilding": "ignored"}},
                  ctype="application/merge-patch+json")
    ksend("PATCH", f"{LH}/settings/{SETTING}", {"metadata": {"annotations": {OWN: "1"}}, "value": "false"},
          ctype="application/merge-patch+json")


def rebuild_now(volume):
    """Repair one detached volume now: Longhorn's own offline rebuild where it
    has one, Homestead's no-frontend attachment where not."""
    rows = {row["name"]: row for row in short(_items(f"{LH}/volumes"), _items(f"{LH}/replicas"), _hosts())}
    row = rows.get(volume)
    if not row:
        raise ValueError("This volume is not a detached volume that is short of copies. Refresh.")
    if not row["whole"]:
        raise ValueError("This volume has no whole copy to rebuild from. Restore it from a backup instead.")
    _hold(volume, row["hosts"][0])
    return {"ok": True, "detail": f"Homestead is holding {row['claim']} attached, with nothing able to mount it, "
                                  "until its copies are rebuilt"}


def _hold(volume, node):
    attachment = kget(f"{LH}/volumeattachments/{volume}")
    tickets = (attachment.get("spec") or {}).get("attachmentTickets") or {}
    if tickets:
        raise ValueError("Something else has asked for this volume, so Longhorn rebuilds it while that holds it")
    ksend("PATCH", f"{LH}/volumeattachments/{volume}",
          {"metadata": {"resourceVersion": attachment["metadata"]["resourceVersion"]},
           "spec": {"attachmentTickets": {TICKET: {
               "id": TICKET, "type": "longhorn-api", "nodeID": node,
               "parameters": {"disableFrontend": "true", "since": str(int(time.time()))}}}}},
          ctype="application/merge-patch+json")


def _release(volume):
    ksend("PATCH", f"{LH}/volumeattachments/{volume}",
          {"spec": {"attachmentTickets": {TICKET: None}}}, ctype="application/merge-patch+json")


def tick():
    """Run by the leader every minute or so.

    Turns Longhorn's own offline rebuilding off once (_use_own). Holds one
    short volume at a time attached until it is whole, and lets go of one
    whose copies are whole, that anything else asked for, or that has not
    finished in six hours."""
    found = setting()
    if found is not None:
        _use_own(found)
    held = _tickets()
    volumes = {(v.get("metadata") or {}).get("name"): v for v in _items(f"{LH}/volumes")}
    for name, info in held.items():
        volume = volumes.get(name) or {}
        others = [t for t in info["tickets"] if t != TICKET]
        healthy = (volume.get("status") or {}).get("robustness") == "healthy"
        expired = info["since"].isdigit() and time.time() - int(info["since"]) > TICKET_LIMIT
        if expired:
            _gave_up[name] = time.time()
        if others or healthy or expired or not volume:
            _release(name)
    if held or not _enabled():
        return
    for row in short(list(volumes.values()), _items(f"{LH}/replicas"), _hosts()):
        if time.time() - _gave_up.get(row["name"], 0) < 86400:
            continue
        if row["whole"]:
            try:
                _hold(row["name"], row["hosts"][0])
            except (ValueError, urllib.error.HTTPError, KeyError):
                continue
            return


def alert_facts(state):
    """A detached volume short of copies, while nothing is repairing it -
    or while something is, so its progress has a place to be seen."""
    facts = []
    for row in state.get("short") or []:
        repairing = state.get("enabled") or row.get("rebuilding") or row.get("offline") == "enabled"
        if not row["whole"]:
            body = ("No whole copy of this detached volume is left, so Longhorn cannot rebuild it. "
                    "Restore it from a backup if its data is still wanted.")
        elif repairing:
            body = (f"{row['whole']} of {row['wanted']} copies, on {', '.join(row['hosts'])}. "
                    "It is detached, so Longhorn rebuilds the rest offline. If that host fails first, the data is lost with it.")
        else:
            body = (f"{row['whole']} of {row['wanted']} copies, on {', '.join(row['hosts'])}. Longhorn rebuilds copies only "
                    "while a volume is attached: rebuild it now from Volumes, or turn on offline rebuilding in Settings")
        facts.append({"signals": {"whole": row["whole"], "repairing": int(bool(repairing))},
                      "key": f"detached-copies:{row['name']}", "category": "degraded",
                      "severity": "critical" if not row["whole"] else "degraded",
                      "title": f"{row['claim']} is detached with {row['whole']} of {row['wanted']} copies",
                      "resolved": f"{row['claim']} has its copies again",
                      "body": body, "href": "/volumes"})
    return facts


def save_preference(enabled):
    """Offline rebuilding on or off: Homestead's preference, kept in its
    settings, and Longhorn's setting where it has one."""
    homestead_routes.forget("lhrebuild")
    if not isinstance(enabled, bool):
        raise ValueError("enabled must be true or false")
    settings = app_settings()
    settings["longhorn"] = {"offline_rebuilding": enabled}
    save_app_settings(settings)
    return save(enabled)


def _rebuild(volume):
    homestead_routes.forget("lhrebuild", "volumes")
    return rebuild_now(volume)


# Its routes and who may use them (homestead_routes.py).
ROUTES = {
    ("GET", "/api/longhorn/offline-rebuilding"): ("viewer", lambda request: homestead_routes.cached("lhrebuild", 30, status)),
    ("POST", "/api/longhorn/offline-rebuilding"): ("admin", lambda request: save_preference(request.body.get("enabled"))),
    ("POST", "/api/longhorn/rebuild"): ("operator", lambda request: _rebuild(str(request.body.get("volume") or ""))),
}
