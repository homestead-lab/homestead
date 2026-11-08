"use strict";
// What the shared review flow changes in the host power review (#909), one test each, written to fail on the code before it:
// an error from a plan a newer review replaced is not shown; a review asked for while the request is being sent does not replace
// the plan; a plan that fails while it is drawn says "Could not assess this host".
const test = require("node:test"), assert = require("node:assert/strict");
const fs = require("node:fs"), vm = require("node:vm");
const settle = () => new Promise(r => setImmediate(r));

const PLAN = { node: "host1", action: "reboot", review_token: "review", ready: true, pods: 1, vms: [], workloads: [], volumes: [] };
function world({ plan = PLAN, fail = false, apiPlan = null } = {}) {
  const log = [], state = { gate: null, fail };
  const fields = { "#pw_confirm": { value: "host1", dataset: { typedExpect: "host1" } }, "#pw_execute": { disabled: false, textContent: "Reboot host", onclick: "orig" } };
  const c = { window: {}, console, URLSearchParams, Map, document: { addEventListener() {}, getElementById: id => fields["#" + id] || null },
    $: s => fields[s], esc: s => String(s).replaceAll("<", "&lt;").replaceAll(">", "&gt;"),
    childModal: (title, body) => log.push(["childModal", title, body]), modal: (title, body) => log.push(["modal", title, body]),
    toast: (text, tone) => log.push(["toast", text, tone]), closeModal: () => log.push(["closeModal"]), refresh: () => log.push(["refresh"]),
    setTimeout() {}, STATE: { data: {} },
    api: async (path, options) => {
      if (path.startsWith("/api/node/power/plan")) { log.push(["api", path]); if (apiPlan) return apiPlan(path); return typeof plan === "function" ? plan(path) : plan; }
      const body = JSON.parse(options.body); log.push(["api", path, body]);
      if (state.gate) await state.gate;
      if (state.fail) throw new Error("lost reply");
      return { ok: true, operation: state.job === false ? undefined : { id: "power-job" }, steps: ["cordoned"] };
    } };
  c.window = c; vm.createContext(c); require("./helpers/core-text")(c);
  c.jsArg = s => JSON.stringify(String(s ?? "")); c.jsq = s => (c.esc || String)(c.jsArg(s));
  c.noteOperation = op => log.push(["noteOperation", op]); c.refreshOperations = () => log.push(["refreshOperations"]);
  for (const f of ["web/js/ui.js", "web/js/gitops.js"]) vm.runInContext(fs.readFileSync(f, "utf8"), c);
  vm.runInContext(require("./helpers/workloads-source")(), c);
  vm.runInContext(require("./helpers/lifecycle-source")(), c);
  c.nodePowerFollow = (id, note) => log.push(["nodePowerFollow", id, note || null]);
  const snap = label => log.push(["state", label, fields["#pw_execute"].disabled, fields["#pw_execute"].textContent, String(fields["#pw_execute"].onclick), c.__nodePowerPlan ? c.__nodePowerPlan.node : null]);
  return { c, fields, log, state, snap };
}


test("an error from a plan a newer review replaced is not shown", async () => {
  let fail, calls = 0;
  const w = world(); w.c.api = path => ++calls === 1 ? new Promise((_, reject) => { fail = reject; }) : Promise.resolve({ ...PLAN, node: "host2" });
  const old = w.c.nodePowerReview("host1", "reboot"); await w.c.nodePowerReview("host2", "reboot");
  fail(new Error("the old plan failed")); await old;
  assert.deepEqual(w.log.filter(e => e[0] === "toast"), []);
});

test("a review asked for while the request is being sent does not replace the plan", async () => {
  const w = world(); await w.c.nodePowerReview("host1", "reboot");
  let release; w.state.gate = new Promise(r => { release = r; });
  const sending = w.c.nodePower("host1", "reboot"); await settle();
  await w.c.nodePowerReview("host1", "reboot");
  release(); await sending;
  assert.equal(w.log.filter(e => e[0] === "api" && e[1].startsWith("/api/node/power/plan")).length, 1, "one plan was asked for");
  assert.equal(w.log.filter(e => e[0] === "childModal").length, 1, "one dialog");
});

test("a plan that fails while it is drawn says the host could not be assessed", async () => {
  const w = world({ plan: { ...PLAN, workloads: { length: 1 } } });
  await w.c.nodePowerReview("host1", "reboot");
  const toasts = w.log.filter(e => e[0] === "toast");
  assert.equal(toasts.length, 1);
  assert.match(toasts[0][1], /^This host could not be checked\. /);
});
