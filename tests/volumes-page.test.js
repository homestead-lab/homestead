"use strict";
// The Volumes page in the one-bar design (Nov 2026 mockup): the free space and
// the volumes that need a person in one line, rows of copies as dots, usage and
// when each was used, a labelled action only when one is needed, the details
// sidebar and hover cards.
const test = require("node:test"), assert = require("node:assert/strict"), fs = require("node:fs"), vm = require("node:vm");

function page() {
  const esc = x => String(x ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const c = { window: {}, console, Set, Map, Promise, JSON, document: { addEventListener() {} }, STATE: { data: {}, q: "" },
    esc, meter: pct => `<meter value="${pct}"></meter>`, tip: () => "?", toast() {}, fmtAgo: s => `${s}s ago`, fmtDay: () => "21 Nov",
    icon: n => `<svg data-icon="${n}"></svg>`, HOVER_CARDS: {}, DETAIL_KINDS: {} };
  c.jsArg = s => JSON.stringify(String(s ?? "")); c.jsq = s => esc(c.jsArg(s));
  vm.createContext(c);
  for (const f of ["web/js/ui.js", "web/js/details-panel.js", ...require("./helpers/storage-source").files()]) vm.runInContext(fs.readFileSync(f, "utf8"), c);
  // details-panel.js declares the sidebar kinds itself; the parts add to that one.
  c.DETAIL_KINDS = vm.runInContext("DETAIL_KINDS", c);
  return c;
}
const volume = (name, extra = {}) => ({ name: `pvc-${name}`, pvc_name: name, namespace: "lab", state: "attached", robustness: "healthy", replicas: 2,
  copies: [{ node: "host-a", disk: "disk-1", healthy: true }, { node: "host-b", disk: "disk-1", healthy: true }],
  access_modes: ["ReadWriteOnce"], size_gb: 20, actual_gb: 9, attached: ["web"], attached_to: "web", used_by: ["Deployment/web"],
  filesystem: { used_gb: 4, capacity_gb: 19.6, used_pct: 20 }, last_used_secs: 60, ...extra });
const forecast = rows => ({ rows, show_days: 60, warn_days: 14, min_days: 7 });

test("a volume's problem is a chip, a short note for the row and a sentence; healthy, detached and unused volumes have none", () => {
  const c = page();
  assert.equal(c.volumeProblem(volume("web")), null);
  assert.equal(c.volumeProblem(volume("old", { state: "detached", robustness: "unknown", attached: [], used_by: [] })), null, "unused shows in Attached to");
  const degraded = c.volumeProblem(volume("db", { robustness: "degraded", health_reason: "no disk space to create the replicas required", copies: [{ node: "host-a", disk: "d", healthy: true }] }));
  assert.equal(degraded.tone, "warn");
  assert.equal(degraded.note, "1 copy is missing.");
  assert.equal(degraded.word, "is missing a copy");
  assert.equal(degraded.line, "No disk space to create the replicas required.");
  const short = c.volumeProblem(volume("cloud", { state: "detached", copies_short: { whole: 1, wanted: 2 } }));
  assert.equal(short.chip, "1 of 2 copies");
  assert.equal(short.word, "is missing a copy");
  assert.equal(c.volumeProblem(volume("lost", { state: "detached", copies_short: { whole: 0, wanted: 2 } })).tone, "bad");
  c.STATE.data.forecast = forecast([{ key: "volume:pvc-cam", kind: "volume", volume: "pvc-cam", label: "cam", days_left: 9, per_day: 1024 ** 3 / 2, full_on: "2026-12-01", pct: 80 }]);
  const full = c.volumeProblem(volume("cam"));
  assert.match(full.word, /^is full in about 9 days$/);
  assert.equal(full.usage, true, "filling up is shown under the usage, not the copies");
});

test("the summary leads with the free space, then links each volume that needs a person, copies first, set apart by dots", () => {
  const c = page();
  c.STATE.data.forecast = forecast([{ key: "volume:pvc-cam", kind: "volume", volume: "pvc-cam", label: "cam", days_left: 11, per_day: 1, full_on: "2026-12-02", pct: 78 }]);
  const vols = [volume("cam"), volume("web"), volume("db", { robustness: "degraded" }), volume("spare", { state: "detached", attached: [], used_by: [] })];
  const line = c.volumeSummary(vols, { avail_gb: 906, cap_gb: 1392 });
  const wide = line.match(/<span class="sum-wide">([\s\S]*)<\/span><span class="sum-narrow">/)[1];
  assert.match(wide, /^<span data-card="storage\|pool"><b>906 GiB of 1,392 GiB is free\.<\/b><\/span> <button[^>]*onclick="openDetails\('volume',&quot;pvc-db&quot;\)">db is missing a copy<\/button>/);
  assert.match(wide, /db is missing a copy<\/button> <span class="dim sum-dot" aria-hidden="true">·<\/span> <button[^>]*>cam is full in about 11 days<\/button>$/);
  assert.doesNotMatch(wide, /spare/, "an unused volume is not a problem");
  assert.match(line, /<span class="sum-narrow"><b>906 GiB free\.<\/b> <button[^>]*onclick="volumeNeedsOnly\(true\)">2 volumes need attention<\/button><\/span>$/);
  assert.match(c.volumeSummary([volume("web")], null, { reading: true }), /Reading the free space…/);
  assert.match(c.volumeSummary([volume("web")], { avail_gb: 10, cap_gb: 20 }), /<b>10 GiB of 20 GiB is free\.<\/b><\/span><\/span>/);
});

test("a row has a labelled action only when something needs doing, and its ⋯ menu is grouped", () => {
  const c = page();
  const first = x => (c.volumeActions(x).match(/<div class="actionbar"><button[^>]*>(?:<svg[^>]*><\/svg>)?([^<]+)<\/button>/) || [])[1];
  assert.equal(first(volume("web")), undefined, "a healthy volume has only ⋯");
  assert.equal(first(volume("full", { filesystem: { used_gb: 19, capacity_gb: 19.6, used_pct: 96 } })), "Expand volume");
  assert.equal(first(volume("cloud", { state: "detached", copies_short: { whole: 1, wanted: 2 } })), "Rebuild copy");
  assert.match(c.volumeActions(volume("cloud", { state: "detached", copies_short: { whole: 1, wanted: 2 } })), /data-plain="true"/, "the labelled button shows its words alone");
  assert.match(c.volumeActions(volume("web")), /actionmenu-group[^>]*>Use<[\s\S]*actionmenu-group[^>]*>Change<[\s\S]*actionmenu-group[^>]*>Remove<[\s\S]*Delete volume…/);
});

test("copies are dots: whole, not running, missing, or unknown when Longhorn could not say", () => {
  const c = page();
  assert.equal((c.volumeCopyDots(volume("web")).match(/copydot ok/g) || []).length, 2);
  assert.match(c.volumeCopyDots(volume("web", { replicas: 3 })), /copydot missing/);
  assert.match(c.volumeCopyDots(volume("web", { copies: [{ node: "host-a", disk: "d", healthy: false }] })), /copydot warn[\s\S]*copydot missing/);
  assert.match(c.volumeCopyDots(volume("web", { copies: null })), />Unknown</);
  // A detached volume short of copies: the whole ones, then rings for the missing.
  assert.match(c.volumeCopyDots(volume("cloud", { state: "detached", copies: [], copies_short: { whole: 1, wanted: 2 } })), /copydot ok"><\/i><i class="copydot missing"/);
});

test("the Copies cell says under the dots what is missing or how far a rebuild is", () => {
  const c = page();
  assert.doesNotMatch(c.volumeCopiesCell(volume("web")), /ui-reason/);
  assert.match(c.volumeCopiesCell(volume("cloud", { state: "detached", copies_short: { whole: 1, wanted: 2 } })), /<div class="ui-reason under xs" data-card="volume-health\|pvc-cloud">1 copy is missing\.<\/div>/);
  assert.match(c.volumeCopiesCell(volume("db", { rebuild: { pct: 63, replicas: 1 } })), /role="progressbar" aria-valuenow="63"[\s\S]*Rebuilding 63%<\/div>/);
  c.STATE.data.forecast = forecast([{ key: "volume:pvc-cam", kind: "volume", volume: "pvc-cam", label: "cam", days_left: 9, per_day: 1, full_on: "2026-12-01", pct: 80 }]);
  assert.doesNotMatch(c.volumeCopiesCell(volume("cam")), /ui-reason/, "filling up is under the usage");
  assert.match(c.volumeUsageCell(volume("cam")), /forecast-tag[\s\S]*Full in about 9 days/);
});

test("a volume's sidebar and hover cards come from the list the page read, escaped", () => {
  const c = page();
  c.STATE.data.vols = [volume("<b>x</b>", { name: "pvc-x" }), volume("cloud", { state: "detached", attached: [], used_by: ["Deployment/nextcloud"], copies: [], copies_short: { whole: 1, wanted: 2 } })];
  assert.equal(c.DETAIL_KINDS.volume.head("pvc-x").title, "<b>x</b>");
  assert.equal(c.DETAIL_KINDS.volume.head("pvc-cloud").sub, "lab · used by nextcloud, which is stopped");
  assert.deepEqual([...c.DETAIL_KINDS.volume.tabs.map(t => t.label)], ["Overview", "Snapshots", "Activity", "Settings"]);
  // Overview (the mockup): the problem, then Space, Copies and Protection.
  const overview = c.DETAIL_KINDS.volume.tabs[0].html("pvc-cloud");
  assert.match(overview, /1 of 2 copies[\s\S]*<h4>Space<\/h4>[\s\S]*<h4>Copies<\/h4>[\s\S]*<h4>Protection<\/h4>/);
  assert.match(overview, /<dt>Copy 1<\/dt><dd>Whole<\/dd><\/div><div><dt>Copy 2<\/dt><dd>Missing<\/dd>/);
  assert.match(c.volumeCopyTiles(volume("web")), /<dt>host-a<\/dt><dd>Stored on disk-1 · healthy<\/dd>/);
  assert.match(c.volumeSpace(volume("web", { filesystem: { used_gb: 3.8, capacity_gb: 19.6, used_pct: 19 } })), /3\.8 of 19\.6 GiB of files[\s\S]*19%[\s\S]*ui-meter/);
  // Settings: what it is, with the icons.
  const settings = c.volumeSettingsTab("pvc-cloud");
  for (const label of ["Size", "Longhorn footprint", "Copies", "Access", "Engine", "Last used"]) assert.match(settings, new RegExp(`</svg>${label}</dt>`), label);
  assert.deepEqual([...c.DETAIL_KINDS.volume.actions("pvc-cloud").slice(0, 2).map(i => i.label)], ["Rebuild copy", "Take snapshot"]);
  assert.deepEqual([...c.DETAIL_KINDS.volume.actions("pvc-x").slice(0, 2).map(i => i.label)], ["Take snapshot", "Open snapshots"], "a healthy volume leads with a snapshot");
  assert.doesNotMatch(c.HOVER_CARDS.volume("pvc-x"), /<b>x<\/b>/);
  assert.match(c.HOVER_CARDS["volume-copies"]("pvc-x"), /host-a[\s\S]*disk-1/);
  assert.match(c.HOVER_CARDS["volume-health"]("pvc-cloud"), /Why/);
  assert.equal(c.DETAIL_KINDS.volume.head("pvc-gone"), null, "a volume that is gone has nothing to show");
});
