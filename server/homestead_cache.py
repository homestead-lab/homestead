"""Homestead's short-lived answers: each read of the cluster kept for a few
seconds under a key, so a page and its refresh, or two pages at once, ask
the API server once. server.py keeps the same dict and lock as _cache and
_lock, and hands them to the modules that read or clear entries.
Tests patch cached here."""
import threading
import time

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
# Bounds on _cache. An entry used to expire only when read, so a key never
# asked for again (the App Store feed under an old catalogue URL, an image or
# a name seen once) stayed until the restart. Now a sweep, at most every
# SWEEP_EVERY seconds when an answer is kept, drops each entry that is past
# the longest time any reader asked of it and that nobody has asked for in
# IDLE_SECONDS; an entry still asked for keeps its last copy for a reader to
# fall back on. Past CACHE_MAX keys the least recently used go first.
IDLE_SECONDS = 3600
SWEEP_EVERY = 300
CACHE_MAX = 4096
_used = {}          # key -> when it was last kept or asked for
_ttl = {}           # key -> the longest time a reader asked to keep it
_swept = [0.0]
# Keys a page keeps for a set time, refreshed by the leader's page snapshot
# on its own cycle (#978): key -> the least time any reader keeps it. A loop,
# /metrics or a dialog that asks for less would read the cluster again on
# almost every turn for numbers its page shows that old anyway. Set by
# homestead_routes (at_least); a write still forgets them at once.
_at_least = {}
# The leader's page snapshot: fresh(key) -> (as_of, value) while its part for
# key is served to the pages, else None (homestead_page_snapshot.read).
_parts = [lambda key: None]
# Per thread: inside fresh(), at_least() and the parts do not apply.
_scope = threading.local()
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
            _store(key, value, time.time())


def _store(key, value, at):
    """Keep value under key (the caller holds _lock), within the bounds."""
    now = time.time()
    if now - _swept[0] >= SWEEP_EVERY or now < _swept[0]:
        _sweep(now)
    if key not in _cache and len(_cache) >= CACHE_MAX:
        _evict(len(_cache) - CACHE_MAX + 1 + CACHE_MAX // 8)
    _cache[key] = (at, value)
    _used[key] = now


def _sweep(now):
    """Drop what nobody asks for any more (the caller holds _lock)."""
    _swept[0] = now
    for key, entry in list(_cache.items()):
        if key in _running:
            continue
        if now - _used.get(key, entry[0]) > IDLE_SECONDS and now - entry[0] >= _ttl.get(key, 0):
            _drop(key)
    for store in (_used, _ttl):
        for key in [key for key in store if key not in _cache and key not in _running]:
            del store[key]


def _evict(count):
    """Drop the count least recently used answers (the caller holds _lock)."""
    idle = sorted((key for key, entry in _cache.items() if key not in _running),
                  key=lambda key: _used.get(key, _cache[key][0]))
    for key in idle[:count]:
        _drop(key)
        _used.pop(key, None)


def _drop(key):
    """Drop key's kept answer here only. A pop or del on _cache may be told to
    the other copies as a write (#928); a drop for age or size is not one, so
    it goes past whatever _cache's own pop does."""
    dict.pop(_cache, key, None)


def _last(key):
    with _lock:
        e = _cache.get(key)
        if e:
            _used[key] = time.time()
        return e


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


def at_least(seconds, *keys):
    """Keep each of keys at least seconds, whatever a reader asks for, and
    serve the page snapshot's part for it while the pages are served it."""
    for key in keys:
        _at_least[key] = seconds


def use_parts(fresh):
    """The leader's page snapshot, for the keys at_least() names."""
    _parts[0] = fresh


def fresh(fn):
    """fn, run so that its reads of the pages' keys keep only the time each
    reader asks for and take no snapshot part: for a check made just before
    a write (a start's or a drain's capacity, a host's impact), which must
    see the cluster as it is now, not as the page last showed it."""
    def run(*args, **kwargs):
        _scope.depth = getattr(_scope, "depth", 0) + 1
        try:
            return fn(*args, **kwargs)
        finally:
            _scope.depth -= 1
    run.__name__, run.__doc__ = getattr(fn, "__name__", "fresh"), fn.__doc__
    return run


def cached(key, ttl, fn):
    if key in _at_least and not getattr(_scope, "depth", 0):
        # A part is served only within this reader's own bound: a part some
        # cycles late is the page's to show (it says how old), not a loop's.
        ttl = max(ttl, _at_least[key])
        part = _parts[0](key)
        if part is not None and time.time() - part[0] < ttl:
            return part[1]
    with _lock:
        _used[key] = now = time.time()
        if ttl > _ttl.get(key, 0):
            _ttl[key] = ttl
        e = _cache.get(key)
        if e and now - e[0] < ttl:
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
        _store(key, value, time.time() if at is None else at)
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
