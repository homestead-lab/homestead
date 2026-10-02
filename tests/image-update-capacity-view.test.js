"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const updateState = require("../web/js/update-state.js");

function setup({blocked = false, fail = "", missing = false, capacity = null} = {}) {
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
      if(path.endsWith("/preview")) return {capacity: missing ? null : capacity || {blocked, warnings:["High RAM"], candidates:[]},
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
  ctx.jsArg = s => JSON.stringify(String(s ?? "")); ctx.jsq = s => (ctx.esc || String)(ctx.jsArg(s));
  vm.runInContext(fs.readFileSync("web/js/ui.js", "utf8"), ctx);
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

test("routine rollout estimates stay in details while actual memory risks remain visible", async () => {
  const warnings = [
    "updated pod estimates include every container, not only the added container",
    "post-stop capacity assumes old pods have fully terminated and released ports and volumes; termination and storage detach are not guaranteed",
    "live RAM still includes old pods; it is not subtracted from the conservative projection",
    "RollingUpdate permits 1 extra pod(s) and 0 unavailable replica(s); terminating pods can extend the overlap",
    "intermediate rolling-update placement and readiness are not fully simulated; a fitting first pod is not a completion guarantee",
  ];
  for (const extra of [[], ["memory is not limited for data-permissions"], ["projected RAM reaches 95% (warning at 88%)"]]) {
    const t = setup({capacity: {blocked:false, warnings:[...warnings, ...extra], candidates:[],
      rollout:{strategy:"RollingUpdate", replicas:1, ownership_known:true, owned_pods:["old-pod"], release_request_gb:0.1, max_surge:1, max_unavailable:0}}});
    await t.ctx.imageUpdateReview("lab", "homestead");
    const html = t.fields["#mbody"].innerHTML;
    const visible = html.split('<details')[0];
    assert.doesNotMatch(visible, /post-stop capacity|updated pod estimates|live RAM still|intermediate rolling|RollingUpdate permits/);
    assert.match(html, /Capacity becomes available after old pods stop/);
    assert.match(html, /Homestead will be briefly unavailable/);
    assert.equal(visible.includes("upd-flag"), !!extra.length);
    if (extra[0]?.startsWith("memory")) assert.match(visible, /No memory limit is set for: data-permissions/);
    if (extra[0]?.startsWith("projected")) assert.match(visible, /projected RAM reaches 95%/);
    assert.equal(t.ctx.imageReviewReady(), false, "restart acknowledgement remains required");
  }
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

test("a change outside the pod template does not stop the queue; an outside change to it does", () => {
  const { ctx } = setup();
  const accepted = { uid: "u", generation: 22, rollout_at: "2026-09-30T06:33:03Z", images: { homestead: "img@sha256:new" } };
  // Homestead, started after its own update, set its rollout strategy: generation 23, same pods.
  assert.equal(ctx.rolloutChanged(accepted, { ...accepted, generation: 23 }), false);
  assert.equal(ctx.rolloutChanged(accepted, { ...accepted }), false);
  assert.equal(ctx.rolloutChanged(accepted, { ...accepted, uid: "other" }), true, "replaced");
  assert.equal(ctx.rolloutChanged(accepted, { ...accepted, generation: 23, rollout_at: "2026-09-30T06:40:00Z" }), true, "another update");
  assert.equal(ctx.rolloutChanged(accepted, { ...accepted, generation: 23, images: { homestead: "img@sha256:other" } }), true, "image edited");
  // An older Homestead that does not report the stamp: strict, as before.
  assert.equal(ctx.rolloutChanged({ ...accepted, rollout_at: undefined }, { ...accepted, generation: 23 }), true);
});

function cordonedPlan() {
  return {blocked:true, requires_confirmation:true, candidates:[{name:"node-1",eligible:false,reasons:["cordoned"]}],
    warnings:["no ready host satisfies this workload's placement requirements",
      "no scheduling order fits all requested replicas under the observed pod affinity and topology spread rules",
      "updated pod estimates include every container, not only the added container",
      "memory is not limited for data-permissions"],
    rollout:{strategy:"RollingUpdate",replicas:1,ownership_known:true,owned_pods:["old-pod"],release_request_gb:.1,max_surge:1,max_unavailable:0,start_blocked:true}};
}
test("a cordoned single host leads with one actionable error and keeps the review blocked", async () => {
  const t=setup({capacity:cordonedPlan()});
  await t.ctx.imageUpdateReview("lab","homestead");
  const html=t.fields["#mbody"].innerHTML, visible=html.split('<details')[0];
  assert.match(visible,/Update blocked/);
  assert.match(visible,/The only host is cordoned\. Uncordon it before updating\./);
  assert.doesNotMatch(visible,/scheduling order|placement requirements|data-permissions|updated pod estimates|Resolve the placement/);
  assert.doesNotMatch(html.match(/<details[^>]*>/)[0],/\bopen\b/,"Details starts collapsed even when blocked");
  assert.match(html,/The estimate covers all containers/);
  assert.match(html,/No memory limit is set for: data-permissions/);
  t.fields["#imageCapacityApprove"].checked=true;
  await t.ctx.imageReviewedApply();
  assert.equal(t.sent.filter(row=>row.path.endsWith('/apply')).length,0);
});
test("shared cordon blockers are grouped once and unrelated app warnings stay in Details", () => {
  const {ctx}=setup();
  const blocked=cordonedPlan();
  const shared=ctx.groupedConcerns([{config:{name:"homestead",clusterName:"Site A"},preview:{capacity:blocked}},
    {config:{name:"homestead",clusterName:"Site B"},preview:{capacity:blocked}}]);
  assert.equal(shared.length,1);
  assert.match(shared[0],/all 2 updates/);
  const mixed=ctx.groupedConcerns([{config:{name:"homestead",clusterName:"Site A"},preview:{capacity:blocked}},
    {config:{name:"app"},preview:{capacity:{blocked:false,warnings:["projected RAM reaches 95%"]}}}]);
  assert.equal(mixed.length,1);
  assert.match(mixed[0],/Site A · homestead/);
  assert.doesNotMatch(mixed[0],/projected RAM/);
});
test("cordon summaries distinguish all-host, mixed-host and healthy-host cases", () => {
  const {ctx}=setup(), plan=cordonedPlan();
  plan.candidates.push({name:"node-2",eligible:false,reasons:["cordoned"]});
  assert.match(ctx.imagePlacementBlocker(plan),/^All hosts are cordoned/);
  plan.candidates[1]={name:"node-2",eligible:false,reasons:["missing label pool=fast"]};
  assert.doesNotMatch(ctx.imagePlacementBlocker(plan),/cordoned/);
  plan.candidates=[{name:"node-1",eligible:false,reasons:["node is NotReady"]}];
  assert.match(ctx.imagePlacementBlocker(plan),/^The only host is not ready/);
  plan.candidates=[{name:"node-1",eligible:true,reasons:[]}];
  assert.match(ctx.imagePlacementBlocker(plan),/^A replacement pod cannot fit/);
});
