"use strict";
// Pins the "Change storage class" review before it moves onto UI.reviewFlow (#909): the dialog, its plan, the one
// tick that is cleared when the plan is read again, the target chosen being the one reviewed, the request, an
// uncertain start, a start the dialog was closed on, an older plan and a plan asked for while a start is sent.
const fs = require("node:fs"), vm = require("node:vm");
const pins = require("./helpers/review-pins"), { settle } = pins;

function world({ blocked = false, missing = false, fail = false, live = false } = {}) {
  const log = [], state = { gate: null, fail };
  const fields = { "#rc_plan": { _html: "", get innerHTML() { return this._html; }, set innerHTML(v) { this._html = v; } }, "#rc_to": { value: "new-class", disabled: false }, "#rc_go": { disabled: true }, "#rc_ack": { checked: false } };
  const ctx = { console, Date, Map, Set, Promise, URLSearchParams, encodeURIComponent, document: { addEventListener() {}, getElementById: id => fields["#" + id] || null },
    STATE: { data: { storageClasses: [{ name: "old" }, { name: "new-class", default: true }, { name: "other" }] } }, $: key => fields[key], $$: () => [],
    esc: x => String(x).replaceAll("<", "&lt;"), sizeText: x => x + " GiB", toast: (text, tone) => log.push(["toast", text, tone]), closeModal: () => log.push(["closeModal"]),
    modal: (title, html, wide, context) => log.push(["modal", title, html, wide, context]), applyRole: () => log.push(["applyRole"]),
    noteOperation: op => log.push(["noteOperation", op]), refreshOperations: () => log.push(["refreshOperations"]),
    api: async (path, opts) => {
      const body = JSON.parse(opts.body); log.push(["api", path, body]);
      if (path.endsWith("/plan")) return { ok: !blocked, blockers: blocked ? ["Unsafe"] : [], warnings: ["Capacity unknown"], consumers: [{ name: "<app>", kind: "Deployment", running: true }],
        space: { size_gb: 20, replicas: 2, allocated_gb: 40 }, capacity_token: missing ? "" : "signed", from_class: "old", to_class: "new-class", live: live ? { disk_tags: ["nvme"], node_tags: [] } : undefined };
      if (state.gate) await state.gate;
      if (state.fail) throw Error("connection lost");
      return { operation: { id: "move-id" } };
    } };
  ctx.window = ctx; vm.createContext(ctx); require("./helpers/load-ui")(ctx); ctx.jsArg = s => JSON.stringify(String(s ?? "")); ctx.jsq = s => ctx.esc(ctx.jsArg(s));
  vm.runInContext(require("./helpers/storage-source")(), ctx);
  ctx.reclassWatch = id => log.push(["reclassWatch", id]);
  const snap = label => log.push(["state", label, fields["#rc_plan"]?.innerHTML ?? null, fields["#rc_go"].disabled, fields["#rc_ack"].checked, fields["#rc_to"].disabled]);
  return { ctx, fields, log, state, snap };
}

pins("reclass review", "review-pins-reclass", {
  async openingTheDialogAndPlanning() {
    const w = world({ live: true }); await w.ctx.volumeReclass({ name: "pvc-1", pvc_name: "data", namespace: "lab", storage_class: "old" }); await settle(); w.snap("planned");
    return w.log;
  },
  async gateAndStartOnce() {
    const w = world(); await w.ctx.volumeReclassPlan("lab", "data"); w.snap("planned");
    await w.ctx.volumeReclassStart("lab", "data"); w.snap("without the tick");
    w.fields["#rc_ack"].checked = true; w.ctx.volumeReclassReady(); w.snap("ticked");
    await w.ctx.volumeReclassStart("other", "data"); w.snap("another volume");
    let release; w.state.gate = new Promise(r => { release = r; });
    const a = w.ctx.volumeReclassStart("lab", "data"), b = w.ctx.volumeReclassStart("lab", "data"); w.snap("sending");
    release(); await a; await b; w.snap("started");
    return w.log;
  },
  async blockedMissingAndEditedReviews() {
    const out = [];
    for (const options of [{ blocked: true }, { missing: true }, {}]) {
      const w = world(options); await w.ctx.volumeReclassPlan("lab", "data"); w.fields["#rc_ack"].checked = true;
      if (!options.blocked && !options.missing) w.fields["#rc_to"].value = "changed";
      w.ctx.volumeReclassReady(); w.snap("checked"); await w.ctx.volumeReclassStart("lab", "data"); out.push(...w.log);
    }
    return out;
  },
  async anUncertainStartIsNeverReplayed() {
    const w = world({ fail: true }); await w.ctx.volumeReclassPlan("lab", "data"); w.fields["#rc_ack"].checked = true;
    await w.ctx.volumeReclassStart("lab", "data"); w.snap("failed"); await w.ctx.volumeReclassStart("lab", "data"); w.snap("tried again");
    return w.log;
  },
  async planningAgainClearsTheTick() {
    const w = world(); await w.ctx.volumeReclassPlan("lab", "data"); w.fields["#rc_ack"].checked = true; w.ctx.volumeReclassReady(); w.snap("ticked");
    const again = w.ctx.volumeReclassPlan("lab", "data"); w.snap("planning"); await again; w.snap("planned again");
    return w.log;
  },
  async olderPlanCannotReplaceANewerOne() {
    const w = world(); const original = w.ctx.api; let finish;
    w.ctx.api = () => new Promise(r => { finish = r; });
    const first = w.ctx.volumeReclassPlan("lab", "data"); w.ctx.api = original; w.fields["#rc_to"].value = "latest";
    await w.ctx.volumeReclassPlan("lab", "data"); finish({}); await first; w.fields["#rc_ack"].checked = true; await w.ctx.volumeReclassStart("lab", "data"); w.snap("after");
    return w.log;
  },
  async aDialogClosedDuringTheStart() {
    const w = world(); await w.ctx.volumeReclassPlan("lab", "data"); w.fields["#rc_ack"].checked = true; let finish;
    w.ctx.api = () => new Promise(r => { finish = r; });
    const pending = w.ctx.volumeReclassStart("lab", "data"); delete w.fields["#rc_plan"]; finish({ operation: { id: "move" } }); await pending; w.snap("after");
    return w.log;
  },
  async aPlanWhileTheStartIsSentIsIgnored() {
    const w = world(); await w.ctx.volumeReclassPlan("lab", "data"); w.fields["#rc_ack"].checked = true;
    let release; w.state.gate = new Promise(r => { release = r; });
    const pending = w.ctx.volumeReclassStart("lab", "data"); await w.ctx.volumeReclassPlan("lab", "data"); release(); await pending; w.snap("after");
    return w.log;
  },
});
