"""Long-term stats: how the cluster has been, kept whether or not anyone looks.

The dashboard's charts cover the last hour, in memory. This keeps the longer
view on the data volume, recorded by the leading replica in the background:

* a sample every five minutes, kept for two days;
* hourly averages and peaks, kept for ninety days.

Per sample: cluster CPU and RAM, network in and out, workload pods, volumes
that are not healthy, nodes ready - and each node's CPU, RAM and whether it
was Ready, from which each node's availability is worked out.

Deliberately small - a few hundred kilobytes at most. It answers "was it busy
last week?" and "has node2 been dropping out?"; anything deeper is what
Harvester's own monitoring (Prometheus and Grafana) is for.
"""
import os
import time

import homestead_shared as SHARED

DATA_DIR = "/data"
FILE = "history.json"
STEP = 300
FINE_KEEP = 2 * 86400
COARSE_KEEP = 90 * 86400
FIELDS = ("cpu", "mem", "rx", "tx", "pods", "vol_bad", "nodes_ready", "nodes_total")
_lock = SHARED.SharedLock("history")


def bind(data_dir):
    global DATA_DIR
    DATA_DIR = data_dir


def _path():
    return os.path.join(DATA_DIR, FILE)


def _read():
    import json
    try:
        with open(_path(), encoding="utf-8") as handle:
            data = json.load(handle)
        if isinstance(data, dict):
            return {"fine": data.get("fine") or [], "coarse": data.get("coarse") or [],
                    "boots": data.get("boots") or {}, "events": data.get("events") or []}
    except (OSError, ValueError):
        pass
    return {"fine": [], "coarse": [], "boots": {}, "events": []}


def sample_from(overview, now=None):
    """One sample from the dashboard overview."""
    now = int(now or time.time())
    nodes = overview.get("nodes") or []
    return {"t": now - now % STEP,
            "cpu": overview.get("cpu_pct", 0), "mem": overview.get("mem_pct", 0),
            "rx": round(sum(n.get("rx_mbps", 0) or 0 for n in nodes), 2),
            "tx": round(sum(n.get("tx_mbps", 0) or 0 for n in nodes), 2),
            "pods": overview.get("workload_pods", 0),
            "vol_bad": overview.get("vol_degraded", 0) + overview.get("vol_faulted", 0),
            "nodes_ready": overview.get("nodes_ready", 0), "nodes_total": overview.get("nodes_total", 0),
            "nodes": {n["name"]: [n.get("cpu_pct", 0), n.get("mem_pct", 0), 1 if n.get("status") == "Ready" else 0]
                      for n in nodes}}


def _rollup(samples, hour):
    """An hour of samples as one: averages, the peaks, and node availability."""
    out = {"t": hour, "n": len(samples)}
    for field in FIELDS:
        values = [s.get(field, 0) or 0 for s in samples]
        out[field] = round(sum(values) / len(values), 2)
        if field in ("cpu", "mem", "rx", "tx", "vol_bad"):
            out[f"{field}_max"] = max(values)
    names = {name for s in samples for name in (s.get("nodes") or {})}
    out["nodes"] = {}
    for name in names:
        rows = [s["nodes"][name] for s in samples if name in (s.get("nodes") or {})]
        out["nodes"][name] = [round(sum(r[0] for r in rows) / len(rows), 1), round(sum(r[1] for r in rows) / len(rows), 1),
                              round(sum(r[2] for r in rows) / len(rows), 3)]
    return out


def record(overview, now=None):
    """Adds a sample, rolls finished hours up, and forgets what is too old."""
    now = int(now or time.time())
    sample = sample_from(overview, now)
    with _lock:
        data = _read()
        fine = [s for s in data["fine"] if s["t"] != sample["t"]] + [sample]
        fine.sort(key=lambda s: s["t"])
        coarse = data["coarse"]
        done = {c["t"] for c in coarse}
        current_hour = now - now % 3600
        hours = {}
        for s in fine:
            hour = s["t"] - s["t"] % 3600
            if hour < current_hour and hour not in done:
                hours.setdefault(hour, []).append(s)
        coarse = sorted(coarse + [_rollup(rows, hour) for hour, rows in hours.items()], key=lambda c: c["t"])
        boots, events = dict(data.get("boots") or {}), list(data.get("events") or [])
        for node in overview.get("nodes") or []:
            boot = node.get("boot_id") or ""
            if not boot:
                continue
            if boots.get(node["name"]) and boots[node["name"]] != boot:
                events.append({"t": now, "node": node["name"], "kind": "reboot"})
            boots[node["name"]] = boot
        data = {"fine": [s for s in fine if s["t"] > now - FINE_KEEP],
                "coarse": [c for c in coarse if c["t"] > now - COARSE_KEEP],
                "boots": boots, "events": [e for e in events if e["t"] > now - COARSE_KEEP][-500:]}
        SHARED.write_json(_path(), data, separators=(",", ":"))
    return sample


RANGES = {"24h": (86400, "fine"), "48h": (2 * 86400, "fine"), "7d": (7 * 86400, "coarse"),
          "30d": (30 * 86400, "coarse"), "90d": (90 * 86400, "coarse")}


def series(range_name="24h", now=None):
    """What the dashboard draws for a range, with availability per node."""
    now = int(now or time.time())
    span, tier = RANGES.get(range_name, RANGES["24h"])
    data = _read()
    rows = [r for r in data[tier] if r["t"] > now - span]
    if tier == "coarse" and not rows:
        rows = [r for r in data["fine"] if r["t"] > now - span]
    out = {"range": range_name, "step": STEP if tier == "fine" else 3600, "t": [r["t"] for r in rows]}
    for field in FIELDS:
        out[field] = [r.get(field, 0) for r in rows]
    for field in ("cpu", "mem"):
        out[f"{field}_max"] = max([r.get(f"{field}_max", r.get(field, 0)) for r in rows] or [0])
    names = sorted({name for r in rows for name in (r.get("nodes") or {})})
    nodes = []
    for name in names:
        values = [r["nodes"][name] for r in rows if name in (r.get("nodes") or {})]
        weights = [r.get("n", 1) for r in rows if name in (r.get("nodes") or {})]
        total = sum(weights) or 1
        nodes.append({"name": name,
                      "cpu": round(sum(v[0] * w for v, w in zip(values, weights)) / total, 1),
                      "mem": round(sum(v[1] * w for v, w in zip(values, weights)) / total, 1),
                      "availability": round(100 * sum(v[2] * w for v, w in zip(values, weights)) / total, 2),
                      "cpu_series": [v[0] for v in values]})
    out["nodes"] = nodes
    out["since"] = rows[0]["t"] if rows else 0
    out["samples"] = len(rows)
    return out



# ---- each node's uptime --------------------------------------------------------
# Every sample says whether each node was Ready. Five-minute samples for two
# days and hourly ones for ninety give, per node: how much of each window it
# was up, a strip of its days, and the times it was down - to the five
# minutes while the fine samples last, to the hour after that.
WINDOWS = (("24h", 86400), ("7d", 7 * 86400), ("30d", 30 * 86400), ("90d", 90 * 86400))


def _ready_rows(data, name):
    """(start, seconds, share of it Ready) for the node, oldest first: hours
    before the fine samples begin, then the fine samples."""
    fine = [(r["t"], STEP, r["nodes"][name][2]) for r in data["fine"] if name in (r.get("nodes") or {})]
    first_fine = fine[0][0] if fine else float("inf")
    coarse = [(r["t"], 3600, r["nodes"][name][2]) for r in data["coarse"]
              if name in (r.get("nodes") or {}) and r["t"] + 3600 <= first_fine]
    return coarse + fine


def _share(rows, since):
    rows = [(t, span, up) for t, span, up in rows if t + span > since]
    total = sum(span for _, span, _ in rows)
    return round(100 * sum(span * up for _, span, up in rows) / total, 3) if total else None


def _outages(rows, now):
    """Runs of time down, oldest first. Fine samples mark it to five minutes;
    an hour only partly up counts its down share and is marked approximate."""
    out, current = [], None
    for t, span, up in rows:
        down = span * (1 - up)
        if down > 0:
            if current and current["end"] >= t - 1:
                current["end"] = t + span
                current["down_s"] += down
                current["exact"] = current["exact"] and span == STEP
            else:
                current = {"start": t, "end": t + span, "down_s": down, "exact": span == STEP}
                out.append(current)
        else:
            current = None
    for row in out:
        row["ongoing"] = row["end"] >= now - STEP
        row["down_s"] = int(row["down_s"])
    return out


def uptime(now=None):
    """Per node: time up in each window, its days, its outages and reboots."""
    now = int(now or time.time())
    data = _read()
    names = sorted({n for r in data["fine"] + data["coarse"] for n in (r.get("nodes") or {})})
    out = {}
    for name in names:
        rows = _ready_rows(data, name)
        days = []
        for back in range(89, -1, -1):
            start = now - now % 86400 - back * 86400
            share = _share([(t, span, up) for t, span, up in rows if t < start + 86400], start)
            days.append({"day": start, "up": share})
        out[name] = {"windows": {label: _share(rows, now - span) for label, span in WINDOWS},
                     "days": days, "outages": _outages(rows, now)[-50:],
                     "reboots": [e["t"] for e in data.get("events") or []
                                 if e.get("node") == name and e.get("kind") == "reboot"][-50:],
                     "since": rows[0][0] if rows else None}
    return {"nodes": out, "step": STEP}
