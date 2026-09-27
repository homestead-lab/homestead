"use strict";
const test=require("node:test"),assert=require("node:assert/strict"),fs=require("node:fs"),vm=require("node:vm");
function setup({blocked=false,missing=false,fail=false}={}){
  const sent=[],jobs=[],fields={"#jobSummary":{},".modalbox":{scrollTop:400},"#powerRecoveryApply":{disabled:true},
    "#powerRecoveryAck":{checked:false},"#powerRecoveryName":{value:""}};
  fields["#mbody"]={_html:"",get innerHTML(){return this._html;},set innerHTML(html){this._html=html;delete fields["#powerRecoveryLoading"];},
    insertAdjacentHTML(_where,html){this._html=html+this._html;}};
  const ctx={console,Date,URL,STATE:{data:{}},ME:null,$:key=>fields[key],$$:()=>[],
    esc:value=>String(value).replaceAll("<","&lt;"),icon:()=>"",clearTimeout(){},setTimeout(){},toast(){},closeModal(){},
    modal:(_title,html)=>{fields["#mbody"].innerHTML=html;fields["#powerRecoveryLoading"]={};},
    api:async(path,options)=>{
      sent.push({path,body:JSON.parse(options.body)});
      if(path.endsWith("/preview"))return {capacity_token:missing?null:"signed-recovery",plan:{blocked,blockers:blocked?["dispatcher active"]:[],
        warnings:["A late effect is possible"],action:"restart",dispatch_phase:"uncertain",confirm:"guest",resource:{namespace:"lab",name:"<guest>",original_uid:"old"},
        observed:{vm:{uid:"old"},instance:{uid:"new"},same_vm:true,vm_status:"Running",run_strategy:"Manual",instance_phase:"Running",ready:true,paused:false,queued_changes:0}}};
      if(fail)throw new Error("lost response");
      return {ok:true,detail:"Resolved as unknown",operation:{id:"job",status:"failed"}};
    }};
  ctx.window=ctx;vm.createContext(ctx);vm.runInContext(fs.readFileSync("web/js/operations.js","utf8"),ctx);
  ctx.noteOperation=job=>jobs.push(job);
  return {ctx,fields,sent,jobs};
}
test("recovery requires exact name and late-effect acknowledgement and sends once",async()=>{
  const t=setup();await t.ctx.powerRecoveryReview("job");
  assert.match(t.fields["#mbody"].innerHTML,/&lt;guest>/);
  assert.match(t.fields["#mbody"].innerHTML,/not a retry, rollback, cancellation or proof of success/);
  await t.ctx.powerRecoveryResolve();assert.equal(t.sent.length,1);
  t.fields["#powerRecoveryAck"].checked=true;t.fields["#powerRecoveryName"].value="wrong";
  await t.ctx.powerRecoveryResolve();assert.equal(t.sent.length,1);
  t.fields["#powerRecoveryName"].value="guest";await t.ctx.powerRecoveryResolve();await t.ctx.powerRecoveryResolve();
  assert.equal(t.sent.length,2);assert.equal(t.sent[1].body.capacity_token,"signed-recovery");
  assert.equal(t.sent[1].body.acknowledge_unknown,true);assert.equal(t.sent[1].body.confirm,"guest");
  assert.equal(t.jobs.length,1);assert.equal(t.jobs[0].status,"failed");
});
test("blocked or incomplete recovery review never sends resolve",async()=>{
  for(const options of [{blocked:true},{missing:true}]){
    const t=setup(options);await t.ctx.powerRecoveryReview("job");
    t.fields["#powerRecoveryAck"].checked=true;t.fields["#powerRecoveryName"].value="guest";
    await t.ctx.powerRecoveryResolve();assert.equal(t.sent.length,1);
  }
});
test("lost recovery response is not retried and warning scrolls into view",async()=>{
  const t=setup({fail:true});await t.ctx.powerRecoveryReview("job");
  t.fields["#powerRecoveryAck"].checked=true;t.fields["#powerRecoveryName"].value="guest";
  t.fields[".modalbox"].scrollTop=600;
  await t.ctx.powerRecoveryResolve();await t.ctx.powerRecoveryResolve();
  assert.equal(t.sent.length,2);assert.equal(t.jobs.length,0);
  assert.equal(t.fields[".modalbox"].scrollTop,0);assert.match(t.fields["#mbody"].innerHTML,/Nothing was retried/);
});
test("stale recovery response cannot replace a newer job review",async()=>{
  const t=setup(),original=t.ctx.api;let finish;
  t.ctx.api=(path,options)=>JSON.parse(options.body).id==="old"?new Promise(resolve=>finish=resolve):original(path,options);
  const pending=t.ctx.powerRecoveryReview("old");await t.ctx.powerRecoveryReview("new");
  finish({plan:{blocked:false,confirm:"old"},capacity_token:"old-token"});await pending;
  t.fields["#powerRecoveryAck"].checked=true;t.fields["#powerRecoveryName"].value="guest";
  await t.ctx.powerRecoveryResolve();assert.equal(t.sent.at(-1).body.id,"new");
});
