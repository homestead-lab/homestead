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
  const kind = w => w.kind === "vm" ? "virtual machine" : (w.node || "").replace(/^harvester-/, "");

  paint(`<div class="phead">
      <div><h2>Architecture</h2><p>How each app is reached, where its data lives, and which hosts hold the copies · hover anything to trace it</p></div>
      <div class="row hide-sm arch-legend">
        <span><i style="background:var(--arch-access)"></i>port</span>
        <span><i style="background:var(--arch-mount)"></i>mount</span>
        <span><i style="background:var(--arch-copy)"></i>replica</span>
      </div></div>
    <div class="arch2wrap"><svg id="archsvg" aria-hidden="true"></svg><div class="arch2">

      <section class="a2col"><h4>Access</h4>
      ${f.vips.map(v => `<div class="a2item a2vip" data-kind="access" data-id="${esc(v.id)}">
          <div class="a2vipip mono">${esc(v.ip)}</div>
          <div class="a2ports">${v.ports.map(p => `<span class="a2port" id="${esc(portId(v.ip, p.port))}" data-kind="access"
            data-id="${esc(portId(v.ip, p.port))}" title="${esc(p.app)} · open ${esc(v.ip)}:${p.port}"
            onclick="openSvc('${esc(v.ip)}',${p.port})">${p.port}</span>`).join("")}</div></div>`).join("")
        || '<div class="dim xs">No load-balancer addresses</div>'}
      </section>

      <section class="a2col"><h4>Containers <span class="dim">${f.workloads.length}</span></h4>
      ${f.workloads.map(w => `<div class="a2item a2wl ${used.has(w.id) ? "" : "a2alone"}" id="${esc(w.id)}" data-kind="workload" data-id="${esc(w.id)}"
          title="${esc(w.name)} · ${esc(kind(w))} · ${workloadCpuPercent(w.cpu)} CPU · ${w.mem_mb || 0} MB">
          ${appAvatar(w.name, w.icon)}<span class="a2name">${esc(w.name)}</span>
          <span class="a2meta">${esc(kind(w))}</span>
          <button class="a2act" data-need="operator" title="${w.kind === "vm" ? "Migrate" : "Move to another host"}"
            onclick="event.stopPropagation();${w.kind === "vm" ? `vmMove('${esc(w.ns || "lab")}','${esc(w.name)}')` : `moveWorkload('${esc(w.name)}','${esc(w.ns || "lab")}')`}">⇄</button>
        </div>`).join("") || '<div class="dim xs">Nothing running</div>'}
      </section>

      <section class="a2col"><h4>Volumes <span class="dim">${f.volumes.length}</span></h4>
      ${f.volumes.map(v => `<div class="a2item a2vol ${used.has(v.id) ? "" : "a2alone"}" id="${esc(v.id)}" data-kind="volume" data-id="${esc(v.id)}"
          title="${esc(v.name)} · ${v.size_gb} GB · ${v.replicas} replicas · ${esc(v.robustness)}${v.attached ? ` · attached on ${esc(v.attached)}` : ""}">
          <span class="a2dot" style="background:${ROB(v.robustness)}"></span><span class="a2name">${esc(v.name)}</span>
          <span class="a2meta mono">${v.size_gb}G · ${v.replicas}×</span></div>`).join("")
        || '<div class="dim xs">No volumes</div>'}
      </section>

      <section class="a2col"><h4>Nodes &amp; replica copies</h4>
      ${f.nodes.map(n => `<div class="a2item a2node" data-kind="node" data-id="${esc(n.id)}">
          <div class="a2nodehead"><b>${esc(n.name)}</b><span class="dim xs">${n.copies.length} cop${n.copies.length === 1 ? "y" : "ies"}</span></div>
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
  const keep = new Set(start);
  const walk = (from, forward) => {
    links.forEach(([a, b]) => {
      const next = forward ? (a === from ? b : null) : (b === from ? a : null);
      if (next && !keep.has(next)) { keep.add(next); walk(next, forward); }
    });
  };
  start.forEach(item => { walk(item, true); walk(item, false); });
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

async function viewStorage() {
  if (platformLacks("longhorn", "Volumes")) return;
  const [v, st, classes, v2] = await Promise.all([api("/api/volumes"), api("/api/storage").catch(() => null),
    api("/api/storage/classes").catch(() => []), api("/api/storage/v2").catch(() => null)]);
  STATE.data.storageClasses = classes;
  STATE.data.v2 = v2;
  STATE.data.vols = v;
  const q = STATE.q.toLowerCase();
  const rows = v.filter(x => !q || x.name.includes(q) || (x.node || "").includes(q) ||
    (x.pvc_name || "").includes(q) || (x.attached_to || "").toLowerCase().includes(q));
  paint(`<div class="phead"><div><h2>Volumes</h2>
      <p>${v.length} Longhorn volume${v.length === 1 ? "" : "s"} · replicated block storage</p></div>
      <button class="btn pri" data-need="operator" onclick="volumeCreate()">＋ Create volume</button></div>
  ${st ? `<div class="grid g4 statgrid" style="margin-bottom:18px">
    <div class="card glow g-info"><div class="ctitle">Free space</div>
      <div class="bignum" style="margin-top:8px">${st.avail_gb}<span class="unit">GB</span></div>
      <div class="csub">of ${st.cap_gb} GB raw</div>${meter(st.used_pct, 'style="margin-top:10px"')}</div>
    <div class="card flat"><div class="ctitle">Provisioned</div>
      <div class="bignum" style="margin-top:8px">${st.provisioned_gb}<span class="unit">GB</span></div>
      <div class="csub">${st.actual_gb} GB actually written</div></div>
    <div class="card flat statwide"><div class="ctitle">Replica health</div>${(st.reasons || []).length
      ? `<div class="dim xs volume-reason" style="margin-top:8px">${esc(st.reasons[0].name)}: ${esc(st.reasons[0].reason)}${st.reasons.length > 1 ? ` · and ${st.reasons.length - 1} more` : ""}</div>` : ""}
      <div class="row" style="margin-top:10px;gap:8px;flex-wrap:wrap">
        <span class="tag ok">${st.healthy} healthy</span>
        ${st.degraded ? `<span class="tag warn">${st.degraded} degraded</span>` : ""}
        ${st.faulted ? `<span class="tag bad">${st.faulted} faulted</span>` : ""}
        ${(st.detached ?? st.unknown) ? `<span class="tag">${st.detached ?? st.unknown} detached</span>` : ""}</div>
      <div class="csub" style="margin-top:10px">${st.attached} attached of ${st.volumes}</div></div>
    <div class="card flat statwide"><div class="ctitle">Per-node disks</div><div class="csub">free of total</div>
      ${(st.disks || []).map(d => `<div class="drow"><div class="dl">${esc(d.node.replace("harvester-", ""))}</div>
        <div class="dv mono nowrap" title="${esc(sizeText(d.avail_gb))} free of ${esc(sizeText(d.cap_gb))}">${esc(sizePair(d.avail_gb, d.cap_gb))}</div></div>`).join("")
        || '<div class="csub" style="margin-top:8px">Longhorn has not reported any node disks yet.</div>'}</div>
  </div>` : ""}
  <div class="card flat pad0"><div class="tblwrap voltable"><table data-sort="volumes" class="tbl dense"><thead><tr>
   <th>Volume</th><th>Attached to</th><th>Health</th><th>Mode</th><th>Usage</th><th data-nosort>Last used</th><th></th>
   </tr></thead><tbody>${rows.map(x => `<tr>
     <td class="volname"><b>${esc(x.pvc_name || x.name.slice(0, 18))}</b>
       <span class="dim xs mono">${esc(x.namespace || "")}${x.node ? ` · ${esc(x.node.replace("harvester-", ""))}` : ""}</span></td>
     <td data-label="Attached to">${attachedWorkloads(x).length
       ? `<div class="attachlist">${attachedWorkloads(x).map(w => `<span class="tag info">${esc(w)}</span>`).join("")}</div>`
       : '<span class="dim">detached</span>'}${x.pod_status ? `<span class="dim xs"> · ${esc(x.pod_status)}</span>` : ""}</td>
     <td data-label="Health" class="volhealth${volumeReason(x) ? " hasreason" : ""}">${x.state === "attached"
       ? `<span class="pill ${x.robustness === "healthy" ? "ok" : x.robustness === "degraded" ? "med" : "crit"}"${x.health_reason ? ` data-tip="${esc(x.health_reason)}"` : ""}>${esc(x.robustness)}</span>`
       // Detached is where a volume sits when nothing is using it - a stopped
       // workload, not a fault. Longhorn calls it detached, so do we.
       : `<span class="pill neutral" data-tip="Nothing is mounting this volume, so Longhorn reports no live replica health">detached</span>`}
       ${volumeReason(x) ? `<span class="dim xs volume-reason">${esc(x.health_reason)}</span>` : ""}</td>
     <td data-label="Mode"><span class="tag">${esc((x.access_modes || ["?"]).map(m => m === "ReadWriteMany" ? "RWX" : m === "ReadWriteOnce" ? "RWO" : m).join(", "))}</span>${x.engine === "v2" ? '<span class="tag info" data-tip="On Longhorn\'s V2 data engine (SPDK)">V2</span>' : ""}
       <span class="dim xs mono">×${x.replicas}</span></td>
     <td data-label="Usage" class="volusage"><div>${meter(x.used_pct || 0)}
       <span class="dim xs mono">${x.actual_gb} / ${x.size_gb} GB</span></div></td>
     <td data-label="Last used" class="small dim">${x.state === "attached" ? '<span class="tag ok">in use</span>' : esc(fmtAgo(x.last_used_secs))}</td>
     <td class="volactions"><div class="row">
       <button class="iconbtn" data-need="operator" data-tip="Resize or change replicas" onclick='volumeEdit(${JSON.stringify(x).replace(/'/g, "&#39;")})'>${icon("edit")}</button>
       <button class="iconbtn" data-need="admin" data-tip="Browse and edit the files on this volume" onclick="volumeFiles('${esc(x.namespace || "lab")}','${esc(x.pvc_name || x.name)}',${x.state === "attached"})">${icon("list")}</button>
       <button class="iconbtn" data-tip="Snapshots and backups of this volume: take one now, or restore" onclick="lhSnaps('${esc(x.name)}','${esc(x.pvc_name || x.name)}')">${icon("snapshot")}</button>
       <button class="iconbtn" data-need="admin" data-tip="Hand this volume's files to the user the container runs as" onclick="volumeChown('${esc(x.namespace || "lab")}','${esc(x.pvc_name || x.name)}')">${icon("shield")}</button>
       <button class="iconbtn danger" data-need="admin" data-tip="Review attachment and data-loss impact before deleting" onclick='volumeDelete(${JSON.stringify(x).replace(/'/g, "&#39;")})'>${icon("trash")}</button>
     </div></td>
      </tr>`).join("") || `<tr><td colspan=7 class="empty">none</td></tr>`}
   </tbody></table></div></div>
  ${storageClassCard(classes, v2)}`);
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
  <div class="row" style="margin-top:16px"><button class="btn pri" onclick="volumeEditNow('${esc(x.namespace || "lab")}','${esc(x.pvc_name || x.name)}')">Save</button><button class="btn" onclick="closeModal()">Cancel</button></div>`);
};
window.volumeEditNow = async (namespace, name) => {
  const body = { namespace, name, size_gb: +$("#ve_size").value, replicas: +$("#ve_reps").value };
  try { await api("/api/volumes/edit", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
    toast(`${name} updated`, "ok"); closeModal(); resetPaint(); viewStorage(); } catch (e) { toast(e.message, "bad"); }
};

function volumeDeleteConfirmationValid(name, value) {
  return String(value || "").trim() === String(name || "");
}
window.volumeDeleteConfirmationValid = volumeDeleteConfirmationValid;

function volumeImpactCount(value, singular, plural) {
  return `<div class="volume-impact"><b>${esc(value)}</b><span>${esc(value === 1 ? singular : plural)}</span></div>`;
}

window.volumeDelete = async x => {
  const namespace = x.namespace || "lab", name = x.pvc_name || x.name;
  modal("Delete volume · " + name,
    '<div class="empty"><span class="spin2"></span> checking mounts, snapshots, backups and reclaim policy…</div>', true);
  let p;
  try {
    p = await api(`/api/volumes/delete-plan?ns=${encodeURIComponent(namespace)}&name=${encodeURIComponent(name)}`);
  } catch (e) {
    return void ($("#mbody").innerHTML = `<div class="note dependency-danger"><b>Impact check failed.</b> ${esc(e.message)}</div>
      <div class="row" style="margin-top:16px"><button class="btn" onclick="closeModal()">Close</button></div>`);
  }
  window.__volumeDeletePlan = p;
  const stale = new Set((p.stale_consumers || []).map(c => `${c.kind}/${c.name}`));
  const mounts = (p.consumers || []).map(c => `<div class="dependency-row ${c.active ? "stranded" : ""}">
    <div><b>${esc(c.kind)} · ${esc(c.name)}</b>${stale.has(`${c.kind}/${c.name}`) ? '<span class="tag">finished</span>' : ""}<div class="dim xs">${esc(c.namespace)} · ${esc(c.detail || (c.active ? "active" : "inactive"))}</div></div>
    <div class="small mono">${(c.mounts || []).map(m => `${esc(m.container)}:${esc(m.path || m.container_kind)}${m.read_only ? " · read-only" : ""}`).join("<br>") || "claim reference"}</div>
  </div>`).join("");
  const lh = p.longhorn || {}, pv = p.pv || {};
  const blocked = p.blocked;
  const permanentBlocked = !p.actions?.delete_data?.enabled;
  const warningRows = (p.warnings || []).map(w => `<li>${esc(w)}</li>`).join("");
  $("#mbody").innerHTML = `
    ${blocked ? `<div class="note dependency-danger"><b>Deletion is blocked.</b><ul>${p.blocking_reasons.map(r => `<li>${esc(r)}</li>`).join("")}</ul></div>`
      : `<div class="note"><b>Impact check passed.</b> This claim is detached and has no active workload references. Choose what should happen to its backing data.</div>`}
    ${(p.removable_jobs || []).length ? `<div class="note"><b>Finished ${(p.removable_jobs || []).length === 1 ? "job" : "jobs"} still referencing this claim.</b>
      <span class="mono">${(p.removable_jobs || []).map(esc).join(", ")}</span> already completed — typically the copy job from an import.
      Homestead removes ${(p.removable_jobs || []).length === 1 ? "it" : "them"} first, because Kubernetes can otherwise hold the claim in Terminating.</div>` : ""}
    <div class="volume-impact-grid">
      ${volumeImpactCount(lh.actual_gb ?? "?", "GB written", "GB written")}
      ${volumeImpactCount(lh.replicas ?? "?", "replica", "replicas")}
      ${volumeImpactCount(p.snapshots?.count ?? "?", "snapshot on volume", "snapshots on volume")}
      ${volumeImpactCount(p.backups?.count ?? "?", "external backup", "external backups")}
    </div>
    <div class="note"><b>Current objects</b><br>
      Claim <span class="mono">${esc(p.namespace)}/${esc(p.name)}</span> · ${esc(p.phase)} · ${esc(p.requested_storage || "size unknown")}<br>
      PV <span class="mono">${esc(pv.name || "not bound")}</span> · reclaim policy <b>${esc(pv.reclaim_policy || "unknown")}</b><br>
      Longhorn <span class="mono">${esc(lh.name || "not found")}</span> · ${esc(lh.state || "unknown")}${lh.attached_node ? ` on ${esc(lh.attached_node)}` : ""}
    </div>
    <div class="sec">1 · Detach</div>
    <div class="note ${p.actions?.detach?.complete ? "" : "dependency-danger"}">
      <b>${p.actions?.detach?.complete ? "Already detached." : "Stop and unmount this claim first."}</b>
      Detaching keeps the PVC and all data. Homestead will not silently rewrite or stop the workloads listed below.
      ${(p.stale_consumers || []).length ? `Entries marked <i>finished</i> have stopped for good and do not hold it.` : ""}
    </div>
    ${mounts ? `<div class="dependency-list" style="margin-top:10px">${mounts}</div>` : '<div class="empty small">No pods, controllers, jobs or virtual machines reference this claim.</div>'}
    <div class="sec">2 · Choose deletion result</div>
    <div class="volume-delete-grid">
      <label class="volume-delete-option ${blocked ? "disabled" : ""}"><input type="radio" name="vd_action" value="delete_claim" onchange="volumeDeleteGate()" ${blocked ? "disabled" : ""}>
        <span><b>Delete claim, keep data</b><small>Homestead changes the PV policy to Retain, then deletes the PVC. The released data needs Kubernetes/Longhorn administration to recover or remove later.</small></span></label>
      <label class="volume-delete-option danger ${permanentBlocked ? "disabled" : ""}"><input type="radio" name="vd_action" value="delete_data" onchange="volumeDeleteGate()" ${permanentBlocked ? "disabled" : ""}>
        <span><b>Permanently delete data</b><small>Homestead changes the PV policy to Delete. The PVC, PV, Longhorn volume, replicas and ${p.snapshots?.count || 0} on-volume snapshot(s) are removed. ${p.backups?.count || 0} external backup(s) remain.</small></span></label>
    </div>
    ${warningRows ? `<div class="note dependency-danger"><b>Incomplete impact inventory</b><ul>${warningRows}</ul></div>` : ""}
    <div class="sec">3 · Confirm</div>
    <div class="f"><label>Type <b class="mono">${esc(name)}</b> to confirm</label>
      <input id="vd_confirm" autocomplete="off" placeholder="${esc(name)}" oninput="volumeDeleteGate()"></div>
    <div class="row"><button id="vd_go" class="btn danger" data-need="admin" disabled
      onclick="volumeDeleteNow('${esc(namespace)}','${esc(name)}','${esc(p.uid)}')">Delete selected</button>
      <button class="btn" onclick="closeModal()">Cancel</button></div>`;
};

window.volumeDeleteGate = () => {
  const p = window.__volumeDeletePlan;
  const action = document.querySelector('input[name="vd_action"]:checked')?.value || "";
  const input = $("#vd_confirm"), button = $("#vd_go");
  if (!p || !input || !button) return;
  const allowed = action && !p.blocked && (action !== "delete_data" || p.actions?.delete_data?.enabled);
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
      body: JSON.stringify({ namespace, name, uid, action, confirmation }) });
    toast(result.message || `${name} deletion started`, "ok"); closeModal(); resetPaint(); viewStorage();
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
window.v2Details = () => {
  const v2 = STATE.data.v2 || { nodes: [] };
  modal("Longhorn V2 data engine", `<p class="muted small">V2 is Longhorn's SPDK engine: lower latency and less CPU per I/O than V1. A V2 volume needs the engine switched on, and every node that holds one of its replicas needs a disk given to Longhorn as a block device and 2 GiB of hugepages.</p>
    <div class="drow"><div class="dl">Engine</div><div class="dv">${v2.enabled ? '<span class="pill ok">on</span>' : '<span class="pill low">off</span>'}</div></div>
    ${v2.harvester_setting !== null && v2.harvester_setting !== undefined ? `<div class="drow"><div class="dl">Harvester setting</div><div class="dv mono small">longhorn-v2-data-engine-enabled = ${v2.harvester_setting}</div></div>` : ""}
    <div class="card flat pad0" style="margin-top:12px"><table class="tbl stack"><thead><tr><th>Node</th><th>V2 disks</th><th>Hugepages</th><th>Ready</th></tr></thead><tbody>
      ${(v2.nodes || []).map(n => `<tr><td><b>${esc(n.name)}</b></td><td class="mono" data-label="V2 disks">${n.block_disks}</td>
        <td class="mono" data-label="Hugepages">${n.hugepages_mb} MiB</td>
        <td data-label="Ready">${n.ready ? '<span class="pill ok">ready</span>' : `<span class="dim xs">needs ${esc(n.missing.join(" and "))}</span>`}</td></tr>`).join("")
        || '<tr><td colspan="4" class="empty">Longhorn reported no nodes</td></tr>'}</tbody></table></div>
    <div class="note" style="margin-top:12px"><b>Turning it on, on Harvester:</b> Advanced → Settings → <span class="mono">longhorn-v2-data-engine-enabled</span>, which reserves the hugepages on every node. Then, per host, Hosts → Edit Config → Storage → Add Disk with the <b>Longhorn V2</b> provisioner, on a disk holding nothing you need. Harvester owns Longhorn's settings, so Homestead reads them rather than changing them.</div>`, true);
};

function storageClassCard(classes, v2 = null) {
  const rows = classes || [];
  if (!rows.length) return "";
  return `<div class="card flat pad0" style="margin-top:18px">
    <div class="between storage-class-head">
      <div><div class="ctitle">Storage classes</div>
        <div class="csub">What a new volume is built from. Kubernetes fixes a class at creation, so Homestead creates and removes them rather than editing them in place.</div>
        ${v2Summary(v2, rows)}</div>
      <button class="btn pri" data-need="admin" onclick="storageClassCreate()">＋ New storage class</button></div>
    <div class="tblwrap"><table data-sort="storage-classes" class="tbl stack storage-class-table"><thead><tr>
      <th>Class</th><th>Engine</th><th>Replicas</th><th>Shared (RWX)</th><th>Encryption</th><th>Expansion</th><th>Volumes</th><th></th>
    </tr></thead><tbody>${rows.map(row => `<tr>
      <td><b>${esc(row.name)}</b>${row.default ? '<span class="tag ok">default</span>' : ""}${row.internal ? '<span class="tag">Harvester internal</span>' : ""}
        <div class="dim xs mono">${esc(row.provisioner || "")}</div></td>
      <td data-label="Engine">${row.engine === "v2" ? '<span class="tag info" data-tip="Longhorn V2 (SPDK)">V2</span>' : row.engine ? '<span class="tag">V1</span>' : '<span class="dim">—</span>'}</td>
      <td class="mono" data-label="Replicas">${esc(row.replicas || "—")}</td>
      <td data-label="Shared (RWX)">${row.migratable
        ? '<span class="pill low" data-tip="This class creates live-migratable volumes for VM disks. Longhorn cannot mount those into a pod, so it cannot back shared storage.">VM disks only</span>'
        : '<span class="pill ok">usable</span>'}</td>
      <td data-label="Encryption">${row.encrypted ? '<span class="tag info">encrypted</span>' : '<span class="dim">—</span>'}</td>
      <td data-label="Expansion">${row.expandable ? '<span class="tag ok">can grow</span>' : '<span class="tag">fixed size</span>'}</td>
      <td class="mono" data-label="Volumes">${row.in_use ?? 0}</td>
      <td><div class="row" style="gap:6px;flex-wrap:nowrap">
        ${row.default || row.internal ? "" : `<button class="btn sm" data-need="admin" title="Use this class when nothing else is chosen" onclick="storageClassDefault('${esc(row.name)}')">Make default</button>`}
        ${row.internal || row.default || row.in_use ? "" : `<button class="btn sm danger" data-need="admin" onclick="storageClassDelete('${esc(row.name)}')">${icon("trash")}Delete</button>`}
      </div></td></tr>`).join("")}</tbody></table></div></div>`;
}
window.storageClassCreate = () => {
  modal("New storage class", `
    <p class="muted small">A storage class is a recipe Longhorn follows when it creates a volume:
      how many replicas to keep, whether the volume can grow, and what happens to the data when its
      claim is deleted. Kubernetes will not let those settings change afterwards, so choose them now.</p>
    <div class="f" style="margin-top:14px"><label>Name</label>
      <input type="text" id="sc_name" placeholder="longhorn-r3" autocomplete="off"></div>
    <div class="f2"><div class="f"><label>Replicas ${tip("Copies Longhorn keeps on separate disks. Two survives one disk or node loss; one has no redundancy.")}</label>
      <input type="number" id="sc_reps" min="1" max="5" value="2"></div>
      <div class="f"><label>Reclaim policy ${tip("Delete removes the Longhorn volume with its claim. Retain keeps the data behind after the claim is gone.")}</label>
        <select id="sc_reclaim"><option>Delete</option><option>Retain</option></select></div></div>
    <div class="f"><label>Data engine ${tip("V1 is Longhorn's long-standing engine. V2 (SPDK) is faster and needs the engine switched on, a V2 disk and hugepages on the nodes.")}</label>
      <select id="sc_engine" onchange="storageClassEngine()"><option value="v1">V1 · the default</option><option value="v2">V2 · SPDK</option></select>
      <div class="note" id="sc_engine_note" hidden></div></div>
    <label class="switch"><input type="checkbox" id="sc_expand" checked> Allow volumes to grow later</label>
    <label class="switch"><input type="checkbox" id="sc_migratable" onchange="storageClassHint()"> Live-migratable · for VM disks</label>
    <div class="note" id="sc_hint">Leave migratable off for container storage: a migratable volume gets a second controller so a VM can move between hosts, and Longhorn refuses to mount that kind into a pod — which is what breaks ReadWriteMany.</div>
    <label class="switch"><input type="checkbox" id="sc_default"> Make this the default class</label>
    <div class="row" style="margin-top:18px"><button class="btn pri" id="sc_go" data-need="admin" onclick="storageClassSave(this)">Create class</button>
      <button class="btn" onclick="closeModal()">Cancel</button></div>`);
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
      body: JSON.stringify({ name, replicas: +$("#sc_reps").value, reclaim_policy: $("#sc_reclaim").value,
        expandable: $("#sc_expand").checked, migratable: $("#sc_migratable").checked,
        engine: $("#sc_engine").value, default: $("#sc_default").checked }) });
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
  if (!confirm(`Delete storage class "${name}"?

Volumes already built from it keep working and keep their data. New volumes can no longer use it.`)) return;
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
  if (attached && !confirm(`${pvc} is attached to a running workload.\n\nA ReadWriteOnce volume can only mount in one place, so the browser will not start until the workload is stopped. Continue anyway?`)) {
    return closeModal();
  }
  fileBrowse("");
};

window.fileBrowse = async (path) => {
  const { namespace, pvc } = FILEVIEW;
  if (FILEVIEW.dirty && !confirm("Discard unsaved changes?")) return;
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
    crumbs.push(`<span class="dim">/</span><button class="linkish" onclick="fileBrowse('${esc(upto)}')">${esc(part)}</button>`);
  });
  return crumbs.join("");
}

function fileBrowserMarkup(listing) {
  const parent = String(listing.path || "").split("/").slice(0, -1).join("/");
  return `<div class="filecrumbs">${fileCrumbs(listing.path)}</div>
    <div class="filelist">
      ${listing.path ? `<button class="filerow" onclick="fileBrowse('${esc(parent)}')"><span class="fileicon">↩</span><span>..</span><span class="dim xs">up one level</span></button>` : ""}
      ${listing.entries.map(entry => {
        const full = (listing.path ? listing.path + "/" : "") + entry.name;
        return entry.kind === "dir"
          ? `<button class="filerow" onclick="fileBrowse('${esc(full)}')"><span class="fileicon">▸</span><span>${esc(entry.name)}</span><span class="dim xs">folder</span></button>`
          : `<button class="filerow" ${entry.editable ? `onclick="fileOpen('${esc(full)}')"` : "disabled"}><span class="fileicon">·</span><span>${esc(entry.name)}</span><span class="dim xs">${fileSize(entry.size)}${entry.editable ? "" : " · too large to edit"}</span></button>`;
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
        <button class="btn" onclick="fileBrowse('${esc(FILEVIEW.path)}')">Back</button>
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
    if (/^(JSON is invalid|YAML cannot)/.test(e.message) && confirm(`${e.message}\n\nSave it anyway?`)) {
      return fileSave(true);
    }
    toast(e.message, "bad");
  } finally {
    if (button) { button.disabled = false; button.textContent = "Save"; }
  }
};

window.closeFiles = async () => {
  const { namespace, pvc, dirty } = FILEVIEW;
  if (dirty && !confirm("Discard unsaved changes?")) return;
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
      <button class="btn pri" data-need="admin" onclick="volumeChownNow('${esc(namespace)}','${esc(name)}',this)">Set ownership</button>
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
  const sh = await api("/api/shares").catch(() => []);
  STATE.data.shares = sh;
  if (!STATE.data.ov) STATE.data.ov = await api("/api/overview").catch(() => ({}));
  const ip = STATE.data.ov.lb_ip || "server";
  paint(`<div class="phead"><div><h2>Network shares</h2>
    <p>SMB shares backed by replicated Longhorn volumes — mount them straight from Windows</p></div>
    <div class="row"><button class="btn pri" data-need="admin" onclick="newShare()">＋ New share</button></div></div>
  <div class="card flat pad0"><div class="tblwrap sharetable"><table data-sort="shares" class="tbl">
    <thead><tr><th>Share</th><th>Storage</th><th>Size</th><th>Access</th><th>UNC path</th><th></th></tr></thead><tbody>
    ${sh.map(s => `<tr><td class="shareidentity"><div class="row" style="gap:9px"><div class="av n2">${esc(s.name.slice(0, 2).toUpperCase())}</div>
      <div><b>${esc(s.name)}</b><div class="dim xs">${esc(s.created || "")}</div></div></div></td>
      <td class="small mono" data-label="Storage"><span class="sharevol">${esc(s.pvc || "—")}${s.sub_path ? `<span class="dim">/${esc(s.sub_path)}</span>` : ""}</span>
        ${s.owned === false ? '<span class="tag">shared volume</span>' : ""}</td>
      <td class="mono" data-label="Size">${s.size_gb ? s.size_gb + " GB" : "—"}</td>
      <td data-label="Access"><span>${s.public ? '<span class="pill med">guest</span>' : `<span class="pill low">${esc(s.user)}</span>`}
        ${s.read_only ? '<span class="tag">read only</span>' : '<span class="tag">read/write</span>'}</span></td>
      <td class="small muted mono" data-label="UNC path">\\\\${esc(ip)}\\${esc(s.name)}</td>
      <td class="shareactions"><div class="row"><button class="btn sm" data-need="admin" title="Grow this share or change its access policy" onclick="editShare('${esc(s.name)}')">${icon("edit")}Edit</button>
        <button class="btn sm danger" data-need="admin" onclick="rmShare('${esc(s.name)}')">${icon("trash")}Remove</button></div></td></tr>`).join("")
      || `<tr><td colspan=6 class="empty">no shares yet — create one with ＋ New share</td></tr>`}</tbody></table></div></div>`);
}
window.newShare = async () => {
  modal("New share", `<div class="empty"><span class="spin2"></span>loading volumes</div>`, true);
  const options = await api("/api/shares/options").catch(() => ({ pvcs: [], storage_classes: [] }));
  $("#mbody").innerHTML = `
    <div class="f"><label>Share name ${tip("Windows sees this name after the server address, and Homestead mounts it at /shares/<name> inside Samba.")}</label>
      <input type="text" id="sh_name" placeholder="media" autocomplete="off"></div>
    <div class="sec">Storage</div>
    <div class="note storage-guide"><b>Where the files live:</b> a new Longhorn volume is created for this share alone, or pick a volume that already exists — including one a container uses — and optionally share just a folder inside it. A ReadWriteOnce volume can only attach on one node, so Samba and the workload that owns it must run on the same host.</div>
    <div id="sh_storage"></div>
    <div class="f2" style="margin-top:14px"><div class="f"><label>Username</label><input type="text" id="sh_user" value="lab" autocomplete="username" oninput="shareAccountHint()"></div>
      <div class="f"><label>Password</label><input type="password" id="sh_pass" autocomplete="new-password" placeholder="Required unless guest access is enabled">
        <span class="dim xs" id="sh_account"></span></div></div>
    <label class="switch"><input type="checkbox" id="sh_pub"> Allow guest access</label>
    <label class="switch"><input type="checkbox" id="sh_ro"> Read only</label>
    <div class="row" style="margin-top:18px"><button class="btn pri" id="sh_go" data-need="admin" onclick="mkShare(this)">Create share</button>
      <button class="btn" onclick="closeModal()">Cancel</button></div>
    <div class="note" style="margin-top:14px">Creating a share restarts Samba, so open SMB sessions drop briefly.</div>`;
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
  });
  renderVolumeRows(host, [{ kind: "new-rwo", path: "", source: "", size_gb: 10,
    label: "Share storage" }]);
  shareAccountHint();
};
window.shareAccountHint = () => {
  const user = $("#sh_user")?.value.trim(), hint = $("#sh_account"), field = $("#sh_pass");
  if (!hint || !field) return;
  // Samba keeps one password per account, and Homestead never shows it back,
  // so an account that already has one must not have to be retyped.
  const known = (STATE.data.shares || []).filter(row => !row.public && row.user === user && row.has_password);
  hint.textContent = known.length
    ? `${user} already has a password from ${known.map(row => row.name).join(", ")}. Leave this blank to reuse it, or type a new one to change it for every share using ${user}.`
    : "";
  field.placeholder = known.length ? `Leave blank to reuse the ${user} password`
    : "Required unless guest access is enabled";
};
window.mkShare = async button => {
  const name = $("#sh_name").value.trim();
  if (!/^[a-z0-9-]{2,30}$/.test(name)) return toast("lowercase letters, numbers and dashes only", "bad");
  const account = $("#sh_user").value.trim();
  const known = (STATE.data.shares || []).some(row => !row.public && row.user === account && row.has_password);
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
  const body = { name, user: $("#sh_user").value.trim(), password: $("#sh_pass").value,
    public: $("#sh_pub").checked, read_only: $("#sh_ro").checked, sub_path: folder };
  if (reusing) body.pvc = storage.source;
  else Object.assign(body, { pvc: storage.source || "", size_gb: storage.size_gb,
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
  const guest = $("#she_pub")?.checked;
  $("#she_private")?.classList.toggle("hidden", guest);
};
window.editShare = name => {
  const s = (STATE.data.shares || []).find(row => row.name === name);
  if (!s) return toast("share details are no longer available; refresh and try again", "bad");
  modal(`Edit share · ${s.name}`, `
    <div class="callout"><b>\\\\${esc((STATE.data.ov && STATE.data.ov.lb_ip) || "server")}\\${esc(s.name)}</b><br>
      ${s.owned === false ? `Files come from <span class="mono">${esc(s.pvc || "")}${s.sub_path ? "/" + esc(s.sub_path) : ""}</span>, a volume this share only mounts.` : "The claim can grow but cannot shrink."}
      Size-only changes keep Samba running; access changes briefly disconnect open SMB sessions.</div>
    <div class="f2" style="margin-top:14px"><div class="f"><label>Requested size (GB) ${tip("Longhorn volumes can grow online. Kubernetes and Longhorn do not support shrinking a populated claim.")}</label>
      <input type="number" id="she_size" min="${esc(s.size_gb || 1)}" value="${esc(s.size_gb || 1)}" ${s.owned === false ? "disabled" : ""}>
      ${s.owned === false ? `<span class="dim xs">Sized with <span class="mono">${esc(s.pvc || "its volume")}</span> in Volumes, because this share borrows it.</span>` : ""}</div>
      <div class="f"><label>Access</label><select id="she_access" onchange="shareAccessToggle()">
        <option value="private" ${s.public ? "" : "selected"}>Private · username and password</option>
        <option value="guest" ${s.public ? "selected" : ""}>Guest · no sign-in</option></select></div></div>
    <div id="she_private" class="${s.public ? "hidden" : ""}"><div class="f"><label>Username</label>
      <input type="text" id="she_user" value="${esc(s.user || "lab")}" autocomplete="username"></div>
      <div class="f"><label>New password ${tip("Leave blank to keep the existing password. Passwords are stored in a Kubernetes Secret and are never returned to the browser.")}</label>
        <input type="password" id="she_pass" autocomplete="new-password" placeholder="${s.has_password ? "Leave blank to keep current password" : "Required for private access"}">
        ${shareAccountSiblings(s).length ? `<span class="dim xs">${esc(s.user)} is also used by ${esc(shareAccountSiblings(s).join(", "))}. Samba keeps one password per account, so a new one changes those too.</span>` : ""}</div></div>
    <label class="switch"><input type="checkbox" id="she_ro" ${s.read_only ? "checked" : ""}> Read only · clients can browse and download but cannot change files</label>
    <div class="row" style="margin-top:18px"><button class="btn pri" data-need="admin" onclick="saveShareEdit('${esc(s.name)}',this)">${icon("edit")}Save changes</button>
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
  if (!confirm(`Remove share "${name}" from samba?\n\nThe Longhorn volume and its data are kept.`)) return;
  try {
    await api("/api/shares/delete", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name }) });
    toast(`share "${name}" removed`, "ok"); resetPaint(); viewShares();
  } catch (e) { toast(e.message, "bad"); }
};

/* ---------------- events ---------------- */
async function viewEvents() {
  const e = await api("/api/events");
  const q = STATE.q.toLowerCase();
  // The count says what the table shows, so the warnings filter counts too.
  const rows = e.filter(x => (!q || [x.obj, x.ns, x.kind, x.reason, x.msg].join(" ").toLowerCase().includes(q))
    && (!STATE.eventWarnings || x.type === "Warning"));
  paint(`<div class="phead"><div><h2>Events</h2><p>${rows.length} of ${e.length} recent events · newest first</p></div>
    <div class="row"><button class="btn sm ${STATE.eventWarnings ? "pri" : ""}" onclick="STATE.eventWarnings=!STATE.eventWarnings;viewEvents()">Warnings only</button></div></div>
  <div class="card flat pad0 eventtable"><div class="tblwrap"><table data-sort="events" class="tbl stack dense"><thead><tr>
    <th>Object</th><th>Reason</th><th>Message</th><th data-nosort>When</th></tr></thead><tbody>
  ${rows.map(x => `<tr><td><b>${esc(x.obj)}</b><div class="dim xs">${esc(x.ns)} · ${esc(x.kind)}</div></td>
    <td><span class="pill ${x.type === "Warning" ? "med" : "low"}">${esc(x.reason)}</span></td>
    <td class="small muted">${esc(x.msg)}${x.count > 1 ? ` <span class="tag">×${x.count}</span>` : ""}</td>
    <td class="dim xs mono" title="${esc((x.time || "").replace("T", " ").replace("Z", ""))}">${esc(fmtAgo(ageSecs(x.time)))}</td></tr>`).join("")
    || `<tr><td colspan=4 class="empty">no events</td></tr>`}</tbody></table></div></div>`);
}
