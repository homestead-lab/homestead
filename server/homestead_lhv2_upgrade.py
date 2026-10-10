"""Review and observe Longhorn's V2 rolling upgrade; Longhorn owns orchestration.

Never delete instance managers, detach volumes, or reset the upgrade control.
Inventory errors block starting an upgrade rather than imply an empty cluster.
"""
import json
import urllib.error
from urllib.parse import quote

import homestead_capacity_review as REVIEW

LH = "/apis/longhorn.io/v1beta2/namespaces/longhorn-system"
AUTO = "allow-instance-manager-automatic-upgrade"
TIMEOUT = "instance-manager-upgrade-timeout"
CONTROL = "longhorn-instance-manager-upgrade-control"
kget = ksend = platform = version = parse = None


def bind(read, send, detect, installed, parse_version):
    global kget, ksend, platform, version, parse
    kget, ksend, platform, version, parse = read, send, detect, installed, parse_version


def _get(path, optional=False):
    try:
        obj = kget(path)
    except urllib.error.HTTPError as error:
        if optional and error.code == 404:
            return None
        raise ValueError(f"Longhorn upgrade inventory is unavailable (HTTP {error.code}); check access and try again") from error
    if not isinstance(obj, dict):
        raise ValueError("Longhorn upgrade inventory is incomplete; check again")
    return obj


def _items(resource, optional=False):
    obj = _get(resource, optional)
    if obj is None:
        return []
    if not isinstance(obj.get("items"), list):
        raise ValueError("Longhorn upgrade inventory is incomplete; check again")
    return obj["items"]


def _ready(obj, condition="Ready"):
    return not obj.get("metadata", {}).get("deletionTimestamp") and any(
        c.get("type") == condition and c.get("status") == "True"
        for c in obj.get("status", {}).get("conditions", []))


def _setting(name, optional=False):
    obj = _get(LH + "/settings/" + name, optional)
    if obj is None:
        return None, {}
    try:
        values = json.loads(obj.get("value") or obj.get("default") or "{}")
        if not isinstance(values, dict):
            raise ValueError()
    except (ValueError, TypeError) as error:
        raise ValueError(f"Longhorn's {name} setting is unrecognized") from error
    return obj, values


def _set(name, value):
    obj, values = _setting(name)
    if values.get("v2") == str(value):
        return
    meta = obj.get("metadata") or {}
    if not meta.get("resourceVersion") or not meta.get("uid") or "value" not in obj:
        raise ValueError("Longhorn setting identity could not be verified; check again")
    values["v2"] = str(value)
    ksend("PATCH", LH + "/settings/" + name, [
        {"op": "test", "path": "/metadata/uid", "value": meta["uid"]},
        {"op": "test", "path": "/metadata/resourceVersion", "value": meta["resourceVersion"]},
        {"op": "replace", "path": "/value", "value": json.dumps(values, sort_keys=True)},
    ], ctype="application/json-patch+json")


def observation():
    """Read controller records, including paused upgrades still finishing a node."""
    control = _get(LH + "/instancemanagerupgradecontrols/" + CONTROL, True)
    upgrades = _items(LH + "/instancemanagerupgrades")
    _, values = _setting(AUTO)
    _, minutes = _setting(TIMEOUT)
    if values.get("v2") not in ("true", "false") or not str(minutes.get("v2", "")).isdigit():
        raise ValueError("Longhorn's V2 upgrade settings could not be verified")
    state = (control or {}).get("status") or {}
    image = (control or {}).get("spec", {}).get("targetImage", "")
    by_name = {u["metadata"]["name"]: u for u in upgrades}
    nodes = []
    for node, info in sorted((state.get("nodes") or {}).items()):
        u = by_name.get(info.get("imuName"), {})
        us = u.get("status") or {}
        same = u.get("spec", {}).get("targetImage") == image
        stage = us.get("state") if same else None
        nodes.append({"node": node, "state": info.get("state", "pending"),
                      "stage": stage or info.get("state", "pending"),
                      "error": (us.get("errorMsg") or us.get("abortReason") if same else "") or info.get("errorMsg", ""),
                      "retries": info.get("retryCount", 0)})
    active = bool(state.get("currentNode") or any(n["state"] == "in-progress" for n in nodes)
                  or any(u.get("status", {}).get("state") in ("relocating-engines", "waiting-for-source-im", "restoring-engines", "waiting-for-healthy-volumes")
                         and not u.get("metadata", {}).get("deletionTimestamp") for u in upgrades))
    return {"enabled": values.get("v2") == "true", "timeout": int(minutes.get("v2", 60)),
            "active": active, "current_node": state.get("currentNode", ""),
            "target_image": image, "nodes": nodes,
            "pending": any(n["state"] in ("pending", "failed") for n in nodes)}


def manager_ready(target):
    ds = _get("/apis/apps/v1/namespaces/longhorn-system/daemonsets/longhorn-manager")
    spec, status = ds.get("spec") or {}, ds.get("status") or {}
    images = [c.get("image", "") for c in spec.get("template", {}).get("spec", {}).get("containers", [])
              if c.get("name") == "longhorn-manager"]
    desired = status.get("desiredNumberScheduled", 0)
    return bool(images and images[0].rsplit(":", 1)[-1].lstrip("v") == target.lstrip("v")
                and not ds.get("metadata", {}).get("deletionTimestamp") and desired > 0
                and status.get("observedGeneration", 0) >= ds.get("metadata", {}).get("generation", 1)
                and status.get("updatedNumberScheduled") == desired
                and status.get("numberReady") == desired and status.get("numberAvailable") == desired)


def plan(target="", source=""):
    installed = version()
    target = target or installed
    here, to = parse(source or installed), parse(target)
    if not to:
        raise ValueError("Longhorn's upgrade version could not be verified")
    nodes = _items("/api/v1/nodes")
    volumes = _items(LH + "/volumes")
    replicas = _items(LH + "/replicas")
    v2 = [v for v in volumes if v.get("spec", {}).get("dataEngine") == "v2"]
    live, offline, common = [], [], []
    if platform(True).get("harvester"):
        common.append("Longhorn is managed by Harvester. Upgrade it through Harvester.")
    if to[:3] >= (1, 13, 0):
        server = parse(_get("/version").get("gitVersion"))
        if not server or server[:2] < (1, 34) or not nodes or any(
                not parse(n.get("status", {}).get("nodeInfo", {}).get("kubeletVersion")) or
                parse(n.get("status", {}).get("nodeInfo", {}).get("kubeletVersion"))[:2] < (1, 34) for n in nodes):
            common.append("Longhorn 1.13 requires Kubernetes 1.34 or newer on the API server and every host. Upgrade Kubernetes first.")
    for v in v2:
        name, vs, st = v["metadata"]["name"], v.get("spec") or {}, v.get("status") or {}
        running = any(r.get("spec", {}).get("volumeName") == name and
                      (r.get("status", {}).get("currentState") != "stopped" or
                       r.get("spec", {}).get("desiredState") != "stopped") for r in replicas)
        if st.get("state") != "detached" or vs.get("nodeID") or st.get("currentNodeID") or running:
            offline.append(f"{name}: stop its workload, detach the V2 volume and wait for every replica to stop.")
    if not here or here[:3] < (1, 12, 2) or to[:3] < (1, 13, 0):
        live.append("V2 live upgrade requires Longhorn 1.12.2 or later as the source, and 1.13 or later as the target.")
    if len(nodes) < 2:
        live.append("V2 live upgrade needs at least two hosts. Use an offline upgrade on a single host.")
    if v2:
        lhs = _items(LH + "/nodes")
        managers = _items(LH + "/instancemanagers")
        engines = _items(LH + "/engines")
        pods = _items("/api/v1/namespaces/longhorn-system/pods")
        running_pods = set()
        for m in managers:
            if m.get("spec", {}).get("dataEngine") != "v2":
                continue
            tag = m.get("spec", {}).get("image", "").rsplit(":", 1)[-1]
            if not parse(tag) or parse(tag)[:3] < (1, 12, 2):
                live.append(f"{m['metadata']['name']}: its V2 instance manager must already run 1.12.2 or later. Complete an offline upgrade first.")
            pod = next((p for p in pods if p.get("metadata", {}).get("name") == m["metadata"]["name"]), {})
            if _ready(pod) and any(c.get("ready") is True and c.get("image", "").endswith(m.get("spec", {}).get("image", ""))
                                   for c in pod.get("status", {}).get("containerStatuses", [])):
                running_pods.add(m["metadata"]["name"])
            else:
                live.append(f"{m['metadata']['name']}: wait for its V2 pod to be ready on the reported instance-manager image.")
        kube_ready = {n["metadata"]["name"] for n in nodes if _ready(n) and not n.get("spec", {}).get("unschedulable")}
        im_ready = {m.get("spec", {}).get("nodeID") for m in managers
                    if m.get("spec", {}).get("dataEngine") == "v2" and m.get("spec", {}).get("type") == "aio"
                    and m.get("status", {}).get("currentState") == "running" and m["metadata"]["name"] in running_pods
                    and not m.get("metadata", {}).get("deletionTimestamp")}
        eligible = set()
        for n in lhs:
            name = n["metadata"]["name"]
            if name not in kube_ready & im_ready or not _ready(n) or not n.get("spec", {}).get("allowScheduling"):
                continue
            for ident, disk in (n.get("spec", {}).get("disks") or {}).items():
                ds = (n.get("status", {}).get("diskStatus") or {}).get(ident, {})
                if disk.get("diskType") == "block" and disk.get("allowScheduling") and _ready({"status": ds}) and _ready({"status": ds}, "Schedulable") and ds.get("storageAvailable", 0) > 0:
                    eligible.add(name)
        if len(eligible) < 2:
            live.append("At least two ready V2 hosts need a running instance manager and a schedulable block disk with free space.")
        for v in v2:
            name, vs, st = v["metadata"]["name"], v.get("spec") or {}, v.get("status") or {}
            if vs.get("frontend") not in ("blockdev", "nvmf") or vs.get("dataLayout", {}).get("type") == "sharded":
                live.append(f"{name}: live upgrade supports NVMe/TCP volumes; ublk and sharded volumes need an offline upgrade.")
            if vs.get("migrationNodeID") or st.get("currentMigrationNodeID") or st.get("expansionRequired"):
                live.append(f"{name}: wait for its migration or expansion to finish.")
            if st.get("state") == "detached":
                continue
            ve = [e for e in engines if e.get("spec", {}).get("volumeName") == name
                  and e.get("status", {}).get("currentState") == "running"
                  and not e.get("metadata", {}).get("deletionTimestamp")]
            modes = {key: mode for e in ve for key, mode in (e.get("status", {}).get("replicaModeMap") or {}).items()}
            healthy_nodes = {r.get("spec", {}).get("nodeID") for r in replicas
                             if r.get("spec", {}).get("volumeName") == name and modes.get(r["metadata"]["name"]) == "RW"
                             and not r.get("spec", {}).get("failedAt") and r.get("status", {}).get("currentState") == "running"
                             and not r.get("metadata", {}).get("deletionTimestamp")}
            if st.get("state") != "attached" or st.get("robustness") != "healthy" or len(healthy_nodes & eligible) < 2:
                live.append(f"{name}: needs a healthy attached volume and RW replicas on at least two eligible hosts.")
    obs = observation() if parse(installed) and parse(installed)[:3] >= (1, 13, 0) else {"enabled": False, "timeout": 60, "active": False, "nodes": [], "pending": False}
    if obs["active"]:
        common.append("A V2 host upgrade is already running. Wait for it to finish before upgrading Longhorn again.")
    return {"installed": installed, "target": target, "v2_volumes": len(v2), "harvester": bool(platform(True).get("harvester")),
            "live_blockers": common + live, "offline_blockers": common + offline,
            "live_ready": not common and not live, "offline_ready": not common and not offline,
            "warnings": ["Back up V2 volumes before upgrading. Allow spare disk capacity for replica rebuilding.",
                         "Do not expand or live-migrate V2 volumes until all host upgrades finish and volumes are healthy."],
            "upgrade": obs}


def require_upgrade(target, mode):
    if mode not in ("live", "offline"):
        raise ValueError("Choose live or offline for the V2 upgrade")
    report = plan(target)
    blockers = report[mode + "_blockers"]
    # An older V1-only installation has no V2 process to live-upgrade.
    if not report["v2_volumes"] and mode == "offline":
        blockers = report["offline_blockers"]
    if blockers:
        raise ValueError(" ".join(blockers))
    return report


def settings_review(enabled, timeout):
    if not isinstance(enabled, bool) or isinstance(timeout, bool) or not isinstance(timeout, int) or not 1 <= timeout <= 1440:
        raise ValueError("Choose an upgrade state and a timeout between 1 and 1440 minutes")
    installed = parse(version())
    if not installed or installed[:3] < (1, 13, 0) or platform(True).get("harvester"):
        raise ValueError("V2 live upgrade controls require independently managed Longhorn 1.13 or newer")
    report = plan() if enabled else {"upgrade": observation()}
    obs = report["upgrade"]
    if enabled and not obs["enabled"] and not obs["active"]:
        if not manager_ready(version()):
            raise ValueError("Wait for all Longhorn managers to be ready on the installed version")
        if report["live_blockers"]:
            raise ValueError(" ".join(report["live_blockers"]))
    identities = []
    for name in (TIMEOUT, AUTO):
        obj, _ = _setting(name)
        identities.append({"name": name, "uid": obj.get("metadata", {}).get("uid"),
                           "value": obj.get("value")})
    cfg = {"enabled": enabled, "timeout": timeout}
    context = {"settings": identities, "version": version()}
    return {"plan": report, "capacity_token": REVIEW.issue(cfg, context)}, context


def configure(body, operations=None):
    if operations is not None:
        # The progress resolver uses this same shared lock. Persist the pause
        # before changing Longhorn, so a refresh/restart cannot re-enable it.
        with operations._lock:
            return _configure(body, operations)
    return _configure(body)


def _configure(body, operations=None):
    review, context = settings_review(body.get("enabled"), body.get("timeout"))
    cfg = {key: body.get(key) for key in ("enabled", "timeout", "capacity_token", "confirm_capacity")}
    if body.get("confirm_capacity") is not True or not REVIEW.valid(cfg, context):
        raise ValueError("Review the V2 upgrade settings again before applying")
    if not body['enabled'] and operations is not None:
        operations.require_write()
        items = operations._read()
        changed = False
        for item in items:
            ref = item.get('ref') or {}
            if (item.get('kind') == 'platform-upgrade' and item.get('status') in ('queued', 'running')
                    and ref.get('component') == 'longhorn' and ref.get('v2_mode') == 'live'
                    and not ref.get('v2_enabled')):
                ref['v2_enabled'] = True
                changed = True
        if changed:
            operations._write(items)
    # Pausing must work even during degraded recovery. No prerequisites are
    # imposed on disabling; a node already upgrading is allowed to finish.
    if not body["enabled"]:
        _set(AUTO, "false")
    _set(TIMEOUT, body["timeout"])
    if body["enabled"]:
        _set(AUTO, "true")
    return {"ok": True, "detail": "V2 live upgrades enabled" if body["enabled"] else
            "V2 live upgrades paused; the current host upgrade can still finish"}


def progress(item):
    ref = item["ref"]
    target = ref["to"]
    if version() != target or not manager_ready(target):
        return "running", 20, f"Waiting for Longhorn managers to be ready on {target}"
    if ref.get("v2_mode") == "live" and not ref.get("v2_enabled"):
        report = plan(target, ref["from"])
        if report["live_blockers"]:
            return "running", 35, "V2 live upgrade is waiting: " + " ".join(report["live_blockers"])
        _set(AUTO, "true")
        ref["v2_enabled"] = True
    obs = observation() if ref.get("v2_mode") == "live" else {"target_image": "", "active": False, "nodes": [], "enabled": False, "current_node": ""}
    managers = [m for m in _items(LH + "/instancemanagers") if m.get("spec", {}).get("dataEngine") == "v2"
                and not m.get("metadata", {}).get("deletionTimestamp")]
    image = _get(LH + "/settings/default-instance-manager-image").get("value")
    pods = _items("/api/v1/namespaces/longhorn-system/pods") if managers else []
    def ready_manager(m):
        pod = next((p for p in pods if p.get("metadata", {}).get("name") == m["metadata"]["name"]), {})
        return (m.get("spec", {}).get("image") == image and m.get("status", {}).get("currentState") == "running"
                and _ready(pod) and any(c.get("image") == image for c in pod.get("spec", {}).get("containers", []))
                and any(c.get("ready") is True and str(c.get("image", "")).endswith(image) for c in pod.get("status", {}).get("containerStatuses", [])))
    volumes = [v for v in _items(LH + "/volumes") if v.get("spec", {}).get("dataEngine") == "v2"]
    healthy = all(v.get("status", {}).get("state") == "detached" or
                  (v.get("status", {}).get("state") == "attached" and v.get("status", {}).get("robustness") == "healthy") for v in volumes)
    done = sum(n["state"] == "completed" for n in obs["nodes"])
    matching = obs["target_image"] == image
    if (managers or not volumes) and image and image.rsplit(":", 1)[-1].lstrip("v") == target.lstrip("v") and all(ready_manager(m) for m in managers) and healthy and not obs["active"] and (not obs["nodes"] or matching and done == len(obs["nodes"])):
        return "succeeded", 100, f"Longhorn {target}: all V2 instance managers upgraded and volumes healthy"
    if ref.get("v2_mode") == "offline":
        return "running", 40, "Waiting for the offline V2 instance-manager rollout and healthy volumes"
    node = next((n for n in obs["nodes"] if n["node"] == obs["current_node"]), None)
    message = f"{done} of {len(obs['nodes'])} V2 hosts upgraded"
    exhausted = [n for n in obs["nodes"] if n["state"] == "failed" and n["retries"] >= 5]
    if matching and not obs["active"] and exhausted and all(n["state"] in ("completed", "failed") for n in obs["nodes"]) and all(n["state"] != "failed" or n["retries"] >= 5 for n in obs["nodes"]):
        return "failed", 40 + int(55 * done / max(1, len(obs["nodes"]))), message + "; retry limit reached: " + "; ".join(n["node"] + ": " + n["error"] for n in exhausted)
    if node:
        message += f"; {node['node']}: {node['stage']}"
        if node["error"]:
            message += "; " + node["error"]
    elif not obs["enabled"]:
        message += "; paused before the next host"
    elif any(n["state"] == "failed" for n in obs["nodes"]):
        message += "; host upgrade failed; inspect V2 upgrade status for recovery and retries"
    else:
        message += "; waiting for Longhorn's upgrade controller"
    return "running", 40 + int(55 * done / max(1, len(obs["nodes"]))) if matching else 40, message


def ensure_vm_idle(namespace, name):
    installed = parse(version())
    if not installed or installed[:3] < (1, 13, 0):
        return
    namespace, name = quote(namespace, safe=""), quote(name, safe="")
    vmi = _get(f"/apis/kubevirt.io/v1/namespaces/{namespace}/virtualmachineinstances/{name}")
    for volume in vmi.get("spec", {}).get("volumes", []):
        claim = (volume.get("persistentVolumeClaim") or {}).get("claimName") or (volume.get("dataVolume") or {}).get("name")
        if not claim:
            continue
        pvc = _get(f"/api/v1/namespaces/{namespace}/persistentvolumeclaims/{quote(claim, safe='')}")
        pv_name = pvc.get("spec", {}).get("volumeName")
        if not pv_name:
            raise ValueError("VM storage is not bound; wait before migrating")
        pv = _get("/api/v1/persistentvolumes/" + quote(pv_name, safe=""))
        csi = pv.get("spec", {}).get("csi") or {}
        if csi.get("driver") == "driver.longhorn.io":
            lhvol = _get(LH + "/volumes/" + quote(csi.get("volumeHandle") or pv_name, safe=""))
            if lhvol.get("spec", {}).get("dataEngine") == "v2":
                ensure_idle()


def ensure_resize_idle(pvc, read):
    installed = parse(version())
    if not installed or installed[:3] < (1, 13, 0):
        return
    pv_name = pvc.get("spec", {}).get("volumeName")
    if not pv_name:
        raise ValueError("This claim must be bound before expanding it")
    pv = read("/api/v1/persistentvolumes/" + quote(pv_name, safe=""))
    csi = pv.get("spec", {}).get("csi") or {}
    if csi.get("driver") == "driver.longhorn.io":
        volume = read(LH + "/volumes/" + quote(csi.get("volumeHandle") or pv_name, safe=""))
        if volume.get("spec", {}).get("dataEngine") == "v2":
            ensure_idle()


def ensure_idle():
    """Longhorn forbids expansion/live migration during an active live upgrade."""
    installed = parse(version())
    if installed and installed[:3] >= (1, 13, 0):
        obs = observation()
        if obs["active"] or obs["pending"]:
            raise ValueError("Wait for the V2 live upgrade to finish and its volumes to recover before expanding or live-migrating disks")


# Its routes and who may use them (homestead_routes.py).
ROUTES = {
    ("GET", "/api/longhorn/v2/upgrade"): ("viewer", lambda request: plan(request.query.get("to", [""])[0])),
}
