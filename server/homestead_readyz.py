"""/readyz (#736): whether this Homestead can do its work, in one bit.

Open like /healthz, because kube probes and outside uptime checkers cannot
sign in, so it says nothing but {"ready": true} (200) or {"ready": false}
(503). Which check failed is in /metrics (homestead_metrics, behind a key).

Ready means the Kubernetes API answered GET /version within the last 30 s
and /data took a small write. Both checks are cached: at most one API call
per 10 s and, per replica, one write per 30 s, or per 10 s after a write that
didn't succeed, run by one caller at a time; whoever else asks meanwhile gets
the last result. So a burst of probes, or someone hostile, cannot load the API
server or the volume. Until both have run once the answer is no.

The self-data fence answers /readyz in server.py's guard exactly as it
answers /healthz (homestead_web_paths.handoff_body). The probe write itself
goes through the fence and the activity barrier too (homestead_shared's
write scope and guard, as every other /data writer does): while the fence
holds, or a freeze is draining activity, nothing is written and the answer
is not ready. /metrics reads the last /data result and never writes.

/readyz?check=data is what the pod's readiness probe asks (#791): only the
volume counts, so an API outage leaves this copy in the Service and the page
can still say the API is down. A deliberate hold (the fence, a freeze's drain)
counts as ready there, as /healthz does: a data move's source must not leave
the Service before its fence is published. A volume the barrier can't use, or
a write that fails or hangs, counts as not ready. Plain /readyz is unchanged,
except that it too stops trusting a write stuck for over DATA_STUCK seconds,
and that it too asks again sooner after a write that didn't succeed: kubelet
takes a copy out after 3 failures 10 s apart, so one bad write kept for 30 s
would do what only a lasting fault should.
"""
import os
import re
import threading
import time

import homestead_kube as KUBE
import homestead_routes as MODULE_ROUTES
import homestead_self_data_fence as FENCE
import homestead_shared as SHARED
from homestead_storage_journal import Held

DATA_DIR = os.environ.get("DATA_DIR", "/data")
# One probe file per pod, so replicas sharing /data never race on it.
POD_NAME = re.sub(r"[^A-Za-z0-9._-]", "", os.environ.get("HOSTNAME", "")) or "homestead"

API_EVERY, API_FRESH, API_TIMEOUT = 10, 30, 2
DATA_EVERY, DATA_RETRY = 30, 10
# A write that hangs never fails: its caller keeps the data lock, and everyone else
# would read the last result, True, for ever.
DATA_STUCK = 60

_clock = time.monotonic
_lock = threading.Lock()       # the API check
_data_lock = threading.Lock()  # the /data write, apart so a hung write leaves the API check running
# None: not checked yet.
START = {"api_checked": None, "api_ok_at": None, "api_seconds": None, "data_checked": None, "data_ok": None,
         "data_held": None, "data_started": None}
_state = dict(START)


def _write_probe():
    """Write, flush to disk and remove a small file in /data, inside the
    self-data activity barrier and past the fence; Held when either refuses."""
    path = os.path.join(DATA_DIR, f".readyz-{POD_NAME}")
    with SHARED.write_scope(path):
        if SHARED.WRITE_GUARD is not None:
            SHARED.WRITE_GUARD(path)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0), 0o600)
        try:
            os.write(fd, b"ready\n")
            os.fsync(fd)
        finally:
            os.close(fd)
        try:
            os.remove(path)
        except OSError:
            pass    # written is what was asked; the next probe truncates it


def _due(checked, every, now):
    return checked is None or now - checked >= every


def _refresh_api(now):
    """The API check, if due and nobody else is running it."""
    if not _lock.acquire(blocking=False):
        return
    try:
        if _due(_state["api_checked"], API_EVERY, now):
            _state["api_checked"] = now
            started = _clock()
            try:
                KUBE.raw_get("/version", timeout=API_TIMEOUT)
                _state.update(api_ok_at=now, api_seconds=max(0.0, _clock() - started))
            except Exception:
                pass
    finally:
        _lock.release()


def _refresh_data(now):
    """The /data write, if due and nobody else is running it. Its own lock: a
    write that hangs holds only this one, so the API check keeps running (#1323)."""
    if not _data_lock.acquire(blocking=False):
        return
    try:
        every = DATA_EVERY if _state["data_ok"] else DATA_RETRY
        if _due(_state["data_checked"], every, now):
            _state.update(data_checked=now, data_started=now)
            try:
                _write_probe()
                _state.update(data_ok=True, data_held=False)
            except (FENCE.DataUnavailable, OSError):
                _state.update(data_ok=False, data_held=False)
            except Held:
                _state.update(data_ok=False, data_held=True)
            finally:
                _state["data_started"] = None
    finally:
        _data_lock.release()


def _refresh(now, probe_data, probe_api=True):
    """Run whichever check is due, unless another caller is running it."""
    if probe_api:
        _refresh_api(now)
    if probe_data:
        _refresh_data(now)


def state(probe_data=True, probe_api=True):
    """The answer and the detail behind it: each check (data_writable None
    until /readyz has probed it), and how long the API last took to answer
    (seconds, None before it ever has). probe_data=False checks the API
    only and reports the last /data result: what /metrics asks. probe_api=False
    checks the volume only, and data_ready is that answer: what the probe asks."""
    now = _clock()
    _refresh(now, probe_data, probe_api)
    api = _state["api_ok_at"] is not None and now - _state["api_ok_at"] <= API_FRESH
    started = _state["data_started"]
    stuck = started is not None and now - started > DATA_STUCK
    data, held = (False, False) if stuck else (_state["data_ok"], _state["data_held"])
    return {"ready": api and data is True, "data_ready": data is True or held is True,
            "checks": {"kube_api": api, "data_writable": data},
            "kube_api_seconds": _state["api_seconds"]}


def answer(request=None):
    if request is not None and (request.query.get("check") or [""])[0] == "data":
        ready = state(probe_api=False)["data_ready"]
    else:
        ready = state()["ready"]
    return MODULE_ROUTES.Reply(200 if ready else 503, {"ready": ready})


# Public (homestead_web_paths.PUBLIC): the role is never asked for.
ROUTES = {("GET", "/readyz"): ("viewer", answer)}
