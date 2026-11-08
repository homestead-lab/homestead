/* Homestead Deploy: a new app (#911). One of the scripts views-workloads.js was split into; they share the page's globals and load in the order index.html lists them. */
/* ---------------- deploy ---------------- */
let DEPLOY_CONTAINER_NEXT = 10000;
function deployContainerStorage() {
  EDIT_STORAGE = { ...DOPT, pod_volumes: [], node: "" };
}
/* The other containers: a panel in each section that holds containers, as in Edit (#544). */
function renderDeployContainers() {
  if (!$("#d_basics_containers")) return;
  APP_FORM_CONTAINER_SECTIONS.forEach(section => { $(`#d_${section}_containers`).innerHTML = ""; });
  deployContainerStorage();
  for (const container of DCFG.additional_containers || []) {
    const index = DEPLOY_CONTAINER_NEXT++;
    appFormAddPanels("d", { ...container, new: true }, index, "deploy");
    renderVolumeRows(editVolumePicker(index), container.volumes || []);
  }
}
window.deployAddContainer = () => {
  collect();
  DCFG.additional_containers ||= [];
  DCFG.additional_containers.push({ name: `container-${DCFG.additional_containers.length + 2}`, image: "", cpu: "50m", memory: "128Mi", env: {}, ports: [], volumes: [] });
  renderDeployContainers(); syncSummary();
  UI.selectSection("containerDeploy", "basics");
  const cards = $$("#d_basics_containers .edit-container");
  cards.at(-1).open = true;
  $("input", cards.at(-1)).focus();
};
window.deployRemovePrimary = async () => {
  const cfg = collect(), extras = [...(cfg.additional_containers || [])];
  if (!extras.length) return toast("Keep at least one container in this pod", "bad");
  const next = extras.shift();
  const cleared = { command: [], args: [], env_meta: [], env_bindings: {}, template_devices: [], volume_owners: {}, app_profile: null, gpu: false, cap_add: [], privileged: false, tun: false };
  await viewDeploy({ ...cfg, ...cleared, ...next, ...(next.privileges || {}),
    name: cfg.workload_name, workload_name: cfg.workload_name, container_name: next.name, additional_containers: extras })
    .catch(e => toast(e.message, "bad"));
};
window.deployRemoveContainer = index => {
  $$(`#containerDeploy .edit-container[data-index="${index}"]`).forEach(panel => panel.remove()); syncSummary();
};
const deployDefaults = () => ({ name: "", workload_name: "", container_name: "", image: "", icon: "", namespace: typeof defaultNamespace === "function" ? defaultNamespace() : "lab", replicas: 1,
  cpu: "50m", memory: "128Mi", memory_limit: "", ports: [], env: {}, env_meta: [], volumes: [], hardware: [],
  template_devices: [], target_mode: "new", target_workload: "", network_mode: "loadbalancer",
  vip_mode: "shared", lb_ip: "", env_bindings: {}, app_profile: null });
let DCFG = deployDefaults(), DOPT = { deployments: [], pvcs: [], storage_classes: [], shared_storage_classes: [] }, DRENDERING = false;
/* The storage row that would give a container the host's container runtime
   socket, as the server reads it (homestead_deploy_manifests.runtime_socket),
   from the form as it is now: removing the row in Storage lifts the block (#1294). */
const RUNTIME_SOCKETS = ["/var/run/docker.sock", "/run/docker.sock", "/run/containerd/containerd.sock",
  "/var/run/containerd/containerd.sock", "/var/run/crio/crio.sock", "/run/k3s/containerd/containerd.sock"];
function deployRuntimeSocket(c) {
  const rows = [...(c.volumes || []).map(v => [c.container_name, v]),
    ...(c.additional_containers || []).flatMap(x => (x.volumes || []).map(v => [x.name, v]))];
  const hit = rows.find(([, v]) => {
    const host = v.kind === "host" || v.type === "host";
    return RUNTIME_SOCKETS.includes(String(v.path || "").replace(/\/+$/, ""))
      || (host && RUNTIME_SOCKETS.includes(String(v.source || "").replace(/\/+$/, "")));
  });
  return hit ? { container: hit[0], path: hit[1].path || hit[1].source } : null;
}
function deployBlockedHtml(c) {
  const socket = deployRuntimeSocket(c);
  if (!socket) return "";
  return UI.callout("bad", "Review is not available yet",
    `${esc(socket.container || "A container")} mounts the host's container runtime socket at <span class="mono">${esc(socket.path)}</span>, which lets it create and control other containers. Homestead does not deploy that. Remove that row under Storage to continue: features that need the socket then do not work.
    <div class="row" style="margin-top:8px"><button type="button" class="btn sm" onclick="UI.selectSection('containerDeploy','storage')">Open Storage</button></div>`);
}
function generatedSecret() {
  const bytes = new Uint8Array(18); crypto.getRandomValues(bytes);
  return btoa(String.fromCharCode(...bytes)).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}
/* A new app as Placement reads it (#844): it moves when its host fails and
   takes the normal tier, as Deploy always built it, unless chosen otherwise.
   A tier the form does not know (#431) is shown as normal. */
function deployPlacementApp(cfg) {
  const tier = ["critical", "normal", "low"].includes(cfg.priority) ? cfg.priority : "normal";
  return { failover: cfg.failover || "move", priority: tier, start_replicas: cfg.replicas ?? 1, node: cfg.node || "" };
}
async function viewDeploy(pre) {
  pre = pre || window.__deployPrefill;
  // The form's hosts, namespaces, storage and addresses come from these reads;
  // without one it would offer a guess (#620). The page shows the error instead.
  const [, liveNodes, nss, network, others] = await Promise.all([loadHardwareFeatures(), mustRead("/api/nodes", "the hosts"),
    mustRead("/api/namespaces", "the namespaces"), mustRead("/api/network", "the network"),
    STATE.data.wl ? Promise.resolve(STATE.data.wl) : mustRead("/api/workloads", "the other apps")]);
  // Every read is done before the form's settings change, so a failure leaves them as they were.
  const next = Object.assign(deployDefaults(), pre || {});
  if (!nss.includes(next.namespace)) next.namespace = nss.includes("lab") ? "lab" : nss[0];
  const options = await mustRead("/api/deploy/options?ns=" + encodeURIComponent(next.namespace), "the storage options");
  if (liveNodes.length) STATE.data.nodes = liveNodes;
  DCFG = next;
  DOPT = options;
  // Cleared only now, so Retry after a failed read opens the same template.
  window.__deployPrefill = null;
  DCFG.workload_name ||= DCFG.name || "";
  DCFG.container_name ||= DCFG.name || "";
  (DCFG.env_meta || []).forEach(meta => {
    meta.binding = (DCFG.env_bindings || {})[meta.key] || "";
    if (meta.binding) DCFG.env[meta.key] = "";
    if (meta.generate && !DCFG.env[meta.key]) DCFG.env[meta.key] = generatedSecret();
  });
  (DCFG.template_devices || []).forEach(device => {
    const devicePaths = [device.host_path, device.container_path].filter(Boolean);
    const feature = (STATE.data.hardwareFeatures || []).find(f => {
      const featurePaths = [f.host_path, f.container_path].filter(Boolean);
      return featurePaths.some(fp => devicePaths.some(dp => dp === fp || dp.startsWith(fp + "/") || fp.startsWith(dp + "/")));
    });
    if (feature && !DCFG.hardware.includes(feature.id)) DCFG.hardware.push(feature.id);
  });
  const vips = await vipChoices(network);
  const sharedVip = vips.shared || "";
  resetPaint();
  appFormPlacementContext("d", { ns: DCFG.namespace, others: others || [], nodes: nodes0(liveNodes).length,
    volumes: () => readVolumeRows($("#containerDeploy")), pvcs: () => DOPT.pvcs || [] });
  const sections = appFormSections("deploy", {
    basics: `
      ${DCFG.app_profile ? `<div class="app-profile ${esc(DCFG.app_profile.level || "review")}">
        ${UI.moduleHeader(`${esc(DCFG.app_profile.label || "Template guidance")}`, `Review this template’s requirements.`, UI.chip(DCFG.app_profile.intent || "template", DCFG.app_profile.level === "dependency" ? "warn" : "neutral"))}
        ${DCFG.app_profile.notes?.length ? UI.more("Template notes", DCFG.app_profile.notes.map(note => `<p>${esc(note)}</p>`).join(""), !!DCFG.app_profile.blocked) : ""}
        ${(DCFG.app_profile.dependencies || []).length ? `<div class="dependency-list">${DCFG.app_profile.dependencies.map(dep => `<div class="dependency-row stranded"><span>${esc(dep.name)}</span><b>${dep.managed ? "managed" : "deploy separately"}</b></div>`).join("")}</div>` : ""}
      </div>` : ""}
      <div class="f"><label>Deployment model ${tip("A new app gets its own pod and lifecycle. Adding to an existing app creates a sidecar container; Kubernetes restarts that app's pods to apply it.")}</label>
        <select id="d_target_mode"><option value="new" ${DCFG.target_mode !== "existing" ? "selected" : ""}>New app · independent pod</option>
          <option value="existing" ${DCFG.target_mode === "existing" ? "selected" : ""}>Add container to existing app · shared pod</option></select></div>
      <div id="d_join_wrap" class="joinbox">
        <div class="f"><label>Existing app</label><select id="d_target_workload"></select></div>
        <div id="d_join_note" class="note"></div>
      </div>
      <div class="f" id="d_workload_name_wrap"><label>App name ${tip("The stable name for this app. Kubernetes adds a generated suffix to each running pod, such as my-app-7d9f8c6b5-x2abc.")}</label><input type="text" id="d_workload_name" value="${esc(DCFG.workload_name)}" placeholder="my-app"></div>
      <div class="f"><label>Container name ${tip("The name of the container inside the pod. It can differ from the app name and must use lowercase letters, numbers, and dashes.")}</label><input type="text" id="d_container_name" value="${esc(DCFG.container_name)}" placeholder="my-app"></div>
      <div class="f"><label>Image ${tip("The registry image and tag Kubernetes will pull, for example ghcr.io/home-assistant/home-assistant:stable")}</label><input type="text" id="d_image" value="${esc(DCFG.image)}" placeholder="nginx:alpine · ghcr.io/user/app:tag"><span class="dim xs" id="d_image_note">${imagePullNote(DCFG.image)}</span></div>
      <div class="row" id="d_primary_actions"><button class="btn danger" type="button" id="d_remove_primary" onclick="deployRemovePrimary()" disabled>Remove this container</button></div>
      ${UI.more("Container appearance", `<div class="f"><label>Container logo ${tip("The address of an image (PNG, JPEG, GIF, WebP, ICO or SVG), or of a site: Homestead then takes the site's own logo if it is an SVG or at least 64 px. It keeps its own copy, so the logo stays if the source changes.")}</label><input type="url" id="d_icon" value="${esc(DCFG.icon || "")}" placeholder="https://…/logo.png or https://example.com"></div>`)}
      <div class="f"><label>Namespace</label><select id="d_ns">${nss.map(n => `<option ${n === DCFG.namespace ? "selected" : ""}>${esc(n)}</option>`).join("")}</select></div>
      ${appFormContainerBlock("d", "deploy", "")}
      <div id="d_containers_join" class="empty small" hidden>Add one container to the existing app at a time.</div>
    `,
    hardware: `
      <div class="f2 compact-fields">
        <div class="f"><label>CPU reserved ${tip('The share of a CPU core the scheduler keeps for this container: 100% is one core. It is not a limit; the container can use more when the host has it spare.')}</label>${UI.quantity("cpu", "d_cpu", DCFG.cpu, { placeholder: "5", label: "CPU reserved" })}</div>
        <div class="f"><label>Memory reserved ${tip('The RAM the scheduler keeps for this container. It is not a limit. 1 GiB = 1024 MiB.')}</label>${UI.quantity("memory", "d_mem", DCFG.memory, { placeholder: "128", label: "Memory reserved" })}</div>
      </div>
      <div class="f"><label>Memory max (optional) ${tip('The most memory this container may use: past it, it is stopped and restarted. Leave empty for no limit; it cannot be below Memory reserved.')}</label>${UI.quantity("memory", "d_mem_limit", DCFG.memory_limit || "", { placeholder: "No limit", label: "Memory max", reserved: "d_mem" })}</div>
      <div class="sec">Hardware ${tip("Homestead adds the device path and schedules only onto hosts marked as having that hardware.")}</div>
      <div class="hwchoices">
        ${hardwareChoices("d_hw", (DCFG.hardware || []).concat(DCFG.gpu && !(DCFG.hardware || []).includes("igpu") ? ["igpu"] : []))}
      </div>
      <div class="sec">Privileges ${tip("What the container may do to its host beyond the defaults. VPN containers need the tunnel.")}</div>
      ${DCFG.tun || DCFG.privileged || (DCFG.cap_add || []).length ? '<div class="note">Imported privileges: review host access before deploying.</div>' : ""}
      ${privilegeFields("d_pv", DCFG)}
      ${(DCFG.template_devices || []).length ? `<div class="note import-device-note"><b>Imported device mappings:</b> ${(DCFG.template_devices || []).map(d => `<span class="mono">${esc(d.host_path || "?")} → ${esc(d.container_path || "?")}</span>`).join(", ")}. Matching hardware features were selected; review them before deploying.</div>` : ""}
      <div id="d_hardware_containers"></div>
    `,
    environment: `
      <div class="sec">Environment ${tip("Environment variables are passed directly to the container. App Store defaults are imported and remain editable.")}</div><div id="d_env"></div><button class="btn sm" onclick="addEnv()">＋ add variable</button>
      <div id="d_environment_containers"></div>
    `,
    storage: `
      <div class="sec">Storage ${tip("The mount path is inside the container. Choose whether its backing storage is a new Longhorn claim, an existing claim, an existing volume in a shared pod, or a path on one host.")}</div>
      ${UI.more("Choose a storage type", "RWO suits a single app. RWX allows sharing across hosts. Existing claims keep their data. Pod volumes share storage between containers in a pod. Host paths tie data to one host.")}
      <div id="d_vols"></div><button class="btn sm" onclick="addVol()">＋ add storage mapping</button>
      <div id="d_storage_containers"></div>
    `,
    // Edit's section (#844), applied in the first rollout. A joined app keeps its own.
    placement: `<div id="d_placement_join" class="empty small" hidden>The app it joins keeps its own placement, copies and tier: change them in its Edit.</div>
      <div id="d_placement_wrap">${appFormPlacement("d", DCFG.placement || {}, deployPlacementApp(DCFG), nodes0(liveNodes), [{ name: DCFG.container_name }], { minCopies: 0 })}</div>`,
    address: `${addressStepHtml("d", DCFG, vips, sharedVip, {
        lanHtml: '<div id="d_lan_box" hidden></div>',
        portsHtml: `<div id="d_ports"></div><button class="btn sm" onclick="addPort()">＋ add port</button><div id="d_address_containers"></div>`})}
    `,
    monitoring: monitoringFieldHtml("d", DCFG.monitoring ? (DCFG.monitoring.mode === "http" ? DCFG.monitoring.path : DCFG.monitoring.mode === "auto" ? "" : DCFG.monitoring.mode) : ""),
    summary: `
      <div id="d_blocked"></div>
      <div id="d_summary" class="deploy-summary"></div>
    `,
  }, { leads: {
    basics: "Name the app and its first container.",
    hardware: "Set resources and host access for the first container.",
    environment: "Set the values the first container needs.",
    storage: "Choose where the first container stores its data.",
    placement: "Choose its hosts, how its copies spread and which apps it runs beside.",
    address: "Choose how clients reach the app.",
    monitoring: "Choose how Homestead checks the app is up.",
    summary: "Check the configuration, then review capacity and rollout impact.",
  } });
  paint(`${UI.pageHeader("Deploy a container", "Configure the app, then review before deploying.", '<button class="btn" data-need="operator" onclick="composeImport()">Import Docker Compose</button>')}
    <div class="card flat deploy-form">${UI.sectionForm("containerDeploy", sections,
      UI.button("Review deployment", "doDeploy()", {kind:"pri",id:"d_review",disabled:!!deployRuntimeSocket(DCFG)}),
      {page:true,cancelHtml:UI.button("Cancel", "go('workloads')", { attrs: 'data-dialog-dismiss="true"' })})}</div>`);
  DRENDERING = true;
  renderDeployTargets(); renderPorts(); renderVols(); renderEnv(); renderDeployContainers(); applyDeployMode();
  DRENDERING = false; syncSummary();
  ["d_workload_name", "d_container_name", "d_image", "d_icon", "d_cpu", "d_mem", "d_mem_limit", "d_net", "d_vip_mode", "d_lb_ip", "d_target_workload", "d_placement"].forEach(id => {
    const el = $("#" + id); if (!el) return;
    el.addEventListener("input", syncSummary); el.addEventListener("change", syncSummary);
  });
  // The other containers' fields, in whichever section they sit.
  for (const type of ["input", "change"])
    $("#containerDeploy").addEventListener(type, event => { if (event.target.closest(".edit-container")) syncSummary(); });
  $("#d_target_mode").addEventListener("change", () => { applyDeployMode(); syncSummary(); });
  $("#d_target_workload").addEventListener("change", () => { collect(); updateJoinNote(); renderVols(); syncSummary(); });
  $("#d_ns").addEventListener("change", refreshDeployOptions);
  $("#d_net").addEventListener("change", deployLanChanged);
  deployLanChanged();
  $$(".d_hw").forEach(el => el.addEventListener("change", syncSummary));
}
function selectedTarget() { return DOPT.deployments.find(x => x.name === $("#d_target_workload")?.value); }
function renderDeployTargets() {
  const select = $("#d_target_workload"); if (!select) return;
  select.innerHTML = DOPT.deployments.length
    ? DOPT.deployments.map(d => `<option value="${esc(d.name)}" ${d.name === DCFG.target_workload ? "selected" : ""}>${esc(d.name)} · ${d.containers.length} container${d.containers.length === 1 ? "" : "s"}</option>`).join("")
    : `<option value="">No Deployments in this namespace</option>`;
  if (!select.value && DOPT.deployments.length) select.value = DOPT.deployments[0].name;
  updateJoinNote();
}
function updateJoinNote() {
  const target = selectedTarget(), note = $("#d_join_note"); if (!note) return;
  note.innerHTML = target
    ? `<b>Shared lifecycle:</b> Adding this container updates <span class="mono">${esc(target.name)}</span> and may restart all ${target.containers.length} existing containers. Network and placement are shared; review shows the rollout impact.`
    : `<b>No existing app is available.</b> Select another namespace or create a new app.`;
}
async function refreshDeployOptions() {
  collect();
  const ns = $("#d_ns").value;
  APP_FORM_PLACEMENT.d.ns = ns;
  try { DOPT = await api("/api/deploy/options?ns=" + encodeURIComponent(ns)); }
  catch (e) { toast("Could not load namespace storage: " + e.message, "bad"); DOPT = { deployments: [], pvcs: [], storage_classes: [] }; }
  DCFG.target_workload = ""; renderDeployTargets(); renderVols(); renderDeployContainers(); applyDeployMode(); syncSummary();
}
function applyDeployMode() {
  const joining = $("#d_target_mode")?.value === "existing";
  $("#d_join_wrap").style.display = joining ? "block" : "none";
  $("#d_workload_name_wrap").style.display = joining ? "none" : "block";
  // The app it joins keeps its own placement, copies and tier.
  $("#d_placement_wrap").hidden = joining;
  $("#d_placement_join").hidden = !joining;
  $("#d_primary_actions").hidden = joining;
  // Joining adds one container: no list of others, in any section.
  $("#d_containers_block").hidden = joining;
  APP_FORM_CONTAINER_SECTIONS.forEach(section => { $(`#d_${section}_containers`).hidden = joining; });
  $("#d_containers_join").hidden = !joining;
  const host = [...$("#d_net").options].find(o => o.value === "host");
  if (host) host.disabled = joining;
  if (joining && $("#d_net").value === "host") $("#d_net").value = "internal";
  syncVolumeRows($("#d_vols"));
}
function collect() {
  DCFG.workload_name = $("#d_workload_name").value.trim();
  DCFG.container_name = $("#d_container_name").value.trim();
  DCFG.name = $("#d_target_mode").value === "existing" ? DCFG.container_name : DCFG.workload_name;
  DCFG.image = $("#d_image").value.trim();
  DCFG.namespace = $("#d_ns").value; DCFG.replicas = +$("#d_rep").value;
  DCFG.target_mode = $("#d_target_mode").value; DCFG.target_workload = $("#d_target_workload").value;
  DCFG.cpu = $("#d_cpu").value.trim(); DCFG.memory = $("#d_mem").value.trim();
  DCFG.memory_limit = $("#d_mem_limit").value.trim(); DCFG.icon = $("#d_icon").value.trim();
  if ($("#d_mon")) DCFG.monitoring = monitoringFieldValue("d");
  DCFG.priority = $("#d_priority")?.value || "normal";
  // Placement (#844), as Edit sends it.
  DCFG.placement = readPlacement("d"); DCFG.node = $("#d_node")?.value || ""; DCFG.failover = $("#d_failover")?.value || "move";
  DCFG.hardware = selectedHardware("d_hw");
  Object.assign(DCFG, readPrivileges("d_pv") || {});
  DCFG.gpu = DCFG.hardware.includes("igpu"); DCFG.network_mode = $("#d_net").value;
  DCFG.vip_mode = $("#d_vip_mode").value; DCFG.lb_ip = $("#d_lb_ip").value.trim();
  DCFG.lan = DCFG.network_mode === "lan" && $("#dl_ip") ? containerLanRead("dl") : null;
  DCFG.ports = $$("#d_ports .port-row").map(r => ({ container: +$(".pc", r).value,
    host: +$(".ph", r).value || +$(".pc", r).value, protocol: $(".pp", r).value, expose: $(".pe", r).checked }));
  DCFG.volumes = readVolumeRows($("#d_vols"));
  if (DCFG.target_mode === "new") DCFG.additional_containers = $$("#d_basics_containers .edit-container[data-original-name]").map(readEditedContainer);
  DCFG.env = {}; $$("#d_env .env-row").forEach(r => { const k = $(".ek", r).value.trim(); if (k) DCFG.env[k] = $(".ev", r).value; });
  const removePrimary = $("#d_remove_primary");
  if (removePrimary) removePrimary.disabled = DCFG.target_mode !== "new" || !DCFG.additional_containers?.length;
  if (DCFG.target_mode !== "existing") return DCFG;
  const joined = { ...DCFG, additional_containers: [] };
  for (const key of ["placement", "node", "failover"]) delete joined[key];
  return joined;
}
/* The summary's line for Placement: its host, how copies spread, and its rules. */
function deployPlacementWords(c) {
  const rules = (c.placement?.with?.length || 0) + (c.placement?.apart?.length || 0);
  return [c.node ? `prefers ${c.node}` : "any host", c.placement?.spread ? `${c.placement.spread} different hosts` : "",
    rules ? `${rules} rule${rules === 1 ? "" : "s"} for other apps` : ""].filter(Boolean).join(" · ");
}
function syncSummary() {
  const c = collect();
  // The hidden attribute, as deployLanChanged sets it: a display style
  // cannot show what [hidden] hides with !important.
  const vipWrap = $("#d_vip_wrap"); if (vipWrap) vipWrap.hidden = !(c.network_mode === "loadbalancer" && c.vip_mode === "manual");
  const imageNote = $("#d_image_note"); if (imageNote) imageNote.innerHTML = imagePullNote(c.image);
  // The block follows the form: it lifts when the socket row goes (#1294).
  const blocked = $("#d_blocked"); if (blocked) blocked.innerHTML = deployBlockedHtml(c);
  const review = $("#d_review"); if (review) review.disabled = !!deployRuntimeSocket(c);
  if (c.target_mode !== "existing") appFormPodContainers("d", [c.container_name, ...(c.additional_containers || []).map(item => item.name)]);
  const row = (i, l, v) => `<div class="drow"><div class="di">${i}</div><div class="dl">${l}</div><div class="dv">${v}</div></div>`;
  $("#d_summary").innerHTML =
    (c.target_mode === "existing" ? "" : row("◈", "App / pod", c.workload_name ? `<b>${esc(c.workload_name)}</b>` : '<span class="dim">—</span>')) +
    (c.additional_containers?.length ? row("▣", "Containers in each pod", [c.container_name, ...c.additional_containers.map(item => item.name)].map(esc).join(", ")) : "") +
    row("▣", "Container", c.container_name ? `<b>${esc(c.container_name)}</b>` : '<span class="dim">—</span>') +
    row("❏", "Image", c.image ? `<span class="small mono">${esc(c.image)}</span>` : '<span class="dim">—</span>') +
    row("⌗", "Namespace", esc(c.namespace)) + row("⧉", c.target_mode === "existing" ? "Joins app" : "Pod copies", c.target_mode === "existing" ? esc(c.target_workload || "—") : c.replicas) +
    (c.target_mode === "existing" ? "" : row("⌖", "Placement", `<span class="small">${esc(deployPlacementWords(c))}</span>`)) +
    row("◴", "Requests", `<span class="small mono">${esc(c.cpu)} · ${esc(c.memory)}</span>`) +
    row("▣", "Memory max", c.memory_limit ? `<span class="small mono">${esc(c.memory_limit)}</span>` : '<span class="dim">no limit</span>') +
    ((c.command || []).length || (c.args || []).length ? row("›", "Runs", `<span class="small mono">${esc([...(c.command || []), ...(c.args || [])].join(" "))}</span>`) : "") +
    row("▤", "Hardware", c.hardware.length ? hardwareTags(c.hardware) : '<span class="dim">none</span>') +
    row("◎", "Network", `<span class="small">${esc(c.network_mode)}${c.network_mode === "loadbalancer" ? ` · ${esc(c.vip_mode)} VIP` : ""}</span>`) +
    row("⇄", "Ports", c.ports.length ? c.ports.map(p => UI.chip(`${p.host}→${p.container}`, p.expose ? "info" : "neutral")).join(" ") : '<span class="dim">—</span>') +
    row("▥", "Storage", c.volumes.length ? c.volumes.map(v => UI.chip(`${v.source || (v.kind === "ephemeral" ? "temporary" : "choose source")} · ${v.kind}`, "neutral")).join(" ") : '<span class="dim">—</span>') +
    row("≡", "Env vars", Object.keys(c.env).length ? UI.chip(`${Object.keys(c.env).length} set`, "neutral") : '<span class="dim">—</span>');
}
function addPort(cp = "", hp = "", ex = true, protocol = "TCP") {
  const d = document.createElement("div"); d.className = "f4 port-row";
  d.innerHTML = `<div><label>Container port</label><input class="pc" type="number" value="${cp}"></div>
    <div><label>LAN port</label><input class="ph" type="number" value="${hp}"></div>
    <div><label>Protocol</label><select class="pp"><option ${protocol === "TCP" ? "selected" : ""}>TCP</option><option ${protocol === "UDP" ? "selected" : ""}>UDP</option></select></div>
    <label class="switch" style="margin:0 0 10px"><input class="pe" type="checkbox" ${ex ? "checked" : ""}>Expose</label>
    <button class="iconbtn row-remove" type="button" title="Remove port" onclick="this.parentElement.remove();syncSummary()">×</button>`;
  $("#d_ports").appendChild(d); d.addEventListener("input", syncSummary); if (!DRENDERING) syncSummary();
}
function deployVolumePicker() {
  return createVolumePicker($("#d_vols"), {
    pvcs: () => DOPT.pvcs,
    storageClasses: () => DOPT.storage_classes,
    sharedStorageClasses: () => DOPT.shared_storage_classes || DOPT.storage_classes,
    classFacts: () => DOPT.storage_class_facts || {},
    podVolumes: () => selectedTarget()?.volumes || [],
    allowPod: () => $("#d_target_mode")?.value === "existing",
    podLabel: "Existing volume in selected pod",
    podEmpty: "Choose a volume from the selected app…",
    podUnavailable: "No reusable volumes in the selected app",
    podHelp: "Mounts a volume already defined on the selected app into this sidecar.",
    onChange: () => { if (!DRENDERING) syncSummary(); },
  });
}
function addVol(path = "", src = "", type = "pvc", meta = {}) {
  const v = typeof type === "object" ? type : { ...meta, path, source: src, type };
  addVolumeRow(deployVolumePicker(), v);
  if (!DRENDERING) syncSummary();
}
function addEnv(k = "", v = "", meta = {}) {
  const d = document.createElement("div"); d.className = "f3 env-row";
  const editor = meta.options?.length
    ? `<select class="ev">${meta.options.map(option => `<option ${option === v ? "selected" : ""}>${esc(option)}</option>`).join("")}</select>`
    : `<input class="ev" type="${meta.masked ? "password" : "text"}" value="${esc(v)}" ${meta.binding ? `readonly placeholder="Assigned ${esc(meta.binding)} at deploy time"` : ""}>`;
  d.innerHTML = `<div><label>Key</label><input class="ek" type="text" value="${esc(k)}"></div>
    <div><label title="${esc(`${meta.label || "Value"}${meta.required ? " · required" : ""}${meta.generate ? " · generated securely" : ""}${meta.binding ? ` · bound to ${meta.binding}` : ""}`)}">${esc(meta.label || "Value")}${meta.required ? " · required" : ""}${meta.generate ? " · generated securely" : ""}${meta.binding ? ` · bound to ${esc(meta.binding)}` : ""}</label>${editor}${meta.description ? `<span class="dim xs">${esc(meta.description)}</span>` : ""}</div>
    <button class="iconbtn row-remove" type="button" title="Remove variable" onclick="this.parentElement.remove();syncSummary()">×</button>`;
  $("#d_env").appendChild(d); d.addEventListener("input", syncSummary); if (!DRENDERING) syncSummary();
}
function renderPorts() { const before = DRENDERING; DRENDERING = true; const rows = [...(DCFG.ports || [])]; $("#d_ports").innerHTML = ""; rows.forEach(p => addPort(p.container, p.host, p.expose !== false, p.protocol || "TCP")); DRENDERING = before; }
function renderVols() { const before = DRENDERING; DRENDERING = true; renderVolumeRows(deployVolumePicker(), DCFG.volumes || []); DRENDERING = before; }
function renderEnv() { const before = DRENDERING; DRENDERING = true; const rows = Object.entries(DCFG.env || {}); $("#d_env").innerHTML = ""; rows.forEach(([k, v]) => addEnv(k, v, (DCFG.env_meta || []).find(m => m.key === k) || {})); DRENDERING = before; }
window.addPort = addPort; window.addVol = addVol; window.addEnv = addEnv;
/* The capacity review's notes, split: what needs someone, and the caveats
   every new VM or pod has - its disks not made yet, its launcher's memory
   not limited, the estimate a lower bound. Those are true of every review, so
   they sit under "How this is estimated" instead of a warning each time:
   a standard Windows 11 VM listed a dozen of them as needing review. */
const CAPACITY_ROUTINE = [
  /is planned, not provisioned; storage capacity and attachment remain unverified$/,
  /^VM disk \S+ is planned; provisioning/, /^disk import \S+ is planned; provisioning/,
  /^RAM projection includes /, /^VM launcher memory is not explicitly limited$/,
  /^memory is not limited for vm-launcher-estimate$/, /^unrecognised\/injected helpers, admission defaults/,
  /^Image import\/provisioning may start before the guest/, /^If a later step fails, created images/,
];
const CAPACITY_ROLLOUT_ROUTINE = [
  /^updated pod estimates include every container, not only the added container$/,
  /^post-stop capacity assumes old pods have fully terminated and released ports and volumes; termination and storage detach are not guaranteed$/,
  /^live RAM still includes old pods; it is not subtracted from the conservative projection$/,
  /^RollingUpdate permits \d+ extra pod\(s\) and \d+ unavailable replica\(s\); terminating pods can extend the overlap$/,
  /^intermediate rolling-update placement and readiness are not fully simulated; a fitting first pod is not a completion guarantee$/,
];
function capacityNoteWords(text) {
  if (text.startsWith("memory is not limited for ")) return `No memory limit is set for: ${text.slice("memory is not limited for ".length)}. Memory use can exceed this estimate.`;
  if (text.startsWith("updated pod estimates include every container")) return "The estimate covers all containers in the updated pod, including initialization.";
  if (text.startsWith("post-stop capacity assumes")) return "Capacity becomes available after old pods stop and release their ports and volumes. Storage detach may take longer.";
  if (text.startsWith("live RAM still includes old pods")) return "Live memory usage still includes the old pods, so the estimate keeps that usage until they stop.";
  if (text.startsWith("intermediate rolling-update placement")) return "The check covers starting the rollout. Later placement and readiness can still delay completion.";
  const rolling = text.match(/^RollingUpdate permits (\d+) extra pod\(s\) and (\d+) unavailable replica\(s\);/);
  if (rolling) return `The rollout allows ${plural(rolling[1], "extra pod")} and ${plural(rolling[2], "unavailable replica")}. Pods still stopping can temporarily increase the total.`;
  return text;
}
function capacityNotes(plan) {
  const warnings = plan?.warnings || [];
  const planned = new Set(warnings.map(w => (w.match(/^PVC (\S+) is planned, not provisioned/) || [])[1]).filter(Boolean));
  const creating = plan?.vm?.action === "create";
  const routine = w => CAPACITY_ROUTINE.some(re => re.test(w))
    || (plan?.rollout && CAPACITY_ROLLOUT_ROUTINE.some(re => re.test(w)))
    // A claim this review makes is unbound until it is made.
    || planned.has((w.match(/^PVC (\S+) is not bound; provisioning and topology need review$/) || [])[1])
    // A new VM has no TPM or EFI state to keep.
    || (creating && /^No persisted TPM\/EFI\/CBT state was found/.test(w));
  return { concerns: warnings.filter(w => !routine(w)).map(capacityNoteWords), caveats: warnings.filter(routine).map(capacityNoteWords) };
}
window.capacityNotes = capacityNotes;

/* Where it can run and what it needs. notice: false leaves its warnings to
   the dialog's one notice (deployCapacityIssues) instead of a box here. */
function deployCapacityHtml(plan, overlap = false, imageChange = false, { notice = true } = {}) {
  if (!plan) return "";
  const { concerns: needs, caveats } = capacityNotes(plan);
  const title = overlap ? "New pods alongside current pods" : plan.rollout ? "Updated pod: capacity after old pods stop" : "Placement and memory";
  const blocked = overlap ? "This overlap does not fit while old pods remain. Progress may depend on old-pod removal within the rollout policy."
    : plan.rollout?.start_blocked ? "The rollout cannot start within its current availability policy. Review the overlap blockers below."
    : "This deployment cannot fit the checked placement constraints. Change its resources, volumes or host selection before deploying.";
  const shown = needs;
  const concerns = shown.length ? `<ul class="ui-list">${shown.map(w => `<li>${esc(w)}</li>`).join("")}</ul>` : "";
  const rollout = plan.rollout ? `<p>${esc(plan.rollout.strategy)} · ${esc(plural(plan.rollout.replicas, "desired replica"))} · ${plan.rollout.ownership_known
      ? `${esc(plural(plan.rollout.owned_pods.length, "existing pod"))} identified by controller ownership; ${esc(plan.rollout.release_request_gb)} GiB of requests would be released only after termination.`
      : "Pod ownership is unverified; released capacity is unknown."}</p>
    ${plan.rollout.strategy === "RollingUpdate" ? `<p>Up to ${esc(plural(plan.rollout.max_surge, "extra pod"))}, ${esc(plural(plan.rollout.max_unavailable, "unavailable replica"))}. Intermediate rollout steps remain unverified.</p>` : ""}` : "";
  const requests = `${esc(plural(plan.additional, plan.vm ? "VM launcher" : "pod"))}, ${plan.vm?.request_is_lower_bound ? "requesting at least" : "each requesting"} ${esc(plan.pod_request_gb)} GiB RAM; ${plan.vm?.cpu_request_is_estimate ? "conservative CPU allowance" : "CPU request"}: ${esc(plan.pod_cpu_request_percent)}% (100% = one core). Memory estimate: ${esc(plan.pod_memory_gb)} GiB ${plan.vm ? "for the VM and launcher" : "per pod, including init stages"}.`;
  return `<div class="deploy-capacity ui-stack">
    ${overlap ? "" : capacityPlacementHtml(plan)}
    ${UI.section(title, [
      !notice ? "" : plan.blocked ? UI.callout("bad", blocked, `${concerns}${overlap ? "" : "<p>This is a placement blocker, not just a capacity warning. Resolve the listed scheduler, hardware or storage constraints before proceeding.</p>"}`)
        : concerns ? UI.callout("warn", "Placement and memory need review.", concerns) : "",
      plan.pod_request_gb !== undefined && plan.pod_request_gb !== null ? `<p class="ui-help">${requests}</p>` : "",
      capacityHosts(plan.candidates || [], { unavailable: "Live RAM unavailable",
        reserved: plan.rollout ? "Reserved RAM after planned termination" : "Reserved RAM", placement: plan.placement }),
    ].join(""))}
    ${UI.more("How this is estimated", `${rollout}
      ${caveats.length ? `<p>Estimate assumptions:</p><ul class="ui-list">${caveats.map(w => `<li>${esc(w)}</li>`).join("")}</ul>` : ""}
      ${!overlap && !plan.blocked ? "<p>Capacity warnings can be overridden, including high projected RAM and missing usage metrics. Proceeding may cause memory pressure, OOM restarts or downtime; it does not change resource requests or limits.</p>" : ""}
      <p>This is a snapshot, not a reservation or an OOM guarantee. ${plan.vm ? "The server checks again before sending the VM action. Guest readiness and successful rescheduling are not guaranteed." : imageChange ? "The server checks again before changing the app or its recovery metadata." : "The server checks again before creating anything. Planned volumes have not been provisioned."}</p>`, !!plan.blocked)}
    ${plan.rollout?.overlap ? UI.more(`Overlap while old pods remain${plan.rollout.start_blocked ? " - rollout cannot start" : ""}`, deployCapacityHtml(plan.rollout.overlap, true), !!plan.rollout.start_blocked) : ""}
  </div>`;
}
/* The capacity warnings, or the blocker and what blocks, as the dialog's notice. */
function deployCapacityIssues(plan) {
  if (!plan) return { issues: [], title: "" };
  const needs = capacityNotes(plan).concerns;
  if (plan.blocked) return { title: plan.rollout?.start_blocked ? "The rollout cannot start" : "It cannot fit",
    issues: [["bad", { short: plan.rollout?.start_blocked ? "No replacement pod fits within the current availability policy." : "It cannot fit the checked placement constraints.",
      more: "This is a placement blocker, not just a capacity warning: change its resources, volumes or host selection." }], ...needs.map(w => ["bad", w])] };
  return { title: "", issues: needs.map(w => ["warn", w]) };
}
window.deployReviewReady = () => DEPLOY.ready();
function deployInvalid(message, selector) {
  const field=selector?document.querySelector(selector):null, pane=field?.closest('.stepper-pane');
  if(pane)UI.selectSection("containerDeploy",pane.dataset.key);
  for(let parent=field?.parentElement;parent && parent!==pane;parent=parent.parentElement)if(parent.tagName==="DETAILS")parent.open=true;
  field?.focus();toast(message,"bad");
}
/* Review, then deploy (#909), on the shared review flow: it keeps the sequence, the busy flag and the
   one-shot send. This view keeps its words, and the two ticks (joining a pod; deploying despite warnings). */
const DEPLOY = UI.reviewFlow({
  preview: async c => {
    const plan = await api("/api/preview", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(c) });
    if (plan.app_profile?.blocked) throw new Error(plan.app_profile.label || "this app needs a Kubernetes-specific design");
    if (!plan.capacity) throw new Error("Capacity preview unavailable; refresh Homestead before deploying.");
    return { config: JSON.parse(JSON.stringify(c)), plan: plan.capacity, token: plan.capacity_token, preview: plan };
  },
  render: (review, c) => {
    const joining = c.target_mode === "existing", plan = review.preview;
    const notice = deployCapacityIssues(plan.capacity);
    const restart = plan.capacity.rollout?.paused ? "Save the paused template; resuming is reviewed again"
      : plan.capacity.rollout?.replicas === 0 ? "Change the stopped app's template without starting it" : `Restart every container in ${c.target_workload} as its pods roll out`;
    modal(joining ? "Review shared-pod change" : "Review deployment", `<div class="update-review">${UI.dialog({ shape: "review",
      subject: joining ? { kind: "App", name: c.target_workload, where: c.namespace } : { kind: "New app", name: c.workload_name, where: c.namespace },
      lead: `${joining ? `Adds ${esc(c.container_name)} to it.` : `Creates it with container ${esc(c.container_name)}.`} ${esc(plan.impact?.message || "Review the Kubernetes objects before continuing.")}`,
      issues: notice.issues, issuesTitle: notice.title,
      body: `${plan.app_profile?.notes?.length ? `<div class="app-profile ${esc(plan.app_profile.level || "review")}">${plan.app_profile.notes.map(note => `<div class="profile-note">✓ ${esc(note)}</div>`).join("")}</div>` : ""}
        ${UI.facts([["Image", `<span class="mono">${esc(c.image)}</span>`], ["Ports", esc(c.ports.length)], ["Storage mappings", esc(c.volumes.length)]])}
        ${deployCapacityHtml(plan.capacity, false, false, { notice: false })}
        ${joining ? UI.ack("deployConfirm", restart, { onchange: "deployReviewReady()" }) : ""}
        ${gitopsYamlBlock(plan.yaml, plan.secret, { scope: joining ? "added" : "app", file: plan.yaml_file, adds: plan.deploy_adds })}`,
      ack: plan.capacity?.requires_confirmation ? { id: "deployCapacityConfirm", sentence: "Deploy it despite the warnings", onchange: "deployReviewReady()" } : null,
      actions: { dismiss: { label: "Cancel", onclick: "closeModal()" },
        primary: { label: joining ? "Add container & restart pod" : "Deploy app", onclick: "confirmDeploy()", id: "deployGo",
          disabled: !!(joining || plan.capacity?.requires_confirmation || plan.capacity?.blocked) } } })}</div>`, true);
  },
  failed: e => toast(e.message, "bad"),
  blocked: review => !!review.plan?.blocked, button: "deployGo",
  check: review => (review.config.target_mode !== "existing" || !!$("#deployConfirm")?.checked) &&
    (!review.plan?.requires_confirmation || !!$("#deployCapacityConfirm")?.checked),
  apply: async review => {
    const c = { ...review.config, capacity_token: review.token,
      confirm_capacity: !!$("#deployCapacityConfirm")?.checked };
    try { $("#deployGo").disabled = true; const r = await api("/api/deploy", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(c) });
      const kept = (r.reused_volumes || []).length ? ` - kept the existing ${r.reused_volumes.join(", ")}, which nothing was using` : "";
      closeModal(); toast((c.target_mode === "existing" ? `${c.container_name} added to ${c.target_workload}` : `${c.workload_name} deployed`) + kept, "ok"); go("workloads");
    } catch (e) {
      const button = $("#deployGo");
      if (button) { button.disabled = false; button.textContent = "Review again"; button.onclick = () => window.doDeploy(); }
      toast(e.message, "bad");
    }
  },
});
// A second click while the logo is still being checked replaces the first.
let DEPLOY_CLICKS = 0;
window.doDeploy = async () => {
  if (DEPLOY.busy) return;
  const click = ++DEPLOY_CLICKS;
  DEPLOY.back();
  try { await logoReady("#d_icon"); } catch (e) { return deployInvalid(e.message, "#d_icon"); }
  if (click !== DEPLOY_CLICKS) return;
  const c = collect();
  if (deployRuntimeSocket(c)) { UI.selectSection("containerDeploy", "summary"); return toast("Remove the container runtime socket under Storage first", "bad"); }
  if (!c.container_name || !c.image) return deployInvalid("Container name and image are required.", !c.container_name?"#d_container_name":"#d_image");
  if (c.target_mode === "new" && !c.workload_name) return deployInvalid("App name is required.", "#d_workload_name");
  if (c.target_mode === "existing" && !c.target_workload) return deployInvalid("Choose an existing app.", "#d_target_workload");
  const extra = c.additional_containers || [];
  // Another container's field, by its place in the order collect() read them.
  const extraField = (at, field) => {
    const index = $$("#d_basics_containers .edit-container[data-original-name]")[at]?.dataset.index;
    return index === undefined ? null : `#e_${field}_${index}`;
  };
  const unnamed = extra.findIndex(container => !container.name || !container.image);
  if (unnamed >= 0) return deployInvalid("Every container needs a name and image.", extraField(unnamed, extra[unnamed].name ? "image" : "container_name"));
  if (new Set([c.container_name, ...extra.map(container => container.name)]).size !== extra.length + 1) return deployInvalid("Container names must be unique in this pod.", "#d_container_name");
  const storageIssue = [volumeListIssue(c.volumes), ...extra.map(container => volumeListIssue(container.volumes))].find(Boolean);
  if (storageIssue) {
    const vols = volumeListIssue(c.volumes) ? "#d_vols" : extraField(extra.findIndex(container => volumeListIssue(container.volumes)), "vols");
    return deployInvalid(storageIssue, vols && `${vols} input`);
  }
  await DEPLOY.open(c);
};
window.confirmDeploy = async () => {
  if (!window.deployReviewReady()) return toast("Review the deployment and acknowledge its warnings first", "bad");
  await DEPLOY.apply();
};

// Node only (tests): the same names, for require(). The browser has no `module`.
if (typeof module === "object" && module.exports) module.exports = {
  DEPLOY_CONTAINER_NEXT, deployContainerStorage, renderDeployContainers, deployDefaults, DCFG, RUNTIME_SOCKETS, deployRuntimeSocket, deployBlockedHtml, generatedSecret,
  deployPlacementApp, viewDeploy, selectedTarget, renderDeployTargets, updateJoinNote, refreshDeployOptions,
  applyDeployMode, collect, deployPlacementWords, syncSummary, addPort, deployVolumePicker, addVol, addEnv,
  renderPorts, renderVols, renderEnv, CAPACITY_ROUTINE, CAPACITY_ROLLOUT_ROUTINE, capacityNoteWords,
  capacityNotes, deployCapacityHtml, deployCapacityIssues, deployInvalid, DEPLOY, DEPLOY_CLICKS
};
