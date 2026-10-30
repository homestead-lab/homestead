"""SMART inventory and self-test orchestration for the optional node probe.

The ordinary node-probe container remains unprivileged.  A separate, opt-in
sidecar owns raw-device access and accepts signed test requests only from
Homestead.  This module keeps device validation and Kubernetes pod discovery in
one place so the browser never receives a helper credential.
"""
import hashlib
import homestead_names as NAMES
import homestead_shared as SHARED
import hmac
import json
import os
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request


kget = None
signing_key = None
DEFAULT_NAMESPACE = "lab"


def bind(_kget, namespace, _signing_key):
    global kget, signing_key, DEFAULT_NAMESPACE
    kget, signing_key, DEFAULT_NAMESPACE = _kget, _signing_key, namespace
    NAMES.bind(_kget)


def _name(value, label):
    value = str(value or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", value):
        raise ValueError(f"{label} is invalid")
    return value


def _probe(node):
    node = _name(node, "node")
    pods = NAMES.nodeprobe_pods(DEFAULT_NAMESPACE)
    pod = next((item for item in pods
                if item.get("spec", {}).get("nodeName") == node
                and item.get("status", {}).get("phase") == "Running"
                and item.get("status", {}).get("podIP")), None)
    if not pod:
        raise ValueError(f"the SMART helper is not running on {node}")
    statuses = {row.get("name"): row for row in
                pod.get("status", {}).get("containerStatuses", []) or []}
    smart = statuses.get("smart") or {}
    if not smart.get("ready"):
        waiting = ((smart.get("state") or {}).get("waiting") or {})
        reason = waiting.get("message") or waiting.get("reason") or "not ready"
        raise ValueError(f"the SMART helper on {node} is {reason}")
    return pod["status"]["podIP"]


def signed(message):
    """The helper's signature headers for one request. A read signs "GET " and
    its path, a change signs its body, so neither stands in for the other."""
    stamp = str(int(time.time()))
    key = signing_key()
    if isinstance(key, str):
        key = key.encode()
    return {"X-Homestead-Time": stamp,
            "X-Homestead-Signature": hmac.new(key, stamp.encode() + b"." + message,
                                              hashlib.sha256).hexdigest()}


def _request(node, disk=None, body=None):
    ip = _probe(node)
    path = "/" + ("?" + urllib.parse.urlencode({"disk": disk}) if disk else "")
    url = f"http://{ip}:9100{path}"
    raw = json.dumps(body, separators=(",", ":")).encode() if body is not None else None
    headers = {"Accept": "application/json"}
    if raw is not None:
        headers["Content-Type"] = "application/json"
    headers.update(signed(raw if raw is not None else b"GET " + path.encode()))
    request = urllib.request.Request(url, data=raw,
                                     method="POST" if raw is not None else "GET",
                                     headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=45 if raw is not None else 20) as response:
            return json.loads(response.read().decode())
    except urllib.error.HTTPError as error:
        try:
            detail = json.loads(error.read().decode()).get("error")
        except Exception:
            detail = None
        raise ValueError(detail or f"SMART helper returned HTTP {error.code}")


def inventory(node):
    result = _request(node)
    if not isinstance(result, dict) or not isinstance(result.get("disks"), list):
        raise ValueError("the SMART helper returned an invalid inventory")
    return result


def disk(node, name):
    name = _name(name, "disk")
    result = _request(node, name)
    row = result.get("disk") if isinstance(result, dict) else None
    if not isinstance(row, dict) or row.get("name") != name:
        raise ValueError(f"disk {name} was not found on {node}")
    # Read awake (by name, around a self-test): the reading the drive keeps while it sleeps.
    return keep_last(node, {name: row}, whole=False)[name]


# The Hosts page's SMART view answers from a host's last reading. The probe
# loop reads every host's SMART every five minutes and hands it over
# (remember, saying how long it holds), so a page normally reads nothing; a
# host without one is read once, by one request, and the rest wait for it.
READING_SECONDS = 300    # a reading the page took itself
ACTIVE_SECONDS = 15      # while a self-test runs on the host: the page's refresh, so its progress moves
FAILED_SECONDS = 60      # a failed read is answered from memory this long
_clock = time.monotonic
_wall = time.time
_awake = {}              # node -> {name: the drive's last reading taken awake} (#974)
STORE = "smart-readings.json"   # the same, on the data volume, so a restart or another copy keeps it
_store_lock = SHARED.SharedLock("smart-readings")
_host_locks = [threading.Lock() for _ in range(16)]       # one change of a host's kept readings at a time
_saved_at = {}           # node -> _clock() of its last save
_store_warned = [""]     # the last save error said, so a broken volume is reported once
_readings = {}           # node -> {"at", "keep", "rows": {name: row}, "error"}
_readings_lock = threading.Lock()
_reading_locks = [threading.Lock() for _ in range(16)]   # one read per host at a time


def remember(node, rows, keep=READING_SECONDS):
    """A fresh reading of every drive on a host ({name: row}), from the probe
    loop, good for keep seconds."""
    with _readings_lock:
        _readings[node] = {"at": _clock(), "keep": keep, "rows": dict(rows), "error": None}


def _store_path():
    return os.path.join(SHARED.DIR, STORE)


def _stored(node):
    """A host's kept readings from the data volume ({} when there are none or it cannot be read)."""
    try:
        value = SHARED.read_json(_store_path(), {}).get(node)
    except Exception:
        return {}
    return value if isinstance(value, dict) else {}


def _newer(row, other):
    return other if isinstance(other, dict) and int(other.get("read_at") or 0) > int(row.get("read_at") or 0) else row


def _store(node, kept):
    """Save a host's kept readings (None drops the host) and answer what was
    saved. Every copy of Homestead reads every host, so a drive's reading
    another copy saved later is kept over this one's. A data volume that
    cannot be written costs only the readings after a restart."""
    def change(value):
        if not kept:
            value.pop(node, None)
            return {}
        theirs = value.get(node) if isinstance(value.get(node), dict) else {}
        value[node] = {name: _newer(row, theirs.get(name)) for name, row in kept.items()}
        return value[node]
    try:
        saved = SHARED.update_file(_store_path(), change, _store_lock, {}, separators=(",", ":"))
        _store_warned[0] = ""
        return saved
    except Exception as error:
        said = f"SMART readings: not saved: {str(error)[:160]}"
        if said != _store_warned[0]:
            _store_warned[0] = said
            print(said, flush=True)
        return kept


def _serial(value):
    return str(value or "").strip().upper()


def _same_drive(row, serial):
    """Whether a kept reading is of the drive now in its slot: the probe's
    serial for it, when both have one, matches the reading's."""
    old, new = _serial(row.get("serial")), _serial(serial)
    return not old or not new or old == new or old in new or new in old


def _shape(kept):
    return {name: _serial(row.get("serial")) for name, row in kept.items()}


def keep_last(node, rows, whole=True, serials=None):
    """A host's rows ({name: row}) with each sleeping drive's last reading kept.

    The regular read leaves a drive the host spun down asleep, and the helper
    then has nothing new to say about it. Its last reading taken awake stands
    in, marked asleep, with the temperature unknown: a figure from before it
    slept says nothing about it now. A reading whose serial is not the one
    the probe now sees in that slot (serials, {name: serial}) is of a drive
    that was replaced, and is dropped. With whole, rows are every drive on
    the host and drives no longer there are forgotten; otherwise (one drive
    read by name) the other drives' readings stay.

    The kept readings live on the data volume too, so a restart or another
    copy of Homestead has them; a host's are saved when its drives change and
    otherwise at most every READING_SECONDS."""
    serials = serials or {}
    with _host_locks[hash(node) % len(_host_locks)]:
        with _readings_lock:
            known = node in _awake
        stored = None if known else _stored(node)
        with _readings_lock:
            if stored is not None and node not in _awake:
                _awake[node] = stored
            last = _awake.get(node) or {}
            kept, out = ({} if whole else dict(last)), {}
            for name, row in rows.items():
                if not isinstance(row, dict) or not row.get("asleep"):
                    if isinstance(row, dict):
                        row = dict(row, read_at=int(_wall()))
                        kept[name] = row
                    out[name] = row
                elif name in last and _same_drive(last[name], serials.get(name)):
                    kept[name] = last[name]
                    out[name] = dict(last[name], asleep=True, temperature_c=None)
                else:
                    kept.pop(name, None)
                    out[name] = row
            due = (_shape(kept) != _shape(last)
                   or _clock() - _saved_at.get(node, float("-inf")) >= READING_SECONDS)
            _awake[node] = kept
        if due and kept != last:
            saved = _store(node, kept)
            with _readings_lock:
                _saved_at[node] = _clock()
                if _awake.get(node) is kept and isinstance(saved, dict):
                    _awake[node] = dict(saved)
    return out


def forget(node):
    """Drop a host's last reading, so the next view reads it again (a self-test started or ended)."""
    with _readings_lock:
        _readings.pop(node, None)


def drop_host(node):
    """A host gone from the cluster: its readings, kept ones too, in memory and on the data volume."""
    with _host_locks[hash(node) % len(_host_locks)]:
        with _readings_lock:
            _readings.pop(node, None)
            _awake.pop(node, None)
            _saved_at.pop(node, None)
        _store(node, None)


def reset():
    """Forget every reading held in memory (tests; the data volume's copy stays, as across a restart)."""
    with _readings_lock:
        _readings.clear()
        _awake.clear()
        _saved_at.clear()


def _fresh(node):
    with _readings_lock:
        entry = _readings.get(node)
    if entry is None:
        return None
    age = _clock() - entry["at"]
    if entry["error"]:
        keep = FAILED_SECONDS
    elif any((row.get("test") or {}).get("active") for row in entry["rows"].values()):
        keep = ACTIVE_SECONDS
    else:
        keep = entry["keep"]
    return entry if age < keep else None


def _reading(node):
    node = _name(node, "node")
    entry = _fresh(node)
    if entry is None:
        with _reading_locks[hash(node) % len(_reading_locks)]:
            entry = _fresh(node)
            if entry is None:
                try:
                    rows = keep_last(node, {row.get("name"): row for row in inventory(node)["disks"]})
                    entry = {"at": _clock(), "keep": READING_SECONDS, "rows": rows, "error": None}
                except Exception as error:
                    entry = {"at": _clock(), "keep": FAILED_SECONDS, "rows": {}, "error": error}
                with _readings_lock:
                    now = _clock()
                    for old in [key for key, row in _readings.items()
                                if now - row["at"] >= max(row["keep"], READING_SECONDS)]:
                        del _readings[old]
                    _readings[node] = entry
    if entry["error"]:
        raise entry["error"]
    return entry


def latest(node):
    """Every drive on a host from its last reading, as inventory() answers."""
    entry = _reading(node)
    return {"node": node, "disks": [dict(row) for row in entry["rows"].values()]}


def latest_disk(node, name):
    """One drive from its host's last reading, as disk() answers."""
    name = _name(name, "disk")
    row = _reading(node)["rows"].get(name)
    if not isinstance(row, dict):
        raise ValueError(f"disk {name} was not found on {node}")
    return dict(row)


def start_test(node, name, test_type):
    node, name = _name(node, "node"), _name(name, "disk")
    test_type = str(test_type or "").lower()
    if test_type not in ("short", "long"):
        raise ValueError("SMART test type must be short or long")
    current = disk(node, name)
    if not current.get("available"):
        raise ValueError(current.get("unavailable_reason") or "SMART is unavailable for this disk")
    if test_type not in (current.get("supported_tests") or []):
        raise ValueError(f"this disk does not advertise a {test_type} SMART self-test")
    if (current.get("test") or {}).get("active"):
        raise ValueError("a SMART self-test is already running on this disk")
    result = _request(node, body={"node": node, "disk": name, "test": test_type})
    forget(node)
    expected = max(30, int(result.get("expected_seconds", 0) or 0))
    return {
        "ok": True, "node": node, "disk": name, "test": test_type,
        "expected_seconds": expected,
        "baseline": str(result.get("baseline") or ""),
        "started_epoch": int(result.get("started_epoch") or time.time()),
        "message": str(result.get("message") or f"{test_type.title()} SMART test started"),
    }


def abort_test(node, name):
    """Stop the self-test running on a disk: smartctl -X on its node."""
    node, name = _name(node, "node"), _name(name, "disk")
    try:
        result = _request(node, body={"node": node, "disk": name, "test": "abort"})
    except ValueError as error:
        if "short or long" in str(error):
            # A helper from before aborting existed only knows how to start.
            raise ValueError(f"the SMART helper on {node} is too old to abort a test: update it on the "
                             "node's page, or let the test finish") from error
        raise
    forget(node)
    return {"ok": True, "node": node, "disk": name,
            "message": str(result.get("message") or "SMART self-test aborted")}


def progress(ref):
    state, percent, message = _progress(ref)
    if state != "running":
        forget(ref["node"])      # the drive's page shows the result, not the reading from before
    return state, percent, message


def _progress(ref):
    report = disk(ref["node"], ref["disk"])
    test = report.get("test") or {}
    elapsed = max(0, int(time.time()) - int(ref.get("started_epoch", time.time())))
    expected = max(30, int(ref.get("expected_seconds", 30) or 30))
    if test.get("active"):
        remaining = test.get("remaining_percent")
        pct = (max(5, min(95, 100 - int(remaining))) if remaining is not None
               else max(5, min(90, round(elapsed / expected * 90))))
        return "running", pct, test.get("status") or f"SMART {ref['test']} test running"

    latest = (report.get("self_tests") or [{}])[0]
    signature = str(latest.get("signature") or "")
    changed = bool(signature and signature != str(ref.get("baseline") or ""))
    status = str(latest.get("status") or "").lower()
    if changed:
        passed = any(phrase in status for phrase in
                     ("completed without error", "completed successfully", "passed"))
        failed = not passed and any(word in status for word in
                                    ("fail", "error", "aborted", "interrupted", "unknown"))
        return ("failed" if failed else "succeeded"), 100, (
            latest.get("status") or f"SMART {ref['test']} test complete")
    if elapsed <= min(20, max(5, expected // 10)):
        return "running", max(2, min(10, round(elapsed / expected * 90))), \
            "Waiting for the drive to report the self-test"
    if elapsed > expected + 600:
        return "failed", 100, "The drive did not report a SMART self-test result"
    if elapsed >= expected:
        return "succeeded", 100, \
            "SMART self-test command completed; this device exposes no new result log entry"
    return "running", max(10, min(95, round(elapsed / expected * 90))), \
        f"SMART {ref['test']} test running"
