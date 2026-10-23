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
    os: "host OS update settings", settings: "the maintenance window" };
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

  /* The moment a wall-clock time in tz happens; the saved offset when tz is unknown. */
  function wallToMs(y, mo, d, h, mi, tz, fallbackMin = 0) {
    const wall = Date.UTC(y, mo, d, h, mi);
    if (isUtc(tz) && !fallbackMin) return wall;
    const off = tz ? offsetMin(tz, wall) : null;
    if (off === null) return wall - fallbackMin * 60000;
    const again = offsetMin(tz, wall - off * 60000);
    return wall - (again ?? off) * 60000;
  }

  /* Every run of a cron in [from, until), read on tz's clock (UTC without one). */
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
    const until = new Date(from.getTime() + WEEK), items = [], frequent = [], unread = [];
    const add = (base, times) => {
      if (times.length > FREQUENT * 7) frequent.push({ ...base, perDay: Math.round(times.length / 7) });
      else times.forEach(at => items.push({ ...base, at, clash: "" }));
    };
    const read = (key, valid, use) => {
      const value = sources[key];
      if (value === null || value === undefined) return;
      if (valid(value)) use(value); else unread.push(LABELS[key]);
    };
    let clashes = [];
    read("lh", v => answered(v) && Array.isArray(v.jobs), lh => {
      clashes = Array.isArray(lh.clashes) ? lh.clashes : [];
      lh.jobs.forEach(j => add({ kind: "Longhorn", name: j.name, what: TASK_WORDS[j.task] || j.task || "job" }, cronRuns(j.cron, "", from, until)));
    });
    read("crons", Array.isArray, crons => crons.filter(c => !c.suspend)
      .forEach(c => add({ kind: "CronJob", name: c.name, what: c.namespace ? `job in ${c.namespace}` : "job" }, cronRuns(c.schedule, c.time_zone || "", from, until))));
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
      const uses = [settings.updates.policy !== "notify_only" && "apps that update themselves",
        settings.placement?.rebalance === "window" && "Balance hosts"].filter(Boolean);
      if (!m || !Array.isArray(m.days) || !uses.length) return;
      const [h, mi] = String(m.start).split(":").map(Number), close = (h * 60 + mi + Number(m.duration_minutes || 0)) % 1440;
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
    items.sort((a, b) => a.at - b.at || a.name.localeCompare(b.name));
    return { items, frequent, unread, clashes, until, checked: answered(sources.lh) && Array.isArray(sources.lh.jobs) };
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

  function row(i) {
    return `<div class="week-row${i.clash === "clash" ? " clash" : ""}"><span class="week-time mono">${esc(fmtClock(i.at))}</span>
      <span class="week-name"><b>${esc(i.name)}</b>${i.what ? ` <span class="dim">${esc(i.what)}</span>` : ""}</span>
      <span class="week-chips">${UI.chip(i.kind, "neutral", { size: "slim" })}${i.clash ? UI.chip(i.clash === "clash" ? "clash" : "close", i.clash === "clash" ? "warn" : "neutral", { size: "slim" }) : ""}</span></div>`;
  }

  window.viewScheduleWeek = async function () {
    const failed = e => ({ failed: e?.message || "could not be read" });
    const [lh, crons, power, os, settings] = await Promise.all([
      STATE.platform?.longhorn === false ? null : api("/api/lh/overview").catch(failed),
      api("/api/schedules").catch(failed), api("/api/power-schedules").catch(failed),
      api("/api/os-updates").catch(failed), api("/api/settings").catch(failed)]);
    const from = new Date(), week = collect({ lh, crons, power, os, settings }, from);
    const pvc = new Map((answered(lh) && Array.isArray(lh.volumes) ? lh.volumes : []).map(v => [v.name, v.pvc || v.name]));
    const volumeName = name => pvc.get(name) || name;
    const days = group(week.items, from, week.until);
    paint(`${UI.pageHeader("This week", `Everything scheduled from now until ${fmtNext(week.until)}, in your time zone (${esc(yourZone())})`,
        UI.button("Schedules", "go('schedules')"))}
      ${week.unread.length ? UI.callout("warn", `Could not read ${week.unread.join(", ")}`, "<p>Those schedules are missing from this week. Reload to try again.</p>") : ""}
      ${summary(week, volumeName)}
      ${week.frequent.length ? `<div class="card flat week-frequent">${UI.section("Runs all day", week.frequent.map(f =>
        `<div class="week-row"><span class="week-time mono">${esc(f.perDay)}×</span><span class="week-name"><b>${esc(f.name)}</b> <span class="dim">${esc(f.what)}, about ${esc(f.perDay)} times a day</span></span><span class="week-chips">${UI.chip(f.kind, "neutral", { size: "slim" })}</span></div>`).join(""))}</div>` : ""}
      <div class="card flat week-days">${days.map((day, index) => UI.section(dayTitle(day.date, index),
        day.items.length ? day.items.map(row).join("") : `<div class="ui-help">Nothing scheduled</div>`)).join("")}</div>`);
  };

  const api_ = { offsetMin, wallToMs, collect, group };
  if (typeof window !== "undefined") window.ScheduleWeek = api_;
  if (typeof module !== "undefined") module.exports = api_;
})();
