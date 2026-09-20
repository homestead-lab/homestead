/* Services, VIPs, listeners, endpoints and ingress */

const networkPill = health => health === "healthy" ? "ok" : health === "pending" ? "med" : "crit";
const networkPortText = row => (row.ports || []).map(p => `${p.port}/${p.protocol}`).join(", ");

async function viewNetworking() {
  const data = await api("/api/network");
  STATE.data.network = data;
  const q = STATE.q.toLowerCase();
  const showSystem = !!STATE.networkSystem;
  const services = data.services.filter(row => (showSystem || !row.system) && (!q ||
    [row.namespace, row.name, row.cluster_ip, ...row.external_ips, networkPortText(row), ...row.targets]
      .join(" ").toLowerCase().includes(q)));
  const controller = data.controller;
  paint(`<div class="phead"><div><h2>Networking</h2>
      <p>Addresses, listeners and the live path from your LAN to each workload</p></div>
      <div class="row"><button class="btn" onclick="networkToggleSystem()">${showSystem ? "Hide" : "Show"} system</button>
      <button class="btn pri" data-need="operator" onclick="networkExpose()">＋ Expose workload</button></div></div>
    <div class="grid g4" style="margin-bottom:18px">
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
    <div class="between"><div class="sec">Virtual IPs &amp; port ownership</div><div class="row">${data.available_vips.slice(0, 6).map(ip => `<span class="tag ok" title="Unused address in a ready Harvester IP pool">${esc(ip)} available</span>`).join("")}</div></div>
    <div class="cardlist network-vips">${data.vips.map(vip => `<div class="card flat">
      <div class="between"><div><div class="dim xs">${vip.shared ? "SHARED VIP" : "VIRTUAL IP"}</div><b class="mono">${esc(vip.ip)}</b></div>
        <span class="tag ${vip.listeners.some(x => x.health !== "healthy") ? "warn" : "ok"}">${vip.services} service${vip.services === 1 ? "" : "s"}</span></div>
      <div class="netlisteners">${vip.listeners.map(item => `<div class="drow"><div class="dl"><b>${item.port}/${esc(item.protocol)}</b>
        <span class="dim">${esc(item.namespace)}/${esc(item.service)}</span></div><div class="dv">${item.browser
          ? `<a class="tag info" href="${esc(item.access)}" target="_blank" rel="noopener">Open ${icon("ext")}</a>`
          : `<span class="tag">${esc(item.access)}</span>`}</div></div>`).join("")}</div></div>`).join("") || '<div class="card flat empty">No external VIPs</div>'}</div>
    <div class="sec" style="margin-top:22px">Services &amp; endpoint paths</div>
    <div class="card flat pad0"><div class="tblwrap"><table class="tbl"><thead><tr><th>Service</th><th>Addresses</th><th>Listeners</th><th>Traffic path</th><th>Health</th></tr></thead>
      <tbody>${services.map(row => `<tr><td><b>${esc(row.name)}</b><div class="dim xs mono">${esc(row.namespace)} · ${esc(row.type)}</div></td>
        <td><div class="mono small">${row.external_ips.map(esc).join(" · ") || "cluster only"}</div><div class="dim xs mono">ClusterIP ${esc(row.cluster_ip || "—")}</div></td>
        <td>${row.ports.map(p => `<span class="tag">${p.port}/${esc(p.protocol)} → ${esc(p.target_port)}</span>`).join(" ")}</td>
        <td><div class="netpath"><span>${esc(row.external_ips[0] || row.cluster_ip || "pending")}</span><i>→</i><span>${esc(row.name)}</span><i>→</i><span>${row.ready_endpoints} endpoint${row.ready_endpoints === 1 ? "" : "s"}</span></div>
          <div class="dim xs">${row.endpoints.ready.map(e => `${esc(e.target || e.addresses[0] || "endpoint")} @ ${esc(e.node || "unknown node")}`).join(" · ") || "No ready target"}</div></td>
        <td><span class="pill ${networkPill(row.health)}">${esc(row.health)}</span><div class="dim xs" style="margin-top:5px">${esc(row.reason)}</div></td></tr>`).join("") || '<tr><td colspan="5" class="empty">No matching services</td></tr>'}</tbody></table></div></div>
    ${data.ingresses.length ? `<div class="sec" style="margin-top:22px">Ingress routes</div><div class="card flat pad0"><div class="tblwrap"><table class="tbl dense"><thead><tr><th>Ingress</th><th>Address</th><th>Route</th><th>Backend</th></tr></thead><tbody>${data.ingresses.filter(x => showSystem || !x.system).flatMap(row => row.rules.map(rule => `<tr><td>${esc(row.namespace)}/${esc(row.name)}</td><td class="mono">${esc(row.addresses.join(", ") || "pending")}</td><td>${esc(rule.host)}${esc(rule.path)}</td><td>${esc(rule.service)}:${esc(rule.port)}</td></tr>`)).join("")}</tbody></table></div></div>` : ""}`);
}

window.networkToggleSystem = () => { STATE.networkSystem = !STATE.networkSystem; viewNetworking(); };

function networkModalPort(port = {}, first = false) {
  return `<div class="f4 net-port"><div><label>LAN port</label><input class="np-port" type="number" min="1" max="65535" value="${esc(port.port || port.container || "")}"></div>
    <div><label>Container port</label><input class="np-target" type="number" min="1" max="65535" value="${esc(port.port || port.container || "")}"></div>
    <div><label>Protocol</label><select class="np-protocol"><option>TCP</option><option ${port.protocol === "UDP" ? "selected" : ""}>UDP</option></select></div>
    <div><label>&nbsp;</label><button class="btn sm" type="button" onclick="this.closest('.net-port').remove()" ${first ? "disabled" : ""}>Remove</button></div></div>`;
}

window.networkExpose = () => {
  const data = STATE.data.network;
  if (!data?.workloads?.length) return toast("No Deployments are available to expose", "bad");
  const options = data.workloads.map(row => `<option value="${esc(row.namespace + "/" + row.name)}">${esc(row.namespace)}/${esc(row.name)}</option>`).join("");
  const first = data.workloads[0];
  modal("Expose workload", `<p class="dim">Create a Kubernetes Service with a collision-checked address and listener. Nothing changes until you review the plan.</p>
    <div class="f2"><div class="f"><label>Workload</label><select id="net_workload" onchange="networkWorkloadChanged()">${options}</select></div>
      <div class="f"><label>Service name</label><input id="net_name" type="text" value="${esc(first.name)}"></div></div>
    <div class="f2"><div class="f"><label>Reachability ${tip("Cluster only is reachable inside Kubernetes. LAN access asks kube-vip to advertise an address on your network.")}</label><select id="net_type" onchange="networkModeChanged()"><option value="LoadBalancer">LAN access</option><option value="ClusterIP">Cluster only</option></select></div>
      <div class="f" id="net_mode_wrap"><label>VIP allocation ${tip("Automatic reserves the first unused address in a Harvester IP pool. Shared reuses the Homestead address when the requested port is free.")}</label><select id="net_mode" onchange="networkModeChanged()"><option value="automatic">New automatic VIP</option><option value="shared">Shared Homestead VIP</option><option value="manual">Specific VIP</option></select></div></div>
    <div class="f hidden" id="net_vip_wrap"><label>Specific VIP</label><input id="net_vip" type="text" class="mono" placeholder="192.0.2.243"></div>
    <div class="sec">Listeners</div><div id="net_ports">${networkModalPort(first.ports[0] || {}, true)}</div>
    <button class="btn sm" type="button" onclick="$('#net_ports').insertAdjacentHTML('beforeend',networkModalPort())">＋ Add listener</button>
    <div id="net_review"></div><div class="modalactions"><button class="btn" onclick="closeModal()">Cancel</button><button class="btn pri" onclick="networkReview()">Review plan</button></div>`, true);
  networkModeChanged();
};

window.networkWorkloadChanged = () => {
  const [namespace, name] = $("#net_workload").value.split("/");
  $("#net_name").value = name;
  const workload = STATE.data.network.workloads.find(row => row.namespace === namespace && row.name === name);
  $("#net_ports").innerHTML = networkModalPort(workload?.ports?.[0] || {}, true);
};

window.networkModeChanged = () => {
  const external = $("#net_type").value === "LoadBalancer";
  $("#net_mode_wrap").classList.toggle("hidden", !external);
  $("#net_vip_wrap").classList.toggle("hidden", !external || $("#net_mode").value !== "manual");
};

function networkConfig() {
  const [namespace, workload] = $("#net_workload").value.split("/");
  return { namespace, workload, name: $("#net_name").value.trim(), type: $("#net_type").value,
    vip_mode: $("#net_type").value === "ClusterIP" ? "cluster" : $("#net_mode").value,
    vip: $("#net_vip").value.trim(), ports: $$(".net-port").map((row, index) => ({
      name: `port-${index + 1}`, port: $(".np-port", row).value, target_port: $(".np-target", row).value,
      protocol: $(".np-protocol", row).value })) };
}

window.networkReview = async () => {
  try {
    const cfg = networkConfig();
    const plan = await api("/api/network/plan", {method: "POST", headers: {"Content-Type": "application/json", "X-HarvUI-Auth": "1"}, body: JSON.stringify(cfg)});
    $("#net_review").innerHTML = `<div class="reviewbox"><b>Traffic path</b><div class="netpath big"><span>${esc(plan.path.vip)}</span><i>→</i><span>${esc(plan.path.service)}</span><i>→</i><span>${esc(plan.path.workload)}</span></div>
      <div class="dim xs">${plan.ports.map(p => `${p.port}/${p.protocol} → ${p.targetPort}`).join(" · ")}</div>${plan.warnings.map(w => `<div class="tag warn" style="margin-top:8px">${esc(w)}</div>`).join("")}</div>`;
    const actions = $("#mbody .modalactions");
    actions.innerHTML = `<button class="btn" onclick="closeModal()">Cancel</button><button class="btn pri" onclick="networkCreate()">Create service</button>`;
  } catch (error) { toast(error.message, "bad"); }
};

window.networkCreate = async () => {
  try {
    const result = await api("/api/network/services", {method: "POST", headers: {"Content-Type": "application/json", "X-HarvUI-Auth": "1"}, body: JSON.stringify(networkConfig())});
    closeModal(); toast(result.message, "ok"); await viewNetworking();
  } catch (error) { toast(error.message, "bad"); }
};
