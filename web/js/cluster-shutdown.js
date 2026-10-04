/* A lost connection during shutdown keeps the last observation, never success. */
let shutdownReview = null;
let shutdownSequence = 0;
let shutdownTimer;
window.clusterShutdown = async () => {
  const sequence = ++shutdownSequence;
  shutdownReview = null;
  clearTimeout(shutdownTimer);
  try {
    const current = await api('/api/cluster/shutdown');
    if (sequence !== shutdownSequence) return;
    if (current.state && current.state.phase !== 'released') return clusterShutdownProgress(current.state);
    const plan = await api('/api/cluster/shutdown/plan');
    if (sequence !== shutdownSequence) return;
    shutdownReview = plan;
    modal('Shut down cluster', UI.lead(`Stop workloads on all <b>${esc(plan.nodes.length)}</b> hosts, then shut down the hosts. Homestead is the last application to stop.`) +
      UI.callout(plan.ready ? 'warn' : 'bad', plan.ready ? 'The entire cluster will be offline' : 'Resolve these before shutdown', plan.ready
        ? 'Applications and storage become unavailable. You will need console or physical access to power hosts on and restore scheduling.'
        : `<ul class="ui-list">${(plan.blockers || []).map(p => `<li>${esc(p)}</li>`).join('')}</ul>`) +
      (plan.warnings?.length ? UI.callout('info', 'Temporary helpers', `<ul class="ui-list">${plan.warnings.map(p => `<li>${esc(p)}</li>`).join('')}</ul>`) : '') +
      UI.section('Shutdown order', UI.table([{label:'Stage'}, {label:'What happens'}], [
        ['Prepare', 'Verify an independent power helper on every host; cordon all hosts.'],
        ...(plan.vms?.length ? [['VMs', `${esc(plan.vms.length)} VM${plan.vms.length === 1 ? ' is' : 's are'} shut down from inside first, and started again when you recover the cluster.`]] : []),
        ...(plan.homestead_copies > 1 ? [['Homestead', `Runs as one copy for the shutdown, and goes back to ${esc(plan.homestead_copies)} when you recover the cluster.`]] : []),
        ['Drain', `${esc(plan.pods)} application pods stop through graceful eviction. Disruption budgets can block shutdown.`],
        ['Homestead last', `Stop Homestead on ${esc(plan.homestead_node)}. This page disconnects; the coordinator continues.`],
        ['Storage and power', `Wait for ${esc(plan.volumes)} Longhorn volumes to detach, then request host power-off. Homestead’s host is scheduled last.`]
      ])) + UI.more('Hosts and recovery', UI.table([{label:'Host'}, {label:'Scheduling now'}], plan.nodes.map(n => [esc(n.name), n.cordoned ? 'Cordoned' : 'Allowed'])) +
        '<p class="ui-help">Kubernetes infrastructure, admission webhooks, Longhorn, DaemonSets and static pods stay until host shutdown. Running VMs must be stopped first. Draining removes emptyDir data; controllers and persistent volumes are retained. If a drain fails, original scheduling is restored where possible. Hosts stay cordoned after a committed shutdown: power them on, then use kubectl uncordon from a control-plane console to bring Homestead back. Open this dialog to recover scheduling on the remaining hosts. A lost connection does not confirm physical power-off.</p>' +
        (plan.local_storage?.length ? UI.table([{label:'Pod'}, {label:'Local or external storage'}], plan.local_storage.map(v => [esc(v.pod), esc(v.kind)])) : '')) +
      (plan.ready ? UI.field(`Type ${plan.confirm} to confirm`, '<input id="shutdownConfirm" autocomplete="off" spellcheck="false">') : '') +
      UI.actions(UI.cancel('Close') + UI.button('Check again', 'clusterShutdown()') + (plan.ready ? UI.button('Shut down cluster', 'clusterShutdownStart()', {kind:'danger', id:'shutdownStart'}) : '')), true, 'cluster-shutdown');
  } catch (error) { toast(error.message, 'bad'); }
};
window.clusterShutdownStart = async () => {
  const plan = shutdownReview;
  if (!plan?.ready || $('#shutdownConfirm')?.value !== plan.confirm) return toast('Type SHUT DOWN CLUSTER exactly to confirm', 'bad');
  shutdownReview = null;
  const button = $('#shutdownStart');
  if (button) button.disabled = true;
  try {
    const result = await api('/api/cluster/shutdown', {method:'POST', headers:{'Content-Type':'application/json'},
      body:JSON.stringify({confirm:plan.confirm, review_token:plan.review_token})});
    if (result.operation) window.noteOperation?.(result.operation);
    clusterShutdownProgress(result.state);
  } catch (error) {
    modal('Check shutdown progress', UI.lead(esc(error.message)) + UI.callout('warn', 'The request may have been accepted', 'Check the saved shutdown before trying again.') +
      UI.actions(UI.cancel('Close') + UI.button('Check saved shutdown', 'clusterShutdown()')), false, 'cluster-shutdown');
  }
};
window.clusterShutdownProgress = state => {
  clearTimeout(shutdownTimer);
  const expired = Date.now() / 1000 > state.deadline + 180;
  const canCancel = !['handoff', 'released', 'failed'].includes(state.phase) && !expired;
  modal('Cluster shutdown progress', '<div id="shutdownLive" aria-live="polite"></div>' +
    UI.more('If the page disconnects', '<p class="ui-help">The coordinator continues without Homestead. It waits for storage detachment before sending power commands. Disconnection alone is not proof that hosts are off. To inspect the journal from a control-plane console:</p><pre>kubectl -n ' + esc(state.plan.own[0]) + ' get secret homestead-cluster-shutdown -o jsonpath={.data.state} | base64 -d</pre><p class="ui-help">After powering hosts on, uncordon a host from its console so Homestead can start. Recovery here restores only scheduling that this job changed, after the helper deadline has passed.</p>') +
    UI.actions(UI.cancel('Close') + (canCancel ? UI.button('Cancel shutdown', 'clusterShutdownCancel()', {kind:'danger', id:'shutdownCancel'}) : '') +
      ((expired || state.phase === 'failed') && state.phase !== 'released' ? UI.button('Recover scheduling', `clusterShutdownRecover(${jsArg(state.run)})`) : '')), true, 'cluster-shutdown');
  shutdownPaint(state);
  shutdownPoll(state);
};
function shutdownPaint(state, disconnected = false) {
  const box = $('#shutdownLive');
  if (!box) return;
  box.innerHTML = UI.lead(esc(state.message)) + UI.progress(state.progress, {label:'Last verified stage', detail:state.phase}) +
    (disconnected ? UI.callout('warn', 'Connection lost · power state unverified', 'Showing the last observation. The independent coordinator may still be working. This page will check again automatically.') : '') +
    UI.table([{label:'Host'}, {label:'Last observation'}], state.plan.nodes.map(n => [esc(n.name), `${esc(state.hosts?.find(h => h.name === n.name)?.state || 'Waiting for helper')}<span class="sub">${n.name === state.plan.own_node ? 'Last · Homestead host' : 'Power after all applications and volumes stop'}</span>`]));
}
function shutdownPoll(last) {
  if (!$('#shutdownLive')) return;
  shutdownTimer = setTimeout(async () => {
    if (!$('#shutdownLive')) return;
    try {
      const result = await api('/api/cluster/shutdown', {keep:true, timeout:10000});
      if (!$('#shutdownLive')) return;
      if (!result.state || result.state.run !== last.run) throw new Error('Shutdown journal changed');
      const next = result.state;
      if (next.phase !== last.phase || (Date.now() / 1000 > next.deadline + 180 && $('#shutdownCancel'))) return clusterShutdownProgress(next);
      shutdownPaint(next);
      shutdownPoll(next);
    } catch (_) { shutdownPaint(last, true); shutdownPoll(last); }
  }, 2000);
}
window.clusterShutdownCancel = async () => {
  try {
    await api('/api/cluster/shutdown/cancel', {method:'POST', headers:{'Content-Type':'application/json'}, body:'{}'});
    const button = $('#shutdownCancel');
    if (button) { button.disabled = true; button.textContent = 'Cancellation requested'; }
  } catch (error) { toast(error.message, 'bad'); }
};
window.clusterShutdownRecover = async run => {
  try {
    await api('/api/cluster/shutdown/recover', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({run})});
    toast('Scheduling restored. Check workload and storage health.', 'ok');
    clusterShutdown();
  } catch (error) { toast(error.message, 'bad'); }
};
