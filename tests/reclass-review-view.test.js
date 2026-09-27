const test=require('node:test'),assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm');
function setup({blocked=false,missing=false,fail=false}={}) {
  const fields={'#rc_plan':{},'#rc_to':{value:'new-class'},'#rc_go':{},'#rc_ack':{checked:false}},calls=[];
  const ctx={console,Date,Map,Set,Promise,URLSearchParams,encodeURIComponent,
    document:{addEventListener(){}},STATE:{data:{}},$:key=>fields[key],$$:()=>[],
    esc:x=>String(x).replaceAll('<','&lt;'),sizeText:x=>x+' GiB',toast:()=>{},
    UI:new Proxy({}, {get:(_,key)=>(...args)=>JSON.stringify({[key]:args})}),
    api:async(path,opts)=>{
      calls.push({path,body:JSON.parse(opts.body)});
      if(path.endsWith('/plan'))return {ok:!blocked,blockers:blocked?['Unsafe']:[],warnings:['Capacity unknown'],consumers:[{name:'<app>',kind:'Deployment',running:true}],space:{size_gb:20,replicas:2,allocated_gb:40},capacity_token:missing?'':'signed'};
      if(fail)throw Error('connection lost');return {operation:{id:'move-id'}};
    }};
  ctx.window=ctx;vm.createContext(ctx);vm.runInContext(fs.readFileSync('web/js/views-storage.js','utf8'),ctx);
  ctx.reclassWatch=id=>{fields.watched=id;};return{ctx,fields,calls};
}
test('class move has one acknowledgement and submits only the reviewed target once',async()=>{
  const t=setup();await t.ctx.volumeReclassPlan('lab','data');
  assert.match(t.fields['#rc_plan'].innerHTML,/&lt;app>/);
  await t.ctx.volumeReclassStart('lab','data');assert.equal(t.calls.length,1);
  t.fields['#rc_ack'].checked=true;await t.ctx.volumeReclassStart('lab','data');await t.ctx.volumeReclassStart('lab','data');
  assert.equal(t.calls.length,2);assert.deepEqual(t.calls[1].body,{namespace:'lab',claim:'data',target:'new-class',capacity_token:'signed',confirm_capacity:true});
});
test('blocked, missing and edited reviews cannot submit',async()=>{
  for(const options of [{blocked:true},{missing:true},{}]) {
    const t=setup(options);await t.ctx.volumeReclassPlan('lab','data');t.fields['#rc_ack'].checked=true;
    if(!options.blocked&&!options.missing)t.fields['#rc_to'].value='changed';
    await t.ctx.volumeReclassStart('lab','data');assert.equal(t.calls.length,1);
  }
});
test('uncertain start is never replayed and refreshes job history',async()=>{
  const t=setup({fail:true});let refreshes=0;t.ctx.refreshOperations=()=>refreshes++;
  await t.ctx.volumeReclassPlan('lab','data');t.fields['#rc_ack'].checked=true;
  await t.ctx.volumeReclassStart('lab','data');await t.ctx.volumeReclassStart('lab','data');
  assert.equal(t.calls.length,2);assert.equal(refreshes,1);assert.match(t.fields['#rc_plan'].innerHTML,/Check Recent jobs/);
});
test('late preview does not replace a newer review',async()=>{
  const t=setup();const api=t.ctx.api;let finish;
  t.ctx.api=()=>new Promise(resolve=>{finish=resolve;});
  const first=t.ctx.volumeReclassPlan('lab','data');
  t.ctx.api=api;t.fields['#rc_to'].value='latest';await t.ctx.volumeReclassPlan('lab','data');
  finish({});await first;
  t.fields['#rc_ack'].checked=true;await t.ctx.volumeReclassStart('lab','data');
  assert.equal(t.calls[1].body.target,'latest');
});
test('closing the dialog ignores late start results',async()=>{
  const t=setup();await t.ctx.volumeReclassPlan('lab','data');t.fields['#rc_ack'].checked=true;let finish;
  t.ctx.api=()=>new Promise(resolve=>{finish=resolve;});
  const pending=t.ctx.volumeReclassStart('lab','data');delete t.fields['#rc_plan'];finish({operation:{id:'move'}});await pending;
  assert.equal(t.fields.watched,undefined);
});
