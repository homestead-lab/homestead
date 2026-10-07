/* Whether an app updates itself (homestead_autoupdate.py): inside the cluster's
   maintenance window, a newer build of the same tag only, with its volumes
   snapshotted first and the image rolled back if it stops answering.

   wlUpdateMode is the app's own setting; a card and a row carry a small Auto
   tag while it is on, and the dialog shows the last automatic update. */
const DAY_NAMES = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];

function maintenanceWords(policy) {
  const m = policy?.maintenance;
  if (!m) return "the maintenance window";
  const days = (m.days || []).length === 7 ? "every day" : (m.days || []).map(d => DAY_NAMES[d]).join(", ");
  const hours = Math.round((m.duration_minutes || 0) / 6) / 10;
  return `the maintenance window, ${days} from ${m.start} UTC for ${hours} hour${hours === 1 ? "" : "s"}`;
}

const autoUpdateTag = w => w.update_mode === "auto"
  ? `<span class="tag info" data-tip="Updates itself in the maintenance window: a newer build of the same tag, snapshotted first, rolled back if it stops answering">Auto</span>` : "";

function lastAutoUpdate(w) {
  const ops = (STATE.data.operations || []).filter(o => o.kind === "auto-update" && o.auto_update?.namespace === w.ns && o.auto_update?.name === w.name);
  return ops.sort((a, b) => String(b.started_at).localeCompare(String(a.started_at)))[0] || null;
}

window.wlUpdateMode = (ns, name) => {
  const w = (STATE.data.wl || []).find(x => x.ns === ns && x.name === name) || { ns, name };
  const policy = STATE.data.imageUpdates?.policy;
  const last = lastAutoUpdate(w);
  const blocked = policy?.policy === "notify_only";
  const option = (value, label, help) => `<label class="answer-mode"><input type="radio" name="um_mode" value="${value}" ${(w.update_mode || "manual") === value ? "checked" : ""}>
    <span><b>${esc(label)}</b><span class="dim small">${esc(help)}</span></span></label>`;
  modal(`Updates · ${name}`, `<div class="ui-stack">
    ${UI.lead(esc(`How ${name} gets new images. Either way, Containers shows when one is waiting.`))}
    <div class="answer-modes">
      ${option("manual", "When I choose", "You review each update and install it with Update.")}
      ${option("auto", "Automatically", `In ${maintenanceWords(policy)}: a newer build of the tag it follows, never a new version. Its Longhorn volumes are snapshotted first, and if it stops answering afterwards the old image comes back and you are told.`)}
    </div>
    ${blocked ? UI.callout("warn", "The cluster policy is notify only", "Nothing updates automatically until an admin changes it under Settings › Updates.") : ""}
    ${last ? `<div class="dim small">Last automatic update: ${esc(last.status === "succeeded" ? "worked" : last.status === "failed" ? "rolled back or stopped" : "under way")} · ${esc(last.message || "")}</div>` : ""}
    ${UI.actions(UI.cancel() + UI.button("Save", "updateModeSave()", { kind: "pri", id: "um_go", attrs: `data-need="operator" data-ns="${esc(ns)}" data-name="${esc(name)}"` }))}</div>`);
};

window.updateModeSave = async () => {
  const go = $("#um_go"), mode = $("#mbody input[name=um_mode]:checked")?.value || "manual";
  go.disabled = true;
  try {
    const result = await api("/api/image-updates/mode", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ ns: go.dataset.ns, name: go.dataset.name, mode }) });
    toast(result.detail, "ok");
    const w = (STATE.data.wl || []).find(x => x.ns === go.dataset.ns && x.name === go.dataset.name);
    if (w) w.update_mode = result.mode;
    closeModal();
    if (STATE.view === "workloads") renderWorkloads();
  } catch (e) { toast(e.message, "bad"); go.disabled = false; }
};
