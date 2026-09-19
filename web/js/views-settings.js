/* Full settings workspace: cluster policy, hardware, access, and installation info. */

const ROLE_COPY = {
  viewer: "Read-only access to dashboards, workloads, storage, events, and settings.",
  operator: "Viewer access plus deploy, edit, move, start, and stop workloads and VMs.",
  admin: "Full access, including users, hosts, hardware mappings, imports, and cluster policy.",
};

function thresholdEditor(id, label, unit, help, pair) {
  return `<div class="threshold-card">
    <div><b>${esc(label)}</b><div class="dim xs">${esc(help)}</div></div>
    <div class="threshold-values">
      <label>Warning <span class="warn-dot"></span><input id="set_${id}_warn" type="number" min="1" max="${id === "temperature" ? 119 : 99}" value="${pair.warning}" ${can("admin") ? "" : "disabled"}><span>${unit}</span></label>
      <label>Critical <span class="crit-dot"></span><input id="set_${id}_crit" type="number" min="2" max="${id === "temperature" ? 120 : 100}" value="${pair.critical}" ${can("admin") ? "" : "disabled"}><span>${unit}</span></label>
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
          <div><span>HarvUI</span><b>v${esc(info.version || HARVUI_VERSION)}</b></div>
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
  const body = { thresholds: { cpu: read("cpu"), memory: read("memory"), disk: read("disk"), temperature: read("temperature") } };
  try {
    const saved = await api("/api/settings", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
    HEALTH = { thresholds: { ...HEALTH_DEFAULTS.thresholds, ...(saved.thresholds || {}) } };
    STATE.data.appSettings = null;
    toast("health thresholds saved", "ok");
    viewSettings();
  } catch (e) { toast(e.message, "bad"); }
};

