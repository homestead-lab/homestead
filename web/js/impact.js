/* If this host goes down (homestead_impact.py): what stops until it is back,
   what waits for it, what moves and where, which volumes are at risk, and
   which addresses move. Opened from a host's actions. */
const shortHost = name => String(name || "").replace(/^harvester-/, "");

function impactApp(r, node = "") {
  const tone = { stops: "bad", waits: "warn", unknown: "neutral" }[r.outcome] || "ok";
  const avatar = r.kind === "vm" && typeof vmAvatar === "function" ? vmAvatar(r) : appAvatar(r.name, r.icon);
  const where = r.outcome === "moves" && r.to?.length ? `to ${r.to.slice(0, 3).map(shortHost).join(", ")}${r.to.length > 3 ? ` +${r.to.length - 3}` : ""} · ` : "";
  // A StatefulSet says how many of its copies are here (#440).
  const kind = r.kind === "vm" ? " · VM" : r.kind === "set" ? ` · ${r.here} of ${r.copies} cop${r.copies === 1 ? "y" : "ies"} here` : "";
  const setting = r.setting === "node_down" ? ' <button class="linkish xs" onclick="ifDownSetting()">Open the setting</button>' : "";
  // A StatefulSet Longhorn's setting can help says so in one line; why it
  // waits, and where the setting is, sit behind a disclosure (word budget).
  const why = r.setting === "node_down" ? "waits until its pod is deleted." : r.why;
  // A waiting StatefulSet on a host that is down can be started elsewhere (#441).
  // Harvester does it itself, so the button isn't offered there.
  const hostDown = node && STATE.data?.nodes?.find(n => n.name === node)?.status === "NotReady";
  const restart = r.kind !== "set" || r.outcome !== "waits" || !hostDown ? ""
    : STATE.platform?.harvester ? '<span class="dim xs">Harvester restarts these itself; see its VM force reset policy.</span>'
    : `<span>${UI.button("Start on another host…", `setRestartReview(${jsArg(r.ns)},${jsArg(r.name)},${jsArg(node)})`, { attrs: 'data-need="admin"' })}</span>`;
  return `<div class="impact-row">${avatar}<div class="impact-text"><b>${esc(r.name)}</b>${r.kind === "set" ? ` ${UI.chip("StatefulSet", "neutral")}` : ""}
      <span class="dim xs">${esc(r.ns + kind)}${r.keeps_answering ? " · keeps answering from its other copies" : ""}</span>
      <span class="small">${esc(where + why)}${setting}</span>${why !== r.why ? UI.more("Why it waits", `<p>${esc(r.why)}</p>`, false, `why:${r.ns}/${r.name}`) : ""}
      ${(r.notes || []).map(n => `<span class="dim xs">${esc(n)}</span>`).join("")}${restart}</div>
    ${UI.chip(r.outcome, tone, { size: "slim" })}</div>`;
}

window.nodeIfDown = async (node, back = null) => {
  window.__ifDownBack = back;
  modal(`If ${node} goes down`, '<div class="empty"><span class="spin2"></span> Working it out…</div>', true);
  let p;
  try { p = await api(`/api/nodes/impact?node=${encodeURIComponent(node)}`); }
  catch (e) { $("#mbody").innerHTML = UI.callout("bad", "It could not be worked out", esc(e.message)) + UI.actions(ifDownClose()); return; }
  const c = p.counts, by = o => p.apps.filter(r => r.outcome === o);
  const section = (title, rows, html) => rows.length ? UI.section(title, `<div class="impact-list">${html}</div>`) : "";
  const chips = [c.stops && UI.chip(`${c.stops} stop`, "bad"), c.waits && UI.chip(`${c.waits} wait`, "warn"),
    c.unknown && UI.chip(`${c.unknown} unknown`, "neutral"),
    c.moves && UI.chip(`${c.moves} move`, "ok"), c.at_risk && UI.chip(`${c.at_risk} volume${c.at_risk === 1 ? "" : "s"} at risk`, "bad"),
    // null: Longhorn's copies weren't read, so the risk is unknown, not none (#1002).
    c.at_risk === null && UI.chip("volume risk unknown", "neutral"),
    c.fewer_copies && UI.chip(`${c.fewer_copies} with fewer copies`, "warn"), c.addresses && UI.chip(`${c.addresses} address${c.addresses === 1 ? "" : "es"} move`, "info")].filter(Boolean);
  $("#mbody").innerHTML = `<div class="ui-stack">
    ${UI.lead(esc(`What becomes of what runs on ${node} if it stops answering - a failure, or a reboot without moving things first. ${p.others_ready.length ? `${p.others_ready.length} other host${p.others_ready.length === 1 ? " is" : "s are"} ready to take work.` : "No other host is ready."}`))}
    ${chips.length ? `<div class="row impact-chips">${chips.join("")}</div>` : '<div class="ui-empty">Nothing of yours runs here, and no volume keeps a copy here.</div>'}
    ${(p.unread || []).length ? `<div class="dim xs">Couldn't read ${esc(p.unread.join(", "))}, so none of them is listed.</div>` : ""}
    ${section("Stops until it is back", by("stops"), by("stops").map(r => impactApp(r, node)).join(""))}
    ${section("Waits", by("waits"), by("waits").map(r => impactApp(r, node)).join(""))}
    ${section("Couldn't be checked", by("unknown"), by("unknown").map(r => impactApp(r, node)).join(""))}
    ${section("Moves to another host", by("moves"), by("moves").map(r => impactApp(r, node)).join(""))}
    ${section("Volumes", [...p.at_risk, ...p.fewer_copies],
      p.at_risk.map(v => `<div class="impact-row"><div class="impact-text"><b>${esc(v.name)}</b><span class="dim xs">${esc(v.namespace)}</span>
        <span class="small">Its only healthy copy is here: unavailable until the host is back, and lost if its disk is.</span></div>${UI.chip("at risk", "bad", { size: "slim" })}</div>`).join("")
      + p.fewer_copies.map(v => `<div class="impact-row"><div class="impact-text"><b>${esc(v.name)}</b><span class="dim xs">${esc(v.namespace)}</span>
        <span class="small">Carries on with ${v.left} of ${v.wanted} cop${v.wanted === 1 ? "y" : "ies"} until Longhorn rebuilds one.</span></div>${UI.chip("fewer copies", "warn", { size: "slim" })}</div>`).join(""))}
    ${section("Addresses", p.addresses, p.addresses.map(a => `<div class="impact-row"><div class="impact-text"><b class="mono">${esc(a.ip)}</b>
        <span class="small">Moves to another host within seconds${a.services?.length ? `; used by ${esc(a.services.slice(0, 3).join(", "))}` : ""}.</span></div>${UI.chip("moves", "info", { size: "slim" })}</div>`).join(""))}
    <div id="ifDownRoom" data-node="${esc(node)}"></div>
    ${UI.more("How this is worked out", "<p>Where each app could go comes from the same checks as moving it: the hardware and devices it uses, its placement rules, and a copy of each of its Longhorn volumes on another host. When it goes comes from its failover setting. A StatefulSet's pod is never started elsewhere while the old one might still run: it waits until it is deleted, by hand or by Longhorn's Pods on a failed host setting. A VM with a device passed through stays with it. It is a forecast: Kubernetes decides at the time, with what is free then.</p>")}
    ${UI.actions(ifDownClose())}</div>`;
  window.applyRole?.();   // hides what this role may not use (data-need)
  ifDownRoom(node);
};

/* Whether the hosts left have room for what moves (homestead_capacity):
   each one's memory once it has taken its share, and what would wait. */
const capacityVerdict = v => ({ fits: ["ok", "fits"], tight: ["warn", "tight"], waits: ["bad", "apps wait"], "no-host": ["bad", "no host left"], unknown: ["neutral", "unknown"] })[v] || ["neutral", v];
function capacityRoomHtml(f) {
  const rows = (f.hosts || []).map(h => {
    const pct = gib => h.mem_alloc_gib ? gib / h.mem_alloc_gib * 100 : 100;
    return [`<span class="mono">${esc(shortHost(h.name))}</span>`,
    `${UI.meter({ now: pct(h.mem_before_gib ?? h.mem_gib), after: pct(h.mem_gib), warnAt: 90, label: `${h.name}: memory reserved after the move` })}
     <span class="sub">${h.mem_before_gib !== undefined && h.mem_before_gib !== h.mem_gib ? `${esc(h.mem_before_gib)} → ` : ""}${esc(h.mem_gib)} of ${esc(h.mem_alloc_gib)} GiB reserved · CPU ${esc(Math.round(h.cpu_alloc_m ? h.cpu_m / h.cpu_alloc_m * 100 : 100))}%</span>`];
  });
  const stuck = [...(f.waits || []), ...(f.evicted || [])];
  return [
    UI.lead(esc(f.sentence)),
    rows.length ? UI.table([{ label: "Host" }, { label: "Reserved after the move", className: "grow" }], rows) : "",
    stuck.length ? UI.more(`${f.evicted?.length ? "Would wait or be evicted" : "Would wait"} · ${stuck.length}`, `<div class="impact-list">${stuck.map(p => `<div class="impact-row"><div class="impact-text"><b class="mono">${esc(p.id)}</b>
      <span class="small">${esc(p.why)}</span><span class="dim xs">${esc(p.tier)} tier · reserves ${esc(p.mem_gib)} GiB</span></div>${UI.chip(p.on ? "evicted" : "waits", "warn", { size: "slim" })}</div>`).join("")}</div>`) : "",
    f.unreserved ? UI.more(`Reserving nothing · ${f.unreserved}`, `<p>${f.unreserved} of the ${f.moving} app${f.moving === 1 ? "" : "s"} that move reserve${f.unreserved === 1 ? "s" : ""} nothing, so ${f.unreserved === 1 ? "it fits" : "they fit"} anywhere on paper and still use memory: the figures are a floor.</p>`) : "",
  ].join("");
}
async function ifDownRoom(node) {
  const host = $("#ifDownRoom");
  if (!host) return;
  try {
    const all = await api("/api/capacity/forecast");
    const f = (all.hosts || []).find(h => h.node === node);
    // Another host's preview may have opened while this one loaded.
    const now = $("#ifDownRoom");
    if (f && now && now.dataset.node === node) now.innerHTML = UI.section(`Room on the other hosts · ${capacityVerdict(f.verdict)[1]}`, capacityRoomHtml(f));
  } catch (e) {
    // The rest of the preview stands, and this part says it could not be worked out (#620).
    const now = $("#ifDownRoom");
    if (now && now.dataset.node === node) now.innerHTML = UI.section("Room on the other hosts", UI.callout("warn", "Could not work out the room on the other hosts", esc(e.message)));
  }
}
window.capacityRoomHtml = capacityRoomHtml;
window.capacityVerdict = capacityVerdict;

const ifDownClose = () => window.__ifDownBack ? UI.button("Back", "ifDownBack()", { attrs: 'data-dialog-dismiss="true"' }) : UI.cancel("Close");
window.ifDownBack = () => { const back = window.__ifDownBack; window.__ifDownBack = null; back ? back() : closeModal(); };

// Where Longhorn's "Pods on a failed node" is set: Settings → Hardware and
// storage, on the Longhorn storage card (homestead_lhcapacity, #440).
window.ifDownSetting = () => { closeModal(); settingsTab("cluster"); go("settings"); };
