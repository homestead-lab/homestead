/* Homestead views: a host's actions: cordon, drain, promote and power. Split from views-lifecycle.js (#911); a classic script. */
/* ---------------- node power ---------------- */
window.nodeActions = async name => {
  let qr = {}, impact = { workloads: [], stranded: [] };
  try { [qr, impact] = await Promise.all([api("/api/quorum"), api(`/api/node/impact?node=${encodeURIComponent(name)}`)]); }
  catch (e) {
    return childModal("Host actions unavailable", `${UI.callout("bad", "", `The checks of what this host affects could not be completed. ${esc(String(e.message).replace(/\.$/, ""))}. Restart and shutdown are not available until the checks work.`)}
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
      ? action("Cordoned", `${declared ? "Uncordoning does not end its maintenance. " : ""}No new apps start on this host. Uncordon it when the host is ready for work again.`, UI.button("Uncordon host", `nodeCordon(${jsArg(name)},false)`, { attrs: 'data-need="admin"' }), "warn")
      : action("Cordon", "Stops new apps from starting on this host. The apps already running keep running.", UI.button("Cordon host", `nodeCordon(${jsArg(name)},true)`, { attrs: 'data-need="admin"' }))}
    ${action("If this host fails", "Shows what stops, what waits, what moves and where, which volumes are at risk and which addresses move.",
      UI.button("Preview failure", `nodeIfDown(${jsArg(name)},() => nodeActions(${jsArg(name)}))`))}
    ${action("Move apps off", "Cordons the host, then moves every app on it to other hosts.", UI.button("Move apps off", `evacuateNode(${jsArg(name)})`, { attrs: 'data-need="admin"' }))}
    ${action("Drain", "Stops the apps on this host so that they start on other hosts, within their disruption budgets.", UI.button("Drain host", `nodeDrain(${jsArg(name)})`, { attrs: 'data-need="admin"' }))}
    ${off ? action("Power control is off", "Restart and shutdown need ENABLE_NODE_POWER=true on the Homestead Deployment.", "", "neutral")
      : action("Restart or shut down", (qr.total === 1 && isEtcd ? "This is the only host, so the whole cluster goes down. " : "")
          + "Reviewed first: what moves, what waits for the host, and its volume copies.",
        UI.button("Restart host…", `nodePowerReview(${jsArg(name)},'reboot')`, { attrs: 'data-need="admin"' })
        + UI.button("Shut down host…", `nodePowerReview(${jsArg(name)},'poweroff')`, { kind: "danger", attrs: 'data-need="admin"' }))}
    ${window.hostMaintenanceRow?.(name, declared) || ""}
    ${promotable ? action("Make this a server", "Gives the cluster another vote on its state. The apps move off while k3s is reinstalled, and the storage stays.",
      UI.button("Make server…", `nodePromoteReview(${jsArg(name)})`, { attrs: 'data-need="admin"' })) : ""}
    ${action("Remove from cluster", "Removes this host for good, after checking the votes the cluster needs and the volume copies.",
      UI.button("Remove host…", `nodeRemoval(${jsArg(name)})`, { kind: "danger", attrs: 'data-need="admin"' }), "bad")}
  </div>`;
  const apps = typeof hostAppsHtml === "function" ? hostAppsHtml(impact) : workloads.length
    ? (stranded.length ? UI.callout("warn", `${stranded.length} can run only on this host`, `${stranded.map(w => `<span class="mono">${esc(w.name)}</span>`).join(", ")} ${stranded.length === 1 ? "has" : "have"} no other ready host with what ${stranded.length === 1 ? "it needs" : "they need"}.`) : "")
      + `<div class="dependency-list">${impactRows(impact)}</div>`
    : '<div class="ui-empty">No apps run on this host.</div>';
  const quorum = UI.facts([
    [`Voting hosts ${tip("Hosts that vote on the cluster state. Kubernetes calls them etcd members, and a majority must be running for the cluster to work. This majority is called quorum.")}`, `${esc(qr.total ?? "?")} · ${(qr.ready || []).length} ready`],
    ["Votes needed", esc(qr.quorum_needs ?? "?")],
    ["Hosts that can fail", UI.chip(String(qr.can_lose ?? "?"), qr.can_lose > 0 ? "ok" : "bad")],
    ["This host", isEtcd ? "votes" : "does not vote"]]);
  childModal("Host actions · " + name, UI.sectionForm("hostActions", [
    { key: "actions", title: "Actions", html: actions },
    { key: "apps", title: `Apps · ${workloads.length}`, html: apps },
    { key: "quorum", title: "Cluster votes", html: quorum },
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
  catch (e) { return toast(`${node} could not be checked. ${e.message}`, "bad"); }
  window.__nodePromotePlan = plan;
  const f = plan.facts || {}, blockers = plan.blockers || [], warnings = plan.warnings || [];
  const list = rows => `<ul>${rows.map(r => `<li>${esc(r)}</li>`).join("")}</ul>`;
  const notice = blockers.length ? UI.callout("bad", "This host cannot be made a server", list(blockers))
    : warnings.length ? UI.callout("warn", "Check first", list(warnings)) : "";
  const facts = UI.facts([
    ["k3s", `<span class="mono">${esc(f.version || "?")}</span>`],
    ["Joins through", `<span class="mono">${esc(f.server || "?")}</span>`],
    ["Volume copies on it", `${esc(f.replicas ?? "unknown")} · they stay`]]);
  // The flags the server is started with: detail most people do not need.
  const settings = UI.more("Settings", UI.facts([
    ["The cluster's settings", `<span class="mono">${esc((f.settings || []).join(" ") || "none")}</span>`],
    ["This host's settings", `<span class="mono">${esc((f.host_settings || []).join(" ") || "none")}</span>`]]));
  const stages = UI.steps([
    { title: "Drain", detailHtml: `Cordon ${esc(node)} and move its apps off.` },
    { title: "Reinstall", detailHtml: "Remove the k3s agent and install a server. The host keeps its identity." },
    { title: "Start voting", detailHtml: `Wait for ${esc(node)} to return as a voting member, then uncordon it.` }]);
  childModal(`Make a server · ${node}`, UI.lead(`Makes ${esc(node)} a server. The cluster gains a vote on its state, and the host keeps its storage.`)
    + notice + facts + stages + settings
    + (blockers.length ? "" : UI.ack("promoteAck", `Take ${node} out of service while k3s reinstalls`))
    + UI.actions(UI.cancel() + UI.button("Make server", `nodePromoteStart(${jsArg(node)})`,
      { kind: "pri", disabled: blockers.length > 0, attrs: 'data-need="admin"' })));
  window.applyRole?.();
};
window.nodePromoteStart = async node => {
  const plan = window.__nodePromotePlan || {};
  if (!$("#promoteAck")?.checked) return toast(`First confirm that ${node} can be taken out of service`, "warn");
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
  if (!row || !row.can_remove) return toast("Review the host again", "bad");
  const records = row.records || [];
  childModal(`Remove leftover records · ${plan.node}`, UI.lead(`Longhorn then stops the leftover volume processes on ${esc(plan.node)}. `
    + "The volumes keep running on the hosts where their engines run. Homestead checks each record again before removing it.")
    + UI.table([{ label: "Leftover record" }, { label: "Engine" }, { label: "Runs on" }],
      records.map(r => [`<span class="mono">${esc(r.name)}</span>`, `<span class="mono">${esc(r.engine)}</span>`, esc(r.engine_node)]))
    + UI.actions(UI.button(`Remove ${plural(records.length, "leftover record")}`, `nodePowerRemoveOrphansConfirm(${i})`, { kind: "danger", id: "pw_orphans_remove" })
      + UI.cancel("Back")), true);
};
window.nodePowerRemoveOrphansConfirm = async i => {
  const plan = window.__nodePowerPlan;
  const row = nodePowerDrainBlockers(plan)[i];
  if (!row || !row.can_remove) return toast("Review the host again", "bad");
  const button = $("#pw_orphans_remove");
  const label = button?.textContent;
  if (button) { button.disabled = true; button.textContent = "Removing…"; }
  let r;
  try {
    r = await api("/api/node/orphans/remove", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ node: plan.node, records: (row.records || []).map(x => ({ name: x.name, uid: x.uid, resourceVersion: x.resourceVersion })) }) });
  } catch (e) {
    if (button) { button.disabled = false; button.textContent = label; }
    return toast(`The leftover records could not be removed. ${e.message}`, "bad");
  }
  const refused = r.refused || [];
  toast(refused.length ? `Removed ${r.removed.length} and kept ${refused.length}: ${refused.map(x => `${x.name} (${x.why})`).join("; ")}`
    : `Removed ${plural(r.removed.length, "leftover record")}`, refused.length ? "warn" : "ok");
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
      throw Object.assign(new Error("The host review is incomplete. Refresh before continuing."), { incomplete: true });
    return { plan };
  },
  render: (review, { node, action }) => {
    if (review.followed) return window.nodePowerFollow(review.followed);
    const plan = review.plan;
  const verb = action === "reboot" ? "Restart" : "Shut down";
  const volumes = plan.volumes || [];
  const workloads = plan.workloads || [];
  const budgets = plan.maintenance?.budgets || [];
  const local = plan.maintenance?.local_storage || [];
  const risk = { unavailable: ["no healthy copy on another host", "bad"], "single-copy": ["one copy left · unprotected", "warn"], resync: ["copy needs updating", "info"] };
  const concerns = [...(plan.vms?.length && !plan.hold ? [`VMs to check: ${plan.vms.join(", ")}. Check separately that they moved or shut down.`] : []), ...(plan.warnings || [])];
  const overridable = !plan.force && (plan.overridable || []).length && !(plan.hard_blockers || []).length;
  const forced = window.__nodePowerForced = !!plan.force;
  const outage = !!plan.planned_outage;
  // Forced sends nothing but the command, so nothing is stopped or moved first.
  const hold = forced ? [] : plan.hold || [];
  const drainBlockers = nodePowerDrainBlockers(plan);
  const strandedMove = !outage && (plan.stranded || []).some(w => hold.some(h => h.ns === w.ns && h.name === w.name && h.options.includes("move")));
  const lead = outage
    ? `<b>${esc(node)}</b> is the only host, so the whole cluster goes down. Homestead stops the apps and VMs below cleanly, then ${action === "reboot" ? "restarts" : "turns off"} the host. They start again when it is back.`
    : forced
    ? `Forced, with no cordon or drain. <b>${esc(node)}</b> ${action === "reboot" ? "restarts" : "turns off"} as it would with its power button, stopping everything in order.`
    : `Homestead drains <b>${esc(node)}</b>, which stops what waits for it and moves the rest. It then ${action === "reboot" ? "restarts" : "shuts down"} the host.`;
  const summary = [
    UI.lead(lead),
    UI.facts([["On the host", `${plan.pods} app cop${plan.pods === 1 ? "y" : "ies"} · ${plan.vms?.length || 0} VM${plan.vms?.length === 1 ? "" : "s"}`],
      hold.length ? ["Moves", String(hold.filter(h => h.default === "move").length)] : null,
      hold.length ? ["Waits for the host", String(hold.filter(h => h.default === "wait").length)] : null,
      drainBlockers.length ? ["Before draining", UI.chip(`${drainBlockers.length} to accept`, "warn")] : null,
      volumes.length ? ["Copies on other hosts", volumes.some(v => v.risk === "unavailable") ? UI.chip("none for some", "bad")
        : volumes.some(v => v.risk === "single-copy") ? UI.chip("one for some", "warn") : UI.chip("yes", "ok")] : null]),
    plan.blockers?.length
      ? UI.callout("bad", "This cannot go ahead", `<ul class="ui-list">${plan.blockers.map(b => `<li>${esc(b)}</li>`).join("")}</ul>`)
        + (overridable ? `<label class="switch" data-need="admin"><input type="checkbox" onchange="if (this.checked) nodePowerReview(${jsq(node)},${jsq(action)},true)">
            Override these checks and ${esc(action === "reboot" ? "restart" : "shut down")} anyway, without cordon or drain</label>` : "")
      : concerns.length ? UI.callout(forced ? "bad" : "warn", forced ? "Effects of forcing" : "Check before continuing", `<ul class="ui-list">${concerns.map(c => `<li>${esc(c)}</li>`).join("")}</ul>`) : "",
    UI.more("After the request", "Progress is shown in Recent jobs. " + (outage || forced ? "Check the apps, VMs and storage when the host is back." : plan.cordoned ? "The host was already cordoned and stays cordoned." : "The host takes new apps again when it is back with its volumes.")),
  ].join("");
  const apps = plan.hold ? (hold.length
      ? `<p class="ui-help">${outage ? "Nothing can move off the only host. Each app stops cleanly and starts again afterwards."
          : "Move starts it on another host now. Wait stops it and starts it on this host again afterwards, for apps tied to this host."}</p>`
        + UI.table([{ label: "App or VM" }, { label: "While the host is down" }], hold.map((h, i) => [
          `<span class="mono">${esc(h.ns)}/${esc(h.name)}</span> <span class="sub">${h.kind === "VirtualMachine" ? "VM" : esc(h.kind)}${h.here > 1 ? ` · ${h.here} copies` : ""}</span>`,
          !h.options.length ? UI.chip(h.why || "stop it manually", "bad")
          : h.options.length === 1 ? `${UI.chip(h.options[0] === "wait" ? "stops and waits" : h.kind === "VirtualMachine" ? "moves while running" : "moves", h.options[0] === "wait" ? "warn" : "ok")}${h.why ? ` <span class="sub">${esc(h.why)}</span>` : ""}`
          : `<select id="pw_hold_${i}" data-hold="${esc(h.id)}" aria-label="${esc(h.ns)}/${esc(h.name)} while the host is down">
              <option value="move" ${h.default === "move" ? "selected" : ""}>${h.kind === "VirtualMachine" ? "Move while running" : "Move"} to ${esc(h.hosts.length === 1 ? h.hosts[0] : "another host")}</option>
              <option value="wait" ${h.default === "wait" ? "selected" : ""}>Stop and wait for this host</option></select>`]))
      : `<div class="ui-empty">No apps or VMs run on this host.</div>`)
    : workloads.length
      ? UI.table([{ label: "App or VM" }, { label: "During the outage" }], workloads.map(w => [`<span class="mono">${esc(w.ns)}/${esc(w.name)}</span>`,
        w.stranded ? UI.chip("no other suitable host", "bad") : `${UI.chip("may move", "ok")} <span class="sub">to ${esc((w.eligible || []).join(", "))}</span>`]))
      : `<div class="ui-empty">No apps run on this host.</div>`;
  const copies = volumes.length
    ? UI.table([{ label: "Volume" }, { label: "Copies" }], volumes.map(v => [`<span class="mono">${esc(v.claim)}</span>`, UI.chip(...(risk[v.risk] || risk.resync))]))
    : `<div class="ui-empty">No volume copies were found on this host.</div>`;
  // A removable row's button opens its own review (nodePowerRemoveOrphans).
  const blockersHtml = `<p class="ui-help">Each of these can stop the drain. Fix it and review again, or tick it to drain anyway.</p>`
    + drainBlockers.map((b, i) => `<div>${UI.ack(`pw_blocker_${i}`, b.what, { onchange: "nodePowerReady()", attrs: `data-drain-ack="${esc(b.id)}"` })}
      ${UI.more("Why", `${esc(b.why)} ${esc(b.fix)}`, false, `pw_blocker_fix_${i}`)}${!b.can_remove ? ""
        : can("admin") ? UI.button("Remove leftover records", `nodePowerRemoveOrphans(${i})`, { id: `pw_blocker_remove_${i}`, attrs: `data-drain-blocker="${esc(b.id)}"` })
        : `<p class="ui-help">An admin can remove these leftover records here.</p>`}</div>`).join("");
  const sections = [
    { key: "summary", title: "Summary", html: summary },
    drainBlockers.length ? { key: "drain", title: `Before draining · ${drainBlockers.length}`, html: blockersHtml } : null,
    { key: "apps", title: `${plan.hold ? "Apps and VMs" : "What goes down"} · ${plan.hold ? hold.length : workloads.length}`, html: apps },
    { key: "volumes", title: `Volume copies · ${volumes.length}`, html: copies },
    budgets.length ? { key: "budgets", title: `Disruption budgets · ${budgets.length}`, html: UI.table([{ label: "Pod" }, { label: "Budget" }], budgets.map(b => [`<span class="mono">${esc(b.pod)}</span>`,
      `${esc(b.budget)} · ${b.allowed == null ? "status unknown" : `${plural(b.allowed, "interruption")} allowed`}${b.unhealthy_allowed ? " · unhealthy copies can be stopped" : ""}${b.wait_for_drain && !forced && !outage ? " · waits for Longhorn during drain" : ""}`])) } : null,
    local.length ? { key: "local", title: `Local and external storage · ${local.length}`, html: `<p class="ui-help">${outage ? "Temporary app data may be lost. Storage on the host or outside the cluster may be unavailable while the host is down." : "Draining deletes temporary (emptyDir) data, and host folders stay on this host. Check that storage outside the cluster does not depend on this host."}</p>`
      + UI.table([{ label: "Pod" }, { label: "Storage" }], local.map(v => [`<span class="mono">${esc(v.pod)}</span>`, `${esc(v.kind)} · ${esc(v.source)}`])) } : null,
  ];
  // The confirmation stays in view whichever section is open.
  const confirm = plan.ready ? [
    UI.typedConfirm({ id: "pw_confirm", expect: node, oninput: "nodePowerReady()" }),
    outage ? UI.ack("pw_outage", action === "reboot"
      ? "Restart anyway. The whole cluster, including Homestead, is offline until the host returns."
      : "Shut down anyway. The whole cluster stays offline until the host is turned on manually.") : "",
    !outage && (plan.hold ? strandedMove : plan.stranded?.length) ? UI.ack("pw_allow", `Leave ${plan.stranded.length === 1 ? "1 app or VM" : `${plan.stranded.length} apps and VMs`} down`) : "",
    plan.requires_data_ack ? UI.ack("pw_data", "Continue despite the risk to volume copies") : "",
  ].join("") : "";
  childModal(`${forced ? `Forced ${verb.toLowerCase()}` : verb} · ${node}`, UI.sectionForm("hostPower", sections,
    plan.ready ? UI.button(`${forced ? "Force " + verb.toLowerCase() : verb + " host"}`, `nodePower(${jsArg(node)},${jsArg(action)})`, { kind: "danger", id: "pw_execute", disabled: true }) : "",
    { always: true, noticeHtml: confirm, cancelHtml: UI.cancel(plan.ready ? "Cancel" : "Close") }), true);
  },
  failed: error => toast(error.incomplete ? error.message : `This host could not be checked. ${error.message}`, "bad"),
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
        ? "Homestead goes offline during this host's maintenance. This view reconnects automatically. The host was not cordoned."
        : plan.force ? "The request was accepted without cordon or drain. The host was not cordoned."
        : `The host is cordoned and drained before the command is sent. ${plan.cordoned ? "It was already cordoned and stays cordoned." : "It is uncordoned when it is back."}`, r.operation);
      modal("Host maintenance", UI.lead(plan.force || plan.planned_outage ? `Sent. Progress is shown in Recent jobs${plan.action === "reboot" ? ", and this page returns when the host does" : ""}. The host was not cordoned. Check the apps and storage when the host returns.`
        : `Progress is shown in Recent jobs. ${plan.cordoned ? "The host stays cordoned, as it was before." : "It is uncordoned when it is back."}`) +
        UI.more("Steps so far", `<pre>${esc((r.steps || []).join("\n"))}</pre>`) + UI.actions(UI.cancel("Close")));
    } catch (e) {
      // A power job already going for this host: follow it rather than start another.
      if (e.body?.operation?.id) return window.nodePowerFollow(e.body.operation.id);
      toast(String(e.message).replace(/\.$/, "") + ". Check Recent jobs and the host before making another request.", "bad");
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
  if (!plan || !plan.ready || plan.node !== node || plan.action !== action) return toast("Review the host again", "bad");
  if (!typedConfirmMatches("pw_confirm")) return;
  if (plan.planned_outage && !$("#pw_outage")?.checked)
    return toast("Accept that the whole cluster goes down", "bad");
  if (!plan.planned_outage && $("#pw_allow") && !$("#pw_allow").checked)
    return toast("Confirm the apps and VMs that will remain down", "bad");
  if (plan.requires_data_ack && !$("#pw_data")?.checked)
    return toast("Confirm the volume risk", "bad");
  if (!nodePowerBlockersTicked(plan))
    return toast("Tick each item that would stop the drain to drain anyway", "bad");
  await NODE_POWER.apply();
};

/* A reboot or shutdown as it goes: its steps, from the job's recorded phase.
   Homestead itself may be unreachable while a host restarts - on a host it
   runs on, or the only one - so the view keeps asking until the job ends. */
let NODE_POWER_FOLLOW = 0;
function nodePowerSteps(power) {
  const reboot = power.action === "reboot";
  return [
    ...(power.direct ? [["verifying", "Check the host again"], ...(power.holds ? [["holding", "Stop the apps and VMs so their volumes are released"]] : [])] : [
      ["cordoning", "Stop new apps starting on the host (cordon)"],
      ...(power.holds ? [["holding", "Stop what waits for the host, and move VMs while they run"]] : []),
      ["draining", "Move apps and VMs off the host (drain)"],
      ["verifying", "Check the cluster votes, VMs and volume copies again"]]),
    ["sending", `Send the ${reboot ? "restart" : "shutdown"} command`],
    ["observing", reboot ? "Wait for the host to restart and its volumes to recover" : "Wait for the host to turn off"],
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
      ? "A helper on the host finishes the request. Homestead must stop first, so that its data volume is released before the host restarts or shuts down."
      : expected ? "Homestead goes offline during maintenance on the only host. The last update may be from before the request was sent."
      : "The connection to Homestead was lost. The host's current state cannot be confirmed.";
    return UI.callout(prolonged ? "warn" : "info", prolonged ? "Still waiting for Homestead" : "Waiting for Homestead to reconnect",
      `${detail} ${power.action === "poweroff" ? "After shutdown, turn the host on manually to reconnect." : "This view continues when Homestead can be reached."}`)
      + `<p class="ui-help" role="status">Checking automatically${minutes ? ` · disconnected for ${minutes} min` : ""}. ${prolonged
        ? "If the host has not returned, check its console or the network. Do not send another restart or shutdown while its state is unknown."
        : "Progress cannot be shown while disconnected."}</p>`
      + (power.phase ? UI.more("Last confirmed progress", UI.checklist(items)) : "")
      + UI.actions(UI.cancel("Close"));
  }
  return UI.progress(done ? 100 : op.progress, { label: done ? "Finished" : stopped ? "Stopped" : "In progress", kind: done ? "ok" : stopped ? "bad" : "info" })
    + UI.checklist(items)
    + (done ? UI.callout("ok", "Done", esc(op.message || "")) : "")
    + (stopped ? UI.callout("bad", "Stopped", `${esc(op.message || "The job did not finish.")} Check the host before making another request.`) : "")
    + (power.waiting && !done && !stopped && power.phase === "observing"
      ? UI.callout("info", `${power.held} waiting for this host`, "They start again when it is back. To start the ones that can run elsewhere on other hosts now, release them.")
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
        $("#pwProgress").innerHTML = UI.callout("warn", "The maintenance progress cannot be read", "Sign in with access to Jobs to check this request. Its outcome has not been confirmed.") + UI.actions(UI.cancel("Close"));
        return;
      }
      offline = true;
      disconnectedAt ??= Date.now();
    }
    if (!$("#pwProgress") || sequence !== NODE_POWER_FOLLOW) return;
    if (last) $("#pwProgress").innerHTML = nodePowerProgressMarkup(last, offline, offline ? Date.now() - disconnectedAt : 0);
    else if (offline) $("#pwProgress").innerHTML = nodePowerProgressMarkup({ power: {}, status: "running" }, true, Date.now() - disconnectedAt);
    else if (!offline) { $("#pwProgress").innerHTML = UI.callout("warn", "The job was not found", "It may have been cleared. Check Recent jobs and the host.") + UI.actions(UI.cancel("Close")); return; }
    if (last && ["succeeded", "failed", "cancelled"].includes(last.status)) { window.refreshOperations?.(true); return; }
    await new Promise(resolve => setTimeout(resolve, 2000));
  }
};

// Node only (tests): the same names, for require(). The browser has no `module`.
if (typeof module === "object" && module.exports) module.exports = {
  nodePowerDrainBlockers, nodePowerTickedIds, nodePowerBlockersTicked, NODE_POWER, NODE_POWER_FOLLOW, nodePowerSteps,
  nodePowerProgressMarkup
};
