/* Existing installer logical volumes: review online growth, track each step. */
let diskAllocationSequence=0,diskAllocationTimer=null,diskAllocationPlan=null,diskAllocationBusy=false;
const allocationGiB=value=>`${(Number(value)/1073741824).toFixed(1)} GiB`;
const allocationPost=(path,body)=>api(`/api/disks/allocation/${path}`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
function allocationReset() {++diskAllocationSequence;clearTimeout(diskAllocationTimer);diskAllocationPlan=null;}
function allocationDiagram(plan,role,target) {
  const current=plan.areas.map(a=>({label:a.name,size:a.size_bytes,role:a.role}));
  const known=current.reduce((sum,a)=>sum+a.size,0),other=Math.max(0,plan.total_bytes-plan.free_bytes-known);
  if(other)current.push({label:'Other volumes',size:other,role:'other'});
  current.push({label:'Unallocated',size:plan.free_bytes,role:'free'});
  const selected=plan.areas.find(a=>a.role===role),increase=Math.max(0,target-selected.size_bytes);
  const proposed=current.map(a=>({...a,size:a.role===role?target:a.role==='free'?Math.max(0,a.size-increase):a.size}));
  function bar(label,rows) {
    return `<div class="allocation-bar"><strong>${label}</strong><div class="pmap" role="img" aria-label="${esc(label+': '+rows.map(a=>a.label+' '+allocationGiB(a.size)).join(', '))}">${rows.filter(a=>a.size>0).map(a=>`<span class="pmap-seg ${a.role==='free'?'free':a.role===role?'allocation-selected':'sys'}" style="flex:${a.size}" title="${esc(a.label+' '+allocationGiB(a.size))}"></span>`).join('')}</div></div>`;
  }
  return `<div class="allocation-diagram">${bar('Current',current)}${bar('Proposed',proposed)}</div>`+
    UI.table([{label:'Allocation'},{label:'Current'},{label:'Proposed'}],current.map((a,i)=>[esc(a.label),esc(allocationGiB(a.size)),esc(allocationGiB(proposed[i].size))]))+
    `<p class="ui-help">Allocation in ${esc(plan.vg)}. These bars do not show where the partitions are on the disk.</p>`;
}
function allocationLimits() {
  return UI.more('Reducing or moving space', '<p class="ui-help">This change uses unallocated LVM space. It does not move or shrink partitions. Shrinking the OS requires offline maintenance and a verified backup. Homestead does not support XFS shrinking.</p><p class="ui-help">Reclaiming Longhorn space needs every affected store and its data moved or deliberately deleted first, followed by recreating the empty area. That workflow is not available here. Free space inside a filesystem is not unallocated LVM space.</p>');
}
window.diskAllocationOpen=async node=>{
  allocationReset();const sequence=diskAllocationSequence;
  modal('Adjust storage allocation','<div id="allocationBody"><div class="empty"><span class="spin2"></span>Checking host allocation</div>'+UI.actions(UI.cancel('Close'))+'</div>',true,'disk-allocation');
  const host=$('#allocationBody');
  try {
    const plan=await allocationPost('review',{node});
    if(sequence!==diskAllocationSequence||$('#allocationBody')!==host||$('#modal')?.classList.contains('hidden'))return;
    if(plan.operation)return diskAllocationWatch(plan.operation.id);
    diskAllocationPlan=plan;
    host.innerHTML=UI.lead('Grow the OS or Longhorn filesystem using unallocated space. Existing files stay in place.')+
      UI.fields(UI.field('Area',`<select id="allocationRole" onchange="diskAllocationChanged(true)">${plan.areas.map(a=>`<option value="${esc(a.role)}">${esc(a.name)}</option>`).join('')}</select>`)+
        UI.field('New total size (GiB)','<input id="allocationSize" type="number" step="1" inputmode="numeric" oninput="diskAllocationChanged()">'))+
      '<div id="allocationPreview"></div>'+allocationLimits()+
      UI.actions(UI.cancel('Close')+UI.button('Review growth','diskAllocationReview()',{kind:'pri',id:'allocationReview',attrs:'data-need="admin"'}));
    diskAllocationChanged(true);window.applyRole?.();
  } catch(e) {if(sequence===diskAllocationSequence&&$('#allocationBody')===host)host.innerHTML=UI.callout('warn','Allocation unavailable',esc(e.message))+allocationLimits()+UI.actions(UI.cancel('Close'));}
};
window.diskAllocationChanged=(reset=false)=>{
  const plan=diskAllocationPlan,role=$('#allocationRole')?.value,field=$('#allocationSize');
  if(!plan||!field)return;
  const area=plan.areas.find(a=>a.role===role);if(!area)return;
  const min=Math.floor(area.size_bytes/1073741824)+1,max=Math.floor((area.size_bytes+plan.free_bytes)/1073741824);
  field.min=min;field.max=max;
  if(reset)field.value=Math.min(max,min+15);
  const amount=Number(field.value),target=Math.ceil(amount*1073741824/plan.extent_bytes)*plan.extent_bytes;
  const valid=Number.isInteger(amount)&&amount>=min&&target<=area.size_bytes+plan.free_bytes;
  $('#allocationReview').disabled=!valid||diskAllocationBusy;
  $('#allocationPreview').innerHTML=allocationDiagram(plan,role,valid?target:area.size_bytes)+
    UI.facts([['Filesystem',esc(area.filesystem+' at '+area.mountpoint)],['Used',esc(allocationGiB(area.used_bytes))],['Filesystem reserve',esc(allocationGiB(area.reserved_bytes))],['Downtime','None expected for online growth']])+
    (!valid?UI.callout('warn','Growth unavailable',max<min?'There is no whole GiB of unallocated space available. Existing allocations are retained.':`Choose a total size from ${min} to ${max} GiB.`):'');
};
window.diskAllocationReview=async()=>{
  if(diskAllocationBusy||$('#allocationReview')?.disabled||!diskAllocationPlan)return;
  diskAllocationBusy=true;$('#allocationReview').disabled=true;const sequence=diskAllocationSequence;
  try {
    const plan=await allocationPost('review',{node:diskAllocationPlan.node,role:$('#allocationRole').value,size_gib:Number($('#allocationSize').value)});
    if(sequence!==diskAllocationSequence||!$('#allocationBody')||$('#modal')?.classList.contains('hidden'))return;
    if(plan.operation)return diskAllocationWatch(plan.operation.id);
    allocationConfirm(plan,false);
  } catch(e){toast(e.message,'bad');}finally{diskAllocationBusy=false;if($('#allocationReview'))diskAllocationChanged();}
};
function allocationConfirm(plan,recovery) {
  diskAllocationPlan=plan;const area=recovery?plan.area:plan.areas.find(a=>a.role===plan.role);
  modal(recovery?'Finish filesystem growth':'Review storage growth',`<div id="allocationConfirm">`+
    UI.lead(recovery?'The logical volume has grown. Finish expanding its filesystem without allocating more space.':'Expand this logical volume and its mounted filesystem. Other allocations stay the same.')+
    (recovery?UI.facts([['Logical volume',esc(allocationGiB(area.size_bytes))],['Filesystem now',esc(allocationGiB(area.fs_bytes))],['Filesystem after',esc(allocationGiB(plan.target_bytes))]]):allocationDiagram(plan,plan.role,plan.target_bytes))+
    UI.facts([['Host',esc(plan.node)],['Area',esc(area.name)],['Mount folder',`<span class="mono">${esc(area.mountpoint)}</span>`],['Downtime','None expected']])+
    UI.more('Device identity',UI.facts([['Logical volume',`<span class="allocation-identity mono">${esc(area.stable_device)}</span>`],['Filesystem UUID',`<span class="allocation-identity mono">${esc(area.fs_uuid)}</span>`]]))+
    UI.ack('allocationAck',recovery?'Finish growing this filesystem':'Grow this area to the reviewed size',{onchange:"$('#allocationApply').disabled=!this.checked"})+
    UI.actions(UI.cancel('Close')+UI.button(recovery?'Finish growth':'Grow filesystem',`diskAllocationApply(${recovery})`,{kind:'pri',id:'allocationApply',disabled:true,attrs:'data-need="admin"'}))+'</div>',true,'disk-allocation');
  window.applyRole?.();
}
window.diskAllocationApply=async recovery=>{
  if(diskAllocationBusy||!diskAllocationPlan||!$('#allocationAck')?.checked)return;
  const plan=diskAllocationPlan,sequence=diskAllocationSequence;diskAllocationPlan=null;diskAllocationBusy=true;$('#allocationApply').disabled=true;
  const keys=recovery?['operation_id','request_id','action','capacity_token']:['node','role','size_gib','request_id','capacity_token'];
  const body=Object.fromEntries(keys.map(key=>[key,plan[key]]));body.confirm_capacity=true;
  try {
    const item=await allocationPost(recovery?'finish':'start',body);window.noteOperation?.(item);
    if(sequence===diskAllocationSequence&&$('#allocationConfirm')&&!$('#modal')?.classList.contains('hidden'))await diskAllocationWatch(item.id);
  }catch(e){toast(e.message+' Reopen allocation to check the saved job.','bad');}finally{diskAllocationBusy=false;}
};
window.diskAllocationWatch=async id=>{
  allocationReset();const sequence=diskAllocationSequence;
  modal('Storage allocation','<div id="allocationStatus"><div class="empty"><span class="spin2"></span>Reading saved job</div>'+UI.actions(UI.cancel('Close'))+'</div>',true,'disk-allocation');
  const host=$('#allocationStatus');
  async function poll() {
    if(sequence!==diskAllocationSequence||$('#allocationStatus')!==host||$('#modal')?.classList.contains('hidden'))return;
    try {
      const item=await api('/api/disks/allocation/status?id='+encodeURIComponent(id));
      if(sequence!==diskAllocationSequence||$('#allocationStatus')!==host||$('#modal')?.classList.contains('hidden'))return;
      const done=item.phase==='complete',failed=item.status==='failed',settled=done||item.phase==='cancelled';
      host.innerHTML=UI.lead(esc(item.message))+UI.facts([['Host',esc(item.node)],['Mount folder',esc(item.mountpoint)],['Approved total',esc(allocationGiB(item.target_bytes))],['State',UI.chip(done?'Complete':settled?'No growth applied':failed?'Needs inspection':'Growing',done?'ok':failed&&!settled?'warn':'')]])+
        (!settled&&!failed?UI.progress(item.progress,{label:'Storage growth'}):'')+
        (failed&&!settled?UI.callout('warn','Check before changing allocation','The logical volume may have grown before its filesystem. Checking again reads the saved job and the current sizes, and does not repeat the command that grows it.'):'')+
        UI.actions(UI.cancel('Close')+(failed&&!settled?UI.button('Recheck outcome',`diskAllocationRecheck(${jsArg(id)})`,{kind:'pri',attrs:'data-need="admin"'}):settled?UI.button('View allocation',`diskAllocationOpen(${jsArg(item.node)})`,{kind:'pri'}):''),UI.button('Job log',`diskAllocationLog(${jsArg(id)})`));
      window.noteOperation?.(item);window.applyRole?.();if(['succeeded','failed','cancelled'].includes(item.status))return;
    }catch(e){if($('#allocationStatus')===host)host.innerHTML=UI.callout('warn','Status unavailable',esc(e.message)+' Completion is not assumed.')+UI.actions(UI.cancel('Close'));}
    if(sequence===diskAllocationSequence&&$('#allocationStatus')===host)diskAllocationTimer=setTimeout(poll,4000);
  }
  await poll();
};
window.diskAllocationRecheck=async id=>{
  if(diskAllocationBusy)return;diskAllocationBusy=true;const sequence=diskAllocationSequence;
  try {
    const plan=await allocationPost('recheck',{id});
    if(sequence!==diskAllocationSequence||!$('#allocationStatus')||$('#modal')?.classList.contains('hidden'))return;
    if(plan.operation){if(plan.detail)toast(plan.detail);return diskAllocationWatch(id);}
    allocationConfirm(plan,true);
  }catch(e){toast(e.message,'bad');}finally{diskAllocationBusy=false;}
};
window.diskAllocationLog=id=>{allocationReset();pushModal(()=>diskAllocationWatch(id));jobLog(id);};
