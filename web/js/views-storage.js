/* Architecture, Volumes, Shares, Events */

const ROB = r => r === "healthy" ? "#3ddc91" : r === "degraded" ? "#ffb020" : "#ff4d4f";

/* The architecture page: how each app is reached, what it runs, where its data
   is and which hosts hold the copies - four columns read left to right, sized
   to one desktop screen.

     Access  ->  Containers  ->  Volumes  ->  Nodes (replica copies)

   Lines join a VIP's port to the container behind it, a container to each
   volume it mounts, and a volume to each copy of it. Hovering anything lights
   up what it depends on and what depends on it - following the lines away from
   it in each direction and never back, so a VIP that twelve apps share does
   not light up all twelve when one of them is pointed at. */
async function viewFlow() {
  const [f] = await Promise.all([api("/api/flow"), loadHardwareFeatures()]);
  STATE.data.flow = f;
  const links = [];
  const portId = (ip, port) => `p:${ip}:${port}`;
  f.workloads.forEach(w => w.ports.forEach(p => { if (p.vip) links.push([portId(p.vip, p.port), w.id, "access"]); }));
  f.workloads.forEach(w => w.claims.forEach(c => { if (c.vid) links.push([w.id, c.vid, "mount"]); }));
  f.nodes.forEach(n => n.copies.forEach(c => links.push([c.vid, `${n.id}|${c.vid}`, "copy"])));
  STATE.data.alinks = links;
  const used = new Set(links.flat());
  const mountedVolumes = new Set(links.filter(([, , type]) => type === "mount").map(([, id]) => id));
  const host = w => (w.node || "").replace(/^harvester-/, "");
  const kind = w => w.kind === "vm" ? (w.running ? `VM · ${host(w) || w.state || "starting"}` : `VM · ${(w.state || "stopped").toLowerCase()}`) : host(w);
  const containers = f.workloads.filter(w => w.kind !== "vm");
  const vms = f.workloads.filter(w => w.kind === "vm");
  const connectedVolumes = f.volumes.filter(v => mountedVolumes.has(v.id));
  const disconnectedVolumes = f.volumes.filter(v => !mountedVolumes.has(v.id));
  const workloadCard = w => `<div class="a2item a2wl ${used.has(w.id) ? "" : "a2alone"} ${w.kind === "vm" && !w.running ? "a2stopped" : ""}"
          id="${esc(w.id)}" data-kind="workload" data-id="${esc(w.id)}"
          title="${esc(w.name)} · ${esc(kind(w))}${w.ip ? ` · ${esc(w.ip)}` : ""}${w.kind !== "vm" || w.running ? ` · ${workloadCpuPercent(w.cpu)} CPU · ${w.mem_mb || 0} MB` : ""}">
          ${appAvatar(w.name, w.icon)}<span class="a2name">${esc(w.name)}</span>
          <span class="a2meta">${esc(kind(w))}</span>
          ${w.kind === "vm" && !w.running ? "" : `<button class="a2act" data-need="operator" title="${w.kind === "vm" ? "Migrate" : "Move to another host"}"
            onclick="event.stopPropagation();${w.kind === "vm" ? `vmMove(${jsq(w.ns || "lab")},${jsq(w.name)})` : `moveWorkload(${jsq(w.name)},${jsq(w.ns || "lab")})`}">⇄</button>`}
        </div>`;
  const volumeCard = (v, disconnected = false) => `<div class="a2item a2vol ${used.has(v.id) ? "" : "a2alone"} ${disconnected ? "a2disconnected" : ""}" id="${esc(v.id)}" data-kind="volume" data-id="${esc(v.id)}"
          title="${esc(v.name)} · ${v.size_gb} GB · ${v.replicas} replicas · ${esc(v.robustness)}${v.attached ? ` · attached on ${esc(v.attached)}` : ""}">
          <span class="a2dot" style="background:${ROB(v.robustness)}"></span><span class="a2name">${esc(v.name)}</span>
          <span class="a2meta mono">${v.size_gb}G · ${v.replicas}×</span></div>`;

  paint(`<div class="phead">
      <div><h2>Architecture</h2><p>How each app is reached, where its data lives, and which hosts hold the copies · hover anything to trace it</p></div>
      <div class="row arch-head-actions">
        ${disconnectedVolumes.length ? `<button class="btn sm ${STATE.archDisconnected ? "pri" : ""}" onclick="STATE.archDisconnected=!STATE.archDisconnected;viewFlow()"
          data-tip="Volumes no container or VM is defined to mount. They stay hidden so old and retained data does not obscure the live paths.">${STATE.archDisconnected ? "Hide" : "Show"} ${disconnectedVolumes.length} disconnected</button>` : ""}
        <div class="row hide-sm arch-legend">
          <span><i style="background:var(--arch-access)"></i>port</span>
          <span><i style="background:var(--arch-mount)"></i>mount</span>
          <span><i style="background:var(--arch-copy)"></i>replica</span>
        </div>
      </div></div>
    <div class="arch2wrap"><svg id="archsvg" aria-hidden="true"></svg><div class="arch2">

      <section class="a2col"><h4>Access</h4>
      ${f.vips.map(v => `<div class="a2item a2vip ${v.state && !["ok", "idle"].includes(v.state) ? "a2broken" : ""}" data-kind="access" data-id="${esc(v.id)}"
          data-node="${esc(v.node || "")}" title="${esc(archAddressWords(v))}">
          <div class="a2vipip"><span class="mono">${esc(v.ip)}</span>
            <span class="a2meta">${v.kind === "node" ? "node" : "VIP"}${v.node ? ` · ${esc(v.node.replace(/^harvester-/, ""))}` : ""}</span></div>
          ${v.state && !["ok", "idle"].includes(v.state) ? `<div class="a2why">${esc(v.state === "unrouted" ? "not reachable" : v.state === "pending" ? "port taken" : "no node answers")}</div>` : ""}
          <div class="a2ports">${v.ports.map(p => `<span class="a2port" id="${esc(portId(v.ip, p.port))}" data-kind="access"
            data-id="${esc(portId(v.ip, p.port))}" title="${esc(p.app)} · open ${esc(v.ip)}:${p.port}"
            onclick="openSvc(${jsq(v.ip)},${p.port})">${p.port}</span>`).join("")}</div></div>`).join("")
        || '<div class="dim xs">No load-balancer addresses</div>'}
      </section>

      <section class="a2col"><h4>Workloads <span class="dim">${f.workloads.length}</span></h4>
        <div class="a2group"><div class="a2subhead">Containers <span>${containers.length}</span></div>
          ${containers.map(workloadCard).join("") || '<div class="dim xs">No containers</div>'}</div>
        ${vms.length ? `<div class="a2group a2vmgroup"><div class="a2subhead">Virtual machines <span>${vms.length}</span></div>
          ${vms.map(workloadCard).join("")}</div>` : ""}
      </section>

      <section class="a2col"><h4>Volumes <span class="dim">${f.volumes.length}</span></h4>
        ${connectedVolumes.map(v => volumeCard(v)).join("") || '<div class="dim xs">No connected volumes</div>'}
        ${STATE.archDisconnected && disconnectedVolumes.length ? `<div class="a2group a2disconnected-group">
          <div class="a2subhead badtext">Disconnected <span>${disconnectedVolumes.length}</span></div>
          ${disconnectedVolumes.map(v => volumeCard(v, true)).join("")}</div>` : ""}
      </section>

      <section class="a2col"><h4>Nodes &amp; replica copies</h4>
      ${f.nodes.map(n => `<div class="a2item a2node" data-kind="node" data-id="${esc(n.id)}">
          <div class="a2nodehead"><b>${esc(n.name)}</b><span class="dim xs">${n.copies.length} cop${n.copies.length === 1 ? "y" : "ies"}</span></div>
          ${(n.ips || []).length || (n.vips || []).length ? `<div class="a2addrs">${(n.ips || []).map(ip => `<span class="a2addr mono" title="${esc(n.name)}'s own address">${esc(ip)}</span>`).join("")}${(n.vips || []).map(ip => `<span class="a2addr vip mono" title="${esc(n.name)} answers for VIP ${esc(ip)}">VIP ${esc(ip)}</span>`).join("")}</div>` : ""}
          <div class="a2copies">${n.copies.map(c => `<span class="a2copy ${c.running ? "" : "stopped"}" id="${esc(`${n.id}|${c.vid}`)}"
            data-kind="copy" data-id="${esc(`${n.id}|${c.vid}`)}" title="${esc(c.vol)} on ${esc(n.name)}${c.running ? "" : " · not running"}">${esc(c.vol)}</span>`).join("")
            || '<span class="dim xs">no replicas here</span>'}</div></div>`).join("")}
      </section>
    </div></div>`);
  // A live refresh redraws the page; whatever was being traced stays traced.
  if (STATE.data.archHover) archHighlight(STATE.data.archHover);
  else drawArch();
  archWatch();
}

/* What an address is, where it lives, and why it is not reachable if not. */
function archAddressWords(v) {
  const what = v.kind === "node" ? `${v.ip} is ${v.node || "a node"}'s own address`
    : v.node ? `VIP ${v.ip} · ${v.node} answers for it` : `VIP ${v.ip} · no node answers for it`;
  return v.reason ? `${what} · ${v.reason}` : what;
}

/* Hover and resize, wired once for the page rather than per element, so a
   live refresh that adds a row does not leave it unwired. */
function archWatch() {
  const wrap = $(".arch2wrap");
  if (!wrap || wrap.dataset.watched) return;
  wrap.dataset.watched = "1";
  wrap.addEventListener("mouseover", event => {
    const item = event.target.closest("[data-id]");
    if (item && item.dataset.id !== STATE.data.archHover) archHighlight(item.dataset.id);
  });
  wrap.addEventListener("mouseleave", () => archHighlight(null));
  if (window.ResizeObserver) new ResizeObserver(() => drawArch(STATE.data.archKeep)).observe(wrap);
}

function archHighlight(id) {
  STATE.data.archHover = id;
  const keep = id ? archRelated(id) : null;
  STATE.data.archKeep = keep;
  $$(".arch2 [data-id]").forEach(el => {
    const on = !keep || keep.has(el.dataset.id)
      || (el.classList.contains("a2node") && [...keep].some(k => k.startsWith(el.dataset.id + "|")))
      || (el.classList.contains("a2vip") && [...keep].some(k => k.startsWith("p:" + el.dataset.id.slice(2) + ":")));
    el.classList.toggle("dimmed", !on);
    el.classList.toggle("sel", el.dataset.id === id);
  });
  drawArch(keep);
}

/* Everything upstream and downstream of one item: from it, each link is
   followed left or right, and each step keeps going the same way. */
function archRelated(id) {
  const links = STATE.data.alinks || [];
  const start = [id];
  // A VIP or a node stands for all of its ports or copies.
  if (id.startsWith("i:")) links.forEach(([a]) => { if (a.startsWith("p:" + id.slice(2) + ":")) start.push(a); });
  if (id.startsWith("n:")) links.forEach(([, b]) => { if (b.startsWith(id + "|")) start.push(b); });
  // Where an address lives: its node, and a node's addresses - not followed
  // further, or a node would light every app it answers for.
  const places = (STATE.data.flow?.vips || []).filter(v => v.node);
  const beside = id.startsWith("i:") ? places.filter(v => v.id === id).map(v => "n:" + v.node)
    : id.startsWith("n:") ? places.filter(v => "n:" + v.node === id).map(v => v.id) : [];
  const keep = new Set(start);
  const walk = (from, forward) => {
    links.forEach(([a, b]) => {
      const next = forward ? (a === from ? b : null) : (b === from ? a : null);
      if (next && !keep.has(next)) { keep.add(next); walk(next, forward); }
    });
  };
  start.forEach(item => { walk(item, true); walk(item, false); });
  beside.forEach(item => keep.add(item));
  return keep;
}

function drawArch(keep = STATE.data.archKeep) {
  const svg = $("#archsvg"), wrap = $(".arch2wrap");
  if (!svg || !wrap) return;
  const R = wrap.getBoundingClientRect();
  svg.setAttribute("viewBox", `0 0 ${R.width} ${R.height}`);
  const box = id => {
    const el = document.getElementById(id);
    if (!el || !el.offsetParent) return null;
    const b = el.getBoundingClientRect();
    // A port or a copy sits among others in its box; its line meets the box's
    // edge at its height rather than crossing its neighbours to reach it.
    const edge = el.closest(".a2vip, .a2node")?.getBoundingClientRect() || b;
    return { l: edge.left - R.left, r: edge.right - R.left, y: b.top - R.top + b.height / 2 };
  };
  const motion = SET.motion !== "off";
  svg.innerHTML = (STATE.data.alinks || []).map(([a, b, type]) => {
    const A = box(a), B = box(b);
    if (!A || !B) return "";
    const lit = keep && keep.has(a) && keep.has(b);
    const bend = Math.max(24, (B.l - A.r) / 2);
    const d = `M ${A.r} ${A.y} C ${A.r + bend} ${A.y} ${B.l - bend} ${B.y} ${B.l} ${B.y}`;
    const cls = `a2link ${type}${keep ? (lit ? " lit" : " faded") : ""}`;
    return `<path class="${cls}" d="${d}"/>` + (lit && motion ? `<path class="a2flow ${type}" d="${d}"/>` : "");
  }).join("");
}

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
  if (attachedWorkloads(x).length || x.state === "attached") return { kind: "in-use" };
  if (x.unclaimed) return { kind: "unclaimed" };
  if (x.used_by == null) return { kind: "detached" };
  return x.used_by.length ? { kind: "stopped", by: x.used_by } : { kind: "orphaned" };
}

/* Where a volume's copies are: host and disk, grouped by host. */
function volumeCopiesLine(x) {
  const copies = x.copies || [];
  if (!copies.length) return "";
  const hosts = {};
  copies.forEach(c => (hosts[c.node || "?"] ||= []).push(c));
  return `<span class="dim xs vol-copies" data-tip="${esc(volumeCopiesTip(x))}">on ${Object.entries(hosts).map(([node, rows]) =>
    `${esc(node.replace(/^harvester-/, ""))}: ${rows.map(c => `<span class="${c.healthy ? "" : "warn"}">${esc(c.disk)}</span>`).join(" + ")}`).join(" · ")}</span>`;
}
function volumeCopiesTip(x) {
  const copies = x.copies || [], want = +x.replicas || 0;
  const where = copies.map(c => `${c.node} · ${c.disk}${c.healthy ? "" : ` (${c.state || "not running"})`}`).join(", ");
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
    ? `<div class="attachlist">${attachedWorkloads(x).map(w => `<span class="tag info">${esc(w)}</span>`).join("")}</div>`
    : '<span class="dim">attached</span>';
  if (use.kind === "stopped") return `<div class="attachlist">${use.by.map(w => `<span class="tag" data-tip="${esc(w)} is defined to use this volume and is stopped; the data waits for it to start">${esc(w.split("/").pop())} · stopped</span>`).join("")}</div>`;
  if (use.kind === "orphaned") return `<span class="tag warn" data-tip="No container, VM or job refers to this volume. If its data is not wanted, it can be deleted; otherwise attach it to something from its editor">orphaned</span>`;
  if (use.kind === "unclaimed") return `<span class="tag warn" data-tip="Its claim is gone and the volume was kept - an old copy from a storage class change, or a claim deleted with its data retained. Nothing can mount it as it is">no claim</span>`;
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
  if (restore && restore.error) return `<span class="pill crit">restore failed</span>
    <span class="dim xs volume-reason">${esc(restore.error)}</span>`;
  if (restore) return `<span class="pill info" data-tip="Longhorn is copying this volume's data in from a backup; it can be used once the restore finishes">restoring ${restore.pct}%</span>
    ${volumeProgressBar(restore.pct)}`;
  if (rebuild && rebuild.error) return `<span class="pill crit">rebuild failed</span>
    <span class="dim xs volume-reason">${esc(rebuild.error)}</span>`;
  if (rebuild) return `<span class="pill med" data-tip="Longhorn is copying a replica from a healthy one; the volume is readable and writable meanwhile">rebuilding ${rebuild.pct}%</span>
    ${volumeProgressBar(rebuild.pct)}
    <span class="dim xs volume-reason">${rebuild.replicas === 1 ? "1 replica" : `${rebuild.replicas} replicas`} catching up</span>`;
  return `${x.state === "attached"
    ? `<span class="pill ${x.robustness === "healthy" ? "ok" : x.robustness === "degraded" ? "med" : "crit"}"${x.health_reason ? ` data-tip="${esc(x.health_reason)}"` : ""}>${esc(x.robustness)}</span>`
    // Detached is where a volume sits when nothing is using it - a stopped
    // workload, not a fault. Longhorn calls it detached, so do we.
    : `<span class="pill neutral" data-tip="Nothing is mounting this volume, so Longhorn reports no live replica health">detached</span>`}
    ${volumeReason(x) ? `<span class="dim xs volume-reason">${esc(x.health_reason)}</span>` : ""}`;
}

function volumeUsageCell(x) {
  const fs = x.filesystem;
  const known = fs && Number.isFinite(fs.used_gb) && Number.isFinite(fs.capacity_gb)
    && Number.isFinite(fs.used_pct) && fs.used_pct >= 0 && fs.used_pct <= 100
    && fs.capacity_gb > 0 && fs.used_gb >= 0 && fs.used_gb <= fs.capacity_gb;
  return `<div class="volusage-body">${known
    ? `<div class="volusage-line" data-tip="Filesystem usage reported by kubelet; filesystem capacity can be slightly smaller than the provisioned block device.">${meter(fs.used_pct)}<span class="mono">${esc(fs.used_gb)} / ${esc(fs.capacity_gb)} GiB files</span></div>`
    : `<span class="dim xs" data-tip="No fresh filesystem measurement is available. Detached volumes and some shared/raw-block mounts do not report filesystem usage.">Filesystem usage unavailable</span>`}
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
    }
    volumeProgressWatch();
  }, 4000);
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
    ${stuck.length ? `<div class="note warn small" style="margin-bottom:10px"><b>${stuck.length} volume${stuck.length === 1 ? " is" : "s are"} not made.</b>
      Usually a shared (ReadWriteMany) volume asked of a class that serves one node - local-path does - which never makes it. Delete it and make it again on a class that shares, or as a single-node volume.</div>` : ""}
    <div class="card flat pad0"><div class="tblwrap"><table class="tbl stack dense"><thead><tr><th>Volume</th><th>Class</th><th>Size</th><th>Mode</th><th>State</th></tr></thead><tbody>
    ${rows.map(r => `<tr><td><b>${esc(r.name)}</b><div class="dim xs">${esc(r.namespace)}</div></td>
      <td data-label="Class" class="mono">${esc(r.storage_class)}</td><td data-label="Size" class="mono">${esc(r.size || "—")}</td>
      <td data-label="Mode"><span class="tag">${esc(r.access_modes.map(m => m === "ReadWriteMany" ? "RWX" : m === "ReadWriteOnce" ? "RWO" : m).join(", ") || "?")}</span></td>
      <td data-label="State"><span class="pill ${r.phase === "Bound" ? "ok" : "crit"}">${esc(r.phase === "Bound" ? "in use" : r.phase || "unknown")}</span>
        ${r.reason ? `<div class="dim xs" style="margin-top:4px">${esc(r.reason)}</div>` : ""}</td></tr>`).join("")}</tbody></table></div></div>`;
}

async function viewStorage() {
  if (platformLacks("longhorn", "Volumes")) return;
  const [v, st, classes, v2, cap] = await Promise.all([api("/api/volumes"), api("/api/storage").catch(() => null),
    api("/api/storage/classes").catch(() => []), api("/api/storage/v2").catch(() => null),
    api("/api/longhorn/capacity").catch(() => null)]);
  const [oldCopies, others] = await Promise.all([api("/api/volumes/old-copies").catch(() => []), api("/api/volumes/other").catch(() => [])]);
  STATE.data.lhcap = cap || STATE.data.lhcap;
  STATE.data.storageClasses = classes;
  STATE.data.v2 = v2;
  STATE.data.vols = v;
  const q = STATE.q.toLowerCase();
  const spare = v.filter(x => ["orphaned", "unclaimed"].includes(volumeUse(x).kind));
  const onlySpare = STATE.volSpare && spare.length;
  const rows = v.filter(x => (!q || x.name.includes(q) || (x.node || "").includes(q) ||
    (x.pvc_name || "").includes(q) || (x.attached_to || "").toLowerCase().includes(q))
    && (!onlySpare || spare.includes(x)));
  paint(`<div class="phead"><div><h2>Volumes</h2>
      <p>${v.length} Longhorn volume${v.length === 1 ? "" : "s"} · replicated block storage${onlySpare ? ` · <a class="linkish" onclick="STATE.volSpare=false;viewStorage()">showing ${spare.length} unused · show all</a>` : ""}</p></div>
      <div class="row">${moreMenu([spare.length ? { label: onlySpare ? "Show all volumes" : `Show the ${spare.length} unused`, icon: "list", run: "STATE.volSpare=!STATE.volSpare;viewStorage()",
          tip: "Volumes nothing is defined to use - no container, VM or job - and ones kept after their claim went: the ones to look at when freeing space" } : null])}
      <button class="btn pri" data-need="operator" onclick="volumeCreate()">＋ Create volume</button></div></div>
  ${st ? summaryLine("volumes", [
      `<b>${st.avail_gb} GB</b> free of ${st.cap_gb}`, `<b>${st.provisioned_gb} GB</b> provisioned`,
      `<span class="tag ok">${st.healthy} healthy</span>${st.degraded ? ` <span class="tag warn">${st.degraded} degraded</span>` : ""}${st.faulted ? ` <span class="tag bad">${st.faulted} faulted</span>` : ""}`,
      `${st.attached} of ${st.volumes} attached`],
    `<div class="grid g4 statgrid">
    <div class="card flat"><div class="ctitle">Free space</div>
      <div class="bignum" style="margin-top:8px">${st.avail_gb}<span class="unit">GB</span></div>
      <div class="csub">of ${st.cap_gb} GB raw</div>${meter(st.used_pct, 'style="margin-top:10px"')}</div>
    <div class="card flat"><div class="ctitle">Provisioned</div>
      <div class="bignum" style="margin-top:8px">${st.provisioned_gb}<span class="unit">GB</span></div>
      <div class="csub" data-tip="Sum of Longhorn volume block footprints, including snapshots; not filesystem usage or total storage across all replica copies.">${st.actual_gb} GiB Longhorn footprint</div></div>
    <div class="card flat statwide"><div class="ctitle">Replica health</div>${(st.reasons || []).length
      ? `<div class="dim xs volume-reason" style="margin-top:8px">${esc(st.reasons[0].name)}: ${esc(st.reasons[0].reason)}${st.reasons.length > 1 ? ` · and ${st.reasons.length - 1} more` : ""}</div>` : ""}
      <div class="row" style="margin-top:10px;gap:8px;flex-wrap:wrap">
        <span class="tag ok">${st.healthy} healthy</span>
        ${st.degraded ? `<span class="tag warn">${st.degraded} degraded</span>` : ""}
        ${st.faulted ? `<span class="tag bad">${st.faulted} faulted</span>` : ""}
        ${(st.detached ?? st.unknown) ? `<span class="tag">${st.detached ?? st.unknown} detached</span>` : ""}</div>
      <div class="csub" style="margin-top:10px">${st.attached} attached of ${st.volumes}</div></div>
    ${lhCapacityCard(cap, st)}
  </div>`, "Per node") : ""}
  <div class="card flat pad0"><div class="tblwrap voltable"><table data-sort="volumes" class="tbl dense"><thead><tr>
   <th>Volume</th><th>Attached to</th><th>Health</th><th>Mode</th><th>Usage</th><th data-nosort>Last used</th><th></th>
   </tr></thead><tbody>${rows.map(x => `<tr data-vol="${esc(x.name)}"${clusterAttr(x)}>
     <td class="volname"><div class="volname-content"><b>${esc(x.pvc_name || x.name.slice(0, 18))}</b> ${clusterTag(x)}
       <span class="dim xs mono">${esc(x.namespace || "")}${x.node ? ` · ${esc(x.node.replace("harvester-", ""))}` : ""}</span>
       ${volumeCopiesLine(x)}</div></td>
     <td data-label="Attached to">${volumeUseCell(x)}${x.pod_status ? `<span class="dim xs"> · ${esc(x.pod_status)}</span>` : ""}</td>
     <td data-label="Health" class="volhealth${volumeReason(x) || volumeBusy(x) ? " hasreason" : ""}">${volumeHealthCell(x)}</td>
     <td data-label="Mode"><span class="tag">${esc((x.access_modes || ["?"]).map(m => m === "ReadWriteMany" ? "RWX" : m === "ReadWriteOnce" ? "RWO" : m).join(", "))}</span>
       <span class="tag ${+x.replicas === 1 || (x.copies?.length && x.copies.length < +x.replicas) ? "warn" : ""}" data-tip="${esc(volumeCopiesTip(x))}">×${esc(x.replicas)}</span><span class="tag ${x.engine === "v2" ? "info" : ""}" data-tip="${x.engine === "v2" ? "Longhorn's V2 data engine (SPDK)" : "Longhorn's V1 data engine - the default"}">${x.engine === "v2" ? "V2" : "V1"}</span></td>
     <td data-label="Usage" class="volusage">${volumeUsageCell(x)}</td>
     <td data-label="Last used" class="small dim">${x.state === "attached" ? '<span class="tag">in use</span>' : esc(fmtAgo(x.last_used_secs))}</td>
     <td class="volactions"><div class="row">
       <button class="iconbtn" data-tip="Snapshots and backups of this volume: take one now, or restore" onclick="lhSnaps(${jsq(x.name)},${jsq(x.pvc_name || x.name)})">${icon("snapshot")}</button>
       <button class="iconbtn" data-need="operator" data-tip="Resize or change replicas" onclick='volumeEdit(${JSON.stringify(x).replace(/'/g, "&#39;")})'>${icon("edit")}</button>
       <details class="actionmenu"><summary class="iconbtn" title="More actions" aria-label="More actions for ${esc(x.pvc_name || x.name)}">⋯</summary><div class="actionmenu-pop">
         <button data-need="admin" title="Browse and edit the files on this volume" onclick="this.closest('details').open=false;volumeFiles(${jsq(x.namespace || "lab")},${jsq(x.pvc_name || x.name)},${x.state === "attached"})">${icon("list")}Browse files</button>
         <button data-need="admin" title="Copy it to a volume on another class, under the same name" onclick='this.closest("details").open=false;volumeReclass(${JSON.stringify(x).replace(/'/g, "&#39;")})'>${icon("move")}Change storage class</button>
         <button data-need="admin" title="Hand this volume's files to the user the container runs as" onclick="this.closest('details').open=false;volumeChown(${jsq(x.namespace || "lab")},${jsq(x.pvc_name || x.name)})">${icon("shield")}Fix ownership</button>
         <button class="danger" data-need="admin" title="Review attachment and data-loss impact before deleting" onclick='this.closest("details").open=false;volumeDelete(${JSON.stringify(x).replace(/'/g, "&#39;")})'>${icon("trash")}Delete</button>
       </div></details>
     </div></td>
      </tr>`).join("") || `<tr><td colspan=7 class="empty">none</td></tr>`}
   </tbody></table></div></div>
  ${oldCopies.length ? `<div class="sec">Old copies ${tip("The original of a volume moved to another storage class, kept in case the new copy disappoints. Remove each once its app works on the new one.")}</div>
    <div class="card flat pad0"><div class="tblwrap"><table class="tbl dense"><thead><tr><th>Was</th><th>Class</th><th>Size</th><th></th></tr></thead><tbody>
    ${oldCopies.map(o => `<tr><td><b>${esc(o.was)}</b><div class="dim xs mono">${esc(o.pv)}</div></td><td>${esc(o.storage_class)}</td><td class="mono">${esc(o.size)}</td>
      <td><button class="btn sm danger" data-need="admin" onclick="reclassRemoveOld(${jsq(o.pv)})">Remove</button></td></tr>`).join("")}</tbody></table></div></div>` : ""}
  ${storageClassCard(classes, v2)}
  ${otherVolumesHtml(others)}`);
  volumeProgressWatch();
}
window.volumeCreate = async () => {
  const [nss, classes] = await Promise.all([api("/api/namespaces"),
    api("/api/storageclasses?facts=1").catch(() => ({ names: [], facts: {}, shared: [] }))]);
  const scs = classes.names || [];
  window.__volumeClasses = classes;
  modal("Create volume", `<div class="f2"><div class="f"><label>Name</label><input id="vc_name" placeholder="frigate-media"></div>
    <div class="f"><label>Namespace</label><select id="vc_ns">${nss.map(n => `<option ${n === "lab" ? "selected" : ""}>${esc(n)}</option>`).join("")}</select></div></div>
    <div class="f2"><div class="f"><label>Size (GB)</label><input id="vc_size" type="number" min="1" value="10"></div>
    <div class="f"><label>Access mode ${tip("RWO mounts on one node at a time and suits most apps. RWX can mount on several nodes, using Longhorn's shared-volume support.")}</label><select id="vc_mode"><option value="ReadWriteOnce">RWO · one node</option><option value="ReadWriteMany">RWX · many nodes</option></select></div></div>
    <div class="f"><label>Storage class</label><select id="vc_sc" onchange="volumeClassFacts()">${storageClassOptions(scs, "longhorn-r2", classes.facts)}</select>
      <div class="vclass-badges" id="vc_badges"></div></div>
    <div class="note" id="vc_mode_note" hidden></div>
    <div class="row"><button class="btn pri" onclick="volumeCreateNow()">Create volume</button><button class="btn" onclick="closeModal()">Cancel</button></div>`);
  $("#vc_mode").addEventListener("change", volumeClassFacts);
  volumeClassFacts();
};
window.volumeCreateNow = async () => {
  const body = { name: $("#vc_name").value.trim(), namespace: $("#vc_ns").value, size_gb: +$("#vc_size").value,
    access_mode: $("#vc_mode").value, storage_class: $("#vc_sc").value };
  try { await api("/api/volumes/create", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
    toast(`${body.name} created`, "ok"); closeModal(); resetPaint(); viewStorage(); } catch (e) { toast(e.message, "bad"); }
};
window.volumeEdit = (x, fromRoute = false) => {
  if (!fromRoute && window.setModalRoute) setModalRoute({ panel: "edit", ns: x.namespace || "lab", volume: x.pvc_name || x.name }, x.pvc_name || x.name);
  modal("Edit · " + (x.pvc_name || x.name), `
  <div class="f2"><div class="f"><label>Size (GB) ${tip("Volumes can be grown but not shrunk.")}</label><input id="ve_size" type="number" min="${Math.ceil(x.size_gb)}" value="${Math.ceil(x.size_gb)}"></div>
  <div class="f"><label>Replica count</label><input id="ve_reps" type="number" min="1" max="5" value="${x.replicas}"></div></div>
  <div class="note"><b>${esc((x.access_modes || []).join(", ") || "Access mode unknown")}</b> · ${esc(x.storage_class || "storage class unknown")}<br>
  Kubernetes locks access mode and storage class after a claim is bound. To change RWO ↔ RWX, create a new volume and migrate the data.</div>
  <div class="row" style="margin-top:16px"><button class="btn pri" onclick="volumeEditNow(${jsq(x.namespace || "lab")},${jsq(x.pvc_name || x.name)})">Save</button><button class="btn" onclick="closeModal()">Cancel</button></div>`);
};
window.volumeEditNow = async (namespace, name) => {
  const body = { namespace, name, size_gb: +$("#ve_size").value, replicas: +$("#ve_reps").value };
  try { const r = await api("/api/volumes/edit", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
    toast(r.detail || `${name} updated`, "ok"); closeModal(); resetPaint(); viewStorage(); } catch (e) { toast(e.message, "bad"); }
};

function volumeDeleteConfirmationValid(name, value) {
  return String(value || "").trim() === String(name || "");
}
window.volumeDeleteConfirmationValid = volumeDeleteConfirmationValid;

window.volumeDelete = async x => {
  const namespace = x.namespace || "lab", name = x.pvc_name || x.name;
  modal("Delete volume · " + name,
    '<div class="empty"><span class="spin2"></span> checking mounts, snapshots, backups and reclaim policy…</div>', true);
  let p;
  try {
    p = await api(`/api/volumes/delete-plan?ns=${encodeURIComponent(namespace)}&name=${encodeURIComponent(name)}&volume=${encodeURIComponent(x.name)}`);
  } catch (e) {
    return void ($("#mbody").innerHTML = UI.callout("bad", "Impact check failed.", esc(e.message)) + UI.actions(UI.cancel("Close")));
  }
  window.__volumeDeletePlan = p;
  const stale = new Set((p.stale_consumers || []).map(c => `${c.kind}/${c.name}`));
  const lh = p.longhorn || {}, pv = p.pv || {};
  const blocked = p.blocked;
  const permanentBlocked = !p.actions?.delete_data?.enabled;
  const detached = !!p.actions?.detach?.complete;
  const jobs = p.removable_jobs || [];
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
      : UI.callout("ok", "Impact check passed.", p.orphan ? "The PVC is already gone. This retained backing volume is detached; deleting it permanently removes its remaining data."
        : "This claim is detached and has no active workload references. Choose what should happen to its backing data."),
    UI.facts([
      ["Longhorn footprint", `${esc(lh.actual_gb ?? "?")} GiB`],
      ["Replicas", esc(lh.replicas ?? "?")],
      ["Snapshots on volume", esc(p.snapshots?.count ?? "?")],
      ["External backups", esc(p.backups?.count ?? "?")],
    ]),
    UI.section("1. Detach", UI.checklist([
      { state: detached ? "ok" : "bad", title: detached ? "Already detached." : "Stop and unmount this claim first.",
        detailHtml: `Detaching keeps the PVC and all data. Homestead will not silently rewrite or stop the workloads listed below.${(p.stale_consumers || []).length ? " Entries marked finished have stopped for good and do not hold it." : ""}` },
      jobs.length && { state: "ok", title: `Finished ${jobs.length === 1 ? "job" : "jobs"} will be removed first`,
        detailHtml: `<span class="mono">${jobs.map(esc).join(", ")}</span> already completed - typically the copy job from an import. Kubernetes could otherwise hold the claim in Terminating.` },
    ]) + (consumers.length ? UI.table([{ label: "Used by" }, { label: "Mounts" }], consumers)
      : `<div class="ui-empty">No pods, controllers, jobs or virtual machines reference this claim.</div>`)),
    UI.section("2. Choose what happens to the data", `<div class="volume-delete-grid">
      <label class="volume-delete-option ${blocked || p.orphan ? "disabled" : ""}"><input type="radio" name="vd_action" value="delete_claim" onchange="volumeDeleteGate()" ${blocked || p.orphan ? "disabled" : ""}>
        <span><b>Delete claim, keep data</b><small>The PV policy becomes Retain, then the PVC is deleted. The released data needs Kubernetes or Longhorn administration to recover or remove later.</small></span></label>
      <label class="volume-delete-option danger ${permanentBlocked ? "disabled" : ""}"><input type="radio" name="vd_action" value="delete_data" onchange="volumeDeleteGate()" ${permanentBlocked ? "disabled" : ""}>
        <span><b>Permanently delete data</b><small>${p.orphan ? "The PVC is already absent. Longhorn and CSI remove the retained backing objects through their normal controllers; finalizers are never forced." : "The PV policy becomes Delete, and the PVC and backing PV are removed."} The Longhorn volume, replicas and ${count(p.snapshots?.count || 0, "on-volume snapshot are", "on-volume snapshots are")} removed; ${count(p.backups?.count || 0, "external backup remains", "external backups remain")}.</small></span></label>
    </div>`),
    UI.more("Objects affected", UI.facts([
      ["Claim", `<span class="mono">${esc(p.namespace)}/${esc(p.name)}</span> · ${esc(p.phase)} · ${esc(p.requested_storage || "size unknown")}`],
      ["Persistent volume", `<span class="mono">${esc(pv.name || "not bound")}</span> · reclaim policy ${esc(pv.reclaim_policy || "unknown")}`],
      ["Longhorn volume", `<span class="mono">${esc(lh.name || "not found")}</span> · ${esc(lh.state || "unknown")}${lh.attached_node ? ` on ${esc(lh.attached_node)}` : ""}`],
    ])),
    UI.section("3. Confirm", UI.field(`Type ${name} to confirm`,
      `<input id="vd_confirm" autocomplete="off" placeholder="${esc(name)}" oninput="volumeDeleteGate()">`)),
    UI.actions(UI.cancel() + UI.button("Delete selected", `volumeDeleteNow(${jsArg(namespace)},${jsArg(name)},${jsArg(p.uid)})`,
      { kind: "danger", id: "vd_go", disabled: true, attrs: 'data-need="admin"' })),
  ].join("");
};

window.volumeDeleteGate = () => {
  const p = window.__volumeDeletePlan;
  const action = document.querySelector('input[name="vd_action"]:checked')?.value || "";
  const input = $("#vd_confirm"), button = $("#vd_go");
  if (!p || !input || !button) return;
  const allowed = action && !p.blocked && p.actions?.[action]?.enabled;
  button.disabled = !allowed || !volumeDeleteConfirmationValid(p.name, input.value);
  button.textContent = action === "delete_claim" ? "Delete claim · keep data" : action === "delete_data" ? "Permanently delete data" : "Delete selected";
};

window.volumeDeleteNow = async (namespace, name, uid) => {
  const action = document.querySelector('input[name="vd_action"]:checked')?.value || "";
  const confirmation = $("#vd_confirm").value.trim();
  if (!volumeDeleteConfirmationValid(name, confirmation)) return toast("type the volume name exactly to confirm", "bad");
  if (!action) return toast("choose whether to retain or permanently delete the backing data", "bad");
  const button = $("#vd_go"); button.disabled = true; button.textContent = "Requesting deletion…";
  try {
    const result = await api("/api/volumes/delete", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ namespace, name, uid, action, confirmation,
        volume: window.__volumeDeletePlan?.longhorn?.name || "", pv_uid: window.__volumeDeletePlan?.pv?.uid || "" }) });
    toast(result.message || `${name} deletion started`, "ok"); closeModal();
    if (window.noteOperation) noteOperation(result.operation);
    resetPaint(); viewStorage();
  } catch (e) {
    button.disabled = false; volumeDeleteGate(); toast(e.message, "bad");
  }
};

/* Longhorn's V2 engine in one line: whether it is on, and how many nodes can
   hold its volumes. Shown once it is on or something already uses it. */
function v2Summary(v2, rows) {
  if (!v2 || (!v2.enabled && !rows.some(row => row.engine === "v2"))) return "";
  const text = !v2.enabled ? "switched off, yet a class uses it: its volumes will not schedule"
    : v2.ready_nodes ? `${v2.ready_nodes} of ${v2.total_nodes} node${v2.total_nodes === 1 ? "" : "s"} ready for its volumes`
    : "on, but no node has a V2 disk and hugepages yet";
  return `<div class="dim xs v2line"><span class="tag ${v2.enabled && v2.ready_nodes ? "info" : "warn"}">Longhorn V2</span> ${esc(text)}
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
  disk: h => h ? "Hosts → Edit Config → Storage → Add Disk, provisioner Longhorn V2, on a disk holding nothing you need - or Homestead's Nodes → the host → Disks."
    : "Nodes → the host → Disks → an unused disk → Give to Longhorn → V2. It takes the whole disk as a raw block device, so it must hold nothing you need.",
  nvme: h => h ? "Harvester's OS includes it." : "On each host: sudo apt-get install -y nvme-cli (or sudo dnf install -y nvme-cli). Homestead cannot see the host's packages, so this one is yours to check: nvme version.",
  cpu_core: () => "Longhorn's V2 engine polls rather than waiting: on every node running it, one CPU core stays busy. Longhorn's v2-data-engine-cpu-mask setting chooses which.",
};
const v2Mark = ok => ok === true ? '<span class="v2mark ok" aria-label="done">✓</span>'
  : ok === false ? '<span class="v2mark bad" aria-label="missing">✗</span>' : '<span class="v2mark unknown" aria-label="not known">?</span>';
function v2Item(ok, label, how, detail = "") {
  return `<div class="v2item">${v2Mark(ok)}<div><b>${esc(label)}</b> ${tip(how)}${detail ? `<div class="dim xs">${esc(detail)}</div>` : ""}</div></div>`;
}
window.v2Details = async (fresh = false) => {
  let v2 = STATE.data.v2;
  if (fresh || !v2?.nodes?.[0]?.checks) v2 = STATE.data.v2 = await api("/api/storage/v2").catch(() => v2 || { nodes: [] });
  const h = v2.harvester_setting !== null && v2.harvester_setting !== undefined, d = v2.distribution;
  const nodes = v2.nodes || [];
  const count = key => nodes.filter(n => n.checks?.[key] === true).length;
  const col = (key, label) => `<th>${esc(label)} ${tip(V2_HOW[key](h, d))}</th>`;
  const cell = (n, key, label, extra = "") => `<td data-label="${esc(label)}">${v2Mark(n.checks?.[key])}${extra ? ` <span class="dim xs">${esc(extra)}</span>` : ""}</td>`;
  modal("Longhorn V2 data engine", `<p class="muted small" style="margin-top:0">V2 is Longhorn's SPDK engine: lower latency and less CPU per I/O than V1.
      A V2 volume schedules only on hosts where everything below is ticked. Hover the <b>?</b> beside any line for how to do it${h ? " on Harvester" : ` on ${d === "rke2" ? "RKE2" : d === "k3s" ? "k3s" : "this cluster"}`}.</p>
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
        <td data-label="Ready">${n.ready ? '<span class="pill ok">ready</span>' : '<span class="pill">not yet</span>'}</td></tr>`).join("")
        || '<tr><td colspan="6" class="empty">Longhorn reported no nodes</td></tr>'}</tbody></table></div></div>
    <div class="row" style="margin-top:12px"><button class="btn sm" onclick="v2Details(true)">${icon("refresh")}Check again</button>
      <span class="dim xs">${v2.ready_nodes ?? 0} of ${v2.total_nodes ?? 0} host${v2.total_nodes === 1 ? "" : "s"} ready. A volume keeping 2 copies needs 2 ready hosts.</span></div>`, true);
  if (window.applyRole) applyRole();
};

function storageClassCard(classes, v2 = null) {
  const every = classes || [];
  if (!every.length) return "";
  // Classes made for one image or one restore are not for choosing: folded
  // away, with the restore ones - spent once their claim is bound - clearable.
  const special = every.filter(row => row.made_for);
  const restores = special.filter(row => row.made_for === "restore");
  const rows = STATE.showSpecialClasses ? every : every.filter(row => !row.made_for);
  const specialLine = special.length ? `<div class="dim xs" style="padding:10px 16px">${special.length} class${special.length === 1 ? "" : "es"} made for
      ${[special.length - restores.length ? `${special.length - restores.length} Harvester image${special.length - restores.length === 1 ? "" : "s"}` : "", restores.length ? `${restores.length} restore${restores.length === 1 ? "" : "s"}` : ""].filter(Boolean).join(" and ")}
      ${STATE.showSpecialClasses ? "shown" : "hidden"}, and never offered when choosing a class.
      <a style="cursor:pointer;text-decoration:underline" onclick="STATE.showSpecialClasses=!STATE.showSpecialClasses;viewStorage()">${STATE.showSpecialClasses ? "Hide" : "Show"} them</a>
      ${restores.length ? ` · <button class="btn sm" data-need="admin" onclick="storageClassCleanup()">Remove the restore ones</button>` : ""}</div>` : "";
  return `<div class="card flat pad0" style="margin-top:18px">
    <div class="between storage-class-head">
      <div><div class="ctitle">Storage classes</div>
        <div class="csub">What a new volume is built from. Kubernetes fixes a class at creation, so Homestead creates and removes them rather than editing them in place.</div>
        ${v2Summary(v2, rows)}</div>
      <button class="btn pri" data-need="admin" onclick="storageClassCreate()">＋ New storage class</button></div>
    <div class="tblwrap"><table data-sort="storage-classes" class="tbl stack storage-class-table"><thead><tr>
      <th>Class</th><th>Engine</th><th>Replicas</th><th>Shared (RWX)</th><th>Encryption</th><th>Expansion</th><th>Volumes</th><th></th>
    </tr></thead><tbody>${rows.map(row => `<tr>
      <td><b>${esc(row.name)}</b>${row.default ? '<span class="tag ok">default</span>' : ""}${row.internal ? '<span class="tag">Harvester internal</span>' : ""}${row.made_for === "image" ? '<span class="tag" data-tip="Harvester made it for one image: disks from that image are made on it">image</span>' : row.made_for === "restore" ? '<span class="tag warn" data-tip="Made to read one backup into a new volume; not needed once that volume exists">restore</span>'
        : row.made_for === "iso" ? '<span class="tag" data-tip="ISO copies Homestead made for VM CD-ROM drives: one replica, since the originals are on your shares">ISO copies</span>' : ""}
        <div class="dim xs mono">${esc(row.provisioner || "")}</div>
        ${(row.disk_tags || []).length || (row.node_tags || []).length ? `<div class="row" style="gap:4px;margin-top:4px">
          ${(row.disk_tags || []).map(t => `<span class="tag info" data-tip="Only on disks tagged ${esc(t)}">disk: ${esc(t)}</span>`).join("")}
          ${(row.node_tags || []).map(t => `<span class="tag" data-tip="Only on nodes tagged ${esc(t)}">node: ${esc(t)}</span>`).join("")}</div>` : ""}</td>
      <td data-label="Engine">${row.engine === "v2" ? '<span class="tag info" data-tip="Longhorn V2 (SPDK)">V2</span>' : row.engine ? '<span class="tag">V1</span>' : '<span class="dim">—</span>'}</td>
      <td class="mono" data-label="Replicas">${esc(row.replicas || "—")}</td>
      <td data-label="Shared (RWX)">${row.migratable
        ? '<span class="pill low" data-tip="This class creates live-migratable volumes for VM disks. Longhorn cannot mount those into a pod, so it cannot back shared storage.">VM disks only</span>'
        : '<span class="pill ok">usable</span>'}</td>
      <td data-label="Encryption">${row.encrypted ? '<span class="tag info">encrypted</span>' : '<span class="dim">—</span>'}</td>
      <td data-label="Expansion">${row.expandable ? '<span class="tag ok">can grow</span>' : '<span class="tag">fixed size</span>'}</td>
      <td class="mono" data-label="Volumes">${row.in_use ?? 0}</td>
      <td><div class="row" style="gap:6px;flex-wrap:nowrap">
        ${row.default || row.internal || row.made_for ? "" : `<button class="btn sm" data-need="admin" title="Use this class when nothing else is chosen" onclick="storageClassDefault(${jsq(row.name)})">Make default</button>`}
        ${row.internal || row.default || row.in_use ? "" : `<button class="btn sm danger" data-need="admin" onclick="storageClassDelete(${jsq(row.name)})">${icon("trash")}Delete</button>`}
      </div></td></tr>`).join("")}</tbody></table></div>${specialLine}</div>`;
}
window.storageClassCleanup = async () => {
  try {
    const r = await api("/api/storage/classes/cleanup", { method: "POST", headers: { "Content-Type": "application/json" }, body: "{}" });
    toast(r.detail, "ok"); viewStorage();
  } catch (e) { toast(e.message, "bad"); }
};
window.storageClassCreate = () => {
  modal("New storage class", `
    <p class="muted small">A storage class is a recipe Longhorn follows when it creates a volume:
      how many replicas to keep, whether the volume can grow, and what happens to the data when its
      claim is deleted. Kubernetes will not let those settings change afterwards, so choose them now.</p>
    <div class="f" style="margin-top:14px"><label>Name</label>
      <input type="text" id="sc_name" placeholder="longhorn-r3" autocomplete="off"></div>
    <div class="f2"><div class="f"><label>Replicas ${tip("Copies of each volume Longhorn keeps. One has no redundancy.")}</label>
      <input type="number" id="sc_reps" min="1" max="5" value="2" oninput="storageClassReach()"></div>
      <div class="f"><label>Copies go on ${tip("Different hosts: each copy on a host of its own, so a host or a disk can fail - Longhorn's default. Different disks: copies may share a host but never a disk - on one host, a failed disk is survived but a failed host is not.")}</label>
        <select id="sc_copies" onchange="storageClassReach()"><option value="hosts">Different hosts</option><option value="disks">Different disks, same host allowed</option></select></div></div>
    <div class="f2">
      <div class="f"><label>Reclaim policy ${tip("Retain keeps a volume's data when its claim is deleted - with an app, say - and lists it on Volumes as having no claim, to reuse or delete there. Delete removes the data with the claim.")}</label>
        <select id="sc_reclaim"><option>Retain</option><option>Delete</option></select></div>
    <div class="f"><label>Data engine ${tip("V1 is Longhorn's long-standing engine. V2 (SPDK) is faster and needs the engine switched on, a V2 disk and hugepages on the nodes.")}</label>
      <select id="sc_engine" onchange="storageClassEngine()"><option value="v1">V1 · the default</option><option value="v2">V2 · SPDK</option></select></div></div>
    <div class="note" id="sc_engine_note" hidden></div>
    <div class="f"><label>Only on disks tagged ${tip("Longhorn puts this class's replicas only on disks with every tag chosen - tag your SSDs ssd and pick it here. Tag disks under Nodes, or Volumes → Disks.")}</label>
      <div class="row sc-tags" id="sc_disktags"><span class="dim xs">loading tags…</span></div></div>
    <div class="f"><label>Only on nodes tagged ${tip("And only on nodes with every tag chosen here.")}</label>
      <div class="row sc-tags" id="sc_nodetags"></div>
      <div class="dim xs" id="sc_reach" style="margin-top:6px"></div></div>
    <label class="switch"><input type="checkbox" id="sc_expand" checked> Allow volumes to grow later</label>
    <label class="switch"><input type="checkbox" id="sc_migratable" onchange="storageClassHint()"> Live-migratable · for VM disks</label>
    <div class="note" id="sc_hint">Leave migratable off for container storage: a migratable volume gets a second controller so a VM can move between hosts, and Longhorn refuses to mount that kind into a pod — which is what breaks ReadWriteMany.</div>
    <label class="switch"><input type="checkbox" id="sc_default"> Make this the default class</label>
    <div class="row" style="margin-top:18px"><button class="btn pri" id="sc_go" data-need="admin" onclick="storageClassSave(this)">Create class</button>
      <button class="btn" onclick="closeModal()">Cancel</button></div>`);
  storageClassTags();
};
/* The tags disks and nodes have, to choose from, and which nodes a choice
   leaves - a class needs one node per replica. */
async function storageClassTags() {
  const inv = await loadDisks().catch(() => null);
  const box = (id, tags, empty) => {
    const host = $(id);
    if (!host) return;
    host.innerHTML = tags.length ? tags.map(t => `<label class="daychip"><input type="checkbox" value="${esc(t)}" onchange="storageClassReach()"><span>${esc(t)}</span></label>`).join("")
      : `<span class="dim xs">${empty}</span>`;
  };
  if (!inv) { box("#sc_disktags", [], "Could not read the disks' tags."); return; }
  STATE.data.scInv = inv;
  box("#sc_disktags", inv.disk_tags || [], "No disk has a tag yet: add them to disks under Nodes, or Volumes → Disks.");
  box("#sc_nodetags", inv.all_node_tags || [], "No node has a tag yet.");
  // One host: only "different disks" can place a second copy.
  if (Object.keys(inv.nodes || {}).length === 1 && $("#sc_copies")) $("#sc_copies").value = "disks";
  storageClassReach();
}
window.storageClassReach = () => {
  const inv = STATE.data.scInv, out = $("#sc_reach");
  if (!inv || !out) return;
  const disk = $$("#sc_disktags input:checked").map(b => b.value), node = $$("#sc_nodetags input:checked").map(b => b.value);
  const reps = +($("#sc_reps").value || 1), byDisk = $("#sc_copies")?.value === "disks";
  const fits = (name, x) => x.scheduling && node.every(t => ((inv.node_tags || {})[name] || []).includes(t)) && disk.every(t => (x.tags || []).includes(t));
  const places = Object.entries(inv.nodes || {}).flatMap(([name, disks]) =>
    disks.flatMap(d => d.longhorn.filter(x => fits(name, x)).map(x => ({ name, disk: x.id }))));
  const hosts = [...new Set(places.map(p => p.name))];
  const count = byDisk ? places.length : hosts.length, unit = byDisk ? "disk" : "host";
  out.className = count >= reps ? "dim xs" : "xs badtext";
  out.textContent = !count ? `No ${disk.length || node.length ? "tagged " : ""}disk can hold its copies, so its volumes would not schedule.`
    : `${count} ${unit}${count === 1 ? "" : "s"} can hold its copies${byDisk ? ` (on ${hosts.join(", ")})` : `: ${hosts.join(", ")}`}`
      + (count < reps ? ` - fewer than ${reps}, so volumes would run a copy short${!byDisk && places.length >= reps ? '. Choose "Different disks" to place them on one host' : ""}.` : ".");
};
window.storageClassEngine = async () => {
  const note = $("#sc_engine_note");
  if ($("#sc_engine").value !== "v2") { note.hidden = true; return; }
  note.hidden = false;
  note.textContent = "Checking the nodes…";
  const v2 = STATE.data.v2 = await api("/api/storage/v2").catch(() => STATE.data.v2);
  if (!v2) { note.textContent = "Could not read Longhorn's V2 status."; return; }
  note.classList.toggle("bad", !v2.enabled || !v2.ready_nodes);
  note.innerHTML = !v2.enabled
    ? "<b>V2 is switched off in Longhorn.</b> A class can be made now, but its volumes will not schedule until it is on. <a onclick=\"v2Details()\" style=\"cursor:pointer;text-decoration:underline\">How to turn it on</a>"
    : v2.ready_nodes < +($("#sc_reps").value || 1)
      ? `<b>${v2.ready_nodes} node${v2.ready_nodes === 1 ? " is" : "s are"} ready for V2</b>, fewer than the replicas asked for, so volumes will run degraded or not schedule. <a onclick="v2Details()" style="cursor:pointer;text-decoration:underline">What each node needs</a>`
      : `${v2.ready_nodes} of ${v2.total_nodes} nodes are ready for V2 volumes.`;
};
window.storageClassHint = () => {
  const on = $("#sc_migratable").checked, hint = $("#sc_hint");
  hint.classList.toggle("bad", on);
  hint.innerHTML = on
    ? "<b>Volumes from this class cannot be mounted by containers.</b> Only pick this for VM disks that need live migration; ReadWriteMany claims built on it will never attach to a pod."
    : "Leave migratable off for container storage: a migratable volume gets a second controller so a VM can move between hosts, and Longhorn refuses to mount that kind into a pod — which is what breaks ReadWriteMany.";
};
window.storageClassSave = async button => {
  const name = $("#sc_name").value.trim();
  if (!/^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$/.test(name)) return toast("lowercase letters, numbers and dashes only", "bad");
  if (button) { button.disabled = true; button.textContent = "Creating…"; }
  try {
    const result = await api("/api/storage/classes", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name, replicas: +$("#sc_reps").value, reclaim_policy: $("#sc_reclaim").value, copies: $("#sc_copies").value,
        expandable: $("#sc_expand").checked, migratable: $("#sc_migratable").checked,
        engine: $("#sc_engine").value, default: $("#sc_default").checked,
        disk_tags: $$("#sc_disktags input:checked").map(b => b.value),
        node_tags: $$("#sc_nodetags input:checked").map(b => b.value) }) });
    toast(result.message || `storage class "${name}" created`, "ok"); closeModal(); resetPaint(); viewStorage();
  } catch (e) { if (button) { button.disabled = false; button.textContent = "Create class"; } toast(e.message, "bad"); }
};
window.storageClassDefault = async name => {
  try {
    const result = await api("/api/storage/classes/default", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name }) });
    toast(result.message || `${name} is now the default`, "ok"); resetPaint(); viewStorage();
  } catch (e) { toast(e.message, "bad"); }
};
window.storageClassDelete = async name => {
  if (!(await ask(`Delete storage class "${name}"?

Volumes already built from it keep working and keep their data. New volumes can no longer use it.`))) return;
  try {
    const result = await api("/api/storage/classes/delete", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name }) });
    toast(result.message || `storage class "${name}" deleted`, "ok"); resetPaint(); viewStorage();
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
    note.innerHTML = `<b>${esc(name)} cannot back a shared volume.</b> It creates live-migratable
      volumes for VM disks, which Longhorn will not mount into a pod.
      ${(classes.shared || []).length ? `Use ${(classes.shared || []).map(esc).join(" or ")} instead.` : ""}`;
  }
};

const FILEVIEW = { namespace: "", pvc: "", path: "", file: "", dirty: false, editor: null };

function disposeEditor() {
  if (FILEVIEW.editor) {
    FILEVIEW.editor.getModel()?.dispose();
    FILEVIEW.editor.dispose();
    FILEVIEW.editor = null;
  }
}

const fileSize = bytes => bytes >= 1024 * 1024 ? `${(bytes / 1024 / 1024).toFixed(1)} MB`
  : bytes >= 1024 ? `${Math.round(bytes / 1024)} KB` : `${bytes} B`;

/* Monaco is several megabytes, so it is fetched the first time a file is
   opened rather than on page load, and never at all for the rest of the app.
   If it cannot load - offline, blocked, a stripped image - the editor falls
   back to the textarea that was here before, which still saves correctly. */
const MONACO_BASE = "/vendor/monaco";
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
    style.href = `${MONACO_BASE}/vs/editor/editor.main.css?v=${HOMESTEAD_VERSION}`;
    document.head.appendChild(style);
    const script = document.createElement("script");
    script.src = `${MONACO_BASE}/vs/loader.js?v=${HOMESTEAD_VERSION}`;
    script.onerror = () => failed("the editor could not be loaded");
    script.onload = () => {
      try {
        window.require.config({ paths: { vs: `${MONACO_BASE}/vs` } });
        // Same origin, so the worker needs no blob shim.
        window.MonacoEnvironment = { getWorkerUrl: () => `${MONACO_BASE}/vs/base/worker/workerMain.js` };
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

async function mountEditor(host, content, filename) {
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
  editor.onDidChangeModelContent(() => fileTouched());
  editor.addCommand(monaco.KeyMod.CtrlCmd | monaco.KeyCode.KeyS, () => fileSave());
  return editor;
}

window.volumeFiles = async (namespace, pvc, attached) => {
  Object.assign(FILEVIEW, { namespace, pvc, path: "", file: "", dirty: false });
  modal(`Files · ${pvc}`, `<div class="empty"><span class="spin2"></span>starting a file browser on ${esc(pvc)}</div>`, true);
  if (attached && !(await ask(`${pvc} is attached to a running workload.\n\nA ReadWriteOnce volume can only mount in one place, so the browser will not start until the workload is stopped. Continue anyway?`))) {
    return closeModal();
  }
  fileBrowse("");
};

window.fileBrowse = async (path) => {
  const { namespace, pvc } = FILEVIEW;
  if (FILEVIEW.dirty && !(await ask("Discard unsaved changes?"))) return;
  disposeEditor();
  try {
    const listing = await api(`/api/files/list?namespace=${encodeURIComponent(namespace)}&pvc=${encodeURIComponent(pvc)}&path=${encodeURIComponent(path || "")}`);
    Object.assign(FILEVIEW, { path: listing.path || "", file: "", dirty: false });
    $("#mbody").innerHTML = fileBrowserMarkup(listing);
    if (window.applyRole) window.applyRole();
  } catch (e) {
    $("#mbody").innerHTML = `<div class="note dependency-danger"><b>The file browser could not start.</b> ${esc(e.message)}</div>
      <div class="row" style="margin-top:14px"><button class="btn" onclick="fileBrowse('')">Try again</button>
      <button class="btn" onclick="closeFiles()">Close</button></div>`;
  }
};

function fileCrumbs(path) {
  const parts = String(path || "").split("/").filter(Boolean);
  const crumbs = [`<button class="linkish" onclick="fileBrowse('')">${esc(FILEVIEW.pvc)}</button>`];
  parts.forEach((part, index) => {
    const upto = parts.slice(0, index + 1).join("/");
    crumbs.push(`<span class="dim">/</span><button class="linkish" onclick="fileBrowse(${jsq(upto)})">${esc(part)}</button>`);
  });
  return crumbs.join("");
}

function fileBrowserMarkup(listing) {
  const parent = String(listing.path || "").split("/").slice(0, -1).join("/");
  return `<div class="filecrumbs">${fileCrumbs(listing.path)}</div>
    <div class="filelist">
      ${listing.path ? `<button class="filerow" onclick="fileBrowse(${jsq(parent)})"><span class="fileicon">↩</span><span>..</span><span class="dim xs">up one level</span></button>` : ""}
      ${listing.entries.map(entry => {
        const full = (listing.path ? listing.path + "/" : "") + entry.name;
        return entry.kind === "dir"
          ? `<button class="filerow" onclick="fileBrowse(${jsq(full)})"><span class="fileicon">▸</span><span>${esc(entry.name)}</span><span class="dim xs">folder</span></button>`
          : `<button class="filerow" ${entry.editable ? `onclick="fileOpen(${jsq(full)})"` : "disabled"}><span class="fileicon">·</span><span>${esc(entry.name)}</span><span class="dim xs">${fileSize(entry.size)}${entry.editable ? "" : " · too large to edit"}</span></button>`;
      }).join("") || '<div class="empty small">this folder is empty</div>'}
    </div>
    ${listing.truncated ? '<div class="dim xs">Only the first 500 entries are listed.</div>' : ""}
    <div class="row" style="margin-top:16px"><button class="btn" onclick="closeFiles()">Close browser</button></div>
    <div class="note" style="margin-top:12px">The browser runs as a short-lived pod that mounts this volume.
      It stops on its own after 30 minutes, or when you close it.</div>`;
}

window.fileOpen = async (path) => {
  const { namespace, pvc } = FILEVIEW;
  try {
    const file = await api(`/api/files/read?namespace=${encodeURIComponent(namespace)}&pvc=${encodeURIComponent(pvc)}&path=${encodeURIComponent(path)}`);
    FILEVIEW.file = file.path;
    FILEVIEW.dirty = false;
    $("#mbody").innerHTML = `<div class="filecrumbs">${fileCrumbs(FILEVIEW.path)}<span class="dim">/</span><b>${esc(file.path.split("/").pop())}</b></div>
      <div class="between fileeditbar"><span class="dim xs">${fileSize(file.size)} · saving keeps the previous contents as <span class="mono">${esc(file.path.split("/").pop())}.homestead-bak</span></span>
        <span class="dim xs" id="file_state"></span></div>
      <div id="file_editor" class="fileeditor"></div>
      <div class="row" style="margin-top:14px">
        <button class="btn pri" id="file_save" data-need="admin" onclick="fileSave()">Save</button>
        <button class="btn" onclick="fileBrowse(${jsq(FILEVIEW.path)})">Back</button>
        <button class="btn" onclick="closeFiles()">Close browser</button></div>`;
    FILEVIEW.editor = null;
    try {
      FILEVIEW.editor = await mountEditor($("#file_editor"), file.content, file.path);
    } catch (e) {
      // Still perfectly editable, just without highlighting.
      const host = $("#file_editor");
      if (host) {
        host.innerHTML = `<textarea id="file_body" class="mono fileeditor-plain" spellcheck="false"
          oninput="fileTouched()"></textarea>`;
        $("#file_body").value = file.content;
        $("#file_body").addEventListener("keydown", event => {
          if (event.key !== "Tab") return;
          event.preventDefault();
          const input = $("#file_body");
          input.setRangeText("  ", input.selectionStart, input.selectionEnd, "end");
          fileTouched();
        });
        const state = $("#file_state");
        if (state) state.textContent = "plain editor";
      }
    }
    if (window.applyRole) window.applyRole();
  } catch (e) { toast(e.message, "bad"); }
};

window.fileTouched = () => {
  FILEVIEW.dirty = true;
  window.__modalGuard = () => FILEVIEW.dirty
    ? `${FILEVIEW.file} has unsaved changes.

Close the editor and lose them?` : "";
  const state = $("#file_state");
  if (state) state.textContent = "unsaved changes";
};

window.fileSave = async (ignoreSyntax = false) => {
  const { namespace, pvc, file } = FILEVIEW;
  const button = $("#file_save");
  const content = FILEVIEW.editor ? FILEVIEW.editor.getValue() : ($("#file_body")?.value ?? "");
  if (button) { button.disabled = true; button.textContent = "Saving…"; }
  try {
    const result = await api("/api/files/write", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ namespace, pvc, path: file, content, ignore_syntax: ignoreSyntax }) });
    FILEVIEW.dirty = false;
    const state = $("#file_state");
    if (state) state.textContent = `saved ${new Date().toLocaleTimeString()}`;
    toast(result.message || "saved", "ok");
  } catch (e) {
    // A syntax complaint is a warning, not a refusal: it is your file.
    if (/^(JSON is invalid|YAML cannot)/.test(e.message) && (await ask(`${e.message}\n\nSave it anyway?`))) {
      return fileSave(true);
    }
    toast(e.message, "bad");
  } finally {
    if (button) { button.disabled = false; button.textContent = "Save"; }
  }
};

window.closeFiles = async () => {
  const { namespace, pvc, dirty } = FILEVIEW;
  if (dirty && !(await ask("Discard unsaved changes?"))) return;
  disposeEditor();
  closeModal();
  try {
    await api("/api/files/close", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ namespace, pvc }) });
  } catch (e) { /* the pod expires on its own */ }
};

window.volumeChown = async (namespace, name) => {
  modal(`Ownership · ${name}`, '<div class="empty"><span class="spin2"></span>reading what uses this volume</div>');
  // The workload that mounts it already says who it runs as; asking the person
  // for a uid they have no way to know is not a question worth putting to them.
  const hint = await api(`/api/volumes/ownership?namespace=${encodeURIComponent(namespace)}&name=${encodeURIComponent(name)}`)
    .catch(() => ({ known: false, source: "" }));
  $("#mbody").innerHTML = `
    <p class="muted small">An import keeps the ownership the files had on the source, and a volume created
      from the App Store is written by the container itself, so both are already correct. This is for a
      volume that is not: appdata imported before Homestead preserved ownership, or a container that runs
      as a different user here than it did on the source. Its log fills with permission errors when so.</p>
    ${hint.known
      ? `<div class="note"><b>${esc(hint.workload)} runs as ${hint.uid ?? hint.gid}${hint.gid != null && hint.gid !== hint.uid ? `:${hint.gid}` : ""}.</b>
          Taken from ${esc(hint.source)}${hint.image ? ` · <span class="mono">${esc(hint.image)}</span>` : ""}.</div>`
      : `<div class="note dependency-danger"><b>Nothing here declares a user.</b> ${esc(hint.source || "")}</div>`}
    <div class="f2" style="margin-top:14px">
      <div class="f"><label>User (UID)</label><input type="number" id="vc_uid" min="0" max="65535" value="${hint.uid ?? ""}" placeholder="1883"></div>
      <div class="f"><label>Group (GID)</label><input type="number" id="vc_gid" min="0" max="65535" value="${hint.gid ?? ""}" placeholder="same as UID"></div></div>
    <div class="note">Homestead runs a short job that mounts the volume and changes ownership. The workload
      should be stopped first: a ReadWriteOnce volume cannot attach to the job while its pod holds it.</div>
    <div class="row" style="margin-top:16px">
      <button class="btn pri" data-need="admin" onclick="volumeChownNow(${jsq(namespace)},${jsq(name)},this)">Set ownership</button>
      <button class="btn" onclick="closeModal()">Cancel</button></div>`;
  if (window.applyRole) window.applyRole();
};
window.volumeChownNow = async (namespace, name, button) => {
  const uid = $("#vc_uid").value.trim();
  if (!uid) return toast("a user id is required", "bad");
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

/* ---------------- shares ---------------- */
async function viewShares() {
  let sh;
  try { sh = await api("/api/shares"); }
  catch (e) {
    paint(`<div class="phead"><div><h2>Network shares</h2></div></div>
      <div class="note bad">Could not load shares: ${esc(e.message)} <button class="btn sm" onclick="viewShares()">Retry</button></div>`);
    return;
  }
  STATE.data.shares = sh;
  const smb = await api("/api/shares/server").catch(error => ({ error: error.message }));
  const nfs = await api("/api/shares/nfs/server").catch(error => ({ error: error.message }));
  STATE.data.samba = smb;
  STATE.data.nfs = nfs;
  STATE.data.sambaInstalled = !!smb.installed;
  const ip = smb.address || "address pending";
  paint(`<div class="phead"><div><h2>Network shares</h2>
    <p>SMB shares and optional NFSv4 exports backed by Longhorn volumes</p></div>
    <div class="row">${moreMenu([{ label: "SMB users", icon: "list", run: "smbUsers()", need: "admin" }])}<button class="btn pri" data-need="admin" onclick="newShare()">＋ New share</button></div></div>
  <div class="card" style="margin-bottom:14px"><div class="between"><div><div class="ctitle">SMB server · ${esc(smb.name || "homestead-smb")}</div>
    <div class="dim small">${smb.error ? `Status unavailable: ${esc(smb.error)}` : !smb.installed ? "Not installed · your first share can install it" :
      `${smb.enabled ? `${smb.ready || 0}/${smb.desired || 1} ready` : "Stopped"}${smb.address ? ` · \\\\${esc(smb.address)}` : " · waiting for an address"} · ${smb.served_shares?.length ?? 0}/${sh.length} share mappings${smb.in_sync ? "" : " · out of sync"}`}</div></div>
    ${can("admin") && !smb.error ? `<div class="row">${smb.installed && (!smb.in_sync || Object.keys(smb.recovery_failures || {}).length) ? '<button class="btn sm" onclick="repairSamba(this)">Repair / retry recovery</button>' : ""}<button class="btn sm" onclick="settingsTab('cluster');go('settings')">Manage add-on</button></div>` : ""}</div>
    ${smb.partial ? `<div class="note warn"><b>Partial service · ${smb.offline_shares.length} share(s) offline</b><br>Unavailable storage is temporarily excluded so other shares can start. No volumes, credentials or share definitions were deleted.<ul>${smb.offline_shares.map(s => `<li><b>${esc(s.name)}</b> · ${esc(s.pvc)}: ${esc(s.reason)}</li>`).join("")}</ul>After storage is confirmed recoverable for two minutes, Homestead re-adds the shares. This restarts SMB and briefly interrupts connections.</div>` : ""}
    ${smb.recovery_warning ? `<div class="note warn">${esc(smb.recovery_warning)}</div>` : ""}
    ${Object.keys(smb.recovery_failures || {}).length ? `<div class="note bad">Automatic restore paused: ${Object.entries(smb.recovery_failures).map(([pvc, reason]) => `${esc(pvc)}: ${esc(reason)}`).join(" ")} The working subset was restored; use Repair / retry recovery after resolving the problem.</div>` : ""}
    ${Object.keys(smb.recovery_pending || {}).length ? `<div class="note warn">Checking storage stability: ${Object.entries(smb.recovery_pending).map(([pvc, p]) => `${esc(pvc)} (${p.action === "restore" ? "waiting to restore" : "waiting to exclude unavailable storage"})`).join(", ")}.</div>` : ""}
    ${UI.more("How the SMB server is managed", `<p>Homestead manages this container, its image and its volume mounts from the share list. Enable, stop or remove the server in Settings → Hardware and storage → Add-ons; volumes are kept. Confirmed unavailable volumes are temporarily excluded after one minute; recovered volumes return after a two-minute stability check. Either change restarts SMB. Storage recovery and a working cluster control plane are still required.</p>`)}</div>
  <div class="card" style="margin-bottom:14px"><div class="between"><div><div class="ctitle">NFSv4 server · ${esc(nfs.name || "homestead-nfs")}</div>
    <div class="dim small">${nfs.error ? `Status unavailable: ${esc(nfs.error)}` : !nfs.installed ? "Not installed" :
      `${nfs.enabled ? `${nfs.ready || 0}/${nfs.desired || 1} ready` : "Stopped"}${nfs.address ? ` · ${esc(nfs.address)}:/<share>` : " · waiting for an address"}`} · ${(nfs.exports || []).length} configured exports</div></div>
    <button class="btn sm" onclick="settingsTab('cluster');go('settings')">Manage add-on</button></div>
    <div class="dim xs" style="margin-top:9px">NFS is a separate opt-in container. Only selected RWX shares are exported to their allowed client networks; SMB and all PVCs remain independent.</div>${nfsRecovery(nfs)}</div>
  <div class="card flat pad0"><div class="tblwrap sharetable"><table data-sort="shares" class="tbl stack">
    <thead><tr><th>Share</th><th>Storage</th><th>Size</th><th>Access</th><th>UNC path</th><th></th></tr></thead><tbody>
    ${sh.map(s => `<tr><td class="shareidentity"><div class="row" style="gap:9px"><div class="av n2">${esc(s.name.slice(0, 2).toUpperCase())}</div>
      <div><b>${esc(s.name)}</b>${(smb.offline_shares || []).some(row => row.name === s.name) ? ' <span class="pill crit">SMB offline · storage unavailable</span>' : ""}<div class="dim xs">${esc(s.created || "")}</div></div></div></td>
      <td class="small mono" data-label="Storage"><span class="sharevol">${esc(s.pvc || "—")}${s.sub_path ? `<span class="dim">/${esc(s.sub_path)}</span>` : ""}</span>
        ${s.owned === false ? '<span class="tag">shared volume</span>' : ""}</td>
      <td class="mono" data-label="Size">${s.size_gb ? s.size_gb + " GB" : "—"}</td>
      <td data-label="Access"><span>${s.public ? `<span class="pill med" data-tip="No sign-in. Windows 11 refuses guest shares until two of its own settings are changed - see Allow guest access when making or editing a share">guest</span>` : `<span class="pill low">${esc(s.user)}</span>`}
        ${s.read_only ? '<span class="tag">read only</span>' : '<span class="tag">read/write</span>'}
        ${s.nfs_clients ? `<span class="pill slim info" data-tip="NFS ${s.nfs_read_only === false ? "read/write" : "read only"} for ${esc(s.nfs_clients)}">NFS</span>` : ""}</span></td>
      <td class="small muted mono" data-label="UNC path">${smb.address ? `\\\\${esc(ip)}\\${esc(s.name)}` : "Waiting for SMB address"}</td>
      <td class="shareactions"><div class="row"><button class="btn sm" data-need="admin" title="Configure this share's NFSv4 export and allowed clients" onclick="nfsExport(${jsq(s.name)})">NFS</button>
        <button class="btn sm" data-need="admin" title="Grow this share or change its access policy" onclick="editShare(${jsq(s.name)})">${icon("edit")}Edit</button>
        <button class="btn sm danger" data-need="admin" onclick="rmShare(${jsq(s.name)})">${icon("trash")}Remove</button></div></td></tr>`).join("")
      || `<tr><td colspan=6 class="empty">no shares yet — create one with ＋ New share</td></tr>`}</tbody></table></div></div>`);
}
window.nfsExport = name => {
  const share = (STATE.data.shares || []).find(row => row.name === name);
  if (!share) return toast("share details are no longer available; refresh and try again", "bad");
  const rwx = (share.access_modes || []).includes("ReadWriteMany");
  modal(`NFS export · ${esc(name)}`, `<p>Export this share from the optional NFSv4 container to a specific client address or network. Leave the field blank to remove its NFS export. SMB access is unchanged.</p>
    ${rwx ? "" : `<div class="note warn">This share may not be RWX. NFS requires a Bound ReadWriteMany volume so its separate container can mount the data; the server checks this before saving.</div>`}
    <div class="f"><label>Allowed IPv4 client or CIDR ${tip("For example, 192.0.2.42 or 192.0.2.0/24. An everyone-accessible export is not allowed.")}</label>
      <input id="nfs_clients" value="${esc(share.nfs_clients || "")}" placeholder="192.0.2.0/24" autocomplete="off"></div>
    <label class="switch"><input type="checkbox" id="nfs_ro" ${share.nfs_read_only !== false ? "checked" : ""}> Read only</label>
    <div class="dim xs" style="margin-top:8px">NFSv4 clients mount ${esc(STATE.data.nfs?.address || "<server-ip>")}:/${esc(name)} on TCP port 2049. The server and its VIP are enabled in Settings → Hardware and storage → Add-ons. Longhorn RWX re-export adds an extra NFS layer.</div>
    <div class="modalactions"><button class="btn pri" onclick="nfsExportSave(${jsq(name)},this)">Save export</button><button class="btn" onclick="closeModal()">Cancel</button></div>`);
};
window.nfsExportSave = async (name, button) => {
  const clients = $("#nfs_clients")?.value.trim() || "";
  if (button) button.disabled = true;
  try {
    const result = await api("/api/shares/nfs", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name, clients, read_only: !!$("#nfs_ro")?.checked }) });
    closeModal(); toast(result.detail, "ok"); viewShares();
  } catch (e) { toast(e.message, "bad"); if (button) button.disabled = false; }
};
window.repairSamba = async button => {
  if (button) { button.disabled = true; button.textContent = "Repairing…"; }
  try {
    const result = await api("/api/shares/repair", { method: "POST", headers: { "Content-Type": "application/json" },
      body: "{}" });
    toast(result.detail, "ok"); viewShares();
  } catch (e) { toast(e.message, "bad"); if (button) { button.disabled = false; button.textContent = "Repair mapping"; } }
};
window.smbUsers = async () => {
  modal("SMB users", '<div class="empty">Loading users…</div>', true, "smb-access");
  try {
    const users = await api("/api/shares/users");
    STATE.data.smbUsers = users;
    $("#mbody").innerHTML = UI.lead("One password per SMB user, shared across their private shares.")
      + UI.table([{ label: "User" }, { label: "Shares" }, { label: "Actions" }], users.map(user => [esc(user.user),
        esc(user.shares.join(", ") || "No shares"),
        UI.button("Change password", `smbUserEdit(${jsArg(user.user)})`, { small: true })
        + (!user.shares.length ? UI.button("Remove", `smbUserRemove(${jsArg(user.user)})`, { small: true }) : "")]))
      + UI.more("About SMB users", "These accounts are separate from Homestead sign-ins. Saved passwords are never displayed. Remove a user only after reassigning their private shares; unused users remain available for new shares.")
      + UI.actions(UI.cancel("Close") + UI.button("Add user", "smbUserEdit()", { kind: "pri" }));
  } catch (error) {
    $("#mbody").innerHTML = UI.callout("bad", "Could not load SMB users", esc(error.message))
      + UI.actions(UI.cancel("Close") + UI.button("Retry", "smbUsers()", { kind: "pri" }));
  }
};
window.smbUserEdit = (name = "") => {
  const user = (STATE.data.smbUsers || []).find(row => row.user === name);
  modal(name ? `Change SMB password · ${esc(name)}` : "Add SMB user",
    UI.lead(name ? "Set one password for every share assigned to this user." : "Save an SMB account to select when creating private shares.")
    + (user?.shares.length ? UI.callout("warn", "SMB connections will restart", `This changes access to ${esc(user.shares.join(", "))}. Reconnect clients with the new password.`) : "")
    + UI.fields(UI.field("Username", `<input id="su_user" value="${esc(name)}" ${name ? "readonly" : ""} autocomplete="username" maxlength="32">`)
      + UI.field("New password", '<input id="su_password" type="password" autocomplete="new-password">'))
    + UI.actions(UI.button("Back", "smbUsers()") + UI.button(name ? "Change password" : "Add user", `smbUserSave(${jsArg(name ? "password" : "create")},this)`, { kind: "pri" })), false, "smb-access");
};
window.smbUserSave = async (action, button) => {
  const user = $("#su_user").value.trim(), password = $("#su_password").value;
  if (!/^[A-Za-z0-9_.-]{1,32}$/.test(user)) return toast("Enter a valid SMB username", "bad");
  if (!password) return toast("A password is required", "bad");
  button.disabled = true;
  try {
    const result = await api("/api/shares/users", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ user, password, action }) });
    toast(result.message, "ok"); if (window.noteOperation) noteOperation(result.operation);
    await smbUsers();
  } catch (error) { button.disabled = false; toast(error.message, "bad"); }
};
window.smbUserRemove = name => {
  modal(`Remove SMB user · ${esc(name)}`, UI.lead("Remove this unused account and its saved password. Shares and their data are kept.")
    + UI.actions(UI.button("Back", "smbUsers()") + UI.button("Remove user", `smbUserDelete(${jsArg(name)},this)`, { kind: "danger" })), false, "smb-access");
};
window.smbUserDelete = async (user, button) => {
  button.disabled = true;
  try {
    const result = await api("/api/shares/users/delete", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ user }) });
    toast(result.message, "ok"); await smbUsers();
  } catch (error) { button.disabled = false; toast(error.message, "bad"); }
};
window.newShare = async () => {
  modal("New share", `<div class="empty"><span class="spin2"></span>loading volumes</div>`, true, "smb-access");
  let options, users;
  try {
    [options, users] = await Promise.all([api("/api/shares/options"), api("/api/shares/users")]);
  } catch (error) {
    $("#mbody").innerHTML = UI.callout("bad", "Could not load share options", esc(error.message))
      + UI.actions(UI.cancel() + UI.button("Retry", "newShare()", { kind: "pri" }));
    return;
  }
  STATE.data.smbUsers = users;
  const existing = users.filter(user => user.has_password);
  $("#mbody").innerHTML = `
    <div class="f"><label>Share name ${tip("Windows sees this name after the server address, and Homestead mounts it at /shares/<name> inside Samba.")}</label>
      <input type="text" id="sh_name" placeholder="media" autocomplete="off"></div>
    <div class="sec">Storage</div>
    <p class="ui-help">Use a new Longhorn volume or a folder in an existing volume.</p>
    <div id="sh_storage"></div>
    ${UI.more("Sharing an existing volume", "A volume can also be used by a container. ReadWriteOnce storage attaches on one node, so Samba and that workload must run on the same host.")}
    ${UI.section("SMB access", `<div class="f"><label>SMB user</label><select id="sh_identity" onchange="shareAccountHint()">
      ${existing.map(user => `<option value="${esc(user.user)}">${esc(user.user)} · existing user</option>`).join("")}
      <option value="">Add a new user…</option></select><span class="ui-help">Existing users reuse their saved password. Manage passwords in SMB users.</span></div>
      <div class="f2" id="sh_new_account"><div class="f"><label>New username</label><input type="text" id="sh_user" value="${existing.length ? "" : "lab"}" autocomplete="username"></div>
      <div class="f"><label>Password</label><input type="password" id="sh_pass" autocomplete="new-password"></div></div>`)}
    ${options.samba_installed ? "" : `<div class="sec">Samba's address</div>
      <p class="ui-help">Samba is not installed yet; this share installs it. Choose the address Windows will find it at (\\\\address\\share).</p>
      <div class="f" id="sh_smb"><span class="dim xs"><span class="spin2"></span></span></div>`}
    <label class="switch"><input type="checkbox" id="sh_pub" onchange="shareAccountHint()"> Allow guest access</label>
    <div id="sh_guest_note" class="hidden">${shareGuestWindowsNote()}</div>
    <label class="switch"><input type="checkbox" id="sh_ro"> Read only</label>
    <p class="ui-help">Creating a share restarts Samba, so open SMB sessions drop briefly.</p>
    ${UI.actions(UI.cancel() + UI.button("Create share", "mkShare(this)", { kind: "pri", id: "sh_go", attrs: 'data-need="admin"' }))}`;
  const host = createVolumePicker($("#sh_storage"), {
    pvcs: () => options.pvcs || [],
    storageClasses: () => options.storage_classes || [],
    sharedStorageClasses: () => options.shared_storage_classes || options.storage_classes || [],
    classFacts: () => options.storage_class_facts || {},
    targetNode: () => options.node || "",
    kinds: ["new-rwo", "new-rwx", "existing"],
    pathLabel: "Folder inside the volume",
    pathPlaceholder: "whole volume",
    newSourceHelp: "Creates a Longhorn claim for this share. Leave the name blank to use share-<share name>.",
    removable: false,
    readOnlyToggle: false,
    // The share's folder is the path field above; a second one would be ignored.
    folders: false,
  });
  renderVolumeRows(host, [{ kind: "new-rwo", path: "", source: "", size_gb: 10,
    label: "Share storage" }]);
  if (!options.samba_installed) vipChoices().then(choices => {
    const own = (choices.own || []).find(v => v.free);
    const smb = $("#sh_smb");
    if (smb) smb.innerHTML = vipPicker("sh_smb", own ? own.ip : (choices.free[0] || ""), choices);
  });
  shareAccountHint();
};
window.shareAccountHint = () => {
  const existing = !!$("#sh_identity")?.value, guest = !!$("#sh_pub")?.checked;
  $("#sh_guest_note")?.classList.toggle("hidden", !guest);
  $("#sh_new_account")?.classList.toggle("hidden", existing || guest);
  if ($("#sh_identity")) $("#sh_identity").disabled = guest;
  if ($("#sh_pass")) { $("#sh_pass").disabled = existing || guest; if (existing || guest) $("#sh_pass").value = ""; }
};
window.mkShare = async button => {
  const name = $("#sh_name").value.trim();
  if (!/^[a-z0-9-]{2,30}$/.test(name)) return toast("lowercase letters, numbers and dashes only", "bad");
  const selected = $("#sh_identity").value;
  const account = selected || $("#sh_user").value.trim();
  const known = (STATE.data.smbUsers || []).some(row => row.user === selected && row.has_password);
  if (!$("#sh_pub").checked && !$("#sh_pass").value && !known) {
    return toast("a password is required for a private share", "bad");
  }
  const storage = readVolumeRows($("#sh_storage"))[0] || {};
  const folder = String(storage.path || "").trim().replace(/^\/+|\/+$/g, "");
  if (folder && !/^[A-Za-z0-9._-]+(?:\/[A-Za-z0-9._-]+)*$/.test(folder)) {
    return toast("the folder must be a path inside the volume, such as media/photos", "bad");
  }
  const reusing = storage.kind === "existing";
  if (reusing && !storage.source) return toast("choose the volume this share should use", "bad");
  const body = { name, user: account, password: selected || $("#sh_pub").checked ? "" : $("#sh_pass").value,
    account_mode: selected ? "existing" : "new",
    public: $("#sh_pub").checked, read_only: $("#sh_ro").checked, sub_path: folder,
    samba_ip: ($("#sh_smb_lb_ip")?.value || "").trim() };
  if ($("#sh_smb") && !body.samba_ip) return toast("choose the address Samba should answer on - add VIPs under Networking if the list is empty", "bad");
  if (reusing) body.pvc = storage.source;
  // A new volume's name is the one to create, not one to look up.
  else Object.assign(body, { new_name: storage.source || "", size_gb: storage.size_gb,
    storage_class: storage.storage_class, access_mode: storage.access_mode });
  if (button) { button.disabled = true; button.textContent = "Creating…"; }
  try {
    const result = await api("/api/shares", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body) });
    (result.warnings || []).forEach(warning => toast(warning, "warn"));
    toast(`share "${name}" created`, "ok"); closeModal(); resetPaint(); viewShares();
  } catch (e) { if (button) { button.disabled = false; button.textContent = "Create share"; } toast(e.message, "bad"); }
};
const shareAccountSiblings = share => (STATE.data.shares || [])
  .filter(row => !row.public && row.user === share.user && row.name !== share.name)
  .map(row => row.name);

window.shareAccessToggle = () => {
  const guest = $("#she_access")?.value === "guest";
  $("#she_private")?.classList.toggle("hidden", guest);
  $("#she_guest_note")?.classList.toggle("hidden", !guest);
};
window.editShare = name => {
  const s = (STATE.data.shares || []).find(row => row.name === name);
  if (!s) return toast("share details are no longer available; refresh and try again", "bad");
  modal(`Edit share · ${s.name}`, `
    <div class="callout"><b>\\\\${esc(STATE.data.samba?.address || "address pending")}\\${esc(s.name)}</b><br>
      ${s.owned === false ? `Files come from <span class="mono">${esc(s.pvc || "")}${s.sub_path ? "/" + esc(s.sub_path) : ""}</span>, a volume this share only mounts.` : "The claim can grow but cannot shrink."}
      Size-only changes keep Samba running; access changes briefly disconnect open SMB sessions.</div>
    <div class="f2" style="margin-top:14px"><div class="f"><label>Requested size (GB) ${tip("Longhorn volumes can grow online. Kubernetes and Longhorn do not support shrinking a populated claim.")}</label>
      <input type="number" id="she_size" min="${esc(s.size_gb || 1)}" value="${esc(s.size_gb || 1)}" ${s.owned === false ? "disabled" : ""}>
      ${s.owned === false ? `<span class="dim xs">Sized with <span class="mono">${esc(s.pvc || "its volume")}</span> in Volumes, because this share borrows it.</span>` : ""}</div>
      <div class="f"><label>Access</label><select id="she_access" onchange="shareAccessToggle()">
        <option value="private" ${s.public ? "" : "selected"}>Private · username and password</option>
        <option value="guest" ${s.public ? "selected" : ""}>Guest · no sign-in</option></select></div></div>
    <div id="she_guest_note" class="${s.public ? "" : "hidden"}">${shareGuestWindowsNote()}</div>
    <div id="she_private" class="${s.public ? "hidden" : ""}"><div class="f"><label>Username</label>
      <input type="text" id="she_user" value="${esc(s.user || "lab")}" autocomplete="username"></div>
      <div class="f"><label>New password ${tip("Leave blank to keep the existing password. Passwords are stored in a Kubernetes Secret and are never returned to the browser.")}</label>
        <input type="password" id="she_pass" autocomplete="new-password" placeholder="${s.has_password ? "Leave blank to keep current password" : "Required for private access"}">
        ${shareAccountSiblings(s).length ? `<span class="dim xs">${esc(s.user)} is also used by ${esc(shareAccountSiblings(s).join(", "))}. Samba keeps one password per account, so a new one changes those too.</span>` : ""}</div></div>
    <label class="switch"><input type="checkbox" id="she_ro" ${s.read_only ? "checked" : ""}> Read only · clients can browse and download but cannot change files</label>
    <div class="row" style="margin-top:18px"><button class="btn pri" data-need="admin" onclick="saveShareEdit(${jsq(s.name)},this)">${icon("edit")}Save changes</button>
      <button class="btn" onclick="closeModal()">Cancel</button></div>`);
};
window.saveShareEdit = async (name, button) => {
  const original = (STATE.data.shares || []).find(row => row.name === name);
  if (!original) return toast("share details are no longer available", "bad");
  const size = +$("#she_size").value || +(original.size_gb || 1);
  const publicAccess = $("#she_access").value === "guest";
  const password = $("#she_pass")?.value || "";
  if (original.owned !== false && (!Number.isInteger(size) || size < +(original.size_gb || 1))) {
    return toast(`size must be at least ${original.size_gb || 1} GB`, "bad");
  }
  if (!publicAccess && !original.has_password && !password) return toast("set a password before enabling private access", "bad");
  if (button) { button.disabled = true; button.textContent = "Saving…"; }
  try {
    const result = await api("/api/shares/edit", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name, size_gb: size, user: $("#she_user")?.value.trim() || original.user || "lab",
        password, public: publicAccess, read_only: $("#she_ro").checked }) });
    toast(result.message || `share "${name}" updated`, "ok"); closeModal(); resetPaint(); viewShares();
  } catch (e) { if (button) { button.disabled = false; button.innerHTML = `${icon("edit")}Save changes`; } toast(e.message, "bad"); }
};
window.rmShare = async name => {
  if (!(await ask(`Remove share "${name}" from SMB?\n\nThe volume and data are kept. Its mount is removed after the SMB restart unless another configured share uses the same PVC. Other workloads may also keep the volume attached.`))) return;
  try {
    const result = await api("/api/shares/delete", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name }) });
    toast(result.message || `share "${name}" removed`, "ok");
    if (window.noteOperation) noteOperation(result.operation);
    resetPaint(); viewShares();
  } catch (e) { toast(e.message, "bad"); }
};

/* ---------------- events ---------------- */
/* Events: the cluster's, and - for admins - Homestead's sign-ins. One filter
   bar for each; the filters narrow the table in place, so typing in the
   text filter keeps its focus. */
const SIGNIN_WORDS = {
  signin: ["Signed in", "ok"], "signin-failed": ["Sign-in failed", "med"], "signin-blocked": ["Blocked: too many tries", "crit"],
  signout: ["Signed out", "low"], "signout-everywhere": ["Signed out everywhere", "low"], password: ["Password changed", "low"],
  "password-failed": ["Password change refused", "med"], setup: ["Set Homestead up", "low"], "user-added": ["User added", "low"],
  "user-removed": ["User removed", "med"], role: ["Role changed", "low"],
};
const SIGNIN_GROUPS = {
  signins: x => x.event === "signin", failures: x => !x.ok,
  signouts: x => ["signout", "signout-everywhere"].includes(x.event),
  account: x => ["password", "password-failed", "setup", "user-added", "user-removed", "role"].includes(x.event),
};
const EVENT_SINCE = { "1h": 3600, "24h": 86400, "7d": 604800 };

const eventTab = () => (STATE.eventsTab === "signins" && can("admin") ? "signins" : "cluster");
const eventFilter = () => (eventTab() === "signins"
  ? (STATE.signinFilter ||= { what: "", user: "", since: "" })
  : (STATE.eventFilter ||= { type: STATE.eventWarnings ? "Warning" : "", kind: "", ns: "", since: "" }));
const eventTime = x => (eventTab() === "signins" ? x.at * 1000 : Date.parse(x.time || "") || 0);

function eventTabs(tab) {
  if (!can("admin")) return "";
  const button = (id, label) => `<button type="button" class="${tab === id ? "on" : ""}" aria-pressed="${tab === id}"
    onclick="STATE.eventsTab=${jsq(id)};viewEvents()">${label}</button>`;
  return `<div class="seg event-tabs" role="group" aria-label="Which events">${button("cluster", "Cluster")}${button("signins", "Sign-ins")}</div>`;
}

function eventSelect(key, label, options, value) {
  return `<select class="evf" aria-label="${esc(label)}" onchange="eventFilterSet(${jsq(key)}, this.value)">
    ${options.map(([v, text]) => `<option value="${esc(v)}" ${v === value ? "selected" : ""}>${esc(text)}</option>`).join("")}</select>`;
}

function eventFilterBar(rows) {
  const f = eventFilter();
  const since = eventSelect("since", "When", [["", "Any time"], ["1h", "Last hour"], ["24h", "Last day"], ["7d", "Last week"]], f.since);
  const text = `<input class="evf evf-text" type="search" placeholder="Filter…" aria-label="Filter the events" value="${esc(STATE.q)}"
    oninput="eventText(this.value)">`;
  const distinct = key => [...new Set(rows.map(x => x[key]).filter(Boolean))].sort((a, b) => a.localeCompare(b));
  const selects = eventTab() === "signins"
    ? eventSelect("what", "What", [["", "Everything"], ["signins", "Sign-ins"], ["failures", "Failures"], ["signouts", "Sign-outs"],
        ["account", "Account changes"]], f.what)
      + eventSelect("user", "User", [["", "All users"], ...distinct("user").map(u => [u, u])], f.user)
    : eventSelect("type", "Type", [["", "All types"], ["Warning", "Warnings"], ["Normal", "Normal"]], f.type)
      + eventSelect("kind", "Kind", [["", "All kinds"], ...distinct("kind").map(k => [k, k])], f.kind)
      + eventSelect("ns", "Namespace", [["", "All namespaces"], ...distinct("ns").map(n => [n, n])], f.ns);
  return `<div class="evfilters" role="group" aria-label="Filters">${text}${selects}${since}
    <button type="button" class="btn sm evf-clear" id="evClear" onclick="eventFiltersClear()">Clear</button></div>`;
}

function eventMatches(x) {
  const f = eventFilter(), q = STATE.q.toLowerCase();
  const age = f.since ? (Date.now() - eventTime(x)) / 1000 : 0;
  if (f.since && age > EVENT_SINCE[f.since]) return false;
  if (eventTab() === "signins") {
    return (!q || [x.user, x.ip, x.device, x.detail, x.via].join(" ").toLowerCase().includes(q))
      && (!f.what || SIGNIN_GROUPS[f.what](x)) && (!f.user || x.user === f.user);
  }
  return (!q || [x.obj, x.ns, x.kind, x.reason, x.msg].join(" ").toLowerCase().includes(q))
    && (!f.type || x.type === f.type) && (!f.kind || x.kind === f.kind) && (!f.ns || x.ns === f.ns);
}

function eventRowHtml(x) {
  const ago = fmtAgo(Math.max(0, (Date.now() - eventTime(x)) / 1000));
  if (eventTab() === "signins") {
    const [word, tone] = SIGNIN_WORDS[x.event] || [x.event, "low"];
    const when = new Date(x.at * 1000);
    return `<tr><td><b>${esc(x.user || "—")}</b><div class="dim xs">${x.detail ? `${esc(x.detail)}` : ""}<span class="ev-when-sm">${x.detail ? " · " : ""}${esc(ago)}</span></div></td>
      <td data-status><span class="pill slim ${tone}">${esc(word)}</span></td>
      <td class="small ev-from"><span class="mono">${esc(x.ip || "—")}</span>${x.device || x.via ? ` <span class="dim xs">${esc([x.device, x.via && `via ${x.via}`].filter(Boolean).join(" · "))}</span>` : ""}</td>
      <td class="dim xs mono nowrap" data-sm-hide title="${esc(when.toLocaleString())}">${esc(ago)}</td></tr>`;
  }
  return `<tr><td><b>${esc(x.obj)}</b><div class="dim xs">${esc(x.ns)} · ${esc(x.kind)}<span class="ev-when-sm"> · ${esc(ago)}</span></div></td>
    <td data-status><span class="pill slim ${x.type === "Warning" ? "med" : "low"}">${esc(x.reason)}</span></td>
    <td class="small muted ev-msg">${esc(x.msg)}${x.count > 1 ? ` <span class="tag">×${x.count}</span>` : ""}</td>
    <td class="dim xs mono nowrap" data-sm-hide title="${esc((x.time || "").replace("T", " ").replace("Z", ""))}">${esc(ago)}</td></tr>`;
}

/* The table and the count, from the rows already read. */
function eventsRender() {
  const all = STATE.data.eventRows || [];
  const rows = all.filter(eventMatches);
  const f = eventFilter();
  const filtered = !!STATE.q || Object.values(f).some(Boolean);
  const body = $("#evRows"), count = $("#evCount"), clear = $("#evClear");
  if (count) count.textContent = eventTab() === "signins"
    ? `${rows.length} of ${all.length} sign-ins and account changes · newest first`
    : `${rows.length} of ${all.length} recent events · newest first`;
  if (clear) clear.hidden = !filtered;
  if (body) body.innerHTML = rows.map(eventRowHtml).join("")
    || `<tr><td colspan=4 class="empty">${filtered ? "Nothing matches these filters." : eventTab() === "signins" ? "No sign-ins recorded yet." : "No events."}</td></tr>`;
}

window.eventFilterSet = (key, value) => { eventFilter()[key] = value; eventsRender(); };
window.eventText = value => {
  STATE.q = value.trim();
  const search = $("#globalSearch");
  if (search) search.value = STATE.q;
  eventsRender();
};
window.eventFiltersClear = () => {
  const f = eventFilter();
  Object.keys(f).forEach(k => { f[k] = ""; });
  STATE.eventWarnings = false;
  STATE.q = "";
  const search = $("#globalSearch");
  if (search) search.value = "";
  viewEvents();
};

async function viewEvents() {
  const tab = eventTab();
  const rows = await api(tab === "signins" ? "/api/auth/history" : "/api/events");
  STATE.data.eventRows = rows;
  const heads = tab === "signins" ? ["Who", "What", "From", "When"] : ["Object", "Reason", "Message", "When"];
  paint(`<div class="phead"><div><h2>Events</h2><p id="evCount"></p></div>${eventTabs(tab)}</div>
  ${eventFilterBar(rows)}
  <div class="card flat pad0 eventtable"><div class="tblwrap"><table data-sort="${tab === "signins" ? "signins" : "events"}" class="tbl stack compact evtable"><thead><tr>
    ${heads.map((h, i) => `<th${i === 3 ? " data-nosort" : ""}>${h}</th>`).join("")}</tr></thead><tbody id="evRows"></tbody></table></div></div>`);
  eventsRender();
}

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
    <div class="meter ${lhLevel(n.level)}" data-tip="${esc(`${n.pct}% of what Longhorn may place here (${n.size_gb} GB × ${cap.over_provisioning}%)`)}"><span style="width:${Math.min(100, n.pct)}%"></span></div>
    <div class="dim xs">${n.blocked ? `<span class="badtext">${esc(n.blocked)}</span>` : `${esc(sizeText(n.room_gb))} allocation headroom`} · ${esc(sizeText(n.used_gb))} physically used</div>
    <div class="dim xs" data-tip="Free bytes above Longhorn's minimum-free-space reserve on the best eligible disk. A rebuild must fit its block/snapshot footprint below this budget as well as its logical allocation; tags, anti-affinity and other placement rules still apply.">Physical rebuild budget: ${Number.isFinite(n.physical_room_gb) ? `${esc(n.physical_room_gb)} GiB` : "unknown"}</div></div>`).join("");
}

function lhLargest(cap) {
  const count = (cap.nodes || []).length;
  return [1, 2, 3].filter(c => c <= count).map(c => `<span class="tag ${cap.largest[c] < 10 ? "bad" : cap.largest[c] < 50 ? "warn" : ""}"
    data-tip="Logical allocation upper bound for ${c} empty cop${c === 1 ? "y" : "ies"} on different nodes. Not rebuild capacity or a placement guarantee: existing data and snapshots also need physical space.">${c} cop${c === 1 ? "y" : "ies"} · ${esc(sizeText(cap.largest[c]))}</span>`).join("");
}

/* The Volumes page's per-node card. */
function lhCapacityCard(cap, st) {
  if (!cap) return `<div class="card flat statwide"><div class="ctitle">Per-node disks</div><div class="csub">free of total</div>
      ${(st?.disks || []).map(d => `<div class="drow"><div class="dl">${esc(lhShort(d.node))}</div>
        <div class="dv mono nowrap">${esc(sizePair(d.avail_gb, d.cap_gb))}</div></div>`).join("") || '<div class="csub" style="margin-top:8px">Longhorn has not reported any node disks yet.</div>'}</div>`;
  return `<div class="card flat statwide lh-card"><div class="between"><div class="ctitle">Allocated per node</div>
      <span class="row" style="gap:10px"><a class="dim xs" data-need="admin" onclick="lhDisks()" data-tip="Every disk on every node; add one to Longhorn">Disks</a>
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
  let cap;
  try { cap = await api("/api/longhorn/capacity"); }
  catch (e) { host.innerHTML = `<div class="ctitle">Longhorn storage</div><div class="empty small">${esc(e.message)}</div>`; return; }
  STATE.data.lhcap = cap;
  const admin = can("admin"), v2 = cap.v2 || {};
  host.innerHTML = `<div class="settings-card-head"><div><div class="ctitle">Longhorn storage</div>
      <div class="csub">How much Longhorn may promise on each disk, and its V2 data engine</div></div>
      <div class="row"><button class="btn sm" onclick="lhDisks()">Disks</button>
      ${admin ? "" : '<span class="pill neutral">admin managed</span>'}</div></div>
    <div class="f2">
      <div class="f"><label>Over-provisioning ${tip("Longhorn books a volume's full size on a disk when it places a replica, however little it holds. At 100% a disk can be promised its own size; at 200%, twice that, betting volumes never fill up. If they do, the disk runs out and its replicas fail.")}</label>
        <div class="row" style="flex-wrap:nowrap"><input id="lh_over" type="number" min="100" max="1000" step="10" value="${cap.over_provisioning}" ${admin ? "" : "disabled"} oninput="lhPreview()"><span class="dim">%</span></div></div>
      <div class="f"><label>Minimal free space ${tip("A disk with less than this share physically free takes no new replica, whatever is allocated. Longhorn's default is 25%.")}</label>
        <div class="row" style="flex-wrap:nowrap"><input id="lh_min" type="number" min="0" max="100" value="${cap.minimal_available}" ${admin ? "" : "disabled"}><span class="dim">%</span></div></div></div>
    <div class="f" style="margin-top:10px"><label>Pods on a failed node ${tip("What Longhorn does with the pods of a node that stops answering. Left at do-nothing, such a pod keeps its volume attached to the dead node, so a container moving to another node cannot mount it there until the node is back. Containers > If a node fails chooses which containers move.")}</label>
      <select id="lh_nodedown" ${admin ? "" : "disabled"}>${[["do-nothing", "keep them - their volumes wait for the node"], ["delete-deployment-pod", "delete containers' pods, so their volumes can move"],
        ["delete-statefulset-pod", "delete stateful sets' pods only"], ["delete-both-statefulset-and-deployment-pod", "delete both, so every volume can move"]]
        .map(([v, l]) => `<option value="${v}" ${v === cap.node_down ? "selected" : ""}>${l}</option>`).join("")}</select></div>
    <div id="lh_nodes" class="lh-nodes">${lhNodeRows(cap)}</div>
    <div class="dim xs" style="margin-top:10px">Largest new volume</div><div class="row lh-largest" id="lh_largest">${lhLargest(cap)}</div>
    <div class="note" style="margin-top:12px">Past a node's limit, new volumes come up a copy short, replica rebuilds wait and expansions are refused;
      nothing already placed is moved. What fills a disk for real is data written — ${esc(sizeText((cap.nodes || []).reduce((s, n) => s + n.used_gb, 0)))} across all nodes now.</div>
    <div class="lh-v2">
      <label class="switch"><input type="checkbox" id="lh_v2" ${v2.enabled ? "checked" : ""} ${admin ? "" : "disabled"}> <b>V2 data engine</b> (SPDK)</label>
      <div class="dim xs">Faster volumes for a price: each host needs a disk given to Longhorn as a block device and 2 GiB of hugepages, and V2 volumes are a separate storage class.
        ${v2.harvester_setting !== null && v2.harvester_setting !== undefined ? "On Harvester this switches Harvester's own setting, which sets up hugepages and the kernel modules on each host." : ""}
        It cannot be switched off while V2 volumes exist. <a class="linkish" onclick="v2Details(true)">What each host needs</a></div>
      ${(v2.nodes || []).length ? `<div class="lh-v2-nodes">${v2.nodes.map(n => `<span class="tag ${n.ready ? "ok" : ""}" ${n.missing.length ? `data-tip="Needs ${esc(n.missing.join(" and "))}"` : ""}>${esc(lhShort(n.name))} · ${n.ready ? "ready" : "not ready"}</span>`).join("")}</div>` : ""}</div>`;
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

window.lhSettingsSave = async () => {
  const cap = STATE.data.lhcap || {};
  const body = { over_provisioning: +$("#lh_over").value, minimal_available: +$("#lh_min").value, v2: $("#lh_v2").checked,
    node_down: $("#lh_nodedown")?.value || "" };
  if (body.v2 !== !!cap.v2?.enabled && !(await ask(body.v2
    ? "Enable Longhorn's V2 data engine? Longhorn starts V2 instance managers on every node, which reserve CPU and hugepages even before any V2 volume exists."
    : "Disable the V2 data engine? Longhorn refuses while V2 volumes exist."))) return;
  try {
    const r = await api("/api/longhorn/settings", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
    toast(r.detail, "ok"); lhSettingsPaint();
  } catch (e) { toast(e.message, "bad"); throw e; }
};

/* ---------------- node disks ----------------
   Every disk on a node, what it is for, and the Longhorn disks on it - with
   adding a disk to Longhorn and taking one out. */
const DISK_ROLE = { longhorn: ["Longhorn", "ok"], system: ["system", ""], provisioning: ["being added", "warn"],
  "in use": ["in use", ""], unused: ["unused", "info"] };

function diskRowsHtml(node, disks, harvester) {
  const v2 = STATE.data.lhcap?.v2?.enabled;
  return disks.map(d => {
    const [word, tone] = DISK_ROLE[d.role] || [d.role, ""];
    const lh = d.longhorn.map(x => {
      const pct = x.size_gb ? Math.round(x.used_gb / x.size_gb * 100) : 0;
      return `<div class="disk-lh">
        <div class="between"><span class="mono xs">${esc(x.path)}${x.type === "block" ? ' <span class="tag info">V2</span>' : ""}</span>
          <span class="mono xs">${esc(sizePair(x.used_gb, x.size_gb))} used · ${esc(sizeText(x.allocated_gb))} allocated · ${x.replicas} replica${x.replicas === 1 ? "" : "s"}</span></div>
        ${meter(pct, "", "disk")}
        ${!x.ready ? `<div class="badtext xs disk-why">${esc(x.missing || x.problem || "Longhorn reports this disk not ready")}</div>` : ""}
        <div class="row disk-tags">${(x.tags || []).map(t => `<span class="tag info">${esc(t)}</span>`).join("")
          || '<span class="dim xs">no tags</span>'}
          <button class="linkish xs" data-need="admin" data-tags="${esc(JSON.stringify(x.tags || []))}"
            onclick="diskTags(${jsq(node)},${jsq(x.id)},JSON.parse(this.dataset.tags))">${(x.tags || []).length ? "Edit tags" : "Add tags"}</button></div>
        <div class="row disk-lh-acts">
          ${!x.ready ? `<span class="tag bad" data-tip="${esc(x.problem)}">${x.missing ? "drive missing" : "failed"}</span>
            <button class="btn sm pri" data-need="admin" onclick="diskRetire(${jsq(node)},${jsq(x.id)})"
              title="Let go of its failed replicas so they rebuild from healthy copies, and take it out of Longhorn for a new drive">Replace failed disk</button>` : ""}
          ${x.evicting ? '<span class="tag warn">moving replicas off</span>' : !x.scheduling ? '<span class="tag">no new replicas</span>' : ""}
          <button class="btn sm" data-need="admin" onclick="diskAction('scheduling',${jsq(node)},${jsq(x.id)},${!x.scheduling})">${x.scheduling ? "Stop new replicas" : "Allow new replicas"}</button>
          ${x.ready && x.replicas && !x.evicting ? `<button class="btn sm" data-need="admin" onclick="diskAction('evict',${jsq(node)},${jsq(x.id)},true)" title="Rebuild every replica on this disk somewhere else">Move replicas off</button>` : ""}
          ${x.evicting ? `<button class="btn sm" data-need="admin" onclick="diskAction('evict',${jsq(node)},${jsq(x.id)},false)">Stop moving</button>` : ""}
          ${!x.replicas && !x.scheduling ? `<button class="btn sm danger" data-need="admin" onclick="diskAction('remove',${jsq(node)},${jsq(x.id)})">Remove from Longhorn</button>` : ""}</div></div>`;
    }).join("");
    return `<div class="disk-card">
      <div class="between"><div><b class="mono">${esc(d.device || "Longhorn")}</b> <span class="dim xs">${esc(d.model || "")}</span>
          <div class="dim xs">${esc(sizeText(d.size_gb))}${d.kind ? ` · ${esc(d.kind)}` : ""}${d.mounts.length ? ` · ${esc(d.mounts.slice(0, 3).join(", "))}` : ""}</div></div>
        <div class="row">${d.system ? '<span class="tag">system</span>' : ""}<span class="tag ${tone}">${esc(word)}</span>
          ${d.can_add ? `<button class="btn sm pri" data-need="admin" onclick="diskAdd(${jsq(node)},${jsq(d.blockdevice.name)},${jsq(d.path)},${d.needs_wipe})">Add to Longhorn</button>` : ""}
          ${!harvester && d.role === "unused" && d.device ? `<button class="btn sm pri" data-need="admin" onclick="diskSetup(${jsq(node)},${jsq("/dev/" + d.device)})">Add to Longhorn</button>` : ""}
          ${!harvester && d.system ? `<button class="btn sm" data-need="admin" onclick="diskOsSpace(${jsq(node)})" title="Give Longhorn space nothing uses - free LVM space, or unallocated space on a disk - for V1 or V2">Use free space</button>` : ""}</div></div>
      ${lh}</div>`;
  }).join("") + (harvester ? "" : `<button class="btn sm" data-need="admin" style="margin-top:8px" onclick="diskAdd(${jsq(node)})">＋ Add a disk to Longhorn</button>`)
    + (harvester && !disks.some(d => d.can_add) ? '<div class="dim xs" style="margin-top:8px">Every disk Harvester found here is in use. A new disk shows up once it is plugged in and Harvester has scanned it.</div>' : "");
}

/* A node's own tags, for classes that keep replicas on some nodes. */
function nodeTagsLine(node, inv) {
  const tags = (inv.node_tags || {})[node];
  if (!tags) return "";
  return `<div class="row disk-tags node-tags"><span class="dim xs">Node tags</span>
    ${tags.map(t => `<span class="tag">${esc(t)}</span>`).join("") || '<span class="dim xs">none</span>'}
    <button class="linkish xs" data-need="admin" data-tags="${esc(JSON.stringify(tags))}"
      onclick="nodeTags(${jsq(node)},JSON.parse(this.dataset.tags))">${tags.length ? "Edit" : "Add"}</button></div>`;
}

/* Tags are words Longhorn matches a storage class against: a class that
   asks for "ssd" puts its replicas only on disks tagged ssd. */
const TAG_IDEAS = ["ssd", "nvme", "hdd", "fast", "bulk"];
function tagEditor(title, about, current, known, save) {
  const ideas = [...new Set([...known, ...TAG_IDEAS])].filter(t => !current.includes(t));
  modal(title, `<p class="muted small" style="margin-top:0">${about}</p>
    <div class="f"><label>Tags</label>
      <input type="text" id="tg_text" value="${esc(current.join(", "))}" placeholder="ssd, fast" autocomplete="off"></div>
    ${ideas.length ? `<div class="row" style="gap:6px;flex-wrap:wrap;margin:-4px 0 14px"><span class="dim xs">Add</span>${ideas.map(t =>
      `<button class="tag linkish" onclick="tagAdd(${jsq(t)})">＋ ${esc(t)}</button>`).join("")}</div>` : ""}
    <div class="row"><button class="btn pri" id="tg_save">Save tags</button><button class="btn" onclick="closeModal()">Cancel</button></div>`);
  $("#tg_save").onclick = () => save($("#tg_text").value.split(/[\s,]+/).filter(Boolean));
  $("#tg_text").focus();
}
window.tagAdd = tag => {
  const input = $("#tg_text"), tags = input.value.split(/[\s,]+/).filter(Boolean);
  if (!tags.includes(tag)) tags.push(tag);
  input.value = tags.join(", ");
};
async function tagsSave(path, body, node, reopen) {
  try {
    const r = await api(path, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
    toast(r.detail, "ok"); closeModal(); STATE.data.disks = null;
    if (reopen) lhDisks(); else setTimeout(() => disksRepaint(node), 500);
  } catch (e) { toast(e.message, "bad"); }
}
window.diskTags = async (node, disk, current) => {
  const inv = await loadDisks().catch(() => ({}));
  // Opened from the Disks list (a dialog), it goes back there; from a node's page, it stays.
  const reopen = !$("#modal").classList.contains("hidden");
  tagEditor(`Tags · ${disk}`, `A storage class can keep its replicas on disks with a tag: tag the fast disks
      <b>ssd</b>, say, and make a class that asks for ssd.${inv.harvester ? " Harvester keeps a disk's tags with the disk, as its own dashboard does." : ""}`,
    current, inv.disk_tags || [], tags => tagsSave("/api/disks/tags", { node, disk, tags }, node, reopen));
};
window.nodeTags = async (node, current) => {
  const inv = await loadDisks().catch(() => ({}));
  const reopen = !$("#modal").classList.contains("hidden");
  tagEditor(`Node tags · ${node}`, "A storage class can keep its replicas on nodes with a tag as well as, or instead of, disks with one.",
    current, inv.all_node_tags || [], tags => tagsSave("/api/disks/node-tags", { node, tags }, node, reopen));
};

async function loadDisks(force = false) {
  if (!force && STATE.data.disks && Date.now() - STATE.data.disksAt < 8000) return STATE.data.disks;
  STATE.data.disks = await api("/api/disks");
  STATE.data.disksAt = Date.now();
  return STATE.data.disks;
}

/* The node page's disk card. */
window.nodeDisksPaint = async node => {
  const host = $("#nodeDisks");
  if (!host) return;
  try {
    const inv = await loadDisks(true);
    const disks = inv.nodes[node] || [];
    host.innerHTML = nodeTagsLine(node, inv) + (disks.length ? diskRowsHtml(node, disks, inv.harvester)
      : '<div class="dim small">No disks reported yet: the node probe tells Homestead which disks this host has.</div>');
    // Each disk's partition table, where the host's OS has been read.
    if (window.nodePartitionsPaint) nodePartitionsPaint(node);
    if (window.applyRole) applyRole();
  } catch (e) { host.innerHTML = `<div class="dim small">${esc(e.message)}</div>`; }
};

/* Every node's disks in one place, from Volumes or Settings. */
window.lhDisks = async () => {
  modal("Disks", '<div class="empty"><span class="spin2"></span>reading every node</div>', true);
  try {
    const inv = await loadDisks(true);
    $("#mbody").innerHTML = `<p class="dim small" style="margin-top:0">${inv.harvester
      ? "Harvester lists each host's disks. Adding one lets Harvester format it and hand it to Longhorn, as its own UI does."
      : "Longhorn stores data in folders where a disk is mounted, or - for the V2 engine - on a raw device."}</p>
      ${Object.entries(inv.nodes).map(([node, disks]) => `<div class="sec">${esc(node)}</div>${nodeTagsLine(node, inv)}${diskRowsHtml(node, disks, inv.harvester)}`).join("")
        || '<div class="empty small">No node has reported its disks yet.</div>'}`;
    window.__disksModal = true;
    if (window.applyRole) applyRole();
  } catch (e) { $("#mbody").innerHTML = `<div class="empty">${esc(e.message)}</div>`; }
};

function disksRepaint(node) {
  if ($("#nodeDisks")) nodeDisksPaint(node);
  else if (window.__disksModal && !$("#modal").classList.contains("hidden")) lhDisks();
}

window.diskAction = async (action, node, disk, value) => {
  if (action === "remove" && !(await ask(`Remove ${disk} on ${node} from Longhorn? Nothing is on it, and its files are left where they are.`))) return;
  if (action === "evict" && value && !(await ask(`Move every replica off ${disk}? Longhorn rebuilds each one on another disk first, which copies their data.`))) return;
  const body = { node, disk };
  if (action === "scheduling") body.allow = value;
  if (action === "evict") body.on = value;
  try {
    const r = await api(`/api/disks/${action}`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
    toast(r.detail, "ok"); STATE.data.disks = null; setTimeout(() => disksRepaint(node), 800);
  } catch (e) { toast(e.message, "bad"); }
};

window.diskAdd = (node, blockdevice = "", path = "", needsWipe = false) => {
  const v2 = STATE.data.lhcap?.v2?.enabled;
  const back = $("#nodeDisks") ? null : true;
  childModal(`Add a disk · ${node}`, `
    ${blockdevice ? `<p>Harvester formats <b class="mono">${esc(path)}</b> and gives it to Longhorn, which starts placing replicas on it.</p>`
      : `${diskMountGuide(path)}
        <div class="f"><label>Where the disk is ${tip("For the V1 engine: the folder a formatted disk is mounted at on the host, like /mnt/disk2. For V2: the raw device, like /dev/sdb.")}</label>
        <input id="da_path" class="mono" placeholder="/mnt/disk2" value="${path ? `/mnt/${esc(path.replace("/dev/", ""))}` : ""}"></div>`}
    <div class="f"><label>Engine</label><select id="da_engine">
      <option value="v1">V1 — ${blockdevice ? "formatted and mounted" : "a mounted folder"}</option>
      ${v2 ? `<option value="v2">V2 (SPDK) — the raw device</option>` : ""}</select>
      ${v2 ? "" : '<div class="dim xs">The V2 engine is off; switch it on in Settings › Hardware and storage to add a V2 disk.</div>'}</div>
    ${blockdevice ? `<label class="switch"><input type="checkbox" id="da_wipe" ${needsWipe ? "" : "disabled"}> Erase it first
      ${needsWipe ? '<span class="badtext xs">— it already holds a filesystem or partitions, which are destroyed</span>' : '<span class="dim xs">— it is blank</span>'}</label>` : ""}
    <div class="row" style="margin-top:14px"><button class="btn pri" onclick="diskAddGo(${jsq(node)},${jsq(blockdevice)})">Add</button>
      <button class="btn" onclick="modalBack()">Cancel</button></div>`);
};
/* Off Harvester: set a whole disk up for Longhorn from here. Homestead looks
   at it on its host first (homestead_disk_setup), then offers only what is
   safe for what it found: format a blank disk, keep one that already holds
   Longhorn data, or - typed to confirm - erase one holding something else.
   The system disk, and a mounted one, are refused. */
window.diskSetup = async (node, device) => {
  const v2 = STATE.data.lhcap?.v2?.enabled;
  childModal(`Add ${device} · ${node}`, '<div class="empty"><span class="spin2"></span> Looking at the disk on its host…</div>');
  let f;
  try {
    f = await api("/api/disks/inspect", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ node, device }) });
  } catch (e) { $("#mbody").innerHTML = UI.callout("bad", "The disk could not be looked at.", esc(e.message)) + UI.actions(UI.button("Back", "modalBack()")); return; }
  STATE.data.diskFacts = f;
  const what = {
    blank: "It is blank.",
    longhorn: `It already holds Longhorn data${f.replicas ? ` - ${f.replicas} replica folder${f.replicas === 1 ? "" : "s"}` : ""}.`,
    data: `It holds a ${esc(f.fstype)} filesystem with ${f.entries} item${f.entries === 1 ? "" : "s"} at its top.`,
    partitioned: `It has ${f.partitions.length} partition${f.partitions.length === 1 ? "" : "s"} (${esc(f.partitions.map(p => `${p.name}${p.fstype ? ` ${p.fstype}` : ""}`).join(", "))}).`,
    system: `It is this host's system disk (${esc(f.mounts.join(", "))}).`,
    mounted: `It is mounted at ${esc(f.mounts.join(", "))}.`,
    "longhorn-v2": `Longhorn's V2 engine keeps data on it (${esc((f.longhorn_block || []).join(", "))}): move its replicas off and remove it from Longhorn before setting it up again.`,
    held: `It cannot be wiped:<ul class="small">${(f.reasons || []).map(r => `<li>${esc(r)}</li>`).join("")}</ul>`,
    lvm: `It holds LVM from an earlier install that nothing on this host is using: ${esc((f.lvm || []).map(g =>
      `volume group ${g.vg || g.uuid}${g.lvs.length ? ` (${g.lvs.map(l => l.name).join(", ")})` : ""}`).concat((f.orphan_pvs || []).map(p => `an LVM label on ${p}`)).join("; "))}${
      f.partitions.length ? `, on ${f.partitions.length} partition${f.partitions.length === 1 ? "" : "s"}` : ""}.`,
    missing: esc(f.error || "It is not there."),
  }[f.state];
  const choice = (value, label, detail, checked) => `<label class="disk-choice"><input type="radio" name="ds_mode" value="${value}" ${checked ? "checked" : ""} onchange="diskSetupChanged()">
    <span><b>${label}</b><small>${detail}</small></span></label>`;
  const modes = f.choices || [];
  $("#mbody").innerHTML = `<div class="ui-stack">
    <div class="note ${["system", "mounted", "missing", "held", "longhorn-v2"].includes(f.state) ? "bad" : ""}"><b class="mono">${esc(device)}</b> · ${f.size_gb} GB${f.by_id ? ` · <span class="mono dim xs">${esc(f.by_id.replace("/dev/disk/by-id/", ""))}</span>` : ""}<br>${what}${(f.multipath || []).length ? `<br><span class="small">multipathd has claimed it (${esc(f.multipath.map(m => m.name).join(", "))}). Setting it up releases that and adds its WWID to the blacklist in /etc/multipath.conf, so multipathd leaves this disk alone; nothing else changes.</span>` : ""}</div>
    ${modes.length ? `
      ${v2 ? `<div class="f"><label>Engine</label><select id="ds_engine" onchange="diskSetupChanged()"><option value="v1">V1 - formatted and mounted</option><option value="v2">V2 (SPDK) - the raw device</option></select></div>` : ""}
      <div class="disk-choices" id="ds_modes">
        ${modes.includes("format") ? choice("format", "Format it", `As a filesystem mounted at <span class="mono">${esc(f.mount_point)}</span>, for Longhorn.`, true) : ""}
        ${modes.includes("import") ? choice("import", "Keep its Longhorn data", `Mounted as it is at <span class="mono">${esc(f.mount_point)}</span>. Replicas from another cluster show in Longhorn as orphaned data; volumes come back from their backups.`, true) : ""}
        ${modes.includes("wipe") ? choice("wipe", "Wipe and prepare it", "Switches its old volume groups off and removes them, wipes the LVM labels, filesystem signatures and partition table, has the host re-read the disk, checks it is blank, then formats it. Everything on it is destroyed.", true) : ""}
        ${modes.includes("erase") ? choice("erase", "Erase it and format", "Everything on it is destroyed.", !modes.includes("import")) : ""}</div>
      <div class="f" id="ds_fs_row"><label>Filesystem</label><select id="ds_fs"><option value="ext4">ext4</option><option value="xfs">XFS</option></select></div>
      <div class="f" id="ds_confirm_row"><label>Type <span class="mono">${esc(device)}</span> to confirm - this erases it</label><input id="ds_confirm" class="mono" autocomplete="off"></div>
      <p class="dim xs">Mounted the safe way: the empty folder is locked so nothing lands on the system disk if the drive dies, fstab names it
        by UUID with <span class="mono">nofail</span> so the host still starts without it (a copy of fstab is kept first), and the mount is checked
        before Longhorn is told. New disks are tagged <span class="mono">ssd</span> or <span class="mono">hdd</span>.</p>
      ${UI.actions(UI.button("Back", "modalBack()") + UI.button("Set it up", `diskSetupGo(${jsArg(node)},${jsArg(device)})`, { kind: "pri", id: "ds_go", attrs: 'data-need="admin"' }))}`
      : UI.actions(UI.button("Back", "modalBack()"))}</div>`;
  diskSetupChanged();
  if (window.applyRole) applyRole();
};

/* The OS drive's unused LVM space, as a Longhorn disk of its own. */
/* Space nothing uses, given to Longhorn without touching what is there: a
   logical volume in the OS drive's volume group, or a new partition in a
   disk's unallocated space - made live, the table saved first. V1 gets a
   filesystem of its own; V2 the raw volume or partition. */
window.diskOsSpace = async node => {
  childModal(`Free space · ${node}`, '<div class="empty"><span class="spin2"></span> Looking at the host\'s disks…</div>');
  let f;
  try {
    f = await api("/api/disks/os-space", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ node }) });
  } catch (e) { $("#mbody").innerHTML = UI.callout("bad", "The disks could not be looked at.", esc(e.message)) + UI.actions(UI.button("Back", "modalBack()")); return; }
  STATE.data.osSpace = f;
  const v2 = !!STATE.data.lhcap?.v2?.enabled;
  const lvmFree = f.vg && !f.error && f.usable_gb >= 5;
  const sources = [
    ...(lvmFree && (!f.taken?.v1 || (v2 && !f.taken?.v2))
      ? [["lvm", `${f.vg} · ${f.usable_gb} GB free, ${f.reserve_gb} GB kept for the system`, f.usable_gb]] : []),
    ...(f.regions || []).map(r => [`part:${r.disk}:${r.start}`, `/dev/${r.disk} · ${r.size_gb} GB unallocated - a new partition`, r.size_gb]),
  ];
  if (f.problem || !sources.length) {
    $("#mbody").innerHTML = `<div class="ui-stack">${UI.callout("warn", "Nothing to use",
      `${esc(((f.problem || f.lvm_problems?.v1 || "no free space") + "").replace(/^./, c => c.toUpperCase()))}.`)}
      ${UI.more("What Homestead will and will not do", `<p class="small">It uses space nothing else does: free space in the
        system's LVM volume group, or space past a GPT disk's last partition. It never shrinks or moves a partition or
        filesystem of a running system - ext4 cannot shrink while mounted and XFS not at all; that needs a rescue boot.</p>`)}
      ${UI.actions(UI.button("Back", "modalBack()"))}</div>`;
    return;
  }
  $("#mbody").innerHTML = `<div class="ui-stack">
    ${UI.facts([["Root filesystem free", `${f.root_free_gb} GB`], f.vg ? ["Volume group", `<span class="mono">${esc(f.vg)}</span> · ${f.size_gb} GB`] : null])}
    <div class="f"><label>Space</label><select id="os_source" onchange="diskOsSpaceChanged(true)">${sources.map(([value, label]) =>
      `<option value="${esc(value)}">${esc(label)}</option>`).join("")}</select></div>
    ${v2 ? `<div class="f"><label>Engine</label><select id="os_engine" onchange="diskOsSpaceChanged()">
      <option value="v1">V1 - a filesystem of its own, mounted</option><option value="v2">V2 (SPDK) - the raw volume or partition</option></select></div>` : ""}
    <div class="f"><label>Size (GB)</label><input id="os_size" type="number" min="5"></div>
    <p class="small" id="os_what"></p>
    <div class="f" id="os_confirm_row" hidden><label id="os_confirm_label"></label><input id="os_confirm" class="mono" autocomplete="off"></div>
    <p class="dim xs">On the system's drive it shares a drive with the system, so a copy here and one on another folder of that drive are not two drives.</p>
    ${UI.actions(UI.button("Back", "modalBack()") + UI.button("Create and add", `diskOsSpaceGo(${jsArg(node)})`, { kind: "pri", id: "os_go", attrs: 'data-need="admin"' }))}</div>`;
  window.__osSources = Object.fromEntries(sources.map(([value, , max]) => [value, max]));
  diskOsSpaceChanged(true);
  if (window.applyRole) applyRole();
};
window.diskOsSpaceChanged = (first = false) => {
  const f = STATE.data.osSpace || {}, source = $("#os_source").value, engine = $("#os_engine")?.value || "v1";
  const max = (window.__osSources || {})[source] || 5, size = $("#os_size");
  size.max = max;
  if (first || +size.value > max || !+size.value) size.value = max;
  const part = source.startsWith("part:"), disk = part ? source.split(":")[1] : "";
  const taken = !part && f.taken?.[engine];
  $("#os_what").innerHTML = taken ? `<b>${esc(f.vg)}/${engine === "v2" ? "longhorn-v2" : "longhorn"}</b> exists already; choose the other engine or unallocated space.`
    : part ? `A new GPT partition after the last one on <span class="mono">/dev/${esc(disk)}</span>, made while the system runs: the
      partition table is saved to <span class="mono">/var/lib/homestead</span>, the space is checked free again, and only the new
      partition is shown to the kernel. ${engine === "v2" ? "It is given raw to the V2 engine by its PARTUUID." : `Formatted ext4 and mounted at <span class="mono">/mnt/${esc(disk)}-longhorn</span> the safe way.`}`
    : `A logical volume of its own, <span class="mono">${esc(f.vg)}/${engine === "v2" ? "longhorn-v2" : "longhorn"}</span>, ${engine === "v2"
      ? "given raw to the V2 engine" : `formatted ext4 and mounted at <span class="mono">${esc(f.mount_point)}</span> the safe way`}. Nothing existing is resized.`;
  $("#os_confirm_row").hidden = !part;
  if (part) $("#os_confirm_label").innerHTML = `Type <span class="mono">/dev/${esc(disk)}</span> to confirm - its partition table gets a new entry`;
  if ($("#os_go")) $("#os_go").disabled = !!taken;
};
window.diskOsSpaceGo = async node => {
  const go = $("#os_go");
  if (go) { go.disabled = true; go.textContent = "Creating…"; }
  try {
    const r = await api("/api/disks/os-space/use", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ node, size_gb: +$("#os_size").value, source: $("#os_source").value,
        engine: $("#os_engine")?.value || "v1", confirm: $("#os_confirm")?.value || "" }) });
    toast(r.detail, "ok"); STATE.data.disks = null; modalBack(); setTimeout(() => disksRepaint(node), 800);
  } catch (e) {
    toast(e.message, "bad");
    if (go) { go.disabled = false; go.textContent = "Create and add"; }
  }
};

window.diskSetupChanged = () => {
  const f = STATE.data.diskFacts || {};
  const v2 = $("#ds_engine")?.value === "v2";
  const mode = $("input[name=ds_mode]:checked")?.value || "";
  if ($("#ds_modes")) $("#ds_modes").hidden = v2;
  if ($("#ds_fs_row")) $("#ds_fs_row").hidden = v2 || mode === "import";
  // Formatting a blank disk still asks for the device: it is a format all the same.
  if ($("#ds_confirm_row")) $("#ds_confirm_row").hidden = v2 ? f.state === "blank" : mode === "import";
};

window.diskSetupGo = async (node, device) => {
  const v2 = $("#ds_engine")?.value === "v2";
  const body = { node, device, engine: v2 ? "v2" : "v1", mode: $("input[name=ds_mode]:checked")?.value || "",
    fstype: $("#ds_fs")?.value || "ext4", confirm: $("#ds_confirm")?.value || "" };
  const go = $("#ds_go");
  if (go) { go.disabled = true; go.textContent = "Setting it up…"; }
  try {
    const r = await api("/api/disks/setup", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
    toast(r.detail, "ok"); STATE.data.disks = null; modalBack(); setTimeout(() => disksRepaint(node), 800);
  } catch (e) {
    toast(e.message, "bad");
    if (go) { go.disabled = false; go.textContent = "Set it up"; }
  }
};

/* Off Harvester, a disk is mounted on the host by hand before Longhorn is
   given the folder - and how it is mounted decides what a dead drive does at
   the next boot. A plain fstab line makes systemd wait for the drive and drop
   the host to an emergency shell when it never comes; nofail lets the host
   start without it. The folder is made immutable while empty, so when the
   drive is missing nothing can write into the folder on the system disk:
   Longhorn marks the disk failed instead of filling the system disk. */
function diskMountGuide(device) {
  const dev = device || "/dev/sdX";
  const dir = device ? `/mnt/${device.replace("/dev/", "")}` : "/mnt/disk2";
  const lines = [
    `sudo mkfs.ext4 ${dev}                # erases it`,
    `sudo mkdir -p ${dir} && sudo chattr +i ${dir}`,
    `echo "UUID=$(sudo blkid -s UUID -o value ${dev}) ${dir} ext4 defaults,nofail,x-systemd.device-timeout=10s 0 2" | sudo tee -a /etc/fstab`,
    `sudo mount ${dir}`];
  return `<div class="note" style="margin-bottom:12px"><b>Mount it on the host first</b>, as root on ${device ? "that machine" : "the machine the disk is in"}:
    <pre class="mono xs" style="white-space:pre-wrap;margin:8px 0">${esc(lines.join("\n"))}</pre>
    <span class="dim xs"><b>nofail</b> lets the machine start when this drive is dead or missing - without it, it stops at an emergency
    shell. The empty folder is locked (<span class="mono">chattr +i</span>) so that, with the drive gone, nothing lands on the system disk
    in its place: Longhorn marks the disk failed, and Homestead offers to replace it.</span></div>`;
}

/* Replacing a failed disk: the review says what happens to every volume
   that had a copy on it before anything is let go of. */
window.diskRetire = async (node, disk) => {
  childModal(`Replace failed disk · ${node}`, '<div class="empty"><span class="spin2"></span>reading its replicas</div>');
  let p;
  try {
    p = await api("/api/disks/retire/plan", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ node, disk }) });
  } catch (e) { $("#mbody").innerHTML = `<div class="note bad">${esc(e.message)}</div>`; return; }
  const word = { "only-copy": ["only copy", "bad"], waits: ["waits for the new disk", "warn"], elsewhere: ["rebuilds elsewhere", "ok"] };
  $("#mbody").innerHTML = `
    <p style="margin-top:0"><b class="mono">${esc(p.path)}</b> on ${esc(node)}${p.problem ? ` - <span class="badtext">${esc(p.problem)}</span>` : ""}</p>
    <p class="small">Longhorn keeps a failed disk, and the replicas it held, until told otherwise. This lets go of them, so
      each volume rebuilds from its healthy copy, then takes the disk out of Longhorn${p.harvester_device ? " and Harvester" : ""}
      so the new drive can be added in its place.</p>
    ${p.volumes.length ? `<div class="sec">${p.volumes.length} volume${p.volumes.length === 1 ? "" : "s"} had a copy on it</div>
      <div class="rc-uses">${p.volumes.map(v => `<div><b>${esc(v.claim)}</b> <span class="tag ${word[v.outcome][1]}">${word[v.outcome][0]}</span>
        <span class="dim xs">${esc(v.why)}</span></div>`).join("")}</div>` : '<div class="dim small">No replicas were on it.</div>'}
    ${p.only_copies ? `<div class="note bad" style="margin-top:12px"><b>${p.only_copies} volume${p.only_copies === 1 ? "" : "s"} had the only copy on this disk.</b>
      If the drive is only unplugged, reconnect it instead: the data comes back with it. Otherwise restore
      ${p.only_copies === 1 ? "it" : "them"} from a backup (Data protection). Carrying on leaves ${p.only_copies === 1 ? "it" : "them"} alone and keeps the disk,
      unless you give ${p.only_copies === 1 ? "it" : "them"} up:
      <label class="switch" style="margin-top:8px"><input type="checkbox" id="dr_force" onchange="$('#dr_confirm_row').hidden=!this.checked"> Give ${p.only_copies === 1 ? "it" : "them"} up - the data is lost</label>
      <div id="dr_confirm_row" hidden class="f" style="margin-top:8px"><label>Type <span class="mono">${esc(disk)}</span> to confirm</label><input id="dr_confirm" class="mono"></div></div>` : ""}
    <div class="row" style="margin-top:14px"><button class="btn pri" data-need="admin" onclick="diskRetireGo(${jsq(node)},${jsq(disk)})">Replace it</button>
      <button class="btn" onclick="modalBack()">Cancel</button></div>`;
  if (window.applyRole) applyRole();
};
window.diskRetireGo = async (node, disk) => {
  const force = !!$("#dr_force")?.checked;
  try {
    await api("/api/disks/retire", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ node, disk, force, confirm: force ? $("#dr_confirm").value.trim() : "" }) });
    toast("Replacing the failed disk - follow it in the job tray", "ok");
    STATE.data.disks = null; modalBack(); if (window.refreshOperations) refreshOperations(true);
    setTimeout(() => disksRepaint(node), 1500);
  } catch (e) { toast(e.message, "bad"); }
};

window.diskAddGo = async (node, blockdevice) => {
  const body = { node, engine: $("#da_engine").value };
  if (blockdevice) {
    body.blockdevice = blockdevice; body.wipe = !!$("#da_wipe")?.checked;
    if (body.wipe && !(await ask("Erase everything on this disk? This cannot be undone."))) return;
  } else body.path = $("#da_path").value.trim();
  try {
    const r = await api("/api/disks/add", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
    toast(r.detail, "ok"); STATE.data.disks = null; modalBack(); setTimeout(() => disksRepaint(node), 800);
  } catch (e) { toast(e.message, "bad"); }
};

/* ---------------- changing a volume's storage class ----------------
   A review first - what uses it, what stops, how much room it takes while
   both copies exist - then the move as a tracked job with its steps. */
let RECLASS_REVIEW = null, RECLASS_SEQUENCE = 0, RECLASS_BUSY = false;
window.volumeReclass = async x => {
  if (RECLASS_BUSY) return;
  RECLASS_REVIEW = null;
  const classes = (STATE.data.storageClasses || []).filter(c => !c.internal && !c.made_for && c.name !== x.storage_class);
  if (!classes.length) return toast("there is no other storage class to move it to", "warn");
  const pick = classes.find(c => c.default) || classes[0];
  modal(`Change storage class · ${x.pvc_name || x.name}`, UI.lead("Copy this volume to another storage class, keeping its name. Workloads using it stop during the move; the original data is kept afterward.") +
    UI.fields(UI.field("From", `<input value="${esc(x.storage_class || "unknown")}" disabled>`),
      UI.field("To", `<select id="rc_to" onchange="volumeReclassPlan(${jsq(x.namespace || "lab")},${jsq(x.pvc_name || x.name)})">${classes.map(c => `<option value="${esc(c.name)}" ${c.name === pick.name ? "selected" : ""}>${esc(c.name)}${c.replicas ? ` · ${esc(c.replicas)} copies` : ""}</option>`).join("")}</select>`)) +
    '<div id="rc_plan"></div>' +
    UI.ack("rc_ack", "I approve the downtime and the storage warnings shown above.", {onchange:"volumeReclassReady()"}) +
    UI.actions(UI.cancel() + UI.button("Move volume", `volumeReclassStart(${jsArg(x.namespace || "lab")},${jsArg(x.pvc_name || x.name)})`, {kind:"pri",id:"rc_go",disabled:true})), false, "operation-review");
  volumeReclassPlan(x.namespace || "lab", x.pvc_name || x.name);
};

window.volumeReclassPlan = async (ns, claim) => {
  if (RECLASS_BUSY) return;
  RECLASS_REVIEW = null;
  const sequence = ++RECLASS_SEQUENCE;
  const host = $("#rc_plan"), go = $("#rc_go");
  if (!host) return;
  host.innerHTML = '<div class="empty"><span class="spin2"></span> checking what uses it and where it fits</div>';
  if (go) go.disabled = true;
  if ($("#rc_ack")) $("#rc_ack").checked = false;
  const config = {namespace:ns,claim,target:$("#rc_to").value};
  let p;
  try {
    p = await api("/api/volumes/reclass/plan", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(config) });
    if (sequence !== RECLASS_SEQUENCE || $("#rc_plan") !== host) return;
    if (!p.capacity_token || !Array.isArray(p.blockers) || !Array.isArray(p.warnings) || !Array.isArray(p.consumers)) throw new Error("Review is incomplete; nothing can start.");
  } catch (e) { if (sequence === RECLASS_SEQUENCE && $("#rc_plan") === host) host.innerHTML = UI.callout("bad", "Review unavailable", esc(e.message)); return; }
  const sp = p.space || {};
  const verb = c => c.kind === "VirtualMachine" ? (c.running ? "shut down, then started again" : "stopped already; stays stopped")
    : c.kind === "CronJob" ? "paused, then resumed" : c.running ? "stopped, then started again" : "stopped already; stays stopped";
  const messages = [...p.blockers, ...p.warnings];
  host.innerHTML = (messages.length ? UI.callout(p.blockers.length ? "bad" : "warn", p.blockers.length ? "This move cannot start" : "Before moving", `<ul>${messages.map(m=>`<li>${esc(m)}</li>`).join("")}</ul>`) : "") +
    UI.section("Affected workloads", UI.table([{label:"Workload"},{label:"Effect"}], p.consumers.map(c=>[`${esc(c.name)} · ${esc(c.kind)}`,esc(verb(c))]), {empty:"No workloads currently reference this volume."})) +
    UI.section("Storage needed", UI.facts([["New volume",esc(sizeText(sp.size_gb))],["Replica allocation",`${esc(sizeText(sp.allocated_gb))} · ${esc(sp.replicas)} copies`],["Estimated destination room",sp.room_gb == null ? "Unknown" : esc(sizeText(sp.room_gb))]])) +
    UI.more("What is checked", "The volume, backing disk, destination class and affected workloads are checked again before starting. Missing inventory blocks the move. This is not a storage or scheduler reservation; later recovery and host-loss validation remain separate safeguards. Both copies use space until you remove the original.");
  RECLASS_REVIEW = {config,plan:p};
  volumeReclassReady();
  if (window.applyRole) applyRole();
};

window.volumeReclassReady = () => {
  const ready = !!(RECLASS_REVIEW?.plan.ok && !RECLASS_REVIEW.plan.blockers.length && !RECLASS_BUSY && $("#rc_ack")?.checked && $("#rc_to")?.value === RECLASS_REVIEW.config.target);
  if ($("#rc_go")) $("#rc_go").disabled = !ready;
  return ready;
};

window.volumeReclassStart = async (ns, claim) => {
  if (!volumeReclassReady() || RECLASS_REVIEW.config.namespace !== ns || RECLASS_REVIEW.config.claim !== claim) return;
  const review = RECLASS_REVIEW, host = $("#rc_plan"), go = $("#rc_go");
  RECLASS_REVIEW = null; RECLASS_BUSY = true; go.disabled = true; $("#rc_to").disabled = true;
  try {
    const r = await api("/api/volumes/reclass/start", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({...review.config,capacity_token:review.plan.capacity_token,confirm_capacity:true}) });
    if (!r.operation?.id) throw new Error("Move receipt was not returned");
    if (typeof noteOperation === "function") noteOperation(r.operation);
    if ($("#rc_plan") === host) reclassWatch(r.operation.id);
  } catch (e) {
    if ($("#rc_plan") === host) host.innerHTML = UI.callout("bad", "Start not confirmed", esc(e.message) + " Check Recent jobs before reviewing again. Nothing was retried.");
    else toast("Move start not confirmed; check Recent jobs.", "bad");
  } finally {
    RECLASS_BUSY = false;
    if ($("#rc_plan") === host && $("#rc_to")) $("#rc_to").disabled = false;
    if (typeof refreshOperations === "function") refreshOperations();
  }
};

/* The move as it runs: its steps, the copy's progress, and at the end the
   old copy to remove when you are happy. */
window.reclassWatch = async id => {
  const watchToken = {}; window.__reclassWatchToken = watchToken;
  const paint = op => {
    const steps = op.steps || [];
    const copy = op.copy || {};
    $("#mbody").innerHTML = `<div class="rc-steps">${steps.map(s => `<div class="rc-step ${s.state}"><span>${s.state === "done" ? "✓" : s.state === "active" ? '<span class="spin2"></span>' : "•"}</span>${esc(s.label)}
        ${s.state === "active" && (s.id === "copy" || s.id === "verify") ? `<div class="rc-copy">${meter(s.id === "verify" ? 100 : copy.percent || 0, "", "cpu")}
          <span class="mono xs">${s.id === "verify" ? "comparing with the original" : copy.unavailable ? "waiting for reported progress" : `${copy.percent || 0}%${copy.speed ? ` · ${esc(copy.speed)}` : ""}`}</span></div>` : ""}</div>`).join("")}</div>
      <div class="note ${op.status === "failed" ? "bad" : op.status === "succeeded" ? "good" : ""}" style="margin-top:12px">${esc(op.message || "")}</div>
      ${op.storage_recovery ? `<div class="row"><button class="btn pri" data-need="admin" onclick="storageRecoveryReview(${jsq(op.id)})">Review storage move</button></div>` : ""}
      ${op.status === "failed" && op.resumable ? `<div class="row" style="margin-top:12px"><button class="btn pri" data-need="admin" onclick="resumeOperation(${jsq(op.id)})">Carry on from this step</button></div>` : ""}
      ${op.status === "succeeded" && op.old_pv ? `<div class="row" style="margin-top:12px"><button class="btn danger" data-need="admin" onclick="reclassRemoveOld(${jsq(op.old_pv)})">Remove the old copy</button>
        <span class="dim xs">Keep it until the app is working on the new one.</span></div>` : ""}
      <div class="row" style="margin-top:12px"><button class="btn" onclick="closeModal()">${op.status === "running" ? "Keep going in the background" : "Close"}</button></div>`;
    if (window.applyRole) applyRole();
  };
  modal("Changing storage class", '<div class="empty"><span class="spin2"></span></div>', true);
  clearInterval(window.__reclassTimer);
  const tick = async () => {
    if ($("#modal").classList.contains("hidden")) return clearInterval(window.__reclassTimer);
    const op = (await api("/api/operations", { keep: true }).catch(() => [])).find(o => o.id === id);
    if (!op || window.__reclassWatchToken !== watchToken || $("#modal").classList.contains("hidden")) return;
    $("#mtitle").textContent = op.title;
    paint(op);
    if (op.status !== "running") { clearInterval(window.__reclassTimer); if (STATE.view === "storage") refresh(true); }
  };
  tick();
  window.__reclassTimer = setInterval(tick, 2500);
};

window.reclassRemoveOld = async pv => {
  if (!(await ask(`Remove the old copy ${pv}? Its data is deleted.`))) return;
  try {
    const r = await api("/api/volumes/old-copies/remove", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ pv }) });
    toast(r.detail, "ok");
    if ($("#modal") && !$("#modal").classList.contains("hidden") && $("#mtitle").textContent.startsWith("Move")) closeModal();
    if (STATE.view === "storage") refresh(true);
  } catch (e) { toast(e.message, "bad"); }
};
