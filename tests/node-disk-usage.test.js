"use strict";
const test = require("node:test"), assert = require("node:assert/strict");
const fs = require("node:fs"), vm = require("node:vm");
const ctx = {STATE:{data:{}},esc:String};ctx.window=ctx;
vm.createContext(ctx);
for (const file of ["metrics-utils.js","views-overview.js","diagrams.js"])
  vm.runInContext(fs.readFileSync(`web/js/${file}`,"utf8"),ctx);
const node = {fs_cap_gb:100,fs_used_gb:40};
const root = {device:"nvme0n1",size_gb:500,root_fs:true,system:true,role:"longhorn",lh_size_gb:70,
  lh_filesystems:[{capacity_gb:100,used_gb:40,data_gb:10,available_gb:60,reserved_gb:30,on_root:true}]};

test("a small root LV uses its own capacity and respects Longhorn's reserve",()=>{
  const usage=ctx.diskUsage(node,root);
  assert.equal(usage.capacity,100);assert.equal(usage.physical,500);
  assert.equal(usage.pct,40);assert.equal(usage.host,30);assert.equal(usage.longhorn,10);
  assert.equal(usage.room,30);assert.equal(usage.remaining,30);
});

test("a separate Longhorn filesystem adds capacity without counting the root twice",()=>{
  const disk={...root,lh_filesystems:[...root.lh_filesystems,
    {capacity_gb:200,used_gb:50,data_gb:50,available_gb:150,reserved_gb:20,on_root:false}]};
  const usage=ctx.diskUsage(node,disk);
  assert.equal(usage.capacity,300);assert.equal(usage.used,90);assert.equal(usage.pct,30);
  assert.equal(usage.room,160);assert.equal(usage.remaining,50);
});

test("a boot-only drive does not inherit another drive's root filesystem",()=>{
  const usage=ctx.diskUsage(node,{device:"sdb",size_gb:50,system:true,root_fs:false,lh_filesystems:[]});
  assert.equal(usage.capacity,0);assert.equal(usage.pct,null);
});

test("a separate kubelet filesystem uses its counters without duplicating them on the root disk",()=>{
  const usage=ctx.diskUsage({fs_cap_gb:200,fs_used_gb:50},{size_gb:500,root_fs:false,node_fs:true,
    lh_filesystems:[{capacity_gb:200,used_gb:50,data_gb:10,available_gb:150,reserved_gb:20,on_root:false,on_node_fs:true}]});
  assert.equal(usage.capacity,200);assert.equal(usage.host,40);assert.equal(usage.longhorn,10);
  assert.equal(usage.room,130);assert.equal(usage.pct,25);
  const otherRoot=ctx.diskUsage({fs_cap_gb:200,fs_used_gb:50},{...root,node_fs:false,
    lh_filesystems:root.lh_filesystems.map(f=>({...f,on_node_fs:false}))});
  assert.equal(otherRoot.capacity,100);assert.equal(otherRoot.host,30);assert.equal(otherRoot.longhorn,10);
});

test("incomplete and inconsistent reports cannot fill beyond the filesystem",()=>{
  assert.equal(ctx.diskUsage({},root).pct,40,"Longhorn can supply root filesystem counters when kubelet is unavailable");
  const usage=ctx.diskUsage({fs_cap_gb:100,fs_used_gb:150},{...root,
    lh_filesystems:[{capacity_gb:100,used_gb:150,data_gb:120,available_gb:90,reserved_gb:0,on_root:true}]});
  assert.equal(usage.used,100);assert.equal(usage.room,0);assert.equal(usage.host,0);
  assert.equal(ctx.diskUsage({},{size_gb:500,system:true,lh_filesystems:[]}).pct,null);
});

test("cached disk summaries never infer free Longhorn space without its reservation counters",()=>{
  const usage=ctx.diskUsage(node,{device:"nvme0n1",size_gb:500,system:true,
    lh_size_gb:70,lh_used_gb:10,lh_root_used_gb:10,lh_paths:["/var/lib/longhorn/"]});
  assert.equal(usage.capacity,100);assert.equal(usage.used,40);assert.equal(usage.room,0);
});

test("host cards and the node diagram use the same filesystem scale",()=>{
  const html=ctx.nodeDiskLines({...node,disks:[root]});
  assert.match(html,/<b>40%<\/b>/);assert.match(html,/40\/100 GB/);
  assert.match(html,/Filesystem use · 500 GB disk/);assert.doesNotMatch(html,/40\/500/);
  const picture=ctx.Diagram.node({...node,name:"node-a",status:"Ready",disks:[root]});
  assert.match(picture,/Filesystem 40\/100 GB · 40%/);
  assert.match(picture,/width="54" height="9" rx="0" class="dg-sys"/);
});
