"use strict";
// What the shared review flow changes in the Deploy review (#909), written to fail on the code before it: an error from a preview
// a newer review has replaced is not shown.
const test = require("node:test"), assert = require("node:assert/strict");
const fs = require("node:fs"), vm = require("node:vm");
const settle = () => new Promise(r => setImmediate(r));

function world(capacity, { app_profile = undefined, fail = false, impact = {} } = {}) {
  const log = [], state = { gate: null, fail };
  let config = { name: "demo", container_name: "demo", workload_name: "demo", target_mode: "new", image: "demo:1", ports: [], volumes: [] };
  const fields = { "#deployGo": { disabled: true, textContent: "Deploy", onclick: "orig" }, "#deployCapacityConfirm": { checked: false }, "#deployConfirm": { checked: false } };
  const ctx = { window: {}, console, URLSearchParams, Map, document: { addEventListener() {}, getElementById: id => fields["#" + id] || null, querySelector: () => null },
    logoReady: async () => {}, $: selector => fields[selector], $$: () => [], esc: s => String(s).replaceAll("<", "&lt;").replaceAll(">", "&gt;"),
    api: async (path, options) => {
      const body = JSON.parse(options.body); log.push(["api", path, body]);
      if (path === "/api/preview") { if (state.preview) return state.preview(body); return { capacity, capacity_token: "signed-token", impact, app_profile, deployment: {}, service: null }; }
      if (state.gate) await state.gate;
      if (state.fail) throw new Error("rejected");
      return { ok: true, reused_volumes: ["old-claim"] };
    }, modal: (title, body) => log.push(["modal", title, body]), toast: (text, tone) => log.push(["toast", text, tone]),
    volumeListIssue: () => "", closeModal: () => log.push(["closeModal"]), go: view => log.push(["go", view]) };
  vm.createContext(ctx); require("./helpers/core-text")(ctx);
  ctx.jsArg = s => JSON.stringify(String(s ?? "")); ctx.jsq = s => (ctx.esc || String)(ctx.jsArg(s));
  for (const file of ["ui.js", "gitops.js"]) vm.runInContext(fs.readFileSync(`web/js/${file}`, "utf8"), ctx);
  vm.runInContext(require("./helpers/workloads-source")(), ctx);
  ctx.collect = () => config;
  const snap = label => log.push(["state", label, fields["#deployGo"].disabled, fields["#deployGo"].textContent, String(fields["#deployGo"].onclick)]);
  return { ctx, fields, log, state, snap, setConfig: c => { config = { ...config, ...c }; } };
}
const WARNING = { additional: 1, requires_confirmation: true, blocked: false, pod_request_gb: 1, pod_cpu_request_percent: 50, pod_memory_gb: 2, warnings: ["planned <volume>"],
  candidates: [{ name: "host<a>", eligible: true, metrics_available: false, reservations_known: false }] };


test("an error from a preview a newer review has replaced is not shown", async () => {
  const w = world(WARNING); let fail, n = 0; const original = w.ctx.api;
  w.ctx.api = (path, options) => path === "/api/preview" && n++ === 0 ? new Promise((_, reject) => { fail = reject; }) : original(path, options);
  const old = w.ctx.window.doDeploy(); await settle();
  await w.ctx.window.doDeploy();
  fail(new Error("the old preview failed")); await old;
  assert.deepEqual(w.log.filter(e => e[0] === "toast"), []);
});
