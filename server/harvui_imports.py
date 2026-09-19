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
import re
import time
import urllib.error

kget = ksend = create_pvc = build_deployment = None
NS = "lab"
_cache = {}


def bind(_kget, _ksend, _create_pvc, _build_dep, _ns, _cache_ref):
    global kget, ksend, create_pvc, build_deployment, NS, _cache
    kget, ksend, create_pvc, build_deployment = _kget, _ksend, _create_pvc, _build_dep
    NS, _cache = _ns, _cache_ref


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
    script = (f"sshpass -p \"$SRC_PASS\" ssh -o StrictHostKeyChecking=no "
              f"-o UserKnownHostsFile=/dev/null {s['user']}@{s['host']} "
              f"'ls -1 {p} 2>/dev/null | head -200'")
    return run_probe(f"browse-{name}", script, s)


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
def import_container(cfg):
    """Create the PVC, launch the copy Job, then create the Deployment.

    cfg: {source, remote_path, name, image, ports, env, mount_path, size_gb,
          start_after_copy}
    """
    name = cfg["name"]
    if not SAFE.match(name):
        raise ValueError("name must be lowercase letters, numbers and dashes")
    src = _source(cfg["source"])
    pvc = f"{name}-appdata"
    size = int(cfg.get("size_gb", 10))
    try:
        create_pvc(NS, pvc, size)
    except urllib.error.HTTPError as e:
        if e.code != 409:
            raise

    remote = cfg["remote_path"]
    job = f"harvui-import-{name}"
    try:
        ksend("DELETE", f"/apis/batch/v1/namespaces/{NS}/jobs/{job}?propagationPolicy=Background")
        time.sleep(1)
    except urllib.error.HTTPError:
        pass

    script = (
        "set -e\n"
        "apk add --no-cache rsync openssh-client sshpass >/dev/null 2>&1\n"
        "echo '==> copying %s:%s -> /appdata'\n"
        "sshpass -p \"$SRC_PASS\" rsync -aH --info=progress2 --no-perms --no-owner --no-group "
        "-e 'ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null' "
        "\"%s@%s:%s/\" /appdata/\n"
        "echo '==> done'; du -sh /appdata\n"
    ) % (src["host"], remote, src["user"], src["host"], remote.rstrip("/"))

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
            "volumes": [{"path": cfg.get("mount_path", "/config"), "source": pvc, "type": "pvc"}],
            "gpu": bool(cfg.get("gpu")),
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
            "note": "Deployment created stopped; start it once the copy job finishes."
                    if cfg.get("start_after_copy", True) else ""}


def import_status():
    try:
        jobs = kget(f"/apis/batch/v1/namespaces/{NS}/jobs?labelSelector=harvui.io/task%3Dimport").get("items", [])
    except Exception:
        return []
    out = []
    for j in jobs:
        st = j.get("status", {})
        out.append({
            "name": j["metadata"]["name"],
            "app": j["metadata"].get("labels", {}).get("harvui.io/app", ""),
            "active": st.get("active", 0), "succeeded": st.get("succeeded", 0),
            "failed": st.get("failed", 0),
            "start": st.get("startTime", ""), "end": st.get("completionTime", ""),
            "state": "running" if st.get("active") else
                     "done" if st.get("succeeded") else
                     "failed" if st.get("failed") else "pending",
        })
    return sorted(out, key=lambda x: x["start"], reverse=True)


# --------------------------------------------------------------- image cache
def image_cache():
    """What container images each node already has on disk."""
    nodes = kget("/api/v1/nodes").get("items", [])
    per_node, totals = [], {}
    for n in nodes:
        name = n["metadata"]["name"]
        imgs = n["status"].get("images", []) or []
        rows = []
        for i in imgs:
            tag = (i.get("names") or ["<none>"])[-1]
            sz = round((i.get("sizeBytes") or 0) / 1024**2, 1)
            rows.append({"name": tag, "size_mb": sz})
            totals[tag] = totals.get(tag, {"size_mb": sz, "nodes": []})
            totals[tag]["nodes"].append(name)
        rows.sort(key=lambda x: -x["size_mb"])
        per_node.append({"node": name, "count": len(rows),
                         "total_gb": round(sum(r["size_mb"] for r in rows) / 1024, 1),
                         "images": rows[:60]})
    shared = [{"name": k, "size_mb": v["size_mb"], "nodes": sorted(set(v["nodes"]))}
              for k, v in totals.items()]
    shared.sort(key=lambda x: -x["size_mb"])
    return {"nodes": per_node, "images": shared[:200],
            "distinct": len(shared),
            "node_names": [n["metadata"]["name"] for n in nodes]}


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
    name = cfg["name"]
    if not SAFE.match(name):
        raise ValueError("name must be lowercase letters, numbers and dashes")
    ns = cfg.get("namespace", NS)
    cores = int(cfg.get("cores", 2))
    mem = cfg.get("memory", "2Gi")
    disk = int(cfg.get("disk_gb", 20))
    sc = cfg.get("storage_class", "longhorn-r2")
    dv = f"{name}-disk"

    src = {"blank": {}}
    if cfg.get("image_url"):
        src = {"http": {"url": cfg["image_url"]}}
    elif cfg.get("image_id"):
        src = {"pvc": {"namespace": "harvester-public", "name": cfg["image_id"]}}

    cloudinit = cfg.get("cloud_init") or (
        "#cloud-config\n"
        f"hostname: {name}\n"
        "ssh_pwauth: true\n"
        f"password: {cfg.get('password', 'harvui')}\n"
        "chpasswd: {expire: false}\n")

    vm = {
        "apiVersion": "kubevirt.io/v1", "kind": "VirtualMachine",
        "metadata": {"name": name, "namespace": ns,
                     "labels": {"harvui.io/managed": "true", "app": name}},
        "spec": {
            "running": bool(cfg.get("start", True)),
            "dataVolumeTemplates": [{
                "metadata": {"name": dv},
                "spec": {"source": src,
                         "pvc": {"accessModes": ["ReadWriteMany"],
                                 "resources": {"requests": {"storage": f"{disk}Gi"}},
                                 "storageClassName": sc}},
            }],
            "template": {
                "metadata": {"labels": {"kubevirt.io/domain": name, "app": name}},
                "spec": {
                    "domain": {
                        "cpu": {"cores": cores},
                        "memory": {"guest": mem},
                        "resources": {"requests": {"memory": mem}},
                        "devices": {
                            "disks": [{"name": "root", "disk": {"bus": "virtio"}, "bootOrder": 1},
                                      {"name": "cloudinit", "disk": {"bus": "virtio"}}],
                            "interfaces": [{"name": "default", "masquerade": {}}],
                        },
                    },
                    "networks": [{"name": "default", "pod": {}}],
                    "volumes": [
                        {"name": "root", "dataVolume": {"name": dv}},
                        {"name": "cloudinit", "cloudInitNoCloud": {"userData": cloudinit}},
                    ],
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
