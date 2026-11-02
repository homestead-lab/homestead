"use strict";
const test = require("node:test"), assert = require("node:assert/strict");
const fs = require("node:fs");
const between = require("./helpers/between");

const source = fs.readFileSync("web/js/vm-console.js", "utf8");

test("closing a console asks nothing: keys typed into the VM are not edits", () => {
  assert.match(source, /window\.__modalGuard = \(\) => "";/);
  assert.match(require("./helpers/workloads-source")(), /window\.__modalGuard = \(\) => "";\n  if \(pods\.length\) consolePodChanged\(\);/);
});

test("the screen says why it is black when the picture is on a passed-through GPU", () => {
  const note = new Function("STATE", "UI", between(source, "function vmConsoleDisplayNote", "window.vmConsoleDisplayNote") + "return vmConsoleDisplayNote;")(
    { data: { vms: [{ ns: "vms", name: "gpu", display: "gpu" }, { ns: "vms", name: "plain", display: "virtual" }, { ns: "vms", name: "headless", display: "none" }] } },
    { callout: (tone, title) => `${tone}:${title}` });
  assert.equal(note("vms", "gpu"), "warn:No virtual display: the picture is on the passed-through GPU");
  assert.equal(note("vms", "headless"), "warn:No virtual display");
  assert.equal(note("vms", "plain"), "");
  assert.equal(note("vms", "unknown"), "");
});
