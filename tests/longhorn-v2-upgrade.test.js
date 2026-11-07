'use strict';
const test=require('node:test'), assert=require('node:assert/strict'), fs=require('node:fs'), vm=require('node:vm');
function setup() {
  let html='', resolvePost, timer, closed=false;
  const sent=[], fields={'#lhV2UpgradeBody':{innerHTML:''},'#lhV2UpgradeBackup':{checked:false},'#lhV2UpgradeTimeout':{value:'60'},'#lhV2SettingsAck':{checked:false}};
  const report={installed:'v1.12.2',target:'v1.13.0',v2_volumes:3,live_ready:true,offline_ready:false,live_blockers:[],offline_blockers:['Stop and detach disks'],upgrade:{enabled:false,timeout:60,nodes:[]}};
  const c={window:null,console,document:{addEventListener(){},getElementById:id=>closed?null:fields['#'+id]||null},$:s=>closed?null:fields[s],esc:s=>String(s??'').replaceAll('&','&amp;').replaceAll('<','&lt;').replaceAll('>','&gt;').replaceAll('"','&quot;'),
    can:()=>true,icon:()=>'',modal:(_,h)=>{html=h;closed=false;},childModal:(_,h)=>{html=h;},closeModal:()=>{closed=true;},toast(){},clusterComponentsPaint(){},
    setTimeout:f=>{timer=f;return 1;},clearTimeout(){},api:async(path,opts)=>{
      if(!opts)return report;
      const body=JSON.parse(opts.body);sent.push({path,body});
      if(path.endsWith('/review'))return {capacity_token:'review-token',plan:report};
      return new Promise(r=>{resolvePost=r;});
    }};
  c.window=c;vm.createContext(c);vm.runInContext(fs.readFileSync('web/js/ui.js','utf8'),c);vm.runInContext(fs.readFileSync('web/js/longhorn-v2-upgrade.js','utf8'),c);
  return {c,fields,sent,report,html:()=>html,close:()=>{closed=true;},resolve:()=>resolvePost({detail:'Updated'}),poll:()=>timer()};
}
test('live upgrade review displays eligibility and requires backup acknowledgement',async()=>{
  const t=setup();await t.c.lhV2Upgrade('v1.13.0');
  assert.match(t.fields['#lhV2UpgradeBody'].innerHTML,/meet the live upgrade requirements/);
  await t.c.lhV2UpgradeStart();assert.equal(t.sent.length,0);
  t.fields['#lhV2UpgradeBackup'].checked=true;
  const pending=t.c.lhV2UpgradeStart();await t.c.lhV2UpgradeStart();assert.equal(t.sent.length,1);
  assert.equal(t.sent[0].body.v2_mode,'live');assert.equal(t.sent[0].body.confirm_backup,true);t.resolve();await pending;
});
test('offline blockers cannot be bypassed by clicking start and are escaped',async()=>{
  const t=setup();t.report.offline_blockers=['<script>bad</script>'];await t.c.lhV2Upgrade('v1.13.0');t.c.lhV2UpgradeMode('offline');
  const h=t.fields['#lhV2UpgradeBody'].innerHTML;assert.match(h,/&lt;script&gt;/);assert.doesNotMatch(h,/<script>/);assert.match(h,/disabled/);
  await t.c.lhV2UpgradeStart();assert.equal(t.sent.length,0);
});
test('settings acknowledgement consumes its reviewed request before sending',async()=>{
  const t=setup();t.report.installed='v1.13.0';await t.c.lhV2Upgrade();t.c.lhV2UpgradeSettings(true);await t.c.lhV2UpgradeSettingsReview(true);
  await t.c.lhV2UpgradeSettingsApply();assert.equal(t.sent.length,1);
  t.fields['#lhV2SettingsAck'].checked=true;const pending=t.c.lhV2UpgradeSettingsApply();await t.c.lhV2UpgradeSettingsApply();assert.equal(t.sent.length,2);
  assert.equal(t.sent[1].body.capacity_token,'review-token');assert.equal(t.sent[1].body.confirm_capacity,true);t.resolve();await pending;
});
test('closed upgrade status does not reopen when the poll runs',async()=>{
  const t=setup();await t.c.lhV2Upgrade();const before=t.html();t.close();await t.poll();assert.equal(t.html(),before);
});
test('progress shows actual stage, host and escaped error',async()=>{
  const t=setup();Object.assign(t.report,{installed:'v1.13.0',upgrade:{enabled:false,timeout:60,current_node:'node-2',nodes:[{node:'node-2',stage:'waiting-for-healthy-volumes',state:'in-progress',error:'<img src=x>',retries:2}]}});
  await t.c.lhV2Upgrade();const h=t.fields['#lhV2UpgradeBody'].innerHTML;
  assert.match(h,/Waiting for healthy volumes/);assert.match(h,/node-2/);assert.match(h,/&lt;img/);assert.doesNotMatch(h,/<img/);assert.match(h,/Enable \/ resume/);
});
test('Harvester shows upgrade status without independent upgrade controls',async()=>{
  const t=setup();Object.assign(t.report,{installed:'v1.13.0',harvester:true});await t.c.lhV2Upgrade();assert.doesNotMatch(t.fields['#lhV2UpgradeBody'].innerHTML,/lhV2UpgradeSettings\(/);
});
test('the upgrade check shows in the review, and a blocked one needs its tick',async()=>{
  const t=setup();t.fields['#cmp_check_ack']={checked:false};
  // core.js declares STATE with const: a global name, not a window property.
  vm.runInContext("const STATE={data:{components:{components:[{id:'longhorn',next:'v1.13.0',check:{verdict:'blocked',findings:[]}}]}}};",t.c);
  t.c.componentCheckSection=r=>r?.verdict==='blocked'?'<section>Before you upgrade</section>':'';
  await t.c.lhV2Upgrade('v1.13.0');t.fields['#lhV2UpgradeBackup'].checked=true;
  assert.match(t.fields['#lhV2UpgradeBody'].innerHTML,/Before you upgrade/);
  await t.c.lhV2UpgradeStart();assert.equal(t.sent.length,0,'not without the tick');
  t.fields['#cmp_check_ack'].checked=true;const pending=t.c.lhV2UpgradeStart();assert.equal(t.sent.length,1);
  assert.equal(t.sent[0].body.accept_check,true);t.resolve();await pending;
});
test('an upgrade with no blocked check sends no acceptance',async()=>{
  const t=setup();t.fields['#lhV2UpgradeBackup'].checked=true;await t.c.lhV2Upgrade('v1.13.0');
  const pending=t.c.lhV2UpgradeStart();assert.equal(t.sent.length,1);assert.equal('accept_check' in t.sent[0].body,false);t.resolve();await pending;
});
