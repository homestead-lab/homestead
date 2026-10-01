"use strict";
const test = require('node:test'), assert = require('node:assert/strict'), vm = require('node:vm'), fs = require('node:fs');
function setup(status='running') {
  let frame={status,progress:20,history:[{t:'2026-01-01T12:00:00Z',s:status,p:20,m:'Copying'}],sources:[{
    title:'Disk 1 · test-disk · copy',pod:'copy-pod',kind:'disk-copy',text:'20.0% · ETA 1m 20s',
    progress:{percent:20,bytes:200000000,total_bytes:1000000000,bytes_per_second:10000000,eta_seconds:80}}]};
  const fields={'#jobSummary':{},'#modal':{classList:{contains:()=>false}},'.modalbox':{scrollTop:40}};
  let steps, pres=[], tick;
  fields['#oplogBody']={_html:'',get innerHTML(){return this._html},set innerHTML(html){this._html=html;
    steps={scrollHeight:800,scrollTop:0};pres=frame.sources.map(s=>({dataset:{source:JSON.stringify([s.pod||'',s.title])},scrollHeight:1000,scrollTop:0}));
    fields['.modalbox'].scrollTop=0;}};
  const ctx={console,Date,Math,Number,Map,STATE:{data:{operations:[]}},encodeURIComponent,
    esc:s=>String(s??'').replaceAll('<','&lt;').replaceAll('>','&gt;').replaceAll('"','&quot;'),
    $:s=>s==='#oplogBody .oplog-steps'?steps:fields[s], $$:()=>pres,
    modal:()=>{fields['#oplogFollow']={checked:true};fields['#oplogState']={}},
    setInterval:fn=>{tick=fn;return 1},clearInterval:()=>{},api:async()=>frame};ctx.window=ctx;
  vm.createContext(ctx);vm.runInContext(fs.readFileSync('web/js/operations.js','utf8'),ctx);ctx.renderOperations=()=>{};
  return {ctx,fields,steps:()=>steps,pres:()=>pres,tick:()=>tick,frame:value=>{frame=value},getFrame:()=>frame};
}
test('Follow latest moves both steps and source output to the bottom after every repaint',async()=>{
  const t=setup();await t.ctx.operationLog('import');
  assert.equal(t.steps().scrollTop,800);assert.equal(t.pres()[0].scrollTop,1000);
  t.steps().scrollTop=0;await t.tick()();assert.equal(t.steps().scrollTop,800);
  assert.match(t.fields['#oplogBody'].innerHTML,/20\.0%.*ETA 1m 20s/s);
  assert.match(t.fields['#oplogBody'].innerHTML,/oplog-copy/);
});
test('Pausing follow preserves steps, output identity and outer dialog position',async()=>{
  const t=setup();await t.ctx.operationLog('import');t.fields['#oplogFollow'].checked=false;
  t.steps().scrollTop=52;t.pres()[0].scrollTop=75;t.fields['.modalbox'].scrollTop=90;
  t.frame({...t.getFrame(),sources:[{title:'New events',text:'event'},...t.getFrame().sources]});await t.tick()();
  assert.equal(t.steps().scrollTop,52);assert.equal(t.pres()[0].scrollTop,0);assert.equal(t.pres()[1].scrollTop,75);
  assert.equal(t.fields['.modalbox'].scrollTop,90);
  t.fields['#oplogFollow'].checked=true;t.fields['#oplogFollow'].onchange();assert.equal(t.steps().scrollTop,800);
});
test('Finished jobs do not start polling and failed copies have no live ETA',async()=>{
  const t=setup('failed');await t.ctx.operationLog('import');assert.equal(t.tick(),undefined);
  assert.match(t.fields['#oplogBody'].innerHTML,/Copy stopped/);
  assert.doesNotMatch(t.ctx.operationCopyProgress({percent:20,eta_seconds:80},false),/ETA/);
  assert.match(t.ctx.operationCopyProgress({percent:100},true),/waiting for CDI/);
  assert.match(t.ctx.operationCopyProgress({percent:3,eta_seconds:null},true),/ETA estimating/);
});
test('Events and output from the same pod retain separate scroll positions',async()=>{
  const t=setup();t.frame({...t.getFrame(),sources:[...t.getFrame().sources,{title:'Pod events',pod:'copy-pod',text:'event'}]});
  await t.ctx.operationLog('import');t.fields['#oplogFollow'].checked=false;
  t.pres()[0].scrollTop=30;t.pres()[1].scrollTop=80;await t.tick()();
  assert.equal(t.pres()[0].scrollTop,30);assert.equal(t.pres()[1].scrollTop,80);
});
test('A late response from a previous job cannot replace the new job log',async()=>{
  const t=setup();const original=t.ctx.api;let resolve;
  t.ctx.api=path=>path.endsWith('id=old')?new Promise(r=>resolve=r):original(path);
  const pending=t.ctx.operationLog('old');await t.ctx.operationLog('new');
  const html=t.fields['#oplogBody'].innerHTML;resolve({status:'failed',message:'old error',sources:[]});await pending;
  assert.equal(t.fields['#oplogBody'].innerHTML,html);
});
