/* Homestead views: the Volumes page's summary line and attention list, a volume's details sidebar and its hover cards (design.md "Details sidebar", "Hover cards"). Part of the Volumes page, split from views-storage.js (#911); a classic script. */
// A volume's key: its name, and its cluster's in the view of every cluster,
// where two clusters can each have a volume of the same name.
const volumeKey = x => x.site?.id ? `${x.site.id}/${x.name}` : x.name;
const volumeByName = key => (STATE.data.vols || []).find(x => volumeKey(x) === key) || null;
const volumeTitle = x => x.pvc_name || x.name;
const accessWords = modes => (modes || []).map(m => m === "ReadWriteMany" ? "RWX" : m === "ReadWriteOnce" ? "RWO" : m).join(", ") || "?";
const gibPerDay = bytes => (bytes / 1024 ** 3).toFixed(1);
// Longhorn's own reason, as a sentence.
const reasonSentence = text => { const s = String(text || "").trim(); return s ? `${s[0].toUpperCase()}${s.slice(1)}${/[.!?]$/.test(s) ? "" : "."}` : ""; };

/* What is wrong with a volume, if anything: {tone, chip, word, line}. The
   line is a sentence for the row and the sidebar; the word finishes "<name> …"
   in the summary. A volume no app uses is for the attention list, not a fault. */
function volumeProblem(x) {
  if (x.restore?.error) return { tone: "bad", chip: "Restore failed", word: "could not be restored", line: reasonSentence(x.restore.error) };
  if (x.rebuild?.error) return { tone: "bad", chip: "Rebuild failed", word: "could not rebuild a copy", line: reasonSentence(x.rebuild.error) };
  const short = x.copies_short;
  if (short && !short.whole) return { tone: "bad", chip: "No whole copy", word: "has no whole copy", line: "No complete copy is left, so Longhorn cannot rebuild it." };
  if (x.state === "attached" && x.robustness === "faulted") return { tone: "bad", chip: "Faulted", word: "is faulted", line: reasonSentence(x.health_reason) || "Longhorn cannot read or write it." };
  if (x.state === "attached" && x.robustness === "degraded" && !x.rebuild) return { tone: "warn", chip: "Degraded", word: "is missing a copy",
    line: reasonSentence(x.health_reason) || "A copy is missing, and Longhorn rebuilds it when there is room." };
  if (short) return { tone: "warn", chip: `${short.whole} of ${short.wanted} copies`, word: `has ${short.whole} of ${short.wanted} copies`,
    line: "Longhorn rebuilds missing copies only while a volume is attached. Rebuild now starts it." };
  const soon = forecastFor(`volume:${x.name}`), warn = STATE.data.forecast?.warn_days || 14;
  if (soon && soon.days_left <= warn) return { tone: soon.days_left <= 3 ? "bad" : "warn", chip: `Full ${forecastWhen(soon.days_left)}`,
    word: `is full ${forecastWhen(soon.days_left)}`, line: `It grows by about ${gibPerDay(soon.per_day)} GiB a day, so it is full by ${forecastDate(soon.full_on)}.` };
  return null;
}

/* The page's one line (design.md "Summary line"): whether the data is safe,
   each volume that is not as a link to its details, and the free space. */
function volumeSummary(v, st, { q = "", spare = 0 } = {}) {
  const troubled = v.map(x => [x, volumeProblem(x)]).filter(([, p]) => p);
  const scope = q ? ` matching “${esc(q)}”` : "", n = v.length;
  const head = !n ? `No Longhorn volumes${scope}.`
    : !troubled.length ? `${n === 1 ? "The volume" : `All ${n} volumes`}${scope} ${n === 1 ? "is" : "are"} healthy.`
    : `${n - troubled.length} of ${n} volumes${scope} ${n - troubled.length === 1 ? "is" : "are"} healthy.`;
  const shown = troubled.slice(0, 3).map(([x, p]) => `<button type="button" class="linkish sum-link sum-${p.tone}" onclick="openDetails('volume',${jsq(volumeKey(x))})">${esc(volumeTitle(x))} ${esc(p.word)}</button>`);
  const pool = (STATE.data.forecast?.rows || []).find(r => r.kind === "pool" && r.days_left !== null && r.days_left <= (STATE.data.forecast.show_days || 60));
  return [`<b>${head}</b>`,
    shown.length ? `${shown.join(", ")}${troubled.length > 3 ? ` and ${troubled.length - 3} more` : ""}.` : "",
    st ? `<span data-card="storage|pool">${esc(st.avail_gb)} GiB of ${esc(st.cap_gb)} GiB is free${pool ? `, and all storage is full ${esc(forecastWhen(pool.days_left))}` : ""}.</span>` : "",
    spare ? `<button type="button" class="linkish sum-link" onclick="STATE.volSpare=true;viewStorage()">${spare === 1 ? "1 volume is" : `${spare} volumes are`} not used by any app.</button>` : "",
  ].filter(Boolean).join(" ");
}

/* What needs a person on this page, one line each with the button that deals
   with it: a volume or disk filling up, copies to rebuild, volumes nothing
   uses and old copies. The page's one notice; nothing to do shows nothing. */
function volumeAttention(v, forecast, oldCopies = []) {
  const items = [];
  const show = forecast?.show_days || 60;
  for (const r of (forecast?.rows || []).filter(r => r.days_left !== null && r.days_left <= show)) {
    const vol = r.kind === "volume" ? v.find(x => x.name === r.volume) : null;
    const name = r.kind === "pool" ? "All Longhorn storage" : r.kind === "disk" ? `The disk ${r.label}` : r.label;
    const button = vol ? UI.button("Expand volume", `volumeEdit(${JSON.stringify(vol)})`, { attrs: 'data-need="operator"' })
      : r.kind === "disk" ? UI.button("Open host", `nodeDetail(${jsArg(r.node)})`) : "";
    items.push([forecastTone(r.days_left, forecast.warn_days), `${esc(name)} is full ${esc(forecastWhen(r.days_left))}`,
      `${esc(r.pct)}% full, growing by ${gibPerDay(r.per_day)} GiB a day.`, button]);
  }
  for (const x of v.filter(x => x.copies_short?.whole)) items.push(["warn", `${esc(volumeTitle(x))} has ${x.copies_short.whole} of ${x.copies_short.wanted} copies`,
    "Longhorn rebuilds missing copies only while a volume is attached.", UI.button("Rebuild now", `volumeRebuild(${jsArg(x.name)})`, { attrs: 'data-need="operator"' })]);
  const spare = v.filter(x => ["orphaned", "unclaimed"].includes(volumeUse(x).kind));
  if (spare.length) items.push(["info", `${spare.length === 1 ? "1 volume is" : `${spare.length} volumes are`} not used by any app`,
    "Review them before deleting, to free their space.", UI.button(STATE.volSpare ? "Show all volumes" : "Show them", "STATE.volSpare=!STATE.volSpare;viewStorage()")]);
  if (oldCopies.length) items.push(["info", `${oldCopies.length === 1 ? "1 old copy is" : `${oldCopies.length} old copies are`} kept from a storage class change`,
    "Remove each one once its app works with the new copy.", UI.button("Review old copies", "document.getElementById('volOldCopies')?.scrollIntoView({behavior:'smooth'})")]);
  if (!items.length) return "";
  return `<div class="card flat attention-list" role="region" aria-label="Needs attention"><div class="ctitle">Needs attention</div>${items.map(([tone, head, line, button]) =>
    `<div class="attention-item ${tone}"><i class="attention-dot" aria-hidden="true"></i><div class="attention-text"><b>${head}.</b> <span class="dim">${line}</span></div>${button || ""}</div>`).join("")}</div>`;
}

/* Copies as dots: one for each copy the volume should have, filled when it is
   whole, amber when it is not running, an empty ring when it is missing. */
function volumeCopyDots(x) {
  if (x.copies === null) return `<span class="dim xs" data-tip="${esc(volumeCopiesTip(x))}">Unknown</span>`;
  const copies = x.copies || [], want = Math.max(+x.replicas || 0, copies.length);
  const dots = Array.from({ length: want }, (_, i) => {
    const c = copies[i];
    return `<i class="copydot ${!c && copies.length ? "missing" : !c ? "unknown" : c.healthy ? "ok" : "warn"}"></i>`;
  }).join("");
  const label = copies.length ? `${copies.filter(c => c.healthy).length} of ${want} copies whole` : `${want} cop${want === 1 ? "y" : "ies"}`;
  return `<span class="copydots" role="img" tabindex="0" aria-label="${esc(label)}" data-card="volume-copies|${esc(volumeKey(x))}">${dots}</span>`;
}

function volumeOverview(name) {
  const x = volumeByName(name);
  if (!x) return "";
  const problem = volumeProblem(x), use = volumeUse(x), fs = x.filesystem;
  const notice = [problem && UI.callout(problem.tone, `${volumeTitle(x)} ${problem.word}`, esc(problem.line)),
    volumeBusy(x) && !problem ? volumeHealthCell(x) : "",
    ["orphaned", "unclaimed"].includes(use.kind) && UI.callout("info", "No app uses this volume", "Delete it if the data is not needed, or attach it to an app from the app's editor.")].filter(Boolean).join("");
  const copies = (x.copies || []).map(c => `${esc(c.node)} · ${esc(c.disk)}${c.healthy ? "" : ` <span class="warn">(${esc(c.state || "not running")})</span>`}`).join("<br>");
  return notice + UI.facts([
    // The notice above says why; the fact is the state alone.
    ["State", problem ? UI.chip(problem.chip, problem.tone, { size: "slim" }) : volumeHealthCell(x), "pulse"],
    ["Used by", volumeUseCell(x), "box"],
    ["Files", fs && Number.isFinite(fs.used_pct) ? `<span class="volusage-line">${meter(fs.used_pct)}<span class="mono">${esc(fs.used_gb)} / ${esc(fs.capacity_gb)} GiB</span></span>` : '<span class="dim">Not measured</span>', "disk"],
    ["Size", `<span class="mono">${esc(x.size_gb)} GiB</span>`, "disk"],
    ["Longhorn footprint", `<span class="mono">${esc(x.actual_gb ?? "—")} GiB</span>`, "layers"],
    ["Copies", `${volumeCopyDots(x)}${copies ? `<div class="dim xs">${copies}</div>` : ""}`, "copy"],
    ["Access", esc(accessWords(x.access_modes)), "link"],
    ["Engine", x.engine === "v2" ? "Longhorn V2 (SPDK)" : "Longhorn V1", "chip"],
    x.storage_class && ["Storage class", esc(x.storage_class), "sliders"],
    x.state === "attached" ? ["Host", esc(x.node || "—"), "node"] : ["Last used", esc(fmtAgo(x.last_used_secs)), "clock"],
    ["Namespace", esc(x.namespace || "—"), "list"],
    ["Longhorn volume", `<span class="mono small">${esc(x.name)}</span>`, "info"],
  ]);
}

if (typeof DETAIL_KINDS === "object") DETAIL_KINDS.volume = {
  head: name => {
    const x = volumeByName(name);
    if (!x) return null;
    const by = attachedWorkloads(x), use = volumeUse(x);
    const users = by.length ? `used by ${by.join(", ")}` : use.kind === "stopped" ? `used by ${use.by.map(w => w.split("/").pop()).join(", ")}, stopped` : x.state;
    return { title: volumeTitle(x), sub: [x.namespace, users].filter(Boolean).join(" · "),
      iconHtml: `<span class="details-icon">${icon("disk")}</span>`, cluster: x.site && typeof fleetAll === "function" && fleetAll() ? x.site.id : "" };
  },
  actions: name => { const x = volumeByName(name); return x ? volumeActionItems(x) : []; },
  tabs: [{ id: "overview", label: "Overview", html: volumeOverview }],
};

if (typeof HOVER_CARDS === "object") {
  /* The name's card: what the volume is and who uses it. */
  HOVER_CARDS.volume = name => {
    const x = volumeByName(name);
    if (!x) return "";
    const by = attachedWorkloads(x);
    return UI.card({ title: volumeTitle(x), chipHtml: UI.chip(accessWords(x.access_modes), "neutral", { size: "slim" }), rows: [
      ["Used by", esc(by.join(", ") || (volumeUse(x).kind === "stopped" ? "A stopped app" : "Nothing")), "box"],
      ["Size", `${esc(x.size_gb)} GiB`, "disk"],
      ["Engine", x.engine === "v2" ? "V2" : "V1", "chip"],
      x.storage_class && ["Storage class", esc(x.storage_class), "sliders"],
    ], link: { label: "Open details", onclick: `openDetails('volume',${JSON.stringify(name)})` } });
  };
  /* The state's card: the reason, and the copies behind it. */
  HOVER_CARDS["volume-health"] = name => {
    const x = volumeByName(name);
    if (!x) return "";
    const problem = volumeProblem(x), busy = volumeBusy(x);
    return UI.card({ title: problem ? `${volumeTitle(x)} ${problem.word}` : x.state === "attached" ? `${volumeTitle(x)} is healthy` : `${volumeTitle(x)} is detached`,
      chipHtml: problem ? UI.chip(problem.chip, problem.tone, { size: "slim" }) : "", rows: [
        problem && ["Why", esc(problem.line), "alert"],
        busy && !busy.error && [x.restore ? "Restore" : "Rebuild", `${esc(busy.pct)}%`, "refresh"],
        ["Copies", esc(volumeCopiesTip(x)), "copy"],
        x.state !== "attached" && ["Last used", esc(fmtAgo(x.last_used_secs)), "clock"],
      ], link: { label: "Open details", onclick: `openDetails('volume',${JSON.stringify(name)})` } });
  };
  /* The copies' card: each copy's host and disk. */
  HOVER_CARDS["volume-copies"] = name => {
    const x = volumeByName(name);
    if (!x) return "";
    const copies = x.copies || [];
    return UI.card({ title: `${copies.length || x.replicas} cop${(copies.length || +x.replicas) === 1 ? "y" : "ies"} of ${volumeTitle(x)}`,
      rows: copies.length ? copies.map(c => [c.node, `${esc(c.disk)}${c.healthy ? "" : ` · ${esc(c.state || "not running")}`}`, "node"])
        : [["Where", esc(volumeCopiesTip(x)), "info"]] });
  };
  /* The usage's card: files, the space Longhorn uses, and the forecast. */
  HOVER_CARDS["volume-usage"] = name => {
    const x = volumeByName(name);
    if (!x) return "";
    const fs = x.filesystem, soon = forecastFor(`volume:${x.name}`);
    return UI.card({ title: `Space on ${volumeTitle(x)}`, rows: [
      ["Files", fs && Number.isFinite(fs.used_gb) ? `${esc(fs.used_gb)} of ${esc(fs.capacity_gb)} GiB` : "Not measured", "disk"],
      ["Size", `${esc(x.size_gb)} GiB provisioned`, "disk"],
      ["Longhorn footprint", `${esc(x.actual_gb ?? "—")} GiB, snapshots included`, "layers"],
      soon && ["Forecast", `Full by ${esc(forecastDate(soon.full_on))}, +${gibPerDay(soon.per_day)} GiB a day`, "clock"],
    ], link: { label: "Expand volume", onclick: `volumeEdit(${JSON.stringify(x)})` } });
  };
  /* The page's space: free and used, each host, and the pool's forecast. */
  HOVER_CARDS.storage = () => {
    const st = STATE.data.storageTotals;
    if (!st) return "";
    const pool = (STATE.data.forecast?.rows || []).find(r => r.kind === "pool");
    return UI.card({ title: "Longhorn storage", chipHtml: UI.chip(`${Math.round(st.used_pct)}% used`, st.used_pct >= 85 ? "warn" : "neutral", { size: "slim" }),
      chartHtml: meter(st.used_pct), rows: [
        ["Free", `${esc(st.avail_gb)} of ${esc(st.cap_gb)} GiB`, "disk"],
        ["Provisioned", `${esc(st.provisioned_gb)} GiB`, "layers"],
        ...(st.disks || []).map(d => [d.node, `${esc(d.avail_gb)} GiB free of ${esc(d.cap_gb)}`, "node"]),
        pool && ["Forecast", pool.days_left !== null ? `Full ${esc(forecastWhen(pool.days_left))}` : esc(reasonSentence(pool.why) || "Not growing"), "clock"],
      ] });
  };
}

// Node only (tests): the same names, for require(). The browser has no `module`.
if (typeof module === "object" && module.exports) module.exports = {
  volumeKey, volumeByName, volumeTitle, accessWords, gibPerDay, reasonSentence, volumeProblem, volumeSummary, volumeAttention, volumeCopyDots, volumeOverview,
};
