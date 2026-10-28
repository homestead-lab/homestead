"""The audit log, in one list (#738, part 2).

Two records say what was done through Homestead: audit.jsonl, one line per
change request (homestead_audit), and agent-audit.jsonl, one line per call an
API key makes (homestead_agent_audit). They stay two files - the agent audit
keeps redacted tool arguments, the change log deliberately keeps none, and
they are kept for different times (#817). An admin reads them here together,
newest first, each row saying which log it came from, who, what and how it
ended. Arguments are not copied in: AI agent requests (under Settings) shows
them beside the keys.

Reading stays cheap however large the files are: each is read backwards from
its end, a chunk at a time, and reading stops once the page is full, or after
SCAN lines of each log when a filter matches little (the answer then says it
is partial). Nothing here writes.
"""
import datetime
import heapq
import itertools
import json
import math
import os

import homestead_agent_audit as AGENT
import homestead_audit as CHANGES
import homestead_shared as SHARED

SOURCES = ("changes", "agents")
LIMIT = 100
MAX_LIMIT = 500
MAX_OFFSET = 10000
SCAN = 20000            # lines looked at in each log, at most, for one page
CHUNK = 65536
AGENT_RESULTS = ("ok", "refused", "error", "pending", "approved", "denied")


def _backwards(path, budget):
    """The lines of path, last first, as bytes. budget["left"] counts down each
    line looked at; at zero reading stops, and budget["stopped"] says a line
    was left unread. A missing file has none."""
    try:
        handle = open(path, "rb")
    except OSError:
        return
    with handle:
        end = handle.seek(0, os.SEEK_END)
        tail = b""
        while end > 0:
            start = max(0, end - CHUNK)
            handle.seek(start)
            block = handle.read(end - start) + tail
            end = start
            lines = block.split(b"\n")
            tail = lines.pop(0) if end > 0 else b""
            for line in reversed(lines):
                if not line.strip():
                    continue
                if budget["left"] <= 0:
                    budget["stopped"] = True
                    return
                budget["left"] -= 1
                yield line


def _parse(line):
    try:
        row = json.loads(line)
    except ValueError:
        return None
    return row if isinstance(row, dict) else None


def _seconds(at):
    if isinstance(at, bool):
        return None
    if isinstance(at, (int, float)):
        return float(at) if math.isfinite(at) else None     # NaN would make the answer invalid JSON
    if isinstance(at, str):
        try:
            return datetime.datetime.fromisoformat(at.replace("Z", "+00:00")).timestamp()
        except ValueError:
            return None
    return None


def _text(value, cap=200):
    return value[:cap] if isinstance(value, str) else ""


def _change_result(row):
    outcome, status = row.get("outcome"), row.get("status")
    if outcome in ("refused", "error"):
        return outcome
    if not isinstance(status, int) or isinstance(status, bool):
        return "unknown"
    if status < 400:
        return "ok"
    if status < 500 or status == 503 and not row.get("user"):
        return "refused"            # part 1's rule: a full server shedding load refuses
    return "error"


def _change_row(row):
    at = _seconds(row.get("at"))
    if at is None:
        return None
    out = {"source": "changes", "at": at, "who": _text(row.get("user") or row.get("owner"), 64),
           "via": _text(row.get("via"), 16), "addr": _text(row.get("addr"), 64)}
    if row.get("event"):
        out.update(what=_text(row["event"], 64), result="note", detail=_text(row.get("detail"), 300))
        return out
    target = row.get("target") if isinstance(row.get("target"), dict) else {}
    out.update(what=_text(row.get("tool"), 120) or f"{_text(row.get('method'), 8)} {_text(row.get('path'))}".strip(),
               target="/".join(_text(target.get(k), 253) for k in ("ns", "kind", "name") if target.get(k)),
               result=_change_result(row), code=row.get("status") if isinstance(row.get("status"), int) else None)
    for key in ("to", "from", "access"):
        if isinstance(row.get(key), str):
            out[key] = _text(row[key], 254)
    return out


def _agent_row(row):
    at = _seconds(row.get("at"))
    if at is None:
        return None
    outcome = row.get("outcome")
    return {"source": "agents", "at": at, "who": _text(row.get("owner"), 64), "via": _text(row.get("via"), 16),
            "addr": _text(row.get("addr"), 64), "what": _text(row.get("action"), 120),
            "key": _text(row.get("name"), 80), "result": outcome if outcome in AGENT_RESULTS else "unknown",
            "detail": _text(row.get("detail"), 300)}


def _rows(paths, shape, budget):
    for path in paths:
        for line in _backwards(path, budget):
            row = _parse(line)
            shaped = shape(row) if row is not None else None
            if shaped is not None:
                yield shaped


def _number(query, name, default, low, high):
    raw = (query.get(name) or [""])[0]
    if raw in ("", None):
        return default
    try:
        value = int(raw)
    except (TypeError, ValueError):
        raise ValueError(f"{name} must be a whole number") from None
    return max(low, min(high, value))


def log(query):
    """One page of both logs, newest first: {rows, offset, limit, more, partial}."""
    source = (query.get("source") or [""])[0] or ""
    if source and source not in SOURCES:
        raise ValueError(f"source must be one of {', '.join(SOURCES)}")
    person = (query.get("person") or [""])[0].strip().lower()
    limit = _number(query, "limit", LIMIT, 1, MAX_LIMIT)
    offset = _number(query, "offset", 0, 0, MAX_OFFSET)
    budgets = {name: {"left": SCAN, "stopped": False} for name in SOURCES}
    streams = []
    if source in ("", "changes"):
        current = os.path.join(SHARED.DIR, CHANGES.FILE)
        streams.append(_rows((current, current + ".1"), _change_row, budgets["changes"]))
    if source in ("", "agents"):
        streams.append(_rows((os.path.join(AGENT.DATA_DIR, AGENT.FILE),), _agent_row, budgets["agents"]))
    merged = heapq.merge(*streams, key=lambda row: -row["at"])
    matching = (row for row in _until_stopped(merged, budgets.values()) if not person or row["who"].lower() == person)
    page = list(itertools.islice(matching, offset, offset + limit + 1))
    stopped = any(budget["stopped"] for budget in budgets.values())
    return {"rows": page[:limit], "offset": offset, "limit": limit,
            "more": len(page) > limit and offset + limit <= MAX_OFFSET, "partial": stopped and len(page) <= limit}


def _until_stopped(merged, budgets):
    """The merged rows until one log's search stops at SCAN: the rows it left
    unread are older than its last, so a row of the other log past that point
    could stand in front of them. The page ends there instead."""
    for row in merged:
        if any(budget["stopped"] for budget in budgets):
            return
        yield row


ROUTES = {
    ("GET", "/api/audit/log"): ("admin", lambda request: log(request.query or {})),
}
