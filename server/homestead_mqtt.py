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

Power control is a second, separate switch: an admin turns it on and picks
the containers and VMs Home Assistant may start, stop and restart (and, for a
VM, force off and reset). Each one gets a device with a Power switch and its
buttons, and Homestead listens on

  harvester/control/<app|vm>/<namespace>/<name>/<power|restart|force_off|reset>
  harvester/control/<app|vm>/<namespace>/<name>/state   ON / OFF, retained

A command is carried out as an operator would, through the same checks the
app makes, and written to the agent audit log as Home Assistant's. Only the
items picked are listened to; Homestead's own containers and the platform's
never can be.
"""
import queue
import json
import re
import socket
import ssl
import struct
import threading
import time
import urllib.error

import homestead_agent_audit as AUDIT
import homestead_kube as KUBE
import homestead_leader_state as LEADER_STATE
import homestead_names as NAMES

kget = ksend = None
NAMESPACE = "default"
snapshot = lambda: None          # server supplies: the numbers to publish
is_leader = lambda: True
DEFAULTS = {"enabled": False, "host": "", "port": 1883, "tls": False, "username": "", "base": "harvester",
            "discovery": "homeassistant", "interval": 60, "device_name": "Harvester Cluster", "model": "Harvester",
            "control": False, "controlled": [], "retire": []}
# Kept by their own route: saving the broker's settings leaves them as they are.
CONTROL_KEYS = ("control", "controlled", "retire")
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


def bind_control(items, act):
    """In place of the API's own (a test's): items() -> [{id, kind, ns, name,
    on, refused}]; act(kind, ns, name, action) -> detail."""
    global control_items, control_act
    control_items, control_act = items, act


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


def subscribe_packet(packet_id, topic):
    body = struct.pack("!H", packet_id) + _string(topic) + b"\x00"   # QoS 0
    return bytes([0x82]) + _remaining(len(body)) + body


def read_packet(recv):
    """(first byte, body) of one packet; recv(n) returns up to n bytes, b"" when closed."""
    def exactly(n):
        out = b""
        while len(out) < n:
            chunk = recv(n - len(out))
            if not chunk:
                raise ConnectionError("the broker closed the connection")
            out += chunk
        return out
    first = exactly(1)[0]
    length, shift = 0, 0
    for _ in range(4):
        byte = exactly(1)[0]
        length |= (byte & 0x7f) << shift
        shift += 7
        if not byte & 0x80:
            break
    else:
        raise ConnectionError("the broker sent a malformed packet")
    return first, exactly(length) if length else b""


def parse_publish(first, body):
    """(topic, payload, retained) of a PUBLISH packet."""
    (size,) = struct.unpack("!H", body[:2])
    topic = body[2:2 + size].decode("utf-8", "replace")
    rest = body[2 + size:]
    if (first >> 1) & 0x03:    # QoS 1 or 2: a packet id comes first
        rest = rest[2:]
    return topic, rest, bool(first & 0x01)


CONNACK_REASONS = {1: "the broker does not speak MQTT 3.1.1", 2: "the broker refused the client id",
                   3: "the broker is unavailable", 4: "the username or password was refused",
                   5: "the broker needs a username and password"}


class Client:
    def __init__(self, cfg, password, client_id, will_topic=""):
        self.cfg, self.password, self.client_id, self.will_topic = cfg, password, client_id, will_topic
        self.sock = None
        self._send = threading.Lock()
        self.lost = threading.Event()

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

    def _sendall(self, data):
        with self._send:
            if not self.sock:
                raise ConnectionError("not connected")
            self.sock.sendall(data)

    def publish(self, topic, message, retain=True):
        self._sendall(publish_packet(topic, message, retain))

    def ping(self):
        self._sendall(b"\xc0\x00")

    def listen(self, topic, on_message):
        """Subscribes, then hands each message to on_message(topic, payload,
        retained) from a thread of its own until the connection goes."""
        self._sendall(subscribe_packet(1, topic))
        sock = self.sock

        def recv(n):
            while True:
                try:
                    return sock.recv(n)
                except socket.timeout:
                    if self.sock is not sock:
                        return b""

        def reader():
            try:
                while self.sock is sock:
                    first, body = read_packet(recv)
                    if first >> 4 == 3:
                        on_message(*parse_publish(first, body))
            except (OSError, ConnectionError, ValueError, struct.error):
                pass
            if self.sock is sock:
                self.lost.set()
                _restart.set()
        threading.Thread(target=reader, daemon=True, name="mqtt-listen").start()

    def close(self, gracefully=True):
        with self._send:
            sock, self.sock = self.sock, None   # first, so the reader knows it was asked to go
        if not sock:
            return
        try:
            if gracefully:
                sock.sendall(b"\xe0\x00")
        except OSError:
            pass
        try:
            sock.close()
        except OSError:
            pass


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
    clean_cfg = clean(cfg)
    password = cfg.get("password")
    new_password = password is not None and str(password) != ""
    # Settings that cannot be saved stop this before the password changes.
    _readable()
    if new_password:
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
    elif cfg.get("clear_password"):
        try:
            ksend("DELETE", f"/api/v1/namespaces/{NAMESPACE}/secrets/{_secret()}")
        except urllib.error.HTTPError:
            pass

    def change(current):
        # Power control's keys are its own save's: kept as they are stored now.
        out = {**clean_cfg, **{key: current.get(key, DEFAULTS[key]) for key in CONTROL_KEYS}}
        out["has_password"] = new_password or (bool(current.get("has_password")) and not cfg.get("clear_password"))
        return out
    saved = _update(change)
    _restart.set()
    return {"ok": True, "config": public(saved)}


def _readable():
    """Raise, as _update would, when the stored settings are there but are not
    an object of JSON; a missing ConfigMap or key is fine."""
    try:
        cm = kget(f"/api/v1/namespaces/{NAMESPACE}/configmaps/{_map()}")
    except urllib.error.HTTPError as error:
        if error.code != 404:
            raise
        error.close()
        return
    raw = (cm.get("data") or {}).get("mqtt.json")
    try:
        if raw is not None and not isinstance(json.loads(raw), dict):
            raise ValueError
    except (TypeError, ValueError):
        raise ValueError("The MQTT settings could not be read (mqtt.json is not an object of JSON); nothing was changed") from None


def _update(change):
    """Change the settings as they are stored when the change is saved (#621):
    change(current) gets them, defaults filled in, and returns the new ones;
    it runs again on the newer settings if another copy saved meanwhile.
    Settings that cannot be read stop the change rather than be saved over."""
    def apply(stored):
        if stored is not None and not isinstance(stored, dict):
            raise ValueError("The MQTT settings could not be read (mqtt.json is not an object); nothing was changed")
        current = {**DEFAULTS, **{k: v for k, v in (stored or {}).items() if k in DEFAULTS or k == "has_password"}}
        return change(current)
    return KUBE.update_json(f"/api/v1/namespaces/{NAMESPACE}/configmaps/{_map()}", "mqtt.json", apply,
                            read=kget, write=ksend, indent=1)


def public(cfg=None):
    cfg = cfg or load()
    return {**{k: v for k, v in cfg.items() if k != "password"}, "status": shown_status(cfg["interval"])}


# ------------------------------------------------------------------ status every replica can show
# Only the leader publishes, but any replica may answer the browser. The
# leader shares what it is doing (homestead_leader_state) - on a change, and
# at most once a minute while it publishes - and a replica on standby shows
# that, rather than its own "standby".
SHARE_EVERY = 60


def _share(now=None):
    with _lock:
        status = dict(STATUS)
    LEADER_STATE.put("mqtt", status, every=SHARE_EVERY, key=status["state"], now=now)


def shown_status(interval=60, now=None):
    """This replica's status; on standby, the leader's, while it is fresh."""
    with _lock:
        local = dict(STATUS)
    if local["state"] != "standby":
        return local
    shared, _ = LEADER_STATE.get("mqtt", max_age=max(3 * SHARE_EVERY, 3 * interval), now=now)
    if isinstance(shared, dict) and shared.get("state") in ("publishing", "error"):
        return {k: shared.get(k, v) for k, v in local.items()}
    return local


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


def publish_once(cfg, client, snap, announced, items=None, unread=None):
    """Discovery for nodes not yet announced, then availability and state;
    with power control, the same for the items it covers, after retiring the
    ones it no longer does. A kind in `unread` could not be read this round:
    its items are left as last published, neither stated nor announced again."""
    names = tuple(sorted(n["name"] for n in snap.get("nodes") or []))
    if announced.get("nodes") != names:
        for topic, payload in discovery(cfg, snap):
            client.publish(topic, json.dumps(payload, separators=(",", ":")))
        announced["nodes"] = names
    items = items or []
    kept = [i for i in announced.get("control") or () if i.split(":")[0] in (unread or {})]
    ids = tuple(sorted([item["id"] for item in items] + kept))
    if "retired" not in announced:
        for topic in retire_topics(cfg, [i for i in cfg.get("retire") or [] if i not in ids]):
            client.publish(topic, "")
        announced["retired"] = True
    if announced.get("control") != ids:
        for topic, payload in control_discovery(cfg, items):
            client.publish(topic, json.dumps(payload, separators=(",", ":")))
        announced["control"] = ids
    client.publish(f"{cfg['base']}/cluster/availability", "online")
    count = 0
    for topic, payload in states(cfg, snap):
        client.publish(topic, json.dumps(payload, separators=(",", ":")))
        count += 1
    for topic, word in control_states(cfg, items):
        client.publish(topic, word)
        count += 1
    return count


# ------------------------------------------------------------------ power control
KINDS = {"app": "Container", "vm": "Virtual machine"}
PLURAL = {"app": "containers", "vm": "virtual machines"}
# What each kind answers to: a Power switch, then its buttons.
ACTIONS = {"app": ("power", "restart"), "vm": ("power", "restart", "force_off", "reset")}
BUTTONS = {"restart": ("Restart", "mdi:restart"), "force_off": ("Force off", "mdi:power-plug-off"),
           "reset": ("Reset", "mdi:restore-alert")}
ITEM = re.compile(r"(app|vm):([a-z0-9](?:[-a-z0-9]{0,61}[a-z0-9])?)/([a-z0-9](?:[-a-z0-9.]{0,251}[a-z0-9])?)")
MAX_CONTROLLED = 100
MAX_RETIRE = 200
COMMAND_GAP = 10            # seconds between two commands to one item
COMMANDS_PER_MINUTE = 20    # for them all
AUDIT_KEY = {"id": "mqtt", "name": "Home Assistant (MQTT)", "owner": ""}


class Unread(Exception):
    """A list power control covers could not be read (#620): `unread` says
    which kind and why, `items` holds what the other lists gave."""
    def __init__(self, unread, items):
        super().__init__("; ".join(f"could not read the {PLURAL[kind]}: {why}" for kind, why in unread.items()))
        self.unread, self.items = unread, items


def control_items():
    """What power control may be given, each with whether it is on, from the
    lists the API reads (homestead_api_v1); Homestead's own containers, and
    anything in the platform's namespaces, never. A list that cannot be read
    raises Unread, never stands for none."""
    import homestead_api_v1 as API_V1
    import homestead_workloads as WORKLOADS
    out, unread = [], {}
    try:
        for w in API_V1.ctx["workloads"]():
            refused = ("it is part of Homestead" if w.get("self") or w.get("homestead")
                       else "it runs in a system namespace" if w["ns"] in WORKLOADS.SYS_NS else "")
            out.append({"id": f"app:{w['ns']}/{w['name']}", "kind": "app", "ns": w["ns"], "name": w["name"],
                        "on": int(w.get("desired") or 0) > 0, "refused": refused})
    except Exception as error:
        unread["app"] = _why(error)
    try:
        for v in API_V1.ctx["vms"]():
            out.append({"id": f"vm:{v['ns']}/{v['name']}", "kind": "vm", "ns": v["ns"], "name": v["name"],
                        "on": bool(v.get("running")),
                        "refused": "it runs in a system namespace" if v["ns"] in WORKLOADS.SYS_NS else ""})
    except Exception as error:
        unread["vm"] = _why(error)
    if unread:
        raise Unread(unread, [item for item in out if item["kind"] not in unread])
    return out


def _why(error):
    return (getattr(error, "message", "") or str(error) or type(error).__name__)[:200]


def control_act(kind, ns, name, action):
    """One command, made as an operator would make it, through what /api/v1
    calls: start, stop, restart, and for a VM force-off and reset."""
    import homestead_api_v1 as API_V1
    import homestead_host_access as HOSTACCESS
    with HOSTACCESS.acting_as("operator"):
        if kind == "vm":
            result = API_V1.ctx["vm_power"](ns, name, action)
        elif action == "restart":
            API_V1.ctx["restart"](ns, name)
            result = {}
        elif action in ("start", "stop"):
            result = API_V1.ctx["scale"](ns, name, 1 if action == "start" else 0)
        else:
            raise ValueError(f"a container cannot {action}")
    return "; ".join(result.get("warnings") or [])[:300]


def split_id(item_id):
    """("app" or "vm", namespace, name) of an id like vm:lab/win11."""
    found = ITEM.fullmatch(str(item_id or ""))
    if not found:
        raise ValueError(f"{item_id} is not a container or VM, like app:lab/frigate or vm:lab/win11")
    return found.group(1), found.group(2), found.group(3)


def _item_topic(cfg, kind, ns, name):
    return f"{cfg['base']}/control/{kind}/{ns}/{name}"


def _uid(cfg, kind, ns, name):
    """Home Assistant's id for an item. Linked clusters each publish their
    own; two on one broker keep theirs apart by their base topic, which then
    is part of the id (harvester, the default, is not)."""
    scope = "" if cfg["base"] == DEFAULTS["base"] else re.sub(r"[^A-Za-z0-9]", "_", cfg["base"]) + "_"
    return f"hs_{scope}{kind}_{slug(ns)}_{slug(name)}"


def controlled(cfg, unread=None):
    """The items power control covers now: picked, still there, and allowed.
    Given a dict `unread`, a list that cannot be read is put in it and left
    out; without one, that raises Unread."""
    if not cfg.get("control"):
        return []
    picked = set(cfg.get("controlled") or [])
    try:
        items = control_items()
    except Unread as error:
        if unread is None:
            raise
        unread.update(error.unread)
        items = error.items
    return [item for item in items if item["id"] in picked and not item.get("refused")]


def publish_round(cfg, client, snap, announced):
    """One publish: the sensors and what power control covers. A list power
    control cannot read leaves its items as last published, and the rest still
    goes out (#620): an error here would drop the connection, and the broker's
    last will would make every entity unavailable. Returns (count, unread)."""
    unread = {}
    return publish_once(cfg, client, snap, announced, controlled(cfg, unread), unread), unread


def control_discovery(cfg, items):
    """A device per item: a Power switch, Restart, and for a VM Force off and Reset."""
    prefix, availability = cfg["discovery"], f"{cfg['base']}/cluster/availability"
    out = []
    for item in items:
        kind, ns, name = item["kind"], item["ns"], item["name"]
        uid, topic = _uid(cfg, kind, ns, name), _item_topic(cfg, kind, ns, name)
        device = {"identifiers": [uid], "name": name, "manufacturer": "Homestead",
                  "model": f"{KINDS[kind]} in {ns}", "via_device": "harvester_cluster"}
        out.append((f"{prefix}/switch/{uid}/config",
                    {"name": "Power", "unique_id": f"{uid}_power", "command_topic": f"{topic}/power",
                     "state_topic": f"{topic}/state", "payload_on": "ON", "payload_off": "OFF",
                     "availability_topic": availability, "icon": "mdi:power", "device": device}))
        for action in ACTIONS[kind][1:]:
            label, icon = BUTTONS[action]
            out.append((f"{prefix}/button/{uid}_{action}/config",
                        {"name": label, "unique_id": f"{uid}_{action}", "command_topic": f"{topic}/{action}",
                         "payload_press": "PRESS", "availability_topic": availability, "icon": icon,
                         "device": device}))
    return out


def control_states(cfg, items):
    return [(f"{_item_topic(cfg, item['kind'], item['ns'], item['name'])}/state", "ON" if item.get("on") else "OFF")
            for item in items]


def retire_topics(cfg, ids):
    """Each retained topic an item had, emptied so Home Assistant drops it."""
    out = []
    for item_id in ids:
        try:
            kind, ns, name = split_id(item_id)
        except ValueError:
            continue
        uid = _uid(cfg, kind, ns, name)
        out.append(f"{cfg['discovery']}/switch/{uid}/config")
        out += [f"{cfg['discovery']}/button/{uid}_{action}/config" for action in BUTTONS]
        out.append(f"{_item_topic(cfg, kind, ns, name)}/state")
    return out


def control_public(cfg=None):
    cfg = cfg or load()
    return {"control": bool(cfg.get("control")), "controlled": list(cfg.get("controlled") or []),
            "base": cfg["base"], "items": control_items()}


def save_control(body):
    """Power control on or off, and what it covers: an admin's action on its own."""
    current = load()
    picked = body.get("controlled", current.get("controlled") or [])
    if not isinstance(picked, list):
        raise ValueError("controlled is a list, like [\"vm:lab/win11\"]")
    known = {item["id"]: item for item in control_items()}
    ids = []
    for item_id in picked:
        kind, ns, name = split_id(item_id)
        found = known.get(item_id)
        if found and found.get("refused"):
            raise ValueError(f"{ns}/{name} cannot be controlled from Home Assistant: {found['refused']}")
        if found and item_id not in ids:
            ids.append(item_id)
    if len(ids) > MAX_CONTROLLED:
        raise ValueError(f"at most {MAX_CONTROLLED} containers and VMs")
    on = bool(body.get("control"))

    def change(current):
        # What is retired follows what is stored now, not what was read above.
        was = set(current.get("controlled") or []) if current.get("control") else set()
        now = set(ids) if on else set()
        retire = [i for i in current.get("retire") or [] if i not in now] + sorted(was - now)
        retire = list(dict.fromkeys(retire))[-MAX_RETIRE:]
        return {**current, "control": on, "controlled": ids, "retire": retire}
    cfg = _update(change)
    _restart.set()
    return {"ok": True, **control_public(cfg)}


def parse_command(cfg, topic, payload):
    """(kind, ns, name, action) a command asks for, None for a state topic,
    ValueError for anything else. Actions: start, stop, restart, force-off, reset."""
    prefix = f"{cfg['base']}/control/"
    if not topic.startswith(prefix):
        raise ValueError(f"{topic} is not a control topic")
    parts = topic[len(prefix):].split("/")
    if len(parts) != 4:
        raise ValueError(f"{topic} is not <kind>/<namespace>/<name>/<action>")
    kind, ns, name, action = parts
    if action == "state":
        return None
    split_id(f"{kind}:{ns}/{name}")
    if action not in ACTIONS[kind]:
        raise ValueError(f"a {KINDS[kind].lower()} has no {action}")
    word = (payload.decode("utf-8", "replace") if isinstance(payload, bytes) else str(payload)).strip().upper()
    if action == "power":
        if word not in ("ON", "OFF"):
            raise ValueError("power takes ON or OFF")
        return kind, ns, name, "start" if word == "ON" else "stop"
    if word != "PRESS":
        raise ValueError(f"{action} takes PRESS")
    return kind, ns, name, action.replace("_", "-")


_commands = queue.Queue(maxsize=20)
_recent = {}        # item id -> when its last command came
_minute = []        # when each command of the last minute came
_live = {}          # the connected client and its settings, for a state published after a command


def _audit(action, args, outcome, detail=""):
    AUDIT.record(AUDIT_KEY, "mqtt", action, args, outcome, detail)
    return outcome


def handle(cfg, topic, payload, now=None):
    """One command: checked, held to the rate limits, carried out, written
    down. Returns how it ended (ok, refused, error), None for a state."""
    now = now or time.time()
    try:
        parsed = parse_command(cfg, topic, payload)
    except ValueError as error:
        return _audit("mqtt_command", {"topic": topic[:200]}, "refused", str(error))
    if parsed is None:
        return None
    kind, ns, name, action = parsed
    item_id, label = f"{kind}:{ns}/{name}", f"mqtt_{kind}_{action.replace('-', '_')}"
    args = {"namespace": ns, "name": name}
    if not cfg.get("control") or item_id not in (cfg.get("controlled") or []):
        return _audit(label, args, "refused", "power control from Home Assistant is not on for it")
    try:
        items = control_items()
    except Unread as error:
        # Never acted on what was not read (#620): the command is refused, and said.
        if kind in error.unread:
            return _audit(label, args, "refused", f"could not read the {PLURAL[kind]} just now: {error.unread[kind]}")
        items = error.items
    found = next((item for item in items if item["id"] == item_id), None)
    if not found:
        return _audit(label, args, "refused", f"there is no {KINDS[kind].lower()} {ns}/{name}")
    if found.get("refused"):
        return _audit(label, args, "refused", found["refused"])
    with _lock:
        _minute[:] = [t for t in _minute if now - t < 60]
        if len(_minute) >= COMMANDS_PER_MINUTE:
            return _audit(label, args, "refused", f"at most {COMMANDS_PER_MINUTE} commands a minute")
        if now - _recent.get(item_id, 0) < COMMAND_GAP:
            return _audit(label, args, "refused", f"one command every {COMMAND_GAP} seconds for each item")
        _minute.append(now)
        _recent[item_id] = now
    try:
        detail = control_act(kind, ns, name, action) or ""
    except Exception as error:
        return _audit(label, args, "error", getattr(error, "message", "") or str(error) or type(error).__name__)
    return _audit(label, args, "ok", str(detail))


def _on_message(topic, payload, retained):
    # A retained command is an old one the broker kept: never acted on.
    if retained:
        return
    try:
        _commands.put_nowait((topic, payload))
    except queue.Full:
        pass


def _publish_control_states():
    client, cfg = _live.get("client"), _live.get("cfg")
    if not client or not cfg:
        return
    try:
        for topic, word in control_states(cfg, controlled(cfg, {})):
            client.publish(topic, word)
    except Exception:
        pass


def _command_worker():
    while True:
        topic, payload = _commands.get()
        try:
            if not is_leader():
                continue
            if handle(load(), topic, payload) == "ok":
                # The new state soon, and again once a VM has had time to boot.
                time.sleep(3)
                _publish_control_states()
                threading.Timer(20, _publish_control_states).start()
        except Exception as error:
            print(f"mqtt control: {str(error)[:160]}", flush=True)


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
    threading.Thread(target=_command_worker, daemon=True, name="mqtt-control").start()
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
                if cfg.get("control"):
                    client.listen(f"{cfg['base']}/control/+/+/+/+", _on_message)
                _live.update(client=client, cfg=cfg)
            snap = snapshot()
            if snap:
                count, unread = publish_round(cfg, client, snap, announced)
                note = "".join(f"; could not read the {PLURAL[kind]}, so theirs are as last published" for kind in unread)
                with _lock:
                    STATUS.update(state="publishing", error="", last_publish=int(time.time()),
                                  published=STATUS["published"] + count,
                                  detail=f"publishing to {cfg['host']}:{cfg['port']} every {cfg['interval']}s{note}")
                _share()
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
            if is_leader():
                _share()
            if client:
                client.close(gracefully=False)
                client = None
            time.sleep(backoff)
            backoff = min(backoff * 2, 300)


# Its routes and who may use them (homestead_routes.py).
ROUTES = {
    ("POST", "/api/mqtt"): ("admin", lambda request: save(request.body)),
    ("POST", "/api/mqtt/test"): ("admin", lambda request: test(request.body)),
    ("GET", "/api/mqtt/control"): ("admin", lambda request: control_public()),
    ("POST", "/api/mqtt/control"): ("admin", lambda request: save_control(request.body)),
    ("GET", "/api/mqtt"): ("viewer", lambda request: {**public(), "sensors": {"cluster": len(CLUSTER_SENSORS), "node": len(NODE_SENSORS)}}),
}
