"use strict";
// #927: the Housekeeping protection plan makes no Longhorn trim job (one in
// the default group switches Homestead's own weekly trim off, and fails each
// week on detached volumes and VM disks), and Data protection says when
// Longhorn's recurring jobs could not be read instead of showing none.
const test = require("node:test"), assert = require("node:assert/strict");
const fs = require("node:fs"), vm = require("node:vm");

function view() {
  const ctx = { console, Promise, JSON, URLSearchParams, location: { search: "", href: "http://example.com/" },
    STATE: { data: {}, q: "", view: "protect" }, document: { addEventListener() {}, getElementById: () => null },
    $: () => null, moreMenu: () => "", meter: () => "", icon: () => "", tip: t => t, nextRun: () => "",
    fmtAt: String, fmtWhen: String, CRON: { describe: String },
    esc: s => String(s ?? "").replaceAll("&", "&amp;").replaceAll("<", "&lt;").replaceAll('"', "&quot;") };
  ctx.jsq = s => ctx.esc(JSON.stringify(s)); ctx.window = ctx;
  vm.createContext(ctx); require("./helpers/load-ui")(ctx);
  vm.runInContext(fs.readFileSync("web/js/views-protect.js", "utf8"), ctx);
  return ctx;
}

test("no protection plan makes a Longhorn trim job; Housekeeping keeps the snapshot clean-up and says Homestead trims", () => {
  const ctx = view();
  const plans = vm.runInContext("PROTECT_PLANS", ctx);
  for (const plan of plans) assert.ok(!plan.jobs.some(j => j.task === "filesystem-trim"), `${plan.id} makes no trim job`);
  const tidy = plans.find(p => p.id === "tidy");
  assert.equal(tidy.jobs.map(j => j.task).join(), "snapshot-cleanup");
  assert.match(tidy.blurb, /Homestead trims every attached volume itself/);
});

const UNREAD = { total: 1, protected: null, backed_up: null, unprotected: [], groups: ["default"], group_rows: [], clashes: [],
  target: { configured: true, available: true, url: "s3://backups@example.com/" }, jobs: [],
  jobs_unread: "Longhorn's recurring jobs could not be read: forbidden",
  volumes: [{ name: "pvc-1", pvc: "data", namespace: "lab", groups: ["default"], protected_by: null, backed_up: null, snapshotted: null }] };

test("jobs that could not be read are named, coverage is unknown, and no plan is offered on a guess", () => {
  const ctx = view();
  const top = ctx.protectTop(UNREAD, { deployed: false, unread: "" }, UNREAD.target, null);
  assert.match(top, /Longhorn's recurring jobs could not be read: forbidden/);
  assert.doesNotMatch(top, /% covered/);
  assert.doesNotMatch(top, /null/);
  const body = ctx.protectBody(UNREAD, UNREAD.target, []);
  assert.doesNotMatch(body, /No recurring jobs yet/);
  assert.match(body, /could not be read/);
});

test("with the jobs unread, a volume's coverage is unknown: not 'nothing', and in neither the covered nor the uncovered filter", () => {
  const ctx = view();
  const v = UNREAD.volumes[0];
  const row = ctx.protectVolumeRow(v);
  assert.match(row, /ui-chip neutral[^>]*>unknown</);
  assert.doesNotMatch(row, />nothing</);
  const shows = vm.runInContext("PROTECT_SHOWS", ctx);
  const test = key => shows.find(s => s[0] === key)[2](v);
  assert.equal(test("uncovered"), false);
  assert.equal(test("protected"), false);
  assert.equal(shows.find(s => s[0] === "uncovered")[2]({ protected_by: [] }), true, "a read with no jobs is still not covered");
});

test("with the jobs unread, the Plans and New job actions are held back", () => {
  const ctx = view();
  ctx.STATE.data.lh = UNREAD;
  const seen = [];
  ctx.modal = (title, html) => seen.push(title);
  ctx.toast = (text, tone) => seen.push([text, tone]);
  ctx.lhPlans();
  ctx.lhJob();
  assert.equal(seen.filter(s => Array.isArray(s) && s[1] === "warn" && /could not be read/.test(s[0])).length, 2);
  assert.ok(!seen.includes("Protection plans"));
});
