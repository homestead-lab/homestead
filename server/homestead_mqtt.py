"""Cluster and node stats to MQTT, with Home Assistant discovery.

This is what hv-exporter did - a shell loop with curl, jq and mosquitto_pub in
its own pod - done by Homestead, which already reads everything it published.
The topics, entity names and unique ids are hv-exporter's, so Home Assistant
keeps the entities it has, with their history, when one replaces the other:

  harvester/cluster/state         cluster JSON: nodes, volumes, pods, VMs, CPU, RAM
  harvester/cluster/availability  online / offline (the broker says offline if
                                  Homestead drops without saying goodbye)
  harvester/node/<node>/state     per node: CPU, RAM, network, pods, VMs, status
  homeassistant/sensor/.../config one retained discovery message per sensor

MQTT is spoken directly - 3.1.1, QoS 0, retained messages, a last will -
which is all a stats publisher needs and keeps Homestead free of libraries.
Only the leading replica publishes. The password lives in a Secret.
"""
import json
import re
import socket
import ssl
import struct
import threading
import time
import urllib.error

import homestead_names as NAMES

kget = ksend = None
NAMESPACE = "default"
snapshot = lambda: None          # server supplies: the numbers to publish
is_leader = lambda: True
DEFAULTS = {"enabled": False, "host": "", "port": 1883, "tls": False, "username": "", "base": "harvester",
            "discovery": "homeassistant", "interval": 60, "device_name": "Harvester Cluster", "model": "Harvester"}
STATUS = {"state": "off", "detail": "", "last_publish": 0, "published": 0, "error": ""}
_lock = threading.Lock()

CLUSTER_SENSORS = [
    ("nodes_ready", "Nodes Ready", "nodes", "mdi:server"),
    ("nodes_total", "Nodes Total", "nodes", "mdi:server-network"),
    ("nodes_notready", "Nodes Not Ready", "nodes", "mdi:server-off"),
    ("vol_total", "Volumes Total", "vol", "mdi:database"),
    ("vol_degraded", "Volumes Degraded", "vol", "mdi:database-alert"),
    ("vol_faulted", "Volumes Faulted", "vol", "mdi:database-remove"),
    ("pods_system", "System Pods", "pods", "mdi:cog-outline"),
    ("pods_workload", "Workload Pods", "pods", "mdi:cube-outline"),
    ("pods_wl_bad", "Workload Pods Bad", "pods", "mdi:cube-off-outline"),
    ("pods_sys_bad", "System Pods Bad", "pods", "mdi:cog-off-outline"),
    ("vms_running", "VMs Running", "vms", "mdi:monitor"),
    ("health", "Cluster Health", "", "mdi:heart-pulse"),
    ("wl_summary", "Workloads", "", "mdi:format-list-bulleted"),
    ("cpu_pct", "Cluster CPU", "%", "mdi:cpu-64-bit"),
    ("mem_pct", "Cluster RAM", "%", "mdi:memory"),
]
NODE_SENSORS = [
    ("cpu", "CPU", "cpu_pct", "%", "mdi:cpu-64-bit"),
    ("mem", "RAM", "mem_pct", "%", "mdi:memory"),
    ("memgb", "RAM Used", "mem_gb", "GB", "mdi:memory"),
    ("rx", "Net In", "rx_mbps", "Mbit/s", "mdi:download-network"),
    ("tx", "Net Out", "tx_mbps", "Mbit/s", "mdi:upload-network"),
    ("pods", "Pods", "pods", "pods", "mdi:cube-outline"),
    ("wl", "Workloads", "wl", "", "mdi:format-list-bulleted"),
    ("vms", "VMs", "vms", "vms", "mdi:monitor"),
    ("st", "Status", "status", "", "mdi:server"),
]


def bind(_kget, _ksend, namespace, _snapshot, _is_leader):
    global kget, ksend, NAMESPACE, snapshot, is_leader
    kget, ksend, NAMESPACE, snapshot, is_leader = _kget, _ksend, namespace, _snapshot, _is_leader


# ------------------------------------------------------------------ MQTT
def _remaining(length):
    out = bytearray()
    while True:
        byte, length = length % 128, length // 128
        out.append(byte | (0x80 if length else 0))
        if not length:
            return bytes(out)


def _string(value):
    data = value.encode("utf-8") if isinstance(value, str) else value
    return struct.pack("!H", len(data)) + data


def connect_packet(client_id, username="", password="", will_topic="", will_message="", keepalive=90):
    flags = 0x02  # clean session
    payload = _string(client_id)
    if will_topic:
        flags |= 0x04 | 0x20  # will, retained, QoS 0
        payload += _string(will_topic) + _string(will_message)
    if username:
        flags |= 0x80
        payload += _string(username)
        if password:
            flags |= 0x40
            payload += _string(password)
    variable = _string("MQTT") + bytes([4, flags]) + struct.pack("!H", keepalive)
    body = variable + payload
    return bytes([0x10]) + _remaining(len(body)) + body


def publish_packet(topic, message, retain=True):
    body = _string(topic) + (message.encode("utf-8") if isinstance(message, str) else message)
    return bytes([0x30 | (0x01 if retain else 0)]) + _remaining(len(body)) + body


CONNACK_REASONS = {1: "the broker does not speak MQTT 3.1.1", 2: "the broker refused the client id",
                   3: "the broker is unavailable", 4: "the username or password was refused",
                   5: "the broker needs a username and password"}


class Client:
    def __init__(self, cfg, password, client_id, will_topic=""):
        self.cfg, self.password, self.client_id, self.will_topic = cfg, password, client_id, will_topic
        self.sock = None

    def connect(self, timeout=10):
        raw = socket.create_connection((self.cfg["host"], int(self.cfg["port"])), timeout=timeout)
        if self.cfg.get("tls"):
            context = ssl.create_default_context()
            raw = context.wrap_socket(raw, server_hostname=self.cfg["host"])
        raw.sendall(connect_packet(self.client_id, self.cfg.get("username", ""), self.password,
                                   self.will_topic, "offline" if self.will_topic else ""))
        head = raw.recv(4)
        if len(head) < 4 or head[0] != 0x20:
            raw.close()
            raise ConnectionError("the broker did not answer as an MQTT broker")
        if head[3]:
            raw.close()
            raise ConnectionError(CONNACK_REASONS.get(head[3], f"the broker refused the connection ({head[3]})"))
        self.sock = raw
        return self

    def publish(self, topic, message, retain=True):
        self.sock.sendall(publish_packet(topic, message, retain))

    def ping(self):
        self.sock.sendall(b"\xc0\x00")

    def close(self, gracefully=True):
        if not self.sock:
            return
        try:
            if gracefully:
                self.sock.sendall(b"\xe0\x00")
        except OSError:
            pass
        try:
            self.sock.close()
        except OSError:
            pass
        self.sock = None


# ------------------------------------------------------------------ settings
def _map():
    return NAMES.object_name("mqtt")


def _secret():
    return NAMES.object_name("mqtt")


def load():
    try:
        cm = kget(f"/api/v1/namespaces/{NAMESPACE}/configmaps/{_map()}")
        stored = json.loads((cm.get("data") or {}).get("mqtt.json") or "{}")
    except (urllib.error.HTTPError, ValueError):
        stored = {}
    except Exception:
        stored = {}
    return {**DEFAULTS, **{k: v for k, v in stored.items() if k in DEFAULTS or k == "has_password"}}


def _password():
    try:
        secret = kget(f"/api/v1/namespaces/{NAMESPACE}/secrets/{_secret()}")
        import base64
        return base64.b64decode((secret.get("data") or {}).get("password", "")).decode()
    except Exception:
        return ""


def _topic(value, label):
    value = str(value or "").strip().strip("/")
    if not value or not re.fullmatch(r"[A-Za-z0-9_\-/]{1,80}", value) or "//" in value:
        raise ValueError(f"the {label} is letters, numbers, _ - and /, like harvester")
    return value


def clean(cfg):
    out = dict(DEFAULTS)
    out["enabled"] = bool(cfg.get("enabled"))
    host = str(cfg.get("host") or "").strip()
    if out["enabled"] or host:
        if not re.fullmatch(r"[A-Za-z0-9.\-:\[\]]{1,253}", host):
            raise ValueError("the broker is a host name or IP address, like 192.0.2.177")
    out["host"] = host
    port = int(cfg.get("port") or (8883 if cfg.get("tls") else 1883))
    if not 1 <= port <= 65535:
        raise ValueError("the port is between 1 and 65535")
    out["port"] = port
    out["tls"] = bool(cfg.get("tls"))
    out["username"] = str(cfg.get("username") or "").strip()[:120]
    out["base"] = _topic(cfg.get("base") or DEFAULTS["base"], "base topic")
    out["discovery"] = _topic(cfg.get("discovery") or DEFAULTS["discovery"], "discovery prefix")
    interval = int(cfg.get("interval") or 60)
    if not 10 <= interval <= 3600:
        raise ValueError("publish every 10 seconds to an hour")
    out["interval"] = interval
    out["device_name"] = " ".join(str(cfg.get("device_name") or DEFAULTS["device_name"]).split())[:60]
    out["model"] = " ".join(str(cfg.get("model") or DEFAULTS["model"]).split())[:60]
    return out


def save(cfg):
    current = load()
    clean_cfg = clean(cfg)
    password = cfg.get("password")
    if password is not None and str(password) != "":
        import base64
        body = {"apiVersion": "v1", "kind": "Secret", "type": "Opaque",
                "metadata": {"name": _secret(), "namespace": NAMESPACE, "labels": {NAMES.key("managed"): "true"}},
                "data": {"password": base64.b64encode(str(password).encode()).decode()}}
        try:
            ksend("POST", f"/api/v1/namespaces/{NAMESPACE}/secrets", body)
        except urllib.error.HTTPError as error:
            if error.code != 409:
                raise
            ksend("PUT", f"/api/v1/namespaces/{NAMESPACE}/secrets/{_secret()}", body)
        clean_cfg["has_password"] = True
    else:
        clean_cfg["has_password"] = bool(current.get("has_password")) and not cfg.get("clear_password")
        if cfg.get("clear_password"):
            try:
                ksend("DELETE", f"/api/v1/namespaces/{NAMESPACE}/secrets/{_secret()}")
            except urllib.error.HTTPError:
                pass
    body = {"apiVersion": "v1", "kind": "ConfigMap",
            "metadata": {"name": _map(), "namespace": NAMESPACE, "labels": {NAMES.key("managed"): "true"}},
            "data": {"mqtt.json": json.dumps(clean_cfg, indent=1)}}
    try:
        existing = kget(f"/api/v1/namespaces/{NAMESPACE}/configmaps/{_map()}")
        body["metadata"]["resourceVersion"] = existing["metadata"]["resourceVersion"]
        ksend("PUT", f"/api/v1/namespaces/{NAMESPACE}/configmaps/{_map()}", body)
    except urllib.error.HTTPError as error:
        if error.code != 404:
            raise
        ksend("POST", f"/api/v1/namespaces/{NAMESPACE}/configmaps", body)
    _restart.set()
    return {"ok": True, "config": public(clean_cfg)}


def public(cfg=None):
    cfg = cfg or load()
    return {**{k: v for k, v in cfg.items() if k != "password"}, "status": dict(STATUS)}


# ------------------------------------------------------------------ what is published
def slug(name):
    return re.sub(r"[.\-]", "_", str(name))


def discovery(cfg, snap):
    """Every retained discovery message: hv-exporter's entities, same ids."""
    base, prefix = cfg["base"], cfg["discovery"]
    device = {"identifiers": ["harvester_cluster"], "name": cfg["device_name"], "manufacturer": "SUSE",
              "model": cfg["model"]}
    availability = f"{base}/cluster/availability"
    out = []
    for key, name, unit, icon in CLUSTER_SENSORS:
        payload = {"name": name, "state_topic": f"{base}/cluster/state", "value_template": "{{ value_json.%s }}" % key,
                   "unique_id": f"hv_{key}", "availability_topic": availability, "icon": icon, "device": device}
        if unit:
            payload["unit_of_measurement"] = unit
        out.append((f"{prefix}/sensor/harvester_{key}/config", payload))
    for node in snap.get("nodes") or []:
        s = slug(node["name"])
        node_device = {"identifiers": [f"hv_node_{s}"], "name": f"Harvester {node['name']}", "manufacturer": "SUSE",
                       "model": "Harvester Node", "via_device": "harvester_cluster"}
        for key, name, field, unit, icon in NODE_SENSORS:
            payload = {"name": name, "state_topic": f"{base}/node/{s}/state", "value_template": "{{ value_json.%s }}" % field,
                       "unique_id": f"hv_{s}_{key}", "availability_topic": availability, "icon": icon, "device": node_device}
            if unit:
                payload["unit_of_measurement"] = unit
            out.append((f"{prefix}/sensor/hv_{s}_{key}/config", payload))
    return out


def states(cfg, snap):
    base = cfg["base"]
    out = [(f"{base}/cluster/state", snap["cluster"])]
    for node in snap.get("nodes") or []:
        out.append((f"{base}/node/{slug(node['name'])}/state",
                    {k: node[k] for k in ("cpu_pct", "mem_pct", "mem_gb", "rx_mbps", "tx_mbps", "pods", "vms", "wl", "status")}))
    return out


def publish_once(cfg, client, snap, announced):
    """Discovery for nodes not yet announced, then availability and state."""
    names = tuple(sorted(n["name"] for n in snap.get("nodes") or []))
    if announced.get("nodes") != names:
        for topic, payload in discovery(cfg, snap):
            client.publish(topic, json.dumps(payload, separators=(",", ":")))
        announced["nodes"] = names
    client.publish(f"{cfg['base']}/cluster/availability", "online")
    count = 0
    for topic, payload in states(cfg, snap):
        client.publish(topic, json.dumps(payload, separators=(",", ":")))
        count += 1
    return count


def test(cfg_in):
    """Connects with these settings and says whether the broker accepted them."""
    cfg = clean({**load(), **cfg_in, "enabled": True})
    password = cfg_in.get("password") or _password()
    client = Client(cfg, password, f"homestead-test-{int(time.time())}")
    try:
        client.connect(timeout=8)
    except (OSError, ConnectionError) as error:
        raise ValueError(f"could not connect to {cfg['host']}:{cfg['port']}: {error}")
    finally:
        client.close()
    return {"ok": True, "detail": f"{cfg['host']}:{cfg['port']} accepted the connection"}


def _set(state, detail="", error=""):
    with _lock:
        STATUS.update(state=state, detail=detail, error=error)


_restart = threading.Event()


def run():
    """Publishes while enabled and leading; reconnects after any failure."""
    client, announced, backoff = None, {}, 5
    while True:
        cfg = load()
        if not cfg["enabled"] or not cfg["host"]:
            _set("off", "publishing is switched off")
            if client:
                client.close()
                client = None
            _restart.wait(30)
            _restart.clear()
            continue
        if not is_leader():
            _set("standby", "another Homestead replica is publishing")
            if client:
                client.close(gracefully=True)
                client = None
            _restart.wait(15)
            _restart.clear()
            continue
        try:
            if client is None:
                client = Client(cfg, _password(), "homestead-stats", f"{cfg['base']}/cluster/availability").connect()
                announced = {}
            snap = snapshot()
            if snap:
                count = publish_once(cfg, client, snap, announced)
                with _lock:
                    STATUS.update(state="publishing", error="", last_publish=int(time.time()),
                                  published=STATUS["published"] + count,
                                  detail=f"publishing to {cfg['host']}:{cfg['port']} every {cfg['interval']}s")
            backoff = 5
            # Between publishes the broker hears a ping at least every minute,
            # inside the keepalive, so a long interval does not look like a
            # dead client.
            remaining, changed = cfg["interval"], False
            while remaining > 0 and not changed:
                step = min(60, remaining)
                changed = _restart.wait(step)
                remaining -= step
                if not changed and remaining > 0:
                    client.ping()
            if changed:
                _restart.clear()
                client.close()
                client = None
        except Exception as error:
            _set("error", "", str(error)[:200])
            if client:
                client.close(gracefully=False)
                client = None
            time.sleep(backoff)
            backoff = min(backoff * 2, 300)
