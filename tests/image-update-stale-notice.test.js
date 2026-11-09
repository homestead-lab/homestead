"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs"), vm = require("node:vm");
const updateState = require("../web/js/update-state.js");
const settle = () => new Promise(resolve => setImmediate(resolve));
function world() {
  const sent = [];
  const fields = { "#imageCapacityApprove": { checked: false }, "#imageCapacityApply": { disabled: true }, "#modal": { classList: { contains: () => false } } };
  const ctx = { console, URLSearchParams, Map, Date, Promise, encodeURIComponent,
    document: { addEventListener() {}, getElementById: id => fields["#" + id] || null }, STATE: { data: {} }, HomesteadUpdateState: updateState,
    $: id => fields[id], $$: () => [], esc: s => String(s).replaceAll("<", "&lt;"),
    toast() {}, setTimeout: cb => cb(),
    modal: (title, body) => {
      delete fields["#imageReviewLoading"]; delete fields["#imageQueue"]; delete fields["#imageUpdatesLoading"];
      if (body.includes('id="imageUpdatesLoading"')) fields["#imageUpdatesLoading"] = {};
      if (body.includes('id="imageReviewLoading"')) fields["#imageReviewLoading"] = {};
      if (body.includes('id="imageQueue"')) fields["#imageQueue"] = { innerHTML: "" };
      fields["#mbody"] = { _html: body, get innerHTML() { return this._html; }, set innerHTML(v) { this._html = v; } };
    },
    api: async (path, options) => {
      const body = options?.body ? JSON.parse(options.body) : null; sent.push({ path, body });
      if (path.endsWith("/preview")) return {
        capacity: { blocked: false, warnings: [], candidates: [] },
        capacity_token: "signed-" + body.name,
        images: [{ container: "app", before: "old", after: "new@digest", rollback: "old@digest" }]
      };
      return { uid: "workload-uid", generation: 4, phase: "starting", ready: 0, desired: 1 };
    } };
  ctx.window = ctx; vm.createContext(ctx); require("./helpers/core-text")(ctx);
  vm.runInContext(fs.readFileSync("web/js/metrics-utils.js", "utf8"), ctx);
  ctx.jsArg = s => JSON.stringify(String(s ?? "")); ctx.jsq = s => (ctx.esc || String)(ctx.jsArg(s));
  vm.runInContext(fs.readFileSync("web/js/ui.js", "utf8"), ctx);
  vm.runInContext(require("./helpers/workloads-source")(), ctx);
  ctx.loadImageUpdates = async () => null; ctx.renderWorkloads = () => {};
  return { ctx, fields, sent };
}

async function completedUpdate(closeEarly) {
  const w = world();
  let hidden = false, current = false, refreshes = 0, release;
  const gate = new Promise(resolve => { release = resolve; });
  const report = available => ({ channel: "dev", checked_at: "2026-11-08T12:00:00Z",
    updates: available ? 1 : 0, workloads: [{ ns: "lab", name: "app", available,
      images: [{ container: "app", available }] }] });
  w.ctx.STATE.data.imageUpdates = report(true);
  w.fields["#modal"].classList.contains = () => hidden;
  w.ctx.loadImageUpdates = async () => {
    refreshes++;
    return w.ctx.STATE.data.imageUpdates = report(!current);
  };
  const original = w.ctx.api;
  w.ctx.api = async (path, options) => {
    if (path.endsWith("/preview") && current) throw new Error("no image update is currently available");
    if (path.includes("/progress")) {
      await gate;
      if (closeEarly) return {uid: "workload-uid", generation: 4, phase: "starting", ready: 0, desired: 1};
      return {uid: "workload-uid", generation: 4, phase: "ready", ready: 1, desired: 1};
    }
    return original(path, options);
  };
  await w.ctx.imageUpdateReview("lab", "app");
  w.fields["#imageCapacityApprove"].checked = true;
  const running = w.ctx.imageReviewedApply();
  await settle();
  assert.equal(w.sent.filter(x => x.path.endsWith("/apply")).length, 1);
  if (closeEarly) hidden = true;
  release();
  current = true;
  await running;
  await settle();
  return {w, report, refreshes, reopen: () => { hidden = false; }};
}

test("keeping rollout progress open refreshes a completed update", async () => {
  const {w, refreshes} = await completedUpdate(false);
  assert.equal(refreshes, 1);
  assert.equal(w.ctx.STATE.data.imageUpdates.updates, 0);
});

test("closing rollout progress before ready does not leave a completed update selected", async () => {
  const {w, reopen} = await completedUpdate(true);
  reopen();
  // Reproduce the next review: the server knows it is current, but the browser kept the old candidate.
  await w.ctx.imageUpdateReview("lab", "app");
  assert.equal(w.sent.filter(x => x.path.endsWith("/apply")).length, 1, "review cannot apply the update again");
  assert.equal(w.ctx.STATE.data.imageUpdates.updates, 0,
    "the completed update must leave the browser candidate list after review discovers it is current");
  assert.match(w.fields["#mbody"].innerHTML, /The selected update is no longer available/);
  assert.match(w.fields["#mbody"].innerHTML, /No update was started by this review/);
});


test("opening updates replaces a cached candidate with the current server report", async () => {
  const w = world();
  w.ctx.STATE.data.imageUpdates = { updates: 1, workloads: [{ns: "lab", name: "app", available: true}] };
  let reads = 0;
  w.ctx.loadImageUpdates = async (force, quiet) => {
    assert.equal(force, false, "opening does not force a registry scan");
    reads++;
    return w.ctx.STATE.data.imageUpdates = { updates: 0, workloads: [] };
  };
  await w.ctx.imageUpdateCenter();
  assert.equal(reads, 1);
  assert.match(w.fields["#mbody"].innerHTML, /Images are current/);
  assert.doesNotMatch(w.fields["#mbody"].innerHTML, /Review selected/);
});

test("a late update report does not replace another dialog", async () => {
  const w = world();
  let release;
  w.ctx.loadImageUpdates = () => new Promise(resolve => { release = resolve; });
  const opening = w.ctx.imageUpdateCenter();
  w.ctx.modal("Another dialog", "Keep this content");
  release({ updates: 0, workloads: [] });
  await opening;
  assert.equal(w.fields["#mbody"].innerHTML, "Keep this content");
});

test("a failed report read does not offer a cached update", async () => {
  const w = world();
  w.ctx.STATE.data.imageUpdates = { updates: 1, workloads: [{available: true}] };
  await w.ctx.imageUpdateCenter();
  assert.match(w.fields["#mbody"].innerHTML, /could not be checked/);
  assert.doesNotMatch(w.fields["#mbody"].innerHTML, /Review selected/);
});
