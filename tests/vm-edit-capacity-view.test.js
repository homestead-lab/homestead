"use strict";
const test=require("node:test"), assert=require("node:assert/strict"), fs=require("node:fs"), vm=require("node:vm");
function setup({blocked=false,missing=false,fail=false}={}) {
  const fields={"#vmEditLoading":{},"#vmEditApprove":{checked:false},"#vmEditApply":{},"#mbody":{innerHTML:"",insertAdjacentHTML(_where,html){this.innerHTML=html+this.innerHTML;}}};
  const sent=[],restarts=[];
  const ctx={console, URLSearchParams, Map, Date, Promise, encodeURIComponent,
    document:{addEventListener(){}},STATE:{data:{}},$:key=>fields[key],$$:()=>[],esc:v=>String(v).replaceAll("<","&lt;"),
    toast(){},refresh(){},closeModal(){},modalBack(){},childModal:(_title,html)=>fields["#mbody"].innerHTML=html,
    deployCapacityHtml:p=>`<div>${p.warnings.join(" ")}</div>`,
    api:async(path,options)=>{
      const body=JSON.parse(options.body);sent.push({path,body});
      if(path.endsWith("/preview"))return {capacity_token:missing ? null : "signed-edit",volumes:[],
        capacity:{blocked,warnings:["high RAM"],blockers:blocked ? ["missing KVM"] : [],
          vm:{admission_needed:true,policy_before:"Halted",policy_after:"Always"}}};
      if(fail)throw new Error("connection lost");
      return {ok:true,detail:"saved"};
    }};
  ctx.window=ctx;vm.createContext(ctx);vm.runInContext(fs.readFileSync("web/js/views-vms.js","utf8"),ctx);
  ctx.vmPowerReview=async config=>restarts.push(config);
  return {ctx,fields,sent,restarts};
}
test("edit review freezes nested input, hides secrets and requires consent",async()=>{
  const t=setup(),cfg={name:"guest",ns:"lab",memory:"6Gi",cloud_init:{user_data:"private-test-data"}};
  await t.ctx.vmEditReview(cfg);
  assert.doesNotMatch(t.fields["#mbody"].innerHTML,/private-test-data/);
  assert.match(t.fields["#mbody"].innerHTML,/Halted.*Always/);
  await t.ctx.vmEditReviewedApply();assert.equal(t.sent.length,1);
  cfg.memory="64Gi";cfg.cloud_init.user_data="changed";
  t.fields["#vmEditApprove"].checked=true;await t.ctx.vmEditReviewedApply();
  assert.equal(t.sent[1].body.memory,"6Gi");
  assert.equal(t.sent[1].body.cloud_init.user_data,"private-test-data");
  assert.equal(t.sent[1].body.capacity_token,"signed-edit");
  await t.ctx.vmEditReviewedApply();assert.equal(t.sent.length,2);
});
test("blocked or missing edit review cannot send",async()=>{
  for(const options of [{blocked:true},{missing:true}]){
    const t=setup(options);await t.ctx.vmEditReview({name:"guest",ns:"lab"});
    t.fields["#vmEditApprove"].checked=true;await t.ctx.vmEditReviewedApply();assert.equal(t.sent.length,1);
  }
});
test("uncertain save is not replayed and never opens restart",async()=>{
  const t=setup({fail:true});await t.ctx.vmEditReview({name:"guest",ns:"lab"},true);
  t.fields["#vmEditApprove"].checked=true;await t.ctx.vmEditReviewedApply();await t.ctx.vmEditReviewedApply();
  assert.equal(t.sent.length,2);assert.equal(t.restarts.length,0);
  assert.match(t.fields["#mbody"].innerHTML,/may already be saved/);
  assert.equal(t.fields["#vmEditApply"].disabled,true);
});
test("restart is a new review only after a confirmed save",async()=>{
  const t=setup();await t.ctx.vmEditReview({name:"guest",ns:"lab",restart:false},true);
  t.fields["#vmEditApprove"].checked=true;await t.ctx.vmEditReviewedApply();
  assert.equal(t.sent.length,2);assert.equal(t.sent[1].body.restart,false);
  assert.equal(t.restarts.length,1);assert.equal(t.restarts[0].action,"restart");
  assert.equal(t.restarts[0].name,"guest");
});
test("going back invalidates approval",async()=>{
  const t=setup();await t.ctx.vmEditReview({name:"guest",ns:"lab"});
  t.fields["#vmEditApprove"].checked=true;t.ctx.vmEditReviewBack();
  await t.ctx.vmEditReviewedApply();assert.equal(t.sent.length,1);
});
