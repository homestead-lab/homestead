'use strict';
const test=require('node:test'),assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm');
function setup() {
  const fields={},sent=[],timers=[];let closed=false,html='',pending=null;
  const plan={node:'node-1',disk:'data',device:'/dev/sdb',size_bytes:1073741824000,volumes:[],blockers:[],request_id:'a'.repeat(24),capacity_token:'evacuate-token'};
  const item={id:'task-1',node:'node-1',device:'/dev/sdb',phase:'awaiting-erase',status:'running',progress:70,message:'Ready for review',remaining:0,volumes:[],needs_erase_review:true,cancellable:true};
  const prep={operation_id:'task-1',device:'/dev/sdb',node:'node-1',size_bytes:plan.size_bytes,request_id:'b'.repeat(24),capacity_token:'erase-token'};
  function register(body) {
    for(const [,id] of body.matchAll(/id="([^"]+)"/g)) {
      let value='';const el={checked:false,disabled:false,value:''};
      Object.defineProperty(el,'innerHTML',{get:()=>value,set:s=>{value=s;register(s);}});fields['#'+id]=el;
    }
  }
  function show(title,body) {html=body;for(const key of Object.keys(fields)) delete fields[key];fields['#modal']={classList:{contains:()=>closed}};closed=false;register(body);}
  const c={window:null,console,MODAL_STACK:[],esc:s=>String(s??'').replaceAll('&','&amp;').replaceAll('<','&lt;').replaceAll('>','&gt;').replaceAll('"','&quot;'),jsq:s=>JSON.stringify(s).replaceAll('"','&quot;'),
    actionBar:actions=>actions.filter(Boolean).map(a=>a.label).join(' '), $:s=>fields[s],modal:show,childModal:show,toast(){},noteOperation(){},clearTimeout(){},setTimeout:f=>{timers.push(f);return timers.length;},
    api:async(path,opts)=>{
      if(!opts)return item;
      const body=JSON.parse(opts.body);sent.push({path,body});
      if(path.endsWith('/plan'))return plan;
      if(path.endsWith('/prepare-review'))return prep;
      return new Promise(resolve=>{pending=resolve;});
    }};
  c.window=c;vm.createContext(c);vm.runInContext(fs.readFileSync('web/js/ui.js','utf8'),c);vm.runInContext(fs.readFileSync('web/js/disk-v2.js','utf8'),c);
  return {c,fields,sent,plan,item,prep,html:()=>html,close:()=>{closed=true;},resolve:()=>pending(item),timers};
}
test('blocked evacuation explains the constraint and has no usable acknowledgement',async()=>{
  const t=setup();t.plan.blockers=['Add V1 capacity'];await t.c.diskV2Open('node-1','data');
  assert.match(t.fields['#diskV2Review'].innerHTML,/Add V1 capacity/);assert.equal(t.fields['#diskV2Confirm'],undefined);
  await t.c.diskV2Begin();assert.equal(t.sent.length,1);
});
test('evacuation requires acknowledgement and consumes its token before dispatch',async()=>{
  const t=setup();await t.c.diskV2Open('node-1','data');await t.c.diskV2Begin();assert.equal(t.sent.length,1);
  t.fields['#diskV2Confirm'].checked=true;const pending=t.c.diskV2Begin();await t.c.diskV2Begin();
  assert.equal(t.sent.length,2);assert.equal(t.sent[1].body.capacity_token,'evacuate-token');assert.equal(t.sent[1].body.confirm_capacity,true);
  t.resolve();await pending;assert.ok(t.fields['#diskV2Status']);
});
test('a completion after closing does not reopen the dialog',async()=>{
  const t=setup();await t.c.diskV2Open('node-1','data');t.fields['#diskV2Confirm'].checked=true;
  const pending=t.c.diskV2Begin();t.close();const before=t.html();t.resolve();await pending;assert.equal(t.html(),before);
});
test('polling finished evacuation never requests an erase',async()=>{
  const t=setup();await t.c.diskV2Watch('task-1');assert.match(t.fields['#diskV2Status'].innerHTML,/Review erase/);
  await t.timers[0]();assert.equal(t.sent.length,0);
});
test('erase needs the exact device and checkbox and only dispatches once',async()=>{
  const t=setup();await t.c.diskV2Watch('task-1');await t.c.diskV2EraseReview('task-1');
  assert.match(t.html(),/filesystem will be erased/);
  t.fields['#diskV2EraseConfirm'].checked=true;t.fields['#diskV2Device'].value='/dev/sdc';
  await t.c.diskV2Erase();assert.equal(t.sent.length,1);
  t.fields['#diskV2Device'].value='/dev/sdb';const pending=t.c.diskV2Erase();await t.c.diskV2Erase();
  assert.equal(t.sent.length,2);assert.equal(t.sent[1].body.confirm_device,'/dev/sdb');assert.equal(t.sent[1].body.capacity_token,'erase-token');
  t.resolve();await pending;
});
test('failed and completed tasks stop polling and escape cluster messages',async()=>{
  for(const status of ['failed','succeeded']) {
    const t=setup();Object.assign(t.item,{status,phase:status==='failed'?'preparing':'complete',needs_erase_review:false,cancellable:false,message:'<script>cluster message</script>'});
    await t.c.diskV2Watch('task-1');const html=t.fields['#diskV2Status'].innerHTML;
    assert.match(html,/&lt;script&gt;/);assert.doesNotMatch(html,/<script>/);assert.equal(t.timers.length,0);
    assert.doesNotMatch(html,/diskV2EraseReview/);
  }
});

test('a direct node visit offers preparation without cached engine settings and retains the task link after removal',()=>{
  const t=setup();t.c.STATE={data:{}};t.c.sizePair=()=>'';t.c.sizeText=()=>'';t.c.meter=()=>'';
  vm.runInContext(fs.readFileSync('web/js/views-storage.js','utf8'),t.c);
  const disk={device:'sdb',mounts:['/mnt/data'],role:'longhorn',longhorn:[{id:'data',type:'filesystem',ready:true,scheduling:true,tags:[],replicas:1}]};
  const html=t.c.diskRowsHtml('node-1',[disk],false);assert.match(html,/Prepare for V2/);
  assert.doesNotMatch(t.c.diskRowsHtml('node-1',[disk],true),/Prepare for V2/);
  Object.assign(disk,{role:'unused',longhorn:[],v2_preparation:{id:'task-1',phase:'preparing'}});
  const active=t.c.diskRowsHtml('node-1',[disk],false);assert.match(active,/V2 preparation/);assert.doesNotMatch(active,/diskSetup/);
});
