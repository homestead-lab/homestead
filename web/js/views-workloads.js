/* Containers, Deploy, App Store */

async function viewWorkloads() {
  STATE.data.wl = await api("/api/workloads");
  renderWorkloads();
  loadImageUpdates(false, true).then(() => {
    if (STATE.view === "workloads" && $("#modal").classList.contains("hidden")) renderWorkloads();
  });
}

const updateKey = (ns, name) => `${ns}/${name}`;
const workloadUpdate = (ns, name) => (STATE.data.imageUpdateMap || {})[updateKey(ns, name)];

function paintUpdateBadge(count, errors = 0) {
  const badge = $("#updateBadge");
  if (badge) {
    badge.textContent = count;
    badge.classList.toggle("hidden", !count);
  }
  window.BELL = { ...(window.BELL || {}), images: count, errors };
  if (window.paintBell) paintBell();
  const notice = $("#updateNotice"), noticeBadge = $("#updateNoticeBadge"), noticeErrors = $("#updateNoticeErrors");
  if (!notice || !noticeBadge) return;
  // Updates and failed checks are counted apart: one registry that cannot be
  // reached must not turn the updates that were found into a red error count.
  notice.classList.toggle("hidden", !count && !errors);
  notice.classList.toggle("only-errors", !count && !!errors);
  noticeBadge.textContent = count;
  if (noticeErrors) {
    noticeErrors.textContent = errors;
    noticeErrors.classList.toggle("hidden", !errors);
  }
  notice.setAttribute("aria-label", `${count} image update${count === 1 ? "" : "s"} available${errors ? `, ${errors} registry check failure${errors === 1 ? "" : "s"}` : ""}`);
}

/* An image being fetched: containerd's count of the bytes so far. */
function pullBar(pull) {
  const pct = Math.min(100, pull.percent || 0);
  return `<div class="pullbar" title="Fetching ${esc(pull.image || "the image")}${pull.node ? ` on ${esc(pull.node)}` : ""}">
    <span class="dim xs">Pulling image · ${pct}% of ${pullSize(pull.total_bytes)}</span>
    <div class="meter"><span style="width:${pct}%"></span></div></div>`;
}
const workloadPull = w => (w.pods || []).map(p => p.pull).find(pull => pull?.total_bytes);
/* Why a workload that should run does not: the scheduler's reason, said plainly. */
const workloadBlocked = w => {
  const pod = (w.pods || []).find(p => p.unplaced);
  return pod ? `Cannot start: ${pod.unplaced}` : "";
};

function workloadHierarchy(w) {
  const pods = w.pods || [];
  const podCount = w.pod_count ?? pods.length;
  const containerCount = w.container_count ?? pods.reduce((n, p) => n + (p.container_count || p.containers?.filter(c => c.kind !== "init").length || 0), 0);
  const podLabel = `${podCount} pod${podCount === 1 ? "" : "s"}`;
  const containerLabel = `${containerCount} container${containerCount === 1 ? "" : "s"}`;
  return `<details class="workloadtree">
    <summary title="Show ${esc(w.kind || "Deployment")} runtime objects"><span class="tree-kind">${esc(w.kind || "Deployment")}</span><span class="tree-arrow">→</span>
      <span>${podLabel}</span><span class="tree-arrow">→</span><span>${containerLabel}</span>
      <span class="tree-hint">show runtime objects</span></summary>
    <div class="tree-body">${pods.map(p => `<div class="tree-pod">
      <div class="tree-pod-head"><span class="tree-branch">Pod</span><b class="mono">${esc(p.hostname || p.name)}</b>
        ${p.hostname ? `<span class="mono dim tree-resource" title="Kubernetes runtime name">${esc(p.name)}</span>` : ""}
        <span class="pill ${p.terminating ? "low" : p.ready ? "ok" : p.phase === "Pending" ? "med" : "crit"}" ${p.terminating ? 'data-tip="Told to stop and not gone yet. One that never started is cleared when the container is stopped or started again"' : ""}>${p.terminating ? "stopping" : p.ready ? "ready" : esc(p.phase || "pending")}</span>
        <span class="dim xs tree-node">${esc(p.node || "unscheduled")}${p.restarts ? ` · ${p.restarts} restart${p.restarts === 1 ? "" : "s"}` : ""}</span></div>
      ${p.pull?.total_bytes ? `<div class="tree-pull">${pullBar(p.pull)}</div>` : ""}
      ${p.unplaced ? `<div class="tree-why unplaced" title="${esc(p.unplaced)}">Cannot be placed: ${esc(p.unplaced)}</div>` : ""}
      <div class="tree-containers">${(p.containers || []).map(c => `<div class="tree-container">
        <span class="tree-branch ${c.kind === "init" ? "init" : ""}">${c.kind === "init" ? "Init" : "Container"}</span>
        <b>${esc(c.name)}</b><span class="pill ${c.ready || (c.kind === "init" && c.state === "Completed") ? "ok" : c.state === "running" ? "med" : "low"}">${esc(c.state || "pending")}</span>
        ${c.restarts ? `<span class="tag warn">${c.restarts} restart${c.restarts === 1 ? "" : "s"}</span>` : ""}
        <span class="mono dim tree-image">${esc(c.image || "image unavailable")}</span>
      </div>${c.message && !c.ready ? `<div class="tree-why" title="${esc(c.detail || c.message)}">${esc(c.message)}${c.detail && c.detail !== c.message
        ? ` <a class="linkish xs" onclick="event.stopPropagation(); modal(${jsq(`Why ${c.name} is not running`)}, '<pre class=&quot;mono small wrap-pre&quot;>' + esc(this.dataset.detail) + '</pre>')" data-detail="${esc(c.detail)}">full message</a>` : ""}</div>` : ""}`).join("") || `<div class="dim xs">Container detail is unavailable for this pod.</div>`}</div>
    </div>`).join("") || `<div class="dim xs">No pods exist yet. The workload controller will create them when the instance count is above zero.</div>`}</div>
  </details>`;
}

/* When the registries were last asked, so a stale answer is not mistaken for
   a fresh one. */
function checkedAgo() {
  const at = STATE.data.imageUpdates?.checked_at;
  if (!at) return "";
  const secs = Math.max(0, (Date.now() - Date.parse(at)) / 1000);
  return "checked " + (secs < 90 ? "just now" : fmtAgo(secs));
}

async function loadImageUpdates(force = false, quiet = false, only = "") {
  try {
    // A forced check outlives the page it started on: its answer is for the
    // whole app, not just the view that asked. only="homestead" checks
    // Homestead's own parts and leaves every app's answer as it was.
    const query = force ? `?force=1${only ? `&only=${encodeURIComponent(only)}` : ""}` : "";
    let report = await api(`/api/image-updates${query}`, force ? { keep: true } : undefined);
    const selectedChannel = STATE.data.appSettings?.updates?.channel;
    if (selectedChannel && report.channel && report.channel !== selectedChannel) return STATE.data.imageUpdates;
    // The page's quiet refresh asks every few seconds and can land before or
    // after a forced check. Which report is newer is the server's to say, by
    // when it was checked - never by which request happened to be sent last.
    const existing = STATE.data.imageUpdates;
    if (report.partial && existing?.workloads) {
      // Homestead's parts alone, the server having no full report yet: laid
      // over the apps' answers this page already has.
      report = { ...existing, channel: report.channel, homestead: report.homestead, policy: report.policy || existing.policy,
        workloads: [...existing.workloads.filter(w => !w.homestead), ...(report.workloads || [])] };
    }
    if (HomesteadUpdateState.isStale(existing, report)) {
      existing.policy = report.policy || existing.policy;
      return existing;
    }
    STATE.data.imageUpdates = report;
    STATE.data.imageUpdateMap = Object.fromEntries((report.workloads || [])
      .map(x => [updateKey(x.ns, x.name), x]));
    paintUpdateBadge(report.updates || 0, report.errors || 0);
    if (window.paintHomesteadNotice) paintHomesteadNotice();
    const previous = +(localStorage.getItem("homestead.update-count") || 0);
    const preferences = STATE.data.appSettings?.updates || {};
    if (!quiet && preferences.notify_available !== false && report.updates > previous)
      toast(`${report.updates} container image update${report.updates === 1 ? "" : "s"} available`, "ok");
    const previousErrors = +(localStorage.getItem("homestead.update-errors") || 0);
    if (!quiet && preferences.notify_failures !== false && report.errors > previousErrors)
      toast(`${report.errors} image registry check${report.errors === 1 ? " needs" : "s need"} attention`, "bad");
    localStorage.setItem("homestead.update-count", report.updates || 0);
    localStorage.setItem("homestead.update-errors", report.errors || 0);
    return report;
  } catch (e) {
    if (!quiet) toast("Image update check failed · " + e.message, "bad");
    return null;
  }
}
window.loadImageUpdates = loadImageUpdates;
window.startUpdateChecks = () => {
  clearInterval(window.__imageUpdateLoop);
  clearTimeout(window.__imageUpdateStart);
  window.__imageUpdateStart = setTimeout(() => loadImageUpdates(false, false), 3500);
  window.__imageUpdateLoop = setInterval(() => {
    if (!document.hidden) loadImageUpdates(false, false);
  }, 15 * 60 * 1000);
};

/* The release an update moves to: 2.8.215 → 2.8.217, or a new build of
   the same tag. */
function updateVersions(w) {
  const moving = (w.images || []).filter(i => i.available);
  return moving.map(i => {
    const from = imageVersion(i.source || i.deployed).tag, to = i.candidate_tag || "";
    const text = from && to && from !== to ? `${from} → ${to}` : `${to || from || "latest"} · new build`;
    return (moving.length > 1 ? `${i.container} ` : "") + text;
  }).join(" · ");
}

window.imageUpdateCenter = async () => {
  let report = STATE.data.imageUpdates;
  if (!report) {
    modal("Image updates", '<div class="empty"><span class="spin2"></span>checking registries…</div>');
    report = await loadImageUpdates(false, true);
  }
  if (!report) {
    // A quiet load says nothing when it fails, so this dialog has to.
    if (!$("#modal").classList.contains("hidden")) $("#mbody").innerHTML = `<div class="empty"><b>The registries could not be checked</b>
      <br><span class="dim small">Try ↻ Check images on the Containers page, which shows what went wrong.</span></div>`;
    return;
  }
  // Updates first: a failed check beside them is a note, not the headline.
  const affected = (report.workloads || []).filter(w => !w.homestead && (w.available || w.images?.some(image => image.error)))
    .sort((a, b) => Number(!!b.available) - Number(!!a.available));
  const available = HomesteadUpdateState.availableWorkloads(report);
  const policy = report.policy || {};
  modal("Image updates", `<div class="update-center">
    ${UI.lead(`<b>${esc(policy.policy === "notify_only" ? "Notify only" : policy.policy === "maintenance_window" ? "Maintenance window" : "Approval required")}.</b>
      ${esc(policy.reason || "Every rollout requires an explicit review.")}`)}
    ${available.length ? `<div class="update-selectbar">
      <label class="switch"><input type="checkbox" id="updateSelectAll" checked onchange="toggleImageUpdateSelection(this.checked)"> Select all</label>
      <span id="updateSelectedCount">${available.length} of ${available.length} selected</span>
      <button class="btn pri" id="updateStage" data-need="operator" onclick="imageUpdateBatchReview()">Stage selected (${available.length})</button>
    </div>` : ""}
    ${affected.length ? `<div class="settings-list">${affected.map(w => {
      const failures = (w.images || []).filter(image => image.error);
      return `<div class="settings-list-row update-center-row">${w.available ? `<label class="update-pick" title="Stage ${esc(w.name)}"><input class="update-select" type="checkbox" checked data-ns="${esc(w.ns)}" data-name="${esc(w.name)}" onchange="syncImageUpdateSelection()"><span></span></label>` : '<span class="update-pick-spacer"></span>'}<div><b>${esc(w.name)}</b><div class="dim xs mono">${esc(w.ns)}${updateVersions(w) ? ` · ${esc(updateVersions(w))}` : ""}</div>
        ${failures.map(image => `<div class="updateerror">${esc(image.container)} · ${esc(image.error)}</div>`).join("")}</div>
        <div class="row">${w.available ? '<span class="pill warn">update available</span>' : ""}
        ${failures.length ? '<span class="pill crit">check failed</span>' : ""}
        <button class="btn sm" onclick="openUpdateWorkload(${jsq(w.name)})">Open</button></div></div>`;
    }).join("")}</div>` : '<div class="empty small">Images are current and registry checks succeeded.</div>'}
    ${UI.actions(UI.button("Check now", "checkImageUpdates()") + UI.button("Open Containers", "closeModal();go('workloads')"))}</div>`, true);
  if (window.applyRole) window.applyRole();
};
window.syncImageUpdateSelection = () => {
  const boxes = [...document.querySelectorAll("#mbody .update-select")];
  const selected = boxes.filter(box => box.checked).length;
  const all = document.getElementById("updateSelectAll");
  if (all) {
    all.checked = !!boxes.length && selected === boxes.length;
    all.indeterminate = selected > 0 && selected < boxes.length;
  }
  const count = document.getElementById("updateSelectedCount");
  if (count) count.textContent = `${selected} of ${boxes.length} selected`;
  const stage = document.getElementById("updateStage");
  if (stage) {
    stage.disabled = selected === 0;
    stage.textContent = `Stage selected (${selected})`;
  }
};
window.toggleImageUpdateSelection = checked => {
  document.querySelectorAll("#mbody .update-select").forEach(box => { box.checked = checked; });
  syncImageUpdateSelection();
};
window.imageUpdateBatchReview = () => {
  const keys = new Set($$(".update-select:checked").map(box => updateKey(box.dataset.ns, box.dataset.name)));
  const items = HomesteadUpdateState.availableWorkloads(STATE.data.imageUpdates).filter(item => keys.has(updateKey(item.ns, item.name)));
  if (!items.length) return toast("Select at least one update to stage", "bad");
  return reviewImageActions(items.map(item => ({ns: item.ns, name: item.name})));
};
window.openUpdateWorkload = name => {
  closeModal();
  go("workloads");
  highlightInPage(name);
};
const updateNoticeButton = $("#updateNotice");
if (updateNoticeButton) {
  updateNoticeButton.onclick = () => imageUpdateCenter();
  updateNoticeButton.onkeydown = event => {
    if (event.key === "Enter" || event.key === " ") { event.preventDefault(); imageUpdateCenter(); }
  };
}

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
  return `<button type="button" class="wgroup-head" aria-expanded="${!folded}" onclick="foldWorkloadGroup(${groupKeyIndex(group || NO_GROUP)})">
    <span class="wgroup-chevron">›</span><b>${esc(group || "Ungrouped")}</b>
    <span class="dim xs">${rows.length} workload${rows.length === 1 ? "" : "s"}${running < rows.length ? ` · ${running} running` : ""}</span></button>`;
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

/* Put one workload in a group: pick one it could join, or name a new one. */
window.wlGroup = (ns, name) => {
  const w = (STATE.data.wl || []).find(x => x.ns === ns && x.name === name) || {};
  const names = workloadGroupNames(STATE.data.wl || []);
  window.__groupTarget = [{ ns, name }];
  modal("Group · " + name, `<p class="muted small">Groups gather workloads under a divider on Containers, and each gets a chip to show it alone.</p>
    <div class="f" style="margin-top:12px"><label>Group</label><input id="wg_name" list="wg_names" maxlength="40" value="${esc(w.group || "")}" placeholder="e.g. Media">
      <datalist id="wg_names">${names.map(n => `<option value="${esc(n)}">`).join("")}</datalist></div>
    <div class="row" style="justify-content:flex-end;margin-top:14px;gap:8px">
      ${w.group ? `<button class="btn" onclick="saveWorkloadGroup(window.__groupTarget, '')">Remove from ${esc(w.group)}</button>` : ""}
      <button class="btn pri" onclick="saveWorkloadGroup(window.__groupTarget, $('#wg_name').value)">Save</button></div>`);
  setTimeout(() => $("#wg_name")?.focus(), 30);
};

/* Several at once: tick the workloads, then name the group they go in. */
window.manageWorkloadGroups = () => {
  const rows = STATE.data.wl || [], names = workloadGroupNames(rows);
  modal("Groups", `<p class="muted small">Tick workloads, then move them to a group - an existing one or a new name - or out of every group. A group disappears when nothing is left in it.</p>
    <div class="wg-list">${rows.map(w => `<label class="wg-item"><input type="checkbox" data-ns="${esc(w.ns)}" data-name="${esc(w.name)}">
      ${appAvatar(w.name, w.icon)}<span><b>${esc(w.name)}</b><span class="dim xs"> ${esc(w.ns)}</span></span>
      <span class="pill slim ${w.group ? "" : "neutral"}">${esc(w.group || "ungrouped")}</span></label>`).join("")}</div>
    <div class="wg-apply"><div class="f"><label>Group</label><input id="wg_bulk" list="wg_bulk_names" maxlength="40" placeholder="e.g. Media">
      <datalist id="wg_bulk_names">${names.map(n => `<option value="${esc(n)}">`).join("")}</datalist></div>
      <button class="btn pri" onclick="saveWorkloadGroup(checkedWorkloads(), $('#wg_bulk').value)">Move ticked</button>
      <button class="btn" onclick="saveWorkloadGroup(checkedWorkloads(), '')">Ungroup ticked</button></div>`);
  loadAppIcons($("#mbody"));
};
window.checkedWorkloads = () => $$("#mbody .wg-item input:checked").map(box => ({ ns: box.dataset.ns, name: box.dataset.name }));
window.saveWorkloadGroup = async (items, group) => {
  if (!items || !items.length) return toast("Tick at least one workload", "bad");
  try {
    const result = await api("/api/workloads/group", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ items, group: String(group || "").trim() }) });
    toast(result.detail, "ok");
    const moved = new Set(items.map(item => `${item.ns}/${item.name}`));
    (STATE.data.wl || []).forEach(w => { if (moved.has(`${w.ns}/${w.name}`)) w.group = result.group; });
    closeModal(); renderWorkloads();
  } catch (e) { toast(e.message, "bad"); }
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

function renderWorkloads() {
  const q = STATE.q.toLowerCase();
  // Every linked cluster at once: each cluster is a group, with its chip.
  const everything = (STATE.data.wl || []).map(w => fleetAll() && w.site ? { ...w, group: w.site.name } : w);
  const platform = everything.filter(w => w.platform);
  const all = platformShown() ? everything : everything.filter(w => !w.platform);
  // A chip for a group that has since emptied would hide everything.
  const saved = workloadGroupPrefs().pick;
  const group = saved === NO_GROUP ? (all.some(w => !w.group) ? saved : "") : workloadGroupNames(all).includes(saved) ? saved : "";
  const rows = all.filter(x => !q || x.name.includes(q) || x.ns.includes(q) || (x.group || "").toLowerCase().includes(q) ||
    x.images.join(" ").toLowerCase().includes(q) || x.nodes.join(" ").includes(q))
    .filter(x => !group || (group === NO_GROUP ? !x.group : x.group === group));
  const report = STATE.data.imageUpdates;
  // Platform containers move on with the part they belong to, not one by one.
  const platformKeys = new Set(platform.map(w => `${w.ns}/${w.name}`));
  const updateCount = (report?.workloads || []).filter(w => w.available && !platformKeys.has(`${w.ns}/${w.name}`)).length;
  const updateErrors = report?.errors || 0;
  const unchecked = (report?.workloads || []).filter(w => w.unchecked && !w.homestead).length;
  const layout = viewLayout("containers");
  paint(`<div class="phead">
      <div><h2>Containers</h2><p>${rows.length} workload${rows.length === 1 ? "" : "s"}${q ? ` matching “${esc(q)}”` : ""}${group ? ` in ${esc(group === NO_GROUP ? "no group" : group)}` : ""} · ${platform.length
        ? `<a class="linkish" onclick="togglePlatformContainers()" data-tip="Homestead and the helpers it runs - updated under Settings › Updates - and KubeVirt, CDI and the like, run by their own operators and upgraded under System → Cluster">${platformShown() ? "hide" : "show"} ${platform.length} platform container${platform.length === 1 ? "" : "s"}</a>`
        : "system pods hidden"}${unchecked ? ` · <span data-tip="Marked ? in the list: stopped since Homestead started, so not yet compared with their registries">${unchecked} not checked yet</span>` : report && !updateCount && !updateErrors ? " · images current" : ""}</p>
        ${all.length ? workloadGroupBar(all, group) : ""}</div>
      <div class="row"><span class="dim xs scanprogress" id="scanprogress"></span>
      ${updateCount ? `<button class="pill warn pillbtn" title="Review and stage image updates" onclick="imageUpdateCenter()">${updateCount} update${updateCount === 1 ? "" : "s"}</button>` : ""}
      ${updateErrors ? `<button class="pill crit pillbtn" data-tip="${updateErrors} image${updateErrors === 1 ? "" : "s"} could not be compared with ${updateErrors === 1 ? "its" : "their"} registry; every other image was" onclick="imageUpdateCenter()">${updateErrors} <span class="hide-sm">check${updateErrors === 1 ? "" : "s"} </span>failed</button>` : ""}
      ${layoutSwitch("containers", "renderWorkloads")}
      ${moreMenu([{ label: "Check for image updates", icon: "refresh", run: "checkImageUpdates()", tip: "Ask the registries for newer images" },
        { label: workloadGroupNames(all).length ? "Groups" : "Group workloads", icon: "list", run: "manageWorkloadGroups()", need: "operator" },
        {label:layout === "cards" ? "Show as rows" : "Show as cards",run:`setViewLayout('containers','renderWorkloads',${jsq(layout === "cards" ? "rows" : "cards")})`},
        { label: "If a node fails", icon: "node", run: "wlFailover()", tip: "What each container does when its node fails: move, or wait for the node" }])}
      <button class="btn pri" data-need="operator" onclick="go('deploy')">＋ Deploy</button></div></div>

    ${rows.length ? workloadSections(rows, layout, group)
      : `<div class="empty">${q || group ? "Nothing matches that search." : "Nothing deployed yet."}</div>`}`);
}

/* An image with no tag is Docker's latest; saying so is clearer than leaving it off. */
function imageLabel(ref) {
  if (!ref || ref.includes("@")) return ref || "";
  return ref.split("/").pop().includes(":") ? ref : `${ref}:latest`;
}

/* Show what containerd will resolve without changing what the user entered. */
function imagePullRef(ref) {
  const tagged = imageLabel((ref || "").trim());
  if (!tagged) return "";
  const first = tagged.split("/")[0];
  const hasRegistry = tagged.includes("/") && (first.includes(".") || first.includes(":") || first === "localhost");
  if (hasRegistry) return tagged;
  return `docker.io/${tagged.includes("/") ? tagged : `library/${tagged}`}`;
}
function imagePullNote(ref) {
  const pull = imagePullRef(ref);
  if (!pull) return "";
  const leaf = pull.split("/").pop();
  const split = leaf.lastIndexOf(":");
  const repositoryLeaf = split >= 0 ? leaf.slice(0, split) : leaf;
  const tag = split >= 0 ? leaf.slice(split + 1) : "";
  const repeated = repositoryLeaf && repositoryLeaf === tag
    ? ` The repository is named <b>${esc(repositoryLeaf)}</b>; the final <b>:${esc(tag)}</b> is its tag.` : "";
  return `Will pull <span class="mono">${esc(pull)}</span>.${repeated}`;
}

/* What a platform container belongs to, and where that is upgraded. */
function platformTag(w) {
  if (w.homestead) return `<span class="pill slim info" data-tip="Part of Homestead, which keeps it in step. It is updated with Homestead under Settings › Updates, not as an app." onclick="homesteadPartManage('self')" style="cursor:pointer">Homestead</span>`;
  return `<span class="pill slim info" data-tip="Part of ${esc(w.platform)}, run by its operator, which puts back anything changed here. It is upgraded with ${esc(w.platform)} under System → Cluster → Platform versions." onclick="go('cluster')" style="cursor:pointer">${esc(w.platform)}</span>`;
}

/* The same actions for a card and a row: a row shows them as icons. */
function workloadActions(w, update, off, compact = false) {
  const ns = jsq(w.ns), name = jsq(w.name);
  const item = (label, run, iconName, options = {}) => ({label, run, icon:iconName, ariaLabel:`${label} ${w.name}`, ...options});
  const bar = items => actionBar(items, {label:`More actions for ${w.name}`,iconOnly:compact});
  const logs = item("Logs", `wlLogs(${ns},${jsq(w.pods?.[0]?.name || "")},${name})`, "log", {tip:"View live container logs"});
  const restart = item("Restart", `wlRestart(${ns},${name})`, "restart", {need:"operator",tip:"Replace every pod with a fresh one"});
  if (w.managed_smb || w.managed_nfs) return bar([logs, item("Network Shares", "go('shares')", "edit")]);
  if (w.homestead === "self" || w.homestead === "objectstore") return bar([logs, restart,
    item(w.homestead === "self" ? "Settings" : "Migration", `homesteadPartManage(${jsq(w.homestead)})`, "gear")]);
  if (w.platform) return bar([logs, restart]);
  return bar([
    update?.available && item("Update", `imageUpdateReview(${ns},${name})`, "update", {pri:true,need:"operator"}),
    logs,
    off ? item("Start", `wlScale(${ns},${name},1)`, "play", {need:"operator"}) : restart,
    !off && item("Stop", w.self ? `wlStopSelf(${ns},${name})` : `wlScale(${ns},${name},0)`, "stop", {need:"operator"}),
    item("Console", `wlConsole(${ns},${name})`, "console", {need:"operator",tip:"Open an audited shell in a running container"}),
    item("Edit", `wlEdit(${ns},${name})`, "edit", {need:"operator"}),
    FLEET.view?.linked && !w.self && item("Move to cluster", `moveToCluster('container',${name},${jsq(w.site?.handle || "")})`, "move", {need:"admin"}),
    (w.ports || []).length > 1 && item("Main port", `wlPrimaryPort(${ns},${name})`, "ext"),
    item("Placement", `wlPlacement(${ns},${name})`, "node"),
    item("Group", `wlGroup(${ns},${name})`, "list", {need:"operator"}),
    item("Move", `moveWorkload(${name},${ns})`, "move", {need:"operator"}),
    update?.can_rollback && item("Rollback", `imageRollback(${ns},${name})`, "rollback", {need:"operator"}),
    item("Delete", `wlDelete(${ns},${name})`, "trash", {danger:true,need:"operator"})
  ]);
}

function workloadCard(w) {
      const ok = w.ready === w.desired && w.desired > 0, off = w.desired === 0;
      const update = w.platform || w.managed_smb || w.managed_nfs || remoteRow(w) ? null : workloadUpdate(w.ns, w.name);
      const updateError = update?.images?.find(x => x.error);
  return `<div class="wcard card flat"${clusterAttr(w)}>
        <div class="between whead">
          <div class="row" style="gap:10px;min-width:0">
            ${appAvatar(w.name, w.icon)}
            <div class="wtitle"><div style="font-weight:680">${esc(w.name)}</div>
              <div class="dim xs">${esc(w.ns)} · ${w.managed_smb || w.managed_nfs ? esc(w.nodes.join(", ") || "unscheduled") : `<span class="nodelink"
                onclick="moveWorkload(${jsq(w.name)},${jsq(w.ns)})">${esc(w.nodes.join(", ") || "unscheduled")}</span>`}</div></div>
          </div>
          <div class="row">${w.platform ? platformTag(w) : ""}${w.managed_smb || w.managed_nfs ? `<span class="pill slim info" data-tip="Managed by Homestead under Network Shares">managed ${w.managed_nfs ? "NFS" : "SMB"}</span>` : ""}${update?.available ? '<span class="pill warn">update available</span>' : ""}${uncheckedMark(update)}
          ${updateError ? `<span class="tip warn-tip" tabindex="0" role="img" aria-label="Registry check unavailable: ${esc(updateError.error)}" data-tip="Registry check unavailable — ${esc(updateError.error)}">!</span>` : ""}
          <span class="pill ${ok ? "ok" : off ? "low" : "crit"}">${w.ready}/${w.desired}</span></div>
        </div>
        <div class="wmeta">
          <div><div class="dim xs">UPTIME</div>${w.uptime ? upChip(w.uptime) : '<span class="dim">—</span>'}</div>
          <div><div class="dim xs" data-tip="Live usage. 100% equals one fully used CPU core.">CPU</div><div class="mono small">${workloadCpuPercent(w.cpu)}</div></div>
          <div><div class="dim xs" data-tip="Memory in use right now">RAM</div><div class="mono small">${workloadMemory(w.mem_mb)}</div></div>
          <div><div class="dim xs">ACCESS</div><div class="waccess">${accessPorts(w.ports)}</div></div>
        </div>
        <div class="dim xs mono wimg"><span class="wimage-name">${w.images.map(i => esc(imageLabel(i))).join(" · ")}</span>
          <span class="wimage-hardware">${hardwareTags(w.hardware || (w.gpu ? ["igpu"] : []))}</span></div>
        ${workloadPull(w) ? pullBar(workloadPull(w)) : ""}
        ${workloadBlocked(w) ? `<div class="wblocked" title="${esc(workloadBlocked(w))}">${esc(workloadBlocked(w))}</div>` : ""}
        <div class="wfoot">
          ${workloadHierarchy(w)}
          <div class="row wacts">
          ${workloadActions(w, update, off)}
          </div>
        </div></div>`;
}

/* One line per workload: for a long list, or anyone who would rather scan than browse. */
function workloadTable(rows, sections = null, folded = new Set()) {
  // Grouped, each group is a body of its own under a heading body, so sorting
  // orders the rows within each group rather than mixing them.
  const bodies = sections ? sections.map(([name, members]) => {
    const shut = folded.has(name || NO_GROUP);
    return `<tbody class="grouphead"><tr><td colspan="7">${workloadGroupHead(name, members, shut)}</td></tr></tbody>
      <tbody${shut ? " hidden" : ""}>${workloadTableRows(members)}</tbody>`;
  }).join("") : `<tbody>${workloadTableRows(rows)}</tbody>`;
  return `<div class="card flat pad0 wltable-wrap"><table class="tbl dense stack compact wltable" data-sort="containers"><thead><tr>
    <th>Workload</th><th>Status</th><th class="wl-image">Image</th><th>CPU</th><th>RAM</th><th class="wl-access">Access</th><th data-nosort>Actions</th></tr></thead>
    ${bodies}</table></div>`;
}

/* An image not yet compared with its registry is one quiet mark, its reason
   on hover; the page subtitle gives the count. */
function uncheckedMark(update) {
  if (!update?.unchecked) return "";
  const tip = update.images?.some(i => i.starting)
    ? "Image not checked yet: still starting. It is compared with the registry once it runs."
    : "Image not checked yet: stopped, and not seen running here. It is compared with the registry once it has run.";
  return `<span class="tip unchecked-tip" tabindex="0" role="img" aria-label="${tip}" data-tip="${tip}">?</span>`;
}

const workloadExpanded = new Set();
const workloadRowKey = w => JSON.stringify([w.site?.id || "", w.ns, w.name]);
function workloadTableRows(rows) {
  return rows.map(w => {
    const ok = w.ready === w.desired && w.desired > 0, off = w.desired === 0;
    const key = workloadRowKey(w), id = `wl-detail-${encodeURIComponent(key)}`, open = workloadExpanded.has(key);
    const update = w.platform || w.managed_smb || w.managed_nfs || remoteRow(w) ? null : workloadUpdate(w.ns, w.name);
    const updateError = update?.images?.find(x => x.error), pull = workloadPull(w), blocked = workloadBlocked(w);
    const quiet = off && !update?.unchecked && !updateError && !update?.available && !pull && !blocked;
    const disclosure = collectionDisclosure({label:`Details for ${w.name}`,id:`wl-toggle-${encodeURIComponent(key)}`,controls:id,expanded:open,
      run:`workloadToggleDetails(${jsq(key)},this)`,bodyHtml:`${appAvatar(w.name, w.icon)}<span class="wtitle"><b>${esc(w.name)}</b> ${clusterTag(w)}</span>`});
    return `<tr class="wl-row${off ? " wl-off" : ""}${quiet ? " wl-quiet" : ""}${open ? " wl-expanded" : ""}" data-row-key="${esc(key)}"${clusterAttr(w)}>
      <td class="wl-name" data-sort="${esc(w.name)}">${disclosure}
        <div class="wl-row-meta dim xs">${w.platform ? `${platformTag(w)} ` : ""}${w.managed_smb || w.managed_nfs ? `<span class="pill slim info">managed ${w.managed_nfs ? "NFS" : "SMB"}</span> ` : ""}${esc(w.ns)} · ${esc(off ? "stopped" : (w.nodes || []).join(", ") || "unscheduled")}</div></td>
      <td class="wl-status" data-status data-sort="${off ? -1 : w.desired ? w.ready / w.desired : 0}"><div class="row wl-state-tags">
        <span class="pill slim wl-ready ${ok ? "ok" : off ? "low" : "crit"}" title="${w.ready} of ${w.desired} ready">${off ? "Stopped" : `${w.ready}/${w.desired}`}</span>
        ${update?.available ? '<span class="tag warn">Update</span>' : ""}${uncheckedMark(update)}
        ${updateError ? `<span class="tip warn-tip" tabindex="0" role="img" aria-label="Registry check unavailable: ${esc(updateError.error)}" data-tip="Registry check unavailable — ${esc(updateError.error)}">!</span>` : ""}
        ${pull ? `<span class="tag" title="Fetching ${esc(pull.image || "image")}">Pulling ${Math.min(100, pull.percent || 0)}%</span>` : ""}
        ${blocked ? `<span class="tag bad" data-tip="${esc(blocked)}">Blocked</span>` : ""}</div></td>
      <td class="wl-image" data-sm-hide><div class="mono xs wl-imagetext" title="${esc(w.images.map(imageLabel).join(" · "))}">${w.images.map(i => esc(imageLabel(i))).join(" · ")}</div>
        ${hardwareTags(w.hardware || (w.gpu ? ["igpu"] : []))}</td>
      <td class="mono small nowrap wl-cpu" data-sort="${off ? "" : w.cpu}" data-tip="Live usage. 100% equals one fully used CPU core.">${off ? "—" : workloadCpuPercent(w.cpu)}</td>
      <td class="mono small nowrap wl-ram" data-sort="${off ? "" : w.mem_mb}">${off ? "—" : workloadMemory(w.mem_mb)}</td>
      <td class="wl-access" data-sm-hide><div class="waccess">${accessPorts(w.ports)}</div></td>
      <td class="wl-actions" data-actions><div class="wacts">${workloadActions(w, update, off, true)}</div></td>
    </tr>
    <tr class="wl-detail-row" data-detail-for="${esc(key)}"${clusterAttr(w)}${open ? "" : " hidden"}><td colspan="7">
      <div class="wl-inline-detail" id="${esc(id)}">
        ${blocked ? `<div class="wblocked">${esc(blocked)}</div>` : ""}${pull ? pullBar(pull) : ""}
        <div class="about-grid">
          <div><span>Host</span><b>${esc((w.nodes || []).join(", ") || "Unscheduled")}</b></div>
          <div><span>Namespace · uptime</span><b>${esc(w.ns)} · ${esc(off ? "Stopped" : w.uptime ? fmtUp(w.uptime) : "Starting")}</b></div>
          <div class="wl-detail-image"><span>Image</span><b class="mono small">${w.images.map(esc).join("<br>") || "—"}</b><div class="wl-detail-hardware">${hardwareTags(w.hardware || (w.gpu ? ["igpu"] : []))}</div></div>
          <div class="wl-detail-access"><span>Access</span><b class="waccess">${accessPorts(w.ports)}</b></div>
        </div>
        ${workloadHierarchy(w)}
        <div class="wl-detail-actions">${workloadActions(w, update, off)}</div>
      </div></td></tr>`;
  }).join("");
}
window.workloadToggleDetails = (key, button) => {
  const open = !workloadExpanded.has(key);
  if (open) workloadExpanded.add(key); else workloadExpanded.delete(key);
  button.setAttribute("aria-expanded", String(open));
  const row = button.closest("tr");
  row?.classList.toggle("wl-expanded", open);
  const detail = row?.nextElementSibling;
  if (detail?.dataset.detailFor === key) detail.hidden = !open;
};


function accessPorts(ports) {
  const rows = ports || [];
  if (!rows.length) return '<span class="dim">—</span>';
  const shown = rows.slice(0, 2);
  // A narrow card has room for one port, a wide one for two; each size gets
  // its own "+N" so the count is right whichever one is showing.
  const more = (from, kind) => rows.length > from
    ? `<span class="tag more ${kind}" data-tip="Also listening on ${esc(rows.slice(from).map(p => p.port).join(", "))}">+${rows.length - from}</span>` : "";
  return shown.map((p, i) => (p.ip
    ? `<span class="plink${i ? " second" : ""}" title="Open ${esc(svcUrl(p.ip, p.port))}" onclick="openSvc(${jsq(p.ip)},${p.port})">${p.port}<svg class="ext" width="9" height="9"><use href="#i-ext"/></svg></span>`
    : `<span class="tag${i ? " second" : ""}">${p.port}</span>`)).join("") +
    more(1, "more-narrow") + more(2, "more-wide");
}

/* Where a workload's pods would go, host by host: memory now and after the
   start, what the scheduler has already reserved, and why a host is out.
   Shared by the start review and anything else that places pods. */
function capacityPlacementHtml(plan) {
  const placement = plan.placement || {}, hosts = (plan.candidates || []).filter(host => host.eligible);
  const resident = placement.resident || plan.vm?.resident_node;
  let title, detail;
  if (resident) {
    title = `Resumes on ${resident}`;
    detail = "The paused VM keeps its current host.";
  } else if (placement.pinned) {
    title = `Required host: ${placement.pinned}`;
    detail = "This workload is pinned to this host. It cannot launch on another host.";
  } else if (placement.preferred) {
    const available = hosts.some(host => host.name === placement.preferred);
    title = `${available ? "Preferred host" : "Preferred host currently unavailable"}: ${placement.preferred}`;
    detail = "Kubernetes can choose another eligible host if needed.";
  } else if (hosts.length === 1) {
    title = `Only eligible host: ${hosts[0].name}`;
    detail = "Kubernetes can schedule it here if these constraints still hold at launch.";
  } else {
    title = hosts.length ? "Host selected at launch" : "Launch host not determined";
    detail = hosts.length ? "Kubernetes chooses from the eligible hosts below when the workload starts." : "No eligible host has been confirmed by this review.";
  }
  if (plan.blocked) detail += " Launch is blocked until the issues below are resolved.";
  const names = hosts.length > 1 || placement.preferred ? `<p>Eligible hosts: <b>${hosts.map(host => esc(host.name)).join(", ") || "none"}</b>.</p>` : "";
  return `<div class="capacity-placement">${UI.section("Launch host", `<b>${esc(title)}</b><p class="ui-help">${esc(detail)}</p>${names}`)}</div>`;
}
window.capacityPlacementHtml = capacityPlacementHtml;

function capacityHosts(candidates, { unavailable = "live RAM unavailable", reserved = "Already reserved", placement = {} } = {}) {
  const eligible = candidates.filter(x => x.eligible);
  const rejected = candidates.filter(x => !x.eligible);
  const memory = host => {
    if (!host.metrics_available || host.projected_percent === null || host.projected_percent === undefined) return `<span class="ui-help">${esc(unavailable)}</span>`;
    const now = host.capacity_gb ? host.used_gb / host.capacity_gb * 100 : 0;
    return `${UI.meter({ now, after: host.projected_percent, label: `${host.name} memory` })}
      <span class="sub">${esc(host.projected_gb)} / ${esc(host.capacity_gb)} GiB after start · ${esc(host.projected_percent)}%</span>`;
  };
  const about = host => `<span class="mono">${esc(host.name)}</span><span class="sub">${host.metrics_available ? `Live RAM ${esc(host.used_gb)} GiB` : esc(unavailable)}${host.reservations_known
    ? ` · ${esc(reserved)}: ${esc(host.reserved_gb)} / ${esc(host.allocatable_gb)} GiB RAM · ${esc(host.reserved_cpu_percent)}% CPU`
    : " · Scheduler reservations unavailable."}</span>`;
  const status = host => (placement.resident === host.name ? UI.chip("Current host", "info")
    : placement.pinned === host.name ? UI.chip("Pinned", "info")
    : placement.preferred === host.name ? UI.chip("Preferred", "info") : UI.chip("Eligible", ""))
    + (host.warnings?.length ? UI.chip("Warning", "warn") : "");
  let html = "";
  if (eligible.length) html += UI.section("Hosts that can run it", UI.table(
    [{ label: "Host" }, { label: "Memory", className: "grow" }, { label: "" }],
    eligible.map(host => [about(host), memory(host), status(host)])));
  if (rejected.length) html += UI.section("Unavailable for the next pod", UI.table(
    [{ label: "Host" }, { label: "Why" }],
    rejected.map(host => [`<span class="mono">${esc(host.name)}</span>`, esc((host.reasons || []).join(" · ") || "not eligible")])));
  return html;
}
window.capacityHosts = capacityHosts;

window.wlScale = async (ns, name, n) => {
  if (n > 0) {
    try {
      const plan = await api(`/api/workloads/start-plan?${new URLSearchParams({ ns, name, replicas: n })}`);
      if (plan.requires_confirmation || plan.blocked) {
        const pods = `${plan.additional} pod${plan.additional === 1 ? "" : "s"}`;
        const topology = plan.topology_status && plan.topology_status !== "not-needed"
          ? plan.topology_status === "fits" ? "A scheduling order fits the checked pod affinity and spread rules in this snapshot."
            : plan.topology_status === "blocked" ? "The checked pod affinity and spread rules cannot fit all requested replicas."
            : "Pod affinity and spread placement remains unverified." : "";
        const concerns = [...(plan.warnings || []),
          ...(plan.unbounded?.length ? [`${plan.unbounded.join(", ")} ${plan.unbounded.length === 1 ? "has" : "have"} no memory limit, so actual use could exceed this estimate`] : []),
          ...(topology && plan.topology_status !== "fits" ? [topology] : [])];
        const concernHtml = concerns.length ? `<ul class="ui-list">${concerns.map(c => `<li>${esc(c)}</li>`).join("")}</ul>` : "";
        const slots = (plan.candidates || []).filter(x => x.eligible && x.reservations_known && x.request_slots !== null && x.request_slots !== undefined)
          .map(x => `${esc(x.name)}: ${esc(x.request_slots)} additional pod(s)`);
        modal(`Start ${name}?`, [
          UI.lead(`Starting ${pods} of <b>${esc(name)}</b>. ${plan.blocked ? "There is not room for it on any host that may run it." : "It fits, but check where it would land first."}`),
          plan.blocked
            ? UI.callout("bad", "Not enough eligible capacity for the requested replicas.", concernHtml)
            : UI.callout("warn", "Placement and memory need review.", concernHtml),
          capacityPlacementHtml(plan),
          capacityHosts(plan.candidates || [], { placement: plan.placement }),
          UI.facts([
            ["Each pod requests", `${esc(plan.pod_request_gb ?? "unknown")} GiB RAM · ${esc(plan.pod_cpu_request_percent ?? "unknown")}% CPU`],
            ["Estimated peak memory", plan.pod_memory_gb ? `${esc(plan.pod_memory_gb)} GiB` : "unknown"],
          ]),
          UI.more("How this is estimated", `
            <p>Requests reserve scheduler capacity (100% CPU is one core); limits bound container usage. Neither is the same as live usage. The peak estimate includes init and sidecar containers and overhead.</p>
            <p>Projection uses the greater of live RAM and existing reservations, plus the estimated new pods that fit each host. This is a snapshot, not a reservation or an OOM guarantee; competing starts, storage and other scheduler constraints can change placement.</p>
            ${slots.length ? `<p>Resource, port and storage upper bound - ${slots.join(" · ")}; topology may reduce this.</p>` : ""}
            ${topology ? `<p>${esc(topology)}</p>` : ""}`),
          plan.blocked ? "" : UI.ack("wl_capacity_ok", "Proceed despite capacity warnings: I accept the placement and memory risks"),
          UI.actions(plan.blocked ? UI.cancel("Close")
            : UI.cancel() + UI.button("Start anyway", `wlScaleGo(${jsArg(ns)},${jsArg(name)},${n},true)`, { kind: "danger" })),
        ].join(""));
        return;
      }
    } catch (e) { return toast(`Could not check node memory: ${e.message}`, "bad"); }
  }
  return wlScaleGo(ns, name, n, false);
};
window.wlScaleGo = async (ns, name, n, confirmed = false) => {
  if (confirmed && !$("#wl_capacity_ok")?.checked) return toast("confirm the memory warning first", "bad");
  try {
    await api("/api/scale", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ ns, name, replicas: n, confirm_capacity: confirmed }) });
    if (confirmed) closeModal();
    toast(`${name} ${n ? "started" : "stopped"}`, "ok"); setTimeout(() => refresh(true), 900);
  } catch (e) { toast(e.message, "bad"); }
};
/* Homestead stopping itself takes this page with it, and nothing here can
   start it again - so it is said plainly, with restart offered instead. */
window.wlStopSelf = (ns, name) => {
  modal("Stop Homestead?", `
    <div class="note bad"><b>This takes Homestead down, and this page with it.</b> Nothing here can start it again:
      it stays down until someone runs this on the cluster -
      <pre class="mono xs" style="white-space:pre-wrap;margin:8px 0">kubectl -n ${esc(ns)} scale deployment/${esc(name)} --replicas=1</pre>
      Your apps keep running; jobs in the tray, alerts and moves pause until it is back.</div>
    <p class="small">If it needs a fresh start, <b>Restart</b> brings it straight back.</p>
    <label class="switch"><input type="checkbox" id="ss_ok"> I understand - stop it</label>
    <div class="row" style="margin-top:14px"><button class="btn pri" onclick="closeModal();wlRestart(${jsq(ns)},${jsq(name)})">Restart instead</button>
      <button class="btn danger" onclick="wlStopSelfGo(${jsq(ns)},${jsq(name)})">Stop Homestead</button>
      <button class="btn" onclick="closeModal()">Cancel</button></div>`);
};
window.wlStopSelfGo = async (ns, name) => {
  if (!$("#ss_ok").checked) return toast("Tick the box to confirm", "bad");
  try {
    await api("/api/scale", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ ns, name, replicas: 0, confirm_self: true }) });
    closeModal(); toast("Homestead is stopping", "ok");
  } catch (e) { toast(e.message, "bad"); }
};

/* Every container's behaviour when its node fails, in one list. Longhorn
   decides whether a moved container's single-node volume can follow it, so
   its policy is shown here too. */
window.wlFailover = async () => {
  modal("If a node fails", '<div class="empty"><span class="spin2"></span></div>', true);
  const [rows, lh] = await Promise.all([api("/api/workloads").catch(() => STATE.data.wl || []),
    api("/api/longhorn/capacity").catch(() => null)]);
  const policy = lh?.node_down || "";
  const moving = policy && policy !== "do-nothing";
  $("#mbody").innerHTML = `
    <p class="small" style="margin-top:0">When a node stops answering, each container either moves to another node, waits for its node to
      come back, or follows Kubernetes' default of five minutes. Changing a container restarts it.</p>
    ${lh ? `<div class="note ${moving ? "good" : "warn"}">${moving
      ? `Longhorn lets go of a failed node's volumes (<span class="mono">${esc(policy)}</span>), so a container moving to another node takes its volume with it.`
      : `<b>A container with a single-node volume cannot really move yet.</b> Longhorn keeps its volume attached to the dead node, so on the
         new node it waits until the old one is back. <button class="btn sm pri" data-need="admin" onclick="wlFailoverPolicy()" style="margin-top:6px">Let Longhorn release them</button>`}</div>` : ""}
    <div class="row" style="margin:12px 0;gap:6px;flex-wrap:wrap"><span class="small dim">Set all to</span>
      ${Object.entries(FAILOVER_WORDS).map(([v, l]) => `<button class="btn sm" onclick="$$('#mbody select[data-fo]').forEach(s => s.value=${jsq(v)})">${esc(l)}</button>`).join("")}</div>
    <table class="tbl dense stack"><thead><tr><th>Container</th><th>If its node fails</th></tr></thead><tbody>
      ${rows.map(w => `<tr><td><b>${esc(w.name)}</b> <span class="dim xs">${esc(w.ns)}${w.group ? ` · ${esc(w.group)}` : ""}</span>
          ${(w.hardware || []).length ? `<span class="tag hw" data-tip="Tied to hardware on its host">${esc(w.hardware.join(", "))}</span>` : ""}</td>
        <td data-label="If its node fails">${failoverSelect(`fo_${w.ns}_${w.name}`, w.failover || "default", `data-fo data-ns="${esc(w.ns)}" data-name="${esc(w.name)}" data-was="${esc(w.failover || "default")}"`)}</td></tr>`).join("")}
    </tbody></table>
    <div class="row" style="margin-top:14px"><button class="btn pri" data-need="operator" onclick="wlFailoverSave()">Save changes</button>
      <button class="btn" onclick="closeModal()">Cancel</button></div>`;
  if (window.applyRole) applyRole();
};
window.wlFailoverSave = async () => {
  const items = $$("#mbody select[data-fo]").filter(s => s.value !== s.dataset.was)
    .map(s => ({ ns: s.dataset.ns, name: s.dataset.name, mode: s.value }));
  if (!items.length) return closeModal();
  if (items.some(i => (STATE.data.wl || []).some(w => w.self && w.ns === i.ns && w.name === i.name))
      && !(await ask("Homestead itself is among them: it restarts, and this page reconnects when it is back."))) return;
  try {
    const r = await api("/api/workloads/failover", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ items }) });
    toast(r.detail, "ok"); closeModal(); setTimeout(() => refresh(true), 900);
  } catch (e) { toast(e.message, "bad"); }
};
window.wlFailoverPolicy = async () => {
  if (!(await ask("Let Longhorn delete the pods of a node that stops answering, so their volumes can attach elsewhere?\n\nThis is Longhorn's \"Pod Deletion Policy When Node is Down\" set to delete-both-statefulset-and-deployment-pod."))) return;
  try {
    await api("/api/longhorn/settings", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ node_down: "delete-both-statefulset-and-deployment-pod" }) });
    toast("Longhorn now lets go of a failed node's volumes", "ok"); wlFailover();
  } catch (e) { toast(e.message, "bad"); }
};

/* A container with an address of its own on the LAN: a second interface on
   a bridged VM network, beside its pod network, so it answers there directly
   without a Service - what an app that wants to be found on the LAN needs. */
async function containerLanFields(p, current) {
  const opts = window.__vmCreateOptions || await api("/api/vm/create-options").catch(() => ({}));
  window.__vmCreateOptions = opts;
  const lan = vmLanNetworks(opts);
  if (!lan.length) return vmNetworkNote(opts);
  return `<div class="f"><label>LAN network ${tip("Its bridge and VLAN; the container joins it as a second interface, lan0, and keeps the pod network for everything else.")}</label>
      <select id="${p}_net">${lan.map(n => `<option value="${esc(n.name)}" ${current?.network === n.name ? "selected" : ""}>${esc(n.name)}${n.vlan ? ` (VLAN ${esc(n.vlan)})` : ""}</option>`).join("")}</select></div>
    ${vmAddressFields(p, opts)}
    <div class="dim xs">It answers on this address directly - no Service or VIP - and it is recorded under the container's name in IP addresses.</div>`;
}
function containerLanRead(p) {
  return Object.assign(vmReadAddress(p), { network: $(`#${p}_net`)?.value || "", address: ($(`#${p}_ip`)?.value || "").trim() });
}
window.containerLanFields = containerLanFields;
window.containerLanRead = containerLanRead;
window.deployLanChanged = async () => {
  const lan = $("#d_net")?.value === "lan", box = $("#d_lan_box");
  if (!box) return;
  box.hidden = !lan;
  const vip = $("#d_vip_mode")?.closest(".f");
  if (vip) vip.hidden = lan;
  if ($("#d_vip_wrap")) $("#d_vip_wrap").hidden = lan || $("#d_vip_mode").value !== "manual";
  if (lan && !box.dataset.filled) {
    box.innerHTML = await containerLanFields("dl", DCFG.lan);
    box.dataset.filled = "1";
    if ($("#dl_subnet")) vmSubnetPicked("dl");
    if (DCFG.lan?.address) $("#dl_ip").value = DCFG.lan.address;
  }
};

window.wlRestart = async (ns, name) => {
  try {
    await api("/api/restart", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ ns, name }) });
    toast(`${name} restarting`, "ok"); setTimeout(() => refresh(true), 1300);
  } catch (e) { toast(e.message, "bad"); }
};
window.wlDelete = async (ns, name) => {
  modal("Delete · " + name, '<div class="empty"><span class="spin2"></span>checking what this removes</div>', true);
  // The card list alone cannot say which Services or claims belong to this
  // workload, and deleting it removes the first and keeps the second.
  const [network, vols] = await Promise.all([
    STATE.data.network ? Promise.resolve(STATE.data.network) : api("/api/network").catch(() => ({ services: [] })),
    STATE.data.vols ? Promise.resolve(STATE.data.vols) : api("/api/volumes").catch(() => []),
  ]);
  const workload = (STATE.data.wl || []).find(row => row.ns === ns && row.name === name) || {};
  const services = (network.services || [])
    .filter(row => row.namespace === ns && (row.targets || []).includes(name));
  const listeners = services.flatMap(row => (row.external_ips || [])
    .flatMap(ip => (row.ports || []).map(port => `${ip}:${port.port}/${port.protocol}`)));
  const claims = (vols || []).filter(vol => (vol.attached || []).includes(name));
  $("#mbody").innerHTML = `
    <div class="note dependency-danger"><b>This removes the workload, not its data.</b>
      The Deployment and every Service pointing at it are deleted. Persistent volumes are kept and can be
      removed afterwards from Volumes.</div>
    <div class="dependency-list" style="margin-top:12px">
      <div class="dependency-row"><span>Deployment</span><b class="mono">${esc(ns)}/${esc(name)}</b></div>
      <div class="dependency-row"><span>Containers</span><b>${(workload.images || []).length || 1}</b></div>
      <div class="dependency-row ${services.length ? "stranded" : ""}"><span>Services removed</span>
        <b>${services.length ? services.map(row => esc(row.name)).join(", ") : "none"}</b></div>
      ${listeners.length ? `<div class="dependency-row stranded"><span>LAN listeners freed</span>
        <b class="mono">${listeners.map(esc).join(", ")}</b></div>` : ""}
      <div class="dependency-row"><span>Volumes kept</span>
        <b>${claims.length ? claims.map(vol => esc(vol.pvc_name || vol.name)).join(", ") : "none attached"}</b></div>
    </div>
    <div class="f" style="margin-top:16px"><label>Type <b class="mono">${esc(name)}</b> to confirm</label>
      <input id="wd_confirm" autocomplete="off" placeholder="${esc(name)}" oninput="wlDeleteGate(${jsq(name)})"></div>
    <div class="row"><button class="btn danger" id="wd_go" data-need="operator" disabled
      onclick="wlDeleteNow(${jsq(ns)},${jsq(name)},this)">Delete workload</button>
      <button class="btn" onclick="closeModal()">Cancel</button></div>`;
  if (window.applyRole) window.applyRole();
};
window.wlDeleteGate = name => {
  const button = $("#wd_go"), input = $("#wd_confirm");
  if (button && input) button.disabled = input.value.trim() !== name;
};
window.wlDeleteNow = async (ns, name, button) => {
  if (button) { button.disabled = true; button.textContent = "Deleting…"; }
  try {
    const result = await api(`/api/workload/${ns}/${name}`, { method: "DELETE" });
    const freed = (result.services || []).length;
    closeModal();
    toast(`${name} deleted${freed ? ` with ${freed} service${freed === 1 ? "" : "s"}` : ""}`, "ok");
    setTimeout(() => refresh(true), 900);
  } catch (e) {
    if (button) { button.disabled = false; button.textContent = "Delete workload"; }
    toast(e.message, "bad");
  }
};
/* Logs for one container of one pod. A workload with several replicas, or a
   pod with sidecars, gets pickers saying exactly whose output this is. */
function openLogs(title, path, pods = null, ns = "") {
  if (window.__logTimer) clearInterval(window.__logTimer);
  const LOG = window.__logView = { path, pods: pods || [], ns };
  const multi = LOG.pods.length > 1 || LOG.pods.some(p => (p.containers || []).filter(c => c.kind === "app").length > 1);
  modal("Logs · " + title, `<div class="logtools">
      ${multi ? `<div class="logpick"><label>Pod <select id="logPod" onchange="logPodChanged()">${LOG.pods.map(p =>
        `<option value="${esc(p.name)}">${esc(p.name)} · ${esc(p.node || "unscheduled")}${p.ready ? "" : " · not ready"}</option>`).join("")}</select></label>
        <label>Container <select id="logContainer" onchange="logTargetChanged()"></select></label></div>` : ""}
      <span id="logstate"><span class="spin2"></span> connecting</span>
      <label class="switch"><input type="checkbox" id="logfollow" checked> Follow latest</label></div>
    <div class="dim xs mono logsource" id="logSource"></div>
    <pre class="logview">waiting for log output…</pre>`, true);
  if (multi) logPodChanged(); else logShowSource();
  const poll = async () => {
    if ($("#modal").classList.contains("hidden")) return clearInterval(window.__logTimer);
    try {
      const t = await api(LOG.path + (LOG.path.includes("?") ? "&" : "?") + "tail=500");
      const pre = $("#mbody .logview"); if (!pre) return;
      pre.textContent = t || "(the container is running but has not written any logs yet)";
      $("#logstate").innerHTML = '<span class="ld"></span> live · refreshes every 2s';
      if ($("#logfollow")?.checked) pre.scrollTop = pre.scrollHeight;
    } catch (e) {
      const pre = $("#mbody .logview"); if (pre) pre.textContent = e.message;
      if ($("#logstate")) $("#logstate").innerHTML = '<span class="cd badbg"></span> no logs';
    }
  };
  LOG.poll = poll;
  poll(); window.__logTimer = setInterval(poll, 2000);
}

window.logPodChanged = () => {
  const LOG = window.__logView, pod = LOG.pods.find(p => p.name === $("#logPod")?.value);
  const apps = (pod?.containers || []).filter(c => c.kind === "app");
  $("#logContainer").innerHTML = apps.map(c => `<option value="${esc(c.name)}">${esc(c.name)} · ${esc(c.state || "unknown")}</option>`).join("");
  $("#logContainer").closest("label").style.display = apps.length > 1 ? "" : "none";
  logTargetChanged();
};

window.logTargetChanged = () => {
  const LOG = window.__logView, pod = $("#logPod")?.value, container = $("#logContainer")?.value;
  LOG.path = `/api/logs?ns=${encodeURIComponent(LOG.ns)}&pod=${encodeURIComponent(pod)}${container ? `&container=${encodeURIComponent(container)}` : ""}`;
  const pre = $("#mbody .logview"); if (pre) pre.textContent = "waiting for log output…";
  logShowSource();
  if (LOG.poll) LOG.poll();
};

function logShowSource() {
  const LOG = window.__logView, host = $("#logSource");
  if (!host) return;
  const params = new URLSearchParams(LOG.path.split("?")[1] || "");
  const pod = LOG.pods.find(p => p.name === params.get("pod"));
  host.textContent = params.get("pod") ? `pod ${params.get("pod")}${pod?.node ? ` on ${pod.node}` : ""}${params.get("container") ? ` · container ${params.get("container")}` : ""}` : "";
}

/* A scan asks a registry about every workload in turn, so it is neither
   instant nor evenly paced. The server counts as it goes; this reads that
   count so the wait shows its work instead of going quiet. */
window.checkImageUpdates = async () => {
  const button = typeof event !== "undefined" ? event.currentTarget : null;
  if (button) { button.disabled = true; button.textContent = "Checking…"; }
  const host = $("#scanprogress");
  if (host) host.innerHTML = '<span class="spin2"></span> asking the registries…';
  let watching = true;
  const watch = async () => {
    while (watching) {
      await new Promise(r => setTimeout(r, 500));
      if (!watching) break;
      const at = await api("/api/image-updates/scan-progress", { keep: true }).catch(() => null);
      if (!at || !at.total) continue;
      const done = Math.min(at.done, at.total);
      if (button && at.running) button.textContent = `Checking ${done}/${at.total}…`;
      const bar = $("#scanprogress");
      if (!bar) continue;
      bar.innerHTML = at.running
        ? `<span class="spin2"></span> checked ${done} of ${at.total}`
          + (at.current ? ` · ${esc(at.current)}` : "")
          + (at.updates ? ` · <b>${at.updates} update${at.updates === 1 ? "" : "s"} so far</b>` : "")
        : "";
    }
  };
  watch();
  try {
    const report = await loadImageUpdates(true, false);
    if (!report) return;      // the failure has been said already
    const updates = HomesteadUpdateState.availableWorkloads(report).length;
    const errors = report?.errors || 0;
    toast(updates ? `${updates} update${updates === 1 ? "" : "s"} available${errors ? `; ${errors} image${errors === 1 ? "" : "s"} could not be checked` : ""}`
      : errors ? `no updates found; ${errors} image${errors === 1 ? "" : "s"} could not be checked`
      : "every image is up to date", updates ? "ok" : errors ? "warn" : "ok");
    if (report && STATE.view === "workloads") renderWorkloads();
  } catch (e) {
    toast(e.message, "bad");
  } finally {
    watching = false;
    if (button) { button.disabled = false; button.innerHTML = '↻ Check<span class="hide-sm"> images</span>'; }
    const bar = $("#scanprogress");
    if (bar) bar.innerHTML = "";
  }
};

/* An image reference short enough to read: its last path part, and the start
   of a digest. The whole reference stays in the title and in the details. */
function shortImage(ref) {
  const text = String(ref || "");
  const [name, digest] = text.split("@");
  const tail = name.split("/").pop() || name;
  return digest ? `${tail} · ${digest.replace(/^sha256:/, "").slice(0, 12)}` : tail;
}

/* The part of an image that changes: a tag, or the start of a digest. */
function imageVersion(ref, tag = "") {
  const [name, digest] = String(ref || "").split("@");
  const tail = name.split("/").pop() || name;
  // A tag in the reference, else the release Homestead tracks for it; an
  // image pinned only to its digest has none to show.
  const own = tail.includes(":") ? tail.slice(tail.lastIndexOf(":") + 1) : digest ? "" : "latest";
  return { tag: tag || own, short: digest ? digest.replace(/^sha256:/, "").slice(0, 7) : "" };
}
/* What a person reads for an image change: the release when it changes
   (2.8.200 → 2.8.205), and the start of the digest only when the release
   stays the same - latest → latest is a different build, not no change. */
function imageChangeWords(before, after, beforeTag = "", afterTag = "") {
  const was = imageVersion(before, beforeTag), now = imageVersion(after, afterTag);
  if (was.tag && now.tag && was.tag !== now.tag) return [was.tag, now.tag];
  const word = v => (v.tag && v.short ? `${v.tag} · ${v.short}` : v.tag || v.short || "?");
  return [word(was), word(now)];
}
window.imageChangeWords = imageChangeWords;

/* The same warning for several apps is said once, with the apps it is about. */
function groupedConcerns(rows) {
  const byText = new Map();
  rows.forEach(({config, preview}) => (preview.capacity.blocked ? preview.capacity.warnings || [] : capacityNotes(preview.capacity).concerns).forEach(text => {
    if (!byText.has(text)) byText.set(text, []);
    byText.get(text).push(config.name);
  }));
  return [...byText].map(([text, names]) => rows.length > 1
    ? `${text.replace(/\.$/, "")} - ${names.length === rows.length ? `all ${rows.length} apps` : names.join(", ")}` : text);
}

let IMAGE_REVIEW = null, IMAGE_REVIEW_SEQUENCE = 0;
/* An update can be for a linked cluster (the Homestead updates dialog
   updates them too): its requests carry that cluster, and its rollout is
   keyed by it, since the same app can run on two clusters. */
const clusterHeaders = item => item?.cluster ? { "X-Homestead-Cluster": item.cluster } : {};
const rolloutKey = item => `${item?.cluster ? `${item.cluster}|` : ""}${updateKey(item.ns, item.name)}`;
const rolloutBody = config => { const { cluster, clusterName, part, ...rest } = config; return rest; };
// Homestead replacing itself: its API is away for a minute, which is not a failure.
const restartsHomestead = item => item?.part === "self" || (!item?.part && item?.name === "homestead");
/* Whether someone else changed the workload while this rollout was watched:
   replaced, or its pod template no longer this rollout's - stamp or images.
   A change outside the template leaves the pods as they are: Homestead,
   starting after its own update, sets its rollout strategy to what its data
   volume allows, and that raised the generation and stopped the queue. */
const rolloutChanged = (accepted, state) => state.uid !== accepted.uid || (state.generation !== accepted.generation
  && (!accepted.rollout_at || state.rollout_at !== accepted.rollout_at
      || JSON.stringify(state.images || {}) !== JSON.stringify(accepted.images || {})));
window.rolloutChanged = rolloutChanged;

async function reviewImageActions(items, action = "update") {
  const sequence = ++IMAGE_REVIEW_SEQUENCE;
  IMAGE_REVIEW = null;
  const rollback = action === "rollback";
  const count = items.length;
  modal(rollback ? "Review rollback" : count > 1 ? `Update ${count} apps` : "Review image update",
    '<div id="imageReviewLoading" class="empty"><span class="spin2"></span> Checking exact images and rollout capacity…</div>', true);
  try {
    const rows = [];
    for (const item of HomesteadUpdateState.orderApply(items)) {
      const config = {ns: item.ns, name: item.name, action, approved: true,
        ...(item.cluster ? {cluster: item.cluster, clusterName: item.clusterName || item.cluster} : {}),
        ...(item.part ? {part: item.part} : {})};
      const preview = await api("/api/image-updates/preview", {method: "POST",
        headers: {"Content-Type": "application/json", ...clusterHeaders(config)}, body: JSON.stringify(rolloutBody(config))});
      if (sequence !== IMAGE_REVIEW_SEQUENCE || !$("#imageReviewLoading")) return;
      if (!preview.capacity || !preview.capacity_token || !Array.isArray(preview.images)) throw new Error("Image capacity review unavailable; no update was started");
      rows.push({config, preview});
    }
    if (!rows.length) throw new Error("Select at least one image change");
    IMAGE_REVIEW = rows;
    const blocked = rows.some(row => row.preview.capacity.blocked);
    const many = rows.length > 1;
    const concerns = groupedConcerns(rows);
    const list = concerns.length ? `<ul class="ui-list">${concerns.map(c => `<li>${esc(c)}</li>`).join("")}</ul>` : "";
    // One line per app: its name, and what its image moves from and to.
    const apps = rows.map(({config, preview}) => {
      const flagged = preview.capacity.blocked || capacityNotes(preview.capacity).concerns.length;
      const change = preview.images.map(i => `<span class="upd-change" title="${esc(i.before)} → ${esc(i.after)}">${preview.images.length > 1 ? `${esc(i.container)} ` : ""}${(([was, now]) => `<code>${esc(was)}</code> → <code>${esc(now)}</code>`)(imageChangeWords(i.before, i.after, i.before_tag, i.after_tag))}</span>`).join("");
      return `<li><span class="upd-name">${flagged ? `<span class="upd-flag ${preview.capacity.blocked ? "bad" : "warn"}" title="See the notes above">!</span>` : ""}<b>${esc(config.name)}</b> <span class="dim">${config.clusterName ? `${esc(config.clusterName)} · ` : ""}${esc(config.ns)}</span></span>${change}</li>`;
    }).join("");
    $("#mbody").innerHTML = `<div class="update-review ui-stack">
      <p class="ui-lead">${many ? "Apps update one at a time, with Homestead last. " : ""}${items.some(restartsHomestead) ? "Homestead will be briefly unavailable while it restarts. " : ""}${many ? "Each app restarts" : "The app restarts"} to ${rollback ? "return to its previous image" : "use the new image"}.</p>
      ${blocked ? UI.callout("bad", "Placement blocks this change.", `${list}<p>Resolve the placement blockers before starting.</p>`)
        : concerns.length ? UI.callout("warn", "", list) : ""}
      <ul class="upd-apps">${apps}</ul>
      ${UI.more("Details: capacity, exact images, how it runs", `
        ${rows.map(({config, preview}) => `${many ? `<p><b>${esc(config.ns)}/${esc(config.name)}</b></p>` : ""}
          ${UI.facts(preview.images.flatMap(i => [[`${i.container} now`, `<code>${esc(i.before)}</code>`],
            [`${i.container} ${rollback ? "back to" : "new"}`, `<code>${esc(i.after)}</code>`], [`${i.container} recovery`, `<code>${esc(i.rollback)}</code>`]]))}
          ${deployCapacityHtml(preview.capacity, false, true)}`).join("")}
        <p>Exact image digests and full-pod capacity are checked again before each change. Failure, lost contact or an expired review stops the remaining queue.
        Closing this dialog stops unstarted updates; a rollout already submitted continues.</p>`, blocked)}
      ${UI.actions(UI.cancel() + UI.button(rollback ? "Start rollback" : many ? `Update ${rows.length}` : "Update", "imageReviewedApply()", { kind: "pri", id: "imageCapacityApply", disabled: true }),
        blocked ? "" : `<label class="upd-ok"><input type="checkbox" id="imageCapacityApprove" onchange="imageReviewReady()"> ${concerns.length ? "Accept the restart and the notes above" : "Accept the restart"}</label>`)}
    </div>`;
  } catch (error) {
    IMAGE_REVIEW = null;
    if (sequence === IMAGE_REVIEW_SEQUENCE && $("#imageReviewLoading")) $("#mbody").innerHTML = UI.callout("bad", "Review could not finish.", `${esc(error.message)}. Nothing was changed. Close this dialog and review again.`) + UI.actions(UI.cancel("Close"));
  }
}
window.imageReviewReady = () => {
  const ready = !!IMAGE_REVIEW?.length && !IMAGE_REVIEW.some(r => r.preview.capacity.blocked) && $("#imageCapacityApprove")?.checked;
  if ($("#imageCapacityApply")) $("#imageCapacityApply").disabled = !ready;
  return !!ready;
};
window.imageUpdateReview = (ns, name) => reviewImageActions([{ns, name}]);
window.imageUpdateApply = () => imageReviewedApply();
window.imageReviewedApply = async () => {
  if (!imageReviewReady()) return toast("Review and acknowledge the image and capacity changes first", "bad");
  const rows = IMAGE_REVIEW; IMAGE_REVIEW = null;
  const sequence = ++IMAGE_REVIEW_SEQUENCE;
  const items = rows.map(r => r.config);
  const states = Object.fromEntries(items.map(item => [rolloutKey(item), {phase: "queued", ready: 0, desired: 1}]));
  const failures = [];
  modal("Reviewed image rollouts", '<div id="imageQueue"></div>', true);
  const active = () => sequence === IMAGE_REVIEW_SEQUENCE && $("#imageQueue") && !$("#modal").classList.contains("hidden");
  const paint = () => {if (active()) $("#imageQueue").innerHTML = batchUpdateMarkup(items, states, failures, false, true);};
  paint();
  for (let index = 0; index < rows.length; index++) {
    const {config, preview} = rows[index], key = rolloutKey(config);
    if (!active()) break;
    try {
      const result = await api(config.action === "rollback" ? "/api/image-updates/rollback" : "/api/image-updates/apply",
        {method: "POST", headers: {"Content-Type": "application/json", ...clusterHeaders(config)},
         body: JSON.stringify({...rolloutBody(config), capacity_token: preview.capacity_token, confirm_capacity: true})});
      states[key] = result; paint();
      // Never start the next workload until this exact accepted generation is ready.
      if (!result.uid || !Number.isInteger(result.generation)) throw new Error("Rollout identity unavailable; check Jobs before continuing");
      const deadline = Date.now() + 15 * 60 * 1000;
      while (true) {
        if (!active()) return;
        let state;
        try {
          state = await api(`/api/image-updates/progress?ns=${encodeURIComponent(config.ns)}&name=${encodeURIComponent(config.name)}`,
            config.cluster ? {headers: clusterHeaders(config)} : undefined);
        } catch (error) {
          // Homestead is replacing itself - here, or on a linked cluster the
          // relay cannot reach for that minute: wait for it to answer again.
          if (!restartsHomestead(config) || Date.now() > deadline) throw error;
          states[key] = {...(states[key] || {}), phase: "restarting"}; paint();
          await new Promise(resolve => setTimeout(resolve, 3000));
          continue;
        }
        states[key] = state; paint();
        if (rolloutChanged(result, state)) throw new Error("Workload changed during monitoring; review remaining updates again");
        if (state.phase === "failed") throw new Error("Rollout failed; remaining updates were not started");
        if (state.phase === "ready") break;
        if (Date.now() > deadline) throw new Error("Monitoring timed out; check Jobs before continuing");
        await new Promise(resolve => setTimeout(resolve, 2000));
      }
    } catch (error) {
      failures.push({...config, error: error.message + " No automatic retry: check Jobs for this rollout before reviewing again."});
      for (const remaining of rows.slice(index + 1)) states[rolloutKey(remaining.config)] = {phase: "not started", ready: 0, desired: 1};
      paint();
      return;
    }
  }
  paint();
};

function batchUpdateMarkup(items, states, startFailures = [], reconnecting = false, queueMode = false) {
  const failureMap = Object.fromEntries(startFailures.map(item => [rolloutKey(item), item.error]));
  const complete = items.filter(item => failureMap[rolloutKey(item)] ||
    ["ready", "failed"].includes(states[rolloutKey(item)]?.phase)).length;
  return `<div class="batch-rollout">
    <div class="between"><div><b>${complete}/${items.length} rollouts complete</b>
      <div class="dim xs">Each workload is tracked independently and keeps its own rollback image.</div></div>
      ${queueMode && startFailures.length ? '<span class="pill warn">queue stopped</span>' : complete === items.length ? '<span class="pill ok">finished</span>' : reconnecting ? '<span class="pill warn">reconnecting</span>' : '<span class="pill ok">monitoring</span>'}</div>
    <div class="rollout-meter"><span style="width:${items.length ? Math.round(complete / items.length * 100) : 100}%"></span></div>
    <div class="batch-rollout-list">${items.map(item => {
      const key = rolloutKey(item), state = states[key], startError = failureMap[key];
      const phase = startError ? "needs attention" : state?.phase || "starting";
      const tone = phase === "ready" ? "ok" : phase === "failed" || startError ? "crit" : "warn";
      // The ready count can be the old pod's: what the new one waits for says more.
      const waiting = phase !== "ready" && (state?.pods || []).find(pod => pod.blocked);
      return `<div><span><b>${esc(item.name)}</b><small>${item.clusterName ? `${esc(item.clusterName)} · ` : ""}${esc(item.ns)}${state && state.desired != null ? ` · ${state.ready || 0}/${state.desired} ready` : ""}</small></span>
        <span class="pill ${tone}">${esc(phase)}</span>${startError ? `<div class="updateerror">${esc(startError)}</div>` : ""}
        ${waiting ? `<div class="dim xs">New pod waiting${waiting.node ? ` on ${esc(waiting.node)}` : ""}: ${esc(waiting.blocked)}</div>` : ""}</div>`;
    }).join("")}</div>
    ${queueMode ? '<p class="small dim">Closing stops unstarted updates. Submitted rollouts continue and can be monitored in Jobs. A stopped queue always needs a new review.</p>' : ""}
    <div class="row" style="margin-top:18px"><button class="btn" onclick="closeModal()">${queueMode ? complete === items.length ? "Done" : "Close / stop queue" : "Monitor in background"}</button></div>
  </div>`;
}

window.imageUpdateBatchApply = () => imageReviewedApply();

window.monitorImageRollouts = (items, startFailures = [], initialStates = {}, allItems = items) => {
  if (window.__updateTimer) clearInterval(window.__updateTimer);
  const states = { ...initialStates };
  let misses = 0;
  const poll = async () => {
    if ($("#modal").classList.contains("hidden")) return clearInterval(window.__updateTimer);
    const results = await Promise.all(items.map(async item => {
      try {
        const state = await api(`/api/image-updates/progress?ns=${encodeURIComponent(item.ns)}&name=${encodeURIComponent(item.name)}`);
        return { item, state };
      } catch (error) { return { item, error }; }
    }));
    const successful = results.filter(result => result.state);
    misses = successful.length ? 0 : misses + 1;
    successful.forEach(result => { states[rolloutKey(result.item)] = result.state; });
    if ($("#mbody")) $("#mbody").innerHTML = batchUpdateMarkup(allItems, states, startFailures, misses > 0);
    if (window.applyRole) window.applyRole();
    const terminal = items.every(item => ["ready", "failed"].includes(states[rolloutKey(item)]?.phase));
    if (terminal) {
      clearInterval(window.__updateTimer); window.__updateTimer = null;
      if ($("#mbody")) $("#mbody").insertAdjacentHTML("beforeend", '<button class="btn pri" onclick="closeModal();go(\'workloads\')">Done</button>');
      setTimeout(() => loadImageUpdates(true, true), 1000);
    }
  };
  poll(); window.__updateTimer = setInterval(poll, 2000);
};

function pullElapsed(seconds) {
  const value = Math.max(0, Math.round(seconds || 0));
  return value < 60 ? `${value}s` : `${Math.floor(value / 60)}m ${String(value % 60).padStart(2, "0")}s`;
}

/* Kubernetes reports no byte progress for an image pull — the kubelet only
   says it started and, later, how long it took — so this says which node is
   fetching and for how long rather than drawing a percentage it cannot know. */
function pullDetail(s) {
  const pull = s.pull || {};
  if (pull.state === "pulling") {
    // containerd's own count of the layers fetched, against the registry's sizes.
    const amount = pull.total_bytes ? ` · ${pull.percent || 0}% of ${pullSize(pull.total_bytes)}` : "";
    return `Fetching ${pull.image || "the image"}${pull.node ? ` on ${pull.node}` : ""}${amount} · ${pullElapsed(pull.seconds)} so far`;
  }
  if (pull.state === "failed") return pull.detail || "The image could not be pulled";
  if (pull.state === "pulled" && s.updated < s.desired) {
    return `Pulled${pull.took ? ` in ${pull.took}` : ""}; starting the container`;
  }
  return `${s.updated} replacement pod${s.updated === 1 ? "" : "s"} created`;
}

function pullSize(b) { return b >= 1024 ** 3 ? `${(b / 1024 ** 3).toFixed(1)} GB` : `${Math.max(1, Math.round(b / 1024 ** 2))} MB`; }

function rolloutMarkup(s) {
  // While the new image is fetched, the bar is the fetch: it is most of the wait.
  const pulling = s.pull?.state === "pulling" && s.pull.total_bytes;
  const pct = pulling ? Math.min(99, s.pull.percent || 0)
    : s.desired ? Math.min(100, Math.round(s.ready / s.desired * 100)) : (s.phase === "ready" ? 100 : 0);
  return `<div class="rollout-head"><span class="pill ${s.phase === "ready" ? "ok" : s.phase === "failed" ? "crit" : "warn"}">${esc(s.phase)}</span>
    <span class="mono small">${s.ready}/${s.desired} ready · ${s.updated}/${s.desired} updated</span></div>
    <div class="rollout-meter"><span style="width:${pct}%"></span></div>
    <div class="rollout-steps">
      <div class="${s.observed_generation >= s.generation ? "done" : "active"}"><i></i><span><b>Deployment accepted</b><small>Generation ${s.generation}</small></span></div>
      <div class="${s.updated >= s.desired && s.pull?.state !== "pulling" ? "done" : s.pull?.state === "failed" ? "failed" : "active"}"><i></i><span><b>${s.pull?.state === "pulling" ? "Pulling new image" : s.pull?.state === "failed" ? "Image pull failed" : "New image pulled"}</b><small>${esc(pullDetail(s))}</small></span></div>
      <div class="${s.phase === "ready" ? "done" : s.phase === "failed" ? "failed" : "active"}"><i></i><span><b>Readiness checks</b><small>${s.ready} pod${s.ready === 1 ? "" : "s"} serving</small></span></div>
    </div>
    ${s.problems?.length ? `<div class="gateerr">${s.problems.map(esc).join("<br>")}</div>` : ""}
    <div class="podprogress">${(s.pods || []).map(p => `<div><span><b>${esc(p.name)}</b><small>${esc(p.node || "scheduling")}${p.pull?.state === "pulling" ? ` · pulling${p.pull.total_bytes ? ` ${p.pull.percent || 0}%` : ""} ${esc(pullElapsed(p.pull.seconds))}` : ""}</small>${p.blocked ? `<small class="pod-blocked">${esc(p.blocked)}</small>` : ""}</span>
      <span class="pill ${p.phase === "Running" ? "ok" : "warn"}">${esc(p.pull?.state === "pulling" ? "pulling image" : p.waiting?.[0]?.reason || p.phase)}</span></div>`).join("")}</div>
    <div class="row" style="margin-top:18px">
      ${s.can_rollback ? `<button class="btn ${s.phase === "failed" ? "danger" : ""}" data-need="operator" onclick="imageRollback(${jsq(s.ns)},${jsq(s.name)})">Rollback</button>` : ""}
      ${s.phase === "ready" ? '<button class="btn pri" onclick="closeModal();go(\'workloads\')">Done</button>' : '<button class="btn" onclick="closeModal()">Monitor in background</button>'}
    </div>`;
}

window.monitorImageRollout = (ns, name) => {
  if (window.__updateTimer) clearInterval(window.__updateTimer);
  modal("Rollout · " + name, '<div class="empty"><span class="spin2"></span> waiting for Kubernetes…</div>', true);
  let misses = 0;
  const poll = async () => {
    if ($("#modal").classList.contains("hidden")) return clearInterval(window.__updateTimer);
    try {
      const s = await api(`/api/image-updates/progress?ns=${encodeURIComponent(ns)}&name=${encodeURIComponent(name)}`);
      misses = 0; $("#mbody").innerHTML = rolloutMarkup(s);
      if (window.applyRole) window.applyRole();
      if (s.phase === "ready" || s.phase === "failed") {
        clearInterval(window.__updateTimer); window.__updateTimer = null;
        setTimeout(() => loadImageUpdates(true, true), 1000);
      }
    } catch (e) {
      misses++;
      $("#mbody").innerHTML = `<div class="empty"><span class="spin2"></span><b>Reconnecting to Homestead…</b><br>
        <span class="dim small">The control-panel container may be replacing itself (${misses}). Monitoring will resume automatically.</span></div>`;
    }
  };
  poll(); window.__updateTimer = setInterval(poll, 2000);
};

window.imageRollback = (ns, name) => reviewImageActions([{ns, name}], "rollback");
window.imageRollbackApply = () => imageReviewedApply();
window.wlLogs = (ns, pod, workload = "", fromRoute = false) => {
  if (!fromRoute && window.setModalRoute) setModalRoute({ panel: "logs", ns, workload: workload || pod }, (workload || pod) + " logs");
  if (!pod) return modal("Logs unavailable", '<div class="empty"><b>No running pod</b><br><span class="dim small">Start the container and wait for Kubernetes to create a pod.</span></div>');
  const workloadRow = (STATE.data.wl || []).find(x => x.ns === ns && x.name === workload);
  openLogs(workload || pod, `/api/logs?ns=${encodeURIComponent(ns)}&pod=${encodeURIComponent(pod)}`,
    workloadRow?.pods || null, ns);
};
window.jobLogs = (ns, job) => openLogs(job, `/api/logs?ns=${encodeURIComponent(ns)}&job=${encodeURIComponent(job)}`);

/* ---------------- interactive container console ---------------- */
window.wlConsole = (ns, name, fromRoute = false) => {
  const workload = (STATE.data.wl || []).find(x => x.ns === ns && x.name === name);
  if (!workload) return toast("Workload details are not available yet", "bad");
  const pods = (workload.pods || []).filter(p => p.phase === "Running" && (p.containers || []).some(c => c.kind === "app"));
  if (!fromRoute && window.setModalRoute) setModalRoute({ panel: "console", ns, workload: name }, name + " console");
  window.__consoleWorkload = workload;
  modal("Console · " + name, `<div class="consolebar">
      <div class="f"><label>Pod</label><select id="consolePod" onchange="consolePodChanged()">
        ${pods.map(p => `<option value="${esc(p.name)}">${esc(p.name)} · ${esc(p.node || "unscheduled")}</option>`).join("")}
      </select></div>
      <div class="f"><label>Container</label><select id="consoleContainer"></select></div>
      <div class="f"><label>Shell ${tip("Automatic tries /bin/sh, /bin/bash, then /bin/ash. Select one explicitly if the image uses a known shell.")}</label><select id="consoleShell">
        <option value="auto">automatic</option><option value="/bin/sh">/bin/sh</option><option value="/bin/bash">/bin/bash</option><option value="/bin/ash">/bin/ash</option>
      </select></div>
      <button class="btn pri" id="consoleConnect" onclick="consoleConnect()">Connect</button>
    </div>
    ${pods.length ? `<div class="console-security">Operator-only · session start and stop are audited; commands and output are not recorded.</div>
      <div class="consolestate" id="consoleState">not connected</div>
      <pre class="consoleview" id="consoleView" tabindex="0" aria-label="Container terminal output">Choose a pod and container, then connect.</pre>
      <div class="consoleinput"><textarea id="consoleInput" rows="1" spellcheck="false" autocomplete="off" placeholder="Type a command · Enter sends · Shift+Enter adds a line"></textarea>
        <button class="btn" onclick="consoleSend()">Send</button></div>`
      : `<div class="empty"><b>No running pod with an application container</b><br><span class="dim">Start the workload and wait until its pod is running.</span></div>`}`, true);
  if (pods.length) consolePodChanged();
};

window.consolePodChanged = () => {
  const podName = $("#consolePod")?.value;
  const pod = (window.__consoleWorkload?.pods || []).find(p => p.name === podName);
  const select = $("#consoleContainer");
  if (!select) return;
  select.innerHTML = (pod?.containers || []).filter(c => c.kind === "app")
    .map(c => `<option value="${esc(c.name)}">${esc(c.name)} · ${esc(c.state || "unknown")}</option>`).join("");
};

function consoleWrite(data, stream = "stdout") {
  const view = $("#consoleView");
  if (!view) return;
  if (view.dataset.empty !== "0") { view.textContent = ""; view.dataset.empty = "0"; }
  const span = document.createElement("span");
  span.className = stream === "stderr" ? "console-stderr" : "";
  span.textContent = data;
  view.appendChild(span);
  if (view.textContent.length > 250000) view.removeChild(view.firstChild);
  view.scrollTop = view.scrollHeight;
}

function consoleSize() {
  const view = $("#consoleView");
  if (!view) return { cols: 80, rows: 24 };
  return { cols: Math.max(20, Math.floor(view.clientWidth / 8.2)), rows: Math.max(5, Math.floor(view.clientHeight / 18)) };
}

window.consoleConnect = (attempt = 0) => {
  if (window.__consoleSocket) window.__consoleSocket.close();
  const pod = $("#consolePod")?.value, container = $("#consoleContainer")?.value;
  if (!pod || !container) return toast("Choose a running pod and container", "bad");
  const shells = ["/bin/sh", "/bin/bash", "/bin/ash"];
  const selected = $("#consoleShell").value;
  const shell = selected === "auto" ? shells[Math.min(attempt, shells.length - 1)] : selected;
  const ns = window.__consoleWorkload.ns;
  const protocol = location.protocol === "https:" ? "wss:" : "ws:";
  const query = new URLSearchParams({ ns, pod, container, shell });
  const socket = new WebSocket(fleetSocketUrl(`${protocol}//${location.host}/api/console?${query}`));
  window.__consoleSocket = socket;
  window.__consoleAttempt = attempt;
  $("#consoleState").textContent = `connecting · ${shell}`;
  $("#consoleConnect").textContent = "Reconnect";
  socket.onopen = () => {
    $("#consoleState").textContent = `connected · ${shell}`;
    socket.send(JSON.stringify({ type: "resize", ...consoleSize() }));
    $("#consoleInput").focus();
  };
  socket.onmessage = event => {
    let message;
    try { message = JSON.parse(event.data); } catch (_) { return; }
    if (message.type === "output") consoleWrite(message.data, message.stream);
    else if (message.type === "error") {
      const canFallback = selected === "auto" && attempt < shells.length - 1 && /not found|executable|no such file/i.test(message.data || "");
      if (canFallback) { consoleWrite(`\r\n${shell} unavailable; trying ${shells[attempt + 1]}…\r\n`, "stderr"); return consoleConnect(attempt + 1); }
      consoleWrite(`\r\n${message.data || "Console error"}\r\n`, "stderr");
    } else if (message.type === "disconnected") $("#consoleState").textContent = `disconnected · ${message.reason || "session ended"}`;
  };
  socket.onclose = () => { if (window.__consoleSocket === socket) $("#consoleState").textContent = "disconnected · use Reconnect to try again"; };
  socket.onerror = () => { if (window.__consoleSocket === socket) $("#consoleState").textContent = "connection unavailable · check role and pod state"; };
  if (window.__consoleResize) window.__consoleResize.disconnect();
  window.__consoleResize = new ResizeObserver(() => {
    if (socket.readyState === WebSocket.OPEN) socket.send(JSON.stringify({ type: "resize", ...consoleSize() }));
  });
  window.__consoleResize.observe($("#consoleView"));
};

/* Text selected in the console's input or its output, if any. */
function consoleSelection() {
  const input = $("#consoleInput");
  if (input && document.activeElement === input && input.selectionStart !== input.selectionEnd) {
    return input.value.slice(input.selectionStart, input.selectionEnd);
  }
  const selection = window.getSelection(), view = $("#consoleView");
  return selection && view && !selection.isCollapsed && view.contains(selection.anchorNode) ? selection.toString() : "";
}
window.consoleSend = () => {
  const input = $("#consoleInput"), socket = window.__consoleSocket;
  if (!input || !socket || socket.readyState !== WebSocket.OPEN) return toast("Connect the console first", "bad");
  socket.send(JSON.stringify({ type: "input", data: input.value + "\n" }));
  input.value = "";
};
document.addEventListener("keydown", event => {
  if (event.target?.id !== "consoleInput") return;
  if (event.key === "Enter" && !event.shiftKey) { event.preventDefault(); consoleSend(); }
  // Ctrl+C with text selected copies it, as in a terminal; only without a
  // selection does it interrupt.
  else if (event.key.toLowerCase() === "c" && event.ctrlKey && !event.shiftKey && !consoleSelection()
      && window.__consoleSocket?.readyState === WebSocket.OPEN) {
    event.preventDefault(); window.__consoleSocket.send(JSON.stringify({ type: "input", data: "\u0003" }));
  }
});

/* ---------------- deploy ---------------- */
const deployDefaults = () => ({ name: "", workload_name: "", container_name: "", image: "", icon: "", namespace: "lab", replicas: 1,
  cpu: "50m", memory: "128Mi", memory_limit: "", ports: [], env: {}, env_meta: [], volumes: [], hardware: [],
  template_devices: [], target_mode: "new", target_workload: "", network_mode: "loadbalancer",
  vip_mode: "shared", lb_ip: "", env_bindings: {}, app_profile: null });
let DCFG = deployDefaults(), DOPT = { deployments: [], pvcs: [], storage_classes: [], shared_storage_classes: [] }, DRENDERING = false;
function generatedSecret() {
  const bytes = new Uint8Array(18); crypto.getRandomValues(bytes);
  return btoa(String.fromCharCode(...bytes)).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}
async function viewDeploy(pre) {
  pre = pre || window.__deployPrefill;
  window.__deployPrefill = null;
  const [, liveNodes] = await Promise.all([loadHardwareFeatures(), api("/api/nodes").catch(() => [])]);
  if (liveNodes.length) STATE.data.nodes = liveNodes;
  DCFG = Object.assign(deployDefaults(), pre || {});
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
  const nss = await api("/api/namespaces").catch(() => ["lab"]);
  if (!nss.includes(DCFG.namespace)) DCFG.namespace = nss.includes("lab") ? "lab" : nss[0];
  DOPT = await api("/api/deploy/options?ns=" + encodeURIComponent(DCFG.namespace)).catch(() => DOPT);
  const vips = await vipChoices();
  const sharedVip = vips.shared || "";
  resetPaint();
  paint(`<div class="phead"><div><h2>Deploy a container</h2>
      <p>Run an independent workload or add a sidecar container to an existing pod</p></div>
      <button class="btn" data-need="operator" onclick="composeImport()">Import Docker Compose</button></div>
  <div class="split">
    <div class="card flat">
      ${DCFG.app_profile ? `<div class="app-profile ${esc(DCFG.app_profile.level || "review")}">
        <div class="settings-card-head"><div><b>${esc(DCFG.app_profile.label || "Template guidance")}</b>
          <div class="dim small">Compatibility guidance derived from ports, paths, variables, and runtime access</div></div><span class="pill ${DCFG.app_profile.level === "dependency" ? "warn" : "info"}">${esc(DCFG.app_profile.intent || "template")}</span></div>
        ${(DCFG.app_profile.notes || []).map(note => `<div class="profile-note">✓ ${esc(note)}</div>`).join("")}
        ${(DCFG.app_profile.dependencies || []).length ? `<div class="dependency-list">${DCFG.app_profile.dependencies.map(dep => `<div class="dependency-row stranded"><span>${esc(dep.name)}</span><b>${dep.managed ? "managed" : "deploy separately"}</b></div>`).join("")}</div>` : ""}
      </div>` : ""}
      <div class="f"><label>Deployment model ${tip("A new workload gets its own pod and lifecycle. Adding to an existing workload creates a sidecar container; Kubernetes restarts that workload's pods to apply it.")}</label>
        <select id="d_target_mode"><option value="new" ${DCFG.target_mode !== "existing" ? "selected" : ""}>New workload · independent pod</option>
          <option value="existing" ${DCFG.target_mode === "existing" ? "selected" : ""}>Add container to existing workload · shared pod</option></select></div>
      <div id="d_join_wrap" class="joinbox">
        <div class="f"><label>Existing workload</label><select id="d_target_workload"></select></div>
        <div id="d_join_note" class="note"></div>
      </div>
      <div class="f" id="d_workload_name_wrap"><label>Workload / pod prefix ${tip("The stable name for this workload. Kubernetes adds a generated suffix to each running pod, such as my-app-7d9f8c6b5-x2abc.")}</label><input type="text" id="d_workload_name" value="${esc(DCFG.workload_name)}" placeholder="my-app"></div>
      <div class="f"><label>Container name ${tip("The name of the container inside the pod. It can differ from the workload name and must use lowercase letters, numbers, and dashes.")}</label><input type="text" id="d_container_name" value="${esc(DCFG.container_name)}" placeholder="my-app"></div>
      <div class="f"><label>Docker image ${tip("The registry image and tag Kubernetes will pull, for example ghcr.io/home-assistant/home-assistant:stable")}</label><input type="text" id="d_image" value="${esc(DCFG.image)}" placeholder="nginx:alpine · ghcr.io/user/app:tag"><span class="dim xs" id="d_image_note">${imagePullNote(DCFG.image)}</span></div>
      <div class="f"><label>Container logo ${tip("Optional public HTTPS image URL. Homestead validates and saves a private copy on its persistent volume, so the logo survives source outages and upgrades.")}</label><input type="url" id="d_icon" value="${esc(DCFG.icon || "")}" placeholder="https://…/icon.png"></div>
      <div class="f2">
        <div class="f"><label>Namespace</label><select id="d_ns">${nss.map(n => `<option ${n === DCFG.namespace ? "selected" : ""}>${esc(n)}</option>`).join("")}</select></div>
        <div class="f" id="d_rep_wrap"><label>Instances ${tip("How many copies of this workload run at once. Most homelab apps want one; Longhorn replicas are a separate, storage-level idea.")}</label><input type="number" id="d_rep" value="${DCFG.replicas}" min="0" max="5"></div>
      </div>
      <div class="f2">
        <div class="f"><label>CPU reserved ${tip("The scheduler guarantees this much CPU capacity. 1000m = one CPU core; 50m = 5% of one core. This is not a hard limit.")}</label><input type="text" id="d_cpu" value="${esc(DCFG.cpu)}" placeholder="50m"></div>
        <div class="f"><label>Memory reserved ${tip("The scheduler keeps this much RAM available for the container. Mi means mebibytes and Gi means gibibytes. This is not a hard limit.")}</label><input type="text" id="d_mem" value="${esc(DCFG.memory)}" placeholder="128Mi"></div>
      </div>
      <div class="f"><label>Memory max (optional) ${tip("The most memory this container may use. Exceeding it can cause an OOM kill and restart. Leave blank for no container memory limit; set it at least as high as Memory reserved. Use Mi or Gi, for example 1Gi.")}</label><input type="text" id="d_mem_limit" value="${esc(DCFG.memory_limit || "")}" placeholder="No limit · e.g. 1Gi"><span class="dim xs">A limit protects the host, but setting it too low can repeatedly restart the app.</span></div>
      <div class="sec">Hardware ${tip("Homestead adds the device path and schedules only onto nodes marked as having that hardware.")}</div>
      <div class="hwchoices">
        ${hardwareChoices("d_hw", (DCFG.hardware || []).concat(DCFG.gpu && !(DCFG.hardware || []).includes("igpu") ? ["igpu"] : []))}
      </div>
      <div class="sec">Privileges ${tip("What the container may do to its host beyond the defaults. VPN containers need the tunnel.")}</div>
      ${DCFG.tun || DCFG.privileged || (DCFG.cap_add || []).length ? '<div class="note">Set from the template: Unraid gives this app these privileges.</div>' : ""}
      ${privilegeFields("d_pv", DCFG)}
      ${(DCFG.template_devices || []).length ? `<div class="note import-device-note"><b>Imported device mappings:</b> ${(DCFG.template_devices || []).map(d => `<span class="mono">${esc(d.host_path || "?")} → ${esc(d.container_path || "?")}</span>`).join(", ")}. Matching hardware features were selected; review them before deploying.</div>` : ""}
      <div class="sec">Network ${tip("Kubernetes replaces Docker bridge networking with Services. Use a dedicated VIP for DNS servers and other workloads that must own common ports.")}</div>
      <div class="f2"><div class="f"><label>Access mode</label><select id="d_net">
        <option value="loadbalancer" ${DCFG.network_mode === "loadbalancer" ? "selected" : ""}>LAN access (VIP)</option>
        <option value="internal" ${DCFG.network_mode === "internal" ? "selected" : ""}>Cluster only</option>
        <option value="host" ${DCFG.network_mode === "host" ? "selected" : ""}>Host network (advanced)</option>
        <option value="lan" ${DCFG.network_mode === "lan" ? "selected" : ""}>Its own LAN address (bridged)</option></select></div>
        <div class="f"><label>${nodeAddressesOnly() ? `LAN address ${tip(NODE_ADDRESS_TIP)}` : "VIP allocation"}</label><select id="d_vip_mode">
          ${nodeAddressesOnly() ? nodeAddressOption() : `${nodeAddressChoice(DCFG.vip_mode === "nodes" || (DCFG.vip_mode === "shared" && !sharedVip))}
          ${nodeAddressBeside() && !sharedVip ? "" : `<option value="shared" ${DCFG.vip_mode === "shared" ? "selected" : ""}>Default workload VIP${sharedVip ? ` · ${esc(sharedVip)}` : " · configure in Networking"}</option>`}
          <option value="auto" ${DCFG.vip_mode === "auto" ? "selected" : ""}>New automatic VIP${vips.freeCount ? ` · ${vips.freeCount} free` : ""}</option>
          <option value="manual" ${DCFG.vip_mode === "manual" ? "selected" : ""}>Specific VIP</option>`}</select></div></div>
      <div class="f" id="d_vip_wrap"><label>Specific VIP</label>${vipPicker("d", DCFG.lb_ip || "", vips)}</div>
      <div id="d_lan_box" hidden></div>
      ${nodeAddressesOnly() ? `<div class="note"><b>Docker bridge → Kubernetes Service.</b> On k3s it answers on every node's own address at its LAN port.
        Each port can be used by one Service only; a DNS server wanting port 53 needs it free there. Host network binds directly on one node and reduces failover safety.
        For an address of its own, add <b>kube-vip</b> under Settings → Hardware and storage → Add-ons.</div>` : `<div class="note"><b>Docker bridge → Kubernetes Service.</b> Default workload VIP shares the address configured in Networking on unique LAN ports. New automatic VIP selects another reserved address; Specific VIP lets you choose. Neither uses the control-plane address. Multus is only needed for a separate bridged LAN interface. Host network binds directly on one node and reduces failover safety.</div>`}
      <div class="sec">Ports ${tip("Container port is where the process listens. LAN port is what clients use through the Kubernetes Service. TCP and UDP on the same number are separate listeners.")}</div><div id="d_ports"></div><button class="btn sm" onclick="addPort()">＋ add port</button>
      <div class="sec">Storage ${tip("The mount path is inside the container. Choose whether its backing storage is a new Longhorn claim, an existing claim, an existing volume in a shared pod, or a path on one host.")}</div>
      <div class="note storage-guide"><b>Choose deliberately:</b> RWO is best for one workload; RWX permits multi-node sharing; an existing PVC keeps its current data; a pod volume shares the exact backing volume with a sidecar. Host paths reduce failover portability.</div>
      <div id="d_vols"></div><button class="btn sm" onclick="addVol()">＋ add storage mapping</button>
      <div class="sec">Environment ${tip("Environment variables are passed directly to the container. App Store defaults are imported and remain editable.")}</div><div id="d_env"></div><button class="btn sm" onclick="addEnv()">＋ add variable</button>
      <div class="row" style="margin-top:24px">
        <button class="btn pri" onclick="doDeploy()" ${DCFG.app_profile?.blocked ? "disabled" : ""}>Deploy container</button>
        <button class="btn" onclick="previewYaml()">Preview manifest</button>
      </div>
    </div>
    <div class="card flat"><div class="ctitle">Configuration</div><div class="csub">Live summary</div>
      <div id="d_summary" style="margin-top:14px"></div>
      <button class="btn pri wide" style="margin-top:18px" onclick="doDeploy()" ${DCFG.app_profile?.blocked ? "disabled" : ""}>Deploy</button></div>
  </div>`);
  DRENDERING = true;
  renderDeployTargets(); renderPorts(); renderVols(); renderEnv(); applyDeployMode();
  DRENDERING = false; syncSummary();
  ["d_workload_name", "d_container_name", "d_image", "d_icon", "d_rep", "d_cpu", "d_mem", "d_mem_limit", "d_net", "d_vip_mode", "d_lb_ip", "d_target_workload"].forEach(id => {
    const el = $("#" + id); if (!el) return;
    el.addEventListener("input", syncSummary); el.addEventListener("change", syncSummary);
  });
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
    ? `<b>Shared lifecycle:</b> saving restarts <span class="mono">${esc(target.name)}</span> and all of its containers (${target.containers.map(esc).join(", ")}). The new container shares the pod network, scheduler placement, and selected pod volumes.`
    : `<b>No existing workload is available.</b> Select another namespace or create a new workload.`;
}
async function refreshDeployOptions() {
  collect();
  const ns = $("#d_ns").value;
  try { DOPT = await api("/api/deploy/options?ns=" + encodeURIComponent(ns)); }
  catch (e) { toast("Could not load namespace storage: " + e.message, "bad"); DOPT = { deployments: [], pvcs: [], storage_classes: [] }; }
  DCFG.target_workload = ""; renderDeployTargets(); renderVols(); applyDeployMode(); syncSummary();
}
function applyDeployMode() {
  const joining = $("#d_target_mode")?.value === "existing";
  $("#d_join_wrap").style.display = joining ? "block" : "none";
  $("#d_workload_name_wrap").style.display = joining ? "none" : "block";
  $("#d_rep_wrap").style.display = joining ? "none" : "block";
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
  DCFG.hardware = selectedHardware("d_hw");
  Object.assign(DCFG, readPrivileges("d_pv") || {});
  DCFG.gpu = DCFG.hardware.includes("igpu"); DCFG.network_mode = $("#d_net").value;
  DCFG.vip_mode = $("#d_vip_mode").value; DCFG.lb_ip = $("#d_lb_ip").value.trim();
  DCFG.lan = DCFG.network_mode === "lan" && $("#dl_ip") ? containerLanRead("dl") : null;
  DCFG.ports = $$("#d_ports .port-row").map(r => ({ container: +$(".pc", r).value,
    host: +$(".ph", r).value || +$(".pc", r).value, protocol: $(".pp", r).value, expose: $(".pe", r).checked }));
  DCFG.volumes = readVolumeRows($("#d_vols"));
  DCFG.env = {}; $$("#d_env .env-row").forEach(r => { const k = $(".ek", r).value.trim(); if (k) DCFG.env[k] = $(".ev", r).value; });
  return DCFG;
}
function syncSummary() {
  const c = collect();
  const vipWrap = $("#d_vip_wrap"); if (vipWrap) vipWrap.style.display = c.network_mode === "loadbalancer" && c.vip_mode === "manual" ? "block" : "none";
  const imageNote = $("#d_image_note"); if (imageNote) imageNote.innerHTML = imagePullNote(c.image);
  const row = (i, l, v) => `<div class="drow"><div class="di">${i}</div><div class="dl">${l}</div><div class="dv">${v}</div></div>`;
  $("#d_summary").innerHTML =
    (c.target_mode === "existing" ? "" : row("◈", "Workload / pod", c.workload_name ? `<b>${esc(c.workload_name)}</b>` : '<span class="dim">—</span>')) +
    row("▣", "Container", c.container_name ? `<b>${esc(c.container_name)}</b>` : '<span class="dim">—</span>') +
    row("❏", "Image", c.image ? `<span class="small mono">${esc(c.image)}</span>` : '<span class="dim">—</span>') +
    row("⌗", "Namespace", esc(c.namespace)) + row("⧉", c.target_mode === "existing" ? "Joins workload" : "Instances", c.target_mode === "existing" ? esc(c.target_workload || "—") : c.replicas) +
    row("◴", "Requests", `<span class="small mono">${esc(c.cpu)} · ${esc(c.memory)}</span>`) +
    row("▣", "Memory max", c.memory_limit ? `<span class="small mono">${esc(c.memory_limit)}</span>` : '<span class="dim">no limit</span>') +
    ((c.command || []).length || (c.args || []).length ? row("›", "Runs", `<span class="small mono">${esc([...(c.command || []), ...(c.args || [])].join(" "))}</span>`) : "") +
    row("▤", "Hardware", c.hardware.length ? hardwareTags(c.hardware) : '<span class="dim">none</span>') +
    row("◎", "Network", `<span class="small">${esc(c.network_mode)}${c.network_mode === "loadbalancer" ? ` · ${esc(c.vip_mode)} VIP` : ""}</span>`) +
    row("⇄", "Ports", c.ports.length ? c.ports.map(p => `<span class="tag ${p.expose ? "info" : ""}">${p.host}→${p.container}</span>`).join("") : '<span class="dim">—</span>') +
    row("▥", "Storage", c.volumes.length ? c.volumes.map(v => `<span class="tag">${esc(v.source || (v.kind === "ephemeral" ? "temporary" : "choose source"))} · ${esc(v.kind)}</span>`).join("") : '<span class="dim">—</span>') +
    row("≡", "Env vars", Object.keys(c.env).length ? `<span class="tag">${Object.keys(c.env).length} set</span>` : '<span class="dim">—</span>');
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
    podEmpty: "Choose a volume from the selected workload…",
    podUnavailable: "No reusable volumes in the selected workload",
    podHelp: "Mounts a volume already defined on the selected workload into this sidecar.",
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
window.previewYaml = async () => {
  try { const r = await api("/api/preview", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(collect()) });
    modal("Manifest preview", `<pre>${esc(JSON.stringify(r, null, 2))}</pre>`, true); } catch (e) { toast(e.message, "bad"); }
};
let DEPLOY_REVIEW = null;
let DEPLOY_REVIEW_SEQUENCE = 0;
let DEPLOY_SUBMITTING = false;
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
  if (rolling) return `The rollout allows ${rolling[1]} extra pod(s) and ${rolling[2]} unavailable replica(s). Pods still stopping can temporarily increase the total.`;
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

function deployCapacityHtml(plan, overlap = false, imageChange = false) {
  if (!plan) return "";
  const { concerns: needs, caveats } = capacityNotes(plan);
  const title = overlap ? "New pods alongside current pods" : plan.rollout ? "Updated pod: capacity after old pods stop" : "Placement and memory";
  const blocked = overlap ? "This overlap does not fit while old pods remain. Progress may depend on old-pod removal within the rollout policy."
    : plan.rollout?.start_blocked ? "The rollout cannot start within its current availability policy. Review the overlap blockers below."
    : "This deployment cannot fit the checked placement constraints. Change its resources, volumes or host selection before deploying.";
  // Blocked, everything shows: the reason may be among them.
  const shown = plan.blocked ? plan.warnings || [] : needs;
  const concerns = shown.length ? `<ul class="ui-list">${shown.map(w => `<li>${esc(w)}</li>`).join("")}</ul>` : "";
  const rollout = plan.rollout ? `<p>${esc(plan.rollout.strategy)} · ${esc(plan.rollout.replicas)} desired replica(s) · ${plan.rollout.ownership_known
      ? `${esc(plan.rollout.owned_pods.length)} existing pod(s) identified by controller ownership; ${esc(plan.rollout.release_request_gb)} GiB of requests would be released only after termination.`
      : "Pod ownership is unverified; released capacity is unknown."}</p>
    ${plan.rollout.strategy === "RollingUpdate" ? `<p>Up to ${esc(plan.rollout.max_surge)} extra pod(s), ${esc(plan.rollout.max_unavailable)} unavailable replica(s). Intermediate rollout steps remain unverified.</p>` : ""}` : "";
  const requests = `${esc(plan.additional)} ${plan.vm ? "VM launcher" : "pod(s)"}, ${plan.vm?.request_is_lower_bound ? "requesting at least" : "each requesting"} ${esc(plan.pod_request_gb)} GiB RAM; ${plan.vm?.cpu_request_is_estimate ? "conservative CPU allowance" : "CPU request"}: ${esc(plan.pod_cpu_request_percent)}% (100% = one core). Memory estimate: ${esc(plan.pod_memory_gb)} GiB ${plan.vm ? "for the VM and launcher" : "per pod, including init stages"}.`;
  return `<div class="deploy-capacity ui-stack">
    ${overlap ? "" : capacityPlacementHtml(plan)}
    ${UI.section(title, [
      plan.blocked ? UI.callout("bad", blocked, `${concerns}${overlap ? "" : "<p>This is a placement blocker, not just a capacity warning. Resolve the listed scheduler, hardware or storage constraints before proceeding.</p>"}`)
        : concerns ? UI.callout("warn", "Placement and memory need review.", concerns) : "",
      plan.pod_request_gb !== undefined && plan.pod_request_gb !== null ? `<p class="ui-help">${requests}</p>` : "",
      capacityHosts(plan.candidates || [], { unavailable: "Live RAM unavailable",
        reserved: plan.rollout ? "Reserved RAM after planned termination" : "Reserved RAM", placement: plan.placement }),
    ].join(""))}
    ${UI.more("How this is estimated", `${rollout}
      ${caveats.length && !plan.blocked ? `<p>Estimate assumptions:</p><ul class="ui-list">${caveats.map(w => `<li>${esc(w)}</li>`).join("")}</ul>` : ""}
      ${!overlap && !plan.blocked ? "<p>Capacity warnings can be overridden, including high projected RAM and missing usage metrics. Proceeding may cause memory pressure, OOM restarts or downtime; it does not change resource requests or limits.</p>" : ""}
      <p>This is a snapshot, not a reservation or an OOM guarantee. ${plan.vm ? "The server checks again before sending the VM action. Guest readiness and successful rescheduling are not guaranteed." : imageChange ? "The server checks again before changing the workload or its recovery metadata." : "The server checks again before creating anything. Planned volumes have not been provisioned."}</p>`, !!plan.blocked)}
    ${plan.rollout?.overlap ? UI.more(`Overlap while old pods remain${plan.rollout.start_blocked ? " - rollout cannot start" : ""}`, deployCapacityHtml(plan.rollout.overlap, true), !!plan.rollout.start_blocked) : ""}
  </div>`;
}
window.deployReviewReady = () => {
  const review = DEPLOY_REVIEW;
  const ready = !DEPLOY_SUBMITTING && review && !review.plan?.blocked &&
    (review.config.target_mode !== "existing" || $("#deployConfirm")?.checked) &&
    (!review.plan?.requires_confirmation || $("#deployCapacityConfirm")?.checked);
  if ($("#deployGo")) $("#deployGo").disabled = !ready;
  return !!ready;
};
window.doDeploy = async () => {
  if (DEPLOY_SUBMITTING) return;
  const sequence = ++DEPLOY_REVIEW_SEQUENCE;
  DEPLOY_REVIEW = null;
  const c = collect();
  if (c.app_profile?.blocked) return toast(c.app_profile.label || "this template is not directly compatible", "bad");
  if (!c.container_name || !c.image) return toast("container name and image are required", "bad");
  if (c.target_mode === "new" && !c.workload_name) return toast("workload / pod name is required", "bad");
  if (c.target_mode === "existing" && !c.target_workload) return toast("choose an existing workload", "bad");
  const storageIssue = volumeListIssue(c.volumes);
  if (storageIssue) return toast(storageIssue, "bad");
  try {
    const plan = await api("/api/preview", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(c) });
    if (sequence !== DEPLOY_REVIEW_SEQUENCE) return;
    if (plan.app_profile?.blocked) return toast(plan.app_profile.label || "this workload needs a Kubernetes-specific design", "bad");
    const joining = c.target_mode === "existing";
    if (!plan.capacity) throw new Error("Capacity preview unavailable; refresh Homestead before deploying.");
    DEPLOY_REVIEW = { config: JSON.parse(JSON.stringify(c)), plan: plan.capacity, token: plan.capacity_token };
    modal(joining ? "Review shared-pod change" : "Review deployment", `<div class="update-review">
      <div class="reviewbox"><b>${joining ? `Add ${esc(c.container_name)} to ${esc(c.target_workload)}` : `Create ${esc(c.workload_name)} with container ${esc(c.container_name)}`}</b>
        <p class="dim">${esc(plan.impact?.message || "Review the Kubernetes objects before continuing.")}</p>
        ${plan.app_profile?.notes?.length ? `<div class="app-profile ${esc(plan.app_profile.level || "review")}">${plan.app_profile.notes.map(note => `<div class="profile-note">✓ ${esc(note)}</div>`).join("")}</div>` : ""}
        <div class="dependency-list"><div class="dependency-row"><span>Image</span><b class="mono">${esc(c.image)}</b></div>
          <div class="dependency-row"><span>Ports</span><b>${c.ports.length}</b></div><div class="dependency-row"><span>Storage mappings</span><b>${c.volumes.length}</b></div></div>
      </div>
      ${deployCapacityHtml(plan.capacity)}
      ${joining ? `<label class="switch dependency-confirm"><input type="checkbox" id="deployConfirm" onchange="deployReviewReady()"> ${plan.capacity.rollout?.paused ? "I understand this saves a paused template; capacity must be reviewed again before resuming" : plan.capacity.rollout?.replicas === 0 ? "I understand this changes the stopped workload's pod template without starting it" : `I understand every container in ${esc(c.target_workload)} restarts as its pods roll out, with the downtime or overlap shown above`}</label>` : ""}
      ${plan.capacity?.requires_confirmation && !plan.capacity.blocked ? `<label class="switch dependency-confirm"><input type="checkbox" id="deployCapacityConfirm" onchange="deployReviewReady()"> Proceed despite capacity warnings — I accept the placement, memory and provisioning risks</label>` : ""}
      <details><summary>Manifest preview</summary><pre>${esc(JSON.stringify({ deployment: plan.deployment, service: plan.service }, null, 2))}</pre></details>
      <div class="modalactions"><button class="btn" onclick="closeModal()">Cancel</button><button class="btn pri" id="deployGo" ${joining || plan.capacity?.requires_confirmation || plan.capacity?.blocked ? "disabled" : ""} onclick="confirmDeploy()">${joining ? "Add container & restart pod" : "Deploy workload"}</button></div></div>`, true);
  } catch (e) { toast(e.message, "bad"); }
};
window.confirmDeploy = async () => {
  if (!window.deployReviewReady()) return toast("Review the deployment and acknowledge its warnings first", "bad");
  const c = { ...DEPLOY_REVIEW.config, capacity_token: DEPLOY_REVIEW.token,
    confirm_capacity: !!$("#deployCapacityConfirm")?.checked };
  DEPLOY_SUBMITTING = true;
  try { $("#deployGo").disabled = true; const r = await api("/api/deploy", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(c) });
    const kept = (r.reused_volumes || []).length ? ` - kept the existing ${r.reused_volumes.join(", ")}, which nothing was using` : "";
    DEPLOY_REVIEW = null;
    closeModal(); toast((c.target_mode === "existing" ? `${c.container_name} added to ${c.target_workload}` : `${c.workload_name} deployed`) + kept, "ok"); go("workloads");
  } catch (e) {
    DEPLOY_REVIEW = null;
    const button = $("#deployGo");
    if (button) { button.disabled = false; button.textContent = "Review again"; button.onclick = () => window.doDeploy(); }
    toast(e.message, "bad");
  } finally { DEPLOY_SUBMITTING = false; }
};

/* ---------------- choosing a VIP ----------------
   A specific VIP is picked from what the cluster has: the free addresses in
   Harvester's IP pools, or a VIP already in use, whose ports are then shared.
   "Type an address" is there for one outside the pools. */
async function vipChoices(networkData) {
  const net = networkData || await api("/api/network", { keep: true }).catch(() => null);
  // The cluster's own address (Harvester's management VIP, an ingress
  // controller's) and addresses other software owns are never offered: an app
  // sharing the management VIP is how host joining breaks.
  const closed = { ...(net?.platform_addresses || {}), ...(net?.foreign_addresses || {}) };
  for (const ip of net?.node_ips || []) closed[ip] = "Cluster node address";
  for (const service of net?.services || []) {
    if (service.exclusive_vip) for (const ip of service.external_ips || []) closed[ip] = service.name;
  }
  const own = (net?.registered_vips || []).filter(v => !v.blocked && !closed[v.ip]);
  const mine = new Set(own.map(v => v.ip));
  return { free: (net?.available_vips || []).filter(ip => !mine.has(ip) && !closed[ip]), freeCount: net?.available_vip_count || 0,
    shared: closed[net?.shared_vip?.ip] ? "" : net?.shared_vip?.ip || "",
    used: (net?.vips || []).filter(v => !closed[v.ip]), own, labels: net?.vip_labels || {}, blocked: closed };
}
window.vipChoices = vipChoices;

function vipPicker(prefix, current, choices) {
  if (choices.blocked?.[current]) current = "";
  const own = choices.own || [], labels = choices.labels || {};
  const known = choices.free.includes(current) || choices.used.some(v => v.ip === current) || own.some(v => v.ip === current);
  const typed = !!current && !known;
  const option = (value, label) => `<option value="${esc(value)}" ${value === current ? "selected" : ""}>${esc(label)}</option>`;
  return `<select id="${prefix}_lb_pick" onchange="vipPicked(${jsq(prefix)})">
      <option value="" ${!current ? "selected" : ""}>Choose an address…</option>
      ${own.some(v => v.free) ? `<optgroup label="Your VIPs - free">${own.filter(v => v.free).map(v => option(v.ip, `${v.ip}${v.label ? ` · ${v.label}` : ""}`)).join("")}</optgroup>` : ""}
      ${choices.free.length ? `<optgroup label="Free in the IP pools (${choices.free.length})">${choices.free.slice(0, 60).map(ip => option(ip, ip)).join("")}</optgroup>` : ""}
      ${choices.used.length ? `<optgroup label="In use - shared with what is there">${choices.used.map(v =>
        option(v.ip, `${v.ip}${labels[v.ip] ? ` · ${labels[v.ip]}` : ""} · ${v.services} service${v.services === 1 ? "" : "s"} · ports ${v.listeners.map(l => l.port).slice(0, 5).join(", ")}`)).join("")}</optgroup>` : ""}
      <option value="__typed" ${typed ? "selected" : ""}>Type an address…</option></select>
    <input id="${prefix}_lb_ip" class="mono" value="${esc(current || "")}" placeholder="192.0.2.250" data-ipam ${typed ? "" : 'style="display:none"'}>`;
}

window.vipPicked = prefix => {
  const pick = $(`#${prefix}_lb_pick`), input = $(`#${prefix}_lb_ip`);
  const typed = pick.value === "__typed";
  input.style.display = typed ? "" : "none";
  if (!typed) input.value = pick.value;
  else input.focus();
  input.dispatchEvent(new Event("input"));
};

/* ---------------- app store ----------------
   The Community Applications catalogue, laid out the way Community
   Applications lays it out: a front page of this month's spotlights, the
   newest templates and what is trending, and a page for each. Any app opens a
   full description - project, support, spotlight note, what it asks for -
   before anything is configured. */
let STORE_MODE = "home";
const STORE_MODES = {
  home: ["Home", ""],
  spotlight: ["Spotlight", "Picked by the Unraid team each month, newest first"],
  recent: ["Recently added", "The newest templates, by the date the feed first saw them"],
  trending: ["Top trending", "Rising fastest in downloads right now"],
  popular: ["Top performing", "The best performers in the feed"],
};
const STORE_APPS = new Map();

function storeCount(value) {
  return new Intl.NumberFormat(undefined, { notation: Number(value || 0) >= 10000 ? "compact" : "standard",
    maximumFractionDigits: 1 }).format(Number(value || 0));
}

const storeKey = app => `${app.name}|${app.repo}`;
const storeDate = seconds => seconds ? new Date(seconds * 1000).toLocaleDateString(undefined,
  { month: "short", day: "numeric", year: "numeric" }) : "";

function storeMetric(app, mode) {
  if (mode === "spotlight" && app.spotlight) return `${icon("clock")} Spotlight · ${esc(app.spotlight.month)}`;
  if (mode === "recent" && app.first_seen) return `${icon("clock")} Added ${storeDate(app.first_seen)}`;
  if (mode === "trending" && (app.top_trending || app.trending)) {
    return `${icon("update")} ${Number(app.top_trending || app.trending || 0).toFixed(1)}% trend`;
  }
  if (mode === "popular" && app.top_performing) return `${icon("update")} ${Number(app.top_performing).toFixed(1)}% performance`;
  return app.downloads ? `${icon("import")} ${storeCount(app.downloads)} downloads` : `${icon("box")} Community template`;
}

function storeIcon(app, cls = "ico") {
  return app.icon ? `<img class="${cls}" src="${esc(app.icon)}" alt="" referrerpolicy="no-referrer" onerror="this.style.display='none'">`
    : `<span class="${cls} store-noicon">${icon("store")}</span>`;
}

function storeCard(app, mode) {
  STORE_APPS.set(storeKey(app), app);
  const key = esc(JSON.stringify(storeKey(app)));
  const label = appCategoryLabel(app.categories || app.cat);
  return `<div class="card app" role="button" tabindex="0" onclick="storeDetails(${key})"
      onkeydown="if(event.key==='Enter')storeDetails(${key})">
    <div class="row" style="gap:11px">${storeIcon(app)}
      <div style="min-width:0"><div class="nm">${esc(app.name)}</div>
        <div class="dim xs store-by">${esc(app.maintainer || "")}${app.official ? ' · <span class="store-official">official</span>' : ""}</div></div></div>
    <div class="row store-tags">${label ? `<span class="tag">${esc(label)}</span>` : ""}
      ${app.deploy?.app_profile ? `<span class="tag ${app.deploy.app_profile.level === "dependency" ? "warn" : "info"}">${esc(app.deploy.app_profile.label)}</span>` : ""}
      ${app.beta ? '<span class="tag warn">beta</span>' : ""}</div>
    <div class="store-metric">${storeMetric(app, mode)}</div>
    ${mode === "spotlight" && app.spotlight?.reason ? `<div class="store-why">“${esc(app.spotlight.reason)}”</div>` : ""}
    <div class="ds">${esc(app.desc || "No description provided.")}</div>
    <div class="rp">${esc(app.repo)}</div>
    <div class="row store-card-actions"><button class="btn sm" onclick="event.stopPropagation();storeDetails(${key})">Details</button>
      <button class="btn pri sm" onclick="event.stopPropagation();storeInstall(${key})">Configure &amp; deploy</button></div></div>`;
}

/* Say where the listings come from: the public feed, or one set in Settings. */
function storeSource(source) {
  const host = $("#s_source");
  if (!host || !source) return;
  host.innerHTML = source.default
    ? `Listings are read on demand from the public <a href="https://github.com/Squidly271/AppFeed" target="_blank" rel="noopener">Community Applications feed</a> and cached for six hours; another feed can be set in Settings. Homestead is independent and is not endorsed by the catalogue maintainers. Unraid® is a registered trademark of Lime Technology, Inc. This application is not affiliated with, endorsed, or sponsored by Lime Technology, Inc.`
    : `Listings are read from <span class="mono">${esc(source.url)}</span>, set in Settings, and cached for six hours. Templates come from whoever publishes that feed; review what each one asks for before deploying it.`;
}

function storeSection(mode, apps, total) {
  const [title, detail] = STORE_MODES[mode];
  return `<section class="store-section">
    <div class="store-section-head"><div><h3>${title}</h3><span>${detail}</span></div>
      <button class="btn sm" onclick="storeBrowse(${jsq(mode)})">Show more</button></div>
    ${apps.length ? `<div class="apps">${apps.map(a => storeCard(a, mode)).join("")}</div>` : '<div class="empty small">Nothing here yet.</div>'}
  </section>`;
}

async function viewStore() {
  resetPaint();
  paint(`<div class="phead">
      <div><h2>Community catalogue</h2><p>Third-party Community Applications templates adapted into reviewed Kubernetes workloads</p></div>
      <div class="row store-search"><input class="search" id="s_q" placeholder="plex, nextcloud, jellyfin…" value="${esc(STATE.q)}" style="width:260px;padding-left:16px">
      <button class="btn pri" onclick="storeSearch()">Search</button></div></div>
    <div class="store-browse-head"><div class="seg store-modes" id="s_modes">
      ${Object.entries(STORE_MODES).map(([mode, [label]]) => `<button data-mode="${mode}" class="${STORE_MODE === mode ? "on" : ""}" onclick="storeBrowse(${jsq(mode)})">${label}</button>`).join("")}
    </div></div>
    <div id="s_res"><div class="empty"><span class="spin2"></span>loading catalogue…</div></div>
    <div class="note catalogue-notice" id="s_source"></div>`);
  $("#s_q").addEventListener("keydown", e => { if (e.key === "Enter") { e.preventDefault(); storeSearch(); } });
  if (STATE.q) storeSearch(); else storeBrowse(STORE_MODE);
}
window.storeSearch = async () => {
  const q = $("#s_q").value.trim();
  if (!q) return storeBrowse(STORE_MODE);
  $$("#s_modes button").forEach(button => button.classList.remove("on"));
  $("#s_res").innerHTML = `<div class="empty"><span class="spin2"></span>searching catalogue…</div>`;
  try {
    const r = await api("/api/appstore?q=" + encodeURIComponent(q));
    $("#s_res").innerHTML = r.apps.length
      ? `<div class="dim small" style="margin-bottom:12px">${r.total} match${r.total === 1 ? "" : "es"} · showing ${r.apps.length}</div>
        <div class="apps stagger">${r.apps.map(a => storeCard(a, "search")).join("")}</div>`
      : `<div class="empty">nothing matched “${esc(q)}”</div>`;
  } catch (e) { $("#s_res").innerHTML = `<div class="empty">${esc(e.message)}</div>`; }
};
window.storeBrowse = async mode => {
  STORE_MODE = STORE_MODES[mode] ? mode : "home";
  STATE.q = "";
  if ($("#s_q")) $("#s_q").value = "";
  $$("#s_modes button").forEach(button => button.classList.toggle("on", button.dataset.mode === STORE_MODE));
  $("#s_res").innerHTML = `<div class="empty"><span class="spin2"></span>loading catalogue…</div>`;
  try {
    const r = await api("/api/appstore?sort=" + encodeURIComponent(STORE_MODE));
    storeSource(r.source);
    if (r.sections) {
      $("#s_res").innerHTML = ["spotlight", "recent", "trending", "popular"]
        .map(section => storeSection(section, r.sections[section] || [])).join("");
      return;
    }
    const [title, detail] = STORE_MODES[STORE_MODE];
    $("#s_res").innerHTML = `<div class="store-section-head"><div><h3>${title}</h3><span>${detail}</span></div><b>${r.apps.length}</b></div>
      ${r.apps.length ? `<div class="apps stagger">${r.apps.map(a => storeCard(a, STORE_MODE)).join("")}</div>` : `<div class="empty">No apps to show.</div>`}`;
  } catch (e) { $("#s_res").innerHTML = `<div class="empty">${esc(e.message)}</div>`; }
};

window.storeInstall = key => {
  const a = STORE_APPS.get(key);
  if (!a) return toast("That app is no longer in the catalogue", "bad");
  closeModal();
  window.__deployPrefill = a.deploy || { name: a.name, image: a.repo, icon: a.icon || "" };
  go("deploy");
  toast(`"${a.name}" loaded — check storage paths before deploying`);
};

const STORE_LINKS = [["project", "Project"], ["support", "Support"], ["github", "GitHub"], ["registry", "Registry"],
  ["readme", "Read me"], ["video", "Video"], ["discord", "Discord"], ["web", "Website"]];

/* One app, in full: what it is, who keeps it, why it was picked, what it asks for. */
window.storeDetails = async key => {
  const summary = STORE_APPS.get(key) || {};
  modal(summary.name || "App", '<div class="empty"><span class="spin2"></span>reading the template</div>', true, "store");
  let app;
  try { app = Object.assign({}, summary, await api("/api/appstore/app?key=" + encodeURIComponent(key))); }
  catch (e) { $("#mbody").innerHTML = `<div class="note bad">${esc(e.message)}</div>`; return; }
  STORE_APPS.set(key, app);
  const jsKey = esc(JSON.stringify(key));
  const profile = app.deploy?.app_profile;
  const facts = [
    ["Categories", (app.categories || []).join(", ")],
    ["Maintainer", app.maintainer],
    ["Added", storeDate(app.first_seen)],
    ["Updated", storeDate(app.last_update)],
    ["Downloads", app.downloads ? storeCount(app.downloads) : ""],
    ["Stars", app.stars ? String(app.stars) : ""],
    ["Image", app.repo],
    ["Network", app.network],
    ["License", app.license],
  ].filter(([, value]) => value);
  $("#mbody").innerHTML = `
    <div class="store-detail-head">${storeIcon(app, "store-detail-icon")}
      <div class="store-detail-title"><h3>${esc(app.name)}</h3>
        <div class="dim small">${esc(app.maintainer || "")}${app.official ? ' · <span class="store-official">official container</span>' : ""}</div>
        <div class="row store-tags">${(app.categories || []).slice(0, 4).map(c => `<span class="tag">${esc(c)}</span>`).join("")}
          ${app.beta ? '<span class="tag warn">beta</span>' : ""}${app.privileged ? '<span class="tag bad">asks for privileged</span>' : ""}</div></div>
      <button class="btn pri" onclick="storeInstall(${jsKey})">Configure &amp; deploy</button></div>
    <div class="row store-links">${STORE_LINKS.filter(([k]) => app.links?.[k]).map(([k, label]) =>
      `<a class="btn sm" href="${safeHref(app.links[k])}" target="_blank" rel="noopener noreferrer">${label} ${icon("ext")}</a>`).join("")}</div>
    ${app.spotlight ? `<div class="store-spot"><div class="store-spot-badge"><b>Monthly<br>spotlight</b><span>${esc(app.spotlight.month)}</span></div>
      <div><b>Why it was picked</b><p>${esc(app.spotlight.reason || "")}</p>${app.spotlight.who ? `<span class="dim xs">— ${esc(app.spotlight.who)}</span>` : ""}</div></div>` : ""}
    <div class="store-overview">${esc(app.overview || app.desc || "No description provided.")}</div>
    ${app.comment ? `<div class="note warn"><b>From the catalogue moderators:</b> ${esc(app.comment)}</div>` : ""}
    ${app.requires ? `<div class="note"><b>Requires:</b> ${esc(app.requires)}</div>` : ""}
    ${profile ? `<div class="note ${profile.level === "dependency" ? "warn" : ""}"><b>${esc(profile.label)}.</b> ${esc(
      [...(profile.blocked || []), ...(profile.dependencies || []), ...(profile.notes || [])].slice(0, 4)
        .map(n => typeof n === "string" ? n : (n.message || n.reason || n.name || "")).filter(Boolean).join(" · ")
      || "Homestead reviews its storage, ports and hardware when you configure it.")}</div>` : ""}
    ${(app.screenshots || []).length ? `<div class="sec">Screenshots</div><div class="store-shots">${app.screenshots.map(src =>
      `<a href="${safeHref(src)}" target="_blank" rel="noopener noreferrer"><img src="${esc(src)}" alt="" loading="lazy" referrerpolicy="no-referrer" onerror="this.parentElement.remove()"></a>`).join("")}</div>` : ""}
    <div class="sec">Details</div>
    <div class="store-facts">${facts.map(([label, value]) => `<div><span>${label}</span><b class="${label === "Image" ? "mono" : ""}">${esc(value)}</b></div>`).join("")}</div>`;
};

/* The port a card links to first: the app's web UI, usually. */
window.wlPrimaryPort = (ns, name) => {
  const w = (STATE.data.wl || []).find(x => x.ns === ns && x.name === name) || { ports: [] };
  const current = (w.ports.find(p => p.primary) || {}).port || 0;
  modal(`Main port · ${name}`, `<p class="small">The card links to this port first - usually the app's web UI.</p>
    <div class="primary-ports">${w.ports.map(p => `<label class="switch"><input type="radio" name="pp" value="${p.port}" ${p.port === current ? "checked" : ""}>
      <b class="mono">${p.port}</b> <span class="dim xs">${esc(p.name || "")}${p.ip ? ` · ${esc(p.ip)}` : ""}</span></label>`).join("")}
      <label class="switch"><input type="radio" name="pp" value="0" ${current ? "" : "checked"}> <span class="dim">No preference - their own order</span></label></div>
    <div class="row" style="margin-top:14px"><button class="btn pri" onclick="wlPrimaryPortSave(${jsq(ns)},${jsq(name)})">Save</button>
      <button class="btn" onclick="closeModal()">Cancel</button></div>`);
};
window.wlPrimaryPortSave = async (ns, name) => {
  const port = +(document.querySelector('input[name="pp"]:checked')?.value || 0);
  try {
    const r = await api("/api/workload/primary-port", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ ns, name, port }) });
    toast(r.detail, "ok"); closeModal(); refresh(true);
  } catch (e) { toast(e.message, "bad"); }
};
