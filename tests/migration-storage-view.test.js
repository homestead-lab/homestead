"use strict";
const test = require("node:test"), assert = require("node:assert/strict"), fs = require("node:fs"), vm = require("node:vm");

function setup() {
  const fields = {"#mv_ns": {value:"lab"}, "#mv_mode": {value:"shared"},
    "#mv_sc": {value:"fast"}, "#mv_plan": {}, "#mv_go": {disabled:true}};
  const calls=[], replies=[];
  const ctx = {console, URLSearchParams, Map, Set, Date, Promise, encodeURIComponent, clearTimeout,
    document:{addEventListener(){}}, STATE:{data:{}}, $:key=>fields[key], $$:()=>[],
    esc:String, toast:()=>{}, ask:async()=>true, closeModal:()=>{}, movesRepaint:()=>{}, tip:t=>t,
    childModal:(title,html)=>{ctx.modalTitle=title;ctx.modalHtml=html;},
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
  assert.match(t.fields["#mv_plan"].innerHTML, /id="mv_volsc_app-data"[^]*value="fast" selected/);
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

test("VM copy review and start retain copy mode and storage selection", async () => {
  const t = setup();
  t.ctx.moveReview("source", "vm", "desktop", "copy", "guests");
  assert.match(t.ctx.modalTitle, /^Copy desktop/);
  assert.match(t.ctx.modalHtml, /Creates a stopped copy here/);
  assert.match(t.ctx.modalHtml, /Start copy/);
  assert.equal(t.calls[0].body.transfer_mode, "copy");
  assert.equal(t.calls[0].body.source_namespace, "guests");
  t.replies.shift()({ok:true,transfer_mode:"copy",storage_class:"fast",storage_classes:["fast"],
    claims:[{claim:"os-disk",size_gb:40,volume_mode:"Block"}],total_gb:40,will_run:false});
  await new Promise(resolve=>setImmediate(resolve));
  assert.match(t.fields["#mv_plan"].innerHTML, /Copy its data/);
  assert.match(t.fields["#mv_plan"].innerHTML, /Ready to copy/);
  const start=t.ctx.moveStart("source","vm","desktop");
  await new Promise(resolve=>setImmediate(resolve));
  assert.equal(t.calls[1].body.transfer_mode,"copy");
  assert.equal(t.calls[1].body.source_namespace,"guests");
  assert.equal(t.calls[1].body.storage_class,"fast");
  t.replies.shift()({}); await start;
});

test("a destination without copy support cannot enable Start copy", async () => {
  const t=setup(); t.ctx.moveReview("source","vm","desktop","copy");
  t.replies.shift()({ok:true,will_run:true,claims:[]});
  await new Promise(resolve=>setImmediate(resolve));
  assert.equal(t.fields["#mv_go"].disabled,true);
  assert.match(t.fields["#mv_plan"].innerHTML,/Update Homestead on this destination/);
});

test("copy activity has cleanup actions and never offers source removal", () => {
  const t=setup();
  const html=t.ctx.movesHtml([{id:"copy-job",name:"desktop",cluster:"source",kind:"vm",namespace:"lab",
    transfer_mode:"copy",status:"succeeded",phases:["releasing-source","starting","done"],phase_index:2,
    progress:100,source_stopped:false,created_at:new Date().toISOString()}]);
  assert.match(html,/Resume source/); assert.match(html,/Keep copy stopped/);
  assert.match(html,/Remove copy/); assert.doesNotMatch(html,/moveFinish\(|Put back|keeps its stopped copy/);
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

test("each volume can be moved, made blank at a size, or skipped, with its own class", async () => {
  const t = setup(), pending = t.ctx.movePlan("source", "container", "camera-app");
  t.replies.shift()({ ok: true, storage_class: "fast", storage_classes: ["fast"], all_storage_classes: ["fast", "local-path"],
    claims: [{ claim: "app-data", size_gb: 10, action: "move", storage_class: "fast" },
             { claim: "app-cache", size_gb: 20, action: "blank", storage_class: "fast", target_size_gb: 20 }],
    total_gb: 10, joined: true, will_run: true });
  await pending;
  const html = t.fields["#mv_plan"].innerHTML;
  assert.match(html, /id="mv_vol_app-cache"[^]*value="blank" selected/);
  assert.match(html, /id="mv_volsize_app-cache"[^>]*value="20"/, "a blank volume's size can be chosen");
  assert.doesNotMatch(html, /id="mv_volsize_app-data"/, "a moved volume keeps its size");
  assert.match(html, /local-path/, "a blank volume may use any class");
  t.ctx.moveVolumeSet("source", "container", "camera-app", "app-cache", "size_gb", 5);
  t.ctx.moveVolumeSet("source", "container", "camera-app", "app-cache", "storage_class", "local-path");
  t.ctx.moveVolumeSet("source", "container", "camera-app", "app-data", "action", "skip");
  assert.deepEqual(JSON.parse(JSON.stringify(t.calls.at(-1).body.volumes)),
    { "app-cache": { size_gb: 5, storage_class: "local-path" }, "app-data": { action: "skip" } });
});
