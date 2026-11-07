"use strict";
// Pins the linked-cluster move / copy review (Move here / Copy here) before it moves onto UI.reviewFlow (#909):
// the plan drawn into the dialog, Start's state, a re-check when a field changes, the question asked before it
// starts, the request, a plan or field change that arrives while another is being read, and a refused start.
// Also what #1239 added: the switch to stop a VM for its copy, stop_source in both requests, and the three questions.
const fs = require("node:fs"), vm = require("node:vm");
const pins = require("./helpers/review-pins"), { settle } = pins;

function world({ ask = true } = {}) {
  const log = [], replies = [];
  const fields = { "#mv_ns": { value: "lab" }, "#mv_mode": { value: "shared" }, "#mv_sc": { value: "fast", disabled: true },
    "#mv_plan": { _html: "", get innerHTML() { return this._html; }, set innerHTML(v) { this._html = v; } }, "#mv_go": { disabled: true }, "#mv_ip": { value: "", oninput: null }, "#mv_stop_row": { hidden: true } };
  const state = { ask };
  const ctx = { console, URLSearchParams, Map, Set, Date, Promise, encodeURIComponent, clearTimeout, defaultNamespace: () => "lab",
    document: { addEventListener() {}, getElementById: id => fields["#" + id] || null }, STATE: { data: {} }, $: key => fields[key], $$: () => [],
    esc: value => String(value ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c])),
    toast: (text, tone) => log.push(["toast", text, tone]), ask: async (q, o) => { log.push(["ask", q, o]); return state.ask; }, closeModal: () => log.push(["closeModal"]),
    movesRepaint: () => log.push(["movesRepaint"]), tip: t => t, clusterStorage: () => {}, modalBack: () => log.push(["modalBack"]),
    childModal: (title, html, wide, context) => log.push(["childModal", title, html, wide, context]),
    api: (path, opts) => { log.push(["api", path, JSON.parse(opts.body)]); return new Promise((resolve, reject) => replies.push({ resolve, reject })); } };
  ctx.window = ctx; vm.createContext(ctx); require("./helpers/load-ui")(ctx); ctx.jsq = value => ctx.esc(JSON.stringify(value)); ctx.jsArg = value => JSON.stringify(String(value ?? ""));
  vm.runInContext(require("./helpers/lifecycle-source")(), ctx);
  const snap = label => log.push(["state", label, fields["#mv_plan"].innerHTML, fields["#mv_go"].disabled, fields["#mv_sc"].innerHTML ?? null, fields["#mv_sc"].disabled]);
  const answer = (value, error) => { const r = replies.shift(); error ? r.reject(new Error(error)) : r.resolve(value); };
  return { ctx, fields, log, state, snap, answer, replies };
}
const stopRow = (w, label) => w.log.push(["stop row", label, w.fields["#mv_stop_row"].hidden]);
const LIVE = { ok: true, transfer_mode: "copy", live: true, can_live: true, claims: [{ claim: "os", size_gb: 40, volume_mode: "Block" }], total_gb: 40 };
const PLAN = { ok: true, storage_class: "fast", storage_classes: ["fast", "longhorn-r2"], claims: [{ claim: "app-data", size_gb: 10 }], total_gb: 10, joined: true, will_run: true };

pins("move plan review", "review-pins-move-plan", {
  async planStartQuestionAndRequest() {
    const w = world(); w.ctx.moveReview("source", "container", "camera-app"); w.snap("opened");
    w.answer(PLAN); await settle(); w.snap("planned");
    const start = w.ctx.moveStart("source", "container", "camera-app"); await settle(); w.snap("asked"); w.answer({}); await start; w.snap("started");
    return w.log;
  },
  async blockedWarningsAndAFix() {
    const w = world(); w.ctx.moveReview("source", "container", "camera-app"); w.answer({ ok: false, blockers: ["No capacity"], warnings: ["Slow disk"], fixes: [{ kind: "source-address" }], claims: [] });
    await settle(); w.snap("blocked");
    return w.log;
  },
  async copyNeedsADestinationThatCanCopy() {
    const w = world(); w.ctx.moveReview("source", "vm", "desktop", "copy", "guests"); w.answer({ ok: true, will_run: true, claims: [] }); await settle(); w.snap("old destination");
    w.ctx.movePlan("source", "vm", "desktop"); w.answer({ ok: true, transfer_mode: "copy", claims: [{ claim: "os", size_gb: 40, volume_mode: "Block" }], total_gb: 40 }); await settle(); w.snap("copy plan");
    const start = w.ctx.moveStart("source", "vm", "desktop"); await settle(); w.answer({}); await start;
    return w.log;
  },
  async aFailedPlan() {
    const w = world(); w.ctx.moveReview("source", "container", "camera-app"); w.answer(null, "no route to the other cluster"); await settle(); w.snap("failed");
    return w.log;
  },
  async anotherClassIsCheckedAgain() {
    const w = world(); w.ctx.moveReview("source", "container", "camera-app");
    w.answer({ ok: true, storage_class: "other", storage_classes: ["fast", "other"], claims: [] }); await settle(); w.snap("re-plans");
    w.answer({ ok: true, storage_class: "fast", storage_classes: ["fast", "other"], claims: [] }); await settle(); w.snap("settled");
    return w.log;
  },
  async anEditedFieldMakesAPlanStale() {
    const w = world(); w.ctx.moveReview("source", "container", "camera-app"); w.snap("opened");
    w.fields["#mv_ns"].value = "other"; w.fields["#mv_ip"].oninput(); w.snap("typed"); w.answer(PLAN); await settle(); w.snap("after the late plan");
    w.ctx.moveAddressMode("manual"); w.snap("address mode");
    return w.log;
  },
  async anOlderPlanCannotReplaceANewerOne() {
    const w = world(); const first = w.ctx.movePlan("source", "container", "camera-app"); w.fields["#mv_sc"].value = "longhorn-r2";
    const second = w.ctx.movePlan("source", "container", "camera-app"); w.replies[1].resolve({ ok: false, storage_class: "longhorn-r2", storage_classes: ["fast", "longhorn-r2"], blockers: ["No capacity"] });
    await second; w.replies[0].resolve({ ok: true, storage_class: "fast", storage_classes: ["fast", "longhorn-r2"] }); await first; w.snap("after");
    return w.log;
  },
  async settingsChangeWhileAskedAreCaught() {
    const w = world(); w.ctx.moveReview("source", "container", "camera-app"); w.answer(PLAN); await settle();
    let release; w.ctx.ask = async (q, o) => { w.log.push(["ask", q, o]); await new Promise(r => { release = r; }); return true; };
    const start = w.ctx.moveStart("source", "container", "camera-app"); await settle(); w.ctx.moveAddressMode("manual"); release(); await start; w.snap("after");
    return w.log;
  },
  async declinedStart() {
    const w = world({ ask: false }); w.ctx.moveReview("source", "volume", "data"); w.answer(PLAN); await settle();
    await w.ctx.moveStart("source", "volume", "data"); w.snap("declined");
    // (A refused start now reads the plan again so Start can be tried again: tests/move-plan-flow.test.js.)
    return w.log;
  },
  async deviceChoicesAndAVbiosFile() {
    const w = world(); w.ctx.vmReadRom = async () => "VaoA";
    w.ctx.moveDeviceSet("source", "vm", "desktop", "gpu", "resource", "example.test/new"); w.snap("device set"); w.answer({ ok: true, claims: [] }); await settle();
    await w.ctx.moveDeviceRom("source", "vm", "desktop", "gpu", { files: [{ name: "gpu.rom" }] }); w.snap("rom read");
    w.answer({ ok: true, claims: [], host_devices: [{ name: "gpu", rom: true }], device_resources: [] }); await settle(); w.snap("planned");
    return w.log;
  },
  // A VM copy (#1239): it keeps running, it stops because the person asked, or its cluster can't copy it live.
  async aCopyThatKeepsRunning() {
    const w = world(); w.ctx.moveReview("source", "vm", "desktop", "copy"); stopRow(w, "opened");
    w.answer(LIVE); await settle(); stopRow(w, "planned"); w.snap("planned");
    const start = w.ctx.moveStart("source", "vm", "desktop"); await settle(); w.answer({}); await start;
    return w.log;
  },
  async aCopyStoppedOnRequest() {
    const w = world(); w.ctx.moveReview("source", "vm", "desktop", "copy"); w.answer(LIVE); await settle();
    w.ctx.moveStopSource("source", "vm", "desktop", true); w.snap("switched"); w.answer({ ...LIVE, live: false }); await settle(); stopRow(w, "planned"); w.snap("planned");
    const start = w.ctx.moveStart("source", "vm", "desktop"); await settle(); w.answer({}); await start;
    return w.log;
  },
  async aCopyThatCannotKeepRunning() {
    const w = world(); w.ctx.moveReview("source", "vm", "desktop", "copy"); w.answer({ ...LIVE, live: false, can_live: false }); await settle(); stopRow(w, "planned");
    const start = w.ctx.moveStart("source", "vm", "desktop"); await settle(); w.answer({}); await start;
    return w.log;
  },
  async theStopChoiceChangedWhileAsked() {
    const w = world(); w.ctx.moveReview("source", "vm", "desktop", "copy"); w.answer(LIVE); await settle();
    let release; w.ctx.ask = async (q, o) => { w.log.push(["ask", q, o]); await new Promise(r => { release = r; }); return true; };
    const start = w.ctx.moveStart("source", "vm", "desktop"); await settle(); w.ctx.moveStopSource("source", "vm", "desktop", true); release(); await start; w.snap("after");
    return w.log;
  },
});
