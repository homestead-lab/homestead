"use strict";
// Volumes (Storage) draws each part as its own read returns (#705): every read
// starts at once, the page is drawn when the volumes answer, and the totals, the
// forecast, old copies and other volumes fill in as they arrive.
const test = require("node:test"), assert = require("node:assert/strict");
const fs = require("node:fs"), vm = require("node:vm");
const src = fs.readFileSync("web/js/views-storage.js", "utf8");

const VOLUME = { name: "pvc-1", pvc_name: "paperless-data", namespace: "lab", state: "attached", robustness: "healthy",
  replicas: 2, copies: [], access_modes: ["ReadWriteOnce"], engine: "v1", size_gb: 10, actual_gb: 2 };
const ANSWERS = {
  "/api/volumes": [VOLUME],
  "/api/storage": { avail_gb: 321, cap_gb: 400, provisioned_gb: 10, actual_gb: 2, used_pct: 20, healthy: 1, degraded: 0,
    faulted: 0, attached: 1, volumes: 1, disks: [] },
  "/api/storage/classes": [], "/api/storage/v2": { enabled: false },
  "/api/longhorn/capacity": null,
  "/api/storage/forecast": { rows: [], min_days: 7, show_days: 60, warn_days: 14 },
  "/api/volumes/old-copies": [{ pv: "pvc-old-1", was: "lab/old-data", storage_class: "longhorn", size: "5Gi" }],
  "/api/volumes/other": [{ namespace: "lab", name: "scratch-cache", storage_class: "local-path", size: "1Gi",
    access_modes: ["ReadWriteOnce"], phase: "Bound" }],
};
const PATHS = Object.keys(ANSWERS);

function page() {
  const reads = [], waiting = {}, paints = [];
  const ctx = { console, setTimeout, clearTimeout, Promise, JSON, CSS: { escape: String },
    STATE: { data: {}, q: "", view: "storage" }, NAV_TOKEN: 1,
    document: { querySelector: () => null, getElementById: () => null },
    platformLacks: () => false, paint: html => paints.push(html), moreMenu: () => "", meter: () => "",
    fmtAgo: String, clusterAttr: () => "", clusterTag: () => "", tip: () => "", icon: () => "",
    esc: s => String(s ?? "").replaceAll("&", "&amp;").replaceAll("<", "&lt;").replaceAll('"', "&quot;"),
    api: path => new Promise((resolve, reject) => { reads.push(path); (waiting[path] ||= []).push({ resolve, reject }); }) };
  ctx.jsq = s => ctx.esc(JSON.stringify(s)); ctx.window = ctx;
  vm.createContext(ctx); require("./helpers/load-ui")(ctx);
  vm.runInContext(src, ctx);
  const answer = (path, value = ANSWERS[path]) => waiting[path].shift().resolve(JSON.parse(JSON.stringify(value)));
  const fail = (path, message) => waiting[path].shift().reject(new Error(message));
  return { ctx, reads, paints, answer, fail, last: () => paints[paints.length - 1] || "" };
}
const settle = () => new Promise(resolve => setTimeout(resolve, 60));

test("every read of the page starts at once, not in two waves", async () => {
  const { ctx, reads } = page();
  ctx.viewStorage();
  await settle();
  assert.deepEqual([...reads].sort(), [...PATHS].sort());
});

test("the volumes are drawn while slower parts are still being read", async () => {
  const { ctx, paints, answer, last } = page();
  ctx.viewStorage();
  await settle();
  answer("/api/volumes");
  await settle();
  assert.equal(paints.length, 1, "drawn when the volumes answer");
  assert.match(last(), /paperless-data/);
  assert.match(last(), /Reading the totals/);
  assert.doesNotMatch(last(), /321/);
});

test("each part is drawn when its own read returns", async () => {
  const { ctx, answer, last } = page();
  const done = ctx.viewStorage();
  await settle();
  answer("/api/volumes"); await settle();
  answer("/api/storage"); await settle();
  assert.match(last(), /<b>321 GiB<\/b> free of 400/);
  assert.doesNotMatch(last(), /old-data/);
  answer("/api/volumes/old-copies"); await settle();
  assert.match(last(), /old-data/);
  for (const p of ["/api/storage/classes", "/api/storage/v2", "/api/longhorn/capacity", "/api/storage/forecast", "/api/volumes/other"]) answer(p);
  await done;
  assert.match(last(), /scratch-cache/);
  assert.match(last(), /Filling up/);
  assert.doesNotMatch(last(), /Reading the/);
});

test("parts that answer together are drawn in one paint", async () => {
  const { ctx, paints, answer } = page();
  const done = ctx.viewStorage();
  await settle();
  for (const p of PATHS) answer(p);
  await done;
  assert.equal(paints.length, 1);
});

test("a part that could not be read is named above the page", async () => {
  const { ctx, answer, fail, last } = page();
  const done = ctx.viewStorage();
  await settle();
  for (const p of PATHS) p === "/api/volumes/old-copies" ? fail(p, "timed out") : answer(p);
  await done;
  assert.match(last(), /Parts of this page could not be read[^]*old copies: timed out/);
});

test("a refresh keeps each part's last answer until the new one arrives", async () => {
  const { ctx, answer, last } = page();
  let done = ctx.viewStorage();
  await settle();
  for (const p of PATHS) answer(p);
  await done;
  done = ctx.viewStorage();
  await settle();
  answer("/api/volumes"); await settle();
  assert.match(last(), /321 GiB/, "the totals stay while they are read again");
  assert.match(last(), /old-data/);
  assert.doesNotMatch(last(), /Reading the/);
});

test("another visit to the page does not show the last visit's parts as current", async () => {
  const { ctx, answer, last } = page();
  let done = ctx.viewStorage();
  await settle();
  for (const p of PATHS) answer(p);
  await done;
  ctx.NAV_TOKEN++;
  ctx.viewStorage();
  await settle();
  answer("/api/volumes"); await settle();
  assert.match(last(), /Reading the totals/);
  assert.doesNotMatch(last(), /old-data/);
});

test("a load overtaken by a newer one draws nothing", async () => {
  const { ctx, paints, answer } = page();
  ctx.viewStorage();
  await settle();
  const newer = ctx.viewStorage();
  await settle();
  for (const p of PATHS) answer(p);       // the first load's answers
  await settle();
  assert.equal(paints.length, 0);
  for (const p of PATHS) answer(p);       // the newer load's
  await newer;
  assert.equal(paints.length, 1);
});

test("a part that failed is still named while it is read again on a refresh", async () => {
  const { ctx, answer, fail, last } = page();
  let done = ctx.viewStorage();
  await settle();
  for (const p of PATHS) p === "/api/volumes/old-copies" ? fail(p, "timed out") : answer(p);
  await done;
  ctx.viewStorage();
  await settle();
  answer("/api/volumes"); await settle();
  assert.match(last(), /old copies: timed out/, "not shown as no old copies (#620)");
});

test("a part that answers on the refresh is no longer named as unread", async () => {
  const { ctx, answer, fail, last } = page();
  let done = ctx.viewStorage();
  await settle();
  for (const p of PATHS) p === "/api/volumes/old-copies" ? fail(p, "timed out") : answer(p);
  await done;
  done = ctx.viewStorage();
  await settle();
  for (const p of PATHS) answer(p);
  await done;
  assert.doesNotMatch(last(), /could not be read/);
  assert.match(last(), /old-data/);
});

test("a page that cannot be drawn fails its load rather than hanging it", async () => {
  const { ctx, answer } = page();
  const done = ctx.viewStorage();
  await settle();
  ctx.storagePaint = () => { throw new Error("broken markup"); };
  for (const p of PATHS) answer(p);
  const outcome = await Promise.race([done.then(() => "settled", e => e.message), new Promise(r => setTimeout(() => r("hung"), 500))]);
  assert.equal(outcome, "broken markup");
});
