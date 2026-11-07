/* Homestead Containers: groups, group tiers, platform containers and Move several (#911). One of the scripts views-workloads.js was split into; they share the page's globals and load in the order index.html lists them. */
/* ---------------- groups ----------------
   A group is a word on each workload (an annotation), so it exists while
   something is in it. The page shows each under a divider that folds, with a
   chip per group to show just that one. */
const NO_GROUP = "(none)";
function workloadGroupPrefs() {
  let pick = "", folded = [];
  try {
    pick = localStorage.getItem("homestead.containers.group") || "";
    folded = JSON.parse(localStorage.getItem("homestead.containers.folded") || "[]");
  } catch (e) { /* defaults */ }
  return { pick, folded: new Set(Array.isArray(folded) ? folded : []) };
}
function workloadGroupNames(rows) {
  return [...new Set(rows.map(w => w.group).filter(Boolean))].sort((a, b) => a.localeCompare(b, undefined, { sensitivity: "base" }));
}
/* Group names are the user's own words, so handlers get them by index. */
let WL_GROUP_KEYS = [];
function groupKeyIndex(key) {
  let at = WL_GROUP_KEYS.indexOf(key);
  if (at < 0) { WL_GROUP_KEYS.push(key); at = WL_GROUP_KEYS.length - 1; }
  return at;
}
window.pickWorkloadGroup = index => {
  try { localStorage.setItem("homestead.containers.group", WL_GROUP_KEYS[index] || ""); } catch (e) { /* this visit only */ }
  renderWorkloads();
};
window.foldWorkloadGroup = index => {
  const key = WL_GROUP_KEYS[index];
  if (key === undefined) return;
  const { folded } = workloadGroupPrefs();
  folded.has(key) ? folded.delete(key) : folded.add(key);
  try { localStorage.setItem("homestead.containers.folded", JSON.stringify([...folded])); } catch (e) { /* this visit only */ }
  renderWorkloads();
};
function workloadGroupHead(group, rows, folded) {
  const running = rows.filter(w => w.desired > 0 && w.ready === w.desired).length;
  const head = `<button type="button" class="wgroup-head" aria-expanded="${!folded}" onclick="foldWorkloadGroup(${groupKeyIndex(group || NO_GROUP)})">
    <span class="wgroup-chevron">›</span><b>${esc(group || "Ungrouped")}</b>
    <span class="dim xs">${rows.length} app${rows.length === 1 ? "" : "s"}${running < rows.length ? ` · ${running} running` : ""}</span></button>`;
  // Homestead's own group looks after its own tier (#429); Ungrouped isn't a group.
  if (!group || rows.every(w => w.homestead || w.platform)) return `<div class="wgroup-headrow">${head}</div>`;
  const tier = rows.find(w => w.group_tier)?.group_tier || "";
  return `<div class="wgroup-headrow">${head}${tier ? UI.chip(tier) : ""}${actionBar([{ label: "Tier…", run: `groupTier(${groupKeyIndex(group)})`, need: "operator" }], { shown: 1, label: `More for ${group}` })}</div>`;
}
function workloadSections(rows, layout, pick) {
  const { folded } = workloadGroupPrefs();
  const names = workloadGroupNames(rows);
  if (!names.length || pick) return layout === "rows" ? workloadTable(rows) : `<div class="cardlist">${rows.map(workloadCard).join("")}</div>`;
  const sections = names.map(name => [name, rows.filter(w => w.group === name)]);
  const loose = rows.filter(w => !w.group);
  if (loose.length) sections.push(["", loose]);
  if (layout === "rows") return workloadTable(rows, sections, folded);
  return sections.map(([name, members]) => {
    const shut = folded.has(name || NO_GROUP);
    return `<section class="wgroup${shut ? " folded" : ""}">${workloadGroupHead(name, members, shut)}
      ${shut ? "" : `<div class="cardlist">${members.map(workloadCard).join("")}</div>`}</section>`;
  }).join("");
}
function workloadGroupBar(all, pick) {
  const names = workloadGroupNames(all);
  const loose = all.filter(w => !w.group).length;
  if (!names.length) return "";
  const chip = (value, label, count) => `<button type="button" class="${pick === value ? "on" : ""}" aria-pressed="${pick === value}" onclick="pickWorkloadGroup(${groupKeyIndex(value)})">${esc(label)} <span class="dim">${count}</span></button>`;
  return `<div class="wgroup-bar">
    ${names.length ? `<div class="seg wgroup-chips" role="group" aria-label="Show group">${chip("", "All", all.length)}${names.map(name =>
      chip(name, name, all.filter(w => w.group === name).length)).join("")}${loose ? chip(NO_GROUP, "Ungrouped", loose) : ""}</div>` : ""}</div>`;
}

function workloadGroupSelect(all, pick) {
  const names = workloadGroupNames(all), loose = all.filter(w => !w.group).length;
  if (!names.length) return '<span class="collection-mobile-scope">All containers</span>';
  const option = (value, label, count) => `<option value="${groupKeyIndex(value)}"${pick === value ? " selected" : ""}>${esc(label)} · ${count}</option>`;
  return `<select class="wl-group-select" aria-label="Container group" title="${esc(pick === NO_GROUP ? "Ungrouped" : pick || "All groups")}" onchange="pickWorkloadGroup(+this.value)">
    ${option("", "All groups", all.length)}${names.map(name => option(name, name, all.filter(w => w.group === name).length)).join("")}${loose ? option(NO_GROUP, "Ungrouped", loose) : ""}</select>`;
}

function workloadListOptions(platform, layout, items) {
  const controls = `${layout === "rows" ? '<div class="sortbar" data-sort-controls="containers"></div>' : ""}
    ${platform.length ? `<label class="list-option-setting">Show platform containers <input type="checkbox"${platformShown() ? " checked" : ""} onchange="togglePlatformContainers()"></label>` : ""}
    <label class="list-option-setting">Layout <select aria-label="Container layout" onchange="workloadListLayout(this)">
      <option value="rows"${layout === "rows" ? " selected" : ""}>Rows</option><option value="cards"${layout === "cards" ? " selected" : ""}>Cards</option></select></label>`;
  return listOptions(controls, items);
}
window.workloadListLayout = select => {
  const layout = select.value;
  closeActionMenu(select.closest("details"));
  // Keep the header and focus in place while only the list changes shape.
  document.querySelector(".collection-mobile-toolbar .list-options>summary")?.focus({preventScroll:true});
  setViewLayout("containers", "renderWorkloads", layout, {preservePaint:true});
};

/* Put one workload in a group: pick one it could join, or name a new one. */
window.wlGroup = (ns, name) => {
  const w = (STATE.data.wl || []).find(x => x.ns === ns && x.name === name) || {};
  const names = workloadGroupNames(STATE.data.wl || []);
  window.__groupTarget = [{ ns, name }];
  modal("Group · " + name, `<p class="muted small">Groups gather apps under a divider on Containers, and each gets a chip to show it alone.</p>
    <div class="f" style="margin-top:12px"><label>Group</label><input id="wg_name" list="wg_names" maxlength="40" value="${esc(w.group || "")}" placeholder="e.g. Media">
      <datalist id="wg_names">${names.map(n => `<option value="${esc(n)}">`).join("")}</datalist></div>
    <div class="row" style="justify-content:flex-end;margin-top:14px;gap:8px">
      ${w.group ? `<button class="btn" onclick="saveWorkloadGroup(window.__groupTarget, '')">Remove from ${esc(w.group)}</button>` : ""}
      <button class="btn pri" onclick="saveWorkloadGroup(window.__groupTarget, $('#wg_name').value)">Save</button></div>`);
  setTimeout(() => $("#wg_name")?.focus(), 30);
};

/* Several at once: tick containers - across groups, and across filters, which
   keep what is ticked - then move them into a group or out of every group.
   A group is a label on each container, so it exists while something is in
   it: renaming one moves its containers to the new name, and ungrouping them
   all removes it. */
const WG = { picked: new Set(), q: "", group: "", folded: new Set(), keys: [] };
const wgKey = w => `${w.ns}/${w.name}`;
const wgRows = () => (STATE.data.wl || []).filter(w => !w.site && (!w.platform || platformShown()));

window.manageWorkloadGroups = (keep = false) => {
  if (!keep) Object.assign(WG, { picked: new Set(), q: "", group: "", folded: new Set() });
  const rows = wgRows(), names = workloadGroupNames(rows);
  const count = g => rows.filter(w => g === NO_GROUP ? !w.group : w.group === g).length;
  modal("Groups", `<div class="ui-stack">
    ${UI.lead("Tick containers, then move them into a group or out of every group. What you tick stays ticked when you change the filter. A group lasts while something is in it.")}
    <div class="wg-tools">
      <input id="wg_q" type="search" placeholder="Filter by name or namespace" aria-label="Filter containers" value="${esc(WG.q)}" oninput="wgFilter()">
      <select id="wg_group" aria-label="Show group" onchange="wgFilter()"><option value="">Every group</option>
        ${names.map(n => `<option value="${esc(n)}"${WG.group === n ? " selected" : ""}>${esc(n)} (${count(n)})</option>`).join("")}
        ${count(NO_GROUP) ? `<option value="${NO_GROUP}"${WG.group === NO_GROUP ? " selected" : ""}>Ungrouped (${count(NO_GROUP)})</option>` : ""}</select></div>
    <div class="wg-selbar"><b id="wg_count" aria-live="polite"></b>
      ${UI.button("Tick all shown", "wgTickShown(true)")}${UI.button("Clear", "wgClear()")}</div>
    <div class="wg-list" id="wg_list"></div>
    <div class="wg-apply"><div class="f"><label for="wg_bulk">Group</label><input id="wg_bulk" list="wg_bulk_names" maxlength="40" placeholder="e.g. Media">
      <datalist id="wg_bulk_names">${names.map(n => `<option value="${esc(n)}">`).join("")}</datalist></div>
      ${UI.button("Ungroup ticked", "wgApply(false)")}${UI.button("Move ticked", "wgApply(true)", { kind: "pri" })}</div>
    ${UI.actions(UI.cancel("Close"))}</div>`);
  wgRender();
};

function wgShown() {
  const q = WG.q.toLowerCase();
  return wgRows().filter(w => (!q || w.name.toLowerCase().includes(q) || w.ns.toLowerCase().includes(q))
    && (!WG.group || (WG.group === NO_GROUP ? !w.group : w.group === WG.group)));
}

function wgRender() {
  const list = $("#wg_list");
  if (!list) return;
  const shown = wgShown();
  const groups = [...workloadGroupNames(shown), ...(shown.some(w => !w.group) ? [NO_GROUP] : [])];
  WG.keys = groups;          // handlers name a group by its index here
  list.innerHTML = shown.length ? groups.map((g, i) => {
    const members = shown.filter(w => g === NO_GROUP ? !w.group : w.group === g);
    const all = wgRows().filter(w => g === NO_GROUP ? !w.group : w.group === g);
    const ticked = members.filter(w => WG.picked.has(wgKey(w))).length;
    const folded = WG.folded.has(g);
    return `<section class="wg-group">
      <div class="wg-head"><button type="button" class="wg-fold" aria-expanded="${!folded}" onclick="wgFold(${i})">
          <svg class="btnicon" aria-hidden="true"><use href="#i-chevron-down"/></svg><b>${esc(g === NO_GROUP ? "Ungrouped" : g)}</b>
          <span class="dim xs">${members.length < all.length ? `${members.length} of ${all.length}` : all.length}${ticked ? ` · ${ticked} ticked` : ""}</span></button>
        ${actionBar([{ label: ticked === members.length ? "Untick these" : "Tick these", run: `wgTickGroup(${i},${ticked !== members.length})` },
          ...(g === NO_GROUP ? [] : [{ label: "Rename…", run: `wgRename(${i})`, need: "operator" }, { label: "Ungroup all…", run: `wgUngroupAll(${i})`, need: "operator" }])], { shown: 1, label: `More for ${g === NO_GROUP ? "ungrouped" : g}` })}</div>
      <div class="wg-members"${folded ? " hidden" : ""}>${members.map(w => `<label class="wg-item"><input type="checkbox" data-key="${esc(wgKey(w))}"${WG.picked.has(wgKey(w)) ? " checked" : ""} onchange="wgTick(this)">
        ${appAvatar(w.name, w.icon)}<span><b>${esc(w.name)}</b><span class="dim xs"> ${esc(w.ns)}</span></span></label>`).join("")}</div></section>`;
  }).join("") : '<div class="empty small">No container matches.</div>';
  loadAppIcons(list);
  wgCount();
}

function wgCount() {
  const n = WG.picked.size, host = $("#wg_count");
  if (host) host.textContent = n ? `${n} ticked` : "None ticked";
}

window.wgFilter = () => {
  WG.q = $("#wg_q")?.value || "";
  WG.group = $("#wg_group")?.value || "";
  wgRender();
};
window.wgTick = box => {
  box.checked ? WG.picked.add(box.dataset.key) : WG.picked.delete(box.dataset.key);
  wgRender();
};
window.wgTickShown = on => {
  wgShown().forEach(w => on ? WG.picked.add(wgKey(w)) : WG.picked.delete(wgKey(w)));
  wgRender();
};
window.wgClear = () => { WG.picked.clear(); wgRender(); };
const wgMembers = (i, shownOnly = false) => {
  const g = WG.keys[i];
  return (shownOnly ? wgShown() : wgRows()).filter(w => g === NO_GROUP ? !w.group : w.group === g);
};
window.wgTickGroup = (i, on) => {
  wgMembers(i, true).forEach(w => on ? WG.picked.add(wgKey(w)) : WG.picked.delete(wgKey(w)));
  wgRender();
};
window.wgFold = i => {
  const g = WG.keys[i];
  WG.folded.has(g) ? WG.folded.delete(g) : WG.folded.add(g);
  wgRender();
};
const wgItems = rows => rows.map(w => ({ ns: w.ns, name: w.name }));
window.wgApply = into => {
  const items = wgRows().filter(w => WG.picked.has(wgKey(w)));
  const group = into ? ($("#wg_bulk")?.value || "").trim() : "";
  if (into && !group) return toast("Name the group to move them into", "bad");
  saveWorkloadGroup(wgItems(items), group, true);
};

/* Renaming moves every container in the group to the new name. */
window.wgRename = i => {
  const g = WG.keys[i], members = wgMembers(i), names = workloadGroupNames(wgRows()).filter(n => n !== g);
  window.__wgGroup = { items: wgItems(members), from: g };
  modal(`Rename group · ${g}`, `<div class="ui-stack">
    ${UI.lead(esc(`Its ${members.length} container${members.length === 1 ? "" : "s"} move to the new name. A name another group has merges the two.`))}
    ${UI.field("New name", `<input id="wg_rename" maxlength="40" value="${esc(g)}" list="wg_rename_names"><datalist id="wg_rename_names">${names.map(n => `<option value="${esc(n)}">`).join("")}</datalist>`)}
    ${UI.actions(UI.button("Back", "manageWorkloadGroups(true)", { attrs: 'data-dialog-dismiss="true"' }) + UI.button("Rename", "wgRenameApply()", { kind: "pri", attrs: 'data-need="operator"' }))}</div>`);
  setTimeout(() => { const input = $("#wg_rename"); input?.focus(); input?.select(); }, 30);
};
window.wgRenameApply = () => {
  const name = ($("#wg_rename")?.value || "").trim(), { items, from } = window.__wgGroup || {};
  if (!name) return toast("Name the group", "bad");
  if (name === from) return manageWorkloadGroups(true);
  if (WG.group === from) WG.group = name;
  saveWorkloadGroup(items, name, true, from);
};

/* Ungrouping every container in a group removes it. */
window.wgUngroupAll = i => {
  const g = WG.keys[i], members = wgMembers(i);
  window.__wgGroup = { items: wgItems(members), from: g };
  modal(`Ungroup · ${g}`, `<div class="ui-stack">
    ${UI.lead(esc(`Its ${members.length} container${members.length === 1 ? "" : "s"} leave the group, and ${g} is gone: a group lasts while something is in it. Nothing restarts.`))}
    ${UI.actions(UI.button("Back", "manageWorkloadGroups(true)", { attrs: 'data-dialog-dismiss="true"' }) + UI.button("Ungroup all", "wgUngroupApply()", { kind: "danger", attrs: 'data-need="operator"' }))}</div>`);
};
window.wgUngroupApply = () => {
  const { items, from } = window.__wgGroup || {};
  if (WG.group === from) WG.group = "";
  saveWorkloadGroup(items, "", true);
};

window.checkedWorkloads = () => wgItems(wgRows().filter(w => WG.picked.has(wgKey(w))));
window.saveWorkloadGroup = async (items, group, stay = false, renamedFrom = "") => {
  if (!items || !items.length) return toast("Tick at least one container", "bad");
  try {
    const result = await api("/api/workloads/group", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ items, group: String(group || "").trim(), ...(renamedFrom ? { renamed_from: renamedFrom } : {}) }) });
    toast(result.detail, "ok");
    const moved = new Set(items.map(item => `${item.ns}/${item.name}`));
    (STATE.data.wl || []).forEach(w => { if (moved.has(`${w.ns}/${w.name}`)) w.group = result.group; });
    if (stay) {
      WG.picked.clear();
      if (WG.group && WG.group !== NO_GROUP && !workloadGroupNames(wgRows()).includes(WG.group)) WG.group = "";
      manageWorkloadGroups(true);
    } else closeModal();
    renderWorkloads();
  } catch (e) { toast(e.message, "bad"); }
};

/* A group's tier (#430): the apps that follow it take it. Only the tier is
   saved and nothing restarts; Priority tiers… lists the restarts to start. */
const GROUP_TIER = { group: "", plan: null };
function groupTierLine(plan, group, tier) {
  if (!plan) return "Homestead couldn't read the apps just now; the tier can still be saved.";
  const rows = (plan.apps || []).filter(a => a.group === group), want = tier === "none" ? "normal" : tier;
  const follow = rows.filter(a => a.follows), restart = follow.filter(a => a.current !== want).length;
  const git = tier === "none" ? 0 : rows.filter(a => a.managed && a.current !== want).length;
  const lead = !follow.length ? `No app follows ${group}: each has a tier of its own.`
    : `${follow.length} app${follow.length === 1 ? " follows" : "s follow"} ${group}; ${restart
      ? `${restart} would need a restart. Nothing restarts now. Start ${restart === 1 ? "it" : "them"} from Priority tiers…` : "none would need a restart."}`;
  return git ? `${lead} ${git} ${git === 1 ? "is" : "are"} applied by Flux or Argo CD: change ${git === 1 ? "it" : "them"} in git.` : lead;
}
window.groupTier = async index => {
  const group = WL_GROUP_KEYS[index];
  if (!group || group === NO_GROUP) return;
  // null: the group tiers couldn't be read. Nothing is preselected then, so
  // "None" is never saved by mistake; Save waits for a choice.
  const known = (STATE.data.wl || []).find(w => w.group === group)?.group_tier, now = known === null ? "" : known || "none";
  Object.assign(GROUP_TIER, { group, plan: null });
  modal(`Tier · ${group}`, `<div class="ui-stack">
    ${UI.lead(esc(`Apps that follow ${group} take this tier. Apps with a tier of their own keep it.`))}
    ${UI.field("Tier", `<select id="gt_tier" aria-label="Tier for ${esc(group)}" onchange="groupTierEffect()">${now ? "" : '<option value="" selected disabled>Choose a tier</option>'}${["critical", "normal", "low", "none"].map(t =>
      `<option value="${t}" ${t === now ? "selected" : ""}>${t[0].toUpperCase() + t.slice(1)}</option>`).join("")}</select>`)}
    ${now ? "" : `<p class="dim small">${esc(`Homestead couldn't read ${group}'s tier just now; choose the tier it should have.`)}</p>`}
    <p class="dim small" id="gt_effect" aria-live="polite">Reading the apps…</p>
    ${UI.actions(UI.button("Save", "groupTierSave()", { kind: "pri", id: "gtGo", attrs: 'data-need="operator"', disabled: !now }), UI.cancel())}</div>`);
  try { GROUP_TIER.plan = await api("/api/workloads/priority/plan"); } catch (e) { GROUP_TIER.plan = null; }
  groupTierEffect();
  window.applyRole?.();
};
window.groupTierEffect = () => {
  const host = $("#gt_effect"), tier = $("#gt_tier")?.value, button = $("#gtGo");
  if (button) button.disabled = !tier;
  if (host) host.textContent = tier ? groupTierLine(GROUP_TIER.plan, GROUP_TIER.group, tier) : "Choose a tier to see what it would do.";
};
window.groupTierSave = async () => {
  const { group } = GROUP_TIER, tier = $("#gt_tier")?.value, button = $("#gtGo");
  if (!tier) return;      // nothing chosen yet: the tier couldn't be read
  if (button) button.disabled = true;
  try {
    const result = await api("/api/workloads/group-tier", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ group, tier }) });
    (STATE.data.wl || []).forEach(w => { if (w.group === group) w.group_tier = result.tier; });
    closeModal(); toast(result.detail, "ok"); renderWorkloads();
  } catch (e) {
    toast(e.message, "bad");     // a 409: the tiers changed elsewhere; the dialog stays open to try again
    if (button) button.disabled = false;
  }
};

/* The platform's own containers - KubeVirt, CDI, the upgrade controller that
   Homestead's add-ons install on k3s and RKE2 - are hidden unless asked for,
   as Harvester's are. */
window.togglePlatformContainers = () => {
  STATE.showPlatform = !STATE.showPlatform;
  try { localStorage.setItem("homestead.showPlatform", STATE.showPlatform ? "1" : ""); } catch (_) { /* private window */ }
  renderWorkloads();
};
function platformShown() {
  if (STATE.showPlatform === undefined) {
    try { STATE.showPlatform = localStorage.getItem("homestead.showPlatform") === "1"; } catch (_) { STATE.showPlatform = false; }
  }
  return STATE.showPlatform;
}

/* Move several (#366): tick containers - across filters, which keep what
   is ticked - choose a host, and review them together: each one's host,
   why one can't go, and the hosts' room with all of them counted. One job
   moves them, one at a time, and stops at the first that does not go. */
const MV = { picked: new Set(), q: "", to: "", over: {}, plan: null, seq: 0 };
window.MV = MV;
const mvRows = () => (STATE.data.wl || []).filter(w => !w.site && !w.platform && !w.managed_smb && !w.managed_nfs);
const mvShown = () => mvRows().filter(w => !MV.q || `${w.ns}/${w.name}`.toLowerCase().includes(MV.q.toLowerCase()));
const mvHosts = () => ((STATE.data.nodes || []).filter(n => n.schedulable !== false)).map(n => n.name);

window.moveSeveral = async (ids = null, to = "", avoid = "") => {
  // The hosts come with the Nodes page; opened from elsewhere, read them here.
  if (!(STATE.data.nodes || []).length) {
    try { STATE.data.nodes = await api("/api/nodes"); } catch (e) { return toast(`Could not read the hosts: ${e.message}`, "bad"); }
  }
  Object.assign(MV, { picked: new Set(ids || []), q: "", to: to || mvHosts().find(h => h !== avoid) || "", over: {}, plan: null });
  if (ids) return mvReview();
  modal("Move several", `<div class="ui-stack">
    ${UI.lead("Tick the containers to move, then review them together. What you tick stays ticked when you change the filter.")}
    <div class="wg-tools"><input id="mv_q" type="search" placeholder="Filter by name or namespace" aria-label="Filter containers" oninput="mvFilter()"></div>
    <div class="wg-selbar"><b id="mv_count" aria-live="polite"></b>${UI.button("Tick all shown", "mvTickShown(true)")}${UI.button("Clear", "mvClear()")}</div>
    <div class="wg-list" id="mv_list"></div>
    ${UI.actions(UI.cancel() + UI.button("Review…", "mvReview()", { kind: "pri", id: "mvNext" }))}</div>`);
  mvRender();
};
window.mvFilter = () => { MV.q = ($("#mv_q") || {}).value || ""; mvRender(); };
window.mvTick = (id, on) => { on ? MV.picked.add(id) : MV.picked.delete(id); mvRender(); };
window.mvTickShown = on => { mvShown().forEach(w => on ? MV.picked.add(`${w.ns}/${w.name}`) : MV.picked.delete(`${w.ns}/${w.name}`)); mvRender(); };
window.mvTickHost = host => { mvShown().filter(w => (w.nodes || []).includes(host)).forEach(w => MV.picked.add(`${w.ns}/${w.name}`)); mvRender(); };
window.mvClear = () => { MV.picked.clear(); mvRender(); };
function mvRender() {
  const list = $("#mv_list"), count = $("#mv_count");
  if (count) count.innerHTML = MV.picked.size ? `${MV.picked.size} ticked` : "None ticked";
  if (!list) return;
  const shown = mvShown(), hosts = [...new Set(shown.map(w => (w.nodes || [])[0] || "unscheduled"))].sort();
  list.innerHTML = shown.length ? hosts.map(h => `<div class="wg-group"><div class="between"><b class="mono">${esc(h)}</b>
      ${h === "unscheduled" ? "" : UI.button("Tick these", `mvTickHost(${jsArg(h)})`, { kind: "sm" })}</div>
      ${shown.filter(w => ((w.nodes || [])[0] || "unscheduled") === h).map(w => { const id = `${w.ns}/${w.name}`;
        return `<label class="wg-item"><input type="checkbox" value="${esc(id)}" ${MV.picked.has(id) ? "checked" : ""}
          onchange="mvTick(${jsq(id)},this.checked)"> <span class="mono">${esc(id)}</span></label>`; }).join("")}</div>`).join("")
    : '<div class="ui-empty">No container matches.</div>';
}

window.mvReview = () => {
  if (!MV.picked.size) return toast("Tick at least one container", "warn");
  modal("Move several", `<div id="mvBody"><div class="empty"><span class="spin2"></span> Checking the hosts…</div></div>`);
  return mvPlan();
};
window.mvTo = to => { MV.to = to; MV.over = {}; return mvPlan(); };
window.mvOverride = (id, to) => { MV.over[id] = to; return mvPlan(); };
window.mvUntick = id => { MV.picked.delete(id); return mvPlan(); };
const mvPicks = () => [...MV.picked].map(id => ({ id, to: MV.over[id] || MV.to }));

window.mvPlan = async () => {
  const seq = ++MV.seq;
  let plan;
  try { plan = await api("/api/workloads/rebalance/review", { method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ picks: mvPicks() }) }); }
  catch (e) { if (seq === MV.seq) $("#mbody").innerHTML = UI.callout("bad", "Could not check the hosts", esc(e.message)); return; }
  if (seq !== MV.seq) return;              // a newer review was asked for meanwhile
  MV.plan = plan;
  const moves = plan.moves || [], stays = (plan.rows || []).filter(r => r.why);
  const hostSelect = (value, change, label) => `<select aria-label="${esc(label)}" onchange="${change}">${mvHosts().map(h =>
    `<option value="${esc(h)}"${h === value ? " selected" : ""}>${esc(h)}</option>`).join("")}</select>`;
  const rows = (plan.rows || []).map(r => [
    `<label class="rb-app"><input type="checkbox" value="${esc(r.id)}" ${r.why ? "disabled" : "checked"}
      onchange="mvUntick(${jsq(r.id)})"> <span class="mono">${esc(r.id)}</span></label>`,
    r.why ? `<span class="ui-help">${esc(r.why)}</span>`
      : `<span class="mono">${esc(r.from)} → ${esc(r.to)}</span> <span class="sub">${r.reserves ? `${esc(r.cpu_m)}m CPU · ${esc(r.mem_mb)} MiB reserved` : "reserves nothing"}${r.replaces ? ` · replaces its preference for ${esc(r.replaces)}` : ""}</span>`,
    hostSelect(MV.over[r.id] || MV.to, `mvOverride(${jsq(r.id)},this.value)`, `Host for ${r.id}`)]);
  const notice = !moves.length ? UI.callout("bad", "Can't move", "Choose another host for them, or untick them.")
    : stays.length || (plan.hosts || []).some(h => h.over) ? UI.callout("warn", "Check first",
      `${stays.length ? `${stays.length} stay where they are. ` : ""}Each one moved restarts on its new host.`) : "";
  $("#mbody").innerHTML = [
    UI.lead(!moves.length ? "None of these can move to the hosts chosen; each one says why."
      : `Moves ${moves.length} container${moves.length === 1 ? "" : "s"} to ${esc([...new Set(moves.map(m => m.to))].join(", "))}; each restarts there, one at a time. ${BALANCE_LEAVES_PREFERENCE}`),
    `<div class="f"><label>Move to</label>${hostSelect(MV.to, "mvTo(this.value)", "Move to")}</div>`,
    notice,
    UI.section(`Containers · ${rows.length}`, UI.table([{ label: "Container" }, { label: "Move", className: "grow" }, { label: "Host" }], rows)),
    (plan.hosts || []).length ? UI.section("Hosts", mvHostTable(plan)) : "",
    moves.length ? UI.ack("mvAck", `Restart these ${moves.length} container${moves.length === 1 ? "" : "s"}`) : "",
    UI.actions(UI.cancel() + UI.button(`Move ${moves.length}`, "mvGo()", { kind: "pri", disabled: !moves.length, attrs: 'data-need="operator"' })),
  ].join("");
  window.applyRole?.();
};

/* What the destinations have reserved - the scheduler places by requests -
   with every container sent there counted. Not what they use: a container
   that reserves nothing adds nothing here, so the table says so. */
function mvHostTable(plan) {
  const meter = (before, after, total, label) => total ? UI.meter({ now: before / total * 100, after: after / total * 100, warnAt: 90, label }) : "";
  return UI.table([{ label: "Host" }, { label: "Reserved CPU", className: "grow" }, { label: "Reserved memory", className: "grow" }], (plan.hosts || []).map(h => {
    const nothing = (plan.rows || []).some(r => r.to === h.name && !r.why && !r.reserves);
    return [`<span class="mono">${esc(h.name)}</span>${h.over ? ` ${UI.chip("over", "bad")}` : ""}`,
      `${meter(h.cpu_before_m, h.cpu_after_m, h.cpu_total_m, `${h.name} CPU`)}<span class="sub">${esc(h.cpu_after_m)}m of ${esc(h.cpu_total_m)}m CPU</span>`,
      `${meter(h.used_gb, h.projected_gb, h.capacity_gb, `${h.name} memory`)}<span class="sub">${esc(h.projected_gb)} of ${esc(h.capacity_gb)} GiB${
        nothing ? " · some here reserve nothing, so they may use more" : ""}</span>`];
  }));
}

window.mvGo = async () => {
  const plan = MV.plan;
  if (!plan || !(plan.moves || []).length) return;
  if (!$("#mvAck")?.checked) return toast("Confirm the restarts first", "warn");
  try {
    await api("/api/workloads/rebalance", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ picks: plan.moves.map(m => ({ id: m.id, to: m.to })), review_token: plan.review_token, restart: true }) });
    toast(`Moving ${plan.moves.length}; follow it in Jobs`, "ok"); closeModal(); refresh(true);
  } catch (e) { toast(e.message, "bad"); return mvPlan(); }
};


// Node only (tests): the same names, for require(). The browser has no `module`.
if (typeof module === "object" && module.exports) module.exports = {
  NO_GROUP, workloadGroupPrefs, workloadGroupNames, WL_GROUP_KEYS, groupKeyIndex, workloadGroupHead, workloadSections, workloadGroupBar, workloadGroupSelect, workloadListOptions, WG, wgKey, wgRows, wgShown, wgRender, wgCount, wgMembers, wgItems, GROUP_TIER, groupTierLine, platformShown, MV, mvRows, mvShown, mvHosts, mvRender, mvPicks, mvHostTable
};
