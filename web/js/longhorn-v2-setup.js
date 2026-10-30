/* Explicit host preparation, reviewed maintenance, then engine enablement. */
let lhV2Plan=null, lhV2Approval=null, lhV2Timer=null, lhV2Sequence=0;
const lhV2Footer = main => UI.actions(UI.cancel('Close') + main, UI.button('Check again','lhV2Setup()'), { attrs: 'id="lhV2Foot"' });
window.lhV2Setup = async () => {
  const sequence=++lhV2Sequence;
  if(window.__logTimer) {window.__logTimer.stop?.();window.__logTimer=null;}
  clearTimeout(lhV2Timer); lhV2Approval=null;
  // Setup is a resumable root dialog; discard completed child reviews.
  if(typeof MODAL_STACK!=='undefined') MODAL_STACK.length=0;
  modal('Set up Longhorn V2', '<div id="lhV2Setup"><div class="empty"><span class="spin2"></span>Checking hosts</div></div>' + lhV2Footer(''), true, 'longhorn-v2');
  try {
    const plan=await api('/api/longhorn/v2/plan');
    if(sequence!==lhV2Sequence || !$('#lhV2Setup')) return;
    lhV2Paint(plan);
  } catch(e) { if(sequence===lhV2Sequence && $('#lhV2Setup')) $('#lhV2Setup').innerHTML=UI.callout('bad','Could not verify setup',esc(e.message)); }
  if($('#lhV2Setup')) lhV2Timer=setTimeout(lhV2Poll,4000);
};
function lhV2Paint(plan) {
  lhV2Plan=plan;
  const host=$('#lhV2Setup'); if(!host) return;
  const snapshot=JSON.stringify(plan); if(host.__lhV2Snapshot===snapshot) return; host.__lhV2Snapshot=snapshot;
  const preparing=plan.nodes.some(n=>n.job?.state==='running');
  const stage=plan.engine_ready ? 3 : plan.enabled || plan.harvester_requested || plan.can_enable ? 2 : plan.nodes.some(n=>n.needs_reboot) ? 1 : 0;
  host.innerHTML=UI.lead(plan.engine_ready ? 'V2 instance managers are running. Choose the disks and storage class for new V2 volumes.' : 'Prepare each host before switching on the engine. Existing volumes keep their current data engine.') +
    UI.steps([{title:plan.harvester?'Review Harvester setup':'Prepare hosts'},{title:'Verify hugepages after restart'},{title:'Enable and verify V2'},{title:'Choose disks and a storage class'}],stage) +
    UI.facts([['Hugepages per host',plan.required_mib ? `${esc(plan.required_mib)} MiB plus existing reservations` : 'Disabled in Longhorn · using ordinary RAM'],['Engine',plan.engine_ready?'Ready':plan.enabled?'Enabled · waiting for instance managers':plan.harvester_requested?'Harvester is preparing hosts':'Off']]) +
    (plan.harvester ? UI.callout('info','Harvester manages host setup','The enable review changes Harvester’s setting. Harvester controls host preparation and any required restarts; Homestead follows the observed result.') : '') +
    UI.table([{label:'Host'},{label:'Prerequisites'},{label:'Next action'}],plan.nodes.map(n=>[
      `<b>${esc(n.node)}</b><span class="sub">${esc(n.capacity_mib)} MiB hugepage capacity</span>`,
      (n.job?.state==='running' ? '<span class="spin2"></span> Host preparation running' : n.job?.state==='failed' ? '<span class="warntext">Preparation failed · inspect log</span>' : n.needs_reboot ? 'Configuration saved · Kubernetes needs to rediscover hugepages' : n.problems.length ? n.problems.map(esc).join('<br>') : 'Host prerequisites verified') +
        (plan.enabled ? `<span class="sub">V2 instance manager: ${n.engine_ready?'running':'not ready'}</span>` : ''),
      actionBar([
        n.can_prepare && !n.needs_reboot && (!n.configured || n.problems.length) && {label:n.job?.state==='failed'?'Review retry':n.configured?'Review repair':'Prepare host',need:'admin',run:`lhV2ReviewHost(${jsq(n.node)})`},
        n.needs_reboot && {label:'Review reboot',need:'admin',run:`lhV2Reboot(${jsq(n.node)})`},
        n.job && {label:'Task log',run:`lhV2TaskLog(${jsq(plan.namespace)},${jsq(n.job.name)})`},
        plan.enabled && {label:plan.engine_ready?'Choose disks':'Inspect host',run:`closeModal();nodeDetail(${jsq(n.node)})`}
      ],{shown:1,label:`Actions for ${n.node}`})
    ])) +
    (preparing ? UI.progress(null,{label:'Host preparation',detail:'Tasks continue if you close this dialog. Check again to follow them; logs show the current host step.'}) : '') +
    (plan.blockers.length ? UI.more('What still needs attention',`<ul class="ui-list">${plan.blockers.map(b=>`<li>${esc(b)}</li>`).join('')}</ul>`) : '') +
    UI.more('What changes', '<p class="ui-help">Preparation installs nvme-cli if missing, loads and persists the V2 kernel modules, and reserves the required 2 MiB hugepages without reducing existing reservations. Ubuntu may need the extra modules package for its running kernel. Package repositories must be reachable. A host task does not reboot or change disks. Any reboot uses the normal app, quorum and storage checks, one host at a time. A single-node reboot interrupts Homestead and its apps; reopen this setup when it returns. After reboot, the host stays cordoned until you allow scheduling from its host page.</p>') +
    (!plan.engine_ready && !plan.can_enable ? `<p class="ui-help">${plan.blockers.length ? 'V2 can be enabled once what still needs attention is resolved.' : 'Prepare a host first; V2 can be enabled once one is ready.'}</p>` : '');
  // The main action is the footer's, where it stays in reach (#504).
  const foot = $('#lhV2Foot');
  if (foot) foot.outerHTML = lhV2Footer(plan.engine_ready ? UI.button('Create V2 storage class',"storageClassCreate({engine:'v2'})",{kind:'pri',attrs:'data-need="admin"'})
    : UI.button(plan.harvester?'Review Harvester enablement':'Review enabling V2','lhV2ReviewEnable()',{kind:'pri',disabled:!plan.can_enable,attrs:'data-need="admin"'}));
  window.applyRole?.();
}
async function lhV2Poll() {
  const host=$('#lhV2Setup'),sequence=lhV2Sequence;
  if(!host || $('#modal')?.classList.contains('hidden')) return;
  try {
    const plan=await api('/api/longhorn/v2/plan');
    if(sequence!==lhV2Sequence || $('#lhV2Setup')!==host) return;
    lhV2Paint(plan);
  } catch(e) {
    if($('#lhV2Setup')===host) {host.__lhV2Snapshot=null;host.innerHTML=UI.callout('warn','Setup status unavailable',esc(e.message)+' Retrying; no completion is assumed.');}
  }
  if($('#lhV2Setup')===host) lhV2Timer=setTimeout(lhV2Poll,4000);
}
window.lhV2ReviewHost = node => {
  const row=lhV2Plan?.nodes.find(n=>n.node===node); if(!row?.can_prepare) return;
  clearTimeout(lhV2Timer); ++lhV2Sequence;
  lhV2Approval={action:'prepare',node,review_token:row.review_token};
  childModal(`Prepare V2 · ${node}`, UI.lead(`Configure <b>${esc(node)}</b> for Longhorn V2.`) +
    UI.checklist([{title:'Install nvme-cli if missing',state:'todo'},{title:'Load and persist vfio_pci, uio_pci_generic and nvme_tcp',state:'todo'},
      {title:row.required_mib ? `Reserve at least ${Math.max(row.required_mib,row.target_pages*2)} MiB of hugepages; preserve larger existing pools` : 'Keep Longhorn’s ordinary-memory configuration',state:'todo'}]) +
    UI.callout('warn',row.required_mib?'Host memory will be reserved':'Host packages and modules will change',row.required_mib?'Hugepages reduce the RAM available to ordinary apps and VMs. The task checks memory headroom before reserving hugepages. A separate reviewed reboot may be required before Kubernetes reports the new capacity.':'Longhorn is configured to use ordinary RAM. This task installs and verifies its host tools and kernel modules.') +
    UI.ack('lhV2Confirm','Apply these package, kernel-module and memory changes to this host',{onchange:"$('#lhV2Apply').disabled=!this.checked"}) +
    UI.actions(UI.button('Back to setup','lhV2Setup()', { attrs: 'data-dialog-dismiss="true"' }) + UI.button('Prepare host','lhV2Apply()',{kind:'pri',id:'lhV2Apply',disabled:true})),true,'longhorn-v2');
  lhV2ReturnToSetup();
};
window.lhV2ReviewEnable = () => {
  if(!lhV2Plan?.can_enable) return;
  clearTimeout(lhV2Timer); ++lhV2Sequence;
  lhV2Approval={action:'enable',review_token:lhV2Plan.review_token};
  childModal('Enable Longhorn V2', UI.lead(lhV2Plan.harvester ? 'Harvester will configure its hosts and enable Longhorn V2 through its own setting.' : 'Host prerequisites are verified. Longhorn will start its V2 instance managers.') +
    UI.callout('warn','CPU and memory stay allocated','V2 consumes CPU and memory even before you create a V2 volume.' + (lhV2Plan.harvester?' Harvester may restart hosts to apply its configuration.':'') +
      (lhV2Plan.cpu_mask_fix ? ` V2 will poll with ${lhV2Plan.cpu_mask_fix.cores} CPU core${lhV2Plan.cpu_mask_fix.cores === 1 ? '' : 's'} on each host instead of Longhorn's ${lhV2Plan.cpu_mask_fix.was}: the smallest host has ${lhV2Plan.cpu_mask_fix.host_cpus}, and V2 keeps its cores to itself.` : '')) +
    UI.ack('lhV2Confirm','Enable V2 with these host resource costs' + (lhV2Plan.harvester?' and Harvester-managed host changes and restarts':''),{onchange:"$('#lhV2Apply').disabled=!this.checked"}) +
    UI.actions(UI.button('Back to setup','lhV2Setup()', { attrs: 'data-dialog-dismiss="true"' }) + UI.button('Enable V2','lhV2Apply()',{kind:'pri',id:'lhV2Apply',disabled:true})),true,'longhorn-v2');
  lhV2ReturnToSetup();
};
window.lhV2Apply = async () => {
  if(!lhV2Approval || !$('#lhV2Confirm')?.checked) return;
  const approval=lhV2Approval,sequence=lhV2Sequence,confirmation=$('#lhV2Confirm'); lhV2Approval=null;
  const button=$('#lhV2Apply'); if(button) button.disabled=true;
  try {
    const request_id=Array.from(crypto.getRandomValues(new Uint8Array(12)),n=>n.toString(16).padStart(2,'0')).join('');
    const result=await api(`/api/longhorn/v2/${approval.action}`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({...approval,request_id,confirm:true})});
    if(result.operation) window.noteOperation?.(result.operation);
    if(sequence===lhV2Sequence && $('#lhV2Confirm')===confirmation && !$('#modal')?.classList.contains('hidden')) await lhV2Setup();
  } catch(e) { toast(e.message+' Reopen setup and check saved tasks before trying again.','bad'); }
};
function lhV2ReturnToSetup() {
  if(typeof MODAL_STACK!=='undefined' && MODAL_STACK.length) MODAL_STACK[MODAL_STACK.length-1].onReturn=()=>lhV2Setup();
}
window.lhV2TaskLog = (namespace,name) => {
  clearTimeout(lhV2Timer); ++lhV2Sequence;
  pushModal(()=>lhV2Setup());
  jobLogs(namespace,name);
};
window.lhV2Reboot = async node => {
  clearTimeout(lhV2Timer); ++lhV2Sequence;
  await nodePowerReview(node,'reboot');
  lhV2ReturnToSetup();
};
window.lhV2Disable = async () => {
  if(typeof STATE!=='undefined' && STATE.data.lhcap?.v2?.harvester_setting!=null){
    if(!(await ask('Disable V2 through Harvester? Harvester manages host memory. V2 volumes and block disks must be removed first.',{title:'Disable Harvester V2',ok:'Disable V2',danger:true})))return;
    try{await api('/api/longhorn/settings',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({v2:false})});await lhSettingsPaint();}catch(e){toast(e.message,'bad');}
    return;
  }
  return lhV2CleanupOpen();
};

let v2CleanupSeq=0,v2CleanupTimer=null,v2CleanupApproval=null,v2CleanupBusy=false;
const v2CleanupPost=(path,body)=>api('/api/longhorn/v2/cleanup'+path,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
function v2CleanupHosts(rows){
  return UI.table([{label:'Host'},{label:'RAM owned by V2'},{label:'Result'}],rows.map(row=>[
    esc(row.node),`${esc((row.owned_pages||0)*2)} MiB`,
    row.reason?esc(row.reason):row.phase==='complete'?'Released':row.phase==='restart-required'?
      UI.button('Review restart',`nodePowerReview(${jsArg(row.node)},'reboot')`,{attrs:'data-need="admin"'}):
      `${esc((row.baseline||0)*2)} MiB kept for the original pool`
  ]));
}
window.lhV2CleanupOpen=async()=>{
  const seq=++v2CleanupSeq;clearTimeout(v2CleanupTimer);v2CleanupApproval=null;
  modal('Disable V2 and release resources','<div id="v2Cleanup"><div class="empty"><span class="spin2"></span>Checking V2 and memory ownership</div></div>',true,'longhorn-v2');
  const host=$('#v2Cleanup');
  try{
    const plan=await api('/api/longhorn/v2/cleanup');
    if(seq!==v2CleanupSeq||$('#v2Cleanup')!==host||$('#modal')?.classList.contains('hidden'))return;
    if(plan.operation)return lhV2CleanupWatch(plan.operation.id);
    v2CleanupApproval={request_id:plan.request_id,capacity_token:plan.capacity_token};
    host.innerHTML=UI.lead('Stop V2 to release its CPU and process memory. Release owned hugepage RAM after reviewed host restarts.')+
      v2CleanupHosts(plan.nodes)+UI.more('What cleanup changes','<p class="ui-help">Only the recorded V2 addition is removed from the host’s memory configuration. Earlier or changed reservations stay. Hosts needing a restart are cordoned to prevent new placements while Kubernetes reports the old capacity. Review each restart separately, then allow scheduling when the host is ready. Disks, drivers and other CPU or memory settings stay in place.</p>')+
      UI.ack('v2CleanupAck','Disable V2 and cordon hosts needing RAM cleanup; I will review their restarts separately',{onchange:"$('#v2CleanupStart').disabled=!this.checked"})+
      UI.actions(UI.cancel('Close')+UI.button('Disable and prepare cleanup','lhV2CleanupStart()',{kind:'pri',id:'v2CleanupStart',disabled:true,attrs:'data-need="admin"'}));
    window.applyRole?.();
  }catch(e){if(seq===v2CleanupSeq&&$('#v2Cleanup')===host)host.innerHTML=UI.callout('warn','Cleanup unavailable',esc(e.message))+UI.actions(UI.cancel('Close'));}
};
window.lhV2CleanupStart=async()=>{
  if(v2CleanupBusy||!v2CleanupApproval||!$('#v2CleanupAck')?.checked||$('#modal')?.classList.contains('hidden'))return;
  const plan=v2CleanupApproval,seq=v2CleanupSeq;v2CleanupApproval=null;v2CleanupBusy=true;
  try{const item=await v2CleanupPost('',{...plan,confirm_capacity:true});window.noteOperation?.(item);if(seq===v2CleanupSeq&&!$('#modal')?.classList.contains('hidden'))await lhV2CleanupWatch(item.id);}
  catch(e){if(seq===v2CleanupSeq&&$('#v2Cleanup'))$('#v2Cleanup').innerHTML=UI.callout('warn','Cleanup not confirmed',esc(e.message))+UI.actions(UI.cancel('Close')+UI.button('Review again','lhV2CleanupOpen()'));}
  finally{v2CleanupBusy=false;}
};
window.lhV2CleanupWatch=async id=>{
  const seq=++v2CleanupSeq;clearTimeout(v2CleanupTimer);v2CleanupApproval=null;
  modal('V2 resource cleanup','<div id="v2CleanupStatus"></div>',true,'longhorn-v2');const host=$('#v2CleanupStatus');
  async function poll(){
    if(seq!==v2CleanupSeq||$('#v2CleanupStatus')!==host||$('#modal')?.classList.contains('hidden'))return;
    try{const item=await api('/api/longhorn/v2/cleanup/status?id='+encodeURIComponent(id));
      if(seq!==v2CleanupSeq||$('#v2CleanupStatus')!==host)return;
      host.innerHTML=UI.lead(esc(item.message))+v2CleanupHosts(item.nodes)+
        UI.actions(UI.cancel('Close')+UI.button('Recheck',`lhV2CleanupRecheck(${jsArg(id)})`));
      window.applyRole?.();if(['succeeded','failed'].includes(item.status))return;
    }catch(e){if($('#v2CleanupStatus')===host)host.innerHTML=UI.callout('warn','Status unavailable',esc(e.message))+UI.actions(UI.cancel('Close')+UI.button('Recheck',`lhV2CleanupRecheck(${jsArg(id)})`));return;}
    v2CleanupTimer=setTimeout(poll,4000);
  }await poll();
};
window.lhV2CleanupRecheck=async id=>{try{await v2CleanupPost('/recheck',{id});await lhV2CleanupWatch(id);}catch(e){toast(e.message,'bad');}};
