"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");

const context = {};
vm.createContext(context);
vm.runInContext(fs.readFileSync("web/js/metrics-utils.js", "utf8"), context);

test("workload CPU usage is shown as percent of one core", () => {
  assert.equal(context.workloadCpuPercent(0.279), "27.9%");
  assert.equal(context.workloadCpuPercent(1.25), "125%");
  assert.equal(context.workloadCpuPercent(0), "0%");
  assert.equal(context.workloadCpuPercent(null), "0%");
});

test("several cores drop the decimal so the number stays card-width", () => {
  assert.equal(context.workloadCpuPercent(12.345), "1235%");
  assert.equal(context.workloadCpuPercent(0.9994), "99.9%");
  assert.equal(context.workloadCpuPercent(3.9), "390%");
});

test("workload memory switches to GB rather than growing digits", () => {
  assert.equal(context.workloadMemory(738), "738 MB");
  assert.equal(context.workloadMemory(1840), "1.8 GB");
  assert.equal(context.workloadMemory(20480), "20 GB");
  assert.equal(context.workloadMemory(123456), "121 GB");
  assert.equal(context.workloadMemory(null), "0 MB");
});

test("sizes move to TB past 10 TB instead of growing digits", () => {
  assert.equal(context.sizeText(986), "986 GB");
  assert.equal(context.sizeText(25.24), "25.2 GB");
  assert.equal(context.sizeText(12345.6), "12.1 TB");
  assert.equal(context.sizeText(null), "0 GB");
});

test("a used/capacity pair shares the unit the larger half needs", () => {
  assert.equal(context.sizePair(25.2, 46.8), "25.2/46.8 GB");
  assert.equal(context.sizePair(1843.2, 2048), "1843/2048 GB");
  assert.equal(context.sizePair(486, 1392), "486/1392 GB");
  assert.equal(context.sizePair(12345.6, 14000), "12.1/13.7 TB");
});

test("network rates move to Gb/s past 1000 Mb/s", () => {
  assert.deepEqual([...context.rateParts(36.14)], ["36.1", "Mb/s"]);
  assert.deepEqual([...context.rateParts(29629.6)], ["29.6", "Gb/s"]);
  assert.deepEqual([...context.ratePair(9876.5, 12345.7)], ["↓9.9 ↑12.3", "Gb/s"]);
  assert.deepEqual([...context.ratePair(1.2, 0.4)], ["↓1.2 ↑0.4", "Mb/s"]);
});
