/* Schedules › This week: everything Homestead knows is scheduled in the next
   seven days, as one list grouped by day in the reader's time zone.

   It reads what the pages already read: Longhorn's recurring jobs, Homestead's
   own weekly trim, and the clashes the server found between them (/api/lh/overview,
   homestead_schedule_clashes), CronJobs (/api/schedules), power schedules
   (/api/power-schedules), the host OS update window (/api/os-updates) and the
   maintenance window apps update in (/api/settings, with /api/workloads for
   which apps update themselves and what they claim, and /api/volumes for
   which hosts hold a volume's copies). Each one that cannot be
   read is named, so a quiet week is never a week that was not read (#620).
   A job that runs more than six times a day is one "runs all day" line. */
(function () {
  const DAY = 86400000, WEEK = 7 * DAY, FREQUENT = 6;
  const DAY_NAMES = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"];
  const LABELS = { lh: "Longhorn's recurring jobs and Homestead's weekly trim", crons: "CronJobs", power: "power schedules",
    os: "host OS update settings", settings: "the maintenance window", workloads: "the apps list", copies: "volume copies" };
  const TASK_WORDS = { backup: "backup", "backup-force-create": "backup", snapshot: "snapshot", "snapshot-force-create": "snapshot",
    "snapshot-cleanup": "snapshot cleanup", "snapshot-delete": "snapshot delete", "filesystem-trim": "trim" };
  /* The kinds on the bar (#919), as Timeline.KINDS words them: [one, many, label].
     System is what Homestead and Longhorn run for themselves. */
  const KINDS = { backup: ["backup", "backups", "Backups"], snapshot: ["snapshot", "snapshots", "Snapshots"], trim: ["trim", "trims", "Trims"],
    cron: ["CronJob", "CronJobs", "CronJobs"], power: ["power schedule", "power schedules", "Power"], os: ["OS update", "OS updates", "OS updates"],
    update: ["app update", "app updates", "App updates"], system: ["system job", "system jobs", "System"] };
  const STOP_UNCHECKED = "whether an app's power stop falls in its backup was not checked";
  const WINDOW_UNKNOWN = "the maintenance window may be missing: whether an app updates itself in it is not known";
  const taskType = task => /^backup/.test(task || "") ? "backup" : /^snapshot/.test(task || "") ? "snapshot" : task === "filesystem-trim" ? "trim" : "system";
  /* The app ("ns/name") a job's claims belong to: the one app in its namespace
     that mounts any of them. None when no app or more than one does - never a guess. */
  function appOf(apps, ns, claims) {
    if (!ns || !Array.isArray(claims) || !claims.length) return "";
    const found = new Set((apps || []).filter(w => w.ns === ns && (w.claims || []).some(c => claims.includes(c))).map(w => `${w.ns}/${w.name}`));
    return found.size === 1 ? [...found][0] : "";
  }
  const pad = n => String(n).padStart(2, "0");
  const isUtc = tz => !tz || /^(Etc\/)?(UTC|GMT)$/.test(tz);
  // An answer, as opposed to a read that failed or came back as an error.
  const answered = value => !!value && typeof value === "object" && !value.failed && !value.error;
  // Longhorn's overview with its jobs read: jobs_unread says they could not be (#927).
  const lhRead = value => answered(value) && Array.isArray(value.jobs) && !value.jobs_unread;

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

  /* How long a backup runs: its last run, from start to success, when Longhorn
     recorded one under a day; else half an hour (CLOSE in homestead_schedule_clashes). */
  function runLength(job) {
    const took = Date.parse(job.last_success) - Date.parse(job.last_run);
    return took > 0 && took < DAY ? took : 30 * 60000;
  }
  const names = list => list.length > 1 ? `${list.slice(0, -1).join(", ")} and ${list.at(-1)}` : list[0];

  /* Everything that runs from `from` for a week. Each source is its answer,
     {failed: message} when the read failed, or null/undefined when it does not apply. */
  function collect(sources, from) {
    const until = new Date(from.getTime() + WEEK), items = [], frequent = [], unread = [], unreadable = [], unnamed = [];
    let windowUnknown = false;
    const add = (base, times) => {
      if (times.length > FREQUENT * 7) frequent.push({ ...base, perDay: Math.round(times.length / 7), clash: "" });
      else times.forEach(at => items.push({ ...base, at, clash: "" }));
      return times;
    };
    // A cron schedule this page cannot read is named, never left out quietly (#620).
    const addCron = (base, cron, tz) => {
      const text = normalise(cron);
      if (text) return add(base, cronRuns(text, tz, from, until));
      unreadable.push(base.name);
      return [];
    };
    const read = (key, valid, use) => {
      const value = sources[key];
      if (value === null || value === undefined) return;
      if (valid(value)) use(value); else unread.push(LABELS[key]);
    };
    // Read when first needed, and named once if it could not be. The apps and the
    // volumes' copies only name what a run touches (review, #919): unread, the runs
    // are all there, in unnamed, not unread.
    const lazy = (key, valid) => { let done = false, out = null;
      return () => { if (!done) { done = true; const value = sources[key];
        if (value !== null && value !== undefined) { if (valid(value)) out = value; else unnamed.push(LABELS[key]); } } return out; }; };
    const apps = lazy("workloads", Array.isArray), copies = lazy("copies", Array.isArray);
    let clashes = [], restarts = true;
    const backups = [], stops = [], windows = [], volumeOf = new Map();
    // Homestead's own jobs on the volumes (its weekly trim, #927) are named
    // as Homestead's; a clash's job is Longhorn's unless it is one of them.
    const ours = new Set();
    // A Longhorn job's app: the one app all its volumes belong to, else none.
    // Its host: the one host holding every copy of its volumes, else none.
    const volumeInfo = new Map();
    const volumeApp = names => { const found = new Set(names.map(name => { const v = volumeInfo.get(name); return v ? appOf(apps(), v.namespace, [v.pvc]) : ""; }));
      return found.size === 1 ? [...found][0] : ""; };
    const volumeHost = names => {
      const list = copies(), hosts = new Set();
      if (!list) return "";
      for (const name of names) {
        const v = list.find(row => row.name === name);
        if (!v || !(v.copies || []).length) return "";
        v.copies.forEach(c => hosts.add(c.node || ""));
      }
      return hosts.size === 1 && !hosts.has("") ? [...hosts][0] : "";
    };
    const touches = j => { const names = j.volumes || [];
      return { app: names.length ? volumeApp(names) : "", host: names.length ? volumeHost(names) : "", href: "/data-protection" }; };
    read("lh", lhRead, lh => {
      clashes = Array.isArray(lh.clashes) ? lh.clashes : [];
      (lh.volumes || []).forEach(v => { volumeOf.set(`${v.namespace}/${v.pvc}`, v.name); volumeInfo.set(v.name, v); });
      lh.jobs.forEach(j => {
        const times = addCron({ kind: "Longhorn", type: taskType(j.task), name: j.name, what: TASK_WORDS[j.task] || j.task || "job", ...touches(j) }, j.cron, "");
        if (String(j.task || "").startsWith("backup")) backups.push({ name: j.name, volumes: new Set(j.volumes || []), times, length: runLength(j) });
      });
      // Homestead's own maintenance on the volumes is System (#919).
      (Array.isArray(lh.homestead_jobs) ? lh.homestead_jobs : []).forEach(j => {
        ours.add(j.name);
        addCron({ kind: "Homestead", type: "system", name: j.name, what: TASK_WORDS[j.task] || j.task || "job", ...touches(j) }, j.cron, "");
      });
    });
    const jobKind = name => ours.has(name) ? "Homestead" : "Longhorn";
    // The CronJob Longhorn keeps for a recurring job is that job, listed above once Longhorn was read.
    read("crons", Array.isArray, crons => crons.filter(c => !c.suspend && !(c.owner === "longhorn" && lhRead(sources.lh))).forEach(c => {
      const ns = c.ns || c.namespace || "";
      addCron({ kind: "CronJob", type: c.owner === "longhorn" ? "system" : "cron", name: c.name, ns, what: ns ? `job in ${ns}` : "job",
        app: c.claims?.length ? appOf(apps(), ns, c.claims) : "", host: "",
        // Another namespace's CronJob comes without its claims (#739): its app cannot be seen, not "none".
        unseen: !!ns && !("claims" in c), href: `/schedules?find=${ns}/${c.name}` }, c.schedule, c.time_zone || "");
    }));
    read("power", v => answered(v) && Array.isArray(v.items), power => power.items.forEach(p => {
      const base = { kind: "Power", type: "power", name: p.name, ns: p.ns || "", app: p.kind === "app" && p.ns ? `${p.ns}/${p.name}` : "", host: "",
        href: p.kind === "vm" ? `/vms?find=${p.name}` : `/containers?find=${p.name}` };
      const s = p.schedule;
      let stopped = [];
      if (s && Array.isArray(s.days)) {
        for (const [action, what] of [["stop", "stops"], ["start", "starts"]]) {
          const times = s[action] ? add({ ...base, what }, weekly(from, until, s.days, s[action], s.tz || "UTC")) : [];
          if (action === "stop") stopped = times;
        }
      } else {
        // No schedule in the answer: the next few runs the server worked out.
        for (const n of p.next || []) {
          const at = new Date(n.at * 1000);
          if (at < from || at >= until) continue;
          items.push({ ...base, what: n.action === "stop" ? "stops" : "starts", at, clash: "" });
          if (n.action === "stop") stopped.push(at);
        }
      }
      if (p.kind === "app" && stopped.length) stops.push({ ns: p.ns, name: p.name, times: stopped });
    }));
    read("os", v => answered(v) && "applies" in v, os => {
      const s = os.settings?.schedule;
      if (!os.applies || !s?.enabled) return;
      restarts = os.settings?.reboot !== "never";
      const days = (s.days || []).map(d => DAY_NAMES.indexOf(d)).filter(d => d >= 0);
      windows.push(...add({ kind: "OS updates", type: "os", name: "Host OS updates", what: "one host at a time", app: "", host: "", href: "/settings?tab=updates" },
        weekly(from, until, days, `${s.hour}:00`, s.tz || "", Number(s.offset_min) || 0)));
    });
    read("settings", v => answered(v) && v.updates && typeof v.updates === "object", settings => {
      const m = settings.updates.maintenance;
      // Apps update themselves in the window only when set to (homestead_autoupdate), never under notify only.
      const updating = settings.updates.policy !== "notify_only" ? (apps() || []).filter(w => w.update_mode === "auto") : [];
      // Without the apps, a window set for apps to update in may be missing: whether
      // an app updates itself in it is not known. One used to balance hosts is listed anyway.
      const auto = updating.length;
      const uses = [auto && `${auto} app${auto === 1 ? "" : "s"} set to update ${auto === 1 ? "itself" : "themselves"}`,
        ["window", "auto"].includes(settings.placement?.rebalance) && "Balance hosts"].filter(Boolean);
      const [h, mi] = String(m?.start).split(":").map(Number);
      const valid = m && Array.isArray(m.days) && m.days.length > 0 && h >= 0 && mi >= 0;
      if (valid && settings.updates.policy !== "notify_only" && sources.workloads != null && !apps() && settings.placement?.rebalance !== "window") windowUnknown = true;
      if (!valid || !uses.length) return;
      const close = (h * 60 + mi + Number(m.duration_minutes || 0)) % 1440;
      // App updates when apps update themselves in it (each one is matched by
      // the App menu); used only to balance hosts it is Homestead's own, System.
      const names = updating.map(w => `${w.ns}/${w.name}`);
      add({ kind: "Maintenance", type: auto ? "update" : "system", name: "Maintenance window",
        what: `${m.start}–${pad(Math.floor(close / 60))}:${pad(close % 60)} UTC: ${uses.join(" and ")}`,
        app: names.length === 1 ? names[0] : "", apps: names, host: "", href: "/settings?tab=updates" },
        weekly(from, until, m.days, m.start, "UTC"));
    });
    const overlaps = overlapsOf(backups, stops, restarts ? windows : [], apps, copies, volumeOf);
    // What the unread sources that only name things cost, one list each (re-review, #919).
    const effects = {};
    if (unnamed.includes(LABELS.workloads)) effects[LABELS.workloads] = ["no run names its app",
      ...(stops.length && backups.length ? [STOP_UNCHECKED] : []), ...(windowUnknown ? [WINDOW_UNKNOWN] : [])];
    if (unnamed.includes(LABELS.copies)) effects[LABELS.copies] = ["no run names its host"];
    // The two runs of each pair the server found, marked; a clash outranks close.
    const marks = new Map(), mark = (key, severity) => { if (marks.get(key) !== "clash") marks.set(key, severity); };
    // Keys: kind, namespace and name at the second; a power start is never marked.
    const key = (kind, name, at, ns = "") => `${kind}:${ns}/${name}@${at}`;
    for (const c of clashes) for (const pair of c.times || []) pair.forEach((t, i) => mark(key(jobKind(c.jobs?.[i]?.name), c.jobs?.[i]?.name, t), c.severity));
    for (const o of overlaps) { mark(key("Longhorn", o.backup, o.started.getTime() / 1000), "close"); mark(key(o.kind, o.other, o.at.getTime() / 1000, o.ns), "close"); }
    for (const item of items) if (item.kind !== "Power" || item.what === "stops") item.clash = marks.get(key(item.kind, item.name, item.at.getTime() / 1000, item.ns)) || "";
    // A collapsed job has no single run to mark: it carries its worst pair.
    for (const f of frequent) if (f.kind === "Longhorn" || f.kind === "Homestead") {
      const named = clashes.filter(c => (c.jobs || []).some(j => j.name === f.name));
      f.clash = named.some(c => c.severity === "clash") ? "clash" : named.length || overlaps.some(o => o.backup === f.name) ? "close" : "";
    }
    items.sort((a, b) => a.at - b.at || a.name.localeCompare(b.name));
    return { items, frequent, unread, unreadable, unnamed, effects, windowUnknown, clashes, overlaps, until, checked: lhRead(sources.lh) };
  }

  /* Andreas, 2026-10-23: a host OS update window, or an app's power stop, while
     a backup of its volumes runs is close - amber, never a clash. The OS
     window counts from the hour it opens (a rollout starts then, one host at a
     time), and only when it may restart hosts; it names the hosts that hold a
     copy of the backup's volumes when /api/volumes says, else any backup in
     that hour counts. A power stop counts for an app whose claims the backup
     covers (VMs are not matched). A backup that began before the week did is
     not seen. */
  function overlapsOf(backups, stops, windows, apps, copies, volumeOf) {
    const out = [], HOUR = 3600000;
    // Running at a moment: started at or before it (or within the window's span) and not yet done.
    const during = (b, at, span = 0) => b.times.find(t => (span ? t.getTime() < at.getTime() + span : t.getTime() <= at.getTime()) && at.getTime() < t.getTime() + b.length);
    for (const at of windows) for (const b of backups) {
      const started = during(b, at, HOUR);
      if (!started) continue;
      const hosts = [...new Set((copies() || []).filter(v => b.volumes.has(v.name)).flatMap(v => (v.copies || []).map(c => c.node)).filter(Boolean))].sort();
      out.push({ severity: "close", at, started, backup: b.name, kind: "OS updates", other: "Host OS updates",
        reason: hosts.length ? `host${hosts.length === 1 ? "" : "s"} ${names(hosts)} may restart during backup ${b.name}` : `a host may restart during backup ${b.name}` });
    }
    if (stops.length) {
      const list = apps() || [];
      for (const s of stops) {
        const app = list.find(w => w.ns === s.ns && w.name === s.name);
        const volumes = new Set((app?.claims || []).map(c => volumeOf.get(`${s.ns}/${c}`)).filter(Boolean));
        for (const b of backups) {
          if (![...b.volumes].some(v => volumes.has(v))) continue;
          for (const at of s.times) {
            const started = during(b, at);
            if (started) out.push({ severity: "close", at, started, backup: b.name, kind: "Power", other: s.name, ns: s.ns || "", reason: `app ${s.name} stops during its backup ${b.name}` });
          }
        }
      }
    }
    return out.sort((a, b) => a.at - b.at);
  }

  /* Today ends at local midnight, in the reader's own zone; the week is the whole list. */
  function inRange(items, from, range) {
    if (range !== "today") return items;
    const midnight = new Date(from.getFullYear(), from.getMonth(), from.getDate() + 1);
    return items.filter(i => i.at < midnight);
  }

  /* The filter (#919): kind and range are kept in the address, so a link or a
     reload shows the same; App and Host last for the session, on this page
     and on Schedules. A value the address names that the page does not have
     is ignored. */
  const FILTER_KEY = "homestead.schedule.filter";
  function readFilter(kinds, ranges = {}) {
    let q = { get: () => null }, kept = {};
    try { q = new URLSearchParams(location.search); } catch { /* no address */ }
    try { kept = JSON.parse(sessionStorage.getItem(FILTER_KEY) || "{}") || {}; } catch { kept = {}; }
    const text = v => typeof v === "string" ? v : "";
    return { kind: Object.hasOwn(kinds, q.get("kind") || "") ? q.get("kind") : "", range: Object.hasOwn(ranges, q.get("range") || "") ? q.get("range") : "",
      app: text(kept.app), host: text(kept.host) };
  }
  function keepFilter(f, inAddress) {
    try {
      const url = new URL(location.href);
      for (const key of inAddress) { if (f[key]) url.searchParams.set(key, f[key]); else url.searchParams.delete(key); }
      const next = url.pathname + url.search + url.hash;
      if (next !== location.pathname + location.search + location.hash) history.replaceState(history.state, "", next);
    } catch { /* this view only */ }
    try { sessionStorage.setItem(FILTER_KEY, JSON.stringify({ app: f.app || "", host: f.host || "" })); } catch { /* this view only */ }
  }
  const matches = (row, f) => (!f.kind || row.type === f.kind) && (!f.host || row.host === f.host)
    && (!f.app || row.app === f.app || (row.apps || []).includes(f.app));
  const counts = rows => rows.reduce((all, r) => ({ ...all, [r.type]: (all[r.type] || 0) + 1 }), {});
  /* The shared bar (timeline.js loads after this file: asked at run time). Its
     menus list every app a row names, a window's apps too. */
  function bar(rows, f, call) {
    const entries = rows.flatMap(r => [r, ...(r.apps || []).map(app => ({ app, host: "" }))]);
    return window.Timeline.filterBar({ entries, counts: counts(rows), filter: f, kinds: KINDS, call });
  }

  function clashLine(c, volumeName) {
    const [a, b] = c.jobs || [], first = new Date(((c.times || [])[0] || [0])[0] * 1000);
    const when = c.severity === "clash" ? "start in the same minute" : `start ${c.gap_min} minutes apart`;
    const more = c.count > 1 ? `, and ${c.count - 1} more time${c.count === 2 ? "" : "s"} this week` : "";
    return `<li>${UI.chip(c.severity === "clash" ? "same minute" : "close", "warn", { size: "slim" })}
      <strong>${esc(a?.name)}</strong> (${esc(TASK_WORDS[a?.task] || a?.task)}) and <strong>${esc(b?.name)}</strong> (${esc(TASK_WORDS[b?.task] || b?.task)})
      ${when} on ${esc((c.volumes || []).map(volumeName).join(", "))}: ${fmtNext(first)}${more}</li>`;
  }

  function missingLines(week) {
    return [week.unread.length && `Could not read ${week.unread.join(", ")}: those schedules are missing from this week. Reload to try again.`,
      week.unreadable.length && `Could not read the schedule of ${week.unreadable.join(", ")}, so ${week.unreadable.length === 1 ? "it is" : "they are"} not in this list.`,
      // A window that may be missing makes the unread apps list a missing schedule: its one line goes here.
      week.windowUnknown && unnamedText(week, "the apps list")]
      .filter(Boolean).map(line => `<p>${esc(line)}</p>`).join("");
  }

  /* One callout at most (#919): the clashes, with what could not be read in it;
     else what could not be read; else the card that says there is no clash. */
  function attention(week, volumeName) {
    const real = week.clashes.filter(c => c.severity === "clash"), close = week.clashes.filter(c => c.severity !== "clash");
    const overlapLine = o => `<li>${UI.chip("close", "warn", { size: "slim" })} ${esc(o.reason)}: ${fmtNext(o.at)}</li>`;
    const closeList = close.length || week.overlaps.length ? `<ul class="ui-list week-clashes">${close.map(c => clashLine(c, volumeName)).join("")}${week.overlaps.map(overlapLine).join("")}</ul>` : "";
    const missing = missingLines(week);
    if (week.checked && real.length) return UI.callout("warn", `${real.length} clash${real.length === 1 ? "" : "es"} this week`,
      `<ul class="ui-list week-clashes">${real.map(c => clashLine(c, volumeName)).join("")}</ul>${closeList}
      <p>Two backups of a volume at once, or a trim while it is backed up, slow each other down and can fail. Move one of them in Data protection.</p>${missing}`);
    // A check not done is not a quiet week (re-review): with the stops unchecked it says "No clash found".
    const unchecked = (week.effects?.["the apps list"] || []).includes(STOP_UNCHECKED);
    const quiet = week.checked ? `<div class="card flat week-summary"><b>${unchecked ? "No clash found" : "No clashes this week"}</b>
      <div class="ui-help">No trim or backup starts with a backup on the same volume${closeList ? "; these may get in each other's way" : ""}.</div>${closeList}</div>` : "";
    return (missing ? UI.callout("warn", "Some schedules are missing", missing) : "") + quiet;
  }

  /* The summary line: how many runs are shown, and the clashes and close ones among them. */
  function summaryLine(week, shown, range) {
    const runs = shown.reduce((n, r) => n + (r.perDay ? r.perDay * (range === "today" ? 1 : 7) : 1), 0);
    const real = week.clashes.filter(c => c.severity === "clash").length, close = week.clashes.length - real + week.overlaps.length;
    const clash = !week.checked ? "clashes not checked" : `${real} clash${real === 1 ? "" : "es"}${close ? `, ${close} close` : ""}`;
    return `<div class="dim small week-line">About ${runs} run${runs === 1 ? "" : "s"} ${range === "today" ? "today" : "this week"} · ${clash}</div>`;
  }

  /* A source that only names what runs touch, unread (review, #919): every run
     is still listed, without those names - said as such, not as missing schedules. */
  // One line per source, with everything it costs (re-review, #919).
  const unnamedText = (week, label) => `Could not read ${label}: ${(week.effects?.[label] || ["some runs name nothing"]).join("; ")}.`;
  const unnamedLines = week => (week.unnamed || []).filter(label => !(week.windowUnknown && label === "the apps list"))
    .map(label => `<div class="dim small">${esc(unnamedText(week, label))}</div>`).join("");
  const UNSEEN_TIP = "Homestead can't see which app this touches (another namespace)";
  /* An App filter hides the runs of other namespaces, whose app cannot be seen: said, never silent. */
  // weight: how many runs a row stands for (an all-day job, about perDay a day); its count is then "about".
  function unseenLine(rows, f, one, many, weight = () => 1) {
    if (!f.app) return "";
    const hidden = rows.filter(r => r.unseen && matches(r, { ...f, app: "" }) && !matches(r, f));
    const n = hidden.reduce((sum, r) => sum + weight(r), 0), about = hidden.some(r => weight(r) !== 1) ? "About " : "";
    if (!n) return "";
    return `<div class="dim small">${n === 1 ? `1 ${one} in another namespace may touch it: Homestead can't see which app it touches`
      : `${about}${n} ${many} in other namespaces may touch it: Homestead can't see which app they touch`}.</div>`;
  }

  const open = href => href ? `<button type="button" class="btn sm" onclick="openOperation(${jsq(href)})">Open</button>` : "";
  // One run, or a job that runs all day; the clash chip says it in words, not only colour.
  function tableRow(i) {
    const when = i.perDay ? `<span class="dim">All day</span>` : fmtNext(i.at);
    const what = i.perDay ? `${i.what}, about ${i.perDay} times a day` : i.what;
    const touches = [i.app, i.host].filter(Boolean);
    return `<tr><td data-label="When" class="nowrap" data-sort="${i.perDay ? 0 : i.at.getTime()}">${when}</td>
      <td data-label="Kind">${UI.chip(KINDS[i.type]?.[2] || i.kind, "neutral", { size: "slim" })}</td>
      <td data-label="What"><b>${esc(i.name)}</b>${what ? ` <span class="dim">${esc(what)}</span>` : ""}${i.clash ? " " + UI.chip(i.clash === "clash" ? "clash" : "close", "warn", { size: "slim" }) : ""}</td>
      <td data-label="App or host">${touches.length ? esc(touches.join(" · ")) : i.unseen
        ? `<span class="dim" tabindex="0" data-tip="${esc(UNSEEN_TIP)}" aria-label="${esc(UNSEEN_TIP)}">—</span>` : '<span class="dim">—</span>'}</td>
      <td data-actions>${open(i.href)}</td></tr>`;
  }

  const RANGES = { today: "Today", week: "7 days" };
  const page = { week: null, from: null, volumeName: n => n, filter: null };
  function paintWeek() {
    const { week, from, volumeName } = page, f = page.filter, range = f.range || "week";
    const rows = [...week.frequent, ...inRange(week.items, from, range)];
    const shown = rows.filter(r => matches(r, f));
    const rangeSeg = `<div class="seg" role="group" aria-label="Range">${Object.entries(RANGES).map(([key, label]) =>
      `<button type="button" class="${range === key ? "on" : ""}" aria-pressed="${range === key}" onclick="weekRange(${jsq(key)})">${esc(label)}</button>`).join("")}</div>`;
    paint(`${UI.pageHeader("This week", `Everything scheduled from now until ${fmtNext(week.until)}, in your time zone (${esc(yourZone())})`,
        `${rangeSeg} ${UI.button("Schedules", "go('schedules')")}`)}
      ${attention(week, volumeName)}
      ${summaryLine(week, shown, range)}${unnamedLines(week)}${unseenLine(rows, f, "run", "runs", r => r.perDay ? r.perDay * (range === "today" ? 1 : 7) : 1)}
      ${bar(rows, f, "weekFilter")}
      ${shown.length ? `<div class="card flat pad0"><div class="tblwrap"><table class="tbl stack compact" data-sort="schedule-week"><thead><tr>
        <th>When</th><th>Kind</th><th>What</th><th>App or host</th><th data-nosort></th></tr></thead>
        <tbody>${shown.map(tableRow).join("")}</tbody></table></div></div>`
        : UI.empty(rows.length ? "Nothing matches the filter." : range === "today" ? "Nothing scheduled for the rest of today." : "Nothing scheduled this week.")}`);
  }

  window.viewScheduleWeek = async function () {
    const failed = e => ({ failed: e?.message || "could not be read" });
    const [lh, crons, power, os, settings, workloads, copies] = await Promise.all([
      STATE.platform?.longhorn === false ? null : api("/api/lh/overview").catch(failed),
      api("/api/schedules").catch(failed), api("/api/power-schedules").catch(failed),
      api("/api/os-updates").catch(failed), api("/api/settings").catch(failed), api("/api/workloads").catch(failed),
      api("/api/volumes").catch(failed)]);
    const from = new Date(), week = collect({ lh, crons, power, os, settings, workloads, copies }, from);
    const pvc = new Map((answered(lh) && Array.isArray(lh.volumes) ? lh.volumes : []).map(v => [v.name, v.pvc || v.name]));
    Object.assign(page, { week, from, volumeName: name => pvc.get(name) || name, filter: readFilter(KINDS, RANGES) });
    paintWeek();
  };
  window.weekFilter = (key, value) => {
    if (!page.filter || !["kind", "app", "host"].includes(key)) return;
    page.filter[key] = key === "kind" && !Object.hasOwn(KINDS, value) ? "" : String(value || "");
    keepFilter(page.filter, ["kind", "range"]);
    paintWeek();
  };
  window.weekRange = range => {
    if (!page.filter || !Object.hasOwn(RANGES, range)) return;
    page.filter.range = range === "week" ? "" : range;
    keepFilter(page.filter, ["kind", "range"]);
    paintWeek();
  };

  const api_ = { offsetMin, wallToMs, collect, inRange, KINDS, appOf, readFilter, keepFilter, matches, bar, unseenLine };
  if (typeof window !== "undefined") window.ScheduleWeek = api_;
  if (typeof module !== "undefined") module.exports = api_;
})();
