"use strict";
// What the shared review flow changes in the linked-cluster Move / Copy review (#909), one test each. Confirming the question
// twice sends one start (it sent two before); and after a refused start the plan is read again, so Start can be tried again
// as it always could (the first version of the move left Start enabled with nothing to send).
// The rest carries #1239's VM copy through the flow, and passes on the hand-written version too: a resolution that drops
// one of these lines would send a plan without stop_source, or ask the wrong question before a copy.
const test = require("node:test"), assert = require("node:assert/strict");
const fs = require("node:fs"), vm = require("node:vm");
const settle = () => new Promise(r => setImmediate(r));

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
const PLAN = { ok: true, storage_class: "fast", storage_classes: ["fast", "longhorn-r2"], claims: [{ claim: "app-data", size_gb: 10 }], total_gb: 10, joined: true, will_run: true };

const starts = w => w.log.filter(e => e[0] === "api" && e[1] === "/api/move/start");

test("confirming the question twice starts the move once", async () => {
  const w = world(); w.ctx.moveReview("source", "container", "camera-app"); w.answer(PLAN); await settle();
  const first = w.ctx.moveStart("source", "container", "camera-app"), second = w.ctx.moveStart("source", "container", "camera-app");
  await settle(); while (w.replies.length) w.answer({}); await first; await second;
  assert.equal(starts(w).length, 1);
});

const plans = w => w.log.filter(e => e[0] === "api" && e[1] === "/api/move/plan");
for (const kind of ["container", "volume"]) test(`after a refused start of a ${kind} the plan is read again and Start can be tried again`, async () => {
  const w = world(); w.ctx.moveReview("source", kind, "camera-app"); w.answer(PLAN); await settle();
  assert.equal(plans(w).length, 1);
  const first = w.ctx.moveStart("source", kind, "camera-app"); await settle(); w.answer(null, "The other cluster refused"); await first; await settle();
  // Between the refusal and the new plan: the person is told, the plan is being read again, and Start waits for it.
  assert.ok(w.log.some(e => e[0] === "toast" && e[1] === "The other cluster refused" && e[2] === "bad"), "the refusal is shown");
  assert.equal(plans(w).length, 2, "exactly one new plan was asked for");
  assert.equal(w.fields["#mv_go"].disabled, true, "Start waits while the plan is read again");
  w.answer(PLAN); await settle();
  assert.equal(w.fields["#mv_go"].disabled, false, "Start is enabled on a plan that can start");
  const again = w.ctx.moveStart("source", kind, "camera-app"); await settle(); while (w.replies.length) w.answer({}); await again;
  assert.equal(starts(w).length, 2, "the second try was sent");
  assert.equal(plans(w).length, 2, "and no further plan was asked for");
  assert.ok(!w.log.some(e => e[0] === "toast" && /settings changed/.test(e[1])), "no one is told the settings changed");
});

const LIVE = { ok: true, transfer_mode: "copy", live: true, can_live: true, claims: [{ claim: "os", size_gb: 40, volume_mode: "Block" }], total_gb: 40 };
const lastBody = (w, path) => w.log.filter(e => e[0] === "api" && e[1] === path).at(-1)[2];
const asked = w => w.log.filter(e => e[0] === "ask").at(-1)[1];
const startCopy = async w => { const start = w.ctx.moveStart("source", "vm", "desktop"); await settle(); while (w.replies.length) w.answer({}); await start; };

test("the stop choice goes in the plan and in the start, and reopening the dialog forgets it", async () => {
  const w = world(); w.ctx.moveReview("source", "vm", "desktop", "copy");
  assert.equal(lastBody(w, "/api/move/plan").stop_source, false);
  w.answer(LIVE); await settle();
  w.ctx.moveStopSource("source", "vm", "desktop", true);
  assert.equal(plans(w).length, 2, "choosing asks for a new plan");
  assert.equal(lastBody(w, "/api/move/plan").stop_source, true);
  assert.equal(w.fields["#mv_go"].disabled, true, "Start waits for that plan");
  w.answer({ ...LIVE, live: false }); await settle();
  await startCopy(w);
  assert.equal(lastBody(w, "/api/move/start").stop_source, true);
  w.ctx.moveReview("source", "vm", "desktop", "copy");
  assert.equal(lastBody(w, "/api/move/plan").stop_source, false);
});

test("the question before a copy says whether it keeps running, stops because it was asked to, or stops", async () => {
  const live = world(); live.ctx.moveReview("source", "vm", "desktop", "copy"); live.answer(LIVE); await settle();
  await startCopy(live);
  assert.match(asked(live), /It keeps running while its disks are backed up/);
  const stop = world(); stop.ctx.moveReview("source", "vm", "desktop", "copy"); stop.answer(LIVE); await settle();
  stop.ctx.moveStopSource("source", "vm", "desktop", true); stop.answer({ ...LIVE, live: false }); await settle();
  await startCopy(stop);
  assert.match(asked(stop), /It stops while its disks are backed up, then starts again as it was/);
  const other = world(); other.ctx.moveReview("source", "container", "camera-app", "copy"); other.answer({ ...LIVE, live: false, can_live: false }); await settle();
  const start = other.ctx.moveStart("source", "container", "camera-app"); await settle(); while (other.replies.length) other.answer({}); await start;
  assert.match(asked(other), /The original stops while its volumes are backed up/);
});

test("the stop switch shows only when the plan says this VM can be copied live", async () => {
  const yes = world(); yes.ctx.moveReview("source", "vm", "desktop", "copy"); yes.answer(LIVE); await settle();
  assert.equal(yes.fields["#mv_stop_row"].hidden, false);
  const stopped = world(); stopped.ctx.moveReview("source", "vm", "desktop", "copy"); stopped.answer({ ...LIVE, live: false }); await settle();
  assert.equal(stopped.fields["#mv_stop_row"].hidden, false, "still shown once it is chosen, so it can be undone");
  const no = world(); no.ctx.moveReview("source", "vm", "desktop", "copy"); no.answer({ ...LIVE, live: false, can_live: false }); await settle();
  assert.equal(no.fields["#mv_stop_row"].hidden, true);
});

// Copy A, confirm; during the send, Back and Copy B. What B's dialog shows and asks must be B's, whatever happens to A.
const B_PLAN = { ok: true, transfer_mode: "copy", live: false, can_live: false, warnings: ["B stops for its backups"], claims: [], total_gb: 1 };
const A_PLAN = { ...LIVE, warnings: ["A keeps running"] };
async function aSendingWithBOpen() {
  const w = world(); w.ctx.moveReview("source", "vm", "A", "copy"); w.answer(A_PLAN); await settle();
  const sendA = w.ctx.moveStart("source", "vm", "A"); await settle();
  w.ctx.moveReview("source", "container", "B", "copy"); await settle();
  return { w, sendA };
}
test("a dialog opened during a send waits on the spinner with Start off, and reads nothing yet", async () => {
  const { w } = await aSendingWithBOpen();
  assert.equal(plans(w).length, 1, "only A's plan so far");
  assert.match(w.fields["#mv_plan"].innerHTML, /asking both clusters/);
  assert.equal(w.fields["#mv_go"].disabled, true);
});
test("after a refused start, a dialog opened during the send plans and starts its own transfer", async () => {
  const { w, sendA } = await aSendingWithBOpen();
  w.answer(null, "The other cluster refused"); await sendA; await settle();
  assert.equal(plans(w).length, 2, "one plan for the dialog open now");
  assert.deepEqual([lastBody(w, "/api/move/plan").name, lastBody(w, "/api/move/plan").kind], ["B", "container"]);
  w.answer(B_PLAN); await settle();
  assert.match(w.fields["#mv_plan"].innerHTML, /B stops for its backups/);
  assert.doesNotMatch(w.fields["#mv_plan"].innerHTML, /A keeps running/);
  assert.equal(w.fields["#mv_go"].disabled, false);
  const sendB = w.ctx.moveStart("source", "container", "B"); await settle(); w.answer({}); await sendB;
  assert.match(asked(w), /The original stops while its volumes are backed up/);
  assert.deepEqual([lastBody(w, "/api/move/start").name, lastBody(w, "/api/move/start").kind], ["B", "container"]);
});
test("Check again during a send waits for it, then reads the plan once", async () => {
  const w = world(); w.ctx.moveReview("source", "vm", "A", "copy"); w.answer(A_PLAN); await settle();
  const send = w.ctx.moveStart("source", "vm", "A"); await settle();
  w.ctx.movePlan("source", "vm", "A"); w.ctx.movePlan("source", "vm", "A");
  assert.equal(plans(w).length, 1);
  w.answer(null, "refused"); await send; await settle();
  assert.equal(plans(w).length, 2);
});
