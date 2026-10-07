/* If this host goes down (homestead_impact.py): what stops until it is back,
   what waits for it, what moves and where, which volumes are at risk, and
   which addresses move. Opened from a host's actions. */
const shortHost = name => String(name || "").replace(/^harvester-/, "");

function impactApp(r) {
  const tone = r.outcome === "stops" ? "crit" : r.outcome === "waits" ? "warn" : "ok";
  const avatar = r.kind === "vm" && typeof vmAvatar === "function" ? vmAvatar(r) : appAvatar(r.name, r.icon);
  const where = r.outcome === "moves" && r.to?.length ? `to ${r.to.slice(0, 3).map(shortHost).join(", ")}${r.to.length > 3 ? ` +${r.to.length - 3}` : ""} · ` : "";
  return `<div class="impact-row">${avatar}<div class="impact-text"><b>${esc(r.name)}</b>
      <span class="dim xs">${esc(r.ns)}${r.kind === "vm" ? " · VM" : ""}${r.keeps_answering ? " · keeps answering from its other copies" : ""}</span>
      <span class="small">${esc(where + r.why)}</span>
      ${(r.notes || []).map(n => `<span class="dim xs">${esc(n)}</span>`).join("")}</div>
    <span class="pill slim ${tone}">${esc(r.outcome)}</span></div>`;
}

window.nodeIfDown = async (node, back = null) => {
  window.__ifDownBack = back;
  modal(`If ${node} goes down`, '<div class="empty"><span class="spin2"></span> Working it out…</div>', true);
  let p;
  try { p = await api(`/api/nodes/impact?node=${encodeURIComponent(node)}`); }
  catch (e) { $("#mbody").innerHTML = UI.callout("bad", "It could not be worked out", esc(e.message)) + UI.actions(ifDownClose()); return; }
  const c = p.counts, by = o => p.apps.filter(r => r.outcome === o);
  const section = (title, rows, html) => rows.length ? UI.section(title, `<div class="impact-list">${html}</div>`) : "";
  const chips = [c.stops && `<span class="pill crit">${c.stops} stop</span>`, c.waits && `<span class="pill warn">${c.waits} wait</span>`,
    c.moves && `<span class="pill ok">${c.moves} move</span>`, c.at_risk && `<span class="pill crit">${c.at_risk} volume${c.at_risk === 1 ? "" : "s"} at risk</span>`,
    c.fewer_copies && `<span class="pill warn">${c.fewer_copies} with fewer copies</span>`, c.addresses && `<span class="pill info">${c.addresses} address${c.addresses === 1 ? "" : "es"} move</span>`].filter(Boolean);
  $("#mbody").innerHTML = `<div class="ui-stack">
    ${UI.lead(esc(`What becomes of what runs on ${node} if it stops answering - a failure, or a reboot without moving things first. ${p.others_ready.length ? `${p.others_ready.length} other host${p.others_ready.length === 1 ? " is" : "s are"} ready to take work.` : "No other host is ready."}`))}
    ${chips.length ? `<div class="row impact-chips">${chips.join("")}</div>` : '<div class="ui-empty">Nothing of yours runs here, and no volume keeps a copy here.</div>'}
    ${section("Stops until it is back", by("stops"), by("stops").map(impactApp).join(""))}
    ${section("Waits for it", by("waits"), by("waits").map(impactApp).join(""))}
    ${section("Moves to another host", by("moves"), by("moves").map(impactApp).join(""))}
    ${section("Volumes", [...p.at_risk, ...p.fewer_copies],
      p.at_risk.map(v => `<div class="impact-row"><div class="impact-text"><b>${esc(v.name)}</b><span class="dim xs">${esc(v.namespace)}</span>
        <span class="small">Its only healthy copy is here: unavailable until the host is back, and lost if its disk is.</span></div><span class="pill slim crit">at risk</span></div>`).join("")
      + p.fewer_copies.map(v => `<div class="impact-row"><div class="impact-text"><b>${esc(v.name)}</b><span class="dim xs">${esc(v.namespace)}</span>
        <span class="small">Carries on with ${v.left} of ${v.wanted} cop${v.wanted === 1 ? "y" : "ies"} until Longhorn rebuilds one.</span></div><span class="pill slim warn">fewer copies</span></div>`).join(""))}
    ${section("Addresses", p.addresses, p.addresses.map(a => `<div class="impact-row"><div class="impact-text"><b class="mono">${esc(a.ip)}</b>
        <span class="small">Moves to another host within seconds${a.services?.length ? `; used by ${esc(a.services.slice(0, 3).join(", "))}` : ""}.</span></div><span class="pill slim info">moves</span></div>`).join(""))}
    ${UI.more("How this is worked out", "<p>Where each app could go comes from the same checks as moving it: the hardware and devices it uses, its placement rules, and a copy of each of its Longhorn volumes on another host. When it goes comes from its failover setting. A VM with a device passed through stays with it. It is a forecast: Kubernetes decides at the time, with what is free then.</p>")}
    ${UI.actions(ifDownClose())}</div>`;
};

const ifDownClose = () => window.__ifDownBack ? UI.button("Back", "ifDownBack()") : UI.cancel("Close");
window.ifDownBack = () => { const back = window.__ifDownBack; window.__ifDownBack = null; back ? back() : closeModal(); };
