"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const context = { window: {}, console, esc: x => String(x).replaceAll("<", "&lt;"),
  meter: pct => `<meter value="${pct}"></meter>`, tip: () => "?", HOVER_CARDS: {}, STATE: { data: {} } };
vm.createContext(context);
context.jsArg = s => JSON.stringify(String(s ?? "")); context.jsq = s => (context.esc || String)(context.jsArg(s));
vm.runInContext(fs.readFileSync("web/js/ui.js", "utf8"), context);
vm.runInContext(require("./helpers/storage-source")(), context);
const volume = { name: "v1", pvc_name: "v1", size_gb: 200, actual_gb: 292.06, used_pct: 146,
  filesystem: { used_gb: 180, capacity_gb: 196, used_pct: 91.8 } };

test("usage shows the files against the file system, never the footprint against the size", () => {
  const html = context.volumeUsageCell(volume);
  assert.match(html, /180 \/ 196 GiB files</);
  assert.match(html, /value="91\.8"/);
  assert.doesNotMatch(html, /292\.06|value="146"/);
  assert.match(html, /data-card="volume-usage\|v1"/, "the rest is in its hover card");
});
test("missing or invalid filesystem samples are unknown, never a footprint bar", () => {
  for (const filesystem of [null, {}, {used_gb: 4, capacity_gb: 0, used_pct: 0},
    {used_gb: 3, capacity_gb: 2, used_pct: 150}, {used_gb: 0, capacity_gb: 2, used_pct: -1}]) {
    const html = context.volumeUsageCell({...volume, filesystem});
    assert.match(html, /Filesystem usage unavailable/);
    assert.doesNotMatch(html, /<meter/);
  }
});
test("the usage hover card separates files, provisioned size and snapshot footprint, escaped", () => {
  context.STATE.data.vols = [volume];
  const card = context.HOVER_CARDS["volume-usage"]("v1");
  assert.match(card, /180 of 196 GiB/);
  assert.match(card, /200 GiB provisioned/);
  assert.match(card, /292\.06 GiB, snapshots included/);
  context.STATE.data.vols = [{...volume, actual_gb: "<script>"}];
  assert.doesNotMatch(context.HOVER_CARDS["volume-usage"]("v1"), /<script>/);
  assert.match(context.volumeUsageCell({...volume, filesystem: {used_gb: 0, capacity_gb: 196, used_pct: 0}}), /0 \/ 196 GiB files</);
});
test("desktop volume-name cell remains a table cell, flex belongs to its child", () => {
  const css = fs.readFileSync("web/style.css", "utf8");
  assert.match(css, /\.voltable \.volname-content\{display:flex/);
  assert.doesNotMatch(css, /\.voltable \.volname\{display:flex/);
});

test("retained detached storage is unused despite cached historical importer names", () => {
  const v = {state: "detached", unclaimed: true, attached: ["prime-268211de"], attached_to: "prime-268211de", used_by: null};
  assert.equal(context.volumeUse(v).kind, "unclaimed");
  assert.match(context.volumeUseCell(v), /no claim/);
  assert.doesNotMatch(context.volumeUseCell(v), /prime-268211de/);
  assert.equal(context.volumeUse({...v, state: "attached"}).kind, "in-use", "physical attachment stays protected");
});
test("current workload references remain protected and stopped workloads keep their identity", () => {
  assert.equal(context.volumeUse({state: "attached", attached: []}).kind, "in-use");
  assert.equal(context.volumeUse({state: "detached", attached: ["importer-active"], unclaimed: false}).kind, "in-use");
  assert.equal(context.volumeUse({state: "detached", attached: [], unclaimed: false, used_by: ["Deployment/nextcloud"]}).kind, "stopped");
  assert.equal(context.volumeUse({state: "detached", attached: [], unclaimed: false, used_by: []}).kind, "orphaned");
  assert.equal(context.volumeUse({state: "detached", attached: [], unclaimed: false, used_by: null}).kind, "detached");
});
test("a claim whose users could not be read is 'could not check', never orphaned or spare", () => {
  const v = {state: "detached", attached: [], unclaimed: false, pvc_name: "nextcloud-data", used_by: null};
  assert.equal(context.volumeUse(v).kind, "unknown");
  assert.match(context.volumeUseCell(v), /could not check/);
  assert.doesNotMatch(context.volumeUseCell(v), /orphaned/);
  // A Longhorn volume no claim was ever made for is simply detached.
  assert.equal(context.volumeUse({...v, pvc_name: ""}).kind, "detached");
});
