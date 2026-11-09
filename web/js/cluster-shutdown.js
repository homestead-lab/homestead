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
    modal('Shut down cluster', UI.lead(`Stops the apps and VMs on all <b>${esc(plan.nodes.length)}</b> hosts, then shuts down the hosts. Homestead is the last app to stop.`) +
      UI.callout(plan.ready ? 'warn' : 'bad', plan.ready ? 'The whole cluster goes offline' : 'Resolve these before the shutdown', plan.ready
        ? 'Apps and storage become unavailable. Console or physical access is needed to turn the hosts on again.'
        : `<ul class="ui-list">${(plan.blockers || []).map(p => `<li>${esc(p)}</li>`).join('')}</ul>`) +
      (plan.warnings?.length ? UI.callout('info', 'Temporary helpers', `<ul class="ui-list">${plan.warnings.map(p => `<li>${esc(p)}</li>`).join('')}</ul>`) : '') +
      UI.section('Shutdown order', UI.table([{label:'Stage'}, {label:'What happens'}], [
        ['Prepare', 'Check that a separate power helper runs on every host, and cordon all hosts.'],
        ...(plan.vms?.length ? [['VMs', `${esc(plan.vms.length)} VM${plan.vms.length === 1 ? ' is' : 's are'} shut down from inside first, and started again when the cluster is recovered.`]] : []),
        ...(plan.homestead_copies > 1 ? [['Homestead', `Runs as one copy for the shutdown, and returns to ${esc(plan.homestead_copies)} when the cluster is recovered.`]] : []),
        ['Drain', `${esc(plan.pods)} app pods stop cleanly. Disruption budgets can stop the shutdown.`],
        ['Homestead last', `Homestead stops on ${esc(plan.homestead_node)}. This page disconnects, and the shutdown helper continues.`],
        ['Storage and power', `The helper waits for ${esc(plan.volumes)} Longhorn volumes to be released, then turns off the hosts, with Homestead’s host last.`]
      ])) + UI.more('Hosts and recovery', UI.table([{label:'Host'}, {label:'Takes new apps now'}], plan.nodes.map(n => [esc(n.name), n.cordoned ? 'No (cordoned)' : 'Yes'])) +
        '<p class="ui-help">Kubernetes system components, admission webhooks, Longhorn, DaemonSets and static pods keep running until the hosts shut down. Running VMs are stopped first. Draining deletes temporary (emptyDir) data, and apps and volumes are kept. If a drain fails, the hosts are uncordoned where possible. When the hosts are turned on again and every one is ready, Homestead recovers automatically, and hosts, VMs and apps return to how they were. If a host stays off, uncordon another host from its console so that Homestead can start, then recover here. A lost connection does not confirm that the hosts are off.</p>' +
        (plan.local_storage?.length ? UI.table([{label:'Pod'}, {label:'Local or external storage'}], plan.local_storage.map(v => [esc(v.pod), esc(v.kind)])) : '')) +
      (plan.ready ? UI.typedConfirm({ id: 'shutdownConfirm', expect: plan.confirm, button: 'shutdownStart' }) : '') +
      UI.actions(UI.cancel('Close') + UI.button('Check again', 'clusterShutdown()') + (plan.ready ? UI.button('Shut down cluster', 'clusterShutdownStart()', {kind:'danger', id:'shutdownStart', disabled:true}) : '')), true, 'cluster-shutdown');
  } catch (error) { toast(error.message, 'bad'); }
};
window.clusterShutdownStart = async () => {
  const plan = shutdownReview;
  if (!plan?.ready || !typedConfirmMatches('shutdownConfirm')) return;
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
    UI.more('If the page disconnects', '<p class="ui-help">The shutdown helper continues without Homestead. It waits for the storage to be released before turning off the hosts. A lost connection alone does not show that the hosts are off. To read the shutdown record from a control plane console, run:</p><pre>kubectl -n ' + esc(state.plan.own[0]) + ' get secret homestead-cluster-shutdown -o jsonpath={.data.state} | base64 -d</pre><p class="ui-help">Once every host is turned on and ready, Homestead recovers and starts automatically. If a host stays off, uncordon another host from its console so that Homestead can start. Recovering here uncordons only the hosts this job cordoned, once the time limit for the helper has passed.</p>') +
    UI.actions(UI.cancel('Close') + (canCancel ? UI.button('Cancel shutdown', 'clusterShutdownCancel()', {kind:'danger', id:'shutdownCancel'}) : '') +
      ((expired || state.phase === 'failed') && state.phase !== 'released' ? UI.button('Uncordon hosts', `clusterShutdownRecover(${jsArg(state.run)})`) : '')), true, 'cluster-shutdown');
  shutdownPaint(state);
  shutdownPoll(state);
};
const SHUTDOWN_FINAL = ['stopping-homestead', 'detaching', 'handoff'];
// What a pod still on its host is waiting for, while the hosts drain.
const SHUTDOWN_DRAIN_WORDS = {evicting:'Being evicted', stopping:'Stopping',
  budget:'Its disruption budget · stopped directly within 30 s'};
function shutdownPaint(state, disconnected = false) {
  const box = $('#shutdownLive');
  if (!box) return;
  const drain = ['draining', 'stopping-homestead'].includes(state.phase) && state.drain?.total ? state.drain : null;
  // From here Homestead goes down with the cluster: losing this page is the plan, not a fault.
  const final = SHUTDOWN_FINAL.includes(state.phase);
  const left = name => (drain?.pods || []).filter(p => p.node === name).length;
  // The table says which pods and why; the lead says only what is happening.
  const lead = drain && state.phase === 'draining'
    ? 'Stopping the apps on every host. Homestead stays online until they have stopped. With every host cordoned, a pod whose disruption budget can no longer be met is stopped directly after 30 seconds.'
    : state.message;
  box.innerHTML = UI.lead(esc(lead)) +
    UI.progress(state.progress, {label: drain ? `${drain.done} of ${drain.total} pod${drain.total === 1 ? '' : 's'} stopped` : 'Last verified stage', detail:state.phase}) +
    (final ? UI.callout('info', disconnected ? 'Final stage · disconnected, as expected' : 'Final stage · this page will disconnect',
        "Homestead stops itself so that its storage is released, and the separate shutdown helper finishes without it. It waits for the last volumes to be released, then turns off the hosts, with Homestead's host last. This takes up to 5 minutes from here. Once the hosts are off, this page cannot reach them, so check a host's console or power light to confirm.")
      : disconnected ? UI.callout('warn', 'Connection lost · power state not confirmed', 'The last known state is shown. The shutdown helper may still be working. This page checks again automatically.') : '') +
    (drain && drain.pods.length ? UI.section('Still stopping', UI.table([{label:'Pod'}, {label:'Host'}, {label:'Waiting for'}],
      drain.pods.map(p => [esc(p.pod), esc(p.node), esc(SHUTDOWN_DRAIN_WORDS[p.state] || p.state)]))) : '') +
    UI.table([{label:'Host'}, {label:'Last known state'}], state.plan.nodes.map(n => [esc(n.name), `${esc(state.hosts?.find(h => h.name === n.name)?.state || 'Waiting for the helper')}${left(n.name) ? ` · ${left(n.name)} pod${left(n.name) === 1 ? '' : 's'} still stopping` : ''}<span class="sub">${n.name === state.plan.own_node ? 'Last · Homestead\'s host' : 'Turned off after all apps and volumes stop'}</span>`]));
}
function shutdownPoll(last) {
  if (!$('#shutdownLive')) return;
  shutdownTimer = setTimeout(async () => {
    if (!$('#shutdownLive')) return;
    try {
      const result = await api('/api/cluster/shutdown', {keep:true, timeout:10000});
      if (!$('#shutdownLive')) return;
      if (!result.state || result.state.run !== last.run) throw new Error('The shutdown record changed');
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
    toast('Hosts uncordoned. Check the health of the apps, VMs and storage.', 'ok');
    clusterShutdown();
  } catch (error) { toast(error.message, 'bad'); }
};
