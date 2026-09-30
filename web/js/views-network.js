/* Services, VIPs, listeners, endpoints and ingress */

const networkPill = health => health === "healthy" ? "ok" : health === "pending" ? "med" : "crit";
const networkPortText = row => (row.ports || []).map(p => `${p.port}/${p.protocol}`).join(", ");

function networkVipCard(v, data) {
  const isDefault = v.ip === data.shared_vip?.ip;
  const blocked = v.blocked || ((data.node_ips || []).includes(v.ip) ? "This is a node address, not a workload VIP." : "");
  const canUse = !blocked && !nodeAddressesOnly();
  return `<article class="vip-card${isDefault ? " is-default" : ""}">
    <div class="vip-card-heading"><b class="mono">${esc(v.ip)}</b><span class="tag ${blocked ? "bad" : v.free ? "" : "info"}">${blocked ? "Unavailable" : v.free ? "Not assigned" : "In use"}</span></div>
    <div class="vip-card-label">${esc(v.label || "No label")}</div>
    ${isDefault ? '<div class="vip-card-default"><span class="tag ok">Default workload VIP</span><span class="dim xs">Suggested for new workloads; existing services stay where they are.</span></div>' : ""}
    ${blocked || (v.used_by || []).length ? `<div class="vip-card-usage small">${blocked ? esc(blocked) : `<span class="dim">Used by</span> ${(v.used_by || []).map(n => `<span class="mono">${esc(n)}</span>`).join("")}`}</div>` : ""}
    <div class="vip-card-actions">
      ${canUse ? `<button class="btn sm" data-need="operator" onclick="networkExpose('','','Deployment',${jsq(v.ip)})">Use this VIP</button>` : ""}
      ${canUse && !isDefault ? `<button class="btn sm" data-need="admin" onclick="vipDefault(${jsq(v.ip)})">Make default</button>` : ""}
      <button class="btn sm" data-need="admin" onclick="vipLabel(${jsq(v.ip)})">Edit label</button>
      ${!blocked ? `<button class="btn sm" data-need="admin" onclick="vipChange(${jsq(v.ip)})" title="A new address for this VIP, with everything on it">Change address</button>` : ""}
      ${!isDefault && (v.free || blocked) ? `<button class="btn sm" data-need="admin" onclick="vipRemove(${jsq(v.ip)})">Remove</button>` : ""}
    </div></article>`;
}

/* Nodes & addresses: each node with its own address and the VIPs it answers
   for, and on every address the ports, the Services behind them and what
   they run. An address works only when a node answers for it and its
   Services carry it; each state says in words which half is missing. */
const ADDRESS_STATE = {
  ok: ["ok", "working"], unrouted: ["bad", "not reachable"], unannounced: ["bad", "no node answers"],
  idle: ["", "nothing running"], pending: ["warn", "port taken"],
};

function addressListeners(row) {
  if (!row.listeners.length) return '<div class="dim xs addr-none">Nothing listens here</div>';
  return `<div class="addr-listeners">${row.listeners.map(l => {
    const [href, browser] = networkOpenUrl(row.ip, l.port);
    return `<div class="addr-listener"><span class="addr-port mono">${l.port}<span class="dim">/${esc(l.protocol)}</span></span>
      <span class="addr-arrow" aria-hidden="true">→</span>
      <span class="addr-svc"><b>${esc(l.workloads.length ? l.workloads.map(w => w.replace(/^VirtualMachine\//, "VM ")).join(", ") : l.service)}</b>
        <span class="dim xs mono">${esc(l.namespace)}/${esc(l.service)}${l.ready ? "" : " · not running"}</span></span>
      ${browser && row.state === "ok" ? `<a class="tag info addr-open" href="${safeHref(href)}" target="_blank" rel="noopener">Open ${icon("ext")}</a>` : ""}</div>`;
  }).join("")}</div>`;
}

function networkOpenUrl(ip, port) {
  const secure = [443, 8443, 9443].includes(+port);
  const web = secure || [80, 3000, 5000, 8000, 8080, 8088, 8096, 8123, 9001, 32400].includes(+port);
  return [`${secure ? "https" : "http"}://${ip}:${port}`, web];
}

function addressRow(row, data) {
  const [tone, words] = ADDRESS_STATE[row.state] || ["", row.state];
  const label = (data.vip_labels || {})[row.ip];
  const kind = row.kind === "node" ? "Node address" : "VIP";
  const tip = row.kind === "node"
    ? "The node's own address. k3s's ServiceLB publishes Services here; it does not move if the node goes down."
    : "A virtual address one node answers for at a time; it moves to another node if that one goes down.";
  return `<div class="addr-row ${row.state === "ok" ? "" : "addr-" + esc(row.state)}" data-ip="${esc(row.ip)}">
    <div class="addr-head"><b class="mono">${esc(row.ip)}</b>
      <span class="tag ${row.kind === "node" ? "" : "info"}" data-tip="${esc(tip)}">${kind}</span>
      ${label ? `<span class="dim xs">${esc(label)}</span>` : ""}
      ${row.listeners.length || row.kind === "vip" ? `<span class="tag ${tone} addr-state">${esc(words)}</span>` : ""}</div>
    ${row.reason ? `<div class="addr-reason small">${esc(row.reason)}</div>` : ""}
    ${addressListeners(row)}</div>`;
}

function networkAddressesHtml(data) {
  const map = data.addresses || { nodes: [], addresses: [] };
  const byIp = Object.fromEntries(map.addresses.map(row => [row.ip, row]));
  const placed = new Set();
  const nodes = map.nodes.map(n => {
    const rows = [...n.ips, ...n.vips].map(ip => byIp[ip]).filter(Boolean);
    rows.forEach(row => placed.add(row.ip));
    return `<article class="addr-node" data-node="${esc(n.name)}">
      <div class="addr-node-head"><div><b>${esc(n.name)}</b><span class="dim xs">${n.control_plane ? "control plane" : "worker"}</span></div>
        <span class="pill ${n.ready ? "low" : "crit"}">${n.ready ? "Ready" : "Not ready"}</span></div>
      ${rows.map(row => addressRow(row, data)).join("")}
      ${n.vips.length ? "" : '<div class="dim xs addr-none">Answers for no VIP right now</div>'}</article>`;
  });
  const loose = map.addresses.filter(row => !placed.has(row.ip) && row.kind === "vip");
  if (loose.length) nodes.push(`<article class="addr-node addr-loose">
      <div class="addr-node-head"><div><b>No node</b><span class="dim xs">addresses nothing answers for</span></div></div>
      ${loose.map(row => addressRow(row, data)).join("")}</article>`);
  const kept = (map.kept || []).slice(-3);
  return `<div class="between"><div class="sec">Nodes &amp; addresses ${tip("Which node answers for each address, and what is reached there. An address works when a node answers for it on the LAN and the Services on it carry it - kube-proxy forwards only addresses a Service carries.")}</div></div>
    ${kept.length ? `<div class="note small addr-kept">${kept.map(k => `kube-vip answered for <span class="mono">${esc(k.ips.join(", "))}</span> on ${esc(k.node)} but left it off <span class="mono">${esc(k.namespace)}/${esc(k.name)}</span>, so its ports were refused. Homestead recorded it ${esc(fmtAgo(Math.max(1, Math.round(Date.now() / 1000 - k.at))))}.`).join("<br>")}</div>` : ""}
    <div class="addr-nodes">${nodes.join("") || '<div class="card flat empty">No nodes</div>'}</div>`;
}

async function viewNetworking() {
  if (networkTab() === "ip") return viewIpam();
  const [data, baseline] = await Promise.all([api("/api/network"), api("/api/platform/baseline").catch(() => null)]);
  STATE.data.network = data;
  const q = STATE.q.toLowerCase();
  const showSystem = !!STATE.networkSystem;
  const services = data.services.filter(row => (showSystem || !row.system) && (!q ||
    [row.namespace, row.name, row.cluster_ip, ...row.external_ips, networkPortText(row), ...row.targets]
      .join(" ").toLowerCase().includes(q)));
  const controller = data.controller;
  const orphans = services.filter(row => row.orphaned).length;
  paint(`<div class="phead"><div><h2>Networking</h2>
      <p>Addresses, listeners and the live path from your LAN to each workload</p></div>
      <div class="row">${moreMenu([{ label: `${showSystem ? "Hide" : "Show"} system services`, icon: "layers", run: "networkToggleSystem()" }])}
      <button class="btn pri" data-need="operator" onclick="networkExpose()">＋ Expose workload</button></div></div>
    ${networkTabs("services")}
    ${baselineHtml(baseline, "network")}
    ${UI.guide("How addresses work here", `
      <p><b>${esc(controller.name)}</b> handles service addresses. Multus adds separate LAN interfaces; it does not provide VIP failover.
      ${STATE.platform?.servicelb ? "ServiceLB also exposes unclassified Services on node IPs, not a movable VIP." : ""}</p>
      <p><b>Default workload VIP:</b> ${esc(data.shared_vip?.ip || "not configured")}. Choose a default below, then select <b>Default workload VIP</b> or <b>Specific VIP</b> when deploying.
      VMs on the pod network can be exposed through a Service; bridged VMs use their own DHCP/static address, not a service VIP.</p>
      <p>VIP failover is not full-cluster HA: multiple eligible hosts, a surviving control-plane quorum, portable storage with healthy replicas, and workload restart policies are also needed.
      ${Object.keys(data.node_names || {}).length < 2 ? "This is a single-node cluster: there is no second host to take over." : "Test host failure before relying on recovery."}</p>`)}
    ${UI.stats([
      { title: "Load balancer", value: controller.ready, unit: `/${controller.desired}`, tone: controller.healthy ? "ok" : "bad",
        sub: `${controller.name} agents ready · ${controller.mode}` },
      { title: "Virtual IPs", value: data.summary.vips, sub: `${data.summary.listeners} LAN listeners · ${data.available_vip_count} unused in pools` },
      { title: "Application services", value: data.summary.app_services, sub: `${data.summary.ready_endpoints} ready endpoints` },
      { title: "Attention", value: data.summary.unhealthy, tone: data.summary.unhealthy || data.conflicts.length || data.addresses?.problems ? "warn" : "",
        sub: data.addresses?.problems ? `${data.addresses.problems} address${data.addresses.problems === 1 ? "" : "es"} not reachable - see Nodes & addresses`
          : data.conflicts.length ? `${data.conflicts.length} listener conflict(s)` : "no VIP/port conflicts" },
    ])}
    ${(data.platform_clashes || []).length ? `<div class="note bad" style="margin-bottom:14px"><b>${data.platform_clashes.length === 1 ? "An app is" : `${data.platform_clashes.length} apps are`} on the cluster's own address.</b>
      ${esc(data.platform_clashes.map(c => `${c.namespace}/${c.service}`).join(", "))} ${data.platform_clashes.length === 1 ? "uses" : "use"}
      <span class="mono">${esc(data.platform_clashes[0].ip)}</span>, which ${esc(data.platform_clashes[0].owner)} holds: the dashboard answers there and new hosts join
      through it, so sharing it can stop hosts joining. Give ${data.platform_clashes.length === 1 ? "it an address" : "each an address"} of its own (Edit → Network).</div>` : ""}
    ${(data.shared_vip || {}).problem ? `<div class="note bad" style="margin-bottom:14px"><b>Homestead's shared address is the cluster's own.</b> ${esc(data.shared_vip.problem)}.
      Choose a separate default workload VIP below; do not use the control-plane address.</div>` : ""}
    <section class="vip-section" aria-label="Workload VIPs">
    <div class="vip-section-heading"><div><h3>Workload VIPs</h3><p class="dim small">Stable LAN addresses for containers and pod-network VMs, separate from your hosts' addresses.</p></div>
      <button class="btn sm pri" data-need="admin" onclick="vipAdd()">＋ Add VIP</button></div>
    ${(data.registered_vips || []).length ? "" : `<ol class="vip-steps"><li><b>1 · Add an address</b><span>Reserve an unused address outside DHCP, then save it here.</span></li>
      <li><b>2 · Choose a default</b><span>Use <b>Make default</b> for the address suggested to new workloads.</span></li>
      <li><b>3 · Connect a workload</b><span>Use a card below, or choose <b>Default workload VIP</b> / <b>Specific VIP</b> in Deploy → Networking.</span></li></ol>`}
    <div id="selfAddress"></div>
    <p class="small vip-default-summary">A saved VIP is advertised once a workload's Service requests it. <b>Current default:</b> <span class="mono">${esc(data.shared_vip?.ip || "Not configured")}</span> · Multiple workloads can share it on different ports. Changing it does not move existing services.</p>
    ${(data.registered_vips || []).length ? `<div class="vip-cards">${data.registered_vips.map(v => networkVipCard(v, data)).join("")}</div>`
      : '<div class="card flat empty small">No saved VIPs. Start with <b>Add VIP</b> above. Adding an address does not change your router or start a workload.</div>'}
    </section>
    <div class="between"><div class="sec">LAN networks ${tip("Networks bridged to the LAN - Harvester calls them VM networks. A VM, or a container given an address of its own, joins one to be on the LAN like any machine there.")}</div>
      <button class="btn sm" data-need="admin" onclick="vmNetworkAdd()">＋ LAN network</button></div>
    <div id="netVmNets">${window.__vmCreateOptions ? networkVmNetsHtml(window.__vmCreateOptions) : '<div class="dim small">reading LAN networks…</div>'}</div>
    ${networkAddressesHtml(data)}
    <div class="sec" style="margin-top:22px">Services &amp; endpoint paths</div>
    ${orphans ? `<div class="note" style="margin-bottom:12px">${orphans === 1
      ? "<b>1 Service no longer points at a workload.</b> It still owns its VIP and port, so that number stays taken until the Service is removed."
      : `<b>${orphans} Services no longer point at a workload.</b> They still own their VIPs and ports, so those numbers stay taken until the Services are removed.`}</div>` : ""}
    <div class="card flat pad0"><div class="tblwrap"><table data-sort="services" class="tbl stack dense"><thead><tr><th>Service</th><th>Listeners</th><th>Traffic path</th><th>Health</th><th></th></tr></thead>
      <tbody>${services.map(row => `<tr><td class="netsvc"><b>${esc(row.name)}</b>${row.orphaned ? '<span class="tag warn" data-tip="No Deployment matches this Service selector, so nothing answers on it. Its VIP and port stay reserved until it is removed.">no workload</span>' : ""}<div class="dim xs mono">${esc(row.namespace)} · ${esc(row.type)}</div><div class="dim xs mono" title="Address inside the cluster">ClusterIP ${esc(row.cluster_ip || "—")}</div></td>
        <td>${row.ports.map(p => `<span class="tag">${p.port}/${esc(p.protocol)} → ${esc(p.target_port)}</span>`).join(" ")}</td>
        <td class="netpathcell"><div class="netpath"><span title="${esc(row.external_ips.join(", ") || "cluster only")}">${esc(row.external_ips[0] || row.cluster_ip || "pending")}${row.external_ips.length > 1 ? ` +${row.external_ips.length - 1}` : ""}</span><i>→</i><span>${esc(row.name)}</span><i>→</i><span>${row.ready_endpoints} endpoint${row.ready_endpoints === 1 ? "" : "s"}</span></div>
          <div class="dim xs">${row.endpoints.ready.map(e => `${esc(e.target || e.addresses[0] || "endpoint")} @ ${esc(e.node || "unknown node")}`).join(" · ") || "No ready target"}</div></td>
        <td><span class="pill ${networkPill(row.health)}">${esc(row.health)}</span><div class="dim xs" style="margin-top:5px">${esc(row.reason)}</div></td>
        <td>${row.system ? "" : `<button class="btn sm ${row.orphaned ? "danger" : ""}" data-need="admin" title="${row.orphaned ? "Release this listener" : "Remove this Service and take its workload off the LAN"}" onclick="networkServiceDelete(${jsq(row.namespace)},${jsq(row.name)})">${icon("trash")}${row.orphaned ? "Release" : "Remove"}</button>`}</td></tr>`).join("") || '<tr><td colspan="5" class="empty">No matching services</td></tr>'}</tbody></table></div></div>
    ${data.ingresses.length ? `<div class="sec" style="margin-top:22px">Ingress routes</div><div class="card flat pad0"><div class="tblwrap"><table data-sort="ingresses" class="tbl stack dense"><thead><tr><th>Ingress</th><th>Address</th><th>Route</th><th>Backend</th></tr></thead><tbody>${data.ingresses.filter(x => showSystem || !x.system).flatMap(row => row.rules.map(rule => `<tr><td>${esc(row.namespace)}/${esc(row.name)}</td><td class="mono">${esc(row.addresses.join(", ") || "pending")}</td><td>${esc(rule.host)}${esc(rule.path)}</td><td>${esc(rule.service)}:${esc(rule.port)}</td></tr>`)).join("")}</tbody></table></div></div>` : ""}`);
  networkVmNetsPaint();
  selfAddressPaint();
}

/* ---------------- Homestead itself ----------------
   Its web page, backup storage and shares: where each answers, and one
   step to put them all on a VIP beside the nodes' addresses. */
async function selfAddressPaint() {
  const host = $("#selfAddress");
  if (!host) return;
  let r;
  try { r = await api("/api/self/address"); } catch (e) { host.innerHTML = ""; return; }
  STATE.data.selfAddress = r;
  const rows = r.components.filter(c => c.present).map(c => [
    `<b>${esc(c.label)}</b>`,
    c.vip ? `<span class="mono">${esc(c.vip)}</span> ${UI.chip("VIP", "ok")}` : "",
    c.node_addresses.length ? `<span class="dim xs mono">${esc(c.node_addresses.join(", "))}</span>` : '<span class="dim xs">—</span>',
    `<span class="mono xs">${esc(c.ports.map(p => p.port).join(", "))}</span>`]);
  const vips = (STATE.data.network?.registered_vips || []).filter(v => !v.blocked);
  host.innerHTML = `<div class="card flat self-address">
    <div class="between"><div><div class="ctitle">Homestead itself</div>
      <div class="csub">${r.on_vip ? `On <span class="mono">${esc(r.components.find(c => c.id === "web").vip)}</span>: <a class="linkish" href="${esc(r.url)}" target="_blank" rel="noopener">${esc(r.url)}</a>`
        : "On the nodes' own addresses: they stop answering when that node is down. Put Homestead on a VIP."}</div></div>
      ${!r.on_vip && vips.length && !nodeAddressesOnly() ? UI.button("Put Homestead on a VIP", "selfAddressMove()", { kind: "pri", attrs: 'data-need="admin"' }) : ""}</div>
    ${UI.table([{ label: "Service" }, { label: "VIP" }, { label: "Node addresses" }, { label: "Ports" }], rows)}</div>`;
  if (window.applyRole) applyRole();
}
window.selfAddressPaint = selfAddressPaint;

window.selfAddressMove = async (preset = "") => {
  const net = STATE.data.network?.registered_vips ? STATE.data.network : (STATE.data.network = await api("/api/network"));
  const shared = net.shared_vip?.ip || "";
  // The default VIP may come from Homestead's own setting rather than the list.
  const vips = [...(shared && !(net.registered_vips || []).some(v => v.ip === shared) ? [{ ip: shared, label: "default for apps" }] : []),
    ...(net.registered_vips || []).filter(v => !v.blocked)];
  const first = preset || shared || vips[0]?.ip || "";
  childModal("Put Homestead on a VIP", `<div class="ui-stack">
    ${UI.lead("Homestead's page, its backup storage and its shares get a second connection on the VIP, with the same ports. Their node addresses keep working; nothing is restarted.")}
    <div class="f"><label>VIP</label><select id="sa_vip" onchange="selfAddressPlan()">${vips.map(v => `<option value="${esc(v.ip)}" ${v.ip === first ? "selected" : ""}>${esc(v.ip)}${v.label ? ` · ${esc(v.label)}` : ""}</option>`).join("")}</select></div>
    <label class="check"><input type="checkbox" id="sa_default" ${shared ? "" : "checked"}> Also make it the default for new apps</label>
    <div id="sa_plan" class="small"><span class="spin2"></span> checking</div>
    ${UI.actions(UI.button("Back", "modalBack()") + UI.button("Put Homestead here", "selfAddressGo()", { kind: "pri", id: "sa_go", attrs: 'data-need="admin"' }))}</div>`);
  selfAddressPlan();
};
window.selfAddressPlan = async () => {
  const out = $("#sa_plan");
  try {
    const r = await api("/api/self/address/plan", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ vip: $("#sa_vip").value }) });
    out.innerHTML = `<ul class="osu-results">${r.steps.map(s => `<li><b>${esc(s.label)}</b> · ${s.action === "add" ? "" : `${UI.chip(s.action, s.action === "refused" ? "bad" : "")} `}${esc(s.detail)}</li>`).join("")}</ul>`;
    $("#sa_go").disabled = !r.steps.some(s => s.action === "add");
  } catch (e) { out.innerHTML = UI.callout("bad", "It cannot go there.", esc(e.message)); $("#sa_go").disabled = true; }
};
window.selfAddressGo = async () => {
  const vip = $("#sa_vip").value, go = $("#sa_go");
  go.disabled = true; go.textContent = "Putting Homestead there…";
  try {
    const r = await api("/api/self/address", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ vip, default: $("#sa_default").checked }) });
    const added = r.steps.filter(s => s.action === "added").length, refused = r.steps.filter(s => s.action === "refused");
    toast(`${added} connection${added === 1 ? "" : "s"} on ${vip}${refused.length ? `; not moved: ${refused.map(s => s.label).join(", ")}` : ""}${r.default_error ? `; default not changed: ${r.default_error}` : ""}`, refused.length ? "warn" : "ok");
    modalBack(); if (HomesteadRouter.resolve(window.location.pathname).view === "network") viewNetworking();
  } catch (e) { toast(e.message, "bad"); go.disabled = false; go.textContent = "Put Homestead here"; }
};

/* A VIP's new address, and everything on it moved with it. */
window.vipChange = ip => {
  modal(`Change ${ip}`, `<div class="ui-stack">
    ${UI.lead("Give this VIP a new address. Every Service on it moves with it - its ports, and Homestead's own if they are here - and it stays the default if it is. Open connections drop for a moment.")}
    <div class="f"><label for="vc_new">New address</label><input id="vc_new" class="mono" placeholder="e.g. 192.0.2.210" autocomplete="off" data-ipam oninput="vipChangeReset()"></div>
    <label class="check"><input type="checkbox" id="vc_confirm"> The new address is reserved outside DHCP and no other device uses it</label>
    <div id="vc_plan" class="small"></div>
    ${UI.actions(UI.button("Cancel", "closeModal()") + UI.button("Review the move", `vipChangeReview(${jsArg(ip)})`, { kind: "pri", id: "vc_go", attrs: 'data-need="admin"' }))}</div>`);
};
window.vipChangeReset = () => { $("#vc_plan").innerHTML = ""; const go = $("#vc_go"); go.textContent = "Review the move"; go.dataset.ready = ""; };
window.vipChangeReview = async old => {
  const go = $("#vc_go"), fresh = $("#vc_new").value.trim();
  if (!$("#vc_confirm").checked) return toast("Confirm the new address is reserved and unused first", "bad");
  const apply = go.dataset.ready === fresh;
  go.disabled = true;
  try {
    const r = await api("/api/network/vips/change", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ old, new: fresh, apply }) });
    if (apply) { toast(r.detail, "ok"); closeModal(); viewNetworking(); return; }
    $("#vc_plan").innerHTML = r.services.length
      ? `<b>Moves to ${esc(fresh)}:</b><ul class="osu-results">${r.services.map(s => `<li><span class="mono">${esc(s.namespace)}/${esc(s.name)}</span> · ${esc(s.ports.join(", "))}</li>`).join("")}</ul>`
      : `Nothing uses ${esc(old)} yet; only the reserved address changes.`;
    go.dataset.ready = fresh; go.textContent = `Move to ${fresh}`;
  } catch (e) { $("#vc_plan").innerHTML = UI.callout("bad", "It cannot move there.", esc(e.message)); }
  go.disabled = false;
};

window.networkToggleSystem = () => { STATE.networkSystem = !STATE.networkSystem; viewNetworking(); };

window.networkServiceDelete = async (namespace, name) => {
  const row = (STATE.data.network?.services || []).find(item => item.namespace === namespace && item.name === name);
  const listeners = (row?.external_ips || []).flatMap(ip => (row?.ports || []).map(p => `${ip}:${p.port}/${p.protocol}`));
  const serving = row?.targets?.length
    ? `\n\n${name} still serves ${row.targets.join(", ")}, which will lose its LAN address.` : "";
  const frees = listeners.length ? `Frees ${listeners.join(", ")}.` : "It holds no external listener.";
  if (!(await ask(`Remove Service ${namespace}/${name}?${serving}\n\n${frees}`))) return;
  try {
    const result = await api("/api/network/service/delete", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ namespace, name, force: !!row?.targets?.length }) });
    toast(result.message || `${name} removed`, "ok"); resetPaint(); viewNetworking();
  } catch (e) { toast(e.message, "bad"); }
};

function networkModalPort(port = {}, first = false) {
  return `<div class="f4 net-port" data-port-name="${esc(port.name || "")}"><div><label>Port on your LAN</label><input class="np-port" type="number" min="1" max="65535" value="${esc(port.port || port.container || "")}"></div>
    <div><label>Port inside the app / VM</label><input class="np-target" type="text" value="${esc(port.target_port || port.port || port.container || "")}"></div>
    <div><label>Protocol</label><select class="np-protocol"><option>TCP</option><option ${port.protocol === "UDP" ? "selected" : ""}>UDP</option></select></div>
    <div><label>&nbsp;</label><button class="btn sm" type="button" onclick="this.closest('.net-port').remove();networkInvalidateReview()" ${first ? "disabled" : ""}>Remove</button></div></div>`;
}

function networkIsNodeAccess(row, data) {
  const ips = row?.external_ips || [];
  return row?.type === "LoadBalancer" && !row.lb_class && ips.length > 0 && ips.every(ip => (data.node_ips || []).includes(ip));
}
function networkAdditionalName(data, ns, workload) {
  const base = workload.slice(0, 55) + "-vip";
  let name = base, suffix = 2;
  while (data.services.some(s => s.namespace === ns && s.name === name)) name = `${base}-${suffix++}`;
  return name;
}
function networkVipCards(current, choices) {
  if (choices.blocked?.[current]) current = "";
  const addresses = [...new Set([...(choices.own || []).map(v => v.ip), ...choices.free, ...choices.used.map(v => v.ip)])];
  const typed = !!current && !addresses.includes(current);
  return `<input id="net_lb_ip" type="hidden" value="${esc(current)}"><div class="net-address-list" role="group" aria-label="Available VIPs">${addresses.map(ip => {
    const used = choices.used.find(v => v.ip === ip);
    const label = choices.labels[ip] || choices.own.find(v => v.ip === ip)?.label || "";
    return `<label class="net-address-choice"><input type="radio" name="net_address" value="${esc(ip)}" ${current === ip ? "checked" : ""} onchange="networkAddressPicked(this.value)"><span><b class="mono">${esc(ip)}</b>${label ? `<span>${esc(label)}</span>` : ""}<small>${used ? `In use · ${used.services} service(s) · ports ${esc(used.listeners.map(p => `${p.port}/${p.protocol || "TCP"}`).slice(0, 6).join(", "))}` : "Not assigned to a Service"}</small></span>${ip === choices.shared ? '<span class="tag ok">Default</span>' : ""}</label>`;
  }).join("") || '<p class="small">No saved VIPs yet. Add reserved addresses in Networking → Workload VIPs.</p>'}</div>
  <details class="net-custom-address" ${typed ? "open" : ""}><summary>Use another reserved address</summary><p class="small">Only use an unused LAN address reserved outside DHCP. Node and cluster-management addresses are not VIP choices.</p><label for="net_custom_ip">Reserved IPv4 address</label><input id="net_custom_ip" value="${typed ? esc(current) : ""}" placeholder="e.g. 192.0.2.230" oninput="networkAddressPicked(this.value,true)" data-ipam></details>`;
}
window.networkAddressPicked = (ip, typed = false) => {
  $("#net_lb_ip").value = ip.trim();
  if (typed) $$("input[name=net_address]").forEach(e => {e.checked = false;});
  else if ($("#net_custom_ip")) $("#net_custom_ip").value = "";
  networkInvalidateReview(); networkModeChanged();
};
window.networkAccessPicked = mode => {
  $("#net_mode").value = mode;
  networkModeChanged(); networkInvalidateReview();
};
function networkAccessChoices(data, choices) {
  const option = (mode, title, detail, disabled = false) => `<label class="net-access-choice${disabled ? " disabled" : ""}"><input type="radio" name="net_access" value="${mode}" ${disabled ? "disabled" : ""} onchange="networkAccessPicked(this.value)"><span><b>${title}</b><small>${detail}</small></span></label>`;
  return `<fieldset class="net-access-options"><legend>How should this app be reached?</legend>
    ${option("shared", "Use the default VIP", choices.shared ? `<span class="mono">${esc(choices.shared)}</span> · Share this address using different ports.` : "No usable default set. Choose one in Networking → Workload VIPs.", !choices.shared || nodeAddressesOnly())}
    ${option("manual", "Choose a VIP", "Select a reserved address below. Host addresses are excluded.", nodeAddressesOnly())}
    ${STATE.platform?.servicelb ? option("nodes", "Use node addresses", "Keep access through the hosts' own IPs. This is not a movable VIP.") : ""}
    <label class="net-access-choice net-auto-choice"><input type="radio" name="net_access" value="automatic" onchange="networkAccessPicked(this.value)" ${nodeAddressesOnly() ? "disabled" : ""}><span><b>Pick an unused VIP for me</b><small>The review will show the address before anything changes.</small></span></label>
    </fieldset>`;
}

window.networkExpose = async (namespace = "", name = "", kind = "Deployment", selectedVip = "", editing = false) => {
  const data = STATE.data.network = await api("/api/network");
  if (!data?.workloads?.length) return toast("No Deployments or pod-network VMs are available to expose", "bad");
  if (name && !data.workloads.some(row => row.namespace === namespace && row.name === name && (row.kind || "Deployment") === kind)) return toast(kind === "VirtualMachine" ? "VM exists but is not eligible for Service exposure yet. It needs a masquerade pod interface and unique template labels; bridged VMs use their guest LAN address. Try Edit → Network after saving." : "This workload is not available for Service exposure. Save it first, then retry.", "bad");
  if (name) data.workloads.sort((a, b) => Number(b.namespace === namespace && b.name === name && (b.kind || "Deployment") === kind) - Number(a.namespace === namespace && a.name === name && (a.kind || "Deployment") === kind));
  // Opened without an app - from a VIP's Use this VIP, or Expose workload -
  // nothing is picked for you: the first app in the list (often Homestead
  // itself) is not a guess worth making about someone's address.
  const choose = !name;
  window.__netSelectedVip = selectedVip;
  const options = (choose ? '<option value="" selected>Choose an app or VM…</option>' : "")
    + data.workloads.map(row => `<option value="${esc(row.namespace + "/" + row.name + "/" + (row.kind || "Deployment"))}">${esc(row.kind || "Deployment")} · ${esc(row.namespace)}/${esc(row.name)}${row.name === "homestead" ? " (Homestead itself)" : ""}</option>`).join("");
  const first = choose ? { name: "", ports: [] } : data.workloads[0];
  const choices = await vipChoices(data);
  window.__networkChoices = choices;
  const open = editing && modalIsOpen() ? childModal : modal;
  open(editing ? `Network access · ${name}` : selectedVip ? `Use ${selectedVip}` : "Connect an app to your network", `<div id="net_editor" data-nested="${editing ? "1" : "0"}">
    <p class="net-intro">${selectedVip && choose ? `Choose the app or VM that devices on your LAN will reach at <b class="mono">${esc(selectedVip)}</b>, then its ports.`
      : "Choose the address and ports devices on your LAN use to reach this app."} Nothing changes until you review and apply.</p>
    <div class="f" ${editing ? "hidden" : ""}><label for="net_workload">Which app or VM?</label><select id="net_workload" onchange="networkWorkloadChanged()">${options}</select></div>
    <div id="net_rest" ${choose ? "hidden" : ""}>
    <div id="net_current_access" class="net-current-access"></div>
    <div id="net_mode_wrap">${networkAccessChoices(data, choices)}</div>
    <select id="net_mode" hidden><option value="shared">Default VIP</option><option value="manual">Selected VIP</option><option value="nodes">Node addresses</option><option value="automatic">Unused VIP</option></select>
    <div class="f hidden" id="net_vip_wrap">${networkVipCards(selectedVip, choices)}</div>
    <div id="net_change_note" class="net-change-note" role="status"></div>
    <div class="sec">Ports</div><p class="net-help">Devices connect to the LAN port; traffic is forwarded to the port inside the app or VM.
      <span id="net_ports_from"></span></p>
    <div id="net_ports">${networkModalPort(first.ports[0] || {}, true)}</div>
    <button class="btn sm" type="button" onclick="$('#net_ports').insertAdjacentHTML('beforeend',networkModalPort());networkInvalidateReview()">＋ Add port</button>
    <details class="net-advanced"><summary>Advanced · Kubernetes Service</summary>
      <p class="net-help">A Service is the Kubernetes object holding this connection. Its name is not an IP address.</p>
      <div class="f"><label for="net_existing">Connection to edit</label><select id="net_existing" onchange="networkServicePicked()"></select></div>
      <div class="f2"><div class="f"><label for="net_name">Kubernetes Service name</label><input id="net_name" type="text" value="${esc(first.name)}"></div>
      <div class="f"><label for="net_type">Reachability</label><select id="net_type" onchange="networkModeChanged()"><option value="LoadBalancer">LAN access</option><option value="ClusterIP">Cluster only</option></select></div></div></details>
    <div id="net_review"></div><div class="modalactions"><button class="btn" onclick="modalBack()">Cancel</button><button class="btn pri" onclick="networkReview()">Review changes</button></div></div></div>`, true);
  $("#net_workload").disabled = editing;
  if (!choose) {
    networkServiceOptions(editing);
    networkUseSelectedVip();
    networkModeChanged();
  }
  // A changed form always requires a fresh review.
  $("#net_editor").addEventListener("input", networkInvalidateReview);
  $("#net_editor").addEventListener("change", networkInvalidateReview);
  $(".modalbox").scrollTop = 0;
};

window.networkManage = (ns, name, kind = "Deployment") => {
  if ($("#e_containers") && editPortSignature() !== window.__editPortBaseline) return toast("Save your changed container ports first, then configure its VIP. Other unsaved fields can be kept.", "bad");
  return networkExpose(ns, name, kind, "", true);
};
function networkServiceOptions(selectExisting = false) {
  const [ns, name, kind] = $("#net_workload").value.split("/");
  const target = kind === "VirtualMachine" ? `VirtualMachine/${name}` : name;
  const rows = (STATE.data.network.services || []).filter(s => s.namespace === ns && (s.targets || []).includes(target) && !s.system)
    .sort((a, b) => Number(networkIsNodeAccess(a, STATE.data.network)) - Number(networkIsNodeAccess(b, STATE.data.network)));
  $("#net_existing").innerHTML = '<option value="">Create another connection</option>' + rows.map(s => `<option value="${esc(s.name)}" data-uid="${esc(s.uid || "")}" data-rv="${esc(s.resource_version || "")}">${esc(s.name)} · ${networkIsNodeAccess(s, STATE.data.network) ? "Node access" : s.type === "ClusterIP" ? "Cluster only" : "VIP access"}</option>`).join("");
  if (selectExisting && rows.length) { $("#net_existing").value = rows[0].name; networkServicePicked(); }
  else networkServicePicked();
}
window.networkServicePicked = () => {
  const [ns, name] = $("#net_workload").value.split("/");
  const existing = $("#net_existing").value;
  const row = STATE.data.network.services.find(s => s.namespace === ns && s.name === existing);
  $("#net_name").value = row ? row.name : networkAdditionalName(STATE.data.network, ns, name);
  $("#net_name").disabled = !!row;
  $("#net_type").disabled = !!row;
  if (row) {
    $("#net_type").value = row.type;
    $("#net_ports").innerHTML = row.ports.map((p, i) => networkModalPort(p, i === 0)).join("");
    $("#net_ports_from").textContent = "";
    const ip = networkIsNodeAccess(row, STATE.data.network) ? "" : row.requested_ips?.[0] || row.external_ips?.[0] || "";
    // Preserve the selected address on open, even if today's default has changed.
    $("#net_mode").value = ip && !nodeAddressesOnly() ? (row.vip_mode === "shared" && ip === STATE.data.network.shared_vip?.ip ? "shared" : "manual") : "nodes";
    $("#net_vip_wrap").innerHTML = networkVipCards(ip, window.__networkChoices);
  } else {
    $("#net_type").value = "LoadBalancer";
    $("#net_mode").value = window.__networkChoices.shared ? "shared" : nodeAddressesOnly() ? "nodes" : "automatic";
    $("#net_vip_wrap").innerHTML = networkVipCards("", window.__networkChoices);
    // Another connection starts with the ports the app is already reached
    // on - Homestead's 8088, say - not the port it listens on inside.
    const target = STATE.data.network.services.find(s => s.namespace === ns && s.type === "LoadBalancer" && !s.system
      && (s.targets || []).includes(name) && (s.ports || []).length);
    const workload = STATE.data.network.workloads.find(w => w.namespace === ns && w.name === name);
    $("#net_ports").innerHTML = target ? target.ports.map((p, i) => networkModalPort(p, i === 0)).join("")
      : networkModalPort(workload?.ports?.[0] || {}, true);
    $("#net_ports_from").textContent = target
      ? `The same ports as its current connection, ${target.name}: ${target.ports.map(p => `${p.port} → ${p.target_port || p.port}`).join(", ")}.`
      : "";
  }
  const node = networkIsNodeAccess(row, STATE.data.network);
  $("#net_current_access").innerHTML = row ? `<span class="net-eyebrow">Current connection</span><b>${node ? "Node address — not a VIP" : row.type === "ClusterIP" ? "Cluster only" : "VIP access"}</b><span class="mono">${esc((row.external_ips || []).map(ip => row.ports.map(p => `${ip}:${p.port} (${p.protocol})`).join(" · ")).join(" · ") || "No LAN address")}</span>${node ? '<small>k3s exposes these ports on the host itself. This address does not move to another host if it goes offline.</small>' : ""}` : `<span class="net-eyebrow">New connection</span><b>How devices on your LAN reach ${esc(name)}${name === "homestead" ? " - Homestead itself" : ""}</b>`;
  networkModeChanged(); networkInvalidateReview();
};
let NETWORK_REVIEW = null;
function networkInvalidateReview() {
  NETWORK_REVIEW = null;
  if ($("#net_review")) $("#net_review").innerHTML = "";
  const actions = $("#net_editor .modalactions");
  if (actions) actions.innerHTML = '<button class="btn" onclick="modalBack()">Cancel</button><button class="btn pri" onclick="networkReview()">Review changes</button>';
}

/* The VIP the dialog was opened for, on a new connection. */
function networkUseSelectedVip() {
  const vip = window.__netSelectedVip;
  if (!vip || nodeAddressesOnly() || $("#net_existing").value) return;
  $("#net_mode").value = "manual";
  $("#net_vip_wrap").innerHTML = networkVipCards(vip, window.__networkChoices);
}

window.networkWorkloadChanged = () => {
  const picked = !!$("#net_workload").value;
  $("#net_rest").hidden = !picked;
  if (!picked) return;
  const [namespace, name, kind] = $("#net_workload").value.split("/");
  $("#net_name").value = name;
  const workload = STATE.data.network.workloads.find(row => row.namespace === namespace && row.name === name && (row.kind || "Deployment") === kind);
  $("#net_ports").innerHTML = networkModalPort(workload?.ports?.[0] || {}, true);
  $("#net_name").disabled = false; $("#net_type").disabled = false;
  networkServiceOptions();
  networkUseSelectedVip();
  networkModeChanged();
};

window.networkModeChanged = () => {
  const external = $("#net_type").value === "LoadBalancer";
  $("#net_mode_wrap").classList.toggle("hidden", !external);
  $("#net_vip_wrap").classList.toggle("hidden", !external || $("#net_mode").value !== "manual");
  const mode = $("#net_mode").value;
  $$("input[name=net_access]").forEach(e => {e.checked = e.value === mode;});
  const [ns] = $("#net_workload").value.split("/");
  const row = STATE.data.network.services.find(s => s.namespace === ns && s.name === $("#net_existing").value);
  const adding = external && networkIsNodeAccess(row, STATE.data.network) && mode !== "nodes";
  $("#net_change_note").textContent = !external ? "Only reachable inside the cluster." : adding
    ? "Adds VIP access alongside the current node connection. The existing node address keeps working; nothing is removed."
    : mode === "shared" ? `Uses ${window.__networkChoices.shared || "the default VIP"}. Apps can share this address when their LAN ports are different.`
    : mode === "nodes" ? "Uses the host address, not a movable VIP. A single-node cluster cannot provide failover."
    : "Review the address and ports below before applying. Changing an existing VIP may interrupt open connections.";
};

function networkConfig() {
  const [namespace, workload, workload_kind] = $("#net_workload").value.split("/");
  const existing = STATE.data.network.services.find(s => s.namespace === namespace && s.name === $("#net_existing").value);
  const additional = $("#net_type").value === "LoadBalancer" && networkIsNodeAccess(existing, STATE.data.network) && $("#net_mode").value !== "nodes";
  return { namespace, workload, workload_kind, name: additional ? networkAdditionalName(STATE.data.network, namespace, workload) : $("#net_name").value.trim(), type: $("#net_type").value,
    vip_mode: $("#net_type").value === "ClusterIP" ? "cluster" : $("#net_mode").value,
    update: !!existing && !additional, uid: additional ? "" : $("#net_existing").selectedOptions[0]?.dataset.uid || "",
    resource_version: additional ? "" : $("#net_existing").selectedOptions[0]?.dataset.rv || "",
    vip: $("#net_lb_ip").value.trim(), ports: $$(".net-port").map((row, index) => ({
      name: row.dataset.portName || `port-${index + 1}`, port: $(".np-port", row).value, target_port: $(".np-target", row).value,
      protocol: $(".np-protocol", row).value })) };
}

window.networkReview = async () => {
  NETWORK_REVIEW = null;
  try {
    const cfg = networkConfig();
    const plan = await api("/api/network/plan", {method: "POST", headers: {"Content-Type": "application/json", "X-Homestead-Auth": "1"}, body: JSON.stringify(cfg)});
    if (!$("#net_editor") || JSON.stringify(networkConfig()) !== JSON.stringify(cfg)) return;
    NETWORK_REVIEW = { ...cfg, reviewed_vip: plan.vip || "" };
    $("#net_review").innerHTML = `<div class="reviewbox"><b>After applying</b><div class="netpath big"><span>${esc(plan.path.vip)}</span><i>→</i><span>${esc(cfg.workload)}</span></div>
      <div class="dim xs">${plan.ports.map(p => `${p.port}/${p.protocol} → ${p.targetPort}`).join(" · ")}</div>${plan.warnings.map(w => `<div class="tag warn" style="margin-top:8px">${esc(w)}</div>`).join("")}</div>`;
    const actions = $("#mbody .modalactions");
    actions.innerHTML = '<button class="btn" onclick="modalBack()">Cancel</button><button class="btn pri" onclick="networkCreate()">Apply changes</button>';
  } catch (error) { toast(error.message, "bad"); }
};

window.networkCreate = async () => {
  if (!NETWORK_REVIEW) return toast("Review the current VIP and ports before saving", "bad");
  const cfg = NETWORK_REVIEW; NETWORK_REVIEW = null;
  try {
    const result = await api("/api/network/services", {method: "POST", headers: {"Content-Type": "application/json", "X-Homestead-Auth": "1"}, body: JSON.stringify(cfg)});
    const nested = $("#net_editor")?.dataset.nested === "1";
    if (nested) modalBack(); else closeModal();
    if (nested && cfg.workload_kind === "Deployment" && $("#e_containers")) {
      // Reflect separately saved listeners so a later workload save cannot restore stale ports.
      $$("#e_containers .edit-port-row").forEach(row => {
        const port = cfg.ports.find(p => p.protocol === $(".ep-protocol", row).value &&
          (String(p.target_port) === $(".ep-number", row).value || String(p.target_port) === $(".ep-name", row).value));
        $(".ep-expose", row).checked = !!port;
        if (port) $(".ep-host", row).value = port.port;
      });
      window.__editHadService = true;
      editPortsChanged(); window.__editPortBaseline = editPortSignature();
    }
    toast(result.message, "ok");
    if (STATE.view === "network") await viewNetworking();
  } catch (error) { toast(error.message, "bad"); networkInvalidateReview(); }
};

/* ---------------- your VIPs ----------------
   Addresses kept for Homestead to hand to Services, one or a range. */
window.vipDefault = async ip => {
  if (!(await ask(`Use ${ip} by default for new workload Services? Existing Services and Homestead's access address are unchanged. Each shared port must be unique.`))) return;
  try {
    const r = await api("/api/network/vips/default", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ ip }) });
    if (STATE.data.ov) STATE.data.ov.lb_ip = ip;
    toast(r.detail, "ok"); viewNetworking();
  } catch (e) { toast(e.message, "bad"); }
};
window.vipAdd = () => {
  const provider = STATE.platform?.load_balancer;
  const supported = ["kube-vip", "metallb"].includes(provider);
  modal("Add workload VIP", `<div class="vip-add-form">
    <p class="small">A VIP is a stable LAN address for reaching apps, such as <span class="mono">http://VIP:port</span>. Save one address for shared ports, or a range so workloads can have separate addresses.</p>
    <div class="note small"><b>Before you add an address</b><br>Choose an unused IPv4 address on the LAN your load balancer serves, outside your router's DHCP allocation range. Do not use a host, router or cluster-management address. Homestead checks known cluster conflicts, but cannot confirm that another LAN device is not using it.</div>
    <div class="f"><label for="va_kind">What would you like to add?</label><select id="va_kind" onchange="vipAddKindChanged()"><option value="single">One VIP</option><option value="range">A range of VIPs</option></select></div>
    <div class="f2"><div class="f"><label for="va_start" id="va_start_label">VIP address</label><input id="va_start" class="mono" placeholder="e.g. 192.0.2.230" autocomplete="off" data-ipam></div>
      <div class="f hidden" id="va_end_wrap"><label for="va_end">Last address (inclusive)</label><input id="va_end" class="mono" placeholder="e.g. 192.0.2.239" autocomplete="off" data-ipam><span class="dim xs">Up to 64 addresses per range.</span></div></div>
    <div class="f"><label for="va_label">Label (optional)</label><input id="va_label" maxlength="60" placeholder="e.g. Shared apps or Media"><span class="dim xs">Shown in the VIP list and workload address picker.</span></div>
    <label class="vip-check"><input id="va_default" type="checkbox" ${supported ? "" : "disabled"} ${supported && !STATE.data.network?.shared_vip?.ip ? "checked" : ""}><span><b>Make this the default workload VIP</b><span class="dim small" id="va_default_hint">New workloads can share this address on different ports. Existing services will not move.</span></span></label>
    ${supported && STATE.data.selfAddress && !STATE.data.selfAddress.on_vip ? `<label class="vip-check"><input id="va_self" type="checkbox" checked><span><b>Put Homestead itself here</b><span class="dim small">Its page (port 8088), backup storage and shares get a connection on this address too; their node addresses keep working.</span></span></label>` : ""}
    <p class="small dim">${provider === "kube-vip" ? "kube-vip advertises the address when a workload Service requests it; saving it alone does not make it respond." : provider === "metallb" ? "MetalLB must also have these addresses in a configured address pool. Saving them here does not configure MetalLB's pools." : "You can save addresses now. Install kube-vip or MetalLB in Settings → Cluster → Add-ons before using a movable workload VIP; ServiceLB uses node addresses."}</p>
    <label class="vip-check"><input id="va_confirm" type="checkbox"><span>I have checked that these addresses are reserved outside DHCP and are not used by another device.</span></label>
    <div id="va_result" class="small" role="status" aria-live="polite"></div>
    <div class="modalactions"><button class="btn" onclick="closeModal()">Cancel</button><button class="btn pri" id="va_save" onclick="vipAddGo()">Add VIP</button></div></div>`);
};
window.vipAddKindChanged = () => {
  const range = $("#va_kind").value === "range";
  $("#va_end_wrap").classList.toggle("hidden", !range);
  $("#va_start_label").textContent = range ? "First address" : "VIP address";
  $("#va_save").textContent = range ? "Add VIP range" : "Add VIP";
  $("#va_default_hint").textContent = `${range ? "The first address in the range will be the default. " : ""}New workloads can share this address on different ports. Existing services will not move.`;
};
window.vipAddGo = async () => {
  const result = $("#va_result"), save = $("#va_save");
  if (save.disabled) return;
  if (!$("#va_confirm").checked) { result.textContent = "Check your DHCP settings and confirm the addresses are unused before adding them."; return; }
  const body = { start: $("#va_start").value.trim(), end: $("#va_kind").value === "range" ? $("#va_end").value.trim() : "", label: $("#va_label").value };
  if (!body.start || ($("#va_kind").value === "range" && !body.end)) { result.textContent = "Enter the VIP address (and last address for a range)."; return; }
  const makeDefault = $("#va_default").checked;
  save.disabled = true;
  result.textContent = "Saving addresses…";
  try {
    const r = await api("/api/network/vips/add", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
    // A range may partially succeed. Never silently default to a different address.
    if (makeDefault) {
      try {
        await api("/api/network/vips/default", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ ip: body.start }) });
        if (STATE.data.ov) STATE.data.ov.lb_ip = body.start;
      } catch (e) {
        result.textContent = `${r.detail}. Default was not changed: ${e.message}. Saved addresses remain available; you can use Make default on their cards.`;
        await viewNetworking(); return;
      }
    }
    let self = "";
    if ($("#va_self")?.checked && r.added.includes(body.start)) {
      try {
        const moved = await api("/api/self/address", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ vip: body.start }) });
        const refused = moved.steps.filter(s => s.action === "refused");
        self = refused.length ? ` · Homestead not moved for ${refused.map(s => s.label).join(", ")}: ${refused[0].detail}` : ` · Homestead is on ${body.start} too`;
      } catch (e) { self = ` · Homestead was not moved: ${e.message}`; }
    }
    if ((r.skipped || []).length || !r.added.length) {
      result.textContent = `${r.detail}${makeDefault ? ` · Default workload VIP: ${body.start}` : ""}${self}`;
      await viewNetworking(); return;
    }
    closeModal(); toast((makeDefault ? `${r.detail} · ${body.start} is the default workload VIP` : `${r.detail} · Choose Use this VIP to connect a workload`) + self, "ok");
    await viewNetworking();
  } catch (e) { result.textContent = e.message; }
  finally { save.disabled = false; }
};
window.vipRemove = async ip => {
  if (!(await ask(`Stop keeping ${ip} for Homestead?`))) return;
  try {
    const r = await api("/api/network/vips/remove", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ ip }) });
    toast(r.detail, "ok"); viewNetworking();
  } catch (e) { toast(e.message, "bad"); }
};
window.vipLabel = async ip => {
  const current = (STATE.data.network?.vip_labels || {})[ip] || "";
  const label = (await askText(`Label for ${ip}`, current));
  if (label === null) return;
  try {
    await api("/api/network/vips/label", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ ip, label }) });
    viewNetworking();
  } catch (e) { toast(e.message, "bad"); }
};


/* The VM networks there are, from the same list the VM and container forms
   choose from. The page repaints on its own every few seconds, so it draws the last list
   it had at once and swaps in the new one when it comes, rather than going
   back to "reading" each time. */
async function networkVmNetsPaint() {
  const opts = await api("/api/vm/create-options").catch(() => null);
  if (opts) window.__vmCreateOptions = opts;
  const host = $("#netVmNets");
  if (!host) return;
  const html = networkVmNetsHtml(window.__vmCreateOptions || {});
  if (host.innerHTML !== html) host.innerHTML = html;
  if (window.applyRole) applyRole();
}

function networkVmNetsHtml(opts) {
  return networkVmNetsList(opts) + networkHostBridgesHtml(opts);
}

/* Off Harvester: hosts whose LAN is a plain NIC, which a bridge would let
   VMs and containers share with the host itself (homestead_host_bridge). */
function networkHostBridgesHtml(opts) {
  const o = opts.vm_network_options || {};
  if (o.harvester || !o.interfaces) return "";
  const bridged = n => o.interfaces.some(i => i.kind === "bridge" && !["cni0", "docker0", "virbr0"].includes(i.name) && i.nodes.includes(n));
  const plain = (opts.nodes || []).filter(n => !bridged(n));
  if (!plain.length) return "";
  return `<div class="dim xs" style="margin-top:10px">No host bridge on ${plain.map(n => `<b class="mono">${esc(n)}</b>
      <button class="btn sm" data-need="admin" onclick="nodeBridge(${jsq(n)})">Move into a bridge…</button>`).join(" ")}
    ${tip("VMs reach the LAN through macvtap without one, but then their own host cannot reach them. A bridge (br0) carries VMs, containers and the host alike, as Proxmox's vmbr0 does.")}</div>`;
}

window.nodeBridge = async node => {
  modal(`Host bridge · ${node}`, '<div class="empty"><span class="spin2"></span> Looking at the host\'s network…</div>');
  let f;
  try {
    f = await api("/api/node/bridge/inspect", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ node }) });
  } catch (e) { $("#mbody").innerHTML = UI.callout("bad", "The host's network could not be read.", esc(e.message)) + UI.actions(UI.cancel("Close")); return; }
  const facts = f.interface ? UI.facts([["Interface", `<span class="mono">${esc(f.interface)}</span> · ${esc(f.mac)}`],
    ["Address", `${esc(f.address)} · ${f.dhcp ? "from DHCP" : "static"}`], ["Gateway", esc(f.gateway)],
    ["Set up in", `<span class="mono">${esc(f.file || "?")}</span>`]]) : "";
  $("#mbody").innerHTML = `<div class="ui-stack">${facts}
    ${f.problem ? UI.callout("warn", "Not converted", `${esc(f.problem[0].toUpperCase() + f.problem.slice(1))}.`) + UI.actions(UI.cancel("Close")) : `
      <p class="small"><span class="mono">${esc(f.interface)}</span>'s address, routes and DNS move to a new bridge, <span class="mono">${esc(f.bridge)}</span>,
        which takes ${esc(f.interface)}'s MAC address${f.dhcp ? " and asks DHCP as that MAC, so your router gives it the same address" : ""}.</p>
      ${UI.callout("warn", `${esc(node)} drops off the network for a few seconds`,
        `netplan's files are copied aside first, and a rollback is armed on the host: unless Homestead sees ${esc(f.address)} on ${esc(f.bridge)}
         with the gateway answering, the host puts its old network back by itself after ${Math.round(f.rollback_seconds / 60)} minutes.
         kube-vip then starts again on ${esc(f.bridge)}${(f.services || []).length ? `, and on a cluster of several hosts ${esc(f.services[0])} restarts so flannel follows` : ""}. Containers keep running.`)}
      <div class="f"><label>Type <span class="mono">${esc(node)}</span> to confirm</label><input id="nb_confirm" class="mono" autocomplete="off"></div>
      ${UI.actions(UI.cancel() + UI.button(`Move into ${f.bridge}`, `nodeBridgeGo(${jsArg(node)})`, { kind: "danger", id: "nb_go", attrs: 'data-need="admin"' }))}`}</div>`;
  if (window.applyRole) applyRole();
};
window.nodeBridgeGo = async node => {
  const go = $("#nb_go");
  if (go) { go.disabled = true; go.textContent = "Moving…"; }
  try {
    const r = await api("/api/node/bridge", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ node, confirm: $("#nb_confirm").value }) });
    toast(r.detail, "ok"); closeModal();
  } catch (e) {
    toast(e.message, "bad");
    if (go) { go.disabled = false; go.textContent = "Move into br0"; }
  }
};

function networkVmNetsList(opts) {
  const rows = opts.network_details || [];
  return rows.length ? `<div class="vip-own">${rows.map(n => `<div class="vip-chip ${n.lan ? "free" : "used"}">
      <div class="vip-name"><b class="mono">${esc(n.name)}</b><span class="dim xs">${n.vlan ? `VLAN ${esc(n.vlan)}` : n.lan ? "untagged" : esc(n.type || "network")}${n.bridge ? ` · ${esc(n.bridge)}` : ""}</span></div>
      <span class="tag ${n.lan ? "ok" : ""}" ${n.type === "macvlan" ? 'data-tip="macvlan: containers only - a VM needs a macvtap network or one on a host bridge"' : n.type === "macvtap" ? 'data-tip="macvtap: VMs only, each with its own address on the LAN; a VM cannot be reached from its own host this way"' : ""}>${n.lan ? (n.type === "macvlan" ? "LAN · containers" : n.type === "macvtap" ? "LAN · VMs" : "LAN") : "not bridged"}</span></div>`).join("")}</div>`
    : `<div class="card flat empty small">No LAN networks yet. <b>＋ LAN network</b> makes one on your LAN, untagged like the hosts or on a VLAN.</div>`;
}
