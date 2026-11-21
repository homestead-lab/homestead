"use strict";
// Hover cards (Nov 2026): a hover, focus or tap on a status, a figure or a name
// shows a card built only then, from the page's builder for its kind.
const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");

function load() {
  const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const listeners = {};
  const c = { console, esc, jsq: s => esc(JSON.stringify(String(s))), jsArg: s => JSON.stringify(String(s ?? "")), icon: n => `<i data-icon="${n}"></i>`,
    document: { getElementById: () => null, addEventListener: (t, f) => { listeners[t] = f; }, querySelector: () => null },
    STATE: { data: {} }, location: { search: "" }, URLSearchParams, JSON, Date, Math, setTimeout: () => 0, clearTimeout() {}, setInterval: () => 0, clearInterval() {}, Element: class {} };
  c.window = c; c.addEventListener = () => {}; c.$ = () => null; c.$$ = () => [];
  vm.createContext(c);
  vm.runInContext(fs.readFileSync("web/js/ui.js", "utf8"), c);
  vm.runInContext(fs.readFileSync("web/js/core-tooltips-sort.js", "utf8"), c);
  return c;
}
// An element as the card code reads it: closest() for [data-card], and its dataset.
const element = (c, card) => {
  const el = Object.assign(Object.create(c.Element.prototype), { dataset: card ? { card } : {} });
  el.closest = sel => sel === "[data-card]" ? (card ? el : null) : null;
  return el;
};

test("a card has a title and its chip, facts with their icons, a chart, and one link", () => {
  const { UI } = load();
  const html = UI.card({ title: "paperless is down", chipHtml: UI.chip("Down", "bad", { size: "slim" }),
    rows: [["Web check", "Port 8000, no answer for 6 minutes"], ["Last answer", "09:35"], ["Container", "Running"]],
    chartHtml: UI.strip(["up", "up", "down"], "Last 30 days"), link: { label: "Open logs", onclick: "openLogs()" } });
  assert.match(html, /^<div class="ui-card-head"><b>paperless is down<\/b><span class="ui-chip bad slim">Down<\/span><\/div>/);
  assert.equal((html.match(/<dt><svg class="ui-fact-icon"/g) || []).length, 3, "every fact has its icon");
  assert.match(html, /<i class="uptime-ok"><\/i><i class="uptime-ok"><\/i><i class="uptime-bad"><\/i>/);
  assert.match(html, /<button type="button" class="linkish" onclick="openLogs\(\)">Open logs ›<\/button>/);
});

test("a small chart marks the last value, and draws a projection and a limit when given", () => {
  const { UI } = load();
  const svg = UI.spark([1, 2, 3, 4], { project: 6, limit: 5, label: "Growth" });
  assert.match(svg, /^<svg class="ui-spark"[^>]*aria-label="Growth">/);
  assert.match(svg, /<line class="limit"/);
  assert.match(svg, /<path class="project"/);
  assert.match(svg, /<circle cx="[\d.]+" cy="[\d.]+" r="3"\/>/);
  assert.equal(UI.spark([4]), "", "one value is no trend");
});

test("a card is built only by the page's builder for its kind, with its key", () => {
  const c = load();
  const seen = [];
  c.HOVER_CARDS.app = (key, owner) => { seen.push(key); return `<b>${key}</b>`; };
  const owner = element(c, "app|lab/paperless");
  assert.equal(c.cardTarget(owner), owner);
  assert.equal(c.cardFor(owner), "<b>lab/paperless</b>");
  assert.deepEqual(seen, ["lab/paperless"]);
  const inside = Object.assign(Object.create(c.Element.prototype), { closest: sel => owner.closest(sel) });
  assert.equal(c.cardTarget(inside), owner, "a hover over something inside the element finds it");
});

test("with no builder for its kind, or a builder that fails, there is no card and the plain tooltip stands", () => {
  const c = load();
  assert.equal(c.cardTarget(element(c, "unknown|x")), null);
  c.HOVER_CARDS.broken = () => { throw new Error("no data"); };
  assert.equal(c.cardFor(element(c, "broken|x")), "");
  assert.equal(c.cardTarget(element(c, null)), null);
});
