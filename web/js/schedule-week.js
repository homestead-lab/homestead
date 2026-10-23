/* Schedules › This week: everything Homestead knows is scheduled in the next
   seven days, as one list grouped by day in the reader's time zone.

   It reads what the pages already read: Longhorn's recurring jobs and the
   clashes the server found between them (/api/lh/overview,
   homestead_schedule_clashes), CronJobs (/api/schedules), power schedules
   (/api/power-schedules), the host OS update window (/api/os-updates) and the
   maintenance window apps update in (/api/settings). Each one that cannot be
   read is named, so a quiet week is never a week that was not read (#620).
   A job that runs more than six times a day is one "runs all day" line. */
(function () {
  const DAY = 86400000, WEEK = 7 * DAY, FREQUENT = 6;
  const DAY_NAMES = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"];
  const LABELS = { lh: "Longhorn's recurring jobs", crons: "CronJobs", power: "power schedules",
    os: "host OS update settings", settings: "the maintenance window", workloads: "apps' update settings" };
  const TASK_WORDS = { backup: "backup", "backup-force-create": "backup", snapshot: "snapshot", "snapshot-force-create": "snapshot",
    "snapshot-cleanup": "snapshot cleanup", "snapshot-delete": "snapshot delete", "filesystem-trim": "trim" };
  const pad = n => String(n).padStart(2, "0");
  const isUtc = tz => !tz || /^(Etc\/)?(UTC|GMT)$/.test(tz);
  // An answer, as opposed to a read that failed or came back as an error.
  const answered = value => !!value && typeof value === "object" && !value.failed && !value.error;

  /* Minutes a zone is ahead of UTC at ms, or null for a zone this browser does not know. */
  function offsetMin(tz, ms) {
    try {
      const parts = Object.fromEntries(new Intl.DateTimeFormat("en-US", { timeZone: tz, hourCycle: "h23", year: "numeric",
        month: "numeric", day: "numeric", hour: "numeric", minute: "numeric" }).formatToParts(new Date(ms)).map(p => [p.type, p.value]));
      const wall = Date.UTC(+parts.year, +parts.month - 1, +parts.day, +parts.hour % 24, +parts.minute);
      return Math.round((wall - Math.floor(ms / 60000) * 60000) / 60000);
    } catch { return null; }
  }

  /* The moment a wall-clock time in tz happens; the saved offset when tz is
     unknown. A time the autumn change repeats is its first occurrence, as
     Python's zoneinfo (power schedules, OS updates) reads it; one the spring
     change skips takes the offset from before the change. */
  function wallToMs(y, mo, d, h, mi, tz, fallbackMin = 0) {
    const wall = Date.UTC(y, mo, d, h, mi);
    if (isUtc(tz) && !fallbackMin) return wall;
    const before = tz ? offsetMin(tz, wall - DAY) : null;
    if (before === null) return wall - fallbackMin * 60000;
    const after = offsetMin(tz, wall + DAY);
    const fits = [before, after].map(off => wall - off * 60000).filter(t => offsetMin(tz, t) * 60000 === wall - t);
    return fits.length ? Math.min(...fits) : wall - before * 60000;
  }

  /* A cron as cron.js reads it: Kubernetes and Longhorn also take @daily and
     the like, month and day names, and ? for "any day". "" when it is not
     five fields after that (@every, or something malformed). */
  const MACROS = { "@yearly": "0 0 1 1 *", "@annually": "0 0 1 1 *", "@monthly": "0 0 1 * *", "@weekly": "0 0 * * 0",
    "@daily": "0 0 * * *", "@midnight": "0 0 * * *", "@hourly": "0 * * * *" };
  const MONTHS = "jan feb mar apr may jun jul aug sep oct nov dec".split(" "), WEEKDAYS = "sun mon tue wed thu fri sat".split(" ");
  function normalise(cron) {
    const text = String(cron || "").trim().toLowerCase();
    const parts = (MACROS[text] || text).split(/\s+/);
    if (parts.length !== 5) return "";
    const named = (field, names, base) => field.replace(/[a-z]{3}/g, word => names.includes(word) ? String(names.indexOf(word) + base) : word);
    parts[2] = parts[2].replace(/^\?$/, "*");
    parts[3] = named(parts[3], MONTHS, 1);
    parts[4] = named(parts[4].replace(/^\?$/, "*"), WEEKDAYS, 0);
    const out = parts.join(" ");
    return CRON.parse(out) ? out : "";
  }

  /* Every run of a (normalised) cron in [from, until), read on tz's clock (UTC without one). */
  function cronRuns(cron, tz, from, until) {
    const utc = isUtc(tz), margin = utc ? 0 : 2 * DAY, end = until.getTime() + margin, out = [];
    let at = new Date(from.getTime() - margin - 1);   // CRON.next is after, so a run at from counts
    for (let guard = 0; guard < 100; guard++) {
      const batch = CRON.next(cron, at, 500);
      for (const wall of batch) {
        if (wall.getTime() >= end) return out;
        const t = utc ? wall.getTime() : wallToMs(wall.getUTCFullYear(), wall.getUTCMonth(), wall.getUTCDate(), wall.getUTCHours(), wall.getUTCMinutes(), tz);
        if (t >= from.getTime() && t < until.getTime()) out.push(new Date(t));
      }
      if (batch.length < 500) return out;
      at = batch[batch.length - 1];
    }
    return out;
  }

  /* Each calendar date around the week, Monday 0, for weekly wall-clock schedules. */
  function dates(from) {
    const first = Date.UTC(from.getUTCFullYear(), from.getUTCMonth(), from.getUTCDate()) - DAY;
    return [...Array(10)].map((_, i) => { const d = new Date(first + i * DAY);
      return { y: d.getUTCFullYear(), mo: d.getUTCMonth(), d: d.getUTCDate(), weekday: (d.getUTCDay() + 6) % 7 }; });
  }
  function weekly(from, until, days, hhmm, tz, fallbackMin) {
    const [h, mi] = String(hhmm).split(":").map(Number);
    if (!(h >= 0 && h < 24 && mi >= 0 && mi < 60)) return [];
    return dates(from).filter(x => days.includes(x.weekday)).map(x => new Date(wallToMs(x.y, x.mo, x.d, h, mi, tz, fallbackMin)))
      .filter(at => at >= from && at < until);
  }

  /* Everything that runs from `from` for a week. Each source is its answer,
     {failed: message} when the read failed, or null/undefined when it does not apply. */
  function collect(sources, from) {
    const until = new Date(from.getTime() + WEEK), items = [], frequent = [], unread = [], unreadable = [];
    const add = (base, times) => {
      if (times.length > FREQUENT * 7) frequent.push({ ...base, perDay: Math.round(times.length / 7), clash: "" });
      else times.forEach(at => items.push({ ...base, at, clash: "" }));
    };
    // A cron schedule this page cannot read is named, never left out quietly (#620).
    const addCron = (base, cron, tz) => {
      const text = normalise(cron);
      if (text) add(base, cronRuns(text, tz, from, until)); else unreadable.push(base.name);
    };
    const read = (key, valid, use) => {
      const value = sources[key];
      if (value === null || value === undefined) return;
      if (valid(value)) use(value); else unread.push(LABELS[key]);
    };
    let clashes = [];
    read("lh", v => answered(v) && Array.isArray(v.jobs), lh => {
      clashes = Array.isArray(lh.clashes) ? lh.clashes : [];
      lh.jobs.forEach(j => addCron({ kind: "Longhorn", name: j.name, what: TASK_WORDS[j.task] || j.task || "job" }, j.cron, ""));
    });
    read("crons", Array.isArray, crons => crons.filter(c => !c.suspend)
      .forEach(c => addCron({ kind: "CronJob", name: c.name, what: c.namespace ? `job in ${c.namespace}` : "job" }, c.schedule, c.time_zone || "")));
    read("power", v => answered(v) && Array.isArray(v.items), power => power.items.forEach(p => {
      const base = { kind: "Power", name: p.name };
      const s = p.schedule;
      if (s && Array.isArray(s.days)) {
        for (const [action, what] of [["stop", "stops"], ["start", "starts"]])
          if (s[action]) add({ ...base, what }, weekly(from, until, s.days, s[action], s.tz || "UTC"));
      } else {
        // No schedule in the answer: the next few runs the server worked out.
        for (const n of p.next || []) {
          const at = new Date(n.at * 1000);
          if (at >= from && at < until) items.push({ ...base, what: n.action === "stop" ? "stops" : "starts", at, clash: "" });
        }
      }
    }));
    read("os", v => answered(v) && "applies" in v, os => {
      const s = os.settings?.schedule;
      if (!os.applies || !s?.enabled) return;
      const days = (s.days || []).map(d => DAY_NAMES.indexOf(d)).filter(d => d >= 0);
      add({ kind: "OS updates", name: "Host OS updates", what: "one host at a time" },
        weekly(from, until, days, `${s.hour}:00`, s.tz || "", Number(s.offset_min) || 0));
    });
    read("settings", v => answered(v) && v.updates && typeof v.updates === "object", settings => {
      const m = settings.updates.maintenance;
      // Apps update themselves in the window only when set to (homestead_autoupdate), never under notify only.
      let apps = 0;
      if (settings.updates.policy !== "notify_only") read("workloads", Array.isArray, list => { apps = list.filter(w => w.update_mode === "auto").length; });
      const uses = [apps && `${apps} app${apps === 1 ? "" : "s"} set to update ${apps === 1 ? "itself" : "themselves"}`,
        settings.placement?.rebalance === "window" && "Balance hosts"].filter(Boolean);
      const [h, mi] = String(m?.start).split(":").map(Number);
      if (!m || !Array.isArray(m.days) || !uses.length || !(h >= 0 && mi >= 0)) return;
      const close = (h * 60 + mi + Number(m.duration_minutes || 0)) % 1440;
      add({ kind: "Maintenance", name: "Maintenance window", what: `${m.start}–${pad(Math.floor(close / 60))}:${pad(close % 60)} UTC: ${uses.join(" and ")}` },
        weekly(from, until, m.days, m.start, "UTC"));
    });
    // The two runs of each pair the server found, marked; a clash outranks close.
    const marks = new Map();
    for (const c of clashes) for (const pair of c.times || []) pair.forEach((t, i) => {
      const key = `${c.jobs?.[i]?.name}@${t}`;
      if (marks.get(key) !== "clash") marks.set(key, c.severity);
    });
    for (const item of items) if (item.kind === "Longhorn") item.clash = marks.get(`${item.name}@${item.at.getTime() / 1000}`) || "";
    // A collapsed job has no single run to mark: it carries its worst pair.
    for (const f of frequent) if (f.kind === "Longhorn") {
      const named = clashes.filter(c => (c.jobs || []).some(j => j.name === f.name));
      f.clash = named.some(c => c.severity === "clash") ? "clash" : named.length ? "close" : "";
    }
    items.sort((a, b) => a.at - b.at || a.name.localeCompare(b.name));
    return { items, frequent, unread, unreadable, clashes, until, checked: answered(sources.lh) && Array.isArray(sources.lh.jobs) };
  }

  /* The items by local day, from today to the day the week ends on. */
  function group(items, from, until = new Date(from.getTime() + WEEK)) {
    const out = [];
    for (let start = new Date(from.getFullYear(), from.getMonth(), from.getDate()); start < until;) {
      const next = new Date(start.getFullYear(), start.getMonth(), start.getDate() + 1);
      out.push({ date: start, items: items.filter(i => i.at >= start && i.at < next) });
      start = next;
    }
    return out;
  }

  // Times are written by core.js (#503): fmtClock, fmtNext, fmtWeekday.
  function dayTitle(date, index) {
    const label = fmtWeekday(date);
    return index === 0 ? `Today, ${label}` : index === 1 ? `Tomorrow, ${label}` : label;
  }

  function clashLine(c, volumeName) {
    const [a, b] = c.jobs || [], first = new Date(((c.times || [])[0] || [0])[0] * 1000);
    const when = c.severity === "clash" ? "start in the same minute" : `start ${c.gap_min} minutes apart`;
    const more = c.count > 1 ? `, and ${c.count - 1} more time${c.count === 2 ? "" : "s"} this week` : "";
    return `<li>${UI.chip(c.severity === "clash" ? "same minute" : "close", c.severity === "clash" ? "warn" : "neutral", { size: "slim" })}
      <strong>${esc(a?.name)}</strong> (${esc(TASK_WORDS[a?.task] || a?.task)}) and <strong>${esc(b?.name)}</strong> (${esc(TASK_WORDS[b?.task] || b?.task)})
      ${when} on ${esc((c.volumes || []).map(volumeName).join(", "))}: ${fmtNext(first)}${more}</li>`;
  }

  function summary(week, volumeName) {
    if (!week.checked) return "";
    const real = week.clashes.filter(c => c.severity === "clash"), close = week.clashes.filter(c => c.severity !== "clash");
    const closeList = close.length ? `<ul class="ui-list week-clashes">${close.map(c => clashLine(c, volumeName)).join("")}</ul>` : "";
    if (!real.length) return `<div class="card flat week-summary"><b>No clashes this week</b>
      <div class="ui-help">No trim or backup starts with a backup on the same volume${close.length ? "; these start close together" : ""}.</div>${closeList}</div>`;
    return UI.callout("warn", `${real.length} clash${real.length === 1 ? "" : "es"} this week`,
      `<ul class="ui-list week-clashes">${real.map(c => clashLine(c, volumeName)).join("")}</ul>${closeList}
      <p>Two backups of a volume at once, or a trim while it is backed up, slow each other down and can fail. Move one of them in Data protection.</p>`);
  }

  // One run, or (with time given) a job that runs all day; the clash chip says it in words, not only colour.
  function row(i, time = fmtClock(i.at)) {
    return `<div class="week-row"><span class="week-time mono">${esc(time)}</span>
      <span class="week-name"><b>${esc(i.name)}</b>${i.what ? ` <span class="dim">${esc(i.what)}</span>` : ""}</span>
      <span class="week-chips">${UI.chip(i.kind, "neutral", { size: "slim" })}${i.clash ? UI.chip(i.clash === "clash" ? "clash" : "close", i.clash === "clash" ? "warn" : "neutral", { size: "slim" }) : ""}</span></div>`;
  }

  function missing(week) {
    const lines = [week.unread.length && `Could not read ${week.unread.join(", ")}: those schedules are missing from this week. Reload to try again.`,
      week.unreadable.length && `Could not read the schedule of ${week.unreadable.join(", ")}, so ${week.unreadable.length === 1 ? "it is" : "they are"} not in this list.`].filter(Boolean);
    return lines.length ? UI.callout("warn", "Some schedules are missing", lines.map(line => `<p>${esc(line)}</p>`).join("")) : "";
  }

  window.viewScheduleWeek = async function () {
    const failed = e => ({ failed: e?.message || "could not be read" });
    const [lh, crons, power, os, settings, workloads] = await Promise.all([
      STATE.platform?.longhorn === false ? null : api("/api/lh/overview").catch(failed),
      api("/api/schedules").catch(failed), api("/api/power-schedules").catch(failed),
      api("/api/os-updates").catch(failed), api("/api/settings").catch(failed), api("/api/workloads").catch(failed)]);
    const from = new Date(), week = collect({ lh, crons, power, os, settings, workloads }, from);
    const pvc = new Map((answered(lh) && Array.isArray(lh.volumes) ? lh.volumes : []).map(v => [v.name, v.pvc || v.name]));
    const volumeName = name => pvc.get(name) || name;
    const days = group(week.items, from, week.until);
    paint(`${UI.pageHeader("This week", `Everything scheduled from now until ${fmtNext(week.until)}, in your time zone (${esc(yourZone())})`,
        UI.button("Schedules", "go('schedules')"))}
      ${missing(week)}
      ${summary(week, volumeName)}
      ${week.frequent.length ? `<div class="card flat week-frequent">${UI.section("Runs all day", week.frequent.map(f =>
        row({ ...f, what: `${f.what}, about ${f.perDay} times a day` }, `${f.perDay}×`)).join(""))}</div>` : ""}
      <div class="card flat week-days">${days.map((day, index) => UI.section(dayTitle(day.date, index),
        day.items.length ? day.items.map(i => row(i)).join("") : `<div class="ui-help">Nothing scheduled</div>`)).join("")}</div>`);
  };

  const api_ = { offsetMin, wallToMs, collect, group };
  if (typeof window !== "undefined") window.ScheduleWeek = api_;
  if (typeof module !== "undefined") module.exports = api_;
})();
