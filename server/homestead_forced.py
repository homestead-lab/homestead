"""Forced refreshes (?force=1) on reads any viewer may make: the image-update
scan, the portal's link probes, API discovery, the components' release
lookups and platform detection. Each is costly for the cluster, the registries or GitHub, so per
key a forced refresh runs one at a time (callers that arrive while it runs
share its answer) and at most once every MIN_INTERVAL seconds, or the
key's own interval (a forced call inside that window gets the last forced
answer). A forget of the key,
here or in homestead_cache, ends the window so a change is seen at once.
Quiet (unforced) reads do not come here."""
import threading
import time

import homestead_cache as CACHE

MIN_INTERVAL = 30
clock = time.time       # tests patch it; homestead_cache dates its forgets with time.time too

_lock = threading.Lock()
_last = {}              # key -> (started, value) of the last forced run that succeeded
_running = {}           # key -> _Flight now running
_forgot = {}            # key -> when forget(key) was last called


class _Flight:
    def __init__(self, started):
        self.started = started
        self.done = threading.Event()
        self.waiters = 0
        self.value = self.error = None


def _since(key):
    """The last forget of key: nothing that started before it is reused."""
    return max(_forgot.get(key, 0.0), CACHE.forgotten_at(key))


def run(key, fn, interval=None):
    """fn() as a forced refresh under key: shared while it runs, reused for
    interval (default MIN_INTERVAL) seconds after it started. Errors reach every caller of that run and
    are not kept."""
    CACHE.epoch(key)        # from now on homestead_cache records each forget of key
    with _lock:
        now, since = clock(), _since(key)
        kept = _last.get(key)
        window = MIN_INTERVAL if interval is None else interval
        if kept and since < kept[0] and now - kept[0] < window:
            return kept[1]
        flight = _running.get(key)
        mine = flight is None or flight.started <= since
        if mine:
            flight = _running[key] = _Flight(now)
        else:
            flight.waiters += 1
    if not mine:
        flight.done.wait()
        if flight.error is not None:
            raise flight.error
        return flight.value
    try:
        flight.value = fn()
        return flight.value
    except BaseException as error:
        flight.error = error
        raise
    finally:
        with _lock:
            if _running.get(key) is flight:
                del _running[key]
            if flight.error is None and _since(key) < flight.started:
                _last[key] = (flight.started, flight.value)
        flight.done.set()


def forget(*keys):
    """The next forced call under each key runs again (after a write it reads)."""
    with _lock:
        for key in keys:
            _forgot[key] = clock()
            _last.pop(key, None)


def reset():
    """Drop everything kept (tests)."""
    with _lock:
        _last.clear()
        _running.clear()
        _forgot.clear()
