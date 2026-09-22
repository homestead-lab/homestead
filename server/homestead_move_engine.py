"""The destination half of a move, and the part that drives it.

A move is a sequence of phases, each one a question with a yes-or-not-yet
answer: are both clusters writing to the same bucket, has the workload stopped
over there, are its backups done, can this cluster's Longhorn see them, have
the claims been restored, does the workload exist here, is it running.

The engine asks the current phase's question every few seconds and moves on
when the answer is yes. Nothing waits in memory: the move's state is written to
Homestead's own volume after every step, and every step is safe to repeat, so
a restart part-way through - of this Homestead or the other one - picks up
exactly where it was. Data moves take as long as data takes; nothing here has
a timeout that would abandon a large volume halfway.
"""
import base64
import json
import os
import secrets
import threading
import time
import urllib.error
import urllib.parse

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
MOVE_ID = "move-id"
MOVED_FROM = "moved-from"
TICK_SECONDS = 5
# Unreachable is waited out; this is how long before waiting stops being useful.
MAX_TRANSIENT = 120
START_GRACE = 15 * 60
MAX_MOVES = 50

PHASES = ("joining", "quiescing", "backing-up", "syncing", "restoring",
          "creating", "starting", "done")
_lock = threading.RLock()


def bind(_kget, _ksend, longhorn, client, network, operations, data_dir, namespace):
    global kget, ksend, LH, CLIENT, NETWORK, OPS, DATA_DIR, NS
    kget, ksend, LH, CLIENT, NETWORK, OPS = _kget, _ksend, longhorn, client, network, operations
    DATA_DIR, NS = data_dir, namespace
    NAMES.bind(_kget)


# ---------------------------------------------------------------- persistence
def _now():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _path():
    return os.path.join(DATA_DIR, STORE)


def _read():
    try:
        with open(_path(), encoding="utf-8") as handle:
            rows = json.load(handle)
        return rows if isinstance(rows, list) else []
    except (OSError, ValueError, TypeError):
        return []


def _write(rows):
    os.makedirs(DATA_DIR, exist_ok=True)
    tmp = _path() + ".tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(rows[-MAX_MOVES:], handle, separators=(",", ":"))
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, _path())


def _find(move_id):
    with _lock:
        return next((m for m in _read() if m["id"] == move_id), None)


def _store(move):
    with _lock:
        rows = [m for m in _read() if m["id"] != move["id"]]
        rows.append(move)
        rows.sort(key=lambda m: m.get("created_at", ""))
        _write(rows)


def _public(move):
    """What the browser sees. No keys, no definitions, nothing from Secrets."""
    return {key: move.get(key) for key in (
        "id", "cluster", "kind", "name", "source_namespace", "namespace", "status", "phase",
        "progress", "message", "created_at", "updated_at", "finished_at", "previous_target",
        "source_removed", "address", "address_mode")} | {
        "claims": [{k: c.get(k) for k in ("claim", "size_gb", "backup", "created", "restored")}
                   for c in move.get("claims", [])],
        "phase_index": PHASES.index(move["phase"]) if move.get("phase") in PHASES else 0,
        "phases": list(PHASES),
    }


def moves():
    with _lock:
        rows = _read()
    rows.sort(key=lambda m: m.get("created_at", ""), reverse=True)
    return [_public(m) for m in rows]


# ------------------------------------------------------------------- helpers
def _get(path):
    try:
        return kget(path)
    except urllib.error.HTTPError as error:
        if error.code == 404:
            return None
        raise


def _q(**params):
    return "?" + urllib.parse.urlencode(params)


def _here_endpoint(target):
    secret_name = (target or {}).get("secret") or ""
    if not secret_name:
        return ""
    secret = _get(f"/api/v1/namespaces/{LHNS}/secrets/{secret_name}") or {}
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
    return CLIENT.remote(move["cluster"], "/api/move/definition"
                         + _q(kind=move["kind"], name=move["name"]))


def _source_status(move):
    return CLIENT.remote(move["cluster"], "/api/move/source-status"
                         + _q(kind=move["kind"], name=move["name"]))


def _source_action(move, action, **extra):
    return CLIENT.remote(move["cluster"], "/api/move/source",
                         dict({"action": action, "kind": move["kind"], "name": move["name"]},
                              **extra))


def _object_path(kind, namespace, name):
    if kind == "vm":
        return f"/apis/kubevirt.io/v1/namespaces/{namespace}/virtualmachines/{name}"
    return f"/apis/apps/v1/namespaces/{namespace}/deployments/{name}"


def _collection_path(kind, namespace):
    if kind == "vm":
        return f"/apis/kubevirt.io/v1/namespaces/{namespace}/virtualmachines"
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


# -------------------------------------------------------------------- the plan
def plan(cluster, kind, name, namespace=None, address_mode="shared", address=""):
    """Everything that would stop a move, or surprise someone, before it starts.

    Asks both clusters, and reports blockers and warnings separately: a blocker
    means the move would fail, a warning means it would work in a way someone
    should know about first.
    """
    namespace = namespace or NS
    blockers, warnings = [], []
    if kind not in ("container", "vm"):
        raise ValueError("kind must be container or vm")
    if address_mode not in ("shared", "automatic", "manual"):
        raise ValueError("address must be shared, automatic or manual")
    versions = CLIENT.check_cluster(cluster)
    if versions.get("compatible") is False:
        return {"ok": False, "blockers": [versions["message"]], "warnings": [], "claims": [],
                "versions": versions}
    if versions.get("state") == "differs":
        warnings.append(versions["message"])
    try:
        definition = CLIENT.remote(cluster, "/api/move/definition" + _q(kind=kind, name=name))
        there = CLIENT.remote(cluster, "/api/move/target")
    except CLIENT.Unreachable as error:
        return {"ok": False, "blockers": [str(error)], "warnings": [], "claims": []}
    except ValueError as error:
        return {"ok": False, "blockers": [str(error)], "warnings": [], "claims": []}

    here = LH.backup_target()
    joined = bool(_same_target(here, there))
    if not joined:
        if not there.get("reachable_off_cluster"):
            blockers.append(f"backup storage on {cluster} is only reachable inside that cluster; "
                            "give its object store a LAN address first")
        if here.get("configured"):
            warnings.append(f"this cluster's Longhorn backup target changes from {here.get('url')} "
                            f"to {there.get('url')}; backups already written to the old one stay there")

    if not _get(f"/api/v1/namespaces/{namespace}"):
        warnings.append(f"namespace {namespace} does not exist here and will be created")
    if _get(_object_path(kind, namespace, name)):
        blockers.append(f"{name} already exists in {namespace} on this cluster")
    for claim in definition.get("claims", []):
        if _get(f"/api/v1/namespaces/{namespace}/persistentvolumeclaims/{claim['claim']}"):
            blockers.append(f"volume {claim['claim']} already exists in {namespace} here")
        if claim.get("backing_image") and not _get(
                f"{LH_API}/namespaces/{LHNS}/backingimages/{claim['backing_image']}"):
            warnings.append(f"disk {claim['claim']} is built on the {claim['backing_image']} image, "
                            "which will be restored from its backup first")
    base = _get(f"/apis/storage.k8s.io/v1/storageclasses/{LH.STORAGE_CLASS}")
    if not base or base.get("provisioner") != "driver.longhorn.io":
        blockers.append(f"storage class {LH.STORAGE_CLASS} is missing here or is not Longhorn")

    selector = definition.get("node_selector") or {}
    if selector:
        try:
            nodes = kget("/api/v1/nodes").get("items", [])
        except Exception:
            nodes = []
        fits = [n for n in nodes if all(((n.get("metadata", {}) or {}).get("labels", {}) or {})
                                        .get(k) == v for k, v in selector.items())]
        if not fits:
            warnings.append("no node here carries " + ", ".join(f"{k}={v}" for k, v in selector.items())
                            + ", so it will wait unscheduled until one does")
    for secret in definition.get("pull_secrets", []) or []:
        if not _get(f"/api/v1/namespaces/{namespace}/secrets/{secret}"):
            warnings.append(f"image pull secret {secret} does not exist here, so a private image "
                            "may not pull")
    for network in definition.get("networks", []) or []:
        net_ns, _, net_name = network.rpartition("/")
        if not _get(f"/apis/k8s.cni.cncf.io/v1/namespaces/{net_ns or namespace}/"
                    f"network-attachment-definitions/{net_name}"):
            warnings.append(f"network {network} does not exist here; the VM will not start "
                            "until it does")

    addresses = []
    chosen = None
    for service in definition.get("services", []):
        try:
            planned = _plan_service(service, definition, namespace, name, address_mode,
                                    address, chosen)
        except (ValueError, PermissionError) as error:
            blockers.append(f"service {service['metadata']['name']}: {error}")
            continue
        warnings.extend(planned.get("warnings") or [])
        if planned.get("vip"):
            chosen = chosen or planned["vip"]
            addresses.append(f"{service['metadata']['name']} on {planned['vip']}")

    origin = definition.get("origin") or {}
    will_run = (origin.get("replicas", 0) > 0 if kind == "container"
                else origin.get("runStrategy", "Halted") != "Halted" or origin.get("running"))
    return {
        "ok": not blockers, "blockers": blockers,
        "warnings": list(dict.fromkeys(warnings)),
        "cluster": cluster, "kind": kind, "name": name, "namespace": namespace,
        "joined": joined, "will_run": bool(will_run), "addresses": addresses,
        "versions": versions,
        "claims": [{k: c.get(k) for k in ("claim", "size_gb", "access_mode", "volume_mode",
                                          "backing_image")}
                   for c in definition.get("claims", [])],
        "total_gb": sum(int(c.get("size_gb") or 0) for c in definition.get("claims", [])),
    }


def start(cluster, kind, name, namespace=None, address_mode="shared", address=""):
    namespace = namespace or NS
    checked = plan(cluster, kind, name, namespace, address_mode, address)
    if not checked["ok"]:
        raise ValueError(checked["blockers"][0])
    active = [m for m in _read() if m.get("status") == "running"
              and (m["cluster"], m["kind"], m["name"]) == (cluster, kind, name)]
    if active:
        raise ValueError(f"{name} is already being moved")
    definition = _definition({"cluster": cluster, "kind": kind, "name": name})
    move = {
        "id": secrets.token_hex(6), "cluster": cluster, "kind": kind, "name": name,
        "source_namespace": definition.get("namespace", ""), "namespace": namespace,
        "address_mode": address_mode, "address": address,
        "status": "running", "phase": "joining", "progress": 1,
        "message": "Queued", "claims": [dict(c, backup="", created=False, restored=False)
                                        for c in definition.get("claims", [])],
        "origin": definition.get("origin") or {}, "flags": {},
        "previous_target": "", "failures": 0, "source_removed": False,
        "created_at": _now(), "updated_at": _now(), "finished_at": "",
    }
    move["op"] = _operation(move)
    _store(move)
    return _public(move)


def _operation(move):
    item = OPS.start("move", f"Move {move['name']} from {move['cluster']}",
                     {"kind": "VirtualMachine" if move["kind"] == "vm" else "Deployment",
                      "name": move["name"], "namespace": move["namespace"]},
                     "/import", {"move": move["id"]}, message="Queued")
    return item["id"]


def op_state(item):
    """How a move reads in the Activity tray."""
    move = _find((item.get("ref") or {}).get("move", ""))
    if not move:
        return "failed", item.get("progress", 0), "This move's record is gone"
    return move["status"], move.get("progress", 0), move.get("message", "")


# ------------------------------------------------------------------ the phases
def _note(move, progress, message):
    move.update(progress=max(move.get("progress", 0), int(progress)), message=message)


def _advance(move, phase, progress, message):
    move["phase"] = phase
    _note(move, progress, message)


def _joining(move):
    there = CLIENT.remote(move["cluster"], "/api/move/target")
    here = LH.backup_target()
    if _same_target(here, there):
        return _advance(move, "quiescing", 4, "Both clusters share backup storage")
    credentials = there.get("credentials") or {}
    body = {"apiVersion": "v1", "kind": "Secret", "type": "Opaque",
            "metadata": {"name": JOIN_SECRET, "namespace": LHNS,
                         "labels": {NAMES.key("managed"): "true"}},
            "stringData": {str(k): str(v) for k, v in credentials.items()}}
    if _get(f"/api/v1/namespaces/{LHNS}/secrets/{JOIN_SECRET}"):
        ksend("PUT", f"/api/v1/namespaces/{LHNS}/secrets/{JOIN_SECRET}", body)
    else:
        ksend("POST", f"/api/v1/namespaces/{LHNS}/secrets", body)
    move["previous_target"] = here.get("url", "") if here.get("configured") else ""
    # Checked often while a move waits on it; Longhorn's default is minutes.
    LH.set_backup_target(there["url"], JOIN_SECRET, poll="30s")
    return _advance(move, "quiescing", 4, f"Reading backups from {move['cluster']}'s storage")


def _quiescing(move):
    flags = move.setdefault("flags", {})
    if not flags.get("quiesced"):
        _source_action(move, "quiesce")
        flags["quiesced"] = True
    status = _source_status(move)
    if status.get("running"):
        return _note(move, 6, f"Waiting for {move['name']} to stop on {move['cluster']}")
    return _advance(move, "backing-up", 10, f"{move['name']} stopped on {move['cluster']}")


def _backing_up(move):
    flags = move.setdefault("flags", {})
    if not flags.get("backed_up"):
        made = _source_action(move, "backup")
        by_claim = {row["claim"]: row["backup"] for row in made.get("backups", [])}
        for claim in move["claims"]:
            claim["backup"] = by_claim.get(claim["claim"], claim.get("backup", ""))
        flags["backed_up"] = True
    rows = _source_status(move).get("backups", [])
    for row in rows:
        state = str(row.get("state", "")).lower()
        if state in ("error", "failed") or row.get("error"):
            raise ValueError(f"backup of {row['claim']} failed on {move['cluster']}: "
                             f"{row.get('error') or state}")
        image = row.get("image") or {}
        if str(image.get("state", "")).lower() in ("error", "failed") or image.get("error"):
            raise ValueError(f"backup of the {row.get('backing_image')} image failed: "
                             f"{image.get('error') or image.get('state')}")
    if not rows and move["claims"]:
        return _note(move, 10, "Waiting for backups to start")
    done = [r for r in rows if str(r.get("state", "")).lower() == "completed"
            and (not r.get("backing_image")
                 or str((r.get("image") or {}).get("state", "")).lower() in ("completed", "ready"))]
    average = sum(int(r.get("progress") or 0) for r in rows) / max(1, len(rows))
    if len(done) == len(move["claims"]):
        return _advance(move, "syncing", 50, "Every volume is backed up")
    return _note(move, 10 + 40 * average / 100,
                 f"Backing up {len(rows)} volume{'' if len(rows) == 1 else 's'} on "
                 f"{move['cluster']}: {int(average)}%")


def _request_sync(move):
    flags = move.setdefault("flags", {})
    if time.time() - float(flags.get("synced_at", 0)) < 60:
        return
    flags["synced_at"] = time.time()
    try:
        ksend("PATCH", f"{LH_API}/namespaces/{LHNS}/backuptargets/default",
              {"spec": {"syncRequestedAt": _now()}}, ctype="application/merge-patch+json")
    except Exception:
        pass                # Longhorn will look on its own interval regardless


def _syncing(move):
    _request_sync(move)
    here = {row["name"]: row for row in LH.backups()}
    waiting = [c["claim"] for c in move["claims"] if not here.get(c["backup"], {}).get("restorable")]
    # An image a disk is built on has to be readable here too, or the disk
    # cannot be restored however visible its own backup is.
    for image in {c["backing_image"] for c in move["claims"] if c.get("backing_image")}:
        if _get(f"{LH_API}/namespaces/{LHNS}/backingimages/{image}"):
            continue
        found = _get(f"{LH_API}/namespaces/{LHNS}/backupbackingimages/{image}")
        if not ((found or {}).get("status", {}) or {}).get("url"):
            waiting.append(f"the {image} image")
    if waiting:
        return _note(move, 52, f"Waiting for this cluster's Longhorn to see "
                               f"{len(waiting)} backup{'' if len(waiting) == 1 else 's'}")
    return _advance(move, "restoring", 55, "Backups visible here")


def _ensure_namespace(namespace):
    if _get(f"/api/v1/namespaces/{namespace}"):
        return
    ksend("POST", "/api/v1/namespaces", {"apiVersion": "v1", "kind": "Namespace",
                                         "metadata": {"name": namespace}})


def _image_ready(name):
    image = _get(f"{LH_API}/namespaces/{LHNS}/backingimages/{name}")
    if not image:
        return False
    files = ((image.get("status", {}) or {}).get("diskFileStatusMap") or {}).values()
    return any(str(f.get("state", "")).lower() == "ready" for f in files)


def _ensure_image(move, name):
    """Restore a Harvester image from its backup before the disks built on it."""
    if _get(f"{LH_API}/namespaces/{LHNS}/backingimages/{name}"):
        return
    backup = _get(f"{LH_API}/namespaces/{LHNS}/backupbackingimages/{name}")
    url = ((backup or {}).get("status", {}) or {}).get("url", "")
    if not url:
        raise CLIENT.Unreachable(f"the {name} image backup is not visible here yet")
    ksend("POST", f"{LH_API}/namespaces/{LHNS}/backingimages", {
        "apiVersion": "longhorn.io/v1beta2", "kind": "BackingImage",
        "metadata": {"name": name, "namespace": LHNS,
                     "annotations": {NAMES.key(MOVE_ID): move["id"]}},
        "spec": {"sourceType": "restore",
                 "sourceParameters": {"backup-url": url, "concurrent-limit": "2"}}})


def _restore_progress(namespace, claim):
    """(finished, percent) for one claim being restored from a backup."""
    pvc = _get(f"/api/v1/namespaces/{namespace}/persistentvolumeclaims/{claim}")
    if not pvc or (pvc.get("status", {}) or {}).get("phase") != "Bound":
        return False, 0
    volume_name = (pvc.get("spec", {}) or {}).get("volumeName", "")
    volume = _get(f"{LH_API}/namespaces/{LHNS}/volumes/{volume_name}") if volume_name else None
    if not volume:
        return False, 0
    status = volume.get("status", {}) or {}
    if not status.get("restoreRequired") and status.get("state") in ("detached", "attached"):
        return True, 100
    percents = []
    try:
        engines = kget(f"{LH_API}/namespaces/{LHNS}/engines").get("items", [])
    except Exception:
        engines = []
    for engine in engines:
        if (engine.get("spec", {}) or {}).get("volumeName") != volume_name:
            continue
        for row in ((engine.get("status", {}) or {}).get("restoreStatus") or {}).values():
            if row.get("error"):
                raise ValueError(f"restoring {claim} failed: {row['error']}")
            percents.append(int(row.get("progress") or 0))
    return False, (sum(percents) / len(percents)) if percents else 0


def _restoring(move):
    namespace = move["namespace"]
    _ensure_namespace(namespace)
    for claim in move["claims"]:
        if claim.get("created"):
            continue
        existing = _get(f"/api/v1/namespaces/{namespace}/persistentvolumeclaims/{claim['claim']}")
        if existing:
            if not _ours(existing, move["id"]):
                raise ValueError(f"volume {claim['claim']} already exists in {namespace} "
                                 "and was not made by this move")
            claim["created"] = True
            continue
        if claim.get("backing_image") and not _image_ready(claim["backing_image"]):
            _ensure_image(move, claim["backing_image"])
            return _note(move, 56, f"Restoring the {claim['backing_image']} image first")
        LH.restore_backup({
            "backup": claim["backup"], "namespace": namespace, "name": claim["claim"],
            "size_gb": claim.get("size_gb"), "access_mode": claim.get("access_mode"),
            "replicas": claim.get("replicas") or 2, "volume_mode": claim.get("volume_mode"),
            "migratable": claim.get("migratable"), "backing_image": claim.get("backing_image"),
            "annotations": {NAMES.key(MOVE_ID): move["id"],
                            NAMES.key(MOVED_FROM): f"{move['cluster']}/{move['name']}"}})
        claim["created"] = True
    states = []
    for claim in move["claims"]:
        finished, percent = _restore_progress(namespace, claim["claim"])
        claim["restored"] = finished
        states.append(100 if finished else percent)
    if all(c["restored"] for c in move["claims"]):
        return _advance(move, "creating", 86, "Every volume is restored here")
    average = sum(states) / max(1, len(states))
    return _note(move, 56 + 29 * average / 100,
                 f"Restoring {len(states)} volume{'' if len(states) == 1 else 's'} here: {int(average)}%")


def _post_ours(path, collection, body, move):
    """Create something, treating 'it exists and this move made it' as done."""
    existing = _get(path)
    if existing:
        if _ours(existing, move["id"]):
            return
        raise ValueError(f"{path.rsplit('/', 1)[-1]} already exists here and was not made "
                         "by this move")
    ksend("POST", collection, body)


def _stamp(meta, move, namespace):
    meta["namespace"] = namespace
    annotations = meta.setdefault("annotations", {})
    annotations[NAMES.key(MOVE_ID)] = move["id"]
    annotations[NAMES.key(MOVED_FROM)] = f"{move['cluster']}/{move['name']}"
    return meta


def _creating(move):
    namespace, name = move["namespace"], move["name"]
    definition = _definition(move)
    for secret in definition.get("secrets", []):
        _stamp(secret["metadata"], move, namespace)
        _post_ours(f"/api/v1/namespaces/{namespace}/secrets/{secret['metadata']['name']}",
                   f"/api/v1/namespaces/{namespace}/secrets", secret, move)
    chosen = move.setdefault("flags", {}).get("vip")
    for service in definition.get("services", []):
        service_name = service["metadata"]["name"]
        path = f"/api/v1/namespaces/{namespace}/services/{service_name}"
        existing = _get(path)
        if existing and _ours(existing, move["id"]):
            continue
        planned = _plan_service(service, definition, namespace, name,
                                move.get("address_mode") or "shared", move.get("address", ""),
                                chosen)
        meta = _stamp(service["metadata"], move, namespace)
        if planned.get("vip"):
            meta["annotations"][VIP_ANNOTATION] = planned["vip"]
            meta["annotations"][NAMES.key("vip-mode")] = planned["vip_mode"]
            chosen = chosen or planned["vip"]
            move["flags"]["vip"] = chosen
        _post_ours(path, f"/api/v1/namespaces/{namespace}/services", service, move)
    body = definition["object"]
    _stamp(body["metadata"], move, namespace)
    _post_ours(_object_path(move["kind"], namespace, name),
               _collection_path(move["kind"], namespace), body, move)
    move["origin"] = definition.get("origin") or move.get("origin") or {}
    move["flags"]["created_at"] = time.time()
    where = f" on {chosen}" if chosen else ""
    return _advance(move, "starting", 90, f"{name} created here{where}")


def _starting(move):
    namespace, name, origin = move["namespace"], move["name"], move.get("origin") or {}
    path = _object_path(move["kind"], namespace, name)
    if move["kind"] == "vm":
        stopped = (origin.get("runStrategy") == "Halted"
                   or ("running" in origin and not origin.get("running")))
    else:
        stopped = int(origin.get("replicas", 1) or 0) == 0
    if stopped:
        return _finish(move, "succeeded", f"{name} is here, stopped as it was on {move['cluster']}")
    flags = move.setdefault("flags", {})
    if not flags.get("started"):
        ksend("PATCH", path, {"spec": dict(origin)}, ctype="application/merge-patch+json")
        flags["started"] = time.time()
    if move["kind"] == "vm":
        vmi = _get(f"/apis/kubevirt.io/v1/namespaces/{namespace}/virtualmachineinstances/{name}")
        ready = (vmi or {}).get("status", {}).get("phase") == "Running"
        waiting = "Waiting for the VM to boot"
    else:
        found = _get(path) or {}
        want = int(origin.get("replicas", 1) or 1)
        have = int((found.get("status", {}) or {}).get("readyReplicas", 0) or 0)
        ready = have >= want
        waiting = f"Waiting for {name} to become ready: {have} of {want}"
    if ready:
        return _finish(move, "succeeded", f"{name} is running here; still stopped on "
                                          f"{move['cluster']} until you remove it there")
    if time.time() - float(flags["started"]) > START_GRACE:
        # The data arrived; the application not starting is a different
        # problem, and one to be looked at rather than waited on for ever.
        raise ValueError(f"{name} was created and started here but is not ready after "
                         f"{START_GRACE // 60} minutes; check its logs. It is still stopped "
                         f"on {move['cluster']}")
    return _note(move, 95, waiting)


HANDLERS = {"joining": _joining, "quiescing": _quiescing, "backing-up": _backing_up,
            "syncing": _syncing, "restoring": _restoring, "creating": _creating,
            "starting": _starting}


def _finish(move, status, message):
    move.update(status=status, message=message, finished_at=_now())
    if status == "succeeded":
        move.update(phase="done", progress=100)


def _tick(move):
    handler = HANDLERS.get(move.get("phase"))
    if not handler:
        return _finish(move, "failed", f"unknown phase {move.get('phase')}")
    try:
        handler(move)
        move["failures"] = 0
    except CLIENT.Unreachable as error:
        move["failures"] = int(move.get("failures", 0)) + 1
        if move["failures"] > MAX_TRANSIENT:
            _finish(move, "failed", f"gave up waiting: {error}")
        else:
            move["message"] = f"Waiting: {error}"
    except (ValueError, PermissionError) as error:
        _finish(move, "failed", str(error)[:400])
    except urllib.error.HTTPError as error:
        move["failures"] = int(move.get("failures", 0)) + 1
        move["message"] = f"Waiting: Kubernetes returned HTTP {error.code}"
    except Exception as error:
        move["failures"] = int(move.get("failures", 0)) + 1
        move["message"] = f"Waiting: {str(error)[:200]}"
    move["updated_at"] = _now()


def tick_all():
    """One pass over every running move."""
    with _lock:
        running = [m["id"] for m in _read() if m.get("status") == "running"]
    for move_id in running:
        move = _find(move_id)
        if not move or move.get("status") != "running":
            continue
        _tick(move)
        with _lock:
            current = _find(move_id)
            # Someone put it back or retried it while this step ran: theirs wins.
            if current and current.get("status") == "running" and \
                    current.get("updated_at") <= move.get("updated_at"):
                _store(move)


def run():
    """The engine: every few seconds, the next step of every running move."""
    while True:
        try:
            tick_all()
        except Exception:
            pass
        time.sleep(TICK_SECONDS)


# ------------------------------------------------------------ what people do
def retry(move_id):
    move = _find(move_id)
    if not move:
        raise ValueError("no such move")
    if move["status"] != "failed":
        raise ValueError("only a failed move can be retried")
    move.update(status="running", failures=0, finished_at="",
                message=f"Retrying from {move['phase']}", updated_at=_now())
    move["op"] = _operation(move)
    _store(move)
    return _public(move)


def abandon(move_id):
    """Put the workload back where it came from, and remove what arrived here.

    Only what this move made is removed - each object carries the move's id -
    so nothing that happened to share a name is touched.
    """
    move = _find(move_id)
    if not move:
        raise ValueError("no such move")
    if move.get("source_removed"):
        raise ValueError(f"{move['name']} was already removed from {move['cluster']}; "
                         "there is nothing to put back")
    move.update(status="cancelled", message="Putting it back", updated_at=_now())
    _store(move)
    released = CLIENT.remote(move["cluster"], "/api/move/source",
                             {"action": "release", "kind": move["kind"], "name": move["name"]})
    namespace, removed = move["namespace"], []
    obj = _get(_object_path(move["kind"], namespace, move["name"]))
    if obj and _ours(obj, move_id):
        ksend("DELETE", _object_path(move["kind"], namespace, move["name"])
              + "?propagationPolicy=Background")
        removed.append(move["name"])
    for kind in ("services", "secrets"):
        try:
            items = kget(f"/api/v1/namespaces/{namespace}/{kind}").get("items", [])
        except Exception:
            items = []
        for item in items:
            if _ours(item, move_id):
                ksend("DELETE", f"/api/v1/namespaces/{namespace}/{kind}/{item['metadata']['name']}")
                removed.append(item["metadata"]["name"])
    for claim in move["claims"]:
        pvc = _get(f"/api/v1/namespaces/{namespace}/persistentvolumeclaims/{claim['claim']}")
        if pvc and _ours(pvc, move_id):
            ksend("DELETE", f"/api/v1/namespaces/{namespace}/persistentvolumeclaims/{claim['claim']}")
            removed.append(claim["claim"])
    move.update(message=(released.get("detail") or "Running on the source again")
                + (f"; removed {', '.join(removed)} here" if removed else ""),
                finished_at=_now())
    _store(move)
    return _public(move)


def finish(move_id, volumes=False):
    """Remove the stopped original from the source, once the move has landed."""
    move = _find(move_id)
    if not move:
        raise ValueError("no such move")
    if move["status"] != "succeeded":
        raise ValueError("only a finished move's source can be removed")
    if move.get("source_removed"):
        return _public(move)
    done = CLIENT.remote(move["cluster"], "/api/move/source",
                         {"action": "remove", "kind": move["kind"], "name": move["name"],
                          "volumes": bool(volumes)})
    move.update(source_removed=True, updated_at=_now(),
                message=f"{move['name']} lives here now; {done.get('detail', 'removed')} "
                        f"on {move['cluster']}")
    _store(move)
    return _public(move)
