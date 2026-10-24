"""Schedule clashes: Longhorn recurring jobs that start together on a volume.

Two backups of one volume at once, or a filesystem trim while a backup reads
it, slow each other down and can fail; Longhorn does not stagger them. In the
coming week, a trim or a backup that starts in the same minute as a backup on
a volume they share is a clash, and one that starts within half an hour of it
is close (said only of a pair that never clashes). A snapshot (or a snapshot
cleanup) during a backup is fine: Longhorn takes it in its stride, so it is
not reported.

Read by /api/lh/overview, which the Data protection page, the Dashboard's
health advice and the week of schedules (web/js/schedule-week.js) share.
Jobs are plain rows - name, task, cron (five fields, UTC), volumes - so a
schedule that is not a RecurringJob can join them.
"""
import bisect
import time

WEEK_DAYS = 7
CLOSE = 30 * 60
KEEP_TIMES = 20         # times listed per pair; count says how many there are
MACROS = {"@yearly": "0 0 1 1 *", "@annually": "0 0 1 1 *", "@monthly": "0 0 1 * *",
          "@weekly": "0 0 * * 0", "@daily": "0 0 * * *", "@midnight": "0 0 * * *", "@hourly": "0 * * * *"}
MONTHS = {m: i + 1 for i, m in enumerate("jan feb mar apr may jun jul aug sep oct nov dec".split())}
DAYS = {d: i for i, d in enumerate("sun mon tue wed thu fri sat".split())}


def _field(text, lo, hi, names=None):
    text = text.lower()
    for name, number in (names or {}).items():
        text = text.replace(name, str(number))
    out = set()
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


def runs(cron, start, end):
    """When a five-field cron (UTC) starts in [start, end), as epoch seconds.
    Nothing for a schedule that cannot be read."""
    text = str(cron or "").strip()
    parts = MACROS.get(text.lower(), text).split()
    if len(parts) != 5:
        return []
    try:
        minutes, hours = sorted(_field(parts[0], 0, 59)), sorted(_field(parts[1], 0, 23))
        dom, months = _field(parts[2], 1, 31), _field(parts[3], 1, 12, MONTHS)
        dow = {d % 7 for d in _field(parts[4], 0, 7, DAYS)}
    except ValueError:
        return []
    any_dom, any_dow = parts[2] in ("*", "?"), parts[4] in ("*", "?")
    out = []
    day = int(start) // 86400 * 86400
    while day < end:
        g = time.gmtime(day)
        weekday = (g.tm_wday + 1) % 7
        # Cron's rule: with both day fields restricted, either one will do.
        on = (weekday in dow) if any_dom else (g.tm_mday in dom) if any_dow else (g.tm_mday in dom or weekday in dow)
        if on and g.tm_mon in months:
            out.extend(t for t in (day + h * 3600 + m * 60 for h in hours for m in minutes) if start <= t < end)
        day += 86400
    return out


def role(task):
    task = str(task or "")
    return "backup" if task.startswith("backup") else "trim" if task == "filesystem-trim" else ""


def _meetings(starts, others, strict):
    """(start, the next of others at or after it, gap) for each start that
    another run follows within CLOSE; strict leaves out the same second, so a
    pair's same-minute starts are counted once."""
    find = bisect.bisect_right if strict else bisect.bisect_left
    for at in starts:
        i = find(others, at)
        if i < len(others) and others[i] - at <= CLOSE:
            yield at, others[i], others[i] - at


def clashes(jobs, now=None, days=WEEK_DAYS):
    """Pairs of jobs that start together on a shared volume in the coming
    days: a clash in the same minute, close within CLOSE (only when the pair
    never clashes - every-minute jobs are close to everything). One row per
    pair and severity, the job that starts first leading; clashes first, then
    by when they first happen. Each start is matched to the other job's next
    start by bisection, so frequent jobs stay cheap."""
    now = time.time() if now is None else now
    end = now + days * 86400
    plans = []
    for job in jobs or []:
        kind, volumes = role(job.get("task")), set(job.get("volumes") or [])
        starts = runs(job.get("cron"), now, end) if kind and volumes else []
        if starts:
            plans.append((str(job.get("name", "")), str(job.get("task", "")), kind, volumes, starts))
    tasks = {plan[0]: plan[1] for plan in plans}
    found = {}      # (pair of names, severity) -> {"volumes", "meetings": [(name first, at, name second, at, gap)]}
    for i, a in enumerate(plans):
        for b in plans[i + 1:]:
            shared = a[3] & b[3]
            if a[0] == b[0] or "backup" not in (a[2], b[2]) or not shared:
                continue
            meetings = [(a[0], x, b[0], y, gap) for x, y, gap in _meetings(a[4], b[4], False)]
            meetings += [(b[0], y, a[0], x, gap) for y, x, gap in _meetings(b[4], a[4], True)]
            for meeting in meetings:
                severity = "clash" if meeting[4] < 60 else "close"
                row = found.setdefault((frozenset((a[0], b[0])), severity), {"volumes": set(), "meetings": []})
                row["volumes"] |= shared
                row["meetings"].append(meeting)
    out = []
    for (names, severity), row in found.items():
        if severity == "close" and (names, "clash") in found:
            continue
        meetings = sorted(row["meetings"], key=lambda m: (m[1], m[0]))
        lead, other = meetings[0][0], meetings[0][2]
        out.append({"severity": severity,
                    "jobs": [{"name": lead, "task": tasks[lead]}, {"name": other, "task": tasks[other]}],
                    "volumes": sorted(row["volumes"]),
                    "times": [[m[1], m[3]] if m[0] == lead else [m[3], m[1]] for m in meetings[:KEEP_TIMES]],
                    "count": len(meetings), "gap_min": min(m[4] for m in meetings) // 60})
    return sorted(out, key=lambda r: (r["severity"] != "clash", min(r["times"][0]), r["jobs"][0]["name"]))
