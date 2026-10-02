'use strict';
const test = require('node:test'), assert = require('node:assert/strict');
const fs = require('node:fs'), vm = require('node:vm'), {webcrypto} = require('node:crypto');

function setup(method='full-copy') {
  let html='', pending, timer;
  const fields={'#snapshotFilesProgress':{innerHTML:''}}, sent=[];
  const plan={volume:'vol',snapshot:'daily',claim:'data',namespace:'lab',size:'1073741824',method,engine:method==='linked-clone'?'v2':'v1',review_token:'token'};
  const c={console,crypto:webcrypto,window:null,FILEVIEW:{},fileSize:()=> '1 GiB',fileBrowse(){},
    $:s=>fields[s],esc:s=>String(s??'').replaceAll('&','&amp;').replaceAll('<','&lt;').replaceAll('>','&gt;').replaceAll('"','&quot;'),
    modal:(_,body)=>{html=body;},closeModal(){},toast(){},setTimeout:f=>{timer=f;return 1;},clearTimeout(){timer=null;},
    api:async(path,opts)=> {
      if(path.includes('/plan?')) return plan;
      if(opts?.method==='POST') {sent.push({path,body:JSON.parse(opts.body)});if(path.endsWith('/start') && pending) return pending;return {state:'preparing',stage:'clone',source:plan};}
      return {state:'preparing',stage:'clone',source:plan};
    }};
  c.window=c; vm.createContext(c);
  for(const file of ['ui','snapshot-files']) vm.runInContext(fs.readFileSync(`web/js/${file}.js`,'utf8'),c);
  return {c,plan,sent,fields,html:()=>html,pending:p=>{pending=p;},timer:()=>timer};
}

test('review explains linked clones separately from the full copy cost',async()=>{
  const full=setup();await full.c.snapshotFiles('vol','daily');
  assert.match(full.html(),/temporary copy is required/);
  const linked=setup('linked-clone');await linked.c.snapshotFiles('vol','daily');
  assert.match(linked.html(),/no full data copy/);
  assert.match(linked.html(),/depends on the original snapshot/);
});
test('double clicks submit one request with the reviewed snapshot',async()=>{
  const t=setup();await t.c.snapshotFiles('vol','daily');
  await Promise.all([t.c.snapshotFilesStart(),t.c.snapshotFilesStart()]);
  assert.equal(t.sent.length,1);assert.equal(t.sent[0].body.review_token,'token');
  assert.match(t.sent[0].body.request_id,/^[a-f0-9]{24}$/);
});
test('progress uses observed percentage and leaves unknown progress indeterminate',()=>{
  const t=setup();
  t.c.snapshotFilesPaint({state:'preparing',stage:'clone',message:'<error>',percent:42,source:t.plan});
  assert.match(t.fields['#snapshotFilesProgress'].innerHTML,/aria-valuenow="42"/);
  assert.match(t.fields['#snapshotFilesProgress'].innerHTML,/&lt;error&gt;/);
  t.c.snapshotFilesPaint({state:'preparing',stage:'clone',message:'Waiting',percent:null,source:t.plan});
  assert.doesNotMatch(t.fields['#snapshotFilesProgress'].innerHTML,/aria-valuenow|100%/);
});
test('a late start after closing requests cleanup without reopening the browser',async()=>{
  const t=setup();let done;t.pending(new Promise(resolve=>{done=resolve;}));
  await t.c.snapshotFiles('vol','daily');const starting=t.c.snapshotFilesStart();
  await t.c.snapshotFilesDismiss();const before=t.html();
  done({state:'ready',namespace:'lab',session:'late',source:t.plan});await starting;
  assert.equal(t.html(),before);
  assert.equal(t.sent.filter(x=>x.path.endsWith('/close')).length,2);
  assert.equal(t.c.FILEVIEW.snapshotSession,null);
});
test('snapshot download URL encodes path characters instead of executing markup',()=>{
  const t=setup();t.c.FILEVIEW.snapshotSession={namespace:'lab',session:'session'};
  const url=t.c.snapshotFileUrl('dir/a&b?<script>.txt');
  assert.equal(new URL(url,'https://example.test').searchParams.get('path'),'dir/a&b?<script>.txt');
  assert.doesNotMatch(url,/<script>/);
});
