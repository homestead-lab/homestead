/* Start a StatefulSet's stuck pod on another host (#441, set_restart.py).
   Opened from "If this host goes down" for a set that waits on a host that
   has stopped answering. The review is a fresh read of the cluster; the run
   sends its token back, and a changed review asks to be read again. */
let SET_RESTART_PLAN = null;
let SET_RESTART_FOLLOW = 0;
const setRestartAgo = seconds => {
  const minutes = Math.max(0, Math.round(Number(seconds) / 60));
  return minutes >= 120 ? `${Math.round(minutes / 60)} h` : `${minutes} min`;
};
const setRestartClock = iso => {
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? String(iso || "unknown") : d.toISOString().slice(11, 16) + " UTC";
};

function setRestartBody(plan, node) {
  const blockers = plan.blockers || [];
  if (!plan.ready || blockers.length) {
    return UI.lead(esc(`Homestead did not start anything on ${node}, because it could not show that this is safe.`))
      + UI.callout("bad", "Blocked", `<ul class="ui-list">${(blockers.length ? blockers : ["The check could not be completed."]).map(b => `<li>${esc(b)}</li>`).join("")}</ul>`)
      + UI.actions(UI.cancel("Close"));
  }
  const set = plan.set || {}, pods = plan.pods || [], volumes = plan.volumes || [];
  const facts = UI.facts([
    ["Not ready since", `${esc(setRestartClock(plan.down_since))} (${esc(setRestartAgo(plan.down_seconds))})`],
    ["Last heard from", esc(plan.last_heard ? setRestartClock(plan.last_heard) : "not known")],
    ["Longhorn", (plan.relied_on || []).length ? "sees the host as down" : "sees the host as down, running none of these volumes"],
    ...volumes.map(v => [`Volume ${v.pvc}`, `the current copy is on ${esc(v.copy_on)}`]),
    ...(plan.relied_on || []).map(r => ["Relied on", esc(r)]),
  ]);
  const podList = UI.table([{ label: "Pod to remove" }, { label: "State" }], pods.map(p => [`<span class="mono">${esc(p.name)}</span>`,
    p.terminating ? "being deleted; made final" : "stuck on the host"]));
  return `<div class="ui-stack">
    ${UI.lead(esc(`${set.ns}/${set.name} has a pod on ${node}, which has stopped answering.`))}
    ${set.wait_mode ? UI.callout("warn", "This app is set to wait for its host to come back; this overrides that once") : ""}
    ${UI.section("What Homestead checked", facts)}
    ${UI.section("What will happen", `<ul class="ui-list"><li>The stuck pod is removed; the StatefulSet starts it again on another host.</li>
      <li>Its volume is reattached there. That can take about 6 minutes; a slow start is not a failure.</li></ul>`)}
    ${UI.section("Pods", podList)}
    <p class="ui-help">Homestead cannot tell a host that is off from one only cut off from the network.</p>
    ${UI.typedConfirm({ id: "sr_confirm", expect: set.name, button: "sr_run" })}
    ${UI.actions(UI.button("Start on another host", `setRestartRun(${jsArg(set.ns)},${jsArg(set.name)},${jsArg(node)})`,
      { kind: "danger", id: "sr_run", disabled: true, attrs: 'data-need="admin"' }))}</div>`;
}

window.setRestartReview = async (ns, name, node) => {
  SET_RESTART_PLAN = null;
  let plan;
  try { plan = await api(`/api/statefulset/restart-elsewhere/plan?${new URLSearchParams({ ns, name, node })}`); }
  catch (e) { return toast(`Could not check this: ${e.message}`, "bad"); }
  if (plan.ready && (!plan.review_token || !plan.set)) return toast("The review is incomplete; try again", "bad");
  if (plan.ready) SET_RESTART_PLAN = { ...plan, set: { ...plan.set, ns, name } };
  modal(`Start on another host · ${name}`, setRestartBody(plan.ready ? SET_RESTART_PLAN : plan, node), true);
  window.applyRole?.();
};

window.setRestartRun = async (ns, name, node) => {
  const plan = SET_RESTART_PLAN;
  if (!plan || plan.set.ns !== ns || plan.set.name !== name) return toast("Review it again first", "bad");
  if (!typedConfirmMatches("sr_confirm")) return;
  const confirm = $("#sr_confirm").value.trim(), button = $("#sr_run");
  SET_RESTART_PLAN = null;   // one approval, one send
  if (button) { button.disabled = true; button.textContent = "Starting…"; }
  try {
    const r = await api("/api/statefulset/restart-elsewhere", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ ns, name, node, confirm, review_token: plan.review_token }) });
    window.noteOperation?.(r);
    const left = (r.not_deleted || []).map(p => `${p.name} (${p.error})`);
    if (left.length) toast(`Not removed: ${left.join(", ")}. The rest were.`, "warn");
    return window.setRestartFollow(r.id, name);
  } catch (e) {
    // A job already going for this set (or one following a restart): follow it.
    // The reply names the job by id only; Recent jobs fills in the rest.
    const left = (e.body?.not_deleted || []).map(p => `${p.name} (${p.error})`);
    if (e.body?.operation?.id) {
      if (left.length) toast(`Not removed: ${left.join(", ")}.`, "warn");
      return window.setRestartFollow(e.body.operation.id, name);
    }
    toast(e.message + (left.length ? ` Not removed: ${left.join(", ")}.` : "") + " Review it again before trying once more.", "bad");
    if (button) {
      button.disabled = false; button.textContent = "Review again";
      button.onclick = () => window.setRestartReview(ns, name, node);
      window.applyRole?.();
    }
  } finally { window.refreshOperations?.(true); }
};

/* The job as it goes: its progress and message from Recent jobs, until it ends. */
window.setRestartFollow = async (id, name) => {
  const sequence = ++SET_RESTART_FOLLOW;
  modal(`Starting elsewhere · ${name}`, '<div id="srProgress"><div class="empty"><span class="spin2"></span> Starting…</div></div>', true);
  let last = null;
  while (sequence === SET_RESTART_FOLLOW && $("#srProgress")) {
    let offline = false;
    try { last = (await api("/api/operations", { keep: true, timeout: 10000 })).find(o => o.id === id) || last; }
    catch { offline = true; }
    if (!$("#srProgress") || sequence !== SET_RESTART_FOLLOW) return;
    const done = last?.status === "succeeded", stopped = last?.status === "failed" || last?.status === "cancelled";
    if (last) $("#srProgress").innerHTML = UI.progress(done ? 100 : last.progress, { label: done ? "Finished" : stopped ? "Stopped" : "In progress", kind: done ? "ok" : stopped ? "bad" : "info" })
      + (last.message ? UI.lead(esc(last.message)) : "")
      + (offline ? UI.callout("warn", "Reconnecting", "Homestead cannot be reached right now; this view carries on by itself.") : "")
      + UI.actions(UI.cancel("Close"));
    else if (!offline) { $("#srProgress").innerHTML = UI.callout("warn", "Job not found", "It may have been cleared. Check Recent jobs.") + UI.actions(UI.cancel("Close")); return; }
    if (done || stopped) { window.refreshOperations?.(true); return; }
    await new Promise(resolve => setTimeout(resolve, 2000));
  }
};
