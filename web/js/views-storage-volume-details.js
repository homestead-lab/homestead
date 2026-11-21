/* Homestead views: the Volumes page's summary line and copies, a volume's details sidebar and its hover cards (design.md "Details sidebar", "Hover cards"). Part of the Volumes page, split from views-storage.js (#911); a classic script. */
// A volume's key: its name, and its cluster's in the view of every cluster,
// where two clusters can each have a volume of the same name.
const volumeKey = x => x.site?.id ? `${x.site.id}/${x.name}` : x.name;
const volumeByName = key => (STATE.data.vols || []).find(x => volumeKey(x) === key) || null;
const volumeTitle = x => x.pvc_name || x.name;
const accessWords = modes => (modes || []).map(m => m === "ReadWriteMany" ? "RWX" : m === "ReadWriteOnce" ? "RWO" : m).join(", ") || "?";
const gibPerDay = bytes => (bytes / 1024 ** 3).toFixed(1);
// Longhorn's own reason, as a sentence.
const reasonSentence = text => { const s = String(text || "").trim(); return s ? `${s[0].toUpperCase()}${s.slice(1)}${/[.!?]$/.test(s) ? "" : "."}` : ""; };

/* What is wrong with a volume, if anything: {tone, chip, word, line, note}.
   The word finishes "<name> …" in the summary; the note is the short line
   under the row's copies; the line is the full sentence for the sidebar and
   the hover card. Filling up is shown under the usage instead (usage: true).
   A volume no app uses is not a fault: its Attached to cell says so. */
function volumeProblem(x) {
  if (x.restore?.error) return { tone: "bad", chip: "Restore failed", word: "could not be restored", note: "The restore failed.", line: reasonSentence(x.restore.error) };
  if (x.rebuild?.error) return { tone: "bad", chip: "Rebuild failed", word: "could not rebuild a copy", note: "A rebuild failed.", line: reasonSentence(x.rebuild.error) };
  const short = x.copies_short, missing = n => `${n} cop${n === 1 ? "y is" : "ies are"} missing.`;
  if (short && !short.whole) return { tone: "bad", chip: "No whole copy", word: "has no whole copy", note: "No whole copy is left.", line: "No complete copy is left, so Longhorn cannot rebuild it." };
  if (x.state === "attached" && x.robustness === "faulted") return { tone: "bad", chip: "Faulted", word: "is faulted", note: "Longhorn cannot read or write it.", line: reasonSentence(x.health_reason) || "Longhorn cannot read or write it." };
  if (x.state === "attached" && x.robustness === "degraded" && !x.rebuild) {
    const want = Math.max(+x.replicas || 0, (x.copies || []).length), gone = Math.max(1, want - (x.copies || []).filter(c => c.healthy).length);
    return { tone: "warn", chip: "Degraded", word: gone === 1 ? "is missing a copy" : `is missing ${gone} copies`, note: missing(gone),
      line: reasonSentence(x.health_reason) || "A copy is missing, and Longhorn rebuilds it when there is room." };
  }
  if (short) {
    const gone = Math.max(1, short.wanted - short.whole);
    return { tone: "warn", chip: `${short.whole} of ${short.wanted} copies`, word: gone === 1 ? "is missing a copy" : `is missing ${gone} copies`, note: missing(gone),
      line: "Longhorn rebuilds missing copies only while a volume is attached. Rebuild copy starts it now." };
  }
  const soon = forecastFor(`volume:${x.name}`), warn = STATE.data.forecast?.warn_days || 14;
  if (soon && soon.days_left <= warn) return { tone: soon.days_left <= 3 ? "bad" : "warn", chip: `Full ${forecastWhen(soon.days_left)}`, usage: true,
    word: `is full ${forecastWhen(soon.days_left)}`, line: `It grows by about ${gibPerDay(soon.per_day)} GiB a day, so it is full by ${forecastDate(soon.full_on)}.` };
  return null;
}

// Sizes as people read them: 1,392 rather than 1392.
const GIB_FORMAT = new Intl.NumberFormat("en-US", { maximumFractionDigits: 1 });
const gibText = n => Number.isFinite(+n) ? GIB_FORMAT.format(+n) : "?";

/* The page's one line (design.md "Summary line"): the free space, then each
   volume or disk that needs a person as a link, set apart by a dot. A phone
   shows the short form: the free space and how many need attention. */
function volumeSummary(v, st, { q = "", reading = false } = {}) {
  // Copies before space, and the worst of each first.
  const rank = p => (p.usage ? 2 : 0) + (p.tone === "bad" ? 0 : 1);
  const troubled = v.map(x => [x, volumeProblem(x)]).filter(([, p]) => p).sort((a, b) => rank(a[1]) - rank(b[1]));
  const f = STATE.data.forecast, warn = f?.warn_days || 14;
  const stores = (f?.rows || []).filter(r => r.kind !== "volume" && r.days_left !== null && r.days_left <= warn);
  const free = st ? `<span data-card="storage|pool"><b>${gibText(st.avail_gb)} GiB of ${gibText(st.cap_gb)} GiB is free.</b></span>`
    : reading ? '<span class="dim">Reading the free space…</span>' : `<b>${v.length === 1 ? "1 volume" : `${v.length} volumes`}.</b>`;
  const links = [...troubled.slice(0, 3).map(([x, p]) => `<button type="button" class="linkish sum-link sum-${p.tone}" onclick="openDetails('volume',${jsq(volumeKey(x))})">${esc(volumeTitle(x))} ${esc(p.word)}</button>`),
    troubled.length > 3 ? `<span class="sum-note">${troubled.length - 3} more</span>` : "",
    ...stores.map(r => {
      const tone = forecastTone(r.days_left, warn), words = `${r.kind === "pool" ? "All Longhorn storage" : `The disk ${esc(r.label)}`} is full ${esc(forecastWhen(r.days_left))}`;
      return r.kind === "disk" ? `<button type="button" class="linkish sum-link sum-${tone}" onclick="nodeDetail(${jsq(r.node)})">${words}</button>` : `<span class="sum-${tone}">${words}</span>`;
    }),
    q ? `<span class="sum-note">${v.length === 1 ? "1 volume matches" : `${v.length} volumes match`} “${esc(q)}”</span>` : ""].filter(Boolean);
  const needs = troubled.length + stores.length;
  const wide = `${free}${links.length ? ` ${links.join(SUM_DOT)}` : ""}`;
  const narrow = `${st ? `<b>${gibText(st.avail_gb)} GiB free.</b>` : free}${needs ? ` <button type="button" class="linkish sum-link sum-warn" onclick="volumeNeedsOnly(true)">${needs === 1 ? "1 volume needs" : `${needs} volumes need`} attention</button>` : ""}`;
  return `<span class="sum-wide">${wide}</span><span class="sum-narrow">${narrow}</span>`;
}
if (typeof window === "object") window.volumeNeedsOnly = on => { STATE.volNeeds = on; viewStorage(); };

/* Copies as dots: one for each copy the volume should have, filled when it is
   whole, amber when it is not running, an empty ring when it is missing. */
function volumeCopyDots(x) {
  if (x.copies === null) return `<span class="dim xs" data-tip="${esc(volumeCopiesTip(x))}">Unknown</span>`;
  const copies = x.copies || [], short = x.copies_short;
  // A detached volume short of copies says how many are whole; Longhorn may list none.
  const want = Math.max(+x.replicas || 0, copies.length, short?.wanted || 0), whole = short ? short.whole : null;
  const dots = Array.from({ length: want }, (_, i) => {
    const c = copies[i];
    const kind = whole !== null ? (i < whole ? "ok" : "missing") : !c && copies.length ? "missing" : !c ? "ok" : c.healthy ? "ok" : "warn";
    return `<i class="copydot ${kind}"></i>`;
  }).join("");
  const label = whole !== null ? `${whole} of ${want} copies whole` : copies.length ? `${copies.filter(c => c.healthy).length} of ${want} copies whole` : `${want} cop${want === 1 ? "y" : "ies"}`;
  return `<span class="copydots" role="img" tabindex="0" aria-label="${esc(label)}" data-card="volume-copies|${esc(volumeKey(x))}">${dots}</span>`;
}

/* The row's Copies cell: the dots, then a short line when a copy is missing,
   being rebuilt or restored. Its hover card gives Longhorn's reason. */
function volumeCopiesCell(x) {
  const busy = volumeBusy(x), problem = volumeProblem(x), key = esc(volumeKey(x));
  const doing = busy && !busy.error && `${x.restore ? "Restoring" : x.rebalancing ? "Moving a copy" : "Rebuilding"} ${+busy.pct || 0}%`;
  const note = doing ? `${volumeProgressBar(busy.pct)}${esc(doing)}` : problem && !problem.usage ? esc(problem.note) : "";
  return `${volumeCopyDots(x)}${note ? `<div class="vol-note xs" data-card="volume-health|${key}">${note}</div>` : ""}`;
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
  volumeKey, volumeByName, volumeTitle, accessWords, gibPerDay, reasonSentence, volumeProblem, GIB_FORMAT, gibText, volumeSummary, volumeCopyDots, volumeCopiesCell, volumeOverview,
};
