"""metrics-server's CPU and memory readings, read in the background (#906).

Hosts and Apps show each host's and each app's CPU and memory from
metrics-server. They used to read it inside the request, with kget's 10 s
timeout, so a metrics-server that hung held those pages back by up to 10 s,
and one that failed left the figures at nothing with no word why.

Each replica's loop (started with the node probes) reads both lists every
INTERVAL with a TIMEOUT of a few seconds and keeps the last good reading. A
request takes that reading and never waits on metrics-server. With it comes
the reading's state, which the pages show:

- ok: the last read answered;
- waiting: the loop has not finished its first read;
- stale: metrics-server has stopped answering, and the figures are its last
  answer (served up to KEEP_FOR after it);
- unavailable: it is not answering and there is no answer recent enough;
- absent: the cluster has no metrics API at all (404).

Before the loop starts, and in tests, a request reads at once, with the same
short timeout, and keeps nothing."""
import threading
import time
import urllib.error

NODES = "/apis/metrics.k8s.io/v1beta1/nodes"
PODS = "/apis/metrics.k8s.io/v1beta1/pods"
PATHS = {"nodes": NODES, "pods": PODS}
INTERVAL = 15      # metrics-server itself scrapes every 15 s by default
TIMEOUT = 2        # seconds one read may take
KEEP_FOR = 300     # how long the last answer is served after metrics-server stops answering
FIRST_WAIT = 3     # how long a request waits for the loop's first read

_lock = threading.Lock()
_state = {}        # kind -> entry (_blank())
_loop = {"thread": None, "stop": threading.Event(), "done": threading.Event(), "get": None}


def _blank():
    return {"items": None, "last_ok": None, "error": None, "absent": False, "tried": False}


def _detail(error):
    if isinstance(error, urllib.error.HTTPError):
        return f"HTTP {error.code}"
    if isinstance(error, TimeoutError) or "timed out" in str(error).lower():
        return f"no answer within {TIMEOUT} s"
    return str(error)[:120] or type(error).__name__


def _fetch(get, kind):
    """(items, None), or (None, the error)."""
    try:
        return (get(PATHS[kind], timeout=TIMEOUT) or {}).get("items") or [], None
    except Exception as error:
        return None, error


def _record(entry, items, error, now):
    entry["tried"] = True
    if error is None:
        entry.update(items=items, last_ok=now, error=None, absent=False)
    else:
        entry.update(error=_detail(error),
                     absent=isinstance(error, urllib.error.HTTPError) and error.code == 404)


def _status(entry, now):
    last_ok = entry["last_ok"]
    if not entry["tried"]:
        state = "waiting"
    elif entry["error"] is None and last_ok is not None and now - last_ok >= 3 * INTERVAL:
        # The loop is stuck in a read: its last answer is getting old.
        return {"state": "stale", "last_ok": last_ok, "age_s": max(0, round(now - last_ok)),
                "detail": f"not read since, for {round(now - last_ok)} s"}
    elif entry["error"] is None:
        state = "ok"
    elif entry["absent"]:
        state = "absent"
    elif last_ok is not None and now - last_ok < KEEP_FOR:
        state = "stale"
    else:
        state = "unavailable"
    return {"state": state, "last_ok": last_ok,
            "age_s": None if last_ok is None else max(0, round(now - last_ok)),
            "detail": entry["error"] or ""}


def run_round(get=None, now=None):
    """Read both lists once and keep what each answered."""
    get = get or _loop["get"]
    for kind in PATHS:
        items, error = _fetch(get, kind)
        with _lock:
            _record(_state.setdefault(kind, _blank()), items, error, time.time() if now is None else now)
    _loop["done"].set()


def read(kind, get, now=None):
    """(items, status) for kind, "nodes" or "pods": the loop's last reading
    while it runs (never reading metrics-server here), otherwise read now
    with get and the short timeout. items is empty unless the state is ok
    or stale."""
    if running():
        if not _loop["done"].is_set():
            _loop["done"].wait(FIRST_WAIT)
        with _lock:
            entry = dict(_state.get(kind) or _blank())
    else:
        entry = _blank()
        items, error = _fetch(get, kind)
        _record(entry, items, error, time.time() if now is None else now)
    status = _status(entry, time.time() if now is None else now)
    return (entry["items"] or []) if status["state"] in ("ok", "stale") else [], status


def running():
    thread = _loop["thread"]
    return bool(thread and thread.is_alive())


def _run():
    while not _loop["stop"].is_set():
        try:
            run_round()
        except Exception as error:
            _loop["done"].set()
            print(f"metrics-server: {str(error)[:160]}", flush=True)
        _loop["stop"].wait(INTERVAL)


def start(get):
    """Start this replica's loop once."""
    _loop["get"] = get
    if running():
        return
    _loop["stop"].clear()
    _loop["thread"] = threading.Thread(target=_run, name="metrics-server", daemon=True)
    _loop["thread"].start()


def stop():
    _loop["stop"].set()
    thread = _loop["thread"]
    if thread:
        thread.join(5)
    _loop["thread"] = None


def reset():
    """Forget every reading (tests)."""
    with _lock:
        _state.clear()
    _loop["done"].clear()
