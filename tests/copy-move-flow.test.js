"use strict";
// What the shared review flow changes in Move copies (#909), one test each, written to fail on the code before it: Start twice sends
// one request; a plan asked for while Start is being sent is ignored, so another volume's dialog waits on its spinner.
const test = require("node:test"), assert = require("node:assert/strict");
const fs = require("node:fs"), vm = require("node:vm");
const settle = () => new Promise(r => setImmediate(r));

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


const posts = w => w.log.filter(e => e[0] === "api" && e[1] === "/api/longhorn/copies/move");
const plans = w => w.log.filter(e => e[0] === "api" && e[1].startsWith("/api/longhorn/copies/plan"));

test("Start twice sends the move once", async () => {
  const w = world(); w.ctx.volumeCopyMove(volume, { disk_tags: ["nvme"] }); w.answer(PLAN); await settle();
  const first = w.ctx.volumeCopyMoveStart(), second = w.ctx.volumeCopyMoveStart(); await settle();
  while (w.replies.length) w.answer({ operation: { id: "move-job" } });
  await first; await second;
  assert.equal(posts(w).length, 1);
});

test("a plan asked for while Start is being sent is ignored, so another volume's dialog waits on its spinner", async () => {
  const w = world(); w.ctx.volumeCopyMove(volume, { disk_tags: ["nvme"] }); w.answer(PLAN); await settle();
  const start = w.ctx.volumeCopyMoveStart(); await settle();
  const before = plans(w).length;
  w.ctx.volumeCopyMoveTag("disk", "ssd", true); await settle();
  assert.equal(plans(w).length, before, "no plan is read while the send is running");
  w.answer({ operation: { id: "move-job" } }); await start; await settle();
  assert.ok(posts(w).length === 1);
});
