"use strict";
// Data protection after the Nov 2026 mockup: whether each volume can be
// restored, in words (views-protect-volumes.js) - the summary line, a row a
// volume, the schedules table and the volume's details sidebar.
const test = require("node:test"), assert = require("node:assert/strict");
const fs = require("node:fs"), vm = require("node:vm");

const daysAgo = n => new Date(Date.now() - n * 86400e3).toISOString();
const LH = () => ({ total: 3, groups: ["critical", "default", "media"], target: { configured: true, available: true, url: "s3://backups@us-east-1/" },
  jobs: [
    { name: "nightly-backup", task: "backup", cron: "0 2 * * *", retain: 7, groups: ["default"], volumes: ["pvc-f", "pvc-s"], last_run: daysAgo(60), last_failed: false },
    { name: "hourly-snapshot", task: "snapshot", cron: "0 * * * *", retain: 24, groups: ["critical"], volumes: ["pvc-f"], last_run: "", last_success: "" },
    { name: "weekly-trim", task: "filesystem-trim", cron: "0 4 * * 6", retain: 0, groups: ["media"], volumes: ["pvc-h"], last_run: "" }],
  clashes: [{ severity: "clash", jobs: [{ name: "nightly-backup" }, { name: "weekly-trim" }], volumes: ["pvc-f"], times: [] }],
  volumes: [
    { name: "pvc-f", pvc: "frigate-config", namespace: "lab", size_gb: 20, groups: ["critical", "default"], protected_by: ["nightly-backup", "hourly-snapshot"], last_backup_at: daysAgo(63) },
    { name: "pvc-s", pvc: "scratch", namespace: "lab", size_gb: 5, groups: ["default"], protected_by: ["nightly-backup"], last_backup_at: "" },
    { name: "pvc-h", pvc: "<b>ha</b>", namespace: "lab", size_gb: 2, groups: ["media"], protected_by: [], last_backup_at: "" }] });

function page() {
  const ctx = { console, Promise, JSON, URLSearchParams, Date, location: { search: "", href: "http://example.com/" },
    STATE: { data: {}, q: "", view: "protect" }, document: { addEventListener() {}, getElementById: () => null, querySelector: () => null },
    $: () => null, moreMenu: () => "", meter: () => "", icon: () => "", tip: t => t, nextRun: () => "in 1 h",
    fmtAt: String, fmtWhen: String, fmtDay: () => "19 Sept", bytesText: n => `${n} B`, CRON: { describe: c => c === "0 * * * *" ? "Every hour" : "Every day at 02:00" },
    DETAIL_KINDS: {}, HOVER_CARDS: {},
    esc: s => String(s ?? "").replaceAll("&", "&amp;").replaceAll("<", "&lt;").replaceAll('"', "&quot;") };
  ctx.jsq = s => ctx.esc(JSON.stringify(s)); ctx.window = ctx;
  vm.createContext(ctx); require("./helpers/load-ui")(ctx);
  for (const f of ["web/js/views-protect.js", "web/js/views-protect-volumes.js"]) vm.runInContext(fs.readFileSync(f, "utf8"), ctx);
  ctx.STATE.data.lh = LH();
  return ctx;
}

test("a volume's protection is said in words: snapshots first, then backups, or what little covers it", () => {
  const c = page(), [f, , h] = c.STATE.data.lh.volumes;
  assert.match(c.protectWords(f), /^<div>Snapshot every hour, 24 kept<\/div><div>Backup every day at 02:00, 7 kept<\/div>$/);
  assert.match(c.protectWords(h), /<span class="sum-bad">No snapshots or backups<\/span><div class="ui-reason under xs">Only the weekly trim\.<\/div>/);
  assert.match(c.protectWords({ ...f, protected_by: null }), /ui-chip neutral[^>]*>unknown</);
});

test("each volume's problem: none protects it, a backup that is late or never made", () => {
  const c = page(), [f, s, h] = c.STATE.data.lh.volumes;
  assert.equal(c.protectProblem(h).word, "has no protection");
  assert.equal(c.protectProblem(h).tone, "bad");
  assert.match(c.protectProblem(f).title, /^The last backup is 63 days old$/);
  assert.match(c.protectProblem(f).line, /^nightly-backup should run every day at 02:00, but has not run since 19 Sept\.$/);
  assert.equal(c.protectProblem(s).word, "has never been backed up");
  c.STATE.data.restoreTests = { apps: [{ ns: "lab", name: "frigate", restores: [{ claim: "frigate-config", backup: "b" }], last: { at: 1, ok: false, message: "It did not start." } }] };
  assert.equal(c.protectProblem(f).fix, "test", "a failed restore test comes before a late backup");
});

test("the summary says how recent the backups are and links each volume that needs a person, worst first", () => {
  const c = page();
  const line = c.protectSummary(c.STATE.data.lh);
  assert.match(line, /^<span class="sum-wide"><b>No volume has a backup from the last day\.<\/b> <button type="button" class="linkish sum-link sum-bad" onclick="openDetails\('protect',&quot;pvc-h&quot;\)">&lt;b>ha&lt;\/b> has no protection<\/button>/);
  assert.match(line, /frigate-config's last backup is 63 days old<\/button>/);
  assert.match(c.protectSummary({ ...LH(), jobs_unread: "forbidden" }), /schedules could not be read/);
});

test("a row: a coloured edge, Protect volume when nothing protects it, otherwise Back up now, and the name opens the sidebar", () => {
  const c = page(), [f, , h] = c.STATE.data.lh.volumes;
  const row = c.protectVolumeRow(h);
  assert.match(row, /class="protect-row protect-problem bad"/);
  assert.match(row, /aria-label="Protect volume [^"]*" data-plain="true"[\s\S]*?>Protect volume<\/button>/);
  assert.doesNotMatch(row, /<b>ha<\/b>/, "names are escaped");
  assert.match(c.protectVolumeRow(f), /data-plain="true"[^>]*>Back up now<\/button>/);
  assert.match(c.protectVolumeRow(f), /openDetails\('protect',&quot;pvc-f&quot;\)/);
  assert.match(c.protectLastSnapshot(f), /Not taken yet/);
  assert.match(c.protectLastSnapshot(h), /None/);
  assert.match(c.protectLastBackup(f), /class="sum-warn"[^>]*>19 Sept, 63 days ago<\/span><div class="ui-reason under xs">nightly-backup has not run since\.<\/div>/);
});

test("the Schedules table says what each does, when, for which groups and volumes, and the clash with its fix", () => {
  const c = page(), d = c.STATE.data.lh;
  const table = c.protectSchedulesTable(d);
  assert.match(table, /<b>nightly-backup<\/b>[\s\S]*Backup to the target, 7 kept[\s\S]*frigate-config, scratch[\s\S]*class="sum-warn">60 days ago/);
  assert.match(table, /protect-problem warn/, "a late backup schedule has a coloured edge");
  assert.match(table, /data-plain="true"[^>]*>Run now</);
  assert.match(c.protectClashes(d), /Two schedules start in the same minute[\s\S]*nightly-backup and weekly-trim both start in the same minute on frigate-config\.[\s\S]*Change weekly-trim/);
});

test("a volume's sidebar: Back up now and Restore first, the mockup's tabs, its protection by group", () => {
  const c = page();
  const kind = vm.runInContext("DETAIL_KINDS.protect", c);
  assert.deepEqual([...kind.tabs.map(t => t.label)], ["Overview", "Snapshots", "Backups", "Restore tests"]);
  assert.deepEqual([...kind.actions("pvc-f").slice(0, 2).map(i => i.label)], ["Back up now", "Restore…"]);
  assert.equal(kind.head("pvc-f").sub, "lab · 20 GiB");
  // The restore points are read when the tab opens (details-panel.js); here they are still being read.
  Object.assign(c, { detailsData: () => ({ loading: true }), detailsSlot: () => "<i>reading</i>", volumeProtectRead: () => () => ({}) });
  const overview = kind.tabs[0].html("pvc-f");
  assert.match(overview, /The last backup is 63 days old[\s\S]*>Back up now<\/button>/);
  assert.match(overview, /<dt>Snapshots<\/dt><dd>Every hour, 24 kept · group critical<\/dd>[\s\S]*<dt>Backups<\/dt><dd>Every day at 02:00 to the target, 7 kept · group default<\/dd>/);
});
