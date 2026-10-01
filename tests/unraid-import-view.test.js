"use strict";
const test = require("node:test"), assert = require("node:assert/strict");
const fs = require("node:fs"), vm = require("node:vm");

test("recent failed imports stay visible and Job details opens the shared log", () => {
  const host = {}, actions = [], opened = [];
  const ctx = { STATE: { data: { operations: [
    { id: "failed-copy", kind: "unraid-vm-import", title: "Import desktop", status: "failed", progress: 100, message: "Upload refused" },
    { id: "running-copy", kind: "unraid-vm-import", title: "Import server", status: "running", progress: 10 },
    { id: "other", kind: "deployment", status: "running" },
  ] } }, $: () => host, esc: String, jsq: JSON.stringify, operationTone: () => "",
    actionBar: rows => { actions.push(...rows); return ""; }, operationLog: id => opened.push(id) };
  ctx.window = ctx;
  vm.createContext(ctx);
  vm.runInContext(fs.readFileSync("web/js/views-vms.js", "utf8"), ctx);
  ctx.uvmCopiesPaint();
  assert.match(host.innerHTML, /Recent imports/);
  assert.match(host.innerHTML, /Upload refused/);
  assert.equal(actions.length, 2);
  assert.equal(actions[0].label, "Job details");
  vm.runInContext(actions[0].run, ctx);
  assert.deepEqual(opened, ["failed-copy"]);
});
