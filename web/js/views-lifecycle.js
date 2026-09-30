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
      <div class="f"><label>Memory max (optional) ${tip("The container's memory ceiling. Exceeding it can cause an OOM kill and restart. Leave blank for no limit; it must be at least Memory reserved.")}</label><input id="e_mem_limit_${index}" type="text" value="${esc(container.memory_limit || "")}" placeholder="No limit · e.g. 1Gi"></div>
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
      <div class="sec">Service VIP</div><div class="note small">A Service VIP exposes ports on the default workload address or a VIP you select. It is separate from the direct LAN interface above.
        <p>Save container/port changes first, then configure its Service. VIP changes are applied separately and do not restart the pod.</p>
        <button class="btn" data-need="operator" onclick="networkManage(${jsq(ns)},${jsq(name)})">Configure default / selected VIP</button></div>
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
        <button class="btn pri" id="e_save" onclick="editSave(${jsq(ns)},${jsq(name)})">Save &amp; restart</button>
        <button class="btn" onclick="closeModal()">Cancel</button>
      </div>
      <div class="note" style="margin-top:14px">Saving rolls the pod. Renaming is a separate, reviewed action with a short outage; volumes and service addresses are kept. If it stops part-way, inspect the job before restarting either workload.</div>`;
    containers.forEach((container, index) => renderVolumeRows(editVolumePicker(index),
      (container.volumes || []).filter(volume => !volume.managed).map(editVolumeRow)));
    window.__editHadService = !!w.has_service;
    editPortsChanged();
    window.__editPortBaseline = editPortSignature();
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
function editPortSignature() {
  return JSON.stringify($$("#e_containers .edit-port-row").map(row => $$("input,select", row).map(e => e.type === "checkbox" ? e.checked : e.value)));
}
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
let EDIT_REVIEW = null, EDIT_REVIEW_SEQUENCE = 0;
window.editSave = async (ns, name) => {
  const workloadName = $("#e_workload_name").value.trim();
  if (workloadName !== name) return window.editReview({ns, name, workload_name: workloadName});
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
      memory: $("#e_mem_" + index).value.trim(), memory_limit: $("#e_mem_limit_" + index).value.trim(),
      hardware: selectedHardware("e_hw_" + index), env, ports,
      privileges: readPrivileges("e_pv_" + index) || undefined,
      volumes: readVolumeRows($("#e_vols_" + index)) };
  });
  const storageIssue = containers.map(container => volumeListIssue(container.volumes)).find(Boolean);
  if (storageIssue) return toast(storageIssue, "bad");
  const seed_configs = $$("#mbody .e_seed").map(el => ({
    init_container: el.dataset.init, config_map: el.dataset.configMap,
    key: el.dataset.key, value: el.value,
  }));
  const body = { ns, name, workload_name: workloadName, pod_hostname: $("#e_pod_name").value.trim(),
    icon: $("#e_icon").value.trim(), replicas: Math.max(1, +$("#e_rep").value || 1),
    autostart: $("#e_autostart").checked, manage_ports: true, containers, seed_configs,
    placement: readPlacement(), failover: $("#e_failover")?.value || "" };
  if ($("#e_lan_on")) body.lan = $("#e_lan_on").checked && $("#el_ip") ? containerLanRead("el") : null;
  const nodeSelect = $("#e_node");
  if (nodeSelect.value !== (nodeSelect.dataset.current || "")) body.node = nodeSelect.value || null;
  if ((STATE.data.wl || []).some(x => x.self && x.ns === ns && x.name === name) && !body.autostart) {
    if (!(await ask(`Turning autostart off stops Homestead, and this page with it. Nothing here can start it again - it stays down until someone runs\n\n  kubectl -n ${ns} scale deployment/${name} --replicas=1\n\non the cluster. Stop it anyway?`))) return;
    body.confirm_self = true;
  }
  await window.editReview(body);
};
function storageCopyReview(config, capacity) {
  const where = (claim, folder) => esc(folder ? `${claim}/${folder}` : claim);
  const rows = (config.containers || []).flatMap(c => (c.volumes || []).filter(v => v.copy_from).map(v => [
    `<b>${esc(c.name)}</b><br>${esc(v.path)}`, where(v.copy_from.claim, v.copy_from.sub_path),
    where(v.source, v.sub_path) + (v.copy_from.data === false ? '<br><span class="muted">Prepare empty; no files copied.</span>' : "")
  ]));
  return UI.lead('The app stops while its storage changes. Follow progress in <b>Recent jobs</b>.') +
    UI.callout(capacity.blocked ? "bad" : "warn", capacity.blocked ? "This move is blocked" : "Check your backup first",
      'Existing destination files may be overwritten. Volumes are kept, but this is not a backup.') +
    UI.section("Storage changes", UI.table([{label:"Container path"},{label:"From"},{label:"To"}],rows)) +
    UI.lead("Placement is checked again before copying and restarting. Failed or uncertain steps stop for inspection—no automatic rollback or retry.") +
    UI.more("Copy safety checks", "The helper checks destination paths and estimates free space before copying. Unavailable estimates appear in its log; they are not a guarantee that the data fits. A failed copy keeps both volumes and the app stays stopped. Inspect Recent jobs before retrying.") +
    UI.more("Capacity and placement details", UI.section("After the move",deployCapacityHtml(capacity)) +
      UI.section("Temporary copy helper",deployCapacityHtml(capacity.copy_helper)), capacity.blocked) +
    (!capacity.blocked ? UI.ack("editCapacityConfirm","I accept the outage, destination changes and capacity warnings") : "") +
    UI.actions(UI.button("Back to edit","modalBack()") + UI.button("Start data move","confirmEdit()",{id:"editGo",kind:"pri",disabled:capacity.blocked}));
}
window.editReview = async body => {
  EDIT_REVIEW = null;
  const sequence = ++EDIT_REVIEW_SEQUENCE;
  const config = JSON.parse(JSON.stringify(body));
  try {
    const review = await api("/api/edit/preview", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(config) });
    if (sequence !== EDIT_REVIEW_SEQUENCE) return;
    if (!review.capacity || !review.capacity_token) throw new Error("Capacity review unavailable; refresh before saving.");
    EDIT_REVIEW = { config, ...review, submitting: false };
    const rename = review.capacity.rename;
    const copying = !!review.capacity.copy_helper;
    if (copying) {
      childModal("Move container data", storageCopyReview(config,review.capacity), true, "operation-review");
      return;
    }
    childModal(rename ? "Rename workload" : "Review workload changes", `
      ${rename ? `<p><b>${esc(rename.from)}</b> → <b>${esc(rename.to)}</b></p><p>Only the workload name changes. Save other edits separately. Expect a short outage; volumes and service addresses are kept.</p>` : ""}
      ${rename ? `<div class="note ${review.capacity.blocked ? "bad" : ""}">${review.capacity.blocked ? "Rename is blocked by the placement check. Review the details below." : "If a step fails, inspect both workload names in Recent jobs. Homestead will not automatically restart the old copy or remove the replacement."}</div>
        <details ${review.capacity.blocked ? "open" : ""}><summary>Capacity and placement · ${(review.capacity.warnings || []).length} warning(s)</summary>${deployCapacityHtml(review.capacity)}</details>` : deployCapacityHtml(review.capacity)}
      ${!review.capacity.blocked ? `<label class="switch"><input type="checkbox" id="editCapacityConfirm"> ${rename ? "I accept the outage and capacity warnings" : "Proceed despite capacity warnings — I accept the restart, placement, memory and storage risks"}</label>` : ""}
      <div class="modalactions"><button class="btn" onclick="modalBack()">Back to edit</button><button id="editGo" class="btn pri" ${review.capacity.blocked ? "disabled" : ""} onclick="confirmEdit()">${rename ? "Rename workload" : "Save reviewed changes"}</button></div>`, true, "operation-review");
  } catch (e) { toast(e.message, "bad"); }
};
window.confirmEdit = async () => {
  const review = EDIT_REVIEW;
  if (!review || review.submitting || review.capacity.blocked || !$("#editCapacityConfirm")?.checked)
    return toast("Review the changes and acknowledge their warnings first", "bad");
  const body = { ...review.config, capacity_token: review.capacity_token, confirm_capacity: true };
  const { ns, name, workload_name: workloadName } = body;
  const renaming = workloadName && workloadName !== name;
  review.submitting = true;
  const button = $("#editGo");
  button.disabled = true;
  button.textContent = renaming ? "Renaming… follow Recent jobs" : "Saving reviewed changes…";
  if (renaming) window.startOperationChecks?.();
  try {
    const result = await api("/api/edit", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
    const activeName = result.name || workloadName || name;
    if (result.operation) window.noteOperation?.(result.operation);
    toast(renaming ? `${name} renamed to ${activeName}` : result.operation ? `${activeName} saved; copying its data in the job tray` :
      result.network || (renaming ? `${name} renamed to ${activeName}` : `${activeName} updated`), "ok");
    EDIT_REVIEW = null;
    closeModal(); setTimeout(() => refresh(true), 1200);
  } catch (e) {
    EDIT_REVIEW = null;
    window.startOperationChecks?.();
    toast(e.message, "bad");
    button.disabled = false;
    button.textContent = "Review again";
    button.onclick = () => window.editReview(review.config);
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
      <button class="btn pri" onclick="doMove(${jsq(ns)},${jsq(name)})">Move</button>
      <button class="btn" onclick="closeModal()">Cancel</button></div>
    <div class="note" style="margin-top:14px">This is a stop-then-start, not a live move —
    a ReadWriteOnce volume can only attach to one node at a time.</div>`);
};
window.doMove = async (ns, name) => {
  await window.hostMoveReview({ ns, name, node: $("#mv_node").value || null, pin: true });
};

/* ---------------- node power ---------------- */
window.nodeActions = async name => {
  let qr = {}, impact = { workloads: [], stranded: [] };
  try { [qr, impact] = await Promise.all([api("/api/quorum"), api(`/api/node/impact?node=${encodeURIComponent(name)}`)]); }
  catch (e) {
    return childModal("Host actions unavailable", `<div class="note bad">The host-impact checks could not be completed: ${esc(e.message)}. No power action is available until they work.</div>
      <button class="btn" onclick="closeModal()">Close</button>`);
  }
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
          <button class="btn" onclick="nodeCordon(${jsq(name)},true)">Cordon</button>
          <button class="btn" onclick="nodeCordon(${jsq(name)},false)">Uncordon</button>
          <button class="btn" onclick="nodeDrain(${jsq(name)})">Drain</button>
          <button class="btn" onclick="evacuateNode(${jsq(name)})">Move all off</button>
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
      <button class="btn danger" data-need="admin" onclick="nodeRemoval(${jsq(name)})">Remove from cluster…</button></div>
    <div class="sec">Power</div>
    ${off ? `<div class="note"><b>Host power control is disabled.</b> Rebooting needs a privileged
        helper pod that enters the host namespaces. This installation has disabled it. Set
        <span class="mono">ENABLE_NODE_POWER=true</span> on the Homestead Deployment to enable it.
        Cordon and drain above work regardless.</div>`
      : risky ? `<div class="note" style="border-color:rgba(255,77,79,.35);background:rgba(255,77,79,.08);color:#ffb4b8">
        <b>Blocked.</b> ${(qr.ready || []).length} of ${qr.total} etcd members are ready and quorum needs
        ${qr.quorum_needs}. ${qr.total === 1 ? "This is the cluster's only member: while it is down, so is the cluster - Homestead with it - until it is back."
          : "Taking this host down would lose the cluster. Bring the other members back first."}</div>
        <label class="switch" data-need="admin" style="margin-top:10px"><input type="checkbox" onchange="$('#pw_forced_row').hidden = !this.checked">
          Override - reboot or shut down anyway</label>
        <div class="row" id="pw_forced_row" hidden style="margin-top:8px">
          <button class="btn danger" data-need="admin" onclick="nodePowerReview(${jsq(name)},'reboot',true)">Review forced reboot…</button>
          <button class="btn danger" data-need="admin" onclick="nodePowerReview(${jsq(name)},'poweroff',true)">Review forced shutdown…</button>
        </div>`
      : `<p class="muted small">Review fresh workload, VM, quorum and Longhorn replica impacts before either action. Homestead will cordon and wait for drained pods to leave before sending host power.</p>
      <div class="row">
        <button class="btn danger" onclick="nodePowerReview(${jsq(name)},'reboot')">Review reboot…</button>
        <button class="btn danger" onclick="nodePowerReview(${jsq(name)},'poweroff')">Review shutdown…</button>
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
let NODE_POWER_REVIEW_SEQUENCE = 0;
window.nodePowerReview = async (node, action, force = false) => {
  window.__nodePowerPlan = null;
  const sequence = ++NODE_POWER_REVIEW_SEQUENCE;
  let plan;
  try { plan = await api(`/api/node/power/plan?${new URLSearchParams({ node, action, ...(force ? { force: "1" } : {}) })}`); }
  catch (e) { return toast(`Could not assess this host: ${e.message}`, "bad"); }
  if (sequence !== NODE_POWER_REVIEW_SEQUENCE) return;
  if (plan.node !== node || plan.action !== action || !plan.review_token || typeof plan.ready !== 'boolean')
    return toast("Host review is incomplete; refresh before continuing", "bad");
  window.__nodePowerPlan = plan;
  const verb = action === "reboot" ? "Reboot" : "Shut down";
  const volumes = plan.volumes || [];
  const workloads = plan.workloads || [];
  const budgets = plan.maintenance?.budgets || [];
  const local = plan.maintenance?.local_storage || [];
  const risk = { unavailable: ["no healthy copy elsewhere", "bad"], "single-copy": ["one copy left · unprotected", "warn"], resync: ["replica resync needed", "info"] };
  const concerns = [...(plan.vms?.length ? [`VMs to check: ${plan.vms.join(", ")}. Their migration or shutdown must be verified separately.`] : []), ...(plan.warnings || [])];
  const overridable = !plan.force && (plan.overridable || []).length && !(plan.hard_blockers || []).length;
  const forced = window.__nodePowerForced = !!plan.force;
  childModal(`${forced ? "Forced " : ""}${verb.toLowerCase()} · ${node}`, [
    UI.lead(forced
      ? `${plan.pods} pod${plan.pods === 1 ? "" : "s"} and ${plan.vms?.length || 0} VM${plan.vms?.length === 1 ? "" : "s"} run on <b>${esc(node)}</b>. Forced: Homestead does not cordon or drain it - it asks the host's own systemd to ${action === "reboot" ? "reboot" : "power off"}, which stops everything in order, as its power button would.`
      : `${plan.pods} pod${plan.pods === 1 ? "" : "s"} and ${plan.vms?.length || 0} VM${plan.vms?.length === 1 ? "" : "s"} currently run on <b>${esc(node)}</b>. Homestead cordons it and waits for drained pods to leave; live migration and restart elsewhere are not guaranteed.`),
    plan.blockers?.length
      ? UI.callout("bad", "Blocked", `<ul class="ui-list">${plan.blockers.map(b => `<li>${esc(b)}</li>`).join("")}</ul>`)
        + (overridable ? `<label class="switch" data-need="admin"><input type="checkbox" onchange="if (this.checked) nodePowerReview(${jsq(node)},${jsq(action)},true)">
            Override these checks - ${esc(action === "reboot" ? "reboot" : "shut down")} anyway, without cordon or drain</label>` : "")
      : concerns.length ? UI.callout(forced ? "bad" : "warn", forced ? "Forced - what happens" : "Check before going ahead", `<ul class="ui-list">${concerns.map(c => `<li>${esc(c)}</li>`).join("")}</ul>`) : "",
    UI.section("What goes down", workloads.length
      ? UI.table([{ label: "Workload" }, { label: "During the outage" }], workloads.map(w => [`<span class="mono">${esc(w.ns)}/${esc(w.name)}</span>`,
        w.stranded ? UI.chip("no other eligible host", "bad") : `${UI.chip("may move", "ok")} <span class="sub">to ${esc((w.eligible || []).join(", "))}</span>`]))
      : `<div class="ui-empty">No user Deployments are mapped to this host.</div>`),
    UI.section("Volume copies during the outage", volumes.length
      ? UI.table([{ label: "Volume" }, { label: "Copies" }], volumes.map(v => [`<span class="mono">${esc(v.claim)}</span>`, UI.chip(...(risk[v.risk] || risk.resync))]))
      : `<div class="ui-empty">No Longhorn replica on this host was found.</div>`),
    budgets.length ? UI.section("Disruption budgets", UI.table([{ label: "Pod" }, { label: "Budget" }], budgets.map(b => [`<span class="mono">${esc(b.pod)}</span>`,
      `${esc(b.budget)} · ${b.allowed == null ? "status unknown" : `${b.allowed} disruption(s) allowed`}${b.unhealthy_allowed ? " · unhealthy eviction allowed" : ""}`]))) : "",
    local.length ? UI.section("Local and external storage", `<p class="ui-help">Drain deletes emptyDir data. Host-local paths do not move with pods. External storage may depend on this host; verify availability before proceeding.</p>`
      + UI.table([{ label: "Pod" }, { label: "Storage" }], local.map(v => [`<span class="mono">${esc(v.pod)}</span>`, `${esc(v.kind)} · ${esc(v.source)}`]))) : "",
    plan.ready ? UI.field(`Type ${node} to confirm`, `<input type="text" id="pw_confirm" autocomplete="off" placeholder="${esc(node)}">`) : "",
    plan.ready && plan.stranded?.length ? UI.ack("pw_allow", `I understand ${plan.stranded.length} workload${plan.stranded.length === 1 ? "" : "s"} may remain down`) : "",
    plan.ready && plan.requires_data_ack ? UI.ack("pw_data", "I understand the volume copies or storage visibility risk") : "",
    UI.more("After the request", "Follow Recent jobs for the helper's events, logs and host status. Reboot checks use a changed boot ID; shutdown cannot be confirmed from NotReady alone. The host stays cordoned until you inspect it and allow scheduling."),
    UI.actions(plan.ready ? UI.cancel() + UI.button(`${forced ? "Force " + verb.toLowerCase() : verb + " host"}`, `nodePower(${jsArg(node)},${jsArg(action)})`, { kind: "danger", id: "pw_execute" }) : UI.cancel("Close")),
  ].join(""), true);
};
window.nodePower = async (node, action) => {
  const plan = window.__nodePowerPlan;
  if (!plan || !plan.ready || plan.node !== node || plan.action !== action) return toast("review the host impact again", "bad");
  const c = $("#pw_confirm")?.value.trim();
  if (c !== node) return toast("type the host name exactly to confirm", "bad");
  if (plan.stranded?.length && !$("#pw_allow")?.checked)
    return toast("confirm the workloads that will remain down", "bad");
  if (plan.requires_data_ack && !$("#pw_data")?.checked)
    return toast("confirm the volume risk", "bad");
  // Consume approval before sending, including double clicks and lost replies.
  window.__nodePowerPlan = null;
  const button = $("#pw_execute");
  if (button) { button.disabled = true; button.textContent = plan.force ? "Sending…" : "Draining host…"; }
  try {
    const r = await api("/api/node/power", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ node, action, confirm: c, review_token: plan.review_token, force: !!plan.force,
        allow_stranded: !!$("#pw_allow")?.checked, allow_data_risk: !!$("#pw_data")?.checked }) });
    if (r.operation) window.noteOperation?.(r.operation);
    modal("Host maintenance", UI.lead(plan.force ? `Sent. Follow it in Recent jobs${plan.action === "reboot" ? "; this page comes back when the host does" : ""}. It was not cordoned, so its pods start again as it comes back.`
      : "Follow progress in Recent jobs. The host stays cordoned; check it before allowing scheduling.") +
      UI.more("Steps so far", `<pre>${esc((r.steps || []).join("\n"))}</pre>`) + UI.actions(UI.cancel("Close")));
  } catch (e) {
    toast(e.message + " — inspect Recent jobs and the host before making another request.", "bad");
    if (button) {
      button.disabled = false; button.textContent = "Review host again";
      button.onclick = () => window.nodePowerReview(node, action);
    }
  } finally { window.refreshOperations?.(true); }
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
      <button class="btn pri" onclick="doVmMove(${jsq(ns)},${jsq(name)})">Migrate</button>
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
/* LAN networks a container can join; for a VM, only those on a bridge -
   macvlan cannot carry a VM's own MAC. */
function vmLanNetworks(opts, forVm = false) {
  // macvlan carries containers only, macvtap VMs only; a bridge both.
  return (opts.network_details || []).filter(n => n.lan && (forVm ? n.vms !== false : n.containers !== false));
}
function vmNetworkNote(opts, forVm = false) {
  if (vmLanNetworks(opts, forVm).length) return "";
  const other = forVm && vmLanNetworks(opts).length;
  return `<div class="note" style="margin-top:8px"><b>${other ? "No LAN network a VM can join yet." : "No LAN network yet."}</b>
    ${other ? "The ones there use macvlan, which carries containers only; a VM needs a macvtap network or one on a host bridge."
      : `A LAN network puts ${opts.harvester ? "VMs and containers" : "VMs or containers"} on your LAN, untagged like the hosts or on a VLAN.`}
    <button class="btn sm pri" data-need="admin" style="margin-top:6px" onclick="vmNetworkAdd()">＋ Make one</button></div>`;
}

/* A VM network, made here: the same object Harvester's dashboard makes under
   Networks > VM Networks, so it shows there too. When made from a form that
   needed one, that form opens again with it to choose. */
window.vmNetworkAdd = async (reopen = null) => {
  const opts = window.__vmCreateOptions || await api("/api/vm/create-options").catch(() => ({}));
  const back = reopen || window.__vmNetworkReopen || null;
  const o = opts.vm_network_options || {};
  const open = window.childModal && !$("#modal").classList.contains("hidden") ? childModal : modal;
  if (!o.harvester && !o.multus) {
    open("New LAN network", `<div class="note"><b>Multus is needed first.</b> A container or VM joins the LAN as a second network,
        which Kubernetes does through Multus - k3s and RKE2 leave it out unless asked.</div>
      <p class="small">${esc(o.multus_help || "Install Multus, then come back here.")}</p>
      <div class="row" style="margin-top:14px">${STATE.platform?.helm_controller && ["k3s", "rke2"].includes(STATE.platform?.distribution)
        ? `<button class="btn pri" data-need="admin" onclick="closeModal(); addonInstall('multus')">Install Multus</button>` : ""}
        <button class="btn" onclick="modalBack()">Close</button></div>`);
    window.__vmNetworkReopen = back;
    return;
  }
  const clusters = o.cluster_networks?.length ? o.cluster_networks : ["mgmt"];
  const ifaces = o.interfaces || [];
  const ifaceWord = i => `${i.name} · ${i.kind === "bridge" ? "bridge - VMs and containers" : `${i.kind}${i.master ? ` in ${i.master}` : ""}`}${i.everywhere ? "" : ` · only on ${i.nodes.join(", ")}`}`;
  const carrier = o.harvester
    ? `<div class="f"><label>Cluster network ${tip("Which of Harvester's cluster networks it rides on. mgmt is the hosts' own network - the usual choice.")}</label>
        <select id="vn_cluster">${clusters.map(c => `<option ${c === "mgmt" ? "selected" : ""}>${esc(c)}</option>`).join("")}</select></div>`
    : `<div class="f"><label>Host interface ${tip("The interface on each host that your LAN is on. A bridge (br0) carries VMs and containers. On a plain NIC (eth0), a network carries VMs (macvtap) or containers (macvlan), not both. Every host needs one of the same name.")}</label>
        ${ifaces.length ? `<select id="vn_iface" onchange="vmNetworkForChanged()">${ifaces.map(i => `<option value="${esc(i.name)}" data-kind="${esc(i.kind)}">${esc(ifaceWord(i))}</option>`).join("")}</select>`
          : `<input id="vn_iface" class="mono" placeholder="eth0 or br0" oninput="vmNetworkForChanged()">`}</div>`;
  const forVms = back === "vm" || back === "k3s";
  const usedBy = o.harvester ? "" : `<div class="f" id="vn_for_row"><label>For ${tip("A plain NIC can carry VMs, through macvtap, or containers, through macvlan - each with an address of its own on your LAN. Make one network of each to have both. Through macvtap a VM cannot be reached from its own host; other machines on the LAN reach it.")}</label>
      <select id="vn_for" onchange="vmNetworkForChanged()"><option value="vms" ${forVms ? "selected" : ""}>Virtual machines (macvtap)</option>
        <option value="containers" ${forVms ? "" : "selected"}>Containers (macvlan)</option></select>
      <div id="vn_for_note"></div></div>`;
  open("New LAN network", `
    <p class="small" style="margin-top:0">VMs and containers on it are on your LAN, with addresses from your router's DHCP or ones of their own.</p>
    <div class="f2"><div class="f"><label>Name ${tip("How it is listed wherever a network is chosen, like lan or vlan20.")}</label><input id="vn_name" value="lan"></div>
      ${carrier}</div>
    ${!o.harvester && !ifaces.length ? '<div class="dim xs">The node probe has not reported the hosts\' interfaces, so type the name - <span class="mono">ip link</span> on a host lists them.</div>' : ""}
    ${usedBy}
    <div class="f"><label>VLAN ${tip("Empty: untagged - the same LAN the hosts are on. A number: that VLAN, which your switch must carry to the hosts.")}</label>
      <input id="vn_vlan" type="number" min="1" max="4094" placeholder="empty - untagged, the hosts' own LAN"></div>
    <div class="row" style="margin-top:14px"><button class="btn pri" onclick="vmNetworkAddGo()">Make it</button>
      <button class="btn" onclick="modalBack()">Cancel</button></div>`);
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
    ? `<div class="note small" style="margin-top:6px"><b>macvtap is not installed yet.</b> It is one of Homestead's required components where VMs run.
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
      <div class="f"><label>Subnet ${tip("From Networking › IP addresses: the addresses offered are free there, outside the DHCP range and the VIP pools.")}</label>
        <select id="${p}_subnet" onchange="vmSubnetPicked(${jsq(p)}, ${count})">${subnets.map(s => `<option value="${esc(s.cidr)}">${esc(s.cidr)}${s.name ? ` · ${esc(s.name)}` : ""} · ${s.free.length} free</option>`).join("")}
          <option value="">another - type it in</option></select></div>
      <div class="f"><label>${count > 1 ? "Addresses, one per node" : "Address"}</label>
        <input id="${p}_ip" class="mono" placeholder="${count > 1 ? "192.0.2.60, 192.0.2.61" : "192.0.2.60"}" list="${p}_free" data-ipam="${count > 1 ? "multi" : ""}">
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
  const lan = vmLanNetworks(window.__vmCreateOptions || {}, true).some(n => n.name === net);
  $("#v_addr_wrap").hidden = !lan;
  $("#v_static").hidden = !lan || $("#v_addr_mode").value !== "static";
  if ($("#v_service_wrap")) $("#v_service_wrap").hidden = net !== "pod";
  if ($("#v_service_pick")) $("#v_service_pick").hidden = $("#v_service").value !== "manual";
};

window.vmNew = async (selectedDisk = "", selectedNamespace = "") => {
  window.__vmNetworkReopen = "vm";
  const [opts, disks, vipOptions] = await Promise.all([
    api("/api/vm/create-options").catch(() => ({ harvester: !!STATE.platform?.harvester, cdi: true, storage_classes: [], images: [] })),
    api("/api/vm-disks").catch(() => []),
    vipChoices(),
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
        ${images.map(i => `<option value="image:${esc(i.namespace)}/${esc(i.name)}" data-size="${i.size_gb}" ${window.__vmPreset === `image:${i.namespace}/${i.name}` ? "selected" : ""}>Harvester image · ${esc(i.display)} (${i.size_gb}G)</option>`).join("")}
        ${(opts.store || []).length ? `<optgroup label="Image store - the publisher's newest build">${opts.store.map(s => `<option value="store:${esc(s.id)}" data-size="${s.min_gb}"
          ${window.__vmPreset === `store:${s.id}` ? "selected" : ""}>${esc(s.name)} · ${esc(s.variant)}${opts.harvester ? (s.kept ? (s.ready ? " · kept" : " · downloading") : " · downloads now") : ""}</option>`).join("")}</optgroup>` : ""}
        ${opts.cdi || opts.harvester ? `<option value="url">${opts.harvester ? "Download from a URL (as a Harvester image)" : "Download from HTTP(S) URL"}</option>` : ""}
        ${(opts.isos || []).length ? `<optgroup label="Install from an ISO - a blank disk, the ISO in its CD-ROM drive">${opts.isos.map(i => `<option value="iso:${esc(i.name)}">${esc(i.file)}</option>`).join("")}</optgroup>` : ""}
      </select>
      <div class="dim xs" style="margin-top:6px">Installing from an ISO? Make one ready in the <a class="linkish" onclick="vmIsoLibrary()">ISO library</a> first.</div></div>
    <div class="f" id="v_drivers_row" hidden><label>Drivers CD ${tip("A second CD-ROM for the installer's drivers. Windows setup sees no disk until it loads the VirtIO storage driver: attach virtio-win.iso here, and in setup choose Load driver, then vioscsi or viostor for your Windows version.")}</label>
      <select id="v_drivers"><option value="">none</option>${(opts.isos || []).map(i => `<option value="${esc(i.name)}" ${/virtio/i.test(i.file) ? "selected" : ""}>${esc(i.file)}</option>`).join("")}</select>
      <div class="dim xs" style="margin-top:4px">${(opts.isos || []).some(i => /virtio/i.test(i.file)) ? "" : "For Windows, make virtio-win.iso (from the Fedora project) ready in the ISO library."}</div></div>
    <div class="f"><label>Guest type ${tip("Firmware, TPM, clock and devices for the kind of guest, as Proxmox's OS type sets them. Adjust them under Hardware, now or later.")}</label>
      <select id="v_preset" onchange="vmCreatePreset(this.value)">
        <option value="">Default</option>${Object.entries(VM_PRESETS).map(([id, p]) => `<option value="${esc(id)}">${esc(p.name)}</option>`).join("")}</select>
      <div class="dim xs" id="v_preset_about" style="margin-top:4px">KubeVirt defaults: BIOS, UTC, a display and serial console</div></div>
    ${opts.hardware_base ? `<details class="ui-more" id="v_hw"><summary>Hardware - CPU model, firmware, TPM, devices, memory</summary>
      ${vmHardwareFields(opts.hardware_base, opts, false, true)}</details>` : ""}
    <div class="f" id="v_url_row" hidden><label>Image URL</label><input type="url" id="v_url" placeholder="https://cloud-images.ubuntu.com/…/img"></div>
    <div class="note small">A <b>Service VIP</b> forwards selected ports to a VM on the pod network. A <b>direct LAN interface</b> gets its address from DHCP or guest configuration; a MAC address only identifies that interface.</div>
    <div class="f"><label>Network ${tip("The pod network: reached through a Service, like a container. A LAN network (bridged): a machine there like any other, with an address from DHCP or one of its own.")}</label>
      <select id="v_net" onchange="vmNetChanged()"><option value="pod">Pod network - reached through a Service</option>
        ${(opts.network_details || []).filter(n => n.vms !== false).map(n => `<option value="${esc(n.name)}">${esc(n.name)}${n.lan ? ` · LAN${n.vlan ? ` (VLAN ${esc(n.vlan)})` : ""}` : ""}</option>`).join("")}</select>
      ${vmNetworkNote(opts, true)}</div>
    <div class="f"><label>Network card ${tip("The virtual card the guest sees. VirtIO is fastest but needs its driver: Linux has it, Windows needs the guest tools from virtio-win.iso. An Intel e1000e works in Windows with no extra drivers, which is what the Windows guest types choose.")}</label>
      <select id="v_nic_model"><option value="virtio">VirtIO</option><option value="e1000e">Intel e1000e</option><option value="e1000">Intel e1000</option><option value="rtl8139">Realtek RTL8139</option></select></div>
    <div id="v_service_wrap"><div class="f"><label>Service VIP</label><select id="v_service" onchange="vmNetChanged()"><option value="">Cluster only / configure later</option>${nodeAddressesOnly() ? "" : `${vipOptions.shared ? `<option value="shared">Default workload VIP · ${esc(vipOptions.shared)}</option>` : ""}<option value="manual">Selected VIP</option>`}</select></div>
      ${nodeAddressesOnly() ? '<p class="note small">ServiceLB uses node addresses, not movable VIPs. Install kube-vip or MetalLB before choosing a Service VIP.</p>' : ""}
      <div id="v_service_pick" class="f" hidden><label>Selected VIP</label>${vipPicker("vsvc", "", vipOptions)}</div>
      <p class="dim small">Two steps: create the VM, then review its VIP and port mappings before publishing them. If VIP setup is cancelled or fails, the VM remains created and can be configured from Edit → Network.</p></div>
    <div id="v_addr_wrap" hidden>
      <div class="f"><label>Address</label><select id="v_addr_mode" onchange="vmNetChanged();vmSubnetPicked('v')">
        <option value="dhcp">From the network's DHCP</option><option value="static">One of its own</option></select></div>
      <div id="v_static" hidden>${vmAddressFields("v", opts)}</div></div>
    ${(opts.storage_classes || []).length ? `<div class="f" id="v_sc_row"><label>Storage class ${tip(opts.harvester
      ? "Where a blank or downloaded disk lives. A disk from a Harvester image always lives on that image's own class."
      : "Where the disk lives. The cluster's default class is chosen for you.")}</label>
      <select id="v_sc">${opts.storage_classes.map(c => `<option value="${esc(c)}" ${c === opts.default_class ? "selected" : ""}>${esc(classLabel(c))}</option>`).join("")}</select></div>` : ""}
    <div class="row" style="margin-top:18px">
      <button class="btn pri" onclick="doVmCreate()">Review VM</button>
      <button class="btn" onclick="closeModal()">Cancel</button></div>
    <div class="note" style="margin-top:14px">${opts.harvester
      ? "New disks are made the way Harvester makes them: shared block volumes, so the VM can move between hosts. A URL is downloaded as a Harvester image, kept in its image list for the next VM."
      : opts.cdi ? `New disks are CDI DataVolumes on the class above, with the access mode that class supports.
        A VM on a disk only one host can reach stays on that host.`
      : `<b>CDI is not installed</b>, so a VM here starts from a blank disk that KubeVirt formats itself.
        To download or import disk images, install CDI (the containerized data importer) from kubevirt.io.`}
    ${readyDisks.length ? " Imported disks are attached directly and remain visible on the Import page." : ""}</div>`, true);
  window.__vmPreset = "";
  vmBootChanged();
  vmHardwareChanged();
};
window.vmBootChanged = () => {
  const boot = $("#v_boot")?.value || "";
  // An installer asks for its own password; cloud-init is for cloud images.
  if ($("#v_pass")) $("#v_pass").placeholder = boot.startsWith("iso:") ? "Not used: the installer asks" : "Set an initial password";
  if ($("#v_url_row")) $("#v_url_row").hidden = boot !== "url";
  if ($("#v_drivers_row")) $("#v_drivers_row").hidden = !boot.startsWith("iso:");
  // An import brings its own disk; a Harvester image brings its own class.
  if ($("#v_sc_row")) $("#v_sc_row").hidden = boot.startsWith("disk:") || boot.startsWith("image:");
  const size = +($("#v_boot")?.selectedOptions[0]?.dataset.size || 0);
  if (size && +$("#v_disk").value < Math.ceil(size)) $("#v_disk").value = Math.ceil(size);
};
let VM_CREATE_REVIEW = null, VM_CREATE_BUSY = false, VM_CREATE_SEQUENCE = 0;
window.doVmCreate = async () => {
  if (VM_CREATE_BUSY) return;
  const boot = $("#v_boot").value;
  const imported = boot.startsWith("disk:") ? boot.slice(5).split("/") : [];
  const body = { name: $("#v_name").value.trim(), cores: +$("#v_cores").value,
    memory: $("#v_mem").value.trim(), disk_gb: +$("#v_disk").value, password: $("#v_pass").value,
    namespace: imported[0] || "lab", disk_import: imported[1] || "",
    image_id: boot.startsWith("image:") ? boot.slice(6) : "",
    image_url: boot === "url" ? $("#v_url").value.trim() : "",
    store_id: boot.startsWith("store:") ? boot.slice(6) : "",
    install_iso: boot.startsWith("iso:") ? boot.slice(4) : "",
    drivers_iso: boot.startsWith("iso:") ? $("#v_drivers")?.value || "" : "",
    storage_class: $("#v_sc")?.value || "", network: $("#v_net")?.value || "pod",
    nic_model: $("#v_nic_model")?.value || "virtio" };
  if (body.network !== "pod" && $("#v_addr_mode")?.value === "static") {
    body.static_ip = Object.assign(vmReadAddress("v"), { address: $("#v_ip").value.trim() });
    if (!body.static_ip.address) return toast("give the VM its address", "bad");
  }
  if (!body.name) return toast("name is required", "bad");
  const serviceMode = body.network === "pod" ? $("#v_service")?.value || "" : "";
  const selectedVip = serviceMode === "manual" ? $("#vsvc_lb_ip").value.trim() : "";
  if (serviceMode === "manual" && !selectedVip) return toast("Choose the VM's Service VIP", "bad");
  // Only what differs from a plain VM: the guest type's settings and any changed by hand.
  const base = window.__vmCreateOptions?.hardware_base;
  const hardware = base && $("#vh") ? vmHardwareChanges(base) : vmPresetSettings($("#v_preset")?.value || "");
  if (hardware) body.hardware = hardware;
  if (body.install_iso) body.password = "";
  if (!body.disk_import && !body.install_iso && body.password.length < 10) return toast("root password must be at least 10 characters", "bad");
  if (boot === "url" && !body.image_url) return toast("image URL is required", "bad");
  if (boot === "url" && !/^https?:\/\/[^/\s]+/i.test(body.image_url)) {
    // A file name here is usually a Harvester image, which is picked from the list instead.
    const image = (window.__vmCreateOptions?.images || []).find(i => i.display === body.image_url || i.name === body.image_url);
    return toast(image ? `${image.display} is a Harvester image: choose it under Boot disk instead of a URL`
      : "the image URL must start with http:// or https://", "bad");
  }
  return vmCreateReview(body, {serviceMode, selectedVip});
};
window.vmCreateReview = async (body, network = {}) => {
  if (VM_CREATE_BUSY) return;
  VM_CREATE_REVIEW = null;
  const sequence = ++VM_CREATE_SEQUENCE;
  const frozen = JSON.parse(JSON.stringify(body));
  try {
    const review = await api("/api/vm/create/preview", {method:"POST", headers:{"Content-Type":"application/json"},body:JSON.stringify(frozen)});
    if (sequence !== VM_CREATE_SEQUENCE) return;
    if (!review.config?.mac || !review.capacity_token || typeof review.capacity?.blocked !== "boolean")
      throw new Error("VM creation review unavailable. Nothing was created; refresh before continuing.");
    VM_CREATE_REVIEW = {...review, network:{...network}};
    const cfg = review.config;
    childModal(`Review new VM · ${cfg.name}`, `<div class="update-review">
      <div class="reviewbox"><b>${esc(cfg.namespace)}/${esc(cfg.name)}</b><p class="small">${esc(cfg.cores || 2)} virtual CPUs · ${esc(cfg.memory || "2Gi")} guest memory · ${esc(cfg.start === false ? "Created stopped" : "Starts after disk preparation")}</p>
        <p class="small muted">${esc(cfg.network || "pod")} · MAC ${esc(cfg.mac)}${cfg.static_ip?.address ? ` · ${esc(cfg.static_ip.address)}` : ""}. Guest login settings are included in this review but are not displayed.</p></div>
      <div class="reviewbox"><b>Disks to create</b>${review.volumes?.length ? review.volumes.map(v=>`<p class="small"><b>${esc(v.name)}</b> · ${esc(v.size)} · ${esc(v.access_mode)} · ${esc(v.volume_mode)}<br><span class="muted">${esc(v.storage_class)}</span></p>`).join("") : '<p class="small muted">Uses the selected existing imported disk; no new disk claim.</p>'}</div>
      ${review.capacity.blockers?.length ? `<div class="note bad">${review.capacity.blockers.map(esc).join(" · ")}</div>` : ""}
      ${deployCapacityHtml(review.capacity)}
      ${!review.capacity.blocked && capacityNotes(review.capacity).concerns.length ? '<label class="check"><input id="vmCreateApprove" type="checkbox" onchange="vmCreateReviewReady()"> Create it anyway, accepting the warnings above</label>' : ""}
      <div class="modalactions"><button class="btn" onclick="vmCreateReviewBack()">Back to configuration</button><button id="vmCreateApply" class="btn pri" disabled onclick="vmCreateReviewedApply()">Create VM</button></div></div>`,true);
    vmCreateReviewReady();
  } catch (error) { if(sequence === VM_CREATE_SEQUENCE) toast(error.message,"bad"); }
};
window.vmCreateReviewBack = () => { if (!VM_CREATE_BUSY) { VM_CREATE_REVIEW = null; ++VM_CREATE_SEQUENCE; modalBack(); } };
window.vmCreateReviewReady = () => {
  // Nothing needing attention: no tickbox to tick, only the button.
  const accepted = $("#vmCreateApprove") ? $("#vmCreateApprove").checked : true;
  const ready = !!(VM_CREATE_REVIEW && !VM_CREATE_BUSY && !VM_CREATE_REVIEW.capacity.blocked && accepted);
  if($("#vmCreateApply")) $("#vmCreateApply").disabled = !ready;
  return ready;
};
window.vmCreateReviewedApply = async () => {
  if (!vmCreateReviewReady()) return toast("Review the VM and acknowledge its warnings first", "bad");
  const review = VM_CREATE_REVIEW, cfg = review.config, button = $("#vmCreateApply");
  VM_CREATE_REVIEW = null;
  VM_CREATE_BUSY = true;
  button.disabled = true; button.textContent = "Preparing disk and creating VM…";
  try {
    const result = await api("/api/vm/create", {method:"POST",headers:{"Content-Type":"application/json"},
      body:JSON.stringify({...cfg, capacity_token:review.capacity_token,confirm_capacity:true})});
    if (result.operation && window.noteOperation) noteOperation(result.operation);
    toast(result.warning || `${cfg.name} created${result.address ? ` at ${result.address}` : ""}`, result.warning ? "bad" : "ok");
    closeModal(); go("vms");
    if (review.network.serviceMode) {
      try { await networkExpose(cfg.namespace,cfg.name,"VirtualMachine",review.network.selectedVip); }
      catch(error) { toast(`VM created, but VIP setup could not open: ${error.message}. Use Edit → Network → Configure VIP / ports.`,"bad"); }
    }
  } catch(error) {
    if($("#vmCreateApply") === button) {
      button.textContent = "Inspect before creating again";
      $("#mbody").insertAdjacentHTML("afterbegin",`<div class="note bad">${esc(error.message)}. The request was not repeated. Images, disks, login settings or the VM itself may already exist. Inspect Virtual machines and Volumes before a new review; retained disks are not deleted automatically.</div>`);
    }
    toast(error.message,"bad");
  } finally { VM_CREATE_BUSY = false; if (window.refreshOperations) refreshOperations(true); }
};

/* ---------------- image cache ---------------- */
/* Why an image is kept: something runs it, will run it, or can go back to it. */
const IMAGE_REASONS = {
  active: { tone: "ok", tip: "" },
  rollback: { tone: "warn", tip: "kept so Roll back can return to it" },
  stopped: { tone: "info", tip: "stopped, and starts from this image; delete it or change its image to clean this up" },
  scheduled: { tone: "info", tip: "a scheduled job that runs this image" },
};
async function viewImages() {
  const [d, vm] = await Promise.all([api("/api/images"), api("/api/images/vm").catch(() => null)]);
  STATE.data.imageCache = d;
  STATE.data.vmImages = vm;
  // Kubernetes lists only each node's largest images; the full list is
  // containerd's. Homestead asks for it itself when its last answer is old,
  // so this only waits for a scan that is running to finish.
  if ((d.scanning || []).length) setTimeout(() => { if (STATE.view === "images") viewImages(); }, 4000);
  const q = STATE.q.toLowerCase();
  const core = n => /(^|\/)(rancher|harvester|longhornio|kubevirt|cdi-|cilium|kube-|metrics-server|registry\.k8s\.io|pause|traefik|fleet|system-upgrade|k8snetworkplumbingwg|multus|whereabouts|kubeovn|calico|canal|flannel|coredns|etcd|rke2|neuvector|suse\/sles\/)/i.test(n);
  const all = d.images.filter(i => !q || i.name.toLowerCase().includes(q));
  const hidden = all.filter(i => core(i.name)).length;
  const imgs = all.filter(i => STATE.showCoreImages || !core(i.name));
  paint(`<div class="phead"><div><h2>Image cache</h2>
      <p>${imgs.length} app images across ${d.nodes.length} nodes · ${d.protected || 0} kept (running, stopped or for rollback) · ${hidden && !STATE.showCoreImages ? `${hidden} Harvester/system images hidden` : `${d.distinct} total`} ${tip("Each node's containerd is asked for every image it holds every 15 minutes or so; images pulled since are added from what Kubernetes reports.")}</p></div>
      <label class="switch"><input type="checkbox" ${STATE.showCoreImages ? "checked" : ""} onchange="STATE.showCoreImages=this.checked;viewImages()"> Show Harvester/system images</label></div>
    <div class="grid g3 statgrid" style="margin-bottom:18px">
      ${d.nodes.map(n => `<div class="card flat"><div class="ctitle" title="${esc(n.node)}">${esc(n.node)}</div>
        <div class="bignum" style="margin-top:8px">${n.total_gb}<span class="unit">GB</span></div>
        <div class="csub">${n.count} images cached${n.scanned_at ? ` · scanned ${esc(fmtAgo(Date.now() / 1000 - n.scanned_at))}` : " · not scanned yet"}</div></div>`).join("")}
    </div>
    ${(d.scanning || []).length ? `<div class="note" style="margin-bottom:12px"><span class="spin2"></span> Asking containerd on ${d.scanning.length} node${d.scanning.length === 1 ? "" : "s"} for every image it holds…</div>`
      : !d.complete ? `<div class="note warn between" style="margin-bottom:12px"><span>Kubernetes reports only each node's largest images, so some - running ones included - are missing here.
          A scan asks each node's containerd for all of them.</span><button class="btn sm" data-need="admin" onclick="imageScan()">Scan every node</button></div>` : ""}
    <div class="note" style="margin-bottom:12px"><b>Removing images.</b> <b>Clean up</b> removes an image nothing uses from the nodes that hold it.
      <span class="tag ok">active</span> images are what running containers use, and <span class="tag info">stopped</span> ones what a container scaled to zero starts from; both stay. <span class="tag warn">rollback</span> copies are the image a container
      had before its last update, kept so <b>Roll back</b> can return to it - <b>Forget</b> one and it becomes unused, to clean up like the rest.</div>
    ${(d.pulls || []).map(pull => `<div class="note warn between prepull-note" style="margin-bottom:12px">
      <span>Pre-pulling <span class="mono">${esc(pull.image)}</span> · ${pull.ready} of ${pull.desired} node${pull.desired === 1 ? "" : "s"} done.
        It runs until every node has the image; its pods come straight back if you delete them.</span>
      <button class="btn sm danger" data-need="admin" onclick="prepullStop(${jsq(pull.name)})">Stop</button></div>`).join("")}
    ${(d.pulls_finished || []).length ? `<div class="note good" style="margin-bottom:12px">Finished pre-pulling
      ${d.pulls_finished.map(pull => `<span class="mono">${esc(pull.image)}</span>`).join(", ")} — cleared away.</div>` : ""}
    <div class="card flat pad0"><div class="tblwrap"><table data-sort="images" class="tbl stack imgtable"><thead><tr>
      <th>Image</th><th>Size</th><th>Cached on</th><th>Retention</th><th></th></tr></thead><tbody>
      ${imgs.map(i => {
        const missing = d.node_names.filter(n => !i.nodes.includes(n));
        const retained = i.retained_by || [];
        const retention = retained.length ? retained.slice(0, 3).map(r => `<span class="tag ${IMAGE_REASONS[r.reason]?.tone ?? "ok"}"
          data-tip="${esc(`${r.namespace} · ${r.workload} · ${r.container}`)}${IMAGE_REASONS[r.reason]?.tip ? esc(" - " + IMAGE_REASONS[r.reason].tip) : ""}">${esc(r.reason)} · ${esc(r.workload)}</span>${r.reason === "rollback"
          ? `<button class="btn sm" data-need="admin" data-tip="Stop keeping this image for ${esc(r.workload)}'s Roll back, so it can be cleaned up" onclick="forgetRollback(${jsq(r.namespace)},${jsq(r.workload)})">Forget</button>` : ""}`).join("") +
          (retained.length > 3 ? `<span class="tag">+${retained.length - 3}</span>` : "") : '<span class="tag">unreferenced</span>';
        return `<tr><td class="mono small imgname">${esc(i.name)}</td>
        <td class="mono">${i.size_mb >= 1024 ? (i.size_mb / 1024).toFixed(1) + " GB" : i.size_mb + " MB"}</td>
        <td>${i.nodes.map(n => `<span class="tag ok">${esc(n.replace("harvester-", ""))}</span>`).join("")}
            ${missing.map(n => `<span class="tag">${esc(n.replace("harvester-", ""))} ✕</span>`).join("")}</td>
        <td><div class="row" style="gap:5px">${retention}</div></td>
        <td><div class="row" style="gap:6px">${missing.length ? `<button class="btn sm" onclick="prepull(${jsq(i.name)})">Pre-pull</button>` : '<span class="dim xs">everywhere</span>'}
          ${!i.protected && !i.system && i.digest ? `<button class="btn sm danger" data-need="admin" onclick="imageCleanupReview(${jsq(i.digest)})">Clean up</button>` : ""}</div></td></tr>`;
      }).join("") || `<tr><td colspan=5 class="empty">none</td></tr>`}
    </tbody></table></div></div>
    ${vmImagesSection(vm, d.node_names)}`);
}

/* VM images: on Harvester each is downloaded once and kept as a Longhorn
   backing image. A disk made from it starts as a copy, and every node its
   disks run on holds a copy of the image - a cache as much as the container
   one above, and as easy to lose track of. */
function vmImagesSection(vm, nodeNames) {
  if (!vm) return "";
  if (!vm.harvester) return `<div class="ctitle" style="margin-top:24px">VM images</div><div class="note">${esc(vm.note)}</div>`;
  const size = mb => mb >= 1024 ? `${(mb / 1024).toFixed(1)} GB` : `${mb} MB`;
  const q = STATE.q.toLowerCase();
  const rows = vm.images.filter(i => !q || i.display.toLowerCase().includes(q));
  const total = vm.images.reduce((sum, i) => sum + i.size_mb * Math.max(1, i.copies), 0);
  return `<div class="between" style="margin:24px 0 10px"><div><div class="ctitle" style="margin:0">VM images</div>
      <p class="dim small" style="margin:4px 0 0">${vm.images.length} image${vm.images.length === 1 ? "" : "s"} · ${size(Math.round(total))} across their copies on nodes ·
        a disk made from one keeps reading from it, so an image a disk came from stays</p></div></div>
    <div class="card flat pad0"><div class="tblwrap"><table data-sort="vmimages" class="tbl stack imgtable"><thead><tr>
      <th>Image</th><th>Size</th><th>Copies on</th><th>Disks made from it</th><th></th></tr></thead><tbody>
      ${rows.map(i => {
        const missing = (nodeNames || []).filter(n => !i.nodes.includes(n));
        const state = i.deleting ? '<span class="tag">deleting</span>'
          : i.state === "failed" ? `<span class="tag bad" data-tip="${esc(i.message)}">download failed</span>`
          : i.state === "downloading" ? `<span class="tag warn">downloading ${i.progress}%</span>` : "";
        const used = i.used_by.length ? i.used_by.map(v => `<span class="tag ok">${esc(v.split("/").pop())}</span>`).join("")
          : i.disks.length ? `<span class="tag" data-tip="${esc(i.disks.join(", "))}">${i.disks.length} disk${i.disks.length === 1 ? "" : "s"}, no VM</span>`
          : '<span class="tag">unused</span>';
        return `<tr><td class="imgname"><b>${esc(i.display)}</b> ${state}
            <div class="dim xs mono">${esc(i.namespace)}${i.source ? ` · from ${esc(i.source)}` : ""}${i.virtual_size_gb ? ` · ${i.virtual_size_gb} GB disk` : ""}</div></td>
          <td class="mono nowrap" data-sort="${i.size_mb}">${i.size_mb ? size(i.size_mb) : '<span class="dim">—</span>'}</td>
          <td>${i.nodes.map(n => `<span class="tag ok">${esc(n.replace("harvester-", ""))}</span>`).join("")}
            ${missing.map(n => `<span class="tag">${esc(n.replace("harvester-", ""))} ✕</span>`).join("")}</td>
          <td><div class="row" style="gap:5px">${used}</div></td>
          <td>${!i.disks.length && !i.deleting ? `<button class="btn sm danger" data-need="admin" onclick="vmImageDelete(${jsq(i.namespace)},${jsq(i.name)})">Delete</button>` : ""}</td></tr>`;
      }).join("") || `<tr><td colspan=5 class="empty">No VM images: an image is kept here when a VM is made from a download address.</td></tr>`}
    </tbody></table></div></div>`;
}
window.vmImageDelete = (namespace, name) => {
  const i = (STATE.data.vmImages?.images || []).find(x => x.namespace === namespace && x.name === name);
  if (!i) return;
  modal(`Delete · ${i.display}`, `<p>The image is deleted, with its copies on ${i.nodes.length} node${i.nodes.length === 1 ? "" : "s"}
      (${esc(i.nodes.join(", ") || "none")}). No disk was made from it. A VM made from the same download address later downloads it again.</p>
    <div class="f" style="margin-top:12px"><label>Type <b class="mono">${esc(i.display)}</b> to confirm</label><input id="vmi_confirm" autocomplete="off"></div>
    <div class="row"><button class="btn danger" onclick="vmImageDeleteGo(${jsq(namespace)},${jsq(name)})">Delete</button><button class="btn" onclick="closeModal()">Cancel</button></div>`);
};
window.vmImageDeleteGo = async (namespace, name) => {
  try {
    const r = await api("/api/images/vm/delete", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ namespace, name, confirm: $("#vmi_confirm").value.trim() }) });
    toast(r.detail, "ok"); closeModal(); resetPaint(); viewImages();
  } catch (e) { toast(e.message, "bad"); }
};
window.imageScan = async () => {
  try {
    const r = await api("/api/images/scan", { method: "POST", headers: { "Content-Type": "application/json" }, body: "{}" });
    toast(r.detail, "ok"); window.__imageScanAt = Date.now();
    setTimeout(() => { if (STATE.view === "images") viewImages(); }, 5000);
  } catch (e) { toast(e.message, "bad"); }
};
window.forgetRollback = async (namespace, name) => {
  if (!(await ask(`Stop keeping ${name}'s previous image? Roll back for its last update goes, and the image can be cleaned up.`))) return;
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
          <button class="btn sm" onclick="jobRun(${jsq(j.name)})">Run now</button>
          <button class="btn sm" onclick='jobEdit(${JSON.stringify(j).replace(/'/g, "&#39;")})'>Edit</button>
          <button class="btn sm danger" onclick="jobDel(${jsq(j.name)})">✕</button>
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
  if (!(await ask(`Delete schedule "${name}"?`))) return;
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
  const [srcs, jobs, disks, namespaces, storageClasses] = await Promise.all([
    api("/api/sources"), api("/api/imports").catch(() => []), api("/api/vm-disks").catch(() => []),
    api("/api/namespaces").catch(() => ["lab"]), api("/api/storageclasses").catch(() => ["longhorn-r2"]),
  ]);
  STATE.data.classFacts = (await api("/api/storageclasses?facts=1").catch(() => ({}))).facts || {};
  STATE.data.srcs = srcs; STATE.data.importNamespaces = namespaces; STATE.data.importStorageClasses = storageClasses;
  paint(`<div class="phead"><div><h2>Import</h2>
      <p>Bring containers, appdata and virtual-machine disks into Homestead</p></div>
      <div class="row"><button class="btn" data-need="operator" onclick="composeImport()">＋ Docker Compose</button>
      <button class="btn" data-need="admin" onclick="srcAdd()">＋ Container source</button>
      <button class="btn pri" data-need="admin" onclick="vmDiskImport()">＋ VM disk</button></div></div>

    <div class="sec">VM disk images ${tip("CDI downloads supported QEMU disk formats, including qcow2 and vmdk, converts them into a VM-ready disk, and writes the result into a new Longhorn PVC.")}</div>
    ${disks.length ? `<div class="card flat pad0"><div class="tblwrap"><table data-sort="vm-disks" class="tbl stack"><thead><tr>
      <th>Disk / PVC</th><th>Capacity</th><th>Status</th><th>Progress</th><th>Attached to</th><th></th></tr></thead><tbody>
      ${disks.map(d => { const done = d.phase === "Succeeded", failed = ["Failed","Error","Unknown"].includes(d.phase); return `<tr>
        <td><b>${esc(d.name)}</b><div class="dim xs mono">${esc(d.namespace)} · ${esc(d.storage_class || "storage class unknown")}</div></td>
        <td class="mono small">${esc(d.capacity || "—")}<div class="dim xs">${esc((d.access_modes || []).join(", "))}</div></td>
        <td><span class="pill ${done ? "ok" : failed ? "crit" : "med"}">${esc(d.phase.replace(/([a-z])([A-Z])/g, "$1 $2"))}</span>${d.message ? `<div class="dim xs" style="margin-top:5px;max-width:320px">${esc(d.message)}</div>` : ""}</td>
        <td style="min-width:130px"><div class="jobmeter"><span class="${failed ? "failed" : ""}" style="width:${Math.max(2, done ? 100 : d.progress || 0)}%"></span></div><div class="dim xs mono">${done ? "ready" : `${esc(d.progress || 0)}%`}</div></td>
        <td class="small">${d.in_use ? esc((d.used_by || []).join(", ")) : '<span class="dim">not attached</span>'}</td>
        <td>${done && !d.in_use ? `<button class="btn sm" onclick="vmNew(${jsq(d.name)},${jsq(d.namespace)})">Create VM</button>` : ""}</td></tr>`; }).join("")}
      </tbody></table></div></div>` : `<div class="empty">No managed VM disk imports yet. Import an HTTP(S) qcow2, vmdk, raw, vdi, vhd or vhdx image into a new PVC.</div>`}

    <div class="sec">Container sources</div>
    <div class="grid g3">${srcs.map(s => `<div class="card flat">
      <div class="between"><div><div class="ctitle">${esc(s.name)}</div>
        <div class="csub">${esc(s.kind)} · ${esc(s.user)}@${esc(s.host)}</div></div>
        <button class="btn sm danger" onclick="srcDel(${jsq(s.name)})">✕</button></div>
      <div class="drow"><div class="dl">Base path</div><div class="dv mono small">${esc(s.base_path)}</div></div>
      <div class="drow"><div class="dl">SSH identity</div><div class="dv">${s.ssh_trust ? 'Key pinned' : 'Verification needed'}</div></div>
      <div class="row" style="margin-top:12px"><button class="btn" data-need="admin" onclick="srcVerify(${jsq(s.name)})">Verify source</button>
      <button class="btn" ${s.ssh_trust ? '' : 'disabled'} onclick="srcBrowse(${jsq(s.name)})">Browse appdata</button></div>
    </div>`).join("") || `<div class="empty">No import sources yet. Add the host you want to pull from.</div>`}</div>

    ${jobs.length ? `<div class="sec">Transfers</div>
    <div class="card flat pad0"><div class="tblwrap"><table data-sort="imports" class="tbl stack"><thead><tr>
      <th>App</th><th>Job</th><th>State</th><th>Progress</th><th data-nosort>Started</th><th></th></tr></thead><tbody>
      ${jobs.map(j => `<tr><td><b>${esc(j.app || "—")}</b>${j.kind === "chown" ? '<span class="tag">ownership</span>' : ""}</td>
        <td class="mono small dim">${esc(j.name)}</td>
        <td><span class="pill ${j.state === "done" ? "ok" : j.state === "failed" ? "crit" : "med"}">${esc(j.state)}</span></td>
        <td style="min-width:150px">${importProgressCell(j)}</td>
        <td class="small dim">${esc((j.start || "").replace("T", " ").replace("Z", ""))}</td>
        <td><div class="row" style="gap:6px;flex-wrap:nowrap"><button class="btn sm" onclick="jobLogs('lab',${jsq(j.name)})">Logs</button>
          <button class="btn sm ${j.state === "failed" ? "danger" : ""}" data-need="admin" title="${j.state === "running" ? "Stop this copy and remove its job" : "Remove this job; it keeps referencing the volume until it is gone"}" onclick="importRemove(${jsq(j.name)},${jsq(j.state)})">${j.state === "running" ? "Cancel" : "Remove"}</button></div></td></tr>`).join("")}
    </tbody></table></div></div>` : ""}

    <div class="note" style="margin-top:20px">
      <b>Container import.</b> It creates a Longhorn volume, runs an rsync job that copies the remote
      appdata directory into it, and creates the workload pointing at that volume — left stopped so you can
      start it once the copy finishes. Path mappings from the source host do not carry over; the appdata
      lands at the mount path you choose.
    </div>`);
}
window.importRemove = async (name, state) => {
  const running = state === "running";
  const plan = await api("/api/imports/cleanup-plan", { method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ name }) }).catch(() => ({ workload: "", volume: "", volume_created: false, volumes: [], known: false }));
  if (plan.journalled) {
    modal(`Remove import Job · ${name}`, UI.lead("Remove the copy Job after a verified successful import. The app, Services and all volumes stay in place.") +
      UI.callout("info", "Incomplete import?", "Open Recent jobs → Inspect import. An active or uncertain copy cannot be removed here.") +
      UI.actions(UI.cancel("Keep it") + UI.button("Remove completed Job", `importRemoveNow(${jsArg(name)},this)`, {kind:"danger",id:"imr_go"})));
    return;
  }
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
      <button class="btn danger" id="imr_go" data-need="admin" onclick="importRemoveNow(${jsq(name)},this)">${running ? "Cancel import" : "Remove"}</button>
      <button class="btn" onclick="closeModal()">Keep it</button></div>`);
  if (window.applyRole) window.applyRole();
};
window.importRemoveNow = async (name, button) => {
  const volumes = $$(".imr-volume").filter(box => box.checked).map(box => box.value);
  const body = { name, remove_workload: !!$("#imr_workload")?.checked,
    remove_volume: volumes.length > 0, remove_volumes: volumes };
  if (volumes.length && !(await ask(`Delete ${volumes.length === 1 ? volumes[0] : volumes.join(" and ")} `
      + "and everything copied into " + (volumes.length === 1 ? "it" : "them") + "?\n\nThis cannot be undone."))) return;
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
window.srcAdd = () => modal("Add import source", UI.lead("Save the connection, then verify its SSH fingerprint. No login is attempted until you trust the source.") +
  UI.fields(UI.field("Name", '<input id="sc_name" placeholder="unraid">'), UI.field("Host", '<input id="sc_host" placeholder="192.0.2.10">'),
    UI.field("Username", '<input id="sc_user" value="root" autocomplete="off">'), UI.field("Password", '<input type="password" id="sc_pass" autocomplete="new-password">'),
    UI.field("SSH port", '<input id="sc_port" type="number" min="1" max="65535" value="22">'),
    UI.field("Type", '<select id="sc_kind"><option value="unraid">Unraid</option><option value="proxmox">Proxmox</option><option value="ssh">Generic SSH</option></select>')) +
  UI.field("Appdata base path", '<input id="sc_path" value="/mnt/user/appdata">') +
  UI.more("Credentials and source ownership", '<p>The password is stored in a Kubernetes Secret. Administrators with access to Secrets can read it. Removing a source does not revoke credentials already used by copy Jobs.</p><p>Unraid® is a registered trademark of Lime Technology, Inc. Homestead is not affiliated with or endorsed by Lime Technology, Inc.</p>') +
  UI.actions(UI.cancel() + UI.button("Save and verify", "doSrcAdd()", {kind:"pri",id:"sc_save"})), false, "operation-review");
window.doSrcAdd = async () => {
  const save = $("#sc_save"), password = $("#sc_pass");
  if (!save || save.disabled) return;
  const body = { name: $("#sc_name").value.trim(), host: $("#sc_host").value.trim(),
    user: $("#sc_user").value.trim(), password: $("#sc_pass").value,
    kind: $("#sc_kind").value, base_path: $("#sc_path").value.trim(), port: +$("#sc_port").value };
  if (!body.name || !body.host) return toast("name and host are required", "bad");
  try {
    save.disabled = true;
    const result = await api("/api/sources", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
    password.value = ""; body.password = "";
    STATE.data.srcs = result.sources;
    if ($("#sc_save") === save) await srcVerify(body.name);
  } catch (e) { toast(e.message + " — refresh Sources before retrying an uncertain save.", "bad"); }
};
let SOURCE_KEY_REVIEW = null, SOURCE_KEY_SEQUENCE = 0, SOURCE_KEY_BUSY = false;
window.srcVerify = async name => {
  if (SOURCE_KEY_BUSY) return;
  SOURCE_KEY_REVIEW = null;
  const sequence = ++SOURCE_KEY_SEQUENCE;
  modal(`Verify source · ${name}`, '<div id="sourceKeyLoading" class="empty"><span class="spin2"></span>Reading the SSH host key. No login or password is sent…</div>', false, "operation-review");
  try {
    const review = await api("/api/sources/scan", {method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({name})});
    if (sequence !== SOURCE_KEY_SEQUENCE || !$("#sourceKeyLoading")) return;
    if (!review.capacity_token || !review.key || !review.fingerprint || review.name !== name) throw new Error("Fingerprint review is incomplete. Nothing was trusted.");
    SOURCE_KEY_REVIEW = review;
    const file = {"ssh-ed25519":"ed25519","ecdsa-sha2-nistp256":"ecdsa","ssh-rsa":"rsa"}[review.algorithm];
    $("#mbody").innerHTML = UI.lead("Compare this fingerprint with the source host's own console before allowing Homestead to log in.") +
      UI.callout(review.changed ? "bad" : "warn", review.changed ? "The source key changed" : "A scan is not proof of identity",
        review.changed ? "A reinstall or an unexpected host can cause this. Confirm the change independently; do not trust it just to clear an error." : "Use the host's physical console or an already trusted management session, not this new SSH connection.") +
      UI.facts([["Host",esc(review.connection?.host) + ":" + esc(review.connection?.port)],["Host key",esc(review.algorithm)]]) +
      UI.section("Compare fingerprint", `<div class="mono" style="overflow-wrap:anywhere">${esc(review.fingerprint)}</div>`) +
      UI.section("On the source console", `<code style="overflow-wrap:anywhere">ssh-keygen -lf /etc/ssh/ssh_host_${esc(file || "ed25519")}_key.pub</code>`) +
      UI.more("What trusting changes", `<p>New discovery and copy connections must match this key. A key change stops new connections; it is never accepted automatically. Existing helpers retain the key and credentials they started with.</p>${review.previous ? `<p>Previously pinned: <span class="mono" style="overflow-wrap:anywhere">${esc(review.previous)}</span></p>` : ""}`) +
      UI.ack("sourceKeyAck", "I compared this fingerprint with the source host's console.", {onchange:"sourceKeyReady()"}) +
      UI.actions(UI.cancel("Not now") + UI.button("Trust this source", "sourceKeyTrust()", {kind:"pri",id:"sourceKeyApply",disabled:true}));
  } catch(e) {
    if (sequence !== SOURCE_KEY_SEQUENCE || !$("#sourceKeyLoading")) return;
    SOURCE_KEY_REVIEW = null;
    $("#mbody").innerHTML = UI.callout("bad", "Source not verified", esc(e.message)) + UI.actions(UI.cancel("Close"));
  }
};
window.sourceKeyReady = () => {
  const ready = !!(SOURCE_KEY_REVIEW && !SOURCE_KEY_BUSY && $("#sourceKeyAck")?.checked);
  if ($("#sourceKeyApply")) $("#sourceKeyApply").disabled = !ready;
  return ready;
};
window.sourceKeyTrust = async () => {
  if (!sourceKeyReady()) return;
  const review = SOURCE_KEY_REVIEW, button = $("#sourceKeyApply");
  SOURCE_KEY_REVIEW = null; SOURCE_KEY_BUSY = true; $("#sourceKeyApply").disabled = true;
  try {
    await api("/api/sources/trust", {method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({
      name:review.name,key:review.key,capacity_token:review.capacity_token,confirm_fingerprint:true})});
    toast("Source key pinned", "ok");
    if ($("#sourceKeyApply") === button) { closeModal(); resetPaint(); viewImport(); }
  } catch(e) {
    if ($("#sourceKeyApply") === button) $("#mbody").innerHTML = UI.callout("bad", "Trust save not confirmed", esc(e.message) + " Refresh and scan again; nothing was retried.") + UI.actions(UI.cancel("Close"));
    else toast("Trust save not confirmed. Refresh Sources before retrying.", "bad");
  } finally { SOURCE_KEY_BUSY = false; }
};
window.srcDel = async name => {
  if (!(await ask(`Remove import source "${name}"? Existing copy Jobs and their credentials are retained; this does not stop or revoke them.`))) return;
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
        <button class="btn pri wide sm" style="margin-top:10px" onclick="inspectImport(${jsq(name)},${jsq(c.name)})">Import container</button></div>`).join("")}</div>
      <div class="sec">Appdata folders</div>` : ""}` + (entries.length
      ? `<p class="muted small">${entries.length} directories under <span class="mono">${esc(src.base_path)}</span>. Use this when Docker metadata is unavailable.</p>
         <div class="apps" style="margin-top:14px;grid-template-columns:repeat(auto-fill,minmax(200px,1fr))">
         ${entries.map(e => `<div class="card flat" style="padding:13px">
           <div style="font-weight:660;word-break:break-all">${esc(e)}</div>
           <button class="btn wide sm" style="margin-top:10px"
             onclick="importSetup(${jsq(name)},${jsq(e)})">Import</button></div>`).join("")}</div>`
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
    <div class="row"><button id="cleanupGo" class="btn danger" data-need="admin" disabled onclick="imageCleanupApply(${jsq(digest)})">Remove cached image</button>
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
  const importVips = await vipChoices();
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
    ${UI.field("Source data safety", '<select id="im_consistency"><option value="">Choose before copying…</option><option value="stopped">All source writers are stopped</option><option value="snapshot">These paths are a consistent snapshot or backup</option></select>', {help: cfg.source_container_id ? "The original Docker container must stay stopped. Snapshot mode skips that check; verify the paths really point to the snapshot." : "Homestead does not stop source applications. Stop every writer or choose stable backup paths before copying."})}
    <div class="sec">Folders to copy ${tip("Every Docker path under the source appdata directory can come across. They all live in one Longhorn volume for this app, each in its own subfolder, mounted back where the container expects it.")}</div>
    <div class="note">Source folders → the volumes you define below → mounted back at each container path.</div>
    ${keeps && cfg.guessed_path ? `<div class="note warn">Nothing this container mounts sits under <span class="mono">${esc(src.base_path || "/mnt/user/appdata")}</span>,
      so Homestead cannot tell which folder holds its configuration. Tick the ones to copy yourself.</div>` : ""}
    <div id="im_maps">${importMappingRows(cfg, src).map(importMappingRow).join("")}</div>
    <div class="row"><button class="btn sm" onclick="imAddMap()">＋ add folder</button>
      <button class="btn sm" onclick="imAddScratch()">＋ add RAM scratch</button>
      <button class="btn sm" id="im_measure" onclick="imMeasure(${jsq(source)})">Measure sizes</button></div>
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
      <div class="f"><label>VIP allocation ${tip("Use the default workload VIP configured in Networking, another reserved address, or a specific VIP. Shared ports must be unique.")}</label><select id="im_vip" onchange="$('#im_vip_wrap').style.display = this.value === 'manual' ? '' : 'none'">${nodeAddressesOnly() ? nodeAddressOption() : `${importVips.shared ? `<option value="shared">Default workload VIP · ${esc(importVips.shared)}</option>` : ""}${nodeAddressChoice(!importVips.shared)}<option value="auto">New automatic VIP</option><option value="manual">Specific VIP</option>`}</select></div></div>
    <div class="f" id="im_vip_wrap" style="display:none"><label>Specific VIP</label><div id="im_vip_pick"><span class="dim xs"><span class="spin2"></span></span></div></div>
    <div class="sec">Port mappings ${tip("Container port is what the app listens on. LAN port is what you open from another device. TCP and UDP mappings are kept separately.")}</div>
    <div id="im_ports">${(cfg.ports || []).map(p => `<div class="f4 im-port"><div><label>Container</label><input class="ipc" type="number" value="${p.container}"></div><div><label>LAN</label><input class="iph" type="number" value="${p.host}"></div><div><label>Protocol</label><select class="ipp"><option ${p.protocol === "TCP" ? "selected" : ""}>TCP</option><option ${p.protocol === "UDP" ? "selected" : ""}>UDP</option></select></div><label class="switch"><input class="ipe" type="checkbox" ${p.expose !== false ? "checked" : ""}>Expose</label></div>`).join("")}</div>
    <button class="btn sm" onclick="imAddPort()">＋ add port</button>
    <div class="sec">Environment variables ${tip("Copied from Docker inspect. Review secrets and host-specific paths before starting the imported app.")}</div>
    <div id="im_env">${Object.entries(cfg.env || {}).map(([k,v]) => `<div class="f2 im-env"><div class="f"><label>Variable</label><input class="iek" value="${esc(k)}"></div><div class="f"><label>Value</label><input class="iev" value="${esc(v)}"></div></div>`).join("")}</div>
    <button class="btn sm" onclick="imAddEnv()">＋ add variable</button>
    <div class="sec">Memory</div>
    <div class="f2"><div class="f"><label>Memory reserved ${tip("The scheduler reserves this much RAM for the application. This is not its maximum usage.")}</label><input id="im_memory" value="${esc(cfg.memory || '256Mi')}" placeholder="256Mi"></div>
      <div class="f"><label>Memory max ${tip("Optional hard ceiling. Exceeding it can cause an OOM kill. Must be at least the reserved memory; blank means unlimited.")}</label><input id="im_memory_limit" value="${esc(cfg.memory_limit || '')}" placeholder="No limit · e.g. 1Gi"></div></div>
    <div class="note">The application is created stopped. After copying, use Start in Containers for a fresh capacity review. The copy helper reserves 128 MiB and is limited to 512 MiB.</div>
    <div class="row" style="margin-top:16px">
      <button class="btn pri" onclick="doImport(${jsq(source)})">Review import</button>
      <button class="btn" onclick="closeModal()">Cancel</button></div>
    <div class="note" style="margin-top:14px">The copy runs as a Job — you can close this and watch it
    on the Import page, folder by folder. Large appdata directories can take a while.</div>`, true);
  if (keeps) $("#im_volumes").innerHTML = importVolumeRow({ name: `${name}-appdata`, size_gb: 10 }, 0);
  vipChoices().then(choices => { const host = $("#im_vip_pick"); if (host) host.innerHTML = vipPicker("im", "", choices); });
  imSyncVolumes();
};
/* Another Homestead on the network. The destination pulls, so these
   credentials are this cluster reaching out, not the far one reaching in. */
/* One cluster a move can come from: whether the two releases can move
   workloads, what a move still needs over there, and its workloads. Shown
   under Settings → Linked clusters. */
function clusterCardHtml(c) {
  return `<div class="card flat clcard">
    <div class="clhead"><div class="ctitle">${esc(c.label || c.name)}</div>
      <div class="csub mono clurl" title="${esc(c.url)}">${c.fleet ? "linked" : "added before linking"} · ${esc(c.url)}</div></div>
    <div class="clver"><span class="dim xs">Version</span>
      <span id="clver_${esc(c.name)}"><span class="dim xs"><span class="spin2"></span> checking…</span></span>
      <button class="iconbtn clrecheck" data-tip="Check the version again" onclick="clusterCheck(${jsq(c.name)})">${icon("refresh")}</button></div>
    <div id="clvermsg_${esc(c.name)}"></div>
    <div class="clsteps" id="clready_${esc(c.name)}"></div>
    <div class="row" style="margin-top:12px"><button class="btn sm" onclick="clusterBrowse(${jsq(c.name)})">Browse workloads</button></div>
    <div class="dim xs" id="cluster_${esc(c.name)}" style="margin-top:10px"></div>
  </div>`;
}
window.clusterCardHtml = clusterCardHtml;

/* Wherever the moves are shown, show them afresh. */
window.movesRepaint = () => {
  if ($("#fleetMovesCard") && window.fleetMovesPaint) fleetMovesPaint();
  else watchMoves();
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
    + (["pending", "applying"].includes(store.longhorn?.state)
      ? step(false, `Backup target on ${esc(name)} is switching`, `<div class="dim xs">${esc(store.longhorn.detail)}. Homestead retries automatically.</div>`)
      : store.longhorn?.state === "failed" || (store.ready && store.longhorn?.pointed === false)
      ? step(false, `Longhorn on ${esc(name)} is not writing to its backup storage`,
          `<div class="dim xs">${esc(store.longhorn.detail || `Current endpoint: ${store.longhorn.endpoint || "none"}`)}</div>
           <div><button class="btn sm" data-need="admin" onclick="clusterPointTarget(${jsq(name)})">Retry target switch</button></div>`)
      : store.stopped
      ? step(false, `Migration from ${esc(name)} is off: its backup storage is stopped`,
          `<div><button class="btn sm pri" data-need="admin" onclick="clusterTransfersOn(${jsq(name)})">Enable it</button></div>`)
      : target.configured && target.reachable_off_cluster && !target.answers
      ? step(false, `Backup storage on ${esc(name)} is at <span class="mono">${esc(target.endpoint || "")}</span>, but this cluster cannot reach it`,
          `<div class="dim xs">It may still be starting. Otherwise the address is taken by something else or firewalled - give it another.</div>
           <div><button class="btn sm" data-need="admin" onclick="clusterStorage(${jsq(name)},true)">Give it another address</button></div>`)
      : r.update_first
      ? step(false, `${esc(name)} runs Homestead v${esc(r.version?.version || "?")}, which sets up backup storage with MinIO - whose images can no longer be downloaded`,
          `<div class="dim xs">Update it to 2.8.111 or later, then set it up from here:</div><div class="mono xs">kubectl -n lab set image deployment/homestead homestead=ghcr.io/wjcloudy/homestead:2.8.111</div>`)
      : target.configured && target.reachable_off_cluster
      ? step(true, `Backup storage on ${esc(name)}`, `<div class="dim xs mono">${esc(target.url || "")} · ${esc(target.endpoint || "")}</div>`)
      : target.configured
        ? step(false, `Backup storage on ${esc(name)} has no LAN address, so this cluster cannot read it${target.endpoint ? ` - it is at <span class="mono">${esc(target.endpoint)}</span>, inside that cluster` : ""}`,
            `<div><button class="btn sm" data-need="admin" onclick="clusterStorage(${jsq(name)},true)">Give it an address</button></div>`)
        : store.deployed && !store.ready
          ? step(false, `Backup storage on ${esc(name)} is starting`, '<div class="dim xs">Its first start downloads the S3 server; this checks again every 15 seconds.</div>')
        : store.deployed
          ? step(false, `Backup storage is running on ${esc(name)}, but its Longhorn is not pointed at it yet`,
              `<div><button class="btn sm pri" data-need="admin" onclick="clusterStorage(${jsq(name)})">Finish setting it up</button></div>`)
        : step(false, `${esc(name)} has no backup storage yet`,
            `<div><button class="btn sm pri" data-need="admin" onclick="clusterStorage(${jsq(name)})">Set it up on ${esc(name)}</button></div>`))
    + step(r.ready, "Browse its workloads and move them here");
  if (window.applyRole) applyRole();
  clearTimeout(window["__clready_" + name]);
  if (store.deployed && (!store.ready || ["pending", "applying"].includes(store.longhorn?.state)))
    window["__clready_" + name] = setTimeout(() => { if ($("#clready_" + name)) clusterReady(name); }, 15000);
};

window.clusterPointTarget = async (name, after = null) => {
  try {
    const r = await api("/api/move/clusters/target", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name }) });
    toast(r.detail, "ok");
    if (after) after(); else clusterReady(name);
  } catch (e) { toast(e.message, "bad"); }
};

window.clusterTransfersOn = async name => {
  try {
    const r = await api("/api/move/clusters/transfers", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name, allow: true }) });
    toast(`${name}: ${r.detail}`, "ok");
    clusterReady(name);
    if (window.fleetTransfersPaint) fleetTransfersPaint();
  } catch (e) { toast(e.message, "bad"); }
};

/* Backup storage on the other cluster, made from here with the account
   Homestead already holds for it - what its own Data protection page does. */
window.clusterStorage = (name, addressOnly = false, after = null) => {
  window.__clusterStorageAfter = after;
  window.__clusterStorageAddressOnly = addressOnly;
  const ready = window.__clusterReady?.[name] || {};
  const free = ready.free_vips || [];
  const shared = ready.shared_vip || "";
  // Its shared address first: the store answers on its own port there, beside
  // the apps that share it, so no address of its own is needed.
  childModal(`Backup storage on ${name}`, `<div class="ui-stack">
    ${UI.lead(addressOnly
      ? `The backup storage on <b>${esc(name)}</b> runs, but only inside that cluster. Give it an address on your LAN and this cluster reads the backups from there.`
      : `Puts an S3 store (RustFS) on a Longhorn volume on <b>${esc(name)}</b> and points its Longhorn backups at it. A move backs each volume up there, then restores it here.`)}
    ${UI.fields(
      addressOnly ? "" : UI.field("Size (GB)", '<input id="cs_size" type="number" min="5" value="100">'),
      UI.field("Address", `<select id="cs_pick" onchange="$('#cs_ip').hidden = this.value !== '__typed'">
          <option value="__shared" selected>${shared ? `Its shared address · ${esc(shared)}` : "Its nodes' own addresses"}</option>
          ${free.some(v => v.from === "vips") ? `<optgroup label="An address of its own: ${esc(name)}'s VIPs">${free.filter(v => v.from === "vips").map(v =>
            `<option value="${esc(v.ip)}">${esc(v.ip)}${v.label ? ` · ${esc(v.label)}` : ""}</option>`).join("")}</optgroup>` : ""}
          ${free.some(v => v.from !== "vips") ? `<optgroup label="An address of its own: free in ${esc(name)}'s IP pools">${free.filter(v => v.from !== "vips").map(v =>
            `<option value="${esc(v.ip)}">${esc(v.ip)}</option>`).join("")}</optgroup>` : ""}
          <option value="__typed">Type an address…</option></select>
        <input id="cs_ip" class="mono" placeholder="192.0.2.243" hidden data-ipam>`,
        { help: `${shared ? `${esc(name)}'s shared address is the one its apps share; the store answers on its own port there.` : `${esc(name)} puts Services on its nodes' own addresses; the store answers on its own port there.`} Choose an address of its own only to keep its traffic apart.` }),
      addressOnly ? "" : UI.field("Port", '<input id="cs_port" type="number" min="1" max="65534" placeholder="9000" class="mono">',
        { help: "Where the store answers: 9000, or the port it already has, when left blank. Pick another if an app there already uses it; the next port up is its console." }))}
    ${UI.more("Whose address this is", `<p>The address belongs to ${esc(name)}, the cluster sending the workloads: its backup store answers on it.
      This cluster never takes it - it only connects to it to read the backups during a move. It shares ${esc(name)}'s disks, so it is for moving, not your only copy of anything.</p>`)}
    ${UI.actions(UI.button("Cancel", "modalBack()") + UI.button(addressOnly ? "Set the address" : "Set it up", `clusterStorageGo(${jsArg(name)})`, { kind: "pri", id: "cs_go" }))}
  </div>`);
};
window.clusterStorageGo = async name => {
  const pick = $("#cs_pick")?.value || "__shared";
  const address = pick === "__typed" ? $("#cs_ip").value.trim() : pick === "__shared" ? "" : pick;
  if (pick === "__typed" && !address) return toast("type the address this cluster should reach it at", "bad");
  const button = $("#cs_go");
  if (button) { button.disabled = true; button.textContent = "Working…"; }
  try {
    const r = await api("/api/move/clusters/storage", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name, size_gb: +($("#cs_size")?.value || 100), lb_ip: address,
        vip_mode: pick === "__shared" ? "shared" : "", port: +($("#cs_port")?.value || 0) || undefined }) });
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
    const volumeCount = (report.volumes || []).length;
    if (host) host.textContent = `${report.workloads.length} workload${report.workloads.length === 1 ? "" : "s"}`
      + ` · ${report.movable} movable` + (volumeCount ? ` · ${volumeCount} volume${volumeCount === 1 ? "" : "s"}` : "");
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
        ? `<button class="btn sm" data-need="admin" onclick="moveReview(${jsq(report.cluster)},${jsq(w.kind)},${jsq(w.name)})">Move to this cluster</button>`
          + ((w.warnings || []).length ? `<div class="dim xs" style="max-width:240px;margin-top:4px">${w.warnings.map(esc).join("; ")}</div>` : "")
        : `<span class="tag bad">cannot move</span><div class="dim xs" style="max-width:240px">${w.blockers.map(esc).join("; ")}</div>`}</td>
    </tr>`).join("")}</tbody></table></div>`
    : '<div class="empty">That cluster is running nothing Homestead can see.</div>'}
  ${clusterVolumesHtml(report)}
  <div class="row" style="margin-top:16px"><button class="btn" onclick="closeModal()">Close</button></div>`);
};

/* Volumes that can move on their own. One a running app or VM uses is
   listed but refused, naming what uses it: moving that brings the volume. */
function clusterVolumesHtml(report) {
  const volumes = report.volumes || [];
  if (!volumes.length) return "";
  return `<h3 style="margin:18px 0 8px">Volumes</h3>
  <div class="dim xs" style="margin-bottom:8px">A volume moves on its own when nothing is using it. Moving an app or VM brings its volumes with it.</div>
  <div class="tblwrap"><table class="tbl dense"><thead><tr>
    <th>Volume</th><th>Size</th><th>Used by</th><th></th></tr></thead><tbody>
    ${volumes.map(v => `<tr>
      <td><b class="mono">${esc(v.name)}</b>${v.volume_mode === "Block" ? ' <span class="tag">disk</span>' : ""}
        <div class="dim xs mono">${esc(v.namespace)}${v.storage_class ? ` · ${esc(v.storage_class)}` : ""}</div></td>
      <td class="mono small">${v.size_gb} GB</td>
      <td class="small">${v.used_by.length ? v.used_by.map(esc).join(", ") : '<span class="dim">nothing</span>'}</td>
      <td>${v.movable
        ? `<button class="btn sm" data-need="admin" onclick="moveReview(${jsq(report.cluster)},'volume',${jsq(v.name)})">Move to this cluster</button>`
          + ((v.warnings || []).length ? `<div class="dim xs" style="max-width:240px;margin-top:4px">${v.warnings.map(esc).join("; ")}</div>` : "")
        : `<span class="tag bad">cannot move</span><div class="dim xs" style="max-width:240px">${v.blockers.map(esc).join("; ")}</div>`}</td>
    </tr>`).join("")}</tbody></table></div>`;
}

/* Before anything stops: where it lands, what address it gets, and every
   reason it would fail or surprise someone - asked of both clusters. */
let MOVE_PLAN_SEQUENCE = 0;
window.moveReview = (cluster, kind, name) => {
  ++MOVE_PLAN_SEQUENCE;
  childModal(`Move ${name} from ${cluster}`, `
  <p class="muted small">${kind === "volume"
    ? `Holds ${esc(name)} on ${esc(cluster)} while nothing uses it, backs it up to the shared backup storage,
      and restores it here. The original stays on ${esc(cluster)} until you remove it, so nothing is lost if
      you change your mind.`
    : `Stops ${esc(name)} on ${esc(cluster)}, backs up its volumes to the shared backup
      storage, restores them here, and starts it here. The original stays on ${esc(cluster)}, stopped,
      until you remove it, so it can be put back at any point before then.`}</p>
  <div class="f2"><div class="f"><label>Namespace here</label><input id="mv_ns" value="lab"></div>
    ${kind !== "container" ? "" : `<div class="f"><label>Address ${tip("The LAN address its Services get on this cluster. Shared uses this Homestead's own address; automatic takes a free one from the Harvester IP pool.")}</label>
      <select id="mv_mode" onchange="moveAddressMode(this.value)">
        <option value="shared">This cluster's shared address</option>
        <option value="automatic">Next free pool address</option>
        <option value="manual">A specific address</option></select></div>`}</div>
  <div class="f" id="mv_ip_wrap" hidden><label>Specific address</label><input id="mv_ip" class="mono" placeholder="192.0.2.245" data-ipam></div>
  <div class="f"><label>Destination storage class</label><select id="mv_sc" disabled><option value="">Loading destination classes…</option></select>
    <div class="dim xs">Restored volumes inherit this class's replica count, disk tags and other storage settings.</div></div>
  <div id="mv_plan"></div>
  <div class="row" style="margin-top:16px">
    <button class="btn" onclick="movePlan(${jsq(cluster)},${jsq(kind)},${jsq(name)})">Check again</button>
    <button class="btn pri" id="mv_go" data-need="admin" disabled
      onclick="moveStart(${jsq(cluster)},${jsq(kind)},${jsq(name)})">Start move</button>
    <button class="btn" onclick="modalBack()">Cancel</button></div>`);
  movePlan(cluster, kind, name);
  for (const id of ["#mv_ns", "#mv_ip"]) {
    const field = $(id);
    if (field) field.oninput = () => { ++MOVE_PLAN_SEQUENCE; $("#mv_go").disabled = true; };
  }
};

window.moveAddressMode = value => {
  ++MOVE_PLAN_SEQUENCE;
  if ($("#mv_go")) $("#mv_go").disabled = true;
  const wrap = $("#mv_ip_wrap");
  if (wrap) wrap.hidden = value !== "manual";
};

const moveBody = (cluster, kind, name) => ({
  cluster, kind, name, namespace: $("#mv_ns")?.value.trim() || "lab",
  address_mode: $("#mv_mode")?.value || "shared", address: $("#mv_ip")?.value.trim() || "",
  storage_class: $("#mv_sc")?.value || "" });

window.movePlan = async (cluster, kind, name) => {
  const host = $("#mv_plan"), go = $("#mv_go");
  if (!host) return;
  const sequence = ++MOVE_PLAN_SEQUENCE;
  host.innerHTML = '<div class="empty"><span class="spin2"></span> asking both clusters…</div>';
  if (go) go.disabled = true;
  try {
    const plan = await api("/api/move/plan", { method: "POST",
      headers: { "Content-Type": "application/json" }, body: JSON.stringify(moveBody(cluster, kind, name)) });
    if (sequence !== MOVE_PLAN_SEQUENCE || $("#mv_plan") !== host) return;
    const volumes = plan.claims || [];
    const classes = $("#mv_sc");
    if (classes && plan.storage_classes) {
      const selected = classes.value || plan.storage_class;
      classes.innerHTML = plan.storage_classes.map(c => `<option value="${esc(c)}" ${c === selected ? "selected" : ""}>${esc(c)}</option>`).join("");
      classes.disabled = !plan.storage_classes.length;
      classes.onchange = () => { if (go) go.disabled = true; movePlan(cluster, kind, name); };
      // A cluster without Homestead's old default can still choose another
      // valid class. Review the actual selection before enabling Start.
      if (classes.value && classes.value !== plan.storage_class) return movePlan(cluster, kind, name);
    }
    const fix = (plan.fixes || [])[0];
    host.innerHTML = `
      ${plan.blockers?.length ? `<div class="note bad"><b>This move would fail.</b><ul>${plan.blockers.map(b => `<li>${esc(b)}</li>`).join("")}</ul>
        ${fix ? `<button class="btn sm pri" data-need="admin" onclick="clusterStorage(${jsq(cluster)},${fix.kind === "source-address"},() => movePlan(${jsq(cluster)},${jsq(kind)},${jsq(name)}))">${fix.kind === "source-address"
          ? `Give ${esc(cluster)}'s backup storage an address` : `Set up backup storage on ${esc(cluster)}`}</button>` : ""}</div>` : ""}
      ${plan.warnings?.length ? `<div class="note warn"><b>Worth knowing first.</b><ul>${plan.warnings.map(w => `<li>${esc(w)}</li>`).join("")}</ul></div>` : ""}
      ${plan.ok ? `<div class="note good"><b>Ready to move.</b>
        ${kind === "volume" ? `${esc(name)} (${plan.total_gb} GB) goes through ${plan.joined ? "the backup storage both clusters already share" : `the backup storage on ${esc(cluster)}, which this cluster will be pointed at`}.`
          : volumes.length ? `${volumes.length === 1 ? "Its volume" : `Its ${volumes.length} volumes`} (${plan.total_gb} GB) ${volumes.length === 1 ? "goes" : "go"}
          through ${plan.joined ? "the backup storage both clusters already share" : `the backup storage on ${esc(cluster)}, which this cluster will be pointed at`}.`
          : "It has no volumes, so only its definition travels."}
        ${plan.addresses?.length ? `<br>Reachable here at ${plan.addresses.map(esc).join(", ")}.` : ""}
        ${plan.will_run || kind === "volume" ? "" : `<br>It is stopped on ${esc(cluster)}, and will arrive stopped.`}</div>` : ""}
      ${volumes.length ? `<div class="drow"><div class="dl">Volumes</div><div class="dv mono xs">${volumes.map(c =>
        `${esc(c.claim)} · ${c.size_gb} GB${c.volume_mode === "Block" ? " · disk" : ""}`).join("<br>")}<br>Storage class: ${esc(plan.storage_class || "cluster default")}</div></div>` : ""}`;
    if (go) go.disabled = !plan.ok;
  } catch (e) {
    if (sequence !== MOVE_PLAN_SEQUENCE || $("#mv_plan") !== host) return;
    host.innerHTML = `<div class="note bad">${esc(e.message)}</div>`;
  }
};

window.moveStart = async (cluster, kind, name) => {
  const question = kind === "volume"
    ? `Bring the volume ${name} here from ${cluster}?` + String.fromCharCode(10, 10)
      + `Nothing on ${cluster} can use it until the move finishes or is put back.`
    : `Stop ${name} on ${cluster} and bring it here?` + String.fromCharCode(10, 10)
      + "It is unavailable from the moment it stops there until it starts here.";
  if (!(await ask(question))) return;
  try {
    await api("/api/move/start", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(moveBody(cluster, kind, name)) });
    toast(`moving ${name} from ${cluster}; follow it under Linked clusters or in Activity`, "ok");
    closeModal(); movesRepaint();
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
      m.status === "failed" ? `<button class="btn sm" data-need="admin" onclick="moveAct('retry',${jsq(m.id)})">${m.phase === "backing-up" ? "Retry failed backups" : "Retry"}</button>` : "",
      m.status === "succeeded" && !m.source_removed
        ? `<button class="btn sm" data-need="admin" onclick="moveFinish(${jsq(m.id)},${jsq(m.name)},${jsq(m.cluster)},${jsq(m.kind)})">Remove from ${esc(m.cluster)}</button>` : "",
      ["running", "failed", "succeeded"].includes(m.status) && !m.source_removed
        ? `<button class="btn sm danger" data-need="admin" onclick="moveBack(${jsq(m.id)},${jsq(m.name)},${jsq(m.cluster)},${jsq(m.status)},${m.source_stopped === false ? "false" : "true"})">${m.source_stopped === false ? "Cancel" : "Put back"}</button>` : "",
      ["succeeded", "cancelled", "failed"].includes(m.status)
        ? `<button class="btn sm" data-need="admin" data-tip="${m.status === "failed" ? `Clear it from this list, touching neither cluster` : m.status === "succeeded" && !m.source_removed ? `Clear it from this list. ${esc(m.cluster)} keeps its stopped copy until you remove it there.` : "Clear it from this list"}"
            onclick="moveDismiss(${jsq(m.id)})">Dismiss</button>` : "",
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
  if (!$("#movesList")) return;
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
    toast(action === "retry" ? "retrying from where it stopped" : "done", "ok");
    watchMoves();
  } catch (e) { toast(e.message, "bad"); }
};

window.moveBack = async (id, name, cluster, status, stopped = true) => {
  // Nothing has stopped on the source yet: undoing it only cancels the move.
  if (!stopped) {
    if (!(await ask(`Cancel moving ${name}?` + String.fromCharCode(10, 10)
        + `Nothing has stopped on ${cluster}; anything this move set up here is removed.`))) return;
    return moveAct("abandon", id);
  }
  const landed = status === "succeeded";
  if (!(await ask(`Put ${name} back on ${cluster}?` + String.fromCharCode(10, 10)
      + "It starts again there, as it was, and what this move created here is removed"
      + (landed ? ", including anything written to it here since it arrived." : ".")))) return;
  moveAct("abandon", id);
};

window.moveFinish = (id, name, cluster, kind) => modal(`Remove ${name} from ${cluster}`, kind === "volume" ? `
  <p>${esc(name)} is here now. Removing the original from ${esc(cluster)} deletes that copy and makes the move
    permanent: after this it cannot be put back.</p>
  <div class="note">Its backup stays in the backup storage.</div>` : `
  <p>${esc(name)} is running here. Removing the stopped original from ${esc(cluster)} makes the move
    permanent: after this it cannot be put back.</p>
  <label class="switch"><input type="checkbox" id="mv_vols"> Also delete its volumes on ${esc(cluster)}</label>
  <div class="note">Leaving the volumes costs space on ${esc(cluster)} but keeps a copy of the data as it
    was at the moment of the move. Their backups stay in the backup storage either way.</div>
  <div class="row" style="margin-top:16px">
    <button class="btn danger" data-need="admin" onclick="moveFinishNow(${jsq(id)})">Remove from ${esc(cluster)}</button>
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
    source_consistency: $("#im_consistency")?.value || "", source_container_id: cfg.source_container_id || "",
    image: $("#im_image").value.trim(), icon: $("#im_icon").value.trim(),
    mappings, volumes: keepsNothing ? [] : volumes,
    pvc_name: first?.name || "", size_gb: first?.size_gb || 0, reuse_existing: existing,
    storage_class: first?.storage_class || "", access_mode: first?.access_mode || "",
    start_after_copy: true, memory: $("#im_memory").value.trim(), memory_limit: $("#im_memory_limit").value.trim(),
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
    // Longhorn actual_gb includes snapshot/replica allocation, not filesystem
    // usage. Never subtract it to invent the free space of an existing claim.
    const neededGb = measured / 1024 ** 3;
    if (capacityGb && neededGb > capacityGb) {
      return toast(`${volume.name}: ${neededGb.toFixed(1)} GiB to copy exceeds its ${capacityGb.toFixed(1)} GiB total capacity — grow it or raise its size`, "bad");
    }
  }
  // A RAM scratch mapping has no source by design, so only copied folders are
  // asked for one.
  const copied = body.mappings.filter(row => !row.medium && row.copy !== false);
  if (copied.length && !["stopped", "snapshot"].includes(body.source_consistency))
    return toast("Choose Source data safety: stop all source writers or copy a consistent snapshot/backup.", "bad");
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
  if (reused.length && !(await ask(`Import into existing volume${reused.length === 1 ? "" : "s"} ${reused.join(", ")}?` +
      "\n\nThe current data is kept, but imported files with the same names may be replaced."))) return;
  await importReview(body);
};

let IMPORT_REVIEW = null, IMPORT_REVIEW_SEQUENCE = 0;
window.importReview = async body => {
  IMPORT_REVIEW = null;
  const sequence = ++IMPORT_REVIEW_SEQUENCE;
  const config = JSON.parse(JSON.stringify(body));
  try {
    const result = await api("/api/import/preview", {method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify(config)});
    if (sequence !== IMPORT_REVIEW_SEQUENCE) return;
    if (!result.capacity || !result.capacity_token || !Array.isArray(result.phases)) throw new Error("Import review unavailable; refresh before continuing.");
    IMPORT_REVIEW = {config, ...result, submitting:false};
    childModal("Review import", `<div class="update-review">
      <div class="reviewbox"><b>${esc(config.name)}</b><p>${esc(config.image)}</p>
        ${config.source_consistency ? `<p>${config.source_consistency === "snapshot" ? "Copy from a consistent snapshot or backup. Source-container checks are skipped; verify the selected paths." : "All source writers must stay stopped." + (config.source_container_id ? " The original Docker container is checked before and after copying each folder." : " Homestead cannot verify other writers.")}</p>` : ""}
        ${(result.volumes || []).map(v => `<div class="dependency-row"><span>${v.create ? 'Create' : 'Reuse'} volume</span><b>${esc(v.name)} · ${esc(v.access_mode)} · ${esc(v.storage_class)}</b></div>`).join('')}
      </div><div class="note warn">${result.capacity.warnings.map(esc).join('<br>')}</div>
      ${(config.mappings || []).some(m => m.copy !== false && m.medium !== 'memory') || config.remote_path ? UI.more('Copy safety checks', 'Free space is checked on the mounted destination against measured source sizes. Missing measurements are reported in the job log, not treated as zero. Estimates do not reserve space. Unsafe destination paths stop the copy; failed transfers keep both copies.') : ''}
      ${result.phases.map(p => `<h3>${esc(p.title)}</h3>${deployCapacityHtml(p.capacity)}`).join('')}
      ${!result.capacity.blocked ? '<label class="switch"><input type="checkbox" id="importConfirm"> I approve this import, including file replacement and any capacity warnings.</label>' : ''}
      <div class="modalactions"><button class="btn" onclick="modalBack()">Back to import</button><button class="btn pri" id="importGo" ${result.capacity.blocked ? 'disabled' : ''} onclick="confirmImport()">Create reviewed import</button></div></div>`, true);
  } catch (e) { toast(e.message, "bad"); }
};
window.confirmImport = async () => {
  const review = IMPORT_REVIEW;
  if (!review || review.submitting || review.capacity.blocked || !$("#importConfirm")?.checked)
    return toast("Review and approve the import first", "bad");
  review.submitting = true;
  $("#importGo").disabled = true;
  try {
    const result = await api("/api/import", {method:"POST", headers:{"Content-Type":"application/json"},
      body:JSON.stringify({...review.config, capacity_token:review.capacity_token, confirm_capacity:true})});
    if (result.operation) window.noteOperation?.(result.operation);
    IMPORT_REVIEW = null;
    toast(result.job ? "Copy started; the application stays stopped" : "Import configuration saved", "ok");
    closeModal(); resetPaint(); viewImport();
  } catch (e) {
    IMPORT_REVIEW = null; // An unknown outcome is never an automatic retry.
    toast(e.message + " — inspect Recent jobs before trying again; any created volumes are kept.", "bad");
    $("#importGo").textContent = "Review again";
    $("#importGo").disabled = false;
    $("#importGo").onclick = () => importReview(review.config);
  } finally {
    window.refreshOperations?.(true);
  }
};

/* Finished moves off the list, leaving the source's stopped copy where it is. */
window.moveDismiss = async (id = "") => {
  const all = await api("/api/move/moves").catch(() => []);
  const failed = id ? all.find(m => m.id === id && m.status === "failed") : null;
  const rows = all.filter(m => (!id || m.id === id) && m.status === "succeeded" && !m.source_removed);
  if (failed && !(await ask(`Clear the failed move of ${failed.name}?` + String.fromCharCode(10, 10)
      + "Nothing is changed on either cluster. "
      + (failed.source_stopped !== false ? `${failed.name} stays stopped on ${failed.cluster}: start it there from its page if you still want it running. ` : "")
      + "Anything the move made here stays too; remove it here if you do not want it. Retry and Put back are no longer offered."))) return;
  if (!failed && rows.length && !(await ask(`Clear ${id ? "this move" : "finished moves"} from the list?` + String.fromCharCode(10, 10)
      + `${[...new Set(rows.map(m => m.cluster))].join(", ")} keeps the stopped original of ${rows.map(m => m.name).join(", ")} - `
      + "nothing is removed there, and Put back is no longer offered here."))) return;
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
