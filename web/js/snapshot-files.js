/* A snapshot browser prepares its own copy; the live claim stays mounted. */
let snapshotFilesReview = null, snapshotFilesSession = null, snapshotFilesTimer, snapshotFilesSequence = 0;
window.snapshotFiles = async (volume, snapshot) => {
  snapshotFilesDismiss();
  const sequence = ++snapshotFilesSequence;
  snapshotFilesReview = null;
  try {
    const plan = await api(`/api/snapshot-files/plan?volume=${encodeURIComponent(volume)}&snapshot=${encodeURIComponent(snapshot)}`);
    if (sequence !== snapshotFilesSequence) return;
    snapshotFilesReview = plan;
    const linked = plan.method === 'linked-clone';
    modal('Browse snapshot', UI.head({ kind: 'Snapshot', name: plan.snapshot, where: plan.claim }) + UI.lead(`Browse and download its files. The live volume stays online.`) +
      UI.facts([['Volume', esc(plan.claim)], ['Volume capacity', fileSize(Number(plan.size))], ['Method', linked ? 'V2 linked clone · no full data copy' : 'Full snapshot copy · one temporary replica'], ['Access', 'Read-only · expires after 30 minutes']]) +
      UI.callout(linked ? 'info' : 'warn', linked ? 'Uses the existing snapshot blocks' : 'A temporary copy is required', linked ? 'Keep the snapshot until you close the browser. Starting it can still take a while.' : 'It takes time, disk space and I/O.' + (plan.engine === 'v2' ? ' Homestead could not confirm linked clones here.' : '')) +
      UI.more('How this works', '<p class="ui-help">A temporary copy of the snapshot, removed when you close it or after 30 minutes unused. Symbolic links and special files are left out.</p>') +
      UI.actions(UI.cancel() + UI.button('Prepare browser', 'snapshotFilesStart()', {kind:'pri', id:'snapshotFilesStart'})), true, 'snapshot-files');
  } catch (error) { toast(error.message, 'bad'); }
};
window.snapshotFilesStart = async () => {
  const plan = snapshotFilesReview;
  if (!plan) return;
  snapshotFilesReview = null;
  const sequence = ++snapshotFilesSequence;
  const id = Array.from(crypto.getRandomValues(new Uint8Array(12)), n => n.toString(16).padStart(2, '0')).join('');
  snapshotFilesSession = {namespace:plan.namespace, session:'homestead-snapshot-files-' + id, source:plan};
  const session = snapshotFilesSession;
  modal('Preparing snapshot browser', '<div id="snapshotFilesProgress"></div>' + UI.actions(UI.button('Close and clean up', 'snapshotFilesClose()')), true, 'snapshot-files');
  snapshotFilesPaint({state:'preparing', stage:'import', source:plan, message:'Requesting a temporary snapshot browser; the live volume stays online'});
  try {
    const state = await api('/api/snapshot-files/start', {method:'POST', headers:{'Content-Type':'application/json'},
      body:JSON.stringify({volume:plan.volume, snapshot:plan.snapshot, review_token:plan.review_token, request_id:id})});
    if (sequence !== snapshotFilesSequence) { snapshotFilesRelease(session); return; }
    snapshotFilesPaint(state);
  } catch (error) {
    if (sequence !== snapshotFilesSequence) { snapshotFilesRelease(session); return; }
    if ($('#snapshotFilesProgress')) $('#snapshotFilesProgress').innerHTML = UI.callout('warn', 'Check preparation progress', esc(error.message) + ' The request may have created a copy; its saved session will be checked.');
  }
  if ($('#snapshotFilesProgress')) snapshotFilesTimer = setTimeout(snapshotFilesPoll, 2000);
};
function snapshotFilesPaint(state) {
  if (state.state === 'ready') {
    clearTimeout(snapshotFilesTimer);
    // The file manager owns the session from here: closing it ends the session.
    snapshotFilesSession = null;
    window.fileManagerSnapshot(state);
    return;
  }
  const host = $('#snapshotFilesProgress');
  if (!host) return;
  if (['failed', 'closed', 'closing'].includes(state.state)) clearTimeout(snapshotFilesTimer);
  if (snapshotFilesSession) snapshotFilesSession.state = state.state;
  const linked = state.source?.method === 'linked-clone';
  host.innerHTML = state.state === 'preparing'
    ? UI.lead('The live volume stays online while the snapshot browser prepares.') +
      UI.steps([{title:'Import snapshot'}, {title:linked ? 'Create linked clone' : 'Copy snapshot'}, {title:'Mount read-only'}], Math.max(0, ['import','clone','mount'].indexOf(state.stage))) +
      UI.progress(state.percent ?? null, {label:state.stage === 'clone' ? (linked ? 'Linked clone' : 'Snapshot copy') : 'Preparation', detail:state.message})
    : UI.callout(state.state === 'failed' ? 'bad' : 'warn', state.state === 'closed' ? 'Browser closed' : 'Browser needs attention', esc(state.message || 'The temporary session no longer exists.'));
}
async function snapshotFilesPoll() {
  if (!$('#snapshotFilesProgress') || !snapshotFilesSession) return;
  const sequence = snapshotFilesSequence, s = snapshotFilesSession;
  try {
    const state = await api(`/api/snapshot-files/status?namespace=${encodeURIComponent(s.namespace)}&session=${encodeURIComponent(s.session)}`);
    if (sequence !== snapshotFilesSequence) return;
    snapshotFilesPaint(state);
  } catch (error) {
    if (sequence !== snapshotFilesSequence) return;
    if ($('#snapshotFilesProgress')) $('#snapshotFilesProgress').innerHTML = UI.callout('warn', 'Progress unavailable', esc(error.message) + ' Retrying; the temporary session expires automatically.');
  }
  if ($('#snapshotFilesProgress') && !['failed','closed','closing'].includes(snapshotFilesSession?.state)) snapshotFilesTimer = setTimeout(snapshotFilesPoll, 3000);
}
async function snapshotFilesRelease(session) {
  if (!session) return;
  try {
    await api('/api/snapshot-files/close', {method:'POST', headers:{'Content-Type':'application/json'},
      body:JSON.stringify({namespace:session.namespace, session:session.session})});
  } catch (error) { toast('Cleanup deferred; the temporary volume expires automatically. ' + error.message, 'warn'); }
}
window.snapshotFilesDismiss = () => {
  ++snapshotFilesSequence;
  clearTimeout(snapshotFilesTimer);
  const session = snapshotFilesSession;
  snapshotFilesSession = null;
  snapshotFilesReview = null;
  return snapshotFilesRelease(session);
};
window.snapshotFilesClose = () => {
  snapshotFilesDismiss();
  closeModal();
};
