"""The watchdog: Homestead's data volume brought back after an unclean
restart, without a person (docs/plans/resilience-and-validated-updates.md,
section 3.3).

On 2026-11-10 a host was restarted without a drain. Longhorn marked the only
copy of Homestead's data volume failed and needed to salvage it, but its
auto-salvage uses only copies on a disk that takes new copies, and that disk
was below Longhorn's free-space threshold. Auto-salvage logged "Bringing up 0
replicas" for ever, and Homestead waited on its mount with nothing to say why.

The node probe kept running through that outage: it has no Longhorn volume.
So the watchdog runs inside it, and talks only to Longhorn's own API, which
needs no Kubernetes permissions. It acts on one case and nothing else:

- the volume is one Homestead's own pod mounts (Homestead writes their names
  into the probe's ConfigMap, homestead.json),
- that pod is waiting for it, and nothing but Homestead's own pods uses it (a
  data move's copy job or a restore never runs beside a salvage),
- the volume is detached and faulted, and every failed copy Longhorn could
  salvage is on a disk or host that takes no new copies, so auto-salvage
  refuses them all,
- and all of that has held for PATIENCE, so Longhorn's own salvage goes first.

It then asks Longhorn to salvage that one volume's copies, the same action
as Salvage in Longhorn's UI, which does not apply the free-space rule. It
changes no setting and touches no other volume. Only the probe on the host of
the first such copy acts, and it acts at most once per boot of its host: the
record is written to the host before the request, so a lost answer is never
sent again. Homestead raises an alert from the record once it is back.

A second case (k3s, 18 November 2026): after a restart without a drain,
Longhorn kept Homestead's data volume attached, with its engine recorded as
started, but the instance manager that came back with the host ran no engine.
Longhorn never noticed, so the volume never became usable and Homestead waited
on its mount for good. Stopping Homestead lets the volume detach, which resets
Longhorn's record, and starting it again attaches the volume cleanly. So when

- Homestead's data volume is attached and none of its engines runs,
- nothing but Homestead's own pods uses it, and none of them is Ready,
- and all of that has held for STALE_PATIENCE,

the probe on the host the volume is attached to scales Homestead's Deployment
to zero, waits for the volume to detach (removing Homestead's pods outright if
they are still there after POD_GRACE), and scales it back to the copies it
had. This needs the probe's own account and Role, which ship with Homestead
(deploy/deploy.yaml, deploy/rbac.yaml and the chart): get and patch
on Homestead's Deployment, and get, list and delete on pods in its namespace.
Kubernetes cannot limit pod deletion to Homestead's pods, so the watchdog
checks each pod's owner itself. It acts at most once per boot of its host, the
record is written before each step, and a run cut short - the probe restarted,
the host restarted, or the volume never detached within GIVE_UP - always ends
with Homestead scaled back up.
"""
import json
import os
import ssl
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

LONGHORN = os.environ.get("LONGHORN_API", "http://longhorn-backend.longhorn-system:9500/v1")
CONF = os.environ.get("WATCHDOG_CONF", "/srv/homestead.json")
STATE = os.environ.get("WATCHDOG_STATE", "/state/watchdog.json")
BOOT_ID = "/host/proc/sys/kernel/random/boot_id"
EVERY = 30                 # seconds between looks
AWAY = 300                 # between looks while Longhorn does not answer
PATIENCE = 180             # how long it must stay stuck before the watchdog acts
KEEP = 10                  # salvages kept in the record, across boots
MINIMAL = "storage-minimal-available-percentage"
STALE_PATIENCE = 300       # how long an attached volume with no running engine is left to Longhorn
POD_GRACE = 60             # after scaling down, how long Homestead's pods may take to stop by themselves
GIVE_UP = 600              # after scaling down, when Homestead is started again even if the volume never detached
SERVICE_ACCOUNT = "/var/run/secrets/kubernetes.io/serviceaccount"


def _conditions(obj):
    """Longhorn's conditions by type: a map in its API, a list in its CRDs."""
    raw = (obj or {}).get("conditions") or {}
    if isinstance(raw, list):
        return {row.get("type"): row for row in raw if isinstance(row, dict)}
    return {key: value for key, value in raw.items() if isinstance(value, dict)} if isinstance(raw, dict) else {}


def _true(obj, kind):
    return str(_conditions(obj).get(kind, {}).get("status", "")) == "True"


def _disk(nodes, host, disk_id, path):
    """(node, disk name, disk) holding a copy, or (node, "", {}) if the disk is not listed."""
    node = next((row for row in nodes if row.get("name") == host), None)
    if node is None:
        return None, "", {}
    for name, disk in (node.get("disks") or {}).items():
        if (disk_id and disk.get("diskUUID") == disk_id) or (path and disk.get("path") == path):
            return node, name, disk
    return node, "", {}


def _takes(node, disk):
    """Whether Longhorn would place, and so salvage, a copy on this disk."""
    return bool(node.get("allowScheduling", True) and disk.get("allowScheduling", True) and _true(disk, "Schedulable"))


def stuck(conf, volumes, nodes):
    """(volume, the copies to salvage) for Homestead's data volume when it is
    stuck as described above, else (None, why not)."""
    claims = set(conf.get("claims") or [])
    namespace, deployment = conf.get("namespace") or "", conf.get("deployment") or ""
    if not claims or not namespace or not deployment:
        return None, "Homestead has not named its data volume to the probe yet"
    mine = [v for v in volumes if ((v.get("kubernetesStatus") or {}).get("namespace") == namespace
                                    and (v.get("kubernetesStatus") or {}).get("pvcName") in claims)]
    if not mine:
        return None, "Homestead's data volume is not a Longhorn volume"
    for volume in mine:
        if volume.get("state") != "detached" or volume.get("robustness") != "faulted":
            continue
        users = (volume.get("kubernetesStatus") or {}).get("workloadsStatus") or []
        if not users or not all(row.get("workloadType") == "ReplicaSet"
                                and str(row.get("workloadName") or "").startswith(deployment + "-") for row in users):
            continue        # something besides Homestead holds it: a data move, a restore, a person
        if not any(row.get("podStatus") == "Pending" for row in users):
            continue
        failed = [r for r in volume.get("replicas") or [] if r.get("failedAt") and r.get("diskID") and r.get("hostId")]
        candidates = []
        for replica in failed:
            node, _, disk = _disk(nodes, replica["hostId"], replica.get("diskID"), replica.get("diskPath"))
            if node is None or not _true(node, "Ready") or not disk:
                continue    # a copy on a host that is down cannot be salvaged by anyone
            if _takes(node, disk):
                return None, "Longhorn can salvage Homestead's data volume by itself"
            candidates.append((replica, node, disk))
        if candidates:
            return volume, candidates
    return None, "Homestead's data volume is not stuck"


def stale(conf, volumes):
    """(volume, the host it is attached to) when Homestead's data volume is
    attached but none of its engines runs and nothing else uses it, else
    (None, why not)."""
    claims = set(conf.get("claims") or [])
    namespace, deployment = conf.get("namespace") or "", conf.get("deployment") or ""
    if not claims or not namespace or not deployment:
        return None, "Homestead has not named its data volume to the probe yet"
    for volume in volumes:
        status = volume.get("kubernetesStatus") or {}
        if status.get("namespace") != namespace or status.get("pvcName") not in claims:
            continue
        if volume.get("state") != "attached":
            continue
        engines = volume.get("controllers") or []
        if not engines or any(engine.get("running") for engine in engines):
            continue
        users = status.get("workloadsStatus") or []
        if not all(row.get("workloadType") == "ReplicaSet" and str(row.get("workloadName") or "").startswith(deployment + "-")
                   for row in users):
            continue        # a data move's copy, a restore or a person holds it
        host = next((engine.get("hostId") for engine in engines if engine.get("hostId")), "")
        if host:
            return volume, host
    return None, "Homestead's data volume is not stuck attached"


def _ready(pod):
    return any(c.get("type") == "Ready" and c.get("status") == "True"
               for c in (pod.get("status") or {}).get("conditions") or [])


def _owned(pod, deployment):
    return any(row.get("kind") == "ReplicaSet" and str(row.get("name") or "").startswith(deployment + "-")
               for row in (pod.get("metadata") or {}).get("ownerReferences") or [])


def _read_json(path, default):
    try:
        with open(path, encoding="utf-8") as handle:
            value = json.load(handle)
        return value if isinstance(value, type(default)) else default
    except (OSError, ValueError):
        return default


def _write_json(path, value):
    """Written whole, then renamed over the old file and flushed to the
    disk: a host losing power mid-write keeps the old record or the new."""
    temp = path + ".tmp"
    with open(temp, "w", encoding="utf-8") as handle:
        json.dump(value, handle)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temp, path)


class Watchdog:
    def __init__(self, node, get=None, post=None, clock=time.time, conf=CONF, state=STATE, boot_id=BOOT_ID, kube=None):
        self.node, self.clock = node, clock
        self.get, self.post = get or _get, post or _post
        self.kube = kube or _kube
        self.stale_since = None      # when the volume was first seen attached with no running engine
        self.conf, self.state, self.boot_path = conf, state, boot_id
        self.since = None            # when the volume was first seen stuck, this boot
        self.report = {"state": "starting", "detail": "", "salvaged": []}
        self.lock = threading.Lock()

    def _boot(self):
        try:
            with open(self.boot_path, encoding="utf-8") as handle:
                return handle.read().strip()
        except OSError:
            return ""

    def _say(self, state, detail):
        record = _read_json(self.state, {})
        with self.lock:
            changed = (self.report.get("state"), self.report.get("detail")) != (state, detail)
            self.report = {"state": state, "detail": detail, "salvaged": list(record.get("salvaged") or []),
                           "reset": list(record.get("reset") or [])}
        if changed and state in ("salvaged", "failed", "waiting"):
            print(f"watchdog: {detail}", flush=True)
        return state

    def _homestead(self, conf):
        """Homestead's Deployment and its pods, from Kubernetes."""
        namespace, name = conf["namespace"], conf["deployment"]
        deployment = self.kube("GET", f"/apis/apps/v1/namespaces/{namespace}/deployments/{name}")
        labels = ((deployment.get("spec") or {}).get("selector") or {}).get("matchLabels") or {}
        selector = urllib.parse.quote(",".join(f"{k}={v}" for k, v in sorted(labels.items())), safe="")
        pods = (self.kube("GET", f"/api/v1/namespaces/{namespace}/pods?labelSelector={selector}") or {}).get("items") or []
        return deployment, [pod for pod in pods if _owned(pod, name)]

    def _scale(self, conf, replicas):
        self.kube("PATCH", f"/apis/apps/v1/namespaces/{conf['namespace']}/deployments/{conf['deployment']}",
                  {"spec": {"replicas": replicas}}, "application/merge-patch+json")

    def _save_reset(self, record, resets):
        _write_json(self.state, {**record, "reset": resets[-KEEP:]})

    def _resume(self, conf, record, volumes, boot):
        """Carry on a reset under way: Homestead scaled down and waiting for
        its volume. Returns a state, or None when no reset is under way."""
        resets = [row for row in record.get("reset") or [] if isinstance(row, dict)]
        entry = next((row for row in reversed(resets) if row.get("phase") in ("stopping", "stopped")), None)
        if entry is None:
            return None
        if entry.get("node") != self.node:
            return self._say("watching", f"The probe on {entry.get('node')} is starting Homestead again")
        now = self.clock()
        started = float(entry.get("stopped_at") or entry.get("at") or now)
        found = next((v for v in volumes if v.get("name") == entry.get("volume")), None)
        detached = (found or {}).get("state") == "detached"
        if not detached and entry.get("boot_id") == boot and now - started < GIVE_UP:
            if not entry.get("pods_removed") and now - started >= POD_GRACE:
                try:
                    _, pods = self._homestead(conf)
                    left = [p["metadata"]["name"] for p in pods if not _ready(p)]
                    for name in left:
                        self.kube("DELETE", f"/api/v1/namespaces/{conf['namespace']}/pods/{urllib.parse.quote(name, safe='')}",
                                  {"gracePeriodSeconds": 0})
                    entry["pods_removed"] = left or ["none left"]
                    self._save_reset(record, resets)
                except Exception as error:
                    return self._say("waiting", f"Homestead's pods could not be removed yet: {str(error)[:120]}")
            return self._say("waiting", f"Homestead is stopped while {entry.get('volume')} detaches")
        try:
            self._scale(conf, max(1, int(entry.get("replicas") or 1)))
        except Exception as error:
            # Tried again at the next look: Homestead is never left stopped.
            return self._say("failed", f"Homestead could not be started again yet: {str(error)[:120]}")
        entry["phase"], entry["done_at"] = "done", now
        entry["result"] = ("accepted" if detached else "started again after the host restarted" if entry.get("boot_id") != boot
                           else f"started again: {entry.get('volume')} did not detach within {GIVE_UP // 60} minutes")
        self._save_reset(record, resets)
        return self._say("reset" if detached else "failed",
                         f"Started Homestead again after {entry.get('volume')} detached" if detached
                         else f"Homestead was {entry['result']}")

    def _reset(self, conf, record, volumes, boot):
        """Start a reset when Homestead's data volume is stuck attached, or
        None when it is not."""
        volume, host = stale(conf, volumes)
        if volume is None:
            self.stale_since = None
            return None
        if host != self.node:
            return self._say("watching", f"The probe on {host} looks after this volume")
        now = self.clock()
        try:
            deployment, pods = self._homestead(conf)
        except Exception as error:
            return self._say("waiting", f"{volume['name']} is attached with no running engine, and Kubernetes "
                                        f"did not answer: {str(error)[:120]}")
        if any(_ready(pod) for pod in pods):
            self.stale_since = None
            return None
        resets = [row for row in record.get("reset") or [] if isinstance(row, dict)]
        if any(row.get("boot_id") == boot for row in resets):
            return self._say("done", "Homestead was stopped and started again once since this host started. "
                                     "It is not done again until the next restart.")
        if self.stale_since is None:
            self.stale_since = now
        if now - self.stale_since < STALE_PATIENCE:
            return self._say("waiting", f"{volume['name']} is attached but its engine is not running. "
                                        f"The watchdog waits {STALE_PATIENCE // 60} minutes for Longhorn first.")
        replicas = int((deployment.get("spec") or {}).get("replicas") or 0) or 1
        status = volume.get("kubernetesStatus") or {}
        entry = {"boot_id": boot, "at": now, "node": self.node, "volume": volume["name"],
                 "claim": f"{status.get('namespace')}/{status.get('pvcName')}", "replicas": replicas,
                 "phase": "stopping", "result": "under way"}
        # The record first: at most once per boot, and a cut-short run is finished by the next look.
        resets.append(entry)
        self._save_reset(record, resets)
        try:
            self._scale(conf, 0)
        except Exception as error:
            entry["phase"], entry["result"] = "done", f"refused ({str(error)[:80]})"
            self._save_reset(record, resets)
            return self._say("failed", f"Homestead could not be stopped to reset {volume['name']}: {entry['result']}")
        entry["phase"], entry["stopped_at"] = "stopped", now
        self._save_reset(record, resets)
        return self._say("waiting", f"Stopped Homestead so that {volume['name']} detaches: it was attached on "
                                    f"{host} with no running engine")

    def snapshot(self):
        with self.lock:
            return json.loads(json.dumps(self.report))

    def step(self):
        """One look. Returns its state: idle, watching, waiting, salvaged,
        failed, done or unavailable."""
        conf = _read_json(self.conf, {})
        if not conf.get("claims"):
            return self._say("idle", "Homestead has not named its data volume to the probe yet")
        try:
            volumes = self.get("/volumes").get("data") or []
            nodes = self.get("/nodes").get("data") or []
        except Exception as error:
            self.since = None
            return self._say("unavailable", f"Longhorn's API did not answer: {str(error)[:120]}")
        boot = self._boot()
        record = _read_json(self.state, {})
        if boot:
            under_way = self._resume(conf, record, volumes, boot)
            if under_way:
                return under_way
        volume, found = stuck(conf, volumes, nodes)
        if volume is None:
            self.since = None
            if boot:
                resetting = self._reset(conf, record, volumes, boot)
                if resetting:
                    return resetting
            return self._say("watching", found)
        now = self.clock()
        salvaged = [row for row in record.get("salvaged") or [] if isinstance(row, dict)]
        if not boot:
            return self._say("waiting", "The host's boot ID is unreadable, so the watchdog cannot keep to once per boot")
        if any(row.get("boot_id") == boot for row in salvaged):
            return self._say("done", "Homestead's data volume was salvaged once since this host started. "
                                     "It is not salvaged again until the next restart.")
        acting = min(replica["hostId"] for replica, _, _ in found)
        if acting != self.node:
            return self._say("watching", f"The probe on {acting} looks after this volume")
        if self.since is None:
            self.since = now
        if now - self.since < PATIENCE:
            return self._say("waiting", f"{volume['name']} is faulted and Longhorn's auto-salvage refuses its copies. "
                                        f"The watchdog waits {PATIENCE // 60} minutes for Longhorn first.")
        try:
            minimal = int(str(self.get(f"/settings/{MINIMAL}").get("value") or "25"))
        except Exception:
            minimal = None
        replica, _, disk = found[0]
        entry = {"boot_id": boot, "at": now, "node": self.node, "volume": volume["name"],
                 "claim": f"{(volume.get('kubernetesStatus') or {}).get('namespace')}/{(volume.get('kubernetesStatus') or {}).get('pvcName')}",
                 "replicas": sorted(r["name"] for r, _, _ in found), "path": replica.get("diskPath") or disk.get("path") or "",
                 "available": int(disk.get("storageAvailable") or 0), "maximum": int(disk.get("storageMaximum") or 0),
                 "minimal": minimal, "result": "sending"}
        # The record first: at most once per boot holds even if the answer is lost.
        _write_json(self.state, {**record, "salvaged": (salvaged + [entry])[-KEEP:]})
        try:
            self.post(f"/volumes/{urllib.parse.quote(volume['name'], safe='')}?action=salvage", {"names": entry["replicas"]})
            entry["result"] = "accepted"
        except urllib.error.HTTPError as error:
            entry["result"] = f"refused (HTTP {error.code})"
        except Exception as error:
            entry["result"] = f"uncertain ({str(error)[:80]})"
        _write_json(self.state, {**record, "salvaged": (salvaged + [entry])[-KEEP:]})
        if entry["result"] == "accepted":
            return self._say("salvaged", f"Asked Longhorn to salvage {volume['name']} ({entry['claim']}), "
                                         f"copies {', '.join(entry['replicas'])}: its disk {entry['path']} takes no new copies")
        return self._say("failed", f"Salvaging {volume['name']} was {entry['result']}. It is not tried again until the next restart.")

    def run(self):
        while True:
            try:
                state = self.step()
            except Exception as error:
                state = self._say("failed", f"The watchdog's look failed: {str(error)[:160]}")
            time.sleep(AWAY if state == "unavailable" else EVERY)


def _request(method, path, body=None, timeout=10):
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(LONGHORN + path, data=data, method=method,
                                     headers={"Accept": "application/json", "Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=timeout) as answer:
        return json.loads(answer.read(4 * 1024 * 1024) or b"{}")


def _get(path):
    return _request("GET", path)


def _kube(method, path, body=None, ctype="application/json", timeout=15):
    """Kubernetes' API as the probe's own service account (homestead_probe.account_ready)."""
    host, port = os.environ.get("KUBERNETES_SERVICE_HOST", ""), os.environ.get("KUBERNETES_SERVICE_PORT", "443")
    if not host:
        raise RuntimeError("the probe is not running in a cluster")
    with open(os.path.join(SERVICE_ACCOUNT, "token"), encoding="utf-8") as handle:
        token = handle.read().strip()
    context = ssl.create_default_context(cafile=os.path.join(SERVICE_ACCOUNT, "ca.crt"))
    url = f"https://{'[' + host + ']' if ':' in host else host}:{port}{path}"
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(url, data=data, method=method, headers={
        "Authorization": f"Bearer {token}", "Accept": "application/json", "Content-Type": ctype})
    with urllib.request.urlopen(request, timeout=timeout, context=context) as answer:
        return json.loads(answer.read(4 * 1024 * 1024) or b"{}")


def _post(path, body):
    return _request("POST", path, body, timeout=30)


def start(node):
    """The watchdog's thread, for the probe's main."""
    dog = Watchdog(node)
    threading.Thread(target=dog.run, name="watchdog", daemon=True).start()
    return dog
