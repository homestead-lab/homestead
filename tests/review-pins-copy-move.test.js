"use strict";
// Pins "Move copies" (a Longhorn volume's copies to other disks) before its plan moves onto UI.reviewFlow (#909):
// the plan drawn into the dialog, Start's state, the re-plan when a tag is ticked, the request, a refused start that
// reads the plan again, an older plan and a plan the dialog has left.
const fs = require("node:fs"), vm = require("node:vm");
const pins = require("./helpers/review-pins"), { settle } = pins;

const PLAN = { ok: true, blockers: [], warnings: ["Slow disk"], disk_tags: ["nvme"], node_tags: [], review_token: "tok", size_gb: 10, used_gb: 4, state: "attached", attached_to: "host-a",
  choices: { disk_tags: ["nvme", "ssd"], node_tags: ["rack<1>"] }, copies: [{ host: "host-a", path: "/var/a", tags: ["ssd"], moves: true }, { host: "host-b", path: "/var/b", tags: ["nvme"], moves: false }],
  targets: [{ host: "host-c", path: "/var/c", fits: true, room_gb: 100 }, { host: "host-d", path: "/var/d", fits: false, room_gb: 1 }] };
function world() {
  const log = [], replies = [], state = {};
  const fields = { "#cm_body": { _html: "", get innerHTML() { return this._html; }, set innerHTML(v) { this._html = v; } }, "#cm_go": { disabled: true } };
  const ctx = { console, URLSearchParams, Map, Set, Date, Promise, STATE: { data: {} }, $: key => fields[key], $$: () => [],
    document: { addEventListener() {}, getElementById: id => fields["#" + id] || null },
    esc: s => String(s ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c])),
    sizeText: gb => `${gb} GiB`, toast: (text, tone) => log.push(["toast", text, tone]), closeModal: () => log.push(["closeModal"]), refresh: v => log.push(["refresh", v]),
    applyRole: () => log.push(["applyRole"]), noteOperation: op => log.push(["noteOperation", op]),
    modal: (title, html, wide, context) => { log.push(["modal", title, html, wide, context]); fields["#cm_body"].innerHTML = '<div class="empty">reading</div>'; },
    api: (path, opts) => { log.push(["api", path, opts?.body ? JSON.parse(opts.body) : null]); return new Promise((resolve, reject) => replies.push({ resolve, reject })); } };
  ctx.window = ctx; vm.createContext(ctx); require("./helpers/load-ui")(ctx); ctx.jsArg = s => JSON.stringify(String(s ?? "")); ctx.jsq = s => ctx.esc(ctx.jsArg(s));
  vm.runInContext(fs.readFileSync("web/js/views-storage-copies.js", "utf8"), ctx);
  const snap = label => log.push(["state", label, fields["#cm_body"]?.innerHTML ?? null, fields["#cm_go"].disabled]);
  const answer = (value, error) => { const r = replies.shift(); error ? r.reject(new Error(error)) : r.resolve(value); };
  return { ctx, fields, log, state, snap, answer, replies };
}
const volume = { name: "pvc-1", pvc_name: "data" };

pins("copy move review", "review-pins-copy-move", {
  async planTickAndStart() {
    const w = world(); w.ctx.volumeCopyMove(volume, { disk_tags: ["nvme"] }); w.answer(PLAN); await settle(); w.snap("planned");
    w.ctx.volumeCopyMoveTag("disk", "ssd", true); w.snap("ticked"); w.answer({ ...PLAN, disk_tags: ["nvme", "ssd"] }); await settle(); w.snap("re-planned");
    const start = w.ctx.volumeCopyMoveStart(); w.snap("starting"); w.answer({ operation: { id: "move-job" } }); await start; w.snap("started");
    return w.log;
  },
  async blockedAndFailedPlans() {
    const w = world(); w.ctx.volumeCopyMove(volume); w.answer({ ...PLAN, ok: false, blockers: ["No disk has room"], warnings: [] }); await settle(); w.snap("blocked");
    await w.ctx.volumeCopyMoveStart(); w.snap("tried");
    const v = world(); v.ctx.volumeCopyMove(volume); v.answer(null, "Longhorn is not answering"); await settle(); v.snap("failed");
    return [...w.log, ...v.log];
  },
  async refusedStartReadsThePlanAgain() {
    const w = world(); w.ctx.volumeCopyMove(volume, { disk_tags: ["nvme"] }); w.answer(PLAN); await settle();
    const start = w.ctx.volumeCopyMoveStart(); w.answer(null, "The plan changed"); await start; await settle(); w.snap("after the refusal"); w.answer(PLAN); await settle(); w.snap("read again");
    return w.log;
  },
  async olderPlanCannotReplaceANewerOne() {
    const w = world(); w.ctx.volumeCopyMove(volume); w.ctx.volumeCopyMoveTag("node", "rack<1>", true);
    w.replies[1].resolve({ ...PLAN, copies: [{ host: "newer", path: "/n", tags: [], moves: true }] }); await settle(); w.replies[0].resolve(PLAN); await settle(); w.snap("after");
    return w.log;
  },
  async aPlanTheDialogHasLeftIsDropped() {
    const w = world(); w.ctx.volumeCopyMove(volume); delete w.fields["#cm_body"]; w.answer(PLAN); await settle(); w.snap("dropped");
    return w.log;
  },
  async tickingBeforeAnyDialog() {
    const w = world(); w.ctx.volumeCopyMoveTag("disk", "ssd", true); await w.ctx.volumeCopyMoveStart(); w.snap("nothing");
    return w.log;
  },
});
