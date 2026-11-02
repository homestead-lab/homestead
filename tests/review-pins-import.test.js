"use strict";
// Pins the "Review import" dialog (Import from a source) before it moves onto UI.reviewFlow (#909): the dialog,
// the tick and the toast when it is missing, the request, the job it starts, a lost reply that offers a fresh
// review, blocked and incomplete reviews, an older review and the frozen input. (A review asked for while the import is being sent used to draw a second dialog
// under the running send; the flow ignores it, which is the one difference.)
const fs = require("node:fs"), vm = require("node:vm");
const pins = require("./helpers/review-pins"), { settle } = pins;

function world({ blocked = false, missing = false, fail = false, preview = null, job = true } = {}) {
  const log = [], state = { gate: null, fail };
  const fields = { "#importConfirm": { checked: false }, "#importGo": { disabled: false, textContent: "Import", onclick: "orig" } };
  const ctx = { console, URLSearchParams, Map, Set, Date, Promise, encodeURIComponent,
    document: { addEventListener() {}, getElementById: id => fields["#" + id] || null }, STATE: { data: {} },
    capacityNotes: plan => ({ concerns: plan.warnings || [] }),
    $: key => fields[key], $$: () => [], esc: value => String(value).replaceAll("<", "&lt;"),
    toast: (text, tone) => log.push(["toast", text, tone]), closeModal: () => log.push(["closeModal"]), resetPaint: () => log.push(["resetPaint"]), viewImport: () => log.push(["viewImport"]),
    deployCapacityHtml: plan => `<div>${plan.name}</div>`, childModal: (title, body) => log.push(["childModal", title, body]),
    api: async (path, opts) => {
      const body = JSON.parse(opts.body); log.push(["api", path, body]);
      if (path.endsWith("/preview")) { if (preview) return preview(body);
        return { capacity: missing ? null : { blocked, warnings: ["File replacement"] }, capacity_token: "signed",
          volumes: [{ name: "data", create: false, access_mode: "ReadWriteMany", storage_class: "storage" }],
          phases: [{ title: "Copy files", capacity: { name: "copy", warnings: ["RAM warning"] } }, { title: "Imported application", capacity: { name: "app", warnings: ["RAM warning"] } }] }; }
      if (state.gate) await state.gate;
      if (state.fail) throw Error("Lost connection");
      return job ? { job: "homestead-import-app", operation: { id: "import-job" } } : { ok: true };
    } };
  ctx.window = ctx; vm.createContext(ctx); require("./helpers/load-ui")(ctx);
  ctx.jsArg = s => JSON.stringify(String(s ?? "")); ctx.jsq = s => (ctx.esc || String)(ctx.jsArg(s));
  vm.runInContext(require("./helpers/lifecycle-source")(), ctx);
  ctx.noteOperation = op => log.push(["noteOperation", op]); ctx.refreshOperations = () => log.push(["refreshOperations"]);
  ctx.viewImport = () => log.push(["viewImport"]);
  const snap = label => log.push(["state", label, fields["#importGo"].disabled, fields["#importGo"].textContent, String(fields["#importGo"].onclick)]);
  return { ctx, fields, log, state, snap };
}
const body = () => ({ name: "app", image: "example/app:1", mappings: [{ copy: true }], remote_path: "/data" });

pins("import review", "review-pins-import", {
  async gateFrozenInputAndSendOnce() {
    const w = world(), b = body(); await w.ctx.importReview(b); w.snap("reviewed"); b.name = "changed";
    await w.ctx.confirmImport(); w.snap("without the tick");
    w.fields["#importConfirm"].checked = true;
    let release; w.state.gate = new Promise(r => { release = r; });
    const a = w.ctx.confirmImport(), c = w.ctx.confirmImport(); w.snap("sending");
    release(); await a; await c; w.snap("sent");
    return w.log;
  },
  async configurationOnly() {
    const w = world({ job: false }); await w.ctx.importReview(body()); w.fields["#importConfirm"].checked = true; await w.ctx.confirmImport();
    return w.log;
  },
  async lostReplyOffersAFreshReview() {
    const w = world({ fail: true }); await w.ctx.importReview(body()); w.fields["#importConfirm"].checked = true;
    await w.ctx.confirmImport(); w.snap("failed"); await w.ctx.confirmImport();
    w.fields["#importGo"].onclick(); await settle(); w.snap("review again");
    return w.log;
  },
  async blockedIncompleteAndFailedReviews() {
    const out = [];
    for (const options of [{ blocked: true }, { missing: true }, { preview: () => { throw Error("Not found"); } }, { preview: () => ({ capacity: { blocked: false }, capacity_token: "t" }) }]) {
      const w = world(options); await w.ctx.importReview(body()); w.snap("reviewed");
      w.fields["#importConfirm"].checked = true; await w.ctx.confirmImport(); out.push(...w.log);
    }
    return out;
  },
  async olderReviewCannotReplaceANewerOne() {
    const w = world(); let first; const original = w.ctx.api;
    w.ctx.api = (path, opts) => JSON.parse(opts.body).name === "old" ? new Promise(r => { first = r; }) : original(path, opts);
    const old = w.ctx.importReview({ ...body(), name: "old" }); await w.ctx.importReview({ ...body(), name: "new" });
    first({ capacity: { blocked: false, warnings: [] }, capacity_token: "t", phases: [] }); await old;
    w.fields["#importConfirm"].checked = true; await w.ctx.confirmImport(); w.snap("sent");
    return w.log;
  },
});
