/* Edit / move containers, node power actions, VMs, images, schedules, import */

/* ---------------- edit a pod and its containers ---------------- */
const editEnvRow = (index, key = "", value = "") => `<div class="f3 e-env-row">
  <div><label>Key</label><input class="ek" type="text" value="${esc(key)}"></div>
  <div><label>Value</label><input class="ev" type="text" value="${esc(value)}"></div>
  <button class="btn sm danger row-remove" type="button" onclick="this.parentNode.remove()">✕</button></div>`;

const editPortRow = (index, port = {}) => `<div class="edit-port-row">
  <div><label>Name</label><input class="ep-name" type="text" value="${esc(port.name || "")}" placeholder="web"></div>
  <div><label>Container port</label><input class="ep-number" type="number" min="1" max="65535" value="${esc(port.container || "")}" placeholder="8080"></div>
  <div><label>LAN port</label><input class="ep-host" type="number" min="1" max="65535" value="${esc(port.host || port.container || "")}" placeholder="8080" oninput="editPortsChanged()"></div>
  <div><label>Protocol</label><select class="ep-protocol">${["TCP", "UDP", "SCTP"].map(value => `<option ${value === (port.protocol || "TCP") ? "selected" : ""}>${value}</option>`).join("")}</select></div>
  <label class="switch"><input class="ep-expose" type="checkbox" ${port.expose ? "checked" : ""} onchange="editPortsChanged()">Expose</label>
  <button class="btn sm danger row-remove" type="button" onclick="this.parentNode.remove();editPortsChanged()">✕</button></div>`;

/* Live namespace storage inventory for the editor's unified volume picker. */
let EDIT_STORAGE = { pvcs: [], storage_classes: [], pod_volumes: [] };
const editVolumeRow = mount => ({
  path: mount.path || "",
  kind: ["existing", "host", "ephemeral", "memory"].includes(mount.kind) ? mount.kind : "existing",
  source: ["ephemeral", "memory"].includes(mount.kind) ? "" : (mount.value || mount.source || ""),
  // value carries the size limit for a RAM volume, e.g. "1024Mi".
  size_gb: mount.kind === "memory" ? (parseInt(mount.value, 10) || 1024) : undefined,
  read_only: !!mount.read_only, volume_name: mount.name || "", sub_path: mount.sub_path || "",
  origin: mount.kind === "existing" ? { claim: mount.value || mount.source || "", sub_path: mount.sub_path || "" } : null,
});
const editVolumePicker = index => createVolumePicker($("#e_vols_" + index), {
  pvcs: () => EDIT_STORAGE.pvcs,
  storageClasses: () => EDIT_STORAGE.storage_classes,
  sharedStorageClasses: () => EDIT_STORAGE.shared_storage_classes || EDIT_STORAGE.storage_classes,
  classFacts: () => EDIT_STORAGE.storage_class_facts || {},
  targetNode: () => $("#e_node")?.value || EDIT_STORAGE.node || "",
  podVolumes: () => EDIT_STORAGE.pod_volumes,
  allowPod: () => EDIT_STORAGE.pod_volumes.length > 0,
  podLabel: "Existing volume in this pod",
  podEmpty: "Choose a volume already in this pod…",
  podUnavailable: "No reusable volumes in this pod",
  podHelp: "Mounts a volume already defined in this pod, sharing that storage with the other container.",
});

const editContainerPanel = (container, index) => {
  const env = Object.entries(container.env || {});
  const refs = container.env_refs || [];
  const ports = container.ports || [];
  const volumes = container.volumes || [];
  const managed = volumes.filter(volume => volume.managed);
  const mounts = volumes.filter(volume => !volume.managed);
  return `<details class="edit-container card flat" data-index="${index}" data-original-name="${esc(container.original_name || container.name)}" ${index === 0 ? "open" : ""}>
    <summary><span class="edit-container-chevron">›</span><span class="edit-container-title"><b>${esc(container.name)}</b><small class="mono">${esc(container.image)}</small></span><span class="pill">${env.length + refs.length} vars</span><span class="pill">${ports.length} ports</span><span class="pill">${mounts.length} mounts</span></summary>
    <div class="edit-container-body">
      <div class="f2">
        <div class="f"><label>Container name ${tip("The Kubernetes name of this container inside the pod. Each container name must be unique.")}</label><input id="e_container_name_${index}" type="text" value="${esc(container.name)}"></div>
        <div class="f"><label>Image</label><input id="e_image_${index}" type="text" value="${esc(container.image)}"></div>
      </div>
      <div class="f2">
        <div class="f"><label>CPU reserved ${tip("Guaranteed scheduling capacity. 1000m = one core; it is not a hard usage limit.")}</label><input id="e_cpu_${index}" type="text" value="${esc(container.cpu || "")}" placeholder="50m"></div>
        <div class="f"><label>Memory reserved ${tip("Guaranteed scheduling capacity in Mi or Gi; it is not a hard usage limit.")}</label><input id="e_mem_${index}" type="text" value="${esc(container.memory || "")}" placeholder="128Mi"></div>
      </div>
      <div class="subsec">Hardware passed to this container</div>
      <div class="hwchoices">${hardwareChoices(`e_hw_${index}`, container.hardware || [])}</div>
      <div class="subsec">Privileges</div>
      ${privilegeFields(`e_pv_${index}`, container.privileges || {})}
      <div class="subsec">Environment</div>
      ${refs.length ? `<div class="managed-env-list">${refs.map(ref => `<div><span class="mono">${esc(ref.name)}</span><span>${esc(ref.source)}</span><span class="pill info">managed reference</span></div>`).join("")}</div><div class="dim xs managed-env-note">References remain connected to Kubernetes and are not exposed or replaced when you save.</div>` : ""}
      <div class="e-env" id="e_env_${index}">${(env.length ? env : [["", ""]]).map(([key, value]) => editEnvRow(index, key, value)).join("")}</div>
      <button class="btn sm" type="button" onclick="editAddEnv(${index})">＋ add variable</button>
      <div class="subsec">Ports ${tip("Container port is where the process listens inside the container. LAN port is the number clients use on the Service address; unexposed ports stay inside the cluster.")}</div>
      <div class="e-ports" id="e_ports_${index}">${ports.map(port => editPortRow(index, port)).join("")}</div>
      <button class="btn sm" type="button" onclick="editAddPort(${index})">＋ add port</button>
      <div class="subsec">Storage</div>
      <div class="note storage-guide"><b>Choose deliberately:</b> RWO is best for one workload; RWX permits multi-node sharing; an existing PVC keeps its current data; a volume already in this pod shares the exact backing storage with another container. Host paths reduce failover portability. Saving creates any new claim, then rolls the pod.</div>
      ${managed.length ? `<div class="edit-mount-list">${managed.map(volume => `<span class="tag info">${esc(volume.source || "?")} → ${esc(volume.path)}${volume.read_only ? " · read-only" : ""}</span>`).join("")}</div><div class="dim xs edit-mount-note">ConfigMap, Secret and hardware device mounts are managed by Homestead and stay as they are.</div>` : ""}
      <div class="e-vols" id="e_vols_${index}"></div>
      <button class="btn sm" type="button" onclick="editAddVol(${index})">＋ add storage mapping</button>
    </div>
  </details>`;
};

/* ---------------- placement rules ----------------
   Keep this workload's instances on different nodes, or run it with or apart
   from other workloads. Each rule is a preference or a requirement. */
let EDIT_PLACEMENT = { others: [], nodes: 0 };
const nodes0 = list => (list || []).filter(n => n.schedulable !== false);
const PLACEMENT_MODES = [["prefer", "prefer"], ["require", "require"]];
function placementRow(kind, row = {}) {
  const others = EDIT_PLACEMENT.others || [];
  const key = row.name ? `${row.ns || EDIT_PLACEMENT.ns}/${row.name}` : "";
  const known = !key || others.some(w => `${w.ns}/${w.name}` === key);
  return `<div class="aff-row" data-kind="${kind}">
    <select class="aff-target"><option value="">choose a workload…</option>${known ? "" : `<option value="${esc(key)}" selected>${esc(key)} (gone)</option>`}
      ${others.map(w => `<option value="${esc(w.ns)}/${esc(w.name)}" ${`${w.ns}/${w.name}` === key ? "selected" : ""}>${esc(w.name)}${w.ns !== EDIT_PLACEMENT.ns ? ` · ${esc(w.ns)}` : ""}</option>`).join("")}</select>
    <select class="aff-mode">${PLACEMENT_MODES.map(([value, label]) => `<option value="${value}" ${row.mode === value ? "selected" : ""}>${label}</option>`).join("")}</select>
    <button class="iconbtn row-remove" type="button" title="Remove this rule" onclick="this.closest('.aff-row').remove();placementChanged()">×</button></div>`;
}
window.editLanToggle = async () => {
  const box = $("#e_lan_box");
  box.hidden = !$("#e_lan_on").checked;
  if (!box.hidden && !box.dataset.filled) {
    let current = null;
    try { current = JSON.parse(box.dataset.current || "null"); } catch (e) { /* none */ }
    box.innerHTML = await containerLanFields("el", current);
    box.dataset.filled = "1";
    if ($("#el_subnet")) vmSubnetPicked("el");
    if (current?.address) { $("#el_ip").value = current.address; $("#el_prefix").value = current.prefix || 24; $("#el_gw").value = current.gateway || ""; }
  }
};

/* What a container does when the node it runs on stops answering. */
const FAILOVER_WORDS = { move: "Move to another node", wait: "Wait for its node", default: "Kubernetes default (5 min)" };
const FAILOVER_HELP = {
  move: "About 15 seconds after its node stops answering, it starts on another node - for apps that should come back quickly wherever there is room.",
  wait: "It stays with its node and starts again when that node is back - for apps tied to that host's hardware, or better restarted where they were.",
  default: "Kubernetes moves it after five minutes, the default for anything Homestead did not deploy.",
};
function failoverSelect(id, current, extra = "") {
  return `<select id="${id}" ${extra}>${Object.entries(FAILOVER_WORDS).map(([value, label]) =>
    `<option value="${value}" ${value === current ? "selected" : ""}>${label}</option>`).join("")}</select>`;
}
window.FAILOVER_WORDS = FAILOVER_WORDS;
window.failoverSelect = failoverSelect;

/* Where it runs, at the three levels there are: the containers in one pod
   (always together - that is what a pod is), the copies of this workload,
   and other workloads. */
function placementSection(p, w, nodes, containers) {
  const names = (containers || []).map(c => c.name);
  return `<section class="placement card flat" id="e_placement">
    <div class="sec" style="margin-top:0">Where it runs</div>
    <div class="place-level"><div class="place-title">Containers in this pod</div>
      <div class="dim small">${names.length > 1 ? `${names.map(n => `<span class="tag">${esc(n)}</span>`).join(" ")} always run together on one node, sharing its network and any pod volumes.`
        : `<span class="tag">${esc(names[0] || w.name)}</span> is this pod's only container.`}
        Containers in one pod cannot be spread apart; to run one on its own node, make it a workload of its own and use the rules below.</div></div>
    <div class="place-level"><div class="place-title">Copies of this workload</div>
      <div class="place-grid">
        <div class="f"><label>Copies ${tip("How many copies of this workload run at once - Kubernetes calls them replicas. Most homelab apps want one; Longhorn volume replicas are a separate, storage-level idea.")}</label><input type="number" id="e_rep" value="${w.start_replicas ?? w.replicas ?? 1}" min="1" max="5" ${w.autostart === false ? "disabled" : ""}></div>
        <div class="f"><label>Spread copies ${tip("Puts this workload's copies on different nodes, so losing one host does not take every copy. Only matters with more than one copy.")}</label>
          <select id="e_spread" onchange="placementChanged()"><option value="">no preference</option>
            <option value="prefer" ${p.spread === "prefer" ? "selected" : ""}>prefer different nodes</option>
            <option value="require" ${p.spread === "require" ? "selected" : ""}>require different nodes</option></select></div>
        <div class="f"><label>Preferred node ${tip("A preference guides placement but still allows failover. Use Move for hardware-aware choices and optional hard pinning.")}</label><select id="e_node" data-current="${esc(w.node || "")}">
          <option value="">any node</option>
          ${(nodes || []).map(n => `<option value="${esc(n.name)}" ${n.name === w.node ? "selected" : ""}>${esc(n.name)}</option>`).join("")}
        </select></div></div></div>
    <div class="place-level"><div class="place-title">Other workloads</div>
      <div class="place-grid two">
        <div class="f"><label>Run on the same node as ${tip("For apps that talk constantly or share a device. Required: this does not start until that workload is running, and only on its node.")}</label>
          <div id="e_with">${(p.with || []).map(row => placementRow("with", row)).join("")}</div>
          <button class="btn sm" type="button" onclick="placementAdd('with')">＋ Add</button></div>
        <div class="f"><label>Keep off the node of ${tip("For pairs that should never share a host: two DNS servers, or two apps that would compete for one disk.")}</label>
          <div id="e_apart">${(p.apart || []).map(row => placementRow("apart", row)).join("")}</div>
          <button class="btn sm" type="button" onclick="placementAdd('apart')">＋ Add</button></div></div></div>
    <div class="place-level"><div class="place-title">Its own LAN address</div>
      <label class="switch"><input type="checkbox" id="e_lan_on" ${w.lan ? "checked" : ""} onchange="editLanToggle()"> An address of its own on the LAN, beside the pod network</label>
      <div id="e_lan_box" ${w.lan ? "" : "hidden"} data-current="${esc(JSON.stringify(w.lan || null))}"></div></div>
    <div class="place-level"><div class="place-title">If its node fails</div>
      <div class="place-grid"><div class="f">${failoverSelect("e_failover", w.failover || "default")}</div>
        <div class="dim small">${esc(FAILOVER_HELP[w.failover || "default"])}</div></div></div>
    <div class="note" id="e_place_note" hidden></div></section>`;
}
/* From a container's menu: the editor, opened at its placement. */
window.wlPlacement = async (ns, name) => {
  await wlEdit(ns, name);
  setTimeout(() => {
    const box = $(".modalbox"), section = $("#e_placement");
    // Clear of the dialog's own header, which stays at the top as it scrolls.
    if (box && section) box.scrollTo({ top: section.getBoundingClientRect().top - box.getBoundingClientRect().top + box.scrollTop - 72, behavior: "smooth" });
  }, 80);
};
window.placementAdd = kind => { $("#e_" + kind).insertAdjacentHTML("beforeend", placementRow(kind)); placementChanged(); };
function readPlacement() {
  const rows = kind => $$(`#e_${kind} .aff-row`).map(row => {
    const [ns, name] = ($(".aff-target", row).value || "/").split("/");
    return { ns, name, mode: $(".aff-mode", row).value };
  }).filter(row => row.name);
  return { spread: $("#e_spread")?.value || "", with: rows("with"), apart: rows("apart") };
}
window.placementChanged = () => {
  const note = $("#e_place_note");
  if (!note) return;
  const p = readPlacement(), replicas = +$("#e_rep")?.value || 1, warnings = [];
  if (p.spread === "require" && replicas > EDIT_PLACEMENT.nodes)
    warnings.push(`${replicas} instances but ${EDIT_PLACEMENT.nodes} schedulable node${EDIT_PLACEMENT.nodes === 1 ? "" : "s"}: the extra instances stay pending.`);
  const singleNode = readVolumeRows($("#e_containers")).some(v => v.kind === "new-rwo" || (v.kind === "existing" &&
    ((EDIT_STORAGE.pvcs || []).find(c => c.name === v.source)?.access_modes || []).includes("ReadWriteOnce")));
  if (p.spread && replicas > 1 && singleNode)
    warnings.push("A single-node (RWO) volume attaches to one node at a time, so instances spread across nodes cannot all mount it.");
  const names = [...p.with, ...p.apart].map(row => `${row.ns}/${row.name}`);
  if (names.length !== new Set(names).size) warnings.push("A workload is named twice; each can be kept either with this one or apart from it.");
  if (p.with.some(row => row.mode === "require")) warnings.push("Required: this workload waits for the one it runs with, and stops being schedulable if that one stops.");
  note.hidden = !warnings.length;
  note.innerHTML = warnings.map(esc).join("<br>");
};
document.addEventListener("change", event => { if (event.target.closest(".placement") || event.target.id === "e_rep") placementChanged(); });

window.wlEdit = async (ns, name, fromRoute = false) => {
  if (!fromRoute && window.setModalRoute) setModalRoute({ panel: "edit", ns, workload: name }, name);
  modal("Edit · " + name, `<div class="empty"><span class="spin2"></span>loading</div>`, true);
  try {
    const [w, liveNodes, options, others] = await Promise.all([
      api(`/api/workload?ns=${encodeURIComponent(ns)}&name=${encodeURIComponent(name)}`),
      loadHardwareFeatures().then(() => api("/api/nodes").catch(() => [])),
      api(`/api/deploy/options?ns=${encodeURIComponent(ns)}`).catch(() => ({})),
      STATE.data.wl ? Promise.resolve(STATE.data.wl) : api("/api/workloads").catch(() => []),
    ]);
    EDIT_PLACEMENT = { ns, name, others: (others || []).filter(x => !(x.ns === ns && x.name === name)),
      nodes: nodes0(liveNodes).length };
    if (liveNodes.length) STATE.data.nodes = liveNodes;
    const nodes = (liveNodes.length ? liveNodes : (STATE.data.ov ? STATE.data.ov.nodes : [])).filter(n => n.schedulable !== false);
    const seeds = w.seed_configs || [];
    const containers = w.containers || [{ original_name: w.container_name || w.name, name: w.container_name || w.name,
      image: w.image, cpu: w.cpu, memory: w.memory, env: w.env || {}, env_refs: [], ports: w.ports || [],
      hardware: w.hardware || [], volumes: w.volumes || [] }];
    EDIT_STORAGE = { pvcs: options.pvcs || [], storage_classes: options.storage_classes || [],
      shared_storage_classes: options.shared_storage_classes || [],
      storage_class_facts: options.storage_class_facts || {}, pod_volumes: w.pod_volumes || [],
      node: w.node || "" };
    $("#mbody").innerHTML = `
      <div class="f"><label>Workload name ${tip("The real Kubernetes Deployment name. Renaming creates a replacement Deployment, waits for it to become ready, then removes the old one. Generated pods use this name plus a Kubernetes suffix.")}</label><input type="text" id="e_workload_name" value="${esc(w.name)}"></div>
      <div class="f"><label>Pod hostname ${tip("The hostname visible inside the pod. It does not rename the Kubernetes Pod; generated pods use the workload name plus a suffix.")}</label><input type="text" id="e_pod_name" value="${esc(w.pod_hostname || "")}" placeholder="optional"></div>
      <div class="f"><label>Container logo ${tip("Optional public HTTPS image URL. Homestead validates it and keeps a persistent local copy while retaining this source for later edits.")}</label><input type="url" id="e_icon" value="${esc(w.icon || "")}" placeholder="https://…/icon.png"></div>
      ${placementSection(w.placement || {}, w, nodes, containers)}
      <label class="switch" id="e_autostart_wrap"><input type="checkbox" id="e_autostart" onchange="editAutostartToggle()" ${w.autostart === false ? "" : "checked"}>
        Autostart ${tip("On keeps the workload running: Kubernetes restarts it after a crash, a node reboot or a cluster restart. Off scales it to zero and remembers the instance count for when you switch it back on.")}</label>
      <div class="dim xs" id="e_autostart_note" style="margin:-4px 0 6px">${w.autostart === false ? "Stays stopped until you switch autostart back on." : "Runs continuously and comes back after a reboot."}</div>
      <div class="sec">Containers <span class="pill">${containers.length}</span></div>
      <div id="e_containers">${containers.map(editContainerPanel).join("")}</div>
      <div class="note" id="e_ports_note" hidden></div>
      ${seeds.length ? `<div class="sec">Startup seed config ${tip("This ConfigMap is copied into the container's persistent storage by an init container before every start. It is authoritative: editing only the mounted file will be overwritten on restart.")}</div>
        <div class="note seed-note"><b>Authoritative startup configuration.</b> Saving here updates the ConfigMap and restarts the workload so the init container copies the new value into appdata.</div>
        ${seeds.map((s, i) => `<div class="seed-editor card flat">
          <div class="between seed-head"><div><b>${esc(s.key)}</b><div class="dim xs mono">ConfigMap ${esc(s.config_map)} · init ${esc(s.init_container)}</div></div><span class="pill info">seeded on start</span></div>
          <label for="e_seed_${i}">Contents</label>
          <textarea id="e_seed_${i}" class="e_seed mono" rows="14"
            data-init="${esc(s.init_container)}" data-config-map="${esc(s.config_map)}" data-key="${esc(s.key)}">${esc(s.value)}</textarea>
          ${s.command ? `<div class="dim xs mono seed-command">${esc(s.command)}</div>` : ""}
        </div>`).join("")}` : ""}
      <div class="row" style="margin-top:22px">
        <button class="btn pri" id="e_save" onclick="editSave('${esc(ns)}','${esc(name)}')">Save &amp; restart</button>
        <button class="btn" onclick="closeModal()">Cancel</button>
      </div>
      <div class="note" style="margin-top:14px">Saving rolls the pod. Renaming the workload performs a guarded stop, recreate and readiness check. With a ReadWriteOnce volume the old pod must fully stop before the renamed one starts, so expect a short outage. A failed rename restores the original Deployment.</div>`;
    containers.forEach((container, index) => renderVolumeRows(editVolumePicker(index),
      (container.volumes || []).filter(volume => !volume.managed).map(editVolumeRow)));
    window.__editHadService = !!w.has_service;
    editPortsChanged();
    placementChanged();
    if ($("#e_lan_on")?.checked) editLanToggle();
  } catch (e) { $("#mbody").innerHTML = `<div class="empty">${esc(e.message)}</div>`; }
};
window.editAddEnv = (index, key = "", value = "") => $("#e_env_" + index).insertAdjacentHTML("beforeend", editEnvRow(index, key, value));
window.editAddPort = (index, port = {}) => {
  $("#e_ports_" + index).insertAdjacentHTML("beforeend", editPortRow(index, port));
  editPortsChanged();
};
window.editAddVol = (index, volume = {}) => addVolumeRow(editVolumePicker(index), volume);
window.editPortsChanged = () => {
  const note = $("#e_ports_note");
  if (!note) return;
  const rows = $$("#e_containers .edit-port-row");
  const exposed = rows.filter(row => $(".ep-expose", row).checked);
  const remapped = exposed.filter(row => +$(".ep-host", row).value !== +$(".ep-number", row).value);
  $$(".ep-host").forEach(input => { input.disabled = !$(".ep-expose", input.closest(".edit-port-row")).checked; });
  if (!exposed.length && window.__editHadService) {
    note.hidden = false;
    note.innerHTML = "<b>No port is exposed.</b> Saving removes this workload's Service, so its LAN address is released and clients lose the listener.";
  } else if (remapped.length) {
    note.hidden = false;
    note.innerHTML = `<b>LAN listeners change on save:</b> ${remapped.map(row =>
      `<span class="mono">${+$(".ep-host", row).value} → ${+$(".ep-number", row).value}</span>`).join(", ")}. Existing bookmarks on the old port stop working.`;
  } else {
    note.hidden = true;
  }
};
window.editAutostartToggle = () => {
  const on = $("#e_autostart").checked;
  $("#e_rep").disabled = !on;
  $("#e_autostart_note").textContent = on
    ? "Runs continuously and comes back after a reboot."
    : "Stays stopped until you switch autostart back on.";
};
window.editSave = async (ns, name) => {
  const containers = $$("#e_containers .edit-container").map(panel => {
    const index = panel.dataset.index;
    const env = {};
    $$(".e-env-row", panel).forEach(row => { const key = $(".ek", row).value.trim(); if (key) env[key] = $(".ev", row).value; });
    const ports = $$(".edit-port-row", panel).map(row => ({ name: $(".ep-name", row).value.trim(),
      container: +$(".ep-number", row).value, protocol: $(".ep-protocol", row).value,
      host: +$(".ep-host", row).value || +$(".ep-number", row).value,
      expose: $(".ep-expose", row).checked })).filter(port => port.container);
    return { original_name: panel.dataset.originalName, name: $("#e_container_name_" + index).value.trim(),
      image: $("#e_image_" + index).value.trim(), cpu: $("#e_cpu_" + index).value.trim(),
      memory: $("#e_mem_" + index).value.trim(), hardware: selectedHardware("e_hw_" + index), env, ports,
      privileges: readPrivileges("e_pv_" + index) || undefined,
      volumes: readVolumeRows($("#e_vols_" + index)) };
  });
  const storageIssue = containers.map(container => volumeListIssue(container.volumes)).find(Boolean);
  if (storageIssue) return toast(storageIssue, "bad");
  const seed_configs = $$("#mbody .e_seed").map(el => ({
    init_container: el.dataset.init, config_map: el.dataset.configMap,
    key: el.dataset.key, value: el.value,
  }));
  const workloadName = $("#e_workload_name").value.trim();
  const renaming = workloadName !== name;
  if (renaming && !confirm(`Rename Kubernetes Deployment “${name}” to “${workloadName}”?\n\nHomestead will stop the old workload, start the renamed one, wait for readiness, and restore the original if startup fails. Expect a short outage.`)) return;
  const body = { ns, name, workload_name: workloadName, pod_hostname: $("#e_pod_name").value.trim(),
    icon: $("#e_icon").value.trim(), replicas: Math.max(1, +$("#e_rep").value || 1),
    autostart: $("#e_autostart").checked, manage_ports: true, containers, seed_configs,
    placement: readPlacement(), failover: $("#e_failover")?.value || "" };
  if ($("#e_lan_on")) body.lan = $("#e_lan_on").checked && $("#el_ip") ? containerLanRead("el") : null;
  const moves = containers.flatMap(container => container.volumes.filter(volume => volume.copy_from));
  if (moves.length && renaming) return toast("Rename the workload and move its data in separate saves", "bad");
  if ((STATE.data.wl || []).some(x => x.self && x.ns === ns && x.name === name) && !body.autostart) {
    if (!confirm(`Turning autostart off stops Homestead, and this page with it. Nothing here can start it again - it stays down until someone runs\n\n  kubectl -n ${ns} scale deployment/${name} --replicas=1\n\non the cluster. Stop it anyway?`)) return;
    body.confirm_self = true;
  }
  const where = (claim, folder) => folder ? `${claim}/${folder}` : claim;
  if (moves.length && !confirm(`Copy ${moves.length} location${moves.length === 1 ? "" : "s"} to new storage?\n\n` +
      moves.map(volume => `${volume.path}: ${where(volume.copy_from.claim, volume.copy_from.sub_path)} → ${where(volume.source, volume.sub_path)}`).join("\n") +
      `\n\n${name} stops, the data is copied, and it starts again. The old volumes are kept; delete them from Volumes once you have checked.`)) return;
  const button = $("#e_save");
  button.disabled = true;
  button.textContent = renaming ? "Renaming & checking readiness…"
    : body.autostart ? "Saving & restarting…" : "Saving & stopping…";
  try {
    const result = await api("/api/edit", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
    const activeName = result.name || workloadName || name;
    const nodeSelect = $("#e_node");
    const node = nodeSelect.value;
    if (node !== (nodeSelect.dataset.current || "")) {
      await api("/api/move", { method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ ns, name: activeName, node: node || null }) });
    }
    toast(result.operation ? `${activeName} saved; copying its data in the job tray` :
      result.network || (renaming ? `${name} renamed to ${activeName}` : `${activeName} updated`), "ok");
    closeModal(); setTimeout(() => refresh(true), 1200);
  } catch (e) {
    toast(e.message, "bad");
    button.disabled = false;
    button.textContent = "Save & restart";
  }
};

/* ---------------- move a container ---------------- */
window.wlMoveLegacy = async (ns, name) => {
  const nodes = (STATE.data.ov ? STATE.data.ov.nodes : []);
  modal("Move · " + name, `
    <p class="muted small">Pick the host this workload should run on. Homestead pins it with a
    node selector and rolls the pod.</p>
    <div class="f" style="margin-top:14px"><label>Target host</label><select id="mv_node">
      <option value="">any node (unpin)</option>
      ${nodes.map(n => `<option value="${esc(n.name)}">${esc(n.name)} · ${n.cpu_pct}% cpu, ${n.mem_pct}% ram${n.igpu ? " · iGPU" : ""}</option>`).join("")}
    </select></div>
    <div class="row" style="margin-top:18px">
      <button class="btn pri" onclick="doMove('${esc(ns)}','${esc(name)}')">Move</button>
      <button class="btn" onclick="closeModal()">Cancel</button></div>
    <div class="note" style="margin-top:14px">This is a stop-then-start, not a live move —
    a ReadWriteOnce volume can only attach to one node at a time.</div>`);
};
window.doMove = async (ns, name) => {
  try {
    const node = $("#mv_node").value;
    await api("/api/move", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ ns, name, node: node || null }) });
    toast(`${name} → ${node || "any node"}`, "ok"); closeModal(); setTimeout(() => refresh(true), 1400);
  } catch (e) { toast(e.message, "bad"); }
};

/* ---------------- node power ---------------- */
window.nodeActions = async name => {
  let qr = {}, impact = { workloads: [], stranded: [] };
  try { [qr, impact] = await Promise.all([api("/api/quorum"), api(`/api/node/impact?node=${encodeURIComponent(name)}`)]); } catch (e) { }
  window.__nodeImpact = impact;
  const isEtcd = (qr.members || []).includes(name);
  const risky = isEtcd && qr.can_lose < 1;
  const off = qr.power_enabled === false;
  childModal("Host actions · " + name, `
    <div class="grid g2" style="gap:12px">
      <div class="card flat"><div class="ctitle">Scheduling</div>
        <p class="muted small" style="margin:6px 0 14px">Cordon stops new pods landing here.
        Drain evicts the ones already running.</p>
        <div class="row">
          <button class="btn" onclick="nodeCordon('${esc(name)}',true)">Cordon</button>
          <button class="btn" onclick="nodeCordon('${esc(name)}',false)">Uncordon</button>
          <button class="btn" onclick="nodeDrain('${esc(name)}')">Drain</button>
          <button class="btn" onclick="evacuateNode('${esc(name)}')">Move all off</button>
        </div></div>
      <div class="card flat"><div class="ctitle">Quorum</div>
        <div class="drow"><div class="dl">etcd members</div><div class="dv mono">${qr.total || "?"}</div></div>
        <div class="drow"><div class="dl">Ready</div><div class="dv mono">${(qr.ready || []).length}</div></div>
        <div class="drow"><div class="dl">Needs</div><div class="dv mono">${qr.quorum_needs || "?"}</div></div>
        <div class="drow"><div class="dl">Can lose</div><div class="dv mono">
          <span class="pill ${qr.can_lose > 0 ? "ok" : "crit"}">${qr.can_lose ?? "?"}</span></div></div>
        ${isEtcd ? '<div class="dim xs" style="margin-top:8px">This host is an etcd member.</div>' : ""}
      </div>
    </div>
    <div class="sec">Workload dependencies</div>
    ${(impact.workloads || []).length ? `<div class="dependency-list">${impactRows(impact)}</div>` : '<div class="empty small">No user workloads are currently running on this host.</div>'}
    ${(impact.stranded || []).length ? `<div class="note dependency-danger" style="margin-top:12px"><b>Stopping this host strands ${impact.stranded.length} workload${impact.stranded.length === 1 ? "" : "s"}.</b> ${impact.stranded.map(w => `<span class="mono">${esc(w.name)}</span>`).join(", ")} cannot run on any other ready host with the required hardware.</div>` : `<div class="note" style="margin-top:12px">Every current user workload has at least one compatible destination.</div>`}
    <div class="sec">Leaving the cluster</div>
    <div class="row between removal-entry"><p class="muted small">Take this host out for good: Homestead checks quorum and
      volume copies first, then removes it in Harvester's order.</p>
      <button class="btn danger" data-need="admin" onclick="nodeRemoval('${esc(name)}')">Remove from cluster…</button></div>
    <div class="sec">Power</div>
    ${off ? `<div class="note"><b>Host power control is disabled.</b> Rebooting needs a privileged
        helper pod that enters the host namespaces, so it ships off. Set
        <span class="mono">ENABLE_NODE_POWER=true</span> on the Homestead Deployment to enable it.
        Cordon and drain above work regardless.</div>`
      : risky ? `<div class="note" style="border-color:rgba(255,77,79,.35);background:rgba(255,77,79,.08);color:#ffb4b8">
        <b>Blocked.</b> ${(qr.ready || []).length} of ${qr.total} etcd members are ready and quorum needs
        ${qr.quorum_needs}. Taking this host down would lose the cluster. Bring the other members back first.</div>`
      : `<p class="muted small">The host is cordoned and drained first, then a privileged helper pod
         asks systemd. Type the host name to confirm.</p>
      <div class="f" style="margin-top:12px"><label>Type <b class="mono">${esc(name)}</b> to confirm</label>
        <input type="text" id="pw_confirm" placeholder="${esc(name)}" autocomplete="off"></div>
      <label class="switch"><input type="checkbox" id="pw_drain" checked> Drain workloads first (recommended)</label>
      ${(impact.stranded || []).length ? `<label class="switch dependency-confirm"><input type="checkbox" id="pw_allow"> I understand ${impact.stranded.map(w => esc(w.name)).join(", ")} will remain down until compatible hardware is available</label>` : ""}
      <div class="row">
        <button class="btn danger" onclick="nodePower('${esc(name)}','reboot')">Reboot host</button>
        <button class="btn danger" onclick="nodePower('${esc(name)}','poweroff')">Shut down host</button>
      </div>`}`, true);
};
window.nodeCordon = async (node, cordon) => {
  try {
    await api("/api/node/cordon", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ node, cordon }) });
    toast(`${node} ${cordon ? "cordoned" : "uncordoned"}`, "ok"); refresh(true);
  } catch (e) { toast(e.message, "bad"); }
};
window.nodeDrain = async node => {
  evacuateNode(node);
};
window.nodePower = async (node, action) => {
  const c = $("#pw_confirm").value.trim();
  if (c !== node) return toast("type the host name exactly to confirm", "bad");
  if ((window.__nodeImpact?.stranded || []).length && !$("#pw_allow")?.checked)
    return toast("confirm the workloads that will remain down", "bad");
  try {
    const r = await api("/api/node/power", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ node, action, drain: $("#pw_drain").checked, confirm: c,
        allow_stranded: !!$("#pw_allow")?.checked }) });
    modal("Host " + action, `<pre>${esc(r.steps.join("\n"))}</pre>
      <div class="note" style="margin-top:12px">The host will go down shortly. It stays cordoned —
      uncordon it from this dialog once it is back.</div>`);
  } catch (e) { toast(e.message, "bad"); }
};

/* ---------------- VMs: the page is views-vms.js; moving and creating stay here ---------------- */
window.vmMove = (ns, name) => {
  const nodes = STATE.data.nodes || [];
  modal("Migrate · " + name, `
    <p class="muted small">KubeVirt live-migrates the VM — memory is copied while it keeps running,
    so there is no downtime.</p>
    <div class="f" style="margin-top:14px"><label>Target host</label><select id="vm_target">
      <option value="">let KubeVirt choose</option>
      ${nodes.map(n => `<option value="${esc(n.name)}">${esc(n.name)}</option>`).join("")}</select></div>
    <div class="row" style="margin-top:16px">
      <button class="btn pri" onclick="doVmMove('${esc(ns)}','${esc(name)}')">Migrate</button>
      <button class="btn" onclick="closeModal()">Cancel</button></div>`);
};
window.doVmMove = async (ns, name) => {
  try {
    const r = await api("/api/vm/migrate", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ ns, name, target: $("#vm_target").value || null }) });
    toast(`migration ${r.migration} started`, "ok"); closeModal(); setTimeout(() => refresh(true), 2000);
  } catch (e) { toast(e.message, "bad"); }
};
/* ---------------- a VM's own address ----------------
   A VM on the pod network is reached through a Service. One on a VM network
   bridged to the LAN is a machine there like any other, and can be given an
   address of its own: picked from what IP addresses knows is free outside
   the DHCP range, written into cloud-init's network config, and recorded
   under the VM's name so nothing else is given it. */
function vmLanNetworks(opts) {
  return (opts.network_details || []).filter(n => n.lan);
}
function vmNetworkNote(opts) {
  if (vmLanNetworks(opts).length) return "";
  return `<div class="note" style="margin-top:8px"><b>No LAN network yet.</b> ${opts.harvester
    ? `A LAN network puts VMs and containers on your LAN, untagged like the hosts or on a VLAN.
       <button class="btn sm pri" data-need="admin" style="margin-top:6px" onclick="vmNetworkAdd()">＋ Make one</button>`
    : "Create a Multus bridge network attachment on the host bridge your LAN is on; VMs on it get LAN addresses."}</div>`;
}

/* A VM network, made here: the same object Harvester's dashboard makes under
   Networks > VM Networks, so it shows there too. When made from a form that
   needed one, that form opens again with it to choose. */
window.vmNetworkAdd = async (reopen = null) => {
  const opts = window.__vmCreateOptions || await api("/api/vm/create-options").catch(() => ({}));
  const back = reopen || window.__vmNetworkReopen || null;
  const clusters = (opts.vm_network_options || {}).cluster_networks || ["mgmt"];
  (window.childModal && !$("#modal").classList.contains("hidden") ? childModal : modal)("New LAN network", `
    <p class="small" style="margin-top:0">VMs and containers on it are on your LAN - with addresses from your router's DHCP, or ones of their own.</p>
    <div class="f2"><div class="f"><label>Name ${tip("How it is listed wherever a network is chosen, like lan or vlan20.")}</label><input id="vn_name" value="lan"></div>
      <div class="f"><label>Cluster network ${tip("Which of Harvester's cluster networks it rides on. mgmt is the hosts' own network - the usual choice.")}</label>
        <select id="vn_cluster">${clusters.map(c => `<option ${c === "mgmt" ? "selected" : ""}>${esc(c)}</option>`).join("")}</select></div></div>
    <div class="f"><label>VLAN ${tip("Empty: untagged - the same LAN the hosts are on. A number: that VLAN, which your switch must carry to the hosts.")}</label>
      <input id="vn_vlan" type="number" min="1" max="4094" placeholder="empty - untagged, the hosts' own LAN"></div>
    <div class="row" style="margin-top:14px"><button class="btn pri" onclick="vmNetworkAddGo()">Make it</button>
      <button class="btn" onclick="modalBack()">Cancel</button></div>`);
  window.__vmNetworkReopen = back;
};
window.vmNetworkAddGo = async () => {
  try {
    const r = await api("/api/network/vm-networks", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name: $("#vn_name").value.trim(), cluster_network: $("#vn_cluster").value, vlan: $("#vn_vlan").value.trim() }) });
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
      <div class="f"><label>Subnet ${tip("From Networking › IP addresses: the addresses offered are free there, outside the DHCP range and the VIP pools.")}</label>
        <select id="${p}_subnet" onchange="vmSubnetPicked('${p}', ${count})">${subnets.map(s => `<option value="${esc(s.cidr)}">${esc(s.cidr)}${s.name ? ` · ${esc(s.name)}` : ""} · ${s.free.length} free</option>`).join("")}
          <option value="">another - type it in</option></select></div>
      <div class="f"><label>${count > 1 ? "Addresses, one per node" : "Address"}</label>
        <input id="${p}_ip" class="mono" placeholder="${count > 1 ? "192.0.2.60, 192.0.2.61" : "192.0.2.60"}" list="${p}_free">
        <datalist id="${p}_free"></datalist></div></div>
    <div class="f2">
      <div class="f"><label>Prefix length</label><input id="${p}_prefix" type="number" min="8" max="30" value="${first ? esc(first.cidr.split("/")[1]) : 24}"></div>
      <div class="f"><label>Gateway</label><input id="${p}_gw" class="mono" value="${esc(first?.gateway || "")}" placeholder="192.0.2.1"></div></div>
    <div class="f"><label>DNS servers ${tip("Comma-separated. Left empty, the gateway answers DNS.")}</label><input id="${p}_dns" class="mono" placeholder="${esc(first?.gateway || "192.0.2.1")}"></div>
    ${subnets.length ? "" : '<div class="dim xs">IP addresses knows no subnet yet: add yours under Networking › IP addresses to be offered free addresses and have these recorded.</div>'}`;
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
  const net = $("#v_net")?.value || "pod";
  const lan = vmLanNetworks(window.__vmCreateOptions || {}).some(n => n.name === net);
  $("#v_addr_wrap").hidden = !lan;
  $("#v_static").hidden = !lan || $("#v_addr_mode").value !== "static";
};

window.vmNew = async (selectedDisk = "", selectedNamespace = "") => {
  window.__vmNetworkReopen = "vm";
  const [opts, disks] = await Promise.all([
    api("/api/vm/create-options").catch(() => ({ harvester: !!STATE.platform?.harvester, cdi: true, storage_classes: [], images: [] })),
    api("/api/vm-disks").catch(() => []),
  ]);
  // Imports are CDI DataVolumes, so without CDI there are none to attach.
  const readyDisks = opts.cdi ? disks.filter(d => d.phase === "Succeeded" && !d.in_use) : [];
  const selected = selectedDisk ? `disk:${selectedNamespace || "lab"}/${selectedDisk}` : "";
  const images = (opts.images || []).filter(i => i.storage_class);
  const facts = opts.storage_class_facts || {};
  const classLabel = c => `${c}${facts[c]?.default ? " (default)" : ""}${facts[c]?.replicas ? ` · ${facts[c].replicas} copies` : ""}`;
  window.__vmCreateOptions = opts;
  modal("New virtual machine", `
    <div class="f"><label>Name</label><input type="text" id="v_name" placeholder="ubuntu-test"></div>
    <div class="f2">
      <div class="f"><label>CPU cores</label><input type="number" id="v_cores" value="2" min="1" max="16"></div>
      <div class="f"><label>Memory</label><input type="text" id="v_mem" value="2Gi"></div>
    </div>
    <div class="f2">
      <div class="f"><label>Disk (GB)</label><input type="number" id="v_disk" value="20" min="5"></div>
      <div class="f"><label>Root password ${tip("Required when Homestead provisions a new disk. Optional for an imported disk that already has login access configured.")}</label><input type="password" id="v_pass" autocomplete="new-password" placeholder="Set an initial password"></div>
    </div>
    <div class="f"><label>Boot disk ${tip(opts.harvester
      ? "Attach a completed import as it is, start from one of Harvester's images, or download an image while the VM is created."
      : "Attach a completed import as it is, or download an image while the VM is created.")}</label>
      <select id="v_boot" onchange="vmBootChanged()">
        <option value="">blank disk</option>
        ${readyDisks.map(d => `<option value="disk:${esc(d.namespace)}/${esc(d.name)}" ${selected === `disk:${d.namespace}/${d.name}` ? "selected" : ""}>Imported · ${esc(d.namespace)}/${esc(d.name)} (${esc(d.capacity || "size unknown")})</option>`).join("")}
        ${images.map(i => `<option value="image:${esc(i.namespace)}/${esc(i.name)}" data-size="${i.size_gb}">Harvester image · ${esc(i.display)} (${i.size_gb}G)</option>`).join("")}
        ${opts.cdi || opts.harvester ? `<option value="url">${opts.harvester ? "Download from a URL (as a Harvester image)" : "Download from HTTP(S) URL"}</option>` : ""}
      </select></div>
    <div class="f" id="v_url_row" hidden><label>Image URL</label><input type="url" id="v_url" placeholder="https://cloud-images.ubuntu.com/…/img"></div>
    <div class="f"><label>Network ${tip("The pod network: reached through a Service, like a container. A LAN network (bridged): a machine there like any other, with an address from DHCP or one of its own.")}</label>
      <select id="v_net" onchange="vmNetChanged()"><option value="pod">Pod network - reached through a Service</option>
        ${(opts.network_details || []).map(n => `<option value="${esc(n.name)}">${esc(n.name)}${n.lan ? ` · LAN${n.vlan ? ` (VLAN ${esc(n.vlan)})` : ""}` : ""}</option>`).join("")}</select>
      ${vmNetworkNote(opts)}</div>
    <div id="v_addr_wrap" hidden>
      <div class="f"><label>Address</label><select id="v_addr_mode" onchange="vmNetChanged();vmSubnetPicked('v')">
        <option value="dhcp">From the network's DHCP</option><option value="static">One of its own</option></select></div>
      <div id="v_static" hidden>${vmAddressFields("v", opts)}</div></div>
    ${(opts.storage_classes || []).length ? `<div class="f" id="v_sc_row"><label>Storage class ${tip(opts.harvester
      ? "Where a blank or downloaded disk lives. A disk from a Harvester image always lives on that image's own class."
      : "Where the disk lives. The cluster's default class is chosen for you.")}</label>
      <select id="v_sc">${opts.storage_classes.map(c => `<option value="${esc(c)}" ${c === opts.default_class ? "selected" : ""}>${esc(classLabel(c))}</option>`).join("")}</select></div>` : ""}
    <div class="row" style="margin-top:18px">
      <button class="btn pri" onclick="doVmCreate()">Create VM</button>
      <button class="btn" onclick="closeModal()">Cancel</button></div>
    <div class="note" style="margin-top:14px">${opts.harvester
      ? "New disks are made the way Harvester makes them: shared block volumes, so the VM can move between hosts. A URL is downloaded as a Harvester image, kept in its image list for the next VM."
      : opts.cdi ? `New disks are CDI DataVolumes on the class above, with the access mode that class supports.
        A VM on a disk only one host can reach stays on that host.`
      : `<b>CDI is not installed</b>, so a VM here starts from a blank disk that KubeVirt formats itself.
        To download or import disk images, install CDI (the containerized data importer) from kubevirt.io.`}
    ${readyDisks.length ? " Imported disks are attached directly and remain visible on the Import page." : ""}</div>`, true);
  vmBootChanged();
};
window.vmBootChanged = () => {
  const boot = $("#v_boot")?.value || "";
  if ($("#v_url_row")) $("#v_url_row").hidden = boot !== "url";
  // An import brings its own disk; a Harvester image brings its own class.
  if ($("#v_sc_row")) $("#v_sc_row").hidden = boot.startsWith("disk:") || boot.startsWith("image:");
  const size = +($("#v_boot")?.selectedOptions[0]?.dataset.size || 0);
  if (size && +$("#v_disk").value < Math.ceil(size)) $("#v_disk").value = Math.ceil(size);
};
window.doVmCreate = async () => {
  const boot = $("#v_boot").value;
  const imported = boot.startsWith("disk:") ? boot.slice(5).split("/") : [];
  const body = { name: $("#v_name").value.trim(), cores: +$("#v_cores").value,
    memory: $("#v_mem").value.trim(), disk_gb: +$("#v_disk").value, password: $("#v_pass").value,
    namespace: imported[0] || "lab", disk_import: imported[1] || "",
    image_id: boot.startsWith("image:") ? boot.slice(6) : "",
    image_url: boot === "url" ? $("#v_url").value.trim() : "",
    storage_class: $("#v_sc")?.value || "", network: $("#v_net")?.value || "pod" };
  if (body.network !== "pod" && $("#v_addr_mode")?.value === "static") {
    body.static_ip = Object.assign(vmReadAddress("v"), { address: $("#v_ip").value.trim() });
    if (!body.static_ip.address) return toast("give the VM its address", "bad");
  }
  if (!body.name) return toast("name is required", "bad");
  if (!body.disk_import && body.password.length < 10) return toast("root password must be at least 10 characters", "bad");
  if (boot === "url" && !body.image_url) return toast("image URL is required", "bad");
  if (boot === "url" && !/^https?:\/\/[^/\s]+/i.test(body.image_url)) {
    // A file name here is usually a Harvester image, which is picked from the list instead.
    const image = (window.__vmCreateOptions?.images || []).find(i => i.display === body.image_url || i.name === body.image_url);
    return toast(image ? `${image.display} is a Harvester image: choose it under Boot disk instead of a URL`
      : "the image URL must start with http:// or https://", "bad");
  }
  try {
    const r = await api("/api/vm/create", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
    toast(r.address ? `${body.name} created at ${r.address}, recorded in IP addresses` : `${body.name} created`, "ok"); closeModal(); go("vms");
  } catch (e) { toast(e.message, "bad"); }
};

/* ---------------- image cache ---------------- */
async function viewImages() {
  const d = await api("/api/images");
  STATE.data.imageCache = d;
  // Kubernetes lists only each node's largest images; the full list is
  // containerd's, asked for by a scan - started here at most every 15 minutes.
  if (!d.complete && !(d.scanning || []).length && can("admin")
      && Date.now() - (window.__imageScanAt || 0) > 15 * 60 * 1000) {
    window.__imageScanAt = Date.now();
    api("/api/images/scan", { method: "POST", headers: { "Content-Type": "application/json" }, body: "{}" })
      .then(() => setTimeout(() => { if (STATE.view === "images") viewImages(); }, 6000)).catch(() => {});
  }
  if ((d.scanning || []).length) setTimeout(() => { if (STATE.view === "images") viewImages(); }, 4000);
  const q = STATE.q.toLowerCase();
  const core = n => /(^|\/)(rancher|harvester|longhornio|kubevirt|cdi-|cilium|kube-|metrics-server|registry\.k8s\.io|pause|traefik|fleet|system-upgrade|k8snetworkplumbingwg|multus|whereabouts|kubeovn|calico|canal|flannel|coredns|etcd|rke2|neuvector|suse\/sles\/)/i.test(n);
  const all = d.images.filter(i => !q || i.name.toLowerCase().includes(q));
  const hidden = all.filter(i => core(i.name)).length;
  const imgs = all.filter(i => STATE.showCoreImages || !core(i.name));
  paint(`<div class="phead"><div><h2>Image cache</h2>
      <p>${imgs.length} app images across ${d.nodes.length} nodes · ${d.protected || 0} active/rollback retained · ${hidden && !STATE.showCoreImages ? `${hidden} Harvester/system images hidden` : `${d.distinct} total`} ${tip("Kubernetes reports each node's largest cached images. Smaller cache entries may not appear and cannot be removed here.")}</p></div>
      <label class="switch"><input type="checkbox" ${STATE.showCoreImages ? "checked" : ""} onchange="STATE.showCoreImages=this.checked;viewImages()"> Show Harvester/system images</label></div>
    <div class="grid g3 statgrid" style="margin-bottom:18px">
      ${d.nodes.map(n => `<div class="card flat"><div class="ctitle" title="${esc(n.node)}">${esc(n.node)}</div>
        <div class="bignum" style="margin-top:8px">${n.total_gb}<span class="unit">GB</span></div>
        <div class="csub">${n.count} images cached</div></div>`).join("")}
    </div>
    ${(d.scanning || []).length ? `<div class="note" style="margin-bottom:12px"><span class="spin2"></span> Asking containerd on ${d.scanning.length} node${d.scanning.length === 1 ? "" : "s"} for every image it holds…</div>`
      : !d.complete ? `<div class="note warn between" style="margin-bottom:12px"><span>Kubernetes reports only each node's largest images, so some - running ones included - are missing here.
          A scan asks each node's containerd for all of them.</span><button class="btn sm" data-need="admin" onclick="imageScan()">Scan every node</button></div>` : ""}
    <div class="note" style="margin-bottom:12px"><b>Removing images.</b> <b>Clean up</b> removes an image nothing uses from the nodes that hold it.
      <span class="tag ok">active</span> images are what running containers use, and stay. <span class="tag warn">rollback</span> copies are the image a container
      had before its last update, kept so <b>Roll back</b> can return to it - <b>Forget</b> one and it becomes unused, to clean up like the rest.</div>
    ${(d.pulls || []).map(pull => `<div class="note warn between prepull-note" style="margin-bottom:12px">
      <span>Pre-pulling <span class="mono">${esc(pull.image)}</span> · ${pull.ready} of ${pull.desired} node${pull.desired === 1 ? "" : "s"} done.
        It runs until every node has the image; its pods come straight back if you delete them.</span>
      <button class="btn sm danger" data-need="admin" onclick="prepullStop('${esc(pull.name)}')">Stop</button></div>`).join("")}
    ${(d.pulls_finished || []).length ? `<div class="note good" style="margin-bottom:12px">Finished pre-pulling
      ${d.pulls_finished.map(pull => `<span class="mono">${esc(pull.image)}</span>`).join(", ")} — cleared away.</div>` : ""}
    <div class="card flat pad0"><div class="tblwrap"><table data-sort="images" class="tbl stack imgtable"><thead><tr>
      <th>Image</th><th>Size</th><th>Cached on</th><th>Retention</th><th></th></tr></thead><tbody>
      ${imgs.map(i => {
        const missing = d.node_names.filter(n => !i.nodes.includes(n));
        const retained = i.retained_by || [];
        const retention = retained.length ? retained.slice(0, 3).map(r => `<span class="tag ${r.reason === "rollback" ? "warn" : "ok"}"
          title="${esc(r.namespace)} · ${esc(r.workload)} · ${esc(r.container)}">${r.reason === "rollback" ? "rollback" : "active"} · ${esc(r.workload)}</span>${r.reason === "rollback"
          ? `<button class="btn sm" data-need="admin" data-tip="Stop keeping this image for ${esc(r.workload)}'s Roll back, so it can be cleaned up" onclick="forgetRollback('${esc(r.namespace)}','${esc(r.workload)}')">Forget</button>` : ""}`).join("") +
          (retained.length > 3 ? `<span class="tag">+${retained.length - 3}</span>` : "") : '<span class="tag">unreferenced</span>';
        return `<tr><td class="mono small imgname">${esc(i.name)}</td>
        <td class="mono">${i.size_mb >= 1024 ? (i.size_mb / 1024).toFixed(1) + " GB" : i.size_mb + " MB"}</td>
        <td>${i.nodes.map(n => `<span class="tag ok">${esc(n.replace("harvester-", ""))}</span>`).join("")}
            ${missing.map(n => `<span class="tag">${esc(n.replace("harvester-", ""))} ✕</span>`).join("")}</td>
        <td><div class="row" style="gap:5px">${retention}</div></td>
        <td><div class="row" style="gap:6px">${missing.length ? `<button class="btn sm" onclick="prepull('${esc(i.name)}')">Pre-pull</button>` : '<span class="dim xs">everywhere</span>'}
          ${!i.protected && !i.system && i.digest ? `<button class="btn sm danger" data-need="admin" onclick="imageCleanupReview('${esc(i.digest)}')">Clean up</button>` : ""}</div></td></tr>`;
      }).join("") || `<tr><td colspan=5 class="empty">none</td></tr>`}
    </tbody></table></div></div>`);
}
window.imageScan = async () => {
  try {
    const r = await api("/api/images/scan", { method: "POST", headers: { "Content-Type": "application/json" }, body: "{}" });
    toast(r.detail, "ok"); window.__imageScanAt = Date.now();
    setTimeout(() => { if (STATE.view === "images") viewImages(); }, 5000);
  } catch (e) { toast(e.message, "bad"); }
};
window.forgetRollback = async (namespace, name) => {
  if (!confirm(`Stop keeping ${name}'s previous image? Roll back for its last update goes, and the image can be cleaned up.`)) return;
  try {
    const r = await api("/api/images/forget-rollback", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ namespace, name }) });
    toast(r.detail, "ok"); resetPaint(); viewImages();
  } catch (e) { toast(e.message, "bad"); }
};
window.prepull = async image => {
  try {
    const result = await api("/api/images/prepull", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ image }) });
    toast(result.message || "pre-pull started", result.skipped?.length ? "warn" : "ok");
    resetPaint(); viewImages();
  } catch (e) { toast(e.message, "bad"); }
};
/* Its pods are a DaemonSet's to replace, so stopping means deleting the set. */
window.prepullStop = async name => {
  try {
    const result = await api("/api/images/prepull/stop", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name }) });
    toast(result.message || "pre-pull stopped", "ok");
    resetPaint(); viewImages();
  } catch (e) { toast(e.message, "bad"); }
};

/* ---------------- schedules ---------------- */
async function viewSchedules() {
  const js = await api("/api/schedules");
  paint(`<div class="phead"><div><h2>Schedules</h2>
      <p>${js.length} scheduled job${js.length === 1 ? "" : "s"} · standard cron syntax</p></div>
      <button class="btn pri" data-need="operator" onclick="jobEdit()">＋ New schedule</button></div>
    <div class="card flat pad0"><div class="tblwrap"><table data-sort="schedules" class="tbl stack"><thead><tr>
      <th>Name</th><th>Schedule</th><th>Image</th><th data-nosort>Last run</th><th>State</th><th></th></tr></thead><tbody>
      ${js.map(j => `<tr>
        <td><b>${esc(j.name)}</b></td>
        <td class="mono">${esc(j.schedule)}</td>
        <td class="mono small dim">${esc(j.image)}</td>
        <td class="small dim">${esc((j.last || "—").replace("T", " ").replace("Z", ""))}</td>
        <td>${j.suspend ? '<span class="pill low">paused</span>'
            : j.active ? '<span class="pill med">running</span>' : '<span class="pill ok">active</span>'}</td>
        <td><div class="row" style="gap:6px">
          <button class="btn sm" onclick="jobRun('${esc(j.name)}')">Run now</button>
          <button class="btn sm" onclick='jobEdit(${JSON.stringify(j).replace(/'/g, "&#39;")})'>Edit</button>
          <button class="btn sm danger" onclick="jobDel('${esc(j.name)}')">✕</button>
        </div></td></tr>`).join("") || `<tr><td colspan=6 class="empty">no schedules yet</td></tr>`}
    </tbody></table></div></div>`);
}
window.jobEdit = (j) => {
  j = j || { name: "", schedule: "0 3 * * *", image: "alpine:3.20", command: "" };
  modal(j.name ? "Edit · " + j.name : "New schedule", `
    <div class="f"><label>Name</label><input type="text" id="j_name" value="${esc(j.name)}" ${j.name ? "readonly" : ""} placeholder="nightly-prune"></div>
    <div class="f"><label>Cron schedule</label><input type="text" id="j_sched" value="${esc(j.schedule)}">
      <div class="dim xs" style="margin-top:6px">min hour day month weekday · e.g. <span class="mono">0 3 * * *</span> = 03:00 daily</div></div>
    <div class="f"><label>Image</label><input type="text" id="j_image" value="${esc(j.image || "alpine:3.20")}"></div>
    <div class="f"><label>Command</label><textarea id="j_cmd" rows="4" placeholder="echo hello">${esc(j.command || "")}</textarea></div>
    <label class="switch"><input type="checkbox" id="j_susp" ${j.suspend ? "checked" : ""}> Paused</label>
    <div class="row" style="margin-top:16px">
      <button class="btn pri" onclick="jobSave()">Save</button>
      <button class="btn" onclick="closeModal()">Cancel</button></div>`);
};
window.jobSave = async () => {
  const body = { name: $("#j_name").value.trim(), schedule: $("#j_sched").value.trim(),
    image: $("#j_image").value.trim(), command: $("#j_cmd").value, suspend: $("#j_susp").checked };
  if (!body.name || !body.command) return toast("name and command are required", "bad");
  try {
    await api("/api/schedules", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
    toast("schedule saved", "ok"); closeModal(); resetPaint(); viewSchedules();
  } catch (e) { toast(e.message, "bad"); }
};
window.jobRun = async name => {
  try { await api("/api/schedules/run", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ name }) });
    toast(`${name} started`, "ok"); } catch (e) { toast(e.message, "bad"); }
};
window.jobDel = async name => {
  if (!confirm(`Delete schedule "${name}"?`)) return;
  try { await api("/api/schedules/delete", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ name }) });
    toast("deleted", "ok"); resetPaint(); viewSchedules(); } catch (e) { toast(e.message, "bad"); }
};

/* ---------------- import ---------------- */
/* rsync restarts its percentage for every folder, so the job announces each
   one and the bar shows how far through the whole set the copy is. */
function importProgressCell(job) {
  const done = job.state === "done", failed = job.state === "failed";
  if (job.percent == null && !done) {
    return `<div class="dim xs">${failed ? "stopped before reporting" : "starting…"}</div>`;
  }
  const percent = done ? 100 : Math.max(2, job.percent || 0);
  if (failed && job.error) {
    return `<div class="jobmeter"><span class="failed" style="width:${percent}%"></span></div>
      <div class="xs" style="color:#ffb4b8">${esc(job.error)}</div>
      ${job.error_detail ? `<div class="dim xs mono" data-tip="${esc(job.error_detail)}">${esc(job.error_detail.slice(0, 48))}${job.error_detail.length > 48 ? "…" : ""}</div>` : ""}`;
  }
  const label = done ? "copied"
    : job.steps > 1 ? `folder ${job.step || 1} of ${job.steps}${job.folder ? ` · ${job.folder}` : ""}`
    : job.folder ? esc(job.folder) : "copying";
  return `<div class="jobmeter"><span class="${failed ? "failed" : ""}" style="width:${percent}%"></span></div>
    <div class="dim xs mono">${done ? "100%" : `${job.percent ?? 0}%`}${job.rate ? ` · ${esc(job.rate)}` : ""}</div>
    <div class="dim xs">${esc(label)}</div>`;
}

async function viewImport() {
  const [, nodes] = await Promise.all([loadHardwareFeatures(), api("/api/nodes").catch(() => [])]);
  if (nodes.length) STATE.data.nodes = nodes;
  const [srcs, jobs, disks, namespaces, storageClasses, clusters] = await Promise.all([
    api("/api/sources"), api("/api/imports").catch(() => []), api("/api/vm-disks").catch(() => []),
    api("/api/namespaces").catch(() => ["lab"]), api("/api/storageclasses").catch(() => ["longhorn-r2"]),
    api("/api/move/clusters").catch(() => []),
  ]);
  STATE.data.clusters = clusters;
  STATE.data.classFacts = (await api("/api/storageclasses?facts=1").catch(() => ({}))).facts || {};
  const moves = await api("/api/move/moves").catch(() => []);
  STATE.data.srcs = srcs; STATE.data.importNamespaces = namespaces; STATE.data.importStorageClasses = storageClasses;
  // After the page is up: each check waits on the other Homestead answering.
  setTimeout(() => clusters.forEach(c => clusterCheck(c.name)), 0);
  paint(`<div class="phead"><div><h2>Import</h2>
      <p>Bring containers, appdata and virtual-machine disks into Homestead</p></div>
      <div class="row"><button class="btn" data-need="operator" onclick="composeImport()">＋ Docker Compose</button>
      <button class="btn" data-need="admin" onclick="clusterAdd()">＋ Homestead cluster</button>
      <button class="btn" data-need="admin" onclick="srcAdd()">＋ Container source</button>
      <button class="btn pri" data-need="admin" onclick="vmDiskImport()">＋ VM disk</button></div></div>

    ${moves.length ? `<div class="between"><div class="sec">Moves ${tip("Workloads being brought here from another Homestead cluster. Each keeps going across restarts of either Homestead; the source is only removed when you say so.")}</div>
      ${moves.some(m => ["succeeded", "cancelled"].includes(m.status)) ? '<button class="btn sm" data-need="admin" onclick="moveDismiss()">Clear finished</button>' : ""}</div>` : ""}
    <div id="movesList">${movesHtml(moves)}</div>

    <div class="sec">Other Homestead clusters ${tip("Another Homestead installation on the network. Its workloads can be listed here, and later moved across: volume data travels through the shared Longhorn backup target, the definition comes straight from the other Homestead.")}</div>
    ${clusters.length ? `<div class="grid g3">${clusters.map(c => `<div class="card flat clcard">
      <div class="between"><div class="clhead"><div class="ctitle">${esc(c.name)}</div>
        <div class="csub mono clurl" title="${esc(c.user)}@${esc(c.url)}">${esc(c.user)}@${esc(c.url)}</div></div>
        <button class="btn sm danger" data-need="admin" onclick="clusterDel('${esc(c.name)}')">✕</button></div>
      <div class="clver"><span class="dim xs">Version</span>
        <span id="clver_${esc(c.name)}"><span class="dim xs"><span class="spin2"></span> checking…</span></span>
        <button class="iconbtn clrecheck" data-tip="Check the version again" onclick="clusterCheck('${esc(c.name)}')">${icon("refresh")}</button></div>
      <div id="clvermsg_${esc(c.name)}"></div>
      <div class="clsteps" id="clready_${esc(c.name)}"></div>
      <div class="row" style="margin-top:12px"><button class="btn sm" onclick="clusterBrowse('${esc(c.name)}')">Browse workloads</button></div>
      <div class="dim xs" id="cluster_${esc(c.name)}" style="margin-top:10px"></div>
    </div>`).join("")}</div>`
    : `<div class="empty">No other clusters connected.
       <ol class="clguide">
         <li><b>＋ Homestead cluster</b> above: the other Homestead's address and an admin account there.</li>
         <li>Backup storage on that cluster, which its volumes travel through - its card offers to set it up.</li>
         <li><b>Browse workloads</b> on its card, then <b>Move to this cluster</b>.</li></ol></div>`}

    <div class="sec">VM disk images ${tip("CDI downloads supported QEMU disk formats, including qcow2 and vmdk, converts them into a VM-ready disk, and writes the result into a new Longhorn PVC.")}</div>
    ${disks.length ? `<div class="card flat pad0"><div class="tblwrap"><table data-sort="vm-disks" class="tbl stack"><thead><tr>
      <th>Disk / PVC</th><th>Capacity</th><th>Status</th><th>Progress</th><th>Attached to</th><th></th></tr></thead><tbody>
      ${disks.map(d => { const done = d.phase === "Succeeded", failed = ["Failed","Error","Unknown"].includes(d.phase); return `<tr>
        <td><b>${esc(d.name)}</b><div class="dim xs mono">${esc(d.namespace)} · ${esc(d.storage_class || "storage class unknown")}</div></td>
        <td class="mono small">${esc(d.capacity || "—")}<div class="dim xs">${esc((d.access_modes || []).join(", "))}</div></td>
        <td><span class="pill ${done ? "ok" : failed ? "crit" : "med"}">${esc(d.phase.replace(/([a-z])([A-Z])/g, "$1 $2"))}</span>${d.message ? `<div class="dim xs" style="margin-top:5px;max-width:320px">${esc(d.message)}</div>` : ""}</td>
        <td style="min-width:130px"><div class="jobmeter"><span class="${failed ? "failed" : ""}" style="width:${Math.max(2, done ? 100 : d.progress || 0)}%"></span></div><div class="dim xs mono">${done ? "ready" : `${esc(d.progress || 0)}%`}</div></td>
        <td class="small">${d.in_use ? esc((d.used_by || []).join(", ")) : '<span class="dim">not attached</span>'}</td>
        <td>${done && !d.in_use ? `<button class="btn sm" onclick="vmNew('${esc(d.name)}','${esc(d.namespace)}')">Create VM</button>` : ""}</td></tr>`; }).join("")}
      </tbody></table></div></div>` : `<div class="empty">No managed VM disk imports yet. Import an HTTP(S) qcow2, vmdk, raw, vdi, vhd or vhdx image into a new PVC.</div>`}

    <div class="sec">Container sources</div>
    <div class="grid g3">${srcs.map(s => `<div class="card flat">
      <div class="between"><div><div class="ctitle">${esc(s.name)}</div>
        <div class="csub">${esc(s.kind)} · ${esc(s.user)}@${esc(s.host)}</div></div>
        <button class="btn sm danger" onclick="srcDel('${esc(s.name)}')">✕</button></div>
      <div class="drow"><div class="dl">Base path</div><div class="dv mono small">${esc(s.base_path)}</div></div>
      <button class="btn wide" style="margin-top:12px" onclick="srcBrowse('${esc(s.name)}')">Browse appdata</button>
    </div>`).join("") || `<div class="empty">No import sources yet. Add the host you want to pull from.</div>`}</div>

    ${jobs.length ? `<div class="sec">Transfers</div>
    <div class="card flat pad0"><div class="tblwrap"><table data-sort="imports" class="tbl stack"><thead><tr>
      <th>App</th><th>Job</th><th>State</th><th>Progress</th><th data-nosort>Started</th><th></th></tr></thead><tbody>
      ${jobs.map(j => `<tr><td><b>${esc(j.app || "—")}</b>${j.kind === "chown" ? '<span class="tag">ownership</span>' : ""}</td>
        <td class="mono small dim">${esc(j.name)}</td>
        <td><span class="pill ${j.state === "done" ? "ok" : j.state === "failed" ? "crit" : "med"}">${esc(j.state)}</span></td>
        <td style="min-width:150px">${importProgressCell(j)}</td>
        <td class="small dim">${esc((j.start || "").replace("T", " ").replace("Z", ""))}</td>
        <td><div class="row" style="gap:6px;flex-wrap:nowrap"><button class="btn sm" onclick="jobLogs('lab','${esc(j.name)}')">Logs</button>
          <button class="btn sm ${j.state === "failed" ? "danger" : ""}" data-need="admin" title="${j.state === "running" ? "Stop this copy and remove its job" : "Remove this job; it keeps referencing the volume until it is gone"}" onclick="importRemove('${esc(j.name)}','${esc(j.state)}')">${j.state === "running" ? "Cancel" : "Remove"}</button></div></td></tr>`).join("")}
    </tbody></table></div></div>` : ""}

    <div class="note" style="margin-top:20px">
      <b>Container import.</b> It creates a Longhorn volume, runs an rsync job that copies the remote
      appdata directory into it, and creates the workload pointing at that volume — left stopped so you can
      start it once the copy finishes. Path mappings from the source host do not carry over; the appdata
      lands at the mount path you choose.
    </div>`);
  // A running move keeps its own card current without repainting the page.
  if (moves.some(m => m.status === "running")) {
    clearTimeout(window.__moveTimer);
    window.__moveTimer = setTimeout(watchMoves, 4000);
  }
}
window.importRemove = async (name, state) => {
  const running = state === "running";
  const plan = await api("/api/imports/cleanup-plan", { method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ name }) }).catch(() => ({ workload: "", volume: "", volume_created: false, volumes: [], known: false }));
  // An import can have filled several volumes; every one it created is offered.
  const claims = plan.volumes?.length ? plan.volumes
    : plan.volume ? [{ name: plan.volume, created: plan.volume_created }] : [];
  modal(running ? `Cancel import · ${name}` : `Remove import · ${name}`, `
    <div class="note">${running
      ? "Cancelling stops the copy where it is. Files already written stay on the volume."
      : "Removing the job clears it from Transfers. While it exists it keeps referencing the volume it copied into, which blocks deleting that volume."}</div>
    <div class="sec">Also remove what this import created</div>
    <div class="volume-delete-grid">
      <label class="volume-delete-option ${plan.workload ? "" : "disabled"}">
        <input type="checkbox" id="imr_workload" ${plan.workload ? "" : "disabled"}>
        <span><b>The workload${plan.workload ? ` · ${esc(plan.workload)}` : ""}</b>
          <small>${plan.workload
            ? "Deletes the Deployment this import created, and every Service pointing at it."
            : "This import created no workload, or it has already been deleted."}</small></span></label>
      ${claims.length ? claims.map(claim => `
      <label class="volume-delete-option danger ${claim.created ? "" : "disabled"}">
        <input type="checkbox" class="imr-volume" value="${esc(claim.name)}" ${claim.created ? "" : "disabled"}>
        <span><b>The volume · ${esc(claim.name)}</b>
          <small>${claim.created
            ? "Deletes the claim this import created, along with whatever was copied into it."
            : `${esc(claim.name)} existed before this import, so it is not this import's to delete. Remove it from Volumes if you mean to.`}</small></span></label>`).join("")
      : `<label class="volume-delete-option danger disabled"><input type="checkbox" disabled>
        <span><b>The volume</b><small>No volume is recorded for this import.</small></span></label>`}
    </div>
    ${plan.known === false ? '<div class="note">This import predates the record of what it created, so only the job is removed.</div>' : ""}
    <div class="row" style="margin-top:18px">
      <button class="btn danger" id="imr_go" data-need="admin" onclick="importRemoveNow('${esc(name)}',this)">${running ? "Cancel import" : "Remove"}</button>
      <button class="btn" onclick="closeModal()">Keep it</button></div>`);
  if (window.applyRole) window.applyRole();
};
window.importRemoveNow = async (name, button) => {
  const volumes = $$(".imr-volume").filter(box => box.checked).map(box => box.value);
  const body = { name, remove_workload: !!$("#imr_workload")?.checked,
    remove_volume: volumes.length > 0, remove_volumes: volumes };
  if (volumes.length && !confirm(`Delete ${volumes.length === 1 ? volumes[0] : volumes.join(" and ")} `
      + "and everything copied into " + (volumes.length === 1 ? "it" : "them") + "?\n\nThis cannot be undone.")) return;
  if (button) { button.disabled = true; button.textContent = "Removing…"; }
  try {
    const result = await api("/api/imports/delete", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body) });
    closeModal(); toast(result.message || `${name} removed`, "ok"); resetPaint(); viewImport();
  } catch (e) {
    if (button) { button.disabled = false; button.textContent = "Remove"; }
    toast(e.message, "bad");
  }
};

window.vmDiskImport = () => {
  const namespaces = STATE.data.importNamespaces || ["lab"];
  const classes = STATE.data.importStorageClasses || ["longhorn-r2"];
  modal("Import VM disk", `
    <div class="note"><b>CDI handles the conversion.</b> Provide a directly downloadable HTTP(S) qcow2, vmdk, raw, vdi, vhd or vhdx image. The source is streamed into a new Longhorn PVC; an existing disk is never overwritten.</div>
    <div class="f2" style="margin-top:16px">
      <div class="f"><label>Namespace ${tip("The imported DataVolume and its PVC are created here. A VM using the disk must be in the same namespace.")}</label><select id="vd_ns">${namespaces.map(n => `<option value="${esc(n)}" ${n === "lab" ? "selected" : ""}>${esc(n)}</option>`).join("")}</select></div>
      <div class="f"><label>Disk / PVC name ${tip("This becomes both the CDI DataVolume name and the resulting PVC name.")}</label><input id="vd_name" placeholder="ubuntu-server"></div>
    </div>
    <div class="f"><label>Image URL ${tip("HTTP and HTTPS only. URLs containing embedded usernames or passwords are rejected; use a Kubernetes Secret for authenticated endpoints.")}</label><input type="url" id="vd_url" placeholder="https://example.net/images/server.qcow2"></div>
    <div class="f2">
      <div class="f"><label>Capacity (GiB) ${tip("Must be at least as large as the image's virtual disk capacity. CDI expands the converted disk to fit this PVC.")}</label><input type="number" min="1" max="16384" id="vd_size" value="20"></div>
      <div class="f"><label>Storage class</label><select id="vd_sc">${classes.map(s => `<option value="${esc(s)}" ${s === "longhorn-r2" ? "selected" : ""}>${esc(s)}</option>`).join("")}</select></div>
    </div>
    <div class="f2">
      <div class="f"><label>Access mode ${tip("RWO is the normal choice for a VM boot disk. RWX allows several nodes to mount it but does not make concurrent VM writes safe.")}</label><select id="vd_mode"><option value="ReadWriteOnce">RWO · one node</option><option value="ReadWriteMany">RWX · many nodes</option></select></div>
      <div class="f"><label>Checksum (optional) ${tip("Recommended for downloaded images. Paste the publisher's SHA-256 or SHA-512 hexadecimal digest.")}</label><div class="row" style="gap:7px"><select id="vd_algo" style="max-width:110px"><option value="sha256">SHA-256</option><option value="sha512">SHA-512</option></select><input id="vd_checksum" class="mono" placeholder="hex digest"></div></div>
    </div>
    <details><summary class="small">Authenticated or private CA source</summary><div class="f2" style="margin-top:12px">
      <div class="f"><label>Credential Secret ${tip("Optional Secret in the destination namespace containing CDI-compatible accessKeyId and secretKey fields.")}</label><input id="vd_secret" placeholder="image-download-credentials"></div>
      <div class="f"><label>CA ConfigMap ${tip("Optional ConfigMap in the destination namespace containing the endpoint's CA certificate.")}</label><input id="vd_ca" placeholder="private-ca"></div></div></details>
    <div class="row" style="margin-top:18px"><button class="btn pri" onclick="doVmDiskImport()">Start import</button><button class="btn" onclick="closeModal()">Cancel</button></div>
    <div class="dim xs" style="margin-top:12px">Progress continues in the active-jobs tray after this dialog closes. Source URLs are not copied into Homestead's operation history.</div>`, true);
};
window.doVmDiskImport = async () => {
  const digest = $("#vd_checksum").value.trim();
  const body = { namespace: $("#vd_ns").value, name: $("#vd_name").value.trim(),
    source_url: $("#vd_url").value.trim(), size_gb: +$("#vd_size").value,
    storage_class: $("#vd_sc").value, access_mode: $("#vd_mode").value,
    checksum: digest ? `${$("#vd_algo").value}:${digest}` : "",
    secret_ref: $("#vd_secret").value.trim(), cert_config_map: $("#vd_ca").value.trim() };
  if (!body.name || !body.source_url) return toast("disk name and image URL are required", "bad");
  try {
    const plan = await api(`/api/vm-disks/import-plan?ns=${encodeURIComponent(body.namespace)}&name=${encodeURIComponent(body.name)}`);
    if (!plan.ready) return toast(plan.message, "bad");
    const result = await api("/api/vm-disks/import", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
    toast(result.message, "ok"); closeModal(); resetPaint(); viewImport();
  } catch (e) { toast(e.message, "bad"); }
};
window.srcAdd = () => modal("Add import source", `
  <div class="f"><label>Name</label><input type="text" id="sc_name" placeholder="unraid"></div>
  <div class="f2">
    <div class="f"><label>Host</label><input type="text" id="sc_host" placeholder="192.0.2.177"></div>
    <div class="f"><label>Type</label><select id="sc_kind">
      <option value="unraid">Unraid</option><option value="proxmox">Proxmox</option><option value="ssh">Generic SSH</option></select></div>
  </div>
  <div class="f2">
    <div class="f"><label>Username</label><input type="text" id="sc_user" value="root"></div>
    <div class="f"><label>Password</label><input type="password" id="sc_pass"></div>
  </div>
  <div class="f"><label>Appdata base path</label><input type="text" id="sc_path" value="/mnt/user/appdata"></div>
  <div class="dim xs" style="margin-bottom:12px">Unraid® is a registered trademark of Lime Technology, Inc. This application is not affiliated with, endorsed, or sponsored by Lime Technology, Inc.</div>
  <div class="row" style="margin-top:16px">
    <button class="btn pri" onclick="doSrcAdd()">Add source</button>
    <button class="btn" onclick="closeModal()">Cancel</button></div>
  <div class="note" style="margin-top:14px">The password is stored in a Kubernetes Secret in the
  <span class="mono">lab</span> namespace, not in the ConfigMap. Anyone with cluster access can read it.</div>`);
window.doSrcAdd = async () => {
  const body = { name: $("#sc_name").value.trim(), host: $("#sc_host").value.trim(),
    user: $("#sc_user").value.trim(), password: $("#sc_pass").value,
    kind: $("#sc_kind").value, base_path: $("#sc_path").value.trim() };
  if (!body.name || !body.host) return toast("name and host are required", "bad");
  try {
    await api("/api/sources", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
    toast("source added", "ok"); closeModal(); resetPaint(); viewImport();
  } catch (e) { toast(e.message, "bad"); }
};
window.srcDel = async name => {
  if (!confirm(`Remove import source "${name}"?`)) return;
  try { await api("/api/sources/delete", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ name }) });
    toast("removed", "ok"); resetPaint(); viewImport(); } catch (e) { toast(e.message, "bad"); }
};
window.srcBrowse = async name => {
  modal("Browse · " + name, `<div class="empty"><span class="spin2"></span>connecting to host — this runs a
    one-shot pod, so it takes ~20s</div>`, true);
  try {
    const [r, dc] = await Promise.all([
      api("/api/sources/browse", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ name }) }),
      api("/api/sources/containers", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ name }) }).catch(() => ({ containers: [] })),
    ]);
    const src = (STATE.data.srcs || []).find(s => s.name === name) || {};
    const entries = r.entries.filter(e => !e.startsWith("==") && !/^(WARNING|Permission|Warning)/i.test(e));
    $("#mbody").innerHTML = `${dc.containers.length ? `<div class="ctitle">Docker containers found</div>
      <p class="muted small">Choose a container to carry over its image, ports, environment variables, icon and appdata mount automatically.</p>
      <div class="apps importapps" style="margin-top:14px">${dc.containers.map(c => `<div class="card flat importapp">
        <div class="between"><b>${esc(c.name)}</b><span class="pill ${c.state === "running" ? "ok" : "low"}">${esc(c.state || "unknown")}</span></div>
        <div class="mono xs dim" style="margin-top:7px;word-break:break-all">${esc(c.image)}</div>
        <button class="btn pri wide sm" style="margin-top:10px" onclick="inspectImport('${esc(name)}','${esc(c.name)}')">Import container</button></div>`).join("")}</div>
      <div class="sec">Appdata folders</div>` : ""}` + (entries.length
      ? `<p class="muted small">${entries.length} directories under <span class="mono">${esc(src.base_path)}</span>. Use this when Docker metadata is unavailable.</p>
         <div class="apps" style="margin-top:14px;grid-template-columns:repeat(auto-fill,minmax(200px,1fr))">
         ${entries.map(e => `<div class="card flat" style="padding:13px">
           <div style="font-weight:660;word-break:break-all">${esc(e)}</div>
           <button class="btn wide sm" style="margin-top:10px"
             onclick="importSetup('${esc(name)}','${esc(e)}')">Import</button></div>`).join("")}</div>`
      : `<div class="empty">No appdata folders returned. Check the credentials and that
         <span class="mono">${esc(src.base_path)}</span> exists on ${esc(src.host)}.</div>`);
  } catch (e) { $("#mbody").innerHTML = `<div class="empty">${esc(e.message)}</div>`; }
};

window.imageCleanupReview = digest => {
  const image = (STATE.data.imageCache?.images || []).find(row => row.digest === digest);
  if (!image) return toast("refresh Image Cache before cleaning this image", "bad");
  if (image.protected || image.system) return toast("this image is protected and cannot be cleaned", "bad");
  const perNode = image.size_mb >= 1024 ? `${(image.size_mb / 1024).toFixed(1)} GB` : `${image.size_mb} MB`;
  modal("Clean cached image", `<div class="update-review">
    <div class="note dependency-danger"><b>This deletes only the cached image layers, not a workload.</b>
      A future start may be slower while the registry image is downloaded again. Active, immediate rollback, and platform images are blocked server-side.</div>
    <div class="update-image"><div><span>Image</span><code>${esc(image.name)}</code></div>
      <div><span>Digest</span><code>${esc(digest)}</code></div><div><span>Per node</span><code>${esc(perNode)}</code></div></div>
    <div class="f"><label>Remove from nodes</label><div class="cleanup-nodes">${image.nodes.map(node =>
      `<label class="switch"><input class="cleanup-node" type="checkbox" value="${esc(node)}" checked onchange="imageCleanupGate()"> ${esc(node)}</label>`).join("")}</div></div>
    <div class="f"><label>Type CLEAN to confirm</label><input id="cleanupConfirm" autocomplete="off" oninput="imageCleanupGate()" placeholder="CLEAN"></div>
    <div class="row"><button id="cleanupGo" class="btn danger" data-need="admin" disabled onclick="imageCleanupApply('${esc(digest)}')">Remove cached image</button>
      <button class="btn" onclick="closeModal()">Cancel</button></div></div>`, true);
  if (window.applyRole) window.applyRole();
};
window.imageCleanupGate = () => {
  const button = $("#cleanupGo");
  if (button) button.disabled = $("#cleanupConfirm")?.value !== "CLEAN" || !$(".cleanup-node:checked");
};
window.imageCleanupApply = async digest => {
  const nodes = $$(".cleanup-node:checked").map(input => input.value);
  try {
    const result = await api("/api/images/cleanup", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ digest, nodes }) });
    toast(`cleanup started on ${result.nodes.length} node${result.nodes.length === 1 ? "" : "s"}`, "ok");
    closeModal();
  } catch (error) { toast(error.message, "bad"); }
};
window.inspectImport = async (source, container) => {
  $("#mbody").innerHTML = '<div class="empty"><span class="spin2"></span>reading Docker configuration…</div>';
  try {
    const cfg = await api("/api/sources/inspect", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name: source, container }) });
    importSetup(source, container, cfg);
  } catch (e) { $("#mbody").innerHTML = `<div class="empty"><b>Could not inspect ${esc(container)}</b><br><span class="dim small">${esc(e.message)}</span></div>`; }
};
/* Every Docker mount under the source appdata path is importable; anything
   else on the host is offered but left unticked, because it is not appdata. */
function importMappingRows(cfg, src) {
  const base = (src.base_path || "/mnt/user/appdata").replace(/\/+$/, "");
  const seen = new Set();
  const rows = (cfg.mounts || [])
    .filter(mount => mount.path && (mount.source || mount.type === "tmpfs"))
    .map(mount => mount.type === "tmpfs"
      // tmpfs on the source is RAM, so there is nothing to copy and nowhere to
      // copy it from: it becomes a memory-backed scratch volume instead.
      ? { mount_path: mount.path, medium: "memory", size_mb: mount.size_mb || 1024, include: true }
      : { remote_path: mount.source, mount_path: mount.path,
          include: mount.source === cfg.remote_path || mount.source.startsWith(base + "/") });
  // cfg.remote_path is only worth adding when the host actually reported it.
  // A guessed one is a folder nobody has: it would be ticked, copied, and fail.
  // Kubernetes gives a pod 64 MiB of /dev/shm and no way to ask for more except
  // a memory-backed volume over the top, so a container Docker gave more than
  // that keeps it: without it Frigate's frames never reach the detectors.
  if (cfg.shm_mb && !rows.some(row => row.mount_path === "/dev/shm")) {
    rows.push({ mount_path: "/dev/shm", medium: "memory", size_mb: cfg.shm_mb, include: true });
  }
  if (cfg.remote_path && !cfg.guessed_path && !rows.some(row => row.remote_path === cfg.remote_path)) {
    rows.unshift({ remote_path: cfg.remote_path, mount_path: cfg.mount_path || "/config", include: true });
  }
  // Nothing mounted means nothing to copy. An empty list says that plainly;
  // a blank row invites someone to invent a path the container never had.
  if (!rows.length && cfg.remote_path && !cfg.guessed_path) {
    rows.push({ remote_path: cfg.remote_path, mount_path: cfg.mount_path || "/config", include: true });
  }
  return rows.filter(row => {
    const key = row.medium ? `ram:${row.mount_path}` : row.remote_path;
    return !seen.has(key) && seen.add(key);
  });
}

function importFolderName(remotePath, mountPath) {
  const tail = String(remotePath || mountPath || "data").replace(/\/+$/, "").split("/").pop();
  return tail.toLowerCase().replace(/[^a-z0-9._-]+/g, "-").replace(/^[-.]+|[-.]+$/g, "").slice(0, 60) || "data";
}

function importMappingRow(row = {}) {
  if (row.medium === "memory") {
    return `<div class="im-map im-scratch" data-medium="memory">
      <div><label>Source</label><input class="imm-remote" type="text" value="" disabled placeholder="RAM — nothing to copy"></div>
      <div><label>Kind</label><input type="text" value="Memory scratch" disabled></div>
      <div><label>Size MiB</label><input class="imm-ram" type="number" min="1" max="65536" value="${esc(row.size_mb || 1024)}"></div>
      <div><label>Path inside the container</label><input class="imm-mount" type="text" value="${esc(row.mount_path || "")}" placeholder="/tmp/cache"></div>
      <label class="switch"><input class="imm-on" type="checkbox" ${row.include === false ? "" : "checked"} onchange="imSyncMaps()">Create</label>
      <button class="iconbtn row-remove" type="button" title="Remove scratch volume" onclick="this.closest('.im-map').remove();imSyncMaps()">×</button>
      <div class="dim xs" style="grid-column:1/-1;margin-top:-4px">${row.mount_path === "/dev/shm"
        ? "Shared memory, as <code>--shm-size</code> gave it on the source. A pod gets 64 MiB otherwise, which is not enough for apps that pass frames or buffers between processes."
        : "This was tmpfs on the source: a RAM disk that starts empty every time. Kubernetes gives it the same thing, capped at this size and counted against the node's memory."}</div></div>`;
  }
  return `<div class="im-map" data-pvc="${esc(row.pvc || "")}">
    <div><label>Source folder on the host</label><input class="imm-remote" type="text" value="${esc(row.remote_path || "")}" placeholder="/mnt/user/appdata/app/config" oninput="imSyncMaps()"></div>
    <div><label>Goes to volume</label><select class="imm-pvc" onchange="imSyncMaps()"></select></div>
    <div><label>Subfolder</label><input class="imm-folder" type="text" value="${esc(row.folder || "")}" placeholder="${esc(importFolderName(row.remote_path, row.mount_path))}" oninput="imSyncMaps()"></div>
    <div><label>Path inside the container</label><input class="imm-mount" type="text" value="${esc(row.mount_path || "")}" placeholder="/config"></div>
    <div><label>Contents ${tip("Copy: the folder's files are copied into the volume. Mount empty: the volume is mounted at this path with nothing copied - new recordings, say, where the old ones stay behind. Leave out: not mounted at all.")}</label>
      <select class="imm-mode" onchange="imSyncMaps()">
        <option value="copy" ${row.include === false || row.copy === false ? "" : "selected"}>Copy</option>
        <option value="empty" ${row.copy === false && row.include !== false ? "selected" : ""}>Mount empty</option>
        <option value="skip" ${row.include === false ? "selected" : ""}>Leave out</option></select></div>
    <button class="iconbtn row-remove" type="button" title="Remove folder" onclick="this.closest('.im-map').remove();imSyncMaps()">×</button>
    <div class="dim xs imm-size" style="grid-column:1/-1;margin-top:-4px"></div></div>`;
}

window.imAddScratch = () => {
  $("#im_maps").insertAdjacentHTML("beforeend",
    importMappingRow({ medium: "memory", size_mb: 1024, mount_path: "/tmp/cache", include: true }));
  imSyncMaps();
};

/* A volume row is just a claim: a name, whether it is new, and how big.
   The mapping rows above pick from these by name. */
function importVolumeRow(volume = {}, index = 0) {
  const classes = (STATE.data.importStorage?.storage_classes || ["longhorn-r2"]);
  const claims = (STATE.data.importStorage?.pvcs || []);
  return `<div class="f4 im-volume">
    <div><label>Volume name</label><input class="imv-name" type="text" value="${esc(volume.name || "")}" list="im_pvc_list" oninput="imSyncVolumes()">
      <datalist id="im_pvc_list">${claims.map(claim => `<option value="${esc(claim.name)}">${esc(claim.size || "")} ${esc((claim.access_modes || []).join("/"))}</option>`).join("")}</datalist></div>
    <div><label>Source</label><select class="imv-kind" onchange="imSyncVolumes()">
      <option value="new-rwo" ${volume.access_mode === "ReadWriteMany" ? "" : "selected"}>New Longhorn volume · RWO</option>
      <option value="new-rwx" ${volume.access_mode === "ReadWriteMany" ? "selected" : ""}>New shared volume · RWX</option>
      <option value="existing" ${volume.create === false ? "selected" : ""}>Existing PVC · merge data</option></select></div>
    <div><label>Size GiB</label><input class="imv-size" type="number" min="1" max="16384" value="${esc(volume.size_gb || 10)}"></div>
    <div><label>Storage class</label><select class="imv-class">${storageClassOptions(classes, volume.storage_class || "longhorn-r2", STATE.data.classFacts)}</select></div>
    ${index === 0 ? "" : '<button class="iconbtn row-remove" type="button" title="Remove volume" onclick="this.closest(\'.im-volume\').remove();imSyncVolumes()">×</button>'}
  </div>`;
}

window.imAddVolume = () => {
  const rows = $$("#im_volumes .im-volume");
  const base = ($("#im_name")?.value.trim() || "app");
  $("#im_volumes").insertAdjacentHTML("beforeend",
    importVolumeRow({ name: `${base}-${rows.length === 0 ? "appdata" : "data" + (rows.length + 1)}`, size_gb: rows.length ? 100 : 10 }, rows.length));
  imSyncVolumes();
};

window.importVolumes = () => $$("#im_volumes .im-volume").map(row => {
  const kind = $(".imv-kind", row).value;
  return { name: $(".imv-name", row).value.trim(), create: kind !== "existing",
    size_gb: +$(".imv-size", row).value || 10, storage_class: $(".imv-class", row).value,
    access_mode: kind === "new-rwx" ? "ReadWriteMany" : "ReadWriteOnce" };
}).filter(volume => volume.name);

window.imSyncVolumes = () => {
  const volumes = importVolumes();
  $$("#im_volumes .im-volume").forEach(row => {
    const existing = $(".imv-kind", row).value === "existing";
    $(".imv-size", row).disabled = existing;
    $(".imv-class", row).disabled = existing;
  });
  // Every mapping keeps its choice if that volume still exists, else falls to the first.
  $$("#im_maps .im-map").forEach(row => {
    const select = $(".imm-pvc", row);
    if (!select) return;
    const wanted = select.value || row.dataset.pvc || "";
    select.innerHTML = volumes.map(volume => `<option ${volume.name === wanted ? "selected" : ""}>${esc(volume.name)}</option>`).join("")
      || '<option value="">add a volume first</option>';
    if (!volumes.some(volume => volume.name === wanted) && volumes.length) select.value = volumes[0].name;
    row.dataset.pvc = select.value;
  });
  imSyncMaps();
};

window.imAddMap = () => {
  $("#im_maps").insertAdjacentHTML("beforeend", importMappingRow({ include: true }));
  imSyncMaps();
};
window.imSyncMaps = () => {
  const all = $$("#im_maps .im-map").filter(row => imMode(row) !== "skip");
  const scratch = all.filter(row => row.dataset.medium === "memory");
  const empty = all.filter(row => row.dataset.medium !== "memory" && imMode(row) === "empty");
  const rows = all.filter(row => row.dataset.medium !== "memory" && imMode(row) === "copy");
  const note = $("#im_maps_note");
  if (note) {
    const targets = new Set(rows.map(row => $(".imm-pvc", row)?.value).filter(Boolean));
    const ram = scratch.length ? ` ${scratch.length} RAM scratch volume${scratch.length === 1 ? "" : "s"} is created empty.` : "";
    const mounted = empty.length ? ` ${empty.length} more mounted empty, nothing copied.` : "";
    note.textContent = (!rows.length ? "Nothing selected to copy."
      : targets.size > 1 ? `${rows.length} folders across ${targets.size} volumes, each mounted back separately.`
      : rows.length > 1 ? `${rows.length} folders into one volume, each in its own subfolder.`
      : "One folder copied to the root of its volume.") + mounted + ram;
  }
  const sizes = STATE.data.importSizes || {};
  const everything = importSourcePaths();
  $$("#im_maps .im-map").forEach(row => {
    if (row.dataset.medium === "memory") return;
    $(".imm-folder", row).placeholder = importFolderName($(".imm-remote", row).value, $(".imm-mount", row).value);
    const path = $(".imm-remote", row).value.trim().replace(/\/+$/, ""), readout = $(".imm-size", row);
    if (!readout) return;
    // A folder mapped in its own right never travels inside its parent, so say
    // so on the parent: otherwise the recordings look like they went missing.
    const inside = path ? everything.filter(other => other.startsWith(path + "/")) : [];
    const skips = inside.length ? ` · leaves out ${inside.map(other => other.slice(path.length)).join(", ")}, mapped separately` : "";
    readout.classList.toggle("bad", sizes[path] === "missing");
    readout.textContent = (!(path in sizes) ? ""
      : sizes[path] === "missing" ? "this folder does not exist on the source"
      : sizes[path] === null ? "size unknown — du timed out on this folder"
      : `${importBytes(sizes[path])} to copy`) + (sizes[path] === "missing" ? "" : skips);
  });
};
const importBytes = value => {
  const size = Number(value || 0);
  if (!size) return "";
  const units = ["B", "KB", "MB", "GB", "TB"];
  let index = 0, amount = size;
  while (amount >= 1024 && index < units.length - 1) { amount /= 1024; index++; }
  return `${amount >= 10 || index === 0 ? Math.round(amount) : amount.toFixed(1)} ${units[index]}`;
};

/* du walks every inode, so a deep appdata tree can take a while. Each folder
   is measured under its own timeout on the host: a slow one costs its own
   answer, not the whole measurement. */
window.imMeasure = async source => {
  const rows = $$("#im_maps .im-map");
  const paths = rows.filter(row => row.dataset.medium !== "memory")
    .map(row => $(".imm-remote", row).value.trim()).filter(path => path.startsWith("/"));
  if (!paths.length) return toast("add a source folder first", "bad");
  const button = $("#im_measure");
  if (button) { button.disabled = true; button.textContent = "Measuring…"; }
  try {
    const result = await api("/api/sources/measure", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name: source, paths }) });
    STATE.data.importSizes = Object.fromEntries((result.paths || [])
      .map(row => [row.path, row.measured ? row.bytes : row.exists === false ? "missing" : null]));
    // Each volume is sized from the folders pointed at it, not the total.
    const mappings = importMappings();
    $$("#im_volumes .im-volume").forEach(row => {
      const name = $(".imv-name", row).value.trim();
      if ($(".imv-kind", row).value === "existing") return;
      const bytes = mappings.filter(map => !map.medium && (map.pvc || name) === name)
        .reduce((sum, map) => sum + ((STATE.data.importSizes || {})[map.remote_path] || 0), 0);
      if (bytes) $(".imv-size", row).value = Math.max(1, Math.round(bytes / 1024 ** 3 * 1.25) + 1);
    });
    imSyncMaps();
    const note = $("#im_maps_note");
    if (note) {
      const copying = new Set(importMappings().filter(map => !map.medium).map(map => map.remote_path));
      const missing = (result.missing || []).filter(path => copying.has(path));
      note.innerHTML = `${importBytes(result.total_bytes)} measured across ${paths.length - missing.length} folder${paths.length - missing.length === 1 ? "" : "s"}`
        + (missing.length ? `. <b>${missing.map(esc).join(", ")} ${missing.length === 1 ? "does" : "do"} not exist on the source</b> — fix the path or untick the folder, or the copy will fail.`
          : result.complete ? ". Volume sizes set from what is actually there, with room to grow."
          : `, but some folders timed out after ${result.timeout_seconds}s — the sizes cover only what was measured.`);
    }
  } catch (e) { toast(e.message, "bad"); }
  finally { if (button) { button.disabled = false; button.textContent = "Measure sizes"; } }
};

/* Every folder listed, ticked or not: an unticked subfolder still has to be
   kept out of its parent's copy, or leaving it out achieves nothing. */
window.importSourcePaths = () => $$("#im_maps .im-map")
  .filter(row => row.dataset.medium !== "memory")
  .map(row => $(".imm-remote", row).value.trim())
  .filter(path => path.startsWith("/"))
  .map(path => path.replace(/\/+$/, ""));

/* Whether a folder row is in the import: copied, mounted empty, or left out. */
const imMode = row => row.dataset.medium === "memory" ? ($(".imm-on", row).checked ? "copy" : "skip")
  : ($(".imm-mode", row)?.value || "copy");

window.importMappings = () => {
  const all = importSourcePaths();
  return $$("#im_maps .im-map")
    .filter(row => imMode(row) !== "skip")
    .map(row => {
      if (row.dataset.medium === "memory") {
        return { medium: "memory", mount_path: $(".imm-mount", row).value.trim() || "/tmp/cache",
          size_mb: +$(".imm-ram", row).value || 1024 };
      }
      const remote = $(".imm-remote", row).value.trim().replace(/\/+$/, "");
      const copy = imMode(row) === "copy";
      return { remote_path: remote, mount_path: $(".imm-mount", row).value.trim() || "/config",
        folder: $(".imm-folder", row).value.trim(), pvc: $(".imm-pvc", row)?.value || "", copy,
        exclude: copy ? all.filter(path => path.startsWith(remote + "/")).map(path => path.slice(remote.length)) : [],
        bytes: copy ? Number((STATE.data.importSizes || {})[remote]) || 0 : 0 };
    });
};

window.importSetup = async (source, dir, cfg = {}) => {
  const src = (STATE.data.srcs || []).find(s => s.name === source) || {};
  const name = (cfg.name || dir).toLowerCase().replace(/[^a-z0-9-]/g, "-").replace(/^-+|-+$/g, "").slice(0, 38);
  STATE.data.importCfg = cfg;
  // Live nodes, so hardware plugged in since the page loaded is offered.
  const [storage, liveNodes] = await Promise.all([
    api("/api/deploy/options?ns=lab").catch(() => ({ pvcs: [], storage_classes: ["longhorn-r2"] })),
    api("/api/nodes").catch(() => [])]);
  if (liveNodes.length) STATE.data.nodes = liveNodes;
  STATE.data.importStorage = storage;
  const classes = storage.storage_classes?.length ? storage.storage_classes : ["longhorn-r2"];
  // A container that mounts nothing needs no storage: the folders and volumes
  // stay out of the way, offered only if someone wants to add some anyway.
  const keeps = (cfg.mounts || []).some(m => m.source || m.type === "tmpfs") || !!cfg.shm_mb;
  childModal("Import · " + dir, `
    <div class="f"><label>Workload name</label><input type="text" id="im_name" value="${esc(name)}"></div>
    <div class="f"><label>Remote path</label>
      <input type="text" id="im_path" value="${esc(cfg.remote_path || "")}" placeholder="${esc((src.base_path || "") + "/" + dir)}"></div>
    <div class="f"><label>Docker image ${tip("Read from Docker on the source host. You can change the tag before importing.")}</label>
      <input type="text" id="im_image" value="${esc(cfg.image || "")}" placeholder="lscr.io/linuxserver/${esc(name)}:latest"></div>
    <div class="f"><label>Logo URL</label><input type="url" id="im_icon" value="${esc(cfg.icon || "")}" placeholder="https://…/icon.png"></div>
    <div class="sec">Hardware requirements ${tip("Docker device mappings are pre-selected. Add or remove features before import; placement will be limited to nodes that provide every selected feature.")}</div>
    <div class="hwchoices">${hardwareChoices("im_hw", cfg.hardware || [])}</div>
    <div class="sec">Privileges ${tip("Read from Docker on the source: privileged mode, added capabilities, and the /dev/net/tun device a VPN needs.")}</div>
    ${privilegeFields("im_pv", cfg)}
    ${keeps ? "" : `<div class="note good" id="im_nothing">This container keeps nothing on disk, so there is nothing to copy and no
        volume to create. Import will bring across its image, ports and environment alone.
        <div style="margin-top:8px"><button class="btn sm" onclick="imStorageAnyway()">Add storage anyway</button></div></div>`}
    <div id="im_storage" ${keeps ? "" : "hidden"}>
    <div class="sec">Folders to copy ${tip("Every Docker path under the source appdata directory can come across. They all live in one Longhorn volume for this app, each in its own subfolder, mounted back where the container expects it.")}</div>
    <div class="note">Source folders → the volumes you define below → mounted back at each container path.</div>
    ${keeps && cfg.guessed_path ? `<div class="note warn">Nothing this container mounts sits under <span class="mono">${esc(src.base_path || "/mnt/user/appdata")}</span>,
      so Homestead cannot tell which folder holds its configuration. Tick the ones to copy yourself.</div>` : ""}
    <div id="im_maps">${importMappingRows(cfg, src).map(importMappingRow).join("")}</div>
    <div class="row"><button class="btn sm" onclick="imAddMap()">＋ add folder</button>
      <button class="btn sm" onclick="imAddScratch()">＋ add RAM scratch</button>
      <button class="btn sm" id="im_measure" onclick="imMeasure('${esc(source)}')">Measure sizes</button></div>
    <div class="dim xs" id="im_maps_note" style="margin-top:8px"></div>
    <details class="import-advanced"><summary class="dim small">Override file ownership (rarely needed)</summary>
      <div class="note">The copy keeps the ownership the files already had on the source, so the app finds
        its appdata exactly as it left it. Set these only when the container should run as a different user
        here than it did there.</div>
      <div class="f2"><div class="f"><label>Owner UID</label>
        <input type="number" id="im_uid" min="0" max="65535" placeholder="keep what the source had"></div>
        <div class="f"><label>Owner GID</label><input type="number" id="im_gid" min="0" max="65535" placeholder="same as UID"></div></div>
    </details>
    <div class="sec">Volumes ${tip("An import can fill more than one volume: appdata on a small replicated claim, recordings on a large one. Each folder above says which volume it goes to.")}</div>
    <div class="note">Appdata and bulk storage rarely want the same volume. Add a second one and point the
      heavy folders at it.</div>
    <div id="im_volumes"></div>
    <button class="btn sm" onclick="imAddVolume()">＋ add volume</button>
    </div>
    <div class="sec">Network</div><div class="f2"><div class="f"><label>Docker network → Kubernetes</label><select id="im_net"><option value="loadbalancer">LAN access (VIP)</option><option value="internal">Cluster only</option><option value="host" ${cfg.network_mode === "host" ? "selected" : ""}>Host network (advanced)</option></select></div>
      <div class="f"><label>VIP allocation ${tip("Choose a new automatic or specific VIP for apps such as Pi-hole that need port 53 on their own address.")}</label><select id="im_vip" onchange="$('#im_vip_wrap').style.display = this.value === 'manual' ? '' : 'none'"><option value="shared">Shared Homestead VIP</option><option value="auto">New automatic VIP</option><option value="manual">Specific VIP</option></select></div></div>
    <div class="f" id="im_vip_wrap" style="display:none"><label>Specific VIP</label><div id="im_vip_pick"><span class="dim xs"><span class="spin2"></span></span></div></div>
    <div class="sec">Port mappings ${tip("Container port is what the app listens on. LAN port is what you open from another device. TCP and UDP mappings are kept separately.")}</div>
    <div id="im_ports">${(cfg.ports || []).map(p => `<div class="f4 im-port"><div><label>Container</label><input class="ipc" type="number" value="${p.container}"></div><div><label>LAN</label><input class="iph" type="number" value="${p.host}"></div><div><label>Protocol</label><select class="ipp"><option ${p.protocol === "TCP" ? "selected" : ""}>TCP</option><option ${p.protocol === "UDP" ? "selected" : ""}>UDP</option></select></div><label class="switch"><input class="ipe" type="checkbox" ${p.expose !== false ? "checked" : ""}>Expose</label></div>`).join("")}</div>
    <button class="btn sm" onclick="imAddPort()">＋ add port</button>
    <div class="sec">Environment variables ${tip("Copied from Docker inspect. Review secrets and host-specific paths before starting the imported app.")}</div>
    <div id="im_env">${Object.entries(cfg.env || {}).map(([k,v]) => `<div class="f2 im-env"><div class="f"><label>Variable</label><input class="iek" value="${esc(k)}"></div><div class="f"><label>Value</label><input class="iev" value="${esc(v)}"></div></div>`).join("")}</div>
    <button class="btn sm" onclick="imAddEnv()">＋ add variable</button>
    <label class="switch"><input type="checkbox" id="im_start" checked> Leave stopped until the copy finishes</label>
    <div class="row" style="margin-top:16px">
      <button class="btn pri" onclick="doImport('${esc(source)}')">Start import</button>
      <button class="btn" onclick="closeModal()">Cancel</button></div>
    <div class="note" style="margin-top:14px">The copy runs as a Job — you can close this and watch it
    on the Import page, folder by folder. Large appdata directories can take a while.</div>`, true);
  if (keeps) $("#im_volumes").innerHTML = importVolumeRow({ name: `${name}-appdata`, size_gb: 10 }, 0);
  vipChoices().then(choices => { const host = $("#im_vip_pick"); if (host) host.innerHTML = vipPicker("im", "", choices); });
  imSyncVolumes();
};
/* Another Homestead on the network. The destination pulls, so these
   credentials are this cluster reaching out, not the far one reaching in. */
window.clusterAdd = () => modal("Add a Homestead cluster", `
  <p class="muted small">Connects to <b>another Homestead installation</b> running on a different
    Harvester cluster. Homestead must already be installed and reachable there. Use it to see what
    that cluster is running, and to move workloads across.</p>
  <div class="f"><label>Label ${tip("What you will call this cluster here. Any short name.")}</label>
    <input id="cl_name" placeholder="loft"></div>
  <div class="f"><label>Homestead address ${tip("The address you use to open the other Homestead in a browser, including the port.")}</label>
    <input id="cl_url" placeholder="http://192.0.2.242:8088"></div>
  <div class="sec">Sign in to that Homestead</div>
  <p class="muted small">A Homestead account on the <b>other</b> cluster — the username and password you
    would type into its own sign-in page. Not a Harvester, Rancher or SSH login.</p>
  <div class="f2"><div class="f"><label>Homestead username</label>
      <input id="cl_user" autocomplete="off" placeholder="admin"></div>
    <div class="f"><label>Homestead password</label>
      <input id="cl_pass" type="password" autocomplete="new-password"></div></div>
  <div class="note">The password is kept in a Kubernetes Secret, never in the ConfigMap that lists
    the clusters. Operator access there is enough to browse; moving a workload will need admin.</div>
  <div class="row" style="margin-top:16px">
    <button class="btn pri" data-need="admin" onclick="clusterSave()">Add cluster</button>
    <button class="btn" onclick="closeModal()">Cancel</button></div>`);

window.clusterSave = async () => {
  const body = { name: $("#cl_name").value.trim(), url: $("#cl_url").value.trim(),
    user: $("#cl_user").value.trim(), password: $("#cl_pass").value };
  if (!body.name || !body.url) return toast("name and address are required", "bad");
  try {
    await api("/api/move/clusters/add", { method: "POST",
      headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
    toast(`${body.name} added`, "ok"); closeModal(); resetPaint(); viewImport();
  } catch (e) { toast(e.message, "bad"); }
};

window.clusterDel = async name => {
  if (!confirm(`Forget ${name}?` + String.fromCharCode(10, 10)
      + "Its credentials are deleted. Nothing on that cluster is touched.")) return;
  try {
    await api("/api/move/clusters/remove", { method: "POST",
      headers: { "Content-Type": "application/json" }, body: JSON.stringify({ name }) });
    toast(`${name} forgotten`, "ok"); resetPaint(); viewImport();
  } catch (e) { toast(e.message, "bad"); }
};

/* Whether the two Homesteads can move workloads between them, and if not,
   which one to update. Same protocol on different releases still works. */
const CLUSTER_VERSION_TAGS = {
  same: ["ok", v => `v${v} · matches`],
  differs: ["info", v => `v${v} · differs`],
  behind: ["bad", v => `v${v || "?"} · too old`],
  ahead: ["bad", v => `v${v || "?"} · newer`],
  unreachable: ["warn", () => "no answer"],
  refused: ["bad", () => "unknown"],
};

window.clusterCheck = async name => {
  const host = $("#clver_" + name), note = $("#clvermsg_" + name);
  if (!host) return;
  host.innerHTML = '<span class="dim xs"><span class="spin2"></span> checking…</span>';
  if (note) note.innerHTML = "";
  let check;
  try {
    check = await api("/api/move/clusters/check", { method: "POST",
      headers: { "Content-Type": "application/json" }, body: JSON.stringify({ name }) });
  } catch (e) { check = { state: "refused", message: e.message }; }
  const [tone, label] = CLUSTER_VERSION_TAGS[check.state] || CLUSTER_VERSION_TAGS.refused;
  host.innerHTML = `<span class="tag ${tone}" title="${esc(check.message || "")}">${esc(label(check.version))}</span>`;
  // A reason to act gets a note; a harmless difference gets a quiet line.
  const trouble = check.compatible === false || ["unreachable", "refused"].includes(check.state);
  if (!note) return;
  if (trouble) note.innerHTML = `<div class="note ${tone === "warn" ? "warn" : "bad"} clvernote">${esc(check.message || "")}</div>`;
  else if (check.state === "differs") note.innerHTML = `<div class="dim xs clverdim">This cluster runs v${esc(HOMESTEAD_VERSION)}. Moves work between the two.</div>`;
  if (!trouble) clusterReady(name);
};

/* What a move from this cluster still needs, as steps with the fix beside
   each: the connection, then backup storage over there that this cluster
   can reach. */
window.clusterReady = async name => {
  const host = $("#clready_" + name);
  if (!host) return;
  host.innerHTML = '<span class="dim xs"><span class="spin2"></span> checking what a move needs…</span>';
  let r;
  try {
    r = await api("/api/move/clusters/readiness", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name }) });
  } catch (e) { host.innerHTML = `<div class="dim xs">${esc(e.message)}</div>`; return; }
  const target = r.target || {}, store = r.storage || {};
  (window.__clusterReady ||= {})[name] = r;
  const step = (ok, text, action = "") => `<div class="clstep ${ok ? "done" : "todo"}"><span>${ok ? "✓" : "•"}</span><div>${text}${action}</div></div>`;
  host.innerHTML = step(true, "Connected")
    + (target.configured && target.reachable_off_cluster && !target.answers
      ? step(false, `Backup storage on ${esc(name)} is at <span class="mono">${esc(target.endpoint || "")}</span>, but this cluster cannot reach it`,
          `<div class="dim xs">It may still be starting. Otherwise the address is taken by something else or firewalled - give it another.</div>
           <div><button class="btn sm" data-need="admin" onclick="clusterStorage('${esc(name)}',true)">Give it another address</button></div>`)
      : r.update_first
      ? step(false, `${esc(name)} runs Homestead v${esc(r.version?.version || "?")}, which sets up backup storage with MinIO - whose images can no longer be downloaded`,
          `<div class="dim xs">Update it to 2.8.111 or later, then set it up from here:</div><div class="mono xs">kubectl -n lab set image deployment/homestead homestead=ghcr.io/wjcloudy/homestead:2.8.111</div>`)
      : target.configured && target.reachable_off_cluster
      ? step(true, `Backup storage on ${esc(name)}`, `<div class="dim xs mono">${esc(target.url || "")} · ${esc(target.endpoint || "")}</div>`)
      : target.configured
        ? step(false, `Backup storage on ${esc(name)} has no LAN address, so this cluster cannot read it${target.endpoint ? ` - it is at <span class="mono">${esc(target.endpoint)}</span>, inside that cluster` : ""}`,
            `<div><button class="btn sm" data-need="admin" onclick="clusterStorage('${esc(name)}',true)">Give it an address</button></div>`)
        : store.deployed && !store.ready
          ? step(false, `Backup storage on ${esc(name)} is starting`, '<div class="dim xs">Its first start downloads the S3 server; this checks again every 15 seconds.</div>')
        : store.deployed
          ? step(false, `Backup storage is running on ${esc(name)}, but its Longhorn is not pointed at it yet`,
              `<div><button class="btn sm pri" data-need="admin" onclick="clusterStorage('${esc(name)}')">Finish setting it up</button></div>`)
        : step(false, `${esc(name)} has no backup storage yet`,
            `<div><button class="btn sm pri" data-need="admin" onclick="clusterStorage('${esc(name)}')">Set it up on ${esc(name)}</button></div>`))
    + step(r.ready, "Browse its workloads and move them here");
  if (window.applyRole) applyRole();
  clearTimeout(window["__clready_" + name]);
  if (store.deployed && !store.ready)
    window["__clready_" + name] = setTimeout(() => { if (STATE.view === "imports") clusterReady(name); }, 15000);
};

/* Backup storage on the other cluster, made from here with the account
   Homestead already holds for it - what its own Data protection page does. */
window.clusterStorage = (name, addressOnly = false, after = null) => {
  window.__clusterStorageAfter = after;
  window.__clusterStorageAddressOnly = addressOnly;
  const free = (window.__clusterReady?.[name]?.free_vips) || [];
  childModal(`Backup storage on ${name}`, `
    <p class="small">${addressOnly
      ? `The backup storage on <b>${esc(name)}</b> runs, but only inside that cluster. Give it an address on your LAN - one nothing else uses - and this cluster reads the backups from there.`
      : `Homestead puts an S3 store (RustFS) on a Longhorn volume on <b>${esc(name)}</b> and points that cluster's Longhorn backups at it.
        A move backs each volume up there, then restores it here. It shares ${esc(name)}'s disks, so it is for moving, not your only copy of anything.`}</p>
    <div class="f2">
      ${addressOnly ? "" : '<div class="f"><label>Size (GB)</label><input id="cs_size" type="number" min="5" value="100"></div>'}
      <div class="f"><label>Address for ${esc(name)}'s backup storage</label>
        ${free.length ? `<select id="cs_pick" onchange="$('#cs_ip').hidden = this.value !== '__typed'; if (this.value !== '__typed') $('#cs_ip').value = this.value">
            ${free.some(v => v.from === "vips") ? `<optgroup label="${esc(name)}'s VIPs (its Networking › Your VIPs)">${free.filter(v => v.from === "vips").map(v =>
              `<option value="${esc(v.ip)}">${esc(v.ip)}${v.label ? ` · ${esc(v.label)}` : ""}</option>`).join("")}</optgroup>` : ""}
            ${free.some(v => v.from !== "vips") ? `<optgroup label="Free in ${esc(name)}'s Harvester IP pools">${free.filter(v => v.from !== "vips").map(v =>
              `<option value="${esc(v.ip)}">${esc(v.ip)}</option>`).join("")}</optgroup>` : ""}
            <option value="__typed">Type an address…</option></select>
          <input id="cs_ip" class="mono" value="${esc(free[0].ip)}" hidden style="margin-top:6px">`
          : `<input id="cs_ip" class="mono" placeholder="e.g. 192.0.2.243">
            <div class="dim xs">${esc(name)} has no VIPs of its own and no free IP-pool address. Type one nothing else on your LAN uses,
              or add some under Networking › Your VIPs on ${esc(name)}'s Homestead.</div>`}</div></div>
    <div class="note" style="margin-top:10px"><b>This address belongs to ${esc(name)}</b>, the cluster sending the workloads: its backup store answers on it,
      announced by ${esc(name)}'s load balancer. This cluster never takes the address - it only connects to it to read the backups during a move.
      Both clusters share your LAN, so it just has to be one nothing else uses, outside your router's DHCP range.</div>
    <div class="row" style="margin-top:14px"><button class="btn pri" id="cs_go" onclick="clusterStorageGo('${esc(name)}')">${addressOnly ? "Set the address" : "Set it up"}</button>
      <button class="btn" onclick="modalBack()">Cancel</button></div>`);
};
window.clusterStorageGo = async name => {
  if (window.__clusterStorageAddressOnly && !$("#cs_ip").value.trim())
    return toast("choose the address this cluster should reach it at", "bad");
  const button = $("#cs_go");
  if (button) { button.disabled = true; button.textContent = "Working…"; }
  try {
    const r = await api("/api/move/clusters/storage", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name, size_gb: +($("#cs_size")?.value || 100), lb_ip: $("#cs_ip").value.trim() }) });
    toast(r.detail, "ok");
    modalBack();
    const after = window.__clusterStorageAfter;
    setTimeout(() => { clusterReady(name); if (after) after(); }, 1500);
  } catch (e) {
    toast(e.message, "bad");
    if (button) { button.disabled = false; button.textContent = "Set it up"; }
  }
};

window.clusterBrowse = async name => {
  const host = $("#cluster_" + name);
  if (host) host.innerHTML = '<span class="spin2"></span> asking…';
  try {
    const report = await api("/api/move/remote", { method: "POST",
      headers: { "Content-Type": "application/json" }, body: JSON.stringify({ name }) });
    STATE.data.remoteInventory = report;
    clusterInventory(report);
    if (host) host.textContent = `${report.workloads.length} workload${report.workloads.length === 1 ? "" : "s"}`
      + ` · ${report.movable} movable`;
  } catch (e) {
    if (host) host.innerHTML = `<span class="bad">${esc(e.message)}</span>`;
    toast(e.message, "bad");
  }
};

/* What the far cluster has, and plainly what of it cannot come. */
window.clusterInventory = report => {
  const rows = [...(report.workloads || []), ...(report.vms || [])];
  const theirs = report.version || "";
  modal(`Workloads on ${report.cluster}`, `
  <div class="dim xs" style="margin-bottom:10px">${esc(report.cluster)} runs Homestead
    ${theirs ? `<b class="mono">v${esc(theirs)}</b>` : "an older release that does not say which"};
    this one runs <b class="mono">v${esc(HOMESTEAD_VERSION)}</b>.</div>
  ${rows.length ? `<div class="tblwrap"><table class="tbl dense"><thead><tr>
    <th>Workload</th><th>Runs</th><th>Volumes</th><th>State</th><th></th></tr></thead><tbody>
    ${rows.map(w => `<tr>
      <td><b>${esc(w.name)}</b> ${w.kind === "vm" ? '<span class="tag">VM</span>' : ""}
        <div class="dim xs mono">${esc(w.namespace)}</div></td>
      <td class="mono small" style="word-break:break-all">${w.kind === "vm"
        ? `${esc(w.cores || "?")} cores · ${esc(w.memory || "?")}` : esc(w.image)}</td>
      <td class="small">${w.volumes.length
        ? w.volumes.map(v => `<div class="mono xs">${esc(v.claim)} · ${v.size_gb} GB${v.path ? ` → ${esc(v.path)}` : ""}</div>`).join("")
        : '<span class="dim">none</span>'}</td>
      <td><span class="tag ${w.running ? "ok" : ""}">${w.running ? "running" : "stopped"}</span></td>
      <td>${w.movable
        ? `<button class="btn sm" data-need="admin" onclick="moveReview('${esc(report.cluster)}','${esc(w.kind)}','${esc(w.name)}')">Move to this cluster</button>`
          + ((w.warnings || []).length ? `<div class="dim xs" style="max-width:240px;margin-top:4px">${w.warnings.map(esc).join("; ")}</div>` : "")
        : `<span class="tag bad">cannot move</span><div class="dim xs" style="max-width:240px">${w.blockers.map(esc).join("; ")}</div>`}</td>
    </tr>`).join("")}</tbody></table></div>`
    : '<div class="empty">That cluster is running nothing Homestead can see.</div>'}
  <div class="row" style="margin-top:16px"><button class="btn" onclick="closeModal()">Close</button></div>`);
};

/* Before anything stops: where it lands, what address it gets, and every
   reason it would fail or surprise someone - asked of both clusters. */
window.moveReview = (cluster, kind, name) => {
  childModal(`Move ${name} from ${cluster}`, `
  <p class="muted small">Stops ${esc(name)} on ${esc(cluster)}, backs up its volumes to the shared backup
    storage, restores them here, and starts it here. The original stays on ${esc(cluster)}, stopped,
    until you remove it, so it can be put back at any point before then.</p>
  <div class="f2"><div class="f"><label>Namespace here</label><input id="mv_ns" value="lab"></div>
    ${kind === "vm" ? "" : `<div class="f"><label>Address ${tip("The LAN address its Services get on this cluster. Shared uses this Homestead's own address; automatic takes a free one from the Harvester IP pool.")}</label>
      <select id="mv_mode" onchange="moveAddressMode(this.value)">
        <option value="shared">This cluster's shared address</option>
        <option value="automatic">Next free pool address</option>
        <option value="manual">A specific address</option></select></div>`}</div>
  <div class="f" id="mv_ip_wrap" hidden><label>Specific address</label><input id="mv_ip" placeholder="192.0.2.245"></div>
  <div id="mv_plan"></div>
  <div class="row" style="margin-top:16px">
    <button class="btn" onclick="movePlan('${esc(cluster)}','${esc(kind)}','${esc(name)}')">Check again</button>
    <button class="btn pri" id="mv_go" data-need="admin" disabled
      onclick="moveStart('${esc(cluster)}','${esc(kind)}','${esc(name)}')">Start move</button>
    <button class="btn" onclick="modalBack()">Cancel</button></div>`);
  movePlan(cluster, kind, name);
};

window.moveAddressMode = value => {
  const wrap = $("#mv_ip_wrap");
  if (wrap) wrap.hidden = value !== "manual";
};

const moveBody = (cluster, kind, name) => ({
  cluster, kind, name, namespace: $("#mv_ns")?.value.trim() || "lab",
  address_mode: $("#mv_mode")?.value || "shared", address: $("#mv_ip")?.value.trim() || "" });

window.movePlan = async (cluster, kind, name) => {
  const host = $("#mv_plan"), go = $("#mv_go");
  if (!host) return;
  host.innerHTML = '<div class="empty"><span class="spin2"></span> asking both clusters…</div>';
  if (go) go.disabled = true;
  try {
    const plan = await api("/api/move/plan", { method: "POST",
      headers: { "Content-Type": "application/json" }, body: JSON.stringify(moveBody(cluster, kind, name)) });
    const volumes = plan.claims || [];
    const fix = (plan.fixes || [])[0];
    host.innerHTML = `
      ${plan.blockers?.length ? `<div class="note bad"><b>This move would fail.</b><ul>${plan.blockers.map(b => `<li>${esc(b)}</li>`).join("")}</ul>
        ${fix ? `<button class="btn sm pri" data-need="admin" onclick="clusterStorage('${esc(cluster)}',${fix.kind === "source-address"},() => movePlan('${esc(cluster)}','${esc(kind)}','${esc(name)}'))">${fix.kind === "source-address"
          ? `Give ${esc(cluster)}'s backup storage an address` : `Set up backup storage on ${esc(cluster)}`}</button>` : ""}</div>` : ""}
      ${plan.warnings?.length ? `<div class="note warn"><b>Worth knowing first.</b><ul>${plan.warnings.map(w => `<li>${esc(w)}</li>`).join("")}</ul></div>` : ""}
      ${plan.ok ? `<div class="note good"><b>Ready to move.</b>
        ${volumes.length ? `${volumes.length === 1 ? "Its volume" : `Its ${volumes.length} volumes`} (${plan.total_gb} GB) ${volumes.length === 1 ? "goes" : "go"}
          through ${plan.joined ? "the backup storage both clusters already share" : `the backup storage on ${esc(cluster)}, which this cluster will be pointed at`}.`
          : "It has no volumes, so only its definition travels."}
        ${plan.addresses?.length ? `<br>Reachable here at ${plan.addresses.map(esc).join(", ")}.` : ""}
        ${plan.will_run ? "" : `<br>It is stopped on ${esc(cluster)}, and will arrive stopped.`}</div>` : ""}
      ${volumes.length ? `<div class="drow"><div class="dl">Volumes</div><div class="dv mono xs">${volumes.map(c =>
        `${esc(c.claim)} · ${c.size_gb} GB${c.volume_mode === "Block" ? " · disk" : ""}`).join("<br>")}</div></div>` : ""}`;
    if (go) go.disabled = !plan.ok;
  } catch (e) {
    host.innerHTML = `<div class="note bad">${esc(e.message)}</div>`;
  }
};

window.moveStart = async (cluster, kind, name) => {
  if (!confirm(`Stop ${name} on ${cluster} and bring it here?` + String.fromCharCode(10, 10)
      + "It is unavailable from the moment it stops there until it starts here.")) return;
  try {
    await api("/api/move/start", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(moveBody(cluster, kind, name)) });
    toast(`moving ${name} from ${cluster}; follow it here or in Activity`, "ok");
    closeModal(); resetPaint(); viewImport();
  } catch (e) { toast(e.message, "bad"); }
};

const MOVE_PHASE_WORDS = { joining: "Share storage", quiescing: "Stop there", "backing-up": "Back up",
  syncing: "See backups", restoring: "Restore here", creating: "Create here", starting: "Start here" };

function movesHtml(moves) {
  return (moves || []).map(m => {
    const tone = m.status === "succeeded" ? "ok" : m.status === "failed" ? "crit"
      : m.status === "cancelled" ? "low" : "warn";
    const steps = m.phases.filter(p => p !== "done").map((phase, index) => {
      const state = m.status === "succeeded" || index < m.phase_index ? "done"
        : index === m.phase_index ? (m.status === "failed" ? "failed" : m.status === "running" ? "active" : "")
        : "";
      return `<div class="${state}"><i></i><span><b>${esc(MOVE_PHASE_WORDS[phase] || phase)}</b></span></div>`;
    }).join("");
    const actions = [
      m.status === "failed" ? `<button class="btn sm" data-need="admin" onclick="moveAct('retry','${m.id}')">Retry</button>` : "",
      m.status === "succeeded" && !m.source_removed
        ? `<button class="btn sm" data-need="admin" onclick="moveFinish('${m.id}','${esc(m.name)}','${esc(m.cluster)}')">Remove from ${esc(m.cluster)}</button>` : "",
      ["running", "failed", "succeeded"].includes(m.status) && !m.source_removed
        ? `<button class="btn sm danger" data-need="admin" onclick="moveBack('${m.id}','${esc(m.name)}','${esc(m.cluster)}','${m.status}')">Put back</button>` : "",
      ["succeeded", "cancelled"].includes(m.status)
        ? `<button class="btn sm" data-need="admin" data-tip="${m.status === "succeeded" && !m.source_removed ? `Clear it from this list. ${esc(m.cluster)} keeps its stopped copy until you remove it there.` : "Clear it from this list"}"
            onclick="moveDismiss('${m.id}')">Dismiss</button>` : "",
    ].join("");
    const started = Math.max(0, (Date.now() - Date.parse(m.created_at)) / 1000);
    return `<div class="card flat moveitem">
      <div class="between"><div><div class="ctitle">${esc(m.name)} <span class="dim">from ${esc(m.cluster)}</span>
        ${m.kind === "vm" ? '<span class="tag">VM</span>' : ""}</div>
        <div class="csub">into ${esc(m.namespace)} · started ${esc(started < 90 ? "just now" : fmtAgo(started))}</div></div>
        <span class="pill ${tone}">${esc(m.status)}</span></div>
      <div class="rollout-meter"><span style="width:${Math.max(2, m.progress)}%"></span></div>
      <div class="rollout-steps movesteps">${steps}</div>
      <div class="between"><span class="dim xs">${esc(m.message || "")}</span><div class="row">${actions}</div></div>
    </div>`;
  }).join("");
}

/* Only the moves section repaints while one runs, so a form half-filled
   elsewhere on the page is left alone. */
async function watchMoves() {
  clearTimeout(window.__moveTimer);
  if (STATE.view !== "imports") return;
  const moves = await api("/api/move/moves").catch(() => null);
  const host = $("#movesList");
  if (moves && host) host.innerHTML = movesHtml(moves);
  if (window.applyRole) window.applyRole();
  if ((moves || []).some(m => m.status === "running")) window.__moveTimer = setTimeout(watchMoves, 4000);
}
window.watchMoves = watchMoves;

window.moveAct = async (action, id) => {
  try {
    await api(`/api/move/moves/${action}`, { method: "POST",
      headers: { "Content-Type": "application/json" }, body: JSON.stringify({ id }) });
    toast(action === "retry" ? "retrying from where it stopped" : "putting it back", "ok");
    watchMoves();
  } catch (e) { toast(e.message, "bad"); }
};

window.moveBack = (id, name, cluster, status) => {
  const landed = status === "succeeded";
  if (!confirm(`Put ${name} back on ${cluster}?` + String.fromCharCode(10, 10)
      + "It starts again there, as it was, and what this move created here is removed"
      + (landed ? ", including anything written to it here since it arrived." : "."))) return;
  moveAct("abandon", id);
};

window.moveFinish = (id, name, cluster) => modal(`Remove ${name} from ${cluster}`, `
  <p>${esc(name)} is running here. Removing the stopped original from ${esc(cluster)} makes the move
    permanent: after this it cannot be put back.</p>
  <label class="switch"><input type="checkbox" id="mv_vols"> Also delete its volumes on ${esc(cluster)}</label>
  <div class="note">Leaving the volumes costs space on ${esc(cluster)} but keeps a copy of the data as it
    was at the moment of the move. Their backups stay in the backup storage either way.</div>
  <div class="row" style="margin-top:16px">
    <button class="btn danger" data-need="admin" onclick="moveFinishNow('${id}')">Remove from ${esc(cluster)}</button>
    <button class="btn" onclick="closeModal()">Not yet</button></div>`);

window.moveFinishNow = async id => {
  try {
    const result = await api("/api/move/moves/finish", { method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ id, volumes: !!$("#mv_vols")?.checked }) });
    toast(result.message || "source removed", "ok");
    closeModal(); watchMoves();
  } catch (e) { toast(e.message, "bad"); }
};

window.imAddPort = () => { $("#im_ports").insertAdjacentHTML("beforeend", '<div class="f4 im-port"><div><label>Container</label><input class="ipc" type="number"></div><div><label>LAN</label><input class="iph" type="number"></div><div><label>Protocol</label><select class="ipp"><option>TCP</option><option>UDP</option></select></div><label class="switch"><input class="ipe" type="checkbox" checked>Expose</label></div>'); };
window.imAddEnv = () => { $("#im_env").insertAdjacentHTML("beforeend", '<div class="f2 im-env"><div class="f"><label>Variable</label><input class="iek"></div><div class="f"><label>Value</label><input class="iev"></div></div>'); };
window.doImport = async source => {
  const env = {}; $$(".im-env").forEach(r => { const k = $(".iek", r).value.trim(); if (k) env[k] = $(".iev", r).value; });
  const cfg = STATE.data.importCfg || {};
  const volumes = importVolumes();
  const mappings = importMappings();
  const first = volumes[0];
  // A container that keeps nothing imports as just the workload. Demanding a
  // volume for it would mean creating storage nobody asked for.
  const keepsNothing = !mappings.some(row => !row.medium && (row.remote_path || row.copy === false));
  if (!first && !keepsNothing) return toast("add at least one volume", "bad");
  const existing = !!first && !first.create;
  const body = { source, name: $("#im_name").value.trim(), remote_path: $("#im_path").value.trim(),
    image: $("#im_image").value.trim(), icon: $("#im_icon").value.trim(),
    mappings, volumes: keepsNothing ? [] : volumes,
    pvc_name: first?.name || "", size_gb: first?.size_gb || 0, reuse_existing: existing,
    storage_class: first?.storage_class || "", access_mode: first?.access_mode || "",
    start_after_copy: $("#im_start").checked,
    ports: $$(".im-port").map(r => ({ container: +$(".ipc", r).value, host: +$(".iph", r).value || +$(".ipc", r).value, protocol: $(".ipp", r).value, expose: $(".ipe", r).checked })).filter(p => p.container),
    env, hardware: selectedHardware("im_hw"), ...(readPrivileges("im_pv") || {}), network_mode: $("#im_net").value, vip_mode: $("#im_vip").value, lb_ip: ($("#im_lb_ip")?.value || "").trim(),
    uid: $("#im_uid").value.trim(), gid: $("#im_gid").value.trim() };
  if (!body.name || !body.image) return toast("workload name and image are required", "bad");
  for (const volume of volumes) {
    const measured = body.mappings.filter(row => row.pvc === volume.name)
      .reduce((sum, row) => sum + (row.bytes || 0), 0);
    if (!measured) continue;
    const claim = volume.create ? null : (STATE.data.vols || []).find(vol => vol.pvc_name === volume.name);
    const capacityGb = volume.create ? volume.size_gb : (claim?.size_gb || 0);
    const usedGb = volume.create ? 0 : (claim?.actual_gb || 0);
    const neededGb = measured / 1024 ** 3;
    if (capacityGb && neededGb > capacityGb - usedGb) {
      return toast(`${volume.name}: ${neededGb.toFixed(1)} GiB to copy but only ${Math.max(0, capacityGb - usedGb).toFixed(1)} GiB free`
        + (usedGb ? ` (${usedGb.toFixed(1)} GiB already written)` : "") + " — grow it or raise its size", "bad");
    }
  }
  // A RAM scratch mapping has no source by design, so only copied folders are
  // asked for one.
  const copied = body.mappings.filter(row => !row.medium && row.copy !== false);
  const bad = body.mappings.find(row => row.medium || row.copy === false
    ? !String(row.mount_path || "").startsWith("/")
    : !String(row.remote_path || "").startsWith("/") || !String(row.mount_path || "").startsWith("/"));
  if (bad) {
    return toast(bad.medium
      ? `a RAM scratch volume needs an absolute path inside the container (${esc(bad.mount_path || "blank")})`
      : `every folder needs an absolute source and container path (${esc(bad.remote_path || "blank")})`, "bad");
  }
  const reused = keepsNothing ? [] : volumes.filter(volume => !volume.create).map(volume => volume.name);
  const absent = copied.find(row => (STATE.data.importSizes || {})[row.remote_path] === "missing");
  if (absent) return toast(`${absent.remote_path} does not exist on the source — fix the path or untick it`, "bad");
  body.remote_path = copied[0]?.remote_path || "";
  body.mount_path = copied[0]?.mount_path || "/config";
  if (reused.length && !confirm(`Import into existing volume${reused.length === 1 ? "" : "s"} ${reused.join(", ")}?` +
      "\n\nThe current data is kept, but imported files with the same names may be replaced.")) return;
  try {
    const r = await api("/api/import", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
    toast(`import started (${r.job})`, "ok"); closeModal(); resetPaint(); viewImport();
  } catch (e) { toast(e.message, "bad"); }
};

/* Finished moves off the list, leaving the source's stopped copy where it is. */
window.moveDismiss = async (id = "") => {
  const rows = (await api("/api/move/moves").catch(() => [])).filter(m => (!id || m.id === id) && m.status === "succeeded" && !m.source_removed);
  if (rows.length && !confirm(`Clear ${id ? "this move" : "finished moves"} from the list?` + String.fromCharCode(10, 10)
      + `${[...new Set(rows.map(m => m.cluster))].join(", ")} keeps the stopped original of ${rows.map(m => m.name).join(", ")} - `
      + "nothing is removed there, and Put back is no longer offered here.")) return;
  try {
    const r = await api("/api/move/moves/dismiss", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ id }) });
    toast(r.detail, "ok");
    const host = $("#movesList");
    if (host) host.innerHTML = movesHtml(await api("/api/move/moves").catch(() => []));
    if (window.applyRole) applyRole();
  } catch (e) { toast(e.message, "bad"); }
};

/* Storage for an import that has none on the source: a volume and a folder
   row to fill in, only because someone asked. */
window.imStorageAnyway = () => {
  $("#im_storage").hidden = false;
  $("#im_nothing")?.remove();
  if (!$$("#im_volumes .im-volume").length) {
    const base = $("#im_name")?.value.trim() || "app";
    $("#im_volumes").innerHTML = importVolumeRow({ name: `${base}-appdata`, size_gb: 10 }, 0);
  }
  if (!$$("#im_maps .im-map").length) imAddMap();
  imSyncVolumes();
};
