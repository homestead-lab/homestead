"""
Import sources and discovery, the state the server binds for importing, and
the routes of the import pipeline, the image cache, schedules and VM creation.

Import sources are remote hosts (Unraid, Proxmox, plain SSH) registered in a
ConfigMap, with credentials in a Secret. Importing a container means:
  1. create a PVC for its appdata,
  2. run a Job that rsyncs the remote path into that PVC,
  3. create the Deployment pointing at it.
Step 2 is the part that takes real time, so it runs as a Job we can poll.

Split by feature (#623): importing a container lives in
homestead_container_import, the image cache in homestead_image_cache,
schedules in homestead_scheduled_jobs and VM disks, images and creation in
homestead_vm_create. Every name they define is re-exported here, so other
modules, server.py and tests that use or replace a name here keep working.
"""
import base64
import copy
import json
import hashlib
import os
import re
import shlex
import secrets
import time
import homestead_copy_checks as COPY_CHECKS
import homestead_self as SELF
import urllib.error
import urllib.parse

import homestead_names as NAMES
import homestead_shared as SHARED
import homestead_hvimage as HVIMAGE
import homestead_runtime as RUNTIME
import homestead_pod_resources as RESOURCES
import homestead_import_job as IMPORT_JOB
import homestead_operations as OPS
import homestead_source_ssh as SOURCE_SSH
import homestead_capacity_review as SOURCE_REVIEW
import homestead_passthrough as PASSTHROUGH
import homestead_vm_network as VMNETWORK
import homestead_routes

kget = ksend = create_pvc = build_deployment = None
NS = "lab"
# An ISO volume a new VM can install from (homestead_isos.ready_volume), set
# by the server; None where ISOs are not offered.
iso_ready = None
# Where the image cache keeps each node's full image list
# (homestead_image_cache), set by the server.
SCAN_DIR = ""        # set by the server; empty keeps scans in memory only
_cache = {}
hardware_features = lambda: []


def bind(_kget, _ksend, _create_pvc, _build_dep, _ns, _cache_ref, _hardware_features=None):
    global kget, ksend, create_pvc, build_deployment, NS, _cache, hardware_features
    kget, ksend, create_pvc, build_deployment = _kget, _ksend, _create_pvc, _build_dep
    NS, _cache = _ns, _cache_ref
    # Name lookups need the same client, and binding here means a caller never
    # has to remember to do it separately.
    NAMES.bind(_kget)
    if _hardware_features:
        hardware_features = _hardware_features


def _bust(*keys):
    for k in list(_cache):
        if not keys or any(k.startswith(x) for x in keys):
            _cache.pop(k, None)


SAFE = re.compile(r"^[a-z0-9]([a-z0-9-]{0,38}[a-z0-9])?$")


# --------------------------------------------------------------- sources
def _sources_map():
    return NAMES.object_name("sources", NS)


SOURCE_LOCK = SHARED.SharedLock("import-sources", strict=True, directory=lambda: OPS.DATA_DIR)


def _sources_state():
    try:
        cm = kget(f"/api/v1/namespaces/{NS}/configmaps/{_sources_map()}")
    except urllib.error.HTTPError as error:
        if error.code == 404:
            return None, []
        raise
    rows = json.loads(cm.get("data", {}).get("sources.json", "[]"))
    if not isinstance(rows, list) or any(not isinstance(row, dict) or not SAFE.fullmatch(str(row.get("name", ""))) for row in rows):
        raise ValueError("Source inventory is invalid; refusing to replace it")
    if len({row["name"] for row in rows}) != len(rows):
        raise ValueError("Source inventory has duplicate names")
    return cm, rows


def list_sources():
    return _sources_state()[1]


def save_sources(srcs, current):
    name = _sources_map()
    body = {"apiVersion": "v1", "kind": "ConfigMap",
            "metadata": {"name": name, "namespace": NS},
            "data": {"sources.json": json.dumps(srcs, indent=2)}}
    if current is not None:
        meta = current.get("metadata", {})
        if not meta.get("uid") or not meta.get("resourceVersion"):
            raise ValueError("Source inventory identity is unavailable")
        body["metadata"].update(uid=meta["uid"], resourceVersion=meta["resourceVersion"])
        return ksend("PUT", f"/api/v1/namespaces/{NS}/configmaps/{name}", body)
    return ksend("POST", f"/api/v1/namespaces/{NS}/configmaps", body)


def add_source(name, host, user, password, kind="unraid", base_path="/mnt/user/appdata", port=22):
    if not SAFE.fullmatch(name):
        raise ValueError("name must be lowercase letters, numbers and dashes")
    endpoint = SOURCE_SSH.connection({"host": host, "user": user, "port": port})
    if kind not in ("unraid", "proxmox", "ssh") or not str(base_path).startswith("/") or any(c in str(base_path) for c in "\r\n\0"):
        raise ValueError("Choose a source type and an absolute appdata path")
    with SOURCE_LOCK:
        cm, srcs = _sources_state()
        if any(row["name"] == name for row in srcs):
            raise ValueError("That source already exists; verify it, or use a new name for a different connection")
        # Immutable credentials keep a partial save or later reuse of a source
        # name from changing credentials underneath an existing copy Job.
        secret = f"homestead-src-{name}-{secrets.token_hex(4)}"
        sec = {"apiVersion": "v1", "kind": "Secret", "type": "Opaque", "immutable": True,
               "metadata": {"name": secret, "namespace": NS, "labels": {"homestead.io/import-source": name}},
               "stringData": {"password": password or ""}}
        ksend("POST", f"/api/v1/namespaces/{NS}/secrets", sec)
        srcs.append({"name": name, **endpoint, "kind": kind, "base_path": base_path,
                     "credential_secret": secret, "added": time.strftime("%Y-%m-%d %H:%M")})
        save_sources(srcs, cm)  # failure retains the unused Secret, never adopts it
        return srcs


def del_source(name):
    with SOURCE_LOCK:
        cm, srcs = _sources_state()
        if not any(s["name"] == name for s in srcs):
            raise ValueError("Source no longer exists")
        srcs = [s for s in srcs if s["name"] != name]
        save_sources(srcs, cm)
        # Existing helpers retain their immutable credential reference. Removing
        # the directory entry is not credential revocation or Job cancellation.
        return srcs


def source_secret(name, src=None):
    """This source's password secret."""
    return (src or {}).get("credential_secret") or f"homestead-src-{name}"


def _source(name):
    s = next((x for x in list_sources() if x["name"] == name), None)
    if not s:
        raise ValueError(f"no import source named {name}")
    return s


def _trust_context(cm, src, actor):
    meta = (cm or {}).get("metadata", {})
    if not meta.get("uid") or not meta.get("resourceVersion"):
        raise ValueError("Source inventory identity is unavailable")
    return {"action": "source-trust", "actor": actor, "namespace": NS,
            "uid": meta["uid"], "version": meta["resourceVersion"], "source": src}


def scan_source(name, actor):
    cm, srcs = _sources_state()
    src = next((s for s in srcs if s["name"] == name), None)
    if src is None:
        raise ValueError("Source no longer exists")
    context = _trust_context(cm, src, actor)
    found = SOURCE_SSH.candidate(run_probe("key-" + name, SOURCE_SSH.scan_script(src), src, authenticated=False))
    config = {"name": name, "key": found["key"]}
    previous = (src.get("ssh_trust") or {}).get("fingerprint", "")
    return {**config, **found, "connection": SOURCE_SSH.connection(src), "previous": previous,
            "changed": bool(previous and previous != found["fingerprint"]),
            "capacity_token": SOURCE_REVIEW.issue(config, context)}


def trust_source(body, actor):
    with SOURCE_LOCK:
        cm, srcs = _sources_state()
        src = next((s for s in srcs if s["name"] == body.get("name")), None)
        if src is None:
            raise ValueError("Source no longer exists")
        context = _trust_context(cm, src, actor)
        found = SOURCE_SSH.key(body.get("key"))
        config = {"name": src["name"], "key": found["key"], "capacity_token": body.get("capacity_token")}
        if body.get("confirm_fingerprint") is not True or not SOURCE_REVIEW.valid(config, context):
            raise ValueError("Scan again and confirm the fingerprint from the source host's console")
        src["ssh_trust"] = {**found, "connection": SOURCE_SSH.connection(src), "by": actor,
                            "verified_at": time.strftime("%Y-%m-%d %H:%M")}
        save_sources(srcs, cm)
        return {"ok": True, "sources": srcs}


# --------------------------------------------------------------- discovery
def browse_source(name, path=None):
    """List candidate appdata directories on a source host, via a short-lived Job.

    We cannot SSH from the API server, so we run a pod that does it and read the
    result from its logs. Slower than a direct call but keeps credentials inside
    the cluster.
    """
    s = _source(name)
    p = path or s.get("base_path", "/mnt/user/appdata")
    remote_cmd = f"entries=$(ls -1 {shlex.quote(p)}) || exit $?; printf '%s\\n' \"$entries\" | head -200"
    script = _ssh_script(s, remote_cmd)
    return run_probe(f"browse-{name}", script, s)


def _ssh_script(src, remote_cmd):
    return SOURCE_SSH.command(src, remote_cmd)


def source_containers(name):
    """List Docker containers on an Unraid/generic Docker source.

    Docker's JSON line format gives us the real image reference instead of
    asking the user to remember or retype it during import.
    """
    src = _source(name)
    lines = run_probe(f"containers-{name}", _ssh_script(
        src, "entries=$(docker ps -a --format '{{json .}}') || exit $?; printf '%s\\n' \"$entries\" | head -200"), src)
    out = []
    for line in lines:
        try:
            row = json.loads(line)
        except Exception:
            continue
        nm = row.get("Names") or row.get("Name") or ""
        if nm:
            out.append({"name": nm, "image": row.get("Image", ""),
                        "state": row.get("State", ""), "status": row.get("Status", "")})
    return sorted(out, key=lambda x: x["name"].lower())


DEFAULT_SHM_MB = 64


def _shm_mb(size):
    """The /dev/shm a container was given, in MiB, if it was more than default."""
    try:
        megabytes = int(size or 0) // 1024 ** 2
    except (TypeError, ValueError):
        return 0
    return megabytes if megabytes > DEFAULT_SHM_MB else 0


def _tmpfs_mb(options):
    """The size a tmpfs mount was given, in MiB, or 0 when it was not."""
    match = re.search(r"size=(\d+)([kmg]?)", str(options or "").lower())
    if not match:
        return 0
    amount, unit = int(match.group(1)), match.group(2)
    scale = {"": 1 / 1024 ** 2, "k": 1 / 1024, "m": 1, "g": 1024}[unit]
    return max(1, int(amount * scale))


def inspect_source_container(name, container):
    """Translate Docker inspect fields into Homestead's deploy/import model."""
    if not re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", container or ""):
        raise ValueError("invalid container name")
    src = _source(name)
    lines = run_probe(f"inspect-{name}", _ssh_script(
        src, f"docker inspect {shlex.quote(container)} 2>/dev/null"), src)
    try:
        # Kubernetes combines a probe container's stdout and stderr.  SSH can
        # therefore put a host-key notice ahead of Docker's JSON on older
        # sources.  Decode the first JSON value instead of assuming byte zero.
        text = "\n".join(lines)
        starts = [i for i in (text.find("["), text.find("{")) if i >= 0]
        if not starts:
            raise ValueError("Docker returned no JSON")
        raw, _ = json.JSONDecoder().raw_decode(text[min(starts):])
        item = raw[0] if isinstance(raw, list) else raw
    except Exception as e:
        raise ValueError(f"could not inspect {container}: {e}")
    config, host = item.get("Config", {}) or {}, item.get("HostConfig", {}) or {}
    import homestead_privileges as PRIV
    privileges = PRIV.from_docker("", host.get("Privileged"), host.get("CapAdd") or (),
                                  [d.get("PathOnHost", "") for d in host.get("Devices") or []])
    env = {}
    for pair in config.get("Env", []) or []:
        key, sep, val = pair.partition("=")
        if sep and key not in ("PATH", "HOSTNAME", "HOME", "TERM"):
            env[key] = val
    ports = []
    bindings = host.get("PortBindings", {}) or {}
    exposed = config.get("ExposedPorts", {}) or {}
    for spec in sorted(set(bindings) | set(exposed)):
        num, _, proto = spec.partition("/")
        if not num.isdigit():
            continue
        vals = bindings.get(spec) or [{}]
        hp = next((x.get("HostPort") for x in vals if x.get("HostPort")), num)
        ports.append({"container": int(num), "host": int(hp),
                      "protocol": (proto or "tcp").upper(), "expose": True})
    mounts = [{"source": m.get("Source", ""), "path": m.get("Destination", ""),
               "type": m.get("Type", ""),
               "size_mb": _tmpfs_mb(m.get("Mode", "")) if m.get("Type") == "tmpfs" else 0}
              for m in item.get("Mounts", []) or [] if m.get("Destination")]
    # --tmpfs never appears in Mounts, only in HostConfig, and it is how
    # /tmp/cache is usually given to Frigate on Unraid.
    for destination, options in (host.get("Tmpfs", {}) or {}).items():
        if destination and not any(row["path"] == destination for row in mounts):
            mounts.append({"source": "", "path": destination, "type": "tmpfs",
                           "size_mb": _tmpfs_mb(options)})
    app_mount = _config_mount(mounts, src.get("base_path", "/mnt/user/appdata"))
    labels = config.get("Labels", {}) or {}
    devices = host.get("Devices", []) or []
    paths = " ".join([d.get("PathOnHost", "") for d in devices] + (host.get("Binds", []) or [])).lower()
    hardware = []
    for feature in hardware_features():
        host_path = feature.get("host_path", "").lower().rstrip("/")
        if host_path and (host_path in paths or any(
                str(d.get("PathOnHost", "")).lower().startswith(host_path + "/") for d in devices)):
            hardware.append(feature["id"])
    return {
        **privileges,
        "name": (item.get("Name") or container).lstrip("/"),
        "source_container_id": item.get("Id", ""),
        "source_running": bool((item.get("State") or {}).get("Running")),
        "image": config.get("Image", ""),
        "icon": (labels.get("net.unraid.docker.icon", "")
                 or labels.get("homestead.icon", "")),
        "webui": labels.get("net.unraid.docker.webui", ""),
        "env": env, "ports": ports, "mounts": mounts,
        "remote_path": app_mount["source"] if app_mount else "",
        "mount_path": app_mount["path"] if app_mount else "/config",
        # Nothing on the host said where this container keeps its config, so the
        # import must not pretend: a guessed path is one rsync cannot find.
        "guessed_path": not app_mount,
        "network_mode": host.get("NetworkMode", "bridge"), "hardware": hardware,
        "memory": str(host.get("MemoryReservation")) if int(host.get("MemoryReservation") or 0) > 0 else
                  str(min(256 * 1024 ** 2, int(host.get("Memory") or 256 * 1024 ** 2))),
        "memory_limit": str(host.get("Memory")) if int(host.get("Memory") or 0) > 0 else "",
        # Docker's --shm-size has no Kubernetes equivalent: a pod gets 64 MiB of
        # /dev/shm whatever it asks for. Frigate keeps every camera's frames
        # there, so a container given more on the source has to be given it
        # again here or ffmpeg hands over frames that never fit.
        "shm_mb": _shm_mb(host.get("ShmSize")),
    }


CONFIG_MOUNTS = ("/config", "/data", "/etc/config", "/var/lib")


def _config_mount(mounts, base_path):
    """The bind mount holding this container's configuration, or None.

    Usually it lives under the source's appdata directory. Plenty of Unraid
    templates keep it elsewhere - Frigate often sits beside the recordings on
    a camera share - so a container path that conventionally means config is
    the second choice. Guessing a path that is not there is not a choice at
    all: rsync only discovers that halfway through a copy.
    """
    binds = [m for m in mounts if m.get("source", "").startswith("/") and m.get("path")]
    under_base = [m for m in binds if m["source"].startswith(base_path.rstrip("/") + "/")
                  or m["source"] == base_path.rstrip("/")]
    if under_base:
        return under_base[0]
    return next((m for m in binds if m["path"].rstrip("/") in CONFIG_MOUNTS), None)


def run_probe(tag, script, src, timeout=70, *, authenticated=True):
    """Run a one-shot pod, wait for it, return its stdout."""
    setup = SOURCE_SSH.setup(src) if authenticated else ""
    pod = f"homestead-probe-{re.sub(r'[^a-z0-9-]', '-', tag)[:30]}-{secrets.token_hex(5)}"
    body = {
        "apiVersion": "v1", "kind": "Pod",
        "metadata": {"name": pod, "namespace": NS, "labels": NAMES.labels("probe")},
        "spec": {"restartPolicy": "Never", "terminationGracePeriodSeconds": 1,
                 "automountServiceAccountToken": False, "activeDeadlineSeconds": timeout,
                 "containers": [{
                     "name": "probe", "image": "alpine:3.24",
                     "resources": {"requests": {"cpu": "50m", "memory": "64Mi"}, "limits": {"memory": "256Mi"}},
                     "command": ["sh", "-c",
                                 "set -e\napk add --no-cache openssh-client sshpass >/dev/null 2>&1\n" + setup + script],
                     "env": [{"name": "SSHPASS", "valueFrom": {"secretKeyRef": {
                         "name": source_secret(src["name"], src), "key": "password"}}}] if authenticated else [],
                 }]},
    }
    receipt = ksend("POST", f"/api/v1/namespaces/{NS}/pods", body)
    uid = receipt.get("metadata", {}).get("uid")
    if not uid or receipt.get("metadata", {}).get("name") != pod:
        raise ValueError("Probe creation was not confirmed; no retry or name-based cleanup was attempted")
    out, deadline = "", time.time() + timeout
    try:
        while time.time() < deadline:
            time.sleep(2)
            current = kget(f"/api/v1/namespaces/{NS}/pods/{pod}")
            if current.get("metadata", {}).get("uid") != uid:
                raise ValueError("Probe identity changed; its result is not trusted")
            st = current.get("status", {})
            if st.get("phase") in ("Succeeded", "Failed"):
                if st["phase"] != "Succeeded":
                    raise ValueError("SSH probe failed. Check the source key, login and SSH service; changed keys require a new fingerprint review.")
                break
        else:
            raise ValueError("SSH probe timed out; no result was accepted")
        import urllib.request
        out = _pod_logs(pod)
        if kget(f"/api/v1/namespaces/{NS}/pods/{pod}").get("metadata", {}).get("uid") != uid:
            raise ValueError("Probe identity changed while reading logs; its result is not trusted")
    finally:
        try:
            ksend("DELETE", f"/api/v1/namespaces/{NS}/pods/{pod}",
                  {"apiVersion": "v1", "kind": "DeleteOptions", "preconditions": {"uid": uid}})
        except Exception:
            pass
    return [l.strip() for l in out.splitlines() if l.strip()]


def _pod_logs(pod, ns=None):
    import urllib.request
    from homestead_shim import raw_get  # provided by server.py
    return raw_get(f"/api/v1/namespaces/{ns or NS}/pods/{pod}/log?tailLines=400")


# Moved by feature (#623). Re-exported, so other modules, server.py and tests
# that use or replace these names here keep working.
from homestead_vm_create import (  # noqa: E402,F401
    VM_DISK_LABEL, _required_name, _get_or_none, vm_disk_import_plan, _http_disk_source,
    import_vm_disk, _disk_consumers, list_vm_disks, _epoch, list_vm_images, LH_API, _items_or_empty,
    vm_image_cache, delete_vm_image, _harvester_image, _modes_for_cdi, _vm_mac, static_network,
    prepare_vm, _recheck_vm_creation, commit_vm, create_vm,
)
from homestead_scheduled_jobs import (  # noqa: E402,F401
    CRONJOB_NAME, _job_managed, _job_managed_by, list_jobs, cronjob_alert_facts, _managed_job, save_job, del_job,
    run_job_now,
)
from homestead_image_cache import (  # noqa: E402,F401
    _canonical_image, _image_digest, _system_image, _needed_list, _group_absent,
    image_retention_inventory, _stopped_references, _rollback_note, _unreadable, _scaled_to_zero, _dig,
    _count, _waiting, _kept, _disk_images, _cdi_rows, _node_pull, _optional_list, _served_path,
    _unread_message, _discovery, _imported_digests, _rows, _holds, image_cache, SCAN_TASK, SCAN_FRESH,
    SCAN_RETRY, SCAN_STORE, _SCANS, _SCAN_FAILED, _SCAN_STARTED, _scan_lock, _scan_path,
    _load_scans, _save_scans, _merged, tick, _scannable, _rescan_if_stale, start_image_scan,
    _collect_scans, _scanned_images, PREPULL_NAMES, _pullable_nodes, prepull, prepull_status,
    stop_prepull, forget_rollback, cleanup_image,
)
from homestead_container_import import (  # noqa: E402,F401
    MEASURE_LINE, measure_source_paths, _deduct_nested, _ownership, _numeric, ownership_hint,
    chown_claim, _folder_name, import_volumes, import_mappings, _excludes, _claim_capacity_gb,
    prepare_import, commit_import, import_container, import_inventory, STEP, TOTAL, TROUBLE, RSYNC,
    import_progress, _job_ns, import_cleanup_plan, _names, _plan_volumes, delete_import,
    _daemonset_exists, wait_for_pods_gone, _stop_job_pods, _drop_source_copy, _drop_finished_copies,
    _job_pod, import_status,
)


# server.py's helpers the routes below call, bound by name (bind_routes):
# an import's capacity review lives there still.
ROUTE_NEEDS = ("guard_managed_smb", "preview_import", "reviewed_import", "workload_service_names")


def bind_routes(helpers):
    missing = set(ROUTE_NEEDS) - set(helpers)
    if missing:
        raise TypeError("homestead_imports needs " + ", ".join(sorted(missing)))
    globals().update({name: helpers[name] for name in ROUTE_NEEDS})


def _prepull(request):
    b = request.body
    result = prepull(b["image"], b.get("nodes"))
    result["operation"] = OPS.start(
        "image-pull", f"Pull {b['image']}",
        {"kind": "Image", "name": b["image"], "namespace": NS},
        "/image-cache", {"namespace": NS, "name": result["daemonset"]})
    return result


def _cleanup_image(request):
    b = request.body
    result = cleanup_image(b.get("digest"), b.get("nodes"))
    result["operation"] = OPS.start(
        "image-cleanup", f"Clean cached image {result['digest'][:19]}…",
        {"kind": "Image", "name": result["image"], "namespace": NS},
        "/image-cache", {"namespace": NS, "pods": result["pods"]})
    return result


def _delete_vm_image(request):
    homestead_routes.forget("vmimages")
    b = request.body
    return delete_vm_image(b.get("namespace", ""), b.get("name", ""), b.get("confirm", ""))


def _delete_import(request):
    """An import's record, and what it made that the request also names."""
    b = request.body
    plan = import_cleanup_plan(b.get("name"), b.get("namespace"))
    if plan.get("journalled") and any(b.get(key) for key in ("remove_workload", "remove_volume", "remove_volumes")):
        raise ValueError("This import retains workloads and volumes. Manage them separately after inspecting the copy.")
    made = {row["name"] for row in plan["volumes"] if row["created"]}
    # The request names claims; asking for all of them is the old
    # boolean, which an import with one volume still sends.
    wanted = [str(claim) for claim in (b.get("remove_volumes") or [])]
    if b.get("remove_volume") and not wanted:
        wanted = [row["name"] for row in plan["volumes"] if row["created"]]
    borrowed = [claim for claim in wanted if claim not in made]
    if borrowed:
        raise ValueError(f"this import copied into {', '.join(sorted(borrowed))} "
                         "without creating it, so it will not delete it")
    if b.get("remove_workload") and plan["workload"]:
        guard_managed_smb(plan["namespace"], plan["workload"])
    result = delete_import(b.get("name"), b.get("namespace"))
    if result.get("journalled"):
        return {**result, "removed": [], "releasing": []}
    removed = []
    if b.get("remove_workload") and plan["workload"]:
        ns, name = plan["namespace"], plan["workload"]
        services = set(workload_service_names(ns, name)) | {name}
        ksend("DELETE", f"/apis/apps/v1/namespaces/{ns}/deployments/{name}")
        for service in sorted(services):
            try:
                ksend("DELETE", f"/api/v1/namespaces/{ns}/services/{service}")
            except urllib.error.HTTPError:
                pass
        removed.append(f"workload {name}")
        # Its pod holds the same claims the caller may be deleting
        # next, so wait for it to go rather than leaving them
        # Terminating behind the pvc-protection finaliser.
        wait_for_pods_gone(ns, f"app={name}")
        homestead_routes.forget("wl", "ov", "network")
    for claim in wanted:
        ksend("DELETE", f"/api/v1/namespaces/{plan['namespace']}/"
                        f"persistentvolumeclaims/{claim}")
        removed.append(f"volume {claim}")
        homestead_routes.forget("vol", "stor")
    # A claim something still mounts only gets a deletion stamp, so
    # say it is releasing rather than reporting it gone.
    releasing = []
    for claim in wanted:
        try:
            live = kget(f"/api/v1/namespaces/{plan['namespace']}/"
                        f"persistentvolumeclaims/{claim}")
        except Exception:
            continue
        if (live.get("metadata", {}) or {}).get("deletionTimestamp"):
            releasing.append(claim)
    result["releasing"] = releasing
    if removed:
        result["message"] = result["message"] + " with " + " and ".join(removed)
    if result.get("releasing"):
        result["message"] += (" — " + ", ".join(result["releasing"])
                              + " will finish deleting once released")
    result["removed"] = removed
    return result


# Its routes and who may use them (homestead_routes.py).
def _schedule_saved(request):
    save_job(request.body)
    return {"ok": True}


def _schedule_run(request):
    run_job_now(request.body["name"])
    return {"ok": True}


ROUTES = {
    ("POST", "/api/schedules"): ("operator", _schedule_saved),
    ("POST", "/api/schedules/run"): ("operator", _schedule_run),
    ("POST", "/api/images/prepull"): ("operator", _prepull),
    ("POST", "/api/images/cleanup"): ("admin", _cleanup_image),
    ("POST", "/api/images/vm/delete"): ("admin", _delete_vm_image),
    ("POST", "/api/images/forget-rollback"): ("admin", lambda request: forget_rollback(
        request.body.get("namespace") or NS, request.body.get("name", ""))),
    ("POST", "/api/imports/delete"): ("admin", _delete_import),
    ("POST", "/api/import/preview"): ("admin", lambda request: preview_import(request.body)),
    ("POST", "/api/import"): ("admin", lambda request: reviewed_import(request.body)),
    ("GET", "/api/vm-disks"): ("viewer", lambda request: list_vm_disks()),
    ("GET", "/api/sources"): ("admin", lambda request: list_sources()),
    ("GET", "/api/imports"): ("viewer", lambda request: import_status()),
    ("POST", "/api/imports/cleanup-plan"): ("admin", lambda request: import_cleanup_plan(request.body.get("name"), request.body.get("namespace"))),
    ("POST", "/api/images/prepull/stop"): ("operator", lambda request: stop_prepull(request.body.get("name"))),
    ("POST", "/api/images/scan"): ("admin", lambda request: start_image_scan()),
    ("POST", "/api/schedules/delete"): ("operator", lambda request: del_job(request.body["name"])),
    ("POST", "/api/sources/scan"): ("admin", lambda request: scan_source(request.body.get("name"), request.user)),
    ("POST", "/api/sources/trust"): ("admin", lambda request: trust_source(request.body, request.user)),
    ("POST", "/api/sources/inspect"): ("admin", lambda request: inspect_source_container(request.body["name"], request.body["container"])),
    ("GET", "/api/images/vm"): ("viewer", lambda request: homestead_routes.cached("vmimages", 15, vm_image_cache)),
    ("GET", "/api/vmimages"): ("viewer", lambda request: homestead_routes.cached("vmimg", 30, list_vm_images)),
    ("GET", "/api/images"): ("viewer", lambda request: homestead_routes.cached("imgcache", 30, image_cache)),
    ("GET", "/api/schedules"): ("viewer", lambda request: homestead_routes.cached("cron", 8, list_jobs)),
    ("POST", "/api/sources/measure"): ("admin", lambda request: measure_source_paths(request.body.get("name"), request.body.get("paths") or [], request.body.get("seconds", 25))),
    ("POST", "/api/sources"): ("admin", lambda request: {"ok": True, "sources": add_source(request.body["name"], request.body["host"], request.body["user"], request.body.get("password"), request.body.get("kind", "unraid"), request.body.get("base_path", "/mnt/user/appdata"), request.body.get("port", 22))}),
    ("POST", "/api/sources/delete"): ("admin", lambda request: {"ok": True, "sources": del_source(request.body["name"])}),
    ("POST", "/api/sources/browse"): ("admin", lambda request: {"entries": browse_source(request.body["name"], request.body.get("path"))}),
    ("POST", "/api/sources/containers"): ("admin", lambda request: {"containers": source_containers(request.body["name"])}),
    ("GET", "/api/volumes/ownership"): ("viewer", lambda request: ownership_hint(
        (request.query.get("namespace") or [NS])[0], (request.query.get("name") or [""])[0])),
    ("GET", "/api/vm-disks/import-plan"): ("viewer", lambda request: vm_disk_import_plan(
        (request.query.get("ns") or [NS])[0], (request.query.get("name") or [""])[0])),
    ("POST", "/api/volumes/chown"): ("admin", lambda request: chown_claim(
        request.body.get("namespace") or NS, request.body.get("name"), request.body.get("uid"), request.body.get("gid"))),
    ("POST", "/api/vm-disks/import"): ("admin", lambda request: import_vm_disk(request.body, ops=OPS)),
}
