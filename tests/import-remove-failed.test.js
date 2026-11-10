"use strict";
// Remove for a failed or cancelled import (#1301): the review lists what the import made, the volumes apart and unticked,
// and the request names a volume only when it was ticked.
const test = require("node:test"), assert = require("node:assert/strict");
const vm = require("node:vm");
const settle = () => new Promise(r => setImmediate(r));

function world(plan) {
  const log = [], boxes = [];
  const ctx = { console, URLSearchParams, Map, Set, Date, Promise, encodeURIComponent,
    document: { addEventListener() {}, getElementById: () => null }, STATE: { data: {} },
    $: key => key === "#imr_workload" ? null : null, $$: sel => sel === ".imr-volume" ? boxes : [],
    esc: value => String(value).replaceAll("<", "&lt;"), defaultNamespace: () => "lab", tip: t => t,
    modal: (title, html) => log.push(["modal", title, html]), closeModal: () => log.push(["closeModal"]),
    toast: (text, tone) => log.push(["toast", text, tone]), resetPaint() {}, viewImport() {},
    ask: async (q, o) => { log.push(["ask", q, o]); return true; },
    api: async (path, opts) => { const body = JSON.parse(opts.body); log.push(["api", path, body]); return path.endsWith("cleanup-plan") ? plan : { message: "Import removed." }; } };
  ctx.window = ctx; vm.createContext(ctx); require("./helpers/load-ui")(ctx);
  ctx.jsArg = s => JSON.stringify(String(s ?? "")); ctx.jsq = s => ctx.esc(ctx.jsArg(s));
  vm.runInContext(require("./helpers/lifecycle-source")(), ctx);
  ctx.viewImport = () => {};
  return { ctx, log, boxes };
}
const PLAN = { journalled: true, removable: true, known: true, workload: "notes", services: ["notes"], copy_job: true,
  volumes: [{ name: "notes-appdata", created: true }, { name: "notes-extra", created: true }] };

test("the review of a failed import lists what it made, and each volume apart and unticked", async () => {
  const w = world(PLAN); await w.ctx.importRemove("homestead-import-notes", "failed", "lab");
  const html = w.log.find(e => e[0] === "modal")[2];
  assert.match(html, /The app · notes/); assert.match(html, /The copy job/);
  assert.equal((html.match(/class="imr-volume"/g) || []).length, 2);
  assert.doesNotMatch(html, /imr-volume"[^>]*checked/, "kept unless ticked");
  assert.match(html, /Remove import<\/button>/);
});

test("Remove sends no volume unless it was ticked, and asks before deleting one", async () => {
  const w = world(PLAN);
  await w.ctx.importRemoveNow("homestead-import-notes", null, "lab");
  assert.deepEqual(w.log.find(e => e[0] === "api").slice(2)[0].remove_volumes, []);
  assert.ok(!w.log.some(e => e[0] === "ask"), "nothing to confirm");
  w.log.length = 0; w.boxes.push({ checked: true, value: "notes-appdata" }, { checked: false, value: "notes-extra" });
  await w.ctx.importRemoveNow("homestead-import-notes", null, "lab");
  assert.deepEqual(w.log.find(e => e[0] === "api")[2].remove_volumes, ["notes-appdata"]);
  assert.ok(w.log.some(e => e[0] === "ask" && /cannot be undone/.test(e[1])));
});

test("the copy job is listed only when it exists, and an app without the stamp is said to be kept", async () => {
  const w = world({ ...PLAN, copy_job: false, workload: "", kept_app: "notes" }); await w.ctx.importRemove("homestead-import-notes", "failed", "lab");
  const html = w.log.find(e => e[0] === "modal")[2];
  assert.doesNotMatch(html, /The copy job/);
  assert.match(html, /The app · notes is kept, since it does not carry this import's stamp/);
});
