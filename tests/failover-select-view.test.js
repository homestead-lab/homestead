"use strict";

/* The workload editor's "If its node fails" choice sits in its own grid, sized
   to its longest option rather than the narrow column the Copies input uses,
   and the explanation beside it follows the choice. */
const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");

const source = require("./helpers/lifecycle-source")();
const form = fs.readFileSync("web/js/app-form.js", "utf8");   // Placement, shared with Deploy (#844)
const css = fs.readFileSync("web/style.css", "utf8");

test("the failover choice is not squeezed into the Copies column", () => {
  assert.match(form, /<div class="place-grid failover"><div class="f">\$\{failoverSelect\(id\("failover"\)/);
  assert.match(css, /\.place-grid\.failover\{grid-template-columns:max-content 1fr/);
  assert.match(css, /\.place-grid\.failover select\{width:auto;min-width:max-content\}/);
});

test("the failover grid stacks on a phone like the others", () => {
  assert.match(css, /@media\(max-width:700px\)\{[^}]*\.place-grid\.failover[^}]*\{grid-template-columns:1fr\}/);
});

test("the explanation follows the chosen failover", () => {
  assert.match(form, /onchange="\$\(\$\{jsq\("#" \+ id\("failover_help"\)\)\}\)\.textContent = FAILOVER_HELP\[this\.value\]/);
  assert.match(form, /id="\$\{id\("failover_help"\)\}"/);
  assert.match(source, /window\.FAILOVER_HELP = FAILOVER_HELP;/);
});

/* The choice as an app has it (#1308): a time Homestead does not offer is
   shown as it is, and kept until another choice is saved. */
function choice() {
  const vm = require("node:vm");
  const between = require("./helpers/between");
  const part = between(source, "/* What a container does when the node it runs on stops answering. */", "window.FAILOVER_HELP = FAILOVER_HELP;");
  const c = { esc: s => String(s).replaceAll("&", "&amp;").replaceAll("<", "&lt;").replaceAll('"', "&quot;"), window: {} };
  vm.createContext(c);
  vm.runInContext(part + "window.failoverSelect = failoverSelect; window.FAILOVER_HELP = FAILOVER_HELP;", c);
  return c.window;
}

test("a time of its own is shown with its seconds and is the one selected", () => {
  const { failoverSelect, FAILOVER_HELP } = choice();
  const html = failoverSelect("e_failover", "custom", "", "If its host fails", 120);
  assert.match(html, /<option value="custom" selected>Moves after 2 minutes<\/option>/);
  assert.match(failoverSelect("e_failover", "custom", "", "If its host fails", 45), />Moves after 45 seconds</);
  assert.doesNotMatch(failoverSelect("e_failover", "move"), /value="custom"/, "only when it has one");
  assert.match(failoverSelect("e_failover", "custom", "", "If its host fails", null), /<option value="custom" selected>Set outside Homestead<\/option>/,
    "a time that never runs out is still kept, not shown as the first choice");
  assert.ok(FAILOVER_HELP.custom, "the explanation says it is kept");
});

test("every place that shows the choice passes the app's own time", () => {
  const actions = fs.readFileSync("web/js/views-workloads-actions.js", "utf8");
  assert.match(actions, /failoverSelect\(`fo_\$\{w\.ns\}_\$\{w\.name\}`, w\.failover \|\| "default", [^\n]*, w\.failover_seconds\)/);
  assert.match(form, /failoverSelect\(id\("failover"\), w\.failover \|\| "default",[\s\S]{0,200}?, "If its host fails", w\.failover_seconds\)/);
});

test("moving after 90 seconds is offered beside 15 seconds and the default (#1308)", () => {
  const { failoverSelect, FAILOVER_HELP } = choice();
  assert.match(failoverSelect("e_failover", "slow"), /<option value="slow" selected>Move after 90 seconds<\/option>/);
  assert.match(FAILOVER_HELP.slow, /90 seconds/);
  assert.match(failoverSelect("e_failover", "move"), /<option value="move" selected>Move after 15 seconds<\/option>/, "the two times in one form");
});

test("the demo has an app with a time of its own, so the dialog audit shows that choice", () => {
  const demo = fs.readFileSync("web/js/demo.js", "utf8");
  assert.match(demo, /failover: "custom", failover_seconds: \d+/);
});
