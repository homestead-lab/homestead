"""Start a StatefulSet's stuck pod on another host (#441): the reads, the plan, the routes and the job.

Every read here is strict. The check that decides whether a pod may be
started elsewhere is fail-closed, so a read that fails raises Unread with a
reason a person can act on, rather than being treated as "nothing there"."""
import hashlib
import json
import time
import urllib.error
import urllib.parse
from datetime import datetime, timezone

import homestead_failover as FAILOVER
import homestead_hostrun as HOSTRUN
import homestead_operations as OPS
import homestead_routes as ROUTER

LH = "/apis/longhorn.io/v1beta2/namespaces/longhorn-system"
LEASES = "/apis/coordination.k8s.io/v1/namespaces/kube-node-lease/leases/"

# Wired like homestead_promote: server.py is at its line cap.
kget = lambda path, *a, **k: HOSTRUN.kget(path, *a, **k)  # noqa: E731
ksend = lambda *a, **k: HOSTRUN.ksend(*a, **k)  # noqa: E731


clock = time.time  # tests replace it


def bind(_kget, _ksend):
    global kget, ksend
    kget, ksend = _kget, _ksend


class Unread(Exception):
    """Something the check needs could not be read; the message says what."""


def _read(path, what):
    try:
        return kget(path)
    except Exception as e:
        raise Unread(f"Could not read {what} ({e}), so Homestead cannot tell whether starting the pod elsewhere is safe.") from e


def _need(v):
    if not v:
        raise KeyError("empty field")
    return v


def _q(s):
    return urllib.parse.quote(str(s), safe="")


def _ready(obj):
    for c in (obj.get("status") or {}).get("conditions") or []:
        if c.get("type") == "Ready":
            return c.get("status") == "True", c.get("lastTransitionTime")
    return False, None


def _cond(obj):
    """The Ready condition's (status string, lastTransitionTime); (None, None) if absent."""
    for c in (obj.get("status") or {}).get("conditions") or []:
        if c.get("type") == "Ready":
            return c.get("status"), c.get("lastTransitionTime")
    return None, None


def _lease(node):
    try:
        return (kget(LEASES + _q(node)).get("spec") or {}).get("renewTime")
    except Exception as e:
        if isinstance(e, urllib.error.HTTPError) and e.code == 404:
            return None
        raise Unread(f"Could not read the lease of host {node} ({e}), so Homestead cannot tell whether starting the pod elsewhere is safe.") from e


def read_state(ns, name, node):
    try:
        return _read_state(ns, name, node)
    except Unread:
        raise
    except (KeyError, TypeError, AttributeError, IndexError, ValueError) as e:
        raise Unread("Kubernetes or Longhorn answered in a form Homestead cannot read, so Homestead cannot tell "
                     "whether starting the pod elsewhere is safe.") from e


def _named(item):
    meta = item.get("metadata") if isinstance(item, dict) else None
    return (meta.get("name") if isinstance(meta, dict) else None) or "(without a name)"


def _each(items, kind, parse):
    """parse(item) for every item; a malformed one raises Unread naming its kind and name."""
    if not isinstance(items, list):
        raise Unread(f"The list of {kind}s is in a form Homestead cannot read.")
    out = []
    for item in items:
        try:
            out.append(parse(item))
        except Unread:
            raise
        except (KeyError, TypeError, AttributeError, IndexError, ValueError) as e:
            raise Unread(f"{kind[0].upper()}{kind[1:]} {_named(item)} is in a form Homestead cannot read, "
                         "so Homestead cannot tell whether starting the pod elsewhere is safe.") from e
    return out


def _lh_spec(item, kind):
    """(spec, volumeName) of a Longhorn replica or engine. Without them Homestead
    can't tell which volume the item belongs to, so it can't be skipped."""
    spec = item.get("spec") if isinstance(item, dict) else None
    if not isinstance(spec, dict):
        raise Unread(f"Longhorn {kind} {_named(item)} has no spec, so Homestead cannot read which volume it belongs to.")
    if not spec.get("volumeName"):
        raise Unread(f"Longhorn {kind} {_named(item)} has no spec.volumeName, so Homestead cannot read which volume it belongs to.")
    return spec, spec["volumeName"]


# Volume types that hold no data of the app's own: nothing to protect.
PLAIN_VOLUMES = ("configMap", "secret", "emptyDir", "projected", "downwardAPI")


def _pod_volumes(pod_name, volumes):
    """(claim names, volumes Homestead can't check) of one pod. A generic
    ephemeral volume is the claim Kubernetes made for it, `{pod}-{volume}`."""
    claims, unchecked = [], []
    for v in volumes or []:
        kinds = [k for k in v if k != "name"]
        kind = kinds[0] if len(kinds) == 1 else (" and ".join(sorted(kinds)) or "of no type")
        if kind == "persistentVolumeClaim":
            claims.append(_need(v["persistentVolumeClaim"]["claimName"]))
        elif kind == "ephemeral":
            claims.append(f"{pod_name}-{_need(v['name'])}")
        elif kind not in PLAIN_VOLUMES:
            unchecked.append({"name": v.get("name"), "type": kind})
    return list(dict.fromkeys(claims)), unchecked


def _read_state(ns, name, node):
    nodes = _read("/api/v1/nodes", "the hosts").get("items", [])
    ready = dict(_each(nodes, "host", lambda n: (n["metadata"]["name"], _ready(n))))
    if node not in ready:
        raise Unread(f"Host {node} is not in the cluster's host list, so Homestead cannot tell its state.")
    status = next((_cond(n)[0] for n in nodes if n["metadata"]["name"] == node), None)
    host = {"ready": ready[node][0], "status": status,
            "since": None if ready[node][0] else ready[node][1], "lease": _lease(node)}
    groups = _read("/apis", "the API groups").get("groups", [])
    harvester = "harvesterhci.io" in [g.get("name") for g in groups]

    sts = _read(f"/apis/apps/v1/namespaces/{_q(ns)}/statefulsets/{_q(name)}", f"StatefulSet {ns}/{name}")
    spec = sts.get("spec") or {}
    wait = FAILOVER.mode_of((spec.get("template") or {}).get("spec")) == "wait"
    labels = (spec.get("selector") or {}).get("matchLabels") or {}
    if not labels:
        raise Unread(f"StatefulSet {ns}/{name} has no label selector, so Homestead cannot find its pods.")
    selector = urllib.parse.quote(",".join(f"{k}={v}" for k, v in sorted(labels.items())))
    items = _read(f"/api/v1/namespaces/{_q(ns)}/pods?labelSelector={selector}", f"the pods of {ns}/{name}").get("items", [])

    def one_pod(p):
        meta = p.get("metadata") or {}
        owned = any(o.get("kind") == "StatefulSet" and o.get("name") == name for o in meta.get("ownerReferences") or [])
        if not owned or (p.get("spec") or {}).get("nodeName") != node:
            return None
        pod_name = _need(meta["name"])
        if not meta.get("uid"):
            # The delete is made exact by the uid: without one nothing may go out.
            raise Unread(f"Pod {pod_name} has no metadata.uid, so Homestead cannot delete exactly that pod.")
        claims, unchecked = _pod_volumes(pod_name, p["spec"].get("volumes"))
        return {"name": pod_name, "uid": meta["uid"], "node": node,
                "terminating": bool(meta.get("deletionTimestamp")), "claims": claims, "unchecked": unchecked}
    pods = [p for p in _each(items, "pod", one_pod) if p]
    claim_names = list(dict.fromkeys(c for p in pods for c in p["claims"]))

    pvcs = {}
    for c in claim_names:
        pvc = _read(f"/api/v1/namespaces/{_q(ns)}/persistentvolumeclaims/{_q(c)}", f"claim {c}")
        s = pvc.get("spec") or {}
        pvcs[c] = {"access": list(s.get("accessModes") or []), "volume": s.get("volumeName") or None}
    # Only the volumes under review count: Longhorn objects of every other
    # volume (detached ones have no host) are left alone, malformed or not.
    reviewed = {row["volume"] for row in pvcs.values() if row["volume"]}

    def one_lh_node(n):
        st = _cond(n)[0]
        return n["metadata"]["name"], None if st is None else st == "True"
    lh_nodes = dict(_each(_read(f"{LH}/nodes", "the Longhorn nodes").get("items", []), "Longhorn node", one_lh_node))
    replicas, engines, robustness = {}, {}, {}

    def one_replica(r):
        s, vol = _lh_spec(r, "replica")
        if vol in reviewed and s.get("nodeID"):  # no nodeID: not placed yet, never a copy
            replicas.setdefault(vol, []).append(
                {"node": s["nodeID"], "failed": bool(s.get("failedAt")), "healthy_at": bool(s.get("healthyAt"))})

    def one_engine(e):
        s, vol = _lh_spec(e, "engine")
        if vol not in reviewed:
            return
        state = (e.get("status") or {}).get("currentState")
        # Without a nodeID only a stopped engine is known to be detached; any
        # other is kept with no host, which blocks its volume.
        if s.get("nodeID") or state != "stopped":
            engines.setdefault(vol, []).append({"node": s.get("nodeID") or None, "state": state})

    def one_volume(v):
        vol = _need(v["metadata"]["name"])
        if vol in reviewed:
            robustness[vol] = (v.get("status") or {}).get("robustness")
    _each(_read(f"{LH}/replicas", "the Longhorn replicas").get("items", []), "Longhorn replica", one_replica)
    _each(_read(f"{LH}/engines", "the Longhorn engines").get("items", []), "Longhorn engine", one_engine)
    _each(_read("/apis/longhorn.io/v1beta2/volumes", "the Longhorn volumes").get("items", []), "Longhorn volume", one_volume)
    claims = {c: dict(row, longhorn=row["volume"] in robustness) for c, row in pvcs.items()}

    return {"node": host, "harvester": harvester, "set": {"ns": ns, "name": name, "wait_mode": wait},
            "pods": pods, "claims": claims, "lh_nodes": lh_nodes, "replicas": replicas, "engines": engines,
            "robustness": robustness, "nodes_ready": {n: r[0] for n, r in ready.items()}}


MIN_DOWN = 600  # seconds the host must have been NotReady


def _epoch(s):
    """Seconds since the epoch, or None when `s` is not a timestamp."""
    try:
        d = datetime.fromisoformat(s.replace("Z", "+00:00"))
        return (d if d.tzinfo else d.replace(tzinfo=timezone.utc)).timestamp()
    except (AttributeError, ValueError):
        return None


def _copies(state, volume, node):
    """Healthy copies of `volume` on hosts other than `node`, by host name: [(host, is_ready)]."""
    return sorted((r["node"], bool(state["nodes_ready"].get(r["node"])))
                  for r in state["replicas"].get(volume) or []
                  if r["node"] != node and not r["failed"] and r["healthy_at"])


def _chosen_copy(state, volume, node):
    """First healthy copy, by host name, on a Ready host other than `node`."""
    return next((n for n, up in _copies(state, volume, node) if up), None)


def _host_down(state, node, now):
    """True only when every measure says `node` is down: Kubernetes Ready is
    Unknown, NotReady and unheard from for MIN_DOWN, and Longhorn's node Ready
    is False. Checked on its own, apart from the blockers, so that accepting
    Longhorn's "unknown" can never rest on fewer conditions."""
    st = state["node"]
    since = _epoch(st["since"]) if st["since"] else None
    heard = _epoch(st["lease"]) if st["lease"] else None
    return (node is not None and st["ready"] is False and st.get("status") == "Unknown"
            and since is not None and now - since >= MIN_DOWN
            and heard is not None and now - heard >= MIN_DOWN
            and state["lh_nodes"].get(node) is False)


def plan(state, now):
    """What would be done, and what stands in the way. Pure: no reads.

    The host's name is taken from its pods (read_state keeps only pods on the
    host); with no pod there is nothing to start, which is itself a blocker."""
    st, pods = state["node"], state["pods"]
    node = pods[0]["node"] if pods else None
    blockers, volumes = [], []
    down_seconds = None
    if state["harvester"]:
        blockers.append("This cluster runs Harvester, which manages its own hosts and volumes. Starting a pod on another host is not supported here.")
    if st["ready"]:
        blockers.append("The host is Ready, so its pod is not stuck on a down host.")
    elif st.get("status") != "Unknown":
        blockers.append(f"The host's kubelet is still answering (Ready status: {st.get('status')}), so it is not unreachable.")
    since = _epoch(st["since"]) if st["since"] else None
    if not st["ready"]:
        if since is None:
            blockers.append("Homestead cannot read when the host went down.")
        else:
            down_seconds = int(now - since)
            if down_seconds < MIN_DOWN:
                blockers.append(f"The host has been down for {down_seconds // 60} minutes. It must be down for at least 10 minutes first.")
        heard = _epoch(st["lease"]) if st["lease"] else None
        if heard is None:
            blockers.append("Homestead cannot tell when the host last reported.")
        elif now - heard < MIN_DOWN:
            blockers.append("The host last reported less than 10 minutes ago.")
    if not pods:
        blockers.append("There is no pod of this StatefulSet on the host.")
    else:
        lh = state["lh_nodes"].get(node)
        if lh is None:
            blockers.append(f"Longhorn does not report {node} as down. Homestead cannot tell whether its volumes are still attached.")
        elif lh:
            blockers.append(f"Longhorn still sees {node} as Ready, so its volumes may still be in use.")
    host_down = _host_down(state, node, now)
    relied_on, seen = [], set()
    for pod in pods:
        for v in pod["unchecked"]:
            blockers.append(f"{pod['name']}: volume {v['name']} is {v['type']}, which Homestead cannot check")
        for pvc in pod["claims"]:
            if pvc in seen:
                continue
            seen.add(pvc)
            c = state["claims"].get(pvc)
            if not c or not c["volume"] or not c["longhorn"]:
                blockers.append(f"{pvc}: it is not a bound Longhorn volume, so Homestead cannot check where its data is.")
                continue
            vol = c["volume"]
            if "ReadWriteMany" in c["access"]:
                blockers.append(f"{pvc}: it is ReadWriteMany (shared), which this action does not handle.")
            copy_on = _chosen_copy(state, vol, node)
            # Longhorn reports "unknown" for what is on a host it can't reach
            # (#441's real host-loss test). It is read as "down" only when the
            # host is down by every other measure and a current copy is elsewhere.
            down_ok = host_down and copy_on is not None
            engines = state["engines"].get(vol) or []
            for e in engines:
                if e["node"] is None:
                    blockers.append(f"{pvc}: Longhorn cannot say where the engine of {vol} runs (state: {e['state']}).")
                    break
                if e["node"] == node and e["state"] == "unknown" and down_ok:
                    relied_on.append(f"Longhorn reports the engine of {vol} on {node} as unknown, as it does for a host that is down.")
                elif e["node"] == node and e["state"] != "stopped":
                    blockers.append(f"{pvc}: Longhorn cannot confirm that its engine has stopped. It is {vol} on {node} (state: {e['state']}).")
                    break
            rob = state["robustness"].get(vol)
            if rob == "unknown" and down_ok and engines and all(e["node"] == node for e in engines):
                relied_on.append(f"Longhorn reports {vol} as unknown, as it does for a volume whose engine is on a host that is down.")
            elif rob not in ("healthy", "degraded"):
                blockers.append(f"{pvc}: Longhorn reports {vol} as {rob}, not healthy or degraded.")
            if copy_on is None:
                others = [n for n, _ in _copies(state, vol, node)]
                if others:
                    blockers.append(f"{pvc}: its only other copy is on {others[0]}, which is not Ready either.")
                else:
                    blockers.append(f"{pvc}: its volume {vol} has no healthy copy on another host.")
            volumes.append({"pvc": pvc, "volume": vol, "copy_on": copy_on})
    relied_on = list(dict.fromkeys(relied_on))
    # What the person saw is what runs: the facts relied on are part of the review.
    review = {"node": node, "set": [state["set"]["ns"], state["set"]["name"]],
              "pods": sorted(p["uid"] for p in pods),
              "copies": sorted([v["volume"], v["copy_on"]] for v in volumes),
              "relied_on": sorted(relied_on)}
    token = hashlib.sha256(json.dumps(review, sort_keys=True).encode()).hexdigest()[:20]
    return {"ready": not blockers, "blockers": blockers, "node": node, "down_since": st["since"],
            "down_seconds": down_seconds, "last_heard": st["lease"], "set": dict(state["set"]),
            "pods": [{"name": p["name"], "uid": p["uid"], "terminating": p["terminating"]} for p in pods],
            "volumes": volumes, "relied_on": relied_on, "review_token": token}


KIND = "set-restart"
LIMIT = 900  # seconds the job follows the replacement pods
STAGES = ("deleted", "scheduled", "attaching volume", "ready")


def _plan(request):
    q = request.query
    arg = lambda k: (q.get(k) or [""])[0]  # noqa: E731
    try:
        return plan(read_state(arg("ns"), arg("name"), arg("node")), clock())
    except Unread as e:
        return {"ready": False, "blockers": [str(e)]}


def _active(ns, name):
    # The stored records: the public list leaves each job's ref out.
    for item in OPS.stored():
        ref = item.get("ref") or {}
        if item.get("kind") == KIND and (ref.get("ns"), ref.get("name")) == (ns, name) and item.get("status") not in OPS.TERMINAL:
            return item["id"]
    return ""


def _run(request):
    """Refused (409) whenever the review it carries no longer holds, so the
    browser shows the plan again. Order as node power: confirm, running job,
    re-plan, blockers, token, then act."""
    b = request.body
    ns, name, node = str(b.get("ns") or ""), str(b.get("name") or ""), str(b.get("node") or "")
    if not name or b.get("confirm") != name:
        raise ValueError("confirmation must repeat the StatefulSet name")
    running = _active(ns, name)
    if running:
        return ROUTER.Reply(409, {"error": "A restart of this StatefulSet is still running. Follow it instead of sending another.",
                                  "operation": {"id": running}})
    try:
        now = clock()
        reviewed = plan(read_state(ns, name, node), now)
    except Unread as e:
        return ROUTER.Reply(409, {"error": str(e), "plan": {"ready": False, "blockers": [str(e)]}})
    if not reviewed["ready"]:
        return ROUTER.Reply(409, {"error": "; ".join(reviewed["blockers"]), "plan": reviewed})
    if b.get("review_token") != reviewed["review_token"]:
        return ROUTER.Reply(409, {"error": "What the host holds changed. Review the plan again.", "plan": reviewed})
    deleted, replaced, failed = [], [], []
    for pod in reviewed["pods"]:
        try:
            ksend("DELETE", f"/api/v1/namespaces/{_q(ns)}/pods/{_q(pod['name'])}",
                  {"apiVersion": "v1", "kind": "DeleteOptions", "gracePeriodSeconds": 0, "preconditions": {"uid": pod["uid"]}})
            deleted.append(pod)
        except urllib.error.HTTPError as e:
            if e.code in (404, 409):
                replaced.append(pod)  # gone or already replaced: never retried
            else:
                failed.append({"name": pod["name"], "error": str(e)})
        except Exception as e:
            failed.append({"name": pod["name"], "error": str(e)})
    if failed and not deleted and not replaced:
        return ROUTER.Reply(502, {"error": "Homestead could not delete any of the pods: "
                                  + "; ".join(f"{f['name']}: {f['error']}" for f in failed), "not_deleted": failed})
    followed = [p for p in reviewed["pods"] if p in deleted or p in replaced]
    ref = {"ns": ns, "name": name, "node": reviewed["node"], "started": now, "started_by": request.user,
           "stage": "deleted" if deleted else "already replaced",
           "pods": [{"name": p["name"], "uid": p["uid"]} for p in followed]}
    names = lambda rows: ", ".join(p["name"] for p in rows)  # noqa: E731
    parts = ([f"Deleted {names(deleted)}"] if deleted else []) + [
        f"{names(replaced)} {'was' if len(replaced) == 1 else 'were'} already replaced"] * bool(replaced)
    parts += [f"could not delete {f['name']}: {f['error']}, so nothing was changed for it" for f in failed]
    if not deleted:
        parts.append("nothing was deleted")
    parts.append("waiting for a replacement on another host" if deleted else
                 ("waiting for it to be Ready on another host" if len(replaced) == 1 else "waiting for them to be Ready on another host"))
    try:
        op = OPS.start(KIND, f"Start {name} on another host", {"kind": "StatefulSet", "name": name, "namespace": ns},
                       "/nodes", ref, message="; ".join(parts))
    except ValueError:
        if not deleted:
            raise
        return ROUTER.Reply(409, {"error": "Pods were deleted, but another restart of this set is already being followed",
                                  "deleted": [{"name": p["name"]} for p in deleted], "operation": {"id": _active(ns, name)},
                                  "not_deleted": failed})
    if failed:
        op = dict(op, not_deleted=failed)
    return ROUTER.Reply(202, op)


def _pod_stage(pod, uid):
    """How far one replacement has got: (rank into STAGES, text, its host).
    The deleted pod (its old uid) never counts; a new one counts on any host,
    the one that went down included, should it have come back."""
    meta, spec = pod.get("metadata") or {}, pod.get("spec") or {}
    where = spec.get("nodeName")
    if meta.get("uid") == uid or not where:
        return 0, STAGES[0], None
    if _ready(pod)[0]:
        return 3, "ready", where
    status = pod.get("status") or {}
    started = status.get("containerStatuses") or any(
        c.get("type") == "Initialized" and c.get("status") == "True" for c in status.get("conditions") or [])
    return (2, "attaching volume", where) if started else (1, f"scheduled on {where}", where)


def status(item):
    """(status, progress, message): succeeded when every reviewed pod has a
    new Ready pod (on another host, or on its own should it come back);
    failed after LIMIT seconds."""
    ref = item["ref"]
    waited = clock() - float(ref.get("started") or 0)
    rows, unread = [], None
    for p in ref.get("pods") or []:
        try:
            pod = kget(f"/api/v1/namespaces/{_q(ref['ns'])}/pods/{_q(p['name'])}")
            rows.append((p["name"],) + _pod_stage(pod, p["uid"]))
        except urllib.error.HTTPError as e:
            if e.code == 404:
                rows.append((p["name"], 0, STAGES[0], None))  # not recreated yet
            else:
                unread = e
        except Exception as e:
            unread = e
    if rows and not unread:
        low = min(rows, key=lambda r: r[1])
        if low[1] or ref.get("stage") != "already replaced":
            ref["stage"] = low[2]  # "already replaced" stays until a new pod is seen
        if low[1] == 3:
            moved = [r[0] for r in rows if r[3] != ref["node"]]
            back = [f"{r[0]} is Ready on {r[3]}, which came back" for r in rows if r[3] == ref["node"]]
            return "succeeded", 100, "; ".join((["Ready on another host: " + ", ".join(moved)] if moved else []) + back)
    if waited > LIMIT:
        return "failed", 100, (f"No replacement was ready on another host within 15 minutes. "
                               f"The last stage reached was {ref.get('stage') or STAGES[0]}.")
    if unread:
        return "running", item.get("progress") or 10, f"Waiting to read the pods again ({unread}). Last stage: {ref.get('stage')}"
    rank = min(r[1] for r in rows) if rows else 0
    names = ", ".join(r[0] for r in rows if r[1] == rank)
    note = (f". Kubernetes can take about 6 minutes to release the volume from {ref['node']}"
            if ref["stage"] in ("deleted", "attaching volume") else "")
    return "running", (10, 40, 70)[rank], f"{names}: {ref['stage']}{note}"


# The job's own, registered at import like homestead_promote's. Nothing to
# cancel: the delete is done when the job starts.
OPS.RESOLVERS[KIND] = status

ROUTES = {
    ("GET", "/api/statefulset/restart-elsewhere/plan"): ("viewer", _plan),
    ("POST", "/api/statefulset/restart-elsewhere"): ("admin", _run),
}
