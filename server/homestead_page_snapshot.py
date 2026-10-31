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
for itself again, as before.

A write on one replica is told to the others (#928): a forget or pop of a
part's key is noted, and the replica's loop, woken by it, records the key and
when in FORGETS on the shared volume (tell()). Every replica's loop looks at
that file each turn and forgets the key itself, dated when the write was
made (hear()): its own read and any leader part read before the write are no
longer served, and the leader, woken by the forget, reads the part again
within MIN_GAP. So the others usually serve a part from before a write for
a turn of the loop (LEADER_CHECK), not until the leader's next cycle. It is
best effort, as the volume is NFS: its attribute caching can delay a
replica's look at the file by a few seconds, and a write is dated by its
replica's clock, so the replicas' clocks are assumed in step (NTP). Missed,
it is no worse than before: the leader's next cycle.

Every replica changes FORGETS, so it is read, merged and replaced as one step
under a SharedLock, as the other shared files are. On NFS a replica can still
merge from a copy read before another's write and so write that key away.
Each replica checks the file on each look (hear()): a key of its own, told
in the last STALE_AFTER and now missing there, or older by over CLOCK_SKEW,
is told again on its next turn. The lock is waited on for LOCK_WAIT at
most, so a replica stuck holding it never holds this loop: the write is
tried again next turn. A lock NFS refuses outright (ENOLCK) also fails the
write, said once, rather than writing unlocked."""
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
# Writes told between the replicas: every replica reads and changes this
# file, so under a lock they all hold.
FORGETS = "page-forgets.json"
# Seconds the loop waits for that lock before trying again next turn.
LOCK_WAIT = 1
# A write of this replica's held in FORGETS up to this much earlier counts as
# held: another replica's clock may be that far behind (tell() never writes a
# time ahead of its own clock), and telling it again would only undo that.
CLOCK_SKEW = 5
_forgets_lock = SHARED.SharedLock("page-forgets", strict=True, timeout=LOCK_WAIT)
_told_lock = threading.Lock()
# pending: key -> when, written here and not yet told; known: key -> the
# latest forget told or heard, so neither is applied or told twice; mine:
# key -> the latest write told from here, checked on each look at the file;
# again: those another replica's merge wrote away, to tell again; seen: the
# file last read; failing: whether telling fails.
_told = {"pending": {}, "known": {}, "mine": {}, "again": {}, "seen": None, "failing": False}


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
    with _told_lock:
        _told.update(pending={}, known={}, mine={}, again={}, seen=None, failing=False)
    _wake.clear()


def wake():
    """Run a cycle soon rather than at its next turn."""
    _wanted.set()
    _wake.set()


def _note(key, when):
    """A write here dropped key: tell the other replicas (tell()), and on
    the leader run a cycle soon. Never blocks: it runs on the writer's
    thread."""
    with _told_lock:
        _told["pending"][key] = max(_told["pending"].get(key, 0.0), when)
    wake()


def _forgotten(keys):
    for key in set(keys) & set(PARTS):
        _note(key, CACHE.forgotten_at(key))


def _popped(key, when):
    if key in PARTS:
        _note(key, when)


CACHE.on_forget(_forgotten)
CACHE.on_pop(_popped)


def _register():
    """Each part's key known to the cache, so a forget_where() of it is heard
    though this replica never read it."""
    for key in PARTS:
        CACHE.epoch(key)


_register()


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
CACHE.use_parts(read)


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
    # Writes first: this replica's to the others, theirs here, so a cycle
    # due below never builds a part a write has already overtaken.
    tell()
    hear()
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


def _forgets_path():
    return os.path.join(SHARED.DIR, FORGETS)


def _told_forgets():
    try:
        forgets = _load(_forgets_path()).get("forgets")
    except (OSError, ValueError, AttributeError):
        return {}
    return forgets if isinstance(forgets, dict) else {}


def tell():
    """Record this replica's writes (forgets and pops of a part's key) in
    FORGETS for the other replicas, each key with its latest write. Never
    raises: a write that fails, held during a move of Homestead's own data,
    is tried again next turn and said once, as is one whose lock is held
    longer than LOCK_WAIT."""
    with _told_lock:
        known, again = _told["known"], _told["again"]
        pending = {key: when for key, when in _told["pending"].items() if when > known.get(key, 0.0)}
        _told["pending"] = dict(pending)    # what was heard, or already told, is dropped
        now = time.time()
        for key, when in list(again.items()):    # written away by another replica's merge
            if now - when >= STALE_AFTER:
                again.pop(key)                   # no part read before it is still served
                continue
            pending[key] = max(pending.get(key, 0.0), when)
        if not pending:
            return
    try:
        with _forgets_lock:
            forgets = _told_forgets()
            for key, when in pending.items():
                try:
                    # A clock ahead of this one does not hold the key past now.
                    held = min(float(forgets.get(key) or 0.0), time.time())
                except (TypeError, ValueError):
                    held = 0.0
                forgets[key] = max(held, when)
            SHARED.write_json(_forgets_path(), {"forgets": forgets}, separators=(",", ":"))
    except Exception as error:
        if not _told["failing"]:
            _told["failing"] = True
            reason = (f"{FORGETS} is locked elsewhere for over {LOCK_WAIT} s" if isinstance(error, TimeoutError)
                      else str(error)[:160])
            print(f"page snapshot: a write not told to the other replicas: {reason}", flush=True)
        return
    _told["failing"] = False
    with _told_lock:
        for key, when in pending.items():
            known[key] = max(known.get(key, 0.0), when)
            _told["mine"][key] = max(_told["mine"].get(key, 0.0), when)
            if _told["pending"].get(key, 0.0) <= when:
                _told["pending"].pop(key, None)
            if again.get(key, 0.0) <= when:
                again.pop(key, None)


def _check_mine(forgets, now):
    """Tell again, next turn, each write of this replica's from the last
    STALE_AFTER that forgets no longer holds: another replica merged from a
    copy of the file read before it, and wrote it away (NFS)."""
    with _told_lock:
        mine, again = _told["mine"], _told["again"]
        for key, when in list(mine.items()):
            if now - when >= STALE_AFTER:
                mine.pop(key)            # no part read before it is still served
                again.pop(key, None)
                continue
            try:
                held = float(forgets.get(key) or 0.0)
            except (TypeError, ValueError):
                held = 0.0
            if held < when - CLOCK_SKEW:
                again[key] = max(again.get(key, 0.0), when)
        missing = bool(again)
    if missing:
        _wake.set()     # tell it on the next turn, at once; no cycle is wanted for it


def hear(now=None):
    """Forget here each part's key another replica has written since this
    one last looked, dated when that write was made (never later than now).
    A write older than STALE_AFTER changes nothing: no part read before it
    is still served. This replica's own writes are checked as well
    (_check_mine())."""
    path = _forgets_path()
    try:
        status = os.stat(path)
    except OSError:
        return
    seen = (path, status.st_ino, status.st_mtime_ns, status.st_size)
    if seen == _told["seen"]:
        return
    _told["seen"] = seen
    now = time.time() if now is None else now
    forgets = _told_forgets()
    _check_mine(forgets, now)
    for key, when in forgets.items():
        if key not in PARTS:
            continue
        try:
            when = min(float(when), now)
        except (TypeError, ValueError):
            continue
        with _told_lock:
            if when <= _told["known"].get(key, 0.0):
                continue
            _told["known"][key] = when
        if now - when >= STALE_AFTER or when <= CACHE.forgotten_at(key):
            continue
        CACHE.epoch(key)     # so the key is forgotten here though nothing read it yet
        CACHE.forget_where(lambda k, key=key: k == key, at=when)
        # The forget is dated; the held part goes with it, so take_in() does
        # not read its missing cache entry as an undated pop and hold off a
        # leader part read after the write but before this turn.
        with _lock:
            _parts.pop(key, None)


def sleep(seconds):
    """Wait up to seconds, or until something wakes the loop."""
    _wake.wait(seconds)
    _wake.clear()
