"""A daily or weekly summary push (#921): "Past day: 3 changes, 1 restart,
nothing failed".

Push says when a condition starts, and an unchanged one is not repeated, so a
quiet week sends nothing at all. A person can ask for one summary a day, or
a week on a chosen day, at an hour of their own time zone. At that time the
leader builds it from the Timeline for that person's role
(homestead_timeline, no record of its own) and pushes it to their devices.

Kept per person in timeline-summary.json on the data volume, beside
"last here": the setting and the last slot claimed. Sent once: the leader
builds the summary, then claims the slot in that file under the shared
lock, and only then pushes. A restart or another copy taking the lead finds
the claim and sends nothing; a copy that stops between the claim and the push
loses that one summary rather than sending it twice. Slots missed while
Homestead was down collapse into the latest: at most one late summary, never
a backlog. Saving a setting claims the slot just gone, so turning it on
never sends at once.

A push carries nothing (homestead_push): the summary is a line in the alert
log meant for one person (category "summary", "for"), which only their
devices are handed, and which the bell, the Timeline and the alert webhook
leave out (ALERTS.PRIVATE). A source that could not be read is named in it,
never counted as nothing (#620).
"""
import datetime
import os
import threading
import time
import zoneinfo

import homestead_alerts as ALERTS
import homestead_push as PUSH
import homestead_shared as SHARED
import homestead_timeline as TIMELINE

FILE = "timeline-summary.json"
EVERY = 60
CHOICES = ("off", "daily", "weekly")
DAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
# The order kinds are counted in, and their words.
KINDS = (("change", "change", "changes"), ("restart", "restart", "restarts"), ("host", "host event", "host events"),
         ("schedule", "scheduled job run", "scheduled job runs"), ("alert", "alert", "alerts"),
         ("action", "action", "actions"))
TITLE_MAX = 100
# Strict: the claim is what keeps two copies from both sending, so it is never
# quietly a lock of this process only.
_lock = SHARED.SharedLock("timeline-summary", strict=True)
_started = []
_starting = threading.Lock()


def _path():
    return os.path.join(SHARED.DIR, FILE)


def _users(data):
    users = data.get("users")
    return users if isinstance(users, dict) else {}


# ------------------------------------------------------------ the schedule
def _zone(name):
    """The person's zone. One this server does not know is refused on save,
    never quietly read as UTC."""
    if not isinstance(name, str) or not name or len(name) > 64 or ".." in name or name.startswith("/"):
        raise ValueError("the time zone is not one Homestead knows")
    try:
        return zoneinfo.ZoneInfo(name)
    except (zoneinfo.ZoneInfoNotFoundError, ValueError, OSError):
        raise ValueError(f"the time zone {name} is not one Homestead knows") from None


def _at(day, hour, zone):
    # An hour the clocks skip is read with the offset before the change, one
    # they repeat as its first time: either way one slot that day.
    return int(datetime.datetime(day.year, day.month, day.day, hour, tzinfo=zone).timestamp())


def _slots(row, now, step):
    """The person's slots from the day of now, a day at a time (step +1 or -1)."""
    zone = _zone(row["tz"])
    today = datetime.datetime.fromtimestamp(now, zone).date()
    for n in range(0, 9):
        day = today + datetime.timedelta(days=n * step)
        if row["every"] == "weekly" and day.weekday() != row["day"]:
            continue
        yield _at(day, row["hour"], zone)


def last_slot(row, now):
    """The latest time the summary was due at or before now."""
    return next(s for s in _slots(row, now, -1) if s <= now)


def previous_slot(row, slot):
    return last_slot(row, slot - 1)


def next_slot(row, now):
    return next(s for s in _slots(row, now, 1) if s > now)


def due(row, now):
    """The slot to send for, or None. Only the latest slot is ever due."""
    slot = last_slot(row, now)
    return slot if slot > (row.get("slot") or 0) else None


# ------------------------------------------------------------- the setting
def clean(body):
    """The setting as asked for, checked; None for Off."""
    body = body if isinstance(body, dict) else {}
    every = body.get("every")
    if every not in CHOICES:
        raise ValueError("a summary is off, daily or weekly")
    if every == "off":
        return None
    hour, day = body.get("hour"), body.get("day", 0)
    if type(hour) is not int or not 0 <= hour <= 23:
        raise ValueError("the hour is a whole number from 0 to 23")
    if every == "weekly" and (type(day) is not int or not 0 <= day <= 6):
        raise ValueError("the day is 0 (Monday) to 6 (Sunday)")
    _zone(body.get("tz"))
    return {"every": every, "day": day if every == "weekly" else 0, "hour": hour, "tz": body["tz"]}


def _shown(row, now):
    if not isinstance(row, dict) or row.get("every") not in ("daily", "weekly"):
        return {"every": "off"}
    shown = {k: row.get(k) for k in ("every", "day", "hour", "tz", "sent_at", "result")}
    try:
        shown["next"] = next_slot(row, now)
    except ValueError:
        shown["next"] = None        # a zone this server no longer has: said on the card, not sent
    return shown


def view(user, now):
    """Raises SHARED.Unreadable when the file is there but unreadable."""
    return _shown(_users(SHARED.read_json(_path(), {})).get(user) if user else None, now)


def save(user, body, now):
    if not user:
        raise ValueError("Only a signed-in account can have a summary push.")
    wanted = clean(body)

    def change(data):
        users = _users(data)
        old = users.get(user) if isinstance(users.get(user), dict) else {}
        if wanted is None:
            users.pop(user, None)
        elif all(old.get(k) == v for k, v in wanted.items()):
            pass            # saved again unchanged: a summary due now still goes
        else:
            # The slot just gone counts as sent: the first summary is the next one.
            users[user] = {**wanted, "slot": max(old.get("slot") or 0, last_slot(wanted, now)),
                           **{k: v for k, v in old.items() if k in ("sent_at", "result", "upto")}}
        data["users"] = users
        return users.get(user)

    row = SHARED.update_file(_path(), change, _lock, {}, separators=(",", ":"), sort_keys=True, mode=0o600,
                             durable=True)
    return _shown(row, now)


# ---------------------------------------------------------------- the words
def _local(stamp, row):
    moment = datetime.datetime.fromtimestamp(stamp, _zone(row["tz"]))
    return f"{DAYS[moment.weekday()]} {moment:%H:%M}"


def _and(words):
    return words[0] if len(words) == 1 else ", ".join(words[:-1]) + " and " + words[-1]


def compose(feed, row, since, slot):
    """The push's title, body and link, from a timeline() answer."""
    counts = feed.get("counts") or {}
    unread = [u for u in feed.get("unread") or [] if not u.get("admin_only") and u.get("source") != "last-here"]
    # The feed counts failures over the whole window; its entries stop at 300.
    known = "failed" in feed or not feed.get("more")
    failed = feed["failed"] if "failed" in feed else sum(
        1 for e in feed.get("entries") or [] if e.get("tone") == "bad" and e.get("kind") != "alert")
    parts = [f"{counts[kind]} {one if counts[kind] == 1 else more}" for kind, one, more in KINDS if counts.get(kind)]
    if failed:
        parts.append(f"{failed} failed")
    elif parts and not unread and known:
        parts.append("nothing failed")
    if not parts:
        parts = ["nothing recorded" if unread else "nothing happened"]
    period = {"daily": "Past day", "weekly": "Past week"}[row["every"]]
    # From the last summary, which went out a minute or so after its slot.
    prefix = period if abs(since - previous_slot(row, slot)) <= 3600 else f"Since {_local(since, row)}"
    title = f"{prefix}: {', '.join(parts)}"
    if len(title) > TITLE_MAX:
        title = title[:TITLE_MAX - 1].rsplit(",", 1)[0] + "…"
    labels = [u.get("label") or u.get("source") or "a source" for u in unread]
    body = f"{_and([labels[0]] + [l[:1].lower() + l[1:] for l in labels[1:]])} could not be read." if labels else ""
    return {"title": title, "body": body, "href": f"/timeline?since={int(since)}"}


# ----------------------------------------------------------------- sending
def _since(row, slot):
    """Where the last summary ended, so nothing is counted twice; without
    one, a whole period before this one, or the slot claimed on saving when
    that is earlier. Never further back than the Timeline reaches."""
    claimed, previous = row.get("slot") or 0, previous_slot(row, slot)
    start = row.get("upto") or (claimed if claimed and claimed < previous else previous)
    return max(int(start), slot - TIMELINE.MAX_WINDOW)


def _claim(user, slot, now):
    """Mark the slot sent for this person; False when it no longer is due
    (claimed already, or the setting changed)."""
    def change(data):
        row = _users(data).get(user)
        if not isinstance(row, dict) or due(row, now) != slot:
            return False
        # upto: the summary counted up to now, and the next starts there.
        row.update(slot=slot, sent_at=int(now), upto=int(now), result="")
        return True
    return SHARED.update_file(_path(), change, _lock, {}, separators=(",", ":"), sort_keys=True, mode=0o600,
                              durable=True)


def _record(user, slot, result):
    def change(data):
        row = _users(data).get(user)
        if isinstance(row, dict) and row.get("slot") == slot:
            row["result"] = result[:200]
    try:
        SHARED.update_file(_path(), change, _lock, {}, separators=(",", ":"), sort_keys=True, mode=0o600)
    except Exception as error:      # what the card shows is not worth more than the send
        print(f"summary: result not kept: {str(error)[:160]}", flush=True)


def _send(user, role, row, slot, now):
    if not PUSH.devices(user):
        # Nothing to send to: the slot passes, and no summary is built.
        if _claim(user, slot, now):
            _record(user, slot, "no device of yours has notifications on")
        return "no device"
    since = _since(row, slot)
    words = compose(TIMELINE.timeline(user, role, since=since, now=now), row, since, slot)
    if not _claim(user, slot, now):
        return "claimed"
    try:
        ALERTS.note({"key": f"summary:{user}:{slot}", "category": "summary", "severity": "info", "event": True,
                     "for": user, **words}, now)
        result = PUSH.send(lambda device: device["user"] == user)
        said = f"sent to {result['sent']} device{'s' if result['sent'] != 1 else ''}"
    except Exception as error:
        said = f"not sent: {str(error)[:160]}"
    _record(user, slot, said)
    return said


def step(now=None, users=None):
    """One round on the leader: each person whose summary is due gets it.
    Returns [(user, what happened)]."""
    now = int(time.time() if now is None else now)
    rows = _users(SHARED.read_json(_path(), {}))
    if not rows:
        return []           # nobody asked: the accounts are not read
    if users is None:
        import homestead_auth as AUTH
        users = AUTH.list_users()
    roles = {u["name"]: u.get("role", "viewer") for u in users}
    done = []
    for user, row in sorted(rows.items()):
        if user not in roles or not isinstance(row, dict):
            continue        # an account since removed is sent nothing
        try:
            slot = due(row, now)
            if slot is not None:
                done.append((user, _send(user, roles[user], row, slot, now)))
        except Exception as error:      # one person's summary never stops the others'
            print(f"summary: {user}: {str(error)[:160]}", flush=True)
            done.append((user, f"not sent: {str(error)[:160]}"))
    return done


def run(sleep=time.sleep, clock=time.time):
    """Every minute on the leader, a few seconds after it starts."""
    import homestead_heartbeats as HEART
    import homestead_leader as LEADER
    while True:
        if LEADER.is_leader():
            try:
                step()
                HEART.beat("summary", EVERY, leader_only=True)
            except Exception as error:
                HEART.beat("summary", EVERY, error, leader_only=True)
                print(f"summary: {str(error)[:160]}", flush=True)
        sleep(EVERY - clock() % EVERY + 5)


def start():
    """Start the worker once; the alert loop calls this before its own loop."""
    with _starting:
        if _started:
            return
        _started.append(threading.Thread(target=run, name="summary-push", daemon=True))
    _started[0].start()


def forget_user(user):
    """An account removed: its setting goes too, so a new account of the same
    name never inherits summaries it did not ask for. Most people have no
    row: nothing is locked or written for them."""
    if user not in _users(SHARED.read_json(_path(), {})):
        return

    def change(data):
        _users(data).pop(user, None)
    SHARED.update_file(_path(), change, _lock, {}, separators=(",", ":"), sort_keys=True, mode=0o600)


def _post(request):
    return save(request.user, request.body, time.time())


ROUTES = {
    ("GET", "/api/timeline/summary"): ("viewer", lambda request: view(request.user, time.time())),
    ("POST", "/api/timeline/summary"): ("viewer", _post),
}
