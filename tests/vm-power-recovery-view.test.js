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
  ctx.window=ctx;vm.createContext(ctx);vm.runInContext(fs.readFileSync("web/js/ui.js","utf8"),ctx);vm.runInContext(fs.readFileSync("web/js/operations.js","utf8"),ctx);
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

test("VM save recovery uses its own endpoint, escaped receipts and truthful retention language",async()=>{
  const t=setup(),original=t.ctx.api;
  t.ctx.api=async(path,options)=>{
    const result=await original(path,options);
    if(path.endsWith('/preview')) {
      delete result.plan.observed;
      result.plan.resources=[{resource:{kind:'Secret',namespace:'lab',name:'<login>'},last_write:'uncertain',relationship:'replacement; not adopted',
        expected:{uid:'old'},current:{uid:'new'}}];
    }
    return result;
  };
  await t.ctx.powerRecoveryReview('job',true);
  assert.equal(t.sent[0].path,'/api/operations/vm-recovery/preview');
  const html=t.fields['#mbody'].innerHTML;
  assert.match(html,/Resolve tracking, keep resources/);assert.match(html,/Secret · lab\/&lt;login>/);
  assert.match(html,/replacement; not adopted/);assert.doesNotMatch(html,/separately reviewed power action/);
  t.fields['#powerRecoveryAck'].checked=true;t.fields['#powerRecoveryName'].value='guest';
  await t.ctx.powerRecoveryResolve();await t.ctx.powerRecoveryResolve();
  assert.equal(t.sent.length,2);assert.equal(t.sent[1].path,'/api/operations/vm-recovery/resolve');
});

test("batch recovery lists unsent VMs and confirms the batch name, not a single VM",async()=>{
  const t=setup(),original=t.ctx.api;
  t.ctx.api=async(path,options)=>{
    const result=await original(path,options);
    if(path.endsWith('/preview')) {
      delete result.plan.observed;
      Object.assign(result.plan,{action:'k3s-cluster',confirm:'cluster',resource:{namespace:'lab',name:'cluster'},
        resources:[{resource:{kind:'VirtualMachine',namespace:'lab',name:'server-1'},last_write:'accepted',relationship:'same identity',expected:{uid:'one'},current:{uid:'one'}},
          {resource:{kind:'VirtualMachine',namespace:'lab',name:'agent-1'},last_write:'not dispatched',relationship:'not found',expected:null,current:null}]});
    }
    return result;
  };
  await t.ctx.powerRecoveryReview('batch-job',true);
  const html=t.fields['#mbody'].innerHTML;
  assert.match(html,/planned VMs and retained resources/);assert.match(html,/agent-1/);assert.match(html,/not dispatched/);
  t.fields['#powerRecoveryAck'].checked=true;t.fields['#powerRecoveryName'].value='server-1';
  await t.ctx.powerRecoveryResolve();assert.equal(t.sent.length,1);
  t.fields['#powerRecoveryName'].value='cluster';await t.ctx.powerRecoveryResolve();
  assert.equal(t.sent[1].body.confirm,'cluster');assert.equal(t.sent[1].path,'/api/operations/vm-recovery/resolve');
});

test("configuration recovery cannot be acknowledged without resource inspection data",async()=>{
  const t=setup();await t.ctx.powerRecoveryReview('job',true);
  assert.match(t.fields['#mbody'].innerHTML,/Resource inspection is incomplete/);
  t.fields['#powerRecoveryAck'].checked=true;t.fields['#powerRecoveryName'].value='guest';
  await t.ctx.powerRecoveryResolve();assert.equal(t.sent.length,1);
});

test("import recovery has one acknowledgement, keeps identities in details and never promises a retry",async()=>{
  const t=setup(),original=t.ctx.api;
  t.ctx.api=async(path,options)=>{
    const result=await original(path,options);
    if(path.endsWith('/preview')) Object.assign(result.plan,{action:'import-create',resources:[
      {resource:{kind:'Deployment',namespace:'lab',name:'<photos>'},last_write:'uncertain',relationship:'identity unproven',current:{uid:'observed-only'}}]});
    return result;
  };
  await t.ctx.powerRecoveryReview('import','import');
  const html=t.fields['#mbody'].innerHTML;
  assert.match(html,/&lt;photos>/);assert.match(html,/Retained resources/);assert.match(html,/details class="ui-more"/);
  assert.match(html,/Nothing will be retried, started or deleted/);assert.doesNotMatch(html,/id="powerRecoveryName"/);
  assert.equal((html.match(/type="checkbox"/g)||[]).length,1);
  await t.ctx.powerRecoveryResolve();assert.equal(t.sent.length,1);
  t.fields['#powerRecoveryAck'].checked=true;await t.ctx.powerRecoveryResolve();await t.ctx.powerRecoveryResolve();
  assert.equal(t.sent.length,2);assert.equal(t.sent[1].path,'/api/operations/vm-recovery/resolve');
  assert.equal(t.sent[1].body.acknowledge_unknown,true);
});
