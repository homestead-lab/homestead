"use strict";
const test = require("node:test"), assert = require("node:assert/strict");
const fs = require("node:fs"), vm = require("node:vm");

function setup(review) {
  let html = "", fail = false;
  const sent = [], notices = [], fields = { "#editCapacityConfirm": { checked: false }, "#editGo": {},
    "#mbody": { insertAdjacentHTML(_where,body) { html += body; } } };
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

test("An uncertain edit requires fresh saved state instead of replaying additions", async () => {
  const t = setup(review);
  let scrolled = false;
  t.fields["#editSaveNotice"] = { scrollIntoView() { scrolled = true; } };
  await t.c.window.editReview({ ns: "lab", name: "demo" });
  t.fail();
  t.fields["#editCapacityConfirm"].checked = true;
  await t.c.window.confirmEdit();
  await t.c.window.confirmEdit();
  assert.equal(t.sent.length, 1);
  assert.equal(scrolled, true);
  assert.equal(t.fields["#editGo"].textContent, "Inspect before saving again");
  assert.equal(t.fields["#editGo"].disabled, true);
  assert.equal(t.fields["#editGo"].onclick, null);
  assert.match(t.html(), /Some changes may already be saved/);
  assert.match(t.html(), /Reload saved workload/);
  assert.match(t.html(), /does not need a new name/);
  await t.c.window.editReview({ns:"lab",name:"demo",containers:[{new:true,name:"helper"}]});
  await t.c.window.editSave("lab","demo");
  assert.equal(t.sent.length,1);
  assert.match(t.notices.at(-1),/Reload the saved workload/);
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
test('Longhorn protection explains the bounded drain wait and allows reviewed maintenance', async () => {
  const t = setup({...hostReview, warnings: ['Wait up to 2 minutes; power will not be sent if Longhorn keeps it protected'],
    maintenance: {budgets: [{pod: 'longhorn-system/<manager>', budget: 'longhorn-system/manager', allowed: 0, wait_for_drain: true}]}});
  await t.c.window.nodePowerReview('host1', 'reboot');
  assert.match(t.html(), /0 disruption\(s\) allowed · waits for Longhorn during drain/);
  assert.match(t.html(), /Wait up to 2 minutes/);
  assert.match(t.html(), /power will not be sent/);
  assert.match(t.html(), /&lt;manager&gt;/);
  assert.match(t.html(), /id="pw_execute"/);
});
test('forced review never promises a Longhorn drain wait', async () => {
  const t = setup({...hostReview, force:true,
    maintenance: {budgets: [{pod:'longhorn-system/manager',budget:'longhorn-system/manager',allowed:0,wait_for_drain:true}]}});
  await t.c.window.nodePowerReview('host1', 'reboot', true);
  assert.match(t.html(), /does not cordon or drain/);
  assert.doesNotMatch(t.html(), /waits for Longhorn during drain/);
});
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


test("uncertain rename directs inspection of both names and never offers stale retry",async()=>{
  const t=setup({...review,capacity:{...review.capacity,rename:{from:'old',to:'new'}}});
  await t.c.window.editReview({ns:'lab',name:'old',workload_name:'new'});
  t.fail();t.fields['#editCapacityConfirm'].checked=true;
  await t.c.window.confirmEdit();
  assert.match(t.html(),/Inspect both workload names/);
  assert.match(t.html(),/Inspect workloads/);
  assert.doesNotMatch(t.html(),/Reload saved workload/);
  assert.equal(t.fields['#editGo'].disabled,true);
});


test('single-host shutdown and reboot need outage acknowledgement without force', async () => {
  for (const action of ['reboot', 'poweroff']) {
    const t=setup({...hostReview,action,planned_outage:true,stranded:[{ns:'lab',name:'homestead'}],
      maintenance:{budgets:[{pod:'longhorn-system/manager',budget:'manager',allowed:0,wait_for_drain:false}]}});
    hostFields(t);
    t.fields['#pw_outage']={checked:false};
    await t.c.window.nodePowerReview('host1',action);
    assert.match(t.html(), /planned whole-cluster outage/);
    assert.match(t.html(), /does not evict pods/);
    assert.match(t.html(), /id="pw_outage"/);
    assert.doesNotMatch(t.html(), /Override these checks|id="pw_allow"|waits for Longhorn during drain|host stays cordoned/);
    if(action==='poweroff') assert.match(t.html(), /console or physical access to power this host on again/);
    await t.c.window.nodePower('host1',action);
    assert.equal(t.sent.length,0);
    t.fields['#pw_outage'].checked=true;
    await t.c.window.nodePower('host1',action);
    assert.equal(t.sent.length,1);
    assert.equal(t.sent[0].body.force,false);
    assert.equal(t.sent[0].body.allow_cluster_outage,true);
    assert.equal(t.sent[0].body.allow_stranded,true);
    assert.match(t.html(), /Scheduling was left unchanged/);
    assert.doesNotMatch(t.html(), /host stays cordoned/);
  }
});

test('single-host outage still requires separate storage-risk acknowledgement', async () => {
  const t=setup({...hostReview,planned_outage:true,requires_data_ack:true});
  hostFields(t); t.fields['#pw_outage']={checked:true};t.fields['#pw_data']={checked:false};
  await t.c.window.nodePowerReview('host1','reboot');
  await t.c.window.nodePower('host1','reboot');
  assert.equal(t.sent.length,0);
  t.fields['#pw_data'].checked=true;
  await t.c.window.nodePower('host1','reboot');
  assert.equal(t.sent.length,1);
});

test('host actions exposes normal reviews even when quorum cannot lose a member', async () => {
  const t=setup(hostReview);
  t.c.api=async path=>path==='/api/quorum'?{members:['host1'],can_lose:0,total:1,ready:['host1'],quorum_needs:1}:
    {workloads:[],stranded:[]};
  await t.c.window.nodeActions('host1');
  assert.match(t.html(), /Review reboot/);
  assert.match(t.html(), /Review shutdown/);
  assert.match(t.html(), /planned whole-cluster outage/);
  assert.doesNotMatch(t.html(), /Override - reboot or shut down anyway|Review forced/);
});

test('a host power job shows its steps, the current one, and how it ended', () => {
  const t = setup(review);
  const markup = (phase, status = 'running', direct = false) =>
    t.c.nodePowerProgressMarkup({ status, progress: 12, message: 'Evicting pods: 3 left', power: { phase, action: 'poweroff', direct } });
  const states = html => [...html.matchAll(/<li class="(\w+)">/g)].map(m => m[1]);
  assert.deepEqual(states(markup('draining')), ['ok', 'run', 'todo', 'todo', 'todo']);
  assert.match(markup('draining'), /Evicting pods: 3 left/);
  assert.deepEqual(states(markup('observing', 'succeeded')), ['ok', 'ok', 'ok', 'ok', 'ok']);
  assert.deepEqual(states(markup('draining', 'failed')), ['ok', 'bad', 'todo', 'todo', 'todo']);
  assert.match(markup('draining', 'failed'), /Check the host before making another request/);
  // An outage or forced job skips cordon and drain.
  assert.deepEqual(states(markup('sending', 'running', true)), ['ok', 'run', 'todo']);
  assert.match(markup('draining', 'running', false, true), /Stop new work on the host/);
});
