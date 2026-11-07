"use strict";
// Pins the image update / rollback review (and the rollout queue it starts) before the review moves onto
// UI.reviewFlow (#909): the review dialog, the tick and the button, the requests it sends, the queue's final
// text for each way a rollout can go, an older review, a review the dialog has left and a queue another dialog replaced.
const fs = require("node:fs"), vm = require("node:vm");
const pins = require("./helpers/review-pins"), { settle } = pins;
const updateState = require("../web/js/update-state.js");

function world({ blocked = false, fail = "", missing = false } = {}) {
  const log = [], sent = [], state = { gate: null };
  const fields = { "#imageCapacityApprove": { checked: false }, "#imageCapacityApply": { disabled: true }, "#modal": { classList: { contains: () => false } } };
  const ctx = { console, URLSearchParams, Map, Date, Promise, encodeURIComponent,
    document: { addEventListener() {}, getElementById: id => fields["#" + id] || null }, STATE: { data: {} }, HomesteadUpdateState: updateState,
    $: id => fields[id], $$: () => [], esc: s => String(s).replaceAll("<", "&lt;"),
    toast: (text, tone) => log.push(["toast", text, tone]), setTimeout: cb => cb(),
    modal: (title, body) => {
      log.push(["modal", title]);
      delete fields["#imageReviewLoading"]; delete fields["#imageQueue"];
      if (body.includes('id="imageReviewLoading"')) fields["#imageReviewLoading"] = {};
      if (body.includes('id="imageQueue"')) fields["#imageQueue"] = { innerHTML: "" };
      fields["#mbody"] = { _html: body, get innerHTML() { return this._html; }, set innerHTML(v) { this._html = v; } };
    },
    api: async (path, options) => {
      const body = options?.body ? JSON.parse(options.body) : null; sent.push({ path, body }); log.push(["api", path, body]);
      if (path.endsWith("/preview")) { if (state.gate) await state.gate; return { capacity: missing ? null : { blocked, warnings: ["High RAM"], candidates: [] },
        capacity_token: "signed-" + body.name, images: [{ container: "app", before: "old", after: "new@digest", rollback: "old@digest" }] }; }
      if (path.includes("/progress")) {
        if (fail === "contact") throw new Error("connection lost");
        if (fail === "blip" && sent.filter(s => s.path.includes("/progress")).length <= 2) throw new TypeError("Failed to fetch");
        if (fail === "refused") throw Object.assign(new Error("your role (viewer) cannot do this"), { status: 403 });
        return { uid: fail === "replacement" ? "different" : "workload-uid", generation: 4, phase: fail === "rollout" ? "failed" : "ready", ready: 1, desired: 1 };
      }
      if (fail === "apply") throw new Error("changed target");
      return { uid: "workload-uid", generation: 4, phase: "starting", ready: 0, desired: 1 };
    } };
  ctx.window = ctx; vm.createContext(ctx); require("./helpers/core-text")(ctx);
  vm.runInContext(fs.readFileSync("web/js/metrics-utils.js", "utf8"), ctx);
  ctx.jsArg = s => JSON.stringify(String(s ?? "")); ctx.jsq = s => (ctx.esc || String)(ctx.jsArg(s));
  vm.runInContext(fs.readFileSync("web/js/ui.js", "utf8"), ctx);
  vm.runInContext(require("./helpers/workloads-source")(), ctx);
  ctx.loadImageUpdates = async () => null; ctx.renderWorkloads = () => {};
  const snap = label => log.push(["state", label, fields["#mbody"]?.innerHTML ?? null, fields["#imageCapacityApply"].disabled, fields["#imageQueue"]?.innerHTML ?? null]);
  return { ctx, fields, log, sent, state, snap };
}

pins("image update review", "review-pins-image-update", {
  async singleUpdateGateAndConsume() {
    const w = world(); await w.ctx.imageUpdateReview("lab", "app"); w.snap("reviewed");
    await w.ctx.imageReviewedApply(); w.snap("without the tick");
    w.fields["#imageCapacityApprove"].checked = true; w.ctx.imageReviewReady(); w.snap("ticked");
    await w.ctx.imageReviewedApply(); w.snap("applied"); await w.ctx.imageReviewedApply(); w.snap("replayed");
    return w.log;
  },
  async blockedAndIncompletePlans() {
    const out = [];
    for (const options of [{ blocked: true }, { missing: true }]) {
      const w = world(options); await w.ctx.imageUpdateReview("lab", "app"); w.snap("reviewed");
      if (w.fields["#imageCapacityApprove"]) w.fields["#imageCapacityApprove"].checked = true; await w.ctx.imageReviewedApply(); out.push(...w.log);
    }
    return out;
  },
  async rollbackUsesTheSameFlow() {
    const w = world(); await w.ctx.imageRollback("lab", "app"); w.snap("reviewed");
    w.fields["#imageCapacityApprove"].checked = true; await w.ctx.imageRollbackApply(); w.snap("queue");
    return w.log;
  },
  async batchAndEachWayAQueueEnds() {
    const out = [];
    for (const fail of ["", "apply", "rollout", "contact", "replacement", "blip", "refused"]) {
      const w = world({ fail }); await w.ctx.reviewImageActions([{ ns: "lab", name: "homestead" }, { ns: "lab", name: "app" }]); w.snap("reviewed");
      w.fields["#imageCapacityApprove"].checked = true; await w.ctx.imageReviewedApply(); w.snap("queue " + fail);
      out.push(...w.log);
    }
    return out;
  },
  async olderReviewCannotReplaceANewerOne() {
    const w = world(); let release; w.state.gate = new Promise(r => { release = r; });
    const old = w.ctx.imageUpdateReview("lab", "old"); const original = w.ctx.api;
    w.state.gate = null; await w.ctx.imageUpdateReview("lab", "new"); release(); await old; w.snap("after the late review");
    w.fields["#imageCapacityApprove"].checked = true; await w.ctx.imageReviewedApply(); w.snap("queue");
    return w.log;
  },
  async aDialogThatMovedOnDropsTheReview() {
    const w = world(); const pending = w.ctx.reviewImageActions([{ ns: "lab", name: "a" }, { ns: "lab", name: "b" }]);
    delete w.fields["#imageReviewLoading"]; await pending; w.snap("dropped");
    return w.log;
  },
  async anotherReviewStopsARunningQueue() {
    const w = world(); await w.ctx.reviewImageActions([{ ns: "lab", name: "a" }, { ns: "lab", name: "b" }]); w.fields["#imageCapacityApprove"].checked = true;
    const original = w.ctx.api; let release, held = false;
    w.ctx.api = async (path, options) => { if (path.includes("/progress") && !held) { held = true; await new Promise(r => { release = r; }); } return original(path, options); };
    const running = w.ctx.imageReviewedApply(); await settle();
    await w.ctx.reviewImageActions([{ ns: "lab", name: "c" }]); release(); await running; w.snap("after");
    return w.log;
  },
});
