"use strict";
const test = require("node:test"), assert = require("node:assert/strict"), fs = require("node:fs"), vm = require("node:vm");

async function setup(response, ok = true) {
  const timers = [], fields = { "#gatebox": { innerHTML: "" }, "#gate": { classList: { add() {}, remove() {} } }, "#whoami": {} };
  let started = 0;
  const ctx = { console, window: null, fetch: async () => ({ ok, json: async () => response }),
    $: s => fields[s], $$: () => [], esc: v => String(v).replaceAll("<", "&lt;"),
    setTimeout: f => { timers.push(f); return timers.length; }, clearTimeout() {},
    api() {}, localStorage: { getItem() { return null; } } };
  ctx.window = ctx; vm.createContext(ctx);ctx.jsArg=s=>JSON.stringify(String(s??""));ctx.jsq=s=>(ctx.esc||String)(ctx.jsArg(s));
  vm.runInContext(fs.readFileSync("web/js/auth.js", "utf8"), ctx);
  ctx.afterAuth = () => started++;
  await new Promise(setImmediate);
  return { ctx, fields, timers, started: () => started };
}

test("read-only destination startup shows waiting page instead of setup or sign-in", async () => {
  const t = await setup({ data_handoff: true, setup: true });
  assert.match(t.fields["#gatebox"].innerHTML, /Checking the new data volume/);
  assert.doesNotMatch(t.fields["#gatebox"].innerHTML, /lg_pass|Create account/);
  assert.equal(t.started(), 0); assert.equal(t.timers.length, 1);
  // Existing session is restored only after the server releases the startup gate.
  vm.runInContext("authState = async () => ({ user: 'admin', role: 'admin' })", t.ctx);
  await t.timers[0](); assert.equal(t.started(), 1);
});

test("unknown destination readiness never looks like first-time setup", async () => {
  const t = await setup({ data_handoff: true, error: "Verification unavailable" }, false);
  assert.match(t.fields["#gatebox"].innerHTML, /Waiting for the cluster/);
  assert.doesNotMatch(t.fields["#gatebox"].innerHTML, /lg_pass|Create account/);
  assert.equal(t.started(), 0);
});
