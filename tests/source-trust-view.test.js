"use strict";
const test = require("node:test"), assert = require("node:assert/strict");
const fs = require("node:fs"), vm = require("node:vm");
function setup({fail=false}={}) {
  const calls=[], fields={"#sourceKeyLoading":{},"#mbody":{},"#sourceKeyAck":{checked:false},"#sourceKeyApply":{}};
  let closed=0;
  const ctx={console,Map,Set,Date,Promise,URLSearchParams,encodeURIComponent,
    document:{addEventListener(){}},STATE:{data:{}},$:key=>fields[key],$$:()=>[],
    esc:x=>String(x).replaceAll('<','&lt;'),toast:()=>{},modal:(_title,_html,_wide,context)=>{fields.context=context;},
    closeModal:()=>closed++,resetPaint:()=>{},
    UI:new Proxy({}, {get:(_,key)=>(...args)=>JSON.stringify({[key]:args})}),
    api:async(path,opts)=>{
      calls.push({path,body:JSON.parse(opts.body)});
      if(path.endsWith('/scan'))return {name:'tower',key:'public-key',fingerprint:'SHA256:fixture',algorithm:'ssh-ed25519',capacity_token:'signed',connection:{host:'192.0.2.10',port:22}};
      if(fail)throw Error('lost response');
      return {ok:true};
    }};
  ctx.window=ctx;vm.createContext(ctx);ctx.jsArg=s=>JSON.stringify(String(s??""));ctx.jsq=s=>(ctx.esc||String)(ctx.jsArg(s));vm.runInContext(fs.readFileSync('web/js/views-lifecycle.js','utf8'),ctx);
  ctx.viewImport=()=>{};
  return {ctx,calls,fields,closed:()=>closed};
}
test('source trust requires acknowledgement and submits exact reviewed key once',async()=>{
  const t=setup();await t.ctx.srcVerify('tower');
  assert.equal(t.fields.context,'operation-review');
  assert.match(t.fields['#mbody'].innerHTML,/physical console/);
  await t.ctx.sourceKeyTrust();assert.equal(t.calls.length,1);
  t.fields['#sourceKeyAck'].checked=true;
  await t.ctx.sourceKeyTrust();await t.ctx.sourceKeyTrust();
  assert.equal(t.calls.length,2);
  assert.deepEqual(t.calls[1].body,{name:'tower',key:'public-key',capacity_token:'signed',confirm_fingerprint:true});
});
test('uncertain trust is not retried and asks for a fresh scan',async()=>{
  const t=setup({fail:true});await t.ctx.srcVerify('tower');t.fields['#sourceKeyAck'].checked=true;
  await t.ctx.sourceKeyTrust();await t.ctx.sourceKeyTrust();
  assert.equal(t.calls.length,2);assert.match(t.fields['#mbody'].innerHTML,/nothing was retried/);
});
test('closing a dialog ignores late scan and trust responses',async()=>{
  for(const operation of ['scan','trust']) {
    const t=setup();let finish;
    if(operation==='trust') {await t.ctx.srcVerify('tower');t.fields['#sourceKeyAck'].checked=true;}
    t.ctx.api=()=>new Promise(resolve=>{finish=resolve;});
    const pending=operation==='scan'?t.ctx.srcVerify('tower'):t.ctx.sourceKeyTrust();
    delete t.fields['#sourceKeyLoading'];delete t.fields['#sourceKeyApply'];
    t.fields['#mbody'].innerHTML='new unrelated dialog';finish({});await pending;
    assert.equal(t.fields['#mbody'].innerHTML,'new unrelated dialog');assert.equal(t.closed(),0);
  }
});
test('incomplete scan cannot enable trust',async()=>{
  const t=setup();t.ctx.api=async()=>({name:'tower'});
  await t.ctx.srcVerify('tower');t.fields['#sourceKeyAck'].checked=true;
  assert.equal(t.ctx.sourceKeyReady(),false);await t.ctx.sourceKeyTrust();
  assert.match(t.fields['#mbody'].innerHTML,/Nothing was trusted/);
});
