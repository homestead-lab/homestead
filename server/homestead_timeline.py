"""What happened while you were away (#748): one read-only timeline.

The story of a night was spread over four pages. This merges what Homestead
records already - app changes, why apps restarted (#740), how scheduled jobs
ran (#739), who did what (#738's audit log, admins only), alerts raised and
cleared, and host maintenance - into one list since the person was last here:
time, kind, app or host, one sentence and a link to where the detail lives.

It adds no record of its own but one: when each person was last here, in
timeline-seen.json on the data volume (not the accounts Secret, which a visit
every few minutes would keep rewriting). A row is only ever read for the person
it belongs to. A visit ends after VISIT_GAP without Homestead open, so a
refresh never empties the list.

Each source is read on its own. One that is missing or cannot be read is named
in the answer ("unread"), never left out as if nothing had happened (#620).
Entries carry names, times and Homestead's own words only: never a setting's
value, a run's message, a log line, an address or a key.
"""
import calendar
import os
import time

import json
import urllib.parse

import homestead_alerts as ALERTS
import homestead_audit as AUDIT
import homestead_changes as CHANGES
import homestead_history as HISTORY
import homestead_restarts as RESTARTS
import homestead_routes
import homestead_shared as SHARED

SEEN_FILE = "timeline-seen.json"
VISIT_GAP = 30 * 60          # this long without Homestead open ends a visit
FIRST_LOOK = 86400           # a first visit looks back one day
MAX_WINDOW = 30 * 86400      # the furthest back the timeline reaches
SEEN_USERS = 500
SEEN_KEEP = 90 * 86400
SEEN_QUIET = 60              # a mark this soon after the last is not written
MAX_ENTRIES = 300
AUDIT_READ = 2 * 1024 * 1024     # bytes of the audit files read, from their end
_seen_lock = SHARED.SharedLock("timeline-seen")


def _seen_path():
    return os.path.join(SHARED.DIR, SEEN_FILE)


def visit(row, now):
    """When this visit's "since you were last here" starts."""
    if not isinstance(row, dict) or not isinstance(row.get("last"), (int, float)):
        return int(now - FIRST_LOOK)
    if now - row["last"] > VISIT_GAP:
        return int(row["last"])
    since = row.get("since")
    return int(since) if isinstance(since, (int, float)) else int(row["last"])


def _users(data):
    users = data.get("users")
    return users if isinstance(users, dict) else {}


def last_here(user, now):
    """The start of the person's window. Raises SHARED.Unreadable."""
    if not user:
        return int(now - FIRST_LOOK)
    return visit(_users(SHARED.read_json(_seen_path(), {})).get(user), now)


def mark_seen(user, now):
    """Note that the person is here now; returns their window's start, or
    None without an account. Raises SHARED.Unreadable, or what a held write
    raises; the file is then left as it was."""
    if not user:
        return None

    def change(data):
        users = _users(data)
        row = users.get(user)
        since = visit(row, now)
        if isinstance(row, dict) and row.get("since") == since and now - (row.get("last") or 0) < SEEN_QUIET:
            raise _Unchanged(since)
        users[user] = {"since": since, "last": int(now)}
        for name in [n for n, r in users.items() if not isinstance(r, dict) or now - (r.get("last") or 0) > SEEN_KEEP]:
            del users[name]
        for name in sorted(users, key=lambda n: users[n].get("last") or 0)[:max(0, len(users) - SEEN_USERS)]:
            del users[name]
        data["users"] = users
        return since

    try:
        return SHARED.update_file(_seen_path(), change, _seen_lock, {}, separators=(",", ":"), sort_keys=True,
                                  mode=0o600)
    except _Unchanged as same:
        return same.since


class _Unchanged(Exception):
    def __init__(self, since):
        super().__init__("unchanged")
        self.since = since


# ---------------------------------------------------------------- the feed
def _at(text):
    try:
        return calendar.timegm(time.strptime(str(text), "%Y-%m-%dT%H:%M:%SZ"))
    except (TypeError, ValueError):
        return None


def _entry(at, kind, tone, text, href="", app="", host=""):
    return {"at": int(at), "kind": kind, "tone": tone, "app": app, "host": host, "text": text, "href": href}


def _app_href(name):
    return "/containers?find=" + urllib.parse.quote(name, safe="")


def _changes(since, now, role, unread):
    out = []
    for e in CHANGES.history(limit=None):
        if not isinstance(e.get("at"), (int, float)) or e["at"] <= since:
            continue
        # Field names only: a before or after value can be a setting someone keeps private.
        fields = ", ".join(str(r.get("field", "")) for r in e.get("changes") or [] if r.get("field"))
        who = (", outside Homestead" if e.get("source") == "outside"
               else ", by its power schedule" if e.get("by") == CHANGES.BY_SCHEDULE
               else f", by {e['by']}" if e.get("by") else "")
        out.append(_entry(e["at"], "change", "info", f"{e['name']} changed: {fields}{who}", _app_href(e["name"]),
                          app=f"{e['namespace']}/{e['name']}"))
    return out


RESTART_WORDS = {"restart": (" restarted", "warn"), "replaced": ("'s pod was replaced", "warn"),
                 "rollout": (" rolled out", "info")}


def _restarts(since, now, role, unread):
    out = []
    for r in RESTARTS.recent(since):
        at = _at(r.get("at"))
        words, tone = RESTART_WORDS.get(r.get("kind"), (" restarted", "warn"))
        if at is None:
            continue
        # The reason is Homestead's own words; never the detail (an event's
        # message) or the log tail, which are the app's.
        name = r.get("name", "")
        out.append(_entry(at, "restart", tone, f"{name}{words}: {r.get('reason') or 'reason not known'}", _app_href(name),
                          app=f"{r.get('ns', '')}/{name}"))
    return out


def _cron_rows():
    import homestead_scheduled_jobs as SCHEDULED
    return homestead_routes.cached("cron", 8, SCHEDULED.list_jobs)


def _schedules(since, now, role, unread):
    out, unknown = [], 0
    for row in _cron_rows():
        ns, name = row.get("ns", ""), row.get("name", "")
        href, app = f"/schedules?find={ns}/{name}", f"{ns}/{name}"
        at = _at(row.get("result_at"))
        if row.get("result") in ("ok", "failed") and at is not None and at > since:
            # Never the run's message: it can carry what the job was given.
            out.append(_entry(at, "schedule", "ok" if row["result"] == "ok" else "bad",
                              f"{name} ran" if row["result"] == "ok" else f"{name} failed", href, app=app))
        elif row.get("result") == "unknown":
            unknown += 1
        if row.get("missed"):
            # When it was due is not kept: stamped at its last run, or the
            # window's start, so it does not move to the top on each look.
            out.append(_entry(max(_at(row.get("last")) or since, since), "schedule", "warn", f"{name} did not run when it was due", href, app=app))
    if unknown:
        unread.append({"source": "schedules", "label": "Scheduled jobs",
                       "reason": f"how {unknown} scheduled job{'s' if unknown != 1 else ''} last ran is not known"})
    return out


ALERT_TONES = {"critical": "bad", "degraded": "warn", "info": "info"}


def _alerts(since, now, role, unread):
    # homestead_alerts reads its file leniently (a bad file is a fresh start);
    # here a file that is there but unreadable is said, not shown as no alerts.
    SHARED.read_json(os.path.join(ALERTS.DATA_DIR, "alerts.json"), {})
    out = []
    for a in ALERTS.log(limit=ALERTS.LOG_SIZE)["alerts"]:
        if a.get("category") == "test" or not isinstance(a.get("at"), (int, float)) or a["at"] <= since:
            continue
        tone = "ok" if a.get("phase") == "resolved" else ALERT_TONES.get(a.get("severity"), "info")
        out.append(_entry(a["at"], "alert", tone, str(a.get("title", "")), str(a.get("href") or "")))
    return out


def _hosts(since, now, role, unread):
    SHARED.read_json(os.path.join(HISTORY.DATA_DIR, HISTORY.FILE), {})     # as _alerts: unreadable is said
    out = []
    for e in HISTORY.maintenance_since(since):
        node, start, end = e["node"], e["start"], e.get("end")
        text, tone = f"{node} went into maintenance ({e['reason']})", "info"
        if end is not None and e.get("outcome") == "returned":
            text += f", back after {max(1, round((end - start) / 60))} min"
        elif end is not None and e.get("outcome") == "limit":
            text, tone = text + ", and was not back when its job's time ran out", "warn"
        out.append(_entry(start if start > since or end is None else end, "host", tone, text, f"/nodes?node={node}",
                          host=node))
    return out


def _audit_lines(since):
    """The audit lines after since, newest first, from audit.jsonl and then
    audit.jsonl.1, reading at most AUDIT_READ bytes from their ends; and
    whether that limit stopped the read before since. A missing file is no
    lines yet (the file is off, or nothing changed); one that cannot be read
    raises."""
    handles = []
    try:
        # Both opened before either is read: a rotation in between would
        # otherwise hand the same file over twice, as both names.
        for name in (AUDIT.FILE, AUDIT.FILE + ".1"):
            try:
                handles.append(open(os.path.join(SHARED.DIR, name), "rb"))
            except FileNotFoundError:
                continue
        if len(handles) == 2 and os.path.sameopenfile(handles[0].fileno(), handles[1].fileno()):
            handles.pop().close()
        return _read_lines(handles, since, AUDIT_READ)
    finally:
        for handle in handles:
            handle.close()


def _read_lines(handles, since, budget):
    out = []
    for handle in handles:
        size = os.fstat(handle.fileno()).st_size
        start = max(0, size - budget)
        handle.seek(start)
        data = handle.read(size - start)
        budget -= len(data)
        lines = data.split(b"\n")[1 if start else 0:]          # a line cut at the start is not read
        for raw in reversed(lines):
            try:
                entry = json.loads(raw)
            except ValueError:
                continue                                       # a half-written or garbled line
            at = AUDIT._at(raw) if isinstance(entry, dict) else None
            if at is not None and at <= since:
                return out, False
            if at is not None and entry.get("event") == "audit-suppressed" and isinstance(entry.get("suppressed"), int):
                out.append((at, {"suppressed": entry["suppressed"]}))     # refusals held back from the file
            elif at is not None and isinstance(entry.get("method"), str) and isinstance(entry.get("path"), str):
                out.append((at, entry))                        # anything else is a note, not a request
        if start:
            return out, True
    return out, False


def _who(entry):
    user = entry.get("user") if isinstance(entry.get("user"), str) and entry.get("user") else "someone not signed in"
    if entry.get("via") == "mcp":
        return f"{user}'s AI agent"
    return user


def _audit(since, now, role, unread):
    if role != "admin":
        # The lines name people and what they tried: an admin's to read.
        unread.append({"source": "audit", "label": "Audit log", "reason": "shown to admins only", "admin_only": True})
        return []
    lines, cut = _audit_lines(since)
    out, refused = [], []
    for at, e in lines:
        if "suppressed" in e:
            refused += [at] * e["suppressed"]
            continue
        if AUDIT._refusal({"status": e.get("status"), "outcome": e.get("outcome"), "user": e.get("user") or ""}):
            refused.append(at)
            continue
        status = e.get("status") if isinstance(e.get("status"), int) else 0
        # Never the address, e-mail, key, its owner or the replica: who, how and what only.
        what = e["tool"] if e.get("via") == "mcp" and isinstance(e.get("tool"), str) else f"{e['method']} {e['path']}"
        text = f"{_who(e)}{': ' if e.get('via') == 'mcp' else ' '}{what}"
        text += " (API key)" if e.get("via") == "key" else f" (from {e['from']})" if isinstance(e.get("from"), str) else ""
        text += f" failed ({status})" if status >= 400 else ""
        target = e.get("target") if isinstance(e.get("target"), dict) else {}
        app = f"{target['ns']}/{target['name']}" if target.get("ns") and target.get("name") else ""
        out.append(_entry(at, "action", "bad" if status >= 400 else "info", text,
                          _app_href(target["name"]) if app else "", app=app))
    if refused:
        out.append(_entry(max(refused), "action", "warn",
                          f"{len(refused)} request{'s were' if len(refused) != 1 else ' was'} refused"))
    if cut:
        unread.append({"source": "audit", "label": "Audit log",
                       "reason": f"the newest {_size(AUDIT_READ)} of the audit log were read; older lines are not shown"})
    return out


def _size(n):
    return f"{n // (1024 * 1024)} MiB" if n >= 1024 * 1024 else f"{n // 1024} KiB"


SOURCES = (("changes", "App changes", _changes), ("restarts", "App restarts", _restarts),
           ("schedules", "Scheduled jobs", _schedules), ("audit", "Audit log", _audit),
           ("alerts", "Alerts", _alerts), ("hosts", "Host maintenance", _hosts))


def timeline(user, role, since=None, now=None):
    """Everything recorded since `since` (default: when the person was last
    here), newest first, at most MAX_ENTRIES; what could not be read is named."""
    now = int(time.time() if now is None else now)
    unread, last = [], None
    try:
        last = last_here(user, now)
    except SHARED.Unreadable as error:
        unread.append({"source": "last-here", "label": "When you were last here", "reason": str(error)[:300]})
    if since is None:
        since = last if last is not None else now - FIRST_LOOK
    since = min(int(since), now)
    clamped = since < now - MAX_WINDOW
    since = max(since, now - MAX_WINDOW)
    entries = []
    for key, label, read in SOURCES:
        try:
            entries += read(since, now, role, unread)
        except Exception as error:      # one source never takes the others down (#620)
            # Any other error's words can name the API server's address: the
            # kind only here, the whole of it in the pod log.
            if not isinstance(error, SHARED.Unreadable):
                print(f"timeline: {key} not read: {str(error)[:300]}", flush=True)
            reason = (str(error) if isinstance(error, SHARED.Unreadable)
                      else f"Homestead could not read it ({type(error).__name__})")
            unread.append({"source": key, "label": label, "reason": reason[:300]})
    entries.sort(key=lambda e: e["at"], reverse=True)
    counts = {}
    for e in entries:
        counts[e["kind"]] = counts.get(e["kind"], 0) + 1
    return {"since": since, "now": now, "last_here": last, "clamped": clamped, "entries": entries[:MAX_ENTRIES],
            "counts": counts, "more": max(0, len(entries) - MAX_ENTRIES), "unread": unread}


def _since(request):
    """?since=<epoch seconds>, or None for "since I was last here"."""
    text = (request.query.get("since") or [None])[0]
    if text is None:
        return None
    if not str(text).isdigit() or len(str(text)) > 12:
        raise ValueError("since is a time in seconds since 1970")
    return int(text)


def _seen(request):
    if not request.user:
        return {"ok": False, "reason": "Only a signed-in account has a last visit to remember."}
    try:
        return {"ok": True, "since": mark_seen(request.user, time.time())}
    except (SHARED.Unreadable, OSError, ValueError) as error:
        # Held by a data move, or the file unreadable: the timeline still
        # answers, from the last visit Homestead could keep.
        return {"ok": False, "reason": str(error)[:300]}


ROUTES = {
    ("GET", "/api/timeline"): ("viewer", lambda request: timeline(request.user, request.role, _since(request))),
    ("POST", "/api/timeline/seen"): ("viewer", _seen),
}
