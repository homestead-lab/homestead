"use strict";
/* Maintenance a person declares (#535): Host actions' reviewed action, Extend
   and End, and what a host's card and page say about it. The review is the one
   a reboot gets, from the same route; what's asked for here is a reason and an
   end, and no power is sent. A dialog of its own, using UI.reviewFlow. */
(() => {
  const HOURS = [[1, "1 hour"], [2, "2 hours"], [4, "4 hours"], [8, "8 hours"], [24, "Until I end it (24 hours at most)"]];
  const seconds = () => Math.floor(Date.now() / 1000);
  const hoursSelect = (id, chosen = 1) => `<select id="${id}">${HOURS.map(([value, label]) =>
    `<option value="${value}"${value === chosen ? " selected" : ""}>${esc(label)}</option>`).join("")}</select>`;

  function leftWords(left) {
    const minutes = Math.max(1, Math.ceil(left / 60)), hours = Math.floor(minutes / 60);
    return hours ? `${hours}h${minutes % 60 ? ` ${minutes % 60}m` : ""} left` : `${minutes}m left`;
  }
  const isOpen = event => event.state ? event.state === "open" : event.ongoing && event.until > seconds();
  // A person ended it and that hasn't finished: End stays offered.
  const isEnding = event => event.state === "ending" || !!event.ending;
  /* "Drive swap · 1h 20m left", from /api/node/maintenance's answer or a row of
     /api/nodes/uptime's. Plain text: whoever shows it escapes it. */
  window.hostMaintenanceWords = event => `${event.reason} · ${isEnding(event) ? "not finished" : isOpen(event) ? leftWords(event.until - seconds()) : "ran out"}`;

  /* The maintenance a person declared on this host and hasn't ended, from its
     uptime record. Maintenance Homestead caused itself is not this. */
  const declaredIn = uptime => (uptime?.maintenance || []).filter(m => m.declared && !m.released).at(-1) || null;
  window.hostMaintenanceChip = name => {
    const event = declaredIn(STATE.data.nodeUptime?.nodes?.[name]);
    if (!event) return "";
    return UI.chip(isEnding(event) ? "maintenance not finished" : isOpen(event) ? "in maintenance" : "maintenance ran out", isOpen(event) ? "warn" : "bad",
      { tip: window.hostMaintenanceWords(event) });
  };
  // The line at the head of a host's page, from the uptime record just read.
  window.hostMaintenanceLine = uptime => {
    const event = declaredIn(uptime);
    if (!event) return "";
    return `<span class="small">${isEnding(event)
      ? `Ending maintenance did not finish: ${esc(event.reason)}. End it again`
      : isOpen(event) ? `In maintenance: ${esc(window.hostMaintenanceWords(event))}`
      : `Maintenance ran out: ${esc(event.reason)}. End or extend it in Host actions`}</span>`;
  };

  /* Host actions' row. declared: /api/node/maintenance's answer, or undefined
     when it could not be read, which leaves Host actions as it is. */
  window.hostMaintenanceRow = (name, declared) => {
    if (declared === undefined) return "";
    const row = (tone, title, detail, buttons) => `<div class="ui-insight-row">${UI.statusDot(tone)}<div><b>${esc(title)}</b><small>${esc(detail)}</small></div><div class="ui-insight-action">${buttons}</div></div>`;
    if (!declared) return row("neutral", "Maintenance", "Drain this host for a drive swap or an upgrade, with a reason and an end. Sends no power.",
      UI.button("Maintenance…", `hostMaintenanceReview(${jsArg(name)})`, { attrs: 'data-need="admin"' }));
    const end = UI.button("End…", `hostMaintenanceEnd(${jsArg(name)})`, { attrs: 'data-need="admin"' });
    const change = UI.button("Extend…", `hostMaintenanceExtend(${jsArg(name)})`, { attrs: 'data-need="admin"' }) + end;
    if (isEnding(declared)) return row("bad", "Ending maintenance did not finish", `${declared.reason}. End it again: that is safe to repeat.`, end);
    return declared.state === "open"
      ? row("warn", "In maintenance", window.hostMaintenanceWords(declared), change)
      : row("bad", "Maintenance ran out", `${declared.reason}. End it, or extend it.`, change);
  };

  const refreshHost = async node => {
    noteNodeUptime(await api("/api/nodes/uptime").catch(e => ({ unread: e.message })));
    window.refresh?.();
    return node;
  };

  /* ---------------- the review ---------------- */
  const reasonNow = () => ($("#mm_reason")?.value || "").trim();
  const flow = UI.reviewFlow({
    preview: node => api(`/api/node/power/plan?${new URLSearchParams({ node, action: "maintenance" })}`),
    render: (plan, node) => renderReview(plan, node),
    failed: error => toast(`Could not assess this host: ${error.message}`, "bad"),
    blocked: plan => !plan.ready,
    button: "mm_execute",
    check: () => typedConfirmMatches("mm_confirm") && reasonNow().length >= 1 && reasonNow().length <= 120
      && [...$$("[data-mm-ack]")].every(box => box.checked),
    apply: plan => send(plan),
    // Something changed since the review: a fresh one, not a retry of the old.
    conflict: (error, plan) => {
      if (error.body?.operation?.id) return follow(error.body.operation.id, plan.node);
      toast(error.message, "warn");
      modalBack();
      return flow.open(plan.node);
    },
  });
  window.hostMaintenanceReview = node => flow.open(node);
  window.hostMaintenanceReady = () => flow.ready();
  window.hostMaintenanceApply = async () => {
    try { await flow.apply(); }
    catch (error) { toast(`${error.message}. Inspect Recent jobs and the host before asking again.`, "bad"); }
  };

  function renderReview(plan, node) {
    if (plan.operation?.id) return follow(plan.operation.id, node);
    const outage = !!plan.planned_outage, hold = plan.hold || [], volumes = plan.volumes || [], drain = plan.drain_blockers || [];
    const budgets = plan.maintenance?.budgets || [], local = plan.maintenance?.local_storage || [];
    const lead = outage
      ? `<b>${esc(node)}</b> is the only host, so nothing is stopped or moved. Homestead only records the maintenance, for uptime and alerts.`
      : `Homestead drains <b>${esc(node)}</b> - stopping what waits for it, moving the rest - then marks it in maintenance. Power is up to you.`;
    const summary = [UI.lead(lead),
      UI.facts([["On the host", `${plan.pods} pod${plan.pods === 1 ? "" : "s"} · ${plan.vms?.length || 0} VM${plan.vms?.length === 1 ? "" : "s"}`],
        hold.length ? ["Waits for the host", String(hold.filter(h => h.default === "wait").length)] : null,
        drain.length ? ["Before draining", UI.chip(`${drain.length} to acknowledge`, "warn")] : null]),
      plan.blockers?.length
        ? UI.callout("bad", "Blocked", `<ul class="ui-list">${plan.blockers.map(b => `<li>${esc(b)}</li>`).join("")}</ul>`)
        : (plan.warnings || []).length ? UI.callout("warn", "Check before going ahead", `<ul class="ui-list">${plan.warnings.map(w => `<li>${esc(w)}</li>`).join("")}</ul>`) : "",
      plan.ready ? UI.fields(
        UI.field("Reason", `<input id="mm_reason" maxlength="120" autocomplete="off" placeholder="Drive swap" oninput="hostMaintenanceReady()">`),
        UI.field("How long", hoursSelect("mm_hours"), { help: "When it runs out, the host stays cordoned and any downtime counts as an outage." })) : "",
    ].join("");
    const apps = hold.length ? UI.table([{ label: "App or VM" }, { label: "During maintenance" }], hold.map((h, i) => [
      `<span class="mono">${esc(h.ns)}/${esc(h.name)}</span>`,
      h.options.length > 1
        ? `<select id="mm_hold_${i}" aria-label="${esc(h.ns)}/${esc(h.name)} during maintenance"><option value="move"${h.default === "move" ? " selected" : ""}>Move to another host</option><option value="wait"${h.default === "wait" ? " selected" : ""}>Stop until maintenance ends</option></select>`
        : UI.chip(h.options[0] === "move" ? "moves" : h.options[0] === "wait" ? "stops until maintenance ends" : h.why || "stop it by hand", h.options[0] === "move" ? "ok" : h.options.length ? "warn" : "bad")]))
      : `<div class="ui-empty">${outage ? "Nothing is stopped or moved." : "No apps or VMs run on this host."}</div>`;
    const copies = volumes.length ? UI.table([{ label: "Volume" }, { label: "Copies elsewhere" }], volumes.map(v => [`<span class="mono">${esc(v.claim)}</span>`, UI.chip(String(v.healthy_elsewhere), v.risk === "unavailable" ? "bad" : v.risk === "single-copy" ? "warn" : "ok")]))
      : `<div class="ui-empty">No Longhorn copy on this host was found.</div>`;
    const blockers = `<p class="ui-help">Each can stall the drain. Fix it and review again, or tick it to drain anyway.</p>` + drain.map((b, i) =>
      `<div>${UI.ack(`mm_blocker_${i}`, b.what, { onchange: "hostMaintenanceReady()", attrs: `data-mm-ack="1" data-drain-ack="${esc(b.id)}"` })}${UI.more("Why", `${esc(b.why)} ${esc(b.fix)}`, false, `mm_blocker_fix_${i}`)}</div>`).join("");
    const sections = [{ key: "summary", title: "Summary", html: summary },
      drain.length ? { key: "drain", title: `Before draining · ${drain.length}`, html: blockers } : null,
      { key: "apps", title: `Apps and VMs · ${hold.length}`, html: apps },
      { key: "volumes", title: `Volume copies · ${volumes.length}`, html: copies },
      budgets.length ? { key: "budgets", title: `Disruption budgets · ${budgets.length}`, html: UI.table([{ label: "Pod" }, { label: "Budget" }], budgets.map(b => [`<span class="mono">${esc(b.pod)}</span>`, esc(b.budget)])) } : null,
      local.length ? { key: "local", title: `Local storage · ${local.length}`, html: UI.table([{ label: "Pod" }, { label: "Storage" }], local.map(v => [`<span class="mono">${esc(v.pod)}</span>`, `${esc(v.kind)} · ${esc(v.source)}`])) } : null];
    const stranded = !outage && (plan.stranded || []).some(w => hold.some(h => h.ns === w.ns && h.name === w.name && h.options.includes("move")));
    const ack = (id, words) => UI.ack(id, words, { onchange: "hostMaintenanceReady()", attrs: 'data-mm-ack="1"' });
    const confirm = plan.ready ? [UI.typedConfirm({ id: "mm_confirm", expect: node, oninput: "hostMaintenanceReady()" }),
      outage ? ack("mm_outage", "Whatever I do to this host takes the whole cluster offline, Homestead too") : "",
      stranded ? ack("mm_allow", `Leave ${plan.stranded.length === 1 ? "1 app or VM" : `${plan.stranded.length} apps and VMs`} down`) : "",
      plan.requires_data_ack ? ack("mm_data", "Go ahead despite the risk to volume copies") : ""].join("") : "";
    childModal(`Maintenance · ${node}`, UI.sectionForm("hostMaintenance", sections,
      plan.ready ? UI.button("Start maintenance", "hostMaintenanceApply()", { kind: "pri", id: "mm_execute", disabled: true }) : "",
      { always: true, noticeHtml: confirm, cancelHtml: UI.cancel(plan.ready ? "Cancel" : "Close") }), true);
    flow.ready();
  }

  async function send(plan) {
    const choices = Object.fromEntries((plan.hold || []).map((h, i) => [h.id, $(`#mm_hold_${i}`)?.value]).filter(([, pick]) => pick));
    const reply = await api("/api/node/power", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({
      node: plan.node, action: "maintenance", confirm: $("#mm_confirm").value.trim(), review_token: plan.review_token,
      reason: reasonNow(), hours: Number($("#mm_hours").value), choices,
      allow_cluster_outage: !!$("#mm_outage")?.checked, allow_stranded: !!$("#mm_allow")?.checked, allow_data_risk: !!$("#mm_data")?.checked,
      allow_drain_blockers: [...$$("[data-drain-ack]")].filter(box => box.checked).map(box => box.dataset.drainAck) }) });
    if (reply.operation) window.noteOperation?.(reply.operation);
    window.refreshOperations?.(true);
    return follow(reply.operation.id, plan.node);
  }

  /* The job's own words as it goes. The drain may evict the copy this page is
     talking to, so a failed read is said and the next one tries again. */
  window.hostMaintenanceFollow = (id, node) => follow(id, node);
  function follow(id, node) {
    modal("Maintenance", '<div id="mmProgress"><div class="empty"><span class="spin2"></span>Starting…</div></div>' + UI.actions(UI.cancel("Close")), true);
    const paint = html => { const box = $("#mmProgress"); if (box) box.innerHTML = html; };
    UI.poll(async signal => {
      let op;
      try { op = (await api("/api/operations", { keep: true, timeout: 10000, signal })).find(row => row.id === id); }
      catch (_) { paint(UI.lead("Homestead is not answering yet; it may be moving off this host. Still trying.")); return; }
      if (!op) { paint(UI.callout("warn", "This job is no longer listed", "Check the host's page for its maintenance.")); return false; }
      const stopped = op.status === "failed" || op.status === "cancelled", done = op.status === "succeeded";
      // A job that failed only at recording it: the event is open and what waited stays stopped.
      const opened = /^In maintenance:/.test(op.message || "");
      paint(stopped ? UI.callout(opened ? "warn" : "bad", opened ? "In maintenance, but its job did not finish" : "Maintenance was not opened", esc(op.message || ""))
        : done ? UI.callout("ok", "In maintenance", `${esc(op.message || "")}. Power is up to you: Reboot and Shut down stay in Host actions.`)
        : `<p class="ui-lead"><span class="spin2"></span> ${esc(op.message || "Preparing the host")}</p>`);
      if (!stopped && !done) return;
      await refreshHost(node);
      return false;
    }, 2000);
  }

  /* ---------------- Extend and End ---------------- */
  window.hostMaintenanceExtend = node => childModal(`Extend maintenance · ${node}`,
    UI.lead("It runs until this long from now. It can only get longer, and 24 hours from now is the most it can be.")
    + UI.fields(UI.field("From now", hoursSelect("mm_extend_hours", 2)))
    + UI.actions(UI.cancel() + UI.button("Extend", `hostMaintenanceExtendGo(${jsArg(node)})`, { kind: "pri", id: "mm_extend_go" })));
  window.hostMaintenanceExtendGo = async node => {
    try {
      await api("/api/node/maintenance/extend", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ node, hours: Number($("#mm_extend_hours").value) }) });
      closeModal();
      toast("Maintenance extended", "ok");
      await refreshHost(node);
    } catch (error) { toast(error.message, "bad"); }
  };
  window.hostMaintenanceEnd = node => {
    const host = (STATE.data.nodes || []).find(n => n.name === node);
    const down = host && host.status !== "Ready";
    childModal(`End maintenance · ${node}`, UI.lead("Ending it allows scheduling on this host again and starts what waited for it, unless it was cordoned before.")
      + (down ? UI.callout("warn", "This host is not Ready", "From now on its downtime counts as an outage, and its alert fires.") : "")
      + '<div id="mm_end_result"></div>'
      + UI.typedConfirm({ id: "mm_end_confirm", expect: node, button: "mm_end_go" })
      + UI.actions(UI.cancel() + UI.button("End maintenance", `hostMaintenanceEndGo(${jsArg(node)})`, { kind: "pri", id: "mm_end_go", disabled: true })));
  };
  window.hostMaintenanceEndGo = async node => {
    if (!typedConfirmMatches("mm_end_confirm")) return;
    try {
      const reply = await api("/api/node/maintenance/end", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ node }) });
      if (reply.ok) {
        closeModal();
        toast(`Maintenance ended. ${reply.detail}`, "ok");
      } else {
        // Not ended: said where it stays, with End still there to ask again.
        const box = $("#mm_end_result");
        if (box) box.innerHTML = UI.callout("bad", "Not finished", esc(reply.detail));
        toast("Ending maintenance did not finish", "bad");
      }
      await refreshHost(node);
    } catch (error) { toast(error.message, "bad"); }
  };
})();
