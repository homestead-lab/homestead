"use strict";
const test = require("node:test"), assert = require("node:assert/strict");
const fs = require("node:fs"), vm = require("node:vm");

function setup(review) {
  let html = "", fail = false;
  const sent = [], notices = [], fields = { "#editCapacityConfirm": { checked: false }, "#editGo": {} };
  const c = { window: {}, console, URLSearchParams, Map, document: { addEventListener() {} },
    $: s => fields[s], esc: s => String(s).replaceAll("<", "&lt;").replaceAll(">", "&gt;"),
    childModal: (_, body) => { html = body; }, modal: (_, body) => { html = body; }, toast: m => notices.push(m), closeModal() {}, refresh() {}, setTimeout() {},
    api: async (path, options) => {
      if (path === "/api/edit/preview") return review;
      if (path.startsWith("/api/node/power/plan")) return review;
      sent.push({ path, body: JSON.parse(options.body) });
      if (fail) throw new Error("Review expired");
      return { ok: true, name: "demo", operation: {id:'power-job'}, steps:['cordoned'] };
    } };
  vm.createContext(c);
  c.jsArg = s => JSON.stringify(String(s ?? "")); c.jsq = s => (c.esc || String)(c.jsArg(s));
  vm.runInContext(fs.readFileSync("web/js/ui.js", "utf8"), c);
  vm.runInContext(fs.readFileSync("web/js/views-workloads.js", "utf8"), c);
  vm.runInContext(fs.readFileSync("web/js/views-lifecycle.js", "utf8"), c);
  return { c, fields, sent, notices, html: () => html, fail: () => { fail = true; } };
}
const review = { capacity_token: "token", capacity: { blocked: false, requires_confirmation: true, candidates: [],
  warnings: ["unknown <memory>"], additional: 1 } };

test("Edit requires acknowledgement and submits only the frozen reviewed edit", async () => {
  const t = setup(review), body = { ns: "lab", name: "demo", containers: [{ name: "one", memory: "1Gi" }], node: "host1" };
  await t.c.window.editReview(body);
  assert.match(t.html(), /unknown &lt;memory&gt;/);
  await t.c.window.confirmEdit();
  assert.equal(t.sent.length, 0);
  body.containers[0].memory = "20Gi";
  t.fields["#editCapacityConfirm"].checked = true;
  await t.c.window.confirmEdit();
  assert.equal(t.sent.length, 1);
  assert.equal(t.sent[0].path, "/api/edit");
  assert.equal(t.sent[0].body.containers[0].memory, "1Gi");
  assert.equal(t.sent[0].body.node, "host1");
  assert.equal(t.sent[0].body.capacity_token, "token");
});

test("Edit fails closed for missing preview and hard blockers", async () => {
  for (const response of [{}, { ...review, capacity: { ...review.capacity, blocked: true } }]) {
    const t = setup(response);
    await t.c.window.editReview({ ns: "lab", name: "demo" });
    t.fields["#editCapacityConfirm"].checked = true;
    await t.c.window.confirmEdit();
    assert.equal(t.sent.length, 0);
  }
});

test("A refused edit requires another preview, not a replay", async () => {
  const t = setup(review);
  await t.c.window.editReview({ ns: "lab", name: "demo" });
  t.fail();
  t.fields["#editCapacityConfirm"].checked = true;
  await t.c.window.confirmEdit();
  await t.c.window.confirmEdit();
  assert.equal(t.sent.length, 1);
  assert.equal(t.fields["#editGo"].textContent, "Review again");
});

test("Rename review explains a name-only outage and requires a separate acknowledgement", async () => {
  const t = setup({ ...review, capacity: { ...review.capacity, rename: { from: "<old>", to: "new" } } });
  await t.c.window.editReview({ ns: "lab", name: "old", workload_name: "new" });
  assert.match(t.html(), /&lt;old&gt;/);
  assert.match(t.html(), /Only the workload name changes/);
  assert.match(t.html(), /Save other edits separately/);
  assert.match(t.html(), /short outage/);
  assert.match(t.html(), /Rename workload/);
  await t.c.window.confirmEdit();
  assert.equal(t.sent.length, 0);
  t.fields["#editCapacityConfirm"].checked = true;
  await t.c.window.confirmEdit();
  assert.deepEqual(t.sent[0].body, { ns: "lab", name: "old", workload_name: "new", capacity_token: "token", confirm_capacity: true });
});

test("Host review names budgets and local data with escaped content", async () => {
  const t = setup({ node:'host1',action:'reboot',review_token:'review',ready: false, blockers: ["Budget prevents eviction"], pods: 1, vms: [], workloads: [], volumes: [],
    maintenance: { budgets: [{ pod: "lab/<app>", budget: "lab/pdb", allowed: 0 }],
      local_storage: [{ pod: "lab/app", kind: "host-local path", source: "/<files>" }] } });
  await t.c.window.nodePowerReview("host1", "reboot");
  assert.match(t.html(), /Disruption budgets/);
  assert.match(t.html(), /0 disruption\(s\) allowed/);
  assert.match(t.html(), /Drain deletes emptyDir data/);
  assert.match(t.html(), /&lt;files&gt;/);
  assert.doesNotMatch(t.html(), /id="pw_execute"/);
});

const hostReview = {node:'host1',action:'reboot',review_token:'review',ready:true,pods:1,vms:[],workloads:[],volumes:[]};
function hostFields(t) {
  t.fields['#pw_confirm']={value:'host1'}; t.fields['#pw_execute']={};
}
test('host power consumes approval once and records the job',async()=>{
  const t=setup(hostReview), jobs=[];let refreshed=0;hostFields(t);
  t.c.window.noteOperation=o=>jobs.push(o);t.c.window.refreshOperations=()=>refreshed++;
  await t.c.window.nodePowerReview('host1','reboot');
  await Promise.all([t.c.window.nodePower('host1','reboot'),t.c.window.nodePower('host1','reboot')]);
  assert.equal(t.sent.length,1);assert.equal(jobs[0].id,'power-job');assert.equal(refreshed,1);
  assert.match(t.html(),/stays cordoned/);
});
test('lost power response cannot offer a blind retry',async()=>{
  const t=setup(hostReview);hostFields(t);t.fail();
  await t.c.window.nodePowerReview('host1','reboot');
  await t.c.window.nodePower('host1','reboot');
  await t.c.window.nodePower('host1','reboot');
  assert.equal(t.sent.length,1);
  assert.equal(t.fields['#pw_execute'].textContent,'Review host again');
  assert.match(t.notices.join(' '),/inspect Recent jobs/);
  await t.fields['#pw_execute'].onclick();
  assert.equal(t.sent.length,1);
});
test('blocked or incomplete host reviews cannot send power',async()=>{
  for (const response of [{...hostReview,ready:false},{...hostReview,review_token:''},{}]) {
    const t=setup(response);hostFields(t);
    await t.c.window.nodePowerReview('host1','reboot');
    await t.c.window.nodePower('host1','reboot');
    assert.equal(t.sent.length,0);
  }
});
test('late host preview cannot replace the most recent review',async()=>{
  const t=setup(hostReview);let resolveFirst;let calls=0;
  t.c.api=()=>++calls===1?new Promise(resolve=>{resolveFirst=resolve;}):Promise.resolve({...hostReview,node:'host2'});
  const old=t.c.window.nodePowerReview('host1','reboot');
  await t.c.window.nodePowerReview('host2','reboot');
  resolveFirst(hostReview);await old;
  assert.equal(t.c.window.__nodePowerPlan.node,'host2');
});

test("Storage move has one plain-language review with escaped paths and both placement phases", async () => {
  const t = setup({ ...review, capacity: { ...review.capacity, copy_helper: review.capacity } });
  await t.c.window.editReview({ ns: "lab", name: "demo", containers: [{ name: "<main>", volumes: [
    { path: "/data", source: "new", sub_path: "<folder>", copy_from: { claim: "old" } }
  ] }] });
  assert.match(t.html(), /&lt;main&gt;/);
  assert.match(t.html(), /data-label="From">old/);
  assert.match(t.html(), /new\/&lt;folder&gt;/);
  assert.match(t.html(), /Existing destination files may be overwritten/);
  assert.match(t.html(), /no automatic rollback or retry/);
  assert.match(t.html(), /Temporary copy helper/);
  assert.match(t.html(), /Start data move/);
  await t.c.window.confirmEdit();
  assert.equal(t.sent.length, 0);
  t.fields["#editCapacityConfirm"].checked = true;
  await t.c.window.confirmEdit();
  assert.equal(t.sent.length, 1);
});
