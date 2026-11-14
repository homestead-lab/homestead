/* Homestead views: moving and creating VMs, and a VM's own address (the page is views-vms.js). Split from views-lifecycle.js (#911); a classic script. */
/* ---------------- VMs: the page is views-vms.js; moving and creating stay here ---------------- */
window.vmMove = (ns, name) => {
  const nodes = STATE.data.nodes || [];
  modal("Move to another host · " + name, `
    <p class="muted small">KubeVirt moves the VM while it runs. Its memory is copied while it keeps running,
    so there is no downtime.</p>
    <div class="f" style="margin-top:14px"><label>Target host</label><select id="vm_target">
      <option value="">Let KubeVirt choose</option>
      ${nodes.map(n => `<option value="${esc(n.name)}">${esc(n.name)}</option>`).join("")}</select></div>
    ${UI.actions(`<button class="btn pri" onclick="doVmMove(${jsq(ns)},${jsq(name)})">Move to another host</button>
      <button data-dialog-dismiss="true" class="btn" onclick="closeModal()">Cancel</button>`)}`);
};
window.doVmMove = async (ns, name) => {
  try {
    const r = await api("/api/vm/migrate", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ ns, name, target: $("#vm_target").value || null }) });
    toast(`Move ${r.migration} started`, "ok"); closeModal(); setTimeout(() => refresh(true), 2000);
  } catch (e) { toast(e.message, "bad"); }
};
/* ---------------- a VM's own address ----------------
   A VM on the pod network is reached through a Service. One on a VM network
   bridged to the LAN is a machine there like any other, and can be given an
   address of its own: picked from what IP addresses knows is free outside
   the DHCP range, written into cloud-init's network config, and recorded
   under the VM's name so nothing else is given it. */
/* LAN networks a container can join; for a VM, only those on a bridge -
   macvlan cannot carry a VM's own MAC. */
function vmLanNetworks(opts, forVm = false) {
  // macvlan carries containers only, macvtap VMs only; a bridge both.
  return (opts.network_details || []).filter(n => n.lan && (forVm ? n.vms !== false : n.containers !== false));
}
function vmNetworkNote(opts, forVm = false) {
  if (vmLanNetworks(opts, forVm).length) return "";
  const other = forVm && vmLanNetworks(opts).length;
  return `<div class="note" style="margin-top:8px"><b>${other ? "There is no LAN network that a VM can join yet." : "There is no LAN network yet."}</b>
    ${other ? "The existing networks use macvlan, which carries only apps. A VM needs a macvtap network or a network on a host bridge."
      : `A LAN network puts ${opts.harvester ? "VMs and apps" : "VMs or apps"} on the LAN, either untagged like the hosts or on a VLAN.`}
    <button class="btn sm pri" data-need="admin" style="margin-top:6px" onclick="vmNetworkAdd()">＋ Create LAN network</button></div>`;
}

/* A VM network, made here: the same object Harvester's dashboard makes under
   Networks > VM Networks, so it shows there too. When made from a form that
   needed one, that form opens again with it to choose. */
window.vmNetworkAdd = async (reopen = null) => {
  const opts = window.__vmCreateOptions || await api("/api/vm/create-options").catch(e => ({ unread: e.message }));
  const back = reopen || window.__vmNetworkReopen || null;
  const o = opts.vm_network_options || {};
  const open = window.childModal && !$("#modal").classList.contains("hidden") ? childModal : modal;
  // Unread options are not "Multus is needed first" (#620).
  if (opts.unread) {
    open("New LAN network", UI.callout("bad", "The network options could not be read", esc(opts.unread)) + UI.actions(UI.cancel("Close")));
    return;
  }
  if (!o.harvester && !o.multus) {
    open("New LAN network", `<div class="note"><b>Multus is needed first.</b> An app or VM joins the LAN through a second network,
        which Kubernetes provides through Multus. k3s and RKE2 do not include it by default.</div>
      <p class="small">${esc(o.multus_help || "Install Multus, then return here.")}</p>
      ${UI.actions(`${STATE.platform?.helm_controller && ["k3s", "rke2"].includes(STATE.platform?.distribution)
        ? `<button class="btn pri" data-need="admin" onclick="closeModal(); addonInstall('multus')">Install Multus</button>` : ""}
        <button data-dialog-dismiss="true" class="btn" onclick="modalBack()">Close</button>`)}`);
    window.__vmNetworkReopen = back;
    return;
  }
  const clusters = o.cluster_networks?.length ? o.cluster_networks : ["mgmt"];
  const ifaces = o.interfaces || [];
  const ifaceWord = i => `${i.name} · ${i.kind === "bridge" ? "bridge · VMs and apps" : `${i.kind}${i.master ? ` in ${i.master}` : ""}`}${i.everywhere ? "" : ` · only on ${i.nodes.join(", ")}`}`;
  const carrier = o.harvester
    ? `<div class="f"><label>Cluster network ${tip("The Harvester cluster network that carries it. mgmt is the hosts' own network and the usual choice.")}</label>
        <select id="vn_cluster">${clusters.map(c => `<option ${c === "mgmt" ? "selected" : ""}>${esc(c)}</option>`).join("")}</select></div>`
    : `<div class="f"><label>Host interface ${tip("The network interface on each host that connects to the LAN. A bridge (br0) carries VMs and apps. On a plain network card (eth0), a network carries either VMs (macvtap) or apps (macvlan), not both. Every host needs an interface with the same name.")}</label>
        ${ifaces.length ? `<select id="vn_iface" onchange="vmNetworkForChanged()">${ifaces.map(i => `<option value="${esc(i.name)}" data-kind="${esc(i.kind)}">${esc(ifaceWord(i))}</option>`).join("")}</select>`
          : `<input id="vn_iface" class="mono" placeholder="eth0 or br0" oninput="vmNetworkForChanged()">`}</div>`;
  const forVms = back === "vm" || back === "k3s";
  const usedBy = o.harvester ? "" : `<div class="f" id="vn_for_row"><label>For ${tip("A plain network card can carry VMs through macvtap, or apps through macvlan, each with its own address on the LAN. Create one network of each to have both. Through macvtap, a VM cannot be reached from its own host, but other machines on the LAN can reach it.")}</label>
      <select id="vn_for" onchange="vmNetworkForChanged()"><option value="vms" ${forVms ? "selected" : ""}>Virtual machines (macvtap)</option>
        <option value="containers" ${forVms ? "" : "selected"}>Apps (macvlan)</option></select>
      <div id="vn_for_note"></div></div>`;
  open("New LAN network", `
    <p class="small" style="margin-top:0">VMs and apps on this network are on the LAN, with addresses from the router's DHCP or their own fixed addresses.</p>
    <div class="f2"><div class="f"><label>Name ${tip("The name shown wherever a network is chosen, such as lan or vlan20.")}</label><input id="vn_name" value="lan"></div>
      ${carrier}</div>
    ${!o.harvester && !ifaces.length ? '<div class="dim xs">The node probe has not reported the hosts\' interfaces, so enter the name. <span class="mono">ip link</span> on a host lists them.</div>' : ""}
    ${usedBy}
    <div class="f"><label>VLAN ${tip("Leave empty for untagged traffic on the same LAN as the hosts. Enter a number for that VLAN, which the switch must carry to the hosts.")}</label>
      <input id="vn_vlan" type="number" min="1" max="4094" placeholder="Empty for untagged, the hosts' own LAN"></div>
    ${UI.actions(`<button class="btn pri" onclick="vmNetworkAddGo()">Create network</button>
      <button data-dialog-dismiss="true" class="btn" onclick="modalBack()">Cancel</button>`)}`);
  window.__vmNetworkReopen = back;
  window.__vmNetworkOptions = o;
  vmNetworkForChanged();
};
/* A bridge carries both; on a NIC the choice is made, and macvtap must be there. */
window.vmNetworkForChanged = () => {
  const o = window.__vmNetworkOptions || {}, row = $("#vn_for_row");
  if (!row) return;
  const picked = $("#vn_iface")?.selectedOptions?.[0];
  const bridge = picked ? picked.dataset.kind === "bridge" : /^(br|vmbr|virbr)/.test($("#vn_iface")?.value || "");
  row.hidden = bridge;
  const vms = $("#vn_for").value === "vms";
  $("#vn_for_note").innerHTML = !bridge && vms && !o.macvtap
    ? `<div class="note small" style="margin-top:6px"><b>macvtap is not installed yet.</b> It is one of Homestead's required components on clusters that run VMs.
        <button class="btn sm pri" data-need="admin" onclick="baselineInstall()">Install components</button></div>` : "";
};
window.vmNetworkAddGo = async () => {
  try {
    const r = await api("/api/network/vm-networks", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name: $("#vn_name").value.trim(), cluster_network: $("#vn_cluster")?.value || "",
        interface: $("#vn_iface")?.value.trim() || "", vlan: $("#vn_vlan").value.trim(),
        for: $("#vn_for_row") && !$("#vn_for_row").hidden ? $("#vn_for").value : "" }) });
    toast(r.detail, "ok");
    window.__vmCreateOptions = null;
    const reopen = window.__vmNetworkReopen;
    window.__vmNetworkReopen = null;
    closeModal();
    if (reopen === "k3s" && window.k3sCluster) k3sCluster();
    else if (reopen === "vm") vmNew();
    else if (STATE.view === "network") viewNetworking();
  } catch (e) { toast(e.message, "bad"); }
};
function vmSubnetFor(opts, cidr) { return (opts.subnets || []).find(s => s.cidr === cidr); }
function vmAddressFields(p, opts, count = 1) {
  const subnets = opts.subnets || [];
  const first = subnets[0];
  return `<div class="f2">
      <div class="f"><label>Subnet ${tip("From Networking › IP addresses. The addresses offered are free, and outside the DHCP range and the virtual IP pools.")}</label>
        <select id="${p}_subnet" onchange="vmSubnetPicked(${jsq(p)}, ${count})">${subnets.map(s => `<option value="${esc(s.cidr)}">${esc(s.cidr)}${s.name ? ` · ${esc(s.name)}` : ""} · ${s.free.length} free</option>`).join("")}
          <option value="">Another subnet (enter it)</option></select></div>
      <div class="f"><label>${count > 1 ? "Addresses, one per host" : "Address"}</label>
        <input id="${p}_ip" class="mono" placeholder="${count > 1 ? "192.0.2.60, 192.0.2.61" : "192.0.2.60"}" list="${p}_free" data-ipam="${count > 1 ? "multi" : ""}">
        <datalist id="${p}_free"></datalist></div></div>
    <div class="f2">
      <div class="f"><label>Prefix length</label><input id="${p}_prefix" type="number" min="8" max="30" value="${first ? esc(first.cidr.split("/")[1]) : 24}"></div>
      <div class="f"><label>Gateway</label><input id="${p}_gw" class="mono" value="${esc(first?.gateway || "")}" placeholder="192.0.2.1"></div></div>
    <div class="f"><label>DNS servers ${tip("Separate addresses with commas. If left empty, the gateway is used for DNS.")}</label><input id="${p}_dns" class="mono" placeholder="${esc(first?.gateway || "192.0.2.1")}"></div>
    ${subnets.length ? "" : '<div class="dim xs">No subnet has been added yet. Add one under Networking › IP addresses to be offered free addresses and to have the chosen addresses recorded.</div>'}`;
}
window.vmSubnetPicked = (p, count = 1) => {
  const opts = window.__vmCreateOptions || {};
  const subnet = vmSubnetFor(opts, $(`#${p}_subnet`)?.value);
  $(`#${p}_free`).innerHTML = (subnet?.free || []).map(ip => `<option value="${esc(ip)}">`).join("");
  if (!subnet) return;
  $(`#${p}_prefix`).value = subnet.cidr.split("/")[1];
  $(`#${p}_gw`).value = subnet.gateway || "";
  $(`#${p}_ip`).value = (subnet.free || []).slice(0, count).join(", ");
};
function vmReadAddress(p) {
  return { prefix: +$(`#${p}_prefix`).value || 24, gateway: $(`#${p}_gw`).value.trim(),
    dns: $(`#${p}_dns`).value.split(",").map(x => x.trim()).filter(Boolean) };
}
window.vmNetChanged = () => {
  const isolated = !!$("#v_isolated")?.checked;
  $$("#v_network_controls input, #v_network_controls select, #v_network_controls button").forEach(el => {
    if (isolated && !el.disabled) { el.dataset.isolationDisabled = "true"; el.disabled = true; }
    else if (!isolated && el.dataset.isolationDisabled) { el.disabled = false; delete el.dataset.isolationDisabled; }
  });
  const net = $("#v_net")?.value || "pod";
  const lan = vmLanNetworks(window.__vmCreateOptions || {}, true).some(n => n.name === net);
  $("#v_addr_wrap").hidden = !lan;
  $("#v_static").hidden = !lan || $("#v_addr_mode").value !== "static";
  if ($("#v_service_wrap")) $("#v_service_wrap").hidden = net !== "pod";
  if ($("#v_service_pick")) $("#v_service_pick").hidden = $("#v_service").value !== "manual";
};

window.vmNew = async (selectedDisk = "", selectedNamespace = "") => {
  // A form built on invented options would offer classes, images and
  // addresses that may not be there (#620). A read that only hides some
  // choices (other namespaces, imported disks when none was chosen) is named
  // on the form instead.
  const missed = [];
  const partly = (path, what, fallback) => api(path).catch(e => { missed.push(`${what[0].toUpperCase()}${what.slice(1)} could not be read. ${e.message}`); return fallback; });
  let opts, disks, vipOptions, devices, namespaces;
  try {
    let network;
    [opts, disks, devices, namespaces, network] = await Promise.all([
      mustRead("/api/vm/create-options", "what a new VM can use"),
      selectedDisk ? mustRead("/api/vm-disks", "the imported disks") : partly("/api/vm-disks", "the imported disks", []),
      api("/api/passthrough/resources").catch(error => ({ resources: [], error: error.message })),
      partly("/api/namespaces", "the other namespaces", []),
      mustRead("/api/network", "the network"),
    ]);
    vipOptions = await vipChoices(network);
  } catch (e) {
    modal("New virtual machine", UI.error(e, { retry: `vmNew(${jsArg(selectedDisk)},${jsArg(selectedNamespace)})`, dialog: true }));
    return;
  }
  window.__vmNetworkReopen = "vm";
  // Imports are CDI DataVolumes, so without CDI there are none to attach.
  const readyDisks = opts.cdi ? disks.filter(d => d.phase === "Succeeded" && !d.in_use) : [];
  const selected = selectedDisk ? `disk:${selectedNamespace || defaultNamespace()}/${selectedDisk}` : "";
  const images = (opts.images || []).filter(i => i.storage_class);
  const facts = opts.storage_class_facts || {};
  const classLabel = c => `${c}${facts[c]?.default ? " (default)" : ""}${facts[c]?.replicas ? ` · ${facts[c].replicas} copies` : ""}`;
  window.__vmCreateOptions = opts;
  const vmMachine = `
    <div class="f2">
      <div class="f"><label>Name</label><input type="text" id="v_name" placeholder="ubuntu-test"></div>
      <div class="f"><label for="v_ns">Namespace ${tip("The namespace for the VM, its disk and its cloud-init Secret. An imported disk sets its own namespace. ISOs are in " + defaultNamespace() + ".")}</label>
        <select id="v_ns" onchange="vmNamespaceChanged()">${[...new Set([defaultNamespace(), ...namespaces])].map(n => `<option ${n === defaultNamespace() ? "selected" : ""}>${esc(n)}</option>`).join("")}</select>
        <div class="dim xs" id="v_ns_note" style="margin-top:4px" hidden></div></div>
    </div>
    <div class="f2">
      <div class="f"><label>vCPUs</label><input type="number" id="v_cores" value="2" min="1" max="16"></div>
      <div class="f"><label>Memory</label><input type="text" id="v_mem" value="2Gi"></div>
    </div>
    <div class="f2">
      <div class="f"><label>Disk (GiB)</label><input type="number" id="v_disk" value="20" min="5"></div>
      <div class="f"><label>Root password ${tip("Required when Homestead creates a new disk. Optional for an imported disk that already has sign-in set up.")}</label><input type="password" id="v_pass" autocomplete="new-password" placeholder="Set an initial password"></div>
    </div>
`;
  const vmDisk = `
    <div class="f"><label>Boot disk ${tip(opts.harvester
      ? "Attach a completed import as it is, start from one of Harvester's images, or download an image while the VM is created."
      : "Attach a completed import as it is, or download an image while the VM is created.")}</label>
      <select id="v_boot" onchange="vmBootChanged()">
        <option value="">Blank disk</option>
        ${readyDisks.map(d => `<option value="disk:${esc(d.namespace)}/${esc(d.name)}" ${selected === `disk:${d.namespace}/${d.name}` ? "selected" : ""}>Imported · ${esc(d.namespace)}/${esc(d.name)} (${esc(d.capacity || "size unknown")})</option>`).join("")}
        ${images.map(i => `<option value="image:${esc(i.namespace)}/${esc(i.name)}" data-size="${i.size_gb}" ${window.__vmPreset === `image:${i.namespace}/${i.name}` ? "selected" : ""}>Harvester image · ${esc(i.display)} (${i.size_gb}G)</option>`).join("")}
        ${(opts.store || []).length ? `<optgroup label="Image store · the publisher's newest build">${opts.store.map(s => `<option value="store:${esc(s.id)}" data-size="${s.min_gb}"
          ${window.__vmPreset === `store:${s.id}` ? "selected" : ""}>${esc(s.name)} · ${esc(s.variant)}${opts.harvester ? (s.kept ? (s.ready ? " · kept" : " · downloading") : " · downloads now") : ""}</option>`).join("")}</optgroup>` : ""}
        ${opts.cdi || opts.harvester ? `<option value="url">${opts.harvester ? "Download from a URL (as a Harvester image)" : "Download from HTTP(S) URL"}</option>` : ""}
        ${(opts.isos || []).length ? `<optgroup label="Install from an ISO · a blank disk, with the ISO in its CD-ROM drive">${opts.isos.map(i => `<option value="iso:${esc(i.name)}">${esc(i.file)}</option>`).join("")}</optgroup>` : ""}
      </select>
      <div class="dim xs" style="margin-top:6px">To install from an ISO, prepare it in the <a class="linkish" onclick="vmIsoLibrary()">ISO library</a> first.</div></div>
    <div class="f" id="v_drivers_row" hidden><label>Drivers CD ${tip("A second CD-ROM drive for the installer's drivers. Windows setup finds no disk until it loads the VirtIO storage driver. Attach virtio-win.iso here, then in setup choose Load driver, and vioscsi or viostor for the Windows version.")}</label>
      <select id="v_drivers"><option value="">None</option>${(opts.isos || []).map(i => `<option value="${esc(i.name)}" ${/virtio/i.test(i.file) ? "selected" : ""}>${esc(i.file)}</option>`).join("")}</select>
      <div class="dim xs" style="margin-top:4px">${(opts.isos || []).some(i => /virtio/i.test(i.file)) ? "" : "For Windows, prepare virtio-win.iso (from the Fedora project) in the ISO library."}</div></div>
    <div class="f" id="v_url_row" hidden><label>Image URL</label><input type="url" id="v_url" placeholder="https://cloud-images.ubuntu.com/…/img"></div>
`;
  // A step of its own, as Edit has a Hardware tab: the guest type first, then
  // the fields it fills in.
  const vmHardware = `
    <div class="f"><label>Guest type ${tip("Sets the firmware, TPM, clock and devices for the kind of guest, like the OS type in Proxmox. They can be changed below, or later in Edit.")}</label>
      <select id="v_preset" onchange="vmCreatePreset(this.value)">
        <option value="">Default</option>${Object.entries(VM_PRESETS).map(([id, p]) => `<option value="${esc(id)}">${esc(p.name)}</option>`).join("")}</select>
      <div class="dim xs" id="v_preset_about" style="margin-top:4px">KubeVirt defaults: BIOS, UTC, a display and a serial console</div></div>
    ${opts.hardware_base ? vmHardwareFields(opts.hardware_base, opts, false, true, true) : ""}
`;
  const vmNetwork = `
    <label class="check"><input id="v_isolated" type="checkbox" onchange="vmNetChanged()"> Isolated VM</label>
    <p class="dim small">The VM has no virtual network cards, not even the automatic one on the cluster network. Clear this option before adding a card. A physical network device can still be passed through over PCI or USB.</p>
    <div id="v_network_controls">
    <div class="f"><label>Network ${tip("On the cluster network, the VM is reached through a service, like an app. On a LAN network, it is a machine on the LAN like any other, with an address from DHCP or its own fixed address.")}</label>
      <select id="v_net" onchange="vmNetChanged()"><option value="pod">Cluster network · reached through a service</option>
        ${(opts.network_details || []).filter(n => n.vms !== false).map(n => `<option value="${esc(n.name)}">${esc(n.name)}${n.lan ? ` · LAN${n.vlan ? ` (VLAN ${esc(n.vlan)})` : ""}` : ""}</option>`).join("")}</select>
      ${vmNetworkNote(opts, true)}</div>
    <div class="f"><label>Network card ${tip("The virtual network card the guest sees. VirtIO is the fastest but needs its driver. Linux includes it, and Windows needs the guest tools from virtio-win.iso. An Intel e1000e works in Windows without extra drivers, so the Windows guest types choose it.")}</label>
      <select id="v_nic_model"><option value="virtio">VirtIO</option><option value="e1000e">Intel e1000e</option><option value="e1000">Intel e1000</option><option value="rtl8139">Realtek RTL8139</option></select></div>
    <div id="v_service_wrap"><div class="f"><label>Virtual IP</label><select id="v_service" onchange="vmNetChanged()"><option value="">Cluster only, or set up later</option>${nodeAddressesOnly() ? "" : `${vipOptions.shared ? `<option value="shared">Default virtual IP · ${esc(vipOptions.shared)}</option>` : ""}<option value="manual">Specific virtual IP</option>`}</select></div>
      ${nodeAddressesOnly() ? '<p class="note small">ServiceLB uses the hosts\' own addresses, not virtual IPs that can move. Install kube-vip or MetalLB before choosing a virtual IP.</p>' : ""}
      <div id="v_service_pick" class="f" hidden><label>Specific virtual IP</label>${vipPicker("vsvc", "", vipOptions)}</div>
      <p class="dim small">The VM is created first. Its virtual IP and ports are then reviewed before they are made available. If that step is cancelled or fails, the VM is kept and its network can be set up under Edit → Network.</p></div>
    <div id="v_addr_wrap" hidden>
      <div class="f"><label>Address</label><select id="v_addr_mode" onchange="vmNetChanged();vmSubnetPicked('v')">
        <option value="dhcp">From the network's DHCP</option><option value="static">A fixed address</option></select></div>
      <div id="v_static" hidden>${vmAddressFields("v", opts)}</div></div>
    </div>
`;
  const vmStorage = `
    ${(opts.storage_classes || []).length ? `<div class="f" id="v_sc_row"><label>Storage class ${tip(opts.harvester
      ? "The storage class for a blank or downloaded disk. A disk from a Harvester image always uses that image's storage class."
      : "The storage class for the disk. The cluster's default class is selected.")}</label>
      <select id="v_sc">${opts.storage_classes.map(c => `<option value="${esc(c)}" ${c === opts.default_class ? "selected" : ""}>${esc(classLabel(c))}</option>`).join("")}</select></div>` : ""}
    ${UI.more("How the disk is created", `<p>${opts.harvester
      ? "New disks are created the way Harvester creates them, as shared block volumes, so the VM can move between hosts. A URL is downloaded as a Harvester image and kept in its image list for the next VM."
      : opts.cdi ? `New disks are CDI DataVolumes in the storage class above, with the access mode that class supports.
        A VM on a disk that only one host can use stays on that host.`
      : `<b>CDI is not installed</b>, so a VM on this cluster starts from a blank disk that KubeVirt formats.
        To download or import disk images, install CDI (the containerized data importer) from kubevirt.io.`}
    ${readyDisks.length ? " Imported disks are attached directly and remain visible on the Import page." : ""}</p>`)}`;
  modal("New virtual machine", UI.notices(missed.map(text => ["warn", text]), "Parts of this form could not be read") + stepper("v_steps", [
    // The Edit dialog's tabs, in its order: what is set here is changed there.
    { title: "General", html: vmMachine }, { title: "Hardware", html: vmHardware },
    ...(opts.hardware_base ? [{ title: "Display and devices", html: vmDisplayFields(opts.hardware_base) }] : []),
    { title: "Disks", html: vmDisk + vmStorage }, { title: "Network", html: vmNetwork },
    { title: "Passthrough", html: window.vmDevicesPane ? vmDevicesPane({}, devices) : "" }],
    `<button class="btn pri" onclick="doVmCreate()">Review VM</button>`), true, "vm-config");
  window.__vmPreset = "";
  vmBootChanged();
  vmHardwareChanged();
};
window.vmBootChanged = () => {
  const boot = $("#v_boot")?.value || "";
  // An installer asks for its own password; cloud-init is for cloud images.
  if ($("#v_pass")) $("#v_pass").placeholder = boot.startsWith("iso:") ? "Not used. The installer asks for it." : "Set an initial password";
  if ($("#v_url_row")) $("#v_url_row").hidden = boot !== "url";
  if ($("#v_drivers_row")) $("#v_drivers_row").hidden = !boot.startsWith("iso:");
  // An import brings its own disk; a Harvester image brings its own class.
  if ($("#v_sc_row")) $("#v_sc_row").hidden = boot.startsWith("disk:") || boot.startsWith("image:");
  // An imported disk can only be attached in its own namespace.
  const ns = $("#v_ns");
  if (ns) {
    // It comes back to the person's own choice when the disk is changed again.
    if (boot.startsWith("disk:")) {
      if (!ns.disabled) ns.dataset.chosen = ns.value;
      ns.value = boot.slice(5).split("/")[0];
    } else if (ns.disabled && ns.dataset.chosen) ns.value = ns.dataset.chosen;
    ns.disabled = boot.startsWith("disk:");
    if ($("#v_ns_note")) { $("#v_ns_note").hidden = !ns.disabled; $("#v_ns_note").textContent = "Set by the imported disk"; }
  }
  const size = +($("#v_boot")?.selectedOptions[0]?.dataset.size || 0);
  if (size && +$("#v_disk").value < Math.ceil(size)) $("#v_disk").value = Math.ceil(size);
};
/* The ISO library is in the default namespace, and an ISO's volume can't be
   mounted from another one: elsewhere the ISO choices are switched off. */
window.vmNamespaceChanged = () => {
  const outside = ($("#v_ns")?.value || defaultNamespace()) !== defaultNamespace();
  $$("#v_boot option[value^='iso:']").forEach(option => { option.disabled = outside; });
  if (outside && ($("#v_boot")?.value || "").startsWith("iso:")) { $("#v_boot").value = ""; vmBootChanged(); }
};
window.doVmCreate = async () => {
  if (VM_CREATE.busy) return;
  const boot = $("#v_boot").value;
  const imported = boot.startsWith("disk:") ? boot.slice(5).split("/") : [];
  const body = { name: $("#v_name").value.trim(), cores: +$("#v_cores").value,
    memory: $("#v_mem").value.trim(), disk_gb: +$("#v_disk").value, password: $("#v_pass").value,
    namespace: imported[0] || $("#v_ns")?.value || defaultNamespace(), disk_import: imported[1] || "",
    image_id: boot.startsWith("image:") ? boot.slice(6) : "",
    image_url: boot === "url" ? $("#v_url").value.trim() : "",
    store_id: boot.startsWith("store:") ? boot.slice(6) : "",
    install_iso: boot.startsWith("iso:") ? boot.slice(4) : "",
    drivers_iso: boot.startsWith("iso:") ? $("#v_drivers")?.value || "" : "",
    storage_class: $("#v_sc")?.value || "", network: $("#v_net")?.value || "pod",
    nic_model: $("#v_nic_model")?.value || "virtio", isolated: !!$("#v_isolated")?.checked };
  if (body.isolated) { delete body.network; delete body.nic_model; }
  if (!body.isolated && body.network !== "pod" && $("#v_addr_mode")?.value === "static") {
    body.static_ip = Object.assign(vmReadAddress("v"), { address: $("#v_ip").value.trim() });
    if (!body.static_ip.address) return toast("Enter the VM's address", "bad");
  }
  if (!body.name) return toast("Name is required", "bad");
  if (body.install_iso && body.namespace !== defaultNamespace())
    return toast(`ISOs are in ${defaultNamespace()}. Choose that namespace to install from an ISO.`, "bad");
  const serviceMode = body.network === "pod" ? $("#v_service")?.value || "" : "";
  const selectedVip = serviceMode === "manual" ? $("#vsvc_lb_ip").value.trim() : "";
  if (serviceMode === "manual" && !selectedVip) return toast("Choose the VM's virtual IP", "bad");
  // Only what differs from a plain VM: the guest type's settings and any changed by hand.
  const base = window.__vmCreateOptions?.hardware_base;
  const hardware = base && $("#vh") ? vmHardwareChanges(base) : vmPresetSettings($("#v_preset")?.value || "");
  if (hardware) body.hardware = hardware;
  try {
    const devices = window.vmDevicesChanges ? await vmDevicesChanges() : null;
    if (devices) body.host_devices = devices;
  } catch (error) { return toast(error.message, "bad"); }
  if (body.install_iso) body.password = "";
  if (!body.disk_import && !body.install_iso && body.password.length < 10) return toast("Root password must be at least 10 characters", "bad");
  if (boot === "url" && !body.image_url) return toast("Image URL is required", "bad");
  if (boot === "url" && !/^https?:\/\/[^/\s]+/i.test(body.image_url)) {
    // A file name here is usually a Harvester image, which is picked from the list instead.
    const image = (window.__vmCreateOptions?.images || []).find(i => i.display === body.image_url || i.name === body.image_url);
    return toast(image ? `${image.display} is a Harvester image. Choose it under Boot disk instead of entering a URL.`
      : "The image URL must start with http:// or https://", "bad");
  }
  return vmCreateReview(body, {serviceMode, selectedVip});
};
/* Review, then create a VM (#909), on the shared review flow: it keeps the sequence, the busy flag and the
   one-shot send. This view keeps its words and the Service virtual IP set up after the create. */
const VM_CREATE = UI.reviewFlow({
  preview: async ({ frozen, network }) => {
    const review = await api("/api/vm/create/preview", {method:"POST", headers:{"Content-Type":"application/json"},body:JSON.stringify(frozen)});
    if (!(review.config?.isolated === true || review.config?.mac) || !review.capacity_token || typeof review.capacity?.blocked !== "boolean")
      throw new Error("The VM review is not available. Nothing was created. Refresh before continuing.");
    return {...review, network:{...network}};
  },
  render: review => {
    const cfg = review.config;
    const ip = cfg.static_ip?.address ? ` · ${esc(cfg.static_ip.address)}` : "";
    const objects = UI.facts([
      ["VM", `${esc(cfg.cores || 2)} vCPUs · ${esc(cfg.memory || "2Gi")} memory · ${cfg.start === false ? "created stopped" : "starts when its disks are ready"}`],
      ["Network", cfg.isolated ? "Isolated, with no virtual network cards" : `${esc(cfg.network || "pod")} · MAC <span class="mono">${esc(cfg.mac)}</span>${ip}`],
      ["Disks", review.volumes?.length ? review.volumes.map(v => `<span class="mono">${esc(v.name)}</span> · ${esc(v.size)} · ${esc(v.storage_class)}`).join("<br>") : "none"],
      cfg.host_devices?.add?.length ? ["Passthrough", cfg.host_devices.add.map(d => `${esc(d.name)} · ${esc(d.resource)}${cfg.host_devices.roms?.[d.name] ? " · supplied vBIOS" : ""}`).join("<br>")] : null]);
    const details = `${review.volumes?.length ? `<ul class="ui-list">${review.volumes.map(v => `<li><span class="mono">${esc(v.name)}</span>: ${esc(v.access_mode)} · ${esc(v.volume_mode)}</li>`).join("")}</ul>` : ""}
      <p class="ui-help">The guest sign-in settings are part of this review but are not shown.</p>`;
    childModal(`Create VM · ${cfg.name}`, startReview(review.capacity, { what: "Creates", kind: "VM", name: cfg.name, where: cfg.namespace || "", objects, details,
        ackId: "vmCreateApprove", onAck: "vmCreateReviewReady()",
        actions: { dismiss: { label: "Back to configuration", onclick: "vmCreateReviewBack()" },
          primary: { label: "Create VM", onclick: "vmCreateReviewedApply()", id: "vmCreateApply", disabled: true } } }), true);
    vmCreateReviewReady();
  },
  failed: error => toast(error.message,"bad"),
  // Nothing needing attention: no tickbox to tick, only the button.
  blocked: review => review.capacity.blocked, ack: "vmCreateApprove", ackWhen: review => startReviewAsks(review.capacity), button: "vmCreateApply",
  apply: async review => {
    const cfg = review.config, button = $("#vmCreateApply");
    button.disabled = true; button.textContent = "Preparing the disk and creating the VM…";
    try {
      const result = await api("/api/vm/create", {method:"POST",headers:{"Content-Type":"application/json"},
        body:JSON.stringify({...cfg, capacity_token:review.capacity_token,confirm_capacity:true})});
      if (result.operation && window.noteOperation) noteOperation(result.operation);
      toast(result.warning || `${cfg.name} created${result.address ? ` at ${result.address}` : ""}`, result.warning ? "bad" : "ok");
      closeModal(); go("vms");
      if (!cfg.isolated && review.network.serviceMode) {
        try { await networkExpose(cfg.namespace,cfg.name,"VirtualMachine",review.network.selectedVip); }
        catch(error) { toast(`The VM was created, but its virtual IP could not be set up. ${String(error.message).replace(/\.$/, "")}. Use Edit → Network to set it up.`,"bad"); }
      }
    } catch(error) {
      if($("#vmCreateApply") === button) {
        button.textContent = "Check before creating again";
        $("#mbody").insertAdjacentHTML("afterbegin",`${UI.callout("bad", "", `${esc(String(error.message).replace(/\.$/, ""))}. The request was not sent again. Images, disks, sign-in settings or the VM itself may already exist. Check Virtual machines and Volumes before starting a new review. Disks that were kept are not deleted automatically.`)}`);
      }
      toast(error.message,"bad");
    }
  },
  after: () => { if (window.refreshOperations) refreshOperations(true); },
});
window.vmCreateReview = (body, network = {}) => VM_CREATE.open({ frozen: JSON.parse(JSON.stringify(body)), network });
window.vmCreateReviewBack = () => { if (VM_CREATE.back()) modalBack(); };
window.vmCreateReviewReady = () => VM_CREATE.ready();
window.vmCreateReviewedApply = async () => {
  if (!vmCreateReviewReady()) return toast("First review the VM and accept its warnings", "bad");
  await VM_CREATE.apply();
};

// Node only (tests): the same names, for require(). The browser has no `module`.
if (typeof module === "object" && module.exports) module.exports = {
  vmLanNetworks, vmNetworkNote, vmSubnetFor, vmAddressFields, vmReadAddress, VM_CREATE
};
