"""One deadline for all of a request's Kubernetes reads (#984).

Each read is allowed its own timeout (10 s for kget), and a cold page makes
dozens to hundreds of them in turn. A slow API server made such a page take
seconds and a hung one minutes, holding a connection slot all that time.

A GET request now has SECONDS for all its reads together (begin(), from
homestead_request_scope when the request starts; end() when it ends). Each
read is given what is left of it, or its own timeout when that is shorter;
none starts once it has run out, and a 429 is not retried past it. A read
refused or cut short that way is counted (cuts()), so a page whose
computation swallowed the error is not shown as complete: the route answers
504 (homestead_routes.call), or the cache serves its last complete copy
(homestead_cache).

The deadline is the request thread's own. Background loops, POST and DELETE
requests (whose writes and waits keep their own timeouts), the console and
shell connections and a download's stream have none, and a read on a thread
with none keeps its own timeout, as before."""
import threading
import time

# Longer than a cold page against a slow API server takes (6.7 s for the
# setup page at 30 ms a call, #984); about one UI refresh (15 s).
SECONDS = 15
clock = time.monotonic   # tests put a fake clock here
MESSAGE = ("The cluster's API server did not answer in time (it is slow or not answering), "
           "so this page could not be read in full. Try again in a moment.")
_local = threading.local()


class Spent(TimeoutError):
    """The request's time for Kubernetes reads ran out. A TimeoutError, so
    every caller that already handles a read's timeout handles this too."""


def begin(seconds=None):
    """This thread's request has seconds (default SECONDS) for its reads."""
    _local.until = clock() + (SECONDS if seconds is None else seconds)
    _local.cut = 0


def end():
    """No deadline on this thread (between requests; background work)."""
    _local.until = None
    _local.cut = 0


def lift():
    """No deadline from here on in this request (a download being streamed),
    keeping the count of reads already cut."""
    _local.until = None


def remaining():
    """Seconds left for this thread's reads, or None without a deadline."""
    until = getattr(_local, "until", None)
    return None if until is None else until - clock()


def cuts():
    """How many of this request's reads the deadline refused or cut short."""
    return getattr(_local, "cut", 0)


def forgive(count):
    """Back to count cuts: what was cut was answered with a complete copy."""
    _local.cut = count


def spent():
    """Count a read the deadline refused or cut short, and the error for it."""
    _local.cut = cuts() + 1
    return Spent(MESSAGE)


def timeout(wanted):
    """The timeout for one read that wants wanted seconds: wanted, or what is
    left of the request when that is less. Spent when nothing is left."""
    left = remaining()
    if left is None:
        return wanted
    if left <= 0:
        raise spent()
    return min(wanted, left)


def read(wanted, fn):
    """fn(timeout) for one read, given timeout(wanted). A timeout of a read
    that the deadline shortened is the deadline's, and counted."""
    allowed = timeout(wanted)
    try:
        return fn(allowed)
    except Spent:
        raise
    except TimeoutError as error:
        if allowed < wanted:
            raise spent() from error
        raise


def allows(seconds):
    """Whether a pause of seconds (a 429's Retry-After) ends before the deadline."""
    left = remaining()
    return left is None or seconds < left
