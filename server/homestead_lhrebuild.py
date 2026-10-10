"""Keeping detached Longhorn volumes at the copies they ask for.

Longhorn rebuilds a missing replica only while its volume is attached. A
volume nothing uses can sit with one copy for good - nas-backup did, until a
reboot found its only copy on the host being drained. Longhorn 1.9 and later
can rebuild detached volumes itself (offline replica rebuilding); Homestead
turns that on once, by default, and leaves the setting alone after that.
Older Longhorn has no such setting: there Homestead attaches the volume
itself, with no frontend - nothing can mount it - until its copies are whole,
and steps aside the moment anything else wants the volume.
"""
import time
import urllib.error
import homestead_routes

LH = "/apis/longhorn.io/v1beta2/namespaces/longhorn-system"
SETTING = "offline-replica-rebuilding"
# Marks the setting as one Homestead has set once; after that it is yours.
DEFAULTED = "homestead.io/offline-rebuilding-default"
TICKET = "homestead-rebuild"
# A fallback attachment that has not made the volume whole by then is let go.
TICKET_LIMIT = 6 * 3600
_gave_up = {}           # volume -> when a hold on it ran out; tried again a day later

kget = ksend = None
_enabled = lambda: True     # Homestead's own preference, for Longhorn without the setting


def bind(_kget, _ksend, enabled=None):
    global kget, ksend, _enabled
    kget, ksend = _kget, _ksend
    if enabled is not None:
        _enabled = enabled


def _items(path):
    try:
        return kget(path).get("items", [])
    except urllib.error.HTTPError as error:
        if error.code == 404:
            return []
        raise


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
    found = setting()
    volumes, replicas = _items(f"{LH}/volumes"), _items(f"{LH}/replicas")
    rows = short(volumes, replicas, _hosts())
    held = _tickets()
    for row in rows:
        row["rebuilding"] = row["name"] in held
    supported = found is not None
    return {"supported": supported, "enabled": found[0] if supported else bool(_enabled()),
            "short": rows}


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
    """Turn offline rebuilding on or off for the whole cluster."""
    found = setting()
    if found is None:
        return {"ok": True, "enabled": bool(enabled), "supported": False}
    ksend("PATCH", f"{LH}/settings/{SETTING}",
          {"metadata": {"annotations": {DEFAULTED: "set"}}, "value": "true" if enabled else "false"},
          ctype="application/merge-patch+json")
    return {"ok": True, "enabled": bool(enabled), "supported": True}


def rebuild_now(volume):
    """Repair one detached volume now: Longhorn's own offline rebuild where it
    has one, Homestead's no-frontend attachment where not."""
    rows = {row["name"]: row for row in short(_items(f"{LH}/volumes"), _items(f"{LH}/replicas"), _hosts())}
    row = rows.get(volume)
    if not row:
        raise ValueError("This volume is not a detached volume short of copies; refresh")
    if not row["whole"]:
        raise ValueError("This volume has no whole copy to rebuild from; restore it from a backup instead")
    if setting() is not None:
        ksend("PATCH", f"{LH}/volumes/{volume}", {"spec": {"offlineRebuilding": "enabled"}},
              ctype="application/merge-patch+json")
        return {"ok": True, "detail": f"Longhorn is rebuilding {row['claim']} while it is detached"}
    _hold(volume, row["hosts"][0])
    return {"ok": True, "detail": f"Homestead is holding {row['claim']} attached, with nothing able to mount it, "
                                  "until its copies are rebuilt"}


def _hold(volume, node):
    attachment = kget(f"{LH}/volumeattachments/{volume}")
    tickets = (attachment.get("spec") or {}).get("attachmentTickets") or {}
    if tickets:
        raise ValueError("Something else has asked for this volume; Longhorn will rebuild it while that holds it")
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

    Turns Longhorn's offline rebuilding on once, by default. Where Longhorn
    has no such setting, holds one short volume at a time attached until it
    is whole, and lets go of one whose copies are whole, that anything else
    asked for, or that has not finished in six hours."""
    found = setting()
    if found is not None:
        value, item = found
        if not value and not ((item.get("metadata") or {}).get("annotations") or {}).get(DEFAULTED):
            save(True)
        return
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
            body = ("No whole copy of this detached volume is left. Longhorn cannot rebuild it; "
                    "restore it from a backup if its data is still wanted")
        elif repairing:
            body = (f"{row['whole']} of {row['wanted']} copies, on {', '.join(row['hosts'])}. "
                    "It is detached, so Longhorn rebuilds the rest offline; if that host fails first, the data goes with it")
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


# Its routes and who may use them (homestead_routes.py).
ROUTES = {
    ("GET", "/api/longhorn/offline-rebuilding"): ("viewer", lambda request: homestead_routes.cached("lhrebuild", 30, status)),
}
