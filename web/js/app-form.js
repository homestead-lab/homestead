/* The app form that Deploy and Edit share (#544): one list of sections in one
   order, and one block that lists the containers in each pod. Each view still
   draws its own fields; this file says where they go. */

/* Every section, in order. Deploy adds its Summary after these. */
const APP_FORM_SECTIONS = Object.freeze([
  { key: "basics", title: "Basics" },
  { key: "hardware", title: "Hardware and access" },
  { key: "environment", title: "Environment values" },
  { key: "storage", title: "Storage" },
  { key: "placement", title: "Placement" },
  { key: "address", title: "Address" },
  { key: "monitoring", title: "Monitoring" },
].map(section => Object.freeze(section)));

/* The sections that show each container's part of their topic, one panel per container. */
const APP_FORM_CONTAINER_SECTIONS = Object.freeze(["basics", "hardware", "environment", "storage", "address"]);

/* The sections for UI.sectionForm, from the HTML the view drew for each key.
   later: { key: "when it can be set" } greys a section the form cannot set yet. */
function appFormSections(mode, html, { leads = {}, later = {} } = {}) {
  const keys = [...APP_FORM_SECTIONS, ...(mode === "deploy" ? [{ key: "summary", title: "Summary" }] : [])];
  return keys.map(({ key, title }) => {
    if (typeof html[key] !== "string") throw new Error(`app form: no html for ${key}`);
    return { key, title, html: html[key], ...(leads[key] ? { lead: leads[key] } : {}), ...(later[key] ? { later: later[key] } : {}) };
  });
}

/* Basics' list of containers: the add button, how they share a pod, their
   panels and the ones removed on save. prefix is "e" (Edit) or "d" (Deploy). */
function appFormContainerBlock(prefix, mode, panelsHtml) {
  return `<div class="app-form-containers" id="${prefix}_containers_block">
    <div class="between"><div class="sec">Containers in each pod</div>
      <button class="btn" type="button" onclick="containerAdd(${jsq(mode)})">＋ Add container</button></div>
    ${UI.more("How containers share a pod", `<p>These containers share networking and volumes. Set pod copies under Placement.</p>`)}
    <div id="${prefix}_basics_containers">${panelsHtml}</div>
    <div id="${prefix}_removed_containers" class="dim small"></div></div>`;
}

/* A container's panel in every section that holds one. */
function appFormAddPanels(prefix, container, index, mode) {
  for (const section of APP_FORM_CONTAINER_SECTIONS)
    document.getElementById(`${prefix}_${section}_containers`).insertAdjacentHTML("beforeend", editContainerPanel(container, index, section, mode));
}

/* ---------------- Placement (#844) ----------------
   The same section in Edit ("e") and Deploy ("d"): the containers in one pod
   (always together - that is what a pod is), the copies of this app, other
   apps, what happens if its host fails, and its tier. Keep this workload's
   copies on different hosts, or run it with or apart from other apps; each
   rule is a preference or a requirement. */
const APP_FORM_PLACEMENT = { e: { others: [], nodes: 0 }, d: { others: [], nodes: 0 } };
/* What a form's placement reads: its namespace and name, the other apps, how
   many hosts can take pods, and its volume rows and claims for the RWO note. */
function appFormPlacementContext(prefix, context) {
  APP_FORM_PLACEMENT[prefix] = { others: [], nodes: 0, volumes: () => [], pvcs: () => [], ...context };
  return APP_FORM_PLACEMENT[prefix];
}
const PLACEMENT_MODES = [["prefer", "prefer"], ["require", "require"]];
function placementRow(prefix, kind, row = {}) {
  const context = APP_FORM_PLACEMENT[prefix] || {}, others = context.others || [];
  const key = row.name ? `${row.ns || context.ns}/${row.name}` : "";
  const known = !key || others.some(w => `${w.ns}/${w.name}` === key);
  return `<div class="aff-row" data-kind="${kind}">
    <select class="aff-target"><option value="">choose an app…</option>${known ? "" : `<option value="${esc(key)}" selected>${esc(key)} (gone)</option>`}
      ${others.map(w => `<option value="${esc(w.ns)}/${esc(w.name)}" ${`${w.ns}/${w.name}` === key ? "selected" : ""}>${esc(w.name)}${w.ns !== context.ns ? ` · ${esc(w.ns)}` : ""}</option>`).join("")}</select>
    <select class="aff-mode">${PLACEMENT_MODES.map(([value, label]) => `<option value="${value}" ${row.mode === value ? "selected" : ""}>${label}</option>`).join("")}</select>
    <button class="iconbtn row-remove" type="button" title="Remove this rule" onclick="this.closest('.aff-row').remove();placementChanged(${jsq(prefix)})">×</button></div>`;
}
/* The section. p: the rules ({ spread, with, apart }); w: the app's host,
   copies, failover and tier as Edit reads them. minCopies: 0 lets Deploy
   create an app stopped, as it always could. */
function appFormPlacement(prefix, p, w, nodes, containers, { startsHtml = "", minCopies = 1 } = {}) {
  const id = name => `${prefix}_${name}`, names = (containers || []).map(c => c.name);
  return `<section class="placement card flat" id="${id("placement")}" data-prefix="${prefix}">
    <div class="sec" style="margin-top:0">Placement</div>
    <div class="place-level"><div class="place-title">Containers in this pod</div>
      <div class="dim small" id="${id("pod_containers")}">${names.length > 1 ? `${names.map(n => UI.chip(n, "neutral")).join(" ")} always run together on one node, sharing its network and any pod volumes.`
        : `${UI.chip(names[0] || w.name || "This app", "neutral")} is this pod's only container.`}
        Containers in one pod cannot be spread apart; to run one on its own host, make it an app of its own and use the rules below.</div></div>
    <div class="place-level"><div class="place-title">Copies of this app</div>
      <div class="place-grid">
        <div class="f"><label>Copies ${tip("How many copies of this app run at once - Kubernetes calls them replicas. Most homelab apps want one; Longhorn volume replicas are a separate, storage-level idea.")}</label><input type="number" id="${id("rep")}" value="${w.start_replicas ?? w.replicas ?? 1}" min="${minCopies}" max="5" ${w.autostart === false ? "disabled" : ""}></div>
        <div class="f"><label>Spread copies ${tip("Puts this app's copies on different hosts, so losing one host does not take every copy. Only matters with more than one copy.")}</label>
          <select id="${id("spread")}" onchange="placementChanged(${jsq(prefix)})"><option value="">no preference</option>
            <option value="prefer" ${p.spread === "prefer" ? "selected" : ""}>prefer different hosts</option>
            <option value="require" ${p.spread === "require" ? "selected" : ""}>require different hosts</option></select></div>
        <div class="f"><label>Preferred host ${tip("A preference guides placement but still allows failover. Use Move for hardware-aware choices and optional hard pinning.")}</label><select id="${id("node")}" data-current="${esc(w.node || "")}">
          <option value="">any host</option>
          ${(nodes || []).map(n => `<option value="${esc(n.name)}" ${n.name === w.node ? "selected" : ""}>${esc(n.name)}${n.schedulable === false ? " (not schedulable)" : ""}</option>`).join("")}
          ${w.node && !(nodes || []).some(n => n.name === w.node) ? `<option value="${esc(w.node)}" selected>${esc(w.node)} (gone)</option>` : ""}
        </select></div></div></div>
    <div class="place-level"><div class="place-title">Other apps</div>
      <div class="place-grid two">
        <div class="f"><label>Run on the same host as ${tip("For apps that talk constantly or share a device. Required: this does not start until that app is running, and only on its host.")}</label>
          <div id="${id("with")}">${(p.with || []).map(row => placementRow(prefix, "with", row)).join("")}</div>
          <button class="btn sm" type="button" onclick="placementAdd(${jsq(prefix)},'with')">＋ Add</button></div>
        <div class="f"><label>Keep off the host of ${tip("For pairs that should never share a host: two DNS servers, or two apps that would compete for one disk.")}</label>
          <div id="${id("apart")}">${(p.apart || []).map(row => placementRow(prefix, "apart", row)).join("")}</div>
          <button class="btn sm" type="button" onclick="placementAdd(${jsq(prefix)},'apart')">＋ Add</button></div></div></div>
    ${startsHtml}
    <div class="place-level"><div class="place-title">If its host fails</div>
      <div class="place-grid failover"><div class="f">${failoverSelect(id("failover"), w.failover || "default",
          `onchange="$(${jsq("#" + id("failover_help"))}).textContent = FAILOVER_HELP[this.value] || ''"`)}</div>
        <div class="dim small" id="${id("failover_help")}">${esc(FAILOVER_HELP[w.failover || "default"])}</div></div></div>
    <div class="place-level"><div class="place-title">When the cluster is short of room</div>
      <div class="place-grid failover"><div class="f">${priorityFixed(w)
          ? `<span class="mono">${esc(w.priority_class || "no class")}</span>`
          : prioritySelect(id("priority"), w.priority || "normal", `aria-label="Tier when the cluster is short of room" aria-describedby="${id("priority_help")}" data-group="${esc(w.priority_group ? priorityGroupHelp(w.priority_group) : "")}" onchange="$(${jsq("#" + id("priority_help"))}).textContent = PRIORITY_HELP[this.value] || this.dataset.group || ''"`,
              w.priority_group ? `<option value="group" ${w.priority === "group" ? "selected" : ""}>${esc(priorityGroupLabel(w.priority_group, w.priority_group_tier))}</option>` : "")}</div>
        <div class="dim small" id="${id("priority_help")}" aria-live="polite">${esc(priorityFixed(w) || (w.priority === "group" ? priorityGroupHelp(w.priority_group) : PRIORITY_HELP[w.priority || "normal"]))}</div></div></div>
    <div class="note" id="${id("place_note")}" hidden></div></section>`;
}
window.placementAdd = (prefix, kind) => { $(`#${prefix}_${kind}`).insertAdjacentHTML("beforeend", placementRow(prefix, kind)); placementChanged(prefix); };
/* The rules as chosen: { spread, with: [{ ns, name, mode }], apart: [...] }. */
function readPlacement(prefix = "e") {
  const rows = kind => $$(`#${prefix}_${kind} .aff-row`).map(row => {
    const [ns, name] = ($(".aff-target", row).value || "/").split("/");
    return { ns, name, mode: $(".aff-mode", row).value };
  }).filter(row => row.name);
  return { spread: $(`#${prefix}_spread`)?.value || "", with: rows("with"), apart: rows("apart") };
}
window.placementChanged = (prefix = "e") => {
  const note = $(`#${prefix}_place_note`);
  if (!note) return;
  const context = APP_FORM_PLACEMENT[prefix] || {};
  const p = readPlacement(prefix), replicas = +$(`#${prefix}_rep`)?.value || 1, warnings = [];
  if (p.spread === "require" && replicas > context.nodes)
    warnings.push(`${replicas} instances but ${context.nodes} schedulable host${context.nodes === 1 ? "" : "s"}: the extra instances stay pending.`);
  const claims = context.pvcs?.() || [];
  const singleNode = (context.volumes?.() || []).some(v => v.kind === "new-rwo" || (v.kind === "existing" &&
    (claims.find(c => c.name === v.source)?.access_modes || []).includes("ReadWriteOnce")));
  if (p.spread && replicas > 1 && singleNode)
    warnings.push("A single-node (RWO) volume attaches to one node at a time, so instances spread across hosts cannot all mount it.");
  const names = [...p.with, ...p.apart].map(row => `${row.ns}/${row.name}`);
  if (names.length !== new Set(names).size) warnings.push("An app is named twice; each can be kept either with this one or apart from it.");
  if (p.with.some(row => row.mode === "require")) warnings.push("Required: this app waits for the one it runs with, and stops being schedulable if that one stops.");
  note.hidden = !warnings.length;
  note.innerHTML = warnings.map(esc).join("<br>");
};
document.addEventListener("change", event => {
  const prefix = event.target.closest?.(".placement")?.dataset.prefix || (/^([ed])_rep$/.exec(event.target.id || "") || [])[1];
  if (prefix) placementChanged(prefix);
});
/* Placement's line naming the containers in each pod, as the list changes. */
function appFormPodContainers(prefix, names) {
  const host = $(`#${prefix}_pod_containers`);
  if (host) host.innerHTML = `${names.map(name => UI.chip(name || "unnamed", "neutral")).join(" ")} run together in every pod, sharing its network and volumes.`;
}
