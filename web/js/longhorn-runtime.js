/* Runtime stays at its fixed host path; the original store is retained. */
let runtimeSequence=0, runtimeTimer=null, runtimeApproval=null, runtimeBusy=false;
const runtimePost=(path,body)=>api('/api/disks/runtime/'+path,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
function runtimeReset(){clearTimeout(runtimeTimer);runtimeApproval=null;++runtimeSequence;}
function runtimeScope(plan,retire=false){
  return UI.facts([
    ['Host',esc(plan.node)],['Selected logical volume',`<span class="mono">${esc(plan.device)}</span>`],
    ['LV size',esc(diskV2GiB(plan.size_bytes))],['Runtime files',esc(diskV2GiB(plan.runtime_bytes))]
  ])+diskV2Region(plan,{label:retire?'Retire files':'Move runtime',summary:retire?'Remove only the archived runtime files. The LV and its filesystem stay.':'Copy runtime to the OS filesystem. The LV and its files stay.'})+
  UI.more('Locations and what stays',UI.facts([
    ['Runtime destination on OS',`<span class="disk-v2-identity mono">${esc(plan.destination)}</span>`],
    ['Original LV retained at',`<span class="disk-v2-identity mono">${esc(plan.retained)}</span>`]
  ])+UI.table([{label:'Area'},{label:'Result'}],[
    ['OS and other logical volumes','Keep all existing files and boundaries'],
    ['/var/lib/longhorn','Same path; runtime uses the OS filesystem'],
    ['Original Longhorn LV',retire?'Remove only verified, archived runtime files; keep filesystem and disk configuration':'Keep all files; mount read-only after moving runtime'],
    ['Archive on OS filesystem','Keep a separate, verified copy'],
    ['V2 preparation','Separate review and erase approval after retirement']
  ])+UI.facts([['Stable device',`<span class="disk-v2-identity mono">${esc(plan.stable_device)}</span>`]]));
}
window.runtimeMoveOpen=async(node,disk)=>{
  runtimeReset();const sequence=runtimeSequence;
  modal('Move Longhorn runtime','<div id="runtimeReview"><div class="empty"><span class="spin2"></span>Checking runtime and storage</div>'+UI.actions(UI.cancel('Close'))+'</div>',true,'disk-v2');
  const host=$('#runtimeReview');
  try{
    const plan=await runtimePost('review',{node,disk});
    if(sequence!==runtimeSequence||$('#runtimeReview')!==host||$('#modal')?.classList.contains('hidden'))return;
    if(plan.operation)return runtimeMoveWatch(plan.operation.id);
    runtimeApproval={node,disk,request_id:plan.request_id,capacity_token:plan.capacity_token};
    host.innerHTML=UI.lead('Move Longhorn’s runtime files to the OS filesystem. The logical volume is kept until the copy is verified.')+
      UI.steps([{title:'Move runtime'},{title:'Retire original files'},{title:'Review V2'}],0)+
      UI.callout('warn','Two maintenance restarts','One moves the runtime; after a second review, the next removes the archived originals. A single-host cluster is down for each.')+
      runtimeScope(plan)+`<p class="ui-help">${esc(diskV2GiB(plan.runtime_bytes*2))} for runtime and archive · ${esc(diskV2GiB(plan.available_bytes))} free on OS</p>`+UI.meter({now:plan.runtime_bytes*200/plan.available_bytes,label:'Runtime and archive as a share of OS free space'})+
      UI.more('Safety and recovery','<p class="ui-help">Move or remove every replica and backing image first. The host checks the copy before Kubernetes starts; if unsure, it stops and keeps both. Recovery steps are saved with the receipt. Leave LVM, mounts and Longhorn disks alone until it ends.</p>')+
      UI.ack('runtimeConfirm','Stage the move; I restart the host separately',{onchange:"$('#runtimeStart').disabled=!this.checked"})+
      UI.actions(UI.cancel('Close')+UI.button('Stage runtime move','runtimeMoveStart()',{kind:'pri',id:'runtimeStart',disabled:true,attrs:'data-need="admin"'}));
    window.applyRole?.();
  }catch(e){if($('#runtimeReview')===host)host.innerHTML=UI.callout('warn','Runtime move unavailable',esc(e.message))+UI.actions(UI.cancel('Close'));}
};
window.runtimeMoveStart=async()=>{
  if(runtimeBusy||!runtimeApproval||!$('#runtimeConfirm')?.checked||$('#modal')?.classList.contains('hidden'))return;
  const body=runtimeApproval,sequence=runtimeSequence,host=$('#runtimeReview');runtimeApproval=null;runtimeBusy=true;
  try{const item=await runtimePost('start',{...body,confirm_capacity:true});window.noteOperation?.(item);if(sequence===runtimeSequence&&!$('#modal')?.classList.contains('hidden'))await runtimeMoveWatch(item.id);}
  catch(e){if(sequence===runtimeSequence&&$('#runtimeReview')===host&&!$('#modal')?.classList.contains('hidden'))host.innerHTML=UI.callout('warn','Runtime move not confirmed',esc(e.message))+UI.actions(UI.cancel('Close')+UI.button('Review again',`runtimeMoveOpen(${jsArg(body.node)},${jsArg(body.disk)})`,{kind:'pri'}));}finally{runtimeBusy=false;}
};
function runtimePaint(item){
  const host=$('#runtimeStatus');if(!host)return;
  const moved=item.phase==='relocated',done=item.phase==='retired',reboot=['staged','retire-staged'].includes(item.phase);
  host.innerHTML=UI.lead(esc(item.message))+
    UI.steps([{title:'Move runtime'},{title:'Retire original files'},{title:'Review V2'}],done?2:moved||item.phase==='retire-staged'?1:0)+
    runtimeScope(item,moved||item.phase==='retire-staged'||done)+
    (item.status==='failed'?UI.callout('warn','Maintenance needs inspection','Recheck the saved outcome. If the host cannot start Kubernetes, use its local console and the recovery instructions in '+esc(item.destination.replace(/\/active$/,'/RECOVERY.txt'))+'. Keep the original LV and archive.'):'')+
    UI.actions(UI.cancel('Close')+(done?UI.button('Prepare for V2',`diskV2Open(${jsArg(item.node)},${jsArg(item.disk)})`,{kind:'pri',attrs:'data-need="admin"'}):
      moved?UI.button('Review retirement',`runtimeRetireReview(${jsArg(item.id)})`,{kind:'pri',attrs:'data-need="admin"'}):
      reboot?UI.button('Review host restart',`closeModal();nodeActions(${jsArg(item.node)})`,{kind:'pri',attrs:'data-need="admin"'}):''),
      UI.button('Recheck outcome',`runtimeRecheck(${jsArg(item.id)})`,{attrs:'data-need="admin"'}));
  window.applyRole?.();
}
window.runtimeMoveWatch=async id=>{
  runtimeReset();const sequence=runtimeSequence;
  modal('Longhorn runtime maintenance','<div id="runtimeStatus"><div class="empty"><span class="spin2"></span>Reading saved maintenance</div></div>',true,'disk-v2');
  const host=$('#runtimeStatus');
  async function poll(){
    if(sequence!==runtimeSequence||$('#runtimeStatus')!==host||$('#modal')?.classList.contains('hidden'))return;
    try{const item=await api('/api/disks/runtime/status?id='+encodeURIComponent(id));if(sequence!==runtimeSequence||$('#runtimeStatus')!==host)return;runtimePaint(item);if(item.status==='succeeded'||item.status==='failed')return;}
    catch(e){if($('#runtimeStatus')===host)host.innerHTML=UI.callout('info','Waiting for host maintenance','The original LV remains retained. Reconnect when the host returns; completion is not assumed.')+UI.actions(UI.cancel('Close'));}
    runtimeTimer=setTimeout(poll,5000);
  }await poll();
};
window.runtimeRecheck=async id=>{try{await runtimePost('recheck',{id});await runtimeMoveWatch(id);}catch(e){toast(e.message,'bad');}};
window.runtimeRetireReview=async id=>{
  runtimeReset();const sequence=runtimeSequence;
  modal('Review runtime retirement','<div id="runtimeRetirementReview"><div class="empty"><span class="spin2"></span>Checking the retained source and archive</div></div>',true,'disk-v2');
  const host=$('#runtimeRetirementReview');
  try{
    const plan=await runtimePost('retirement-review',{id});if(sequence!==runtimeSequence||$('#runtimeRetirementReview')!==host||$('#modal')?.classList.contains('hidden'))return;
    runtimeApproval=plan;
    modal('Retire archived runtime files','<div id="runtimeRetirement">'+UI.lead('The moved runtime is in use. The next restart removes the archived originals from the LV.')+
      UI.callout('warn','Only archived runtime files are removed','The filesystem, LV and disk identity stay. A changed or extra file stops it. Erasing for V2 is approved separately.')+
      runtimeScope(plan,true)+UI.typedConfirm({id:'runtimeDevice',expect:plan.device,oninput:'runtimeRetireReady()'})+
      UI.ack('runtimeRetireConfirm','Remove the archived originals at the next restart',{onchange:'runtimeRetireReady()'})+
      UI.actions(UI.cancel('Close')+UI.button('Retire after restart','runtimeRetire()',{kind:'danger',id:'runtimeRetire',disabled:true,attrs:'data-need="admin"'}))+'</div>',true,'disk-v2');
    window.applyRole?.();
  }catch(e){if(sequence===runtimeSequence&&$('#runtimeRetirementReview')===host&&!$('#modal')?.classList.contains('hidden'))host.innerHTML=UI.callout('warn','Retirement review unavailable',esc(e.message))+UI.actions(UI.cancel('Close')+UI.button('Review again',`runtimeRetireReview(${jsArg(id)})`,{kind:'pri'}));}
};
window.runtimeRetireReady=()=>{const b=$('#runtimeRetire');if(b)b.disabled=runtimeBusy||!runtimeApproval||!$('#runtimeRetireConfirm')?.checked||!typedConfirmMatches('runtimeDevice');};
window.runtimeRetire=async()=>{
  if(runtimeBusy||!runtimeApproval||!$('#runtimeRetireConfirm')?.checked||!typedConfirmMatches('runtimeDevice')||$('#modal')?.classList.contains('hidden'))return;
  const plan=runtimeApproval,sequence=runtimeSequence,host=$('#runtimeRetirement');runtimeApproval=null;runtimeBusy=true;
  try{const item=await runtimePost('retire',{id:plan.id,action:plan.action,capacity_token:plan.capacity_token,confirm_capacity:true,confirm_device:plan.device});if(sequence===runtimeSequence&&!$('#modal')?.classList.contains('hidden'))await runtimeMoveWatch(item.id);}
  catch(e){if(sequence===runtimeSequence&&$('#runtimeRetirement')===host&&!$('#modal')?.classList.contains('hidden'))host.innerHTML=UI.callout('warn','Retirement not confirmed',esc(e.message))+UI.actions(UI.cancel('Close')+UI.button('Review again',`runtimeRetireReview(${jsArg(plan.id)})`,{kind:'pri'}));}finally{runtimeBusy=false;}
};
