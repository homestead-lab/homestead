"""How often a job that only waits looks at its host (#975).

The jobs loop asks every active job's resolver every few seconds. A job that
waits for hours (a disk preparation at its erase review, a Longhorn runtime
move waiting for a reboot, a host OS update installing, a SMART self-test)
would start a helper pod on its host on every ask. Its resolver instead calls
look(): the host is asked at most once per its kind's interval, and between
looks the last answer is given again, as host promotion does (PROBE_EVERY).

A look that raises keeps nothing: the resolver handles the error as before.
"""
import time

# Seconds between looks at the host, per job kind (a ratchet test holds every
# kind that waits on its host here).
INTERVALS = {
    "disk-v2-convert": 30,
    "longhorn-runtime": 30,
    "host-os": 30,
    "smart-test": 30,
}
# Kept on the job item, beside its ref and never inside it: a ref is hashed
# into reviews (the Longhorn runtime's retirement review) and compared with plans
# saved on hosts, so a look must not change it. The item is saved whole, so a
# restart keeps the cadence.
KEY = "host_cadence"
# A SMART self-test looks about 50 times in its expected run, but at least this
# often, so a finished, failed or aborted test shows within minutes.
SMART_MAX = 300


def interval(ref, kind):
    """The kind's interval; a SMART self-test's grows with its expected run."""
    every = INTERVALS[kind]
    if kind == "smart-test":
        every = min(SMART_MAX, max(every, int(ref.get("expected_seconds") or 0) // 50))
    return every


def look(item, kind, fn, now=None, force=False):
    """fn() (a resolver's answer from the host) when one is due, else the last
    answer. A look is due once the interval has passed, when the job's phase
    changed since the last one (a Recheck, a Retire, a step of its own), or
    when force is set, or when the last look's time is ahead of this clock (the
    leader moved to a pod whose clock is behind). The time is kept before the look is made, as promote
    does; a look that raises keeps no answer."""
    now = time.time() if now is None else now
    ref = item["ref"]
    state = item.get(KEY) or {}
    phase = ref.get("phase")
    if (not force and "answer" in state and state.get("phase") == phase
            and 0 <= now - state.get("at", 0) < interval(ref, kind)):
        return tuple(state["answer"])
    state = {"at": now, "phase": phase}
    item[KEY] = state
    result = fn()
    state["answer"] = list(result)
    state["phase"] = ref.get("phase")   # the look may move the phase itself
    return tuple(result)
