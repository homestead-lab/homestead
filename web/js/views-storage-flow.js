/* Homestead views: the Architecture page: how each app is reached and where its data is. Split from views-storage.js (#911); a classic script. */
const ROB = r => r === "healthy" ? "#3ddc91" : r === "degraded" ? "#ffb020" : "#ff4d4f";

/* The architecture page: how each app is reached, what it runs, where its data
   is and which hosts hold the copies - four columns read left to right, sized
   to one desktop screen.

     Access  ->  Containers  ->  Volumes  ->  Nodes (replica copies)

   Lines join a VIP's port to the container behind it, a container to each
   volume it mounts, and a volume to each copy of it. Hovering anything lights
   up what it depends on and what depends on it - following the lines away from
   it in each direction and never back, so a VIP that twelve apps share does
   not light up all twelve when one of them is pointed at. */
// Prefix graph identities, never display names or operational resource names.
// Even matching VIPs and node names on two clusters must remain separate paths.
const archId = (id, site) => site ? `${id.slice(0,2)}${encodeURIComponent(site.id)}/${id.slice(2)}` : id;
// A cluster missing from the view of all of them, in the words the lists use
// (#705): one whose last graph is still drawn is out of date, not unloaded.
function archMissingCallout(missing) {
  if (!missing?.length) return "";
  const title = missing.some(m => m.age_s == null) ? "Architecture is incomplete" : "Architecture may be out of date";
  return UI.callout("warn", title, esc(missing.map(m => fleetMissingSentence(m, "graph")).join(" ")));
}
function architectureData(data) {
  if(!Array.isArray(data.clusters))return data;
  const graph={workloads:[],volumes:[],nodes:[],vips:[],jobs:[],missing:data.missing || []};
  for(const cluster of data.clusters){
    const site=cluster.site, key=id=>id?archId(id,site):id;
    graph.workloads.push(...cluster.workloads.map(w=>({...w,site,id:key(w.id),claims:w.claims.map(c=>({...c,vid:key(c.vid)})),
      after:(w.after||[]).map(a=>({...a,id:key(a.id)}))})));
    graph.volumes.push(...cluster.volumes.map(v=>({...v,site,id:key(v.id)})));
    graph.nodes.push(...cluster.nodes.map(n=>({...n,site,id:key(n.id),copies:n.copies.map(c=>({...c,vid:key(c.vid)}))})));
    graph.vips.push(...cluster.vips.map(v=>({...v,site,id:key(v.id)})));
    // A member on an older version sends no scheduled jobs.
    graph.jobs.push(...(cluster.jobs||[]).map(j=>({...j,site,id:key(j.id),claims:(j.claims||[]).map(c=>({...c,vid:key(c.vid)}))})));
  }
  return graph;
}
async function viewFlow() {
  const navigation=window.NAV_TOKEN;
  const [answer] = await Promise.all([api("/api/flow"), loadHardwareFeatures()]);
  if(navigation!==window.NAV_TOKEN)return;
  const f=architectureData(answer);
  STATE.data.flow = f;
  const links = [];
  const portId = (ip, port, site) => archId(`p:${ip}:${port}`,site);
  f.workloads.forEach(w => w.ports.forEach(p => { if (p.vip) links.push([portId(p.vip, p.port, w.site), w.id, "access"]); }));
  f.workloads.forEach(w => w.claims.forEach(c => { if (c.vid) links.push([w.id, c.vid, "mount"]); }));
  f.nodes.forEach(n => n.copies.forEach(c => links.push([c.vid, `${n.id}|${c.vid}`, "copy"])));
  links.push(...archStartLinks(f.workloads));
  STATE.data.alinks = links;
  const used = new Set(links.flat());
  const mountedVolumes = mountedVolumeIds(links, f.jobs);
  const host = w => (w.node || "").replace(/^harvester-/, "");
  const kind = w => w.kind === "vm" ? (w.running ? `VM · ${host(w) || w.state || "starting"}` : `VM · ${(w.state || "stopped").toLowerCase()}`) : host(w);
  const disconnectedVolumes = f.volumes.filter(v => !mountedVolumes.has(v.id));
  const workloadCard = w => `<div class="a2item a2wl ${used.has(w.id) ? "" : "a2alone"} ${w.kind === "vm" && !w.running ? "a2stopped" : ""}"
          id="${esc(w.id)}"${clusterAttr(w)} data-kind="workload" data-id="${esc(w.id)}"
          title="${esc(w.name)} · ${esc(kind(w))}${w.ip ? ` · ${esc(w.ip)}` : ""}${w.kind !== "vm" || w.running ? ` · ${workloadCpuPercent(w.cpu)} CPU · ${w.mem_mb || 0} MiB` : ""}">
          ${appAvatar(w.name, w.icon)}<span class="a2name">${esc(w.name)}</span>
          <span class="a2meta">${esc(kind(w))}</span>
          ${(w.after || []).length ? `<span class="a2after">after ${esc(w.after.map(a => f.workloads.find(x => x.id === a.id)?.name || a.id.split("/").pop()).join(", "))}</span>` : ""}
          ${w.kind === "vm" && !w.running ? "" : `<button class="a2act" data-need="operator" title="${w.kind === "vm" ? "Migrate" : "Move to another host"}"
            onclick="event.stopPropagation();${w.kind === "vm" ? `vmMove(${jsq(w.ns || "lab")},${jsq(w.name)})` : `moveWorkload(${jsq(w.name)},${jsq(w.ns || "lab")})`}">⇄</button>`}
        </div>`;
  const volumeCard = (v, disconnected = false) => `<div class="a2item a2vol ${used.has(v.id) ? "" : "a2alone"} ${disconnected ? "a2disconnected" : ""}" id="${esc(v.id)}"${clusterAttr(v)} data-kind="volume" data-id="${esc(v.id)}"
          title="${esc(v.name)} · ${v.size_gb} GiB · ${v.replicas} replicas · ${esc(v.robustness)}${v.attached ? ` · attached on ${esc(v.attached)}` : ""}">
          <span class="a2dot" style="background:${ROB(v.robustness)}"></span><span class="a2name">${esc(v.name)}</span>
          <span class="a2meta mono">${v.size_gb}G · ${v.replicas}×</span></div>`;

  const renderGraph = f => {
    const jobs=f.jobs||[];
    const containers=f.workloads.filter(w=>w.kind!=="vm"),vms=f.workloads.filter(w=>w.kind==="vm");
    const connectedVolumes=f.volumes.filter(v=>mountedVolumes.has(v.id)),disconnectedVolumes=f.volumes.filter(v=>!mountedVolumes.has(v.id));
    return `<div class="arch2wrap"><svg class="archsvg" aria-hidden="true"></svg><div class="arch2">

      <section class="a2col"><h4>Access</h4>
      ${f.vips.map(v => `<div class="a2item a2vip ${v.state && !["ok", "idle"].includes(v.state) ? "a2broken" : ""}" ${clusterAttr(v)} data-kind="access" data-id="${esc(v.id)}"
          data-node="${esc(v.node || "")}" title="${esc(archAddressWords(v))}">
          <div class="a2vipip"><span class="mono">${esc(v.ip)}</span>
            <span class="a2meta">${v.kind === "node" ? "node" : "VIP"}${v.node ? ` · ${esc(v.node.replace(/^harvester-/, ""))}` : ""}</span></div>
          ${v.state && !["ok", "idle"].includes(v.state) ? `<div class="a2why">${esc(v.state === "unrouted" ? "not reachable" : v.state === "pending" ? "port taken" : "no host answers")}</div>` : ""}
          <div class="a2ports">${v.ports.map(p => `<button type="button" class="a2port" id="${esc(portId(v.ip, p.port, v.site))}" data-kind="access"
            data-id="${esc(portId(v.ip, p.port, v.site))}" title="${esc(p.app)} · open ${esc(v.ip)}:${p.port}" aria-label="Open ${esc(p.app)} on port ${p.port}"
            onclick="openSvc(${jsq(v.ip)},${p.port})">${p.port}</button>`).join("")}</div></div>`).join("")
        || '<div class="dim xs">No load-balancer addresses</div>'}
      </section>

      <section class="a2col"><h4>Apps <span class="dim">${f.workloads.length}</span></h4>
        <div class="a2group"><div class="a2subhead">Containers <span>${containers.length}</span></div>
          ${containers.map(workloadCard).join("") || '<div class="dim xs">No containers</div>'}</div>
        ${vms.length ? `<div class="a2group a2vmgroup"><div class="a2subhead">Virtual machines <span>${vms.length}</span></div>
          ${vms.map(workloadCard).join("")}</div>` : ""}
        ${jobs.length ? scheduledJobs(jobs) : ""}
      </section>

      <section class="a2col"><h4>Volumes <span class="dim">${f.volumes.length}</span></h4>
        ${connectedVolumes.map(v => volumeCard(v)).join("") || '<div class="dim xs">No connected volumes</div>'}
        ${STATE.archDisconnected && disconnectedVolumes.length ? `<div class="a2group a2disconnected-group">
          <div class="a2subhead badtext">Disconnected <span>${disconnectedVolumes.length}</span></div>
          ${disconnectedVolumes.map(v => volumeCard(v, true)).join("")}</div>` : ""}
      </section>

      <section class="a2col"><h4>Hosts &amp; replica copies</h4>
      ${f.nodes.map(n => `<div class="a2item a2node"${clusterAttr(n)} data-kind="node" data-id="${esc(n.id)}">
          <div class="a2nodehead"><b>${esc(n.name)}</b><span class="dim xs">${n.copies.length} cop${n.copies.length === 1 ? "y" : "ies"}</span></div>
          ${(n.ips || []).length || (n.vips || []).length ? `<div class="a2addrs">${(n.ips || []).map(ip => `<span class="a2addr mono" title="${esc(n.name)}'s own address">${esc(ip)}</span>`).join("")}${(n.vips || []).map(ip => `<span class="a2addr vip mono" title="${esc(n.name)} answers for VIP ${esc(ip)}">VIP ${esc(ip)}</span>`).join("")}</div>` : ""}
          <div class="a2copies">${n.copies.map(c => `<span class="a2copy ${c.running ? "" : "stopped"}" id="${esc(`${n.id}|${c.vid}`)}"
            data-kind="copy" data-id="${esc(`${n.id}|${c.vid}`)}" title="${esc(c.vol)} on ${esc(n.name)}${c.running ? "" : " · not running"}">${esc(c.vol)}</span>`).join("")
            || '<span class="dim xs">no replicas here</span>'}</div></div>`).join("")}
      </section>
    </div></div>`;
  };
  const graphs=answer.clusters?.map(cluster=>({site:cluster.site,...Object.fromEntries(["workloads","volumes","nodes","vips","jobs"].map(key=>[key,f[key].filter(row=>row.site.id===cluster.site.id)]))}));
  paint(`${UI.pageHeader(`Architecture`, `How each app is reached, where its data lives, and which hosts hold the copies · point at, tap or Tab to anything to trace it${metricsNotice([answer])}`, `
        ${disconnectedVolumes.length ? `<button class="btn sm ${STATE.archDisconnected ? "pri" : ""}" onclick="STATE.archDisconnected=!STATE.archDisconnected;viewFlow()"
          data-tip="Volumes no container, VM or scheduled job is defined to mount. They stay hidden so old and retained data does not obscure the live paths.">${STATE.archDisconnected ? "Hide" : "Show"} ${disconnectedVolumes.length} disconnected</button>` : ""}
        <div class="row hide-sm arch-legend">
          <span><i style="background:var(--arch-access)"></i>port</span>
          <span><i style="background:var(--arch-mount)"></i>mount</span>
          <span><i style="background:repeating-linear-gradient(90deg,var(--arch-copy) 0 3px,transparent 3px 7px)"></i>replica</span>
          ${f.workloads.some(w => (w.after || []).length) ? `<span><i style="background:var(--arch-after)"></i>starts after (→ the app it waits for)</span>` : ""}
        </div>
      `, {actionsClass:`arch-head-actions`})}
    ${archMissingCallout(f.missing)}
    ${graphs?graphs.map(graph=>`<section class="architecture-cluster" data-architecture-cluster="${esc(graph.site.id)}">${UI.moduleHeader(graph.site.name,"",`<span class="dim small">${graph.workloads.length} apps and VMs · ${graph.nodes.length} hosts</span>`)}${renderGraph(graph)}</section>`).join(""):renderGraph(f)}`);
  // A live refresh redraws the page; whatever was being traced stays traced.
  if (STATE.data.archHover) archHighlight(STATE.data.archHover);
  else drawArch();
  archWatch();
}

/* Starts after (#746): an app to each app it starts after, as [from, to,
   type]; "pending" while its gate isn't in the template (Flux, not committed). */
function archStartLinks(workloads) {
  return (workloads || []).flatMap(w => (w.after || []).map(a => [w.id, a.id, a.committed ? "starts-after" : "starts-after pending"]));
}
const archStartsAfter = type => String(type).startsWith("starts-after");

/* The volumes something is defined to mount: an app's or a VM's, and one
   only a scheduled job mounts - a backup target - which is in use too. */
function mountedVolumeIds(links, jobs) {
  return new Set([...links.filter(([, , type]) => type === "mount").map(([, id]) => id),
    ...(jobs || []).flatMap(j => (j.claims || []).map(c => c.vid)).filter(Boolean)]);
}

/* Scheduled jobs, collapsed under the apps: a nightly backup runs for a minute
   and is not part of how anything is reached, but a failed run must not look
   like a good one - so the count of failures stays on the closed summary. */
function scheduledJobs(jobs) {
  const failed = jobs.filter(j => j.last_failed).length;
  // A schedule runs on its own time zone or, with none, on the clock of the
  // cluster's controller - on k3s often the host's local time, which the
  // page cannot know. So only a UTC one gets a next run; one with no zone
  // is put in words on the cluster's clock, and one with another zone, or
  // written in a way CRON cannot read (@daily, SUN), is shown as written.
  const plain = j => ["UTC", "Etc/UTC"].includes(j.time_zone) && CRON.parse(j.schedule);
  const when = j => plain(j) ? CRON.describe(j.schedule)
    : !j.time_zone && CRON.parse(j.schedule) ? `${CRON.describe(j.schedule).replace(" UTC", "")} on the cluster's clock`
    : `${j.schedule}${j.time_zone ? ` (${j.time_zone})` : ""}`;
  const uses = j => (j.claims || []).map(c => c.pvc).join(", ");
  const row = j => `<div class="a2item a2job ${j.last_failed ? "a2jobfailed" : ""}"${clusterAttr(j)} tabindex="0"
      data-tip="${esc(`${j.ns}/${j.name} · ${when(j)}${uses(j) ? ` · uses ${uses(j)}` : ""}`)}">
      <span class="a2name">${esc(j.name)}</span>
      <span class="a2jobwhen">${lastRunText(j, `the log of its last run, in ${j.ns}`)}<span class="dim"> · ${j.suspended ? "paused"
        : plain(j) ? `next ${nextRun(j.schedule)}` : esc(when(j))}</span></span>
      ${uses(j) ? `<span class="a2jobwhen dim">uses ${esc(uses(j))}</span>` : ""}</div>`;
  return `<details class="a2group a2jobs"${STATE.archJobs ? " open" : ""} ontoggle="STATE.archJobs=this.open">
    <summary class="a2subhead">Scheduled jobs <span>${failed ? `<b class="badtext">${failed} failed</b> · ` : ""}${jobs.length}</span></summary>
    ${jobs.map(row).join("")}</details>`;
}

/* What an address is, where it lives, and why it is not reachable if not. */
function archAddressWords(v) {
  const what = v.kind === "node" ? `${v.ip} is ${v.node || "a host"}'s own address`
    : v.node ? `VIP ${v.ip} · ${v.node} answers for it` : `VIP ${v.ip} · no host answers for it`;
  return v.reason ? `${what} · ${v.reason}` : what;
}

/* Hover and resize, wired once for the page rather than per element, so a
   live refresh that adds a row does not leave it unwired. */
function archWatch() {
  $$(".arch2wrap").forEach(wrap=>{
  // Every traced item takes focus, so the keyboard can trace too (#487).
  $$("[data-id]:not(button):not([tabindex])", wrap).forEach(item => item.setAttribute("tabindex", "0"));
  if (wrap.dataset.watched) return;
  wrap.dataset.watched = "1";
  const trace = event => {
    const item = event.target.closest("[data-id]");
    if (item && item.dataset.id !== STATE.data.archHover) archHighlight(item.dataset.id);
  };
  // Hover with a mouse, focus with the keyboard, a tap on a touch screen.
  wrap.addEventListener("mouseover", trace);
  wrap.addEventListener("focusin", trace);
  wrap.addEventListener("click", trace);
  wrap.addEventListener("mouseleave", () => archHighlight(null));
  wrap.addEventListener("focusout", event => { if (!wrap.contains(event.relatedTarget)) archHighlight(null); });
  if (window.ResizeObserver) new ResizeObserver(() => drawArch(STATE.data.archKeep)).observe(wrap);
  });
}

function archHighlight(id) {
  STATE.data.archHover = id;
  const keep = id ? archRelated(id) : null;
  STATE.data.archKeep = keep;
  const selected=[...$$(".arch2 [data-id]")].find(el=>el.dataset.id===id)?.closest(".arch2wrap");
  $$(".arch2 [data-id]").forEach(el => {
    const on = el.closest(".arch2wrap")!==selected || !keep || keep.has(el.dataset.id)
      || (el.classList.contains("a2node") && [...keep].some(k => k.startsWith(el.dataset.id + "|")))
      || (el.classList.contains("a2vip") && [...keep].some(k => k.startsWith("p:" + el.dataset.id.slice(2) + ":")));
    el.classList.toggle("dimmed", !on);
    el.classList.toggle("sel", el.dataset.id === id);
  });
  drawArch(keep);
}

/* Everything upstream and downstream of one item: from it, each link is
   followed left or right, and each step keeps going the same way. */
function archRelated(id) {
  const links = STATE.data.alinks || [];
  const start = [id];
  // A VIP or a node stands for all of its ports or copies.
  if (id.startsWith("i:")) links.forEach(([a]) => { if (a.startsWith("p:" + id.slice(2) + ":")) start.push(a); });
  if (id.startsWith("n:")) links.forEach(([, b]) => { if (b.startsWith(id + "|")) start.push(b); });
  // Where an address lives: its node, and a node's addresses - not followed
  // further, or a node would light every app it answers for.
  const places = (STATE.data.flow?.vips || []).filter(v => v.node);
  const beside = id.startsWith("i:") ? places.filter(v => v.id === id).map(v => archId("n:" + v.node,v.site))
    : id.startsWith("n:") ? places.filter(v => archId("n:" + v.node,v.site) === id).map(v => v.id) : [];
  const keep = new Set(start);
  // Starts after joins two apps, not a path to their data: only the apps
  // either side of the one traced light up, and the trace does not go on.
  const walk = (from, forward) => {
    links.forEach(([a, b, type]) => {
      if (archStartsAfter(type)) return;
      const next = forward ? (a === from ? b : null) : (b === from ? a : null);
      if (next && !keep.has(next)) { keep.add(next); walk(next, forward); }
    });
  };
  start.forEach(item => { walk(item, true); walk(item, false); });
  links.forEach(([a, b, type]) => { if (archStartsAfter(type) && (a === id || b === id)) { keep.add(a); keep.add(b); } });
  beside.forEach(item => keep.add(item));
  return keep;
}

function drawArch(keep = STATE.data.archKeep) {
  $$(".arch2wrap").forEach((wrap,index)=>{
  const svg=wrap.querySelector(".archsvg");
  if(!svg)return;
  const arrow=`a2arrow-${index}`;
  let arrows=false;
  const localKeep=keep && [...wrap.querySelectorAll("[data-id]")].some(el=>el.dataset.id===STATE.data.archHover)?keep:null;
  const R = wrap.getBoundingClientRect();
  svg.setAttribute("viewBox", `0 0 ${R.width} ${R.height}`);
  const box = id => {
    const el = document.getElementById(id);
    if (!el || !el.offsetParent || !wrap.contains(el)) return null;
    const b = el.getBoundingClientRect();
    // A port or a copy sits among others in its box; its line meets the box's
    // edge at its height rather than crossing its neighbours to reach it.
    const edge = el.closest(".a2vip, .a2node")?.getBoundingClientRect() || b;
    return { l: edge.left - R.left, r: edge.right - R.left, y: b.top - R.top + b.height / 2 };
  };
  const motion = SET.motion !== "off";
  svg.innerHTML = (STATE.data.alinks || []).map(([a, b, type]) => {
    const A = box(a), B = box(b);
    if (!A || !B) return "";
    const lit = localKeep && localKeep.has(a) && localKeep.has(b);
    const after = archStartsAfter(type);
    arrows ||= after;
    const bend = after ? 18 + Math.min(42, Math.abs(B.y - A.y) / 5) : Math.max(24, (B.l - A.r) / 2);
    // Both apps are in one column: starts after loops out on its right, to an arrowhead.
    const d = after ? `M ${A.r} ${A.y} C ${A.r + bend} ${A.y} ${B.r + bend} ${B.y} ${B.r + 2} ${B.y}`
      : `M ${A.r} ${A.y} C ${A.r + bend} ${A.y} ${B.l - bend} ${B.y} ${B.l} ${B.y}`;
    const cls = `a2link ${type}${localKeep ? (lit ? " lit" : " faded") : ""}`;
    return `<path class="${cls}" d="${d}"${after ? ` marker-end="url(#${arrow})"` : ""}/>` + (lit && motion ? `<path class="a2flow ${type}" d="${d}"/>` : "");
  }).join("");
  if (arrows) svg.insertAdjacentHTML("afterbegin", `<defs><marker id="${arrow}" viewBox="0 0 6 6" refX="5" refY="3" markerWidth="6" markerHeight="6" orient="auto"><path class="a2arrowhead" d="M0,0L6,3L0,6z"/></marker></defs>`);
  });
}

// Node only (tests): the same names, for require(). The browser has no `module`.
if (typeof module === "object" && module.exports) module.exports = {
  ROB, archId, archMissingCallout, architectureData, viewFlow, archStartLinks, archStartsAfter, mountedVolumeIds,
  scheduledJobs, archAddressWords, archWatch, archHighlight, archRelated, drawArch
};
