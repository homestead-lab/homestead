"""What each host's node probe last said, read in the background (#705).

The probe DaemonSet (homestead_probe) answers on each host: temperatures,
devices and drives on 9099, SMART on 9100. Reading every host in turn inside
a request made the Hosts page and the Dashboard wait for the slowest probe,
and the wait grew with every host added. Here a loop reads every host at
once, each with a short timeout, and keeps per host the last reading, when it
was taken and how many reads in a row have failed. Pages answer from that
straight away: a host not heard from yet is "waiting", one that missed a read
keeps its last values as "stale", and after three misses in a row it is
"unreachable".

Every replica runs its own loop: the reads are a few small HTTP requests to
pods on the cluster network, and keeping the state in memory means no
cross-replica storage and no leader to wait for. Before the loop runs (and in
tests) a read happens in the caller, as before, at most every INTERVAL.
"""
import json
import threading
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor

import homestead_names as NAMES
import homestead_smart as SMART

INTERVAL = 15            # seconds between rounds
TIMEOUT = 2              # temperatures, devices and drives (9099)
SMART_TIMEOUT = 20       # the SMART helper reads every drive, so it may take longer
SMART_EVERY = 300        # SMART changes slowly; read it every five minutes
SMART_RETRY = 60         # after a failed SMART read, try again sooner
UNREACHABLE_AFTER = 3    # misses in a row before a host's probe is called unreachable
GONE_AFTER = 4           # listings in a row without the host's probe pod before its state is dropped
FIRST_WAIT = 5           # how long a caller that wants data waits for the first round
MAX_WORKERS = 8          # one round at a time, so this is the cap for the whole replica
LIMIT = 4 * 1024**2
SMART_UNAVAILABLE = "SMART helper unavailable; install or update deploy/nodeprobe.yaml"

_lock = threading.Lock()         # the state
_single = threading.Lock()       # one round at a time
_state = {}                      # node -> {"payload", "at", "misses", "last_ok", "smart", "smart_at", "smart_stale", "absent"}
_round = {"at": 0.0, "read": None, "done": threading.Event(), "thread": None, "stop": threading.Event()}
_namespace = ["lab"]


def _read(ip, port, timeout, headers=None):
    url = f"http://{ip}:{port}/"
    with urllib.request.urlopen(urllib.request.Request(url, headers=headers) if headers else url,
                                timeout=timeout) as response:
        raw = response.read(LIMIT + 1)
    if len(raw) > LIMIT:
        raise ValueError("node telemetry exceeds the size limit")
    return json.loads(raw.decode())


def _probe(pod):
    """One host's temperatures, devices and drives, or None when the probe didn't answer."""
    try:
        payload = {"node": pod["spec"]["nodeName"], "thermal": [], "hwmon": [], "devices": {},
                   "disks": [], "sensors": 0}
        payload.update(_read(pod["status"]["podIP"], 9099, TIMEOUT))
    except Exception:
        return None
    # Provenance is assigned by this backend, never accepted from the probe
    # response, and stamped when the answer arrived. Consumers still verify
    # Pod/DaemonSet ownership, current host boot and sample age before
    # trusting NUMA data.
    payload["numa_source"] = {"pod": {key: pod.get("metadata", {}).get(key) for key in ("namespace", "name", "uid")},
                              "received_at": time.time()}
    return payload


def _smart(pod):
    """One host's SMART rows by drive name ({} when it has none), or None when the helper didn't answer."""
    try:
        # The helper answers only signed reads (homestead_smart.signed); without
        # the key the read goes unsigned and the helper refuses it.
        try:
            headers = SMART.signed(b"GET /")
        except Exception:
            headers = None
        answer = _read(pod["status"]["podIP"], 9100, SMART_TIMEOUT, headers)
        return {row.get("name"): row for row in answer.get("disks", [])}
    except Exception:
        return None


def _listing():
    """The probe DaemonSet's pods. A listing that fails raises: an API blip is
    not every probe gone."""
    answer = NAMES.kget(f"/api/v1/namespaces/{_namespace[0]}/pods?labelSelector=app%3D{NAMES.NODEPROBE}")
    if not isinstance(answer, dict) or not isinstance(answer.get("items"), list):
        raise ValueError("the probe pods could not be listed")
    return answer["items"]


def _ready(pod):
    status, spec = pod.get("status") or {}, pod.get("spec") or {}
    return bool(status.get("podIP") and spec.get("nodeName") and status.get("phase") == "Running")


def _pool(work):
    """Run {node: callable} at the same time; {node: result}."""
    if not work:
        return {}
    with ThreadPoolExecutor(max_workers=min(MAX_WORKERS, len(work))) as pool:
        futures = {node: pool.submit(call) for node, call in work.items()}
        return {node: future.result() for node, future in futures.items()}


def run_round(now=None, smart=False):
    """Read every host's probe once, all at the same time, and record what came back.

    One round at a time: a caller that finds one running waits for it rather
    than starting another, so misses are not counted twice and the thread cap
    holds for the replica. A listing that fails changes nothing."""
    if not _single.acquire(blocking=False):
        with _single:
            return
    try:
        _run(time.time() if now is None else now, smart)
    finally:
        _single.release()


def _run(now, smart):
    pods = _listing()
    listed = {(p.get("spec") or {}).get("nodeName") for p in pods} - {None}
    ready = {p["spec"]["nodeName"]: p for p in pods if _ready(p)}
    readings = _pool({node: (lambda p=pod: _probe(p)) for node, pod in ready.items()})
    with _lock:
        for node in list(_state):
            if node in listed:
                _state[node]["absent"] = 0
            else:
                # Its probe pod is gone from the listing. Keep the reading a
                # while: a DaemonSet rollout replaces pods one by one.
                _state[node]["absent"] += 1
                if _state[node]["absent"] >= GONE_AFTER:
                    del _state[node]
        for node, payload in readings.items():
            entry = _state.setdefault(node, {"payload": None, "at": 0, "misses": 0, "last_ok": None,
                                             "smart": None, "smart_at": 0, "smart_stale": False, "absent": 0})
            if payload is None:
                entry["misses"] += 1
            else:
                entry.update(payload=payload, at=now, misses=0, last_ok=now)
        due = {node: pod for node, pod in ready.items()
               if node in _state and (smart or now - _state[node]["smart_at"] >= SMART_EVERY)}
        _round["at"] = now
        _round["read"] = time.monotonic()
    # The temperatures are recorded before SMART is read, so nobody waits on it.
    _round["done"].set()
    rows = _pool({node: (lambda p=pod: _smart(p)) for node, pod in due.items()})
    with _lock:
        for node, found in rows.items():
            entry = _state.get(node)
            if entry is None:
                continue
            if found is None:
                # Keep the last rows, marked stale, and try again sooner.
                entry["smart_stale"] = entry["smart"] is not None
                entry["smart_at"] = now - SMART_EVERY + SMART_RETRY
            else:
                entry.update(smart=found, smart_at=now, smart_stale=False)
                # The SMART page answers from this reading until the next one has had time to land.
                SMART.remember(node, found, keep=SMART_EVERY + SMART_TIMEOUT + INTERVAL)


def _with_smart(entry):
    payload = dict(entry["payload"])
    rows = entry["smart"]
    if isinstance(rows, dict):
        payload["disks"] = [dict(disk, smart=rows.get(disk.get("name"))) for disk in payload.get("disks", [])]
        payload["smart_helper"] = {"available": True, "disks": len(rows)}
        if entry["smart_stale"]:
            payload["smart_helper"]["stale"] = True
    else:
        payload["smart_helper"] = {"available": False, "reason": SMART_UNAVAILABLE}
    return payload


def running():
    thread = _round["thread"]
    return bool(thread and thread.is_alive())


def node_temps(namespace=None, wait=True, unreachable=False):
    """Each host's last good reading, by node name, for anything that acts on it.

    Left out: hosts whose probe has never answered (as hosts without a probe
    always were) and, unless asked for, hosts whose probe is unreachable -
    their last reading is too old to act on. A stale reading (one or two
    missed reads, under a minute old) is kept.

    With the loop running this never reads a probe; a caller that wants data
    waits at most FIRST_WAIT for the loop's first round (wait=False: not at
    all). Without the loop (before it starts, in tests) the caller reads, as
    before, at most every INTERVAL."""
    if namespace:
        _namespace[0] = namespace
    if running():
        if wait and not _round["done"].is_set():
            _round["done"].wait(FIRST_WAIT)
    elif _round["read"] is None or time.monotonic() - _round["read"] >= INTERVAL:
        try:
            run_round()
        except Exception:
            pass                     # the listing failed: answer from what is known
    with _lock:
        return {node: _with_smart(entry) for node, entry in _state.items()
                if entry["payload"] is not None and (unreachable or entry["misses"] < UNREACHABLE_AFTER)}


def status(now=None):
    """Each probed host's state for the pages: ok, waiting, stale or unreachable."""
    now = time.time() if now is None else now
    with _lock:
        out = {}
        for node, entry in _state.items():
            if entry["misses"] >= UNREACHABLE_AFTER:
                state = "unreachable"
            elif entry["payload"] is None:
                state = "waiting"
            elif entry["misses"]:
                state = "stale"
            else:
                state = "ok"
            out[node] = {"state": state, "misses": entry["misses"], "last_ok": entry["last_ok"],
                         "age_s": None if entry["last_ok"] is None else max(0, round(now - entry["last_ok"]))}
        return out


def read_smart_again():
    """A SMART test started: read SMART on the next round."""
    with _lock:
        for entry in _state.values():
            entry["smart_at"] = 0
        _round["read"] = None


def _loop():
    while not _round["stop"].is_set():
        try:
            run_round()
        except Exception as error:
            _round["done"].set()
            print(f"node probes: {str(error)[:160]}", flush=True)
        _round["stop"].wait(INTERVAL)


def start(namespace):
    """Start this replica's loop once."""
    _namespace[0] = namespace
    if running():
        return
    _round["stop"].clear()
    _round["thread"] = threading.Thread(target=_loop, name="node-probes", daemon=True)
    _round["thread"].start()


def stop():
    _round["stop"].set()
    thread = _round["thread"]
    if thread:
        thread.join(5)
    _round["thread"] = None


def reset():
    """Forget everything (tests)."""
    with _lock:
        _state.clear()
        _round["at"] = 0
        _round["read"] = None
        _round["done"].clear()
