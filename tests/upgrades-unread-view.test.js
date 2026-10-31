"use strict";
// Harvester's upgrades or offered versions that could not be read (sweep
// unknown-as-fine, row 16): the card never says "up to date", and names what
// it could not read.
const test = require("node:test"), assert = require("node:assert/strict"), fs = require("node:fs"), vm = require("node:vm");

function card(report) {
  const ctx = { console, Map, Set, document: { addEventListener() {} }, STATE: { data: {} }, $: () => null, $$: () => [],
    esc: value => String(value ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c])),
    tip: t => t, icon: () => "", jsq: v => JSON.stringify(v), safeHref: v => v, fmtDay: v => v };
  ctx.window = ctx; vm.createContext(ctx); require("./helpers/load-ui")(ctx);
  vm.runInContext(fs.readFileSync("web/js/views-cluster.js", "utf8"), ctx);
  return ctx.upgradesCard({ current: "1.5.1", stable: null, test: null, error: "", recent: [], active: null, last: null,
                            offered: [], history: [], unread: "", ...report });
}

test("read, and nothing newer: up to date", () => {
  assert.match(card({}), /up to date/);
});

test("upgrades unread: not up to date, and the callout says an upgrade under way is not known", () => {
  const html = card({ history: null, unread: "Homestead could not read its upgrades (500) from Harvester" });
  assert.doesNotMatch(html, /up to date/);
  assert.match(html, /not read/);
  assert.match(html, /whether an upgrade is under way is not known/);
  assert.doesNotMatch(html, /what Harvester offers is not known/);
});

test("offered versions unread: the callout names those", () => {
  const html = card({ offered: null, unread: "Homestead could not read the versions it offers (500) from Harvester" });
  assert.doesNotMatch(html, /up to date/);
  assert.match(html, /what Harvester offers is not known/);
  assert.doesNotMatch(html, /whether an upgrade is under way is not known/);
});
