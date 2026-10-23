"""Schedule clashes: Longhorn recurring jobs that start together on a volume.

Two backups of one volume at once, or a filesystem trim while a backup reads
it, slow each other down and can fail; Longhorn does not stagger them. In the
coming week, a trim or a backup that starts in the same minute as a backup on
a volume they share is a clash, and one that starts within half an hour of it
is close. A snapshot (or a snapshot cleanup) during a backup is fine:
Longhorn takes it in its stride, so it is not reported.

Read by /api/lh/overview, which the Data protection page, the Dashboard's
health advice and the week of schedules (web/js/schedule-week.js) share.
Jobs are plain rows - name, task, cron (five fields, UTC), volumes - so a
schedule that is not a RecurringJob can join them.
"""
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


def clashes(jobs, now=None, days=WEEK_DAYS):
    """Pairs of jobs that start together on a shared volume in the coming
    days: a clash in the same minute, close within CLOSE. One row per pair,
    the earlier job first; clashes first, then by when they first happen."""
    now = time.time() if now is None else now
    end = now + days * 86400
    events = []
    for job in jobs or []:
        kind, volumes = role(job.get("task")), set(job.get("volumes") or [])
        if kind and volumes:
            events.extend((at, job.get("name", ""), kind, volumes, job.get("task", ""))
                          for at in runs(job.get("cron"), now, end))
    events.sort(key=lambda e: (e[0], e[1]))
    rows = {}
    for i, first in enumerate(events):
        for second in events[i + 1:]:
            gap = second[0] - first[0]
            if gap > CLOSE:
                break
            if second[1] == first[1] or "backup" not in (first[2], second[2]):
                continue
            shared = first[3] & second[3]
            if not shared:
                continue
            severity = "clash" if gap < 60 else "close"
            row = rows.setdefault((first[1], second[1], severity), {
                "severity": severity, "jobs": [{"name": first[1], "task": first[4]}, {"name": second[1], "task": second[4]}],
                "volumes": set(), "times": [], "count": 0, "gap_min": gap // 60})
            row["volumes"] |= shared
            row["count"] += 1
            row["gap_min"] = min(row["gap_min"], gap // 60)
            if len(row["times"]) < KEEP_TIMES:
                row["times"].append([first[0], second[0]])
    out = [{**row, "volumes": sorted(row["volumes"])} for row in rows.values()]
    return sorted(out, key=lambda r: (r["severity"] != "clash", r["times"][0][0], r["jobs"][0]["name"]))
