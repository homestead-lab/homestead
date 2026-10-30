"""Homestead's short-lived answers: each read of the cluster kept for a few
seconds under a key, so a page and its refresh, or two pages at once, ask
the API server once. server.py keeps the same dict and lock as _cache and
_lock, and hands them to the modules that read or clear entries.
Tests patch cached here."""
import threading
import time

import homestead_read_deadline as DEADLINE


class _Answers(dict):
    """The kept answers. Many writes drop their page's key with a plain
    _cache.pop(key) rather than forget_where(); a pop is told to the
    on_pop() listeners, so the other replicas hear of that write too (#928)."""

    def pop(self, key, *default):
        try:
            return super().pop(key, *default)
        finally:
            _popped(key)

    def __delitem__(self, key):
        super().__delitem__(key)
        _popped(key)

    def clear(self):
        """Drop every answer (a config restore) as a forget of every key this
        replica knows of, held or not: a read started before it is not kept,
        and the other replicas hear of it, as one never read here may be
        served by them."""
        forget_where(lambda key: True)
        super().clear()


_cache = _Answers()
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
# Who wants to hear of a key popped from _cache: fn(key, when). Called on the
# popping thread, maybe under its caller's locks, so it must not block or
# take this module's _lock.
_pop_listeners = []
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
    if mine and DEADLINE.remaining() is not None:
        # A request's computation runs without its deadline (#984), on a
        # thread of its own: it ends and is kept for the next caller, and a
        # loop waiting on it gets the whole answer. The request waits only
        # for what it has left.
        flight.owner = None
        threading.Thread(target=_fly, args=(key, fn, flight), daemon=True, name=f"cache {key}"[:60]).start()
        mine = False
    if not mine:
        left = DEADLINE.remaining()
        if not DEADLINE.wait(flight.done, WAIT_SECONDS):
            e = _last(key)
            if e:
                return e[1]
            if left is not None and left < WAIT_SECONDS:
                raise DEADLINE.spent()
            raise TimeoutError(f"{key} is still being read")
        if flight.error is not None:
            if isinstance(flight.error, DEADLINE.Spent) and left is not None:
                DEADLINE.spent()     # this request's answer is cut too
            raise flight.error
        return flight.value
    _fly(key, fn, flight)
    if flight.error is not None:
        raise flight.error
    return flight.value


def _fly(key, fn, flight):
    """Compute key for everyone waiting on flight."""
    flight.owner = threading.get_ident()
    try:
        flight.value = _compute(key, fn, flight.epoch)
    except BaseException as ex:
        flight.error = ex
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


def on_pop(fn):
    """Call fn(key, when) after each pop of key from _cache."""
    _pop_listeners.append(fn)


def _popped(key):
    when = time.time()
    for fn in list(_pop_listeners):
        try:
            fn(key, when)
        except Exception as error:   # the write has happened: never fail it here
            print(f"cache: a pop listener failed: {str(error)[:160]}", flush=True)


def forget_where(match, at=None):
    """Drop every kept or running answer whose key matches, so the next
    caller reads again and nothing computed before now is kept after it.
    at, when given, dates the forget: a write another replica made then
    (#928), never later than now. Only keys this replica has kept, is
    reading, or has asked epoch() of are dropped."""
    with _lock:
        now = time.time()
        dropped = {key for key in {*_cache, *_running, *_epoch} if match(key)}
        for key in dropped:
            dict.pop(_cache, key, None)
            _running.pop(key, None)
            _epoch[key] = _epoch.get(key, 0) + 1
            _forgot[key] = now if at is None else max(_forgot.get(key, 0.0), min(at, now))
    if dropped:
        for fn in list(_listeners):
            try:
                fn(dropped)
            except Exception as error:   # the write has happened: never fail it here
                print(f"cache: a forget listener failed: {str(error)[:160]}", flush=True)


def drop_where(cache, match):
    """A write's bust: drop every answer in cache whose key matches. On the
    kept answers that is forget_where(), which also reaches the keys this
    replica knows of but does not hold, so the other replicas hear of the
    write though this one never read the page (#928). Walking the keys held
    here would miss those. Another dict (a test's own, or a module not yet
    bound to the kept answers) has its matching keys popped, and tells
    nothing."""
    if cache is _cache:
        forget_where(lambda key: isinstance(key, str) and match(key))
        return
    for key in [key for key in cache if match(key)]:
        cache.pop(key, None)
