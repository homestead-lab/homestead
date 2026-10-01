"use strict";
const test = require("node:test"), assert = require("node:assert/strict"), fs = require("node:fs"), vm = require("node:vm");
function setup(operations = []) {
  const calls = [], fields = {"#jobSummary": {}};
  const ctx = {console, URL, Date, STATE: {data: {operations}}, window: null, location: {origin: "https://homestead.test"},
    $: key => fields[key], HomesteadRouter: {resolve: () => ({view: "vms"})},
    go: (...args) => calls.push(["navigate", ...args]), setTimeout: fn => fn(), clearTimeout() {}};
  ctx.window = ctx; vm.createContext(ctx);
  vm.runInContext(fs.readFileSync("web/js/operations.js", "utf8"), ctx);
  ctx.renderOperations = () => calls.push(["render"]);
  ctx.powerRecoveryReview = (...args) => calls.push(["vm-review", ...args]);
  ctx.storageRecoveryReview = (...args) => calls.push(["storage-review", ...args]);
  ctx.cancelOperation = (...args) => calls.push(["retention-review", ...args]);
  ctx.highlightInPage = (...args) => calls.push(["highlight", ...args]);
  return {ctx, calls};
}

test("Open on a failed k3s job reviews the batch instead of navigating to Machines", () => {
  const t = setup([{id: "batch", kind: "k3s-cluster", status: "failed", mutation_recovery: true}]);
  t.ctx.openOperation("/vms?find=k3s-demo", "batch");
  assert.deepEqual(t.calls, [["render"], ["vm-review", "batch", true]]);
});

test("a blocker supplies recovery flags even when absent from the tray or its cache is stale", () => {
  for (const cached of [[], [{id: "batch", kind: "k3s-cluster", mutation_recovery: false}]]) {
    const t = setup(cached);
    t.ctx.openOperation("/vms", "batch", {id: "batch", kind: "k3s-cluster", status: "failed", mutation_recovery: true});
    assert.deepEqual(t.calls, [["render"], ["vm-review", "batch", true]]);
  }
});

test("power, import, storage and legacy retention jobs open their own safe reviews", () => {
  for (const [job, expected] of [
    [{id: "job", kind: "vm-power", power_recovery: true}, ["vm-review", "job"]],
    [{id: "job", kind: "import-create", status: "failed", mutation_recovery: true}, ["vm-review", "job", "import"]],
    [{id: "job", kind: "reclass", status: "failed", storage_recovery: true}, ["storage-review", "job"]],
    [{id: "job", kind: "k3s-cluster", tracking_only: true, cleanable: true}, ["retention-review", "job"]],
  ]) {
    const t = setup([job]); t.ctx.openOperation("/vms", "job");
    assert.deepEqual(t.calls, [["render"], expected]);
  }
});

test("ordinary job links still navigate and highlight their resource", () => {
  const t = setup([{id: "job", kind: "k3s-cluster", status: "succeeded"}]);
  t.ctx.openOperation("/vms?find=batch", "job");
  assert.equal(t.calls[1][0], "navigate");
  assert.equal(t.calls[1][1], "vms");
  assert.deepEqual(t.calls[2], ["highlight", "batch"]);
});

test("an unrelated supplied record cannot replace the selected job", () => {
  const t = setup([{id: "selected", kind: "vm-power", power_recovery: true}]);
  t.ctx.openOperation("/vms", "selected", {id: "different", mutation_recovery: true});
  assert.deepEqual(t.calls, [["render"], ["vm-review", "selected"]]);
});


test("a running batch or import still opens its progress page instead of recovery", () => {
  for (const kind of ["k3s-cluster", "import-create"]) {
    const t = setup([{id: "job", kind, status: "running", mutation_recovery: true}]);
    t.ctx.openOperation("/vms", "job");
    assert.equal(t.calls[1][0], "navigate");
    assert.equal(t.calls.some(call => call[0] === "vm-review"), false);
  }
});
