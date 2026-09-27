"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");

function setup({blocked=false, missing=false, fail=false}={}) {
  const calls=[], fields={"#importConfirm":{checked:false}, "#importGo":{}};
  const ctx={console, URLSearchParams, Map, Set, Date, Promise, encodeURIComponent,
    document:{addEventListener(){}}, STATE:{data:{}},
    UI:{more:(title,body)=>`<details><summary>${title}</summary>${body}</details>`},
    $:key=>fields[key], $$:()=>[], esc:value=>String(value).replaceAll("<","&lt;"),
    toast:()=>{}, closeModal:()=>{}, resetPaint:()=>{}, viewImport:()=>{},
    deployCapacityHtml:plan=>`<div>${plan.name}</div>`,
    childModal:(_title,body)=>{fields.html=body;},
    api:async(path,opts)=>{
      calls.push({path,body:JSON.parse(opts.body)});
      if(path.endsWith("/preview")) return {capacity:missing?null:{blocked,warnings:["File replacement"]},
        capacity_token:"signed",volumes:[{name:"data",create:false,access_mode:"ReadWriteMany",storage_class:"storage"}],
        phases:[{title:"Copy files",capacity:{name:"copy"}},{title:"Imported application",capacity:{name:"app"}}]};
      if(fail) throw Error("Lost connection");
      return {job:"homestead-import-app"};
    }};
  ctx.window=ctx; vm.createContext(ctx);
  vm.runInContext(fs.readFileSync("web/js/views-lifecycle.js","utf8"),ctx);
  ctx.viewImport=()=>{};
  return {ctx,calls,fields};
}

test("import shows both phases and reused claims; submits exact reviewed token once",async()=>{
  const t=setup(), body={name:"app",image:"example/app:1",memory_limit:"1Gi"};
  await t.ctx.importReview(body);
  assert.match(t.fields.html,/Copy files/); assert.match(t.fields.html,/Imported application/);
  assert.match(t.fields.html,/Reuse volume/);
  await t.ctx.confirmImport(); assert.equal(t.calls.length,1);
  t.fields["#importConfirm"].checked=true;
  await t.ctx.confirmImport(); await t.ctx.confirmImport();
  assert.equal(t.calls.length,2);
  assert.equal(t.calls[1].body.capacity_token,"signed");
  assert.equal(t.calls[1].body.confirm_capacity,true);
  assert.equal(t.calls[1].body.memory_limit,"1Gi");
});
test("missing and blocked plans cannot be forced through",async()=>{
  for(const opts of [{blocked:true},{missing:true}]) {
    const t=setup(opts); await t.ctx.importReview({name:"app"});
    t.fields["#importConfirm"].checked=true; await t.ctx.confirmImport();
    assert.equal(t.calls.length,1);
  }
});
test("uncertain submission consumes approval and requires a new preview",async()=>{
  const t=setup({fail:true}); await t.ctx.importReview({name:"app"});
  t.fields["#importConfirm"].checked=true; await t.ctx.confirmImport(); await t.ctx.confirmImport();
  assert.equal(t.calls.length,2); assert.equal(t.fields["#importGo"].textContent,"Review again");
  await t.fields["#importGo"].onclick();
  assert.equal(t.calls[2].path,"/api/import/preview");
});

test("import submission shows its durable job and refreshes history after lost responses",async()=>{
  for (const fail of [false,true]) {
    const t=setup({fail}), jobs=[];let refreshed=0;
    const api=t.ctx.api;
    t.ctx.api=async(...args)=>{const result=await api(...args);return {...result,operation:{id:'durable-import'}};};
    t.ctx.noteOperation=job=>jobs.push(job);t.ctx.refreshOperations=()=>refreshed++;
    await t.ctx.importReview({name:'app'});t.fields['#importConfirm'].checked=true;
    await t.ctx.confirmImport();
    assert.equal(refreshed,1);assert.equal(jobs.length,fail?0:1);
  }
});

test("copy review explains unknown space estimates and retained partial copies",async()=>{
  const t=setup();
  await t.ctx.importReview({name:'app',mappings:[{remote_path:'/data',mount_path:'/config'}]});
  assert.match(t.fields.html,/Copy safety checks/);
  assert.match(t.fields.html,/Missing measurements/);
  assert.match(t.fields.html,/failed transfers keep both copies/);
});
