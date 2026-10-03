const test=require("node:test"),assert=require("node:assert/strict"),H=require("../web/js/insights.js");
const base=()=>({overview:{health_issues:[]},cluster:{control_plane:{etcd_total:3,etcd_ready:3,quorum_margin:1}},protection:{volumes:[]},nodes:[],self:{},settings:{thresholds:{temperature:{warning:70,critical:85},disk:{warning:75,critical:90}}}});
test("quorum advice uses etcd membership, not total hosts or control-plane count",()=>{
 const s=base();s.cluster.control_plane={total:2,etcd_total:0};assert.equal(H.advice(s).length,0);
 s.cluster.control_plane={total:2,etcd_total:2,etcd_ready:2};assert.match(H.advice(s)[0].title,/Two etcd/);assert.equal(H.advice(s)[0].severity,"medium");
 s.cluster.control_plane.etcd_ready=1;assert.equal(H.advice(s)[0].severity,"critical");
});
test("snapshot-only volumes need external backups; a schedule does not mean a copy exists",()=>{
 const s=base();s.protection.volumes=[{name:"snapshot",snapshotted:true,backed_up:false},{name:"scheduled",backed_up:true,last_backup_at:""}];
 assert.deepEqual(H.advice(s).map(r=>r.id).sort(),["backup-coverage","backup-never"]);
});
test("missing sources are explicit unknown checks, not zero healthy results",()=>{
 const s=Object.fromEntries(Object.keys(base()).map(k=>[k,null]));assert.equal(H.advice(s).length,6);assert.ok(H.advice(s).every(r=>r.id.startsWith("unknown:")));
});
test("advice sorts critical first and respects configured temperature/disk thresholds",()=>{
 const s=base();s.nodes=[{name:"host",fs_pct:91,temps:{max_c:73,disks:[{name:"sda",health:{state:"critical",summary:"Pending sectors"}}]}}];
 const r=H.advice(s);assert.equal(r[0].severity,"critical");assert.equal(r.at(-1).severity,"medium");assert.match(r.find(r=>r.id.startsWith("temperature")).title,/73°C/);
 s.settings.thresholds.temperature.warning=80;assert.ok(!H.advice(s).some(r=>r.id.startsWith("temperature")));
});
test("standby loops are healthy; failed loops and replicas get actionable advice",()=>{
 const s=base();s.self={loops:[{name:"sampler",state:"standby"}]};assert.equal(H.advice(s).length,0);
 s.self.loops.push({name:"alerts",label:"Alerts",state:"failing"});s.self.replicas={desired:2,pods:[{ready:true}]};assert.equal(H.advice(s).length,2);
});
test("backup freshness shows age without inventing a daily SLA, and puts missing copies first",()=>{
 const r=H.backupRows({volumes:[{name:"weekly",last_backup_at:"2026-10-01T00:00:00Z"},{name:"none"}]},Date.parse("2026-10-03T00:00:00Z"));
 assert.equal(r[0].id,"none");assert.equal(r[1].detail,"Last backup 2d ago");assert.equal(r[1].severity,"low");
});
test("stopped containers and halted VMs are not failed workloads",()=>{
 const r=H.workloadSummary([{name:"stopped",desired:0,ready:0},{name:"ready",desired:1,ready:1},{name:"starting",desired:1,ready:0}], [{name:"off",run_strategy:"Halted",status:"Stopped"},{name:"vm",run_strategy:"RerunOnFailure",status:"ErrorUnschedulable"}]);
 assert.equal(r.ready,1);assert.equal(r.stopped,1);assert.equal(r.rows.length,2);
});

test("cluster health retains the dashboard banner reasons without duplicate drive findings",()=>{
 const s=base();s.nodes=[{name:"host",temps:{disks:[{name:"sda",health:{state:"attention",summary:"2 reallocated sectors"}}]}}];
 s.overview.health_issues=[{kind:"Disk",name:"host/sda",severity:"degraded",reason:"2 reallocated sectors"},{kind:"Workload",name:"lab/app",severity:"degraded",reason:"0/1 replicas ready"}];
 const rows=H.advice(s);assert.equal(rows.filter(r=>r.id==="disk:host:sda").length,1);assert.ok(rows.some(r=>r.detail==="0/1 replicas ready"));
});

test("unknown drive observations do not hide a warning reported by overview",()=>{
 const s=base();s.nodes=[{name:"host",temps:{disks:[{name:"sda",health:{state:"unknown"}}]}}];
 s.overview.health_issues=[{kind:"Disk",name:"host/sda",severity:"critical",reason:"Pending sectors"}];
 const drive=H.advice(s).filter(r=>r.id==="disk:host:sda");assert.equal(drive.length,1);assert.equal(drive[0].severity,"critical");assert.equal(drive[0].detail,"Pending sectors");
});

test("compact resource lists exclude platform helpers and preserve unknown and stopped readings",()=>{
 const rows=H.resourceRows('containers',[{name:'platform',platform:true},{name:'app',desired:1,ready:1,cpu:.125,mem_mb:32},{name:'off',desired:0,ready:0,cpu:2,mem_mb:200},{name:'unknown',desired:1,ready:0}]);
 assert.equal(rows.length,3);assert.equal(rows[0].cpu,12.5);assert.equal(rows[0].memory,32*1024**2);assert.equal(rows[1].status,'Stopped');assert.equal(rows[1].cpu,null);assert.equal(rows[2].cpu,null);assert.equal(rows[2].status,'0/1 ready');
 const vms=H.resourceRows('vms',[{name:'vm',status:'Running',usage:{cpu_pct:25,mem:1024}},{name:'broken',status:'CrashLoopBackOff'},{name:'off',status:'Stopped',usage:{cpu_pct:30}}]);
 assert.equal(vms[0].tone,'bad');assert.equal(vms[1].cpu,null);assert.equal(vms[2].cpu,25);assert.equal(vms[2].memory,1024);
});
