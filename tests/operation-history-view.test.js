"use strict";
const test = require("node:test"), assert = require("node:assert/strict"), fs = require("node:fs"), vm = require("node:vm");
function setup(items) {
  const fields = {};
  for (const key of ["#jobTray", "#jobList", "#jobSummary", "#jobClear", "#jobsDialogList", "#jobsDialogClear"])
    fields[key] = {innerHTML: "", classList: {add() {}, remove() {}, toggle() {}}, setAttribute() {}};
  const ctx = {console, Date, Math, STATE: {data: {operations: items}}, $: key => fields[key],
    esc: value => String(value || ""), jsq: value => JSON.stringify(value), icon: () => "",
    api: async () => ({ok: true}), setTimeout() {}, clearTimeout() {}};
  ctx.window = ctx; vm.createContext(ctx);
  vm.runInContext(fs.readFileSync("web/js/operations.js", "utf8"), ctx);
  return {ctx, fields};
}
function move(id, status, dismissible) {
  return {id, title: "Move Homestead data", kind: "self-data-handoff", status, dismissible};
}
test("settled moves offer Dismiss and bulk clear while recovery moves keep their controls", () => {
  const {ctx, fields} = setup([move("done", "succeeded", true), move("recovered", "cancelled", true), move("held", "failed", false)]);
  ctx.renderOperations();
  const html = fields["#jobsDialogList"].innerHTML;
  assert.ok(html.includes('dismissOperation("done")'));
  assert.ok(html.includes('dismissOperation("recovered")'));
  assert.ok(!html.includes('dismissOperation("held")'));
  assert.equal(fields["#jobClear"].textContent, "Clear 2 finished");
  assert.equal(fields["#jobsDialogClear"].textContent, "Clear 2 finished");
});

test("the open Jobs dialog and both clear buttons update after each dismissal, including the last", async () => {
  const {ctx, fields} = setup([move("done", "succeeded", true), move("recovered", "cancelled", true)]);
  ctx.renderOperations();
  await ctx.dismissOperation("done");
  assert.equal(fields["#jobsDialogClear"].textContent, "Clear 1 finished");
  assert.ok(!fields["#jobsDialogList"].innerHTML.includes('dismissOperation("done")'));
  await ctx.dismissOperation("recovered");
  assert.match(fields["#jobsDialogList"].innerHTML, /No jobs/);
  assert.equal(fields["#jobList"].innerHTML, "");
  assert.equal(fields["#jobClear"].hidden, true);
  assert.equal(fields["#jobsDialogClear"].hidden, true);
});
test("clearing the last dismissible job leaves the recovery job visible and hides bulk clear", async () => {
  const {ctx, fields} = setup([move("done", "succeeded", true), move("held", "failed", false)]);
  await ctx.dismissOperation("done");
  assert.match(fields["#jobsDialogList"].innerHTML, /failed/);
  assert.equal(fields["#jobsDialogClear"].hidden, true);
});
