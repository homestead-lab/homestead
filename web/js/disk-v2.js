/* A separate approval for evacuation and for erasing a dedicated V1 disk. */
let diskV2Sequence=0, diskV2Timer=null, diskV2Approval=null, diskV2Busy=false;
const diskV2GiB = bytes => `${(Number(bytes)/1073741824).toFixed(1)} GiB`;
function diskV2Region(plan, scope={}) {
  const root=plan.layout;
  if (!root) return '';
  const selected=row=>row.path===plan.device;
  const contains=row=>selected(row)||(row.children||[]).some(contains);
  const label=row=>selected(row)?(scope.label||'Erase contents'):contains(row)?'Contains selected volume':'Keep';
  const physical=(root.children||[]).filter(r=>r.kind==='part').sort((a,b)=>(a.start_bytes||0)-(b.start_bytes||0));
  const directLogical=(root.children||[]).filter(r=>r.kind!=='part');
  const segment=(row,width)=>`<span class="pmap-seg ${selected(row)?(scope.label?'allocation-selected':'erase'):contains(row)?'lvm':'sys'}" style="flex:${Math.max(0.01,width)}" title="${esc(row.path+' - '+label(row))}"><span>${esc(label(row))}</span></span>`;
  let cursor=0,bar='';
  for(const row of physical) {
    const start=Number(row.start_bytes)||cursor;
    if(start>cursor) bar+=`<span class="pmap-seg free" style="flex:${start-cursor}" title="Other space - keep"></span>`;
    bar+=segment(row,row.size_bytes);cursor=start+row.size_bytes;
  }
  if(directLogical.length) bar=segment(root,root.size_bytes);
  else if(root.size_bytes>cursor) bar+=`<span class="pmap-seg free" style="flex:${root.size_bytes-cursor}" title="Other space - keep"></span>`;
  function rows(row,depth=0) {
    const children=row.children||[],logical=row.kind==='lvm',isSelected=selected(row);
    return `<div class="disk-v2-layout-row ${isSelected?(scope.label?'maintenance-selected':'selected'):''}">
      <div><strong>${esc(row.path)}</strong><span>${esc(logical?'Logical volume':row.kind==='part'?'Partition':'Disk')}${row.mountpoint?' - '+esc((row.mountpoint==='/'?'Host OS ':row.mountpoint.startsWith('/boot')?'Boot ':'')+row.mountpoint):''}</span></div>
      <span>${esc(diskV2GiB(row.size_bytes))}</span><b>${esc(label(row))}</b>
    </div>`+(children.length?`<div class="disk-v2-layout-children">${children.some(r=>r.kind==='lvm')?'<p class="ui-help">Logical volumes share this '+(row.kind==='disk'?'disk':'partition')+': the order below is not their position on it.</p>':''}${children.map(r=>rows(r,depth+1)).join('')}</div>`:'');
  }
  return `<div class="disk-v2-layout" aria-label="${esc(scope.label?'Disk layout and maintenance scope':'Disk layout and erase scope')}">
    <div class="sec">${esc(root.path)} - ${esc(diskV2GiB(root.size_bytes))}</div>
    <div class="pmap" role="img" aria-label="${esc(scope.label?'Physical partitions. The list identifies the selected logical volume and the regions to keep.':'Physical partitions. The list below identifies the contents to erase and the regions to keep.')}">${bar}</div>
    ${directLogical.length?rows(root):physical.map(row=>rows(row)).join('')}
    <p class="ui-help">${scope.summary?esc(scope.summary):`Erase all contents of <strong>${esc(plan.device)}</strong> (${esc(plan.mountpoint||'dedicated Longhorn filesystem')}). Keep its size and boundary.`}</p>
  </div>`+(scope.label?'':UI.more('Device identity and checks',UI.facts([['Stable device',`<span class="disk-v2-identity mono">${esc(plan.stable_device||plan.device)}</span>`],['Disk access','AIO; Linux keeps control of the physical disk']])+ '<p class="ui-help">System files, shared mounts or leftover replica files stop it. Other partitions and logical volumes are kept.</p>'));
}
const diskV2Post = (path, body) => api(`/api/disks/v2/${path}`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
function diskV2Reset() {
  clearTimeout(diskV2Timer); ++diskV2Sequence; diskV2Approval=null;
  if(window.__logTimer) {window.__logTimer.stop?.();window.__logTimer=null;}
}
function diskV2Volumes(rows, destinations=false) {
  const volumes=[...new Map(rows.map(r=>[r.volume,r])).values()];
  return UI.more(`${volumes.length} affected volume${volumes.length===1?'':'s'}`,UI.table(
    [{label:'Volume'},{label:'Replica size'},{label:destinations?'Eligible V1 destination':'Healthy replicas required'}],
    volumes.map(r=>[esc(r.volume),esc(diskV2GiB(r.size_bytes)),destinations?esc(r.destination):esc(r.replicas)])) +
    (destinations?'<p class="ui-help">Longhorn picks the actual disks. Keep V1 disks online until it finishes.</p>':''));
}
window.diskV2Open = async (node,disk) => {
  diskV2Reset(); const sequence=diskV2Sequence;
  if(typeof MODAL_STACK!=='undefined') MODAL_STACK.length=0;
  modal('Prepare a disk for V2',UI.head({kind:'Disk',name:disk,where:node})+'<div id="diskV2Review"><div class="empty"><span class="spin2"></span>Checking disk and V1 capacity</div>'+UI.actions(UI.cancel('Close'))+'</div>',true,'disk-v2');
  const host=$('#diskV2Review');
  try {
    const plan=await diskV2Post('plan',{node,disk});
    if(sequence!==diskV2Sequence || $('#diskV2Review')!==host || $('#modal')?.classList.contains('hidden')) return;
    if(plan.operation) return diskV2Watch(plan.operation.id);
    diskV2Approval={node,disk,request_id:plan.request_id,capacity_token:plan.capacity_token};
    host.innerHTML=UI.lead('Move its V1 replicas off, then prepare it for V2.') +
      UI.steps([{title:'Evacuate V1'},{title:'Review erase'},{title:'V2 ready'}],0) +
      UI.facts([['Host',esc(plan.node)],[plan.region_kind==='lvm'?'Logical volume':plan.region_kind==='part'?'Partition':'Device',`<span class="mono">${esc(plan.device)}</span>`],['Disk size',esc(diskV2GiB(plan.size_bytes))],['Next step',plan.blockers.length?UI.chip('Needs attention','warn'):UI.chip('Ready to evacuate','ok')]]) +
      (plan.blockers.length?UI.callout('warn','Evacuation cannot start',`<ul class="ui-list">${plan.blockers.map(b=>`<li>${esc(b)}</li>`).join('')}</ul>`):UI.callout('info','Erasing is approved separately','Nothing is erased now. Once its replicas are healthy elsewhere, you review the erase.')) +
      diskV2Region(plan) + diskV2Volumes(plan.volumes,true) +
      UI.more('V1 and V2 capacity','<p class="ui-help">A V2 disk takes no V1 replicas. If every host already holds a copy, add a V1 disk or host first. No volume changes engine.</p>') +
      (!plan.blockers.length?UI.ack('diskV2Confirm','Stop new replicas here and move these off',{onchange:"$('#diskV2Begin').disabled=!this.checked"}):'') +
      UI.actions(UI.cancel('Close')+UI.button('Move replicas off','diskV2Begin()',{kind:'pri',id:'diskV2Begin',disabled:true,attrs:'data-need="admin"'}),UI.button('Check again',`diskV2Open(${jsArg(node)},${jsArg(disk)})`));
    window.applyRole?.();
  } catch(e) {
    if(sequence!==diskV2Sequence || $('#diskV2Review')!==host)return;
    const runtime=e.message.includes('Longhorn runtime storage shares');
    host.innerHTML=UI.callout('warn',runtime?'Move Longhorn runtime before preparing V2':'Disk review unavailable',esc(e.message))+
      UI.actions(UI.cancel('Close')+(runtime?UI.button('Review runtime move',`runtimeMoveOpen(${jsArg(node)},${jsArg(disk)})`,{kind:'pri',attrs:'data-need="admin"'}):
        e.message.startsWith('Set up V2')?UI.button('Set up V2','lhV2Setup()',{kind:'pri',attrs:'data-need="admin"'}):UI.button('Check again',`diskV2Open(${jsArg(node)},${jsArg(disk)})`)));
    window.applyRole?.();
  }
};
window.diskV2Begin = async () => {
  if(diskV2Busy || !diskV2Approval || !$('#diskV2Confirm')?.checked || !$('#diskV2Review') || $('#modal')?.classList.contains('hidden')) return;
  const body=diskV2Approval,sequence=diskV2Sequence; diskV2Approval=null;diskV2Busy=true;
  $('#diskV2Begin').disabled=true;
  try {
    const item=await diskV2Post('start',{...body,confirm_capacity:true});
    window.noteOperation?.(item);
    if(sequence===diskV2Sequence && $('#diskV2Review') && !$('#modal')?.classList.contains('hidden')) await diskV2Watch(item.id);
  } catch(e) {toast(e.message+' Reopen disk preparation to check its saved task.','bad');}
  finally {diskV2Busy=false;}
};
window.diskV2Watch = async id => {
  diskV2Reset(); const sequence=diskV2Sequence;
  if(typeof MODAL_STACK!=='undefined') MODAL_STACK.length=0;
  modal('Disk preparation · Longhorn V2','<div id="diskV2Status"><div class="empty"><span class="spin2"></span>Checking saved task</div>'+UI.actions(UI.cancel('Close'))+'</div>',true,'disk-v2');
  const host=$('#diskV2Status');
  async function poll() {
    if(sequence!==diskV2Sequence || $('#diskV2Status')!==host || $('#modal')?.classList.contains('hidden')) return;
    try {
      const item=await api('/api/disks/v2/status?id='+encodeURIComponent(id));
      if(sequence!==diskV2Sequence || $('#diskV2Status')!==host || $('#modal')?.classList.contains('hidden')) return;
      diskV2Paint(item);window.noteOperation?.(item);
      if(['succeeded','failed','cancelled'].includes(item.status)) return;
    } catch(e) {if($('#diskV2Status')===host) {host.__snapshot=null;host.innerHTML=UI.callout('warn','Task status unavailable',esc(e.message)+' Checking again; completion is not assumed.');}}
    if(sequence===diskV2Sequence && $('#diskV2Status')===host) diskV2Timer=setTimeout(poll,4000);
  }
  await poll();
};
function diskV2Paint(item) {
  const host=$('#diskV2Status'); if(!host) return;
  const snapshot=JSON.stringify(item);if(host.__snapshot===snapshot) return;host.__snapshot=snapshot;
  const stopped=['failed','cancelled'].includes(item.status),done=item.status==='succeeded';
  const stage=done?3:item.phase==='evacuating'?0:item.phase==='awaiting-erase'?1:2;
  const label=done?'Ready for V2':item.status==='failed'?'Preparation stopped':item.status==='cancelled'?'Evacuation stopped':item.needs_erase_review?'Ready for erase review':'In progress';
  host.innerHTML=UI.lead(esc(item.message)) + UI.steps([{title:'Evacuate V1'},{title:'Review erase'},{title:'V2 ready'}],stage) +
    UI.facts([['Host',esc(item.node)],['Device',`<span class="mono">${esc(item.device)}</span>`],['State',UI.chip(label,done || item.needs_erase_review?'ok':stopped?'warn':'')],['V1 replicas on disk',esc(item.remaining)]]) +
    (!stopped && !item.needs_erase_review?UI.progress(item.progress,{label:'Disk preparation',detail:'Reopen this saved task from Jobs.'}):'') +
    (item.needs_erase_review?UI.callout('info','Evacuation finished','Its replicas are healthy elsewhere. Nothing is erased until you review it.'):item.status==='failed'?UI.callout('warn','Inspect before continuing','It is not retried. Some changes may remain: check the device and the task log.'):done?UI.callout('ok','Block disk ready','Volumes keep their engine. Change storage class copies one to V2, stopping its apps.'):item.status==='cancelled'?UI.callout('info','Filesystem kept','The disk takes no new replicas; allow them again from its actions.'):'') +
    diskV2Volumes(item.volumes) +
    UI.actions(UI.cancel('Close') +
      (item.needs_erase_review?UI.button('Review erase',`diskV2EraseReview(${jsArg(item.id)})`,{kind:'pri',attrs:'data-need="admin"'}):done?UI.button('V2 storage class',"closeModal();storageClassCreate({engine:'v2'})",{kind:'pri',attrs:'data-need="admin"'}):''),
      actionBar([{label:'Task log',run:`diskV2Log(${jsq(item.id)})`},item.cancellable && {label:'Stop evacuation',need:'admin',run:`diskV2Cancel(${jsq(item.id)})`}],{shown:1,label:'Task actions'}));
  window.applyRole?.();
}
window.diskV2EraseReview = async id => {
  diskV2Reset();const sequence=diskV2Sequence;
  try {
    const plan=await diskV2Post('prepare-review',{id});
    if(sequence!==diskV2Sequence || !$('#diskV2Status') || $('#modal')?.classList.contains('hidden')) return;
    diskV2Approval=plan;
    childModal('Erase and prepare device',UI.head({kind:plan.region_kind==='lvm'?'Logical volume':plan.region_kind==='part'?'Partition':'Device',name:plan.device,where:plan.node})+UI.lead(`Its V1 replicas have moved off. Check the device, then erase it for V2.`) +
      UI.facts([['Host',esc(plan.node)],[plan.region_kind==='lvm'?'Logical volume':plan.region_kind==='part'?'Partition':'Device',`<span class="mono">${esc(plan.device)}</span>`],['Size',esc(diskV2GiB(plan.size_bytes))],['Result','Longhorn V2 block disk']]) +
      UI.callout('warn','The filesystem will be erased','It is unmounted, dropped from fstab and wiped, then added to V2. Leftover files stop it.') +
      diskV2Region(plan) + UI.typedConfirm({ id: 'diskV2Device', expect: plan.device, oninput: 'diskV2EraseReady()' }) +
      UI.ack('diskV2EraseConfirm','Erase it and use it for V2',{onchange:'diskV2EraseReady()'}) +
      UI.actions(UI.button('Back to task',`diskV2Watch(${jsArg(id)})`, { attrs: 'data-dialog-dismiss="true"' }) +UI.button('Erase and prepare','diskV2Erase()',{kind:'danger',id:'diskV2Erase',disabled:true,attrs:'data-need="admin"'})),true,'disk-v2');
    if(typeof MODAL_STACK!=='undefined' && MODAL_STACK.length) MODAL_STACK[MODAL_STACK.length-1].onReturn=()=>diskV2Watch(id);
    window.applyRole?.();
  } catch(e) {toast(e.message,'bad');if(sequence===diskV2Sequence && $('#diskV2Status') && !$('#modal')?.classList.contains('hidden')) await diskV2Watch(id);}
};
window.diskV2EraseReady = () => {
  const button=$('#diskV2Erase');if(button) button.disabled=diskV2Busy || !diskV2Approval || !$('#diskV2EraseConfirm')?.checked || !typedConfirmMatches('diskV2Device');
};
window.diskV2Erase = async () => {
  if(diskV2Busy || !diskV2Approval || !$('#diskV2EraseConfirm')?.checked || !typedConfirmMatches('diskV2Device') || $('#modal')?.classList.contains('hidden')) return;
  const approval=diskV2Approval,sequence=diskV2Sequence;diskV2Approval=null;diskV2Busy=true;$('#diskV2Erase').disabled=true;
  try {
    const item=await diskV2Post('prepare',{operation_id:approval.operation_id,request_id:approval.request_id,capacity_token:approval.capacity_token,confirm_device:approval.device,confirm_capacity:true});
    window.noteOperation?.(item);
    if(sequence===diskV2Sequence && $('#diskV2EraseConfirm') && !$('#modal')?.classList.contains('hidden')) await diskV2Watch(item.id);
  } catch(e) {toast(e.message+' Open the saved task before reviewing again.','bad');}
  finally {diskV2Busy=false;}
};
window.diskV2Log = id => {diskV2Reset();pushModal(()=>diskV2Watch(id));jobLog(id);};
window.diskV2Cancel = id => {diskV2Reset();pushModal(()=>diskV2Watch(id));cancelOperation(id);};
