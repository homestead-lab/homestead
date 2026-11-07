"use strict";
// UI.dialog: every dialog is one of five shapes, built from one spec, so it
// says what it acts on, says it once, and closes the same way (design.md).
const test = require("node:test"), assert = require("node:assert/strict");
const fs = require("node:fs"), vm = require("node:vm");
const seen = require("./helpers/seen");

const context = { window: {}, console, document: {},
  esc: value => String(value).replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c])),
  icon: name => `<svg data-icon="${name}"></svg>` };
vm.createContext(context);
context.jsArg = s => JSON.stringify(String(s ?? "")); context.jsq = s => context.esc(context.jsArg(s));
vm.runInContext(fs.readFileSync("web/js/ui.js", "utf8"), context);
const UI = context.window.UI;

const update = extra => UI.dialog({ shape: "review",
  subject: { kind: "App", name: "immich-stack", where: "lab", change: ["latest · cf6f0d6", "latest · 9b74872"] },
  lead: "The app restarts to use the new image.",
  body: '<ul class="upd-apps"><li>immich-stack</li></ul>',
  details: "<p>Exact images</p>",
  ack: { id: "approve", sentence: "Restart it now", onchange: "ready()" },
  actions: { primary: { label: "Update", onclick: "apply()", id: "apply", disabled: true } }, ...extra });

test("a review says what it acts on first, then the sentence, then the button named for the result", () => {
  const html = update();
  // The object goes in the header, under the title (syncDialogHead copies it up).
  assert.match(html, /^<div class="ui-dialog ui-dialog-review ui-stack"><div class="ui-dialog-head"><span class="ui-head-name">immich-stack<\/span><span class="ui-head-where">App · lab<\/span>/);
  assert.match(seen(html), /^immich-stack App · lab latest · cf6f0d6 → latest · 9b74872 The app restarts/);
  assert.match(html, /<details class="ui-more" data-disclosure="Details">/);
  assert.match(html, /id="approve"[\s\S]*Restart it now/);
  assert.match(html, /ui-actions-start"><button[^>]*data-dialog-dismiss="true"[^>]*>Cancel<\/button>/, "Cancel on the left");
  assert.match(html, /ui-actions-end"><button[^>]*class="btn pri" id="apply"[^>]*disabled[^>]*>Update<\/button>/);
});

test("its warnings are one summary bar that counts them and opens to each in full", () => {
  const html = update({ issues: [["warn", "Recreate stops all old pods before replacements start; all containers in this workload will be unavailable during the restart"], ["warn", "Projected RAM is high"]] });
  assert.equal((html.match(/class="ui-callout /g) || []).length, 1);
  assert.match(html, /ui-callout warn[\s\S]*<summary><span class="ui-notice-line"><b class="ui-notice-head">2 things to check<\/b><span class="ui-notice-first">Recreate stops all old pods before replacements start\.<\/span>/);
  assert.match(html, /<li class="ui-issue"><span class="ui-issue-short">Recreate stops all old pods before replacements start\.<\/span><span class="ui-issue-more"> All containers/);
});

test("a blocker blocks: no tick and nothing to press but Cancel", () => {
  const html = update({ issues: [["bad", "No host can take it"], ["warn", "Projected RAM is high"]] });
  assert.match(html, /ui-callout bad[\s\S]*<b class="ui-notice-head">Can&#39;t go ahead<\/b>/);
  assert.doesNotMatch(html, /id="approve"|id="apply"/);
  assert.match(html, />Cancel<\/button>/);
});

test("each shape closes the way it should", () => {
  assert.match(UI.dialog({ shape: "info", body: "<p>x</p>" }), />Close<\/button>/);
  assert.match(UI.dialog({ shape: "watch", progress: { value: 45, label: "Pulling" } }), />Close<\/button>/);
  const confirm = UI.dialog({ shape: "confirm", lead: "Delete frigate?", actions: { primary: { label: "Delete app", onclick: "del()", danger: true } } });
  assert.match(confirm, /class="btn danger"[^>]*>Delete app/);
  assert.match(confirm, />Cancel<\/button>/);
  assert.doesNotMatch(UI.dialog({ shape: "form", body: "<input id=a>", ack: { id: "x", sentence: "Sure" } }), /id="x"/, "a form asks no tick");
});

test("steps are a track: done, current with its measure, still to do", () => {
  const html = UI.dialog({ shape: "watch", steps: { list: [{ title: "Back up" }, { title: "Copy", detailHtml: "Restoring the disk" }, { title: "Start" }], current: 1 },
    progress: { value: 62, label: "124 of 200 GiB" } });
  assert.match(html, /<li class="done">[\s\S]*Back up[\s\S]*<li class="current" aria-current="step"><i style="--p:62%">[\s\S]*Copy[\s\S]*<li class="todo">[\s\S]*Start/);
  assert.match(seen(html), /Step 2 of 3 Copy 62% Restoring the disk · 124 of 200 GiB/);
  assert.doesNotMatch(html, /ui-progress/, "the measure fills the step, not a second bar");
});

test("plain text in a spec is escaped", () => {
  const html = UI.dialog({ shape: "info", subject: { kind: "<App>", name: "<b>x</b>", where: "a&b", chips: [["<hot>", "warn"]] } });
  assert.doesNotMatch(html, /<b>x<\/b>|<App>|<hot>/);
  assert.match(html, /&lt;b&gt;x&lt;\/b&gt;/);
});
