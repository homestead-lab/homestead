"""App volumes on a one-host Longhorn cluster: an offer to match copies to hosts (#841).

Stock Longhorn's `longhorn` class asks for three copies on different hosts.
A cluster of one host holds one, so Longhorn marks every app volume degraded,
and every warning that reads a volume's robustness says so for as long as
the cluster has one host. homestead_data_copies (#809) lowers Homestead's
own data volume by itself; app volumes are the person's own, so nothing here
changes one without an admin's reviewed click.

facts() lists what is offered, reading only:
  lower  the cluster has exactly one Kubernetes node and one Longhorn node
         record, and these volumes ask for more copies than Longhorn can
         place: degraded, Scheduled False for ReplicaSchedulingFailure, no
         copy being rebuilt (#809's tests, so a host that is only rebooting
         on a bigger cluster is never mistaken for a one-host cluster).
         Each would keep the one copy the host can hold.
  raise  two or more Ready hosts Longhorn stores on, and volumes this offer
         lowered before: each back to the count it asked for, as far as the
         hosts allow.
Homestead's own data volumes are left to homestead_data_copies, and Harvester
manages its own copies. A failed read offers nothing ("unknown").

apply() takes the rows the admin reviewed and changes them only while facts()
still offers exactly those: the count a volume asked for is kept on it as
homestead.io/one-host-copies, the same record #809 keeps, and is dropped once
the volume is back to it. No StorageClass or Longhorn setting is changed.

It reads through homestead_node_parity's bound kget and ksend, platform and
own Deployment, as homestead_data_copies does.
"""
import homestead_data_copies as DATA_COPIES
import homestead_node_parity as NODE_PARITY
import homestead_rebalance as REBALANCE
import homestead_routes

LOWERED_FROM = DATA_COPIES.LOWERED_FROM
CACHE = "one-host-copies"


def _recorded(volume):
    """The count a volume asked for before it was lowered, or 0."""
    value = str(((volume.get("metadata") or {}).get("annotations") or {}).get(LOWERED_FROM) or "")
    return int(value) if value.isdigit() else 0


def _row(volume, have, want):
    k8s = (volume.get("status") or {}).get("kubernetesStatus") or {}
    return {"volume": (volume.get("metadata") or {}).get("name", ""), "claim": k8s.get("pvcName", ""),
            "namespace": k8s.get("namespace", ""), "app": REBALANCE._app(volume), "from": have, "to": want}


def _rebuilding():
    """The volumes with a copy being rebuilt (WO), from one read of every
    engine. Raises when the engines cannot be read: the caller offers nothing."""
    found = NODE_PARITY.kget(DATA_COPIES.ENGINES) or {}
    return {(engine.get("spec") or {}).get("volumeName") for engine in found.get("items") or []
            if "WO" in ((engine.get("status") or {}).get("replicaModeMap") or {}).values()}


def _lower(volumes):
    candidates = [(volume, int((volume.get("spec") or {}).get("numberOfReplicas") or 0)) for volume in volumes]
    candidates = [(volume, have) for volume, have in candidates
                  if have > 1 and (volume.get("status") or {}).get("robustness") == "degraded"
                  and DATA_COPIES._cannot_place(volume)]
    if not candidates:
        return []
    rebuilding = _rebuilding()
    return [_row(volume, have, 1) for volume, have in candidates if volume["metadata"]["name"] not in rebuilding]


def _raise(volumes, hosts):
    rows = []
    for volume in volumes:
        have, recorded = int((volume.get("spec") or {}).get("numberOfReplicas") or 0), _recorded(volume)
        want = min(recorded, hosts)
        if recorded and want > have:
            rows.append(_row(volume, have, want))
    return rows


def facts():
    """What is offered: {state, hosts, volumes: [{volume, claim, namespace,
    app, from, to}]}, state n/a, unknown (with error), none, lower or raise.
    Reads only."""
    out = {"state": "n/a", "hosts": 0, "volumes": []}
    p = NODE_PARITY.platform() or {}
    if p.get("harvester") or not p.get("longhorn"):
        return out
    try:
        own = {found["pv"] for found in DATA_COPIES.own_data_volumes()}
        volumes = [v for v in (NODE_PARITY.kget(NODE_PARITY.LH_VOLUMES) or {}).get("items") or []
                   if (v.get("metadata") or {}).get("name") and v["metadata"]["name"] not in own]
        if DATA_COPIES._one_host():
            rows, state, hosts = _lower(volumes), "lower", 1
        else:
            storing = NODE_PARITY._storage_hosts(NODE_PARITY._ready_nodes())
            if storing is None:
                return {**out, "state": "unknown", "error": "Longhorn's nodes could not be read"}
            hosts = len(storing)
            rows, state = (_raise(volumes, hosts), "raise") if hosts >= 2 else ([], "none")
    except Exception as error:
        return {**out, "state": "unknown", "error": str(error)[:160] or type(error).__name__}
    return {"state": state if rows else "none", "hosts": hosts, "volumes": rows}


def cached_facts():
    return homestead_routes.cached(CACHE, 15, facts)


def apply(body):
    """The admin's reviewed click: {"action": "lower" | "raise", "volumes":
    [{volume, from, to}]}. Changes them only while facts() offers exactly
    those rows; otherwise nothing changes."""
    body = body if isinstance(body, dict) else {}
    action = body.get("action")
    if action not in ("lower", "raise"):
        raise ValueError("action must be lower or raise")
    try:
        reviewed = sorted((str(r["volume"]), int(r["from"]), int(r["to"])) for r in body.get("volumes") or [])
    except (KeyError, TypeError, ValueError):
        raise ValueError("each volume needs its name and the counts reviewed")
    found = facts()
    if found["state"] == "unknown":
        return {"ok": False, "changed": [], "message": f"Could not read the volumes' copies: {found.get('error')}. Nothing was changed"}
    offered = {row["volume"]: row for row in found["volumes"]} if found["state"] == action else {}
    if not reviewed or any(offered.get(name, {}).get("from") != have or offered.get(name, {}).get("to") != want
                           for name, have, want in reviewed):
        return {"ok": False, "changed": [], "message": "The volumes changed since the review. Nothing was changed; review again"}
    changed, failed = [], []
    try:
        for name, have, want in reviewed:
            try:
                volume = NODE_PARITY.kget(f"{NODE_PARITY.LH_VOLUMES}/{name}") or {}
                if action == "lower":
                    # A volume lowered before, raised part way, then lowered
                    # again keeps the count it first asked for.
                    patch = {"metadata": {"annotations": {LOWERED_FROM: str(max(have, _recorded(volume)))}},
                             "spec": {"numberOfReplicas": want}}
                else:
                    patch = {"spec": {"numberOfReplicas": want}}
                    if want >= _recorded(volume):
                        patch = {"metadata": {"annotations": {LOWERED_FROM: None}}, **patch}
                NODE_PARITY.ksend("PATCH", f"{NODE_PARITY.LH_VOLUMES}/{name}", patch,
                                  ctype="application/merge-patch+json")
                changed.append(name)
            except Exception as error:
                failed.append({"volume": name, "error": str(error)[:160] or type(error).__name__})
    finally:
        homestead_routes.forget(CACHE)
    count = f"{len(changed)} volume{'' if len(changed) == 1 else 's'}"
    message = (f"{count} now keep{'s' if len(changed) == 1 else ''} one copy, as this host can hold. "
               "The count each asked for is kept on it" if action == "lower" else
               f"{count} {'has' if len(changed) == 1 else 'have'} copies back; Longhorn is making them")
    if failed:
        message = (f"{message}. Not changed: " if changed else "Nothing was changed: ") + \
            "; ".join(f"{f['volume']} ({f['error']})" for f in failed)
    return {"ok": not failed, "changed": changed, "failed": failed, "message": message}


ROUTES = {
    ("GET", "/api/longhorn/one-host-copies"): ("viewer", lambda request: cached_facts()),
    ("POST", "/api/longhorn/one-host-copies"): ("admin", lambda request: apply(request.body)),
}
