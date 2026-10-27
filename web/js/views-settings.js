/* Full settings workspace: cluster policy, hardware, access, and installation info. */

const ROLE_COPY = {
  viewer: "Read-only access to dashboards, apps and VMs, storage, events, and settings.",
  operator: "Viewer access plus deploy, edit, move, start, and stop apps and VMs and VMs.",
  admin: "Full access, including users, hosts, hardware mappings, imports, and cluster policy.",
};
// What each role adds, in a word or two, beside its name where it is chosen (#350).
const ROLE_SHORT = { viewer: "looks, changes nothing", operator: "runs apps and VMs", admin: "everything, users and hosts too" };
const ROLE_NAMES = { viewer: "Viewer", operator: "Operator", admin: "Admin" };
// The full comparison, for the rare time it is needed.
const ROLE_TABLE = [
  ["See dashboards, apps, VMs, storage, events and settings", 1, 1, 1],
  ["Deploy, edit, move, start, stop and update apps and VMs", 0, 1, 1],
  ["Snapshots, backups, restores and schedules", 0, 1, 1],
  ["Users, roles and API keys", 0, 0, 1],
  ["Hosts: cordon, drain, reboot, shell, disks and hardware", 0, 0, 1],
  ["Network shares, imports and cluster policy", 0, 0, 1],
];
const USERS_FILTER = { role: "" };

function userRow(u, admin, count) {
  const me = u.name === ME;
  const role = !admin ? `<span class="${roleClass(u.role)} rolechip">${esc(ROLE_NAMES[u.role] || u.role)}</span>`
    : me ? `<span class="${roleClass(u.role)} rolechip">${esc(ROLE_NAMES[u.role] || u.role)}</span><div class="dim xs">You can't change your own role</div>`
    : `<select class="role-select" aria-label="Role for ${esc(u.name)}" onchange="userRoleSet(${jsq(u.name)},this)" data-was="${esc(u.role)}">
        ${["viewer", "operator", "admin"].map(r => `<option value="${r}"${u.role === r ? " selected" : ""}>${ROLE_NAMES[r]} - ${esc(ROLE_SHORT[r])}</option>`).join("")}</select>`;
  const actions = admin ? actionBar([
    { label: "Sign-in history", run: `userSignins(${jsq(u.name)})` },
    !me && count > 1 && { label: "Remove", run: `userRemove(${jsq(u.name)})`, danger: true }], { shown: 0, label: `More for ${u.name}` }) : "";
  return `<tr data-role="${esc(u.role)}"${USERS_FILTER.role && USERS_FILTER.role !== u.role ? " hidden" : ""}><td><b>${esc(u.name)}</b>${me ? ` ${UI.chip("you", "ok")}` : ""}</td>
    <td data-label="Role">${role}</td><td data-label="Last sign-in" class="dim xs mono">${esc(u.last_login || "never")}</td>${admin ? `<td>${actions}</td>` : ""}</tr>`;
}

function usersFilterChips(users) {
  const n = r => users.filter(u => !r || u.role === r).length;
  return `<div class="pf-shows users-filter" role="group" aria-label="Show">${[["", "All"], ["admin", "Admins"], ["operator", "Operators"], ["viewer", "Viewers"]]
    .filter(([r]) => !r || n(r)).map(([r, label]) => `<button type="button" class="pf-chip" aria-pressed="${USERS_FILTER.role === r}" onclick="usersFilter(${jsq(r)})">${label} <span class="pf-n">${n(r)}</span></button>`).join("")}</div>`;
}

window.usersFilter = role => {
  USERS_FILTER.role = role;
  document.querySelectorAll(".users-filter .pf-chip").forEach(b => b.setAttribute("aria-pressed", String(b.getAttribute("onclick").includes(`'${role}'`) || b.getAttribute("onclick").includes(`"${role}"`))));
  document.querySelectorAll("#usersTable tbody tr").forEach(tr => { tr.hidden = !!role && tr.dataset.role !== role; });
};

window.rolesCompare = () => modal("Roles", `<div class="ui-stack">
  ${UI.lead("Each role can do what the one before it can, and more.")}
  <div class="tblwrap"><table class="tbl stack dense roles-table"><thead><tr><th></th><th>Viewer</th><th>Operator</th><th>Admin</th></tr></thead><tbody>
  ${ROLE_TABLE.map(([what, ...yes]) => `<tr><td>${esc(what)}</td>${yes.map((y, i) => `<td class="center" data-label="${["Viewer", "Operator", "Admin"][i]}">${y ? '<span aria-label="yes">✓</span>' : '<span class="dim" aria-label="no">–</span>'}</td>`).join("")}</tr>`).join("")}
  </tbody></table></div>
  <div class="dim xs">An API key never manages users or keys, reaches a host's shell or changes settings, whatever its scopes.</div>
  ${UI.actions(UI.cancel("Close"))}</div>`);

function thresholdEditor(id, label, unit, help, pair) {
  const temperature = id.includes("temperature");
  return `<div class="threshold-card">
    <div><b>${esc(label)}</b><div class="dim xs">${esc(help)}</div></div>
    <div class="threshold-values">
      <label>Warning <span class="warn-dot"></span><input id="set_${id}_warn" type="number" min="1" max="${temperature ? 119 : 99}" value="${pair.warning}" ${can("admin") ? "" : "disabled"}><span>${unit}</span></label>
      <label>Critical <span class="crit-dot"></span><input id="set_${id}_crit" type="number" min="2" max="${temperature ? 120 : 100}" value="${pair.critical}" ${can("admin") ? "" : "disabled"}><span>${unit}</span></label>
    </div></div>`;
}

/* Ten sections, each gathering the cards of one topic. A card names its
   topic (data-tab); a section shows the cards of every topic it holds, and a
   link or button can still ask for a topic by its own name - settingsTab("fleet"). */
const SETTINGS_SECTIONS = [
  // [key, label, description, topics, group, icon from index.html's sprite]
  ["homestead", "Homestead", "Version, health, copies, backup", ["homestead", "about"], "Cluster", "home"],
  ["updates", "Updates", "Releases, platform, hosts, containers", ["updates"], "Cluster", "update"],
  ["placement", "Placement", "How hosts share the work", ["placement"], "Cluster", "node"],
  ["monitoring", "Monitoring", "Thresholds and drives", ["monitoring", "health"], "Cluster", "pulse"],
  ["hardware", "Hardware and storage", "Devices, add-ons, Longhorn", ["hardware", "cluster", "namespaces"], "Cluster", "chip"],
  ["fleet", "Linked clusters", "Other Homesteads, moves", ["fleet"], "Cluster", "link"],
  ["connections", "Connections", "UniFi, MQTT, alert webhook, App Store", ["connections", "mqtt", "apps", "integrations"], "Cluster", "plug"],
  ["access", "Users and access", "Accounts and roles", ["access"], "Cluster", "users"],
  ["troubleshooting", "Troubleshooting", "Bug reports and logs", ["troubleshooting"], "Cluster", "bug"],
  ["you", "You", "Appearance, this device, account", ["you", "device", "general"], "Just you", "user"],
];
const settingsSection = topic => (SETTINGS_SECTIONS.find(([id, , , topics]) => id === topic || topics.includes(topic)) || SETTINGS_SECTIONS[0])[0];

function settingsTab(pick, load = true) {
  if (pick) {
    const section = settingsSection(pick);
    try { localStorage.setItem("homestead.settings.tab", section); } catch (e) { /* this visit only */ }
    // The address follows, so a re-render or a reload stays on this section
    // rather than going back to the one a link first named.
    if (typeof location !== "undefined" && typeof history !== "undefined") {
      const url = new URL(location.href);
      if (url.searchParams.get("tab") && url.searchParams.get("tab") !== section) {
        url.searchParams.set("tab", section);
        history.replaceState(history.state, "", url.pathname + url.search);
      }
    }
    // Asked for: open it, on a phone as well, where the list comes first.
    STATE.settingsOpen = true;
    UI.selectWorkspace($(".settings-layout"), section, {gridSelector:".settings-grid"});
    // Shown without a repaint: its cards read their data once they can be seen.
    // A link that goes on to open Settings repaints, and that repaint reads
    // them, so the load waits to see whether a navigation followed.
    if (load && STATE.view === "settings" && $(".settings-layout")) {
      const nav = window.NAV_TOKEN;
      queueMicrotask(() => { if (window.NAV_TOKEN === nav) settingsLoad(section); });
    }
    if (typeof window !== "undefined" && window.scrollPageTop) window.scrollPageTop();
    return section;
  }
  // A link can name the section or a topic in it - /settings?tab=about - and it is then kept.
  const asked = typeof location !== "undefined" ? new URLSearchParams(location.search).get("tab") : "";
  if (asked && SETTINGS_SECTIONS.some(([id, , , topics]) => id === asked || topics.includes(asked))) {
    STATE.settingsOpen = true;
    try { localStorage.setItem("homestead.settings.tab", settingsSection(asked)); } catch (e) { /* this visit only */ }
    return settingsSection(asked);
  }
  let saved = "";
  try { saved = localStorage.getItem("homestead.settings.tab") || ""; } catch (e) { /* default */ }
  return settingsSection(saved || "homestead");
}
window.settingsTab = settingsTab;
/* A phone shows the list of sections first; back returns to it. */
window.settingsBack = () => {
  STATE.settingsOpen = false;
  UI.selectWorkspace($(".settings-layout"), "");
  window.scrollPageTop();
};

async function viewSettings() {
  const [settings, features, overview, users, placementRead] = await Promise.all([
    loadHealthSettings(true), loadHardwareFeatures(),
    // A read that failed is said where its answer is shown, never an older
    // answer as if current, nor none (#620).
    api("/api/overview").catch(e => ({ unread: e.message })),
    can("admin") ? api("/api/auth/users").catch(e => ({ unread: e.message })) : Promise.resolve([]),
    api("/api/placement").catch(e => ({ unread: e.message })),
  ]);
  const hostsUnread = overview.unread || "", kept = hostsUnread ? STATE.data.ov : null;
  if (!hostsUnread) STATE.data.ov = overview;
  const usersUnread = users.unread || "";
  STATE.data.placementUnread = placementRead.unread || "";
  const placement = STATE.data.placement = placementRead.unread ? null : placementRead;
  const thresholds = settings.thresholds || HEALTH_DEFAULTS.thresholds;
  const updates = settings.updates || { policy: "approval_required", notify_available: true,
    notify_failures: true, maintenance: { days: [0, 1, 2, 3, 4, 5, 6], start: "02:00", duration_minutes: 120 } };
  const maintenance = updates.maintenance || {};
  const rebalanceMode = settings.placement?.rebalance || "off";
  const placeMode = settings.placement?.mode || "";
  const standbyHosts = new Set(settings.placement?.standby_nodes || []);
  const smart = settings.smart || { temperature: { warning: 55, critical: 65 },
    reallocated_warning: 1, pending_critical: 1, uncorrectable_critical: 1, notify_failures: true };
  const info = settings.info || {};
  // Hosts read before stay only when said to be from the last read.
  const nodes = (hostsUnread ? kept?.nodes : overview.nodes) || STATE.data.nodes || null;
  const hostsNote = !hostsUnread ? "" : nodes ? UI.callout("warn", "Could not read the hosts just now", `${esc(hostsUnread)} · what each host has is from the last read.`)
    : UI.callout("bad", "Could not read the hosts", `${esc(hostsUnread)} · which hosts have each feature is not known.`);
  const hardwareRows = features.map(f => {
    const hosts = (nodes || []).filter(n => n.hardware?.[f.id]).map(n => n.name.replace("harvester-", ""));
    return `<div class="settings-list-row"><div><b>${esc(f.name)}</b><div class="dim xs mono">${esc(f.host_path)} → ${esc(f.container_path || f.host_path)}</div></div>
      <div class="settings-list-meta">${hosts.length ? `${UI.chip(`${hosts.length} host${hosts.length === 1 ? "" : "s"}`, "hw")}<span class="dim xs">${esc(hosts.join(", "))}</span>` : nodes ? UI.chip("no host detected", "warn") : UI.chip("hosts not read", "neutral")}</div></div>`;
  }).join("");
  const userList = usersUnread ? [] : users;
  const userRows = userList.map(u => userRow(u, can("admin"), userList.length)).join("");

  const tab = settingsTab();
  paint(`${UI.pageHeader(`Settings`, `Cluster policy, hardware, access, and installation information`, ``, {mobileSummary:"omit"})}
    ${UI.workspace(UI.workspaceNav(SETTINGS_SECTIONS.map(([key,label,description,,group,icon]) => ({key,label,descriptionHtml:esc(description),group,icon})),
      {label:"Settings sections",selected:tab,onSelect:key => `settingsGo(${jsArg(key)})`}),
      UI.settingsGrid(`
      ${UI.settingsCard(`${UI.moduleHeader(`Appearance`, `This browser only; it changes as you choose`, ``)}
        <div class="srows">
          ${settingRow("Theme", "", `<div class="seg appearance-seg" id="optTheme"><button type="button" class="opt" data-v="dark">Dark</button><button type="button" class="opt" data-v="light">Light</button><button type="button" class="opt" data-v="auto">Auto</button></div>`)}
          ${settingRow("Background", "What the glass blurs behind it", `<div class="seg appearance-seg" id="optBg"><button type="button" class="opt" data-v="soft">Soft</button><button type="button" class="opt" data-v="gold">Gold</button><button type="button" class="opt" data-v="plain">Plain</button></div>`)}
          ${settingRow("Glass blur", "How frosted the panels are", `<input type="range" class="rng" id="optBlur" min="0" max="60" step="2"><span class="dim small mono"><span id="blurVal"></span>px</span>`)}
          ${settingRow("Motion", "Animated data flow along connectors", `<div class="seg appearance-seg" id="optMotion"><button type="button" class="opt" data-v="on">On</button><button type="button" class="opt" data-v="off">Reduced</button></div>`)}
          ${settingRow("Refresh every", "Pages patch themselves in place", `<input type="range" class="rng" id="optRefresh" min="5" max="60" step="5"><span class="dim small mono"><span id="refreshVal"></span>s</span>`)}
        </div>`, {tab:`you`})}
      ${UI.settingsCard(`${UI.moduleHeader(`Your account`, `Signed in as ${esc(ME || "—")} · ${esc(ROLE || "viewer")}`, ``)}
        <div class="srows">
          ${settingRow("Password", "", '<button class="btn sm" onclick="pwChange()">Change</button>')}
          ${settingRow("Sign out everywhere", "Every session of yours, on every device", '<button class="btn sm" onclick="signOutEverywhere()">Sign out everywhere</button>')}
          ${settingRow("Sign out", "This browser", '<button class="btn sm" onclick="doLogout()">Sign out</button>')}
        </div><div class="dim xs" style="margin-top:8px">${sessionSummary(AUTH_STATE)}</div>`, {tab:`you`})}
      ${UI.settingsCard(`
        ${UI.moduleHeader(`Health thresholds`, `When bars and host cards turn amber and red, for everyone ${tip("Warning turns a bar amber, critical red. A host shows its worst result across CPU, memory, disk and temperature.")}`, `${can("admin") ? "" : UI.chip("admin managed", "neutral")}`)}
        <div class="threshold-grid">
          ${thresholdEditor("cpu", "CPU utilisation", "%", "Sustained host CPU pressure", thresholds.cpu)}
          ${thresholdEditor("memory", "Memory utilisation", "%", "Allocated host RAM pressure", thresholds.memory)}
          ${thresholdEditor("disk", "Host disk utilisation", "%", "Local filesystem capacity", thresholds.disk)}
          ${thresholdEditor("temperature", "CPU temperature", "°C", "Host thermal warning", thresholds.temperature)}
        </div>
      `, {tab:`health`, save:`app`})}

      ${UI.settingsCard(`
        ${UI.moduleHeader(`Drive health`, `SMART warnings shown on host cards and in cluster health`, `${can("admin") ? "" : UI.chip("admin managed", "neutral")}`)}
        <div class="threshold-grid">
          ${thresholdEditor("drive_temperature", "Drive temperature", "°C", "SATA, SAS, and NVMe temperature", smart.temperature)}
          <div class="threshold-card counters"><div><b>Media counters</b> ${tip("NVMe reports media errors; ATA disks report reallocated, pending and uncorrectable sectors. A counter a drive doesn't report shows as unsupported, not zero.")}<div class="dim xs">Recommended: 1 for each counter (first reported sector).</div></div>
            <div class="smart-threshold-values">
              <label>Reallocated · warning ${tip("Sectors already replaced with spares. A stable count can be historical; review increases and verify backups.")} <input id="set_smart_reallocated" type="number" min="1" max="1000000" value="${smart.reallocated_warning}" ${can("admin") ? "" : "disabled"}></label>
              <label>Pending · critical ${tip("Sectors the drive cannot currently read reliably. Verify backups and investigate any nonzero count.")} <input id="set_smart_pending" type="number" min="1" max="1000000" value="${smart.pending_critical}" ${can("admin") ? "" : "disabled"}></label>
              <label>Uncorrectable · critical ${tip("Sectors a drive scan could not recover. Check backups and SMART test results. Some drives retain historical counts.")} <input id="set_smart_uncorrectable" type="number" min="1" max="1000000" value="${smart.uncorrectable_critical}" ${can("admin") ? "" : "disabled"}></label>
            </div></div>
        </div>
        <div class="srows" style="margin-top:14px">${settingRow("Include SMART failures and threshold breaches in cluster health notifications", "",
          `<label class="toggle"><input id="set_smart_notify" type="checkbox" ${smart.notify_failures !== false ? "checked" : ""} ${can("admin") ? "" : "disabled"}><span></span></label>`)}</div>
      `, {tab:`health`, save:`app`})}

      ${UI.settingsCard(`
        ${UI.moduleHeader(`Container updates`, `When Homestead looks for newer images, and when a reviewed rollout may start`, `${can("admin") ? "" : UI.chip("admin managed", "neutral")}`)}
        <div class="srows">
          ${settingRow(`Policy ${tip("Notify only blocks installs. Approval required permits a reviewed manual rollout. Maintenance window permits reviewed rollouts only during the window. Major releases are never picked on their own, and every install is reviewed.")}`, "",
            `<select id="set_update_policy" ${can("admin") ? "" : "disabled"}>
              <option value="notify_only" ${updates.policy === "notify_only" ? "selected" : ""}>Notify only - block installs</option>
              <option value="approval_required" ${updates.policy === "approval_required" ? "selected" : ""}>Approval required - manual rollout</option>
              <option value="maintenance_window" ${updates.policy === "maintenance_window" ? "selected" : ""}>Maintenance window - manual rollout in window</option>
            </select>`)}
          ${settingRow("Tell me about new images", "", `<label class="toggle"><input id="set_notify_available" type="checkbox" ${updates.notify_available !== false ? "checked" : ""} ${can("admin") ? "" : "disabled"}><span></span></label>`)}
          ${settingRow("Tell me when a registry check fails", "", `<label class="toggle"><input id="set_notify_failures" type="checkbox" ${updates.notify_failures !== false ? "checked" : ""} ${can("admin") ? "" : "disabled"}><span></span></label>`)}
          ${settingRow("Update window", `<span id="set_update_window">${esc(updateWindowText(maintenance))}</span>` + (updates.policy === "maintenance_window" ? ""
            : `<div class="dim xs">${updates.policy === "notify_only" ? "No image is installed while the policy is notify only, not even by apps set to update themselves. Moving containers back after a host returns still uses this window, if set to."
              : "Rollouts you approve start straight away under this policy; only apps set to update themselves wait for this window."}</div>`),
            can("admin") ? '<button class="btn sm" onclick="updateWindowEdit()">Change</button>' : "")}
        </div>
      `, {tab:`updates`, save:`app`})}

      ${UI.settingsCard(`
        ${UI.moduleHeader(`How hosts share the work`, `One choice for the whole cluster, instead of placement set app by app`, `${can("admin") ? "" : UI.chip("admin managed", "neutral")}`)}
        <div class="srows">
          ${settingRow(`Share the work ${tip("Hot spare taints its hosts homestead.io/standby (PreferNoSchedule): the scheduler avoids them while another host has room, and uses them when none has. No app changes, so it works for apps Flux, Argo CD or Helm apply.")}`,
            `<div class="dim xs" id="set_place_text">${placeModeText(placeMode, placement)}</div>`,
            `<select id="set_place_mode" aria-label="Share the work" aria-describedby="set_place_text" ${can("admin") ? "" : "disabled"} onchange="placeModeChanged()">
              ${placeMode ? "" : '<option value="" selected>Not chosen yet</option>'}
              ${[["default", "Kubernetes default"], ["spread", "Spread by capacity"], ["standby", "Hot spare"]].map(([v, l]) =>
                `<option value="${v}" ${placeMode === v ? "selected" : ""}>${l}</option>`).join("")}
            </select>`)}
          <div id="set_standby_row" ${placeMode === "standby" ? "" : "hidden"}>
            ${settingRow(`Standby hosts ${tip("Take apps only when the others cannot. A host is never made standby when no other host takes work.")}`,
              STATE.data.placementUnread ? `<span class="badtext">Could not read the hosts: ${esc(STATE.data.placementUnread)}</span>` : (placement?.notes || []).map(esc).join("; "),
              // A standby host no longer in the cluster stays listed, ticked,
              // so saving another section does not quietly drop it.
              `<div class="standby-hosts">${[...(placement?.hosts || []),
                  ...[...standbyHosts].filter(n => !(placement?.hosts || []).some(h => h.name === n)).map(name => ({ name, gone: true }))]
                .map(h => `<label class="switch"><input type="checkbox" class="set_standby" value="${esc(h.name)}"
                ${standbyHosts.has(h.name) ? "checked" : ""} ${can("admin") ? "" : "disabled"}> <span class="mono">${esc(h.name)}</span>${h.gone
                  ? ` <span class="dim xs">${placement ? "not in this cluster" : "hosts could not be read"}</span>` : h.takes_work ? "" : ' <span class="dim xs">not taking work now</span>'}</label>`).join("")
                || (STATE.data.placementUnread ? "" : '<span class="dim xs">No hosts reported.</span>')}</div>`)}
          </div>
        </div>
      `, {tab:`placement`, save:`app`})}

      ${UI.settingsCard(`
        ${UI.moduleHeader(`If a host fails`, `Whether the other hosts have room for what it runs, by what apps reserve and their priority tier`, ``)}
        <div id="capacityForecast"><div class="empty small"><span class="spin2"></span> Working it out for each host…</div></div>
      `, {tab:`placement`})}

      ${UI.settingsCard(`
        ${UI.moduleHeader(`When a host comes back`, `Containers start on the other hosts while one is down or drained, and stay there when it returns`, `${can("admin") ? "" : UI.chip("admin managed", "neutral")}`)}
        <div class="srows">
          ${settingRow(`Move containers back ${tip("Uses Balance hosts: only moves that bring the busiest host down enough, each restarting its container once. In the window, an app whose scheduled job (a backup, say) runs before the window closes is left where it is, since the job needs its volume on that host.")}`,
            `<div class="dim xs" id="set_rebalance_text" aria-live="polite">${rebalanceModeText(rebalanceMode, maintenance)}</div>`,
            `<select id="set_rebalance" aria-label="Move containers back" aria-describedby="set_rebalance_text" ${can("admin") ? "" : "disabled"}
              onchange="$('#set_rebalance_text').innerHTML=rebalanceModeText(this.value,(STATE.data.appSettings?.updates||{}).maintenance||{})">
              <option value="off" ${rebalanceMode === "off" ? "selected" : ""}>No - leave them where they are</option>
              <option value="suggest" ${rebalanceMode === "suggest" ? "selected" : ""}>Suggest - an alert to review the moves</option>
              <option value="window" ${rebalanceMode === "window" ? "selected" : ""}>In the update window</option>
            </select>`)}
        </div>
      `, {tab:`placement`, save:`app`})}

      ${UI.settingsCard(`
        ${UI.moduleHeader(`Hardware features`, `Reusable passthrough paths and automatic host detection`, `${can("operator") ? '<button class="btn sm" onclick="hardwareRescan(this)" title="Look for devices plugged in since the last check">Rescan hosts</button>' : ""}
          ${can("admin") ? '<button class="btn sm" onclick="hardwareFeatureSettings()">Manage</button>' : ""}`)}
        <div class="dim xs" style="margin:-4px 0 8px">Hosts are checked every 30 seconds, so a device plugged in later is found without a restart.</div>
        ${hostsNote}<div class="settings-list">${hardwareRows || '<div class="empty small">No hardware features configured.</div>'}</div>
      `, {tab:`hardware`, wide:false})}

      ${UI.settingsCard(`
        ${UI.moduleHeader(`Users and roles`, `Who can sign in, and what each may do`, `<button class="btn sm" onclick="rolesCompare()">Compare roles</button>${can("admin") ? '<button class="btn sm pri" onclick="userAdd()">＋ Add user</button>' : ""}`)}
        ${usersUnread ? UI.callout("bad", "Could not read the users", esc(usersUnread)) : ""}
        ${userList.length > 1 ? usersFilterChips(userList) : ""}
        ${userList.length ? `<div class="tblwrap"><table class="tbl stack dense" id="usersTable"><thead><tr><th>User</th><th>Role</th><th>Last sign-in</th>${can("admin") ? "<th></th>" : ""}</tr></thead><tbody>${userRows}</tbody></table></div>` : ""}
      `, {tab:`access`, wide:false})}
      ${can("admin") ? UI.settingsCard('<div class="empty small"><span class="spin2"></span>reading keys</div>', {tab:"access",id:"apiKeysCard",wide:false}) : ""}
      ${can("admin") ? UI.settingsCard('<div class="empty small"><span class="spin2"></span>reading agent settings</div>', {tab:"access",id:"mcpCard",wide:false}) : ""}

      ${pwaCard()}

      ${ipamUnifiCard()}

      ${UI.settingsCard(``, {tab:`mqtt`, id:`mqttCard`})}
      ${UI.settingsCard(``, {tab:`connections`, id:`alertHookCard`})}
      ${UI.settingsCard(`<div class="empty small"><span class="spin2"></span> asking each cluster</div>`, {tab:`fleet`, id:`fleetCard`})}
      ${UI.settingsCard(``, {tab:`fleet`, id:`fleetMovesCard`, hidden:true})}
      ${UI.settingsCard(``, {tab:`cluster`, id:`addonsCard`, hidden:true})}
      ${UI.settingsCard(`<div class="empty small"><span class="spin2"></span>reading Longhorn</div>`, {tab:`cluster`, id:`lhSettingsCard`, save:`lh`})}
      ${UI.settingsCard(``, {tab:`cluster`, id:`storageClassesCard`, hidden:true})}

      ${UI.settingsCard(`
        ${UI.moduleHeader(`App Store catalogue`, `The feed the App Store lists apps from: any feed in the Community Applications format`, ``)}
        ${serviceRow(STATE.data.appSettings?.catalog_url ? "Your own feed" : "Community Applications", UI.chip("in use", "ok"),
          esc(STATE.data.appSettings?.catalog_url || "The public feed"), can("admin") ? actionBar([{ label: "Change", run: "catalogEdit()" }]) : "")}
      `, {tab:`connections`})}

      ${UI.settingsCard(`
        ${UI.moduleHeader(`Namespaces`, `The namespaces apps are deployed into; Deploy offers each of them. Harvester, Rancher and Kubernetes keep their own, which are hidden here and in every picker.`, `${can("admin") ? `<div class="row ns-new"><input id="nsName" aria-label="New namespace" placeholder="new-namespace" maxlength="63" autocomplete="off"
            onkeydown="if(event.key==='Enter')namespaceCreate()"><button class="btn sm pri" onclick="namespaceCreate()">Create</button></div>` : ""}`)}
        <div class="ns-body"><div class="empty small"><span class="spin2"></span></div></div>
      `, {tab:`namespaces`, id:`nsCard`})}

      ${UI.settingsCard(`
        ${UI.moduleHeader(`About this installation`, `What runs here, and what it is called`, ``)}
        <div class="srows">${settingRow(`Site name ${tip("Shown under the Homestead wordmark and at the foot of the page; leave it blank to show nothing.")}`, "",
          `<input type="text" id="set_site_name" maxlength="40" placeholder="e.g. Main site" value="${esc(STATE.data.appSettings?.site_name || "")}" ${can("admin") ? "" : "disabled"}>`)}</div>
        <div class="about-grid">
          <div><span>Homestead</span><b>v${esc(info.version || HOMESTEAD_VERSION)}</b></div>
          <div><span>Kubernetes</span><b>${esc(info.kubernetes || "—")}</b></div>
          <div><span>Default namespace</span><b class="mono">${esc(info.namespace || "lab")}</b></div>
          <div><span>Storage class</span><b class="mono">${esc(info.storage_class || "—")}</b></div>
          <div><span>Cluster VIP</span><b class="mono">${esc((hostsUnread ? "" : overview.lb_ip) || info.vip || "—")}</b></div>
          <div><span>Hosts ready</span><b>${hostsUnread ? `<span class="dim" title="${esc(hostsUnread)}">not read</span>` : `${overview.nodes_ready}/${overview.nodes_total}`}</b></div>
          <div><span>Node probe ${tip("Homestead keeps the probe's scripts in step with its own release, so an upgrade needs no kubectl. Installation and removal live under Cluster → Add-ons because the SMART sidecar is privileged.")}</span>
            <b>${esc(probeWord(info.node_probe?.state))}</b><small>${esc(info.node_probe?.detail || "")}</small></div>
          ${permissionsCell(info.permissions)}
        </div>
        <div class="row" style="margin-top:12px"><button class="btn sm" onclick="welcomeCheck(true)">Open setup guide</button></div>
      `, {tab:`about`, save:`app`})}

      ${UI.settingsCard(``, {tab:`updates`, id:`homesteadUpdateCard`})}
      ${UI.settingsCard(`
        ${UI.moduleHeader(`Platform versions`, ``, ``)}
        <div class="empty small"><span class="spin2"></span> checking platform versions</div>
      `, {tab:`updates`, id:`settingsComponents`})}
      ${UI.settingsCard(`
        ${UI.moduleHeader(`Host updates`, ``, ``)}
        <div class="empty small"><span class="spin2"></span> reading host update status</div>
      `, {tab:`updates`, id:`settingsHostUpdates`})}
      ${window.troubleshootingCards ? troubleshootingCards() : ""}
      ${UI.settingsCard(`${window.configCardHtml ? configCardHtml() : ""}`, {tab:`about`, id:`configCard`})}
      ${UI.settingsCard(`<div class="empty small"><span class="spin2"></span> checking Homestead</div>`, {tab:`about`, id:`selfHealthCard`})}
      ${UI.settingsCard(`${STATE.data.replicaHtml || ""}`, {tab:`about`, id:`replicaCard`})}
      ${UI.settingsCard(`<div class="empty small"><span class="spin2"></span> reading Homestead's data</div>`, {tab:`about`, id:`homestead-data`})}
    `, tab) + UI.saveBar({id:"settingsSaveBar",messageId:"settingsSaveMsg",save:"settingsSave(this)",discard:"settingsDiscard()"}),
      {open:STATE.settingsOpen,backLabel:"All settings",currentLabel:SETTINGS_SECTIONS.find(([id])=>id===tab)?.[1],back:"settingsGo('')"})}`);
  STATE.settingsDirty = new Set();
  if (settings === HEALTH_DEFAULTS) toast("The settings could not be read, so the defaults are shown and saving them is refused. Reload to try again.", "bad");
  if (window.bindAppearance) bindAppearance();
  STATE.settingsLoaded = new Set();
  settingsLoad(tab);
}

// What each section's cards read (#705). Every card is painted, but only the
// section on screen reads its data: opening Settings read every section's,
// some twice, and each section switch read them all again.
const SETTINGS_LOADERS = {
  homestead: [() => replicasPaint(), () => homesteadDataPaint(), () => selfHealthPaint()],
  updates: [
    () => { if (window.homesteadUpdateCardPaint) { homesteadUpdateCardPaint(); if (!STATE.data.imageUpdates) loadImageUpdates(false, true); } },
    () => clusterComponentsPaint(), () => window.osUpdatesCardPaint?.(),
  ],
  // Every host's preview at once is not cheap: only when it is to be seen.
  placement: [() => capacityForecastLoad()],
  hardware: [() => namespacesPaint(), () => lhSettingsPaint(), () => window.storageClassesPaint?.(), () => window.addonsPaint?.()],
  fleet: [() => window.fleetSettingsPaint?.()],
  connections: [() => mqttPaint(), () => alertHookPaint(),
    // The UniFi card needs the IPAM record, which Settings does not otherwise load.
    () => unifiCardLoad()],
  access: [() => window.apiKeysPaint?.(), () => window.mcpCardPaint?.()],
  troubleshooting: [() => window.troubleshootingPaint?.()],
  you: [() => pwaPaint()],
};
/* Read the section's cards once per painted page: a section shown again
   without a repaint keeps what it read. */
function settingsLoad(section) {
  const loaded = STATE.settingsLoaded || (STATE.settingsLoaded = new Set());
  if (loaded.has(section)) return;
  loaded.add(section);
  for (const load of SETTINGS_LOADERS[section] || []) load();
}
window.settingsLoad = settingsLoad;

/* ---------------- Homestead's own redundancy ----------------
   More than one copy: a node failure leaves another already serving, and
   the leader lease (alerts, moves) moves within seconds. */
/* Homestead's own data (homestead_housekeeping.py): its volume, what each
   store holds and for how long, and Clean up now. */
const hdSize = bytes => bytesText(bytes);

function homesteadDataHtml(d) {
  const v = d.volume || {}, last = d.last;
  return `${UI.moduleHeader("Homestead's data", "What Homestead keeps on its volume, and for how long. Each store trims itself; once a day the logs, unused logos and leftovers from interrupted writes are tidied too.",
      can("admin") ? UI.button("Clean up now", "homesteadDataTidy()", { attrs: 'data-need="admin" id="hd_go"' }) : "")}
    ${v.pct != null ? `<div class="hd-volume"><div class="between"><span class="small"><b>${hdSize(v.used)}</b> used of ${hdSize(v.size)}</span><span class="dim xs">${esc(v.pct)}% · ${hdSize(v.free)} free</span></div>
      ${meter(v.pct)}${v.pct >= d.pressure_pct ? UI.callout("warn", `Over ${d.pressure_pct}% full`, "Homestead keeps half as much of its logs and drops unused logos at once. Make its data volume larger under Volumes.") : ""}</div>` : ""}
    <table class="tbl dense stack hd-table"><thead><tr><th>Store</th><th>Size</th><th>Kept</th></tr></thead><tbody>
      ${(d.stores || []).map(s => `<tr><td><b>${esc(s.label)}</b>${s.files > 1 ? `<span class="dim xs"> · ${s.files} files</span>` : ""}</td>
        <td data-label="Size" class="mono small">${s.files ? hdSize(s.size) : '<span class="dim">—</span>'}</td><td data-label="Kept" class="small dim">${esc(s.kept)}</td></tr>`).join("")}</tbody></table>
    ${last ? `<div class="dim xs" style="margin-top:8px">Last tidied ${esc(fmtAt(d.last_at))}: ${last.notes?.length ? esc(last.notes.join("; ")) + ` (${hdSize(last.freed)} freed)` : "nothing needed doing"}.</div>` : ""}`;
}

async function homesteadDataPaint() {
  const host = $("#homestead-data");
  if (!host) return;
  if (!can("admin")) { host.hidden = true; return; }      // sizes and housekeeping are an admin's
  try { const d = await api("/api/homestead/data"); host.innerHTML = homesteadDataHtml(d); }
  catch (e) { host.innerHTML = `${UI.moduleHeader("Homestead's data", "")}${UI.callout("warn", "It could not be read", esc(e.message))}`; }
}

window.homesteadDataTidy = async () => {
  const go = $("#hd_go");
  if (go) go.disabled = true;
  try {
    const r = await api("/api/homestead/data/tidy", { method: "POST", headers: { "Content-Type": "application/json" }, body: "{}" });
    toast(r.detail, "ok");
    const host = $("#homestead-data");
    if (host && r.report) host.innerHTML = homesteadDataHtml(r.report);
  } catch (e) { toast(e.message, "bad"); if (go) go.disabled = false; }
};

/* #731: Homestead's data has one copy and two hosts have room for another.
   Keep two copies makes longhorn-r2 if needed and opens the move with it
   picked; an existing longhorn-r2 that would not keep two shareable copies
   is named, and nothing is offered. */
function dataCopiesNote(copies) {
  // #620: a read that failed is said, with its reason, not left blank.
  if (copies?.state === "unknown") return `<div class="note" style="margin-top:10px">Could not read Homestead's data copies: ${esc(copies.error || "no reason given")}</div>`;
  if (copies?.state !== "can-keep-two") return "";
  const r2 = copies.r2 || {}, refused = r2.exists && !r2.usable, raise = copies.action === "raise";
  const how = raise ? ` Keep two copies raises it to two copies in place, on ${esc(copies.storage_class || "its class")}: no move and no outage.`
    : ` Keep two copies ${r2.exists ? "picks" : "makes"} longhorn-r2 (two copies on different hosts) and opens the move; Homestead stops briefly while its data is copied.`;
  return `<div class="note" style="margin-top:10px"><b>Homestead's data has one copy; this cluster can now keep two.</b>
    ${refused ? `${esc(r2.why)}. Pick a class that keeps two copies under Move data.`
      : `A second copy on another host keeps it if one host fails.${can("admin") ? how : ""}`}
    ${!refused && can("admin") ? `<div class="row" style="margin-top:6px"><button class="btn sm pri" id="rep_keep_two" onclick="replicasKeepTwo()">Keep two copies</button></div>` : ""}</div>`;
}
window.replicasKeepTwo = async () => {
  const go = $("#rep_keep_two");
  if (go) go.disabled = true;
  try {
    const r = await api("/api/self/data/two-copies", { method: "POST", headers: { "Content-Type": "application/json" }, body: "{}" });
    if (!r.ok) return toast(r.message, "warn");
    // Raised in place: nothing to move.
    if (r.raised) { toast(r.message, "ok"); return replicasPaint(); }
    await replicasMoveData("", r.storage_class);
  } catch (e) { toast(e.message, "bad"); }
  finally { if (go) go.disabled = false; }
};

async function replicasPaint() {
  let r;
  try { r = await api("/api/self/replicas"); }
  catch (e) {
    // Said, not the last answer kept as if current (#620).
    STATE.data.replicaHtml = "";
    const card = $("#replicaCard");
    if (card) card.innerHTML = UI.moduleHeader("Redundancy") + UI.callout("bad", "Could not read how many copies of Homestead run", esc(e.message))
      + UI.button("Try again", "replicasPaint()");
    return;
  }
  const ready = r.pods.filter(p => p.ready && !p.terminating).length;
  const note = r.desired > 1 && r.spread_nodes < Math.min(r.desired, ready)
    ? `<div class="note">${ready} copies are ready but only on ${r.spread_nodes} host${r.spread_nodes === 1 ? "" : "s"}: spreading is a preference, so the scheduler doubled up where it had to. They move apart as hosts free up.</div>`
    : r.desired === 1 ? `<div class="note">One copy: if its host fails, Homestead is away until Kubernetes starts it elsewhere - about a minute. Two copies on different hosts keep it answering.</div>` : "";
  STATE.data.replicaHtml = `${UI.moduleHeader(`Redundancy`, `How many copies of Homestead run. They share its data volume; one, the leader, raises alerts and advances moves, and another takes over within seconds if it stops.`, `${can("admin") ? `<div class="row"><select id="rep_count" aria-label="Copies of Homestead">${Array.from({ length: r.max }, (_, i) => i + 1).map(n =>
        `<option value="${n}" ${n === r.desired ? "selected" : ""} ${n > 1 && !r.data?.shareable && n !== r.desired ? "disabled" : ""}>${n} cop${n === 1 ? "y" : "ies"}</option>`).join("")}</select>
        <button class="btn sm pri" onclick="replicasSave()">Apply</button></div>` : ""}`)}
    ${r.data && !r.data.shareable ? `<div class="note ${r.desired > 1 ? "bad" : ""}" style="margin-top:10px"><b>More than one copy needs a volume every host can mount.</b> ${esc(r.data.reason)}.
      ${r.data.candidates.length ? "" : "<div>This cluster has no storage class that shares a volume between hosts; create one under Volumes, without live migration.</div>"}</div>` : ""}
    ${r.data?.pvc ? `<div class="row" style="margin-top:10px;flex-wrap:wrap;gap:8px"><span class="small">Its data: <span class="mono">${esc(r.data.pvc)}</span> on <b>${esc(r.data.storage_class || "?")}</b></span>
      ${(r.data.classes || []).length && can("admin") ? `<span class="small">· move to</span>
        <select id="rep_class" aria-label="Storage class for its data">${r.data.classes.map(c => `<option value="${esc(c.name)}" ${c.name === "longhorn" && !r.data.shareable ? "selected" : ""}>${esc(c.name)}${c.shareable ? " · every host" : " · one host"}</option>`).join("")}</select>
        <button class="btn sm" onclick="replicasMoveData()">Move data</button>` : ""}</div>
      ${dataCopiesNote(r.data_copies)}
      ${(r.data.kept || []).length ? `<div class="note" style="margin-top:8px">Other Homestead data volumes:
        ${r.data.kept.map(k => `<span class="mono">${esc(k)}</span>`).join(", ")}. These may be prepared destinations or recovery copies.
        Check their jobs before deleting anything.
        <div class="row" style="margin-top:6px">${r.data.kept.map(k => `<button class="btn sm" onclick="openOperation(${jsq("/volumes?find=" + k)})">Show ${esc(k)}</button>`).join("")}</div></div>` : ""}
      <div class="dim xs" style="margin-top:4px">Prepare a destination first, then review the move. Homestead must stop while its data is copied and verified.
        Both volumes are kept. Preparation alone does not move your data.</div>` : ""}
    <table class="tbl dense stack" style="margin-top:10px"><thead><tr><th>Copy</th><th>Host</th><th>State</th></tr></thead><tbody>
      ${r.pods.map(p => `<tr><td class="mono small">${esc(p.name)}${p.this ? ` ${UI.chip("this page", "neutral")}` : ""}</td>
        <td data-label="Node">${esc(p.node || "unscheduled")}</td>
        <td data-label="State">${p.terminating ? UI.chip("stopping", "neutral", { size: "slim" }) : p.ready ? UI.chip("ready", "ok", { size: "slim" }) : UI.chip("starting", "warn", { size: "slim" })}
          ${p.leader ? UI.chip("leader", "info", { size: "slim", tip: "Raises alerts and advances moves" }) : ""}</td></tr>`).join("")}</tbody></table>
    ${note}`;
  const host = $("#replicaCard");
  if (host) host.innerHTML = STATE.data.replicaHtml;
}
// Two explicit stages: prepare while online, then separately review downtime.
// Keep review tokens in this dialog only; never put them in links or local storage.
let selfDataDialog = null;
window.selfDataClose = () => { selfDataDialog = null; selfDataArchiveDialog = null; };
const selfDataPost = (path, body) => api(path, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
const selfDataId = () => Array.from(crypto.getRandomValues(new Uint8Array(12)), n => n.toString(16).padStart(2, "0")).join("");
const selfDataHostOptions = (nodes, selected = "") => nodes.map(n => `<option value="${esc(n.name)}" ${n.name === selected ? "selected" : ""} ${n.ready ? "" : "disabled"}>${esc(n.name)}${n.ready ? "" : " · unavailable"}</option>`).join("");
window.replicasMoveData = async (jobId = "", chosen = "") => {
  // The class picked beside the button is the one the dialog starts with,
  // unless the caller chose one (Keep two copies: longhorn-r2).
  const picked = chosen || $("#rep_class")?.value || "";
  selfDataArchiveDialog = null;
  const request = {}; selfDataDialog = request;
  modal("Move Homestead data", '<div id="selfDataFlow" class="ui-stack"><p>Reading volumes and hosts…</p></div>', true, "operation-review");
  try {
    const state = await api("/api/self/data/prepare");
    if (selfDataDialog !== request || !$("#selfDataFlow")) return;
    request.state = state;
    const jobs = state.preparations || [];
    request.jobs = jobs;
    const selected = jobs.findIndex(j => j.id === jobId);
    if (selected >= 0) return selfDataWatch(selected);
    $("#selfDataFlow").innerHTML = `${UI.lead(`Two steps: make the new volume (Homestead stays online), then copy to it (a short outage). <b>${esc(state.source)}</b> is kept either way.`)}
      ${jobs.length ? `<div class="ui-stack">${jobs.map((j, i) => `<div class="note"><b>${esc(j.destination)}</b><p class="small">${esc(j.message || j.status)}</p>
        <div class="row"><button class="btn sm" onclick="selfDataWatch(${i})">${j.prepared ? "Review move" : j.status === "succeeded" ? "View record" : "View progress"}</button>
        ${j.archivable ? `<button class="btn sm" data-need="admin" onclick="selfDataArchiveReview(${jsq(j.id)})">Archive record</button>` : ""}</div></div>`).join("")}</div>` : ""}
      ${state.blocking_jobs?.length ? UI.section("Jobs that block preparation", UI.table([{label:"Job"},{label:"State"}], state.blocking_jobs.map(j => [
        `<b>${esc(j.title)}</b><p class="small">${esc(j.message)}</p>${j.closable ? `<button class="btn sm pri" data-need="admin" onclick="selfDataCloseReview(${jsq(j.id)})">Close recovery</button>` : ""}<button class="btn sm" onclick="selfDataOpenJob(${jsq(j.id)})">${j.status === "failed" && j.mutation_recovery && j.kind === "k3s-cluster" ? "Review batch outcome" : "Open job"}</button><button class="btn sm" onclick="operationLog(${jsq(j.id)})">Log</button>`,
        `${esc(j.status)}${j.recovery ? " · requires recovery" : ""}`])) + '<p class="ui-help">Finish or review these jobs before preparing another volume. Archiving a completed preparation does not clear a running or recovery job.</p>') : ""}
      ${UI.fields(UI.field("Destination storage", `<select aria-label="Destination storage" id="selfDataClass" onchange="selfDataClassChanged()">${(state.classes || []).map(c =>
        `<option value="${esc(c.name)}" data-shareable="${c.shareable ? 1 : 0}" ${c.name === picked ? "selected" : ""}>${esc(c.name)} · ${c.shareable ? "every host can use it" : "lives on one host's disk"}</option>`).join("")}</select>`),
        `<div id="selfDataHostField">${UI.field("Which host's disk", `<select aria-label="Host whose disk holds the volume" id="selfDataHost" onchange="selfDataInvalidate()">${selfDataHostOptions(state.nodes || [])}</select>`,
          { help: "This storage keeps the volume on one host's own disk, so Homestead then runs only on that host." })}</div>`)}
      <div id="selfDataReview"></div><div id="selfDataActions">${UI.actions(UI.cancel("Close") + UI.button("Check", "selfDataPrepareReview()", { kind: "pri", id: "selfDataCheck", disabled: !(state.classes?.length && state.nodes?.some(n => n.ready)) || !!state.blocking_jobs?.length }))}</div>`;
    selfDataClassChanged();
  } catch (e) { if (selfDataDialog === request && $("#selfDataFlow")) $("#selfDataFlow").innerHTML = `<p role="alert">${esc(e.message)}</p>`; }
};
window.selfDataOpenJob = id => {
  const job = selfDataDialog?.state?.blocking_jobs?.find(item => item.id === id);
  if (!job) return;
  // A data move's own link is Settings - where this dialog already is - so
  // following it came straight back here. Its record is the job's log.
  if (job.kind === "self-data-handoff") return operationLog(job.id);
  selfDataClose();
  openOperation(job.href, job.id, job);
};

/* A cancelled move that kept both volumes: once Homestead runs on its
   original volume again, its record can be closed so another preparation
   can start. Both volumes and the audit stay. */
let selfDataCloseDialog = null;
window.selfDataCloseReview = async id => {
  const request = {}; selfDataCloseDialog = request;
  try {
    const review = await selfDataPost("/api/self/data/handoff/close/preview", {id});
    if (selfDataCloseDialog !== request) return;
    if (review.id !== id || !review.capacity_token) throw Error("The review is incomplete. Check saved jobs again.");
    request.review = review;
    childModal("Close data move recovery", UI.lead(esc(review.detail)) +
      UI.facts([["Homestead runs on", esc(review.source)], ["Kept, unused", esc(review.destination)]]) +
      UI.ack("selfDataCloseConsent", "Close this move's record; keep both volumes and its audit", {onchange:"selfDataCloseReady()"}) +
      UI.actions(UI.cancel("Back") + UI.button("Close recovery", "selfDataCloseStart()", {kind:"pri",id:"selfDataCloseGo",disabled:true})), true, "operation-review");
  } catch (e) {
    if (selfDataCloseDialog === request) childModal("Close data move recovery", `<p role="alert">${esc(e.message)}</p>` + UI.actions(UI.cancel("Back")), true, "operation-review");
  }
};
window.selfDataCloseReady = () => {
  const ready = !!(selfDataCloseDialog?.review && $("#selfDataCloseConsent")?.checked);
  if ($("#selfDataCloseGo")) $("#selfDataCloseGo").disabled = !ready;
  return ready;
};
window.selfDataCloseStart = async () => {
  if (!selfDataCloseReady()) return;
  const request = selfDataCloseDialog, saved = request.review;
  request.review = null; selfDataCloseReady();
  try {
    const result = await selfDataPost("/api/self/data/handoff/close", {id:saved.id,capacity_token:saved.capacity_token,confirm_close:true});
    toast(result.detail, "ok");
    window.refreshOperations?.(true);
    if (selfDataCloseDialog === request) { selfDataCloseDialog = null; await replicasMoveData(); }
  } catch (e) {
    if (selfDataCloseDialog === request) childModal("Close data move recovery", `<p role="alert">${esc(e.message)}</p><p>Nothing was changed. Both volumes are retained.</p>` +
      UI.actions(UI.cancel("Back")), true, "operation-review");
  }
};
/* The host only matters for storage that lives on one host's disk. */
window.selfDataClassChanged = () => {
  const option = $("#selfDataClass")?.selectedOptions?.[0];
  const field = $("#selfDataHostField");
  if (field) field.hidden = option?.dataset.shareable === "1";
  selfDataInvalidate();
};
window.selfDataInvalidate = () => {
  if (selfDataDialog) { selfDataDialog.review = null; selfDataDialog.request = null; }
  if ($("#selfDataReview")) $("#selfDataReview").innerHTML = "";
  if ($("#selfDataActions")) $("#selfDataActions").innerHTML = UI.actions(UI.cancel("Close") + UI.button("Check", "selfDataPrepareReview()", { kind: "pri", id: "selfDataCheck", disabled: !!selfDataDialog?.state?.blocking_jobs?.length }));
};
window.selfDataPrepareReview = async () => {
  const flow = selfDataDialog;
  if (!flow || flow.busy || flow.state?.blocking_jobs?.length) return;
  selfDataInvalidate();
  const body = { operation: selfDataId(), storage_class: $("#selfDataClass").value, node: $("#selfDataHost").value };
  flow.request = body;
  $("#selfDataReview").innerHTML = '<p>Checking…</p>';
  try {
    const review = await selfDataPost("/api/self/data/prepare/preview", body);
    if (selfDataDialog !== flow || flow.request !== body || !$("#selfDataReview")) return;
    if (!review.capacity_token || !review.capacity || review.capacity.blocked) throw new Error("Preparation could not be approved. Review the storage and host selection.");
    flow.review = { body, review };
    $("#selfDataReview").innerHTML = `${UI.facts([["New volume", `${esc(review.size)} on ${esc(review.storage_class)}`]])}
      ${UI.more("Capacity check", deployCapacityHtml(review.capacity))}
      ${UI.ack("selfDataConsent", "Make this volume", { onchange: "selfDataReady()" })}`;
    $("#selfDataActions").innerHTML = UI.actions(UI.cancel("Close") + UI.button("Prepare volume", "selfDataPrepare()", { kind: "pri", id: "selfDataPrepare", disabled: true }), UI.button("Check again", "selfDataPrepareReview()"));
  } catch (e) { if (selfDataDialog === flow && flow.request === body && $("#selfDataReview")) $("#selfDataReview").innerHTML = `<p role="alert">${esc(e.message)}</p>`; }
};
window.selfDataReady = () => {
  const flow = selfDataDialog, saved = flow?.review;
  const ready = !!(saved && !flow.busy && !flow.state?.blocking_jobs?.length && $("#selfDataConsent")?.checked && saved.body.storage_class === $("#selfDataClass")?.value && saved.body.node === $("#selfDataHost")?.value);
  if ($("#selfDataPrepare")) $("#selfDataPrepare").disabled = !ready;
  return ready;
};
window.selfDataPrepare = async () => {
  if (!selfDataReady()) return;
  const flow = selfDataDialog, saved = flow.review;
  flow.busy = true; flow.review = null; selfDataReady();
  try {
    const result = await selfDataPost("/api/self/data/prepare", { ...saved.body, capacity_token: saved.review.capacity_token, confirm_capacity: true });
    noteOperation(result.operation);
    if (selfDataDialog !== flow || !$("#selfDataFlow")) return;
    flow.jobs = [{ id: result.operation.id, operation: saved.body.operation, destination: result.destination, node: saved.body.node }];
    // Prepared: the move review that follows is a new step, not this one still running.
    flow.busy = false;
    await selfDataWatch(0);
  } catch (e) {
    // A lost response is not permission to resubmit. Reopen the saved jobs.
    if (selfDataDialog === flow && $("#selfDataFlow")) $("#selfDataFlow").innerHTML = `<p role="alert">${esc(e.message)}</p><p>Check saved jobs before trying again. A request may already have created a volume.</p>
      <button class="btn" onclick="replicasMoveData()">Check saved jobs</button>`;
  }
};
window.selfDataWatch = async index => {
  const flow = selfDataDialog, job = flow?.jobs?.[index];
  if (!job) return;
  const watch = {}; flow.watch = watch; flow.review = null;
  const paint = async () => {
    if (selfDataDialog !== flow || flow.watch !== watch || !$("#selfDataFlow")) return;
    try {
      const state = await api("/api/self/data/prepare");
      if (selfDataDialog !== flow || flow.watch !== watch || !$("#selfDataFlow")) return;
      const current = state.preparations.find(j => j.id === job.id);
      if (!current) throw new Error("The preparation record is unavailable. Inspect existing volumes before creating another.");
      flow.state = state; flow.selected = current;
      $("#selfDataFlow").innerHTML = `<p><b>${esc(current.destination)}</b></p><p role="status">${esc(current.message || current.status)}</p>
        ${current.prepared ? "" : UI.progress(current.progress, { label: "Preparation steps", kind: current.status === "failed" ? "bad" : "info" })}
        ${current.prepared ? UI.more("Hosts", UI.fields(
          UI.field("Move coordinator host", `<select aria-label="Move coordinator host" id="selfDataWorker" onchange="selfDataFinalInvalidate()">${selfDataHostOptions(state.nodes, current.node)}</select>`, { help: "Stays running while Homestead is stopped." }),
          UI.field("Copy host", `<select aria-label="Copy host" id="selfDataCopy" onchange="selfDataFinalInvalidate()">${selfDataHostOptions(state.nodes, current.node)}</select>`, { help: "Must be able to mount both volumes." }))) : ""}
        <div id="selfDataFinal"></div><div id="selfDataReviewActions">${UI.actions(UI.cancel("Close") + (current.prepared ? UI.button("Review the move", "selfDataFinalReview()", { kind: "pri" }) : UI.button("Back", "replicasMoveData()", { attrs: 'data-dialog-dismiss="true"' })) +
          (current.archivable ? UI.button("Archive record", `selfDataArchiveReview(${jsArg(current.id)})`, {attrs:'data-need="admin"'}) : ""))}</div>`;
      if (!["succeeded", "failed", "cancelled"].includes(current.status)) setTimeout(paint, 3000);
    } catch (e) { if (selfDataDialog === flow && flow.watch === watch && $("#selfDataFlow")) $("#selfDataFlow").innerHTML = `<p role="alert">${esc(e.message)}</p><button class="btn" onclick="replicasMoveData()">Check again</button>`; }
  };
  await paint();
};
let selfDataArchiveDialog = null;
window.selfDataArchiveReview = async id => {
  const request = {}; selfDataArchiveDialog = request;
  selfDataDialog = null;
  try {
    const review = await selfDataPost("/api/self/data/prepare/archive/preview", {id});
    if (selfDataArchiveDialog !== request) return;
    if (review.id !== id || !review.capacity_token || !review.destination) throw Error("The archive review is incomplete. Check saved jobs again.");
    request.review = review;
    modal("Archive preparation record", UI.lead(esc(review.detail)) +
      UI.facts([["Prepared volume", esc(review.destination)], ["Original volume", esc(review.source)]]) +
      UI.ack("selfDataArchiveConsent", "Hide this preparation and retain its volumes", {onchange:"selfDataArchiveReady()"}) +
      UI.actions(UI.cancel("Close") + UI.button("Archive record", "selfDataArchiveStart()", {kind:"pri",id:"selfDataArchiveGo",disabled:true})), true, "operation-review");
  } catch (e) { if (selfDataArchiveDialog === request) modal("Archive preparation record", `<p role="alert">${esc(e.message)}</p>` + UI.actions(UI.cancel("Close")), true, "operation-review"); }
};
window.selfDataArchiveReady = () => {
  const ready = !!(selfDataArchiveDialog?.review && $("#selfDataArchiveConsent")?.checked);
  if ($("#selfDataArchiveGo")) $("#selfDataArchiveGo").disabled = !ready;
  return ready;
};
window.selfDataArchiveStart = async () => {
  if (!selfDataArchiveReady()) return;
  const request = selfDataArchiveDialog, saved = request.review;
  request.review = null; selfDataArchiveReady();
  try {
    await selfDataPost("/api/self/data/prepare/archive", {id:saved.id,capacity_token:saved.capacity_token,confirm_archive:true});
    if (STATE.data.operations) STATE.data.operations = STATE.data.operations.filter(job => job.id !== saved.id);
    window.renderOperations?.();
    if (selfDataArchiveDialog === request) { selfDataArchiveDialog = null; await replicasMoveData(); }
  } catch (e) {
    if (selfDataArchiveDialog === request) modal("Archive preparation record", `<p role="alert">${esc(e.message)}</p><p>Check saved jobs before reviewing again. The volumes are retained.</p>` +
      UI.actions(UI.cancel("Close") + UI.button("Check saved jobs", "replicasMoveData()")), true, "operation-review");
  }
};
window.selfDataFinalInvalidate = () => {
  if (selfDataDialog) { selfDataDialog.finalRequest = null; selfDataDialog.approvedMove = null; }
  if ($("#selfDataFinal")) $("#selfDataFinal").innerHTML = "";
  if ($("#selfDataReviewActions")) $("#selfDataReviewActions").innerHTML = UI.actions(UI.cancel("Close") + UI.button("Review the move", "selfDataFinalReview()", { kind: "pri" }));
};
window.selfDataFinalReview = async () => {
  const flow = selfDataDialog;
  if (!flow?.selected?.prepared || !$("#selfDataFinal")) return;
  const body = { operation: flow.selected.operation, destination: flow.selected.destination, worker_node: $("#selfDataWorker").value, copy_node: $("#selfDataCopy").value };
  const request = {}; flow.finalRequest = request;
  flow.approvedMove = null;
  selfDataMoveReady();
  $("#selfDataFinal").innerHTML = "<p>Checking…</p>";
  try {
    const result = await selfDataPost("/api/self/data/move/preview", body);
    if (selfDataDialog !== flow || flow.finalRequest !== request || !$("#selfDataFinal")) return;
    if (body.worker_node !== $("#selfDataWorker").value || body.copy_node !== $("#selfDataCopy").value) throw new Error("The host selection changed. Review the move again.");
    if (!result.capacity_token || (result.stages || []).some(s => s.capacity?.blocked)) throw new Error("The move needs a new placement review.");
    flow.approvedMove = { body, result };
    $("#selfDataFinal").innerHTML = `${UI.callout("warn", "Homestead goes offline while its data is copied", "This page shows progress and comes back by itself. The old volume is kept.")}
      ${UI.more("Steps and capacity", (result.stages || []).map(s => `<p><b>${esc(s.label)}</b> - ${esc(s.detail)}</p>${deployCapacityHtml(s.capacity)}`).join(""))}
      ${UI.ack("selfDataMoveConsent", "Take Homestead offline and move its data", { onchange: "selfDataMoveReady()" })}`;
    if ($("#selfDataReviewActions")) $("#selfDataReviewActions").innerHTML = UI.actions(UI.cancel("Close") + UI.button("Move data", "selfDataMoveStart()", { kind: "pri", id: "selfDataMoveStart", disabled: true }));
  } catch (e) { if (selfDataDialog === flow && flow.finalRequest === request && $("#selfDataFinal")) $("#selfDataFinal").innerHTML = `<p role="alert">${esc(e.message)}</p>`; }
};
window.selfDataMoveReady = () => {
  const flow = selfDataDialog, saved = flow?.approvedMove;
  const ready = !!(saved && !flow.busy && $("#selfDataMoveConsent")?.checked &&
    saved.body.worker_node === $("#selfDataWorker")?.value && saved.body.copy_node === $("#selfDataCopy")?.value);
  if ($("#selfDataMoveStart")) $("#selfDataMoveStart").disabled = !ready;
  return ready;
};
window.selfDataMoveStart = async () => {
  if (!selfDataMoveReady()) return;
  const flow = selfDataDialog, saved = flow.approvedMove;
  flow.busy = true; flow.approvedMove = null; selfDataMoveReady();
  try {
    const result = await selfDataPost("/api/self/data/move", { ...saved.body, capacity_token: saved.result.capacity_token, confirm_move: true, confirm_capacity: true });
    noteOperation(result.operation);
    if (!/^[a-f0-9]{24}$/.test(result.handoff || "")) throw new Error("The move response could not be verified.");
    location.assign(`/api/self/data/handoff/${result.handoff}/view`);
  } catch (e) {
    if (selfDataDialog === flow && $("#selfDataFinal")) $("#selfDataFinal").innerHTML = `<p role="alert">${esc(e.message)}</p><p>Check the saved move in Jobs before trying again. It may already have started.</p>`;
  }
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
  catch (e) { host.innerHTML = UI.error(e, { retry: "namespacesPaint()" }); return; }
  const what = namespaceHolds;
  host.innerHTML = `<div class="tblwrap"><table class="tbl stack dense" data-sort="namespaces"><thead><tr>
      <th>Namespace</th><th>Holds</th><th data-nosort>Created</th><th></th></tr></thead><tbody>
    ${inv.namespaces.map(r => `<tr><td data-sort="${esc(r.name)}"><b class="mono">${esc(r.name)}</b>
        ${r.name === inv.default ? UI.chip("default for new apps", "ok") : ""}${r.homestead ? ` ${UI.chip("made here", "neutral")}` : ""}</td>
      <td class="small">${esc(what(r))}</td>
      <td class="dim xs">${r.created ? esc(fmtDay(r.created)) : ""}</td>
      <td class="right">${can("admin") ? (r.protected ? `<span class="dim xs ns-kept">Kept: ${esc(r.protected)}</span>`
        : actionBar([{ label: "Delete", run: `namespaceDelete(${jsq(r.name)})`, danger: true, disabled: !r.empty,
            tip: r.empty ? "" : r.unread?.length ? "Homestead could not check everything in it; refresh to try again"
              : "Move or delete what it holds first" }])) : ""}</td></tr>`).join("")}</tbody></table></div>
    <div class="dim xs" style="margin-top:8px">${inv.system_hidden} platform namespace${inv.system_hidden === 1 ? "" : "s"} hidden.</div>
    ${UI.more("Changing the default namespace", `<p class="small">New apps go in <b class="mono">${esc(inv.default)}</b> unless you pick another in Deploy. The default was chosen when Homestead was installed - <span class="mono">workloadNamespace.name</span> in the Helm chart, <span class="mono">DEFAULT_NS</span> in the manifest - and Homestead keeps its own settings, network shares and node probe there too, so it is not changed from here: moving them on a running cluster would leave them behind. To use another namespace for your apps, create it above and pick it in Deploy.</p>`)}`;
  sortTables(host);
}

/* What a namespace holds, every kind its delete checks; what could not be
   read is said as such, never as "empty" (#599). */
const NS_KINDS = [["deployments", "app", "apps"], ["statefulsets", "stateful set", "stateful sets"],
  ["daemonsets", "daemon set", "daemon sets"], ["cronjobs", "scheduled job", "scheduled jobs"],
  ["pods", "running pod", "running pods"], ["volumes", "volume", "volumes"], ["vms", "VM", "VMs"],
  ["disks", "VM disk", "VM disks"]];
window.namespaceHolds = r => {
  const held = NS_KINDS.filter(([key]) => r[key]).map(([key, one, many]) => `${r[key]} ${r[key] === 1 ? one : many}`);
  if (r.unread?.length) held.push(`could not check ${r.unread.join(", ")}`);
  return held.join(" · ") || "empty";
};

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

window.namespaceDelete = name => modal(`Delete namespace · ${name}`, `${UI.lead(`Deletes the empty namespace <b>${esc(name)}</b>. This can't be undone.`)}
  ${UI.typedConfirm({ id: "ns_delete_confirm", expect: name, button: "ns_delete_go" })}
  ${UI.actions(UI.cancel() + `<button class="btn danger" id="ns_delete_go" disabled onclick="namespaceDeleteGo(${jsq(name)})">Delete namespace</button>`)}`);
window.namespaceDeleteGo = async name => {
  if (!typedConfirmMatches("ns_delete_confirm")) return;
  try {
    await api("/api/namespaces/delete", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name, confirm: $("#ns_delete_confirm").value.trim() }) });
    toast(`${name} deleted`, "ok");
    closeModal(); namespacesPaint();
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

/* Every settings save goes through here: the settings as they are now, read
   fresh (never a copy kept since the page opened, nor the defaults that stand
   in for a failed read), with only the sections this save changes. It names
   the revision it read, so the server refuses it (409, nothing saved) if
   anything else saved in between, rather than undo that; such a refusal is
   tried once more on a fresh read, as it is mostly the server's own short
   cache that was behind.
   change is an object, or a function of the current settings returning one;
   a failed read saves nothing (#601). */
window.settingsWrite = async (change, retry = true) => {
  let current;
  try { current = await api("/api/settings", { keep: true }); }
  catch (e) { throw new Error(`The settings could not be read, so nothing was saved: ${e.message}`); }
  if (!current || typeof current !== "object") throw new Error("The settings could not be read, so nothing was saved");
  const fresh = { ...current };
  delete fresh.info;
  const body = { ...fresh, ...(typeof change === "function" ? change(fresh) : change), revision: current.revision };
  let saved;
  try { saved = await api("/api/settings", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) }); }
  catch (e) { if (e.status === 409 && retry) return settingsWrite(change, false); throw e; }
  STATE.data.appSettings = { ...saved, info: current.info };
  return saved;
};

window.permissionsRecheck = async () => {
  try {
    const result = await api("/api/self/permissions", { method: "POST", headers: { "Content-Type": "application/json" }, body: "{}" });
    toast(PERMISSION_WORDS[result.state] || result.state, result.state === "manual" ? "bad" : "ok");
    STATE.data.appSettings = null;
    await loadHealthSettings(true);
    viewSettings();
  } catch (e) { toast(e.message, "bad"); }
};


const probeWord = state => ({ updated: "updated with this release", current: "up to date",
  absent: "not installed", error: "could not be checked" })[state] || "unknown";


window.saveCatalog = async reset => {
  const url = reset ? "" : $("#set_catalog").value.trim();
  try {
    await settingsWrite({ catalog_url: url });
    STATE.data.appSettings = null;
    await loadHealthSettings(true);
    toast(url ? "App Store now reads that catalogue" : "App Store reads the Community Applications feed again", "ok");
    viewSettings();
    return true;
  } catch (e) { toast(e.message, "bad"); return false; }
};

/* ---------------- the save bar ----------------
   A section saves once, from its bar: every field on it that changed. The
   app's own settings go in one request, so saving two cards never sends the
   older copy of one of them. */
function settingsDirtyPaint() {
  const bar = $("#settingsSaveBar"), n = STATE.settingsDirty?.size || 0;
  if (!bar) return;
  bar.hidden = !n;
  const msg = $("#settingsSaveMsg");
  if (msg) msg.textContent = `${n} unsaved change${n === 1 ? "" : "s"}`;
}
if (typeof document !== "undefined" && document.addEventListener) {
  const mark = event => {
    const card = event.target?.closest?.(".settings-grid section[data-save]");
    if (!card || !event.target.id && !event.target.className) return;
    STATE.settingsDirty = STATE.settingsDirty || new Set();
    STATE.settingsDirty.add(`${card.dataset.save}:${event.target.id || event.target.className}:${event.target.value}`.split(":").slice(0, 2).join(":"));
    settingsDirtyPaint();
  };
  document.addEventListener("input", mark);
  document.addEventListener("change", mark);
  window.addEventListener?.("beforeunload", event => { if (STATE.settingsDirty?.size) { event.preventDefault(); event.returnValue = ""; } });
}
const settingsKinds = () => new Set([...(STATE.settingsDirty || [])].map(key => key.split(":")[0]));

/* Each host as if it failed: a line per host, the details in its preview. */
async function capacityForecastLoad() {
  const host = $("#capacityForecast");
  if (!host) return;
  let all;
  try { all = await api("/api/capacity/forecast"); }
  catch (e) { host.innerHTML = UI.callout("warn", "Could not work it out", esc(e.message)); return; }
  if (!$("#capacityForecast")) return;
  const rows = (all.hosts || []).map(f => {
    const [tone, word] = capacityVerdict(f.verdict);
    return `<tr><td><b class="mono">${esc(f.node)}</b> ${UI.chip(word, tone, { size: "slim" })}</td>
      <td class="small">${esc(f.sentence)}</td>
      <td data-actions>${actionBar([{ label: "Details", run: `nodeIfDown(${jsq(f.node)})`, ariaLabel: `What happens if ${f.node} fails` }])}</td></tr>`;
  });
  const notes = [
    all.unreserved ? `${all.unreserved} app${all.unreserved === 1 ? " reserves" : "s reserve"} nothing and fit${all.unreserved === 1 ? "s" : ""} anywhere on paper, so the figures are a floor.${can("operator")
      ? ` ${UI.button("Set reservations from use…", "reservationsReview()", { kind: "sm" })}` : ""}` : "",
    all.pending ? `${all.pending} app${all.pending === 1 ? " is" : "s are"} already waiting for room, whichever host fails.` : "",
  ].filter(Boolean);
  host.innerHTML = rows.length ? `<div class="tblwrap"><table class="tbl stack dense"><thead><tr><th>Host</th><th>If it fails</th><th></th></tr></thead><tbody>${rows.join("")}</tbody></table></div>`
    + notes.map(n => `<p class="ui-help">${n}</p>`).join("")
    : '<div class="empty small">No host is ready.</div>';
}

/* What each way of sharing the work does, with what it depends on here. */
function placeModeText(mode, placement) {
  if (mode === "standby") {
    // The taint only steers new pods: what runs on a host chosen now stays
    // until Balance hosts moves it - said, so ticking a host is not taken
    // for emptying it.
    const pending = (placement?.hosts || []).filter(h => h.standby && !h.tainted);
    return `The standby hosts take apps only when the others cannot. Apps already on one stay there until Balance hosts moves them - off standby hosts first, and never onto them.${can("operator")
      ? ` ${UI.button("Balance hosts…", "balanceHosts()", { kind: "sm" })}` : ""}${pending.length
      ? ` <span class="badtext">${esc(pending.map(h => h.name).join(", "))} ${pending.length === 1 ? "is" : "are"} not marked standby yet${pending.some(h => h.error) ? `: ${esc(pending.find(h => h.error).error)}` : ""}; Homestead tries again every minute.</span>` : ""}`;
  }
  if (mode === "spread") {
    if (!placement && STATE.data.placementUnread) return `Kubernetes places each app on the host with most room left, by what apps reserve. <span class="badtext">Could not read what the apps reserve: ${esc(STATE.data.placementUnread)}</span>`;
    const n = placement?.unreserved || 0;
    return `Kubernetes places each app on the host with most room left, by what apps reserve.${n
      ? ` ${n} app${n === 1 ? " reserves" : "s reserve"} nothing and ${n === 1 ? "is" : "are"} placed as if ${n === 1 ? "it uses" : "they use"} nothing.${can("operator")
        ? ` ${UI.button("Set reservations from use…", "reservationsReview()", { kind: "sm" })}` : ""}` : " Every app reserves something."}`;
  }
  return mode ? "Kubernetes places apps as it does by default; placement is set app by app." : "Not chosen yet: Kubernetes places apps as it does by default.";
}
window.placeModeChanged = () => {
  const mode = $("#set_place_mode").value;
  $("#set_standby_row").hidden = mode !== "standby";
  $("#set_place_text").innerHTML = placeModeText(mode, STATE.data.placement);
  // Spreading or a spare is kept only by moving apps back after a host
  // returns; offer that, visibly, rather than leaving it off unseen.
  const back = $("#set_rebalance");
  if ((mode === "spread" || mode === "standby") && back?.value === "off") {
    back.value = "suggest";
    back.dispatchEvent(new Event("change", { bubbles: true }));
  }
};

/* What the chosen mode does, in the window's own words. */
function rebalanceModeText(mode, maintenance) {
  return mode === "suggest" ? "An alert says how many containers would move back; nothing moves until someone reviews it."
    : mode === "window" ? `The same alert, then the moves run by themselves in the update window (${esc(updateWindowText(maintenance))}), apart from apps whose scheduled job runs then.`
    : "Nothing moves back on its own. Balance hosts on Hosts, or Rebalance containers in Containers' menu, moves them when you choose.";
}

async function settingsAppSave() {
  const saved = await settingsWrite(current => settingsChanges(current));
  HEALTH = { thresholds: { ...HEALTH_DEFAULTS.thresholds, ...(saved.thresholds || {}) } };
  STATE.data.appSettings = null;
  await loadHealthSettings(true);
  if (saved.updates) loadImageUpdates(false, true);
}

/* Which part of the settings a field of the save bar's form belongs to. */
const SETTINGS_PARTS = [["thresholds", /^set_(cpu|memory|disk|temperature)_(warn|crit)$/],
  ["smart", /^set_(drive_temperature_|smart_)/], ["updates", /^set_(update_|notify_)/],
  ["site_name", /^set_site_name$/], ["placement", /^set_(rebalance|place_mode|standby)/]];
const settingsParts = () => new Set([...(STATE.settingsDirty || [])].filter(key => key.startsWith("app:"))
  .flatMap(key => SETTINGS_PARTS.filter(([, field]) => key.slice(4).split(" ").some(name => field.test(name))).map(([part]) => part)));

/* What the save bar changes, on top of the current settings: only the parts
   whose fields were changed, so a part shown as the page opened is not saved
   back over a change made since (#601). */
function settingsChanges(current) {
  const parts = settingsParts(), body = {};
  const read = id => ({ warning: +$("#set_" + id + "_warn").value, critical: +$("#set_" + id + "_crit").value });
  if (parts.has("thresholds") && $("#set_cpu_warn")) body.thresholds = { cpu: read("cpu"), memory: read("memory"), disk: read("disk"), temperature: read("temperature") };
  if (parts.has("smart") && $("#set_drive_temperature_warn")) body.smart = { temperature: read("drive_temperature"), reallocated_warning: +$("#set_smart_reallocated").value,
    pending_critical: +$("#set_smart_pending").value, uncorrectable_critical: +$("#set_smart_uncorrectable").value, notify_failures: $("#set_smart_notify").checked };
  if (parts.has("updates") && $("#set_update_policy")) body.updates = { ...(current.updates || {}), policy: $("#set_update_policy").value,
    notify_available: $("#set_notify_available").checked, notify_failures: $("#set_notify_failures").checked,
    maintenance: $("#set_update_start") ? { start: $("#set_update_start").value, duration_minutes: +$("#set_update_duration").value,
      days: $$(".set_update_day:checked").map(input => +input.value) } : (current.updates || {}).maintenance };
  if (parts.has("site_name") && $("#set_site_name")) body.site_name = $("#set_site_name").value.trim();
  if (parts.has("placement") && $("#set_rebalance")) body.placement = { ...(current.placement || {}), rebalance: $("#set_rebalance").value,
    ...($("#set_place_mode") ? { mode: $("#set_place_mode").value, standby_nodes: $$(".set_standby:checked").map(input => input.value) } : {}) };
  if (body.placement?.mode === "standby" && !body.placement.standby_nodes.length) {
    settingsTab("placement");
    throw new Error("Tick at least one standby host, or choose another way to share the work");
  }
  return body;
}

window.settingsSave = async button => {
  const kinds = settingsKinds();
  if (button) button.disabled = true;
  try {
    // A failed read leaves the defaults on the form: saving them would replace what is stored.
    if (kinds.has("app") && STATE.data.appSettings === HEALTH_DEFAULTS) throw new Error("The settings could not be read when this page opened, so nothing was saved. Reload and try again.");
    if (kinds.has("app")) await settingsAppSave();
    if (kinds.has("lh")) await lhSettingsSave();
    STATE.settingsDirty = new Set();
    toast("Settings saved", "ok");
    resetPaint(); viewSettings();
  } catch (e) { toast(e.message, "bad"); }
  finally { if (button?.isConnected) button.disabled = false; }
};
window.settingsDiscard = () => { STATE.settingsDirty = new Set(); resetPaint(); viewSettings(); };

/* Changing section, or leaving Settings, with changes not saved asks first. */
window.settingsLeave = async () => {
  if (!STATE.settingsDirty?.size) return true;
  if (!(await ask("Leave without saving? The changes on this section are discarded.", { title: "Leave without saving?", ok: "Discard changes", danger: true }))) return false;
  STATE.settingsDirty = new Set();
  return true;
};
window.settingsGo = async id => {
  if (!(await settingsLeave())) return;
  if (!id) {
    // A deep link must not reopen the section after returning to the list.
    const url = new URL(location.href);url.searchParams.delete("tab");
    history.replaceState(history.state,"",url);
    settingsBack();resetPaint();await viewSettings();
    $(".settings-nav button.on")?.focus({preventScroll:true});return;
  }
  const url = new URL(location.href);
  if (url.searchParams.has("tab")) {url.searchParams.set("tab",settingsSection(id));history.replaceState(history.state,"",url);}
  settingsTab(id, false);  // the repaint below reads the section's data
  resetPaint(); return viewSettings();
};

/* ---------------- dialogs for settings with many fields ---------------- */
const DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];
/* "Weekdays", "Tue–Sun", "Mon, Wed, Fri": the days at a glance. */
function updateWindowDays(days) {
  const on = [...new Set(days || [])].filter(d => d >= 0 && d <= 6).sort((a, b) => a - b);
  if (!on.length) return "No days";
  if (on.length === 7) return "Every day";
  if (on.join() === "0,1,2,3,4") return "Weekdays";
  if (on.join() === "5,6") return "Weekends";
  const run = on.every((d, i) => !i || d === on[i - 1] + 1);
  return run && on.length > 2 ? `${DAYS[on[0]]}–${DAYS[on[on.length - 1]]}` : on.map(d => DAYS[d]).join(", ");
}

/* "Weekdays · 02:00–04:00 UTC (03:00 your time)": its end, not its length,
   and the local time beside UTC when they differ. */
function updateWindowText(m, now = new Date()) {
  const start = /^\d\d:\d\d$/.test(m?.start || "") ? m.start : "02:00";
  const [h, min] = start.split(":").map(Number), length = +(m?.duration_minutes || 120);
  const endAt = (h * 60 + min + length) % 1440, pad = n => String(n).padStart(2, "0");
  const end = `${pad(Math.floor(endAt / 60))}:${pad(endAt % 60)}`;
  const local = new Date(Date.UTC(now.getUTCFullYear(), now.getUTCMonth(), now.getUTCDate(), h, min));
  const yours = `${pad(local.getHours())}:${pad(local.getMinutes())}`;
  return `${updateWindowDays(m?.days)} · ${start}–${end} UTC${yours === start ? "" : ` (${yours} your time)`}`;
}
window.updateWindowEdit = () => {
  const m = (STATE.data.appSettings?.updates || {}).maintenance || {};
  modal("Update window", UI.lead("When a reviewed rollout may start, under the maintenance-window policy. Times are UTC.") +
    UI.fields(UI.field("Start time (UTC)", `<input id="set_update_start" type="time" value="${esc(m.start || "02:00")}">`),
      UI.field("Duration (minutes)", `<input id="set_update_duration" type="number" min="15" max="1440" step="15" value="${+(m.duration_minutes || 120)}">`)) +
    UI.field("Days", `<div class="update-days">${DAYS.map((day, index) =>
      `<label class="daypick"><input type="checkbox" class="set_update_day" value="${index}" ${(m.days || []).includes(index) ? "checked" : ""}><span>${day}</span></label>`).join("")}</div>`) +
    UI.actions(UI.cancel() + UI.button("Save window", "updateWindowSave(this)", { kind: "pri" })));
};
/* The window saves on its own: the stored settings with only the window
   changed, so a change still waiting on the save bar stays there, unsaved. */
window.updateWindowSave = async button => {
  if (button) button.disabled = true;
  const maintenance = { start: $("#set_update_start").value, duration_minutes: +$("#set_update_duration").value,
    days: $$(".set_update_day:checked").map(input => +input.value) };
  try {
    const saved = await settingsWrite(current => ({ updates: { ...(current.updates || {}), maintenance } }));
    // The row changes in place: re-rendering would put the unsaved fields back.
    const row = $("#set_update_window");
    if (row) row.textContent = updateWindowText((saved.updates || {}).maintenance || maintenance);
    closeModal(); toast("Update window saved", "ok");
    loadImageUpdates(false, true);
  } catch (e) { toast(e.message, "bad"); if (button) button.disabled = false; }
};
window.catalogEdit = () => modal("App Store catalogue", UI.lead("Any feed in the Community Applications format: the public one, a mirror, or your own list of templates.") +
  UI.field("Feed URL",
    `<input id="set_catalog" type="url" maxlength="500" placeholder="blank: the public Community Applications feed" value="${esc(STATE.data.appSettings?.catalog_url || "")}">`,
    { tipHtml: " " + tip("A JSON feed shaped like applicationFeed.json - an object with an applist, or a plain list of templates. Blank uses the public feed.") }) +
  UI.actions(UI.cancel() + UI.button("Save", "catalogSave(false)", { kind: "pri" }), UI.button("Use Community Applications", "catalogSave(true)")));
// A failed save keeps the dialog open, with what was typed in it.
window.catalogSave = async reset => { if (await saveCatalog(reset)) closeModal(); };

window.updatePolicyFields = () => {
  const fields = $("#maintenanceFields");
  if (fields) fields.classList.toggle("muted-policy", $("#set_update_policy").value !== "maintenance_window");
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
    // Settings reads the hosts again itself, and says so if it cannot.
    if (STATE.view === "settings") viewSettings();
  } catch (e) { toast(e.message, "bad"); }
  finally { if (button) { button.disabled = false; button.textContent = "Rescan hosts"; } }
};

/* ---------------- Homestead's own health ----------------
   Whether the parts that work in the background are working: the API it
   leans on, each loop, the node probe and Samba. Add-on controls live in Cluster. */
const HEALTH_TONE = { ok: "ok", standby: "neutral", starting: "neutral", late: "warn", failing: "bad" };
const agoText = t => !t ? "not yet" : Date.now() / 1000 - t < 60 ? "just now" : fmtAgo(Math.round(Date.now() / 1000 - t));

/* The card is read again every 15 seconds while Settings shows its section,
   a failed read included: the next one may answer. On another section it
   only waits, since coming back does not load the section again; leaving
   Settings stops it, and Settings starts it again when it paints. */
function selfHealthLater() {
  clearTimeout(window.__selfHealthTimer);
  window.__selfHealthTimer = setTimeout(() => {
    if (STATE.view !== "settings") return;
    if (settingsTab() === settingsSection("about")) selfHealthPaint(); else selfHealthLater();
  }, 15000);
}

async function selfHealthPaint() {
  const host = $("#selfHealthCard");
  if (!host) return;
  let h;
  try { h = await api("/api/self/health"); }
  catch (e) {
    host.innerHTML = UI.moduleHeader("Homestead's health") + UI.callout("bad", "Status unavailable", esc(e.message));
    return selfHealthLater();
  }
  const admin = can("admin"), probe = h.probe || {}, samba = h.samba || {}, pods = h.replicas?.pods || [];
  const metricsOn = !!STATE.data.appSettings?.metrics?.enabled;
  const auditFile = STATE.data.appSettings?.audit?.file !== false;
  STATE.data.sambaInstalled = !!samba.installed;
  const row = (label, tone, word, detail = "", action = "") => `<div class="health-row">${UI.chip(word, tone)}
    <div><b>${esc(label)}</b>${detail ? `<div class="dim xs">${detail}</div>` : ""}</div>${action ? `<div class="row">${action}</div>` : ""}</div>`;
  const problems = h.loops.filter(l => ["failing", "late"].includes(l.state)).length + (h.api.ok ? 0 : 1)
    + (probe.installed && probe.ready < probe.desired ? 1 : 0) + (samba.enabled && samba.ready < samba.desired ? 1 : 0);
  host.innerHTML = `${UI.moduleHeader(`Homestead's health`, `What works in the background, checked every 15 seconds while this is open`, UI.chip(problems ? `${problems} to look at` : "all well", problems ? "warn" : "ok"))}
    <div class="health-list">
      ${row("Kubernetes API", h.api.ok ? (h.api.ms > 2000 ? "warn" : "ok") : "bad", h.api.ok ? `${h.api.ms} ms` : "no answer",
        h.api.ok ? (h.api.ms > 2000 ? "Slow to answer: pages and actions wait on it." : "Answering promptly.") : esc(h.api.error || ""))}
      ${row("Homestead", "ok", `v${h.version}`, `${pods.filter(p => p.ready).length} of ${h.replicas?.desired ?? "?"} cop${(h.replicas?.desired ?? 1) === 1 ? "y" : "ies"} ready`
        + (pods.length ? ` · ${pods.map(p => `${esc(p.node.replace("harvester-", ""))}${p.leader ? " (leader)" : ""}${p.this ? " - this one" : ""}`).join(", ")}` : "")
        + (h.leader ? "" : " · this copy is standing by"))}
      ${h.loops.map(l => row(l.label, HEALTH_TONE[l.state] || "", l.state === "ok" ? "running" : l.state,
        l.state === "standby" ? "Runs on the leader copy." : `Last done ${esc(agoText(l.last_ok))}` + (l.error ? ` · <span class="badtext">${esc(l.error)}</span>` : ""))).join("")}
      ${row("Node probe", !probe.installed ? "neutral" : probe.ready < probe.desired || probe.reporting < probe.desired ? "warn" : "ok",
        !probe.installed ? "not installed" : `${probe.ready}/${probe.desired} hosts`,
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
      ${row("Audit log", auditFile ? "ok" : "neutral", auditFile ? "kept" : "pod log only",
        auditFile ? "Every change is written to the pod log and to audit.jsonl on the data volume (two files of 10 MiB at most)."
          : "Every change is written to the pod log only.",
        admin ? `<button class="btn sm" onclick="auditFileSet(${!auditFile})">${auditFile ? "Pod log only" : "Keep on the data volume"}</button>` : "")}
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
      ${row("Metrics for Prometheus", metricsOn ? "ok" : "neutral", metricsOn ? "on" : "off",
        metricsOn ? `<span class="mono">${esc(location.origin)}/metrics</span>, read with an API key as a bearer token.`
          : "When on, Prometheus or an uptime checker reads Homestead's own health from /metrics with an API key.",
        admin ? `<button class="btn sm" onclick="metricsSet(${!metricsOn})">${metricsOn ? "Turn off" : "Turn on"}</button>` : "")}
    </div>`;
  selfHealthLater();
}
window.selfHealthPaint = selfHealthPaint;

/* /metrics (#736): takes effect at once, like the MCP switch, rather than
   waiting for Save. Only this setting changes (#601). Turning it on asks
   first, as the MCP switch does: every API key there is can then read it.
   Turning it off only narrows who can read, so it acts at once. */
window.metricsSet = async enabled => {
  if (enabled && !(await ask("Every API key there is, whatever it was made for, can then read Homestead's own health from /metrics: "
    + "counts of hosts, apps, jobs and alerts, the last backup's time and Homestead's version, never names. "
    + "Turn it on for Prometheus or an uptime checker.",
    { title: "Turn on metrics", ok: "Turn on metrics" }))) return;
  try {
    await settingsWrite({ metrics: { enabled } });
    STATE.data.appSettings = null;
    await loadHealthSettings(true);
    toast(enabled ? "Metrics are on at /metrics" : "Metrics are off", "ok");
  } catch (e) { toast(e.message, "bad"); }
  selfHealthPaint();
};

/* The SMB and NFS servers are actions, not settings: Enable or Disable acts
   at once, so they are buttons, not switches beside fields (#511). */
window.sambaSet = async on => {
  if (on && !STATE.data.sambaInstalled) {
    if (nodeAddressesOnly()) return sambaInstallGo("");
    let choices;
    try { choices = await vipChoices(); }
    catch (e) { return modal("Install Samba", UI.error(e, { title: "Could not offer an address", retry: "sambaSet(true)", dialog: true })); }
    const own = (choices.own || []).find(v => v.free);
    return modal("Install Samba", `<p class="small">Samba serves the network shares. Choose the address Windows will find it at.</p>
      <div class="f">${vipPicker("smb", own ? own.ip : (choices.free[0] || ""), choices)}</div>
      ${UI.actions(`<button class="btn pri" onclick="sambaInstallGo()">Install</button><button data-dialog-dismiss="true" class="btn" onclick="closeModal()">Cancel</button>`)}`);
  }
  if (!on && !(await ask("Stop Samba? Every share stops being served until it is switched on again; their volumes, settings and passwords are kept.", { title: "Stop Samba", ok: "Stop Samba", danger: true }))) return;
  try {
    const r = await api("/api/self/samba", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ enabled: on }) });
    toast(r.detail, "ok");
    setTimeout(() => { addonsPaint(); selfHealthPaint(); }, 1500);
  } catch (e) { toast(e.message, "bad"); }
};

/* The audit log (#738): always in the pod log; this keeps it on the data
   volume too. Takes effect at once, and only this setting changes (#601). */
window.auditFileSet = async file => {
  try {
    await settingsWrite({ audit: { file } });
    STATE.data.appSettings = null;
    await loadHealthSettings(true);
    toast(file ? "The audit log is kept on the data volume" : "The audit log goes to the pod log only", "ok");
  } catch (e) { toast(e.message, "bad"); }
  selfHealthPaint();
};

window.sambaInstallGo = async (selectedAddress = null) => {
  const address = selectedAddress === null ? ($("#smb_lb_ip")?.value || "").trim() : selectedAddress;
  if (!address && !nodeAddressesOnly()) return toast("Choose an address - add VIPs under Networking if the list is empty", "bad");
  try {
    const r = await api("/api/self/samba", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ enabled: true, address }) });
    toast(r.detail, "ok"); closeModal(); setTimeout(() => { addonsPaint(); selfHealthPaint(); }, 1500);
  } catch (e) { toast(e.message, "bad"); }
};

window.sambaRemove = () => {
  modal("Remove SMB server", `<p>The SMB address and its server will be removed. Network shares stop being served.</p>
    <div class="note warn">Share definitions, passwords, all PVCs and their data remain. Re-enable SMB here to serve them again.</div>
    ${UI.typedConfirm({ id: "smb_remove_confirm", expect: "homestead-smb", button: "smb_remove_go" })}
    ${UI.actions(UI.cancel() + `<button class="btn danger" id="smb_remove_go" disabled onclick="sambaRemoveGo()">Remove SMB server</button>`)}`);
};
window.sambaRemoveGo = async () => {
  if (!typedConfirmMatches("smb_remove_confirm")) return;
  const confirm = $("#smb_remove_confirm").value.trim();
  try {
    const r = await api("/api/addons/smb/remove", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ confirm }) });
    closeModal(); toast(r.detail, "ok"); addonsPaint(); selfHealthPaint();
  } catch (e) { toast(e.message, "bad"); }
};

window.nfsSet = async on => {
  const nfs = STATE.data.nfs || {};
  if (on && !nfs.installed) {
    if (!(nfs.exports || []).length) return modal("Set up NFS exports", `<p>Choose an RWX share and its allowed client IP or CIDR in Network Shares before installing the NFS server.</p>
      ${UI.actions(`<button class="btn pri" onclick="closeModal();go('shares')">Network Shares</button><button data-dialog-dismiss="true" class="btn" onclick="closeModal()">Cancel</button>`)}`);
    if (nodeAddressesOnly()) return modal("NFS needs a VIP", `<p>k3s ServiceLB cannot provide the dedicated address and preserved client IPs needed for this NFSv4 server. Install kube-vip under Cluster Add-ons, then try again.</p>
      ${UI.actions(`<button class="btn pri" onclick="closeModal();addonsPaint()">OK</button>`)}`);
    let choices;
    try { choices = await vipChoices(); }
    catch (e) { return modal("Install NFSv4 server", UI.error(e, { title: "Could not offer an address", retry: "nfsSet(true)", dialog: true })); }
    choices.used = [];
    choices.own = (choices.own || []).filter(v => v.free);
    const own = (choices.own || []).find(v => v.free);
    return modal("Install NFSv4 server", `<p>A separate NFS container will serve ${plural((nfs.exports || []).length, "selected RWX share")} on TCP port 2049. File access pauses if its host fails, until the replacement server and storage are available.</p>
      ${nfsRecovery(nfs, true)}
      <div class="note warn">Only the client networks configured for each share can mount it. Removing this server later leaves every share definition and PVC in place.</div>
      <p class="small">SMB and NFS use different ports, but their separately placed servers need separate VIPs to keep client IP restrictions and routing correct after a host failure. Sharing one VIP would require a combined file-server pod.</p>
      <div class="f">${vipPicker("nfs", own ? own.ip : (choices.free[0] || ""), choices)}</div>
      ${UI.actions(`<button class="btn pri" onclick="nfsInstallGo()">Install NFS server</button><button data-dialog-dismiss="true" class="btn" onclick="closeModal()">Cancel</button>`)}`);
  }
  if (!on && !(await ask("Stop NFS? Exported shares become unavailable until it is switched on again. Their definitions and volumes are kept.", { title: "Stop NFS", ok: "Stop NFS", danger: true }))) return;
  try {
    const result = await api("/api/self/nfs", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ enabled: on }) });
    toast(result.detail, "ok"); setTimeout(() => { addonsPaint(); if (STATE.view === "shares") viewShares(); }, 1500);
  } catch (e) { toast(e.message, "bad"); }
};
window.nfsInstallGo = async () => {
  const address = ($("#nfs_lb_ip")?.value || "").trim();
  if (!address) return toast("Choose a dedicated VIP for NFS", "bad");
  try {
    const result = await api("/api/self/nfs", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ enabled: true, address }) });
    closeModal(); toast(result.detail, "ok"); setTimeout(() => addonsPaint(), 1500);
  } catch (e) { toast(e.message, "bad"); }
};
window.nfsRemove = () => modal("Remove NFS server", `<p>The NFS container and its address will be removed. Clients will lose access to its exports.</p>
  <div class="note warn">Export settings, SMB shares, all PVCs and their data remain.</div>
  ${UI.typedConfirm({ id: "nfs_remove_confirm", expect: "homestead-nfs", button: "nfs_remove_go" })}
  ${UI.actions(UI.cancel() + `<button class="btn danger" id="nfs_remove_go" disabled onclick="nfsRemoveGo()">Remove NFS server</button>`)}`);
window.nfsRemoveGo = async () => {
  if (!typedConfirmMatches("nfs_remove_confirm")) return;
  const confirm = $("#nfs_remove_confirm").value.trim();
  try {
    const result = await api("/api/addons/nfs/remove", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ confirm }) });
    closeModal(); toast(result.detail, "ok"); addonsPaint();
  } catch (e) { toast(e.message, "bad"); }
};
