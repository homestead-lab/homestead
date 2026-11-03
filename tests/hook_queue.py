"""A test class's own after-request hooks, apart from what earlier files queued.

    own_hook_queue(cls)    in setUpClass

server.H runs its after-request hooks (the audit log among them) on one worker per
queue. A class that waits for its hooks' output needs two things:
- No hook another file queued may still run while it waits. Such a hook writes to
  whatever sink the class has patched: the audit line of another file's request
  arrives in this class's list (test_audit_coverage got 311 lines for 300, the
  late ones test_api_v1_route's). So first wait until everything already queued
  has run: a marker on the current queue runs after all of it.
- Its own queue, so its own worker: a hook that is still slow (the audit's settings
  read waits 5 s for DNS on CI) cannot hold this class's lines up.
And audit.file as a failed read leaves it, on, without the read: the hook path
itself stays the real one (server.H queues, homestead_routes._run_hooks runs).
"""
import queue
import threading
from unittest import mock

DRAIN_SECONDS = 20     # longer than one stalled settings read and a full queue behind it


def drain(routes, seconds=DRAIN_SECONDS):
    """Wait until every hook queued so far on the current queue has run."""
    done = threading.Event()
    try:
        routes._queue.put([(lambda request, status, ms: done.set(), None, None, None)], timeout=seconds)
    except queue.Full:
        return False
    routes._ensure_worker()
    return done.wait(seconds)


def own_hook_queue(cls):
    import homestead_audit as AUDIT_LOG
    import homestead_routes as ROUTES
    drain(ROUTES)
    cls.enterClassContext(mock.patch.object(ROUTES, "_queue", queue.Queue(ROUTES.HOOK_QUEUE)))
    cls.enterClassContext(mock.patch.object(AUDIT_LOG, "_file_on", return_value=True))
