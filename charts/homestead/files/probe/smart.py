#!/usr/bin/env python3
"""Narrow HTTP wrapper around smartctl for Homestead's optional helper."""
import hashlib, hmac, json, os, re, subprocess, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

SYS = "/host/sys"
DEV = "/host/dev"
AUTH_STORE = "/auth/store.json"
NODE = os.environ.get("NODE_NAME", "")

def read(path):
    try:
        with open(path) as handle:
            return handle.read().strip()
    except Exception:
        return ""

def disks():
    rows = []
    base = os.path.join(SYS, "class/block")
    try:
        names = sorted(os.listdir(base))
    except Exception:
        return rows
    for name in names:
        block = os.path.join(base, name)
        if not os.path.isdir(os.path.join(block, "device")):
            continue
        if os.path.exists(os.path.join(block, "partition")):
            continue
        model = read(os.path.join(block, "device/model"))
        vendor = read(os.path.join(block, "device/vendor"))
        if vendor.upper() in ("IET", "LIO-ORG") or "VIRTUAL-DISK" in model.upper():
            continue
        path = os.path.join(DEV, name)
        if os.path.exists(path):
            rows.append((name, path))
    return rows

def device_type(name):
    # smartctl normally infers this from a /dev path. The host device tree
    # is deliberately mounted at /host/dev, so make the transport explicit.
    if name.startswith("nvme"):
        return "nvme"
    block = os.path.join(SYS, "class/block", name, "device")
    vendor = read(os.path.join(block, "vendor")).upper()
    model = read(os.path.join(block, "model")).upper()
    if vendor == "ATA" or model.startswith("ATA "):
        return "sat"
    return "scsi"

def run(args, timeout=15):
    done = subprocess.run(["smartctl", *args], capture_output=True, text=True,
                          timeout=timeout, check=False)
    try:
        value = json.loads(done.stdout or "{}")
    except Exception:
        value = {}
    messages = [str(row.get("string") or "") for row in value.get("messages", [])]
    if done.stderr.strip():
        messages.append(done.stderr.strip())
    return done.returncode, value, "; ".join(x for x in messages if x)[:500]

def raw_attr(data, attr_id):
    table = ((data.get("ata_smart_attributes") or {}).get("table") or [])
    item = next((row for row in table if int(row.get("id", -1)) == attr_id), None)
    raw = (item or {}).get("raw") or {}
    value = raw.get("value")
    if value is None:
        match = re.search(r"-?\d+", str(raw.get("string") or ""))
        value = int(match.group()) if match else None
    return value

def norm_attr(data, attr_id):
    """An attribute's normalised value and how far it is above failing.

    SMART attributes are reported both raw and normalised. The normalised
    value counts down from 100 towards a manufacturer threshold, so it is
    the closest thing a drive gives to a percentage of life left.
    """
    table = ((data.get("ata_smart_attributes") or {}).get("table") or [])
    item = next((row for row in table if int(row.get("id", -1)) == attr_id), None)
    if not item:
        return None, None
    value, thresh = item.get("value"), (item.get("thresh") or 0)
    if value is None:
        return None, None
    span = max(1, 100 - int(thresh))
    margin = max(0, min(100, round((int(value) - int(thresh)) / span * 100)))
    return int(value), margin

def wear(data, nvme):
    """How much life the drive says it has left, and what said so.

    NVMe counts endurance used; SATA SSDs carry a life-left attribute;
    spinning disks have no such figure, so the closest honest answer is
    how near the worst attribute is to its failure threshold.
    """
    used = nvme.get("percentage_used")
    if used is not None:
        return {"life_pct": max(0, 100 - int(used)), "basis": "NVMe endurance used",
                "spare_pct": nvme.get("available_spare"),
                "spare_floor_pct": nvme.get("available_spare_threshold")}
    # 231 SSD_Life_Left, 177 Wear_Leveling_Count, 233 Media_Wearout_Indicator
    for attr_id, label in ((231, "SSD life left"), (233, "media wearout indicator"),
                           (177, "wear levelling count")):
        value, _ = norm_attr(data, attr_id)
        if value is not None:
            return {"life_pct": max(0, min(100, value)), "basis": label,
                    "spare_pct": None, "spare_floor_pct": None}
    table = ((data.get("ata_smart_attributes") or {}).get("table") or [])
    margins = []
    for row in table:
        # Only attributes the drive itself says predict failure.
        if not ((row.get("flags") or {}).get("prefailure")):
            continue
        _, margin = norm_attr(data, int(row.get("id", -1)))
        if margin is not None:
            margins.append(margin)
    if margins:
        return {"life_pct": min(margins), "basis": "worst pre-failure attribute",
                "spare_pct": None, "spare_floor_pct": None}
    return {"life_pct": None, "basis": "", "spare_pct": None, "spare_floor_pct": None}

def test_rows(data):
    table = ((data.get("ata_smart_self_test_log") or {}).get("standard") or {}).get("table") or []
    if not table:
        table = (data.get("nvme_self_test_log") or {}).get("table") or []
    if not table:
        table = [value for key, value in sorted(data.items())
                 if key.startswith("scsi_self_test_") and isinstance(value, dict)]
    out = []
    for row in table[:20]:
        status = row.get("status") or row.get("result") or {}
        text = status.get("string") if isinstance(status, dict) else str(status)
        test = row.get("type") or row.get("self_test_code") or {}
        test = test.get("string") if isinstance(test, dict) else str(test)
        hours = row.get("lifetime_hours") or (row.get("power_on_time") or {}).get("hours")
        signature = f"{test}|{text}|{hours}|{row.get('lba_of_first_error', '')}"
        out.append({"type": test or "Self-test", "status": text or "Unknown",
                    "lifetime_hours": hours, "signature": signature})
    return out

def report(name, path):
    code, data, message = run(["-d", device_type(name), "-a", "-j", path])
    device = data.get("device") or {}
    protocol = str(device.get("protocol") or "")
    nvme = data.get("nvme_smart_health_information_log") or {}
    scsi_errors = data.get("scsi_error_counter_log") or {}
    scsi = protocol.lower() in ("scsi", "sas") or bool(scsi_errors)
    support = data.get("smart_support") or {}
    available = bool(support.get("available") or nvme or data.get("smart_status"))
    temperature = (data.get("temperature") or {}).get("current")
    if temperature is None:
        temperature = nvme.get("temperature")
    if temperature is None:
        temperature = (data.get("scsi_temperature") or {}).get("current")
    power_hours = (data.get("power_on_time") or {}).get("hours")
    if power_hours is None:
        power_hours = nvme.get("power_on_hours")
    if power_hours is None:
        power_hours = raw_attr(data, 9)
    if temperature is None:
        temperature = raw_attr(data, 194)
    if temperature is None:
        temperature = raw_attr(data, 190)
    health = "unknown"
    if isinstance(data.get("smart_status"), dict):
        health = "passed" if data["smart_status"].get("passed") else "failed"
    elif nvme:
        health = "passed" if int(nvme.get("critical_warning", 0) or 0) == 0 else "failed"
    polling = ((data.get("ata_smart_data") or {}).get("self_test") or {}).get("polling_minutes") or {}
    nvme_log = data.get("nvme_self_test_log") or {}
    supported = []
    if polling.get("short") or nvme_log or (scsi and available):
        supported.append("short")
    if polling.get("extended") or nvme_log or (scsi and available):
        supported.append("long")
    current = ((data.get("ata_smart_data") or {}).get("self_test") or {}).get("status") or {}
    current_text = str(current.get("string") or "")
    remaining = current.get("remaining_percent")
    if not current_text and nvme_log:
        operation = nvme_log.get("current_self_test_operation") or {}
        completion = nvme_log.get("current_self_test_completion_percent")
        current_text = str(operation.get("string") or "")
        inactive = "no self-test" in current_text.lower() or "not running" in current_text.lower()
        remaining = (100 - int(completion)
                     if completion is not None and not inactive else None)
    inactive = "no self-test" in current_text.lower() or "not running" in current_text.lower()
    active = not inactive and ("in progress" in current_text.lower()
                               or (remaining is not None and int(remaining) > 0))
    rows = test_rows(data)
    size = (data.get("user_capacity") or {}).get("bytes")
    unavailable = ""
    if not available:
        unavailable = message or "This drive or bridge does not expose SMART data"
    if code & 3 and not data:
        unavailable = message or "smartctl could not open this device"
    scsi_uncorrected = sum(int((scsi_errors.get(kind) or {}).get("total_uncorrected_errors", 0) or 0)
                           for kind in ("read", "write", "verify"))
    return {
        "name": name, "path": "/dev/" + name, "available": available,
        "unavailable_reason": unavailable, "protocol": protocol,
        "model": data.get("model_name") or data.get("product") or "",
        "serial": data.get("serial_number") or "",
        "firmware": data.get("firmware_version") or data.get("revision") or "",
        "capacity_gb": round(int(size or 0) / (1024 ** 3), 1),
        "smart_enabled": support.get("enabled"), "health": health,
        "temperature_c": temperature, "power_on_hours": power_hours,
        "reallocated": raw_attr(data, 5), "pending": raw_attr(data, 197),
        "uncorrectable": raw_attr(data, 198),
        "error_count": ((data.get("ata_smart_error_log") or {}).get("summary") or {}).get("count",
                         nvme.get("num_err_log_entries", scsi_uncorrected if scsi else None)),
        "media_errors": nvme.get("media_errors"),
        "wear": wear(data, nvme),
        # An NVMe drive keeps a different set of books from an ATA one. Showing
        # its own counters beats four rows of "unsupported" ATA attributes.
        "nvme": {
            "available_spare": nvme.get("available_spare"),
            "available_spare_threshold": nvme.get("available_spare_threshold"),
            "percentage_used": nvme.get("percentage_used"),
            "unsafe_shutdowns": nvme.get("unsafe_shutdowns"),
            "data_units_written": nvme.get("data_units_written"),
            "critical_warning": nvme.get("critical_warning"),
        } if nvme else None,
        "supported_tests": supported,
        "test": {"active": active, "status": current_text or "No self-test running",
                 "remaining_percent": remaining},
        "self_tests": rows,
        "expected_seconds": {"short": int(polling.get("short", 2) or 2) * 60,
                             "long": int(polling.get("extended", 10) or 10) * 60},
        "smartctl_messages": message,
    }

def one(name):
    match = next((row for row in disks() if row[0] == name), None)
    if not match:
        raise ValueError("disk was not found on this host")
    return report(*match)

def signature_ok(raw, stamp, supplied):
    try:
        if abs(time.time() - int(stamp)) > 30:
            return False
        with open(AUTH_STORE) as handle:
            key = json.load(handle).get("signing_key", "").encode()
        expected = hmac.new(key, stamp.encode() + b"." + raw, hashlib.sha256).hexdigest()
        return bool(key and hmac.compare_digest(expected, supplied or ""))
    except Exception:
        return False

class H(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    def log_message(self, *args): pass
    def send_json(self, code, value):
        raw = json.dumps(value, separators=(",", ":")).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)
    def do_GET(self):
        try:
            if urlparse(self.path).path == "/healthz":
                return self.send_json(200, {"ok": True, "node": NODE})
            name = (parse_qs(urlparse(self.path).query).get("disk") or [""])[0]
            if name:
                return self.send_json(200, {"node": NODE, "disk": one(name)})
            return self.send_json(200, {"node": NODE,
                "disks": [report(name, path) for name, path in disks()]})
        except Exception as error:
            return self.send_json(400, {"error": str(error)[:500]})
    def do_POST(self):
        length = min(4096, int(self.headers.get("Content-Length") or 0))
        raw = self.rfile.read(length)
        if not signature_ok(raw, self.headers.get("X-Homestead-Time", ""),
                            self.headers.get("X-Homestead-Signature", "")):
            return self.send_json(403, {"error": "request signature is invalid"})
        try:
            body = json.loads(raw.decode())
            if body.get("node") != NODE:
                raise ValueError("node does not match this helper")
            name = str(body.get("disk") or "")
            test = str(body.get("test") or "").lower()
            if test not in ("short", "long"):
                raise ValueError("test must be short or long")
            before = one(name)
            if test not in before.get("supported_tests", []):
                raise ValueError(f"this disk does not advertise a {test} self-test")
            if (before.get("test") or {}).get("active"):
                raise ValueError("a self-test is already running")
            path = next(path for disk, path in disks() if disk == name)
            code, data, message = run(
                ["-d", device_type(name), "-t", test, "-j", path], timeout=20)
            if code & 3:
                raise ValueError(message or "smartctl could not start the test")
            expected = int((before.get("expected_seconds") or {}).get(test, 0) or 0)
            return self.send_json(200, {"ok": True, "node": NODE,
                "disk": name, "test": test, "expected_seconds": expected,
                "baseline": ((before.get("self_tests") or [{}])[0].get("signature") or ""),
                "started_epoch": int(time.time()),
                "message": message or f"{test.title()} SMART test started"})
        except Exception as error:
            return self.send_json(400, {"error": str(error)[:500]})

if __name__ == "__main__":
    ThreadingHTTPServer(("0.0.0.0", 9100), H).serve_forever()
