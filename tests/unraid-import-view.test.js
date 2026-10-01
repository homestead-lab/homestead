"use strict";
const test = require("node:test"), assert = require("node:assert/strict");
const fs = require("node:fs"), vm = require("node:vm");

test("finished imports offer dismissal while active and recovery records keep their history", () => {
  const host = {}, actions = [], opened = [], dismissed = [];
  const ctx = { STATE: { data: { operations: [
    { id: "failed-copy", kind: "unraid-vm-import", title: "Import desktop", status: "failed", progress: 100, message: "Upload refused" },
    { id: "running-copy", kind: "unraid-vm-import", title: "Import server", status: "running", progress: 10 },
    { id: "protected-copy", kind: "unraid-vm-import", title: "Inspect partial copy", status: "failed", dismissible: false },
    { id: "finished-copy", kind: "unraid-vm-import", title: "Import completed", status: "succeeded" },
    { id: "cancelled-copy", kind: "unraid-vm-import", title: "Import cancelled", status: "cancelled" },
    { id: "other", kind: "deployment", status: "running" },
  ] } }, $: () => host, esc: String, jsq: JSON.stringify, operationTone: () => "",
    operationActive: op => !["succeeded", "failed", "cancelled"].includes(op.status),
    actionBar: rows => { actions.push(...rows); return ""; }, operationLog: id => opened.push(id),
    dismissOperation: id => dismissed.push(id) };
  ctx.window = ctx;
  vm.createContext(ctx);
  vm.runInContext(fs.readFileSync("web/js/views-vms.js", "utf8"), ctx);
  ctx.uvmCopiesPaint();
  assert.match(host.innerHTML, /Recent imports/);
  assert.match(host.innerHTML, /Upload refused/);
  assert.equal(actions.length, 8);
  assert.equal(actions[0].label, "Job details");
  vm.runInContext(actions[0].run, ctx);
  assert.deepEqual(opened, ["failed-copy"]);
  for (const action of actions.filter(row => row.label === "Dismiss")) {
    assert.equal(action.need, "operator");
    vm.runInContext(action.run, ctx);
  }
  assert.deepEqual(dismissed, ["failed-copy", "finished-copy", "cancelled-copy"]);
});

test("the shared dismissal refreshes the import list only after the server accepts it", async () => {
  const host = {}, requests = [], errors = [], summary = {};
  const ctx = { STATE: { data: { operations: [
    { id: "failed-copy", kind: "unraid-vm-import", title: "Import desktop", status: "failed" },
    { id: "running-copy", kind: "unraid-vm-import", title: "Import server", status: "running" },
  ] } }, $: selector => selector === "#jobSummary" ? summary : host, esc: String, jsq: JSON.stringify,
    actionBar: () => "", toast: message => errors.push(message),
    api: async (path, init) => { requests.push({ path, body: JSON.parse(init.body) }); } };
  ctx.window = ctx;
  vm.createContext(ctx);
  for (const file of ["operations", "views-vms"]) vm.runInContext(fs.readFileSync(`web/js/${file}.js`, "utf8"), ctx);
  vm.runInContext("renderOperations = () => uvmCopiesPaint()", ctx);
  await ctx.dismissOperation("failed-copy");
  assert.deepEqual(requests, [{ path: "/api/operations/dismiss", body: { id: "failed-copy" } }]);
  assert.equal(ctx.STATE.data.operations.length, 1);
  assert.equal(ctx.STATE.data.operations[0].id, "running-copy");
  assert.doesNotMatch(host.innerHTML, /Import desktop/);
  assert.match(host.innerHTML, /Import server/);
  ctx.api = async () => { throw new Error("An active operation cannot be dismissed"); };
  await ctx.dismissOperation("running-copy");
  assert.equal(ctx.STATE.data.operations[0].id, "running-copy");
  assert.deepEqual(errors, ["An active operation cannot be dismissed"]);
});
