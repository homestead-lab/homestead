"use strict";
const test = require("node:test"), assert = require("node:assert/strict"), fs = require("node:fs"), vm = require("node:vm");
function setup(items) {
  const fields = {};
  for (const key of ["#jobTray", "#jobList", "#jobSummary", "#jobClear"])
    fields[key] = {innerHTML: "", classList: {add() {}, remove() {}, toggle() {}}, setAttribute() {}};
  const ctx = {console, Date, Math, STATE: {data: {operations: items}}, $: key => fields[key],
    esc: value => String(value || ""), jsq: value => JSON.stringify(value), icon: () => "",
    api: async () => ({ok: true}), setTimeout() {}, clearTimeout() {}};
  ctx.window = ctx; vm.createContext(ctx);
  vm.runInContext(fs.readFileSync("web/js/ui.js","utf8"),ctx); vm.runInContext(fs.readFileSync("web/js/operations.js", "utf8"), ctx);
  return {ctx, fields};
}
function move(id, status, dismissible) {
  return {id, title: "Move Homestead data", kind: "self-data-handoff", status, dismissible};
}
test("settled moves offer Dismiss and bulk clear while recovery moves keep their controls", () => {
  const {ctx, fields} = setup([move("done", "succeeded", true), move("recovered", "cancelled", true), move("held", "failed", false)]);
  ctx.renderOperations();
  const html = fields["#jobList"].innerHTML;
  assert.ok(html.includes('dismissOperation("done")'));
  assert.ok(html.includes('dismissOperation("recovered")'));
  assert.ok(!html.includes('dismissOperation("held")'));
  assert.equal(fields["#jobClear"].textContent, "Clear 2 finished");
  assert.equal(fields["#jobClear"].textContent, "Clear 2 finished");
});

test("the legacy Jobs list and clear button update after each dismissal, including the last", async () => {
  const {ctx, fields} = setup([move("done", "succeeded", true), move("recovered", "cancelled", true)]);
  ctx.renderOperations();
  await ctx.dismissOperation("done");
  assert.equal(fields["#jobClear"].textContent, "Clear 1 finished");
  assert.ok(!fields["#jobList"].innerHTML.includes('dismissOperation("done")'));
  await ctx.dismissOperation("recovered");
  assert.equal(fields["#jobList"].innerHTML, "");
  assert.equal(fields["#jobList"].innerHTML, "");
  assert.equal(fields["#jobClear"].hidden, true);
  assert.equal(fields["#jobClear"].hidden, true);
});
test("clearing the last dismissible job leaves the recovery job visible and hides bulk clear", async () => {
  const {ctx, fields} = setup([move("done", "succeeded", true), move("held", "failed", false)]);
  await ctx.dismissOperation("done");
  assert.match(fields["#jobList"].innerHTML, /failed/);
  assert.equal(fields["#jobClear"].hidden, true);
});

test("clear completed retains failed and protected moves", async () => {
  const {ctx} = setup([move("done", "succeeded", true), move("recovered", "cancelled", true), move("held", "failed", false), move("other-failure", "failed", true)]);
  const dismissed=[]; ctx.api=async (_url, options)=>{dismissed.push(JSON.parse(options.body).id);return {ok:true};};
  await ctx.dismissCompletedOperations();
  assert.deepEqual(dismissed,["done","recovered"]);
  assert.equal(ctx.STATE.data.operations.map(item=>item.id).join(","),"held,other-failure");
});
