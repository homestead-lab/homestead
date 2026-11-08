/* Homestead views: the Volumes page, storage classes and the file editor (Monaco, loaded on first use). Split from views-storage.js (#911); a classic script. */
/* ---------------- volumes ---------------- */
/* A reason is worth showing only when something is actually wrong: a detached
   volume has no live health to report, which is not a problem to explain. */
const volumeReason = v => (v.health_reason && v.state === "attached"
  && v.robustness !== "healthy") ? v.health_reason : "";

/* What a volume is for, when nothing is using it right now. Detached alone
   said nothing about whether the data is wanted: a stopped container's
   volume keeps its data for the next start; one nothing refers to is the
   kind to think about deleting. */
function volumeUse(x) {
  if (x.state === "attached") return { kind: "in-use" };
  if (x.unclaimed) return { kind: "unclaimed" };
  if (attachedWorkloads(x).length) return { kind: "in-use" };
  // Its claim's users could not be read: not knowing is not "unused".
  if (x.used_by == null) return { kind: x.pvc_name ? "unknown" : "detached" };
  return x.used_by.length ? { kind: "stopped", by: x.used_by } : { kind: "orphaned" };
}

/* Where a volume's copies are: host and disk, grouped by host. */
function volumeCopiesLine(x) {
  // null: Longhorn's replicas could not be read (#1002).
  if (x.copies === null) return `<span class="dim xs vol-copies" data-tip="${esc(volumeCopiesTip(x))}">copies unknown</span>`;
  const copies = x.copies || [];
  if (!copies.length) return "";
  const hosts = {};
  copies.forEach(c => (hosts[c.node || "?"] ||= []).push(c));
  return `<span class="dim xs vol-copies" data-tip="${esc(volumeCopiesTip(x))}">on ${Object.entries(hosts).map(([node, rows]) =>
    `${esc(node.replace(/^harvester-/, ""))}: ${rows.map(c => `<span class="${c.healthy ? "" : "warn"}">${esc(c.disk)}</span>`).join(" + ")}`).join(" · ")}</span>`;
}
function volumeCopiesTip(x) {
  if (x.copies === null) return "Where its copies are could not be read from Longhorn just now";
  const copies = x.copies || [], want = +x.replicas || 0;
  const where = copies.map(c => `${c.node} · ${c.disk}${c.healthy ? (c.detached ? " (stopped while the volume is detached)" : "") : ` (${c.state || "not running"})`}`).join(", ");
  if (!copies.length) return want === 1 ? "One copy: if its host or disk fails, this volume is gone until they come back" : `${want} copies`;
  const hosts = new Set(copies.map(c => c.node)).size;
  if (copies.length < want) return `${want} copies wanted, ${copies.length} placed (${where}). The rest wait for a host or disk Longhorn may use - its health says why. By default each copy needs a host of its own; a class with "Copies go on: Different disks" lets them share one.`;
  if (want === 1) return `One copy, on ${where}: if that disk or host fails, this volume is gone until it comes back`;
  if (hosts === 1) return `${want} copies, all on ${copies[0].node} (${copies.map(c => c.disk).join(", ")}): a failed disk is survived, a failed host is not`;
  return `${want} copies: ${where}`;
}

function volumeUseCell(x) {
  const use = volumeUse(x);
  if (use.kind === "in-use") return attachedWorkloads(x).length
    ? `<div class="attachlist">${attachedWorkloads(x).map(w => UI.chip(w, "info")).join("")}</div>`
    : '<span class="dim">attached</span>';
  if (use.kind === "stopped") return `<div class="attachlist">${use.by.map(w => UI.chip(`${w.split("/").pop()} · stopped`, "neutral", { tip: `${w} is defined to use this volume and is stopped; the data waits for it to start` })).join("")}</div>`;
  if (use.kind === "orphaned") return UI.chip("orphaned", "warn", { tip: "No container, VM or job refers to this volume. If its data is not wanted, it can be deleted; otherwise attach it to something from its editor" });
  if (use.kind === "unclaimed") return UI.chip("no claim", "warn", { tip: "Its claim is gone and the volume was kept - an old copy from a storage class change, or a claim deleted with its data retained. Nothing can mount it as it is" });
  if (use.kind === "unknown") return UI.chip("could not check", "neutral", { tip: "Homestead could not read what is defined to use this volume, so it is not called unused. Refresh to check again" });
  return '<span class="dim">detached</span>';
}

/* A replica rebuild or a backup restore in flight, as Longhorn's engine reports
   it: the slowest replica's progress, since that is what the volume waits on. */
const volumeBusy = v => v.restore || v.rebuild || null;

// Not meter(): that colours by how full something is, and a restore at 95%
// is nearly done, not nearly out of room.
const volumeProgressBar = pct => `<div class="meter volprogress" role="progressbar" aria-valuenow="${+pct || 0}" aria-valuemin="0" aria-valuemax="100"><span style="width:${Math.min(100, +pct || 0)}%"></span></div>`;

function volumeHealthCell(x) {
  const restore = x.restore, rebuild = x.rebuild;
  if (restore && restore.error) return `${UI.chip("restore failed", "bad")}
    <span class="dim xs volume-reason">${esc(restore.error)}</span>`;
  if (restore) return `${UI.chip(`restoring ${restore.pct}%`, "info", { tip: "Longhorn is copying this volume's data in from a backup; it can be used once the restore finishes" })}
    ${volumeProgressBar(restore.pct)}`;
  if (rebuild && rebuild.error) return `${UI.chip("rebuild failed", "bad")}
    <span class="dim xs volume-reason">${esc(rebuild.error)}</span>`;
  // Rebalancing builds the new copy before the old one goes: nothing is short.
  if (rebuild && x.rebalancing) return `${UI.chip(`moving a copy ${rebuild.pct}%`, "ok", { tip: x.health_reason || "Building a copy on another host before the old one is removed; every copy it had stays whole meanwhile" })}
    ${volumeProgressBar(rebuild.pct)}`;
  if (x.rebalancing && x.state === "attached") return `${UI.chip("healthy", "ok", { tip: x.health_reason || "" })} ${UI.chip("moving a copy", "info")}
    ${volumeShortCell(x)}`;
  if (rebuild) return `${UI.chip(`rebuilding ${rebuild.pct}%`, "neutral", { tip: "Longhorn is copying a replica from a healthy one; the volume is readable and writable meanwhile" })}
    ${volumeProgressBar(rebuild.pct)}
    <span class="dim xs volume-reason">${rebuild.replicas === 1 ? "1 replica" : `${rebuild.replicas} replicas`} catching up</span>`;
  return `${x.state === "attached"
    ? UI.chip(x.robustness, x.robustness === "healthy" ? "ok" : x.robustness === "degraded" ? "warn" : "bad", { tip: x.health_reason || "" })
    // Detached is where a volume sits when nothing is using it - a stopped
    // workload, not a fault. Longhorn calls it detached, so do we.
    : UI.chip("detached", "neutral", { tip: "Nothing is mounting this volume, so Longhorn reports no live replica health" })}
    ${volumeShortCell(x)}
    ${volumeReason(x) ? `<span class="dim xs volume-reason">${esc(x.health_reason)}</span>` : ""}`;
}

/* Longhorn rebuilds a missing copy only while a volume is attached, unless
   offline rebuilding is on. A detached volume a copy short stays that way. */
function volumeShortCell(x) {
  const short = x.copies_short;
  if (!short) return "";
  if (!short.whole) return UI.chip("no whole copy", "bad", { tip: "No whole copy of this volume is left; Longhorn cannot rebuild it" });
  return UI.chip(`${short.whole} of ${short.wanted} copies`, "warn", { tip: `Detached with ${short.whole} of ${short.wanted} copies. Longhorn rebuilds the rest only while the volume is attached, or offline when that is on. Rebuild now is in the row's actions` });
}
/* A volume row's buttons (#506): Rebuild now first when a detached volume is
   short of copies, then Snapshots and Edit, the rest and Delete in ⋯. */
function volumeActions(x) {
  const vol = esc(JSON.stringify(x)), ns = jsq(x.namespace || "lab"), pvc = jsq(x.pvc_name || x.name);
  const rebuild = x.copies_short?.whole ? { label: "Rebuild now", icon: "refresh", run: `volumeRebuild(${jsq(x.name)})`, need: "operator",
    tip: "Rebuild the missing copies now, with the volume attached where nothing can mount it" } : null;
  return actionBar([
    rebuild,
    { label: "Snapshots", icon: "snapshot", run: `lhSnaps(${jsq(x.name)},${pvc})`, tip: "Snapshots and backups of this volume: take one now, or restore" },
    { label: "Edit", icon: "edit", run: `volumeEdit(${vol})`, need: "operator", tip: "Resize or change replicas" },
    { label: "Browse files", icon: "list", run: `fileManager(${ns},${pvc},${x.state === "attached"},${esc(JSON.stringify(x.access_modes || []))})`, need: "admin", tip: "Browse and edit the files on this volume" },
    { label: "Move copies", icon: "move", run: `volumeCopyMove(${vol})`, need: "operator",
      tip: "Move its copies to disks with other tags - an NVMe, say - while it stays in use" },
    { label: "Change storage class", icon: "move", run: `volumeReclass(${vol})`, need: "admin", tip: "Copy it to a volume on another class, under the same name" },
    { label: "Fix ownership", icon: "shield", run: `volumeChown(${ns},${pvc})`, need: "admin", tip: "Hand this volume's files to the user the container runs as" },
    { label: "Delete", icon: "trash", run: `volumeDelete(${vol})`, need: "admin", danger: true, tip: "Review attachment and data-loss impact before deleting" },
  ], { shown: rebuild ? 3 : 2, label: `More actions for ${x.pvc_name || x.name}`, iconOnly: true });
}
window.volumeRebuild = async name => {
  try {
    const r = await api("/api/longhorn/rebuild", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ volume: name }) });
    toast(r.detail, "ok"); refresh(true);
  } catch (e) { toast(e.message, "bad"); }
};

/* When storage fills, from how it has grown (homestead_forecast.py). */
const forecastWhen = days => days < 1 ? "within a day" : `in about ${Math.round(days)} day${Math.round(days) === 1 ? "" : "s"}`;
const forecastDate = day => { const d = new Date(`${day}T00:00:00Z`); return isNaN(d) ? day : d.toLocaleDateString([], { day: "numeric", month: "short", timeZone: "UTC" }); };
const forecastTone = (days, warn = 14) => days <= 3 ? "bad" : days <= warn ? "warn" : "info";

function forecastFor(key) {
  const f = typeof STATE === "undefined" ? null : STATE.data?.forecast;
  const row = (f?.rows || []).find(r => r.key === key);
  return row && row.days_left !== null && row.days_left <= (f.show_days || 60) ? row : null;
}

function forecastCard(f) {
  if (!f) return "";
  const rows = f.rows || [], soon = rows.filter(r => r.days_left !== null && r.days_left <= (f.show_days || 60));
  const pool = rows.find(r => r.kind === "pool");
  const collecting = !rows.some(r => r.days >= (f.min_days || 7));
  const line = r => `<div class="forecast-row">
      ${UI.chip(`Full ${forecastWhen(r.days_left)}`, forecastTone(r.days_left, f.warn_days))}
      <span class="forecast-name"><b>${esc(r.kind === "pool" ? "All Longhorn storage" : r.label)}</b><span class="dim xs">${esc(r.kind === "volume" ? `${r.namespace} · volume` : r.kind === "disk" ? "disk" : "every host")} · ${esc(r.pct)}% full · +${(r.per_day / 1024 ** 3).toFixed(1)} GiB a day · by ${esc(forecastDate(r.full_on))}</span></span></div>`;
  return `<div class="card flat forecast-card"><div class="between"><div><div class="ctitle">Filling up</div>
      <div class="csub">From how each volume, disk and the pool has grown over the last 30 days</div></div></div>
    ${collecting ? `<div class="dim small forecast-empty">Collecting daily usage: forecasts start after ${f.min_days || 7} days of samples.</div>`
      : soon.length ? `<div class="forecast-list">${soon.map(line).join("")}</div>`
      : `<div class="dim small forecast-empty">Nothing is forecast to fill in the next ${f.show_days || 60} days${pool ? ` · all Longhorn storage ${pool.days_left !== null ? `fills ${esc(forecastWhen(pool.days_left))}` : `is ${esc(pool.why || "steady")}`}` : ""}.</div>`}</div>`;
}

function volumeUsageCell(x) {
  const fs = x.filesystem;
  const soon = forecastFor(`volume:${x.name}`);
  const known = fs && Number.isFinite(fs.used_gb) && Number.isFinite(fs.capacity_gb)
    && Number.isFinite(fs.used_pct) && fs.used_pct >= 0 && fs.used_pct <= 100
    && fs.capacity_gb > 0 && fs.used_gb >= 0 && fs.used_gb <= fs.capacity_gb;
  return `<div class="volusage-body">${known
    ? `<div class="volusage-line" data-tip="Filesystem usage reported by kubelet; filesystem capacity can be slightly smaller than the provisioned block device.">${meter(fs.used_pct)}<span class="mono">${esc(fs.used_gb)} / ${esc(fs.capacity_gb)} GiB files</span></div>`
    : `<span class="dim xs" data-tip="No fresh filesystem measurement is available. Detached volumes and some shared/raw-block mounts do not report filesystem usage.">Filesystem usage unavailable</span>`}
    ${soon ? `<span class="forecast-tag">${UI.chip(`Full ${forecastWhen(soon.days_left)}`, forecastTone(soon.days_left), { tip: `Growing about ${(soon.per_day / 1024 ** 3).toFixed(1)} GiB a day over the last weeks; full by ${forecastDate(soon.full_on)} at that rate` })}</span>` : ""}
    <span class="dim xs mono volsizes"><span data-tip="Provisioned logical volume size; this is not physical storage consumed.">${esc(x.size_gb)} GiB provisioned</span> · <span data-tip="Longhorn block footprint, including snapshots and untrimmed blocks. Not filesystem usage, and not a sum across replicas; it can exceed the provisioned size.">${esc(x.actual_gb ?? "—")} GiB Longhorn footprint ${tip("Longhorn includes snapshots and allocated blocks, not just current files. Inspect snapshots before choosing any cleanup; deleting them removes recovery points.")}</span></span></div>`;
}

/* While anything rebuilds or restores, re-read the volumes every few seconds
   and repaint only their Health and Usage cells, so the percentages move
   without the table jumping. The request is an ordinary one: leaving the page
   abandons it, which ends the loop, and coming back starts it again. */
function volumeProgressWatch() {
  clearTimeout(window.__volProgressTimer);
  if (!(STATE.data.vols || []).some(volumeBusy)) return;
  window.__volProgressTimer = setTimeout(async () => {
    if (STATE.view !== "storage") return;
    // A hidden tab reads nothing (#983): a rebuild can last hours. Showing
    // the tab starts the watch again.
    if (document.hidden) return document.addEventListener("visibilitychange", volumeProgressResume);
    let vols;
    try { vols = await api("/api/volumes"); } catch (e) { return volumeProgressWatch(); }
    if (STATE.view !== "storage") return;
    STATE.data.vols = vols;
    for (const x of vols) {
      const row = document.querySelector(`#views tr[data-vol="${CSS.escape(x.name)}"]`);
      if (!row) continue;
      const health = row.querySelector("td.volhealth"), usage = row.querySelector("td.volusage");
      if (health) {
        health.innerHTML = volumeHealthCell(x);
        health.classList.toggle("hasreason", !!(volumeReason(x) || volumeBusy(x)));
      }
      if (usage) usage.innerHTML = volumeUsageCell(x);
      // Rebuild now comes and goes with the copies; an open ⋯ is left alone.
      const actions = row.querySelector("td.volactions");
      if (actions && !actions.querySelector("details[open]")) actions.innerHTML = volumeActions(x);
    }
    volumeProgressWatch();
  }, 4000);
}
function volumeProgressResume() {
  if (document.hidden) return;
  document.removeEventListener("visibilitychange", volumeProgressResume);
  if (STATE.view === "storage") volumeProgressWatch();
}

/* Claims on classes that are not Longhorn - k3s's local-path, NFS - which
   Longhorn's list above leaves out, and why one is stuck. */
/* Windows refuses guest shares by default, from its side: since Windows 10
   it blocks insecure guest logons, and Windows 11 24H2 requires signing,
   which a guest session cannot do. Nothing on the server changes that; the
   PC's own settings do. */
function shareGuestWindowsNote() {
  return `<div class="note small"><b>Windows refuses guest shares until two of its own settings change.</b>
    In PowerShell as administrator on each PC:
    ${(cmd => window.guideCopy ? guideCopy(cmd) : `<pre class="mono small wrap-pre">${esc(cmd)}</pre>`)("Set-SmbClientConfiguration -EnableInsecureGuestLogons $true -RequireSecuritySignature $false -Force")}
    <span class="dim xs">The first allows sign-in-free shares (off since Windows 10); the second stops Windows 11 24H2
    requiring signed sessions, which a guest cannot sign. Both apply to every server that PC connects to - a share with
    a user and password needs neither.</span></div>`;
}

function otherVolumesHtml(rows) {
  if (!(rows || []).length) return "";
  const stuck = rows.filter(r => r.phase !== "Bound");
  return `<div class="between" style="margin-top:22px"><div class="sec">Other volumes <span class="dim">${rows.length}</span>
      ${tip("Volumes on storage classes other than Longhorn, such as k3s's local-path. They have no replicas, snapshots or backups here; their data lives where that class keeps it.")}</div></div>
    ${stuck.length ? `${UI.callout("warn", { html: `${stuck.length} volume${stuck.length === 1 ? " is" : "s are"} not made.` }, `Usually a shared (ReadWriteMany) volume asked of a class that serves one host - local-path does - which never makes it. Delete it and make it again on a class that shares, or as a single-node volume.`, { className: "small", attrs: 'style="margin-bottom:10px"' })}` : ""}
    <div class="card flat pad0"><div class="tblwrap"><table class="tbl stack dense"><thead><tr><th>Volume</th><th>Class</th><th>Size</th><th>Mode</th><th>State</th><th></th></tr></thead><tbody>
    ${rows.map(r => `<tr><td><b>${esc(r.name)}</b><div class="dim xs">${esc(r.namespace)}</div></td>
      <td data-label="Class" class="mono">${esc(r.storage_class)}</td><td data-label="Size" class="mono">${esc(r.size || "—")}</td>
      <td data-label="Mode">${UI.chip(r.access_modes.map(m => m === "ReadWriteMany" ? "RWX" : m === "ReadWriteOnce" ? "RWO" : m).join(", ") || "?", "neutral")}</td>
      <td data-label="State">${UI.chip(r.phase === "Bound" ? "in use" : r.phase || "unknown", r.phase === "Bound" ? "ok" : "bad")}
        ${r.reason ? `<div class="dim xs" style="margin-top:4px">${esc(r.reason)}</div>` : ""}</td>
      <td data-actions>${actionBar([{ label: "Delete", icon: "trash", run: `volumeDelete(${esc(JSON.stringify({ name: r.volume || "", pvc_name: r.name, namespace: r.namespace }))})`,
        need: "admin", danger: true, tip: "Review what uses it and what would be lost before deleting" }], { shown: 0, label: `Actions for ${r.name}` })}</td></tr>`).join("")}</tbody></table></div></div>`;
}

/* The page is drawn as its reads return, not once all of them have (#705).
   Every read starts at once; the page is drawn when the volumes answer, and
   each other part - the totals, the forecast, old copies, other volumes - fills
   in when its own read does. Until then it says it is being read. A refresh
   keeps each part's last answer on screen until the new one arrives; another
   visit to the page starts afresh. Parts that answer together are drawn in one
   paint, and a load overtaken by a newer one draws nothing. */
let storageLoad = { id: 0, token: null, parts: {}, missed: {} };
const partReading = what => `<div class="empty small"><span class="spin2"></span> Reading ${what}…</div>`;

async function viewStorage() {
  if (platformLacks("longhorn", "Volumes")) return;
  const token = window.NAV_TOKEN;
  // Same visit: last answers stay until replaced. Another visit: nothing kept.
  const same = storageLoad.token === token;
  const load = storageLoad = { id: storageLoad.id + 1, token,
    parts: { ...(same ? storageLoad.parts : {}) }, missed: { ...(same ? storageLoad.missed : {}) } };
  const current = () => storageLoad === load;
  // A part of the page that could not be read is named above the page, not
  // silently left out (#620): no old copies listed is not no old copies. A
  // failure is kept like an answer, so it is still named while it is read again.
  let volumes = null, queued = null, broken = null;
  const draw = () => {
    if (queued || !current() || !volumes) return;
    queued = new Promise(resolve => setTimeout(() => {
      queued = null;
      try { if (current()) storagePaint(volumes, load.parts, Object.values(load.missed)); }
      catch (e) { broken = broken || e; }
      resolve();
    }, 0));
  };
  // A paint that throws fails the load, as before, rather than leaving it unsettled.
  const drawn = async () => { while (queued) await queued; if (broken) throw broken; };
  // The path comes first and is written out, so the API contract test reads it.
  const part = (path, key, label, fallback) => api(path)
    .then(answer => { delete load.missed[key]; return answer; },
      e => { load.missed[key] = `${label}: ${e.message}`; return fallback; })
    .then(answer => { load.parts[key] = answer; draw(); });
  const parts = [part("/api/storage", "st", "totals", null), part("/api/storage/classes", "classes", "storage classes", []),
    part("/api/storage/v2", "v2", "the V2 engine", null), part("/api/longhorn/capacity", "cap", "capacity", null),
    part("/api/storage/forecast", "forecast", "the forecast", null), part("/api/volumes/old-copies", "oldCopies", "old copies", []),
    part("/api/volumes/other", "others", "other volumes", [])];
  volumes = await api("/api/volumes");
  if (!current()) return;
  draw();
  await drawn();
  // Settled when the whole page is drawn, as before, for whoever waits on it.
  await Promise.all(parts);
  await drawn();
}

function storagePaint(v, parts, missed) {
  const has = key => key in parts;
  const { st, classes, v2, cap, forecast } = parts;
  const allOldCopies = parts.oldCopies || [], allOthers = parts.others || [];
  if (has("forecast")) STATE.data.forecast = forecast;
  STATE.data.lhcap = cap || STATE.data.lhcap;
  if (has("classes")) STATE.data.storageClasses = classes;
  if (has("v2")) STATE.data.v2 = v2;
  STATE.data.vols = v;
  const q = STATE.q.toLowerCase();
  // A search narrows every list on the page, not only Longhorn's.
  const matches = (...fields) => !q || fields.some(f => String(f || "").toLowerCase().includes(q));
  const oldCopies = allOldCopies.filter(o => matches(o.was, o.pv));
  const others = allOthers.filter(r => matches(r.name, r.namespace));
  const spare = v.filter(x => ["orphaned", "unclaimed"].includes(volumeUse(x).kind));
  const onlySpare = STATE.volSpare && spare.length;
  const rows = v.filter(x => (!q || x.name.includes(q) || (x.node || "").includes(q) ||
    (x.pvc_name || "").includes(q) || (x.attached_to || "").toLowerCase().includes(q))
    && (!onlySpare || spare.includes(x)));
  paint(`${UI.pageHeader(`Volumes`, `${v.length} Longhorn volume${v.length === 1 ? "" : "s"} · replicated block storage${onlySpare ? ` · <a class="linkish" onclick="STATE.volSpare=false;viewStorage()">showing ${spare.length} unused · show all</a>` : ""}`, `${moreMenu([spare.length ? { label: onlySpare ? "Show all volumes" : `Show the ${spare.length} unused`, icon: "list", run: "STATE.volSpare=!STATE.volSpare;viewStorage()",
          tip: "Volumes nothing is defined to use - no container, VM or job - and ones kept after their claim went: the ones to look at when freeing space" } : null,
        { label: "Rebalance copies…", icon: "layers", run: "volumeRebalance()", need: "operator", tip: "Move volume copies so hosts hold similar amounts; nothing restarts" },
        { label: "Storage classes", icon: "disk", run: "settingsTab('hardware');go('settings')", tip: "What new volumes are made from: in Settings › Hardware and storage" }])}
      <button class="btn pri" data-need="operator" onclick="volumeCreate()">＋ Create volume</button>`)}
  ${UI.notices(missed.map(text => ["warn", text]), "Parts of this page could not be read")}
  ${!has("st") ? summaryLine("volumes", [`<span class="dim small"><span class="spin2"></span> Reading the totals…</span>`])
  : st ? summaryLine("volumes", [
      `<b>${st.avail_gb} GiB</b> free of ${st.cap_gb}`, `<b>${st.provisioned_gb} GiB</b> provisioned`,
      `${UI.chip(`${st.healthy} healthy`, "ok")}${st.degraded ? ` ${UI.chip(`${st.degraded} degraded`, "warn")}` : ""}${st.faulted ? ` ${UI.chip(`${st.faulted} faulted`, "bad")}` : ""}`,
      `${st.attached} of ${st.volumes} attached`],
    `<div class="grid g4 statgrid">
    <div class="card flat"><div class="ctitle">Free space</div>
      <div class="bignum" style="margin-top:8px">${st.avail_gb}<span class="unit">GiB</span></div>
      <div class="csub">of ${st.cap_gb} GiB raw</div>${meter(st.used_pct, 'style="margin-top:10px"')}</div>
    <div class="card flat"><div class="ctitle">Provisioned</div>
      <div class="bignum" style="margin-top:8px">${st.provisioned_gb}<span class="unit">GiB</span></div>
      <div class="csub" data-tip="Sum of Longhorn volume block footprints, including snapshots; not filesystem usage or total storage across all replica copies.">${st.actual_gb} GiB Longhorn footprint</div></div>
    <div class="card flat statwide"><div class="ctitle">Replica health</div>${(st.reasons || []).length
      ? `<div class="dim xs volume-reason" style="margin-top:8px">${esc(st.reasons[0].name)}: ${esc(st.reasons[0].reason)}${st.reasons.length > 1 ? ` · and ${st.reasons.length - 1} more` : ""}</div>` : ""}
      <div class="row" style="margin-top:10px;gap:8px;flex-wrap:wrap">
        ${UI.chip(`${st.healthy} healthy`, "ok")}
        ${st.degraded ? UI.chip(`${st.degraded} degraded`, "warn") : ""}
        ${st.faulted ? UI.chip(`${st.faulted} faulted`, "bad") : ""}
        ${(st.detached ?? st.unknown) ? UI.chip(`${st.detached ?? st.unknown} detached`, "neutral") : ""}</div>
      <div class="csub" style="margin-top:10px">${st.attached} attached of ${st.volumes}</div></div>
    ${lhCapacityCard(cap, st)}
  </div>`, "Per host") : ""}
  ${has("forecast") ? forecastCard(forecast) : `<div class="card flat forecast-card"><div class="ctitle">Filling up</div>${partReading("the forecast")}</div>`}
  <div class="card flat pad0"><div class="tblwrap voltable"><table data-sort="volumes" class="tbl dense"><thead><tr>
   <th>Volume</th><th>Attached to</th><th>Health</th><th>Mode</th><th>Usage</th><th data-nosort>Last used</th><th></th>
   </tr></thead><tbody>${rows.map(x => `<tr data-vol="${esc(x.name)}"${clusterAttr(x)}>
     <td class="volname"><div class="volname-content"><b>${esc(x.pvc_name || x.name.slice(0, 18))}</b> ${clusterTag(x)}
       <span class="dim xs mono">${esc(x.namespace || "")}${x.node ? ` · ${esc(x.node.replace("harvester-", ""))}` : ""}</span>
       ${volumeCopiesLine(x)}</div></td>
     <td data-label="Attached to">${volumeUseCell(x)}${x.pod_status ? `<span class="dim xs"> · ${esc(x.pod_status)}</span>` : ""}</td>
     <td data-label="Health" class="volhealth${volumeReason(x) || volumeBusy(x) ? " hasreason" : ""}">${volumeHealthCell(x)}</td>
     <td data-label="Mode">${UI.chip((x.access_modes || ["?"]).map(m => m === "ReadWriteMany" ? "RWX" : m === "ReadWriteOnce" ? "RWO" : m).join(", "), "neutral")}
       ${UI.chip(`×${x.replicas}`, +x.replicas === 1 || (x.copies?.length && x.copies.length < +x.replicas) ? "warn" : "neutral", { tip: volumeCopiesTip(x) })} ${UI.chip(x.engine === "v2" ? "V2" : "V1", x.engine === "v2" ? "info" : "neutral", { tip: x.engine === "v2" ? "Longhorn's V2 data engine (SPDK)" : "Longhorn's V1 data engine - the default" })}</td>
     <td data-label="Usage" class="volusage">${volumeUsageCell(x)}</td>
     <td data-label="Last used" class="small dim">${x.state === "attached" ? UI.chip("in use", "neutral") : esc(fmtAgo(x.last_used_secs))}</td>
     <td class="volactions" data-actions>${volumeActions(x)}</td>
      </tr>`).join("") || `<tr><td colspan=7 class="empty">No volumes to show.</td></tr>`}
   </tbody></table></div></div>
  ${oldCopies.length ? `<div class="sec">Old copies ${tip("The original of a volume moved to another storage class, kept in case the new copy disappoints. Remove each once its app works on the new one.")}</div>
    <div class="card flat pad0"><div class="tblwrap"><table class="tbl stack dense"><thead><tr><th>Was</th><th>Class</th><th>Size</th><th></th></tr></thead><tbody>
    ${oldCopies.map(o => `<tr><td><b>${esc(o.was)}</b><div class="dim xs mono">${esc(o.pv)}</div></td><td>${esc(o.storage_class)}</td><td class="mono">${esc(o.size)}</td>
      <td data-actions>${actionBar([{ label: "Remove", run: `reclassRemoveOld(${jsq(o.pv)})`, need: "admin", danger: true }])}</td></tr>`).join("")}</tbody></table></div></div>` : ""}
  ${otherVolumesHtml(others)}`);
  volumeProgressWatch();
}
window.volumeCreate = async () => {
  const [nss, classes] = await Promise.all([api("/api/namespaces"),
    api("/api/storageclasses?facts=1").catch(e => ({ unread: e.message }))]);
  // No class list is not "pick none": the server would choose one (#620).
  if (classes.unread) {
    modal("Create volume", UI.callout("bad", "Could not read the storage classes", esc(classes.unread)) + UI.actions(UI.cancel("Close")));
    return;
  }
  const scs = classes.names || [];
  window.__volumeClasses = classes;
  modal("Create volume", `<div class="f2"><div class="f"><label>Name</label><input id="vc_name" placeholder="frigate-media"></div>
    <div class="f"><label>Namespace</label><select id="vc_ns">${nss.map(n => `<option ${n === "lab" ? "selected" : ""}>${esc(n)}</option>`).join("")}</select></div></div>
    <div class="f2"><div class="f"><label>Size (GiB)</label><input id="vc_size" type="number" min="1" value="10"></div>
    <div class="f"><label>Access mode ${tip("RWO mounts on one node at a time and suits most apps. RWX can mount on several nodes, using Longhorn's shared-volume support.")}</label><select id="vc_mode"><option value="ReadWriteOnce">RWO · one node</option><option value="ReadWriteMany">RWX · many nodes</option></select></div></div>
    <div class="f"><label>Storage class</label><select id="vc_sc" onchange="volumeClassFacts()">${storageClassOptions(scs, defaultStorageClass(scs, "", classes.facts), classes.facts)}</select>
      <div class="vclass-badges" id="vc_badges"></div></div>
    <div class="note" id="vc_mode_note" hidden></div>
    ${UI.actions(`<button class="btn pri" onclick="volumeCreateNow()">Create volume</button><button data-dialog-dismiss="true" class="btn" onclick="closeModal()">Cancel</button>`)}`);
  $("#vc_mode").addEventListener("change", volumeClassFacts);
  volumeClassFacts();
};
window.volumeCreateNow = async () => {
  const body = { name: $("#vc_name").value.trim(), namespace: $("#vc_ns").value, size_gb: +$("#vc_size").value,
    access_mode: $("#vc_mode").value, storage_class: $("#vc_sc").value };
  try { await api("/api/volumes/create", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
    toast(`${body.name} created`, "ok"); closeModal(); resetPaint(); viewStorage(); } catch (e) { toast(e.message, "bad"); }
};
window.volumeEdit = async (x, fromRoute = false) => {
  if (!fromRoute && window.setModalRoute) setModalRoute({ panel: "edit", ns: x.namespace || "lab", volume: x.pvc_name || x.name }, x.pvc_name || x.name);
  const namespace = x.namespace || "lab", name = x.pvc_name || x.name;
  modal("Edit · " + name, '<div id="ve_loading" class="empty"><span class="spin2"></span> Checking volume expansion…</div>');
  const loading = $("#ve_loading");
  let options;
  try {
    options = await api(`/api/volumes/edit-options?ns=${encodeURIComponent(namespace)}&name=${encodeURIComponent(name)}`);
  } catch (e) {
    if ($("#ve_loading") === loading && !$("#modal").classList.contains("hidden"))
      modal("Edit · " + name, `<div class="note">Could not check this volume. ${esc(e.message)}</div><button class="btn" onclick="closeModal()">Close</button>`);
    return;
  }
  if ($("#ve_loading") !== loading || $("#modal").classList.contains("hidden")) return;
  const size = options.requested_gb || Math.ceil(x.size_gb);
  modal("Edit · " + name, `
  <div class="f2"><div class="f"><label>Size (GiB) ${tip("Volumes can be grown but not shrunk.")}</label><input id="ve_size" type="number" min="${size}" value="${size}" ${options.can_expand ? "" : "disabled"}></div>
  <div class="f"><label>Replica count</label><input id="ve_reps" type="number" min="1" max="5" value="${x.replicas}"></div></div>
  ${options.can_expand ? "" : `<div class="note"><b>Size cannot be changed.</b> ${esc(options.reason)}<br>Replica count can still be changed.
    ${options.repair_class ? `<div class="row" style="margin-top:10px"><button class="btn sm" data-need="admin" onclick="volumeClassRepairReview(${jsq(namespace)},${jsq(name)},${jsq(options.storage_class)})">Repair resize support…</button></div>` : ""}</div>`}
  <div class="note"><b>${esc((x.access_modes || []).join(", ") || "Access mode unknown")}</b> · ${esc(options.storage_class || "No StorageClass")}<br>
  Kubernetes locks access mode and storage class after a claim is bound. To change RWO ↔ RWX, create a new volume and migrate the data.</div>
  ${UI.actions(`<button class="btn pri" onclick="volumeEditNow(${jsq(x.namespace || "lab")},${jsq(x.pvc_name || x.name)})">Save</button><button data-dialog-dismiss="true" class="btn" onclick="closeModal()">Cancel</button>`)}`);
};
window.volumeEditNow = async (namespace, name) => {
  const body = { namespace, name, replicas: +$("#ve_reps").value };
  if (!$("#ve_size").disabled) body.size_gb = +$("#ve_size").value;
  try { const r = await api("/api/volumes/edit", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
    toast(r.detail || `${name} updated`, "ok"); closeModal(); resetPaint(); viewStorage(); } catch (e) { toast(e.message, "bad"); }
};

window.volumeClassRepairReview = (namespace, name, storageClass) => {
  modal("Repair resize support", `<div class="note">Recreate <b>${esc(storageClass)}</b> from the bound Longhorn volume's storage settings and enable volume expansion.
    This recreates Kubernetes configuration for every volume using that class. No backup is restored, and volume sizes and data stay unchanged.</div>
    ${UI.actions(`<button class="btn pri" data-need="admin" onclick="volumeClassRepairNow(${jsq(namespace)},${jsq(name)},${jsq(storageClass)},this)">Repair resize support</button><button data-dialog-dismiss="true" class="btn" onclick="closeModal()">Cancel</button>`)}`);
};
window.volumeClassRepairNow = async (namespace, name, storageClass, button) => {
  button.disabled = true;
  try {
    const r = await api("/api/volumes/repair-class", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ namespace, name, confirm: storageClass }) });
    toast(r.detail, "ok"); closeModal(); resetPaint(); viewStorage();
  } catch (e) { toast(e.message, "bad"); button.disabled = false; }
};

function volumeDeleteConfirmationValid(name, value) {
  return String(value || "").trim() === String(name || "");
}
window.volumeDeleteConfirmationValid = volumeDeleteConfirmationValid;

/* A Released volume whose claim name is in use again (#932), beside the
   volume that claim uses now: another PV, another UID, another Longhorn
   volume. The delete is of the left column only. */
function releasedBesideCurrent(p) {
  const pv = p.pv || {}, now = p.current || {}, mono = v => `<span class="mono">${esc(v || "—")}</span>`;
  const users = (now.consumers || []).map(c => `${esc(c.kind)} · ${esc(c.name)}`).join("<br>") || '<span class="dim">nothing</span>';
  return UI.section(`${esc(p.namespace)}/${esc(p.name)} is in use again`, UI.table(
    [{ label: "" }, { label: "Released" }, { label: "In use now" }], [
      ["Persistent volume", mono(pv.name), mono(now.pv)],
      ["Its UID", mono(pv.uid), mono(now.pv_uid)],
      ["Phase", esc(pv.phase || "?"), esc(now.phase || "?")],
      ["Claim UID", mono(pv.claim_uid), mono(now.claim_uid)],
      ["Longhorn volume", mono(pv.longhorn), mono(now.longhorn)],
      ["Used by", p.longhorn?.state === "detached" && !p.longhorn?.attached_node ? '<span class="dim">nothing: detached</span>' : `Longhorn: ${esc(p.longhorn?.state || "unknown")}`, users],
    ]) + UI.more("How the delete is pinned", "<p>Only the Released volume is deleted, pinned by its name, UID and version. The volume the claim uses now is not the one deleted, and is never changed.</p>"));
}

let volumeDeleteTarget = null, volumeDeleteRequest = 0;
window.volumeDeleteRefresh = () => volumeDeleteTarget && volumeDelete(volumeDeleteTarget);
window.volumeDelete = async x => {
  const request = ++volumeDeleteRequest;
  volumeDeleteTarget = {name: x.name, pvc_name: x.pvc_name, namespace: x.namespace, ...(x.pv ? {pv: x.pv} : {})};
  window.__volumeDeletePlan = null;
  const namespace = x.namespace || "lab", name = x.pvc_name || x.name;
  // A Released volume is asked for by its PV (#932): its claim name may be
  // another volume's now, and only the server can say which is which.
  modal(x.pv ? "Delete Released volume · " + x.pv : "Delete volume · " + name,
    '<div id="volumeDeleteReview" class="empty"><span class="spin2"></span> checking mounts, snapshots, backups and reclaim policy…</div>', true);
  const waiting = $("#volumeDeleteReview");
  let p;
  try {
    p = await api(`/api/volumes/delete-plan?ns=${encodeURIComponent(namespace)}&name=${encodeURIComponent(name)}&volume=${encodeURIComponent(x.name)}${x.pv ? `&pv=${encodeURIComponent(x.pv)}` : ""}`);
  } catch (e) {
    if (request !== volumeDeleteRequest || $("#volumeDeleteReview") !== waiting) return;
    return void ($("#mbody").innerHTML = UI.callout("bad", "Impact check failed.", esc(e.message)) +
      UI.actions(UI.cancel("Close") + UI.button("Refresh review", "volumeDeleteRefresh()")));
  }
  if (request !== volumeDeleteRequest || $("#volumeDeleteReview") !== waiting) return;
  window.__volumeDeletePlan = p;
  const stale = new Set((p.stale_consumers || []).map(c => `${c.kind}/${c.name}`));
  const lh = p.longhorn || {}, pv = p.pv || {};
  const blocked = p.blocked;
  const permanentBlocked = !p.actions?.delete_data?.enabled;
  const detached = !!p.actions?.detach?.complete;
  const jobs = p.removable_jobs || [];
  const verifyPending = !detached && p.inventory_complete === false && lh.state === "detached" &&
    !lh.attached_node && !(p.consumers || []).length;
  const incomplete = (p.warnings || []).length
    ? `<p><b>Incomplete impact inventory</b></p><ul class="ui-list">${p.warnings.map(w => `<li>${esc(w)}</li>`).join("")}</ul>` : "";
  const consumers = (p.consumers || []).map(c => [
    `${esc(c.kind)} · ${esc(c.name)}${stale.has(`${c.kind}/${c.name}`) ? ` ${UI.chip("finished")}` : c.active ? ` ${UI.chip("active", "bad")}` : ""}<span class="sub">${esc(c.namespace)} · ${esc(c.detail || (c.active ? "active" : "inactive"))}</span>`,
    `<span class="mono small">${(c.mounts || []).map(m => `${esc(m.container)}:${esc(m.path || m.container_kind)}${m.read_only ? " · read-only" : ""}`).join("<br>") || "claim reference"}</span>`]);
  const count = (value, one, many) => `${esc(value)} ${value === 1 ? one : many}`;
  $("#mbody").innerHTML = [
    blocked
      ? UI.callout("bad", "Deletion is blocked.", `<ul class="ui-list">${p.blocking_reasons.map(r => `<li>${esc(r)}</li>`).join("")}</ul>${incomplete}`)
      : incomplete ? UI.callout("warn", "Impact check passed, with gaps.", incomplete)
      : UI.callout("ok", "Impact check passed.", p.released ? `This is not the volume ${esc(p.name)} uses now; that one is not changed.`
        : p.orphan ? "The PVC is already gone. Deleting this detached volume removes its data for good."
        : "Detached, and no app or VM uses it. Choose what happens to its data."),
    p.released ? releasedBesideCurrent(p) : "",
    UI.facts([
      ["Longhorn footprint", `${esc(lh.actual_gb ?? "?")} GiB`],
      ["Replicas", esc(lh.replicas ?? "?")],
      ["Snapshots on volume", esc(p.snapshots?.count ?? "?")],
      ["External backups", esc(p.backups?.count ?? "?")],
    ]),
    UI.section("1. Detach", UI.checklist([
      { state: detached ? "ok" : verifyPending ? "warn" : "bad",
        title: detached ? "Already detached." : verifyPending ? "Longhorn reports detached; safety checks are incomplete." : "Stop and unmount this claim first.",
        detailHtml: verifyPending ? "Refresh the review to verify the remaining checks before deleting." :
          `Detaching keeps the PVC and data. Homestead does not stop the apps and VMs below for you.${(p.stale_consumers || []).length ? " Finished ones do not hold it." : ""}` },
      jobs.length && { state: "ok", title: `Finished ${jobs.length === 1 ? "job" : "jobs"} will be removed first`,
        detailHtml: `<span class="mono">${jobs.map(esc).join(", ")}</span> finished, usually an import's copy job. Otherwise Kubernetes could hold the claim in Terminating.` },
    ]) + (consumers.length ? UI.table([{ label: "Used by" }, { label: "Mounts" }], consumers)
      : `<div class="ui-empty">${p.inventory_complete === false ? "No app or VM references found in the available inventory." : "No pods, controllers, jobs or virtual machines reference this claim."}</div>`)),
    UI.section("2. Choose what happens to the data", `<div class="volume-delete-grid">
      <label class="volume-delete-option ${blocked || p.orphan ? "disabled" : ""}"><input type="radio" name="vd_action" value="delete_claim" onchange="volumeDeleteGate()" ${blocked || p.orphan ? "disabled" : ""}>
        <span><b>Delete claim, keep data</b><small>The PV is set to Retain, then the PVC is deleted. Recovering or removing the data later is done in Kubernetes or Longhorn.</small></span></label>
      <label class="volume-delete-option danger ${permanentBlocked ? "disabled" : ""}"><input type="radio" name="vd_action" value="delete_data" onchange="volumeDeleteGate()" ${permanentBlocked ? "disabled" : ""}>
        <span><b>Permanently delete data</b><small>${p.orphan ? "Longhorn removes what is left in the normal way; finalizers are never forced." : "The PVC and its PV are removed."} The Longhorn volume, replicas and ${count(p.snapshots?.count || 0, "on-volume snapshot are", "on-volume snapshots are")} removed; ${count(p.backups?.count || 0, "external backup remains", "external backups remain")}.</small></span></label>
    </div>`),
    UI.more("Objects affected", UI.facts([
      ["Claim", `<span class="mono">${esc(p.namespace)}/${esc(p.name)}</span> · ${esc(p.phase)} · ${esc(p.requested_storage || "size unknown")}`],
      ["Persistent volume", `<span class="mono">${esc(pv.name || "not bound")}</span> · reclaim policy ${esc(pv.reclaim_policy || "unknown")}`],
      ["Longhorn volume", `<span class="mono">${esc(lh.name || "not found")}</span> · ${esc(lh.state || "unknown")}${lh.attached_node ? ` on ${esc(lh.attached_node)}` : ""}`],
    ])),
    UI.section("3. Confirm", UI.typedConfirm({ id: "vd_confirm", expect: p.confirm || name, oninput: "volumeDeleteGate()" })),
    UI.actions(UI.cancel() + (blocked || p.inventory_complete === false ? UI.button("Refresh review", "volumeDeleteRefresh()") : "") +
      UI.button("Delete selected", `volumeDeleteNow(${jsArg(namespace)},${jsArg(name)},${jsArg(p.uid)})`,
      { kind: "danger", id: "vd_go", disabled: true, attrs: 'data-need="admin"' })),
  ].join("");
};

window.volumeDeleteGate = () => {
  const p = window.__volumeDeletePlan;
  const action = document.querySelector('input[name="vd_action"]:checked')?.value || "";
  const input = $("#vd_confirm"), button = $("#vd_go");
  if (!p || !input || !button) return;
  const allowed = action && !p.blocked && p.actions?.[action]?.enabled;
  button.disabled = !allowed || !volumeDeleteConfirmationValid(p.confirm || p.name, input.value);
  button.textContent = action === "delete_claim" ? "Delete claim · keep data" : action === "delete_data" ? "Permanently delete data" : "Delete selected";
};

window.volumeDeleteNow = async (namespace, name, uid) => {
  const action = document.querySelector('input[name="vd_action"]:checked')?.value || "";
  const confirmation = $("#vd_confirm").value.trim(), plan = window.__volumeDeletePlan;
  if (!volumeDeleteConfirmationValid(plan?.confirm || name, confirmation)) return;
  if (!action) return toast("Choose whether to retain or permanently delete the backing data", "bad");
  const button = $("#vd_go"); button.disabled = true; button.textContent = "Requesting deletion…";
  try {
    const result = await api("/api/volumes/delete", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ namespace, name, uid, action, confirmation,
        volume: plan?.longhorn?.name || "", pv_uid: plan?.pv?.uid || "", pv: volumeDeleteTarget?.pv || "" }) });
    toast(result.message || `${name} deletion started`, "ok"); closeModal();
    if (window.noteOperation) noteOperation(result.operation);
    // From an app's old-volume row (#932), stay on the Containers page.
    resetPaint(); if (typeof STATE !== "undefined" && STATE.view === "workloads" && window.viewWorkloads) viewWorkloads(); else viewStorage();
  } catch (e) {
    button.disabled = false; volumeDeleteGate(); toast(e.message, "bad");
  }
};

/* Longhorn's V2 engine in one line: whether it is on, and how many nodes can
   hold its volumes. Shown once it is on or something already uses it. */
function v2Summary(v2, rows) {
  // Unread is not "off" (#620); it matters only where a class uses V2.
  if (v2?.unread) return rows.some(row => row.engine === "v2")
    ? `<div class="dim xs v2line">${UI.chip("Longhorn V2", "warn")} Could not read whether it is on: ${esc(v2.unread)}
      ${STATE.data.v2 && !STATE.data.v2.unread ? '<a onclick="v2Details()" style="cursor:pointer;text-decoration:underline">last reading</a>' : ""}</div>` : "";
  if (!v2 || (!v2.enabled && !rows.some(row => row.engine === "v2"))) return "";
  const text = !v2.enabled ? "switched off, yet a class uses it: its volumes will not schedule"
    : v2.ready_nodes ? `${v2.ready_nodes} of ${v2.total_nodes} host${v2.total_nodes === 1 ? "" : "s"} ready for its volumes`
    : "on, but no host has a V2 disk and hugepages yet";
  return `<div class="dim xs v2line">${UI.chip("Longhorn V2", v2.enabled && v2.ready_nodes ? "info" : "warn")} ${esc(text)}
    <a onclick="v2Details()" style="cursor:pointer;text-decoration:underline">details</a></div>`;
}
/* Longhorn's V2 engine as a checklist: what the cluster needs, then what
   each host needs, each ticked, crossed or unknown - with how to do it, for
   this distribution, under its tooltip. */
const V2_HOW = {
  longhorn: h => h ? "Harvester brings its own Longhorn and upgrades it with itself: upgrade Harvester (System → Cluster)."
    : "V2 is Longhorn's to run from 1.8; earlier it is an experiment. Upgrade Longhorn under System → Cluster → Platform versions, one minor version at a time.",
  engine: h => h ? "Settings → Hardware and storage → V2 data engine switches Harvester's own longhorn-v2-data-engine-enabled setting. Harvester then reserves 2 GiB of hugepages and loads the kernel modules on every host, which restart to take them."
    : "Settings → Hardware and storage → V2 data engine - once the hosts have hugepages and the kernel modules, as Longhorn's V2 instance managers cannot start without them.",
  cpu: () => "An x86 CPU with SSE4.2 - any from about 2008 on - or an arm64 one. Nothing to do unless this is crossed: V2 cannot run on that host.",
  modules: h => h ? "Harvester loads vfio_pci, uio_pci_generic and nvme_tcp once longhorn-v2-data-engine-enabled is on."
    : "On the host: sudo modprobe vfio_pci uio_pci_generic nvme_tcp - and to keep them after a reboot: printf 'vfio_pci\\nuio_pci_generic\\nnvme_tcp\\n' | sudo tee /etc/modules-load.d/longhorn-v2.conf",
  hugepages: (h, d) => h ? "Harvester reserves them once longhorn-v2-data-engine-enabled is on; the host restarts to take them."
    : `On the host: echo 'vm.nr_hugepages=1024' | sudo tee /etc/sysctl.d/90-longhorn-v2.conf && sudo sysctl --system - then restart ${d === "rke2" ? "rke2-server (or rke2-agent)" : "k3s (sudo systemctl restart k3s, or k3s-agent)"} so Kubernetes counts them. 1024 pages of 2 MiB is 2 GiB, taken from the host's memory.`,
  disk: h => h ? "Hosts → Edit Config → Storage → Add Disk, provisioner Longhorn V2, on a disk holding nothing you need - or Homestead's Hosts → the host → Disks."
    : "Hosts → the host → Disks → an unused disk → Give to Longhorn → V2. It takes the whole disk as a raw block device, so it must hold nothing you need.",
  nvme: h => h ? "Harvester's OS includes it." : "On each host: sudo apt-get install -y nvme-cli (or sudo dnf install -y nvme-cli). Homestead cannot see the host's packages, so this one is yours to check: nvme version.",
  cpu_core: () => "Longhorn's V2 engine polls rather than waiting: on every host running it, one CPU core stays busy. Longhorn's v2-data-engine-cpu-mask setting chooses which.",
};
const v2Mark = ok => ok === true ? '<span class="v2mark ok" aria-label="done">✓</span>'
  : ok === false ? '<span class="v2mark bad" aria-label="missing">✗</span>' : '<span class="v2mark unknown" aria-label="not known">?</span>';
function v2Item(ok, label, how, detail = "") {
  return `<div class="v2item">${v2Mark(ok)}<div><b>${esc(label)}</b> ${tip(how)}${detail ? `<div class="dim xs">${esc(detail)}</div>` : ""}</div></div>`;
}
window.v2Details = async (fresh = false) => {
  if (can("admin")) return lhV2Setup();
  let v2 = STATE.data.v2, unread = "";
  if (fresh || !v2?.nodes?.[0]?.checks) {
    // An unread answer is said; a reading kept from before is shown only as that (#620).
    const read = await api("/api/storage/v2").catch(e => ({ unread: e.message }));
    if (!read.unread) v2 = STATE.data.v2 = read;
    else if (v2?.nodes?.[0]?.checks) unread = read.unread;
    else return modal("Longhorn V2 data engine", UI.callout("bad", "Could not read Longhorn's V2 engine", esc(read.unread))
      + UI.actions(UI.cancel("Close") + UI.button("Try again", "v2Details(true)", { kind: "pri" })), true);
  }
  const h = v2.harvester_setting !== null && v2.harvester_setting !== undefined, d = v2.distribution;
  const nodes = v2.nodes || [];
  const count = key => nodes.filter(n => n.checks?.[key] === true).length;
  const col = (key, label) => `<th>${esc(label)} ${tip(V2_HOW[key](h, d))}</th>`;
  const cell = (n, key, label, extra = "") => `<td data-label="${esc(label)}">${v2Mark(n.checks?.[key])}${extra ? ` <span class="dim xs">${esc(extra)}</span>` : ""}</td>`;
  modal("Longhorn V2 data engine", `<p class="muted small" style="margin-top:0">V2 is Longhorn's SPDK engine: lower latency and less CPU per I/O than V1.
      A V2 volume schedules only on hosts where everything below is ticked. Hover the <b>?</b> beside any line for how to do it${h ? " on Harvester" : ` on ${d === "rke2" ? "RKE2" : d === "k3s" ? "k3s" : "this cluster"}`}.</p>
    ${unread ? UI.callout("warn", "Could not read it just now", `${esc(unread)} · what is shown is the last reading.`) : ""}
    <div class="sec">The cluster</div>
    <div class="v2list">
      ${v2Item(v2.longhorn_ok ?? null, "Longhorn 1.8 or newer", V2_HOW.longhorn(h), v2.longhorn_version ? `runs ${v2.longhorn_version}` : "version not reported")}
      ${v2Item(v2.enabled, "V2 engine switched on", V2_HOW.engine(h), h ? `Harvester's setting is ${v2.harvester_setting ? "on" : "off"}` : "")}
      ${v2Item(h ? true : null, "nvme-cli on every host", V2_HOW.nvme(h), h ? "" : "check on each host: nvme version")}
      ${v2Item(true, "A CPU core for it on each V2 host", V2_HOW.cpu_core(), "not a setup step - worth knowing")}
    </div>
    ${!v2.enabled ? `<div class="row" style="margin-top:10px"><button class="btn sm pri" data-need="admin" onclick="closeModal(); go('settings'); setTimeout(() => settingsTab('cluster'), 300)">Open the V2 switch</button>
      <span class="dim xs">${h || count("hugepages") ? "" : "No host has hugepages yet - do those first."}</span></div>` : ""}
    <div class="sec">Each host</div>
    <div class="card flat pad0"><div class="tblwrap"><table class="tbl stack"><thead><tr><th>Host</th>
      ${col("cpu", "CPU")}${col("modules", "Kernel modules")}${col("hugepages", "2 GiB hugepages")}${col("disk", "V2 disk")}<th>Ready</th></tr></thead><tbody>
      ${nodes.map(n => `<tr><td><b>${esc(n.name)}</b></td>${cell(n, "cpu", "CPU")}
        ${cell(n, "modules", "Kernel modules", n.missing_modules?.length ? `needs ${n.missing_modules.join(", ")}` : n.checks?.modules == null ? "probe has not said" : "")}
        ${cell(n, "hugepages", "Hugepages", `${n.hugepages_mb} MiB`)}${cell(n, "disk", "V2 disk", n.block_disks ? `${n.block_disks}` : "")}
        <td data-label="Ready">${n.ready ? UI.chip("ready", "ok") : UI.chip("not yet", "neutral")}</td></tr>`).join("")
        || '<tr><td colspan="6" class="empty">Longhorn reported no hosts</td></tr>'}</tbody></table></div></div>
    <div class="row" style="margin-top:12px"><button class="btn sm" onclick="v2Details(true)">${icon("refresh")}Check again</button>
      <span class="dim xs">${v2.ready_nodes ?? 0} of ${v2.total_nodes ?? 0} host${v2.total_nodes === 1 ? "" : "s"} ready. A volume keeping 2 copies needs 2 ready hosts.</span></div>`, true);
  if (window.applyRole) applyRole();
};

/* Settings › Hardware and storage: the storage classes, beside Longhorn's
   own settings - what new volumes are made from is cluster configuration,
   not a list of volumes. */
async function storageClassesPaint() {
  const host = $("#storageClassesCard");
  if (!host) return;
  const [classes, v2] = await Promise.all([api("/api/storage/classes").catch(e => ({ unread: e.message })),
    api("/api/storage/v2").catch(e => ({ unread: e.message }))]);
  if (classes.unread) {
    // The card says so, rather than vanish as if there were no classes (#620).
    host.innerHTML = UI.callout("bad", "Could not read the storage classes", esc(classes.unread));
    host.hidden = false;
    return;
  }
  STATE.data.storageClasses = classes;
  // An unread answer is shown on the card, but the last good one is kept.
  if (!v2.unread) STATE.data.v2 = v2;
  host.innerHTML = storageClassCard(classes, v2);
  host.hidden = !host.innerHTML;
  if (window.applyRole) applyRole();
}
window.storageClassesPaint = storageClassesPaint;

/* Which class Homestead puts new volumes on, and why (#1125), said beside
   every Make default (this card and the setup guide's storage step): an
   install class wins over the cluster's default. "" for an answer without it. */
function homesteadClassLine(classes) {
  const ours = (classes || []).find(row => row.homestead_default);
  return !ours ? "" : `<p class="dim xs" data-homestead-class>Homestead puts new volumes on <b class="mono">${esc(ours.name)}</b>${
    ours.homestead_reason === "install" ? " (its install class); making another class the default here does not change that."
    : ours.homestead_reason === "default" ? ", the cluster's default: making another class the default here moves new volumes there too."
    : " (no class is marked default); making a class the default here moves new volumes there."}</p>`;
}
window.homesteadClassLine = homesteadClassLine;

function storageClassCard(classes, v2 = null) {
  const every = classes || [];
  if (!every.length) return "";
  // Classes made for one image or one restore are not for choosing: folded
  // away. Repair resize supportes are kept while any claim still needs them.
  const special = every.filter(row => row.made_for);
  const restores = special.filter(row => row.made_for === "restore");
  const rows = STATE.showSpecialClasses ? every : every.filter(row => !row.made_for);
  const specialLine = special.length ? `<div class="dim xs" style="margin-top:8px">${special.length} class${special.length === 1 ? "" : "es"} made for
      ${[special.length - restores.length ? `${special.length - restores.length} Harvester image${special.length - restores.length === 1 ? "" : "s"}` : "", restores.length ? `${restores.length} restore${restores.length === 1 ? "" : "s"}` : ""].filter(Boolean).join(" and ")}
      ${STATE.showSpecialClasses ? "shown" : "hidden"}, and never offered when choosing a class.
      <a class="linkish" onclick="STATE.showSpecialClasses=!STATE.showSpecialClasses;storageClassesPaint()">${STATE.showSpecialClasses ? "Hide" : "Show"} them</a>
      ${restores.length ? ` · <a class="linkish" data-need="admin" onclick="storageClassCleanup()">Remove unused restore classes</a>` : ""}</div>` : "";
  const oursLine = homesteadClassLine(every);
  // What a volume on it can do, as tags: one column, not three.
  const features = row => [
    row.migratable ? UI.chip("VM disk class", "neutral", { tip: "Made for VM live migration. Fine for one pod on one host; not for a volume several hosts mount (RWX)." })
      : UI.chip("shareable", "ok", { tip: "Volumes can be shared between pods (RWX)" }),
    row.encrypted ? UI.chip("encrypted", "info") : "",
    row.expandable ? UI.chip("can grow", "neutral") : UI.chip("fixed size", "neutral", { tip: "Volumes keep the size they were made with" }),
  ].filter(Boolean).join(" ");
  const kind = row => row.made_for === "image" ? ` ${UI.chip("image", "neutral", { tip: "Harvester made it for one image: disks from that image are made on it" })}`
    : row.made_for === "restore" ? ` ${UI.chip("restore", "neutral", { tip: "Made to restore a backup; kept while claims reference it so they can be resized" })}`
    : row.made_for === "iso" ? ` ${UI.chip("ISO copies", "neutral", { tip: "ISO copies Homestead made for VM CD-ROM drives: one replica, since the originals are on your shares" })}` : "";
  return `${UI.moduleHeader(`Storage classes`, `What a new volume is made from. Kubernetes fixes a class once it is made, so a change means a new class.`, `<button class="btn sm pri" data-need="admin" onclick="storageClassCreate()">＋ New storage class</button>`, {extraHtml:`${v2Summary(v2, rows)}`})}${oursLine}
    <div class="tblwrap"><table data-sort="storage-classes" class="tbl stack dense"><thead><tr>
      <th>Class</th><th>Engine</th><th>Copies</th><th data-nosort>Its volumes</th><th>In use</th><th data-nosort></th>
    </tr></thead><tbody>${rows.map(row => `<tr>
      <td><b>${esc(row.name)}</b> ${row.default ? UI.chip("default", "ok") : ""}${row.internal ? ` ${UI.chip("Harvester internal", "neutral")}` : ""}${kind(row)}
        <div class="dim xs mono">${esc(row.provisioner || "")}</div>
        ${(row.disk_tags || []).length || (row.node_tags || []).length ? `<div class="row" style="gap:4px;margin-top:4px">
          ${(row.disk_tags || []).map(t => UI.chip(`disk: ${t}`, "info", { tip: `Only on disks tagged ${t}` })).join("")}
          ${(row.node_tags || []).map(t => UI.chip(`host: ${t}`, "neutral", { tip: `Only on nodes tagged ${t}` })).join("")}</div>` : ""}</td>
      <td data-label="Engine">${row.engine === "v2" ? UI.chip("V2", "info", { tip: "Longhorn V2 (SPDK)" }) : row.engine ? UI.chip("V1", "neutral") : '<span class="dim">—</span>'}</td>
      <td class="mono" data-label="Copies">${esc(row.replicas || "—")}</td>
      <td data-label="Its volumes">${features(row)}</td>
      <td class="mono" data-label="In use">${row.in_use ?? 0}</td>
      <td data-actions>${actionBar([
        row.default || row.internal || row.made_for ? null : { label: "Make default", run: `storageClassDefault(${jsq(row.name)})`, need: "admin", tip: "The cluster's default, for claims that name no class" },
        row.internal || row.default || row.in_use ? null : { label: "Delete", icon: "trash", run: `storageClassDelete(${jsq(row.name)})`, need: "admin", danger: true },
      ], { shown: 1 })}</td></tr>`).join("")}</tbody></table></div>${specialLine}`;
}
window.storageClassCleanup = async () => {
  try {
    const r = await api("/api/storage/classes/cleanup", { method: "POST", headers: { "Content-Type": "application/json" }, body: "{}" });
    toast(r.detail, "ok"); storageClassesPaint();
  } catch (e) { toast(e.message, "bad"); }
};
window.storageClassCreate = async (prefill = {}, onSaved = null) => {
  modal("New storage class", `
    <p class="muted small">Set the defaults for new volumes: replicas, expansion and deletion policy. These storage-class settings cannot be edited after creation.</p>
    <div class="f" style="margin-top:14px"><label>Name</label>
      <input type="text" id="sc_name" placeholder="longhorn-r3" autocomplete="off"></div>
    <div class="f2"><div class="f"><label>Replicas ${tip("Copies of each volume Longhorn keeps. One has no redundancy.")}</label>
      <input type="number" id="sc_reps" min="1" max="5" value="2" oninput="storageClassReach()"></div>
      <div class="f"><label>Copies go on ${tip("Different hosts: each copy on a host of its own, so a host or a disk can fail - Longhorn's default. Different disks: copies may share a host but never a disk - on one host, a failed disk is survived but a failed host is not.")}</label>
        <select id="sc_copies" onchange="storageClassReach()"><option value="hosts">Different hosts</option><option value="disks">Different disks, same host allowed</option></select></div></div>
    <div class="f2">
      <div class="f"><label>Reclaim policy ${tip("Retain keeps a volume's data when its claim is deleted - with an app, say - and lists it on Volumes as having no claim, to reuse or delete there. Delete removes the data with the claim.")}</label>
        <select id="sc_reclaim"><option>Retain</option><option>Delete</option></select></div>
    <div class="f"><label>Data engine ${tip("V1 is Longhorn's long-standing engine. V2 (SPDK) is faster and needs the engine switched on, a V2 disk and hugepages on the hosts.")}</label>
      <select id="sc_engine" onchange="storageClassEngine()"><option value="v1">V1 · the default</option><option value="v2">V2 · SPDK</option></select></div></div>
    <div class="note" id="sc_engine_note" hidden></div>
    <div class="f"><label>Only on disks tagged ${tip("Longhorn puts this class's replicas only on disks with every tag chosen - tag your SSDs ssd and pick it here. Tag disks under Hosts, or Volumes → Disks.")}</label>
      <div class="row sc-tags" id="sc_disktags"><span class="dim xs">loading tags…</span></div></div>
    <div class="f"><label>Only on nodes tagged ${tip("And only on nodes with every tag chosen here.")}</label>
      <div class="row sc-tags" id="sc_nodetags"></div>
      <div class="dim xs" id="sc_reach" style="margin-top:6px"></div></div>
    <label class="switch"><input type="checkbox" id="sc_expand" checked> Allow volumes to grow later</label>
    <label class="switch"><input type="checkbox" id="sc_migratable" onchange="storageClassHint()"> Live-migratable · for VM disks ${tip("Migratable is for VM live migration. Its volumes mount on one host at a time: fine for one pod, but not for a volume several hosts share (RWX).")}</label>
    <div id="sc_hint" hidden></div>
    <label class="switch"><input type="checkbox" id="sc_default"> Make this the cluster's default class ${tip("The cluster's default, for claims that name no class.")}</label>
    ${homesteadClassLine(STATE.data.storageClasses)}
    ${UI.actions(`<button class="btn pri" id="sc_go" data-need="admin" onclick="storageClassSave(this)">Create class</button>
      <button data-dialog-dismiss="true" class="btn" onclick="closeModal()">Cancel</button>`)}`);
  for (const [id, key] of [["sc_name", "name"], ["sc_reps", "replicas"], ["sc_copies", "copies"],
    ["sc_reclaim", "reclaim_policy"], ["sc_engine", "engine"]]) {
    if (prefill[key] !== undefined) $("#" + id).value = prefill[key];
  }
  for (const [id, key] of [["sc_expand", "expandable"], ["sc_migratable", "migratable"], ["sc_default", "default"]]) {
    if (prefill[key] !== undefined) $("#" + id).checked = !!prefill[key];
  }
  storageClassHint();
  const button = $("#sc_go");
  button.__storageSaved = onSaved;
  button.disabled = true;
  await storageClassTags(prefill, button);
  if ($("#sc_go") === button) button.disabled = false;
};
/* The tags disks and nodes have, to choose from, and which nodes a choice
   leaves - a class needs one node per replica. */
async function storageClassTags(prefill = {}, button = $("#sc_go")) {
  let inv = null, unread = "";
  try { inv = await loadDisks(); } catch (e) { unread = e.message; }
  if ($("#sc_go") !== button) return;
  const box = (id, available, selected, empty) => {
    const host = $(id);
    if (!host) return;
    const tags = [...new Set([...available, ...selected])];
    host.innerHTML = tags.length ? tags.map(t => `<label class="daychip"><input type="checkbox" value="${esc(t)}"${selected.includes(t) ? " checked" : ""} onchange="storageClassReach()"><span>${esc(t)}${available.includes(t) ? "" : " · not reported"}</span></label>`).join("")
      : `<span class="dim xs">${empty}</span>`;
  };
  STATE.data.scInv = inv;
  // Unread disks are not disks without tags (#620): the tags it was opened
  // with stay, and the empty text says what could not be read.
  const unreadText = unread && `Could not read the disks: ${esc(unread)}`;
  box("#sc_disktags", inv?.disk_tags || [], prefill.disk_tags || [], unreadText || "No disk has a tag yet: add them to disks under Hosts, or Volumes → Disks.");
  box("#sc_nodetags", inv?.all_node_tags || [], prefill.node_tags || [], unreadText || "No node has a tag yet.");
  // One host: only "different disks" can place a second copy.
  if (inv && prefill.copies === undefined && Object.keys(inv.nodes || {}).length === 1 && $("#sc_copies")) $("#sc_copies").value = "disks";
  storageClassReach();
}
window.storageClassReach = () => {
  const inv = STATE.data.scInv, out = $("#sc_reach");
  if (!out) return;
  if (!inv) { out.className = "xs badtext"; out.textContent = "Could not read disk availability. Review tags and placement before creating this class."; return; }
  const disk = $$("#sc_disktags input:checked").map(b => b.value), node = $$("#sc_nodetags input:checked").map(b => b.value);
  const reps = +($("#sc_reps").value || 1), byDisk = $("#sc_copies")?.value === "disks";
  const fits = (name, x) => x.scheduling && x.ready !== false && !x.missing && !x.failed
    && ($("#sc_engine")?.value === "v2" ? x.type === "block" : x.type !== "block")
    && node.every(t => ((inv.node_tags || {})[name] || []).includes(t)) && disk.every(t => (x.tags || []).includes(t));
  const places = Object.entries(inv.nodes || {}).flatMap(([name, disks]) =>
    disks.flatMap(d => (d.longhorn || []).filter(x => fits(name, x)).map(x => ({ name, disk: x.id }))));
  const hosts = [...new Set(places.map(p => p.name))];
  const count = byDisk ? places.length : hosts.length, unit = byDisk ? "disk" : "host";
  out.className = count >= reps ? "dim xs" : "xs badtext";
  out.textContent = !count ? `No ${disk.length || node.length ? "tagged " : ""}disk can hold its copies, so its volumes would not schedule.`
    : `${count} ${unit}${count === 1 ? "" : "s"} can hold its copies${byDisk ? ` (on ${hosts.join(", ")})` : `: ${hosts.join(", ")}`}`
      + (count < reps ? ` - fewer than ${reps}, so volumes would run a copy short${!byDisk && places.length >= reps ? '. Choose "Different disks" to place them on one host' : ""}.` : ".");
};
window.storageClassEngine = async () => {
  storageClassReach();
  const note = $("#sc_engine_note");
  if ($("#sc_engine").value !== "v2") { note.hidden = true; return; }
  note.hidden = false;
  note.textContent = "Checking the hosts…";
  // Unread is said, never the last answer as if current (#620); a good answer is kept.
  const v2 = await api("/api/storage/v2").catch(e => ({ unread: e.message }));
  if (v2.unread) { note.textContent = `Could not read Longhorn's V2 status: ${v2.unread}`; note.classList.add("bad"); return; }
  STATE.data.v2 = v2;
  note.classList.toggle("bad", !v2.enabled || !v2.ready_nodes);
  note.innerHTML = !v2.enabled
    ? "<b>V2 is switched off in Longhorn.</b> A class can be made now, but its volumes will not schedule until it is on. <a onclick=\"v2Details()\" style=\"cursor:pointer;text-decoration:underline\">How to turn it on</a>"
    : v2.ready_nodes < +($("#sc_reps").value || 1)
      ? `<b>${v2.ready_nodes} host${v2.ready_nodes === 1 ? " is" : "s are"} ready for V2</b>, fewer than the replicas asked for, so volumes will run degraded or not schedule. <a onclick="v2Details()" style="cursor:pointer;text-decoration:underline">What each host needs</a>`
      : `${v2.ready_nodes} of ${v2.total_nodes} hosts are ready for V2 volumes.`;
};
window.storageClassHint = () => {
  // Nothing to say until it is ticked; then it is the dialog's one notice (#501).
  const on = $("#sc_migratable").checked, hint = $("#sc_hint");
  hint.hidden = !on;
  hint.innerHTML = on ? UI.callout("warn", "Volumes from this class can't be shared between hosts",
    "One pod on one host can use them; a ReadWriteMany claim on it never attaches to a second host's pod.") : "";
};
window.storageClassSave = async button => {
  const name = $("#sc_name").value.trim();
  if (!/^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$/.test(name)) return toast("Lowercase letters, numbers and dashes only", "bad");
  if (button) { button.disabled = true; button.textContent = "Creating…"; }
  try {
    const result = await api("/api/storage/classes", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name, replicas: +$("#sc_reps").value, reclaim_policy: $("#sc_reclaim").value, copies: $("#sc_copies").value,
        expandable: $("#sc_expand").checked, migratable: $("#sc_migratable").checked,
        engine: $("#sc_engine").value, default: $("#sc_default").checked,
        disk_tags: $$("#sc_disktags input:checked").map(b => b.value),
        node_tags: $$("#sc_nodetags input:checked").map(b => b.value) }) });
    const after = button?.__storageSaved;
    toast(result.message || `storage class "${name}" created`, "ok"); closeModal();
    if (after) await after(); else await storageClassesPaint();
  } catch (e) { if (button) { button.disabled = false; button.textContent = "Create class"; } toast(e.message, "bad"); }
};
window.storageClassDefault = async name => {
  try {
    const result = await api("/api/storage/classes/default", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name }) });
    toast(result.message || `${name} is now the default`, "ok"); storageClassesPaint(); return result;
  } catch (e) { toast(e.message, "bad"); }
};
window.storageClassDelete = async name => {
  if (!(await ask(`Delete storage class "${name}"?

Volumes already built from it keep working and keep their data. New volumes can no longer use it.`, { title: "Delete storage class", ok: "Delete storage class", danger: true }))) return;
  try {
    const result = await api("/api/storage/classes/delete", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name }) });
    toast(result.message || `storage class "${name}" deleted`, "ok"); storageClassesPaint();
  } catch (e) { toast(e.message, "bad"); }
};

window.volumeClassFacts = () => {
  const classes = window.__volumeClasses || { facts: {}, shared: [] };
  const name = $("#vc_sc")?.value, badges = $("#vc_badges"), note = $("#vc_mode_note");
  if (badges) badges.innerHTML = storageClassBadges((classes.facts || {})[name]);
  if (!note) return;
  const rwx = $("#vc_mode")?.value === "ReadWriteMany";
  const usable = (classes.shared || []).includes(name);
  note.hidden = !(rwx && !usable);
  if (!note.hidden) {
    note.innerHTML = `<b>${esc(name)} cannot back a shared volume.</b> Its volumes are live-migratable,
      so they mount on one host at a time, and a shared volume needs several.
      ${(classes.shared || []).length ? `Use ${(classes.shared || []).map(esc).join(" or ")} instead.` : ""}`;
  }
};

const fileSize = bytes => bytesText(bytes);

/* Monaco is several megabytes, so it is fetched the first time a file is
   opened rather than on page load, and never at all for the rest of the app.
   If it cannot load - offline, blocked, a stripped image - the editor falls
   back to the textarea that was here before, which still saves correctly. */
const monacoBase = () => `${vendorBase()}/monaco`;
let monacoLoading = null;

function monacoLanguage(name) {
  const extension = String(name || "").toLowerCase().split(".").pop();
  return { yml: "yaml", yaml: "yaml", json: "json", md: "markdown", markdown: "markdown",
    xml: "xml", conf: "ini", ini: "ini", cfg: "ini", env: "ini", toml: "ini",
    sh: "shell", bash: "shell", py: "python", js: "javascript", ts: "typescript" }[extension] || "plaintext";
}

function loadMonaco() {
  if (window.monaco?.editor) return Promise.resolve(window.monaco);
  if (monacoLoading) return monacoLoading;
  monacoLoading = new Promise((resolve, reject) => {
    const failed = message => { monacoLoading = null; reject(new Error(message)); };
    const style = document.createElement("link");
    style.rel = "stylesheet";
    style.href = `${monacoBase()}/vs/editor/editor.main.css`;
    document.head.appendChild(style);
    const script = document.createElement("script");
    script.src = `${monacoBase()}/vs/loader.js`;
    script.onerror = () => failed("the editor could not be loaded");
    script.onload = () => {
      try {
        window.require.config({ paths: { vs: `${monacoBase()}/vs` } });
        // Same origin, so the worker needs no blob shim.
        window.MonacoEnvironment = { getWorkerUrl: () => `${monacoBase()}/vs/base/worker/workerMain.js` };
        window.require(["vs/editor/editor.main"], () => resolve(window.monaco), () => failed("the editor could not start"));
      } catch (e) { failed(e.message); }
    };
    document.head.appendChild(script);
  });
  return monacoLoading;
}

function monacoTheme() {
  return document.documentElement.dataset.theme === "light" ? "vs" : "vs-dark";
}

/* The caller owns what a change and Ctrl+S mean: { onChange, onSave }. */
async function mountEditor(host, content, filename, { onChange, onSave } = {}) {
  const monaco = await loadMonaco();
  const editor = monaco.editor.create(host, {
    value: content,
    language: monacoLanguage(filename),
    theme: monacoTheme(),
    automaticLayout: true,
    minimap: { enabled: false },
    fontSize: 12.5,
    fontFamily: '"JetBrains Mono", ui-monospace, monospace',
    scrollBeyondLastLine: false,
    renderWhitespace: "selection",
    tabSize: 2,
    insertSpaces: true,
    rulers: [],
    padding: { top: 10, bottom: 10 },
  });
  if (onChange) editor.onDidChangeModelContent(() => onChange());
  if (onSave) editor.addCommand(monaco.KeyMod.CtrlCmd | monaco.KeyCode.KeyS, () => onSave());
  return editor;
}

window.volumeChown = async (namespace, name) => {
  modal(`Ownership · ${name}`, '<div class="empty"><span class="spin2"></span>reading what uses this volume</div>');
  // The workload that mounts it already says who it runs as; asking the person
  // for a uid they have no way to know is not a question worth putting to them.
  const hint = await api(`/api/volumes/ownership?namespace=${encodeURIComponent(namespace)}&name=${encodeURIComponent(name)}`)
    .catch(e => ({ known: false, unread: e.message }));
  $("#mbody").innerHTML = `
    <p class="muted small">Set file ownership to match the container's user and group. ${tip("Imports preserve source ownership. Change it only when the container requires different IDs or reports permission errors.")}</p>
    ${hint.unread ? UI.callout("warn", "Could not read what uses this volume", esc(hint.unread)) : hint.known
      ? `<div class="note"><b>${esc(hint.workload)} runs as ${hint.uid ?? hint.gid}${hint.gid != null && hint.gid !== hint.uid ? `:${hint.gid}` : ""}.</b>
          Taken from ${esc(hint.source)}${hint.image ? ` · <span class="mono">${esc(hint.image)}</span>` : ""}.</div>`
      : `${UI.callout("bad", { html: `Nothing here declares a user.` }, `${esc(hint.source || "")}`)}`}
    <div class="f2" style="margin-top:14px">
      <div class="f"><label>User (UID)</label><input type="number" id="vc_uid" min="0" max="65535" value="${hint.uid ?? ""}" placeholder="1883"></div>
      <div class="f"><label>Group (GID)</label><input type="number" id="vc_gid" min="0" max="65535" value="${hint.gid ?? ""}" placeholder="same as UID"></div></div>
    <div class="note">Stop the app or VM first so the ownership job can mount its volume. A ReadWriteOnce volume cannot attach while the app or VM holds it.</div>
    ${UI.actions(`<button class="btn pri" data-need="admin" onclick="volumeChownNow(${jsq(namespace)},${jsq(name)},this)">Set ownership</button>
      <button data-dialog-dismiss="true" class="btn" onclick="closeModal()">Cancel</button>`)}`;
  if (window.applyRole) window.applyRole();
};
window.volumeChownNow = async (namespace, name, button) => {
  const uid = $("#vc_uid").value.trim();
  if (!uid) return toast("A user id is required", "bad");
  if (button) { button.disabled = true; button.textContent = "Starting…"; }
  try {
    const result = await api("/api/volumes/chown", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ namespace, name, uid, gid: $("#vc_gid").value.trim() }) });
    closeModal(); toast(result.message || "ownership job started", "ok");
  } catch (e) {
    if (button) { button.disabled = false; button.textContent = "Set ownership"; }
    toast(e.message, "bad");
  }
};

const attachedWorkloads = volume => volume.attached
  || String(volume.attached_to || "").split(",").map(name => name.trim()).filter(Boolean);

// Node only (tests): the same names, for require(). The browser has no `module`.
if (typeof module === "object" && module.exports) module.exports = {
  volumeReason, volumeUse, volumeCopiesLine, volumeCopiesTip, volumeUseCell, volumeBusy, volumeProgressBar,
  volumeHealthCell, volumeShortCell, volumeActions, forecastWhen, forecastDate, forecastTone, forecastFor,
  forecastCard, volumeUsageCell, volumeProgressWatch, volumeProgressResume, shareGuestWindowsNote, otherVolumesHtml,
  storageLoad, partReading, viewStorage, storagePaint, volumeDeleteConfirmationValid, releasedBesideCurrent,
  volumeDeleteTarget, v2Summary, V2_HOW, v2Mark, v2Item, storageClassesPaint, homesteadClassLine, storageClassCard,
  storageClassTags, fileSize, monacoBase, monacoLoading, monacoLanguage, loadMonaco, monacoTheme, mountEditor,
  attachedWorkloads
};
