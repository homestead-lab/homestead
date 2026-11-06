"""Outbound answers read with a size cap and one deadline over the whole body.

urllib's timeout is per wait on the socket, so an answer that trickles in a
byte at a time is never timed out, and response.read() keeps whatever size the
far end sends. chunks() reads in pieces, refuses more than `limit` bytes
(whatever Content-Length says), and stops at `deadline` (time.monotonic()):
each wait on the socket is cut to the time left, as homestead_icons._fetch does.

get() opens the request too, so a caller has one way to fetch: the cap and the
deadline above, and redirects that keep credentials on the host they were
meant for. A redirect to another origin (scheme, host or port: the rule curl
and browsers follow) still goes ahead, since release downloads move from
github.com to another host, but carries only the headers in REDIRECT_KEEPS;
anything else - an API key, a cookie, a signature - stays behind. A redirect
to anything but http or https is refused.

The deadline covers the body, not the name lookup: that is bounded by the
resolver (about 5 s in the Alpine image's musl), with no extra deadline here.
"""
import http.client
import io
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import NamedTuple

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
        try:
            piece = read(min(CHUNK, limit + 1 - total))
        except TimeoutError:
            # The wait was cut to the time left: say what took too long.
            if deadline is not None and time.monotonic() >= deadline - 0.05:
                raise TimeoutError(f"{what} took too long") from None
            raise
        if not piece:
            return
        total += len(piece)
        if total > limit:
            raise TooLarge(f"{what} is larger than {limit} bytes")
        yield piece


def read(response, limit, deadline=None, what="the answer"):
    """The whole body as bytes, under the same cap and deadline."""
    return b"".join(chunks(response, limit, deadline, what))


REDIRECT_KEEPS = {"accept", "user-agent"}
ERROR_MAX_BYTES = 64 << 10     # what is kept of an error answer's body, and the most a redirect's may be


class Answer(NamedTuple):
    body: bytes
    url: str          # where the answer came from, after any redirects
    headers: object


def _origin(url):
    parts = urllib.parse.urlsplit(url)
    return parts.scheme, (parts.hostname or "").lower(), parts.port or {"http": 80, "https": 443}.get(parts.scheme)


def _prefix(response, limit, deadline, what):
    """At most `limit` bytes of a body, whatever else comes or does not."""
    out, total = [], 0
    try:
        for piece in chunks(response, float("inf"), deadline, what):
            out.append(piece[:limit - total])
            total += len(out[-1])
            if total >= limit:
                break
    except (OSError, ValueError, http.client.HTTPException):
        pass
    return b"".join(out)


class _Redirects(urllib.request.HTTPRedirectHandler):
    """Redirects under the fetch's deadline, credentials kept on their origin.

    urllib reads a redirect's own body whole after this returns: it is read
    here first, under the cap and the deadline, so that read finds nothing."""

    def __init__(self, deadline, what):
        self.deadline, self.what = deadline, what

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        try:
            return self._follow(req, fp, code, msg, headers, newurl)
        except BaseException:
            # Given up on: close the answer now, not when it is collected.
            fp.close()
            raise

    def _follow(self, req, fp, code, msg, headers, newurl):
        read(fp, ERROR_MAX_BYTES, self.deadline, f"{self.what} (a redirect)")
        refuse = lambda why: urllib.error.HTTPError(newurl, code, why, headers, fp)
        try:
            before, after = _origin(req.full_url), _origin(newurl)
        except ValueError:
            raise refuse("a redirect to an address that is not valid") from None
        if after[0] not in ("http", "https"):
            raise refuse("a redirect to something other than http or https")
        if before[0] == "https" and after[0] == "http":
            raise refuse("a redirect from https to plain http")
        left = self.deadline - time.monotonic()
        if left <= 0:
            raise TimeoutError(f"{self.what} took too long")
        new = super().redirect_request(req, fp, code, msg, headers, newurl)
        if new is not None:
            # The next hop's waits are cut to the time left, as the body's are.
            req.timeout = min(req.timeout, left) if isinstance(req.timeout, (int, float)) else left
            if after != before:
                for name in [n for n in new.headers if n.lower() not in REDIRECT_KEEPS]:
                    del new.headers[name]
        return new


def get(url, *, limit, seconds, timeout=None, headers=None, data=None, method=None, context=None,
        what="the answer"):
    """Fetch url: the body (at most `limit` bytes, all of it within `seconds`),
    the address it came from and its headers. HTTP errors raise HTTPError, as
    urlopen does, carrying at most ERROR_MAX_BYTES of their body, read under the
    same deadline. `timeout` caps each wait on the socket (default `seconds`)."""
    deadline = time.monotonic() + seconds
    request = urllib.request.Request(url, data=data, method=method, headers=dict(headers or {}))
    opener = urllib.request.build_opener(urllib.request.HTTPSHandler(context=context), _Redirects(deadline, what))
    wait = min(timeout or seconds, seconds)
    try:
        with opener.open(request, timeout=wait) as response:
            body = read(response, limit, deadline, what)
            return Answer(body, response.geturl(), response.headers)
    except urllib.error.HTTPError as error:
        kept = _prefix(error.fp, ERROR_MAX_BYTES, deadline, what) if error.fp is not None else b""
        error.close()
        raise urllib.error.HTTPError(error.url, error.code, error.msg, error.hdrs, io.BytesIO(kept)) from None


def get_text(url, *, errors="strict", **options):
    return get(url, **options).body.decode("utf-8", errors)


def get_json(url, **options):
    return json.loads(get(url, **options).body)
