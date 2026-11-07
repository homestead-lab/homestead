"use strict";
const test = require("node:test"), assert = require("node:assert/strict"), fs = require("node:fs"), vm = require("node:vm");
const between = require("./helpers/between");

/* Stopping a VM, and stopping or restarting a container, asks first with the
   standard dialog; saying no sends nothing. */
function slice(file, start, end) {
  return between(file === "web/js/views-workloads.js" ? require("./helpers/workloads-source")() : fs.readFileSync(file, "utf8"), start, end);
}
function run(code, answer) {
  const asked = [], sent = [];
  const ctx = { asked, sent, VM_POWER: { busy: false }, toast() {}, refresh() {}, setTimeout() {}, closeModal() {}, $: () => null,
    ask: async (text, options) => { asked.push([text, options.title, options.ok]); return answer; },
    api: async (url, opts) => { sent.push([url, JSON.parse(opts.body)]); return { detail: "ok" }; },
    vmPowerReview: () => sent.push(["review"]) };
  ctx.window = ctx;
  vm.createContext(ctx);
  vm.runInContext(code, ctx);
  return ctx;
}
const vmPower = slice("web/js/views-vms.js", "window.vmPower = async", "\nconst VM_POWER_WHAT");
const wlScale = slice("web/js/views-workloads.js", "window.wlScale = async", "\n/* Balancing hosts");
const wlRestart = slice("web/js/views-workloads.js", "window.wlRestart = async", "\nwindow.wlDelete");

test("shutting down a VM asks first, and no sends nothing", async () => {
  let t = run(vmPower, false);
  await t.vmPower("lab", "win11", "stop");
  assert.deepEqual(t.asked.map(a => a.slice(1)), [["Shut down", "Shut down"]]);
  assert.deepEqual(t.sent, []);
  t = run(vmPower, true);
  await t.vmPower("lab", "win11", "stop");
  assert.deepEqual(t.sent, [["/api/vm/power", { ns: "lab", name: "win11", action: "stop" }]]);
  // Starting is reviewed instead, not asked.
  t = run(vmPower, true);
  await t.vmPower("lab", "win11", "start");
  assert.deepEqual([t.asked.length, t.sent], [0, [["review"]]]);
});

test("stopping or restarting a container asks first", async () => {
  let t = run(wlScale, false);
  await t.wlScale("lab", "frigate", 0);
  assert.deepEqual(t.asked.map(a => a[1]), ["Stop"]);
  assert.deepEqual(t.sent, []);
  t = run(wlScale, true);
  await t.wlScale("lab", "frigate", 0);
  assert.deepEqual(t.sent, [["/api/scale", { ns: "lab", name: "frigate", replicas: 0, confirm_capacity: false }]]);
  t = run(wlRestart, false);
  await t.wlRestart("lab", "frigate");
  assert.deepEqual([t.asked.map(a => a[1]), t.sent], [["Restart"], []]);
  t = run(wlRestart, true);
  await t.wlRestart("lab", "frigate");
  assert.deepEqual(t.sent, [["/api/restart", { ns: "lab", name: "frigate" }]]);
});
