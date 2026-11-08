/* Homestead views: Longhorn allocation and its settings. Split from views-storage.js (#911); a classic script. */
/* ---------------- Longhorn allocation ----------------
   Longhorn books each replica's full size on a disk when it places it, and a
   disk takes no new replica past its size x over-provisioning. New volumes
   then come up a copy short and rebuilds wait - so each node's allocation is
   shown against that limit, with the biggest volume that still fits. */
const lhLevel = level => level === "crit" ? "b" : level === "warn" ? "w" : "";
const lhShort = name => name.replace("harvester-", "");

function lhNodeRows(cap) {
  return (cap.nodes || []).map(n => `<div class="lh-node">
    <div class="between"><b>${esc(lhShort(n.name))}</b>
      <span class="mono xs ${n.level === "ok" ? "dim" : n.level === "warn" ? "warntext" : "badtext"}">${esc(sizePair(n.allocated_gb, n.limit_gb))} allocated</span></div>
    <div class="meter ${lhLevel(n.level)}" data-tip="${esc(`${n.pct}% of what Longhorn may place here (${n.size_gb} GiB × ${cap.over_provisioning}%)`)}"><span style="width:${Math.min(100, n.pct)}%"></span></div>
    <div class="dim xs">${n.blocked ? `<span class="badtext">${esc(n.blocked)}</span>` : `${esc(sizeText(n.room_gb))} allocation headroom`} · ${esc(sizeText(n.used_gb))} physically used</div>
    <div class="dim xs" data-tip="Free bytes above Longhorn's minimum-free-space reserve on the best eligible disk. A rebuild must fit its block/snapshot footprint below this budget as well as its logical allocation; tags, anti-affinity and other placement rules still apply.">Physical rebuild budget: ${Number.isFinite(n.physical_room_gb) ? `${esc(n.physical_room_gb)} GiB` : "unknown"}</div></div>`).join("");
}

function lhLargest(cap) {
  const count = (cap.nodes || []).length;
  return [1, 2, 3].filter(c => c <= count).map(c => UI.chip(`${c} cop${c === 1 ? "y" : "ies"} · ${sizeText(cap.largest[c])}`, cap.largest[c] < 10 ? "bad" : cap.largest[c] < 50 ? "warn" : "neutral",
    { tip: `Logical allocation upper bound for ${c} empty cop${c === 1 ? "y" : "ies"} on different hosts. Not rebuild capacity or a placement guarantee: existing data and snapshots also need physical space.` })).join("");
}

/* The Volumes page's per-node card. */
function lhCapacityCard(cap, st) {
  if (!cap) return `<div class="card flat statwide"><div class="ctitle">Per-node disks</div><div class="csub">free of total</div>
      ${(st?.disks || []).map(d => `<div class="drow"><div class="dl">${esc(lhShort(d.node))}</div>
        <div class="dv mono nowrap">${esc(sizePair(d.avail_gb, d.cap_gb))}</div></div>`).join("") || '<div class="csub" style="margin-top:8px">Longhorn has not reported any node disks yet.</div>'}</div>`;
  return `<div class="card flat statwide lh-card"><div class="between"><div class="ctitle">Allocated per host</div>
      <span class="row" style="gap:10px"><a class="dim xs" data-need="admin" onclick="lhDisks()" data-tip="Every disk on every host; add one to Longhorn">Disks</a>
        <a class="dim xs" onclick="go('settings');settingsTab('cluster')" data-tip="Over-provisioning is ${cap.over_provisioning}%">Longhorn settings</a></span></div>
    ${lhNodeRows(cap) || '<div class="csub" style="margin-top:8px">Longhorn has not reported any node disks yet.</div>'}
    <div class="dim xs" style="margin-top:8px">Empty-volume allocation limit</div><div class="row lh-largest">${lhLargest(cap)}</div></div>`;
}
window.lhCapacityCard = lhCapacityCard;

/* Settings > Hardware and storage: over-provisioning, minimal free space, the V2 engine. */
async function lhSettingsPaint() {
  const host = $("#lhSettingsCard");
  if (!host) return;
  if (STATE.platform && STATE.platform.longhorn === false) {
    host.innerHTML = `<div class="ctitle">Longhorn storage</div><div class="empty small">Longhorn is not installed on this cluster.</div>`;
    return;
  }
  let cap, offline = null, offlineUnread = "";
  try { [cap, offline] = await Promise.all([api("/api/longhorn/capacity"), api("/api/longhorn/offline-rebuilding").catch(e => ({ unread: e.message }))]); }
  catch (e) { host.innerHTML = `<div class="ctitle">Longhorn storage</div>${UI.error(e, { retry: "lhSettingsPaint()" })}`; return; }
  // Unread, the setting is named rather than left out, and offers no toggle to save a guess (#620).
  if (offline?.unread) { offlineUnread = offline.unread; offline = null; }
  STATE.data.lhcap = cap;
  STATE.data.lhOffline = offline;
  const admin = can("admin"), v2 = cap.v2 || {};
  host.innerHTML = `${UI.moduleHeader(`Longhorn storage`, `How much Longhorn may promise on each disk, and its V2 data engine`, `<button class="btn sm" onclick="lhDisks()">Disks</button>
      ${admin ? "" : UI.chip("admin managed", "neutral")}`)}
    <div class="f2">
      <div class="f"><label>Over-provisioning ${tip("Longhorn books a volume's full size on a disk when it places a replica, however little it holds. At 100% a disk can be promised its own size; at 200%, twice that, betting volumes never fill up. If they do, the disk runs out and its replicas fail.")}</label>
        <div class="row" style="flex-wrap:nowrap"><input id="lh_over" type="number" min="100" max="1000" step="10" value="${cap.over_provisioning}" ${admin ? "" : "disabled"} oninput="lhPreview()"><span class="dim">%</span></div></div>
      <div class="f"><label>Minimal free space ${tip("A disk with less than this share physically free takes no new replica, whatever is allocated. Longhorn's default is 25%.")}</label>
        <div class="row" style="flex-wrap:nowrap"><input id="lh_min" type="number" min="0" max="100" value="${cap.minimal_available}" ${admin ? "" : "disabled"}><span class="dim">%</span></div></div></div>
    <div class="f" style="margin-top:10px"><label>Replicas rebuilt at once, per host ${tip("When a host or disk comes back, or a volume is a copy short, Longhorn rebuilds replicas by copying them from a healthy one. More at once makes volumes whole sooner; fewer keeps the disks and network free for everything else while it runs. Longhorn's default is 5.")}</label>
      <input id="lh_rebuild" type="number" min="1" max="10" value="${cap.rebuild_limit ?? 5}" ${admin ? "" : "disabled"}></div>
    ${offline ? `<div class="srows" style="margin-top:10px">${settingRow(`Rebuild copies of detached volumes ${tip(offline.supported
        ? "Longhorn rebuilds a missing copy only while its volume is attached. With this on, it also attaches a detached volume by itself - nothing can mount it meanwhile - until its copies are whole, so a volume nothing uses is not left with one copy."
        : "Longhorn rebuilds a missing copy only while its volume is attached, and this version cannot do it offline. With this on, Homestead attaches a detached volume short of copies itself - nothing can mount it meanwhile - one at a time, until its copies are whole, and lets go as soon as anything else asks for it.")}`, "",
      `<label class="toggle"><input type="checkbox" id="lh_offline" ${offline.enabled ? "checked" : ""} ${admin ? "" : "disabled"}><span></span></label>`)}</div>
      ${(offline.short || []).length ? `<div class="dim xs">${offline.short.length} detached volume${offline.short.length === 1 ? " is" : "s are"} short of copies now.</div>` : ""}`
      : offlineUnread ? `<div class="dim small" style="margin-top:10px">Could not read whether Longhorn rebuilds copies of detached volumes: ${esc(offlineUnread)}</div>` : ""}
    <div class="srows" style="margin-top:10px">${settingRow(`Allow apps with storage to move off a failed host ${tip("When a host fails, Longhorn keeps its storage attached to it, so apps that use that storage cannot start on another host. With this on, Longhorn deletes the failed host's pods and releases their storage.")}`,
      ["do-nothing", "delete-both-statefulset-and-deployment-pod"].includes(cap.node_down) ? "" : `Currently set outside Homestead to ${esc(cap.node_down)}. Turning this on applies it to all apps.`,
      `<label class="toggle"><input type="checkbox" id="lh_nodedown" data-was="${esc(cap.node_down)}" ${cap.node_down === "delete-both-statefulset-and-deployment-pod" ? "checked" : ""} ${admin ? "" : "disabled"}><span></span></label>`)}</div>
    ${UI.more("Effects and risks", `<p><b>On:</b> if a host fails, apps set to move restart on another host within a few minutes, with their storage.</p>
      <p><b>Off:</b> those apps stay stopped until the failed host is back online.</p>
      <p><b>Storage:</b> either way, any volume with a copy on the failed host is left with one copy fewer. After 10 minutes, Longhorn creates a new copy on another host if one has enough space. If none does, the volume runs with fewer copies until the failed host is back. A volume whose only copy is on the failed host is unavailable until then.</p>
      <p><b>Risk of turning it on:</b> Homestead cannot tell a host that has failed from one that has only lost its network connection. If the host is still running, its copy of an app may keep running too, and any data that copy writes after the switch-over is lost. Leave this off if your hosts sometimes lose their network connection while still running, or if an app must never run twice.</p>`, false, "lh-nodedown-why")}
    <div id="lh_nodes" class="lh-nodes">${lhNodeRows(cap)}</div>
    <div class="dim xs" style="margin-top:10px">Largest new volume ${tip("Past a host's limit, new volumes come up a copy short, replica rebuilds wait and expansions are refused; nothing already placed is moved.")}</div><div class="row lh-largest" id="lh_largest">${lhLargest(cap)}</div>
    <div class="dim xs" style="margin-top:8px">What fills a disk for real is data written: ${esc(sizeText((cap.nodes || []).reduce((s, n) => s + n.used_gb, 0)))} across all hosts now.</div>
    <div class="lh-v2">
      <div class="between"><b>V2 data engine · ${v2.enabled ? 'enabled' : 'off'}</b>${admin ? actionBar([
        {label:v2.enabled?'V2 setup and status':'Set up Longhorn V2',run:'lhV2Setup()'},
        ...(v2.enabled || v2.harvester_setting == null ? [{label:v2.enabled?'Disable V2':'Review V2 resource cleanup',run:'lhV2Disable()',danger:!!v2.enabled}] : [])
      ],{shown:1}) : ''}</div>
      <div class="dim xs">Faster volumes for a price: each host needs a disk given to Longhorn as a block device and usually 2 GiB of hugepages (setup checks the installed requirement), and V2 volumes are a separate storage class.
        ${v2.harvester_setting !== null && v2.harvester_setting !== undefined ? "On Harvester this switches Harvester's own setting, which sets up hugepages and the kernel modules on each host." : ""}
        It cannot be switched off while V2 volumes exist. <a class="linkish" onclick="v2Details(true)">What each host needs</a></div>
      ${(v2.nodes || []).length ? `<div class="lh-v2-nodes">${v2.nodes.map(n => UI.chip(`${lhShort(n.name)} · ${n.ready ? "ready" : "not ready"}`, n.ready ? "ok" : "neutral", { tip: n.missing.length ? `Needs ${n.missing.join(" and ")}` : "" })).join("")}</div>` : ""}</div>`;
}
window.lhSettingsPaint = lhSettingsPaint;

/* What the limits become at the over-provisioning being typed. */
window.lhPreview = () => {
  const cap = STATE.data.lhcap, over = +$("#lh_over").value;
  if (!cap || !(over >= 100)) return;
  const nodes = cap.nodes.map(n => {
    const limit = Math.round(n.size_gb * over) / 100, pct = limit ? Math.round(n.allocated_gb / limit * 1000) / 10 : 0;
    const room = n.blocked ? 0 : Math.max(0, Math.round((Math.max(...n.disks.filter(d => !d.blocked).map(d => d.size_gb * over / 100 - d.allocated_gb), 0)) * 10) / 10);
    return { ...n, limit_gb: limit, pct, room_gb: room, level: n.blocked || pct >= cap.crit_pct ? "crit" : pct >= cap.warn_pct ? "warn" : "ok" };
  });
  const rooms = nodes.map(n => n.room_gb).sort((a, b) => b - a);
  const preview = { ...cap, over_provisioning: over, nodes, largest: { 1: rooms[0] || 0, 2: rooms[1] || 0, 3: rooms[2] || 0 } };
  $("#lh_nodes").innerHTML = lhNodeRows(preview);
  $("#lh_largest").innerHTML = lhLargest(preview);
};

/* The switch's answer: a policy to save only when it was turned on or off. */
function lhNodeDownChoice() {
  const box = $("#lh_nodedown");
  if (!box) return "";
  const was = box.dataset.was === "delete-both-statefulset-and-deployment-pod";
  return box.checked === was ? "" : box.checked ? "delete-both-statefulset-and-deployment-pod" : "do-nothing";
}
window.lhSettingsSave = async () => {
  const body = { over_provisioning: +$("#lh_over").value, minimal_available: +$("#lh_min").value,
    node_down: lhNodeDownChoice(), ...($("#lh_rebuild") ? { rebuild_limit: +$("#lh_rebuild").value } : {}) };
  try {
    const r = await api("/api/longhorn/settings", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
    const offline = $("#lh_offline"), was = STATE.data.lhOffline;
    if (offline && was && offline.checked !== !!was.enabled)
      await api("/api/longhorn/offline-rebuilding", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ enabled: offline.checked }) });
    toast(r.detail, "ok"); lhSettingsPaint();
  } catch (e) { toast(e.message, "bad"); throw e; }
};

// Node only (tests): the same names, for require(). The browser has no `module`.
if (typeof module === "object" && module.exports) module.exports = {
  lhLevel, lhShort, lhNodeRows, lhLargest, lhCapacityCard, lhSettingsPaint, lhNodeDownChoice
};
