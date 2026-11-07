"use strict";
// What the shared review flow changes in the Edit review (#909), one test each, written to fail on the code before it:
// a review asked for while a save is being sent is ignored, and an error from a preview a newer review replaced is not shown.
const test = require("node:test"), assert = require("node:assert/strict");
const fs = require("node:fs"), vm = require("node:vm");
const settle = () => new Promise(r => setImmediate(r));

const REVIEW = { capacity_token: "token", managed: null, capacity: { blocked: false, requires_confirmation: true, candidates: [], warnings: ["unknown <memory>"], additional: 1 } };
function world({ review = REVIEW, fail = false, preview = null } = {}) {
  const log = [], state = { gate: null, fail };
  const fields = { "#editCapacityConfirm": { checked: false }, "#editManagedConfirm": { checked: false },
    "#editGo": { disabled: false, textContent: "Save", onclick: "orig" }, "#editSaveNotice": { scrollIntoView() {} },
    "#mbody": { insertAdjacentHTML: (_where, html) => log.push(["insertAdjacentHTML", html]) } };
  const c = { window: {}, console, URLSearchParams, Map, document: { addEventListener() {}, getElementById: id => fields["#" + id] || null },
    $: s => fields[s], esc: s => String(s).replaceAll("<", "&lt;").replaceAll(">", "&gt;"),
    childModal: (title, body) => log.push(["childModal", title, body]), modal: (title, body) => log.push(["modal", title, body]),
    toast: (text, tone) => log.push(["toast", text, tone]), closeModal: () => log.push(["closeModal"]), refresh: () => log.push(["refresh"]),
    setTimeout: (fn, ms) => log.push(["setTimeout", ms]),
    api: async (path, options) => {
      const body = JSON.parse(options.body); log.push(["api", path, body]);
      if (path === "/api/edit/preview") { if (preview) return preview(body); return typeof review === "function" ? review(body) : review; }
      if (state.gate) await state.gate;
      if (state.fail) throw new Error("Review expired");
      return { ok: true, name: "demo", operation: body.containers?.[0]?.volumes ? { id: "copy-job" } : undefined };
    } };
  c.window = c; vm.createContext(c); require("./helpers/core-text")(c);
  c.jsArg = s => JSON.stringify(String(s ?? "")); c.jsq = s => (c.esc || String)(c.jsArg(s));
  c.noteOperation = op => log.push(["noteOperation", op]); c.startOperationChecks = () => log.push(["startOperationChecks"]);
  for (const f of ["web/js/ui.js", "web/js/gitops.js"]) vm.runInContext(fs.readFileSync(f, "utf8"), c);
  vm.runInContext(require("./helpers/workloads-source")(), c);
  vm.runInContext(require("./helpers/lifecycle-source")(), c);
  const snap = label => log.push(["state", label, fields["#editGo"].disabled, fields["#editGo"].textContent, String(fields["#editGo"].onclick)]);
  return { c, fields, log, state, snap };
}



const body = () => ({ ns: "lab", name: "demo", containers: [{ name: "one", memory: "1Gi" }], node: "host1" });

test("a review asked for while a save is being sent is ignored, not drawn over it", async () => {
  const w = world(), b = body(); await w.c.editReview(b); w.fields["#editCapacityConfirm"].checked = true;
  let release; w.state.gate = new Promise(r => { release = r; });
  const sending = w.c.confirmEdit(); await settle();
  await w.c.editReview({ ...b, name: "other" });
  release(); await sending;
  assert.equal(w.log.filter(e => e[0] === "api" && e[1] === "/api/edit/preview").length, 1, "one preview");
  assert.equal(w.log.filter(e => e[0] === "childModal").length, 1, "one dialog");
});

test("an error from a preview a newer review has replaced is not shown", async () => {
  let fail;
  const preview = config => config.name === "old" ? new Promise((_, reject) => { fail = reject; }) : REVIEW;
  const w2 = world({ preview });
  const old = w2.c.editReview({ ...body(), name: "old" }); await settle();
  await w2.c.editReview({ ...body(), name: "new" });
  fail(Object.assign(new Error("the old preview failed"), { status: 500 })); await old;
  assert.deepEqual(w2.log.filter(e => e[0] === "toast"), [], "nothing about the replaced preview");
});
