"""When storage fills up: a forecast from how it has grown.

Usage alerts say a volume is full now. This says it will be, while there is
time to do something: a daily sample of each volume's filesystem (what kubelet
reports, never Longhorn's block footprint), each Longhorn disk and the pool as
a whole, kept for KEEP_DAYS, and a straight line through the last FIT_DAYS.

There is no estimate until MIN_DAYS of samples span at least MIN_SPAN days, nor
when usage is flat or shrinking, nor when it moves too irregularly for a line
to say anything (R squared below MIN_FIT): a guess there would be noise. An
estimate within WARN_DAYS raises an alert, critical within CRIT_DAYS.

A day keeps its latest sample. The file is written only when a day's figure
changes by more than a hair, so a quiet hour writes nothing.
"""
import calendar
import json
import os
import threading
import time

import homestead_shared as SHARED

DATA_DIR = "/data"
KEEP_DAYS = 90
FIT_DAYS = 30
MIN_DAYS = 7
MIN_SPAN = 6
MIN_FIT = 0.6
WARN_DAYS = 14
CRIT_DAYS = 3
SHOW_DAYS = 60            # the page lists what fills within this
NOISE = 0.002             # a change under 0.2% of capacity is not worth a write
_lock = threading.Lock()


def bind(data_dir="/data"):
    global DATA_DIR
    DATA_DIR = data_dir


def _path():
    return os.path.join(DATA_DIR, "storage-forecast.json")


def _load():
    try:
        with open(_path(), encoding="utf-8") as handle:
            data = json.load(handle)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _day(now):
    return time.strftime("%Y-%m-%d", time.gmtime(now))


def _day_number(day):
    return int(calendar.timegm(time.strptime(day, "%Y-%m-%d")) // 86400)


def series_rows(volumes, capacity):
    """What is sampled now: {key: {label, kind, used, capacity, ...}} for each
    volume with a filesystem reading, each Longhorn disk, and the pool."""
    rows = {}
    for v in volumes or []:
        fs = v.get("filesystem") or {}
        if not fs.get("capacity_bytes"):
            continue
        rows[f"volume:{v['name']}"] = {"kind": "volume", "label": v.get("pvc_name") or v["name"], "namespace": v.get("namespace", ""),
                                       "volume": v["name"], "used": int(fs["used_bytes"]), "capacity": int(fs["capacity_bytes"])}
    total_used = total_size = 0
    for node in (capacity or {}).get("nodes") or []:
        for disk in node.get("disks") or []:
            # The disk as it physically is: what it holds against what it holds plus what is free.
            used = float(disk.get("used_gb") or 0)
            size = used + float(disk.get("free_gb") or 0)
            if size <= 0:
                continue
            total_used, total_size = total_used + used, total_size + size
            key = f"disk:{node['name']}:{disk.get('id') or disk.get('path') or ''}"
            rows[key] = {"kind": "disk", "label": f"{node['name']} · {disk.get('path') or disk.get('id') or 'disk'}",
                         "node": node["name"], "used": int(used * 1024**3), "capacity": int(size * 1024**3)}
    if total_size:
        rows["pool"] = {"kind": "pool", "label": "Longhorn storage, all hosts", "used": int(total_used * 1024**3),
                        "capacity": int(total_size * 1024**3)}
    return rows


def observe(volumes, capacity, now=None):
    """Keep today's figure for each; write only when one moved."""
    now = now or time.time()
    today, oldest = _day(now), _day(now - KEEP_DAYS * 86400)
    rows = series_rows(volumes, capacity)
    with _lock:
        data = _load()
        series = data.setdefault("series", {})
        changed = False
        for key, row in rows.items():
            entry = series.setdefault(key, {"days": {}})
            before = entry["days"].get(today)
            if before is None or abs(before - row["used"]) > NOISE * max(1, row["capacity"]) \
                    or entry.get("capacity") != row["capacity"]:
                entry["days"][today] = row["used"]
                changed = True
            meta = {k: v for k, v in row.items() if k != "used"}
            if any(entry.get(k) != v for k, v in meta.items()):
                entry.update(meta)
                changed = True
            entry["seen"] = today
        for key in list(series):
            days = {d: v for d, v in series[key]["days"].items() if d >= oldest}
            if days != series[key]["days"]:
                series[key]["days"], changed = days, True
            if not days or series[key].get("seen", "") < oldest:
                series.pop(key)
                changed = True
        if changed:
            SHARED.write_json(_path(), {"format": 1, "series": series}, separators=(",", ":"), sort_keys=True)
        return series


def fit(days, capacity, now=None):
    """A line through the last FIT_DAYS: {days_left, per_day, why}. days_left is
    None with a reason when there is nothing to say."""
    now = now or time.time()
    first = _day(now - FIT_DAYS * 86400)
    points = sorted((_day_number(d), v) for d, v in (days or {}).items() if d >= first)
    if len(points) < MIN_DAYS or points[-1][0] - points[0][0] < MIN_SPAN:
        return {"days_left": None, "per_day": None, "why": f"needs {MIN_DAYS} days of samples"}
    n = len(points)
    mx, my = sum(x for x, _ in points) / n, sum(y for _, y in points) / n
    sxx = sum((x - mx) ** 2 for x, _ in points)
    sxy = sum((x - mx) * (y - my) for x, y in points)
    syy = sum((y - my) ** 2 for _, y in points)
    slope = sxy / sxx if sxx else 0.0
    if slope <= max(1024**2, capacity * 0.0005):        # under 1 MiB or 0.05% a day: not filling
        return {"days_left": None, "per_day": slope, "why": "not growing"}
    r2 = (sxy * sxy) / (sxx * syy) if sxx and syy else 0.0
    if r2 < MIN_FIT:
        return {"days_left": None, "per_day": slope, "why": "too irregular to forecast"}
    latest = points[-1][1]
    left = max(0.0, (capacity - latest) / slope)
    return {"days_left": round(left, 1), "per_day": slope, "why": ""}


def report(now=None):
    """Every series with its forecast, soonest first."""
    now = now or time.time()
    with _lock:
        series = _load().get("series", {})
    out = []
    for key, entry in series.items():
        days = entry.get("days") or {}
        if not days:
            continue
        latest_day = max(days)
        capacity = int(entry.get("capacity") or 0)
        f = fit(days, capacity, now) if capacity else {"days_left": None, "per_day": None, "why": "no size known"}
        out.append({"key": key, "kind": entry.get("kind", ""), "label": entry.get("label", key),
                    "namespace": entry.get("namespace", ""), "volume": entry.get("volume", ""), "node": entry.get("node", ""),
                    "used": days[latest_day], "capacity": capacity,
                    "pct": round(days[latest_day] / capacity * 100, 1) if capacity else None,
                    "days": len(days), "days_left": f["days_left"], "per_day": f["per_day"], "why": f["why"],
                    "full_on": _day(now + f["days_left"] * 86400) if f["days_left"] is not None else ""})
    return sorted(out, key=lambda r: (r["days_left"] is None, r["days_left"] if r["days_left"] is not None else 0, r["label"]))


def alert_facts(rows):
    """Something fills within WARN_DAYS: degraded; within CRIT_DAYS: critical."""
    facts = []
    for r in rows or []:
        left = r.get("days_left")
        if left is None or left > WARN_DAYS:
            continue
        what = {"volume": f"Volume {r['label']}", "disk": f"Disk {r['label']}", "pool": "Longhorn storage"}.get(r["kind"], r["label"])
        when = "within a day" if left < 1 else f"in about {round(left)} day{'s' if round(left) != 1 else ''}"
        rate = f"{r['per_day'] / 1024**3:.1f} GB a day" if r.get("per_day") else ""
        facts.append({"key": f"forecast:{r['key']}", "category": "outage" if left <= CRIT_DAYS else "degraded",
                      "severity": "critical" if left <= CRIT_DAYS else "degraded",
                      "title": f"{what} fills up {when}",
                      "body": f"It is {r['pct']}% full and has grown {rate} over the last weeks; at that rate it is full by {r['full_on']}.",
                      "resolved": f"{what} is no longer forecast to fill within {WARN_DAYS} days",
                      "href": "/storage" if r["kind"] == "volume" else "/nodes", "signals": {"days_left": round(left)}})
    return facts
