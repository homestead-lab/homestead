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

kget = ksend = create_pvc = build_deployment = None
NS = "lab"
_cache = {}
hardware_features = lambda: []


def bind(_kget, _ksend, _create_pvc, _build_dep, _ns, _cache_ref, _hardware_features=None):
    global kget, ksend, create_pvc, build_deployment, NS, _cache, hardware_features
    kget, ksend, create_pvc, build_deployment = _kget, _ksend, _create_pvc, _build_dep
    NS, _cache = _ns, _cache_ref
    if _hardware_features:
        hardware_features = _hardware_features


def _bust(*keys):
    for k in list(_cache):
        if not keys or any(k.startswith(x) for x in keys):
            _cache.pop(k, None)


SAFE = re.compile(r"^[a-z0-9]([a-z0-9-]{0,38}[a-z0-9])?$")


# --------------------------------------------------------------- sources
def list_sources():
    try:
        cm = kget(f"/api/v1/namespaces/{NS}/configmaps/harvui-sources")
        return json.loads(cm.get("data", {}).get("sources.json", "[]"))
    except Exception:
        return []


def save_sources(srcs):
    body = {"apiVersion": "v1", "kind": "ConfigMap",
            "metadata": {"name": "harvui-sources", "namespace": NS},
            "data": {"sources.json": json.dumps(srcs, indent=2)}}
    try:
        kget(f"/api/v1/namespaces/{NS}/configmaps/harvui-sources")
        return ksend("PUT", f"/api/v1/namespaces/{NS}/configmaps/harvui-sources", body)
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
           "metadata": {"name": f"harvui-src-{name}", "namespace": NS},
           "stringData": {"password": password or ""}}
    try:
        kget(f"/api/v1/namespaces/{NS}/secrets/harvui-src-{name}")
        ksend("PUT", f"/api/v1/namespaces/{NS}/secrets/harvui-src-{name}", sec)
    except urllib.error.HTTPError as e:
        if e.code != 404:
            raise
        ksend("POST", f"/api/v1/namespaces/{NS}/secrets", sec)
    return srcs


def del_source(name):
    srcs = [s for s in list_sources() if s["name"] != name]
    save_sources(srcs)
    try:
        ksend("DELETE", f"/api/v1/namespaces/{NS}/secrets/harvui-src-{name}")
    except urllib.error.HTTPError:
        pass
    return srcs


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
               "type": m.get("Type", "")}
              for m in item.get("Mounts", []) or [] if m.get("Destination")]
    app_mount = next((m for m in mounts if m["source"].startswith(src.get("base_path", "/mnt/user/appdata"))), None)
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
        "name": (item.get("Name") or container).lstrip("/"),
        "image": config.get("Image", ""),
        "icon": labels.get("net.unraid.docker.icon", "") or labels.get("harvui.icon", ""),
        "webui": labels.get("net.unraid.docker.webui", ""),
        "env": env, "ports": ports, "mounts": mounts,
        "remote_path": app_mount["source"] if app_mount else src.get("base_path", "/mnt/user/appdata") + "/" + container,
        "mount_path": app_mount["path"] if app_mount else "/config",
        "network_mode": host.get("NetworkMode", "bridge"), "hardware": hardware,
    }


def run_probe(tag, script, src, timeout=70):
    """Run a one-shot pod, wait for it, return its stdout."""
    pod = f"harvui-probe-{re.sub(r'[^a-z0-9-]', '-', tag)[:30]}-{int(time.time()) % 100000}"
    body = {
        "apiVersion": "v1", "kind": "Pod",
        "metadata": {"name": pod, "namespace": NS, "labels": {"harvui.io/task": "probe"}},
        "spec": {"restartPolicy": "Never", "terminationGracePeriodSeconds": 1,
                 "containers": [{
                     "name": "probe", "image": "alpine:3.20",
                     "command": ["sh", "-c",
                                 "apk add --no-cache openssh-client sshpass >/dev/null 2>&1; " + script],
                     "env": [{"name": "SRC_PASS", "valueFrom": {"secretKeyRef": {
                         "name": f"harvui-src-{src['name']}", "key": "password"}}}],
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
    from harvui_shim import raw_get  # provided by server.py
    return raw_get(f"/api/v1/namespaces/{NS}/pods/{pod}/log?tailLines=400")


# --------------------------------------------------------------- import job
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


def import_mappings(cfg):
    """Every remote directory this import copies, and where it lands.

    An Unraid container usually maps several folders under appdata. They belong
    in one volume per container, each copied into its own subdirectory and
    mounted back at the path the container expects through subPath, rather than
    a volume per mapping.
    """
    rows, used, paths = [], set(), set()
    requested = cfg.get("mappings")
    if not requested:
        # The original single-folder shape, kept so older clients still work.
        requested = [{"remote_path": cfg.get("remote_path"),
                      "mount_path": cfg.get("mount_path", "/config"), "folder": ""}]
    single = len(requested) == 1
    for item in requested:
        remote = str(item.get("remote_path") or "").strip().rstrip("/")
        mount = str(item.get("mount_path") or "").strip().rstrip("/") or "/config"
        if not remote.startswith("/"):
            raise ValueError(f"remote path must be absolute, got {remote or '(blank)'}")
        if not mount.startswith("/"):
            raise ValueError(f"container path must be absolute, got {mount}")
        if mount in paths:
            raise ValueError(f"{mount} is mapped twice")
        paths.add(mount)
        folder = str(item.get("folder") or "").strip("/")
        if folder:
            if not re.fullmatch(r"[A-Za-z0-9._-]+(?:/[A-Za-z0-9._-]+)*", folder):
                raise ValueError(f"folder {folder} must be a plain relative path")
            if ".." in folder.split("/"):
                raise ValueError("folder cannot climb out of the volume")
            used.add(folder)
        elif not single:
            folder = _folder_name(remote or mount, used)
        rows.append({"remote_path": remote, "mount_path": mount, "folder": folder})
    return rows


def import_container(cfg):
    """Create the PVC, launch the copy Job, then create the Deployment.

    cfg: {source, remote_path, name, image, ports, env, mount_path, pvc_name,
          size_gb, storage_class, access_mode, reuse_existing, start_after_copy}
    """
    name = cfg["name"]
    if not SAFE.match(name):
        raise ValueError("name must be lowercase letters, numbers and dashes")
    src = _source(cfg["source"])
    pvc = str(cfg.get("pvc_name") or f"{name}-appdata").strip()
    if not re.fullmatch(r"[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?", pvc):
        raise ValueError("PVC name must be lowercase letters, numbers and dashes")
    size = int(cfg.get("size_gb", 10))
    if not 1 <= size <= 16384:
        raise ValueError("volume size must be between 1 and 16384 GiB")
    access_mode = str(cfg.get("access_mode") or "ReadWriteOnce")
    if access_mode not in ("ReadWriteOnce", "ReadWriteMany"):
        raise ValueError("access mode must be ReadWriteOnce or ReadWriteMany")
    storage_class = str(cfg.get("storage_class") or "longhorn-r2").strip()
    if not re.fullmatch(r"[a-z0-9]([a-z0-9.-]{0,251}[a-z0-9])?", storage_class):
        raise ValueError("storage class must use lowercase letters, numbers, dots and dashes")
    if cfg.get("reuse_existing"):
        try:
            existing = kget(f"/api/v1/namespaces/{NS}/persistentvolumeclaims/{pvc}")
        except urllib.error.HTTPError as error:
            if error.code == 404:
                raise ValueError(f"existing PVC {pvc} was not found") from error
            raise
        existing_modes = (existing.get("spec", {}) or {}).get("accessModes", []) or []
        access_mode = existing_modes[0] if existing_modes else access_mode
        storage_class = (existing.get("spec", {}) or {}).get("storageClassName", storage_class)
    else:
        create_pvc(NS, pvc, size, storage_class, access_mode)

    mappings = import_mappings(cfg)
    job = f"harvui-import-{name}"
    try:
        ksend("DELETE", f"/apis/batch/v1/namespaces/{NS}/jobs/{job}?propagationPolicy=Background")
        time.sleep(1)
    except urllib.error.HTTPError:
        pass

    # Every interpolated value arrives from the UI, so all of it is quoted.
    # Each step announces itself on its own line before rsync's own progress,
    # which is what lets the UI say "2 of 5" instead of replaying 0-100% per
    # folder with nothing to say which folder it is on.
    steps = ["set -e",
             "apk add --no-cache rsync openssh-client sshpass >/dev/null 2>&1"]
    total = len(mappings)
    for index, mapping in enumerate(mappings, start=1):
        target = ("/appdata/" + mapping["folder"]) if mapping["folder"] else "/appdata"
        spec = shlex.quote(f"{src['user']}@{src['host']}:{mapping['remote_path']}/")
        label = mapping["folder"] or "appdata"
        steps.append(f"mkdir -p {shlex.quote(target)}")
        steps.append("echo " + shlex.quote(
            f"==> step {index}/{total} {label} :: {src['host']}:{mapping['remote_path']}"
            f" -> {mapping['mount_path']}"))
        steps.append(
            'sshpass -p "$SRC_PASS" rsync -aH --info=progress2 --no-perms --no-owner --no-group '
            "-e 'ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null' "
            f"{spec} {shlex.quote(target + '/')}")
        steps.append("echo " + shlex.quote(f"==> step {index}/{total} {label} complete"))
    steps.append("echo '==> done'; du -sh /appdata")
    script = "\n".join(steps) + "\n"

    body = {
        "apiVersion": "batch/v1", "kind": "Job",
        "metadata": {"name": job, "namespace": NS,
                     "labels": {"harvui.io/task": "import", "harvui.io/app": name}},
        "spec": {"backoffLimit": 1, "ttlSecondsAfterFinished": 3600,
                 "template": {"metadata": {"labels": {"harvui.io/task": "import"}},
                              "spec": {"restartPolicy": "Never",
                                       "containers": [{
                                           "name": "copy", "image": "alpine:3.20",
                                           "command": ["sh", "-c", script],
                                           "env": [{"name": "SRC_PASS", "valueFrom": {"secretKeyRef": {
                                               "name": f"harvui-src-{src['name']}", "key": "password"}}}],
                                           "volumeMounts": [{"name": "appdata", "mountPath": "/appdata"}],
                                       }],
                                       "volumes": [{"name": "appdata",
                                                    "persistentVolumeClaim": {"claimName": pvc}}]}}},
    }
    ksend("POST", f"/apis/batch/v1/namespaces/{NS}/jobs", body)

    created = None
    if cfg.get("create_workload", True):
        dcfg = {
            "name": name, "namespace": NS, "image": cfg["image"],
            "replicas": 0 if cfg.get("start_after_copy", True) else 1,
            "cpu": cfg.get("cpu", "50m"), "memory": cfg.get("memory", "256Mi"),
            "ports": cfg.get("ports") or [], "env": cfg.get("env") or {},
            "volumes": [{"path": mapping["mount_path"], "source": pvc, "type": "pvc",
                          "sub_path": mapping["folder"]} for mapping in mappings],
            "gpu": bool(cfg.get("gpu")),
            "hardware": cfg.get("hardware") or [], "icon": cfg.get("icon", ""),
            "icon_source": cfg.get("icon_source", cfg.get("icon", "")),
            "network_mode": cfg.get("network_mode", "loadbalancer"),
            "vip_mode": cfg.get("vip_mode", "shared"), "lb_ip": cfg.get("lb_ip", ""),
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
            "mappings": mappings,
            "storage_class": storage_class, "access_mode": access_mode,
            "note": "Deployment created stopped; start it once the copy job finishes."
                    if cfg.get("start_after_copy", True) else ""}


STEP = re.compile(r"==> step (\d+)/(\d+) (\S+)\s*(.*)$")
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
    for line in str(log or "").replace("\r", "\n").splitlines():
        line = line.strip()
        match = STEP.match(line)
        if match:
            step, total = int(match.group(1)), int(match.group(2))
            label, rest = match.group(3), (match.group(4) or "").strip()
            if rest == "complete":
                done_steps, percent, rate = step, 100, ""
            else:
                detail = rest[2:].strip() if rest.startswith("::") else rest
                percent, rate = 0, ""
            continue
        moved = RSYNC.search(line)
        if moved:
            percent, rate = int(moved.group(2)), moved.group(3)
        elif line.startswith("==> done"):
            step = total = max(total, step)
            done_steps, percent = total, 100
    if not total:
        return {}
    # Whole steps already finished, plus how far the running one has come.
    overall = ((done_steps + (percent / 100 if done_steps < step else 0)) / total) * 100
    return {"step": step, "steps": total, "folder": label, "detail": detail,
            "step_percent": percent, "percent": round(min(100.0, max(0.0, overall)), 1),
            "rate": rate}


def delete_import(name):
    """Remove an import Job, cancelling the copy if it is still running.

    A failed import leaves its Job behind, and while that Job exists it still
    references the appdata claim - which used to leave both the import and the
    volume it was filling unremovable from the UI.
    """
    if not re.fullmatch(r"harvui-import-[a-z0-9][a-z0-9-]{0,60}", str(name or "")):
        raise ValueError("unknown import job")
    try:
        ksend("DELETE", f"/apis/batch/v1/namespaces/{NS}/jobs/{name}"
                        "?propagationPolicy=Background")
    except urllib.error.HTTPError as error:
        if error.code != 404:
            raise
    _bust("wl", "ov")
    return {"ok": True, "name": name, "message": f"Import {name} removed"}


def _job_pod(job):
    try:
        pods = kget(f"/api/v1/namespaces/{NS}/pods?labelSelector=job-name%3D{job}").get("items", [])
    except Exception:
        return ""
    pods.sort(key=lambda pod: pod["metadata"].get("creationTimestamp", ""), reverse=True)
    return pods[0]["metadata"]["name"] if pods else ""


def import_status():
    try:
        jobs = kget(f"/apis/batch/v1/namespaces/{NS}/jobs?labelSelector=harvui.io/task%3Dimport").get("items", [])
    except Exception:
        return []
    out = []
    for j in jobs:
        st = j.get("status", {})
        state = ("running" if st.get("active") else "done" if st.get("succeeded")
                 else "failed" if st.get("failed") else "pending")
        row = {
            "name": j["metadata"]["name"],
            "app": j["metadata"].get("labels", {}).get("harvui.io/app", ""),
            "active": st.get("active", 0), "succeeded": st.get("succeeded", 0),
            "failed": st.get("failed", 0),
            "start": st.get("startTime", ""), "end": st.get("completionTime", ""),
            "state": state,
        }
        if state in ("running", "failed"):
            pod = _job_pod(j["metadata"]["name"])
            if pod:
                try:
                    row.update(import_progress(_pod_logs(pod)))
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
            previous = json.loads((meta.get("annotations", {}) or {}).get(
                "harvui.io/update-previous", "{}"))
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
    return {"nodes": per_node, "images": shared[:200],
            "distinct": len(shared),
            "node_names": [n["metadata"]["name"] for n in nodes],
            "retained": retained,
            "protected": sum(1 for image in shared if image["protected"])}


def prepull(image, nodes=None):
    """Warm an image onto every (or selected) node with a DaemonSet-style pull."""
    tag = re.sub(r"[^a-z0-9-]", "-", image.split("/")[-1].split(":")[0].lower())[:30]
    name = f"harvui-pull-{tag}"
    body = {
        "apiVersion": "apps/v1", "kind": "DaemonSet",
        "metadata": {"name": name, "namespace": NS,
                     "labels": {"harvui.io/task": "prepull", "harvui.io/image": tag}},
        "spec": {"selector": {"matchLabels": {"app": name}},
                 "template": {"metadata": {"labels": {"app": name}},
                              "spec": {"terminationGracePeriodSeconds": 1,
                                       "initContainers": [{"name": "pull", "image": image,
                                                           "command": ["/bin/true"]}],
                                       "containers": [{"name": "pause",
                                                       "image": "registry.k8s.io/pause:3.9",
                                                       "resources": {"requests": {"cpu": "1m", "memory": "4Mi"}}}]}}},
    }
    if nodes:
        body["spec"]["template"]["spec"]["affinity"] = {"nodeAffinity": {
            "requiredDuringSchedulingIgnoredDuringExecution": {"nodeSelectorTerms": [
                {"matchExpressions": [{"key": "kubernetes.io/hostname", "operator": "In", "values": nodes}]}]}}}
    try:
        ksend("DELETE", f"/apis/apps/v1/namespaces/{NS}/daemonsets/{name}")
        time.sleep(1)
    except urllib.error.HTTPError:
        pass
    ksend("POST", f"/apis/apps/v1/namespaces/{NS}/daemonsets", body)
    return {"ok": True, "daemonset": name, "image": image}


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
    for node in selected:
        suffix = hashlib.sha256((digest + "|" + node).encode()).hexdigest()[:10]
        pod_name = "harvui-image-clean-" + suffix
        body = {
            "apiVersion": "v1", "kind": "Pod",
            "metadata": {"name": pod_name, "namespace": NS,
                         "labels": {"app": "harvui-image-cleaner", "harvui.io/task": "image-cleanup"},
                         "annotations": {"harvui.io/image-digest": digest,
                                         "harvui.io/cache-node": node}},
            "spec": {"nodeName": node, "restartPolicy": "Never",
                     "terminationGracePeriodSeconds": 1,
                     "tolerations": [{"operator": "Exists"}],
                     "containers": [{
                         "name": "cleanup", "image": "python:3.12-alpine",
                         "command": ["/usr/local/bin/crictl", "--runtime-endpoint",
                                     "unix:///host/run/k3s/containerd/containerd.sock",
                                     "--image-endpoint",
                                     "unix:///host/run/k3s/containerd/containerd.sock",
                                     "rmi", image_ref],
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
        try:
            ksend("DELETE", f"/api/v1/namespaces/{NS}/pods/{pod_name}")
            time.sleep(.2)
        except urllib.error.HTTPError as error:
            if error.code != 404:
                raise
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
        "metadata": {"name": name, "namespace": NS, "labels": {"harvui.io/managed": "true"}},
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
                         "labels": {"harvui.io/managed": "true", "harvui.io/from": name}},
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
            "labels": {"harvui.io/managed": "true", VM_DISK_LABEL: "true"},
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
    try:
        return [{"name": i["metadata"]["name"],
                 "display": i["spec"].get("displayName", i["metadata"]["name"]),
                 "size_gb": round(int(i.get("status", {}).get("size", 0) or 0) / 1024**3, 1),
                 "progress": i.get("status", {}).get("progress", 0)}
                for i in kget("/apis/harvesterhci.io/v1beta1/virtualmachineimages").get("items", [])]
    except Exception:
        return []


def create_vm(cfg):
    """Create a KubeVirt VM backed by a Longhorn DataVolume."""
    name = _required_name(cfg.get("name"))
    ns = _required_name(cfg.get("namespace") or NS, "namespace")
    cores = int(cfg.get("cores", 2))
    mem = cfg.get("memory", "2Gi")
    disk = int(cfg.get("disk_gb", 20))
    sc = cfg.get("storage_class", "longhorn-r2")
    imported_dv = str(cfg.get("disk_import") or "").strip()
    dv = imported_dv or f"{name}-disk"
    password = str(cfg.get("password") or "")
    if not imported_dv and not cfg.get("cloud_init") and len(password) < 10:
        raise ValueError("root password must be at least 10 characters")

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

    src = {"blank": {}}
    if cfg.get("image_url"):
        src = {"http": {"url": cfg["image_url"]}}
    elif cfg.get("image_id"):
        src = {"pvc": {"namespace": "harvester-public", "name": cfg["image_id"]}}

    cloudinit = cfg.get("cloud_init") or (password and (
        "#cloud-config\n"
        f"hostname: {name}\n"
        "ssh_pwauth: true\n"
        f"password: {password}\n"
        "chpasswd: {expire: false}\n"))

    disks = [{"name": "root", "disk": {"bus": "virtio"}, "bootOrder": 1}]
    volumes = [{"name": "root", "dataVolume": {"name": dv}}]
    if cloudinit:
        disks.append({"name": "cloudinit", "disk": {"bus": "virtio"}})
        volumes.append({"name": "cloudinit", "cloudInitNoCloud": {"userData": cloudinit}})

    vm = {
        "apiVersion": "kubevirt.io/v1", "kind": "VirtualMachine",
        "metadata": {"name": name, "namespace": ns,
                     "labels": {"harvui.io/managed": "true", "app": name}},
        "spec": {
            "running": bool(cfg.get("start", True)),
            **({} if imported_dv else {"dataVolumeTemplates": [{
                "metadata": {"name": dv},
                "spec": {"source": src,
                         "pvc": {"accessModes": ["ReadWriteMany"],
                                 "resources": {"requests": {"storage": f"{disk}Gi"}},
                                 "storageClassName": sc}},
            }]}),
            "template": {
                "metadata": {"labels": {"kubevirt.io/domain": name, "app": name}},
                "spec": {
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
                    "evictionStrategy": "LiveMigrate",
                },
            },
        },
    }
    out = ksend("POST", f"/apis/kubevirt.io/v1/namespaces/{ns}/virtualmachines", vm)
    _bust("flow", "ov")
    return {"ok": True, "vm": name, "datavolume": dv}


def list_vms():
    try:
        vms = kget("/apis/kubevirt.io/v1/virtualmachines").get("items", [])
    except Exception:
        return []
    try:
        vmis = {(v["metadata"]["namespace"], v["metadata"]["name"]): v
                for v in kget("/apis/kubevirt.io/v1/virtualmachineinstances").get("items", [])}
    except Exception:
        vmis = {}
    out = []
    for v in vms:
        ns, name = v["metadata"]["namespace"], v["metadata"]["name"]
        i = vmis.get((ns, name), {})
        dom = v["spec"]["template"]["spec"]["domain"]
        out.append({
            "ns": ns, "name": name,
            "running": bool(v["spec"].get("running")),
            "phase": i.get("status", {}).get("phase", "Stopped"),
            "node": i.get("status", {}).get("nodeName", ""),
            "cores": dom.get("cpu", {}).get("cores", 0),
            "memory": dom.get("memory", {}).get("guest", ""),
            "ip": (i.get("status", {}).get("interfaces") or [{}])[0].get("ipAddress", ""),
            "migratable": any(c.get("type") == "LiveMigratable" and c.get("status") == "True"
                              for c in i.get("status", {}).get("conditions", []) or []),
        })
    return sorted(out, key=lambda x: x["name"])
