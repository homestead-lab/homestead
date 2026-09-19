/* Dashboard, Nodes, node detail modal */

async function viewDash() {
  const [o, hist, st] = await Promise.all([
    api("/api/overview"),
    api("/api/history").catch(() => ({})),
    api("/api/storage").catch(() => null),
  ]);
  STATE.data.ov = o; STATE.data.stor = st;
  const hp = $("#healthPill");
  hp.className = "pill " + (o.health === "healthy" ? "ok" : o.health === "degraded" ? "med" : "crit");
  hp.textContent = o.health.toUpperCase();
  $("#lbinfo").textContent = o.lb_ip ? "VIP " + o.lb_ip : "";
  const sv = $("#setVip"), sn = $("#setNodes");
  if (sv) sv.textContent = o.lb_ip || "—";
  if (sn) sn.textContent = `${o.nodes_ready}/${o.nodes_total}`;

  const H = hist || {};
  const rx = (H.net_rx || []).slice(-40), tx = (H.net_tx || []).slice(-40);
  const netNow = o.nodes.reduce((s, n) => s + (n.rx_mbps || 0), 0);
  const txNow = o.nodes.reduce((s, n) => s + (n.tx_mbps || 0), 0);
  const diskUsed = o.nodes.reduce((s, n) => s + (n.fs_used_gb || 0), 0);
  const diskCap = o.nodes.reduce((s, n) => s + (n.fs_cap_gb || 0), 0);

  const storParts = st ? [
    { n: "Healthy", v: st.healthy, c: "#3ddc91" },
    { n: "Degraded", v: st.degraded, c: "#ffb020" },
    { n: "Faulted", v: st.faulted, c: "#ff4d4f" },
  ].filter(p => p.v) : [];

  paint(`
  <div class="phead">
    <div><h2>Cluster overview</h2><p>Live health, capacity and placement across ${o.nodes_total} node${o.nodes_total > 1 ? "s" : ""}</p></div>
    <div class="row hide-sm">
      <button class="btn" onclick="go('flow')">Architecture</button>
      <button class="btn pri" onclick="go('deploy')">＋ Deploy</button>
    </div>
  </div>

  <div class="grid g3 stagger">
    <div class="card glow ${gcls(Math.max(o.cpu_pct, o.mem_pct))}">
      <div class="between"><div><div class="ctitle">Compute</div>
        <div class="csub">CPU and memory across the cluster</div></div>${trend(H.cpu)}</div>
      ${dualSpark((H.cpu || []).slice(-40), (H.mem || []).slice(-40))}
      <div class="row" style="gap:26px;margin-top:12px">
        <div><div class="bignum">${o.cpu_pct}<span class="unit">%</span></div>
          <div class="csub"><span class="kdot s1"></span>CPU · ${o.cpu_used}/${o.cpu_cap} cores</div></div>
        <div><div class="bignum">${o.mem_pct}<span class="unit">%</span></div>
          <div class="csub"><span class="kdot s2"></span>RAM · ${o.mem_used_gb}/${o.mem_cap_gb} GB</div></div>
      </div>
    </div>

    <div class="card glow g-info">
      <div class="between"><div><div class="ctitle">Throughput</div>
        <div class="csub">Network and local disk</div></div>${trend(rx)}</div>
      ${dualSpark(rx, tx)}
      <div class="row" style="gap:26px;margin-top:12px">
        <div><div class="bignum">${netNow.toFixed(1)}<span class="unit">Mb/s</span></div>
          <div class="csub"><span class="kdot s1"></span>in · ${txNow.toFixed(1)} out</div></div>
        <div><div class="midnum">${diskUsed.toFixed(0)}<span class="unit">GB</span></div>
          <div class="csub">node disk of ${diskCap.toFixed(0)} GB</div>
          ${meter(diskCap ? diskUsed / diskCap * 100 : 0)}</div>
      </div>
    </div>

    <div class="card flat">
      <div class="ctitle">Storage</div><div class="csub">Longhorn capacity and replica health</div>
      ${st ? `
      <div class="row" style="margin-top:12px;justify-content:center">
        ${segDonut(storParts, st.volumes, "volumes", 150)}</div>
      <div class="drow"><div class="dl">Free</div><div class="dv mono">${st.avail_gb} GB</div></div>
      <div class="drow"><div class="dl">Used</div><div class="dv mono">${st.used_gb} / ${st.cap_gb} GB</div></div>
      <div class="drow"><div class="dl">Provisioned</div><div class="dv mono">${st.provisioned_gb} GB</div></div>
      <div style="margin-top:10px">${meter(st.used_pct)}
        <div class="csub" style="margin-top:6px">${st.used_pct}% of raw capacity used
          ${st.degraded || st.faulted ? `· <span class="tag ${st.faulted ? "bad" : "warn"}">${st.degraded + st.faulted} unhealthy</span>` : `· <span class="tag ok">all healthy</span>`}</div></div>`
      : '<div class="empty">storage data unavailable</div>'}
    </div>
  </div>

  <div class="sec">Node health <span class="dim xs" style="text-transform:none;letter-spacing:0">— click a node for detail</span></div>
  <div class="nodegrid stagger">${o.nodes.map(nodeCard).join("")}</div>

  <div class="sec">Top consumers</div>
  <div class="grid g2">
    <div class="card flat pad0">
      <div class="cardhd"><div class="ctitle">Top CPU</div></div>
      <div class="tblwrap"><table class="tbl"><thead><tr><th>Workload</th><th>Node</th><th style="width:150px">CPU</th></tr></thead><tbody>
      ${o.top_cpu.slice(0, 5).map(w => `<tr><td><b>${esc(w.name)}</b><div class="dim xs">${esc(w.ns)}</div></td>
        <td class="small muted">${esc(w.nodes.join(", ") || "—")}</td>
        <td>${meter(Math.min(100, w.cpu * 100))}<div class="dim xs mono" style="margin-top:4px">${w.cpu} cores</div></td></tr>`).join("")}
      </tbody></table></div></div>
    <div class="card flat pad0">
      <div class="cardhd"><div class="ctitle">Top memory</div></div>
      <div class="tblwrap"><table class="tbl"><thead><tr><th>Workload</th><th>Node</th><th style="width:120px">RAM</th></tr></thead><tbody>
      ${o.top_mem.slice(0, 5).map(w => `<tr><td><b>${esc(w.name)}</b><div class="dim xs">${esc(w.ns)}</div></td>
        <td class="small muted">${esc(w.nodes.join(", ") || "—")}</td>
        <td class="mono"><b>${w.mem_mb}</b> <span class="dim xs">MB</span></td></tr>`).join("")}
      </tbody></table></div></div>
  </div>`);
}

function nodeCard(n) {
  const dots = "<i></i>".repeat(Math.min(n.pods_sys, 80)) + '<i class="wl"></i>'.repeat(Math.min(n.pods_wl, 40));
  const bad = n.status !== "Ready";
  return `<div class="card glow ${bad ? "g-bad" : gcls(Math.max(n.cpu_pct, n.mem_pct))} clickable"
       onclick="nodeDetail('${esc(n.name)}')">
    <div class="between">
      <div class="row" style="gap:10px">
        <div class="av n2">${esc(n.name.replace(/[^0-9a-z]/gi, "").slice(-2).toUpperCase())}</div>
        <div><div style="font-weight:680">${esc(n.name)}</div>
          <div class="dim xs">${n.roles.join(" · ")}</div></div>
      </div>
      <span class="pill ${bad ? "crit" : "low"}">${n.status}</span>
    </div>
    <div class="row" style="margin-top:16px;gap:14px;align-items:flex-start">
      <div style="flex:1;min-width:0">
        <div class="between"><span class="dim xs">CPU</span>
          <span class="small mono"><b>${n.cpu_pct}%</b> <span class="dim">of ${n.cpu_cap}</span></span></div>
        ${meter(n.cpu_pct, 'style="margin:5px 0 11px"')}
        <div class="between"><span class="dim xs">MEMORY</span>
          <span class="small mono"><b>${n.mem_pct}%</b> <span class="dim">${n.mem_used_gb}/${n.mem_cap_gb}G</span></span></div>
        ${meter(n.mem_pct, 'style="margin:5px 0 11px"')}
        <div class="between"><span class="dim xs">NETWORK</span>
          <span class="small mono">↓${(n.rx_mbps || 0).toFixed(1)} ↑${(n.tx_mbps || 0).toFixed(1)} <span class="dim">Mb/s</span></span></div>
      </div>
      <div style="text-align:right">
        <div class="midnum">${n.pods}</div><div class="dim xs">pods</div>
        ${n.vms ? `<div class="midnum" style="margin-top:8px">${n.vms}</div><div class="dim xs">VMs</div>` : ""}
      </div>
    </div>
    <div class="podgrid">${dots}</div>
    <div style="margin-top:12px">
      ${n.igpu ? '<span class="tag gpu"><span class="dt"></span>iGPU</span>' : ""}
      ${n.fs_pct ? `<span class="tag">disk ${n.fs_pct}%</span>` : ""}
      ${n.workloads.length ? n.workloads.slice(0, 4).map(w => `<span class="tag">${esc(w)}</span>`).join("")
        : '<span class="dim xs">no workloads here</span>'}
    </div></div>`;
}

window.nodeDetail = async name => {
  modal("Node · " + name, `<div class="empty"><span class="spin2"></span>loading</div>`, true);
  try {
    const n = await api("/api/node?name=" + encodeURIComponent(name));
    const i = n.info || {};
    const row = (l, v) => `<div class="drow"><div class="dl">${l}</div><div class="dv mono">${v}</div></div>`;
    $("#mbody").innerHTML = `
      <div class="grid g2" style="margin-bottom:16px">
        <div class="card flat"><div class="ctitle">Utilisation</div>
          <div style="margin-top:12px">
            <div class="between"><span class="dim xs">CPU</span><span class="mono small">${n.cpu_used} / ${n.cpu_cap} cores</span></div>
            ${meter(n.cpu_pct, 'style="margin:6px 0 14px"')}
            <div class="between"><span class="dim xs">MEMORY</span><span class="mono small">${n.mem_used_gb} / ${n.mem_cap_gb} GB</span></div>
            ${meter(n.mem_pct, 'style="margin:6px 0 14px"')}
            <div class="between"><span class="dim xs">NODE DISK</span><span class="mono small">${n.fs_used_gb} / ${n.fs_cap_gb} GB</span></div>
            ${meter(n.fs_pct || 0, 'style="margin:6px 0 0"')}
          </div></div>
        <div class="card flat"><div class="ctitle">Network</div>
          ${row("Interface", esc(n.net_iface || "—"))}
          ${row("In", (n.rx_mbps || 0).toFixed(2) + " Mb/s")}
          ${row("Out", (n.tx_mbps || 0).toFixed(2) + " Mb/s")}
          ${row("Total in", (n.rx_total_gb || 0) + " GB")}
          ${row("Total out", (n.tx_total_gb || 0) + " GB")}
          ${row("Address", esc((n.addresses || {}).InternalIP || "—"))}
        </div>
      </div>
      <div class="card flat"><div class="ctitle">System</div>
        ${row("Status", `<span class="pill ${n.status === "Ready" ? "ok" : "crit"}">${esc(n.status)}</span>`)}
        ${row("Roles", n.roles.map(r => `<span class="tag">${esc(r)}</span>`).join(""))}
        ${row("Schedulable", n.schedulable ? "yes" : `<span class="tag warn">cordoned</span>`)}
        ${row("iGPU", n.igpu ? `<span class="tag gpu">yes</span>` : "—")}
        ${row("Pods", `${n.pods_wl} yours · ${n.pods_sys} system`)}
        ${row("OS", esc(n.os))}
        ${row("Kernel", esc(n.kernel))}
        ${row("Container runtime", esc(i.containerRuntimeVersion || "—"))}
        ${row("Kubelet", esc(i.kubeletVersion || "—"))}
        ${row("Image storage", (n.img_used_gb || 0) + " GB")}
      </div>
      <div class="card flat" style="margin-top:16px"><div class="ctitle">Conditions</div>
        <div style="margin-top:10px">${(n.conditions || []).map(c =>
          `<span class="tag ${c.type === "Ready" ? (c.status === "True" ? "ok" : "bad")
            : (c.status === "True" ? "warn" : "")}">${esc(c.type)}: ${esc(c.status)}</span>`).join("")}</div>
      </div>
      <div class="note" style="margin-top:16px">
        <b>Temperatures are not shown.</b> Kubernetes exposes no thermal data — it needs a
        privileged DaemonSet reading <code>/sys/class/thermal</code> on each host. Tracked as 2.5a in the plan.
      </div>`;
  } catch (e) { $("#mbody").innerHTML = `<div class="empty">${esc(e.message)}</div>`; }
};

async function viewNodes() {
  const n = await api("/api/nodes");
  paint(`<div class="phead"><div><h2>Nodes</h2>
      <p>${n.length} node${n.length === 1 ? "" : "s"} · click any card for detail</p></div></div>
   <div class="nodegrid stagger">${n.map(nodeCard).join("")}</div>
   <div class="sec">Detail</div>
   <div class="card flat pad0"><div class="tblwrap"><table class="tbl"><thead><tr>
     <th>Node</th><th>Roles</th><th>CPU</th><th>Memory</th><th>Network</th><th>Disk</th><th>Pods</th><th>iGPU</th></tr></thead><tbody>
   ${n.map(x => `<tr class="clickable" onclick="nodeDetail('${esc(x.name)}')">
     <td><b>${esc(x.name)}</b><div class="dim xs">${esc(x.kernel)}</div></td>
     <td>${x.roles.map(r => `<span class="tag">${esc(r)}</span>`).join("")}</td>
     <td style="min-width:120px">${meter(x.cpu_pct)}<div class="dim xs mono" style="margin-top:4px">${x.cpu_pct}% of ${x.cpu_cap}</div></td>
     <td style="min-width:120px">${meter(x.mem_pct)}<div class="dim xs mono" style="margin-top:4px">${x.mem_used_gb}/${x.mem_cap_gb} GB</div></td>
     <td class="mono small">↓${(x.rx_mbps || 0).toFixed(1)}<br>↑${(x.tx_mbps || 0).toFixed(1)}</td>
     <td style="min-width:100px">${meter(x.fs_pct || 0)}<div class="dim xs mono" style="margin-top:4px">${x.fs_used_gb}/${x.fs_cap_gb}G</div></td>
     <td class="mono"><b>${x.pods_wl}</b><div class="dim xs">${x.pods_sys} sys</div></td>
     <td>${x.igpu ? '<span class="tag gpu">yes</span>' : '<span class="dim">—</span>'}</td></tr>`).join("")}
   </tbody></table></div></div>`);
}
