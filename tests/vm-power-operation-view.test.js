"use strict";
const test=require("node:test"), assert=require("node:assert/strict"), fs=require("node:fs"), vm=require("node:vm");
test("protected power receipts cannot be dismissed or counted by clear finished",()=>{
  const elements={};
  for(const id of ["jobTray","jobSummary","jobClear","jobList"])elements[`#${id}`]={innerHTML:"",classList:{add(){},remove(){},toggle(){}},setAttribute(){}};
  const ctx={Date,console,URL,STATE:{data:{operations:[{id:"receipt",kind:"vm-power",status:"succeeded",title:"Start guest",
    message:"Ready",progress:100,dismissible:false,cancellable:false,resource:{name:"guest",namespace:"lab"}}]}},
    $:id=>elements[id],esc:String,icon:()=>"",setTimeout:()=>{},clearTimeout(){},ME:null};
  ctx.window=ctx;vm.createContext(ctx);vm.runInContext(fs.readFileSync("web/js/operations.js","utf8"),ctx);
  ctx.renderOperations();
  assert.equal(elements["#jobClear"].hidden,true);
  assert.doesNotMatch(elements["#jobList"].innerHTML,/dismissOperation\(/);
  assert.match(elements["#jobList"].innerHTML,/operationLog\('receipt'\)/);
  ctx.STATE.data.operations[0].dismissible=true;ctx.renderOperations();
  assert.equal(elements["#jobClear"].hidden,false);
  assert.match(elements["#jobList"].innerHTML,/dismissOperation\('receipt'\)/);
});
