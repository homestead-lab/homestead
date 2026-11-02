"use strict";
// What the shared review flow changes in the host Move review (#909), one test each, written to fail on the code before it:
// an error from a preview that a newer review has replaced is not shown; Back, or reopening Move, while a send is running does
// not open a second dialog; a send that succeeds no longer drops a host-picker plan that is still loading.
const test = require("node:test"), assert = require("node:assert/strict");
const fs = require("node:fs"), vm = require("node:vm");

const review = { capacity_token: "signed", managed: null, capacity: { blocked: false, warnings: [], move: { node: "<host>", mode: "preferred", target: { blocked: false } } } };
function world() {
  const log = [], gates = {}, state = {};
  const fields = { "#hostMoveConfirm": { checked: false }, "#hostMoveGo": { disabled: false, textContent: "Move", onclick: null }, "#mv_unpin": { checked: false }, "#mv_pin": { checked: false }, "#mv_go": { disabled: true },
    "#mbody": { innerHTML: "" } };
  const c = { console, Date, $: s => fields[s], $$: () => [], toast: (text, tone) => log.push(["toast", text, tone]), closeModal: () => log.push(["closeModal"]), setTimeout: () => 0, refresh() {}, STATE: { data: {} },
    meter: () => "", tip: () => "", hardwareName: x => x, hardwareTags: () => "", loadHardwareFeatures: async () => {},
    esc: v => String(v).replaceAll("<", "&lt;").replaceAll(">", "&gt;"), document: { getElementById: id => fields["#" + id] || null },
    childModal: (title, body) => log.push(["childModal", title]), deployCapacityHtml: () => "capacity report",
    api: async (url, options) => {
      const body = options?.body ? JSON.parse(options.body) : null; log.push(["api", url, body]);
      if (url === "/api/move/preview") return state.preview ? state.preview(body) : review;
      if (url.startsWith("/api/move/plan")) return state.plan ? state.plan() : { current: "a", candidates: [{ name: "a", current: true, ok: true, pods_wl: 1, score: 1, cpu_after: 1, mem_after: 1, hardware: {} }, { name: "b", ok: true, current: false, pods_wl: 0, score: 2, cpu_after: 2, mem_after: 2, hardware: {} }], requirements: { devices: [], resources: {}, labels: {} } };
      if (state.send) await state.send;
      return { ok: true };
    } };
  c.window = c; vm.createContext(c); require("./helpers/load-ui")(c);
  c.jsArg = s => JSON.stringify(String(s ?? "")); c.jsq = s => (c.esc || String)(c.jsArg(s));
  vm.runInContext(fs.readFileSync("web/js/gitops.js", "utf8"), c);
  vm.runInContext(fs.readFileSync("web/js/move.js", "utf8"), c);
  return { c, fields, log, state };
}
const body = (node = "b") => ({ ns: "lab", name: "app", node, pin: false });
const settle = () => new Promise(r => setImmediate(r));

test("an error from a preview a newer review has replaced is not shown", async () => {
  const w = world(); let fail;
  w.state.preview = config => config.node === "old" ? new Promise((_, reject) => { fail = reject; }) : review;
  const old = w.c.hostMoveReview(body("old")); await settle();
  await w.c.hostMoveReview(body("new"));
  fail(new Error("the old preview failed")); await old;
  assert.deepEqual(w.log.filter(e => e[0] === "toast"), [], "nothing about the replaced preview");
});

test("Back, or reopening Move, while a send is running does not open a second dialog", async () => {
  const w = world(); let release; w.state.send = new Promise(r => { release = r; });
  await w.c.hostMoveReview(body()); w.fields["#hostMoveConfirm"].checked = true;
  const sending = w.c.confirmHostMove(); await settle();
  w.c.invalidateHostMove();                       // what Back's button does first
  await w.c.hostMoveReview(body("c"));            // and Move asked for again
  release(); await sending;
  assert.equal(w.log.filter(e => e[0] === "api" && e[1] === "/api/move/preview").length, 1, "one preview");
  assert.equal(w.log.filter(e => e[0] === "childModal").length, 1, "one dialog");
});

test("a send that succeeds does not drop a host-picker plan that is still loading", async () => {
  const w = world(); await w.c.hostMoveReview(body()); w.fields["#hostMoveConfirm"].checked = true;
  let release; w.state.send = new Promise(r => { release = r; });
  const sending = w.c.confirmHostMove(); await settle();
  let plan; w.state.plan = () => new Promise(r => { plan = r; });
  const picker = w.c.moveWorkload("app", "lab"); await settle();   // the picker asks for its plan while the send is running
  release(); await sending;                                          // the send succeeds
  plan({ current: "a", candidates: [{ name: "a", current: true, ok: true, pods_wl: 1, score: 1, cpu_after: 1, mem_after: 1, hardware: {} }, { name: "b", ok: true, current: false, pods_wl: 0, score: 2, cpu_after: 2, mem_after: 2, hardware: {} }], requirements: { devices: [], resources: {}, labels: {} } });
  await picker;
  assert.match(w.fields["#mbody"].innerHTML, /Currently on/, "the plan was drawn");
});
