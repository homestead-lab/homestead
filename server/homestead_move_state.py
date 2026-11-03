"""What every part of a move shares: the clients server.py binds (through
homestead_move_engine.bind), the names, phases and limits, the locks, the move
records kept on Homestead's own volume, and the small helpers that ask the
source cluster about a move or name the objects it creates.

Split out of homestead_move_engine (#623). Tests replace the bound clients
here, not on homestead_move_engine.
"""
import base64
import contextlib
import json
import os
import re
import threading
import time
import urllib.error
import urllib.parse
import weakref
import homestead_shared as SHARED
import homestead_kube as KUBE
import homestead_names as NAMES


kget = ksend = None
LH = CLIENT = NETWORK = OPS = None
DATA_DIR = "/data"
STORE = "moves.json"
NS = "lab"
LHNS = "longhorn-system"
LH_API = "/apis/longhorn.io/v1beta2"
JOIN_SECRET = "homestead-move-credentials"
VIP_ANNOTATION = "kube-vip.io/loadbalancerIPs"
HARVESTER_IMAGE_CLASS = "harvesterhci.io/storageClassName"
MOVE_ID = "move-id"
MOVED_FROM = "moved-from"
TICK_SECONDS = 5
# Unreachable is waited out; this is how long before waiting stops being useful.
MAX_TRANSIENT = 120
START_GRACE = 15 * 60
MAX_MOVES = 50

PHASES = ("joining", "quiescing", "backing-up", "syncing", "restoring",
          "creating", "starting", "done")
# A volume on its own arrives once it is restored: nothing to create or start.
VOLUME_PHASES = ("joining", "quiescing", "backing-up", "syncing", "restoring", "done")
COPY_PHASES = ("joining", "quiescing", "backing-up", "releasing-source", "syncing", "restoring",
               "creating", "starting", "done")
KINDS = ("container", "vm", "volume")


class StoreUnavailable(ValueError):
    """The moves store's lock could not be taken, so nothing was read or written."""


class NotRecorded(ValueError):
    """A change on a cluster was made, and the moves store could not record it."""


# What a failed read or lock of the moves store raises; the engine stops its
# pass on these instead of failing the move.
STORE_ERRORS = (SHARED.Unreadable, StoreUnavailable)


class _MovesLock(SHARED.SharedLock):
    def _strict_acquire(self):
        try:
            super()._strict_acquire()
        except TimeoutError as error:
            raise StoreUnavailable(f"Another copy of Homestead has held the moves store ({STORE}) for over "
                                   f"{self.timeout} s, so this was not done; try again shortly") from error
        except OSError as error:
            raise StoreUnavailable(f"Homestead could not lock the moves store ({STORE}) on its data volume "
                                   f"({error.strerror or error}), so this was not done") from error


# Shared with any other Homestead replica on the same data volume. Strict: a
# lock only this process honours would let two replicas each write back their
# own copy of the moves and lose the other's.
_lock = _MovesLock("moves", strict=True, directory=lambda: DATA_DIR)


def _why(error):
    """Why the moves store failed, without its own "changed nothing"."""
    if isinstance(error, StoreUnavailable):
        cause = error.__cause__
        return "its lock was busy" if isinstance(cause, TimeoutError) else \
            f"it could not be locked: {getattr(cause, 'strerror', None) or cause}"
    cause = error.__cause__
    if isinstance(cause, OSError):
        return cause.strerror or str(cause)
    return "it is not valid JSON" if isinstance(cause, ValueError) else "it does not hold a list of moves"


@contextlib.contextmanager
def recording(done, what="it", then=""):
    """Writes to the store after a change on a cluster was made: if the store
    fails, say what was done and what was not recorded, never the store's own
    "changed nothing". done is a string or a function giving one."""
    try:
        yield
    except STORE_ERRORS as error:
        text = done() if callable(done) else done
        raise NotRecorded(f"{text}, but Homestead could not record {what} on its data volume ({_why(error)})."
                          + (f" {then}" if then else "")) from error


def _step_lock(move_id):
    """Keep transfer effects and cancellation exclusive across replicas."""
    if not isinstance(move_id, str) or not re.fullmatch(r"[0-9a-f]{12}", move_id):
        raise ValueError("Invalid transfer identity")
    return SHARED.SharedLock("move-step-" + move_id, strict=True, directory=lambda: DATA_DIR, timeout=60)


_step_locks = weakref.WeakValueDictionary()
_step_locks_guard = threading.Lock()


def _serialized(move_id):
    # SharedLock's thread mutex must be shared by callers in this process too.
    with _step_locks_guard:
        return _step_locks.setdefault(move_id, _step_lock(move_id))


def _now():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _path():
    return os.path.join(DATA_DIR, STORE)


def _read():
    """Every move recorded here. A missing file is no moves (a fresh install);
    one that is there but cannot be read raises SHARED.Unreadable, so it is
    never taken for "no moves" and written back over the moves it holds."""
    return SHARED.read_json(_path(), [])


def _write(rows):
    protected = [m for m in rows if m.get("status") in ("running", "failed")
                 or (m.get("flags") or {}).get("resource_cleanup_pending")
                 or (m.get("flags") or {}).get("target_cleanup_pending")]
    historical = [m for m in rows if m not in protected][-MAX_MOVES:]
    SHARED.write_json(_path(), sorted(protected + historical, key=lambda m: m.get("created_at", "")),
                      durable=True, mode=0o600, separators=(",", ":"))


def _find(move_id):
    with _lock:
        return next((m for m in _read() if m["id"] == move_id), None)


def _store(move):
    """Write a move, last writer wins: whoever changes an existing move holds
    its step lock (_serialized). Each write bumps its rev, which tells a holder
    of an older copy that someone has written since: updated_at has one-second
    ties."""
    with _lock:
        rows = _read()
        before = next((m for m in rows if m["id"] == move["id"]), {})
        move["rev"] = int(before.get("rev", 0)) + 1
        rows = [m for m in rows if m["id"] != move["id"]]
        rows.append(move)
        rows.sort(key=lambda m: m.get("created_at", ""))
        _write(rows)


def _update(move, flags=None, **fields):
    """Store the caller's copy if nobody has written the move since it was
    read; otherwise write only these fields onto the newer record, so an older
    copy never undoes someone else's write. A dismissed move stays dismissed."""
    with _lock:
        current = _find(move["id"])
        if current is None:
            return
        if current.get("rev", 0) == move.get("rev", 0):
            return _store(move)
        current.setdefault("flags", {}).update(flags or {})
        current.update(fields)
        _store(current)


def _public(move):
    """What the browser sees. No keys, no definitions, nothing from Secrets."""
    return {key: move.get(key) for key in (
        "id", "cluster", "kind", "name", "source_namespace", "namespace", "status", "phase",
        "progress", "message", "created_at", "updated_at", "finished_at", "previous_target",
        "source_removed", "address", "address_mode", "storage_class", "transfer_mode")} | {
        # Nothing has stopped on the source yet: undoing it is a cancel, not a
        # "put back".
        "source_stopped": _source_held(move),
        "cleanup_pending": bool((move.get("flags") or {}).get("cancelling")) and move.get("status") == "failed",
        "backup_target_cleanup_pending": bool((move.get("flags") or {}).get("target_cleanup_pending")),
        "resource_cleanup_pending": bool((move.get("flags") or {}).get("resource_cleanup_pending")),
        "cleanup_message": move.get("cleanup_message", ""),
        "can_dismiss": not ((move.get("flags") or {}).get("resource_cleanup_pending")
                            or (move.get("flags") or {}).get("target_cleanup_pending")
                            or (move.get("status") == "failed" and move.get("cleanup_protocol") == 1
                                and ((move.get("flags") or {}).get("quiesced") or (move.get("flags") or {}).get("quiesce_requested")))),
        "claims": [{**{k: c.get(k) for k in ("claim", "size_gb", "backup", "created", "restored")},
                    "action": c.get("action", "move"), "storage_class": c.get("target_class") or move.get("storage_class") or ""}
                   for c in move.get("claims", [])],
        "phase_index": _phases(move).index(move["phase"]) if move.get("phase") in _phases(move) else 0,
        "phases": list(_phases(move)),
    }


def _phases(move):
    if move.get("transfer_mode") == "copy":
        return COPY_PHASES
    return VOLUME_PHASES if move.get("kind") == "volume" else PHASES


def _source_held(move):
    flags = move.get("flags") or {}
    return bool(flags.get("quiesced") or flags.get("quiesce_requested")) and not flags.get("source_released")


def moves():
    with _lock:
        rows = _read()
    rows.sort(key=lambda m: m.get("created_at", ""), reverse=True)
    return [_public(m) for m in rows]


def _q(**params):
    return "?" + urllib.parse.urlencode(params)


def _here_endpoint(target):
    secret_name = (target or {}).get("secret") or ""
    if not secret_name:
        return ""
    secret = KUBE.get_optional(f"/api/v1/namespaces/{LHNS}/secrets/{secret_name}", read=kget) or {}
    raw = (secret.get("data") or {}).get("AWS_ENDPOINTS", "")
    try:
        return base64.b64decode(raw).decode()
    except Exception:
        return ""


def _same_target(here, there):
    """Whether this cluster's Longhorn already reads the bucket the source writes."""
    return (here.get("configured") and here.get("url") == there.get("url")
            and _here_endpoint(here) == there.get("endpoint", ""))


def _definition(move):
    if move.get("definition"):
        return json.loads(json.dumps(move["definition"]))
    return CLIENT.remote(move["cluster"], "/api/move/definition"
                         + _q(kind=move["kind"], name=move["name"], namespace=move.get("source_namespace", "")))


def _source_status(move):
    return CLIENT.remote(move["cluster"], "/api/move/source-status"
                         + _q(kind=move["kind"], name=move["name"], namespace=move.get("source_namespace", "")))


def _source_action(move, action, **extra):
    if move.get("cleanup_protocol") == 1:
        extra["expected_uid"] = move["source_uid"]
    if move.get("transfer_mode") == "copy":
        extra.update(transfer_id=move["id"], expected_uid=move["source_uid"])
        if action == "quiesce":
            extra["expected_version"] = move.get("flags", {}).get("quiesce_version", "")
    return CLIENT.remote(move["cluster"], "/api/move/source",
                         dict({"action": action, "kind": move["kind"], "name": move["name"],
                               "namespace": move.get("source_namespace", "")},
                              **extra))


def _object_path(kind, namespace, name):
    if kind == "vm":
        return f"/apis/kubevirt.io/v1/namespaces/{namespace}/virtualmachines/{name}"
    if kind == "volume":
        return f"/api/v1/namespaces/{namespace}/persistentvolumeclaims/{name}"
    return f"/apis/apps/v1/namespaces/{namespace}/deployments/{name}"


def _collection_path(kind, namespace):
    if kind == "vm":
        return f"/apis/kubevirt.io/v1/namespaces/{namespace}/virtualmachines"
    if kind == "volume":
        return f"/api/v1/namespaces/{namespace}/persistentvolumeclaims"
    return f"/apis/apps/v1/namespaces/{namespace}/deployments"


def _ours(obj, move_id):
    return NAMES.annotation_of((obj or {}).get("metadata"), MOVE_ID) == move_id


def _service_ports(service, definition):
    """A Service's listeners as Homestead's service planner wants them."""
    named = {}
    containers = ((((definition.get("object") or {}).get("spec", {}) or {})
                   .get("template", {}) or {}).get("spec", {}) or {}).get("containers", []) or []
    for container in containers:
        for port in container.get("ports", []) or []:
            if port.get("name"):
                named[port["name"]] = port.get("containerPort")
    rows = []
    for port in (service.get("spec", {}) or {}).get("ports", []) or []:
        target = port.get("targetPort", port.get("port"))
        if isinstance(target, str) and not target.isdigit():
            target = named.get(target, port.get("port"))
        rows.append({"name": port.get("name"), "port": port.get("port"),
                     "target_port": target, "protocol": port.get("protocol", "TCP")})
    return rows


def _plan_service(service, definition, namespace, name, mode, address, chosen=None):
    spec = service.get("spec", {}) or {}
    load_balanced = spec.get("type") == "LoadBalancer"
    return NETWORK.service_plan({
        "namespace": namespace, "name": service["metadata"]["name"], "workload": name,
        "type": "LoadBalancer" if load_balanced else "ClusterIP",
        "vip_mode": ("manual" if chosen else mode) if load_balanced else "cluster",
        "vip": chosen or address, "ports": _service_ports(service, definition),
    }, require_workload=False)


def _moving(move):
    return [c for c in move["claims"] if c.get("action", "move") == "move"]


def _note(move, progress, message):
    move.update(progress=max(move.get("progress", 0), int(progress)), message=message)


def _advance(move, phase, progress, message):
    move["phase"] = phase
    _note(move, progress, message)
