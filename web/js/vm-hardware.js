/* A VM's virtual hardware, and the ISO library its CD-ROM drives read from.

   The Hardware tab keeps each group folded, with a one-line summary of what
   it is set to: processor, firmware and security, guest tuning, devices,
   memory and placement. Only fields someone changes are sent, so opening
   the form never rewrites a VM. What a setting needs from the cluster is
   said beside it as it is chosen.

   ISOs come from folders an admin picks on the Network Shares. Each is made
   ready once - copied into a volume VMs can attach - and then offered to any
   VM's CD-ROM drive (homestead_isos.py). */

/* Presets for common guests, as Proxmox's OS types set them: each sets only
   what matters for that guest and leaves the rest as it is. "LOCAL" is this
   browser's time zone, since Windows keeps its clock in local time. `nic` is
   the network card a new VM of that type gets: Windows has no VirtIO network
   driver of its own, so a Windows guest installed from an ISO came up with no
   network. Proxmox gives its Windows types an Intel e1000; an e1000e is the
   one Windows 10 and 11 drive out of the box, and virtio stays a choice once
   the guest tools from virtio-win.iso are in. */
const VM_PRESETS = {
  windows11: { name: "Windows 11 / Server 2022+", about: "UEFI with Secure Boot, a TPM whose state is kept, Hyper-V enlightenments, a tablet pointer, local time, and an Intel e1000e network card that Windows supports without extra drivers",
    nic: "e1000e", hw: { firmware: "uefi", secure_boot: true, efi_persistent: true, tpm: "persistent", machine: "q35", hyperv: true,
      tablet: true, graphics: true, sound: false, rng: false, timezone: "LOCAL" } },
  windows10: { name: "Windows 10 / Server 2019", about: "UEFI, Hyper-V enlightenments, a tablet pointer, local time, and an Intel e1000e network card that Windows supports without extra drivers",
    nic: "e1000e", hw: { firmware: "uefi", secure_boot: false, efi_persistent: true, tpm: "off", machine: "q35", hyperv: true,
      tablet: true, graphics: true, sound: false, rng: false, timezone: "LOCAL" } },
  linux: { name: "Linux server", about: "UEFI, a random-number device and UTC",
    hw: { firmware: "uefi", secure_boot: false, efi_persistent: true, tpm: "off", hyperv: false, rng: true,
      tablet: false, sound: false, graphics: true, timezone: "" } },
  linuxdesktop: { name: "Linux desktop", about: "UEFI, a random-number device, a tablet pointer and sound",
    hw: { firmware: "uefi", secure_boot: false, efi_persistent: true, tpm: "off", hyperv: false, rng: true,
      tablet: true, sound: true, graphics: true, timezone: "" } },
  cloud: { name: "Linux cloud image (BIOS)", about: "BIOS, as most cloud images expect, and a random-number device",
    hw: { firmware: "bios", secure_boot: false, efi_persistent: false, tpm: "off", hyperv: false, rng: true,
      tablet: false, sound: false, timezone: "" } },
  headless: { name: "Headless appliance", about: "No display, only the serial console, and a random-number device",
    hw: { graphics: false, serial: true, tablet: false, sound: false, rng: true } },
};
window.VM_PRESETS = VM_PRESETS;

/* A preset's settings with its placeholders filled in: what a new VM is
   sent, and what the Hardware tab's fields are set to. */
function vmPresetSettings(id) {
  const preset = VM_PRESETS[id];
  if (!preset) return null;
  const hw = { ...preset.hw };
  if (hw.timezone === "LOCAL") hw.timezone = Intl.DateTimeFormat().resolvedOptions().timeZone || "";
  return hw;
}
window.vmPresetSettings = vmPresetSettings;

const VH_SECTIONS = {
  cpu: "Processor", firmware: "Firmware and security", tuning: "Guest tuning",
  devices: "Display and devices", memory: "Memory and placement",
};

function vhOpt(value, label, current) {
  return `<option value="${esc(value)}" ${String(current) === String(value) ? "selected" : ""}>${esc(label)}</option>`;
}
function vhCheck(id, label, checked, help = "") {
  return `<label class="switch vh-check"><input type="checkbox" id="${id}" ${checked ? "checked" : ""} onchange="vmHardwareChanged()">
    <span>${esc(label)}${help ? ` ${tip(help)}` : ""}</span></label>`;
}

function vhSummary(h) {
  const c = h.cpu || {};
  const total = c.sockets * c.cores * c.threads;
  return {
    cpu: `${total} vCPU${total === 1 ? "" : "s"} · ${c.sockets} socket${c.sockets === 1 ? "" : "s"} × ${c.cores} core${c.cores === 1 ? "" : "s"}${c.threads > 1 ? ` × ${c.threads} threads` : ""} · ${c.model || "cluster default model"}${c.dedicated ? " · dedicated" : ""}`,
    firmware: `${h.firmware === "uefi" ? `UEFI${h.secure_boot ? " · Secure Boot" : ""}` : "BIOS"} · TPM ${h.tpm === "off" ? "off" : h.tpm === "persistent" ? "on, kept" : "on"}${h.machine ? ` · ${h.machine}` : ""}`,
    tuning: [h.hyperv ? "Hyper-V enlightenments" : "", h.kvm_hidden ? "KVM hidden" : "", h.timezone || "UTC"].filter(Boolean).join(" · "),
    devices: [h.boot_output === "gpu" ? "GPU display, no virtual display" : h.graphics ? "virtual display" : "no display, serial only", h.serial ? "serial console" : "", h.tablet ? "tablet" : "", h.rng ? "RNG" : "",
      h.balloon ? "balloon" : "", h.sound ? "sound" : ""].filter(Boolean).join(" · "),
    memory: `${h.hugepages ? `${h.hugepages} hugepages` : "normal pages"} · on drain: ${({ LiveMigrate: "live-migrate", LiveMigrateIfPossible: "live-migrate if it can", None: "stop" })[h.eviction] || "cluster default"}`,
  };
}

/* The fields, grouped. `h` is the VM's hardware as the server reads it. */
function vmHardwareFields(h, o = {}, locked = false, creating = false, split = false) {
  const c = h.cpu || {};
  // What the summaries and notes compare against: this VM, or a new one.
  window.__vhBase = h;
  window.__vhOpts = o;
  const models = ["", "host-model", "host-passthrough", ...(o.cpu_models || [])];
  const summary = vhSummary(h);
  const section = (key, body) => `<details class="ui-more vh-sec" data-sec="${key}">
      <summary><span>${esc(VH_SECTIONS[key])}</span><span class="vh-sum dim xs" id="vh_sum_${key}">${esc(summary[key])}</span></summary>
      <div class="vh-body">${body}</div></details>`;
  return `<div class="vh" id="vh">
    ${creating ? "" : `<div class="row vh-presets"><label for="vh_preset" class="dim xs">Guest type</label>
      <select id="vh_preset" onchange="vmHardwarePreset(this.value); this.value = ''"><option value="">Apply a preset…</option>
        ${Object.entries(VM_PRESETS).map(([id, p]) => `<option value="${esc(id)}" title="${esc(p.about)}">${esc(p.name)}</option>`).join("")}</select>
      <span class="dim xs">Fills in the fields below. Nothing changes until the VM is reviewed and saved.</span></div>`}
    ${section("cpu", `${creating ? "" : locked ? '<div class="note small">CPU topology is set by this VM\'s instance type.</div>' : `
      <div class="vh-3"><div class="f"><label>Sockets</label><input id="vh_sockets" type="number" min="1" max="8" value="${c.sockets}" oninput="vmHardwareChanged()"></div>
        <div class="f"><label>Cores per socket</label><input id="vh_cores" type="number" min="1" max="128" value="${c.cores}" oninput="vmHardwareChanged()"></div>
        <div class="f"><label>Threads per core</label><input id="vh_threads" type="number" min="1" max="8" value="${c.threads}" oninput="vmHardwareChanged()"></div></div>`}
      <div class="f"><label>CPU model ${tip("host-model uses the host's CPU as a named model that KubeVirt can match on other hosts. host-passthrough uses every feature of this host's exact CPU. It is the fastest, but the VM can move while running only to hosts with identical CPUs. A named model keeps VMs movable across mixed hardware.")}</label>
        <select id="vh_model" onchange="vmHardwareChanged()">${models.map(m => vhOpt(m, m || "Cluster default", c.model)).join("")}</select></div>
      ${vhCheck("vh_dedicated", "Dedicated CPUs (pinned)", c.dedicated, "Each vCPU gets its own host CPU, like CPU affinity in Proxmox. Needs the kubelet's static CPU manager policy on the host.")}
      ${vhCheck("vh_isolate", "Isolate the emulator thread", c.isolate_emulator, "QEMU's own work runs on one more dedicated CPU, separate from the guest's CPUs. Needs dedicated CPUs.")}`)}
    ${section("firmware", `<div class="f2"><div class="f"><label>Firmware</label>
        <select id="vh_firmware" onchange="vmHardwareChanged()">${vhOpt("bios", "BIOS (SeaBIOS)", h.firmware)}${vhOpt("uefi", "UEFI (OVMF)", h.firmware)}</select></div>
      <div class="f"><label>Machine type ${tip("q35 is KubeVirt's modern PC with PCIe, which nearly every guest needs. Leave blank for the cluster default.")}</label>
        <select id="vh_machine" onchange="vmHardwareChanged()">${["", "q35"].concat(h.machine && h.machine !== "q35" ? [h.machine] : []).map(m => vhOpt(m, m || "Cluster default", h.machine)).join("")}</select></div></div>
      ${vhCheck("vh_secure", "Secure Boot", h.secure_boot, "Starts only signed bootloaders, such as Windows 11 and most current Linux installers. Needs UEFI.")}
      ${vhCheck("vh_efikeep", "Keep EFI variables", h.efi_persistent, "Boot entries and Secure Boot keys are kept across restarts, as on real hardware. They are stored in a small volume for each VM.")}
      <div class="f"><label>TPM ${tip("A virtual TPM 2.0. Windows 11 needs one. When its state is kept, BitLocker keys and measured boot data survive restarts.")}</label>
        <select id="vh_tpm" onchange="vmHardwareChanged()">${vhOpt("off", "Off", h.tpm)}${vhOpt("on", "On", h.tpm)}${vhOpt("persistent", "On, state kept", h.tpm)}</select></div>`)}
    ${section("tuning", `${vhCheck("vh_hyperv", "Hyper-V enlightenments", h.hyperv, "Paravirtual timers, spinlocks and interrupts that help Windows run faster under a hypervisor. For Windows guests.")}
      ${vhCheck("vh_kvmhidden", "Hide KVM from the guest", h.kvm_hidden, "Some GPU drivers refuse to run in a VM. Hiding the hypervisor's signature lets them run. Only for passed-through GPUs.")}
      <div class="f"><label>Clock ${tip("UTC suits Linux. Windows keeps its clock in local time, so enter the time zone, such as Europe/London.")}</label>
        <input id="vh_tz" class="mono" value="${esc(h.timezone || "")}" placeholder="UTC" oninput="vmHardwareChanged()"></div>`)}
    ${split ? "" : section("devices", vmDisplayFields(h))}
    ${section("memory", `<div class="f2"><div class="f"><label>Hugepages ${tip("Guest memory in large pages. This is faster for large or busy guests, but the pages must be reserved on the host first.")}</label>
        <select id="vh_huge" onchange="vmHardwareChanged()">${vhOpt("", "Normal pages", h.hugepages)}${vhOpt("2Mi", "2 MiB", h.hugepages)}${vhOpt("1Gi", "1 GiB", h.hugepages)}</select></div>
      <div class="f"><label>When the host is drained ${tip("What happens when its host is drained for maintenance: move it to another host while it runs, try to move it, or stop it.")}</label>
        <select id="vh_evict" onchange="vmHardwareChanged()">${vhOpt("", "Cluster default", h.eviction)}${vhOpt("LiveMigrate", "Move while running", h.eviction)}${vhOpt("LiveMigrateIfPossible", "Move while running if possible", h.eviction)}${vhOpt("None", "Stop it", h.eviction)}</select></div></div>`)}
    <div id="vh_notes"></div></div>`;
}
window.vmHardwareFields = vmHardwareFields;

/* The display and the virtual devices: in Hardware, or a dialog's own
   section beside Disks and Passthrough (vmHardwareFields with split). */
function vmDisplayFields(h) {
  return `<div class="f"><label for="vh_boot_output">Display</label>
      <select id="vh_boot_output" onchange="vmBootOutputChanged()">${vhOpt("console", "Virtual display (the console's Screen)", h.boot_output || (h.graphics ? "console" : "serial"))}${vhOpt("gpu", "Passed-through GPU only (a monitor on the GPU, no virtual display)", h.boot_output)}${vhOpt("serial", "No display (serial console only)", h.boot_output || (!h.graphics ? "serial" : "console"))}</select>
      <div class="dim xs">Keep the virtual display to use a passed-through GPU for computing or transcoding while still seeing the VM in the console. GPU only removes the virtual display and uses UEFI. Attach the GPU under Passthrough first. This applies at the next VM start.</div></div>
      ${vhCheck("vh_serial", "Serial console", h.serial, "A text console, and the start-up log that Homestead keeps.")}
      ${vhCheck("vh_tablet", "Tablet pointer", h.tablet, "An absolute pointer, so that the mouse in the console lines up with the guest's.")}
      ${vhCheck("vh_rng", "Random-number device", h.rng, "virtio-rng provides randomness from the host, so that a new guest does not stall while creating keys.")}
      ${vhCheck("vh_balloon", "Memory balloon", h.balloon, "Lets the guest report and return unused memory.")}
      ${vhCheck("vh_sound", "Sound card", h.sound, "An ich9 sound card, for a desktop guest.")}
      <div id="vh_display_notes"></div>`;
}
window.vmDisplayFields = vmDisplayFields;

/* The form's values, in the server's terms. */
function vmHardwareValues() {
  const val = id => $(`#${id}`);
  const num = id => val(id) ? Math.max(1, parseInt(val(id).value, 10) || 1) : null;
  const cpu = { model: val("vh_model")?.value || "", dedicated: !!val("vh_dedicated")?.checked,
    isolate_emulator: !!val("vh_isolate")?.checked };
  if (val("vh_sockets")) Object.assign(cpu, { sockets: num("vh_sockets"), cores: num("vh_cores"), threads: num("vh_threads") });
  return { cpu, firmware: val("vh_firmware")?.value || "bios", secure_boot: !!val("vh_secure")?.checked,
    efi_persistent: !!val("vh_efikeep")?.checked, tpm: val("vh_tpm")?.value || "off", machine: val("vh_machine")?.value || "",
    hyperv: !!val("vh_hyperv")?.checked, kvm_hidden: !!val("vh_kvmhidden")?.checked, timezone: (val("vh_tz")?.value || "").trim(),
    boot_output: val("vh_boot_output")?.value || "console", graphics: (val("vh_boot_output")?.value || "console") === "console", serial: !!val("vh_serial")?.checked, tablet: !!val("vh_tablet")?.checked,
    rng: !!val("vh_rng")?.checked, balloon: !!val("vh_balloon")?.checked, sound: !!val("vh_sound")?.checked,
    hugepages: val("vh_huge")?.value || "", eviction: val("vh_evict")?.value || "" };
}

/* Only what differs from the VM as it is - the server writes nothing else. */
function vmHardwareChanges(original) {
  if (!$("#vh")) return null;
  const now = vmHardwareValues(), out = {};
  const cpu = {};
  for (const key of ["sockets", "cores", "threads", "model", "dedicated", "isolate_emulator"]) {
    if (key in now.cpu && now.cpu[key] !== original.cpu[key]) cpu[key] = now.cpu[key];
  }
  if (Object.keys(cpu).length) out.cpu = cpu;
  for (const key of Object.keys(now)) {
    const before = key === "boot_output" ? original.boot_output || (original.graphics ? "console" : "serial") : original[key];
    if (key !== "cpu" && now[key] !== before) out[key] = now[key];
  }
  // Firmware settings travel together: the server needs all three to decide.
  if (["firmware", "secure_boot", "efi_persistent"].some(key => key in out)) {
    Object.assign(out, { firmware: now.firmware, secure_boot: now.secure_boot, efi_persistent: now.efi_persistent });
  }
  return Object.keys(out).length ? out : null;
}
window.vmHardwareChanges = vmHardwareChanges;

/* What the cluster must provide for what is chosen, said as it is chosen. */
function vmHardwareNotes(h, o = {}) {
  const gates = new Set(o.kubevirt_gates || []);
  const notes = [];
  if (h.secure_boot && h.firmware !== "uefi") notes.push(["bad", "Secure Boot needs UEFI firmware."]);
  if (h.cpu.isolate_emulator && !h.cpu.dedicated) notes.push(["bad", "An isolated emulator thread needs dedicated CPUs."]);
  if (h.cpu.dedicated) notes.push(["warn", "Dedicated CPUs need hosts whose kubelet uses the static CPU manager policy. On other hosts the VM does not start."]);
  if (h.hugepages) notes.push(["warn", `${h.hugepages} hugepages must be reserved on the host (a kernel setting), or the VM does not start.`]);
  if ((h.efi_persistent || h.tpm === "persistent") && !gates.has("VMPersistentState"))
    notes.push(["info", "Kept EFI variables and TPM state are stored in a small volume for each VM. KubeVirt versions before 1.5 need the VMPersistentState feature for them."]);
  if (h.cpu.model === "host-passthrough") notes.push(["info", "With host-passthrough, the VM can move while running only to hosts with the same CPU."]);
  if (h.eviction === "LiveMigrate" && (o.nodes || []).length < 2) notes.push(["info", "Moving while running needs a second host. With one host, a drain waits for the VM."]);
  // The display's own notes, shown beside the Display field (a third element).
  if (h.boot_output === "gpu") {
    if (h.firmware !== "uefi") notes.push(["bad", "GPU only needs UEFI firmware.", "display"]);
    notes.push(["warn", "Connect the monitor to the passed-through GPU. Its ROM must support UEFI, and the guest needs GPU drivers. A guest installed with BIOS may need its bootloader repaired after switching to UEFI.", "display"]);
  }
  if (!h.graphics) notes.push(["info", "Without a virtual display, the console's Screen stays black. Keep the serial console on for troubleshooting.", "display"]);
  if (h.boot_output === "serial" && !h.serial) notes.push(["bad", "No display needs the serial console on.", "display"]);
  return notes;
}

window.vmHardwareChanged = () => {
  if (!$("#vh")) return;
  const h = vmHardwareValues();
  const base = window.__vhBase || {};
  const full = { ...h, cpu: { ...(base.cpu || {}), ...h.cpu } };
  const summary = vhSummary(full);
  Object.entries(summary).forEach(([key, text]) => { const el = $(`#vh_sum_${key}`); if (el) el.textContent = text; });
  if ($("#vh_secure")) $("#vh_secure").disabled = h.firmware !== "uefi";
  if ($("#vh_efikeep")) $("#vh_efikeep").disabled = h.firmware !== "uefi";
  if ($("#vh_isolate")) $("#vh_isolate").disabled = !h.cpu.dedicated;
  const notes = vmHardwareNotes(full, window.__vhOpts || {});
  const shown = list => UI.notices(list.map(([tone, text]) => [tone === "bad" || tone === "warn" ? tone : "info", text]));
  const display = $("#vh_display_notes");
  $("#vh_notes").innerHTML = shown(notes.filter(n => !display || n[2] !== "display"));
  if (display) display.innerHTML = shown(notes.filter(n => n[2] === "display"));
};

window.vmBootOutputChanged = () => {
  const output = $("#vh_boot_output")?.value;
  if (output === "gpu" && $("#vh_firmware")?.value !== "uefi") {
    $("#vh_firmware").value = "uefi";
    $("#vh_secure").checked = false;
  }
  if (output === "serial" && $("#vh_serial")) $("#vh_serial").checked = true;
  vmHardwareChanged();
};
const VH_FIELDS = { firmware: "vh_firmware", secure_boot: "vh_secure", efi_persistent: "vh_efikeep", tpm: "vh_tpm",
  machine: "vh_machine", hyperv: "vh_hyperv", kvm_hidden: "vh_kvmhidden", timezone: "vh_tz", boot_output: "vh_boot_output",
  serial: "vh_serial", tablet: "vh_tablet", rng: "vh_rng", balloon: "vh_balloon", sound: "vh_sound" };

function vhSet(hw) {
  if ("graphics" in hw && !("boot_output" in hw)) hw = { ...hw, boot_output: hw.graphics ? "console" : "serial" };
  for (const [key, value] of Object.entries(hw)) {
    const el = $(`#${VH_FIELDS[key]}`);
    if (!el) continue;
    if (el.type === "checkbox") el.checked = !!value;
    else {
      if (el.tagName === "SELECT" && ![...el.options].some(o => o.value === String(value))) el.add(new Option(value, value));
      el.value = value;
    }
  }
}

window.vmHardwarePreset = id => {
  const hw = vmPresetSettings(id);
  if (!hw) return;
  vhSet(hw);
  // Show what changed: the sections a preset touches open.
  $$("#vh details.vh-sec").forEach(d => { if (["firmware", "tuning", "devices"].includes(d.dataset.sec)) d.open = true; });
  vmHardwareChanged();
  toast(`${VM_PRESETS[id].name} settings chosen; review before saving`, "ok");
};

/* New VM: the guest type fills the Hardware fields in, from a plain VM, so
   switching type leaves nothing of the last one behind. */
window.vmCreatePreset = id => {
  const base = window.__vhBase || {};
  vhSet(Object.fromEntries(Object.keys(VH_FIELDS).map(key => [key, base[key]])));
  vhSet(vmPresetSettings(id) || {});
  if ($("#v_preset_about")) $("#v_preset_about").textContent = VM_PRESETS[id]?.about || "KubeVirt defaults: BIOS, UTC, a display and a serial console";
  if ($("#v_nic_model")) $("#v_nic_model").value = VM_PRESETS[id]?.nic || "virtio";
  vmHardwareChanged();
};

/* ---------------- the ISO library ---------------- */
window.vmIsoLibrary = async () => {
  modal("ISO library", '<div class="empty"><span class="spin2"></span> Reading the ISO folders…</div>', true);
  let lib;
  try { lib = await api("/api/vm/isos"); } catch (e) {
    $("#mbody").innerHTML = UI.callout("bad", "The ISO library could not be read.", esc(e.message)) + UI.actions(UI.cancel("Close"));
    return;
  }
  STATE.data.isoLibrary = lib;
  const size = bytes => bytesText(bytes);
  const state = f => f.volume
    ? (f.state === "ready" ? UI.chip("ready", "ok") : f.state === "copying" ? UI.chip("copying", "neutral") : UI.chip("failed", "bad"))
    : '<span class="dim xs">On the share only</span>';
  const volumeOf = f => lib.volumes.find(v => v.name === f.volume);
  // Each list is a stack table whose rows end in an actionBar (#506).
  const table = (heads, rows) => `<div class="tblwrap"><table class="tbl stack dense"><thead><tr>${heads.map(h => `<th>${h}</th>`).join("")}<th></th></tr></thead><tbody>${rows}</tbody></table></div>`;
  const orphans = lib.volumes.filter(v => !lib.files.some(f => f.volume === v.name));
  // A copy no VM has in a drive: how long until the tidy-up removes it.
  const unused = v => {
    if (!v || v.used_by.length || !v.unused_since || v.state === "copying") return "";
    const left = lib.keep_days ? Math.max(0, Math.ceil((v.unused_since + lib.keep_days * 86400 - Date.now() / 1000) / 86400)) : null;
    return ` · Unused${left === null ? "" : left ? `, removed in ${left} day${left === 1 ? "" : "s"}` : ", removed soon"}`;
  };
  $("#mbody").innerHTML = `<div class="ui-stack iso-lib">
    ${UI.lead("ISOs on network shares can be used to install VMs. <b>Prepare</b> creates a shared copy with one volume copy. The originals stay on the share, and lost copies can be created again.")}
    <div class="iso-folders"><div class="between"><b>Folders</b><button class="btn sm" data-need="admin" onclick="vmIsoAddFolder()">＋ Add folder</button></div>
      ${lib.folders.length ? table(["Folder"], lib.folders.map((f, i) => `<tr><td class="mono">${esc(f.share)}${f.path ? ` / ${esc(f.path)}` : ""}</td>
        <td data-actions>${actionBar([{ label: "Remove", run: `vmIsoRemoveFolder(${i})`, need: "admin", tip: "Stop listing this folder. Its files and copies are kept." }], { label: `Actions for ${f.share}${f.path ? ` / ${f.path}` : ""}` })}</td></tr>`).join(""))
        : `<div class="dim small">${lib.shares.length ? "No folders have been chosen yet. Add a folder from one of the shares." : "There are no network shares yet. Create one under Storage → Network shares, then choose a folder on it here."}</div>`}
      ${(lib.problems || []).map(p => `${UI.callout("bad", "", `${esc(p.share)}${p.path ? ` / ${esc(p.path)}` : ""}: ${esc(p.error)}`, { className: "small" })}`).join("")}</div>
    ${lib.files.length ? `<div class="iso-files">${table(["File", "Copy"], lib.files.map(f => `<tr><td><b>${esc(f.name)}</b>
        <div class="dim xs mono">${esc(f.share)}${f.folder ? ` / ${esc(f.folder)}` : ""} · ${size(f.size)}${f.volume && (volumeOf(f)?.used_by || []).length
          ? ` · in ${esc(volumeOf(f).used_by.join(", "))}` : esc(unused(volumeOf(f)))}</div></td>
        <td data-label="Copy">${state(f)}</td>
        <td data-actions>${actionBar([
          f.volume ? null : { label: "Prepare", run: `vmIsoPrepare(${jsq(f.share)},${jsq(f.path)})`, need: "operator", tip: "Create a shared copy that VMs can start from" },
          f.volume ? { label: "Delete copy", icon: "trash", run: `vmIsoDelete(${jsq(f.volume)})`, need: "admin", danger: true, tip: "Delete its volume. The file on the share is kept." } : null,
        ], { shown: f.volume ? 0 : 1, label: `Actions for ${f.name}` })}</td></tr>`).join(""))}</div>`
      : lib.folders.length ? '<div class="dim small">There are no .iso files in these folders yet.</div>' : ""}
    ${orphans.length ? `<div class="iso-files"><div class="dim xs">VOLUMES WHOSE FILE WAS REMOVED OR CHANGED</div>${table(["Volume"], orphans.map(v => `<tr><td><b>${esc(v.file || v.name)}</b>
        <div class="dim xs mono">${esc(v.name)}${v.used_by.length ? ` · in ${esc(v.used_by.join(", "))}` : esc(unused(v))}</div></td>
        <td data-actions>${actionBar([{ label: "Delete", icon: "trash", run: `vmIsoDelete(${jsq(v.name)})`, need: "admin", danger: true }], { shown: 0, label: `Actions for ${v.file || v.name}` })}</td></tr>`).join(""))}</div>` : ""}
    <div class="iso-keep"><label for="iso_keep">Remove copies no VM uses after</label>
      <input id="iso_keep" type="number" min="0" max="365" value="${esc(lib.keep_days ?? 7)}" ${can("admin") ? "" : "disabled"}> <span class="dim small">days (0 keeps them)</span>
      <button class="btn sm" data-need="admin" onclick="vmIsoKeep()">Save setting</button></div>
    ${UI.actions(UI.cancel("Close") + UI.button("↻ Refresh", "vmIsoLibrary()"))}</div>`;
  if (window.applyRole) applyRole();
};

window.vmIsoKeep = async () => {
  try {
    const r = await api("/api/vm/isos/keep", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ days: +$("#iso_keep").value }) });
    toast(r.detail, "ok");
    vmIsoLibrary();
  } catch (e) { toast(e.message, "bad"); }
};

window.vmIsoPrepare = async (share, path) => {
  try {
    const r = await api("/api/vm/isos/prepare", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ share, path }) });
    toast(r.detail, "ok");
    vmIsoLibrary();
  } catch (e) { toast(e.message, "bad"); }
};

window.vmIsoDelete = async name => {
  if (!(await ask("Delete this ISO's volume? The file on the share is kept, and it can be prepared again.", { title: "Delete ISO volume", ok: "Delete volume", danger: true }))) return;
  try {
    const r = await api("/api/vm/isos/delete", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ name }) });
    toast(r.detail, "ok");
    vmIsoLibrary();
  } catch (e) { toast(e.message, "bad"); }
};

async function vmIsoSaveFolders(folders) {
  const r = await api("/api/vm/isos/folders", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ folders }) });
  toast(r.detail, "ok");
}

window.vmIsoRemoveFolder = async index => {
  const folders = [...(STATE.data.isoLibrary?.folders || [])];
  folders.splice(index, 1);
  try { await vmIsoSaveFolders(folders); vmIsoLibrary(); } catch (e) { toast(e.message, "bad"); }
};

/* Choose a folder: a share, then down through its folders. */
window.vmIsoAddFolder = (share = "", path = "") => {
  const shares = STATE.data.isoLibrary?.shares || [];
  if (!shares.length) return toast("Create a network share first", "bad");
  share = share || shares[0].name;
  childModal("Add an ISO folder", `<div class="ui-stack"><div class="f"><label>Share</label>
      <select id="iso_share" onchange="vmIsoAddFolderGo(this.value, '')">${shares.map(s => vhOpt(s.name, s.name, share)).join("")}</select></div>
    <div id="iso_browse"><div class="empty small"><span class="spin2"></span> Reading the share…</div></div></div>`);
  vmIsoAddFolderGo(share, path);
};

window.vmIsoAddFolderGo = async (share, path) => {
  const host = $("#iso_browse");
  if (!host) return;
  try {
    const r = await api(`/api/vm/isos/browse?share=${encodeURIComponent(share)}&path=${encodeURIComponent(path)}`);
    const up = path.includes("/") ? path.slice(0, path.lastIndexOf("/")) : "";
    host.innerHTML = `<div class="iso-crumb mono">${esc(share)} / ${esc(path || "")}</div>
      <div class="iso-dirs">${path ? `<button class="btn sm" onclick="vmIsoAddFolderGo(${jsq(share)},${jsq(up)})">↑ Up</button>` : ""}
        ${r.folders.map(d => `<button class="btn sm" onclick="vmIsoAddFolderGo(${jsq(share)},${jsq(path ? `${path}/${d}` : d)})">${esc(d)}</button>`).join("") || '<span class="dim xs">No folders inside</span>'}</div>
      <div class="dim xs">${r.isos} .iso file${r.isos === 1 ? "" : "s"} here</div>
      ${UI.actions(UI.button("Back", "modalBack()", { attrs: 'data-dialog-dismiss="true"' }) + UI.button("Use this folder", `vmIsoPickFolder(${jsArg(share)},${jsArg(path)})`, { kind: "pri" }))}`;
  } catch (e) { host.innerHTML = UI.callout("bad", "That folder could not be read.", esc(e.message)); }
};

window.vmIsoPickFolder = async (share, path) => {
  const folders = [...(STATE.data.isoLibrary?.folders || []), { share, path }];
  try { await vmIsoSaveFolders(folders); modalBack(); vmIsoLibrary(); } catch (e) { toast(e.message, "bad"); }
};
