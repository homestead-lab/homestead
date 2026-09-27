"use strict";
const test=require("node:test"), assert=require("node:assert/strict"), fs=require("node:fs"), vm=require("node:vm");
function setup(send) {
  const fields={"#k_go":{disabled:false},"#k_capacity_confirm":{checked:true},"#k_review":{innerHTML:"",insertAdjacentHTML(_where,html){this.innerHTML+=html;}}};
  let calls=0,refreshes=0;
  const ctx={console,Date,Map,Promise,URLSearchParams,encodeURIComponent,STATE:{data:{}},document:{addEventListener(){}},
    $:key=>fields[key],$$:()=>[],esc:v=>String(v).replaceAll("<","&lt;"),toast(){},go(){},closeModal(){},
    refreshOperations(){refreshes++;},api:async(path)=>{
      if(path.endsWith("/plan"))return {config:{name:"test",review_id:"frozen"},capacity_token:"signed",ok:true,nodes:[],
        capacity:{blocked:false,warnings:[],nodes:[]}};
      calls++;return send();}};
  ctx.window=ctx;vm.createContext(ctx);vm.runInContext(fs.readFileSync("web/js/views-vms.js","utf8"),ctx);
  ctx.k3sBody=()=>({name:"test"});
  return {ctx,fields,calls:()=>calls,refreshes:()=>refreshes};
}
test("uncertain bootstrap is not replayed and directs user to retained resources",async()=>{
  const t=setup(()=>{throw new Error("<uncertain> response");});
  await t.ctx.k3sReview();
  await t.ctx.k3sCreate();await t.ctx.k3sCreate();
  assert.equal(t.calls(),1);assert.equal(t.fields["#k_go"].disabled,true);
  assert.match(t.fields["#k_review"].innerHTML,/may already exist/);
  assert.doesNotMatch(t.fields["#k_review"].innerHTML,/<uncertain>/);
  assert.equal(t.refreshes(),1);
});
test("a pending bootstrap cannot dispatch a duplicate even if button is re-enabled",async()=>{
  let finish;const wait=new Promise(resolve=>finish=resolve),t=setup(()=>wait);
  await t.ctx.k3sReview();
  const running=t.ctx.k3sCreate();t.fields["#k_go"].disabled=false;
  await t.ctx.k3sCreate();assert.equal(t.calls(),1);
  finish({ok:true});await running;
});
test("closing a form during an uncertain response still refreshes operation status",async()=>{
  let fail;const wait=new Promise((_resolve,reject)=>fail=reject),t=setup(()=>wait);
  await t.ctx.k3sReview();
  const running=t.ctx.k3sCreate();delete t.fields["#k_review"];
  fail(new Error("connection lost"));await running;
  assert.equal(t.calls(),1);assert.equal(t.refreshes(),1);
});
