/* Full settings workspace: cluster policy, hardware, access, and installation info. */

const ROLE_COPY = {
  viewer: "Read-only access to dashboards, workloads, storage, events, and settings.",
  operator: "Viewer access plus deploy, edit, move, start, and stop workloads and VMs.",
  admin: "Full access, including users, hosts, hardware mappings, imports, and cluster policy.",
};

function thresholdEditor(id, label, unit, help, pair) {
  const temperature = id.includes("temperature");
  return `<div class="threshold-card">
    <div><b>${esc(label)}</b><div class="dim xs">${esc(help)}</div></div>
    <div class="threshold-values">
      <label>Warning <span class="warn-dot"></span><input id="set_${id}_warn" type="number" min="1" max="${temperature ? 119 : 99}" value="${pair.warning}" ${can("admin") ? "" : "disabled"}><span>${unit}</span></label>
      <label>Critical <span class="crit-dot"></span><input id="set_${id}_crit" type="number" min="2" max="${temperature ? 120 : 100}" value="${pair.critical}" ${can("admin") ? "" : "disabled"}><span>${unit}</span></label>
    </div></div>`;
}

/* One topic at a time: the page was every setting in one long column. */
const SETTINGS_TABS = [["health", "Health"], ["updates", "Updates"], ["cluster", "Cluster"], ["hardware", "Hardware"], ["access", "Access"],
  ["apps", "Apps"], ["mqtt", "MQTT"], ["device", "This device"], ["about", "About"]];

function settingsTab(pick) {
  if (pick) {
    try { localStorage.setItem("homestead.settings.tab", pick); } catch (e) { /* this visit only */ }
    const grid = $(".settings-grid");
    if (grid) grid.dataset.tab = pick;
    $$(".settings-tabs button").forEach(b => { b.classList.toggle("on", b.dataset.tab === pick); b.setAttribute("aria-selected", b.dataset.tab === pick); });
    return pick;
  }
  let saved = "";
  try { saved = localStorage.getItem("homestead.settings.tab") || ""; } catch (e) { /* default */ }
  return SETTINGS_TABS.some(([id]) => id === saved) ? saved : "health";
}
window.settingsTab = settingsTab;

async function viewSettings() {
  const [settings, features, overview, users] = await Promise.all([
    loadHealthSettings(true), loadHardwareFeatures(),
    api("/api/overview").catch(() => STATE.data.ov || null),
    can("admin") ? api("/api/auth/users").catch(() => []) : Promise.resolve([]),
  ]);
  STATE.data.ov = overview || STATE.data.ov;
  const thresholds = settings.thresholds || HEALTH_DEFAULTS.thresholds;
  const updates = settings.updates || { policy: "approval_required", notify_available: true,
    notify_failures: true, maintenance: { days: [0, 1, 2, 3, 4, 5, 6], start: "02:00", duration_minutes: 120 } };
  const maintenance = updates.maintenance || {};
  const smart = settings.smart || { temperature: { warning: 55, critical: 65 },
    reallocated_warning: 1, pending_critical: 1, uncorrectable_critical: 1, notify_failures: true };
  const info = settings.info || {};
  const nodes = overview?.nodes || STATE.data.nodes || [];
  const hardwareRows = features.map(f => {
    const hosts = nodes.filter(n => n.hardware?.[f.id]).map(n => n.name.replace("harvester-", ""));
    return `<div class="settings-list-row"><div><b>${esc(f.name)}</b><div class="dim xs mono">${esc(f.host_path)} → ${esc(f.container_path || f.host_path)}</div></div>
      <div class="settings-list-meta">${hosts.length ? `<span class="tag hw">${hosts.length} host${hosts.length === 1 ? "" : "s"}</span><span class="dim xs">${esc(hosts.join(", "))}</span>` : '<span class="pill med">no host detected</span>'}</div></div>`;
  }).join("");
  const userRows = users.map(u => `<tr><td><b>${esc(u.name)}</b>${u.name === ME ? ' <span class="tag ok">you</span>' : ""}</td>
    <td><span class="${roleClass(u.role)} rolechip">${esc(u.role)}</span></td><td class="dim xs mono">${esc(u.last_login || "never")}</td></tr>`).join("");

  const tab = settingsTab();
  paint(`<div class="phead"><div><h2>Settings</h2><p>Cluster policy, hardware, access, and installation information</p></div>
    <button class="btn" onclick="document.getElementById('drawer').classList.add('open')">Appearance</button></div>
    <div class="seg settings-tabs" role="tablist">${SETTINGS_TABS.map(([id, label]) =>
      `<button role="tab" data-tab="${id}" class="${id === tab ? "on" : ""}" aria-selected="${id === tab}" onclick="settingsTab('${id}')">${label}</button>`).join("")}</div>

    <div class="settings-grid" data-tab="${tab}">
      <section class="card flat settings-wide" data-tab="health">
        <div class="settings-card-head"><div><div class="ctitle">Health thresholds</div><div class="csub">Controls when utilisation bars and node cards turn yellow or red for everyone</div></div>
          ${can("admin") ? '<button class="btn pri" onclick="saveHealthSettings()">Save thresholds</button>' : '<span class="pill neutral">admin managed</span>'}</div>
        <div class="threshold-grid">
          ${thresholdEditor("cpu", "CPU utilisation", "%", "Sustained node CPU pressure", thresholds.cpu)}
          ${thresholdEditor("memory", "Memory utilisation", "%", "Allocated node RAM pressure", thresholds.memory)}
          ${thresholdEditor("disk", "Node disk utilisation", "%", "Local filesystem capacity", thresholds.disk)}
          ${thresholdEditor("temperature", "CPU temperature", "°C", "Host thermal warning", thresholds.temperature)}
        </div>
        <div class="note"><b>Warning</b> changes the metric and node card to yellow. <b>Critical</b> changes them to red. A node uses the most severe result across CPU, memory, disk, and temperature.</div>
      </section>

      <section class="card flat settings-wide" data-tab="health">
        <div class="settings-card-head"><div><div class="ctitle">Drive health policy</div>
          <div class="csub">SMART warnings shown on node cards and in cluster health</div></div>
          ${can("admin") ? '<button class="btn pri" onclick="saveHealthSettings()">Save drive policy</button>' : '<span class="pill neutral">admin managed</span>'}</div>
        <div class="threshold-grid">
          ${thresholdEditor("drive_temperature", "Drive temperature", "°C", "SATA, SAS, and NVMe temperature", smart.temperature)}
          <div class="threshold-card"><div><b>Media counters</b><div class="dim xs">Alert when raw drive counters reach these values</div></div>
            <div class="smart-threshold-values">
              <label>Reallocated warning <input id="set_smart_reallocated" type="number" min="1" max="1000000" value="${smart.reallocated_warning}" ${can("admin") ? "" : "disabled"}></label>
              <label>Pending critical <input id="set_smart_pending" type="number" min="1" max="1000000" value="${smart.pending_critical}" ${can("admin") ? "" : "disabled"}></label>
              <label>Uncorrectable critical <input id="set_smart_uncorrectable" type="number" min="1" max="1000000" value="${smart.uncorrectable_critical}" ${can("admin") ? "" : "disabled"}></label>
            </div></div>
        </div>
        <label class="switch" style="margin-top:14px"><input id="set_smart_notify" type="checkbox" ${smart.notify_failures !== false ? "checked" : ""} ${can("admin") ? "" : "disabled"}> Include SMART failures and threshold breaches in cluster health notifications</label>
        <div class="note"><b>Device differences are preserved.</b> NVMe reports media errors; ATA disks report reallocated, pending, and uncorrectable sectors. Missing counters are shown as unsupported, not zero.</div>
      </section>

      <section class="card flat settings-wide" data-tab="updates">
        <div class="settings-card-head"><div><div class="ctitle">Container image update policy</div>
          <div class="csub">Controls registry notifications and when a reviewed rollout may start; major releases are never selected automatically</div></div>
          ${can("admin") ? '<button class="btn pri" onclick="saveUpdateSettings()">Save update policy</button>' : '<span class="pill neutral">admin managed</span>'}</div>
        <div class="update-policy-grid">
          <div class="f"><label>Policy ${tip("Notify only blocks installs. Approval required permits a reviewed manual rollout. Maintenance window permits reviewed rollouts only during the configured UTC window.")}</label>
            <select id="set_update_policy" ${can("admin") ? "" : "disabled"} onchange="updatePolicyFields()">
              <option value="notify_only" ${updates.policy === "notify_only" ? "selected" : ""}>Notify only — block installs</option>
              <option value="approval_required" ${updates.policy === "approval_required" ? "selected" : ""}>Approval required — manual rollout</option>
              <option value="maintenance_window" ${updates.policy === "maintenance_window" ? "selected" : ""}>Maintenance window — manual rollout in window</option>
            </select></div>
          <div class="update-notify-options">
            <label class="switch"><input id="set_notify_available" type="checkbox" ${updates.notify_available !== false ? "checked" : ""} ${can("admin") ? "" : "disabled"}> Notify when new images are available</label>
            <label class="switch"><input id="set_notify_failures" type="checkbox" ${updates.notify_failures !== false ? "checked" : ""} ${can("admin") ? "" : "disabled"}> Notify when a registry check fails</label>
          </div>
          <div id="maintenanceFields" class="maintenance-fields ${updates.policy === "maintenance_window" ? "" : "muted-policy"}">
            <div class="f"><label>Start time (UTC)</label><input id="set_update_start" type="time" value="${esc(maintenance.start || "02:00")}" ${can("admin") ? "" : "disabled"}></div>
            <div class="f"><label>Duration (minutes)</label><input id="set_update_duration" type="number" min="15" max="1440" step="15" value="${+(maintenance.duration_minutes || 120)}" ${can("admin") ? "" : "disabled"}></div>
            <div class="f update-days"><label>Days (UTC)</label><div>${["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"].map((day, index) =>
              `<label class="daypick"><input type="checkbox" class="set_update_day" value="${index}" ${(maintenance.days || []).includes(index) ? "checked" : ""} ${can("admin") ? "" : "disabled"}><span>${day}</span></label>`).join("")}</div></div>
          </div>
        </div>
        <div class="note"><b>No silent upgrades.</b> Every install still shows the exact current and candidate image and requires an operator acknowledgement. Semantic-version discovery stays within the current major release.</div>
      </section>

      <section class="card flat" data-tab="hardware">
        <div class="settings-card-head"><div><div class="ctitle">Hardware features</div><div class="csub">Reusable passthrough paths and automatic host detection</div></div>
          <div class="row">${can("operator") ? '<button class="btn sm" onclick="hardwareRescan(this)" title="Look for devices plugged in since the last check">Rescan hosts</button>' : ""}
          ${can("admin") ? '<button class="btn sm" onclick="hardwareFeatureSettings()">Manage</button>' : ""}</div></div>
        <div class="dim xs" style="margin:-4px 0 8px">Hosts are checked every 30 seconds, so a device plugged in later is found without a restart.</div>
        <div class="settings-list">${hardwareRows || '<div class="empty small">No hardware features configured.</div>'}</div>
      </section>

      <section class="card flat" data-tab="access">
        <div class="settings-card-head"><div><div class="ctitle">Account & access</div><div class="csub">Signed in as ${esc(ME || "—")}</div></div><span class="${roleClass(ROLE)} rolechip">${esc(ROLE || "—")}</span></div>
        <div class="role-summary"><b>${esc(ROLE || "viewer")}</b><span>${esc(ROLE_COPY[ROLE] || ROLE_COPY.viewer)}</span></div>
        <div class="row settings-actions"><button class="btn sm" onclick="pwChange()">Change password</button>
          ${can("admin") ? '<button class="btn sm" onclick="manageUsers()">Manage users</button>' : ""}
          <button class="btn sm" onclick="signOutEverywhere()">Sign out everywhere</button>
          <button class="btn sm danger" onclick="doLogout()">Sign out</button></div>
        <div class="dim xs">${sessionSummary(AUTH_STATE)}</div>
        <div class="role-legend">
          ${Object.entries(ROLE_COPY).map(([role, copy]) => `<div><span class="${roleClass(role)} rolechip">${role}</span><span class="dim xs">${esc(copy)}</span></div>`).join("")}
        </div>
        ${users.length ? `<div class="sec">Users</div><div class="tblwrap"><table class="tbl stack dense"><thead><tr><th>User</th><th>Role</th><th>Last sign-in</th></tr></thead><tbody>${userRows}</tbody></table></div>` : ""}
      </section>

      ${pwaCard()}

      ${portalSettingsCard()}

      ${ipamUnifiCard()}

      <section class="card flat settings-wide" data-tab="mqtt" id="mqttCard"></section>
      <section class="card flat settings-wide" data-tab="cluster" id="addonsCard" hidden></section>
      <section class="card flat settings-wide" data-tab="cluster" id="lhSettingsCard"><div class="empty small"><span class="spin2"></span>reading Longhorn</div></section>

      <section class="card flat settings-wide" data-tab="apps">
        <div class="settings-card-head"><div><div class="ctitle">App Store catalogue</div>
          <div class="csub">Any feed in the Community Applications format: the public one, a mirror, or your own list of templates</div></div>
          ${can("admin") ? '<div class="row"><button class="btn sm" onclick="saveCatalog(true)">Use Community Applications</button><button class="btn sm pri" onclick="saveCatalog()">Save</button></div>' : '<span class="pill neutral">admin managed</span>'}</div>
        <div class="f"><label>Catalogue feed URL ${tip("A JSON feed shaped like Community Applications' applicationFeed.json - an object with an applist, or a plain list of templates. Blank uses the public Community Applications feed. It is fetched on demand and cached for six hours.")}</label>
          <input id="set_catalog" type="url" maxlength="500" placeholder="blank: https://raw.githubusercontent.com/Squidly271/AppFeed/master/applicationFeed.json"
            value="${esc(STATE.data.appSettings?.catalog_url || "")}" ${can("admin") ? "" : "disabled"}></div>
      </section>

      <section class="card flat settings-wide" id="nsCard" data-tab="apps">
        <div class="settings-card-head"><div><div class="ctitle">Namespaces</div>
          <div class="csub">Where apps live. Harvester, Rancher and Kubernetes keep their own, which are hidden here and in every picker.</div></div>
          ${can("admin") ? `<div class="row ns-new"><input id="nsName" placeholder="new-namespace" maxlength="63" autocomplete="off"
            onkeydown="if(event.key==='Enter')namespaceCreate()"><button class="btn sm pri" onclick="namespaceCreate()">Create</button></div>` : ""}</div>
        <div class="ns-body"><div class="empty small"><span class="spin2"></span></div></div>
      </section>

      <section class="card flat settings-wide" data-tab="about">
        <div class="ctitle">About this installation</div><div class="csub">Runtime and cluster connection details</div>
        <div class="f sitename"><label>Site name ${tip("Shown under the Homestead wordmark and at the foot of the page. Name the cluster or the house it lives in; leave it blank to show nothing.")}</label>
          <div class="row"><input type="text" id="set_site_name" maxlength="40" placeholder="e.g. Loft rack, or nothing at all"
            value="${esc(STATE.data.appSettings?.site_name || "")}" ${can("admin") ? "" : "disabled"}>
            ${can("admin") ? '<button class="btn sm" onclick="saveSiteName()">Save</button>' : ""}</div></div>
        <div class="about-grid">
          <div><span>Homestead</span><b>v${esc(info.version || HOMESTEAD_VERSION)}</b></div>
          <div><span>Kubernetes</span><b>${esc(info.kubernetes || "—")}</b></div>
          <div><span>Default namespace</span><b class="mono">${esc(info.namespace || "lab")}</b></div>
          <div><span>Storage class</span><b class="mono">${esc(info.storage_class || "—")}</b></div>
          <div><span>Cluster VIP</span><b class="mono">${esc(overview?.lb_ip || info.vip || "—")}</b></div>
          <div><span>Nodes ready</span><b>${overview ? `${overview.nodes_ready}/${overview.nodes_total}` : "—"}</b></div>
          <div><span>Node probe ${tip("Homestead keeps the probe's scripts in step with its own release, so an upgrade needs no kubectl. Installation and removal live under Cluster → Add-ons because the SMART sidecar is privileged.")}</span>
            <b>${esc(probeWord(info.node_probe?.state))}</b><small>${esc(info.node_probe?.detail || "")}</small></div>
          ${permissionsCell(info.permissions)}
        </div>
      </section>

      <section class="card flat settings-wide" data-tab="about" id="selfHealthCard"><div class="empty small"><span class="spin2"></span> checking Homestead</div></section>
      <section class="card flat settings-wide" data-tab="about" id="replicaCard">${STATE.data.replicaHtml || ""}</section>
    </div>`);
  pwaPaint();
  namespacesPaint();
  replicasPaint();
  mqttPaint();
  lhSettingsPaint();
  if (window.addonsPaint) addonsPaint();
  selfHealthPaint();
  // The UniFi card needs the IPAM record, which Settings does not otherwise load.
  api("/api/ipam").then(data => { STATE.data.ipam = data; const host = $("#unifiCard"); if (host) host.outerHTML = ipamUnifiCard(); }).catch(() => {});
}

/* ---------------- Homestead's own redundancy ----------------
   More than one copy: a node failure leaves another already serving, and
   the leader lease (alerts, moves) moves within seconds. */
async function replicasPaint() {
  let r;
  try { r = await api("/api/self/replicas"); } catch (e) { return; }
  const ready = r.pods.filter(p => p.ready && !p.terminating).length;
  const note = r.desired > 1 && r.spread_nodes < Math.min(r.desired, ready)
    ? `<div class="note">${ready} copies are ready but only on ${r.spread_nodes} node${r.spread_nodes === 1 ? "" : "s"}: spreading is a preference, so the scheduler doubled up where it had to. They move apart as nodes free up.</div>`
    : r.desired === 1 ? `<div class="note">One copy: if its node fails, Homestead is away until Kubernetes starts it elsewhere - about a minute. Two copies on different nodes keep it answering.</div>` : "";
  STATE.data.replicaHtml = `<div class="settings-card-head between"><div><div class="ctitle">Redundancy</div>
      <div class="csub">How many copies of Homestead run. They share its data volume; one, the leader, raises alerts and advances moves, and another takes over within seconds if it stops.</div></div>
      ${can("admin") ? `<div class="row"><select id="rep_count">${Array.from({ length: r.max }, (_, i) => i + 1).map(n =>
        `<option value="${n}" ${n === r.desired ? "selected" : ""} ${n > 1 && !r.data?.shareable && n !== r.desired ? "disabled" : ""}>${n} cop${n === 1 ? "y" : "ies"}</option>`).join("")}</select>
        <button class="btn sm pri" onclick="replicasSave()">Apply</button></div>` : ""}</div>
    ${r.data && !r.data.shareable ? `<div class="note ${r.desired > 1 ? "bad" : ""}" style="margin-top:10px"><b>More than one copy needs a volume every node can mount.</b> ${esc(r.data.reason)}.
      ${r.data.candidates.length ? "" : "<div>This cluster has no storage class that shares a volume between nodes; create one under Volumes, without live migration.</div>"}</div>` : ""}
    ${r.data?.pvc ? `<div class="row" style="margin-top:10px;flex-wrap:wrap;gap:8px"><span class="small">Its data: <span class="mono">${esc(r.data.pvc)}</span> on <b>${esc(r.data.storage_class || "?")}</b></span>
      ${(r.data.classes || []).length && can("admin") ? `<span class="small">· move to</span>
        <select id="rep_class">${r.data.classes.map(c => `<option value="${esc(c.name)}" ${c.name === "longhorn" && !r.data.shareable ? "selected" : ""}>${esc(c.name)}${c.shareable ? " · every node" : " · one node"}</option>`).join("")}</select>
        <button class="btn sm" onclick="replicasMoveData()">Move data</button>` : ""}</div>
      ${(r.data.kept || []).length ? `<div class="note" style="margin-top:8px">Kept from moving its data:
        ${r.data.kept.map(k => `<span class="mono">${esc(k)}</span>`).join(", ")} - Homestead no longer uses
        ${r.data.kept.length === 1 ? "it" : "them"}. Delete ${r.data.kept.length === 1 ? "it" : "them"} from Volumes once Homestead is working on the new one.
        <div class="row" style="margin-top:6px">${r.data.kept.map(k => `<button class="btn sm" onclick="openOperation('/volumes?find=${esc(k)}')">Show ${esc(k)}</button>`).join("")}</div></div>` : ""}
      <div class="dim xs" style="margin-top:4px">Homestead keeps running while its data is copied, then restarts once onto the new volume;
        the old one is kept until you delete it. Wait for running jobs to finish first - the copy is taken as it stands.</div>` : ""}
    <table class="tbl dense stack" style="margin-top:10px"><thead><tr><th>Copy</th><th>Node</th><th>State</th></tr></thead><tbody>
      ${r.pods.map(p => `<tr><td class="mono small">${esc(p.name)}${p.this ? ' <span class="tag">this page</span>' : ""}</td>
        <td data-label="Node">${esc(p.node || "unscheduled")}</td>
        <td data-label="State">${p.terminating ? '<span class="pill slim neutral">stopping</span>' : p.ready ? '<span class="pill slim ok">ready</span>' : '<span class="pill slim med">starting</span>'}
          ${p.leader ? '<span class="pill slim info" data-tip="Raises alerts and advances moves">leader</span>' : ""}</td></tr>`).join("")}</tbody></table>
    ${note}`;
  const host = $("#replicaCard");
  if (host) host.innerHTML = STATE.data.replicaHtml;
}
window.replicasMoveData = async () => {
  const storage_class = $("#rep_class").value;
  if (!confirm(`Copy Homestead's data to a new volume on ${storage_class} and restart onto it?`)) return;
  try {
    const r = await api("/api/self/data/move", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ storage_class }) });
    toast(r.detail, "ok");
  } catch (e) { toast(e.message, "bad"); }
};
window.replicasSave = async () => {
  const replicas = +$("#rep_count").value;
  try {
    const result = await api("/api/self/replicas", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ replicas }) });
    toast(result.detail, "ok");
    setTimeout(replicasPaint, 3000);
  } catch (e) { toast(e.message, "bad"); }
};

/* ------------------------------------------------ namespaces */
async function namespacesPaint() {
  const host = $("#nsCard .ns-body");
  if (!host) return;
  let inv;
  try { inv = await api("/api/namespaces/manage", { keep: true }); }
  catch (e) { host.innerHTML = `<div class="empty small">${esc(e.message)}</div>`; return; }
  const what = r => [r.deployments && `${r.deployments} app${r.deployments === 1 ? "" : "s"}`,
    r.statefulsets && `${r.statefulsets} stateful set${r.statefulsets === 1 ? "" : "s"}`,
    r.volumes && `${r.volumes} volume${r.volumes === 1 ? "" : "s"}`,
    r.vms && `${r.vms} VM${r.vms === 1 ? "" : "s"}`].filter(Boolean).join(" · ") || "empty";
  host.innerHTML = `<div class="tblwrap"><table class="tbl stack dense" data-sort="namespaces"><thead><tr>
      <th>Namespace</th><th>Holds</th><th data-nosort>Created</th><th></th></tr></thead><tbody>
    ${inv.namespaces.map(r => `<tr><td data-sort="${esc(r.name)}"><b class="mono">${esc(r.name)}</b>
        ${r.name === inv.default ? '<span class="tag ok">default for new apps</span>' : ""}${r.homestead ? '<span class="tag">made here</span>' : ""}</td>
      <td class="small">${esc(what(r))}</td>
      <td class="dim xs">${r.created ? esc(new Date(r.created).toLocaleDateString()) : ""}</td>
      <td class="right">${can("admin") ? (r.protected ? `<span class="dim xs" title="${esc(r.protected)}">kept</span>`
        : `<button class="btn sm danger" ${r.empty ? "" : `disabled title="Move or delete what it holds first"`}
            onclick="namespaceDelete('${esc(r.name)}')">Delete</button>`) : ""}</td></tr>`).join("")}</tbody></table></div>
    <div class="dim xs" style="margin-top:8px">${inv.system_hidden} platform namespace${inv.system_hidden === 1 ? "" : "s"} hidden.</div>`;
  sortTables(host);
}

window.namespaceCreate = async () => {
  const name = $("#nsName").value.trim().toLowerCase();
  if (!name) return;
  try {
    await api("/api/namespaces/create", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ name }) });
    toast(`${name} created`, "ok");
    $("#nsName").value = "";
    namespacesPaint();
  } catch (e) { toast(e.message, "bad"); }
};

window.namespaceDelete = async name => {
  const typed = prompt(`Delete the namespace ${name}? It is empty, and this cannot be undone.

Type its name to confirm:`);
  if (typed === null) return;
  try {
    await api("/api/namespaces/delete", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name, confirm: typed.trim() }) });
    toast(`${name} deleted`, "ok");
    namespacesPaint();
  } catch (e) { toast(e.message, "bad"); }
};

/* ------------------------------------------------ Homestead's own objects */
const PERMISSION_WORDS = { current: "up to date", updated: "updated with this release",
  manual: "needs a one-time command", unknown: "could not be checked", pending: "not checked yet" };

function commandBlock(command) {
  return `<div class="self-command"><pre class="onboard-args">${esc(command)}</pre>
    <button class="btn sm" onclick="navigator.clipboard.writeText(this.previousElementSibling.textContent).then(() => toast('Copied', 'ok'))">Copy</button></div>`;
}

function permissionsCell(state) {
  state = state || { state: "pending", detail: "" };
  return `<div><span>Permissions ${tip("Homestead keeps its own ClusterRole in step with its release, so an upgrade brings the permissions new features need. Granting it that is a one-time command.")}</span>
    <b>${esc(PERMISSION_WORDS[state.state] || state.state)}</b><small>${esc(state.detail || "")}</small>
    ${state.state === "manual" && can("admin") ? `<div class="dim xs" style="margin-top:6px">Run once, wherever you use kubectl:</div>${commandBlock(state.command)}` : ""}
    ${can("admin") && state.state === "manual" ? '<div class="row" style="margin-top:6px"><button class="btn sm" onclick="permissionsRecheck()">Check again</button></div>' : ""}</div>`;
}

window.permissionsRecheck = async () => {
  try {
    const result = await api("/api/self/permissions", { method: "POST", headers: { "Content-Type": "application/json" }, body: "{}" });
    toast(PERMISSION_WORDS[result.state] || result.state, result.state === "manual" ? "bad" : "ok");
    STATE.data.appSettings = null;
    await loadHealthSettings(true);
    viewSettings();
  } catch (e) { toast(e.message, "bad"); }
};

window.saveHealthSettings = async () => {
  const read = id => ({ warning: +$("#set_" + id + "_warn").value, critical: +$("#set_" + id + "_crit").value });
  const body = { thresholds: { cpu: read("cpu"), memory: read("memory"), disk: read("disk"), temperature: read("temperature") },
    smart: { temperature: read("drive_temperature"), reallocated_warning: +$("#set_smart_reallocated").value,
      pending_critical: +$("#set_smart_pending").value, uncorrectable_critical: +$("#set_smart_uncorrectable").value,
      notify_failures: $("#set_smart_notify").checked },
    site_name: STATE.data.appSettings?.site_name || "",
    updates: STATE.data.appSettings?.updates };
  try {
    const saved = await api("/api/settings", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
    HEALTH = { thresholds: { ...HEALTH_DEFAULTS.thresholds, ...(saved.thresholds || {}) } };
    STATE.data.appSettings = null;
    toast("health thresholds saved", "ok");
    viewSettings();
  } catch (e) { toast(e.message, "bad"); }
};

const probeWord = state => ({ updated: "updated with this release", current: "up to date",
  absent: "not installed", error: "could not be checked" })[state] || "unknown";

window.saveSiteName = async () => {
  const body = { ...(STATE.data.appSettings || {}), site_name: $("#set_site_name").value.trim() };
  delete body.info;
  try {
    const saved = await api("/api/settings", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
    STATE.data.appSettings = null;
    await loadHealthSettings(true);
    toast(saved.site_name ? `named ${saved.site_name}` : "site name cleared", "ok");
    viewSettings();
  } catch (e) { toast(e.message, "bad"); }
};

window.saveCatalog = async reset => {
  const url = reset ? "" : $("#set_catalog").value.trim();
  const body = { ...(STATE.data.appSettings || {}), catalog_url: url };
  delete body.info;
  try {
    await api("/api/settings", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
    STATE.data.appSettings = null;
    await loadHealthSettings(true);
    toast(url ? "App Store now reads that catalogue" : "App Store reads the Community Applications feed again", "ok");
    viewSettings();
  } catch (e) { toast(e.message, "bad"); }
};

window.updatePolicyFields = () => {
  const fields = $("#maintenanceFields");
  if (fields) fields.classList.toggle("muted-policy", $("#set_update_policy").value !== "maintenance_window");
};

window.saveUpdateSettings = async () => {
  const current = STATE.data.appSettings || {};
  const body = { thresholds: current.thresholds || HEALTH_DEFAULTS.thresholds,
    smart: current.smart, updates: {
    policy: $("#set_update_policy").value,
    notify_available: $("#set_notify_available").checked,
    notify_failures: $("#set_notify_failures").checked,
    maintenance: {
      start: $("#set_update_start").value,
      duration_minutes: +$("#set_update_duration").value,
      days: $$(".set_update_day:checked").map(input => +input.value),
    },
  } };
  try {
    const saved = await api("/api/settings", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
    STATE.data.appSettings = saved;
    toast("image update policy saved", "ok");
    await loadImageUpdates(false, true);
    viewSettings();
  } catch (e) { toast(e.message, "bad"); }
};

/* Look for hardware now rather than at the next 30-second check. */
window.hardwareRescan = async button => {
  if (button) { button.disabled = true; button.textContent = "Scanning…"; }
  try {
    const r = await api("/api/hardware/rescan", { method: "POST", headers: { "Content-Type": "application/json" }, body: "{}" });
    const names = Object.fromEntries((STATE.data.hardwareFeatures || []).map(f => [f.id, f.name]));
    const found = Object.entries(r.nodes || {}).filter(([, ids]) => ids.length)
      .map(([node, ids]) => `${node.replace("harvester-", "")}: ${ids.map(id => names[id] || id).join(", ")}`);
    toast(found.length ? found.join(" · ") : "no hardware features detected on any host", found.length ? "ok" : "warn");
    STATE.data.ov = await api("/api/overview").catch(() => STATE.data.ov);
    if (STATE.view === "settings") viewSettings();
  } catch (e) { toast(e.message, "bad"); }
  finally { if (button) { button.disabled = false; button.textContent = "Rescan hosts"; } }
};

/* ---------------- Homestead's own health ----------------
   Whether the parts that work in the background are working: the API it
   leans on, each loop, the node probe and Samba. Add-on controls live in Cluster. */
const HEALTH_TONE = { ok: "ok", standby: "neutral", starting: "neutral", late: "warn", failing: "bad" };
const agoText = t => !t ? "not yet" : Date.now() / 1000 - t < 60 ? "just now" : fmtAgo(Math.round(Date.now() / 1000 - t));

async function selfHealthPaint() {
  const host = $("#selfHealthCard");
  if (!host) return;
  let h;
  try { h = await api("/api/self/health"); }
  catch (e) { host.innerHTML = `<div class="ctitle">Homestead's health</div><div class="note bad">${esc(e.message)}</div>`; return; }
  const admin = can("admin"), probe = h.probe || {}, samba = h.samba || {}, pods = h.replicas?.pods || [];
  STATE.data.sambaInstalled = !!samba.installed;
  const row = (label, tone, word, detail = "", action = "") => `<div class="health-row"><span class="tag ${tone}">${esc(word)}</span>
    <div><b>${esc(label)}</b>${detail ? `<div class="dim xs">${detail}</div>` : ""}</div>${action ? `<div class="row">${action}</div>` : ""}</div>`;
  const problems = h.loops.filter(l => ["failing", "late"].includes(l.state)).length + (h.api.ok ? 0 : 1)
    + (probe.installed && probe.ready < probe.desired ? 1 : 0) + (samba.enabled && samba.ready < samba.desired ? 1 : 0);
  host.innerHTML = `<div class="settings-card-head"><div><div class="ctitle">Homestead's health</div>
      <div class="csub">What works in the background, checked every 15 seconds while this is open</div></div>
      <span class="pill ${problems ? "warn" : "ok"}">${problems ? `${problems} to look at` : "all well"}</span></div>
    <div class="health-list">
      ${row("Kubernetes API", h.api.ok ? (h.api.ms > 2000 ? "warn" : "ok") : "bad", h.api.ok ? `${h.api.ms} ms` : "no answer",
        h.api.ok ? (h.api.ms > 2000 ? "Slow to answer: pages and actions wait on it." : "Answering promptly.") : esc(h.api.error || ""))}
      ${row("Homestead", "ok", `v${h.version}`, `${pods.filter(p => p.ready).length} of ${h.replicas?.desired ?? "?"} cop${(h.replicas?.desired ?? 1) === 1 ? "y" : "ies"} ready`
        + (pods.length ? ` · ${pods.map(p => `${esc(p.node.replace("harvester-", ""))}${p.leader ? " (leader)" : ""}${p.this ? " - this one" : ""}`).join(", ")}` : "")
        + (h.leader ? "" : " · this copy is standing by"))}
      ${h.loops.map(l => row(l.label, HEALTH_TONE[l.state] || "", l.state === "ok" ? "running" : l.state,
        l.state === "standby" ? "Runs on the leader copy." : `Last done ${esc(agoText(l.last_ok))}` + (l.error ? ` · <span class="badtext">${esc(l.error)}</span>` : ""))).join("")}
      ${row("Node probe", !probe.installed ? "neutral" : probe.ready < probe.desired || probe.reporting < probe.desired ? "warn" : "ok",
        !probe.installed ? "not installed" : `${probe.ready}/${probe.desired} nodes`,
        !probe.installed ? "Temperatures, host devices, every disk and drive health come from it."
          : `${probe.reporting} reporting · drive health on ${probe.smart} · ${esc(probe.detail || "")}`,
        "")}
      ${row("Samba (network shares)", !samba.installed || !samba.enabled ? "neutral" : samba.ready < samba.desired ? "warn" : "ok",
        !samba.installed ? "not installed" : !samba.enabled ? "off" : samba.ready < samba.desired ? "starting" : "serving",
        (samba.installed ? `${samba.shares} share${samba.shares === 1 ? "" : "s"}${samba.address ? ` at <span class="mono">\\\\${esc(samba.address)}</span>` : ""} · ${esc(samba.image || "")}`
          : `Installed with the first share. ${samba.shares ? `${samba.shares} share${samba.shares === 1 ? " is" : "s are"} defined.` : ""}`),
        `<button class="btn sm" onclick="go('shares')">Network shares</button>`)}
      ${row("Permissions", h.permissions?.state === "error" ? "bad" : h.permissions?.state === "current" || h.permissions?.state === "updated" ? "ok" : "neutral",
        h.permissions?.state || "unknown", esc(h.permissions?.detail || ""))}
      ${h.addresses ? row("Addresses", h.addresses.error ? "neutral" : h.addresses.problem || (h.addresses.clashes || []).length ? "bad" : "ok",
        h.addresses.error ? "unknown" : h.addresses.problem || (h.addresses.clashes || []).length ? "on the cluster's address" : "separate",
        h.addresses.error ? esc(h.addresses.error)
          : h.addresses.problem ? `<span class="badtext">${esc(h.addresses.problem)}</span>`
          : (h.addresses.clashes || []).length ? `<span class="badtext">${esc(h.addresses.clashes.map(c => `${c.namespace}/${c.service}`).join(", "))}
              ${h.addresses.clashes.length === 1 ? "is" : "are"} on ${esc(h.addresses.clashes[0].ip)}, the cluster's own address (${esc(h.addresses.clashes[0].owner)}).
              Hosts join through it, so give ${h.addresses.clashes.length === 1 ? "it an address" : "them addresses"} of their own: Edit → Network.</span>`
          : `Apps are kept off the cluster's own address${(h.addresses.platform || []).length ? ` (<span class="mono">${esc(h.addresses.platform.join(", "))}</span>)` : ""}.`) : ""}
      ${row("Backup storage", h.backups?.ready ? "ok" : h.backups?.deployed ? "warn" : "neutral",
        h.backups?.ready ? "ready" : h.backups?.deployed ? "starting" : "none", h.backups?.endpoint ? `<span class="mono">${esc(h.backups.endpoint)}</span>` : "Set up under Data protection.")}
      ${h.mqtt?.state && h.mqtt.state !== "off" ? row("MQTT", h.mqtt.state === "publishing" ? "ok" : "warn", h.mqtt.state,
        h.mqtt.error ? `<span class="badtext">${esc(h.mqtt.error)}</span>` : esc(h.mqtt.detail || "")) : ""}
    </div>`;
  clearTimeout(window.__selfHealthTimer);
  window.__selfHealthTimer = setTimeout(() => { if (STATE.view === "settings" && settingsTab() === "about") selfHealthPaint(); }, 15000);
}
window.selfHealthPaint = selfHealthPaint;

window.sambaToggle = async box => {
  const on = box.checked;
  if (on && !STATE.data.sambaInstalled) {
    box.checked = false;
    if (nodeAddressesOnly()) return sambaInstallGo("");
    const choices = await vipChoices();
    const own = (choices.own || []).find(v => v.free);
    return modal("Install Samba", `<p class="small">Samba serves the network shares. Choose the address Windows will find it at.</p>
      <div class="f">${vipPicker("smb", own ? own.ip : (choices.free[0] || ""), choices)}</div>
      <div class="row" style="margin-top:14px"><button class="btn pri" onclick="sambaInstallGo()">Install</button><button class="btn" onclick="closeModal()">Cancel</button></div>`);
  }
  if (!on && !confirm("Stop Samba? Every share stops being served until it is switched on again; their volumes, settings and passwords are kept.")) {
    box.checked = true; return;
  }
  box.disabled = true;
  try {
    const r = await api("/api/self/samba", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ enabled: on }) });
    toast(r.detail, "ok");
    setTimeout(() => { addonsPaint(); selfHealthPaint(); }, 1500);
  } catch (e) { toast(e.message, "bad"); box.checked = !on; box.disabled = false; }
};

window.sambaInstallGo = async (selectedAddress = null) => {
  const address = selectedAddress === null ? ($("#smb_lb_ip")?.value || "").trim() : selectedAddress;
  if (!address && !nodeAddressesOnly()) return toast("choose an address - add VIPs under Networking if the list is empty", "bad");
  try {
    const r = await api("/api/self/samba", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ enabled: true, address }) });
    toast(r.detail, "ok"); closeModal(); setTimeout(() => { addonsPaint(); selfHealthPaint(); }, 1500);
  } catch (e) { toast(e.message, "bad"); }
};

window.sambaRemove = () => {
  modal("Remove SMB server", `<p>The SMB address and server workload will be removed. Network shares stop being served.</p>
    <div class="note warn">Share definitions, passwords, all PVCs and their data remain. Re-enable SMB here to serve them again.</div>
    <div class="f"><label>Type <b class="mono">homestead-smb</b> to confirm</label><input id="smb_remove_confirm" autocomplete="off"></div>
    <div class="modalactions"><button class="btn" onclick="closeModal()">Cancel</button><button class="btn danger" onclick="sambaRemoveGo()">Remove server</button></div>`);
};
window.sambaRemoveGo = async () => {
  const confirm = $("#smb_remove_confirm")?.value.trim();
  if (confirm !== "homestead-smb") return toast("type homestead-smb to confirm", "bad");
  try {
    const r = await api("/api/addons/smb/remove", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ confirm }) });
    closeModal(); toast(r.detail, "ok"); addonsPaint(); selfHealthPaint();
  } catch (e) { toast(e.message, "bad"); }
};

window.nfsToggle = async box => {
  const on = box.checked;
  const nfs = STATE.data.nfs || {};
  if (on && !nfs.installed) {
    box.checked = false;
    if (!(nfs.exports || []).length) return modal("Set up NFS exports", `<p>Choose an RWX share and its allowed client IP or CIDR in Network Shares before installing the NFS server.</p>
      <div class="modalactions"><button class="btn pri" onclick="closeModal();go('shares')">Network Shares</button><button class="btn" onclick="closeModal()">Cancel</button></div>`);
    if (nodeAddressesOnly()) return modal("NFS needs a VIP", `<p>k3s ServiceLB cannot provide the dedicated address and preserved client IPs needed for this NFSv4 server. Install kube-vip under Cluster Add-ons, then try again.</p>
      <div class="modalactions"><button class="btn pri" onclick="closeModal();addonsPaint()">OK</button></div>`);
    const choices = await vipChoices();
    choices.used = [];
    choices.own = (choices.own || []).filter(v => v.free);
    const own = (choices.own || []).find(v => v.free);
    return modal("Install NFSv4 server", `<p>A separate NFS container will serve ${(nfs.exports || []).length} selected RWX share(s) on TCP port 2049. File access pauses if its host fails, until the replacement server and storage are available.</p>
      ${nfsRecovery(nfs, true)}
      <div class="note warn">Only the client networks configured for each share can mount it. Removing this server later leaves every share definition and PVC in place.</div>
      <p class="small">SMB and NFS use different ports, but their separately placed servers need separate VIPs to keep client IP restrictions and routing correct after a host failure. Sharing one VIP would require a combined file-server pod.</p>
      <div class="f">${vipPicker("nfs", own ? own.ip : (choices.free[0] || ""), choices)}</div>
      <div class="modalactions"><button class="btn pri" onclick="nfsInstallGo()">Install NFS server</button><button class="btn" onclick="closeModal()">Cancel</button></div>`);
  }
  if (!on && !confirm("Stop NFS? Exported shares become unavailable until it is switched on again. Their definitions and volumes are kept.")) {
    box.checked = true; return;
  }
  box.disabled = true;
  try {
    const result = await api("/api/self/nfs", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ enabled: on }) });
    toast(result.detail, "ok"); setTimeout(() => { addonsPaint(); if (STATE.view === "shares") viewShares(); }, 1500);
  } catch (e) { toast(e.message, "bad"); box.checked = !on; box.disabled = false; }
};
window.nfsInstallGo = async () => {
  const address = ($("#nfs_lb_ip")?.value || "").trim();
  if (!address) return toast("choose a dedicated VIP for NFS", "bad");
  try {
    const result = await api("/api/self/nfs", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ enabled: true, address }) });
    closeModal(); toast(result.detail, "ok"); setTimeout(() => addonsPaint(), 1500);
  } catch (e) { toast(e.message, "bad"); }
};
window.nfsRemove = () => modal("Remove NFS server", `<p>The NFS container and its address will be removed. Clients will lose access to its exports.</p>
  <div class="note warn">Export settings, SMB shares, all PVCs and their data remain.</div>
  <div class="f"><label>Type <b class="mono">homestead-nfs</b> to confirm</label><input id="nfs_remove_confirm" autocomplete="off"></div>
  <div class="modalactions"><button class="btn" onclick="closeModal()">Cancel</button><button class="btn danger" onclick="nfsRemoveGo()">Remove server</button></div>`);
window.nfsRemoveGo = async () => {
  const confirm = $("#nfs_remove_confirm")?.value.trim();
  if (confirm !== "homestead-nfs") return toast("type homestead-nfs to confirm", "bad");
  try {
    const result = await api("/api/addons/nfs/remove", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ confirm }) });
    closeModal(); toast(result.detail, "ok"); addonsPaint();
  } catch (e) { toast(e.message, "bad"); }
};
