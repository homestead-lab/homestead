/* Containers, Deploy, App Store */

async function viewWorkloads() {
  [STATE.data.wl] = await Promise.all([api("/api/workloads"), loadUptime()]);
  renderWorkloads();
  loadImageUpdates(false, true).then(() => {
    if (STATE.view === "workloads" && $("#modal").classList.contains("hidden")) renderWorkloads();
  });
  // Whether Flux is applying each app (#742): the details show it once read.
  gitopsLoad().then(report => {
    if (Object.keys(report?.apps || {}).length && STATE.view === "workloads" && $("#modal").classList.contains("hidden")) renderWorkloads();
  });
  // Old Released volumes of each app's claims (#932): shown once read.
  if (typeof releasedVolumesLoad === "function") releasedVolumesLoad().then(rows => {
    if (rows.length && STATE.view === "workloads" && $("#modal").classList.contains("hidden")) renderWorkloads();
  });
}

/* An app's old Released volumes and the space they hold (storage-leftovers.js). */
const workloadReleasedRow = (w, detail = false) => !remoteRow(w) && typeof appReleasedRow === "function" ? appReleasedRow(w, detail) : "";

const updateKey = (ns, name) => `${ns}/${name}`;
const workloadUpdate = (ns, name) => (STATE.data.imageUpdateMap || {})[updateKey(ns, name)];

/* An app something else updates - Flux, Argo CD, or as an administrator
   marked it (#296). Its update stays a notice; the action is not offered,
   because a change made here would be undone at the owner's next sync. */
const updateOwner = update => update?.managed?.by ? update.managed : null;
function updateOwnerTip(owner) {
  return owner.detected
    ? `Updated by ${owner.by}${owner.source ? ` (${owner.source})` : ""}. Homestead shows when a newer image exists; change it in ${owner.by}.`
    : `Marked as updated elsewhere: ${owner.by}. Homestead shows when a newer image exists; clear the mark under Image updates to update it here.`;
}
function updateOwnerPill(update, slim = false) {
  const owner = updateOwner(update);
  if (!owner) return "";
  return UI.chip(`${update.available ? "update in " : "updated by "}${owner.by}`, "info", { size: slim ? "slim" : "", tip: updateOwnerTip(owner) });
}
window.markUpdatesElsewhere = async (ns, name) => {
  const by = await askText(`Who updates ${name} - Renovate, CI, git? Homestead then shows its updates as a notice and does not offer to update it here.`,
    "", {title:"Updated elsewhere", placeholder:"Renovate", ok:"Mark"});
  if (by === null) return;
  if (!by.trim()) return toast("Say who updates it", "bad");
  await setUpdateOwner(ns, name, by.trim());
};
window.clearUpdatesElsewhere = (ns, name) => setUpdateOwner(ns, name, "");
async function setUpdateOwner(ns, name, by) {
  try {
    const result = await api("/api/image-updates/managed", {method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify({ns, name, by})});
    const row = (STATE.data.imageUpdates?.workloads || []).find(w => w.ns === ns && w.name === name);
    if (row) row.managed = result.managed || {};
    toast(by ? `${name} is marked as updated elsewhere` : `${name} can be updated here again`, "ok");
    if ($("#mtitle")?.textContent === "Image updates") imageUpdateCenter();
    if (STATE.view === "workloads") renderWorkloads();
  } catch (error) { toast(error.message, "bad"); }
}

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

/* What tells one pod of an app from another: the part Kubernetes adds after
   the ReplicaSet hash (#873). A name of 20 characters or fewer, such as most
   StatefulSet pods', is shown whole; the title and the wide layout carry the full one. */
function podShortName(name) {
  const text = String(name || "");
  const last = text.split("-").pop();
  return text.length <= 20 || !last || last === text ? text : `…-${last}`;
}
/* An image as the path a narrow card may cut and the name that keeps its tag,
   a digest shortened in the middle (#873). The full reference goes in a title. */
function imageParts(ref) {
  const text = String(ref || "").replace(/@sha256:([0-9a-f]{4})[0-9a-f]{4,}([0-9a-f]{3})$/, "@sha256:$1…$2");
  const at = text.lastIndexOf("/", text.includes("@") ? text.indexOf("@") : text.length);
  return [text.slice(0, at + 1), text.slice(at + 1)];
}
function treeImage(image) {
  if (!image) return `<div class="dim xs tree-image">Image unavailable</div>`;
  const [path, name] = imageParts(image);
  return `<div class="mono dim tree-image" title="${esc(image)}">${path ? `<span class="tree-image-path">${esc(path)}</span>` : ""}<span class="tree-image-name">${esc(name)}</span></div>`;
}
/* The pod's name: the part that tells it apart, or, in the wide rows layout,
   the whole name where the screen has room for it. */
function treePodName(p, wide) {
  const short = p.hostname || podShortName(p.name);
  const title = p.hostname ? `${p.name} (pod hostname ${p.hostname})` : p.name;
  return wide && short !== p.name
    ? `<span class="mono dim tree-podname wide" title="${esc(title)}"><span class="tree-podname-short">${esc(short)}</span><span class="tree-podname-full">${esc(p.name)}</span></span>`
    : `<span class="mono dim tree-podname" title="${esc(title)}">${esc(short)}</span>`;
}

/* An app's Deployment → pods → containers. Each pod row leads with its host,
   which is never cut (#873); the pod name and image give way instead. */
function workloadHierarchy(w, { wide = false } = {}) {
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
      <div class="tree-pod-head"><span class="tree-branch">Pod</span><b class="tree-node">${esc(p.node || "unscheduled")}</b>
        ${UI.chip(p.terminating ? "stopping" : p.ready ? "ready" : p.phase || "pending", p.terminating ? "neutral" : p.ready ? "ok" : p.phase === "Pending" ? "warn" : "bad",
          { tip: p.terminating ? "Told to stop and not gone yet. One that never started is cleared when the container is stopped or started again" : "" })}
        ${p.restarts ? `<span class="dim xs tree-restarts">${p.restarts} restart${p.restarts === 1 ? "" : "s"}</span>` : ""}
        ${treePodName(p, wide)}</div>
      ${p.pull?.total_bytes ? `<div class="tree-pull">${pullBar(p.pull)}</div>` : ""}
      ${p.unplaced ? `<div class="tree-why unplaced" title="${esc(p.unplaced)}">Cannot be placed: ${esc(p.unplaced)}</div>` : ""}
      <div class="tree-containers">${(p.containers || []).map(c => `<div class="tree-container">
        <span class="tree-branch ${c.kind === "init" ? "init" : ""}">${c.kind === "init" ? "Init" : "Container"}</span>
        <b title="${esc(c.name)}">${esc(c.name)}</b>${UI.chip(c.state || "pending", c.ready || (c.kind === "init" && c.state === "Completed") ? "ok" : "neutral")}
        ${c.restarts ? UI.chip(`${c.restarts} restart${c.restarts === 1 ? "" : "s"}`, "warn") : ""}
      </div>${treeImage(c.image)}${c.message && !c.ready ? `<div class="tree-why" title="${esc(c.detail || c.message)}">${esc(c.message)}${c.detail && c.detail !== c.message
        ? ` <a class="linkish xs" onclick="event.stopPropagation(); modal(${jsq(`Why ${c.name} is not running`)}, '<pre class=&quot;mono small wrap-pre&quot;>' + esc(this.dataset.detail) + '</pre>')" data-detail="${esc(c.detail)}">full message</a>` : ""}</div>` : ""}`).join("") || `<div class="dim xs">Container detail is unavailable for this pod.</div>`}</div>
    </div>`).join("") || `<div class="dim xs">No pods exist yet. Kubernetes creates them once the instance count is above zero.</div>`}</div>
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
    // Without storage, compared with what this page last saw: no failed check,
    // and no repeat of the same toast every quarter of an hour.
    let previous = existing?.updates || 0, previousErrors = existing?.errors || 0;
    try {
      previous = +(localStorage.getItem("homestead.update-count") || 0);
      previousErrors = +(localStorage.getItem("homestead.update-errors") || 0);
    } catch (e) { /* this page's counts */ }
    const preferences = STATE.data.appSettings?.updates || {};
    if (!quiet && preferences.notify_available !== false && report.updates > previous)
      toast(`${report.updates} container image update${report.updates === 1 ? "" : "s"} available`, "ok");
    if (!quiet && preferences.notify_failures !== false && report.errors > previousErrors)
      toast(`${report.errors} image registry check${report.errors === 1 ? " needs" : "s need"} attention`, "bad");
    try {
      localStorage.setItem("homestead.update-count", report.updates || 0);
      localStorage.setItem("homestead.update-errors", report.errors || 0);
    } catch (e) { /* this visit only */ }
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
  // What Homestead updates first; what Flux, Argo CD or a mark says is updated elsewhere after, apart (#296).
  const here = affected.filter(w => !updateOwner(w)), elsewhere = affected.filter(w => updateOwner(w));
  const policy = report.policy || {};
  modal("Image updates", `<div class="update-center">
    ${UI.lead("Choose apps, review the changes, then start the update. Apps something else updates - Flux, Argo CD, or as you marked them - are listed for the notice and updated where they come from.")}
    ${UI.more("Update policy", esc(policy.reason || "Updates require your approval before any app restarts."))}
    ${available.length ? `<div class="update-selectbar">
      <label class="switch"><input type="checkbox" id="updateSelectAll" checked onchange="toggleImageUpdateSelection(this.checked)"> Select all</label>
      <span id="updateSelectedCount">${available.length} of ${available.length} selected</span>
    </div>` : ""}
    ${affected.length ? [here, elsewhere].map((rows, i) => !rows.length ? "" : `${i ? UI.section(`Updated elsewhere · ${rows.length}`, "") : ""}<div class="settings-list">${rows.map(w => {
      const failures = (w.images || []).filter(image => image.error);
      const owner = updateOwner(w), pick = w.available && !owner;
      return `<div class="settings-list-row update-center-row">${pick ? `<label class="update-pick" title="Select ${esc(w.name)}"><input class="update-select" type="checkbox" aria-label="Select ${esc(w.name)}" checked data-ns="${esc(w.ns)}" data-name="${esc(w.name)}" onchange="syncImageUpdateSelection()"><span></span></label>` : '<span class="update-pick-spacer"></span>'}<div><b>${esc(w.name)}</b><div class="dim xs mono">${esc(w.ns)}${updateVersions(w) ? ` · ${esc(updateVersions(w))}` : ""}</div>
        ${owner ? `<div class="dim xs">${esc(updateOwnerTip(owner))}</div>` : ""}
        ${failures.map(image => `<div class="updateerror">${esc(image.container)} · ${esc(image.error)}</div>`).join("")}</div>
        <div class="row">${owner ? updateOwnerPill(w) : w.available ? UI.chip("update available", "warn") : ""}
        ${failures.length ? UI.chip("check failed", "bad") : ""}
        ${owner && !owner.detected ? `<button class="btn sm" data-need="operator" onclick="clearUpdatesElsewhere(${jsq(w.ns)},${jsq(w.name)})">Update here</button>`
          : !owner && w.available ? `<button class="btn sm" data-need="operator" onclick="markUpdatesElsewhere(${jsq(w.ns)},${jsq(w.name)})">Updated elsewhere…</button>` : ""}
        ${owner && w.yaml ? gitopsCopyButton(w.yaml) : ""}
        <button class="btn sm" onclick="openUpdateWorkload(${jsq(w.name)})">Open</button></div></div>`;
    }).join("")}</div>`).join("") : '<div class="empty small">Images are current and registry checks succeeded.</div>'}
    ${UI.actions(available.length ? UI.button(`Review selected (${available.length})`, "imageUpdateBatchReview()", {kind:"pri",id:"updateStage",attrs:'data-need="operator"'}) : "", UI.cancel("Close") + UI.button("Check now", "checkImageUpdates()"))}</div>`, true);
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
    stage.textContent = `Review selected (${selected})`;
  }
};
window.toggleImageUpdateSelection = checked => {
  document.querySelectorAll("#mbody .update-select").forEach(box => { box.checked = checked; });
  syncImageUpdateSelection();
};
window.imageUpdateBatchReview = () => {
  const keys = new Set($$(".update-select:checked").map(box => updateKey(box.dataset.ns, box.dataset.name)));
  const items = HomesteadUpdateState.availableWorkloads(STATE.data.imageUpdates).filter(item => keys.has(updateKey(item.ns, item.name)));
  if (!items.length) return toast("Select at least one update to review", "bad");
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
  modal(`Rename ${g}`, `<div class="ui-stack">
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
  modal(`Ungroup ${g}`, `<div class="ui-stack">
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
  modal(`Tier for ${group}`, `<div class="ui-stack">
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
      : `<span class="mono">${esc(r.from)} → ${esc(r.to)}</span> <span class="sub">${r.reserves ? `${esc(r.cpu_m)}m CPU · ${esc(r.mem_mb)} MiB reserved` : "reserves nothing"}</span>`,
    hostSelect(MV.over[r.id] || MV.to, `mvOverride(${jsq(r.id)},this.value)`, `Host for ${r.id}`)]);
  const notice = !moves.length ? UI.callout("bad", "Can't move", "Choose another host for them, or untick them.")
    : stays.length || (plan.hosts || []).some(h => h.over) ? UI.callout("warn", "Check first",
      `${stays.length ? `${stays.length} stay where they are. ` : ""}Each one moved restarts on its new host.`) : "";
  $("#mbody").innerHTML = [
    UI.lead(!moves.length ? "None of these can move to the hosts chosen; each one says why."
      : `Moves ${moves.length} container${moves.length === 1 ? "" : "s"} to ${esc([...new Set(moves.map(m => m.to))].join(", "))}; each restarts there, one at a time.`),
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
  const items = [{ label: "Check for image updates", icon: "refresh", run: "checkImageUpdates()", tip: "Ask the registries for newer images" },
    { label: "Manage groups", icon: "list", run: "manageWorkloadGroups()", need: "operator" },
    { label: "If a host fails", icon: "node", run: "wlFailover()", tip: "What each container does when its host fails: move, or wait for the host" },
    { label: "Rebalance containers…", icon: "move", run: "containerRebalance()", need: "operator", tip: "Move containers so hosts carry similar CPU and memory" },
    { label: "Move several…", icon: "move", run: "moveSeveral()", need: "operator", tip: "Tick containers and move them to a host you choose, reviewed together" },
    { label: "Set reservations from use…", icon: "sliders", run: "reservationsReview()", need: "operator", tip: "Reserve CPU and memory for apps from what they used over the last week" },
    { label: "Priority tiers…", icon: "list", run: "priorityReview()", need: "operator", tip: "Which apps fit first when a host fails and the others are short of room" },
    { label: "Logos for apps without one", icon: "logo", run: "logoFixup()", need: "operator", tip: "Match apps with no logo to the app store's" },
    { label: "Change history", icon: "clock", run: "wlHistory()", tip: "Every app's changes: who, when and what" },
    { label: "Search logs", icon: "search", run: "logSearch()", tip: "Find a line in every app's recent logs" },
    { label: "Log alerts", icon: "bell", run: "logAlerts()", tip: "Saved log searches that raise an alert when they match too many lines" },
    { label: "Power schedules", icon: "clock", run: "schedulesOverview()", tip: "Apps and VMs that stop and start on their own" }];
  const logoless = all.filter(logoCanHave).length;
  const updateButtons = `${updateCount ? `<button class="pill warn pillbtn" title="Review and stage image updates" onclick="imageUpdateCenter()">${updateCount} update${updateCount === 1 ? "" : "s"}</button>` : ""}
    ${updateErrors ? `<button class="pill crit pillbtn" data-tip="${updateErrors} image${updateErrors === 1 ? "" : "s"} could not be compared with ${updateErrors === 1 ? "its" : "their"} registry; every other image was" onclick="imageUpdateCenter()">${updateErrors} check${updateErrors === 1 ? "" : "s"} failed</button>` : ""}`.trim();
  const deploy = '<button class="btn pri" data-need="operator" onclick="go(\'deploy\')">＋ Deploy</button>';
  paint(`<div class="containers-page collection-page" data-collection="containers">${UI.pageHeader(`Containers`, `${rows.length} app${rows.length === 1 ? "" : "s"}${q ? ` matching “${esc(q)}”` : ""}${group ? ` in ${esc(group === NO_GROUP ? "no group" : group)}` : ""} · ${platform.length
        ? `<a class="linkish" onclick="togglePlatformContainers()" data-tip="Homestead and the helpers it runs - updated under Settings › Updates - and KubeVirt, CDI and the like, run by their own operators and upgraded under System → Cluster">${platformShown() ? "hide" : "show"} ${platform.length} platform container${platform.length === 1 ? "" : "s"}</a>`
        : "system pods hidden"}${unchecked ? ` · <span data-tip="Marked ? in the list: stopped since Homestead started, so not yet compared with their registries">${unchecked} not checked yet</span>` : report && !updateCount && !updateErrors ? " · images current" : ""}${logoless ? ` · <a class="linkish" data-need="operator" onclick="logoFixup()">${logoless} without a logo</a>` : ""}${metricsNotice(STATE.data.wl)}`, `<span class="dim xs scanprogress" id="scanprogress"></span>${updateButtons}
      ${layoutSwitch("containers", "renderWorkloads")}
      ${moreMenu(items)}
      ${deploy}`, {extraHtml:`${all.length ? workloadGroupBar(all, group) : ""}`})}
    ${UI.collectionHeader(`${workloadGroupSelect(all, group)}${workloadListOptions(platform, layout, items)}${deploy}`, `<span>${rows.length} container${rows.length === 1 ? "" : "s"}</span>${updateButtons ? `<span aria-hidden="true">·</span>${updateButtons}` : ""}
        ${unchecked ? `<span class="dim" data-tip="Stopped or still starting; not yet compared with their registries">· ${unchecked} not checked yet</span>` : report && !updateCount && !updateErrors ? '<span class="dim">· images current</span>' : ""}${logoless ? `<span class="dim">·</span> <a class="linkish" data-need="operator" onclick="logoFixup()">${logoless} without a logo</a>` : ""}`)}

    ${uptimeUnreadNote()}
    ${rows.length ? workloadSections(rows, layout, group)
      : `<div class="empty">${q || group ? "Nothing matches that search." : "Nothing deployed yet."}</div>`}</div>`);
}

/* An image with no tag is Docker's latest; saying so is clearer than leaving it off. */
function imageLabel(ref) {
  if (!ref || ref.includes("@")) return ref || "";
  return ref.split("/").pop().includes(":") ? ref : `${ref}:latest`;
}

/* The part of an image people judge an update by: its name and tag, or the
   start of its digest. The full reference goes in a title beside it. */
function imageShortLabel(ref) {
  const label = imageLabel(ref), name = label.split("/").pop();
  const pinned = name.match(/^(.*@sha256:)([0-9a-f]+)$/);
  return pinned ? `${pinned[1]}${pinned[2].slice(0, 12)}${pinned[2].length > 12 ? "…" : ""}` : name;
}
const imageTitle = images => esc((images || []).map(imageLabel).join(" · "));
const imageShort = images => (images || []).map(i => esc(imageShortLabel(i))).join(" · ");

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
    update?.available && !updateOwner(update) && item("Update", `imageUpdateReview(${ns},${name})`, "update", {pri:true,need:"operator"}),
    logs,
    // Not the magnifying glass: Search logs has that, in the same menu.
    item("Diagnostics", `wlDiagnostics(${ns},${name})`, "bug", {tip:"Exit details, previous logs and reviewed commands"}),
    off ? item("Start", `wlScale(${ns},${name},1)`, "play", {need:"operator"}) : restart,
    !off && item("Stop", w.self ? `wlStopSelf(${ns},${name})` : `wlScale(${ns},${name},0)`, "stop", {need:"operator"}),
    item("Console", `wlConsole(${ns},${name})`, "console", {need:"operator",tip:"Open an audited shell in a running container"}),
    item("Edit", `wlEdit(${ns},${name})`, "edit", {need:"operator"}),
    // Reconcile now (#742): only for an app Flux applies, and only for an admin.
    gitopsReconcileItem(w),
    FLEET.view?.linked && !w.self && item("Move to cluster", `moveToCluster('container',${name},${jsq(w.site?.handle || "")},'move',${jsq(w.ns)})`, "move", {need:"admin"}),
    FLEET.view?.linked && !w.self && item("Copy to cluster", `moveToCluster('container',${name},${jsq(w.site?.handle || "")},'copy',${jsq(w.ns)})`, "copy", {need:"admin"}),
    (w.ports || []).length > 1 && item("Main port", `wlPrimaryPort(${ns},${name})`, "ext"),
    item("Placement", `wlPlacement(${ns},${name})`, "node"),
    item("Group", `wlGroup(${ns},${name})`, "list", {need:"operator"}),
    item("Logo", `wlLogo(${ns},${name})`, "logo", {need:"operator",tip:"Pick a logo from the app store"}),
    item("Monitoring", `wlMonitoring(${ns},${name})`, "pulse", {tip:"Whether it is up at its address, checked every minute"}),
    item("History", `wlHistory(${ns},${name})`, "clock", {tip:"Who changed it, when and what, with Undo"}),
    item("Search logs", `logSearch(${ns},${name})`, "search", {tip:"Find a line in its recent logs"}),
    !w.self && item("Schedule", `wlSchedule(${ns},${name})`, "clock", {need:"operator",tip:"Stop and start it at set times"}),
    !updateOwner(update) && item("Updates", `wlUpdateMode(${ns},${name})`, "update", {need:"operator",tip:"Update it yourself, or let it update itself in the maintenance window"}),
    item("Move", `moveWorkload(${name},${ns})`, "move", {need:"operator"}),
    update?.can_rollback && item("Rollback", `imageRollback(${ns},${name})`, "rollback", {need:"operator"}),
    item("Delete", `wlDelete(${ns},${name})`, "trash", {danger:true,need:"operator"})
  ]);
}

/* Starts after (#746): names in words, and a time in seconds or minutes. */
const startsWords = names => { const list = [...names]; return list.length < 2 ? list.join("") : `${list.slice(0, -1).join(", ")} and ${list.at(-1)}`; };
const startsSpan = seconds => seconds < 60 ? `${Math.max(0, Math.round(seconds))} s` : `${Math.round(seconds / 6) / 10} min`;
/* What a row says about the apps it starts after: {chip, tone, line} while
   it waits, when it started without an app, or when
   its gate or setting is not as saved; null when there is nothing to say. */
function startWaitText(w, now = Date.now() / 1000) {
  const setting = w.start_setting || {}, wait = w.start_wait || {}, timeout = setting.timeout;
  const said = line => ({ chip: "", tone: "", line });
  // #620: a setting the server could not work out is "could not read", never none.
  if (w.start_note) return said("Could not read its Starts after setting");
  if (w.start_unreadable) return said("Its Starts after setting can't be read: set it again in Edit");
  if (wait.waiting_for?.length) {
    const so = timeout ? `${startsSpan(now - wait.since)} of ${startsSpan(timeout)}` : `${startsSpan(now - wait.since)} so far`;
    return { chip: "Waiting", tone: "warn", line: `Waiting for ${startsWords(wait.waiting_for)} (${so})` };
  }
  if (wait.started_without?.length) {
    const names = startsWords(wait.started_without);
    return said(`Started without ${names} at ${fmtClock(wait.at)}: ${names} ${wait.started_without.length > 1 ? "were" : "was"} not ready${timeout ? ` after ${startsSpan(timeout)}` : ""}`);
  }
  if (w.start_gate === "missing" && setting.managed) return said("Its start gate isn't committed yet: commit the lines Edit shows");
  if (w.start_gate === "differs" || w.start_gate === "missing") return said("Its start gate doesn't match its setting: put it back in Edit → Placement");
  return null;
}
/* The apps it starts after, the ones that no longer exist marked. */
function startsAfterNames(w) {
  const after = w.start_setting?.after;
  return after ? after.map(a => `${a.app}${a.gone ? " (gone)" : ""}`).join(", ") : (w.starts_after || []).join(", ");
}
/* Start's lead: the stopped apps a start of this one would wait for, or
   that it could not check (#620). */
function startWaitsLine(name, waits, timeout, onTimeout = "start", error = "") {
  if (error) return ` ${error[0].toUpperCase()}${error.slice(1)}.`;
  if (!waits?.length) return "";
  const many = waits.length > 1;
  const until = onTimeout === "wait" ? `until ${many ? "they are" : "it is"} up` : timeout ? `up to ${startsSpan(timeout)}` : "before it starts";
  return ` ${startsWords(waits)} ${many ? "are" : "is"} stopped: ${name} will wait for ${many ? "them" : "it"} ${until}.`;
}
/* The app's details: Starts after, and what its start is waiting on. */
function startsAfterFact(w) {
  const names = startsAfterNames(w), said = startWaitText(w);
  if (!names && !said) return "";
  return `<div class="wl-detail-starts"><span>Starts after</span><b>${esc(names || "—")}</b>${said ? `<div class="dim xs">${esc(said.line)}</div>` : ""}</div>`;
}
const startWaitChip = w => { const said = startWaitText(w); return said?.chip ? UI.chip(said.chip, said.tone, { size: "slim", tip: said.line }) : ""; };
window.startWaitText = startWaitText;

/* A workload's state tags, the same in a row and a card so the two can't
   drift: its schedule, how it updates, who updates it, and an update waiting.
   A row whose own Update button is shown says the update there, not twice. */
function workloadStateTags(w, update, { row = false } = {}) {
  const owner = updateOwner(update);
  const button = row && update?.available && !owner && (typeof can !== "function" || can("operator"));
  return `${startWaitChip(w)}${scheduleTag(w)}${autoUpdateTag(w)}${owner ? updateOwnerPill(update, row)
    : update?.available && !button ? UI.chip("update available", "warn") : ""}${uncheckedMark(update)}`;
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
          <div class="row">${w.platform ? platformTag(w) : ""}${w.managed_smb || w.managed_nfs ? UI.chip(`managed ${w.managed_nfs ? "NFS" : "SMB"}`, "info", { size: "slim", tip: "Managed by Homestead under Network Shares" }) : ""}${workloadStateTags(w, update)}
          ${updateError ? `<span class="tip warn-tip" tabindex="0" role="img" aria-label="Registry check unavailable: ${esc(updateError.error)}" data-tip="Registry check unavailable — ${esc(updateError.error)}">!</span>` : ""}
          ${UI.chip(`${w.ready}/${w.desired}`, ok ? "ok" : off ? "neutral" : "bad")}</div>
        </div>
        <div class="wmeta">
          <div><div class="dim xs">UPTIME</div>${w.uptime ? upChip(w.uptime) : '<span class="dim">—</span>'}</div>
          <div><div class="dim xs" data-tip="Live usage. 100% equals one fully used CPU core.">CPU</div><div class="mono small">${workloadCpuPercent(w.cpu)}</div></div>
          <div><div class="dim xs" data-tip="Memory in use right now">RAM</div><div class="mono small">${workloadMemory(w.mem_mb)}</div></div>
          <div><div class="dim xs">ACCESS</div><div class="waccess">${accessPorts(w.ports)}</div></div>
        </div>
        <div class="dim xs mono wimg"><span class="wimage-name" title="${imageTitle(w.images)}">${imageShort(w.images)}</span>
          <span class="wimage-hardware">${hardwareTags(w.hardware || (w.gpu ? ["igpu"] : []))}</span></div>
        ${answerCardRow(w)}
        ${workloadReleasedRow(w)}
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
    return `<tbody class="grouphead"><tr><td colspan="8">${workloadGroupHead(name, members, shut)}</td></tr></tbody>
      <tbody${shut ? " hidden" : ""}>${workloadTableRows(members)}</tbody>`;
  }).join("") : `<tbody>${workloadTableRows(rows)}</tbody>`;
  return `<div class="card flat pad0 wltable-wrap"><table class="tbl dense stack compact wltable" data-sort="containers" data-sort-controls="containers"><thead><tr>
    <th>App</th><th>Status</th><th class="wl-image">Image</th><th>CPU</th><th>RAM</th><th class="wl-access">Access</th><th class="wl-uptime">Uptime</th><th data-nosort>Actions</th></tr></thead>
    ${bodies}</table></div>`;
}

/* An image not yet compared with its registry is one quiet mark, its reason
   on hover; the page subtitle gives the count. */
function uncheckedMark(update) {
  if (!update?.unchecked) return "";
  const tip = update.images?.some(i => i.rate_limited)
    ? "Image not checked yet: its registry asked Homestead to slow down. It is checked again within half an hour."
    : update.images?.some(i => i.starting)
    ? "Image not checked yet: still starting. It is compared with the registry once it runs."
    : "Image not checked yet: stopped, and not seen running here. It is compared with the registry once it has run.";
  return `<span class="tip unchecked-tip" tabindex="0" role="img" aria-label="${tip}" data-tip="${tip}">?</span>`;
}

/* A container's volume, on the Volumes page: filtered to it, with the
   page's own "Showing matches" bar to clear it again. */
window.openWorkloadVolume = claim => {
  STATE.q = claim;
  const search = $("#globalSearch");
  if (search) search.value = claim;
  go("storage", { keepSearch: true });
};

const workloadExpanded = new Set();
const workloadRowKey = w => JSON.stringify([w.site?.id || "", w.ns, w.name]);
function workloadTableRows(rows) {
  return rows.map(w => {
    const ok = w.ready === w.desired && w.desired > 0, off = w.desired === 0;
    const key = workloadRowKey(w), id = `wl-detail-${encodeURIComponent(key)}`, open = workloadExpanded.has(key);
    const uptime = !off && Number.isFinite(w.uptime) && w.uptime > 0 ? w.uptime : null;
    const update = w.platform || w.managed_smb || w.managed_nfs || remoteRow(w) ? null : workloadUpdate(w.ns, w.name);
    const updateError = update?.images?.find(x => x.error), pull = workloadPull(w), blocked = workloadBlocked(w);
    const quiet = off && !update?.unchecked && !updateError && !update?.available && !pull && !blocked;
    const disclosure = collectionDisclosure({label:`Details for ${w.name}`,id:`wl-toggle-${encodeURIComponent(key)}`,controls:id,expanded:open,
      run:`workloadToggleDetails(${jsq(key)},this)`,bodyHtml:`${appAvatar(w.name, w.icon)}<span class="wtitle"><b>${esc(w.name)}</b> ${clusterTag(w)}</span>`});
    return `<tr class="wl-row${off ? " wl-off" : ""}${quiet ? " wl-quiet" : ""}${open ? " wl-expanded" : ""}" data-row-key="${esc(key)}"${clusterAttr(w)}>
      <td class="wl-name" data-sort="${esc(w.name)}">${disclosure}
        <div class="wl-row-meta dim xs">${w.platform ? `${platformTag(w)} ` : ""}${w.managed_smb || w.managed_nfs ? `${UI.chip(`managed ${w.managed_nfs ? "NFS" : "SMB"}`, "info", { size: "slim" })} ` : ""}${esc(w.ns)} · ${esc(off ? "stopped" : (w.nodes || []).join(", ") || "unscheduled")}</div></td>
      <td class="wl-status" data-status data-sort="${off ? -1 : w.desired ? w.ready / w.desired : 0}"><div class="row wl-state-tags">
        ${UI.chip(off ? "Stopped" : `${w.ready}/${w.desired}`, ok ? "ok" : off ? "neutral" : "bad", { size: "slim", tip: `${w.ready} of ${w.desired} ready` })}
        ${answerTag(w)}${workloadStateTags(w, update, { row: true })}
        ${updateError ? `<span class="tip warn-tip" tabindex="0" role="img" aria-label="Registry check unavailable: ${esc(updateError.error)}" data-tip="Registry check unavailable — ${esc(updateError.error)}">!</span>` : ""}
        ${pull ? UI.chip(`Pulling ${Math.min(100, pull.percent || 0)}%`, "neutral", { tip: `Fetching ${pull.image || "image"}` }) : ""}
        ${blocked ? UI.chip("Blocked", "bad", { tip: blocked }) : ""}</div></td>
      <td class="wl-image" data-sm-hide><div class="mono xs wl-imagetext" title="${imageTitle(w.images)}">${imageShort(w.images)}</div>
        ${hardwareTags(w.hardware || (w.gpu ? ["igpu"] : []))}</td>
      <td class="mono small nowrap wl-cpu" data-sort="${off ? "" : w.cpu}" data-tip="Live usage. 100% equals one fully used CPU core.">${off ? "—" : workloadCpuPercent(w.cpu)}</td>
      <td class="mono small nowrap wl-ram" data-sort="${off ? "" : w.mem_mb}">${off ? "—" : workloadMemory(w.mem_mb)}</td>
      <td class="wl-access" data-sm-hide><div class="waccess">${accessPorts(w.ports)}</div></td>
      <td class="mono small nowrap wl-uptime" data-sm-hide data-sort="${uptime ?? ""}">${uptime === null ? "—" : esc(fmtUp(uptime))}</td>
      <td class="wl-actions" data-actions><div class="wacts">${workloadActions(w, update, off, true)}</div></td>
    </tr>
    <tr class="wl-detail-row" data-detail-for="${esc(key)}"${clusterAttr(w)}${open ? "" : " hidden"}><td colspan="8">
      <div class="wl-inline-detail" id="${esc(id)}">
        ${blocked ? `<div class="wblocked">${esc(blocked)}</div>` : ""}${pull ? pullBar(pull) : ""}
        <div class="about-grid">
          <div><span>Host</span><b>${esc((w.nodes || []).join(", ") || "Unscheduled")}</b></div>
          <div><span>Namespace · uptime</span><b>${esc(w.ns)} · ${esc(off ? "Stopped" : w.uptime ? fmtUp(w.uptime) : "Starting")}</b></div>
          <div class="wl-detail-image"><span>Image</span><b class="mono small">${w.images.map(esc).join("<br>") || "—"}</b><div class="wl-detail-hardware">${hardwareTags(w.hardware || (w.gpu ? ["igpu"] : []))}</div></div>
          <div class="wl-detail-access"><span>Access</span><b class="waccess">${accessPorts(w.ports)}</b></div>
          ${(w.claims || []).length && !remoteRow(w) ? `<div class="wl-detail-volumes"><span>Volumes</span><b class="row">${w.claims.map(claim =>
            `<button type="button" class="tag info linkish" title="Open ${esc(claim)} in Volumes" onclick="openWorkloadVolume(${jsq(claim)})">${esc(claim)}</button>`).join("")}</b></div>` : ""}
          ${(released => released ? `<div class="wl-detail-released"><span>Old volumes</span>${released}</div>` : "")(workloadReleasedRow(w, true))}
          ${gitopsDetail(w)}
          ${startsAfterFact(w)}
        </div>
        ${workloadHierarchy(w, { wide: true })}
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
  // null: the Services couldn't be read, which is not "no listeners".
  if (ports === null) return `<span class="dim" data-tip="Its Services couldn't be read just now, so where it listens is not known.">couldn't read</span>`;
  const rows = ports || [];
  if (!rows.length) return '<span class="dim">—</span>';
  const shown = rows.slice(0, 2);
  // A narrow card has room for one port, a wide one for two; each size gets
  // its own "+N" so the count is right whichever one is showing.
  const more = (from, kind) => rows.length > from
    ? `<span class="tag more ${kind}" data-tip="Also listening on ${esc(rows.slice(from).map(p => p.port).join(", "))}">+${rows.length - from}</span>` : "";
  return shown.map((p, i) => (p.ip
    ? `<button type="button" class="plink${i ? " second" : ""}" title="Open ${esc(svcUrl(p.ip, p.port))}" aria-label="Open port ${p.port}" onclick="openSvc(${jsq(p.ip)},${p.port})">${p.port}<svg class="ext" width="9" height="9" aria-hidden="true"><use href="#i-ext"/></svg></button>`
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
    detail = "This app is pinned to this host. It cannot launch on another host.";
  } else if (placement.preferred) {
    const available = hosts.some(host => host.name === placement.preferred);
    title = `${available ? "Preferred host" : "Preferred host currently unavailable"}: ${placement.preferred}`;
    detail = "Kubernetes can choose another eligible host if needed.";
  } else if (hosts.length === 1) {
    title = `Only eligible host: ${hosts[0].name}`;
    detail = "Kubernetes can schedule it here if these constraints still hold at launch.";
  } else {
    title = hosts.length ? "Host selected at launch" : "Launch host not determined";
    detail = hosts.length ? "Kubernetes chooses from the eligible hosts below when the app starts." : "No eligible host has been confirmed by this review.";
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

/* The review before something starts - a container's pods or a VM: one
   sentence on what starts where, a warning only when there is one, each host
   with the memory it would be left with, and one tickbox to accept a
   warning. How it was worked out stays under Details. */
/* Each host with the memory it would be left with, and why a host is out. */
function capacityHostTable(plan) {
  const placement = plan.placement || {}, hosts = plan.candidates || [];
  const memory = host => !host.metrics_available || host.projected_percent == null
    ? '<span class="ui-help">memory use unknown</span>'
    : `${UI.meter({ now: host.capacity_gb ? host.used_gb / host.capacity_gb * 100 : 0, after: host.projected_percent, label: `${host.name} memory` })}
       <span class="sub">${esc(host.projected_gb)} of ${esc(host.capacity_gb)} GiB after · ${esc(host.projected_percent)}%</span>`;
  const tag = host => placement.resident === host.name ? UI.chip("current host", "info")
    : placement.pinned === host.name ? UI.chip("pinned", "info")
    : placement.preferred === host.name ? UI.chip("preferred", "info") : "";
  return UI.table([{ label: "Host" }, { label: "Memory after it starts", className: "grow" }], [
    ...hosts.filter(host => host.eligible).map(host => [`<span class="mono">${esc(host.name)}</span> ${tag(host)}`, memory(host)]),
    ...hosts.filter(host => !host.eligible).map(host => [`<span class="mono">${esc(host.name)}</span>`,
      `<span class="ui-help">can't run here: ${esc((host.reasons || []).join(" · ") || "not eligible")}</span>`])]);
}
window.capacityHostTable = capacityHostTable;

/* The acknowledgement a review asks for, from what it does: "Restarts" asks
   "Restart it anyway", never "Start it anyway". */
const reviewAck = what => `${String(what || "Starts").split(" ")[0].replace(/s$/, "")} it anyway`;
window.reviewAck = reviewAck;
function startReview(plan, { what = "Starts", name, extra = "", ackId, onAck = "", details = "", objects = "", warnings = [] }) {
  const { concerns, caveats } = capacityNotes(plan);
  const placement = plan.placement || {}, hosts = plan.candidates || [];
  const ok = hosts.filter(host => host.eligible), out = hosts.filter(host => !host.eligible);
  const fixed = placement.resident || placement.pinned || plan.vm?.resident_node;
  const where = fixed ? ` on <b>${esc(fixed)}</b>`
    : ok.length === 1 ? ` on <b>${esc(ok[0].name)}</b>, the only host that can run it`
    : placement.preferred && ok.some(host => host.name === placement.preferred) ? ` on <b>${esc(placement.preferred)}</b> if it can, or another host`
    : ok.length ? ` on one of ${ok.length} hosts` : "";
  const topology = plan.topology_status === "blocked" ? "Its affinity and spread rules cannot fit all requested replicas."
    : plan.topology_status && !["fits", "not-needed"].includes(plan.topology_status) ? "Its affinity and spread placement remains unverified." : "";
  const problems = [...new Set([...(plan.blockers || []), ...concerns,
    ...(plan.unbounded?.length ? [`${plan.unbounded.join(", ")} ${plan.unbounded.length === 1 ? "has" : "have"} no memory limit, so it could use more than estimated`] : []),
    ...(topology ? [topology] : []), ...warnings])];
  const verb = reviewAck(what).split(" ")[0].toLowerCase();
  const list = problems.length ? `<ul class="ui-list">${problems.map(p => `<li>${esc(p)}</li>`).join("")}</ul>` : "";
  const rows = hosts.length;
  const reservations = ok.filter(host => host.reservations_known).map(host =>
    `<li>${esc(host.name)}: Already reserved: ${esc(host.reserved_gb)} / ${esc(host.allocatable_gb ?? "?")} GiB RAM · ${esc(host.reserved_cpu_percent ?? "?")}% CPU${host.request_slots != null ? ` · room for ${esc(host.request_slots)} more` : ""}</li>`)
    .concat(ok.filter(host => !host.reservations_known).map(host => `<li>${esc(host.name)}: Scheduler reservations unavailable</li>`));
  return [
    UI.lead(plan.blocked ? `<b>${esc(name)}</b> can't ${verb} until what is below is resolved.`
      : `${what} <b>${esc(name)}</b>${where}.${extra}`),
    plan.blocked ? UI.callout("bad", `Can't ${verb}`, list || "No host that may run it has room for it.")
      : problems.length ? UI.callout("warn", "Check first", list) : "",
    objects,
    rows ? UI.section("Hosts with room", capacityHostTable(plan)) : "",
    UI.more("Details", `${UI.facts([
        [plan.vm ? "VM reserves" : `Each pod reserves`, `${esc(plan.pod_request_gb ?? "?")} GiB RAM${plan.vm?.request_is_lower_bound ? " at least" : ""} · ${esc(plan.pod_cpu_request_percent ?? "?")}% CPU`],
        ["Memory estimate", plan.pod_memory_gb ? `${esc(plan.pod_memory_gb)} GiB${plan.vm ? " with its launcher" : " per pod"}` : "unknown"],
        plan.additional > 1 ? ["Pods starting", esc(plan.additional)] : null])}
      ${reservations.length ? `<ul class="ui-list">${reservations.join("")}</ul>` : ""}
      ${caveats.length ? `<ul class="ui-list">${caveats.map(c => `<li>${esc(c)}</li>`).join("")}</ul>` : ""}
      ${details}
      <p class="ui-help">Memory after it starts is the greater of live use and what is already reserved, plus this estimate. It is a snapshot, not a reservation: other starts can still change where it lands. The server checks again before anything starts.</p>`),
    !plan.blocked && problems.length ? UI.ack(ackId, reviewAck(what), { onchange: onAck }) : "",
  ].join("");
}
window.startReview = startReview;

window.wlScale = async (ns, name, n) => {
  if (n > 0) {
    try {
      const plan = await api(`/api/workloads/start-plan?${new URLSearchParams({ ns, name, replicas: n })}`);
      // Starts after (#746): the apps it starts after that are stopped, which it would wait for.
      const waits = plan.waits_for || [];
      if (plan.requires_confirmation || plan.blocked || waits.length || plan.waits_for_error) {
        const pods = plan.additional > 1 ? `${plan.additional} pods of` : "";
        const row = waits.length ? (STATE.data.wl || []).find(x => x.ns === ns && x.name === name) : null, setting = row?.start_setting;
        modal(`Start ${name}`, startReview(plan, { what: `Starts ${pods}`.trim(), name, ackId: "wl_capacity_ok",
          extra: esc(startWaitsLine(name, waits, setting?.timeout, setting?.on_timeout, plan.waits_for_error)) })
          + UI.actions(plan.blocked ? UI.cancel("Close")
            : UI.cancel() + UI.button("Start", `wlScaleGo(${jsArg(ns)},${jsArg(name)},${n},${!!plan.requires_confirmation},true)`, { kind: "pri" })));
        return;
      }
    } catch (e) { return toast(`Could not check host memory: ${e.message}`, "bad"); }
  }
  // Stopping is asked first: whatever it serves goes away until it is started.
  if (!n && !(await ask(`Stop ${name}? Its pods stop and what it serves goes away until you start it again. Its data and settings are kept.`, { title: "Stop", ok: "Stop" }))) return;
  return wlScaleGo(ns, name, n, false);
};
window.wlScaleGo = async (ns, name, n, confirmed = false, reviewed = false) => {
  if (confirmed && $("#wl_capacity_ok") && !$("#wl_capacity_ok").checked) return toast("Tick Start it anyway to accept the warning", "bad");
  try {
    await api("/api/scale", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ ns, name, replicas: n, confirm_capacity: confirmed }) });
    if (confirmed || reviewed) closeModal();
    toast(`${name} ${n ? "started" : "stopped"}`, "ok"); setTimeout(() => refresh(true), 900);
  } catch (e) { toast(e.message, "bad"); }
};
/* Balancing hosts, on demand: containers by CPU and memory - each move a
   restart - then volume copies by space - none restart. Each step has its own
   plan, a switch to leave that part out, and a tickbox per container or app
   that plans it again without it; the review starts what is left. */
const BALANCE = {};
const balanceReset = () => {
  BALANCE.containers = { on: true, exclude: new Set(), plan: null };
  BALANCE.volumes = { on: true, exclude: new Set(), plan: null, follow: true };
};
window.balanceHosts = async (start = "containers") => {
  balanceReset();
  const loading = text => `<div class="empty"><span class="spin2"></span> ${esc(text)}</div>`;
  modal("Balance hosts", UI.sectionForm("balance", [
    { key: "containers", title: "Containers", html: `<div id="balContainers">${loading("Working out container moves…")}</div>` },
    { key: "volumes", title: "Volume copies", html: `<div id="balVolumes">${loading("Working out volume copy moves…")}</div>` },
    { key: "review", title: "Review", html: '<div id="balReview"></div>' },
  ], UI.button("Start balancing", "balanceStart()", { kind: "pri", id: "balanceGo", attrs: 'data-need="operator"' })), true);
  if (start !== "containers") UI.selectSection("balance", start);
  balanceReview();
  await balancePlan("containers");
  await balancePlan("volumes");
};
window.containerRebalance = () => balanceHosts("containers");
window.volumeRebalance = () => balanceHosts("volumes");

async function balancePlan(kind) {
  const b = BALANCE[kind], pane = $(kind === "containers" ? "#balContainers" : "#balVolumes");
  if (!pane) return;
  const path = kind === "containers" ? "/api/workloads/rebalance/plan" : "/api/longhorn/rebalance/plan";
  // Volume copies follow the containers moved, so each reads locally on its new host.
  const follow = kind === "volumes" && b.follow ? balanceMoves("containers").map(m => `${m.id}:${m.to}`).join(",") : "";
  try { b.plan = await api(`${path}?${new URLSearchParams({ exclude: [...b.exclude].join(","), ...(follow ? { follow } : {}) })}`); }
  catch (e) { b.plan = null; pane.innerHTML = UI.callout("bad", "Could not work out a plan", esc(e.message)); balanceReview(); return; }
  if (!$(kind === "containers" ? "#balContainers" : "#balVolumes")) return;
  pane.innerHTML = kind === "containers" ? balanceContainersHtml(b) : balanceVolumesHtml(b);
  balanceReview();
  window.applyRole?.();
}

const balanceSwitch = (kind, b, label) => `<label class="switch balance-switch"><input type="checkbox" ${b.on ? "checked" : ""}
  onchange="balanceToggle(${jsq(kind)},this.checked)"> ${esc(label)}</label>`;
const balanceTick = (kind, id, i, off) => `<label class="rb-app"><input type="checkbox" id="bal_${kind}_${i}" ${off ? "" : "checked"}
  onchange="balanceItem(${jsq(kind)},${jsq(id)},this.checked)"> <span class="mono">${esc(id)}</span></label>`;
const balanceSkipped = rows => rows.length ? UI.more(`Not moved · ${rows.length}`,
  `<ul class="ui-list">${rows.map(([id, why]) => `<li><span class="mono">${esc(id)}</span>: ${esc(why)}</li>`).join("")}</ul>`) : "";

/* The plan works from metrics-server's figures as Homestead last read them
   (#928): say when they are missing, still being read after a start, or
   old. One callout. */
function balanceMetrics(plan) {
  const m = plan.metrics_state;
  if (plan.metrics === false && m?.state === "waiting")
    return UI.callout("warn", "Still reading CPU and memory", "Homestead has just started and has not had metrics-server's first figures yet, so the plan may be off. Open it again in a few seconds.");
  if (plan.metrics === false)
    return UI.callout("warn", "Usage is partly unknown", esc(`A host or its apps report no live CPU and memory${m?.detail ? ` (${m.detail})` : ""}, so the plan may be off. Check metrics-server.`));
  if (m?.state === "stale")
    return UI.callout("warn", `CPU and memory from ${fmtAgo(m.age_s)}`, esc(`metrics-server has not answered since then${m.detail ? ` (${m.detail})` : ""}, so the plan uses its last figures and may be off.`));
  return "";
}

function balanceContainersHtml(b) {
  const plan = b.plan, moves = plan.moves || [];
  const meter = (before, after, label) => `${UI.meter({ now: before, after: after !== before ? after : null, warnAt: 80, label })}
    <span class="sub">${esc(label)} ${before}%${after !== before ? ` → ${after}%` : ""}</span>`;
  const hosts = UI.table([{ label: "Host" }, { label: "CPU", className: "grow" }, { label: "Memory", className: "grow" }], (plan.hosts || []).map(h => [
    `<span class="mono">${esc(h.name)}</span>${h.takes ? "" : ` ${UI.chip("takes no new containers", "warn")}`}`,
    meter(h.cpu_before, b.on ? h.cpu_after : h.cpu_before, "CPU"), meter(h.mem_before, b.on ? h.mem_after : h.mem_before, "Memory")]));
  const rows = (plan.apps || []).map((id, i) => {
    const m = moves.find(x => x.id === id), off = b.exclude.has(id);
    return [balanceTick("containers", id, i, off), off || !m ? '<span class="ui-help">stays where it is</span>'
      : `<span class="mono">${esc(m.from)} → ${esc(m.to)}</span> <span class="sub">${esc(m.cpu_m)}m CPU · ${esc(m.mem_gb)} GiB${m.near ? " · its volumes have a copy there" : ""}${m.note ? ` · ${esc(m.note)}` : ""}</span>`];
  });
  return [
    balanceSwitch("containers", b, "Balance containers"),
    UI.lead(!b.on ? "Containers are left where they are."
      : moves.length ? `Moves ${moves.length} container${moves.length === 1 ? "" : "s"} so hosts carry similar CPU and memory. Each one restarts once, on its new host.`
      : plan.quiet === "even" ? "The hosts already carry similar CPU and memory; nothing is worth a restart."
      : plan.quiet === "nothing-movable" ? "Nothing here can move: each container is pinned, left out, only an admin can move it, or it has no other host it may run on. See Not moved below."
      : "No single move evens the hosts out by enough to be worth a restart: the containers that could move are too small to shift CPU or memory much."),
    balanceMetrics(plan),
    UI.section("Hosts", hosts),
    b.on && rows.length ? UI.section(`Containers · ${rows.length}`, '<p class="ui-help">Untick a container to leave it where it is; the moves are worked out again without it.</p>'
      + UI.table([{ label: "Container" }, { label: "Moves", className: "grow" }], rows)) : "",
    balanceSkipped((plan.skipped || []).map(s => [s.id, s.why])),
  ].join("");
}

function balanceVolumesHtml(b) {
  const plan = b.plan, moves = plan.moves || [], gb = n => `${n} GiB`;
  const hosts = UI.table([{ label: "Host" }, { label: "Copies held", className: "grow" }], (plan.hosts || []).map(h => {
    const after = b.on ? h.after_gb : h.before_gb;
    return [`<span class="mono">${esc(h.name)}</span>${h.takes ? "" : ` ${UI.chip("takes no new copies", "warn")}`}`,
      `${UI.meter({ now: h.capacity_gb ? h.before_gb / h.capacity_gb * 100 : 0, after: h.capacity_gb && after !== h.before_gb ? after / h.capacity_gb * 100 : null, label: `${h.name} copies` })}
       <span class="sub">${esc(gb(h.before_gb))}${after !== h.before_gb ? ` → ${esc(gb(after))}` : ""} of ${esc(gb(h.capacity_gb))}</span>`];
  }));
  const rows = (plan.apps || []).map((app, i) => {
    const mine = moves.filter(m => m.app === app), off = b.exclude.has(app);
    return [balanceTick("volumes", app, i, off), off ? '<span class="ui-help">left where they are</span>'
      : mine.map(m => `<div><span class="mono">${esc(m.claim)}</span> <span class="sub">${esc(gb(m.size_gb))} · ${esc(m.from)} → ${esc(m.to)}${m.follows ? " · follows its container there" : ""}</span></div>`).join("")];
  });
  return [
    balanceSwitch("volumes", b, "Balance volume copies"),
    b.on && balanceMoves("containers").length ? `<label class="switch balance-switch"><input type="checkbox" id="balFollow" ${b.follow ? "checked" : ""}
      onchange="balanceFollow(this.checked)"> Move a copy of each moved container's volumes to its new host</label>
      <p class="ui-help">So it reads its data locally there. Off, its volumes stay where they are and it reads them over the network.</p>` : "",
    UI.lead(!b.on ? "Volume copies are left where they are."
      : moves.length ? `Moves ${moves.length} cop${moves.length === 1 ? "y" : "ies"} so hosts hold similar amounts${moves.some(m => m.follows) ? ", and so each container moved has a copy of its volumes on its new host" : ""}. Nothing restarts: each copy is built on its new host before the old one is removed.`
      : (plan.closed || []).length ? `Nothing can move now: ${plan.closed.map(esc).join(", ")} ${plan.closed.length === 1 ? "takes" : "take"} no new copies - cordoned, not Ready, or with scheduling off in Longhorn.`
      : "The hosts are as even as moving copies can make them."),
    UI.section("Hosts", hosts),
    b.on && rows.length ? UI.section(`Apps · ${rows.length}`, '<p class="ui-help">Untick an app to leave its volumes where they are; the moves are worked out again without it.</p>'
      + UI.table([{ label: "App" }, { label: "Copies it moves", className: "grow" }], rows)) : "",
    balanceSkipped((plan.skipped || []).map(s => [s.claim, s.why])),
  ].join("");
}

const balanceMoves = kind => BALANCE[kind]?.on && BALANCE[kind].plan ? (BALANCE[kind].plan.moves || []) : [];
function balanceReview() {
  const pane = $("#balReview");
  if (!pane) return;
  const containers = balanceMoves("containers"), volumes = balanceMoves("volumes");
  const pending = ["containers", "volumes"].some(k => BALANCE[k].on && !BALANCE[k].plan);
  pane.innerHTML = pending ? '<div class="empty"><span class="spin2"></span> Waiting for the plans…</div>' : [
    UI.lead(containers.length || volumes.length ? "Starts what is below, as jobs you can follow and stop in Jobs." : "Nothing to balance with these choices."),
    UI.facts([
      ["Containers", !BALANCE.containers.on ? "left where they are" : containers.length
        ? `${containers.length} move${containers.length === 1 ? "" : "s"} · each restarts once` : "nothing worth moving"],
      ["Volume copies", !BALANCE.volumes.on ? "left where they are" : volumes.length
        ? `${volumes.length} cop${volumes.length === 1 ? "y" : "ies"} · nothing restarts` : "nothing to move"]]),
    containers.length ? '<p class="ui-help">Containers move one at a time, each with the same capacity check as a manual move; if one does not start on its new host within 10 minutes, the job stops there and the rest are not moved; once that container runs, review again to move the rest. Volume copies are built one at a time; stopping drops the copy being built.</p>' : "",
    containers.length ? UI.ack("balanceRestart", containers.length === 1 ? "Restart it now" : "Restart them now") : "",
  ].join("");
  const go = $("#balanceGo");
  if (go) go.disabled = pending || !(containers.length || volumes.length);
}
// The volume plan follows the container moves: worked out again when they change.
const balanceReplanVolumes = () => { BALANCE.volumes.plan = null; balanceReview(); return balancePlan("volumes"); };
window.balanceFollow = on => { BALANCE.volumes.follow = on; balanceReplanVolumes(); };
window.balanceToggle = (kind, on) => {
  BALANCE[kind].on = on;
  const pane = $(kind === "containers" ? "#balContainers" : "#balVolumes");
  if (pane && BALANCE[kind].plan) pane.innerHTML = kind === "containers" ? balanceContainersHtml(BALANCE[kind]) : balanceVolumesHtml(BALANCE[kind]);
  balanceReview();
  if (kind === "containers") balanceReplanVolumes();
};
window.balanceItem = async (kind, id, on) => {
  on ? BALANCE[kind].exclude.delete(id) : BALANCE[kind].exclude.add(id);
  BALANCE[kind].plan = null; balanceReview();
  await balancePlan(kind);
  if (kind === "containers") balanceReplanVolumes();
};
window.balanceStart = async () => {
  const containers = balanceMoves("containers"), volumes = balanceMoves("volumes");
  if (!containers.length && !volumes.length) return;
  if (containers.length && !$("#balanceRestart")?.checked) return toast("Tick Restart to confirm the container restarts", "bad");
  const button = $("#balanceGo");
  if (button) { button.disabled = true; button.textContent = "Starting…"; }
  const started = [];
  try {
    if (containers.length) {
      await api("/api/workloads/rebalance", { method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ exclude: [...BALANCE.containers.exclude], review_token: BALANCE.containers.plan.review_token,
          moves: BALANCE.containers.plan.moves, restart: true }) });
      started.push(`${containers.length} container${containers.length === 1 ? "" : "s"}`);
    }
    if (volumes.length) {
      await api("/api/longhorn/rebalance", { method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ exclude: [...BALANCE.volumes.exclude], review_token: BALANCE.volumes.plan.review_token,
          moves: BALANCE.volumes.plan.moves }) });
      started.push(`${volumes.length} volume cop${volumes.length === 1 ? "y" : "ies"}`);
    }
    closeModal(); toast(`Balancing ${started.join(" and ")}; follow it in Jobs`, "ok");
  } catch (e) {
    toast((started.length ? `Started ${started.join(" and ")}, but ` : "") + e.message, "bad");
    if (button) { button.disabled = false; button.textContent = "Start balancing"; }
    if (e.body?.plan) { balancePlan("containers").then(() => balancePlan("volumes")); }
  } finally { window.refreshOperations?.(true); }
};

/* Reservations from a week of use, for many apps at once. The scheduler
   places apps by what they reserve, so apps reserving nothing are ticked;
   ones whose reservation only differs from their use are offered unticked,
   as someone may have set it on purpose. Ticking plans again, so the review
   token covers exactly what is started. */
const RESERVE = { chosen: null, plan: null, asked: 0 };
window.reservationsReview = async () => {
  RESERVE.chosen = null; RESERVE.plan = null;
  modal("Set reservations from use", `<div id="resPlan"><div class="empty"><span class="spin2"></span> Reading a week of use…</div></div>
    ${UI.actions(UI.button("Set reservations", "reservationsStart()", { kind: "pri", id: "reserveGo", attrs: 'data-need="operator" disabled' }), UI.cancel())}`, true);
  await reservationsPlan();
};
async function reservationsPlan() {
  const pane = $("#resPlan");
  if (!pane) return;
  // Each tick plans again; an answer to an earlier tick is dropped.
  const asked = ++RESERVE.asked;
  const query = RESERVE.chosen ? `?${new URLSearchParams({ chosen: [...RESERVE.chosen].join(",") })}` : "";
  let plan;
  try { plan = await api(`/api/workloads/requests/plan${query}`); }
  catch (e) { if (asked === RESERVE.asked) pane.innerHTML = UI.callout("bad", "Could not read the apps' use", esc(e.message)); return; }
  if (asked !== RESERVE.asked || !$("#resPlan")) return;
  RESERVE.plan = plan;
  if (!RESERVE.chosen) RESERVE.chosen = new Set(plan.apps.filter(a => a.chosen).map(a => a.id));
  pane.innerHTML = reservationsHtml(plan);
  const go = $("#reserveGo");
  if (go) go.disabled = !RESERVE.chosen.size || RESERVE.chosen.size > plan.max;
  window.applyRole?.();
}
/* A reservation in Edit's words (editCpuWords, editSpan in views-lifecycle.js).
   What a container reserves now is what it reserves in effect: Kubernetes
   takes a limit as the request where none is set, and the review says so. */
const reserveNow = (c, r) => {
  const value = r === "cpu" ? (c.cpu_now_m ? editCpuWords(c.cpu_now_m) : "none") : (c.memory_now_mi ? reservedMemory(c.memory_now_mi) : "none");
  return (c.from_limit || []).includes(r) ? `${value} (its limit)` : value;
};
function reservationsHtml(plan) {
  const apps = plan.apps || [], chosen = apps.filter(a => RESERVE.chosen.has(a.id));
  const over = chosen.length > plan.max;
  const rows = apps.map((a, i) => [
    `<label class="rb-app"><input type="checkbox" id="res_${i}" ${RESERVE.chosen.has(a.id) ? "checked" : ""}
      onchange="reservationsItem(${jsq(a.id)},this.checked)"> <span class="mono">${esc(a.id)}</span></label>${a.missing ? ` ${UI.chip("reserves nothing", "warn")}` : ""}`,
    a.changes.map(c => `<div>${a.changes.length > 1 ? `<span class="mono">${esc(c.container)}</span> ` : ""}
      <span>${esc(reserveNow(c, "cpu"))} → <b>${esc(editCpuWords(parseInt(c.cpu, 10)))}</b> CPU · ${esc(reserveNow(c, "memory"))} → <b>${esc(reservedMemory(parseInt(c.memory, 10)))}</b></span>
      <span class="sub">usually at or under ${esc(editCpuWords(c.cpu_p95_m))} and ${esc(reservedMemory(c.mem_p95_mi))}, peak ${esc(reservedMemory(c.mem_max_mi))} over ${esc(editSpan(c))}${c.lowers ? " · lowers it" : ""}${c.capped.length ? " · held at its limit" : ""}${(c.from_limit || []).some(r => !c.capped.includes(r)) ? " · no longer reserves its whole limit" : ""}</span></div>`).join("")
      + (a.replicas ? "" : `<div class="sub">Stopped: takes the new reservation when it next starts.</div>`)]);
  const managed = (plan.skipped || []).filter(s => s.yaml), other = (plan.skipped || []).filter(s => !s.yaml);
  return [
    UI.lead(over ? `${chosen.length} apps are ticked; one run changes at most ${plan.max}. Untick some, and set the rest afterwards.`
      : chosen.length ? `Sets what ${chosen.length} app${chosen.length === 1 ? "" : "s"} reserve${chosen.length === 1 ? "s" : ""} from their use over the last week. Each restarts once, one at a time.`
      : apps.length ? "Tick the apps to change." : "Every app's reservation already matches its use, or has too few readings yet."),
    rows.length ? UI.section(`Apps · ${rows.length}`, UI.table([{ label: "App" }, { label: "Reservation", className: "grow" }], rows)) : "",
    managed.length ? UI.more(`Applied by Flux or Argo CD · ${managed.length}`, managed.map(s => `<p><span class="mono">${esc(s.id)}</span>: ${esc(s.why)}</p>
      <pre class="mono small">${esc(s.yaml)}</pre>`).join(""), false, "reserve-managed") : "",
    other.length ? UI.more(`Not changed · ${other.length}`, `<ul class="ui-list">${other.map(s => `<li><span class="mono">${esc(s.id)}</span>: ${esc(s.why)}</li>`).join("")}</ul>`, false, "reserve-other") : "",
    UI.more("How the reservations are worked out", `<p>Each container's use is read every five minutes. The reservation is 95% of the week's readings with headroom - CPU 1.5 times, memory 1.25 times - and never above a limit. Apps reserving nothing are ticked; others are offered unticked, since a reservation may have been set on purpose.</p>
      <p>Apps change one at a time, each waiting for the last to run again. An app with no host that has room for its new reservation, or changed since this review, is skipped. If one does not come back within ten minutes, the job stops and the rest are not changed. Follow and stop it in Jobs.</p>`, false, "reserve-how"),
    chosen.length && !over ? UI.ack("reserveRestart", chosen.length === 1 ? "Restart it now" : `Restart these ${chosen.length} apps now`) : "",
  ].join("");
}
window.reservationsItem = (id, on) => {
  on ? RESERVE.chosen.add(id) : RESERVE.chosen.delete(id);
  const go = $("#reserveGo");
  if (go) go.disabled = true;
  reservationsPlan();
};
window.reservationsStart = async () => {
  // What was reviewed: the plan's own ticks, which its token covers.
  const plan = RESERVE.plan, apps = (plan?.apps || []).filter(a => a.chosen);
  if (!apps.length) return;
  if (!$("#reserveRestart")?.checked) return toast("Tick Restart to confirm the restarts", "bad");
  const button = $("#reserveGo");
  if (button) { button.disabled = true; button.textContent = "Starting…"; }
  try {
    await api("/api/workloads/requests", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ apps: apps.map(a => ({ id: a.id, changes: a.changes })), review_token: plan.review_token, restart: true }) });
    closeModal(); toast(`Setting reservations on ${apps.length} app${apps.length === 1 ? "" : "s"}; follow it in Jobs`, "ok");
  } catch (e) {
    toast(e.message, "bad");
    if (button) { button.disabled = false; button.textContent = "Set reservations"; }
    // Planned again with the same ticks: what changed is shown, not a reset.
    reservationsPlan();
  } finally { window.refreshOperations?.(true); }
};

/* Priority tiers for many apps at once. Apps whose name or image suggests a
   tier start set to it; the rest keep what they have. Changing a row plans
   again, so the review token covers exactly what is started. */
const PRIORITY = { picks: {}, plan: null, asked: 0 };
window.priorityReview = async () => {
  PRIORITY.picks = {}; PRIORITY.plan = null;
  modal("Priority tiers", `<div id="prioPlan"><div class="empty"><span class="spin2"></span> Reading the apps…</div></div>
    ${UI.actions(UI.button("Set tiers", "priorityStart()", { kind: "pri", id: "prioGo", attrs: 'data-need="operator" disabled' }), UI.cancel())}`, true);
  await priorityPlan();
};
async function priorityPlan() {
  const pane = $("#prioPlan");
  if (!pane) return;
  // Each change plans again; an answer to an earlier one is dropped.
  const asked = ++PRIORITY.asked;
  const picks = Object.entries(PRIORITY.picks).map(([id, tier]) => `${id}=${tier}`).join(",");
  let plan;
  try { plan = await api(`/api/workloads/priority/plan${picks ? `?${new URLSearchParams({ chosen: picks })}` : ""}`); }
  catch (e) { if (asked === PRIORITY.asked) pane.innerHTML = UI.callout("bad", "Could not read the apps", esc(e.message)); return; }
  if (asked !== PRIORITY.asked || !$("#prioPlan")) return;
  PRIORITY.plan = plan;
  pane.innerHTML = priorityHtml(plan);
  const go = $("#prioGo");
  if (go) go.disabled = !plan.changes || plan.changes > plan.max;
  window.applyRole?.();
}
/* Where a row's tier comes from, and how it stands (#430). */
const PRIORITY_STATUS = { in_line: ["in line", ""], restart: ["needs a restart", "warn"], git: ["change in git", "info"],
  outside: ["changed outside Homestead", "warn"], unknown_group: ["group tier unknown", "bad"] };
function prioritySource(a) {
  if (a.source === "group") return a.wanted ? `${a.wanted}, from ${a.group}` : `from ${a.group}`;
  if (a.source === "default") return "normal, the default";
  return `${a.wanted || a.current}, its own`;
}
function priorityStatus(a) {
  const [label, tone] = PRIORITY_STATUS[a.status] || PRIORITY_STATUS.in_line;
  const runs = a.wanted && a.wanted !== a.current ? ` · runs ${a.current}` : "";
  return `${esc(prioritySource(a) + runs)} · ${tone ? UI.chip(label, tone) : esc(label)}${a.line ? `<br><span class="mono prio-line">${esc(a.line)}</span>` : ""}`;
}
function priorityHtml(plan) {
  const apps = plan.apps || [], changes = apps.filter(a => a.tier !== a.current), over = changes.length > plan.max;
  // A VM, and a StatefulSet that doesn't restart itself, takes its tier when it next starts.
  const rolling = changes.filter(a => a.kind !== "vm" && a.restarts !== false), restarting = rolling.length;
  const sets = rolling.filter(a => a.kind === "sts").length, held = changes.length - restarting;
  const vms = changes.filter(a => a.kind === "vm").length, heldSets = held - vms;
  const count = (n, noun) => n ? `${n} ${noun}${n === 1 ? "" : "s"}` : "";
  const and = (...parts) => parts.filter(Boolean).join(" and ");
  const chips = { vm: "VM", sts: "StatefulSet" };
  const rows = apps.map((a, i) => [
    `<span class="mono">${esc(chips[a.kind] ? `${a.ns}/${a.name}` : a.id)}</span>${chips[a.kind] ? ` ${UI.chip(chips[a.kind])}` : ""}${a.tier !== a.current ? ` ${UI.chip(`was ${a.current}`, "info")}` : ""}`,
    `${prioritySelect(`prio_${i}`, a.tier, `${a.managed || a.status === "unknown_group" ? "disabled" : ""} onchange="priorityItem(${jsq(a.id)},this.value)" aria-label="${esc(a.id)} tier"`)}
      <span class="sub">${priorityStatus(a)}</span>
      <span class="sub">${esc(a.note || (a.status === "unknown_group" ? "Group tiers couldn't be read; try again shortly." : "") || (a.suggested && a.suggested !== "normal" ? `suggested ${a.suggested}: it ${a.why}` : ""))}</span>`]);
  return [
    UI.lead(over ? `${changes.length} apps would change; one run changes at most ${plan.max}. Set some back, and change the rest afterwards.`
      : changes.length ? [restarting ? `Changes the tier of ${and(count(restarting - sets, "app"), count(sets, "StatefulSet"))}. Each restarts once, one at a time${sets ? "; a StatefulSet one pod at a time" : ""}.` : "",
        held ? `${and(count(vms, "VM"), count(heldSets, "StatefulSet"))} ${held === 1 ? "takes its" : "take their"} new tier when ${held === 1 ? "it next starts" : "they next start"}; none restarts now.` : ""].filter(Boolean).join(" ")
      : "Every app keeps its tier. Choose a tier to change one."),
    // What the cluster's classes get wrong (#429), said once.
    (plan.warnings || []).length ? UI.callout("warn", "Check first", `<ul class="ui-list">${plan.warnings.map(w => `<li>${esc(w)}</li>`).join("")}</ul>`) : "",
    (plan.groups || []).length ? UI.section(`Groups · ${plan.groups.length}`, `<ul class="ui-list">${plan.groups.map(g =>
      `<li><b>${esc(g.name)}</b> ${esc(g.tier === null ? "tier unknown" : g.tier || "no tier")} · ${g.apps} app${g.apps === 1 ? "" : "s"}</li>`).join("")}</ul>`) : "",
    UI.section(`Apps, StatefulSets and VMs · ${rows.length}`, UI.table([{ label: "App" }, { label: "Tier", className: "grow" }], rows)),
    (plan.skipped || []).length ? UI.more(`Left alone · ${plan.skipped.length}`, `<ul class="ui-list">${plan.skipped.map(s => `<li><span class="mono">${esc(s.id)}</span>: ${esc(s.why)}</li>`).join("")}</ul>`, false, "prio-skipped") : "",
    UI.more("What the tiers do", `<p>When the cluster is short of room - after a host fails, but also as apps restart, update or grow - critical apps fit first and may evict any pod of a lower tier: low apps, and anything with no tier, VMs included. Normal apps may evict low ones. Low apps wait, evict nothing, and are evicted first. Normal is the cluster's default class, or no class where there is none. VMs take tiers too, from their next start. StatefulSets take tiers of their own, never a group's; one that waits for its pods to be deleted (OnDelete, or a partition at or above its replicas) takes its tier when they next restart, and Homestead never deletes them. Homestead ranks itself at least as high as any app, so none can evict it; it restarts once to do so.</p>
      <p>A tier uses the cluster's own class where one fits; Homestead makes one, just above or below the default, only where none does. Apps then change one at a time, each waiting for the last to run again, and a StatefulSet one pod at a time. If one does not come back within ten minutes - for a StatefulSet, ten per pod - the job stops and the rest are not changed.</p>`, false, "prio-how"),
    changes.length && !over ? UI.ack("prioRestart", !restarting ? "Set the tiers" : restarting === 1 ? "Restart it now" : `Restart these ${restarting}${sets ? "" : " apps"} now`) : "",
  ].join("");
}
window.priorityItem = (id, tier) => {
  PRIORITY.picks[id] = tier;
  const go = $("#prioGo");
  if (go) go.disabled = true;
  priorityPlan();
};
window.priorityStart = async () => {
  const plan = PRIORITY.plan, changes = (plan?.apps || []).filter(a => a.tier !== a.current);
  if (!changes.length) return;
  if (!$("#prioRestart")?.checked) return toast("Tick Restart to confirm the restarts", "bad");
  const button = $("#prioGo");
  if (button) { button.disabled = true; button.textContent = "Starting…"; }
  try {
    await api("/api/workloads/priority", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ changes: changes.map(a => ({ id: a.id, current: a.current, tier: a.tier, follow: !!a.follow })), review_token: plan.review_token, restart: true }) });
    const sets = changes.filter(a => a.kind === "sts").length, apps = changes.length - sets;
    const named = [apps || !sets ? `${apps} app${apps === 1 ? "" : "s"}` : "", sets ? `${sets} StatefulSet${sets === 1 ? "" : "s"}` : ""];
    closeModal(); toast(`Setting the tier of ${named.filter(Boolean).join(" and ")}; follow it in Jobs`, "ok");
  } catch (e) {
    toast(e.message, "bad");
    if (button) { button.disabled = false; button.textContent = "Set tiers"; }
    // Planned again with the same choices: what changed is shown, not a reset.
    priorityPlan();
  } finally { window.refreshOperations?.(true); }
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
    <label class="switch"><input type="checkbox" id="ss_ok"> Stop it anyway</label>
    ${UI.actions(`<button class="btn pri" onclick="closeModal();wlRestart(${jsq(ns)},${jsq(name)})">Restart instead</button>
      <button class="btn danger" onclick="wlStopSelfGo(${jsq(ns)},${jsq(name)})">Stop Homestead</button>
      <button data-dialog-dismiss="true" class="btn" onclick="closeModal()">Cancel</button>`)}`);
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
  modal("If a host fails", '<div class="empty"><span class="spin2"></span></div>', true);
  // An unread container list is said; no policy is offered or saved against an old one (#620).
  const [rows, lh] = await Promise.all([api("/api/workloads").catch(e => ({ unread: e.message })),
    // Without Longhorn there is no policy to show; with it, an unread one is said (#620).
    STATE.platform?.longhorn === false ? null : api("/api/longhorn/capacity").catch(e => ({ unread: e.message }))]);
  const policy = lh?.unread ? "" : lh?.node_down || "";
  // Not knowing whether Longhorn is installed (/api/platform unread), a failed
  // read may only mean it is not there.
  const unread = lh?.unread && (STATE.platform?.longhorn == null
    ? `${String(lh.unread).replace(/\.$/, "")}. Longhorn may not be installed on this cluster.` : lh.unread);
  const moving = policy && policy !== "do-nothing";
  $("#mbody").innerHTML = `
    <p class="small" style="margin-top:0">Choose whether containers move, wait or use Kubernetes’ default five-minute delay after host failure. Saving a changed policy restarts that container.</p>
    ${lh?.unread ? UI.callout("warn", "Could not read Longhorn's node-failure policy", esc(unread)) : lh ? `<div class="note ${moving ? "good" : "warn"}">${moving
      ? `Longhorn lets go of a failed node's volumes (<span class="mono">${esc(policy)}</span>), so a container moving to another host takes its volume with it.`
      : `<b>Volume attachment blocks failover.</b> Single-node volumes remain attached to the failed host until Longhorn releases them. <button class="btn sm pri" data-need="admin" onclick="wlFailoverPolicy()" style="margin-top:6px">Let Longhorn release them</button>`}</div>` : ""}
    ${rows.unread ? `${UI.callout("warn", "Could not read the containers", esc(rows.unread))}
    ${UI.actions(`<button class="btn" onclick="wlFailover()">Try again</button>
      <button data-dialog-dismiss="true" class="btn" onclick="closeModal()">Close</button>`)}` : `
    <div class="row" style="margin:12px 0;gap:6px;flex-wrap:wrap"><span class="small dim">Set all to</span>
      ${Object.entries(FAILOVER_WORDS).map(([v, l]) => `<button class="btn sm" onclick="$$('#mbody select[data-fo]').forEach(s => s.value=${jsq(v)})">${esc(l)}</button>`).join("")}</div>
    <table class="tbl dense stack"><thead><tr><th>Container</th><th>If its host fails</th></tr></thead><tbody>
      ${rows.map(w => `<tr><td><b>${esc(w.name)}</b> <span class="dim xs">${esc(w.ns)}${w.group ? ` · ${esc(w.group)}` : ""}</span>
          ${(w.hardware || []).length ? UI.chip(w.hardware.join(", "), "hw", { tip: "Tied to hardware on its host" }) : ""}</td>
        <td data-label="If its node fails">${failoverSelect(`fo_${w.ns}_${w.name}`, w.failover || "default", `data-fo data-ns="${esc(w.ns)}" data-name="${esc(w.name)}" data-was="${esc(w.failover || "default")}"`, `If ${w.name}'s host fails`)}</td></tr>`).join("")}
    </tbody></table>
    ${UI.actions(`<button class="btn pri" data-need="operator" onclick="wlFailoverSave()">Save changes</button>
      <button data-dialog-dismiss="true" class="btn" onclick="closeModal()">Cancel</button>`)}`}`;
  if (window.applyRole) applyRole();
};
window.wlFailoverSave = async () => {
  const items = $$("#mbody select[data-fo]").filter(s => s.value !== s.dataset.was)
    .map(s => ({ ns: s.dataset.ns, name: s.dataset.name, mode: s.value }));
  if (!items.length) return closeModal();
  if (items.some(i => (STATE.data.wl || []).some(w => w.self && w.ns === i.ns && w.name === i.name))
      && !(await ask("Homestead itself is among them: it restarts, and this page reconnects when it is back.", { title: "Homestead restarts", ok: "Save and restart" }))) return;
  try {
    const r = await api("/api/workloads/failover", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ items }) });
    toast(r.detail, "ok"); closeModal(); setTimeout(() => refresh(true), 900);
  } catch (e) { toast(e.message, "bad"); }
};
window.wlFailoverPolicy = async () => {
  if (!(await ask("Let Longhorn delete the pods of a node that stops answering, so their volumes can attach elsewhere?\n\nThis is Longhorn's \"Pod Deletion Policy When Host is Down\" set to delete-both-statefulset-and-deployment-pod.", { title: "Pod deletion when a host is down", ok: "Allow" }))) return;
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
  // Unread networks are not "no LAN network yet", and the failure is not kept as the answer (#620).
  let opts = window.__vmCreateOptions;
  if (!opts) {
    try { opts = await api("/api/vm/create-options"); }
    catch (e) { return UI.callout("warn", "Could not read the networks", esc(e.message)); }
    window.__vmCreateOptions = opts;
  }
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
  if (!(await ask(`Restart ${name}? Its pods are replaced with fresh ones; what it serves may be away for a moment.`, { title: "Restart", ok: "Restart" }))) return;
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
  // A failed read is not "none": it is shown, and the delete is not offered.
  const [network, vols] = await Promise.all([
    STATE.data.network ? Promise.resolve(STATE.data.network) : api("/api/network").catch(e => ({ error: e.message })),
    STATE.data.vols ? Promise.resolve(STATE.data.vols) : api("/api/volumes").catch(e => ({ error: e.message })),
  ]);
  const failed = [network, vols].find(read => read?.error);
  const unread = read => `<span class="warn">could not be read: ${esc(read.error)}</span>`;
  const workload = (STATE.data.wl || []).find(row => row.ns === ns && row.name === name) || {};
  const services = (network.services || [])
    .filter(row => row.namespace === ns && (row.targets || []).includes(name));
  const listeners = services.flatMap(row => (row.external_ips || [])
    .flatMap(ip => (row.ports || []).map(port => `${ip}:${port.port}/${port.protocol}`)));
  const claims = vols.error ? [] : (vols || []).filter(vol => (vol.attached || []).includes(name));
  $("#mbody").innerHTML = `
    <div class="note dependency-danger"><b>This removes the app, not its data.</b>
      The Deployment and every Service pointing at it are deleted. Persistent volumes are kept and can be
      removed afterwards from Volumes.</div>
    <div class="dependency-list" style="margin-top:12px">
      <div class="dependency-row"><span>Deployment</span><b class="mono">${esc(ns)}/${esc(name)}</b></div>
      <div class="dependency-row"><span>Containers</span><b>${(workload.images || []).length || 1}</b></div>
      <div class="dependency-row ${services.length ? "stranded" : ""}"><span>Services removed</span>
        <b>${network.error ? unread(network) : services.length ? services.map(row => esc(row.name)).join(", ") : "none"}</b></div>
      ${listeners.length ? `<div class="dependency-row stranded"><span>LAN listeners freed</span>
        <b class="mono">${listeners.map(esc).join(", ")}</b></div>` : ""}
      <div class="dependency-row"><span>Volumes kept</span>
        <b>${vols.error ? unread(vols) : claims.length ? claims.map(vol => esc(vol.pvc_name || vol.name)).join(", ") : "none attached"}</b></div>
    </div>
    ${failed ? `<div class="note" style="margin-top:12px">Deleting is offered once Homestead can read which Services it removes and which volumes it keeps. Try again in a moment.</div>
    ${UI.actions(`<button class="btn" onclick="wlDelete(${jsq(ns)},${jsq(name)})">Retry</button>
      <button data-dialog-dismiss="true" class="btn" onclick="closeModal()">Cancel</button>`)}`
    : `${UI.typedConfirm({ id: "wd_confirm", expect: name, button: "wd_go" })}
    ${UI.actions(`<button class="btn danger" id="wd_go" data-need="operator" disabled
      onclick="wlDeleteNow(${jsq(ns)},${jsq(name)},this)">Delete app</button>
      <button data-dialog-dismiss="true" class="btn" onclick="closeModal()">Cancel</button>`)}`}`;
  if (window.applyRole) window.applyRole();
};
window.wlDeleteNow = async (ns, name, button) => {
  if (!typedConfirmMatches("wd_confirm")) return;
  if (button) { button.disabled = true; button.textContent = "Deleting…"; }
  try {
    const result = await api(`/api/workload/${ns}/${name}`, { method: "DELETE" });
    const freed = (result.services || []).length;
    closeModal();
    toast(`${name} deleted${freed ? ` with ${freed} service${freed === 1 ? "" : "s"}` : ""}`, "ok");
    setTimeout(() => refresh(true), 900);
  } catch (e) {
    if (button) { button.disabled = false; button.textContent = "Delete app"; }
    toast(e.message, "bad");
  }
};
/* Logs for one container of one pod. A workload with several replicas, or a
   pod with sidecars, gets pickers saying exactly whose output this is. */
function openLogs(title, path, pods = null, ns = "") {
  window.__logTimer?.stop?.();
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
  // The dialog's own read (`keep`, not the page's): closing it cancels it.
  const poll = async signal => {
    if ($("#modal").classList.contains("hidden")) return false;
    try {
      const t = await api(LOG.path + (LOG.path.includes("?") ? "&" : "?") + "tail=500", { keep: true, signal });
      if (UI.pollCancelled(signal)) return;
      const pre = $("#mbody .logview"); if (!pre) return;
      pre.textContent = t || "(the container is running but has not written any logs yet)";
      $("#logstate").innerHTML = '<span class="ld"></span> live · refreshes every 2s';
      if ($("#logfollow")?.checked) pre.scrollTop = pre.scrollHeight;
    } catch (e) {
      if (UI.pollCancelled(signal)) return;
      const pre = $("#mbody .logview"); if (pre) pre.textContent = e.message;
      if ($("#logstate")) $("#logstate").innerHTML = '<span class="cd badbg"></span> no logs';
    }
  };
  const timer = window.__logTimer = UI.poll(poll, 2000);
  // Another pod or container: the old one's read is cancelled, so its output never paints here.
  LOG.poll = () => timer.restart();
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
      const at = await api("/api/image-updates/scan-progress", { keep: true }).catch(e => ({ unread: e.message }));
      // The check goes on; the bar says this read failed, until the next one answers.
      if (at?.unread) {
        const bar = $("#scanprogress");
        if (bar && !bar.querySelector(".scan-unread"))
          bar.insertAdjacentHTML("beforeend", ` <span class="scan-unread">· could not read the progress just now: ${esc(at.unread)}</span>`);
        continue;
      }
      if (!at.total) continue;
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

/* Lead with the constraint someone can resolve; estimates stay in Details. */
function imagePlacementBlocker(plan) {
  const hosts = plan.candidates || [];
  if (hosts.length && hosts.every(host => !host.eligible && (host.reasons || []).includes("cordoned"))) {
    return hosts.length === 1 ? "The only host is cordoned. Uncordon it before updating."
      : "All hosts are cordoned. Uncordon a suitable host before updating.";
  }
  if (hosts.length && hosts.every(host => !host.eligible && (host.reasons || []).some(reason => reason.startsWith("node is ")))) {
    return hosts.length === 1 ? "The only host is not ready. Restore its health before updating."
      : "No host is ready. Restore a host's health before updating.";
  }
  if (plan.rollout?.start_blocked) return "A replacement pod cannot fit while the current pods are running. Free capacity or review the rollout policy in Details.";
  return "No host can accept this update. Review the host, storage and placement requirements in Details.";
}
/* The same concern for several updates is said once. A blocked review leads
   only with blockers; warnings for otherwise eligible apps remain in Details. */
const UNCHANGED_BY_UPDATE = /^No memory limit is set/;
const updateConcerns = plan => capacityNotes(plan).concerns.filter(text => !UNCHANGED_BY_UPDATE.test(text));
function groupedConcerns(rows) {
  const byText = new Map();
  const blocked = rows.some(row => row.preview.capacity.blocked);
  rows.forEach(({config, preview}) => {
    const messages = blocked ? (preview.capacity.blocked ? [imagePlacementBlocker(preview.capacity)] : [])
      : updateConcerns(preview.capacity);
    const who = config.clusterName ? `${config.clusterName} · ${config.name}` : config.name;
    new Set(messages).forEach(text => {
      if (!byText.has(text)) byText.set(text, []);
      if (!byText.get(text).includes(who)) byText.get(text).push(who);
    });
  });
  return [...byText].map(([text, names]) => rows.length > 1
    ? `${text.replace(/\.$/, "")} - ${names.length === rows.length ? `all ${rows.length} updates` : names.join(", ")}` : text);
}

let IMAGE_REVIEW = null, IMAGE_REVIEW_SEQUENCE = 0;
/* An update can be for a linked cluster (the Homestead updates dialog
   updates them too): its requests carry that cluster, and its rollout is
   keyed by it, since the same app can run on two clusters. */
const clusterHeaders = item => {
  const cluster = item?.cluster || window.FLEET?.view?.self;
  return cluster ? { "X-Homestead-Cluster": cluster } : {};
};
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
        ...(item.part ? {part: item.part} : {}), ...(item.version ? {version: item.version} : {})};
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
    // One line per app: its name, and what its image moves from and to -
    // once, when every container moves between the same two images.
    const changeWords = i => imageChangeWords(i.before, i.after, i.before_tag, i.after_tag);
    const apps = rows.map(({config, preview}) => {
      const flagged = preview.capacity.blocked || updateConcerns(preview.capacity).length;
      const words = preview.images.map(changeWords), same = words.every(w => w.join() === words[0]?.join());
      const change = (same ? [[null, words[0]]] : preview.images.map((i, n) => [i.container, words[n]]))
        .filter(([, w]) => w).map(([container, [was, now]]) => `<span class="upd-change">${container ? `${esc(container)} ` : ""}<code>${esc(was)}</code> → <code>${esc(now)}</code></span>`).join("");
      return `<li><span class="upd-name">${flagged ? `<span class="upd-flag ${preview.capacity.blocked ? "bad" : "warn"}" title="See the notes above">!</span>` : ""}<b>${esc(config.name)}</b> <span class="dim">${config.clusterName ? `${esc(config.clusterName)} · ` : ""}${esc(config.ns)}</span></span>${change}</li>`;
    }).join("");
    // Details: per app, the exact images and where it can run; notes shared
    // by every app, once.
    const short = digest => String(digest || "").replace(/@sha256:([0-9a-f]{12})[0-9a-f]+$/, "@sha256:$1…");
    const notes = [...new Set(rows.flatMap(({preview}) => [...capacityNotes(preview.capacity).caveats,
      ...capacityNotes(preview.capacity).concerns.filter(text => UNCHANGED_BY_UPDATE.test(text))]))];
    const images = preview => [...new Map(preview.images.map(i => [`${i.before}|${i.after}`, i])).values()];
    const details = rows.map(({config, preview}) => UI.section(many ? `${config.clusterName ? `${config.clusterName} · ` : ""}${config.ns}/${config.name}` : "",
      UI.facts(images(preview).flatMap(i => [[`Image ${rollback ? "back to" : "now"}`, `<code title="${esc(rollback ? i.after : i.before)}">${esc(short(rollback ? i.after : i.before))}</code>`],
        [rollback ? "From" : "New", `<code title="${esc(rollback ? i.before : i.after)}">${esc(short(rollback ? i.before : i.after))}</code>`]]))
      + capacityHostTable(preview.capacity))).join("");
    $("#mbody").innerHTML = `<div class="update-review ui-stack">
      <p class="ui-lead">${many ? "Apps update one at a time, with Homestead last. " : ""}${items.some(restartsHomestead) ? "Homestead will be briefly unavailable while it restarts. " : ""}${many ? "Each app restarts" : "The app restarts"} to ${rollback ? "return to its previous image" : "use the new image"}.</p>
      ${blocked ? UI.callout("bad", "Update blocked", list)
        : concerns.length ? UI.callout("warn", "Check first", list) : ""}
      ${rows.filter(r => r.preview.unread).map(({config, preview}) => UI.callout("warn", "Installed by its tag",
        `Homestead could not read the registry (${esc(preview.unread)}), so ${esc(config.version)} is not pinned to an exact build: the cluster pulls it by its tag and checks it. If the tag does not exist, the new pod waits and the old one keeps serving.`)).join("")}
      <ul class="upd-apps">${apps}</ul>
      ${rollback ? "" : '<div id="updNotes"></div>'}
      ${UI.more("Details", `${details}
        ${notes.length ? `<ul class="ui-list">${notes.map(n => `<li>${esc(n)}</li>`).join("")}</ul>` : ""}
        <p class="ui-help">Exact images and capacity are checked again before each change. A failure, lost contact or an expired review stops the rest of the queue; closing this dialog stops updates not yet started.</p>`)}
      ${blocked ? "" : UI.ack("imageCapacityApprove", concerns.length ? "Update despite the warnings" : many ? "Restart them now" : "Restart it now", { onchange: "imageReviewReady()" })}
      ${UI.actions(UI.button(rollback ? "Start rollback" : many ? `Update ${rows.length}` : "Update", "imageReviewedApply()", { kind: "pri", id: "imageCapacityApply", disabled: true }), UI.cancel())}
    </div>`;
    // What is in the update: read beside the review, never waited for.
    if (!rollback && window.updateNotesInto) updateNotesInto($("#updNotes"), rows.map(r => r.config));
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
  // Arm the reload only when this Homestead's own rollout starts, and hold
  // version notices until the whole queue has finished, including both pods
  // of a redundant Homestead. An unrelated early response cannot end it.
  try { sessionStorage.removeItem("homestead.selfUpdate"); } catch (e) { /* no session storage */ }
  const sequence = ++IMAGE_REVIEW_SEQUENCE;
  const items = rows.map(r => r.config);
  const states = Object.fromEntries(items.map(item => [rolloutKey(item), {phase: "queued", ready: 0, desired: 1}]));
  const failures = [];
  modal("Reviewed image rollouts", '<div id="imageQueue"></div>', true);
  const active = () => sequence === IMAGE_REVIEW_SEQUENCE && $("#imageQueue") && !$("#modal").classList.contains("hidden");
  window.homesteadUpdateQueueActive = active;
  delete window.__homesteadQueuedVersion;
  const paint = () => {if (active()) paintRollout($("#imageQueue"), batchUpdateMarkup(items, states, failures, false, true));};
  // Runs the queue from `start`; a failure stops it there, and Skip and
  // continue (#295) picks it up again after the failed app.
  const run = async start => {
    IMAGE_QUEUE = null;
    let complete = false;
    try {
      for (let index = start; index < rows.length; index++) {
        const {config, preview} = rows[index], key = rolloutKey(config);
        if (!active()) return;
        let job = "";
        try {
          if (restartsHomestead(config) && !config.cluster && config.action !== "rollback") {
            try { sessionStorage.setItem("homestead.selfUpdate", String(Date.now())); } catch (e) { /* the bar offers Reload */ }
          }
          const result = await api(config.action === "rollback" ? "/api/image-updates/rollback" : "/api/image-updates/apply",
            {method: "POST", headers: {"Content-Type": "application/json", ...clusterHeaders(config)},
             body: JSON.stringify({...rolloutBody(config), capacity_token: preview.capacity_token, confirm_capacity: true})});
          job = result.operation?.id || "";
          states[key] = result; paint();
          // Never start the next workload until this exact accepted generation is ready.
          if (!result.uid || !Number.isInteger(result.generation)) throw new Error("Rollout identity unavailable; check its job before continuing");
          const deadline = Date.now() + 15 * 60 * 1000;
          let misses = 0;
          while (true) {
            if (!active()) return;
            let state;
            try {
              state = await api(`/api/image-updates/progress?ns=${encodeURIComponent(config.ns)}&name=${encodeURIComponent(config.name)}`,
                {timeout: 10000, headers: clusterHeaders(config)});
              misses = 0;
            } catch (error) {
              // Homestead is replacing itself - here, or on a linked cluster the
              // relay cannot reach for that minute: wait for it to answer again.
              // For any other app, no answer at all (a dropped connection, a
              // proxy giving up on a long pull, a gateway error) says nothing
              // about the rollout: ask again, and stop only once it has been
              // gone a while. An answer that is an error still stops the queue.
              const unanswered = !error.status || error.status >= 502;
              misses += 1;
              if (Date.now() > deadline || !(restartsHomestead(config) || (unanswered && misses <= PROGRESS_MISSES))) {
                throw unanswered && !restartsHomestead(config) ? new Error(`Homestead stopped answering while ${config.name} updated (${error.message})`) : error;
              }
              states[key] = {...(states[key] || {}), phase: restartsHomestead(config) ? "restarting" : "reconnecting"}; paint();
              await new Promise(resolve => setTimeout(resolve, 3000));
              continue;
            }
            states[key] = state; paint();
            if (rolloutChanged(result, state)) throw new Error("App changed during monitoring; review remaining updates again");
            if (state.phase === "failed") throw new Error("Rollout failed");
            if (state.phase === "ready") { refreshAfterRollout(); break; }
            if (Date.now() > deadline) throw new Error("Monitoring timed out; check its job before continuing");
            await new Promise(resolve => setTimeout(resolve, 2000));
          }
        } catch (error) {
          // Already on the newest image - updated elsewhere since the review,
          // by GitOps for one: nothing to do, and no reason to stop the rest.
          if (!job && /no image update is currently available/i.test(error.message || "")) {
            states[key] = {phase: "up to date", ready: 0, desired: 0}; paint();
            continue;
          }
          failures.push({...config, job, error: `${asSentence(error.message)} No automatic retry: ${job ? "check its job" : "review it again"} before trying it again.`});
          const rest = rows.slice(index + 1);
          for (const remaining of rest) states[rolloutKey(remaining.config)] = {phase: "not started", ready: 0, desired: 1};
          IMAGE_QUEUE = rest.length ? {skip: () => run(index + 1)} : null;
          paint();
          return;
        }
      }
      paint();
      complete = true;
    } finally {
      if (!complete && window.homesteadUpdateQueueActive === active) {
        try { sessionStorage.removeItem("homestead.selfUpdate"); } catch (e) { /* no session storage */ }
      }
      // Keep the hold while Skip and continue is offered. Closing the dialog
      // makes active() false, so an abandoned queue never blocks later notices.
      if (window.homesteadUpdateQueueActive === active && (complete || !IMAGE_QUEUE || !active())) {
        delete window.homesteadUpdateQueueActive;
        const served = window.__homesteadQueuedVersion;
        delete window.__homesteadQueuedVersion;
        if (served && window.homesteadUpdated) window.homesteadUpdated(served);
      }
    }
  };
  paint();
  await run(0);
};
let IMAGE_QUEUE = null;     // the stopped queue, while Skip and continue can resume it
// Progress reads in a row with no answer before the queue stops: about a
// minute and a half, with each read's own 10 s timeout and 3 s pause.
const PROGRESS_MISSES = 20;

/* After a rollout: the list and the waiting updates as they are now, so the
   page shows the new image without a refresh. The server has re-asked about
   this app; the page repaints when its dialog is closed, or at once if not. */
async function refreshAfterRollout() {
  try {
    const [wl] = await Promise.all([api("/api/workloads"), new Promise(r => setTimeout(r, 1500)).then(() => loadImageUpdates(false, true))]);
    STATE.data.wl = wl;
  } catch (e) { return; }
  if (STATE.view !== "workloads") return;
  if ($("#modal").classList.contains("hidden")) renderWorkloads();
  else window.__repaintAfterModal = () => { if (STATE.view === "workloads") renderWorkloads(); };
}
window.refreshAfterRollout = refreshAfterRollout;
window.imageQueueSkip = () => IMAGE_QUEUE?.skip();
// A rollout's job from the queue, opened over it: Back returns to the queue.
window.imageQueueJob = id => { pushModal(); jobsDialog(); selectJob(id); };
const asSentence = text => {
  const words = String(text || "Rollout failed").trim();
  return words.charAt(0).toUpperCase() + words.slice(1) + (/[.!?]$/.test(words) ? "" : ".");
};

// Polling updates status without closing details the reader has opened.
function paintRollout(host, html) {
  if (!host) return;
  const open=[...(host.querySelectorAll?.("details[open][data-disclosure]") || [])].map(el=>el.dataset.disclosure);
  host.innerHTML=html;
  for(const el of host.querySelectorAll?.("details[data-disclosure]") || [])if(open.includes(el.dataset.disclosure))el.open=true;
}

function batchUpdateMarkup(items, states, startFailures = [], reconnecting = false, queueMode = false) {
  const failureMap = Object.fromEntries(startFailures.map(item => [rolloutKey(item), item]));
  const phaseOf = item => states[rolloutKey(item)]?.phase;
  const ready = items.filter(item => phaseOf(item) === "ready" && !failureMap[rolloutKey(item)]);
  const current = items.filter(item => phaseOf(item) === "up to date");
  const failedItems = items.filter(item => failureMap[rolloutKey(item)] || phaseOf(item) === "failed");
  const notStarted = items.filter(item => phaseOf(item) === "not started");
  const done = ready.length + current.length;
  const pending = items.filter(item => !ready.includes(item) && !current.includes(item));
  const failed = failedItems.length > 0;
  const running = pending.find(item=>{const s=states[rolloutKey(item)];return s && !["queued","not started","failed"].includes(s.phase) && !failureMap[rolloutKey(item)];});
  // One count, the same everywhere: the header and the bar agree (#295).
  const counts = [[ready.length, "updated"], [current.length, "already up to date"], [failedItems.length, "failed"], [notStarted.length, "not started"]]
    .filter(([n]) => n).map(([n, words]) => `${n} ${words}`).join(" · ");
  const row = item => {
      const key = rolloutKey(item), state = states[key], startError = failureMap[key];
      const phase = startError ? "failed" : state?.phase || "starting";
      const tone = phase === "ready" || phase === "up to date" ? "ok" : phase === "failed" ? "bad" : phase === "not started" ? "neutral" : "warn";
      // The ready count can be the old pod's: what the new one waits for says more.
      const waiting = !["ready", "up to date"].includes(phase) && (state?.pods || []).find(pod => pod.blocked);
      const actions = startError && queueMode ? [startError.job ? UI.button("Open job", `imageQueueJob(${jsArg(startError.job)})`) : "",
        IMAGE_QUEUE && notStarted.length ? UI.button(`Skip and continue with ${notStarted.length} more`, "imageQueueSkip()") : ""].join("") : "";
      return `<div><span><b>${esc(item.name)}</b><small>${item.clusterName ? `${esc(item.clusterName)} · ` : ""}${esc(item.ns)}${state && state.desired ? ` · ${state.ready || 0}/${state.desired} ready` : ""}</small></span>
        ${UI.chip(phase, tone)}${startError ? `<div class="updateerror">${esc(startError.error)}</div>` : ""}
        ${actions ? `<div class="row" style="gap:8px;margin-top:6px;flex-wrap:wrap">${actions}</div>` : ""}
        ${waiting ? `<div class="dim xs">New pod waiting${waiting.node ? ` on ${esc(waiting.node)}` : ""}: ${esc(waiting.blocked)}</div>` : ""}</div>`;
    };
  const left = notStarted.length;
  const closing = !queueMode ? ""
    : left ? `<p class="ui-help">Closing ends the queue. ${left === 1 ? "The app not started keeps its" : `The ${left} apps not started keep their`} current image; review ${left === 1 ? "it" : "them"} again to update. Updates already started carry on in Jobs.</p>`
    : done + failedItems.length < items.length ? '<p class="ui-help">Closing stops the queue before the apps still waiting. Updates already started carry on in Jobs.</p>' : "";
  return `<div class="batch-rollout">
    ${reconnecting?UI.callout("warn","Some updates could not be checked","Showing last-known progress. Reconnecting automatically."):""}
    <div class="between"><div><b>${items.length} app${items.length === 1 ? "" : "s"}${counts ? ` · ${counts}` : ""}</b>
      <div class="dim xs">Updates run one app at a time${queueMode ? "; a failure stops the ones after it" : ""}.</div></div>
      ${failed ? UI.chip(queueMode ? (IMAGE_QUEUE ? "queue stopped" : "queue ended") : "needs attention", "warn") : done === items.length ? UI.chip("finished", "ok") : reconnecting ? UI.chip("reconnecting", "warn") : UI.chip("monitoring", "ok")}</div>
    ${UI.progress(items.length ? done/items.length*100 : 100, {label:`${done} of ${items.length} done`,kind:failed?"bad":"info"})}
    ${running?UI.section(`Updating ${running.name}`,rolloutProgress(states[rolloutKey(running)])):""}
    <div class="batch-rollout-list">${pending.filter(item=>item!==running).map(row).join("")}</div>
    ${done ? UI.more(`Done · ${done}`, `<div class="batch-rollout-list">${[...ready, ...current].map(row).join("")}</div>`).replace(/data-disclosure="[^"]*"/, 'data-disclosure="Updated"') : ""}
    ${closing}
    ${UI.actions(UI.cancel(queueMode ? done === items.length ? "Done" : "Close queue" : "Monitor in background"))}
  </div>`;
}

window.imageUpdateBatchApply = () => imageReviewedApply();

window.monitorImageRollouts = (items, startFailures = [], initialStates = {}, allItems = items) => {
  window.__updateTimer?.stop?.();
  const states = { ...initialStates };
  let misses = 0;
  const poll = async signal => {
    if ($("#modal").classList.contains("hidden")) return false;
    const results = await Promise.all(items.map(async item => {
      try {
        const state = await api(`/api/image-updates/progress?ns=${encodeURIComponent(item.ns)}&name=${encodeURIComponent(item.name)}`, { keep: true, signal });
        return { item, state };
      } catch (error) { return { item, error }; }
    }));
    if (UI.pollCancelled(signal)) return false;
    const successful = results.filter(result => result.state);
    misses = results.some(result=>result.error) ? misses + 1 : 0;
    successful.forEach(result => { states[rolloutKey(result.item)] = result.state; });
    if ($("#mbody")) paintRollout($("#mbody"), batchUpdateMarkup(allItems, states, startFailures, misses > 0));
    if (window.applyRole) window.applyRole();
    const terminal = items.every(item => ["ready", "failed"].includes(states[rolloutKey(item)]?.phase));
    if (terminal && !misses) {
      window.__updateTimer = null;
      setTimeout(() => loadImageUpdates(true, true), 1000);
      return false;
    }
  };
  window.__updateTimer = UI.poll(poll, 2000);
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

function pullSize(b) { return bytesText(b); }

function rolloutProgress(s) {
  const ready=s.phase === "ready", failed=s.phase === "failed", pull=s.pull || {};
  const accepted=s.observed_generation >= s.generation;
  const pulled=s.updated >= s.desired && pull.state !== "pulling" && pull.state !== "failed";
  const pulling=pull.state === "pulling";
  return `${failed?UI.callout("bad","Update needs attention","Review the failure details before retrying."):UI.progress(ready?100:pulling && pull.total_bytes?pull.percent:null, {
    label:ready?"Update complete":failed?"Update needs attention":pulling?"Downloading image":"Waiting for the replacement pods",
    detail:ready?`${s.ready}/${s.desired} pods ready`:pulling?pullDetail(s):`${s.updated || 0}/${s.desired ?? "?"} replacement pods created`,kind:failed?"bad":ready?"ok":"info"})}
    ${UI.checklist([
      {title:"Change accepted",state:accepted?"ok":"run"},
      {title:pulling?"Downloading image":"Image and replacement pods",state:pull.state==="failed"?"bad":pulled?"ok":accepted?"run":"todo",detailHtml:pull.state==="failed"?esc(pull.detail || "Image download failed"):""},
      {title:"Readiness checks",state:ready?"ok":failed?"bad":pulled?"run":"todo",detailHtml:esc(`${s.ready || 0}/${s.desired ?? "?"} pods ready; completion waits for the accepted update.`)}
    ])}
    ${s.problems?.length ? UI.callout("bad","Needs attention",s.problems.map(esc).join("<br>")):""}
    ${s.pods?.length?UI.more("Pod details",UI.insightList(s.pods.map(p=>({title:p.name,detail:[p.node || "Scheduling",p.blocked,p.waiting?.[0]?.reason].filter(Boolean).join(" · "),label:p.pull?.state==="pulling"?"Pulling image":p.phase,tone:p.blocked?"warn":p.phase==="Running"?"ok":"info"})))):""}`;
}
function rolloutMarkup(s) {
  return `<div class="ui-stack">${rolloutProgress(s)}${UI.actions(
    (s.can_rollback?UI.button("Review rollback",`imageRollback(${jsArg(s.ns)},${jsArg(s.name)})`,{kind:s.phase==="failed"?"danger":"",attrs:'data-need="operator"'}):"") +
    (s.phase==="ready"?UI.button("Done","closeModal();go('workloads')",{kind:"pri"}):""),s.phase==="ready"?"":UI.cancel("Monitor in background"))}</div>`;
}

window.monitorImageRollout = (ns, name) => {
  window.__updateTimer?.stop?.();
  modal("Rollout · " + name, '<div class="empty"><span class="spin2"></span> waiting for Kubernetes…</div>', true);
  let misses = 0, lastState = null;
  const poll = async signal => {
    if ($("#modal").classList.contains("hidden")) return false;
    try {
      const s = await api(`/api/image-updates/progress?ns=${encodeURIComponent(ns)}&name=${encodeURIComponent(name)}`, { keep: true, signal });
      misses = 0; lastState = s; paintRollout($("#mbody"),rolloutMarkup(s));
      if (window.applyRole) window.applyRole();
      if (s.phase === "ready" || s.phase === "failed") {
        window.__updateTimer = null;
        setTimeout(() => loadImageUpdates(true, true), 1000);
        return false;
      }
    } catch (e) {
      if (UI.pollCancelled(signal)) return false;
      misses++;
      paintRollout($("#mbody"), UI.callout("warn","Connection lost","Retrying automatically. The update may still be running; showing the last known progress.") + (lastState ? rolloutMarkup(lastState) : UI.actions(UI.cancel("Monitor in background"))));
    }
  };
  window.__updateTimer = UI.poll(poll, 2000);
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
  if (!workload) return toast("App details are not available yet", "bad");
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
      <div class="consolestate" id="consoleState">not connected · choose a pod and container, then connect</div>
      <div class="nodeterm" id="consoleTerm" aria-label="Container terminal"></div>`
      : `<div class="empty"><b>No running pod with an application container</b><br><span class="dim">Start the app and wait until its pod is running.</span></div>`}`, true);
  // What is typed into the shell is not an edit to this dialog: closing it asks nothing.
  window.__modalGuard = () => "";
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

/* A container's shell in a real terminal, as a host's (nodeShell): what the
   shell sends to drive the screen - colours, bracketed paste, a prompt
   redrawn - is drawn, not printed, and keys go straight to it. */
window.consoleConnect = async (attempt = 0) => {
  if (window.__consoleSocket) window.__consoleSocket.close();
  const pod = $("#consolePod")?.value, container = $("#consoleContainer")?.value;
  if (!pod || !container) return toast("Choose a running pod and container", "bad");
  const shells = ["/bin/sh", "/bin/bash", "/bin/ash"];
  const selected = $("#consoleShell").value;
  const shell = selected === "auto" ? shells[Math.min(attempt, shells.length - 1)] : selected;
  const ns = window.__consoleWorkload.ns;
  const state = text => { const el = $("#consoleState"); if (el) el.textContent = text; };
  state(`connecting · ${shell}`);
  $("#consoleConnect").textContent = "Reconnect";
  try { await loadXterm(); } catch (e) { return state(e.message); }
  const host = $("#consoleTerm");
  if (!host) return;
  let term = window.__consoleTerm;
  if (!term || !host.contains(term.element)) {
    if (term) { try { term.dispose(); } catch (_) { /* gone with its dialog */ } }
    const css = getComputedStyle(document.documentElement);
    term = new Terminal({ cursorBlink: true, fontSize: 13, scrollback: 5000, convertEol: false,
      fontFamily: css.getPropertyValue("--mono").trim() || "ui-monospace, Menlo, Consolas, monospace",
      theme: { background: "#0b0b0d", foreground: "#e8e8ea" } });
    term.__fit = new FitAddon.FitAddon();
    term.loadAddon(term.__fit);
    term.open(host);
    // Ctrl+C with text selected copies it, as in a terminal; only without a
    // selection does it reach the shell as an interrupt.
    term.attachCustomKeyEventHandler(event => {
      if (event.type === "keydown" && event.ctrlKey && !event.shiftKey && event.key.toLowerCase() === "c" && term.hasSelection()) {
        navigator.clipboard?.writeText(term.getSelection()).catch(() => {});
        return false;
      }
      return true;
    });
    term.onData(data => {
      const socket = window.__consoleSocket;
      if (socket?.readyState === WebSocket.OPEN) socket.send(JSON.stringify({ type: "input", data }));
    });
    window.__consoleTerm = term;
  }
  const protocol = location.protocol === "https:" ? "wss:" : "ws:";
  const query = new URLSearchParams({ ns, pod, container, shell });
  const socket = new WebSocket(fleetSocketUrl(`${protocol}//${location.host}/api/console?${query}`));
  window.__consoleSocket = socket;
  window.__consoleAttempt = attempt;
  const resize = () => {
    try { term.__fit.fit(); } catch (_) { /* not laid out yet */ }
    if (socket.readyState === WebSocket.OPEN) socket.send(JSON.stringify({ type: "resize", cols: term.cols, rows: term.rows }));
  };
  socket.onopen = () => { state(`connected · ${shell}`); resize(); term.focus(); };
  socket.onmessage = event => {
    let message;
    try { message = JSON.parse(event.data); } catch (_) { return; }
    if (message.type === "output") term.write(message.data);
    else if (message.type === "error") {
      const canFallback = selected === "auto" && attempt < shells.length - 1 && /not found|executable|no such file/i.test(message.data || "");
      if (canFallback) { term.write(`\r\n\x1b[33m${shell} unavailable; trying ${shells[attempt + 1]}…\x1b[0m\r\n`); return consoleConnect(attempt + 1); }
      term.write(`\r\n\x1b[31m${message.data || "Console error"}\x1b[0m\r\n`);
    } else if (message.type === "disconnected") state(`disconnected · ${message.reason || "session ended"}`);
  };
  socket.onclose = () => { if (window.__consoleSocket === socket) state("disconnected · use Reconnect to try again"); };
  socket.onerror = () => { if (window.__consoleSocket === socket) state("connection unavailable · check role and pod state"); };
  if (window.__consoleResize) window.__consoleResize.disconnect();
  window.__consoleResize = new ResizeObserver(resize);
  window.__consoleResize.observe(host);
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
      UI.button("Preview manifest", "previewYaml()") + UI.button("Review deployment", "doDeploy()", {kind:"pri",disabled:!!DCFG.app_profile?.blocked}),
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

function deployCapacityHtml(plan, overlap = false, imageChange = false) {
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
      plan.blocked ? UI.callout("bad", blocked, `${concerns}${overlap ? "" : "<p>This is a placement blocker, not just a capacity warning. Resolve the listed scheduler, hardware or storage constraints before proceeding.</p>"}`)
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
window.deployReviewReady = () => {
  const review = DEPLOY_REVIEW;
  const ready = !DEPLOY_SUBMITTING && review && !review.plan?.blocked &&
    (review.config.target_mode !== "existing" || $("#deployConfirm")?.checked) &&
    (!review.plan?.requires_confirmation || $("#deployCapacityConfirm")?.checked);
  if ($("#deployGo")) $("#deployGo").disabled = !ready;
  return !!ready;
};
function deployInvalid(message, selector) {
  const field=selector?document.querySelector(selector):null, pane=field?.closest('.stepper-pane');
  if(pane)UI.selectSection("containerDeploy",pane.dataset.key);
  for(let parent=field?.parentElement;parent && parent!==pane;parent=parent.parentElement)if(parent.tagName==="DETAILS")parent.open=true;
  field?.focus();toast(message,"bad");
}
window.doDeploy = async () => {
  if (DEPLOY_SUBMITTING) return;
  const sequence = ++DEPLOY_REVIEW_SEQUENCE;
  DEPLOY_REVIEW = null;
  try { await logoReady("#d_icon"); } catch (e) { return deployInvalid(e.message, "#d_icon"); }
  if (sequence !== DEPLOY_REVIEW_SEQUENCE) return;
  const c = collect();
  if (c.app_profile?.blocked) return toast(c.app_profile.label || "this template is not directly compatible", "bad");
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
  try {
    const plan = await api("/api/preview", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(c) });
    if (sequence !== DEPLOY_REVIEW_SEQUENCE) return;
    if (plan.app_profile?.blocked) return toast(plan.app_profile.label || "this app needs a Kubernetes-specific design", "bad");
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
      ${joining ? `<label class="switch dependency-confirm"><input type="checkbox" id="deployConfirm" onchange="deployReviewReady()"> ${plan.capacity.rollout?.paused ? "Save the paused template; resuming is reviewed again" : plan.capacity.rollout?.replicas === 0 ? "Change the stopped app's template without starting it" : `Restart every container in ${esc(c.target_workload)} as its pods roll out`}</label>` : ""}
      ${plan.capacity?.requires_confirmation && !plan.capacity.blocked ? `<label class="switch dependency-confirm"><input type="checkbox" id="deployCapacityConfirm" onchange="deployReviewReady()"> Deploy it despite the warnings</label>` : ""}
      <details><summary>Manifest preview</summary><pre>${esc(JSON.stringify({ deployment: plan.deployment, service: plan.service }, null, 2))}</pre></details>
      ${UI.actions(`<button data-dialog-dismiss="true" class="btn" onclick="closeModal()">Cancel</button><button class="btn pri" id="deployGo" ${joining || plan.capacity?.requires_confirmation || plan.capacity?.blocked ? "disabled" : ""} onclick="confirmDeploy()">${joining ? "Add container & restart pod" : "Deploy app"}</button>`)}</div>`, true);
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
  // An unread network is not "no addresses": the caller says it could not read it (#620).
  const net = networkData || await mustRead("/api/network", "the network");
  // The cluster's own address (Harvester's management VIP, an ingress
  // controller's) and addresses other software owns are never offered: an app
  // sharing the management VIP is how host joining breaks.
  const closed = { ...(net?.platform_addresses || {}), ...(net?.foreign_addresses || {}) };
  for (const ip of net?.node_ips || []) closed[ip] = "Cluster host address";
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

/* The Address step - how clients reach a workload, and on which ports -
   drawn the same in Deploy (prefix d) and Edit (prefix e). Edit cannot turn
   host networking on or off; a workload deployed with it shows it as set. */
function addressStepHtml(prefix, cfg, vips, sharedVip, { lanHtml = "", portsHtml = "", onChange = "" } = {}) {
  const editing = prefix !== "d", host = cfg.network_mode === "host";
  const change = onChange ? ` onchange="${onChange}"` : "";
  const mode = value => cfg.network_mode === value ? "selected" : "";
  const vip = value => cfg.vip_mode === value ? "selected" : "";
  return `
      <div class="sec">Network ${tip("Kubernetes replaces Docker bridge networking with Services. Use a dedicated VIP for DNS servers and other apps that must own common ports.")}</div>
      <div class="f2"><div class="f"><label>Access mode</label><select id="${prefix}_net"${change}${editing && host ? " disabled" : ""}>
        <option value="loadbalancer" ${mode("loadbalancer")}>LAN access (VIP)</option>
        <option value="internal" ${mode("internal")}>Cluster only</option>
        ${!editing || host ? `<option value="host" ${mode("host")}>Host network (advanced)</option>` : ""}
        <option value="lan" ${mode("lan")}>Its own LAN address (bridged)</option></select></div>
        <div class="f"><label>${nodeAddressesOnly() ? `LAN address ${tip(NODE_ADDRESS_TIP)}` : "VIP allocation"}</label><select id="${prefix}_vip_mode"${change}>
          ${nodeAddressesOnly() ? nodeAddressOption() : `${nodeAddressChoice(cfg.vip_mode === "nodes" || (cfg.vip_mode === "shared" && !sharedVip))}
          ${nodeAddressBeside() && !sharedVip ? "" : `<option value="shared" ${vip("shared")}>Default workload VIP${sharedVip ? ` · ${esc(sharedVip)}` : " · configure in Networking"}</option>`}
          <option value="auto" ${vip("auto")}>New automatic VIP${vips.freeCount ? ` · ${vips.freeCount} free` : ""}</option>
          <option value="manual" ${vip("manual")}>Specific VIP</option>`}</select></div></div>
      ${editing && host ? '<p class="ui-help">It uses its host\'s network, set when it was deployed. Deploy it again to change that.</p>' : ""}
      <div class="f" id="${prefix}_vip_wrap"><label>Specific VIP</label>${vipPicker(prefix, cfg.lb_ip || "", vips, editing)}</div>
      ${lanHtml}
      ${UI.more("How addresses work", nodeAddressesOnly()
        ? "Host addresses expose each port on every host. Each port can belong to one Service. Add kube-vip in Settings for dedicated addresses. Host networking binds directly to one node."
        : "The default VIP shares an address using separate ports. Automatic and specific VIPs give the app another address. Bridged LAN networking adds its own interface; host networking binds directly to one node.")}
      <div class="sec">Ports ${tip("Container port is where the process listens. LAN port is what clients use through the Kubernetes Service. TCP and UDP on the same number are separate listeners.")}</div>${portsHtml}`;
}
window.addressStepHtml = addressStepHtml;

function vipPicker(prefix, current, choices, keep = false) {
  // Editing a workload keeps the address it has, whatever else is said of
  // it: a change elsewhere in it is never refused for its address (#306).
  const kept = keep && current && choices.blocked?.[current];
  if (choices.blocked?.[current] && !kept) current = "";
  const own = choices.own || [], labels = choices.labels || {};
  const known = kept || choices.free.includes(current) || choices.used.some(v => v.ip === current) || own.some(v => v.ip === current);
  const typed = !!current && !known;
  const option = (value, label) => `<option value="${esc(value)}" ${value === current ? "selected" : ""}>${esc(label)}</option>`;
  return `<select id="${prefix}_lb_pick" onchange="vipPicked(${jsq(prefix)})">
      <option value="" ${!current ? "selected" : ""}>Choose an address…</option>
      ${kept ? option(current, `${current} · its address now`) : ""}
      ${own.some(v => v.free) ? `<optgroup label="Your VIPs - free">${own.filter(v => v.free).map(v => option(v.ip, `${v.ip}${v.label ? ` · ${v.label}` : ""}`)).join("")}</optgroup>` : ""}
      ${choices.free.length ? `<optgroup label="Free in the IP pools (${choices.free.length})">${choices.free.slice(0, 60).map(ip => option(ip, ip)).join("")}</optgroup>` : ""}
      ${choices.used.length ? `<optgroup label="In use - shared with what is there">${choices.used.map(v =>
        option(v.ip, `${v.ip}${labels[v.ip] ? ` · ${labels[v.ip]}` : ""} · ${v.services} service${v.services === 1 ? "" : "s"} · ports ${v.listeners.map(l => l.port).slice(0, 5).join(", ")}`)).join("")}</optgroup>` : ""}
      <option value="__typed" ${typed ? "selected" : ""}>Type an address…</option></select>
    <input id="${prefix}_lb_ip" class="mono" value="${esc(current || "")}" placeholder="192.0.2.250" data-ipam ${typed && !kept ? "" : 'style="display:none"'}>
    ${kept ? `<p class="ui-help">${esc(choices.blocked[current])}</p>` : ""}`;
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
const storeDate = seconds => seconds ? fmtDay(seconds) : "";

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
    <div class="row store-tags">${label ? UI.chip(label, "neutral") : ""}
      ${app.deploy?.app_profile ? UI.chip(app.deploy.app_profile.label, app.deploy.app_profile.level === "dependency" ? "warn" : "neutral") : ""}
      ${app.beta ? UI.chip("beta", "warn") : ""}</div>
    <div class="store-metric">${storeMetric(app, mode)}</div>
    ${mode === "spotlight" && app.spotlight?.reason ? `<div class="store-why">“${esc(app.spotlight.reason)}”</div>` : ""}
    <div class="ds">${esc(app.desc || "No description provided.")}</div>
    <div class="rp">${esc(app.repo)}</div>
    <div class="row store-card-actions"><button class="btn sm" onclick="event.stopPropagation();storeDetails(${key})">Details</button>
      <button class="btn pri sm" onclick="event.stopPropagation();storeInstall(${key})">Configure &amp; deploy</button></div></div>`;
}

/* Say where the listings come from, once the server has said: the public feed,
   or one set in Settings. The trademark line shows whatever the feed. */
function storeSource(source) {
  const host = $("#s_source");
  if (!host) return;
  const feed = !source ? ""
    : source.default
    ? `Listings are read on demand from the public <a href="https://github.com/Squidly271/AppFeed" target="_blank" rel="noopener">Community Applications feed</a> and cached for six hours; another feed can be set in Settings. Homestead is independent and is not endorsed by the catalogue maintainers. `
    : `Listings are read from <span class="mono">${esc(source.url)}</span>, set in Settings, and cached for six hours. Templates come from whoever publishes that feed; review what each one asks for before deploying it. `;
  host.innerHTML = `${feed}Unraid® is a registered trademark of Lime Technology, Inc. This application is not affiliated with, endorsed, or sponsored by Lime Technology, Inc.`;
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
  paint(`${UI.pageHeader(`Community catalogue`, `Third-party Community Applications templates adapted into reviewed Kubernetes workloads`, `<input class="search" id="s_q" aria-label="Search the App Store" placeholder="plex, nextcloud, jellyfin…" value="${esc(STATE.q)}" style="width:260px;padding-left:16px">
      <button class="btn pri" onclick="storeSearch()">Search</button>`, {actionsClass:`store-search`})}
    <div class="store-browse-head"><div class="seg store-modes" id="s_modes">
      ${Object.entries(STORE_MODES).map(([mode, [label]]) => `<button data-mode="${mode}" class="${STORE_MODE === mode ? "on" : ""}" onclick="storeBrowse(${jsq(mode)})">${label}</button>`).join("")}
    </div></div>
    <div id="s_res"><div class="empty"><span class="spin2"></span>loading catalogue…</div></div>
    <div class="note catalogue-notice" id="s_source"></div>`);
  $("#s_q").addEventListener("keydown", e => { if (e.key === "Enter") { e.preventDefault(); storeSearch(); } });
  storeSource(null);
  if (STATE.q) storeSearch(); else storeBrowse(STORE_MODE);
}
window.storeSearch = async () => {
  const q = $("#s_q").value.trim();
  if (!q) return storeBrowse(STORE_MODE);
  $$("#s_modes button").forEach(button => button.classList.remove("on"));
  $("#s_res").innerHTML = `<div class="empty"><span class="spin2"></span>searching catalogue…</div>`;
  try {
    const r = await api("/api/appstore?q=" + encodeURIComponent(q));
    storeSource(r.source);
    $("#s_res").innerHTML = r.apps.length
      ? `<div class="dim small" style="margin-bottom:12px">${r.total} match${r.total === 1 ? "" : "es"} · showing ${r.apps.length}</div>
        <div class="apps stagger">${r.apps.map(a => storeCard(a, "search")).join("")}</div>`
      : `<div class="empty">nothing matched “${esc(q)}”</div>`;
  } catch (e) { $("#s_res").innerHTML = UI.error(e, { retry: "storeSearch()" }); }
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
  } catch (e) { $("#s_res").innerHTML = UI.error(e, { retry: `storeBrowse(${jsArg(mode)})` }); }
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
  catch (e) {
    $("#mbody").innerHTML = UI.error(e, { retry: `storeDetails(${jsArg(key)})`, dialog: true,
      title: e.body?.loading ? "The catalogue is still loading" : "Couldn't read this app" });
    return;
  }
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
        <div class="row store-tags">${(app.categories || []).slice(0, 4).map(c => UI.chip(c, "neutral")).join("")}
          ${app.beta ? UI.chip("beta", "warn") : ""}${app.privileged ? UI.chip("asks for privileged", "bad") : ""}</div></div>
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
    ${UI.actions(`<button class="btn pri" onclick="wlPrimaryPortSave(${jsq(ns)},${jsq(name)})">Save</button>
      <button data-dialog-dismiss="true" class="btn" onclick="closeModal()">Cancel</button>`)}`);
};
window.wlPrimaryPortSave = async (ns, name) => {
  const port = +(document.querySelector('input[name="pp"]:checked')?.value || 0);
  try {
    const r = await api("/api/workload/primary-port", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ ns, name, port }) });
    toast(r.detail, "ok"); closeModal(); refresh(true);
  } catch (e) { toast(e.message, "bad"); }
};
