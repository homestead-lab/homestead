"use strict";
const test = require("node:test"), assert = require("node:assert/strict");
const fs = require("node:fs"), vm = require("node:vm");
const core = fs.readFileSync("web/js/core.js", "utf8");
function context() {
  const c = {document:{addEventListener(){},querySelector(){return null}}, FLEET:{view:{}}, icon:()=>"<svg class=\"btnicon\"></svg>",
    appAvatar:()=>"", clusterAttr:r=>r.site?` data-cluster=\"${r.site.id}\"`:"", clusterTag:()=>"",remoteRow:r=>!!r.site&&!r.site.self,
    platformTag:()=>"",hardwareTags:()=>"",imageLabel:String, fmtUp:String, svcUrl:()=>"",fmtAgo:String,
    ratePair:()=>["0","Mb/s"],sizePair:()=>"0/10 GB",sizeText:()=>"10 GB",meter:()=>"",sev:()=>""};
  c.window=c;vm.createContext(c);
  vm.runInContext(core.slice(0,core.indexOf("/* Put text on the clipboard.")),c);
  for(const file of ["metrics-utils.js","ui.js","uptime.js","views-workloads.js","views-overview.js"])
    vm.runInContext(fs.readFileSync(`web/js/${file}`,"utf8"),c);
  vm.runInContext("this.workloadRowKey=workloadRowKey;this.nodeComparisonKey=nodeComparisonKey",c);
  return c;
}
const workload = (site,name="example-app") => ({site,name,ns:"apps",desired:1,ready:1,cpu:.2,mem_mb:128,images:["example/image:stable"],nodes:["host-a"],pods:[],ports:[],hardware:[]});
test("inline expansion belongs to a workload identity including its cluster",()=>{
  const c=context(),a=workload({id:"cluster-a",self:true}),b=workload({id:"cluster-b"});
  assert.notEqual(c.workloadRowKey(a),c.workloadRowKey(b));
  vm.runInContext(`workloadExpanded.add(${JSON.stringify(c.workloadRowKey(a))})`,c);
  const html=c.workloadTableRows([b,a]);
  assert.match(html,/data-cluster="cluster-b" hidden/);
  assert.match(html,/class="wl-row wl-expanded"[^>]*data-cluster="cluster-a"/);
  assert.equal((html.match(/aria-expanded="true"/g)||[]).length,1);
  assert.match(c.workloadTableRows([a]),/aria-expanded="true"/,"refresh keeps it open");
});
test("collapsed rows keep failure and update signals, remote rows omit local update records",()=>{
  const c=context(),a=workload(),remote=workload({id:"cluster-b"});
  vm.runInContext('STATE.data.imageUpdateMap={"apps/example-app":{available:true,images:[{error:"Registry unreachable"}]}}',c);
  a.pods=[{name:"pod-a",unplaced:"Insufficient memory"}];
  const html=c.workloadTableRows([a]);
  const summary=html.split('<tr class="wl-detail-row"')[0];
  assert.match(summary,/>Update</);assert.match(summary,/Registry unreachable/);assert.match(summary,/>Blocked</);
  assert.doesNotMatch(c.workloadTableRows([remote]),/>Update<|Registry unreachable/);
});
test("detail values and identity controls escape hostile names and image references",()=>{
  const c=context(),a=workload(null,'<img src=x onerror="bad()">');a.images=['<script>bad()</script>'];
  const html=c.workloadTableRows([a]);
  assert.doesNotMatch(html,/<img src=x|<script>bad/);
  assert.match(html,/&lt;script&gt;bad\(\)&lt;\/script&gt;/);
});
test("node comparison keeps one scrollable matrix and selection across refresh",()=>{
  const c=context(),nodes=Array.from({length:6},(_,i)=>({name:`compute-host-${i+1}`,status:"Ready",cpu_pct:0,cpu_cap:4,mem_pct:20,mem_used_gb:2,mem_cap_gb:10,workloads:[],disks:[]}));
  const html=c.nodeComparison(nodes,"test");
  assert.equal((html.match(/class="tbl stack comparison-table"/g)||[]).length,1);
  assert.match(html,/6 nodes · scroll to compare/);
  assert.equal(c.nodeComparisonLabel(nodes,nodes[3].name),"4");
  vm.runInContext(`STATE.nodeCompareSelections["test-0"]=${JSON.stringify(c.nodeComparisonKey(nodes[3]))}`,c);
  assert.match(c.nodeComparison(nodes,"test"),/aria-pressed="true"[^>]*aria-label="Select compute-host-4"/);
  assert.doesNotMatch(c.nodeComparison(nodes.slice(0,2),"test"),/aria-label="Select compute-host-4"/);
});
test("sorting keeps detail rows next to their parents, including empty values and stable ties",()=>{
  const c={};vm.createContext(c);
  vm.runInContext(core.slice(core.indexOf('const SORT_UNITS'),core.indexOf('function sortRows(')),c);
  vm.runInContext(core.slice(core.indexOf('function sortBody('),core.indexOf('/* On a phone a stacked table')),c);
  const row=(key,value)=>({dataset:{rowKey:key},cells:[{dataset:{sort:value},textContent:value}]});
  const detail=key=>({dataset:{detailFor:key},cells:[{},{}]});
  const a=row("a","2"),b=row("b","1"),d=row("d","1"),empty=row("empty",""),da=detail("a"),db=detail("b"),dd=detail("d"),de=detail("empty");
  const body={rows:[a,da,b,db,d,dd,empty,de],appendChild(row){this.rows.splice(this.rows.indexOf(row),1);this.rows.push(row)}};
  c.sortBody(body,[{}],{col:0,dir:1});assert.deepEqual(body.rows,[b,db,d,dd,a,da,empty,de]);
  c.sortBody(body,[{}],{col:0,dir:-1});assert.deepEqual(body.rows,[a,da,b,db,d,dd,empty,de]);
});

test("a remote node never borrows local uptime history under the same name",()=>{
  const c=context();
  vm.runInContext('STATE.data.uptime={nodes:{"example-host":{windows:{"30d":100},days:[{day:1,up:100}]}}}',c);
  assert.match(c.nodeUptimeStrip({name:"example-host"}),/uptime-ok/);
  const remote=c.nodeUptimeStrip({name:"example-host",site:{id:"secondary-example"}});
  assert.doesNotMatch(remote,/uptime-ok/);assert.match(remote,/its cluster’s uptime history/);
});
test("a Longhorn drive without a device name still has a readable identity",()=>{
  const c=context();
  assert.match(c.comparisonDisk({}, {device:"",lh_size_gb:100,size_gb:200}),/Longhorn filesystem/);
});
