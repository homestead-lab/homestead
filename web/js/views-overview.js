const tempCls = c => c == null ? "" : sev(c, "temperature") === "b" ? "t-hot" : sev(c, "temperature") === "w" ? "t-warm" : "t-ok";
const tempTag = c => c == null ? "" : sev(c, "temperature") === "b" ? "bad" : sev(c, "temperature") === "w" ? "warn" : "";
/* The sensor a maximum came from, in brackets: "71° (NVMe nvme0)". */
const tempSource = temps => temps?.max_source ? ` (${esc(temps.max_source)})` : "";
/* One temperature for a host, labelled the same on every page (#485): the
   CPU's, with the hottest sensor beside it when that runs hotter. */
function nodeTemp(n) {
  const t = n.temps || {}, cpu = t.cpu_c ?? null, max = t.max_c ?? null;
  const value = cpu ?? max;
  if (value == null && n.probe?.state === "waiting") return { value, label: "CPU temp", text: "waiting for the probe", extra: "" };
  const extra = [cpu != null && max != null && max > cpu ? `max ${max}°C${t.max_source ? `, ${t.max_source}` : ""}` : "",
    value != null && ["stale", "unreachable"].includes(n.probe?.state) ? `last reading ${probeAge(n.probe.age_s)} ago` : ""].filter(Boolean).join(" · ");
  return { value, label: cpu != null || max == null ? "CPU temp" : "Max temp", text: value == null ? "—" : `${value}°C`, extra };
}
/* The node probe is read in the background (#705), so a host shows at once and
   fills in when its probe answers. These say how old its readings are. */
function probeAge(seconds) {
  if (seconds == null) return "";
  return seconds < 90 ? `${seconds} s` : seconds < 5400 ? `${Math.round(seconds / 60)} min` : `${Math.round(seconds / 3600)} h`;
}
function probeProblem(n) {
  const p = n.probe || {};
  if (p.state !== "unreachable") return "";
  return `The probe on ${n.name} hasn't answered the last ${p.misses} times` + (p.last_ok ? `; its last reading is ${probeAge(p.age_s)} old` : "");
}
/* The kubelet's filesystem, which may not be the system drive's. */
const NODE_FS_TIP = "The filesystem the kubelet reports for this host; Storage lists each drive.";
/* The processor, as the node probe reads it, with its cores. */
const cpuSummary = n => [n.temps?.cpu_model, n.cpu_cap != null ? `${n.cpu_cap} core${n.cpu_cap == 1 ? "" : "s"}` : ""].filter(Boolean).map(esc).join(" · ") || "—";
const smartTone = health => health === "passed" ? "ok" : health === "failed" ? "bad" : "";
/* The verdict Homestead reaches from the counters, not smartctl's own
   overall-health bit - that stays PASSED until a drive is nearly gone. */
const diskHealthTone = state => state === "healthy" ? "ok" : state === "attention" ? "warn"
  : state === "critical" ? "bad" : "";
const diskHealthWord = state => state === "attention" ? "needs attention"
  : state === "unavailable" ? "not reported" : (state || "unknown");
/* Life remaining, where the drive reports something that means it. */
const lifeTone = pct => pct == null ? "" : pct <= 10 ? "bad" : pct <= 25 ? "warn" : "ok";
const smartMetric = value => value == null ? "unsupported" : String(value);
const smartTestTone = status => /without error|success|passed/i.test(status || "") ? "ok" :
  /fail|error|aborted|interrupted/i.test(status || "") ? "bad" : "";
const nodeHardwareIds = n => (STATE.data.hardwareFeatures || [])
  .filter(f => n.hardware?.[f.id]).map(f => f.id);

/* Dashboard, Nodes, node detail modal */

/* The Dashboard shows one warning (#506): whatever needs saying - the
   cluster's health, chart history, the saved layout - is one UI.callout,
   led by the most serious, with the rest listed under it. */
function dashboardAlert(items) {
  const list = items.filter(Boolean);
  if (!list.length) return "";
  const [lead, ...rest] = list;
  const tone = list.some(item => item.tone === "bad") ? "bad" : "warn";
  const body = `${lead.text ? `<p>${esc(lead.text)}</p>` : ""}
    ${rest.length ? `<ul>${rest.map(item => `<li><b>${esc(item.title)}.</b> ${esc(item.text || "")}</li>`).join("")}</ul>` : ""}
    ${list.some(item => item.action) ? `<div class="row dash-alert-actions">${list.map(item => item.action || "").join("")}</div>` : ""}`;
  // The health item leads when there is one; insights.js marks it
  // acknowledged once every issue in it has been.
  return `<div class="dash-alert"${lead.health ? " data-health" : ""}>${UI.callout(tone, lead.title, body)}</div>`;
}

/* The sidebar's health pill, on every page (#940). The Dashboard paints it
   from its overview; every page from each /api/alerts read (pwa.js), which
   carries the same verdict. Colour is the verdict; starting, updating,
   repairing (a volume rebuilding a copy) and maintenance (a host Homestead is
   working on) are not faults (#941), so they stay neutral with a pulsing
   dot. Dashed is "not there yet": CHECKING before the first answer, UNKNOWN when there is no verdict
   (null, or the server's "unknown"), and the last verdict once it is a
   minute old. h undefined (a server without the field) leaves the pill be. */
function paintHealthPill(h) {
  const pill = $("#healthPill");
  if (!pill || h === undefined) return;
  const state = h?.state ? String(h.state).toLowerCase() : "unknown";
  const unread = state === "unknown";
  const tone = { healthy: "ok", degraded: "warn", critical: "bad" }[state] || "neutral";
  const busy = ["starting", "updating", "repairing", "maintenance"].includes(state);
  const age = unread || typeof h.age_s !== "number" ? null : h.age_s;
  const extra = unread ? " pending" : busy ? " busy" : "";
  pill.className = `ui-chip ${tone}${extra}${age !== null && age >= 60 ? " stale" : ""}`;
  // role="status": a rewritten text is read out again, so only when it changes.
  if (pill.textContent !== state.toUpperCase()) pill.textContent = state.toUpperCase();
  pill.dataset.state = state;
  const checked = age === null ? "" : age < 10 ? "checked just now" : age < 60 ? `checked ${age}s ago` : `checked ${fmtAgo(age)}`;
  pill.title = unread ? "Homestead could not read the cluster's health just now"
    : [h.summary, checked].filter(Boolean).join(" · ");
}
/* The pill as index.html draws it, before any answer: on sign-out, so the
   next session does not start from this one's verdict. */
function healthPillChecking() {
  const pill = $("#healthPill");
  if (!pill) return;
  pill.className = "ui-chip neutral pending";
  pill.textContent = "CHECKING";
  pill.dataset.state = "checking";
  pill.title = "Reading the cluster's health";
}
window.paintHealthPill = paintHealthPill;
window.healthPillChecking = healthPillChecking;

async function viewDash() {
  if (Dashboard.editing()) return;
  const requestedNavigation = window.NAV_TOKEN;
  // A part that could not be read says so, rather than "nothing yet" (#620).
  const [o, histRead, stRead, capRead, , , upRead] = await Promise.all([
    api("/api/overview"),
    api("/api/history").catch(e => ({ unread: e.message })),
    api("/api/storage").catch(e => ({ unread: e.message })),
    STATE.platform?.longhorn === false ? null : api("/api/longhorn/capacity").catch(e => ({ unread: e.message })),
    loadHardwareFeatures(),
    loadHealthSettings(),
    api("/api/nodes/uptime").catch(e => ({ unread: e.message })),
    Dashboard.load(),
  ]);
  if (Dashboard.editing() || STATE.view !== "dash" || requestedNavigation !== window.NAV_TOKEN) return;
  const [hist, st, cap] = [histRead, stRead, capRead].map(r => r?.unread ? null : r);
  noteNodeUptime(upRead);
  STATE.data.ov = o; STATE.data.stor = st; STATE.data.lhcap = cap || STATE.data.lhcap;
  window.HealthInsights?.seed?.("overview", o);
  // A node near its allocation limit takes no new replicas: said before it bites.
  const tight = (cap?.nodes || []).filter(n => n.level !== "ok");
  window.refreshPwaAlerts?.();
  paintHealthPill({ state: o.health_state || o.health, summary: o.health_summary, age_s: o.age_s });
  $("#lbinfo").textContent = o.lb_ip ? "VIP " + o.lb_ip : "";
  const sv = $("#setVip"), sn = $("#setNodes");
  if (sv) sv.textContent = o.lb_ip || "—";
  if (sn) sn.textContent = `${o.nodes_ready}/${o.nodes_total}`;

  const H = hist || {};
  const rx = H.net_rx || [], tx = H.net_tx || [];
  const sampledAt = H.last_sample || H.t?.at(-1) || 0;
  const sampleAge = sampledAt ? Math.max(0, Date.now() / 1000 - sampledAt) : 0;
  const chartWindow = H.step === 300 ? "Last hour · saved five-minute samples" : "Last hour · sampled every 30 seconds";
  const noChart = histRead?.unread
    ? `<div class="empty small">Could not read the recorded metrics: ${esc(histRead.unread)}</div>`
    : '<div class="empty small">Waiting for recorded metrics</div>';
  const netNow = o.nodes.reduce((s, n) => s + (n.rx_mbps || 0), 0);
  const txNow = o.nodes.reduce((s, n) => s + (n.tx_mbps || 0), 0);
  const diskUsed = o.nodes.reduce((s, n) => s + (n.fs_used_gb || 0), 0);
  const diskCap = o.nodes.reduce((s, n) => s + (n.fs_cap_gb || 0), 0);

  const storParts = st ? [
    { n: "Healthy", v: st.healthy, c: "#3ddc91" },
    { n: "Degraded", v: st.degraded, c: "#ffb020" },
    { n: "Faulted", v: st.faulted, c: "#ff4d4f" },
  ].filter(p => p.v) : [];

  Dashboard.content = {
    compute: `    <div class="card glow dashcard ${worstMetricClass([{ value: o.cpu_pct, metric: "cpu" }, { value: o.mem_pct, metric: "memory" }])}">
      <div class="between"><div><div class="ctitle">Compute</div>
        <div class="csub">CPU and memory · ${chartWindow}</div></div>${trend(H.cpu)}</div>
      ${H.cpu?.length ? dualSpark(H.cpu, H.mem || [], { times: H.t }) : noChart}
      <div class="row dashnums">
        <div><div class="bignum">${o.cpu_pct}<span class="unit">%</span></div>
          <div class="csub"><span class="kdot s1"></span>CPU · ${o.cpu_cap} cores capacity</div></div>
        <div><div class="bignum">${o.mem_pct}<span class="unit">%</span></div>
          <div class="csub"><span class="kdot s2"></span>RAM · ${sizePair(o.mem_used_gb, o.mem_cap_gb)}</div></div>
      </div>
    </div>`,
    throughput: `    <div class="card glow g-info dashcard">
      <div class="between"><div><div class="ctitle">Throughput</div>
        <div class="csub">Network and local disk · ${chartWindow}</div></div>${trend(rx)}</div>
      ${rx.length ? dualSpark(rx, tx, { times: H.t }) : noChart}
      <div class="row dashnums">
        <div><div class="bignum">${rateParts(netNow)[0]}<span class="unit">${rateParts(netNow)[1]}</span></div>
          <div class="csub"><span class="kdot s1"></span>in · ${rateParts(txNow).join(" ")} out</div></div>
        <div><div class="midnum">${sizeParts(diskUsed)[0]}<span class="unit">${sizeParts(diskUsed)[1]}</span></div>
          <div class="csub">host disk of ${sizeText(diskCap)}</div>
          ${meter(diskCap ? diskUsed / diskCap * 100 : 0, "", "disk")}</div>
      </div>
    </div>`,
    storage: `    <div class="card flat storagecard">
      <div class="ctitle">Storage</div><div class="csub">Longhorn capacity and replica health</div>
      ${st ? `<div class="storbody">
      <div class="stordonut">${segDonut(storParts, st.volumes, "volumes", 128)}</div>
      <div class="storstats">
      <div class="drow"><div class="dl">Free</div><div class="dv mono nowrap">${sizeText(st.avail_gb)}</div></div>
      <div class="drow"><div class="dl">Used</div><div class="dv mono nowrap">${sizePair(st.used_gb, st.cap_gb)}</div></div>
      <div class="drow"><div class="dl">Provisioned</div><div class="dv mono nowrap">${sizeText(st.provisioned_gb)}</div></div>
      <div style="margin-top:10px">${meter(st.used_pct)}
        <div class="csub" style="margin-top:6px">${st.used_pct}% of raw capacity used
          ${st.degraded || st.faulted ? `· ${UI.chip(`${st.degraded + st.faulted} unhealthy`, st.faulted ? "bad" : "warn")}` : `· ${UI.chip("all healthy", "ok")}`}</div>
        ${tight.length ? `<a class="tag ${tight.some(n => n.level === "crit") ? "bad" : "warn"} lh-tight" onclick="go('storage')"
          data-tip="${esc(tight.map(n => `${n.name}: ${n.allocated_gb} of ${n.limit_gb} GiB allocated, room for a ${n.room_gb} GiB replica`).join("; "))}">${esc(tight.map(n => n.name.replace("harvester-", "")).join(", "))} nearly full · a new ${cap.nodes.length > 1 ? "2-copy" : ""} volume fits ${esc(sizeText(cap.largest[Math.min(2, cap.nodes.length)]))}</a>` : ""}
        ${capRead?.unread && STATE.platform?.longhorn === true ? `<div class="csub" style="margin-top:6px">${UI.chip("Could not read Longhorn's allocation limits", "warn")}
          a host near its limit is not flagged: ${esc(capRead.unread)}</div>` : ""}</div>
      </div></div>`
      : stRead?.unread ? UI.callout("warn", "Could not read storage", esc(stRead.unread))
      : '<div class="empty">Storage figures aren\'t available yet.</div>'}
    </div>`,
    cpu: `<div class="card flat pad0 consumer-card"><div class="cardhd"><div class="ctitle">Top CPU</div></div>${consumerTable(o.top_cpu, "cpu")}</div>`,
    memory: `<div class="card flat pad0 consumer-card"><div class="cardhd"><div class="ctitle">Top memory</div></div>${consumerTable(o.top_mem, "memory")}</div>`,
  };

  paint(`
  ${UI.pageHeader(`Cluster overview`, dashboardScope(o) + dashboardAge(o), `
      <button class="btn hide-sm" onclick="Dashboard.start()">${icon("edit")}Edit dashboard</button>
      <button class="btn pri hide-sm" onclick="go('deploy')">＋ Deploy</button>
    `, {mobileSummary:"omit",actionsClass:"hide-sm"})}

  ${dashboardAlert([
    (o.health_issues || []).length && { health: true, tone: o.health === "critical" ? "bad" : "warn",
      title: o.health === "critical" ? "Cluster needs attention" : "Cluster is degraded", text: o.health_summary,
      action: `<button class="btn sm" onclick="HealthInsights.open('health')">Review</button>` },
    hist === null ? { title: "Chart history could not be loaded", text: "Current overview values are shown; history will retry on the next refresh." }
      : sampledAt && sampleAge > Math.max(120, (H.step || 30) * 2) ? { title: `Charts last sampled ${fmtAgo(sampleAge)}`, text: "Check Live charts in Settings → About." } : null,
    Dashboard.notice(),
  ])}
  ${Dashboard.render()}`);
  // Saved history must not hold the live refresh loop's busy flag.
  historyPaint();
  Dashboard.loadPortal();
  Dashboard.loadMonitoring();
  Dashboard.loadTimeline();
  HealthInsights.load();
}

/* How old the overview's numbers are (#705 step 8). age_s comes from the
   server, so a browser clock that is off does not skew it. The numbers are
   normally at most one snapshot cycle old; a minute or more means the cluster
   has not answered since, and the page shows the last numbers it had. */
function dashboardAge(o) {
  const age = o.age_s;
  if (typeof age !== "number") return "";
  if (age < 10) return ` · <span class="dim">updated just now</span>`;
  if (age < 60) return ` · <span class="dim">updated ${age}s ago</span>`;
  return ` · ${UI.chip(`updated ${fmtAgo(age)}`, "warn", { tip: "The cluster has not answered since: these are the last numbers Homestead read" })}`;
}

/* Whether metrics-server's CPU and memory figures on Hosts and Containers are
   current (#906). The server reads it in the background and gives every row
   the reading's state; old or missing figures are said, not shown as zeros. */
function metricsNotice(rows) {
  const m = (Array.isArray(rows) ? rows : []).find(r => r?.metrics)?.metrics;
  if (!m) return "";
  const why = m.detail ? ` (${m.detail})` : "";
  if (m.state === "stale")
    return ` · ${UI.chip(`CPU and memory from ${fmtAgo(m.age_s)}`, "warn", { tip: `metrics-server has not answered since then${why}: these are its last figures` })}`;
  if (m.state === "unavailable")
    return ` · ${UI.chip("no CPU or memory figures", "warn", { tip: `metrics-server is not answering${why}${m.age_s ? `; it last answered ${fmtAgo(m.age_s)}` : ""}` })}`;
  if (m.state === "absent")
    return ` · <span class="dim" data-tip="metrics-server is not installed in this cluster, so there are no CPU or memory figures to show">no CPU or memory figures</span>`;
  return "";
}

/* Dense host summaries respond to widget width, rather than viewport width. */
/* The dashboard reads this cluster only. With All clusters picked it says so,
   and points to Nodes, which combines them. */
function dashboardScope(o) {
  const count = `${o.nodes_total} host${o.nodes_total === 1 ? "" : "s"}`;
  if (!fleetAll()) return `Live health, capacity and placement across ${count}`;
  const me = (FLEET.view?.members || []).find(m => m.self);
  return `${esc(me?.name || "This cluster")} only · ${count} · the Dashboard doesn't combine clusters yet. <button type="button" class="linkish" onclick="go('nodes')">Every cluster's hosts</button>`;
}

/* A host's state, worked out once for the dashboard, Nodes and its own page
   (#484): not ready, a drive warning, cordoned or a host OS warning. A drive
   warning shows even when Kubernetes says Ready. */
function nodeHealth(n) {
  const drives = n.temps?.disks || [];
  const issues = [...new Set([...(n.disk_issues || []).map(i => `${i.disk}: ${i.reason}`),
    ...drives.filter(d => ["critical", "attention", "degraded"].includes(d.health?.state)).map(d => `${d.name}: ${d.health.summary || "Drive needs attention"}`)])];
  const ready = n.status === "Ready";
  const critical = !ready || (n.disk_issues || []).some(i => i.severity === "critical") || drives.some(d => d.health?.state === "critical");
  const hostOs = ["warn", "bad"].includes(n.host_os?.tone);
  const probe = probeProblem(n);
  const tone = critical ? "bad" : issues.length || probe || n.schedulable === false || hostOs ? "warn" : "ok";
  const status = !ready ? stateWords(n.status) || "Not ready" : issues.length ? "Drive warning"
    : probe ? "Probe not answering" : n.schedulable === false ? "Cordoned" : hostOs ? "Host OS warning" : "Ready";
  return { tone, status, issues: probe ? [...issues, probe] : issues, ready };
}
/* The node page's Storage line: its drives, any with a warning, any unused. */
function nodeStorageLine(n, drives, unused) {
  // The probe's own warning is not a drive's.
  const warned = nodeHealth(n).issues.length - (probeProblem(n) ? 1 : 0);
  return [plural(drives, "drive"), warned ? plural(warned, "warning") : "", unused ? `${unused} unused` : ""].filter(Boolean).join(" · ");
}
/* One callout on a node's Overview when it needs doing something about. */
function nodeAttention(n) {
  const h = nodeHealth(n);
  if (h.tone === "ok" || (h.ready && !h.issues.length)) return "";
  const lines = [!h.ready ? `${n.name} is not answering, so its readings are not current.` : "", ...h.issues];
  return UI.callout(h.tone === "bad" ? "bad" : "warn", h.status, `<ul class="ui-list">${lines.filter(Boolean).map(l => `<li>${esc(l)}</li>`).join("")}</ul>
    ${UI.actions(UI.button("Review in Health", "HealthInsights.open('health')"))}`);
}
function dashboardNodes(nodes, display="compact") {
  const header=UI.moduleHeader("Host health", "", UI.button("Health", "HealthInsights.open('health')", {attrs:'aria-label="Review cluster health"'}));
  if(display==="detailed")return `<div class="card flat dashboard-nodes">${header}${nodeComparison(nodes,"dashboard")}</div>`;
  const known=value=>typeof value==="number" && Number.isFinite(value);
  const percent=(label,value,kind)=>`<div class="node-compact-metric"><span>${label}</span><b class="mono">${known(value)?esc(Math.round(value*10)/10)+"%":"—"}</b>${known(value)?meter(value,"",kind):'<span class="node-compact-no-data" title="Not reported">—</span>'}</div>`;
  const line=(label,value,tip="")=>`<div class="node-compact-line"><span>${label}</span><b class="mono" title="${esc(tip)}">${esc(value)}</b></div>`;
  return `<div class="card flat dashboard-nodes">${header}<div class="node-compact-grid">${nodes.map(n=>{
    const {issues,tone,status,ready}=nodeHealth(n);
    // A host that isn't answering has no current readings: dashes, not zeros.
    const live=value=>ready?value:null;
    const temp=ready?nodeTemp(n):{label:"CPU temp",text:"—",extra:""};
    const network=ready&&known(n.rx_mbps)&&known(n.tx_mbps)?ratePair(n.rx_mbps,n.tx_mbps).join(" "):"—";
    return `<article class="node-compact-host"${clusterAttr(n)}>
      <div class="node-compact-heading">${UI.statusDot(tone,status)}<button class="linkish" onclick="nodeDetail(${jsq(n.name)})" title="${esc(n.name)}">${esc(n.name)}</button></div>
      <div class="node-compact-state ${tone==="bad"?"t-hot":tone==="warn"?"t-warm":"dim"}">${esc(status)}</div>
      ${percent("CPU",live(n.cpu_pct),"cpu")}${percent("Memory",live(n.mem_pct),"memory")}${percent("Node filesystem",live(n.fs_pct),"disk")}
      ${line("Network",network,"Receive / transmit")}${line(temp.label,temp.text+(temp.extra?` · ${temp.extra}`:""),"The CPU's temperature, and the hottest sensor when it runs hotter")}
      ${line("Pods",n.pods ?? "—")}${line("Uptime",nodeUpFor(n).replace(/^up /,"") || "—")}
      ${issues.length?`<button class="node-compact-warning" onclick="HealthInsights.open('health')" title="${esc(issues.join("; "))}">${esc(issues.join("; "))}</button>`:""}
    </article>`;
  }).join("") || '<div class="empty small">No hosts reported.</div>'}</div></div>`;
}

function consumerTable(workloads, metric) {
  return `<div class="tblwrap"><table class="tbl stack compact consumer-table"><thead><tr><th>App</th><th>${metric === "cpu" ? "CPU" : "RAM"}</th></tr></thead>
    <tbody>${workloads.slice(0, 5).map(w => consumerRow(w, metric)).join("")}</tbody></table></div>`;
}

function consumerRow(workload, metric) {
  const details = [workload.ns, (workload.nodes || []).join(", ")].filter(Boolean).join(" · ");
  const cpu = metric === "cpu";
  return `<tr class="consumer-row"><td class="consumer-identity"><b title="${esc(workload.name)}">${esc(workload.name)}</b>
    <div class="consumer-meta dim" title="${esc(details)}">${esc(details || "—")}</div></td>
    <td class="consumer-value mono" data-status${cpu ? ' title="100% equals one CPU core"' : ""}><b>${cpu ? workloadCpuPercent(workload.cpu) : workloadMemory(workload.mem_mb)}</b>
      ${cpu ? meter(Math.min(100, Number(workload.cpu || 0) * 100)) : ""}</td></tr>`;
}

/* How long a node has been up, and how much of the last while. The host's
   own uptime comes from the node probe; without it, how long Kubernetes has
   had it Ready. Availability is from the samples Homestead keeps. */
function nodeUpFor(n) {
  if (n.status !== "Ready") return "";
  if (n.uptime_s) return `up ${fmtUp(n.uptime_s)}`;
  if (n.ready_since) return `Ready ${fmtAgo((Date.now() - Date.parse(n.ready_since)) / 1000).replace(" ago", "")}`;
  return "";
}
const uptimePct = v => v == null ? "—" : v >= 99.995 ? "100%" : `${v.toFixed(v >= 99 ? 2 : 1)}%`;
function uptimeDuration(s) {
  s = Math.max(0, Math.round(s));
  const d = Math.floor(s / 86400), h = Math.floor(s % 86400 / 3600), m = Math.round(s % 3600 / 60);
  return d ? (h ? `${d}d ${h}h` : `${d}d`) : h ? (m ? `${h}h ${m}m` : `${h}h`) : `${Math.max(1, m)}m`;
}
const uptimeWhen = t => fmtAt(t);
/* Hosts' uptime as last read. A read that failed is kept as such, so a host
   says it could not be read rather than "no samples yet" (#620). Its own key:
   Monitoring's checks (STATE.data.uptime) are another answer, and neither read
   replaces the other's. */
function noteNodeUptime(read) {
  if (read?.unread) { STATE.data.nodeUptimeUnread = read.unread; return; }
  STATE.data.nodeUptimeUnread = "";
  STATE.data.nodeUptime = read || STATE.data.nodeUptime;
}

/* The node page's uptime card: how it is now, the windows, ninety days as a
   strip, and a log of each outage and reboot. The log is history - an entry
   says when it went down and how long for - so a node that came back does
   not read as down; only one down at this moment says so. */
window.nodeUptimePaint = async n => {
  const host = $("#nodeUptime");
  if (!host) return;
  const all = await api("/api/nodes/uptime").catch(e => ({ unread: e.message }));
  const u = all?.nodes?.[n.name];
  const now = n.status === "Ready"
    ? `${UI.chip("Up now", "neutral")}<span class="mono small">${esc(nodeUpFor(n))}</span>`
    : UI.chip(`${n.status || "Down"} now`, "bad");
  const head = `<div class="between"><div class="ctitle">Uptime</div><div class="row" style="gap:8px">${now}</div></div>`;
  if (all?.unread) { host.innerHTML = `${head}<div class="dim small">Could not read uptime: ${esc(all.unread)}</div>`; return; }
  if (!u) { host.innerHTML = `${head}<div class="dim small">No samples yet: Homestead records one every five minutes.</div>`; return; }
  const tone = uptimeTone;
  // Maintenance Homestead caused, or Harvester's maintenance mode, is listed
  // as itself; an outage inside it is not listed again (#535).
  const maintenance = u.maintenance || [];
  const events = [...(u.outages || []).filter(o => !o.maintenance).map(o => ({ t: o.start, kind: "down", o })),
    ...maintenance.map(m => ({ t: m.start, kind: "maint", m })),
    ...(u.reboots || []).map(r => ({ t: r.t ?? r, kind: "reboot", planned: !!r.planned }))]
    .sort((a, b) => b.t - a.t).slice(0, 12);
  const took = o => `${o.exact ? "" : "about "}${uptimeDuration(o.down_s)}`;
  const outside = u.windows_excluding_maintenance || {};
  const dayReasons = d => [...new Set(maintenance.filter(m => m.start < d.day + 86400 && (m.end ?? Infinity) > d.day).map(m => m.reason))];
  host.innerHTML = `${head}
    <div class="uptime-windows">${Object.entries(u.windows).map(([label, v]) => `<div><span class="dim xs">LAST ${label.toUpperCase()}</span>
      <b class="mono uptime-${tone(v)}">${uptimePct(v)}</b>${outside[label] != null && uptimePct(outside[label]) !== uptimePct(v)
        ? `<span class="dim xs">· ${uptimePct(outside[label])} outside maintenance</span>` : ""}</div>`).join("")}</div>
    <div class="uptime-strip" aria-label="Each of the last 90 days">${u.days.map(d => uptimeDayStriped(d)
      ? `<i class="uptime-maint" data-tip="${esc(fmtDay(d.day))} · maintenance: ${esc(dayReasons(d).join(", ") || "planned")}"></i>`
      : `<i class="uptime-${tone(uptimeDayShare(d))}"
      data-tip="${esc(fmtDay(d.day))} · ${d.up == null ? "no samples" : uptimePct(uptimeDayShare(d)) + " up"}${d.maintenance ? " outside maintenance" : ""}"></i>`).join("")}</div>
    <div class="between dim xs" style="margin-top:3px"><span>90 days ago</span><span>today</span></div>
    <div class="dim xs uptime-loghead">OUTAGES, MAINTENANCE AND REBOOTS</div>
    ${events.length ? `<div class="uptime-events">${events.map(e => e.kind === "reboot"
      ? (e.planned ? `<div>${UI.chip("rebooted (planned)", "neutral")}<span class="mono xs">${esc(uptimeWhen(e.t))}</span></div>`
        : `<div>${UI.chip("unexpected reboot", "warn")}<span class="mono xs">${esc(uptimeWhen(e.t))}</span></div>`)
      : e.kind === "maint"
        ? (e.m.ongoing ? `<div>${UI.chip(`maintenance: ${e.m.reason}, since ${uptimeWhen(e.m.start)}`, "neutral")}</div>`
          : `<div>${UI.chip(`maintenance: ${e.m.reason}`, "neutral")}<span class="mono xs">${esc(uptimeWhen(e.m.start))}</span>
            <span class="dim xs">for ${uptimeDuration(e.m.end - e.m.start)}</span></div>`)
      : e.o.ongoing
        ? `<div>${UI.chip("down now", "bad")}<span class="mono xs">since ${esc(uptimeWhen(e.o.start))}</span>
            <span class="dim xs">${took(e.o)} so far</span></div>`
        : `<div>${UI.chip("was down", "warn")}<span class="mono xs">${esc(uptimeWhen(e.o.start))}</span>
            <span class="dim xs">for ${took(e.o)}, then back</span></div>`).join("")}</div>`
      : '<div class="dim small">No outage or reboot recorded.</div>'}`;
};

/* What falls to this node rather than another: the addresses kube-vip has
   it announce - the management VIP hosts join through among them - and the
   shared volumes whose share manager runs here. */
function nodeDutyTags(n) {
  const d = n.duties || {};
  const tags = [];
  if ((d.management_vip || []).length) tags.push(`<span class="tag duty info" data-tip="This host answers for the cluster's management address - the dashboard and hosts joining. If it fails, another host takes the address within seconds.">management VIP ${esc(d.management_vip.join(", "))}</span>`);
  if (d.control_plane_vip) tags.push('<span class="tag duty info" data-tip="This host holds the control-plane address the Kubernetes API answers on">API VIP</span>');
  if ((d.rwx || []).length) tags.push(`<span class="tag duty" data-tip="Longhorn serves these shared (RWX) volumes from this host: ${esc(d.rwx.join(", "))}. If it fails they pause until their share manager starts elsewhere">serves ${d.rwx.length} shared volume${d.rwx.length === 1 ? "" : "s"}</span>`);
  return tags.join("");
}
window.nodeDutyTags = nodeDutyTags;

/* The node's own address and each VIP it answers for right now - the ones
   that move to another node if this one goes down. */
function nodeAddressTags(n) {
  const own = [...new Set([n.addresses?.InternalIP, n.addresses?.ExternalIP].filter(Boolean))];
  const d = n.duties || {};
  const vips = (d.vips || []).filter(ip => !(d.management_vip || []).includes(ip));
  // The mono wrapper sets the chip's font: an address reads as one.
  // .ui-chip has no margin, and three of the four places that show these have no gap: a space keeps them apart.
  return [...own.map(ip => `<span class="mono">${UI.chip(ip, "neutral", { tip: `${n.name}'s own address. Services k3s's ServiceLB publishes answer here too` })}</span>`),
    ...vips.map(ip => `<span class="mono">${UI.chip(`VIP ${ip}`, "info", { tip: `${n.name} answers for VIP ${ip} now. If it goes down another host takes the address over` })}</span>`)].join(" ");
}

/* A node at a glance. The head is the same shape on every card - name, roles
   and how long it has been up on two lines, then thirty days of uptime - so
   the meters below line up across a row; what falls to one node alone (a
   VIP, shared volumes it serves) sits in the footer with its hardware. */
function nodeCard(n) {
  const dots = "<i></i>".repeat(Math.min(n.pods_sys, 80)) + '<i class="wl"></i>'.repeat(Math.min(n.pods_wl, 40));
  const bad = n.status !== "Ready";
  const health = worstMetricClass([
    { value: n.cpu_pct, metric: "cpu" }, { value: n.mem_pct, metric: "memory" },
    { value: n.fs_pct || 0, metric: "disk" }, { value: n.temps?.cpu_c || 0, metric: "temperature" },
  ]);
  const duties = nodeDutyTags(n);
  const up = nodeUpFor(n);
  const net = ratePair(n.rx_mbps, n.tx_mbps);
  return `<div class="card glow ${bad ? "g-bad" : health} clickable nodecard"${clusterAttr(n)}
       onclick="nodeDetail(${jsq(n.name)})">
    <div class="between nodehead">
      <div class="row" style="gap:10px">
        <div class="av n2">${esc(n.name.replace(/[^0-9a-z]/gi, "").slice(-2).toUpperCase())}</div>
        <div class="nodename"><div style="font-weight:680" title="${esc(n.name)}">${esc(n.name)} ${clusterTag(n)}</div>
          <div class="dim xs" title="${esc(n.roles.join(" · "))}">${esc([n.roles.join(" · "), up].filter(Boolean).join(" · "))}</div></div>
      </div>
      <div class="row nodehead-acts" style="gap:7px">
        ${n.schedulable === false ? UI.chip("cordoned", "warn") : ""}
        ${n.host_os && ["warn", "bad"].includes(n.host_os.tone) ? UI.chip("host OS", n.host_os.tone === "bad" ? "bad" : "warn", { tip: n.host_os.text }) : ""}
        ${UI.chip(n.status, bad ? "bad" : "ok")}
        <button class="btn sm" onclick="event.stopPropagation();nodeActions(${jsq(n.name)})">⋯</button>
      </div>
    </div>
    ${nodeUptimeStrip(n)}
    <div class="nodemetrics">
      <div><div class="between"><span class="dim xs">CPU</span>
        <span class="small mono"><b>${n.cpu_pct}%</b> <span class="dim">of ${n.cpu_cap}</span></span></div>
        ${meter(n.cpu_pct, "", "cpu")}</div>
      <div><div class="between"><span class="dim xs">MEMORY</span>
        <span class="small mono"><b>${n.mem_pct}%</b> <span class="dim">${sizePair(n.mem_used_gb, n.mem_cap_gb)}</span></span></div>
        ${meter(n.mem_pct, "", "memory")}</div>
      ${nodeDiskLines(n)}
      <div class="between"><span class="dim xs">NETWORK</span>
        <span class="small mono">${net[0]} <span class="dim">${net[1]}</span></span></div>
      ${n.temps && n.temps.cpu_c != null ? `<div class="between"><span class="dim xs">TEMP</span>
        <span class="small mono ${tempCls(n.temps.cpu_c)}"><b>${n.temps.cpu_c}°C</b>
          ${n.temps.max_c > n.temps.cpu_c ? `<span class="dim">max ${n.temps.max_c}°${tempSource(n.temps)}</span>` : ""}</span></div>`
        : n.probe?.state === "waiting" ? `<div class="between"><span class="dim xs">TEMP</span><span class="small dim">waiting for the probe</span></div>` : ""}
      ${["stale", "unreachable"].includes(n.probe?.state) && n.temps ? `<div class="between"><span class="dim xs">PROBE</span>
        <span class="small ${n.probe.state === "unreachable" ? "t-warm" : "dim"}">${n.probe.state === "unreachable" ? "not answering · " : ""}last reading ${esc(probeAge(n.probe.age_s))} ago</span></div>` : ""}
    </div>
    <div class="nodepods"><span class="small mono" data-tip="${n.pods_wl} of your pods (bright) and ${n.pods_sys} system pods">
      <b>${n.pods}</b> <span class="dim">pods${n.vms ? ` · <b>${n.vms}</b> VM${n.vms === 1 ? "" : "s"}` : ""}</span></span>
      <div class="podgrid">${dots}</div></div>
    <div class="nodebadges">
      ${nodeAddressTags(n) ? `<div class="badgegroup"><span class="badgecap">ADDRESSES</span><div class="badge-values">${nodeAddressTags(n)}</div></div>` : ""}
      ${duties ? `<div class="badgegroup"><span class="badgecap">DUTIES</span><div class="badge-values">${duties}</div></div>` : ""}
      <div class="badgegroup"><span class="badgecap">HARDWARE</span><div class="badge-values">
        ${hardwareTags(nodeHardwareIds(n)) || '<span class="dim xs">none defined</span>'}</div></div>
      <div class="badgegroup"><span class="badgecap">WORKLOADS</span><div class="badge-values">
        ${n.workloads.length ? n.workloads.slice(0, 5).map(w =>
            `<button type="button" class="tag movable" title="Move ${esc(w)} to another host"
               onclick="event.stopPropagation();moveWorkload(${jsq(w)})">${esc(w)} <span class="mv" aria-hidden="true">⇄</span></button>`).join("")
            + (n.workloads.length > 5 ? `<span class="tag more" data-tip="${esc(n.workloads.slice(5).join(", "))}">+${n.workloads.length - 5}</span>` : "")
          : '<span class="dim xs">none</span>'}</div></div>
    </div></div>`;
}

/* Dashboard and Nodes share one matrix. Wider fleets scroll inside it;
   a single host uses the existing card instead of an empty comparison. */
const nodeComparisons = new Map();
const nodeComparisonKey = n => JSON.stringify([n.site?.id || "", n.name]);
function nodeComparisonLabel(nodes, name) {
  let prefix = nodes[0].name;
  for (const n of nodes) while (prefix && !n.name.startsWith(prefix)) prefix = prefix.slice(0, -1);
  const boundary = Math.max(prefix.lastIndexOf("-"), prefix.lastIndexOf("_"), prefix.lastIndexOf("."));
  return name.slice(boundary + 1) || name;
}
// One node is a card in a dashboard widget; the Nodes page shows it as a
// one-column table when rows were chosen there.
function nodeComparison(nodes, context, { singleCard = true } = {}) {
  if (!nodes.length) return '<div class="empty">No hosts reported.</div>';
  if (nodes.length === 1 && singleCard) return `<div class="single-node-summary">${nodeCard(nodes[0])}</div>`;
  const id = `${context}-0`;
  nodeComparisons.set(id, { nodes });
  return nodeComparisonMarkup(id);
}
/* Reuse the filesystem calculation and bar, but keep identity, tags and
   capacity on separate lines. One table row per drive aligns their bars. */
function comparisonDisk(n, d) {
  if (!d) return '<span class="dim">—</span>';
  const usage = diskUsage(n, d), lh = d.lh_size_gb > 0;
  const label = d.name || (d.device === "longhorn" ? "Longhorn folder" : (d.device || "")) || (lh ? "Longhorn filesystem" : "Unnamed drive");
  const asReported = !d.name && label === d.device;  // a device name is case-sensitive: shown as the system gives it
  return `<div class="comparison-disk">
    <b class="comparison-disk-name${asReported ? " mono" : ""}" title="${esc([label,d.device,d.model,...(d.lh_paths || [])].filter(Boolean).join(" · "))}">${esc(label)}</b>
    <div class="comparison-disk-tags">${d.system ? UI.chip("system", "neutral") : ""}${lh ? UI.chip("Longhorn", "ok") : !d.system ? UI.chip(d.role || "unassigned", "neutral") : ""}</div>
    <div class="comparison-disk-capacity mono small">${usage.capacity ? `<b>${usage.pct}%</b><span class="dim">${sizePair(usage.used,usage.capacity)}</span>` : `<span class="dim">${sizeText(d.size_gb)}</span>`}</div>
    ${usage.capacity ? `${driveBar(usage)}<span class="dim xs">Filesystem use${usage.physical ? ` · ${sizeText(usage.physical)} disk` : ""}</span>` : '<span class="dim xs">Usage unavailable</span>'}
    </div>`;
}
function nodePodDots(n) {
  const sys = Math.max(0, Number(n.pods_sys) || 0), wl = Math.max(0, Number(n.pods_wl) || 0);
  return `<div class="podgrid" role="img" aria-label="${wl} of your pods and ${sys} system pods" data-tip="${wl} of your pods (bright) · ${sys} system pods">
    ${'<i></i>'.repeat(Math.min(sys,80))}${'<i class="wl"></i>'.repeat(Math.min(wl,40))}</div>`;
}

/* The rows Nodes compares hosts by. A host that isn't Ready shows dashes
   for its readings, which would otherwise look current. */
function nodeComparisonRows(nodes) {
  const metric = (label, value, phone = true) => ({ label, values: nodes.map(value), phone });
  const percent = (value, kind) => value == null ? '<span class="dim">—</span>' : `<b class="mono">${esc(value)}%</b>${meter(value, "", kind)}`;
  const live = (n, value) => n.status === "Ready" ? value : null;
  return [
    metric("Status", n => { const h = nodeHealth(n);
      return UI.chip(h.status, h.tone, { tip: [n.status, ...h.issues, n.host_os && h.tone !== "ok" ? n.host_os.text : ""].filter(Boolean).join(" · ") }); }),
    metric("CPU", n => percent(live(n, n.cpu_pct), "cpu")),
    metric("Cores", n => `<span class="mono">${esc(n.cpu_cap ?? "—")}</span>${n.temps?.cpu_model ? `<span class="comparison-note">${esc(n.temps.cpu_model)}</span>` : ""}`),
    metric("Memory", n => n.status === "Ready" ? `${percent(n.mem_pct, "memory")}<span class="comparison-note">${sizePair(n.mem_used_gb, n.mem_cap_gb)}</span>` : percent(null)),
    ...Array.from({length:Math.max(1,...nodes.map(n => (n.disks || []).length))}, (_, i) =>
      metric(`Storage ${i + 1}`, n => n.disks?.length ? comparisonDisk(n,n.disks[i]) : i ? '<span class="dim">—</span>' : comparisonDisk(n,{device:"filesystem",name:"Node filesystem",root_fs:true,system:true,size_gb:n.fs_cap_gb}), false)),
    metric("Network", n => n.status === "Ready" ? `<span class="mono small">${ratePair(n.rx_mbps, n.tx_mbps).join(" ")}</span>` : '<span class="dim">—</span>', false),
    metric("CPU temp", n => { const t = nodeTemp(n);
      return n.status === "Ready" && t.value == null && n.probe?.state === "waiting" ? `<span class="dim">${esc(t.text)}</span>`
        : n.status !== "Ready" || t.value == null ? '<span class="dim">—</span>'
        : `<span class="mono ${tempCls(t.value)}">${esc(t.text)}</span>${t.extra ? `<span class="comparison-note">${esc(t.extra)}</span>` : ""}`; }),
    metric("Pods", n => `<b class="mono">${esc(n.pods ?? "—")}</b>${nodePodDots(n)}`),
    metric("VMs", n => `<span class="mono">${esc(n.vms ?? 0)}</span>`),
    metric("Uptime", n => `${nodeUptimeStrip(n)}<span class="comparison-note">${esc(nodeUpFor(n) || "—")}</span>`),
    metric("Addresses", n => nodeAddressTags(n) || '<span class="dim">—</span>', false),
    metric("Duties", n => nodeDutyTags(n) || '<span class="dim">—</span>', false),
    metric("Hardware", n => hardwareTags(nodeHardwareIds(n)) || '<span class="dim">None defined</span>', false),
    metric("Apps", n => `<span class="small">${esc((n.workloads || []).join(", ") || "None")}</span>`, false),
    metric("Details", n => actionBar([{label:"Open host",run:`nodeDetail(${jsq(n.name)})`,icon:"node"}]), false)
  ];
}

function nodeComparisonMarkup(id) {
  const { nodes } = nodeComparisons.get(id);
  const choices = STATE.nodeCompareSelections ||= {};
  const selected = nodes.find(n => nodeComparisonKey(n) === choices[id]) || nodes[0];
  choices[id] = nodeComparisonKey(selected);
  const columns = nodes.map(n => ({ selected: n === selected, attrs: clusterAttr(n), html:
    `<button type="button" class="comparison-select" aria-pressed="${n === selected}" aria-controls="node-comparison-detail-${id}"
      aria-label="Select ${esc(n.name)}" onclick="nodeCompareSelect(${jsq(id)},${jsq(nodeComparisonKey(n))})">
      <span class="comparison-full-name">${esc(n.name)}</span><span class="comparison-short-name">${esc(nodeComparisonLabel(nodes, n.name))}</span></button>${clusterTag(n)}` }));
  const rows = nodeComparisonRows(nodes);
  return `<section class="card flat pad0 node-comparison" id="node-comparison-${id}">
    ${nodes.length > 4 ? `<div class="cardhd comparison-scroll-hint"><span class="dim small">${nodes.length} hosts · scroll to compare →</span></div>` : ""}
    ${comparisonTable(columns, rows, "Host health and capacity")}
    <div class="node-comparison-detail" id="node-comparison-detail-${id}"${clusterAttr(selected)}>
      <div class="node-comparison-head"><div><b>${esc(selected.name)}</b> ${clusterTag(selected)}<div class="dim small">${esc(nodeUpFor(selected) || selected.status)}</div></div>
        ${actionBar([{label:"Open host",run:`nodeDetail(${jsq(selected.name)})`,icon:"node"}])}</div>
      <div class="comparison-detail-disks">${nodeDiskLines(selected)}</div>
      <div class="about-grid">
        <div><span>Memory</span><b>${sizePair(selected.mem_used_gb, selected.mem_cap_gb)}</b></div>
        <div><span>Network</span><b>${ratePair(selected.rx_mbps, selected.tx_mbps).join(" ")}</b></div>
        <div><span>Addresses</span><b>${nodeAddressTags(selected) || "—"}</b></div>
        <div><span>Duties</span><b>${nodeDutyTags(selected) || "—"}</b></div>
        <div><span>Hardware</span><b>${hardwareTags(nodeHardwareIds(selected)) || "None defined"}</b></div>
        <div><span>Apps</span><b>${esc((selected.workloads || []).join(", ") || "None")}</b></div>
      </div>
    </div></section>`;
}
window.nodeCompareSelect = (id, key) => {
  const group = nodeComparisons.get(id);
  if (!group?.nodes.some(n => nodeComparisonKey(n) === key)) return;
  (STATE.nodeCompareSelections ||= {})[id] = key;
  const element = document.getElementById(`node-comparison-${id}`);
  if (!element) return;
  const left = element.querySelector(".comparison-scroll")?.scrollLeft || 0;
  element.outerHTML = nodeComparisonMarkup(id);
  const next = document.getElementById(`node-comparison-${id}`);
  next.querySelector(".comparison-scroll").scrollLeft = left;
  enhanceActions(next);
  if (window.applyRole) applyRole();
  // Replacing the matrix retains keyboard focus on the chosen host.
  [...next.querySelectorAll('.comparison-select')].find(b => b.getAttribute('aria-pressed') === 'true')?.focus({preventScroll:true});
};

/* The last thirty days, a day a bar, as the node page draws ninety. The
   row is there on every card - saying why when there is nothing yet - so
   the cards stay the same shape. */
const uptimeTone = v => v == null ? "none" : v >= 99.9 ? "ok" : v >= 99 ? "warn" : "bad";
// A day is painted by its share outside planned maintenance, and striped as
// maintenance only when that share is fine: an outage the same day still shows.
const uptimeDayShare = d => d.up_excluding_maintenance ?? d.up;
const uptimeDayStriped = d => d.maintenance && uptimeTone(uptimeDayShare(d)) === "ok";
function nodeUptimeStrip(n) {
  const remote = remoteRow(n);
  // A failed read keeps the last good strip and says so, where a phone shows it too.
  const unread = !remote && STATE.data.nodeUptimeUnread;
  const u = remote ? null : STATE.data.nodeUptime?.nodes?.[n.name];
  const days = (u?.days || []).slice(-30);
  // The share outside maintenance Homestead caused (#535), saying when there
  // was some, so a 100% never hides it; the share Ready in the tip.
  const raw = u?.windows?.["30d"], month = u?.windows_excluding_maintenance?.["30d"] ?? raw;
  const kept = u?.maintenance_s?.["30d"] || 0, outs = (u?.outages || []).filter(o => !o.maintenance).length;
  const tip = remote ? "Open this host to view its cluster’s uptime history"
    : unread ? `Could not read uptime: ${unread}${u ? ". Showing the last reading" : ""}` : u
    ? [kept ? `Ready ${uptimePct(raw)} of the last 30 days; ${uptimeDuration(kept)} of planned maintenance left out` : "",
       outs ? `${outs} outage${outs === 1 ? "" : "s"} recorded - open the host for when` : "No outage recorded"].filter(Boolean).join(". ")
    : "No samples yet: Homestead records one every five minutes";
  return `<div class="node-upstrip" data-tip="${esc(tip)}">
    <span class="dim xs">UPTIME</span>
    <div class="uptime-strip mini" aria-label="Each of the last 30 days">${days.length
      ? days.map(d => uptimeDayStriped(d) ? `<i class="uptime-maint" data-tip="${esc(fmtDay(d.day))} · maintenance"></i>`
        : `<i class="uptime-${uptimeTone(uptimeDayShare(d))}" data-tip="${esc(fmtDay(d.day))} · ${d.up == null ? "no samples" : uptimePct(uptimeDayShare(d)) + " up"}"></i>`).join("")
      : "<i></i>".repeat(30)}</div>
    <b class="small mono uptime-${uptimeTone(month)}">${month == null ? "—" : uptimePct(month)}</b><span class="dim xs">30d${kept
      ? ` · ${uptimeDuration(kept)} maintenance` : ""}</span>${unread
      ? `<span class="dim xs uptime-unread">Could not read uptime: ${esc(unread)}${u ? " · last reading shown" : ""}</span>` : ""}</div>`;
}

/* A line per disk: its name, or its device; what it is - the system drive,
   Longhorn's, both, or nothing yet - and its use. A Longhorn folder on the
   system drive shows on that drive's line, not as a disk of its own. */
/* A drive's bar in parts: the system's use, Longhorn's data, and Longhorn's
   remaining room on it, each its own colour, the rest free. */
function driveBar(usage) {
  const w = gb => (gb / usage.capacity * 100).toFixed(2);
  const tip = [`Filesystem use ${sizePair(usage.used, usage.capacity)}`, usage.host ? `host files ${sizeText(usage.host)}` : "",
    usage.longhorn ? `Longhorn used ${sizeText(usage.longhorn)}` : "", usage.room ? `Longhorn allowance left ${sizeText(usage.room)}` : "",
    usage.remaining ? `other filesystem space ${sizeText(usage.remaining)}` : ""].filter(Boolean).join(" · ");
  return `<div class="meter split" data-tip="${esc(tip)}">${usage.host ? `<span class="seg-sys" style="width:${w(usage.host)}%"></span>` : ""}${
    usage.longhorn ? `<span class="seg-lh" style="width:${w(usage.longhorn)}%"></span>` : ""}${usage.room ? `<span class="seg-lhroom" style="width:${w(usage.room)}%"></span>` : ""}</div>`;
}

function nodeDiskLines(n) {
  const disks = n.disks || [];
  if (!disks.length) return `<div><div class="between"><span class="dim xs">DISK</span>
      <span class="small mono"><b>${n.fs_pct || 0}%</b> <span class="dim">${sizePair(n.fs_used_gb, n.fs_cap_gb)}</span></span></div>
    ${meter(n.fs_pct || 0, "", "disk")}</div>`;
  return disks.map(d => {
    const lh = d.lh_size_gb > 0, system = !!d.system || d.role === "system";
    const usage = diskUsage(n, d);
    const used = usage.capacity ? sizePair(usage.used, usage.capacity) : sizeText(d.size_gb);
    const folder = d.device === "longhorn";
    const label = d.name || (folder ? "Longhorn folder" : d.device), asReported = !d.name && !folder;
    const where = [d.name && !folder ? d.device : "", d.model, ...(d.lh_paths || [])].filter(Boolean).join(" · ");
    const tags = [d.system ? '<span class="tag slimtag">system</span>' : "",
      lh ? '<span class="tag ok slimtag">Longhorn</span>' : "",
      !lh && !d.system ? `<span class="tag slimtag ${d.role === "unused" ? "info" : ""}">${esc(d.role)}</span>` : ""].join("");
    return `<div><div class="between diskline-head"><span class="dim xs disklabel-row"><span${asReported ? ' class="mono"' : ""} title="${esc(where || d.device)}">${esc(label)}</span>${tags}</span>
      <span class="small mono">${usage.capacity ? `<b>${usage.pct}%</b> ` : ""}<span class="dim">${esc(used)}</span></span></div>
      ${usage.capacity ? `${driveBar(usage)}<div class="dim xs disk-usage-caption">Filesystem use${usage.physical ? ` · ${sizeText(usage.physical)} disk` : ""}</div>`
        : lh || system ? '<div class="dim xs disk-usage-caption">Usage unavailable</div>' : ""}
      ${lh && system && usage.capacity ? `<div class="dim xs mono drivesplit"><i class="k-sys"></i>host ${sizeText(usage.host)} <i class="k-lh"></i>Longhorn ${sizeText(usage.longhorn)} · ${sizeText(usage.room)} room</div>` : ""}</div>`;
  }).join("");
}

/* A drive's name, shown on the node card instead of its device. Kept by its
   serial, so it follows the drive to another port. */
window.diskRename = async (node, device, current) => {
  const name = await askText(`Name ${device} on ${node}`, current, { placeholder: "Media, System NVMe…" });
  if (name === null) return;
  try {
    const r = await api("/api/disks/name", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ node, device, name }) });
    toast(r.detail, "ok");
    nodeDetail(node, true);
  } catch (e) { toast(e.message, "bad"); }
};

/* A node's detail is a page of its own: /nodes?node=<name>. */
window.nodeDetail = (name, fromRoute = false) => {
  if (fromRoute && STATE.view === "nodes") return viewNodes();
  go("nodes", { params: { node: name } });
};

/* The node page: one line of how it is, then its sections in a column as
   Settings has them - Overview with its picture, Workloads, Storage,
   Hardware, Network, Host OS - one shown at a time. */
/* On this host: each app a chip that moves it. In select mode (#366) the
   same chips toggle instead and light up when picked (#589), with Select
   all, Clear and Move selected; Escape in the card ends it. */
const NODE_PICK = { on: new Set(), picked: new Map(), node: new Map() };
// The host page draws from its own answer, which STATE.data.nodes may not hold.
window.nodeRemember = n => NODE_PICK.node.set(n.name, n);
const nodeIds = n => n.workload_ids || n.workloads.map(w => `lab/${w}`);
function nodeWorkloadChips(n) {
  const ids = nodeIds(n), selecting = NODE_PICK.on.has(n.name), picked = NODE_PICK.picked.get(n.name) || new Set();
  if (!ids.length) return '<span class="dim xs">nothing of yours is scheduled here</span>';
  const chips = ids.map(id => { const [ns, name] = id.split("/");
    return selecting
      ? `<button type="button" class="tag movable" aria-pressed="${picked.has(id)}" data-id="${esc(id)}" onclick="nodeToggle(${jsq(n.name)},${jsq(id)})">${esc(name)}</button>`
      : `<button type="button" class="tag movable" title="Move ${esc(name)} to another host" onclick="moveWorkload(${jsq(name)},${jsq(ns)})">${esc(name)} <span class="mv" aria-hidden="true">⇄</span></button>`; }).join("");
  const bar = selecting
    ? `<div class="wg-selbar"><b aria-live="polite">${picked.size} selected</b>
        ${UI.button("Select all", `nodePickAll(${jsArg(n.name)},true,${JSON.stringify(ids)})`, { kind: "sm" })}
        ${UI.button("Clear", `nodePickAll(${jsArg(n.name)},false)`, { kind: "sm" })}
        ${UI.button("Move selected…", `nodeMoveSelected(${jsArg(n.name)})`, { kind: "sm pri", disabled: !picked.size, attrs: 'data-need="operator"' })}
        ${UI.button("Done", `nodeSelect(${jsArg(n.name)},false)`, { kind: "sm" })}</div>`
    : `<div class="dim xs" style="margin-top:8px">Click one to move it to another host, or ${UI.button("Select", `nodeSelect(${jsArg(n.name)},true)`, { kind: "sm" })} several.</div>`;
  return `<div onkeydown="nodeChipsKey(event,${jsq(n.name)})"><div id="nodeChips">${chips}</div>${bar}</div>`;
}
window.nodeWorkloadChips = nodeWorkloadChips;
const nodeRedraw = name => { const n = NODE_PICK.node.get(name) || (STATE.data.nodes || []).find(x => x.name === name);
  const box = $("#nodeChipsCard"); if (n && box) box.innerHTML = nodeWorkloadChips(n); };
window.nodeSelect = (name, on) => { on ? NODE_PICK.on.add(name) : (NODE_PICK.on.delete(name), NODE_PICK.picked.delete(name)); nodeRedraw(name); };
window.nodePick = (name, id, on) => { const s = NODE_PICK.picked.get(name) || new Set(); on ? s.add(id) : s.delete(id); NODE_PICK.picked.set(name, s); nodeRedraw(name); };
// A click in select mode: the chip toggles, and keeps focus once redrawn.
window.nodeToggle = (name, id) => { const s = NODE_PICK.picked.get(name) || new Set();
  nodePick(name, id, !s.has(id));
  // ids are namespace/name, Kubernetes names: safe in a quoted selector.
  $(`#nodeChips [data-id="${id}"]`)?.focus(); };
window.nodeChipsKey = (event, name) => {
  if (event.key !== "Escape" || !NODE_PICK.on.has(name) || (window.modalIsOpen && modalIsOpen())) return;
  event.preventDefault(); event.stopPropagation(); nodeSelect(name, false);
  // The chip that had focus is gone; Select takes it, so the keyboard keeps its place.
  $('#nodeChipsCard button[onclick^="nodeSelect"]')?.focus(); };
window.nodePickAll = (name, on, ids = []) => { NODE_PICK.picked.set(name, new Set(on ? ids : [])); nodeRedraw(name); };
// The review opens on another host that takes work - never this one.
window.nodeMoveSelected = name => { const s = NODE_PICK.picked.get(name); if (!s?.size) return;
  const to = ((STATE.data.nodes || []).find(x => x.name !== name && x.schedulable !== false) || {}).name || "";
  moveSeveral([...s], to, name); };

async function nodePage(name) {
  const [n] = await Promise.all([api("/api/node?name=" + encodeURIComponent(name)), loadHardwareFeatures()]);
  const summary = nodePageSummary(n);
  // A refresh keeps the page as it is - its sections read their own data -
  // and only the summary line moves.
  if (STATE.busy && $("#nodePage")?.dataset.node === name) { const line = $("#nodeSummary"); if (line) line.innerHTML = summary; return; }
  const i = n.info || {};
  const row = (l, v) => `<div class="drow"><div class="dl">${l}</div><div class="dv mono">${v}</div></div>`;
  const disks = (n.temps && n.temps.disks) || [];
  const given = Object.fromEntries((n.disks || []).map(x => [x.device, x.name || ""]));
  const diskRows = disks.map(d => { const s = d.smart || null; return `<div class="diskrow ${d.health?.state === "critical" ? "smart-failed" : ""}">
    <div class="diskidentity"><b class="mono">${given[d.name] ? `${esc(given[d.name])} <span class="dim">${esc(d.name)}</span>` : esc(d.name)} ${UI.chip(d.kind || "Disk", "neutral")}
      ${can("admin") ? `<button class="btn sm" data-tip="Name this drive" onclick="diskRename(${jsq(n.name)},${jsq(d.name)},${jsq(given[d.name] || "")})">${given[d.name] ? "Rename" : "Name"}</button>` : ""}</b><span>${esc(s?.model || d.model || d.name)}</span><span class="mono">${esc(s?.serial || d.serial || "serial unavailable")}</span></div>
    <div><span class="disklabel">CAPACITY</span><b class="mono">${Number(d.size_gb || 0).toFixed(1)} GiB</b></div>
    <div><span class="disklabel">HEALTH</span><b>${UI.chip(diskHealthWord(d.health?.state), diskHealthTone(d.health?.state) || "neutral", { tip: d.health?.summary || "no SMART data for this drive" })}</b></div>
    <div><span class="disklabel">LIFE</span><b class="mono ${lifeTone(d.health?.life_pct)}">${d.health?.life_pct == null ? "—" : esc(d.health.life_pct) + "%"}</b></div>
    <div><span class="disklabel">TEMP</span><b class="mono ${tempCls(s?.temperature_c)}">${s?.temperature_c == null ? "—" : esc(s.temperature_c) + "°C"}</b></div>
    <div><span class="disklabel">READ</span><b class="mono diskrate read">↓ ${Number(d.read_mbps || 0).toFixed(2)} MiB/s</b></div>
    <div><span class="disklabel">WRITE</span><b class="mono diskrate write">↑ ${Number(d.write_mbps || 0).toFixed(2)} MiB/s</b></div>
    <button class="btn sm" ${s ? "" : "disabled"} onclick="smartDisk(${jsq(n.name)},${jsq(d.name)})" title="${s ? "Drive health, history, and self-tests" : "SMART helper is not available on this host"}">Details</button>
  </div>`; }).join("");
  const hw = nodeHardwareIds(n);
  const hwNames = hw.map(id => (STATE.data.hardwareFeatures || []).find(f => f.id === id)?.name || id);
  const duties = n.duties || {};
  const vips = [...new Set([...(duties.management_vip || []), ...(duties.vips || [])])];
  const unused = (n.disks || []).filter(d => d.role === "unused").length;
  const drives = (n.disks || []).length || disks.length;
  const odd = (n.conditions || []).filter(c => c.type === "Ready" ? c.status !== "True" : c.status === "True");
  const fact = (label, value) => `<div><span>${label}</span><b>${value}</b></div>`;
  // Sorted by what you came to find out, as Settings is.
  const sections = [
    ["overview", "Overview", n.status === "Ready" ? esc(nodeUpFor(n)) : esc(stateWords(n.status)), `
      ${nodeAttention(n)}
      <div class="card flat node-picture">${window.Diagram ? Diagram.node(n) : ""}</div>
      <div class="card flat"><div id="nodeUptime"><div class="dim small"><span class="spin2"></span></div></div></div>
      <div class="card flat"><div class="about-grid node-facts">
        ${fact("Roles", esc(n.roles.join(", ") || "worker"))}
        ${fact("Address", `<span class="mono">${esc((n.addresses || {}).InternalIP || "—")}</span>`)}
        ${fact("CPU", cpuSummary(n))}
        ${fact("Serves", nodeDutyTags(n) || "—")}
        ${fact("Takes new work", n.schedulable ? "yes" : UI.chip("cordoned", "warn"))}
      </div>${odd.length ? `<div class="note warn" style="margin-top:10px">${odd.map(c => `<b>${esc(c.type)}</b>: ${esc(c.status)}`).join(" · ")}</div>` : ""}</div>`],
    ["workloads", "Apps and VMs", `${n.workloads.length} app${n.workloads.length === 1 ? "" : "s"} · ${n.vms || 0} VM${n.vms === 1 ? "" : "s"}`, `
      <div class="card flat">${UI.moduleHeader(`On this host`, `${n.pods_wl} of yours · ${n.pods_sys} system pods`, `${n.workloads.length ? `<button class="btn sm" data-need="admin" onclick="evacuateNode(${jsq(n.name)})">Move everything off</button>` : ""}`)}
        <div id="nodeChipsCard">${(nodeRemember(n), nodeWorkloadChips(n))}</div></div>`],
    ["storage", "Storage", nodeStorageLine(n, drives, unused), `
      <div class="card flat" id="nodeDrive" hidden></div>
      <div id="nodeDrives" class="node-drives">
      <div class="card flat"><div class="ctitle">Drives</div><div class="csub">Every drive on this host, and Longhorn's storage on it</div>
        <div id="nodeDisks" style="margin-top:10px"><div class="dim small"><span class="spin2"></span> reading disks</div></div></div>
      <div class="card flat"><div class="ctitle">Disk activity ${tip("Live host block-device throughput and SMART health, from the node probe")}</div>
        <div class="diskactivity" style="margin-top:10px">${diskRows || '<div class="dim small">No per-disk counters: the node probe provides them.</div>'}</div></div></div>`],
    ["hardware", "Hardware", esc([hwNames.join(", "), nodeTemp(n).value != null ? `${nodeTemp(n).label} ${nodeTemp(n).text}` : ""].filter(Boolean).join(" · ") || "none defined"), `
      <div class="card flat">${UI.moduleHeader(`Hardware for apps`, `What placement checks look for before a container moves or starts`, `<button class="btn sm" data-need="admin" onclick="hardwareEdit(${esc(JSON.stringify(n))})">Define</button>`)}
        <div>${hardwareTags(hw) || '<span class="dim xs">No hardware is defined.</span>'}</div></div>
      <div class="card flat" id="nodeDevices"><div class="ctitle">Devices for VMs</div><div class="csub">PCI and USB devices this host can give to its virtual machines</div>
        <div class="pt-body" style="margin-top:10px"></div></div>
      <div class="card flat"><div class="ctitle">Temperatures</div>
        <div class="node-chips" style="margin-top:10px">${n.temps && n.temps.sensors ? ([...(n.temps.hwmon || []), ...(n.temps.thermal || [])].sort((a, b) => b.celsius - a.celsius).slice(0, 14)
          .map(t => UI.chip(`${t.chip ? t.chip + " " : ""}${t.name} ${t.celsius}°`, tempTag(t.celsius) || "neutral")).join("")
          || `<span class="small">CPU <b class="${tempCls(n.temps.cpu_c)}">${n.temps.cpu_c ?? "—"}°C</b> · hottest sensor <b class="${tempCls(n.temps.max_c)}">${n.temps.max_c ?? "—"}°C</b>${tempSource(n.temps)}</span>`)
          : `<span class="dim small">No thermal data: it comes from the node probe. <a class="linkish" data-need="admin" onclick="probeInstallConfirm()">Install it</a></span>`}</div></div>`],
    ["network", "Network", `${(n.rx_mbps || 0).toFixed(1)} Mb/s in${vips.length ? ` · ${vips.length} VIP${vips.length === 1 ? "" : "s"}` : ""}`, `
      <div class="card flat"><div class="about-grid node-facts">
        ${fact("Address", `<span class="mono">${esc((n.addresses || {}).InternalIP || "—")}</span>`)}
        ${fact("Interface", `<span class="mono">${esc(n.net_iface || "—")}</span>`)}
        ${fact("In / out now", `<span class="mono">${(n.rx_mbps || 0).toFixed(2)} / ${(n.tx_mbps || 0).toFixed(2)} Mb/s</span>`)}
        ${fact("In / out since boot", `<span class="mono">${n.rx_total_gb || 0} / ${n.tx_total_gb || 0} GiB</span>`)}
      </div></div>
      <div class="card flat"><div class="ctitle">Addresses it answers for</div><div class="csub">Its own, and the VIPs that move to another host if it goes down</div>
        <div style="margin-top:10px">${nodeAddressTags(n) || "—"}</div></div>
      <div class="card flat" id="nodePorts"><div class="ctitle">Ports ${tip("Each network port's link, speed, errors and bond, read from the host by the node probe")}</div><div class="csub">Link, speed and errors, and what each port carries</div>
        <div class="ports-body" style="margin-top:10px"><div class="dim small"><span class="spin2"></span> reading ports</div></div></div>`],
    ["hostos", "Host OS", esc(n.os || "the host's own system"), `
      <div class="card flat" id="nodeHostOs"><div class="ctitle">Updates and services</div><div class="csub">Updates, restarts and services on the host itself</div>
        <div class="hos-body" style="margin-top:10px"><div class="dim small"><span class="spin2"></span> reading</div></div></div>
      <div class="card flat"><div class="about-grid node-facts">
        ${fact("Kernel", `<span class="mono">${esc(n.kernel || "—")}</span>`)}
        ${fact("Container runtime", `<span class="mono">${esc(i.containerRuntimeVersion || "—")}</span>`)}
        ${fact("Kubelet", `<span class="mono">${esc(i.kubeletVersion || "—")}</span>`)}
        ${fact("Image storage", `<span class="mono">${n.img_used_gb || 0} GiB</span>`)}
      </div>
      <div class="node-chips" style="margin-top:10px">${(n.conditions || []).map(c => UI.chip(`${c.type}: ${c.status}`, c.type === "Ready" ? (c.status === "True" ? "ok" : "bad") : (c.status === "True" ? "warn" : "neutral"))).join("")}</div></div>`],
  ];
  // ?section= opens one directly: an alert about a port links to Network.
  const asked = new URLSearchParams(location.search).get("section");
  if (asked && sections.some(([id]) => id === asked) && !STATE.busy) { STATE.nodeSection = asked; STATE.nodeSectionOpen = true; }
  const current = sections.some(([id]) => id === STATE.nodeSection) ? STATE.nodeSection : "overview";
  paint(`${UI.pageHeader(`${esc(n.name)}`, `${esc(n.roles.join(" · ") || "worker")} · <span class="mono">${esc((n.addresses || {}).InternalIP || "")}</span>`, `<button class="btn" data-need="admin" onclick="nodeShell(${jsq(n.name)})" title="A root shell on the host itself, as SSH would give">${icon("console")}Terminal</button>
        <button class="btn pri" onclick="nodeActions(${jsq(n.name)})">Host actions</button>`,
      // One tap up, as each section has (#382): the menu was the only way back on a phone.
      { extraHtml: `<button type="button" class="page-back" onclick="go('nodes')">‹ Hosts</button>` })}
    <div id="nodePage" data-node="${esc(n.name)}">
      <div class="sumline" id="nodeSummary">${summary}</div>
      ${UI.workspace(UI.workspaceNav(sections.map(([key,label,descriptionHtml]) => ({key,label,descriptionHtml})),
        {label:`${n.name} sections`,selected:current,onSelect:key => `nodeSectionGo(${jsArg(key)})`}),
        sections.map(([id, , , body]) => `<div class="node-pane" data-pane="${id}"${id === current ? "" : " hidden"}>${body}</div>`).join(""),
        {open:STATE.nodeSectionOpen,backLabel:"All sections",currentLabel:sections.find(([id])=>id===current)?.[1],back:"nodeSectionGo('')"})}
    </div>`);
  window.__disksModal = false;
  nodeDisksPaint(n.name);
  nodeUptimePaint(n);
  if (window.nodeHostOsPaint) nodeHostOsPaint(n.name, true);
  if (window.nodeDevicesPaint) nodeDevicesPaint(n.name);
  if (window.nodePortsPaint) nodePortsPaint(n);
}
function nodePageSummary(n) {
  const h = nodeHealth(n), known = value => h.ready && value != null && value !== "";
  const pair = (used, cap) => known(used) && known(cap) ? `${esc(used)}/${esc(cap)} GiB` : "—";
  return `<span class="sumitem">${UI.chip(h.status, h.tone, { tip: [n.status, ...h.issues].join(" · ") })}</span>
    <span class="sumitem">CPU <b>${known(n.cpu_pct) ? `${esc(n.cpu_pct)}%` : "—"}</b> of ${esc(n.cpu_cap ?? "—")}</span>
    <span class="sumitem">RAM <b>${pair(n.mem_used_gb, n.mem_cap_gb)}</b></span>
    <span class="sumitem" data-tip="${esc(NODE_FS_TIP)}">Node filesystem <b>${pair(n.fs_used_gb, n.fs_cap_gb)}</b></span>
    ${h.ready && nodeTemp(n).value != null ? `<span class="sumitem">${nodeTemp(n).label} <b class="${tempCls(nodeTemp(n).value)}">${esc(nodeTemp(n).text)}</b>${nodeTemp(n).extra ? ` <span class="dim xs">${esc(nodeTemp(n).extra)}</span>` : ""}</span>` : ""}
    ${n.schedulable ? "" : `<span class="sumitem">${UI.chip("cordoned", "warn")}</span>`}`;
}
/* One section at a time, as in Settings; on a phone the list comes first. */
window.nodeDriveBack = () => {
  const box = $("#nodeDrive"), list = $("#nodeDrives");
  if (box) { box.hidden = true; box.innerHTML = ""; }
  if (list) list.hidden = false;
};
window.nodeSectionGo = id => {
  if (id) STATE.nodeSection = id;
  STATE.nodeSectionOpen = !!id;
  const layout = $("#nodePage .settings-layout");
  if (!layout) return;
  UI.selectWorkspace(layout, id, {paneSelector:".node-pane[data-pane]"});
  if (!id) return;
  window.scrollPageTop();
};

window.smartDisk = async (node, disk) => {
  // On the node's page a drive opens in its Storage section; elsewhere, a dialog.
  let target;
  if (STATE.view === "nodes" && $("#nodePage") && $("#nodeDrive")) {
    nodeSectionGo("storage");
    $("#nodeDrives").hidden = true;
    const box = $("#nodeDrive");
    box.hidden = false;
    box.innerHTML = `<a class="linkish node-drive-back" onclick="nodeDriveBack()">‹ All drives</a><div id="nodeDriveBody"><div class="empty"><span class="spin2"></span>reading SMART data</div></div>`;
    target = $("#nodeDriveBody");
  } else {
    childModal(`Drive · ${disk}`, `<div class="empty"><span class="spin2"></span>reading SMART data</div>`, true);
    target = $("#mbody");
  }
  try {
    const s = await api(`/api/node/smart?node=${encodeURIComponent(node)}&disk=${encodeURIComponent(disk)}`);
    const tests = s.self_tests || [], active = s.test?.active;
    const health = s.health_assessment || { state: "unavailable", issues: [], summary: "" };
    const nvme = s.nvme || null;
    const stat = (label, value, tone = "") => `<div class="smartstat ${tone}"><span>${label}</span><b class="mono">${esc(smartMetric(value))}</b></div>`;
    target.innerHTML = `
      <div class="between smart-drive-head"><div><div class="ctitle">${esc(s.model || disk)}</div><div class="csub mono">${esc(s.path || "/dev/" + disk)} · ${esc(s.serial || "serial unavailable")} · ${esc(s.protocol || "protocol unknown")}</div></div>
        ${UI.chip(diskHealthWord(health.state), diskHealthTone(health.state) || "neutral")}</div>
      ${s.available ? "" : `<div class="note"><b>SMART unavailable.</b> ${esc(s.unavailable_reason || "This drive or USB bridge does not expose SMART data.")}</div>`}
      ${health.issues?.length ? `<div class="note ${health.state === "critical" ? "bad" : "warn"}">
        <b>${health.state === "critical" ? "This drive needs replacing." : "This drive needs watching."}</b>
        <ul class="diskfindings">${health.issues.map(issue =>
          `<li>${UI.chip(issue.severity, issue.severity === "critical" ? "bad" : "warn")} ${esc(issue.reason)}</li>`).join("")}</ul>
        <span class="dim xs">Measured against the drive-health policy in Settings.</span></div>`
      : s.available ? `<div class="note good"><b>No defects reported.</b> Every counter is within the
          thresholds set in Settings${s.health ? `, and the drive's own overall-health check says ${esc(s.health)}` : ""}.</div>` : ""}
      ${active ? UI.callout("info", "Self-test running", `<p>${esc(s.test.status || "In progress")}${s.test.remaining_percent == null ? "" : ` · ${esc(s.test.remaining_percent)}% remaining`}</p>`) : ""}
      ${health.stale_probe ? `<div class="note warn"><b>The node probe predates this release.</b>
        Wear and life figures come from the probe, which has not picked up the current scripts yet.
        Homestead updates it when it starts; if this persists, check Settings → Homestead.</div>` : ""}
      <div class="smartstats">
        ${stat("Life remaining", health.life_pct == null ? null : health.life_pct + "%", lifeTone(health.life_pct))}
        ${health.spare_pct == null ? "" : stat("Spare blocks", health.spare_pct + "%", lifeTone(health.spare_pct))}
        ${stat("Temperature", s.temperature_c == null ? null : s.temperature_c + "°C", tempTag(s.temperature_c))}
        ${stat("Power-on hours", s.power_on_hours)}
        ${nvme
          // An NVMe drive keeps different books from an ATA one, so it is asked
          // its own questions rather than shown four "unsupported" rows.
          ? `${stat("Endurance used", nvme.percentage_used == null ? null : nvme.percentage_used + "%",
               +(nvme.percentage_used || 0) >= 90 ? "bad" : +(nvme.percentage_used || 0) >= 75 ? "warn" : "")}
             ${stat("Media errors", s.media_errors, +(s.media_errors || 0) ? "bad" : "")}
             ${stat("Unsafe shutdowns", nvme.unsafe_shutdowns)}
             ${stat("Data written", nvme.data_units_written == null ? null
               : (nvme.data_units_written * 512000 / 1024 ** 4).toFixed(2) + " TiB")}`
          : `${stat("Reallocated", s.reallocated, +(s.reallocated || 0) ? "warn" : "")}
             ${stat("Pending", s.pending, +(s.pending || 0) ? "bad" : "")}
             ${stat("Uncorrectable", s.uncorrectable, +(s.uncorrectable || 0) ? "bad" : "")}
             ${stat("Errors", s.error_count, +(s.error_count || 0) ? "bad" : "")}`}
      </div>
      ${health.life_basis ? `<div class="drow"><div class="dl">Life measured from</div><div class="dv">${esc(health.life_basis)}</div></div>` : ""}
      <div class="drow"><div class="dl">Firmware</div><div class="dv mono">${esc(s.firmware || "—")}</div></div>
      <div class="drow"><div class="dl">SMART enabled</div><div class="dv">${s.smart_enabled == null ? "not reported" : s.smart_enabled ? "yes" : "no"}</div></div>
      <div class="drow"><div class="dl">Drive's own health check ${tip("smartctl's overall-health bit. Drives report PASSED until failure is imminent, which is why Homestead judges the counters as well.")}</div>
        <div class="dv">${UI.chip(s.health || "not reported", smartTone(s.health) || "neutral")}</div></div>
      <div class="sec">Self-test history</div>
      ${tests.length ? `<div class="tblwrap"><table class="tbl stack dense"><thead><tr><th>Test</th><th>Result</th><th>Drive hours</th></tr></thead><tbody>${tests.map(t => `<tr><td>${esc(t.type || "Self-test")}</td><td>${UI.chip(t.status || "Unknown", smartTestTone(t.status) || "neutral")}</td><td class="mono">${esc(t.lifetime_hours ?? "—")}</td></tr>`).join("")}</tbody></table></div>` : '<div class="empty small">The drive has no self-test history.</div>'}
      <div class="smart-actions"><div><b>Run a drive self-test</b><div class="dim xs">Tests run inside the drive. A long test can reduce disk performance while active.</div></div><div class="row">
        <button class="btn" data-need="admin" ${!s.available || active || !(s.supported_tests || []).includes("short") ? "disabled" : ""} onclick="smartStartConfirm(${jsq(node)},${jsq(disk)},'short')">Short test</button>
        <button class="btn" data-need="admin" ${!s.available || active || !(s.supported_tests || []).includes("long") ? "disabled" : ""} onclick="smartStartConfirm(${jsq(node)},${jsq(disk)},'long')">Long test</button>
      </div></div>
      <div class="note"><b>USB and NVMe caveat.</b> Some USB bridges hide SMART commands; NVMe exposes different counters from ATA/SATA. Homestead shows unsupported values explicitly instead of treating them as zero.</div>`;
  } catch (e) { target.innerHTML = UI.error(e, { retry: `smartDisk(${jsArg(node)},${jsArg(disk)})`, dialog: target.id === "mbody" }); }
};

/* The probe is two containers. One reads sensors with nothing special
   granted to it; the other must be privileged to reach the drives at all, so
   installing it is stated plainly and asked for rather than assumed. */
window.probeInstallConfirm = () => childModal("Install the node probe?", `
  <p>The probe runs one pod on every host and reports what Kubernetes does not:
    temperatures, host devices, per-disk throughput, and SMART drive health.</p>
  <div class="note"><b>Telemetry container.</b> Mounts <span class="mono">/sys</span>,
    <span class="mono">/proc</span> and <span class="mono">/dev</span> read-only, drops every
    capability, runs with a read-only root and cannot escalate privilege.</div>
  <div class="note warn"><b>SMART requires a privileged container</b> to read drive health. It uses a read-only root, isolated PID/IPC/network namespaces and signed requests. ${tip("Direct block-device access is required for the host’s varying drive types. Without the SMART container, drive-health data is unavailable.")}</div>
  ${UI.actions(`<button class="btn pri" data-need="admin" onclick="probeInstall()">Install probe</button>
    <button data-dialog-dismiss="true" class="btn" onclick="closeModal()">Not now</button>`)}`);

window.probeInstall = async () => {
  try {
    const result = await api("/api/node/probe/install", { method: "POST",
      headers: { "Content-Type": "application/json" }, body: "{}" });
    toast(result.detail || "node probe installed", "ok");
    closeModal(); resetPaint(); viewNodes();
  } catch (e) { toast(e.message, "bad"); }
};

window.probeRemove = async () => {
  if (!(await ask("Remove the node probe from every host?" + String.fromCharCode(10, 10)
      + "Temperatures, drive health and disk throughput stop being reported.", { title: "Remove the node probe", ok: "Remove", danger: true }))) return;
  try {
    const result = await api("/api/node/probe/remove", { method: "POST",
      headers: { "Content-Type": "application/json" }, body: "{}" });
    toast(result.detail || "node probe removed", "ok");
    closeModal(); resetPaint(); viewNodes();
  } catch (e) { toast(e.message, "bad"); }
};

window.allocationProbeSettings = async () => {
  try {
    const current = await api("/api/node/probe/allocation");
    window._allocationProbeReview = current;
    const capacity = current.capacity || {blocked:true, blockers:["Capacity is unavailable. Refresh before enabling."], warnings:[]};
    childModal("VM placement checks", `
      <p>Checks whether dedicated CPUs and memory are available together before starting a NUMA VM.</p>
      <div class="note">${esc(current.detail || "Status unavailable")}</div>
      ${!current.installed ? '<p>Install the node probe first in Cluster → Add-ons.</p>' : `
      <p>Uses an extra 32–96 MiB RAM per host. Saving briefly restarts host monitoring. Apps and VMs are not restarted.</p>
      ${capacity.blockers.map(text=>`<div class="note warn">${esc(text)}</div>`).join("")}
      ${capacity.warnings.length ? `<div class="note warn">${capacity.warnings.map(esc).join("<br>")}</div><label class="vip-check"><input id="allocationProbeCapacity" type="checkbox"><span>Continue despite these capacity warnings.</span></label>` : ""}
      <details class="vm-placement-advanced"><summary>Advanced · host access and socket location</summary>
      <p class="dim small">The helper runs as root with no Linux capabilities and a read-only filesystem. It reads kubelet allocations through a dedicated host socket, authenticated with a separate key. The socket mount is read-only, but permits RPC calls; this helper only implements read operations. It receives no Kubernetes API token.</p>
      <label class="f">Kubelet socket directory ${tip("Use the kubelet root directory’s pod-resources subdirectory. It must already exist on every selected probe host and contain kubelet.sock. Custom k3s/RKE2 installations may use a different root; do not enter the whole kubelet directory.")}
        <input id="allocationProbeDirectory" class="input mono" value="${esc(current.directory || "/var/lib/kubelet/pod-resources")}" autocomplete="off" spellcheck="false"></label>
      <p class="dim small">This directory must exist on every probe host. Missing or unsupported data blocks new NUMA starts; it is never treated as free capacity. Multi-NUMA hosts need Static CPU/memory management and pod-scoped single-numa-node topology policy. Checks do not reserve resources.</p>
      </details>
      <label class="vip-check"><input id="allocationProbeConsent" type="checkbox"><span>Allow read-only host allocation checks and restart monitoring.</span></label>
      ${current.enabled ? '<button class="btn sm" onclick="allocationProbeCheck(this)">Check hosts</button><div id="allocationProbeDiagnostics" role="status" aria-live="polite"></div>' : ""}
      ${UI.actions(`<button class="btn pri" data-need="admin" ${capacity.blocked ? "disabled" : ""} onclick="allocationProbeSave(true,this)">${current.enabled ? "Save settings" : "Enable checks"}</button>
        ${current.enabled ? '<button class="btn" data-need="admin" onclick="allocationProbeSave(false,this)">Disable checks</button>' : ""}
        <button data-dialog-dismiss="true" class="btn" onclick="modalBack()">Cancel</button>`)}`}`, false, "operation-review");
  } catch (e) { toast(e.message, "bad"); }
};

window.allocationProbeCheck = async button => {
  button.disabled = true;
  const target = $("#allocationProbeDiagnostics");
  target.textContent = "Checking hosts…";
  try {
    const nodes = STATE.data?.nodes || [];
    if (!nodes.length) { target.textContent = "Open Hosts to refresh the host list, then check again."; return; }
    target.innerHTML = "";
    for (const node of nodes) {
      const result = await api("/api/node/probe/allocation/check?node=" + encodeURIComponent(node.name));
      target.innerHTML += `<p><b>${esc(node.name)}</b> · ${esc(result.detail)}</p>`;
    }
  } catch (_) { target.textContent = "Host checks are unavailable. Refresh and try again."; }
  finally { button.disabled = false; }
};

window.allocationProbeSave = async (enabled, button) => {
  const current = window._allocationProbeReview;
  if (!current?.installed || !current.uid || !current.resource_version) return toast("Reload the probe configuration first", "bad");
  if (enabled && !$("#allocationProbeConsent")?.checked) return toast("Confirm socket access and the probe restart first", "bad");
  if (enabled && current.capacity?.blocked) return toast("Resolve the host checks before enabling", "bad");
  if (enabled && current.capacity?.warnings?.length && !$("#allocationProbeCapacity")?.checked) return toast("Review and acknowledge the capacity warnings first", "bad");
  if (!enabled && !(await ask("Disable allocation collection? Probe pods restart; NUMA starts will remain blocked without verified allocation evidence. Apps and their volumes are unchanged.", { title: "Disable allocation collection", ok: "Disable", danger: true }))) return;
  const body = {enabled, directory: $("#allocationProbeDirectory")?.value || "",
    uid: current.uid, resource_version: current.resource_version, acknowledge_host_access: enabled,
    confirm_capacity: !!$("#allocationProbeCapacity")?.checked, capacity_review: current.capacity?.fingerprint};
  window._allocationProbeReview = null; // uncertain saves always require a fresh read
  if (button) button.disabled = true;
  try {
    const result = await api("/api/node/probe/allocation", {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(body)});
    toast(result.detail, "ok");
    modalBack();
  } catch (e) { toast(`${e.message}. Reopen VM allocation to read the current configuration before another change.`, "bad"); }
};

window.smartStartConfirm = (node, disk, type) => childModal(`Start ${type} SMART test?`, `
  <p>This asks <b>${esc(node)} / ${esc(disk)}</b> to run its built-in ${esc(type)} self-test.</p>
  <div class="note">The test does not erase data, but a long test can reduce storage performance and may take hours. Progress and the final drive result remain in Activity.</div>
  ${UI.actions(`<button class="btn pri" onclick="smartStart(${jsq(node)},${jsq(disk)},${jsq(type)})">Start ${esc(type)} test</button><button data-dialog-dismiss="true" class="btn" onclick="modalBack()">Cancel</button>`)}`);

window.smartStart = async (node, disk, type) => {
  try {
    const result = await api("/api/node/smart/test", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ node, disk, test: type }) });
    toast(result.message || `${type} SMART test started`, "ok");
    // Back to the drive, redrawn so the running test shows.
    modalBack(); smartDisk(node, disk);
  } catch (e) { toast(e.message, "bad"); }
};
window.hardwareEdit = n => modal("Hardware · " + n.name, `
  <p class="muted small">Choose which configured features apps and VMs may use on this host. Saving writes explicit Kubernetes labels; unchecked features are explicitly disabled even if detected.</p>
  <div class="hwchoices">${hardwareChoices("hw_node", nodeHardwareIds(n))}</div>
  <div class="note">${(n.hardware_inventory || []).map(x => `<div><b>${esc(x.name)}</b> · ${x.detected ? "detected" : "not detected"} · ${x.explicit == null ? "automatic" : x.explicit ? "enabled" : "disabled"}</div>`).join("") || "The node probe has not reported hardware inventory yet."}</div>
  ${UI.actions(`<button class="btn pri" onclick="hardwareSave(${jsq(n.name)})">Save</button><button data-dialog-dismiss="true" class="btn" onclick="closeModal()">Cancel</button>`)}`);
window.hardwareSave = async node => {
  const body = { node, features: selectedHardware("hw_node") };
  try { await api("/api/node/hardware", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
    toast(`Hardware updated on ${node}`, "ok"); closeModal(); setTimeout(() => { resetPaint(); viewNodes(); }, 600); } catch (e) { toast(e.message, "bad"); }
};

window.hardwareFeatureSettings = async () => {
  const defs = await loadHardwareFeatures(true);
  modal("Hardware features", `
    <div class="between"><p class="muted small">Define reusable passthrough and placement features. iGPU remains built in; everything else is editable.</p>
      <div class="row"><button class="btn pri sm" data-need="admin" onclick="hardwareFeatureEdit()">＋ Add feature</button></div></div>
    <div class="featurelist" style="margin-top:14px">${defs.map(f => `<div class="card flat featurecard">
      <div class="between"><div><b>${esc(f.name)}</b> ${f.builtin ? UI.chip("built in", "neutral") : ""}<div class="dim xs mono">${esc(f.id)} · ${esc(f.label)}</div></div>
      ${f.builtin ? "" : `<div class="row"><button class="btn sm" onclick="hardwareFeatureEdit(${jsq(f.id)})">Edit</button><button class="btn sm danger" onclick="hardwareFeatureDelete(${jsq(f.id)})">Delete</button></div>`}</div>
      <div class="small" style="margin-top:9px">${UI.chip(`${f.host_path} → ${f.container_path}`, "hw")} ${UI.chip(f.path_type, "neutral")}</div>
      ${f.usb_ids?.length ? `<div class="dim xs" style="margin-top:7px">USB IDs: ${esc(f.usb_ids.join(", "))}</div>` : ""}
      ${f.description ? `<div class="dim small" style="margin-top:7px">${esc(f.description)}</div>` : ""}</div>`).join("")}</div>
    <div class="note" style="margin-top:14px">A feature adds a node selector and mounts its host path into the container. USB IDs control host detection; the configured path controls passthrough.</div>`, true);
};
/* After a save or delete in the feature editor, the list it opened from is
   shown again, redrawn with the change. */
function backToHardwareList() {
  if (/hardware feature$/.test($("#mtitle").textContent)) modalBack();
  hardwareFeatureSettings();
}

window.hardwareFeatureEdit = async id => {
  const old = id ? hardwareDef(id) : { id: "", name: "", description: "", host_path: "/dev/", container_path: "/dev/", path_type: "CharDevice", usb_ids: [] };
  // Unread hosts are not hosts without devices (#620): the browser says so,
  // and the paths can still be typed in.
  let hostsUnread = "";
  if (!(STATE.data.nodes || STATE.data.ov?.nodes)?.length) {
    try { STATE.data.nodes = await api("/api/nodes"); } catch (e) { hostsUnread = e.message; }
  }
  const nodes = STATE.data.nodes || STATE.data.ov?.nodes || [];
  childModal((id ? "Edit" : "Add") + " hardware feature", `
    <div class="f2"><div class="f"><label>Feature ID ${tip("Stable internal ID used by apps and VMs and node labels. It cannot be changed after creation.")}</label><input id="hf_id" value="${esc(old.id)}" ${id ? "disabled" : ""} placeholder="hailo-8"></div>
      <div class="f"><label>Display name</label><input id="hf_name" value="${esc(old.name)}" placeholder="Hailo-8 accelerator"></div></div>
    <div class="f"><label>Description</label><input id="hf_desc" value="${esc(old.description || "")}" placeholder="Optional note for operators"></div>
    <div class="f2"><div class="f"><label>Host device path ${tip("Path present on the Harvester node. It is also used for automatic detection when USB IDs are blank.")}</label><input id="hf_host" value="${esc(old.host_path)}" placeholder="/dev/hailo0"></div>
      <div class="f"><label>Path inside container</label><input id="hf_container" value="${esc(old.container_path)}" placeholder="/dev/hailo0"></div></div>
    <div class="f2"><div class="f"><label>Path type</label><select id="hf_type">${["CharDevice","Directory","BlockDevice","Socket","File"].map(x => `<option ${x === old.path_type ? "selected" : ""}>${x}</option>`).join("")}</select></div>
      <div class="f"><label>USB vendor:product IDs ${tip("Optional comma-separated VID:PID pairs, for example 18d1:9302. A matching device marks the feature present on that host.")}</label><input id="hf_usb" value="${esc((old.usb_ids || []).join(", "))}" placeholder="18d1:9302"></div></div>
    <div class="device-browser">
      <div class="between"><div><b>Browse a host</b><div class="dim xs">Choose the host whose <span class="mono">/dev</span> tree you want to map.</div></div>
        ${UI.chip("read only", "neutral")}</div>
      ${hostsUnread ? `<div style="margin-top:10px">${UI.callout("warn", "Could not read the hosts", esc(hostsUnread))}</div>` : `
      <div class="f2" style="margin-top:10px"><div class="f"><label>Host</label><select id="hf_browse_node" onchange="hardwareBrowseRender()">
        ${nodes.map(n => `<option value="${esc(n.name)}">${esc(n.name)}${n.status !== "Ready" ? " · " + esc(n.status) : ""}</option>`).join("")}</select></div>
        <div class="f"><label>Filter devices</label><input id="hf_browse_q" placeholder="coral, dri, video, tty…" oninput="hardwareBrowseRender()"></div></div>
      <div id="hf_browse_results"></div>`}
    </div>
    <div class="note">Device passthrough makes the container privileged. Use the narrowest stable /dev path available. For a USB VID:PID, /dev/bus/usb is commonly required because bus addresses can change after reboot.</div>
    ${UI.actions(`<button class="btn pri" onclick="hardwareFeatureSave(${jsq(id || "")} )">Save feature</button><button data-dialog-dismiss="true" class="btn" onclick="modalBack()">Cancel</button>`)}`, true);
  hardwareBrowseRender();
};
window.hardwareBrowseRender = () => {
  const host = $("#hf_browse_node")?.value;
  const q = ($("#hf_browse_q")?.value || "").trim().toLowerCase();
  const node = (STATE.data.nodes || STATE.data.ov?.nodes || []).find(n => n.name === host);
  const dev = node?.temps?.devices || {};
  const pathEntries = (dev.path_entries || (dev.paths || []).map(path => ({ path, type: "File" })))
    .map(x => ({ kind: "path", path: x.path, type: x.type || "File", search: `${x.path} ${x.type}`.toLowerCase() }));
  const usb = (dev.usb || []).map(x => ({ kind: "usb", path: x.path || "/dev/bus/usb", type: "Directory",
    id: `${x.vid}:${x.pid}`, name: x.name || "USB device",
    search: `${x.name || ""} ${x.vid}:${x.pid} ${x.path || ""}`.toLowerCase() }));
  window.__hardwareBrowseItems = usb.concat(pathEntries).filter(x => !q || x.search.includes(q)).slice(0, 160);
  const el = $("#hf_browse_results");
  if (!el) return;
  if (!node?.temps) {
    el.innerHTML = '<div class="empty small">No probe data from this host yet.</div>'; return;
  }
  el.innerHTML = window.__hardwareBrowseItems.length ? `<div class="device-list">${window.__hardwareBrowseItems.map((x, i) =>
    `<button class="device-row" type="button" onclick="hardwareUsePath(${i})"><span><b>${esc(x.kind === "usb" ? x.name : x.path)}</b>
      <span class="dim xs mono">${x.kind === "usb" ? `${esc(x.id)} · ${esc(x.path || "/dev/bus/usb")}` : esc(x.type)}</span></span>${UI.chip("Use", "neutral")}</button>`).join("")}</div>
    ${window.__hardwareBrowseItems.length >= 160 ? '<div class="dim xs" style="margin-top:7px">Showing the first 160 matches — type a filter to narrow the list.</div>' : ""}`
    : '<div class="empty small">No matching device paths on this host.</div>';
};
window.hardwareUsePath = i => {
  const x = (window.__hardwareBrowseItems || [])[i];
  if (!x) return;
  if (x.kind === "usb") {
    $("#hf_host").value = "/dev/bus/usb"; $("#hf_container").value = "/dev/bus/usb"; $("#hf_type").value = "Directory";
    const input = $("#hf_usb"), ids = input.value.split(/[\s,]+/).filter(Boolean);
    if (!ids.includes(x.id)) ids.push(x.id);
    input.value = ids.join(", ");
  } else {
    $("#hf_host").value = x.path; $("#hf_container").value = x.path; $("#hf_type").value = x.type;
  }
  toast(`Mapped ${x.kind === "usb" ? x.name : x.path}`, "ok");
};
window.hardwareFeatureSave = async oldId => {
  const feature = { id: $("#hf_id").value.trim(), name: $("#hf_name").value.trim(), description: $("#hf_desc").value.trim(),
    host_path: $("#hf_host").value.trim(), container_path: $("#hf_container").value.trim(), path_type: $("#hf_type").value,
    usb_ids: $("#hf_usb").value.split(/[\s,]+/).filter(Boolean) };
  const custom = (STATE.data.hardwareFeatures || []).filter(x => !x.builtin && x.id !== oldId).map(x => ({ ...x }));
  custom.push(feature);
  try { await api("/api/hardware/features", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ features: custom }) });
    STATE.data.hardwareFeatures = null; toast(`${feature.name} saved`, "ok"); backToHardwareList(); }
  catch (e) { toast(e.message, "bad"); }
};
window.hardwareFeatureDelete = async id => {
  const f = hardwareDef(id);
  if (!(await ask(`Delete hardware feature "${f.name}"?\n\nExisting apps and VMs using its node label are not changed.`, { title: "Delete hardware feature", ok: "Delete", danger: true }))) return;
  const custom = (STATE.data.hardwareFeatures || []).filter(x => !x.builtin && x.id !== id);
  try { await api("/api/hardware/features", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ features: custom }) });
    STATE.data.hardwareFeatures = null; toast(`${f.name} deleted`, "ok"); backToHardwareList(); }
  catch (e) { toast(e.message, "bad"); }
};

async function viewNodes() {
  const page = new URLSearchParams(location.search).get("node");
  if (page) return nodePage(page);
  const [n, up] = await Promise.all([api("/api/nodes"), api("/api/nodes/uptime").catch(e => ({ unread: e.message })), loadHardwareFeatures()]);
  STATE.data.nodes = n;
  noteNodeUptime(up);
  const layout = viewLayout("nodes");
  paint(`${UI.pageHeader(`Hosts`, `${n.length} host${n.length === 1 ? "" : "s"} · ${n.length > 1 && layout === "rows" ? "compare health and capacity" : "health and capacity by host"}${metricsNotice(n)}`, `${layoutSwitch("nodes", "viewNodes", { phone: true })}
      ${moreMenu([
        {label:"Add a host…",run:"clusterOnboarding()",need:"admin"},
        STATE.platform && !STATE.platform.harvester && {label:"OS updates",run:"osUpdates()"},
        {label:"Hardware features",run:"hardwareFeatureSettings()",need:"admin"}
      ])}
      ${n.length > 1 ? `<button class="btn" data-need="operator" onclick="balanceHosts()" data-tip="Move containers and volume copies so hosts carry similar loads">${icon("layers")}Balance hosts…</button>` : ""}`)}
   ${layout === "cards" ? `<div class="nodegrid stagger">${n.map(nodeCard).join("")}</div>` : nodeComparison(n, "nodes", { singleCard: false })}`);
}

/* ---------------- a node's own terminal ----------------
   A root shell on the host, as SSH would give it: a small helper pod on the
   node enters the host's namespaces, and xterm.js draws the terminal, so
   full-screen tools - top, vi, less - work. Admin only; each session is
   written to the console audit log with the node's name. */
let xtermLoading = null;
function loadXterm() {
  if (window.Terminal && window.FitAddon) return Promise.resolve();
  if (xtermLoading) return xtermLoading;
  const load = (tag, attrs) => new Promise((resolve, reject) => {
    const el = Object.assign(document.createElement(tag), attrs);
    el.onload = resolve;
    el.onerror = () => { xtermLoading = null; reject(new Error("the terminal could not be loaded")); };
    document.head.appendChild(el);
  });
  const v = `?v=${HOMESTEAD_VERSION}`;
  xtermLoading = Promise.all([
    load("link", { rel: "stylesheet", href: `/vendor/xterm/xterm.css${v}` }),
    load("script", { src: `/vendor/xterm/xterm.js${v}` }).then(() => load("script", { src: `/vendor/xterm/addon-fit.js${v}` })),
  ]);
  return xtermLoading;
}

window.nodeShell = async name => {
  modal(`Terminal · ${name}`, `<div class="console-security">Admin only · a root shell on ${esc(name)} itself. Session start and stop are
      audited; commands and output are not recorded.</div>
    <div class="consolestate" id="nodeShellState"><span class="spin2"></span> starting a helper on ${esc(name)}…</div>
    <div class="nodeterm" id="nodeTerm"></div>
    <div class="row" style="margin-top:8px"><button class="btn sm" id="nodeShellAgain" onclick="nodeShell(${jsq(name)})" hidden>${icon("restart")}Reconnect</button></div>`, true);
  const state = text => { const el = $("#nodeShellState"); if (el) el.innerHTML = text; };
  try {
    await Promise.all([loadXterm(), api("/api/node/shell/prepare", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ node: name }) })]);
  } catch (e) { state(`<span class="badtext">${esc(e.message)}</span>`); $("#nodeShellAgain").hidden = false; return; }
  if (!$("#nodeTerm")) return;
  if (window.__nodeTerm) { try { window.__nodeTerm.dispose(); } catch (_) {} }
  if (window.__nodeSocket) window.__nodeSocket.close();
  const css = getComputedStyle(document.documentElement);
  const term = new Terminal({ cursorBlink: true, fontSize: 13, scrollback: 5000, convertEol: false,
    fontFamily: css.getPropertyValue("--mono").trim() || "ui-monospace, Menlo, Consolas, monospace",
    theme: { background: "#0b0b0d", foreground: "#e8e8ea" } });
  const fit = new FitAddon.FitAddon();
  term.loadAddon(fit);
  term.open($("#nodeTerm"));
  fit.fit();
  window.__nodeTerm = term;
  const protocol = location.protocol === "https:" ? "wss:" : "ws:";
  const socket = new WebSocket(fleetSocketUrl(`${protocol}//${location.host}/api/node/shell?node=${encodeURIComponent(name)}`));
  window.__nodeSocket = socket;
  const send = value => { if (socket.readyState === WebSocket.OPEN) socket.send(JSON.stringify(value)); };
  const resize = () => { try { fit.fit(); } catch (_) {} send({ type: "resize", cols: term.cols, rows: term.rows }); };
  socket.onopen = () => { state(`connected · root on ${esc(name)}`); resize(); term.focus(); };
  socket.onmessage = event => {
    let message;
    try { message = JSON.parse(event.data); } catch (_) { return; }
    if (message.type === "output") term.write(message.data);
    else if (message.type === "error") term.write(`\r\n\x1b[31m${message.data || "error"}\x1b[0m\r\n`);
    else if (message.type === "disconnected") state(`disconnected · ${esc(message.reason || "session ended")}`);
  };
  socket.onclose = () => {
    if (window.__nodeSocket !== socket) return;
    state("disconnected");
    const again = $("#nodeShellAgain");
    if (again) again.hidden = false;
  };
  term.onData(data => send({ type: "input", data }));
  if (window.__nodeTermResize) window.__nodeTermResize.disconnect();
  window.__nodeTermResize = new ResizeObserver(resize);
  window.__nodeTermResize.observe($("#nodeTerm"));
};
