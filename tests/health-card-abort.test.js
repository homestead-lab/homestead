"use strict";
// The health card's reads are the page's own (#987): leaving the page cancels
// them like the page's other reads (#705), and a read abandoned that way does
// not hold the health card of the page you went to.
const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");

function load() {
  const card = { dataset: { insight: "full" }, cloneNode: () => ({ innerHTML: "" }) };
  const calls = [];
  const ctx = { console, Map, Set, Promise, Date, Math, JSON, Object, Array, Number, String,
    document: { querySelectorAll: selector => selector === "[data-insight]" ? [card] : [], querySelector: () => null, addEventListener() {} },
    STATE: { platform: {} }, esc: String, jsArg: s => JSON.stringify(String(s ?? "")), icon: () => "", morph() {},
    setTimeout, clearTimeout, setInterval: () => 0, clearInterval() {} };
  ctx.window = ctx; ctx.jsq = ctx.jsArg; ctx.window.NAV_TOKEN = 0;
  ctx.api = (path, opts) => { const call = { path, opts, token: ctx.window.NAV_TOKEN }; calls.push(call); return ctx.answer(call); };
  ctx.answer = () => Promise.resolve(null);
  vm.createContext(ctx);
  vm.runInContext(fs.readFileSync("web/js/ui.js", "utf8"), ctx);
  ctx.UI = ctx.window.UI;
  vm.runInContext(fs.readFileSync("web/js/insights.js", "utf8"), ctx);
  return { ctx, calls, H: ctx.window.HealthInsights };
}

test("the health card's reads are ordinary page reads, cancelled when the page is left", async () => {
  const { calls, H } = load();
  await H.load(true);
  assert.ok(calls.length >= 6, `the full card reads its sources (${calls.length})`);
  assert.deepEqual(calls.filter(c => c.opts?.keep).map(c => c.path), [], "none is kept past leaving the page");
});

test("a read abandoned by leaving the page does not hold the next page's health card", async () => {
  const { ctx, calls, H } = load();
  // Leaving the page abandons a read: its promise never settles (core.js api()).
  ctx.answer = () => new Promise(() => {});
  H.load(true);
  const first = calls.length;
  assert.ok(first > 0);
  ctx.window.NAV_TOKEN++;
  ctx.answer = () => Promise.resolve(null);
  const next = H.load(true);
  const settled = await Promise.race([next.then(() => "loaded"), new Promise(r => setTimeout(() => r("still waiting"), 200))]);
  assert.equal(settled, "loaded", "the new page's card loads instead of waiting on the abandoned one");
  assert.ok(calls.length > first, "and reads its sources for the new page");
  assert.ok(calls.slice(first).every(c => c.token === 1));
});

test("on the same page, a second load waits for the one running instead of reading beside it", async () => {
  const { ctx, calls, H } = load();
  ctx.answer = () => new Promise(() => {});
  H.load(true);
  const first = calls.length;
  ctx.answer = () => Promise.resolve(null);
  H.load(true);
  await new Promise(r => setTimeout(r, 20));
  assert.equal(calls.length, first);
});
