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
            return {"fine": data.get("fine") or [], "coarse": data.get("coarse") or []}
    except (OSError, ValueError):
        pass
    return {"fine": [], "coarse": []}


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
        data = {"fine": [s for s in fine if s["t"] > now - FINE_KEEP],
                "coarse": [c for c in coarse if c["t"] > now - COARSE_KEEP]}
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
