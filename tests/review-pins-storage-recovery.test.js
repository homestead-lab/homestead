"use strict";
// Pins the storage-move review dialog (Recent jobs > Review) as it behaves, before it moves onto
// UI.reviewFlow (#909): its words, requests, toasts, buttons, double click, late reply and lost reply.
// The transcript is compared with tests/fixtures/review-pins-storage-recovery.json, recorded from the
// hand-written version (RECORD_PINS=1 re-records).
const test = require("node:test"), assert = require("node:assert/strict");
const fs = require("node:fs"), vm = require("node:vm");
const FIXTURE = "tests/fixtures/review-pins-storage-recovery.json";

function world({ held = false, preview = null } = {}) {
  const log = [], fields = { "#jobSummary": {}, "#storageRecoveryAck": { checked: false }, "#storageRecoveryRollbackAck": { checked: false },
    "#storageRecoveryApply": { disabled: false }, "#storageRecoveryRollback": { disabled: false } };
  const state = { refreshes: 0 };
  const ctx = { console, Date, URL, STATE: { data: {} }, ME: null, $: k => fields[k], $$: () => [], esc: v => String(v).replaceAll("<", "&lt;"),
    icon: () => "", clearInterval() {}, clearTimeout() {}, setTimeout() {},
    toast: (text, tone) => log.push(["toast", text, tone]), closeModal() { log.push(["closeModal"]); delete fields["#storageRecoveryContent"]; },
    modal: (title) => { log.push(["modal", title]); fields["#storageRecoveryContent"] = { _html: "", get innerHTML() { return this._html; }, set innerHTML(v) { this._html = v; } }; },
    api: async (path, opts) => {
      const body = JSON.parse(opts.body); log.push(["api", path, body]);
      if (path.endsWith("/preview")) return preview ? preview(body) : (held ? HELD(body) : PLAIN(body));
      if (state.fail) throw Error("Lost response");
      if (state.gate) await state.gate;
      return { ok: true, detail: "Queued" };
    } };
  ctx.window = ctx; vm.createContext(ctx);
  ctx.jsArg = s => JSON.stringify(String(s ?? "")); ctx.jsq = s => (ctx.esc || String)(ctx.jsArg(s));
  for (const file of ["ui", "operations"]) vm.runInContext(fs.readFileSync(`web/js/${file}.js`, "utf8"), ctx);
  ctx.refreshOperations = () => { state.refreshes++; log.push(["refreshOperations"]); };
  const html = () => fields["#storageRecoveryContent"]?.innerHTML;
  const snap = label => log.push(["state", label, html() ?? null, fields["#storageRecoveryApply"].disabled, fields["#storageRecoveryRollback"].disabled]);
  return { ctx, fields, log, state, html, snap };
}
const PLAIN = body => ({ tokens: { continue: "continue-token" }, plan: { id: body.id, claim: "<data>", phase: "copy", can_continue: true, can_pause: false,
  from_class: "old", to_class: "new", resources: [{ resource: { name: "<copy>" }, receipt: "accepted", relationship: "same identity" }],
  workloads: [{ name: "<app>", kind: "Deployment" }], warnings: ["Memory warning"], blockers: [], message: "Retained" } });
const HELD = body => ({ tokens: { retry: "retry-token", continue: "retry-token", rollback: "rollback-token" }, plan: { id: body.id, claim: "data", phase: "copy",
  status: "failed", can_continue: true, can_retry: true, can_finish: false, can_rollback: true, can_pause: false, resources: [], workloads: [], warnings: [],
  blockers: [], message: "Held", rollback: { blockers: [], next_write: null } } });

const scenarios = {
  async continueTwice() {
    const w = world(); await w.ctx.storageRecoveryReview("job"); w.snap("reviewed");
    await w.ctx.storageRecoveryApply(); w.snap("applied without the tick");
    w.fields["#storageRecoveryAck"].checked = true; w.ctx.storageRecoveryReady(); w.snap("ticked");
    let release; w.state.gate = new Promise(r => { release = r; });
    const first = w.ctx.storageRecoveryApply(), second = w.ctx.storageRecoveryApply(); w.snap("sending");
    release(); await first; await second; w.snap("sent");
    return w.log;
  },
  async heldRollbackEachWithItsOwnTick() {
    const w = world({ held: true }); await w.ctx.storageRecoveryReview("job"); w.snap("reviewed");
    w.fields["#storageRecoveryAck"].checked = true; await w.ctx.storageRecoveryApply("rollback"); w.snap("rollback with the retry tick");
    w.fields["#storageRecoveryAck"].checked = false; w.fields["#storageRecoveryRollbackAck"].checked = true;
    await w.ctx.storageRecoveryApply(); w.snap("retry with the rollback tick");
    await w.ctx.storageRecoveryApply("rollback"); w.snap("rolled back");
    return w.log;
  },
  async incompleteAndFailedPreviews() {
    const w = world({ preview: () => ({ tokens: {}, plan: { id: "job", can_continue: true, can_pause: false, resources: [], warnings: [], blockers: [] } }) });
    await w.ctx.storageRecoveryReview("job"); w.snap("incomplete");
    const v = world({ preview: () => { throw Error("Not found"); } }); await v.ctx.storageRecoveryReview("job"); v.snap("failed");
    return [...w.log, ...v.log];
  },
  async lostReplyShowsTheJobAndNeverRetries() {
    const w = world(); w.state.fail = true; await w.ctx.storageRecoveryReview("job");
    w.fields["#storageRecoveryAck"].checked = true; await w.ctx.storageRecoveryApply(); w.snap("failed");
    await w.ctx.storageRecoveryApply(); w.snap("tried again");
    return w.log;
  },
  async oldPreviewCannotReplaceANewerOne() {
    const w = world(); const original = w.ctx.api; let finish;
    w.ctx.api = (path, options) => JSON.parse(options.body).id === "old" ? new Promise(r => { finish = r; }) : original(path, options);
    const old = w.ctx.storageRecoveryReview("old"); await w.ctx.storageRecoveryReview("new"); finish({}); await old; w.snap("after the late reply");
    w.fields["#storageRecoveryAck"].checked = true; await w.ctx.storageRecoveryApply(); w.snap("sent");
    return w.log;
  },
  async anotherDialogOpensWhileSending() {
    const w = world(); await w.ctx.storageRecoveryReview("job"); w.fields["#storageRecoveryAck"].checked = true;
    let release; w.state.gate = new Promise(r => { release = r; });
    const pending = w.ctx.storageRecoveryApply(); w.ctx.modal("Another"); await w.ctx.storageRecoveryReview("other"); release(); await pending; w.snap("after");
    return w.log;
  },
};

for (const [name, run] of Object.entries(scenarios)) test(`storage move review: ${name}`, async () => {
  const transcript = JSON.parse(JSON.stringify(await run()));
  const all = fs.existsSync(FIXTURE) ? JSON.parse(fs.readFileSync(FIXTURE, "utf8")) : {};
  if (process.env.RECORD_PINS) { all[name] = transcript; fs.writeFileSync(FIXTURE, JSON.stringify(all, null, 1) + "\n"); return; }
  assert.deepEqual(transcript, all[name]);
});
