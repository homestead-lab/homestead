"use strict";
const test = require("node:test"), assert = require("node:assert/strict"), fs = require("node:fs"), vm = require("node:vm");

function setup(admin=true) {
  const host={}, calls=[], noted=[];
  const ctx={console, STATE:{data:{}}, $:key=>key==="#hostConsoles"?host:null, esc:String,
    can:()=>admin, toast:()=>{}, noteOperation:o=>noted.push(o),
    api:async(path,opts)=> {
      calls.push({path,body:opts?JSON.parse(opts.body):null});
      return opts?{operation:{id:"job"}}:{version:"2.8.245",nodes:[
        {name:"node1",ready:true,detail:"update available"}, {name:"node2",ready:false},
        {name:"native",ready:true,native:true}]};
    }};
  ctx.window=ctx;ctx.jsq=JSON.stringify; vm.createContext(ctx);
  vm.runInContext(fs.readFileSync("web/js/views-settings.js","utf8"),ctx);
  return {ctx,host,calls,noted};
}

test("administrator can queue bundled console update from settings",async()=>{
  const t=setup(); await t.ctx.loadHostConsoles();
  assert.match(t.host.innerHTML,/v2.8.245/);
  assert.match(t.host.innerHTML,/Install \/ update/);
  assert.match(t.host.innerHTML,/disabled onclick="hostConsoleAction\("node2"/);
  assert.match(t.host.innerHTML,/Harvester console/);
  const button={}; await t.ctx.hostConsoleAction("node1","enable",button);
  assert.deepEqual(t.calls[1].body,{node:"node1",action:"enable"});
  assert.equal(t.noted[0].id,"job"); assert.equal(button.disabled,false);
});

test("non-administrator is shown instructions without calling privileged API",async()=>{
  const t=setup(false); await t.ctx.loadHostConsoles();
  assert.equal(t.calls.length,0); assert.match(t.host.innerHTML,/administrator manages/);
});
