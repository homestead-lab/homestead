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
  if (!items.length) {
    tray.classList.add("hidden");
    return;
  }
  tray.classList.remove("hidden");
  tray.classList.toggle("open", operationPanelOpen);
  const latest = active[0] || items[0];
  $("#jobSummary").innerHTML = `<span class="jobpulse ${active.length ? "" : "idle"}"></span>
    <span class="jobsummarycopy"><b>${active.length ? `${active.length} active job${active.length === 1 ? "" : "s"}` : "Recent jobs"}</b>
    <small>${esc(latest.title)} · ${esc(latest.message || latest.status)}</small></span>
    ${active.length ? `<span class="jobsummarypct">${Math.round(latest.progress || 0)}%</span>` : ""}
    <span class="jobchev" aria-hidden="true">⌃</span>`;
  $("#jobSummary").setAttribute("aria-expanded", String(operationPanelOpen));
  // Clearing one at a time is fine for a stray failure and tedious after a
  // batch, so the header offers the lot - and says how many, because it will
  // not touch anything still running.
  const finished = items.filter(item => !operationActive(item));
  const clear = $("#jobClear");
  if (clear) {
    // Only relabel when there is something to clear, so it never reads
    // "Clear 0 finished" in the moment between clearing and hiding.
    clear.hidden = !finished.length;
    if (finished.length) clear.textContent = `Clear ${finished.length} finished`;
  }
  $("#jobList").innerHTML = items.slice(0, 12).map(operation => `<article class="jobitem">
    <div class="jobitemtop"><div><b>${esc(operation.title)}</b>
      <span>${esc(operation.resource?.namespace ? operation.resource.namespace + " · " : "")}${esc(operation.resource?.kind || operation.kind)}</span></div>
      <span class="pill ${operationTone(operation.status)}">${esc(operation.status)}</span></div>
    <div class="jobmeter"><span class="${operation.status === "failed" ? "failed" : ""}" style="width:${Math.max(2, Math.min(100, operation.progress || 0))}%"></span></div>
    <div class="jobfoot"><span>${esc(operation.message || "")}</span><span>${operationAge(operation.finished_at || operation.started_at)}</span></div>
    <div class="jobactions">
      <button class="btn sm" onclick="openOperation('${esc(operation.href || "/")}')">Open</button>
      ${operationActive(operation) ? "" : `<button class="btn sm" data-need="operator" onclick="dismissOperation('${esc(operation.id)}')">Dismiss</button>`}
    </div>
  </article>`).join("");
  if (window.applyRole) window.applyRole();
}

async function refreshOperations(immediate = false) {
  clearTimeout(operationTimer);
  if (!ME) return;
  try {
    STATE.data.operations = await api("/api/operations");
    renderOperations();
  } catch (_) { /* retain the last known state during API interruptions */ }
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
window.openOperation = href => {
  const url = new URL(href || "/", window.location.origin);
  const route = HomesteadRouter.resolve(url.pathname);
  operationPanelOpen = false;
  go(route.view, { params: Object.fromEntries(url.searchParams) });
  renderOperations();
};
window.dismissFinishedOperations = async () => {
  try {
    const result = await api("/api/operations/dismiss", { method: "POST",
      headers: { "Content-Type": "application/json" }, body: JSON.stringify({ all: true }) });
    toast(result.detail || "finished jobs cleared", "ok");
  } catch (e) { toast(e.message, "bad"); }
  refreshOperations(true);
};

window.dismissOperation = async id => {
  try {
    await api("/api/operations/dismiss", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ id }) });
    STATE.data.operations = (STATE.data.operations || []).filter(item => item.id !== id);
    renderOperations();
  } catch (error) { toast(error.message, "bad"); }
};

$("#jobSummary").onclick = toggleOperations;
