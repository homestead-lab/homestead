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

  paint(`<div class="phead"><div><h2>Settings</h2><p>Cluster policy, hardware, access, and installation information</p></div>
    <button class="btn" onclick="document.getElementById('drawer').classList.add('open')">Appearance</button></div>

    <div class="settings-grid">
      <section class="card flat settings-wide">
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

      <section class="card flat settings-wide">
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

      <section class="card flat settings-wide">
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

      <section class="card flat">
        <div class="settings-card-head"><div><div class="ctitle">Hardware features</div><div class="csub">Reusable passthrough paths and automatic host detection</div></div>
          ${can("admin") ? '<button class="btn sm" onclick="hardwareFeatureSettings()">Manage</button>' : ""}</div>
        <div class="settings-list">${hardwareRows || '<div class="empty small">No hardware features configured.</div>'}</div>
      </section>

      <section class="card flat">
        <div class="settings-card-head"><div><div class="ctitle">Account & access</div><div class="csub">Signed in as ${esc(ME || "—")}</div></div><span class="${roleClass(ROLE)} rolechip">${esc(ROLE || "—")}</span></div>
        <div class="role-summary"><b>${esc(ROLE || "viewer")}</b><span>${esc(ROLE_COPY[ROLE] || ROLE_COPY.viewer)}</span></div>
        <div class="row settings-actions"><button class="btn sm" onclick="pwChange()">Change password</button>
          ${can("admin") ? '<button class="btn sm" onclick="manageUsers()">Manage users</button>' : ""}
          <button class="btn sm danger" onclick="doLogout()">Sign out</button></div>
        <div class="role-legend">
          ${Object.entries(ROLE_COPY).map(([role, copy]) => `<div><span class="${roleClass(role)} rolechip">${role}</span><span class="dim xs">${esc(copy)}</span></div>`).join("")}
        </div>
        ${users.length ? `<div class="sec">Users</div><div class="tblwrap"><table class="tbl dense"><thead><tr><th>User</th><th>Role</th><th>Last sign-in</th></tr></thead><tbody>${userRows}</tbody></table></div>` : ""}
      </section>

      <section class="card flat settings-wide">
        <div class="ctitle">About this installation</div><div class="csub">Runtime and cluster connection details</div>
        <div class="about-grid">
          <div><span>Homestead</span><b>v${esc(info.version || HOMESTEAD_VERSION)}</b></div>
          <div><span>Kubernetes</span><b>${esc(info.kubernetes || "—")}</b></div>
          <div><span>Default namespace</span><b class="mono">${esc(info.namespace || "lab")}</b></div>
          <div><span>Storage class</span><b class="mono">${esc(info.storage_class || "—")}</b></div>
          <div><span>Cluster VIP</span><b class="mono">${esc(overview?.lb_ip || info.vip || "—")}</b></div>
          <div><span>Nodes ready</span><b>${overview ? `${overview.nodes_ready}/${overview.nodes_total}` : "—"}</b></div>
        </div>
      </section>
    </div>`);
}

window.saveHealthSettings = async () => {
  const read = id => ({ warning: +$("#set_" + id + "_warn").value, critical: +$("#set_" + id + "_crit").value });
  const body = { thresholds: { cpu: read("cpu"), memory: read("memory"), disk: read("disk"), temperature: read("temperature") },
    smart: { temperature: read("drive_temperature"), reallocated_warning: +$("#set_smart_reallocated").value,
      pending_critical: +$("#set_smart_pending").value, uncorrectable_critical: +$("#set_smart_uncorrectable").value,
      notify_failures: $("#set_smart_notify").checked },
    updates: STATE.data.appSettings?.updates };
  try {
    const saved = await api("/api/settings", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
    HEALTH = { thresholds: { ...HEALTH_DEFAULTS.thresholds, ...(saved.thresholds || {}) } };
    STATE.data.appSettings = null;
    toast("health thresholds saved", "ok");
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
