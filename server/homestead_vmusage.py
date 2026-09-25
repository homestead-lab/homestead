"""What each running VM is using: CPU, memory and disk traffic.

A VM runs inside its virt-launcher pod, so the metrics API's figures for that
pod are the VM's CPU and memory, QEMU's own small share included. Disk
traffic is not in the metrics API: KubeVirt's virt-handler on each node
counts every VM's reads and writes, and publishes the counts on its metrics
endpoint. A count becomes a rate between two readings, so readings are taken
in the background every half minute, and the page shows the rate between the
last two.

Each part is best effort: a cluster without metrics-server has no CPU or
memory figures, and one whose virt-handler cannot be reached from Homestead
has no disk figures. What is missing is left out, never made up.
"""
import re
import ssl
import threading
import time
import urllib.parse
import urllib.request

kget = None
PORT = 8443
TIMEOUT = 4
READ = "kubevirt_vmi_storage_read_traffic_bytes_total"
WRITE = "kubevirt_vmi_storage_write_traffic_bytes_total"
# Rates from readings further apart than this say little about now.
STALE = 180
_lock = threading.Lock()
_readings = []        # the last two: {"at": time, "io": {(ns, vm): [read, write]}}
_usage = {}           # (ns, vm) -> {"cpu": cores, "mem": bytes}
_note = {"io": ""}
LINE = re.compile(r'^([a-zA-Z_:][a-zA-Z0-9_:]*)\{([^}]*)\}\s+([-+0-9.eE]+|NaN)')
LABEL = re.compile(r'(\w+)="((?:[^"\\]|\\.)*)"')


def bind(_kget):
    global kget
    kget = _kget


def _cores(value):
    value = str(value or "0")
    if value.endswith("n"):
        return int(value[:-1]) / 1e9
    if value.endswith("u"):
        return int(value[:-1]) / 1e6
    if value.endswith("m"):
        return int(value[:-1]) / 1e3
    return float(value)


def _bytes(value):
    match = re.fullmatch(r"(\d+(?:\.\d+)?)(Ki|Mi|Gi|Ti|K|M|G|T)?", str(value or "0"))
    if not match:
        return 0
    scale = {"Ki": 2**10, "Mi": 2**20, "Gi": 2**30, "Ti": 2**40, "K": 10**3, "M": 10**6, "G": 10**9, "T": 10**12}
    return int(float(match.group(1)) * scale.get(match.group(2) or "", 1))


def parse_io(text):
    """Each VM's bytes read and written so far, summed over its drives."""
    out = {}
    for line in str(text or "").splitlines():
        match = LINE.match(line)
        if not match or match.group(1) not in (READ, WRITE) or match.group(3) == "NaN":
            continue
        labels = dict(LABEL.findall(match.group(2)))
        key = (labels.get("namespace", ""), labels.get("name", ""))
        if not key[1]:
            continue
        row = out.setdefault(key, [0.0, 0.0])
        row[0 if match.group(1) == READ else 1] += float(match.group(3))
    return out


def _fetch(ip):
    context = ssl.create_default_context()
    # virt-handler serves its metrics with its own certificate, as Prometheus
    # reads them; only counters come back, and nothing is sent.
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    with urllib.request.urlopen(f"https://{ip}:{PORT}/metrics", context=context, timeout=TIMEOUT) as response:
        return response.read().decode("utf-8", "replace")


def _handlers():
    selector = urllib.parse.quote("kubevirt.io=virt-handler", safe="")
    return [p for p in kget(f"/api/v1/pods?labelSelector={selector}").get("items", [])
            if (p.get("status") or {}).get("phase") == "Running" and (p.get("status") or {}).get("podIP")]


def sample(fetch=None, now=None):
    """Take one reading of every running VM."""
    fetch = fetch or _fetch
    now = now or time.time()
    usage = {}
    try:
        for m in kget("/apis/metrics.k8s.io/v1beta1/pods").get("items", []):
            meta = m.get("metadata") or {}
            vm = (meta.get("labels") or {}).get("vm.kubevirt.io/name") or ""
            if not vm and str(meta.get("name", "")).startswith("virt-launcher-"):
                vm = re.sub(r"-[a-z0-9]{5}$", "", meta["name"][len("virt-launcher-"):])
            if not vm:
                continue
            containers = m.get("containers") or []
            usage[(meta.get("namespace", ""), vm)] = {
                "cpu": sum(_cores((c.get("usage") or {}).get("cpu")) for c in containers),
                "mem": sum(_bytes((c.get("usage") or {}).get("memory")) for c in containers)}
    except Exception:
        pass
    io, failed = {}, []
    try:
        handlers = _handlers()
    except Exception:
        handlers = []
    for pod in handlers:
        try:
            io.update(parse_io(fetch(pod["status"]["podIP"])))
        except Exception:
            failed.append((pod.get("spec") or {}).get("nodeName") or pod["status"]["podIP"])
    with _lock:
        _usage.clear()
        _usage.update(usage)
        if io or not failed:
            _readings.append({"at": now, "io": io})
            del _readings[:-2]
        _note["io"] = (f"virt-handler on {', '.join(failed)} did not answer, so disk traffic there is unknown"
                       if failed else "" if handlers else "no virt-handler found to read disk traffic from")


def usage(now=None):
    """Per VM: CPU cores, memory bytes, and disk read and write bytes/s."""
    now = now or time.time()
    with _lock:
        rates = {}
        if len(_readings) == 2 and now - _readings[1]["at"] < STALE:
            before, after = _readings
            span = max(1.0, after["at"] - before["at"])
            for key, (read, write) in after["io"].items():
                if key in before["io"]:
                    # A counter that went backwards is a VM that restarted.
                    rates[key] = [max(0.0, read - before["io"][key][0]) / span,
                                  max(0.0, write - before["io"][key][1]) / span]
        out = {}
        for key in set(_usage) | set(rates):
            row = dict(_usage.get(key) or {})
            if key in rates:
                row["read_bps"], row["write_bps"] = (round(x) for x in rates[key])
            out[key] = row
        return out, _note["io"]
