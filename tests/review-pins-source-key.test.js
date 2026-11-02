"use strict";
// Pins the "Verify source" review (import sources) before it moves onto UI.reviewFlow (#909): the loading and review
// dialogs, the acknowledgement and the button's state, the one request, a refused save, a changed key, an older
// scan, a scan asked for while saving and the dialog closing before the answer.
const fs = require("node:fs"), vm = require("node:vm");
const pins = require("./helpers/review-pins");

const SCAN = { name: "tower", key: "public-key", fingerprint: "SHA256:fixture", algorithm: "ssh-ed25519", capacity_token: "signed", connection: { host: "192.0.2.10", port: 22 } };
function world({ scan = SCAN, fail = false } = {}) {
  const log = [], state = { gate: null, fail };
  const fields = { "#sourceKeyLoading": {}, "#sourceKeyAck": { checked: false }, "#sourceKeyApply": { disabled: true },
    "#mbody": { _html: "", get innerHTML() { return this._html; }, set innerHTML(v) { this._html = v; delete fields["#sourceKeyLoading"]; log.push(["mbody", v]); } } };
  const ctx = { console, Map, Set, Date, Promise, URLSearchParams, encodeURIComponent, STATE: { data: {} }, $: key => fields[key], $$: () => [],
    document: { addEventListener() {}, getElementById: id => fields["#" + id] || null },
    esc: x => String(x).replaceAll("<", "&lt;"), toast: (text, tone) => log.push(["toast", text, tone]),
    modal: (title, html, wide, context) => { log.push(["modal", title, html, wide, context]); fields["#sourceKeyLoading"] = {}; },
    closeModal: () => log.push(["closeModal"]), resetPaint() {}, importRepaint: () => log.push(["importRepaint"]),
    api: async (path, opts) => {
      const body = JSON.parse(opts.body); log.push(["api", path, body]);
      if (path.endsWith("/scan")) { if (state.scan) return state.scan(body); return typeof scan === "function" ? scan(body) : scan; }
      if (state.gate) await state.gate;
      if (state.fail) throw Error("lost response");
      return { ok: true };
    } };
  ctx.window = ctx; vm.createContext(ctx); require("./helpers/load-ui")(ctx);
  ctx.jsArg = s => JSON.stringify(String(s ?? "")); ctx.jsq = s => (ctx.esc || String)(ctx.jsArg(s));
  vm.runInContext(require("./helpers/lifecycle-source")(), ctx);
  ctx.viewImport = () => {};
  const snap = label => log.push(["state", label, fields["#sourceKeyApply"].disabled]);
  return { ctx, fields, log, state, snap };
}

pins("source key review", "review-pins-source-key", {
  async gateAndTrustOnce() {
    const w = world(); await w.ctx.srcVerify("tower"); w.snap("reviewed");
    await w.ctx.sourceKeyTrust(); w.snap("without the tick");
    w.fields["#sourceKeyAck"].checked = true; w.ctx.sourceKeyReady(); w.snap("ticked");
    let release; w.state.gate = new Promise(r => { release = r; });
    const a = w.ctx.sourceKeyTrust(), b = w.ctx.sourceKeyTrust(); w.snap("sending");
    release(); await a; await b; w.snap("sent");
    return w.log;
  },
  async changedKey() {
    const w = world({ scan: { ...SCAN, changed: true, algorithm: "ecdsa-sha2-nistp256" } }); await w.ctx.srcVerify("tower");
    return w.log;
  },
  async incompleteAndFailedScans() {
    const out = [];
    for (const scan of [{ ...SCAN, key: "" }, { ...SCAN, name: "other" }, () => { throw Error("no route"); }]) {
      const w = world({ scan }); await w.ctx.srcVerify("tower"); w.snap("reviewed");
      w.fields["#sourceKeyAck"].checked = true; await w.ctx.sourceKeyTrust(); out.push(...w.log);
    }
    return out;
  },
  async lostReplyIsNotRetried() {
    const w = world({ fail: true }); await w.ctx.srcVerify("tower"); w.fields["#sourceKeyAck"].checked = true;
    await w.ctx.sourceKeyTrust(); w.snap("failed"); await w.ctx.sourceKeyTrust(); w.snap("tried again");
    return w.log;
  },
  async lostReplyAfterTheDialogMovedOn() {
    const w = world({ fail: true }); await w.ctx.srcVerify("tower"); w.fields["#sourceKeyAck"].checked = true;
    let release; w.state.gate = new Promise(r => { release = r; });
    const pending = w.ctx.sourceKeyTrust(); w.fields["#sourceKeyApply"] = { disabled: false }; release(); await pending; w.snap("after");
    return w.log;
  },
  async olderScanCannotReplaceANewerOne() {
    const w = world(); let first; const original = w.ctx.api;
    w.ctx.api = (path, opts) => JSON.parse(opts.body).name === "old" ? new Promise(r => { first = r; }) : original(path, opts);
    const old = w.ctx.srcVerify("old"); await w.ctx.srcVerify("tower"); first({ ...SCAN, name: "old" }); await old; w.snap("after the late scan");
    w.fields["#sourceKeyAck"].checked = true; await w.ctx.sourceKeyTrust(); w.snap("sent");
    return w.log;
  },
  async scanTheDialogHasLeftIsDropped() {
    const w = world(); let finish; w.ctx.api = () => new Promise(r => { finish = r; });
    const pending = w.ctx.srcVerify("tower"); delete w.fields["#sourceKeyLoading"]; finish(SCAN); await pending; w.snap("dropped");
    return w.log;
  },
  async noScanWhileSaving() {
    const w = world(); await w.ctx.srcVerify("tower"); w.fields["#sourceKeyAck"].checked = true;
    let release; w.state.gate = new Promise(r => { release = r; });
    const pending = w.ctx.sourceKeyTrust(); await w.ctx.srcVerify("tower"); release(); await pending; w.snap("after");
    return w.log;
  },
});
