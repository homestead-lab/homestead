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
    <button class="btn pri" data-need="admin" onclick="clusterOnboarding()">${icon("plus")}Add a host</button></div>

  <section class="cluster-hero ${esc(report.state)}">
    <div><span class="cluster-kicker">PLATFORM STATUS</span><h3>${esc(report.state === "healthy" ? "Platform healthy" : report.state === "critical" ? "Platform action required" : "Platform online · review items")}</h3>
      <p>${esc(report.summary)}</p></div>${clusterPill(report.state, report.state)}</section>

  ${(report.unavailable || []).length ? `<div class="note"><b>Partial platform data.</b> ${esc(report.unavailable.map(row => row.section).join(", "))} could not be read. Other sections remain live.</div>` : ""}

  <div class="cluster-facts">
    <section><span>Harvester</span><b>${versions.harvester ? `v${esc(versions.harvester)}` : "Not reported"}</b><small>Host operating platform</small></section>
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
      <button class="btn" data-need="admin" onclick="clusterOnboarding()">Add a host</button></section>
  </div>
  <div id="clusterUpgrades">${STATE.data.upgradeHtml || ""}</div>
  <div id="clusterCleanup">${STATE.data.cleanupHtml || ""}</div>`);
  // Admin only, and a round trip of its own: the page does not wait for it.
  if (window.can && can("admin")) clusterCleanupPaint();
  // GitHub is asked at most hourly, so this is cheap; still, the page does not wait.
  clusterUpgradesPaint();
}

/* ---------------- Harvester releases and upgrades ----------------
   Shown and followed, never started: an upgrade rewrites every host, so it
   is begun from Harvester's own dashboard. */
async function clusterUpgradesPaint(force = false) {
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

function upgradesCard(r) {
  const behind = r.stable && r.current;
  const status = !r.current ? "Harvester did not report its version"
    : behind ? `${r.stable.tag} is out; this cluster runs v${r.current}` : `v${r.current} is the newest stable release`;
  return `<section class="card flat cluster-wide upgrades-card" style="margin-top:14px">
    <div class="settings-card-head"><div><div class="ctitle">Harvester releases</div>
      <div class="csub">${esc(status)}. Upgrades are started from Harvester's own dashboard; Homestead follows them here.</div></div>
      <div class="row">${clusterPill(r.active ? "attention" : behind ? "attention" : "healthy", r.active ? "upgrading" : behind ? "update available" : "up to date")}
        <button class="btn sm" onclick="clusterUpgradesPaint(true)">${icon("refresh")}Check</button></div></div>
    ${r.last ? upgradeProgress(r.last) : ""}
    <div class="release-list">
      ${releaseRow("NEWEST STABLE", r.stable, "stable")}
      ${releaseRow("NEWEST TEST BUILD", r.test, "test")}
      ${!r.stable && !r.test ? `<div class="dim small">${r.error ? `Could not reach GitHub for Harvester's releases: ${esc(r.error)}` : "Nothing newer than this cluster's version has been published."}</div>` : ""}
    </div>
    ${(r.offered || []).length ? `<div class="dim xs" style="margin-top:8px">Harvester offers: ${r.offered.map(o => `<span class="tag">${esc(o.version)}</span>`).join("")}</div>` : ""}
    ${r.stable && !r.stable.offered ? `<div class="note" style="margin-top:10px">Harvester shows an Upgrade button once it lists a version, which its upgrade checker does for supported upgrade paths - sometimes a few days after release. A test build is never offered: installing one means creating its Version by hand, as Harvester's upgrade guide describes.</div>` : ""}
  </section>`;
}

