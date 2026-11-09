/* Moving a workload between hosts — one implementation, surfaced everywhere.
   The picker shows live headroom so the choice is informed, and flags the two
   things that actually bite: losing the iGPU, and RWO volume downtime. */

function nodesNow() {
  return (STATE.data.ov && STATE.data.ov.nodes) || STATE.data.nodes || [];
}

/* find a workload's namespace from whatever view we happen to be in */
function findWl(name) {
  const lists = [STATE.data.wl || [],
                 ((STATE.data.flow || {}).workloads || []).map(w => ({ name: w.name, ns: w.ns, node: w.node, gpu: w.gpu })),
                 (STATE.data.ov ? STATE.data.ov.top_cpu.concat(STATE.data.ov.top_mem) : [])];
  for (const l of lists) {
    const hit = l.find(w => w.name === name);
    if (hit) return hit;
  }
  return { name, ns: "lab" };
}

/* headroom after this workload lands, so "will it fit" is visible up front */
function fitAfter(node, wl) {
  const cpu = (wl && wl.cpu) || 0;
  const mem = (wl && wl.mem_mb) || 0;
  const cpuPct = node.cpu_cap ? Math.min(100, (node.cpu_used + cpu) / node.cpu_cap * 100) : 0;
  const memPct = node.mem_cap_gb ? Math.min(100, (node.mem_used_gb + mem / 1024) / node.mem_cap_gb * 100) : 0;
  return { cpuPct: Math.round(cpuPct), memPct: Math.round(memPct) };
}

/* The Move review (#909) on the shared review flow, which keeps the sequence, the
   busy flag and the one-shot send. Picking a host or changing a switch makes an
   open preview stale (invalidateHostMove). This view keeps its words and the
   consent tick; the Move button stays enabled until the review is blocked, and
   pressing it too early says why, so the flow is given no button id. */
const HOST_MOVE = UI.reviewFlow({
  preview: async config => {
    const review = await api("/api/move/preview", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(config) });
    if (!review.capacity || !review.capacity_token || !review.capacity.move) throw new Error("The move review is not available. Refresh before moving.");
    return { config, ...review };
  },
  render: review => {
    const config = review.config;
    childModal("Review move · " + config.name, `${gitopsNote(review.managed)}<p>Destination: <b>${esc(review.capacity.move.node || "scheduler chooses")}</b> · ${esc(review.capacity.move.mode)}.</p>
      ${UI.callout("warn", "", `This saves a Recreate strategy, so all containers in this app restart after the old copies stop. Clearing a preference can also restart them. Volumes are released and attached again, so the app does not keep running during the move.`)}
      ${deployCapacityHtml(review.capacity)}
      ${review.capacity.move.target ? `<details${review.capacity.move.target.blocked ? " open" : ""}><summary>Selected host: full desired replica count</summary><p class="small muted">The selected host must fit the desired replicas even for a preference. This check does not turn a preference into a hard pin.</p>${deployCapacityHtml(review.capacity.move.target)}</details>` : ""}
      ${!review.capacity.blocked ? `<label class="switch"><input type="checkbox" id="hostMoveConfirm"> Move it despite the warnings</label>` : ""}
      ${UI.actions(`<button data-dialog-dismiss="true" class="btn" onclick="invalidateHostMove();modalBack()">Back</button><button class="btn pri" id="hostMoveGo" ${review.capacity.blocked ? "disabled" : ""} onclick="confirmHostMove()">Move</button>`)}`, true);
  },
  failed: error => toast(error.message, "bad"),
  blocked: review => review.capacity.blocked, ack: "hostMoveConfirm",
  apply: async review => {
    const button = $("#hostMoveGo");
    button.disabled = true; button.textContent = "Applying placement…";
    try {
      await api("/api/move", { method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ ...review.config, capacity_token: review.capacity_token, confirm_capacity: true }) });
      toast(`${review.config.name}: placement saved. Progress is shown in Recent jobs.`, "ok");
      closeModal();
      setTimeout(() => refresh(true), 1500);
    } catch (e) {
      toast(e.message, "bad"); button.disabled = false; button.textContent = "Review again";
      button.onclick = () => window.hostMoveReview(review.config);
    }
  },
});
// The host picker's own plan is not a review, but a pick, a switch or a new review still
// makes the plan being fetched stale, as before.
let HOST_MOVE_PICKER = 0;
function invalidateHostMove() { HOST_MOVE.back(); ++HOST_MOVE_PICKER; }

window.moveWorkload = async (name, ns) => {
  invalidateHostMove();
  const sequence = HOST_MOVE_PICKER;
  const wl = Object.assign(findWl(name), ns ? { ns } : {});
  childModal("Move · " + name, '<div class="empty"><span class="spin2"></span>checking capacity and hardware constraints…</div>', true);
  let plan;
  try {
    await loadHardwareFeatures();
    plan = await api(`/api/move/plan?ns=${encodeURIComponent(wl.ns)}&name=${encodeURIComponent(name)}&cpu=${wl.cpu || 0}&mem=${wl.mem_mb || 0}`);
    if (sequence !== HOST_MOVE_PICKER) return;
    // An answer with no hosts in it is an error to show, not a spinner to leave running.
    if (!Array.isArray(plan?.candidates)) throw new Error("the plan came back without any hosts to compare");
  } catch (e) { if (sequence === HOST_MOVE_PICKER) $("#mbody").innerHTML = `<div class="empty"><b>A placement plan could not be worked out</b><br><span class="dim small">${esc(e.message)}</span></div>`; return; }
  const here = plan.current || wl.node || (wl.nodes && wl.nodes[0]) || "";
  const req = plan.requirements || { devices: [], resources: {}, labels: {} };
  const viable = (plan.candidates || []).filter(n => n.ok && !n.current);
  window.__moveStranded = !viable.length;
  $("#mbody").innerHTML = `
    <div class="between"><p class="muted small">Currently on <b>${esc(here || "no host")}</b>. Pick a host for a full capacity review. These initial estimates are not an admission check.</p>
      ${plan.recommended ? `<button class="btn pri sm" onclick="pickMove(${jsq(plan.recommended)})">Best fit · ${esc(plan.recommended.replace("harvester-", ""))}</button>` : ""}</div>
    ${(req.devices || []).length || (req.features || []).length || Object.keys(req.labels || {}).length ? `<div class="constraintbar"><b>Required</b>
      ${(req.devices || []).map(d => UI.chip(d.label, "hw")).join("")}
      ${(req.features || []).filter(id => !(req.devices || []).some(d => d.id === id)).map(id => UI.chip(hardwareName(id), "hw")).join("")}
      ${Object.entries(req.labels || {}).map(([k,v]) => UI.chip(`${k}=${v}`, "hw")).join("")}</div>` : '<div class="constraintbar"><span class="dim small">No host-specific hardware requirements.</span></div>'}
    ${!viable.length ? `${UI.callout("bad", { html: `No failover destination.` }, `No other ready, schedulable host satisfies every dependency shown above. If <span class="mono">${esc(here)}</span> stops, <b>${esc(name)}</b> will remain Pending and will not come back up until compatible hardware is available.`, { attrs: 'style="margin-top:12px"' })}` : ""}

    <div class="movegrid" style="margin-top:16px">
      ${plan.candidates.map(n => {
        const cur = n.current, blocked = !n.ok;
        return `<div class="movecard ${cur ? "cur" : ""} ${blocked ? "blocked" : ""}"
             ${blocked || cur ? "" : `onclick="pickMove(${jsq(n.name)})"`} data-node="${esc(n.name)}">
          <div class="between">
            <div class="row" style="gap:9px">
              <div class="av n2">${esc(n.name.replace(/[^0-9a-z]/gi, "").slice(-2).toUpperCase())}</div>
              <div><div style="font-weight:670">${esc(n.name)}</div>
                <div class="dim xs">${n.pods_wl} app copies · score ${n.score}</div></div>
            </div>
            ${cur ? UI.chip("current", "neutral")
              : blocked ? UI.chip("blocked", "bad")
              : UI.chip("review needed", "neutral")}
          </div>
          <div style="margin-top:12px">
            <div class="between"><span class="dim xs">Preliminary CPU estimate</span>
              <span class="mono small">${n.cpu_after}%</span></div>
            ${meter(n.cpu_after, 'style="margin:5px 0 10px"')}
            <div class="between"><span class="dim xs">Preliminary RAM estimate</span>
              <span class="mono small">${n.mem_after}%</span></div>
            ${meter(n.mem_after, 'style="margin:5px 0 0"')}
          </div>
          <div style="margin-top:11px">
             ${hardwareTags(Object.entries(n.hardware || {}).filter(([,v]) => v).map(([id]) => id))}
             ${n.temp_c != null ? UI.chip(`${n.temp_c}°C`, "neutral") : ""}
             ${blocked ? `<div class="blockreasons">${n.why.map(x => `<span>${esc(x)}</span>`).join("")}</div>` : ""}
          </div>
        </div>`;
      }).join("")}
    </div>

    <div class="row" style="margin-top:16px"><label class="switch"><input type="checkbox" id="mv_unpin">
      Clear preference — let the scheduler choose${!viable.length ? " (no failover host currently exists)" : ""}</label>
      <label class="switch"><input type="checkbox" id="mv_pin"> Hard pin ${tip("A hard pin guarantees this host but prevents automatic failover. The default is a preference so the scheduler may recover elsewhere if the host fails.")}</label></div>

    ${UI.actions(`<button class="btn pri" id="mv_go" onclick="doMoveNow(${jsq(wl.ns)},${jsq(name)})" disabled>Review move</button>
      <button data-dialog-dismiss="true" class="btn" onclick="modalBack()">Cancel</button>`)}
    <div class="note" style="margin-top:14px">Expect downtime: the old pod must stop before its ReadWriteOnce volume can attach elsewhere. Termination or reattachment may fail. ${tip("Eligible hosts meet the app’s hardware, device, label, readiness and scheduling requirements.")}</div>`;

  const un = $("#mv_unpin");
  un.onchange = () => {
    invalidateHostMove();
    $("#mv_go").disabled = !un.checked && !window.__moveTarget;
    if (un.checked) $$(".movecard").forEach(c => c.classList.remove("sel"));
  };
  $("#mv_pin").onchange = invalidateHostMove;
  window.__moveTarget = null;
};

window.pickMove = node => {
  invalidateHostMove();
  window.__moveTarget = node;
  $("#mv_unpin").checked = false;
  $$(".movecard").forEach(c => c.classList.toggle("sel", c.dataset.node === node));
  $("#mv_go").disabled = false;
};

window.doMoveNow = async (ns, name) => {
  const unpin = $("#mv_unpin").checked;
  const node = unpin ? null : window.__moveTarget;
  if (!unpin && !node) return toast("Pick a host first", "bad");
  await window.hostMoveReview({ ns, name, node, pin: !unpin && $("#mv_pin").checked });
};

window.hostMoveReview = body => { if (!HOST_MOVE.busy) ++HOST_MOVE_PICKER; return HOST_MOVE.open(JSON.parse(JSON.stringify(body))); };

window.confirmHostMove = async () => {
  if (!HOST_MOVE.ready()) return toast("Review the move and acknowledge its warnings first", "bad");
  await HOST_MOVE.apply();
};

/* "get everything off this host" — drain without the reboot */
function impactRows(impact) {
  return (impact.workloads || []).map(w => `<div class="dependency-row ${w.stranded ? "stranded" : ""}">
    <div><b>${esc(w.name)}</b><div class="dim xs">${esc(w.ns)} ${hardwareTags(w.hardware || [])}</div></div>
    <div style="text-align:right">${w.stranded ? UI.chip("will stay down", "bad") : UI.chip(`${w.eligible.length} destination${w.eligible.length === 1 ? "" : "s"}`, "ok")}
      <div class="dim xs">${w.stranded ? (w.blocked || []).map(x => `${esc(x.name.replace("harvester-", ""))}: ${esc((x.why || []).join(", "))}`).join(" · ") : esc(w.eligible.map(x => x.replace("harvester-", "")).join(", "))}</div></div></div>`).join("");
}
/* Host actions → Apps (#375): what breaks first, then where the rest land
   and whether those hosts have room. Kubernetes decides where each app goes;
   the landing is homestead_place's estimate (the eligible host with the most
   memory free, largest app first). */
const shortHostName = name => String(name || "").replace(/^harvester-/, "");
const cores = m => (m / 1000).toFixed(m >= 10000 ? 0 : 1);
const gib = b => (b / 1024 ** 3).toFixed(b >= 10 * 1024 ** 3 ? 0 : 1);

function hostAppsHtml(impact) {
  const workloads = impact.workloads || [], stranded = workloads.filter(w => w.stranded);
  const hosts = impact.hosts || [], moving = workloads.length - stranded.length;
  if (!workloads.length) return '<div class="ui-empty">No apps run on this host.</div>';
  const strip = `<div class="hostapps-strip">
    ${stranded.length ? UI.chip(`${stranded.length} stay down`, "bad") : ""}
    ${moving ? UI.chip(`${moving} move`, "ok") : ""}</div>`;
  const down = stranded.length ? UI.section("Stays down until this host is back", `<div class="hostapps-down">${stranded.map(w => `
      <div class="hostapps-app"><b>${esc(w.name)}</b> <span class="dim xs">${esc(w.ns)}</span>
        <div class="dim xs">${(w.blocked || []).map(x => `${esc(shortHostName(x.name))}: ${esc((x.why || []).join(", ") || "not suitable")}`).join(" · ") || "no other host is ready"}</div></div>`).join("")}</div>`) : "";
  const landing = hosts.filter(h => (h.apps || []).length);
  const pct = (part, whole) => whole ? Math.round(part / whole * 100) : 0;
  const dest = h => {
    const unknown = !h.cpu_m || !h.memory;
    const line = (label, now, after, total, fmt, unit) => total
      ? `<div class="hostapps-meter"><span class="dim xs">${label}</span>${UI.meter({ now: pct(now, total), after: pct(after, total), label: `${label} on ${h.name}` })}
          <span class="mono xs">${fmt(now)} → ${fmt(after)} of ${fmt(total)} ${unit}</span></div>` : "";
    return `<div class="hostapps-host">
      <div class="hostapps-head"><b>${esc(shortHostName(h.name))}</b><span class="dim small">${h.apps.length} app${h.apps.length === 1 ? "" : "s"} land here</span>
        ${(h.over || []).length ? UI.chip(`over its ${h.over.join(" and ")}`, "bad") : ""}</div>
      ${line("CPU requested", h.cpu_now, h.cpu_after, h.cpu_m, cores, "cores")}
      ${line("Memory requested", h.memory_now, h.memory_after, h.memory, gib, "GiB")}
      ${unknown ? '<div class="dim xs">Its allocatable CPU or memory is not known, so whether it has room is not either.</div>' : ""}
      ${h.unrequested ? `<div class="dim xs">${h.unrequested} of these ask for no CPU or memory, so the real total may be higher.</div>` : ""}
      ${UI.more(`Show the ${h.apps.length} app${h.apps.length === 1 ? "" : "s"}`, `<div class="hostapps-names">${h.apps.map(a => UI.chip(a.split("/")[1], "neutral")).join("")}</div>`)}</div>`;
  };
  const none = hosts.filter(h => !(h.apps || []).length && (h.why || []).length);
  return `<div class="ui-stack">${strip}${down}
    ${landing.length ? UI.section("Destinations", `<div class="hostapps-hosts">${landing.map(dest).join("")}</div>
      <div class="dim xs">Kubernetes picks the host when it happens. This is the likely spread: each app on the host it may run on with the most memory free.</div>`) : ""}
    ${none.length ? UI.section("Not a destination", none.map(h => `<div class="hostapps-app"><b>${esc(shortHostName(h.name))}</b> <span class="dim xs">${esc(h.why.join(", "))}</span></div>`).join("")) : ""}
    ${UI.more(`Every app (${workloads.length})`, `<div class="dependency-list">${impactRows(impact)}</div>`)}</div>`;
}
window.hostAppsHtml = hostAppsHtml;

window.evacuateNode = async node => {
  modal("Evacuate · " + node, '<div class="empty"><span class="spin2"></span>checking app dependencies…</div>', true);
  let impact;
  try { await loadHardwareFeatures(); impact = await api(`/api/node/impact?node=${encodeURIComponent(node)}`); }
  catch (e) { $("#mbody").innerHTML = `<div class="empty"><b>The dependencies could not be checked</b><br><span class="dim small">${esc(e.message)}</span></div>`; return; }
  window.__nodeImpact = impact;
  const wls = impact.workloads || [];
  modal("Evacuate · " + node, `
    <p class="muted small">Move every app currently on <b>${esc(node)}</b> elsewhere.
    The host is cordoned first so nothing lands back on it.</p>
    ${wls.length ? `<div class="dependency-list" style="margin-top:14px">${impactRows(impact)}</div>`
      : '<div class="empty">Nothing of yours is running here.</div>'}
    ${impact.stranded.length ? `${UI.callout("bad", { html: `${impact.stranded.length} app${impact.stranded.length === 1 ? " has" : "s have"} no compatible destination.` }, `Moving them off stops their current copies, and Kubernetes leaves them waiting until a host with all the hardware they need returns.`, { attrs: 'style="margin-top:14px"' })}
      <label class="switch dependency-confirm"><input type="checkbox" id="ev_allow" onchange="document.getElementById('ev_go').disabled=!this.checked"> Leave ${impact.stranded.map(w => esc(w.name)).join(", ")} down for now</label>` : ""}
    ${wls.length ? `${UI.actions(`<button class="btn pri" id="ev_go" onclick="doEvacuate(${jsq(node)})" ${impact.stranded.length ? "disabled" : ""}>Evacuate ${plural(wls.length, "app")}</button>
      <button data-dialog-dismiss="true" class="btn" onclick="closeModal()">Cancel</button>`)}
    <div class="note" style="margin-top:14px">Each app restarts on another host. With
    ReadWriteOnce volumes they restart one at a time, so this is not instant.</div>` : ""}`);
};
window.doEvacuate = async node => {
  try {
    const allow = !!$("#ev_allow")?.checked;
    await api("/api/node/cordon", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ node, cordon: true }) });
    const r = await api("/api/node/drain", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ node, allow_stranded: allow }) });
    toast(`Cordoned and evicted ${plural(r.evicted.length, "pod")}`, "ok");
    closeModal(); setTimeout(() => refresh(true), 1800);
  } catch (e) { toast(e.message, "bad"); }
};
