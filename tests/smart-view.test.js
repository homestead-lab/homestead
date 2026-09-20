"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");

const context = { window: {}, console };
vm.createContext(context);
vm.runInContext(fs.readFileSync("web/js/views-overview.js", "utf8"), context);

const tone = value => vm.runInContext(`smartTestTone(${JSON.stringify(value)})`, context);

test("SMART self-test results do not mistake 'without error' for a failure", () => {
  assert.equal(tone("Completed without error"), "ok");
  assert.equal(tone("Completed successfully"), "ok");
  assert.equal(tone("Completed: read failure"), "bad");
  assert.equal(tone("Self-test in progress"), "");
});
