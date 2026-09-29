"use strict";
const test = require("node:test"), assert = require("node:assert/strict"), fs = require("node:fs"), vm = require("node:vm");

function setup() {
  const fields = {"#mv_ns": {value:"lab"}, "#mv_mode": {value:"shared"},
    "#mv_sc": {value:"fast"}, "#mv_plan": {}, "#mv_go": {disabled:true}};
  const calls=[], replies=[];
  const ctx = {console, URLSearchParams, Map, Set, Date, Promise, encodeURIComponent, clearTimeout,
    document:{addEventListener(){}}, STATE:{data:{}}, $:key=>fields[key], $$:()=>[],
    esc:String, toast:()=>{}, ask:async()=>true, closeModal:()=>{}, movesRepaint:()=>{},
    api:(path,opts)=> { calls.push({path,body:JSON.parse(opts.body)}); return new Promise(resolve=>replies.push(resolve)); }};
  ctx.window=ctx; vm.createContext(ctx); ctx.jsq=JSON.stringify;
  vm.runInContext(fs.readFileSync("web/js/views-lifecycle.js","utf8"),ctx);
  return {ctx,fields,calls,replies};
}

test("review and submission carry the chosen destination storage class",async()=>{
  const t=setup(), pending=t.ctx.movePlan("source","container","camera-app");
  assert.equal(t.calls[0].body.storage_class,"fast");
  t.replies.shift()({ok:true,storage_class:"fast",storage_classes:["fast","longhorn-r2"],
    claims:[{claim:"app-data",size_gb:10}],total_gb:10,joined:true,will_run:true});
  await pending;
  assert.match(t.fields["#mv_sc"].innerHTML, /value="fast" selected/);
  assert.match(t.fields["#mv_plan"].innerHTML, /Storage class: fast/);
  assert.equal(t.fields["#mv_go"].disabled,false);
  t.fields["#mv_sc"].value="longhorn-r2";
  t.fields["#mv_sc"].onchange();
  assert.equal(t.fields["#mv_go"].disabled,true);
  assert.equal(t.calls[1].body.storage_class,"longhorn-r2");
  t.replies.shift()({ok:true,storage_class:"longhorn-r2",storage_classes:["fast","longhorn-r2"]});
  await new Promise(resolve=>setImmediate(resolve));
  const starting=t.ctx.moveStart("source","container","camera-app");
  await new Promise(resolve=>setImmediate(resolve));
  assert.equal(t.calls[2].body.storage_class,"longhorn-r2");
  t.replies.shift()({}); await starting;
});

test("late plan response cannot replace the newer choice or enable a blocked move",async()=>{
  const t=setup(), first=t.ctx.movePlan("source","container","camera-app");
  t.fields["#mv_sc"].value="longhorn-r2";
  const second=t.ctx.movePlan("source","container","camera-app");
  t.replies[1]({ok:false,storage_class:"longhorn-r2",storage_classes:["fast","longhorn-r2"],blockers:["No capacity"]});
  await second;
  t.replies[0]({ok:true,storage_class:"fast",storage_classes:["fast","longhorn-r2"]});
  await first;
  assert.equal(t.fields["#mv_go"].disabled,true);
  assert.match(t.fields["#mv_plan"].innerHTML,/No capacity/);
  assert.equal(t.fields["#mv_sc"].value,"longhorn-r2");
});
