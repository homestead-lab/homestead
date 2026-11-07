/* Homestead Containers: Balance hosts, reservations and priority tiers (#911). One of the scripts views-workloads.js was split into; they share the page's globals and load in the order index.html lists them. */
/* Balancing hosts, on demand: containers by CPU and memory - each move a
   restart - then volume copies by space - none restart. Each step has its own
   plan, a switch to leave that part out, and a tickbox per container or app
   that plans it again without it; the review starts what is left. A viewer
   sees the plan read-only (#1084): every switch and tick drawn disabled, and
   the handlers refuse as well. */
const BALANCE = {};
const balanceReadOnly = () => typeof can === "function" && !can("operator");
// Disabled, and described by the dialog's one line saying who can change it.
const balanceOff = () => balanceReadOnly() ? ' disabled aria-describedby="balReadOnly"' : "";
// The lines that tell a person what to do; a viewer gets a plain description.
const balanceHint = text => `<p class="ui-help">${esc(balanceReadOnly() ? "The plan as Homestead suggests it." : text)}</p>`;
const balanceReset = () => {
  BALANCE.containers = { on: true, exclude: new Set(), plan: null };
  BALANCE.volumes = { on: true, exclude: new Set(), plan: null, follow: true };
};
window.balanceHosts = async (start = "containers") => {
  balanceReset();
  const loading = text => `<div class="empty"><span class="spin2"></span> ${esc(text)}</div>`;
  modal("Balance hosts", UI.sectionForm("balance", [
    { key: "containers", title: "Containers", html: `<div id="balContainers">${loading("Working out container moves…")}</div>` },
    { key: "volumes", title: "Volume copies", html: `<div id="balVolumes">${loading("Working out volume copy moves…")}</div>` },
    { key: "review", title: "Review", html: '<div id="balReview"></div>' },
  ], UI.button("Start balancing", "balanceStart()", { kind: "pri", id: "balanceGo", attrs: 'data-need="operator"' }),
  { noticeHtml: balanceReadOnly() ? '<p class="ui-help" id="balReadOnly">Operators can change and apply this plan.</p>' : "" }), true);
  if (start !== "containers") UI.selectSection("balance", start);
  balanceReview();
  await balancePlan("containers");
  await balancePlan("volumes");
};
window.containerRebalance = () => balanceHosts("containers");
window.volumeRebalance = () => balanceHosts("volumes");

async function balancePlan(kind) {
  const b = BALANCE[kind], pane = $(kind === "containers" ? "#balContainers" : "#balVolumes");
  if (!pane) return;
  const path = kind === "containers" ? "/api/workloads/rebalance/plan" : "/api/longhorn/rebalance/plan";
  // Volume copies follow the containers moved, so each reads locally on its new host.
  const follow = kind === "volumes" && b.follow ? balanceMoves("containers").map(m => `${m.id}:${m.to}`).join(",") : "";
  try { b.plan = await api(`${path}?${new URLSearchParams({ exclude: [...b.exclude].join(","), ...(follow ? { follow } : {}) })}`); }
  catch (e) { b.plan = null; pane.innerHTML = UI.callout("bad", "Could not work out a plan", esc(e.message)); balanceReview(); return; }
  if (!$(kind === "containers" ? "#balContainers" : "#balVolumes")) return;
  pane.innerHTML = kind === "containers" ? balanceContainersHtml(b) : balanceVolumesHtml(b);
  balanceReview();
  window.applyRole?.();
}

const balanceSwitch = (kind, b, label) => `<label class="switch balance-switch"><input type="checkbox" ${b.on ? "checked" : ""}${balanceOff()}
  onchange="balanceToggle(${jsq(kind)},this.checked)"> ${esc(label)}</label>`;
const balanceTick = (kind, id, i, off) => `<label class="rb-app"><input type="checkbox" id="bal_${kind}_${i}" ${off ? "" : "checked"}${balanceOff()}
  onchange="balanceItem(${jsq(kind)},${jsq(id)},this.checked)"> <span class="mono">${esc(id)}</span></label>`;
const balanceSkipped = rows => rows.length ? UI.more(`Not moved · ${rows.length}`,
  `<ul class="ui-list">${rows.map(([id, why]) => `<li><span class="mono">${esc(id)}</span>: ${esc(why)}</li>`).join("")}</ul>`) : "";

/* The plan works from metrics-server's figures as Homestead last read them
   (#928): say when they are missing, still being read after a start, or
   old. One callout. */
function balanceMetrics(plan) {
  const m = plan.metrics_state;
  if (plan.metrics === false && m?.state === "waiting")
    return UI.callout("warn", "Still reading CPU and memory", "Homestead has just started and has not had metrics-server's first figures yet, so the plan may be off. Open it again in a few seconds.");
  if (plan.metrics === false)
    return UI.callout("warn", "Usage is partly unknown", esc(`A host or its apps report no live CPU and memory${m?.detail ? ` (${m.detail})` : ""}, so the plan may be off. Check metrics-server.`));
  if (m?.state === "stale")
    return UI.callout("warn", `CPU and memory from ${fmtAgo(m.age_s)}`, esc(`metrics-server has not answered since then${m.detail ? ` (${m.detail})` : ""}, so the plan uses its last figures and may be off.`));
  return "";
}

const BALANCE_LEAVES_PREFERENCE = "Each one then prefers its new host, and that stays until cleared in Edit → Placement, which restarts it.";
function balanceContainersHtml(b) {
  const plan = b.plan, moves = plan.moves || [];
  const meter = (before, after, label) => `${UI.meter({ now: before, after: after !== before ? after : null, warnAt: 80, label })}
    <span class="sub">${esc(label)} ${before}%${after !== before ? ` → ${after}%` : ""}</span>`;
  const hosts = UI.table([{ label: "Host" }, { label: "CPU", className: "grow" }, { label: "Memory", className: "grow" }], (plan.hosts || []).map(h => [
    `<span class="mono">${esc(h.name)}</span>${h.takes ? "" : ` ${UI.chip("takes no new containers", "warn")}`}`,
    meter(h.cpu_before, b.on ? h.cpu_after : h.cpu_before, "CPU"), meter(h.mem_before, b.on ? h.mem_after : h.mem_before, "Memory")]));
  const rows = (plan.apps || []).map((id, i) => {
    const m = moves.find(x => x.id === id), off = b.exclude.has(id);
    return [balanceTick("containers", id, i, off), off || !m ? '<span class="ui-help">stays where it is</span>'
      : `<span class="mono">${esc(m.from)} → ${esc(m.to)}</span> <span class="sub">${esc(m.cpu_m)}m CPU · ${esc(m.mem_gb)} GiB${m.near ? " · its volumes have a copy there" : ""}${m.note ? ` · ${esc(m.note)}` : ""}</span>`];
  });
  return [
    balanceSwitch("containers", b, "Balance containers"),
    UI.lead(!b.on ? "Containers are left where they are."
      : moves.length ? `Moves ${moves.length} container${moves.length === 1 ? "" : "s"} so hosts carry similar CPU and memory. Each one restarts once, on its new host. ${BALANCE_LEAVES_PREFERENCE}`
      : plan.quiet === "even" ? "The hosts already carry similar CPU and memory; nothing is worth a restart."
      : plan.quiet === "nothing-movable" ? "Nothing here can move: each container is pinned, left out, only an admin can move it, or it has no other host it may run on. See Not moved below."
      : "No single move evens the hosts out by enough to be worth a restart: the containers that could move are too small to shift CPU or memory much."),
    balanceMetrics(plan),
    UI.section("Hosts", hosts),
    b.on && rows.length ? UI.section(`Containers · ${rows.length}`, balanceHint("Untick a container to leave it where it is; the moves are worked out again without it.")
      + UI.table([{ label: "Container" }, { label: "Moves", className: "grow" }], rows)) : "",
    balanceSkipped((plan.skipped || []).map(s => [s.id, s.why])),
  ].join("");
}

function balanceVolumesHtml(b) {
  const plan = b.plan, moves = plan.moves || [], gb = n => `${n} GiB`;
  const hosts = UI.table([{ label: "Host" }, { label: "Copies held", className: "grow" }], (plan.hosts || []).map(h => {
    const after = b.on ? h.after_gb : h.before_gb;
    return [`<span class="mono">${esc(h.name)}</span>${h.takes ? "" : ` ${UI.chip("takes no new copies", "warn")}`}`,
      `${UI.meter({ now: h.capacity_gb ? h.before_gb / h.capacity_gb * 100 : 0, after: h.capacity_gb && after !== h.before_gb ? after / h.capacity_gb * 100 : null, label: `${h.name} copies` })}
       <span class="sub">${esc(gb(h.before_gb))}${after !== h.before_gb ? ` → ${esc(gb(after))}` : ""} of ${esc(gb(h.capacity_gb))}</span>`];
  }));
  const rows = (plan.apps || []).map((app, i) => {
    const mine = moves.filter(m => m.app === app), off = b.exclude.has(app);
    return [balanceTick("volumes", app, i, off), off ? '<span class="ui-help">left where they are</span>'
      : mine.map(m => `<div><span class="mono">${esc(m.claim)}</span> <span class="sub">${esc(gb(m.size_gb))} · ${esc(m.from)} → ${esc(m.to)}${m.follows ? " · follows its container there" : ""}</span></div>`).join("")];
  });
  return [
    balanceSwitch("volumes", b, "Balance volume copies"),
    b.on && balanceMoves("containers").length ? `<label class="switch balance-switch"><input type="checkbox" id="balFollow" ${b.follow ? "checked" : ""}${balanceOff()}
      onchange="balanceFollow(this.checked)"> Move a copy of each moved container's volumes to its new host</label>
      <p class="ui-help">So it reads its data locally there. Off, its volumes stay where they are and it reads them over the network.</p>` : "",
    UI.lead(!b.on ? "Volume copies are left where they are."
      : moves.length ? `Moves ${moves.length} cop${moves.length === 1 ? "y" : "ies"} so hosts hold similar amounts${moves.some(m => m.follows) ? ", and so each container moved has a copy of its volumes on its new host" : ""}. Nothing restarts: each copy is built on its new host before the old one is removed.`
      : (plan.closed || []).length ? `Nothing can move now: ${plan.closed.map(esc).join(", ")} ${plan.closed.length === 1 ? "takes" : "take"} no new copies - cordoned, not Ready, or with scheduling off in Longhorn.`
      : "The hosts are as even as moving copies can make them."),
    UI.section("Hosts", hosts),
    b.on && rows.length ? UI.section(`Apps · ${rows.length}`, balanceHint("Untick an app to leave its volumes where they are; the moves are worked out again without it.")
      + UI.table([{ label: "App" }, { label: "Copies it moves", className: "grow" }], rows)) : "",
    balanceSkipped((plan.skipped || []).map(s => [s.claim, s.why])),
  ].join("");
}

const balanceMoves = kind => BALANCE[kind]?.on && BALANCE[kind].plan ? (BALANCE[kind].plan.moves || []) : [];
function balanceReview() {
  const pane = $("#balReview");
  if (!pane) return;
  const containers = balanceMoves("containers"), volumes = balanceMoves("volumes");
  const pending = ["containers", "volumes"].some(k => BALANCE[k].on && !BALANCE[k].plan);
  pane.innerHTML = pending ? '<div class="empty"><span class="spin2"></span> Waiting for the plans…</div>' : [
    UI.lead(!(containers.length || volumes.length) ? "Nothing to balance with these choices."
      : balanceReadOnly() ? "The plan as Homestead suggests it." : "Starts what is below, as jobs you can follow and stop in Jobs."),
    UI.facts([
      ["Containers", !BALANCE.containers.on ? "left where they are" : containers.length
        ? `${containers.length} move${containers.length === 1 ? "" : "s"} · each restarts once` : "nothing worth moving"],
      ["Volume copies", !BALANCE.volumes.on ? "left where they are" : volumes.length
        ? `${volumes.length} cop${volumes.length === 1 ? "y" : "ies"} · nothing restarts` : "nothing to move"]]),
    containers.length ? '<p class="ui-help">Containers move one at a time, each with the same capacity check as a manual move; if one does not start on its new host within 10 minutes, the job stops there and the rest are not moved; once that container runs, review again to move the rest. Volume copies are built one at a time; stopping drops the copy being built.</p>' : "",
    containers.length ? UI.ack("balanceRestart", containers.length === 1 ? "Restart it now" : "Restart them now", { attrs: balanceOff() }) : "",
  ].join("");
  const go = $("#balanceGo");
  if (go) go.disabled = pending || !(containers.length || volumes.length);
}
// The volume plan follows the container moves: worked out again when they change.
const balanceReplanVolumes = () => { BALANCE.volumes.plan = null; balanceReview(); return balancePlan("volumes"); };
window.balanceFollow = on => { if (balanceReadOnly()) return; BALANCE.volumes.follow = on; balanceReplanVolumes(); };
window.balanceToggle = (kind, on) => {
  if (balanceReadOnly()) return;
  BALANCE[kind].on = on;
  const pane = $(kind === "containers" ? "#balContainers" : "#balVolumes");
  if (pane && BALANCE[kind].plan) pane.innerHTML = kind === "containers" ? balanceContainersHtml(BALANCE[kind]) : balanceVolumesHtml(BALANCE[kind]);
  balanceReview();
  if (kind === "containers") balanceReplanVolumes();
};
window.balanceItem = async (kind, id, on) => {
  if (balanceReadOnly()) return;
  on ? BALANCE[kind].exclude.delete(id) : BALANCE[kind].exclude.add(id);
  BALANCE[kind].plan = null; balanceReview();
  await balancePlan(kind);
  if (kind === "containers") balanceReplanVolumes();
};
window.balanceStart = async () => {
  const containers = balanceMoves("containers"), volumes = balanceMoves("volumes");
  if (!containers.length && !volumes.length) return;
  if (containers.length && !$("#balanceRestart")?.checked) return toast("Tick Restart to confirm the container restarts", "bad");
  const button = $("#balanceGo");
  if (button) { button.disabled = true; button.textContent = "Starting…"; }
  const started = [];
  try {
    if (containers.length) {
      await api("/api/workloads/rebalance", { method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ exclude: [...BALANCE.containers.exclude], review_token: BALANCE.containers.plan.review_token,
          moves: BALANCE.containers.plan.moves, restart: true }) });
      started.push(`${containers.length} container${containers.length === 1 ? "" : "s"}`);
    }
    if (volumes.length) {
      await api("/api/longhorn/rebalance", { method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ exclude: [...BALANCE.volumes.exclude], review_token: BALANCE.volumes.plan.review_token,
          moves: BALANCE.volumes.plan.moves }) });
      started.push(`${volumes.length} volume cop${volumes.length === 1 ? "y" : "ies"}`);
    }
    closeModal(); toast(`Balancing ${started.join(" and ")}; follow it in Jobs`, "ok");
  } catch (e) {
    toast((started.length ? `Started ${started.join(" and ")}, but ` : "") + e.message, "bad");
    if (button) { button.disabled = false; button.textContent = "Start balancing"; }
    if (e.body?.plan) { balancePlan("containers").then(() => balancePlan("volumes")); }
  } finally { window.refreshOperations?.(true); }
};

/* Reservations from a week of use, for many apps at once. The scheduler
   places apps by what they reserve, so apps reserving nothing are ticked;
   ones whose reservation only differs from their use are offered unticked,
   as someone may have set it on purpose. Ticking plans again, so the review
   token covers exactly what is started. */
const RESERVE = { chosen: null, plan: null, asked: 0 };
window.reservationsReview = async () => {
  RESERVE.chosen = null; RESERVE.plan = null;
  modal("Set reservations from use", `<div id="resPlan"><div class="empty"><span class="spin2"></span> Reading a week of use…</div></div>
    ${UI.actions(UI.button("Set reservations", "reservationsStart()", { kind: "pri", id: "reserveGo", attrs: 'data-need="operator" disabled' }), UI.cancel())}`, true);
  await reservationsPlan();
};
async function reservationsPlan() {
  const pane = $("#resPlan");
  if (!pane) return;
  // Each tick plans again; an answer to an earlier tick is dropped.
  const asked = ++RESERVE.asked;
  const query = RESERVE.chosen ? `?${new URLSearchParams({ chosen: [...RESERVE.chosen].join(",") })}` : "";
  let plan;
  try { plan = await api(`/api/workloads/requests/plan${query}`); }
  catch (e) { if (asked === RESERVE.asked) pane.innerHTML = UI.callout("bad", "Could not read the apps' use", esc(e.message)); return; }
  if (asked !== RESERVE.asked || !$("#resPlan")) return;
  RESERVE.plan = plan;
  if (!RESERVE.chosen) RESERVE.chosen = new Set(plan.apps.filter(a => a.chosen).map(a => a.id));
  pane.innerHTML = reservationsHtml(plan);
  const go = $("#reserveGo");
  if (go) go.disabled = !RESERVE.chosen.size || RESERVE.chosen.size > plan.max;
  window.applyRole?.();
}
/* A reservation in Edit's words (editCpuWords, editSpan in views-lifecycle-edit-pod.js).
   What a container reserves now is what it reserves in effect: Kubernetes
   takes a limit as the request where none is set, and the review says so. */
const reserveNow = (c, r) => {
  const value = r === "cpu" ? (c.cpu_now_m ? editCpuWords(c.cpu_now_m) : "none") : (c.memory_now_mi ? reservedMemory(c.memory_now_mi) : "none");
  return (c.from_limit || []).includes(r) ? `${value} (its limit)` : value;
};
function reservationsHtml(plan) {
  const apps = plan.apps || [], chosen = apps.filter(a => RESERVE.chosen.has(a.id));
  const over = chosen.length > plan.max;
  const rows = apps.map((a, i) => [
    `<label class="rb-app"><input type="checkbox" id="res_${i}" ${RESERVE.chosen.has(a.id) ? "checked" : ""}
      onchange="reservationsItem(${jsq(a.id)},this.checked)"> <span class="mono">${esc(a.id)}</span></label>${a.missing ? ` ${UI.chip("reserves nothing", "warn")}` : ""}`,
    a.changes.map(c => `<div>${a.changes.length > 1 ? `<span class="mono">${esc(c.container)}</span> ` : ""}
      <span>${esc(reserveNow(c, "cpu"))} → <b>${esc(editCpuWords(parseInt(c.cpu, 10)))}</b> CPU · ${esc(reserveNow(c, "memory"))} → <b>${esc(reservedMemory(parseInt(c.memory, 10)))}</b></span>
      <span class="sub">usually at or under ${esc(editCpuWords(c.cpu_p95_m))} and ${esc(reservedMemory(c.mem_p95_mi))}, peak ${esc(reservedMemory(c.mem_max_mi))} over ${esc(editSpan(c))}${c.lowers ? " · lowers it" : ""}${c.capped.length ? " · held at its limit" : ""}${(c.from_limit || []).some(r => !c.capped.includes(r)) ? " · no longer reserves its whole limit" : ""}</span></div>`).join("")
      + (a.replicas ? "" : `<div class="sub">Stopped: takes the new reservation when it next starts.</div>`)]);
  const managed = (plan.skipped || []).filter(s => s.yaml), other = (plan.skipped || []).filter(s => !s.yaml);
  return [
    UI.lead(over ? `${chosen.length} apps are ticked; one run changes at most ${plan.max}. Untick some, and set the rest afterwards.`
      : chosen.length ? `Sets what ${chosen.length} app${chosen.length === 1 ? "" : "s"} reserve${chosen.length === 1 ? "s" : ""} from their use over the last week. Each restarts once, one at a time.`
      : apps.length ? "Tick the apps to change." : "Every app's reservation already matches its use, or has too few readings yet."),
    rows.length ? UI.section(`Apps · ${rows.length}`, UI.table([{ label: "App" }, { label: "Reservation", className: "grow" }], rows)) : "",
    managed.length ? UI.more(`Applied by Flux or Argo CD · ${managed.length}`, managed.map(s => `<p><span class="mono">${esc(s.id)}</span>: ${esc(s.why)}</p>
      <pre class="mono small">${esc(s.yaml)}</pre>`).join(""), false, "reserve-managed") : "",
    other.length ? UI.more(`Not changed · ${other.length}`, `<ul class="ui-list">${other.map(s => `<li><span class="mono">${esc(s.id)}</span>: ${esc(s.why)}</li>`).join("")}</ul>`, false, "reserve-other") : "",
    UI.more("How the reservations are worked out", `<p>Each container's use is read every five minutes. The reservation is 95% of the week's readings with headroom - CPU 1.5 times, memory 1.25 times - and never above a limit. Apps reserving nothing are ticked; others are offered unticked, since a reservation may have been set on purpose.</p>
      <p>Apps change one at a time, each waiting for the last to run again. An app with no host that has room for its new reservation, or changed since this review, is skipped. If one does not come back within ten minutes, the job stops and the rest are not changed. Follow and stop it in Jobs.</p>`, false, "reserve-how"),
    chosen.length && !over ? UI.ack("reserveRestart", chosen.length === 1 ? "Restart it now" : `Restart these ${chosen.length} apps now`) : "",
  ].join("");
}
window.reservationsItem = (id, on) => {
  on ? RESERVE.chosen.add(id) : RESERVE.chosen.delete(id);
  const go = $("#reserveGo");
  if (go) go.disabled = true;
  reservationsPlan();
};
window.reservationsStart = async () => {
  // What was reviewed: the plan's own ticks, which its token covers.
  const plan = RESERVE.plan, apps = (plan?.apps || []).filter(a => a.chosen);
  if (!apps.length) return;
  if (!$("#reserveRestart")?.checked) return toast("Tick Restart to confirm the restarts", "bad");
  const button = $("#reserveGo");
  if (button) { button.disabled = true; button.textContent = "Starting…"; }
  try {
    await api("/api/workloads/requests", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ apps: apps.map(a => ({ id: a.id, changes: a.changes })), review_token: plan.review_token, restart: true }) });
    closeModal(); toast(`Setting reservations on ${apps.length} app${apps.length === 1 ? "" : "s"}; follow it in Jobs`, "ok");
  } catch (e) {
    toast(e.message, "bad");
    if (button) { button.disabled = false; button.textContent = "Set reservations"; }
    // Planned again with the same ticks: what changed is shown, not a reset.
    reservationsPlan();
  } finally { window.refreshOperations?.(true); }
};

/* Priority tiers for many apps at once. Apps whose name or image suggests a
   tier start set to it; the rest keep what they have. Changing a row plans
   again, so the review token covers exactly what is started. */
const PRIORITY = { picks: {}, plan: null, asked: 0 };
window.priorityReview = async () => {
  PRIORITY.picks = {}; PRIORITY.plan = null;
  modal("Priority tiers", `<div id="prioPlan"><div class="empty"><span class="spin2"></span> Reading the apps…</div></div>
    ${UI.actions(UI.button("Set tiers", "priorityStart()", { kind: "pri", id: "prioGo", attrs: 'data-need="operator" disabled' }), UI.cancel())}`, true);
  await priorityPlan();
};
async function priorityPlan() {
  const pane = $("#prioPlan");
  if (!pane) return;
  // Each change plans again; an answer to an earlier one is dropped.
  const asked = ++PRIORITY.asked;
  const picks = Object.entries(PRIORITY.picks).map(([id, tier]) => `${id}=${tier}`).join(",");
  let plan;
  try { plan = await api(`/api/workloads/priority/plan${picks ? `?${new URLSearchParams({ chosen: picks })}` : ""}`); }
  catch (e) { if (asked === PRIORITY.asked) pane.innerHTML = UI.callout("bad", "Could not read the apps", esc(e.message)); return; }
  if (asked !== PRIORITY.asked || !$("#prioPlan")) return;
  PRIORITY.plan = plan;
  pane.innerHTML = priorityHtml(plan);
  const go = $("#prioGo");
  if (go) go.disabled = !plan.changes || plan.changes > plan.max;
  window.applyRole?.();
}
/* Where a row's tier comes from, and how it stands (#430). */
const PRIORITY_STATUS = { in_line: ["in line", ""], restart: ["needs a restart", "warn"], git: ["change in git", "info"],
  outside: ["changed outside Homestead", "warn"], unknown_group: ["group tier unknown", "bad"] };
function prioritySource(a) {
  if (a.source === "group") return a.wanted ? `${a.wanted}, from ${a.group}` : `from ${a.group}`;
  if (a.source === "default") return "normal, the default";
  return `${a.wanted || a.current}, its own`;
}
function priorityStatus(a) {
  const [label, tone] = PRIORITY_STATUS[a.status] || PRIORITY_STATUS.in_line;
  const runs = a.wanted && a.wanted !== a.current ? ` · runs ${a.current}` : "";
  return `${esc(prioritySource(a) + runs)} · ${tone ? UI.chip(label, tone) : esc(label)}${a.line ? `<br><span class="mono prio-line">${esc(a.line)}</span>` : ""}`;
}
function priorityHtml(plan) {
  const apps = plan.apps || [], changes = apps.filter(a => a.tier !== a.current), over = changes.length > plan.max;
  // A VM, and a StatefulSet that doesn't restart itself, takes its tier when it next starts.
  const rolling = changes.filter(a => a.kind !== "vm" && a.restarts !== false), restarting = rolling.length;
  const sets = rolling.filter(a => a.kind === "sts").length, held = changes.length - restarting;
  const vms = changes.filter(a => a.kind === "vm").length, heldSets = held - vms;
  const count = (n, noun) => n ? `${n} ${noun}${n === 1 ? "" : "s"}` : "";
  const and = (...parts) => parts.filter(Boolean).join(" and ");
  const chips = { vm: "VM", sts: "StatefulSet" };
  const rows = apps.map((a, i) => [
    `<span class="mono">${esc(chips[a.kind] ? `${a.ns}/${a.name}` : a.id)}</span>${chips[a.kind] ? ` ${UI.chip(chips[a.kind])}` : ""}${a.tier !== a.current ? ` ${UI.chip(`was ${a.current}`, "info")}` : ""}`,
    `${prioritySelect(`prio_${i}`, a.tier, `${a.managed || a.status === "unknown_group" ? "disabled" : ""} onchange="priorityItem(${jsq(a.id)},this.value)" aria-label="${esc(a.id)} tier"`)}
      <span class="sub">${priorityStatus(a)}</span>
      <span class="sub">${esc(a.note || (a.status === "unknown_group" ? "Group tiers couldn't be read; try again shortly." : "") || (a.suggested && a.suggested !== "normal" ? `suggested ${a.suggested}: it ${a.why}` : ""))}</span>`]);
  return [
    UI.lead(over ? `${changes.length} apps would change; one run changes at most ${plan.max}. Set some back, and change the rest afterwards.`
      : changes.length ? [restarting ? `Changes the tier of ${and(count(restarting - sets, "app"), count(sets, "StatefulSet"))}. Each restarts once, one at a time${sets ? "; a StatefulSet one pod at a time" : ""}.` : "",
        held ? `${and(count(vms, "VM"), count(heldSets, "StatefulSet"))} ${held === 1 ? "takes its" : "take their"} new tier when ${held === 1 ? "it next starts" : "they next start"}; none restarts now.` : ""].filter(Boolean).join(" ")
      : "Every app keeps its tier. Choose a tier to change one."),
    // What the cluster's classes get wrong (#429), said once.
    (plan.warnings || []).length ? UI.callout("warn", "Check first", `<ul class="ui-list">${plan.warnings.map(w => `<li>${esc(w)}</li>`).join("")}</ul>`) : "",
    (plan.groups || []).length ? UI.section(`Groups · ${plan.groups.length}`, `<ul class="ui-list">${plan.groups.map(g =>
      `<li><b>${esc(g.name)}</b> ${esc(g.tier === null ? "tier unknown" : g.tier || "no tier")} · ${g.apps} app${g.apps === 1 ? "" : "s"}</li>`).join("")}</ul>`) : "",
    UI.section(`Apps, StatefulSets and VMs · ${rows.length}`, UI.table([{ label: "App" }, { label: "Tier", className: "grow" }], rows)),
    (plan.skipped || []).length ? UI.more(`Left alone · ${plan.skipped.length}`, `<ul class="ui-list">${plan.skipped.map(s => `<li><span class="mono">${esc(s.id)}</span>: ${esc(s.why)}</li>`).join("")}</ul>`, false, "prio-skipped") : "",
    UI.more("What the tiers do", `<p>When the cluster is short of room - after a host fails, but also as apps restart, update or grow - critical apps fit first and may evict any pod of a lower tier: low apps, and anything with no tier, VMs included. Normal apps may evict low ones. Low apps wait, evict nothing, and are evicted first. Normal is the cluster's default class, or no class where there is none. VMs take tiers too, from their next start. StatefulSets take tiers of their own, never a group's; one that waits for its pods to be deleted (OnDelete, or a partition at or above its replicas) takes its tier when they next restart, and Homestead never deletes them. Homestead ranks itself at least as high as any app, so none can evict it; it restarts once to do so.</p>
      <p>A tier uses the cluster's own class where one fits; Homestead makes one, just above or below the default, only where none does. Apps then change one at a time, each waiting for the last to run again, and a StatefulSet one pod at a time. If one does not come back within ten minutes - for a StatefulSet, ten per pod - the job stops and the rest are not changed.</p>`, false, "prio-how"),
    changes.length && !over ? UI.ack("prioRestart", !restarting ? "Set the tiers" : restarting === 1 ? "Restart it now" : `Restart these ${restarting}${sets ? "" : " apps"} now`) : "",
  ].join("");
}
window.priorityItem = (id, tier) => {
  PRIORITY.picks[id] = tier;
  const go = $("#prioGo");
  if (go) go.disabled = true;
  priorityPlan();
};
window.priorityStart = async () => {
  const plan = PRIORITY.plan, changes = (plan?.apps || []).filter(a => a.tier !== a.current);
  if (!changes.length) return;
  if (!$("#prioRestart")?.checked) return toast("Tick Restart to confirm the restarts", "bad");
  const button = $("#prioGo");
  if (button) { button.disabled = true; button.textContent = "Starting…"; }
  try {
    await api("/api/workloads/priority", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ changes: changes.map(a => ({ id: a.id, current: a.current, tier: a.tier, follow: !!a.follow })), review_token: plan.review_token, restart: true }) });
    const sets = changes.filter(a => a.kind === "sts").length, apps = changes.length - sets;
    const named = [apps || !sets ? `${apps} app${apps === 1 ? "" : "s"}` : "", sets ? `${sets} StatefulSet${sets === 1 ? "" : "s"}` : ""];
    closeModal(); toast(`Setting the tier of ${named.filter(Boolean).join(" and ")}; follow it in Jobs`, "ok");
  } catch (e) {
    toast(e.message, "bad");
    if (button) { button.disabled = false; button.textContent = "Set tiers"; }
    // Planned again with the same choices: what changed is shown, not a reset.
    priorityPlan();
  } finally { window.refreshOperations?.(true); }
};


// Node only (tests): the same names, for require(). The browser has no `module`.
if (typeof module === "object" && module.exports) module.exports = {
  BALANCE, balanceReadOnly, balanceOff, balanceHint, balanceReset, balancePlan, balanceSwitch, balanceTick, balanceSkipped, balanceMetrics, BALANCE_LEAVES_PREFERENCE, balanceContainersHtml, balanceVolumesHtml, balanceMoves, balanceReview, balanceReplanVolumes, RESERVE, reservationsPlan, reserveNow, reservationsHtml, PRIORITY, priorityPlan, PRIORITY_STATUS, prioritySource, priorityStatus, priorityHtml
};
