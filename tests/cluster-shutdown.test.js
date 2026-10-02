'use strict';
const test = require('node:test'), assert = require('node:assert/strict');
const fs = require('node:fs'), vm = require('node:vm');

function setup(warnings = []) {
  let html = '', lastTimer, lost = false;
  const sent = [], fields = {'#shutdownConfirm': {value:''}, '#shutdownStart': {}, '#shutdownLive': {innerHTML:''}};
  const state = {run:'run', phase:'draining', progress:35, deadline:Date.now()/1000+1800, message:'Waiting for pods',
    plan:{own:['lab','homestead','uid'], own_node:'a', nodes:[{name:'a'}, {name:'b'}]}};
  const c = {console, Date, window:null, $:s=>fields[s], esc:s=>String(s??'').replaceAll('<','&lt;').replaceAll('>','&gt;'),
    modal:(_, body)=>{html=body;}, toast(){}, setTimeout:f=>{lastTimer=f;return 1;}, clearTimeout(){},
    jsArg:s=>JSON.stringify(s), api:async(path,opts)=>{
      if (opts?.method==='POST') { sent.push({path, body:JSON.parse(opts.body)}); if(lost) throw Error('Connection lost'); return {state}; }
      if (path.endsWith('/plan')) return {ready:true, review_token:'review', confirm:'SHUT DOWN CLUSTER', nodes:state.plan.nodes, pods:5, volumes:2, homestead_node:'a', warnings};
      if (lost) throw Error('Connection lost');
      return {state:null};
    }};
  c.window = c;
  vm.createContext(c);
  vm.runInContext(fs.readFileSync('web/js/ui.js','utf8'),c);
  vm.runInContext(fs.readFileSync('web/js/cluster-shutdown.js','utf8'),c);
  return {c, state, sent, fields, html:()=>html, lose:()=>{lost=true;}, poll:()=>lastTimer()};
}

test('cluster shutdown requires exact confirmation and consumes approval before sending', async()=>{
  const t=setup(); await t.c.clusterShutdown();
  assert.match(t.html(), /all <b>2<\/b> hosts/);
  t.fields['#shutdownConfirm'].value='shutdown'; await t.c.clusterShutdownStart();
  assert.equal(t.sent.length,0);
  t.fields['#shutdownConfirm'].value='SHUT DOWN CLUSTER';
  await Promise.all([t.c.clusterShutdownStart(),t.c.clusterShutdownStart()]);
  assert.equal(t.sent.length,1);
  assert.equal(t.sent[0].body.review_token,'review');
});

test('lost submission never restores a reusable approval', async()=>{
  const t=setup(); await t.c.clusterShutdown(); t.lose();
  t.fields['#shutdownConfirm'].value='SHUT DOWN CLUSTER';
  await t.c.clusterShutdownStart(); await t.c.clusterShutdownStart();
  assert.equal(t.sent.length,1);
  assert.match(t.html(),/Check saved shutdown/);
});

test('connection loss retains the last progress and explicitly leaves power unverified',async()=>{
  const t=setup(); t.c.clusterShutdownProgress(t.state); t.lose(); await t.poll();
  assert.match(t.fields['#shutdownLive'].innerHTML,/Connection lost · power state unverified/);
  assert.match(t.fields['#shutdownLive'].innerHTML,/35%/);
  assert.doesNotMatch(t.fields['#shutdownLive'].innerHTML,/100%|Shutdown complete/);
});

test('handoff has no cancel action and reopening progress escapes cluster strings',()=>{
  const t=setup(); t.state.phase='handoff'; t.state.message='<script>bad</script>';
  t.c.clusterShutdownProgress(t.state);
  assert.doesNotMatch(t.html(),/Cancel shutdown/);
  assert.match(t.fields['#shutdownLive'].innerHTML,/&lt;script&gt;/);
  assert.doesNotMatch(t.fields['#shutdownLive'].innerHTML,/<script>/);
});

test('shutdown review explains observer removal and escapes helper names', async () => {
  const t = setup(['lab/<watch>: temporary image-pull progress watcher will be evicted; only its report is lost']);
  await t.c.clusterShutdown();
  assert.match(t.html(), /Temporary helpers/);
  assert.match(t.html(), /only its report is lost/);
  assert.match(t.html(), /&lt;watch&gt;/);
  assert.doesNotMatch(t.html(), /<watch>/);
  assert.equal(t.sent.length, 0);
});
