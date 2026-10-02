"use strict";
const test=require("node:test"), assert=require("node:assert/strict"), fs=require("node:fs"), vm=require("node:vm");
function setup({blocked=false,missing=false,fail=false,stateInit=false}={}) {
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
          vm:{admission_needed:true,policy_before:"Halted",policy_after:"Always",state_initialization:stateInit ? {name:"guest",reason:"Fresh state is not recovery"} : null}}};
      if(fail)throw new Error("connection lost");
      return {ok:true,detail:"saved"};
    }};
  ctx.window=ctx;vm.createContext(ctx);ctx.jsArg=s=>JSON.stringify(String(s??""));ctx.jsq=s=>(ctx.esc||String)(ctx.jsArg(s));vm.runInContext(fs.readFileSync("web/js/views-vms.js","utf8"),ctx);
  ctx.vmPowerReview=async config=>restarts.push(config);
  return {ctx,fields,sent,restarts};
}
test("edit initialization consent is separate and submitted only for exact typed name",async()=>{
  const t=setup({stateInit:true});
  await t.ctx.vmEditReview({ns:"lab",name:"guest",memory:"6Gi"});
  assert.match(t.fields["#mbody"].innerHTML,/Fresh state is not recovery/);
  t.fields["#vmEditApprove"].checked=true;
  t.fields["#vmEditStateAck"]={checked:false};t.fields["#vmEditStateName"]={value:"guest"};
  await t.ctx.vmEditReviewedApply();assert.equal(t.sent.length,1);
  t.fields["#vmEditStateAck"].checked=true;t.fields["#vmEditStateName"].value="wrong";
  assert.equal(t.ctx.vmEditReviewReady(),false);
  t.fields["#vmEditStateName"].value="guest";
  await t.ctx.vmEditReviewedApply();
  assert.equal(t.sent[1].body.ack_state_initialization,true);
  assert.equal(t.sent[1].body.confirm_state_name,"guest");
});
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
test("profile resources render read-only, escaped and explicitly unknown on failure",()=>{
  const t=setup();
  let html=t.ctx.vmEditResourceFields({cores:3,memory:"6Gi",resource_profile:{name:"medium<profile>"}});
  assert.match(html,/managed by instance type medium&lt;profile>/);
  assert.match(html,/>3</);assert.match(html,/>6Gi</);
  assert.doesNotMatch(html,/id="ve_cores"|id="ve_mem"/);
  html=t.ctx.vmEditResourceFields({cores:null,memory:"",profile_error:"Profile resources unavailable"});
  assert.match(html,/Unavailable/);assert.doesNotMatch(html,/input/);
});
test("save omits locked or unchanged profile fields but sends deliberate resource edits",async()=>{
  const t=setup(),reviews=[];
  t.ctx.__vmEdit={ns:"lab",name:"guest",v:{cores:3,memory:"6Gi"}};
  for(const [id,value] of Object.entries({ve_strategy:"Manual",ve_desc:"text",ve_node:""}))t.fields[`#${id}`]={value};
  t.ctx.vmEditReview=async body=>reviews.push(body);
  await t.ctx.vmEditSave();assert.equal("cores" in reviews[0],false);assert.equal("memory" in reviews[0],false);
  t.fields["#ve_cores"]={value:"3"};t.fields["#ve_mem"]={value:"6Gi"};
  await t.ctx.vmEditSave();assert.equal("cores" in reviews[1],false);assert.equal("memory" in reviews[1],false);
  t.fields["#ve_cores"].value="4";t.fields["#ve_mem"].value="8Gi";
  await t.ctx.vmEditSave();assert.equal(reviews[2].cores,4);assert.equal(reviews[2].memory,"8Gi");
});

test("edit records its job and refreshes the tray on success or lost response",async()=>{
  for(const fail of [false,true]){
    const t=setup({fail}),jobs=[];let refreshed=0;
    const original=t.ctx.api;
    t.ctx.api=async(path,options)=>{const result=await original(path,options);return path.endsWith('/preview')?result:{...result,operation:{id:'saved'}};};
    t.ctx.noteOperation=op=>jobs.push(op);t.ctx.refreshOperations=()=>refreshed++;
    await t.ctx.vmEditReview({name:'guest',ns:'lab'});
    t.fields['#vmEditApprove'].checked=true;await t.ctx.vmEditReviewedApply();
    assert.equal(jobs.length,fail?0:1);assert.equal(refreshed,1);
  }
});


test("isolation disables NIC controls without unlocking role-disabled controls",()=>{
  const t=setup(),card={disabled:false,dataset:{}},role={disabled:true,dataset:{}};
  t.fields['#ve_isolated']={checked:true};
  t.ctx.$$=()=>[card,role];
  t.ctx.vmIsolationChanged();
  assert.equal(card.disabled,true);assert.equal(role.disabled,true);
  t.fields['#ve_isolated'].checked=false;t.ctx.vmIsolationChanged();
  assert.equal(card.disabled,false);assert.equal(role.disabled,true);
});

test("isolation guards Add interface and save discards stale NIC rows",async()=>{
  const t=setup(),reviews=[];let additions=0;
  t.ctx.__vmEdit={ns:'lab',name:'guest',v:{cores:2,memory:'2Gi'},o:{}};
  t.fields['#ve_isolated']={checked:true};
  t.fields['#ve_nics']={insertAdjacentHTML(){additions++;}};
  for(const [id,value] of Object.entries({ve_strategy:'Manual',ve_desc:'',ve_node:''})) t.fields[`#${id}`]={value};
  t.ctx.$$=selector=>selector.includes('nic')||selector.includes('vn-add') ? [{querySelector(){throw new Error('stale NIC accessed');}}] : [];
  t.ctx.vmEditReview=async body=>reviews.push(body);
  t.ctx.vmAddNic();assert.equal(additions,0);
  await t.ctx.vmEditSave();
  assert.equal(reviews[0].isolated,true);
  assert.equal(reviews[0].nics.length,0);assert.equal(reviews[0].add_nics.length,0);
  assert.equal(reviews[0].restart,false);
});

test("isolation change is explicit in the edit review",async()=>{
  const t=setup();await t.ctx.vmEditReview({name:'guest',ns:'lab',isolated:true});
  assert.match(t.fields['#mbody'].innerHTML,/Isolated VM/);
  assert.match(t.fields['#mbody'].innerHTML,/removes all virtual network cards/);
  assert.match(t.fields['#mbody'].innerHTML,/at next start/);
});
