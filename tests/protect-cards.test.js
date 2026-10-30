"use strict";
// Data protection draws each part as its own read returns (#705): the page is
// drawn when Longhorn's overview answers, and backup storage and the list of
// backups are filled into their own places when their reads do.
const test = require("node:test"), assert = require("node:assert/strict");
const fs = require("node:fs"), vm = require("node:vm");
const src = fs.readFileSync("web/js/views-protect.js", "utf8");

const ANSWERS = {
  "/api/lh/overview": { total: 1, protected: 1, backed_up: 1, unprotected: [], groups: ["default"], group_rows: [],
    target: { configured: true, available: true, url: "s3://backups@example.com/" }, jobs: [],
    volumes: [{ name: "pvc-1", pvc: "paperless-data", namespace: "lab", size_gb: 10, robustness: "healthy", groups: ["default"], protected_by: ["nightly"] }] },
  "/api/objectstore": { deployed: true, ready: true, endpoint: "http://192.0.2.10:9000" },
  "/api/lh/backupvolumes": [{ name: "pvc-1", pvc: "paperless-data", exists: true, count: 3, last_backup_at: "2026-10-01T02:00:00Z", size_mb: 100 }],
};
const PATHS = Object.keys(ANSWERS);

function page() {
  const reads = [], waiting = {}, paints = [], fills = [];
  const host = id => ({ id, cloneNode: deep => (assert.equal(deep, false), { id, innerHTML: "" }) });
  const hosts = { "protect-top": host("protect-top"), "protect-backups": host("protect-backups") };
  const ctx = { console, setTimeout, clearTimeout, Promise, JSON, URLSearchParams, location: { search: "", href: "http://example.com/" },
    history: { replaceState() {}, state: null }, STATE: { data: {}, q: "", view: "protect" }, NAV_TOKEN: 1,
    document: { addEventListener() {}, getElementById: id => hosts[id] || null, activeElement: null, querySelector: () => null },
    $: () => null, platformLacks: () => false, paint: html => paints.push(html), moreMenu: () => "", meter: () => "",
    tip: t => t, icon: () => "", fmtWhen: String, fmtAt: String, bytesText: String, nextRun: () => "", CRON: { describe: String },
    enhanceActions() {}, loadAppIcons() {}, applyRole() {}, morph: (a, b) => fills.push([a.id, b.innerHTML]),
    esc: s => String(s ?? "").replaceAll("&", "&amp;").replaceAll("<", "&lt;").replaceAll('"', "&quot;"),
    api: path => new Promise((resolve, reject) => { reads.push(path); (waiting[path] ||= []).push({ resolve, reject }); }) };
  ctx.jsq = s => ctx.esc(JSON.stringify(s)); ctx.jsArg = s => JSON.stringify(String(s)); ctx.window = ctx;
  vm.createContext(ctx); require("./helpers/load-ui")(ctx);
  vm.runInContext(src, ctx);
  const answer = path => waiting[path].shift().resolve(JSON.parse(JSON.stringify(ANSWERS[path])));
  const fail = (path, message) => waiting[path].shift().reject(new Error(message));
  const own = new Set(PATHS);
  return { ctx, reads: () => reads.filter(p => own.has(p)), paints, fills, answer, fail, last: () => paints[paints.length - 1] || "" };
}
const settle = () => new Promise(resolve => setTimeout(resolve, 30));

test("the page is drawn when the overview answers, while the backups are still being read", async () => {
  const { ctx, paints, answer, last } = page();
  ctx.viewProtect();
  await settle();
  answer("/api/lh/overview");
  await settle();
  assert.equal(paints.length, 1);
  assert.match(last(), /paperless-data|Coverage/);
  assert.match(last(), /<div id="protect-backups">[^]*Reading the backups/);
  assert.match(last(), /Backup storage[^<]*<span class="dim[^"]*">reading/);
});

test("the backups fill their own section when they answer, without drawing the page again", async () => {
  const { ctx, paints, fills, answer } = page();
  const done = ctx.viewProtect();
  await settle();
  answer("/api/lh/overview"); await settle();
  answer("/api/lh/backupvolumes"); await settle();
  assert.equal(paints.length, 1);
  const [id, html] = fills.find(([id]) => id === "protect-backups");
  assert.equal(id, "protect-backups");
  assert.match(html, /data-sort="backupvols"[^]*paperless-data/);
  answer("/api/objectstore");
  await done;
  const top = fills.find(([id]) => id === "protect-top");
  assert.ok(top, "backup storage fills in the summary");
  assert.match(top[1], /Backup storage[^]*serving/);
  assert.equal(paints.length, 1);
});

test("parts that answered before the overview are in the first paint", async () => {
  const { ctx, paints, fills, answer, last } = page();
  const done = ctx.viewProtect();
  await settle();
  answer("/api/objectstore"); answer("/api/lh/backupvolumes"); await settle();
  answer("/api/lh/overview");
  await done;
  assert.equal(paints.length, 1);
  assert.equal(fills.length, 0);
  assert.match(last(), /data-sort="backupvols"/);
  assert.match(last(), /serving/);
});

test("backups that could not be read say so in their section", async () => {
  const { ctx, fills, answer, fail } = page();
  const done = ctx.viewProtect();
  await settle();
  answer("/api/lh/overview"); await settle();
  fail("/api/lh/backupvolumes", "timed out"); answer("/api/objectstore");
  await done;
  assert.match(fills.find(([id]) => id === "protect-backups")[1], /Could not read the backups[^]*timed out/);
});

test("a refresh keeps the last backups on screen until they are read again", async () => {
  const { ctx, answer, last } = page();
  let done = ctx.viewProtect();
  await settle();
  for (const p of PATHS) answer(p);
  await done;
  ctx.viewProtect();
  await settle();
  answer("/api/lh/overview"); await settle();
  assert.match(last(), /data-sort="backupvols"/);
  assert.doesNotMatch(last(), /Reading the backups/);
});

test("another visit to the page does not show the last visit's backups as current", async () => {
  const { ctx, answer, last } = page();
  let done = ctx.viewProtect();
  await settle();
  for (const p of PATHS) answer(p);
  await done;
  ctx.NAV_TOKEN++;
  ctx.viewProtect();
  await settle();
  answer("/api/lh/overview"); await settle();
  assert.match(last(), /Reading the backups/);
});

test("a load overtaken by a newer one fills nothing", async () => {
  const { ctx, paints, fills, answer } = page();
  const first = ctx.viewProtect();
  await settle();
  answer("/api/lh/overview"); await settle();
  ctx.viewProtect();
  await settle();
  answer("/api/lh/backupvolumes"); answer("/api/objectstore");   // the first load's
  await first;
  assert.equal(fills.length, 0);
  assert.equal(paints.length, 1);
});
