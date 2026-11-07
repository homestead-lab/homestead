/* Homestead views: changing a volume's storage class. Split from views-storage.js (#911); a classic script. */
/* ---------------- changing a volume's storage class ----------------
   A review first - what uses it, what stops, how much room it takes while
   both copies exist - then the move as a tracked job with its steps. */
window.volumeReclass = async x => {
  if (RECLASS.busy) return;
  RECLASS.back();
  window.__reclassVolume = x;
  const classes = (STATE.data.storageClasses || []).filter(c => !c.internal && !c.made_for && c.name !== x.storage_class);
  if (!classes.length) return toast("There is no other storage class to move it to", "warn");
  const pick = classes.find(c => c.default) || classes[0];
  modal(`Change storage class · ${x.pvc_name || x.name}`, UI.lead("Copy this volume to another storage class, keeping its name. Apps and VMs using it stop during the move; the original data is kept afterward.") +
    UI.fields(UI.field("From", `<input value="${esc(x.storage_class || "unknown")}" disabled>`),
      UI.field("To", `<select id="rc_to" onchange="volumeReclassPlan(${jsq(x.namespace || "lab")},${jsq(x.pvc_name || x.name)})">${classes.map(c => `<option value="${esc(c.name)}" ${c.name === pick.name ? "selected" : ""}>${esc(c.name)}${c.replicas ? ` · ${esc(c.replicas)} copies` : ""}</option>`).join("")}</select>`)) +
    '<div id="rc_plan"></div>' +
    UI.ack("rc_ack", "Copy it despite the downtime and warnings", {onchange:"volumeReclassReady()"}) +
    UI.actions(UI.cancel() + UI.button("Move volume", `volumeReclassStart(${jsArg(x.namespace || "lab")},${jsArg(x.pvc_name || x.name)})`, {kind:"pri",id:"rc_go",disabled:true})), false, "operation-review");
  volumeReclassPlan(x.namespace || "lab", x.pvc_name || x.name);
};

/* Review, then change a volume's storage class (#909), on the shared review flow: it keeps the sequence, the
   busy flag and the one-shot send. This view keeps its words and the target it reviewed. */
let RECLASS_HOST = null;
const RECLASS = UI.reviewFlow({
  loading: ({ host, go }) => {
    host.innerHTML = '<div class="empty"><span class="spin2"></span> checking what uses it and where it fits</div>';
    if (go) go.disabled = true;
    if ($("#rc_ack")) $("#rc_ack").checked = false;
  },
  live: ({ host }) => $("#rc_plan") === host,
  preview: async ({ config }) => {
    const p = await api("/api/volumes/reclass/plan", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(config) });
    if (!p.capacity_token || !Array.isArray(p.blockers) || !Array.isArray(p.warnings) || !Array.isArray(p.consumers)) throw new Error("Review is incomplete; nothing can start.");
    return { config, plan: p };
  },
  render: (review, { host }) => {
    const p = review.plan;
  const sp = p.space || {};
  const verb = c => c.kind === "VirtualMachine" ? (c.running ? "shut down, then started again" : "stopped already; stays stopped")
    : c.kind === "CronJob" ? "paused, then resumed" : c.running ? "stopped, then started again" : "stopped already; stays stopped";
  const messages = [...p.blockers, ...p.warnings];
  // Only which disks hold the copies differs: they can move with it in use.
  const live = p.live && window.__reclassVolume ? UI.callout("info", "This can move with nothing stopped",
    `<p>${esc(p.to_class)} differs from ${esc(p.from_class)} only in which disks hold the copies (${esc([...p.live.disk_tags, ...p.live.node_tags].join(", "))}). Its copies can move there while it stays in use; the volume keeps the class name ${esc(p.from_class)}.</p>
     <div class="row">${UI.button("Move copies instead", `volumeCopyMove(window.__reclassVolume,${JSON.stringify(p.live)})`, { kind: "pri", attrs: 'data-need="operator"' })}</div>`) : "";
  host.innerHTML = live + (messages.length ? UI.callout(p.blockers.length ? "bad" : "warn", p.blockers.length ? "This move cannot start" : "Before moving", `<ul>${messages.map(m=>`<li>${esc(m)}</li>`).join("")}</ul>`) : "") +
    UI.section("Affected apps and VMs", UI.table([{label:"App or VM"},{label:"Effect"}], p.consumers.map(c=>[`${esc(c.name)} · ${esc(c.kind)}`,esc(verb(c))]), {empty:"No apps and VMs currently reference this volume."})) +
    UI.section("Storage needed", UI.facts([["New volume",esc(sizeText(sp.size_gb))],["Replica allocation",`${esc(sizeText(sp.allocated_gb))} · ${esc(sp.replicas)} copies`],["Estimated destination room",sp.room_gb == null ? "Unknown" : esc(sizeText(sp.room_gb))]])) +
    UI.more("What is checked", "The volume, backing disk, destination class and affected apps and VMs are checked again before starting. Missing inventory blocks the move. This is not a storage or scheduler reservation; later recovery and host-loss validation remain separate safeguards. Both copies use space until you remove the original.");
    RECLASS.ready();
    if (window.applyRole) applyRole();
  },
  failed: (e, { host }) => { host.innerHTML = UI.callout("bad", "Review unavailable", esc(e.message)); },
  // Only the plan reviewed can start: it must allow it, the target must still be the one it was made for.
  blocked: review => !review.plan.ok || !!review.plan.blockers.length, ack: "rc_ack", button: "rc_go",
  check: review => $("#rc_to")?.value === review.config.target,
  apply: async review => {
    const host = RECLASS_HOST = $("#rc_plan"), go = $("#rc_go");
    go.disabled = true; $("#rc_to").disabled = true;
    try {
      const r = await api("/api/volumes/reclass/start", { method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({...review.config,capacity_token:review.plan.capacity_token,confirm_capacity:true}) });
      if (!r.operation?.id) throw new Error("Move receipt was not returned");
      if (typeof noteOperation === "function") noteOperation(r.operation);
      if ($("#rc_plan") === host) reclassWatch(r.operation.id);
    } catch (e) {
      if ($("#rc_plan") === host) host.innerHTML = UI.callout("bad", "Start not confirmed", esc(e.message) + " Check Recent jobs before reviewing again. Nothing was retried.");
      else toast("Move start not confirmed; check Recent jobs.", "bad");
    }
  },
  after: () => {
    if ($("#rc_plan") === RECLASS_HOST && $("#rc_to")) $("#rc_to").disabled = false;
    if (typeof refreshOperations === "function") refreshOperations();
  },
});
window.volumeReclassPlan = async (ns, claim) => {
  if (RECLASS.busy) return;
  RECLASS.back();
  const host = $("#rc_plan"), go = $("#rc_go");
  if (!host) return;
  return RECLASS.open({ host, go, config: { namespace: ns, claim, target: $("#rc_to").value } });
};
window.volumeReclassReady = () => RECLASS.ready();
window.volumeReclassStart = async (ns, claim) => {
  if (!volumeReclassReady() || RECLASS.review.config.namespace !== ns || RECLASS.review.config.claim !== claim) return;
  await RECLASS.apply();
};

/* The move as it runs: its steps, the copy's progress, and at the end the
   old copy to remove when you are happy. */
window.reclassWatch = async id => {
  const watchToken = {}; window.__reclassWatchToken = watchToken;
  const paint = op => {
    const steps = op.steps || [];
    const copy = op.copy || {};
    $("#mbody").innerHTML = `<div class="rc-steps">${steps.map(s => `<div class="rc-step ${s.state}"><span>${s.state === "done" ? "✓" : s.state === "active" ? '<span class="spin2"></span>' : "•"}</span>${esc(s.label)}
        ${s.state === "active" && (s.id === "copy" || s.id === "verify") ? `<div class="rc-copy">${meter(s.id === "verify" ? 100 : copy.percent || 0, "", "cpu")}
          <span class="mono xs">${s.id === "verify" ? "comparing with the original" : copy.unavailable ? "waiting for reported progress" : `${copy.percent || 0}%${copy.speed ? ` · ${esc(copy.speed)}` : ""}`}</span></div>` : ""}</div>`).join("")}</div>
      ${UI.callout(op.status === "failed" ? "bad" : op.status === "succeeded" ? "good" : "", "", `${esc(op.message || "")}`, { attrs: 'style="margin-top:12px"' })}
      ${op.storage_recovery ? `<div class="row"><button class="btn pri" data-need="admin" onclick="storageRecoveryReview(${jsq(op.id)})">Review storage move</button></div>` : ""}
      ${op.status === "failed" && op.resumable ? `<div class="row" style="margin-top:12px"><button class="btn pri" data-need="admin" onclick="resumeOperation(${jsq(op.id)})">Carry on from this step</button></div>` : ""}
      ${op.status === "succeeded" && op.old_pv ? `<div class="row" style="margin-top:12px"><button class="btn danger" data-need="admin" onclick="reclassRemoveOld(${jsq(op.old_pv)})">Remove the old copy</button>
        <span class="dim xs">Keep it until the app is working on the new one.</span></div>` : ""}
      ${UI.actions(`<button data-dialog-dismiss="true" class="btn" onclick="closeModal()">${op.status === "running" ? "Keep going in the background" : "Close"}</button>`)}`;
    if (window.applyRole) applyRole();
  };
  modal("Changing storage class", '<div class="empty"><span class="spin2"></span></div>', true);
  window.__reclassTimer?.stop?.();
  const tick = async signal => {
    if ($("#modal").classList.contains("hidden")) return false;
    const ops = await api("/api/operations", { keep: true, signal }).catch(e => ({ unread: e.message }));
    if (UI.pollCancelled(signal)) return false;
    if (window.__reclassWatchToken !== watchToken || $("#modal").classList.contains("hidden")) return false;
    if (ops.unread) {
      // A failed poll keeps what was last shown and says so; the next tick tries again (#620).
      const note = `<div id="rc_unread" class="dim small" style="margin-top:8px">Could not read the job: ${esc(ops.unread)} · still trying</div>`;
      if (!$("#mbody .rc-steps")) {
        // Nothing shown yet: the note, and a way out of the dialog.
        $("#mbody").innerHTML = `${note}${UI.actions(`<button data-dialog-dismiss="true" class="btn" onclick="closeModal()">Close</button>`)}`;
        return;
      }
      $("#rc_unread")?.remove();
      $("#mbody").insertAdjacentHTML("beforeend", note);
      return;
    }
    $("#rc_unread")?.remove();
    const op = ops.find(o => o.id === id);
    if (!op) return;
    $("#mtitle").textContent = op.title;
    paint(op);
    if (op.status !== "running") { if (STATE.view === "storage") refresh(true); return false; }
  };
  window.__reclassTimer = UI.poll(tick, 2500);
};

window.reclassRemoveOld = async pv => {
  if (!(await ask(`Remove the old copy ${pv}? Its data is deleted.`, { title: "Remove old copy", ok: "Remove old copy", danger: true }))) return;
  try {
    const r = await api("/api/volumes/old-copies/remove", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ pv }) });
    toast(r.detail, "ok");
    if ($("#modal") && !$("#modal").classList.contains("hidden") && $("#mtitle").textContent.startsWith("Move")) closeModal();
    if (STATE.view === "storage") refresh(true);
  } catch (e) { toast(e.message, "bad"); }
};

// Node only (tests): the same names, for require(). The browser has no `module`.
if (typeof module === "object" && module.exports) module.exports = {
  RECLASS_HOST, RECLASS
};
