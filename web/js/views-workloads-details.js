/* Homestead Containers: an app's details sidebar and its hover cards (design.md "Details sidebar", "Hover cards"). One of the scripts views-workloads.js was split into (#911); they share the page's globals and load in the order index.html lists them. */
// The app a row, a card or the summary names, by the key workloadRowKey gives it.
const workloadByKey = key => (STATE.data.wl || []).find(w => workloadRowKey(w) === key) || null;
const workloadUpdateOf = w => w.platform || w.managed_smb || w.managed_nfs || remoteRow(w) ? null : workloadUpdate(w.ns, w.name);
const workloadHost = w => w.desired === 0 ? "Stopped" : (w.nodes || []).join(", ") || "Unscheduled";

/* How an app is, in a word and a chip: its problem when it has one. */
function workloadStateChip(w) {
  const problem = workloadProblem(w);
  if (problem) return UI.chip(problem.chip, problem.tone, { size: "slim" });
  return UI.chip(w.desired === 0 ? "Stopped" : "Running", w.desired === 0 ? "neutral" : "ok", { size: "slim" });
}

/* What needs attention first, above the facts: what is wrong, why it cannot
   start, its download, or an update waiting. */
function workloadDetailNotice(w, update) {
  const problem = workloadProblem(w), pull = workloadPull(w);
  const ns = jsq(w.ns), name = jsq(w.name);
  const parts = [];
  if (problem) parts.push(UI.callout(problem.tone, `${w.name} ${problem.word}`, esc(problem.line)));
  if (pull) parts.push(pullBar(pull));
  if (update?.available && !updateOwner(update)) parts.push(UI.callout("info", "An image update is ready",
    `${esc(imageShort(w.images))} has a newer build. <button type="button" class="linkish" data-need="operator" onclick="imageUpdateReview(${ns},${name})">Review the update ›</button>`));
  return parts.join("");
}

/* The sidebar's reads (detailsData): the app's log, its change history and
   the cluster's recent events. `keep`, so leaving the page does not cancel them. */
const workloadPod = w => w.pods?.[0]?.name || "";
const workloadLogRead = w => () => api(`/api/logs?ns=${encodeURIComponent(w.ns)}&pod=${encodeURIComponent(workloadPod(w))}&tail=200`, { keep: true });
const workloadChangesRead = w => () => api(`/api/changes?ns=${encodeURIComponent(w.ns)}&name=${encodeURIComponent(w.name)}`, { keep: true });
const workloadLogSlot = (w, lines) => detailsSlot(detailsData("logs", workloadLogRead(w)),
  text => String(text || "").trim() ? UI.logTail(text, lines) : '<p class="dim small">It has not written anything to its log yet.</p>',
  { reading: "Reading its log…", failed: "Its log could not be read" });

/* Its address in words: 192.0.2.209 port 8000, the main one first. A port
   with no address of its own keeps the usual port chips. */
function workloadAddressWords(w) {
  const ports = (w.ports || []).filter(p => p.ip);
  if (!ports.length) return `<span class="waccess">${accessPorts(w.ports)}</span>`;
  const sorted = [...ports].sort((a, b) => (b.primary ? 1 : 0) - (a.primary ? 1 : 0));
  return sorted.slice(0, 2).map(p => `<button type="button" class="linkish details-address" title="Open ${esc(svcUrl(p.ip, p.port))}" onclick="openSvc(${jsq(p.ip)},${p.port})">`
    + `<span class="mono">${esc(p.ip)}</span> <span class="dim">port</span> <span class="mono">${esc(p.port)}</span></button>`).join("<br>")
    + (sorted.length > 2 ? `<div class="dim xs">and ${sorted.length - 2} more in Settings</div>` : "");
}

/* Overview (the mockup): what is wrong first, the last lines of its log when
   it is in trouble, then its address, host, memory and uptime. */
function workloadOverview(key) {
  const w = workloadByKey(key);
  if (!w) return "";
  const update = workloadUpdateOf(w), off = w.desired === 0, problem = workloadProblem(w), a = answerOf(w);
  const tail = problem && !off && workloadPod(w) && !remoteRow(w) ? UI.section("Last lines of its log", workloadLogSlot(w, 3)) : "";
  return workloadDetailNotice(w, update) + tail
    + UI.facts([
      ["Address", workloadAddressWords(w)],
      ["Host", esc(workloadHost(w))],
      !off && ["Memory", esc(workloadMemory(w.mem_mb))],
      a && a.state !== "off" && a.uptime_30d !== null && a.uptime_30d !== undefined ? ["Uptime", esc(`${answerShare(a.uptime_30d)} over 30 days`)]
        : !off && ["Running for", esc(w.uptime ? fmtUp(w.uptime) : "Starting")],
    ], { icons: false });
}

/* Logs: its last 200 lines, and the full log viewer to follow it live. */
function workloadLogsTab(key) {
  const w = workloadByKey(key);
  if (!w) return "";
  if (!workloadPod(w)) return `<p class="dim small">${w.desired === 0 ? "It is stopped, so it is not writing a log." : "No copy of it is running yet, so there is no log to show."}</p>`;
  return workloadLogSlot(w, 0)
    + `<div class="details-foot">${actionBar([{ label: "Open full logs", run: `wlLogs(${jsq(w.ns)},${jsq(workloadPod(w))},${jsq(w.name)})` },
      { label: "Search logs", run: `logSearch(${jsq(w.ns)},${jsq(w.name)})` }], { shown: 2 })}</div>`;
}

/* Activity: its web check over the last day, the changes made to it and
   what Kubernetes said about it lately. */
function workloadActivityTab(key) {
  const w = workloadByKey(key);
  if (!w) return "";
  const local = !remoteRow(w);
  const changes = local ? detailsData("changes", workloadChangesRead(w)) : { data: { entries: [] } };
  const events = detailsData("events", clusterEventsRead);
  const mine = e => (!e.ns || e.ns === w.ns) && (e.obj === w.name || String(e.obj || "").startsWith(`${w.name}-`));
  const rows = [
    ...(changes.data?.entries || []).map(e => ({ at: e.at, title: `Changed ${e.changes.map(c => c.field).join(", ")}`,
      detail: e.source === "outside" ? "Outside Homestead" : e.by ? `By ${UI.who(e.by)}` : "By Homestead" })),
    ...(events.data?.events || []).filter(mine).map(e => ({ at: e.time, title: eventWords(e.reason), detail: reasonSentence(e.msg), tone: e.type === "Warning" ? "warn" : "" })),
  ];
  const reading = changes.loading && events.loading;
  return (answerOf(w) ? answerCardRow(w) : "")
    + (changes.error ? UI.callout("warn", "Its change history could not be read", esc(changes.error)) : "")
    + (events.error ? UI.callout("warn", "The cluster's events could not be read", esc(events.error)) : "")
    + (reading ? detailsSlot({ loading: true }, () => "", { reading: "Reading what happened…" }) : UI.activity(rows, { empty: "Nothing has been recorded for this app lately." }))
    + (local ? `<div class="details-foot">${actionBar([{ label: "Open change history", run: `wlHistory(${jsq(w.ns)},${jsq(w.name)})`, tip: "Every change, with Undo." }], { shown: 1 })}</div>` : "");
}

/* Settings: what it runs and how, and the changes that can be made to it. */
function workloadSettingsTab(key) {
  const w = workloadByKey(key);
  if (!w) return "";
  const update = workloadUpdateOf(w), off = w.desired === 0;
  const volumes = (w.claims || []).length && !remoteRow(w) ? `<span class="row">${w.claims.map(claim =>
    `<button type="button" class="tag info linkish" title="Open ${esc(claim)} in Volumes" onclick="closeDetails();openWorkloadVolume(${jsq(claim)})">${esc(claim)}</button>`).join("")}</span>` : "";
  const hardware = hardwareTags(w.hardware || (w.gpu ? ["igpu"] : []));
  const released = workloadReleasedRow(w, true);
  const more = [released ? `<div class="wl-detail-released"><span>Old volumes</span>${released}</div>` : "", gitopsDetail(w), startsAfterFact(w)].filter(Boolean).join("");
  const change = (workloadMenuItems(w, update, off) || []).filter(i => ["Edit settings", "Move app…"].includes(i.label));
  return UI.facts([
    ["Image", `<span class="mono small">${(w.images || []).map(esc).join("<br>") || "—"}</span>`, "box"],
    ["Copies ready", esc(`${w.ready} of ${w.desired}`), "layers"],
    ["Address", `<span class="waccess">${accessPorts(w.ports)}</span>`, "link"],
    !off && ["CPU", `<span class="mono">${workloadCpuPercent(w.cpu)}</span>`, "chip"],
    ["Updates", autoUpdateTag(w) || updateOwnerPill(update) || "Manual", "update"],
    w.prefers && ["Placement", preferTag(w), "node"],
    hardware && ["Hardware", hardware, "chip"],
    volumes && ["Volumes", volumes, "disk"],
    ["Namespace", esc(w.ns), "list"],
  ])
    + (more ? UI.withFactIcons(`<div class="about-grid wl-detail-more">${more}</div>`) : "")
    + workloadHierarchy(w, { wide: true })
    + (change.length ? `<div class="details-foot">${actionBar(change.map(i => ({ ...i, plain: true })), { shown: 2 })}</div>` : "");
}

if (typeof DETAIL_KINDS === "object") DETAIL_KINDS.app = {
  head: key => {
    const w = workloadByKey(key);
    return w && { title: w.name, sub: [w.group || "Ungrouped", w.ns, workloadHost(w)].join(" · "), iconHtml: appAvatar(w.name, w.icon),
      cluster: w.site && typeof fleetAll === "function" && fleetAll() ? w.site.id : "" };
  },
  actions: key => {
    const w = workloadByKey(key);
    return w ? workloadDetailActions(w, workloadUpdateOf(w), w.desired === 0) : [];
  },
  tabs: [
    { id: "overview", label: "Overview", html: workloadOverview },
    { id: "logs", label: "Logs", html: workloadLogsTab, when: key => !remoteRow(workloadByKey(key)) },
    { id: "activity", label: "Activity", html: workloadActivityTab },
    { id: "settings", label: "Settings", html: workloadSettingsTab },
  ],
};

/* The status's card: how it is, the last day of checks, and its next step. */
if (typeof HOVER_CARDS === "object") HOVER_CARDS["app-status"] = key => {
  const w = workloadByKey(key);
  if (!w) return "";
  const a = answerOf(w), problem = workloadProblem(w), off = w.desired === 0;
  return UI.card({
    title: problem ? `${w.name} ${problem.word}` : off ? `${w.name} is stopped` : `${w.name} is running`,
    chipHtml: workloadStateChip(w),
    chartHtml: a?.strip?.length && a.state !== "off" ? answerStrip(a.strip, "ui-card-answer") : "",
    rows: [
      problem && ["Why", esc(problem.line), "alert"],
      // The row says it in words; the card gives the check's own result.
      problem?.detail && ["Last check", esc(problem.detail), "pulse"],
      ["Copies ready", `${w.ready} of ${w.desired}`, "layers"],
      a && !["off", "down", "slow"].includes(a.state) && ["Monitoring", esc(`${ANSWER_WORDS[a.state] || a.state} · ${answerDetail(a)}`), "pulse"],
      a && a.state !== "off" && ["Last 30 days", esc(answerShare(a.uptime_30d)), "check"],
      !off && w.uptime && ["Running for", esc(fmtUp(w.uptime)), "clock"],
    ],
    link: problem && !off ? { label: "Open logs", onclick: `wlLogs(${JSON.stringify(w.ns)},${JSON.stringify(w.pods?.[0]?.name || "")},${JSON.stringify(w.name)})` }
      : { label: "Open details", onclick: `openDetails('app',${JSON.stringify(key)})` },
  });
};

/* The name's card: what it runs, where, and how it is kept up to date. */
if (typeof HOVER_CARDS === "object") HOVER_CARDS.app = key => {
  const w = workloadByKey(key);
  if (!w) return "";
  const update = workloadUpdateOf(w), hardware = hardwareTags(w.hardware || (w.gpu ? ["igpu"] : []));
  return UI.card({
    title: w.name,
    chipHtml: workloadStateChip(w),
    rows: [
      ["Image", `<span class="mono xs">${imageShort(w.images) || "—"}</span>`, "box"],
      ["Host", esc(workloadHost(w)), "node"],
      w.desired > 0 && w.uptime && ["Running for", esc(fmtUp(w.uptime)), "clock"],
      ["Updates", autoUpdateTag(w) || (update?.available ? UI.chip("Update ready", "warn", { size: "slim" }) : "Manual"), "update"],
      w.prefers && ["Placement", esc(`Prefers ${w.prefers}`), "node"],
      hardware && ["Hardware", hardware, "chip"],
    ],
    link: { label: "Open details", onclick: `openDetails('app',${JSON.stringify(key)})` },
  });
};

// Node only (tests): the same names, for require(). The browser has no `module`.
if (typeof module === "object" && module.exports) module.exports = {
  workloadByKey, workloadUpdateOf, workloadHost, workloadStateChip, workloadDetailNotice, workloadPod, workloadLogRead, workloadChangesRead, workloadLogSlot,
  workloadAddressWords, workloadOverview, workloadLogsTab, workloadActivityTab, workloadSettingsTab,
};
