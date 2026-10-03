/* A k3s or RKE2 host's own operating system, on its node page.

   Harvester looks after its hosts itself; on k3s and RKE2 Homestead reads
   each host every six hours through the host helper (homestead_host_os.py):
   the distribution and kernel, updates waiting and which are security fixes,
   whether a restart is needed, failed services, the clock and the root
   filesystem, and each disk's partition table, which the Disks card draws
   to scale. Installing updates is the host's own package manager, run as a
   job; a restart afterwards is Host actions, with its drain review. */

const HOST_OS = { cache: {} };

async function hostOsLoad(node, force = false) {
  if (!force && HOST_OS.cache[node]) return HOST_OS.cache[node];
  const r = await api("/api/node/os?name=" + encodeURIComponent(node));
  HOST_OS.cache[node] = { applies: r.applies, facts: (r.hosts || {})[node] || null };
  return HOST_OS.cache[node];
}

function hostOsAge(at) {
  if (!at) return "never";
  const seconds = Math.max(0, Date.now() / 1000 - at);
  return seconds < 90 ? "just now" : fmtAgo(seconds);
}

function hostOsBody(node, f) {
  if (!f) return `${UI.lead("Not read yet: the leader reads each host within ten minutes of starting, then every six hours.")}
    ${UI.actions(UI.button("Check now", `hostOsCheck(${jsArg(node)})`, { id: "hosCheck", attrs: 'data-need="admin"' }))}`;
  const updates = f.updates || [], sec = f.security || 0;
  const updateText = f.upgrading ? UI.chip("installing now", "info")
    : updates.length ? `${updates.length} waiting ${sec ? UI.chip(`${sec} security`, "warn") : ""}`
    : f.package_manager ? "up to date" : "no package manager Homestead knows";
  const failed = f.failed_units || [];
  const facts = UI.facts([
    ["System", esc(f.os || "—")],
    ["Kernel", `<span class="mono">${esc(f.kernel || "—")}</span>`],
    ["Updates", updateText],
    ["Restart", f.reboot ? `${UI.chip("needed", "warn")} <span class="dim xs">${esc(f.reboot_for || "to finish an update")}</span>` : "not needed"],
    ["Services", failed.length ? `${UI.chip(`${failed.length} failed`, "bad")} <span class="mono xs">${esc(failed.join(", "))}</span>` : "all running"],
    ["Clock", f.ntp === false ? UI.chip("not synchronised", "warn") : f.ntp ? "synchronised" : "—"],
    ["Automatic updates", hostOsAuto(f.auto)],
    ["Root filesystem", `${UI.meter({ now: f.root_used_pct || 0, warnAt: 90 })}<span class="dim xs">${f.root_used_pct || 0}% of ${sizeText(f.root_total_gb || 0)}</span>`],
  ]);
  const list = updates.length ? UI.more(`The ${updates.length} update${updates.length === 1 ? "" : "s"}`,
    `<div class="hos-updates">${updates.slice(0, 300).map(u => `<span class="tag ${u.security ? "warn" : ""}">${esc(u.name)}</span>`).join("")}
      ${updates.length > 300 ? `<span class="dim xs">and ${updates.length - 300} more</span>` : ""}</div>`) : "";
  const lists = f.lists_at ? ` · package lists from ${hostOsAge(f.lists_at)}` : "";
  const last = f.last_upgrade?.at ? ` · last update ${f.last_upgrade.ok ? "installed" : "failed"} ${hostOsAge(f.last_upgrade.at)}` : "";
  return `${facts}${list}
    <div class="dim xs" style="margin-top:8px">Read ${hostOsAge(f.at)}${lists}${last}</div>
    ${UI.actions([
      f.reboot ? UI.button("Restart…", `nodeActions(${jsArg(node)})`, { attrs: 'data-need="admin"' }) : "",
      UI.button("Check now", `hostOsCheck(${jsArg(node)})`, { id: "hosCheck", attrs: 'data-need="admin"' }),
      updates.length && !f.upgrading ? UI.button(`Install ${updates.length} update${updates.length === 1 ? "" : "s"}`,
        `hostOsUpgrade(${jsArg(node)})`, { kind: "pri", attrs: 'data-need="admin"' }) : "",
    ].join(""))}
    <div class="dim xs" style="margin-top:8px">Every host, one at a time, or in a weekly window:
      <a class="linkish" onclick="osUpdates()">OS updates</a>.</div>`;
}

/* What the host installs by itself: Ubuntu's unattended-upgrades, and
   whether Homestead holds it off. */
function hostOsAuto(auto) {
  auto = auto || {};
  if (!auto.tool) return "none";
  if (auto.held) return `${esc(auto.tool)} · ${UI.chip("held off by Homestead", "info")}`;
  if (!auto.on) return `${esc(auto.tool)} · off`;
  return `${esc(auto.tool)} · on${auto.reboots ? ` ${UI.chip("restarts the host by itself", "warn")}` : ""}`;
}

window.nodeHostOsPaint = async (node, force = false) => {
  const card = $("#nodeHostOs");
  if (!card) return;
  try {
    const r = await hostOsLoad(node, force);
    // Harvester's hosts are Harvester's to update.
    if (!r.applies) { card.remove(); return; }
    card.querySelector(".hos-body").innerHTML = hostOsBody(node, r.facts);
    if (window.applyRole) applyRole();
    if ($("#nodeDisks") && r.facts) nodePartitionsPaint(node);
  } catch (e) { card.querySelector(".hos-body").innerHTML = `<div class="dim small">${esc(e.message)}</div>`; }
};

window.hostOsCheck = async node => {
  const button = $("#hosCheck");
  if (button) { button.disabled = true; button.textContent = "Checking…"; }
  try {
    const r = await api("/api/node/os/check", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ node }) });
    HOST_OS.cache[node] = { applies: true, facts: r.facts };
    toast(`${node}: ${r.facts.summary?.text || "read"}`, "ok");
  } catch (e) { toast(e.message, "bad"); }
  nodeHostOsPaint(node);
};

window.hostOsUpgrade = async node => {
  const f = HOST_OS.cache[node]?.facts || {};
  const count = (f.updates || []).length;
  const ok = await ask(`Install ${count} update${count === 1 ? "" : "s"} on ${node}? Its package manager (${f.package_manager || "the host's"}) refreshes its lists and upgrades everything waiting. Workloads keep running; ${f.reboot || (f.updates || []).some(u => /^linux-(image|modules)|^kernel/.test(u.name)) ? "a kernel update then needs a restart, from Host actions" : "a restart is only needed if an update asks for one"}.`,
    { title: "Install updates", ok: "Install updates" });
  if (!ok) return;
  try {
    const r = await api("/api/node/os/upgrade", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ node }) });
    if (r.operation && window.noteOperation) noteOperation(r.operation);
    toast(r.detail, "ok");
    HOST_OS.cache[node] = null;
    nodeHostOsPaint(node, true);
  } catch (e) { toast(e.message, "bad"); }
};

/* ---------- partition tables, to scale ---------- */
const HOS_SYSTEM = /^\/(boot(\/efi)?|usr|var|home)?$/;
function hostOsRole(p) {
  if (!p.name) return "free";
  const mounts = p.mounts || [];
  if (mounts.some(m => HOS_SYSTEM.test(m))) return "sys";
  if (mounts.some(m => /longhorn|^\/mnt\//.test(m))) return "data";
  if ((p.holds || []).includes("lvm") || /LVM|raid|crypto/i.test(p.fstype || "")) return "lvm";
  return p.fstype ? "other" : "free";
}
function hostOsBytes(bytes) { return sizeText((Number(bytes) || 0) / 1024 ** 3); }
function hostOsFilesystem(type) { return type === "LVM2_member" ? "LVM physical volume" : type; }

function hostPhysicalLayouts(node, facts) {
  // Cached OS reports predate transport metadata; use live hardware when known.
  const hardware = new Set((STATE.data.disks?.nodes?.[node] || []).map(d => d.device).filter(Boolean));
  return (facts?.disks || []).filter(d => d.size > 0 && (!hardware.size || hardware.has(d.name)));
}

function partitionMap(d) {
  const size = d.size || 1, segs = [];
  let at = 0;
  for (const p of d.partitions || []) {
    if (p.start - at > size * 0.004) segs.push({ size: p.start - at });
    segs.push(p);
    at = Math.max(at, p.start + p.size);
  }
  if (!(d.partitions || []).length) segs.push(d.fstype ? { name: d.name, size, fstype: d.fstype, mounts: d.mount ? [d.mount] : [] } : { size });
  else if (size - at > size * 0.004) segs.push({ size: size - at });
  const label = s => s.name ? `${s.name} · ${hostOsBytes(s.size)}${s.fstype ? ` · ${hostOsFilesystem(s.fstype)}` : ""}${(s.mounts || []).length ? ` · ${s.mounts.join(", ")}` : (s.holds || []).length ? ` · holds ${s.holds.join(", ")}` : ""}`
    : `unallocated · ${hostOsBytes(s.size)}`;
  return `<div class="pmap-disk"><div class="between"><span class="small"><b class="mono">${esc(d.name)}</b>
      <span class="dim xs">${hostOsBytes(d.size)} · ${d.table ? esc(d.table.toUpperCase()) : (d.partitions || []).length ? "partitioned" : d.fstype ? `whole-disk ${esc(hostOsFilesystem(d.fstype))} filesystem` : "blank"}</span></span></div>
    <div class="pmap" role="img" aria-label="${esc(`${d.name}: ${segs.map(label).join("; ")}`)}">${segs.map(s =>
      `<span class="pmap-seg ${hostOsRole(s)}" style="flex:${Math.max(s.size / size, 0.012)} 1 0" data-tip="${esc(label(s))}">
        ${s.size / size >= 0.08 ? `<span>${esc(s.name ? (s.mounts?.[0] || hostOsFilesystem(s.fstype) || s.name) : "free")}</span>` : ""}</span>`).join("")}</div>
    ${segs.length > 1 ? `<div class="dim xs mono pmap-list">${segs.map(s => esc(label(s))).join("<br>")}</div>` : ""}</div>`;
}

window.nodePartitionsPaint = async node => {
  const host = $("#nodeDisks");
  if (!host) return;
  const f = HOST_OS.cache[node]?.facts;
  const disks = hostPhysicalLayouts(node, f);
  host.querySelector(".pmaps")?.remove();
  if (!disks.length) return;
  host.insertAdjacentHTML("beforeend", `<div class="pmaps">${UI.more(`Partition tables · ${disks.length} disk${disks.length === 1 ? "" : "s"}`,
    `${disks.map(partitionMap).join("")}
     <div class="pmap-key dim xs"><span class="pmap-seg sys"></span>system <span class="pmap-seg data"></span>Longhorn and data
       <span class="pmap-seg lvm"></span>LVM or RAID <span class="pmap-seg other"></span>other <span class="pmap-seg free"></span>unallocated
       · as read ${hostOsAge(f.at)}</div>`, true)}</div>`);
};

/* ---------- every host: one at a time, now or in a weekly window ----------
   homestead_os_rollout.py: each Ready host in turn - the one Homestead's
   leader runs on last - gets its updates, and a restart through the same
   review, drain and wait as Host actions when an update asks for one. */
const OSU_DAYS = [["mon", "Mon"], ["tue", "Tue"], ["wed", "Wed"], ["thu", "Thu"], ["fri", "Fri"], ["sat", "Sat"], ["sun", "Sun"]];

function osUpdatesHostsHtml(r) {
  const rows = Object.entries(r.hosts || {}).sort(([a], [b]) => a.localeCompare(b)).map(([name, f]) => [
    `<b>${esc(name)}</b><div class="dim xs">${esc(f.os || "")}${f.at ? ` · read ${esc(hostOsAge(f.at))}` : ""}</div>`,
    (f.updates || []).length ? `${f.updates.length}${f.security ? ` ${UI.chip(`${f.security} security`, "warn")}` : ""}` : "up to date",
    f.reboot ? UI.chip("needed", "warn") : "—",
    hostOsAuto(f.auto),
  ]);
  return rows.length ? UI.table([{ label: "Host" }, { label: "Updates" }, { label: "Restart" }, { label: "Automatic updates" }], rows)
    : UI.lead("No host has been read yet; the leader reads each within ten minutes of starting.");
}
function osUpdatesUnavailableHtml() {
  return STATE.platform?.harvester
    ? UI.callout("info", "Harvester updates its own hosts.", `Its hosts' operating system comes with Harvester. ${UI.actions(UI.button("Open Cluster", "go('cluster')"))}`)
    : UI.callout("info", "Host updates are managed here on k3s and RKE2.", "Use your platform's host tools to update this cluster's operating systems.");
}
window.osUpdatesCardPaint = async () => {
  const card = $("#settingsHostUpdates");
  if (!card) return;
  const header = actions => `<div class="settings-card-head"><div><div class="ctitle">Host updates</div>
    <div class="csub">Operating system packages, security updates and restarts</div></div><div class="row">${actions}</div></div>`;
  try {
    const r = await api("/api/os-updates");
    if ($("#settingsHostUpdates") !== card) return;
    if (!r.applies) { card.innerHTML = header("") + osUpdatesUnavailableHtml(); return; }
    const s = r.settings || {}, schedule = s.schedule || {};
    const run = r.rollout?.status === "running" ? r.rollout : null;
    const last = run ? null : r.rollout || r.last;
    const days = OSU_DAYS.filter(([id]) => (schedule.days || []).includes(id)).map(([, label]) => label).join(", ");
    const windowText = schedule.enabled ? `${days} at ${String(schedule.hour ?? 0).padStart(2, "0")}:00 ${schedule.tz || ""}` : "Weekly window off";
    card.innerHTML = header(`${UI.button("Refresh status", "osUpdatesCardPaint()")}${can("admin")
      ? UI.button("Manage host updates", "osUpdates()", { attrs: 'data-need="admin"' }) : UI.chip("admin managed")}`) +
      `<div class="ui-stack">
        ${run ? UI.progress(Math.round((run.index || 0) * 100 / Math.max(1, (run.nodes || []).length)),
          {label: "Updating hosts one at a time", detail: run.message || ""}) : ""}
        ${last ? UI.callout(last.status === "failed" ? "bad" : "info", `Last run ${last.status || "finished"}`, esc(last.message || "")) : ""}
        ${osUpdatesHostsHtml(r)}
        <p class="dim small">${s.manage === "homestead" ? "Managed by Homestead, one host at a time" : "Managed by each host's automatic updates"}
          · ${esc(windowText)} · ${s.reboot === "never" ? "Rollout restarts: manual" : "Rollout restarts: when needed, drained first"}.
          Manage host updates to change the schedule or start a reviewed update.</p>
      </div>`;
    if (window.applyRole) applyRole();
  } catch (e) {
    if ($("#settingsHostUpdates") !== card) return;
    card.innerHTML = header(UI.button("Try again", "osUpdatesCardPaint()")) + UI.callout("bad", "Host update status could not be read.", esc(e.message));
  }
};

window.osUpdates = async (child = false) => {
  (child ? childModal : modal)("OS updates", '<div class="empty"><span class="spin2"></span> reading every host</div>', true);
  let r;
  try { r = await api("/api/os-updates"); }
  catch (e) { $("#mbody").innerHTML = UI.callout("bad", "OS updates could not be read.", esc(e.message)); return; }
  if (!r.applies) {
    $("#mbody").innerHTML = osUpdatesUnavailableHtml();
    return;
  }
  const s = r.settings, run = r.rollout && r.rollout.status === "running" ? r.rollout : null, last = r.rollout && !run ? r.rollout : null;
  const results = rollout => (rollout.results || []).map(x => `<li><b>${esc(x.node)}</b> · ${esc(x.note)}</li>`).join("");
  const tz = Intl.DateTimeFormat().resolvedOptions().timeZone || "this browser's time";
  $("#mbody").innerHTML = `<div class="ui-stack">
    ${run ? `${UI.progress(Math.round((run.index || 0) * 100 / Math.max(1, run.nodes.length)), { label: `Updating ${run.nodes.length} hosts, one at a time`, detail: run.message || "" })}
      ${results(run) ? `<ul class="osu-results small">${results(run)}</ul>` : ""}` : ""}
    ${last ? `<div class="note ${last.status === "failed" ? "bad" : ""} small"><b>Last run ${esc(last.status)}</b>${last.finished ? ` · ${esc(hostOsAge(last.finished))}` : ""}<br>${esc(last.message || "")}</div>` : ""}
    ${osUpdatesHostsHtml(r)}
    ${UI.section("Settings", `
      <div class="f"><label>Who installs updates ${tip("Ubuntu's unattended-upgrades installs security updates on each host by itself, and with Automatic-Reboot restarts it without a drain; Homestead then holds it off only while it updates every host. Chosen, Homestead switches it off on every host and installs updates itself, one host at a time, restarting through its review and drain.")}</label>
        <select id="osu_manage">
          <option value="ubuntu" ${s.manage === "ubuntu" ? "selected" : ""}>Each host, by itself (Ubuntu's automatic updates)</option>
          <option value="homestead" ${s.manage === "homestead" ? "selected" : ""}>Homestead, one host at a time</option></select></div>
      <label class="switch"><input type="checkbox" id="osu_on" ${s.schedule.enabled ? "checked" : ""} onchange="osUpdatesWindow()"> <span>A weekly window</span></label>
      <div id="osu_window" ${s.schedule.enabled ? "" : "hidden"}>
        <div class="osu-days">${OSU_DAYS.map(([id, label]) => `<label class="check"><input type="checkbox" value="${id}" ${s.schedule.days.includes(id) ? "checked" : ""}> ${label}</label>`).join("")}</div>
        <div class="f"><label>Starting at (${esc(tz)})</label><select id="osu_hour">${Array.from({ length: 24 }, (_, h) =>
          `<option value="${h}" ${s.schedule.hour === h ? "selected" : ""}>${String(h).padStart(2, "0")}:00</option>`).join("")}</select></div></div>
      <div class="f"><label>Restarts</label><select id="osu_reboot">
        <option value="when-needed" ${s.reboot === "when-needed" ? "selected" : ""}>When an update needs one, drained first</option>
        <option value="never" ${s.reboot === "never" ? "selected" : ""}>Never - I restart hosts myself</option></select></div>
      <label class="check"><input type="checkbox" id="osu_single" ${s.single_copy ? "checked" : ""}> Restart a host even when a volume has its only healthy copy there - that volume is unavailable until the host is back</label>
      ${UI.actions(UI.button("Save settings", "osUpdatesSave()", { id: "osu_save", attrs: 'data-need="admin"' }))}`)}
    ${UI.more("What an update of every host does", `<p class="small">Updates run one host at a time, with Homestead's leader last. Restarts use Host actions checks: running VMs, a sole etcd member or a volume's only copy can block them. Hosts drain through disruption budgets and resume scheduling once Ready with healthy storage. Failed installs stop the queue. Ubuntu automatic updates pause during the run.</p>`)}
    ${UI.actions([
      run ? UI.button("Stop after this host", "osUpdatesStop()", { attrs: 'data-need="admin"' }) : "",
      !run ? UI.button("Update every host now", "osUpdatesStart()", { kind: "pri", attrs: 'data-need="admin"' }) : "",
    ].join(""))}</div>`;
  if (window.applyRole) applyRole();
};
window.osUpdatesWindow = () => { $("#osu_window").hidden = !$("#osu_on").checked; };
window.osUpdatesSave = async () => {
  const body = { manage: $("#osu_manage").value, reboot: $("#osu_reboot").value, single_copy: $("#osu_single").checked,
    schedule: { enabled: $("#osu_on").checked, hour: +$("#osu_hour").value,
      days: [...document.querySelectorAll(".osu-days input:checked")].map(x => x.value),
      tz: Intl.DateTimeFormat().resolvedOptions().timeZone || "", offset_min: -new Date().getTimezoneOffset() } };
  try {
    await api("/api/os-updates/settings", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
    toast("OS update settings saved", "ok");
    osUpdatesCardPaint();
  } catch (e) { toast(e.message, "bad"); }
};
window.osUpdatesStart = async () => {
  if (!(await ask("Update every host now, one at a time? Each installs what is waiting; a host whose update asks for a restart is drained and restarted before the next is touched. Workloads move off each host while it restarts.",
    { title: "Update every host", ok: "Update every host" }))) return;
  try {
    const r = await api("/api/os-updates/start", { method: "POST", headers: { "Content-Type": "application/json" }, body: "{}" });
    if (r.operation && window.noteOperation) noteOperation(r.operation);
    toast(r.detail, "ok");
    osUpdates();
    osUpdatesCardPaint();
  } catch (e) { toast(e.message, "bad"); }
};
window.osUpdatesStop = async () => {
  try {
    const r = await api("/api/os-updates/stop", { method: "POST", headers: { "Content-Type": "application/json" }, body: "{}" });
    toast(r.detail, "ok");
    osUpdates();
    osUpdatesCardPaint();
  } catch (e) { toast(e.message, "bad"); }
};
