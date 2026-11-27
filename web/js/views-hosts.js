/* Homestead Hosts, after the Nov 2026 mockup: the page's summary line, a card
   a host that ends with whether it can restart safely and its two main
   actions, and the disks' hover card. A classic script, loaded after
   views-overview.js, whose helpers it uses. A phone keeps the comparison. */

// What a restart of each host would stop (/api/nodes/impact, as "If this host
// fails" reads it), read beside the page and kept until the next visit.
const HOST_IMPACT = new Map();
const hostSentence = text => { const s = String(text || "").trim(); return s ? `${s[0].toUpperCase()}${s.slice(1)}${/[.!?]$/.test(s) ? "" : "."}` : ""; };
const hostNames = names => names.length < 2 ? names.join("") : `${names.slice(0, -1).join(", ")} and ${names[names.length - 1]}`;

/* Whether a host can restart without stopping an app: {tone, title, line}, or
   null until it is read. What stops comes first; what moves says where. */
function hostRestartVerdict(name) {
  const p = HOST_IMPACT.get(name);
  if (!p) return null;
  // A read that failed is its message; the answer's own unread is a list of parts it could not read.
  if (typeof p.unread === "string") return { tone: "warn", title: "Not known whether a restart is safe", line: hostSentence(p.unread) };
  const apps = p.apps || [], stops = apps.filter(a => a.outcome === "stops"), waits = apps.filter(a => a.outcome === "waits");
  if (stops.length) return { tone: "warn", title: `A restart stops ${hostNames(stops.slice(0, 2).map(a => a.name))}${stops.length > 2 ? ` and ${stops.length - 2} more` : ""}`,
    line: hostSentence(String(stops[0].why || "").replace(/^no other host can run it: lacks (the )?/i, "No other host has a ")) };
  const moves = apps.filter(a => a.outcome === "moves"), to = [...new Set(moves.flatMap(a => a.to || []))];
  const where = to.length === 1 ? to[0] : to.length === 2 ? "the other two hosts" : "the other hosts";
  return { tone: "ok", title: "Safe to restart", line: moves.length
    ? `${moves.length === 1 ? `${moves[0].name} moves` : `Its ${moves.length} apps move`} to ${where}.${waits.length ? ` ${hostNames(waits.map(a => a.name))} ${waits.length === 1 ? "waits" : "wait"} for it to come back.` : ""}`
    : waits.length ? `${hostNames(waits.map(a => a.name))} ${waits.length === 1 ? "waits" : "wait"} for it to come back.` : "Nothing runs on it that would stop." };
}
const hostVerdictHtml = name => { const v = hostRestartVerdict(name); return v ? UI.callout(v.tone, v.title, esc(v.line)) : '<div class="dim xs host-verdict-reading">Checking whether a restart is safe…</div>'; };

/* The page's one line: how many hosts are ready, each one that is not as a
   link, and which can restart without stopping an app once that is read. */
function hostsSummary(nodes) {
  const ready = nodes.filter(n => n.status === "Ready"), down = nodes.filter(n => n.status !== "Ready");
  const head = !nodes.length ? "No hosts reported." : !down.length ? `${nodes.length === 1 ? "The host is" : `All ${nodes.length} hosts are`} ready.`
    : `${ready.length} of ${nodes.length} hosts ${ready.length === 1 ? "is" : "are"} ready.`;
  const read = nodes.every(n => HOST_IMPACT.has(n.name)) && nodes.length > 1;
  const safe = nodes.filter(n => hostRestartVerdict(n.name)?.tone === "ok").map(n => n.name);
  return UI.summary({ head: `<b>${head}</b>`, parts: [
    // Kubernetes' NotReady, in words: not ready.
    ...down.map(n => ({ text: `${n.name} is ${String(n.status || "NotReady").replace(/([a-z])([A-Z])/g, "$1 $2").toLowerCase()}`, tone: "bad", run: `nodeDetail(${jsq(n.name)})` })),
    read && { text: safe.length === nodes.length ? "Every host can restart without stopping an app."
      : safe.length ? `${hostNames(safe)} can restart without stopping an app.` : "No host can restart without stopping an app.", note: true }], narrow: { parts: down.map(n => ({ text: `${n.name} is not ready`, tone: "bad", run: `nodeDetail(${jsq(n.name)})` })) } });
}

/* The temperature, only when it needs a look - the probe has not reported
   yet, or a sensor runs warm - and the probe's age when its reading is old.
   A normal reading is in the disks' card and on the host's page. */
function hostProbeLines(n) {
  const t = n.temps, probe = n.probe || {};
  const warm = t && t.cpu_c != null && tempCls(Math.max(t.cpu_c, t.max_c || 0)) !== "t-ok";
  return `${warm ? `<div class="between"><span class="dim xs">TEMP</span><span class="small mono ${tempCls(t.cpu_c)}"><b>${t.cpu_c}°C</b>
      ${t.max_c > t.cpu_c ? `<span class="dim">max ${t.max_c}°${tempSource(t)}</span>` : ""}</span></div>`
    : probe.state === "waiting" ? `<div class="between"><span class="dim xs">TEMP</span><span class="small dim">waiting for the probe</span></div>` : ""}
    ${["stale", "unreachable"].includes(probe.state) && t ? `<div class="between"><span class="dim xs">PROBE</span>
      <span class="small ${probe.state === "unreachable" ? "t-warm" : "dim"}">${probe.state === "unreachable" ? "not answering · " : ""}last reading ${esc(probeAge(probe.age_s))} ago</span></div>` : ""}`;
}

/* A host at a glance (the mockup): its uptime, CPU, memory and disks, what
   runs on it, whether it can restart safely, and Restart host… and Start
   maintenance…. The rest is on its own page, which its name opens. */
function hostCard(n) {
  const bad = n.status !== "Ready", up = nodeUpFor(n);
  const workloads = n.workloads || [];
  return `<div class="card hostcard nodecard${bad ? " g-bad" : ""}" data-mkey="${esc(JSON.stringify([n.site?.id || "", n.name]))}"${clusterAttr(n)}>
    <div class="between nodehead">
      <div class="row" style="gap:10px;min-width:0">
        <div class="av n2">${esc(n.name.replace(/[^0-9a-z]/gi, "").slice(-2).toUpperCase())}</div>
        <div class="nodename"><button type="button" class="linkish host-open" title="Open ${esc(n.name)}" onclick="nodeDetail(${jsq(n.name)})">${esc(n.name)}</button> ${clusterTag(n)}
          <div class="dim xs" title="${esc((n.roles || []).join(" · "))}">${esc([(n.roles || []).join(" · "), up].filter(Boolean).join(" · "))}</div></div>
      </div>
      <div class="row nodehead-acts" style="gap:7px">
        ${window.hostMaintenanceChip?.(n.name) || (n.schedulable === false ? UI.chip("cordoned", "warn", { tip: nodeCordonWords(n) }) : "")}
        ${n.host_os && ["warn", "bad"].includes(n.host_os.tone) ? UI.chip("host OS", n.host_os.tone === "bad" ? "bad" : "warn", { tip: n.host_os.text }) : ""}
        ${UI.chip(n.status, bad ? "bad" : "ok")}
        <button type="button" class="btn sm" aria-label="More actions for ${esc(n.name)}" onclick="nodeActions(${jsq(n.name)})">⋯</button>
      </div>
    </div>
    ${nodeUptimeStrip(n)}
    <div class="nodemetrics">
      <div><div class="between"><span class="dim xs">CPU</span><span class="small mono"><b>${n.cpu_pct}%</b> <span class="dim">of ${n.cpu_cap}</span></span></div>${meter(n.cpu_pct, "", "cpu")}</div>
      <div><div class="between"><span class="dim xs">MEMORY</span><span class="small mono"><b>${n.mem_pct}%</b> <span class="dim">${sizePair(n.mem_used_gb, n.mem_cap_gb)}</span></span></div>${meter(n.mem_pct, "", "memory")}</div>
      <div class="host-disks" data-card="host-disks|${esc(n.name)}">${nodeDiskLines(n)}</div>
      ${hostProbeLines(n)}
    </div>
    <div class="badgegroup host-workloads"><span class="badgecap">WORKLOADS</span><div class="badge-values">
      ${workloads.length ? workloads.slice(0, 5).map(w => `<button type="button" class="tag movable" title="Move ${esc(w)} to another host" onclick="moveWorkload(${jsq(w)})">${esc(w)} <span class="mv" aria-hidden="true">⇄</span></button>`).join("")
        + (workloads.length > 5 ? `<span class="tag more" data-tip="${esc(workloads.slice(5).join(", "))}">+${workloads.length - 5}</span>` : "")
        : '<span class="dim xs">none</span>'}</div></div>
    <div class="host-verdict" data-host-verdict="${esc(n.name)}">${hostVerdictHtml(n.name)}</div>
    <div class="host-actions">${actionBar([
      { label: "Restart host…", run: `nodePowerReview(${jsq(n.name)},'reboot')`, need: "admin", plain: true, tip: "Reviewed first: what moves, what waits for the host, and its volume copies." },
      // A host already in maintenance changes it from its ⋯ (host actions).
      window.hostMaintenanceReview && !window.hostMaintenanceChip?.(n.name) && { label: "Start maintenance…", run: `hostMaintenanceReview(${jsq(n.name)})`, need: "admin", plain: true, tip: "Mark the host as in maintenance, so its downtime is not counted against its uptime." },
    ].filter(Boolean), { shown: 2 })}</div></div>`;
}

/* Reads what a restart of each host would stop, then fills each card's
   verdict and the summary line, without drawing the page again. */
async function hostsReadImpact(nodes) {
  if (nodes.length < 2) return;
  await Promise.all(nodes.map(n => api(`/api/nodes/impact?node=${encodeURIComponent(n.name)}`, { keep: true })
    .then(p => HOST_IMPACT.set(n.name, p), e => HOST_IMPACT.set(n.name, { unread: e.message }))));
  if (typeof document === "undefined") return;
  document.querySelectorAll("[data-host-verdict]").forEach(slot => { slot.innerHTML = hostVerdictHtml(slot.dataset.hostVerdict); });
  const line = document.querySelector("#views .phead > div:first-child > p");
  if (line && STATE.view === "nodes") line.innerHTML = hostsSummary(nodes);
}

/* The disks' card: each disk, what it holds and how full, the space Longhorn
   keeps for rebuilds, and the temperature. */
if (typeof HOVER_CARDS === "object") HOVER_CARDS["host-disks"] = name => {
  const n = (STATE.data.nodes || []).find(x => x.name === name);
  if (!n) return "";
  const disks = n.disks || [];
  // What Longhorn keeps free on each disk, so a copy can be rebuilt there.
  const reserved = disks.flatMap(d => d.lh_filesystems || []).reduce((sum, f) => sum + (+f.reserved_gb || 0), 0);
  return UI.card({ title: `Disks · ${n.name}`, rows: [
    ...disks.map(d => { const u = diskUsage(n, d), holds = [d.system && "System", d.lh_size_gb > 0 && "Longhorn"].filter(Boolean).join(" and ");
      return [d.name || d.device || "Disk", esc(u.capacity ? `${holds ? `${holds}, ` : ""}${Math.round(u.used)} of ${Math.round(u.capacity)} GiB` : `${sizeText(d.size_gb)}, not used yet`), "disk"]; }),
    reserved > 0 && ["Space for rebuilds", esc(`${Math.round(reserved * 10) / 10} GiB`), "layers"],
    n.temps?.cpu_c != null && ["Temperature", esc(`${n.temps.max_c ?? n.temps.cpu_c} °C${n.temps.max_source ? `, ${n.temps.max_source}` : ""}`), "thermo"],
  ], link: { label: "Open the host's storage", onclick: `nodeDetail(${JSON.stringify(n.name)})` } });
};

// Node only (tests): the same names, for require(). The browser has no `module`.
if (typeof module === "object" && module.exports) module.exports = {
  HOST_IMPACT, hostSentence, hostNames, hostRestartVerdict, hostVerdictHtml, hostsSummary, hostProbeLines, hostCard, hostsReadImpact,
};
