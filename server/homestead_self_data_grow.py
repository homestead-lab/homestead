"""Homestead's data, moved off local-path by itself once the cluster grows
(docs/plans/resilience-and-validated-updates.md, section 3.1 and decision 7).

A new one-host install keeps Homestead's data on k3s's local-path on the
system disk: Homestead then depends only on the host and Kubernetes, and
starts even when Longhorn does not. local-path keeps the volume on that one
host, though, so once a second host joins Homestead could not start anywhere
else if the first failed. So, once a second Ready host can keep a Longhorn
copy of the data, Homestead moves it to Longhorn by itself, two copies on
different hosts, mounted by one host at a time.

It is the same two-stage move a person reviews in Settings (prepare the new
volume, then copy, check and switch), run by the leader with its own name as
the actor:

- only on k3s or RKE2, when Homestead's data claim is on local-path, Longhorn
  is installed, two or more hosts that store Longhorn data have room for it,
  and one copy of Homestead runs;
- only at a quiet moment: no job running or waiting, and no OS update rollout
  under way. Until then it waits, and an alert says what it will do;
- Homestead is unavailable for about a minute while its data is copied and it
  restarts. The local-path volume is kept, as after any move;
- the phase is in Homestead's data (self-data-grow.json), so the copy of it on
  the new volume tells the Homestead that starts there how the move went;
- a failure is said with its reason, and tried again six hours later, three
  times at most. After that it is for a person to move, in Settings.

It moves only data it saw on local-path while the cluster had one host
(armed), and only once: a person who puts the data on local-path on a bigger
cluster keeps it there. It never moves data that is already on Longhorn, and
it does nothing on a cluster without Longhorn.
"""
import os
import secrets
import time

import homestead_data_copies as DATA_COPIES
import homestead_lhcapacity as LHCAP
import homestead_node_parity as NODE_PARITY
import homestead_shared as SHARED
import homestead_storage_classes as STORAGE_CLASSES

ACTOR = "Homestead (the cluster grew)"
LOCAL_PATH = "rancher.io/local-path"
RETRY = 6 * 3600
TRIES = 3
SHOW_DONE = 86400
DATA_DIR = None     # None: the self-data routes' own, as server.py binds them
# The steps of a person's move: the self-data routes' own, looked up when
# asked (server.py binds them there), unless bind() gives others (tests).
prepare = preview_move = start_move = None
jobs = blockers = rollout_running = own_node = None


def bind(data_dir, *, prepare_fn, preview_fn, start_fn, jobs_fn, blockers_fn, rollout_fn, node_fn):
    global DATA_DIR, prepare, preview_move, start_move, jobs, blockers, rollout_running, own_node
    DATA_DIR, prepare, preview_move, start_move = data_dir, prepare_fn, preview_fn, start_fn
    jobs, blockers, rollout_running, own_node = jobs_fn, blockers_fn, rollout_fn, node_fn


def _routes():
    import homestead_self_data_server as ROUTES
    return ROUTES


def _prepare_step(body, actor, start=False):
    return (prepare or _routes().self_data_preparation)(body, actor, start=start)


def _preview_step(body, actor):
    return (preview_move or _routes().preview_self_data_move)(body, actor)


def _start_step(body, actor):
    return (start_move or _routes().start_self_data_move)(body, actor)


def _jobs():
    if jobs:
        return jobs()
    import homestead_operations as OPS
    return OPS.stored()


def _blockers():
    if blockers:
        return blockers()
    import homestead_operations as OPS
    import homestead_self_data_prepare as PREPARE
    return PREPARE.blocking_jobs(OPS)


def _rolling():
    if rollout_running:
        return rollout_running()
    import homestead_os_rollout as ROLLOUT
    return (ROLLOUT._load().get("rollout") or {}).get("status") == "running"


def _node():
    return (own_node or _routes().own_node)()


def _path():
    return os.path.join(DATA_DIR or _routes().DATA_DIR, "self-data-grow.json")


def _load():
    return SHARED.read_json(_path(), {})


def _save(state):
    """Written whole. Whether the move is armed carries over unless state says."""
    if "armed" not in state:
        state = {**state, "armed": bool(_load().get("armed"))}
    SHARED.write_json(_path(), state, indent=1, sort_keys=True)


def _on_local_path(own):
    klass = NODE_PARITY.kget(f"/apis/storage.k8s.io/v1/storageclasses/{own['storage_class']}") or {}
    return not own["volume"] and klass.get("provisioner") == LOCAL_PATH


def wanted():
    """(own data volume, target class) when the move is due, else (None, why not)."""
    p = NODE_PARITY.platform() or {}
    if p.get("harvester") or p.get("distribution") not in ("k3s", "rke2") or not p.get("longhorn"):
        return None, "not a k3s or RKE2 cluster with Longhorn"
    own = next(iter(DATA_COPIES.own_data_volumes()), None)
    if not own or not _on_local_path(own):
        return None, "Homestead's data is not on local-path"
    ns, name = NODE_PARITY.OWN
    dep = NODE_PARITY.kget(f"/apis/apps/v1/namespaces/{ns}/deployments/{name}") or {}
    if int((dep.get("spec") or {}).get("replicas") or 1) != 1:
        return None, "more than one copy of Homestead runs"
    storing = NODE_PARITY._storage_hosts(NODE_PARITY._ready_nodes())
    if storing is None:
        raise ValueError("Longhorn's nodes could not be read")
    if not own["size_gb"]:
        raise ValueError("the size of Homestead's data volume could not be read")
    rooms = {n["name"]: n["room_gb"] for n in LHCAP.capacity()["nodes"]}
    if sum(1 for host in storing if rooms.get(host, 0) >= own["size_gb"]) < 2:
        return None, "fewer than two hosts that store Longhorn data have room for it"
    return own, DATA_COPIES.TWO_COPIES


def _quiet():
    """"" at a quiet moment, else what Homestead waits for. The jobs are the
    ones a person's move would wait for too (running, or keeping resources)."""
    if _blockers():
        return "the jobs that are running or keep resources to finish"
    if _rolling():
        return "the OS update rollout to finish"
    return ""


def _target_class():
    """longhorn-r2, made if the cluster has none, as Keep two copies makes it."""
    found = DATA_COPIES.two_copy_class()
    if found.get("exists") and not found.get("usable"):
        raise ValueError(f"{found.get('why')}, so Homestead's data was not moved")
    if not found.get("exists"):
        STORAGE_CLASSES.create_storage_class({"name": DATA_COPIES.TWO_COPIES, "replicas": 2, "migratable": False,
                                              "reclaim_policy": "Retain", "copies": "hosts", "hosts_only": True,
                                              "expandable": True})
    return DATA_COPIES.TWO_COPIES


def _prepare(own, now):
    node = _node()
    target = _target_class()
    operation = secrets.token_hex(12)
    body = {"operation": operation, "storage_class": target, "node": node, "one_host": True}
    review = _prepare_step(body, ACTOR, start=False)
    started = _prepare_step({**body, "capacity_token": review["capacity_token"], "confirm_capacity": True}, ACTOR, start=True)
    return {"phase": "preparing", "operation": operation, "destination": started["destination"],
            "source": own["claim"], "storage_class": target, "node": node, "at": now}


def _move(state, now):
    body = {"operation": state["operation"], "destination": state["destination"],
            "worker_node": state["node"], "copy_node": state["node"]}
    review = _preview_step(body, ACTOR)
    # The phase first: the copy taken once Homestead stops carries it to the new volume.
    _save({**state, "phase": "moving", "moved_at": now})
    try:
        _start_step({**body, "capacity_token": review["capacity_token"], "confirm_move": True, "confirm_capacity": True}, ACTOR)
    except Exception:
        _save({**state, "phase": "prepared"})
        raise
    return {**state, "phase": "moving", "moved_at": now}


def _failed(state, error, now):
    tries = int(state.get("tries") or 0) + 1
    return {"phase": "failed", "error": str(error)[:300] or type(error).__name__, "at": now, "tries": tries,
            "next_try": now + RETRY if tries < TRIES else None, "source": state.get("source", ""),
            "armed": bool(state.get("armed"))}


def tick(now=None):
    """One look, on the leader. Returns a line for the log when something changed, else ""."""
    now = time.time() if now is None else now
    state = _load()
    phase = state.get("phase", "idle")
    try:
        if phase == "moving":
            own = next(iter(DATA_COPIES.own_data_volumes()), None)
            if own and own["claim"] == state.get("destination"):
                _save({"phase": "done", "at": now, "destination": state["destination"], "source": state.get("source", ""),
                       "storage_class": state.get("storage_class", ""), "armed": False})
                return f"Homestead's data moved to {state.get('storage_class')}: {state['destination']}"
            moved = state.get("moved_at")
            if own and own["claim"] == state.get("source") and moved is not None and now - float(moved) > 3600:
                raise ValueError("the move did not finish, and Homestead is back on its original data")
            return ""
        if phase in ("preparing", "prepared"):
            item = next((row for row in _jobs() if (row.get("ref") or {}).get("operation") == state["operation"]), None)
            if item is None:
                raise ValueError("the preparation of the new volume is no longer listed in Jobs")
            if item.get("status") == "failed":
                raise ValueError(f"the new volume could not be prepared: {item.get('message') or 'no reason given'}")
            if item.get("status") != "succeeded" or not (item.get("ref") or {}).get("prepared"):
                return ""
            if _quiet():
                if phase != "prepared":
                    _save({**state, "phase": "prepared"})
                return ""
            _move(state, now)
            return f"Homestead's data is moving to {state['storage_class']}: Homestead restarts on {state['destination']}"
        if phase in ("done", "failed") and (phase == "done" or state.get("next_try") is None or now < float(state["next_try"])):
            return ""
        # Reads only, until the preparation starts: a read that fails is said in
        # the log and looked at again next minute, never counted as a try.
        try:
            p = NODE_PARITY.platform() or {}
            if p.get("harvester") or p.get("distribution") not in ("k3s", "rke2") or not p.get("longhorn"):
                return ""          # nothing to move to: the data stays where it is
            if not state.get("armed"):
                own = next(iter(DATA_COPIES.own_data_volumes()), None)
                if own and DATA_COPIES._one_host() and _on_local_path(own):
                    _save({**state, "armed": True})
                    return ("Homestead's data is on local-path on one host: it moves to Longhorn by itself "
                            "once a second host can keep a copy")
                return ""
            own, why = wanted()
            waiting = _quiet() if own is not None else ""
        except Exception as error:
            return f"Homestead's data move to Longhorn not checked: {str(error)[:160]}"
        if own is None:
            if phase == "waiting":
                _save({"phase": "idle"})
            return ""
        if waiting:
            if phase != "waiting":
                _save({**({"tries": state.get("tries")} if state.get("tries") else {}), "phase": "waiting",
                       "since": now, "source": own["claim"], "for": waiting})
                return f"Homestead's data waits for a quiet moment to move to Longhorn: {waiting}"
            return ""
        _save({**_prepare(own, now), **({"tries": state.get("tries")} if state.get("tries") else {})})
        return "Homestead's data: preparing its volume on Longhorn, now that a second host can keep a copy"
    except Exception as error:
        failed = _failed(state, error, now)
        _save(failed)
        return f"Homestead's data was not moved to Longhorn: {failed['error']}"


def alert_facts(now=None):
    """Before, during and after the move, and its failure, as alerts."""
    now = time.time() if now is None else now
    state = _load()
    phase = state.get("phase", "idle")
    if phase in ("waiting", "preparing", "prepared", "moving"):
        waiting = f" It waits for {state['for']}." if phase == "waiting" and state.get("for") else ""
        return [{"signals": {"moving": 1}, "key": "self-data-grow:moving", "category": "degraded", "severity": "info",
                 "title": "Homestead will move its data to Longhorn, now that a second host can keep a copy",
                 "resolved": "Homestead's data move to Longhorn ended",
                 "body": (f"Its data is on local-path, which keeps it on one host. Homestead moves it to "
                          f"{DATA_COPIES.TWO_COPIES}, two copies on different hosts, at a quiet moment with no jobs "
                          f"running.{waiting} Homestead is unavailable for about a minute while its data is copied and "
                          f"it restarts. The volume on local-path is kept."), "href": "/settings?tab=about"}]
    if phase == "done" and 0 <= now - float(state.get("at") or 0) <= SHOW_DONE:
        return [{"signals": {"moved": 1}, "key": f"self-data-grow:done:{int(state['at'])}", "category": "degraded",
                 "severity": "info", "title": "Homestead moved its data to Longhorn",
                 "resolved": "Homestead's data move to Longhorn is over a day old",
                 "body": (f"Homestead's data is now on {state.get('destination')}, {state.get('storage_class')}: two copies "
                          f"on different hosts, so Homestead can start on another host if this one fails. "
                          f"{state.get('source') or 'The local-path volume'} is kept. Delete it on Volumes once it is no longer needed."),
                 "href": "/volumes"}]
    if phase == "failed":
        again = ("It tries again in six hours." if state.get("next_try") else
                 "It does not try again by itself. Move it in Settings, About, Redundancy.")
        return [{"signals": {"failed": int(state.get("tries") or 1)}, "key": "self-data-grow:failed", "category": "degraded",
                 "severity": "degraded", "title": "Homestead could not move its data to Longhorn",
                 "resolved": "Homestead's data move to Longhorn no longer fails",
                 "body": f"Its data stays on local-path, on one host. The reason: {state.get('error')}. {again}",
                 "href": "/settings?tab=about"}]
    return []
