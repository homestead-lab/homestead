'use strict';
const test=require('node:test'),assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm');
function setup(){
  const fields={},sent=[],timers=[];let closed=false,resolve;
  const plan={request_id:'review',capacity_token:'token',nodes:[{node:'host-1',owned_pages:1024,baseline:256,phase:'prepared'},{node:'host-2',owned_pages:0,phase:'retained',reason:'Legacy pool <unknown>'}]};
  const item={id:'cleanup-1',kind:'longhorn-v2-cleanup',status:'running',message:'V2 stopped',nodes:[{...plan.nodes[0],phase:'restart-required'},plan.nodes[1]]};
  function register(body){for(const [tag,id]of body.matchAll(/<[^>]*\bid="([^"]+)"[^>]*>/g)){
    let value='';const el={checked:false,disabled:tag.includes('disabled')};Object.defineProperty(el,'innerHTML',{get:()=>value,set:v=>{value=v;register(v);}});fields['#'+id]=el;
  }}
  const c={window:null,console,esc:v=>String(v??'').replaceAll('&','&amp;').replaceAll('<','&lt;').replaceAll('>','&gt;').replaceAll('"','&quot;'),jsArg:v=>JSON.stringify(v),$:s=>fields[s],clearTimeout(){},setTimeout:f=>timers.push(f),toast(){},modal(title,body){Object.keys(fields).forEach(k=>delete fields[k]);closed=false;fields['#modal']={classList:{contains:()=>closed}};register(body);},api:async(path,opts)=>{if(!opts)return path.includes('/status')?item:plan;sent.push(JSON.parse(opts.body));return new Promise(r=>{resolve=r;});}};
  c.window=c;vm.createContext(c);for(const name of ['ui','longhorn-v2-setup'])vm.runInContext(fs.readFileSync('web/js/'+name+'.js','utf8'),c);
  return {c,fields,sent,timers,plan,item,close:()=>{closed=true;},resolve:()=>resolve(item)};
}
test('cleanup review distinguishes owned and retained memory and requires confirmation',async()=>{
  const t=setup();await t.c.lhV2CleanupOpen();const html=t.fields['#v2Cleanup'].innerHTML;
  assert.match(html,/2048 MiB/);assert.match(html,/512 MiB kept/);assert.match(html,/Legacy pool &lt;unknown&gt;/);
  assert.match(html,/cordoned/);assert.match(html,/reviewed separately/);assert.equal(t.fields['#v2CleanupStart'].disabled,true);
  await t.c.lhV2CleanupStart();assert.equal(t.sent.length,0);
  t.fields['#v2CleanupAck'].checked=true;const pending=t.c.lhV2CleanupStart();await t.c.lhV2CleanupStart();
  assert.equal(t.sent.length,1);assert.equal(t.sent[0].capacity_token,'token');t.close();t.resolve();await pending;
  assert.equal(t.fields['#v2CleanupStatus'],undefined);
});
test('cleanup status offers reviewed restart without submitting a power action',async()=>{
  const t=setup();await t.c.lhV2CleanupWatch('cleanup-1');const html=t.fields['#v2CleanupStatus'].innerHTML;
  assert.match(html,/Review restart/);assert.match(html,/nodePowerReview\(&quot;host-1&quot;/);assert.equal(t.sent.length,0);
  t.item.status='succeeded';t.item.nodes[0].phase='complete';await t.c.lhV2CleanupWatch('cleanup-1');
  assert.doesNotMatch(t.fields['#v2CleanupStatus'].innerHTML,/Review restart/);
});
test('late cleanup review cannot overwrite a closed or replaced dialog',async()=>{
  const t=setup();let resolve;t.c.api=()=>new Promise(r=>{resolve=r;});const pending=t.c.lhV2CleanupOpen();
  t.c.modal('Different','<div id="different">Keep</div>');resolve(t.plan);await pending;
  assert.ok(t.fields['#different']);assert.equal(t.fields['#v2Cleanup'],undefined);
});
