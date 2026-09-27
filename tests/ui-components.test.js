"use strict";
// The dialog components: what they put on the page, and that plain text
// given to them is escaped.
const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");

const context = { window: {}, console, document: {},
  esc: value => String(value).replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c])),
  icon: name => `<svg data-icon="${name}"></svg>` };
vm.createContext(context);
vm.runInContext(fs.readFileSync("web/js/ui.js", "utf8"), context);
const UI = context.window.UI;

test("a callout escapes its title and keeps its tone", () => {
  const html = UI.callout("warn", "<b>tight</b>", "<ul><li>kept</li></ul>");
  assert.match(html, /ui-callout warn/);
  assert.match(html, /&lt;b&gt;tight&lt;\/b&gt;/);
  assert.match(html, /<ul><li>kept<\/li><\/ul>/, "the body is HTML the caller built");
  assert.match(UI.callout("nonsense", "x"), /ui-callout info/, "an unknown tone is information");
});

test("table cells carry their column's label for the phone layout", () => {
  const html = UI.table([{ label: "Host" }, { label: "Memory", className: "grow" }], [["node<1>", "81%"]]);
  assert.match(html, /<td class="" data-label="Host">node<1><\/td>/);
  assert.match(html, /data-label="Memory">81%/);
  assert.match(UI.table([{ label: "Host" }], [], { empty: "No hosts <yet>" }), /ui-empty">No hosts &lt;yet&gt;/);
});

test("steps mark what is done and what is current", () => {
  const html = UI.steps([{ title: "Copy" }, { title: "Verify" }, { title: "Switch" }], 1);
  assert.match(html, /<li class="done">/);
  assert.match(html, /<li class="current">/);
  assert.match(html, /<li class="todo">/);
  assert.match(UI.steps([{ title: "Read" }]), /ui-steps guide/, "no current step is a plain guide");
});

test("a meter turns amber past its warning mark and red past full", () => {
  assert.match(UI.meter({ now: 50, after: 60, warnAt: 85 }), /ui-meter ok/);
  assert.match(UI.meter({ now: 80, after: 91, warnAt: 88 }), /ui-meter warn/);
  assert.match(UI.meter({ now: 95, after: 104 }), /ui-meter bad/);
  assert.match(UI.meter({ now: 95, after: 104 }), /width:5%/, "the change is drawn only up to the edge");
});

test("progress without a value is shown as under way", () => {
  assert.match(UI.progress(null, { label: "Copying" }), /ui-progress info unknown/);
  assert.match(UI.progress(42.4, { label: "Copying" }), /<b>42%<\/b>/);
  assert.match(UI.progress(130), /width:100%/);
});

test("actions put their buttons in the bar, extra buttons at the start", () => {
  const html = UI.actions(UI.cancel() + UI.button("Delete <it>", "go()", { kind: "danger", id: "x" }), UI.button("Help", "help()"));
  assert.match(html, /class="ui-actions"/);
  assert.match(html, /ui-actions-start">.*Help/s);
  assert.match(html, /class="btn danger" id="x" onclick="go\(\)"/);
  assert.match(html, /Delete &lt;it&gt;/);
});

test("the acknowledgement is one checkbox with an escaped sentence", () => {
  const html = UI.ack("ok_box", "I accept <risk>");
  assert.match(html, /<input type="checkbox" id="ok_box">/);
  assert.match(html, /I accept &lt;risk&gt;/);
});
