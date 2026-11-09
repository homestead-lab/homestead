/* Homestead Containers: the list's entry, the update badges and the Image updates center (#911). One of the scripts views-workloads.js was split into; they share the page's globals and load in the order index.html lists them. */
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
  window.__imageUpdateLoop?.stop?.();
  clearTimeout(window.__imageUpdateStart);
  window.__imageUpdateStart = setTimeout(() => loadImageUpdates(false, false), 3500);
  // Every 15 minutes; a tab coming back is no reason to ask the registries again.
  window.__imageUpdateLoop = UI.poll(async () => { await loadImageUpdates(false, false); },
    15 * 60 * 1000, { later: true, dialog: false, readOnShow: false });
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
  // A closed rollout stops its monitor. Ask for the current server report
  // each time this dialog opens, even when this page already has a report.
  const opening = {};
  window.__imageUpdateCenterOpening = opening;
  modal("Image updates", '<div id="imageUpdatesLoading" class="empty"><span class="spin2"></span>reading image updates…</div>');
  const report = await loadImageUpdates(false, true);
  if (window.__imageUpdateCenterOpening !== opening || !$("#imageUpdatesLoading") || $("#modal").classList.contains("hidden")) return;
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


// Node only (tests): the same names, for require(). The browser has no `module`.
if (typeof module === "object" && module.exports) module.exports = {
  viewWorkloads, workloadReleasedRow, updateKey, workloadUpdate, updateOwner, updateOwnerTip, updateOwnerPill, setUpdateOwner, paintUpdateBadge, pullBar, workloadPull, workloadBlocked, podShortName, imageParts, treeImage, treePodName, workloadHierarchy, checkedAgo, loadImageUpdates, updateVersions, updateNoticeButton
};
