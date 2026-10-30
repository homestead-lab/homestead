"""Long-term stats: how the cluster has been, kept whether or not anyone looks.

The dashboard's charts and longer view are kept on the data volume,
recorded by the leading replica in the background:

* thirty-second live samples, kept for an hour;
* a sample every five minutes, kept for two days;
* hourly averages and peaks, kept for ninety days.

Per sample: cluster CPU and RAM, network in and out, workload pods, volumes
that are not healthy, nodes ready - and each node's CPU, RAM and whether it
was Ready, from which each node's availability is worked out.

Deliberately small - a few hundred kilobytes at most. It answers "was it busy
last week?" and "has node2 been dropping out?"; anything deeper is what
Harvester's own monitoring (Prometheus and Grafana) is for.
"""
import bisect
import os
import time

import homestead_shared as SHARED
import homestead_routes

DATA_DIR = "/data"
FILE = "history.json"
LIVE_FILE = "history-live.json"
STEP = 300
LIVE_STEP = 30
LIVE_KEEP = 3600
FINE_KEEP = 2 * 86400
COARSE_KEEP = 90 * 86400
FIELDS = ("cpu", "mem", "rx", "tx", "pods", "vol_bad", "nodes_ready", "nodes_total")
LIVE_FIELDS = {"cpu": "cpu", "mem": "mem", "wl_pods": "pods", "sys_pods": "sys_pods",
               "vol_bad": "vol_bad", "net_rx": "rx", "net_tx": "tx"}
_lock = SHARED.SharedLock("history")


def bind(data_dir):
    global DATA_DIR
    DATA_DIR = data_dir


def _path():
    return os.path.join(DATA_DIR, FILE)


def _valid(event):
    """A maintenance entry of the shape this module writes: anything else (a
    hand edit, a half-written file) is left out, never fatal."""
    number = lambda v: isinstance(v, (int, float)) and not isinstance(v, bool)
    return (isinstance(event, dict) and all(isinstance(event.get(k), str) and event.get(k) for k in ("job", "node", "reason"))
            and number(event.get("start")) and (event.get("end") is None or number(event.get("end"))))


def _read():
    import json
    try:
        with open(_path(), encoding="utf-8") as handle:
            data = json.load(handle)
        if isinstance(data, dict):
            return {"fine": data.get("fine") or [], "coarse": data.get("coarse") or [],
                    "boots": data.get("boots") or {}, "events": data.get("events") or [],
                    "maintenance": [e for e in data.get("maintenance") or [] if _valid(e)]}
    except (OSError, ValueError):
        pass
    return {"fine": [], "coarse": [], "boots": {}, "events": [], "maintenance": []}


def _read_live():
    import json
    try:
        with open(os.path.join(DATA_DIR, LIVE_FILE), encoding="utf-8") as handle:
            rows = json.load(handle)
        return rows if isinstance(rows, list) else []
    except (OSError, ValueError):
        return []


def record_live(overview, now=None):
    """Keep a small live buffer without rewriting ninety days every thirty seconds."""
    now = int(time.time() if now is None else now)
    sample = sample_from(overview, now)
    sample["t"] = now - now % LIVE_STEP
    sample["sys_pods"] = overview.get("system_pods", 0)
    # The live charts don't need each node's breakdown; the fine tier has it.
    sample.pop("nodes", None)
    with _lock:
        rows = [s for s in _read_live() if now - LIVE_KEEP < s["t"] <= now and s["t"] != sample["t"]]
        rows = sorted(rows + [sample], key=lambda s: s["t"])[-(LIVE_KEEP // LIVE_STEP):]
        SHARED.write_json(os.path.join(DATA_DIR, LIVE_FILE), rows, separators=(",", ":"))
    return sample


def live_series(now=None):
    now = int(time.time() if now is None else now)
    rows = [s for s in _read_live() if now - LIVE_KEEP < s["t"] <= now]
    step = LIVE_STEP
    # Existing installations can show their real five-minute observations
    # until their first live sample is recorded after upgrading.
    if not rows:
        rows = [s for s in _read()["fine"] if now - LIVE_KEEP < s["t"] <= now]
        step = STEP
    out = {"t": [s["t"] for s in rows], "step": step,
           "last_sample": rows[-1]["t"] if rows else 0}
    for public, field in LIVE_FIELDS.items():
        out[public] = [s.get(field, 0) for s in rows]
    return out


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
        rebooted = set()
        for node in overview.get("nodes") or []:
            boot = node.get("boot_id") or ""
            if not boot:
                continue
            if boots.get(node["name"]) and boots[node["name"]] != boot:
                events.append({"t": now, "node": node["name"], "kind": "reboot"})
                rebooted.add(node["name"])
            boots[node["name"]] = boot
        maintenance = _harvester(_cluster_returns(_expire(data["maintenance"], now), rebooted, now),
                                 overview.get("nodes") or [], now)
        data = {"fine": [s for s in fine if s["t"] > now - FINE_KEEP],
                "coarse": [c for c in coarse if c["t"] > now - COARSE_KEEP],
                "boots": boots, "events": [e for e in events if e["t"] > now - COARSE_KEEP][-500:],
                "maintenance": [e for e in maintenance if e["end"] is None or e["end"] > now - COARSE_KEEP]}
        SHARED.write_json(_path(), data, separators=(",", ":"))
    return sample


# Maintenance: downtime Homestead caused, or Harvester's maintenance mode
# (#535). Kept beside the samples, never written into them, so the share a
# host was Ready stays what it was; uptime() reports it with and without.
# Opened and closed by the job that caused it, from either Homestead copy.
MAX_OPEN = 6 * 3600
# An open event for these whose job is gone must not hide an outage for
# ever: after MAX_OPEN it counts as closed at its limit.
CEILING_REASONS = ("host reboot", "host shutdown", "OS update")


def _expire(events, now):
    events = [dict(e) for e in events]
    for event in events:
        if event["end"] is None and event["reason"] in CEILING_REASONS and now - event["start"] > MAX_OPEN:
            event.update(end=event["start"] + MAX_OPEN, outcome="limit")
    return events


# A planned cluster shutdown can last as long as the cluster is off. Once
# Homestead runs again, each host's entry closes when the host is back on a
# new boot; one still not back RETURN_GRACE after that is an outage.
RETURN_GRACE = 30 * 60
SHUTDOWN_SETTLE = 10 * 60   # the hosts power off within this of the commit


def _cluster_returns(events, rebooted, now):
    for event in events:
        if event["end"] is not None or event["reason"] != "cluster shutdown":
            continue
        if event["node"] in rebooted:
            event.update(end=now, outcome="returned")
        elif now >= event["start"] + SHUTDOWN_SETTLE:
            event.setdefault("resumed", now)
            if now - event["resumed"] > RETURN_GRACE:
                event.update(end=now, outcome="limit")
    return events


# A host gone from the samples (its node deleted) while Harvester had it in
# maintenance: its entry ends where it was last seen once it has been gone this long.
MISSING_CLOSE = 30 * 60


def _harvester(events, nodes, now):
    """Harvester's maintenance mode, as the samples see it: opened on the first
    that shows a host in it, closed on the first that doesn't."""
    inside = {n["name"] for n in nodes if n.get("maintenance")}
    present = {n["name"] for n in nodes}
    open_for = {e["node"]: e for e in events if e["end"] is None and e["reason"] == "Harvester maintenance"}
    for name in inside - set(open_for):
        job = f"harvester:{name}:{now}"
        events.append({"id": job, "node": name, "start": now, "end": None, "reason": "Harvester maintenance",
                       "job": job, "outcome": None, "seen": now})
    for name, event in open_for.items():
        if name in inside:
            event["seen"] = now
        elif name in present:
            event.update(end=now, outcome="ended")
        elif now - event.get("seen", event["start"]) > MISSING_CLOSE:
            event.update(end=max(event["start"], event.get("seen", event["start"])), outcome="ended")
    return events


def _save(data):
    SHARED.write_json(_path(), data, separators=(",", ":"))


def maintenance_open(node, reason, job, now=None):
    """Open the event for job, or return the one it opened already."""
    now = int(time.time() if now is None else now)
    with _lock:
        data = _read()
        for event in data["maintenance"]:
            if event["job"] == job:
                return event
        event = {"id": job, "node": node, "start": now, "end": None, "reason": reason, "job": job, "outcome": None}
        data["maintenance"].append(event)
        _save(data)
        return event


def maintenance_close(job, now=None, outcome="ended"):
    """Close job's event: returned, limit or ended. Closed or unknown: as it is."""
    now = int(time.time() if now is None else now)
    with _lock:
        data = _read()
        for event in data["maintenance"]:
            if event["job"] == job:
                if event["end"] is None:
                    event.update(end=max(event["start"], now), outcome=outcome)
                    _save(data)
                return event
    return None


def maintenance_open_for(node, now=None):
    now = int(time.time() if now is None else now)
    for event in reversed(_expire(_read()["maintenance"], now)):
        if event["node"] == node and event["end"] is None:
            return event
    return None


# How long after maintenance overran its job's limit the host's alert skips
# the usual hold: it has waited for the maintenance already.
OVERRAN_RECENT = 15 * 60


def maintenance_view(now=None):
    """The hosts in maintenance now, and those whose maintenance just ended at
    its job's limit, from one read of the history."""
    now = int(time.time() if now is None else now)
    events = _expire(_read()["maintenance"], now)
    open_nodes = {e["node"] for e in events if e["end"] is None}
    latest = {}
    for e in events:
        if e["end"] is not None and e["end"] >= latest.get(e["node"], {"end": -1})["end"]:
            latest[e["node"]] = e
    overran = {n for n, e in latest.items() if e["outcome"] == "limit" and now - e["end"] <= OVERRAN_RECENT}
    return open_nodes, overran


def maintenance_since(since):
    """Host maintenance that started or ended after since (#748's timeline)."""
    return sorted((e for e in _read()["maintenance"] if e["start"] > since or (e["end"] or 0) > since),
                  key=lambda e: e["start"])


def maintenance_events(node):
    return sorted((e for e in _read()["maintenance"] if e["node"] == node), key=lambda e: e["start"])


RANGES = {"24h": (86400, "fine"), "48h": (2 * 86400, "fine"), "7d": (7 * 86400, "coarse"),
          "30d": (30 * 86400, "coarse"), "90d": (90 * 86400, "coarse")}


def series(range_name="24h", now=None):
    """What the dashboard draws for a range, with availability per node."""
    now = int(now or time.time())
    span, tier = RANGES.get(range_name, RANGES["24h"])
    data = _read()
    rows = [r for r in data[tier] if now - span < r["t"] <= now]
    if tier == "coarse":
        rolled = {r["t"] for r in rows}
        recent = {}
        for sample in data["fine"]:
            hour = sample["t"] - sample["t"] % 3600
            if now - span < sample["t"] <= now and hour not in rolled:
                recent.setdefault(hour, []).append(sample)
        rows = sorted(rows + [_rollup(samples, hour) for hour, samples in recent.items()], key=lambda r: r["t"])
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


def _ready_rows(data):
    """Per node, (start, seconds, share of it Ready), oldest first: hours
    before the fine samples begin, then the fine samples. One walk over the
    samples for every node, not one each (#979)."""
    fine, coarse = {}, {}
    for r in data["fine"]:
        for name, row in (r.get("nodes") or {}).items():
            fine.setdefault(name, []).append((r["t"], STEP, row[2]))
    for r in data["coarse"]:
        for name, row in (r.get("nodes") or {}).items():
            coarse.setdefault(name, []).append((r["t"], row))
    out = {}
    for name in set(fine) | set(coarse):
        first_fine = fine[name][0][0] if name in fine else float("inf")
        out[name] = [(t, 3600, row[2]) for t, row in coarse.get(name, ()) if t + 3600 <= first_fine] + fine.get(name, [])
    return out


def _share(rows, since):
    # Lists, not generators, into sum(): the same values in the same order, so
    # the same total, for less work (#979).
    rows = [row for row in rows if row[0] + row[1] > since]
    total = sum([row[1] for row in rows])
    return round(100 * sum([row[1] * row[2] for row in rows]) / total, 3) if total else None


DAY = 86400


def _in_order(rows):
    """The rows' starts, ends, lengths and seconds up, when the starts and ends
    both ascend, as recorded rows do; otherwise None."""
    starts, ends = [row[0] for row in rows], [row[0] + row[1] for row in rows]
    if starts != sorted(starts) or ends != sorted(ends):
        return None
    return starts, ends, [row[1] for row in rows], [row[1] * row[2] for row in rows]


def _tail(order, lo, hi):
    """_share of rows[lo:hi] from their lengths and seconds up: the same values
    in the same order into sum(), so the same figure."""
    total = sum(order[2][lo:hi])
    return round(100 * sum(order[3][lo:hi]) / total, 3) if total else None


def _windows(rows, now, order):
    """_share of each window. Rows in order make each window a tail of them,
    found by bisection (#979)."""
    if order is None:
        return {label: _share(rows, now - span) for label, span in WINDOWS}
    return {label: _tail(order, bisect.bisect_right(order[1], now - span), len(rows)) for label, span in WINDOWS}


def _days(rows, first, order, count=90):
    """_share for each of count days from first (#979). A row counts in every
    day it overlaps, in its order, so each day says exactly what filtering
    every row for it said. Rows in order make a day a slice of them, found by
    bisection; others are sorted into their days one by one."""
    if order is not None:
        starts, ends = order[0], order[1]
        return [_tail(order, bisect.bisect_right(ends, first + k * DAY), bisect.bisect_left(starts, first + (k + 1) * DAY))
                for k in range(count)]
    days = [[] for _ in range(count)]
    for row in rows:
        t, span = row[0], row[1]
        k = max(0, int((t - first) // DAY) - 1)
        while k < count and first + k * DAY < t + span:
            if t < first + (k + 1) * DAY:
                days[k].append(row)
            k += 1
    return [_share(days[k], first + k * DAY) for k in range(count)]


def _spans(events, now):
    """The node's maintenance as merged (start, end, reason) spans."""
    spans = []
    for e in sorted(events, key=lambda e: e["start"]):
        end = now if e["end"] is None else e["end"]
        if spans and e["start"] <= spans[-1][1]:
            reason = spans[-1][2] if e["reason"] in spans[-1][2].split(", ") else f"{spans[-1][2]}, {e['reason']}"
            spans[-1] = (spans[-1][0], max(spans[-1][1], end), reason)
        elif end > e["start"]:
            spans.append((e["start"], end, e["reason"]))
    return spans


def _pieces(rows, spans):
    """Each row cut at maintenance edges: (start, seconds, seconds down, reason,
    row span). A sample can't say when in its five minutes the host went down,
    and an hour's row only how long, so a row's downtime goes to the
    maintenance it overlaps first, with one sample's slack at each edge."""
    out = []
    ends = [b for _, b, _ in spans]     # merged spans: their ends ascend
    for t, span, up in rows:
        i = bisect.bisect_right(ends, t)
        if span and not (i < len(spans) and spans[i][0] < t + span):
            down = span * (1 - up)     # outside maintenance: the row as it is (#979)
            out.append((t, span, down * span / span, None, span))
            continue
        cuts = {t, t + span}
        for a, b, _ in spans:
            if a < t + span and b > t:
                cuts.update((max(a, t), min(b, t + span)))
        edges = sorted(cuts)
        cut = [(a, b - a, next((r for s, e, r in spans if s <= a and b <= e), None)) for a, b in zip(edges, edges[1:])]
        down = span * (1 - up)
        overlap = sum(length for _, length, reason in cut if reason)
        inside = min(down, overlap + 2 * STEP) if overlap else 0
        rest, outside_len = down - inside, span - overlap
        for a, length, reason in cut:
            share = (inside * length / overlap) if reason else (rest * length / outside_len if outside_len else 0)
            out.append((a, length, share, reason, span))
    return out


def _covered(spans, since, now):
    return int(sum(max(0, min(b, now) - max(a, since)) for a, b, _ in spans))


def _outages(pieces, now):
    """Runs of time down, oldest first. Fine samples mark it to five minutes;
    an hour only partly up counts its down share and is marked approximate.
    A run inside maintenance says so, and ends where the maintenance ends."""
    out, current = [], None
    for t, span, down, reason, row_span in pieces:
        if down > 0:
            if current and current["end"] >= t - 1 and current.get("maintenance") == reason:
                current["end"] = t + span
                current["down_s"] += down
                current["exact"] = current["exact"] and row_span == STEP
            else:
                current = {"start": t, "end": t + span, "down_s": down, "exact": row_span == STEP}
                if reason:
                    current["maintenance"] = reason
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
    out = {}
    maintenance = _expire(data["maintenance"], now)
    ready = _ready_rows(data)
    first = now - now % DAY - 89 * DAY
    for name in sorted(ready):
        rows = ready[name]
        events = [e for e in maintenance if e["node"] == name]
        spans = _spans(events, now)
        pieces = _pieces(rows, spans)
        outside = [(t, length, 1 - down / length) for t, length, down, reason, _ in pieces if reason is None and length]
        order, order_outside = _in_order(rows), _in_order(outside)
        up, excluding = _days(rows, first, order), _days(outside, first, order_outside)
        days = [{"day": first + k * DAY, "up": up[k], "up_excluding_maintenance": excluding[k],
                 "maintenance": any(a < first + (k + 1) * DAY and b > first + k * DAY for a, b, _ in spans)}
                for k in range(90)]
        planned = lambda t: any(a <= t <= b + STEP for a, b, _ in spans)
        out[name] = {"windows": _windows(rows, now, order),
                     "windows_excluding_maintenance": _windows(outside, now, order_outside),
                     "maintenance_s": {label: _covered(spans, now - span, now) for label, span in WINDOWS},
                     "days": days, "outages": _outages(pieces, now)[-50:],
                     "reboots": [{"t": e["t"], "planned": planned(e["t"])} for e in data.get("events") or []
                                 if e.get("node") == name and e.get("kind") == "reboot"][-50:],
                     "maintenance": [{"start": e["start"], "end": e["end"], "reason": e["reason"], "job": e["job"],
                                      "ongoing": e["end"] is None} for e in sorted(events, key=lambda e: e["start"])][-50:],
                     "since": rows[0][0] if rows else None}
    return {"nodes": out, "step": STEP}


# Its routes and who may use them (homestead_routes.py).
ROUTES = {
    ("GET", "/api/history/long"): ("viewer", lambda request: series((request.query.get("range") or ["24h"])[0])),
    ("GET", "/api/nodes/uptime"): ("viewer", lambda request: homestead_routes.cached("uptime", 60, uptime)),
    # Never hold the global cache mutex while writing to a client.
    ("GET", "/api/history"): ("viewer", lambda request: live_series()),
}
