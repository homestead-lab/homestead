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
    <div class="vip-card-usage small">${blocked ? esc(blocked) : (v.used_by || []).length ? `<span class="dim">Used by</span> ${(v.used_by || []).map(n => `<span class="mono">${esc(n)}</span>`).join("")}` : 'Saved for use. It is advertised when a workload Service requests it.'}</div>
    <div class="vip-card-actions">
      ${canUse ? `<button class="btn sm" data-need="operator" onclick="networkExpose('','','Deployment','${esc(v.ip)}')">Use this VIP</button>` : ""}
      ${canUse && !isDefault ? `<button class="btn sm" data-need="admin" onclick="vipDefault('${esc(v.ip)}')">Make default</button>` : ""}
      <button class="btn sm" data-need="admin" onclick="vipLabel('${esc(v.ip)}')">Edit label</button>
      ${!isDefault && (v.free || blocked) ? `<button class="btn sm" data-need="admin" onclick="vipRemove('${esc(v.ip)}')">Remove</button>` : ""}
    </div></article>`;
}

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
    <section class="vip-section" aria-label="Workload VIPs">
    <div class="vip-section-heading"><div><h3>Workload VIPs</h3><p class="dim small">Stable LAN addresses for containers and pod-network VMs, separate from your hosts' addresses.</p></div>
      <button class="btn sm pri" data-need="admin" onclick="vipAdd()">＋ Add VIP</button></div>
    <ol class="vip-steps"><li><b>1 · Add an address</b><span>Reserve an unused address outside DHCP, then save it here.</span></li>
      <li><b>2 · Choose a default</b><span>Use <b>Make default</b> for the address suggested to new workloads.</span></li>
      <li><b>3 · Connect a workload</b><span>Use a card below, or choose <b>Default workload VIP</b> / <b>Specific VIP</b> in Deploy → Networking.</span></li></ol>
    <p class="small vip-default-summary"><b>Current default:</b> <span class="mono">${esc(data.shared_vip?.ip || "Not configured")}</span> · Multiple workloads can share it on different ports. Changing it does not move existing services.</p>
    ${(data.registered_vips || []).length ? `<div class="vip-cards">${data.registered_vips.map(v => networkVipCard(v, data)).join("")}</div>`
      : '<div class="card flat empty small">No saved VIPs. Start with <b>Add VIP</b> above. Adding an address does not change your router or start a workload.</div>'}
    </section>
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
  <details class="net-custom-address" ${typed ? "open" : ""}><summary>Use another reserved address</summary><p class="small">Only use an unused LAN address reserved outside DHCP. Node and cluster-management addresses are not VIP choices.</p><label for="net_custom_ip">Reserved IPv4 address</label><input id="net_custom_ip" value="${typed ? esc(current) : ""}" placeholder="e.g. 192.168.1.230" oninput="networkAddressPicked(this.value,true)"></details>`;
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
  const options = data.workloads.map(row => `<option value="${esc(row.namespace + "/" + row.name + "/" + (row.kind || "Deployment"))}">${esc(row.kind || "Deployment")} · ${esc(row.namespace)}/${esc(row.name)}</option>`).join("");
  const first = data.workloads[0];
  const choices = await vipChoices(data);
  window.__networkChoices = choices;
  const open = editing && modalIsOpen() ? childModal : modal;
  open(editing ? `Network access · ${name}` : "Connect a workload to your network", `<div id="net_editor" data-nested="${editing ? "1" : "0"}">
    <p class="net-intro">Choose the address and ports devices on your LAN use to reach this app. Nothing changes until you review and apply.</p>
    <div class="f" ${editing ? "hidden" : ""}><label for="net_workload">App or VM</label><select id="net_workload" onchange="networkWorkloadChanged()">${options}</select></div>
    <div id="net_current_access" class="net-current-access"></div>
    <div id="net_mode_wrap">${networkAccessChoices(data, choices)}</div>
    <select id="net_mode" hidden><option value="shared">Default VIP</option><option value="manual">Selected VIP</option><option value="nodes">Node addresses</option><option value="automatic">Unused VIP</option></select>
    <div class="f hidden" id="net_vip_wrap">${networkVipCards(selectedVip, choices)}</div>
    <div id="net_change_note" class="net-change-note" role="status"></div>
    <div class="sec">Ports</div><p class="net-help">Devices connect to the LAN port; traffic is forwarded to the port inside the app or VM.</p>
    <div id="net_ports">${networkModalPort(first.ports[0] || {}, true)}</div>
    <button class="btn sm" type="button" onclick="$('#net_ports').insertAdjacentHTML('beforeend',networkModalPort());networkInvalidateReview()">＋ Add port</button>
    <details class="net-advanced"><summary>Advanced · Kubernetes Service</summary>
      <p class="net-help">A Service is the Kubernetes object holding this connection. Its name is not an IP address.</p>
      <div class="f"><label for="net_existing">Connection to edit</label><select id="net_existing" onchange="networkServicePicked()"></select></div>
      <div class="f2"><div class="f"><label for="net_name">Kubernetes Service name</label><input id="net_name" type="text" value="${esc(first.name)}"></div>
      <div class="f"><label for="net_type">Reachability</label><select id="net_type" onchange="networkModeChanged()"><option value="LoadBalancer">LAN access</option><option value="ClusterIP">Cluster only</option></select></div></div></details>
    <div id="net_review"></div><div class="modalactions"><button class="btn" onclick="modalBack()">Cancel</button><button class="btn pri" onclick="networkReview()">Review changes</button></div></div>`, true);
  networkServiceOptions(editing);
  $("#net_workload").disabled = editing;
  if (selectedVip && !nodeAddressesOnly()) {
    $("#net_mode").value = "manual";
    $("#net_vip_wrap").innerHTML = networkVipCards(selectedVip, choices);
  }
  networkModeChanged();
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
    const ip = networkIsNodeAccess(row, STATE.data.network) ? "" : row.requested_ips?.[0] || row.external_ips?.[0] || "";
    // Preserve the selected address on open, even if today's default has changed.
    $("#net_mode").value = ip && !nodeAddressesOnly() ? (row.vip_mode === "shared" && ip === STATE.data.network.shared_vip?.ip ? "shared" : "manual") : "nodes";
    $("#net_vip_wrap").innerHTML = networkVipCards(ip, window.__networkChoices);
  } else {
    $("#net_type").value = "LoadBalancer";
    $("#net_mode").value = window.__networkChoices.shared ? "shared" : nodeAddressesOnly() ? "nodes" : "automatic";
    $("#net_vip_wrap").innerHTML = networkVipCards("", window.__networkChoices);
  }
  const node = networkIsNodeAccess(row, STATE.data.network);
  $("#net_current_access").innerHTML = row ? `<span class="net-eyebrow">Current connection</span><b>${node ? "Node address — not a VIP" : row.type === "ClusterIP" ? "Cluster only" : "VIP access"}</b><span class="mono">${esc((row.external_ips || []).map(ip => row.ports.map(p => `${ip}:${p.port} (${p.protocol})`).join(" · ")).join(" · ") || "No LAN address")}</span>${node ? '<small>k3s exposes these ports on the host itself. This address does not move to another host if it goes offline.</small>' : ""}` : '<span class="net-eyebrow">New connection</span><b>Choose how to reach this app or VM</b>';
  networkModeChanged(); networkInvalidateReview();
};
let NETWORK_REVIEW = null;
function networkInvalidateReview() {
  NETWORK_REVIEW = null;
  if ($("#net_review")) $("#net_review").innerHTML = "";
  const actions = $("#net_editor .modalactions");
  if (actions) actions.innerHTML = '<button class="btn" onclick="modalBack()">Cancel</button><button class="btn pri" onclick="networkReview()">Review changes</button>';
}

window.networkWorkloadChanged = () => {
  const [namespace, name, kind] = $("#net_workload").value.split("/");
  $("#net_name").value = name;
  const workload = STATE.data.network.workloads.find(row => row.namespace === namespace && row.name === name && (row.kind || "Deployment") === kind);
  $("#net_ports").innerHTML = networkModalPort(workload?.ports?.[0] || {}, true);
  $("#net_name").disabled = false; $("#net_type").disabled = false;
  networkServiceOptions();
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
  if (!confirm(`Use ${ip} by default for new workload Services? Existing Services and Homestead's access address are unchanged. Each shared port must be unique.`)) return;
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
    <div class="f2"><div class="f"><label for="va_start" id="va_start_label">VIP address</label><input id="va_start" class="mono" placeholder="e.g. 192.168.1.230" autocomplete="off"></div>
      <div class="f hidden" id="va_end_wrap"><label for="va_end">Last address (inclusive)</label><input id="va_end" class="mono" placeholder="e.g. 192.168.1.239" autocomplete="off"><span class="dim xs">Up to 64 addresses per range.</span></div></div>
    <div class="f"><label for="va_label">Label (optional)</label><input id="va_label" maxlength="60" placeholder="e.g. Shared apps or Media"><span class="dim xs">Shown in the VIP list and workload address picker.</span></div>
    <label class="vip-check"><input id="va_default" type="checkbox" ${supported ? "" : "disabled"}><span><b>Make this the default workload VIP</b><span class="dim small" id="va_default_hint">New workloads can share this address on different ports. Existing services and Homestead's access address will not move.</span></span></label>
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
  $("#va_default_hint").textContent = `${range ? "The first address in the range will be the default. " : ""}New workloads can share this address on different ports. Existing services and Homestead's access address will not move.`;
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
    if ((r.skipped || []).length || !r.added.length) {
      result.textContent = `${r.detail}${makeDefault ? ` · Default workload VIP: ${body.start}` : ""}`;
      await viewNetworking(); return;
    }
    closeModal(); toast(makeDefault ? `${r.detail} · ${body.start} is the default workload VIP` : `${r.detail} · Choose Use this VIP to connect a workload`, "ok");
    await viewNetworking();
  } catch (e) { result.textContent = e.message; }
  finally { save.disabled = false; }
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
