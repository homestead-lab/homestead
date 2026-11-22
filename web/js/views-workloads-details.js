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

function workloadOverview(key) {
  const w = workloadByKey(key);
  if (!w) return "";
  const update = workloadUpdateOf(w), off = w.desired === 0;
  const answer = answerOf(w);
  const volumes = (w.claims || []).length && !remoteRow(w) ? `<span class="row">${w.claims.map(claim =>
    `<button type="button" class="tag info linkish" title="Open ${esc(claim)} in Volumes" onclick="closeDetails();openWorkloadVolume(${jsq(claim)})">${esc(claim)}</button>`).join("")}</span>` : "";
  const hardware = hardwareTags(w.hardware || (w.gpu ? ["igpu"] : []));
  const released = workloadReleasedRow(w, true);
  const more = [released ? `<div class="wl-detail-released"><span>Old volumes</span>${released}</div>` : "", gitopsDetail(w), startsAfterFact(w)].filter(Boolean).join("");
  return workloadDetailNotice(w, update)
    + UI.facts([
      ["State", `${workloadStateChip(w)} <span class="dim xs">${w.ready} of ${w.desired} ready</span>`],
      ["Address", `<span class="waccess">${accessPorts(w.ports)}</span>`, "link"],
      ["Host", esc(workloadHost(w)), "node"],
      !off && ["CPU", `<span class="mono">${workloadCpuPercent(w.cpu)}</span>`, "chip"],
      !off && ["Memory", `<span class="mono">${workloadMemory(w.mem_mb)}</span>`, "memory"],
      ["Image", `<span class="mono small">${(w.images || []).map(esc).join("<br>") || "—"}</span>`, "box"],
      hardware && ["Hardware", hardware, "chip"],
      volumes && ["Volumes", volumes, "disk"],
      !off && ["Running for", esc(w.uptime ? fmtUp(w.uptime) : "Starting"), "clock"],
      ["Updates", autoUpdateTag(w) || updateOwnerPill(update) || "Manual", "update"],
      w.prefers && ["Placement", preferTag(w), "node"],
      ["Namespace", esc(w.ns), "list"],
    ])
    + (answer ? answerCardRow(w) : "")
    + (more ? UI.withFactIcons(`<div class="about-grid wl-detail-more">${more}</div>`) : "")
    + workloadHierarchy(w, { wide: true });
}

if (typeof DETAIL_KINDS === "object") DETAIL_KINDS.app = {
  head: key => {
    const w = workloadByKey(key);
    return w && { title: w.name, sub: [w.group, w.ns, workloadHost(w)].filter(Boolean).join(" · "), iconHtml: appAvatar(w.name, w.icon),
      cluster: w.site && typeof fleetAll === "function" && fleetAll() ? w.site.id : "" };
  },
  actions: key => {
    const w = workloadByKey(key);
    return w ? workloadDetailActions(w, workloadUpdateOf(w), w.desired === 0) : [];
  },
  tabs: [{ id: "overview", label: "Overview", html: workloadOverview }],
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
  workloadByKey, workloadUpdateOf, workloadHost, workloadStateChip, workloadDetailNotice, workloadOverview,
};
