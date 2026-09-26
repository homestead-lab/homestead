/* Services, VIPs, listeners, endpoints and ingress */

const networkPill = health => health === "healthy" ? "ok" : health === "pending" ? "med" : "crit";
const networkPortText = row => (row.ports || []).map(p => `${p.port}/${p.protocol}`).join(", ");

async function viewNetworking() {
  if (networkTab() === "ip") return viewIpam();
  const data = await api("/api/network");
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
      <div class="row"><button class="btn" onclick="networkToggleSystem()">${showSystem ? "Hide" : "Show"} system</button>
      <button class="btn pri" data-need="operator" onclick="networkExpose()">＋ Expose workload</button></div></div>
    ${networkTabs("services")}
    <div class="note" style="margin-bottom:14px"><b>Networking roles</b><br>
      <b>${esc(controller.name)}</b> handles service addresses. Multus adds separate LAN interfaces; it does not provide VIP failover.
      ${STATE.platform?.servicelb ? "ServiceLB also exposes unclassified Services on node IPs, not a movable VIP." : ""}
      <div class="small" style="margin-top:8px"><b>Default workload VIP:</b> ${esc(data.shared_vip?.ip || "not configured")}. Choose a default below, then select <b>Default workload VIP</b> or <b>Specific VIP</b> when deploying.
      VMs on the pod network can be exposed through a Service; bridged VMs use their own DHCP/static address, not a service VIP.</div>
      <div class="small dim" style="margin-top:8px">VIP failover is not full-cluster HA: multiple eligible hosts, a surviving control-plane quorum, portable storage with healthy replicas, and workload restart policies are also needed.
      ${Object.keys(data.node_names || {}).length < 2 ? "This is a single-node cluster: there is no second host to take over." : "Test host failure before relying on recovery."}</div></div>
    <div class="grid g4 statgrid" style="margin-bottom:18px">
      <div class="card glow ${controller.healthy ? "g-ok" : "g-bad"}"><div class="ctitle">Load balancer</div>
        <div class="bignum" style="margin-top:8px">${controller.ready}<span class="unit">/${controller.desired}</span></div>
        <div class="csub">${esc(controller.name)} agents ready · ${esc(controller.mode)}</div></div>
      <div class="card flat"><div class="ctitle">Virtual IPs</div><div class="bignum" style="margin-top:8px">${data.summary.vips}</div>
        <div class="csub">${data.summary.listeners} LAN listeners · ${data.available_vip_count} unused in pools</div></div>
      <div class="card flat"><div class="ctitle">Application services</div><div class="bignum" style="margin-top:8px">${data.summary.app_services}</div>
        <div class="csub">${data.summary.ready_endpoints} ready endpoints</div></div>
      <div class="card flat"><div class="ctitle">Attention</div><div class="bignum" style="margin-top:8px">${data.summary.unhealthy}</div>
        <div class="csub">${data.conflicts.length ? `${data.conflicts.length} listener conflict(s)` : "no VIP/port conflicts"}</div></div>
    </div>
    ${(data.platform_clashes || []).length ? `<div class="note bad" style="margin-bottom:14px"><b>${data.platform_clashes.length === 1 ? "An app is" : `${data.platform_clashes.length} apps are`} on the cluster's own address.</b>
      ${esc(data.platform_clashes.map(c => `${c.namespace}/${c.service}`).join(", "))} ${data.platform_clashes.length === 1 ? "uses" : "use"}
      <span class="mono">${esc(data.platform_clashes[0].ip)}</span>, which ${esc(data.platform_clashes[0].owner)} holds: the dashboard answers there and new hosts join
      through it, so sharing it can stop hosts joining. Give ${data.platform_clashes.length === 1 ? "it an address" : "each an address"} of its own (Edit → Network).</div>` : ""}
    ${(data.shared_vip || {}).problem ? `<div class="note bad" style="margin-bottom:14px"><b>Homestead's shared address is the cluster's own.</b> ${esc(data.shared_vip.problem)}.
      Choose a separate default workload VIP below; do not use the control-plane address.</div>` : ""}
    <div class="between"><div class="sec">Your VIPs ${tip("Reserved LAN addresses for Services, outside DHCP. kube-vip advertises requested addresses; MetalLB also requires them in its configured pool. Automatic allocation uses this list first. A default VIP shares free ports between workloads; changing it does not move existing Services.")}</div>
      <button class="btn sm pri" data-need="admin" onclick="vipAdd()">＋ Add VIPs</button></div>
    ${(data.registered_vips || []).length ? `<div class="vip-own">${data.registered_vips.map(v => `<div class="vip-chip ${v.blocked ? "used" : v.free ? "free" : "used"}">
        <div class="vip-name"><b class="mono">${esc(v.ip)}</b><span class="dim xs" onclick="vipLabel('${esc(v.ip)}')" data-tip="Rename">${esc(v.label || "no label")}</span></div>
        ${v.default ? '<span class="tag ok">default workload VIP</span>' : !v.blocked ? `<button class="btn sm" data-need="admin" onclick="vipDefault('${esc(v.ip)}')">Use as default</button>` : ""}
        ${v.blocked ? `<span class="tag bad" data-tip="${esc(v.blocked)}">not usable</span>`
          : `<span class="tag ${v.free ? "ok" : "info"}">${v.free ? "free" : esc(v.used_by.join(", ") || "in use")}</span>`}
        ${v.free || v.blocked ? `<button class="iconbtn" data-need="admin" data-tip="No longer keep this address for Homestead" onclick="vipRemove('${esc(v.ip)}')">×</button>` : ""}</div>`).join("")}</div>`
      : `<div class="card flat empty small">No VIPs of your own yet. Add the addresses Homestead may give to apps and shares - ${data.available_vip_count
          ? `${data.available_vip_count} more are free in Harvester IP pools.` : "there are no Harvester IP pools to take them from either."}</div>`}
    <div class="between"><div class="sec">LAN networks ${tip("Networks bridged to the LAN - Harvester calls them VM networks. A VM, or a container given an address of its own, joins one to be on the LAN like any machine there.")}</div>
      <button class="btn sm" data-need="admin" onclick="vmNetworkAdd()">＋ LAN network</button></div>
    <div id="netVmNets">${window.__vmCreateOptions ? networkVmNetsHtml(window.__vmCreateOptions) : '<div class="dim small">reading LAN networks…</div>'}</div>
    <div class="between"><div class="sec">Virtual IPs &amp; port ownership</div><div class="row">${data.available_vips.filter(ip => !(data.vip_labels || {}).hasOwnProperty(ip)).slice(0, 6).map(ip => `<span class="tag ok" title="Unused address in a ready Harvester IP pool">${esc(ip)} available</span>`).join("")}</div></div>
    <div class="cardlist network-vips">${data.vips.map(vip => `<div class="card flat">
      <div class="between"><div><div class="dim xs">${vip.shared ? "SHARED VIP" : "VIRTUAL IP"}</div><b class="mono">${esc(vip.ip)}</b></div>
        <span class="tag ${vip.listeners.some(x => x.health !== "healthy") ? "warn" : "ok"}">${vip.services} service${vip.services === 1 ? "" : "s"}</span></div>
      <div class="netlisteners">${vip.listeners.map(item => `<div class="drow"><div class="dl"><b>${item.port}/${esc(item.protocol)}</b>
        <span class="dim">${esc(item.namespace)}/${esc(item.service)}</span></div><div class="dv">${item.browser
          ? `<a class="tag info" href="${esc(item.access)}" target="_blank" rel="noopener">Open ${icon("ext")}</a>`
          : `<span class="tag">${esc(item.access)}</span>`}</div></div>`).join("")}</div></div>`).join("") || '<div class="card flat empty">No external VIPs</div>'}</div>
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
        <td>${row.system ? "" : `<button class="btn sm ${row.orphaned ? "danger" : ""}" data-need="admin" title="${row.orphaned ? "Release this listener" : "Remove this Service and take its workload off the LAN"}" onclick="networkServiceDelete('${esc(row.namespace)}','${esc(row.name)}')">${icon("trash")}${row.orphaned ? "Release" : "Remove"}</button>`}</td></tr>`).join("") || '<tr><td colspan="5" class="empty">No matching services</td></tr>'}</tbody></table></div></div>
    ${data.ingresses.length ? `<div class="sec" style="margin-top:22px">Ingress routes</div><div class="card flat pad0"><div class="tblwrap"><table data-sort="ingresses" class="tbl stack dense"><thead><tr><th>Ingress</th><th>Address</th><th>Route</th><th>Backend</th></tr></thead><tbody>${data.ingresses.filter(x => showSystem || !x.system).flatMap(row => row.rules.map(rule => `<tr><td>${esc(row.namespace)}/${esc(row.name)}</td><td class="mono">${esc(row.addresses.join(", ") || "pending")}</td><td>${esc(rule.host)}${esc(rule.path)}</td><td>${esc(rule.service)}:${esc(rule.port)}</td></tr>`)).join("")}</tbody></table></div></div>` : ""}`);
  networkVmNetsPaint();
}

window.networkToggleSystem = () => { STATE.networkSystem = !STATE.networkSystem; viewNetworking(); };

window.networkServiceDelete = async (namespace, name) => {
  const row = (STATE.data.network?.services || []).find(item => item.namespace === namespace && item.name === name);
  const listeners = (row?.external_ips || []).flatMap(ip => (row?.ports || []).map(p => `${ip}:${p.port}/${p.protocol}`));
  const serving = row?.targets?.length
    ? `\n\n${name} still serves ${row.targets.join(", ")}, which will lose its LAN address.` : "";
  const frees = listeners.length ? `Frees ${listeners.join(", ")}.` : "It holds no external listener.";
  if (!confirm(`Remove Service ${namespace}/${name}?${serving}\n\n${frees}`)) return;
  try {
    const result = await api("/api/network/service/delete", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ namespace, name, force: !!row?.targets?.length }) });
    toast(result.message || `${name} removed`, "ok"); resetPaint(); viewNetworking();
  } catch (e) { toast(e.message, "bad"); }
};

function networkModalPort(port = {}, first = false) {
  return `<div class="f4 net-port"><div><label>LAN port</label><input class="np-port" type="number" min="1" max="65535" value="${esc(port.port || port.container || "")}"></div>
    <div><label>Container port</label><input class="np-target" type="number" min="1" max="65535" value="${esc(port.port || port.container || "")}"></div>
    <div><label>Protocol</label><select class="np-protocol"><option>TCP</option><option ${port.protocol === "UDP" ? "selected" : ""}>UDP</option></select></div>
    <div><label>&nbsp;</label><button class="btn sm" type="button" onclick="this.closest('.net-port').remove()" ${first ? "disabled" : ""}>Remove</button></div></div>`;
}

window.networkExpose = async (namespace = "", name = "", kind = "Deployment") => {
  const data = STATE.data.network = await api("/api/network");
  if (!data?.workloads?.length) return toast("No Deployments or pod-network VMs are available to expose", "bad");
  if (name && !data.workloads.some(row => row.namespace === namespace && row.name === name && row.kind === kind)) return toast("This VM needs a masquerade pod interface and template labels to expose a Service; bridged VMs use their guest LAN address", "bad");
  if (name) data.workloads.sort((a, b) => Number(b.namespace === namespace && b.name === name && b.kind === kind) - Number(a.namespace === namespace && a.name === name && a.kind === kind));
  const options = data.workloads.map(row => `<option value="${esc(row.namespace + "/" + row.name + "/" + (row.kind || "Deployment"))}">${esc(row.kind || "Deployment")} · ${esc(row.namespace)}/${esc(row.name)}</option>`).join("");
  const first = data.workloads[0];
  modal("Expose workload", `<p class="dim">Create a Kubernetes Service with a collision-checked address and listener. Nothing changes until you review the plan.</p>
    <div class="f2"><div class="f"><label>Workload</label><select id="net_workload" onchange="networkWorkloadChanged()">${options}</select></div>
      <div class="f"><label>Service name</label><input id="net_name" type="text" value="${esc(first.name)}"></div></div>
    <div class="f2"><div class="f"><label>Reachability ${tip("Cluster only is reachable inside Kubernetes. LAN access asks the cluster's load balancer to advertise a Service address.")}</label><select id="net_type" onchange="networkModeChanged()"><option value="LoadBalancer">LAN access</option><option value="ClusterIP">Cluster only</option></select></div>
      <div class="f" id="net_mode_wrap"><label>VIP allocation ${tip("Default shares your configured workload address on free ports. Automatic chooses another reserved address. Specific uses the address you choose. No VM guest address is changed.")}</label><select id="net_mode" onchange="networkModeChanged()">${nodeAddressesOnly() ? nodeAddressOption() : `${data.shared_vip?.ip && !(data.node_ips || []).includes(data.shared_vip.ip) ? `<option value="shared">Default workload VIP · ${esc(data.shared_vip.ip)}</option>` : ""}<option value="automatic">New automatic VIP</option>${nodeAddressChoice()}<option value="manual">Specific VIP</option>`}</select></div></div>
    <div class="f hidden" id="net_vip_wrap"><label>Specific VIP</label><input id="net_vip" type="text" class="mono" placeholder="192.168.1.243"></div>
    <div class="sec">Listeners</div><div id="net_ports">${networkModalPort(first.ports[0] || {}, true)}</div>
    <button class="btn sm" type="button" onclick="$('#net_ports').insertAdjacentHTML('beforeend',networkModalPort())">＋ Add listener</button>
    <div id="net_review"></div><div class="modalactions"><button class="btn" onclick="closeModal()">Cancel</button><button class="btn pri" onclick="networkReview()">Review plan</button></div>`, true);
  networkModeChanged();
};

window.networkWorkloadChanged = () => {
  const [namespace, name, kind] = $("#net_workload").value.split("/");
  $("#net_name").value = name;
  const workload = STATE.data.network.workloads.find(row => row.namespace === namespace && row.name === name && (row.kind || "Deployment") === kind);
  $("#net_ports").innerHTML = networkModalPort(workload?.ports?.[0] || {}, true);
};

window.networkModeChanged = () => {
  const external = $("#net_type").value === "LoadBalancer";
  $("#net_mode_wrap").classList.toggle("hidden", !external);
  $("#net_vip_wrap").classList.toggle("hidden", !external || $("#net_mode").value !== "manual");
};

function networkConfig() {
  const [namespace, workload, workload_kind] = $("#net_workload").value.split("/");
  return { namespace, workload, workload_kind, name: $("#net_name").value.trim(), type: $("#net_type").value,
    vip_mode: $("#net_type").value === "ClusterIP" ? "cluster" : $("#net_mode").value,
    vip: $("#net_vip").value.trim(), ports: $$(".net-port").map((row, index) => ({
      name: `port-${index + 1}`, port: $(".np-port", row).value, target_port: $(".np-target", row).value,
      protocol: $(".np-protocol", row).value })) };
}

window.networkReview = async () => {
  try {
    const cfg = networkConfig();
    const plan = await api("/api/network/plan", {method: "POST", headers: {"Content-Type": "application/json", "X-Homestead-Auth": "1"}, body: JSON.stringify(cfg)});
    $("#net_review").innerHTML = `<div class="reviewbox"><b>Traffic path</b><div class="netpath big"><span>${esc(plan.path.vip)}</span><i>→</i><span>${esc(plan.path.service)}</span><i>→</i><span>${esc(plan.path.workload)}</span></div>
      <div class="dim xs">${plan.ports.map(p => `${p.port}/${p.protocol} → ${p.targetPort}`).join(" · ")}</div>${plan.warnings.map(w => `<div class="tag warn" style="margin-top:8px">${esc(w)}</div>`).join("")}</div>`;
    const actions = $("#mbody .modalactions");
    actions.innerHTML = `<button class="btn" onclick="closeModal()">Cancel</button><button class="btn pri" onclick="networkCreate()">Create service</button>`;
  } catch (error) { toast(error.message, "bad"); }
};

window.networkCreate = async () => {
  try {
    const result = await api("/api/network/services", {method: "POST", headers: {"Content-Type": "application/json", "X-Homestead-Auth": "1"}, body: JSON.stringify(networkConfig())});
    closeModal(); toast(result.message, "ok"); await viewNetworking();
  } catch (error) { toast(error.message, "bad"); }
};

/* ---------------- your VIPs ----------------
   Addresses kept for Homestead to hand to Services, one or a range. */
window.vipDefault = async ip => {
  if (!confirm(`Use ${ip} by default for new workload Services? Existing Services and Homestead's access address are unchanged. Each shared port must be unique.`)) return;
  try {
    const r = await api("/api/network/vips/default", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ ip }) });
    if (STATE.data.ov) STATE.data.ov.lb_ip = ip;
    toast(r.detail, "ok"); viewNetworking();
  } catch (e) { toast(e.message, "bad"); }
};
window.vipAdd = () => modal("Add VIPs", `
  <p class="small">Addresses Homestead may give to apps and shares. Choose ones outside your router's DHCP range, so nothing else takes them.</p>
  <div class="f2"><div class="f"><label>Address</label><input id="va_start" class="mono" placeholder="192.168.1.230"></div>
    <div class="f"><label>Up to (optional)</label><input id="va_end" class="mono" placeholder="192.168.1.239"></div></div>
  <div class="f"><label>Label</label><input id="va_label" maxlength="60" placeholder="e.g. Media apps, Pi-hole, Shares"></div>
  <div class="row" style="margin-top:14px"><button class="btn pri" onclick="vipAddGo()">Add</button><button class="btn" onclick="closeModal()">Cancel</button></div>`);
window.vipAddGo = async () => {
  const body = { start: $("#va_start").value.trim(), end: $("#va_end").value.trim(), label: $("#va_label").value };
  try {
    const r = await api("/api/network/vips/add", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
    toast(r.detail, r.added.length ? "ok" : "warn");
    if (r.added.length) { closeModal(); viewNetworking(); }
  } catch (e) { toast(e.message, "bad"); }
};
window.vipRemove = async ip => {
  if (!confirm(`Stop keeping ${ip} for Homestead?`)) return;
  try {
    const r = await api("/api/network/vips/remove", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ ip }) });
    toast(r.detail, "ok"); viewNetworking();
  } catch (e) { toast(e.message, "bad"); }
};
window.vipLabel = async ip => {
  const current = (STATE.data.network?.vip_labels || {})[ip] || "";
  const label = prompt(`Label for ${ip}`, current);
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
  const rows = opts.network_details || [];
  return rows.length ? `<div class="vip-own">${rows.map(n => `<div class="vip-chip ${n.lan ? "free" : "used"}">
      <div class="vip-name"><b class="mono">${esc(n.name)}</b><span class="dim xs">${n.vlan ? `VLAN ${esc(n.vlan)}` : n.lan ? "untagged" : esc(n.type || "network")}${n.bridge ? ` · ${esc(n.bridge)}` : ""}</span></div>
      <span class="tag ${n.lan ? "ok" : ""}" ${n.type === "macvlan" ? 'data-tip="macvlan: containers only - a VM needs a network on a host bridge"' : ""}>${n.lan ? (n.type === "macvlan" ? "LAN · containers" : "LAN") : "not bridged"}</span></div>`).join("")}</div>`
    : `<div class="card flat empty small">No LAN networks yet. <b>＋ LAN network</b> makes one on your LAN, untagged like the hosts or on a VLAN.</div>`;
}
