"""Homestead's own health and its copies: the API it depends on, each
background task, the node probe, Samba, and how many Homesteads run and
where. Settings shows it; server.py runs the tasks and keeps their beats.
"""
import time
import urllib.parse

import homestead_leader as LEADER
import homestead_mqtt as MQTT
import homestead_names as NAMES
import homestead_networking as NETWORK
import homestead_objectstore as OBJECTS
import homestead_probe as PROBE
import homestead_routes
import homestead_self as SELF

# server.py's, once bound.
kget = ksend = data_volume = heartbeats = node_temps = samba_state = None
NAMESPACE, LB_IP, VERSION = "lab", "", ""


def bind(_kget, _ksend, _data_volume, _heartbeats, _node_temps, _samba_state, namespace, lb_ip, version):
    """server.py's API calls, Homestead's data claim, the background tasks'
    beats, the probe's temperatures and the Samba state."""
    global kget, ksend, data_volume, heartbeats, node_temps, samba_state, NAMESPACE, LB_IP, VERSION
    kget, ksend, data_volume, heartbeats, node_temps, samba_state = _kget, _ksend, _data_volume, _heartbeats, _node_temps, _samba_state
    NAMESPACE, LB_IP, VERSION = namespace, lb_ip, version


MAX_REPLICAS = 3
# What each background task is called in Settings.
LOOP_WORDS = {"sampler": "Live charts", "alerts": "Alerts and notifications", "history": "Long-term stats", "host-fixes": "Host fixes", "host-console": "Host console add-on", "storage-pending": "New nodes held until their storage is ready", "os-updates": "OS updates", "baseline": "Platform installs", "vips": "VIP keeper",
              "hardware": "Hardware detection", "moves": "Cluster moves", "samba": "Network shares", "uptime": "Uptime checks", "auto-updates": "Automatic updates", "restore-tests": "Restore tests", "forecast": "Storage forecast", "changes": "Change history", "housekeeping": "Data housekeeping", "schedules": "Schedules", "unifi": "UniFi sync"}


def report():
    """Homestead's own health: the API it depends on, its copies and leader,
    each background task, the node probe, Samba and its permissions."""
    started = time.time()
    try:
        kget("/version")
        api = {"ok": True, "ms": int((time.time() - started) * 1000)}
    except Exception as error:
        api = {"ok": False, "ms": int((time.time() - started) * 1000), "error": str(error)[:160]}
    leading = LEADER.is_leader()
    now = time.time()
    loops = []
    rows = heartbeats()
    for name, word in LOOP_WORDS.items():
        row = rows.get(name)
        if not row:
            state = "standby" if name != "sampler" and not leading else "starting"
        elif row["leader_only"] and not leading:
            state = "standby"
        elif row["error"] and row["error_at"] >= row["last_ok"]:
            state = "failing"
        elif now - row["last_ok"] > max(3 * row["every"], 120):
            state = "late"
        else:
            state = "ok"
        loops.append({"name": name, "label": word, "state": state,
                      "last_ok": int(row["last_ok"]) if row and row["last_ok"] else 0,
                      "error": (row or {}).get("error", ""), "every": (row or {}).get("every", 0)})
    try:
        replicas = replicas_now()
    except Exception as error:
        replicas = {"error": str(error)[:160]}
    probe = dict(PROBE.status())
    try:
        ds = kget(f"/apis/apps/v1/namespaces/{NAMESPACE}/daemonsets/{NAMES.NODEPROBE}")
        st = ds.get("status") or {}
        probe.update(installed=True, desired=int(st.get("desiredNumberScheduled", 0) or 0),
                     ready=int(st.get("numberReady", 0) or 0))
    except Exception:
        probe.update(installed=False, desired=0, ready=0)
    try:
        temps = node_temps()
        probe["reporting"] = len(temps)
        probe["smart"] = sum(1 for t in temps.values() if (t.get("smart_helper") or {}).get("available"))
    except Exception:
        probe["reporting"] = probe["smart"] = 0
    try:
        samba = samba_state()
    except Exception as error:
        samba = {"error": str(error)[:160]}
    try:
        backups = OBJECTS.status()
    except Exception:
        backups = {}
    mqtt = {}
    try:
        mqtt = dict(MQTT.STATUS)
    except Exception:
        pass
    # Homestead's shared address, and any app, on the cluster's own address:
    # host joining (RKE2's 9345) and the dashboard answer there.
    try:
        network = homestead_routes.cached("network", 5, NETWORK.inventory)
        addresses = {"lb_ip": LB_IP, "problem": (network.get("shared_vip") or {}).get("problem", ""),
                     "clashes": network.get("platform_clashes") or [],
                     "platform": sorted(network.get("platform_addresses") or {})}
    except Exception as error:
        addresses = {"lb_ip": LB_IP, "error": str(error)[:160]}
    return {"version": VERSION, "addresses": addresses, "api": api, "leader": leading, "identity": LEADER.IDENTITY,
            "replicas": replicas, "loops": loops, "probe": probe, "samba": samba,
            "permissions": dict(SELF.LAST), "backups": {k: backups.get(k) for k in ("deployed", "ready", "endpoint")},
            "mqtt": {k: mqtt.get(k) for k in ("state", "detail", "error", "last_publish")}}


def replicas_now():
    """How many Homesteads run, where, and which one leads."""
    ns, name = SELF.NS, NAMES.BRAND
    dep = kget(f"/apis/apps/v1/namespaces/{ns}/deployments/{name}")
    selector = ",".join(f"{k}={v}" for k, v in sorted(((dep["spec"].get("selector") or {}).get("matchLabels") or {}).items()))
    pods = kget(f"/api/v1/namespaces/{ns}/pods?labelSelector={urllib.parse.quote(selector, safe='')}").get("items", [])
    try:
        holder = (kget(f"/apis/coordination.k8s.io/v1/namespaces/{ns}/leases/{LEADER.NAME}").get("spec") or {}).get("holderIdentity", "")
    except Exception:
        holder = ""
    rows = []
    for pod in pods:
        conditions = {c.get("type"): c.get("status") for c in (pod.get("status") or {}).get("conditions") or []}
        rows.append({"name": pod["metadata"]["name"], "node": (pod.get("spec") or {}).get("nodeName", ""),
                     "ready": conditions.get("Ready") == "True", "leader": pod["metadata"]["name"] == holder,
                     "this": pod["metadata"]["name"] == LEADER.IDENTITY,
                     "terminating": bool(pod["metadata"].get("deletionTimestamp"))})
    nodes = len({row["node"] for row in rows if row["node"] and row["ready"]})
    try:
        data = data_volume(dep)
    except Exception as error:
        data = {"pvc": "", "shareable": False, "reason": f"could not read the data claim: {str(error)[:120]}", "candidates": []}
    return {"desired": int(dep["spec"].get("replicas", 1) or 0), "pods": sorted(rows, key=lambda row: row["name"]),
            "leader": holder, "spread_nodes": nodes, "max": MAX_REPLICAS, "data": data}


ROLLING = {"type": "RollingUpdate", "rollingUpdate": {"maxSurge": 1, "maxUnavailable": 0}}


def own_strategy(shareable):
    """How Homestead replaces itself. Rolling - the new copy up before the old
    one goes - only when every node can mount the data volume; otherwise the
    two overlap on one volume, and on a migratable class Longhorn takes that
    for a VM migration and refuses the mount ("invalid controller count")."""
    return dict(ROLLING) if shareable else {"type": "Recreate"}


def set_replicas(count):
    """Runs this many Homesteads, spread over different nodes where it can.

    More than one means a node failure leaves another already serving: the
    Service drops the dead one and the leader lease moves within seconds.
    Rolling updates replace one at a time, so an update never takes it down."""
    count = int(count)
    if not 1 <= count <= MAX_REPLICAS:
        raise ValueError(f"run between 1 and {MAX_REPLICAS} copies of Homestead")
    if count > 1:
        data = data_volume()
        if not data["shareable"]:
            raise ValueError(f"{data['reason']}. Move Homestead's data to a shareable volume first (Settings, Redundancy).")
    ns, name = SELF.NS, NAMES.BRAND
    dep = kget(f"/apis/apps/v1/namespaces/{ns}/deployments/{name}")
    dep["spec"]["replicas"] = count
    dep["spec"]["strategy"] = own_strategy(data_volume(dep)["shareable"])
    labels = (dep["spec"].get("selector") or {}).get("matchLabels") or {"app": name}
    spec = dep["spec"]["template"]["spec"]
    affinity = spec.setdefault("affinity", {})
    spread = {"weight": 100, "podAffinityTerm": {"labelSelector": {"matchLabels": dict(labels)},
                                                 "topologyKey": "kubernetes.io/hostname"}}
    anti = affinity.setdefault("podAntiAffinity", {})
    preferred = [term for term in anti.get("preferredDuringSchedulingIgnoredDuringExecution") or [] if term != spread]
    anti["preferredDuringSchedulingIgnoredDuringExecution"] = preferred + [spread]
    ksend("PUT", f"/apis/apps/v1/namespaces/{ns}/deployments/{name}", dep)
    return {"ok": True, "desired": count,
            "detail": f"Homestead runs as {count} cop{'ies' if count != 1 else 'y'}" +
                      (", spread over different nodes" if count > 1 else "")}


ROUTES = {
    ("GET", "/api/self/health"): ("viewer", lambda request: report()),
    ("GET", "/api/self/replicas"): ("viewer", lambda request: replicas_now()),
    ("POST", "/api/self/replicas"): ("admin", lambda request: set_replicas(request.body.get("replicas"))),
}
