"use strict";
const test=require("node:test"), assert=require("node:assert/strict"), fs=require("node:fs"), vm=require("node:vm");
/* The VM's own dialog, as GET /api/vm answers it: the namespace is "ns". */
function open(detail) {
  const body={innerHTML:""};
  const ctx={console, URLSearchParams, Map, Date, Promise, encodeURIComponent,
    document:{addEventListener(){}},STATE:{data:{}},$:key=>key==="#mbody" ? body : null,$$:()=>[],
    esc:v=>String(v).replaceAll("&","&amp;").replaceAll("<","&lt;").replaceAll('"',"&quot;"),
    toast(){},refresh(){},modal(){},icon:()=>"",
    api:async path=>{assert.match(path,/^\/api\/vm\?/);return detail;}};
  ctx.window=ctx;vm.createContext(ctx);require("./helpers/load-ui")(ctx);
  ctx.jsArg=s=>JSON.stringify(String(s??""));ctx.jsq=s=>ctx.esc(ctx.jsArg(s));
  vm.runInContext(fs.readFileSync("web/js/views-vms.js","utf8"),ctx);
  return {ctx,body};
}
const detail={ns:"lab",name:"guest",status:"Running",run_strategy:"Always",node:"node-a",actions:[],
  cores:2,memory:"4Gi",disks:[],nics:[{name:"default",model:"virtio",network:"pod network",mac:"",ips:[]}],
  conditions:[],events:[]};
test("a VM's Configure VIP / ports button names the VM's namespace",async()=>{
  const t=open(detail);
  await t.ctx.vmOpen("lab","guest");
  const call=t.body.innerHTML.match(/onclick="(networkManage\([^"]*\))"/);
  assert.ok(call,"the Network tab offers Configure VIP / ports");
  const args=[];
  vm.runInContext(call[1].replaceAll("&quot;",'"'),Object.assign(t.ctx,{networkManage:(...a)=>args.push(...a)}));
  assert.deepEqual(args,["lab","guest","VirtualMachine"]);
});
/* The dialog takes the shared parts (#506): where the VM is, then its figures,
   as one facts panel; conditions in UI.more; the shared section tabs; a Close footer. */
const shown={...detail,ip:"192.0.2.60",ips:["192.0.2.60","192.0.2.61"],
  conditions:[{type:"Ready",status:"True",reason:"",message:""}]};
const facts=html=>[...(html.match(/<dl class="ui-facts">[\s\S]*?<\/dl>/)||[""])[0].matchAll(/<dt>(?:<svg[^>]*><use[^>]*\/><\/svg>)?([^<]*)<\/dt><dd>([\s\S]*?)<\/dd>/g)].map(m=>[m[1],m[2]]);
test("a VM's dialog leads with its address and host, then its figures, as one facts panel",async()=>{
  const t=open(shown);await t.ctx.vmOpen("lab","guest");
  const pairs=facts(t.body.innerHTML);
  assert.deepEqual(pairs.slice(0,2).map(p=>p[0]),["Address","Host"]);
  assert.match(pairs[0][1],/192\.0\.2\.60/);assert.match(pairs[0][1],/192\.0\.2\.61/);
  assert.match(pairs[1][1],/node-a/);
  for(const label of ["CPU","Memory","Guest OS","Live migration"])assert.ok(pairs.some(p=>p[0]===label),`${label} is a fact`);
  assert.doesNotMatch(t.body.innerHTML,/vm-facts/,"no hand-built figure grid");
});
test("a VM's conditions sit in UI.more",async()=>{
  const t=open(shown);await t.ctx.vmOpen("lab","guest");
  const more=t.body.innerHTML.match(/<details class="ui-more"[^>]*>\s*<summary>Conditions<\/summary>[\s\S]*?<\/details>/);
  assert.ok(more,"a Conditions disclosure");
  assert.match(more[0],/Ready/);
});
test("a VM's dialog uses the shared section tabs and closes from a footer",async()=>{
  const t=open(shown);await t.ctx.vmOpen("lab","guest");
  const html=t.body.innerHTML;
  assert.doesNotMatch(html,/class="seg"/,"no hand-built tab strip");
  assert.match(html,/role="tablist"/);
  const tabs=[...html.matchAll(/role="tab"[^>]*>([^<]*)<\/button>/g)].map(m=>m[1]);
  assert.deepEqual(tabs.map(x=>x.split(" ·")[0]),["Overview","Disks","Network","Passthrough","Events"]);
  assert.match(html,/<button[^>]*data-dialog-dismiss="true"[^>]*>Close<\/button>/);
});
/* What Edit changes is shown on the page too, each with a Change that opens Edit at it. */
test("a VM's page shows its hardware, display and passthrough without opening Edit",async()=>{
  const hardware={cpu:{sockets:1,cores:4,threads:1,model:"host-passthrough",dedicated:false},firmware:"uefi",secure_boot:false,
    efi_persistent:true,tpm:"persistent",machine:"q35",hyperv:true,timezone:"Europe/London",hugepages:"",graphics:false,
    boot_output:"gpu",serial:true,tablet:true,rng:false,balloon:true,sound:false};
  const t=open({...shown,hardware,host_devices:[{name:"gpu1",resource:"nvidia.com/GA104",gpu:true,rom:false}]});
  await t.ctx.vmOpen("lab","guest");
  const html=t.body.innerHTML;
  const tabs=[...html.matchAll(/role="tab"[^>]*>([^<]*)<\/button>/g)].map(m=>m[1].split(" ·")[0]);
  assert.deepEqual(tabs,["Overview","Hardware","Display and devices","Disks","Network","Passthrough","Events"]);
  assert.match(html,/Passed-through GPU only - no virtual display/);
  assert.match(html,/nvidia\.com\/GA104/);
  assert.match(html,/vmEditAt\(&quot;lab&quot;,&quot;guest&quot;,&quot;display&quot;\)/);
});

/* The Network tab's one action is its own, not part of a note (#559). */
test("a VM's Network tab leads with Configure VIP / ports as its own action, not inside a note",async()=>{
  const t=open(detail);await t.ctx.vmOpen("lab","guest");
  const pane=t.body.innerHTML.match(/class="stepper-pane"[^>]*data-key="network"[^>]*>([\s\S]*?)<\/div>\s*<div class="stepper-pane"/);
  assert.ok(pane,"a Network pane");
  assert.doesNotMatch(pane[1],/class="note[^"]*"[^>]*>[^]*?Configure VIP/,"the button is not inside a note");
  assert.match(pane[1],/class="ui-lead/,"the explanation is a lead");
  assert.ok(pane[1].indexOf("Configure VIP / ports")<pane[1].indexOf("<table"),"the action comes before the table");
});
/* #543: a chip with a tip is a Tab stop, so a VM row's status chip takes one only when there is a problem to say. */
test("a VM row's status chip is a Tab stop only when it has a problem to explain",()=>{
  const t=open(detail);
  Object.assign(t.ctx,{clusterAttr:()=>"",clusterTag:()=>"",answerTag:()=>"",scheduleTag:()=>"",vmAvatar:()=>"",vmAddress:()=>"",vmActions:()=>""});
  const row=v=>vm.runInContext("vmTable",t.ctx)([{ns:"lab",name:"guest",node:"node-a",ips:[],...v}]);
  const chip=html=>(html.match(/<span class="ui-chip [^"]*"[^>]*>(Running|Stopped)<\/span>/)||[""])[0];
  assert.match(chip(row({status:"Running"})),/^<span class="ui-chip ok">/,"a healthy VM's chip is plain");
  assert.match(chip(row({status:"Stopped",problem:"the disk is missing"})),/tabindex="0"[^>]*data-tip="the disk is missing"/);
});
