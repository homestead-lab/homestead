"use strict";
// The Jobs page's "End: host removed" for a disk job a removed host left behind.
const test=require("node:test"),assert=require("node:assert/strict"),fs=require("node:fs"),vm=require("node:vm");
const PLAN={id:"job",kind:"longhorn-runtime",node:"node-a",phase:"staged",blocked:false,blockers:[],evidence:{node:"missing",longhorn:"replaced"},
  left_on_host:["Boot unit /etc/systemd/system/homestead-longhorn-runtime.service, which runs <before> k3s on every boot."],
  left_in_cluster:["Helper Job homestead-allocation-x in lab: deleted when you end the job"],
  released:["Longhorn upgrades, cluster shutdown and OS rollouts, which this job holds back cluster-wide."],
  warnings:["Nothing on the host is changed."],requires_confirmation:true,confirm:"node-a"};
function setup({plan=PLAN,token="signed-review",fail=false}={}){
  const sent=[],jobs=[],toasts=[],fields={"#jobSummary":{},".modalbox":{scrollTop:400},"#hostGoneApply":{disabled:true},
    "#hostGoneAck":{checked:false},"#hostGoneName":{value:"",get dataset(){return {typedExpect:fields["#mbody"]._html.match(/id="hostGoneName"[^>]*data-typed-expect="([^"]*)"/)?.[1]};}}};
  fields["#mbody"]={_html:"",get innerHTML(){return this._html;},set innerHTML(html){this._html=html;delete fields["#hostGoneLoading"];},
    insertAdjacentHTML(_where,html){this._html=html+this._html;}};
  const ctx={console,Date,URL,STATE:{data:{}},ME:null,$:key=>fields[key],$$:()=>[],
    esc:value=>String(value).replaceAll("<","&lt;"),icon:()=>"",clearTimeout(){},setTimeout(){},toast:(m,k)=>toasts.push([m,k]),closeModal(){},refreshOperations(){},
    modal:(_title,html)=>{fields["#mbody"].innerHTML=html;fields["#hostGoneLoading"]={};},
    api:async(path,options)=>{
      sent.push({path,body:JSON.parse(options.body)});
      if(path.endsWith("/preview"))return {capacity_token:token,plan};
      if(fail)throw new Error("What Homestead found has changed since the review");
      return {ok:true,detail:"Ended.",operation:{id:"job",status:"failed"}};
    }};
  ctx.window=ctx;vm.createContext(ctx);ctx.jsArg=s=>JSON.stringify(String(s??""));ctx.jsq=s=>(ctx.esc||String)(ctx.jsArg(s));
  vm.runInContext(fs.readFileSync("web/js/ui.js","utf8"),ctx);vm.runInContext(fs.readFileSync("web/js/operations.js","utf8"),ctx);
  ctx.noteOperation=job=>jobs.push(job);
  return {ctx,fields,sent,jobs,toasts};
}
const job=extra=>({id:"job",kind:"disk-v2-convert",title:"Prepare /dev/sdb",status:"failed",message:"",...extra});

test("the card offers the end, to admins, only for a job flagged endable",()=>{
  const t=setup();
  const card=t.ctx.operationCard(job({host_gone_endable:true}));
  assert.match(card,/<button class="btn sm" data-need="admin"[^>]*onclick="hostGoneReview\((&quot;|")job(&quot;|")\)">End: host removed<\/button>/);
  assert.doesNotMatch(t.ctx.operationCard(job({host_gone_endable:false})),/hostGoneReview/);
  assert.doesNotMatch(t.ctx.operationCard(job({})),/hostGoneReview/);
});

test("the review lists what is left, escaped, and needs the host name and the tick before it sends once",async()=>{
  const t=setup();await t.ctx.hostGoneReview("job");
  assert.deepEqual(t.sent[0],{path:"/api/operations/host-gone/preview",body:{id:"job"}});
  const html=t.fields["#mbody"].innerHTML;
  assert.match(html,/runs &lt;before> k3s/);
  assert.match(html,/homestead-allocation-x/);
  assert.match(html,/Longhorn upgrades, cluster shutdown and OS rollouts/);
  assert.match(html,/Nothing on the host is changed/);
  assert.match(html,/which is gone, so it can.t finish/);
  assert.match(html,/id="hostGoneName"[^>]*data-typed-expect="node-a"/);
  await t.ctx.hostGoneResolve();assert.equal(t.sent.length,1,"nothing without the name and the tick");
  t.fields["#hostGoneAck"].checked=true;t.fields["#hostGoneName"].value="node-b";
  await t.ctx.hostGoneResolve();assert.equal(t.sent.length,1,"nothing with another name");
  t.fields["#hostGoneName"].value="node-a";
  await t.ctx.hostGoneResolve();await t.ctx.hostGoneResolve();
  assert.equal(t.sent.length,2,"sent once");
  assert.deepEqual(t.sent[1],{path:"/api/operations/host-gone/resolve",body:{id:"job",capacity_token:"signed-review",confirm_capacity:true,
    confirm:"node-a",acknowledge_host_gone:true}});
  assert.equal(t.jobs.length,1);
});

test("a blocked or tokenless review never sends the end",async()=>{
  for(const options of [{plan:{...PLAN,blocked:true,blockers:["node-a is still in the cluster with the same identity"]},token:null},{token:null}]){
    const t=setup(options);await t.ctx.hostGoneReview("job");
    t.fields["#hostGoneAck"].checked=true;t.fields["#hostGoneName"].value="node-a";
    await t.ctx.hostGoneResolve();assert.equal(t.sent.length,1);
  }
  const blocked=setup({plan:{...PLAN,blocked:true,blockers:["node-a is still in the cluster with the same identity"]},token:null});
  await blocked.ctx.hostGoneReview("job");
  assert.match(blocked.fields["#mbody"].innerHTML,/still in the cluster with the same identity/);
  assert.match(blocked.fields["#mbody"].innerHTML,/Homestead checks whether that host is gone/);
  assert.doesNotMatch(blocked.fields["#mbody"].innerHTML,/which is gone, so it can.t finish/,"a blocked review does not claim the host is gone");
  assert.doesNotMatch(blocked.fields["#mbody"].innerHTML,/id="hostGoneName"/);
});

test("a refused end says nothing was ended and is not retried",async()=>{
  const t=setup({fail:true});await t.ctx.hostGoneReview("job");
  t.fields["#hostGoneAck"].checked=true;t.fields["#hostGoneName"].value="node-a";
  await t.ctx.hostGoneResolve();await t.ctx.hostGoneResolve();
  assert.equal(t.sent.length,2);assert.equal(t.jobs.length,0);
  assert.match(t.fields["#mbody"].innerHTML,/Nothing was ended/);
  assert.equal(t.fields[".modalbox"].scrollTop,0);
});
