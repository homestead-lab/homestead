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
  const parts = [...troubled.slice(0, 3).map(([x, p]) => ({ text: `${volumeTitle(x)} ${p.word}`, tone: p.tone, run: `openDetails('volume',${jsq(volumeKey(x))})` })),
    troubled.length > 3 && { text: `${troubled.length - 3} more`, note: true },
    ...stores.map(r => ({ text: `${r.kind === "pool" ? "All Longhorn storage" : `The disk ${r.label}`} is full ${forecastWhen(r.days_left)}`,
      tone: forecastTone(r.days_left, warn), run: r.kind === "disk" ? `nodeDetail(${jsq(r.node)})` : "" })),
    q && { text: `${v.length === 1 ? "1 volume matches" : `${v.length} volumes match`} “${q}”`, note: true }];
  const needs = troubled.length + stores.length;
  return UI.summary({ head: free, parts, narrow: { head: st ? `<b>${gibText(st.avail_gb)} GiB free.</b>` : free,
    parts: [needs && { text: `${needs === 1 ? "1 volume needs" : `${needs} volumes need`} attention`, tone: "warn", run: "volumeNeedsOnly(true)" }] } });
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
  const busy = volumeBusy(x), problem = volumeProblem(x);
  const doing = busy && !busy.error && `${x.restore ? "Restoring" : x.rebalancing ? "Moving a copy" : "Rebuilding"} ${+busy.pct || 0}%`;
  const note = doing ? `${volumeProgressBar(busy.pct)}${esc(doing)}` : problem && !problem.usage ? esc(problem.note) : "";
  return `${volumeCopyDots(x)}${note ? UI.reason(note, { under: true, card: `volume-health|${volumeKey(x)}` }) : ""}`;
}

/* The details sidebar's reads (detailsData): the volume's snapshots and
   backups (the cluster's events are details-panel.js's). `keep`, so leaving the page does
   not cancel them half way. */
const volumeProtectRead = x => () => Promise.all([
  api(`/api/lh/snapshots?volume=${encodeURIComponent(x.name)}`, { keep: true }),
  // Unread backups are not "no backups" (#620): the snapshots still show.
  api(`/api/lh/backups?volume=${encodeURIComponent(x.name)}`, { keep: true }).catch(e => ({ unread: e.message })),
]).then(([snaps, backups]) => ({ snaps: Array.isArray(snaps) ? snaps : [], backups }));
const newestFirst = rows => [...(rows || [])].sort((a, b) => (Date.parse(b.created) || 0) - (Date.parse(a.created) || 0));
const dayText = at => fmtDay(at) || "—";

/* Space: the files against the file system while an app uses it, otherwise
   what Longhorn stores against the size. The mark is where it warns. */
function volumeSpace(x) {
  const fs = x.filesystem, measured = fs && Number.isFinite(fs.used_pct);
  const pct = measured ? fs.used_pct : Number.isFinite(+x.used_pct) ? +x.used_pct : null;
  const words = measured ? `${gibText(fs.used_gb)} of ${gibText(fs.capacity_gb)} GiB of files` : `${gibText(x.actual_gb)} of ${gibText(x.size_gb)} GiB stored`;
  return `<div class="details-space"><div class="between small"><span>${esc(words)}</span>${pct !== null ? `<span class="dim">${Math.round(pct)}%</span>` : ""}</div>`
    + `${pct !== null ? UI.meter({ now: pct, warnAt: 85, label: `Space used on ${volumeTitle(x)}` }) : ""}</div>`;
}

/* Copies: a tile for each copy the volume should have - its host and disk
   when Longhorn lists it, then the missing ones. */
function volumeCopyTiles(x) {
  if (x.copies === null) return `<p class="dim small">${esc(volumeCopiesTip(x))}</p>`;
  const copies = x.copies || [], short = x.copies_short;
  const want = Math.max(+x.replicas || 0, copies.length, short?.wanted || 0);
  const tiles = copies.map(c => [c.node, `Stored on ${esc(c.disk)} · ${c.healthy ? "healthy" : esc(c.state || "not running")}`]);
  // A detached volume short of copies says how many are whole, but not where.
  if (!copies.length) for (let i = 0; i < (short ? short.whole : want); i++) tiles.push([`Copy ${i + 1}`, "Whole"]);
  while (tiles.length < want) tiles.push([`Copy ${tiles.length + 1}`, "Missing"]);
  return UI.facts(tiles, { icons: false });
}

/* Protection: the newest snapshot and backup, and how many are kept. */
function volumeProtection(x) {
  return detailsSlot(detailsData("protect", volumeProtectRead(x)), ({ snaps, backups }) => {
    const snap = newestFirst(snaps)[0], unread = !Array.isArray(backups), backup = unread ? null : newestFirst(backups)[0];
    return UI.facts([
      ["Last snapshot", snap ? `${esc(dayText(snap.created))} · ${snaps.length} kept` : "None"],
      ["Last backup", unread ? '<span class="dim">Could not be read</span>' : backup ? `${esc(dayText(backup.created))} · ${backups.length} kept` : "None"],
    ], { icons: false });
  }, { reading: "Reading the snapshots and backups…", failed: "The snapshots could not be read" });
}

/* Overview (the mockup): what is wrong first, then Space, Copies and Protection. */
function volumeOverview(name) {
  const x = volumeByName(name);
  if (!x) return "";
  const problem = volumeProblem(x), use = volumeUse(x);
  const notice = [problem && UI.callout(problem.tone, problem.chip, esc(problem.line)),
    volumeBusy(x) && !problem ? volumeHealthCell(x) : "",
    ["orphaned", "unclaimed"].includes(use.kind) && UI.callout("info", "No app uses this volume", "Delete it if the data is not needed, or attach it to an app from the app's editor.")].filter(Boolean).join("");
  return notice + UI.section("Space", volumeSpace(x)) + UI.section("Copies", volumeCopyTiles(x)) + UI.section("Protection", volumeProtection(x));
}

/* Snapshots: the timeline and the backups, as the Snapshots dialog shows them. */
function volumeSnapshotsTab(name) {
  const x = volumeByName(name);
  if (!x) return "";
  const vol = jsq(x.name), label = volumeTitle(x);
  return detailsSlot(detailsData("protect", volumeProtectRead(x)), ({ snaps, backups }) =>
    UI.section(`Snapshots (${snaps.length})`, snapshotTimeline(snaps, x.name, label))
    + UI.section(Array.isArray(backups) ? `Backups (${backups.length})` : "Backups", Array.isArray(backups) ? volumeBackupList(backups, x.name, label)
      : UI.callout("warn", "This volume's backups could not be read", esc(backups.unread)))
    + `<div class="details-foot">${actionBar([
      { label: "Back up now", run: `volumeProtectNow('backup',${vol})`, need: "operator" },
      { label: "Trim now", run: `lhTrimNow(${vol})`, need: "operator", tip: "Give back to Longhorn the space that deleted files still use. Every volume is also trimmed weekly." },
      { label: "Open the Snapshots dialog", run: `lhSnaps(${vol},${jsq(label)})` }], { shown: 3 })}</div>`,
  { reading: "Reading the snapshots and backups…", failed: "The snapshots could not be read" });
}

/* The backups, newest first, narrow enough for the sidebar: each one's state,
   size and date, with Restore and the rest in its ⋯. */
function volumeBackupList(backups, vol, label) {
  return UI.list(newestFirst(backups).map(b => ({ title: b.name, mono: true, detail: `${gibText(b.size_mb)} MiB · ${dayText(b.created)}`,
    noteHtml: b.error ? `<span class="warntext xs">${esc(b.error)}</span>` : "",
    chipHtml: UI.chip(b.state || "Unknown", b.state === "Completed" ? "ok" : b.state === "Error" ? "bad" : "warn", { size: "slim" }),
    actionsHtml: actionBar([
      b.restorable && { label: "Restore", run: `lhRestore(${jsq(b.name)},${jsq(label)})`, need: "admin", plain: true },
      b.restorable && { label: "Restore into app", run: `lhRestoreInto(${jsq(b.name)})`, need: "admin", tip: "Restore this backup and move the app that uses this volume onto it." },
      { label: "Delete", run: `lhBackupDel(${jsq(b.name)},${jsq(vol)},${jsq(label)})`, need: "admin", danger: true, tip: "Delete this backup from the backup target." },
    ].filter(Boolean), { shown: b.restorable ? 1 : 0, label: `More actions for ${b.name}` }) })), { empty: "No backups yet. Back up now needs a backup target." });
}

/* Activity: what Kubernetes said about it lately, and its snapshots and backups. */
function volumeActivityTab(name) {
  const x = volumeByName(name);
  if (!x) return "";
  const events = detailsData("events", clusterEventsRead), protect = detailsData("protect", volumeProtectRead(x));
  if (events.loading && protect.loading) return detailsSlot(events, () => "", { reading: "Reading what happened…" });
  const mine = e => [x.pvc_name, x.name].includes(e.obj) && (!e.ns || !x.namespace || e.ns === x.namespace);
  const backups = Array.isArray(protect.data?.backups) ? protect.data.backups : [];
  const rows = [
    ...(events.data?.events || []).filter(mine).map(e => ({ at: e.time, title: eventWords(e.reason), detail: reasonSentence(e.msg), tone: e.type === "Warning" ? "warn" : "" })),
    ...(protect.data?.snaps || []).map(s => ({ at: s.created, title: s.source === "system" ? "Longhorn took a snapshot" : "Snapshot taken", detail: `${s.size_mb} MiB` })),
    ...backups.map(b => ({ at: b.created, title: b.state === "Error" ? "Backup failed" : "Backed up", detail: b.error || `${b.size_mb} MiB`, tone: b.state === "Error" ? "bad" : "" })),
  ];
  return (events.error ? UI.callout("warn", "The cluster's events could not be read", esc(events.error)) : "")
    + UI.activity(rows, { empty: "Nothing has been recorded for this volume lately." })
    + '<p class="dim xs">Kubernetes keeps its events for about an hour. Snapshots and backups are kept until they are deleted.</p>';
}

/* Settings: what the volume is, and the changes that can be made to it. */
function volumeSettingsTab(name) {
  const x = volumeByName(name);
  if (!x) return "";
  const change = volumeActionItems(x).filter(i => i.group === "Change");
  return UI.facts([
    ["Size", `<span class="mono">${esc(x.size_gb)} GiB</span>`, "disk"],
    ["Longhorn footprint", `<span class="mono">${esc(x.actual_gb ?? "—")} GiB</span>`, "layers"],
    ["Copies", esc(`${x.replicas || (x.copies || []).length || "?"} wanted`), "copy"],
    ["Access", esc(accessWords(x.access_modes)), "link"],
    ["Engine", x.engine === "v2" ? "Longhorn V2 (SPDK)" : "Longhorn V1", "chip"],
    x.storage_class && ["Storage class", esc(x.storage_class), "sliders"],
    x.state === "attached" ? ["Host", esc(x.node || "—"), "node"] : ["Last used", esc(fmtAgo(x.last_used_secs)), "clock"],
    ["Namespace", esc(x.namespace || "—"), "list"],
    ["Longhorn volume", `<span class="mono small">${esc(x.name)}</span>`, "info"],
  ]) + (change.length ? `<div class="details-foot">${actionBar(change.map(i => ({ ...i, plain: true })), { shown: 2, label: `More changes to ${volumeTitle(x)}` })}</div>` : "");
}

/* A snapshot or backup taken from the sidebar: the sidebar reads them again. */
if (typeof window === "object") window.volumeProtectNow = async (kind, vol) => {
  try {
    const send = { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ volume: vol }) };
    // Each path written out, so the API contract test can read it.
    await (kind === "backup" ? api("/api/lh/backup", send) : api("/api/lh/snapshot", send));
    toast(kind === "backup" ? "Backup started" : "Snapshot taken", "ok");
    detailsForget("protect");
  } catch (e) { toast(e.message, "bad"); }
};

if (typeof DETAIL_KINDS === "object") DETAIL_KINDS.volume = {
  head: name => {
    const x = volumeByName(name);
    if (!x) return null;
    const by = attachedWorkloads(x), use = volumeUse(x);
    const stopped = use.kind === "stopped" ? use.by.map(w => w.split("/").pop()) : [];
    const users = by.length ? `used by ${by.join(", ")}` : stopped.length ? `used by ${stopped.join(", ")}, which ${stopped.length === 1 ? "is" : "are"} stopped` : x.state;
    return { title: volumeTitle(x), sub: [x.namespace, users].filter(Boolean).join(" · "),
      cluster: x.site && typeof fleetAll === "function" && fleetAll() ? x.site.id : "" };
  },
  // Rebuild copy or Expand volume when one is needed, then Take snapshot.
  actions: name => {
    const x = volumeByName(name);
    if (!x) return [];
    const items = volumeActionItems(x), soon = forecastFor(`volume:${x.name}`);
    const main = items.find(i => i.label === "Rebuild copy") || ((soon || x.filesystem?.used_pct >= 90) && items.find(i => i.label === "Expand volume"));
    const snap = items.find(i => i.label === "Take snapshot");
    const lead = [main, snap].filter(Boolean);
    return [...lead, ...items.filter(i => !lead.includes(i))];
  },
  shown: 2,
  tabs: [
    { id: "overview", label: "Overview", html: volumeOverview },
    { id: "snapshots", label: "Snapshots", html: volumeSnapshotsTab },
    { id: "activity", label: "Activity", html: volumeActivityTab },
    { id: "settings", label: "Settings", html: volumeSettingsTab },
  ],
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
  volumeKey, volumeByName, volumeTitle, accessWords, gibPerDay, reasonSentence, volumeProblem, GIB_FORMAT, gibText, volumeSummary, volumeCopyDots,
  volumeCopiesCell, volumeProtectRead, newestFirst, dayText, volumeSpace, volumeCopyTiles, volumeProtection, volumeOverview, volumeSnapshotsTab,
  volumeBackupList, volumeActivityTab, volumeSettingsTab,
};
