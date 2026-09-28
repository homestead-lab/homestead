"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const context = { window: {}, console, document: {addEventListener() {}}, esc: x => String(x).replaceAll("<", "&lt;"), icon: () => "<svg></svg>" };
vm.createContext(context);
context.jsArg = s => JSON.stringify(String(s ?? "")); context.jsq = s => (context.esc || String)(context.jsArg(s));
vm.runInContext(fs.readFileSync("web/js/views-protect.js", "utf8"), context);

test("cleanup shows live volume-wide percentage and never invents completion", () => {
  const html = context.snapshotCleanupHtml({known: true, active: true, percent: 51, errors: []});
  assert.match(html, /cleanup 51%/);
  assert.match(html, /aria-valuenow="51"/);
  assert.match(html, /volume-wide/);
  assert.match(context.snapshotCleanupHtml({error: "<offline>"}), /&lt;offline>/);
  assert.doesNotMatch(context.snapshotCleanupHtml({known: false, active: false}), /100%|complete/);
  assert.match(context.snapshotCleanupHtml({known: true, active: true, percent: null}), /in progress/);
});

test("timeline distinguishes system and user points and protects live head", () => {
  const html = context.snapshotTimeline([
    {name: "manual", source: "user", ready: true, created: "2026-09-26T12:00:00Z", size_mb: 10},
    {name: "expand", source: "system", ready: true, created: "2026-09-25T12:00:00Z", size_mb: 20, children: ["volume-head"]}
  ], "vol", "Media");
  assert.ok(html.indexOf("expand") < html.indexOf("manual"));
  assert.match(html, />System</);
  assert.match(html, />User</);
  assert.match(html, /parent of Volume Head/);
  assert.match(html, /<time datetime=/);
  assert.equal((html.match(/lhRevert\(/g) || []).length, 1);
  assert.match(html, /Live data · now · never deleted/);
  assert.doesNotMatch(html, /lhSnapDel\('volume-head'/);
});
test("unknown timestamps and sources do not pretend to be scheduled", () => {
  const html = context.snapshotTimeline([{name: "<unsafe>", ready: false, created: "bad"}], "vol", "Media");
  assert.match(html, /Snapshot time unavailable/);
  assert.match(html, /Unknown source/);
  assert.doesNotMatch(html, /Scheduled|<unsafe>|Invalid Date/);
  assert.match(html, /disabled/);
});
test("removed recovery points cannot be rolled back from the timeline", () => {
  const html = context.snapshotTimeline([{name: "old", source: "user", ready: true, removed: true, deleting: true}], "vol", "Media");
  assert.match(html, /cleanup pending/);
  assert.match(html, /Track cleanup/);
  assert.doesNotMatch(html, /lhRevert\(/);
});
