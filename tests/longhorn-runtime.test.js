'use strict';
const test=require('node:test'),assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm');
function setup(){
  const fields={},sent=[],timers=[];let html='',closed=false,pending,rejectPending;
  const plan={node:'host-1',disk:'store-1',device:'/dev/dm-1',stable_device:'/dev/disk/by-id/dm-uuid-LVM-example',size_bytes:128*1073741824,runtime_bytes:104857600,available_bytes:64*1073741824,destination:'/var/lib/homestead/longhorn-runtime/example/active',retained:'/mnt/homestead-retired-example',request_id:'a'.repeat(24),capacity_token:'review-token',layout:{path:'/dev/sda',kind:'disk',size_bytes:256*1073741824,children:[{path:'/dev/dm-0',kind:'lvm',size_bytes:64*1073741824,mountpoint:'/'},{path:'/dev/dm-1',kind:'lvm',size_bytes:128*1073741824,mountpoint:'/var/lib/longhorn'}]}};
  const item={...plan,id:'runtime-1',phase:'relocated',status:'running',message:'Runtime moved; original retained'};
  function register(body){for(const [tag,id] of body.matchAll(/<[^>]*\bid="([^"]+)"[^>]*>/g)){
    let value='';const el={value:'',checked:false,disabled:tag.includes('disabled'),dataset:{typedExpect:tag.match(/data-typed-expect="([^"]*)"/)?.[1]}};
    Object.defineProperty(el,'innerHTML',{get:()=>value,set:v=>{value=v;register(v);}});fields['#'+id]=el;
  }}
  const c={window:null,console,esc:v=>String(v??'').replaceAll('&','&amp;').replaceAll('<','&lt;').replaceAll('>','&gt;').replaceAll('"','&quot;'),jsArg:v=>JSON.stringify(v),$:s=>fields[s],toast(){},clearTimeout(){},setTimeout:f=>timers.push(f),modal(title,body){html=body;Object.keys(fields).forEach(k=>delete fields[k]);closed=false;fields['#modal']={classList:{contains:()=>closed}};register(body);},api:async(path,opts)=>{
    if(!opts)return item;sent.push({path,body:JSON.parse(opts.body)});
    if(path.endsWith('/review'))return plan;
    if(path.endsWith('/retirement-review'))return {...plan,id:item.id,action:'retire-runtime',capacity_token:'retire-token'};
    return new Promise((resolve,reject)=>{pending=resolve;rejectPending=reject;});
  }};
  c.window=c;vm.createContext(c);for(const name of ['ui','disk-v2','longhorn-runtime'])vm.runInContext(fs.readFileSync('web/js/'+name+'.js','utf8'),c);
  return {c,fields,sent,timers,plan,item,html:()=>html,close:()=>{closed=true;},resolve:()=>pending(item),reject:()=>rejectPending(new Error('Review expired'))};
}
test('runtime review retains source and OS while displaying accurate logical allocation',async()=>{
  const t=setup();await t.c.runtimeMoveOpen('host-1','store-1');const html=t.fields['#runtimeReview'].innerHTML;
  assert.match(html,/Move runtime/);assert.match(html,/Host OS/);assert.match(html,/not their position on it/);
  assert.match(html,/The LV and its files stay/);assert.doesNotMatch(html,/Erase contents/);
  assert.equal(t.fields['#runtimeStart'].disabled,true);await t.c.runtimeMoveStart();assert.equal(t.sent.length,1);
});
test('runtime staging consumes approval once and does not reopen a closed dialog',async()=>{
  const t=setup();await t.c.runtimeMoveOpen('host-1','store-1');t.fields['#runtimeConfirm'].checked=true;
  const pending=t.c.runtimeMoveStart();await t.c.runtimeMoveStart();assert.equal(t.sent.length,2);
  assert.deepEqual(Object.keys(t.sent[1].body).sort(),['node','disk','request_id','capacity_token','confirm_capacity'].sort());
  const before=t.html();t.close();t.resolve();await pending;assert.equal(t.html(),before);
});
test('retirement needs fresh review, exact device and separate acknowledgement',async()=>{
  const t=setup();await t.c.runtimeRetireReview(t.item.id);assert.match(t.html(),/filesystem, LV and disk identity stay/);
  assert.match(t.html(),/Retire files/);t.fields['#runtimeRetireConfirm'].checked=true;t.fields['#runtimeDevice'].value='/dev/dm-0';
  await t.c.runtimeRetire();assert.equal(t.sent.length,1);
  t.fields['#runtimeDevice'].value='/dev/dm-1';const pending=t.c.runtimeRetire();await t.c.runtimeRetire();
  assert.equal(t.sent.length,2);assert.equal(t.sent[1].body.capacity_token,'retire-token');assert.equal(t.sent[1].body.action,'retire-runtime');
  t.resolve();await pending;
});
test('status routes each phase to its separate review and never erases automatically',async()=>{
  for(const [phase,action] of [['staged','Review host restart'],['relocated','Review retirement'],['retire-staged','Review host restart'],['retired','Prepare for V2']]){
    const t=setup();Object.assign(t.item,{phase,status:phase==='retired'?'succeeded':'running'});await t.c.runtimeMoveWatch(t.item.id);
    assert.match(t.fields['#runtimeStatus'].innerHTML,new RegExp(action));assert.equal(t.sent.length,0);
  }
});
test('failed status stops polling and escapes untrusted host messages',async()=>{
  const t=setup();t.item.status='failed';t.item.message='<script>host output</script>';await t.c.runtimeMoveWatch(t.item.id);
  assert.match(t.fields['#runtimeStatus'].innerHTML,/&lt;script&gt;/);assert.doesNotMatch(t.fields['#runtimeStatus'].innerHTML,/<script>/);assert.equal(t.timers.length,0);
});
test('failed staging and retirement require a new visible review rather than a dead submit button',async()=>{
  const start=setup();await start.c.runtimeMoveOpen('host-1','store-1');start.fields['#runtimeConfirm'].checked=true;
  const pending=start.c.runtimeMoveStart();start.reject();await pending;
  assert.match(start.fields['#runtimeReview'].innerHTML,/Review again/);assert.match(start.fields['#runtimeReview'].innerHTML,/Runtime move not confirmed/);
  await start.c.runtimeMoveStart();assert.equal(start.sent.length,2);
  const retire=setup();await retire.c.runtimeRetireReview(retire.item.id);retire.fields['#runtimeRetireConfirm'].checked=true;retire.fields['#runtimeDevice'].value='/dev/dm-1';
  const retiring=retire.c.runtimeRetire();retire.reject();await retiring;
  assert.match(retire.fields['#runtimeRetirement'].innerHTML,/Review again/);assert.match(retire.fields['#runtimeRetirement'].innerHTML,/Retirement not confirmed/);
  await retire.c.runtimeRetire();assert.equal(retire.sent.length,2);
});
test('late retirement reviews cannot reopen a closed dialog or replace another modal',async()=>{
  for(const action of ['close','replace']){
    const t=setup();let resolve;t.c.api=()=>new Promise(r=>{resolve=r;});const pending=t.c.runtimeRetireReview(t.item.id);
    if(action==='close')t.close();else t.c.modal('Another dialog','Unrelated content');
    const before=t.html();resolve({...t.plan,id:t.item.id,action:'retire-runtime'});await pending;assert.equal(t.html(),before);
  }
});
