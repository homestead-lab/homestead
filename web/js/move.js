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

let HOST_MOVE_REVIEW = null;
let HOST_MOVE_SEQUENCE = 0;
function invalidateHostMove() { HOST_MOVE_REVIEW = null; ++HOST_MOVE_SEQUENCE; }

window.moveWorkload = async (name, ns) => {
  invalidateHostMove();
  const sequence = HOST_MOVE_SEQUENCE;
  const wl = Object.assign(findWl(name), ns ? { ns } : {});
  childModal("Move · " + name, '<div class="empty"><span class="spin2"></span>checking capacity and hardware constraints…</div>', true);
  let plan;
  try {
    await loadHardwareFeatures();
    plan = await api(`/api/move/plan?ns=${encodeURIComponent(wl.ns)}&name=${encodeURIComponent(name)}&cpu=${wl.cpu || 0}&mem=${wl.mem_mb || 0}`);
    if (sequence !== HOST_MOVE_SEQUENCE) return;
    // An answer with no hosts in it is an error to show, not a spinner to leave running.
    if (!Array.isArray(plan?.candidates)) throw new Error("the plan came back without any hosts to compare");
  } catch (e) { if (sequence === HOST_MOVE_SEQUENCE) $("#mbody").innerHTML = `<div class="empty"><b>Could not build a placement plan</b><br><span class="dim small">${esc(e.message)}</span></div>`; return; }
  const here = plan.current || wl.node || (wl.nodes && wl.nodes[0]) || "";
  const req = plan.requirements || { devices: [], resources: {}, labels: {} };
  const viable = (plan.candidates || []).filter(n => n.ok && !n.current);
  window.__moveStranded = !viable.length;
  $("#mbody").innerHTML = `
    <div class="between"><p class="muted small">Currently on <b>${esc(here || "no node")}</b>. Pick a host for a full capacity review. These initial estimates are not an admission check.</p>
      ${plan.recommended ? `<button class="btn pri sm" onclick="pickMove('${esc(plan.recommended)}')">Best fit · ${esc(plan.recommended.replace("harvester-", ""))}</button>` : ""}</div>
    ${(req.devices || []).length || (req.features || []).length || Object.keys(req.labels || {}).length ? `<div class="constraintbar"><b>Required</b>
      ${(req.devices || []).map(d => `<span class="tag hw">${esc(d.label)}</span>`).join("")}
      ${(req.features || []).filter(id => !(req.devices || []).some(d => d.id === id)).map(id => `<span class="tag hw">${esc(hardwareName(id))}</span>`).join("")}
      ${Object.entries(req.labels || {}).map(([k,v]) => `<span class="tag hw">${esc(k)}=${esc(v)}</span>`).join("")}</div>` : '<div class="constraintbar"><span class="dim small">No host-specific hardware requirements.</span></div>'}
    ${!viable.length ? `<div class="note dependency-danger" style="margin-top:12px"><b>No failover destination.</b> No other ready, schedulable host satisfies every dependency shown above. If <span class="mono">${esc(here)}</span> stops, <b>${esc(name)}</b> will remain Pending and will not come back up until compatible hardware is available.</div>` : ""}

    <div class="movegrid" style="margin-top:16px">
      ${plan.candidates.map(n => {
        const cur = n.current, blocked = !n.ok;
        return `<div class="movecard ${cur ? "cur" : ""} ${blocked ? "blocked" : ""}"
             ${blocked || cur ? "" : `onclick="pickMove('${esc(n.name)}')"`} data-node="${esc(n.name)}">
          <div class="between">
            <div class="row" style="gap:9px">
              <div class="av n2">${esc(n.name.replace(/[^0-9a-z]/gi, "").slice(-2).toUpperCase())}</div>
              <div><div style="font-weight:670">${esc(n.name)}</div>
                <div class="dim xs">${n.pods_wl} workload${n.pods_wl === 1 ? "" : "s"} · score ${n.score}</div></div>
            </div>
            ${cur ? '<span class="pill low">current</span>'
              : blocked ? '<span class="pill crit">blocked</span>'
              : '<span class="pill low">review needed</span>'}
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
             ${n.temp_c != null ? `<span class="tag">${n.temp_c}°C</span>` : ""}
             ${blocked ? `<div class="blockreasons">${n.why.map(x => `<span>${esc(x)}</span>`).join("")}</div>` : ""}
          </div>
        </div>`;
      }).join("")}
    </div>

    <div class="row" style="margin-top:16px"><label class="switch"><input type="checkbox" id="mv_unpin">
      Clear preference — let the scheduler choose${!viable.length ? " (no failover host currently exists)" : ""}</label>
      <label class="switch"><input type="checkbox" id="mv_pin"> Hard pin ${tip("A hard pin guarantees this host but prevents automatic failover. The default is a preference so the scheduler may recover elsewhere if the node fails.")}</label></div>

    <div class="row" style="margin-top:6px">
      <button class="btn pri" id="mv_go" onclick="doMoveNow('${esc(wl.ns)}','${esc(name)}')" disabled>Review move</button>
      <button class="btn" onclick="modalBack()">Cancel</button>
    </div>
    <div class="note" style="margin-top:14px">Eligible hosts already account for every configured hardware feature, passthrough path, USB ID, node label, advertised device resource, readiness and cordon. This is a stop-then-start, not a live move. A
    ReadWriteOnce volume can only attach to one node at a time, so the old pod must fully
    terminate before the new one starts. Expect downtime; termination and storage reattachment can take longer or fail.</div>`;

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
  if (!unpin && !node) return toast("pick a host first", "bad");
  await window.hostMoveReview({ ns, name, node, pin: !unpin && $("#mv_pin").checked });
};

window.hostMoveReview = async body => {
  if (HOST_MOVE_REVIEW?.submitting) return;
  invalidateHostMove();
  const sequence = HOST_MOVE_SEQUENCE, config = JSON.parse(JSON.stringify(body));
  try {
    const review = await api("/api/move/preview", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(config) });
    if (sequence !== HOST_MOVE_SEQUENCE) return;
    if (!review.capacity || !review.capacity_token || !review.capacity.move) throw new Error("Move review unavailable; refresh before moving.");
    HOST_MOVE_REVIEW = { config, ...review, submitting: false };
    childModal("Review move · " + config.name, `<p>Destination: <b>${esc(review.capacity.move.node || "scheduler chooses")}</b> · ${esc(review.capacity.move.mode)}.</p>
      <div class="note warn">This saves a Recreate strategy: all containers in this workload restart after the old pods stop. Clearing a preference can also restart them. Volumes must detach and reattach; this is not live migration.</div>
      ${deployCapacityHtml(review.capacity)}
      ${review.capacity.move.target ? `<details${review.capacity.move.target.blocked ? " open" : ""}><summary>Selected host: full desired replica count</summary><p class="small muted">The selected host must fit the desired replicas even for a preference. This check does not turn a preference into a hard pin.</p>${deployCapacityHtml(review.capacity.move.target)}</details>` : ""}
      ${!review.capacity.blocked ? `<label class="switch"><input type="checkbox" id="hostMoveConfirm"> Proceed despite capacity warnings — I accept the downtime, data, placement and memory risks</label>` : ""}
      <div class="modalactions"><button class="btn" onclick="invalidateHostMove();modalBack()">Back</button><button class="btn pri" id="hostMoveGo" ${review.capacity.blocked ? "disabled" : ""} onclick="confirmHostMove()">Apply reviewed placement</button></div>`, true);
  } catch (e) { toast(e.message, "bad"); }
};

window.confirmHostMove = async () => {
  const review = HOST_MOVE_REVIEW;
  if (!review || review.submitting || review.capacity.blocked || !$("#hostMoveConfirm")?.checked)
    return toast("Review the move and acknowledge its warnings first", "bad");
  review.submitting = true;
  const button = $("#hostMoveGo");
  button.disabled = true; button.textContent = "Applying placement…";
  try {
    await api("/api/move", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ ...review.config, capacity_token: review.capacity_token, confirm_capacity: true }) });
    invalidateHostMove();
    toast(`${review.config.name}: placement saved; watch rollout progress in Recent jobs`, "ok");
    closeModal();
    setTimeout(() => refresh(true), 1500);
  } catch (e) {
    invalidateHostMove();
    toast(e.message, "bad"); button.disabled = false; button.textContent = "Review again";
    button.onclick = () => window.hostMoveReview(review.config);
  }
};

/* "get everything off this host" — drain without the reboot */
function impactRows(impact) {
  return (impact.workloads || []).map(w => `<div class="dependency-row ${w.stranded ? "stranded" : ""}">
    <div><b>${esc(w.name)}</b><div class="dim xs">${esc(w.ns)} ${hardwareTags(w.hardware || [])}</div></div>
    <div style="text-align:right">${w.stranded ? '<span class="pill crit">will stay down</span>' : `<span class="pill ok">${w.eligible.length} destination${w.eligible.length === 1 ? "" : "s"}</span>`}
      <div class="dim xs">${w.stranded ? (w.blocked || []).map(x => `${esc(x.name.replace("harvester-", ""))}: ${esc((x.why || []).join(", "))}`).join(" · ") : esc(w.eligible.map(x => x.replace("harvester-", "")).join(", "))}</div></div></div>`).join("");
}
window.evacuateNode = async node => {
  modal("Evacuate · " + node, '<div class="empty"><span class="spin2"></span>checking workload dependencies…</div>', true);
  let impact;
  try { await loadHardwareFeatures(); impact = await api(`/api/node/impact?node=${encodeURIComponent(node)}`); }
  catch (e) { $("#mbody").innerHTML = `<div class="empty"><b>Could not check dependencies</b><br><span class="dim small">${esc(e.message)}</span></div>`; return; }
  window.__nodeImpact = impact;
  const wls = impact.workloads || [];
  modal("Evacuate · " + node, `
    <p class="muted small">Move every workload currently on <b>${esc(node)}</b> elsewhere.
    The host is cordoned first so nothing lands back on it.</p>
    ${wls.length ? `<div class="dependency-list" style="margin-top:14px">${impactRows(impact)}</div>`
      : '<div class="empty">Nothing of yours is running here.</div>'}
    ${impact.stranded.length ? `<div class="note dependency-danger" style="margin-top:14px"><b>${impact.stranded.length} workload${impact.stranded.length === 1 ? " has" : "s have"} no compatible destination.</b> Evicting them stops their current pods; Kubernetes will leave them Pending until a host with every required hardware feature returns.</div>
      <label class="switch dependency-confirm"><input type="checkbox" id="ev_allow" onchange="document.getElementById('ev_go').disabled=!this.checked"> I understand ${impact.stranded.map(w => esc(w.name)).join(", ")} will not come back up now</label>` : ""}
    ${wls.length ? `<div class="row" style="margin-top:18px">
      <button class="btn pri" id="ev_go" onclick="doEvacuate('${esc(node)}')" ${impact.stranded.length ? "disabled" : ""}>Evacuate ${wls.length} workload(s)</button>
      <button class="btn" onclick="closeModal()">Cancel</button></div>
    <div class="note" style="margin-top:14px">Each workload restarts on another host. With
    ReadWriteOnce volumes they restart one at a time, so this is not instant.</div>` : ""}`);
};
window.doEvacuate = async node => {
  try {
    const allow = !!$("#ev_allow")?.checked;
    await api("/api/node/cordon", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ node, cordon: true }) });
    const r = await api("/api/node/drain", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ node, allow_stranded: allow }) });
    toast(`cordoned and evicted ${r.evicted.length} pod(s)`, "ok");
    closeModal(); setTimeout(() => refresh(true), 1800);
  } catch (e) { toast(e.message, "bad"); }
};
