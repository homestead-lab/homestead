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
function held({forward=true}={}) {
  const t=setup();
  t.ctx.api=async(path,opts)=>{const body=JSON.parse(opts.body);t.calls.push({path,body});
    if(path.endsWith('/preview'))return {tokens:{...(forward?{retry:'retry-token',continue:'retry-token'}:{}),rollback:'rollback-token'},plan:{
      id:body.id,claim:'data',phase:forward?'copy':'stop',status:'failed',can_continue:forward,can_retry:forward,can_finish:false,can_rollback:true,can_pause:false,
      resources:[],workloads:[],warnings:[],blockers:forward?[]:['A workload changed after review'],message:'Held',rollback:{blockers:[],next_write:null}}};
    return {ok:true,detail:'Queued'};};
  t.fields['#storageRecoveryRollback']={};
  return t;
}
test('a held move offers retry and roll back, each sent with its own token',async()=>{
  const t=held();await t.ctx.storageRecoveryReview('job');
  const html=t.fields['#storageRecoveryContent'].innerHTML;
  assert.match(html,/Retry this step/);assert.match(html,/Roll back to the original/);
  t.fields['#storageRecoveryAck'].checked=true;await t.ctx.storageRecoveryApply('rollback');
  assert.deepEqual(t.calls.at(-1).body,{id:'job',action:'rollback',capacity_token:'rollback-token',confirm_capacity:true});
  await t.ctx.storageRecoveryReview('job');t.fields['#storageRecoveryAck'].checked=true;await t.ctx.storageRecoveryApply();
  assert.equal(t.calls.at(-1).body.action,'retry');
});
test('when the move cannot go forward, roll back is the action and the reason is shown',async()=>{
  const t=held({forward:false});await t.ctx.storageRecoveryReview('job');
  const html=t.fields['#storageRecoveryContent'].innerHTML;
  assert.match(html,/changed after review/);assert.match(html,/Roll back to the original/);assert.doesNotMatch(html,/Retry this step/);
  t.fields['#storageRecoveryAck'].checked=true;await t.ctx.storageRecoveryApply();
  assert.equal(t.calls.at(-1).body.action,'rollback');
});
test('an action without its token is never sent',async()=>{
  const t=held();await t.ctx.storageRecoveryReview('job');t.fields['#storageRecoveryAck'].checked=true;
  await t.ctx.storageRecoveryApply('finish');assert.equal(t.calls.length,1);
});
