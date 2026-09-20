/* Architecture, Volumes, Shares, Events */

const ROB = r => r === "healthy" ? "#3ddc91" : r === "degraded" ? "#ffb020" : "#ff4d4f";

async function viewFlow() {
  const [f] = await Promise.all([api("/api/flow"), loadHardwareFeatures()]);
  STATE.data.flow = f;
  const links = [];
  f.nodes.forEach(n => n.copies.forEach(c => links.push([n.id + "|" + c.vid, c.vid, "#a78bfa"])));
  f.workloads.forEach(w => w.claims.forEach(c => { if (c.vid) links.push([c.vid, w.id, "#7dd3fc"]); }));
  f.workloads.forEach(w => w.ports.forEach(p => { if (p.vip) links.push([w.id, "i:" + p.vip, "#ffb020"]); }));
  STATE.data.alinks = links;

  paint(`<div class="phead">
      <div><h2>Architecture</h2><p>Where every replica lives, what mounts it, and how it is reached</p></div>
      <div class="row hide-sm">
        <span class="tag"><span class="dt" style="background:#a78bfa"></span>replica copy</span>
        <span class="tag"><span class="dt" style="background:#7dd3fc"></span>mount</span>
        <span class="tag"><span class="dt" style="background:#ffb020"></span>port</span>
      </div></div>
    <div class="flowwrap topdown"><svg id="archsvg"></svg><div class="arch topdown">

      <div class="acol"><h4>Nodes &amp; replica copies</h4>
      ${f.nodes.map(n => `<div class="abox" data-id="${esc(n.id)}">
        <div class="ahd"><div class="av n2">${esc(n.name.replace(/[^0-9a-z]/gi, "").slice(-2).toUpperCase())}</div>
          <div class="anm">${esc(n.name)}</div><span class="badge">${n.copies.length}</span></div>
        <div class="copies">${n.copies.map(c => `<span class="copy" data-tgt="${esc(c.vid)}" id="${esc(n.id + "|" + c.vid)}">
          <span class="cd" style="background:${c.running ? "#3ddc91" : "#ffb020"}"></span>${esc(c.vol)}</span>`).join("")
          || '<span class="dim xs">no replicas here</span>'}</div></div>`).join("")}
      </div>

      <div class="acol"><h4>Longhorn volumes</h4>
      ${f.volumes.map(v => `<div class="abox" data-id="${esc(v.id)}" id="${esc(v.id)}">
        <div class="ahd"><span class="cd" style="width:7px;height:7px;border-radius:50%;background:${ROB(v.robustness)}"></span>
          <div class="anm">${esc(v.name)}</div><span class="badge">${v.replicas}×</span></div>
        <div class="row" style="gap:6px">
          <span class="tag">${v.size_gb}G</span>
          <span class="tag ${v.robustness === "healthy" ? "ok" : v.robustness === "degraded" ? "warn" : "bad"}">${esc(v.robustness)}</span>
          ${v.attached ? `<span class="tag info">on ${esc(v.attached.replace("harvester-", ""))}</span>` : ""}
        </div></div>`).join("") || '<div class="dim xs" style="text-align:center">no volumes</div>'}
      </div>

      <div class="acol"><h4>Workloads &amp; their ports</h4>
      ${f.workloads.map(w => `<div class="abox" data-id="${esc(w.id)}" id="${esc(w.id)}">
        <div class="ahd">${appAvatar(w.name, w.icon)}
          <div style="flex:1;min-width:0"><div class="anm">${esc(w.name)}</div>
            <div class="dim xs">${esc(w.kind === "vm" ? "virtual machine" : w.node)}</div></div>
          ${hardwareTags(w.hardware || (w.gpu ? ["igpu"] : []))}
          ${w.kind === "vm"
            ? `<button class="btn sm" data-need="operator" title="Migrate"
                 onclick="event.stopPropagation();vmMove('${esc(w.ns || "lab")}','${esc(w.name)}')">⇄</button>`
            : `<button class="btn sm" data-need="operator" title="Move host"
                 onclick="event.stopPropagation();moveWorkload('${esc(w.name)}','${esc(w.ns || "lab")}')">⇄</button>`}</div>
        <div class="row" style="gap:8px;justify-content:space-between">
          ${w.uptime ? upChip(w.uptime) : '<span class="dim xs">—</span>'}
          <span class="dim xs mono" title="Live CPU usage; 100% equals one CPU core">${workloadCpuPercent(w.cpu)} CPU · ${w.mem_mb || 0}MB</span></div>
        <div class="livebar"><span style="width:${Math.min(100, Number(w.cpu || 0) * 100)}%"></span></div>
        ${w.claims.length ? `<div class="copies" style="margin-top:9px">${w.claims.map(c => `<span class="mchip">▤ ${esc(c.pvc)}</span>`).join("")}</div>` : ""}
        ${w.ports.length ? `<div class="portchips">${w.ports.map(p => p.vip
              ? `<span class="plink" onclick="event.stopPropagation();openSvc('${esc(p.vip)}',${p.port})">${esc(p.name)}:${p.port}<svg class="ext" width="9" height="9"><use href="#i-ext"/></svg></span>`
              : `<span class="pchip">${esc(p.name)}:${p.port}</span>`).join("")}</div>`
          : '<div class="portchips"><span class="dim xs">no exposed ports</span></div>'}
      </div>`).join("")}
      </div>

      <div class="acol"><h4>Access</h4>
      ${f.vips.map(v => `<div class="abox vipbox" data-id="${esc(v.id)}" id="${esc(v.id)}">
        <div class="dim xs" style="margin-bottom:5px">VIRTUAL IP</div>
        <div class="vipip">${esc(v.ip)}</div>
        <div class="portchips" style="justify-content:center">
          ${v.ports.map(p => `<span class="plink" title="${esc(p.app)}" onclick="event.stopPropagation();openSvc('${esc(v.ip)}',${p.port})">${p.port}<svg class="ext" width="9" height="9"><use href="#i-ext"/></svg></span>`).join("")}</div>
        <div class="dim xs" style="margin-top:8px">${v.ports.length} port${v.ports.length === 1 ? "" : "s"} · kube-vip</div>
      </div>`).join("") || '<div class="dim xs" style="text-align:center">no load balancer IPs</div>'}
      </div>
    </div></div>`);
  requestAnimationFrame(() => drawArch());
  $$(".abox").forEach(b => {
    b.onmouseenter = () => archHighlight(b.dataset.id);
    b.onmouseleave = () => { $$(".abox,.copy").forEach(x => x.classList.remove("dimmed", "sel")); drawArch(); };
  });
}

function drawArch(keep) {
  const svg = $("#archsvg"), wrap = $(".flowwrap");
  if (!svg || !wrap) return;
  const R = wrap.getBoundingClientRect();
  svg.setAttribute("viewBox", `0 0 ${R.width} ${R.height}`);
  const P = id => {
    const el = document.getElementById(id); if (!el) return null;
    const b = el.getBoundingClientRect();
    return { l: b.left - R.left, r: b.right - R.left, t: b.top - R.top, b: b.bottom - R.top,
      x: b.left - R.left + b.width / 2, y: b.top - R.top + b.height / 2 };
  };
  const anim = SET.motion !== "off";
  svg.innerHTML = (STATE.data.alinks || []).map(([a, b, col]) => {
    const A = P(a), B = P(b); if (!A || !B) return "";
    const on = !keep || (keep.has(a) && keep.has(b));
    const topdown = $(".arch")?.classList.contains("topdown");
    const cy = (A.b + B.t) / 2;
    const cx = (A.r + B.l) / 2;
    const d = topdown
      ? `M ${A.x} ${A.t} C ${A.x} ${cy} ${B.x} ${cy} ${B.x} ${B.b}`
      : `M ${A.r} ${A.y} C ${cx} ${A.y} ${cx} ${B.y} ${B.l} ${B.y}`;
    return `<path d="${d}" style="stroke:${col};opacity:${on ? .28 : .04};stroke-width:${on ? 1.4 : 1}"/>` +
      (anim && on ? `<path class="flowline glowpath" d="${d}" style="stroke:${col};opacity:.8;stroke-width:1.6;
        animation-duration:${(1.1 + Math.random() * .9).toFixed(2)}s"/>` : "");
  }).join("");
}

function archHighlight(id) {
  const links = STATE.data.alinks || [];
  const keep = new Set([id]);
  let grow = true;
  while (grow) {
    grow = false;
    links.forEach(([a, b]) => {
      if (keep.has(a) && !keep.has(b)) { keep.add(b); grow = true; }
      if (keep.has(b) && !keep.has(a)) { keep.add(a); grow = true; }
    });
    [...keep].forEach(k => {
      if (k.includes("|")) { const n = k.split("|")[0]; if (!keep.has(n)) { keep.add(n); grow = true; } }
    });
  }
  drawArch(keep);
  $$(".abox").forEach(x => {
    const rel = keep.has(x.dataset.id) || [...keep].some(k => k.startsWith(x.dataset.id + "|"));
    x.classList.toggle("dimmed", !rel);
    x.classList.toggle("sel", x.dataset.id === id);
  });
  $$(".copy").forEach(c => c.classList.toggle("dimmed", !keep.has(c.id)));
}

/* ---------------- volumes ---------------- */
async function viewStorage() {
  const [v, st] = await Promise.all([api("/api/volumes"), api("/api/storage").catch(() => null)]);
  STATE.data.vols = v;
  const q = STATE.q.toLowerCase();
  const rows = v.filter(x => !q || x.name.includes(q) || (x.node || "").includes(q) ||
    (x.pvc_name || "").includes(q) || (x.attached_to || "").toLowerCase().includes(q));
  paint(`<div class="phead"><div><h2>Volumes</h2>
      <p>${v.length} Longhorn volume${v.length === 1 ? "" : "s"} · replicated block storage</p></div>
      <button class="btn pri" data-need="operator" onclick="volumeCreate()">＋ Create volume</button></div>
  ${st ? `<div class="grid g4" style="margin-bottom:18px">
    <div class="card glow g-info"><div class="ctitle">Free space</div>
      <div class="bignum" style="margin-top:8px">${st.avail_gb}<span class="unit">GB</span></div>
      <div class="csub">of ${st.cap_gb} GB raw</div>${meter(st.used_pct, 'style="margin-top:10px"')}</div>
    <div class="card flat"><div class="ctitle">Provisioned</div>
      <div class="bignum" style="margin-top:8px">${st.provisioned_gb}<span class="unit">GB</span></div>
      <div class="csub">${st.actual_gb} GB actually written</div></div>
    <div class="card flat"><div class="ctitle">Replica health</div>
      <div class="row" style="margin-top:10px;gap:8px;flex-wrap:wrap">
        <span class="tag ok">${st.healthy} healthy</span>
        ${st.degraded ? `<span class="tag warn">${st.degraded} degraded</span>` : ""}
        ${st.faulted ? `<span class="tag bad">${st.faulted} faulted</span>` : ""}
        ${st.unknown ? `<span class="tag bad">${st.unknown} unknown · detached</span>` : ""}</div>
      <div class="csub" style="margin-top:10px">${st.attached} attached of ${st.volumes}</div></div>
    <div class="card flat"><div class="ctitle">Per-node disks</div>
      ${st.disks.map(d => `<div class="drow"><div class="dl">${esc(d.node.replace("harvester-", ""))}</div>
        <div class="dv mono">${d.avail_gb}<span class="dim"> / ${d.cap_gb} GB free</span></div></div>`).join("")}</div>
  </div>` : ""}
  <div class="card flat pad0"><div class="tblwrap"><table class="tbl"><thead><tr>
   <th>Volume</th><th>Attached to</th><th>Node</th><th>Health</th><th>Mode</th><th>Replicas</th><th>Usage</th><th>Last used</th><th></th>
   </tr></thead><tbody>${rows.map(x => `<tr>
     <td><b>${esc(x.pvc_name || x.name.slice(0, 18))}</b><div class="dim xs mono">${esc(x.namespace || "")}</div></td>
     <td>${x.attached_to ? `<span class="tag info">${esc(x.attached_to)}</span>` : '<span class="dim">detached</span>'}
         ${x.pod_status ? `<div class="dim xs">${esc(x.pod_status)}</div>` : ""}</td>
     <td class="small">${esc(x.node || "—")}</td>
     <td>${x.state === "attached"
       ? `<span class="pill ${x.robustness === "healthy" ? "ok" : x.robustness === "degraded" ? "med" : "crit"}">${esc(x.robustness)}</span>`
       : `<span class="pill crit" title="Longhorn cannot report live health while this volume is detached">unknown</span>`}</td>
     <td><span class="tag">${esc((x.access_modes || ["?"]).map(m => m === "ReadWriteMany" ? "RWX" : m === "ReadWriteOnce" ? "RWO" : m).join(", "))}</span></td>
     <td class="mono"><b>${x.replicas}</b></td>
     <td style="min-width:130px">${meter(x.used_pct || 0)}<div class="between dim xs mono" style="margin-top:4px"><span>${x.actual_gb} GB</span><span>${x.size_gb} GB</span></div></td>
     <td class="small dim">${x.state === "attached" ? '<span class="tag ok">in use</span>' : esc(fmtAgo(x.last_used_secs))}</td>
     <td><div class="row" style="gap:6px;flex-wrap:nowrap">
       <button class="btn sm" data-need="operator" onclick='volumeEdit(${JSON.stringify(x).replace(/'/g, "&#39;")})'>${icon("edit")}Edit</button>
       <button class="btn sm danger" data-need="admin" title="Review attachment and data-loss impact before deleting" onclick='volumeDelete(${JSON.stringify(x).replace(/'/g, "&#39;")})'>${icon("trash")}Delete</button>
     </div></td>
      </tr>`).join("") || `<tr><td colspan=9 class="empty">none</td></tr>`}
   </tbody></table></div></div>`);
}
window.volumeCreate = async () => {
  const [nss, scs] = await Promise.all([api("/api/namespaces"), api("/api/storageclasses")]);
  modal("Create volume", `<div class="f2"><div class="f"><label>Name</label><input id="vc_name" placeholder="frigate-media"></div>
    <div class="f"><label>Namespace</label><select id="vc_ns">${nss.map(n => `<option ${n === "lab" ? "selected" : ""}>${esc(n)}</option>`).join("")}</select></div></div>
    <div class="f2"><div class="f"><label>Size (GB)</label><input id="vc_size" type="number" min="1" value="10"></div>
    <div class="f"><label>Access mode ${tip("RWO mounts on one node at a time and suits most apps. RWX can mount on several nodes, using Longhorn's shared-volume support.")}</label><select id="vc_mode"><option value="ReadWriteOnce">RWO · one node</option><option value="ReadWriteMany">RWX · many nodes</option></select></div></div>
    <div class="f"><label>Storage class</label><select id="vc_sc">${scs.map(s => `<option ${s === "longhorn-r2" ? "selected" : ""}>${esc(s)}</option>`).join("")}</select></div>
    <div class="row"><button class="btn pri" onclick="volumeCreateNow()">Create volume</button><button class="btn" onclick="closeModal()">Cancel</button></div>`);
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
  const mounts = (p.consumers || []).map(c => `<div class="dependency-row ${c.active ? "stranded" : ""}">
    <div><b>${esc(c.kind)} · ${esc(c.name)}</b><div class="dim xs">${esc(c.namespace)} · ${esc(c.detail || (c.active ? "active" : "inactive"))}</div></div>
    <div class="small mono">${(c.mounts || []).map(m => `${esc(m.container)}:${esc(m.path || m.container_kind)}${m.read_only ? " · read-only" : ""}`).join("<br>") || "claim reference"}</div>
  </div>`).join("");
  const lh = p.longhorn || {}, pv = p.pv || {};
  const blocked = p.blocked;
  const permanentBlocked = !p.actions?.delete_data?.enabled;
  const warningRows = (p.warnings || []).map(w => `<li>${esc(w)}</li>`).join("");
  $("#mbody").innerHTML = `
    ${blocked ? `<div class="note dependency-danger"><b>Deletion is blocked.</b><ul>${p.blocking_reasons.map(r => `<li>${esc(r)}</li>`).join("")}</ul></div>`
      : `<div class="note"><b>Impact check passed.</b> This claim is detached and has no active workload references. Choose what should happen to its backing data.</div>`}
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

/* ---------------- shares ---------------- */
async function viewShares() {
  const sh = await api("/api/shares").catch(() => []);
  STATE.data.shares = sh;
  if (!STATE.data.ov) STATE.data.ov = await api("/api/overview").catch(() => ({}));
  const ip = STATE.data.ov.lb_ip || "server";
  paint(`<div class="phead"><div><h2>Network shares</h2>
    <p>SMB shares backed by replicated Longhorn volumes — mount them straight from Windows</p></div></div>
  <div class="split">
    <div class="card flat pad0"><div class="tblwrap sharetable"><table class="tbl">
      <thead><tr><th>Share</th><th>Size</th><th>Access</th><th>UNC path</th><th></th></tr></thead><tbody>
      ${sh.map(s => `<tr><td class="shareidentity"><div class="row" style="gap:9px"><div class="av n2">${esc(s.name.slice(0, 2).toUpperCase())}</div>
        <div><b>${esc(s.name)}</b><div class="dim xs">${esc(s.created || "")}</div></div></div></td>
        <td class="mono" data-label="Size">${s.size_gb ? s.size_gb + " GB" : "—"}</td>
        <td data-label="Access"><span>${s.public ? '<span class="pill med">guest</span>' : `<span class="pill low">${esc(s.user)}</span>`}
          ${s.read_only ? '<span class="tag">read only</span>' : '<span class="tag">read/write</span>'}</span></td>
        <td class="small muted mono" data-label="UNC path">\\\\${esc(ip)}\\${esc(s.name)}</td>
        <td class="shareactions"><div class="row"><button class="btn sm" data-need="admin" title="Grow this share or change its access policy" onclick="editShare('${esc(s.name)}')">${icon("edit")}Edit</button>
          <button class="btn sm danger" data-need="admin" onclick="rmShare('${esc(s.name)}')">${icon("trash")}Remove</button></div></td></tr>`).join("")
        || `<tr><td colspan=5 class="empty">no shares yet — create one →</td></tr>`}</tbody></table></div></div>
    <div class="card flat"><div class="ctitle">New share</div><div class="csub">Creates a Longhorn volume and adds it to samba</div>
      <div class="f" style="margin-top:16px"><label>Share name</label><input type="text" id="sh_name" placeholder="media"></div>
      <div class="f2"><div class="f"><label>Size (GB)</label><input type="number" id="sh_size" value="10" min="1"></div>
        <div class="f"><label>Username</label><input type="text" id="sh_user" value="lab"></div></div>
      <div class="f"><label>Password</label><input type="password" id="sh_pass" autocomplete="new-password" placeholder="Required unless guest access is enabled"></div>
      <label class="switch"><input type="checkbox" id="sh_pub"> Allow guest access</label>
      <label class="switch"><input type="checkbox" id="sh_ro"> Read only</label>
      <button class="btn pri wide" onclick="mkShare()">Create share</button>
      <div class="dim xs" style="margin-top:12px">Creating or removing a share restarts samba, so open SMB sessions drop briefly.</div></div>
  </div>`);
}
window.mkShare = async () => {
  const name = $("#sh_name").value.trim();
  if (!/^[a-z0-9-]{2,30}$/.test(name)) return toast("lowercase letters, numbers and dashes only", "bad");
  if (!$("#sh_pub").checked && !$("#sh_pass").value) return toast("a password is required for a private share", "bad");
  try {
    await api("/api/shares", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name, size_gb: +$("#sh_size").value, user: $("#sh_user").value.trim(),
        password: $("#sh_pass").value, public: $("#sh_pub").checked, read_only: $("#sh_ro").checked }) });
    toast(`share "${name}" created`, "ok"); resetPaint(); viewShares();
  } catch (e) { toast(e.message, "bad"); }
};
window.shareAccessToggle = () => {
  const guest = $("#she_pub")?.checked;
  $("#she_private")?.classList.toggle("hidden", guest);
};
window.editShare = name => {
  const s = (STATE.data.shares || []).find(row => row.name === name);
  if (!s) return toast("share details are no longer available; refresh and try again", "bad");
  modal(`Edit share · ${s.name}`, `
    <div class="callout"><b>\\\\${esc((STATE.data.ov && STATE.data.ov.lb_ip) || "server")}\\${esc(s.name)}</b><br>
      The claim can grow but cannot shrink. Size-only changes keep Samba running; access changes briefly disconnect open SMB sessions.</div>
    <div class="f2" style="margin-top:14px"><div class="f"><label>Requested size (GB) ${tip("Longhorn volumes can grow online. Kubernetes and Longhorn do not support shrinking a populated claim.")}</label>
      <input type="number" id="she_size" min="${esc(s.size_gb || 1)}" value="${esc(s.size_gb || 1)}"></div>
      <div class="f"><label>Access</label><select id="she_access" onchange="shareAccessToggle()">
        <option value="private" ${s.public ? "" : "selected"}>Private · username and password</option>
        <option value="guest" ${s.public ? "selected" : ""}>Guest · no sign-in</option></select></div></div>
    <div id="she_private" class="${s.public ? "hidden" : ""}"><div class="f"><label>Username</label>
      <input type="text" id="she_user" value="${esc(s.user || "lab")}" autocomplete="username"></div>
      <div class="f"><label>New password ${tip("Leave blank to keep the existing password. Passwords are stored in a Kubernetes Secret and are never returned to the browser.")}</label>
        <input type="password" id="she_pass" autocomplete="new-password" placeholder="${s.has_password ? "Leave blank to keep current password" : "Required for private access"}"></div></div>
    <label class="switch"><input type="checkbox" id="she_ro" ${s.read_only ? "checked" : ""}> Read only · clients can browse and download but cannot change files</label>
    <div class="row" style="margin-top:18px"><button class="btn pri" data-need="admin" onclick="saveShareEdit('${esc(s.name)}',this)">${icon("edit")}Save changes</button>
      <button class="btn" onclick="closeModal()">Cancel</button></div>`);
};
window.saveShareEdit = async (name, button) => {
  const original = (STATE.data.shares || []).find(row => row.name === name);
  if (!original) return toast("share details are no longer available", "bad");
  const size = +$("#she_size").value;
  const publicAccess = $("#she_access").value === "guest";
  const password = $("#she_pass")?.value || "";
  if (!Number.isInteger(size) || size < +(original.size_gb || 1)) return toast(`size must be at least ${original.size_gb || 1} GB`, "bad");
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
  const rows = e.filter(x => !q || [x.obj, x.ns, x.kind, x.reason, x.msg].join(" ").toLowerCase().includes(q));
  paint(`<div class="phead"><div><h2>Events</h2><p>${rows.length} of ${e.length} recent events · newest first</p></div>
    <div class="row"><button class="btn sm ${STATE.eventWarnings ? "pri" : ""}" onclick="STATE.eventWarnings=!STATE.eventWarnings;viewEvents()">Warnings only</button></div></div>
  <div class="card flat pad0 eventtable"><div class="tblwrap"><table class="tbl dense"><thead><tr>
    <th>Object</th><th>Reason</th><th>Message</th><th>When</th></tr></thead><tbody>
  ${rows.filter(x => !STATE.eventWarnings || x.type === "Warning").map(x => `<tr><td><b>${esc(x.obj)}</b><div class="dim xs">${esc(x.ns)} · ${esc(x.kind)}</div></td>
    <td><span class="pill ${x.type === "Warning" ? "med" : "low"}">${esc(x.reason)}</span></td>
    <td class="small muted">${esc(x.msg)}${x.count > 1 ? ` <span class="tag">×${x.count}</span>` : ""}</td>
    <td class="dim xs mono" title="${esc((x.time || "").replace("T", " ").replace("Z", ""))}">${esc(fmtAgo(ageSecs(x.time)))}</td></tr>`).join("")
    || `<tr><td colspan=4 class="empty">no events</td></tr>`}</tbody></table></div></div>`);
}
