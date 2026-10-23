"""One audit line per change request (#738).

What a person did through Homestead was spread over the change history, the
agent audit, the sign-in history and an annotation that keeps only the last
writer, and none of it reached the pod log. This writes one JSON line for every
request that changes something: who, how they came in, what, the target, the
status and how long it took - after the answer, wherever it was answered.

The line is built from what homestead_routes.after_request hands a hook: no
body but the named fields ns, namespace, kind and name (checked and redacted
there), no query, no cookies, no secret header and never the answer. So no
password, setup code, key or cookie can reach it. What that view cannot know -
the linked cluster a request was relayed to, an AI agent's tool - is noted
during the request with note(), and taken by the hook.
"""
import collections
import collections.abc
import datetime
import ipaddress
import json
import os
import re
import sys
import threading
import time

import homestead_routes as MODULE_ROUTES
import homestead_shared as SHARED

try:
    import fcntl
except ImportError:
    fcntl = None

CHANGE = ("POST", "PUT", "PATCH", "DELETE")
# Opening a shell or a console is what people ask "who did that" about; one
# line when the session ends, with its length.
CONSOLES = ("/api/console", "/api/node/shell", "/api/vm/console")
MCP_PATH = "/mcp"
NOT_AUDITED = {
    ("POST", "/api/auth/login"): "the sign-in history records it, and a failed sign-in has no user",
    ("POST", "/api/auth/preferences/dashboard"): "a person's own dashboard layout, not the cluster",
    ("POST", "/api/timeline/seen"): "a person's own 'last here' marker, not the cluster",
}
# What a request may note for its line; the MCP tool's class decides whether
# a call to /mcp is a change, and is not written itself.
NOTE_KEYS = ("to", "tool", "kind", "held", "outcome")
CHANGE_TOOLS = ("change", "destructive")
NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9.-]{0,252}")
NOTE_VALUE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,252}")     # tool names have underscores
EMAIL = re.compile(r"[^\s@]{1,64}@[^\s@]{1,189}")
DELETE_WORKLOAD = re.compile(r"/api/workload/([a-z0-9.-]+)/([a-z0-9.-]+)")
PATH_CAP = 200
# A flood of refused requests must not wash real changes out of the two files
# on the data volume. stdout carries every line. The file keeps every success
# and every real server error; it throttles refusals: 4xx, a 503 with no user
# (a full server shedding load, or a data move), and MCP tool calls refused.
# Each WINDOW_SECONDS it keeps:
# - the first refusal from each user (signed-in) or source (no user: the
#   address, an IPv6 address by its /64), at most FIRST_CAP of those firsts;
# - then at most BURST more from that user or source;
# - and at most ALL_BURST in all, one ceiling for refusals with no user and
#   one for signed-in people's.
# What it leaves out is summed up in one line. The table is bounded (least
# recently used goes first, its summary written), never cleared.
WINDOW_SECONDS = 60
BURST = 5
ALL_BURST = 120
FIRST_CAP = 50
KEYS_KEPT = 1000
SWEEP_SECONDS = 5
# Two rotations closer than this say something is filling the file.
ROTATION_ALARM_SECONDS = 3600
# The bell alert for it stays up this long after the fast fill ended.
ROTATION_ALERT_SECONDS = 86400
# On the data volume as well, so the record survives a restart without a log
# collector: two files of at most MAX_BYTES, ~60 000 lines.
FILE = "audit.jsonl"
LOCK = "audit.lock"
MAX_BYTES = 10 * 1024 * 1024
_buckets = collections.OrderedDict()   # ("addr"|"user"|"all", name) -> window
_swept = [0.0]
_rotated = []
_throttle = threading.Lock()
_drops_seen = [0]
_file_dropped = [0]      # lines that could not be written whole to audit.jsonl
_lock = threading.Lock()


def audited(method, path):
    """Whether a request to path with method gets a line."""
    if (method, path) in NOT_AUDITED:
        return False
    if method == "GET":
        return path in CONSOLES
    return method in CHANGE and (path.startswith("/api/") or path == MCP_PATH)


def note(**fields):
    """Something this request's line should say that the hook cannot see:
    homestead_routes.note() for each, kept on the request's thread and handed
    to the hook. A value that is not a short name is left out, never raised:
    noting never breaks the request."""
    unknown = sorted(set(fields) - set(NOTE_KEYS))
    if unknown:
        raise ValueError(f"an audit note may say only {', '.join(NOTE_KEYS)}, not {', '.join(unknown)}")
    for key, value in fields.items():
        try:
            MODULE_ROUTES.note(key, value)
        except ValueError:
            pass


def _name(value):
    return value if isinstance(value, str) and NAME.fullmatch(value) else None


def _target(request):
    body = request.body if isinstance(request.body, dict) else {}
    found = {"ns": _name(body.get("ns")) or _name(body.get("namespace")),
             "kind": _name(body.get("kind")), "name": _name(body.get("name"))}
    match = request.method == "DELETE" and DELETE_WORKLOAD.fullmatch(request.path)
    if match:
        found.update(ns=match.group(1), name=match.group(2))
    return {key: value for key, value in found.items() if value}


def _via(request, conn):
    if conn is not None and conn.key:
        return "mcp" if request.path == MCP_PATH else "key"
    if conn is not None and conn.fleet:
        return "linked"
    return "browser" if request.user else "none"


def _now_text(seconds):
    at = datetime.datetime.fromtimestamp(seconds, datetime.timezone.utc)
    return at.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def line(request, status, ms, notes, now=None):
    """The line for one finished request, or None when it gets none."""
    if request.path == MCP_PATH and notes.get("kind") not in CHANGE_TOOLS:
        return None             # initialize, tools/list, ping and read tools are reads
    conn = request.conn
    entry = {"v": 1, "at": _now_text(time.time() if now is None else now),
             "replica": os.environ.get("HOSTNAME", ""), "via": _via(request, conn),
             "user": request.user or "", "role": request.role, "addr": request.addr}
    email = conn.access.get("email") if conn is not None and isinstance(conn.access, collections.abc.Mapping) else None
    if isinstance(email, str) and len(email) <= 254 and EMAIL.fullmatch(email):
        entry["access"] = email
    if conn is not None and conn.key:
        entry["key"], entry["owner"] = conn.key.get("id"), conn.key.get("owner")
    if conn is not None and isinstance(conn.fleet, str) and NOTE_VALUE.fullmatch(conn.fleet):
        entry["from"] = conn.fleet
    for key in ("to", "tool", "held", "outcome"):
        if isinstance(notes.get(key), str) and NOTE_VALUE.fullmatch(notes[key]):
            entry[key] = notes[key]
    entry.update(method=request.method, path=request.path[:PATH_CAP])
    target = _target(request)
    if target:
        entry["target"] = target
    entry.update(status=status, ms=ms)
    if request.method == "GET" and request.path in CONSOLES:
        entry["session_s"] = round(ms / 1000)
    return entry


def _refusal(entry):
    status = entry["status"]
    if entry.get("outcome") == "refused":
        return True             # an MCP tool call refused before it ran: the HTTP answer is 200
    if entry.get("outcome") == "error":
        return False            # a change tool failed while it acted: kept, as a server error is
    return isinstance(status, int) and (400 <= status < 500 or status == 503 and not entry["user"])


def _source(addr):
    """Where refusals with no user come from: the address, or its /64 for
    IPv6, where one client holds more addresses than it can use."""
    try:
        ip = ipaddress.ip_address(addr)
    except ValueError:
        return addr
    return str(ipaddress.ip_network(f"{ip}/64", strict=False)) if ip.version == 6 else str(ip)


def _summary(key, window):
    kind, name = key
    entry = {"v": 1, "at": _now_text(time.time()), "replica": os.environ.get("HOSTNAME", ""),
             "event": "audit-suppressed", "suppressed": window["dropped"], "window_s": WINDOW_SECONDS,
             "since": _now_text(window["start"])}
    if kind == "user":
        entry["user"] = name
        who = f"by {name}"
    elif kind == "addr":
        entry["addr"] = name
        who = f"from {name}"
    elif name == "firsts":
        entry["addr"] = "*"
        who = "that were the first from a new address or user"
    elif name == "signed-in":
        entry["user"] = "*"
        who = "by signed-in people"
    else:
        entry["addr"] = "*"
        who = "with no user, from any address"
    entry["detail"] = (f"{window['dropped']} more refused requests {who} "
                       f"in the {WINDOW_SECONDS} s since {entry['since']}: on stdout, not kept here")
    return entry


def _window(key, now, summaries):
    window = _buckets.get(key)
    if window is not None and now - window["start"] >= WINDOW_SECONDS:
        if window["dropped"]:
            summaries.append(_summary(key, window))
        window = None
    if window is None:
        window = _buckets[key] = {"start": now, "kept": 0, "dropped": 0}
    _buckets.move_to_end(key)
    return window


def _keep(entry):
    """Whether the data volume keeps this line, and the summaries due first."""
    now, summaries = time.time(), []
    with _throttle:
        if now - _swept[0] >= SWEEP_SECONDS:
            _swept[0] = now
            for key in [k for k, w in _buckets.items() if now - w["start"] >= WINDOW_SECONDS]:
                window = _buckets.pop(key)
                if window["dropped"]:
                    summaries.append(_summary(key, window))
        if not _refusal(entry):
            return True, summaries
        user = entry["user"]
        mine = _window(("user", user) if user else ("addr", _source(entry["addr"])), now, summaries)
        everyone = _window(("all", "signed-in" if user else "anonymous"), now, summaries)
        firsts = _window(("all", "firsts"), now, summaries)
        while len(_buckets) > KEYS_KEPT:
            key, window = _buckets.popitem(last=False)
            if window["dropped"]:
                summaries.append(_summary(key, window))
        if mine["kept"] == 0:
            if firsts["kept"] >= FIRST_CAP:
                firsts["dropped"] += 1
                return False, summaries
            firsts["kept"] += 1
        elif mine["kept"] >= BURST:
            mine["dropped"] += 1
            return False, summaries
        elif everyone["kept"] >= ALL_BURST:
            everyone["dropped"] += 1
            return False, summaries
        mine["kept"] += 1
        everyone["kept"] += 1
        return True, summaries


def _report_drops():
    """Requests whose hooks homestead_routes dropped because its queue was
    full: one line saying how many, before the next one written."""
    dropped = MODULE_ROUTES.hooks_dropped()
    if dropped > _drops_seen[0]:
        missed, _drops_seen[0] = dropped - _drops_seen[0], dropped
        emit({"v": 1, "at": _now_text(time.time()), "replica": os.environ.get("HOSTNAME", ""),
              "event": "audit-lines-dropped", "dropped": missed,
              "detail": f"{missed} request(s) were answered while the audit could not keep up, so their "
                        "lines (if they changed something) are missing here and from the pod log"})


def record(request, status, ms, notes=None):
    """The hook, on homestead_routes' hooks thread: one line for a finished
    change request. notes is what the request noted on its own thread. Never
    raises."""
    try:
        notes = dict(notes or {})
        _report_drops()
        if not audited(request.method, request.path):
            return
        entry = line(request, status, ms, notes)
        if entry is None:
            return
        keep, summaries = _keep(entry)
        for summary in summaries:
            if _file_on():
                _append(text(summary))
        emit(entry, keep)
    except Exception:
        pass        # recording never changes or breaks an answer


def text(entry):
    """One line of JSON, whatever a path or a name holds."""
    return json.dumps(entry, ensure_ascii=True, separators=(",", ":"))


def _stdout(line_text):
    with _lock:
        sys.stdout.write(line_text + "\n")
        sys.stdout.flush()


SETTING_SECONDS = 30
_setting = {"value": True, "read": None}       # on until a read says otherwise


def _file_on():
    """The audit.file setting, read at most every SETTING_SECONDS. A read that
    fails keeps the last answer: the cluster may be restarting."""
    now = time.monotonic()
    if _setting["read"] is None or now - _setting["read"] >= SETTING_SECONDS:
        _setting["read"] = now
        try:
            import homestead_settings_helpers as SETTINGS
            _setting["value"] = SETTINGS.get_app_settings()["audit"]["file"] is not False
        except Exception:
            pass
    return _setting["value"]


def _rotate(path, incoming):
    """audit.jsonl becomes audit.jsonl.1 (replacing it) when the next line would
    not fit. Under audit.lock, and checked again there, so two replicas that
    both saw a full file rotate it once."""
    fd = os.open(os.path.join(os.path.dirname(path), LOCK),
                 os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        if fcntl is not None:
            fcntl.flock(fd, fcntl.LOCK_EX)
        try:
            size = os.stat(path).st_size
        except FileNotFoundError:
            return
        if size + incoming <= MAX_BYTES:
            return
        os.replace(path, path + ".1")
    finally:
        os.close(fd)
    now = time.time()
    if _rotated and now - _rotated[-1] < ROTATION_ALARM_SECONDS:
        _stdout(text({"v": 1, "at": _now_text(now), "replica": os.environ.get("HOSTNAME", ""),
                      "event": "audit-file-rotated-quickly",
                      "detail": f"audit.jsonl filled {MAX_BYTES // (1024 * 1024)} MiB twice within an hour, so "
                                "something may be flooding Homestead with requests; the pod log keeps every line"}))
    _rotated[:] = [now]


def _write_whole(fd, data):
    """One os.write of data with O_APPEND. Nothing is ever cut back: another
    replica may have appended since. A write that fails writes nothing and is
    counted; the next line that fits says how many were lost. A short write
    (a full disk) is ended with a newline and a marker line, so a reader can
    skip the broken line before it."""
    if _file_dropped[0]:
        data = (text({"v": 1, "at": _now_text(time.time()), "replica": os.environ.get("HOSTNAME", ""),
                      "event": "audit-file-lines-dropped", "dropped": _file_dropped[0],
                      "detail": "lines that could not be written here (the volume was full?); "
                                "the pod log has them"}) + "\n").encode("ascii") + data
    try:
        written = os.write(fd, data)
    except OSError:
        _file_dropped[0] += 1
        return
    _file_dropped[0] = 0
    if written < len(data):
        try:
            os.write(fd, ("\n" + text({"v": 1, "at": _now_text(time.time()),
                                        "replica": os.environ.get("HOSTNAME", ""),
                                        "event": "audit-line-truncated",
                                        "detail": "the line before this one was cut short (the volume was full?); "
                                                  "the pod log has it"}) + "\n").encode("ascii"))
        except OSError:
            pass


def _append(line_text):
    """One line onto the data volume, only through the self-data write fence:
    while a data move holds /data nothing is written here, and nothing raises."""
    folder = SHARED.DIR
    if not os.path.isdir(folder):
        return                      # no data volume: never made here
    path, data = os.path.join(folder, FILE), (line_text + "\n").encode("ascii")
    try:
        with SHARED.write_scope(path):
            if SHARED.WRITE_GUARD is not None:
                SHARED.WRITE_GUARD(path)
            try:
                size = os.stat(path).st_size
            except FileNotFoundError:
                size = 0
            if size + len(data) > MAX_BYTES:
                _rotate(path, len(data))
            fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
            try:
                _write_whole(fd, data)
            finally:
                os.close(fd)
    except Exception:
        pass                        # held, full or unwritable: stdout has the line


def _at(line):
    try:
        at = json.loads(line)["at"]
        return datetime.datetime.fromisoformat(at.replace("Z", "+00:00")).timestamp()
    except (ValueError, KeyError, TypeError, AttributeError):
        return None


def _first_and_last(path):
    """When audit.jsonl.1's first and last readable lines were written."""
    with open(path, "rb") as handle:
        first = next((t for t in (_at(handle.readline()) for _ in range(5)) if t is not None), None)
        handle.seek(max(0, os.fstat(handle.fileno()).st_size - 65536))
        tail = handle.read().splitlines()
    last = next((t for t in (_at(line) for line in reversed(tail[-50:])) if t is not None), None)
    return first, last


def alert_facts(now=None):
    """The bell alert (homestead_alerts, via homestead_overview_server): raised
    while audit.jsonl.1 was filled in under an hour, ending within the last
    day. Read from the files, so whichever copy of Homestead leads the alerts
    sees what any copy wrote."""
    now = time.time() if now is None else now
    try:
        first, last = _first_and_last(os.path.join(SHARED.DIR, FILE + ".1"))
    except OSError:
        return []
    if first is None or last is None or last - first >= ROTATION_ALARM_SECONDS or now - last > ROTATION_ALERT_SECONDS:
        return []
    minutes = max(1, round((last - first) / 60))
    return [{"key": "audit:filling-fast", "category": "degraded", "severity": "degraded",
             "title": "The audit log is filling fast",
             "body": (f"audit.jsonl filled {MAX_BYTES // (1024 * 1024)} MiB in {minutes} minutes: something may be "
                      "flooding Homestead with requests, and older lines on the data volume are being replaced. "
                      "The pod log keeps every line."),
             "resolved": "The audit log fills at its usual pace again",
             "href": "/settings?tab=about"}]


def emit(entry, keep=True):
    """Write a line: always to stdout, for a log collector, and to the data
    volume unless the throttle leaves it out (keep False)."""
    line_text = text(entry)
    _stdout(line_text)
    if keep and _file_on():
        _append(line_text)


MODULE_ROUTES.after(record, fields=MODULE_ROUTES.HOOK_FIELDS, notes=NOTE_KEYS)
