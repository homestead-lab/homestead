const tempCls = c => c == null ? "" : sev(c, "temperature") === "b" ? "t-hot" : sev(c, "temperature") === "w" ? "t-warm" : "t-ok";
const tempTag = c => c == null ? "" : sev(c, "temperature") === "b" ? "bad" : sev(c, "temperature") === "w" ? "warn" : "";
const nodeHardwareIds = n => (STATE.data.hardwareFeatures || [])
  .filter(f => n.hardware?.[f.id]).map(f => f.id);

/* Dashboard, Nodes, node detail modal */

async function viewDash() {
  const [o, hist, st] = await Promise.all([
    api("/api/overview"),
    api("/api/history").catch(() => ({})),
    api("/api/storage").catch(() => null),
    loadHardwareFeatures(),
    loadHealthSettings(),
  ]);
  STATE.data.ov = o; STATE.data.stor = st;
  const hp = $("#healthPill");
  const healthState = o.health_state || o.health;
  hp.className = "pill " + (healthState === "healthy" ? "ok" :
    healthState === "critical" ? "crit" : healthState === "degraded" ? "med" : "low");
  hp.textContent = healthState.toUpperCase();
  hp.title = o.health_summary || "";
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
      <button class="btn pri" onclick="go('deploy')">＋ Deploy</button>
    </div>
  </div>

  ${(o.health_issues || []).length ? `<div class="clusteralert ${o.health === "critical" ? "critical" : ""}">
    <div><b>${o.health === "critical" ? "Cluster needs attention" : "Cluster is degraded"}</b>
      <span>${esc(o.health_summary)}</span></div>
    <button class="btn sm" onclick="go('${o.health_issues.some(x => x.kind === "Node") ? "nodes" :
      o.health_issues.some(x => x.kind === "Volume") ? "storage" : "workloads"}')">Review</button>
  </div>` : ""}

  <div class="grid g3 stagger">
    <div class="card glow ${worstMetricClass([{ value: o.cpu_pct, metric: "cpu" }, { value: o.mem_pct, metric: "memory" }])}">
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
          ${meter(diskCap ? diskUsed / diskCap * 100 : 0, "", "disk")}</div>
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
  const health = worstMetricClass([
    { value: n.cpu_pct, metric: "cpu" }, { value: n.mem_pct, metric: "memory" },
    { value: n.fs_pct || 0, metric: "disk" }, { value: n.temps?.cpu_c || 0, metric: "temperature" },
  ]);
  return `<div class="card glow ${bad ? "g-bad" : health} clickable"
       onclick="nodeDetail('${esc(n.name)}')">
    <div class="between">
      <div class="row" style="gap:10px">
        <div class="av n2">${esc(n.name.replace(/[^0-9a-z]/gi, "").slice(-2).toUpperCase())}</div>
        <div><div style="font-weight:680">${esc(n.name)}</div>
          <div class="dim xs">${n.roles.join(" · ")}</div></div>
      </div>
      <div class="row" style="gap:7px">
        ${n.schedulable === false ? '<span class="pill med">cordoned</span>' : ""}
        <span class="pill ${bad ? "crit" : "low"}">${n.status}</span>
        <button class="btn sm" onclick="event.stopPropagation();nodeActions('${esc(n.name)}')">⋯</button>
      </div>
    </div>
    <div class="row" style="margin-top:16px;gap:14px;align-items:flex-start">
      <div style="flex:1;min-width:0">
        <div class="between"><span class="dim xs">CPU</span>
          <span class="small mono"><b>${n.cpu_pct}%</b> <span class="dim">of ${n.cpu_cap}</span></span></div>
        ${meter(n.cpu_pct, 'style="margin:5px 0 11px"', "cpu")}
        <div class="between"><span class="dim xs">MEMORY</span>
          <span class="small mono"><b>${n.mem_pct}%</b> <span class="dim">${n.mem_used_gb}/${n.mem_cap_gb}G</span></span></div>
        ${meter(n.mem_pct, 'style="margin:5px 0 11px"', "memory")}
        <div class="between"><span class="dim xs">DISK</span>
          <span class="small mono"><b>${n.fs_pct || 0}%</b> <span class="dim">${n.fs_used_gb}/${n.fs_cap_gb}G</span></span></div>
        ${meter(n.fs_pct || 0, 'style="margin:5px 0 11px"', "disk")}
        <div class="between"><span class="dim xs">NETWORK</span>
          <span class="small mono">↓${(n.rx_mbps || 0).toFixed(1)} ↑${(n.tx_mbps || 0).toFixed(1)} <span class="dim">Mb/s</span></span></div>
        ${n.temps && n.temps.cpu_c != null ? `<div class="between" style="margin-top:9px">
          <span class="dim xs">TEMP</span>
          <span class="small mono ${tempCls(n.temps.cpu_c)}"><b>${n.temps.cpu_c}°C</b>
            ${n.temps.max_c > n.temps.cpu_c ? `<span class="dim">max ${n.temps.max_c}°</span>` : ""}</span></div>` : ""}
      </div>
      <div style="text-align:right">
        <div class="midnum">${n.pods}</div><div class="dim xs">pods</div>
        ${n.vms ? `<div class="midnum" style="margin-top:8px">${n.vms}</div><div class="dim xs">VMs</div>` : ""}
      </div>
    </div>
    <div class="podgrid">${dots}</div>
    <div class="nodebadges">
      <div class="badgegroup"><span class="badgecap">HARDWARE</span>
        ${hardwareTags(nodeHardwareIds(n)) || '<span class="dim xs">none defined</span>'}</div>
      <div class="badgegroup"><span class="badgecap">WORKLOADS</span>
        ${n.workloads.length ? n.workloads.slice(0, 5).map(w =>
            `<span class="tag movable" title="Move ${esc(w)} to another host"
               onclick="event.stopPropagation();moveWorkload('${esc(w)}')">${esc(w)} <span class="mv">⇄</span></span>`).join("")
          : '<span class="dim xs">none</span>'}</div>
    </div></div>`;
}

window.nodeDetail = async (name, fromRoute = false) => {
  if (!fromRoute && window.setModalRoute) setModalRoute({ node: name }, name);
  modal("Node · " + name, `<div class="empty"><span class="spin2"></span>loading</div>`, true);
  try {
    const n = await api("/api/node?name=" + encodeURIComponent(name));
    const i = n.info || {};
    const row = (l, v) => `<div class="drow"><div class="dl">${l}</div><div class="dv mono">${v}</div></div>`;
    const disks = (n.temps && n.temps.disks) || [];
    const diskRows = disks.map(d => `<div class="diskrow">
      <div class="diskidentity"><b class="mono">${esc(d.name)}</b><span>${esc(d.model || d.name)}</span></div>
      <div><span class="disklabel">TYPE</span><b>${esc(d.kind || "Disk")}</b></div>
      <div><span class="disklabel">CAPACITY</span><b class="mono">${Number(d.size_gb || 0).toFixed(1)} GB</b></div>
      <div><span class="disklabel">READ</span><b class="mono diskrate read">↓ ${Number(d.read_mbps || 0).toFixed(2)} MB/s</b></div>
      <div><span class="disklabel">WRITE</span><b class="mono diskrate write">↑ ${Number(d.write_mbps || 0).toFixed(2)} MB/s</b></div>
    </div>`).join("");
    $("#mbody").innerHTML = `
      <div class="grid g2" style="margin-bottom:16px">
        <div class="card flat"><div class="ctitle">Utilisation</div>
          <div style="margin-top:12px">
            <div class="between"><span class="dim xs">CPU</span><span class="mono small">${n.cpu_used} / ${n.cpu_cap} cores</span></div>
            ${meter(n.cpu_pct, 'style="margin:6px 0 14px"', "cpu")}
            <div class="between"><span class="dim xs">MEMORY</span><span class="mono small">${n.mem_used_gb} / ${n.mem_cap_gb} GB</span></div>
            ${meter(n.mem_pct, 'style="margin:6px 0 14px"', "memory")}
            <div class="between"><span class="dim xs">NODE DISK</span><span class="mono small">${n.fs_used_gb} / ${n.fs_cap_gb} GB</span></div>
            ${meter(n.fs_pct || 0, 'style="margin:6px 0 0"', "disk")}
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
      <div class="card flat" style="margin-bottom:16px">
        <div class="between"><div><div class="ctitle">Disk activity</div>
          <div class="csub">Live host block-device throughput · read and write megabytes per second</div></div>
          <span class="tag">${disks.length} disk${disks.length === 1 ? "" : "s"}</span></div>
        <div class="diskactivity">${diskRows || `<div class="note"><b>No physical disk counters available.</b> The optional node probe must be running with its read-only <span class="mono">/proc</span> mount.</div>`}</div>
      </div>
      <div class="card flat"><div class="ctitle">System</div>
        ${row("Status", `<span class="pill ${n.status === "Ready" ? "ok" : "crit"}">${esc(n.status)}</span>`)}
        ${row("Roles", n.roles.map(r => `<span class="tag">${esc(r)}</span>`).join(""))}
        ${row("Schedulable", n.schedulable ? "yes" : `<span class="tag warn">cordoned</span>`)}
        ${row("Hardware", hardwareTags(nodeHardwareIds(n)) || "—")}
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
      <div class="card flat" style="margin-top:16px"><div class="ctitle">Temperatures</div>
        ${n.temps && n.temps.sensors ? `
          <div class="row" style="margin-top:12px;gap:26px">
            <div><div class="bignum ${tempCls(n.temps.cpu_c)}">${n.temps.cpu_c ?? "—"}<span class="unit">°C</span></div>
              <div class="csub">CPU package</div></div>
            <div><div class="midnum ${tempCls(n.temps.max_c)}">${n.temps.max_c ?? "—"}<span class="unit">°C</span></div>
              <div class="csub">hottest sensor</div></div>
          </div>
          <div style="margin-top:14px">${[...(n.temps.hwmon || []), ...(n.temps.thermal || [])]
            .sort((a, b) => b.celsius - a.celsius).slice(0, 14)
            .map(t => `<span class="tag ${tempTag(t.celsius)}">${esc(t.chip ? t.chip + " " : "")}${esc(t.name)} ${t.celsius}°</span>`).join("")}</div>`
        : `<div class="note" style="margin-top:12px"><b>No thermal data.</b> Kubernetes exposes none —
           it needs the optional node probe. Apply
           <span class="mono">deploy/nodeprobe.yaml</span> to enable it; it mounts
           <span class="mono">/sys</span> read-only, drops all capabilities and is not privileged.</div>`}
      </div>
      <div class="card flat" style="margin-top:16px">
        <div class="between"><div><div class="ctitle">Hardware availability</div>
          <div class="csub">Used by placement checks before containers move or start</div></div>
          <button class="btn sm" data-need="admin" onclick='hardwareEdit(${JSON.stringify(n).replace(/'/g, "&#39;")})'>Define hardware</button></div>
        <div style="margin-top:12px">${hardwareTags(nodeHardwareIds(n)) || '<span class="dim xs">No hardware is currently defined.</span>'}</div>
      </div>
      <div class="card flat" style="margin-top:16px">
        <div class="between"><div><div class="ctitle">Workloads on this host</div>
          <div class="csub">${n.pods_wl} of yours · ${n.pods_sys} system pods</div></div>
          ${n.workloads.length ? `<button class="btn sm" data-need="admin"
            onclick="evacuateNode('${esc(n.name)}')">Evacuate all</button>` : ""}</div>
        <div style="margin-top:12px">${n.workloads.length
          ? n.workloads.map(w => `<span class="tag movable"
              onclick="moveWorkload('${esc(w)}')">${esc(w)} <span class="mv">⇄</span></span>`).join("")
          : '<span class="dim xs">nothing of yours is scheduled here</span>'}</div>
        ${n.workloads.length ? '<div class="dim xs" style="margin-top:10px">Click a workload to move it to another host.</div>' : ""}
      </div>
      <div class="row" style="margin-top:16px">
        <button class="btn" onclick="nodeActions('${esc(n.name)}')">Host actions…</button></div>`;
  } catch (e) { $("#mbody").innerHTML = `<div class="empty">${esc(e.message)}</div>`; }
};
window.hardwareEdit = n => modal("Hardware · " + n.name, `
  <p class="muted small">Choose which configured features workloads may use on this node. Saving writes explicit Kubernetes labels; unchecked features are explicitly disabled even if detected.</p>
  <div class="hwchoices">${hardwareChoices("hw_node", nodeHardwareIds(n))}</div>
  <div class="note">${(n.hardware_inventory || []).map(x => `<div><b>${esc(x.name)}</b> · ${x.detected ? "detected" : "not detected"} · ${x.explicit == null ? "automatic" : x.explicit ? "enabled" : "disabled"}</div>`).join("") || "The node probe has not reported hardware inventory yet."}</div>
  <div class="row" style="margin-top:16px"><button class="btn pri" onclick="hardwareSave('${esc(n.name)}')">Save</button><button class="btn" onclick="closeModal()">Cancel</button></div>`);
window.hardwareSave = async node => {
  const body = { node, features: selectedHardware("hw_node") };
  try { await api("/api/node/hardware", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
    toast(`hardware updated on ${node}`, "ok"); closeModal(); setTimeout(() => { resetPaint(); viewNodes(); }, 600); } catch (e) { toast(e.message, "bad"); }
};

window.hardwareFeatureSettings = async () => {
  const defs = await loadHardwareFeatures(true);
  modal("Hardware features", `
    <div class="between"><p class="muted small">Define reusable passthrough and placement features. iGPU remains built in; everything else is editable.</p>
      <div class="row">${window.__hardwareReturn ? '<button class="btn sm" onclick="hardwareManagerBack()">← Back to container</button>' : ""}<button class="btn pri sm" data-need="admin" onclick="hardwareFeatureEdit()">＋ Add feature</button></div></div>
    <div class="featurelist" style="margin-top:14px">${defs.map(f => `<div class="card flat featurecard">
      <div class="between"><div><b>${esc(f.name)}</b> ${f.builtin ? '<span class="tag">built in</span>' : ""}<div class="dim xs mono">${esc(f.id)} · ${esc(f.label)}</div></div>
      ${f.builtin ? "" : `<div class="row"><button class="btn sm" onclick="hardwareFeatureEdit('${esc(f.id)}')">Edit</button><button class="btn sm danger" onclick="hardwareFeatureDelete('${esc(f.id)}')">Delete</button></div>`}</div>
      <div class="small" style="margin-top:9px"><span class="tag hw">${esc(f.host_path)} → ${esc(f.container_path)}</span> <span class="tag">${esc(f.path_type)}</span></div>
      ${f.usb_ids?.length ? `<div class="dim xs" style="margin-top:7px">USB IDs: ${esc(f.usb_ids.join(", "))}</div>` : ""}
      ${f.description ? `<div class="dim small" style="margin-top:7px">${esc(f.description)}</div>` : ""}</div>`).join("")}</div>
    <div class="note" style="margin-top:14px">A feature adds a node selector and mounts its host path into the container. USB IDs control node detection; the configured path controls passthrough.</div>`, true);
};
window.hardwareFeatureEdit = async id => {
  const old = id ? hardwareDef(id) : { id: "", name: "", description: "", host_path: "/dev/", container_path: "/dev/", path_type: "CharDevice", usb_ids: [] };
  if (!(STATE.data.nodes || STATE.data.ov?.nodes)?.length) {
    STATE.data.nodes = await api("/api/nodes").catch(() => []);
  }
  const nodes = STATE.data.nodes || STATE.data.ov?.nodes || [];
  modal((id ? "Edit" : "Add") + " hardware feature", `
    <div class="f2"><div class="f"><label>Feature ID ${tip("Stable internal ID used by workloads and node labels. It cannot be changed after creation.")}</label><input id="hf_id" value="${esc(old.id)}" ${id ? "disabled" : ""} placeholder="hailo-8"></div>
      <div class="f"><label>Display name</label><input id="hf_name" value="${esc(old.name)}" placeholder="Hailo-8 accelerator"></div></div>
    <div class="f"><label>Description</label><input id="hf_desc" value="${esc(old.description || "")}" placeholder="Optional note for operators"></div>
    <div class="f2"><div class="f"><label>Host device path ${tip("Path present on the Harvester node. It is also used for automatic detection when USB IDs are blank.")}</label><input id="hf_host" value="${esc(old.host_path)}" placeholder="/dev/hailo0"></div>
      <div class="f"><label>Path inside container</label><input id="hf_container" value="${esc(old.container_path)}" placeholder="/dev/hailo0"></div></div>
    <div class="f2"><div class="f"><label>Path type</label><select id="hf_type">${["CharDevice","Directory","BlockDevice","Socket","File"].map(x => `<option ${x === old.path_type ? "selected" : ""}>${x}</option>`).join("")}</select></div>
      <div class="f"><label>USB vendor:product IDs ${tip("Optional comma-separated VID:PID pairs, for example 18d1:9302. A matching device marks the feature present on that node.")}</label><input id="hf_usb" value="${esc((old.usb_ids || []).join(", "))}" placeholder="18d1:9302"></div></div>
    <div class="device-browser">
      <div class="between"><div><b>Browse a host</b><div class="dim xs">Choose the node whose <span class="mono">/dev</span> tree you want to map.</div></div>
        <span class="tag">read only</span></div>
      <div class="f2" style="margin-top:10px"><div class="f"><label>Host</label><select id="hf_browse_node" onchange="hardwareBrowseRender()">
        ${nodes.map(n => `<option value="${esc(n.name)}">${esc(n.name)}${n.status !== "Ready" ? " · " + esc(n.status) : ""}</option>`).join("")}</select></div>
        <div class="f"><label>Filter devices</label><input id="hf_browse_q" placeholder="coral, dri, video, tty…" oninput="hardwareBrowseRender()"></div></div>
      <div id="hf_browse_results"></div>
    </div>
    <div class="note">Device passthrough makes the container privileged. Use the narrowest stable /dev path available. For a USB VID:PID, /dev/bus/usb is commonly required because bus addresses can change after reboot.</div>
    <div class="row" style="margin-top:16px"><button class="btn pri" onclick="hardwareFeatureSave('${esc(id || "")}' )">Save feature</button><button class="btn" onclick="hardwareFeatureSettings()">Cancel</button></div>`, true);
  hardwareBrowseRender();
};
window.hardwareBrowseRender = () => {
  const host = $("#hf_browse_node")?.value;
  const q = ($("#hf_browse_q")?.value || "").trim().toLowerCase();
  const node = (STATE.data.nodes || STATE.data.ov?.nodes || []).find(n => n.name === host);
  const dev = node?.temps?.devices || {};
  const pathEntries = (dev.path_entries || (dev.paths || []).map(path => ({ path, type: "File" })))
    .map(x => ({ kind: "path", path: x.path, type: x.type || "File", search: `${x.path} ${x.type}`.toLowerCase() }));
  const usb = (dev.usb || []).map(x => ({ kind: "usb", path: x.path || "/dev/bus/usb", type: "Directory",
    id: `${x.vid}:${x.pid}`, name: x.name || "USB device",
    search: `${x.name || ""} ${x.vid}:${x.pid} ${x.path || ""}`.toLowerCase() }));
  window.__hardwareBrowseItems = usb.concat(pathEntries).filter(x => !q || x.search.includes(q)).slice(0, 160);
  const el = $("#hf_browse_results");
  if (!el) return;
  if (!node?.temps) {
    el.innerHTML = '<div class="empty small">No probe data from this host yet.</div>'; return;
  }
  el.innerHTML = window.__hardwareBrowseItems.length ? `<div class="device-list">${window.__hardwareBrowseItems.map((x, i) =>
    `<button class="device-row" type="button" onclick="hardwareUsePath(${i})"><span><b>${esc(x.kind === "usb" ? x.name : x.path)}</b>
      <span class="dim xs mono">${x.kind === "usb" ? `${esc(x.id)} · ${esc(x.path || "/dev/bus/usb")}` : esc(x.type)}</span></span><span class="tag">Use</span></button>`).join("")}</div>
    ${window.__hardwareBrowseItems.length >= 160 ? '<div class="dim xs" style="margin-top:7px">Showing the first 160 matches — type a filter to narrow the list.</div>' : ""}`
    : '<div class="empty small">No matching device paths on this host.</div>';
};
window.hardwareUsePath = i => {
  const x = (window.__hardwareBrowseItems || [])[i];
  if (!x) return;
  if (x.kind === "usb") {
    $("#hf_host").value = "/dev/bus/usb"; $("#hf_container").value = "/dev/bus/usb"; $("#hf_type").value = "Directory";
    const input = $("#hf_usb"), ids = input.value.split(/[\s,]+/).filter(Boolean);
    if (!ids.includes(x.id)) ids.push(x.id);
    input.value = ids.join(", ");
  } else {
    $("#hf_host").value = x.path; $("#hf_container").value = x.path; $("#hf_type").value = x.type;
  }
  toast(`mapped ${x.kind === "usb" ? x.name : x.path}`, "ok");
};
window.hardwareFeatureSave = async oldId => {
  const feature = { id: $("#hf_id").value.trim(), name: $("#hf_name").value.trim(), description: $("#hf_desc").value.trim(),
    host_path: $("#hf_host").value.trim(), container_path: $("#hf_container").value.trim(), path_type: $("#hf_type").value,
    usb_ids: $("#hf_usb").value.split(/[\s,]+/).filter(Boolean) };
  const custom = (STATE.data.hardwareFeatures || []).filter(x => !x.builtin && x.id !== oldId).map(x => ({ ...x }));
  custom.push(feature);
  try { await api("/api/hardware/features", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ features: custom }) });
    STATE.data.hardwareFeatures = null; toast(`${feature.name} saved`, "ok"); hardwareFeatureSettings(); }
  catch (e) { toast(e.message, "bad"); }
};
window.hardwareFeatureDelete = async id => {
  const f = hardwareDef(id);
  if (!confirm(`Delete hardware feature "${f.name}"?\n\nExisting workloads using its node label are not changed.`)) return;
  const custom = (STATE.data.hardwareFeatures || []).filter(x => !x.builtin && x.id !== id);
  try { await api("/api/hardware/features", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ features: custom }) });
    STATE.data.hardwareFeatures = null; toast(`${f.name} deleted`, "ok"); hardwareFeatureSettings(); }
  catch (e) { toast(e.message, "bad"); }
};

async function viewNodes() {
  const [n] = await Promise.all([api("/api/nodes"), loadHardwareFeatures()]);
  STATE.data.nodes = n;
  paint(`<div class="phead"><div><h2>Nodes</h2>
      <p>${n.length} node${n.length === 1 ? "" : "s"} · click any card for detail</p></div>
      <button class="btn" data-need="admin" onclick="hardwareFeatureSettings()">Hardware features</button></div>
   <div class="nodegrid stagger">${n.map(nodeCard).join("")}</div>
   <div class="sec">Detail</div>
   <div class="card flat pad0"><div class="tblwrap"><table class="tbl"><thead><tr>
     <th>Node</th><th>Roles</th><th>CPU</th><th>Memory</th><th>Network</th><th>Temp</th><th>Disk</th><th>Pods</th><th>Hardware</th><th>Workloads</th></tr></thead><tbody>
   ${n.map(x => `<tr class="clickable" onclick="nodeDetail('${esc(x.name)}')">
     <td><b>${esc(x.name)}</b><div class="dim xs">${esc(x.kernel)}</div></td>
     <td>${x.roles.map(r => `<span class="tag">${esc(r)}</span>`).join("")}</td>
      <td style="min-width:120px">${meter(x.cpu_pct, "", "cpu")}<div class="dim xs mono" style="margin-top:4px">${x.cpu_pct}% of ${x.cpu_cap}</div></td>
      <td style="min-width:120px">${meter(x.mem_pct, "", "memory")}<div class="dim xs mono" style="margin-top:4px">${x.mem_used_gb}/${x.mem_cap_gb} GB</div></td>
     <td class="mono small">↓${(x.rx_mbps || 0).toFixed(1)}<br>↑${(x.tx_mbps || 0).toFixed(1)}</td>
     <td class="mono ${tempCls(x.temps && x.temps.cpu_c)}">${x.temps && x.temps.cpu_c != null ? x.temps.cpu_c + "°" : '<span class="dim">—</span>'}</td>
      <td style="min-width:100px">${meter(x.fs_pct || 0, "", "disk")}<div class="dim xs mono" style="margin-top:4px">${x.fs_used_gb}/${x.fs_cap_gb}G</div></td>
     <td class="mono"><b>${x.pods_wl}</b><div class="dim xs">${x.pods_sys} sys</div></td>
     <td>${hardwareTags(nodeHardwareIds(x)) || '<span class="dim">—</span>'}</td>
     <td onclick="event.stopPropagation()">${x.workloads.length
        ? x.workloads.slice(0, 3).map(w => `<span class="tag movable"
            onclick="moveWorkload('${esc(w)}')">${esc(w)} <span class="mv">⇄</span></span>`).join("")
        : '<span class="dim xs">—</span>'}</td></tr>`).join("")}
   </tbody></table></div></div>`);
}
