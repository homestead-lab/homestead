"""What each app's containers use, kept long enough to say what to reserve.

The scheduler places pods by what they reserve (their requests), never by what
they use, so an app without requests is placed as if it used nothing - and a
host can fill up while it looks empty to Kubernetes (#347). One reading from
metrics-server is a poor guide: an app that is idle at noon may compile at
night. This keeps, for every container of every app, its CPU and memory every
STEP seconds, and offers a reservation from the last WINDOW_DAYS.

Each day is a histogram per container rather than a list of samples: buckets
growing by RATIO, so a reading is kept to within 10% in a handful of numbers,
and a week's percentile is that of the week's readings, not an average of
daily ones. The day's largest reading is kept exactly. Days older than
KEEP_DAYS are forgotten, and so is a container no longer seen for that long.

A reading is per pod: a request is per pod too, so an app with three copies
gives three readings each time, and none of them is added up. What makes "a
day of readings" is the number of rounds a container was seen in, not the
readings, or three copies would count a day in eight hours.

The file is rewritten whole each round - about 400 bytes per container per
day, so half a megabyte for a hundred containers and a few megabytes for five
hundred - and read once per change, not once per container asked about.

Nothing here changes an app. The suggestion is shown in Edit, beside the
fields it would fill, and saved only if the person saves it.
"""
import json
import math
import os
import time

import homestead_kube_metrics as KUBE_METRICS
import homestead_names as NAMES
import homestead_shared as SHARED

DATA_DIR = "/data"
# Kubernetes quantities in cores and bytes; the server's own parsers, bound in.
_cpu = _mem = None
FILE = "usage.json"
STEP = 300
KEEP_DAYS = 14
WINDOW_DAYS = 7
MIN_ROUNDS = 288           # a day of rounds before anything is suggested
RATIO = 1.1
PERCENTILE = 0.95
# Headroom over the 95th percentile, and the least worth reserving, as the
# issue proposed: CPU is shared and bursts are throttled, so it gets more
# headroom; memory over the reservation is what kubelet evicts first.
CPU_FACTOR, CPU_FLOOR_M, CPU_STEP_M = 1.5, 10, 5
MEM_FACTOR, MEM_FLOOR_MI, MEM_STEP_MI = 1.25, 32, 16
# A critical app gets more: kubelet, short of memory, evicts pods using more
# than they reserve before any within it, whatever their priority, and only
# then weighs priority - so a critical app must stay within its reservation.
MEM_FACTOR_CRITICAL = 1.5
# A reservation within this share of the suggestion already matches it: the
# suggestion moves a little every day, and a restart for 5% is not worth it.
CLOSE = 0.10
# Exclusive across replicas: during a leader handoff two may record a round.
_lock = SHARED.SharedLock("usage")
_cache = {"stamp": None, "data": None}


# server.py's, for the loop: the Kubernetes read, what a pod runs for, the
# platform's namespaces, whether this replica leads, and the heartbeat
# Settings shows.
_read = _owner = None
_skip_ns = ()
_is_leader, _beat = lambda: False, lambda *args, **kwargs: None


def bind(data_dir, cpu_cores, mem_bytes, read=None, owner=None, skip_ns=(), is_leader=None, beat=None):
    global DATA_DIR, _cpu, _mem, _read, _owner, _skip_ns, _is_leader, _beat
    DATA_DIR, _cpu, _mem = data_dir, cpu_cores, mem_bytes
    _read, _owner, _skip_ns = read, owner, skip_ns
    _is_leader, _beat = is_leader or _is_leader, beat or _beat


def _helper(pod):
    """Homestead's own short-lived pods - file browsers, copy jobs - which are
    not apps and have nothing to reserve."""
    return NAMES.label_of(pod.get("metadata") or {}, "task")


def _path():
    return os.path.join(DATA_DIR, FILE)


def _load():
    try:
        with open(_path(), encoding="utf-8") as handle:
            data = json.load(handle)
        return data if isinstance(data, dict) and isinstance(data.get("series"), dict) else {"series": {}}
    except (OSError, ValueError):
        return {"series": {}}


def _day(now):
    return time.strftime("%Y-%m-%d", time.gmtime(now))


def _bucket(value):
    """A reading's bucket: -1 for under one unit, else the power of RATIO it
    falls under, so the bucket's top is never below the reading."""
    return -1 if value < 1 else int(math.floor(math.log(value) / math.log(RATIO)))


def _mid(bucket):
    """A bucket's middle, on the same scale as its edges: a reading in it is
    within 5% either way, where its top would overstate every one by up to 10%."""
    return 0.5 if bucket < 0 else RATIO ** (bucket + 0.5)


def key(ns, workload, container):
    return f"{ns}/{workload}/{container}"


def record(readings, now=None):
    """Adds one round of readings: [(ns, workload, container, millicores, mib)]."""
    now = int(now or time.time())
    today, oldest = _day(now), _day(now - KEEP_DAYS * 86400)
    with _lock:
        data = _load()
        series = data["series"]
        counted = set()
        for ns, workload, container, cpu_m, mem_mi in readings:
            name = key(ns, workload, container)
            entry = series.setdefault(name, {"days": {}})
            day = entry["days"].setdefault(today, {"n": 0, "rounds": 0, "cpu": {}, "mem": {}, "cpu_max": 0, "mem_max": 0})
            day["n"] += 1
            if name not in counted:
                counted.add(name)
                day["rounds"] = day.get("rounds", 0) + 1
            for field, value in (("cpu", cpu_m), ("mem", mem_mi)):
                bucket = str(_bucket(value))
                day[field][bucket] = day[field].get(bucket, 0) + 1
                day[f"{field}_max"] = max(day[f"{field}_max"], round(value, 1))
            entry["seen"] = today
        for name in list(series):
            series[name]["days"] = {d: v for d, v in series[name]["days"].items() if d >= oldest}
            if not series[name]["days"] or series[name].get("seen", "") < oldest:
                series.pop(name)
        SHARED.write_json(_path(), {"format": 1, "series": series}, separators=(",", ":"), sort_keys=True)


def _percentile(histograms, share):
    counts = {}
    for histogram in histograms:
        for bucket, count in histogram.items():
            counts[int(bucket)] = counts.get(int(bucket), 0) + count
    total = sum(counts.values())
    if not total:
        return 0.0
    seen = 0
    for bucket in sorted(counts):
        seen += counts[bucket]
        if seen >= share * total:
            return _mid(bucket)
    return _mid(max(counts))


def _round_up(value, step):
    return int(math.ceil(value / step) * step)


def _millicores(text):
    return _cpu(text) * 1000 if str(text or "").strip() else None


def _mebibytes(text):
    return _mem(text) / 2 ** 20 if str(text or "").strip() else None


def _snapshot():
    """The file as last written, read again only when it changed."""
    try:
        stamp = (_path(), os.stat(_path()).st_mtime_ns)
    except OSError:
        return {"series": {}}
    if _cache["stamp"] != stamp:
        with _lock:
            _cache.update(stamp=stamp, data=_load())
    return _cache["data"]


def summary(ns, workload, container, requests=None, limits=None, now=None, tier=""):
    """What a container has used over the last WINDOW_DAYS, and what to
    reserve for it - or why there is no suggestion yet."""
    now = int(now or time.time())
    first = _day(now - (WINDOW_DAYS - 1) * 86400)
    entry = _snapshot()["series"].get(key(ns, workload, container)) or {}
    days = {d: v for d, v in (entry.get("days") or {}).items() if d >= first}
    samples = sum(v.get("n", 0) for v in days.values())
    rounds = sum(v.get("rounds", 0) for v in days.values())
    # How long it was watched, from its rounds: three hours that cross
    # midnight touch two days but are not two days of use.
    out = {"days": len(days), "hours": rounds * STEP // 3600, "samples": samples, "rounds": rounds,
           "window_days": WINDOW_DAYS, "since": min(days) if days else ""}
    if not samples:
        out["why"] = "No readings yet: Homestead records use every five minutes."
        return out
    peak_cpu = max(v.get("cpu_max", 0) for v in days.values())
    peak_mem = max(v.get("mem_max", 0) for v in days.values())
    # A bucket's middle can sit above the largest reading in it.
    cpu = min(_percentile([v.get("cpu") or {} for v in days.values()], PERCENTILE), peak_cpu)
    mem = min(_percentile([v.get("mem") or {} for v in days.values()], PERCENTILE), peak_mem)
    out.update({"cpu_p95_m": round(cpu), "cpu_max_m": round(peak_cpu), "mem_p95_mi": round(mem), "mem_max_mi": round(peak_mem)})
    if rounds < MIN_ROUNDS:
        hours = rounds * STEP // 3600
        out["why"] = f"A suggestion needs a day of readings; {hours} hour{'' if hours == 1 else 's'} so far."
        return out
    cpu_m = max(CPU_FLOOR_M, _round_up(cpu * CPU_FACTOR, CPU_STEP_M))
    mem_mi = max(MEM_FLOOR_MI, _round_up(mem * (MEM_FACTOR_CRITICAL if tier == "critical" else MEM_FACTOR), MEM_STEP_MI))
    # Kubernetes refuses a request above the limit, so a suggestion never
    # passes one; it says so, since the limit itself may be what is wrong.
    capped = []
    cpu_limit, mem_limit = _millicores((limits or {}).get("cpu")), _mebibytes((limits or {}).get("memory"))
    if cpu_limit and cpu_m > cpu_limit:
        cpu_m = int(cpu_limit)
        capped.append("cpu")
    if mem_limit and mem_mi > mem_limit:
        mem_mi = int(mem_limit)
        capped.append("memory")
    # Kubernetes takes a limit as the request where none is set, so such a
    # container reserves its limit - it does not reserve nothing.
    current_cpu = _millicores((requests or {}).get("cpu") or (limits or {}).get("cpu"))
    current_mem = _mebibytes((requests or {}).get("memory") or (limits or {}).get("memory"))

    def close(now_, then):
        return bool(now_) and abs(now_ - then) <= CLOSE * then

    def change(now_, then):
        """Each resource on its own: one may go up while the other comes
        down, and one within CLOSE of the suggestion stays as it is."""
        return "keep" if close(now_, then) else "lower" if now_ and then < now_ else "raise"

    cpu_change, mem_change = change(current_cpu, cpu_m), change(current_mem, mem_mi)
    out["suggest"] = {"cpu": f"{cpu_m}m", "memory": f"{mem_mi}Mi", "capped": capped,
                      "cpu_now_m": round(current_cpu) if current_cpu else 0,
                      "memory_now_mi": round(current_mem) if current_mem else 0,
                      "cpu_change": cpu_change, "memory_change": mem_change,
                      "lowers": "lower" in (cpu_change, mem_change),
                      "differs": (cpu_change, mem_change) != ("keep", "keep")}
    return out


def readings(pods, metrics, owner, skip_ns=()):
    """This round's readings from pods and their metrics: running pods outside
    skip_ns that owner() names a workload for; anything else is not an app."""
    by_pod = {(m["metadata"]["namespace"], m["metadata"]["name"]): m for m in metrics or []}
    out = []
    for pod in pods or []:
        meta = pod.get("metadata") or {}
        ns = meta.get("namespace", "")
        if ns in skip_ns or (pod.get("status") or {}).get("phase") != "Running":
            continue
        workload = owner(pod)
        metric = by_pod.get((ns, meta.get("name", "")))
        if not workload or not metric:
            continue
        for row in metric.get("containers") or []:
            usage = row.get("usage") or {}
            cpu, mem = _millicores(usage.get("cpu")), _mebibytes(usage.get("memory"))
            if row.get("name") and cpu is not None and mem is not None:
                out.append((ns, workload, row["name"], cpu, mem))
    return out


class MetricsMissing(RuntimeError):
    """No current figures from metrics-server; code 404 when it is not installed."""

    def __init__(self, message, code=None):
        super().__init__(message)
        self.code = code


# The metrics-server reading last recorded, by when it answered: a round
# never records the same reading twice.
_taken = {"last_ok": None}


def sample(read, owner, skip_ns=(), skip=lambda pod: False):
    """One round: every running app container's use, from metrics-server,
    recorded. skip(pod) leaves out pods that are not apps - Homestead's
    helpers, say. The figures are the background reading
    (homestead_kube_metrics), never metrics-server itself (#928): only a
    current reading is recorded, and each one once. True when recorded."""
    metrics, state = KUBE_METRICS.read("pods", read)
    if state["state"] == "waiting" or (state["state"] == "ok" and state["last_ok"] == _taken["last_ok"]):
        return False
    if state["state"] == "absent":
        raise MetricsMissing("metrics-server is not installed", code=404)
    if state["state"] != "ok":
        raise MetricsMissing(f"metrics-server is not answering ({state['detail'] or state['state']}), "
                             "so this round records nothing")
    pods = [pod for pod in read("/api/v1/pods").get("items", []) if not skip(pod)]
    record(readings(pods, metrics, owner, skip_ns))
    _taken["last_ok"] = state["last_ok"]
    return True


def run(sleep=time.sleep):
    """Every STEP seconds on the leader: one round, so Edit can suggest what
    to reserve from a week rather than one moment."""
    last = 0
    while True:
        if _is_leader() and time.time() - last >= STEP:
            # A failed round waits STEP too: a cluster without metrics-server
            # would otherwise be asked, and logged, every 30 seconds.
            last = time.time()
            try:
                if sample(_read, _owner, _skip_ns, _helper) is False:
                    # No new reading yet (the first after a start, say): try
                    # again next turn rather than a whole STEP later.
                    last = 0
                _beat("usage", STEP, leader_only=True)
            except Exception as error:
                if getattr(error, "code", None) == 404:
                    error = RuntimeError("metrics-server is not installed, so there is no use to record; "
                                         "k3s and RKE2 include it")
                _beat("usage", STEP, error, leader_only=True)
                print(f"usage: {str(error)[:160]}", flush=True)
        sleep(30)
