"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");

const context = { window: {} };
vm.createContext(context);
context.jsArg = s => JSON.stringify(String(s ?? "")); context.jsq = s => (context.esc || String)(context.jsArg(s));
vm.runInContext(fs.readFileSync("web/js/appstore-utils.js", "utf8"), context);
const label = value => context.window.appCategoryLabel(value);

test("catalogue category labels tolerate strings, arrays, and objects", () => {
  assert.equal(label("MediaServer Tools"), "MediaServer");
  assert.equal(label(["Network", "Security"]), "Network");
  assert.equal(label([{ name: "HomeAutomation" }]), "HomeAutomation");
  assert.equal(label({ Category: "Backup/Sync" }), "Backup");
  assert.equal(label(null), "");
});
