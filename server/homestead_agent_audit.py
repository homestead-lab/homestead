"""What API keys did: every call a key makes, on /api/v1 and on /mcp (#417).

A key acts without a person watching - Home Assistant on a schedule, an AI
agent on its own judgement - so each call is written down: when, which key
and who made it, what it asked for, how it ended, and from where. The
newest are shown in Settings beside the keys, where one can be revoked.

Kept as JSON lines on the data volume, for KEEP_DAYS and at most MAX_LINES;
every copy of Homestead appends and trims it under one shared lock, so two
copies on one volume lose no line (#986). Anything that looks like a password or token in what was asked is written
as "(hidden)", and long text is cut short.
"""
import json
import hashlib
import os
import re
import time

import homestead_shared as SHARED

DATA_DIR = "/data"
FILE = "agent-audit.jsonl"
KEEP_DAYS = 30
MAX_LINES = 20000
SHOW = 200
TRIM_EVERY = 500         # records written by this process between trims
SECRETISH = re.compile(r"pass|secret|token|key|credential|auth|cookie|private", re.I)
_lock = SHARED.SharedLock("agent-audit", directory=lambda: DATA_DIR)
_written = [0]


def bind(data_dir):
    global DATA_DIR
    DATA_DIR = data_dir


def _path():
    return os.path.join(DATA_DIR, FILE)


def redact(value, depth=0):
    """What was asked, safe to keep: secret-looking fields hidden, every
    environment variable's value hidden (apps keep passwords there), long
    text cut, deep nesting summarised."""
    if depth > 6:
        return "(nested)"
    if isinstance(value, dict):
        out = {}
        for key, item in list(value.items())[:60]:
            key = str(key)[:80]
            if SECRETISH.search(key) and not isinstance(item, (dict, list, bool)) and item not in (None, ""):
                out[key] = "(hidden)"
            elif key in ("env", "environment") and isinstance(item, dict):
                out[key] = {str(k)[:80]: "(hidden)" for k in list(item)[:60]}
            elif key == "argv" and isinstance(item, list):
                # A shell script can carry credentials without named fields.
                # The exact arguments remain in the administrator's review.
                out[key] = {"sha256": hashlib.sha256(json.dumps(item).encode()).hexdigest(), "arguments": len(item)}
            else:
                out[key] = redact(item, depth + 1)
        return out
    if isinstance(value, list):
        return [redact(item, depth + 1) for item in value[:40]]
    if isinstance(value, str):
        return value if len(value) <= 300 else value[:300] + "…"
    return value


def record(key, via, action, args=None, outcome="ok", detail="", addr="", approved_by=""):
    """One call: key is the verified key ({id, name, owner}); via "api" or
    "mcp"; outcome ok, refused, error, pending, approved or denied."""
    entry = {"at": int(time.time()), "key": (key or {}).get("id", ""), "name": (key or {}).get("name", ""),
             "owner": (key or {}).get("owner", ""), "via": via, "action": str(action)[:120],
             "args": redact(args or {}), "outcome": outcome, "detail": str(detail or "")[:300],
             "addr": str(addr or "")[:64]}
    if approved_by:
        entry["approved_by"] = approved_by
    line = json.dumps(entry, separators=(",", ":"), ensure_ascii=False)
    try:
        with _lock:
            os.makedirs(DATA_DIR, exist_ok=True)
            with open(_path(), "a", encoding="utf-8") as handle:
                handle.write(line + "\n")
            _written[0] += 1
            if _written[0] % TRIM_EVERY == 0:
                _trim_locked()
    except Exception as error:      # full, unwritable, or held by a data move
        # The pod log keeps the line the data volume did not take.
        print(f"agent audit: not written to the data volume ({str(error)[:160]}): {line}", flush=True)
    return entry


def _unreadable(error):
    return OSError(error.errno, f"Homestead could not read the AI agents' audit log on its data volume "
                                f"({error.strerror or error})")


def _read():
    """Every row. No log yet is none; one that cannot be read raises, so the
    daily trim does not save an empty log over it."""
    try:
        with open(_path(), encoding="utf-8") as handle:
            lines = handle.readlines()
    except FileNotFoundError:
        return []
    except OSError as error:
        raise _unreadable(error) from error
    out = []
    for line in lines:
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
    return out


def _trim_locked(now=None):
    """Keep KEEP_DAYS, and at most MAX_LINES of them."""
    now = now or time.time()
    rows = [r for r in _read() if r.get("at", 0) >= now - KEEP_DAYS * 86400][-MAX_LINES:]
    temp = SHARED.temporary(_path())
    try:
        with open(temp, "w", encoding="utf-8") as handle:
            handle.writelines(json.dumps(r, separators=(",", ":"), ensure_ascii=False) + "\n" for r in rows)
            handle.flush()
            os.fsync(handle.fileno())       # on disk before it replaces the log
        os.replace(temp, _path())
    except BaseException:
        try:
            os.remove(temp)
        except OSError:
            pass
        raise
    return len(rows)


def trim(now=None):
    """Daily, from housekeeping: drop what is older than KEEP_DAYS."""
    try:
        with _lock:
            return _trim_locked(now)
    except Exception:
        return 0


def _newest(keep, limit, chunk=1 << 16):
    """Up to limit rows that keep() accepts, newest first, read from the end of
    the file a chunk at a time, so the newest few never parse the whole log."""
    out = []
    try:
        handle = open(_path(), "rb")
    except FileNotFoundError:
        return out
    except OSError as error:
        raise _unreadable(error) from error     # not "no calls"
    with handle:
        end, tail = os.fstat(handle.fileno()).st_size, b""
        while end > 0 and len(out) < limit:
            start = max(0, end - chunk)
            handle.seek(start)
            lines = (handle.read(end - start) + tail).split(b"\n")
            end = start
            tail = lines.pop(0) if start else b""      # may be cut: read with the next chunk
            for raw in reversed(lines):
                try:
                    row = json.loads(raw)
                except ValueError:
                    continue
                if isinstance(row, dict) and keep(row):
                    out.append(row)
                    if len(out) >= limit:
                        break
    return out


def recent(key_id="", limit=SHOW):
    """The newest calls first, of one key or of all."""
    return _newest(lambda r: not key_id or r.get("key") == key_id, max(1, min(int(limit), 1000)))


ROUTES = {
    ("GET", "/api/agents/audit"): ("admin", lambda request: {
        "calls": recent((request.query.get("key") or [""])[0], (request.query.get("limit") or [SHOW])[0]),
        "keep_days": KEEP_DAYS}),
}
