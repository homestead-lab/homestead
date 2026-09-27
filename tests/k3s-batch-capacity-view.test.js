"use strict";
const test=require("node:test"), assert=require("node:assert/strict"), fs=require("node:fs"), vm=require("node:vm");
function setup({blocked=false,missing=false}={}) {
  const fields={"#k_go":{disabled:true},"#k_capacity_confirm":{checked:false},"#k_review":{innerHTML:"",insertAdjacentHTML(_where,html){this.innerHTML+=html;}}};
  const sent=[], cfg={name:"cluster",memory:"4Gi",password:"private-data"};
  const ctx={console,Date,Map,Promise,URLSearchParams,encodeURIComponent,STATE:{data:{}},document:{addEventListener(){}},
    $:key=>fields[key],$$:()=>[],esc:v=>String(v).replaceAll("<","&lt;"),toast(){},go(){},closeModal(){},
    api:async(path,options)=>{
      const body=JSON.parse(options.body);sent.push({path,body});
      if(path.endsWith("/plan"))return {config:missing ? null : {...body,review_id:"frozen",macs:{guest:"52:54:00:11:22:33"}},
        capacity_token:"signed-batch",ok:!blocked,nodes:[{name:"guest",role:"server",address:"192.0.2.20"}],
        capacity:{blocked,status:blocked?"blocked":"fits",warnings:["capacity may change"],nodes:[],example:[]}};
      return {ok:true};}};
  ctx.window=ctx;vm.createContext(ctx);vm.runInContext(fs.readFileSync("web/js/views-vms.js","utf8"),ctx);
  ctx.k3sBody=()=>({...cfg});
  return {ctx,fields,sent,cfg};
}
test("batch requires consent and submits frozen server-normalized configuration once",async()=>{
  const t=setup();await t.ctx.k3sReview();await t.ctx.k3sCreate();assert.equal(t.sent.length,1);
  assert.doesNotMatch(t.fields["#k_review"].innerHTML,/private-data/);
  t.fields["#k_capacity_confirm"].checked=true;await t.ctx.k3sCreate();await t.ctx.k3sCreate();
  assert.equal(t.sent.length,2);assert.equal(t.sent[1].body.review_id,"frozen");
  assert.equal(t.sent[1].body.capacity_token,"signed-batch");
  assert.equal(t.sent[1].body.macs.guest,"52:54:00:11:22:33");
});
test("editing a field invalidates approval even if its DOM event is missed",async()=>{
  const t=setup();await t.ctx.k3sReview();t.fields["#k_capacity_confirm"].checked=true;
  t.cfg.memory="8Gi";await t.ctx.k3sCreate();assert.equal(t.sent.length,1);
  assert.equal(t.fields["#k_go"].disabled,true);
});
test("blocked or incomplete batch response never permits creation",async()=>{
  for(const options of [{blocked:true},{missing:true}]){
    const t=setup(options);await t.ctx.k3sReview();t.fields["#k_capacity_confirm"].checked=true;
    await t.ctx.k3sCreate();assert.equal(t.sent.length,1);
  }
});
test("stale batch preview cannot restore approval after input invalidation",async()=>{
  const t=setup();let resolve;
  t.ctx.api=()=>new Promise(r=>resolve=r);
  const waiting=t.ctx.k3sReview();t.ctx.k3sInvalidateReview();
  resolve({config:{name:"cluster"},capacity_token:"old",nodes:[],capacity:{blocked:false}});await waiting;
  t.fields["#k_capacity_confirm"].checked=true;assert.equal(t.ctx.k3sReviewReady(),false);
});
