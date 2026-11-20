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

/* A shared volume (a network share, an app's ReadWriteMany volume) is served
   by Longhorn's share manager from one host. Longhorn moves it once Kubernetes
   marks that host not ready, whatever the apps' failover choice, and without
   waiting for Kubernetes' five minutes. The words pin that fact, not a sentence. */
test("the failover choices that keep an app on its host say they do not keep its shared volumes there", () => {
  const { FAILOVER_HELP } = choice();
  for (const mode of ["wait", "slow"]) {
    assert.match(FAILOVER_HELP[mode], /shared volumes/i, mode);
    assert.match(FAILOVER_HELP[mode], /once the host is marked not ready/, mode);
  }
});

test("the network share tip and wiki name the real trigger and count from one moment", () => {
  const allocation = fs.readFileSync("web/js/views-storage-allocation.js", "utf8");
  const tip = allocation.split("\n").find(line => line.includes("Restart network shares quickly after a host failure"));
  assert.ok(tip, "the setting is still there");
  const wiki = fs.readFileSync("docs/wiki/Network-shares.md", "utf8");
  const failure = require("./helpers/between")(wiki, "### After a host failure", "### Partial SMB service").replace(/\s+/g, " ");
  for (const [where, text] of [["tip", tip], ["wiki", failure]]) {
    assert.doesNotMatch(text, /several minutes|Kubernetes stops waiting/, where);
    assert.match(text, /once Kubernetes marks the host as not ready/, where);
    // Every time given counts from the host going silent, not from a taint or a detection.
    const clocks = [...text.matchAll(/(?:seconds?|minutes?) (?:after|of) ([a-z' ]{0,40}?)(?=[.,])/g)].map(m => m[1]);
    for (const clock of clocks) assert.match(clock, /^(the|its) host stops answering$|^a failure$/, `${where}: "${clock}"`);
  }
});

/* A failover time counts from when Kubernetes marks the host as not ready
   (the taint), about a minute after the host stops answering; the help says
   so for every time, and the wiki's table offers what the dialog offers. */
test("every failover time says it counts from the host marked not ready", () => {
  const { FAILOVER_HELP } = choice();
  for (const mode of ["move", "slow", "default"]) {
    assert.match(FAILOVER_HELP[mode], /(seconds|minutes) after Kubernetes marks its host not ready/, mode);
    assert.doesNotMatch(FAILOVER_HELP[mode], /(seconds|minutes) after (its|the) host stops answering|about a minute after it stops/, mode);
  }
  // The minute is the time to be marked, not when the app moves. The
  // default's help is held to the Edit dialog's word budget.
  for (const mode of ["move", "slow"]) assert.match(FAILOVER_HELP[mode], /marks its host not ready, which takes about a minute/, mode);
});

test("the Containers wiki lists the choices the dialog offers, timed from the host marked not ready", () => {
  const vm = require("node:vm");
  const c = { window: {}, esc: String };
  vm.createContext(c);
  vm.runInContext(require("./helpers/between")(source, "/* What a container does when the node it runs on stops answering. */", "const FAILOVER_HELP") + "window.W = FAILOVER_WORDS;", c);
  const wiki = fs.readFileSync("docs/wiki/Containers.md", "utf8");
  const section = require("./helpers/between")(wiki, "## If a node fails", /\n## /);
  for (const name of Object.values(c.window.W)) assert.ok(section.includes(`| **${name}** |`), name);
  assert.match(section.replace(/\s+/g, " "), /count from when Kubernetes marks the host as not ready, which is about a minute after the host stops answering/);
});
