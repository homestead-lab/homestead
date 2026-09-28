const test=require('node:test'),assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm');
function setup({blocked=false,running=false,missing=false,fail=false}={}) {
  const fields={'#jobSummary':{},'#storageRecoveryAck':{checked:false},'#storageRecoveryApply':{}},calls=[];
  let refreshes=0,closed=0;
  const ctx={console,Date,URL,STATE:{data:{}},ME:null,$:k=>fields[k],$$:()=>[],esc:v=>String(v).replaceAll('<','&lt;'),icon:()=>'',
    clearInterval(){},clearTimeout(){},setTimeout(){},toast(){},closeModal(){closed++;delete fields['#storageRecoveryContent'];},
    modal(){fields['#storageRecoveryContent']={innerHTML:''};},api:async(path,opts)=>{
      const body=JSON.parse(opts.body);calls.push({path,body});
      if(path.endsWith('/preview'))return {tokens:missing?{}:running?{pause:'pause-token'}:blocked?{}:{continue:'continue-token'},plan:{
        id:body.id,claim:'<data>',phase:'copy',can_continue:!running&&!blocked,can_pause:running,from_class:'old',to_class:'new',
        resources:[{resource:{name:'<copy>'},receipt:'accepted',relationship:'same identity'}],workloads:[{name:'<app>',kind:'Deployment'}],
        warnings:['Memory warning'],blockers:blocked?['Request outcome unknown']:[],message:'Retained'}};
      if(fail)throw Error('Lost response');return {ok:true,detail:'Queued'};
    }};
  ctx.window=ctx;vm.createContext(ctx);ctx.jsArg=s=>JSON.stringify(String(s??""));ctx.jsq=s=>(ctx.esc||String)(ctx.jsArg(s));
  for(const file of ['ui','operations'])vm.runInContext(fs.readFileSync(`web/js/${file}.js`,'utf8'),ctx);
  ctx.refreshOperations=()=>{refreshes++;};
  return {ctx,fields,calls,get refreshes(){return refreshes;},get closed(){return closed;}};
}
test('continue requires one acknowledgement, escapes facts and sends once',async()=>{
  const t=setup();await t.ctx.storageRecoveryReview('job');
  const html=t.fields['#storageRecoveryContent'].innerHTML;
  assert.match(html,/&lt;data>/);assert.match(html,/&lt;copy>/);assert.match(html,/Continue move/);
  assert.equal((html.match(/type="checkbox"/g)||[]).length,1);
  await t.ctx.storageRecoveryApply();assert.equal(t.calls.length,1);
  t.fields['#storageRecoveryAck'].checked=true;await t.ctx.storageRecoveryApply();await t.ctx.storageRecoveryApply();
  assert.equal(t.calls.length,2);assert.deepEqual(t.calls[1].body,{id:'job',action:'continue',capacity_token:'continue-token',confirm_capacity:true});
});
test('running move offers pause and explains accepted jobs can finish',async()=>{
  const t=setup({running:true});await t.ctx.storageRecoveryReview('job');
  assert.match(t.fields['#storageRecoveryContent'].innerHTML,/Pause move/);
  assert.match(t.fields['#storageRecoveryContent'].innerHTML,/can still finish/);
  t.fields['#storageRecoveryAck'].checked=true;await t.ctx.storageRecoveryApply();
  assert.equal(t.calls[1].body.action,'pause');
});
test('unknown outcome and incomplete approval cannot send',async()=>{
  for(const options of [{blocked:true},{missing:true}]) {
    const t=setup(options);await t.ctx.storageRecoveryReview('job');t.fields['#storageRecoveryAck'].checked=true;
    await t.ctx.storageRecoveryApply();assert.equal(t.calls.length,1);
  }
});
test('lost response refreshes history but is never retried automatically',async()=>{
  const t=setup({fail:true});await t.ctx.storageRecoveryReview('job');t.fields['#storageRecoveryAck'].checked=true;
  await t.ctx.storageRecoveryApply();await t.ctx.storageRecoveryApply();
  assert.equal(t.calls.length,2);assert.equal(t.refreshes,1);
  assert.match(t.fields['#storageRecoveryContent'].innerHTML,/Nothing was retried/);
});
test('late preview cannot replace a newer review',async()=>{
  const t=setup(),original=t.ctx.api;let finish;
  t.ctx.api=(path,options)=>JSON.parse(options.body).id==='old'?new Promise(resolve=>finish=resolve):original(path,options);
  const old=t.ctx.storageRecoveryReview('old');await t.ctx.storageRecoveryReview('new');finish({});await old;
  t.fields['#storageRecoveryAck'].checked=true;await t.ctx.storageRecoveryApply();assert.equal(t.calls.at(-1).body.id,'new');
});
test('closing while action is pending leaves the new dialog untouched',async()=>{
  const t=setup();await t.ctx.storageRecoveryReview('job');t.fields['#storageRecoveryAck'].checked=true;let finish;
  t.ctx.api=()=>new Promise(resolve=>finish=resolve);
  const pending=t.ctx.storageRecoveryApply();t.ctx.modal();finish({ok:true});await pending;
  assert.equal(t.closed,0);assert.equal(t.refreshes,1);
});
