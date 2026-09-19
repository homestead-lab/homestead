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
          <span class="dim xs mono">${(w.cpu || 0).toFixed(2)} cores · ${w.mem_mb || 0}MB</span></div>
        <div class="livebar"><span style="width:${Math.min(100, (w.cpu || 0) * 120)}%"></span></div>
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
     <td><button class="btn sm" data-need="operator" onclick='volumeEdit(${JSON.stringify(x).replace(/'/g, "&#39;")})'>Edit</button></td>
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

/* ---------------- shares ---------------- */
async function viewShares() {
  const sh = await api("/api/shares").catch(() => []);
  const ip = (STATE.data.ov && STATE.data.ov.lb_ip) || "";
  paint(`<div class="phead"><div><h2>Network shares</h2>
    <p>SMB shares backed by replicated Longhorn volumes — mount them straight from Windows</p></div></div>
  <div class="split">
    <div class="card flat pad0"><div class="tblwrap"><table class="tbl">
      <thead><tr><th>Share</th><th>Size</th><th>Access</th><th>UNC path</th><th></th></tr></thead><tbody>
      ${sh.map(s => `<tr><td><div class="row" style="gap:9px"><div class="av n2">${esc(s.name.slice(0, 2).toUpperCase())}</div>
        <div><b>${esc(s.name)}</b><div class="dim xs">${esc(s.created || "")}</div></div></div></td>
        <td class="mono">${s.size_gb ? s.size_gb + " GB" : "—"}</td>
        <td>${s.public ? '<span class="pill med">guest</span>' : `<span class="pill low">${esc(s.user)}</span>`}</td>
        <td class="small muted mono">\\\\${esc(ip)}\\${esc(s.name)}</td>
        <td><button class="btn sm danger" onclick="rmShare('${esc(s.name)}')">Remove</button></td></tr>`).join("")
        || `<tr><td colspan=5 class="empty">no shares yet — create one →</td></tr>`}</tbody></table></div></div>
    <div class="card flat"><div class="ctitle">New share</div><div class="csub">Creates a Longhorn volume and adds it to samba</div>
      <div class="f" style="margin-top:16px"><label>Share name</label><input type="text" id="sh_name" placeholder="media"></div>
      <div class="f2"><div class="f"><label>Size (GB)</label><input type="number" id="sh_size" value="10" min="1"></div>
        <div class="f"><label>Username</label><input type="text" id="sh_user" value="lab"></div></div>
      <div class="f"><label>Password</label><input type="text" id="sh_pass" value="LabPass2026"></div>
      <label class="switch"><input type="checkbox" id="sh_pub"> Allow guest access</label>
      <button class="btn pri wide" onclick="mkShare()">Create share</button>
      <div class="dim xs" style="margin-top:12px">Creating or removing a share restarts samba, so open SMB sessions drop briefly.</div></div>
  </div>`);
}
window.mkShare = async () => {
  const name = $("#sh_name").value.trim();
  if (!/^[a-z0-9-]{2,30}$/.test(name)) return toast("lowercase letters, numbers and dashes only", "bad");
  try {
    await api("/api/shares", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name, size_gb: +$("#sh_size").value, user: $("#sh_user").value.trim(),
        password: $("#sh_pass").value, public: $("#sh_pub").checked }) });
    toast(`share "${name}" created`, "ok"); resetPaint(); viewShares();
  } catch (e) { toast(e.message, "bad"); }
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
