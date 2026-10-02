'use strict';
const test=require('node:test'), assert=require('node:assert/strict');
const fs=require('node:fs'), vm=require('node:vm'), {webcrypto}=require('node:crypto');
function setup(){
  let html='',timer,postResolve,closed=false;
  const sent=[], fields={'#lhV2Setup':{innerHTML:''},'#lhV2Confirm':{checked:false},'#lhV2Apply':{},'#modal':{classList:{contains:()=>closed}}};
  const plan={namespace:'lab',nodes:[{node:'k3s-test',required_mib:2048,target_pages:1024,capacity_mib:0,can_prepare:true,review_token:'host-token',problems:['Capacity is zero']}],required_mib:2048,blockers:['k3s-test: capacity is zero'],can_enable:false,review_token:'enable-token'};
  const c={window:null,console,crypto:webcrypto,Uint8Array, $:s=>fields[s],esc:s=>String(s??'').replaceAll('<','&lt;').replaceAll('>','&gt;'),jsq:s=>JSON.stringify(s).replaceAll('"','&quot;'),
    modal:(title,body)=>{html=body;closed=false;},childModal:(title,body)=>{html=body;},toast(){},
    actionBar:actions=>actions.filter(Boolean).map(a=>a.label).join(' '),setTimeout:f=>{timer=f;return 1;},clearTimeout(){},
    api:async(path,opts)=>{if(!opts)return plan;sent.push({path,body:JSON.parse(opts.body)});return new Promise(r=>{postResolve=r;});},
    nodePowerReview:async(...args)=>{sent.push(args);}};
  c.window=c;vm.createContext(c);vm.runInContext(fs.readFileSync('web/js/ui.js','utf8'),c);vm.runInContext(fs.readFileSync('web/js/longhorn-v2-setup.js','utf8'),c);
  return {c,fields,sent,plan,html:()=>html,poll:()=>timer(),resolve:()=>postResolve({}),close:()=>{closed=true;delete fields['#lhV2Confirm'];}};
}
test('zero hugepage capacity offers host preparation and disables enabling',async()=>{
  const t=setup();await t.c.lhV2Setup();
  assert.match(t.fields['#lhV2Setup'].innerHTML,/Prepare host/);
  assert.match(t.fields['#lhV2Setup'].innerHTML,/lhV2ReviewEnable\(\)[^>]*disabled/);
  t.c.lhV2ReviewEnable();assert.doesNotMatch(t.html(),/id="lhV2Confirm"/);
});
test('host preparation requires acknowledgement and consumes confirmation before awaiting',async()=>{
  const t=setup();await t.c.lhV2Setup();t.c.lhV2ReviewHost('k3s-test');
  await t.c.lhV2Apply();assert.equal(t.sent.length,0);
  t.fields['#lhV2Confirm'].checked=true;
  const pending=t.c.lhV2Apply();await t.c.lhV2Apply();assert.equal(t.sent.length,1);
  assert.equal(t.sent[0].body.review_token,'host-token');assert.equal(t.sent[0].body.confirm,true);
  assert.match(t.sent[0].body.request_id,/^[a-f0-9]{24}$/);
  t.resolve();await pending;
});
test('finishing a request after closing does not reopen setup',async()=>{
  const t=setup();await t.c.lhV2Setup();t.c.lhV2ReviewHost('k3s-test');t.fields['#lhV2Confirm'].checked=true;
  const pending=t.c.lhV2Apply();t.close();const before=t.html();t.resolve();await pending;assert.equal(t.html(),before);
});
test('configured hosts offer the existing reboot review while preparation remains saved',async()=>{
  const t=setup();Object.assign(t.plan.nodes[0],{configured:true,needs_reboot:true,job:{name:'job',state:'succeeded'}});
  await t.c.lhV2Setup();assert.match(t.fields['#lhV2Setup'].innerHTML,/Review reboot/);assert.doesNotMatch(t.fields['#lhV2Setup'].innerHTML,/>Prepare host</);
  await t.c.lhV2Reboot('k3s-test');assert.deepEqual(t.sent[0],['k3s-test','reboot']);
});
test('engine enabled is not declared ready and cluster messages are escaped',async()=>{
  const t=setup();t.plan.enabled=true;t.plan.nodes[0].problems=['<script>bad</script>'];await t.c.lhV2Setup();
  assert.match(t.fields['#lhV2Setup'].innerHTML,/waiting for instance managers/);
  assert.match(t.fields['#lhV2Setup'].innerHTML,/&lt;script&gt;/);assert.doesNotMatch(t.fields['#lhV2Setup'].innerHTML,/<script>/);
});


test('a host whose modules fail verification after reboot can be prepared again',async()=>{
  const t=setup();Object.assign(t.plan.nodes[0],{configured:true,needs_reboot:false,problems:['Kernel modules need preparation']});
  await t.c.lhV2Setup();assert.match(t.fields['#lhV2Setup'].innerHTML,/Review repair/);
});
