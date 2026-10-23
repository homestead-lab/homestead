// Schedules › This week: everything scheduled in the next 7 days, by day,
// with the server's clashes marked (web/js/schedule-week.js).
"use strict";
process.env.TZ = "Europe/London";
const test = require("node:test"), assert = require("node:assert/strict");
const fs = require("node:fs"), vm = require("node:vm");
const between = require("./helpers/between");

function setup(answers = {}) {
  const out = {};
  const ctx = { console, Date, Intl, Map, Set, Promise, JSON, Math, Number, String, Array, Object, document: { addEventListener() {} },
    STATE: { platform: {} }, esc: x => String(x ?? "").replaceAll("&", "&amp;").replaceAll("<", "&lt;").replaceAll('"', "&quot;"),
    paint: html => { out.html = html; }, go: () => {}, yourZone: () => "Europe/London", jsArg: s => JSON.stringify(String(s ?? "")),
    api: async path => { const a = answers[path]; if (a instanceof Error) throw a; return a; } };
  ctx.window = ctx;
  vm.createContext(ctx);
  require("./helpers/load-ui")(ctx);
  // core.js writes every time (#503).
  vm.runInContext(between(fs.readFileSync("web/js/core.js", "utf8"), "const asDate", "/* Said once on a page"), ctx);
  vm.runInContext(fs.readFileSync("web/js/cron.js", "utf8"), ctx);
  vm.runInContext(fs.readFileSync("web/js/schedule-week.js", "utf8"), ctx);
  return { W: ctx.ScheduleWeek, ctx, out };
}

// Friday 23 October 2026, 13:00 in London (12:00 UTC); clocks go back on Sunday the 25th.
const FROM = new Date("2026-10-23T12:00:00Z");
const SAT_0200 = Date.parse("2026-10-24T02:00:00Z") / 1000;
const iso = d => d.toISOString().slice(0, 16);
// Values made in the page's own context compare by value, not by realm.
const same = (actual, expected, message) => assert.deepEqual(JSON.parse(JSON.stringify(actual)), expected, message);
const LH = {
  jobs: [
    { name: "nightly-backup", task: "backup", cron: "0 2 * * *", volumes: ["pvc-a"] },
    { name: "weekly-trim", task: "filesystem-trim", cron: "0 2 * * 6", volumes: ["pvc-a"] },
    { name: "hourly-snapshot", task: "snapshot", cron: "0 * * * *", volumes: ["pvc-a"] }],
  clashes: [{ severity: "clash", jobs: [{ name: "nightly-backup", task: "backup" }, { name: "weekly-trim", task: "filesystem-trim" }],
    volumes: ["pvc-a"], times: [[SAT_0200, SAT_0200]], count: 1, gap_min: 0 }] };

test("Longhorn runs are listed, and the two the server says clash are marked", () => {
  const { W } = setup();
  const week = W.collect({ lh: LH }, FROM);
  const nightly = week.items.filter(i => i.name === "nightly-backup");
  assert.equal(nightly.length, 7);
  same(nightly.filter(i => i.clash).map(i => [iso(i.at), i.clash]), [["2026-10-24T02:00", "clash"]]);
  const trim = week.items.filter(i => i.name === "weekly-trim");
  same(trim.map(i => [iso(i.at), i.clash, i.kind, i.what]), [["2026-10-24T02:00", "clash", "Longhorn", "trim"]]);
  assert.equal(week.clashes.length, 1);
  same(week.unread, []);
});

test("a job that runs more than six times a day is one 'runs all day' line, not 168 rows", () => {
  const { W } = setup();
  const week = W.collect({ lh: LH }, FROM);
  assert.equal(week.items.filter(i => i.name === "hourly-snapshot").length, 0);
  same(week.frequent.map(f => [f.name, f.perDay, f.what]), [["hourly-snapshot", 24, "snapshot"]]);
  const sixTimes = W.collect({ lh: { jobs: [{ name: "four-hourly", task: "snapshot", cron: "0 */4 * * *", volumes: [] }], clashes: [] } }, FROM);
  assert.equal(sixTimes.frequent.length, 0, "six a day is still listed");
  assert.equal(sixTimes.items.length, 42);
});

test("CronJobs: suspended ones are left out, and a time zone of their own is honoured across the clock change", () => {
  const { W } = setup();
  const week = W.collect({ crons: [
    { name: "db-dump", schedule: "0 3 * * *", time_zone: "Europe/Berlin", suspend: false },
    { name: "restic", schedule: "15 2 * * *", suspend: false },
    { name: "paused", schedule: "0 1 * * *", suspend: true }] }, FROM);
  const dump = week.items.filter(i => i.name === "db-dump").map(i => iso(i.at));
  same(dump.slice(0, 2), ["2026-10-24T01:00", "2026-10-25T02:00"]);
  assert.equal(dump.length, 7);
  assert.equal(iso(week.items.find(i => i.name === "restic").at), "2026-10-24T02:15", "no zone: UTC, as the Schedules page reads it");
  assert.equal(week.items.some(i => i.name === "paused"), false);
  assert.equal(week.items[0].kind, "CronJob");
});

test("power schedules: each stop and start on its days, in its zone; next runs when there is no schedule", () => {
  const { W } = setup();
  const week = W.collect({ power: { items: [
    { kind: "app", name: "paperless", schedule: { stop: "01:00", start: "07:00", days: [0, 1, 2, 3, 4], tz: "UTC" } },
    { kind: "vm", name: "old-vm", next: [{ at: SAT_0200 + 3600, action: "stop" }] }] } }, FROM);
  const paperless = week.items.filter(i => i.name === "paperless");
  assert.equal(paperless.filter(i => i.what === "stops").length, 5);
  assert.equal(paperless.filter(i => i.what === "starts").length, 5);
  assert.equal(iso(paperless[0].at), "2026-10-26T01:00", "Monday is the first weekday after Friday noon");
  same(week.items.filter(i => i.name === "old-vm").map(i => [iso(i.at), i.what, i.kind]), [["2026-10-24T03:00", "stops", "Power"]]);
});

test("host OS updates: the weekly window in its zone, or its saved offset when the zone is unknown", () => {
  const { W } = setup();
  const os = schedule => ({ applies: true, settings: { schedule: { enabled: true, days: ["sun"], hour: 3, offset_min: 60, ...schedule } } });
  same(W.collect({ os: os({ tz: "Europe/London" }) }, FROM).items.map(i => [iso(i.at), i.kind]), [["2026-10-25T03:00", "OS updates"]]);
  same(W.collect({ os: os({ tz: "Not/AZone" }) }, FROM).items.map(i => iso(i.at)), ["2026-10-25T02:00"]);
  same(W.collect({ os: os({ tz: "", enabled: false }) }, FROM).items, []);
  const none = W.collect({ os: { applies: false } }, FROM);
  same([none.items, none.unread], [[], []], "a cluster where OS updates do not apply is not an error");
});

test("the maintenance window shows only when something uses it: an app set to update itself, or Balance hosts", () => {
  const { W } = setup();
  const settings = (policy, rebalance) => ({ updates: { policy, maintenance: { days: [0, 1, 2, 3, 4, 5, 6], start: "02:00", duration_minutes: 120 } },
    placement: { rebalance } });
  const auto = [{ name: "a", update_mode: "auto" }, { name: "b", update_mode: "manual" }];
  const used = W.collect({ settings: settings("approval_required", "off"), workloads: auto }, FROM).items;
  assert.equal(used.length, 7);
  assert.equal(iso(used[0].at), "2026-10-24T02:00");
  assert.match(used[0].what, /02:00–04:00 UTC/);
  assert.match(used[0].what, /1 app set to update itself/);
  assert.equal(W.collect({ settings: settings("approval_required", "off"), workloads: [{ name: "b", update_mode: "manual" }] }, FROM).items.length, 0,
    "a default install, with no app updating itself, has nothing in the window");
  assert.equal(W.collect({ settings: settings("notify_only", "off"), workloads: auto }, FROM).items.length, 0);
  const balance = W.collect({ settings: settings("notify_only", "window"), workloads: auto }, FROM).items;
  assert.equal(balance.length, 7);
  assert.match(balance[0].what, /Balance hosts/);
  assert.doesNotMatch(balance[0].what, /app/);
  const unread = W.collect({ settings: settings("approval_required", "off"), workloads: { failed: "x" } }, FROM);
  same(unread.unread, ["the apps list"]);
  assert.equal(W.collect({ settings: settings("notify_only", "off"), workloads: { failed: "x" } }, FROM).unread.length, 0,
    "under notify only no app updates itself, so the apps need not be read");
});

test("macros, day and month names are read as Kubernetes and Longhorn read them", () => {
  const { W } = setup();
  const week = W.collect({ lh: { jobs: [{ name: "daily", task: "snapshot", cron: "@daily", volumes: [] },
      { name: "saturdays", task: "backup", cron: "0 0 * * sat", volumes: [] }], clashes: [] },
    crons: [{ name: "weekdays", schedule: "0 4 ? * mon-fri" }, { name: "in-june", schedule: "0 4 1 jun *" }, { name: "hourly", schedule: "@hourly" }] }, FROM);
  assert.equal(week.items.filter(i => i.name === "daily").length, 7);
  same(week.items.filter(i => i.name === "saturdays").map(i => iso(i.at)), ["2026-10-24T00:00"]);
  assert.equal(week.items.filter(i => i.name === "weekdays").length, 5);
  assert.equal(week.items.filter(i => i.name === "in-june").length, 0);
  same(week.frequent.map(f => [f.name, f.perDay]), [["hourly", 24]]);
  same(week.unreadable, []);
});

test("a schedule this page cannot read is named, not dropped", async () => {
  const { W, ctx, out } = setup({ "/api/lh/overview": { jobs: [{ name: "odd", task: "backup", cron: "@every 1h", volumes: [] }], clashes: [] },
    "/api/schedules": [{ name: "garbled", schedule: "garbage" }], "/api/power-schedules": { items: [] },
    "/api/os-updates": { applies: false }, "/api/settings": { updates: { policy: "notify_only" }, placement: {} }, "/api/workloads": [] });
  same(W.collect({ lh: { jobs: [{ name: "odd", cron: "@every 1h" }] }, crons: [{ name: "garbled", schedule: "garbage" }] }, FROM).unreadable,
    ["odd", "garbled"]);
  await ctx.viewScheduleWeek();
  assert.match(out.html, /Could not read the schedule of odd, garbled/);
});

test("a job collapsed into 'runs all day' keeps its clash mark", () => {
  const { W } = setup();
  const week = W.collect({ lh: { jobs: [{ name: "often", task: "backup", cron: "*/10 * * * *", volumes: ["v"] },
      { name: "nightly", task: "backup", cron: "0 2 * * *", volumes: ["v"] }],
    clashes: [{ severity: "clash", jobs: [{ name: "nightly", task: "backup" }, { name: "often", task: "backup" }], volumes: ["v"],
      times: [[SAT_0200, SAT_0200]], count: 7, gap_min: 0 }] } }, FROM);
  same(week.frequent.map(f => [f.name, f.clash]), [["often", "clash"]]);
});

test("each source that could not be read is named; one that does not apply is not", () => {
  const { W } = setup();
  const week = W.collect({ lh: { failed: "timed out" }, crons: { error: "forbidden" }, power: "nonsense",
    os: { failed: "x" }, settings: { failed: "x" } }, FROM);
  same(week.unread, ["Longhorn's recurring jobs", "CronJobs", "power schedules", "host OS update settings", "the maintenance window"]);
  same(W.collect({ lh: null }, FROM).unread, []);
});

test("the list is grouped by local day, from today to the day the week ends on", () => {
  const { W } = setup();
  const week = W.collect({ lh: LH }, FROM);
  const days = W.group(week.items, FROM);
  assert.equal(days.length, 8, "Friday afternoon to the next Friday morning");
  assert.equal(days[0].date.toDateString(), new Date("2026-10-23T12:00:00Z").toDateString());
  // 02:00 UTC on Saturday is 03:00 in London (summer time) - still Saturday.
  same(days[1].items.map(i => i.name), ["nightly-backup", "weekly-trim"]);
  assert.equal(days.reduce((n, d) => n + d.items.length, 0), week.items.length);
});

test("the page names what it could not read, leads with the clash and collapses the hourly job", async () => {
  const { ctx, out } = setup({ "/api/lh/overview": LH, "/api/schedules": new Error("forbidden"), "/api/power-schedules": { items: [] },
    "/api/os-updates": { applies: false }, "/api/settings": { updates: { policy: "notify_only" }, placement: {} }, "/api/workloads": [] });
  await ctx.viewScheduleWeek();
  assert.match(out.html, /Could not read CronJobs/);
  assert.match(out.html, /1 clash this week/);
  assert.match(out.html, /<strong>nightly-backup<\/strong> \(backup\) and <strong>weekly-trim<\/strong>/, "inline: a callout shows <b> as its title block");
  assert.match(out.html, /nightly-backup/);
  assert.match(out.html, /Runs all day/);
  assert.match(out.html, /hourly-snapshot/);
  assert.doesNotMatch(out.html, /class="(pill|tag)\b/, "chips come from UI.chip");
});

test("with every source read and nothing clashing, the page says so", async () => {
  const { ctx, out } = setup({ "/api/lh/overview": { jobs: [], clashes: [] }, "/api/schedules": [], "/api/power-schedules": { items: [] },
    "/api/os-updates": { applies: false }, "/api/settings": { updates: { policy: "notify_only" }, placement: {} }, "/api/workloads": [] });
  await ctx.viewScheduleWeek();
  assert.match(out.html, /No clashes this week/);
  assert.doesNotMatch(out.html, /Could not read/);
});

test("without Longhorn, clashes are not checked and nothing is said to be unread", async () => {
  const { ctx, out } = setup({ "/api/schedules": [], "/api/power-schedules": { items: [] },
    "/api/os-updates": { applies: false }, "/api/settings": { updates: { policy: "notify_only" }, placement: {} }, "/api/workloads": [] });
  ctx.STATE.platform.longhorn = false;
  await ctx.viewScheduleWeek();
  assert.doesNotMatch(out.html, /Could not read/);
  assert.doesNotMatch(out.html, /No clashes this week/);
});

test("the page is reachable: its address, its view, its script, the Schedules button and the page audit", () => {
  const router = require("../web/js/router.js");
  assert.equal(router.resolve("/schedules/week").view, "schedule-week");
  assert.deepEqual(router.breadcrumbs("schedule-week").map(c => c.label), ["Apps", "Containers", "This week"]);
  assert.match(fs.readFileSync("web/js/app.js", "utf8"), /"schedule-week": \["system", \(\) => viewScheduleWeek\(\), true\]/);
  const html = fs.readFileSync("web/index.html", "utf8");
  assert.ok(html.indexOf("/js/schedule-week.js") > html.indexOf("/js/cron.js"), "after cron.js, whose CRON it uses");
  assert.match(fs.readFileSync("web/js/views-lifecycle.js", "utf8"), /UI\.button\("This week", "go\('schedule-week'\)"\)/);
  assert.match(fs.readFileSync("scripts/audit_pages.mjs", "utf8"), /\["schedules-week", "schedule-week"\]/);
});

test("each day is headed by its name and date, today and tomorrow said as such, times in core.js's words", async () => {
  const { ctx, out } = setup({ "/api/lh/overview": LH, "/api/schedules": [], "/api/power-schedules": { items: [] },
    "/api/os-updates": { applies: false }, "/api/settings": { updates: { policy: "notify_only" }, placement: {} }, "/api/workloads": [] });
  await ctx.viewScheduleWeek();
  assert.match(out.html, /<h4>Today, [^<]*day[^<]*\d[^<]*<\/h4>/);
  assert.match(out.html, /<h4>Tomorrow, [^<]*day[^<]*\d[^<]*<\/h4>/);
  assert.match(out.html, /class="week-time mono">\d\d:\d\d/);
  assert.match(vm.runInContext(`fmtWeekday(new Date("2026-10-24T12:00:00Z"))`, ctx), /Saturday/);
});

test("the demo has a deliberate clash and a maintenance window to show", () => {
  const demo = fs.readFileSync("web/js/demo.js", "utf8");
  assert.match(demo, /name: "weekly-trim", task: "filesystem-trim", cron: "0 2 \* \* 6"[^}]*groups: \["default"\]/);
  assert.match(demo, /clashes: \[\{ severity: "clash", jobs: \[\{ name: "nightly-backup", task: "backup" \}, \{ name: "weekly-trim", task: "filesystem-trim" \}\]/);
  assert.match(demo, /maintenance: \{ days: \[0, 1, 2, 3, 4, 5, 6\], start: "02:00", duration_minutes: 120 \}/);
  assert.match(demo, /name: "home-assistant", ns: "lab", kind: "Deployment", update_mode: "auto"/, "an app that updates itself, so the window shows");
});

test("a wall time the clock change repeats is its first occurrence, as Python's zoneinfo reads it (power schedules)", () => {
  const { W } = setup();
  assert.equal(new Date(W.wallToMs(2026, 9, 25, 1, 30, "Europe/London")).toISOString().slice(0, 16), "2026-10-25T00:30");
  assert.equal(new Date(W.wallToMs(2026, 9, 26, 1, 30, "Europe/London")).toISOString().slice(0, 16), "2026-10-26T01:30");
  assert.equal(new Date(W.wallToMs(2026, 9, 24, 1, 30, "Europe/London")).toISOString().slice(0, 16), "2026-10-24T00:30");
});

// Andreas, 2026-10-23: a host OS update window or a power stop that overlaps a backup is "close".
const backupJob = (cron, extra = {}) => ({ name: "weekly-backup", task: "backup", cron, volumes: ["pvc-a"], ...extra });
const twentyMinutes = { last_run: "2026-10-18T03:00:00Z", last_success: "2026-10-18T03:20:00Z" };
const osWindow = (extra = {}) => ({ applies: true, settings: { reboot: "when-needed", schedule: { enabled: true, days: ["sun"], hour: 3, tz: "UTC", ...extra } } });

test("a host OS update window during a backup's run is close, naming the hosts that hold its volume", () => {
  const { W } = setup();
  const week = W.collect({ lh: { jobs: [backupJob("50 2 * * 0", twentyMinutes)], clashes: [], volumes: [{ name: "pvc-a", pvc: "data-a", namespace: "lab" }] },
    os: osWindow(), copies: [{ name: "pvc-a", copies: [{ node: "node-1" }, { node: "node-2" }] }] }, FROM);
  same(week.overlaps.map(o => [o.severity, o.reason, iso(o.at)]),
    [["close", "hosts node-1 and node-2 may restart during backup weekly-backup", "2026-10-25T03:00"]]);
  assert.equal(week.items.find(i => i.kind === "OS updates").clash, "close");
  assert.equal(week.items.find(i => i.name === "weekly-backup").clash, "close");
});

test("without the volumes' copies any backup in the window counts; a window that never restarts hosts does not", () => {
  const { W } = setup();
  const lh = { jobs: [backupJob("50 2 * * 0", twentyMinutes)], clashes: [] };
  same(W.collect({ lh, os: osWindow() }, FROM).overlaps.map(o => o.reason), ["a host may restart during backup weekly-backup"]);
  same(W.collect({ lh, os: osWindow(), copies: { failed: "x" } }, FROM).unread, ["volume copies"]);
  const never = osWindow(); never.settings.reboot = "never";
  same(W.collect({ lh, os: never }, FROM).overlaps, []);
  same(W.collect({ lh: { jobs: [backupJob("0 1 * * 0", twentyMinutes)], clashes: [] }, os: osWindow() }, FROM).overlaps, [], "done by 01:20, before 03:00");
  same(W.collect({ lh: { jobs: [{ ...backupJob("50 2 * * 0", twentyMinutes), task: "snapshot" }], clashes: [] }, os: osWindow() }, FROM).overlaps, [],
    "only backups");
});

test("an app's power stop during a backup of its volume is close; a backup's run is its last one, else half an hour", () => {
  const { W } = setup();
  const lh = { jobs: [{ name: "nightly", task: "backup", cron: "0 2 * * *", volumes: ["pvc-db"] }], clashes: [],
    volumes: [{ name: "pvc-db", pvc: "db-data", namespace: "lab" }] };
  const workloads = [{ name: "db", ns: "lab", claims: ["db-data"] }, { name: "web", ns: "lab", claims: [] }];
  const power = stop => ({ items: [{ kind: "app", ns: "lab", name: "db", schedule: { stop, start: "07:00", days: [0, 1, 2, 3, 4, 5, 6], tz: "UTC" } },
    { kind: "app", ns: "lab", name: "web", schedule: { stop: "02:10", days: [0, 1, 2, 3, 4, 5, 6], tz: "UTC" } }] });
  const week = W.collect({ lh, workloads, power: power("02:10") }, FROM);
  assert.equal(week.overlaps.length, 7);
  same([week.overlaps[0].reason, iso(week.overlaps[0].at)], ["app db stops during its backup nightly", "2026-10-24T02:10"]);
  assert.equal(week.items.find(i => i.name === "db" && i.what === "stops").clash, "close");
  assert.equal(week.items.find(i => i.name === "db" && i.what === "starts").clash, "");
  same(W.collect({ lh, workloads, power: power("02:40") }, FROM).overlaps, [], "the backup's half hour is over");
  const timed = { ...lh, jobs: [{ ...lh.jobs[0], last_run: "2026-10-22T02:00:00Z", last_success: "2026-10-22T02:50:00Z" }] };
  assert.equal(W.collect({ lh: timed, workloads, power: power("02:40") }, FROM).overlaps.length, 7, "its last run took 50 minutes");
  same(W.collect({ lh, workloads: { failed: "x" }, power: power("02:10") }, FROM).unread, ["the apps list"]);
});

test("the page lists these with the close ones, and the Dashboard still counts only clashes", async () => {
  const { ctx, out } = setup({ "/api/lh/overview": { jobs: [backupJob("50 2 * * 0", twentyMinutes)], clashes: [], volumes: [] },
    "/api/schedules": [], "/api/power-schedules": { items: [] }, "/api/os-updates": osWindow(), "/api/volumes": [],
    "/api/settings": { updates: { policy: "notify_only" }, placement: {} }, "/api/workloads": [] });
  await ctx.viewScheduleWeek();
  assert.match(out.html, /No clashes this week/);
  assert.match(out.html, /a host may restart during backup weekly-backup/);
  assert.match(out.html, /<span class="ui-chip warn slim">close<\/span> a host may restart/, "close is amber (Andreas, 2026-10-23)");
  const H = require("../web/js/insights.js");
  assert.equal(H.advice({ protection: { volumes: [], clashes: [] } }).some(r => r.id === "schedule-clash"), false);
});

test("the demo has an app that stops during its backup", () => {
  const demo = fs.readFileSync("web/js/demo.js", "utf8");
  assert.match(demo, /kind: "app", ns: "lab", name: "frigate"[^}]*schedule: \{ stop: "02:01", days: \[6\], tz: "UTC" \}/);
});

test("review: a stop at the backup's own start minute is close, and so is one known only from the server's next runs", () => {
  const { W } = setup();
  const lh = { jobs: [{ name: "nightly", task: "backup", cron: "0 2 * * *", volumes: ["pvc-db"] }], clashes: [],
    volumes: [{ name: "pvc-db", pvc: "db-data", namespace: "lab" }] };
  const workloads = [{ name: "db", ns: "lab", claims: ["db-data"] }, { name: "db", ns: "other", claims: [] }];
  const atStart = W.collect({ lh, workloads, power: { items: [{ kind: "app", ns: "lab", name: "db", schedule: { stop: "02:00", days: [5], tz: "UTC" } }] } }, FROM);
  same(atStart.overlaps.map(o => iso(o.at)), ["2026-10-24T02:00"]);
  const next = W.collect({ lh, workloads, power: { items: [
    { kind: "app", ns: "lab", name: "db", next: [{ at: SAT_0200 + 300.4, action: "stop" }] },
    { kind: "app", ns: "other", name: "db", next: [{ at: SAT_0200 + 300.4, action: "stop" }] }] } }, FROM);
  same(next.overlaps.map(o => o.reason), ["app db stops during its backup nightly"]);
  same(next.items.filter(i => i.kind === "Power").map(i => i.clash), ["close", ""], "only the app in lab, not its namesake in other");
});
