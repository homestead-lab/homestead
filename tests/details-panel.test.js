"use strict";
// The details sidebar (Nov 2026): a click on a row opens everything about it at
// the right - its main actions, then tabs - built from the page's own data.
const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");

function load() {
  const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const c = { console, esc, jsq: s => esc(JSON.stringify(String(s))), jsArg: s => JSON.stringify(String(s ?? "")), icon: n => `<i data-icon="${n}"></i>`,
    document: { getElementById: () => null, addEventListener() {}, querySelector: () => null }, STATE: { data: {} }, location: { search: "" },
    URLSearchParams, JSON, Date, Math, setTimeout: () => 0, clearTimeout() {}, setInterval: () => 0, clearInterval() {} };
  c.window = c;
  vm.createContext(c);
  vm.runInContext(fs.readFileSync("web/js/ui.js", "utf8"), c);
  vm.runInContext(fs.readFileSync("web/js/details-panel.js", "utf8"), c);
  return c;
}
const APPS = { "lab/paperless": { name: "paperless", host: "harvester-node3", down: true } };
function register(c) {
  c.DETAIL_KINDS.app = {
    head: key => APPS[key] && { title: APPS[key].name, sub: `lab · ${APPS[key].host}`, iconHtml: '<span class="av">PA</span>' },
    actions: key => [{ label: "Open logs", run: "openLogs()" }, { label: "Restart app", run: "" }, { label: "Open app", run: "" }, { label: "Delete app…", run: "", danger: true }],
    tabs: [
      { id: "overview", label: "Overview", html: key => c.UI.facts([["Host", APPS[key].host]]) },
      { id: "logs", label: "Logs", html: () => "<pre>log</pre>" },
      { id: "problem", label: "Why it is down", when: key => APPS[key].down, html: () => "down" },
    ],
  };
}

test("an item opens on its first tab: its name, its main actions, then the tabs", () => {
  const c = load(); register(c);
  const html = c.detailsHtml("app", "lab/paperless");
  assert.match(html, /<h3 id="detailsTitle">paperless<\/h3><div class="dim xs">lab · harvester-node3<\/div>/);
  assert.match(html, /aria-label="Close" onclick="closeDetails\(\)"/);
  assert.match(html, /<div class="details-actions"><div class="actionbar">/);
  assert.match(html, /Open logs[\s\S]*Restart app[\s\S]*Open app[\s\S]*<details class="actionmenu">[\s\S]*Delete app…/, "the first three are buttons, the rest in ⋯");
  assert.match(html, /<button type="button" class="on" aria-current="true" onclick="detailsTab\(&quot;overview&quot;\)">Overview<\/button>/);
  assert.match(html, /<div class="details-tab" data-tab="overview"><dl class="ui-facts">/);
});

test("another tab shows its own content, and a tab that does not apply is not offered", () => {
  const c = load(); register(c);
  assert.match(c.detailsHtml("app", "lab/paperless", "logs"), /data-tab="logs"><pre>log<\/pre>/);
  APPS["lab/paperless"].down = false;
  assert.doesNotMatch(c.detailsHtml("app", "lab/paperless"), /Why it is down/);
  APPS["lab/paperless"].down = true;
  assert.match(c.detailsHtml("app", "lab/paperless"), /Why it is down/);
});

test("an unknown kind, or an item that is gone, has nothing to show", () => {
  const c = load(); register(c);
  assert.equal(c.detailsHtml("nope", "x"), "");
  assert.equal(c.detailsHtml("app", "lab/gone"), "");
});

test("an item on a linked cluster marks the panel with it, so its actions go there", () => {
  const c = load(); register(c);
  const panel = { dataset: {}, innerHTML: "", querySelector: () => null, classList: { add() {}, remove() {} }, setAttribute() {} };
  c.document.getElementById = id => id === "detailsPanel" ? panel : id === "detailsScrim" ? { hidden: true } : null;
  c.document.body = { classList: { add() {}, remove() {} }, append() {} };
  const head = c.DETAIL_KINDS.app.head;
  c.DETAIL_KINDS.app.head = key => ({ ...head(key), cluster: "secondary-example" });
  c.openDetails("app", "lab/paperless");
  assert.equal(panel.dataset.cluster, "secondary-example");
  c.DETAIL_KINDS.app.head = head;
  c.detailsTab("logs");
  assert.equal(panel.dataset.cluster, undefined, "one on this cluster leaves it off");
});
