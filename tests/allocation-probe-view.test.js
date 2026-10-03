"use strict";
const test=require("node:test"), assert=require("node:assert/strict"), fs=require("node:fs"), vm=require("node:vm");
function setup(fail=false) {
  const calls=[], notices=[], fields={"#allocationProbeConsent":{checked:false}, "#allocationProbeDirectory":{value:"/custom/pod-resources"}};
  let html="", context="";
  const ctx={console, STATE:{data:{}}, $:key=>fields[key], esc:v=>String(v).replaceAll("<","&lt;").replaceAll('"',"&quot;"), tip:()=>"",
    childModal:(_title,body,_wide,style)=>{html=body;context=style;}, modalBack(){}, toast:m=>notices.push(m), ask:async()=>true,
    api:async(path,options)=>{calls.push({path,options});if(!options) return {installed:true,enabled:true,uid:"uid",resource_version:"12",detail:"<private>",directory:"/custom/pod-resources",capacity:{blocked:false,blockers:[],warnings:[],fingerprint:"review"}};
      if(fail) throw new Error("lost response");return {detail:"saved"};}};
  ctx.window=ctx;vm.createContext(ctx); require("./helpers/load-ui")(ctx);ctx.jsArg=s=>JSON.stringify(String(s??""));ctx.jsq=s=>(ctx.esc||String)(ctx.jsArg(s));vm.runInContext(fs.readFileSync("web/js/views-overview.js","utf8"),ctx);
  return {ctx,calls,notices,fields,html:()=>html,context:()=>context};
}
test("allocation collector requires host access consent and preserves reviewed identity",async()=>{
  const t=setup();await t.ctx.allocationProbeSettings();assert.match(t.html(),/&lt;private>/);
  assert.equal(t.context(),"operation-review");
  assert.match(t.html(),/Workload containers and VMs are not restarted/);
  await t.ctx.allocationProbeSave(true,{});assert.equal(t.calls.length,1);
  t.fields["#allocationProbeConsent"].checked=true;
  await t.ctx.allocationProbeSave(true,{});assert.equal(t.calls.length,2);
  const body=JSON.parse(t.calls[1].options.body);
  assert.equal(body.uid,"uid");assert.equal(body.resource_version,"12");assert.equal(body.acknowledge_host_access,true);
  await t.ctx.allocationProbeSave(true,{});assert.equal(t.calls.length,2);
});
test("uncertain collector save cannot be replayed without reloading",async()=>{
  const t=setup(true);await t.ctx.allocationProbeSettings();t.fields["#allocationProbeConsent"].checked=true;
  await t.ctx.allocationProbeSave(true,{});await t.ctx.allocationProbeSave(true,{});
  assert.equal(t.calls.length,2);assert.match(t.notices.join(" "),/Reopen VM allocation/);
});
test("disable requires confirmation but not enable-access consent",async()=>{
  const t=setup();await t.ctx.allocationProbeSettings();t.ctx.ask=async()=>false;
  await t.ctx.allocationProbeSave(false,{});assert.equal(t.calls.length,1);
  t.ctx.ask=async()=>true;await t.ctx.allocationProbeSave(false,{});
  assert.equal(JSON.parse(t.calls[1].options.body).enabled,false);
});
test("advanced details are collapsed and RAM acknowledgement cannot bypass blockers",async()=>{
  const t=setup();await t.ctx.allocationProbeSettings();
  assert.match(t.html(),/<details class="vm-placement-advanced">/);
  assert.doesNotMatch(t.html(),/<details[^>]*open/);
  t.fields["#allocationProbeConsent"].checked=true;
  t.ctx._allocationProbeReview.capacity.warnings=["RAM high"];
  await t.ctx.allocationProbeSave(true,{});assert.equal(t.calls.length,1);
  t.fields["#allocationProbeCapacity"]={checked:true};
  t.ctx._allocationProbeReview.capacity.blocked=true;
  await t.ctx.allocationProbeSave(true,{});assert.equal(t.calls.length,1);
  t.ctx._allocationProbeReview.capacity.blocked=false;
  await t.ctx.allocationProbeSave(true,{});
  const body=JSON.parse(t.calls[1].options.body);
  assert.equal(body.confirm_capacity,true);assert.equal(body.capacity_review,"review");
});
