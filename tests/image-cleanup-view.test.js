"use strict";
const test = require("node:test"), assert = require("node:assert/strict"), fs = require("node:fs"), vm = require("node:vm");
const between = require("./helpers/between");

/* The Image cache page's unused images: how many and how much, Clean all
   unused, and the automatic clean-up's setting and last run. */
function load() {
  const actions = [];
  const ctx = { esc: v => String(v ?? ""), bytesText: b => `${Math.round(b / 1024 ** 2)} MiB`, fmtAgo: s => `${Math.round(s / 60)}m ago`,
    serviceRow: (title, pill, detail, bar) => `<row>${title}|${pill}|${detail}|${bar}</row>`,
    actionBar: rows => { actions.push(...rows.filter(Boolean)); return rows.filter(Boolean).map(r => `[${r.label}]`).join(""); } };
  ctx.window = ctx;
  // The real UI.chip, from its own context so the stubs above (serviceRow, actionBar) stay.
  const ui = { window: {}, esc: v => String(v ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]) };
  vm.createContext(ui);
  vm.runInContext(fs.readFileSync("web/js/ui.js", "utf8"), ui);
  ctx.UI = ui.window.UI;
  vm.createContext(ctx);
  const src = require("./helpers/lifecycle-source")();
  vm.runInContext(between(src, "const imageUnused = d =>", "window.imageCleanupRow = imageCleanupRow;"), ctx);
  return { ctx, actions };
}
const images = { images: [
  { name: "redis:7.2", digest: "sha256:c", size_mb: 41, nodes: ["n1", "n2", "n3"], protected: false, system: false },
  { name: "frigate:stable", digest: "sha256:a", size_mb: 2480, nodes: ["n2"], protected: true, system: false },
  { name: "rancher/agent", digest: "sha256:r", size_mb: 300, nodes: ["n1"], protected: false, system: true },
  { name: "no-digest", digest: "", size_mb: 5, nodes: ["n1"], protected: false, system: false }] };

test("only images nothing keeps count, by every host's copy", () => {
  const { ctx, actions } = load();
  const html = ctx.imageCleanupRow(images, { auto: false, after_days: 14 });
  assert.match(html, /1 unused image · 123 MiB across hosts/);
  assert.match(html, /Automatic clean-up\|<span class="ui-chip neutral">off/);
  assert.deepEqual(actions.map(a => [a.label, a.need, !!a.disabled]), [["Clean all unused", "admin", false], ["Turn on", "admin", false]]);
});

test("the automatic clean-up says its days and its last run", () => {
  const { ctx } = load();
  const html = ctx.imageCleanupRow(images, { auto: true, after_days: 30, last: { at: Date.now() / 1000 - 600, removed: 2, freed_mb: 1384 } });
  assert.match(html, /on · 30 days/);
  assert.match(html, /Images unused for 30 days are removed from every host, checked hourly · last: removed 2 \(1384 MiB\) 10m ago/);
});

test("with nothing unused, Clean all unused is off; an unread setting says so", () => {
  const { ctx, actions } = load();
  const html = ctx.imageCleanupRow({ images: [images.images[1]] }, { error: "HTTP 503" });
  assert.match(html, /0 unused images/);
  assert.match(html, /could not be read/);
  assert.deepEqual(actions.map(a => !!a.disabled), [true, true]);
});
