"""Homestead's short-lived answers: each read of the cluster kept for a few
seconds under a key, so a page and its refresh, or two pages at once, ask
the API server once. server.py keeps the same dict and lock as _cache and
_lock, and hands them to the modules that read or clear entries.
Tests patch cached here."""
import threading
import time

_cache = {}
_lock = threading.Lock()
# The computation now running for each key (#705): a caller that misses
# while one runs waits for its answer instead of asking the API server again.
_running = {}
# Each key's generation. forget_where() moves it on, so a computation that
# started before a write is not kept as the answer after it.
_epoch = {}
# When each key was last forgotten, and who wants to hear of it: the leader's
# page snapshot (homestead_page_snapshot) serves a part only if it was read
# after the last forget of its key, and runs again when one is forgotten.
_forgot = {}
_listeners = []
# How long a caller waits on another's computation before giving up on it.
WAIT_SECONDS = 60


class _Flight:
    def __init__(self, epoch):
        self.done = threading.Event()
        self.owner = threading.get_ident()
        self.epoch = epoch
        self.value = self.error = None


def _keep(key, epoch, value):
    with _lock:
        if _epoch.get(key, 0) == epoch:
            _cache[key] = (time.time(), value)


def _last(key):
    with _lock:
        return _cache.get(key)


def _compute(key, fn, epoch):
    try:
        v = fn()
    except Exception:
        e = _last(key)
        if e:
            return e[1]
        raise
    _keep(key, epoch, v)
    return v


def cached(key, ttl, fn):
    with _lock:
        e = _cache.get(key)
        if e and time.time() - e[0] < ttl:
            return e[1]
        flight = _running.get(key)
        mine = flight is None
        if mine:
            flight = _running[key] = _Flight(_epoch.get(key, 0))
    if not mine and flight.owner == threading.get_ident():
        return _compute(key, fn, flight.epoch)  # the computation asks for its own key: no waiting on itself
    if not mine:
        if not flight.done.wait(WAIT_SECONDS):
            e = _last(key)
            if e:
                return e[1]
            raise TimeoutError(f"{key} is still being read")
        if flight.error is not None:
            raise flight.error
        return flight.value
    try:
        flight.value = _compute(key, fn, flight.epoch)
        return flight.value
    except BaseException as ex:
        flight.error = ex
        raise
    finally:
        with _lock:
            if _running.get(key) is flight:
                del _running[key]
        flight.done.set()


def refresh(key, fn):
    """fn() now, kept under key for the pages, or its error: for a loop that
    must have a fresh answer (no falling back to the last one)."""
    with _lock:
        epoch = _epoch.get(key, 0)
    v = fn()
    _keep(key, epoch, v)
    return v


def epoch(key):
    """key's generation now, for a reader that keeps its answer later with
    keep(): from now on a forget of key is recorded even when nothing is kept."""
    with _lock:
        return _epoch.setdefault(key, 0)


def keep(key, value, epoch, at=None):
    """Keep value under key, dated at (default now), unless key was forgotten
    since epoch was read. True when it was kept."""
    with _lock:
        if _epoch.get(key, 0) != epoch:
            return False
        _cache[key] = (time.time() if at is None else at, value)
        return True


def kept_at(key):
    """When key's kept answer was read (its date), or None."""
    with _lock:
        e = _cache.get(key)
        return e[0] if e else None


def kept(key):
    """(when it was read, value) of key's kept answer, or None."""
    return _last(key)


def forgotten_at(key):
    """When key was last forgotten (time.time()), or 0.0."""
    with _lock:
        return _forgot.get(key, 0.0)


def on_forget(fn):
    """Call fn(keys) after each forget_where() that dropped any keys."""
    _listeners.append(fn)


def forget_where(match):
    """Drop every kept or running answer whose key matches, so the next
    caller reads again and nothing computed before now is kept after it."""
    with _lock:
        now = time.time()
        dropped = {key for key in {*_cache, *_running, *_epoch} if match(key)}
        for key in dropped:
            _cache.pop(key, None)
            _running.pop(key, None)
            _epoch[key] = _epoch.get(key, 0) + 1
            _forgot[key] = now
    if dropped:
        for fn in list(_listeners):
            try:
                fn(dropped)
            except Exception as error:   # the write has happened: never fail it here
                print(f"cache: a forget listener failed: {str(error)[:160]}", flush=True)
