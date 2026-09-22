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
