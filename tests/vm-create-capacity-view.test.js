"use strict";
const test=require("node:test"), assert=require("node:assert/strict"), fs=require("node:fs"), vm=require("node:vm");
function setup({blocked=false,missing=false,fail=false}={}) {
  const fields={"#vmCreateApprove":{checked:false},"#vmCreateApply":{},"#mbody":{innerHTML:"",insertAdjacentHTML(_where,html){this.innerHTML=html+this.innerHTML;}}};
  const sent=[],exposed=[];
  const ctx={console, URLSearchParams, Map, Date, Promise, encodeURIComponent,
    document:{addEventListener(){}},STATE:{data:{}},$:key=>fields[key],$$:()=>[],esc:v=>String(v).replaceAll("<","&lt;"),
    toast(){},go(){},closeModal(){},modalBack(){},childModal:(_title,html)=>fields["#mbody"].innerHTML=html,
    deployCapacityHtml:p=>`<div>${p.warnings.join(" ")}</div>`,networkExpose:async(...args)=>exposed.push(args),
    api:async(path,options)=>{
      const body=JSON.parse(options.body);sent.push({path,body});
      if(path.endsWith("/preview"))return {config:missing ? null : {...body,mac:"52:54:00:11:22:33"},capacity_token:"signed-create",volumes:[],
        capacity:{blocked,warnings:["high RAM"],blockers:blocked ? ["missing KVM"] : []}};
      if(fail)throw new Error("connection lost");
      return {ok:true};
    }};
  ctx.window=ctx;vm.createContext(ctx);ctx.jsArg=s=>JSON.stringify(String(s??""));ctx.jsq=s=>(ctx.esc||String)(ctx.jsArg(s));vm.runInContext(fs.readFileSync("web/js/views-lifecycle.js","utf8"),ctx);
  return {ctx,fields,sent,exposed};
}
test("create review freezes generated MAC and user input, with explicit consent",async()=>{
  const t=setup(),cfg={name:"guest",namespace:"lab",memory:"2Gi",password:"do-not-display-this"};
  await t.ctx.vmCreateReview(cfg);
  assert.match(t.fields["#mbody"].innerHTML,/52:54:00:11:22:33/);
  assert.doesNotMatch(t.fields["#mbody"].innerHTML,/do-not-display-this/);
  await t.ctx.vmCreateReviewedApply();assert.equal(t.sent.length,1);
  cfg.memory="64Gi";
  t.fields["#vmCreateApprove"].checked=true;
  await t.ctx.vmCreateReviewedApply();
  assert.equal(t.sent[1].body.memory,"2Gi");
  assert.equal(t.sent[1].body.mac,"52:54:00:11:22:33");
  assert.equal(t.sent[1].body.capacity_token,"signed-create");
  await t.ctx.vmCreateReviewedApply();assert.equal(t.sent.length,2);
});
test("blocked and missing create reviews cannot submit",async()=>{
  for(const options of [{blocked:true},{missing:true}]){
    const t=setup(options);await t.ctx.vmCreateReview({name:"guest",namespace:"lab"});
    t.fields["#vmCreateApprove"].checked=true;await t.ctx.vmCreateReviewedApply();assert.equal(t.sent.length,1);
  }
});
test("uncertain create consumes review and retains explicit inspection guidance",async()=>{
  const t=setup({fail:true});await t.ctx.vmCreateReview({name:"guest",namespace:"lab"});
  t.fields["#vmCreateApprove"].checked=true;await t.ctx.vmCreateReviewedApply();await t.ctx.vmCreateReviewedApply();
  assert.equal(t.sent.length,2);assert.match(t.fields["#mbody"].innerHTML,/may already exist/);
  assert.equal(t.fields["#vmCreateApply"].disabled,true);
});
test("VIP workflow opens only after successful reviewed creation",async()=>{
  for(const fail of [false,true]){
    const t=setup({fail});await t.ctx.vmCreateReview({name:"guest",namespace:"lab"},{serviceMode:"manual",selectedVip:"192.0.2.10"});
    t.fields["#vmCreateApprove"].checked=true;await t.ctx.vmCreateReviewedApply();
    assert.equal(t.exposed.length,fail ? 0 : 1);
    if(!fail)assert.deepEqual(t.exposed[0],["lab","guest","VirtualMachine","192.0.2.10"]);
  }
});
test("back to configuration invalidates approval",async()=>{
  const t=setup();await t.ctx.vmCreateReview({name:"guest",namespace:"lab"});
  t.fields["#vmCreateApprove"].checked=true;t.ctx.vmCreateReviewBack();
  await t.ctx.vmCreateReviewedApply();assert.equal(t.sent.length,1);
});

test("create records its job and refreshes the tray on success or lost response",async()=>{
  for(const fail of [false,true]){
    const t=setup({fail}),jobs=[];let refreshed=0;
    const original=t.ctx.api;
    t.ctx.api=async(path,options)=>{const result=await original(path,options);return path.endsWith('/preview')?result:{...result,operation:{id:'created'}};};
    t.ctx.noteOperation=op=>jobs.push(op);t.ctx.refreshOperations=()=>refreshed++;
    await t.ctx.vmCreateReview({name:'guest',namespace:'lab'});
    t.fields['#vmCreateApprove'].checked=true;await t.ctx.vmCreateReviewedApply();
    assert.equal(jobs.length,fail?0:1);assert.equal(refreshed,1);
  }
});
