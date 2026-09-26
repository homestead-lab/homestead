"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const updateState = require("../web/js/update-state.js");

function setup({blocked = false, fail = "", missing = false} = {}) {
  const sent = [], fields = {"#imageCapacityApprove": {checked:false}, "#imageCapacityApply": {},
    "#modal": {classList:{contains:()=>false}}};
  const ctx = {console, URLSearchParams, Map, Date, Promise, encodeURIComponent,
    document:{addEventListener(){}}, STATE:{data:{}}, HomesteadUpdateState:updateState,
    $:id=>fields[id], $$:()=>[], esc:s=>String(s).replaceAll("<","&lt;"),
    toast:()=>{}, setTimeout:cb=>cb(),
    modal:(_title,body)=>{
      delete fields["#imageReviewLoading"]; delete fields["#imageQueue"];
      if(body.includes('id="imageReviewLoading"')) fields["#imageReviewLoading"]={};
      if(body.includes('id="imageQueue"')) fields["#imageQueue"]={innerHTML:""};
      fields["#mbody"]={innerHTML:body};
    },
    api:async (path, options)=>{
      const body = options ? JSON.parse(options.body) : null;
      sent.push({path,body});
      if(path.endsWith("/preview")) return {capacity: missing ? null : {blocked, warnings:["High RAM"], candidates:[]},
        capacity_token:"signed-"+body.name,images:[{container:"app",before:"old",after:"new@digest",rollback:"old@digest"}]};
      if(path.includes("/progress")) {
        if(fail==="contact") throw new Error("connection lost");
        return {uid: fail==="replacement" ? "different" : "workload-uid",generation:4,phase:fail==="rollout" ? "failed" : "ready",ready:1,desired:1};
      }
      if(fail==="apply") throw new Error("changed target");
      return {uid:"workload-uid",generation:4,phase:"starting",ready:0,desired:1};
    }};
  ctx.window=ctx;
  vm.createContext(ctx);
  vm.runInContext(fs.readFileSync("web/js/views-workloads.js","utf8"),ctx);
  return {ctx,fields,sent};
}
test("single update requires preview and acknowledgement; exact token is submitted",async()=>{
  const t=setup();
  await t.ctx.imageUpdateReview("lab","app");
  assert.match(t.fields["#mbody"].innerHTML,/High RAM/);
  assert.match(t.fields["#mbody"].innerHTML,/new@digest/);
  await t.ctx.imageReviewedApply();
  assert.equal(t.sent.filter(s=>s.path.endsWith("/apply")).length,0);
  t.fields["#imageCapacityApprove"].checked=true;
  await t.ctx.imageReviewedApply();
  const applied=t.sent.find(s=>s.path.endsWith("/apply")).body;
  assert.equal(applied.capacity_token,"signed-app");
  assert.equal(applied.confirm_capacity,true);
  await t.ctx.imageReviewedApply();
  assert.equal(t.sent.filter(s=>s.path.endsWith("/apply")).length,1,"consumed review cannot be replayed");
});
test("blocked or missing plans cannot be forced through with a checkbox",async()=>{
  for(const options of [{blocked:true},{missing:true}]){
    const t=setup(options);
    await t.ctx.imageUpdateReview("lab","app");
    t.fields["#imageCapacityApprove"].checked=true;
    await t.ctx.imageReviewedApply();
    assert.equal(t.sent.filter(s=>s.path.endsWith("/apply")).length,0);
  }
});
test("rollback uses the same reviewed token flow",async()=>{
  const t=setup();
  await t.ctx.imageRollback("lab","app");
  assert.equal(t.sent[0].body.action,"rollback");
  t.fields["#imageCapacityApprove"].checked=true;
  await t.ctx.imageRollbackApply();
  assert.equal(t.sent.find(s=>s.path.endsWith("/rollback")).body.capacity_token,"signed-app");
});
test("batch waits for readiness, updates Homestead last and stops on any uncertainty",async()=>{
  for(const fail of ["","apply","rollout","contact","replacement"]){
    const t=setup({fail});
    await t.ctx.reviewImageActions([{ns:"lab",name:"homestead"},{ns:"lab",name:"app"}]);
    t.fields["#imageCapacityApprove"].checked=true;
    await t.ctx.imageReviewedApply();
    const applies=t.sent.filter(s=>s.path.endsWith("/apply"));
    assert.equal(applies[0].body.name,"app");
    assert.equal(applies.length,fail ? 1 : 2);
    if(!fail){
      const firstProgress=t.sent.findIndex(s=>s.path.includes("/progress"));
      const secondApply=t.sent.findIndex(s=>s.path.endsWith("/apply")&&s.body.name==="homestead");
      assert.ok(firstProgress<secondApply);
    } else assert.match(t.fields["#imageQueue"].innerHTML,/not started/);
  }
});
