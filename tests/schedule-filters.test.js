// This week and Schedules (#919 PR 2): kinds, App and Host filters, one table and
// an honest State. Kind and range are kept in the address; App and Host for the session.
"use strict";
process.env.TZ = "Europe/London";
const seen = require("./helpers/seen");
const test = require("node:test"), assert = require("node:assert/strict");
const fs = require("node:fs"), vm = require("node:vm");
const between = require("./helpers/between");

const core = require("./helpers/core-source")();

function setup(answers = {}, { search = "", session = {} } = {}) {
  const out = { urls: [] }, store = { ...session };
  const location = { pathname: "/schedules/week", search, hash: "", origin: "http://192.0.2.1", href: `http://192.0.2.1/schedules/week${search}` };
  const ctx = { console, Date, Intl, Map, Set, Promise, JSON, Math, Number, String, Array, Object, URL, URLSearchParams,
    document: { addEventListener() {}, querySelector: () => null }, STATE: { platform: {}, data: {} }, location,
    history: { state: null, replaceState: (state, title, url) => { out.urls.push(url); const u = new URL(url, location.origin); location.search = u.search; location.href = u.href; } },
    sessionStorage: { getItem: k => store[k] ?? null, setItem: (k, v) => { store[k] = String(v); } },
    esc: x => String(x ?? "").replaceAll("&", "&amp;").replaceAll("<", "&lt;").replaceAll('"', "&quot;"),
    paint: html => { out.html = html; }, go: () => {}, yourZone: () => "Europe/London", modal() {}, toast() {}, resetPaint() {},
    $: () => null, $$: () => [], icon: () => "",
    api: async path => { const a = answers[path]; if (a instanceof Error) throw a; return a; } };
  ctx.window = ctx;
  vm.createContext(ctx);
  require("./helpers/load-ui")(ctx);
  ctx.jsArg = s => JSON.stringify(String(s ?? "")); ctx.jsq = s => ctx.esc(ctx.jsArg(s));
  vm.runInContext(between(core, "const fmtUp", "/* Said once on a page"), ctx);
  vm.runInContext(fs.readFileSync("web/js/cron.js", "utf8"), ctx);
  vm.runInContext(fs.readFileSync("web/js/schedule-week.js", "utf8"), ctx);
  vm.runInContext(fs.readFileSync("web/js/timeline.js", "utf8"), ctx);
  vm.runInContext(fs.readFileSync("web/js/views-lifecycle-schedules.js", "utf8"), ctx);
  return { ctx, out, store, W: ctx.ScheduleWeek };
}
const same = (actual, expected, message) => assert.deepEqual(JSON.parse(JSON.stringify(actual)), expected, message);

// Friday 23 October 2026, 13:00 in London; clocks go back on Sunday the 25th.
const FROM = new Date("2026-10-23T12:00:00Z");
const APPS = [{ ns: "lab", name: "frigate", claims: ["frigate-config"], update_mode: "auto" },
  { ns: "lab", name: "paperless", claims: ["paperless-data", "shared"] }, { ns: "lab", name: "wiki", claims: ["shared"] }];
const LH = { clashes: [], volumes: [{ name: "pvc-f", namespace: "lab", pvc: "frigate-config" }, { name: "pvc-p", namespace: "lab", pvc: "paperless-data" }],
  jobs: [{ name: "frigate-backup", task: "backup", cron: "0 2 * * *", volumes: ["pvc-f"] },
    { name: "all-snapshots", task: "snapshot", cron: "0 4 * * *", volumes: ["pvc-f", "pvc-p"] }],
  homestead_jobs: [{ name: "Homestead weekly trim", task: "filesystem-trim", cron: "0 3 * * 0", volumes: ["pvc-f"] }] };
const cron = extra => ({ name: "db-dump", ns: "lab", schedule: "0 1 * * *", time_zone: "Etc/UTC", suspend: false, active: 0, last: "2026-10-23T01:00:00Z",
  result: "ok", missed: false, owner: "", claims: [], ...extra });
const QUIET = { "/api/power-schedules": { items: [] }, "/api/os-updates": { applies: false },
  "/api/settings": { updates: { policy: "notify_only" }, placement: {} }, "/api/volumes": [] };

test("State: the worst that applies, failed > did not run > unknown > running > paused > ok, never a guess", () => {
  const { ctx } = setup();
  const state = j => ctx.jobState(cron(j));
  assert.equal(state({ result: "failed", missed: true, active: 1, suspend: true }), "failed");
  assert.equal(state({ missed: true, active: 1 }), "missed");
  assert.equal(state({ result: "unknown", active: 1 }), "unknown", "its runs could not be read: amber, not running or ok");
  assert.equal(state({ missed: null }), "unknown", "whether it missed a run could not be told");
  assert.equal(state({ result: "running", active: 1 }), "running");
  assert.equal(state({ suspend: true, missed: false }), "paused");
  assert.equal(state({ result: "failed", suspend: true }), "failed", "paused after a failure still says it failed");
  assert.equal(state({}), "ok");
  assert.equal(state({ result: "never", last: "" }), "waiting");
  assert.equal(state({ owner: "longhorn", missed: false, result: "ok" }), "ok");
  same(Object.fromEntries(Object.entries(vm.runInContext("JOB_STATE", ctx)).map(([k, [tone, word]]) => [k, [tone, word]])), {
    failed: ["bad", "failed"], missed: ["warn", "did not run"], unknown: ["warn", "unknown"], running: ["neutral", "running"],
    paused: ["neutral", "paused"], ok: ["ok", "ok"], waiting: ["neutral", "not run yet"] });
});

test("Schedules shows a failed job's State as failed, not a green 'active', and sums the states up", async () => {
  const { ctx, out } = setup({ "/api/schedules": [cron({ name: "backup-photos", result: "failed" }), cron({ name: "report", missed: true }),
    cron({ name: "db-dump" }), cron({ name: "lh-backup", ns: "longhorn-system", owner: "longhorn" })], "/api/workloads": APPS });
  await ctx.viewSchedules();
  const rowOf = name => out.html.split("<tr").find(r => r.includes(`<b>${name}</b>`));
  assert.match(rowOf("backup-photos"), /ui-chip[^"]*bad[^>]*>failed</);
  assert.match(rowOf("report"), /did not run/);
  assert.doesNotMatch(out.html, />active</);
  assert.match(out.html, /4 jobs: 1 failed, 1 did not run, 2 ok/);
});

test("kinds: Longhorn's own CronJobs and Homestead's maintenance are System; an app's job applied by Flux is a CronJob", async () => {
  const { ctx, W } = setup();
  assert.equal(vm.runInContext("scheduleRow", ctx)(cron({ owner: "longhorn" }), APPS).type, "system");
  assert.equal(vm.runInContext("scheduleRow", ctx)(cron({ owner: "flux" }), APPS).type, "cron");
  const week = W.collect({ lh: LH, workloads: APPS, settings: { updates: { policy: "approval_required", maintenance: { days: [0, 1, 2, 3, 4, 5, 6], start: "02:00", duration_minutes: 60 } },
    placement: { rebalance: "off" } } }, FROM);
  const typeOf = name => week.items.find(i => i.name === name)?.type;
  same([typeOf("frigate-backup"), typeOf("all-snapshots"), typeOf("Homestead weekly trim"), typeOf("Maintenance window")],
    ["backup", "snapshot", "system", "update"]);
  const balance = W.collect({ settings: { updates: { policy: "notify_only", maintenance: { days: [0], start: "02:00", duration_minutes: 60 } },
    placement: { rebalance: "window" } } }, FROM);
  assert.equal(balance.items[0].type, "system", "the window used only to balance hosts is Homestead's own run");
});

test("each run carries the one app it touches, or none: never a guess", () => {
  const { W } = setup();
  const week = W.collect({ lh: LH, workloads: APPS, crons: [cron({ name: "one", claims: ["paperless-data"] }), cron({ name: "two", claims: ["shared"] }),
    cron({ name: "other-ns", ns: "media", claims: ["frigate-config"] })] }, FROM);
  const appOf = name => week.items.find(i => i.name === name)?.app;
  same([appOf("frigate-backup"), appOf("all-snapshots"), appOf("one"), appOf("two"), appOf("other-ns")],
    ["lab/frigate", "", "lab/paperless", "", ""], "a job over two apps' volumes, or a claim two apps share, names none");
  assert.equal(W.collect({ crons: [cron({ owner: "longhorn" })], lh: LH, workloads: APPS }, FROM).items.filter(i => i.name === "db-dump").length, 0,
    "Longhorn's CronJob for a recurring job is that job, already listed");
});

test("This week is one table, When / Kind / What / App or host / Open, with one callout", async () => {
  const lh = { ...LH, clashes: [{ severity: "clash", jobs: [{ name: "frigate-backup", task: "backup" }, { name: "all-snapshots", task: "snapshot" }],
    volumes: ["pvc-f"], times: [[Date.parse("2026-10-24T02:00:00Z") / 1000, Date.parse("2026-10-24T02:00:00Z") / 1000]], count: 1, gap_min: 0 }] };
  const { ctx, out } = setup({ ...QUIET, "/api/lh/overview": lh, "/api/schedules": new Error("forbidden"), "/api/workloads": APPS });
  await ctx.viewScheduleWeek();
  same([...out.html.matchAll(/<th[^>]*>([^<]*)<\/th>/g)].map(m => m[1]), ["When", "Kind", "What", "App or host", ""]);
  assert.match(out.html, /class="tbl stack compact"/);
  assert.equal((out.html.match(/<div class="ui-callout /g) || []).length, 1, "the clash and what could not be read share one callout");
  assert.match(out.html, /1 clash this week/);
  assert.match(out.html, /Could not read CronJobs/);
  const backup = out.html.split("<tr").find(r => r.includes("<b>frigate-backup</b>"));
  assert.match(backup, /data-label="App or host">lab\/frigate</);
  assert.match(backup, /Backups/);
  assert.match(backup, /openOperation\(&quot;\/data-protection&quot;\)/);
  assert.match(out.html, /About \d+ runs? this week · 1 clash</);
});

test("kind and range are kept in the address; App and Host for the session", async () => {
  const answers = { ...QUIET, "/api/lh/overview": LH, "/api/workloads": APPS,
    // Every five minutes: a line that runs all day, so it is in Today whenever the test runs.
    "/api/schedules": [cron({ schedule: "*/5 * * * *", claims: ["paperless-data"] })] };
  const { ctx, out, store } = setup(answers, { search: "?demo=1" });
  await ctx.viewScheduleWeek();
  ctx.weekFilter("kind", "backup");
  assert.equal(out.urls.at(-1), "/schedules/week?demo=1&kind=backup");
  assert.doesNotMatch(out.html, /<b>db-dump<\/b>/);
  ctx.weekFilter("app", "lab/frigate");
  assert.equal(out.urls.length, 1, "the app is not put in the address");
  assert.equal(JSON.parse(store["homestead.schedule.filter"]).app, "lab/frigate");
  ctx.weekRange("today");
  assert.equal(out.urls.at(-1), "/schedules/week?demo=1&kind=backup&range=today");
  // Opened anew: kind and range from the address, the app from the session.
  const again = setup(answers, { search: "?kind=cron&range=today", session: { "homestead.schedule.filter": JSON.stringify({ app: "lab/paperless" }) } });
  await again.ctx.viewScheduleWeek();
  assert.match(again.out.html, /aria-pressed="true" onclick="weekFilter\('kind', &quot;cron&quot;\)"/);
  assert.match(again.out.html, /aria-pressed="true" onclick="weekRange\(&quot;today&quot;\)"/);
  assert.match(again.out.html, /<option value="lab\/paperless" selected>/);
  assert.match(again.out.html, /<b>db-dump<\/b>/);
  // A kind the address names that this page does not have is ignored, not trusted.
  const odd = setup(answers, { search: "?kind=%3Cx%3E" });
  await odd.ctx.viewScheduleWeek();
  assert.doesNotMatch(odd.out.html, /&lt;x&gt;/);
});

test("Today ends at local midnight; 7 days a week from now", () => {
  const { W } = setup();
  const week = W.collect({ lh: LH, workloads: APPS }, FROM);
  const today = W.inRange(week.items, FROM, "today"), all = W.inRange(week.items, FROM, "week");
  assert.equal(today.length, 0, "nothing is due before midnight on Friday afternoon");
  assert.equal(all.length, week.items.length);
  const late = new Date("2026-10-23T23:30:00Z");  // 00:30 on Saturday in London
  assert.ok(W.inRange(W.collect({ lh: LH, workloads: APPS }, late).items, late, "today").every(i => i.at < new Date("2026-10-24T23:00:00Z")));
});

test("Schedules has the same bar: its App menu from the jobs' claims, kept for the session; kind in the address", async () => {
  const { ctx, out, store } = setup({ "/api/schedules": [cron({ claims: ["frigate-config"] }), cron({ name: "lh-backup", owner: "longhorn" })], "/api/workloads": APPS },
    { search: "" });
  await ctx.viewSchedules();
  assert.match(out.html, /class="tl-filters"/);
  assert.match(out.html, /<option value="lab\/frigate">/);
  assert.doesNotMatch(out.html, /aria-label="Host"/, "no job names a host, so there is no Host menu");
  ctx.schedulesFilter("kind", "system");
  assert.match(out.urls.at(-1), /kind=system/);
  assert.doesNotMatch(out.html, /<b>db-dump<\/b>/);
  assert.match(out.html, /<b>lh-backup<\/b>/);
  ctx.schedulesFilter("app", "lab/frigate");
  assert.equal(JSON.parse(store["homestead.schedule.filter"]).app, "lab/frigate");
  assert.match(out.html, /Nothing matches/);
});

test("the image is under the name, not a column of its own", async () => {
  const { ctx, out } = setup({ "/api/schedules": [cron({ image: "postgres:16" })], "/api/workloads": APPS });
  await ctx.viewSchedules();
  assert.doesNotMatch(out.html, /<th>Image<\/th>/);
  assert.match(out.html, /<b>db-dump<\/b>[\s\S]*postgres:16[\s\S]*<\/td>\s*<td class="small">/);
});

test("the demo has a failed job and one that did not run, and the page audit has a filtered variant of each page", () => {
  const demo = between(fs.readFileSync("web/js/demo.js", "utf8"), '"/api/schedules": [', '"/api/events"');
  assert.match(demo, /result: "failed"/);
  assert.match(demo, /missed: true/);
  const audit = fs.readFileSync("scripts/audit_pages.mjs", "utf8");
  assert.match(audit, /\["schedules-week-backups", "schedule-week", "weekFilter\('kind', 'backup'\)"\]/);
  assert.match(audit, /\["schedules-system", "schedules", "schedulesFilter\('kind', 'system'\)"\]/);
});

test("a job an alert links to (?find=) shows whatever App was chosen this session", async () => {
  const { ctx, out } = setup({ "/api/schedules": [cron(), cron({ name: "other", claims: ["frigate-config"] })], "/api/workloads": APPS },
    { search: "?find=lab/db-dump", session: { "homestead.schedule.filter": JSON.stringify({ app: "lab/frigate" }) } });
  ctx.location.pathname = "/schedules";
  await ctx.viewSchedules();
  assert.match(out.html, /class="flash-find"[^>]*>\s*<td><b>db-dump<\/b>/);
  assert.match(out.html, /<b>other<\/b>/);
});

test("review I1: when the apps or the volumes cannot be read, every run is still listed and only the names are said to be missing", async () => {
  const { ctx, out } = setup({ ...QUIET, "/api/lh/overview": LH, "/api/schedules": [cron({ claims: ["paperless-data"] })],
    "/api/workloads": new Error("forbidden"), "/api/volumes": new Error("timed out") });
  await ctx.viewScheduleWeek();
  assert.match(out.html, /<b>frigate-backup<\/b>/);
  assert.match(out.html, /<b>db-dump<\/b>/);
  assert.doesNotMatch(out.html, /schedules are missing/);
  assert.doesNotMatch(out.html, /Some schedules are missing/);
  assert.match(out.html, /Could not read the apps list: no run names its app/);
  assert.match(out.html, /Could not read volume copies: no run names its host/);
});

test("review I2: a Host chosen on This week does not empty Schedules, which filters on App only", async () => {
  const { ctx, out } = setup({ "/api/schedules": [cron(), cron({ name: "other", claims: ["frigate-config"] })], "/api/workloads": APPS },
    { session: { "homestead.schedule.filter": JSON.stringify({ host: "node-1" }) } });
  await ctx.viewSchedules();
  assert.match(out.html, /<b>db-dump<\/b>/);
  assert.match(out.html, /<b>other<\/b>/);
  assert.doesNotMatch(out.html, /aria-label="Host"/);
  ctx.schedulesFilter("host", "node-2");
  assert.match(out.html, /<b>db-dump<\/b>/, "Schedules takes no Host");
});

test("review M1: a CronJob in another namespace says Homestead cannot see its app, and an App filter says it may hide such runs", async () => {
  const elsewhere = { ...cron({ name: "backup-photos", ns: "media" }) };
  delete elsewhere.claims;   // as the server sends it for another namespace (#739)
  const answers = { ...QUIET, "/api/lh/overview": LH, "/api/schedules": [elsewhere], "/api/workloads": APPS };
  const { ctx, out } = setup(answers);
  await ctx.viewScheduleWeek();
  const row = out.html.split("<tr").find(r => r.includes("<b>backup-photos</b>"));
  assert.match(row, /data-tip="Homestead can&#39;t see which app this touches \(another namespace\)"|data-tip="Homestead can't see which app this touches \(another namespace\)"/);
  ctx.weekFilter("app", "lab/frigate");
  assert.doesNotMatch(out.html, /<b>backup-photos<\/b>/);
  assert.match(out.html, /\d+ runs? in other namespaces may touch it: Homestead can't see which app they touch/);
  const s = setup({ "/api/schedules": [elsewhere, cron({ claims: ["frigate-config"] })], "/api/workloads": APPS },
    { session: { "homestead.schedule.filter": JSON.stringify({ app: "lab/frigate" }) } });
  await s.ctx.viewSchedules();
  assert.match(s.out.html, /1 job in another namespace may touch it: Homestead can't see which app it touches/);
});

test("review M2: the Schedules summary line follows the filter", async () => {
  const { ctx, out } = setup({ "/api/schedules": [cron({ result: "failed" }), cron({ name: "lh-backup", owner: "longhorn" })], "/api/workloads": APPS });
  await ctx.viewSchedules();
  assert.match(out.html, /2 jobs: 1 failed, 1 ok/);
  ctx.schedulesFilter("kind", "system");
  assert.match(out.html, /1 job: 1 ok/);
});

test("review M4: Longhorn's own CronJobs are never judged for a missed run, so an untold one is not unknown", () => {
  const { ctx } = setup();
  assert.equal(ctx.jobState(cron({ owner: "longhorn", missed: null })), "ok");
  assert.equal(ctx.jobState(cron({ owner: "", missed: null })), "unknown");
});

// Re-review: what an unread apps list costs, said once per source and only when it costs it.
const STOP_LH = { clashes: [], volumes: [{ name: "pvc-db", pvc: "db-data", namespace: "lab" }],
  jobs: [{ name: "nightly", task: "backup", cron: "0 2 * * *", volumes: ["pvc-db"] }] };
const STOPS = { items: [{ kind: "app", ns: "lab", name: "db", schedule: { stop: "02:10", start: "07:00", days: [0, 1, 2, 3, 4, 5, 6], tz: "UTC" } }] };
const window_ = (policy, rebalance, maintenance = { days: [0, 1, 2, 3, 4, 5, 6], start: "02:00", duration_minutes: 60 }) =>
  ({ updates: { policy, ...(maintenance ? { maintenance } : {}) }, placement: { rebalance } });

test("re-review 1: with power stops and the apps unread, the page says the stop-during-backup check was not done, and claims no quiet week", async () => {
  const { ctx, out } = setup({ ...QUIET, "/api/lh/overview": STOP_LH, "/api/schedules": [], "/api/power-schedules": STOPS,
    "/api/workloads": new Error("forbidden") });
  await ctx.viewScheduleWeek();
  assert.match(out.html, /Could not read the apps list: no run names its app; whether an app.s power stop falls in its backup was not checked\./);
  assert.doesNotMatch(out.html, /No clashes this week/);
  assert.equal((out.html.match(/Could not read the apps list/g) || []).length, 1, "one line for the one failed read");
});

test("re-review 2: the window is said to be maybe missing only when it is set and not listed already, in one line", async () => {
  const page = async settings => { const { ctx, out } = setup({ ...QUIET, "/api/settings": settings, "/api/lh/overview": { jobs: [], clashes: [] },
    "/api/schedules": [], "/api/workloads": new Error("forbidden") }); await ctx.viewScheduleWeek(); return out.html; };
  for (const [what, settings] of [["no window", window_("auto", "off", null)], ["a balance-only window", window_("auto", "window")]]) {
    const html = await page(settings);
    assert.doesNotMatch(html, /missing/, `${what}: nothing is missing`);
    assert.equal((html.match(/Could not read the apps list/g) || []).length, 1, `${what}: one line`);
  }
  const html = await page(window_("auto", "off"));
  assert.match(seen(html), /Could not read the apps list: no run names its app\. The maintenance window may be missing: whether an app updates itself in it is not known\./);
  assert.equal((seen(html).match(/Could not read the apps list/g) || []).length, 1, "a window that may be missing: still one line");
});

test("re-review minor: the hidden count of another namespace's runs counts runs, an all-day job by its runs", async () => {
  const elsewhere = { ...cron({ name: "sync", ns: "media", schedule: "*/30 * * * *" }) };
  delete elsewhere.claims;
  const { ctx, out } = setup({ ...QUIET, "/api/lh/overview": LH, "/api/schedules": [elsewhere], "/api/workloads": APPS });
  await ctx.viewScheduleWeek();
  ctx.weekFilter("app", "lab/frigate");
  assert.match(out.html, /About 336 runs in other namespaces may touch it/);
});

test("re-review 3: a window with no days never runs, so an unread apps list does not make it maybe missing", () => {
  const { W } = setup();
  const week = W.collect({ settings: window_("auto", "off", { days: [], start: "02:00", duration_minutes: 60 }), workloads: { failed: "x" } }, FROM);
  assert.equal(week.windowUnknown, false);
  assert.ok(!week.effects["the apps list"].some(e => /may be missing/.test(e)));
});
