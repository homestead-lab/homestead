"use strict";
// Pins the "Inspect ... outcome" review (Recent jobs) before it moves onto UI.reviewFlow (#909):
// its four dialogs (VM power, VM configuration, import, k3s batch), the acknowledgement and typed-name
// gate on the button, requests, toasts, double click, late reply, lost reply. Compared with
// tests/fixtures/review-pins-power-recovery.json, recorded from the hand-written version.
const test = require("node:test"), assert = require("node:assert/strict");
const fs = require("node:fs"), vm = require("node:vm");
const FIXTURE = "tests/fixtures/review-pins-power-recovery.json";

function world(shape = "power", { blocked = false, missing = false, fail = false, preview = null } = {}) {
  const log = [], fields = { "#jobSummary": {}, ".modalbox": { scrollTop: 400 }, "#powerRecoveryApply": { disabled: true },
    "#powerRecoveryAck": { checked: false },
    "#powerRecoveryName": { value: "", get dataset() { return { typedExpect: fields["#mbody"]._html.match(/id="powerRecoveryName"[^>]*data-typed-expect="([^"]*)"/)?.[1] }; } } };
  fields["#mbody"] = { _html: "", get innerHTML() { return this._html; }, set innerHTML(html) { this._html = html; delete fields["#powerRecoveryLoading"]; },
    insertAdjacentHTML(_where, html) { this._html = html + this._html; } };
  fields["#mtitle"] = { textContent: "" };
  const state = { gate: null };
  const ctx = { console, Date, URL, STATE: { data: {} }, ME: null, $: key => fields[key], $$: () => [],
    esc: value => String(value).replaceAll("<", "&lt;"), icon: () => "", clearTimeout() {}, setTimeout() {},
    toast: (text, tone) => log.push(["toast", text, tone]), closeModal: () => log.push(["closeModal"]),
    modal: (title, html) => { log.push(["modal", title]); fields["#mbody"].innerHTML = html; fields["#powerRecoveryLoading"] = {}; },
    api: async (path, options) => {
      const body = JSON.parse(options.body); log.push(["api", path, body]);
      if (path.endsWith("/preview")) {
        if (preview) return preview(body);
        const plan = { blocked, blockers: blocked ? ["dispatcher active"] : [], warnings: ["A late effect is possible"], action: "restart", dispatch_phase: "uncertain",
          confirm: "guest", resource: { namespace: "lab", name: "<guest>", original_uid: "old" },
          observed: { vm: { uid: "old" }, instance: { uid: "new" }, same_vm: true, vm_status: "Running", run_strategy: "Manual", instance_phase: "Running", ready: true, paused: false, queued_changes: 0 } };
        if (shape === "mutation") { delete plan.observed; plan.resources = [{ resource: { kind: "Secret", namespace: "lab", name: "<login>" }, last_write: "uncertain", relationship: "replacement; not adopted", expected: { uid: "old" }, current: { uid: "new" } }]; }
        if (shape === "import") Object.assign(plan, { action: "import-create", resources: [{ resource: { kind: "Deployment", namespace: "lab", name: "<photos>" }, last_write: "uncertain", relationship: "identity unproven", current: { uid: "observed-only" } }] });
        if (shape === "k3s") { delete plan.observed; Object.assign(plan, { action: "k3s-cluster", confirm: "cluster", resource: { namespace: "lab", name: "cluster" },
          resources: [{ resource: { kind: "VirtualMachine", namespace: "lab", name: "server-1" }, last_write: "accepted", relationship: "same identity", expected: { uid: "one" }, current: { uid: "one" } },
            { resource: { kind: "VirtualMachine", namespace: "lab", name: "agent-1" }, last_write: "not dispatched", relationship: "not found", expected: null, current: null }] }); }
        return { capacity_token: missing ? null : "signed-recovery", plan };
      }
      if (state.gate) await state.gate;
      if (fail) throw new Error("lost response");
      return { ok: true, detail: "Resolved as unknown", operation: { id: "job", status: "failed" } };
    } };
  // UI.reviewFlow finds the tickbox and button by id, as the page does.
  ctx.document = { getElementById: id => fields["#" + id] };
  ctx.window = ctx; vm.createContext(ctx);
  ctx.jsArg = s => JSON.stringify(String(s ?? "")); ctx.jsq = s => (ctx.esc || String)(ctx.jsArg(s));
  vm.runInContext(fs.readFileSync("web/js/ui.js", "utf8"), ctx); vm.runInContext(fs.readFileSync("web/js/operations.js", "utf8"), ctx);
  ctx.noteOperation = job => log.push(["noteOperation", job]);
  ctx.refreshOperations = () => log.push(["refreshOperations"]);
  const snap = label => log.push(["state", label, fields["#mbody"].innerHTML, fields["#powerRecoveryApply"].disabled, fields[".modalbox"].scrollTop, fields["#mtitle"].textContent]);
  return { ctx, fields, log, state, snap };
}
const arg = shape => shape === "power" ? [false] : shape === "mutation" || shape === "k3s" ? [true] : ["import"];
const scenarios = {};
for (const shape of ["power", "mutation", "import", "k3s"]) {
  scenarios[`${shape}: gate and send once`] = async () => {
    const w = world(shape); await w.ctx.powerRecoveryReview("job", ...arg(shape)); w.snap("reviewed");
    const name = shape === "k3s" ? "cluster" : "guest";
    await w.ctx.powerRecoveryResolve(); w.snap("resolve without anything");
    w.fields["#powerRecoveryAck"].checked = true; w.ctx.powerRecoveryReady(); w.snap("ticked");
    w.fields["#powerRecoveryName"].value = "wrong"; w.ctx.powerRecoveryReady(); w.snap("wrong name");
    w.fields["#powerRecoveryName"].value = name; w.ctx.powerRecoveryReady(); w.snap("right name");
    let release; w.state.gate = new Promise(r => { release = r; });
    const a = w.ctx.powerRecoveryResolve(), b = w.ctx.powerRecoveryResolve(); w.snap("sending");
    release(); await a; await b; w.snap("sent");
    return w.log;
  };
  scenarios[`${shape}: lost reply is not retried`] = async () => {
    const w = world(shape, { fail: true }); await w.ctx.powerRecoveryReview("job", ...arg(shape));
    w.fields["#powerRecoveryAck"].checked = true; w.fields["#powerRecoveryName"].value = shape === "k3s" ? "cluster" : "guest";
    w.fields[".modalbox"].scrollTop = 600;
    await w.ctx.powerRecoveryResolve(); w.snap("failed"); await w.ctx.powerRecoveryResolve(); w.snap("tried again");
    return w.log;
  };
}
scenarios["blocked, incomplete and failed previews"] = async () => {
  const out = [];
  for (const options of [{ blocked: true }, { missing: true }, { preview: () => { throw Error("Not found"); } }]) {
    const w = world("power", options); await w.ctx.powerRecoveryReview("job"); w.snap("reviewed");
    w.fields["#powerRecoveryAck"].checked = true; w.fields["#powerRecoveryName"].value = "guest"; w.ctx.powerRecoveryReady(); await w.ctx.powerRecoveryResolve(); w.snap("tried");
    out.push(...w.log);
  }
  const w = world("mutation", { preview: () => ({ capacity_token: "t", plan: { blocked: false, resources: [] } }) }); await w.ctx.powerRecoveryReview("job", true); w.snap("no resource data");
  return [...out, ...w.log];
};
scenarios["an older job's reply cannot replace a newer review"] = async () => {
  const w = world(); const original = w.ctx.api; let finish;
  w.ctx.api = (path, options) => JSON.parse(options.body).id === "old" ? new Promise(r => { finish = r; }) : original(path, options);
  const pending = w.ctx.powerRecoveryReview("old"); await w.ctx.powerRecoveryReview("new");
  finish({ plan: { blocked: false, confirm: "old" }, capacity_token: "old-token" }); await pending; w.snap("after the late reply");
  w.fields["#powerRecoveryAck"].checked = true; w.fields["#powerRecoveryName"].value = "guest"; await w.ctx.powerRecoveryResolve(); w.snap("sent");
  return w.log;
};
scenarios["a review does not open while one is being sent"] = async () => {
  const w = world(); await w.ctx.powerRecoveryReview("job"); w.fields["#powerRecoveryAck"].checked = true; w.fields["#powerRecoveryName"].value = "guest";
  let release; w.state.gate = new Promise(r => { release = r; });
  const pending = w.ctx.powerRecoveryResolve(); await w.ctx.powerRecoveryReview("other"); release(); await pending; w.snap("after");
  return w.log;
};

for (const [name, run] of Object.entries(scenarios)) test(`power recovery review: ${name}`, async () => {
  const transcript = JSON.parse(JSON.stringify(await run()));
  const all = fs.existsSync(FIXTURE) ? JSON.parse(fs.readFileSync(FIXTURE, "utf8")) : {};
  if (process.env.RECORD_PINS) { all[name] = transcript; fs.writeFileSync(FIXTURE, JSON.stringify(all, null, 1) + "\n"); return; }
  assert.deepEqual(transcript, all[name]);
});
