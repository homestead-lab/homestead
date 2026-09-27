"use strict";
const test=require("node:test"),assert=require("node:assert/strict"),fs=require("node:fs"),vm=require("node:vm");
function setup({cleanup=true,mode="forget",copy=false,blocked=false}={}) {
  const elements={};
  for(const id of ["jobTray","jobSummary","jobClear","jobList","oc_go","oc_confirm"])
    elements[`#${id}`]={innerHTML:"",value:"",disabled:true,classList:{add(){},remove(){},toggle(){}},setAttribute(){}};
  const ack={checked:false,dataset:{cancelOption:"ack"}};
  elements["[data-cancel-option='ack']"]=ack;
  const calls=[],modal={};
  const job={id:"old",kind:"k3s-cluster",status:"failed",title:"Legacy build",progress:20,
    tracking_only:true,cleanable:true,dismissible:false,cancellable:false};
  const ctx={Date,URL,console,STATE:{data:{operations:[job]}},ME:null,$:id=>elements[id],esc:String,icon:()=>"",
    setTimeout(){},clearTimeout(){},can:()=>true,toast(){},closeModal(){},document:{querySelectorAll:()=>copy?[ack]:[]},
    modal:(title,body,wide,context)=>Object.assign(modal,{title,body,context}),api:async(path,options)=>{
      calls.push({path,body:JSON.parse(options.body)});
      if(path.endsWith("cancel-plan"))return {id:"old",title:"Legacy build",mode,cleanup,can:!blocked,severity:"low",
        ...(copy?{copy_recovery:true,lead:"Release the hold only; no app is started and no data is deleted.",why_not:"Copy is still active."}:{}),
        action:mode==="forget"?"Stop tracking it":"Cancel and put back",needs:"admin",confirm:"k3s-lab",
        undo:mode==="forget"?[]:["Deletes owned resources"],keeps:["All VMs, disks, Secrets and IP-address records remain"],options:[]};
      return {operation:{...job,cleanable:false,dismissible:true,tracking_stopped:true}};
    }};
  ctx.window=ctx;vm.createContext(ctx);vm.runInContext(fs.readFileSync("web/js/operations.js","utf8"),ctx);
  return {ctx,elements,calls,modal};
}
test("retained failed k3s jobs have a review action and cannot be cleared silently",()=>{
  const t=setup();t.ctx.renderOperations();
  assert.match(t.elements["#jobList"].innerHTML,/Review retained resources/);
  assert.doesNotMatch(t.elements["#jobList"].innerHTML,/>Clean up<|dismissOperation\(/);
  assert.equal(t.elements["#jobClear"].hidden,true);
});
test("failed tracking-only confirmation does not claim cleanup and requires the name",async()=>{
  const t=setup();await t.ctx.cancelOperation("old");
  assert.match(t.modal.title,/Stop tracking/);
  assert.equal(t.modal.context,"operation-review");
  assert.match(t.modal.body,/Nothing is deleted or stopped in the cluster/);
  assert.match(t.modal.body,/All VMs, disks, Secrets and IP-address records remain/);
  assert.match(t.modal.body,/Keep tracking/);
  assert.doesNotMatch(t.modal.body,/Remove what it made|Cleaning up removes/);
  await t.ctx.cancelOperationGo("old");assert.equal(t.calls.length,1);
  t.elements["#oc_confirm"].value="k3s-lab";t.ctx.cancelOperationGate();
  assert.equal(t.elements["#oc_go"].disabled,false);
  await t.ctx.cancelOperationGo("old");assert.equal(t.calls.length,2);
  assert.equal(t.calls[1].body.confirm,"k3s-lab");
  assert.equal(t.ctx.STATE.data.operations[0].status,"failed");
  assert.equal(t.ctx.STATE.data.operations[0].dismissible,true);
});
test("actual destructive cleanup plans still say what they remove",async()=>{
  const t=setup({mode:"rollback"});await t.ctx.cancelOperation("old");
  assert.match(t.modal.title,/Clean up/);assert.match(t.modal.body,/Remove what it made/);
});

test("copy recovery needs typed name AND partial-data acknowledgement without promising rollback",async()=>{
  const t=setup({copy:true});await t.ctx.cancelOperation("old");
  assert.match(t.modal.title,/Inspect storage copy/);
  assert.match(t.modal.body,/Release the hold only/);
  assert.doesNotMatch(t.modal.body,/Cleaning up removes|What carries on/);
  t.elements["#oc_confirm"].value="k3s-lab";t.ctx.cancelOperationGate();
  assert.equal(t.elements["#oc_go"].disabled,true);
  await t.ctx.cancelOperationGo("old");assert.equal(t.calls.length,1);
  t.elements["[data-cancel-option='ack']"].checked=true;t.ctx.cancelOperationGate();
  assert.equal(t.elements["#oc_go"].disabled,false);
  await t.ctx.cancelOperationGo("old");assert.equal(t.calls[1].body.options.ack,true);
});

test("active copy recovery has no submit button",async()=>{
  const t=setup({copy:true,blocked:true});await t.ctx.cancelOperation("old");
  assert.match(t.modal.body,/copy hold cannot be released yet/);
  assert.doesNotMatch(t.modal.body,/id="oc_go"/);
});
