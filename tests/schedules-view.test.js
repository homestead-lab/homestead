// Containers › Schedules: schedules in words, times in the reader's own zone.
"use strict";
process.env.TZ = "Europe/Berlin";
const test = require("node:test"), assert = require("node:assert/strict");
const fs = require("node:fs"), vm = require("node:vm");
const between = require("./helpers/between");

const core = fs.readFileSync("web/js/core.js", "utf8");

function setup(jobs = []) {
  const out = { calls: [] }, fields = {};
  const field = id => fields[id] ??= { value: "", hidden: false, innerHTML: "", checked: false };
  const ctx = { console, Date, Map, Set, Promise, JSON, URLSearchParams, document: { addEventListener() {} }, STATE: { data: {} },
    esc: x => String(x ?? "").replaceAll("&", "&amp;").replaceAll("<", "&lt;").replaceAll('"', "&quot;"),
    paint: html => { out.html = html; }, modal: (title, body) => { out.title = title; out.modal = body; },
    toast: () => {}, closeModal: () => {}, resetPaint: () => {}, ask: async () => true,
    $: field, $$: () => [], icon: () => "",
    api: async (path, opts) => { if (opts) { out.calls.push({ path, body: JSON.parse(opts.body) }); return {}; } return jobs; } };
  ctx.window = ctx;
  vm.createContext(ctx);
  require("./helpers/load-ui")(ctx);
  ctx.jsArg = s => JSON.stringify(String(s ?? "")); ctx.jsq = s => ctx.esc(ctx.jsArg(s));
  vm.runInContext(between(core, "const fmtUp", "const upChip"), ctx);
  vm.runInContext(fs.readFileSync("web/js/cron.js", "utf8"), ctx);
  vm.runInContext(fs.readFileSync("web/js/views-lifecycle.js", "utf8"), ctx);
  return { ctx, out, fields: field };
}

const job = extra => ({ name: "nightly-db-dump", schedule: "0 3 * * *", time_zone: "Etc/UTC", image: "postgres:16",
  command: "pg_dumpall", last: "2026-09-22T03:00:04Z", suspend: false, active: 0, ...extra });

test("a schedule is shown in words with its cron in the tooltip", async () => {
  const { ctx, out } = setup([job()]);
  await ctx.viewSchedules();
  assert.match(out.html, /Every day at 03:00 UTC/);
  assert.match(out.html, /data-tip="0 3 \* \* \*"/);
  assert.doesNotMatch(out.html, /<td class="mono">0 3 \* \* \*<\/td>/);
});

test("the last run is in the reader's own time, not the bare UTC string", async () => {
  const { ctx, out } = setup([job()]);
  await ctx.viewSchedules();
  assert.doesNotMatch(out.html, />2026-09-22 03:00:04</);
  assert.match(out.html, /05:00/, "03:00 UTC is 05:00 in Berlin");
  assert.match(out.html, /data-tip="2026-09-22 03:00:04 UTC"/);
});

test("a UTC schedule shows when it runs next", async () => {
  const { ctx, out } = setup([job()]);
  await ctx.viewSchedules();
  assert.match(out.html, /<th[^>]*>Next run<\/th>/);
  assert.match(out.html, /in \d+ (min|h|days)/);
});

test("a job with no zone says it runs on the host's clock and gets no next run", async () => {
  const { ctx, out } = setup([job({ time_zone: "" })]);
  await ctx.viewSchedules();
  assert.match(out.html, /Every day at 03:00 on the host's clock/);
  assert.doesNotMatch(out.html, /03:00 UTC/);
  assert.doesNotMatch(out.html, /in \d+ (min|h|days)/);
});

test("a job in another zone names it and gets no computed next run", async () => {
  const { ctx, out } = setup([job({ time_zone: "America/New_York" })]);
  await ctx.viewSchedules();
  assert.match(out.html, /Every day at 03:00 \(America\/New_York\)/);
  assert.doesNotMatch(out.html, /in \d+ (min|h|days)/);
});

test("the schedule editor offers Data Protection's shapes and saves the cron they build", async () => {
  const { ctx, out, fields } = setup();
  ctx.jobEdit();
  assert.match(out.modal, /id="ls_kind"/);
  assert.match(out.modal, /Every day/);
  Object.assign(fields("#j_name"), { value: "nightly" });
  Object.assign(fields("#j_cmd"), { value: "echo hi" });
  Object.assign(fields("#j_image"), { value: "alpine:3.20" });
  Object.assign(fields("#ls_kind"), { value: "daily" });
  Object.assign(fields("#ls_time"), { value: "04:30" });
  ctx.scheduleChanged();
  assert.match(fields("#ls_next").innerHTML, /Every day at 04:30 UTC\. Next: .*\(your time\)/);
  await ctx.jobSave();
  assert.equal(out.calls[0].body.schedule, "30 4 * * *");
});

test("editing a job with no zone warns that saving pins it to UTC", () => {
  const { ctx, out } = setup();
  ctx.jobEdit(job({ time_zone: "" }));
  assert.match(out.modal, /host's clock/);
  assert.match(out.modal, /pins it to UTC/);
});

test("a CronJob Homestead did not make is read-only and says who manages it", async () => {
  const { ctx, out } = setup([job({ name: "helm-backup", managed: false, managed_by: "Helm" }),
    job({ name: "kubectl-job", managed: false, managed_by: "" })]);
  await ctx.viewSchedules();
  assert.match(out.html, /managed by Helm/);
  assert.match(out.html, /not made here/);
  assert.match(out.html, /Run now/);
  assert.doesNotMatch(out.html, /jobEdit\(\{/);
  assert.doesNotMatch(out.html, /jobDel\(/);
});

test("a schedule Homestead made can still be edited and deleted", async () => {
  const { ctx, out } = setup([job({ managed: true, managed_by: "" })]);
  await ctx.viewSchedules();
  assert.match(out.html, /jobEdit\(\{/);
  assert.match(out.html, /jobDel\(/);
  assert.doesNotMatch(out.html, /managed by|not made here/);
});

test("saving sends the generation the page read, and an empty one for a new schedule", async () => {
  const { ctx, out, fields } = setup();
  ctx.jobEdit(job({ managed: true, generation: 41 }));
  Object.assign(fields("#j_name"), { value: "nightly-db-dump" });
  Object.assign(fields("#j_cmd"), { value: "pg_dumpall" });
  await ctx.jobSave();
  assert.equal(out.calls[0].body.generation, 41);
  ctx.jobEdit();
  Object.assign(fields("#j_name"), { value: "fresh" });
  await ctx.jobSave();
  assert.equal(out.calls[1].body.generation, "");
});

// #739: CronJobs from every namespace, and how their last run went.
test("a CronJob in another namespace says where it is and who applied it, and is only listed", async () => {
  const { ctx, out } = setup([job({ name: "backup-photos", ns: "media", owner: "flux", managed: false,
    runnable: false, editable: false })]);
  await ctx.viewSchedules();
  assert.match(out.html, /media/);
  assert.match(out.html, /Applied by Flux/);
  assert.doesNotMatch(out.html, /jobRun\(/, "Run now only in Homestead's own namespace");
  assert.doesNotMatch(out.html, /jobEdit\(\{|jobDel\(/);
});

test("a Longhorn recurring job is named as one", async () => {
  const { ctx, out } = setup([job({ ns: "longhorn-system", owner: "longhorn", managed: false, runnable: false })]);
  await ctx.viewSchedules();
  assert.match(out.html, /Longhorn recurring job/);
});

test("a failed last run is shown with its reason and the last success", async () => {
  const { ctx, out } = setup([job({ result: "failed", result_reason: "BackoffLimitExceeded: Job has reached the specified backoff limit",
    result_at: "2026-09-22T03:04:00Z", last_success: "2026-09-21T03:00:40Z" })]);
  await ctx.viewSchedules();
  assert.match(out.html, /class="ui-chip bad[^"]*"[^>]*>failed</);
  assert.match(out.html, /backoff limit/);
  assert.match(out.html, /last succeeded/);
});

test("a run that did not happen says so", async () => {
  const { ctx, out } = setup([job({ result: "ok", missed: true })]);
  await ctx.viewSchedules();
  assert.match(out.html, /class="ui-chip warn[^"]*"[^>]*>did not run</);
});

test("results that could not be read are unknown, never fine", async () => {
  const { ctx, out } = setup([job({ result: "unknown", result_reason: "Its runs could not be read: forbidden", managed: false })]);
  await ctx.viewSchedules();
  assert.match(out.html, /class="ui-chip warn[^"]*"[^>]*>unknown</);
  assert.match(out.html, /<div class="note[^"]*"[^>]*>[^<]*could not be read: forbidden/, "said once above the table");
  assert.doesNotMatch(out.html, /class="ui-chip ok[^"]*"[^>]*>succeeded</);
});

test("the server's next run is shown for a schedule in another zone", async () => {
  const soon = new Date(Date.now() + 3 * 3600e3).toISOString().replace(/\.\d+Z$/, "Z");
  const { ctx, out } = setup([job({ time_zone: "Europe/London", next_run: soon })]);
  await ctx.viewSchedules();
  assert.match(out.html, /in 3 h/);
});

test("an alert's link finds its CronJob on the page", async () => {
  const { ctx, out } = setup([job({ name: "backup-photos", ns: "media", runnable: false }), job({ name: "other", ns: "lab" })]);
  ctx.location = { search: "?find=media/backup-photos" };
  await ctx.viewSchedules();
  assert.match(out.html, /<tr class="flash-find" data-find="media\/backup-photos">/);
  assert.doesNotMatch(out.html, /<tr class="flash-find" data-find="lab\/other">/);
});
