"""Each host's kubelet summary (/stats/summary), read in the background (#976).

The Hosts page takes each host's network counters and filesystem figures
from it, and the volume list each mounted claim's bytes in use. Reading one
host after another inside the page's build made it wait for every kubelet in
turn, up to 8 s for one that did not answer, and the volume list read the
same summaries again.

Here a loop reads the hosts someone asked about in the last WANTED_FOR
seconds, all at once with a short timeout, and keeps each host's last
summary with when it came. Pages answer from that: a host whose kubelet
missed a read keeps its last summary, marked stale, until it is DROP_AFTER
old. Before the loop runs (and in tests) a caller reads in place, all hosts
at the same time, as before but in parallel.
"""
import threading
import time
from concurrent.futures import ThreadPoolExecutor

INTERVAL = 15        # seconds between rounds
TIMEOUT = 2          # one kubelet's summary
FRESH = 30           # a summary this young is used as it is
DROP_AFTER = 300     # a summary this old is not shown at all
WANTED_FOR = 60      # a host nobody asked about for this long is not read
MAX_WORKERS = 8

_lock = threading.Lock()
_state = {}          # node -> {"summary", "at", "misses", "before": the reading before, for rates}
_wanted = {}         # node -> when it was last asked about
_loop = {"thread": None, "stop": threading.Event(), "get": None}


def _path(node):
    return f"/api/v1/nodes/{node}/proxy/stats/summary"


def _read_all(get, nodes, now=None):
    """Read every node's summary at the same time and keep what came back."""
    nodes = sorted(set(nodes))
    if not nodes:
        return

    def one(node):
        try:
            summary = get(_path(node), timeout=TIMEOUT)
            return node, summary if isinstance(summary, dict) else None
        except Exception:
            return node, None

    with ThreadPoolExecutor(max_workers=min(MAX_WORKERS, len(nodes))) as pool:
        found = list(pool.map(one, nodes))
    now = time.time() if now is None else now
    with _lock:
        for node, summary in found:
            entry = _state.setdefault(node, {"summary": None, "at": 0.0, "misses": 0, "before": None})
            if summary is None:
                entry["misses"] += 1
            else:
                before = {"summary": entry["summary"], "at": entry["at"]} if entry["summary"] is not None else None
                entry.update(summary=summary, at=now, misses=0, before=before)


def running():
    thread = _loop["thread"]
    return bool(thread and thread.is_alive())


def readings(get, nodes, wait=True, now=None):
    """{node: {"summary", "at", "stale"}} for each of nodes with a summary
    under DROP_AFTER old, and the nodes noted as wanted for the loop.

    With the loop running, nothing is read for a node the loop has read;
    a node it has never read is read now when wait, or left to the loop's
    next round. Without the loop, every node is read now, all at once."""
    nodes = list(nodes)
    now = time.time() if now is None else now
    with _lock:
        for node in nodes:
            _wanted[node] = now
        if not running():
            missing = nodes
        elif wait:
            missing = [n for n in nodes if n not in _state]
        else:
            missing = []
    _read_all(get, missing, now)
    return latest(nodes, now)


def latest(nodes, now=None):
    """What is known of nodes, reading nothing: each one's last summary,
    and the one before it ("before", or None) to take rates from."""
    now = time.time() if now is None else now
    out = {}
    with _lock:
        for node in nodes:
            entry = _state.get(node)
            if not entry or entry["summary"] is None or now - entry["at"] > DROP_AFTER:
                continue
            out[node] = {"summary": entry["summary"], "at": entry["at"], "before": entry["before"],
                         "missed": bool(entry["misses"]),
                         "stale": bool(entry["misses"]) or now - entry["at"] > FRESH}
    return out


def run_round(get=None, now=None):
    """Read every host asked about lately (the loop's round)."""
    get = get or _loop["get"]
    now = time.time() if now is None else now
    with _lock:
        for node, asked in list(_wanted.items()):
            if now - asked > WANTED_FOR:
                del _wanted[node]
        nodes = list(_wanted)
    _read_all(get, nodes, now)


def _run():
    while not _loop["stop"].is_set():
        try:
            run_round()
        except Exception as error:
            print(f"kubelet stats: {str(error)[:160]}", flush=True)
        _loop["stop"].wait(INTERVAL)


def start(get):
    """Start this replica's loop once."""
    _loop["get"] = get
    if running():
        return
    _loop["stop"].clear()
    _loop["thread"] = threading.Thread(target=_run, name="kubelet-stats", daemon=True)
    _loop["thread"].start()


def stop():
    _loop["stop"].set()
    thread = _loop["thread"]
    if thread:
        thread.join(5)
    _loop["thread"] = None


def reset():
    """Forget everything (tests)."""
    stop()
    with _lock:
        _state.clear()
        _wanted.clear()
