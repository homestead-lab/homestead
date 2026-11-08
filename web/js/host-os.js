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
    `<div class="hos-updates">${updates.slice(0, 300).map(u => UI.chip(u.name, u.security ? "warn" : "neutral")).join("")}
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
  const ok = await ask(`Install ${count} update${count === 1 ? "" : "s"} on ${node}? Its package manager (${f.package_manager || "the host's"}) refreshes its lists and upgrades everything waiting. Apps and VMs keep running; ${f.reboot || (f.updates || []).some(u => HOS_KERNEL.test(u.name)) ? "a kernel update then needs a restart, from Host actions" : "a restart is only needed if an update asks for one"}.`,
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
const HOS_KERNEL = /^linux-(image|modules)|^kernel/;
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

/* ---------- the hosts: one at a time, now or in a weekly window ----------
   homestead_os_rollout.py: each Ready host in turn, or the ones chosen here -
   the one Homestead's leader runs on last - gets its updates, and a restart
   through the same review, drain and wait as Host actions when an update asks
   for one. */
const OSU_DAYS = [["mon", "Mon"], ["tue", "Tue"], ["wed", "Wed"], ["thu", "Thu"], ["fri", "Fri"], ["sat", "Sat"], ["sun", "Sun"]];

/* When a host is next read by itself (#371): the leader reads each every
   every_s, so a reading's age says how soon it moves on. */
function hostOsNext(at, every) {
  if (!at || !every) return "";
  const left = at + every - Date.now() / 1000;
  return left <= 60 ? " · next check due now" : ` · next check in ~${fmtUp(left)}`;
}
/* What a row of the dialog offers (actions: the Settings card's table has none).
   A host with updates: its own reviewed run. One that only waits for a restart:
   the Host actions review. While any run goes, no row starts another. */
function osUpdatesRowAction(name, f, r) {
  const run = osUpdatesRunning(r);
  if (run) return (run.node?.name || run.nodes[run.index]) === name ? UI.chip("updating now", "info")
    : !run.stop && run.nodes.indexOf(name) > run.index ? UI.chip("next", "neutral") : "";
  if (f.upgrading) return UI.chip("installing now", "info");
  // Both when both wait: a host the checks refused may have new updates by now.
  return [(f.updates || []).length ? UI.button("Update…", `osUpdatesReview([${jsArg(name)}])`, { attrs: 'data-need="admin"' }) : "",
    f.reboot ? UI.button("Restart…", `nodePowerReview(${jsArg(name)},"reboot")`, { attrs: 'data-need="admin"' }) : ""].join(" ").trim();
}
const osUpdatesRowPick = (name, f, r) => osUpdatesRunning(r) || f.upgrading || !(f.updates || []).length ? "" :
  `<label class="osu-pick" data-need="admin"><input type="checkbox" data-osu-pick="${esc(name)}" aria-label="${esc(`Select ${name}`)}" onchange="osUpdatesPicked()"></label>`;
// Why the last run did not restart this host, while it still waits for that.
function osUpdatesRowWaiting(name, f, r) {
  const waiting = f.reboot && !osUpdatesRunning(r) && (r.rollout?.results || []).find(x => x.node === name && x.restart_needed);
  return waiting ? `<div class="dim xs">${esc(waiting.note.split("restart not done: ").pop())}</div>` : "";
}
function osUpdatesHostsHtml(r, actions = false) {
  const check = r.checking || {}, busy = check.running ? check.hosts.filter(h => !check.done.includes(h)) : [];
  const rows = Object.entries(r.hosts || {}).sort(([a], [b]) => a.localeCompare(b)).map(([name, f]) => [
    `${actions ? `${osUpdatesRowPick(name, f, r)}<button type="button" class="linkish" onclick="closeModal();nodeDetail(${jsq(name)})"><b>${esc(name)}</b></button>` : `<b>${esc(name)}</b>`}<div class="dim xs">${esc(f.os || "")}${busy.includes(name) ? " · checking now…"
      : f.at ? ` · read ${esc(hostOsAge(f.at))}${actions ? "" : esc(hostOsNext(f.at, r.every_s))}` : ""}${check.failed?.[name] ? ` · <span class="bad">${esc(check.failed[name])}</span>` : ""}</div>${actions ? osUpdatesRowWaiting(name, f, r) : ""}`,
    (f.updates || []).length ? `${f.updates.length}${f.security ? ` ${UI.chip(`${f.security} security`, "warn")}` : ""}` : "up to date",
    f.reboot ? UI.chip("needed", "warn") : "—",
    hostOsAuto(f.auto),
    ...(actions ? [osUpdatesRowAction(name, f, r)] : []),
  ]);
  // Right under the table: the ticks are here, and the dialog's own buttons a long way down.
  const picked = actions && !osUpdatesRunning(r) ? `<div class="osu-picked">${UI.button("Update selected hosts…", "osUpdatesReviewPicked()", { id: "osu_picked", attrs: 'data-need="admin" hidden' })}</div>` : "";
  return rows.length ? UI.table([{ label: "Host" }, { label: "Updates" }, { label: "Restart" }, { label: "Automatic updates" }, ...(actions ? [{ label: "" }] : [])], rows) + picked
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
  const header = actions => `${UI.moduleHeader(`Host updates`, `Operating system packages, security updates and restarts`, `${actions}`)}`;
  try {
    const r = await api("/api/os-updates");
    if ($("#settingsHostUpdates") !== card) return;
    if (!r.applies) { card.innerHTML = header("") + osUpdatesUnavailableHtml(); return; }
    const s = r.settings || {}, schedule = s.schedule || {};
    const run = r.rollout?.status === "running" ? r.rollout : null;
    const last = run ? null : r.rollout || r.last;
    const days = OSU_DAYS.filter(([id]) => (schedule.days || []).includes(id)).map(([, label]) => label).join(", ");
    const windowText = schedule.enabled ? `${days} at ${String(schedule.hour ?? 0).padStart(2, "0")}:00 ${schedule.tz || ""}` : "Weekly window off";
    const check = r.checking || {};
    clearTimeout(window._osuCheckTimer);
    if (check.running) window._osuCheckTimer = setTimeout(() => osUpdatesCardPaint(), 4000);
    card.innerHTML = header(`${can("admin")
      ? UI.button(check.running ? "Checking hosts…" : "Check now", "osUpdatesCheckAll()", { icon: check.running ? "" : "refresh", id: "osuCheckAll", attrs: `data-need="admin"${check.running ? " disabled" : ""}` })
      : UI.button("Reload", "osUpdatesCardPaint()")}${can("admin")
      ? UI.button("Manage host updates", "osUpdates()", { attrs: 'data-need="admin"' }) : UI.chip("admin managed")}`) +
      `<div class="ui-stack">
        ${run ? UI.progress(Math.round((run.index || 0) * 100 / Math.max(1, (run.nodes || []).length)),
          {label: "Updating hosts one at a time", detail: run.message || ""}) : ""}
        ${last ? UI.callout(last.status === "failed" ? "bad" : "info", osUpdatesLastRun(last), `${esc(last.message || "")}${osUpdatesSkipLine(r, "<br>")}`)
          : !run && r.window_skip ? UI.callout("warn", "", osUpdatesSkipLine(r)) : ""}
        ${check.running ? UI.progress(Math.round(check.done.length * 100 / Math.max(1, check.hosts.length)),
          { label: "Reading every host again", detail: `${check.done.length} of ${check.hosts.length} · each refreshes its package lists first` }) : ""}
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

// Every Ready host read again, package lists first (#371); the card
// repaints as each one finishes.
window.osUpdatesCheckAll = async () => {
  const button = $("#osuCheckAll");
  if (button) { button.disabled = true; button.textContent = "Checking hosts…"; }
  try {
    const r = await api("/api/os-updates/check", { method: "POST", headers: { "Content-Type": "application/json" }, body: "{}" });
    toast(r.checking?.hosts?.length ? `Reading ${r.checking.hosts.length} host${r.checking.hosts.length === 1 ? "" : "s"} again` : "No host is ready to read", r.checking?.hosts?.length ? "ok" : "warn");
  } catch (e) { toast(e.message, "bad"); }
  osUpdatesCardPaint();
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
  const s = r.settings, run = osUpdatesRunning(r);
  const tz = Intl.DateTimeFormat().resolvedOptions().timeZone || "this browser's time";
  $("#mbody").innerHTML = `<div class="ui-stack">
    <div id="osu_status">${osUpdatesStatusHtml(r)}</div>
    <div id="osu_hosts">${osUpdatesHostsHtml(r, true)}</div>
    ${UI.section("Settings", `
      <div class="f"><label>Who installs updates ${tip("Ubuntu's unattended-upgrades installs security updates on each host by itself, and with Automatic-Reboot restarts it without a drain; Homestead then holds it off only while it updates hosts. Chosen, Homestead switches it off on every host and installs updates itself, one host at a time, restarting through its review and drain.")}</label>
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
    ${UI.more("What an update does", `<p class="small">Updates run one host at a time, with Homestead's leader last, on every host or the ones you choose. Restarts use Host actions checks: running VMs, a sole etcd member, a volume's only copy or anything that would keep the host from draining can block them, and that host then waits for you. Hosts drain through disruption budgets and resume scheduling once Ready with healthy storage. Failed installs stop the queue. Ubuntu automatic updates pause on every host during the run.</p>`)}
    <div id="osu_run">${osUpdatesRunActions(run)}</div></div>`;
  if (window.applyRole) applyRole();
  osUpdatesFollow(run);
};

const osUpdatesRunning = r => r.rollout && r.rollout.status === "running" ? r.rollout : null;
const osuHosts = n => `${n} host${n === 1 ? "" : "s"}`;
function osUpdatesStatusHtml(r) {
  const run = osUpdatesRunning(r), last = r.rollout && !run ? r.rollout : null;
  const results = rollout => (rollout.results || []).map(x => `<li><b>${esc(x.node)}</b> · ${esc(x.note)}</li>`).join("");
  if (run) return `${UI.progress(Math.round((run.index || 0) * 100 / Math.max(1, run.nodes.length)),
      { label: run.nodes.length === 1 ? `Updating ${run.nodes[0]}` : `Updating ${osuHosts(run.nodes.length)}, one at a time`, detail: run.message || "" })}
    ${results(run) ? `<ul class="osu-results small">${results(run)}</ul>` : ""}
    ${osUpdatesHoldsHtml(run)}`;
  // The last run as a notice: a failed one is an error, the rest information (UI.callout).
  return last ? UI.callout(last.status === "failed" ? "bad" : "info",
      { html: `${esc(osUpdatesLastRun(last))}${last.finished ? ` · ${esc(hostOsAge(last.finished))}` : ""}` },
      `${esc(last.message || "")}${osUpdatesSkipLine(r, last.message ? "<br>" : "")}`, { className: "small" })
    : r.window_skip ? `<div class="note small">${osUpdatesSkipLine(r)}</div>` : "";
}
// The weekly window something was in the way of, until a window or every host runs.
const osUpdatesSkipLine = (r, before = "") => !r.window_skip ? "" : r.window_skip.blip
  ? `${before}<b>Window not started yet: ${esc(r.window_skip.why)}.</b> It looks again every few minutes while its hour lasts.`
  : `${before}<b>Window skipped because ${esc(r.window_skip.why)}</b> · ${esc(hostOsAge(r.window_skip.last_try))}`;
// A hold that has kept failing during the run: that host could restart itself.
function osUpdatesHoldsHtml(run) {
  const failing = Object.entries(run.hold_failed || {}).filter(([, row]) => row.noted);
  return failing.length ? UI.callout("bad", `Automatic updates could not be held on ${failing.map(([name]) => name).join(", ")}`,
    `${failing.length === 1 ? "It" : "They"} could restart by ${failing.length === 1 ? "itself" : "themselves"} during this run; Homestead keeps trying. ${esc(failing[0][1].error || "")}`, { className: "small" }) : "";
}
// A run of the hosts someone chose, not of every host.
const osuSmaller = run => run.reason === "asked-hosts" && (run.nodes || []).length;
const osUpdatesLastRun = last => osuSmaller(last) ? `Last run: ${last.nodes.join(", ")}, ${last.status || "finished"}` : `Last run ${last.status || "finished"}`;
// A run of one host has nothing to stop after, unless it is waiting for its restart.
const osUpdatesRunActions = run => run
  ? (run.node?.waiting ? UI.actions(UI.button("Stop and leave this host to me", "osUpdatesStop()", { attrs: 'data-need="admin"' }))
    : run.nodes.length === 1 ? "" : UI.actions(UI.button("Stop after this host", "osUpdatesStop()", { attrs: 'data-need="admin"' })))
  : UI.actions(UI.button("Update every host now", "osUpdatesStart()", { kind: "pri", attrs: 'data-need="admin"' }));

/* The ticked hosts, and the button that takes them to their review. */
const osUpdatesTicked = () => [...$$("input[data-osu-pick]")].filter(box => box.checked).map(box => box.dataset.osuPick);
window.osUpdatesPicked = () => {
  const button = $("#osu_picked"), count = osUpdatesTicked().length;
  if (!button) return;
  button.hidden = !count;
  button.textContent = `Update ${count} selected host${count === 1 ? "" : "s"}…`;
};
window.osUpdatesReviewPicked = () => osUpdatesReview(osUpdatesTicked());

/* The review before a run of chosen hosts: what installs, whether a restart is
   likely, and what the restart checks say today, before anything starts. A
   restart they would refuse is not offered here: that host would restart before
   it has installed. It installs, it waits, and its row then offers Restart…. */
const OSU_REVIEW_TITLE = nodes => nodes.length === 1 ? `Update ${nodes[0]}` : `Update ${osuHosts(nodes.length)}`;
const OSU_REVIEW_LOADING = '<div id="osuReviewLoading" class="empty"><span class="spin2"></span> checking the hosts</div>';
/* One notice per dialog: a lone host's verdict is the notice, while several
   hosts', or one under "can't start", are plain lines. */
function osUpdatesVerdict(h, plain) {
  const said = text => esc(text.replace(/^./, c => c.toUpperCase()).replace(/\.$/, ""));
  const [kind, title, body] = h.restart_unread ? ["warn", "The restart checks could not be read.", `${esc(h.restart_unread)} The updates still install; a restart is checked when due.`]
    : h.restart_refusal && h.restart_may_clear ? ["info", "If this host needs a restart, Homestead may wait up to 15 minutes first.",
      `${said(h.restart_refusal)}. This can clear by itself; if not, the host waits for you to choose Restart….`]
    : h.restart_refusal ? ["warn", "If this host needs a restart, Homestead will not do it by itself today.",
      `${said(h.restart_refusal)}. The updates install; the host then waits for you to choose Restart….`]
    : ["info", "If this host needs a restart, it can go ahead.", plain ? "" : "It is drained, restarted, and given back once Ready with healthy volumes."];
  return plain ? `<p class="small"><b>${esc(title)}</b>${body ? ` ${body}` : ""}</p>` : UI.callout(kind, title, body);
}
function osUpdatesReviewHost(h, several, plain) {
  const count = h.updates.length;
  const restart = h.reboot ? `${UI.chip("needed", "warn")} <span class="dim xs">${esc(h.reboot_for || "to finish an update")}</span>`
    : h.updates.some(u => HOS_KERNEL.test(u.name)) ? `${UI.chip("likely", "warn")} <span class="dim xs">a kernel update is in the list</span>`
    : "only if an update asks for one";
  return `${UI.facts([["Updates", count ? `${count} waiting ${h.security ? UI.chip(`${h.security} security`, "warn") : ""}` : "up to date"], ["Restart", restart],
      h.homestead_here ? ["Homestead", `${UI.chip("runs on this host", "info")} <span class="dim xs">a restart can stop this page answering for a few minutes; the update carries on.${several ? " It goes last." : ""}</span>`] : null])}
    ${count ? UI.more(`The ${count} update${count === 1 ? "" : "s"}`, `<div class="hos-updates">${h.updates.slice(0, 300).map(u => UI.chip(u.name, u.security ? "warn" : "neutral")).join("")}</div>`, false, `osu-${h.node}`) : ""}
    <div class="osu-verdict">${osUpdatesVerdict(h, plain)}</div>`;
}
function osUpdatesReviewHtml(review, restart) {
  const names = review.hosts.map(h => h.node), several = names.length > 1;
  const others = "Other hosts' automatic updates wait until this ends.";
  return `<div class="ui-stack">
    ${review.blocked ? UI.callout("bad", "This can't start now.", esc(review.blocked)) : ""}
    ${UI.lead(several
      ? `Install what is waiting on ${osuHosts(names.length)}, one at a time: ${names.map(n => `<b>${esc(n)}</b>`).join(", then ")}. A failure stops the run. ${others}`
      : `Install what is waiting on <b>${esc(names[0])}</b>. ${others}`)}
    ${review.hosts.map(h => several ? UI.section(h.node, osUpdatesReviewHost(h, true, true)) : osUpdatesReviewHost(h, false, !!review.blocked)).join("")}
    <div class="f"><label for="osu_review_restart">After installing</label><select id="osu_review_restart" onchange="osUpdatesReviewRestart()">
      <option value="when-needed"${restart === "when-needed" ? " selected" : ""}>Restart ${several ? "a host" : "it"} if an update needs one, drained first</option>
      <option value="never"${restart === "never" ? " selected" : ""}>Do not restart; I will do it</option></select></div>
    ${UI.actions(UI.button("Cancel", "modalBack()", { attrs: 'data-dialog-dismiss="true"' })
      + UI.button(OSU_REVIEW_TITLE(names), "osUpdatesReviewedApply()", { kind: "pri", id: "osu_review_go", attrs: 'data-need="admin"', disabled: !!review.blocked }))}
  </div>`;
}
let osuReviewFlow = null;
// What the person chose for this run, kept across a redraw of the same review.
let osuRestartChosen = null;
const OSU_REVIEW = () => osuReviewFlow ||= UI.reviewFlow({
  // Reading again from the review itself redraws it, not another dialog on top.
  loading: nodes => { osuRestartChosen = $("#osu_review_go") ? $("#osu_review_restart")?.value || null : null; if ($("#osu_review_go")) { $("#osu_review_go").disabled = true; $("#mbody").innerHTML = OSU_REVIEW_LOADING; } else childModal(OSU_REVIEW_TITLE(nodes), OSU_REVIEW_LOADING, true); },
  live: () => !!$("#osuReviewLoading") || !!$("#osu_review_go"),
  preview: nodes => api(`/api/os-updates/preview?${new URLSearchParams(nodes.map(node => ["node", node]))}`),
  render: review => {
    $("#mbody").innerHTML = osUpdatesReviewHtml(review, osuRestartChosen || review.restart_policy);
    if (window.applyRole) applyRole();
    osUpdatesReviewRestart();
    OSU_REVIEW().ready();
  },
  failed: error => { $("#mbody").innerHTML = UI.callout("bad", "These hosts could not be checked.", esc(error.message)); },
  blocked: review => !!review.blocked,
  button: "osu_review_go",
  apply: async review => {
    const button = $("#osu_review_go");
    button.disabled = true;
    button.textContent = "Starting…";
    const r = await api("/api/os-updates/start", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ nodes: review.hosts.map(h => h.node), restart: $("#osu_review_restart").value, review_token: review.review_token }) });
    if (r.operation && window.noteOperation) noteOperation(r.operation);
    toast(r.detail, "ok");
    closeModal(false);
    osUpdates();
    osUpdatesCardPaint();
  },
  conflict: (error, review) => {
    toast(error.message, "warn");
    return OSU_REVIEW().open(review.hosts.map(h => h.node));
  },
});
window.osUpdatesReview = nodes => OSU_REVIEW().open(nodes);
// With "do not restart", what the restart checks say is not this run's question.
window.osUpdatesReviewRestart = () => {
  const never = $("#osu_review_restart")?.value === "never";
  $$(".osu-verdict").forEach(box => { box.hidden = never; });
};
window.osUpdatesReviewedApply = async () => {
  const nodes = OSU_REVIEW().review?.hosts.map(h => h.node);
  try { await OSU_REVIEW().apply(); }
  catch (e) {
    // Refused, or the answer was lost: the review is used up, so show a fresh one.
    toast(e.message, "bad");
    if (nodes) await OSU_REVIEW().open(nodes);
  }
};

/* While an update runs, its progress, the hosts and the buttons follow it -
   without redrawing the settings someone may be editing. */
let osUpdatesTimer = null;
function osUpdatesFollow(run) {
  clearTimeout(osUpdatesTimer);
  if (!run) return;
  osUpdatesTimer = setTimeout(async () => {
    if (!$("#osu_status")) return;                       // the dialog was closed
    let r;
    try { r = await api("/api/os-updates", { keep: true }); } catch (e) { return osUpdatesFollow(run); }
    if (!$("#osu_status")) return;
    const now = osUpdatesRunning(r);
    $("#osu_status").innerHTML = osUpdatesStatusHtml(r);
    $("#osu_hosts").innerHTML = osUpdatesHostsHtml(r, true);
    if (window.applyRole) applyRole();
    if (!now !== !run || !now?.node?.waiting !== !run?.node?.waiting) { $("#osu_run").innerHTML = osUpdatesRunActions(now); if (window.applyRole) applyRole(); }
    if (!now) { osUpdatesCardPaint(); return; }
    osUpdatesFollow(now);
  }, 3000);
}
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
  if (!(await ask("Update every host now, one at a time? Each installs what is waiting; a host whose update asks for a restart is drained and restarted before the next is touched. Apps and VMs move off each host while it restarts. A host whose restart the checks refuse keeps its updates and waits for you.",
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
  } catch (e) {
    // It finished between the last look and the click: show how it ended.
    if (/No OS update is running/.test(e.message)) toast("The update had already finished", "ok");
    else toast(e.message, "bad");
  }
  osUpdates();
  osUpdatesCardPaint();
};
