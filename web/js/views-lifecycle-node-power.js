/* Homestead views: a host's actions: cordon, drain, promote and power. Split from views-lifecycle.js (#911); a classic script. */
/* ---------------- node power ---------------- */
window.nodeActions = async name => {
  let qr = {}, impact = { workloads: [], stranded: [] };
  try { [qr, impact] = await Promise.all([api("/api/quorum"), api(`/api/node/impact?node=${encodeURIComponent(name)}`)]); }
  catch (e) {
    return childModal("Host actions unavailable", `${UI.callout("bad", "", `The host-impact checks could not be completed: ${esc(e.message)}. No power action is available until they work.`)}
      <button class="btn" onclick="closeModal()">Close</button>`);
  }
  window.__nodeImpact = impact;
  const declared = await api(`/api/node/maintenance?node=${encodeURIComponent(name)}`).then(r => r.declared).catch(() => undefined);
  const isEtcd = (qr.members || []).includes(name);
  const off = qr.power_enabled === false;
  const host = ((typeof STATE !== "undefined" && STATE.data.nodes) || []).find(n => n.name === name) || {};
  const cordoned = host.schedulable === false;
  const platform = (typeof STATE !== "undefined" && STATE.platform) || {};
  // An app host on k3s with etcd can be made a server in place (#364).
  const promotable = platform.distribution === "k3s" && !platform.harvester && (qr.members || []).length > 0
    && !(host.roles || []).some(r => ["control-plane", "etcd", "master"].includes(r));
  const workloads = impact.workloads || [], stranded = impact.stranded || [];
  const action = (title, detail, buttonHtml, tone = "neutral") =>
    `<div class="ui-insight-row">${UI.statusDot(tone)}<div><b>${esc(title)}</b><small>${esc(detail)}</small></div><div class="ui-insight-action">${buttonHtml}</div></div>`;
  const actions = `<div class="ui-insight-list">
    ${cordoned
      ? action("Cordoned", `${declared ? "Uncordoning doesn't end its maintenance. " : ""}New pods do not land here. Allow scheduling again when the host is ready for work.`, UI.button("Uncordon", `nodeCordon(${jsArg(name)},false)`, { attrs: 'data-need="admin"' }), "warn")
      : action("Cordon", "Stop new pods starting here; what runs here keeps running.", UI.button("Cordon", `nodeCordon(${jsArg(name)},true)`, { attrs: 'data-need="admin"' }))}
    ${action("If this host goes down", "What stops, what waits, what moves and where, which volumes are at risk and which addresses move.",
      UI.button("Preview", `nodeIfDown(${jsArg(name)},() => nodeActions(${jsArg(name)}))`))}
    ${action("Move apps off", "Move every app on this host to another, cordoning it first.", UI.button("Move all off", `evacuateNode(${jsArg(name)})`, { attrs: 'data-need="admin"' }))}
    ${action("Drain", "Evict the pods here so they start elsewhere, through their disruption budgets.", UI.button("Drain", `nodeDrain(${jsArg(name)})`, { attrs: 'data-need="admin"' }))}
    ${off ? action("Power control is off", "Reboot and shutdown need ENABLE_NODE_POWER=true on the Homestead Deployment.", "", "neutral")
      : action("Reboot or shut down", (qr.total === 1 && isEtcd ? "The only host: a planned whole-cluster outage. " : "")
          + "Reviewed first: what moves, what waits for the host, and its volume copies.",
        UI.button("Reboot…", `nodePowerReview(${jsArg(name)},'reboot')`, { attrs: 'data-need="admin"' })
        + UI.button("Shut down…", `nodePowerReview(${jsArg(name)},'poweroff')`, { kind: "danger", attrs: 'data-need="admin"' }))}
    ${window.hostMaintenanceRow?.(name, declared) || ""}
    ${promotable ? action("Make this a server", "Gives the cluster another etcd vote. Its apps move off while k3s reinstalls; its storage stays.",
      UI.button("Make server…", `nodePromoteReview(${jsArg(name)})`, { attrs: 'data-need="admin"' })) : ""}
    ${action("Remove from cluster", "Take this host out for good, after checking quorum and volume copies.",
      UI.button("Remove…", `nodeRemoval(${jsArg(name)})`, { kind: "danger", attrs: 'data-need="admin"' }), "bad")}
  </div>`;
  const apps = typeof hostAppsHtml === "function" ? hostAppsHtml(impact) : workloads.length
    ? (stranded.length ? UI.callout("warn", `${stranded.length} can only run here`, `${stranded.map(w => `<span class="mono">${esc(w.name)}</span>`).join(", ")} ${stranded.length === 1 ? "has" : "have"} no other ready host with what ${stranded.length === 1 ? "it needs" : "they need"}.`) : "")
      + `<div class="dependency-list">${impactRows(impact)}</div>`
    : '<div class="ui-empty">No apps run on this host.</div>';
  const quorum = UI.facts([
    ["etcd members", `${esc(qr.total ?? "?")} · ${(qr.ready || []).length} ready`],
    ["Quorum needs", esc(qr.quorum_needs ?? "?")],
    ["Can lose", UI.chip(String(qr.can_lose ?? "?"), qr.can_lose > 0 ? "ok" : "bad")],
    ["This host", isEtcd ? "an etcd member" : "not an etcd member"]]);
  childModal("Host actions · " + name, UI.sectionForm("hostActions", [
    { key: "actions", title: "Actions", html: actions },
    { key: "apps", title: `Apps · ${workloads.length}`, html: apps },
    { key: "quorum", title: "Quorum", html: quorum },
  ], "", { always: true, cancelHtml: UI.cancel("Close") }), true);
  window.applyRole?.();
};
window.nodeCordon = async (node, cordon) => {
  try {
    await api("/api/node/cordon", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ node, cordon }) });
    toast(`${node} ${cordon ? "cordoned" : "uncordoned"}`, "ok"); refresh(true);
  } catch (e) { toast(e.message, "bad"); }
};
window.nodeDrain = async node => {
  evacuateNode(node);
};
/* Make an app host a server in place (#364): reviewed first, then a job. */
window.nodePromoteReview = async node => {
  let plan;
  try { plan = await api(`/api/node/promote/plan?${new URLSearchParams({ node })}`); }
  catch (e) { return toast(`Could not check ${node}: ${e.message}`, "bad"); }
  window.__nodePromotePlan = plan;
  const f = plan.facts || {}, blockers = plan.blockers || [], warnings = plan.warnings || [];
  const list = rows => `<ul>${rows.map(r => `<li>${esc(r)}</li>`).join("")}</ul>`;
  const notice = blockers.length ? UI.callout("bad", "Can't make it a server", list(blockers))
    : warnings.length ? UI.callout("warn", "Check first", list(warnings)) : "";
  const facts = UI.facts([
    ["k3s", `<span class="mono">${esc(f.version || "?")}</span>`],
    ["Joins through", `<span class="mono">${esc(f.server || "?")}</span>`],
    ["Volume copies here", `${esc(f.replicas ?? "unknown")} · they stay`]]);
  // The flags the server is started with: detail most people do not need.
  const settings = UI.more("Settings", UI.facts([
    ["The cluster's", `<span class="mono">${esc((f.settings || []).join(" ") || "none")}</span>`],
    ["This host's", `<span class="mono">${esc((f.host_settings || []).join(" ") || "none")}</span>`]]));
  const stages = UI.steps([
    { title: "Drain", detailHtml: `Cordon ${esc(node)} and move its apps off` },
    { title: "Reinstall", detailHtml: "Remove the k3s agent and install a server, keeping the host's identity" },
    { title: "Join etcd", detailHtml: `Wait for ${esc(node)} to come back as a member, then uncordon it` }]);
  childModal(`Make a server · ${node}`, UI.lead(`Makes ${esc(node)} a server: the cluster gains an etcd vote, and the host keeps its storage.`)
    + notice + facts + stages + settings
    + (blockers.length ? "" : UI.ack("promoteAck", `Take ${node} out of service while k3s reinstalls`))
    + UI.actions(UI.cancel() + UI.button("Make server", `nodePromoteStart(${jsArg(node)})`,
      { kind: "pri", disabled: blockers.length > 0, attrs: 'data-need="admin"' })));
  window.applyRole?.();
};
window.nodePromoteStart = async node => {
  const plan = window.__nodePromotePlan || {};
  if (!$("#promoteAck")?.checked) return toast(`Confirm taking ${node} out of service first`, "warn");
  try {
    const r = await api("/api/node/promote", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ node, review_token: plan.review_token }) });
    toast(r.detail || `Making ${node} a server`, "ok"); closeModal(); refresh(true);
  } catch (e) { toast(e.message, "bad"); }
};
// What would keep the host from draining: each one ticked ("drain anyway")
// before the normal cordon and drain. Force and a planned outage send no drain.
const nodePowerDrainBlockers = plan => plan && !plan.force && !plan.planned_outage ? plan.drain_blockers || [] : [];
// Each tick carries its row's id: what counts is which rows the open dialog
// has ticked, so a stale review brought back by Back acknowledges nothing
// next to other text, and the server's exact-set check sees any mismatch.
const nodePowerTickedIds = () => $$("input[data-drain-ack]").filter(x => x.checked).map(x => x.dataset.drainAck);
const nodePowerBlockersTicked = plan => {
  const rows = nodePowerDrainBlockers(plan);
  if (!rows.length) return true;
  const ticked = new Set(nodePowerTickedIds());
  return rows.every(b => ticked.has(b.id));
};
window.nodePowerReady = () => {
  const button = $("#pw_execute");
  if (button) button.disabled = !typedConfirmMatches("pw_confirm") || !nodePowerBlockersTicked(window.__nodePowerPlan);
};
// Longhorn's leftover engine records on this host whose engine runs on another
// host: reviewed one by one (record, engine, where it runs), removed by an
// admin, then the restart is reviewed again so the row goes.
window.nodePowerRemoveOrphans = i => {
  const plan = window.__nodePowerPlan;
  const row = nodePowerDrainBlockers(plan)[i];
  if (!row || !row.can_remove) return toast("Review the host impact again", "bad");
  const records = row.records || [];
  childModal(`Remove orphan records · ${plan.node}`, UI.lead(`Longhorn then stops the leftover engine processes on ${esc(plan.node)}. `
    + "The volumes keep running where their engines are. Homestead checks each record again before removing it.")
    + UI.table([{ label: "Orphan record" }, { label: "Engine" }, { label: "Runs on" }],
      records.map(r => [`<span class="mono">${esc(r.name)}</span>`, `<span class="mono">${esc(r.engine)}</span>`, esc(r.engine_node)]))
    + UI.actions(UI.button(`Remove ${plural(records.length, "orphan record")}`, `nodePowerRemoveOrphansConfirm(${i})`, { kind: "danger", id: "pw_orphans_remove" })
      + UI.cancel("Back")), true);
};
window.nodePowerRemoveOrphansConfirm = async i => {
  const plan = window.__nodePowerPlan;
  const row = nodePowerDrainBlockers(plan)[i];
  if (!row || !row.can_remove) return toast("Review the host impact again", "bad");
  const button = $("#pw_orphans_remove");
  const label = button?.textContent;
  if (button) { button.disabled = true; button.textContent = "Removing…"; }
  let r;
  try {
    r = await api("/api/node/orphans/remove", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ node: plan.node, records: (row.records || []).map(x => ({ name: x.name, uid: x.uid, resourceVersion: x.resourceVersion })) }) });
  } catch (e) {
    if (button) { button.disabled = false; button.textContent = label; }
    return toast(`Could not remove the orphan records: ${e.message}`, "bad");
  }
  const refused = r.refused || [];
  toast(refused.length ? `Removed ${r.removed.length}; kept ${refused.length}: ${refused.map(x => `${x.name} (${x.why})`).join("; ")}`
    : `Removed ${plural(r.removed.length, "orphan record")}`, refused.length ? "warn" : "ok");
  // Back to the review before it is drawn again, so Back can't bring back
  // the old review (its ticks) or this spent dialog.
  modalBack();
  return window.nodePowerReview(plan.node, plan.action, !!plan.force);
};
/* Review, then reboot or shut down a host (#909), on the shared review flow: it keeps the sequence, the busy flag
   and the one-shot send. This view keeps its words and each gate's own message. */
const NODE_POWER = UI.reviewFlow({
  preview: async ({ node, action, force }) => {
    const plan = await api(`/api/node/power/plan?${new URLSearchParams({ node, action, ...(force ? { force: "1" } : {}) })}`);
    // A host already being handled is followed, not reviewed.
    if (plan.operation?.id) return { followed: plan.operation.id };
    if (plan.node !== node || plan.action !== action || !plan.review_token || typeof plan.ready !== 'boolean')
      throw Object.assign(new Error("Host review is incomplete; refresh before continuing"), { incomplete: true });
    return { plan };
  },
  render: (review, { node, action }) => {
    if (review.followed) return window.nodePowerFollow(review.followed);
    const plan = review.plan;
  const verb = action === "reboot" ? "Reboot" : "Shut down";
  const volumes = plan.volumes || [];
  const workloads = plan.workloads || [];
  const budgets = plan.maintenance?.budgets || [];
  const local = plan.maintenance?.local_storage || [];
  const risk = { unavailable: ["no healthy copy elsewhere", "bad"], "single-copy": ["one copy left · unprotected", "warn"], resync: ["replica resync needed", "info"] };
  const concerns = [...(plan.vms?.length && !plan.hold ? [`VMs to check: ${plan.vms.join(", ")}. Their migration or shutdown must be verified separately.`] : []), ...(plan.warnings || [])];
  const overridable = !plan.force && (plan.overridable || []).length && !(plan.hard_blockers || []).length;
  const forced = window.__nodePowerForced = !!plan.force;
  const outage = !!plan.planned_outage;
  // Forced sends nothing but the command, so nothing is stopped or moved first.
  const hold = forced ? [] : plan.hold || [];
  const drainBlockers = nodePowerDrainBlockers(plan);
  const strandedMove = !outage && (plan.stranded || []).some(w => hold.some(h => h.ns === w.ns && h.name === w.name && h.options.includes("move")));
  const lead = outage
    ? `<b>${esc(node)}</b> is the only host, so the whole cluster goes down. Homestead stops the apps and VMs below cleanly, without evicting pods, then ${action === "reboot" ? "reboots" : "powers off"} the host. They start again when it is back.`
    : forced
    ? `Forced: no cordon or drain. <b>${esc(node)}</b> ${action === "reboot" ? "reboots" : "powers off"} as its power button would, stopping everything in order.`
    : `Homestead drains <b>${esc(node)}</b> - stopping what waits for it, moving the rest - then ${action === "reboot" ? "reboots" : "shuts down"} the host.`;
  const summary = [
    UI.lead(lead),
    UI.facts([["On the host", `${plan.pods} pod${plan.pods === 1 ? "" : "s"} · ${plan.vms?.length || 0} VM${plan.vms?.length === 1 ? "" : "s"}`],
      hold.length ? ["Moves", String(hold.filter(h => h.default === "move").length)] : null,
      hold.length ? ["Waits for the host", String(hold.filter(h => h.default === "wait").length)] : null,
      drainBlockers.length ? ["Before draining", UI.chip(`${drainBlockers.length} to acknowledge`, "warn")] : null,
      volumes.length ? ["Copies elsewhere", volumes.some(v => v.risk === "unavailable") ? UI.chip("none for some", "bad")
        : volumes.some(v => v.risk === "single-copy") ? UI.chip("one for some", "warn") : UI.chip("yes", "ok")] : null]),
    plan.blockers?.length
      ? UI.callout("bad", "Blocked", `<ul class="ui-list">${plan.blockers.map(b => `<li>${esc(b)}</li>`).join("")}</ul>`)
        + (overridable ? `<label class="switch" data-need="admin"><input type="checkbox" onchange="if (this.checked) nodePowerReview(${jsq(node)},${jsq(action)},true)">
            Override these checks - ${esc(action === "reboot" ? "reboot" : "shut down")} anyway, without cordon or drain</label>` : "")
      : concerns.length ? UI.callout(forced ? "bad" : "warn", forced ? "Forced - what happens" : "Check before going ahead", `<ul class="ui-list">${concerns.map(c => `<li>${esc(c)}</li>`).join("")}</ul>`) : "",
    UI.more("After the request", "Follow it in Recent jobs. " + (outage || forced ? "Check apps, VMs and storage when the host is back." : plan.cordoned ? "The host was cordoned before this and stays so." : "The host takes new pods again once it is back with its volumes.")),
  ].join("");
  const apps = plan.hold ? (hold.length
      ? `<p class="ui-help">${outage ? "Nothing can move off the only host: each stops cleanly and starts again after."
          : "Move starts it on another host now. Wait stops it and starts it here again after - for what is tied to this host."}</p>`
        + UI.table([{ label: "App or VM" }, { label: "While the host is down" }], hold.map((h, i) => [
          `<span class="mono">${esc(h.ns)}/${esc(h.name)}</span> <span class="sub">${h.kind === "VirtualMachine" ? "VM" : esc(h.kind)}${h.here > 1 ? ` · ${h.here} pods` : ""}</span>`,
          !h.options.length ? UI.chip(h.why || "stop it by hand", "bad")
          : h.options.length === 1 ? `${UI.chip(h.options[0] === "wait" ? "stops and waits" : h.kind === "VirtualMachine" ? "live-migrates" : "moves", h.options[0] === "wait" ? "warn" : "ok")}${h.why ? ` <span class="sub">${esc(h.why)}</span>` : ""}`
          : `<select id="pw_hold_${i}" data-hold="${esc(h.id)}" aria-label="${esc(h.ns)}/${esc(h.name)} while the host is down">
              <option value="move" ${h.default === "move" ? "selected" : ""}>${h.kind === "VirtualMachine" ? "Live-migrate" : "Move"} to ${esc(h.hosts.length === 1 ? h.hosts[0] : "another host")}</option>
              <option value="wait" ${h.default === "wait" ? "selected" : ""}>Stop and wait for this host</option></select>`]))
      : `<div class="ui-empty">No apps or VMs run on this host.</div>`)
    : workloads.length
      ? UI.table([{ label: "App or VM" }, { label: "During the outage" }], workloads.map(w => [`<span class="mono">${esc(w.ns)}/${esc(w.name)}</span>`,
        w.stranded ? UI.chip("no other eligible host", "bad") : `${UI.chip("may move", "ok")} <span class="sub">to ${esc((w.eligible || []).join(", "))}</span>`]))
      : `<div class="ui-empty">No user Deployments are mapped to this host.</div>`;
  const copies = volumes.length
    ? UI.table([{ label: "Volume" }, { label: "Copies" }], volumes.map(v => [`<span class="mono">${esc(v.claim)}</span>`, UI.chip(...(risk[v.risk] || risk.resync))]))
    : `<div class="ui-empty">No Longhorn replica on this host was found.</div>`;
  // A removable row's button opens its own review (nodePowerRemoveOrphans).
  const blockersHtml = `<p class="ui-help">Each can stall the drain. Fix it and review again, or tick it to drain anyway.</p>`
    + drainBlockers.map((b, i) => `<div>${UI.ack(`pw_blocker_${i}`, b.what, { onchange: "nodePowerReady()", attrs: `data-drain-ack="${esc(b.id)}"` })}
      ${UI.more("Why", `${esc(b.why)} ${esc(b.fix)}`, false, `pw_blocker_fix_${i}`)}${!b.can_remove ? ""
        : can("admin") ? UI.button("Remove orphan records", `nodePowerRemoveOrphans(${i})`, { id: `pw_blocker_remove_${i}`, attrs: `data-drain-blocker="${esc(b.id)}"` })
        : `<p class="ui-help">An admin can remove these orphan records here.</p>`}</div>`).join("");
  const sections = [
    { key: "summary", title: "Summary", html: summary },
    drainBlockers.length ? { key: "drain", title: `Before draining · ${drainBlockers.length}`, html: blockersHtml } : null,
    { key: "apps", title: `${plan.hold ? "Apps and VMs" : "What goes down"} · ${plan.hold ? hold.length : workloads.length}`, html: apps },
    { key: "volumes", title: `Volume copies · ${volumes.length}`, html: copies },
    budgets.length ? { key: "budgets", title: `Disruption budgets · ${budgets.length}`, html: UI.table([{ label: "Pod" }, { label: "Budget" }], budgets.map(b => [`<span class="mono">${esc(b.pod)}</span>`,
      `${esc(b.budget)} · ${b.allowed == null ? "status unknown" : `${plural(b.allowed, "disruption")} allowed`}${b.unhealthy_allowed ? " · unhealthy eviction allowed" : ""}${b.wait_for_drain && !forced && !outage ? " · waits for Longhorn during drain" : ""}`])) } : null,
    local.length ? { key: "local", title: `Local and external storage · ${local.length}`, html: `<p class="ui-help">${outage ? "Temporary pod data may be lost. Host-local and external storage may be unavailable while the host is down." : "Drain deletes emptyDir data, and host-local paths stay behind. Check external storage does not depend on this host."}</p>`
      + UI.table([{ label: "Pod" }, { label: "Storage" }], local.map(v => [`<span class="mono">${esc(v.pod)}</span>`, `${esc(v.kind)} · ${esc(v.source)}`])) } : null,
  ];
  // The confirmation stays in view whichever section is open.
  const confirm = plan.ready ? [
    UI.typedConfirm({ id: "pw_confirm", expect: node, oninput: "nodePowerReady()" }),
    outage ? UI.ack("pw_outage", action === "reboot"
      ? "Reboot anyway: the whole cluster, Homestead too, is offline until it returns"
      : "Power off anyway: the whole cluster stays offline until it is powered on by hand") : "",
    !outage && (plan.hold ? strandedMove : plan.stranded?.length) ? UI.ack("pw_allow", `Leave ${plan.stranded.length === 1 ? "1 app or VM" : `${plan.stranded.length} apps and VMs`} down`) : "",
    plan.requires_data_ack ? UI.ack("pw_data", "Go ahead despite the risk to volume copies") : "",
  ].join("") : "";
  childModal(`${forced ? `Forced ${verb.toLowerCase()}` : verb} · ${node}`, UI.sectionForm("hostPower", sections,
    plan.ready ? UI.button(`${forced ? "Force " + verb.toLowerCase() : verb + " host"}`, `nodePower(${jsArg(node)},${jsArg(action)})`, { kind: "danger", id: "pw_execute", disabled: true }) : "",
    { always: true, noticeHtml: confirm, cancelHtml: UI.cancel(plan.ready ? "Cancel" : "Close") }), true);
  },
  failed: error => toast(error.incomplete ? error.message : `Could not assess this host: ${error.message}`, "bad"),
  blocked: review => !!review.followed || !review.plan.ready,
  apply: async review => {
    const plan = review.plan, { node, action } = plan;
    const c = $("#pw_confirm").value.trim();
    const choices = Object.fromEntries((plan.force ? [] : plan.hold || []).map((h, i) => [h.id, $(`#pw_hold_${i}`)?.value]).filter(([, pick]) => pick));
    const drainBlockers = nodePowerDrainBlockers(plan);
    // The flow consumed the review before this, so double clicks and lost replies cannot send twice.
    const button = $("#pw_execute");
    if (button) { button.disabled = true; button.textContent = "Starting…"; }
    try {
      const r = await api("/api/node/power", { method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ node, action, confirm: c, review_token: plan.review_token, force: !!plan.force, choices,
          allow_cluster_outage: !!$("#pw_outage")?.checked, allow_stranded: plan.planned_outage ? !!$("#pw_outage")?.checked : !!$("#pw_allow")?.checked, allow_data_risk: !!$("#pw_data")?.checked,
          allow_drain_blockers: drainBlockers.length ? nodePowerTickedIds() : [] }) });
      if (r.operation) window.noteOperation?.(r.operation);
      if (r.operation?.id) return window.nodePowerFollow(r.operation.id, plan.planned_outage
        ? "Homestead will go offline during this host's maintenance. This view reconnects automatically. Scheduling was left unchanged."
        : plan.force ? "Request accepted without cordon or drain. Scheduling was left unchanged."
        : `The host is cordoned and drained before the command is sent. ${plan.cordoned ? "It was cordoned before, and stays so." : "Scheduling is allowed again when it is back."}`, r.operation);
      modal("Host maintenance", UI.lead(plan.force || plan.planned_outage ? `Sent. Follow it in Recent jobs${plan.action === "reboot" ? "; this page comes back when the host does" : ""}. Scheduling was left unchanged; inspect apps and storage when the host returns.`
        : `Follow progress in Recent jobs. ${plan.cordoned ? "The host stays cordoned, as it was before." : "Scheduling is allowed again when it is back."}`) +
        UI.more("Steps so far", `<pre>${esc((r.steps || []).join("\n"))}</pre>`) + UI.actions(UI.cancel("Close")));
    } catch (e) {
      // A power job already going for this host: follow it rather than start another.
      if (e.body?.operation?.id) return window.nodePowerFollow(e.body.operation.id);
      toast(e.message + " — inspect Recent jobs and the host before making another request.", "bad");
      if (button) {
        button.disabled = false; button.textContent = "Review host again";
        button.onclick = () => window.nodePowerReview(node, action);
      }
    }
  
  },
  after: () => window.refreshOperations?.(true),
});
// What the page and its tests read: the plan being reviewed, or nothing once it is consumed.
Object.defineProperty(window, "__nodePowerPlan", { get: () => NODE_POWER.review?.plan || null, configurable: true });
window.nodePowerReview = (node, action, force = false) => NODE_POWER.open({ node, action, force });
window.nodePower = async (node, action) => {
  const plan = NODE_POWER.review?.plan;
  if (!plan || !plan.ready || plan.node !== node || plan.action !== action) return toast("Review the host impact again", "bad");
  if (!typedConfirmMatches("pw_confirm")) return;
  if (plan.planned_outage && !$("#pw_outage")?.checked)
    return toast("Acknowledge the whole-cluster outage", "bad");
  if (!plan.planned_outage && $("#pw_allow") && !$("#pw_allow").checked)
    return toast("Confirm the apps and VMs that will remain down", "bad");
  if (plan.requires_data_ack && !$("#pw_data")?.checked)
    return toast("Confirm the volume risk", "bad");
  if (!nodePowerBlockersTicked(plan))
    return toast("Tick drain anyway for each thing that would keep the host from draining", "bad");
  await NODE_POWER.apply();
};

/* A reboot or shutdown as it goes: its steps, from the job's recorded phase.
   Homestead itself may be unreachable while a host restarts - on a host it
   runs on, or the only one - so the view keeps asking until the job ends. */
let NODE_POWER_FOLLOW = 0;
function nodePowerSteps(power) {
  const reboot = power.action === "reboot";
  return [
    ...(power.direct ? [["verifying", "Recheck the host"], ...(power.holds ? [["holding", "Stop the apps and VMs so their volumes detach"]] : [])] : [
      ["cordoning", "Stop new work on the host (cordon)"],
      ...(power.holds ? [["holding", "Stop what waits for the host; live-migrate VMs"]] : []),
      ["draining", "Move apps and VMs off the host (drain)"],
      ["verifying", "Recheck quorum, VMs and volume copies"]]),
    ["sending", `Send the ${reboot ? "restart" : "shutdown"} command`],
    ["observing", reboot ? "Wait for the host to restart and its volumes to recover" : "Wait for the host to power off"],
    ...(power.holds ? [["restoring", reboot ? "Start what waited for it again" : "Start what waited for it when it is back"]] : []),
  ];
}
function nodePowerProgressMarkup(op, offline = false, disconnectedFor = 0) {
  const power = op.power || {}, steps = nodePowerSteps(power);
  const done = op.status === "succeeded", stopped = op.status === "failed" || op.status === "cancelled";
  const phase = stopped && power.failed_phase ? power.failed_phase : power.phase;
  const at = Math.max(0, steps.findIndex(([name]) => name === phase));
  const items = steps.map(([, title], i) => ({ title,
    state: done || i < at ? "ok" : i === at ? (stopped ? "bad" : offline ? "todo" : "run") : "todo",
    detailHtml: i === at && !done && !stopped && op.message ? esc(op.message) : "" }));
  if (offline && !done && !stopped) {
    const prolonged = disconnectedFor >= 15 * 60 * 1000;
    const minutes = Math.floor(disconnectedFor / 60000);
    const expected = power.planned_outage || power.handoff;
    const detail = power.handoff
      ? "The host helper was created to finish the request. Homestead must stop so its data volume can detach before power is sent."
      : expected ? "Homestead goes offline during maintenance on the only host. The last update may be from before the restart was requested."
      : "The connection to Homestead was lost. The host's current state cannot be confirmed.";
    return UI.callout(prolonged ? "warn" : "info", prolonged ? "Still waiting for Homestead" : "Waiting for Homestead to reconnect",
      `${detail} ${power.action === "poweroff" ? "After shutdown, turn the host on manually to reconnect." : "This view resumes when Homestead is reachable."}`)
      + `<p class="ui-help" role="status">Checking automatically${minutes ? ` · disconnected for ${minutes} min` : ""}. ${prolonged
        ? "Check the host console or network if it has not returned. Do not send another power request while its state is unknown."
        : "Live progress is unavailable while disconnected."}</p>`
      + (power.phase ? UI.more("Last confirmed progress", UI.checklist(items)) : "")
      + UI.actions(UI.cancel("Close"));
  }
  return UI.progress(done ? 100 : op.progress, { label: done ? "Finished" : stopped ? "Stopped" : "In progress", kind: done ? "ok" : stopped ? "bad" : "info" })
    + UI.checklist(items)
    + (done ? UI.callout("ok", "Done", esc(op.message || "")) : "")
    + (stopped ? UI.callout("bad", "Stopped", `${esc(op.message || "The job did not finish.")} Check the host before making another request.`) : "")
    + (power.waiting && !done && !stopped && power.phase === "observing"
      ? UI.callout("info", `${power.held} waiting for this host`, "They start again when it is back. To start them on other hosts now instead - those that can run there - release them.")
        + `<div class="row"><button class="btn" data-need="operator" onclick="nodePowerRelease(${jsq(op.id)})">Start them on other hosts now</button></div>` : "")
    + UI.actions(UI.cancel("Close"));
}
window.nodePowerRelease = async id => {
  try {
    const r = await api("/api/node/power/release", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ id }) });
    toast(r.detail, "ok");
  } catch (e) { toast(e.message, "bad"); }
};
window.nodePowerFollow = async (id, intro = "", initial = null) => {
  const sequence = ++NODE_POWER_FOLLOW;
  modal("Host maintenance", (intro ? UI.lead(esc(intro)) : "") + '<div id="pwProgress"><div class="empty"><span class="spin2"></span>Starting…</div></div>', true);
  let last = initial?.id === id ? initial : null, disconnectedAt = null;
  while (sequence === NODE_POWER_FOLLOW && $("#pwProgress")) {
    let offline = false;
    try {
      last = (await api("/api/operations", { keep: true, timeout: 10000 })).find(o => o.id === id) || null;
      disconnectedAt = null;
    } catch (e) {
      if ((e.status === 401 || e.status === 403) && sequence === NODE_POWER_FOLLOW && $("#pwProgress")) {
        $("#pwProgress").innerHTML = UI.callout("warn", "Cannot read maintenance progress", "Sign in with access to Jobs to check this request. Its outcome has not been confirmed.") + UI.actions(UI.cancel("Close"));
        return;
      }
      offline = true;
      disconnectedAt ??= Date.now();
    }
    if (!$("#pwProgress") || sequence !== NODE_POWER_FOLLOW) return;
    if (last) $("#pwProgress").innerHTML = nodePowerProgressMarkup(last, offline, offline ? Date.now() - disconnectedAt : 0);
    else if (offline) $("#pwProgress").innerHTML = nodePowerProgressMarkup({ power: {}, status: "running" }, true, Date.now() - disconnectedAt);
    else if (!offline) { $("#pwProgress").innerHTML = UI.callout("warn", "Job not found", "It may have been cleared. Check Recent jobs and the host.") + UI.actions(UI.cancel("Close")); return; }
    if (last && ["succeeded", "failed", "cancelled"].includes(last.status)) { window.refreshOperations?.(true); return; }
    await new Promise(resolve => setTimeout(resolve, 2000));
  }
};

// Node only (tests): the same names, for require(). The browser has no `module`.
if (typeof module === "object" && module.exports) module.exports = {
  nodePowerDrainBlockers, nodePowerTickedIds, nodePowerBlockersTicked, NODE_POWER, NODE_POWER_FOLLOW, nodePowerSteps,
  nodePowerProgressMarkup
};
