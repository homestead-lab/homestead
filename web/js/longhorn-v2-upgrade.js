/* Longhorn owns the rolling upgrade. This view reviews eligibility and
   observes its node records; pausing lets the current node finish. */
let LH_V2_UPGRADE_TIMER = null, LH_V2_REVIEWING = false, LH_V2_STARTING = false;
// The status request being waited for, or null once the settings review has taken over the dialog: its late reply is dropped and the poll stops, but the view stays.
let LH_V2_OPEN = null;
const LH_V2_STAGES = {
  pending:'Waiting', 'in-progress':'Upgrading', 'relocating-engines':'Moving engines temporarily',
  'waiting-for-source-im':'Updating instance manager', 'restoring-engines':'Returning engines',
  'waiting-for-healthy-volumes':'Waiting for healthy volumes', completed:'Complete', failed:'Failed'
};

/* The upgrade dialog (#909), on the shared review flow, which keeps the sequence: a late report never replaces a
   newer dialog. Its "review" is the view being shown: the Longhorn report, the target and the method chosen. With no
   target it is only the status of an upgrade under way, which is polled. Start is not one-shot: a refused start
   leaves the view to try again, so it keeps its own busy flag. Settings have their own review below. */
const LH_V2_UPGRADE = UI.reviewFlow({
  loading: input => {
    const { target } = input;
    LH_V2_OPEN = input;
    clearTimeout(LH_V2_UPGRADE_TIMER);
    LH_V2_SETTINGS.back();
    modal(target ? `Upgrade Longhorn to ${target}` : 'Longhorn V2 live upgrade', '<div id="lhV2UpgradeBody">Checking volumes and hosts…</div>', true);
  },
  live: input => LH_V2_OPEN === input && !!$('#lhV2UpgradeBody'),
  preview: async input => {
    const { target } = input;
    const report = await api('/api/longhorn/v2/upgrade' + (target ? `?to=${encodeURIComponent(target)}` : ''));
    return {target, report, mode:report.live_ready ? 'live' : 'offline', input};
  },
  render: view => {
    lhV2UpgradePaint();
    if (!view.target) lhV2UpgradePoll(view);
  },
  failed: e => { $('#lhV2UpgradeBody').innerHTML = UI.callout('bad','Could not check the upgrade',esc(e.message)); },
});
// A send under way, or a settings review being read, holds the others back.
const lhV2Busy = () => LH_V2_STARTING || LH_V2_SETTINGS.busy || LH_V2_REVIEWING;
window.lhV2Upgrade = (target = '') => LH_V2_UPGRADE.open({ target });

function lhV2UpgradePoll(view) {
  LH_V2_UPGRADE_TIMER = setTimeout(async () => {
    if (LH_V2_UPGRADE.review !== view || LH_V2_OPEN !== view.input || !$('#lhV2UpgradeBody')) return;
    try {
      const report = await api('/api/longhorn/v2/upgrade');
      if (LH_V2_UPGRADE.review !== view || LH_V2_OPEN !== view.input || !$('#lhV2UpgradeBody')) return;
      view.report = report;
      lhV2UpgradePaint();
    } catch (e) {
      if ($('#lhV2UpgradeError')) $('#lhV2UpgradeError').textContent = `Status could not be refreshed: ${e.message}`;
    }
    if (LH_V2_UPGRADE.review === view && LH_V2_OPEN === view.input && $('#lhV2UpgradeBody')) lhV2UpgradePoll(view);
  }, 5000);
}

function lhV2UpgradePaint() {
  const view = LH_V2_UPGRADE.review, host = $('#lhV2UpgradeBody');
  if (!view || !host) return;
  const r = view.report, u = r.upgrade, admin = can('admin');
  const rows = (u.nodes || []).map(n => [esc(n.node), UI.chip(LH_V2_STAGES[n.stage] || n.stage, n.state === 'completed' ? 'ok' : n.state === 'failed' ? 'bad' : '')
    + (n.error ? `<div class="small">${esc(n.error)}</div>` : '') + (n.retries ? `<div class="dim small">${n.retries} retries</div>` : '')]);
  const blockers = r[`${view.mode}_blockers`] || [];
  const supported = /^v?1\.(1[3-9]|[2-9]\d)\./.test(r.installed) && !r.harvester;
  host.innerHTML = UI.lead(view.target
    ? 'Choose how to upgrade V2 storage. Homestead checks the cluster again when you start.'
    : 'Longhorn upgrades V2 storage one host at a time. Volumes stay attached while engines move temporarily.')
    + UI.facts([['Installed',esc(r.installed)], ...(view.target ? [['Target',esc(r.target)]] : []), ['V2 volumes',r.v2_volumes],
      ...(!view.target ? [['Live upgrades',supported ? UI.chip(u.enabled ? 'Enabled' : 'Paused',u.enabled ? 'ok' : '') : 'Requires Longhorn 1.13+']] : []),
      ...(u.current_node ? [['Current host',esc(u.current_node)]] : [])])
    + (view.target ? UI.fields(UI.field('Upgrade method', `<select id="lhV2UpgradeMode" onchange="lhV2UpgradeMode(this.value)"><option value="live" ${view.mode === 'live' ? 'selected' : ''}>Live · volumes stay attached</option><option value="offline" ${view.mode === 'offline' ? 'selected' : ''}>Offline · detach V2 volumes</option></select>`))
      + UI.checklist(blockers.length ? blockers.map(title => ({state:'bad',title})) : [{state:'ok',title: view.mode === 'live' ? 'Hosts and V2 replicas meet the live upgrade requirements.' : 'V2 volumes are detached and their replicas are stopped.'}])
      + (view.mode === 'live' ? UI.ack('lhV2UpgradeBackup','I have backed up the V2 volumes before this upgrade.') : '')
      + lhV2UpgradeCheck(view.target)
      + UI.actions([UI.cancel(),UI.button(`Upgrade to ${r.target}`,'lhV2UpgradeStart()', {kind:'pri',disabled:!!blockers.length})].join(''))
      : (rows.length ? UI.section('Host progress',UI.table(['Host','Stage'],rows)) : UI.lead('No V2 host upgrade is recorded.')))
    + (!view.target && supported && admin ? UI.actions([
      UI.button(u.enabled ? 'Pause before next host' : 'Enable / resume live upgrades',`lhV2UpgradeSettings(${!u.enabled})`),
      UI.button('Change timeout',`lhV2UpgradeSettings(${u.enabled},true)`)
    ].join('')) : '')
    + UI.more('Requirements and recovery', UI.lead('Live upgrade requires Longhorn 1.12.2 or later as the source, Kubernetes 1.34+, and healthy RW replicas on at least two eligible hosts. Single-host, ublk and sharded configurations use offline upgrade.')
      + UI.lead('Back up your V2 volumes and leave disk space for replica rebuilding. Avoid volume expansion and live migration until all host upgrades finish and volumes are healthy.')
      + UI.lead(`The host timeout is ${u.timeout} minutes. It does not apply while waiting for healthy volumes. Pausing allows the current host to finish; it does not roll back the manager or storage version.`)
      + UI.lead('Longhorn retries failed hosts up to five times. Resolve the reported error first. Pause/resume does not reset retries; resetting the controller remains a manual recovery action in Longhorn.')
      + `<a href="https://longhorn.io/docs/1.13.0/deploy/upgrade/v2-instance-upgrade/" target="_blank" rel="noopener noreferrer">Longhorn upgrade documentation</a>`)
    + '<div id="lhV2UpgradeError" class="small" role="status"></div>';
  if (window.applyRole) applyRole();
}
// What Homestead's table knows about this target (views-cluster.js,
// homestead_upgrade_check), from the Platform versions report the dialog was opened from.
function lhV2UpgradeCheck(target) {
  // STATE is core.js's const: a global name, not a property of window.
  const parts = typeof STATE === 'undefined' ? [] : STATE.data?.components?.components || [];
  const row = parts.find(c => c.id === 'longhorn' && c.next === target);
  return row?.check && typeof componentCheckSection === 'function' ? componentCheckSection(row.check) : '';
}
window.lhV2UpgradeMode = mode => {
  if (!LH_V2_UPGRADE.review || !['live','offline'].includes(mode)) return;
  LH_V2_UPGRADE.review.mode = mode;
  lhV2UpgradePaint();
};
window.lhV2UpgradeStart = async () => {
  const view = LH_V2_UPGRADE.review;
  if (!view?.target || lhV2Busy() || view.report[`${view.mode}_blockers`]?.length) return;
  if (view.mode === 'live' && !$('#lhV2UpgradeBackup')?.checked) return toast('Confirm that the V2 volumes are backed up first.','bad');
  const ack = $('#cmp_check_ack');
  if (ack && !ack.checked) return toast('Tick that you have read what this upgrade breaks first','bad');
  LH_V2_STARTING = true;
  try {
    const result = await api('/api/cluster/components/upgrade',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({component:'longhorn',to:view.target,v2_mode:view.mode,confirm_backup:view.mode === 'live',...(ack ? {accept_check:true} : {})})});
    if (LH_V2_UPGRADE.review === view && $('#lhV2UpgradeBody')) closeModal();
    toast(result.detail,'ok');
    if (window.noteOperation) noteOperation(result.operation);
    clusterComponentsPaint();
  } catch (e) { toast(e.message,'bad'); }
  finally { LH_V2_STARTING = false; }
};

window.lhV2UpgradeSettings = (enabled, timeoutOnly = false) => {
  const view = LH_V2_UPGRADE.review;
  if (!view || lhV2Busy()) return;
  clearTimeout(LH_V2_UPGRADE_TIMER);
  childModal(timeoutOnly ? 'V2 host upgrade timeout' : enabled ? 'Enable V2 live upgrades' : 'Pause V2 live upgrades',
    UI.lead(enabled ? 'Longhorn can upgrade V2 instance managers automatically after future manager upgrades. Check volume backups and replica health before each upgrade.' : 'The current host upgrade can finish. Longhorn will wait before upgrading another host.')
    + UI.fields(UI.field('Host timeout (minutes)',`<input id="lhV2UpgradeTimeout" type="number" min="1" max="1440" value="${view.report.upgrade.timeout}">`,{help:'Changes also affect the current upgrade. Waiting for healthy volumes has no timeout.'}))
    + UI.actions([UI.button('Back','lhV2Upgrade()', { attrs: 'data-dialog-dismiss="true"' }),UI.button('Review settings',`lhV2UpgradeSettingsReview(${enabled})`,{kind:'pri'})].join('')));
};
/* The settings review: pause, resume or the timeout. Read, then ticked, then applied once. */
const LH_V2_SETTINGS = UI.reviewFlow({
  live: () => !!$('#lhV2UpgradeTimeout'),
  preview: async cfg => {
    const review = await api('/api/longhorn/v2/upgrade/review',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(cfg)});
    return {...cfg,capacity_token:review.capacity_token};
  },
  render: (review, { enabled, timeout }) => {
    childModal('Apply V2 upgrade settings',UI.facts([['Live upgrades',enabled ? 'Enabled' : 'Paused'],['Host timeout',`${timeout} minutes`]])
      + UI.ack('lhV2SettingsAck',enabled ? 'Turn on automatic V2 upgrades; backups are checked' : 'Pause them; the host being upgraded still finishes')
      + UI.actions([UI.button('Back','lhV2Upgrade()', { attrs: 'data-dialog-dismiss="true"' }),UI.button('Apply settings','lhV2UpgradeSettingsApply()',{kind:'pri'})].join('')));
  },
  failed: e => toast(e.message,'bad'),
  ack: 'lhV2SettingsAck',
  apply: async review => {
    const cfg = {...review,confirm_capacity:true};
    try {
      const result = await api('/api/longhorn/v2/upgrade/settings',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(cfg)});
      toast(result.detail,'ok');
      if ($('#lhV2SettingsAck')) lhV2Upgrade();
    } catch (e) { toast(e.message,'bad'); }
  },
});
window.lhV2UpgradeSettingsReview = async enabled => {
  if (lhV2Busy()) return;
  const timeout = Number($('#lhV2UpgradeTimeout')?.value);
  // Reading the settings review stops the status poll and drops a status reply still on its way; the view itself stays.
  LH_V2_OPEN = null;
  LH_V2_REVIEWING = true;
  try { await LH_V2_SETTINGS.open({enabled,timeout}); }
  finally { LH_V2_REVIEWING = false; }
};
window.lhV2UpgradeSettingsApply = async () => {
  if (!LH_V2_SETTINGS.review || !$('#lhV2SettingsAck')?.checked || lhV2Busy()) return;
  await LH_V2_SETTINGS.apply();
};
