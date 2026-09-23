/* Unified storage picker shared by the deployment wizard and the container editor.

   A picker host is any element tagged with data-volume-host and given a context
   through createVolumePicker(). The context supplies the live namespace
   inventory (PVCs, storage classes) and the pod volumes that can be reused, so
   the same rows work while creating a workload and while editing one. */

const VOLUME_KIND_LABELS = {
  "new-rwo": "New Longhorn volume · RWO",
  "new-rwx": "New shared volume · RWX",
  existing: "Existing PVC · keep data",
  ephemeral: "Temporary pod storage · emptyDir",
  memory: "Memory scratch · RAM-backed",
  shm: "Shared memory · /dev/shm",
  host: "Host path · advanced",
};
const VOLUME_KINDS = ["new-rwo", "new-rwx", "existing", "pod", "ephemeral", "memory", "shm", "host"];
const SHM_PATH = "/dev/shm";

const VOLUME_PICKER_DEFAULTS = {
  pvcs: () => [],
  storageClasses: () => [],
  sharedStorageClasses: null,
  classFacts: () => ({}),
  podVolumes: () => [],
  allowPod: () => false,
  kinds: VOLUME_KINDS,
  podLabel: "Existing volume in this pod",
  podEmpty: "Choose a volume already in this pod…",
  podUnavailable: "No reusable volumes in this pod",
  podHelp: "Mounts a volume already defined in this pod into this container.",
  pathLabel: "Container mount path",
  pathPlaceholder: "/config",
  newSourceHelp: "",
  removable: true,
  readOnlyToggle: true,
  onChange: () => { },
};

function volumeKind(v) {
  // A memory volume already mounted at /dev/shm is shared memory, whoever
  // described it: the server reads one back as plain "memory".
  const atShm = String(v.path || "").replace(/\/+$/, "") === SHM_PATH;
  if (v.kind) return v.kind === "memory" && atShm ? "shm" : v.kind;
  if (v.type === "host") return "host";
  if (v.type === "emptyDir") {
    if (String(v.medium || "").toLowerCase() !== "memory") return "ephemeral";
    // /dev/shm is the one RAM mount with a fixed home and a reason of its own.
    return atShm ? "shm" : "memory";
  }
  if (v.type === "pod") return "pod";
  if (v.create === false) return "existing";
  return v.access_mode === "ReadWriteMany" ? "new-rwx" : "new-rwo";
}

function volumeType(kind) {
  return kind === "host" ? "host" : kind === "pod" ? "pod"
    : kind === "ephemeral" || kind === "memory" || kind === "shm" ? "emptyDir" : "pvc";
}

/* Validation runs on plain objects so it can be reused before a save without a
   picker being on screen. */
function volumeRowIssue(v) {
  const path = String((v && v.path) || "").trim();
  if (!path) return "every storage mapping needs a container mount path";
  if (!path.startsWith("/")) return `mount path "${path}" must start with /`;
  const kind = volumeKind(v || {});
  if (!["ephemeral", "memory", "shm"].includes(kind) && !String((v && v.source) || "").trim()) {
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

/* A class's name with what matters when choosing it: its replica count and
   Longhorn engine. An <option> cannot hold a pill, so the text says it. */
function storageClassLabel(name, facts) {
  const f = facts && facts[name];
  if (!f) return name;
  const bits = [];
  if (f.replicas) bits.push(`${f.replicas}×`);
  if (f.engine) bits.push(`Longhorn ${f.engine.toUpperCase()}`);
  if (f.migratable) bits.push("VM disks");
  return bits.length ? `${name} · ${bits.join(" · ")}` : name;
}
window.storageClassLabel = storageClassLabel;

function storageClassOptions(classes, selected, facts) {
  return classes.map(sc => `<option value="${esc(sc)}" ${sc === selected ? "selected" : ""}>${esc(storageClassLabel(sc, facts))}</option>`).join("");
}
window.storageClassOptions = storageClassOptions;

function storageClassBadges(facts) {
  if (!facts) return "";
  const badges = [];
  if (facts.replicas) badges.push(`<span class="tag">${esc(facts.replicas)} replica${facts.replicas === "1" ? "" : "s"}</span>`);
  if (facts.engine) badges.push(`<span class="tag ${facts.engine === "v2" ? "info" : ""}" data-tip="${facts.engine === "v2"
    ? "Longhorn's V2 data engine (SPDK): faster, and needs hugepages and a V2 disk on each node." : "Longhorn's V1 data engine, the default."}">Longhorn ${esc(facts.engine.toUpperCase())}</span>`);
  badges.push(facts.encrypted ? '<span class="tag info">encrypted</span>' : '<span class="tag">not encrypted</span>');
  badges.push(facts.expandable ? '<span class="tag ok">can grow</span>' : '<span class="tag">fixed size</span>');
  if (facts.reclaim === "Retain") badges.push('<span class="tag info">keeps data on delete</span>');
  if (facts.migratable) badges.push('<span class="tag warn" data-tip="Volumes from this class carry a second controller for VM live migration, which Longhorn will not mount into a pod.">VM disks only</span>');
  if (facts.default) badges.push('<span class="tag ok">default</span>');
  return badges.join("");
}
window.storageClassBadges = storageClassBadges;

function claimRisk(claim, ctx) {
  const target = ctx.targetNode ? ctx.targetNode() : "";
  if (!claim.node || (target && claim.node === target)) return "";
  const rwo = !(claim.access_modes || []).includes("ReadWriteMany");
  if (claim.migratable) {
    return ` It is attached on ${claim.node} and its class is migratable, so Longhorn will try to live-migrate it rather than attach it here — that mount usually fails.`;
  }
  if (rwo) {
    return ` It is ReadWriteOnce and attached on ${claim.node}, so it cannot mount here at the same time.`;
  }
  return "";
}
window.claimRisk = claimRisk;

function volumePickerHost(node) { return node.closest("[data-volume-host]"); }

function volumePickerContext(node) {
  const host = volumePickerHost(node);
  return (host && host.__volumeContext) || VOLUME_PICKER_DEFAULTS;
}

function volumeSourceValue(row) {
  const choice = $(".vselect", row);
  return String(choice && choice.style.display !== "none" ? choice.value : $(".vs", row).value).trim();
}

function claimSummary(claim) {
  const parts = [(claim.access_modes || []).map(m => m === "ReadWriteMany" ? "RWX" : m === "ReadWriteOnce" ? "RWO" : m).join("/") || "mode unknown",
    claim.size || "size unknown", claim.status];
  // Bound says nothing about whether a second pod elsewhere can mount it.
  if (claim.robustness && claim.robustness !== "healthy") parts.push(claim.robustness);
  if (claim.node) parts.push(`on ${claim.node}`);
  else if (claim.status === "Bound") parts.push("detached");
  return parts.filter(Boolean).join(" · ");
}
window.claimSummary = claimSummary;

function volumeSourceList(row, kind) {
  const ctx = volumePickerContext(row);
  let values = [];
  if (kind === "existing") values = (ctx.pvcs() || []).map(v => ({ value: v.name,
    label: claimSummary(v) }));
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

function volumeClassList(row, kind) {
  const ctx = volumePickerContext(row), select = $(".vsc", row);
  const all = ctx.storageClasses() || [];
  // Only a non-migratable class can serve ReadWriteMany to a pod.
  const shared = ctx.sharedStorageClasses ? (ctx.sharedStorageClasses() || []) : all;
  const usable = (kind === "new-rwx" ? shared : all).length
    ? (kind === "new-rwx" ? shared : all) : ["longhorn-r2"];
  const wanted = select.value || row.dataset.storageClass || "";
  select.innerHTML = usable.map(name =>
    `<option ${name === wanted ? "selected" : ""}>${esc(name)}</option>`).join("");
  if (usable.includes(wanted)) select.value = wanted;
  row.dataset.storageClass = select.value;
  return all.length - usable.length;
}

function syncVolumeRow(row) {
  const ctx = volumePickerContext(row), kind = $(".vk", row).value, allowPod = !!ctx.allowPod();
  const podOption = [...$(".vk", row).options].find(o => o.value === "pod");
  if (podOption) podOption.disabled = !allowPod;
  if (kind === "pod" && !allowPod) $(".vk", row).value = (ctx.kinds || VOLUME_KINDS).find(k => k !== "pod") || "existing";
  const actual = $(".vk", row).value, isNew = actual.startsWith("new-");
  const shm = actual === "shm", ram = actual === "memory" || shm;
  // Shared memory only means anything at /dev/shm, so the row fills that in
  // and stops it being typed somewhere it would do nothing.
  const pathInput = $(".vp", row);
  if (shm) pathInput.value = SHM_PATH;
  pathInput.disabled = shm;
  $(".vnew", row).style.display = isNew || ram ? "grid" : "none";
  $(".vnew", row).classList.toggle("ram", ram);
  const sizeLabel = $(".vsize-label", row);
  if (sizeLabel) sizeLabel.textContent = ram ? "Size MiB" : "Size GiB";
  if (ram && !row.dataset.ramSized) { $(".vz", row).value = shm ? 2048 : 1024; row.dataset.ramSized = "1"; }
  if (!ram && row.dataset.ramSized) { $(".vz", row).value = 5; delete row.dataset.ramSized; }
  const hidden = volumeClassList(row, actual);
  const badges = $(".vclass-badges", row);
  if (badges) {
    const facts = (ctx.classFacts() || {})[$(".vsc", row).value];
    badges.innerHTML = isNew ? storageClassBadges(facts) : "";
    badges.style.display = isNew && badges.innerHTML ? "flex" : "none";
  }
  volumeSourceList(row, actual);
  const source = $(".vs", row), choice = $(".vselect", row), selectable = actual === "existing" || actual === "pod";
  $(".vsource", row).style.display = actual === "ephemeral" || ram ? "none" : "block";
  source.style.display = selectable ? "none" : "block";
  choice.style.display = selectable ? "block" : "none";
  source.disabled = actual === "ephemeral" || ram;
  source.placeholder = actual === "host" ? "/mnt/storage or /dev/…" : "new PVC name";
  $(".vsource-label", row).textContent = actual === "host" ? "Host path" : actual === "pod" ? "Pod volume" : actual === "existing" ? "Existing PVC" : "New PVC name";
  const selectedSource = volumeSourceValue(row);
  const selectedPvc = actual === "existing" ? (ctx.pvcs() || []).find(v => v.name === selectedSource) : null;
  const selectedPodVolume = actual === "pod" ? (ctx.podVolumes() || []).find(v => v.name === selectedSource) : null;
  $(".vhelp", row).textContent = actual === "new-rwo" ? (ctx.newSourceHelp || "Creates a Longhorn claim for this workload (single-node attachment).")
    : actual === "new-rwx" ? (ctx.newSourceHelp || "Creates shared Longhorn storage that several pods can mount at once.") +
        (hidden ? ` ${hidden} storage class${hidden === 1 ? "" : "es"} hidden: they create live-migratable VM volumes, which Longhorn cannot mount into a pod.` : "")
    : actual === "existing" ? selectedPvc ? `${selectedPvc.name}: ${claimSummary(selectedPvc)}. The claim and data are kept.${claimRisk(selectedPvc, ctx)}` : "Mounts an existing PVC without creating or deleting it."
    : actual === "pod" ? selectedPodVolume ? `${selectedPodVolume.name}: ${selectedPodVolume.kind}${selectedPodVolume.source ? ` (${selectedPodVolume.source})` : ""}. The same storage is shared with the other container.` : ctx.podHelp
    : shm ? "Kubernetes gives every pod 64 MiB of /dev/shm and no way to ask for more, so this mounts a RAM disk over it. Apps that pass frames or buffers between processes - Frigate, Chromium, Postgres - need far more than the default. It counts against the node's memory."
    : ram ? "Creates a RAM disk of this size inside the pod. It starts empty every time and counts against the node's memory; this is what a tmpfs mount becomes."
    : actual === "ephemeral" ? "Creates temporary pod storage. Its contents are deleted when the pod is replaced; ideal for cache or transcoding."
    : "Mounts this exact host path; the container can only run where that path exists.";
}

function addVolumeRow(host, v = {}) {
  if (!host) return null;
  const ctx = volumePickerContext(host);
  const kinds = ctx.kinds || VOLUME_KINDS;
  const requested = volumeKind(v), kind = kinds.includes(requested) ? requested : kinds[0];
  const d = document.createElement("div"); d.className = "deploy-volume";
  d.dataset.label = v.label || ""; d.dataset.description = v.description || "";
  d.dataset.required = String(!!v.required); d.dataset.templateSource = v.template_source || "";
  d.dataset.role = v.role || ""; d.dataset.volumeName = v.volume_name || "";
  d.dataset.templateOrigin = v.template_origin || "";
  d.dataset.storageClass = v.storage_class || "";
  const classes = (ctx.storageClasses() || []).length ? ctx.storageClasses() : ["longhorn-r2"];
  d.innerHTML = `<div class="volume-title"><div><b>${esc(v.label || "Storage mapping")}</b>${v.role ? `<span class="tag">${esc(v.role)}</span>` : ""}${v.required ? ' <span class="pill warn">required</span>' : ""}</div>
      ${ctx.removable ? '<button class="iconbtn row-remove" type="button" title="Remove storage mapping" onclick="removeVolumeRow(this)">×</button>' : ""}</div>
    ${v.description ? `<div class="dim small volume-desc">${esc(v.description)}</div>` : ""}
    <div class="deploy-volume-grid">
      <div><label>${esc(ctx.pathLabel)}</label><input class="vp" type="text" value="${esc(v.path || "")}" placeholder="${esc(ctx.pathPlaceholder)}"></div>
      <div><label>Storage source</label><select class="vk">${kinds.map(k =>
        `<option value="${k}" ${k === kind ? "selected" : ""}>${esc(k === "pod" ? ctx.podLabel : VOLUME_KIND_LABELS[k])}</option>`).join("")}</select></div>
      <div class="vsource"><label class="vsource-label">Volume / path</label><input class="vs" type="text" value="${esc(v.source || "")}"><select class="vselect" style="display:none"></select></div>
      <div class="vnew"><div><label class="vsize-label">Size GiB</label><input class="vz" type="number" min="1" value="${v.size_gb || 5}"></div>
        <div><label>Storage class</label><select class="vsc">${storageClassOptions(classes, v.storage_class || "longhorn-r2", ctx.classFacts())}</select></div></div>
    </div>
    <div class="vclass-badges" style="display:none"></div>
    <div class="volume-foot"><span class="dim small vhelp"></span><label class="switch" ${ctx.readOnlyToggle ? "" : 'style="display:none"'}><input class="vro" type="checkbox" ${v.read_only ? "checked" : ""}>Read-only</label></div>
    ${v.template_source && v.template_source !== v.source ? `<div class="template-source">${esc(v.template_origin || "Unraid")} source: <span class="mono">${esc(v.template_source)}</span> · choose its Kubernetes backing above</div>` : ""}`;
  host.appendChild(d); syncVolumeRow(d);
  d.addEventListener("input", event => { if (event.target.matches(".vs,.vselect")) syncVolumeRow(d); ctx.onChange(); });
  d.addEventListener("change", event => { if (event.target.matches(".vk,.vs,.vselect,.vsc")) syncVolumeRow(d); ctx.onChange(); });
  return d;
}

function readVolumeRow(row) {
  const kind = $(".vk", row).value, inRam = kind === "memory" || kind === "shm";
  const size = +$(".vz", row).value || (inRam ? 1024 : 5);
  return { path: kind === "shm" ? SHM_PATH : $(".vp", row).value.trim(), source: volumeSourceValue(row), kind,
    type: volumeType(kind),
    medium: inRam ? "memory" : "",
    size_limit: inRam ? `${size}Mi` : "",
    create: kind === "new-rwo" || kind === "new-rwx", size_gb: +$(".vz", row).value || 5,
    storage_class: $(".vsc", row).value, access_mode: kind === "new-rwx" ? "ReadWriteMany" : "ReadWriteOnce",
    read_only: $(".vro", row).checked, label: row.dataset.label || "", description: row.dataset.description || "",
    required: row.dataset.required === "true", template_source: row.dataset.templateSource || "",
    template_origin: row.dataset.templateOrigin || "",
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
