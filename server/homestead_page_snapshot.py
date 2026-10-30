"""The leader's page snapshot (#705 step 4b). One cycle on the leader reads
the cluster once for the Dashboard, Hosts, Apps, Volumes and storage pages:
the pod list and Longhorn's volumes are read once and shared, and each page's
part is kept here and in homestead_cache under its route's key. The routes
serve a part through homestead_routes.page() while it is fresh; otherwise
they compute as before.

A part is kept with as_of, when its cycle started reading, so it is never
dated newer than the cluster state it holds. A part read before a write that
forgets its key (homestead_kube.fresh_reads, homestead_routes.forget) is not
kept, nor served after it; the forget also wakes the cycle.

homestead_overview_server's _sampler drives the cycle on the leader and
passes the builders in, so this module imports none of the page modules.

The other replicas serve the leader's parts too (#906), as they show the
leader's other state (homestead_leader_state): after each cycle the leader
writes its fresh parts to FILE on the shared data volume, and each other
replica's sampler loop takes in a newer file - never a request. A part taken
in is kept in this replica's cache under its as_of and served by the same
rules as the leader's own: not after STALE_AFTER, not after a write here
forgets or pops its key, and not over a newer read of this replica's own.
When the leader stops writing, its parts age out and each replica computes
for itself again, as before."""
import hashlib
import json
import os
import threading
import time

import homestead_cache as CACHE
import homestead_routes as ROUTER
import homestead_shared as SHARED

# The parts, by their routes' cache keys.
PARTS = ("nodes", "wl", "vol", "stor", "lhcap", "ov")
# A part older than this is not served: three missed cycles.
STALE_AFTER = 60
# A woken cycle starts no sooner than this after the last one started, so a
# burst of writes does not run cycles back to back.
MIN_GAP = 2
# How often the loop looks at leadership and wakes between cycles.
LEADER_CHECK = 2

_lock = threading.Lock()
_parts = {}             # key -> (as_of, value)
_wake = threading.Event()
_wanted = threading.Event()
_state = {"last": None, "leader": False}
# The leader's parts for the other replicas, on the shared data volume.
FILE = "page-snapshot.json"
# The other replicas ask for it by touching this file whenever they look (at
# most every ASK_EVERY s); the leader writes only while it saw the file change
# within ASK_FRESH s, on its own monotonic clock: the file's time is set by
# another node's clock (on NFS, the server's), so it is never compared with
# the leader's. One replica (the default) writes nothing.
WANTED = "page-snapshot.wanted"
ASK_EVERY = 30
ASK_FRESH = 120
# Parts that did not change are written again only this often, so they never
# go stale for the other replicas (STALE_AFTER).
REFRESH = STALE_AFTER // 2
_shared = {"seen": None, "failing": False,   # the file last taken in; whether writing it fails
           "asked": None, "ask_failing": False,   # this follower's last ask (monotonic); whether it fails
           "ask_seen": None, "ask_changed": None,   # the leader's last look at WANTED; when it changed (monotonic)
           "digest": None, "written": 0.0}
_floor = {}             # key -> no leader part read before this is taken in (a pop here)


def cadence():
    """Seconds between cycles: the pages' own cache time, so no number is
    older than it was before the snapshot."""
    return ROUTER.PAGE_SECONDS


def reset():
    """Forget every part and the loop's state (tests, and a lost lease)."""
    with _lock:
        _parts.clear()
    _wanted.clear()
    _state.update(last=None, leader=False)
    _shared.update(seen=None, failing=False, asked=None, ask_failing=False, ask_seen=None,
                   ask_changed=None, digest=None, written=0.0)
    _floor.clear()


def wake():
    """Run a cycle soon rather than at its next turn."""
    _wanted.set()
    _wake.set()


def _forgotten(keys):
    if set(keys) & set(PARTS):
        wake()


CACHE.on_forget(_forgotten)


def read(key, now=None):
    """(as_of, value) for key, or None when there is no part, it is older than
    STALE_AFTER, key was forgotten after its cycle started reading, or the
    part is no longer what the cache holds under key: a module that pops its
    keys after a write (homestead_hardware, homestead_storage_classes, ...)
    or a newer read of key wins over it."""
    with _lock:
        part = _parts.get(key)
    if part is None:
        return None
    now = time.time() if now is None else now
    if now - part[0] >= STALE_AFTER or part[0] <= CACHE.forgotten_at(key) or CACHE.kept_at(key) != part[0]:
        return None
    return part


ROUTER.use_snapshot(read)


def cycle(inputs, builders):
    """Read each input once (inputs: {name: fn()}), then build each part in
    order (builders: [(key, fn(got))], got holding the inputs and the parts
    built so far). A part that fails, or whose input failed, keeps its last
    value and as_of. Returns {key: True when kept, False when it failed,
    None when a write overtook it (forgotten while it was read)}."""
    epochs = {key: CACHE.epoch(key) for key, _ in builders}
    as_of = time.time()
    got, done = {}, {}
    for name, fn in inputs.items():
        try:
            got[name] = fn()
        except Exception as error:
            print(f"page snapshot: {name} not read: {str(error)[:160]}", flush=True)
    for key, fn in builders:
        try:
            value = fn(got)
        except Exception as error:
            print(f"page snapshot: {key} not built: {str(error)[:160]}", flush=True)
            done[key] = False
            continue
        got[key] = value
        with _lock:
            if CACHE.keep(key, value, epochs[key], at=as_of):
                done[key] = True
                _parts[key] = (as_of, value)
            else:
                done[key] = None
    return done


def tick(is_leader, inputs, builders, now=None):
    """One turn of the loop: on the leader, a cycle when one is due (every
    cadence(), at once on boot and on becoming the leader, and soon after a
    wake). Returns what cycle() returned, or None when none ran."""
    leader = bool(is_leader())
    now = time.monotonic() if now is None else now
    became = leader and not _state["leader"]
    if not leader:
        if _state["leader"]:
            # A replica that lost the lease no longer refreshes its parts:
            # it serves the new leader's, as every other replica does.
            with _lock:
                _parts.clear()
        _state["leader"] = False
        take_in(mono=now)
        return None
    _state["leader"] = True
    last = _state["last"]
    due = (last is None or became or now - last >= cadence()
           or (_wanted.is_set() and now - last >= MIN_GAP))
    if not due:
        return None
    _wanted.clear()
    _state["last"] = now
    done = cycle(inputs, builders)
    share(mono=now)
    return done


def _path():
    return os.path.join(SHARED.DIR, FILE)


def _asked(mono):
    """Whether another replica asked for the parts within ASK_FRESH: whether
    WANTED changed (inode or mtime_ns) within ASK_FRESH of the leader's own
    monotonic clock (mono). A file first seen counts as one ask, so a leader
    that just started writes at once for a follower already asking."""
    try:
        status = os.stat(os.path.join(SHARED.DIR, WANTED))
    except OSError:
        return False
    seen = (status.st_ino, status.st_mtime_ns)
    if seen != _shared["ask_seen"]:
        _shared.update(ask_seen=seen, ask_changed=mono)
    return mono - _shared["ask_changed"] < ASK_FRESH


def _ask(mono):
    """On a replica that is not the leader: ask for the parts (a touch of
    WANTED), at most every ASK_EVERY of mono (its monotonic clock), through
    the self-data write fence like every other write to the data volume.
    Says once when asking fails, then nothing until it works again. Never
    raises."""
    asked = _shared["asked"]
    if asked is not None and mono - asked < ASK_EVERY:
        return
    _shared["asked"] = mono
    path = os.path.join(SHARED.DIR, WANTED)
    try:
        with SHARED.write_scope(path):
            if SHARED.WRITE_GUARD is not None:
                SHARED.WRITE_GUARD(path)
            with open(path, "a", encoding="utf-8"):
                pass
            os.utime(path, None)
        _shared["ask_failing"] = False
    except Exception as error:
        # e.g. during a move of Homestead's own data: asked again ASK_EVERY later.
        if not _shared["ask_failing"]:
            _shared["ask_failing"] = True
            print(f"page snapshot: not asked: {str(error)[:160]}", flush=True)


def share(now=None, mono=None):
    """Write the leader's fresh parts for the other replicas, while one asks
    for them (now: wall time, as the parts are dated; mono: the loop's
    monotonic clock, for the asks). Never raises: a page they compute for
    themselves is no worse than before."""
    now = time.time() if now is None else now
    if not _asked(time.monotonic() if mono is None else mono):
        return
    with _lock:
        keys = list(_parts)
    parts = {}
    for key in keys:
        part = read(key, now)
        if part is not None:
            parts[key] = [part[0], part[1]]
    try:
        digest = hashlib.sha256(json.dumps({k: v[1] for k, v in parts.items()}, sort_keys=True,
                                           separators=(",", ":"), default=str).encode()).hexdigest()
    except (TypeError, ValueError):
        digest = None    # not comparable (e.g. mixed key types): always written
    # share() runs after a cycle, so skip only while the next cycle is still
    # under REFRESH: rewriting on the first cycle past it would age a follower's
    # part by a whole extra cycle.
    if digest is not None and digest == _shared["digest"] and now - _shared["written"] + cadence() < REFRESH:
        return
    try:
        SHARED.write_json(_path(), {"written": now, "parts": parts}, separators=(",", ":"))
        _shared.update(failing=False, digest=digest, written=now)
    except Exception as error:
        # During a move of Homestead's own data, writes are held: tried again next cycle.
        if not _shared["failing"]:
            _shared["failing"] = True
            print(f"page snapshot: not shared: {str(error)[:160]}", flush=True)


def _load(path):
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


def take_in(now=None, mono=None):
    """On a replica that is not the leader: ask for the leader's parts
    (_ask, on mono, the loop's monotonic clock), and take them in when its
    file changed since the last look. A part is taken in only while fresh,
    newer than what is held, newer than the last forget of its key here,
    and newer than this replica's own kept read of it.

    A module that pops its key from the cache after a write leaves no time
    behind. So a held part whose key is no longer in the cache sets a floor
    for its key at the time that is noticed, every look: a leader part is
    then taken in only when its cycle started after that, so never one read
    before the write. (A newer read of this replica's own in its place is
    no write: the kept-read rule below covers it.)"""
    now = time.time() if now is None else now
    _ask(time.monotonic() if mono is None else mono)
    with _lock:
        for key, held in list(_parts.items()):
            if CACHE.kept_at(key) is None:
                _parts.pop(key)
                _floor[key] = now
    path = _path()
    try:
        status = os.stat(path)
    except OSError:
        return
    # Each write replaces the file (homestead_shared.write_json), so a new
    # one has a new inode as well as a new time.
    seen = (path, status.st_ino, status.st_mtime_ns, status.st_size)
    if seen == _shared["seen"]:
        return
    _shared["seen"] = seen
    try:
        parts = _load(path).get("parts")
    except (OSError, ValueError, AttributeError):
        return
    if not isinstance(parts, dict):
        return
    for key, part in parts.items():
        if key not in PARTS or not isinstance(part, list) or len(part) != 2:
            continue
        try:
            as_of = float(part[0])
        except (TypeError, ValueError):
            continue
        # The epoch first, as cycle() does: a forget after this point makes
        # keep() refuse, so a part read before it is never kept.
        epoch = CACHE.epoch(key)
        if (now - as_of >= STALE_AFTER or as_of <= CACHE.forgotten_at(key)
                or as_of <= _floor.get(key, 0.0)):
            continue
        with _lock:
            held, kept = _parts.get(key), CACHE.kept_at(key)
            if (held and held[0] >= as_of) or (kept is not None and kept >= as_of):
                continue
            if CACHE.keep(key, part[1], epoch, at=as_of):
                _parts[key] = (as_of, part[1])


def sleep(seconds):
    """Wait up to seconds, or until something wakes the loop."""
    _wake.wait(seconds)
    _wake.clear()
