"use strict";
// Pins the Move review dialog (after picking a host) before it moves onto UI.reviewFlow (#909): its HTML,
// the consent gate and its toast, requests, success and error states, double click, a pick that makes a
// preview stale, a late reply, and a second review while one is being sent. Compared with
// tests/fixtures/review-pins-host-move.json, recorded from the hand-written version.
const test = require("node:test"), assert = require("node:assert/strict");
const fs = require("node:fs"), vm = require("node:vm");
const FIXTURE = "tests/fixtures/review-pins-host-move.json";

const review = (extra = {}) => ({ capacity_token: "signed", managed: null, capacity: { blocked: false, warnings: [], move: { node: "<host>", mode: "preferred", target: { blocked: false } }, ...extra } });
function world({ response = review(), fail = false } = {}) {
  const log = [], state = { gate: null, fail };
  const fields = { "#hostMoveConfirm": { checked: false }, "#hostMoveGo": { disabled: false, textContent: "Move", onclick: null }, "#mv_unpin": { checked: false }, "#mv_pin": { checked: false }, "#mv_go": { disabled: true } };
  fields["#mbody"] = { innerHTML: "" };
  const c = { console, Date, meter: () => "", tip: () => "", hardwareName: x => x, hardwareTags: () => "", $: s => fields[s], $$: () => [], toast: (text, tone) => log.push(["toast", text, tone]), closeModal: () => log.push(["closeModal"]),
    setTimeout: (fn, ms) => log.push(["setTimeout", ms]), refresh: () => log.push(["refresh"]), STATE: { data: {} },
    esc: v => String(v).replaceAll("<", "&lt;").replaceAll(">", "&gt;"), document: { getElementById: id => fields["#" + id] },
    childModal: (title, body) => log.push(["childModal", title, body]), deployCapacityHtml: () => "capacity report", loadHardwareFeatures: async () => {},
    api: async (url, options) => {
      const body = JSON.parse(options.body); log.push(["api", url, body]);
      if (url === "/api/move/preview") { if (state.previewGate) await state.previewGate; return typeof response === "function" ? response(body) : response; }
      if (state.gate) await state.gate;
      if (state.fail) throw new Error("review expired");
      return { ok: true };
    } };
  c.window = c; vm.createContext(c); require("./helpers/load-ui")(c);
  c.jsArg = s => JSON.stringify(String(s ?? "")); c.jsq = s => (c.esc || String)(c.jsArg(s));
  vm.runInContext(fs.readFileSync("web/js/gitops.js", "utf8"), c);
  vm.runInContext(fs.readFileSync("web/js/move.js", "utf8"), c);
  const snap = label => log.push(["state", label, fields["#hostMoveGo"].disabled, fields["#hostMoveGo"].textContent, typeof fields["#hostMoveGo"].onclick]);
  return { c, fields, log, state, snap };
}
const config = () => ({ ns: "lab", name: "app", node: "b", pin: false });

const scenarios = {
  async consentGateAndSendOnce() {
    const w = world(); await w.c.hostMoveReview(config()); w.snap("reviewed");
    await w.c.confirmHostMove(); w.snap("without the tick");
    w.fields["#hostMoveConfirm"].checked = true;
    let release; w.state.gate = new Promise(r => { release = r; });
    const a = w.c.confirmHostMove(), b = w.c.confirmHostMove(); w.snap("sending");
    release(); await a; await b; w.snap("sent");
    return w.log;
  },
  async reviewedInputNotChangedSelection() {
    const w = world(), body = config(); await w.c.hostMoveReview(body); body.node = "c";
    w.fields["#hostMoveConfirm"].checked = true; await w.c.confirmHostMove();
    return w.log;
  },
  async blockedReview() {
    const w = world({ response: review({ blocked: true }) }); await w.c.hostMoveReview(config()); w.snap("reviewed");
    w.fields["#hostMoveConfirm"].checked = true; await w.c.confirmHostMove(); w.snap("tried");
    return w.log;
  },
  async rejectedSendOffersAnotherReview() {
    const w = world({ fail: true }); await w.c.hostMoveReview(config()); w.fields["#hostMoveConfirm"].checked = true;
    await w.c.confirmHostMove(); w.snap("failed");
    w.fields["#hostMoveGo"].onclick(); await new Promise(r => setImmediate(r)); w.snap("review again");
    return w.log;
  },
  async incompleteAndFailedPreviews() {
    const out = [];
    for (const response of [{ capacity: { blocked: false } }, () => { throw Error("Not found"); }]) {
      const w = world({ response }); await w.c.hostMoveReview(config()); out.push(...w.log);
    }
    return out;
  },
  async pickingAHostMakesAnOpenPreviewStale() {
    const w = world(); let release; w.state.previewGate = new Promise(r => { release = r; });
    const pending = w.c.hostMoveReview(config()); w.c.pickMove("c"); release(); await pending; w.snap("after the late reply");
    return w.log;
  },
  async anOlderPreviewCannotReplaceANewerOne() {
    const w = world(); let first; const original = w.c.api; let n = 0;
    w.c.api = (url, options) => { if (url === "/api/move/preview" && n++ === 0) return new Promise(r => { first = r; }); return original(url, options); };
    const old = w.c.hostMoveReview({ ...config(), node: "old" }); await w.c.hostMoveReview({ ...config(), node: "new" });
    first(review()); await old; w.fields["#hostMoveConfirm"].checked = true; await w.c.confirmHostMove();
    return w.log;
  },
  async noReviewWhileOneIsBeingSent() {
    const w = world(); await w.c.hostMoveReview(config()); w.fields["#hostMoveConfirm"].checked = true;
    let release; w.state.gate = new Promise(r => { release = r; });
    const pending = w.c.confirmHostMove(); await w.c.hostMoveReview({ ...config(), node: "other" }); release(); await pending; w.snap("after");
    return w.log;
  },
  async pickerPlanAndALatePlan() {
    const w = world(); const original = w.c.api; let first; let n = 0;
    const plan = name => ({ current: "a", candidates: [{ name: "a", current: true, ok: true, pods_wl: 1, score: 1, cpu_after: 1, mem_after: 1, hardware: {} },
      { name, ok: true, current: false, pods_wl: 0, score: 2, cpu_after: 2, mem_after: 2, hardware: {} }], recommended: name, requirements: { devices: [], resources: {}, labels: {} } });
    w.c.api = (url, options) => { if (url.startsWith("/api/move/plan")) return n++ === 0 ? new Promise(r => { first = r; }) : Promise.resolve(plan("<new>")); return original(url, options); };
    const old = w.c.moveWorkload("app", "lab"); await w.c.moveWorkload("app", "lab"); w.log.push(["picker", w.fields["#mbody"].innerHTML]);
    first(plan("<old>")); await old; w.log.push(["picker after the late plan", w.fields["#mbody"].innerHTML]);
    w.c.api = () => { throw Error("no plan"); }; await w.c.moveWorkload("app", "lab"); w.log.push(["picker failed", w.fields["#mbody"].innerHTML]);
    return w.log;
  },
  async doMoveNowNeedsAHost() {
    const w = world(); await w.c.doMoveNow("lab", "app"); w.snap("nothing picked");
    w.c.pickMove("b"); await w.c.doMoveNow("lab", "app"); w.snap("picked");
    return w.log;
  },
};

for (const [name, run] of Object.entries(scenarios)) test(`host move review: ${name}`, async () => {
  const transcript = JSON.parse(JSON.stringify(await run()));
  const all = fs.existsSync(FIXTURE) ? JSON.parse(fs.readFileSync(FIXTURE, "utf8")) : {};
  if (process.env.RECORD_PINS) { all[name] = transcript; fs.writeFileSync(FIXTURE, JSON.stringify(all, null, 1) + "\n"); return; }
  assert.deepEqual(transcript, all[name]);
});
