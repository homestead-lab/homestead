/* Platform health: Harvester/Kubernetes control plane, quorum and onboarding. */

function clusterPill(state, text) {
  const tone = state === "healthy" ? "neutral" : state === "critical" ? "bad" : state === "unknown" ? "neutral" : "warn";
  return UI.chip(text || state || "unknown", tone);
}

function clusterMetric(value, suffix = "%") {
  return value === null || value === undefined ? "—" : `${Number(value).toFixed(1)}${suffix}`;
}

function clusterNodeCard(node) {
  const health = !node.ready ? "critical" : node.pressure?.length ? "attention" : "healthy";
  return `<article class="cluster-node-card">
    <div class="between"><div><b>${esc(node.name)}</b><div class="dim xs">${esc((node.roles || []).join(" · "))}</div></div>
      ${clusterPill(health, node.ready ? node.pressure?.length ? "Pressure" : "Ready" : "Not ready")}</div>
    <div class="cluster-node-metrics">
      <span><small>CPU</small><b>${clusterMetric(node.cpu_pct)}</b></span>
      <span><small>Memory</small><b>${clusterMetric(node.memory_pct)}</b></span>
      <span><small>Disk</small><b>${clusterMetric(node.disk_pct)}</b></span>
      <span><small>Pods</small><b>${node.pods ?? "—"}</b></span>
    </div>
    <div class="cluster-node-foot"><span>${node.schedulable ? "Accepting apps and VMs" : "Cordoned"}</span>
      ${node.pressure?.length ? `<span class="warntext">${esc(node.pressure.join(", "))}</span>` : `<span>${esc(node.version || "version unavailable")}</span>`}</div>
  </article>`;
}

function clusterServiceCard(service) {
  const detail = service.pods ? `${service.ready}/${service.pods} pods ready` : "Not observed";
  return `<article class="cluster-service ${esc(service.state)}">
    <div><b>${esc(service.name)}</b><span>${esc(detail)}</span></div>
    ${clusterPill(service.state, service.state === "attention" ? "Partial" : service.state)}
  </article>`;
}

/* The etcd members, a tiebreaker told apart (homestead_roles): with two there
   is no failure margin, and a small machine can be the third vote. */
function etcdMembersHtml(cp, admin) {
  const members = (cp.members || []).map(m => `${esc(m.name)}${m.role === "tiebreaker" ? ` ${UI.chip("tiebreaker", "neutral", { size: "slim" })} <span class="dim xs">votes only</span>` : ""}${m.ready ? "" : ` ${UI.chip("down", "bad", { size: "slim" })}`}`);
  // The join guide is for k3s and RKE2; Harvester adds hosts its own way.
  const joinable = ["k3s", "rke2"].includes(STATE.platform?.distribution) && !STATE.platform?.harvester;
  const add = cp.etcd_total === 2 && admin && joinable ? ` ${UI.button("Add a tiebreaker", "platformJoinGuide('tiebreaker')")}` : "";
  // On k3s an app host can be the third vote too (#364).
  const promote = cp.etcd_total === 2 && admin && STATE.platform?.distribution === "k3s" && !STATE.platform?.harvester
    ? ' <span class="dim xs">or make an app host a server from its Host actions</span>' : "";
  return members.length ? `<small class="etcd-members">${members.join(" · ")}${add}${promote}</small>` : "";
}
window.etcdMembersHtml = etcdMembersHtml;

async function viewCluster() {
  const report = await api("/api/cluster");
  STATE.data.cluster = report;
  const cp = report.control_plane || {};
  const certs = report.certificates || {};
  const warnings = report.warnings || [];
  const versions = report.versions || {};
  const margin = cp.quorum_margin;
  const marginCopy = margin === null || margin === undefined ? "Unknown" : margin === 0 ? "No failure margin" : `${margin} member${margin === 1 ? "" : "s"}`;
  const serviceRows = (report.services || []).filter(service => service.pods || service.required);
  paint(`${UI.pageHeader(`Cluster`, `Harvester and Kubernetes platform health, separate from application health`, `${moreMenu([{ label: "Shut down cluster…", icon: "power", run: "clusterShutdown()", need: "admin", danger: true }, { label: "Remove a host", icon: "trash", run: "clusterRemovePick()", need: "admin", danger: true }])}
      <button class="btn pri" data-need="admin" onclick="clusterOnboarding()">${icon("plus")}Add a host</button>`)}

  ${summaryLine("cluster", [
      clusterPill(report.state, report.state === "healthy" ? "Platform healthy" : report.state === "critical" ? "Platform action required" : "Review items"),
      `Control plane <b>${cp.ready ?? 0}/${cp.total ?? 0}</b> ready`, `etcd: ${esc(marginCopy)}`,
      versions.kubernetes ? `Kubernetes <b>v${esc(versions.kubernetes)}</b>` : ""],
    `<p class="dim small" style="margin:0 0 10px">${esc(report.summary)}</p>
  <div class="cluster-facts">
    ${STATE.platform && !STATE.platform.harvester
      ? `<section><span>${esc(platformName(STATE.platform))}</span><b>${STATE.platform.version ? `v${esc(STATE.platform.version)}` : "Not reported"}</b><small>Kubernetes distribution</small></section>`
      : `<section><span>Harvester</span><b>${versions.harvester ? `v${esc(versions.harvester)}` : "Not reported"}</b><small>Host operating platform</small></section>`}
    <section><span>Kubernetes</span><b>${versions.kubernetes ? `v${esc(versions.kubernetes)}` : "Not reported"}</b><small>Cluster API version</small></section>
    <section><span>Control plane</span><b>${cp.ready ?? 0}/${cp.total ?? 0} ready</b><small>API and scheduling hosts</small></section>
    <section><span>etcd quorum ${tip("The number of additional ready etcd members that can be lost before the control plane loses quorum.")}</span><b>${esc(marginCopy)}</b><small>${cp.etcd_ready ?? 0}/${cp.etcd_total ?? 0} ready · ${cp.quorum_needed ?? "—"} needed</small>${etcdMembersHtml(cp, can("admin"))}</section>
  </div>`)}

  ${(report.unavailable || []).length ? `<div class="note"><b>Partial platform data.</b> ${esc(report.unavailable.map(row => row.section).join(", "))} could not be read. Other sections remain live.</div>` : ""}



  <section class="card flat" id="clusterHealth">${UI.moduleHeader("Health", "Prioritized suggestions across your cluster", UI.button("Refresh", "HealthInsights.load(true)"))}<div data-insight="full">${HealthInsights.fullBody()}</div></section>
  <div class="cluster-layout">
    <section class="card flat cluster-wide">${UI.moduleHeader(`Hosts and capacity pressure`, `Roles, scheduling state, resource use, and Kubernetes pressure conditions`, `${clusterPill((report.capacity?.unready || []).length ? "critical" : (report.capacity?.pressure || []).length ? "attention" : "healthy",
        `${(report.nodes || []).filter(node => node.ready).length}/${(report.nodes || []).length} ready`)}`)}
      <div class="cluster-node-grid">${(report.nodes || []).map(clusterNodeCard).join("") || '<div class="empty">No hosts reported.</div>'}</div></section>

    <section class="card flat cluster-wide">${UI.moduleHeader(`Critical platform services`, `Observed system pods grouped by the job they perform`, ``)}
      <div class="cluster-service-grid">${serviceRows.map(clusterServiceCard).join("") || '<div class="empty">System service inventory unavailable.</div>'}</div>
      <div class="dim xs cluster-legend">“Not observed” is not treated as failed: some Harvester releases package or label components differently.</div></section>

    <section class="card flat">${UI.moduleHeader(`Certificates and requests`, `Kubernetes certificate-signing requests; secret material is never displayed`, `${clusterPill(certs.state, certs.state)}`)}
      <div class="cluster-mini-stats"><span><b>${certs.pending || 0}</b> pending</span><span><b>${certs.failed || 0}</b> denied / failed</span><span><b>${certs.expiring || 0}</b> nearing requested lifetime</span></div>
      ${(certs.entries || []).length ? `<div class="cluster-csr-list">${certs.entries.slice(0, 5).map(row => `<div><span><b>${esc(row.name)}</b><small>${esc(row.signer || "unknown signer")}</small></span>${clusterPill(row.state === "approved" ? "healthy" : row.state === "pending" ? "attention" : "critical", row.state)}</div>`).join("")}</div>` : '<div class="empty small">No certificate-signing requests are currently retained.</div>'}
      <div class="dim xs">${esc(certs.note || "")}</div></section>

    <section class="card flat">${UI.moduleHeader(`Recent platform warnings`, `Warning events from system namespaces and node objects in the last 24 hours`, `${clusterPill(warnings.length ? "attention" : "healthy", warnings.length ? `${warnings.length} warning${warnings.length === 1 ? "" : "s"}` : "clear")}`)}
      <div class="cluster-warning-list">${warnings.slice(0, 8).map(row => `<div>${UI.chip(row.reason, "warn")}<span><b>${esc(row.object)}</b><small>${esc(row.namespace)} · ${esc(row.message)}</small></span><time>${esc(fmtAgo(row.age_seconds))}</time></div>`).join("") || '<div class="empty small">No recent platform warnings.</div>'}</div>
      ${warnings.length ? '<button class="btn sm" onclick="go(\'events\')">View all events</button>' : ""}</section>

    <section class="card flat cluster-wide cluster-onboard-preview"><div><span class="cluster-kicker">NEXT NODE</span>
      <div class="ctitle">Recommended role: ${esc(report.onboarding?.recommended_role || "Review required")}</div>
      <div class="csub">${esc(report.onboarding?.reason || "Review the current control-plane layout before joining another host.")}</div></div>
      <div class="row"><button class="btn" data-need="admin" onclick="clusterRemovePick()">Remove a host</button>
        <button class="btn" data-need="admin" onclick="clusterOnboarding()">Add a host</button></div></section>
  </div>
  <div id="clusterGitops"></div>
  <div id="clusterComponents">${STATE.data.componentsHtml || ""}</div>
  <div id="clusterUpgrades">${STATE.data.upgradeHtml || ""}</div>
  <div id="clusterCleanup">${STATE.data.cleanupHtml || ""}</div>`);
  // Admin only, and a round trip of its own: the page does not wait for it.
  if (window.can && can("admin")) clusterCleanupPaint();
  // GitHub is asked at most hourly, so this is cheap; still, the page does not wait.
  clusterUpgradesPaint();
  clusterComponentsPaint();
  clusterGitopsPaint();
  HealthInsights.load();
}

/* ---------------- what the platform runs ----------------
   The cluster, Longhorn, KubeVirt and CDI: the version each runs, the newest
   published, and - where Homestead can - an upgrade to the next version,
   one minor at a time, as each project supports. */
async function clusterComponentsPaint(force = false) {
  try {
    const report = await api("/api/cluster/components" + (force ? "?force=1" : ""));
    STATE.data.components = report;
    STATE.data.componentsHtml = componentsCard(report);
    const host = $("#clusterComponents");
    if (host) host.innerHTML = STATE.data.componentsHtml;
    const settingsHost = $("#settingsComponents");
    if (settingsHost) settingsHost.outerHTML = componentsCard(report, true);
    if (window.applyRole) applyRole();
    if (force) toast("Releases checked", "ok");
  } catch (e) {
    const host = $("#settingsComponents");
    if (host && host.querySelector(".spin2")) host.innerHTML = `${UI.moduleHeader(`Platform versions`, ``, `<button class="btn sm" onclick="clusterComponentsPaint(true)">${icon("refresh")}Check now</button>`)}
      <div class="empty small">Could not load platform versions: ${esc(e.message)}</div>`;
    if (force) toast(e.message, "bad");
  }
}
window.clusterComponentsPaint = clusterComponentsPaint;

const COMPONENT_HOW = {
  harvester: "comes with Harvester", helmchart: "installed by Homestead", suc: "system-upgrade-controller",
  manual: "installed outside Homestead",
  manifests: "managed by Homestead",
};

function componentRow(c) {
  const running = (STATE.data.operations || []).find(op => op.kind === "platform-upgrade" && !["succeeded", "failed", "cancelled"].includes(op.status)
    && op.title.startsWith(`Upgrade ${c.name} `));
  const state = c.id === "cluster" && c.how === "harvester" ? ""
    : !c.installed ? UI.chip("not reported", "neutral", { size: "slim" })
    : c.behind ? UI.chip(`${c.newest} is out`, c.next ? "warn" : "neutral", { size: "slim", tip: c.next ? "" : c.note || "" }) : UI.chip("up to date", "ok", { size: "slim" });
  const nodes = c.mixed && c.nodes ? `<div class="dim xs">${Object.entries(c.nodes).map(([n, v]) => `${esc(n)} ${esc(v)}`).join(" · ")}</div>` : "";
  const action = running ? UI.chip("upgrading", "info", { size: "slim" })
    : c.next ? `<button class="btn sm pri" data-need="admin" onclick="componentUpgrade(${jsq(c.id)})">Upgrade to ${esc(c.next)}</button>`
    : c.can_manage ? `<button class="btn sm" data-need="admin" onclick="componentManageOperator(${jsq(c.id)})">Enable upgrades</button>` : "";
  return `<div class="release-row component-row"><div>
      <span class="cluster-kicker">${esc(c.name.toUpperCase())}</span>
      <div><b class="mono">${esc(c.installed || "—")}</b> ${state}
        ${c.phase && c.phase !== "Deployed" ? UI.chip(c.phase, "warn", { size: "slim" }) : ""}</div>
      <div class="dim xs">${esc(c.note || COMPONENT_HOW[c.how] || "")}${c.steps_left ? ` · ${esc(c.next)} first, then on to ${esc(c.newest)}: one minor version at a time` : ""}${c.error ? ` · could not check for releases: ${esc(c.error)}` : ""}</div>
      ${nodes}${running ? "" : componentCheckLine(c)}</div>
    <div class="row">${c.id === "longhorn" ? actionBar([
      !running && c.next && {label:`Upgrade to ${c.next}`,run:`componentUpgrade(${jsq(c.id)})`,need:'admin'},
      {label:'V2 upgrade status',run:'lhV2Upgrade()'},
      c.notes_url && {label:'Release notes',run:`window.open(${jsq(safeHref(c.notes_url))},'_blank','noopener,noreferrer')`}
    ]) : `${c.notes_url ? `<a class="btn sm" href="${safeHref(c.notes_url)}" target="_blank" rel="noopener noreferrer">${icon("ext")}Release notes</a>` : ""}${action}`}</div></div>`;
}

function componentsCard(r, settings = false) {
  const rows = (r.components || []).filter(c => !(r.harvester && c.id === "cluster"));
  // Counted: what can be upgraded here. Parts that are behind but move with
  // Harvester, or were installed outside Homestead, say who upgrades them.
  const behind = rows.filter(c => c.next).length;
  const elsewhere = rows.filter(c => c.behind && !c.next);
  const by = elsewhere.every(c => c.how === "harvester") ? "upgraded with Harvester"
    : elsewhere.every(c => c.how === "manual") ? "upgraded outside Homestead" : "upgraded elsewhere";
  const pill = behind ? clusterPill("attention", `${behind} update${behind === 1 ? "" : "s"}`)
    : elsewhere.length ? clusterPill("unknown", `${elsewhere.length} newer, ${by}`) : clusterPill("healthy", "up to date");
  const body = `
    ${UI.moduleHeader(`Platform versions`, `What runs under your apps, and whether anything newer is out. ${r.harvester
        ? "Harvester upgrades its own Longhorn and KubeVirt."
        : "Upgrades go one minor version at a time, as each project supports."}`, `${pill}
        <button class="btn sm" onclick="clusterComponentsPaint(true)">${icon("refresh")}Check now</button>`)}
    <div class="release-list">${rows.map(componentRow).join("") || '<div class="dim small">Nothing to report.</div>'}</div>
    ${componentPairsLine(r.pairs)}
  `;
  return settings ? UI.settingsCard(body, {tab:"updates",id:"settingsComponents"})
    : `<section class="card flat cluster-wide" style="margin-top:14px">${body}</section>`;
}

const COMPONENT_EFFECT = {
  cluster: (c, to) => `Each host is cordoned and restarted on ${to} in turn - the servers one at a time, then the agents.
    Apps on a host wait or move while it restarts. On a one-node cluster everything, Homestead too, is away for a minute or two;
    this page comes back by itself. The first time, Rancher's system-upgrade-controller is installed to do it.`,
  longhorn: (c, to) => `Upgrade Longhorn to ${to} after checking its V2 volumes and host requirements.`,
  kubevirt: (c, to) => `KubeVirt's operator rolls ${to} out. Running VMs carry on, and move to the new version as they restart or live-migrate.`,
  cdi: (c, to) => `CDI's operator rolls ${to} out. Disk imports under way may restart.`,
  "kube-vip": (c, to) => `The kube-vip chart is upgraded to ${to} with its current values. The agent restarts on each host in turn;
    each virtual IP is unavailable for a few seconds while it moves to another host.`,
  multus: (c, to) => `The Multus chart is upgraded to ${to} with its current values. The agent restarts on each host. Running pods are not affected;
    pods scheduled during the restart may start a few seconds later.`,
};

window.componentUpgrade = async id => {
  const c = (STATE.data.components?.components || []).find(row => row.id === id);
  if (!c?.next) return;
  if (id === 'longhorn') return lhV2Upgrade(c.next);
  modal(`Upgrade ${c.name} to ${c.next}`, `
    <p>${esc(c.name)} ${esc(c.installed)} → <b class="mono">${esc(c.next)}</b>${c.steps_left ? ` <span class="dim">(then ${esc(c.newest)}, as a further step)</span>` : ""}</p>
    <div class="note">${esc(COMPONENT_EFFECT[id](c, c.next).replace(/\s+/g, " "))}</div>
    ${componentCheckSection(c.check)}
    ${c.notes_url ? `<p class="small"><a href="${safeHref(c.notes_url)}" target="_blank" rel="noopener noreferrer">Read ${esc(c.next)}'s release notes ${icon("ext")}</a> first: they list anything to do before or after.</p>` : ""}
    ${UI.actions(`<button data-dialog-dismiss="true" class="btn" onclick="closeModal()">Cancel</button>
      <button class="btn pri" onclick="componentUpgradeGo(${jsq(id)}, ${jsq(c.next)})">Upgrade to ${esc(c.next)}</button>`)}`);
};

window.componentUpgradeGo = async (id, to) => {
  const ack = $("#cmp_check_ack");
  if (ack && !ack.checked) return toast("Tick that you have read what this upgrade breaks first", "bad");
  try {
    const r = await api("/api/cluster/components/upgrade", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ component: id, to, ...(ack ? { accept_check: true } : {}) }) });
    closeModal();
    toast(r.detail, "ok");
    if (window.noteOperation) noteOperation(r.operation);
    clusterComponentsPaint();
  } catch (e) { toast(e.message, "bad"); }
};

const componentManagePending = new Set();
window.componentManageOperator = id => {
  if (componentManagePending.has(id)) return toast('This installation is still being checked. Please wait.');
  const c = (STATE.data.components?.components || []).find(row => row.id === id);
  if (!c?.can_manage) return;
  modal(`Manage ${c.name} upgrades`, `
    <p>Let Homestead upgrade this ${esc(c.name)} installation using its upstream release manifests.</p>
    <div id="cmp_manage_status" role="status" aria-live="polite"><div class="note">This enables future upgrades. It does not change the installed version, VM disks or existing settings.</div></div>
    ${UI.more('Before enabling', '<p>Use this for a Homestead installer or plain manifest installation. Keep using Helm, GitOps or another operator if it manages this component. Homestead checks for those owners and active installer files before enabling upgrades.</p>')}
    ${UI.ack('cmp_manage_ack', 'I confirm no other system manages these upgrades.', {onchange:"$('#cmp_manage_go').disabled=!this.checked"})}
    ${UI.actions(UI.cancel('Close') + UI.button('Enable upgrades', `componentManageOperatorGo(${jsArg(id)})`,
      {kind:'pri', id:'cmp_manage_go', disabled:true, attrs:'data-need="admin"'}))}`);
};

window.componentManageOperatorGo = async id => {
  if (componentManagePending.has(id)) return;
  if (!$('#cmp_manage_ack')?.checked) return toast('Confirm who manages this installation first', 'bad');
  const button = $('#cmp_manage_go'), ack = $('#cmp_manage_ack'), status = $('#cmp_manage_status');
  if (!button || !status) return;
  componentManagePending.add(id);
  button.disabled = true; button.textContent = 'Checking installation…'; ack.disabled = true;
  status.innerHTML = '<div class="note"><span class="spin2" aria-hidden="true"></span> Checking the installation and host configuration. This can take a few minutes.</div>';
  try {
    const result = await api('/api/cluster/components/manage-operator', {method:'POST', headers:{'Content-Type':'application/json'},
      body:JSON.stringify({component:id, confirm:true})});
    if ($('#cmp_manage_go') === button) closeModal();
    toast(result.detail, 'ok');
    clusterComponentsPaint(true);
  } catch (error) {
    if ($('#cmp_manage_go') === button) {
      status.innerHTML = UI.callout('bad', 'Could not enable upgrades', esc(error.message));
    } else toast(error.message, 'bad');
  } finally {
    componentManagePending.delete(id);
    if ($('#cmp_manage_go') === button) {
      ack.disabled = false; button.disabled = !ack.checked; button.textContent = 'Enable upgrades';
    }
  }
};

window.harvesterUpgradeStart = version => {
  modal(`Upgrade Harvester to ${version}`, `
    <p>Harvester checks the cluster first, then downloads the release, prepares each host, upgrades its own services and then each host in turn -
      moving VMs off a host before it restarts. It takes an hour or more, and cannot be undone from here.</p>
    <div class="note">Before starting: every host Ready, no volume degraded, and a backup of anything precious. VMs that cannot live-migrate
      (a passed-through device, or one host) are shut down while their host restarts.</div>
    <div class="f"><label>Type <b class="mono">${esc(version)}</b> to start</label><input id="hv_up_confirm" class="mono" autocomplete="off"></div>
    ${UI.actions(`<button data-dialog-dismiss="true" class="btn" onclick="closeModal()">Cancel</button>
      <button class="btn pri" onclick="harvesterUpgradeGo(${jsq(version)})">Start the upgrade</button>`)}`);
};

window.harvesterUpgradeGo = async version => {
  if ($("#hv_up_confirm").value.trim() !== version) return toast(`Type ${version} to start it`, "bad");
  try {
    const r = await api("/api/cluster/upgrades/start", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ version }) });
    closeModal();
    toast(r.detail, "ok");
    if (window.noteOperation) noteOperation(r.operation);
    clusterUpgradesPaint(true);
  } catch (e) { toast(e.message, "bad"); }
};

/* ---------------- Harvester releases and upgrades ----------------
   Shown and followed, never started: an upgrade rewrites every host, so it
   is begun from Harvester's own dashboard. */
async function clusterUpgradesPaint(force = false) {
  if (STATE.platform && !STATE.platform.harvester) return;   // Harvester's releases only mean something on Harvester
  try {
    const report = await api("/api/cluster/upgrades" + (force ? "?force=1" : ""));
    STATE.data.upgrades = report;
    STATE.data.upgradeHtml = upgradesCard(report);
    const host = $("#clusterUpgrades");
    if (host) host.innerHTML = STATE.data.upgradeHtml;
    if (force) toast("Releases checked", "ok");
  } catch (e) { if (force) toast(e.message, "bad"); }
}
window.clusterUpgradesPaint = clusterUpgradesPaint;

function releaseDate(value) {
  if (!value) return "";
  return fmtDay(value);
}

function releaseRow(label, row, kind) {
  if (!row) return "";
  return `<div class="release-row"><div><span class="cluster-kicker">${esc(label)}</span>
      <div><b>${esc(row.tag)}</b> ${UI.chip(row.channel === "stable" ? "stable" : row.channel, kind === "stable" ? "ok" : "warn", { size: "slim" })}
        ${row.offered ? UI.chip("offered by Harvester", "info", { size: "slim", tip: "Harvester lists this version, so its dashboard shows an Upgrade button for it" }) : ""}</div>
      <div class="dim xs">${esc(releaseDate(row.published))}${row.channel !== "stable" ? " · a test build: for a lab cluster, not one you depend on" : ""}</div></div>
    ${row.url ? `<a class="btn sm" href="${safeHref(row.url)}" target="_blank" rel="noopener noreferrer">${icon("ext")}Release notes</a>` : ""}</div>`;
}

function upgradeProgress(up) {
  const stateText = up.state === "succeeded" ? "finished" : up.state === "failed" ? "failed" : "in progress";
  return `<div class="upgrade-live ${esc(up.state)}">
    <div class="between"><div><span class="cluster-kicker">${up.state === "running" ? "UPGRADE UNDER WAY" : "LAST UPGRADE"}</span>
      <div><b>${esc(up.previous ? `v${up.previous.replace(/^v/, "")} → ` : "")}${esc(up.version)}</b> · ${esc(stateText)}
        <span class="dim xs">${up.started ? `started ${esc(fmtAgo(Math.max(0, (Date.now() - Date.parse(up.started)) / 1000)))}` : ""}</span></div></div>
      ${clusterPill(up.state === "succeeded" ? "healthy" : up.state === "failed" ? "critical" : "attention", `${up.progress}%`)}</div>
    <div class="upgrade-bar ${esc(up.state)}"><span style="width:${Math.max(0, Math.min(100, +up.progress || 0))}%"></span></div>
    ${up.message ? `<div class="note ${up.state === "failed" ? "bad" : ""}">${esc(up.message)}</div>` : ""}
    <div class="upgrade-steps">${up.steps.map(step => `<span class="upgrade-step ${esc(step.state)}" ${step.message ? `data-tip="${esc(step.message)}"` : ""}>${esc(step.label)}</span>`).join("")}</div>
    ${up.nodes.length ? `<table class="tbl dense stack upgrade-nodes"><thead><tr><th>Host</th><th>State</th><th data-nosort>Detail</th></tr></thead><tbody>
      ${up.nodes.map(node => `<tr><td><b>${esc(node.name)}</b></td>
        <td data-label="State">${UI.chip(node.state || "waiting", /succeed/i.test(node.state) ? "ok" : /fail/i.test(node.state) ? "bad" : "warn", { size: "slim" })}</td>
        <td data-label="Detail" class="dim xs">${esc(node.message || node.reason || "")}</td></tr>`).join("")}</tbody></table>` : ""}</div>`;
}

/* Whether a Harvester version is newer than the one running: v1.5.1 > 1.5.0. */
function upgradeNewer(version, current) {
  const parts = v => String(v).replace(/^v/, "").split(/[.-]/).map(x => parseInt(x, 10) || 0);
  const a = parts(version), b = parts(current);
  for (let i = 0; i < Math.max(a.length, b.length); i++) if ((a[i] || 0) !== (b[i] || 0)) return (a[i] || 0) > (b[i] || 0);
  return false;
}

function upgradesCard(r) {
  const behind = r.stable && r.current;
  const status = !r.current ? "Harvester did not report its version"
    : behind ? `${r.stable.tag} is out; this cluster runs v${r.current}` : `v${r.current} is the newest stable release`;
  return `<section class="card flat cluster-wide upgrades-card" style="margin-top:14px">
    ${UI.moduleHeader(`Harvester releases`, `${esc(status)}. An upgrade Harvester offers can be started here or from its own dashboard; either way it is followed here.`, `${clusterPill(r.active ? "attention" : behind ? "attention" : "healthy", r.active ? "upgrading" : behind ? "update available" : "up to date")}
        <button class="btn sm" onclick="clusterUpgradesPaint(true)">${icon("refresh")}Check now</button>`)}
    ${r.last ? upgradeProgress(r.last) : ""}
    ${r.unread ? UI.callout("warn", "Upgrades not read", `${esc(r.unread)}. Whether an upgrade is under way, and what Harvester offers, is not known; Check now tries again.`) : ""}
    <div class="release-list">
      ${releaseRow("NEWEST STABLE", r.stable, "stable")}
      ${releaseRow("NEWEST TEST BUILD", r.test, "test")}
      ${!r.stable && !r.test ? `<div class="dim small">${r.error ? `Could not reach GitHub for Harvester's releases: ${esc(r.error)}` : "Nothing newer than this cluster's version has been published."}</div>` : ""}
    </div>
    ${(r.offered || []).length ? `<div class="row" style="margin-top:8px;flex-wrap:wrap;gap:6px"><span class="dim xs">Harvester offers:</span>
      ${r.offered.filter(o => !r.current || upgradeNewer(o.version, r.current)).map(o => `${UI.chip(o.version, "neutral")}${r.active ? "" :
        `<button class="btn sm pri" data-need="admin" onclick="harvesterUpgradeStart(${jsq(o.version)})">Upgrade to ${esc(o.version)}</button>`}`).join("")
        || r.offered.map(o => UI.chip(o.version, "neutral")).join("")}</div>` : ""}
    ${r.stable && r.stable.offered === false ? `<div class="note" style="margin-top:10px">Harvester shows an Upgrade button once it lists a version, which its upgrade checker does for supported upgrade paths - sometimes a few days after release. A test build is never offered: installing one means creating its Version by hand, as Harvester's upgrade guide describes.</div>` : ""}
  </section>`;
}


/* ---------------- before an upgrade: what Homestead's table knows ----------------
   Each part Homestead upgrades carries a check (homestead_upgrade_check):
   what its next version is known to break here, from a dated table with a
   source per row. Unknown is "Not checked", never a pass (#620). The pairs
   are what runs now: KubeVirt against the hosts' Kubernetes. */
const CHECK_VERDICT = { blocked: ["Check first", "bad"], check: ["Worth a look", "warn"],
  clear: ["No known breaks", "ok"], unchecked: ["Not checked", "neutral"] };
const CHECK_LEVEL = { block: ["Blocks", "bad"], warn: ["Read first", "warn"], info: ["Checked", "ok"], unknown: ["Not checked", "neutral"] };
const PAIR_STATUS = { supported: ["Supported", "ok"], eol: ["Kubernetes past end of life", "warn"],
  outside: ["Outside its support matrix", "warn"], unchecked: ["Not checked", "neutral"] };

const checkSource = url => url ? `<a class="btn sm" href="${safeHref(url)}" target="_blank" rel="noopener noreferrer">${icon("ext")}Source</a>` : "";

function componentCheckList(r) {
  const rows = (r.findings || []).map(f => { const [label, tone] = CHECK_LEVEL[f.level] || CHECK_LEVEL.unknown;
    return { tone, label, title: f.title, detail: f.detail, actionsHtml: checkSource(f.source) }; });
  return `${rows.length ? UI.insightList(rows) : `<p class="dim small">Homestead's table says nothing about ${esc(r.to)}.</p>`}
    <p class="dim xs">From Homestead's table of known version pairs, checked ${esc(r.checked)}. It covers a few facts only:
      "No known breaks" is not a promise, so read the release notes too.</p>`;
}

function componentCheckLine(c) {
  const r = c.check;
  if (!r) return "";
  const [label, tone] = CHECK_VERDICT[r.verdict] || CHECK_VERDICT.unchecked;
  const top = ["block", "warn", "unknown"].map(level => (r.findings || []).find(f => f.level === level)).find(Boolean);
  const said = top ? top.title : r.verdict === "clear" ? `Nothing in Homestead's table breaks ${r.to} here` : `Homestead's table says nothing about ${r.to}`;
  return `<div class="component-check">${UI.chip(label, tone, { size: "slim" })}<span class="dim xs">${esc(said)}</span></div>
    ${(r.findings || []).length ? UI.more(`What ${r.to} changes`, componentCheckList(r), false, `component-check-${c.id}`) : ""}`;
}

/* The upgrade review's part: every finding, and a tick when one blocks. */
function componentCheckSection(r) {
  if (!r) return "";
  return UI.section("Before you upgrade", componentCheckList(r))
    + (r.verdict === "blocked" ? UI.ack("cmp_check_ack", "I have read what this upgrade breaks, and go ahead anyway") : "");
}

function componentPairsLine(pairs) {
  if (!pairs?.length) return "";
  return `<div class="component-pairs"><span class="cluster-kicker">VERSION PAIRS</span>${pairs.map(p => {
    const [label, tone] = PAIR_STATUS[p.status] || PAIR_STATUS.unchecked;
    return `<div class="component-check">${UI.chip(label, tone, { size: "slim" })}<span class="xs"><b>${esc(p.title)}</b>
      <span class="dim">${esc(p.detail)}</span></span>${checkSource(p.source)}</div>`; }).join("")}</div>`;
}
