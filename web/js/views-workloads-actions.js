/* Homestead Containers: stop Homestead, failover, a LAN address, restart, delete and logs (#911). One of the scripts views-workloads.js was split into; they share the page's globals and load in the order index.html lists them. */
/* Homestead stopping itself takes this page with it, and nothing here can
   start it again - so it is said plainly, with restart offered instead. */
window.wlStopSelf = (ns, name) => {
  modal("Stop · Homestead", `
    ${UI.callout("bad", "This takes Homestead down, and this page with it", `<p>Nothing here can start it again. It stays down until someone runs
      <code>kubectl -n ${esc(ns)} scale deployment/${esc(name)} --replicas=1</code> on the cluster.</p>
      <p>Your apps keep running. Jobs in the tray, alerts and moves pause until it is back.</p>`)}
    <p class="small">If it needs a fresh start, <b>Restart</b> brings it straight back.</p>
    <label class="switch"><input type="checkbox" id="ss_ok"> Stop it anyway</label>
    ${UI.actions(`<button class="btn pri" onclick="closeModal();wlRestart(${jsq(ns)},${jsq(name)})">Restart instead</button>
      <button class="btn danger" onclick="wlStopSelfGo(${jsq(ns)},${jsq(name)})">Stop Homestead</button>
      <button data-dialog-dismiss="true" class="btn" onclick="closeModal()">Cancel</button>`)}`);
};
window.wlStopSelfGo = async (ns, name) => {
  if (!$("#ss_ok").checked) return toast("Tick the box to confirm", "bad");
  try {
    await api("/api/scale", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ ns, name, replicas: 0, confirm_self: true }) });
    closeModal(); toast("Homestead is stopping", "ok");
  } catch (e) { toast(e.message, "bad"); }
};

/* Every container's behaviour when its node fails, in one list. Longhorn
   decides whether a moved container's single-node volume can follow it, so
   its policy is shown here too. */
window.wlFailover = async () => {
  modal("If a host fails", '<div class="empty"><span class="spin2"></span></div>', true);
  // An unread container list is said; no policy is offered or saved against an old one (#620).
  const [rows, lh] = await Promise.all([api("/api/workloads").catch(e => ({ unread: e.message })),
    // Without Longhorn there is no policy to show; with it, an unread one is said (#620).
    STATE.platform?.longhorn === false ? null : api("/api/longhorn/capacity").catch(e => ({ unread: e.message }))]);
  const policy = lh?.unread ? "" : lh?.node_down || "";
  // Not knowing whether Longhorn is installed (/api/platform unread), a failed
  // read may only mean it is not there.
  const unread = lh?.unread && (STATE.platform?.longhorn == null
    ? `${String(lh.unread).replace(/\.$/, "")}. Longhorn may not be installed on this cluster.` : lh.unread);
  const moving = policy && policy !== "do-nothing";
  $("#mbody").innerHTML = `
    <p class="small" style="margin-top:0">Choose whether containers move, wait or use Kubernetes’ default five-minute delay after host failure. Saving a changed policy restarts that container.</p>
    ${lh?.unread ? UI.callout("warn", "Could not read whether apps with storage can move off a failed host", esc(unread)) : lh ? `${UI.callout(moving ? "good" : "warn", moving ? "" : "Apps with storage cannot move", moving
      ? "If a host fails, Longhorn releases its storage, so apps set to move restart on another host with their storage."
      : "If a host fails, Longhorn keeps its storage attached to it, so apps that use it stay stopped until the host is back.", moving ? {} : { actions: `<button class="btn sm pri" data-need="admin" onclick="wlFailoverPolicy()">Allow them to move</button>` })}` : ""}
    ${rows.unread ? `${UI.callout("warn", "Could not read the containers", esc(rows.unread))}
    ${UI.actions(`<button class="btn" onclick="wlFailover()">Try again</button>
      <button data-dialog-dismiss="true" class="btn" onclick="closeModal()">Close</button>`)}` : `
    <div class="row" style="margin:12px 0;gap:6px;flex-wrap:wrap"><span class="small dim">Set all to</span>
      ${Object.entries(FAILOVER_WORDS).map(([v, l]) => `<button class="btn sm" onclick="$$('#mbody select[data-fo]').forEach(s => s.value=${jsq(v)})">${esc(l)}</button>`).join("")}</div>
    <table class="tbl dense stack"><thead><tr><th>Container</th><th>If its host fails</th></tr></thead><tbody>
      ${rows.map(w => `<tr><td><b>${esc(w.name)}</b> <span class="dim xs">${esc(w.ns)}${w.group ? ` · ${esc(w.group)}` : ""}</span>
          ${(w.hardware || []).length ? UI.chip(w.hardware.join(", "), "hw", { tip: "Tied to hardware on its host" }) : ""}</td>
        <td data-label="If its node fails">${failoverSelect(`fo_${w.ns}_${w.name}`, w.failover || "default", `data-fo data-ns="${esc(w.ns)}" data-name="${esc(w.name)}" data-was="${esc(w.failover || "default")}"`, `If ${w.name}'s host fails`)}</td></tr>`).join("")}
    </tbody></table>
    ${UI.actions(`<button class="btn pri" data-need="operator" onclick="wlFailoverSave()">Save changes</button>
      <button data-dialog-dismiss="true" class="btn" onclick="closeModal()">Cancel</button>`)}`}`;
  if (window.applyRole) applyRole();
};
window.wlFailoverSave = async () => {
  const items = $$("#mbody select[data-fo]").filter(s => s.value !== s.dataset.was)
    .map(s => ({ ns: s.dataset.ns, name: s.dataset.name, mode: s.value }));
  if (!items.length) return closeModal();
  if (items.some(i => (STATE.data.wl || []).some(w => w.self && w.ns === i.ns && w.name === i.name))
      && !(await ask("Homestead itself is among them: it restarts, and this page reconnects when it is back.", { title: "Homestead restarts", ok: "Save and restart" }))) return;
  try {
    const r = await api("/api/workloads/failover", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ items }) });
    toast(r.detail, "ok"); closeModal(); setTimeout(() => refresh(true), 900);
  } catch (e) { toast(e.message, "bad"); }
};
window.wlFailoverPolicy = async () => {
  if (!(await ask("If a host fails, Longhorn will delete its pods and release their storage, so apps set to move can restart on another host.\n\nRisk: if the host has only lost its network connection and is still running, its copy of an app may keep running, and any data that copy writes after the switch-over is lost.", { title: "Allow apps with storage to move", ok: "Allow" }))) return;
  try {
    await api("/api/longhorn/settings", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ node_down: "delete-both-statefulset-and-deployment-pod" }) });
    toast("Apps with storage can now move off a failed host", "ok"); wlFailover();
  } catch (e) { toast(e.message, "bad"); }
};

/* A container with an address of its own on the LAN: a second interface on
   a bridged VM network, beside its pod network, so it answers there directly
   without a Service - what an app that wants to be found on the LAN needs. */
async function containerLanFields(p, current) {
  // Unread networks are not "no LAN network yet", and the failure is not kept as the answer (#620).
  let opts = window.__vmCreateOptions;
  if (!opts) {
    try { opts = await api("/api/vm/create-options"); }
    catch (e) { return UI.callout("warn", "Could not read the networks", esc(e.message)); }
    window.__vmCreateOptions = opts;
  }
  const lan = vmLanNetworks(opts);
  if (!lan.length) return vmNetworkNote(opts);
  return `<div class="f"><label>LAN network ${tip("Its bridge and VLAN; the container joins it as a second interface, lan0, and keeps the pod network for everything else.")}</label>
      <select id="${p}_net">${lan.map(n => `<option value="${esc(n.name)}" ${current?.network === n.name ? "selected" : ""}>${esc(n.name)}${n.vlan ? ` (VLAN ${esc(n.vlan)})` : ""}</option>`).join("")}</select></div>
    ${vmAddressFields(p, opts)}
    <div class="dim xs">It answers on this address directly - no Service or VIP - and it is recorded under the container's name in IP addresses.</div>`;
}
function containerLanRead(p) {
  return Object.assign(vmReadAddress(p), { network: $(`#${p}_net`)?.value || "", address: ($(`#${p}_ip`)?.value || "").trim() });
}
window.containerLanFields = containerLanFields;
window.containerLanRead = containerLanRead;
window.deployLanChanged = async () => {
  const lan = $("#d_net")?.value === "lan", box = $("#d_lan_box");
  if (!box) return;
  box.hidden = !lan;
  const vip = $("#d_vip_mode")?.closest(".f");
  if (vip) vip.hidden = lan;
  if ($("#d_vip_wrap")) $("#d_vip_wrap").hidden = lan || $("#d_vip_mode").value !== "manual";
  if (lan && !box.dataset.filled) {
    box.innerHTML = await containerLanFields("dl", DCFG.lan);
    box.dataset.filled = "1";
    if ($("#dl_subnet")) vmSubnetPicked("dl");
    if (DCFG.lan?.address) $("#dl_ip").value = DCFG.lan.address;
  }
};

window.wlRestart = async (ns, name) => {
  if (!(await ask(`Restart ${name}? Its pods are replaced with fresh ones; what it serves may be away for a moment.`, { title: "Restart", ok: "Restart" }))) return;
  try {
    await api("/api/restart", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ ns, name }) });
    toast(`${name} restarting`, "ok"); setTimeout(() => refresh(true), 1300);
  } catch (e) { toast(e.message, "bad"); }
};
window.wlDelete = async (ns, name) => {
  modal("Delete · " + name, '<div class="empty"><span class="spin2"></span>checking what this removes</div>', true);
  // The card list alone cannot say which Services or claims belong to this
  // workload, and deleting it removes the first and keeps the second.
  // A failed read is not "none": it is shown, and the delete is not offered.
  const [network, vols] = await Promise.all([
    STATE.data.network ? Promise.resolve(STATE.data.network) : api("/api/network").catch(e => ({ error: e.message })),
    STATE.data.vols ? Promise.resolve(STATE.data.vols) : api("/api/volumes").catch(e => ({ error: e.message })),
  ]);
  const failed = [network, vols].find(read => read?.error);
  const unread = read => `<span class="warn">could not be read: ${esc(read.error)}</span>`;
  const workload = (STATE.data.wl || []).find(row => row.ns === ns && row.name === name) || {};
  const services = (network.services || [])
    .filter(row => row.namespace === ns && (row.targets || []).includes(name));
  const listeners = services.flatMap(row => (row.external_ips || [])
    .flatMap(ip => (row.ports || []).map(port => `${ip}:${port.port}/${port.protocol}`)));
  const claims = vols.error ? [] : (vols || []).filter(vol => (vol.attached || []).includes(name));
  $("#mbody").innerHTML = `
    ${UI.callout("bad", { html: `This removes the app, not its data.` }, `The Deployment and every Service pointing at it are deleted. Persistent volumes are kept and can be
      removed afterwards from Volumes.`)}
    <div class="dependency-list" style="margin-top:12px">
      <div class="dependency-row"><span>Deployment</span><b class="mono">${esc(ns)}/${esc(name)}</b></div>
      <div class="dependency-row"><span>Containers</span><b>${(workload.images || []).length || 1}</b></div>
      <div class="dependency-row ${services.length ? "stranded" : ""}"><span>Services removed</span>
        <b>${network.error ? unread(network) : services.length ? services.map(row => esc(row.name)).join(", ") : "none"}</b></div>
      ${listeners.length ? `<div class="dependency-row stranded"><span>LAN listeners freed</span>
        <b class="mono">${listeners.map(esc).join(", ")}</b></div>` : ""}
      <div class="dependency-row"><span>Volumes kept</span>
        <b>${vols.error ? unread(vols) : claims.length ? claims.map(vol => esc(vol.pvc_name || vol.name)).join(", ") : "none attached"}</b></div>
    </div>
    ${failed ? `<div class="note" style="margin-top:12px">Deleting is offered once Homestead can read which Services it removes and which volumes it keeps. Try again in a moment.</div>
    ${UI.actions(`<button class="btn" onclick="wlDelete(${jsq(ns)},${jsq(name)})">Retry</button>
      <button data-dialog-dismiss="true" class="btn" onclick="closeModal()">Cancel</button>`)}`
    : `${UI.typedConfirm({ id: "wd_confirm", expect: name, button: "wd_go" })}
    ${UI.actions(`<button class="btn danger" id="wd_go" data-need="operator" disabled
      onclick="wlDeleteNow(${jsq(ns)},${jsq(name)},this)">Delete app</button>
      <button data-dialog-dismiss="true" class="btn" onclick="closeModal()">Cancel</button>`)}`}`;
  if (window.applyRole) window.applyRole();
};
window.wlDeleteNow = async (ns, name, button) => {
  if (!typedConfirmMatches("wd_confirm")) return;
  if (button) { button.disabled = true; button.textContent = "Deleting…"; }
  try {
    const result = await api(`/api/workload/${ns}/${name}`, { method: "DELETE" });
    const freed = (result.services || []).length;
    closeModal();
    toast(`${name} deleted${freed ? ` with ${freed} service${freed === 1 ? "" : "s"}` : ""}`, "ok");
    setTimeout(() => refresh(true), 900);
  } catch (e) {
    if (button) { button.disabled = false; button.textContent = "Delete app"; }
    toast(e.message, "bad");
  }
};
/* Logs for one container of one pod. A workload with several replicas, or a
   pod with sidecars, gets pickers saying exactly whose output this is. */
function openLogs(title, path, pods = null, ns = "") {
  window.__logTimer?.stop?.();
  const LOG = window.__logView = { path, pods: pods || [], ns };
  const multi = LOG.pods.length > 1 || LOG.pods.some(p => (p.containers || []).filter(c => c.kind === "app").length > 1);
  modal("Logs · " + title, `<div class="logtools">
      ${multi ? `<div class="logpick"><label>Pod <select id="logPod" onchange="logPodChanged()">${LOG.pods.map(p =>
        `<option value="${esc(p.name)}">${esc(p.name)} · ${esc(p.node || "unscheduled")}${p.ready ? "" : " · not ready"}</option>`).join("")}</select></label>
        <label>Container <select id="logContainer" onchange="logTargetChanged()"></select></label></div>` : ""}
      <span id="logstate"><span class="spin2"></span> connecting</span>
      <label class="switch"><input type="checkbox" id="logfollow" checked> Follow latest</label></div>
    <div class="dim xs mono logsource" id="logSource"></div>
    <pre class="logview">waiting for log output…</pre>`, true);
  if (multi) logPodChanged(); else logShowSource();
  // The dialog's own read (`keep`, not the page's): closing it cancels it.
  const poll = async signal => {
    if ($("#modal").classList.contains("hidden")) return false;
    try {
      const t = await api(LOG.path + (LOG.path.includes("?") ? "&" : "?") + "tail=500", { keep: true, signal });
      if (UI.pollCancelled(signal)) return;
      const pre = $("#mbody .logview"); if (!pre) return;
      pre.textContent = t || "(the container is running but has not written any logs yet)";
      $("#logstate").innerHTML = '<span class="ld"></span> live · refreshes every 2s';
      if ($("#logfollow")?.checked) pre.scrollTop = pre.scrollHeight;
    } catch (e) {
      if (UI.pollCancelled(signal)) return;
      const pre = $("#mbody .logview"); if (pre) pre.textContent = e.message;
      if ($("#logstate")) $("#logstate").innerHTML = '<span class="cd badbg"></span> no logs';
    }
  };
  const timer = window.__logTimer = UI.poll(poll, 2000);
  // Another pod or container: the old one's read is cancelled, so its output never paints here.
  LOG.poll = () => timer.restart();
}

window.logPodChanged = () => {
  const LOG = window.__logView, pod = LOG.pods.find(p => p.name === $("#logPod")?.value);
  const apps = (pod?.containers || []).filter(c => c.kind === "app");
  $("#logContainer").innerHTML = apps.map(c => `<option value="${esc(c.name)}">${esc(c.name)} · ${esc(c.state || "unknown")}</option>`).join("");
  $("#logContainer").closest("label").style.display = apps.length > 1 ? "" : "none";
  logTargetChanged();
};

window.logTargetChanged = () => {
  const LOG = window.__logView, pod = $("#logPod")?.value, container = $("#logContainer")?.value;
  LOG.path = `/api/logs?ns=${encodeURIComponent(LOG.ns)}&pod=${encodeURIComponent(pod)}${container ? `&container=${encodeURIComponent(container)}` : ""}`;
  const pre = $("#mbody .logview"); if (pre) pre.textContent = "waiting for log output…";
  logShowSource();
  if (LOG.poll) LOG.poll();
};

function logShowSource() {
  const LOG = window.__logView, host = $("#logSource");
  if (!host) return;
  const params = new URLSearchParams(LOG.path.split("?")[1] || "");
  const pod = LOG.pods.find(p => p.name === params.get("pod"));
  host.textContent = params.get("pod") ? `pod ${params.get("pod")}${pod?.node ? ` on ${pod.node}` : ""}${params.get("container") ? ` · container ${params.get("container")}` : ""}` : "";
}


// Node only (tests): the same names, for require(). The browser has no `module`.
if (typeof module === "object" && module.exports) module.exports = {
  containerLanFields, containerLanRead, openLogs, logShowSource
};
