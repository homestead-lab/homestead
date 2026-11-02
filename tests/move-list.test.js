"use strict";
const test = require("node:test"), assert = require("node:assert/strict"), fs = require("node:fs"), vm = require("node:vm");
const between = require("./helpers/between");

/* The list of moves from linked clusters. A failed read used to show as no
   moves at all - the card even hid itself - and "Clear finished" went ahead
   without its confirmation, as if nothing were there to clear. */
function setup(answers) {
  const els = { "#movesList": { innerHTML: "old" }, "#fleetMovesCard": { innerHTML: "", hidden: false } };
  const sent = [], toasts = [];
  const ctx = { console, Date, Promise, Set, clearTimeout, setTimeout() {}, document: { addEventListener() {} }, STATE: { data: {} },
    $: key => els[key], $$: () => [], toast: (text, tone) => toasts.push([text, tone]), ask: async () => true, closeModal() {}, tip: t => t,
    esc: value => String(value ?? "").replace(/[&<>"]/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c])),
    api: async (path, opts) => {
      if (opts?.method === "POST") { sent.push(path); return { detail: "ok" }; }
      const a = answers[path]; if (a instanceof Error) throw a; return a;
    } };
  ctx.window = ctx; vm.createContext(ctx); require("./helpers/load-ui")(ctx); ctx.jsq = value => ctx.esc(JSON.stringify(value));
  vm.runInContext(require("./helpers/lifecycle-source")(), ctx);
  const fleet = fs.readFileSync("web/js/fleet.js", "utf8");
  vm.runInContext(between(fleet, "window.fleetMovesPaint = async", "\nwindow.fleetSetMode"), ctx);
  Object.assign(ctx, { moveSourceRow: () => "", moveSourceCheck() {}, fleetPendingMove() {} });
  return { ctx, els, sent, toasts };
}
const down = new Error("HTTP 503");

test("a failed read of the moves says so with Retry, never an empty list", async () => {
  const t = setup({ "/api/move/moves": down });
  await t.ctx.watchMoves();
  assert.match(t.els["#movesList"].innerHTML, /could not be read: HTTP 503/);
  assert.match(t.els["#movesList"].innerHTML, /Retry/);
});

test("the Linked clusters card shows a failed read instead of hiding", async () => {
  const t = setup({ "/api/move/moves": down, "/api/move/clusters": [] });
  await t.ctx.fleetMovesPaint();
  assert.equal(t.els["#fleetMovesCard"].hidden, false);
  assert.match(t.els["#fleetMovesCard"].innerHTML, /could not be read/);
});

test("Clear finished clears nothing when the moves could not be read", async () => {
  const t = setup({ "/api/move/moves": down });
  await t.ctx.moveDismiss();
  assert.deepEqual(t.sent, []);
  assert.match(t.toasts[0][0], /could not be read/);
});
