"use strict";
const test = require("node:test"), assert = require("node:assert/strict");
const fs = require("node:fs"), vm = require("node:vm");

function setup(review) {
  let html = "", fail = false;
  const sent = [], notices = [], fields = { "#editCapacityConfirm": { checked: false }, "#editGo": {},
    "#mbody": { insertAdjacentHTML(_where,body) { html += body; } } };
  const c = { window: {}, console, URLSearchParams, Map, document: { addEventListener() {}, getElementById: id => fields['#' + id] || null },
    $: s => fields[s], esc: s => String(s).replaceAll("<", "&lt;").replaceAll(">", "&gt;"),
    childModal: (_, body) => { html = body; }, modal: (_, body) => { html = body; }, toast: m => notices.push(m), closeModal() {}, refresh() {}, setTimeout() {},
    api: async (path, options) => {
      if (path === "/api/edit/preview") return review;
      if (path.startsWith("/api/node/power/plan")) return review;
      sent.push({ path, body: JSON.parse(options.body) });
      if (fail) throw new Error("Review expired");
      return { ok: true, name: "demo", operation: {id:'power-job'}, steps:['cordoned'] };
    } };
  vm.createContext(c); require("./helpers/core-text")(c);
  c.jsArg = s => JSON.stringify(String(s ?? "")); c.jsq = s => (c.esc || String)(c.jsArg(s));
  vm.runInContext(fs.readFileSync("web/js/ui.js", "utf8"), c);
  vm.runInContext(fs.readFileSync("web/js/gitops.js", "utf8"), c);
  vm.runInContext(require("./helpers/workloads-source")(), c);
  vm.runInContext(require("./helpers/lifecycle-source")(), c);
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
  assert.match(t.html(), /Reload saved app/);
  assert.match(t.html(), /does not need a new name/);
  await t.c.window.editReview({ns:"lab",name:"demo",containers:[{new:true,name:"helper"}]});
  await t.c.window.editSave("lab","demo");
  assert.equal(t.sent.length,1);
  assert.match(t.notices.at(-1),/Reload the saved app/);
});

test("Rename review explains a name-only outage and requires a separate acknowledgement", async () => {
  const t = setup({ ...review, capacity: { ...review.capacity, rename: { from: "<old>", to: "new" } } });
  await t.c.window.editReview({ ns: "lab", name: "old", workload_name: "new" });
  assert.match(t.html(), /&lt;old&gt;/);
  assert.match(t.html(), /Only the app name changes/);
  assert.match(t.html(), /Save other edits separately/);
  assert.match(t.html(), /short outage/);
  assert.match(t.html(), /Rename app/);
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
  assert.match(t.html(), /0 disruptions allowed/);
  assert.match(t.html(), /Drain deletes emptyDir data/);
  assert.match(t.html(), /&lt;files&gt;/);
  assert.doesNotMatch(t.html(), /id="pw_execute"/);
});

const hostReview = {node:'host1',action:'reboot',review_token:'review',ready:true,pods:1,vms:[],workloads:[],volumes:[]};
test('Longhorn protection explains the bounded drain wait and allows reviewed maintenance', async () => {
  const t = setup({...hostReview, warnings: ['Wait up to 2 minutes; power will not be sent if Longhorn keeps it protected'],
    maintenance: {budgets: [{pod: 'longhorn-system/<manager>', budget: 'longhorn-system/manager', allowed: 0, wait_for_drain: true}]}});
  await t.c.window.nodePowerReview('host1', 'reboot');
  assert.match(t.html(), /0 disruptions allowed · waits for Longhorn during drain/);
  assert.match(t.html(), /Wait up to 2 minutes/);
  assert.match(t.html(), /power will not be sent/);
  assert.match(t.html(), /&lt;manager&gt;/);
  assert.match(t.html(), /id="pw_execute"/);
});
test('forced review never promises a Longhorn drain wait', async () => {
  const t = setup({...hostReview, force:true,
    maintenance: {budgets: [{pod:'longhorn-system/manager',budget:'longhorn-system/manager',allowed:0,wait_for_drain:true}]}});
  await t.c.window.nodePowerReview('host1', 'reboot', true);
  assert.match(t.html(), /no cordon or drain/);
  assert.doesNotMatch(t.html(), /waits for Longhorn during drain/);
});
function hostFields(t) {
  t.fields['#pw_confirm']={value:'host1',dataset:{typedExpect:'host1'}}; t.fields['#pw_execute']={};
}
test('host power consumes approval once and records the job',async()=>{
  const t=setup(hostReview), jobs=[];let refreshed=0;hostFields(t);
  t.c.window.noteOperation=o=>jobs.push(o);t.c.window.refreshOperations=()=>refreshed++;
  await t.c.window.nodePowerReview('host1','reboot');
  await Promise.all([t.c.window.nodePower('host1','reboot'),t.c.window.nodePower('host1','reboot')]);
  assert.equal(t.sent.length,1);assert.equal(jobs[0].id,'power-job');assert.equal(refreshed,1);
  assert.match(t.html(),/Scheduling is allowed again when it is back/);
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
  assert.match(t.html(),/Inspect both app names/);
  assert.match(t.html(),/Inspect apps/);
  assert.doesNotMatch(t.html(),/Reload saved app/);
  assert.equal(t.fields['#editGo'].disabled,true);
});


test('single-host shutdown and reboot need outage acknowledgement without force', async () => {
  for (const action of ['reboot', 'poweroff']) {
    const t=setup({...hostReview,action,planned_outage:true,stranded:[{ns:'lab',name:'homestead'}],
      maintenance:{budgets:[{pod:'longhorn-system/manager',budget:'manager',allowed:0,wait_for_drain:false}]}});
    hostFields(t);
    t.fields['#pw_outage']={checked:false};
    await t.c.window.nodePowerReview('host1',action);
    assert.match(t.html(), /the whole cluster goes down/);
    assert.match(t.html(), /without evicting pods/);
    assert.match(t.html(), /id="pw_outage"/);
    assert.doesNotMatch(t.html(), /Override these checks|id="pw_allow"|waits for Longhorn during drain|host stays cordoned/);
    if(action==='poweroff') assert.match(t.html(), /powered on by hand/);
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
  assert.match(t.html(), /nodePowerReview\("host1",'reboot'\)[^]*Reboot…/);
  assert.match(t.html(), /nodePowerReview\("host1",'poweroff'\)[^]*Shut down…/);
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

test('a stopped host power job marks the step it stopped at, and says why once', () => {
  const t = setup(review);
  const html = t.c.nodePowerProgressMarkup({ status: 'failed', progress: 10, message: 'Longhorn still prevents eviction',
    power: { phase: 'failed', failed_phase: 'draining', action: 'reboot', direct: false } });
  const states = [...html.matchAll(/<li class="(\w+)">/g)].map(m => m[1]);
  assert.deepEqual(states, ['ok', 'bad', 'todo', 'todo', 'todo'], 'the drain stopped, not the cordon');
  assert.equal(html.split('Longhorn still prevents eviction').length - 1, 1, 'the reason appears once');
});

test('single-host disconnect pauses the display without inventing power progress', () => {
  const t = setup(review);
  const op = {id:'power-1',status:'running',progress:8,message:'Waiting for apps; power has not been sent',
    power:{phase:'holding',action:'reboot',direct:true,holds:true,planned_outage:true}};
  const html = t.c.nodePowerProgressMarkup(op, true);
  assert.match(html, /Waiting for Homestead to reconnect/);
  assert.match(html, /Last confirmed progress/);
  assert.match(html, /before the restart was requested/);
  assert.doesNotMatch(html, /<li class="run">|8%|In progress|host helper was created/);
  assert.equal(op.power.phase, 'holding');
  const helper = t.c.nodePowerProgressMarkup({...op,power:{...op.power,handoff:true,phase:'observing'}},true);
  assert.match(helper, /host helper was created/);
  assert.doesNotMatch(helper, /<li class="run">|8%/);
  const prolonged = t.c.nodePowerProgressMarkup(op,true,16*60*1000);
  assert.match(prolonged, /Still waiting for Homestead|disconnected for 16 min/);
  assert.match(prolonged, /Check the host console or network/);
});

test('shutdown and unexpected loss have distinct offline guidance', () => {
  const t = setup(review);
  const op = {status:'running',power:{action:'poweroff',phase:'observing',planned_outage:true}};
  assert.match(t.c.nodePowerProgressMarkup(op,true), /turn the host on manually/);
  const unknown = t.c.nodePowerProgressMarkup({status:'running',power:{}},true);
  assert.match(unknown, /current state cannot be confirmed/);
  assert.doesNotMatch(unknown, /Last confirmed progress|only host|turn the host on/);
});

test('power follow reconnects to the same job and displays its confirmed result', async () => {
  for (const status of ['succeeded','failed','missing']) {
    const t=setup(review), seen=[], paths=[];
    t.fields['#pwProgress']={set innerHTML(html){seen.push(html);}};
    let reads=0;
    const initial={id:'power-1',status:'running',progress:8,message:'Waiting for apps',
      power:{phase:'holding',planned_outage:true,action:'reboot',holds:true,direct:true}};
    t.c.api=async (path,options)=>{
      paths.push(path); assert.equal(options.method,undefined);
      if(++reads===1) throw new Error('offline');
      return status==='missing'?[]:[{...initial,status,progress:100,message:status==='succeeded'?'Host returned':'Power was not sent'}];
    };
    t.c.setTimeout=resolve=>resolve();
    await t.c.window.nodePowerFollow('power-1','',initial);
    assert.match(seen[0],/Waiting for Homestead to reconnect/);
    assert.match(seen.at(-1),status==='succeeded'?/Finished/:status==='failed'?/Stopped/:/Job not found/);
    assert.doesNotMatch(seen.at(-1),/Waiting for Homestead to reconnect/);
    assert.deepEqual(paths,['/api/operations','/api/operations']);
  }
});

test('power follow renders loss before its first status and stops on access errors', async () => {
  const t=setup(review), seen=[];
  t.fields['#pwProgress']={set innerHTML(html){seen.push(html);}};
  let reads=0;
  t.c.api=async ()=>{throw Object.assign(new Error('unavailable'), ++reads===1?{}:{status:403});};
  t.c.setTimeout=resolve=>resolve();
  await t.c.window.nodePowerFollow('power-1');
  assert.match(seen[0],/current state cannot be confirmed/);
  assert.doesNotMatch(seen[0],/Last confirmed progress|Starting/);
  assert.match(seen[1],/Sign in with access to Jobs/);
  assert.equal(reads,2);
});

test('each app and VM may move or wait for the host, and the choice is sent', async () => {
  const hold = [
    {id:'Deployment/lab/plex',kind:'Deployment',ns:'lab',name:'plex',here:1,hosts:['host2'],options:['move','wait'],default:'move',why:''},
    {id:'VirtualMachine/vms/gpu',kind:'VirtualMachine',ns:'vms',name:'gpu',here:1,hosts:[],options:['wait'],default:'wait',why:'it cannot live-migrate'},
    {id:'Deployment/lab/<web>',kind:'Deployment',ns:'lab',name:'<web>',here:1,hosts:['host2'],options:['move'],default:'move',why:''}];
  const t=setup({...hostReview,hold,stranded:[]});hostFields(t);
  await t.c.window.nodePowerReview('host1','reboot');
  assert.match(t.html(), /Apps and VMs · 3/);
  assert.match(t.html(), /id="pw_hold_0"[^]*Move to host2[^]*Stop and wait for this host/);
  assert.match(t.html(), /stops and waits[^]*it cannot live-migrate/);
  assert.match(t.html(), /&lt;web&gt;/);
  assert.doesNotMatch(t.html(), /id="pw_allow"/);
  t.fields['#pw_hold_0']={value:'wait'};
  await t.c.window.nodePower('host1','reboot');
  assert.deepEqual(t.sent[0].body.choices, {'Deployment/lab/plex':'wait'});
});

test('on the only host everything stops first and starts again', async () => {
  const t=setup({...hostReview,action:'poweroff',planned_outage:true,stranded:[{ns:'lab',name:'plex'}],
    hold:[{id:'Deployment/lab/plex',kind:'Deployment',ns:'lab',name:'plex',here:1,hosts:[],options:['wait'],default:'wait',why:'the only host'}]});
  await t.c.window.nodePowerReview('host1','poweroff');
  assert.match(t.html(), /stops the apps and VMs below cleanly/);
  assert.match(t.html(), /Nothing can move off the only host/);
  assert.doesNotMatch(t.html(), /<select id="pw_hold_0"/);
});

test('a forced review stops and moves nothing', async () => {
  const t=setup({...hostReview,force:true,hold:[{id:'Deployment/lab/plex',kind:'Deployment',ns:'lab',name:'plex',here:1,hosts:['host2'],options:['move','wait'],default:'move',why:''}]});
  hostFields(t);
  await t.c.window.nodePowerReview('host1','reboot',true);
  assert.doesNotMatch(t.html(), /pw_hold_0/);
  await t.c.window.nodePower('host1','reboot');
  assert.deepEqual(t.sent[0].body.choices, {});
});
