"""Whether each CronJob is doing its job (#739): the result of its last run,
whether a run it was due to start did not happen, and when it runs next.

Schedules lists CronJobs from every namespace, and an app's backup is often
one Homestead did not make. Kubernetes says when a CronJob last started a run
(lastScheduleTime) and last finished one well (lastSuccessfulTime), but not
that the last run failed, nor that a run never started (the controller was
down, the schedule was paused and forgotten). The result comes from the Jobs
the CronJob owns; a missed run from reading its schedule.

The leader's alerts loop turns these into alerts (alert_facts). What could not
be read is "unknown": an alert is neither raised nor cleared on a guess (#620).
Longhorn's recurring jobs - Homestead's own weekly trim among them - are shown
but never alerted here: Longhorn's backups and snapshots have their alert
already (Data protection), and the trim its own (#591).
"""
import calendar
import datetime
import functools
import time
import zoneinfo

import homestead_names as NAMES
import homestead_recovery as CRON
import homestead_self as SELF

# A run is missed once it is this late, or its startingDeadlineSeconds if longer.
GRACE = 15 * 60
# How far ahead the next run is looked for.
LOOK_AHEAD = 8 * 86400
# How far back a due run is looked for: past a yearly schedule's gap.
MAX_REACH = 400 * 86400
# A job that runs more than once an hour fails now and then and recovers by
# itself; it alerts once it has gone this long without a success.
FREQUENT_FAILS_FOR = 30 * 60
# CronJobs whose schedules are read per pass; the rest say "unknown".
MAX_JUDGED = 500
FLUX_LABELS = ("kustomize.toolkit.fluxcd.io/name", "helm.toolkit.fluxcd.io/name")
_firsts = {}
_steps = [0]        # days looked at, for the tests' bound


def _at(text):
    try:
        return calendar.timegm(time.strptime(str(text), "%Y-%m-%dT%H:%M:%SZ"))
    except (TypeError, ValueError):
        return None


def _iso(t):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t))


def owner(cj):
    """homestead (made here), longhorn (a recurring job), flux, or ""."""
    meta = cj.get("metadata") or {}
    if any(r.get("kind") == "RecurringJob" for r in meta.get("ownerReferences") or []):
        return "longhorn"
    if "true" in (NAMES.label_of(meta, "managed"), SELF.old_label(meta, "managed")):
        return "homestead"
    labels = meta.get("labels") or {}
    return "flux" if any(labels.get(k) for k in FLUX_LABELS) else ""


@functools.lru_cache(maxsize=1024)
def _fields(schedule):
    """The schedule's five fields as sets (and whether each day field is *),
    or None when it cannot be read."""
    text = CRON.MACROS.get(str(schedule).strip(), str(schedule).strip())
    parts = text.split()
    if len(parts) != 5:
        return None
    try:
        minute, hour, dom, month = (CRON._field(parts[0], 0, 59), CRON._field(parts[1], 0, 23),
                                    CRON._field(parts[2], 1, 31), CRON._field(parts[3], 1, 12, CRON.MONTHS))
        dow = {d % 7 for d in CRON._field(parts[4], 0, 7, CRON.DAYS)}
    except ValueError:
        return None
    return (tuple(sorted(minute)), tuple(sorted(hour)), frozenset(dom), frozenset(month), frozenset(dow),
            parts[2] in ("*", "?"), parts[4] in ("*", "?"))


def _day(f, d, mo, wd):
    _, _, dom, month, dow, any_dom, any_dow = f
    # Cron's rule: with both day fields restricted, either one will do.
    day = (wd in dow) if any_dom else (d in dom) if any_dow else (d in dom or wd in dow)
    return mo in month and day


def _matcher(schedule):
    """The schedule's test for one minute on its clock, or None when it cannot be read."""
    f = _fields(schedule)
    if f is None:
        return None
    minutes, hours = set(f[0]), set(f[1])

    def matches(m, h, d, mo, wd):
        return m in minutes and h in hours and _day(f, d, mo, wd)
    # Fires more than once an hour on average: more than 24 times a day.
    matches.frequent = len(minutes) * len(hours) > 24
    return matches


def _clock(tz, offset=0):
    """Epoch seconds to (minute, hour, day, month, weekday) on the schedule's clock, or None."""
    zone = _zone(tz)
    if zone is False:
        return None
    if zone is not None:
        def local(t):
            x = datetime.datetime.fromtimestamp(t, zone)
            return x.minute, x.hour, x.day, x.month, (x.weekday() + 1) % 7
        return local

    def fixed(t):
        g = time.gmtime(t + offset)
        return g.tm_min, g.tm_hour, g.tm_mday, g.tm_mon, (g.tm_wday + 1) % 7
    return fixed


def _zone(tz):
    """The zone, None for UTC or no zone (a fixed offset), False when unknown."""
    if not tz or tz in ("UTC", "Etc/UTC"):
        return None
    try:
        return zoneinfo.ZoneInfo(tz)
    except Exception:
        return False


def first_fire(schedule, tz, after, limit, offset=0, cache=True):
    """The first minute at or after `after` the schedule fires, within limit
    seconds; float("inf") when it does not, None when it cannot be read.

    Worked out day by day from the cron fields - a day that cannot match
    costs one step, not 1440 - so a schedule that fires on 29 February only,
    or never, is as cheap as a daily one (security review). Each day's times
    are tried in order and turned into epoch seconds on the schedule's clock;
    a time the clocks skip does not exist, so it does not fire."""
    start = -(-int(after) // 60) * 60
    key = (schedule, tz, offset, start, limit)
    if cache and key in _firsts:
        return _firsts[key]
    f, zone = _fields(schedule), _zone(tz)
    if f is None or zone is False:
        return None
    end = start + int(limit)

    def local_day(t):
        if zone is not None:
            return datetime.datetime.fromtimestamp(t, zone).date()
        return datetime.datetime.fromtimestamp(t + offset, datetime.timezone.utc).date()

    def epochs(day, h, m):
        """The moments a local time happens: none when the clocks skip it, two
        when they go back and it happens twice (both count, as for cron)."""
        if zone is None:
            return [calendar.timegm((day.year, day.month, day.day, h, m, 0)) - offset]
        out = []
        for fold in (0, 1):
            t = int(datetime.datetime(day.year, day.month, day.day, h, m, fold=fold, tzinfo=zone).timestamp())
            back = datetime.datetime.fromtimestamp(t, zone)
            if (back.day, back.hour, back.minute) == (day.day, h, m) and t not in out:
                out.append(t)
        return sorted(out)

    found, day, last_day = float("inf"), local_day(start), local_day(end) + datetime.timedelta(days=1)
    while day <= last_day and found == float("inf"):
        _steps[0] += 1
        if _day(f, day.day, day.month, (day.weekday() + 1) % 7):
            for h in f[1]:
                t = None
                for m in f[0]:
                    t = next((x for x in epochs(day, h, m) if x >= start), None)
                    if t is not None:
                        break
                if t is not None:
                    if t < end:
                        found = t
                    break
            if found == float("inf") and t is not None and t >= end:
                break
        day += datetime.timedelta(days=1)
    if cache:
        if len(_firsts) > 2000:
            _firsts.clear()
        _firsts[key] = found
    return found


def _offsets(schedule, last):
    """Without a zone the controller's clock is unknown: the offsets from UTC
    (whole quarter hours) its last run fits, and the hour either side, as a
    clock change may have come between."""
    matches = _matcher(schedule)
    if matches is None or last is None:
        return []
    fits = [o for o in range(-12 * 3600, 14 * 3600 + 1, 900) if matches(*_clock("", o)(last - last % 60))]
    return sorted({o + shift for o in fits for shift in (-3600, 0, 3600)})


def missed(cj, now):
    """True when a run the schedule was due to start did not happen, False
    when none is overdue, None when it cannot be told."""
    meta, spec, status = cj.get("metadata") or {}, cj.get("spec") or {}, cj.get("status") or {}
    if spec.get("suspend") or owner(cj) == "longhorn":
        return False
    if status.get("active"):
        # A run is going: with concurrencyPolicy Forbid the controller skips a
        # due run until it ends, so a long backup is late, not missed.
        return False
    schedule, tz = spec.get("schedule", ""), spec.get("timeZone") or ""
    last, created = _at(status.get("lastScheduleTime")), _at(meta.get("creationTimestamp"))
    if last is None and created is None:
        return None                # nothing to count from
    # From the last run (or creation): a stable start, so the answer is cached
    # while the last run stands, and a run missed long ago stays missed.
    floor = (now - MAX_REACH) // 86400 * 86400          # moves once a day, not once a pass
    start = max((last + 60) if last is not None else (created or now), floor)
    end = now - max(GRACE, int(spec.get("startingDeadlineSeconds") or 0))
    if _matcher(schedule) is None or _clock(tz) is None:
        return None
    if end <= start:
        return False
    # The first due time after start is asked with a fixed reach, so the
    # answer is cached while the last run stands; end is compared outside.
    reach = MAX_REACH + LOOK_AHEAD
    if tz:
        due = first_fire(schedule, tz, start, reach)
        return due is not None and due < end
    offsets = _offsets(schedule, last)
    if not offsets:
        return None
    # Late only if it is late on every clock its last run fits.
    return all((first_fire(schedule, "", start, reach, o) or float("inf")) < end for o in offsets)


def next_run(cj, now):
    """When it runs next (UTC), or "" when paused, unknown or over LOOK_AHEAD away."""
    spec = cj.get("spec") or {}
    tz = spec.get("timeZone") or ""
    if spec.get("suspend") or not tz:
        return ""
    # Asked anew each pass (its start moves), so it stays out of missed()'s cache.
    due = first_fire(spec.get("schedule", ""), tz, now + 1, LOOK_AHEAD, cache=False)
    return _iso(due) if due not in (None, float("inf")) else ""


def _finished(job):
    for c in (job.get("status") or {}).get("conditions") or []:
        if c.get("status") == "True" and c.get("type") in ("Complete", "Failed"):
            return c
    return None


def health(cj, jobs, now, error="", detail=True, judge=True):
    """What the Schedules row adds for this CronJob; jobs is every Job read
    (None when they could not be read, with error saying why). Without
    detail - a CronJob in another namespace, whose spec its viewers never
    saw - only Kubernetes' reason code is kept, not the condition's message,
    and not the volumes it mounts."""
    meta, spec, status = cj.get("metadata") or {}, cj.get("spec") or {}, cj.get("status") or {}
    pod = ((spec.get("jobTemplate") or {}).get("spec") or {}).get("template", {}).get("spec") or {}
    out = {"ns": meta.get("namespace", ""), "owner": owner(cj), "last_success": status.get("lastSuccessfulTime", ""),
           "claims": sorted({(v.get("persistentVolumeClaim") or {}).get("claimName") for v in pod.get("volumes") or []
                             if (v.get("persistentVolumeClaim") or {}).get("claimName")}),
           "result": "unknown", "result_at": "", "result_reason": "",
           # Past the pass's budget (judge False) a missed run is unknown, never "no".
           "missed": missed(cj, now) if judge else None, "next_run": next_run(cj, now) if judge else "",
           "frequent": getattr(_matcher(spec.get("schedule", "")), "frequent", False)}
    if not detail:
        out.pop("claims")
    if jobs is None:
        # The API's own words (an address, a path) only on Homestead's own rows.
        out["result_reason"] = (f"Its runs could not be read: {error}" if detail else "Its runs could not be read")[:300]
        return out
    # Only the CronJob's own Jobs: its namespace, and an owner reference to it
    # by kind, name and uid. A Job made elsewhere cannot speak for it.
    mine = [j for j in jobs if (j.get("metadata") or {}).get("namespace") == meta.get("namespace")
            and any(r.get("kind") == "CronJob" and r.get("name") == meta.get("name") and r.get("uid") == meta.get("uid")
                    and r.get("controller") for r in (j.get("metadata") or {}).get("ownerReferences") or [])]
    mine.sort(key=lambda j: (j.get("status") or {}).get("startTime") or (j.get("metadata") or {}).get("creationTimestamp") or "")
    done = [(j, _finished(j)) for j in mine if _finished(j)]
    # The newest Job kept is older than the last run: that run's Job is gone
    # (failedJobsHistoryLimit 0, a TTL), so an older result cannot stand in.
    started = lambda j: _at((j.get("status") or {}).get("startTime") or (j.get("metadata") or {}).get("creationTimestamp"))
    last_run = _at(status.get("lastScheduleTime"))
    if mine and last_run is not None and (started(mine[-1]) or 0) < last_run - 60:
        done, mine = [], []
    if done:
        j, c = done[-1]
        ok = c["type"] == "Complete"
        out.update(result="ok" if ok else "failed",
                   result_at=(j.get("status") or {}).get("completionTime") or c.get("lastTransitionTime") or "",
                   result_reason="" if ok else ": ".join(x for x in (c.get("reason"), detail and c.get("message")) if x)[:300])
    if done:
        return out
    if mine or status.get("active"):
        out["result"] = "running"
    elif not status.get("lastScheduleTime"):
        out["result"] = "never"
    elif (_at(status.get("lastSuccessfulTime")) or 0) >= (_at(status.get("lastScheduleTime")) or 0):
        out.update(result="ok", result_at=status.get("lastSuccessfulTime"))
    else:
        out["result_reason"] = "Kubernetes no longer keeps its last run, and that run did not succeed"
    return out


def alert_facts(rows, now):
    """Alerts from Schedules rows: a failed last run (cleared by a success),
    and a run that was due and did not start (cleared by a run)."""
    facts = []
    for row in rows:
        if row.get("suspend") or row.get("owner") == "longhorn":
            continue
        ns, name = row.get("ns", ""), row.get("name", "")
        key, href = f"cronjob:{ns}/{name}", f"/schedules?find={ns}/{name}"
        if row.get("result") not in ("ok", "failed"):
            # Unread, gone (a Job's TTL), running or never run: nothing says the
            # failure ended, so a failure already raised is kept, not cleared.
            facts.append({"unknown_key": key + ":failed"})
        elif row.get("result") == "failed":
            since = _at(row.get("last_success")) or _at(row.get("created")) or 0
            if not row.get("frequent") or now - since >= FREQUENT_FAILS_FOR:
                facts.append({"key": key + ":failed", "category": "degraded", "severity": "degraded",
                              "title": f"Scheduled job {name} failed",
                              # Names and times only: never the run's message or command.
                              "body": f"The last run of {name} in {ns} failed"
                                      + (f" at {row['result_at']}" if row.get("result_at") else "")
                                      + ". Schedules shows why. This alert stays until a run succeeds.",
                              "resolved": f"Scheduled job {name} ran successfully", "href": href,
                              "signals": {"failed": 1}})
        if row.get("missed") is None:
            facts.append({"unknown_key": key + ":missed"})
        elif row.get("missed"):
            facts.append({"key": key + ":missed", "category": "degraded", "severity": "degraded",
                          "title": f"Scheduled job {name} did not run",
                          "body": f"{name} in {ns} was due to run on its schedule and has not started"
                                  + (f" since {row['last']}" if row.get("last") else "")
                                  + ". Check that it is not paused, and the cluster's controller.",
                          "resolved": f"Scheduled job {name} ran", "href": href, "signals": {"missed": 1}})
    return facts
