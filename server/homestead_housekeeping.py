"""Homestead's own data: what it keeps on its volume, for how long, and tidying.

Homestead records history - monitoring, stats, changes, forecasts, jobs,
updates, sign-ins, console sessions - and caches logos. Most stores trim
themselves (each says how long it keeps). This keeps the rest in bounds, once
a day on the leader and on demand:

* append-only logs (image updates, console sessions) keep LOG_DAYS and at most
  LOG_LINES lines;
* a cached logo nothing refers to any more - no app, VM or portal link - is
  removed once it has been unused for ICON_DAYS;
* a temporary file a write left behind, older than an hour, is removed.

When the volume passes PRESSURE_PCT full the logs keep half as much and unused
logos go at once, and an alert names the largest stores: by then the volume
needs to be larger, or something is keeping more than it should.
"""
import glob
import json
import os
import re
import time

import homestead_shared as SHARED

DATA_DIR = "/data"
LOG_DAYS = 365
LOG_LINES = 5000
ICON_DAYS = 30
TEMP_AGE = 3600
PRESSURE_PCT = 80
CRIT_PCT = 95
_last = {"at": 0, "result": None}

# Every store Homestead keeps, so the page can say what each is for and how
# long it lasts. Patterns are relative to the data volume.
STORES = [
    ("Monitoring history", ["uptime.json"], "hour by hour for 30 days"),
    ("Long-term stats", ["history.json", "history-live.json"], "live for an hour, five-minutely for 2 days, hourly for 90 days"),
    ("Change history", ["change-history.json"], "50 changes per app, for 180 days"),
    ("Storage forecast", ["storage-forecast.json"], "a figure a day for 90 days"),
    ("Alerts", ["alerts.json"], "what is active, and recent alerts"),
    ("Schedules", ["schedules.json"], "the last stop or start of each schedule"),
    ("Jobs", ["operations-v2.json", "operations*.json"], "the last 100 jobs"),
    ("Image update history", ["image-update-history.jsonl"], f"{LOG_DAYS} days, at most {LOG_LINES} entries"),
    ("Console sessions", ["console-audit.jsonl"], f"{LOG_DAYS} days, at most {LOG_LINES} entries"),
    ("Sign-ins", ["signins.jsonl"], "the last 5,000"),
    ("Logos", ["icons/*"], f"while something uses them, then {ICON_DAYS} days"),
    ("Bug reports", ["diagnostics/*"], "until they expire"),
    ("Host ports and monitoring state", ["host-ports.json", "host-os.json", "node-parity.json", "root-guard.json"], "current state only"),
]
LOGS = ("image-update-history.jsonl", "console-audit.jsonl")
# What an interrupted write leaves: homestead_shared's <file>.<pid>.<thread>.tmp,
# a plain .tmp, the icon cache's .icon-*, and this module's own.
_TEMP = re.compile(r"(\.tmp$|^\.icon-|\.housekeeping$)")


def bind(data_dir="/data"):
    global DATA_DIR
    DATA_DIR = data_dir


def _all_files():
    """Every file on the volume, hidden ones too (glob skips them)."""
    for root, dirs, files in os.walk(DATA_DIR):
        dirs[:] = [d for d in dirs if d != "lost+found"]
        for name in files:
            yield os.path.join(root, name)


def _files(pattern):
    return [p for p in glob.glob(os.path.join(DATA_DIR, pattern)) if os.path.isfile(p)]


def _size(paths):
    total = 0
    for p in paths:
        try:
            total += os.path.getsize(p)
        except OSError:
            pass
    return total


def volume():
    """The data volume: size, used and free bytes, and how full."""
    try:
        st = os.statvfs(DATA_DIR)
    except (OSError, AttributeError):
        return {"size": 0, "used": 0, "free": 0, "pct": None}
    size, free = st.f_blocks * st.f_frsize, st.f_bavail * st.f_frsize
    used = size - st.f_bfree * st.f_frsize
    return {"size": size, "used": used, "free": free, "pct": round(used / size * 100, 1) if size else None}


def report():
    """Each store: what it is, its size, how many files, how long it keeps."""
    claimed, rows = set(), []
    for label, patterns, kept in STORES:
        paths = sorted({p for pattern in patterns for p in _files(pattern)})
        claimed.update(paths)
        rows.append({"label": label, "size": _size(paths), "files": len(paths), "kept": kept})
    everything = list(_all_files())
    other = [p for p in everything if p not in claimed]
    rows.append({"label": "Settings and other state", "size": _size(other), "files": len(other), "kept": "as long as it is needed"})
    rows.sort(key=lambda r: -r["size"])
    return {"volume": volume(), "stores": rows, "total": sum(r["size"] for r in rows),
            "last": _last["result"], "last_at": _last["at"], "pressure_pct": PRESSURE_PCT}


def _trim_log(path, days, lines, now):
    """Keep the entries of the last days, and at most lines of them."""
    try:
        with open(path, encoding="utf-8") as handle:
            rows = handle.readlines()
    except OSError:
        return 0
    cutoff = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now - days * 86400))

    def recent(line):
        try:
            at = json.loads(line).get("at")
        except ValueError:
            return False
        if isinstance(at, (int, float)):
            return at >= now - days * 86400
        return not isinstance(at, str) or at >= cutoff
    kept = [r for r in rows if recent(r)][-lines:]
    if len(kept) == len(rows):
        return 0
    before = os.path.getsize(path)
    with SHARED.write_scope(path):
        temp = path + ".housekeeping"
        with open(temp, "w", encoding="utf-8") as handle:
            handle.writelines(kept)
        os.replace(temp, path)
    return max(0, before - os.path.getsize(path))


def tidy(referenced_icons, now=None):
    """Keep every store in bounds. referenced_icons: the /api/icons/<file> names
    something still uses. Returns what was removed."""
    now = now or time.time()
    vol = volume()
    pressure = vol["pct"] is not None and vol["pct"] >= PRESSURE_PCT
    days, lines = (LOG_DAYS // 2, LOG_LINES // 2) if pressure else (LOG_DAYS, LOG_LINES)
    freed, removed, notes = 0, 0, []
    for name in LOGS:
        path = os.path.join(DATA_DIR, name)
        if os.path.exists(path):
            got = _trim_log(path, days, lines, now)
            if got:
                freed += got
                notes.append(f"trimmed {name}")
    keep = {os.path.basename(str(r)) for r in referenced_icons or []}
    for path in _files("icons/*"):
        name = os.path.basename(path)
        try:
            idle = now - os.path.getmtime(path)
        except OSError:
            continue
        if name in keep or (not pressure and idle < ICON_DAYS * 86400):
            continue
        try:
            size = os.path.getsize(path)
            os.remove(path)
            freed, removed = freed + size, removed + 1
        except OSError:
            pass
    if removed:
        notes.append(f"removed {removed} unused logo{'s' if removed != 1 else ''}")
    temps = 0
    for path in _all_files():
        base = os.path.basename(path)
        if not _TEMP.search(base):
            continue
        try:
            if now - os.path.getmtime(path) < TEMP_AGE:
                continue
            size = os.path.getsize(path)
            os.remove(path)
            freed, temps = freed + size, temps + 1
        except OSError:
            pass
    if temps:
        notes.append(f"removed {temps} file{'s' if temps != 1 else ''} an interrupted write left")
    result = {"freed": freed, "notes": notes, "pressure": pressure}
    _last.update(at=round(now), result=result)
    return result


def alert_facts(rep):
    """The volume is filling: name what is using it."""
    vol = (rep or {}).get("volume") or {}
    pct = vol.get("pct")
    if pct is None or pct < PRESSURE_PCT:
        return []
    biggest = ", ".join(f"{s['label']} ({s['size'] / 1024**2:.0f} MB)" for s in (rep.get("stores") or [])[:3])
    return [{"key": "housekeeping:volume", "category": "outage" if pct >= CRIT_PCT else "degraded",
             "severity": "critical" if pct >= CRIT_PCT else "degraded",
             "title": f"Homestead's data volume is {pct:.0f}% full",
             "body": f"The most is in {biggest}. Homestead now keeps less of its logs and logos; if it keeps filling, "
                     "make its data volume larger under Volumes.",
             "resolved": "Homestead's data volume has room again", "href": "/settings#homestead-data",
             "signals": {"pct": round(pct)}}]
