/* Homestead views: Edit on an app: its pods and containers, Starts after, and moving a container. Split from views-lifecycle.js (#911); a classic script. */
/* ---------------- edit a pod and its containers ---------------- */
/* A value kept in the workload's Secret is still edited here, typed blind. */
const editEnvRow = (index, key = "", value = "", secret = false) => `<div class="f3 e-env-row">
  <div><label>Key</label><input class="ek" type="text" value="${esc(key)}"></div>
  <div><label>Value${secret ? ` ${tip("Kept in this app's Secret, not in its Deployment. Change it here as usual.")}` : ""}</label><input class="ev" type="${secret ? "password" : "text"}" autocomplete="off" value="${esc(value)}"></div>
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

const editContainerPanel = (container, index, section, mode = "edit") => {
  const env = Object.entries(container.env || {});
  const refs = container.env_refs || [];
  const ports = container.ports || [];
  const volumes = container.volumes || [];
  const managed = volumes.filter(volume => volume.managed);
  const mounts = volumes.filter(volume => !volume.managed);
  const fields = {
    basics: () => `
      <div class="f2">
        <div class="f"><label>Container name ${tip("The Kubernetes name of this container inside the pod. Each container name must be unique.")}</label><input id="e_container_name_${index}" type="text" value="${esc(container.name)}" oninput="editContainerIdentity(${index})"></div>
        <div class="f"><label>Image</label><input id="e_image_${index}" type="text" value="${esc(container.image)}" oninput="editContainerIdentity(${index})"></div>
      </div>`,
    hardware: () => `
      <div class="f2">
        <div class="f"><label>CPU reserved ${tip('The share of a CPU core the scheduler keeps for this container: 100% is one core. It is not a limit; the container can use more when the host has it spare.')}</label>${UI.quantity("cpu", `e_cpu_${index}`, container.cpu || "", { placeholder: "5", label: "CPU reserved",
          usage: editUsageWords(container.usage, "cpu") })}</div>
        <div class="f"><label>Memory reserved ${tip('The RAM the scheduler keeps for this container. It is not a limit. 1 GiB = 1024 MiB.')}</label>${UI.quantity("memory", `e_mem_${index}`, container.memory || "", { placeholder: "128", label: "Memory reserved",
          usage: editUsageWords(container.usage, "memory") || (container.usage_mb ? `the app uses about ${esc(workloadMemory(container.usage_mb))} now` : "") })}</div>
      </div>
      ${editUsageSuggestion(container.usage, index)}
      <div class="f"><label>Memory max (optional) ${tip('The most memory this container may use: past it, it is stopped and restarted. Leave empty for no limit; it cannot be below Memory reserved.')}</label>${UI.quantity("memory", `e_mem_limit_${index}`, container.memory_limit || "", { placeholder: "No limit", label: "Memory max", reserved: `e_mem_${index}` })}</div>
      <div class="subsec">Hardware passed to this container</div>
      <div class="hwchoices">${hardwareChoices(`e_hw_${index}`, container.hardware || [])}</div>
      <div class="subsec">Privileges</div>
      ${privilegeFields(`e_pv_${index}`, container.privileges || {})}`,
    address: () => `
      <div class="e-ports" id="e_ports_${index}">${ports.map(port => editPortRow(index, port)).join("")}</div>
      <button class="btn sm" type="button" onclick="editAddPort(${index})">＋ add port</button>`,
    environment: () => `
      <div class="subsec">Environment</div>
      ${refs.length ? `<div class="managed-env-list">${refs.map(ref => `<div><span class="mono">${esc(ref.name)}</span><span>${esc(ref.source)}</span>${UI.chip("managed reference", "info")}</div>`).join("")}</div><div class="dim xs managed-env-note">References remain connected to Kubernetes and are not exposed or replaced when you save.</div>` : ""}
      <div class="e-env" id="e_env_${index}">${(env.length ? env : [["", ""]]).map(([key, value]) => editEnvRow(index, key, value, (container.secret_env || []).includes(key))).join("")}</div>
      <button class="btn sm" type="button" onclick="editAddEnv(${index})">＋ add variable</button>
      <div class="dim xs">Passwords, tokens and keys are kept in this app's Secret rather than in its Deployment when you save.</div>`,
    storage: () => `
      ${managed.length ? `<div class="edit-mount-list">${managed.map(volume => UI.chip(`${volume.source || "?"} → ${volume.path}${volume.read_only ? " · read-only" : ""}`, "info")).join("")}</div><div class="dim xs edit-mount-note">ConfigMap, Secret and hardware device mounts are managed by Homestead and stay as they are.</div>` : ""}
      <div class="e-vols" id="e_vols_${index}"></div>
      <button class="btn sm" type="button" onclick="editAddVol(${index})">＋ add storage mapping</button>`,
  };
  const count = section === "environment" ? plural(env.length + refs.length, "variable")
    : section === "storage" ? plural(mounts.length, "mount") : section === "address" ? plural(ports.length, "port") : "";
  return `<details class="edit-container card flat" data-index="${index}" data-section="${section}"
    ${section === "basics" || section === "all" ? `data-original-name="${esc(container.new ? "" : container.original_name || container.name)}" data-new="${container.new ? 1 : 0}"${
      Array.isArray(container.secret_env) ? ` data-secret-env="${esc(JSON.stringify(container.secret_env))}"` : ""}` : ""} ${index === 0 ? "open" : ""}>
    <summary><span class="edit-container-chevron">›</span><span class="edit-container-title"><b>${esc(container.name)}</b><small class="mono">${esc(container.image)}</small></span>${count ? UI.chip(String(count), "neutral") : ""}</summary>
    <div class="edit-container-body">${section === "basics" || section === "all" ? `<div class="edit-container-remove"><button class="btn sm danger" type="button" data-container-remove onclick="containerRemove(${jsq(mode)},${index})">Remove container</button></div>` : ""}
      ${section === "all" ? Object.entries(fields).map(([key, draw]) => `<div class="subsec">${({ basics: "Container", hardware: "Hardware and access", environment: "Environment", storage: "Storage", address: "Ports" })[key]}</div>${draw()}`).join("") : fields[section]()}</div>
  </details>`;
};

/* What a container has used over the last week, beside the field it informs:
   the 95th percentile, since a reservation is about the usual day, and the
   peak, since a nightly burst is what the usual day hides. */
const editCpuWords = m => m >= 1000 ? `${+(m / 1000).toFixed(2)} cores` : `${+(m / 10).toFixed(1)}%`;
/* How long the use was watched, from its five-minute rounds: three hours
   that cross midnight are three hours, not two days. */
const editSpan = usage => {
  const hours = usage?.hours ?? (usage?.days || 0) * 24;
  if (hours < 24) return `${hours} hour${hours === 1 ? "" : "s"}`;
  const days = Math.floor(hours / 24);
  return `${days} day${days === 1 ? "" : "s"}`;
};
function editUsageWords(usage, kind) {
  if (!usage?.samples || usage.cpu_p95_m === undefined) return "";
  const [usual, peak] = kind === "cpu" ? [editCpuWords(usage.cpu_p95_m), editCpuWords(usage.cpu_max_m)]
    : [reservedMemory(usage.mem_p95_mi), reservedMemory(usage.mem_max_mi)];
  return `<span data-tip="95% of readings over ${esc(editSpan(usage))} were at or under ${esc(usual)}">usually at or under ${esc(usual)} · peak ${esc(peak)} over ${esc(editSpan(usage))}</span>`;
}
/* Each resource on its own, since one can go up while the other comes down. */
function editSuggestionChange(name, change, to, now) {
  if (change === "keep") return `${name} stays at <b>${esc(now)}</b>`;
  return `${change === "lower" ? "lower" : "raise"} ${name} to <b>${esc(to)}</b>${now ? ` (now ${esc(now)})` : " (none reserved now)"}`;
}
/* A reservation worked out from that use, offered rather than applied: it
   fills the fields, and nothing changes until the edit is saved. */
function editUsageSuggestion(usage, index) {
  const s = usage?.suggest;
  if (!s) return usage?.why && usage.samples ? `<div class="ui-help usage-suggest">${esc(usage.why)}</div>` : "";
  if (!s.differs) return `<div class="ui-help usage-suggest">The reservation matches its use over ${esc(editSpan(usage))}.</div>`;
  // Kubernetes refuses a reservation above a limit: the suggestion stops at it.
  const capped = (s.capped || []).map(k => k === "cpu" ? " Held at its CPU limit, which is set outside Homestead."
    : " Held at Memory max: its use and headroom would pass it.").join("");
  const cpu = editSuggestionChange("CPU", s.cpu_change, editCpuWords(parseInt(s.cpu, 10)), s.cpu_now_m ? editCpuWords(s.cpu_now_m) : "");
  const memory = editSuggestionChange("memory", s.memory_change, reservedMemory(parseInt(s.memory, 10)),
    s.memory_now_mi ? reservedMemory(s.memory_now_mi) : "");
  return `<div class="usage-suggest between"><span class="ui-help">From ${esc(editSpan(usage))} of use: ${cpu}, ${memory}.${esc(capped)}
      Saving restarts the app.</span>
    <button class="btn sm" type="button" onclick="editUseSuggested(${index},${jsq(s.cpu)},${jsq(s.memory)})">Use suggested</button></div>`;
}
window.editUseSuggested = (index, cpu, memory) => {
  [[`e_cpu_${index}`, cpu], [`e_mem_${index}`, memory]].forEach(([id, value]) => {
    const box = document.querySelector(`.qty[data-target="${CSS.escape(id)}"]`);
    const input = box?.querySelector(".qty-text");
    if (!input) return;
    input.value = value;
    input.dispatchEvent(new Event("input", { bubbles: true }));
    input.dispatchEvent(new Event("focusout", { bubbles: true }));
  });
};

/* Placement's rules, rows and notes are app-form.js's, shared with Deploy (#844). */
let EDIT_CONTAINER_NEXT = 0, EDIT_REMOVED_CONTAINERS = [];
let EDIT_REMOVED_PANELS = new Map();
let EDIT_SAVE_UNCERTAIN = false;
const nodes0 = list => (list || []).filter(n => n.schedulable !== false);
/* How the workload is reached now, in the Address step's terms. */
function editAddressNow(w, network, vips) {
  const own = (network?.services || []).find(s => s.namespace === w.ns && s.name === w.name && !s.system);
  const nodes = own && window.networkIsNodeAccess ? networkIsNodeAccess(own, network) : false;
  const ip = own && !nodes ? (own.requested_ips?.[0] || own.external_ips?.[0] || "") : "";
  const network_mode = w.network_mode === "host" ? "host" : w.lan && !w.has_service ? "lan" : own?.type === "ClusterIP" ? "internal" : "loadbalancer";
  // An address of its own shows as that address, so keeping it changes nothing.
  const vip_mode = !own ? (vips.shared ? "shared" : "auto") : nodes ? "nodes"
    : own.vip_mode === "shared" || (ip && ip === vips.shared) ? "shared" : ip ? "manual" : "auto";
  return { network_mode, vip_mode, lb_ip: vip_mode === "manual" ? ip : "" };
}
window.editAddressChanged = () => {
  const access = $("#e_net")?.value, lan = access === "lan", lanBeside = $("#e_lan_beside");
  const vip = $("#e_vip_mode")?.closest(".f");
  if (vip) vip.hidden = access !== "loadbalancer";
  if ($("#e_vip_wrap")) $("#e_vip_wrap").hidden = access !== "loadbalancer" || $("#e_vip_mode")?.value !== "manual";
  // Its own LAN address is the mode itself, or an extra beside the VIP.
  if (lanBeside) lanBeside.hidden = lan || access === "host";
  // The LAN mode turns the extra address on; leaving it puts the choice back.
  const on = $("#e_lan_on");
  if (on && lan && on.dataset.before === undefined) { on.dataset.before = on.checked ? "1" : ""; on.checked = true; }
  else if (on && !lan && on.dataset.before !== undefined) { on.checked = on.dataset.before === "1"; delete on.dataset.before; }
  editLanToggle();
  const note = $("#e_address_note");
  if (note) note.hidden = !(lan || access === "host");
};
window.editLanToggle = async () => {
  const box = $("#e_lan_box");
  if (!box) return;
  box.hidden = !($("#e_lan_on")?.checked || $("#e_net")?.value === "lan");
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
const FAILOVER_WORDS = { move: "Move to another host", wait: "Wait for its host", default: "Kubernetes default (5 min)" };
const FAILOVER_HELP = {
  move: "Starts on another host about 15 seconds after its host stops answering.",
  wait: "Waits for its host to come back. For apps tied to that host's hardware.",
  default: "Kubernetes moves it after five minutes.",
};
function failoverSelect(id, current, extra = "", label = "If its host fails") {
  return `<select id="${id}" aria-label="${esc(label)}" ${extra}>${Object.entries(FAILOVER_WORDS).map(([value, label]) =>
    `<option value="${value}" ${value === current ? "selected" : ""}>${label}</option>`).join("")}</select>`;
}
window.FAILOVER_WORDS = FAILOVER_WORDS;
window.FAILOVER_HELP = FAILOVER_HELP;

/* Which apps fit first when the cluster is short of room. */
const PRIORITY_HELP = {
  critical: "Fits first, and may evict lower tiers to fit, VMs included - never Homestead. For what the house depends on: home automation, passwords, DNS.",
  normal: "Fits in turn, as every app does by default, and may evict low apps to fit.",
  low: "Waits for room and never evicts anything; any other app that needs room evicts it first. For batch work and CI runners.",
};
window.PRIORITY_HELP = PRIORITY_HELP;
const prioritySelect = (id, tier, attrs = "", first = "") => `<select id="${id}" ${attrs}>${first}${["critical", "normal", "low"].map(t =>
  `<option value="${t}" ${t === tier ? "selected" : ""}>${t[0].toUpperCase() + t.slice(1)}</option>`).join("")}</select>`;
/* Edit's first choice for an app in a group (#430). */
function priorityGroupLabel(group, tier) {
  if (tier === null) return `From its group: ${group}'s tier can't be read just now`;
  return tier ? `From its group: ${tier}, from ${group}` : `From its group: normal, ${group} has no tier`;
}
function priorityGroupHelp(group) {
  return `It takes ${group}'s tier, set with Tier… on the group in Containers. A new tier there restarts nothing: Priority tiers… lists the apps that need a restart.`;
}
window.failoverSelect = failoverSelect;

/* Placement, at the three levels there are: the containers in one pod
   (always together - that is what a pod is), the copies of this workload,
   and other workloads. */
/* Why an app's tier can't be chosen here (#429), or "" when it can. */
function priorityFixed(w) {
  if (w.priority === "self") return "Set by Homestead's own manifest, which every upgrade applies.";
  if (w.priority_owner) return `Set priorityClassName where ${w.priority_owner} applies it.`;
  if (w.priority === "system") return "Kubernetes' own class; it is left as it is.";
  if (w.priority === "missing") return "This class isn't on the cluster: the app's new pods are refused until it is.";
  if (w.priority === "unknown") return "Homestead couldn't read the cluster's classes just now; it is left as it is.";
  return "";
}

/* ---------------- Starts after (#746) ----------------
   The apps this one waits for each time a pod of it starts. The form comes
   from GET /api/workloads/starts-after; Edit sends `starts_after` only when it
   changed, so a save that doesn't touch it never checks or rewrites it. */
const STARTS_HELP = "Each time a pod of this app starts (after a power cut, a host restart, an update or a move), it first waits until these apps answer on their Service. If one never does, it starts anyway after the time below, or keeps waiting. It does not stop the app if one of them goes away later.";
let EDIT_STARTS = { form: null, ctx: {}, dirty: false };
const startsKey = a => `${a.ns}/${a.app}`;
function startsAfterRow(row = {}, form = EDIT_STARTS.form, ctx = EDIT_STARTS.ctx) {
  const candidates = form?.candidates || [], key = row.app ? startsKey(row) : "";
  const found = candidates.find(c => startsKey(c) === key);
  const apps = candidates.map(c => {
    const value = startsKey(c), none = !(c.services || []).length;
    return `<option value="${esc(value)}"${value === key ? " selected" : ""}${none && value !== key ? " disabled" : ""}>${esc(`${c.app}${c.ns !== ctx.ns ? ` · ${c.ns}` : ""}${none ? " · no Service with a TCP port" : ""}`)}</option>`;
  });
  if (key && !found) apps.unshift(`<option value="${esc(key)}" selected>${esc(`${row.app}${row.ns !== ctx.ns ? ` · ${row.ns}` : ""} (gone)`)}</option>`);
  const ports = startsAfterPorts(row, form);
  return `<div class="aff-row starts-row"><select class="sa-app" aria-label="App it starts after" onchange="startsAfterAppPicked(this)"><option value="">choose an app…</option>${apps.join("")}</select>
    <select class="sa-port" aria-label="Port to wait for"${ports.length > 1 ? "" : " hidden"}>${ports.join("")}</select>
    <button class="iconbtn row-remove" type="button" title="Remove this app" aria-label="Remove this app" onclick="this.closest('.starts-row').remove();startsAfterChanged()">×</button></div>`;
}
/* A row's port choices: each TCP port of each Service that can show the app
   is ready, and a stored one no longer there. */
function startsAfterPorts(row, form = EDIT_STARTS.form) {
  const services = (form?.candidates || []).find(c => startsKey(c) === startsKey(row))?.services || [];
  const ports = services.flatMap(s => s.ports.map(port => {
    const value = `${s.name}:${port}`;
    return `<option value="${esc(value)}"${s.name === row.service && port === row.port ? " selected" : ""}>${esc(services.length > 1 ? value : port)}</option>`;
  }));
  if (row.service && !services.some(s => s.name === row.service && s.ports.includes(row.port)))
    ports.unshift(`<option value="${esc(`${row.service}:${row.port}`)}" selected>${esc(`${row.service}:${row.port}`)}</option>`);
  return ports;
}
function startsAfterBlock(form, ctx = {}) {
  const wrap = body => `<div class="place-level" id="e_starts"><div class="place-title">Starts after ${tip(STARTS_HELP)}</div>${body}</div>`;
  // #620: a read that failed says so, and nothing here acts on a guess.
  if (!form || form.error) return wrap(`<div class="dim small" role="status">Could not read Starts after: ${esc(form?.error || "no answer")}. Saving leaves it as it is.</div>
    ${UI.button("Retry", "startsAfterRetry()", { kind: "sm" })}`);
  if (ctx.fixed) return wrap(`<div class="dim small">Homestead and its helpers can't wait for other apps. Other apps can start after Homestead.</div>`);
  const by = form.managed_by;
  return wrap(`${by ? `<div class="dim small">${esc(by)} applies this app, so Homestead doesn't change it: commit the lines below where ${esc(by)} applies it.</div>` : ""}
    ${form.unreadable ? `<div class="dim small" id="e_starts_unread">Its Starts after setting can't be read: it was changed outside Homestead. Set it again below, or ${UI.button("Clear it", "startsAfterClear()", { kind: "sm" })}</div>` : ""}
    <div class="place-grid two">
      <div class="f"><label id="e_starts_apps">Apps it waits for</label><div id="e_starts_rows" role="group" aria-labelledby="e_starts_apps">${(form.after || []).map(row => startsAfterRow(row, form, ctx)).join("")}</div>
        <button class="btn sm" type="button" onclick="startsAfterAdd()">＋ Add</button></div>
      <div class="starts-when"><div class="f"><label for="e_starts_min">Wait up to (minutes)</label><input type="number" id="e_starts_min" min="0.5" max="60" step="0.5" value="${esc((form.timeout || 300) / 60)}"></div>
      <div class="f"><label for="e_starts_on">If it never comes up</label><select id="e_starts_on">${[["start", "Start anyway"], ["wait", "Keep waiting"]].map(([value, label]) =>
        `<option value="${value}"${(form.on_timeout || "start") === value ? " selected" : ""}>${label}</option>`).join("")}</select></div></div></div>
    ${by ? `<pre class="mono small starts-git" id="e_starts_git"${form.git && (form.after || []).length ? "" : " hidden"}>${esc(form.git || "")}</pre>
      ${UI.button("Show the lines to commit", "startsAfterLines()", { kind: "sm", attrs: 'data-need="operator"' })}` : ""}
    <div class="dim small" id="e_starts_note" aria-live="polite"></div>
    ${!by && !form.unreadable && (form.after || []).length && ["differs", "missing"].includes(form.gate)
      ? UI.button("Put the start gate back", "startsAfterRestore()", { kind: "sm", attrs: 'id="e_starts_restore"' }) : ""}
    <div class="small badtext" id="e_starts_err" role="alert" hidden></div>`);
}
/* What Edit sends: undefined when nothing changed (or the form could not be
   read, or Flux applies the app), else {after, timeout, on_timeout}, with
   restore_gate when its gate, changed outside Homestead, is to be put back. */
function startsAfterValue(form, { dirty, rows, minutes, on_timeout, restore = false }) {
  if (!form || form.error || form.managed_by || !dirty) return undefined;
  const shape = list => (list || []).map(a => ({ ns: a.ns, app: a.app, service: a.service, port: a.port }));
  const value = { after: shape(rows), timeout: Math.round(minutes * 60), on_timeout };
  if (restore) return { ...value, restore_gate: true };
  if (form.unreadable) return value;
  const same = JSON.stringify(value.after) === JSON.stringify(shape(form.after))
    && (!value.after.length || (value.timeout === form.timeout && value.on_timeout === form.on_timeout));
  return same ? undefined : value;
}
function startsAfterRows() {
  return $$("#e_starts_rows .starts-row").map(row => {
    const [ns, app] = ($(".sa-app", row).value || "/").split("/");
    const [service, port] = ($(".sa-port", row).value || "").split(":");
    return { ns, app, ...(service ? { service, port: +port } : {}) };
  }).filter(row => row.app);
}
function readStartsAfter() {
  if (!$("#e_starts_rows")) return undefined;
  return startsAfterValue(EDIT_STARTS.form, { dirty: EDIT_STARTS.dirty, restore: !!EDIT_STARTS.restore, rows: startsAfterRows(),
    minutes: +$("#e_starts_min")?.value, on_timeout: $("#e_starts_on")?.value || "start" });
}
/* Why Save can't send it, or "". The time matters only with apps to wait for. */
function startsAfterProblem(value) {
  return value?.after?.length && !(value.timeout >= 30 && value.timeout <= 3600) ? "Wait up to: between 0.5 and 60 minutes" : "";
}
/* Edit never sends a change for an app Flux applies: the review says so. */
function startsAfterManagedNote(form, dirty) {
  return form?.managed_by && dirty ? `Starts after is not saved here: ${form.managed_by} applies this app, so commit the lines shown under Placement.` : "";
}
/* The picked apps that are stopped or gone: a start would wait for them. */
function startsAfterStopped(after, others) {
  return (after || []).filter(a => !(others || []).some(o => o.ns === a.ns && o.name === a.app && o.desired)).map(a => a.app);
}
function startsAfterReviewLine(name, value) {
  if (!value) return "";
  return value.after.length ? `Saving restarts ${name} once: its pods wait for ${startsWords(value.after.map(a => a.app))} before starting.`
    : `Saving restarts ${name} once: its pods no longer wait for other apps.`;
}
/* A refusal from the server that is about Starts after, shown beside it. */
function startsAfterRefusal(text) { return /^a cycle:|can't start after itself|no Service with a TCP port|Service has no port|cluster DNS|^wait up to|never comes up|Starts after|can't wait for other apps|start order is set where/.test(String(text || "")); }
window.startsAfterShowError = text => {
  const box = $("#e_starts_err");
  if (!box) return;
  box.textContent = `Saving was refused: ${text}`;
  box.hidden = false;
  // Edit's Placement step, where the block is.
  document.getElementById("e_steps-tab-placement")?.click();
  box.scrollIntoView?.({ block: "nearest" });
};
function startsAfterNote() {
  const note = $("#e_starts_note"), form = EDIT_STARTS.form;
  if (!note || !form || form.error) return;
  const rows = startsAfterRows(), name = EDIT_STARTS.ctx.name, seconds = Math.round((+$("#e_starts_min")?.value || 0) * 60), lines = [];
  const stopped = startsAfterStopped(rows, APP_FORM_PLACEMENT.e.others);
  if (stopped.length) lines.push(`${startsWords(stopped)} ${stopped.length > 1 ? "are" : "is"} stopped: ${name}'s pods will wait for ${stopped.length > 1 ? "them" : "it"} ${$("#e_starts_on")?.value === "wait" ? `until ${stopped.length > 1 ? "they are" : "it is"} up` : `up to ${startsSpan(seconds)}`} each time they start.`);
  if (new Set(rows.map(startsKey)).size !== rows.length) lines.push("An app is named twice; it is waited for once.");
  if (rows.length && seconds > 600) lines.push("A wait over 10 min can show an update as not progressing in Kubernetes; it carries on once the app is up.");
  if (form.gate === "not-committed") lines.push("Its start gate isn't committed yet.");
  else if (["differs", "missing"].includes(form.gate) && !EDIT_STARTS.restore) lines.push("Its start gate doesn't match this setting.");
  if (EDIT_STARTS.restore) lines.push("Saving puts its start gate back, restarting it once.");
  note.textContent = lines.join(" ");
}
window.startsAfterChanged = () => { EDIT_STARTS.dirty = true; $("#e_starts_err") && ($("#e_starts_err").hidden = true); startsAfterNote(); };
window.startsAfterAdd = () => { $("#e_starts_rows").insertAdjacentHTML("beforeend", startsAfterRow()); startsAfterChanged(); };
/* Only the port choice is rebuilt: the app list keeps keyboard focus (arrow
   keys on a closed select fire change at each step). */
window.startsAfterAppPicked = select => {
  const [ns, app] = (select.value || "/").split("/");
  const port = $(".sa-port", select.closest(".starts-row"));
  if (port) {
    const ports = startsAfterPorts({ ns, app });
    port.innerHTML = ports.join("");
    port.hidden = ports.length <= 1;
  }
  startsAfterChanged();
};
window.startsAfterRestore = () => {
  EDIT_STARTS.restore = true;
  $("#e_starts_restore")?.remove();
  startsAfterChanged();
};
window.startsAfterClear = () => {
  $("#e_starts_rows").innerHTML = "";
  $("#e_starts_unread")?.remove();
  startsAfterChanged();
};
window.startsAfterRetry = async () => {
  const { ns, name } = EDIT_STARTS.ctx;
  const form = await api(`/api/workloads/starts-after?${new URLSearchParams({ ns, name })}`).catch(e => ({ error: e.message }));
  EDIT_STARTS = { ...EDIT_STARTS, form, dirty: false, restore: false };
  const block = $("#e_starts");
  if (block) block.outerHTML = startsAfterBlock(form, EDIT_STARTS.ctx);
  startsAfterNote();
};
/* An app Flux or Argo CD applies: the lines to commit for the apps chosen
   here. "lines" asks for them only; the server never saves on it. */
window.startsAfterLines = async () => {
  const { ns, name } = EDIT_STARTS.ctx, pre = $("#e_starts_git");
  const body = { ns, name, lines: true, after: startsAfterRows(), timeout: Math.round((+$("#e_starts_min")?.value || 0) * 60), on_timeout: $("#e_starts_on")?.value || "start" };
  try {
    const answer = await api("/api/workloads/starts-after", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
    if (pre) { pre.textContent = answer?.git || ""; pre.hidden = !answer?.git; }
  } catch (e) { startsAfterShowError(e.message); }
};
document.addEventListener("input", event => { if (event.target.closest?.("#e_starts")) startsAfterChanged(); });
document.addEventListener("change", event => { if (event.target.closest?.("#e_starts")) startsAfterChanged(); });

/* From a container's menu: the editor, opened at its placement. */
window.wlPlacement = async (ns, name) => {
  await wlEdit(ns, name);
  setTimeout(() => {
    const box = $(".modalbox"), section = $("#e_placement");
    // Clear of the dialog's own header, which stays at the top as it scrolls.
    if (box && section) box.scrollTo({ top: section.getBoundingClientRect().top - box.getBoundingClientRect().top + box.scrollTop - 72, behavior: "smooth" });
  }, 80);
};

window.wlEdit = async (ns, name, fromRoute = false) => {
  if (!fromRoute && window.setModalRoute) setModalRoute({ panel: "edit", ns, workload: name }, name);
  modal("Edit · " + name, `<div class="empty"><span class="spin2"></span>loading</div>`, true);
  // Placement, storage, its neighbours and its address are built from these
  // reads; a form built without one would save a change nobody made (#620).
  try {
    const [w, liveNodes, options, others, network, starts] = await Promise.all([
      api(`/api/workload?ns=${encodeURIComponent(ns)}&name=${encodeURIComponent(name)}`),
      loadHardwareFeatures().catch(e => { throw new Error(`Could not read the hardware features: ${e.message}`); })
        .then(() => mustRead("/api/nodes", "the hosts")),
      mustRead(`/api/deploy/options?ns=${encodeURIComponent(ns)}`, "the storage options"),
      STATE.data.wl ? Promise.resolve(STATE.data.wl) : mustRead("/api/workloads", "the other apps"),
      mustRead("/api/network", "the network"),
      // Starts after (#746): its block says when it could not be read, and the
      // rest of Edit still works; the save then leaves the setting as it is.
      api(`/api/workloads/starts-after?${new URLSearchParams({ ns, name })}`).catch(e => ({ error: e.message })),
    ]);
    const vips = await vipChoices(network);
    STATE.data.network = network;
    EDIT_SAVE_UNCERTAIN = false;
    // What it uses now, beside what it reserves: with one container, all of it is that container's.
    const listed = (others || []).find(x => x.ns === ns && x.name === name);
    if (listed?.mem_mb && (w.containers || []).length <= 1 && w.containers?.[0]) w.containers[0].usage_mb = listed.mem_mb;
    appFormPlacementContext("e", { ns, name, others: (others || []).filter(x => !(x.ns === ns && x.name === name)),
      nodes: nodes0(liveNodes).length, volumes: () => readVolumeRows($("#e_containers")), pvcs: () => EDIT_STORAGE.pvcs || [] });
    if (liveNodes.length) STATE.data.nodes = liveNodes;
    // Its own host stays on the list when cordoned, so Save does not unpin it.
    const nodes = (liveNodes.length ? liveNodes : (STATE.data.ov ? STATE.data.ov.nodes : [])).filter(n => n.schedulable !== false || n.name === w.node);
    const seeds = w.seed_configs || [];
    const containers = w.containers || [{ original_name: w.container_name || w.name, name: w.container_name || w.name,
      image: w.image, cpu: w.cpu, memory: w.memory, env: w.env || {}, env_refs: [], ports: w.ports || [],
      hardware: w.hardware || [], volumes: w.volumes || [] }];
    EDIT_STORAGE = { pvcs: options.pvcs || [], storage_classes: options.storage_classes || [],
      shared_storage_classes: options.shared_storage_classes || [],
      storage_class_facts: options.storage_class_facts || {}, pod_volumes: w.pod_volumes || [],
      node: w.node || "" };
    EDIT_CONTAINER_NEXT = containers.length;
    EDIT_REMOVED_CONTAINERS = [];
    EDIT_REMOVED_PANELS = new Map();
    const panels = section => `<div id="e_${section}_containers">${containers.map((container, index) => editContainerPanel(container, index, section)).join("")}</div>`;
    const basics = `
      <div class="f"><label>App name ${tip("The real Kubernetes Deployment name. Renaming creates a replacement Deployment, waits for it to become ready, then removes the old one. Generated pods use this name plus a Kubernetes suffix.")}</label><input type="text" id="e_workload_name" value="${esc(w.name)}"></div>
      ${UI.more("Optional settings", `<div class="f"><label>Pod hostname ${tip("The hostname visible inside the pod. It does not rename the Kubernetes Pod; generated pods use the app name plus a suffix.")}</label><input type="text" id="e_pod_name" value="${esc(w.pod_hostname || "")}" placeholder="optional"></div>
      <label class="switch"><input type="checkbox" id="e_service_links" ${w.service_links === false ? "" : "checked"}>
        Service environment variables ${tip("Kubernetes gives each container a NAME_HOST and NAME_PORT variable for every Service in its namespace. Turn this off when an image reads one of those names itself - a Service called immich gives IMMICH_PORT, which the Immich image refuses to start with. On is Kubernetes' default; changing it restarts the app.")}</label>
      <div class="f"><label>Container logo ${tip("The address of an image (PNG, JPEG, GIF, WebP, ICO or SVG), or of a site: Homestead then takes the site's own logo if it is an SVG or at least 64 px. It keeps its own copy, so the logo stays if the source changes.")}</label><div class="row logo-field"><input type="url" id="e_icon" value="${esc(w.icon || "")}" placeholder="https://…/logo.png or https://example.com">
        <button class="btn" type="button" onclick="logoFindInline(${jsq(ns)},${jsq(name)},'e_icon')">${icon("search")}Find</button></div><div id="e_icon_find"></div></div>`)}
      <label class="switch" id="e_autostart_wrap"><input type="checkbox" id="e_autostart" onchange="editAutostartToggle()" ${w.autostart === false ? "" : "checked"}>
        Autostart ${tip("On keeps the app running: Kubernetes restarts it after a crash, a host reboot or a cluster restart. Off scales it to zero and remembers the instance count for when you switch it back on.")}</label>
      <div class="dim xs" id="e_autostart_note" style="margin:-4px 0 6px">${w.autostart === false ? "Stays stopped until you switch autostart back on." : "Runs continuously and comes back after a reboot."}</div>
      ${appFormContainerBlock("e", "edit", containers.map((container, index) => editContainerPanel(container, index, "basics")).join(""))}`;
    EDIT_STARTS = { form: starts, dirty: false,
      ctx: { ns, name, fixed: w.priority === "self" || !!(listed?.self || listed?.homestead || listed?.managed_smb || listed?.managed_nfs) } };
    const running = appFormPlacement("e", w.placement || {}, w, nodes, containers, { startsHtml: startsAfterBlock(starts, EDIT_STARTS.ctx) });
    const access = `
      ${panels("hardware")}`;
    const environment = `
      ${panels("environment")}
      ${seeds.length ? `<div class="sec">Startup seed config ${tip("This ConfigMap is copied into the container's persistent storage by an init container before every start. It is authoritative: editing only the mounted file will be overwritten on restart.")}</div>
        <div class="note seed-note"><b>Authoritative startup configuration.</b> Saving here updates the ConfigMap and restarts the app so the init container copies the new value into appdata.</div>
        ${seeds.map((s, i) => `<div class="seed-editor card flat">
          <div class="between seed-head"><div><b>${esc(s.key)}</b><div class="dim xs mono">ConfigMap ${esc(s.config_map)} · init ${esc(s.init_container)}</div></div>${UI.chip("seeded on start", "info")}</div>
          <label for="e_seed_${i}">Contents</label>
          <textarea id="e_seed_${i}" class="e_seed mono" rows="14"
            data-init="${esc(s.init_container)}" data-config-map="${esc(s.config_map)}" data-key="${esc(s.key)}">${esc(s.value)}</textarea>
          ${s.command ? `<div class="dim xs mono seed-command">${esc(s.command)}</div>` : ""}
        </div>`).join("")}` : ""}`;
    const storage = `${UI.more("Choosing storage", "<p>RWO is best for one app; RWX permits multi-node sharing. An existing PVC keeps its current data; a volume already in this pod shares its backing storage with another container. Host paths reduce failover portability. Saving creates any new claim, then rolls the pod.</p>")}
      ${panels("storage")}`;
    const address = `${addressStepHtml("e", editAddressNow(w, network, vips), vips, vips.shared || "", {
        onChange: "editAddressChanged()",
        lanHtml: `<label class="switch" id="e_lan_beside"><input type="checkbox" id="e_lan_on" ${w.lan ? "checked" : ""} onchange="editAddressChanged()"> Also an address of its own on the LAN, beside its VIP</label>
          <div id="e_lan_box" ${w.lan ? "" : "hidden"} data-current="${esc(JSON.stringify(w.lan || null))}"></div>`,
        portsHtml: `<div class="note" id="e_address_note" hidden>With its own LAN address or the host's network, clients connect to it directly: its LAN ports and VIP are not used.</div>
          ${panels("address")}<div class="note" id="e_ports_note" hidden></div>`})}
      ${UI.more("What saving does", "<p>Saving restarts the pods only when something they run with changes: the image, ports, environment, storage, hardware or placement. A logo, its group or how it is monitored is saved without a restart. Renaming is a separate, reviewed action with a short outage; volumes and service addresses are kept. If it stops part-way, inspect the job before restarting either app.</p>")}`;
    // The app's own Secret could not be read (#1001): its variables are not in
    // the form, so a save would remove them. Said here, and Save stays off.
    window.__editSecretUnread = !!w.secret_env_unread;
    $("#mbody").innerHTML = `${editSecretUnreadNotice(w)}<div id="e_containers">${stepper("e_steps", appFormSections("edit", {
      basics, hardware: access, environment, storage, placement: running, address,
      monitoring: monitoringStepHtml({ ...w, ns, name, answer_check: w.answer_check ?? listed?.answer_check ?? "", ports: listed?.ports || [] }) }),
      editReviewButton(ns, name, w), { always: true })}</div>`;
    containers.forEach((container, index) => renderVolumeRows(editVolumePicker(index),
      (container.volumes || []).filter(volume => !volume.managed).map(editVolumeRow)));
    window.__editHadService = !!w.has_service;
    window.__editIsSelf = w.priority === "self";
    editPortsChanged();
    window.__editPortBaseline = editPortSignature();
    containerRemoveButtons();
    placementChanged();
    startsAfterNote();
    editAddressChanged();
  } catch (e) { $("#mbody").innerHTML = UI.error(e, { retry: `wlEdit(${jsArg(ns)},${jsArg(name)})`, dialog: true }); }
};
window.editAddEnv = (index, key = "", value = "") => $("#e_env_" + index).insertAdjacentHTML("beforeend", editEnvRow(index, key, value));
window.editAddPort = (index, port = {}) => {
  $("#e_ports_" + index).insertAdjacentHTML("beforeend", editPortRow(index, port));
  editPortsChanged();
};
window.editAddVol = (index, volume = {}) => addVolumeRow(editVolumePicker(index), volume);
window.editContainerIdentity = index => {
  const name = $("#e_container_name_" + index).value, image = $("#e_image_" + index).value;
  $$(`.edit-container[data-index="${index}"]`).forEach(panel => {
    $(".edit-container-title b", panel).textContent = name || `Container ${+index + 1}`;
    $(".edit-container-title small", panel).textContent = image;
  });
  editContainerListChanged();
};
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
    note.innerHTML = "<b>No port is exposed.</b> Saving removes this app's Service, so its LAN address is released and clients lose the listener.";
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
// The app's own Secret could not be read (#1001): said above the form, and
// Review changes stays off, since a save would remove what it holds.
const editSecretUnreadNotice = w => UI.notices([["warn", w.secret_env_unread]], "Secret settings not read");
const editReviewButton = (ns, name, w) =>
  `<button class="btn pri" id="e_save" ${w.secret_env_unread ? "disabled" : ""} onclick="editSave(${jsq(ns)},${jsq(name)})">Review changes</button>`;
function readEditedContainer(panel) {
  const index = panel.dataset.index;
  const env = {};
  $$(".e-env-row", $("#e_env_" + index)).forEach(row => { const key = $(".ek", row).value.trim(); if (key) env[key] = $(".ev", row).value; });
  const ports = $$(".edit-port-row", $("#e_ports_" + index)).map(row => ({ name: $(".ep-name", row).value.trim(),
    container: +$(".ep-number", row).value, protocol: $(".ep-protocol", row).value,
    host: +$(".ep-host", row).value || +$(".ep-number", row).value,
    expose: $(".ep-expose", row).checked })).filter(port => port.container);
  return { ...(panel.dataset.new === "1" ? { new: true } : { original_name: panel.dataset.originalName }), name: $("#e_container_name_" + index).value.trim(),
    image: $("#e_image_" + index).value.trim(), cpu: $("#e_cpu_" + index).value.trim(),
    memory: $("#e_mem_" + index).value.trim(), memory_limit: $("#e_mem_limit_" + index).value.trim(),
    hardware: selectedHardware("e_hw_" + index), env, ports,
    // The variables it showed from the app's Secret: the server removes only
    // those that are not sent back, and keeps any other reference (#1001).
    ...(panel.dataset.secretEnv ? { secret_env: JSON.parse(panel.dataset.secretEnv) } : {}),
    privileges: readPrivileges("e_pv_" + index) || undefined,
    volumes: readVolumeRows($("#e_vols_" + index)) };
}
function editContainerListChanged() {
  appFormPodContainers("e", $$("#e_basics_containers .edit-container").map(panel => $("#e_container_name_" + panel.dataset.index).value));
}
function containerRemovedPaint() {
  $("#e_removed_containers").innerHTML = [...EDIT_REMOVED_PANELS].map(([index, entry]) =>
    `<div class="row"><span>Removed on save: ${esc(entry.name)}. Persistent volumes and data are kept.</span>
      <button class="btn sm" type="button" onclick="containerRestore(${index})">Undo removal</button></div>`).join("");
}
window.containerRestore = index => {
  const entry = EDIT_REMOVED_PANELS.get(index);
  if (!entry) return;
  for (const panel of entry.panels) {
    const host = $("#e_" + panel.dataset.section + "_containers");
    const later = [...host.children].find(other => +other.dataset.index > index);
    host.insertBefore(panel, later || null);
  }
  EDIT_REMOVED_PANELS.delete(index);
  EDIT_REMOVED_CONTAINERS = EDIT_REMOVED_CONTAINERS.filter(name => name !== entry.original);
  containerRemovedPaint(); containerRemoveButtons(); editContainerListChanged(); editPortsChanged(); placementChanged();
};
function containerRemoveButtons() {
  const cards = $$("#e_basics_containers .edit-container");
  // With one container there is nothing to remove: the row goes, not just the button.
  cards.forEach(card => {
    const button = $("[data-container-remove]", card);
    button.disabled = cards.length <= 1;
    button.closest(".edit-container-remove").hidden = cards.length <= 1;
  });
}
window.containerAdd = mode => {
  if (mode === "deploy") return deployAddContainer();
  const index = EDIT_CONTAINER_NEXT++;
  const container = { name: `container-${index + 1}`, image: "", new: true, env: {}, ports: [], volumes: [] };
  appFormAddPanels("e", container, index, "edit");
  renderVolumeRows(editVolumePicker(index), []);
  containerRemoveButtons();
  $(`#e_basics_containers .edit-container[data-index="${index}"]`).open = true;
  $("#e_container_name_" + index).focus();
  editContainerListChanged();
};
window.containerRemove = (mode, index) => {
  if (mode === "deploy") return deployRemoveContainer(index);
  if ($$("#e_basics_containers .edit-container").length <= 1) return toast("Keep at least one container in this pod", "bad");
  const panel = $(`#e_basics_containers .edit-container[data-index="${index}"]`);
  if (panel.dataset.originalName) EDIT_REMOVED_CONTAINERS.push(panel.dataset.originalName);
  const panels = $$(`#e_containers .edit-container[data-index="${index}"]`);
  EDIT_REMOVED_PANELS.set(index, { panels, name: $("#e_container_name_" + index).value, original: panel.dataset.originalName });
  panels.forEach(card => card.remove());
  containerRemovedPaint(); containerRemoveButtons(); editContainerListChanged(); editPortsChanged(); placementChanged();
};
window.editSave = async (ns, name) => {
  if (EDIT_SAVE_UNCERTAIN) return toast("Reload the saved app before reviewing another save", "bad");
  const workloadName = $("#e_workload_name").value.trim();
  if (workloadName !== name) return window.editReview({ns, name, workload_name: workloadName});
  const containers = $$("#e_containers .edit-container[data-original-name]").map(readEditedContainer);
  if (containers.some(container => !container.name || !container.image)) return toast("Every container needs a name and image", "bad");
  if (new Set(containers.map(container => container.name)).size !== containers.length) return toast("Container names must be unique in this pod", "bad");
  const storageIssue = containers.map(container => volumeListIssue(container.volumes)).find(Boolean);
  if (storageIssue) return toast(storageIssue, "bad");
  try { await logoReady("#e_icon"); } catch (e) { return toast(e.message, "bad"); }
  const seed_configs = $$("#mbody .e_seed").map(el => ({
    init_container: el.dataset.init, config_map: el.dataset.configMap,
    key: el.dataset.key, value: el.value,
  }));
  const body = { ns, name, workload_name: workloadName, pod_hostname: $("#e_pod_name").value.trim(),
    ...($("#e_service_links") ? { service_links: $("#e_service_links").checked } : {}),
    icon: $("#e_icon").value.trim(), replicas: Math.max(1, +$("#e_rep").value || 1),
    autostart: $("#e_autostart").checked, manage_ports: true, containers, seed_configs, remove_containers: [...EDIT_REMOVED_CONTAINERS],
    placement: readPlacement(), failover: $("#e_failover")?.value || "", monitoring: monitoringFieldValue("e"),
    ...($("#e_priority") ? { priority: $("#e_priority").value } : {}),
    // The server refuses a save from a form built without the Secret's values.
    ...(window.__editSecretUnread ? { secret_env_unread: true } : {}) };
  const access = $("#e_net")?.value;
  if (["loadbalancer", "internal", "lan"].includes(access)) {
    body.address = { network_mode: access, vip_mode: $("#e_vip_mode")?.value || "shared", lb_ip: ($("#e_lb_ip")?.value || "").trim() };
    if (access === "loadbalancer" && body.address.vip_mode === "manual" && !body.address.lb_ip) return toast("Choose the specific VIP, or another VIP allocation", "bad");
  }
  if (access === "lan" && !$("#el_ip")?.value.trim()) return toast("Give it its own LAN address, or choose another access mode", "bad");
  if ($("#e_lan_on")) body.lan = (access === "lan" || $("#e_lan_on").checked) && $("#el_ip") ? containerLanRead("el") : null;
  const nodeSelect = $("#e_node");
  if (nodeSelect.value !== (nodeSelect.dataset.current || "")) body.node = nodeSelect.value || null;
  const startsAfter = readStartsAfter();
  if (startsAfterProblem(startsAfter)) return toast(startsAfterProblem(startsAfter), "bad");
  if (startsAfter) body.starts_after = startsAfter;
  // The app's own answer says whether it is Homestead; the list of apps may not have been read.
  if ((window.__editIsSelf || (STATE.data.wl || []).some(x => x.self && x.ns === ns && x.name === name)) && !body.autostart) {
    if (!(await ask(`Turning autostart off stops Homestead, and this page with it. Nothing here can start it again - it stays down until someone runs\n\n  kubectl -n ${ns} scale deployment/${name} --replicas=1\n\non the cluster. Stop it anyway?`, { title: "Turn autostart off", ok: "Stop Homestead", danger: true }))) return;
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
    (!capacity.blocked ? UI.ack("editCapacityConfirm","Move its storage despite the outage and warnings") : "") +
    UI.actions(UI.button("Back to edit","modalBack()", { attrs: 'data-dialog-dismiss="true"' }) + UI.button("Start data move","confirmEdit()",{id:"editGo",kind:"pri",disabled:capacity.blocked}));
}
/* Review, then save an Edit (#909), on the shared review flow: it keeps the sequence, the busy flag and the
   one-shot send. This view keeps its words, the managed-rename tick, and what happens when a save is refused. */
const EDIT_REVIEW = UI.reviewFlow({
  preview: async config => {
    const review = await api("/api/edit/preview", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(config) });
    if (!review.capacity || !review.capacity_token) throw new Error("Capacity review unavailable; refresh before saving.");
    return { config, ...review };
  },
  render: (review, config) => {
    const rename = review.capacity.rename;
    const concerns = capacityNotes(review.capacity).concerns;
    const copying = !!review.capacity.copy_helper;
    if (copying) {
      childModal("Move container data", UI.head({ kind: "App", name: config.name, where: config.namespace || config.ns || "" }) + gitopsNote(review.managed) + storageCopyReview(config,review.capacity), true, "operation-review");
      return;
    }
    childModal(rename ? "Rename app" : "Review app changes", `${UI.head({ kind: "App", name: config.name, where: config.namespace || config.ns || "" })}
      ${gitopsNote(review.managed, { rename: !!rename, yaml: review.yaml })}
      ${!rename && config.starts_after ? `<p>${esc(startsAfterReviewLine(config.name, config.starts_after))}</p>` : ""}
      ${!rename && startsAfterManagedNote(EDIT_STARTS.form, EDIT_STARTS.dirty) ? `<p>${esc(startsAfterManagedNote(EDIT_STARTS.form, EDIT_STARTS.dirty))}</p>` : ""}
      ${!rename && review.capacity.container_changes ? `<p data-whole>Containers added: ${review.capacity.container_changes.added.map(esc).join(", ") || "none"}. Containers removed: ${review.capacity.container_changes.removed.map(esc).join(", ") || "none"}. Every pod rolls out; persistent volumes and data are kept.</p>` : ""}
      ${rename ? `<p><b>${esc(rename.from)}</b> → <b>${esc(rename.to)}</b></p><p>Only the app name changes. Save other edits separately. Expect a short outage; volumes and service addresses are kept.</p>` : ""}
      ${rename ? `${UI.callout(review.capacity.blocked ? "bad" : "", "", `${review.capacity.blocked ? "Rename is blocked by the placement check. Review the details below." : "If a step fails, inspect both app names in Recent jobs. Homestead will not automatically restart the old copy or remove the replacement."}`)}
        <details ${review.capacity.blocked ? "open" : ""}><summary>Capacity and placement · ${plural((review.capacity.warnings || []).length, "warning")}</summary>${deployCapacityHtml(review.capacity)}</details>` : `${review.capacity.blocked || concerns.length ? UI.callout(review.capacity.blocked ? "bad" : "warn", review.capacity.blocked ? "Changes blocked" : "Check before saving", concerns.length ? `<ul class="ui-list">${concerns.map(c => `<li>${esc(c)}</li>`).join("")}</ul>` : "Change the resources, storage or host selection before saving.") : ""}${UI.more("Capacity and placement", deployCapacityHtml(review.capacity))}` }
      ${gitopsYamlBlock(review.yaml, review.secret)}
      ${!review.capacity.blocked ? `<label class="switch"><input type="checkbox" id="editCapacityConfirm"> ${rename ? "Rename it despite the outage" : concerns.length ? "Restart it anyway" : "Restart it now"}</label>` : ""}
      ${UI.actions(`<button data-dialog-dismiss="true" class="btn" onclick="modalBack()">Back to edit</button><button id="editGo" class="btn pri" ${review.capacity.blocked ? "disabled" : ""} onclick="confirmEdit()">${rename ? "Rename app" : "Save changes"}</button>`)}`, true, "operation-review");
  },
  failed: (e, config) => {
    // A refusal about Starts after (a loop, say) is shown beside it too.
    if (config.starts_after && e.status === 400 && startsAfterRefusal(e.message)) startsAfterShowError(e.message);
    toast(e.message, "bad");
  },
  blocked: review => review.capacity.blocked, ack: "editCapacityConfirm",
  apply: async review => {
    const body = { ...review.config, capacity_token: review.capacity_token, confirm_capacity: true };
    const { ns, name, workload_name: workloadName } = body;
    const renaming = workloadName && workloadName !== name;
    // Flux or Argo CD brings the old name back (#734): its own tick, checked before this, sent so the server knows.
    if (renaming && review.managed?.by) body.confirm_managed = true;
    const button = $("#editGo");
    button.disabled = true;
    button.textContent = renaming ? "Renaming… follow Recent jobs" : "Saving changes…";
    if (renaming) window.startOperationChecks?.();
    try {
      const result = await api("/api/edit", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
      const activeName = result.name || workloadName || name;
      if (result.operation) window.noteOperation?.(result.operation);
      toast(renaming ? `${name} renamed to ${activeName}` : result.operation ? `${activeName} saved; copying its data under Jobs` :
        result.network || (renaming ? `${name} renamed to ${activeName}` : `${activeName} updated`), "ok");
      closeModal(); setTimeout(() => refresh(true), 1200);
    } catch (e) {
      window.startOperationChecks?.();
      EDIT_SAVE_UNCERTAIN = true;
      toast(e.message, "bad");
      button.disabled = true;
      button.textContent = "Inspect before saving again";
      button.onclick = null;
      $("#mbody").insertAdjacentHTML("beforeend", `${UI.callout("warn", { html: `${esc(e.message)}` }, `<p>${renaming
      ? "The rename may have partly completed. Inspect both app names and Recent jobs before another action."
      : "Some changes may already be saved, including new containers or volumes. Reload the saved app to inspect what applied before making another change. Reload replaces this form's unsaved edits; the app does not need a new name."}</p>
      <button class="btn" onclick="${renaming ? "closeModal();go('workloads')" : `wlEdit(${jsq(ns)},${jsq(name)},true)`}">${renaming ? "Inspect apps" : "Reload saved app"}</button>`, { id: "editSaveNotice", attrs: 'role="alert"' })}`);
      $("#editSaveNotice")?.scrollIntoView({ block: "nearest" });
    }
  },
});
window.editReview = async body => {
  if (EDIT_SAVE_UNCERTAIN) return toast("Reload the saved app before reviewing another save", "bad");
  return EDIT_REVIEW.open(JSON.parse(JSON.stringify(body)));
};
window.confirmEdit = async () => {
  if (!EDIT_REVIEW.ready()) return toast("Review the changes and acknowledge their warnings first", "bad");
  const review = EDIT_REVIEW.review, { name, workload_name: workloadName } = review.config;
  if (workloadName && workloadName !== name && review.managed?.by && !$("#editManagedConfirm")?.checked)
    return toast(`Tick "${review.managed.by} will bring back the old name" first`, "bad");
  await EDIT_REVIEW.apply();
};

/* ---------------- move a container ---------------- */
window.wlMoveLegacy = async (ns, name) => {
  const nodes = (STATE.data.ov ? STATE.data.ov.nodes : []);
  modal("Move · " + name, `
    <p class="muted small">Pick the host this app should run on. Homestead pins it with a
    node selector and rolls the pod.</p>
    <div class="f" style="margin-top:14px"><label>Target host</label><select id="mv_node">
      <option value="">any host (unpin)</option>
      ${nodes.map(n => `<option value="${esc(n.name)}">${esc(n.name)} · ${n.cpu_pct}% cpu, ${n.mem_pct}% ram${n.igpu ? " · iGPU" : ""}</option>`).join("")}
    </select></div>
    ${UI.actions(`<button class="btn pri" onclick="doMove(${jsq(ns)},${jsq(name)})">Move</button>
      <button data-dialog-dismiss="true" class="btn" onclick="closeModal()">Cancel</button>`)}
    <div class="note" style="margin-top:14px">This is a stop-then-start, not a live move —
    a ReadWriteOnce volume can only attach to one node at a time.</div>`);
};
window.doMove = async (ns, name) => {
  await window.hostMoveReview({ ns, name, node: $("#mv_node").value || null, pin: true });
};

// Node only (tests): the same names, for require(). The browser has no `module`.
if (typeof module === "object" && module.exports) module.exports = {
  editEnvRow, editPortRow, EDIT_STORAGE, editVolumeRow, editVolumePicker, editContainerPanel, editCpuWords,
  editSpan, editUsageWords, editSuggestionChange, editUsageSuggestion, EDIT_CONTAINER_NEXT, EDIT_REMOVED_PANELS,
  EDIT_SAVE_UNCERTAIN, nodes0, editAddressNow, FAILOVER_WORDS, FAILOVER_HELP, failoverSelect, PRIORITY_HELP,
  prioritySelect, priorityGroupLabel, priorityGroupHelp, priorityFixed, STARTS_HELP, EDIT_STARTS, startsKey,
  startsAfterRow, startsAfterPorts, startsAfterBlock, startsAfterValue, startsAfterRows, readStartsAfter,
  startsAfterProblem, startsAfterManagedNote, startsAfterStopped, startsAfterReviewLine, startsAfterRefusal,
  startsAfterNote, editPortSignature, editSecretUnreadNotice, editReviewButton, readEditedContainer,
  editContainerListChanged, containerRemovedPaint, containerRemoveButtons, storageCopyReview, EDIT_REVIEW
};
