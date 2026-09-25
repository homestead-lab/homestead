/* Virtual machines: each one's real state and the actions that fit it, a
   page per VM (disks, network, guest, events), editing and deleting. */

const VM_TONE = { Running: "ok", Stopped: "low", Paused: "info", Migrating: "info", Starting: "med", Stopping: "med", Deleting: "med",
  Provisioning: "med", WaitingForVolumeBinding: "med" };
const VM_ACTIONS = {
  start: ["Start", "play", "Boot the VM"], stop: ["Stop", "stop", "Shut the VM down: the guest is asked to power off, then it is stopped"],
  restart: ["Restart", "restart", "Restart the VM"], pause: ["Pause", "stop", "Freeze the VM where it is, keeping its memory"],
  unpause: ["Resume", "play", "Carry on from where it was paused"], "force-stop": ["Force stop", "stop", "Stop at once, without asking the guest: like pulling the plug"],
};
const vmTone = status => VM_TONE[status] || (/Error|Fail|Crash|BackOff/i.test(status) ? "crit" : "med");
/* A disk CDI is still filling: what a Provisioning VM is waiting for. */
const VM_FILL_WORDS = { ImageDownloading: "Harvester is downloading the image for", ImportInProgress: "Downloading", CloneInProgress: "Copying", ImportScheduled: "Waiting to start the download",
  CloneScheduled: "Waiting to start the copy", Pending: "Waiting for its volume", WaitForFirstConsumer: "Waiting for the VM to be placed",
  PendingPopulation: "Waiting for its volume", Failed: "Failed" };
const vmWaited = s => s >= 3600 ? `${Math.floor(s / 3600)} h ${Math.floor(s % 3600 / 60)} min` : s >= 60 ? `${Math.floor(s / 60)} min` : `${s} s`;
const vmFilling = v => (v.filling || []).map(f => `<div class="vm-filling ${f.stuck ? "stuck" : ""}"><span>${esc(VM_FILL_WORDS[f.phase] || f.phase)} <b class="mono">${esc(f.claim)}</b>
    ${f.seconds ? `<span class="dim xs">· ${esc(vmWaited(f.seconds))}</span>` : ""}</span>
  ${f.progress != null ? `<span class="mono">${f.progress.toFixed(1)}%</span>` : ""}
  <div class="rollout-meter"><span style="width:${Math.max(2, f.progress || 0)}%"></span></div>
  ${f.why?.length ? `<div class="note ${f.stuck ? "warn" : ""} vm-why">${f.stuck ? "<b>Stuck.</b> " : ""}${f.why.map(w => esc(w)).join("<br>")}</div>`
    : f.stuck ? '<div class="note warn vm-why"><b>Stuck</b>, and CDI says nothing about why. The events of its importer pod, on the Resources page, may.</div>' : ""}</div>`).join("");

async function viewVMs() {
  if (platformLacks("kubevirt", "Virtual machines")) return;
  const vms = await api("/api/vms");
  STATE.data.vms = vms;
  const q = STATE.q.toLowerCase();
  const rows = vms.filter(v => !q || [v.name, v.ns, v.os, v.ip, v.node, v.description].join(" ").toLowerCase().includes(q));
  const running = vms.filter(v => v.status === "Running").length;
  paint(`<div class="phead"><div><h2>Virtual machines</h2>
      <p>${vms.length} VM${vms.length === 1 ? "" : "s"} · ${running} running · ${STATE.platform?.harvester === false ? `KubeVirt on ${esc(platformName(STATE.platform))}${STATE.platform.cdi ? "" : " · no CDI"}` : "KubeVirt on Harvester"}</p></div>
      <div class="row"><button class="btn" data-need="operator" onclick="k3sCluster()" title="A k3s cluster made of VMs here, each with an address of its own">＋ k3s cluster</button>
      <button class="btn pri" data-need="operator" onclick="vmNew()">＋ New VM</button></div></div>
    ${rows.length ? `<div class="vm-grid">${rows.map(vmCard).join("")}</div>`
      : `<div class="empty">${q ? "Nothing matches that search." : "No virtual machines yet — create one to get started."}</div>`}`);
}
window.viewVMs = viewVMs;

function vmActionButton(v, action, primary = false) {
  const [label, iconName, title] = VM_ACTIONS[action];
  return `<button class="btn sm ${primary ? "pri" : ""}" data-need="operator" title="${esc(title)}" onclick="vmPower('${esc(v.ns)}','${esc(v.name)}','${action}')">${icon(iconName)}${label}</button>`;
}

function vmCard(v) {
  const main = v.actions.filter(a => ["start", "stop", "restart", "unpause"].includes(a));
  const disk = v.disks.filter(d => d.kind === "disk");
  return `<div class="card flat vm-card">
    <div class="between vm-head">
      <a class="vm-title" onclick="vmOpen('${esc(v.ns)}','${esc(v.name)}')"><div class="av n3">${esc(v.name.slice(0, 2).toUpperCase())}</div>
        <div><b>${esc(v.name)}</b><div class="dim xs">${esc(v.ns)}${v.os ? ` · ${esc(v.os)}` : ""}</div></div></a>
      <span class="pill ${vmTone(v.status)}" ${v.problem ? `data-tip="${esc(v.problem)}"` : ""}>${esc(v.status)}</span></div>
    ${v.description ? `<div class="dim small vm-desc">${esc(v.description)}</div>` : ""}
    ${v.problem ? `<div class="note bad vm-problem">${esc(v.problem)}</div>` : ""}
    ${vmFilling(v)}
    ${v.restart_required ? '<div class="dim xs vm-restart">Changes are waiting for a restart</div>' : ""}
    <div class="vm-facts">
      <div><span>CPU</span><b>${v.cores} core${v.cores === 1 ? "" : "s"}</b></div>
      <div><span>RAM</span><b>${esc(v.memory || "—")}</b></div>
      <div><span>IP</span><b class="mono">${esc(v.ip || "—")}</b></div>
      <div><span>Host</span><b>${esc(v.node || "—")}</b></div>
      <div><span>Disks</span><b>${disk.length}${disk.length ? ` · ${esc(disk.map(d => d.size).filter(Boolean).join(" + "))}` : ""}</b></div>
    </div>
    <div class="row vm-actions">
      ${main.map((a, i) => vmActionButton(v, a, i === 0 && a === "start")).join("")}
      ${v.actions.includes("console") ? `<button class="btn sm" data-need="operator" onclick="vmConsole('${esc(v.ns)}','${esc(v.name)}')">${icon("console")}Console</button>` : ""}
      <details class="actionmenu"><summary class="btn sm" title="More actions">⋯</summary><div class="actionmenu-pop">
        <button onclick="this.closest('details').open=false;vmOpen('${esc(v.ns)}','${esc(v.name)}')">${icon("list")}Details</button>
        <button data-need="operator" onclick="this.closest('details').open=false;vmEdit('${esc(v.ns)}','${esc(v.name)}')">${icon("edit")}Edit</button>
        ${v.actions.includes("pause") ? `<button data-need="operator" onclick="this.closest('details').open=false;vmPower('${esc(v.ns)}','${esc(v.name)}','pause')">${icon("stop")}Pause</button>` : ""}
        ${v.actions.includes("migrate") ? `<button data-need="operator" onclick="this.closest('details').open=false;vmMove('${esc(v.ns)}','${esc(v.name)}')">${icon("move")}Move host</button>` : ""}
        ${v.actions.includes("force-stop") ? `<button class="danger" data-need="operator" onclick="this.closest('details').open=false;vmPower('${esc(v.ns)}','${esc(v.name)}','force-stop')">${icon("stop")}Force stop</button>` : ""}
        <button class="danger" data-need="admin" onclick="this.closest('details').open=false;vmDelete('${esc(v.ns)}','${esc(v.name)}')">${icon("trash")}Delete</button>
      </div></details></div></div>`;
}

window.vmPower = async (ns, name, action) => {
  if (action === "force-stop" && !confirm(`Force stop ${name}? The guest is not asked to shut down, so unsaved work in it is lost.`)) return;
  try {
    const r = await api("/api/vm/power", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ ns, name, action }) });
    toast(r.detail, "ok"); setTimeout(() => refresh(true), 1200);
  } catch (e) { toast(e.message, "bad"); }
};

window.vmOpen = async (ns, name) => {
  modal(`VM · ${name}`, `<div class="empty"><span class="spin2"></span>loading</div>`, true);
  let v;
  try { v = await api(`/api/vm?ns=${encodeURIComponent(ns)}&name=${encodeURIComponent(name)}`); }
  catch (e) { $("#mbody").innerHTML = `<div class="empty">${esc(e.message)}</div>`; return; }
  const tab = (id, label) => `<button class="${id === "overview" ? "on" : ""}" onclick="vmTab(this,'${id}')">${label}</button>`;
  $("#mbody").innerHTML = `<div class="between"><div><span class="pill ${vmTone(v.status)}">${esc(v.status)}</span>
      <span class="dim xs"> run strategy ${esc(v.run_strategy)}${v.node ? ` · on ${esc(v.node)}` : ""}</span></div>
      <div class="row">${v.actions.filter(a => VM_ACTIONS[a]).map(a => vmActionButton(v, a, a === "start")).join("")}
        <button class="btn sm" data-need="operator" onclick="vmEdit('${esc(ns)}','${esc(name)}')">${icon("edit")}Edit</button></div></div>
    ${v.problem ? `<div class="note bad" style="margin-top:10px">${esc(v.problem)}</div>` : ""}
    ${vmFilling(v)}
    <div class="seg" style="margin:12px 0">${tab("overview", "Overview")}${tab("disks", `Disks · ${v.disks.length}`)}${tab("network", `Network · ${v.nics.length}`)}${tab("events", "Events")}</div>
    <div class="vm-pane" data-pane="overview">
      <div class="vm-facts wide">
        <div><span>CPU</span><b>${v.cores} cores</b></div><div><span>Memory</span><b>${esc(v.memory || "—")}</b></div>
        <div><span>Guest OS</span><b>${esc(v.guest?.prettyName || v.os || "unknown")}</b></div>
        <div><span>Kernel</span><b class="mono xs">${esc(v.guest?.kernelRelease || "—")}</b></div>
        <div><span>Live migration</span><b>${v.migratable ? "possible" : "not possible"}</b></div>
        <div><span>Created</span><b>${esc((v.created || "").slice(0, 10))}</b></div></div>
      ${v.description ? `<p class="small" style="margin-top:10px">${esc(v.description)}</p>` : ""}
      ${v.guest?.prettyName ? "" : '<p class="dim xs" style="margin-top:8px">The guest reports its OS and addresses through the QEMU guest agent, when it runs one.</p>'}
      <table class="tbl dense stack" style="margin-top:10px"><thead><tr><th>Condition</th><th>Status</th><th>Detail</th></tr></thead><tbody>
        ${v.conditions.map(c => `<tr><td>${esc(c.type)}</td><td><span class="pill slim ${c.status === "True" ? "ok" : "neutral"}">${esc(c.status)}</span></td><td class="small">${esc(c.message || c.reason)}</td></tr>`).join("")}</tbody></table></div>
    <div class="vm-pane" data-pane="disks" hidden><table class="tbl dense stack"><thead><tr><th>Disk</th><th>Kind</th><th>Volume</th><th>Size</th><th>Boot</th></tr></thead><tbody>
      ${v.disks.map(d => `<tr><td><b>${esc(d.name)}</b><div class="dim xs">${esc(d.bus)}</div></td><td>${esc(d.kind)}</td>
        <td class="mono small">${esc(d.claim || "—")}${d.storage_class ? `<div class="dim xs">${esc(d.storage_class)}</div>` : ""}</td>
        <td class="mono">${esc(d.size || "—")}</td><td>${d.boot ? `#${d.boot}` : ""}</td></tr>`).join("")}</tbody></table></div>
    <div class="vm-pane" data-pane="network" hidden><table class="tbl dense stack"><thead><tr><th>Interface</th><th>Network</th><th>MAC</th><th>Addresses</th></tr></thead><tbody>
      ${v.nics.map(n => `<tr><td><b>${esc(n.name)}</b><div class="dim xs">${esc(n.model)}</div></td><td>${esc(n.network || "—")}</td>
        <td class="mono xs">${esc(n.mac || "—")}</td><td class="mono small">${esc(n.ips.join(", ") || "—")}</td></tr>`).join("")}</tbody></table></div>
    <div class="vm-pane" data-pane="events" hidden>${v.events.length ? `<table class="tbl dense stack"><thead><tr><th>Type</th><th>Reason</th><th>Message</th><th>Last</th></tr></thead><tbody>
      ${v.events.map(e => `<tr><td><span class="pill slim ${e.type === "Warning" ? "med" : "neutral"}">${esc(e.type)}</span></td><td class="small">${esc(e.reason)}</td>
        <td class="small">${esc(e.message)}</td><td class="small dim">${esc((e.last || "").replace("T", " ").slice(0, 16))}</td></tr>`).join("")}</tbody></table>` : '<div class="dim small">No recent events.</div>'}</div>`;
  if (window.applyRole) applyRole();
};
window.vmTab = (button, pane) => {
  $$("#mbody .seg button").forEach(b => b.classList.toggle("on", b === button));
  $$("#mbody .vm-pane").forEach(p => { p.hidden = p.dataset.pane !== pane; });
};

/* ---- editing: everything the VM is made of ---- */
const VM_BUSES = ["virtio", "sata", "scsi"], VM_MODELS = ["virtio", "e1000", "e1000e", "rtl8139"];
const vmNet = n => n.network === "pod network" ? "pod" : n.network;
const vmOpt = (value, label, chosen) => `<option value="${esc(value)}" ${value === chosen ? "selected" : ""}>${esc(label)}</option>`;

/* Where a disk's contents come from: blank, a download, or a Harvester image. */
function vmSourceSelect(cls, o, current = "") {
  return `<select class="${cls}" onchange="vmSourceChanged(this)">
    ${vmOpt("blank", "blank disk", current)}
    ${o.cdi || o.harvester ? vmOpt("url", o.harvester ? "download from a URL (as a Harvester image)" : "download from a URL", current) : ""}
    ${(o.images || []).filter(i => i.storage_class).map(i => vmOpt(`image:${i.namespace}/${i.name}`, `Harvester image · ${i.display}`, current)).join("")}</select>`;
}
window.vmSourceChanged = select => {
  const url = select.closest("[data-disk],.vd-add")?.querySelector(".vd_url,.va_url");
  if (url) url.hidden = select.value !== "url";
};

function vmDiskRow(d, o) {
  const cdrom = d.kind === "cd-rom", unmade = !d.made && d.template;
  const current = d.source?.url !== undefined ? "url" : d.source?.image ? `image:${d.source.image}` : "blank";
  const buses = cdrom ? VM_BUSES.filter(b => b !== "virtio") : VM_BUSES;
  return `<tr data-disk="${esc(d.name)}" data-size="${esc(d.size || d.template_size || "")}" data-source="${esc(current)}" data-url="${esc(d.source?.url || "")}">
    <td><b>${esc(d.name)}</b><div class="dim xs">${esc(d.kind)}${d.claim ? ` · <span class="mono">${esc(d.claim)}</span>` : ""}</div>
      ${d.storage_class || d.template_class ? `<div class="dim xs">${esc(d.storage_class || d.template_class)}</div>` : ""}
      ${unmade ? `<span class="pill slim crit" data-tip="This disk has not been made${d.phase ? ` (${esc(d.phase)})` : ""}, so its source can still be changed">not made</span>` : ""}</td>
    <td><input class="vd_boot mono" type="number" min="1" max="64" value="${d.boot || ""}" placeholder="—" style="width:64px"></td>
    <td><select class="vd_bus">${buses.map(b => vmOpt(b, b, d.bus)).join("")}</select></td>
    <td>${d.claim ? `<input class="vd_size mono" value="${esc(d.size || d.template_size || "")}" style="width:84px" data-tip="${unmade ? "Its size when it is made" : "Grow the disk; it cannot shrink"}">` : "—"}</td>
    <td>${unmade ? `${vmSourceSelect("vd_src", o, current)}<input class="vd_url mono" type="url" placeholder="https://…/image.img" value="${esc(d.source?.url || "")}" ${current === "url" ? "" : "hidden"} style="margin-top:6px">`
      : `<span class="dim xs">${d.source?.image ? esc(d.source.image) : d.source?.url ? "downloaded" : d.claim ? "made" : "—"}</span>`}</td>
    <td><label class="switch" data-tip="Detach it from the VM; the volume is kept"><input type="checkbox" class="vd_rm"> detach</label></td></tr>`;
}

function vmNicRow(n, o) {
  const nets = [...new Set([...(o.networks || ["pod"]), vmNet(n)].filter(Boolean))];
  return `<tr data-nic="${esc(n.name)}" data-model="${esc(n.model)}" data-net="${esc(vmNet(n))}" data-mac="${esc(n.mac || "")}">
    <td><b>${esc(n.name)}</b><div class="dim xs mono">${esc(n.ips.join(", "))}</div></td>
    <td><select class="vn_model">${VM_MODELS.map(m => vmOpt(m, m, n.model)).join("")}</select></td>
    <td><select class="vn_net">${nets.map(x => vmOpt(x, x === "pod" ? "pod network (NAT)" : x, vmNet(n))).join("")}</select></td>
    <td><input class="vn_mac mono" value="${esc(n.mac || "")}" placeholder="automatic" style="width:150px"></td>
    <td><label class="switch"><input type="checkbox" class="vn_rm"> remove</label></td></tr>`;
}

window.vmEdit = async (ns, name) => {
  modal(`Edit · ${name}`, `<div class="empty"><span class="spin2"></span>loading</div>`, true);
  let v, o;
  try {
    [v, o] = await Promise.all([api(`/api/vm?ns=${encodeURIComponent(ns)}&name=${encodeURIComponent(name)}`),
      api("/api/vm/create-options").catch(() => ({ cdi: true, images: [], storage_classes: [], networks: ["pod"], nodes: [] }))]);
  } catch (e) { $("#mbody").innerHTML = `<div class="empty">${esc(e.message)}</div>`; return; }
  window.__vmEdit = { ns, name, v, o };
  const tab = (id, label) => `<button class="${id === "general" ? "on" : ""}" onclick="vmEditTab(this,'${id}')">${label}</button>`;
  const disks = v.disks.filter(d => d.kind === "disk" || d.kind === "cd-rom");
  const ci = v.cloud_init || {};
  $("#mbody").innerHTML = `<div class="between"><div class="seg">${tab("general", "General")}${tab("disks", `Disks · ${disks.length}`)}${tab("network", `Network · ${v.nics.length}`)}${tab("cloud", "Cloud-init")}</div>
      <button class="btn sm" data-need="admin" onclick="vmYaml('${esc(ns)}','${esc(name)}')" title="Every field, as YAML">${icon("edit")}Edit YAML</button></div>
    <div class="ve-pane" data-pane="general" style="margin-top:12px">
      <div class="f2"><div class="f"><label>CPU cores</label><input id="ve_cores" type="number" min="1" max="128" value="${v.cores}"></div>
        <div class="f"><label>Memory</label><input id="ve_mem" class="mono" value="${esc(v.memory)}" placeholder="4Gi"></div></div>
      <div class="f2"><div class="f"><label>Run strategy ${tip("RerunOnFailure (Harvester's default): runs, and starts again if the guest crashes, but not after you stop it. Always: kept running whatever happens. Manual: runs only when started, never restarted. Halted: kept off.")}</label>
        <select id="ve_strategy">${["RerunOnFailure", "Always", "Manual", "Halted"].map(x => vmOpt(x, x, v.run_strategy)).join("")}</select></div>
        <div class="f"><label>Host ${tip("Keep the VM on one host, or let Kubernetes choose. A VM on a disk only one host can reach stays there anyway.")}</label>
        <select id="ve_node">${vmOpt("", "any host", v.node_selector || "")}${(o.nodes || []).map(n => vmOpt(n, n, v.node_selector || "")).join("")}</select></div></div>
      <div class="f"><label>Description</label><input id="ve_desc" value="${esc(v.description || "")}" maxlength="300"></div></div>
    <div class="ve-pane" data-pane="disks" hidden style="margin-top:12px">
      <div class="tblwrap"><table class="tbl dense stack ve-table"><thead><tr><th>Disk</th><th>Boot</th><th>Bus</th><th>Size</th><th>Source</th><th></th></tr></thead>
        <tbody>${disks.map(d => vmDiskRow(d, o)).join("")}</tbody></table></div>
      <div id="ve_adds"></div>
      <div class="row" style="margin-top:10px"><button class="btn sm" onclick="vmAddDisk('disk')">＋ Disk</button><button class="btn sm" onclick="vmAddDisk('cd-rom')">＋ CD-ROM</button></div>
      <div class="dim xs" style="margin-top:8px">Boot order: the lowest number boots first. Detached disks are kept as volumes.</div></div>
    <div class="ve-pane" data-pane="network" hidden style="margin-top:12px">
      <div class="tblwrap"><table class="tbl dense stack ve-table"><thead><tr><th>Interface</th><th>Model</th><th>Network</th><th>MAC</th><th></th></tr></thead>
        <tbody id="ve_nics">${v.nics.map(n => vmNicRow(n, o)).join("")}</tbody></table></div>
      <div class="row" style="margin-top:10px"><button class="btn sm" onclick="vmAddNic()">＋ Interface</button></div>
      <div class="dim xs" style="margin-top:8px">The pod network reaches out through NAT; a network attachment puts the VM on that VLAN or bridge with its own address.</div></div>
    <div class="ve-pane" data-pane="cloud" hidden style="margin-top:12px">
      ${ci.source === "unreadable" ? `<div class="note bad">This VM's cloud-init is in a secret Homestead cannot read, so it is left as it is.</div>` : `
      ${ci.source === "secret" ? '<div class="dim xs" style="margin-bottom:8px">Kept in the VM\'s own secret, as Harvester does.</div>' : ""}
      <div class="f"><label>User data</label><textarea id="ve_user" class="mono helm-values" spellcheck="false" placeholder="#cloud-config">${esc(ci.user_data || "")}</textarea></div>
      <div class="f"><label>Network data</label><textarea id="ve_netdata" class="mono helm-values" spellcheck="false" style="min-height:90px" placeholder="optional">${esc(ci.network_data || "")}</textarea></div>
      <div class="dim xs">Cloud-init runs when the guest first boots; most images read it only once.</div>`}</div>
    ${v.status === "Running" ? `<label class="switch" style="margin-top:12px"><input type="checkbox" id="ve_restart"> Restart now so the changes take effect</label>
      <div class="dim xs">Otherwise they apply the next time it starts.</div>` : ""}
    <div class="row" style="margin-top:14px"><button class="btn pri" onclick="vmEditSave()">Save</button><button class="btn" onclick="closeModal()">Cancel</button></div>`;
  if (window.applyRole) applyRole();
};
window.vmEditTab = (button, pane) => {
  $$("#mbody .seg button").forEach(b => b.classList.toggle("on", b === button));
  $$("#mbody .ve-pane").forEach(p => { p.hidden = p.dataset.pane !== pane; });
};
window.vmYaml = (ns, name) => {
  RES.pick = { group: "kubevirt.io", version: "v1", resource: "virtualmachines", kind: "VirtualMachine", namespaced: true };
  resOpen(ns, name);
};
window.vmAddDisk = kind => {
  const { o } = window.__vmEdit, cdrom = kind === "cd-rom";
  const classes = o.storage_classes || [];
  $("#ve_adds").insertAdjacentHTML("beforeend", `<div class="vd-add card flat" data-kind="${kind}">
    <div class="between"><b>New ${cdrom ? "CD-ROM" : "disk"}</b><button class="btn sm" onclick="this.closest('.vd-add').remove()">✕</button></div>
    <div class="f2"><div class="f"><label>Contents</label>${vmSourceSelect("va_src", o, cdrom ? ((o.images || [])[0] ? `image:${o.images[0].namespace}/${o.images[0].name}` : "url") : "blank")}
        <input class="va_url mono" type="url" placeholder="https://…/image.iso" ${cdrom && !(o.images || []).length ? "" : "hidden"} style="margin-top:6px"></div>
      <div class="f"><label>Size</label><input class="va_size mono" value="${cdrom ? "10Gi" : "20Gi"}"></div></div>
    <div class="f2">${classes.length ? `<div class="f"><label>Storage class</label><select class="va_class">${classes.map(c => vmOpt(c, c, o.default_class)).join("")}</select></div>` : ""}
      <div class="f"><label>Bus · boot order</label><div class="row" style="flex-wrap:nowrap"><select class="va_bus">${(cdrom ? ["sata", "scsi"] : VM_BUSES).map(b => vmOpt(b, b, cdrom ? "sata" : "virtio")).join("")}</select>
        <input class="va_boot mono" type="number" min="1" max="64" placeholder="boot #" style="width:80px"></div></div></div></div>`);
};
window.vmAddNic = () => {
  const { o } = window.__vmEdit;
  $("#ve_nics").insertAdjacentHTML("beforeend", `<tr class="vn-add"><td><b>new</b></td>
    <td><select class="vn_model">${VM_MODELS.map(m => vmOpt(m, m, "virtio")).join("")}</select></td>
    <td><select class="vn_net">${(o.networks || ["pod"]).map(x => vmOpt(x, x === "pod" ? "pod network (NAT)" : x, (o.networks || [])[1] || "pod")).join("")}</select></td>
    <td class="dim xs">automatic</td><td><button class="btn sm" onclick="this.closest('tr').remove()">✕</button></td></tr>`);
};
window.vmEditSave = async () => {
  const { ns, name } = window.__vmEdit;
  const badUrl = $$("#mbody tr[data-disk], #mbody .vd-add").some(box => box.querySelector(".vd_src,.va_src")?.value === "url"
    && !/^https?:\/\/[^/\s]+/i.test(box.querySelector(".vd_url,.va_url").value.trim()));
  if (badUrl) return toast("a disk's URL must start with http:// or https://", "bad");
  const sourceOf = (select, url) => select.value === "url" ? { url: url.value.trim() }
    : select.value.startsWith("image:") ? { image: select.value.slice(6) } : {};
  const disks = $$("#mbody tr[data-disk]").map(row => {
    const edit = { name: row.dataset.disk, boot: row.querySelector(".vd_boot").value, bus: row.querySelector(".vd_bus").value,
      remove: row.querySelector(".vd_rm").checked };
    const size = row.querySelector(".vd_size");
    if (size && size.value.trim() && size.value.trim() !== row.dataset.size) edit.size = size.value.trim();
    const src = row.querySelector(".vd_src"), url = row.querySelector(".vd_url");
    if (src && (src.value !== row.dataset.source || (src.value === "url" && url.value.trim() !== row.dataset.url))) {
      edit.source = sourceOf(src, url);
    }
    return edit;
  });
  const add_disks = $$("#mbody .vd-add").map(box => ({ kind: box.dataset.kind, size: box.querySelector(".va_size").value.trim(),
    storage_class: box.querySelector(".va_class")?.value || "", bus: box.querySelector(".va_bus").value,
    boot: box.querySelector(".va_boot").value || "", ...sourceOf(box.querySelector(".va_src"), box.querySelector(".va_url")) }));
  const nics = $$("#mbody tr[data-nic]").map(row => ({ name: row.dataset.nic, model: row.querySelector(".vn_model").value,
    network: row.querySelector(".vn_net").value, mac: row.querySelector(".vn_mac").value.trim(), remove: row.querySelector(".vn_rm").checked }));
  const add_nics = $$("#mbody tr.vn-add").map(row => ({ model: row.querySelector(".vn_model").value, network: row.querySelector(".vn_net").value }));
  const body = { ns, name, cores: +$("#ve_cores").value, memory: $("#ve_mem").value.trim(), run_strategy: $("#ve_strategy").value,
    description: $("#ve_desc").value, node: $("#ve_node").value, restart: !!$("#ve_restart")?.checked,
    disks, add_disks, nics, add_nics };
  if ($("#ve_user")) body.cloud_init = { user_data: $("#ve_user").value, network_data: $("#ve_netdata").value };
  try {
    const r = await api("/api/vm/edit", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
    toast(r.detail, "ok"); closeModal(); refresh(true);
  } catch (e) { toast(e.message, "bad"); }
};

window.vmDelete = (ns, name) => {
  const v = (STATE.data.vms || []).find(x => x.ns === ns && x.name === name) || { disks: [] };
  const filling = v.filling || [], unfinished = new Set(filling.map(f => f.claim));
  const disks = v.disks.filter(d => (d.kind === "disk" || d.kind === "cd-rom") && d.claim && !unfinished.has(d.claim));
  modal(`Delete · ${name}`, `<p>The VM is deleted${v.status === "Running" ? ", and stopped first" : ""}. It shows as Deleting until everything it owns is gone.</p>
    ${filling.length ? `<div class="note warn">${filling.map(f => `<b class="mono">${esc(f.claim)}</b> is still being filled${f.progress != null ? ` (${f.progress.toFixed(0)}%)` : ""}`).join("; ")}.
      Deleting the VM stops that and removes the unfinished disk.</div>` : ""}
    ${disks.length ? `<label class="switch"><input type="checkbox" id="vd_disks"> Delete its disks too: ${disks.map(d => `<span class="mono">${esc(d.claim)}</span>`).join(", ")}</label>
      <div class="dim xs">Left unticked, the disks are kept and can be attached to another VM or deleted from Volumes later.</div>` : ""}
    <div class="f" style="margin-top:12px"><label>Type the VM's name to delete it</label><input id="vd_confirm" autocomplete="off"></div>
    <div class="row"><button class="btn danger" onclick="vmDeleteGo('${esc(ns)}','${esc(name)}')">Delete</button><button class="btn" onclick="closeModal()">Cancel</button></div>`);
};
window.vmDeleteGo = async (ns, name) => {
  if ($("#vd_confirm").value.trim() !== name) return toast("type the VM's name exactly", "bad");
  try { const r = await api("/api/vm/delete", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ ns, name, disks: !!$("#vd_disks")?.checked }) });
    toast(r.detail, "ok"); closeModal(); refresh(true); } catch (e) { toast(e.message, "bad"); }
};


/* ---------------- a k3s cluster of VMs ----------------
   Servers and workers as VMs on a LAN network, each with an address of its
   own, the first a server the rest join with a token made here. The review
   says what goes where - and anything already at an address - before a
   single VM is made. */
const K3S_SETUPS = { homestead: "k3s, Longhorn and Homestead - what a new install gets",
  local: "k3s and Homestead, on local-path storage", k3s: "k3s alone" };
const K3S_UBUNTU = "https://cloud-images.ubuntu.com/noble/current/noble-server-cloudimg-amd64.img";

window.k3sCluster = async () => {
  modal("New k3s cluster", '<div class="empty"><span class="spin2"></span></div>', true);
  window.__vmNetworkReopen = "k3s";
  const opts = await api("/api/vm/create-options").catch(() => ({}));
  window.__vmCreateOptions = opts;
  const lan = vmLanNetworks(opts);
  const images = (opts.images || []).filter(i => i.storage_class);
  $("#mbody").innerHTML = `
    <p class="small" style="margin-top:0">VMs here become a k3s cluster: the first is its server, the rest join it. Each gets an address of
      its own on the LAN, so the cluster is reached - and joins - as one built from real machines would be.</p>
    ${lan.length ? "" : vmNetworkNote(opts)}
    <div class="f2"><div class="f"><label>Name ${tip("Starts each VM's name: k3s-demo-server-1, k3s-demo-agent-1 and so on.")}</label><input id="k_name" value="k3s-demo"></div>
      <div class="f"><label>Install ${tip("What each node sets up. k3s, Longhorn and Homestead is what a new install from our bootstrap script gets; local-path skips Longhorn and keeps each volume on one node; k3s alone installs nothing else.")}</label><select id="k_setup">${Object.entries(K3S_SETUPS).map(([v, l]) => `<option value="${v}">${esc(l)}</option>`).join("")}</select></div></div>
    <div class="f2"><div class="f"><label>Servers ${tip("One is enough to try things. Three keep the cluster running if one fails.")}</label>
        <select id="k_servers" onchange="k3sCountChanged()"><option value="1">1</option><option value="3">3</option></select></div>
      <div class="f"><label>Workers ${tip("Nodes that run apps but not the cluster's control plane. They join the first server. Zero is fine: a server runs apps too.")}</label><input id="k_agents" type="number" min="0" max="6" value="2" oninput="k3sCountChanged()"></div></div>
    <div class="f2"><div class="f"><label>Cores each ${tip("CPU cores for every node. Two is enough to try things; k3s itself needs little.")}</label><input id="k_cores" type="number" min="1" max="16" value="2"></div>
      <div class="f"><label>Memory each ${tip("Memory for every node, like 4Gi. Longhorn and Homestead inside want at least 4Gi on the server.")}</label><input id="k_mem" value="4Gi"></div></div>
    <div class="f2"><div class="f"><label>Disk each (GB) ${tip("Longhorn inside the cluster keeps its volumes here, so leave room for your apps.")}</label><input id="k_disk" type="number" min="20" value="40"></div>
      <div class="f"><label>Login password ${tip("For the ubuntu user on every node, at the console or over SSH.")}</label><input id="k_pass" type="password" autocomplete="new-password"></div></div>
    <div class="f"><label>Image ${tip("The operating system every node starts from. An Ubuntu cloud image works best: it runs cloud-init, which sets up the address, login and k3s.")}</label><select id="k_image">
      ${images.map(i => `<option value="image:${esc(i.namespace)}/${esc(i.name)}" ${/noble|24\.04|ubuntu/i.test(i.display) ? "selected" : ""}>Harvester image · ${esc(i.display)}</option>`).join("")}
      <option value="url" ${images.some(i => /noble|24\.04|ubuntu/i.test(i.display)) ? "" : "selected"}>Ubuntu 24.04 cloud image (downloaded${opts.harvester ? " as a Harvester image" : ""})</option></select></div>
    ${(opts.storage_classes || []).length ? `<div class="f"><label>Storage class ${tip("Where each node's disk lives on this cluster.")}</label><select id="k_sc">${opts.storage_classes.map(c => `<option ${c === opts.default_class ? "selected" : ""}>${esc(c)}</option>`).join("")}</select></div>` : ""}
    <div class="sec">Network</div>
    <div class="f"><label>VM network ${tip("The network bridged to your LAN the nodes join, so each has an address of its own there.")}</label><select id="k_net">${lan.map(n => `<option value="${esc(n.name)}">${esc(n.name)}${n.vlan ? ` (VLAN ${esc(n.vlan)})` : ""}</option>`).join("") || '<option value="">none reaches the LAN</option>'}</select></div>
    ${vmAddressFields("k", opts, 3)}
    <div id="k_review"></div>
    <div class="row" style="margin-top:14px"><button class="btn" onclick="k3sReview()" ${lan.length ? "" : "disabled"}>Review</button>
      <button class="btn pri" id="k_go" data-need="operator" onclick="k3sCreate()" disabled>Create cluster</button>
      <button class="btn" onclick="closeModal()">Cancel</button></div>`;
  k3sCountChanged();
  if (window.applyRole) applyRole();
};
window.k3sCountChanged = () => {
  const count = +$("#k_servers").value + Math.max(0, +$("#k_agents").value || 0);
  vmSubnetPicked("k", count);
  $("#k_go").disabled = true;
  $("#k_review").innerHTML = "";
};
function k3sBody() {
  const image = $("#k_image").value;
  return Object.assign(vmReadAddress("k"), {
    name: $("#k_name").value.trim(), setup: $("#k_setup").value,
    servers: +$("#k_servers").value, agents: +$("#k_agents").value || 0,
    cores: +$("#k_cores").value, memory: $("#k_mem").value.trim(), disk_gb: +$("#k_disk").value,
    password: $("#k_pass").value, network: $("#k_net").value, storage_class: $("#k_sc")?.value || "",
    image_id: image.startsWith("image:") ? image.slice(6) : "", image_url: image === "url" ? K3S_UBUNTU : "",
    addresses: $("#k_ip").value.split(",").map(x => x.trim()).filter(Boolean) });
}
window.k3sReview = async () => {
  try {
    const plan = await api("/api/vm/k3s-cluster/plan", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(k3sBody()) });
    $("#k_review").innerHTML = `<div class="sec">What it makes</div>
      <table class="tbl dense stack"><thead><tr><th>VM</th><th>Role</th><th>Address</th></tr></thead><tbody>
      ${plan.nodes.map(n => `<tr><td><b>${esc(n.name)}</b></td><td data-label="Role">${n.role === "server" ? '<span class="tag info">server</span>' : '<span class="tag">worker</span>'}</td>
        <td data-label="Address" class="mono">${esc(n.address)}${n.problem ? `<div class="badtext xs">${esc(n.problem)}</div>` : ""}</td></tr>`).join("")}</tbody></table>
      <div class="note ${plan.ok ? "" : "bad"}" style="margin-top:10px">${plan.ok
        ? `Each address is recorded under its VM in IP addresses. Allow 10-15 minutes: the VMs start, install ${esc(K3S_SETUPS[plan.setup])}, and join.
           ${plan.url ? `Its own Homestead then answers at <span class="mono">${esc(plan.url)}</span>.` : ""} The job tray follows it.`
        : "Choose other addresses for the ones marked: something already has them."}</div>`;
    $("#k_go").disabled = !plan.ok;
  } catch (e) { $("#k_review").innerHTML = `<div class="note bad">${esc(e.message)}</div>`; $("#k_go").disabled = true; }
};
window.k3sCreate = async () => {
  const button = $("#k_go");
  button.disabled = true; button.textContent = "Creating VMs…";
  try {
    await api("/api/vm/k3s-cluster", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(k3sBody()) });
    toast("Cluster VMs created - the job tray follows them coming up", "ok");
    closeModal(); if (window.refreshOperations) refreshOperations(true); go("vms");
  } catch (e) { toast(e.message, "bad"); button.disabled = false; button.textContent = "Create cluster"; }
};
