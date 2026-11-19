"""Agent requests: what an AI agent asked for that a person has to approve (#417).

A destructive tool (homestead_agent_tools) - a delete, a revert, a restore,
a storage move - or a change only an admin may make never runs on an
agent's say-so. It is planned, and the plan is filed here as a request: the
bell shows it and a push notification announces it (category "agents"),
and it opens on the Agents page with what the plan says will happen.

Anyone whose role allows the action may approve it or deny it - an admin
for what only admins may do in the app. Approving plans it again, as that
person, and runs it only when the new plan is the one that was reviewed
(digest): a volume replaced since, a snapshot gone, a capacity that no
longer fits, and it fails instead, saying so; the agent can ask again.

A request waits TTL at most, and a key may have MAX_PENDING waiting. They
are kept on the data volume, so every Homestead replica sees the same list,
and are dropped KEEP after they end.
"""
import os
import re
import secrets
import time

import homestead_agent_audit as AUDIT
import homestead_agent_tools as TOOLS
import homestead_auth as AUTH
import homestead_fleet as FLEET
import homestead_shared as SHARED

DATA_DIR = "/data"
FILE = "agent-requests.json"
TTL = 30 * 60
MAX_PENDING = 10
KEEP = 7 * 86400
# Every copy of Homestead reads and saves the same file, so they share the lock.
_lock = SHARED.SharedLock("agent-requests", directory=lambda: DATA_DIR)


def bind(data_dir):
    global DATA_DIR
    DATA_DIR = data_dir


def _path():
    return os.path.join(DATA_DIR, FILE)


def _load():
    return SHARED.read_json(_path(), {})


def _save(rows):
    SHARED.write_json(_path(), rows)


def _settle(rows, now):
    """Waiting past TTL is expired, and written down once; ended long enough
    ago is dropped. Saved when anything changed. Called under _lock."""
    changed = False
    for rid in list(rows):
        row = rows[rid]
        if row["status"] == "pending" and row["expires"] <= now:
            row.update(status="expired", ended=now, detail="Nobody approved it in time; the agent can ask again")
            AUDIT.record(row["key"], "mcp", row["tool"], row["args"], "expired", addr=row.get("addr", ""))
            changed = True
        if row["status"] not in ("pending", "running") and row.get("ended", now) + KEEP < now:
            rows.pop(rid)
            try:
                os.remove(os.path.join(DATA_DIR, f"agent-request-{rid}.decided"))
            except OSError:
                pass
            changed = True
    if changed:
        _save(rows)
    return rows


def create(tool, args, summary, role, key, reason="", addr="", escalated=False):
    """File a planned action for a person: the request, as the agent sees it."""
    now = time.time()
    with _lock:
        rows = _settle(_load(), now)
        waiting = [r for r in rows.values() if r["status"] == "pending" and r["key"].get("id") == key.get("id")]
        if len(waiting) >= MAX_PENDING:
            raise TOOLS.ToolError(f"{len(waiting)} requests from this key are already waiting for a person; "
                                  "wait for them to be answered")
        rid = secrets.token_hex(8)
        rows[rid] = {"id": rid, "tool": tool.name, "title": tool.title, "args": args, "summary": summary,
                     "digest": TOOLS.digest(summary), "role": role, "escalated": escalated,
                     "key": {k: key.get(k) for k in ("id", "name", "owner")}, "reason": str(reason or "")[:300],
                     "addr": str(addr or "")[:64], "created": now, "expires": now + TTL, "status": "pending"}
        _save(rows)
    return public(rows[rid])


def public(row, full=False):
    """A request without the plan's raw data - unless full, for the page a
    person reviews it on."""
    out = {k: row.get(k) for k in ("id", "tool", "title", "status", "reason", "created", "expires", "ended",
                                    "decided_by", "detail", "role", "escalated")}
    out["key"] = row["key"].get("name")
    for field in ("created", "expires", "ended"):
        if isinstance(row.get(field), (int, float)):
            out[f"{field}_at"] = TOOLS.when(row[field])
    out["action"] = (row.get("summary") or {}).get("action", row.get("title"))
    if full:
        out.update(summary=row.get("summary"), args=AUDIT.redact(row.get("args") or {}), owner=row["key"].get("owner"),
                   addr=row.get("addr"), result=row.get("result"))
    elif row.get("status") == "done":
        out["result"] = row.get("result")
    return out


def get(rid, key=None):
    """One request; for an agent, only one its own key filed."""
    with _lock:
        rows = _settle(_load(), time.time())
    row = rows.get(str(rid or ""))
    if not row or (key is not None and row["key"].get("id") != key.get("id")):
        raise TOOLS.ToolError("there is no such request for this key")
    return row


def pending_count():
    with _lock:
        return sum(1 for r in _settle(_load(), time.time()).values() if r["status"] == "pending")


def listing(role):
    """Waiting first, then the latest ended, for the bell and the page."""
    with _lock:
        rows = _settle(_load(), time.time())
    ordered = sorted(rows.values(), key=lambda r: (r["status"] != "pending", -r["created"]))
    def visible(row):
        details = public(row, full=True)
        # A reviewed command may read credentials that ordinary log viewers
        # cannot access. Only its requesting key and administrators get that
        # output or the exact arguments, not everyone viewing the requests page.
        if row.get("tool") == "run_container_command" and not AUTH.allows(role, "admin"):
            for field in ("summary", "args", "result"):
                details.pop(field, None)
        return {**details, "can_decide": AUTH.allows(role, row["role"])}
    return {"requests": [visible(r) for r in ordered[:100]],
            "pending": sum(1 for r in rows.values() if r["status"] == "pending")}


def _claim(rid):
    """The one decision on a request, across every copy of Homestead: a file
    made with O_EXCL on the shared data volume, which only one copy can make,
    and which stays there as the record that it was decided."""
    if not re.fullmatch(r"[0-9a-f]{16}", str(rid or "")):
        raise ValueError("there is no such request")
    path = os.path.join(DATA_DIR, f"agent-request-{rid}.decided")
    try:
        os.close(os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600))
    except FileExistsError:
        raise ValueError("someone else is deciding it, or has decided it") from None


def _take(rid, user, role, status):
    """The request, moved out of pending under the lock, so two people
    deciding at once cannot both run it, on this copy or another."""
    with _lock:
        rows = _settle(_load(), time.time())
        row = rows.get(str(rid or ""))
        if not row:
            raise ValueError("there is no such request")
        if row["status"] != "pending":
            raise ValueError(f"it is {row['status']} already")
        if not AUTH.allows(role, row["role"]):
            raise PermissionError(f"approving this needs the {row['role']} role")
        _claim(rid)
        rows = _load()      # read again: another copy may have written since
        row = rows.get(str(rid))
        if not row or row["status"] != "pending":
            raise ValueError(f"it is {(row or {}).get('status', 'gone')} already")
        row.update(status=status, decided_by=user, decided_at=time.time())
        _save(rows)
    return row


def _end(rid, **changes):
    with _lock:
        rows = _load()
        if rid in rows:
            rows[rid].update(ended=time.time(), **changes)
            _save(rows)
            return rows[rid]
    return changes


def deny(rid, user, role):
    row = _take(rid, user, role, "denied")
    AUDIT.record(row["key"], "mcp", row["tool"], row["args"], "denied", detail=f"denied by {FLEET.words(user)}",
                 addr=row.get("addr", ""), approved_by=user)
    return public(_end(rid, detail=f"Denied by {FLEET.words(user)}"))


def approve(rid, user, role):
    """Plan it again as user; run it only if that plan is the one reviewed."""
    row = _take(rid, user, role, "running")
    tool = TOOLS.BY_NAME.get(row["tool"])
    # In a form no account can have (a username cannot contain ':'). A key's
    # name may hold ':', written as the look-alike '\u2236' so the name ends at the next ':'.
    # summary: what the person was shown, for a run that must still match it (edit_container's stored values).
    ctx = {"user": f"approving:{str(row['key'].get('name')).replace(':', chr(0x2236))}:{user}", "role": role, "fresh": True,
           "auth": {"name": user, "kind": "session", "expires": None, "scopes": [s for s in ("read",)]},
           "summary": row.get("summary") or {}}
    try:
        if tool is None:
            raise TOOLS.ToolError("this Homestead no longer has that tool")
        if tool.kind == TOOLS.DESTRUCTIVE:
            summary, body = tool.plan(row["args"], ctx)
            if TOOLS.digest(summary) != row["digest"]:
                raise TOOLS.ToolError("what it would do has changed since it was asked for; nothing was done, "
                                      "and the agent can ask again")
            result = tool.execute(body, ctx)
        else:
            result = tool.run(row["args"], ctx)
    except (TOOLS.ToolError, TOOLS.NeedsApproval, ValueError, PermissionError, KeyError) as error:
        AUDIT.record(row["key"], "mcp", row["tool"], row["args"], "error", detail=str(error),
                     addr=row.get("addr", ""), approved_by=user)
        return public(_end(rid, status="failed", detail=str(error)[:400]))
    clipped, _ = TOOLS.clip(result)
    # Which stored values an approved edit deleted: names only, as the save answers them.
    deleted = result.get("secret_removed") if isinstance(result, dict) else None
    AUDIT.record(row["key"], "mcp", row["tool"], row["args"], "approved", addr=row.get("addr", ""), approved_by=user,
                 detail=f"approved by {FLEET.words(user)}" + (f"; deleted stored values: {', '.join(map(str, deleted))}"
                                                              if deleted else ""))
    return public(_end(rid, status="done", result=clipped, detail=f"Approved by {FLEET.words(user)}"))


def alert_facts():
    """One bell entry per waiting request; it clears when it is answered."""
    with _lock:
        rows = _settle(_load(), time.time())
    return [{"key": f"agents:{r['id']}", "category": "agents", "severity": "info",
             "title": f"An AI agent asks to {(r.get('summary') or {}).get('action', r['title']).lower()}"[:200],
             "body": (f"Key {r['key'].get('name')}" + (f": {r['reason']}" if r.get("reason") else "") +
                      ". Review it before it expires."),
             "resolved": "Answered", "href": f"/agents?request={r['id']}", "urgent": True}
            for r in rows.values() if r["status"] == "pending"]


ROUTES = {
    ("GET", "/api/agents/requests"): ("viewer", lambda request: listing(request.role)),
    ("POST", "/api/agents/requests/approve"): ("viewer", lambda request: approve(request.body.get("id"), request.user, request.role)),
    ("POST", "/api/agents/requests/deny"): ("viewer", lambda request: deny(request.body.get("id"), request.user, request.role)),
}
