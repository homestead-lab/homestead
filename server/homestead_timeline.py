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
import os
import time

import homestead_shared as SHARED

SEEN_FILE = "timeline-seen.json"
VISIT_GAP = 30 * 60          # this long without Homestead open ends a visit
FIRST_LOOK = 86400           # a first visit looks back one day
MAX_WINDOW = 30 * 86400      # the furthest back the timeline reaches
SEEN_USERS = 500
SEEN_KEEP = 90 * 86400
SEEN_QUIET = 60              # a mark this soon after the last is not written
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
