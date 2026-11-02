"use strict";

/* On a phone a stacked table sorts from a menu sortBar() puts beside it. In
   Settings a card is a grid item the section filter hides by its data-tab, so
   the menu must not become a grid item of its own, shown in every section. */
const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const between = require("./helpers/between");

const core = require("./helpers/core-source")();
const sortBarSource = between(core, "function sortBar(", "\nfunction saveSort(");

/* Just the parts of the DOM sortBar() touches. */
function el(tag, classes = [], parent = null, dataset = {}) {
  const node = { tagName: tag.toUpperCase(), classes: new Set(classes), dataset, children: [], parentElement: null, innerHTML: "",
    classList: { contains: c => node.classes.has(c) },
    set className(value) { node.classes = new Set(value.split(/\s+/).filter(Boolean)); },
    get previousElementSibling() { const s = node.parentElement?.children; return s ? s[s.indexOf(node) - 1] || null : null; },
    closest(selector) {
      for (let n = node; n; n = n.parentElement) {
        if (selector.startsWith(".") && n.classes.has(selector.slice(1))) return n;
        if (selector === "[data-collection]" && "collection" in n.dataset) return n;
      }
      return null;
    },
    before(other) { const s = node.parentElement.children; other.parentElement = node.parentElement; s.splice(s.indexOf(node), 0, other); } };
  if (parent) { node.parentElement = parent; parent.children.push(node); }
  return node;
}
function sortBar() {
  const document = { createElement: tag => el(tag), querySelector: () => null };
  return new Function("document", "esc", "CSS", `${sortBarSource}; return sortBar;`)(document, String, { escape: String });
}
const heads = [{ classList: { contains: () => true }, textContent: "Namespace" }];

test("a Settings table's sort menu stays inside its card, so the section filter hides it with the card", () => {
  const grid = el("div", ["settings-grid"], null, { tab: "homestead" });
  el("section", ["card"], grid, { tab: "about" });
  const card = el("section", ["card"], grid, { tab: "namespaces" });
  const wrap = el("div", ["tblwrap"], card);
  const table = el("table", ["tbl", "stack"], wrap, { sort: "namespaces" });
  sortBar()(table, heads, null);
  for (const child of grid.children) assert.ok(child.dataset.tab, `every grid item belongs to a section: ${[...child.classes].join(" ")}`);
  assert.equal(wrap.previousElementSibling?.classes.has("sortbar"), true, "the menu sits just above the table");
  sortBar()(table, heads, null);
  assert.equal(card.children.filter(n => n.classes.has("sortbar")).length, 1, "a repaint reuses the menu");
});

test("a table's sort menu outside Settings still goes just before its card", () => {
  const page = el("div", ["views"]);
  const card = el("div", ["card"], page);
  const table = el("table", ["tbl", "stack"], el("div", ["tblwrap"], card), { sort: "volumes" });
  sortBar()(table, heads, null);
  assert.equal(card.previousElementSibling?.classes.has("sortbar"), true);
});
