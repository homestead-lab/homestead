"""
Import pipeline + VM creation + image cache + scheduled jobs.

Import sources are remote hosts (Unraid, Proxmox, plain SSH) registered in a
ConfigMap, with credentials in a Secret. Importing a container means:
  1. create a PVC for its appdata,
  2. run a Job that rsyncs the remote path into that PVC,
  3. create the Deployment pointing at it.
Step 2 is the part that takes real time, so it runs as a Job we can poll.
"""
import json
import hashlib
import re
import shlex
import time
import urllib.error
import urllib.parse

import homestead_names as NAMES
import homestead_hvimage as HVIMAGE

kget = ksend = create_pvc = build_deployment = None
NS = "lab"
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


def list_sources():
    try:
        cm = kget(f"/api/v1/namespaces/{NS}/configmaps/{_sources_map()}")
        return json.loads(cm.get("data", {}).get("sources.json", "[]"))
    except Exception:
        return []


def save_sources(srcs):
    name = _sources_map()
    body = {"apiVersion": "v1", "kind": "ConfigMap",
            "metadata": {"name": name, "namespace": NS},
            "data": {"sources.json": json.dumps(srcs, indent=2)}}
    try:
        kget(f"/api/v1/namespaces/{NS}/configmaps/{name}")
        return ksend("PUT", f"/api/v1/namespaces/{NS}/configmaps/{name}", body)
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return ksend("POST", f"/api/v1/namespaces/{NS}/configmaps", body)
        raise


def add_source(name, host, user, password, kind="unraid", base_path="/mnt/user/appdata"):
    if not SAFE.match(name):
        raise ValueError("name must be lowercase letters, numbers and dashes")
    srcs = [s for s in list_sources() if s["name"] != name]
    srcs.append({"name": name, "host": host, "user": user, "kind": kind,
                 "base_path": base_path, "added": time.strftime("%Y-%m-%d %H:%M")})
    save_sources(srcs)
    # credentials live in a Secret, never in the ConfigMap
    sec = {"apiVersion": "v1", "kind": "Secret", "type": "Opaque",
           "metadata": {"name": source_secret(name), "namespace": NS},
           "stringData": {"password": password or ""}}
    try:
        existing = source_secret(name)
        kget(f"/api/v1/namespaces/{NS}/secrets/{existing}")
        ksend("PUT", f"/api/v1/namespaces/{NS}/secrets/{existing}", sec)
    except urllib.error.HTTPError as e:
        if e.code != 404:
            raise
        ksend("POST", f"/api/v1/namespaces/{NS}/secrets", sec)
    return srcs


def del_source(name):
    srcs = [s for s in list_sources() if s["name"] != name]
    save_sources(srcs)
    try:
        ksend("DELETE", f"/api/v1/namespaces/{NS}/secrets/{source_secret(name)}")
    except urllib.error.HTTPError:
        pass
    return srcs


def source_secret(name):
    """This source's password secret."""
    return f"homestead-src-{name}"


def _source(name):
    s = next((x for x in list_sources() if x["name"] == name), None)
    if not s:
        raise ValueError(f"no import source named {name}")
    return s


# --------------------------------------------------------------- discovery
def browse_source(name, path=None):
    """List candidate appdata directories on a source host, via a short-lived Job.

    We cannot SSH from the API server, so we run a pod that does it and read the
    result from its logs. Slower than a direct call but keeps credentials inside
    the cluster.
    """
    s = _source(name)
    p = path or s.get("base_path", "/mnt/user/appdata")
    remote_cmd = f"ls -1 {shlex.quote(p)} 2>/dev/null | head -200"
    script = ("sshpass -p \"$SRC_PASS\" ssh -o StrictHostKeyChecking=no "
              "-o LogLevel=ERROR "
              "-o UserKnownHostsFile=/dev/null "
              + shlex.quote(f"{s['user']}@{s['host']}") + " "
              + shlex.quote(remote_cmd))
    return run_probe(f"browse-{name}", script, s)


def _ssh_script(src, remote_cmd):
    return ("sshpass -p \"$SRC_PASS\" ssh -o StrictHostKeyChecking=no "
            "-o LogLevel=ERROR "
            "-o UserKnownHostsFile=/dev/null "
            + shlex.quote(f"{src['user']}@{src['host']}") + " "
            + shlex.quote(remote_cmd))


def source_containers(name):
    """List Docker containers on an Unraid/generic Docker source.

    Docker's JSON line format gives us the real image reference instead of
    asking the user to remember or retype it during import.
    """
    src = _source(name)
    lines = run_probe(f"containers-{name}", _ssh_script(
        src, "docker ps -a --format '{{json .}}' 2>/dev/null | head -200"), src)
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


def run_probe(tag, script, src, timeout=70):
    """Run a one-shot pod, wait for it, return its stdout."""
    pod = f"homestead-probe-{re.sub(r'[^a-z0-9-]', '-', tag)[:30]}-{int(time.time()) % 100000}"
    body = {
        "apiVersion": "v1", "kind": "Pod",
        "metadata": {"name": pod, "namespace": NS, "labels": NAMES.labels("probe")},
        "spec": {"restartPolicy": "Never", "terminationGracePeriodSeconds": 1,
                 "containers": [{
                     "name": "probe", "image": "alpine:3.20",
                     "command": ["sh", "-c",
                                 "apk add --no-cache openssh-client sshpass >/dev/null 2>&1; " + script],
                     "env": [{"name": "SRC_PASS", "valueFrom": {"secretKeyRef": {
                         "name": source_secret(src["name"]), "key": "password"}}}],
                 }]},
    }
    ksend("POST", f"/api/v1/namespaces/{NS}/pods", body)
    out, deadline = "", time.time() + timeout
    try:
        while time.time() < deadline:
            time.sleep(2)
            st = kget(f"/api/v1/namespaces/{NS}/pods/{pod}").get("status", {})
            if st.get("phase") in ("Succeeded", "Failed"):
                break
        import urllib.request
        out = _pod_logs(pod)
    finally:
        try:
            ksend("DELETE", f"/api/v1/namespaces/{NS}/pods/{pod}")
        except Exception:
            pass
    return [l.strip() for l in out.splitlines() if l.strip()]


def _pod_logs(pod):
    import urllib.request
    from homestead_shim import raw_get  # provided by server.py
    return raw_get(f"/api/v1/namespaces/{NS}/pods/{pod}/log?tailLines=400")


# --------------------------------------------------------------- import job
MEASURE_LINE = re.compile(r"^(\d+)\s+(.*)$")


def measure_source_paths(name, paths, seconds=25):
    """Ask the source host how big each folder is, and stop asking if it drags.

    du walks every inode, which on a deep appdata tree can take longer than
    anyone wants to wait at a modal. Each path is measured under its own
    timeout so one slow folder costs that folder's answer rather than the
    whole measurement, and an unmeasured folder is reported as unknown instead
    of as zero.
    """
    src = _source(name)
    seconds = max(5, min(120, int(seconds or 25)))
    wanted = []
    for item in paths or []:
        candidate = str(item or "").strip().rstrip("/")
        if not candidate.startswith("/"):
            raise ValueError(f"path must be absolute, got {candidate or '(blank)'}")
        if candidate not in wanted:
            wanted.append(candidate)
    if not wanted:
        return {"paths": [], "total_bytes": 0, "complete": True, "suggested_gb": 1}

    # du -sk is the portable spelling: busybox and GNU both have it.
    commands = []
    for candidate in wanted:
        quoted = shlex.quote(candidate)
        commands.append(f"echo \"### {candidate}\"; [ -e {quoted} ] || echo MISSING; "
                        f"[ -e {quoted} ] && {{ timeout {seconds} du -sk {quoted} 2>/dev/null "
                        f"|| echo TIMEOUT; }}")
    lines = run_probe(f"measure-{name}", _ssh_script(src, "; ".join(commands)), src,
                      timeout=min(180, seconds * len(wanted) + 40))

    rows, current = [], None
    for line in lines:
        if line.startswith("### "):
            current = line[4:].strip()
            rows.append({"path": current, "bytes": None, "measured": False, "exists": True,
                         "timed_out": False})
            continue
        if not rows:
            continue
        if line.strip() == "MISSING":
            rows[-1]["exists"] = False
            continue
        if line.strip() in ("TIMEOUT", "UNKNOWN"):
            rows[-1]["timed_out"] = True
            continue
        match = MEASURE_LINE.match(line.strip())
        if match and rows[-1]["bytes"] is None:
            rows[-1].update(bytes=int(match.group(1)) * 1024, measured=True)
    _deduct_nested(rows)
    measured = [row for row in rows if row["measured"]]
    total = sum(row["bytes"] for row in measured)
    missing = [row["path"] for row in rows if not row["exists"]]
    complete = bool(rows) and len(measured) == len(rows)
    # Room for the copy plus what the app writes next; never below 1 GiB.
    suggested = max(1, int(total / (1024 ** 3) * 1.25) + 1)
    return {"paths": rows, "total_bytes": total, "complete": complete, "missing": missing,
            "suggested_gb": suggested, "timeout_seconds": seconds}


def _deduct_nested(rows):
    """du counts a subfolder inside its parent; the copy will not.

    Anything mapped separately is excluded from its parent's rsync, so the
    parent's measured size has to lose it too - otherwise a 4 TB recordings
    folder is counted once against the appdata volume it is not going to and
    once against the volume it is.
    """
    by_path = {row["path"]: row for row in rows if row["measured"]}
    for row in rows:
        if not row["measured"]:
            continue
        inside = sum(other["bytes"] for path, other in by_path.items()
                     if path.startswith(row["path"] + "/")
                     # Only the nearest parent deducts a folder, or a tree three
                     # deep would subtract the same bytes twice.
                     and not any(path.startswith(mid + "/") and mid.startswith(row["path"] + "/")
                                 for mid in by_path))
        if inside:
            row["gross_bytes"] = row["bytes"]
            row["bytes"] = max(0, row["bytes"] - inside)


def _ownership(cfg):
    """The uid:gid imported files should end up owned by, if any.

    rsync runs as root with --no-owner, so everything it writes lands owned by
    root. A container that runs as its own user - mosquitto as 1883, a
    linuxserver image as PUID - then cannot write to its own appdata.
    """
    def _id(value):
        text = str(value if value is not None else "").strip()
        if not text:
            return None
        if not text.isdigit() or not 0 <= int(text) <= 65535:
            raise ValueError("user and group ids must be whole numbers between 0 and 65535")
        return int(text)

    env = cfg.get("env") or {}
    uid = _id(cfg.get("uid") if cfg.get("uid") not in (None, "") else env.get("PUID"))
    gid = _id(cfg.get("gid") if cfg.get("gid") not in (None, "") else env.get("PGID"))
    if uid is not None and gid is None:
        gid = uid
    return uid, gid


def _numeric(value):
    text = str(value if value is not None else "").strip()
    return int(text) if text.isdigit() and 0 <= int(text) <= 65535 else None


def ownership_hint(namespace, pvc):
    """Who should own this claim's files, judged from whatever mounts it.

    The answer is never guessed: it comes from the workload itself, in the
    order of how explicit each source is - the PUID/PGID convention first,
    then a securityContext, then the pod's fsGroup. When nothing says, the
    mounting workload and its image are reported so the answer can be looked
    up rather than invented.
    """
    namespace = str(namespace or NS)
    try:
        deployments = kget(f"/apis/apps/v1/namespaces/{namespace}/deployments").get("items", [])
    except Exception:
        deployments = []
    for deployment in deployments:
        spec = ((deployment.get("spec", {}) or {}).get("template", {}) or {}).get("spec", {}) or {}
        mounted = {volume.get("name") for volume in spec.get("volumes", []) or []
                   if (volume.get("persistentVolumeClaim") or {}).get("claimName") == pvc}
        if not mounted:
            continue
        workload = deployment["metadata"]["name"]
        pod_security = spec.get("securityContext", {}) or {}
        for container in spec.get("containers", []) or []:
            if not any(mount.get("name") in mounted
                       for mount in container.get("volumeMounts", []) or []):
                continue
            env = {item.get("name"): item.get("value", "")
                   for item in container.get("env", []) or [] if item.get("name")}
            security = container.get("securityContext", {}) or {}
            image = container.get("image", "")
            candidates = (
                (_numeric(env.get("PUID")), _numeric(env.get("PGID")),
                 f"PUID/PGID on {container.get('name', workload)}"),
                (_numeric(security.get("runAsUser")), _numeric(security.get("runAsGroup")),
                 f"the container's security context in {workload}"),
                (_numeric(pod_security.get("runAsUser")), _numeric(pod_security.get("fsGroup")),
                 f"the pod security context in {workload}"),
                (None, _numeric(pod_security.get("fsGroup")), f"the fsGroup already set on {workload}"),
            )
            for uid, gid, source in candidates:
                if uid is None and gid is None:
                    continue
                return {"uid": uid, "gid": gid if gid is not None else uid,
                        "source": source, "workload": workload, "image": image,
                        "known": True}
            return {"uid": None, "gid": None, "workload": workload, "image": image,
                    "known": False,
                    "source": f"{workload} does not declare a user; check what {image or 'its image'} "
                              "runs as - mosquitto uses 1883, linuxserver images use PUID"}
    return {"uid": None, "gid": None, "known": False, "workload": "", "image": "",
            "source": "nothing mounts this volume, so its files can be owned by anyone you choose"}


def chown_claim(namespace, pvc, uid, gid):
    """Hand an existing claim to a user, for appdata already copied as root."""
    namespace = str(namespace or NS)
    if not re.fullmatch(r"[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?", str(pvc or "")):
        raise ValueError("volume name must be lowercase letters, numbers and dashes")
    owner_uid, owner_gid = _ownership({"uid": uid, "gid": gid})
    if owner_uid is None:
        raise ValueError("a user id is required")
    job = f"homestead-chown-{pvc}"[:60].rstrip("-")
    try:
        ksend("DELETE", f"/apis/batch/v1/namespaces/{namespace}/jobs/{job}"
                        "?propagationPolicy=Background")
        time.sleep(1)
    except urllib.error.HTTPError:
        pass
    script = (f"echo '==> step 1/1 ownership :: chown -R {owner_uid}:{owner_gid} /data'\n"
              f"chown -R {owner_uid}:{owner_gid} /data\n"
              "echo '==> done'; ls -ld /data\n")
    body = {
        "apiVersion": "batch/v1", "kind": "Job",
        "metadata": {"name": job, "namespace": namespace,
                     "labels": NAMES.labels("chown", app=pvc)},
        "spec": {"backoffLimit": 1, "ttlSecondsAfterFinished": 600,
                 "template": {"metadata": {"labels": NAMES.labels("chown")},
                              "spec": {"restartPolicy": "Never",
                                       "containers": [{"name": "chown", "image": "alpine:3.20",
                                                       "command": ["sh", "-c", script],
                                                       "volumeMounts": [{"name": "data", "mountPath": "/data"}]}],
                                       "volumes": [{"name": "data",
                                                    "persistentVolumeClaim": {"claimName": pvc}}]}}},
    }
    ksend("POST", f"/apis/batch/v1/namespaces/{namespace}/jobs", body)
    _bust("vol", "stor")
    return {"ok": True, "job": job, "uid": owner_uid, "gid": owner_gid,
            "message": f"Setting ownership of {pvc} to {owner_uid}:{owner_gid}"}


def _folder_name(value, used):
    """A safe directory name inside the appdata volume."""
    base = re.sub(r"[^a-z0-9._-]+", "-", str(value or "").strip("/").split("/")[-1].lower())
    base = base.strip("-.")[:60] or "data"
    name, suffix = base, 2
    while name in used:
        name = f"{base}-{suffix}"
        suffix += 1
    used.add(name)
    return name


def import_volumes(cfg):
    """The claims this import writes into, in the order they were defined.

    Appdata and recordings do not belong on the same volume: one is small and
    wants replicas, the other is large and usually does not. An import may
    therefore land in several claims, and each folder says which one it goes to.
    """
    rows, seen = [], set()
    requested = cfg.get("volumes") or []
    copies = [row for row in (cfg.get("mappings") or [])
              if str(row.get("medium") or "").lower() != "memory"
              and (str(row.get("remote_path") or "").strip() or row.get("copy") is False)]
    if not requested and not copies and cfg.get("mappings") is not None:
        # Plenty of containers keep nothing: a webhook relay, a bridge, a
        # snapshot helper. Inventing an appdata claim for one creates storage
        # nobody asked for and a copy job with nothing to copy.
        return []
    if not requested:
        # The original shape: one claim for the whole import.
        requested = [{"name": cfg.get("pvc_name") or f"{cfg.get('name', 'app')}-appdata",
                      "create": not cfg.get("reuse_existing"),
                      "size_gb": cfg.get("size_gb", 10),
                      "storage_class": cfg.get("storage_class"),
                      "access_mode": cfg.get("access_mode")}]
    for item in requested:
        claim = str(item.get("name") or "").strip()
        if not re.fullmatch(r"[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?", claim):
            raise ValueError(f"volume name {claim or '(blank)'} must be lowercase letters, "
                             "numbers and dashes")
        if claim in seen:
            raise ValueError(f"volume {claim} is listed twice")
        seen.add(claim)
        create = bool(item.get("create", True))
        size = int(item.get("size_gb") or 10)
        if create and not 1 <= size <= 16384:
            raise ValueError("volume size must be between 1 and 16384 GiB")
        access_mode = str(item.get("access_mode") or "ReadWriteOnce")
        if access_mode not in ("ReadWriteOnce", "ReadWriteMany"):
            raise ValueError("access mode must be ReadWriteOnce or ReadWriteMany")
        storage_class = str(item.get("storage_class") or "longhorn-r2").strip()
        if not re.fullmatch(r"[a-z0-9]([a-z0-9.-]{0,251}[a-z0-9])?", storage_class):
            raise ValueError("storage class must use lowercase letters, numbers, dots and dashes")
        rows.append({"name": claim, "create": create, "size_gb": size,
                     "storage_class": storage_class, "access_mode": access_mode})
    return rows


def import_mappings(cfg):
    """Every remote directory this import copies, and where it lands.

    An Unraid container usually maps several folders under appdata. They belong
    in one volume per container, each copied into its own subdirectory and
    mounted back at the path the container expects through subPath, rather than
    a volume per mapping.
    """
    rows, used, paths = [], set(), set()
    volumes = import_volumes(cfg)
    names = [volume["name"] for volume in volumes]
    requested = cfg.get("mappings")
    if requested is None:
        # The original single-folder shape, kept so older clients still work.
        requested = [{"remote_path": cfg.get("remote_path"),
                      "mount_path": cfg.get("mount_path", "/config"), "folder": ""}]
    if not requested:
        return []
    single = len(requested) == 1
    for item in requested:
        remote = str(item.get("remote_path") or "").strip().rstrip("/")
        mount = str(item.get("mount_path") or "").strip().rstrip("/") or "/config"
        if str(item.get("medium") or "").lower() == "memory":
            # A scratch mount has no source: the point of it is that it starts
            # empty and lives in RAM, exactly as tmpfs did on the source host.
            if mount in paths:
                raise ValueError(f"{mount} is mapped twice")
            paths.add(mount)
            size_mb = int(item.get("size_mb") or 1024)
            if not 1 <= size_mb <= 65536:
                raise ValueError("scratch size must be between 1 MiB and 64 GiB")
            rows.append({"remote_path": "", "mount_path": mount, "folder": "", "bytes": 0,
                         "pvc": "", "medium": "memory", "size_mb": size_mb})
            continue
        # Mounted empty: a volume the container gets without anything copied
        # into it - new recordings, say, where the old ones stay behind.
        copy = item.get("copy") is not False
        if copy and not remote.startswith("/"):
            raise ValueError(f"remote path must be absolute, got {remote or '(blank)'}")
        if not mount.startswith("/"):
            raise ValueError(f"container path must be absolute, got {mount}")
        if mount in paths:
            raise ValueError(f"{mount} is mapped twice")
        paths.add(mount)
        target = str(item.get("pvc") or "").strip() or names[0]
        folder = str(item.get("folder") or "").strip("/")
        if folder:
            if not re.fullmatch(r"[A-Za-z0-9._-]+(?:/[A-Za-z0-9._-]+)*", folder):
                raise ValueError(f"folder {folder} must be a plain relative path")
            if ".." in folder.split("/"):
                raise ValueError("folder cannot climb out of the volume")
            used.add((target, folder))
        elif sum(1 for row in requested
                 if str(row.get("medium") or "").lower() != "memory"
                 and (str(row.get("pvc") or "").strip() or names[0]) == target) > 1:
            # A volume receiving one folder takes it at its root; several
            # folders sharing a volume each get their own subdirectory.
            folder = _folder_name(remote or mount, {name for claim, name in used if claim == target})
            used.add((target, folder))
        try:
            size = max(0, int(item.get("bytes") or 0))
        except (TypeError, ValueError):
            size = 0
        claim = str(item.get("pvc") or "").strip() or names[0]
        if claim not in names:
            raise ValueError(f"{mount} points at volume {claim}, which this import does not create")
        rows.append({"remote_path": remote, "mount_path": mount, "folder": folder,
                     "bytes": size if copy else 0, "pvc": claim, "medium": "", "size_mb": 0, "copy": copy,
                     "exclude": _excludes(remote, requested, item.get("exclude")) if copy else []})
    return rows


def _excludes(remote, requested, asked):
    """Subfolders of this folder that the copy must leave alone.

    Frigate on Unraid mounts /mnt/user/cctv/Frigate as its config and
    /mnt/user/cctv/Frigate/recordings as its recordings. Copying the first
    without saying otherwise drags the second along - into the wrong volume,
    twice over, terabytes of it. Anything mapped separately is excluded from
    its parent, whether or not that mapping is being copied: the client sends
    the folders it left unticked so those stay behind too.
    """
    nested = set()
    for other in requested:
        child = str(other.get("remote_path") or "").strip().rstrip("/")
        if child.startswith(remote + "/"):
            nested.add(child[len(remote):])
    for item in asked or []:
        relative = "/" + str(item or "").strip().strip("/")
        if relative == "/" or ".." in relative.split("/"):
            continue
        nested.add(relative)
    return sorted(nested)


def _claim_used_gb(pvc):
    """How much a Longhorn volume has already written into this claim."""
    try:
        volumes = kget("/apis/longhorn.io/v1beta2/volumes").get("items", [])
    except Exception:
        return 0.0
    for volume in volumes:
        kubernetes = (volume.get("status", {}) or {}).get("kubernetesStatus", {}) or {}
        if kubernetes.get("namespace") == NS and kubernetes.get("pvcName") == pvc:
            return int((volume.get("status", {}) or {}).get("actualSize", 0) or 0) / 1024 ** 3
    return 0.0


def _claim_capacity_gb(pvc):
    """What an existing claim actually offers, or 0 when it cannot be read."""
    try:
        claim = kget(f"/api/v1/namespaces/{NS}/persistentvolumeclaims/{pvc}")
    except Exception:
        return 0.0
    quantity = ((claim.get("status", {}) or {}).get("capacity", {}) or {}).get("storage", "")
    match = re.fullmatch(r"([0-9.]+)([KMGTP]i?)?", str(quantity).strip())
    if not match:
        return 0.0
    scale = {"Ki": 1 / 1024 ** 2, "Mi": 1 / 1024, "Gi": 1, "Ti": 1024,
             "K": 1 / 1000 ** 2, "M": 1 / 1000, "G": 1, "T": 1000}
    return float(match.group(1)) * scale.get(match.group(2) or "Gi", 1)


def import_container(cfg):
    """Create the volumes, launch the copy Job, then create the Deployment.

    cfg: {source, name, image, ports, env, volumes[], mappings[], hardware,
          network_mode, start_after_copy, uid, gid}
    """
    name = cfg["name"]
    if not SAFE.match(name):
        raise ValueError("name must be lowercase letters, numbers and dashes")
    src = _source(cfg["source"])
    volumes = import_volumes(cfg)
    mappings = import_mappings(cfg)
    # What the job copies; a mapping mounted empty is only the workload's.
    copied = [row for row in mappings if not row.get("medium") and row.get("copy", True)]
    pvc = volumes[0]["name"] if volumes else ""

    # Each volume is judged on what is going into it, not on the import total:
    # a 500 GiB recordings claim says nothing about whether appdata fits.
    if not cfg.get("ignore_capacity"):
        for volume in volumes:
            needed = sum(row.get("bytes") or 0 for row in mappings if row["pvc"] == volume["name"])
            if not needed:
                continue
            capacity_gb = float(volume["size_gb"])
            used_gb = 0.0
            if not volume["create"]:
                capacity_gb = _claim_capacity_gb(volume["name"])
                used_gb = _claim_used_gb(volume["name"])
            needed_gb = needed / 1024 ** 3
            if capacity_gb and needed_gb > capacity_gb - used_gb:
                raise ValueError(
                    f"the measured source needs {needed_gb:.1f} GiB but {volume['name']} has "
                    f"{max(0.0, capacity_gb - used_gb):.1f} GiB free of {capacity_gb:.0f} GiB"
                    + (f" ({used_gb:.1f} GiB already written)" if used_gb else "")
                    + ". Grow the volume or choose a larger size, then import again.")

    for volume in volumes:
        if volume["create"]:
            create_pvc(NS, volume["name"], volume["size_gb"], volume["storage_class"],
                       volume["access_mode"])
            continue
        try:
            existing = kget(f"/api/v1/namespaces/{NS}/persistentvolumeclaims/{volume['name']}")
        except urllib.error.HTTPError as error:
            if error.code == 404:
                raise ValueError(f"existing PVC {volume['name']} was not found") from error
            raise
        modes = (existing.get("spec", {}) or {}).get("accessModes", []) or []
        volume["access_mode"] = modes[0] if modes else volume["access_mode"]
        volume["storage_class"] = (existing.get("spec", {}) or {}).get(
            "storageClassName", volume["storage_class"])
    storage_class = volumes[0]["storage_class"] if volumes else ""
    access_mode = volumes[0]["access_mode"] if volumes else ""

    job = f"homestead-import-{name}"
    for previous in (job,):
        # A rerun clears the job from before.
        try:
            ksend("DELETE", f"/apis/batch/v1/namespaces/{NS}/jobs/{previous}"
                            "?propagationPolicy=Background")
        except urllib.error.HTTPError:
            pass
    time.sleep(1)

    # Every interpolated value arrives from the UI, so all of it is quoted.
    # Each step announces itself on its own line before rsync's own progress,
    # which is what lets the UI say "2 of 5" instead of replaying 0-100% per
    # folder with nothing to say which folder it is on.
    steps = ["set -e",
             "apk add --no-cache rsync openssh-client sshpass >/dev/null 2>&1"]
    # rsync fails per folder, halfway through, after the volume exists. Asking
    # the source about all of them first turns that into one clear refusal.
    sources = [mapping["remote_path"] for mapping in copied]
    if sources:
        remote_check = "; ".join(f"[ -e {shlex.quote(source)} ] || echo {shlex.quote(source)}"
                                 for source in sources)
        steps.append("echo '==> checking the source folders exist'")
        steps.append(
            'missing=$(sshpass -p "$SRC_PASS" ssh -o StrictHostKeyChecking=no '
            "-o UserKnownHostsFile=/dev/null "
            f"{shlex.quote(src['user'] + '@' + src['host'])} {shlex.quote(remote_check)})")
        steps.append('if [ -n "$missing" ]; then echo "==> missing $missing"; exit 4; fi')
    total = len(copied)
    # Measured up front, the copy can report bytes rather than folder counts.
    copied_rows = copied
    measured = sum(mapping.get("bytes") or 0 for mapping in copied_rows)
    if measured and copied_rows and all(mapping.get("bytes") for mapping in copied_rows):
        steps.append("echo " + shlex.quote(f"==> total {total} folders {measured}B"))
    mount_of = {volume["name"]: (f"/mnt/{volume['name']}" if len(volumes) > 1 else "/appdata")
                for volume in volumes}
    for index, mapping in enumerate(copied, start=1):
        base = mount_of[mapping["pvc"]]
        target = (base + "/" + mapping["folder"]) if mapping["folder"] else base
        spec = shlex.quote(f"{src['user']}@{src['host']}:{mapping['remote_path']}/")
        label = mapping["folder"] or (mapping["pvc"] if len(volumes) > 1 else "appdata")
        steps.append(f"mkdir -p {shlex.quote(target)}")
        weight = f" ({mapping['bytes']}B)" if mapping.get("bytes") else ""
        steps.append("echo " + shlex.quote(
            f"==> step {index}/{total} {label}{weight} :: {src['host']}:{mapping['remote_path']}"
            f" -> {mapping['mount_path']}"))
        # Said out loud, so a copy that comes back smaller than the folder looks
        # is explained by the log rather than by a support question.
        if mapping.get("exclude"):
            steps.append("echo " + shlex.quote(
                "    leaving out " + ", ".join(mapping["exclude"]) + " (mapped separately)"))
        steps.append(
            # Ownership and permissions are preserved by number, so the app
            # finds its appdata exactly as it was on the source. Dropping them
            # made every file root-owned, which is why an imported container
            # could not write to data a fresh deployment would have created
            # itself.
            'sshpass -p "$SRC_PASS" rsync -aH --numeric-ids --info=progress2 '
            "-e 'ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null' "
            # A leading slash anchors the pattern to the folder being copied,
            # so /recordings means that one and not every directory so named.
            + "".join(f"--exclude={shlex.quote(pattern + '/')} "
                      for pattern in mapping.get("exclude") or [])
            + f"{spec} {shlex.quote(target + '/')}")
        steps.append("echo " + shlex.quote(f"==> step {index}/{total} {label} complete"))
    # Only when asked: the copy already keeps whatever the source had.
    owner_uid, owner_gid = _ownership(cfg)
    if owner_uid is not None:
        steps.append("echo " + shlex.quote(f"==> owner {owner_uid}:{owner_gid}"))
        for mount in sorted(set(mount_of.values())):
            steps.append(f"chown -R {owner_uid}:{owner_gid} {shlex.quote(mount)}")
    steps.append("echo '==> done'; du -sh " + " ".join(
        shlex.quote(mount) for mount in sorted(set(mount_of.values()))))
    script = "\n".join(steps) + "\n"

    body = {
        "apiVersion": "batch/v1", "kind": "Job",
        "metadata": {"name": job, "namespace": NS,
                     "labels": NAMES.labels("import", app=name),
                     # What this import made, so cleaning it up later does not
                     # have to guess - and cannot offer to delete a volume it
                     # merely borrowed.
                     "annotations": {"homestead.io/import-workload": name if cfg.get("create_workload", True) else "",
                                     "homestead.io/import-volume": pvc,
                                     "homestead.io/import-volume-created":
                                         "true" if volumes and volumes[0]["create"] else "false",
                                     "homestead.io/import-volumes-created":
                                         ",".join(v["name"] for v in volumes if v["create"]),
                                     "homestead.io/import-volumes":
                                         ",".join(v["name"] for v in volumes)}},
        "spec": {"backoffLimit": 1, "ttlSecondsAfterFinished": 3600,
                 "template": {"metadata": {"labels": NAMES.labels("import")},
                              "spec": {"restartPolicy": "Never",
                                       "containers": [{
                                           "name": "copy", "image": "alpine:3.20",
                                           "command": ["sh", "-c", script],
                                           "env": [{"name": "SRC_PASS", "valueFrom": {"secretKeyRef": {
                                               "name": source_secret(src["name"]), "key": "password"}}}],
                                           "volumeMounts": [
                                               {"name": f"vol{index}", "mountPath": mount_of[volume["name"]]}
                                               for index, volume in enumerate(volumes)],
                                       }],
                                       "volumes": [
                                           {"name": f"vol{index}",
                                            "persistentVolumeClaim": {"claimName": volume["name"]}}
                                           for index, volume in enumerate(volumes)]}}},
    }
    # A container with nothing to copy needs no copy job; the workload below
    # is the whole import.
    if copied:
        ksend("POST", f"/apis/batch/v1/namespaces/{NS}/jobs", body)

    created = None
    if cfg.get("create_workload", True):
        dcfg = {
            "name": name, "namespace": NS, "image": cfg["image"],
            "replicas": 0 if cfg.get("start_after_copy", True) else 1,
            "cpu": cfg.get("cpu", "50m"), "memory": cfg.get("memory", "256Mi"),
            "ports": cfg.get("ports") or [], "env": cfg.get("env") or {},
            "volumes": [
                {"path": mapping["mount_path"], "type": "emptyDir", "medium": "memory",
                 "size_limit": f"{mapping['size_mb']}Mi"}
                if mapping.get("medium") else
                {"path": mapping["mount_path"], "source": mapping["pvc"], "type": "pvc",
                 "sub_path": mapping["folder"]}
                for mapping in mappings],
            "fs_group": owner_gid,
            "gpu": bool(cfg.get("gpu")),
            "hardware": cfg.get("hardware") or [], "icon": cfg.get("icon", ""),
            "icon_source": cfg.get("icon_source", cfg.get("icon", "")),
            "network_mode": cfg.get("network_mode", "loadbalancer"),
            "vip_mode": cfg.get("vip_mode", "shared"), "lb_ip": cfg.get("lb_ip", ""),
            "privileged": bool(cfg.get("privileged")), "cap_add": cfg.get("cap_add") or [],
            "tun": bool(cfg.get("tun")),
        }
        dep, svc = build_deployment(dcfg)
        try:
            ksend("POST", f"/apis/apps/v1/namespaces/{NS}/deployments", dep)
        except urllib.error.HTTPError as e:
            if e.code != 409:
                raise
        if svc:
            try:
                ksend("POST", f"/api/v1/namespaces/{NS}/services", svc)
            except urllib.error.HTTPError as e:
                if e.code != 409:
                    raise
        created = name
    _bust("wl", "ov", "flow")
    return {"ok": True, "job": job, "pvc": pvc, "deployment": created,
            "mappings": mappings, "volumes": volumes,
            "storage_class": storage_class, "access_mode": access_mode,
            "note": "Deployment created stopped; start it once the copy job finishes."
                    if cfg.get("start_after_copy", True) else ""}


STEP = re.compile(r"==> step (\d+)/(\d+) (\S+)(?: \((\d+)B\))?\s*(.*)$")
TOTAL = re.compile(r"==> total (\d+) folders (\d+)B")
# What went wrong, in the words the log used, so the UI need not say "failed".
TROUBLE = (
    ("No such file or directory", "a source folder does not exist on the host"),
    ("No space left on device", "ran out of space on the volume"),
    ("Permission denied", "the source refused the credentials"),
    ("Host key verification failed", "the source host key was rejected"),
    ("Connection refused", "the source host refused the connection"),
    ("Connection timed out", "the source host did not answer"),
    ("No route to host", "the source host was unreachable"),
    ("failed to resolve", "the source host name could not be resolved"),
)
# rsync --info=progress2 redraws one line with \r: "  1,234,567  57%  11.83MB/s  0:00:04"
RSYNC = re.compile(r"([\d,]+)\s+(\d+)%\s+(\S+)\s+(\d+:\d\d:\d\d)")


def import_progress(log):
    """Read the copy job's output as one overall position, not a replayed bar.

    rsync restarts its percentage for every folder it copies, which is why the
    log looks like 0-100% several times over. Each folder is announced first,
    so the step counter carries the real progress and rsync's percentage only
    fills in the step that is running.
    """
    step, total, label, detail, percent, rate = 0, 0, "", "", 0, ""
    done_steps = 0
    sizes, done_bytes, total_bytes = {}, 0, 0
    error, error_detail = "", ""
    for line in str(log or "").replace("\r", "\n").splitlines():
        line = line.strip()
        header = TOTAL.match(line)
        if header:
            total, total_bytes = int(header.group(1)), int(header.group(2))
            continue
        match = STEP.match(line)
        if match:
            step, total = int(match.group(1)), int(match.group(2))
            label, rest = match.group(3), (match.group(5) or "").strip()
            if match.group(4):
                sizes[step] = int(match.group(4))
            if rest == "complete":
                done_steps, percent, rate = step, 100, ""
                done_bytes = sum(sizes.get(index, 0) for index in range(1, step + 1))
            else:
                detail = rest[2:].strip() if rest.startswith("::") else rest
                percent, rate = 0, ""
            continue
        if line.startswith("==> missing "):
            error = "a source folder does not exist on the host"
            error_detail = line[len("==> missing "):][:220]
            continue
        for needle, explanation in TROUBLE:
            if needle.lower() in line.lower():
                error, error_detail = explanation, line[:220]
                break
        if not error and line.startswith("rsync error:"):
            error, error_detail = "the copy failed", line[:220]
        moved = RSYNC.search(line)
        if moved:
            percent, rate = int(moved.group(2)), moved.group(3)
        elif line.startswith("==> done"):
            step = total = max(total, step)
            done_steps, percent = total, 100
    if not total:
        # A copy can fail before it announces a step - a source that is not
        # there, a host that will not answer - and that reason still matters.
        return {"error": error, "error_detail": error_detail, "percent": 0} if error else {}
    running = percent / 100 if done_steps < step else 0
    measured = total_bytes > 0
    if measured:
        # Folders differ wildly in size, so count bytes when the import knows
        # them: four folders are not four equal quarters of the copy.
        overall = (done_bytes + sizes.get(step, 0) * running) / total_bytes * 100
    else:
        overall = (done_steps + running) / total * 100
    return {"step": step, "steps": total, "folder": label, "detail": detail,
            "step_percent": percent, "percent": round(min(100.0, max(0.0, overall)), 1),
            "rate": rate, "weighted": measured, "total_bytes": total_bytes,
            "error": error, "error_detail": error_detail}


def import_cleanup_plan(name):
    """What an import job left behind, read from the job itself."""
    if not re.fullmatch(r"homestead-import-[a-z0-9][a-z0-9-]{0,60}", str(name or "")):
        raise ValueError("unknown import job")
    try:
        job = kget(f"/apis/batch/v1/namespaces/{NS}/jobs/{name}")
    except Exception:
        return {"job": name, "workload": "", "volume": "", "volume_created": False,
                "volumes": [], "namespace": NS, "known": False}
    meta = job.get("metadata", {}) or {}
    annotations = meta.get("annotations", {}) or {}
    app = NAMES.label_of(meta, "app")
    workload = annotations.get("homestead.io/import-workload", app)
    volume = annotations.get("homestead.io/import-volume", "")
    created = str(annotations.get("homestead.io/import-volume-created", "")).lower() == "true"
    exists = False
    if workload:
        try:
            kget(f"/apis/apps/v1/namespaces/{NS}/deployments/{workload}")
            exists = True
        except Exception:
            exists = False
    return {"job": name, "namespace": NS, "workload": workload if exists else "",
            "volume": volume, "volume_created": created,
            # An import can fill several volumes - appdata on one, recordings on
            # another - and cleaning it up has to offer all of them, not just the
            # first one it happened to record.
            "volumes": _plan_volumes(annotations, volume, created),
            # An import that predates this annotation says so rather than
            # implying the volume is safe to delete.
            "known": bool(annotations)}


def _names(annotations, key):
    return [part.strip() for part in str(annotations.get(key, "") or "").split(",") if part.strip()]


def _plan_volumes(annotations, volume, created):
    """Every claim this import used, and whether it is this import's to delete."""
    made = set(_names(annotations, "homestead.io/import-volumes-created"))
    used = _names(annotations, "homestead.io/import-volumes") or sorted(made)
    if not used and volume:
        # An import from before either list, which recorded one claim only.
        return [{"name": volume, "created": created}]
    return [{"name": claim, "created": claim in made} for claim in used]


def delete_import(name):
    """Remove an import Job, cancelling the copy if it is still running.

    A failed import leaves its Job behind, and while that Job exists it still
    references the appdata claim - which used to leave both the import and the
    volume it was filling unremovable from the UI.
    """
    if not re.fullmatch(r"homestead-import-[a-z0-9][a-z0-9-]{0,60}", str(name or "")):
        raise ValueError("unknown import job")
    try:
        ksend("DELETE", f"/apis/batch/v1/namespaces/{NS}/jobs/{name}"
                        "?propagationPolicy=Background")
    except urllib.error.HTTPError as error:
        if error.code != 404:
            raise
    stopped = _stop_job_pods(name)
    # A pre-pull started for this app outlives the import that wanted it: it is
    # a DaemonSet, so deleting its pods only makes it build new ones.
    app = re.sub(r"^homestead-import-", "", name)
    pulls = [f"{prefix}{app}" for prefix in PREPULL_NAMES]
    stopped_pulls = [pull for pull in pulls if _daemonset_exists(pull)]
    for pull in stopped_pulls:
        stop_prepull(pull)
    _bust("wl", "ov")
    return {"ok": True, "name": name, "pods_removed": stopped,
            "prepulls_removed": stopped_pulls,
            "message": f"Import {name} removed"}


def _daemonset_exists(name):
    try:
        found = kget(f"/apis/apps/v1/namespaces/{NS}/daemonsets/{name}")
    except Exception:
        return False
    return bool((found.get("metadata", {}) or {}).get("name"))


def wait_for_pods_gone(namespace, selector, seconds=20):
    """Wait for pods matching a selector to disappear, and say whether they did."""
    deadline = time.time() + max(0, seconds)
    while True:
        try:
            pods = kget(f"/api/v1/namespaces/{namespace}/pods"
                        f"?labelSelector={urllib.parse.quote(selector)}").get("items", [])
        except Exception:
            return True
        if not pods or time.time() >= deadline:
            return not pods
        time.sleep(1)


def _stop_job_pods(job, seconds=20):
    """Kill the copy pod now, and wait for it to actually be gone.

    Deleting the Job in the background hands the pod to the garbage collector
    and returns at once, so an rsync that was running kept running - holding
    the claims open, which left any volume deleted alongside it stuck
    Terminating behind the pvc-protection finaliser. Cancelling a copy means
    cancelling it, so the pod goes first and the claims are free afterwards.
    """
    removed = []
    try:
        pods = kget(f"/api/v1/namespaces/{NS}/pods?labelSelector=job-name%3D{job}").get("items", [])
    except Exception:
        return removed
    for pod in pods:
        pod_name = pod["metadata"]["name"]
        try:
            ksend("DELETE", f"/api/v1/namespaces/{NS}/pods/{pod_name}?gracePeriodSeconds=0")
            removed.append(pod_name)
        except urllib.error.HTTPError as error:
            if error.code != 404:
                raise
    deadline = time.time() + max(0, seconds)
    while removed and time.time() < deadline:
        try:
            left = kget(f"/api/v1/namespaces/{NS}/pods?labelSelector=job-name%3D{job}").get("items", [])
        except Exception:
            break
        if not left:
            break
        time.sleep(1)
    return removed


def _job_pod(job):
    try:
        pods = kget(f"/api/v1/namespaces/{NS}/pods?labelSelector=job-name%3D{job}").get("items", [])
    except Exception:
        return ""
    pods.sort(key=lambda pod: pod["metadata"].get("creationTimestamp", ""), reverse=True)
    return pods[0]["metadata"]["name"] if pods else ""


def import_status():
    try:
        jobs = [job for job in
                NAMES.find(f"/apis/batch/v1/namespaces/{NS}/jobs", "task")
                if NAMES.label_of(job["metadata"], "task") in ("import", "chown")]
    except Exception:
        return []
    out = []
    for j in jobs:
        st = j.get("status", {})
        state = ("running" if st.get("active") else "done" if st.get("succeeded")
                 else "failed" if st.get("failed") else "pending")
        row = {
            "name": j["metadata"]["name"],
            "kind": NAMES.label_of(j["metadata"], "task", "import"),
            "app": NAMES.label_of(j["metadata"], "app"),
            "active": st.get("active", 0), "succeeded": st.get("succeeded", 0),
            "failed": st.get("failed", 0),
            "start": st.get("startTime", ""), "end": st.get("completionTime", ""),
            "state": state,
        }
        if state in ("running", "failed"):
            pod = _job_pod(j["metadata"]["name"])
            if pod:
                try:
                    log = _pod_logs(pod)
                    row.update(import_progress(log) or {})
                    if state == "failed" and not row.get("error"):
                        tail = [line for line in str(log or "").splitlines() if line.strip()]
                        row["error_detail"] = tail[-1][:220] if tail else ""
                        row["error"] = "the copy failed"
                except Exception:
                    pass
        elif state == "done":
            row["percent"] = 100
        out.append(row)
    return sorted(out, key=lambda x: x["start"], reverse=True)


# --------------------------------------------------------------- image cache
def _canonical_image(ref):
    ref = re.sub(r"^(?:docker-pullable|docker)://", "", str(ref or "").strip())
    if not ref:
        return ""
    name = ref.split("@", 1)[0]
    slash, colon = name.rfind("/"), name.rfind(":")
    suffix = ref[len(name):] if "@" in ref else (name[colon:] if colon > slash else ":latest")
    if colon > slash and "@" not in ref:
        name = name[:colon]
    first = name.split("/", 1)[0]
    if not ("." in first or ":" in first or first == "localhost"):
        name = "docker.io/" + (("library/" + name) if "/" not in name else name)
    return name + suffix


def _image_digest(ref):
    match = re.search(r"sha256:[0-9a-f]{64}", str(ref or ""), re.I)
    return match.group(0).lower() if match else ""


def _system_image(ref):
    return bool(re.search(
        r"(^|/)(rancher|harvester|longhornio|kubevirt|cdi-|cilium|kube-|metrics-server|"
        r"registry\.k8s\.io|pause|traefik|fleet|system-upgrade|k8snetworkplumbingwg|"
        r"multus|whereabouts|kubeovn|calico|canal|flannel|coredns|etcd|rke2|neuvector|"
        r"suse/sles/)", str(ref or ""), re.I))


def image_retention_inventory():
    """Images Kubernetes still needs now or for the immediate managed rollback."""
    retained = []
    try:
        pods = kget("/api/v1/pods").get("items", [])
    except Exception:
        pods = []
    for pod in pods:
        meta, spec, status = pod.get("metadata", {}), pod.get("spec", {}), pod.get("status", {})
        if status.get("phase") in ("Succeeded", "Failed") or meta.get("deletionTimestamp"):
            continue
        statuses = {row.get("name"): row for row in
                    (status.get("initContainerStatuses", []) or []) +
                    (status.get("containerStatuses", []) or [])}
        owner = next(iter(meta.get("ownerReferences", []) or []), {})
        workload = (meta.get("labels", {}) or {}).get("app") or owner.get("name") or meta.get("name", "")
        for container in (spec.get("initContainers", []) or []) + (spec.get("containers", []) or []):
            ref = container.get("image", "")
            image_id = (statuses.get(container.get("name")) or {}).get("imageID", "")
            retained.append({
                "ref": _canonical_image(ref), "digest": _image_digest(image_id) or _image_digest(ref),
                "reason": "active", "namespace": meta.get("namespace", ""),
                "workload": workload, "container": container.get("name", ""),
            })
    try:
        deployments = kget("/apis/apps/v1/deployments").get("items", [])
    except Exception:
        deployments = []
    for dep in deployments:
        meta = dep.get("metadata", {})
        try:
            previous = json.loads(NAMES.annotation_of(meta, "update-previous", "{}"))
        except (TypeError, ValueError, json.JSONDecodeError):
            previous = {}
        for container, ref in (previous.get("images") or {}).items():
            retained.append({
                "ref": _canonical_image(ref), "digest": _image_digest(ref),
                "reason": "rollback", "namespace": meta.get("namespace", ""),
                "workload": meta.get("name", ""), "container": container,
                "retained_at": previous.get("at", ""),
            })
    # Deduplicate pods belonging to the same workload and digest while keeping
    # rollback and active reasons distinct.
    unique = {}
    for row in retained:
        key = (row["reason"], row["namespace"], row["workload"], row["container"],
               row["digest"] or row["ref"])
        unique[key] = row
    return list(unique.values())


def image_cache():
    """What container images each node already has on disk."""
    nodes = kget("/api/v1/nodes").get("items", [])
    retained = image_retention_inventory()
    per_node, totals = [], {}
    for n in nodes:
        name = n["metadata"]["name"]
        imgs = n["status"].get("images", []) or []
        rows = []
        for i in imgs:
            names = sorted(set(i.get("names") or ["<none>"]))
            digest_name = next((ref for ref in names if _image_digest(ref)), "")
            tag = next((ref for ref in names if not _image_digest(ref)), digest_name or "<none>")
            digest = _image_digest(digest_name)
            identity = digest or _canonical_image(tag) or tag
            sz = round((i.get("sizeBytes") or 0) / 1024**2, 1)
            rows.append({"name": tag, "names": names, "digest": digest, "size_mb": sz})
            totals[identity] = totals.get(identity, {"name": tag, "names": set(),
                                                       "digest": digest, "size_mb": sz, "nodes": []})
            totals[identity]["names"].update(names)
            totals[identity]["nodes"].append(name)
        rows.sort(key=lambda x: -x["size_mb"])
        per_node.append({"node": name, "count": len(rows),
                         "total_gb": round(sum(r["size_mb"] for r in rows) / 1024, 1),
                         "images": rows[:60]})
    shared = []
    for value in totals.values():
        aliases = {_canonical_image(ref) for ref in value["names"]}
        reasons = [row for row in retained if
                   (value["digest"] and row["digest"] == value["digest"]) or
                   (not row["digest"] and row["ref"] in aliases)]
        shared.append({"name": value["name"], "names": sorted(value["names"]),
                       "digest": value["digest"], "size_mb": value["size_mb"],
                       "nodes": sorted(set(value["nodes"])),
                       "system": _system_image(value["name"]),
                       "protected": bool(reasons), "retained_by": reasons})
    shared.sort(key=lambda x: -x["size_mb"])
    # Read here rather than on its own timer: whoever is looking at the image
    # cache is exactly who wants to know a pull is running, or has finished and
    # been cleared away.
    pulls = prepull_status()
    return {"nodes": per_node, "images": shared[:200],
            "pulls": pulls["pulls"], "pulls_finished": pulls["finished"],
            "distinct": len(shared),
            "node_names": [n["metadata"]["name"] for n in nodes],
            "retained": retained,
            "protected": sum(1 for image in shared if image["protected"])}


PREPULL_NAMES = ("homestead-pull-",)


def _pullable_nodes():
    """Nodes worth warming an image onto: Ready, and not cordoned.

    A DaemonSet cannot decline the tolerations its controller adds - not-ready,
    unreachable, unschedulable and every pressure taint - so the only way to
    keep a pull off a node being drained or already falling over is to leave
    that node out of the set the pods are allowed to land on.
    """
    healthy, all_names = [], []
    for node in kget("/api/v1/nodes").get("items", []):
        name = (node.get("metadata", {}) or {}).get("name", "")
        if not name:
            continue
        all_names.append(name)
        conditions = (node.get("status", {}) or {}).get("conditions", []) or []
        ready = any(c.get("type") == "Ready" and c.get("status") == "True" for c in conditions)
        if ready and not (node.get("spec", {}) or {}).get("unschedulable"):
            healthy.append(name)
    return healthy, all_names


def prepull(image, nodes=None):
    """Warm an image onto every healthy node with a DaemonSet-style pull."""
    tag = re.sub(r"[^a-z0-9-]", "-", image.split("/")[-1].split(":")[0].lower())[:30]
    name = f"homestead-pull-{tag}"
    healthy, every = _pullable_nodes()
    wanted = [node for node in (nodes or every) if node in healthy]
    skipped = sorted(set(nodes or every) - set(wanted))
    if not wanted:
        raise ValueError(f"{', '.join(skipped)} is not ready to take an image pull"
                         if skipped else "no node is ready to take an image pull")
    body = {
        "apiVersion": "apps/v1", "kind": "DaemonSet",
        "metadata": {"name": name, "namespace": NS,
                     "labels": NAMES.labels("prepull", image=tag)},
        "spec": {"selector": {"matchLabels": {"app": name}},
                 "template": {"metadata": {"labels": {"app": name}},
                              "spec": {"terminationGracePeriodSeconds": 1,
                                       "initContainers": [{"name": "pull", "image": image,
                                                           "command": ["/bin/true"]}],
                                       "containers": [{"name": "pause",
                                                       "image": "registry.k8s.io/pause:3.9",
                                                       "resources": {"requests": {"cpu": "1m", "memory": "4Mi"}}}]}}},
    }
    # Always pinned, never left to the DaemonSet's own idea of every node.
    body["spec"]["template"]["spec"]["affinity"] = {"nodeAffinity": {
        "requiredDuringSchedulingIgnoredDuringExecution": {"nodeSelectorTerms": [
            {"matchExpressions": [{"key": "kubernetes.io/hostname",
                                   "operator": "In", "values": sorted(wanted)}]}]}}}
    for previous in (name,):
        try:
            ksend("DELETE", f"/apis/apps/v1/namespaces/{NS}/daemonsets/{previous}")
        except urllib.error.HTTPError:
            pass
    time.sleep(1)
    ksend("POST", f"/apis/apps/v1/namespaces/{NS}/daemonsets", body)
    return {"ok": True, "daemonset": name, "image": image, "nodes": sorted(wanted),
            "skipped": skipped,
            "message": f"Pulling onto {len(wanted)} node{'' if len(wanted) == 1 else 's'}"
                       + (f"; skipped {', '.join(skipped)} (cordoned or not ready)" if skipped else "")}


def prepull_status(sweep=True):
    """Every pull still running, and the finished ones cleared away.

    A DaemonSet has no notion of being done: once the init container has pulled
    the image its pod sits there holding a pause container open for good. So
    completion is read from the outside - every pod it wants is ready - and the
    DaemonSet is deleted rather than left running on every node forever.
    """
    try:
        sets = NAMES.find(f"/apis/apps/v1/namespaces/{NS}/daemonsets", "task", "prepull")
    except Exception:
        return {"pulls": [], "finished": []}
    pulls, finished = [], []
    for item in sets:
        name = item["metadata"]["name"]
        status = item.get("status", {}) or {}
        desired = int(status.get("desiredNumberScheduled", 0) or 0)
        ready = int(status.get("numberReady", 0) or 0)
        image = ""
        for container in (item["spec"]["template"]["spec"].get("initContainers") or []):
            image = container.get("image", "") or image
        row = {"name": name, "image": image, "desired": desired, "ready": ready,
               "complete": bool(desired) and ready >= desired}
        if row["complete"] and sweep:
            stop_prepull(name)
            finished.append(row)
        else:
            pulls.append(row)
    return {"pulls": pulls, "finished": finished}


def stop_prepull(name):
    """Delete a pull DaemonSet. Deleting its pods only makes it replace them."""
    name = str(name or "")
    if not any(name.startswith(prefix) for prefix in PREPULL_NAMES) or "/" in name:
        raise ValueError("that is not an image pre-pull")
    try:
        ksend("DELETE", f"/apis/apps/v1/namespaces/{NS}/daemonsets/{name}"
                        "?propagationPolicy=Background")
    except urllib.error.HTTPError as error:
        if error.code != 404:
            raise
    return {"ok": True, "daemonset": name, "message": f"Pre-pull {name} stopped"}


def cleanup_image(digest, nodes=None):
    """Start one tightly scoped CRI removal pod per selected cache node."""
    digest = str(digest or "").lower()
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", digest):
        raise ValueError("cleanup requires an exact sha256 image digest")
    report = image_cache()
    image = next((row for row in report["images"] if row.get("digest") == digest), None)
    if not image:
        raise ValueError("that digest is not present in the node image cache")
    if image.get("protected"):
        owners = sorted({f"{row['reason']}:{row['namespace']}/{row['workload']}"
                         for row in image.get("retained_by", [])})
        raise PermissionError("image is protected by " + ", ".join(owners))
    if image.get("system"):
        raise PermissionError("Harvester and Kubernetes platform images cannot be cleaned from Homestead")
    selected = sorted(set(nodes or image["nodes"]))
    if not selected or any(node not in image["nodes"] for node in selected):
        raise ValueError("cleanup nodes must currently cache this digest")
    image_ref = next((ref for ref in image.get("names", []) if _image_digest(ref) == digest), "")
    if not image_ref:
        raise ValueError("the cache did not report a removable repository digest")
    pods = []
    socket = "unix:///host/run/k3s/containerd/containerd.sock"
    crictl = f"/usr/local/bin/crictl --runtime-endpoint {socket} --image-endpoint {socket}"
    quoted = "'" + image_ref.replace("'", "") + "'"
    # Exited containers of pods long gone still hold the image, and rmi refuses
    # while any does. Only exited ones are removed - never a running container.
    script = (f"set -e; for id in $({crictl} ps -a -q --state exited --image {quoted}); "
              f"do {crictl} rm \"$id\" >/dev/null; done; {crictl} rmi {quoted}")
    attempt = format(int(time.time()), "x")[-6:]
    for node in selected:
        suffix = hashlib.sha256((digest + "|" + node).encode()).hexdigest()[:10]
        # A name of its own per attempt: a retry used to delete the last pod and
        # recreate it at once, and Kubernetes had not finished deleting it.
        pod_name = f"homestead-image-clean-{suffix}-{attempt}"
        body = {
            "apiVersion": "v1", "kind": "Pod",
            "metadata": {"name": pod_name, "namespace": NS,
                         "labels": dict({"app": "homestead-image-cleaner"}, **NAMES.labels("image-cleanup")),
                         "annotations": {NAMES.key("image-digest"): digest,
                                         NAMES.key("cache-node"): node}},
            "spec": {"nodeName": node, "restartPolicy": "Never",
                     "terminationGracePeriodSeconds": 1,
                     "tolerations": [{"operator": "Exists"}],
                     "containers": [{
                         "name": "cleanup", "image": "python:3.12-alpine",
                         "command": ["sh", "-c", script],
                         # crictl's own words become the failure the job tray shows.
                         "terminationMessagePolicy": "FallbackToLogsOnError",
                         "resources": {"requests": {"cpu": "5m", "memory": "16Mi"},
                                       "limits": {"memory": "48Mi"}},
                         "securityContext": {"runAsUser": 0, "runAsGroup": 0,
                                             "runAsNonRoot": False,
                                             "allowPrivilegeEscalation": False,
                                             "readOnlyRootFilesystem": True,
                                             "capabilities": {"drop": ["ALL"]}},
                         "volumeMounts": [
                             {"name": "crictl", "mountPath": "/usr/local/bin/crictl", "readOnly": True},
                             {"name": "runtime", "mountPath": "/host/run/k3s/containerd", "readOnly": True},
                         ],
                     }],
                     "volumes": [
                         {"name": "crictl", "hostPath": {
                             "path": "/var/lib/rancher/rke2/bin/crictl", "type": "File"}},
                         {"name": "runtime", "hostPath": {
                             "path": "/run/k3s/containerd", "type": "Directory"}},
                     ]},
        }
        for old in NAMES.find(f"/api/v1/namespaces/{NS}/pods", "task", "image-cleanup"):
            meta = old.get("metadata") or {}
            if meta.get("name", "").startswith(f"homestead-image-clean-{suffix}") and                     (old.get("status") or {}).get("phase") in ("Succeeded", "Failed"):
                try:
                    ksend("DELETE", f"/api/v1/namespaces/{NS}/pods/{meta['name']}")
                except urllib.error.HTTPError:
                    pass
        ksend("POST", f"/api/v1/namespaces/{NS}/pods", body)
        pods.append(pod_name)
    _bust("imgcache")
    return {"ok": True, "digest": digest, "image": image_ref,
            "nodes": selected, "pods": pods}


# --------------------------------------------------------------- schedules
def list_jobs():
    try:
        cjs = kget(f"/apis/batch/v1/namespaces/{NS}/cronjobs").get("items", [])
    except Exception:
        return []
    out = []
    for c in cjs:
        sp, st = c["spec"], c.get("status", {})
        cont = sp["jobTemplate"]["spec"]["template"]["spec"]["containers"][0]
        out.append({
            "name": c["metadata"]["name"],
            "schedule": sp.get("schedule", ""),
            "suspend": bool(sp.get("suspend")),
            "image": cont.get("image", ""),
            "command": " ".join(cont.get("command", [])[-1:]) if cont.get("command") else "",
            "last": st.get("lastScheduleTime", ""),
            "active": len(st.get("active", []) or []),
        })
    return sorted(out, key=lambda x: x["name"])


def save_job(cfg):
    name = cfg["name"]
    if not SAFE.match(name):
        raise ValueError("name must be lowercase letters, numbers and dashes")
    body = {
        "apiVersion": "batch/v1", "kind": "CronJob",
        "metadata": {"name": name, "namespace": NS, "labels": {NAMES.key("managed"): "true"}},
        "spec": {"schedule": cfg["schedule"], "suspend": bool(cfg.get("suspend")),
                 "concurrencyPolicy": "Forbid",
                 "successfulJobsHistoryLimit": 3, "failedJobsHistoryLimit": 3,
                 "jobTemplate": {"spec": {"backoffLimit": 1, "ttlSecondsAfterFinished": 86400,
                     "template": {"spec": {"restartPolicy": "Never", "containers": [{
                         "name": "task", "image": cfg.get("image", "alpine:3.20"),
                         "command": ["sh", "-c", cfg["command"]],
                     }]}}}}},
    }
    try:
        kget(f"/apis/batch/v1/namespaces/{NS}/cronjobs/{name}")
        return ksend("PUT", f"/apis/batch/v1/namespaces/{NS}/cronjobs/{name}", body)
    except urllib.error.HTTPError as e:
        if e.code != 404:
            raise
        return ksend("POST", f"/apis/batch/v1/namespaces/{NS}/cronjobs", body)


def del_job(name):
    ksend("DELETE", f"/apis/batch/v1/namespaces/{NS}/cronjobs/{name}?propagationPolicy=Background")
    return {"ok": True}


def run_job_now(name):
    cj = kget(f"/apis/batch/v1/namespaces/{NS}/cronjobs/{name}")
    body = {"apiVersion": "batch/v1", "kind": "Job",
            "metadata": {"name": f"{name}-{int(time.time()) % 1000000}", "namespace": NS,
                         "labels": {NAMES.key("managed"): "true", NAMES.key("from"): name}},
            "spec": cj["spec"]["jobTemplate"]["spec"]}
    return ksend("POST", f"/apis/batch/v1/namespaces/{NS}/jobs", body)


# --------------------------------------------------------------- VMs
VM_DISK_LABEL = "homestead.io/vm-disk-import"


def _required_name(value, label="name"):
    value = str(value or "").strip()
    if not SAFE.match(value):
        raise ValueError(f"{label} must be lowercase letters, numbers and dashes")
    return value


def _get_or_none(path):
    try:
        return kget(path)
    except urllib.error.HTTPError as error:
        if error.code == 404:
            return None
        raise


def vm_disk_import_plan(namespace, name):
    """Preflight an import without returning or persisting its source URL."""
    namespace = _required_name(namespace or NS, "namespace")
    name = _required_name(name, "disk name")
    dv = _get_or_none(
        f"/apis/cdi.kubevirt.io/v1beta1/namespaces/{namespace}/datavolumes/{name}")
    pvc = _get_or_none(f"/api/v1/namespaces/{namespace}/persistentvolumeclaims/{name}")
    conflicts = []
    if dv:
        conflicts.append({"kind": "DataVolume", "name": name})
    if pvc:
        conflicts.append({"kind": "PersistentVolumeClaim", "name": name})
    return {"namespace": namespace, "name": name, "ready": not conflicts,
            "conflicts": conflicts,
            "message": ("Ready to create a new CDI DataVolume and PVC" if not conflicts else
                        f"{namespace}/{name} already exists; choose a new disk name")}


def _http_disk_source(cfg):
    source_url = str(cfg.get("source_url") or "").strip()
    try:
        parsed = urllib.parse.urlsplit(source_url)
    except ValueError as error:
        raise ValueError("source URL is invalid") from error
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise ValueError("source URL must use HTTP or HTTPS")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("put HTTP credentials in a Kubernetes Secret, not in the URL")
    source = {"url": source_url}
    checksum = str(cfg.get("checksum") or "").strip().lower()
    if checksum:
        match = re.fullmatch(r"(sha256|sha512):([0-9a-f]+)", checksum)
        if not match or len(match.group(2)) != {"sha256": 64, "sha512": 128}[match.group(1)]:
            raise ValueError("checksum must be sha256:<64 hex characters> or sha512:<128 hex characters>")
        source["checksum"] = checksum
    secret = str(cfg.get("secret_ref") or "").strip()
    cert = str(cfg.get("cert_config_map") or "").strip()
    if secret:
        source["secretRef"] = _required_name(secret, "secret reference")
    if cert:
        source["certConfigMap"] = _required_name(cert, "CA ConfigMap")
    return source


def import_vm_disk(cfg):
    """Import a qemu-supported disk image into a new PVC through CDI."""
    namespace = _required_name(cfg.get("namespace") or NS, "namespace")
    name = _required_name(cfg.get("name"), "disk name")
    plan = vm_disk_import_plan(namespace, name)
    if not plan["ready"]:
        raise ValueError(plan["message"])
    try:
        size_gb = int(cfg.get("size_gb", 20))
    except (TypeError, ValueError) as error:
        raise ValueError("capacity must be a whole number of GiB") from error
    if not 1 <= size_gb <= 16384:
        raise ValueError("capacity must be between 1 and 16384 GiB")
    access_mode = str(cfg.get("access_mode") or "ReadWriteOnce")
    if access_mode not in ("ReadWriteOnce", "ReadWriteMany"):
        raise ValueError("access mode must be ReadWriteOnce or ReadWriteMany")
    storage_class = _required_name(cfg.get("storage_class") or "longhorn-r2", "storage class")
    source = _http_disk_source(cfg)
    body = {
        "apiVersion": "cdi.kubevirt.io/v1beta1", "kind": "DataVolume",
        "metadata": {
            "name": name, "namespace": namespace,
            "labels": {NAMES.key("managed"): "true", VM_DISK_LABEL: "true"},
            "annotations": {"homestead.io/import-source": "http",
                            "homestead.io/disk-format": "auto-detected"},
        },
        "spec": {
            "source": {"http": source}, "contentType": "kubevirt",
            "storage": {
                "storageClassName": storage_class,
                "accessModes": [access_mode], "volumeMode": "Filesystem",
                "resources": {"requests": {"storage": f"{size_gb}Gi"}},
            },
        },
    }
    ksend("POST", f"/apis/cdi.kubevirt.io/v1beta1/namespaces/{namespace}/datavolumes", body)
    _bust("vms", "vol")
    return {"ok": True, "namespace": namespace, "name": name, "pvc": name,
            "size_gb": size_gb,
            "message": f"CDI import into {namespace}/{name} started"}


def _disk_consumers():
    consumers = {}
    try:
        vms = kget("/apis/kubevirt.io/v1/virtualmachines").get("items", [])
    except Exception:
        return consumers
    for vm in vms:
        meta = vm.get("metadata", {}) or {}
        ns, vm_name = meta.get("namespace", ""), meta.get("name", "")
        for volume in (((vm.get("spec", {}) or {}).get("template", {}) or {})
                       .get("spec", {}).get("volumes", []) or []):
            disk_name = (volume.get("dataVolume") or {}).get("name")
            if disk_name:
                consumers.setdefault((ns, disk_name), []).append(vm_name)
    return consumers


def list_vm_disks():
    """Return managed imports with status only; source URLs remain cluster-private."""
    try:
        items = kget("/apis/cdi.kubevirt.io/v1beta1/datavolumes").get("items", [])
    except Exception:
        return []
    consumers = _disk_consumers()
    out = []
    for item in items:
        meta = item.get("metadata", {}) or {}
        if (meta.get("labels", {}) or {}).get(VM_DISK_LABEL) != "true":
            continue
        spec, status = item.get("spec", {}) or {}, item.get("status", {}) or {}
        ns, name = meta.get("namespace", ""), meta.get("name", "")
        progress = str(status.get("progress", "") or "")
        amount = float(progress.rstrip("%") or 0) if re.fullmatch(r"\d+(?:\.\d+)?%?", progress) else 0
        request = ((spec.get("storage", {}) or {}).get("resources", {}) or {}).get("requests", {}) or {}
        messages = [c.get("message") or c.get("reason") for c in status.get("conditions", []) or []
                    if c.get("status") == "False" and (c.get("message") or c.get("reason"))]
        used_by = consumers.get((ns, name), [])
        out.append({"namespace": ns, "name": name, "pvc": status.get("claimName") or name,
                    "phase": status.get("phase") or "Pending", "progress": round(amount, 1),
                    "capacity": request.get("storage", ""),
                    "storage_class": (spec.get("storage", {}) or {}).get("storageClassName", ""),
                    "access_modes": (spec.get("storage", {}) or {}).get("accessModes", []),
                    "message": str(messages[0])[:300] if messages else "",
                    "in_use": bool(used_by), "used_by": used_by})
    return sorted(out, key=lambda row: (row["namespace"], row["name"]))


def list_vm_images():
    """Harvester's images. Each is a Longhorn backing image with a storage
    class of its own; a disk made on that class starts as a copy of it."""
    try:
        rows = []
        for i in kget("/apis/harvesterhci.io/v1beta1/virtualmachineimages").get("items", []):
            status = i.get("status", {}) or {}
            imported = next((c for c in status.get("conditions") or [] if c.get("type") == "Imported"), {})
            rows.append({"name": i["metadata"]["name"], "namespace": i["metadata"].get("namespace", ""),
                         "display": i["spec"].get("displayName", i["metadata"]["name"]),
                         "size_gb": round(int(status.get("size", 0) or 0) / 1024**3, 1),
                         "storage_class": status.get("storageClassName", ""),
                         "progress": status.get("progress", 0),
                         "ready": imported.get("status") == "True",
                         "failed": imported.get("status") == "False" and imported.get("reason") == "ImportFailed",
                         "message": " ".join(str(imported.get("message") or "").split())[:240]})
        return rows
    except Exception:
        return []


def _harvester_image(ref):
    """namespace/name of a Harvester image -> the image, or a clear refusal."""
    ns, _, image = str(ref).rpartition("/")
    for item in list_vm_images():
        if item["name"] == image and (not ns or item["namespace"] == ns):
            if not item["storage_class"]:
                raise ValueError(f"image {item['display']} is not ready yet")
            return item
    raise ValueError(f"image {ref} was not found")


def create_vm(cfg, platform=None, default_class=""):
    """Create a KubeVirt VM with its boot disk, the way this cluster makes disks.

    Harvester makes a VM's disks from its harvesterhci.io/volumeClaimTemplates
    annotation - block volumes every node can reach, so the VM can live-migrate,
    and a disk from an image is a claim on that image's own storage class. That
    is how its UI does it, so its pages and delete-with-disks treat these VMs
    as their own. Elsewhere, CDI fills a DataVolume and picks the access and
    volume modes its storage profile says the class supports - local-path
    cannot give ReadWriteMany - and with no CDI a blank disk is a plain claim,
    which KubeVirt formats itself. Downloading an image needs CDI either way.
    """
    platform = platform or {}
    harvester = bool(platform.get("harvester"))
    cdi = platform.get("cdi", True)
    name = _required_name(cfg.get("name"))
    ns = _required_name(cfg.get("namespace") or NS, "namespace")
    cores = int(cfg.get("cores", 2))
    mem = cfg.get("memory", "2Gi")
    disk = int(cfg.get("disk_gb", 20))
    sc = str(cfg.get("storage_class") or default_class or "").strip()
    imported_dv = str(cfg.get("disk_import") or "").strip()
    image_ref = str(cfg.get("image_id") or "").strip()
    image_url = str(cfg.get("image_url") or "").strip()
    dv = imported_dv or f"{name}-disk"
    password = str(cfg.get("password") or "")
    if not imported_dv and not cfg.get("cloud_init") and len(password) < 10:
        raise ValueError("root password must be at least 10 characters")
    if image_url:
        parsed = urllib.parse.urlparse(image_url)
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            raise ValueError(f"{image_url} is not a download address; give the full http(s):// URL"
                             + (", or pick the Harvester image from the list" if harvester else ""))
    if image_ref and not harvester:
        raise ValueError("images from the image list are Harvester's; use an image URL on this cluster")
    # On Harvester a URL becomes one of its images, which needs no CDI.
    if (imported_dv or (image_url and not harvester)) and not cdi:
        raise ValueError("CDI (the containerized data importer) is not installed, so disk images cannot be "
                         "downloaded or imported; install it from kubevirt.io, or start from a blank disk")

    if imported_dv:
        _required_name(imported_dv, "imported disk")
        disk_obj = _get_or_none(
            f"/apis/cdi.kubevirt.io/v1beta1/namespaces/{ns}/datavolumes/{imported_dv}")
        if not disk_obj:
            raise ValueError(f"imported disk {ns}/{imported_dv} does not exist")
        if (disk_obj.get("status", {}) or {}).get("phase") != "Succeeded":
            raise ValueError("imported disk is not ready yet")
        users = _disk_consumers().get((ns, imported_dv), [])
        if users:
            raise ValueError(f"imported disk is already attached to VM {users[0]}")

    cloudinit = cfg.get("cloud_init") or (password and (
        "#cloud-config\n"
        f"hostname: {name}\n"
        "ssh_pwauth: true\n"
        f"password: {password}\n"
        "chpasswd: {expire: false}\n"))

    annotations, templates = {}, []
    size = f"{disk}Gi"
    if imported_dv:
        root = {"name": "root", "dataVolume": {"name": dv}}
    elif harvester:
        claim = {"metadata": {"name": dv, "annotations": {}},
                 "spec": {"accessModes": ["ReadWriteMany"], "volumeMode": "Block",
                          "resources": {"requests": {"storage": size}}}}
        if image_url:
            # Harvester downloads it as one of its images, and the disk starts
            # as a copy of that - CDI's importer cannot be given its volumes.
            image = HVIMAGE.download(kget, ksend, ns, image_url, sc)
            claim["metadata"]["annotations"]["harvesterhci.io/imageId"] = f"{image['namespace']}/{image['name']}"
            claim["spec"]["storageClassName"] = image["storage_class"]
        elif image_ref:
            image = _harvester_image(image_ref)
            claim["metadata"]["annotations"]["harvesterhci.io/imageId"] = f"{image['namespace']}/{image['name']}"
            claim["spec"]["storageClassName"] = image["storage_class"]
            if image["size_gb"] and disk < image["size_gb"]:
                raise ValueError(f"the disk must be at least as big as the image ({image['size_gb']} GB)")
        elif sc:
            claim["spec"]["storageClassName"] = sc
        annotations["harvesterhci.io/volumeClaimTemplates"] = json.dumps([claim])
        root = {"name": "root", "persistentVolumeClaim": {"claimName": dv}}
    elif cdi:
        storage = {"resources": {"requests": {"storage": size}}}
        if sc:
            storage["storageClassName"] = sc
        if harvester:
            # A downloaded image on Harvester still gets a disk that can migrate.
            storage.update(accessModes=["ReadWriteMany"], volumeMode="Block")
        templates = [{"metadata": {"name": dv},
                      "spec": {"source": {"http": {"url": image_url}} if image_url else {"blank": {}},
                               "storage": storage}}]
        root = {"name": "root", "dataVolume": {"name": dv}}
    else:
        claim = {"apiVersion": "v1", "kind": "PersistentVolumeClaim",
                 "metadata": {"name": dv, "namespace": ns, "labels": {NAMES.key("managed"): "true", "app": name}},
                 "spec": {"accessModes": ["ReadWriteOnce"], "resources": {"requests": {"storage": size}}}}
        if sc:
            claim["spec"]["storageClassName"] = sc
        if _get_or_none(f"/api/v1/namespaces/{ns}/persistentvolumeclaims/{dv}"):
            raise ValueError(f"a volume named {dv} already exists")
        ksend("POST", f"/api/v1/namespaces/{ns}/persistentvolumeclaims", claim)
        root = {"name": "root", "persistentVolumeClaim": {"claimName": dv}}

    disks = [{"name": "root", "disk": {"bus": "virtio"}, "bootOrder": 1}]
    volumes = [root]
    if cloudinit:
        disks.append({"name": "cloudinit", "disk": {"bus": "virtio"}})
        volumes.append({"name": "cloudinit", "cloudInitNoCloud": {"userData": cloudinit}})

    template_spec = {
        "domain": {
            "cpu": {"cores": cores},
            "memory": {"guest": mem},
            "resources": {"requests": {"memory": mem}},
            "devices": {
                "disks": disks,
                "interfaces": [{"name": "default", "masquerade": {}}],
            },
        },
        "networks": [{"name": "default", "pod": {}}],
        "volumes": volumes,
    }
    if harvester:
        # Harvester's disks are shared block volumes, so a node drain can move
        # the VM. Elsewhere the cluster's own default applies: asking to
        # live-migrate a VM on a local disk only blocks the drain.
        template_spec["evictionStrategy"] = "LiveMigrate"
    vm = {
        "apiVersion": "kubevirt.io/v1", "kind": "VirtualMachine",
        "metadata": {"name": name, "namespace": ns,
                     "labels": {NAMES.key("managed"): "true", "app": name},
                     **({"annotations": annotations} if annotations else {})},
        "spec": {
            # spec.running is deprecated; a run strategy is what KubeVirt and
            # Harvester both read, and what start and stop change.
            "runStrategy": "RerunOnFailure" if cfg.get("start", True) else "Halted",
            **({"dataVolumeTemplates": templates} if templates else {}),
            "template": {
                "metadata": {"labels": {"kubevirt.io/domain": name, "app": name}},
                "spec": template_spec,
            },
        },
    }
    try:
        ksend("POST", f"/apis/kubevirt.io/v1/namespaces/{ns}/virtualmachines", vm)
    except urllib.error.HTTPError as error:
        if harvester and image_url:
            try:
                why = json.loads(error.read().decode("utf-8", "replace")).get("message", "")
            except Exception:
                why = f"HTTP {error.code}"
            raise ValueError(f"Harvester would not take the VM yet ({why}). Its image keeps downloading: "
                             "choose it under Boot disk once it is ready.") from error
        raise
    _bust("flow", "ov")
    return {"ok": True, "vm": name, "datavolume": dv}
