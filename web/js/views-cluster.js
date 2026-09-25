/* Platform health: Harvester/Kubernetes control plane, quorum and onboarding. */

function clusterPill(state, text) {
  const cls = state === "healthy" ? "low" : state === "critical" ? "bad" : state === "unknown" ? "neutral" : "med";
  return `<span class="pill ${cls}">${esc(text || state || "unknown")}</span>`;
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
    <div class="cluster-node-foot"><span>${node.schedulable ? "Accepting workloads" : "Cordoned"}</span>
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
  paint(`<div class="phead"><div><h2>Cluster</h2><p>Harvester and Kubernetes platform health, separate from application health</p></div>
    <div class="row"><button class="btn" data-need="admin" onclick="clusterRemovePick()">${icon("trash")}Remove a host</button>
      <button class="btn pri" data-need="admin" onclick="clusterOnboarding()">${icon("plus")}Add a host</button></div></div>

  <section class="cluster-hero ${esc(report.state)}">
    <div><span class="cluster-kicker">PLATFORM STATUS</span><h3>${esc(report.state === "healthy" ? "Platform healthy" : report.state === "critical" ? "Platform action required" : "Platform online · review items")}</h3>
      <p>${esc(report.summary)}</p></div>${clusterPill(report.state, report.state)}</section>

  ${(report.unavailable || []).length ? `<div class="note"><b>Partial platform data.</b> ${esc(report.unavailable.map(row => row.section).join(", "))} could not be read. Other sections remain live.</div>` : ""}

  <div class="cluster-facts">
    ${STATE.platform && !STATE.platform.harvester
      ? `<section><span>${esc(platformName(STATE.platform))}</span><b>${STATE.platform.version ? `v${esc(STATE.platform.version)}` : "Not reported"}</b><small>Kubernetes distribution</small></section>`
      : `<section><span>Harvester</span><b>${versions.harvester ? `v${esc(versions.harvester)}` : "Not reported"}</b><small>Host operating platform</small></section>`}
    <section><span>Kubernetes</span><b>${versions.kubernetes ? `v${esc(versions.kubernetes)}` : "Not reported"}</b><small>Cluster API version</small></section>
    <section><span>Control plane</span><b>${cp.ready ?? 0}/${cp.total ?? 0} ready</b><small>API and scheduling hosts</small></section>
    <section><span>etcd quorum ${tip("The number of additional ready etcd members that can be lost before the control plane loses quorum.")}</span><b>${esc(marginCopy)}</b><small>${cp.etcd_ready ?? 0}/${cp.etcd_total ?? 0} ready · ${cp.quorum_needed ?? "—"} needed</small></section>
  </div>

  <div class="cluster-layout">
    <section class="card flat cluster-wide"><div class="settings-card-head"><div><div class="ctitle">Nodes and capacity pressure</div>
      <div class="csub">Roles, scheduling state, resource use, and Kubernetes pressure conditions</div></div>
      ${clusterPill((report.capacity?.unready || []).length ? "critical" : (report.capacity?.pressure || []).length ? "attention" : "healthy",
        `${(report.nodes || []).filter(node => node.ready).length}/${(report.nodes || []).length} ready`)}</div>
      <div class="cluster-node-grid">${(report.nodes || []).map(clusterNodeCard).join("") || '<div class="empty">No nodes reported.</div>'}</div></section>

    <section class="card flat cluster-wide"><div class="settings-card-head"><div><div class="ctitle">Critical platform services</div>
      <div class="csub">Observed system pods grouped by the job they perform</div></div></div>
      <div class="cluster-service-grid">${serviceRows.map(clusterServiceCard).join("") || '<div class="empty">System service inventory unavailable.</div>'}</div>
      <div class="dim xs cluster-legend">“Not observed” is not treated as failed: some Harvester releases package or label components differently.</div></section>

    <section class="card flat"><div class="settings-card-head"><div><div class="ctitle">Certificates and requests</div>
      <div class="csub">Kubernetes certificate-signing requests; secret material is never displayed</div></div>${clusterPill(certs.state, certs.state)}</div>
      <div class="cluster-mini-stats"><span><b>${certs.pending || 0}</b> pending</span><span><b>${certs.failed || 0}</b> denied / failed</span><span><b>${certs.expiring || 0}</b> nearing requested lifetime</span></div>
      ${(certs.entries || []).length ? `<div class="cluster-csr-list">${certs.entries.slice(0, 5).map(row => `<div><span><b>${esc(row.name)}</b><small>${esc(row.signer || "unknown signer")}</small></span>${clusterPill(row.state === "approved" ? "healthy" : row.state === "pending" ? "attention" : "critical", row.state)}</div>`).join("")}</div>` : '<div class="empty small">No certificate-signing requests are currently retained.</div>'}
      <div class="dim xs">${esc(certs.note || "")}</div></section>

    <section class="card flat"><div class="settings-card-head"><div><div class="ctitle">Recent platform warnings</div>
      <div class="csub">Warning events from system namespaces and node objects in the last 24 hours</div></div>${clusterPill(warnings.length ? "attention" : "healthy", warnings.length ? `${warnings.length} warning${warnings.length === 1 ? "" : "s"}` : "clear")}</div>
      <div class="cluster-warning-list">${warnings.slice(0, 8).map(row => `<div><span class="pill med">${esc(row.reason)}</span><span><b>${esc(row.object)}</b><small>${esc(row.namespace)} · ${esc(row.message)}</small></span><time>${esc(fmtAgo(row.age_seconds))}</time></div>`).join("") || '<div class="empty small">No recent platform warnings.</div>'}</div>
      ${warnings.length ? '<button class="btn sm" onclick="go(\'events\')">View all events</button>' : ""}</section>

    <section class="card flat cluster-wide cluster-onboard-preview"><div><span class="cluster-kicker">NEXT NODE</span>
      <div class="ctitle">Recommended role: ${esc(report.onboarding?.recommended_role || "Review required")}</div>
      <div class="csub">${esc(report.onboarding?.reason || "Review the current control-plane layout before joining another host.")}</div></div>
      <div class="row"><button class="btn" data-need="admin" onclick="clusterRemovePick()">Remove a host</button>
        <button class="btn" data-need="admin" onclick="clusterOnboarding()">Add a host</button></div></section>
  </div>
  <div id="clusterComponents">${STATE.data.componentsHtml || ""}</div>
  <div id="clusterUpgrades">${STATE.data.upgradeHtml || ""}</div>
  <div id="clusterCleanup">${STATE.data.cleanupHtml || ""}</div>`);
  // Admin only, and a round trip of its own: the page does not wait for it.
  if (window.can && can("admin")) clusterCleanupPaint();
  // GitHub is asked at most hourly, so this is cheap; still, the page does not wait.
  clusterUpgradesPaint();
  clusterComponentsPaint();
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
    if (window.applyRole) applyRole();
    if (force) toast("releases checked", "ok");
  } catch (e) { if (force) toast(e.message, "bad"); }
}
window.clusterComponentsPaint = clusterComponentsPaint;

const COMPONENT_HOW = {
  harvester: "comes with Harvester", helmchart: "installed by Homestead", suc: "system-upgrade-controller",
  manual: "installed outside Homestead",
};

function componentRow(c) {
  const running = (STATE.data.operations || []).find(op => op.kind === "platform-upgrade" && !["succeeded", "failed", "cancelled"].includes(op.status)
    && op.title.startsWith(`Upgrade ${c.name} `));
  const state = c.id === "cluster" && c.how === "harvester" ? ""
    : !c.installed ? '<span class="pill slim">not reported</span>'
    : c.behind ? `<span class="pill slim ${c.next ? "warn" : ""}" ${c.next ? "" : `data-tip="${esc(c.note || "")}"`}>${esc(c.newest)} is out</span>` : '<span class="pill slim ok">up to date</span>';
  const nodes = c.mixed && c.nodes ? `<div class="dim xs">${Object.entries(c.nodes).map(([n, v]) => `${esc(n)} ${esc(v)}`).join(" · ")}</div>` : "";
  const action = running ? `<span class="pill slim info">upgrading</span>`
    : c.next ? `<button class="btn sm pri" data-need="admin" onclick="componentUpgrade('${esc(c.id)}')">Upgrade to ${esc(c.next)}</button>` : "";
  return `<div class="release-row component-row"><div>
      <span class="cluster-kicker">${esc(c.name.toUpperCase())}</span>
      <div><b class="mono">${esc(c.installed || "—")}</b> ${state}
        ${c.phase && c.phase !== "Deployed" ? `<span class="pill slim med">${esc(c.phase)}</span>` : ""}</div>
      <div class="dim xs">${esc(c.note || COMPONENT_HOW[c.how] || "")}${c.steps_left ? ` · ${esc(c.next)} first, then on to ${esc(c.newest)}: one minor version at a time` : ""}${c.error ? ` · could not check for releases: ${esc(c.error)}` : ""}</div>
      ${nodes}</div>
    <div class="row">${c.notes_url ? `<a class="btn sm" href="${esc(c.notes_url)}" target="_blank" rel="noopener noreferrer">${icon("ext")}Notes</a>` : ""}${action}</div></div>`;
}

function componentsCard(r) {
  const rows = (r.components || []).filter(c => !(r.harvester && c.id === "cluster"));
  // Counted: what can be upgraded here. Harvester's own parts move with it.
  const behind = rows.filter(c => c.next).length;
  return `<section class="card flat cluster-wide" style="margin-top:14px">
    <div class="settings-card-head"><div><div class="ctitle">Platform versions</div>
      <div class="csub">What runs under your apps, and whether anything newer is out. ${r.harvester
        ? "Harvester upgrades its own Longhorn and KubeVirt."
        : "Upgrades go one minor version at a time, as each project supports."}</div></div>
      <div class="row">${clusterPill(behind ? "attention" : "healthy", behind ? `${behind} update${behind === 1 ? "" : "s"}` : "up to date")}
        <button class="btn sm" onclick="clusterComponentsPaint(true)">${icon("refresh")}Check</button></div></div>
    <div class="release-list">${rows.map(componentRow).join("") || '<div class="dim small">Nothing to report.</div>'}</div>
  </section>`;
}

const COMPONENT_EFFECT = {
  cluster: (c, to) => `Each node is cordoned and restarted on ${to} in turn - the servers one at a time, then the agents.
    Apps on a node wait or move while it restarts. On a one-node cluster everything, Homestead too, is away for a minute or two;
    this page comes back by itself. The first time, Rancher's system-upgrade-controller is installed to do it.`,
  longhorn: (c, to) => `Longhorn's manager, UI and engines restart on ${to}. Volumes stay attached and apps keep running, though
    each volume's engine is upgraded as it next detaches or live, depending on Longhorn's settings. A backup of anything precious first is wise.`,
  kubevirt: (c, to) => `KubeVirt's operator rolls ${to} out. Running VMs carry on, and move to the new version as they restart or live-migrate.`,
  cdi: (c, to) => `CDI's operator rolls ${to} out. Disk imports under way may restart.`,
};

window.componentUpgrade = async id => {
  const c = (STATE.data.components?.components || []).find(row => row.id === id);
  if (!c?.next) return;
  modal(`Upgrade ${c.name} to ${c.next}`, `
    <p>${esc(c.name)} ${esc(c.installed)} → <b class="mono">${esc(c.next)}</b>${c.steps_left ? ` <span class="dim">(then ${esc(c.newest)}, as a further step)</span>` : ""}</p>
    <div class="note">${esc(COMPONENT_EFFECT[id](c, c.next).replace(/\s+/g, " "))}</div>
    ${c.notes_url ? `<p class="small"><a href="${esc(c.notes_url)}" target="_blank" rel="noopener noreferrer">Read ${esc(c.next)}'s release notes ${icon("ext")}</a> first: they list anything to do before or after.</p>` : ""}
    <div class="modalactions"><button class="btn" onclick="closeModal()">Cancel</button>
      <button class="btn pri" onclick="componentUpgradeGo('${esc(id)}', '${esc(c.next)}')">Upgrade to ${esc(c.next)}</button></div>`);
};

window.componentUpgradeGo = async (id, to) => {
  try {
    const r = await api("/api/cluster/components/upgrade", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ component: id, to }) });
    closeModal();
    toast(r.detail, "ok");
    if (window.noteOperation) noteOperation(r.operation);
    clusterComponentsPaint();
  } catch (e) { toast(e.message, "bad"); }
};

window.harvesterUpgradeStart = version => {
  modal(`Upgrade Harvester to ${version}`, `
    <p>Harvester checks the cluster first, then downloads the release, prepares each node, upgrades its own services and then each node in turn -
      moving VMs off a node before it restarts. It takes an hour or more, and cannot be undone from here.</p>
    <div class="note">Before starting: every node Ready, no volume degraded, and a backup of anything precious. VMs that cannot live-migrate
      (a passed-through device, or one node) are shut down while their node restarts.</div>
    <div class="f"><label>Type <b class="mono">${esc(version)}</b> to start</label><input id="hv_up_confirm" class="mono" autocomplete="off"></div>
    <div class="modalactions"><button class="btn" onclick="closeModal()">Cancel</button>
      <button class="btn pri" onclick="harvesterUpgradeGo('${esc(version)}')">Start the upgrade</button></div>`);
};

window.harvesterUpgradeGo = async version => {
  if ($("#hv_up_confirm").value.trim() !== version) return toast(`type ${version} to start it`, "bad");
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
    if (force) toast("releases checked", "ok");
  } catch (e) { if (force) toast(e.message, "bad"); }
}
window.clusterUpgradesPaint = clusterUpgradesPaint;

function releaseDate(value) {
  if (!value) return "";
  const date = new Date(value);
  return isNaN(date) ? "" : date.toLocaleDateString(undefined, { day: "numeric", month: "short", year: "numeric" });
}

function releaseRow(label, row, kind) {
  if (!row) return "";
  return `<div class="release-row"><div><span class="cluster-kicker">${esc(label)}</span>
      <div><b>${esc(row.tag)}</b> <span class="pill slim ${kind === "stable" ? "ok" : "warn"}">${esc(row.channel === "stable" ? "stable" : row.channel)}</span>
        ${row.offered ? '<span class="pill slim info" data-tip="Harvester lists this version, so its dashboard shows an Upgrade button for it">offered by Harvester</span>' : ""}</div>
      <div class="dim xs">${esc(releaseDate(row.published))}${row.channel !== "stable" ? " · a test build: for a lab cluster, not one you depend on" : ""}</div></div>
    ${row.url ? `<a class="btn sm" href="${esc(row.url)}" target="_blank" rel="noopener noreferrer">${icon("ext")}Release notes</a>` : ""}</div>`;
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
    ${up.nodes.length ? `<table class="tbl dense stack upgrade-nodes"><thead><tr><th>Node</th><th>State</th><th data-nosort>Detail</th></tr></thead><tbody>
      ${up.nodes.map(node => `<tr><td><b>${esc(node.name)}</b></td>
        <td data-label="State"><span class="pill slim ${/succeed/i.test(node.state) ? "ok" : /fail/i.test(node.state) ? "crit" : "med"}">${esc(node.state || "waiting")}</span></td>
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
    <div class="settings-card-head"><div><div class="ctitle">Harvester releases</div>
      <div class="csub">${esc(status)}. An upgrade Harvester offers can be started here or from its own dashboard; either way it is followed here.</div></div>
      <div class="row">${clusterPill(r.active ? "attention" : behind ? "attention" : "healthy", r.active ? "upgrading" : behind ? "update available" : "up to date")}
        <button class="btn sm" onclick="clusterUpgradesPaint(true)">${icon("refresh")}Check</button></div></div>
    ${r.last ? upgradeProgress(r.last) : ""}
    <div class="release-list">
      ${releaseRow("NEWEST STABLE", r.stable, "stable")}
      ${releaseRow("NEWEST TEST BUILD", r.test, "test")}
      ${!r.stable && !r.test ? `<div class="dim small">${r.error ? `Could not reach GitHub for Harvester's releases: ${esc(r.error)}` : "Nothing newer than this cluster's version has been published."}</div>` : ""}
    </div>
    ${(r.offered || []).length ? `<div class="row" style="margin-top:8px;flex-wrap:wrap;gap:6px"><span class="dim xs">Harvester offers:</span>
      ${r.offered.filter(o => !r.current || upgradeNewer(o.version, r.current)).map(o => `<span class="tag">${esc(o.version)}</span>${r.active ? "" :
        `<button class="btn sm pri" data-need="admin" onclick="harvesterUpgradeStart('${esc(o.version)}')">Upgrade to ${esc(o.version)}</button>`}`).join("")
        || r.offered.map(o => `<span class="tag">${esc(o.version)}</span>`).join("")}</div>` : ""}
    ${r.stable && !r.stable.offered ? `<div class="note" style="margin-top:10px">Harvester shows an Upgrade button once it lists a version, which its upgrade checker does for supported upgrade paths - sometimes a few days after release. A test build is never offered: installing one means creating its Version by hand, as Harvester's upgrade guide describes.</div>` : ""}
  </section>`;
}

