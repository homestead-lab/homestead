"use strict";
/* Remove kept resources (operations.js): after a Create VM ended with an
   uncertain error, the review lists what goes and what stays, the typed VM
   name confirms, and the reviewed token is sent once. */
const test = require("node:test"), assert = require("node:assert/strict"), fs = require("node:fs"), vm = require("node:vm");

const ROWS = [
  {resource: {kind: "VirtualMachine", name: "web"}, relationship: "Created by this attempt", remove: true},
  {resource: {kind: "PersistentVolumeClaim", name: "web-disk"}, relationship: "Created by this attempt", remove: true},
  {resource: {kind: "PersistentVolumeClaim", name: "web-rootdisk"}, relationship: "Goes with the VM", remove: false, with_vm: true},
  {resource: {kind: "Secret", name: "web-cloudinit"}, relationship: "Used by VM media", remove: false},
];

function setup({blocked = false, missing = false, fail = false, answer = null} = {}) {
  const sent = [], jobs = [], toasts = [];
  const typed = {value: "", get dataset() { return {typedExpect: fields["#mbody"]._html.match(/id="vmCreateRemoveName"[^>]*data-typed-expect="([^"]*)"/)?.[1]}; }};
  const fields = {"#mtitle": {textContent: ""}, "#jobSummary": {}, ".modalbox": {scrollTop: 300}, "#vmCreateRemoveApply": {disabled: true}, "#vmCreateRemoveName": typed};
  fields["#mbody"] = {_html: "", get innerHTML() { return this._html; }, set innerHTML(html) { this._html = html; delete fields["#vmCreateRemoveLoading"]; },
    insertAdjacentHTML(_where, html) { this._html = html + this._html; }};
  const ctx = {console, Date, URL, STATE: {data: {}}, ME: null, $: key => fields[key], $$: () => [],
    esc: value => String(value).replaceAll("<", "&lt;"), icon: () => "", clearTimeout() {}, setTimeout() {},
    toast: (text, tone) => toasts.push([text, tone]), closeModal() {}, refreshOperations() {},
    modal: (title, html) => { ctx.title = title; fields["#mbody"].innerHTML = html; fields["#vmCreateRemoveLoading"] = {}; },
    api: async (path, options) => {
      sent.push({path, body: JSON.parse(options.body)});
      if (path.endsWith("/removal-preview")) return {capacity_token: missing ? null : "signed-removal", plan: {blocked, confirm: "web",
        blockers: blocked ? ["A VM named web exists, and nothing on it shows that this attempt created it."] : [],
        warnings: ["Only what this attempt provably created is removed."], resources: ROWS}};
      if (fail) throw new Error("lost response");
      return answer || {ok: true, removed: 2, detail: "admin removed what this attempt created.", operation: {id: "job", status: "failed"}};
    }};
  ctx.document = {getElementById: id => fields["#" + id]};
  ctx.window = ctx; vm.createContext(ctx);
  ctx.jsArg = s => JSON.stringify(String(s ?? "")); ctx.jsq = s => ctx.esc(ctx.jsArg(s));
  vm.runInContext(fs.readFileSync("web/js/ui.js", "utf8"), ctx);
  vm.runInContext(fs.readFileSync("web/js/operations.js", "utf8"), ctx);
  ctx.noteOperation = job => jobs.push(job);
  return {ctx, fields, sent, jobs, toasts};
}

test("the review says what goes and what stays, and removal waits for the typed name", async () => {
  const t = setup();
  await t.ctx.vmCreateRemoveReview("job");
  const html = t.fields["#mbody"].innerHTML;
  assert.equal(t.fields["#mtitle"].textContent, "Remove kept resources · web");
  assert.match(html, /VM · web<\/td><td[^>]*>Created by this attempt<\/td><td[^>]*>Removed</);
  assert.match(html, /Volume · web-rootdisk<\/td><td[^>]*>Goes with the VM<\/td><td[^>]*>Removed with the VM</);
  assert.match(html, /Secret · web-cloudinit<\/td><td[^>]*>Used by VM media<\/td><td[^>]*>Kept</);
  assert.match(html, />Cancel</);
  assert.match(html, /Only what this attempt provably created is removed/);
  await t.ctx.vmCreateRemoveApply();
  assert.equal(t.sent.length, 1, "nothing is sent before the name is typed");
  t.fields["#vmCreateRemoveName"].value = "wrong";
  await t.ctx.vmCreateRemoveApply();
  assert.equal(t.sent.length, 1);
  t.fields["#vmCreateRemoveName"].value = "web";
  await t.ctx.vmCreateRemoveApply();
  await t.ctx.vmCreateRemoveApply();
  assert.equal(t.sent.length, 2, "a review is sent once");
  assert.deepEqual(t.sent[1], {path: "/api/operations/vm-recovery/remove", body: {id: "job", capacity_token: "signed-removal", confirm: "web"}});
  assert.equal(t.jobs.length, 1);
});

test("a blocked or incomplete review never sends a removal", async () => {
  for (const options of [{blocked: true}, {missing: true}]) {
    const t = setup(options);
    await t.ctx.vmCreateRemoveReview("job");
    t.fields["#vmCreateRemoveName"].value = "web";
    await t.ctx.vmCreateRemoveApply();
    assert.equal(t.sent.length, 1, JSON.stringify(options));
    if (options.blocked) {
      assert.match(t.fields["#mbody"].innerHTML, /Nothing can be removed now/);
      assert.doesNotMatch(t.fields["#mbody"].innerHTML, /id="vmCreateRemoveApply"/);
    }
  }
});

test("a lost reply is not sent again and says to review again", async () => {
  const t = setup({fail: true});
  await t.ctx.vmCreateRemoveReview("job");
  t.fields["#vmCreateRemoveName"].value = "web";
  await t.ctx.vmCreateRemoveApply();
  await t.ctx.vmCreateRemoveApply();
  assert.equal(t.sent.length, 2);
  assert.equal(t.jobs.length, 0);
  assert.match(t.fields["#mbody"].innerHTML, /lost response\. Review again before removing anything\./);
  assert.equal(t.fields[".modalbox"].scrollTop, 0);
});

test("only a failed Create VM job that kept what it made offers the removal", () => {
  const t = setup();
  const card = operation => t.ctx.operationCard({resource: {}, ...operation});
  assert.match(card({id: "a", kind: "vm-create", status: "failed", mutation_recovery: true}), /vmCreateRemoveReview/);
  for (const operation of [{id: "b", kind: "vm-edit", status: "failed", mutation_recovery: true},
    {id: "c", kind: "vm-create", status: "failed", mutation_recovery: false}]) {
    assert.doesNotMatch(card(operation), /vmCreateRemoveReview/);
  }
});

test("the toast says how much went: all, part or nothing", async () => {
  for (const [answer, tone] of [[{ok: true, removed: 3}, "ok"], [{ok: false, removed: 1}, "warn"], [{ok: false, removed: 0}, "bad"]]) {
    const t = setup({answer: {...answer, detail: "what happened", operation: {id: "job"}}});
    await t.ctx.vmCreateRemoveReview("job");
    t.fields["#vmCreateRemoveName"].value = "web";
    await t.ctx.vmCreateRemoveApply();
    assert.deepEqual(t.toasts, [["what happened", tone]]);
  }
});
