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
      <span><small>Pods ${tip("Kubernetes runs each copy of an app as a pod.")}</small><b>${node.pods ?? "—"}</b></span>
    </div>
    <div class="cluster-node-foot"><span>${node.schedulable ? "Accepting apps and VMs" : "Cordoned"}</span>
      ${node.pressure?.length ? `<span class="warntext">${esc(node.pressure.join(", "))}</span>` : `<span>${esc(node.version || "Version unavailable")}</span>`}</div>
  </article>`;
}

/* The page's one line (Nov 2026 mockup): how the platform is, how many hosts
   are ready and how many voting hosts can fail, and the versions. */
function clusterSummary(report) {
  const cp = report.control_plane || {}, versions = report.versions || {}, nodes = report.nodes || [];
  const ready = nodes.filter(n => n.ready).length, margin = cp.quorum_margin;
  const head = report.state === "healthy" ? "The platform is healthy." : report.state === "critical" ? "The platform needs action." : "The platform has items to check.";
  const votes = margin === null || margin === undefined ? "" : margin === 0 ? ", and no voting host can fail" : `, and ${margin} voting host${margin === 1 ? "" : "s"} can fail`;
  const platform = STATE.platform && !STATE.platform.harvester ? (STATE.platform.version ? `${platformName(STATE.platform)} v${STATE.platform.version}` : "")
    : versions.harvester ? `Harvester v${versions.harvester}` : "";
  const kube = versions.kubernetes ? `Kubernetes v${String(versions.kubernetes).replace(/\+.*$/, "")}` : "";
  return UI.summary({ head: `<b>${head}</b>`, parts: [
    { text: `${ready} of ${nodes.length} host${nodes.length === 1 ? " is" : "s are"} ready${votes}.${[platform, kube].filter(Boolean).length ? ` ${[platform, kube].filter(Boolean).join(", ")}.` : ""}`, note: true }],
    narrow: { parts: [] } });
}

/* The hosts in one line, as the Hosts page has them in full: how many are
   ready and take apps, and each one that is not, or is under pressure. */
function clusterHostsLine(report) {
  const nodes = report.nodes || [], ready = nodes.filter(n => n.ready && n.schedulable !== false);
  const trouble = nodes.filter(n => !n.ready || n.pressure?.length || n.schedulable === false);
  const words = n => !n.ready ? "is not ready" : n.pressure?.length ? `is under ${n.pressure.join(" and ").replace(/Pressure/g, " pressure").toLowerCase()}` : "is cordoned";
  return `<div class="card flat cluster-hosts-line"><i class="share-dot ${trouble.some(n => !n.ready) ? "bad" : ""}" aria-hidden="true"></i>
    <div><b>${ready.length} of ${nodes.length} host${nodes.length === 1 ? " is" : "s are"} ready</b> <span class="dim">and accept${ready.length === 1 ? "s" : ""} apps and VMs.</span>
      ${trouble.map(n => `<span class="${!n.ready ? "sum-bad" : "sum-warn"}">${esc(`${n.name} ${words(n)}.`)}</span>`).join(" ")}</div>
    <button type="button" class="linkish" onclick="go('nodes')">Hosts ›</button></div>`;
}

/* A platform service as a chip: its health as a dot, its copies on hover. */
function clusterServiceChip(service) {
  const tone = service.state === "healthy" ? "ok" : service.state === "attention" ? "warn" : service.pods ? "bad" : "neutral";
  return `<span class="cluster-service-chip" tabindex="0" data-card="cluster-service|${esc(service.id || service.name)}"><i class="share-dot ${tone === "ok" ? "" : tone === "neutral" ? "off" : "bad"}" aria-hidden="true"></i>${esc(service.name)}</span>`;
}
if (typeof HOVER_CARDS === "object") HOVER_CARDS["cluster-service"] = id => {
  const s = ((STATE.data.cluster || {}).services || []).find(x => (x.id || x.name) === id);
  if (!s) return "";
  return UI.card({ title: s.name, chipHtml: clusterPill(s.state, s.state === "attention" ? "Partial" : s.state), rows: [
    ["Copies ready", s.pods ? esc(`${s.ready} of ${s.pods} pods`) : "Not found", "layers"],
    ["Needed", s.required ? "Yes: the cluster depends on it" : "No: only some features use it", "info"],
  ] });
};

function clusterServiceCard(service) {
  const detail = service.pods ? `${service.ready} of ${service.pods} pods ready` : "Not found";
  return `<article class="cluster-service ${esc(service.state)}">
    <div><b>${esc(service.name)}</b><span>${esc(detail)}</span></div>
    ${clusterPill(service.state, service.state === "attention" ? "Partial" : service.state)}
  </article>`;
}

/* The etcd members, a tiebreaker told apart (homestead_roles): with two there
   is no failure margin, and a small machine can be the third vote. */
function etcdMembersHtml(cp, admin) {
  const members = (cp.members || []).map(m => `${esc(m.name)}${m.role === "tiebreaker" ? ` ${UI.chip("tiebreaker", "neutral", { size: "slim" })} <span class="dim xs">Votes only</span>` : ""}${m.ready ? "" : ` ${UI.chip("down", "bad", { size: "slim" })}`}`);
  // The join guide is for k3s and RKE2; Harvester adds hosts its own way.
  const joinable = ["k3s", "rke2"].includes(STATE.platform?.distribution) && !STATE.platform?.harvester;
  const add = cp.etcd_total === 2 && admin && joinable ? ` ${UI.button("Add a tiebreaker", "platformJoinGuide('tiebreaker')")}` : "";
  // On k3s an app host can be the third vote too (#364).
  const promote = cp.etcd_total === 2 && admin && STATE.platform?.distribution === "k3s" && !STATE.platform?.harvester
    ? ' <span class="dim xs">or make an app host a server under its Host actions</span>' : "";
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
  const marginCopy = margin === null || margin === undefined ? "Unknown" : margin === 0 ? "None can fail" : `${margin} can fail`;
  const serviceRows = (report.services || []).filter(service => service.pods || service.required);
  const healthyServices = serviceRows.filter(s => s.state === "healthy").length;
  paint(`${UI.pageHeader(`Cluster`, clusterSummary(report), `${moreMenu([{ label: "Shut down cluster…", icon: "power", run: "clusterShutdown()", need: "admin", danger: true }, { label: "Remove a host", icon: "trash", run: "clusterRemovePick()", need: "admin", danger: true }])}
      <button class="btn pri" data-need="admin" onclick="clusterOnboarding()">${icon("plus")}Add a host</button>`)}

  ${(report.unavailable || []).length ? `<div class="note"><b>Some platform data is missing.</b> ${esc(report.unavailable.map(row => row.section).join(", "))} could not be read. The other sections are current.</div>` : ""}



  <section class="card flat" id="clusterHealth">${UI.moduleHeader("Suggestions", "For the cluster, most important first")}<div data-insight="full">${HealthInsights.fullBody()}</div></section>
  ${clusterHostsLine(report)}
  <div class="cluster-layout">
    <section class="card flat cluster-wide">${UI.moduleHeader(`Platform services`, ``)}
      <div class="cluster-service-chips">${serviceRows.map(clusterServiceChip).join("") || '<div class="empty">The list of system services is unavailable.</div>'}</div>
      <div class="dim xs cluster-legend">${healthyServices === serviceRows.length ? `All ${serviceRows.length} services are healthy.` : `${healthyServices} of ${serviceRows.length} services are healthy.`} A service's copies show on hover.${serviceRows.some(s => !s.pods) ? " “Not found” is not treated as failed: some Harvester releases package or label components differently." : ""}</div></section>

    <div class="cluster-wide">${UI.more("Control plane and votes", `<p class="dim small" style="margin:0 0 10px">${esc(report.summary)}</p>
      <div class="cluster-facts">
        ${STATE.platform && !STATE.platform.harvester
          ? `<section><span>${esc(platformName(STATE.platform))}</span><b>${STATE.platform.version ? `v${esc(STATE.platform.version)}` : "Not reported"}</b><small>Kubernetes distribution</small></section>`
          : `<section><span>Harvester</span><b>${versions.harvester ? `v${esc(versions.harvester)}` : "Not reported"}</b><small>Host operating platform</small></section>`}
        <section><span>Kubernetes</span><b>${versions.kubernetes ? `v${esc(versions.kubernetes)}` : "Not reported"}</b><small>Cluster API version</small></section>
        <section><span>Control plane</span><b>${cp.ready ?? 0}/${cp.total ?? 0} ready</b><small>API and scheduling hosts</small></section>
        <section><span>Cluster votes ${tip("How many more voting hosts can fail before the cluster stops working. Kubernetes stores the cluster state in etcd, and a majority of its members (quorum) must be running.")}</span><b>${esc(marginCopy)}</b><small>${cp.etcd_ready ?? 0} of ${cp.etcd_total ?? 0} ready · ${cp.quorum_needed ?? "—"} needed</small>${etcdMembersHtml(cp, can("admin"))}</section>
      </div>`)}</div>

    <section class="card flat">${UI.moduleHeader(`Certificates and requests`, `Kubernetes certificate signing requests. Secret material is never shown.`, `${clusterPill(certs.state, certs.state)}`)}
      <div class="cluster-mini-stats"><span><b>${certs.pending || 0}</b> pending</span><span><b>${certs.failed || 0}</b> denied / failed</span><span><b>${certs.expiring || 0}</b> nearing requested lifetime</span></div>
      ${(certs.entries || []).length ? `<div class="cluster-csr-list">${certs.entries.slice(0, 5).map(row => `<div><span><b>${esc(row.name)}</b><small>${esc(row.signer || "Unknown signer")}</small></span>${clusterPill(row.state === "approved" ? "healthy" : row.state === "pending" ? "attention" : "critical", row.state)}</div>`).join("")}</div>` : '<div class="empty small">No certificate-signing requests are currently retained.</div>'}
      <div class="dim xs">${esc(certs.note || "")}</div></section>

    <section class="card flat">${UI.moduleHeader(`Recent platform warnings`, `Warning events from system namespaces and hosts in the last 24 hours`, `${clusterPill(warnings.length ? "attention" : "healthy", warnings.length ? `${warnings.length} warning${warnings.length === 1 ? "" : "s"}` : "clear")}`)}
      <div class="cluster-warning-list">${warnings.slice(0, 8).map(row => `<div>${UI.chip(row.reason, "warn")}<span><b>${esc(row.object)}</b><small>${esc(row.namespace)} · ${esc(row.message)}</small></span><time>${esc(fmtAgo(row.age_seconds))}</time></div>`).join("") || '<div class="empty small">No recent platform warnings.</div>'}</div>
      ${warnings.length ? '<button class="btn sm" onclick="go(\'events\')">View all events</button>' : ""}</section>

    <section class="card flat cluster-wide cluster-onboard-preview"><div><span class="cluster-kicker">NEXT NODE</span>
      <div class="ctitle">Recommended role: ${esc(report.onboarding?.recommended_role || "Needs review")}</div>
      <div class="csub">${esc(report.onboarding?.reason || "Review the current control plane before adding another host.")}</div></div>
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
      <div class="empty small">The platform versions could not be loaded. ${esc(e.message)}</div>`;
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
  const harvester = c.id === "cluster" && c.how === "harvester";
  // One line: the version it runs, the one offered next, and one reason.
  const version = !c.installed ? UI.chip("not reported", "neutral", { size: "slim" })
    : `<b class="mono">${esc(c.installed)}</b>${c.next ? ` → <b class="mono">${esc(c.next)}</b>`
      : harvester || c.behind ? "" : ` ${UI.chip("up to date", "ok", { size: "slim" })}`}`;
  const reason = componentReason(c);
  const nodes = c.mixed && c.nodes ? `<div class="dim xs">${Object.entries(c.nodes).map(([n, v]) => `${esc(n)} ${esc(v)}`).join(" · ")}</div>` : "";
  const action = running ? UI.chip("upgrading", "info", { size: "slim" })
    : c.next ? `<button class="btn sm pri" data-need="admin" onclick="componentUpgrade(${jsq(c.id)})">Upgrade</button>`
    : c.can_manage ? `<button class="btn sm" data-need="admin" onclick="componentManageOperator(${jsq(c.id)})">Enable upgrades</button>` : "";
  return `<div class="release-row component-row"><div>
      <span class="cluster-kicker" title="${esc(c.note || COMPONENT_HOW[c.how] || "")}">${esc(c.name.toUpperCase())}</span>
      <div>${version}${c.phase && c.phase !== "Deployed" ? ` ${UI.chip(c.phase, "warn", { size: "slim" })}` : ""}${reason ? ` <span class="dim xs">· ${reason}</span>` : ""}</div>
      ${c.error ? `<div class="dim xs">Could not check for releases: ${esc(c.error)}</div>` : ""}
      ${nodes}${running ? "" : componentCheckLine(c)}</div>
    <div class="row">${c.id === "longhorn" ? actionBar([
      !running && c.next && {label:`Upgrade to ${c.next}`,run:`componentUpgrade(${jsq(c.id)})`,need:'admin'},
      {label:'V2 upgrade status',run:'lhV2Upgrade()'},
      c.notes_url && {label:'Release notes',run:`window.open(${jsq(safeHref(c.notes_url))},'_blank','noopener,noreferrer')`}
    ]) : `${c.notes_url ? `<a class="btn sm" href="${safeHref(c.notes_url)}" target="_blank" rel="noopener noreferrer">${icon("ext")}Release notes</a>` : ""}${action}`}</div></div>`;
}

/* The one reason after the versions: the steps still to come, the newer
   versions held back by the validated-versions list, or who upgrades it. */
function componentReason(c) {
  const held = c.held || [];
  const bad = held.find(h => h.status === "bad");
  const untested = held.filter(h => h.status !== "bad");
  const parts = [];
  if (c.steps_left && c.next && !held.some(h => h.version === c.newest)) parts.push(`then on to ${esc(c.newest)}, one minor version at a time`);
  if (bad) parts.push(`<span title="${esc(bad.reason || "")}">${esc(bad.version)} held back: ${esc(bad.reason && bad.reason.length <= 60 ? bad.reason : "known to break something")}</span>`);
  if (untested.length) parts.push(`${esc(untested[0].version)}${untested.length > 1 ? ` and ${untested.length - 1} more` : ""} not tested yet`);
  if (!held.length && c.versions_note) parts.push(esc(c.versions_note));
  if (!parts.length && c.behind && !c.next) {
    const how = c.note || COMPONENT_HOW[c.how] || "upgraded elsewhere";
    parts.push(`${esc(c.newest)} is out, ${esc(how.charAt(0).toLowerCase() + how.slice(1))}`);
  }
  return parts.join(" · ");
}

/* Settings > Updates: one choice, validated versions only or the newest, for
   the platform parts and the host kernels together; split on request
   (homestead_validated keeps the two apart). */
async function validatedVersionsPaint() {
  const host = $("#validatedVersionsCard");
  if (!host) return;
  let r;
  try { r = await api("/api/updates/validated"); }
  catch (e) { host.innerHTML = `${UI.moduleHeader("Which versions to install", "", "")}${UI.callout("bad", "Homestead could not read its list of tested versions.", esc(e.message))}`; return; }
  STATE.data.validated = r;
  const s = r.settings, split = STATE.validatedSplit || s.platform !== s.host;
  const choose = kind => `<div class="answer-modes boxed">${[
      ["validated", "Validated only", "Waits until Homestead has tested a version, and never offers one known to break something."],
      ["any", "Newest", "Offers each version as soon as it is published, one minor version at a time."],
    ].map(([value, label, help]) => `<label class="answer-mode"><input type="radio" name="vv_${kind}" value="${value}" ${s[kind] === value ? "checked" : ""} data-need="admin" onchange="validatedVersionsSave()">
      <span><b>${label}${value === "validated" ? ` ${UI.chip("recommended", "ok", { size: "slim" })}` : ""}</b><span class="dim small">${help}</span></span></label>`).join("")}</div>`;
  const from = r.source === "downloaded" ? "downloaded" : "built into this release";
  host.innerHTML = `${UI.moduleHeader("Which versions to install",
      split ? "" : "For k3s, RKE2, Longhorn, KubeVirt and CDI, and for host kernels.",
      r.stale ? UI.chip("list out of date", "warn") : "")}
    ${split ? `<div class="cluster-kicker">K3S, RKE2, LONGHORN, KUBEVIRT AND CDI</div>${choose("platform")}
      <div class="cluster-kicker" style="margin-top:12px">HOST KERNELS</div>${choose("host")}` : choose("platform")}
    <div class="dim xs" style="margin-top:10px">Tested list from ${esc(r.generated.slice(0, 10))}, ${esc(from)}. Host kernels are tested on Ubuntu 26.04; on other systems, kernel updates work as before.
      ${r.error ? `The last download failed: ${esc(r.error)}` : ""}
      <button class="linkish" data-need="admin" onclick="validatedVersionsSplit(${split ? "false" : "true"})">${split ? "Use one choice for both" : "Choose separately for host kernels"}</button></div>`;
  if (window.applyRole) applyRole();
}
window.validatedVersionsPaint = validatedVersionsPaint;

window.validatedVersionsSplit = on => {
  STATE.validatedSplit = on;
  const s = STATE.data.validated?.settings;
  if (!on && s && s.platform !== s.host) return validatedVersionsSave();
  validatedVersionsPaint();
};

window.validatedVersionsSave = async () => {
  const picked = kind => document.querySelector(`input[name="vv_${kind}"]:checked`)?.value;
  const platform = picked("platform");
  const host = STATE.validatedSplit ? picked("host") || platform : platform;
  try {
    await api("/api/updates/validated/settings", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ platform, host }) });
    toast("Saved", "ok");
    clusterComponentsPaint(true);
  } catch (e) { toast(e.message, "bad"); }
  validatedVersionsPaint();
};

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
    ${UI.moduleHeader(`Platform versions`, `The software the apps run on, and whether newer versions are available. ${r.harvester
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
  cluster: (c, to) => `Each host restarts on ${to} in turn, servers first. Its apps wait or move in the meantime.
    On a single-host cluster, everything, including Homestead, is unavailable for a minute or two.`,
  longhorn: (c, to) => `Upgrade Longhorn to ${to} after checking its V2 volumes and host requirements.`,
  kubevirt: (c, to) => `KubeVirt's operator rolls out ${to}. Running VMs keep running, and use the new version when they restart or move.`,
  cdi: (c, to) => `CDI's operator rolls out ${to}. Disk imports in progress may restart.`,
  "kube-vip": (c, to) => `kube-vip is upgraded to ${to}, keeping its values. Each virtual IP drops for a few seconds as its host's agent restarts.`,
  multus: (c, to) => `Multus is upgraded to ${to}, keeping its values. Running apps are not affected. New ones may start a few seconds late.`,
};

window.componentUpgrade = async id => {
  const c = (STATE.data.components?.components || []).find(row => row.id === id);
  if (!c?.next) return;
  if (id === 'longhorn') return lhV2Upgrade(c.next);
  modal(`Upgrade to ${c.next} · ${c.name}`, `
    <p>${esc(c.name)} ${esc(c.installed)} → <b class="mono">${esc(c.next)}</b>${c.steps_left ? ` <span class="dim">(then ${esc(c.newest)}, as another step)</span>` : ""}</p>
    <div class="note">${esc(COMPONENT_EFFECT[id](c, c.next).replace(/\s+/g, " "))}</div>
    ${componentCheckSection(c.check)}
    ${c.notes_url ? `<p class="small"><a href="${safeHref(c.notes_url)}" target="_blank" rel="noopener noreferrer">Read ${esc(c.next)}'s release notes ${icon("ext")}</a> first.</p>` : ""}
    ${UI.actions(`<button data-dialog-dismiss="true" class="btn" onclick="closeModal()">Cancel</button>
      <button class="btn pri" onclick="componentUpgradeGo(${jsq(id)}, ${jsq(c.next)})">Upgrade to ${esc(c.next)}</button>`)}`);
};

window.componentUpgradeGo = async (id, to) => {
  const ack = $("#cmp_check_ack");
  if (ack && !ack.checked) return toast("First tick the box to confirm that the problems were read", "bad");
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
  modal(`Manage upgrades · ${c.name}`, `
    <p>Allow Homestead to upgrade this ${esc(c.name)} installation with the project's release manifests.</p>
    <div id="cmp_manage_status" role="status" aria-live="polite"><div class="note">This allows future upgrades. It does not change the installed version, VM disks or settings.</div></div>
    ${UI.more('Before turning this on', '<p>Use this for an installation made by the Homestead installer or from plain manifests. If Helm, GitOps or another operator manages this component, keep using it. Homestead checks for those and for active installer files before allowing upgrades.</p>')}
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
      status.innerHTML = UI.callout('bad', 'Upgrades could not be turned on', esc(error.message));
    } else toast(error.message, 'bad');
  } finally {
    componentManagePending.delete(id);
    if ($('#cmp_manage_go') === button) {
      ack.disabled = false; button.disabled = !ack.checked; button.textContent = 'Enable upgrades';
    }
  }
};

window.harvesterUpgradeStart = version => {
  modal(`Upgrade to ${version} · Harvester`, `
    <p>Harvester checks the cluster first, then downloads the release, prepares each host, and upgrades its own services and then each host in turn.
      VMs are moved off a host before it restarts. This takes an hour or more, and cannot be undone from Homestead.</p>
    <div class="note">Before starting, check that every host is ready, no volume is degraded, and important data is backed up. VMs that cannot move while running
      (with a passed-through device, or on a single host) are shut down while their host restarts.</div>
    <div class="f"><label>Type <b class="mono">${esc(version)}</b> to start</label><input id="hv_up_confirm" class="mono" autocomplete="off"></div>
    ${UI.actions(`<button data-dialog-dismiss="true" class="btn" onclick="closeModal()">Cancel</button>
      <button class="btn pri" onclick="harvesterUpgradeGo(${jsq(version)})">Start upgrade</button>`)}`);
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
        ${row.offered ? UI.chip("offered by Harvester", "info", { size: "slim", tip: "Harvester lists this version, so its dashboard shows an Upgrade button for it." }) : ""}</div>
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
    ${up.message ? `${UI.callout(up.state === "failed" ? "bad" : "", "", `${esc(up.message)}`)}` : ""}
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
  // What Harvester could not be asked is unknown: never "up to date" then.
  const unknown = [r.history === null && "whether an upgrade is under way", r.offered === null && "what Harvester offers"].filter(Boolean);
  const status = !r.current ? "Harvester did not report its version"
    : behind ? `${r.stable.tag} is out; this cluster runs v${r.current}` : `v${r.current} is the newest stable release`;
  return `<section class="card flat cluster-wide upgrades-card" style="margin-top:14px">
    ${UI.moduleHeader(`Harvester releases`, `${esc(status)}. An upgrade that Harvester offers can be started here or from its dashboard, and its progress is shown here either way.`, `${clusterPill(r.active || behind ? "attention" : unknown.length ? "unknown" : "healthy", r.active ? "upgrading" : behind ? "update available" : unknown.length ? "not read" : "up to date")}
        <button class="btn sm" onclick="clusterUpgradesPaint(true)">${icon("refresh")}Check now</button>`)}
    ${r.last ? upgradeProgress(r.last) : ""}
    ${r.unread ? UI.callout("warn", "Upgrades could not be read", `${esc(r.unread)}, so ${esc(unknown.join(" and ") || "this")} is not known. Check now tries again.`) : ""}
    <div class="release-list">
      ${releaseRow("NEWEST STABLE", r.stable, "stable")}
      ${releaseRow("NEWEST TEST BUILD", r.test, "test")}
      ${!r.stable && !r.test ? `<div class="dim small">${r.error ? `GitHub could not be reached for Harvester's releases. ${esc(r.error)}` : "Nothing newer than this cluster's version has been published."}</div>` : ""}
    </div>
    ${(r.offered || []).length ? `<div class="row" style="margin-top:8px;flex-wrap:wrap;gap:6px"><span class="dim xs">Harvester offers:</span>
      ${r.offered.filter(o => !r.current || upgradeNewer(o.version, r.current)).map(o => `${UI.chip(o.version, "neutral")}${r.active ? "" :
        `<button class="btn sm pri" data-need="admin" onclick="harvesterUpgradeStart(${jsq(o.version)})">Upgrade to ${esc(o.version)}</button>`}`).join("")
        || r.offered.map(o => UI.chip(o.version, "neutral")).join("")}</div>` : ""}
    ${r.stable && r.stable.offered === false ? `<div class="note" style="margin-top:10px">Harvester shows an Upgrade button once it lists a version. Its upgrade checker lists supported upgrade paths, sometimes a few days after release. A test build is never offered. Installing one means creating its Version manually, as Harvester's upgrade guide describes.</div>` : ""}
  </section>`;
}


/* ---------------- before an upgrade: what Homestead's table knows ----------------
   Each part Homestead upgrades carries a check (homestead_upgrade_check):
   what its next version is known to break here, from a dated table with a
   source per row. Unknown is "Not checked", never a pass (#620). The pairs
   are what runs now: KubeVirt against the hosts' Kubernetes. */
const CHECK_VERDICT = { blocked: ["Check first", "bad"], check: ["Worth checking", "warn"],
  clear: ["No known breaks", "ok"], unchanged: ["No new problems", "ok"], unchecked: ["Not checked", "neutral"] };
const CHECK_LEVEL = { block: ["Blocks", "bad"], warn: ["Read first", "warn"], already: ["Already true", "neutral"], info: ["Checked", "ok"], unknown: ["Not checked", "neutral"] };
const PAIR_STATUS = { supported: ["Supported", "ok"], eol: ["Kubernetes past end of life", "warn"],
  outside: ["Outside its support matrix", "warn"], unchecked: ["Not checked", "neutral"] };

const checkSource = url => url ? `<a class="btn sm" href="${safeHref(url)}" target="_blank" rel="noopener noreferrer">${icon("ext")}Source</a>` : "";

function componentCheckList(r) {
  const rows = (r.findings || []).map(f => { const [label, tone] = CHECK_LEVEL[f.level] || CHECK_LEVEL.unknown;
    return { tone, label, title: f.title, detail: f.detail, actionsHtml: checkSource(f.source) }; });
  return `${rows.length ? UI.insightList(rows) : `<p class="dim small">Homestead's table says nothing about ${esc(r.to)}.</p>`}
    <p class="dim xs">From Homestead's table of known version pairs, checked ${esc(r.checked)}. It covers only a few facts.
      "No known breaks" is not a guarantee, so read the release notes too.</p>`;
}

/* The verdict and its top finding, in one line. */
function componentVerdict(r) {
  const [label, tone] = CHECK_VERDICT[r.verdict] || CHECK_VERDICT.unchecked;
  const top = ["block", "warn", "unknown"].map(level => (r.findings || []).find(f => f.level === level)).find(Boolean);
  const said = top ? top.title : r.verdict === "clear" ? `Nothing in Homestead's table breaks ${r.to} here`
    : r.verdict === "unchanged" ? `Nothing in Homestead's table gets worse with ${String(r.to).split("+")[0]}.` : `Homestead's table says nothing about ${r.to}`;
  return `<div class="component-check">${UI.chip(label, tone, { size: "slim" })}<span class="dim xs">${esc(said)}</span></div>`;
}

/* On the row, the verdict only when it asks for attention; the details are
   one click away, and the upgrade review always shows the verdict. */
function componentCheckLine(c) {
  const r = c.check;
  if (!r) return "";
  return `${["blocked", "check"].includes(r.verdict) ? componentVerdict(r) : ""}
    ${(r.findings || []).length ? UI.more(`What ${r.to} changes`, componentCheckList(r), false, `component-check-${c.id}`) : ""}`;
}

/* The upgrade review's part: the verdict, every finding, and a tick when one blocks. */
function componentCheckSection(r) {
  if (!r) return "";
  return UI.section("Before upgrading", componentVerdict(r) + componentCheckList(r))
    + (r.verdict === "blocked" ? UI.ack("cmp_check_ack", "The problems with this upgrade were read. Continue anyway.") : "");
}

function componentPairsLine(pairs) {
  if (!pairs?.length) return "";
  return `<div class="component-pairs"><span class="cluster-kicker">VERSION PAIRS</span>${pairs.map(p => {
    const [label, tone] = PAIR_STATUS[p.status] || PAIR_STATUS.unchecked;
    return `<div class="component-check">${UI.chip(label, tone, { size: "slim" })}<span class="xs"><b>${esc(p.title)}</b>
      <span class="dim">${esc(p.detail)}</span></span>${checkSource(p.source)}</div>`; }).join("")}</div>`;
}
