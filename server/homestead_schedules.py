"""Stop and start apps and VMs on a schedule.

A schedule is a stop time, a start time (either may be left out), the days of
the week they apply to, and the time zone they were chosen in - "stop at
01:00, start at 07:00, weekdays, Europe/London". It is kept as an annotation
on the app or VM, so setting one restarts nothing and it goes where the app
goes.

Once a minute the leader looks for a stop or start that fell due in the last
GRACE seconds and has not been done, and does it the way the buttons do: an
app is scaled to 0 and later back to the copies it had (its own checks, the
room check included); a VM is shut down cleanly through its power job and
later started through the same capacity review. A schedule acts on its times
and nothing else: an app started by hand at 03:00 stays up until the next
stop. A time missed while no leader ran (longer than GRACE) is skipped, not
done late. Each item's last result is kept, and one that failed raises an
alert until it next succeeds.
"""
import datetime
import json
import os
import threading
import time

import homestead_shared as SHARED

try:
    from zoneinfo import ZoneInfo
except ImportError:                                   # pragma: no cover
    ZoneInfo = None

KEY = "schedule"                  # homestead.io/schedule on the Deployment or VM
REPLICAS_KEY = "schedule-copies"  # the copies an app had when its schedule stopped it
GRACE = 30 * 60
DAY_NAMES = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
DATA_DIR = "/data"
_lock = threading.Lock()


# The apps and VMs with a schedule, and how one is set: server.py reads and
# writes them beside the rest of each workload.
scheduled = save = None


def bind(data_dir="/data", items=None, setter=None):
    global DATA_DIR, scheduled, save
    DATA_DIR = data_dir
    if items is not None:
        scheduled, save = items, setter


def _path():
    return os.path.join(DATA_DIR, "schedules.json")


def _load():
    try:
        with open(_path(), encoding="utf-8") as handle:
            data = json.load(handle)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _clock(value, what):
    text = str(value or "").strip()
    if not text:
        return ""
    try:
        hour, minute = (int(p) for p in text.split(":"))
    except ValueError:
        raise ValueError(f"the {what} time must be HH:MM") from None
    if not (0 <= hour <= 23 and 0 <= minute <= 59) or len(text) != 5:
        raise ValueError(f"the {what} time must be HH:MM, 00:00 to 23:59")
    return text


def zone(name):
    """The time zone by name, or UTC when it is unknown here."""
    if ZoneInfo and name:
        try:
            return ZoneInfo(str(name))
        except Exception:
            pass
    return datetime.timezone.utc


def clean(value, now=None):
    """A schedule as given, checked; None for no schedule."""
    if not value:
        return None
    if not isinstance(value, dict):
        raise ValueError("a schedule is a stop time, a start time and days")
    stop, start = _clock(value.get("stop"), "stop"), _clock(value.get("start"), "start")
    if not stop and not start:
        raise ValueError("choose a time to stop, a time to start, or both")
    if stop and stop == start:
        raise ValueError("stopping and starting at the same time would do nothing")
    try:
        days = sorted({int(d) for d in value.get("days") or []})
    except (TypeError, ValueError):
        raise ValueError("days are 0 (Monday) to 6 (Sunday)") from None
    if not days or any(d < 0 or d > 6 for d in days):
        raise ValueError("choose at least one day")
    tz = str(value.get("tz") or "UTC")[:64]
    if ZoneInfo and tz != "UTC":
        try:
            ZoneInfo(tz)
        except Exception:
            raise ValueError(f"{tz} is not a time zone Homestead knows") from None
    return {"stop": stop, "start": start, "days": days, "tz": tz, "since": round(now or time.time())}


def read(annotations, names):
    """The schedule an object carries, or None."""
    raw = names.read(annotations or {}, KEY)
    if not raw:
        return None
    try:
        value = json.loads(raw)
        return value if isinstance(value, dict) and (value.get("stop") or value.get("start")) else None
    except ValueError:
        return None


def _events(schedule, around, days_back=1, days_ahead=0):
    """(timestamp, action) of each stop and start from days_back days before
    around to days_ahead days after, in the schedule's time zone."""
    tz = zone(schedule.get("tz"))
    today = datetime.datetime.fromtimestamp(around, tz).date()
    out = []
    for offset in range(-days_back, days_ahead + 1):
        day = today + datetime.timedelta(days=offset)
        if day.weekday() not in schedule.get("days", []):
            continue
        for action in ("stop", "start"):
            at = schedule.get(action)
            if at:
                hour, minute = (int(p) for p in at.split(":"))
                out.append((datetime.datetime(day.year, day.month, day.day, hour, minute, tzinfo=tz).timestamp(), action))
    return sorted(out)


def due(schedule, now, done=""):
    """The stop or start to do now, as (timestamp, action), or None: the latest
    one in the last GRACE seconds, after the schedule was set, not yet done."""
    if not schedule:
        return None
    since = schedule.get("since") or 0
    ready = [e for e in _events(schedule, now) if now - GRACE <= e[0] <= now and e[0] >= since]
    if not ready:
        return None
    event = ready[-1]
    return None if done == f"{event[1]}@{int(event[0])}" else event


def upcoming(schedule, now, count=3):
    """The next few stops and starts, as (timestamp, action)."""
    if not schedule:
        return []
    return [e for e in _events(schedule, now, days_back=0, days_ahead=8) if e[0] > now][:count]


def describe(schedule):
    """In words: "Stops 01:00, starts 07:00, weekdays"."""
    if not schedule:
        return ""
    days = schedule.get("days") or []
    span = ("every day" if len(days) == 7 else "weekdays" if days == [0, 1, 2, 3, 4]
            else "weekends" if days == [5, 6] else ", ".join(DAY_NAMES[d] for d in days))
    parts = ([f"stops {schedule['stop']}"] if schedule.get("stop") else []) + (
        [f"starts {schedule['start']}"] if schedule.get("start") else [])
    text = ", ".join(parts) + f", {span}"
    return text[0].upper() + text[1:]


def items(workloads, vms):
    """Every app and VM with a schedule: kind, ns, name, schedule, and running."""
    out = []
    for w in workloads or []:
        if w.get("schedule"):
            out.append({"kind": "app", "ns": w["ns"], "name": w["name"], "schedule": w["schedule"],
                        "running": (w.get("desired") or 0) > 0})
    for v in vms or []:
        if v.get("schedule"):
            out.append({"kind": "vm", "ns": v["ns"], "name": v["name"], "schedule": v["schedule"],
                        "running": str(v.get("status") or "") not in ("Stopped", "Halted", "Succeeded", "Failed")})
    return out


def tick(scheduled, act, now=None):
    """Do whatever fell due. act(item, action) does it and returns a sentence;
    raising says why it could not. Returns what was done."""
    now = now or time.time()
    with _lock:
        state = _load()
        done_by_key, results, did = state.setdefault("done", {}), state.setdefault("results", {}), []
        keys = set()
        for item in scheduled:
            key = f"{item['kind']}:{item['ns']}/{item['name']}"
            keys.add(key)
            event = due(item["schedule"], now, done_by_key.get(key, ""))
            if not event:
                continue
            at, action = event
            done_by_key[key] = f"{action}@{int(at)}"
            try:
                detail = act(item, action)
                result = {"at": round(now), "action": action, "ok": True, "detail": detail}
            except Exception as error:
                result = {"at": round(now), "action": action, "ok": False, "detail": str(error)[:300]}
            results[key] = result
            did.append({"key": key, **result})
        # Forget items no longer scheduled, so their old failures stop alerting.
        stale = [k for k in list(results) + list(done_by_key) if k not in keys]
        for k in stale:
            results.pop(k, None)
            done_by_key.pop(k, None)
        if did or stale:
            SHARED.write_json(_path(), state)
    return did


def results():
    return _load().get("results") or {}


def report(scheduled, now=None):
    """Each scheduled item with its schedule in words, its next stops and starts
    and its last result."""
    now = now or time.time()
    last = results()
    rows = []
    for item in scheduled:
        key = f"{item['kind']}:{item['ns']}/{item['name']}"
        rows.append({**item, "key": key, "words": describe(item["schedule"]),
                     "next": [{"at": round(at), "action": action} for at, action in upcoming(item["schedule"], now)],
                     "last": last.get(key)})
    return sorted(rows, key=lambda r: (r["next"][0]["at"] if r["next"] else 1e12, r["name"]))


def alert_facts(last=None):
    """A scheduled stop or start that could not be done, until one is."""
    facts = []
    for key, result in sorted((last if last is not None else results()).items()):
        if result.get("ok"):
            continue
        kind, ref = key.split(":", 1)
        ns, name = ref.split("/", 1)
        what = "VM" if kind == "vm" else "app"
        facts.append({"key": f"schedule:{key}", "category": "degraded", "severity": "degraded",
                      "title": f"The scheduled {result.get('action')} of {name} did not happen",
                      "body": f"The {what} {name} was due to {result.get('action')} on its schedule: {result.get('detail')}. "
                              "The next scheduled time tries again.",
                      "resolved": f"{name}'s schedule ran", "href": (f"/vms?panel=schedule&ns={ns}&vm={name}" if kind == "vm"
                                                                     else f"/containers?panel=schedule&ns={ns}&workload={name}"),
                      "signals": {"action": result.get("action")}})
    return facts


# Its routes and who may use them (homestead_routes.py).
ROUTES = {
    ("GET", "/api/power-schedules"): ("viewer", lambda request: {"items": report(scheduled()), "grace": GRACE}),
    ("POST", "/api/power-schedules/set"): ("operator", lambda request: save(request.body)),
}
