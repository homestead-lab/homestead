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
import hashlib
import uuid
import homestead_shared as SHARED
import json
import os
import secrets
import threading
import time
import urllib.error
import urllib.parse

import homestead_icons as ICONS
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
import homestead_platform as PLATFORM
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
# Shared with any other Homestead replica on the same data volume.
_lock = SHARED.SharedLock("moves")


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
    SHARED.write_json(_path(), rows[-MAX_MOVES:], durable=True, mode=0o600, separators=(",", ":"))


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
        "source_removed", "address", "address_mode", "storage_class", "transfer_mode")} | {
        # Nothing has stopped on the source yet: undoing it is a cancel, not a
        # "put back".
        "source_stopped": _source_held(move),
        "cleanup_pending": bool((move.get("flags") or {}).get("cancelling")) and move.get("status") == "failed",
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
    if move.get("definition"):
        return json.loads(json.dumps(move["definition"]))
    return CLIENT.remote(move["cluster"], "/api/move/definition"
                         + _q(kind=move["kind"], name=move["name"], namespace=move.get("source_namespace", "")))


def _source_status(move):
    return CLIENT.remote(move["cluster"], "/api/move/source-status"
                         + _q(kind=move["kind"], name=move["name"], namespace=move.get("source_namespace", "")))


def _source_action(move, action, **extra):
    if move.get("transfer_mode") == "copy":
        extra.update(transfer_id=move["id"], expected_uid=move["source_uid"])
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


# -------------------------------------------------------------------- the plan
ACTIONS = ("move", "blank", "skip")


def _all_classes():
    items = (kget("/apis/storage.k8s.io/v1/storageclasses") or {}).get("items", [])
    return sorted(c["metadata"]["name"] for c in items
                  if not any((c.get("parameters") or {}).get(k) for k in ("fromBackup", "backingImage")))


def _choices(claims, volumes, kind):
    """Each claim's action and storage class: move (backed up and restored,
    the default), blank (an empty volume of the same shape) or skip (one of
    that name already here is used)."""
    volumes = volumes or {}
    out = {}
    for claim in claims:
        pick = volumes.get(claim["claim"]) or {}
        action = str(pick.get("action") or "move")
        if action not in ACTIONS:
            raise ValueError(f"volume {claim['claim']}: choose move, blank or skip")
        if kind == "volume" and action != "move":
            raise ValueError("a volume moved on its own is moved")
        size = int(claim.get("size_gb") or 1)
        if action == "blank" and pick.get("size_gb") not in (None, ""):
            try:
                size = int(pick["size_gb"])
            except (TypeError, ValueError):
                raise ValueError(f"volume {claim['claim']}: its size is a whole number of GB")
            if not 1 <= size <= 16384:
                raise ValueError(f"volume {claim['claim']}: a blank volume is between 1 and 16384 GB")
        out[claim["claim"]] = {"action": action, "storage_class": str(pick.get("storage_class") or "").strip(),
                               "size_gb": size}
    return out


def _moving(move):
    return [c for c in move["claims"] if c.get("action", "move") == "move"]


def storage_choices():
    items = kget("/apis/storage.k8s.io/v1/storageclasses").get("items", [])
    return sorted(c["metadata"]["name"] for c in items
                  if c.get("provisioner") == "driver.longhorn.io"
                  and not any((c.get("parameters") or {}).get(k) for k in ("fromBackup", "backingImage")))


def plan(cluster, kind, name, namespace=None, address_mode="shared", address="", storage_class="", volumes=None,
         transfer_mode="move", source_namespace=""):
    """Everything that would stop a move, or surprise someone, before it starts.

    Asks both clusters, and reports blockers and warnings separately: a blocker
    means the move would fail, a warning means it would work in a way someone
    should know about first.
    """
    namespace = namespace or NS
    blockers, warnings, fixes = [], [], []
    if kind not in KINDS:
        raise ValueError("kind must be container, vm or volume")
    if transfer_mode not in ("move", "copy") or transfer_mode == "copy" and kind == "volume":
        raise ValueError("Choose move or copy; copy supports containers and VMs")
    if address_mode not in ("shared", "automatic", "manual"):
        raise ValueError("address must be shared, automatic or manual")
    versions = CLIENT.check_cluster(cluster)
    if versions.get("compatible") is False:
        return {"ok": False, "transfer_mode": transfer_mode, "blockers": [versions["message"]], "warnings": [], "claims": [],
                "versions": versions}
    if versions.get("state") == "differs":
        warnings.append(versions["message"])
    if transfer_mode == "copy" and "copy-source-lease" not in versions.get("capabilities", []):
        return {"ok": False, "transfer_mode": "copy", "blockers": [
            "Update Homestead on the source cluster before copying workloads; this version cannot protect and release a copy's source"],
            "warnings": [], "claims": [], "versions": versions}
    try:
        definition = _definition({"cluster": cluster, "kind": kind, "name": name, "source_namespace": source_namespace})
        there = CLIENT.remote(cluster, "/api/move/target")
    except CLIENT.Unreachable as error:
        return {"ok": False, "transfer_mode": transfer_mode, "blockers": [str(error)], "warnings": [], "claims": []}
    except ValueError as error:
        # No backup storage over there is the usual first hurdle, and this
        # side can clear it: say so, so the page can offer to.
        fixes = [{"kind": "source-storage", "cluster": cluster}] if "backup target" in str(error) else []
        return {"ok": False, "transfer_mode": transfer_mode, "blockers": [str(error)], "warnings": [], "claims": [], "fixes": fixes}

    here = LH.backup_target()
    if source_namespace and definition.get("namespace") != source_namespace:
        blockers.append("Update Homestead on the source cluster; it did not return the selected source namespace")
    if transfer_mode == "copy" and (definition.get("held") or definition.get("transfer_owner")):
        blockers.append("The source is held by another transfer; finish or put back that transfer first")
    if transfer_mode == "copy" and not definition.get("source_uid"):
        blockers.append("Could not verify the source workload identity; refresh before copying")
    if kind == "vm":
        # A halted copy still needs the VM API; discovering this after backup
        # would needlessly stop the source on a destination without KubeVirt.
        if not _get("/apis/kubevirt.io/v1"):
            blockers.append("Install KubeVirt on the destination before transferring a VM")
        if transfer_mode == "copy":
            warnings.append("The VM copy gets new MAC addresses and a firmware UUID. Review guest static IP and network settings before starting it")
            vm = (definition.get("object") or {}).get("spec") or {}
            if vm.get("instancetype") or vm.get("preference"):
                blockers.append("This VM uses an external instance type or preference; expand those settings into the VM before copying")
            if (definition.get("origin") or {}).get("runStrategy") == "Once":
                blockers.append("A VM with the Once run strategy cannot safely resume after copying. Change its run strategy before copying")
            template = vm.get("template") or {}
            vm_spec = template.get("spec") or {}
            for volume in vm_spec.get("volumes") or []:
                portable = ("persistentVolumeClaim", "cloudInitNoCloud", "cloudInitConfigDrive", "containerDisk",
                            "emptyDisk", "downwardMetrics")
                if not any(key in volume for key in portable):
                    blockers.append(f"Disk {volume.get('name', '?')} has an external source that cannot be copied; use a Longhorn PVC")
            if vm_spec.get("accessCredentials"):
                blockers.append("VM access credentials use additional Secrets; copy those dependencies separately before using this transfer")
            domain = vm_spec.get("domain") or {}
            persistent_efi = (((domain.get("firmware") or {}).get("bootloader") or {}).get("efi") or {}).get("persistent")
            persistent_tpm = (((domain.get("devices") or {}).get("tpm") or {}).get("persistent"))
            if persistent_efi or persistent_tpm:
                blockers.append("This VM has persistent firmware or TPM state outside its disks; copying that state is not supported yet")
        for secret in definition.get("secrets", []):
            if _get(f"/api/v1/namespaces/{namespace}/secrets/{secret['metadata']['name']}"):
                blockers.append(f"Secret {secret['metadata']['name']} already exists in {namespace}; choose another destination namespace")
    joined = bool(_same_target(here, there))
    if not joined:
        if not there.get("reachable_off_cluster"):
            blockers.append(f"backup storage on {cluster} is only reachable inside that cluster; "
                            "give its object store a LAN address first")
            fixes.append({"kind": "source-address", "cluster": cluster})
        if here.get("configured"):
            warnings.append(f"this cluster's Longhorn backup target changes from {here.get('url')} "
                            f"to {there.get('url')}; backups already written to the old one stay there")

    if not _get(f"/api/v1/namespaces/{namespace}"):
        warnings.append(f"namespace {namespace} does not exist here and will be created")
    # A volume is its own claim, checked with the claims below.
    if kind != "volume" and _get(_object_path(kind, namespace, name)):
        blockers.append(f"{name} already exists in {namespace} on this cluster")
    choices = _choices(definition.get("claims", []), volumes, kind)
    chosen_class = storage_class or LH.STORAGE_CLASS

    def check_class(klass, claim, longhorn):
        base = _get(f"/apis/storage.k8s.io/v1/storageclasses/{urllib.parse.quote(klass, safe='')}")
        where = f"volume {claim}: " if claim else ""
        if not base:
            blockers.append(f"{where}storage class {klass} does not exist here")
        elif longhorn and base.get("provisioner") != "driver.longhorn.io":
            blockers.append(f"{where}storage class {klass} is not Longhorn; a moved volume is restored "
                            "from a Longhorn backup, so it needs a Longhorn class (or make it blank)")
        elif any((base.get("parameters") or {}).get(k) for k in ("fromBackup", "backingImage")):
            blockers.append(f"{where}choose a regular storage class, not an existing restore or image class")

    moving = [c for c in definition.get("claims", []) if choices[c["claim"]]["action"] == "move"]
    if moving:
        support = LH.restore_support()
        if not support["ready"]:
            (warnings if support.get("can_install") or support.get("waiting") else blockers).append(support["message"])
    if any(not choices[c["claim"]]["storage_class"] for c in moving) or not definition.get("claims"):
        check_class(chosen_class, "", True)
    for claim in definition.get("claims", []):
        pick = choices[claim["claim"]]
        exists = _get(f"/api/v1/namespaces/{namespace}/persistentvolumeclaims/{claim['claim']}")
        if pick["action"] == "skip":
            if not exists:
                blockers.append(f"volume {claim['claim']} is skipped, but no volume of that name is in "
                                f"{namespace} here; create it first, or make it blank")
            continue
        if exists:
            blockers.append(f"volume {claim['claim']} already exists in {namespace} here")
        if pick["storage_class"] and pick["storage_class"] != chosen_class:
            check_class(pick["storage_class"], claim["claim"], pick["action"] == "move")
        if pick["action"] == "move":
            klass = pick["storage_class"] or chosen_class
            base = _get(f"/apis/storage.k8s.io/v1/storageclasses/{urllib.parse.quote(klass, safe='')}")
            if base:
                try:
                    LH.validate_restore_class(base, claim.get("volume_mode") or "Filesystem", migration=True)
                except ValueError as error:
                    blockers.append(f"volume {claim['claim']}: {error}")
        if pick["action"] == "move" and claim.get("backing_image") and not _get(
                f"{LH_API}/namespaces/{LHNS}/backingimages/{claim['backing_image']}"):
            warnings.append(f"disk {claim['claim']} is built on the {claim['backing_image']} image, "
                            "which will be restored from its backup first")
        if pick["action"] == "blank":
            warnings.append(f"volume {claim['claim']} starts empty here; its data stays on {cluster}")

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
    will_run = (False if kind == "volume" else origin.get("replicas", 0) > 0 if kind == "container"
                else origin.get("runStrategy", "Halted") != "Halted" or origin.get("running"))
    return {
        "ok": not blockers, "blockers": blockers, "fixes": fixes,
        "warnings": list(dict.fromkeys(warnings)),
        "cluster": cluster, "kind": kind, "name": name, "namespace": namespace,
        "source_namespace": definition.get("namespace", ""),
        "joined": joined, "will_run": bool(will_run) and transfer_mode != "copy", "addresses": addresses,
        "transfer_mode": transfer_mode,
        "versions": versions,
        "storage_class": chosen_class, "storage_classes": storage_choices(),
        "claims": [{**{k: c.get(k) for k in ("claim", "size_gb", "access_mode", "volume_mode",
                                             "backing_image", "storage_class")},
                    "source_class": c.get("storage_class"), "action": choices[c["claim"]]["action"],
                    "storage_class": choices[c["claim"]]["storage_class"] or chosen_class,
                    "target_size_gb": choices[c["claim"]]["size_gb"]}
                   for c in definition.get("claims", [])],
        "all_storage_classes": _all_classes(),
        "total_gb": sum(int(c.get("size_gb") or 0) for c in moving),
    }


def start(cluster, kind, name, namespace=None, address_mode="shared", address="", storage_class="", volumes=None,
          transfer_mode="move", source_namespace=""):
    namespace = namespace or NS
    checked = plan(cluster, kind, name, namespace, address_mode, address, storage_class, volumes, transfer_mode, source_namespace)
    if not checked["ok"]:
        raise ValueError(checked["blockers"][0])
    active = [m for m in _read() if m.get("status") == "running"
              and (m["cluster"], m["kind"], m["name"]) == (cluster, kind, name)]
    if active:
        raise ValueError(f"{name} is already being moved")
    definition = _definition({"cluster": cluster, "kind": kind, "name": name, "source_namespace": source_namespace})
    choices = _choices(definition.get("claims", []), volumes, kind)
    move = {
        "id": secrets.token_hex(6), "cluster": cluster, "kind": kind, "name": name,
        "source_namespace": definition.get("namespace", ""), "namespace": namespace,
        "transfer_mode": transfer_mode, "source_uid": definition.get("source_uid", ""),
        "address_mode": address_mode, "address": address,
        "storage_class": checked["storage_class"],
        "status": "running", "phase": "joining", "progress": 1,
        "message": "Queued", "claims": [dict(c, backup="", created=False, restored=False,
                                             action=choices[c["claim"]]["action"],
                                             target_class=choices[c["claim"]]["storage_class"],
                                             target_size_gb=choices[c["claim"]]["size_gb"])
                                        for c in definition.get("claims", [])],
        "origin": definition.get("origin") or {}, "flags": {},
        "previous_target": "", "failures": 0, "source_removed": False,
        "created_at": _now(), "updated_at": _now(), "finished_at": "",
    }
    move["op"] = _operation(move)
    _store(move)
    return _public(move)


def _operation(move):
    verb = "Copy" if move.get("transfer_mode") == "copy" else "Move"
    item = OPS.start("move", f"{verb} {move['name']} from {move['cluster']}",
                     {"kind": {"vm": "VirtualMachine", "volume": "PersistentVolumeClaim"}.get(move["kind"], "Deployment"),
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
    if _moving(move):
        support = LH.ensure_restore_support()
        if not support["ready"]:
            since = move.setdefault("flags", {}).setdefault("snapshot_wait_started", time.time())
            if time.time() - since > START_GRACE:
                raise ValueError("CSI snapshot support did not become ready; repair the snapshot controller, then retry. The source has not been stopped")
            return _note(move, 1, support["message"])
    there = CLIENT.remote(move["cluster"], "/api/move/target")
    here = LH.backup_target()
    if _same_target(here, there):
        return _advance(move, "quiescing", 4, "Both clusters share backup storage")
    credentials = there.get("credentials") or {}
    # Harvester tests a backup target when it is set, and Longhorn needs it to
    # read the backups anyway: a store this cluster cannot reach is said
    # plainly here, rather than as whatever the setting's webhook replies.
    endpoint = there.get("endpoint") or credentials.get("AWS_ENDPOINTS", "")
    if endpoint and hasattr(CLIENT, "answers") and not CLIENT.answers(endpoint):
        raise ValueError(f"this cluster cannot reach {move['cluster']}'s backup storage at {endpoint}. "
                         f"Give it an address this cluster can reach - {move['cluster']}'s Migration button "
                         "under Linked clusters - then retry")
    body = {"apiVersion": "v1", "kind": "Secret", "type": "Opaque",
            "metadata": {"name": JOIN_SECRET, "namespace": LHNS,
                         "labels": {NAMES.key("managed"): "true"}},
            "stringData": {str(k): str(v) for k, v in credentials.items()}}
    if _get(f"/api/v1/namespaces/{LHNS}/secrets/{JOIN_SECRET}"):
        ksend("PUT", f"/api/v1/namespaces/{LHNS}/secrets/{JOIN_SECRET}", body)
    else:
        ksend("POST", f"/api/v1/namespaces/{LHNS}/secrets", body)
    move["previous_target"] = here.get("url", "") if here.get("configured") else ""
    # On Harvester the target is Harvester's own setting, which carries the
    # keys and endpoint itself and never reads the Secret above.
    keys = None
    if getattr(LH, "on_harvester", lambda: False)():
        keys = {"access_key": credentials.get("AWS_ACCESS_KEY_ID", ""),
                "secret_key": credentials.get("AWS_SECRET_ACCESS_KEY", ""),
                "endpoint": credentials.get("AWS_ENDPOINTS", "")}
    # Checked often while a move waits on it; Longhorn's default is minutes.
    LH.set_backup_target(there["url"], JOIN_SECRET, poll="30s", **({"keys": keys} if keys else {}))
    return _advance(move, "quiescing", 4, f"Reading backups from {move['cluster']}'s storage")


def _quiescing(move):
    flags = move.setdefault("flags", {})
    if not flags.get("quiesced"):
        if move.get("transfer_mode") == "copy":
            flags["quiesce_requested"] = True
            _store(move)  # A lost reply must still offer source recovery.
        stopped = _source_action(move, "quiesce")
        if stopped.get("origin"):
            move["origin"] = stopped["origin"]
        flags["quiesced"] = True
    status = _source_status(move)
    if status.get("running"):
        return _note(move, 6, f"Waiting for {move['name']} to stop on {move['cluster']}")
    if move["kind"] == "volume":
        return _advance(move, "backing-up", 10, f"{move['name']} is held on {move['cluster']}")
    if move.get("transfer_mode") == "copy" and not move.get("definition"):
        definition = _definition(move)
        keys = ("claim", "volume", "size_gb", "access_mode", "volume_mode")
        if [tuple(c.get(k) for k in keys) for c in definition["claims"]] != [tuple(c.get(k) for k in keys) for c in move["claims"]]:
            raise ValueError("The source disks changed; cancel this copy and review it again")
        move["definition"] = definition
    return _advance(move, "backing-up", 10, f"{move['name']} stopped on {move['cluster']}")


def _backing_up(move):
    flags = move.setdefault("flags", {})
    # A new move may follow a dismissed/cleared attempt while the source still
    # remembers its backups. Repair failed references on the first pass too.
    moving = _moving(move)
    if not moving:
        return _advance(move, "releasing-source" if move.get("transfer_mode") == "copy" else "restoring",
                        50, "No volume data to back up")
    names = [c["claim"] for c in moving]
    extra = {} if len(moving) == len(move["claims"]) else {"claims": names}
    made = _source_action(move, "backup", retry_failed=bool(flags.get("retry_backups")) or not flags.get("backed_up"),
                          **extra)
    by_claim = {row["claim"]: row["backup"] for row in made.get("backups", [])}
    for claim in moving:
        claim["backup"] = by_claim.get(claim["claim"], claim.get("backup", ""))
    flags["backed_up"] = True
    flags.pop("retry_backups", None)
    rows = [r for r in _source_status(move).get("backups", []) if r.get("claim") in names]
    for row in rows:
        state = str(row.get("state", "")).lower()
        if state in ("error", "failed") or row.get("error"):
            raise ValueError(f"backup of {row['claim']} failed on {move['cluster']}: "
                             f"{row.get('error') or state}. Retry the move to take a fresh snapshot; completed backups are kept")
        image = row.get("image") or {}
        if str(image.get("state", "")).lower() in ("error", "failed") or image.get("error"):
            raise ValueError(f"backup of the {row.get('backing_image')} image failed: "
                             f"{image.get('error') or image.get('state')}")
    if not rows:
        return _note(move, 10, "Waiting for backups to start")
    done = [r for r in rows if str(r.get("state", "")).lower() == "completed"
            and (not r.get("backing_image")
                 or str((r.get("image") or {}).get("state", "")).lower() in ("completed", "ready"))]
    average = sum(int(r.get("progress") or 0) for r in rows) / max(1, len(rows))
    if len(done) == len(moving):
        return _advance(move, "releasing-source" if move.get("transfer_mode") == "copy" else "syncing",
                        50, "Every selected volume is backed up")
    return _note(move, 10 + 40 * average / 100,
                 f"Backing up {len(rows)} volume{'' if len(rows) == 1 else 's'} on "
                 f"{move['cluster']}: {int(average)}%")


def _releasing_source(move):
    flags = move.setdefault("flags", {})
    if not flags.get("source_released"):
        _source_action(move, "release")
        flags["source_released"] = True
    return _advance(move, "syncing", 50, "Source running state restored; restoring the stopped copy here")


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
    waiting = [c["claim"] for c in _moving(move) if not here.get(c["backup"], {}).get("restorable")]
    # An image a disk is built on has to be readable here too, or the disk
    # cannot be restored however visible its own backup is.
    for image in {c["backing_image"] for c in _moving(move) if c.get("backing_image")}:
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


def _image_class():
    """A Longhorn storage class for a restored image to take its settings from.

    Homestead's own class for new volumes when it has one here; otherwise the
    cluster's default, Harvester's stock class, or any Longhorn class that is
    not itself an image's (those carry a backingImage of their own).
    """
    try:
        classes = kget("/apis/storage.k8s.io/v1/storageclasses").get("items", [])
    except Exception:
        classes = []
    longhorn = [c for c in classes if c.get("provisioner") == "driver.longhorn.io"
                and not (c.get("parameters") or {}).get("backingImage")]
    names = [c["metadata"]["name"] for c in longhorn]
    default = [c["metadata"]["name"] for c in longhorn
               if ((c["metadata"].get("annotations") or {})
                   .get("storageclass.kubernetes.io/is-default-class") == "true")]
    for choice in [LH.STORAGE_CLASS] + default + ["harvester-longhorn", "longhorn"] + names:
        if choice and choice in names:
            return choice
    raise ValueError("this cluster has no Longhorn storage class to restore the image with")


def _ensure_image(move, name, storage_class=""):
    """Restore a Harvester image from its backup before the disks built on it."""
    if _get(f"{LH_API}/namespaces/{LHNS}/backingimages/{name}"):
        return
    backup = _get(f"{LH_API}/namespaces/{LHNS}/backupbackingimages/{name}")
    url = ((backup or {}).get("status", {}) or {}).get("url", "")
    if not url:
        raise CLIENT.Unreachable(f"the {name} image backup is not visible here yet")
    # Harvester's admission webhook sets an image's copies and disks from a
    # storage class: the one named here, or else the cluster's default - and
    # refuses the image outright when there is no default.
    ksend("POST", f"{LH_API}/namespaces/{LHNS}/backingimages", {
        "apiVersion": "longhorn.io/v1beta2", "kind": "BackingImage",
        "metadata": {"name": name, "namespace": LHNS,
                     "annotations": {NAMES.key(MOVE_ID): move["id"],
                                     HARVESTER_IMAGE_CLASS: storage_class or move.get("storage_class") or _image_class()}},
        "spec": {"sourceType": "restore",
                 "sourceParameters": {"backup-url": url, "concurrent-limit": "2"}}})


def _restore_progress(namespace, claim):
    """(finished, percent) for one claim being restored from a backup."""
    pvc = _get(f"/api/v1/namespaces/{namespace}/persistentvolumeclaims/{claim}")
    if pvc:
        problem = LH.restore_problem({**pvc, "metadata": {**pvc.get("metadata", {}), "namespace": namespace}})
        if problem:
            raise ValueError(f"Restoring {claim} failed: {problem}")
    if not pvc or (pvc.get("status", {}) or {}).get("phase") != "Bound":
        return False, 0
    volume_name = (pvc.get("spec", {}) or {}).get("volumeName", "")
    volume = _get(f"{LH_API}/namespaces/{LHNS}/volumes/{volume_name}") if volume_name else None
    if not volume:
        return False, 0
    status = volume.get("status", {}) or {}
    if status.get("restoreInitiated") and not status.get("restoreRequired") and status.get("state") in ("detached", "attached"):
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
        if claim.get("action") == "skip":
            if not existing:
                raise ValueError(f"volume {claim['claim']} was to be skipped, using one already in {namespace} "
                                 "here, but there is none; create it, then retry")
            claim.update(created=True, restored=True)
            continue
        if claim.get("action") == "blank" and not existing:
            _blank_claim(move, claim, namespace)
            claim.update(created=True, restored=True)
            continue
        if existing:
            if not _ours(existing, move["id"]):
                raise ValueError(f"volume {claim['claim']} already exists in {namespace} "
                                 "and was not made by this move")
            claim["created"] = True
            continue
        if claim.get("backing_image") and not _image_ready(claim["backing_image"]):
            _ensure_image(move, claim["backing_image"], claim.get("target_class") or move.get("storage_class") or "")
            return _note(move, 56, f"Restoring the {claim['backing_image']} image first")
        result = LH.restore_backup({
            "backup": claim["backup"], "namespace": namespace, "name": claim["claim"],
            "size_gb": claim.get("size_gb"), "access_mode": claim.get("access_mode"),
            "storage_class": claim.get("target_class") or move.get("storage_class") or "",
            "volume_mode": claim.get("volume_mode"),
            "migratable": claim.get("migratable"), "backing_image": claim.get("backing_image"),
            "annotations": {NAMES.key(MOVE_ID): move["id"],
                            NAMES.key(MOVED_FROM): f"{move['cluster']}/{move['name']}"}})
        if result and result.get("created") is False:
            return _note(move, 56, result["message"])
        claim["created"] = True
    states = []
    for claim in move["claims"]:
        if claim.get("action", "move") != "move":
            claim["restored"] = True
            continue
        finished, percent = _restore_progress(namespace, claim["claim"])
        claim["restored"] = finished
        states.append(100 if finished else percent)
    if all(c["restored"] for c in move["claims"]):
        if move["kind"] == "volume":
            return _finish(move, "succeeded", f"{move['name']} is here; the original stays on "
                                              f"{move['cluster']} until you remove it there")
        return _advance(move, "creating", 86, "Every volume is restored here")
    average = sum(states) / max(1, len(states))
    return _note(move, 56 + 29 * average / 100,
                 f"Restoring {len(states)} volume{'' if len(states) == 1 else 's'} here: {int(average)}%")


def _blank_claim(move, claim, namespace):
    """An empty volume of the claim's shape and chosen size, on its chosen class (or the
    cluster's default), stamped as this move's so putting it back removes it.
    Not waited on: a class that binds on first use binds when the app starts."""
    body = {"apiVersion": "v1", "kind": "PersistentVolumeClaim",
            "metadata": {"name": claim["claim"], "namespace": namespace,
                         "labels": {NAMES.key("managed"): "true"},
                         "annotations": {NAMES.key(MOVE_ID): move["id"],
                                         NAMES.key(MOVED_FROM): f"{move['cluster']}/{move['name']}"}},
            "spec": {"accessModes": [claim.get("access_mode") or "ReadWriteOnce"],
                     "volumeMode": claim.get("volume_mode") or "Filesystem",
                     "resources": {"requests": {"storage": f"{int(claim.get('target_size_gb') or claim.get('size_gb') or 1)}Gi"}}}}
    klass = claim.get("target_class") or move.get("storage_class") or ""
    if klass:
        body["spec"]["storageClassName"] = klass
    ksend("POST", f"/api/v1/namespaces/{namespace}/persistentvolumeclaims", body)


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


def carry_icon(cluster, annotations):
    """Make a workload's logo work here, where its cache reference is unknown.

    The logo annotation names the source Homestead's icon cache, which a move
    does not bring. The bytes are fetched from the source (they keep their
    name, being named by their digest), or failing that from the original
    URL. A logo is never a reason for a move to fail: without one the card
    shows the usual placeholder.
    """
    reference = NAMES.read(annotations, "icon") or ""
    if not reference.startswith("/api/icons/") or ICONS.exists(reference, DATA_DIR):
        return reference
    try:
        found = ICONS.store(CLIENT.icon_bytes(cluster, reference), DATA_DIR)
    except Exception:
        source = NAMES.read(annotations, "icon-source") or ""
        try:
            found = ICONS.persist(source, DATA_DIR) if source.startswith(("http://", "https://")) else ""
        except Exception:
            found = ""
    if found:
        annotations[NAMES.key("icon")] = found
    return found


def _creating(move):
    namespace, name = move["namespace"], move["name"]
    definition = _definition(move)
    for secret in definition.get("secrets", []):
        _stamp(secret["metadata"], move, namespace)
        _post_ours(f"/api/v1/namespaces/{namespace}/secrets/{secret['metadata']['name']}",
                   f"/api/v1/namespaces/{namespace}/secrets", secret, move)
    chosen = move.setdefault("flags", {}).get("vip")
    pod_spec = ((((definition.get("object") or {}).get("spec") or {}).get("template") or {}).get("spec") or {})
    for service in definition.get("services", []):
        service_name = service["metadata"]["name"]
        path = f"/api/v1/namespaces/{namespace}/services/{service_name}"
        existing = _get(path)
        if existing and _ours(existing, move["id"]):
            continue
        # On the host network an app answers on the node itself. Here k3s's
        # ServiceLB would publish its Service by holding the same ports on
        # every node - and the app could then never be placed. It needs none.
        if (pod_spec.get("hostNetwork") and (service.get("spec") or {}).get("type") == "LoadBalancer"
                and NETWORK.servicelb_present()):
            move.setdefault("flags", {})["skipped_services"] = sorted(
                set(move["flags"].get("skipped_services") or []) | {service_name})
            continue
        planned = _plan_service(service, definition, namespace, name,
                                move.get("address_mode") or "shared", move.get("address", ""),
                                chosen)
        meta = _stamp(service["metadata"], move, namespace)
        # The class the source cluster's load balancer wanted means nothing
        # here: this cluster's own is set below when the Service gets a VIP.
        (service.get("spec") or {}).pop("loadBalancerClass", None)
        if planned.get("vip"):
            service.setdefault("spec", {}).update(PLATFORM.vip_spec(planned["vip"]))
            meta["annotations"].update(PLATFORM.vip_annotations(planned["vip"]))
            meta["annotations"][NAMES.key("vip-mode")] = planned["vip_mode"]
            chosen = chosen or planned["vip"]
            move["flags"]["vip"] = chosen
        _post_ours(path, f"/api/v1/namespaces/{namespace}/services", service, move)
    body = definition["object"]
    if move.get("transfer_mode") == "copy" and move["kind"] == "vm":
        domain = body["spec"]["template"]["spec"].setdefault("domain", {})
        domain.setdefault("firmware", {})["uuid"] = str(uuid.uuid5(uuid.NAMESPACE_URL, move["id"] + ":firmware"))
        for index, interface in enumerate(domain.get("devices", {}).get("interfaces", [])):
            raw = hashlib.sha256(f"{move['id']}:interface:{index}".encode()).digest()
            interface["macAddress"] = "02:" + ":".join(f"{byte:02x}" for byte in raw[:5])
    _stamp(body["metadata"], move, namespace)
    carry_icon(move["cluster"], body["metadata"]["annotations"])
    _post_ours(_object_path(move["kind"], namespace, name),
               _collection_path(move["kind"], namespace), body, move)
    move["origin"] = definition.get("origin") or move.get("origin") or {}
    move["flags"]["created_at"] = time.time()
    where = f" on {chosen}" if chosen else ""
    return _advance(move, "starting", 90, f"{name} created here{where}")


def _starting(move):
    namespace, name, origin = move["namespace"], move["name"], move.get("origin") or {}
    path = _object_path(move["kind"], namespace, name)
    if move.get("transfer_mode") == "copy":
        return _finish(move, "succeeded", f"{name} copied here, stopped. The original on {move['cluster']} keeps its previous running state")
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
            "releasing-source": _releasing_source,
            "syncing": _syncing, "restoring": _restoring, "creating": _creating,
            "starting": _starting}


# Called after a move finishes to tidy safe restore metadata.
after_finish = None


def _finish(move, status, message):
    move.update(status=status, message=message, finished_at=_now())
    if status == "succeeded":
        move.update(phase="done", progress=100)
        move.pop("definition", None)
    if after_finish:
        try:
            after_finish()
        except Exception:
            pass


def _kubernetes_reason(error):
    """What the API server (or a webhook behind it) said, from its Status body."""
    try:
        body = json.loads(error.read().decode("utf-8", "replace") or "{}")
        return str(body.get("message") or "").strip()
    except Exception:
        return ""


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
        reason = _kubernetes_reason(error)
        # Kubernetes saying no - an invalid object, a webhook refusing it - does
        # not change by asking again, so it stops the move with what was said.
        # A conflict, a rate limit or the API server struggling may clear.
        if error.code in (400, 403, 404, 422):
            return _finish(move, "failed", f"Kubernetes refused it (HTTP {error.code}): {reason}"[:400]) \
                if reason else _finish(move, "failed", f"Kubernetes refused it (HTTP {error.code})")
        move["failures"] = int(move.get("failures", 0)) + 1
        move["message"] = f"Waiting: Kubernetes returned HTTP {error.code}{f': {reason}' if reason else ''}"[:400]
    except Exception as error:
        move["failures"] = int(move.get("failures", 0)) + 1
        move["message"] = f"Waiting: {str(error)[:200]}"
    move["updated_at"] = _now()


def tick_all():
    """One pass over every running move."""
    with SHARED.write_scope(_path()):
        _tick_all()


def _tick_all():
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
    if move.get("transfer_mode") == "copy" and move.get("flags", {}).get("cancelling"):
        return abandon(move_id)
    if move.get("phase") == "backing-up":
        move.setdefault("flags", {})["retry_backups"] = True
    if move.get("phase") == "joining":
        move.setdefault("flags", {}).pop("snapshot_wait_started", None)
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
    stopped = _source_held(move)
    move.update(status="cancelled", message="Putting it back" if stopped else "Cancelling", updated_at=_now())
    if move.get("transfer_mode") == "copy":
        move.setdefault("flags", {})["cancelling"] = True
    _store(move)
    # Nothing stopped there yet means nothing to start again there - and no
    # need for the source to answer before this move can be cancelled.
    try:
        if stopped and move.get("transfer_mode") == "copy":
            status = _source_status(move)
            stopped = status.get("transfer_owner") == move["id"] and status.get("source_uid") == move.get("source_uid")
        released = _source_action(move, "release") if stopped else {"detail": f"Source on {move['cluster']} kept as it was"}
    except Exception:
        move.update(status="failed", message="Source recovery failed; retry cancellation to restore its running state")
        _store(move)
        raise
    if stopped or move.get("transfer_mode") == "copy":
        move.setdefault("flags", {})["source_released"] = True
        _store(move)
    try:
        removed = _remove_created(move)
    except Exception:
        move.update(status="failed", message="Transfer cleanup failed; retry cleanup to remove its remaining destination objects")
        _store(move)
        raise
    move.update(message=(released.get("detail") or "Running on the source again")
                + (f"; removed {', '.join(removed)} here" if removed else ""),
                finished_at=_now())
    move.pop("definition", None)
    _store(move)
    return _public(move)


def _remove_created(move):
    namespace, removed, move_id = move["namespace"], [], move["id"]
    # A volume is its own claim, removed with the claims below.
    obj = None if move["kind"] == "volume" else _get(_object_path(move["kind"], namespace, move["name"]))
    if obj and _ours(obj, move_id):
        ksend("DELETE", _object_path(move["kind"], namespace, move["name"])
              + "?propagationPolicy=Background")
        removed.append(move["name"])
    for kind in ("services", "secrets"):
        try:
            items = kget(f"/api/v1/namespaces/{namespace}/{kind}").get("items", [])
        except urllib.error.HTTPError as error:
            if error.code != 404:
                raise
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
    return removed


DISMISSABLE = ("succeeded", "cancelled")


def dismiss(move_id=None):
    """Clear moves from the list - one, or every finished one - and nothing
    else: neither cluster is touched. The source keeps its stopped original,
    to be removed there when it suits. A failed move is cleared only when
    asked for by itself: its workload stays stopped on the source, and what it
    made here stays, both for someone to deal with - which is why clearing
    every finished move leaves failed ones listed."""
    with _lock:
        rows = _read()
        allowed = DISMISSABLE + (("failed",) if move_id is not None else ())
        gone = [m for m in rows if m.get("status") in allowed and (move_id is None or m["id"] == move_id)]
        if move_id is not None and not gone:
            match = next((m for m in rows if m["id"] == move_id), None)
            if not match:
                raise ValueError("no such move")
            raise ValueError("a move still running cannot be cleared; cancel it, or wait for it to finish or fail")
        if any(m.get("transfer_mode") == "copy" and _source_held(m) for m in gone):
            raise ValueError("Cancel the failed copy to restore its source before dismissing it")
        _write([m for m in rows if m not in gone])
    failed = [m for m in gone if m.get("status") == "failed"]
    if failed:
        m = failed[0]
        stopped = _source_held(m) and not m.get("source_removed")
        made = [c["claim"] for c in m.get("claims") or [] if c.get("created")]
        created = bool((m.get("flags") or {}).get("created_at"))
        left = ([f"{m['name']} stays stopped on {m['cluster']}; start it there from its page"] if stopped else []) +                ([f"what it made here stays ({', '.join(([m['name']] if created else []) + made)})"] if made or created else [])
        return {"ok": True, "dismissed": 1,
                "detail": f"cleared the failed move of {m['name']}" + (f"; {'; '.join(left)}" if left else "")}
    kept = [m for m in gone if m.get("status") == "succeeded" and not m.get("source_removed")
            and m.get("transfer_mode") != "copy"]
    return {"ok": True, "dismissed": len(gone),
            "detail": f"cleared {len(gone)} move{'s' if len(gone) != 1 else ''}"
                      + (f"; {', '.join(sorted({m['cluster'] for m in kept}))} still "
                         f"{'has' if len({m['cluster'] for m in kept}) == 1 else 'have'} the stopped original"
                         f"{'s' if len(kept) != 1 else ''} of {', '.join(m['name'] for m in kept)}" if kept else "")}


def finish(move_id, volumes=False):
    """Remove the stopped original from the source, once the move has landed."""
    move = _find(move_id)
    if not move:
        raise ValueError("no such move")
    if move.get("transfer_mode") == "copy":
        raise ValueError("A copy keeps its source. Remove the original separately from its own cluster if intended")
    if move["status"] != "succeeded":
        raise ValueError("only a finished move's source can be removed")
    if move.get("source_removed"):
        return _public(move)
    body = {"action": "remove", "kind": move["kind"], "name": move["name"], "volumes": bool(volumes)}
    moved = [c["claim"] for c in _moving(move)]
    if volumes and len(moved) != len(move["claims"]):
        # Skipped and blank volumes are the only copy of their data there. A
        # source older than 2.8.250 ignores the list and would delete them.
        there = str((CLIENT.check_cluster(move["cluster"]) or {}).get("version") or "")
        try:
            new_enough = tuple(int(x) for x in there.lstrip("v").split(".")[:3]) >= (2, 8, 250)
        except ValueError:
            new_enough = False
        if not new_enough:
            raise ValueError(f"{move['cluster']} runs Homestead {there or 'of an unknown release'}, which would delete every "
                             f"volume of {move['name']}, not only the moved ones; update it to 2.8.250 or later, "
                             "or remove the original without its volumes")
        body["claims"] = moved
    done = CLIENT.remote(move["cluster"], "/api/move/source", body)
    move.update(source_removed=True, updated_at=_now(),
                message=f"{move['name']} lives here now; {done.get('detail', 'removed')} "
                        f"on {move['cluster']}")
    _store(move)
    return _public(move)
