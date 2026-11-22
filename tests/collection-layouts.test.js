"use strict";
const test = require("node:test"), assert = require("node:assert/strict");
const fs = require("node:fs"), vm = require("node:vm");
const between = require("./helpers/between");
const core = require("./helpers/core-source")();
function context() {
  const c = {document:{addEventListener(){},querySelector(){return null}}, FLEET:{view:{}}, icon:()=>"<svg class=\"btnicon\"></svg>", HOVER_CARDS:{}, DETAIL_KINDS:{},
    appAvatar:()=>"", clusterAttr:r=>r.site?` data-cluster=\"${r.site.id}\"`:"", clusterTag:()=>"",remoteRow:r=>!!r.site&&!r.site.self,
    platformTag:()=>"",hardwareTags:()=>"",imageLabel:String, fmtUp:String, svcUrl:()=>"",fmtAgo:String,
    ratePair:()=>["0","Mb/s"],sizePair:()=>"0/10 GB",sizeText:()=>"10 GB",meter:()=>"",sev:()=>""};
  c.window=c;vm.createContext(c);require("./helpers/core-text")(c);
  require("./helpers/load-time")(c);
  vm.runInContext(between(core,null,"/* Put text on the clipboard."),c);
  for(const file of ["metrics-utils.js","ui.js","uptime.js","autoupdate.js","schedules.js","gitops.js",...require("./helpers/workloads-source").names(),"views-overview.js"])
    vm.runInContext(fs.readFileSync(`web/js/${file}`,"utf8"),c);
  vm.runInContext("this.workloadRowKey=workloadRowKey;this.nodeComparisonKey=nodeComparisonKey",c);
  return c;
}
const workload = (site,name="example-app") => ({site,name,ns:"apps",desired:1,ready:1,cpu:.2,mem_mb:128,images:["example/image:stable"],nodes:["host-a"],pods:[],ports:[],hardware:[]});
test("a row opens the details of its own workload, its cluster included",()=>{
  const c=context(),a=workload({id:"cluster-a",self:true}),b=workload({id:"cluster-b"});
  assert.notEqual(c.workloadRowKey(a),c.workloadRowKey(b));
  const html=c.workloadTableRows([b,a]);
  assert.match(html,/openDetails\('app',&quot;\[[^)]*cluster-b[^)]*\)/);
  assert.match(html,/openDetails\('app',&quot;\[[^)]*cluster-a[^)]*\)/);
  assert.match(html,/data-card="app\|\[&quot;cluster-a&quot;/,"the name's hover card names the same app");
  assert.doesNotMatch(html,/wl-detail-row|aria-expanded/,"no row opens in the list");
  vm.runInContext(`STATE.data.wl=${JSON.stringify([a,b])}`,c);
  assert.equal(vm.runInContext(`workloadByKey(${JSON.stringify(c.workloadRowKey(b))}).site.id`,c),"cluster-b");
});
test("collapsed rows keep failure and update signals, remote rows omit local update records",()=>{
  const c=context(),a=workload(),remote=workload({id:"cluster-b"});
  vm.runInContext('STATE.data.imageUpdateMap={"apps/example-app":{available:true,images:[{error:"Registry unreachable"}]}}',c);
  a.pods=[{name:"pod-a",unplaced:"Insufficient memory"}];
  const html=c.workloadTableRows([a]);
  assert.match(html,/>Update image</);assert.match(html,/Registry unreachable/);assert.match(html,/>Blocked</);
  assert.match(html,/class="wl-row wl-problem bad"/,"a problem colours the row's edge");
  assert.match(html,/>Blocked<\/span><span class="wl-reason xs">Cannot start: Insufficient memory<\/span>/,"and says why beside its chip");
  assert.doesNotMatch(c.workloadTableRows([remote]),/>Update image<|Registry unreachable/);
});
test("detail values and identity controls escape hostile names and image references",()=>{
  const c=context(),a=workload(null,'<img src=x onerror="bad()">');a.images=['<script>bad()</script>'];
  vm.runInContext(`STATE.data.wl=${JSON.stringify([a])}`,c);
  for(const html of [c.workloadTableRows([a]),c.workloadOverview(c.workloadRowKey(a)),c.workloadSettingsTab(c.workloadRowKey(a))])assert.doesNotMatch(html,/<img src=x|<script>bad/);
  assert.match(c.workloadSettingsTab(c.workloadRowKey(a)),/&lt;script&gt;bad\(\)&lt;\/script&gt;/);
});
test("node comparison keeps one scrollable matrix and selection across refresh",()=>{
  const c=context(),nodes=Array.from({length:6},(_,i)=>({name:`compute-host-${i+1}`,status:"Ready",cpu_pct:0,cpu_cap:4,mem_pct:20,mem_used_gb:2,mem_cap_gb:10,workloads:[],disks:[]}));
  const html=c.nodeComparison(nodes,"test");
  assert.equal((html.match(/class="tbl stack comparison-table"/g)||[]).length,1);
  assert.match(html,/6 hosts · scroll to compare/);
  assert.equal(c.nodeComparisonLabel(nodes,nodes[3].name),"4");
  vm.runInContext(`STATE.nodeCompareSelections["test-0"]=${JSON.stringify(c.nodeComparisonKey(nodes[3]))}`,c);
  assert.match(c.nodeComparison(nodes,"test"),/aria-pressed="true"[^>]*aria-label="Select compute-host-4"/);
  assert.doesNotMatch(c.nodeComparison(nodes.slice(0,2),"test"),/aria-label="Select compute-host-4"/);
});
test("sorting keeps detail rows next to their parents, including empty values and stable ties",()=>{
  const c={};vm.createContext(c);require("./helpers/core-text")(c);
  vm.runInContext(between(core,'const SORT_UNITS','function sortRows('),c);
  vm.runInContext(between(core,'function sortBody(','/* On a phone a stacked table'),c);
  const row=(key,value)=>({dataset:{rowKey:key},cells:[{dataset:{sort:value},textContent:value}]});
  const detail=key=>({dataset:{detailFor:key},cells:[{},{}]});
  const a=row("a","2"),b=row("b","1"),d=row("d","1"),empty=row("empty",""),da=detail("a"),db=detail("b"),dd=detail("d"),de=detail("empty");
  const body={rows:[a,da,b,db,d,dd,empty,de],appendChild(row){this.rows.splice(this.rows.indexOf(row),1);this.rows.push(row)}};
  c.sortBody(body,[{}],{col:0,dir:1});assert.deepEqual(body.rows,[b,db,d,dd,a,da,empty,de]);
  c.sortBody(body,[{}],{col:0,dir:-1});assert.deepEqual(body.rows,[a,da,b,db,d,dd,empty,de]);
});

test("a remote node never borrows local uptime history under the same name",()=>{
  const c=context();
  vm.runInContext('STATE.data.nodeUptime={nodes:{"example-host":{windows:{"30d":100},days:[{day:1,up:100}]}}}',c);
  assert.match(c.nodeUptimeStrip({name:"example-host"}),/uptime-ok/);
  const remote=c.nodeUptimeStrip({name:"example-host",site:{id:"secondary-example"}});
  assert.doesNotMatch(remote,/uptime-ok/);assert.match(remote,/its cluster’s uptime history/);
});
test("a Longhorn drive without a device name still has a readable identity",()=>{
  const c=context();
  assert.match(c.comparisonDisk({}, {device:"",lh_size_gb:100,size_gb:200}),/Longhorn filesystem/);
});
test("rows and cards show the same tags, scheduled included, and name an update the same way",()=>{
  const c=context(),a=workload();a.schedule={start:"0 7 * * *",stop:"0 23 * * *"};
  vm.runInContext('STATE.data.imageUpdateMap={"apps/example-app":{available:true,images:[{}]}}',c);
  const row=c.workloadTableRows([a]),card=c.workloadCard(a);
  for(const html of [row,card])assert.match(html,/sched-tag/,"scheduled shows in both views");
  assert.match(card,/update available/);
  assert.doesNotMatch(row,/class="ui-chip warn">(Update|update available)</,"a row says it once, with its Update button");
  assert.match(row,/aria-label="Update image example-app"/);
  vm.runInContext('this.can=()=>false',c);
  assert.match(c.workloadTableRows([a]),/update available/,"someone who can't update still sees it");
});
test("a card shows the image's name and tag with the full reference on hover; a row leaves it to the details",()=>{
  const c=context(),a=workload();a.images=["ghcr.io/home-assistant/home-assistant:2026.10.1"];
  const card=c.workloadCard(a);
  assert.match(card,/title="ghcr.io\/home-assistant\/home-assistant:2026.10.1"/);
  assert.match(card,/>home-assistant:2026.10.1</);
  assert.doesNotMatch(c.workloadTableRows([a]),/home-assistant:2026/);
  vm.runInContext(`STATE.data.wl=${JSON.stringify([a])}`,c);
  assert.match(c.workloadSettingsTab(c.workloadRowKey(a)),/ghcr.io\/home-assistant\/home-assistant:2026.10.1/);
});

/* The Containers page in the one-bar design (Nov 2026): a summary line, rows
   with one labelled action, the details sidebar and hover cards. */
test("the summary line counts the apps running normally and links each one that is not, set apart by dots",()=>{
  const c=context(),ok=workload(null,"alpha"),down=workload(null,"paperless"),off=workload(null,"spare");
  off.desired=0;off.ready=0;
  vm.runInContext('STATE.data.uptime={apps:{"apps/paperless":{state:"down",since:0,last:{error:"connection refused"},strip:[]}}}',c);
  const line=c.workloadSummary([ok,down,off],{updateCount:2});
  const wide=line.match(/<span class="sum-wide">([\s\S]*)<\/span><span class="sum-narrow">/)[1];
  assert.match(wide,/^<b>1 of 3 apps is running normally\.<\/b> <button type="button" class="linkish sum-link sum-bad" onclick="openDetails\('app',[^"]*paperless[^"]*\)">paperless is down<\/button>/);
  assert.match(wide,/aria-hidden="true">·<\/span> <span class="sum-note">1 app is stopped<\/span>/);
  assert.match(wide,/·<\/span> <button type="button" class="linkish sum-link sum-info" onclick="imageUpdateCenter\(\)">Updates are ready for 2 apps<\/button>$/);
  // A phone keeps the sentence short and the apps in trouble.
  assert.match(line,/<span class="sum-narrow"><b>1 of 3 apps is running\.<\/b> <button[^>]*>paperless is down<\/button><\/span>$/);
  assert.match(c.workloadSummary([ok]),/<b>The app is running normally\.<\/b><\/span>/);
  assert.match(c.workloadSummary([ok,ok],{q:"al"}),/All 2 apps matching “al” are running normally/);
});
test("a row's status is a chip and a plain reason, or Running with its copies and an update waiting",()=>{
  const c=context(),ok=workload(null,"alpha"),slow=workload(null,"assistant"),many=workload(null,"edge");
  many.desired=3;many.ready=3;
  vm.runInContext('STATE.data.uptime={apps:{"apps/assistant":{state:"slow",since:0,last:{ok:true,ms:2400,code:200},strip:[]}}}',c);
  assert.equal(c.workloadStatusWords(slow,null),'<span class="ui-chip warn slim">Slow</span><span class="wl-reason xs">Responses take 2.4 seconds.</span>');
  assert.equal(c.workloadStatusWords(many,null),'<span class="wl-state dim">Running, 3 copies</span>');
  assert.equal(c.workloadStatusWords(ok,{available:true,images:[]}),'<span class="wl-state dim">Running</span><span class="wl-reason xs">Update ready</span>');
});
test("a row's labelled action fits the app: its update, its logs when in trouble, Start when stopped, else the app",()=>{
  const c=context(),first=w=>(c.workloadRowActions(w,vm.runInContext(`workloadUpdate(${JSON.stringify(w.ns)},${JSON.stringify(w.name)})`,c),w.desired===0).match(/<div class="actionbar"><button[^>]*>(?:<svg[^>]*><\/svg>)?([^<]+)<\/button>/)||[])[1];
  const web=workload(null,"web");web.ports=[{port:8080,ip:"192.0.2.10",primary:true}];
  assert.equal(first(web),"Open app");
  assert.equal(first(workload(null,"worker")),"Open logs","with no address, the logs");
  const off=workload(null,"spare");off.desired=0;off.ready=0;off.ports=web.ports;
  assert.equal(first(off),"Start app");
  const sick={...web,name:"sick",ready:0};
  assert.equal(first(sick),"Open logs");
  vm.runInContext('STATE.data.imageUpdateMap={"apps/web":{available:true,images:[{}]}}',c);
  assert.equal(first(web),"Update image");
});
test("the row's ⋯ menu is grouped; the sidebar's buttons are the main action, Restart and Open app, then every other action",()=>{
  const c=context(),w=workload();
  const row=c.workloadRowActions(w,null,false);
  assert.match(row,/actionmenu-group[^>]*>Use<[\s\S]*actionmenu-group[^>]*>Change<[\s\S]*actionmenu-group[^>]*>Remove<[\s\S]*Delete app…/);
  assert.doesNotMatch(row,/>Monitoring<|>History</,"the rest is in the details sidebar");
  const all=c.workloadDetailActions(w,null,false),labels=all.map(i=>i.label);
  assert.deepEqual([...labels.slice(0,3)],["Open logs","Restart app","Open console"],"with no address, the logs lead and Restart follows");
  const web={...w,ports:[{port:8000,ip:"192.0.2.20",primary:true}]};
  assert.deepEqual([...c.workloadDetailActions(web,null,false).slice(0,3).map(i=>i.label)],["Open app","Restart app","Open logs"]);
  assert.deepEqual([...c.workloadDetailActions({...web,ready:0},null,false).slice(0,3).map(i=>i.label)],["Open logs","Restart app","Open app"],"an app in trouble: the mockup's order");
  assert.deepEqual([...c.workloadDetailActions({...web,desired:0,ready:0},null,true).slice(0,2).map(i=>i.label)],["Start app","Open app"]);
  assert.ok(labels.includes("Monitoring")&&labels.includes("History"));
  assert.equal(new Set(all.map(i=>i.run)).size,all.length,"no action twice");
  assert.equal(all.find(i=>i.label==="Monitoring").group,"More");
  const platform={...w,platform:true};
  assert.deepEqual([...c.workloadDetailActions(platform,null,false).map(i=>i.label)],["Logs","Restart"],"a platform part keeps its short list");
});
test("an app's sidebar and hover cards are built from the list the page read",()=>{
  const c=context(),w=workload(null,"paperless");w.claims=["paperless-data"];w.ports=[{port:8000,ip:"192.0.2.20"}];
  vm.runInContext(`STATE.data.wl=${JSON.stringify([w])}`,c);
  const key=c.workloadRowKey(w);
  assert.equal(c.DETAIL_KINDS.app.head(key).title,"paperless");
  assert.equal(c.DETAIL_KINDS.app.head(key).sub,"Ungrouped · apps · host-a");
  assert.deepEqual([...c.DETAIL_KINDS.app.tabs.map(t=>t.label)],["Overview","Logs","Activity","Settings"]);
  // Overview (the mockup): the address in words, the host, memory and how long it has run, as tiles without icons.
  const overview=c.DETAIL_KINDS.app.tabs[0].html(key);
  assert.match(overview,/<dl class="ui-facts plain"><div><dt>Address<\/dt><dd><button[^>]*openSvc\(&quot;192.0.2.20&quot;,8000\)[^>]*><span class="mono">192.0.2.20<\/span> <span class="dim">port<\/span> <span class="mono">8000<\/span><\/button>/);
  for(const label of ["Host","Memory","Running for"])assert.match(overview,new RegExp(`<dt>${label}</dt>`),label);
  assert.doesNotMatch(overview,/Last lines of its log/,"a healthy app shows no log");
  // Settings: what it runs, with the icons.
  const settings=c.workloadSettingsTab(key);
  for(const label of ["Image","Copies ready","Volumes","Updates","Namespace"])assert.match(settings,new RegExp(`</svg>${label}</dt>`),`${label} has its icon`);
  assert.match(settings,/openWorkloadVolume\(&quot;paperless-data&quot;\)/);
  assert.match(c.HOVER_CARDS["app-status"](key),/<b>paperless is running<\/b>[\s\S]*Copies ready[\s\S]*1 of 1[\s\S]*Open details ›/);
  assert.match(c.HOVER_CARDS.app(key),/<b>paperless<\/b>[\s\S]*Image[\s\S]*image:stable[\s\S]*Open details ›/);
  assert.equal(c.DETAIL_KINDS.app.head('["","apps","gone"]'),null,"an app that is gone has nothing to show");
  assert.equal(c.HOVER_CARDS.app('["","apps","gone"]'),"");
});
test("a ⋯ menu puts a heading over each run of items with a new group, and none without groups",()=>{
  const c=context();
  const html=c.actionBar([{label:"A",run:"a()"},{label:"B",run:"b()",group:"Use"},{label:"C",run:"c()",group:"Use"},{label:"D",run:"d()",group:"Remove"}],{shown:1});
  assert.equal((html.match(/actionmenu-group/g)||[]).length,2);
  assert.doesNotMatch(c.actionBar([{label:"A",run:"a()"},{label:"B",run:"b()"},{label:"C",run:"c()"}],{shown:1}),/actionmenu-group/);
});
