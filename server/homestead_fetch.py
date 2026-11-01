"""Outbound answers read with a size cap and one deadline over the whole body.

urllib's timeout is per wait on the socket, so an answer that trickles in a
byte at a time is never timed out, and response.read() keeps whatever size the
far end sends. chunks() reads in pieces, refuses more than `limit` bytes
(whatever Content-Length says), and stops at `deadline` (time.monotonic()):
each wait on the socket is cut to the time left, as homestead_icons._fetch does.
"""
import time

CHUNK = 1 << 16


class TooLarge(ValueError):
    pass


def _socket(response):
    """The socket under a urllib/http.client answer, or None (a test's fake)."""
    raw = getattr(getattr(response, "fp", None), "raw", None)
    return getattr(raw, "_sock", None)


def chunks(response, limit, deadline=None, what="the answer"):
    """Yield the body in pieces of at most CHUNK bytes."""
    try:
        declared = int((getattr(response, "headers", None) or {}).get("Content-Length") or 0)
    except (TypeError, ValueError):
        declared = 0
    if declared > limit:
        raise TooLarge(f"{what} is larger than {limit} bytes")
    sock = _socket(response) if deadline is not None else None
    patience = sock.gettimeout() if sock is not None else None
    read = getattr(response, "read1", None) or response.read
    # http.client closes the answer, and with it the socket, once the whole
    # body is in: a timeout set on it then fails with EBADF.
    closed = getattr(response, "isclosed", None) or (lambda: False)
    total = 0
    while True:
        if closed():
            return
        if deadline is not None:
            left = deadline - time.monotonic()
            if left <= 0:
                raise TimeoutError(f"{what} took too long")
            if sock is not None:
                sock.settimeout(left if patience is None else min(patience, left))
        piece = read(min(CHUNK, limit + 1 - total))
        if not piece:
            return
        total += len(piece)
        if total > limit:
            raise TooLarge(f"{what} is larger than {limit} bytes")
        yield piece


def read(response, limit, deadline=None, what="the answer"):
    """The whole body as bytes, under the same cap and deadline."""
    return b"".join(chunks(response, limit, deadline, what))
