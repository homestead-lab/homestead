/* A host's PCI and USB devices given to virtual machines, and a GPU's ROM.

   homestead_passthrough.py does the work: on Harvester through its own
   device claims; on k3s and RKE2 by switching IOMMU on (from the next
   restart), handing a PCI device and its IOMMU group to vfio-pci, and
   listing it with KubeVirt. A VM then asks for the device by name on its
   Devices tab, where a GPU can be given its ROM (vBIOS) too. */

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
  const label = r => `${r.kind === "usb" ? "USB" : "PCI"} ${r.label || r.resource}${r.nodes.length ? ` · on ${r.nodes.join(", ")}` : " · no host offers it now"}`;
  return `${have.length ? `<div class="tblwrap"><table class="tbl dense stack ve-table"><thead><tr><th>Device</th><th>ROM (vBIOS)</th><th></th></tr></thead><tbody>
      ${have.map(d => `<tr data-hostdev="${esc(d.name)}"><td data-label="Device"><b>${esc(d.name)}</b><div class="dim xs mono">${esc(d.resource)}</div></td>
        <td data-label="ROM (vBIOS)">${d.rom ? `${UI.chip("its own ROM", "info")} <label class="check xs"><input type="checkbox" class="pd_clear"> clear</label>` : ""}
          <input type="file" class="pd_rom" accept=".rom,.bin,application/octet-stream"></td>
        <td><label class="check xs"><input type="checkbox" class="pd_rm"> remove</label></td></tr>`).join("")}</tbody></table></div>`
      : UI.lead("No host devices yet.")}
    <div id="pd_adds"></div>
    ${list.length ? `<div class="row" style="margin-top:10px"><select id="pd_pick">${list.map(r => `<option value="${esc(r.resource)}">${esc(label(r))}</option>`).join("")}</select>
      <button class="btn sm" onclick="vmAddHostDevice()">＋ Device</button></div>`
      : '<div class="dim xs" style="margin-top:8px">No device is offered to VMs yet: hand one over on its host\'s page, under Devices.</div>'}
    <p class="dim xs" style="margin-top:8px">A VM with a host device runs only where the device is, and cannot live-migrate; changes apply at its next start.
      A ROM is for a GPU that needs its own (a card the host booted from, or a patched ROM): up to 640 KB, starting 55 AA. It uses KubeVirt's hook
      sidecar, which Homestead switches on.</p>`;
}
window.vmDevicesPane = vmDevicesPane;
window.vmAddHostDevice = () => {
  const pick = $("#pd_pick");
  if (!pick) return;
  $("#pd_adds").insertAdjacentHTML("beforeend", `<div class="pd-add row" data-resource="${esc(pick.value)}" style="margin-top:6px">
    <span class="tag">new</span><span class="mono xs">${esc(pick.value)}</span><button class="btn sm" onclick="this.closest('.pd-add').remove()">✕</button></div>`);
};
async function readRom(file) {
  const buffer = await file.arrayBuffer();
  let text = "";
  const bytes = new Uint8Array(buffer);
  for (let i = 0; i < bytes.length; i += 0x8000) text += String.fromCharCode.apply(null, bytes.subarray(i, i + 0x8000));
  return btoa(text);
}
/* What the Devices tab changes, or null. */
window.vmDevicesChanges = async () => {
  const out = { add: $$("#mbody .pd-add").map(x => ({ resource: x.dataset.resource })), remove: [], roms: {} };
  for (const row of $$("#mbody tr[data-hostdev]")) {
    const name = row.dataset.hostdev;
    if (row.querySelector(".pd_rm")?.checked) { out.remove.push(name); continue; }
    const file = row.querySelector(".pd_rom")?.files?.[0];
    if (file) out.roms[name] = await readRom(file);
    else if (row.querySelector(".pd_clear")?.checked) out.roms[name] = "";
  }
  return out.add.length || out.remove.length || Object.keys(out.roms).length ? out : null;
};
