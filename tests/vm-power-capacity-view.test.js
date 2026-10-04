"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");

function setup({blocked=false, missing=false, fail=false, cpuEstimate=false, stateInit=false}={}) {
  const sent=[], notices=[], fields={"#modal":{classList:{contains:()=>false}},
    "#vmPowerApprove":{checked:false}, "#vmPowerApply":{}, ".modalbox":{scrollTop:500}};
  const body={_html:"", get innerHTML(){return this._html;}, set innerHTML(html){this._html=html;delete fields["#vmPowerLoading"];},
    insertAdjacentHTML(_where, html){this._html=html+this._html;}};
  fields["#mbody"]=body;
  const open=(_title, html)=>{body.innerHTML=html;if(html.includes('id="vmPowerLoading"')) fields["#vmPowerLoading"]={};};
  const ctx={console, URLSearchParams, Map, Date, Promise, encodeURIComponent,
    document:{addEventListener(){}}, STATE:{data:{}},
    $:key=>fields[key], $$:()=>[], esc:v=>String(v).replaceAll("<","&lt;"),
    modal:open, childModal:open, modalBack(){}, refresh(){}, setTimeout:fn=>fn(), ask:async()=>true,
    toast:msg=>notices.push(msg), deployCapacityHtml:p=>`<div>${p.warnings.join(" ")}</div>`,
    api:async(path, options)=>{
      sent.push({path, body:JSON.parse(options.body)});
      if(path.endsWith("/preview")) return {capacity_token:"review-token", capacity:missing ? null : {blocked,
        pod_memory_gb:4.25, warnings:["High RAM"], blockers:blocked ? ["missing device"] : [],
        vm:{guest_memory_gb:4,request_is_lower_bound:true,cpu_request_is_estimate:cpuEstimate,policy_before:"Halted",policy_after:"Always",
          state_initialization:stateInit ? {name:"guest",reason:"New <state> is not recovery"} : null}}};
      if(fail) throw new Error("lost response");
      return {ok:true,detail:"starting"};
    }};
  ctx.window=ctx;
  vm.createContext(ctx); require("./helpers/load-ui")(ctx);
  ctx.jsArg = s => JSON.stringify(String(s ?? "")); ctx.jsq = s => (ctx.esc || String)(ctx.jsArg(s));
  // The shared start review, as the page loads it.
  vm.runInContext(fs.readFileSync("web/js/views-workloads.js","utf8"),ctx);
  vm.runInContext(fs.readFileSync("web/js/views-vms.js","utf8"),ctx);
  return {ctx,fields,sent,notices};
}

test("start restart and resume review before dispatch and require acknowledgement", async()=>{
  for(const action of ["start","restart","unpause"]){
    const t=setup();
    await t.ctx.vmPower("lab","guest",action);
    assert.equal(t.sent.length,1);
    assert.equal(t.sent[0].path,"/api/vm/power/preview");
    assert.match(t.fields["#mbody"].innerHTML,/Halted.*Always/s);
    assert.match(t.fields["#mbody"].innerHTML,/not a configured memory limit/);
    await t.ctx.vmPowerReviewedApply();
    assert.equal(t.sent.length,1);
    t.fields["#vmPowerApprove"].checked=true;
    await t.ctx.vmPowerReviewedApply();
    assert.equal(t.sent.length,2);
    assert.equal(t.sent[1].body.capacity_token,"review-token");
    assert.equal(t.sent[1].body.action,action);
    assert.equal(t.sent[1].body.confirm_capacity,true);
    await t.ctx.vmPowerReviewedApply();
    assert.equal(t.sent.length,2,"consumed review cannot replay");
  }
});
test("unknown renderer CPU allowance is not presented as an exact request", async()=>{
  const t=setup({cpuEstimate:true});
  await t.ctx.vmPower("lab","guest","start");
  assert.match(t.fields["#mbody"].innerHTML,/conservative IO-thread allowance/);
  assert.match(t.fields["#mbody"].innerHTML,/RAM requests are lower bounds/);
  assert.doesNotMatch(t.fields["#mbody"].innerHTML,/Scheduler requests are lower bounds/);
});
test("fresh state needs its own checkbox and exact typed VM name", async()=>{
  const t=setup({stateInit:true});
  await t.ctx.vmPower("lab","guest","start");
  assert.match(t.fields["#mbody"].innerHTML,/Initialize fresh VM state/);
  assert.match(t.fields["#mbody"].innerHTML,/&lt;state>/);
  t.fields["#vmPowerApprove"].checked=true;
  t.fields["#vmPowerStateAck"]={checked:false};t.fields["#vmPowerStateName"]={value:"guest"};
  assert.equal(t.ctx.vmPowerReviewReady(),false);
  t.fields["#vmPowerStateAck"].checked=true;t.fields["#vmPowerStateName"].value="other";
  await t.ctx.vmPowerReviewedApply();assert.equal(t.sent.length,1);
  t.fields["#vmPowerStateName"].value="guest";
  assert.equal(t.ctx.vmPowerReviewReady(),true);
  await t.ctx.vmPowerReviewedApply();
  assert.equal(t.sent[1].body.ack_state_initialization,true);
  assert.equal(t.sent[1].body.confirm_state_name,"guest");
  await t.ctx.vmPowerReviewedApply();assert.equal(t.sent.length,2);
});
test("missing and blocked reviews cannot submit even with checked input", async()=>{
  for(const options of [{blocked:true},{missing:true}]){
    const t=setup(options);
    await t.ctx.vmPower("lab","guest","start");
    t.fields["#vmPowerApprove"].checked=true;
    await t.ctx.vmPowerReviewedApply();
    assert.equal(t.sent.length,1);
  }
});
test("power errors consume approval and explain uncertain outcome without retry", async()=>{
  const t=setup({fail:true});
  await t.ctx.vmPower("lab","guest","restart");
  t.fields["#vmPowerApprove"].checked=true;
  await t.ctx.vmPowerReviewedApply();
  await t.ctx.vmPowerReviewedApply();
  assert.equal(t.sent.length,2);
  assert.match(t.fields["#mbody"].innerHTML,/outcome may be uncertain/);
  assert.equal(t.fields[".modalbox"].scrollTop,0);
  assert.equal(t.fields["#vmPowerApply"].disabled,true);
});
test("stop force stop and pause stay available without capacity review", async()=>{
  for(const action of ["stop","force-stop","pause"]){
    const t=setup({blocked:true});
    await t.ctx.vmPower("lab","guest",action);
    assert.equal(t.sent.length,1);
    assert.equal(t.sent[0].path,"/api/vm/power");
  }
});
test("failed boot without a guest offers Stop retries in detail and compact views",()=>{
  const t=setup();
  t.ctx.icon=()=>"";
  for(const compact of [false,true]){
    const html=t.ctx.vmActionButton({ns:"lab",name:"guest",stop_retries:true},"stop",false,compact);
    assert.match(html,/aria-label="Stop retries"/);
    assert.match(html,/keep the VM off until you start it/);
    assert.doesNotMatch(html,/Ask the guest/);
  }
  const running=t.ctx.vmActionButton({ns:"lab",name:"guest",stop_retries:false},"stop");
  assert.match(running,/aria-label="Shut down"/);
  assert.match(running,/Ask the guest to power off/);
});
test("a stale preview cannot replace a newer VM review", async()=>{
  const t=setup();
  const original=t.ctx.api;
  let resolveOld;
  t.ctx.api=(path, opts)=>JSON.parse(opts.body).name==="old" ? new Promise(resolve=>resolveOld=resolve) : original(path,opts);
  const pending=t.ctx.vmPower("lab","old","start");
  await t.ctx.vmPower("lab","new","start");
  resolveOld({capacity_token:"old-token",capacity:{blocked:false,vm:{},warnings:[]}});
  await pending;
  t.fields["#vmPowerApprove"].checked=true;
  await t.ctx.vmPowerReviewedApply();
  assert.equal(t.sent.at(-1).body.name,"new");
  assert.equal(t.sent.at(-1).body.capacity_token,"review-token");
});
test("force stop stays callable while a start response is pending",async()=>{
  const t=setup();
  await t.ctx.vmPower("lab","guest","start");
  t.fields["#vmPowerApprove"].checked=true;
  const original=t.ctx.api;
  let finish;
  t.ctx.api=(path,options)=>JSON.parse(options.body).action==="start" ? new Promise(resolve=>finish=resolve) : original(path,options);
  const pending=t.ctx.vmPowerReviewedApply();
  await t.ctx.vmPower("lab","guest","force-stop");
  assert.equal(t.sent.at(-1).body.action,"force-stop");
  finish({ok:true,detail:"accepted"});
  await pending;
});
test("confirmed power receipt is added to the job tray and failures refresh it too",async()=>{
  for(const fail of [false,true]){
    const t=setup({fail}),records=[];
    let refreshes=0;
    t.ctx.noteOperation=job=>records.push(job);
    t.ctx.startOperationChecks=()=>refreshes++;
    const original=t.ctx.api;
    t.ctx.api=async(path,options)=>{
      const result=await original(path,options);
      return path.endsWith("/preview")?result:{...result,operation:{id:"job-id",kind:"vm-power"}};
    };
    await t.ctx.vmPower("lab","guest","start");
    t.fields["#vmPowerApprove"].checked=true;await t.ctx.vmPowerReviewedApply();
    assert.equal(records.length,fail?0:1);assert.equal(refreshes,1);
  }
});
