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
const SETTINGS_TABS = [["health", "Health"], ["updates", "Updates"], ["hardware", "Hardware"], ["access", "Access"],
  ["apps", "Apps"], ["device", "This device"], ["about", "About"]];

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
          ${can("admin") ? '<button class="btn sm" onclick="hardwareFeatureSettings()">Manage</button>' : ""}</div>
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
          <div><span>Node probe ${tip("Homestead keeps the probe's scripts in step with its own release, so an upgrade needs no kubectl. It never installs the probe itself: the SMART sidecar is privileged.")}</span>
            <b>${esc(probeWord(info.node_probe?.state))}</b><small>${esc(info.node_probe?.detail || "")}</small>
            ${can("admin") ? `<div class="row" style="margin-top:6px">${info.node_probe?.state === "absent"
              ? '<button class="btn sm" onclick="probeInstallConfirm()">Install</button>'
              : '<button class="btn sm" onclick="probeRemove()">Remove</button>'}</div>` : ""}</div>
          ${permissionsCell(info.permissions)}
        </div>
      </section>
    </div>`);
  pwaPaint();
  namespacesPaint();
}

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
