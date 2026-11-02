"use strict";
// Pins the Edit app review (Save, then Review app changes / Rename app / Move container data) before it moves
// onto UI.reviewFlow (#909): its HTML, the acknowledgement gate and its toast, requests, success, the refused
// send that must be inspected, a lost preview, an older preview and the frozen input.
const fs = require("node:fs"), vm = require("node:vm");
const pins = require("./helpers/review-pins"), { settle } = pins;

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

pins("edit review", "review-pins-edit", {
  async gateAndSendOnce() {
    const w = world(), b = body(); await w.c.editReview(b); w.snap("reviewed"); b.containers[0].memory = "20Gi";
    await w.c.confirmEdit(); w.snap("without the tick");
    w.fields["#editCapacityConfirm"].checked = true;
    let release; w.state.gate = new Promise(r => { release = r; });
    const first = w.c.confirmEdit(), second = w.c.confirmEdit(); w.snap("sending");
    release(); await first; await second; w.snap("sent");
    return w.log;
  },
  async rename() {
    const w = world({ review: { ...REVIEW, managed: { by: "Flux" }, capacity: { ...REVIEW.capacity, rename: { from: "demo", to: "demo2" } } } });
    await w.c.editReview({ ...body(), workload_name: "demo2" }); w.snap("reviewed");
    w.fields["#editCapacityConfirm"].checked = true; await w.c.confirmEdit(); w.snap("managed not ticked");
    w.fields["#editManagedConfirm"].checked = true; await w.c.confirmEdit(); w.snap("sent");
    return w.log;
  },
  async blockedAndIncomplete() {
    const w = world({ review: { ...REVIEW, capacity: { ...REVIEW.capacity, blocked: true } } }); await w.c.editReview(body()); w.snap("blocked");
    w.fields["#editCapacityConfirm"].checked = true; await w.c.confirmEdit();
    const v = world({ review: { capacity: { blocked: false } } }); await v.c.editReview(body());
    const x = world({ preview: () => { const e = new Error("Not found"); e.status = 404; throw e; } }); await x.c.editReview(body());
    return [...w.log, ...v.log, ...x.log];
  },
  async copyingStorage() {
    const w = world({ review: { ...REVIEW, capacity: { ...REVIEW.capacity, copy_helper: { blocked: false, warnings: [] } } } });
    await w.c.editReview({ ...body(), containers: [{ name: "one", volumes: [{ path: "/data", source: "new", copy_from: { claim: "old", sub_path: "" } }] }] }); w.snap("reviewed");
    w.fields["#editCapacityConfirm"].checked = true; await w.c.confirmEdit(); w.snap("sent");
    return w.log;
  },
  async refusedSendMustBeInspected() {
    const w = world({ fail: true }); await w.c.editReview(body()); w.fields["#editCapacityConfirm"].checked = true;
    await w.c.confirmEdit(); w.snap("failed");
    await w.c.confirmEdit(); await w.c.editReview(body()); w.snap("tried again");
    return w.log;
  },
  async olderPreviewCannotReplaceANewerOne() {
    const w = world(); const original = w.c.api; let finish;
    w.c.api = (path, options) => JSON.parse(options.body).name === "old" ? new Promise(r => { finish = r; }) : original(path, options);
    const old = w.c.editReview({ ...body(), name: "old" }); await w.c.editReview({ ...body(), name: "new" });
    finish(REVIEW); await old; w.fields["#editCapacityConfirm"].checked = true; await w.c.confirmEdit(); w.snap("sent");
    return w.log;
  },
  async startsAfterRefusalIsShownBesideIt() {
    const w = world({ preview: () => { const e = new Error("a loop: a -> b -> a"); e.status = 400; throw e; } });
    await w.c.editReview({ ...body(), starts_after: ["b"] });
    return w.log;
  },
});
