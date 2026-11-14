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
"""
import json
import os
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
    def __init__(self, node, get=None, post=None, clock=time.time, conf=CONF, state=STATE, boot_id=BOOT_ID):
        self.node, self.clock = node, clock
        self.get, self.post = get or _get, post or _post
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
            self.report = {"state": state, "detail": detail, "salvaged": list(record.get("salvaged") or [])}
        if changed and state in ("salvaged", "failed", "waiting"):
            print(f"watchdog: {detail}", flush=True)
        return state

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
        volume, found = stuck(conf, volumes, nodes)
        if volume is None:
            self.since = None
            return self._say("watching", found)
        now, boot = self.clock(), self._boot()
        record = _read_json(self.state, {})
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
        _write_json(self.state, {"salvaged": (salvaged + [entry])[-KEEP:]})
        try:
            self.post(f"/volumes/{urllib.parse.quote(volume['name'], safe='')}?action=salvage", {"names": entry["replicas"]})
            entry["result"] = "accepted"
        except urllib.error.HTTPError as error:
            entry["result"] = f"refused (HTTP {error.code})"
        except Exception as error:
            entry["result"] = f"uncertain ({str(error)[:80]})"
        _write_json(self.state, {"salvaged": (salvaged + [entry])[-KEEP:]})
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


def _post(path, body):
    return _request("POST", path, body, timeout=30)


def start(node):
    """The watchdog's thread, for the probe's main."""
    dog = Watchdog(node)
    threading.Thread(target=dog.run, name="watchdog", daemon=True).start()
    return dog
