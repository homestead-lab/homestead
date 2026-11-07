"""Moving containers back after a host returns.

When a host fails or is drained, its containers start on the others, and
nothing moves them back when it returns: the cluster stays lopsided until
someone opens Balance hosts (#347). This watches each host and notices one
coming back - Ready again after not being, or taking work again after being
cordoned - and then, as settings.placement.rebalance says:

* off: nothing.
* suggest: an alert says the host is back and how many containers the
  container rebalance would move; nothing moves until someone reviews it.
* window: the same alert, and in each opening of the maintenance window the
  rebalance runs by itself, as the job a person would start, leaving out any
  app whose backup job is due before the window closes - a backup has to run
  on the app's host while its volume is attached there, and a move under it
  would fail it or the move. Under Spread by capacity it also moves apps Flux
  applies, by restarting them (split_gitops); a placement would be undone.
* auto: the same run with no window, once the hosts pass the gates in
  homestead_recovery_auto. Critical apps wait for the window, an app moves at most
  once a day, and a stop or a failed run stays stopped until someone resumes it.

A returned host is remembered until the rebalance finds nothing worth moving,
or for RETURN_KEEP at most - long enough for a weekly window. A host is "back"
only once it has been seen taking work before: one joining for the first time
is new, not back.
"""
import calendar
import datetime
import json
import os
import threading
import time
import zoneinfo

import homestead_placement_mode as PLACEMODE
import homestead_priority as PRIORITY
import homestead_recovery_auto as AUTO
import homestead_recovery_marks as MARKS
import homestead_shared as SHARED

DATA_DIR = "/data"
FILE = "recovery.json"
MODES = ("off", "suggest", "window", "auto")
RETURN_KEEP = 7 * 86400
RUN_GAP = 900           # between automatic runs
KEPT_SHOWN = 30         # apps left out with why that Settings and the alert have room for
APP_COOLDOWN = 86400    # an app moved automatically stays put this long, so nothing ping-pongs
_lock = threading.Lock()


# What the leader's look reads and starts, bound by server.py: the cluster,
# the app settings, the job list, Balance hosts' plan and its job, and the
# kinds of job that move or restart apps or hosts - none starts beside them.
read = settings = operations = plan = start = None
WAITS_FOR = ()
_planned = [0.0]
# For the loop: whether this replica leads, and the heartbeat Settings shows;
# and which tool applies an app (homestead_updates.managed_by).
is_leader, beat = lambda: False, lambda *args, **kwargs: None
managed_by = lambda dep: {}
# The other jobs that move or restart apps, or change hosts, by the kind
# their modules give them, beside the ones server.py names by their module's
# KIND: the run waits for every one of them.
MOVES_OR_RESTARTS = ("host-os", "host-promote", "cluster-shutdown", "deployment", "move", "workload-copy", "workload-rename",
                     "restructure", "reclass", "volume-restore", "snapshot-revert", "self-data-move",
                     "self-data-handoff", "self-data-prepare", "vm-migration", "vm-power", "image-update",
                     "image-rollback", "disk-retire", "disk-v2-convert", "disk-allocation", "longhorn-v2-prepare",
                     "platform-upgrade", "harvester-upgrade", "restore-test", "set-priority", "restore-swap",
                     "host-move", "set-restart", "volume-rebalance", "volume-copy-move")
# How long one move may take: Balance hosts waits up to ten minutes for each
# container to start on its new host, so a run may outlast the window.
MOVE_WAIT = 600


# What bind() puts back for an argument it is not given: the module's own default,
# not the last bind's (a test's fake must not outlive the test).
_UNBOUND = {"read": read, "settings": settings, "operations": operations, "plan": plan, "start": start, "WAITS_FOR": WAITS_FOR, "is_leader": is_leader, "beat": beat, "managed_by": managed_by}


def bind(data_dir, _read=None, _settings=None, _operations=None, _plan=None, _start=None, waits_for=None,
         _is_leader=None, _beat=None, _managed_by=None):
    global DATA_DIR, read, settings, operations, plan, start, WAITS_FOR, is_leader, beat, managed_by
    DATA_DIR = data_dir
    if _read:
        read, settings, operations, plan, start = _read, _settings, _operations, _plan, _start
        WAITS_FOR = tuple(dict.fromkeys(tuple(waits_for or ()) + MOVES_OR_RESTARTS))
    else:
        read, settings, operations, plan, start, WAITS_FOR = (
            _UNBOUND[n] for n in ("read", "settings", "operations", "plan", "start", "WAITS_FOR"))
    if _is_leader:
        is_leader, beat = _is_leader, _beat or _UNBOUND["beat"]
    else:
        is_leader, beat = _UNBOUND["is_leader"], _UNBOUND["beat"]
    managed_by = _managed_by if _managed_by else _UNBOUND["managed_by"]


def _path():
    return os.path.join(DATA_DIR, FILE)


def _load():
    """Raises SHARED.Unreadable for a file that is there but cannot be read."""
    data = SHARED.read_json(_path(), {})
    return {"hosts": data.get("hosts") or {}, "worked": data.get("worked") or [],
            "returned": data.get("returned") or {}, "suggestion": data.get("suggestion") or {},
            "ran": data.get("ran") or "", "marks": data.get("marks") or {}, "undone": data.get("undone") or {},
            "auto": data.get("auto") or {}}


def _save(data):
    SHARED.write_json(_path(), data, separators=(",", ":"), sort_keys=True)


# ------------------------------------------------------------- the window
def window(maintenance, now):
    """Whether the maintenance window is open at now, and when this opening
    closes (0 when shut). A window may cross midnight: its early minutes then
    belong to the day it started on."""
    current = time.gmtime(now)
    hour, minute = (int(part) for part in maintenance["start"].split(":"))
    start, duration = hour * 60 + minute, int(maintenance["duration_minutes"])
    current_minute = current.tm_hour * 60 + current.tm_min
    midnight = calendar.timegm((current.tm_year, current.tm_mon, current.tm_mday, 0, 0, 0))
    if current.tm_wday in maintenance["days"] and start <= current_minute < min(1440, start + duration):
        return True, midnight + (start + duration) * 60
    carry = max(0, start + duration - 1440)
    if (current.tm_wday - 1) % 7 in maintenance["days"] and current_minute < carry:
        return True, midnight + carry * 60
    return False, 0


def window_key(maintenance, now):
    """Which opening of the window now falls in, so it runs once per opening."""
    open_, closes = window(maintenance, now)
    return time.strftime("%Y-%m-%dT%H:%M", time.gmtime(closes)) if open_ else ""


# ------------------------------------------------------------- cron
MACROS = {"@yearly": "0 0 1 1 *", "@annually": "0 0 1 1 *", "@monthly": "0 0 1 * *", "@weekly": "0 0 * * 0",
          "@daily": "0 0 * * *", "@midnight": "0 0 * * *", "@hourly": "0 * * * *"}


# Names Kubernetes' cron accepts for months and days of the week.
MONTHS = {m: i + 1 for i, m in enumerate("jan feb mar apr may jun jul aug sep oct nov dec".split())}
DAYS = {d: i for i, d in enumerate("sun mon tue wed thu fri sat".split())}


def _field(text, lo, hi, names=None):
    out = set()
    for name, number in (names or {}).items():
        text = text.lower().replace(name, str(number))
    for part in text.split(","):
        rng, _, step = part.partition("/")
        step = int(step) if step else 1
        if rng in ("*", "?"):
            a, b = lo, hi
        elif "-" in rng:
            a, b = (int(x) for x in rng.split("-", 1))
        else:
            a = int(rng)
            b = hi if step > 1 else a
        if step < 1 or a < lo or b > hi or a > b:
            raise ValueError(text)
        out.update(range(a, b + 1, step))
    return out


def fires_between(schedule, start, end, tz="", last=""):
    """Whether a CronJob's schedule fires in [start, end). None when the
    schedule or its time zone cannot be read - the caller treats that as
    'it may', since a backup missed is worse than a move put off.

    Without a time zone of its own, Kubernetes reads a schedule on the clock
    of the host its controller runs on - on k3s, often local time, not UTC.
    That offset is not known, so every offset the job's last run (last) is
    consistent with counts; with no last run, every offset does."""
    text = MACROS.get(str(schedule).strip(), str(schedule).strip())
    parts = text.split()
    if len(parts) != 5:
        return None
    try:
        minute, hour, dom, month = (_field(parts[0], 0, 59), _field(parts[1], 0, 23),
                                    _field(parts[2], 1, 31), _field(parts[3], 1, 12, MONTHS))
        dow = {d % 7 for d in _field(parts[4], 0, 7, DAYS)}
    except ValueError:
        return None
    any_dom, any_dow = parts[2] in ("*", "?"), parts[4] in ("*", "?")

    def matches(m, h, d, mo, wd):
        # Cron's rule: with both day fields restricted, either one will do.
        day = (wd in dow) if any_dom else (d in dom) if any_dow else (d in dom or wd in dow)
        return m in minute and h in hour and mo in month and day

    def on_clock(t, offset):
        g = time.gmtime(t + offset)
        return g.tm_min, g.tm_hour, g.tm_mday, g.tm_mon, (g.tm_wday + 1) % 7

    first = int(start) - int(start) % 60
    if tz:
        if tz in ("UTC", "Etc/UTC"):
            local = lambda t: on_clock(t, 0)
        else:
            try:
                zone = zoneinfo.ZoneInfo(tz)
            except Exception:
                return None
            local = lambda t: (lambda x: (x.minute, x.hour, x.day, x.month, (x.weekday() + 1) % 7))(
                datetime.datetime.fromtimestamp(t, zone))
        return any(matches(*local(t)) for t in range(first, int(end), 60))
    offsets = range(-12 * 3600, 14 * 3600 + 1, 900)
    try:
        ran = calendar.timegm(time.strptime(str(last), "%Y-%m-%dT%H:%M:%SZ")) if last else None
    except ValueError:
        ran = None
    if ran is not None:
        offsets = [o for o in offsets if matches(*on_clock(ran, o))]
        if not offsets:
            return None
        # The last run may be from before the clocks changed: an hour either
        # side of each offset it fits counts too.
        offsets = sorted({o + shift for o in offsets for shift in (-3600, 0, 3600)})
    return any(matches(*on_clock(t, o)) for t in range(first, int(end), 60) for o in offsets)


def guarded(cronjobs, deployments, start, end):
    """Apps (ns/name) a scheduled job shares a volume with and that job runs,
    or may run, before end - or runs now."""
    claims = {}
    for dep in deployments:
        meta = dep.get("metadata") or {}
        for volume in (((dep.get("spec") or {}).get("template") or {}).get("spec") or {}).get("volumes") or []:
            claim = (volume.get("persistentVolumeClaim") or {}).get("claimName")
            if claim:
                claims.setdefault((meta.get("namespace"), claim), set()).add(f"{meta.get('namespace')}/{meta.get('name')}")
    out = {}
    for cron in cronjobs:
        meta, spec, status = cron.get("metadata") or {}, cron.get("spec") or {}, cron.get("status") or {}
        if spec.get("suspend"):
            continue
        pod = (((spec.get("jobTemplate") or {}).get("spec") or {}).get("template") or {}).get("spec") or {}
        apps = set()
        for volume in pod.get("volumes") or []:
            claim = (volume.get("persistentVolumeClaim") or {}).get("claimName")
            apps |= claims.get((meta.get("namespace"), claim), set())
        if not apps:
            continue
        due = True if status.get("active") else fires_between(spec.get("schedule", ""), start, end, spec.get("timeZone") or "",
                                                               status.get("lastScheduleTime") or "")
        if due is not False:
            why = (f"its scheduled job {meta.get('name')} is running" if status.get("active")
                   else f"its scheduled job {meta.get('name')} runs before the window closes" if due
                   else f"its scheduled job {meta.get('name')} has a schedule Homestead cannot read, so it may run then")
            for app in apps:
                out.setdefault(app, why)
    return out


# ------------------------------------------------------------- hosts
def observe(nodes, now=None, standby=()):
    """Notes each host that takes work again; returns the hosts back within
    RETURN_KEEP. A host seen for the first time has not come back, and nor
    has one taking work for the first time - a new host finishing joining -
    nor a standby one: nothing is moved onto it, so its return is no reason
    to move anything."""
    now = int(now or time.time())
    with _lock:
        data = _load()
        before = json.dumps(data, sort_keys=True)
        seen, worked = {}, set(data["worked"])
        for node in nodes:
            name = (node.get("metadata") or {}).get("name")
            if not name:
                continue
            seen[name] = PLACEMODE.takes_work(node)
            if seen[name] and data["hosts"].get(name) is False and name in worked and name not in standby:
                data["returned"][name] = now
            if not seen[name]:
                data["returned"].pop(name, None)
            if seen[name]:
                worked.add(name)
        data["hosts"], data["worked"] = seen, sorted(worked & set(seen))
        data["returned"] = {n: t for n, t in data["returned"].items() if now - t < RETURN_KEEP and n in seen}
        if not data["returned"]:
            data["suggestion"] = {}
        # Every minute, mostly with nothing new: written only when it changed.
        if json.dumps(data, sort_keys=True) != before:
            _save(data)
        return dict(data["returned"])


def suggest(moves, now=None, forget=True):
    """What the rebalance would do now for the hosts back; nothing to move
    forgets them, since the scheduler already evened things out - unless not
    forget: a host just back often has no figures yet, and would be forgotten."""
    with _lock:
        data = _load()
        if not data["returned"]:
            return
        if not moves and forget:
            data["returned"], data["suggestion"] = {}, {}
        else:
            data["suggestion"] = {"hosts": sorted(data["returned"]), "moves": len(moves), "at": int(now or time.time())}
        _save(data)


def observe_returned():
    """The hosts back and not yet dealt with: a suggestion with nothing to
    move forgets them."""
    with _lock:
        return dict(_load()["returned"])


def ran(key):
    """This opening of the window has had its rebalance. The hosts back stay
    remembered: apps a backup kept where they were are moved at the next
    opening, and the next plan that finds nothing to move forgets them."""
    with _lock:
        data = _load()
        data["ran"] = key
        _save(data)


def already_ran(key):
    with _lock:
        return bool(key) and _load()["ran"] == key


def split_gitops(deployments, placement_mode, undone):
    """Which apps a tool applies are left out of the window's run, and which move by
    restarting ({id: tool}). Only Flux, only under Spread by capacity (Andreas, 6 Nov),
    and not once a tool has taken a restart mark off. Argo CD stays out until someone
    has tried it: nothing here can test it. A person's Updated elsewhere mark means
    leave it alone."""
    left_out, restart = set(), {}
    for dep in deployments:
        meta, tool = dep.get("metadata") or {}, managed_by(dep)
        if not tool:
            continue
        app = f"{meta.get('namespace')}/{meta.get('name')}"
        if tool.get("by") == "Flux" and tool.get("detected") and placement_mode == "spread" and not undone:
            restart[app] = tool["by"]
        else:
            left_out.add(app)
    return left_out, restart


def marked(app, value, by, now=None):
    """An app restarted to move it, whose mark is now watched (MARKS)."""
    with _lock:
        data = _load()
        data["marks"][app] = {"value": value, "by": by, "at": int(now or time.time())}
        _save(data)


def undone_by(app, by, now=None):
    """A tool took a restart's mark off: no more restarts of what it applies."""
    with _lock:
        data = _load()
        data["undone"] = {"app": app, "by": by, "at": int(now or time.time())}
        _save(data)


def restart_why(dep, by="Flux"):
    """Why an app the plan chose to restart must not be, now; "" when it still may.
    Asked before each restart, as a run can outlast all of this. by: the tool that
    applies it, "" for one no tool does."""
    with _lock:
        undone = _load()["undone"]
    if undone and by:
        return f"{undone['by']} took a restart mark off, so apps it applies are left out"
    mode = (settings().get("placement") or {}).get("mode", "")
    if not by:
        return "" if mode == "spread" and not managed_by(dep) else "it no longer moves by restarting"
    return "" if split_gitops([dep], mode, undone)[1] else "it no longer moves by restarting"


def _watch_marks(now):
    """In every mode: a mark taken off stops restart moves until someone looks."""
    with _lock:
        marks = _load()["marks"]
    if not marks:
        return
    still, undone = MARKS.check(marks, read("/apis/apps/v1/deployments").get("items", []), now)
    if still == marks:
        return
    with _lock:
        data = _load()
        # One added meanwhile, by the job's thread, is kept.
        data["marks"] = {app: mark for app, mark in data["marks"].items() if app in still or app not in marks}
        for app, mark in sorted(undone.items()):
            data["undone"] = {"app": app, "by": mark.get("by", ""), "at": int(now)}
        _save(data)


def alert_facts(mode=None, maintenance=None):
    """A host back, with containers worth moving back to even things out; and a tool
    that took a restart mark off. mode and maintenance are read from the settings when
    not given."""
    if mode is None:
        current = settings()
        mode = (current.get("placement") or {}).get("rebalance", "off")
        maintenance = current["updates"]["maintenance"]
    with _lock:
        data = _load()
    found, undone = data["suggestion"], data["undone"]
    facts = [{"key": "recovery:restart-undone", "category": "joins", "severity": "degraded",
              "title": f"{undone['by']} removed Homestead's restart mark from {undone['app']}, so it restarted a second time",
              "body": f"Apps {undone['by']} applies are left out of moves back until you Resume.",
              "resolved": "Moves back can restart apps again", "href": "/settings?tab=general",
              "signals": {"undone": 1}}] if undone else []
    if mode not in ("suggest", "window") or not found.get("moves"):
        return facts
    hosts, moves = found["hosts"], found["moves"]
    who = " and ".join(hosts) if len(hosts) < 3 else f"{len(hosts)} hosts"
    if mode != "window" or not maintenance:
        then = "Nothing moves until you review it."
    elif not maintenance.get("days"):
        then = "The update window has no days set, so nothing moves by itself; review it."
    else:
        then = (f"They move in the next update window ({maintenance['start']} UTC) unless you start it sooner - "
                "apart from any whose scheduled job runs then.")
    return facts + [{"key": "recovery:rebalance", "category": "joins", "severity": "info",
             "title": f"{who} {'is' if len(hosts) == 1 else 'are'} back: {moves} container{'' if moves == 1 else 's'} could move to even out the hosts",
             "body": f"Containers started elsewhere while {who} {'was' if len(hosts) == 1 else 'were'} away. "
                     f"Balance hosts shows the moves; each restarts its container once. {then}",
             "resolved": "No containers need to move back now", "href": "/containers?panel=rebalance", "signals": {"moves": moves}}]


# ------------------------------------------------------------- mode auto
def moved_back(app, now=None):
    """An app an automatic run moved: it stays put for APP_COOLDOWN."""
    with _lock:
        data = _load()
        data["auto"].setdefault("moved", {})[app] = int(now or time.time())
        _save(data)


def _auto_update(change):
    with _lock:
        data = _load()
        before = json.dumps(data["auto"], sort_keys=True)
        change(data["auto"])
        if json.dumps(data["auto"], sort_keys=True) != before:
            _save(data)


def _auto_job(now):
    """How the last automatic run ended. A cancel or a failure is someone saying look
    first (Andreas), so it pauses until Resume, or until the choice changes; a run
    that stopped because a host left ended as succeeded, and goes on when it settles."""
    jobs = operations.snapshot()

    def change(auto):
        if "moved" in auto:
            auto["moved"] = {app: t for app, t in auto["moved"].items() if now - t < APP_COOLDOWN}
        job = auto.get("job")
        item = next((i for i in jobs if i.get("id") == job), None) if job else None
        if job and item is None:
            # Dismissed, say: it may have failed or been stopped. Leaves it safe.
            auto.update(job="", paused={"why": "Homestead could not see how its last run ended", "at": int(now)})
        elif item is not None and item.get("status") in operations.TERMINAL:
            auto.update(job="", ended=int(now))
            if item.get("status") in ("cancelled", "failed"):
                auto["paused"] = {"why": item.get("message") or item["status"], "at": int(now)}
    _auto_update(change)


def _auto_wait(reasons=(), hosts=None, later=None, left_out=None, kept=None):
    """What it is waiting for, for Settings and the alert. No cluster read behind it:
    the look stored it."""
    def change(auto):
        waiting = auto.get("waiting") or {"hosts": [], "later": 0, "left_out": 0, "kept": []}
        waiting["reasons"] = list(reasons)
        for key, value in (("hosts", hosts), ("later", later), ("left_out", left_out), ("kept", kept)):
            if value is not None:
                waiting[key] = value
        auto["waiting"] = waiting
    _auto_update(change)


def _hold_reasons(nodes, data, standby, returned, now):
    """Each returned host's clock and copies, and any other host away. Longhorn that
    can't be read raises: nothing moves, and Settings' health shows the tick's error."""
    by_name = {(n.get("metadata") or {}).get("name"): n for n in nodes}
    volumes, replicas, engines = AUTO.read_copies(read)
    rows, reasons = [], []
    for host, back_at in sorted(returned.items()):
        left = AUTO.steady_left(by_name.get(host) or {}, back_at, now)
        copies = AUTO.copies_left(host, volumes, replicas, engines)
        rows.append({"name": host, "steady_in": left, "copies_left": len(copies)})
        if left:
            reasons.append(f"{host} steady in {max(1, -(-left // 60))} min")
        if copies:
            reasons.append(f"{len(copies)} cop{'y' if len(copies) == 1 else 'ies'} rebuilding on {host}")
    reasons += [f"{host} is away" for host in AUTO.away(nodes, data["worked"], standby, returned)]
    return rows, reasons


def _auto_tick(current, placement, nodes, standby, returned, now):
    """One look in mode auto; each hold stores why. Clocks live in the file, so a new
    leader loses no steady time and runs nothing early."""
    busy = sorted({i.get("title") or "Another job" for i in operations.snapshot()
                   if i.get("kind") in WAITS_FOR and i.get("status") not in operations.TERMINAL})
    if busy:
        return _auto_wait([f"{title} is running" for title in busy])
    with _lock:
        data = _load()
    auto = data["auto"]
    if auto.get("paused"):
        return _auto_wait([f"Stopped: {auto['paused'].get('why', '')}"])
    if now - auto.get("ended", 0) < RUN_GAP:
        return _auto_wait([f"The last run ended {max(1, int((now - auto['ended']) // 60))} min ago"])
    rows, reasons = _hold_reasons(nodes, data, standby, returned, now)
    if now - _planned[0] < 300:      # a plan reads every pod and volume
        return _auto_wait(reasons, rows)
    _planned[0] = now
    deployments = read("/apis/apps/v1/deployments").get("items", [])
    maintenance = current["updates"]["maintenance"]
    key = window_key(maintenance, now)
    opening = bool(key) and not already_ran(key)
    ladder = PRIORITY.ladder()
    critical = {f"{(d.get('metadata') or {}).get('namespace')}/{(d.get('metadata') or {}).get('name')}"
                for d in deployments if AUTO.waits_for_window(d, ladder)}
    later = set() if opening else critical
    left_out, restart = split_gitops(deployments, placement.get("mode", ""), data["undone"])
    cooldown = {app for app, t in (auto.get("moved") or {}).items() if now - t < APP_COOLDOWN}
    back = sorted(returned)
    may_restart = placement.get("mode") == "spread"
    whole = plan(sorted(left_out), avoid=standby, restart=restart, back=back, may_restart=may_restart)
    keep = guarded(read("/apis/batch/v1/cronjobs").get("items", []), deployments, now, now + MOVE_WAIT * len(whole["moves"]))
    chosen = plan(sorted(left_out | later | cooldown | set(keep)), avoid=standby, restart=restart, back=back, may_restart=may_restart)
    if not whole.get("metrics") or (whole.get("metrics_state") or {}).get("state") != "ok":
        reasons.append("The CPU and memory figures aren't current")
    kept = (whole.get("kept") or [])[:KEPT_SHOWN]
    _auto_wait(reasons, rows, sum(m["id"] in later for m in whole["moves"]), len(left_out), kept)
    # Forgotten only once it has passed, nothing at all is left to move, and nothing
    # stays only because it can't: that isn't "nothing needs to move". RETURN_KEEP ends it.
    suggest(chosen["moves"], now, forget=not reasons and not whole["moves"] and not kept)
    if reasons or not chosen["moves"]:
        return None
    chosen["automatic"] = True
    try:
        started = start(chosen, f" after {', '.join(back)} came back", unless=WAITS_FOR)
    except ValueError:
        return None      # something else started meanwhile; the next minute looks again
    _auto_update(lambda a: a.update(job=(started or {}).get("id", "")))
    if opening:
        ran(key)
    return started


def may_go_on():
    """Asked before each move of an automatic run: "", or why it stops. It stops if it
    can't tell, since the next look starts it again when it should."""
    try:
        current = settings()
        placement = current.get("placement") or {}
        if placement.get("rebalance", "off") not in ("window", "auto"):
            return "the choice for moving containers back changed"
        with _lock:
            worked = _load()["worked"]
        gone = AUTO.away(read("/api/v1/nodes").get("items", []), worked, sorted(PLACEMODE.standby_of(placement)), ())
        if gone:
            return f"{' and '.join(gone)} no longer {'takes' if len(gone) == 1 else 'take'} work"
        other = sorted({i.get("title") or "Another job" for i in operations.snapshot() if i.get("kind") in WAITS_FOR
                        and i.get("kind") != "container-rebalance" and i.get("status") not in operations.TERMINAL})
    except Exception as error:
        return f"Homestead could not read the cluster ({str(error)[:80]})"
    return f"{other[0]} started" if other else ""


def tick(now=None):
    """One look: hosts that take work again, and - as settings' placement.
    rebalance says - whether moving containers back would even things out,
    and in the maintenance window, doing it. Returns the job started, if any."""
    now = time.time() if now is None else now
    current = settings()
    placement = current.get("placement") or {}
    mode = placement.get("rebalance", "off")
    nodes = read("/api/v1/nodes").get("items", [])
    # The standby taints are kept in step every minute; one that fails waits
    # for the next, and must not stop hosts coming back from being noticed.
    try:
        PLACEMODE.reconcile(nodes, placement)
    except Exception as error:
        print(f"placement: {str(error)[:160]}", flush=True)
    standby = sorted(PLACEMODE.standby_of(placement))
    returned = observe(nodes, now, standby)
    _watch_marks(now)
    if mode == "auto":
        _auto_job(now)
    else:
        _auto_update(lambda auto: (auto.pop("paused", None), auto.pop("job", None)))
    if not returned or mode == "off":
        if mode == "auto":
            _auto_wait([], [], 0, 0, [])
        return None
    if mode == "auto":
        return _auto_tick(current, placement, nodes, standby, returned, now)
    # Not beside other work that moves or restarts apps or hosts: a host
    # uncordoned mid OS rollout is "back", and the next one is about to drain.
    if any(i.get("kind") in WAITS_FOR and i.get("status") not in operations.TERMINAL for i in operations.snapshot()):
        return None
    # A plan reads every pod and volume: worked out again every five minutes.
    if now - _planned[0] >= 300:
        _planned[0] = now
        suggest(plan(avoid=standby)["moves"], now)
    maintenance = current["updates"]["maintenance"]
    key = window_key(maintenance, now)
    if mode != "window" or not key or already_ran(key) or not observe_returned():
        return None
    closes = window(maintenance, now)[1]
    deployments = read("/apis/apps/v1/deployments").get("items", [])
    # An app a tool applies keeps what the tool wrote, a restart's mark included;
    # a new placement is another matter. Flux's apps can move by restarting under
    # Spread by capacity; a person can still move any of them by hand.
    with _lock:
        undone = _load()["undone"]
    left_out, restart = split_gitops(deployments, placement.get("mode", ""), undone)
    back = sorted(returned)
    # A scheduled job is guarded for as long as the run may take - a move can
    # wait MOVE_WAIT for its container - not only until the window closes.
    may_restart = placement.get("mode") == "spread"
    if may_restart:
        # A restart lands where the scheduler says, so not while a copy is rebuilt on the
        # host back. The placement moves go on as they always have.
        volumes, replicas, engines = AUTO.read_copies(read)
        if any(AUTO.copies_left(host, volumes, replicas, engines) for host in back):
            left_out, restart, may_restart = left_out | set(restart), {}, False
    first = plan(sorted(left_out), avoid=standby, restart=restart, back=back, may_restart=may_restart)
    until = max(closes, now + MOVE_WAIT * len(first["moves"]))
    keep = guarded(read("/apis/batch/v1/cronjobs").get("items", []), deployments, now, until)
    chosen = plan(sorted(left_out | set(keep)), avoid=standby, restart=restart, back=back, may_restart=may_restart)
    if not chosen["moves"]:
        ran(key)
        return None
    try:
        # The check and the start in one step, under the job list's lock: an
        # automatic update starting the same minute cannot slip in between.
        started = start(chosen, f" after {', '.join(sorted(returned))} came back", unless=WAITS_FOR)
    except ValueError:
        return None      # something else started meanwhile; the next minute looks again
    ran(key)
    return started


def run(sleep=time.sleep):
    """Every minute on the leader."""
    while True:
        if is_leader():
            try:
                tick()
                beat("recovery", 60, leader_only=True)
            except Exception as error:
                beat("recovery", 60, error, leader_only=True)
                print(f"recovery: {str(error)[:160]}", flush=True)
        sleep(60)
