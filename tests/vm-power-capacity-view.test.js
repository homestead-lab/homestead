"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");

function setup({blocked=false, missing=false, fail=false}={}) {
  const sent=[], notices=[], fields={"#modal":{classList:{contains:()=>false}},
    "#vmPowerApprove":{checked:false}, "#vmPowerApply":{}};
  const body={_html:"", get innerHTML(){return this._html;}, set innerHTML(html){this._html=html;delete fields["#vmPowerLoading"];},
    insertAdjacentHTML(_where, html){this._html=html+this._html;}};
  fields["#mbody"]=body;
  const open=(_title, html)=>{body.innerHTML=html;if(html.includes('id="vmPowerLoading"')) fields["#vmPowerLoading"]={};};
  const ctx={console, URLSearchParams, Map, Date, Promise, encodeURIComponent,
    document:{addEventListener(){}}, STATE:{data:{}},
    $:key=>fields[key], $$:()=>[], esc:v=>String(v).replaceAll("<","&lt;"),
    modal:open, childModal:open, modalBack(){}, refresh(){}, setTimeout:fn=>fn(), confirm:()=>true,
    toast:msg=>notices.push(msg), deployCapacityHtml:p=>`<div>${p.warnings.join(" ")}</div>`,
    api:async(path, options)=>{
      sent.push({path, body:JSON.parse(options.body)});
      if(path.endsWith("/preview")) return {capacity_token:"review-token", capacity:missing ? null : {blocked,
        pod_memory_gb:4.25, warnings:["High RAM"], blockers:blocked ? ["missing device"] : [],
        vm:{guest_memory_gb:4,request_is_lower_bound:true,policy_before:"Halted",policy_after:"Always"}}};
      if(fail) throw new Error("lost response");
      return {ok:true,detail:"starting"};
    }};
  ctx.window=ctx;
  vm.createContext(ctx);
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
