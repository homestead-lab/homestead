/* Unified storage picker shared by the deployment wizard and the container editor.

   A picker host is any element tagged with data-volume-host and given a context
   through createVolumePicker(). The context supplies the live namespace
   inventory (PVCs, storage classes) and the pod volumes that can be reused, so
   the same rows work while creating a workload and while editing one. */

const VOLUME_PICKER_DEFAULTS = {
  pvcs: () => [],
  storageClasses: () => [],
  podVolumes: () => [],
  allowPod: () => false,
  podLabel: "Existing volume in this pod",
  podEmpty: "Choose a volume already in this pod…",
  podUnavailable: "No reusable volumes in this pod",
  podHelp: "Mounts a volume already defined in this pod into this container.",
  onChange: () => { },
};

function volumeKind(v) {
  if (v.kind) return v.kind;
  if (v.type === "host") return "host";
  if (v.type === "emptyDir") return "ephemeral";
  if (v.type === "pod") return "pod";
  if (v.create === false) return "existing";
  return v.access_mode === "ReadWriteMany" ? "new-rwx" : "new-rwo";
}

function volumeType(kind) {
  return kind === "host" ? "host" : kind === "pod" ? "pod" : kind === "ephemeral" ? "emptyDir" : "pvc";
}

/* Validation runs on plain objects so it can be reused before a save without a
   picker being on screen. */
function volumeRowIssue(v) {
  const path = String((v && v.path) || "").trim();
  if (!path) return "every storage mapping needs a container mount path";
  if (!path.startsWith("/")) return `mount path "${path}" must start with /`;
  const kind = volumeKind(v || {});
  if (kind !== "ephemeral" && !String((v && v.source) || "").trim()) {
    return `${path} needs a storage source`;
  }
  if ((kind === "new-rwo" || kind === "new-rwx") &&
      !/^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$/.test(String(v.source || "").trim())) {
    return `new volume name for ${path} must use lowercase letters, numbers and dashes`;
  }
  return "";
}

function volumeListIssue(rows) {
  const seen = new Set();
  for (const row of rows || []) {
    const issue = volumeRowIssue(row);
    if (issue) return issue;
    const path = String(row.path || "").trim().replace(/\/+$/, "") || "/";
    if (seen.has(path)) return `${path} is mounted twice in the same container`;
    seen.add(path);
  }
  return "";
}

function volumePickerHost(node) { return node.closest("[data-volume-host]"); }

function volumePickerContext(node) {
  const host = volumePickerHost(node);
  return (host && host.__volumeContext) || VOLUME_PICKER_DEFAULTS;
}

function volumeSourceValue(row) {
  const choice = $(".vselect", row);
  return String(choice && choice.style.display !== "none" ? choice.value : $(".vs", row).value).trim();
}

function volumeSourceList(row, kind) {
  const ctx = volumePickerContext(row);
  let values = [];
  if (kind === "existing") values = (ctx.pvcs() || []).map(v => ({ value: v.name,
    label: `${(v.access_modes || []).join("/") || "mode unknown"} · ${v.size || "size unknown"} · ${v.status}` }));
  if (kind === "pod") values = (ctx.podVolumes() || []).map(v => ({ value: v.name,
    label: `${v.kind}${v.source ? ` · ${v.source}` : ""}` }));
  const choice = $(".vselect", row), requested = String(choice.value || $(".vs", row).value).trim();
  // A source that is already mounted but missing from the inventory stays
  // selectable, so opening the editor can never silently drop it.
  if (requested && !values.some(v => v.value === requested)) {
    values = [{ value: requested, label: "currently mounted" }, ...values];
  }
  const emptyLabel = kind === "pod" ? ctx.podEmpty : "Choose an existing PVC…";
  const unavailable = kind === "pod" ? ctx.podUnavailable : "No existing PVCs in this namespace";
  choice.innerHTML = values.length
    ? `<option value="">${emptyLabel}</option>` + values.map(v => `<option value="${esc(v.value)}">${esc(v.value)} · ${esc(v.label)}</option>`).join("")
    : `<option value="">${unavailable}</option>`;
  choice.disabled = !values.length;
  choice.value = values.some(v => v.value === requested) ? requested : "";
}

function syncVolumeRow(row) {
  const ctx = volumePickerContext(row), kind = $(".vk", row).value, allowPod = !!ctx.allowPod();
  const podOption = [...$(".vk", row).options].find(o => o.value === "pod");
  if (podOption) podOption.disabled = !allowPod;
  if (kind === "pod" && !allowPod) $(".vk", row).value = "existing";
  const actual = $(".vk", row).value, isNew = actual.startsWith("new-");
  $(".vnew", row).style.display = isNew ? "grid" : "none";
  volumeSourceList(row, actual);
  const source = $(".vs", row), choice = $(".vselect", row), selectable = actual === "existing" || actual === "pod";
  $(".vsource", row).style.display = actual === "ephemeral" ? "none" : "block";
  source.style.display = selectable ? "none" : "block";
  choice.style.display = selectable ? "block" : "none";
  source.disabled = actual === "ephemeral";
  source.placeholder = actual === "host" ? "/mnt/storage or /dev/…" : "new PVC name";
  $(".vsource-label", row).textContent = actual === "host" ? "Host path" : actual === "pod" ? "Pod volume" : actual === "existing" ? "Existing PVC" : "New PVC name";
  const selectedSource = volumeSourceValue(row);
  const selectedPvc = actual === "existing" ? (ctx.pvcs() || []).find(v => v.name === selectedSource) : null;
  const selectedPodVolume = actual === "pod" ? (ctx.podVolumes() || []).find(v => v.name === selectedSource) : null;
  $(".vhelp", row).textContent = actual === "new-rwo" ? "Creates a Longhorn claim for this workload (single-node attachment)."
    : actual === "new-rwx" ? "Creates shared Longhorn storage that can attach from multiple nodes."
    : actual === "existing" ? selectedPvc ? `${selectedPvc.name}: ${(selectedPvc.access_modes || []).join("/") || "mode unknown"}, ${selectedPvc.size}, ${selectedPvc.status}. The claim and data are kept.` : "Mounts an existing PVC without creating or deleting it."
    : actual === "pod" ? selectedPodVolume ? `${selectedPodVolume.name}: ${selectedPodVolume.kind}${selectedPodVolume.source ? ` (${selectedPodVolume.source})` : ""}. The same storage is shared with the other container.` : ctx.podHelp
    : actual === "ephemeral" ? "Creates temporary pod storage. Its contents are deleted when the pod is replaced; ideal for cache or transcoding."
    : "Mounts this exact host path; the container can only run where that path exists.";
}

function addVolumeRow(host, v = {}) {
  if (!host) return null;
  const ctx = volumePickerContext(host);
  const kind = volumeKind(v), d = document.createElement("div"); d.className = "deploy-volume";
  d.dataset.label = v.label || ""; d.dataset.description = v.description || "";
  d.dataset.required = String(!!v.required); d.dataset.templateSource = v.template_source || "";
  d.dataset.role = v.role || ""; d.dataset.volumeName = v.volume_name || "";
  const classes = (ctx.storageClasses() || []).length ? ctx.storageClasses() : ["longhorn-r2"];
  d.innerHTML = `<div class="volume-title"><div><b>${esc(v.label || "Storage mapping")}</b>${v.role ? `<span class="tag">${esc(v.role)}</span>` : ""}${v.required ? ' <span class="pill warn">required</span>' : ""}</div>
      <button class="iconbtn row-remove" type="button" title="Remove storage mapping" onclick="removeVolumeRow(this)">×</button></div>
    ${v.description ? `<div class="dim small volume-desc">${esc(v.description)}</div>` : ""}
    <div class="deploy-volume-grid">
      <div><label>Container mount path</label><input class="vp" type="text" value="${esc(v.path || "")}" placeholder="/config"></div>
      <div><label>Storage source</label><select class="vk">
        <option value="new-rwo" ${kind === "new-rwo" ? "selected" : ""}>New Longhorn volume · RWO</option>
        <option value="new-rwx" ${kind === "new-rwx" ? "selected" : ""}>New shared volume · RWX</option>
        <option value="existing" ${kind === "existing" ? "selected" : ""}>Existing PVC · keep data</option>
        <option value="pod" ${kind === "pod" ? "selected" : ""}>${esc(ctx.podLabel)}</option>
        <option value="ephemeral" ${kind === "ephemeral" ? "selected" : ""}>Temporary pod storage · emptyDir</option>
        <option value="host" ${kind === "host" ? "selected" : ""}>Host path · advanced</option></select></div>
      <div class="vsource"><label class="vsource-label">Volume / path</label><input class="vs" type="text" value="${esc(v.source || "")}"><select class="vselect" style="display:none"></select></div>
      <div class="vnew"><div><label>Size GiB</label><input class="vz" type="number" min="1" value="${v.size_gb || 5}"></div>
        <div><label>Storage class</label><select class="vsc">${classes.map(sc => `<option ${sc === (v.storage_class || "longhorn-r2") ? "selected" : ""}>${esc(sc)}</option>`).join("")}</select></div></div>
    </div>
    <div class="volume-foot"><span class="dim small vhelp"></span><label class="switch"><input class="vro" type="checkbox" ${v.read_only ? "checked" : ""}>Read-only</label></div>
    ${v.template_source && v.template_source !== v.source ? `<div class="template-source">Unraid source: <span class="mono">${esc(v.template_source)}</span> · choose its Kubernetes backing above</div>` : ""}`;
  host.appendChild(d); syncVolumeRow(d);
  d.addEventListener("input", event => { if (event.target.matches(".vs,.vselect")) syncVolumeRow(d); ctx.onChange(); });
  d.addEventListener("change", event => { if (event.target.matches(".vk,.vs,.vselect")) syncVolumeRow(d); ctx.onChange(); });
  return d;
}

function readVolumeRow(row) {
  const kind = $(".vk", row).value;
  return { path: $(".vp", row).value.trim(), source: volumeSourceValue(row), kind,
    type: volumeType(kind),
    create: kind === "new-rwo" || kind === "new-rwx", size_gb: +$(".vz", row).value || 5,
    storage_class: $(".vsc", row).value, access_mode: kind === "new-rwx" ? "ReadWriteMany" : "ReadWriteOnce",
    read_only: $(".vro", row).checked, label: row.dataset.label || "", description: row.dataset.description || "",
    required: row.dataset.required === "true", template_source: row.dataset.templateSource || "",
    role: row.dataset.role || "", volume_name: row.dataset.volumeName || "" };
}

function readVolumeRows(host) { return host ? $$(".deploy-volume", host).map(readVolumeRow) : []; }

function renderVolumeRows(host, rows) {
  if (!host) return;
  host.innerHTML = "";
  (rows || []).forEach(v => addVolumeRow(host, v));
}

function syncVolumeRows(host) { if (host) $$(".deploy-volume", host).forEach(syncVolumeRow); }

function createVolumePicker(host, ctx = {}) {
  if (!host) return null;
  host.dataset.volumeHost = "";
  host.__volumeContext = { ...VOLUME_PICKER_DEFAULTS, ...ctx };
  return host;
}

window.removeVolumeRow = button => {
  const row = button.closest(".deploy-volume"), ctx = volumePickerContext(row);
  row.remove(); ctx.onChange();
};
window.createVolumePicker = createVolumePicker;
window.addVolumeRow = addVolumeRow;
window.renderVolumeRows = renderVolumeRows;
window.readVolumeRows = readVolumeRows;
window.syncVolumeRows = syncVolumeRows;
window.volumeKind = volumeKind;
window.volumeRowIssue = volumeRowIssue;
window.volumeListIssue = volumeListIssue;
