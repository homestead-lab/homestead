/* A host's PCI and USB devices given to virtual machines, and a GPU's ROM.

   homestead_passthrough.py does the work: on Harvester through its own
   device claims; on k3s and RKE2 by switching IOMMU on (from the next
   restart), handing a PCI device and its IOMMU group to vfio-pci, and
   listing it with KubeVirt. A VM then asks for the device by name on its
   Passthrough tab, where a GPU can be given its ROM (vBIOS) too. */

const PT = { facts: {}, sequence: {} };

/* Reopening a host reuses the last complete inspection. A stored snapshot
   is display data; every handoff still inspects the host again on the server. */
function ptKey(node) {
  const fleet = window.FLEET;
  return JSON.stringify([fleet?.target && fleet.target !== fleet.view?.self ? fleet.target : "", node]);
}
function ptBody(node, key = ptKey(node)) {
  const card = $("#nodeDevices");
  return card?.dataset.node === node && card.dataset.scope === key ? card.querySelector(".pt-body") : null;
}
window.nodeDevicesPaint = async node => {
  const card = $("#nodeDevices");
  if (!card) return;
  card.dataset.node = node;
  const key = ptKey(node);
  card.dataset.scope = key;
  const sequence = PT.sequence[key] = (PT.sequence[key] || 0) + 1;
  if (PT.facts[key]) { nodeDevicesRender(node, PT.facts[key], "", key); return; }
  const body = ptBody(node, key);
  body.innerHTML = '<div class="dim small"><span class="spin2"></span> loading the last inspection</div>';
  try {
    const saved = await api("/api/passthrough/inventory?node=" + encodeURIComponent(node));
    if (PT.sequence[key] !== sequence) return;
    if (saved.facts) { PT.facts[key] = saved.facts; nodeDevicesRender(node, saved.facts, "", key); return; }
  } catch { /* A fresh inspection remains available when no cache can be read. */ }
  if (!ptBody(node, key) || PT.sequence[key] !== sequence) return;
  body.innerHTML = `${UI.lead("PCI devices, such as a GPU, network card or HBA, and USB devices that this host can give to its VMs.")}
    ${UI.actions(UI.button("Look at its devices", `nodeDevicesLook(${jsArg(node)})`, { attrs: 'data-need="admin"' }))}`;
  if (window.applyRole) applyRole();
};

window.nodeDevicesLook = async node => {
  const key = ptKey(node);
  const body = ptBody(node, key);
  if (!body) return;
  const sequence = PT.sequence[key] = (PT.sequence[key] || 0) + 1;
  body.innerHTML = '<div class="dim small"><span class="spin2"></span> looking at the host</div>';
  try {
    const facts = await api("/api/passthrough/inspect", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ node }) });
    if (PT.sequence[key] !== sequence) return;
    PT.facts[key] = facts;
    nodeDevicesRender(node, facts, "", key);
  } catch (error) {
    if (!ptBody(node, key) || PT.sequence[key] !== sequence) return;
    if (PT.facts[key]) nodeDevicesRender(node, PT.facts[key], error.message, key);
    else body.innerHTML = UI.callout("bad", "The host's devices could not be read.", esc(error.message)) +
      UI.actions(UI.button("Try again", `nodeDevicesLook(${jsArg(node)})`, { attrs: 'data-need="admin"' }));
  }
};
function ptGroup(facts, row) {
  return [row, ...(facts.pci || []).filter(r => (row.group_members || []).includes(r.address))];
}
function ptDeviceName(row) { return `${row.name || row.address} (${row.address})`; }
function nodeDevicesRender(node, f, error = "", key = ptKey(node)) {
  const body = ptBody(node, key);
  if (!body) return;
  const pci = f.pci || [];
  const groups = new Map();
  for (const row of pci) {
    const key = row.group == null || row.group === "" ? `device-${row.address}` : String(row.group);
    if (!groups.has(key)) groups.set(key, []);
    groups.get(key).push(row);
  }
  const state = r => r.vfio ? UI.chip("for VMs", "ok") : r.listed ? UI.chip("for VMs after a restart", "info") : `<span class="dim xs">${esc(r.driver || "No driver")}</span>`;
  const handoff = r => {
    if (!r.offered) return `<span class="dim xs">${(r.class || "").startsWith("0604") ? "Stays with the host" : "Not offered separately"}</span>`;
    if (r.vfio || r.listed) return UI.button("Return to host", `ptPci(${jsArg(node)},${jsArg(r.address)},false)`, { attrs: 'data-need="admin"' });
    const blocked = ptGroup(f, r).filter(x => !(x.class || "").startsWith("0604") && x.problems?.length);
    const why = !f.iommu ? "IOMMU is off" : blocked.map(x => `${x.name || x.address}: ${x.problems.join("; ")}`).join("; ");
    return why ? `<span class="dim xs">${esc(why)}</span>`
      : UI.button("Give to VMs", `ptPci(${jsArg(node)},${jsArg(r.address)},true)`, { attrs: 'data-need="admin"' });
  };
  const action = r => UI.actions(handoff(r) + ((r.class || "").startsWith("03")
    ? UI.button("Capture vBIOS", `ptCaptureVbios(${jsArg(node)},${jsArg(r.address)},this)`, { attrs: 'data-need="admin"' }) : ""));
  const row = r => [`<b>${esc(r.name)}</b><div class="dim xs mono">${esc(r.address)}${r.class_name ? ` · ${esc(r.class_name)}` : ""}${r.boot_vga ? " · start-up display" : ""}</div>`, state(r), action(r)];
  const group = rows => {
    const id = rows[0].group;
    const moving = rows.filter(r => !(r.class || "").startsWith("0604"));
    const bridges = rows.length - moving.length;
    const text = f.harvester ? "Harvester manages each device claim. Check this group before assigning its devices to VMs."
      : `${moving.length} device${moving.length === 1 ? "" : "s"} move together when any device in this group is given to VMs.${bridges ? ` ${bridges} PCI bridge${bridges === 1 ? " stays" : "s stay"} with the host.` : ""}`;
    return UI.section(`${id == null || id === "" ? "No IOMMU group reported" : `IOMMU group ${id}`} · ${rows.length} device${rows.length === 1 ? "" : "s"}`,
      `<p class="dim small">${esc(text)}</p>` + UI.table([{ label: "Device" }, { label: "Now" }, { label: "" }], rows.map(row)));
  };
  const visible = [...groups.values()].filter(rows => rows.some(r => r.offered));
  const other = [...groups.values()].filter(rows => !rows.some(r => r.offered));
  const usb = (f.usb || []).map(u => [`<b>${esc(u.name)}</b><div class="dim xs mono">${esc(u.vendor)}:${esc(u.product)}${u.port ? ` · ${esc(u.port)}` : ""}</div>`,
    u.permitted ? UI.chip("offered to VMs", "ok") : '<span class="dim xs">Host</span>',
    UI.button(u.permitted ? "Stop offering" : "Offer to VMs",
      `ptUsb(${jsArg(node)},${jsArg(u.vendor)},${jsArg(u.product)},${!u.permitted},${jsArg(u.harvester_name || "")})`, { attrs: 'data-need="admin"' })]);
  body.innerHTML = `
    <div class="between"><span class="dim xs">${f.inspected_at ? `Last checked ${esc(fmtAt(f.inspected_at))}` : "Last checked devices"} · Each change checks the host again</span>
      ${UI.button("Refresh devices", `nodeDevicesLook(${jsArg(node)})`, { attrs: 'data-need="admin"' })}</div>
    ${UI.notices([["bad", error && `The refresh failed, so the last check is shown. ${error}`],
      ["warn", f.inventory_error && `This check could not be saved for after a reload. ${f.inventory_error}`]])}
    ${f.harvester ? UI.lead("Harvester gives devices to VMs with its own claims. This matches its Devices page.")
      : f.iommu ? `<div class="small">IOMMU ${UI.chip("on", "ok")} <span class="dim xs">${f.cmdline_iommu ? "" : "(by default)"}</span></div>`
      : UI.callout("warn", "IOMMU is off on this host.", `A device can be given to a VM only when IOMMU is on. Homestead can add it to the kernel command line.
          It is on from the next restart (Host actions), if VT-d or AMD-Vi is turned on in the firmware.
          ${UI.actions(UI.button("Turn on IOMMU", `ptIommu(${jsArg(node)})`, { kind: "pri", attrs: 'data-need="admin"' }))}`)}
    ${UI.section("PCI devices by IOMMU group", visible.length ? visible.map(group).join("") : UI.lead("This host has no PCI device that can be given to a VM."))}
    ${other.length ? UI.more(`${other.length} other IOMMU groups (bridges and chipset devices)`, other.map(group).join("")) : ""}
    ${UI.section("USB", usb.length ? UI.table([{ label: "Device" }, { label: "Now" }, { label: "" }], usb) : UI.lead("No USB devices."))}
    <p class="dim xs">A device given to VMs is no longer available to the host. A GPU's console goes dark, and a network card's link drops.
      ${f.harvester ? "Harvester manages the device claims." : "Devices in an IOMMU group move together; PCI bridges stay with the host."}
      A VM using PCI passthrough runs only on hosts that offer that device, and cannot move while running.
      ${f.harvester ? "" : "USB devices are offered by vendor and product, on any host that has one."}</p>`;
  if (window.applyRole) applyRole();
}

window.ptCaptureVbios = async (node, address, button) => {
  if (button) { button.disabled = true; button.textContent = "Capturing…"; }
  try {
    const result = await api("/api/passthrough/vbios/capture", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ node, address }) });
    const bytes = Uint8Array.from(atob(result.data), c => c.charCodeAt(0));
    const url = URL.createObjectURL(new Blob([bytes], { type: "application/octet-stream" }));
    const link = document.createElement("a");
    link.href = url; link.download = result.filename; document.body.appendChild(link); link.click(); link.remove();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
    toast("vBIOS downloaded. To use it, capture or upload it next to the GPU in VM → Edit → Passthrough.", "ok");
  } catch (error) { toast(error.message, "bad"); }
  finally { if (button) { button.disabled = false; button.textContent = "Capture vBIOS"; } }
};
window.ptPci = async (node, address, give) => {
  const r = (PT.facts[ptKey(node)]?.pci || []).find(x => x.address === address) || {};
  const others = ptGroup(PT.facts[ptKey(node)] || {}, r).filter(x => x.address !== address && !(x.class || "").startsWith("0604") && (give || x.listed));
  const together = !PT.facts[ptKey(node)]?.harvester && others.length ? ` These devices in IOMMU group ${r.group} move with it: ${others.map(ptDeviceName).join(", ")}.` : "";
  if (give && !(await ask(`Give ${r.name || address} on ${node} to VMs? The host releases it now and at every start-up${r.boot_vga ? ". This is its start-up display, so the local console goes dark" : ""}.${together}`,
    { title: "Give a device to VMs", ok: "Give to VMs" }))) return;
  if (!give && !(await ask(`Return ${r.name || address} to ${node}? A VM using it loses it at its next start.${together}`, { title: "Return a device to the host", ok: "Return to host" }))) return;
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
  if (!(await ask(`Turn on IOMMU for ${node}? It is added to the kernel command line (a copy of /etc/default/grub is kept) and is on from the host's next restart.`,
    { title: "Switch IOMMU on", ok: "Switch it on" }))) return;
  try {
    const res = await api("/api/passthrough/iommu", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ node }) });
    toast(res.detail, "ok");
  } catch (e) { toast(e.message, "bad"); }
  nodeDevicesLook(node);
};

/* ---------- a VM's devices ---------- */
function vmHostDeviceLabel(r) {
  const details = (r.devices || []).map(d => `${d.node}${d.address ? ` ${d.address}` : ""}${d.group != null && d.group !== "" ? ` · group ${d.group}` : ""}`);
  const described = new Set((r.devices || []).map(d => d.node));
  const where = [...new Set([...details, ...(r.nodes || []).filter(node => !described.has(node))])].join("; ");
  return `${r.kind === "usb" ? "USB" : "PCI"} ${r.label || r.resource}${r.selector && r.selector !== r.label ? ` [${r.selector}]` : ""}${where ? ` · ${where}` : ""}${!r.nodes?.length ? " · unavailable" : ""}${r.active_vms?.length ? ` · in use by ${r.active_vms.join(", ")}` : ""}`;
}
function vmDevicesPane(v, res) {
  const have = v.host_devices || [], list = (res && res.resources) || [];
  window.__vmDeviceResources = list;
  window.__vmDeviceNames = have.map(d => d.name);
  const nodesUrl = window.HomesteadRouter?.urlFor("nodes") || "/nodes";
  const label = vmHostDeviceLabel;
  return `${have.length ? `<div class="tblwrap"><table class="tbl dense stack ve-table"><thead><tr><th>Device</th><th>ROM (vBIOS)</th><th></th></tr></thead><tbody>
      ${have.map(d => `<tr data-hostdev="${esc(d.name)}" data-resource="${esc(d.resource)}"><td data-label="Device"><label for="pd_device_${esc(d.name)}">${esc(d.name)}</label><select id="pd_device_${esc(d.name)}" class="pd_resource" aria-label="Device for ${esc(d.name)}">
        ${!list.some(r => r.resource === d.resource) ? `<option value="${esc(d.resource)}">${esc(d.resource)} · unavailable</option>` : ""}${list.filter(r => !d.gpu || r.kind === "pci").map(r => `<option value="${esc(r.resource)}" ${r.resource === d.resource ? "selected" : ""}>${esc(label(r))}</option>`).join("")}</select></td>
        <td data-label="ROM (vBIOS)">${d.rom ? `${UI.chip("its own ROM", "info")} <label class="check xs"><input type="checkbox" class="pd_clear"> clear</label>` : ""}
          ${((r) => vmRomControls(d.name, r, d.gpu || r?.gpu, d.rom))(list.find(r => r.resource === d.resource))}</td>
        <td><label class="check xs"><input type="checkbox" class="pd_rm"> remove</label></td></tr>`).join("")}</tbody></table></div>`
      : UI.lead("Select a PCI or USB device offered by the cluster's hosts.")}
    <div id="pd_adds"></div>
    ${UI.notices([
      ["bad", res?.error && `The devices could not be loaded. ${res.error}`],
      ["warn", res?.usage_error && `Device use is unavailable. ${res.usage_error}`],
      ...list.filter(r => r.configured_vms?.length > 1).map(r => ["info", `${r.label || r.resource} is set up on ${r.configured_vms.join(", ")}`])])}
    ${list.length ? `<div class="f" style="margin-top:10px"><label for="pd_pick">Add PCI or USB device</label><select id="pd_pick">${list.map(r => `<option value="${esc(r.resource)}">${esc(label(r))}</option>`).join("")}</select></div>
      ${UI.actions(UI.button("Add device", "vmAddHostDevice()"))}`
      : '<p class="dim small">No host devices are offered to VMs. Offer a device on its host first.</p>'}
    <p class="dim small">Passthrough limits this VM to hosts that offer its devices, and stops it from moving while running. Several VMs may use the same device in their settings, and Start checks that enough devices are free. Changes apply at the next start.</p>
    ${UI.more("IOMMU and vBIOS setup", `<p>Open <a href="${esc(nodesUrl)}" target="_blank" rel="noopener">Hosts</a> in a new tab, select a host, then Hardware → Devices for VMs. Check IOMMU and offer the PCI or USB device there. Turning on IOMMU may need a host restart.</p><p>A GPU uses its own ROM unless a vBIOS is provided. Some cards need one to show a picture, such as a GPU that was the host's start-up display and many NVIDIA cards. <b>Capture from host</b>, next to the GPU, reads it on the host that offers the GPU (given to VMs, with no VM using it) and saves it with this VM. A file can be chosen instead. It must start with 55 AA and be at most 640 KiB. Homestead turns on KubeVirt's hook sidecar when a vBIOS is used.</p>`)}`;
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
    <div class="between"><b>${esc(resource ? vmHostDeviceLabel(resource) : pick.value)}</b><button type="button" class="btn sm" onclick="this.closest('.pd-add').remove()" aria-label="Remove ${name}">Remove</button></div>
    <p class="dim xs">${esc(name)} · ${esc((resource?.nodes || []).join(", ") || "No host offers this device now")}</p>
    ${resource?.kind !== "usb" ? `<div class="f"><label for="pd_rom_${name}">vBIOS <span class="dim">optional</span></label>${vmRomControls(name, resource, resource?.gpu)}</div>` : ""}</div>`);
};

/* A GPU's vBIOS, in Edit: captured from the host that offers the GPU, or
   a file. Most cards need neither - the card's own ROM is used - but some
   (a GPU that was the host's boot display, many laptops' and some NVIDIA
   cards) give a black screen without a clean copy. The capture reads the
   ROM there and keeps it with this row, so it is saved with the VM. */
function vmRomControls(name, resource, gpu, hasRom = false) {
  const sources = gpu ? (resource?.devices || []).filter(d => d.node && d.address) : [];
  const file = `<input id="pd_rom_${esc(name)}" type="file" class="pd_rom" aria-label="vBIOS file for ${esc(name)}" accept=".rom,.bin,application/octet-stream">`;
  if (!sources.length) return file;
  const from = sources.length > 1
    ? `<select class="pd_rom_from" aria-label="Capture the vBIOS from">${sources.map((d, i) => `<option value="${i}">${esc(d.node)} · ${esc(d.address)}</option>`).join("")}</select>`
    : `<span class="dim xs">from ${esc(sources[0].node)} · ${esc(sources[0].address)}</span>`;
  return `<div class="pd-rom" data-rom-for="${esc(name)}">
      <div class="row">${UI.button("Capture from host", `vmCaptureRom(this,${jsArg(name)})`, { kind: "sm", attrs: 'data-need="admin"' })} ${from}</div>
      <div class="dim xs pd_rom_state">${hasRom ? "Uses the vBIOS saved with this VM." : "Uses the card's own ROM."} Capturing needs the GPU to be given to VMs, with no running VM using it. A file can be chosen instead:</div>
      ${file}</div>`;
}
window.vmRomControls = vmRomControls;
window.vmCaptureRom = async (button, name) => {
  const box = button.closest(".pd-rom"), row = button.closest("[data-hostdev]");
  const resource = row?.querySelector(".pd_resource")?.value || row?.dataset.resource;
  const found = (window.__vmDeviceResources || []).find(r => r.resource === resource);
  const sources = (found?.devices || []).filter(d => d.node && d.address);
  const source = sources[+(box.querySelector(".pd_rom_from")?.value || 0)];
  const state = box.querySelector(".pd_rom_state");
  if (!source) { state.textContent = "No host offers this GPU now, so there is nothing to capture."; return; }
  button.disabled = true; state.textContent = `Reading the vBIOS on ${source.node}…`;
  try {
    const result = await api("/api/passthrough/vbios/capture", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ node: source.node, address: source.address }) });
    box.dataset.captured = result.data;
    state.textContent = `Captured from ${source.node} · ${source.address} (${Math.round(result.size / 1024)} KiB). It is saved with the VM when the changes are reviewed and saved.`;
  } catch (error) {
    delete box.dataset.captured;
    state.textContent = `Not captured. ${error.message}`;
  } finally { button.disabled = false; }
};
async function readRom(file) {
  const buffer = await file.arrayBuffer();
  let text = "";
  const bytes = new Uint8Array(buffer);
  if (!bytes.length || bytes.length > 640 * 1024) throw new Error("vBIOS files must be at most 640 KiB");
  if (bytes[0] !== 0x55 || bytes[1] !== 0xaa) throw new Error("The vBIOS file must start with 55 AA. Remove any dump header first.");
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
    const captured = row.querySelector(".pd-rom")?.dataset?.captured;
    if (file) out.roms[name] = await readRom(file);
    else if (captured) out.roms[name] = captured;
    else if (row.querySelector(".pd_clear")?.checked) out.roms[name] = "";
  }
  return out.add.length || out.remove.length || Object.keys(out.map).length || Object.keys(out.roms).length ? out : null;
};
