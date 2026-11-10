"use strict";
// What the shared review flow changes in the Import review (#909), one test each, written to fail on the code before it: a review
// asked for while the import is being sent is ignored, and an error from a preview a newer review replaced is not shown.
const test = require("node:test"), assert = require("node:assert/strict");
const fs = require("node:fs"), vm = require("node:vm");
const settle = () => new Promise(r => setImmediate(r));

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


test("a review asked for while the import is being sent is ignored, not drawn over it", async () => {
  const w = world(); await w.ctx.importReview(body()); w.fields["#importConfirm"].checked = true;
  let release; w.state.gate = new Promise(r => { release = r; });
  const sending = w.ctx.confirmImport(); await settle();
  await w.ctx.importReview({ ...body(), name: "other" });
  release(); await sending;
  assert.equal(w.log.filter(e => e[0] === "api" && e[1] === "/api/import/preview").length, 1, "one preview");
  assert.equal(w.log.filter(e => e[0] === "childModal").length, 1, "one dialog");
});

test("an error from a preview a newer review has replaced is not shown", async () => {
  let fail;
  const w = world({ preview: config => config.name === "old" ? new Promise((_, reject) => { fail = reject; }) : { capacity: { blocked: false, warnings: [] }, capacity_token: "t", phases: [] } });
  const old = w.ctx.importReview({ ...body(), name: "old" }); await settle();
  await w.ctx.importReview({ ...body(), name: "new" });
  fail(new Error("the old preview failed")); await old;
  assert.deepEqual(w.log.filter(e => e[0] === "toast"), []);
});

test("a send that fails after its dialog has gone does not throw into the page (#1317)", async () => {
  const w = world({ fail: true }); await w.ctx.importReview(body()); w.fields["#importConfirm"].checked = true;
  let release; w.state.gate = new Promise(r => { release = r; });
  const sending = w.ctx.confirmImport(); await settle();
  delete w.fields["#importGo"];
  release(); await sending;
  assert.ok(w.log.some(e => e[0] === "toast" && /Lost connection/.test(e[1])), "the failure is still shown");
});
