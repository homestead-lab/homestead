/* Virtual machines: each one's real state and the actions that fit it, a
   page per VM (disks, network, guest, events), editing and deleting. */

const VM_TONE = { Running: "ok", Stopped: "low", Paused: "info", Migrating: "info", Starting: "med", Stopping: "med",
  Provisioning: "med", WaitingForVolumeBinding: "med" };
const VM_ACTIONS = {
  start: ["Start", "play", "Boot the VM"], stop: ["Stop", "stop", "Shut the VM down: the guest is asked to power off, then it is stopped"],
  restart: ["Restart", "restart", "Restart the VM"], pause: ["Pause", "stop", "Freeze the VM where it is, keeping its memory"],
  unpause: ["Resume", "play", "Carry on from where it was paused"], "force-stop": ["Force stop", "stop", "Stop at once, without asking the guest: like pulling the plug"],
};
const vmTone = status => VM_TONE[status] || (/Error|Fail|Crash|BackOff/i.test(status) ? "crit" : "med");

async function viewVMs() {
  if (platformLacks("kubevirt", "Virtual machines")) return;
  const vms = await api("/api/vms");
  STATE.data.vms = vms;
  const q = STATE.q.toLowerCase();
  const rows = vms.filter(v => !q || [v.name, v.ns, v.os, v.ip, v.node, v.description].join(" ").toLowerCase().includes(q));
  const running = vms.filter(v => v.status === "Running").length;
  paint(`<div class="phead"><div><h2>Virtual machines</h2>
      <p>${vms.length} VM${vms.length === 1 ? "" : "s"} · ${running} running · KubeVirt on Longhorn</p></div>
      <button class="btn pri" data-need="operator" onclick="vmNew()">＋ New VM</button></div>
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

window.vmEdit = async (ns, name) => {
  let v = (STATE.data.vms || []).find(x => x.ns === ns && x.name === name);
  try { v = await api(`/api/vm?ns=${encodeURIComponent(ns)}&name=${encodeURIComponent(name)}`); } catch (e) { if (!v) return toast(e.message, "bad"); }
  modal(`Edit · ${name}`, `
    <div class="f2"><div class="f"><label>CPU cores</label><input id="ve_cores" type="number" min="1" max="128" value="${v.cores}"></div>
      <div class="f"><label>Memory</label><input id="ve_mem" class="mono" value="${esc(v.memory)}" placeholder="4Gi"></div></div>
    <div class="f"><label>Run strategy ${tip("RerunOnFailure (Harvester's default): runs, and starts again if the guest crashes, but not after you stop it. Always: kept running whatever happens. Manual: runs only when started, never restarted. Halted: kept off.")}</label>
      <select id="ve_strategy">${["RerunOnFailure", "Always", "Manual", "Halted"].map(s => `<option ${s === v.run_strategy ? "selected" : ""}>${s}</option>`).join("")}</select></div>
    <div class="f"><label>Description</label><input id="ve_desc" value="${esc(v.description || "")}" maxlength="300"></div>
    ${v.status === "Running" ? `<label class="switch"><input type="checkbox" id="ve_restart"> Restart now so new CPU or memory take effect</label>
      <div class="dim xs">Otherwise they apply the next time it starts.</div>` : ""}
    <div class="row" style="margin-top:14px"><button class="btn pri" onclick="vmEditSave('${esc(ns)}','${esc(name)}')">Save</button><button class="btn" onclick="closeModal()">Cancel</button></div>`);
};
window.vmEditSave = async (ns, name) => {
  const body = { ns, name, cores: +$("#ve_cores").value, memory: $("#ve_mem").value.trim(), run_strategy: $("#ve_strategy").value,
    description: $("#ve_desc").value, restart: !!$("#ve_restart")?.checked };
  try { const r = await api("/api/vm/edit", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
    toast(r.detail, "ok"); closeModal(); refresh(true); } catch (e) { toast(e.message, "bad"); }
};

window.vmDelete = (ns, name) => {
  const v = (STATE.data.vms || []).find(x => x.ns === ns && x.name === name) || { disks: [] };
  const disks = v.disks.filter(d => d.kind === "disk" && d.claim);
  modal(`Delete · ${name}`, `<p>The VM is deleted${v.status === "Running" ? ", and stopped first" : ""}.</p>
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
