/* Edit / move containers, node power actions, VMs, images, schedules, import */

/* ---------------- edit a pod and its containers ---------------- */
const editEnvRow = (index, key = "", value = "") => `<div class="f3 e-env-row">
  <div><label>Key</label><input class="ek" type="text" value="${esc(key)}"></div>
  <div><label>Value</label><input class="ev" type="text" value="${esc(value)}"></div>
  <button class="btn sm danger row-remove" type="button" onclick="this.parentNode.remove()">✕</button></div>`;

const editPortRow = (index, port = {}) => `<div class="edit-port-row">
  <div><label>Name</label><input class="ep-name" type="text" value="${esc(port.name || "")}" placeholder="web"></div>
  <div><label>Container port</label><input class="ep-number" type="number" min="1" max="65535" value="${esc(port.container || "")}" placeholder="8080"></div>
  <div><label>Protocol</label><select class="ep-protocol">${["TCP", "UDP", "SCTP"].map(value => `<option ${value === (port.protocol || "TCP") ? "selected" : ""}>${value}</option>`).join("")}</select></div>
  <button class="btn sm danger row-remove" type="button" onclick="this.parentNode.remove()">✕</button></div>`;

const editContainerPanel = (container, index) => {
  const env = Object.entries(container.env || {});
  const refs = container.env_refs || [];
  const ports = container.ports || [];
  const volumes = container.volumes || [];
  return `<details class="edit-container card flat" data-index="${index}" data-original-name="${esc(container.original_name || container.name)}" ${index === 0 ? "open" : ""}>
    <summary><span class="edit-container-chevron">›</span><span class="edit-container-title"><b>${esc(container.name)}</b><small class="mono">${esc(container.image)}</small></span><span class="pill">${env.length + refs.length} vars</span><span class="pill">${ports.length} ports</span></summary>
    <div class="edit-container-body">
      <div class="f2">
        <div class="f"><label>Container name ${tip("The Kubernetes name of this container inside the pod. Each container name must be unique.")}</label><input id="e_container_name_${index}" type="text" value="${esc(container.name)}"></div>
        <div class="f"><label>Image</label><input id="e_image_${index}" type="text" value="${esc(container.image)}"></div>
      </div>
      <div class="f2">
        <div class="f"><label>CPU reserved ${tip("Guaranteed scheduling capacity. 1000m = one core; it is not a hard usage limit.")}</label><input id="e_cpu_${index}" type="text" value="${esc(container.cpu || "")}" placeholder="50m"></div>
        <div class="f"><label>Memory reserved ${tip("Guaranteed scheduling capacity in Mi or Gi; it is not a hard usage limit.")}</label><input id="e_mem_${index}" type="text" value="${esc(container.memory || "")}" placeholder="128Mi"></div>
      </div>
      <div class="subsec">Hardware passed to this container</div>
      <div class="hwchoices">${hardwareChoices(`e_hw_${index}`, container.hardware || [])}</div>
      <div class="subsec">Environment</div>
      ${refs.length ? `<div class="managed-env-list">${refs.map(ref => `<div><span class="mono">${esc(ref.name)}</span><span>${esc(ref.source)}</span><span class="pill info">managed reference</span></div>`).join("")}</div><div class="dim xs managed-env-note">References remain connected to Kubernetes and are not exposed or replaced when you save.</div>` : ""}
      <div class="e-env" id="e_env_${index}">${(env.length ? env : [["", ""]]).map(([key, value]) => editEnvRow(index, key, value)).join("")}</div>
      <button class="btn sm" type="button" onclick="editAddEnv(${index})">＋ add variable</button>
      <div class="subsec">Container ports</div>
      <div class="e-ports" id="e_ports_${index}">${ports.map(port => editPortRow(index, port)).join("")}</div>
      <button class="btn sm" type="button" onclick="editAddPort(${index})">＋ add port</button>
      ${volumes.length ? `<div class="subsec">Mounted volumes</div><div class="edit-mount-list">${volumes.map(volume => `<span class="tag info">${esc(volume.source || "?")} → ${esc(volume.path)}${volume.read_only ? " · read-only" : ""}</span>`).join("")}</div><div class="dim xs edit-mount-note">Mount changes require a redeploy, so they are shown here as read-only.</div>` : ""}
    </div>
  </details>`;
};

window.wlEdit = async (ns, name, fromRoute = false) => {
  if (!fromRoute && window.setModalRoute) setModalRoute({ panel: "edit", ns, workload: name }, name);
  modal("Edit · " + name, `<div class="empty"><span class="spin2"></span>loading</div>`, true);
  try {
    const [w, liveNodes] = await Promise.all([
      api(`/api/workload?ns=${encodeURIComponent(ns)}&name=${encodeURIComponent(name)}`),
      loadHardwareFeatures().then(() => api("/api/nodes").catch(() => [])),
    ]);
    if (liveNodes.length) STATE.data.nodes = liveNodes;
    const nodes = (liveNodes.length ? liveNodes : (STATE.data.ov ? STATE.data.ov.nodes : [])).filter(n => n.schedulable !== false);
    const seeds = w.seed_configs || [];
    const containers = w.containers || [{ original_name: w.container_name || w.name, name: w.container_name || w.name,
      image: w.image, cpu: w.cpu, memory: w.memory, env: w.env || {}, env_refs: [], ports: w.ports || [],
      hardware: w.hardware || [], volumes: w.volumes || [] }];
    $("#mbody").innerHTML = `
      <div class="f"><label>Workload name ${tip("The real Kubernetes Deployment name. Renaming creates a replacement Deployment, waits for it to become ready, then removes the old one. Generated pods use this name plus a Kubernetes suffix.")}</label><input type="text" id="e_workload_name" value="${esc(w.name)}"></div>
      <div class="f"><label>Pod hostname ${tip("The hostname visible inside the pod. It does not rename the Kubernetes Pod; generated pods use the workload name plus a suffix.")}</label><input type="text" id="e_pod_name" value="${esc(w.pod_hostname || "")}" placeholder="optional"></div>
      <div class="f"><label>Container logo ${tip("Optional public HTTPS image URL. Homestead validates it and keeps a persistent local copy while retaining this source for later edits.")}</label><input type="url" id="e_icon" value="${esc(w.icon || "")}" placeholder="https://…/icon.png"></div>
      <div class="f2">
        <div class="f"><label>Replicas</label><input type="number" id="e_rep" value="${w.replicas}" min="0" max="5"></div>
        <div class="f"><label>Preferred node ${tip("A preference guides placement but still allows failover. Use Move for hardware-aware choices and optional hard pinning.")}</label><select id="e_node" data-current="${esc(w.node || "")}">
          <option value="">any node</option>
          ${nodes.map(n => `<option value="${esc(n.name)}" ${n.name === w.node ? "selected" : ""}>${esc(n.name)}</option>`).join("")}
        </select></div>
      </div>
      <div class="sec">Containers <span class="pill">${containers.length}</span></div>
      <div id="e_containers">${containers.map(editContainerPanel).join("")}</div>
      ${seeds.length ? `<div class="sec">Startup seed config ${tip("This ConfigMap is copied into the container's persistent storage by an init container before every start. It is authoritative: editing only the mounted file will be overwritten on restart.")}</div>
        <div class="note seed-note"><b>Authoritative startup configuration.</b> Saving here updates the ConfigMap and restarts the workload so the init container copies the new value into appdata.</div>
        ${seeds.map((s, i) => `<div class="seed-editor card flat">
          <div class="between seed-head"><div><b>${esc(s.key)}</b><div class="dim xs mono">ConfigMap ${esc(s.config_map)} · init ${esc(s.init_container)}</div></div><span class="pill info">seeded on start</span></div>
          <label for="e_seed_${i}">Contents</label>
          <textarea id="e_seed_${i}" class="e_seed mono" rows="14"
            data-init="${esc(s.init_container)}" data-config-map="${esc(s.config_map)}" data-key="${esc(s.key)}">${esc(s.value)}</textarea>
          ${s.command ? `<div class="dim xs mono seed-command">${esc(s.command)}</div>` : ""}
        </div>`).join("")}` : ""}
      <div class="row" style="margin-top:22px">
        <button class="btn pri" id="e_save" onclick="editSave('${esc(ns)}','${esc(name)}')">Save &amp; restart</button>
        <button class="btn" onclick="closeModal()">Cancel</button>
      </div>
      <div class="note" style="margin-top:14px">Saving rolls the pod. Renaming the workload performs a guarded stop, recreate and readiness check. With a ReadWriteOnce volume the old pod must fully stop before the renamed one starts, so expect a short outage. A failed rename restores the original Deployment.</div>`;
  } catch (e) { $("#mbody").innerHTML = `<div class="empty">${esc(e.message)}</div>`; }
};
window.editAddEnv = (index, key = "", value = "") => $("#e_env_" + index).insertAdjacentHTML("beforeend", editEnvRow(index, key, value));
window.editAddPort = (index, port = {}) => $("#e_ports_" + index).insertAdjacentHTML("beforeend", editPortRow(index, port));
window.editSave = async (ns, name) => {
  const containers = $$("#e_containers .edit-container").map(panel => {
    const index = panel.dataset.index;
    const env = {};
    $$(".e-env-row", panel).forEach(row => { const key = $(".ek", row).value.trim(); if (key) env[key] = $(".ev", row).value; });
    const ports = $$(".edit-port-row", panel).map(row => ({ name: $(".ep-name", row).value.trim(),
      container: +$(".ep-number", row).value, protocol: $(".ep-protocol", row).value })).filter(port => port.container);
    return { original_name: panel.dataset.originalName, name: $("#e_container_name_" + index).value.trim(),
      image: $("#e_image_" + index).value.trim(), cpu: $("#e_cpu_" + index).value.trim(),
      memory: $("#e_mem_" + index).value.trim(), hardware: selectedHardware("e_hw_" + index), env, ports };
  });
  const seed_configs = $$("#mbody .e_seed").map(el => ({
    init_container: el.dataset.init, config_map: el.dataset.configMap,
    key: el.dataset.key, value: el.value,
  }));
  const workloadName = $("#e_workload_name").value.trim();
  const renaming = workloadName !== name;
  if (renaming && !confirm(`Rename Kubernetes Deployment “${name}” to “${workloadName}”?\n\nHomestead will stop the old workload, start the renamed one, wait for readiness, and restore the original if startup fails. Expect a short outage.`)) return;
  const body = { ns, name, workload_name: workloadName, pod_hostname: $("#e_pod_name").value.trim(),
    icon: $("#e_icon").value.trim(), replicas: +$("#e_rep").value, containers, seed_configs };
  const button = $("#e_save");
  button.disabled = true;
  button.textContent = renaming ? "Renaming & checking readiness…" : "Saving & restarting…";
  try {
    const result = await api("/api/edit", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
    const activeName = result.name || workloadName || name;
    const nodeSelect = $("#e_node");
    const node = nodeSelect.value;
    if (node !== (nodeSelect.dataset.current || "")) {
      await api("/api/move", { method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ ns, name: activeName, node: node || null }) });
    }
    toast(renaming ? `${name} renamed to ${activeName}` : `${activeName} updated`, "ok");
    closeModal(); setTimeout(() => refresh(true), 1200);
  } catch (e) {
    toast(e.message, "bad");
    button.disabled = false;
    button.textContent = "Save & restart";
  }
};

/* ---------------- move a container ---------------- */
window.wlMoveLegacy = async (ns, name) => {
  const nodes = (STATE.data.ov ? STATE.data.ov.nodes : []);
  modal("Move · " + name, `
    <p class="muted small">Pick the host this workload should run on. Homestead pins it with a
    node selector and rolls the pod.</p>
    <div class="f" style="margin-top:14px"><label>Target host</label><select id="mv_node">
      <option value="">any node (unpin)</option>
      ${nodes.map(n => `<option value="${esc(n.name)}">${esc(n.name)} · ${n.cpu_pct}% cpu, ${n.mem_pct}% ram${n.igpu ? " · iGPU" : ""}</option>`).join("")}
    </select></div>
    <div class="row" style="margin-top:18px">
      <button class="btn pri" onclick="doMove('${esc(ns)}','${esc(name)}')">Move</button>
      <button class="btn" onclick="closeModal()">Cancel</button></div>
    <div class="note" style="margin-top:14px">This is a stop-then-start, not a live move —
    a ReadWriteOnce volume can only attach to one node at a time.</div>`);
};
window.doMove = async (ns, name) => {
  try {
    const node = $("#mv_node").value;
    await api("/api/move", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ ns, name, node: node || null }) });
    toast(`${name} → ${node || "any node"}`, "ok"); closeModal(); setTimeout(() => refresh(true), 1400);
  } catch (e) { toast(e.message, "bad"); }
};

/* ---------------- node power ---------------- */
window.nodeActions = async name => {
  let qr = {}, impact = { workloads: [], stranded: [] };
  try { [qr, impact] = await Promise.all([api("/api/quorum"), api(`/api/node/impact?node=${encodeURIComponent(name)}`)]); } catch (e) { }
  window.__nodeImpact = impact;
  const isEtcd = (qr.members || []).includes(name);
  const risky = isEtcd && qr.can_lose < 1;
  const off = qr.power_enabled === false;
  modal("Host actions · " + name, `
    <div class="grid g2" style="gap:12px">
      <div class="card flat"><div class="ctitle">Scheduling</div>
        <p class="muted small" style="margin:6px 0 14px">Cordon stops new pods landing here.
        Drain evicts the ones already running.</p>
        <div class="row">
          <button class="btn" onclick="nodeCordon('${esc(name)}',true)">Cordon</button>
          <button class="btn" onclick="nodeCordon('${esc(name)}',false)">Uncordon</button>
          <button class="btn" onclick="nodeDrain('${esc(name)}')">Drain</button>
          <button class="btn" onclick="evacuateNode('${esc(name)}')">Move all off</button>
        </div></div>
      <div class="card flat"><div class="ctitle">Quorum</div>
        <div class="drow"><div class="dl">etcd members</div><div class="dv mono">${qr.total || "?"}</div></div>
        <div class="drow"><div class="dl">Ready</div><div class="dv mono">${(qr.ready || []).length}</div></div>
        <div class="drow"><div class="dl">Needs</div><div class="dv mono">${qr.quorum_needs || "?"}</div></div>
        <div class="drow"><div class="dl">Can lose</div><div class="dv mono">
          <span class="pill ${qr.can_lose > 0 ? "ok" : "crit"}">${qr.can_lose ?? "?"}</span></div></div>
        ${isEtcd ? '<div class="dim xs" style="margin-top:8px">This host is an etcd member.</div>' : ""}
      </div>
    </div>
    <div class="sec">Workload dependencies</div>
    ${(impact.workloads || []).length ? `<div class="dependency-list">${impactRows(impact)}</div>` : '<div class="empty small">No user workloads are currently running on this host.</div>'}
    ${(impact.stranded || []).length ? `<div class="note dependency-danger" style="margin-top:12px"><b>Stopping this host strands ${impact.stranded.length} workload${impact.stranded.length === 1 ? "" : "s"}.</b> ${impact.stranded.map(w => `<span class="mono">${esc(w.name)}</span>`).join(", ")} cannot run on any other ready host with the required hardware.</div>` : `<div class="note" style="margin-top:12px">Every current user workload has at least one compatible destination.</div>`}
    <div class="sec">Power</div>
    ${off ? `<div class="note"><b>Host power control is disabled.</b> Rebooting needs a privileged
        helper pod that enters the host namespaces, so it ships off. Set
        <span class="mono">ENABLE_NODE_POWER=true</span> on the Homestead Deployment to enable it.
        Cordon and drain above work regardless.</div>`
      : risky ? `<div class="note" style="border-color:rgba(255,77,79,.35);background:rgba(255,77,79,.08);color:#ffb4b8">
        <b>Blocked.</b> ${(qr.ready || []).length} of ${qr.total} etcd members are ready and quorum needs
        ${qr.quorum_needs}. Taking this host down would lose the cluster. Bring the other members back first.</div>`
      : `<p class="muted small">The host is cordoned and drained first, then a privileged helper pod
         asks systemd. Type the host name to confirm.</p>
      <div class="f" style="margin-top:12px"><label>Type <b class="mono">${esc(name)}</b> to confirm</label>
        <input type="text" id="pw_confirm" placeholder="${esc(name)}" autocomplete="off"></div>
      <label class="switch"><input type="checkbox" id="pw_drain" checked> Drain workloads first (recommended)</label>
      ${(impact.stranded || []).length ? `<label class="switch dependency-confirm"><input type="checkbox" id="pw_allow"> I understand ${impact.stranded.map(w => esc(w.name)).join(", ")} will remain down until compatible hardware is available</label>` : ""}
      <div class="row">
        <button class="btn danger" onclick="nodePower('${esc(name)}','reboot')">Reboot host</button>
        <button class="btn danger" onclick="nodePower('${esc(name)}','poweroff')">Shut down host</button>
      </div>`}`, true);
};
window.nodeCordon = async (node, cordon) => {
  try {
    await api("/api/node/cordon", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ node, cordon }) });
    toast(`${node} ${cordon ? "cordoned" : "uncordoned"}`, "ok"); refresh(true);
  } catch (e) { toast(e.message, "bad"); }
};
window.nodeDrain = async node => {
  evacuateNode(node);
};
window.nodePower = async (node, action) => {
  const c = $("#pw_confirm").value.trim();
  if (c !== node) return toast("type the host name exactly to confirm", "bad");
  if ((window.__nodeImpact?.stranded || []).length && !$("#pw_allow")?.checked)
    return toast("confirm the workloads that will remain down", "bad");
  try {
    const r = await api("/api/node/power", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ node, action, drain: $("#pw_drain").checked, confirm: c,
        allow_stranded: !!$("#pw_allow")?.checked }) });
    modal("Host " + action, `<pre>${esc(r.steps.join("\n"))}</pre>
      <div class="note" style="margin-top:12px">The host will go down shortly. It stays cordoned —
      uncordon it from this dialog once it is back.</div>`);
  } catch (e) { toast(e.message, "bad"); }
};

/* ---------------- VMs ---------------- */
async function viewVMs() {
  const [vms, nodes] = await Promise.all([api("/api/vms"), api("/api/nodes").catch(() => [])]);
  paint(`<div class="phead"><div><h2>Virtual machines</h2>
      <p>${vms.length} VM${vms.length === 1 ? "" : "s"} · KubeVirt on Longhorn</p></div>
      <button class="btn pri" data-need="operator" onclick="vmNew()">＋ New VM</button></div>
    ${vms.length ? `<div class="cardlist">${vms.map(v => `<div class="card flat wcard">
      <div class="between">
        <div class="row" style="gap:10px"><div class="av n3">${esc(v.name.slice(0, 2).toUpperCase())}</div>
          <div><div style="font-weight:680">${esc(v.name)}</div><div class="dim xs">${esc(v.ns)} · ${esc(v.node || "unscheduled")}</div></div></div>
        <span class="pill ${v.phase === "Running" ? "ok" : v.phase === "Stopped" ? "low" : "med"}">${esc(v.phase)}</span>
      </div>
      <div class="wmeta">
        <div><div class="dim xs">CPU</div><div class="mono small">${v.cores} cores</div></div>
        <div><div class="dim xs">RAM</div><div class="mono small">${esc(v.memory)}</div></div>
        <div><div class="dim xs">IP</div><div class="mono small">${esc(v.ip || "—")}</div></div>
        <div><div class="dim xs">MIGRATE</div><div>${v.migratable ? '<span class="tag ok">live</span>' : '<span class="dim">no</span>'}</div></div>
      </div>
      <div class="row wacts">
        ${v.running ? `<button class="btn sm" onclick="vmPower('${esc(v.ns)}','${esc(v.name)}','stop')">Stop</button>
          <button class="btn sm" onclick="vmPower('${esc(v.ns)}','${esc(v.name)}','restart')">Restart</button>`
          : `<button class="btn sm" onclick="vmPower('${esc(v.ns)}','${esc(v.name)}','start')">Start</button>`}
        ${v.migratable ? `<button class="btn sm" onclick="vmMove('${esc(v.ns)}','${esc(v.name)}')">Move host</button>` : ""}
      </div></div>`).join("")}</div>`
    : `<div class="empty">No virtual machines yet — create one to get started.</div>`}`);
  STATE.data.nodes = nodes;
}
window.vmPower = async (ns, name, action) => {
  try {
    await api("/api/vm/power", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ ns, name, action }) });
    toast(`${name} ${action}`, "ok"); setTimeout(() => refresh(true), 1500);
  } catch (e) { toast(e.message, "bad"); }
};
window.vmMove = (ns, name) => {
  const nodes = STATE.data.nodes || [];
  modal("Migrate · " + name, `
    <p class="muted small">KubeVirt live-migrates the VM — memory is copied while it keeps running,
    so there is no downtime.</p>
    <div class="f" style="margin-top:14px"><label>Target host</label><select id="vm_target">
      <option value="">let KubeVirt choose</option>
      ${nodes.map(n => `<option value="${esc(n.name)}">${esc(n.name)}</option>`).join("")}</select></div>
    <div class="row" style="margin-top:16px">
      <button class="btn pri" onclick="doVmMove('${esc(ns)}','${esc(name)}')">Migrate</button>
      <button class="btn" onclick="closeModal()">Cancel</button></div>`);
};
window.doVmMove = async (ns, name) => {
  try {
    const r = await api("/api/vm/migrate", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ ns, name, target: $("#vm_target").value || null }) });
    toast(`migration ${r.migration} started`, "ok"); closeModal(); setTimeout(() => refresh(true), 2000);
  } catch (e) { toast(e.message, "bad"); }
};
window.vmNew = async (selectedDisk = "", selectedNamespace = "") => {
  const [imgs, disks] = await Promise.all([
    api("/api/vmimages").catch(() => []), api("/api/vm-disks").catch(() => []),
  ]);
  const readyDisks = disks.filter(d => d.phase === "Succeeded" && !d.in_use);
  const selected = selectedDisk ? `disk:${selectedNamespace || "lab"}/${selectedDisk}` : "";
  modal("New virtual machine", `
    <div class="f"><label>Name</label><input type="text" id="v_name" placeholder="ubuntu-test"></div>
    <div class="f2">
      <div class="f"><label>CPU cores</label><input type="number" id="v_cores" value="2" min="1" max="16"></div>
      <div class="f"><label>Memory</label><input type="text" id="v_mem" value="2Gi"></div>
    </div>
    <div class="f2">
      <div class="f"><label>Disk (GB)</label><input type="number" id="v_disk" value="20" min="5"></div>
      <div class="f"><label>Root password ${tip("Required when Homestead provisions a new disk. Optional for an imported disk that already has login access configured.")}</label><input type="password" id="v_pass" autocomplete="new-password" placeholder="Set an initial password"></div>
    </div>
    <div class="f"><label>Boot disk ${tip("Use a completed CDI import without copying it again, select a Harvester image, or let the VM download a URL while it is created.")}</label>
      <select id="v_boot" onchange="vmBootChanged()">
        <option value="">blank disk</option>
        ${readyDisks.map(d => `<option value="disk:${esc(d.namespace)}/${esc(d.name)}" ${selected === `disk:${d.namespace}/${d.name}` ? "selected" : ""}>Imported · ${esc(d.namespace)}/${esc(d.name)} (${esc(d.capacity || "size unknown")})</option>`).join("")}
        ${imgs.map(i => `<option value="image:${esc(i.name)}">Harvester · ${esc(i.display)} (${i.size_gb}G)</option>`).join("")}
        <option value="url">Download from HTTP(S) URL</option>
      </select></div>
    <div class="f" id="v_url_row" hidden><label>Image URL</label><input type="url" id="v_url" placeholder="https://cloud-images.ubuntu.com/…/img"></div>
    <div class="row" style="margin-top:18px">
      <button class="btn pri" onclick="doVmCreate()">Create VM</button>
      <button class="btn" onclick="closeModal()">Cancel</button></div>
    <div class="note" style="margin-top:14px">New disks are provisioned as Longhorn DataVolumes.
    Imported disks are attached directly and remain visible on the Import page.</div>`, true);
  vmBootChanged();
};
window.vmBootChanged = () => { if ($("#v_url_row")) $("#v_url_row").hidden = $("#v_boot").value !== "url"; };
window.doVmCreate = async () => {
  const boot = $("#v_boot").value;
  const imported = boot.startsWith("disk:") ? boot.slice(5).split("/") : [];
  const body = { name: $("#v_name").value.trim(), cores: +$("#v_cores").value,
    memory: $("#v_mem").value.trim(), disk_gb: +$("#v_disk").value, password: $("#v_pass").value,
    namespace: imported[0] || "lab", disk_import: imported[1] || "",
    image_id: boot.startsWith("image:") ? boot.slice(6) : "",
    image_url: boot === "url" ? $("#v_url").value.trim() : "" };
  if (!body.name) return toast("name is required", "bad");
  if (!body.disk_import && body.password.length < 10) return toast("root password must be at least 10 characters", "bad");
  if (boot === "url" && !body.image_url) return toast("image URL is required", "bad");
  try {
    await api("/api/vm/create", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
    toast(`${body.name} created`, "ok"); closeModal(); go("vms");
  } catch (e) { toast(e.message, "bad"); }
};

/* ---------------- image cache ---------------- */
async function viewImages() {
  const d = await api("/api/images");
  STATE.data.imageCache = d;
  const q = STATE.q.toLowerCase();
  const core = n => /(^|\/)(rancher|harvester|longhornio|kubevirt|cdi-|cilium|kube-|metrics-server|registry\.k8s\.io|pause|traefik|fleet|system-upgrade|k8snetworkplumbingwg|multus|whereabouts|kubeovn|calico|canal|flannel|coredns|etcd|rke2|neuvector|suse\/sles\/)/i.test(n);
  const all = d.images.filter(i => !q || i.name.toLowerCase().includes(q));
  const hidden = all.filter(i => core(i.name)).length;
  const imgs = all.filter(i => STATE.showCoreImages || !core(i.name));
  paint(`<div class="phead"><div><h2>Image cache</h2>
      <p>${imgs.length} app images across ${d.nodes.length} nodes · ${d.protected || 0} active/rollback retained · ${hidden && !STATE.showCoreImages ? `${hidden} Harvester/system images hidden` : `${d.distinct} total`} ${tip("Kubernetes reports each node's largest cached images. Smaller cache entries may not appear and cannot be removed here.")}</p></div>
      <label class="switch"><input type="checkbox" ${STATE.showCoreImages ? "checked" : ""} onchange="STATE.showCoreImages=this.checked;viewImages()"> Show Harvester/system images</label></div>
    <div class="grid g3" style="margin-bottom:18px">
      ${d.nodes.map(n => `<div class="card flat"><div class="ctitle">${esc(n.node)}</div>
        <div class="bignum" style="margin-top:8px">${n.total_gb}<span class="unit">GB</span></div>
        <div class="csub">${n.count} images cached</div></div>`).join("")}
    </div>
    <div class="card flat pad0"><div class="tblwrap"><table class="tbl"><thead><tr>
      <th>Image</th><th>Size</th><th>Cached on</th><th>Retention</th><th></th></tr></thead><tbody>
      ${imgs.slice(0, 80).map(i => {
        const missing = d.node_names.filter(n => !i.nodes.includes(n));
        const retained = i.retained_by || [];
        const retention = retained.length ? retained.slice(0, 3).map(r => `<span class="tag ${r.reason === "rollback" ? "warn" : "ok"}"
          title="${esc(r.namespace)} · ${esc(r.workload)} · ${esc(r.container)}">${r.reason === "rollback" ? "rollback" : "active"} · ${esc(r.workload)}</span>`).join("") +
          (retained.length > 3 ? `<span class="tag">+${retained.length - 3}</span>` : "") : '<span class="tag">unreferenced</span>';
        return `<tr><td class="mono small" style="word-break:break-all">${esc(i.name)}</td>
        <td class="mono">${i.size_mb >= 1024 ? (i.size_mb / 1024).toFixed(1) + " GB" : i.size_mb + " MB"}</td>
        <td>${i.nodes.map(n => `<span class="tag ok">${esc(n.replace("harvester-", ""))}</span>`).join("")}
            ${missing.map(n => `<span class="tag">${esc(n.replace("harvester-", ""))} ✕</span>`).join("")}</td>
        <td><div class="row" style="gap:5px">${retention}</div></td>
        <td><div class="row" style="gap:6px">${missing.length ? `<button class="btn sm" onclick="prepull('${esc(i.name)}')">Pre-pull</button>` : '<span class="dim xs">everywhere</span>'}
          ${!i.protected && !i.system && i.digest ? `<button class="btn sm danger" data-need="admin" onclick="imageCleanupReview('${esc(i.digest)}')">Clean up</button>` : ""}</div></td></tr>`;
      }).join("") || `<tr><td colspan=5 class="empty">none</td></tr>`}
    </tbody></table></div></div>`);
}
window.prepull = async image => {
  try {
    await api("/api/images/prepull", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ image }) });
    toast("pre-pull started on all nodes", "ok");
  } catch (e) { toast(e.message, "bad"); }
};

/* ---------------- schedules ---------------- */
async function viewSchedules() {
  const js = await api("/api/schedules");
  paint(`<div class="phead"><div><h2>Schedules</h2>
      <p>${js.length} scheduled job${js.length === 1 ? "" : "s"} · standard cron syntax</p></div>
      <button class="btn pri" data-need="operator" onclick="jobEdit()">＋ New schedule</button></div>
    <div class="card flat pad0"><div class="tblwrap"><table class="tbl"><thead><tr>
      <th>Name</th><th>Schedule</th><th>Image</th><th>Last run</th><th>State</th><th></th></tr></thead><tbody>
      ${js.map(j => `<tr>
        <td><b>${esc(j.name)}</b></td>
        <td class="mono">${esc(j.schedule)}</td>
        <td class="mono small dim">${esc(j.image)}</td>
        <td class="small dim">${esc((j.last || "—").replace("T", " ").replace("Z", ""))}</td>
        <td>${j.suspend ? '<span class="pill low">paused</span>'
            : j.active ? '<span class="pill med">running</span>' : '<span class="pill ok">active</span>'}</td>
        <td><div class="row" style="gap:6px">
          <button class="btn sm" onclick="jobRun('${esc(j.name)}')">Run now</button>
          <button class="btn sm" onclick='jobEdit(${JSON.stringify(j).replace(/'/g, "&#39;")})'>Edit</button>
          <button class="btn sm danger" onclick="jobDel('${esc(j.name)}')">✕</button>
        </div></td></tr>`).join("") || `<tr><td colspan=6 class="empty">no schedules yet</td></tr>`}
    </tbody></table></div></div>`);
}
window.jobEdit = (j) => {
  j = j || { name: "", schedule: "0 3 * * *", image: "alpine:3.20", command: "" };
  modal(j.name ? "Edit · " + j.name : "New schedule", `
    <div class="f"><label>Name</label><input type="text" id="j_name" value="${esc(j.name)}" ${j.name ? "readonly" : ""} placeholder="nightly-prune"></div>
    <div class="f"><label>Cron schedule</label><input type="text" id="j_sched" value="${esc(j.schedule)}">
      <div class="dim xs" style="margin-top:6px">min hour day month weekday · e.g. <span class="mono">0 3 * * *</span> = 03:00 daily</div></div>
    <div class="f"><label>Image</label><input type="text" id="j_image" value="${esc(j.image || "alpine:3.20")}"></div>
    <div class="f"><label>Command</label><textarea id="j_cmd" rows="4" placeholder="echo hello">${esc(j.command || "")}</textarea></div>
    <label class="switch"><input type="checkbox" id="j_susp" ${j.suspend ? "checked" : ""}> Paused</label>
    <div class="row" style="margin-top:16px">
      <button class="btn pri" onclick="jobSave()">Save</button>
      <button class="btn" onclick="closeModal()">Cancel</button></div>`);
};
window.jobSave = async () => {
  const body = { name: $("#j_name").value.trim(), schedule: $("#j_sched").value.trim(),
    image: $("#j_image").value.trim(), command: $("#j_cmd").value, suspend: $("#j_susp").checked };
  if (!body.name || !body.command) return toast("name and command are required", "bad");
  try {
    await api("/api/schedules", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
    toast("schedule saved", "ok"); closeModal(); resetPaint(); viewSchedules();
  } catch (e) { toast(e.message, "bad"); }
};
window.jobRun = async name => {
  try { await api("/api/schedules/run", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ name }) });
    toast(`${name} started`, "ok"); } catch (e) { toast(e.message, "bad"); }
};
window.jobDel = async name => {
  if (!confirm(`Delete schedule "${name}"?`)) return;
  try { await api("/api/schedules/delete", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ name }) });
    toast("deleted", "ok"); resetPaint(); viewSchedules(); } catch (e) { toast(e.message, "bad"); }
};

/* ---------------- import ---------------- */
async function viewImport() {
  const [, nodes] = await Promise.all([loadHardwareFeatures(), api("/api/nodes").catch(() => [])]);
  if (nodes.length) STATE.data.nodes = nodes;
  const [srcs, jobs, disks, namespaces, storageClasses] = await Promise.all([
    api("/api/sources"), api("/api/imports").catch(() => []), api("/api/vm-disks").catch(() => []),
    api("/api/namespaces").catch(() => ["lab"]), api("/api/storageclasses").catch(() => ["longhorn-r2"]),
  ]);
  STATE.data.srcs = srcs; STATE.data.importNamespaces = namespaces; STATE.data.importStorageClasses = storageClasses;
  paint(`<div class="phead"><div><h2>Import</h2>
      <p>Bring containers, appdata and virtual-machine disks into Homestead</p></div>
      <div class="row"><button class="btn" data-need="admin" onclick="srcAdd()">＋ Container source</button>
      <button class="btn pri" data-need="admin" onclick="vmDiskImport()">＋ VM disk</button></div></div>

    <div class="sec">VM disk images ${tip("CDI downloads supported QEMU disk formats, including qcow2 and vmdk, converts them into a VM-ready disk, and writes the result into a new Longhorn PVC.")}</div>
    ${disks.length ? `<div class="card flat pad0"><div class="tblwrap"><table class="tbl"><thead><tr>
      <th>Disk / PVC</th><th>Capacity</th><th>Status</th><th>Progress</th><th>Attached to</th><th></th></tr></thead><tbody>
      ${disks.map(d => { const done = d.phase === "Succeeded", failed = ["Failed","Error","Unknown"].includes(d.phase); return `<tr>
        <td><b>${esc(d.name)}</b><div class="dim xs mono">${esc(d.namespace)} · ${esc(d.storage_class || "storage class unknown")}</div></td>
        <td class="mono small">${esc(d.capacity || "—")}<div class="dim xs">${esc((d.access_modes || []).join(", "))}</div></td>
        <td><span class="pill ${done ? "ok" : failed ? "crit" : "med"}">${esc(d.phase.replace(/([a-z])([A-Z])/g, "$1 $2"))}</span>${d.message ? `<div class="dim xs" style="margin-top:5px;max-width:320px">${esc(d.message)}</div>` : ""}</td>
        <td style="min-width:130px"><div class="jobmeter"><span class="${failed ? "failed" : ""}" style="width:${Math.max(2, done ? 100 : d.progress || 0)}%"></span></div><div class="dim xs mono">${done ? "ready" : `${esc(d.progress || 0)}%`}</div></td>
        <td class="small">${d.in_use ? esc((d.used_by || []).join(", ")) : '<span class="dim">not attached</span>'}</td>
        <td>${done && !d.in_use ? `<button class="btn sm" onclick="vmNew('${esc(d.name)}','${esc(d.namespace)}')">Create VM</button>` : ""}</td></tr>`; }).join("")}
      </tbody></table></div></div>` : `<div class="empty">No managed VM disk imports yet. Import an HTTP(S) qcow2, vmdk, raw, vdi, vhd or vhdx image into a new PVC.</div>`}

    <div class="sec">Container sources</div>
    <div class="grid g3">${srcs.map(s => `<div class="card flat">
      <div class="between"><div><div class="ctitle">${esc(s.name)}</div>
        <div class="csub">${esc(s.kind)} · ${esc(s.user)}@${esc(s.host)}</div></div>
        <button class="btn sm danger" onclick="srcDel('${esc(s.name)}')">✕</button></div>
      <div class="drow"><div class="dl">Base path</div><div class="dv mono small">${esc(s.base_path)}</div></div>
      <button class="btn wide" style="margin-top:12px" onclick="srcBrowse('${esc(s.name)}')">Browse appdata</button>
    </div>`).join("") || `<div class="empty">No import sources yet. Add the host you want to pull from.</div>`}</div>

    ${jobs.length ? `<div class="sec">Transfers</div>
    <div class="card flat pad0"><div class="tblwrap"><table class="tbl"><thead><tr>
      <th>App</th><th>Job</th><th>State</th><th>Started</th><th></th></tr></thead><tbody>
      ${jobs.map(j => `<tr><td><b>${esc(j.app || "—")}</b></td>
        <td class="mono small dim">${esc(j.name)}</td>
        <td><span class="pill ${j.state === "done" ? "ok" : j.state === "failed" ? "crit" : "med"}">${esc(j.state)}</span></td>
        <td class="small dim">${esc((j.start || "").replace("T", " ").replace("Z", ""))}</td>
        <td><button class="btn sm" onclick="jobLogs('lab','${esc(j.name)}')">Logs</button></td></tr>`).join("")}
    </tbody></table></div></div>` : ""}

    <div class="note" style="margin-top:20px">
      <b>Container import.</b> It creates a Longhorn volume, runs an rsync job that copies the remote
      appdata directory into it, and creates the workload pointing at that volume — left stopped so you can
      start it once the copy finishes. Path mappings from the source host do not carry over; the appdata
      lands at the mount path you choose.
    </div>`);
}
window.vmDiskImport = () => {
  const namespaces = STATE.data.importNamespaces || ["lab"];
  const classes = STATE.data.importStorageClasses || ["longhorn-r2"];
  modal("Import VM disk", `
    <div class="note"><b>CDI handles the conversion.</b> Provide a directly downloadable HTTP(S) qcow2, vmdk, raw, vdi, vhd or vhdx image. The source is streamed into a new Longhorn PVC; an existing disk is never overwritten.</div>
    <div class="f2" style="margin-top:16px">
      <div class="f"><label>Namespace ${tip("The imported DataVolume and its PVC are created here. A VM using the disk must be in the same namespace.")}</label><select id="vd_ns">${namespaces.map(n => `<option value="${esc(n)}" ${n === "lab" ? "selected" : ""}>${esc(n)}</option>`).join("")}</select></div>
      <div class="f"><label>Disk / PVC name ${tip("This becomes both the CDI DataVolume name and the resulting PVC name.")}</label><input id="vd_name" placeholder="ubuntu-server"></div>
    </div>
    <div class="f"><label>Image URL ${tip("HTTP and HTTPS only. URLs containing embedded usernames or passwords are rejected; use a Kubernetes Secret for authenticated endpoints.")}</label><input type="url" id="vd_url" placeholder="https://example.net/images/server.qcow2"></div>
    <div class="f2">
      <div class="f"><label>Capacity (GiB) ${tip("Must be at least as large as the image's virtual disk capacity. CDI expands the converted disk to fit this PVC.")}</label><input type="number" min="1" max="16384" id="vd_size" value="20"></div>
      <div class="f"><label>Storage class</label><select id="vd_sc">${classes.map(s => `<option value="${esc(s)}" ${s === "longhorn-r2" ? "selected" : ""}>${esc(s)}</option>`).join("")}</select></div>
    </div>
    <div class="f2">
      <div class="f"><label>Access mode ${tip("RWO is the normal choice for a VM boot disk. RWX allows several nodes to mount it but does not make concurrent VM writes safe.")}</label><select id="vd_mode"><option value="ReadWriteOnce">RWO · one node</option><option value="ReadWriteMany">RWX · many nodes</option></select></div>
      <div class="f"><label>Checksum (optional) ${tip("Recommended for downloaded images. Paste the publisher's SHA-256 or SHA-512 hexadecimal digest.")}</label><div class="row" style="gap:7px"><select id="vd_algo" style="max-width:110px"><option value="sha256">SHA-256</option><option value="sha512">SHA-512</option></select><input id="vd_checksum" class="mono" placeholder="hex digest"></div></div>
    </div>
    <details><summary class="small">Authenticated or private CA source</summary><div class="f2" style="margin-top:12px">
      <div class="f"><label>Credential Secret ${tip("Optional Secret in the destination namespace containing CDI-compatible accessKeyId and secretKey fields.")}</label><input id="vd_secret" placeholder="image-download-credentials"></div>
      <div class="f"><label>CA ConfigMap ${tip("Optional ConfigMap in the destination namespace containing the endpoint's CA certificate.")}</label><input id="vd_ca" placeholder="private-ca"></div></div></details>
    <div class="row" style="margin-top:18px"><button class="btn pri" onclick="doVmDiskImport()">Start import</button><button class="btn" onclick="closeModal()">Cancel</button></div>
    <div class="dim xs" style="margin-top:12px">Progress continues in the active-jobs tray after this dialog closes. Source URLs are not copied into Homestead's operation history.</div>`, true);
};
window.doVmDiskImport = async () => {
  const digest = $("#vd_checksum").value.trim();
  const body = { namespace: $("#vd_ns").value, name: $("#vd_name").value.trim(),
    source_url: $("#vd_url").value.trim(), size_gb: +$("#vd_size").value,
    storage_class: $("#vd_sc").value, access_mode: $("#vd_mode").value,
    checksum: digest ? `${$("#vd_algo").value}:${digest}` : "",
    secret_ref: $("#vd_secret").value.trim(), cert_config_map: $("#vd_ca").value.trim() };
  if (!body.name || !body.source_url) return toast("disk name and image URL are required", "bad");
  try {
    const plan = await api(`/api/vm-disks/import-plan?ns=${encodeURIComponent(body.namespace)}&name=${encodeURIComponent(body.name)}`);
    if (!plan.ready) return toast(plan.message, "bad");
    const result = await api("/api/vm-disks/import", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
    toast(result.message, "ok"); closeModal(); resetPaint(); viewImport();
  } catch (e) { toast(e.message, "bad"); }
};
window.srcAdd = () => modal("Add import source", `
  <div class="f"><label>Name</label><input type="text" id="sc_name" placeholder="unraid"></div>
  <div class="f2">
    <div class="f"><label>Host</label><input type="text" id="sc_host" placeholder="192.0.2.177"></div>
    <div class="f"><label>Type</label><select id="sc_kind">
      <option value="unraid">Unraid</option><option value="proxmox">Proxmox</option><option value="ssh">Generic SSH</option></select></div>
  </div>
  <div class="f2">
    <div class="f"><label>Username</label><input type="text" id="sc_user" value="root"></div>
    <div class="f"><label>Password</label><input type="password" id="sc_pass"></div>
  </div>
  <div class="f"><label>Appdata base path</label><input type="text" id="sc_path" value="/mnt/user/appdata"></div>
  <div class="dim xs" style="margin-bottom:12px">Unraid® is a registered trademark of Lime Technology, Inc. This application is not affiliated with, endorsed, or sponsored by Lime Technology, Inc.</div>
  <div class="row" style="margin-top:16px">
    <button class="btn pri" onclick="doSrcAdd()">Add source</button>
    <button class="btn" onclick="closeModal()">Cancel</button></div>
  <div class="note" style="margin-top:14px">The password is stored in a Kubernetes Secret in the
  <span class="mono">lab</span> namespace, not in the ConfigMap. Anyone with cluster access can read it.</div>`);
window.doSrcAdd = async () => {
  const body = { name: $("#sc_name").value.trim(), host: $("#sc_host").value.trim(),
    user: $("#sc_user").value.trim(), password: $("#sc_pass").value,
    kind: $("#sc_kind").value, base_path: $("#sc_path").value.trim() };
  if (!body.name || !body.host) return toast("name and host are required", "bad");
  try {
    await api("/api/sources", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
    toast("source added", "ok"); closeModal(); resetPaint(); viewImport();
  } catch (e) { toast(e.message, "bad"); }
};
window.srcDel = async name => {
  if (!confirm(`Remove import source "${name}"?`)) return;
  try { await api("/api/sources/delete", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ name }) });
    toast("removed", "ok"); resetPaint(); viewImport(); } catch (e) { toast(e.message, "bad"); }
};
window.srcBrowse = async name => {
  modal("Browse · " + name, `<div class="empty"><span class="spin2"></span>connecting to host — this runs a
    one-shot pod, so it takes ~20s</div>`, true);
  try {
    const [r, dc] = await Promise.all([
      api("/api/sources/browse", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ name }) }),
      api("/api/sources/containers", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ name }) }).catch(() => ({ containers: [] })),
    ]);
    const src = (STATE.data.srcs || []).find(s => s.name === name) || {};
    const entries = r.entries.filter(e => !e.startsWith("==") && !/^(WARNING|Permission|Warning)/i.test(e));
    $("#mbody").innerHTML = `${dc.containers.length ? `<div class="ctitle">Docker containers found</div>
      <p class="muted small">Choose a container to carry over its image, ports, environment variables, icon and appdata mount automatically.</p>
      <div class="apps importapps" style="margin-top:14px">${dc.containers.map(c => `<div class="card flat importapp">
        <div class="between"><b>${esc(c.name)}</b><span class="pill ${c.state === "running" ? "ok" : "low"}">${esc(c.state || "unknown")}</span></div>
        <div class="mono xs dim" style="margin-top:7px;word-break:break-all">${esc(c.image)}</div>
        <button class="btn pri wide sm" style="margin-top:10px" onclick="inspectImport('${esc(name)}','${esc(c.name)}')">Import container</button></div>`).join("")}</div>
      <div class="sec">Appdata folders</div>` : ""}` + (entries.length
      ? `<p class="muted small">${entries.length} directories under <span class="mono">${esc(src.base_path)}</span>. Use this when Docker metadata is unavailable.</p>
         <div class="apps" style="margin-top:14px;grid-template-columns:repeat(auto-fill,minmax(200px,1fr))">
         ${entries.map(e => `<div class="card flat" style="padding:13px">
           <div style="font-weight:660;word-break:break-all">${esc(e)}</div>
           <button class="btn wide sm" style="margin-top:10px"
             onclick="importSetup('${esc(name)}','${esc(e)}')">Import</button></div>`).join("")}</div>`
      : `<div class="empty">No appdata folders returned. Check the credentials and that
         <span class="mono">${esc(src.base_path)}</span> exists on ${esc(src.host)}.</div>`);
  } catch (e) { $("#mbody").innerHTML = `<div class="empty">${esc(e.message)}</div>`; }
};

window.imageCleanupReview = digest => {
  const image = (STATE.data.imageCache?.images || []).find(row => row.digest === digest);
  if (!image) return toast("refresh Image Cache before cleaning this image", "bad");
  if (image.protected || image.system) return toast("this image is protected and cannot be cleaned", "bad");
  const perNode = image.size_mb >= 1024 ? `${(image.size_mb / 1024).toFixed(1)} GB` : `${image.size_mb} MB`;
  modal("Clean cached image", `<div class="update-review">
    <div class="note dependency-danger"><b>This deletes only the cached image layers, not a workload.</b>
      A future start may be slower while the registry image is downloaded again. Active, immediate rollback, and platform images are blocked server-side.</div>
    <div class="update-image"><div><span>Image</span><code>${esc(image.name)}</code></div>
      <div><span>Digest</span><code>${esc(digest)}</code></div><div><span>Per node</span><code>${esc(perNode)}</code></div></div>
    <div class="f"><label>Remove from nodes</label><div class="cleanup-nodes">${image.nodes.map(node =>
      `<label class="switch"><input class="cleanup-node" type="checkbox" value="${esc(node)}" checked onchange="imageCleanupGate()"> ${esc(node)}</label>`).join("")}</div></div>
    <div class="f"><label>Type CLEAN to confirm</label><input id="cleanupConfirm" autocomplete="off" oninput="imageCleanupGate()" placeholder="CLEAN"></div>
    <div class="row"><button id="cleanupGo" class="btn danger" data-need="admin" disabled onclick="imageCleanupApply('${esc(digest)}')">Remove cached image</button>
      <button class="btn" onclick="closeModal()">Cancel</button></div></div>`, true);
  if (window.applyRole) window.applyRole();
};
window.imageCleanupGate = () => {
  const button = $("#cleanupGo");
  if (button) button.disabled = $("#cleanupConfirm")?.value !== "CLEAN" || !$(".cleanup-node:checked");
};
window.imageCleanupApply = async digest => {
  const nodes = $$(".cleanup-node:checked").map(input => input.value);
  try {
    const result = await api("/api/images/cleanup", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ digest, nodes }) });
    toast(`cleanup started on ${result.nodes.length} node${result.nodes.length === 1 ? "" : "s"}`, "ok");
    closeModal();
  } catch (error) { toast(error.message, "bad"); }
};
window.inspectImport = async (source, container) => {
  $("#mbody").innerHTML = '<div class="empty"><span class="spin2"></span>reading Docker configuration…</div>';
  try {
    const cfg = await api("/api/sources/inspect", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name: source, container }) });
    importSetup(source, container, cfg);
  } catch (e) { $("#mbody").innerHTML = `<div class="empty"><b>Could not inspect ${esc(container)}</b><br><span class="dim small">${esc(e.message)}</span></div>`; }
};
window.importSetup = (source, dir, cfg = {}) => {
  const src = (STATE.data.srcs || []).find(s => s.name === source) || {};
  const name = (cfg.name || dir).toLowerCase().replace(/[^a-z0-9-]/g, "-").replace(/^-+|-+$/g, "").slice(0, 38);
  STATE.data.importCfg = cfg;
  modal("Import · " + dir, `
    <div class="f2">
      <div class="f"><label>Workload name</label><input type="text" id="im_name" value="${esc(name)}"></div>
      <div class="f"><label>Volume size (GB)</label><input type="number" id="im_size" value="10" min="1"></div>
    </div>
    <div class="f"><label>Remote path</label>
      <input type="text" id="im_path" value="${esc(cfg.remote_path || ((src.base_path || "") + "/" + dir))}"></div>
    <div class="f"><label>Docker image ${tip("Read from Docker on the source host. You can change the tag before importing.")}</label>
      <input type="text" id="im_image" value="${esc(cfg.image || "")}" placeholder="lscr.io/linuxserver/${esc(name)}:latest"></div>
    <div class="f"><label>Logo URL</label><input type="url" id="im_icon" value="${esc(cfg.icon || "")}" placeholder="https://…/icon.png"></div>
    <div class="sec">Hardware requirements ${tip("Docker device mappings are pre-selected. Add or remove features before import; placement will be limited to nodes that provide every selected feature.")}</div>
    <div class="hwchoices">${hardwareChoices("im_hw", cfg.hardware || [])}</div>
    <div class="f"><label>Mount appdata inside the container ${tip("This is the path the app sees inside the container, usually /config. The copied files themselves live on a Longhorn volume, not at this path on a Harvester node.")}</label>
      <input type="text" id="im_mount" value="${esc(cfg.mount_path || "/config")}">
      <div class="dim xs" style="margin-top:6px">Source files → Longhorn PVC <span class="mono">${esc(name)}-appdata</span> → this path inside the container.</div></div>
    <div class="sec">Network</div><div class="f2"><div class="f"><label>Docker network → Kubernetes</label><select id="im_net"><option value="loadbalancer">LAN access (VIP)</option><option value="internal">Cluster only</option><option value="host" ${cfg.network_mode === "host" ? "selected" : ""}>Host network (advanced)</option></select></div>
      <div class="f"><label>VIP allocation ${tip("Choose a new automatic or specific VIP for apps such as Pi-hole that need port 53 on their own address.")}</label><select id="im_vip"><option value="shared">Shared Homestead VIP</option><option value="auto">New automatic VIP</option><option value="manual">Specific VIP</option></select></div></div>
    <div class="f"><label>Specific VIP (only for manual)</label><input id="im_ip" placeholder="192.0.2.250"></div>
    <div class="sec">Port mappings ${tip("Container port is what the app listens on. LAN port is what you open from another device. TCP and UDP mappings are kept separately.")}</div>
    <div id="im_ports">${(cfg.ports || []).map(p => `<div class="f4 im-port"><div><label>Container</label><input class="ipc" type="number" value="${p.container}"></div><div><label>LAN</label><input class="iph" type="number" value="${p.host}"></div><div><label>Protocol</label><select class="ipp"><option ${p.protocol === "TCP" ? "selected" : ""}>TCP</option><option ${p.protocol === "UDP" ? "selected" : ""}>UDP</option></select></div><label class="switch"><input class="ipe" type="checkbox" ${p.expose !== false ? "checked" : ""}>Expose</label></div>`).join("")}</div>
    <button class="btn sm" onclick="imAddPort()">＋ add port</button>
    <div class="sec">Environment variables ${tip("Copied from Docker inspect. Review secrets and host-specific paths before starting the imported app.")}</div>
    <div id="im_env">${Object.entries(cfg.env || {}).map(([k,v]) => `<div class="f2 im-env"><div class="f"><label>Variable</label><input class="iek" value="${esc(k)}"></div><div class="f"><label>Value</label><input class="iev" value="${esc(v)}"></div></div>`).join("")}</div>
    <button class="btn sm" onclick="imAddEnv()">＋ add variable</button>
    ${(cfg.mounts || []).filter(m => m.source !== cfg.remote_path).length ? `<div class="note" style="margin-top:14px"><b>Other Docker paths need review.</b> This import copies the appdata path only. Docker also mounted: ${(cfg.mounts || []).filter(m => m.source !== cfg.remote_path).map(m => `<span class="mono">${esc(m.source)} → ${esc(m.path)}</span>`).join(", ")}</div>` : ""}
    <label class="switch"><input type="checkbox" id="im_start" checked> Leave stopped until the copy finishes</label>
    <div class="row" style="margin-top:16px">
      <button class="btn pri" onclick="doImport('${esc(source)}')">Start import</button>
      <button class="btn" onclick="closeModal()">Cancel</button></div>
    <div class="note" style="margin-top:14px">The copy runs as a Job — you can close this and watch it
    on the Import page. Large appdata directories can take a while.</div>`, true);
};
window.imAddPort = () => { $("#im_ports").insertAdjacentHTML("beforeend", '<div class="f4 im-port"><div><label>Container</label><input class="ipc" type="number"></div><div><label>LAN</label><input class="iph" type="number"></div><div><label>Protocol</label><select class="ipp"><option>TCP</option><option>UDP</option></select></div><label class="switch"><input class="ipe" type="checkbox" checked>Expose</label></div>'); };
window.imAddEnv = () => { $("#im_env").insertAdjacentHTML("beforeend", '<div class="f2 im-env"><div class="f"><label>Variable</label><input class="iek"></div><div class="f"><label>Value</label><input class="iev"></div></div>'); };
window.doImport = async source => {
  const env = {}; $$(".im-env").forEach(r => { const k = $(".iek", r).value.trim(); if (k) env[k] = $(".iev", r).value; });
  const cfg = STATE.data.importCfg || {};
  const body = { source, name: $("#im_name").value.trim(), remote_path: $("#im_path").value.trim(),
    image: $("#im_image").value.trim(), icon: $("#im_icon").value.trim(), mount_path: $("#im_mount").value.trim(),
    size_gb: +$("#im_size").value, start_after_copy: $("#im_start").checked,
    ports: $$(".im-port").map(r => ({ container: +$(".ipc", r).value, host: +$(".iph", r).value || +$(".ipc", r).value, protocol: $(".ipp", r).value, expose: $(".ipe", r).checked })).filter(p => p.container),
    env, hardware: selectedHardware("im_hw"), network_mode: $("#im_net").value, vip_mode: $("#im_vip").value, lb_ip: $("#im_ip").value.trim() };
  if (!body.name || !body.image) return toast("workload name and image are required", "bad");
  try {
    const r = await api("/api/import", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
    toast(`import started (${r.job})`, "ok"); closeModal(); resetPaint(); viewImport();
  } catch (e) { toast(e.message, "bad"); }
};
