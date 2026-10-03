/* Persistent operation tray — one place for every long-running cluster action. */
let operationTimer = null;
let operationPanelOpen = false;

const operationActive = operation => !["succeeded", "failed", "cancelled"].includes(operation.status);
const operationTone = status => status === "succeeded" ? "ok" : status === "failed" ? "crit" :
  status === "cancelled" ? "low" : "warn";

function operationAge(value) {
  if (!value) return "";
  const seconds = Math.max(0, Math.floor((Date.now() - new Date(value).getTime()) / 1000));
  if (seconds < 60) return `${seconds}s`;
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m`;
  if (seconds < 86400) return `${Math.floor(seconds / 3600)}h`;
  return `${Math.floor(seconds / 86400)}d`;
}

function renderOperations() {
  const tray = $("#jobTray"), items = STATE.data.operations || [];
  const active = items.filter(operationActive);
  // Clearing one at a time is fine for a stray failure and tedious after a
  // batch, so the header offers the lot - and says how many, because it will
  // not touch anything still running.
  const finished = items.filter(item => !operationActive(item) && item.dismissible !== false);
  const clear = $("#jobClear");
  if (clear) {
    // Only relabel when there is something to clear, so it never reads
    // "Clear 0 finished" in the moment between clearing and hiding.
    clear.hidden = !finished.length;
    if (finished.length) clear.textContent = `Clear ${finished.length} finished`;
  }
  if (!items.length) {
    tray.classList.add("hidden");
    $("#jobList").innerHTML = "";
    paintJobsDialog();
    if (window.paintBell) paintBell();
    if (window.uvmCopiesPaint) uvmCopiesPaint();
    return;
  }
  tray.classList.remove("hidden");
  tray.classList.toggle("open", operationPanelOpen);
  const latest = active[0] || items[0];
  $("#jobSummary").innerHTML = `<span class="jobpulse ${active.length ? "" : "idle"}"></span>
    <span class="jobsummarycopy"><b>${active.length ? `${active.length} active job${active.length === 1 ? "" : "s"}` : "Recent jobs"}</b>
    <small>${esc(latest.title)} · ${esc(latest.message || latest.status)}</small></span>
    ${active.length ? `<span class="jobsummarypct">${Math.round(latest.progress || 0)}%</span>` : ""}
    <span class="jobchev" aria-hidden="true"><svg viewBox="0 0 24 24"><path d="M6 15l6-6 6 6"/></svg></span>`;
  $("#jobSummary").setAttribute("aria-expanded", String(operationPanelOpen));
  const list = items.slice(0, 12).map(operationCard).join("");
  $("#jobList").innerHTML = list;
  paintJobsDialog();
  if (window.paintBell) paintBell();
  if (window.uvmCopiesPaint) uvmCopiesPaint();
  if (window.applyRole) window.applyRole();
}

function operationCard(operation) { return `<article class="jobitem" data-operation="${esc(operation.id)}">
    <div class="jobitemtop"><div><b>${esc(operation.title)}</b>
      <span>${esc(operation.resource?.namespace ? operation.resource.namespace + " · " : "")}${esc(operation.resource?.kind || operation.kind)}</span></div>
      <span class="pill ${operationTone(operation.status)}">${esc(operation.status)}</span></div>
    ${operation.progress != null && Number.isFinite(Number(operation.progress)) ? `<div class="jobmeter" role="progressbar" aria-label="Reported progress" aria-valuemin="0" aria-valuemax="100" aria-valuenow="${Math.max(0, Math.min(100, Number(operation.progress)))}"><span class="${operation.status === "failed" ? "failed" : ""}" style="width:${Math.max(0, Math.min(100, Number(operation.progress)))}%"></span></div>` : ""}
    <div class="jobfoot"><span>${esc(operation.message || "")}</span><span>${operationAge(operation.finished_at || operation.started_at)}</span></div>
    <div class="jobactions">
      <button class="btn sm" onclick="${operation.kind === 'cluster-shutdown' ? 'clusterShutdown()' : `openOperation(${jsq(operation.href || "/")},${jsq(operation.id || "")})`}">Open</button>
      <button class="btn sm" data-tip="Every step it has taken, and the output of what does its work" onclick="operationLog(${jsq(operation.id)})">${icon("log")}Log</button>
      ${operation.preparation_archivable ? `<button class="btn sm" data-need="admin" onclick="selfDataArchiveReview(${jsq(operation.id)})">Archive preparation</button>` : ""}
      ${operation.power_recovery ? `<button class="btn sm" data-need="admin" onclick="powerRecoveryReview(${jsq(operation.id)})">Inspect outcome</button>` : ""}
      ${operation.storage_recovery ? `<button class="btn sm" data-need="admin" onclick="storageRecoveryReview(${jsq(operation.id)})">Review storage move</button>` : ""}
      ${operation.mutation_recovery ? `<button class="btn sm" data-need="admin" onclick="powerRecoveryReview(${jsq(operation.id)},${operation.kind === 'import-create' ? "'import'" : 'true'})">Inspect ${operation.kind === "k3s-cluster" ? "batch" : operation.kind === "import-create" ? "import" : "save"} outcome</button>` : ""}
      ${operation.resumable ? `<button class="btn sm pri" data-need="admin" data-tip="Run the steps that are left, from the one it stopped at" onclick="resumeOperation(${jsq(operation.id)})">Carry on</button>` : ""}
      ${operation.cleanable ? `<button class="btn sm ${operation.tracking_only ? "" : "danger"}" data-need="admin" data-tip="${operation.tracking_only ? "Review retained resources and recovery choices; nothing is deleted" : "Says what it left behind and what cleanup removes"}" onclick="cancelOperation(${jsq(operation.id)})">${operation.tracking_only ? "Review retained resources" : "Clean up"}</button>` : ""}
      ${operation.cancellable ? `<button class="btn sm danger" data-need="${operation.copy_recovery ? "admin" : "operator"}" data-tip="Reviews what can be stopped or recovered before anything changes" onclick="cancelOperation(${jsq(operation.id)})">${operation.rename_recovery || operation.copy_recovery ? "Inspect outcome" : operation.status === "cancelling" ? "Cancel again" : "Cancel"}</button>` : ""}
      ${operationActive(operation) || operation.dismissible === false ? "" : `<button class="btn sm" data-need="operator" data-tip="Remove this finished entry from Jobs. Volumes and required recovery records are retained." onclick="dismissOperation(${jsq(operation.id)})">Dismiss</button>`}
    </div>
  </article>`; }

let selectedJobId = "";
const completedJobs = items => items.filter(item => ["succeeded", "cancelled"].includes(item.status) && item.dismissible !== false);
window.selectJob = id => { selectedJobId = id; paintJobsDialog(); window.applyRole?.(); };
function paintJobsDialog() {
  const host = $("#jobsDialogList");
  if (!host) return;
  const items = STATE.data.operations || [];
  const attention = items.filter(item => item.status === "failed");
  const active = items.filter(operationActive);
  const completed = items.filter(item => ["succeeded", "cancelled"].includes(item.status));
  const selected = items.find(item => item.id === selectedJobId) || attention[0] || active[0] || completed[0];
  selectedJobId = selected?.id || "";
  // Capture disclosure state before replacing the DOM; polling must not close it.
  const expanded = [...host.querySelectorAll("details[open][data-disclosure]")].map(el => [el.dataset.disclosure, el.closest("[data-operation]")?.dataset.operation || ""]);
  const rows = items => items.map(item => ({key:item.id, title:item.title,
    detail:`${item.status}${item.resource?.namespace ? ` · ${item.resource.namespace}` : ""}`, attention:item.status === "failed"}));
  const stale = STATE.operationsStale ? UI.callout("warn", "Connection lost", "Showing the last known status. Checking again; completion is not assumed.") : "";
  host.innerHTML = stale + (selected ? UI.masterDetail([
    {key:"attention", title:`Needs attention · ${attention.length}`, items:rows(attention)},
    {key:"active", title:`Running · ${active.length}`, items:rows(active)},
    {key:"Completed", title:`Completed · ${completed.length}`, items:rows(completed), collapsed:true}
  ], selectedJobId, operationCard(selected), {label:"Jobs", detailLabel:"Selected job", onSelect:id => `selectJob(${jsArg(id)})`}) : '<div class="empty small">No jobs.</div>');
  if (selected) {
    const actions = host.querySelector(".dialog-master-content .jobactions");
    const secondary = [...actions.children].filter(button => /^(Log|Dismiss)$/.test(button.textContent.trim()));
    if (secondary.length) {
      const html = secondary.map(button => button.outerHTML).join("");
      secondary.forEach(button => button.remove());
      actions.insertAdjacentHTML("afterend", UI.more("Log and history", `<div class="jobactions">${html}</div>`));
    }
    if (selected.dismissible === false) host.querySelector(".dialog-master-content .jobfoot").insertAdjacentHTML("afterend", '<p class="ui-help">This recovery record is retained until its outcome is resolved.</p>');
  }
  for (const detail of host.querySelectorAll("details[data-disclosure]")) {
    if (expanded.some(([label, id]) => label === detail.dataset.disclosure && id === (detail.closest("[data-operation]")?.dataset.operation || ""))) detail.open = true;
  }
  const clear = $("#jobsClearCompleted"), count = completedJobs(items).length;
  if (clear) { clear.hidden = !count; clear.textContent = `Clear completed (${count})`; }
  window.applyRole?.();
}
window.jobsDialog = () => {
  modal("Jobs", `<div id="jobsDialogList"></div>${UI.actions(UI.button("Clear completed", "dismissCompletedOperations()", {id:"jobsClearCompleted", attrs:'data-need="operator"'}), UI.cancel("Close"))}`);
  paintJobsDialog();
  window.applyRole?.();
};
window.dismissCompletedOperations = async () => {
  const button = $("#jobsClearCompleted"); if (button) button.disabled = true;
  try {
    for (const item of completedJobs(STATE.data.operations || [])) {
      await api("/api/operations/dismiss", {method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify({id:item.id})});
      STATE.data.operations = (STATE.data.operations || []).filter(row => row.id !== item.id);
    }
  } catch (error) { toast(error.message, "bad"); }
  finally { renderOperations(); if (button) button.disabled = false; }
};
window.operationActive = operationActive;

async function refreshOperations(immediate = false) {
  clearTimeout(operationTimer);
  if (!ME) return;
  try {
    // keep: the tray outlives every page. A read abandoned by navigating away
    // never settles, which stopped this loop for good - the job an App Store
    // install started sat at "queued" while its container ran.
    STATE.data.operations = await api("/api/operations", { keep: true });
    STATE.operationsStale = false;
    renderOperations();
  } catch (_) { STATE.operationsStale = true; paintJobsDialog(); }
  if (!ME) return;
  const active = (STATE.data.operations || []).some(operationActive);
  operationTimer = setTimeout(refreshOperations, immediate || active ? 3000 : 15000);
  window.__operationTimer = operationTimer;
}

window.startOperationChecks = () => refreshOperations(true);
window.noteOperation = operation => {
  if (!operation) return;
  const items = STATE.data.operations || [];
  STATE.data.operations = [operation, ...items.filter(item => item.id !== operation.id)];
  renderOperations();
  refreshOperations(true);
};
window.toggleOperations = () => {
  operationPanelOpen = !operationPanelOpen;
  renderOperations();
};
window.openOperation = (href, id = "", savedOperation = null) => {
  const url = new URL(href || "/", window.location.origin);
  const route = HomesteadRouter.resolve(url.pathname);
  const operation = savedOperation?.id === id ? savedOperation : (STATE.data.operations || []).find(item => item.id === id);
  operationPanelOpen = false;
  renderOperations();
  if(operation?.kind==='disk-v2-convert' && window.diskV2Watch) return diskV2Watch(id);
  // Recovery jobs need their retained-resource review, not just the page
  // where the resources live. Every review still checks the saved job afresh.
  if (operation?.status === "failed" && operation.mutation_recovery) return powerRecoveryReview(id, operation.kind === "import-create" ? "import" : true);
  if (operation?.power_recovery) return powerRecoveryReview(id);
  if (operation?.status === "failed" && operation.storage_recovery) return storageRecoveryReview(id);
  if (operation?.tracking_only && operation?.cleanable) return cancelOperation(id);
  // The link names what the job is about. It used to become the site-wide
  // search, which then narrowed every page until cleared by hand; now the
  // page opens whole and the item is brought into view and marked.
  // find= in links made now; q= in the links of jobs from older releases.
  const q = (url.searchParams.get("find") || url.searchParams.get("q") || "").trim();
  url.searchParams.delete("find");
  url.searchParams.delete("q");
  go(route.view, { params: Object.fromEntries(url.searchParams) });
  const opensItsOwn = ["reclass", "image-update", "image-rollback"].includes(operation?.kind);
  if (q && !opensItsOwn) highlightInPage(q);
  // An image update or rollback opens its rollout, as it looked when it ran.
  const target = operation?.resource || {};
  if (operation?.kind === "vm-power" && target.name && window.vmOpen)
    setTimeout(() => vmOpen(target.namespace || "lab", target.name), 150);
  if (["image-update", "image-rollback"].includes(operation?.kind) && target.name && window.monitorImageRollout)
    setTimeout(() => monitorImageRollout(target.namespace || "lab", target.name), 150);
  if (operation?.kind === "reclass" && window.reclassWatch) setTimeout(() => reclassWatch(operation.id), 150);
  if (operation?.kind === "self-data-prepare" && window.replicasMoveData) setTimeout(() => replicasMoveData(operation.id), 150);
};
/* Bring the row or card that mentions this name into view and mark it for a
   moment, once the page has drawn. The name must match a whole word, so
   "frigate" does not land on "frigate-config". */
function highlightInPage(name, tries = 8) {
  const words = new RegExp(`(^|[^a-z0-9-])${name.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")}($|[^a-z0-9-])`, "i");
  const find = () => [...document.querySelectorAll("#views tr, #views .card, #views [data-vol], #views .wlcard, #views .row-item")]
    .filter(el => el.offsetParent !== null && words.test(el.innerText || ""))
    .sort((a, b) => (a.innerText || "").length - (b.innerText || "").length)[0];
  const el = find();
  if (!el) { if (tries > 0) setTimeout(() => highlightInPage(name, tries - 1), 400); return; }
  el.scrollIntoView({ block: "center", behavior: "smooth" });
  el.classList.add("flash-find");
  setTimeout(() => el.classList.remove("flash-find"), 2600);
}
window.highlightInPage = highlightInPage;

window.dismissFinishedOperations = async () => {
  try {
    const result = await api("/api/operations/dismiss", { method: "POST",
      headers: { "Content-Type": "application/json" }, body: JSON.stringify({ all: true }) });
    toast(result.detail || "finished jobs cleared", "ok");
  } catch (e) { toast(e.message, "bad"); }
  refreshOperations(true);
};

/* A job stopped part-way whose steps are safe to repeat - a storage class
   change caught mid-swap - runs the rest from the step it stopped at. */
window.resumeOperation = async id => {
  try {
    await api("/api/operations/resume", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ id }) });
    toast("Carrying on from where it stopped", "ok");
    const op = (STATE.data.operations || []).find(o => o.id === id);
    refreshOperations(true);
    if (op?.kind === "reclass" && window.reclassWatch) reclassWatch(id);
  } catch (e) { toast(e.message, "bad"); }
};

let STORAGE_RECOVERY = null, STORAGE_RECOVERY_SEQ = 0, STORAGE_RECOVERY_BUSY = false;
window.storageRecoveryReview = async id => {
  if (STORAGE_RECOVERY_BUSY) return;
  clearInterval(window.__reclassTimer); window.__reclassWatchToken = null;
  STORAGE_RECOVERY = null;
  const sequence = ++STORAGE_RECOVERY_SEQ;
  modal("Review storage move", '<div id="storageRecoveryContent"><div class="empty"><span class="spin2"></span>Checking this move…</div></div>', true, "operation-review");
  const host = $("#storageRecoveryContent");
  try {
    const response = await api("/api/operations/storage-recovery/preview", {method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({id})});
    if (sequence !== STORAGE_RECOVERY_SEQ || $("#storageRecoveryContent") !== host) return;
    const p = response.plan, tokens = response.tokens || {};
    if (!p || p.id !== id || typeof p.can_continue !== "boolean" || typeof p.can_pause !== "boolean" ||
        !Array.isArray(p.resources) || !Array.isArray(p.warnings) || !Array.isArray(p.blockers) ||
        (p.can_continue && !tokens.continue) || (p.can_pause && !tokens.pause)) throw new Error("The review is incomplete. Refresh it before taking action.");
    const action = p.can_continue ? "continue" : p.can_pause ? "pause" : null;
    STORAGE_RECOVERY = {id, action, tokens, host};
    const phases = {stop:"Stopping workloads",copy:"Copying and checking",cutover:"Switching storage",restart:"Restarting workloads",done:"Complete"};
    const leads = {stop:"Continue stopping the affected workloads before copying their data.",
      copy:"Continue copying and checking the data. Workloads remain stopped during the copy.",
      cutover:"Switch the verified copy into place. Restarting workloads gets its own capacity review.",
      restart:"Restart workloads on the copied volume after fresh capacity checks. The original data is retained."};
    const issues = p.blockers.length ? p.blockers : p.warnings;
    const writeNames = {accepted:"Confirmed",intent:"No receipt",uncertain:"Response lost",unverified:"Unverified receipt",refused:"Rejected"};
    host.innerHTML = UI.lead(action === "continue" ? leads[p.phase] || "Continue from the confirmed steps after fresh safety checks." :
      action === "pause" ? "Pause the next steps without deleting data or rolling back. A copy job already running can still finish." :
      "This move needs inspection before it can continue. Its workloads and data have been left in place.") +
      (issues.length ? UI.callout(p.blockers.length ? "bad" : "warn", p.blockers.length ? "Needs attention" : "Before you continue", `<ul>${issues.map(x=>`<li>${esc(x)}</li>`).join("")}</ul>`) : "") +
      UI.facts([["Volume",esc(p.claim)],["Stage",esc(phases[p.phase] || p.phase)],["Storage",`${esc(p.from_class || "—")} → ${esc(p.to_class || "—")}`]]) +
      UI.section("Current position", `<p>${esc(p.message || "Waiting for the next step")}</p>`) +
      UI.more("Workloads and retained resources", UI.table(["Workload","Type"], (p.workloads || []).map(c=>[esc(c.name),esc(c.kind)])) +
        UI.table(["Resource","Last request","Observed now"], p.resources.map(r=>[esc(r.resource?.name || "—"),esc(writeNames[r.receipt] || r.receipt),esc(r.relationship)]))) +
      (action ? UI.ack("storageRecoveryAck", action === "continue" ? "I approve continuing with the warnings shown above." : "I understand that pausing does not stop an already running copy.", {onchange:"storageRecoveryReady()"}) : "") +
      UI.actions(UI.cancel("Close") + UI.button("Refresh review", `storageRecoveryReview(${jsArg(id)})`) +
        (action ? UI.button(action === "continue" ? "Continue move" : "Pause move", "storageRecoveryApply()", {kind:"pri",id:"storageRecoveryApply",disabled:true}) : ""));
  } catch (error) {
    if (sequence === STORAGE_RECOVERY_SEQ && $("#storageRecoveryContent") === host)
      host.innerHTML = UI.callout("bad","Review unavailable",esc(error.message)) + UI.actions(UI.cancel("Close"));
  }
};
window.storageRecoveryReady = () => {
  const ready = !!(STORAGE_RECOVERY?.action && $("#storageRecoveryAck")?.checked && !STORAGE_RECOVERY_BUSY);
  if ($("#storageRecoveryApply")) $("#storageRecoveryApply").disabled = !ready;
  return ready;
};
window.storageRecoveryApply = async () => {
  if (!storageRecoveryReady()) return;
  const pending = STORAGE_RECOVERY;
  STORAGE_RECOVERY = null; STORAGE_RECOVERY_BUSY = true; storageRecoveryReady();
  try {
    const result = await api("/api/operations/storage-recovery/act", {method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({
      id:pending.id, action:pending.action, capacity_token:pending.tokens[pending.action], confirm_capacity:true})});
    if ($("#storageRecoveryContent") !== pending.host) return;
    toast(result.detail,"ok"); closeModal();
  } catch (error) {
    if ($("#storageRecoveryContent") === pending.host)
      pending.host.innerHTML = UI.callout("warn","Check the job before trying again",`${esc(error.message)} Nothing was retried automatically.`) +
        UI.actions(UI.cancel("Close") + UI.button("Refresh review",`storageRecoveryReview(${jsArg(pending.id)})`));
  } finally { STORAGE_RECOVERY_BUSY = false; refreshOperations(true); }
};

let POWER_RECOVERY = null, POWER_RECOVERY_SEQ = 0, POWER_RECOVERY_BUSY = false;
window.powerRecoveryReview = async (id, mutation = false) => {
  if (POWER_RECOVERY_BUSY) return;
  POWER_RECOVERY = null;
  const sequence = ++POWER_RECOVERY_SEQ;
  const endpoint=mutation ? "/api/operations/vm-recovery" : "/api/operations/power-recovery";
  modal(mutation === "import" ? "Inspect import" : mutation ? "Inspect VM configuration outcome" : "Inspect VM power outcome", '<div id="powerRecoveryLoading" class="empty"><span class="spin2"></span>Checking current resources…</div>', true, "operation-review");
  try {
    const review = await api(endpoint + "/preview", {method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({id})});
    if (sequence !== POWER_RECOVERY_SEQ || !$("#powerRecoveryLoading")) return;
    const p = review.plan;
    if (!p || typeof p.blocked !== "boolean" || (!p.blocked && !review.capacity_token)) throw new Error("Recovery review is incomplete. Nothing was resolved.");
    if (mutation && !p.blocked && (!Array.isArray(p.resources) || !p.resources.length)) throw new Error("Resource inspection is incomplete. Nothing was resolved.");
    POWER_RECOVERY = {id,...review,endpoint};
    const observed=p.observed || {}, resource=p.resource || {};
    if (mutation === "import" || p.action === "import-create") {
      POWER_RECOVERY.importing = true;
      const kindName = kind => ({PersistentVolumeClaim:"Volume",Deployment:"App",Service:"Network Service",Job:"Copy Job"})[kind] || kind;
      const writeName = value => ({accepted:"Saved",intent:"Sent; no receipt",uncertain:"Response lost",unverified:"Receipt not verified",refused:"Rejected","not dispatched":"Not sent"})[value] || value;
      $("#mbody").innerHTML = UI.lead("Inspect what this import left behind. Resolving its record keeps all data and does not start or stop apps.") +
        UI.callout(p.blocked ? "bad" : "warn", p.blocked ? "Not ready to resolve" : "The import outcome is unverified",
          p.blocked ? (p.blockers || []).map(esc).join("<br>") : "A sent request may still finish late. Nothing will be retried, started or deleted.") +
        UI.section("Retained resources", UI.table([{label:"Resource"},{label:"Last request"},{label:"Now"}],
          (p.resources || []).map(row=>[`${esc(kindName(row.resource.kind))} · ${esc(row.resource.name)}`,esc(writeName(row.last_write)),esc(row.relationship)]))) +
        UI.more("Identity checks and recovery limits", (p.warnings || []).map(w=>`<p>${esc(w)}</p>`).join("") +
          (p.resources || []).map(row=>`<p style="overflow-wrap:anywhere">${esc(row.resource.name)}<br>Recorded UID: ${esc(row.expected?.uid || "unverified")}<br>Current UID: ${esc(row.current?.uid || "not found")}</p>`).join("")) +
        (!p.blocked ? UI.ack("powerRecoveryAck", "I inspected the retained resources and accept that the old request may still finish.", {onchange:"powerRecoveryReady()"}) : "") +
        UI.actions(UI.cancel("Keep tracking") + UI.button("Resolve as unknown", "powerRecoveryResolve()", {kind:"pri",id:"powerRecoveryApply",disabled:true}));
      if ($(".modalbox")) $(".modalbox").scrollTop=0;
      return;
    }
    if (p.action === "k3s-cluster") {
      if ($("#mtitle")) $("#mtitle").textContent = "Review k3s batch";
      const writeName = value => ({accepted:"Saved",intent:"Sent; no receipt",uncertain:"Response lost",unverified:"Unverified",refused:"Rejected","not dispatched":"Not sent"})[value] || value;
      $("#mbody").innerHTML = UI.lead(`Review the planned VMs and retained resources for <b>${esc(p.confirm || "this batch")}</b>. Stopping tracking keeps them in the cluster and releases this job's block on moving Homestead data.`) +
        UI.callout(p.blocked ? "bad" : "warn", p.blocked ? "Not ready to resolve" : "Guest health is unverified",
          p.blocked ? (p.blockers || []).map(esc).join("<br>") : "Guest setup may still continue. This does not retry the batch, stop its VMs, delete data or verify that k3s is ready.") +
        UI.section("Retained resources", UI.table(["Resource", "Last request", "Now"], (p.resources || []).map(row => [
          `${row.resource.kind === "VirtualMachine" ? "VM" : esc(row.resource.kind)} · ${row.resource.kind === "VirtualMachine" ? `<a onclick="vmOpen(${jsq(row.resource.namespace)},${jsq(row.resource.name)})">${esc(row.resource.name)}</a>` : esc(row.resource.name)}`,
          esc(writeName(row.last_write)), esc(row.relationship)]))) +
        UI.more("Identity checks and recovery limits", (p.warnings || []).map(w => `<p>${esc(w)}</p>`).join("") +
          (p.resources || []).map(row => `<p style="overflow-wrap:anywhere">${esc(row.resource.name)}<br>Recorded UID: ${esc(row.expected?.uid || "unverified")}<br>Current UID: ${esc(row.current?.uid || "not found")}</p>`).join("")) +
        (!p.blocked ? UI.field(`Type ${esc(p.confirm)} to stop tracking`, '<input id="powerRecoveryName" autocomplete="off" oninput="powerRecoveryReady()">') +
          UI.ack("powerRecoveryAck", "I inspected the planned VMs and retained resources. I accept the unknown outcome and that guest setup or an earlier request may still finish.", {onchange:"powerRecoveryReady()"}) : "") +
        UI.actions(UI.cancel("Keep tracking") + UI.button("Stop tracking batch", "powerRecoveryResolve()", {kind:"pri", id:"powerRecoveryApply", disabled:true}));
      if ($(".modalbox")) $(".modalbox").scrollTop = 0;
      return;
    }
    $("#mbody").innerHTML = `<div class="update-review">
      <div class="reviewbox"><b>${mutation ? "Resolve tracking, keep resources" : "Resolve tracking, not power"}</b><p class="small">${esc(resource.namespace || "")} / ${esc(resource.name || "VM job")} · requested ${esc(p.action || "VM change")} · ${esc(p.dispatch_phase || "dispatcher busy")}</p>
        <p class="small">This is not a retry, rollback, cancellation or proof of success. It records an admin inspection with the original outcome still unknown.</p></div>
      ${p.blockers?.length ? `<div class="note bad">${p.blockers.map(esc).join(" · ")}</div>` : ""}
      ${mutation && p.resources ? `<div class="reviewbox"><b>Resource write receipts and current identities</b>${p.resources.map(row=>`<div class="small" style="margin-top:10px;overflow-wrap:anywhere"><b>${esc(row.resource.kind)} · ${esc(row.resource.namespace)}/${esc(row.resource.name)}</b><br>Last write: ${esc(row.last_write)} · ${esc(row.relationship)}<br>Recorded UID: ${esc(row.expected?.uid || "no verified receipt")}<br>Current UID: ${esc(row.current?.uid || "not found")}</div>`).join("")}</div>` : ""}
      ${p.observed ? `<div class="reviewbox"><b>Current observations</b>
        <p class="small">VM: ${esc(observed.vm_status)} · policy: ${esc(observed.run_strategy)}</p>
        <p class="small">Instance: ${esc(observed.instance_phase)} · Ready: ${observed.ready ? "yes" : "no"} · Paused: ${observed.paused ? "yes" : "no"}</p>
        <p class="small">Queued power changes: ${esc(observed.queued_changes)} · Original VM: ${observed.same_vm ? "same identity" : "missing or replaced"}</p>
        <p class="small" style="overflow-wrap:anywhere">Reviewed VM UID: ${esc(resource.original_uid)}<br>Current VM UID: ${esc(observed.vm?.uid || "not found")}<br>Current instance UID: ${esc(observed.instance?.uid || "not found")}</p></div>` : ""}
      ${(p.warnings || []).map(w=>`<div class="note warn small">${esc(w)}</div>`).join("")}
      ${!p.blocked ? `<div class="f"><label>Type ${esc(p.confirm)} to confirm inspection</label><input id="powerRecoveryName" autocomplete="off" oninput="powerRecoveryReady()"></div>
        <label class="check"><input id="powerRecoveryAck" type="checkbox" onchange="powerRecoveryReady()"> I inspected the ${p.action === "k3s-cluster" ? "planned VMs" : "VM"}${mutation ? " and retained resources" : ""}. I accept that the old request may still take effect late and that resolving this record allows a new, separately reviewed ${mutation ? "VM change" : "power action"}.</label>` : ""}
      ${UI.actions(`<button data-dialog-dismiss="true" class="btn" onclick="closeModal()">Keep tracking</button><button class="btn danger" id="powerRecoveryApply" disabled onclick="powerRecoveryResolve()">Record unknown outcome</button>`)}</div>`;
    if ($(".modalbox")) $(".modalbox").scrollTop=0;
  } catch(error) {
    if (sequence !== POWER_RECOVERY_SEQ || !$("#powerRecoveryLoading")) return;
    POWER_RECOVERY=null;
    $("#mbody").innerHTML=`<div class="note bad">${esc(error.message)}</div><button class="btn" onclick="closeModal()">Close</button>`;
  }
};
window.powerRecoveryReady = () => {
  const ready=!!(POWER_RECOVERY && !POWER_RECOVERY_BUSY && !POWER_RECOVERY.plan.blocked && $("#powerRecoveryAck")?.checked && (POWER_RECOVERY.importing || $("#powerRecoveryName")?.value===POWER_RECOVERY.plan.confirm));
  if ($("#powerRecoveryApply")) $("#powerRecoveryApply").disabled=!ready;
  return ready;
};
window.powerRecoveryResolve = async () => {
  if (!powerRecoveryReady()) return;
  const review=POWER_RECOVERY, button=$("#powerRecoveryApply");
  POWER_RECOVERY=null;POWER_RECOVERY_BUSY=true;button.disabled=true;
  try {
    const result=await api(review.endpoint + "/resolve", {method:"POST",headers:{"Content-Type":"application/json"},
      body:JSON.stringify({id:review.id,capacity_token:review.capacity_token,confirm_capacity:true,confirm:review.plan.confirm,acknowledge_unknown:true})});
    if (result.operation) noteOperation(result.operation);
    toast(result.detail,"ok");closeModal();
  } catch(error) {
    if ($("#powerRecoveryApply")===button) {
      $("#mbody").insertAdjacentHTML("afterbegin",`<div class="note bad">${esc(error.message)}. Nothing was retried. Check the job before opening a new review.</div>`);
      if ($(".modalbox")) $(".modalbox").scrollTop=0;
    }
    toast(error.message,"bad");
  } finally {POWER_RECOVERY_BUSY=false;refreshOperations(true);}
};

/* Cancelling a job says first what it would do: what is put back, what is
   stopped with the work so far kept, and what Kubernetes cannot take back -
   for a few jobs only the tracking stops. Nothing changes until the button
   in the dialog is pressed, and a cancel that deletes data asks for the name. */
const CANCEL_LEAD = {
  rollback: "Cancelling stops this job and puts back what it changed.",
  stop: "Cancelling stops this job. What it has already done stays done.",
  forget: "This job cannot be stopped from Homestead. Cancelling only stops tracking it here.",
};
window.cancelOperation = async id => {
  let plan;
  try {
    plan = await api("/api/operations/cancel-plan", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ id }) });
  } catch (e) { toast(e.message, "bad"); refreshOperations(true); return; }
  operationPanelOpen = false;
  renderOperations();
  if (plan.image_review) return imageRollback(plan.image_review.ns, plan.image_review.name);
  const list = rows => `<ul>${rows.map(row => `<li>${esc(row)}</li>`).join("")}</ul>`;
  const high = plan.severity === "high";
  const cleanup = plan.cleanup && plan.mode !== "forget";
  const body = !plan.can
    ? `<div class="note warn"><b>${plan.copy_recovery ? "The copy hold cannot be released yet." : "It cannot be cancelled at this step."}</b><div>${esc(plan.why_not || "")}</div></div>
       ${UI.actions(`<button data-dialog-dismiss="true" class="btn" onclick="closeModal()">Close</button>`)}`
    : `<p>${esc(plan.lead || (cleanup ? "This job failed part-way. Cleaning up removes what it left behind; the job stays in the list as failed."
        : plan.cleanup ? "This job failed. Stopping tracking keeps its failed outcome and all retained resources. Nothing is deleted or stopped in the cluster."
        : CANCEL_LEAD[plan.mode] || CANCEL_LEAD.stop))}</p>
      <div class="dim xs">${esc(plan.message || "")} · ${Math.round(plan.progress || 0)}% done</div>
      ${plan.undo.length ? `<div class="note ${high ? "warn" : ""}" style="margin-top:12px"><b>${cleanup ? "What cleaning up removes" : plan.mode === "rollback" ? "What cancelling puts back" : "What cancelling does"}</b>${list(plan.undo)}</div>` : ""}
      ${plan.keeps.length ? `<div class="note" style="margin-top:10px"><b>${plan.mode === "forget" && !plan.copy_recovery ? "What carries on" : "What stays as it is"}</b>${list(plan.keeps)}</div>` : ""}
      ${plan.options.map(option => `<label class="switch" style="margin-top:12px"><input type="checkbox" data-cancel-option="${esc(option.id)}" onchange="cancelOperationGate()" ${option.default ? "checked" : ""}> ${esc(option.label)}</label>
        ${option.detail ? `<div class="dim xs">${esc(option.detail)}</div>` : ""}`).join("")}
      ${plan.confirm ? `<div class="f" style="margin-top:12px"><label>Type <b class="mono">${esc(plan.confirm)}</b> to confirm</label>
        <input id="oc_confirm" autocomplete="off" oninput="cancelOperationGate()"></div>` : ""}
      ${plan.needs === "admin" && !can("admin") ? `<div class="note warn" style="margin-top:12px">Cancelling this job needs an admin.</div>` : ""}
      ${UI.actions(`<button class="btn ${high ? "danger" : "pri"}" id="oc_go" data-need="${esc(plan.needs || "operator")}" ${plan.confirm ? "disabled" : ""}
          onclick="cancelOperationGo(${jsq(plan.id)})">${esc(cleanup ? "Remove what it made" : plan.action)}</button>
        <button data-dialog-dismiss="true" class="btn" onclick="closeModal()">${plan.mode === "forget" ? "Keep tracking" : plan.cleanup ? "Leave it" : "Keep it running"}</button>`)}`;
  modal(`${plan.copy_recovery ? "Inspect storage copy" : plan.mode === "forget" ? "Stop tracking" : cleanup ? "Clean up" : "Cancel"} · ${plan.title}`, body, false, "operation-review");
  window.__cancelPlan = plan;
  if (window.applyRole) window.applyRole();
};
window.cancelOperationGate = () => {
  const plan = window.__cancelPlan || {};
  const go = $("#oc_go");
  if (go) go.disabled = (!!plan.confirm && ($("#oc_confirm")?.value || "").trim() !== plan.confirm) ||
    (!!plan.copy_recovery && !$("[data-cancel-option='ack']")?.checked);
};
window.cancelOperationGo = async id => {
  const plan = window.__cancelPlan || {};
  const confirm = ($("#oc_confirm")?.value || "").trim();
  if (plan.confirm && confirm !== plan.confirm) return toast(`type ${plan.confirm} exactly to confirm`, "bad");
  const options = Object.fromEntries([...document.querySelectorAll("[data-cancel-option]")]
    .map(box => [box.dataset.cancelOption, box.checked]));
  if (plan.copy_recovery && !options.ack) return toast("Check the storage and acknowledge partial data first", "bad");
  const go = $("#oc_go");
  if (go) { go.disabled = true; go.textContent = plan.copy_recovery ? "Releasing hold…" : "Cancelling…"; }
  try {
    const result = await api("/api/operations/cancel", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ id, options, confirm }) });
    toast(result.detail || "cancelled", "ok");
    closeModal();
    if (result.operation) window.noteOperation(result.operation);
    if (typeof refresh === "function") refresh(true);
  } catch (e) {
    toast(e.message, "bad");
    if (go) { go.disabled = false; go.textContent = plan.action || "Cancel"; }
  }
  refreshOperations(true);
};

/* A job's log: each step it has said, with the time, then - where it has
   some - the output of what does its work: an import's copy, a rollout's
   newest pod, a k3s node's console. Follows along while the job runs. */
window.operationLog = async id => {
  if (window.__logTimer) clearInterval(window.__logTimer);
  operationPanelOpen = false;
  renderOperations();
  const title = (STATE.data.operations || []).find(o => o.id === id)?.title || "Job";
  const sequence = window.__logSequence = (window.__logSequence || 0) + 1;
  modal(`Log · ${title}`, `<div class="logtools"><span id="oplogState"><span class="spin2"></span> loading</span>
      <label class="switch"><input type="checkbox" id="oplogFollow" checked> Follow latest</label></div>
    <div id="oplogBody"></div>`, true);
  const stamp = t => { const d = new Date(t); return isNaN(d) ? "" : d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit", hourCycle: "h23" }); };
  const followLatest = () => {
    if (!$("#oplogFollow")?.checked) return;
    const steps = $("#oplogBody .oplog-steps");
    if (steps) steps.scrollTop = steps.scrollHeight;
    $$("#oplogBody pre").forEach(pre => { pre.scrollTop = pre.scrollHeight; });
  };
  $("#oplogFollow").onchange = followLatest;
  let polling = false, active = true;
  const poll = async () => {
    if ($("#modal").classList.contains("hidden") || !$("#oplogBody")) return clearInterval(window.__logTimer);
    if (polling || window.__logSequence !== sequence) return;
    polling = true;
    let d;
    try { d = await api(`/api/operations/log?id=${encodeURIComponent(id)}`); }
    catch (e) { if (window.__logSequence === sequence && $("#oplogState")) $("#oplogState").textContent = e.message; return; }
    finally { polling = false; }
    if (window.__logSequence !== sequence || $("#modal").classList.contains("hidden") || !$("#oplogBody")) return;
    const follow = $("#oplogFollow")?.checked;
    const box = $(".modalbox"), outerScroll = box?.scrollTop || 0;
    const stepScroll = $("#oplogBody .oplog-steps")?.scrollTop || 0;
    const kept = new Map([...$$("#oplogBody pre")].map(pre => [pre.dataset.source, pre.scrollTop]));
    $("#oplogBody").innerHTML = `<div class="between oplog-head"><span class="pill ${operationTone(d.status)}">${esc(d.status)}</span>
        <span class="small">${esc(d.message || "")}</span><b class="mono">${Math.round(d.progress || 0)}%</b></div>
      <div class="jobmeter"><span class="${d.status === "failed" ? "failed" : ""}" style="width:${Math.max(2, Math.min(100, d.progress || 0))}%"></span></div>
      <div class="ctitle" style="margin-top:14px">Steps</div>
      <ol class="oplog-steps">${(d.history || []).map(h => `<li class="oplog-${esc(h.s)}"><span class="mono dim xs">${esc(stamp(h.t))}</span>
        <span>${esc(h.m)}</span>${h.p ? `<span class="mono dim xs">${Math.round(h.p)}%</span>` : ""}</li>`).join("") || '<li class="dim">Nothing recorded yet.</li>'}</ol>
      ${(d.sources || []).map(src => `<div class="ctitle" style="margin-top:14px">${esc(src.title)}</div>
        ${src.note ? `<div class="dim small">${esc(src.note)}</div>` : ""}
        ${src.kind === "disk-copy" ? operationCopyProgress(src.progress, operationActive(d)) : ""}
        ${src.text ? `<pre class="logview oplog-pre${src.kind === "disk-copy" ? " oplog-copy" : ""}" data-source="${esc(JSON.stringify([src.pod || "", src.title]))}">${esc(src.text)}</pre>` : ""}`).join("")}
      ${d.sources?.length ? "" : '<p class="dim xs" style="margin-top:12px">This kind of job runs no pod of its own; its steps above are its log.</p>'}`;
    const steps = $("#oplogBody .oplog-steps");
    if (steps) steps.scrollTop = follow ? steps.scrollHeight : stepScroll;
    $$("#oplogBody pre").forEach(pre => { pre.scrollTop = follow ? pre.scrollHeight : (kept.get(pre.dataset.source) || 0); });
    if (box) box.scrollTop = outerScroll;
    active = operationActive(d);
    $("#oplogState").innerHTML = active ? '<span class="ld"></span> live · refreshes every 3s' : `${esc(d.status)} · ${esc(stamp(d.finished_at || d.updated_at))}`;
    if (!active) clearInterval(window.__logTimer);
  };
  await poll();
  if (active && window.__logSequence === sequence && $("#oplogBody")) window.__logTimer = setInterval(poll, 3000);
};

function operationCopyProgress(progress, active) {
  if (!progress || !Number.isFinite(progress.percent)) return '<p class="dim small">Waiting for disk-copy progress…</p>';
  const percent = Math.max(0, Math.min(100, progress.percent));
  const approximate = progress.coarse && percent < 100 ? "~" : "";
  const size = Number.isFinite(progress.bytes) && progress.total_bytes > 0
    ? `${approximate}${(progress.bytes / 1e9).toFixed(1)} / ${(progress.total_bytes / 1e9).toFixed(1)} GB` : "";
  const speed = !progress.coarse && Number.isFinite(progress.bytes_per_second) ? `${(progress.bytes_per_second / 1e6).toFixed(1)} MB/s` : "";
  const remaining = progress.eta_seconds;
  const seconds = Math.ceil(remaining || 0), hours = Math.floor(seconds / 3600), minutes = Math.floor(seconds % 3600 / 60);
  const duration = [hours ? `${hours}h` : "", minutes ? `${minutes}m` : "", `${seconds % 60}s`].filter(Boolean).join(" ");
  const eta = !active ? percent >= 100 ? "Stream transferred" : "Copy stopped" : percent >= 100 ? "Stream transferred; waiting for CDI to finish"
    : progress.coarse ? "Speed / ETA unavailable (1% progress)" : Number.isFinite(remaining) && remaining >= 0 ? `ETA ${duration}` : "ETA estimating";
  return `<div class="oplog-copy-progress"><b class="mono">${approximate}${percent.toFixed(1)}%</b><span>${esc([size, speed, eta].filter(Boolean).join(" · "))}</span></div>
    <div class="jobmeter"><span style="width:${percent}%"></span></div>`;
}

window.dismissOperation = async id => {
  try {
    await api("/api/operations/dismiss", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ id }) });
    STATE.data.operations = (STATE.data.operations || []).filter(item => item.id !== id);
    renderOperations();
  } catch (error) { toast(error.message, "bad"); }
};

$("#jobSummary").onclick = toggleOperations;
