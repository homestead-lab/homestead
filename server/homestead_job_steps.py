"""The steps of jobs resolved outside the job store's lock, run side by side,
a few at a time (#933).

The leader's jobs loop used to run every job's step in turn, so one slow step
- a host step can take 60-90 s, a copy or a trim waits on Longhorn - held up
every other job, although no lock was held. Now each such step runs on its own
thread, and the loop goes on with its rounds:

- a job never runs two steps at once: a job whose step is in flight is not
  started again until that step has ended;
- at most WORKERS steps run at once, and a full pool takes the job that has
  waited longest first, so no job is starved;
- each step names what it works on (its claims: a host, an object, every
  volume); two jobs whose claims meet never step at the same time, so jobs on
  one host or one volume take turns as before;
- a step that runs past OVERDUE_SECONDS stops counting against WORKERS, so a
  hung step does not hold a worker for ever, but it keeps its job and its
  claims, and the loop's heartbeat names it. Python cannot stop a thread:
  the step's own timeouts (Kubernetes calls, host runs) are what end it. At
  most WORKERS such hung steps are carried, so threads stay bounded;
- a job waiting on a clash reserves its claims for the rest of the round, so
  later jobs that meet them (two data moves taking turns, say) never keep a
  job that claims more (the weekly trim) out for ever;
- wait() waits for the steps in flight: the loop's stop waits for them, as
  it waited for its round before, for up to OVERDUE_SECONDS.

Across replicas, each job's own lock file (homestead_operations._job_lock)
still keeps a new leader from running a step an old leader has in flight.
"""
import threading
import time

WORKERS = 3
# Longer than any bounded step: a host run is up to 60 s to start its helper
# and 120 s to run; a trim waits up to 60 s on Longhorn.
OVERDUE_SECONDS = 300

_cond = threading.Condition()
_running = {}    # job id -> {"claims", "label", "began"}
_last = {}       # job id -> when its last step began (monotonic), for fairness
_errors = []     # (job id, label, error, step) of steps that raised, until taken


def overlap(a, b):
    """Whether two sets of claims meet. A claim is a tuple: ("host", node),
    ("object", namespace, name), ("object", "*") for every object, or ("all",)
    for a job that says nothing about what it touches."""
    for x in a:
        for y in b:
            if x[:1] == ("all",) or y[:1] == ("all",):
                return True
            if x[:1] == y[:1] and (x[1:] == y[1:] or "*" in x[1:2] + y[1:2]):
                return True
    return False


def _counted(now):
    return sum(1 for row in _running.values() if now - row["began"] < OVERDUE_SECONDS)


def submit(job_id, claims, step, label="", reserved=()):
    """Run step() on a thread of its own if there is room: "started"; the
    job's step is still in flight: "running"; no room yet (the pool is full,
    or another job in flight claims the same thing): "waiting". reserved:
    the claims of jobs earlier in this round that are waiting; a later job
    whose claims meet them waits too, so it never overtakes them for ever."""
    with _cond:
        if job_id in _running:
            return "running"
        now = time.monotonic()
        if (_counted(now) >= WORKERS or len(_running) >= 2 * WORKERS or overlap(claims, reserved) or
                any(overlap(claims, row["claims"]) for row in _running.values())):
            return "waiting"
        _running[job_id] = {"claims": set(claims), "label": label or job_id, "began": now}
        _last[job_id] = now
        try:
            threading.Thread(target=_run, args=(job_id, step), name=f"job-step-{job_id}"[:40], daemon=True).start()
        except BaseException:
            _running.pop(job_id, None)  # never started: the job is free for a later round
            raise
    return "started"


def _run(job_id, step):
    try:
        step()
    except BaseException as error:
        with _cond:
            _errors.append((job_id, _running[job_id]["label"], error, step))
    finally:
        with _cond:
            _running.pop(job_id, None)
            _cond.notify_all()


def fair(ids, prune=True):
    """ids, the one whose last step began longest ago (or never) first.
    prune: forget the turns of jobs not in ids (pass the round's every job)."""
    with _cond:
        if prune:
            for gone in set(_last) - set(ids) - set(_running):
                del _last[gone]
        return sorted(ids, key=lambda job_id: _last.get(job_id, float("-inf")))


def in_flight():
    with _cond:
        return set(_running)


def wait_change(timeout=1.0):
    """Wait until some step ends, or timeout seconds."""
    with _cond:
        if _running:
            _cond.wait(timeout)


def wait(timeout=None):
    """Wait until no step is in flight; False if timeout ran out first."""
    with _cond:
        return _cond.wait_for(lambda: not _running, timeout)


def overdue():
    """The steps in flight for longer than OVERDUE_SECONDS: [(label, seconds)]."""
    now = time.monotonic()
    with _cond:
        return [(row["label"], int(now - row["began"])) for row in _running.values()
                if now - row["began"] >= OVERDUE_SECONDS]


def take_errors(steps=None):
    """The steps that raised since last asked - of the steps given (the
    callables passed to submit), or every one when None: [(job id, label,
    error)]. The rest stay to be taken."""
    with _cond:
        taken = [row for row in _errors if steps is None or any(row[3] is step for step in steps)]
        _errors[:] = [row for row in _errors if not any(row is t for t in taken)]
        return [row[:3] for row in taken]
