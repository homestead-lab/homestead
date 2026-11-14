/* Full settings workspace: cluster policy, hardware, access, and installation info. */

const ROLE_COPY = {
  viewer: "Read-only access to dashboards, apps and VMs, storage, events, and settings.",
  operator: "Everything a viewer can do, and also deploy, edit, move, start and stop apps and VMs.",
  admin: "Full access, including users, hosts, hardware mappings, imports, and cluster policy.",
};
// What each role adds, in a word or two, beside its name where it is chosen (#350).
const ROLE_SHORT = { viewer: "can view but not change anything", operator: "can run apps and VMs", admin: "can do everything, including users and hosts" };
const ROLE_NAMES = { viewer: "Viewer", operator: "Operator", admin: "Admin" };
// The full comparison, for the rare time it is needed.
const ROLE_TABLE = [
  ["View dashboards, apps, VMs, storage, events and settings", 1, 1, 1],
  ["Deploy, edit, move, start, stop and update apps and VMs", 0, 1, 1],
  ["Snapshots, backups, restores and schedules", 0, 1, 1],
  ["Users, roles and API keys", 0, 0, 1],
  ["Manage hosts: maintenance, restarts, shell access, disks and hardware", 0, 0, 1],
  ["Manage network shares, imports and cluster settings", 0, 0, 1],
];
const USERS_FILTER = { role: "" };


/* Settings › About's cluster VIP: the default workload VIP, else the
   platform's own. lb_ip null means Your VIPs couldn't be read: unknown,
   never the platform address standing in for it. */
function clusterVipText(overview, info, hostsUnread) {
  if (!hostsUnread && overview.lb_ip === null) return "unknown";
  return (hostsUnread ? "" : overview.lb_ip) || info.vip || "—";
}

function userRow(u, admin, count) {
  const me = u.name === ME;
  const role = !admin ? `<span class="${roleClass(u.role)} rolechip">${esc(ROLE_NAMES[u.role] || u.role)}</span>`
    : me ? `<span class="${roleClass(u.role)} rolechip">${esc(ROLE_NAMES[u.role] || u.role)}</span><div class="dim xs">An account cannot change its own role</div>`
    : `<select class="role-select" aria-label="Role for ${esc(u.name)}" onchange="userRoleSet(${jsq(u.name)},this)" data-was="${esc(u.role)}">
        ${["viewer", "operator", "admin"].map(r => `<option value="${r}"${u.role === r ? " selected" : ""}>${ROLE_NAMES[r]} - ${esc(ROLE_SHORT[r])}</option>`).join("")}</select>`;
  const actions = admin ? actionBar([
    { label: "Sign-in history", run: `userSignins(${jsq(u.name)})` },
    !me && count > 1 && { label: "Remove", run: `userRemove(${jsq(u.name)})`, danger: true }], { shown: 0, label: `More actions for ${u.name}` }) : "";
  return `<tr data-role="${esc(u.role)}"${USERS_FILTER.role && USERS_FILTER.role !== u.role ? " hidden" : ""}><td><b>${esc(u.name)}</b>${me ? ` ${UI.chip("this account", "ok")}` : ""}</td>
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
  ${UI.lead("Each role can do everything the role before it can, and more.")}
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
  ["you", "Personal", "Appearance, this device, account", ["you", "device", "general"], "This account only", "user"],
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
  const hostsNote = !hostsUnread ? "" : nodes ? UI.callout("warn", "The hosts could not be read", `${esc(hostsUnread)} The hardware shown for each host is from the last reading.`)
    : UI.callout("bad", "The hosts could not be read", `${esc(hostsUnread)} Which hosts have each device is not known.`);
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
      ${UI.settingsCard(`${UI.moduleHeader(`Appearance`, `Applies to this browser only, and changes immediately`, ``)}
        <div class="srows">
          ${settingRow("Theme", "", `<div class="seg appearance-seg" id="optTheme"><button type="button" class="opt" data-v="dark">Dark</button><button type="button" class="opt" data-v="light">Light</button><button type="button" class="opt" data-v="auto">Auto</button></div>`)}
          ${settingRow("Background", "The image behind the panels", `<div class="seg appearance-seg" id="optBg"><button type="button" class="opt" data-v="soft">Soft</button><button type="button" class="opt" data-v="gold">Gold</button><button type="button" class="opt" data-v="plain">Plain</button></div>`)}
          ${settingRow("Glass blur", "How frosted the panels are", `<input type="range" class="rng" id="optBlur" min="0" max="60" step="2"><span class="dim small mono"><span id="blurVal"></span>px</span>`)}
          ${settingRow("Motion", "Animation along the connecting lines in diagrams", `<div class="seg appearance-seg" id="optMotion"><button type="button" class="opt" data-v="on">On</button><button type="button" class="opt" data-v="off">Reduced</button></div>`)}
          ${settingRow("Refresh every", "How often pages update their information", `<input type="range" class="rng" id="optRefresh" min="5" max="60" step="5"><span class="dim small mono"><span id="refreshVal"></span>s</span>`)}
        </div>`, {tab:`you`})}
      ${UI.settingsCard(`${UI.moduleHeader(`Your account`, `Signed in as ${esc(ME || "—")} · ${esc(ROLE || "viewer")}`, ``)}
        <div class="srows">
          ${settingRow("Password", "", '<button class="btn sm" onclick="pwChange()">Change</button>')}
          ${settingRow("Sign out everywhere", "Ends every session for this account, on every device", '<button class="btn sm" onclick="signOutEverywhere()">Sign out everywhere</button>')}
          ${settingRow("Sign out", "This browser", '<button class="btn sm" onclick="doLogout()">Sign out</button>')}
        </div><div class="dim xs" style="margin-top:8px">${sessionSummary(AUTH_STATE)}</div>`, {tab:`you`})}
      ${UI.settingsCard(`
        ${UI.moduleHeader(`Health thresholds`, `When bars and host cards turn amber and red, for all users ${tip("Warning turns a bar amber, and critical turns it red. A host shows its worst result across CPU, memory, disk and temperature.")}`, `${can("admin") ? "" : UI.chip("admin managed", "neutral")}`)}
        <div class="threshold-grid">
          ${thresholdEditor("cpu", "CPU use", "%", "Sustained CPU use on a host", thresholds.cpu)}
          ${thresholdEditor("memory", "Memory use", "%", "Memory in use on a host", thresholds.memory)}
          ${thresholdEditor("disk", "Host disk use", "%", "Space used on the host's own disk", thresholds.disk)}
          ${thresholdEditor("temperature", "CPU temperature", "°C", "CPU temperature on a host", thresholds.temperature)}
        </div>
      `, {tab:`health`, save:`app`})}

      ${UI.settingsCard(`
        ${UI.moduleHeader(`Drive health`, `Drive warnings shown on host cards and in cluster health`, `${can("admin") ? "" : UI.chip("admin managed", "neutral")}`)}
        <div class="threshold-grid">
          ${thresholdEditor("drive_temperature", "Drive temperature", "°C", "Temperature of SATA, SAS and NVMe drives", smart.temperature)}
          <div class="threshold-card counters"><div><b>Media counters</b> ${tip("NVMe drives report media errors. ATA drives report reallocated, pending and uncorrectable sectors. A counter a drive does not report is shown as unsupported, not as zero.")}<div class="dim xs">Recommended: 1 for each counter, so the first bad sector is reported.</div></div>
            <div class="smart-threshold-values">
              <label>Reallocated · warning ${tip("Sectors the drive has already replaced with spares. A count that does not change may be old. Look into any increase and check that backups work.")} <input id="set_smart_reallocated" type="number" min="1" max="1000000" value="${smart.reallocated_warning}" ${can("admin") ? "" : "disabled"}></label>
              <label>Pending · critical ${tip("Sectors the drive cannot read reliably at the moment. Check backups and look into any count above zero.")} <input id="set_smart_pending" type="number" min="1" max="1000000" value="${smart.pending_critical}" ${can("admin") ? "" : "disabled"}></label>
              <label>Uncorrectable · critical ${tip("Sectors a drive scan could not recover. Check backups and drive test results. Some drives keep old counts.")} <input id="set_smart_uncorrectable" type="number" min="1" max="1000000" value="${smart.uncorrectable_critical}" ${can("admin") ? "" : "disabled"}></label>
            </div></div>
        </div>
        <div class="srows" style="margin-top:14px">${settingRow("Include SMART failures and threshold breaches in cluster health notifications", "",
          `<label class="toggle"><input id="set_smart_notify" type="checkbox" ${smart.notify_failures !== false ? "checked" : ""} ${can("admin") ? "" : "disabled"}><span></span></label>`)}</div>
      `, {tab:`health`, save:`app`})}

      ${UI.settingsCard(`
        ${UI.moduleHeader(`Container updates`, `When Homestead checks for newer images, and when a reviewed update may start`, `${can("admin") ? "" : UI.chip("admin managed", "neutral")}`)}
        <div class="srows">
          ${settingRow(`Policy ${tip("Notify only installs nothing. Approval required allows an update after it is reviewed. Maintenance window allows reviewed updates only during the window. Major releases are never chosen automatically, and every update is reviewed.")}`, "",
            `<select id="set_update_policy" ${can("admin") ? "" : "disabled"}>
              <option value="notify_only" ${updates.policy === "notify_only" ? "selected" : ""}>Notify only, install nothing</option>
              <option value="approval_required" ${updates.policy === "approval_required" ? "selected" : ""}>Approval required, update when approved</option>
              <option value="maintenance_window" ${updates.policy === "maintenance_window" ? "selected" : ""}>Maintenance window, update when approved in the window</option>
            </select>`)}
          ${settingRow("Notify about new images", "", `<label class="toggle"><input id="set_notify_available" type="checkbox" ${updates.notify_available !== false ? "checked" : ""} ${can("admin") ? "" : "disabled"}><span></span></label>`)}
          ${settingRow("Notify when an image check fails", "", `<label class="toggle"><input id="set_notify_failures" type="checkbox" ${updates.notify_failures !== false ? "checked" : ""} ${can("admin") ? "" : "disabled"}><span></span></label>`)}
          ${settingRow("Update window", `<span id="set_update_window">${esc(updateWindowText(maintenance))}</span>` + (updates.policy === "maintenance_window" ? ""
            : `<div class="dim xs">${updates.policy === "notify_only" ? "No image is installed while the policy is Notify only, including for apps set to update themselves. Moving apps back after a host returns still uses this window, if that is turned on."
              : "Approved updates start immediately under this policy. Only apps set to update themselves wait for this window."}</div>`),
            can("admin") ? '<button class="btn sm" onclick="updateWindowEdit()">Change</button>' : "")}
        </div>
      `, {tab:`updates`, save:`app`})}

      ${UI.settingsCard(`
        ${UI.moduleHeader(`How hosts share the work`, `One setting for the whole cluster, instead of placement set for each app`, `${can("admin") ? "" : UI.chip("admin managed", "neutral")}`)}
        <div class="srows">
          ${settingRow(`Share the work ${tip("Hot spare keeps the chosen hosts free while other hosts have room, and uses them when none has. Apps are not changed, so it also works for apps managed by Flux, Argo CD or Helm. (Kubernetes: a homestead.io/standby taint with PreferNoSchedule.)")}`,
            `<div class="dim xs" id="set_place_text">${placeModeText(placeMode, placement)}</div>`,
            `<select id="set_place_mode" aria-label="Share the work" aria-describedby="set_place_text" ${can("admin") ? "" : "disabled"} onchange="placeModeChanged()">
              ${placeMode ? "" : '<option value="" selected>Not chosen yet</option>'}
              ${[["default", "Kubernetes default"], ["spread", "Spread by capacity"], ["standby", "Hot spare"]].map(([v, l]) =>
                `<option value="${v}" ${placeMode === v ? "selected" : ""}>${l}</option>`).join("")}
            </select>`)}
          <div id="set_standby_row" ${placeMode === "standby" ? "" : "hidden"}>
            ${settingRow(`Standby hosts ${tip("These hosts take apps only when the other hosts cannot. A host is never made a standby if no other host can take apps.")}`,
              STATE.data.placementUnread ? `<span class="badtext">The hosts could not be read. ${esc(STATE.data.placementUnread)}</span>` : (placement?.notes || []).map(esc).join("; "),
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
        ${UI.moduleHeader(`If a host fails`, `Whether the other hosts have room for its apps, based on what the apps reserve and their priority`, ``)}
        <div id="capacityForecast"><div class="empty small"><span class="spin2"></span> Checking each host…</div></div>
      `, {tab:`placement`})}

      ${UI.settingsCard(`
        ${UI.moduleHeader(`When a host comes back`, `Apps move to other hosts while a host is down or in maintenance, and stay there when it returns`, `${can("admin") ? "" : UI.chip("admin managed", "neutral")}`)}
        <div class="srows">
          ${settingRow(`Move apps back ${tip("Uses Balance hosts, which makes only moves that reduce the load on the busiest host enough. Each move restarts the app once. In the update window or automatically, an app with a scheduled job, such as a backup, before the window closes is left where it is, because the job needs its volume on that host.")}`,
            `<div class="dim xs" id="set_rebalance_text" aria-live="polite">${rebalanceModeText(rebalanceMode, maintenance, rebalancePlacement())}</div>
             <div id="set_rebalance_status"></div>`,
            `<select id="set_rebalance" aria-label="Move apps back" aria-describedby="set_rebalance_text" ${can("admin") ? "" : "disabled"}
              onchange="$('#set_rebalance_text').innerHTML=rebalanceModeText(this.value,(STATE.data.appSettings?.updates||{}).maintenance||{},rebalancePlacement())">
              <option value="off" ${rebalanceMode === "off" ? "selected" : ""}>No - leave them where they are</option>
              <option value="suggest" ${rebalanceMode === "suggest" ? "selected" : ""}>Suggest - an alert to review the moves</option>
              <option value="window" ${rebalanceMode === "window" ? "selected" : ""}>In the update window</option>
              <option value="auto" ${rebalanceMode === "auto" ? "selected" : ""}>Automatically</option>
            </select>`)}
        </div>
      `, {tab:`placement`, save:`app`})}

      ${UI.settingsCard(`
        ${UI.moduleHeader(`Hardware features`, `Devices apps can use, and which hosts have them`, `${can("operator") ? '<button class="btn sm" onclick="hardwareRescan(this)" title="Look for devices plugged in since the last check">Rescan hosts</button>' : ""}
          ${can("admin") ? '<button class="btn sm" onclick="hardwareFeatureSettings()">Manage</button>' : ""}`)}
        <div class="dim xs" style="margin:-4px 0 8px">Hosts are checked every 30 seconds, so a device plugged in later is found without a restart.</div>
        ${hostsNote}<div class="settings-list">${hardwareRows || '<div class="empty small">No hardware features are set up.</div>'}</div>
      `, {tab:`hardware`, wide:false})}

      ${UI.settingsCard(`
        ${UI.moduleHeader(`Users and roles`, `Who can sign in, and what each user can do`, `<button class="btn sm" onclick="rolesCompare()">Compare roles</button>${can("admin") ? '<button class="btn sm pri" onclick="userAdd()">＋ Add user</button>' : ""}`)}
        ${usersUnread ? UI.callout("bad", "The users could not be read", esc(usersUnread)) : ""}
        ${userList.length > 1 ? usersFilterChips(userList) : ""}
        ${userList.length ? `<div class="tblwrap"><table class="tbl stack dense" id="usersTable"><thead><tr><th>User</th><th>Role</th><th>Last sign-in</th>${can("admin") ? "<th></th>" : ""}</tr></thead><tbody>${userRows}</tbody></table></div>` : ""}
      `, {tab:`access`, wide:false})}
      ${can("admin") ? UI.settingsCard('<div class="empty small"><span class="spin2"></span>reading keys</div>', {tab:"access",id:"apiKeysCard",wide:false}) : ""}
      ${can("admin") ? UI.settingsCard('<div class="empty small"><span class="spin2"></span>reading agent settings</div>', {tab:"access",id:"mcpCard",wide:false}) : ""}

      ${pwaCard()}
      ${Timeline.summaryCard()}

      ${ipamUnifiCard()}

      ${UI.settingsCard(``, {tab:`mqtt`, id:`mqttCard`})}
      ${UI.settingsCard(``, {tab:`connections`, id:`alertHookCard`})}
      ${UI.settingsCard(`<div class="empty small"><span class="spin2"></span> asking each cluster</div>`, {tab:`fleet`, id:`fleetCard`})}
      ${UI.settingsCard(``, {tab:`fleet`, id:`fleetMovesCard`, hidden:true})}
      ${UI.settingsCard(``, {tab:`cluster`, id:`addonsCard`, hidden:true})}
      ${UI.settingsCard(`<div class="empty small"><span class="spin2"></span>reading Longhorn</div>`, {tab:`cluster`, id:`lhSettingsCard`, save:`lh`})}
      ${UI.settingsCard(``, {tab:`cluster`, id:`storageClassesCard`, hidden:true})}

      ${UI.settingsCard(`
        ${UI.moduleHeader(`App Store catalogue`, `The feed the App Store lists apps from. Any feed in the Community Applications format works.`, ``)}
        ${serviceRow(STATE.data.appSettings?.catalog_url ? "Your own feed" : "Community Applications", UI.chip("in use", "ok"),
          esc(STATE.data.appSettings?.catalog_url || "The public feed"), can("admin") ? actionBar([{ label: "Change", run: "catalogEdit()" }]) : "")}
      `, {tab:`connections`})}

      ${UI.settingsCard(`
        ${UI.moduleHeader(`Namespaces`, `The namespaces apps can be deployed into. Harvester, Rancher and Kubernetes have their own namespaces, which are hidden here and in every list.`, `${can("admin") ? `<div class="row ns-new"><input id="nsName" aria-label="New namespace" placeholder="new-namespace" maxlength="63" autocomplete="off"
            onkeydown="if(event.key==='Enter')namespaceCreate()"><button class="btn sm pri" onclick="namespaceCreate()">Create</button></div>` : ""}`)}
        <div class="ns-body"><div class="empty small"><span class="spin2"></span></div></div>
      `, {tab:`namespaces`, id:`nsCard`})}

      ${UI.settingsCard(`
        ${UI.moduleHeader(`About this installation`, `The version, components and name of this installation`, ``)}
        <div class="srows">${settingRow(`Site name ${tip("Shown under the Homestead name and at the bottom of the page. Leave it empty to show nothing.")}`, "",
          `<input type="text" id="set_site_name" maxlength="40" placeholder="e.g. Main site" value="${esc(STATE.data.appSettings?.site_name || "")}" ${can("admin") ? "" : "disabled"}>`)}</div>
        <div class="about-grid">
          <div><span>Homestead</span><b>v${esc(info.version || HOMESTEAD_VERSION)}</b></div>
          <div><span>Kubernetes</span><b>${esc(info.kubernetes || "—")}</b></div>
          <div><span>Default namespace</span><b class="mono">${esc(info.namespace || "lab")}</b></div>
          <div><span>Storage class</span><b class="mono">${esc(info.storage_class || "—")}</b></div>
          <div><span>Cluster VIP</span><b class="mono">${esc(clusterVipText(overview, info, hostsUnread))}</b></div>
          <div><span>Hosts ready</span><b>${hostsUnread ? `<span class="dim" title="${esc(hostsUnread)}">not read</span>` : `${overview.nodes_ready}/${overview.nodes_total}`}</b></div>
          <div><span>Node probe ${tip("Homestead updates the probe together with its own release, so no manual step is needed. It is installed and removed under Cluster → Add-ons, because its drive-health part needs extra permissions.")}</span>
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
  if (settings === HEALTH_DEFAULTS) toast("The settings could not be read, so the defaults are shown and cannot be saved. Reload to try again.", "bad");
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
  placement: [() => capacityForecastLoad(), () => rebalanceStatusLoad()],
  hardware: [() => namespacesPaint(), () => lhSettingsPaint(), () => window.storageClassesPaint?.(), () => window.addonsPaint?.()],
  fleet: [() => window.fleetSettingsPaint?.()],
  connections: [() => mqttPaint(), () => alertHookPaint(),
    // The UniFi card needs the IPAM record, which Settings does not otherwise load.
    () => unifiCardLoad()],
  access: [() => window.apiKeysPaint?.(), () => window.mcpCardPaint?.()],
  troubleshooting: [() => window.troubleshootingPaint?.()],
  you: [() => pwaPaint(), () => Timeline.summaryPaint()],
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
  return `${UI.moduleHeader("Homestead's data", "What Homestead keeps on its volume, and for how long. Each kind of data is trimmed automatically. Once a day, logs, unused logos and leftover files are also cleaned up.",
      can("admin") ? UI.button("Clean up now", "homesteadDataTidy()", { attrs: 'data-need="admin" id="hd_go"' }) : "")}
    ${v.pct != null ? `<div class="hd-volume"><div class="between"><span class="small"><b>${hdSize(v.used)}</b> used of ${hdSize(v.size)}</span><span class="dim xs">${esc(v.pct)}% · ${hdSize(v.free)} free</span></div>
      ${meter(v.pct)}${v.pct >= d.pressure_pct ? UI.callout("warn", `Over ${d.pressure_pct}% full`, "Homestead now keeps half as many logs and removes unused logos immediately. To avoid this, make its data volume larger under Volumes.") : ""}</div>` : ""}
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
  catch (e) { host.innerHTML = `${UI.moduleHeader("Homestead's data", "")}${UI.callout("warn", "This information could not be read", esc(e.message))}`; }
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
  if (copies?.state === "unknown") return `<div class="note" style="margin-top:10px">The copies of Homestead's data could not be read. ${esc(copies.error || "No reason was given.")}</div>`;
  if (copies?.state !== "can-keep-two") return "";
  const r2 = copies.r2 || {}, refused = r2.exists && !r2.usable, raise = copies.action === "raise";
  const how = raise ? ` Keep two copies adds a second copy on ${esc(copies.storage_class || "its storage class")}, without moving the data or stopping Homestead.`
    : ` Keep two copies ${r2.exists ? "uses" : "creates"} the storage class longhorn-r2, which keeps two copies on different hosts, and opens the move. Homestead stops briefly while its data is copied.`;
  return `<div class="note" style="margin-top:10px"><b>Homestead's data has one copy, and this cluster can now keep two.</b>
    ${refused ? `${esc(r2.why)}. Choose a storage class that keeps two copies under Move data.`
      : `A second copy on another host keeps the data available if one host fails.${can("admin") ? how : ""}`}
    ${!refused && can("admin") ? `<div class="row" style="margin-top:6px"><button class="btn sm pri" id="rep_keep_two" onclick="replicasKeepTwo()">Keep two copies</button></div>` : ""}</div>`;
}
/* Design 3.1: what makes Homestead's data fragile after an unclean restart, and
   the reviewed move that fixes it. A copy on a disk that takes no new copies is
   the salvage alert's, not this note's. */
function placementNote(placement) {
  if (placement?.error) return `<div class="note" style="margin-top:10px">Where Homestead's data is stored could not be read. ${esc(placement.error)}</div>`;
  return (placement?.findings || []).map(f => f.kind === "shared"
    ? `<div class="note" style="margin-top:10px"><b>Homestead's data is a shared volume, which only several copies of Homestead need.</b>
      A shared volume is served by a share-manager pod, which an unclean restart can leave down, and Homestead with it. A volume that one host mounts at a time does without it.
      ${can("admin") ? `<div class="row" style="margin-top:6px"><button class="btn sm" id="rep_one_host" onclick="replicasMoveData('', ${jsq(placement.storage_class || "")}, true)">Use a volume one host mounts</button></div>` : ""}</div>`
    : `<div class="note" style="margin-top:10px"><b>Homestead's data is not on the system disk.</b>
      Its only copy is on ${esc(f.path || "a second disk")} on ${esc(f.node || "its host")}, so a problem with that disk stops Homestead even while the host is fine.
      Move the copy to the disk tagged os with Move copies on Volumes. Homestead keeps running while it moves.
      <div class="row" style="margin-top:6px"><button class="btn sm" onclick="openOperation(${jsq("/volumes?find=" + (placement.claim || ""))})">Show on Volumes</button></div></div>`).join("");
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
    if (card) card.innerHTML = UI.moduleHeader("Redundancy") + UI.callout("bad", "The number of Homestead copies could not be read", esc(e.message))
      + UI.button("Try again", "replicasPaint()");
    return;
  }
  const ready = r.pods.filter(p => p.ready && !p.terminating).length;
  const note = r.desired > 1 && r.spread_nodes < Math.min(r.desired, ready)
    ? `<div class="note">${ready} copies are ready, but on only ${r.spread_nodes} host${r.spread_nodes === 1 ? "" : "s"}. Spreading them out is a preference, so some share a host when there is no room elsewhere. They move apart when other hosts have room.</div>`
    : r.desired === 1 ? `<div class="note">One copy. If its host fails, Homestead is unavailable for about a minute while it restarts on another host. Two copies on different hosts keep it available.</div>` : "";
  STATE.data.replicaHtml = `${UI.moduleHeader(`Redundancy`, `How many copies of Homestead run. They share its data volume. One copy, the leader, sends alerts and runs moves, and another takes over within seconds if it stops.`, `${can("admin") ? `<div class="row"><select id="rep_count" aria-label="Copies of Homestead">${Array.from({ length: r.max }, (_, i) => i + 1).map(n =>
        `<option value="${n}" ${n === r.desired ? "selected" : ""} ${n > 1 && !r.data?.shareable && n !== r.desired ? "disabled" : ""}>${n} cop${n === 1 ? "y" : "ies"}</option>`).join("")}</select>
        <button class="btn sm pri" onclick="replicasSave()">Apply</button></div>` : ""}`)}
    ${r.data && !r.data.shareable ? `${UI.callout(r.desired > 1 ? "bad" : "", { html: `More than one copy needs a volume that every host can use.` }, `${esc(r.data.reason)}.
      ${r.data.candidates.length ? "" : "<div>This cluster has no storage class that shares a volume between hosts. Create one under Volumes, without live migration.</div>"}`, { attrs: 'style="margin-top:10px"' })}` : ""}
    ${r.data?.pvc ? `<div class="row" style="margin-top:10px;flex-wrap:wrap;gap:8px"><span class="small">Its data: <span class="mono">${esc(r.data.pvc)}</span> on <b>${esc(r.data.storage_class || "?")}</b></span>
      ${(r.data.classes || []).length && can("admin") ? `<span class="small">· move to</span>
        <select id="rep_class" aria-label="Storage class for its data">${r.data.classes.map(c => `<option value="${esc(c.name)}" ${c.name === "longhorn" && !r.data.shareable ? "selected" : ""}>${esc(c.name)}${c.shareable ? " · shared by all hosts" : " · one host"}</option>`).join("")}</select>
        <button class="btn sm" onclick="replicasMoveData()">Move data</button>` : ""}</div>
      ${dataCopiesNote(r.data_copies)}
      ${placementNote(r.placement)}
      ${(r.data.kept || []).length ? `<div class="note" style="margin-top:8px">Other Homestead data volumes:
        ${r.data.kept.map(k => `<span class="mono">${esc(k)}</span>`).join(", ")}. These may be prepared destinations or recovery copies.
        Check their jobs before deleting anything.
        <div class="row" style="margin-top:6px">${r.data.kept.map(k => `<button class="btn sm" onclick="openOperation(${jsq("/volumes?find=" + k)})">Show ${esc(k)}</button>`).join("")}</div></div>` : ""}
      <div class="dim xs" style="margin-top:4px">Prepare a destination first, then review the move. Homestead stops while its data is copied and checked.
        Both volumes are kept. Preparing a destination does not move the data.</div>` : ""}
    <table class="tbl dense stack" style="margin-top:10px"><thead><tr><th>Copy</th><th>Host</th><th>State</th></tr></thead><tbody>
      ${r.pods.map(p => `<tr><td class="mono small">${esc(p.name)}${p.this ? ` ${UI.chip("this page", "neutral")}` : ""}</td>
        <td data-label="Node">${esc(p.node || "unscheduled")}</td>
        <td data-label="State">${p.terminating ? UI.chip("stopping", "neutral", { size: "slim" }) : p.ready ? UI.chip("ready", "ok", { size: "slim" }) : UI.chip("starting", "warn", { size: "slim" })}
          ${p.leader ? UI.chip("leader", "info", { size: "slim", tip: "Sends alerts and runs moves." }) : ""}</td></tr>`).join("")}</tbody></table>
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
window.replicasMoveData = async (jobId = "", chosen = "", oneHost = false) => {
  // The class picked beside the button is the one the dialog starts with,
  // unless the caller chose one (Keep two copies: longhorn-r2; Use a volume
  // one host mounts: the class it is on now, one host at a time).
  const picked = chosen || $("#rep_class")?.value || "";
  selfDataArchiveDialog = null;
  const request = { oneHost }; selfDataDialog = request;
  modal("Move Homestead data", '<div id="selfDataFlow" class="ui-stack"><p>Reading the volumes and hosts…</p></div>', true, "operation-review");
  try {
    const state = await api("/api/self/data/prepare");
    if (selfDataDialog !== request || !$("#selfDataFlow")) return;
    request.state = state;
    const jobs = state.preparations || [];
    request.jobs = jobs;
    const selected = jobs.findIndex(j => j.id === jobId);
    if (selected >= 0) return selfDataWatch(selected);
    $("#selfDataFlow").innerHTML = `${UI.lead(`There are two steps. First the new volume is created while Homestead keeps running. Then the data is copied to it, which stops Homestead for a short time. <b>${esc(state.source)}</b> is kept in both cases.`)}
      ${jobs.length ? `<div class="ui-stack">${jobs.map((j, i) => `<div class="note"><b>${esc(j.destination)}</b><p class="small">${esc(j.message || j.status)}</p>
        <div class="row"><button class="btn sm" onclick="selfDataWatch(${i})">${j.prepared ? "Review move" : j.status === "succeeded" ? "View record" : "View progress"}</button>
        ${j.archivable ? `<button class="btn sm" data-need="admin" onclick="selfDataArchiveReview(${jsq(j.id)})">Archive record</button>` : ""}</div></div>`).join("")}</div>` : ""}
      ${state.blocking_jobs?.length ? UI.section("Jobs that block preparation", UI.table([{label:"Job"},{label:"State"}], state.blocking_jobs.map(j => [
        `<b>${esc(j.title)}</b><p class="small">${esc(j.message)}</p>${j.closable ? `<button class="btn sm pri" data-need="admin" onclick="selfDataCloseReview(${jsq(j.id)})">Close recovery</button>` : ""}<button class="btn sm" onclick="selfDataOpenJob(${jsq(j.id)})">${j.status === "failed" && j.mutation_recovery && j.kind === "k3s-cluster" ? "Review batch outcome" : "Open job"}</button><button class="btn sm" onclick="operationLog(${jsq(j.id)})">Log</button>`,
        `${esc(j.status)}${j.recovery ? " · requires recovery" : ""}`])) + '<p class="ui-help">Finish or review these jobs before preparing another volume. Archiving a finished preparation does not clear a job that is running or needs recovery.</p>') : ""}
      ${UI.fields(UI.field("Destination storage", `<select aria-label="Destination storage" id="selfDataClass" onchange="selfDataClassChanged()">${(state.classes || []).map(c =>
        `<option value="${esc(c.name)}" data-shareable="${c.shareable ? 1 : 0}" data-current="${c.current ? 1 : 0}" ${c.name === picked ? "selected" : ""}>${esc(c.name)} · ${c.current ? "the class it is on now, mounted by one host at a time" : c.shareable ? "every host can use it" : "stored on one host's disk"}</option>`).join("")}</select>`),
        `<div id="selfDataOneHostField" hidden><label class="row" style="gap:6px"><input type="checkbox" id="selfDataOneHost" ${oneHost ? "checked" : ""} onchange="selfDataInvalidate()"> Mounted by one host at a time</label>
          <p class="ui-help">A volume that one host mounts needs no share-manager, so an unclean restart cannot leave Homestead waiting on one. Only one copy of Homestead can then run.</p></div>`,
        `<div id="selfDataHostField">${UI.field("Which host's disk", `<select aria-label="Host whose disk holds the volume" id="selfDataHost" onchange="selfDataInvalidate()">${selfDataHostOptions(state.nodes || [])}</select>`,
          { help: "This storage keeps the volume on one host's own disk, so Homestead can then run only on that host." })}</div>`)}
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
      UI.facts((review.facts || [["Homestead runs on", review.source], ["Kept, unused", review.destination]]).map(([label, value]) => [label, esc(value)])) +
      UI.ack("selfDataCloseConsent", review.ack || "Close this move's record and keep both volumes and its audit log", {onchange:"selfDataCloseReady()"}) +
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
    if (selfDataCloseDialog === request) childModal("Close data move recovery", `<p role="alert">${esc(e.message)}</p><p>Nothing was changed. Both volumes are kept.</p>` +
      UI.actions(UI.cancel("Back")), true, "operation-review");
  }
};
/* The host only matters for storage that lives on one host's disk. */
window.selfDataClassChanged = () => {
  const option = $("#selfDataClass")?.selectedOptions?.[0];
  const field = $("#selfDataHostField");
  if (field) field.hidden = option?.dataset.shareable === "1";
  // A class every host can share may still be used one host at a time; its own
  // class again only that way.
  const one = $("#selfDataOneHostField"), box = $("#selfDataOneHost");
  if (one) one.hidden = option?.dataset.shareable !== "1";
  if (box) { const current = option?.dataset.current === "1"; if (current) box.checked = true; box.disabled = current; }
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
  if ($("#selfDataOneHostField") && !$("#selfDataOneHostField").hidden) body.one_host = !!$("#selfDataOneHost")?.checked;
  flow.request = body;
  $("#selfDataReview").innerHTML = '<p>Checking…</p>';
  try {
    const review = await selfDataPost("/api/self/data/prepare/preview", body);
    if (selfDataDialog !== flow || flow.request !== body || !$("#selfDataReview")) return;
    if (!review.capacity_token || !review.capacity || review.capacity.blocked) throw new Error("The preparation could not be approved. Check the storage and host chosen.");
    flow.review = { body, review };
    $("#selfDataReview").innerHTML = `${UI.facts([["New volume", `${esc(review.size)} on ${esc(review.storage_class)}`]])}
      ${UI.more("Capacity check", deployCapacityHtml(review.capacity))}
      ${UI.ack("selfDataConsent", "Create this volume", { onchange: "selfDataReady()" })}`;
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
    if (selfDataDialog === flow && $("#selfDataFlow")) $("#selfDataFlow").innerHTML = `<p role="alert">${esc(e.message)}</p><p>Check the saved jobs before trying again. The request may already have created a volume.</p>
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
      if (!current) throw new Error("The preparation record is not available. Check the existing volumes before creating another.");
      flow.state = state; flow.selected = current;
      $("#selfDataFlow").innerHTML = `<p><b>${esc(current.destination)}</b></p><p role="status">${esc(current.message || current.status)}</p>
        ${current.prepared ? "" : UI.progress(current.progress, { label: "Preparation steps", kind: current.status === "failed" ? "bad" : "info" })}
        ${current.prepared ? UI.more("Hosts", UI.fields(
          UI.field("Move coordinator host", `<select aria-label="Move coordinator host" id="selfDataWorker" onchange="selfDataFinalInvalidate()">${selfDataHostOptions(state.nodes, current.node)}</select>`, { help: "This host runs the move while Homestead is stopped." }),
          UI.field("Copy host", `<select aria-label="Copy host" id="selfDataCopy" onchange="selfDataFinalInvalidate()">${selfDataHostOptions(state.nodes, current.node)}</select>`, { help: "This host must be able to use both volumes." }))) : ""}
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
      UI.ack("selfDataArchiveConsent", "Hide this preparation and keep its volumes", {onchange:"selfDataArchiveReady()"}) +
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
    if (selfDataArchiveDialog === request) modal("Archive preparation record", `<p role="alert">${esc(e.message)}</p><p>Check the saved jobs before reviewing again. The volumes are kept.</p>` +
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
    if (!result.capacity_token || (result.stages || []).some(s => s.capacity?.blocked)) throw new Error("The hosts for the move need to be reviewed again.");
    flow.approvedMove = { body, result };
    $("#selfDataFinal").innerHTML = `${UI.callout("warn", "Homestead is offline while its data is copied", "This page shows the progress and reconnects automatically. The old volume is kept.")}
      ${UI.more("Steps and capacity", (result.stages || []).map(s => `<p><b>${esc(s.label)}</b>. ${esc(s.detail)}</p>${deployCapacityHtml(s.capacity)}`).join(""))}
      ${UI.ack("selfDataMoveConsent", "Stop Homestead and move its data", { onchange: "selfDataMoveReady()" })}`;
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
            tip: r.empty ? "" : r.unread?.length ? "Homestead could not check everything in it. Refresh to try again."
              : "Move or delete what it contains first." }])) : ""}</td></tr>`).join("")}</tbody></table></div>
    <div class="dim xs" style="margin-top:8px">${inv.system_hidden} platform namespace${inv.system_hidden === 1 ? "" : "s"} hidden.</div>
    ${UI.more("Changing the default namespace", `<p class="small">New apps go in <b class="mono">${esc(inv.default)}</b> unless another namespace is chosen in Deploy. The default was set when Homestead was installed (<span class="mono">workloadNamespace.name</span> in the Helm chart, or <span class="mono">DEFAULT_NS</span> in the manifest). Homestead also keeps its own settings, network shares and node probe there, so the default cannot be changed here. To put apps in another namespace, create it above and choose it in Deploy.</p>`)}`;
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

window.namespaceDelete = name => modal(`Delete namespace · ${name}`, `${UI.lead(`Deletes the empty namespace <b>${esc(name)}</b>. This cannot be undone.`)}
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
  return `<div><span>Permissions ${tip("Homestead updates its own permissions with each release, so new features have what they need. Allowing this is a one-time command.")}</span>
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
  catch (e) { throw new Error(`The settings could not be read, so nothing was saved. ${e.message}`); }
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
    toast(url ? "The App Store now uses that catalogue" : "The App Store uses the Community Applications feed again", "ok");
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
  catch (e) { host.innerHTML = UI.callout("warn", "The result could not be calculated", esc(e.message)); return; }
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
    return `The standby hosts take apps only when the others cannot. Apps already on a standby host stay there until Balance hosts moves them. It moves apps off standby hosts first, and never onto them.${can("operator")
      ? ` ${UI.button("Balance hosts…", "balanceHosts()", { kind: "sm" })}` : ""}${pending.length
      ? ` <span class="badtext">${esc(pending.map(h => h.name).join(", "))} ${pending.length === 1 ? "is" : "are"} not marked as standby yet${pending.some(h => h.error) ? ` (${esc(pending.find(h => h.error).error)})` : ""}. Homestead tries again every minute.</span>` : ""}`;
  }
  if (mode === "spread") {
    if (!placement && STATE.data.placementUnread) return `Kubernetes places each app on the host with the most room left, based on what apps reserve. <span class="badtext">What the apps reserve could not be read. ${esc(STATE.data.placementUnread)}</span>`;
    const n = placement?.unreserved || 0;
    return `Kubernetes places each app on the host with the most room left, based on what apps reserve.${n
      ? ` ${n} app${n === 1 ? " reserves" : "s reserve"} nothing and ${n === 1 ? "is" : "are"} placed as if ${n === 1 ? "it uses" : "they use"} nothing.${can("operator")
        ? ` ${UI.button("Set reservations from use…", "reservationsReview()", { kind: "sm" })}` : ""}` : " Every app reserves something."}`;
  }
  return mode ? "Kubernetes places apps in its default way. Placement is set for each app." : "Not chosen yet. Kubernetes places apps in its default way.";
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
  if (back) $("#set_rebalance_text").innerHTML = rebalanceModeText(back.value, STATE.data.appSettings?.updates?.maintenance || {}, rebalancePlacement());
};

/* What the chosen mode does, in the window's own words. */
function rebalanceModeText(mode, maintenance, placement) {
  return mode === "suggest" ? "An alert shows how many apps would move back. Nothing moves until the moves are reviewed."
    : mode === "window" ? `The same alert is shown, then the moves run automatically in the update window (${esc(updateWindowText(maintenance))}), except for apps with a scheduled job at that time.`
    : mode === "auto" ? rebalanceAutoText(maintenance, placement)
    : "Nothing moves back automatically. Balance hosts on Hosts, or Rebalance apps in the Containers menu, moves apps when chosen.";
}
/* The placement choice as it stands on the page, and how many apps reserve nothing. */
function rebalancePlacement() {
  return { mode: $("#set_place_mode")?.value ?? STATE.data.appSettings?.placement?.mode ?? "", unreserved: STATE.data.placement?.unreserved || 0 };
}
function rebalanceAutoText(maintenance, placement) {
  const spread = placement?.mode === "spread", unreserved = placement?.unreserved || 0;
  return `When a returned host has been stable for 15 minutes and no volume copy is left to rebuild, the moves run automatically, one at a time. Critical apps wait for the update window (${esc(updateWindowText(maintenance))}). `
    + (spread ? "Apps managed by Flux move by restarting, and Kubernetes chooses the host, so a few may not end up where planned. An app with a host preference from an earlier move stays where it is, and the reason is shown. On a cluster that has used Move or Balance hosts for a while, this can be about half of the apps. Apps managed by Argo CD stay where they are."
      : "Apps managed by Flux or Argo CD stay where they are.")
    + (spread && unreserved >= 3 ? ` ${unreserved} apps reserve nothing, so Kubernetes cannot take them into account. ${UI.button("Set reservations from use…", "reservationsReview()", { kind: "sm" })}` : "");
}
/* What moving back is waiting for, or why it has stopped, from GET /api/recovery. */
function rebalanceStatusHtml(s) {
  if (s?.unread) return UI.callout("warn", "Homestead could not read what moving apps back is waiting for", esc(s.unread));
  if (!s) return "";
  const resume = can("admin") ? UI.button("Resume", "rebalanceResume()", { kind: "sm" }) : "";
  const stopped = [s.paused && `Stopped: ${esc(s.paused.why)}`,
    s.undone && `${esc(s.undone.by)} removed Homestead's restart mark from ${esc(s.undone.app)}, so apps it applies are left out of moves back.`].filter(Boolean);
  if (stopped.length) return UI.callout("warn", "Moving apps back is paused", `${stopped.join(" ")} ${resume}`);
  if (s.mode !== "auto" && s.mode !== "window") return "";
  const words = [...(s.waiting || []), s.later && `${s.later} critical app${s.later === 1 ? " waits" : "s wait"} for the window`,
    s.left_out && `${s.left_out} left out because a tool applies ${s.left_out === 1 ? "it" : "them"}`].filter(Boolean);
  const kept = s.kept || [], total = s.kept_total ?? kept.length;
  return (words.length ? `<div class="dim xs">Waiting: ${words.map(esc).join("; ")}</div>` : "")
    + (kept.length ? UI.more(`${total} container${total === 1 ? " stays where it is" : "s stay where they are"}`,
      `<ul class="ui-list">${kept.map(row => `<li><span class="mono">${esc(row.id)}</span>: ${esc(row.why)}</li>`).join("")}${total > kept.length ? `<li>and ${total - kept.length} more</li>` : ""}</ul>`) : "");
}
async function rebalanceStatusLoad() {
  const status = await api("/api/recovery").catch(e => ({ unread: e.message }));
  const host = $("#set_rebalance_status");
  if (host) host.innerHTML = rebalanceStatusHtml(status);
}
window.rebalanceResume = async () => {
  try {
    const status = await api("/api/recovery/resume", { method: "POST" });
    $("#set_rebalance_status").innerHTML = rebalanceStatusHtml(status);
    toast("Moving apps back can start again", "ok");
  } catch (e) { toast(`Moving apps back could not be resumed. ${e.message}`, "bad"); }
};

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
  if (!(await ask("The changes in this section are not saved and will be discarded.", { title: "Leave without saving", ok: "Discard changes", danger: true }))) return false;
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
  modal("Update window", UI.lead("When a reviewed update may start under the Maintenance window policy. Times are in UTC.") +
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
window.catalogEdit = () => modal("App Store catalogue", UI.lead("Any feed in the Community Applications format: the public feed, a mirror or a custom list of templates.") +
  UI.field("Feed URL",
    `<input id="set_catalog" type="url" maxlength="500" placeholder="blank: the public Community Applications feed" value="${esc(STATE.data.appSettings?.catalog_url || "")}">`,
    { tipHtml: " " + tip("A JSON feed in the same format as applicationFeed.json: an object with an applist, or a list of templates. Leave it empty to use the public feed.") }) +
  UI.actions(UI.cancel() + UI.button("Save catalogue", "catalogSave(false)", { kind: "pri" }), UI.button("Use Community Applications", "catalogSave(true)")));
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
  host.innerHTML = `${UI.moduleHeader(`Homestead's health`, `Background tasks, checked every 15 seconds while this page is open`, UI.chip(problems ? `${problems} to look at` : "all well", problems ? "warn" : "ok"))}
    <div class="health-list">
      ${row("Kubernetes API", h.api.ok ? (h.api.ms > 2000 ? "warn" : "ok") : "bad", h.api.ok ? `${h.api.ms} ms` : "no answer",
        h.api.ok ? (h.api.ms > 2000 ? "Slow to respond, so pages and actions are slow." : "Responding normally.") : esc(h.api.error || ""))}
      ${row("Homestead", "ok", `v${h.version}`, `${pods.filter(p => p.ready).length} of ${h.replicas?.desired ?? "?"} cop${(h.replicas?.desired ?? 1) === 1 ? "y" : "ies"} ready`
        + (pods.length ? ` · ${pods.map(p => `${esc(p.node.replace("harvester-", ""))}${p.leader ? " (leader)" : ""}${p.this ? " - this one" : ""}`).join(", ")}` : "")
        + (h.leader ? "" : " · this copy is standing by"))}
      ${h.loops.map(l => row(l.label, HEALTH_TONE[l.state] || "", l.state === "ok" ? "running" : l.state,
        l.state === "standby" ? "Runs on the leader copy of Homestead." : `Last done ${esc(agoText(l.last_ok))}` + (l.error ? ` · <span class="badtext">${esc(l.error)}</span>` : ""))).join("")}
      ${row("Node probe", !probe.installed ? "neutral" : probe.ready < probe.desired || probe.reporting < probe.desired ? "warn" : "ok",
        probe.installed == null && probe.error ? "unknown" : !probe.installed ? "not installed" : `${probe.ready}/${probe.desired} hosts`,
        probe.installed == null && probe.error ? esc(probe.error)
          : !probe.installed ? "Temperatures, host devices, every disk and drive health come from it."
          : `${probe.reporting} reporting · drive health on ${probe.smart} · ${esc(probe.detail || "")}`,
        "")}
      ${row("Samba (network shares)", !samba.installed || !samba.enabled ? "neutral" : samba.ready < samba.desired ? "warn" : "ok",
        !samba.installed ? "not installed" : !samba.enabled ? "off" : samba.ready < samba.desired ? "starting" : "serving",
        (samba.installed ? `${samba.shares} share${samba.shares === 1 ? "" : "s"}${samba.address ? ` at <span class="mono">\\\\${esc(samba.address)}</span>` : ""} · ${esc(samba.image || "")}`
          : `Installed with the first network share. ${samba.shares ? `${samba.shares} share${samba.shares === 1 ? " is" : "s are"} defined.` : ""}`),
        `<button class="btn sm" onclick="go('shares')">Network shares</button>`)}
      ${row("Permissions", h.permissions?.state === "error" ? "bad" : h.permissions?.state === "current" || h.permissions?.state === "updated" ? "ok" : "neutral",
        h.permissions?.state || "unknown", esc(h.permissions?.detail || ""))}
      ${row("Audit log", auditFile ? "ok" : "neutral", auditFile ? "kept" : "pod log only",
        auditFile ? "Every change is written to Homestead's log and to audit.jsonl on its data volume (at most two files of 10 MiB)."
          : "Every change is written to Homestead's log only.",
        admin ? `<button class="btn sm" onclick="auditFileSet(${!auditFile})">${auditFile ? "Write to the log only" : "Also keep on the data volume"}</button>` : "")}
      ${h.addresses ? row("Addresses", h.addresses.error ? "neutral" : h.addresses.problem || (h.addresses.clashes || []).length ? "bad" : "ok",
        h.addresses.error ? "unknown" : h.addresses.problem || (h.addresses.clashes || []).length ? "on the cluster's address" : "separate",
        h.addresses.error ? esc(h.addresses.error)
          : h.addresses.problem ? `<span class="badtext">${esc(h.addresses.problem)}</span>`
          : (h.addresses.clashes || []).length ? `<span class="badtext">${esc(h.addresses.clashes.map(c => `${c.namespace}/${c.service}`).join(", "))}
              ${h.addresses.clashes.length === 1 ? "is" : "are"} on ${esc(h.addresses.clashes[0].ip)}, the cluster's own address (${esc(h.addresses.clashes[0].owner)}).
              Hosts join the cluster through it, so give ${h.addresses.clashes.length === 1 ? "it an address" : "them addresses"} of their own under Edit → Network.</span>`
          : `No app uses the cluster's own address${(h.addresses.platform || []).length ? ` (<span class="mono">${esc(h.addresses.platform.join(", "))}</span>)` : ""}.`) : ""}
      ${h.backups?.error ? row("Backup storage", "neutral", "unknown", esc(h.backups.error))
        : row("Backup storage", h.backups?.ready ? "ok" : h.backups?.deployed ? "warn" : "neutral",
        h.backups?.ready ? "ready" : h.backups?.deployed ? "starting" : "none", h.backups?.endpoint ? `<span class="mono">${esc(h.backups.endpoint)}</span>` : "Set it up under Data protection.")}
      ${h.mqtt?.state && h.mqtt.state !== "off" ? row("MQTT", h.mqtt.state === "publishing" ? "ok" : "warn", h.mqtt.state,
        h.mqtt.error ? `<span class="badtext">${esc(h.mqtt.error)}</span>` : esc(h.mqtt.detail || "")) : ""}
      ${row("Metrics for Prometheus", metricsOn ? "ok" : "neutral", metricsOn ? "on" : "off",
        metricsOn ? `<span class="mono">${esc(location.origin)}/metrics</span>, read with an API key as a bearer token.`
          : "When this is on, Prometheus or an uptime checker can read Homestead's health from /metrics with an API key.",
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
  if (enabled && !(await ask("Every API key, whatever it was created for, can then read Homestead's health from /metrics. "
    + "This includes the number of hosts, apps, jobs and alerts, the time of the last backup and Homestead's version, but no names. "
    + "Turn it on for Prometheus or an uptime checker.",
    { title: "Turn on metrics", ok: "Turn on metrics" }))) return;
  try {
    await settingsWrite({ metrics: { enabled } });
    STATE.data.appSettings = null;
    await loadHealthSettings(true);
    toast(enabled ? "Metrics are available at /metrics" : "Metrics are turned off", "ok");
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
    catch (e) { return modal("Install Samba", UI.error(e, { title: "No address could be offered", retry: "sambaSet(true)", dialog: true })); }
    const own = (choices.own || []).find(v => v.free);
    return modal("Install Samba", `<p class="small">Samba serves the network shares. Choose the address that Windows PCs will use to reach it.</p>
      <div class="f">${vipPicker("smb", own ? own.ip : (choices.free[0] || ""), choices)}</div>
      ${UI.actions(`<button class="btn pri" onclick="sambaInstallGo()">Install Samba</button><button data-dialog-dismiss="true" class="btn" onclick="closeModal()">Cancel</button>`)}`);
  }
  if (!on && !(await ask("Every network share stops being served until Samba is turned on again. Their volumes, settings and passwords are kept.", { title: "Stop Samba", ok: "Stop Samba", danger: true }))) return;
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
    toast(file ? "The audit log is kept on the data volume" : "The audit log is written to Homestead's log only", "ok");
  } catch (e) { toast(e.message, "bad"); }
  selfHealthPaint();
};

window.sambaInstallGo = async (selectedAddress = null) => {
  const address = selectedAddress === null ? ($("#smb_lb_ip")?.value || "").trim() : selectedAddress;
  if (!address && !nodeAddressesOnly()) return toast("Choose an address. If the list is empty, add addresses under Networking.", "bad");
  try {
    const r = await api("/api/self/samba", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ enabled: true, address }) });
    toast(r.detail, "ok"); closeModal(); setTimeout(() => { addonsPaint(); selfHealthPaint(); }, 1500);
  } catch (e) { toast(e.message, "bad"); }
};

window.sambaRemove = () => {
  modal("Remove SMB server", `<p>The SMB server and its address are removed. Network shares stop being served.</p>
    ${UI.callout("warn", "", `The share settings, passwords, volumes and their data are kept. Turn SMB on again here to serve them.`)}
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
    if (!(nfs.exports || []).length) return modal("Set up NFS exports", `<p>Before installing the NFS server, choose a shared volume and the client addresses allowed to use it in Network Shares.</p>
      ${UI.actions(`<button class="btn pri" onclick="closeModal();go('shares')">Network Shares</button><button data-dialog-dismiss="true" class="btn" onclick="closeModal()">Cancel</button>`)}`);
    if (nodeAddressesOnly()) return modal("NFS needs its own address", `<p>The NFS server needs an address of its own, which k3s's built-in load balancer cannot provide. Install kube-vip under Cluster → Add-ons, then try again.</p>
      ${UI.actions(`<button class="btn pri" onclick="closeModal();addonsPaint()">OK</button>`)}`);
    let choices;
    try { choices = await vipChoices(); }
    catch (e) { return modal("Install NFSv4 server", UI.error(e, { title: "No address could be offered", retry: "nfsSet(true)", dialog: true })); }
    choices.used = [];
    choices.own = (choices.own || []).filter(v => v.free);
    const own = (choices.own || []).find(v => v.free);
    return modal("Install NFSv4 server", `<p>A separate NFS server will serve ${plural((nfs.exports || []).length, "selected shared volume")} on TCP port 2049. If its host fails, file access pauses until the server and its storage are available on another host.</p>
      ${nfsRecovery(nfs, true)}
      ${UI.callout("warn", "", `Only the client addresses set for each share can use it. Removing this server later keeps every share's settings and volume.`)}
      <p class="small">SMB and NFS run as separate servers, so each needs its own address. This keeps client address restrictions working after a host failure.</p>
      <div class="f">${vipPicker("nfs", own ? own.ip : (choices.free[0] || ""), choices)}</div>
      ${UI.actions(`<button class="btn pri" onclick="nfsInstallGo()">Install NFS server</button><button data-dialog-dismiss="true" class="btn" onclick="closeModal()">Cancel</button>`)}`);
  }
  if (!on && !(await ask("Shares served over NFS are unavailable until NFS is turned on again. Their settings and volumes are kept.", { title: "Stop NFS", ok: "Stop NFS", danger: true }))) return;
  try {
    const result = await api("/api/self/nfs", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ enabled: on }) });
    toast(result.detail, "ok"); setTimeout(() => { addonsPaint(); if (STATE.view === "shares") viewShares(); }, 1500);
  } catch (e) { toast(e.message, "bad"); }
};
window.nfsInstallGo = async () => {
  const address = ($("#nfs_lb_ip")?.value || "").trim();
  if (!address) return toast("Choose an address for NFS", "bad");
  try {
    const result = await api("/api/self/nfs", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ enabled: true, address }) });
    closeModal(); toast(result.detail, "ok"); setTimeout(() => addonsPaint(), 1500);
  } catch (e) { toast(e.message, "bad"); }
};
window.nfsRemove = () => modal("Remove NFS server", `<p>The NFS server and its address are removed. Clients lose access to the shares it serves.</p>
  ${UI.callout("warn", "", `The NFS share settings, SMB shares, volumes and their data are kept.`)}
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
