"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const context = { window: {}, console, esc: x => String(x).replaceAll("<", "&lt;"),
  meter: pct => `<meter value="${pct}"></meter>`, tip: () => "?" };
vm.createContext(context);
context.jsArg = s => JSON.stringify(String(s ?? "")); context.jsq = s => (context.esc || String)(context.jsArg(s));
vm.runInContext(fs.readFileSync("web/js/views-storage.js", "utf8"), context);
const volume = { size_gb: 200, actual_gb: 292.06, used_pct: 146,
  filesystem: { used_gb: 180, capacity_gb: 196, used_pct: 91.8 } };

test("usage separates files, provisioned size and snapshot footprint without clamping", () => {
  const html = context.volumeUsageCell(volume);
  assert.match(html, /180 \/ 196 GiB files/);
  assert.match(html, /200 GiB provisioned/);
  assert.match(html, /292\.06 GiB Longhorn footprint/);
  assert.match(html, /value="91\.8"/);
  assert.doesNotMatch(html, /292\.06 \/ 200|value="146"/);
});
test("missing or invalid filesystem samples are unknown, never a footprint bar", () => {
  for (const filesystem of [null, {}, {used_gb: 4, capacity_gb: 0, used_pct: 0},
    {used_gb: 3, capacity_gb: 2, used_pct: 150}, {used_gb: 0, capacity_gb: 2, used_pct: -1}]) {
    const html = context.volumeUsageCell({...volume, filesystem});
    assert.match(html, /Filesystem usage unavailable/);
    assert.doesNotMatch(html, /<meter/);
    assert.match(html, /292\.06 GiB Longhorn footprint/);
  }
});
test("zero filesystem usage is shown and labels are escaped", () => {
  assert.match(context.volumeUsageCell({...volume, filesystem: {used_gb: 0, capacity_gb: 196, used_pct: 0}}), /0 \/ 196 GiB files/);
  assert.doesNotMatch(context.volumeUsageCell({...volume, actual_gb: "<script>"}), /<script>/);
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
