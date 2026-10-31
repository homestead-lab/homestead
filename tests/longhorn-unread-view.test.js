"use strict";
// Longhorn that could not be read is said, not shown as no copies or a healthy cluster (#1002).
const test = require("node:test"), assert = require("node:assert/strict"), fs = require("node:fs"), vm = require("node:vm");
const between = require("./helpers/between");
const context = { window: {}, console, Set, Map, Promise, document: { addEventListener() {} }, STATE: { data: {} },
  $: () => null, $$: () => [], esc: x => String(x ?? "").replaceAll("<", "&lt;").replaceAll('"', "&quot;"),
  meter: pct => `<meter value="${pct}"></meter>`, tip: () => "?", toast() {} };
vm.createContext(context);
context.jsArg = s => JSON.stringify(String(s ?? "")); context.jsq = s => (context.esc || String)(context.jsArg(s));
for (const f of ["web/js/ui.js", "web/js/views-storage.js", "web/js/views-overview.js"]) vm.runInContext(fs.readFileSync(f, "utf8"), context);

test("a volume whose copies Longhorn couldn't read says copies unknown, not one copy", () => {
  const x = { name: "pvc-1", replicas: 2, copies: null };
  assert.match(context.volumeCopiesLine(x), /copies unknown/);
  assert.match(context.volumeCopiesTip(x), /could not be read/);
  assert.doesNotMatch(context.volumeCopiesTip(x), /^2 copies$/);
  assert.equal(context.volumeCopiesLine({ ...x, copies: [] }), "");
});

test("an unknown verdict that says why shows the reason on the pill, on every page", () => {
  // The server sets "unknown" and the reason while Longhorn's volumes are
  // unread (homestead_cluster.unknown_when_unread); the pill keeps it.
  const pill = { className: "", title: "", dataset: {}, textContent: "CHECKING" };
  const ctx = { $: sel => sel === "#healthPill" ? pill : null, fmtAgo: () => "" };
  ctx.window = ctx; vm.createContext(ctx);
  const src = fs.readFileSync("web/js/views-overview.js", "utf8");
  vm.runInContext(`${between(src, "function paintHealthPill(", "\n}\n", { through: true })}; this.paintHealthPill = paintHealthPill;`, ctx);
  ctx.paintHealthPill({ state: "unknown", summary: "Longhorn's volumes could not be read, so their health is not known", age_s: 3 });
  assert.equal(pill.textContent, "UNKNOWN");
  assert.equal(pill.title, "Longhorn's volumes could not be read, so their health is not known");
  ctx.paintHealthPill(null);
  assert.match(pill.title, /could not read/i);
});
