/* Homestead Choosing a virtual IP and the Address step (#911). One of the scripts views-workloads.js was split into; they share the page's globals and load in the order index.html lists them. */
/* ---------------- choosing a virtual IP ----------------
   A specific virtual IP is picked from what the cluster has: the free addresses in
   Harvester's IP pools, or a virtual IP already in use, whose ports are then shared.
   "Type an address" is there for one outside the pools. */
async function vipChoices(networkData) {
  // An unread network is not "no addresses": the caller says it could not read it (#620).
  const net = networkData || await mustRead("/api/network", "the network");
  // The cluster's own address (Harvester's management virtual IP, an ingress
  // controller's) and addresses other software owns are never offered: an app
  // sharing the management virtual IP is how host joining breaks.
  const closed = { ...(net?.platform_addresses || {}), ...(net?.foreign_addresses || {}) };
  for (const ip of net?.node_ips || []) closed[ip] = "Cluster host address";
  for (const service of net?.services || []) {
    if (service.exclusive_vip) for (const ip of service.external_ips || []) closed[ip] = service.name;
  }
  const own = (net?.registered_vips || []).filter(v => !v.blocked && !closed[v.ip]);
  const mine = new Set(own.map(v => v.ip));
  return { free: (net?.available_vips || []).filter(ip => !mine.has(ip) && !closed[ip]), freeCount: net?.available_vip_count || 0,
    shared: closed[net?.shared_vip?.ip] ? "" : net?.shared_vip?.ip || "",
    used: (net?.vips || []).filter(v => !closed[v.ip]), own, labels: net?.vip_labels || {}, blocked: closed };
}
window.vipChoices = vipChoices;

/* The Address step - how clients reach a workload, and on which ports -
   drawn the same in Deploy (prefix d) and Edit (prefix e). Edit cannot turn
   host networking on or off; a workload deployed with it shows it as set. */
function addressStepHtml(prefix, cfg, vips, sharedVip, { lanHtml = "", portsHtml = "", onChange = "" } = {}) {
  const editing = prefix !== "d", host = cfg.network_mode === "host";
  const change = onChange ? ` onchange="${onChange}"` : "";
  const mode = value => cfg.network_mode === value ? "selected" : "";
  const vip = value => cfg.vip_mode === value ? "selected" : "";
  return `
      <div class="sec">Network ${tip("Kubernetes replaces Docker bridge networking with services. Use a separate virtual IP for DNS servers and other apps that need common ports to themselves.")}</div>
      <div class="f2"><div class="f"><label>How clients connect</label><select id="${prefix}_net"${change}${editing && host ? " disabled" : ""}>
        <option value="loadbalancer" ${mode("loadbalancer")}>LAN access (virtual IP)</option>
        <option value="internal" ${mode("internal")}>Cluster only</option>
        ${!editing || host ? `<option value="host" ${mode("host")}>The host's network (advanced)</option>` : ""}
        <option value="lan" ${mode("lan")}>Its own LAN address (bridged)</option></select></div>
        <div class="f"><label>${nodeAddressesOnly() ? `LAN address ${tip(NODE_ADDRESS_TIP)}` : "Virtual IP"}</label><select id="${prefix}_vip_mode"${change}>
          ${nodeAddressesOnly() ? nodeAddressOption() : `${nodeAddressChoice(cfg.vip_mode === "nodes" || (cfg.vip_mode === "shared" && !sharedVip))}
          ${nodeAddressBeside() && !sharedVip ? "" : `<option value="shared" ${vip("shared")}>Default virtual IP${sharedVip ? ` · ${esc(sharedVip)}` : " · set it up in Networking"}</option>`}
          <option value="auto" ${vip("auto")}>A new virtual IP, assigned automatically${vips.freeCount ? ` · ${vips.freeCount} free` : ""}</option>
          <option value="manual" ${vip("manual")}>Specific virtual IP</option>`}</select></div></div>
      ${editing && host ? '<p class="ui-help">It uses its host\'s network, set when it was deployed. Deploy it again to change that.</p>' : ""}
      <div class="f" id="${prefix}_vip_wrap"><label>Specific virtual IP</label>${vipPicker(prefix, cfg.lb_ip || "", vips, editing)}</div>
      ${lanHtml}
      ${UI.more("How addresses work", nodeAddressesOnly()
        ? "Each port is available on every host's own address, and each port can belong to only one service. Add kube-vip in Settings for separate addresses. The host's network ties the app to one host."
        : "The default virtual IP is one address shared by several apps on different ports. A new or specific virtual IP gives the app its own address. Its own LAN address adds a separate network interface. The host's network ties the app to one host.")}
      <div class="sec">Ports ${tip("The container port is where the app listens. The LAN port is what clients use. TCP and UDP on the same number are separate.")}</div>${portsHtml}`;
}
window.addressStepHtml = addressStepHtml;

function vipPicker(prefix, current, choices, keep = false) {
  // Editing a workload keeps the address it has, whatever else is said of
  // it: a change elsewhere in it is never refused for its address (#306).
  const kept = keep && current && choices.blocked?.[current];
  if (choices.blocked?.[current] && !kept) current = "";
  const own = choices.own || [], labels = choices.labels || {};
  const known = kept || choices.free.includes(current) || choices.used.some(v => v.ip === current) || own.some(v => v.ip === current);
  const typed = !!current && !known;
  const option = (value, label) => `<option value="${esc(value)}" ${value === current ? "selected" : ""}>${esc(label)}</option>`;
  return `<select id="${prefix}_lb_pick" onchange="vipPicked(${jsq(prefix)})">
      <option value="" ${!current ? "selected" : ""}>Choose an address…</option>
      ${kept ? option(current, `${current} · its address now`) : ""}
      ${own.some(v => v.free) ? `<optgroup label="Virtual IPs · free">${own.filter(v => v.free).map(v => option(v.ip, `${v.ip}${v.label ? ` · ${v.label}` : ""}`)).join("")}</optgroup>` : ""}
      ${choices.free.length ? `<optgroup label="Free in the IP pools (${choices.free.length})">${choices.free.slice(0, 60).map(ip => option(ip, ip)).join("")}</optgroup>` : ""}
      ${choices.used.length ? `<optgroup label="In use · shared with the apps already there">${choices.used.map(v =>
        option(v.ip, `${v.ip}${labels[v.ip] ? ` · ${labels[v.ip]}` : ""} · ${v.services} service${v.services === 1 ? "" : "s"} · ports ${v.listeners.map(l => l.port).slice(0, 5).join(", ")}`)).join("")}</optgroup>` : ""}
      <option value="__typed" ${typed ? "selected" : ""}>Enter an address…</option></select>
    <input id="${prefix}_lb_ip" class="mono" value="${esc(current || "")}" placeholder="192.0.2.250" data-ipam ${typed && !kept ? "" : 'style="display:none"'}>
    ${kept ? `<p class="ui-help">${esc(choices.blocked[current])}</p>` : ""}`;
}

window.vipPicked = prefix => {
  const pick = $(`#${prefix}_lb_pick`), input = $(`#${prefix}_lb_ip`);
  const typed = pick.value === "__typed";
  input.style.display = typed ? "" : "none";
  if (!typed) input.value = pick.value;
  else input.focus();
  input.dispatchEvent(new Event("input"));
};


// Node only (tests): the same names, for require(). The browser has no `module`.
if (typeof module === "object" && module.exports) module.exports = {
  vipChoices, addressStepHtml, vipPicker
};
