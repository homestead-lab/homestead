"use strict";
const test = require("node:test"), assert = require("node:assert/strict");
const fs = require("node:fs"), vm = require("node:vm");
function fixture() {
  const frames = new Map(); let id = 0;
  const ctx = { SET:{motion:"on"}, document:{hidden:false}, reduced:false,
    esc:String, requestAnimationFrame:fn=>{frames.set(++id,fn);return id}, cancelAnimationFrame:id=>frames.delete(id) };
  ctx.window=ctx;ctx.matchMedia=()=>({matches:ctx.reduced});vm.createContext(ctx);
  const core=fs.readFileSync("web/js/core.js","utf8");
  vm.runInContext(core.slice(core.indexOf("const sparkAnimations"),core.indexOf("const POL")),ctx);
  const step=now=>{const callbacks=[...frames.values()];frames.clear();callbacks.forEach(fn=>fn(now))};
  const svg={sparkWindow:{before:[1,2,3],after:[2,3,4]},getAttribute:()=>"0 0 300 74"};
  const path={d:ctx.sparkPath([[0,10],[150,20],[300,30]]),isConnected:true,closest:()=>svg,
    classList:{contains:()=>false},getAttribute(){return this.d},setAttribute(_,value){this.d=value}};
  return {ctx,frames,step,svg,path};
}
test("rolling observations retain old coordinates and move exactly one sample left",()=>{
  const {ctx}=fixture(),before=[[0,10],[150,20],[300,30]],after=[[0,20],[150,30],[300,40]];
  const frames=ctx.sparkFrames(before,after,{before:[1,2,3],after:[2,3,4]},300);
  assert.deepEqual(JSON.parse(JSON.stringify(frames.from)),[...before,[450,30]]);
  assert.deepEqual(JSON.parse(JSON.stringify(frames.to)),[[-150,10],...after]);
  assert.equal(ctx.sparkFrames(before,after,{before:[1,2,3],after:[5,6,7]},300),null);
});
test("a growing history retains observed samples instead of stretching a fabricated sample",()=>{
  const {ctx}=fixture();const frames=ctx.sparkFrames([[0,10],[300,20]],[[0,10],[150,20],[300,30]],{before:[1,2],after:[1,2,3]},300);
  assert.deepEqual(JSON.parse(JSON.stringify(frames.from)),[[0,10],[300,20],[600,20]]);
  assert.deepEqual(JSON.parse(JSON.stringify(frames.to)),[[0,10],[150,20],[300,30]]);
});
test("a path moves through intermediate observations and settles at the exact target",()=>{
  const {ctx,path,step,frames}=fixture(),target=ctx.sparkPath([[0,20],[150,30],[300,40]]);
  ctx.updateSparkPath(path,target);step(0);const first=path.d;step(325);
  assert.notEqual(path.d,first);assert.notEqual(path.d,target);assert.doesNotMatch(path.d,/NaN|Infinity/);
  step(650);assert.equal(path.d,target);assert.equal(frames.size,0);
});
test("an unchanged refresh does not restart a chart animation",()=>{
  const {ctx,path,step,frames}=fixture(),target=ctx.sparkPath([[0,20],[150,30],[300,40]]);
  ctx.updateSparkPath(path,target);step(0);ctx.updateSparkPath(path,target);step(650);
  assert.equal(path.d,target);assert.equal(frames.size,0);
});
test("disabled motion, reduced motion, hidden pages and changed ranges update immediately",()=>{
  for(const kind of ["off","reduced","hidden","range"]){
    const {ctx,path,svg,frames}=fixture();
    if(kind==="off")ctx.SET.motion="off";if(kind==="reduced")ctx.reduced=true;
    if(kind==="hidden")ctx.document.hidden=true;if(kind==="range")svg.sparkReset=true;
    const target=ctx.sparkPath([[0,20],[150,30],[300,40]]);ctx.updateSparkPath(path,target);
    assert.equal(path.d,target,kind);assert.equal(frames.size,0,kind);
  }
});
test("a newer target cancels old frames, and detached charts stop scheduling work",()=>{
  const {ctx,path,step,frames}=fixture();ctx.updateSparkPath(path,ctx.sparkPath([[0,20],[150,30],[300,40]]));
  ctx.updateSparkPath(path,ctx.sparkPath([[0,25],[150,35],[300,45]]));assert.ok(frames.size<=1);
  path.isConnected=false;step(1000);assert.equal(frames.size,0);
});
test("a late history response cannot overwrite a new range or a different page",async()=>{
  const core=fs.readFileSync("web/js/views-stats.js","utf8");const ctx={AbortController,setTimeout,clearTimeout,STATE:{data:{}},host:{},api:()=>new Promise(resolve=>ctx.resolve=resolve),historyRange:()=>ctx.range,$:()=>ctx.host};
  vm.createContext(ctx);vm.runInContext(core.slice(core.indexOf("let historyRequest"),core.indexOf("window.historyPaint")),ctx);
  ctx.range="24h";let pending=ctx.historyPaint();ctx.range="7d";ctx.resolve({});await pending;
  assert.deepEqual(ctx.STATE.data,{});
  ctx.range="24h";pending=ctx.historyPaint();ctx.host={};ctx.resolve({});await pending;assert.deepEqual(ctx.STATE.data,{});
});

test("dense history refreshes retain their exact geometry without scheduling heavy frame work",()=>{
  const {ctx,path,frames}=fixture();path.d=ctx.sparkPath(Array.from({length:2160},(_,i)=>[i,10]));
  const target=ctx.sparkPath(Array.from({length:2160},(_,i)=>[i,20]));ctx.updateSparkPath(path,target);
  assert.equal(path.d,target);assert.equal(frames.size,0);
});

test("an older history request cannot repaint over a newer observation of the same range",async()=>{
  const requests=[],painted=[],ctx={AbortController,setTimeout,clearTimeout,STATE:{data:{}},host:{},historyRange:()=>"24h",esc:String,jsq:JSON.stringify,
    document:{createElement:()=>({})},morph:(_,next)=>painted.push(next.innerHTML),api:()=>new Promise(resolve=>requests.push(resolve))};
  ctx.$=()=>ctx.host;vm.createContext(ctx);
  const code=fs.readFileSync("web/js/views-stats.js","utf8");vm.runInContext(code.slice(code.indexOf("let historyRequest"),code.indexOf("window.historyPaint")),ctx);
  const older=ctx.historyPaint(),newer=ctx.historyPaint();requests[1]({samples:0});await newer;
  const latest=ctx.STATE.data.historyHtml;requests[0]({samples:2});await older;
  assert.equal(ctx.STATE.data.historyHtml,latest);assert.equal(painted.length,1);
});

test("single observed CPU and RAM samples both render without inventing zero-valued data",()=>{
  const {ctx}=fixture();const html=ctx.dualSpark([20],[80]);
  assert.match(html,/class="ln s1"/);assert.match(html,/class="ln s2"/);
  assert.equal(html,ctx.dualSpark([20,20],[80,80]));
});

test("a timed out history request retains existing charts and shows an error",async()=>{
  const note={},host={querySelector:()=>null,appendChild:el=>{host.note=el}};
  let timeout,cleared=false;
  const ctx={AbortController,setTimeout:fn=>{timeout=fn;return 1},clearTimeout:()=>{cleared=true},
    STATE:{data:{historyHtml:"saved charts"}},historyRange:()=>"24h",$:()=>host,
    document:{createElement:()=>note},api:(_path,opts)=>new Promise((_resolve,reject)=>{
      opts.signal.addEventListener("abort",()=>reject(new Error("timeout")));})};
  vm.createContext(ctx);const code=fs.readFileSync("web/js/views-stats.js","utf8");
  vm.runInContext(code.slice(code.indexOf("let historyRequest"),code.indexOf("window.historyPaint")),ctx);
  const pending=ctx.historyPaint();timeout();await pending;
  assert.equal(ctx.STATE.data.historyHtml,"saved charts");assert.match(host.note.textContent,/could not refresh/);
  assert.equal(cleared,true);
});
