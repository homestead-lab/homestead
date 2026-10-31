"use strict";
// A page seen before opens showing what it showed last time (#987): one
// snapshot per address, and only the 20 most recent are kept, so a tab left
// open for days through many host and app pages does not keep them all.
const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const between = require("./helpers/between");

function load() {
  const core = fs.readFileSync("web/js/core.js", "utf8");
  const host = { dataset: {}, classList: { add() {}, remove() {} }, innerHTML: "" };
  const ctx = { console, Map, location: { search: "" }, STATE: { view: "nodes" }, V: () => host,
    pageTabs: () => "", filterBar: () => "", pageSkeleton: view => `skeleton:${view}`,
    enhanceActions() {}, loadAppIcons() {}, dropOrphanTooltip() {} };
  ctx.window = ctx;
  vm.createContext(ctx);
  vm.runInContext(between(core, "const PAGE_SNAPSHOT", "function pageSkeleton(view)"), ctx);
  vm.runInContext(between(core, "function paint(html)", "function morph(a, b)"), ctx);
  const visit = (search, html) => { ctx.location.search = search; host.dataset = {}; vm.runInContext(`paint(${JSON.stringify(html)})`, ctx); };
  const reopen = search => { ctx.location.search = search; vm.runInContext("pagePlaceholder('nodes')", ctx); return host.innerHTML; };
  return { ctx, visit, reopen, size: () => vm.runInContext("PAGE_SNAPSHOT.size", ctx) };
}

test("the 21st page painted drops the snapshot of the one seen longest ago", () => {
  const t = load();
  for (let i = 1; i <= 20; i++) t.visit(`?node=node-${i}`, `<p>node-${i}</p>`);
  assert.equal(t.size(), 20);
  assert.equal(t.reopen("?node=node-1"), "<p>node-1</p>", "twenty are all kept");
  t.visit("?node=node-21", "<p>node-21</p>");
  assert.equal(t.size(), 20, "never more than twenty");
  assert.equal(t.reopen("?node=node-1"), "skeleton:nodes", "the oldest is gone: its page opens with the skeleton");
  assert.equal(t.reopen("?node=node-2"), "<p>node-2</p>");
  assert.equal(t.reopen("?node=node-21"), "<p>node-21</p>");
});

test("painting a page again makes it the most recent, so a page in use is not dropped", () => {
  const t = load();
  for (let i = 1; i <= 20; i++) t.visit(`?node=node-${i}`, `<p>node-${i}</p>`);
  t.visit("?node=node-1", "<p>node-1 again</p>");
  t.visit("?node=node-21", "<p>node-21</p>");
  assert.equal(t.reopen("?node=node-1"), "<p>node-1 again</p>", "the list you keep returning to stays");
  assert.equal(t.reopen("?node=node-2"), "skeleton:nodes", "the one left longest goes instead");
});
