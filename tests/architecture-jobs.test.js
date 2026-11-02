// The Architecture page's scheduled-jobs lane and the volumes it counts as in use.
const test = require("node:test"), assert = require("node:assert/strict"), fs = require("node:fs"), vm = require("node:vm");

function load() {
  const ctx = { console, Date, Map, Set, Promise, document: { addEventListener() {} }, STATE: { data: {} },
    esc: x => String(x).replaceAll("&", "&amp;").replaceAll("<", "&lt;").replaceAll('"', "&quot;"),
    clusterAttr: () => "", fmtAgo: () => "5 h ago", localTime: at => at.toISOString() };
  ctx.window = ctx;
  vm.createContext(ctx);
  require("./helpers/load-time")(ctx);
  vm.runInContext(fs.readFileSync("web/js/ui.js", "utf8"), ctx);
  vm.runInContext(fs.readFileSync("web/js/cron.js", "utf8"), ctx);
  ctx.CRON = ctx.window.CRON || ctx.CRON;
  for (const file of ["web/js/views-protect.js", ...require("./helpers/storage-source").files()]) vm.runInContext(fs.readFileSync(file, "utf8"), ctx);
  return ctx;
}

const job = extra => ({ name: "backup-web", ns: "lab", schedule: "0 2 * * *", time_zone: "", suspended: false,
  last_run: "2026-10-10T02:00:00Z", last_failed: false, claims: [], ...extra });

test("a UTC schedule is put in words with its next run", () => {
  const html = load().scheduledJobs([job({ time_zone: "UTC" })]);
  assert.match(html, /Every day at 02:00 UTC/);
  assert.match(html, /next <span/);
});

test("a schedule with no time zone runs on the cluster's clock and gets no next run", () => {
  const html = load().scheduledJobs([job()]);
  assert.match(html, /Every day at 02:00 on the cluster's clock/);
  assert.doesNotMatch(html, /02:00 UTC/);
  assert.doesNotMatch(html, /next <span/);
});

test("another time zone or a form CRON cannot read is shown as written", () => {
  const ctx = load();
  assert.match(ctx.scheduledJobs([job({ time_zone: "Europe/London" })]), /0 2 \* \* \* \(Europe\/London\)/);
  assert.match(ctx.scheduledJobs([job({ schedule: "@daily", time_zone: "UTC" })]), /@daily/);
});

test("a paused job says so instead of when it runs next", () => {
  assert.match(load().scheduledJobs([job({ time_zone: "UTC", suspended: true })]), /paused/);
});

test("failed last runs are counted on the closed heading", () => {
  const html = load().scheduledJobs([job({ last_failed: true }), job({ name: "backup-db" })]);
  assert.match(html, /<summary[^>]*>Scheduled jobs <span><b class="badtext">1 failed<\/b> · 2<\/span>/);
});

test("a volume only a scheduled job mounts counts as in use", () => {
  const mounted = load().mountedVolumeIds([["w:lab/web", "v:pvc-web", "mount"], ["p:x", "w:lab/web", "access"]],
    [{ claims: [{ pvc: "backups", vid: "v:pvc-backups" }, { pvc: "gone", vid: "" }] }]);
  assert.deepEqual([...mounted].sort(), ["v:pvc-backups", "v:pvc-web"]);
});
