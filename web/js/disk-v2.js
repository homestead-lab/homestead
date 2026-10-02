/* A separate approval for evacuation and for erasing a dedicated V1 disk. */
let diskV2Sequence=0, diskV2Timer=null, diskV2Approval=null, diskV2Busy=false;
const diskV2GiB = bytes => `${(Number(bytes)/1073741824).toFixed(1)} GiB`;
const diskV2Post = (path, body) => api(`/api/disks/v2/${path}`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
function diskV2Reset() {
  clearTimeout(diskV2Timer); ++diskV2Sequence; diskV2Approval=null;
  if(window.__logTimer) {clearInterval(window.__logTimer);window.__logTimer=null;}
}
function diskV2Volumes(rows, destinations=false) {
  const volumes=[...new Map(rows.map(r=>[r.volume,r])).values()];
  return UI.more(`${volumes.length} affected volume${volumes.length===1?'':'s'}`,UI.table(
    [{label:'Volume'},{label:'Replica size'},{label:destinations?'Eligible V1 destination':'Healthy replicas required'}],
    volumes.map(r=>[esc(r.volume),esc(diskV2GiB(r.size_bytes)),destinations?esc(r.destination):esc(r.replicas)])) +
    (destinations?'<p class="ui-help">This checks room for full replicas, tags and placement rules. Longhorn chooses the actual destinations. Keep V1 disks available until evacuation finishes.</p>':''));
}
window.diskV2Open = async (node,disk) => {
  diskV2Reset(); const sequence=diskV2Sequence;
  if(typeof MODAL_STACK!=='undefined') MODAL_STACK.length=0;
  modal('Prepare a disk for V2','<div id="diskV2Review"><div class="empty"><span class="spin2"></span>Checking disk and V1 capacity</div>'+UI.actions(UI.cancel('Close'))+'</div>',true,'disk-v2');
  const host=$('#diskV2Review');
  try {
    const plan=await diskV2Post('plan',{node,disk});
    if(sequence!==diskV2Sequence || $('#diskV2Review')!==host || $('#modal')?.classList.contains('hidden')) return;
    if(plan.operation) return diskV2Watch(plan.operation.id);
    diskV2Approval={node,disk,request_id:plan.request_id,capacity_token:plan.capacity_token};
    host.innerHTML=UI.lead('Move this disk’s V1 replicas elsewhere, then prepare the device for V2.') +
      UI.steps([{title:'Evacuate V1'},{title:'Review erase'},{title:'V2 ready'}],0) +
      UI.facts([['Host',esc(plan.node)],['Device',`<span class="mono">${esc(plan.device)}</span>`],['Disk size',esc(diskV2GiB(plan.size_bytes))],['Next step',plan.blockers.length?UI.chip('Needs attention','warn'):UI.chip('Ready to evacuate','ok')]]) +
      (plan.blockers.length?UI.callout('warn','Evacuation cannot start',`<ul class="ui-list">${plan.blockers.map(b=>`<li>${esc(b)}</li>`).join('')}</ul>`):UI.callout('info','Erasing needs a second approval','Evacuation disables new replicas and keeps the filesystem mounted. Review erasing the device after its replicas are healthy elsewhere.')) +
      diskV2Volumes(plan.volumes,true) +
      UI.more('V1 and V2 capacity','<p class="ui-help">A V2 disk cannot receive V1 replicas. If every eligible host already holds a replica, add a spare V1 disk on the host being cleared or add another eligible V1 host. Preparing a disk does not change any volume’s data engine.</p>') +
      (!plan.blockers.length?UI.ack('diskV2Confirm','Disable new replicas and move existing replicas off this disk',{onchange:"$('#diskV2Begin').disabled=!this.checked"}):'') +
      UI.actions(UI.cancel('Close')+UI.button('Move replicas off','diskV2Begin()',{kind:'pri',id:'diskV2Begin',disabled:true,attrs:'data-need="admin"'}),UI.button('Check again',`diskV2Open(${jsq(node)},${jsq(disk)})`));
    window.applyRole?.();
  } catch(e) {if(sequence===diskV2Sequence && $('#diskV2Review')===host) host.innerHTML=UI.callout('warn','Disk review unavailable',esc(e.message))+UI.actions(UI.button('Check again',`diskV2Open(${jsq(node)},${jsq(disk)})`) + (e.message.startsWith('Set up V2')?UI.button('Set up V2','lhV2Setup()',{kind:'pri',attrs:'data-need="admin"'}):''));}
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
    (item.needs_erase_review?UI.callout('info','Evacuation finished','Healthy replacement replicas are confirmed. Nothing is erased until you complete a fresh device review.'):item.status==='failed'?UI.callout('warn','Inspect before continuing','Preparation is not retried automatically. Keep this task and its log while checking the device and host receipt; partial changes may remain.'):done?UI.callout('ok','Block disk ready','Longhorn has verified the V2 disk. Existing volumes keep their engine. Use Change storage class on a volume to copy it into a V2 class. This stops its workloads.'):item.status==='cancelled'?UI.callout('info','Filesystem retained','Replacement replicas remain. This disk stays disabled for new replicas; allow them again from the disk’s actions when ready.'):'') +
    diskV2Volumes(item.volumes) +
    UI.actions(UI.cancel('Close') +
      (item.needs_erase_review?UI.button('Review erase',`diskV2EraseReview(${jsq(item.id)})`,{kind:'pri',attrs:'data-need="admin"'}):done?UI.button('V2 storage class',"closeModal();storageClassCreate({engine:'v2'})",{kind:'pri',attrs:'data-need="admin"'}):''),
      actionBar([{label:'Task log',run:`diskV2Log(${jsq(item.id)})`},item.cancellable && {label:'Stop evacuation',need:'admin',run:`diskV2Cancel(${jsq(item.id)})`}],{shown:1,label:'Task actions'}));
  window.applyRole?.();
}
window.diskV2EraseReview = async id => {
  diskV2Reset();const sequence=diskV2Sequence;
  try {
    const plan=await diskV2Post('prepare-review',{id});
    if(sequence!==diskV2Sequence || !$('#diskV2Status') || $('#modal')?.classList.contains('hidden')) return;
    diskV2Approval=plan;
    childModal('Erase and prepare device',UI.lead(`V1 replicas have moved off <b class="mono">${esc(plan.device)}</b>. Review the device before preparing it for V2.`) +
      UI.facts([['Host',esc(plan.node)],['Device',`<span class="mono">${esc(plan.device)}</span>`],['Size',esc(diskV2GiB(plan.size_bytes))],['Result','Longhorn V2 block disk']]) +
      UI.callout('warn','The filesystem will be erased','Remove the V1 entry, unmount this filesystem, remove its mount from fstab and erase its signatures. Its old replica files become inaccessible. The blank device will be registered with V2.') +
      UI.field(`Type ${plan.device} to confirm`,`<input id="diskV2Device" autocomplete="off" spellcheck="false" oninput="diskV2EraseReady()" aria-label="Confirm device to erase">`) +
      UI.ack('diskV2EraseConfirm','Erase this evacuated device and use it for V2',{onchange:'diskV2EraseReady()'}) +
      UI.actions(UI.button('Back to task',`diskV2Watch(${jsq(id)})`) +UI.button('Erase and prepare','diskV2Erase()',{kind:'danger',id:'diskV2Erase',disabled:true,attrs:'data-need="admin"'})),true,'disk-v2');
    if(typeof MODAL_STACK!=='undefined' && MODAL_STACK.length) MODAL_STACK[MODAL_STACK.length-1].onReturn=()=>diskV2Watch(id);
    window.applyRole?.();
  } catch(e) {toast(e.message,'bad');if(sequence===diskV2Sequence && $('#diskV2Status') && !$('#modal')?.classList.contains('hidden')) await diskV2Watch(id);}
};
window.diskV2EraseReady = () => {
  const button=$('#diskV2Erase');if(button) button.disabled=diskV2Busy || !diskV2Approval || !$('#diskV2EraseConfirm')?.checked || $('#diskV2Device')?.value!==diskV2Approval.device;
};
window.diskV2Erase = async () => {
  if(diskV2Busy || !diskV2Approval || !$('#diskV2EraseConfirm')?.checked || $('#diskV2Device')?.value!==diskV2Approval.device || $('#modal')?.classList.contains('hidden')) return;
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
