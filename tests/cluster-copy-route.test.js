"use strict";
const test=require("node:test"), assert=require("node:assert/strict"), fs=require("node:fs"), vm=require("node:vm");

function setup() {
  const sent=[], storage=new Map();
  const ctx={console,URLSearchParams,Set,Map,Date,Promise,encodeURIComponent,
    document:{addEventListener(){}},localStorage:{getItem:k=>storage.get(k),setItem:(k,v)=>storage.set(k,v)},
    esc:String,jsq:JSON.stringify,toast(){},UI:{lead:t=>t,actions:t=>t,cancel:()=>""},
    modal:(title,html)=>{ctx.title=title;ctx.html=html;},
    api:async(path,opts)=>{sent.push({path,body:opts.body?JSON.parse(opts.body):null,headers:opts.headers});
      return path==="/api/move/hello"?{capabilities:["copy-destination"]}:{};},
    location:{href:"",pathname:"/settings",search:""},history:{replaceState(){}},
    HomesteadRouter:require("../web/js/router.js")};
  ctx.window=ctx;vm.createContext(ctx);
  vm.runInContext(fs.readFileSync("web/js/fleet.js","utf8"),ctx);
  ctx.FLEET.view={linked:true,members:[{id:"a",handle:"source",self:true,name:"Source",reachable:true},
    {id:"b",handle:"destination",name:"Destination",reachable:true}]};
  return {ctx,sent};
}

test("VM copy destination selection preserves mode through the cluster switch",async()=>{
  const t=setup();t.ctx.moveToCluster("vm","desktop","source","copy","guests");
  assert.match(t.ctx.title,/Copy to cluster/);
  assert.match(t.ctx.html,/moveToClusterGo\("b","source","vm","desktop","copy","guests"\)/);
  await t.ctx.moveToClusterGo("b","source","vm","desktop","copy","guests");
  assert.equal(t.sent[0].path,"/api/move/hello");
  assert.equal(t.sent[0].headers["X-Homestead-Cluster"],"b");
  assert.equal(t.sent[1].body.id,"b");
  const url=new URL(t.ctx.location.href,"https://example.invalid");
  assert.equal(url.searchParams.get("transfer_mode"),"copy");
  assert.equal(url.searchParams.get("move"),"source:vm:desktop");
  assert.equal(url.searchParams.get("source_namespace"),"guests");
});

test("copying never navigates to an older destination's move screen",async()=>{
  const t=setup(),notices=[];
  t.ctx.toast=(message)=>notices.push(message);
  t.ctx.api=async(path,opts)=>{t.sent.push({path,headers:opts.headers});return {protocol:1};};
  await t.ctx.moveToClusterGo("b","source","vm","desktop","copy","guests");
  assert.equal(t.ctx.location.href,"");
  assert.equal(t.sent.length,1);
  assert.equal(t.sent[0].path,"/api/move/hello");
  assert.match(notices[0],/Update Homestead on the destination/);
});

test("arrival opens a copy review while old move links still open a move",()=>{
  for(const mode of ["copy","move"]){
    const t=setup(),calls=[];t.ctx.moveReview=(...args)=>calls.push(args);
    t.ctx.location.search="?move=source%3Avm%3Adesktop"+(mode==="copy"?"&transfer_mode=copy&source_namespace=guests":"");
    t.ctx.fleetPendingMove();
    assert.deepEqual(calls[0],["source","vm","desktop",mode,mode==="copy"?"guests":""]);
  }
});
