"use strict";
// Pins the Create VM review before it moves onto UI.reviewFlow (#909): the dialog, the acknowledgement and the
// button's state at each step, requests, toasts, the Service VIP that follows, a refused create, Back, an older
// preview and a review asked for while one is being sent.
const fs = require("node:fs"), vm = require("node:vm");
const pins = require("./helpers/review-pins");

function world({ blocked = false, missing = false, fail = false, routine = false } = {}) {
  const log = [], state = { gate: null, fail };
  const fields = { "#vmCreateApprove": { checked: false }, "#vmCreateApply": { disabled: true, textContent: "Create VM" },
    "#mbody": { insertAdjacentHTML: (_w, html) => log.push(["insertAdjacentHTML", html]) } };
  if (routine) delete fields["#vmCreateApprove"];
  const ctx = { console, URLSearchParams, Map, Date, Promise, encodeURIComponent,
    document: { addEventListener() {}, getElementById: id => fields["#" + id] || null }, STATE: { data: {} }, $: key => fields[key], $$: () => [],
    esc: v => String(v).replaceAll("<", "&lt;"), toast: (text, tone) => log.push(["toast", text, tone]), go: view => log.push(["go", view]),
    closeModal: () => log.push(["closeModal"]), modalBack: () => log.push(["modalBack"]), defaultNamespace: () => "lab",
    childModal: (title, html) => log.push(["childModal", title, html]),
    deployCapacityHtml: p => `<div>${p.warnings.join(" ")}</div>`,
    capacityNotes: p => routine ? { concerns: [], caveats: p.warnings } : { concerns: p.warnings, caveats: [] },
    networkExpose: async (...args) => { log.push(["networkExpose", args]); if (state.exposeFails) throw new Error("no VIP"); },
    api: async (path, options) => {
      const body = JSON.parse(options.body); log.push(["api", path, body]);
      if (path.endsWith("/preview")) { if (state.preview) return state.preview(body);
        return { config: missing ? null : { ...body, mac: "52:54:00:11:22:33" }, capacity_token: "signed-create", volumes: [{ name: "disk", size: "10Gi", storage_class: "fast", access_mode: "RWX", volume_mode: "Block" }],
          capacity: { blocked, warnings: ["high RAM"], blockers: blocked ? ["missing KVM"] : [] } }; }
      if (state.gate) await state.gate;
      if (state.fail) throw new Error("connection lost");
      return { ok: true, address: "192.0.2.5", operation: { id: "create-job" } };
    } };
  ctx.window = ctx; vm.createContext(ctx); const notes = ctx.capacityNotes; require("./helpers/load-ui")(ctx);
  vm.runInContext(require("./helpers/workloads-source")(), ctx); ctx.capacityNotes = notes;
  ctx.jsArg = s => JSON.stringify(String(s ?? "")); ctx.jsq = s => (ctx.esc || String)(ctx.jsArg(s));
  vm.runInContext(require("./helpers/lifecycle-source")(), ctx);
  ctx.noteOperation = op => log.push(["noteOperation", op]); ctx.refreshOperations = () => log.push(["refreshOperations"]);
  const snap = label => log.push(["state", label, fields["#vmCreateApply"].disabled, fields["#vmCreateApply"].textContent]);
  return { ctx, fields, log, state, snap };
}
const cfg = () => ({ name: "guest", namespace: "lab", memory: "2Gi", password: "do-not-display-this", network: "pod" });

pins("vm create review", "review-pins-vm-create", {
  async gateFrozenInputAndSendOnce() {
    const w = world(), c = cfg(); await w.ctx.vmCreateReview(c); w.snap("reviewed");
    await w.ctx.vmCreateReviewedApply(); w.snap("without the tick");
    c.memory = "64Gi"; w.fields["#vmCreateApprove"].checked = true; w.ctx.vmCreateReviewReady(); w.snap("ticked");
    let release; w.state.gate = new Promise(r => { release = r; });
    const a = w.ctx.vmCreateReviewedApply(), b = w.ctx.vmCreateReviewedApply(); w.snap("sending");
    release(); await a; await b; w.snap("sent");
    return w.log;
  },
  async routineCaveatsNeedNoTick() {
    const w = world({ routine: true }); await w.ctx.vmCreateReview(cfg()); w.snap("reviewed");
    await w.ctx.vmCreateReviewedApply(); w.snap("sent");
    return w.log;
  },
  async blockedIncompleteAndFailedPreviews() {
    const out = [];
    for (const options of [{ blocked: true }, { missing: true }]) {
      const w = world(options); await w.ctx.vmCreateReview(cfg()); w.snap("reviewed");
      if (w.fields["#vmCreateApprove"]) w.fields["#vmCreateApprove"].checked = true; await w.ctx.vmCreateReviewedApply(); out.push(...w.log);
    }
    const w = world(); w.state.preview = () => { throw new Error("Not found"); }; await w.ctx.vmCreateReview(cfg()); out.push(...w.log);
    return out;
  },
  async serviceVipFollowsTheCreate() {
    const w = world(); await w.ctx.vmCreateReview(cfg(), { serviceMode: "manual", selectedVip: "192.0.2.9" });
    w.fields["#vmCreateApprove"].checked = true; await w.ctx.vmCreateReviewedApply();
    const v = world(); v.state.exposeFails = true; await v.ctx.vmCreateReview(cfg(), { serviceMode: "manual", selectedVip: "192.0.2.9" });
    v.fields["#vmCreateApprove"].checked = true; await v.ctx.vmCreateReviewedApply();
    return [...w.log, ...v.log];
  },
  async refusedCreateMustBeInspected() {
    const w = world({ fail: true }); await w.ctx.vmCreateReview(cfg()); w.fields["#vmCreateApprove"].checked = true;
    await w.ctx.vmCreateReviewedApply(); w.snap("failed"); await w.ctx.vmCreateReviewedApply(); w.snap("tried again");
    return w.log;
  },
  async backDropsALateReview() {
    const w = world(); let finish; const original = w.ctx.api;
    w.ctx.api = (path, options) => new Promise(r => { finish = r; });
    const pending = w.ctx.vmCreateReview(cfg()); w.ctx.vmCreateReviewBack(); finish({ config: { mac: "m", name: "guest" }, capacity_token: "t", volumes: [], capacity: { blocked: false, warnings: [] } });
    await pending; w.snap("after the late reply");
    w.ctx.api = original; await w.ctx.vmCreateReview(cfg()); w.ctx.vmCreateReviewBack(); w.snap("backed out");
    return w.log;
  },
  async backAndANewReviewWhileSending() {
    const w = world(); await w.ctx.vmCreateReview(cfg()); w.fields["#vmCreateApprove"].checked = true;
    let release; w.state.gate = new Promise(r => { release = r; });
    const pending = w.ctx.vmCreateReviewedApply(); w.ctx.vmCreateReviewBack(); await w.ctx.vmCreateReview({ ...cfg(), name: "other" }); release(); await pending; w.snap("after");
    return w.log;
  },
  async olderPreviewCannotReplaceANewerOne() {
    const w = world(); let first; const original = w.ctx.api;
    w.ctx.api = (path, options) => JSON.parse(options.body).name === "old" ? new Promise(r => { first = r; }) : original(path, options);
    const old = w.ctx.vmCreateReview({ ...cfg(), name: "old" }); await w.ctx.vmCreateReview({ ...cfg(), name: "new" });
    first({ config: { mac: "m", name: "old" }, capacity_token: "t", volumes: [], capacity: { blocked: false, warnings: [] } }); await old;
    w.fields["#vmCreateApprove"].checked = true; await w.ctx.vmCreateReviewedApply(); w.snap("sent");
    return w.log;
  },
});
