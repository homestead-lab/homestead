'use strict';
const test=require('node:test'),assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm');
function setup() {
  const g=1073741824,fields={},sent=[],timers=[];let closed=false,html='',pending=null;
  const plan={node:'host-1',vg:'system-vg',total_bytes:256*g,free_bytes:64*g,extent_bytes:4194304,role:'os',size_gib:80,target_bytes:80*g,request_id:'a'.repeat(24),capacity_token:'review-token',
    areas:[{role:'os',name:'Host OS',size_bytes:64*g,fs_bytes:64*g,used_bytes:24*g,reserved_bytes:2*g,filesystem:'ext4',mountpoint:'/',device:'/dev/dm-0',stable_device:'/dev/disk/by-id/dm-uuid-LVM-example-root',fs_uuid:'example-root-uuid'},
      {role:'longhorn',name:'Longhorn',size_bytes:128*g,fs_bytes:128*g,used_bytes:60*g,reserved_bytes:4*g,filesystem:'ext4',mountpoint:'/var/lib/longhorn'}]};
  const item={id:'allocation-1',kind:'disk-allocation',node:'host-1',role:'os',mountpoint:'/',target_bytes:80*g,phase:'growing',status:'running',progress:50,message:'Growing the filesystem'};
  const recovery={...plan,operation_id:item.id,action:'finish-filesystem',area:{...plan.areas[0],size_bytes:80*g},capacity_token:'finish-token'};
  function register(body) {
    for(const [tag,id] of body.matchAll(/<[^>]*\bid="([^"]+)"[^>]*>/g)) {
      const field={value:tag.startsWith('<select')?'os':'',checked:false,disabled:tag.includes('disabled')};let content='';
      Object.defineProperty(field,'innerHTML',{get:()=>content,set:value=>{content=value;register(value);}});fields['#'+id]=field;
    }
  }
  function modal(title,body) {html=body;Object.keys(fields).forEach(key=>delete fields[key]);fields['#modal']={classList:{contains:()=>closed}};closed=false;register(body);}
  const c={window:null,console,esc:value=>String(value??'').replaceAll('&','&amp;').replaceAll('<','&lt;').replaceAll('>','&gt;').replaceAll('"','&quot;'),jsArg:value=>JSON.stringify(value),
    $:key=>fields[key],modal,toast(){},noteOperation(){},clearTimeout(){},setTimeout:fn=>{timers.push(fn);return timers.length;},api:async(path,init)=>{
      if(!init)return item;const body=JSON.parse(init.body);sent.push({path,body});
      if(path.endsWith('/review'))return plan;if(path.endsWith('/recheck'))return recovery;
      return new Promise(resolve=>{pending=resolve;});
    }};
  c.window=c;vm.createContext(c);vm.runInContext(fs.readFileSync('web/js/ui.js','utf8'),c);vm.runInContext(fs.readFileSync('web/js/disk-allocation.js','utf8'),c);
  return {c,fields,sent,plan,item,recovery,html:()=>html,close:()=>{closed=true;},resolve:()=>pending(item),timers};
}
test('allocation preview preserves siblings and distinguishes LVM free space from filesystem usage',async()=>{
  const t=setup();await t.c.diskAllocationOpen('host-1');
  const html=t.fields['#allocationPreview'].innerHTML;
  assert.match(html,/80.0 GiB/);assert.match(html,/48.0 GiB/);assert.match(html,/128.0 GiB/);
  assert.match(html,/do not show where the partitions are on the disk/);assert.match(t.fields['#allocationBody'].innerHTML,/not available here/);
  assert.equal(t.sent.length,1);
});
test('no free extents or invalid size disables growth',async()=>{
  const t=setup();t.plan.free_bytes=0;t.plan.total_bytes=192*1073741824;await t.c.diskAllocationOpen('host-1');
  assert.equal(t.fields['#allocationReview'].disabled,true);assert.match(t.fields['#allocationPreview'].innerHTML,/no whole GiB/);
  await t.c.diskAllocationReview();assert.equal(t.sent.length,1);
});
test('review reads fresh inventory before confirmation and does not grow automatically',async()=>{
  const t=setup();await t.c.diskAllocationOpen('host-1');await t.c.diskAllocationReview();
  assert.equal(t.sent.length,2);assert.equal(t.sent[1].body.size_gib,80);assert.equal(t.fields['#allocationApply'].disabled,true);
  await t.c.diskAllocationApply(false);assert.equal(t.sent.length,2);
});
test('approval dispatch is single use and never posts unreviewed inventory fields',async()=>{
  const t=setup();await t.c.diskAllocationOpen('host-1');await t.c.diskAllocationReview();t.fields['#allocationAck'].checked=true;
  const work=t.c.diskAllocationApply(false);await t.c.diskAllocationApply(false);
  assert.equal(t.sent.length,3);assert.deepEqual(Object.keys(t.sent[2].body).sort(),['capacity_token','confirm_capacity','node','request_id','role','size_gib'].sort());
  t.resolve();await work;assert.ok(t.fields['#allocationStatus']);
});
test('closing during dispatch keeps the dialog closed',async()=>{
  const t=setup();await t.c.diskAllocationOpen('host-1');await t.c.diskAllocationReview();t.fields['#allocationAck'].checked=true;
  const work=t.c.diskAllocationApply(false);const before=t.html();t.close();t.resolve();await work;assert.equal(t.html(),before);
});
test('failure stops polling and routes recovery through its own confirmation',async()=>{
  const t=setup();t.item.status='failed';t.item.message='<script>unsafe source</script>';
  await t.c.diskAllocationWatch(t.item.id);assert.equal(t.timers.length,0);assert.match(t.fields['#allocationStatus'].innerHTML,/&lt;script&gt;/);
  await t.c.diskAllocationRecheck(t.item.id);assert.match(t.html(),/without allocating more space/);
  t.fields['#allocationAck'].checked=true;const work=t.c.diskAllocationApply(true);
  assert.equal(t.sent.at(-1).path,'/api/disks/allocation/finish');assert.equal(t.sent.at(-1).body.action,'finish-filesystem');assert.equal(t.sent.at(-1).body.capacity_token,'finish-token');
  t.resolve();await work;
});
test('allocation diagrams escape volume group labels',()=>{
  const t=setup();t.plan.vg='<img src=x onerror=bad>';
  const html=t.c.allocationDiagram(t.plan,'os',80*1073741824);assert.doesNotMatch(html,/<img/);assert.match(html,/&lt;img/);
});
test('a rechecked job with no applied growth is shown as settled',async()=>{
  const t=setup();Object.assign(t.item,{status:'cancelled',phase:'cancelled',message:'No logical-volume growth was observed.'});
  await t.c.diskAllocationWatch(t.item.id);
  const html=t.fields['#allocationStatus'].innerHTML;assert.match(html,/No growth applied/);assert.match(html,/View allocation/);
  assert.doesNotMatch(html,/Recheck outcome/);assert.equal(t.timers.length,0);
});
