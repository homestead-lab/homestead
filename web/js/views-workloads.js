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
  const notice = $("#updateNotice"), noticeBadge = $("#updateNoticeBadge");
  if (!notice || !noticeBadge) return;
  const total = count + errors;
  notice.classList.toggle("hidden", !total);
  notice.classList.toggle("has-errors", !!errors);
  noticeBadge.textContent = total;
  notice.setAttribute("aria-label", `${count} image update${count === 1 ? "" : "s"} available${errors ? `, ${errors} registry check failure${errors === 1 ? "" : "s"}` : ""}`);
}

function workloadHierarchy(w) {
  const pods = w.pods || [];
  const podCount = w.pod_count ?? pods.length;
  const containerCount = w.container_count ?? pods.reduce((n, p) => n + (p.container_count || p.containers?.filter(c => c.kind !== "init").length || 0), 0);
  const podLabel = `${podCount} pod${podCount === 1 ? "" : "s"}`;
  const containerLabel = `${containerCount} container${containerCount === 1 ? "" : "s"}`;
  return `<details class="workloadtree">
    <summary><span class="tree-kind">${esc(w.kind || "Deployment")}</span><span class="tree-arrow">→</span>
      <span>${podLabel}</span><span class="tree-arrow">→</span><span>${containerLabel}</span>
      <span class="tree-hint">show runtime objects</span></summary>
    <div class="tree-body">${pods.map(p => `<div class="tree-pod">
      <div class="tree-pod-head"><span class="tree-branch">Pod</span><b class="mono">${esc(p.name)}</b>
        <span class="pill ${p.ready ? "ok" : p.phase === "Pending" ? "med" : "crit"}">${p.ready ? "ready" : esc(p.phase || "pending")}</span>
        <span class="dim xs">${esc(p.node || "unscheduled")}${p.restarts ? ` · ${p.restarts} restart${p.restarts === 1 ? "" : "s"}` : ""}</span></div>
      <div class="tree-containers">${(p.containers || []).map(c => `<div class="tree-container">
        <span class="tree-branch ${c.kind === "init" ? "init" : ""}">${c.kind === "init" ? "Init" : "Container"}</span>
        <b>${esc(c.name)}</b><span class="pill ${c.ready || (c.kind === "init" && c.state === "Completed") ? "ok" : c.state === "running" ? "med" : "low"}">${esc(c.state || "pending")}</span>
        ${c.restarts ? `<span class="tag warn">${c.restarts} restart${c.restarts === 1 ? "" : "s"}</span>` : ""}
        <span class="mono dim tree-image">${esc(c.image || "image unavailable")}</span>
      </div>`).join("") || `<div class="dim xs">Container detail is unavailable for this pod.</div>`}</div>
    </div>`).join("") || `<div class="dim xs">No pods exist yet. The workload controller will create them when replicas are above zero.</div>`}</div>
  </details>`;
}

async function loadImageUpdates(force = false, quiet = false) {
  const requestId = (window.__imageUpdateRequestId || 0) + 1;
  window.__imageUpdateRequestId = requestId;
  try {
    const report = await api(`/api/image-updates${force ? "?force=1" : ""}`);
    // A forced check can overtake the quiet background check kicked off by
    // viewWorkloads. A later request may also return the older non-force cache,
    // so compare server timestamps as well as request order.
    if (requestId !== window.__imageUpdateRequestId) return report;
    const existing = STATE.data.imageUpdates;
    if (HarvUpdateState.isStale(existing, report)) {
      existing.policy = report.policy || existing.policy;
      return existing;
    }
    STATE.data.imageUpdates = report;
    STATE.data.imageUpdateMap = Object.fromEntries((report.workloads || [])
      .map(x => [updateKey(x.ns, x.name), x]));
    paintUpdateBadge(report.updates || 0, report.errors || 0);
    const previous = +(localStorage.getItem("homestead.update-count") || localStorage.getItem("harvui.update-count") || 0);
    const preferences = STATE.data.appSettings?.updates || {};
    if (!quiet && preferences.notify_available !== false && report.updates > previous)
      toast(`${report.updates} container image update${report.updates === 1 ? "" : "s"} available`, "ok");
    const previousErrors = +(localStorage.getItem("homestead.update-errors") || localStorage.getItem("harvui.update-errors") || 0);
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
  setTimeout(() => loadImageUpdates(false, false), 3500);
  window.__imageUpdateLoop = setInterval(() => {
    if (!document.hidden) loadImageUpdates(false, false);
  }, 15 * 60 * 1000);
};

window.imageUpdateCenter = async () => {
  let report = STATE.data.imageUpdates;
  if (!report) {
    modal("Image updates", '<div class="empty"><span class="spin2"></span>checking registries…</div>');
    report = await loadImageUpdates(false, true);
  }
  if (!report) return;
  const affected = (report.workloads || []).filter(w => w.available || w.images?.some(image => image.error));
  const policy = report.policy || {};
  modal("Image updates", `<div class="update-center">
    <div class="note"><b>${esc(policy.policy === "notify_only" ? "Notify only" : policy.policy === "maintenance_window" ? "Maintenance window" : "Approval required")}</b>
      ${esc(policy.reason || "Every rollout requires an explicit review.")}</div>
    ${affected.length ? `<div class="settings-list">${affected.map(w => {
      const failures = (w.images || []).filter(image => image.error);
      return `<div class="settings-list-row update-center-row"><div><b>${esc(w.name)}</b><div class="dim xs mono">${esc(w.ns)}</div>
        ${failures.map(image => `<div class="updateerror">${esc(image.container)} · ${esc(image.error)}</div>`).join("")}</div>
        <div class="row">${w.available ? '<span class="pill warn">update available</span>' : ""}
        ${failures.length ? '<span class="pill crit">check failed</span>' : ""}
        <button class="btn sm" onclick="openUpdateWorkload('${esc(w.name)}')">Open</button></div></div>`;
    }).join("")}</div>` : '<div class="empty small">Images are current and registry checks succeeded.</div>'}
    <div class="row" style="margin-top:16px"><button class="btn" onclick="closeModal();go('workloads')">Open Containers</button>
      <button class="btn" onclick="checkImageUpdates()">Check now</button></div></div>`, true);
};
window.openUpdateWorkload = name => {
  closeModal();
  STATE.q = name;
  $("#globalSearch").value = name;
  go("workloads", { params: { q: name } });
};
const updateNoticeButton = $("#updateNotice");
if (updateNoticeButton) {
  updateNoticeButton.onclick = () => imageUpdateCenter();
  updateNoticeButton.onkeydown = event => {
    if (event.key === "Enter" || event.key === " ") { event.preventDefault(); imageUpdateCenter(); }
  };
}

function renderWorkloads() {
  const q = STATE.q.toLowerCase();
  const rows = (STATE.data.wl || []).filter(x => !q || x.name.includes(q) || x.ns.includes(q) ||
    x.images.join(" ").toLowerCase().includes(q) || x.nodes.join(" ").includes(q));
  const report = STATE.data.imageUpdates;
  const updateCount = report?.updates || 0;
  const updateErrors = report?.errors || 0;
  paint(`<div class="phead">
      <div><h2>Containers</h2><p>${rows.length} workload${rows.length === 1 ? "" : "s"}${q ? ` matching “${esc(q)}”` : ""} · Harvester system pods hidden</p></div>
      <div class="row"><button class="btn" onclick="checkImageUpdates()">↻ Check images</button>
      <button class="btn pri hide-sm" data-need="operator" onclick="go('deploy')">＋ Deploy</button></div></div>

    ${updateCount ? `<div class="updatebar"><div><b>${updateCount} update${updateCount === 1 ? "" : "s"} available</b>
      <span>Registry manifests were compared with the digests running in Kubernetes.</span></div>
      <span class="pill warn">review below</span></div>` : updateErrors ? `<div class="updatebar"><div><b>${updateErrors} registry check${updateErrors === 1 ? " needs" : "s need"} attention</b>
      <span>See the affected container cards and check their imagePullSecrets.</span></div><span class="pill crit">check failed</span></div>` : report ? `<div class="updatebar quiet"><div><b>Images are current</b>
      <span>Last checked ${esc(new Date(report.checked_at).toLocaleString())}</span></div><span class="pill ok">up to date</span></div>` : ""}

    <div class="cardlist">${rows.map(w => {
      const ok = w.ready === w.desired && w.desired > 0, off = w.desired === 0;
      const update = workloadUpdate(w.ns, w.name);
      const updateError = update?.images?.find(x => x.error);
      return `<div class="wcard card flat">
        <div class="between">
          <div class="row" style="gap:10px;min-width:0">
            ${appAvatar(w.name, w.icon)}
            <div style="min-width:0"><div style="font-weight:680">${esc(w.name)}</div>
              <div class="dim xs">${esc(w.ns)} · <span class="nodelink"
                onclick="moveWorkload('${w.name}','${w.ns}')">${esc(w.nodes.join(", ") || "unscheduled")}</span></div></div>
          </div>
          <div class="row">${update?.available ? '<span class="pill warn">update available</span>' : ""}
          ${updateError ? `<span class="pill low" title="${esc(updateError.error)}">registry check unavailable</span>` : ""}
          <span class="pill ${ok ? "ok" : off ? "low" : "crit"}">${w.ready}/${w.desired}</span></div>
        </div>
        <div class="wmeta">
          <div><div class="dim xs">UPTIME</div>${w.uptime ? upChip(w.uptime) : '<span class="dim">—</span>'}</div>
          <div><div class="dim xs">CPU</div><div class="mono small">${w.cpu} <span class="dim">cores</span></div></div>
          <div><div class="dim xs">RAM</div><div class="mono small">${w.mem_mb} <span class="dim">MB</span></div></div>
          <div><div class="dim xs">ACCESS</div><div>${w.ports.map(p => p.ip
            ? `<span class="plink" title="Open ${esc(svcUrl(p.ip, p.port))}" onclick="openSvc('${esc(p.ip)}',${p.port})">${p.port}<svg class="ext" width="9" height="9"><use href="#i-ext"/></svg></span>`
            : `<span class="tag">${p.port}</span>`).join("") || '<span class="dim">—</span>'}</div></div>
        </div>
        <div class="dim xs mono wimg">${w.images.map(esc).join(" · ")}
          ${hardwareTags(w.hardware || (w.gpu ? ["igpu"] : []))}</div>
        ${workloadHierarchy(w)}
        ${updateError ? `<div class="updateerror">Image check: ${esc(updateError.error)}</div>` : ""}
        <div class="row wacts">
          <button class="btn sm" title="View live container logs" onclick="wlLogs('${w.ns}','${w.pods[0] ? w.pods[0].name : ""}','${w.name}')">${icon("log")}Logs</button>
          <button class="btn sm" title="Open an audited interactive shell in a running container" data-need="operator" onclick="wlConsole('${w.ns}','${w.name}')">${icon("console")}Console</button>
          <button class="btn sm" title="Edit image, resources, environment, storage and hardware" onclick="wlEdit('${w.ns}','${w.name}')">${icon("edit")}Edit</button>
          <button class="btn sm" title="Move this workload to another eligible host" data-need="operator" onclick="moveWorkload('${w.name}','${w.ns}')">${icon("move")}Move</button>
          <button class="btn sm" title="Restart all pods in this workload" onclick="wlRestart('${w.ns}','${w.name}')">${icon("restart")}Restart</button>
          ${update?.available ? `<button class="btn sm pri" title="Review and install the available image update" data-need="operator" onclick="imageUpdateReview('${w.ns}','${w.name}')">${icon("update")}Update</button>` : ""}
          ${update?.can_rollback ? `<button class="btn sm" title="Restore the exact image digest saved before the last update" data-need="operator" onclick="imageRollback('${w.ns}','${w.name}')">${icon("rollback")}Rollback</button>` : ""}
          ${off ? `<button class="btn sm" title="Start this workload" onclick="wlScale('${w.ns}','${w.name}',1)">${icon("play")}Start</button>`
                : `<button class="btn sm" title="Scale this workload to zero" onclick="wlScale('${w.ns}','${w.name}',0)">${icon("stop")}Stop</button>`}
          <button class="btn sm danger" title="Delete the workload; persistent volumes are kept" onclick="wlDelete('${w.ns}','${w.name}')">${icon("trash")}Delete</button>
        </div></div>`;
    }).join("") || `<div class="empty">nothing here yet</div>`}</div>`);
}

window.wlScale = async (ns, name, n) => {
  try {
    await api("/api/scale", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ ns, name, replicas: n }) });
    toast(`${name} ${n ? "started" : "stopped"}`, "ok"); setTimeout(() => refresh(true), 900);
  } catch (e) { toast(e.message, "bad"); }
};
window.wlRestart = async (ns, name) => {
  try {
    await api("/api/restart", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ ns, name }) });
    toast(`${name} restarting`, "ok"); setTimeout(() => refresh(true), 1300);
  } catch (e) { toast(e.message, "bad"); }
};
window.wlDelete = async (ns, name) => {
  if (!confirm(`Delete "${name}" in ${ns}?\n\nRemoves the Deployment and its Service.\nPersistent volumes are kept.`)) return;
  try { await api(`/api/workload/${ns}/${name}`, { method: "DELETE" });
    toast(`${name} deleted`, "ok"); setTimeout(() => refresh(true), 900);
  } catch (e) { toast(e.message, "bad"); }
};
function openLogs(title, path) {
  if (window.__logTimer) clearInterval(window.__logTimer);
  modal("Logs · " + title, `<div class="logtools"><span id="logstate"><span class="spin2"></span> connecting</span>
    <label class="switch"><input type="checkbox" id="logfollow" checked> Follow latest</label></div>
    <pre class="logview">waiting for log output…</pre>`, true);
  const poll = async () => {
    if ($("#modal").classList.contains("hidden")) return clearInterval(window.__logTimer);
    try {
      const t = await api(path + (path.includes("?") ? "&" : "?") + "tail=500");
      const pre = $("#mbody .logview"); if (!pre) return;
      pre.textContent = t || "(the container is running but has not written any logs yet)";
      $("#logstate").innerHTML = '<span class="ld"></span> live · refreshes every 2s';
      if ($("#logfollow")?.checked) pre.scrollTop = pre.scrollHeight;
    } catch (e) {
      const pre = $("#mbody .logview"); if (pre) pre.textContent = "Logs unavailable\n\n" + e.message;
      if ($("#logstate")) $("#logstate").innerHTML = '<span class="cd badbg"></span> unavailable';
    }
  };
  poll(); window.__logTimer = setInterval(poll, 2000);
}

window.checkImageUpdates = async () => {
  const button = typeof event !== "undefined" ? event.currentTarget : null;
  if (button) { button.disabled = true; button.textContent = "Checking registries…"; }
  const report = await loadImageUpdates(true, false);
  if (button) { button.disabled = false; button.textContent = "↻ Check images"; }
  if (report && STATE.view === "workloads") renderWorkloads();
};

window.imageUpdateReview = (ns, name) => {
  const update = workloadUpdate(ns, name);
  if (!update) return toast("Run an image check first", "bad");
  const changes = update.images.filter(x => x.available);
  const policy = STATE.data.imageUpdates?.policy || {};
  const blocked = policy.allows_install === false;
  modal("Update · " + name, `<div class="update-review">
    <div class="note"><b>Managed update.</b> Homestead will pin the selected registry manifest by digest,
      monitor Kubernetes readiness, and keep the current immutable image ready for rollback.</div>
    ${changes.map(x => `<div class="update-image">
      <div class="between"><b>${esc(x.container)}</b><span class="tag warn">${esc(x.candidate_tag || "new digest")}</span></div>
      <div><span>Running</span><code>${esc(x.deployed)}</code></div>
      <div><span>Install</span><code>${esc(x.candidate)}@${esc((x.remote_digest || "").slice(0, 19))}…</code></div>
    </div>`).join("")}
    <div class="note ${blocked ? "dependency-danger" : ""}"><b>Cluster policy · ${esc(policy.policy === "notify_only" ? "notify only" : policy.policy === "maintenance_window" ? "maintenance window" : "approval required")}</b>
      ${esc(policy.reason || "Review and approve this digest-pinned rollout.")}</div>
    <label class="switch update-approval ${blocked ? "hidden" : ""}"><input type="checkbox" id="updateApprove"
      onchange="document.getElementById('updateInstall').disabled=!this.checked"> I reviewed the image change and approve this rollout</label>
    <div class="row" style="margin-top:18px"><button class="btn pri" id="updateInstall" data-need="operator" ${blocked ? "disabled" : "disabled"}
      onclick="imageUpdateApply('${esc(ns)}','${esc(name)}')">Install update</button>
      <button class="btn" onclick="closeModal()">Cancel</button></div></div>`, true);
  if (window.applyRole) window.applyRole();
};

window.imageUpdateApply = async (ns, name) => {
  try {
    modal("Updating · " + name, '<div class="empty"><span class="spin2"></span> preparing managed rollout…</div>', true);
    await api("/api/image-updates/apply", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ ns, name, approved: true }) });
    monitorImageRollout(ns, name);
  } catch (e) { $("#mbody").innerHTML = `<div class="empty"><b>Update could not start</b><br><span class="dim">${esc(e.message)}</span></div>`; }
};

function rolloutMarkup(s) {
  const pct = s.desired ? Math.min(100, Math.round(s.ready / s.desired * 100)) : (s.phase === "ready" ? 100 : 0);
  return `<div class="rollout-head"><span class="pill ${s.phase === "ready" ? "ok" : s.phase === "failed" ? "crit" : "warn"}">${esc(s.phase)}</span>
    <span class="mono small">${s.ready}/${s.desired} ready · ${s.updated}/${s.desired} updated</span></div>
    <div class="rollout-meter"><span style="width:${pct}%"></span></div>
    <div class="rollout-steps">
      <div class="${s.observed_generation >= s.generation ? "done" : "active"}"><i></i><span><b>Deployment accepted</b><small>Generation ${s.generation}</small></span></div>
      <div class="${s.updated >= s.desired ? "done" : "active"}"><i></i><span><b>New image pulled</b><small>${s.updated} replacement pod${s.updated === 1 ? "" : "s"} created</small></span></div>
      <div class="${s.phase === "ready" ? "done" : s.phase === "failed" ? "failed" : "active"}"><i></i><span><b>Readiness checks</b><small>${s.ready} pod${s.ready === 1 ? "" : "s"} serving</small></span></div>
    </div>
    ${s.problems?.length ? `<div class="gateerr">${s.problems.map(esc).join("<br>")}</div>` : ""}
    <div class="podprogress">${(s.pods || []).map(p => `<div><span><b>${esc(p.name)}</b><small>${esc(p.node || "scheduling")}</small></span>
      <span class="pill ${p.phase === "Running" ? "ok" : "warn"}">${esc(p.waiting?.[0]?.reason || p.phase)}</span></div>`).join("")}</div>
    <div class="row" style="margin-top:18px">
      ${s.can_rollback ? `<button class="btn ${s.phase === "failed" ? "danger" : ""}" data-need="operator" onclick="imageRollback('${esc(s.ns)}','${esc(s.name)}')">Rollback</button>` : ""}
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

window.imageRollback = async (ns, name) => {
  modal("Rollback · " + name, `<div class="note"><b>Restore the image saved before the last managed update?</b>
    The previous digest is immutable, so this does not depend on the registry tag still pointing at it.</div>
    <div class="row" style="margin-top:18px"><button class="btn danger" data-need="operator" onclick="imageRollbackApply('${esc(ns)}','${esc(name)}')">Start rollback</button>
    <button class="btn" onclick="closeModal()">Cancel</button></div>`);
  if (window.applyRole) window.applyRole();
};
window.imageRollbackApply = async (ns, name) => {
  try {
    await api("/api/image-updates/rollback", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ ns, name }) });
    monitorImageRollout(ns, name);
  } catch (e) { $("#mbody").innerHTML = `<div class="empty"><b>Rollback could not start</b><br><span class="dim">${esc(e.message)}</span></div>`; }
};
window.wlLogs = (ns, pod, workload = "", fromRoute = false) => {
  if (!fromRoute && window.setModalRoute) setModalRoute({ panel: "logs", ns, workload: workload || pod }, (workload || pod) + " logs");
  if (!pod) return modal("Logs unavailable", '<div class="empty"><b>No running pod</b><br><span class="dim small">Start the container and wait for Kubernetes to create a pod.</span></div>');
  openLogs(pod, `/api/logs?ns=${encodeURIComponent(ns)}&pod=${encodeURIComponent(pod)}`);
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
  const socket = new WebSocket(`${protocol}//${location.host}/api/console?${query}`);
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

window.consoleSend = () => {
  const input = $("#consoleInput"), socket = window.__consoleSocket;
  if (!input || !socket || socket.readyState !== WebSocket.OPEN) return toast("Connect the console first", "bad");
  socket.send(JSON.stringify({ type: "input", data: input.value + "\n" }));
  input.value = "";
};
document.addEventListener("keydown", event => {
  if (event.target?.id !== "consoleInput") return;
  if (event.key === "Enter" && !event.shiftKey) { event.preventDefault(); consoleSend(); }
  else if (event.key.toLowerCase() === "c" && event.ctrlKey && window.__consoleSocket?.readyState === WebSocket.OPEN) {
    event.preventDefault(); window.__consoleSocket.send(JSON.stringify({ type: "input", data: "\u0003" }));
  }
});

/* ---------------- deploy ---------------- */
const deployDefaults = () => ({ name: "", image: "", icon: "", namespace: "lab", replicas: 1,
  cpu: "50m", memory: "128Mi", ports: [], env: {}, env_meta: [], volumes: [], hardware: [],
  template_devices: [], target_mode: "new", target_workload: "", network_mode: "loadbalancer",
  vip_mode: "shared", lb_ip: "" });
let DCFG = deployDefaults(), DOPT = { deployments: [], pvcs: [], storage_classes: [] }, DRENDERING = false;
async function viewDeploy(pre) {
  pre = pre || window.__deployPrefill;
  window.__deployPrefill = null;
  const [, liveNodes] = await Promise.all([loadHardwareFeatures(), api("/api/nodes").catch(() => [])]);
  if (liveNodes.length) STATE.data.nodes = liveNodes;
  DCFG = Object.assign(deployDefaults(), pre || {});
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
  resetPaint();
  paint(`<div class="phead"><div><h2>Deploy a container</h2>
      <p>Run an independent workload or add a sidecar container to an existing pod</p></div></div>
  <div class="split">
    <div class="card flat">
      <div class="f"><label>Deployment model ${tip("A new workload gets its own pod and lifecycle. Adding to an existing workload creates a sidecar container; Kubernetes restarts that workload's pods to apply it.")}</label>
        <select id="d_target_mode"><option value="new" ${DCFG.target_mode !== "existing" ? "selected" : ""}>New workload · independent pod</option>
          <option value="existing" ${DCFG.target_mode === "existing" ? "selected" : ""}>Add container to existing workload · shared pod</option></select></div>
      <div id="d_join_wrap" class="joinbox">
        <div class="f"><label>Existing workload</label><select id="d_target_workload"></select></div>
        <div id="d_join_note" class="note"></div>
      </div>
      <div class="f"><label>Name</label><input type="text" id="d_name" value="${esc(DCFG.name)}" placeholder="my-app"></div>
      <div class="f"><label>Docker image ${tip("The registry image and tag Kubernetes will pull, for example ghcr.io/home-assistant/home-assistant:stable")}</label><input type="text" id="d_image" value="${esc(DCFG.image)}" placeholder="nginx:alpine · ghcr.io/user/app:tag"></div>
      <div class="f"><label>Container logo ${tip("Optional public HTTPS image URL. Homestead validates and saves a private copy on its persistent volume, so the logo survives source outages and upgrades.")}</label><input type="url" id="d_icon" value="${esc(DCFG.icon || "")}" placeholder="https://…/icon.png"></div>
      <div class="f2">
        <div class="f"><label>Namespace</label><select id="d_ns">${nss.map(n => `<option ${n === DCFG.namespace ? "selected" : ""}>${esc(n)}</option>`).join("")}</select></div>
        <div class="f" id="d_rep_wrap"><label>Replicas</label><input type="number" id="d_rep" value="${DCFG.replicas}" min="0" max="5"></div>
      </div>
      <div class="f2">
        <div class="f"><label>CPU reserved ${tip("The scheduler guarantees this much CPU capacity. 1000m = one CPU core; 50m = 5% of one core. This is not a hard limit.")}</label><input type="text" id="d_cpu" value="${esc(DCFG.cpu)}" placeholder="50m"></div>
        <div class="f"><label>Memory reserved ${tip("The scheduler keeps this much RAM available for the container. Mi means mebibytes and Gi means gibibytes. This is not a hard limit.")}</label><input type="text" id="d_mem" value="${esc(DCFG.memory)}" placeholder="128Mi"></div>
      </div>
      <div class="sec">Hardware ${tip("Homestead adds the device path and schedules only onto nodes marked as having that hardware.")}</div>
      <div class="hwchoices">
        ${hardwareChoices("d_hw", (DCFG.hardware || []).concat(DCFG.gpu && !(DCFG.hardware || []).includes("igpu") ? ["igpu"] : []))}
      </div>
      ${(DCFG.template_devices || []).length ? `<div class="note import-device-note"><b>Imported device mappings:</b> ${(DCFG.template_devices || []).map(d => `<span class="mono">${esc(d.host_path || "?")} → ${esc(d.container_path || "?")}</span>`).join(", ")}. Matching hardware features were selected; review them before deploying.</div>` : ""}
      <div class="sec">Network ${tip("Kubernetes replaces Docker bridge networking with Services. Use a dedicated VIP for apps such as Pi-hole that need their own address or common ports.")}</div>
      <div class="f2"><div class="f"><label>Access mode</label><select id="d_net">
        <option value="loadbalancer" ${DCFG.network_mode === "loadbalancer" ? "selected" : ""}>LAN access (VIP)</option>
        <option value="internal" ${DCFG.network_mode === "internal" ? "selected" : ""}>Cluster only</option>
        <option value="host" ${DCFG.network_mode === "host" ? "selected" : ""}>Host network (advanced)</option></select></div>
        <div class="f"><label>VIP allocation</label><select id="d_vip_mode">
          <option value="shared" ${DCFG.vip_mode === "shared" ? "selected" : ""}>Shared Homestead VIP</option>
          <option value="auto" ${DCFG.vip_mode === "auto" ? "selected" : ""}>New automatic VIP</option>
          <option value="manual" ${DCFG.vip_mode === "manual" ? "selected" : ""}>Specific VIP</option></select></div></div>
      <div class="f" id="d_vip_wrap"><label>Specific VIP</label><input id="d_lb_ip" value="${esc(DCFG.lb_ip || "")}" placeholder="192.0.2.250"></div>
      <div class="note"><b>Docker bridge → Kubernetes Service.</b> Shared VIP reuses ${esc((STATE.data.ov && STATE.data.ov.lb_ip) || "the cluster VIP")} on a unique LAN port. New automatic VIP asks kube-vip IPAM for another address. Specific VIP is ideal for Pi-hole/DNS when port 53 must live on its own address. Host network binds directly on one node and reduces failover safety.</div>
      <div class="sec">Ports ${tip("Container port is where the process listens. LAN port is what clients use through the Kubernetes Service. TCP and UDP on the same number are separate listeners.")}</div><div id="d_ports"></div><button class="btn sm" onclick="addPort()">＋ add port</button>
      <div class="sec">Storage ${tip("The mount path is inside the container. Choose whether its backing storage is a new Longhorn claim, an existing claim, an existing volume in a shared pod, or a path on one host.")}</div>
      <div class="note storage-guide"><b>Choose deliberately:</b> RWO is best for one workload; RWX permits multi-node sharing; an existing PVC keeps its current data; a pod volume shares the exact backing volume with a sidecar. Host paths reduce failover portability.</div>
      <div id="d_vols"></div><button class="btn sm" onclick="addVol()">＋ add storage mapping</button>
      <div class="sec">Environment ${tip("Environment variables are passed directly to the container. App Store defaults are imported and remain editable.")}</div><div id="d_env"></div><button class="btn sm" onclick="addEnv()">＋ add variable</button>
      <div class="row" style="margin-top:24px">
        <button class="btn pri" onclick="doDeploy()">Deploy container</button>
        <button class="btn" onclick="previewYaml()">Preview manifest</button>
      </div>
    </div>
    <div class="card flat"><div class="ctitle">Configuration</div><div class="csub">Live summary</div>
      <div id="d_summary" style="margin-top:14px"></div>
      <button class="btn pri wide" style="margin-top:18px" onclick="doDeploy()">Deploy</button></div>
  </div>`);
  DRENDERING = true;
  renderDeployTargets(); renderPorts(); renderVols(); renderEnv(); applyDeployMode();
  DRENDERING = false; syncSummary();
  ["d_name", "d_image", "d_icon", "d_rep", "d_cpu", "d_mem", "d_net", "d_vip_mode", "d_lb_ip", "d_target_workload"].forEach(id => {
    const el = $("#" + id); if (!el) return;
    el.addEventListener("input", syncSummary); el.addEventListener("change", syncSummary);
  });
  $("#d_target_mode").addEventListener("change", () => { applyDeployMode(); syncSummary(); });
  $("#d_target_workload").addEventListener("change", () => { collect(); updateJoinNote(); renderVols(); syncSummary(); });
  $("#d_ns").addEventListener("change", refreshDeployOptions);
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
  $("#d_rep_wrap").style.display = joining ? "none" : "block";
  const host = [...$("#d_net").options].find(o => o.value === "host");
  if (host) host.disabled = joining;
  if (joining && $("#d_net").value === "host") $("#d_net").value = "internal";
  $$("#d_vols .deploy-volume").forEach(syncVolumeRow);
}
function collect() {
  DCFG.name = $("#d_name").value.trim(); DCFG.image = $("#d_image").value.trim();
  DCFG.namespace = $("#d_ns").value; DCFG.replicas = +$("#d_rep").value;
  DCFG.target_mode = $("#d_target_mode").value; DCFG.target_workload = $("#d_target_workload").value;
  DCFG.cpu = $("#d_cpu").value.trim(); DCFG.memory = $("#d_mem").value.trim(); DCFG.icon = $("#d_icon").value.trim();
  DCFG.hardware = selectedHardware("d_hw");
  DCFG.gpu = DCFG.hardware.includes("igpu"); DCFG.network_mode = $("#d_net").value;
  DCFG.vip_mode = $("#d_vip_mode").value; DCFG.lb_ip = $("#d_lb_ip").value.trim();
  DCFG.ports = $$("#d_ports .port-row").map(r => ({ container: +$(".pc", r).value,
    host: +$(".ph", r).value || +$(".pc", r).value, protocol: $(".pp", r).value, expose: $(".pe", r).checked }));
  DCFG.volumes = $$("#d_vols .deploy-volume").map(r => {
    const kind = $(".vk", r).value;
    return { path: $(".vp", r).value.trim(), source: $(".vs", r).value.trim(), kind,
      type: kind === "host" ? "host" : kind === "pod" ? "pod" : "pvc",
      create: kind === "new-rwo" || kind === "new-rwx", size_gb: +$(".vz", r).value || 5,
      storage_class: $(".vsc", r).value, access_mode: kind === "new-rwx" ? "ReadWriteMany" : "ReadWriteOnce",
      read_only: $(".vro", r).checked, label: r.dataset.label || "", description: r.dataset.description || "",
      required: r.dataset.required === "true", template_source: r.dataset.templateSource || "" };
  });
  DCFG.env = {}; $$("#d_env .env-row").forEach(r => { const k = $(".ek", r).value.trim(); if (k) DCFG.env[k] = $(".ev", r).value; });
  return DCFG;
}
function syncSummary() {
  const c = collect();
  const vipWrap = $("#d_vip_wrap"); if (vipWrap) vipWrap.style.display = c.network_mode === "loadbalancer" && c.vip_mode === "manual" ? "block" : "none";
  const row = (i, l, v) => `<div class="drow"><div class="di">${i}</div><div class="dl">${l}</div><div class="dv">${v}</div></div>`;
  $("#d_summary").innerHTML =
    row("◈", "Name", c.name ? `<b>${esc(c.name)}</b>` : '<span class="dim">—</span>') +
    row("❏", "Image", c.image ? `<span class="small mono">${esc(c.image)}</span>` : '<span class="dim">—</span>') +
    row("⌗", "Namespace", esc(c.namespace)) + row("⧉", c.target_mode === "existing" ? "Joins workload" : "Replicas", c.target_mode === "existing" ? esc(c.target_workload || "—") : c.replicas) +
    row("◴", "Requests", `<span class="small mono">${esc(c.cpu)} · ${esc(c.memory)}</span>`) +
    row("▤", "Hardware", c.hardware.length ? hardwareTags(c.hardware) : '<span class="dim">none</span>') +
    row("◎", "Network", `<span class="small">${esc(c.network_mode)}${c.network_mode === "loadbalancer" ? ` · ${esc(c.vip_mode)} VIP` : ""}</span>`) +
    row("⇄", "Ports", c.ports.length ? c.ports.map(p => `<span class="tag ${p.expose ? "info" : ""}">${p.host}→${p.container}</span>`).join("") : '<span class="dim">—</span>') +
    row("▥", "Storage", c.volumes.length ? c.volumes.map(v => `<span class="tag">${esc(v.source || "?")} · ${esc(v.kind)}</span>`).join("") : '<span class="dim">—</span>') +
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
function volumeKind(v) {
  if (v.kind) return v.kind;
  if (v.type === "host") return "host";
  if (v.type === "pod") return "pod";
  if (v.create === false) return "existing";
  return v.access_mode === "ReadWriteMany" ? "new-rwx" : "new-rwo";
}
function storageSourceList(row, kind) {
  let values = [];
  if (kind === "existing") values = DOPT.pvcs.map(v => ({ value: v.name,
    label: `${(v.access_modes || []).join("/") || "mode unknown"} · ${v.size || "size unknown"} · ${v.status}` }));
  if (kind === "pod") values = (selectedTarget()?.volumes || []).map(v => ({ value: v.name,
    label: `${v.kind}${v.source ? ` · ${v.source}` : ""}` }));
  let list = $(".vchoices", row);
  list.innerHTML = values.map(v => `<option value="${esc(v.value)}" label="${esc(v.label)}"></option>`).join("");
}
function syncVolumeRow(row) {
  const kind = $(".vk", row).value, joining = $("#d_target_mode")?.value === "existing";
  const podOption = [...$(".vk", row).options].find(o => o.value === "pod");
  if (podOption) podOption.disabled = !joining;
  if (kind === "pod" && !joining) $(".vk", row).value = "existing";
  const actual = $(".vk", row).value, isNew = actual.startsWith("new-");
  $(".vnew", row).style.display = isNew ? "grid" : "none";
  storageSourceList(row, actual);
  const source = $(".vs", row);
  source.placeholder = actual === "host" ? "/mnt/storage or /dev/…" : actual === "pod" ? "existing pod volume" : actual === "existing" ? "existing PVC name" : "new PVC name";
  const selectedPvc = actual === "existing" ? DOPT.pvcs.find(v => v.name === source.value) : null;
  const selectedPodVolume = actual === "pod" ? (selectedTarget()?.volumes || []).find(v => v.name === source.value) : null;
  $(".vhelp", row).textContent = actual === "new-rwo" ? "Creates a Longhorn claim for this workload (single-node attachment)."
    : actual === "new-rwx" ? "Creates shared Longhorn storage that can attach from multiple nodes."
    : actual === "existing" ? selectedPvc ? `${selectedPvc.name}: ${(selectedPvc.access_modes || []).join("/") || "mode unknown"}, ${selectedPvc.size}, ${selectedPvc.status}. The claim and data are kept.` : "Mounts an existing PVC without creating or deleting it."
    : actual === "pod" ? selectedPodVolume ? `${selectedPodVolume.name}: ${selectedPodVolume.kind}${selectedPodVolume.source ? ` (${selectedPodVolume.source})` : ""}. Shared with the sidecar.` : "Mounts a volume already defined on the selected workload into this sidecar."
    : "Mounts this exact host path; the container can only run where that path exists.";
}
function addVol(path = "", src = "", type = "pvc", meta = {}) {
  const v = typeof type === "object" ? type : { ...meta, path, source: src, type };
  const kind = volumeKind(v), d = document.createElement("div"); d.className = "deploy-volume";
  d.dataset.label = v.label || ""; d.dataset.description = v.description || "";
  d.dataset.required = String(!!v.required); d.dataset.templateSource = v.template_source || "";
  const listId = `vlist-${Date.now()}-${Math.random().toString(16).slice(2)}`;
  const classes = DOPT.storage_classes.length ? DOPT.storage_classes : ["longhorn-r2"];
  d.innerHTML = `<div class="volume-title"><div><b>${esc(v.label || "Storage mapping")}</b>${v.required ? ' <span class="pill warn">required</span>' : ""}</div>
      <button class="iconbtn row-remove" type="button" title="Remove storage mapping" onclick="this.closest('.deploy-volume').remove();syncSummary()">×</button></div>
    ${v.description ? `<div class="dim small volume-desc">${esc(v.description)}</div>` : ""}
    <div class="deploy-volume-grid">
      <div><label>Container mount path</label><input class="vp" type="text" value="${esc(v.path || "")}" placeholder="/config"></div>
      <div><label>Storage source</label><select class="vk">
        <option value="new-rwo" ${kind === "new-rwo" ? "selected" : ""}>New Longhorn volume · RWO</option>
        <option value="new-rwx" ${kind === "new-rwx" ? "selected" : ""}>New shared volume · RWX</option>
        <option value="existing" ${kind === "existing" ? "selected" : ""}>Existing PVC · keep data</option>
        <option value="pod" ${kind === "pod" ? "selected" : ""}>Existing volume in selected pod</option>
        <option value="host" ${kind === "host" ? "selected" : ""}>Host path · advanced</option></select></div>
      <div><label>Volume / path</label><input class="vs" type="text" list="${listId}" value="${esc(v.source || "")}"><datalist class="vchoices" id="${listId}"></datalist></div>
      <div class="vnew"><div><label>Size GiB</label><input class="vz" type="number" min="1" value="${v.size_gb || 5}"></div>
        <div><label>Storage class</label><select class="vsc">${classes.map(sc => `<option ${sc === (v.storage_class || "longhorn-r2") ? "selected" : ""}>${esc(sc)}</option>`).join("")}</select></div></div>
    </div>
    <div class="volume-foot"><span class="dim small vhelp"></span><label class="switch"><input class="vro" type="checkbox" ${v.read_only ? "checked" : ""}>Read-only</label></div>
    ${v.template_source && v.template_source !== v.source ? `<div class="template-source">Unraid source: <span class="mono">${esc(v.template_source)}</span> · choose its Kubernetes backing above</div>` : ""}`;
  $("#d_vols").appendChild(d); syncVolumeRow(d);
  d.addEventListener("input", event => { if (event.target.classList.contains("vs")) syncVolumeRow(d); syncSummary(); });
  $(".vk", d).addEventListener("change", () => { syncVolumeRow(d); syncSummary(); }); if (!DRENDERING) syncSummary();
}
function addEnv(k = "", v = "", meta = {}) {
  const d = document.createElement("div"); d.className = "f3 env-row";
  d.innerHTML = `<div><label>Key</label><input class="ek" type="text" value="${esc(k)}"></div>
    <div><label>${esc(meta.label || "Value")}${meta.required ? " · required" : ""}</label><input class="ev" type="${meta.masked ? "password" : "text"}" value="${esc(v)}">${meta.description ? `<span class="dim xs">${esc(meta.description)}</span>` : ""}</div>
    <button class="iconbtn row-remove" type="button" title="Remove variable" onclick="this.parentElement.remove();syncSummary()">×</button>`;
  $("#d_env").appendChild(d); d.addEventListener("input", syncSummary); if (!DRENDERING) syncSummary();
}
function renderPorts() { const before = DRENDERING; DRENDERING = true; const rows = [...(DCFG.ports || [])]; $("#d_ports").innerHTML = ""; rows.forEach(p => addPort(p.container, p.host, p.expose !== false, p.protocol || "TCP")); DRENDERING = before; }
function renderVols() { const before = DRENDERING; DRENDERING = true; const rows = [...(DCFG.volumes || [])]; $("#d_vols").innerHTML = ""; rows.forEach(v => addVol("", "", v)); DRENDERING = before; }
function renderEnv() { const before = DRENDERING; DRENDERING = true; const rows = Object.entries(DCFG.env || {}); $("#d_env").innerHTML = ""; rows.forEach(([k, v]) => addEnv(k, v, (DCFG.env_meta || []).find(m => m.key === k) || {})); DRENDERING = before; }
window.addPort = addPort; window.addVol = addVol; window.addEnv = addEnv;
window.previewYaml = async () => {
  try { const r = await api("/api/preview", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(collect()) });
    modal("Manifest preview", `<pre>${esc(JSON.stringify(r, null, 2))}</pre>`, true); } catch (e) { toast(e.message, "bad"); }
};
window.doDeploy = async () => {
  const c = collect();
  if (!c.name || !c.image) return toast("name and image are required", "bad");
  if (c.target_mode === "existing" && !c.target_workload) return toast("choose an existing workload", "bad");
  if (c.volumes.some(v => !v.path || !v.source)) return toast("every storage mapping needs a mount path and source", "bad");
  try {
    const plan = await api("/api/preview", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(c) });
    const joining = c.target_mode === "existing";
    modal(joining ? "Review shared-pod change" : "Review deployment", `<div class="update-review">
      <div class="reviewbox"><b>${joining ? `Add ${esc(c.name)} to ${esc(c.target_workload)}` : `Create ${esc(c.name)}`}</b>
        <p class="dim">${esc(plan.impact?.message || "Review the Kubernetes objects before continuing.")}</p>
        <div class="dependency-list"><div class="dependency-row"><span>Image</span><b class="mono">${esc(c.image)}</b></div>
          <div class="dependency-row"><span>Ports</span><b>${c.ports.length}</b></div><div class="dependency-row"><span>Storage mappings</span><b>${c.volumes.length}</b></div></div>
      </div>
      ${joining ? `<label class="switch dependency-confirm"><input type="checkbox" id="deployConfirm" onchange="document.getElementById('deployGo').disabled=!this.checked"> I understand every container in ${esc(c.target_workload)} will restart together</label>` : ""}
      <details><summary>Manifest preview</summary><pre>${esc(JSON.stringify({ deployment: plan.deployment, service: plan.service }, null, 2))}</pre></details>
      <div class="modalactions"><button class="btn" onclick="closeModal()">Cancel</button><button class="btn pri" id="deployGo" ${joining ? "disabled" : ""} onclick="confirmDeploy()">${joining ? "Add container & restart pod" : "Deploy workload"}</button></div></div>`, true);
  } catch (e) { toast(e.message, "bad"); }
};
window.confirmDeploy = async () => {
  const c = collect();
  try { $("#deployGo").disabled = true; await api("/api/deploy", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(c) });
    closeModal(); toast(c.target_mode === "existing" ? `${c.name} added to ${c.target_workload}` : `${c.name} deployed`, "ok"); go("workloads");
  } catch (e) { if ($("#deployGo")) $("#deployGo").disabled = false; toast(e.message, "bad"); }
};

/* ---------------- app store ---------------- */
async function viewStore() {
  resetPaint();
  paint(`<div class="phead">
      <div><h2>App Store</h2><p>Unraid Community Applications catalogue, deployed as Kubernetes workloads</p></div>
      <div class="row"><input class="search" id="s_q" placeholder="plex, nextcloud, jellyfin…" value="${esc(STATE.q)}" style="width:260px;padding-left:16px">
      <button class="btn pri" onclick="storeSearch()">Search</button></div></div>
    <div id="s_res"><div class="empty">Search the catalogue — it is fetched live from the Unraid CA feed.</div></div>`);
  $("#s_q").addEventListener("keydown", e => { if (e.key === "Enter") { e.preventDefault(); storeSearch(); } });
  if (STATE.q) storeSearch();
}
window.storeSearch = async () => {
  const q = $("#s_q").value.trim();
  $("#s_res").innerHTML = `<div class="empty"><span class="spin2"></span>searching catalogue…</div>`;
  try {
    const r = await api("/api/appstore?q=" + encodeURIComponent(q));
    STATE.data.apps = r.apps;
    $("#s_res").innerHTML = r.apps.length
      ? `<div class="dim small" style="margin-bottom:12px">${r.total} match${r.total === 1 ? "" : "es"} · showing ${r.apps.length}</div>
        <div class="apps stagger">${r.apps.map((a, i) => `<div class="card app">
          <div class="row" style="gap:11px">${a.icon ? `<img class="ico" src="${esc(a.icon)}" referrerpolicy="no-referrer" onerror="this.style.display='none'">` : ""}
            <div style="min-width:0"><div class="nm">${esc(a.name)}</div>
            ${a.cat ? `<span class="tag">${esc(a.cat.split(" ")[0])}</span>` : ""}</div></div>
          <div class="ds">${esc(a.desc || "No description provided.")}</div>
          <div class="rp">${esc(a.repo)}</div>
          <button class="btn pri wide" onclick="storeInstall(${i})">Configure &amp; deploy</button></div>`).join("")}</div>`
      : `<div class="empty">nothing matched “${esc(q)}”</div>`;
  } catch (e) { $("#s_res").innerHTML = `<div class="empty">${esc(e.message)}</div>`; }
};
window.storeInstall = i => {
  const a = STATE.data.apps[i];
  window.__deployPrefill = a.deploy || { name: a.name, image: a.repo, icon: a.icon || "" };
  go("deploy");
  toast(`"${a.name}" loaded — check storage paths before deploying`);
};
