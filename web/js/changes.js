/* Change history (homestead_changes.py): who changed an app, when, and what,
   with Undo - the settings from before a change, reviewed like any rollout.

   wlHistory(ns, name) is one app's; wlHistory() every app's, newest first. A
   change made outside Homestead (kubectl, Helm, GitOps) says so; a value that
   may be secret only says that it changed. */
const HISTORY = { ns: "", name: "" };

function historyWhen(at) {
  const d = new Date(at * 1000), today = new Date().toDateString() === d.toDateString();
  return today ? d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })
    : d.toLocaleString([], { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" });
}

const historyWho = e => e.source === "outside" ? '<span class="tag warn" data-tip="kubectl, Helm, GitOps or another tool changed it, not Homestead">outside Homestead</span>'
  : `<span class="dim small">${esc(e.by ? e.by.replace(/^api-key:/, "API key ") : "Homestead")}</span>`;

const historyValue = v => v === null || v === undefined ? '<span class="dim">—</span>' : `<span class="mono">${esc(v)}</span>`;

function historyEntry(e, all) {
  return `<div class="history-entry">
    <div class="history-head"><b>${esc(historyWhen(e.at))}</b>${all ? ` <span class="history-app">${esc(e.name)}</span><span class="dim xs">${esc(e.namespace)}</span>` : ""}
      ${historyWho(e)}
      ${e.can_undo ? `<span class="history-acts">${UI.button("Undo", `historyUndo(${jsArg(e.namespace)},${jsArg(e.name)},${jsArg(e.id)})`, { attrs: 'data-need="operator"' })}</span>` : ""}</div>
    <div class="history-rows">${e.changes.map(r => `<div class="history-row"><span class="history-field">${esc(r.field)}</span>
      <span class="history-values">${historyValue(r.before)}<span class="dim" aria-label="became"> → </span>${historyValue(r.after)}</span></div>`).join("")}</div></div>`;
}

window.wlHistory = async (ns = "", name = "") => {
  Object.assign(HISTORY, { ns, name });
  modal(name ? `History · ${name}` : "Change history", '<div class="empty"><span class="spin2"></span> Loading…</div>');
  let d;
  try { d = await api(`/api/changes${name ? `?ns=${encodeURIComponent(ns)}&name=${encodeURIComponent(name)}` : ""}`); }
  catch (e) { $("#mbody").innerHTML = UI.callout("bad", "The history could not be read", esc(e.message)) + UI.actions(UI.cancel("Close")); return; }
  const entries = d.entries || [];
  $("#mbody").innerHTML = `<div class="ui-stack">
    ${UI.lead(esc(name ? `Every change to ${name}'s images, environment, resources, ports, volumes, copies and placement, newest first. Undo puts the settings from before a change back, after a review.`
      : "Every change to an app's images, environment, resources, ports, volumes, copies and placement, newest first."))}
    ${entries.length ? `<div class="history-list">${entries.map(e => historyEntry(e, !name)).join("")}</div>`
      : '<div class="empty small">No changes yet. Homestead records them from when it first sees each app.</div>'}
    ${UI.actions(UI.cancel("Close"))}</div>`;
};

window.historyUndo = async (ns, name, id) => {
  const back = () => wlHistory(HISTORY.ns, HISTORY.name);
  modal(`Undo · ${name}`, '<div class="empty"><span class="spin2"></span> Reviewing…</div>');
  let p;
  try { p = await api("/api/changes/undo/preview", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ ns, name, id }) }); }
  catch (e) { $("#mbody").innerHTML = UI.callout("bad", "It cannot be undone", esc(e.message)) + UI.actions(UI.button("Back", "historyBack()")); window.__historyBack = back; return; }
  window.__historyBack = back;
  window.__historyUndo = { ns, name, id, capacity_token: p.capacity_token };
  const cap = p.capacity || {};
  $("#mbody").innerHTML = `<div class="ui-stack">
    ${UI.lead(esc(`${name} goes back to its settings from before that change. Its pods are replaced, as for any change they run with.`))}
    <div class="history-rows">${p.changes.map(r => `<div class="history-row"><span class="history-field">${esc(r.field)}</span>
      <span class="history-values">${historyValue(r.before)}<span class="dim" aria-label="becomes"> → </span>${historyValue(r.after)}</span></div>`).join("")}</div>
    ${(cap.warnings || []).map(w => UI.callout("warn", "", esc(w))).join("")}
    ${cap.blocked ? UI.callout("bad", "There is not room for it", esc(cap.reason || "The cluster cannot place these pods now.")) : ""}
    ${UI.actions(UI.button("Back", "historyBack()") + (cap.blocked ? "" : UI.button("Undo the change", "historyUndoApply()", { kind: "danger", id: "hu_go", attrs: 'data-need="operator"' })))}</div>`;
};

window.historyBack = () => (window.__historyBack || closeModal)();

window.historyUndoApply = async () => {
  const go = $("#hu_go"), body = { ...window.__historyUndo, confirm_capacity: true };
  if (go) go.disabled = true;
  try {
    const r = await api("/api/changes/undo", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
    toast(r.detail, "ok");
    closeModal();
    if (typeof refreshAfterRollout === "function") refreshAfterRollout();
  } catch (e) { toast(e.message, "bad"); if (go) go.disabled = false; }
};
