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
