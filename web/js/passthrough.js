/* A host's PCI and USB devices given to virtual machines, and a GPU's ROM.

   homestead_passthrough.py does the work: on Harvester through its own
   device claims; on k3s and RKE2 by switching IOMMU on (from the next
   restart), handing a PCI device and its IOMMU group to vfio-pci, and
   listing it with KubeVirt. A VM then asks for the device by name on its
   Passthrough tab, where a GPU can be given its ROM (vBIOS) too. */

const PT = { facts: {} };

/* The node page's card: looking at the host's devices runs a helper there,
   so it waits to be asked. */
window.nodeDevicesPaint = node => {
  const card = $("#nodeDevices");
  if (!card) return;
  card.querySelector(".pt-body").innerHTML = `${UI.lead("PCI devices - a GPU, a NIC, an HBA - and USB devices this host can give to its VMs.")}
    ${UI.actions(UI.button("Look at its devices", `nodeDevicesLook(${jsArg(node)})`, { attrs: 'data-need="admin"' }))}`;
  if (window.applyRole) applyRole();
};

window.nodeDevicesLook = async node => {
  const body = $("#nodeDevices .pt-body");
  if (!body) return;
  body.innerHTML = '<div class="dim small"><span class="spin2"></span> looking at the host</div>';
  let f;
  try { f = await api("/api/passthrough/inspect", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ node }) }); }
  catch (e) { body.innerHTML = UI.callout("bad", "The host's devices could not be read.", esc(e.message)); return; }
  PT.facts[node] = f;
  const offered = (f.pci || []).filter(r => r.offered), other = (f.pci || []).filter(r => !r.offered);
  const state = r => r.vfio ? UI.chip("for VMs", "ok") : r.listed ? UI.chip("for VMs after a restart", "info") : `<span class="dim xs">${esc(r.driver || "no driver")}</span>`;
  const action = r => {
    if (r.vfio || r.listed) return UI.button("Give back", `ptPci(${jsArg(node)},${jsArg(r.address)},false)`, { attrs: 'data-need="admin"' });
    const why = !f.iommu ? "IOMMU is off" : (r.problems || []).join("; ");
    return why ? `<span class="dim xs">${esc(why)}</span>`
      : UI.button("Give to VMs", `ptPci(${jsArg(node)},${jsArg(r.address)},true)`, { attrs: 'data-need="admin"' });
  };
  const row = r => [`<b>${esc(r.name)}</b><div class="dim xs mono">${esc(r.address)}${r.class_name ? ` · ${esc(r.class_name)}` : ""}${r.boot_vga ? " · boot display" : ""}</div>`,
    r.group ? `${esc(r.group)}${(r.group_members || []).length ? ` <span class="dim xs">+${r.group_members.length}</span>` : ""}` : "—", state(r), action(r)];
  const usb = (f.usb || []).map(u => [`<b>${esc(u.name)}</b><div class="dim xs mono">${esc(u.vendor)}:${esc(u.product)}${u.port ? ` · ${esc(u.port)}` : ""}</div>`,
    u.permitted ? UI.chip("offered to VMs", "ok") : '<span class="dim xs">host</span>',
    UI.button(u.permitted ? "Stop offering" : "Offer to VMs",
      `ptUsb(${jsArg(node)},${jsArg(u.vendor)},${jsArg(u.product)},${!u.permitted},${jsArg(u.harvester_name || "")})`, { attrs: 'data-need="admin"' })]);
  body.innerHTML = `
    ${f.harvester ? UI.lead("Harvester hands devices over with its own claims; this is the same as its Devices page.")
      : f.iommu ? `<div class="small">IOMMU ${UI.chip("on", "ok")} <span class="dim xs">${f.cmdline_iommu ? "" : "(by default)"}</span></div>`
      : UI.callout("warn", "IOMMU is off on this host.", `A device can only be handed to a VM with it on. Homestead can add it to the kernel command line;
          it is on from the next restart (Host actions), with VT-d or AMD-Vi enabled in the firmware.
          ${UI.actions(UI.button("Switch IOMMU on", `ptIommu(${jsArg(node)})`, { kind: "pri", attrs: 'data-need="admin"' }))}`)}
    ${UI.section("PCI", offered.length ? UI.table([{ label: "Device" }, { label: "IOMMU group" }, { label: "Now" }, { label: "" }], offered.map(row))
      : UI.lead("No PCI device here is one to give a VM."))}
    ${other.length ? UI.more(`${other.length} more - bridges and the chipset's own`, UI.table([{ label: "Device" }, { label: "IOMMU group" }, { label: "Now" }, { label: "" }],
      other.map(r => row(r).slice(0, 3).concat([""])))) : ""}
    ${UI.section("USB", usb.length ? UI.table([{ label: "Device" }, { label: "Now" }, { label: "" }], usb) : UI.lead("No USB devices."))}
    <p class="dim xs">A device given to VMs is no longer the host's: a GPU's console goes dark, a NIC's link drops. Every device in its IOMMU group
      goes with it. A VM using one runs only on this host and cannot live-migrate. ${f.harvester ? "" : "USB devices are offered by vendor and product, on any host that has one."}</p>`;
  if (window.applyRole) applyRole();
};

window.ptPci = async (node, address, give) => {
  const r = (PT.facts[node]?.pci || []).find(x => x.address === address) || {};
  const others = (r.group_members || []).filter(a => !(PT.facts[node].pci.find(x => x.address === a)?.class || "").startsWith("0604"));
  if (give && !(await ask(`Give ${r.name || address} on ${node} to VMs? The host lets go of it now and at every boot${r.boot_vga ? ", and this is its boot display: the local console goes dark" : ""}.${others.length ? ` ${others.join(", ")}, in the same IOMMU group, goes with it.` : ""}`,
    { title: "Give a device to VMs", ok: "Give to VMs" }))) return;
  if (!give && !(await ask(`Give ${r.name || address} back to ${node}? A VM using it loses it at its next start.`, { title: "Give a device back", ok: "Give back" }))) return;
  try {
    const res = await api("/api/passthrough/pci", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ node, address, give }) });
    toast(res.detail, "ok");
  } catch (e) { toast(e.message, "bad"); }
  nodeDevicesLook(node);
};
window.ptUsb = async (node, vendor, product, allow, harvesterName) => {
  try {
    const res = await api("/api/passthrough/usb", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ node, vendor, product, allow, harvester_name: harvesterName }) });
    toast(res.detail, "ok");
  } catch (e) { toast(e.message, "bad"); }
  nodeDevicesLook(node);
};
window.ptIommu = async node => {
  if (!(await ask(`Switch IOMMU on for ${node}? It goes in the kernel command line (a copy of /etc/default/grub is kept) and is on from the host's next restart.`,
    { title: "Switch IOMMU on", ok: "Switch it on" }))) return;
  try {
    const res = await api("/api/passthrough/iommu", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ node }) });
    toast(res.detail, "ok");
  } catch (e) { toast(e.message, "bad"); }
  nodeDevicesLook(node);
};

/* ---------- a VM's devices ---------- */
function vmDevicesPane(v, res) {
  const have = v.host_devices || [], list = (res && res.resources) || [];
  window.__vmDeviceResources = list;
  window.__vmDeviceNames = have.map(d => d.name);
  const nodesUrl = window.HomesteadRouter?.urlFor("nodes") || "/nodes";
  const label = r => `${r.kind === "usb" ? "USB" : "PCI"} ${r.label || r.resource}${r.nodes?.length ? ` · ${r.nodes.join(", ")}` : " · unavailable"}`;
  return `${have.length ? `<div class="tblwrap"><table class="tbl dense stack ve-table"><thead><tr><th>Device</th><th>ROM (vBIOS)</th><th></th></tr></thead><tbody>
      ${have.map(d => `<tr data-hostdev="${esc(d.name)}" data-resource="${esc(d.resource)}"><td data-label="Device"><label for="pd_device_${esc(d.name)}">${esc(d.name)}</label><select id="pd_device_${esc(d.name)}" class="pd_resource" aria-label="Device for ${esc(d.name)}">
        ${!list.some(r => r.resource === d.resource) ? `<option value="${esc(d.resource)}">${esc(d.resource)} · unavailable</option>` : ""}${list.map(r => `<option value="${esc(r.resource)}" ${r.resource === d.resource ? "selected" : ""}>${esc(label(r))}</option>`).join("")}</select></td>
        <td data-label="ROM (vBIOS)">${d.rom ? `${UI.chip("its own ROM", "info")} <label class="check xs"><input type="checkbox" class="pd_clear"> clear</label>` : ""}
          <input type="file" class="pd_rom" aria-label="vBIOS file for ${esc(d.name)}" accept=".rom,.bin,application/octet-stream"></td>
        <td><label class="check xs"><input type="checkbox" class="pd_rm"> remove</label></td></tr>`).join("")}</tbody></table></div>`
      : UI.lead("Select a PCI or USB device offered by the cluster's hosts.")}
    <div id="pd_adds"></div>
    ${res?.error ? UI.callout("bad", "Devices could not be loaded.", esc(res.error)) : ""}
    ${list.length ? `<div class="f" style="margin-top:10px"><label for="pd_pick">Add PCI or USB device</label><select id="pd_pick">${list.map(r => `<option value="${esc(r.resource)}">${esc(label(r))}</option>`).join("")}</select></div>
      ${UI.actions(UI.button("Add device", "vmAddHostDevice()"))}`
      : '<p class="dim small">No host devices are offered to VMs. Prepare a device on its host first.</p>'}
    <p class="dim small">Passthrough restricts this VM to hosts that offer its devices and prevents live migration. Changes apply at its next start.</p>
    ${UI.more("IOMMU and vBIOS setup", `<p>Open <a href="${esc(nodesUrl)}" target="_blank" rel="noopener">Nodes</a> in a new tab, select a host, then Hardware → Devices for VMs. Check IOMMU and offer its PCI or USB device there. Enabling IOMMU may require a host reboot.</p><p>A GPU can use its default ROM or a vBIOS file you supply. Files must start with 55 AA and be at most 640 KiB. Homestead enables the KubeVirt hook sidecar when a ROM is supplied.</p>`)}`;
}
window.vmDevicesPane = vmDevicesPane;
window.vmAddHostDevice = () => {
  const pick = $("#pd_pick");
  if (!pick) return;
  const names = new Set([...(window.__vmDeviceNames || []), ...$$("#pd_adds .pd-add").map(row => row.dataset.hostdev)]);
  let index = 0; while (names.has(`hostdev-${index}`)) index++;
  const name = `hostdev-${index}`;
  const resource = (window.__vmDeviceResources || []).find(r => r.resource === pick.value);
  $("#pd_adds").insertAdjacentHTML("beforeend", `<div class="pd-add reviewbox" data-hostdev="${name}" data-resource="${esc(pick.value)}">
    <div class="between"><b>${esc(resource?.label || pick.value)}</b><button type="button" class="btn sm" onclick="this.closest('.pd-add').remove()" aria-label="Remove ${name}">Remove</button></div>
    <p class="dim xs">${esc(name)} · ${esc((resource?.nodes || []).join(", ") || "No host currently offers this device")}</p>
    ${resource?.kind !== "usb" ? `<div class="f"><label for="pd_rom_${name}">vBIOS file <span class="dim">optional</span></label><input id="pd_rom_${name}" type="file" class="pd_rom" accept=".rom,.bin,application/octet-stream"></div>` : ""}</div>`);
};
async function readRom(file) {
  const buffer = await file.arrayBuffer();
  let text = "";
  const bytes = new Uint8Array(buffer);
  if (!bytes.length || bytes.length > 640 * 1024) throw new Error("vBIOS files must be at most 640 KiB");
  if (bytes[0] !== 0x55 || bytes[1] !== 0xaa) throw new Error("The vBIOS file must start with 55 AA; remove any dump header first");
  for (let i = 0; i < bytes.length; i += 0x8000) text += String.fromCharCode.apply(null, bytes.subarray(i, i + 0x8000));
  return btoa(text);
}
window.vmReadRom = readRom;
/* What the Passthrough tab changes, or null. */
window.vmDevicesChanges = async () => {
  const out = { add: $$("#mbody .pd-add").map(x => ({ name: x.dataset.hostdev, resource: x.dataset.resource })), map: {}, remove: [], roms: {} };
  for (const row of $$("#mbody tr[data-hostdev], #mbody .pd-add")) {
    const name = row.dataset.hostdev;
    if (row.querySelector(".pd_rm")?.checked) { out.remove.push(name); continue; }
    const selected = row.querySelector(".pd_resource")?.value;
    if (selected && selected !== row.dataset.resource) out.map[name] = selected;
    const file = row.querySelector(".pd_rom")?.files?.[0];
    if (file) out.roms[name] = await readRom(file);
    else if (row.querySelector(".pd_clear")?.checked) out.roms[name] = "";
  }
  return out.add.length || out.remove.length || Object.keys(out.map).length || Object.keys(out.roms).length ? out : null;
};
