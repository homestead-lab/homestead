/* Longhorn owns the rolling upgrade. This view reviews eligibility and
   observes its node records; pausing lets the current node finish. */
let LH_V2_UPGRADE = null, LH_V2_UPGRADE_SEQ = 0, LH_V2_UPGRADE_TIMER = null, LH_V2_UPGRADE_BUSY = false;
const LH_V2_STAGES = {
  pending:'Waiting', 'in-progress':'Upgrading', 'relocating-engines':'Moving engines temporarily',
  'waiting-for-source-im':'Updating instance manager', 'restoring-engines':'Returning engines',
  'waiting-for-healthy-volumes':'Waiting for healthy volumes', completed:'Complete', failed:'Failed'
};

window.lhV2Upgrade = async (target = '') => {
  const seq = ++LH_V2_UPGRADE_SEQ;
  clearTimeout(LH_V2_UPGRADE_TIMER);
  LH_V2_UPGRADE = null;
  LH_V2_SETTINGS_REVIEW = null;
  modal(target ? `Upgrade Longhorn to ${target}` : 'Longhorn V2 live upgrade', '<div id="lhV2UpgradeBody">Checking volumes and hosts…</div>', true);
  try {
    const report = await api('/api/longhorn/v2/upgrade' + (target ? `?to=${encodeURIComponent(target)}` : ''));
    if (seq !== LH_V2_UPGRADE_SEQ || !$('#lhV2UpgradeBody')) return;
    LH_V2_UPGRADE = {target, report, mode:report.live_ready ? 'live' : 'offline'};
    lhV2UpgradePaint();
    if (!target) lhV2UpgradePoll(seq);
  } catch (e) {
    if (seq === LH_V2_UPGRADE_SEQ && $('#lhV2UpgradeBody')) $('#lhV2UpgradeBody').innerHTML = UI.callout('bad','Could not check the upgrade',esc(e.message));
  }
};

function lhV2UpgradePoll(seq) {
  LH_V2_UPGRADE_TIMER = setTimeout(async () => {
    if (seq !== LH_V2_UPGRADE_SEQ || !$('#lhV2UpgradeBody') || !LH_V2_UPGRADE) return;
    try {
      const report = await api('/api/longhorn/v2/upgrade');
      if (seq !== LH_V2_UPGRADE_SEQ || !$('#lhV2UpgradeBody')) return;
      LH_V2_UPGRADE.report = report;
      lhV2UpgradePaint();
    } catch (e) {
      if ($('#lhV2UpgradeError')) $('#lhV2UpgradeError').textContent = `Status could not be refreshed: ${e.message}`;
    }
    if (seq === LH_V2_UPGRADE_SEQ && $('#lhV2UpgradeBody')) lhV2UpgradePoll(seq);
  }, 5000);
}

function lhV2UpgradePaint() {
  const view = LH_V2_UPGRADE, host = $('#lhV2UpgradeBody');
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
window.lhV2UpgradeMode = mode => {
  if (!LH_V2_UPGRADE || !['live','offline'].includes(mode)) return;
  LH_V2_UPGRADE.mode = mode;
  lhV2UpgradePaint();
};
window.lhV2UpgradeStart = async () => {
  const view = LH_V2_UPGRADE;
  if (!view?.target || LH_V2_UPGRADE_BUSY || view.report[`${view.mode}_blockers`]?.length) return;
  if (view.mode === 'live' && !$('#lhV2UpgradeBackup')?.checked) return toast('Confirm that the V2 volumes are backed up first.','bad');
  LH_V2_UPGRADE_BUSY = true;
  const seq = LH_V2_UPGRADE_SEQ;
  try {
    const result = await api('/api/cluster/components/upgrade',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({component:'longhorn',to:view.target,v2_mode:view.mode,confirm_backup:view.mode === 'live'})});
    if (seq === LH_V2_UPGRADE_SEQ && $('#lhV2UpgradeBody')) closeModal();
    toast(result.detail,'ok');
    if (window.noteOperation) noteOperation(result.operation);
    clusterComponentsPaint();
  } catch (e) { toast(e.message,'bad'); }
  finally { LH_V2_UPGRADE_BUSY = false; }
};

window.lhV2UpgradeSettings = (enabled, timeoutOnly = false) => {
  const view = LH_V2_UPGRADE;
  if (!view || LH_V2_UPGRADE_BUSY) return;
  clearTimeout(LH_V2_UPGRADE_TIMER);
  childModal(timeoutOnly ? 'V2 host upgrade timeout' : enabled ? 'Enable V2 live upgrades' : 'Pause V2 live upgrades',
    UI.lead(enabled ? 'Longhorn can upgrade V2 instance managers automatically after future manager upgrades. Check volume backups and replica health before each upgrade.' : 'The current host upgrade can finish. Longhorn will wait before upgrading another host.')
    + UI.fields(UI.field('Host timeout (minutes)',`<input id="lhV2UpgradeTimeout" type="number" min="1" max="1440" value="${view.report.upgrade.timeout}">`,{help:'Changes also affect the current upgrade. Waiting for healthy volumes has no timeout.'}))
    + UI.actions([UI.button('Back','lhV2Upgrade()'),UI.button('Review settings',`lhV2UpgradeSettingsReview(${enabled})`,{kind:'pri'})].join('')));
};
let LH_V2_SETTINGS_REVIEW = null;
window.lhV2UpgradeSettingsReview = async enabled => {
  if (LH_V2_UPGRADE_BUSY) return;
  const timeout = Number($('#lhV2UpgradeTimeout')?.value);
  LH_V2_SETTINGS_REVIEW = null;
  const seq = ++LH_V2_UPGRADE_SEQ;
  LH_V2_UPGRADE_BUSY = true;
  try {
    const cfg = {enabled,timeout};
    const review = await api('/api/longhorn/v2/upgrade/review',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(cfg)});
    if (seq !== LH_V2_UPGRADE_SEQ || !$('#lhV2UpgradeTimeout')) return;
    LH_V2_SETTINGS_REVIEW = {...cfg,capacity_token:review.capacity_token};
    childModal('Apply V2 upgrade settings',UI.facts([['Live upgrades',enabled ? 'Enabled' : 'Paused'],['Host timeout',`${timeout} minutes`]])
      + UI.ack('lhV2SettingsAck',enabled ? 'I have checked backups and understand this enables automatic V2 upgrades.' : 'I understand the current host upgrade can still finish.')
      + UI.actions([UI.button('Back','lhV2Upgrade()'),UI.button('Apply settings','lhV2UpgradeSettingsApply()',{kind:'pri'})].join('')));
  } catch (e) { toast(e.message,'bad'); }
  finally { LH_V2_UPGRADE_BUSY = false; }
};
window.lhV2UpgradeSettingsApply = async () => {
  if (!LH_V2_SETTINGS_REVIEW || !$('#lhV2SettingsAck')?.checked || LH_V2_UPGRADE_BUSY) return;
  const cfg = {...LH_V2_SETTINGS_REVIEW,confirm_capacity:true};
  LH_V2_SETTINGS_REVIEW = null;
  LH_V2_UPGRADE_BUSY = true;
  try {
    const result = await api('/api/longhorn/v2/upgrade/settings',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(cfg)});
    toast(result.detail,'ok');
    if ($('#lhV2SettingsAck')) lhV2Upgrade();
  } catch (e) { toast(e.message,'bad'); }
  finally { LH_V2_UPGRADE_BUSY = false; }
};
