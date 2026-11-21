"use strict";
// The Volumes page in the one-bar design (Nov 2026): a summary sentence, one
// attention list, rows with a quiet state, copies as dots and one labelled
// action, the details sidebar and hover cards.
const test = require("node:test"), assert = require("node:assert/strict"), fs = require("node:fs"), vm = require("node:vm");

function page() {
  const esc = x => String(x ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const c = { window: {}, console, Set, Map, Promise, JSON, document: { addEventListener() {} }, STATE: { data: {}, q: "" },
    esc, meter: pct => `<meter value="${pct}"></meter>`, tip: () => "?", toast() {}, fmtAgo: s => `${s}s ago`,
    icon: n => `<svg data-icon="${n}"></svg>`, HOVER_CARDS: {}, DETAIL_KINDS: {} };
  c.jsArg = s => JSON.stringify(String(s ?? "")); c.jsq = s => esc(c.jsArg(s));
  vm.createContext(c);
  for (const f of ["web/js/ui.js", ...require("./helpers/storage-source").files()]) vm.runInContext(fs.readFileSync(f, "utf8"), c);
  return c;
}
const volume = (name, extra = {}) => ({ name: `pvc-${name}`, pvc_name: name, namespace: "lab", state: "attached", robustness: "healthy", replicas: 2,
  copies: [{ node: "host-a", disk: "disk-1", healthy: true }, { node: "host-b", disk: "disk-1", healthy: true }],
  access_modes: ["ReadWriteOnce"], size_gb: 20, actual_gb: 9, attached: ["web"], attached_to: "web", used_by: ["Deployment/web"],
  filesystem: { used_gb: 4, capacity_gb: 19.6, used_pct: 20 }, last_used_secs: 60, ...extra });
const forecast = rows => ({ rows, show_days: 60, warn_days: 14, min_days: 7 });

test("a volume's problem is a chip and a sentence; healthy, detached and unused volumes have none", () => {
  const c = page();
  assert.equal(c.volumeProblem(volume("web")), null);
  assert.equal(c.volumeProblem(volume("old", { state: "detached", robustness: "unknown", attached: [], used_by: [] })), null, "unused is for the attention list");
  const degraded = c.volumeProblem(volume("db", { robustness: "degraded", health_reason: "no disk space to create the replicas required" }));
  assert.equal(degraded.tone, "warn");
  assert.equal(degraded.line, "No disk space to create the replicas required.");
  assert.equal(c.volumeProblem(volume("cloud", { state: "detached", copies_short: { whole: 1, wanted: 2 } })).chip, "1 of 2 copies");
  assert.equal(c.volumeProblem(volume("lost", { state: "detached", copies_short: { whole: 0, wanted: 2 } })).tone, "bad");
  c.STATE.data.forecast = forecast([{ key: "volume:pvc-cam", kind: "volume", volume: "pvc-cam", label: "cam", days_left: 9, per_day: 1024 ** 3 / 2, full_on: "2026-12-01", pct: 80 }]);
  assert.match(c.volumeProblem(volume("cam")).word, /^is full in about 9 days$/);
});

test("the summary counts the healthy volumes, links each one that is not, and says the free space", () => {
  const c = page();
  const vols = [volume("web"), volume("db", { robustness: "degraded" }), volume("spare", { state: "detached", attached: [], used_by: [] })];
  const line = c.volumeSummary(vols, { avail_gb: 906, cap_gb: 1392 }, { spare: 1 });
  assert.match(line, /<b>2 of 3 volumes are healthy\.<\/b>/);
  assert.match(line, /onclick="openDetails\('volume',&quot;pvc-db&quot;\)">db is missing a copy<\/button>\./);
  assert.match(line, /<span data-card="storage\|pool">906 GiB of 1392 GiB is free\.<\/span>/);
  assert.match(line, />1 volume is not used by any app\.<\/button>/);
  assert.match(c.volumeSummary([volume("web")], null), /^<b>The volume is healthy\.<\/b>$/);
});

test("the attention list gives each thing to do its own line and button, and is gone when there is nothing", () => {
  const c = page();
  assert.equal(c.volumeAttention([volume("web")], forecast([]), []), "");
  const cam = volume("cam"), cloud = volume("cloud", { state: "detached", copies_short: { whole: 1, wanted: 2 } });
  const html = c.volumeAttention([cam, cloud], forecast([
    { key: "volume:pvc-cam", kind: "volume", volume: "pvc-cam", label: "cam", days_left: 11.2, per_day: 418759311, full_on: "2026-12-02", pct: 78 },
    { key: "disk:host-b:disk-1", kind: "disk", node: "host-b", label: "host-b · disk-1", days_left: 45, per_day: 7e9, full_on: "2027-01-05", pct: 65 },
    { key: "pool", kind: "pool", label: "all", days_left: null, why: "not growing" }]), [{ was: "old-data" }]);
  assert.match(html, /<b>cam is full in about 11 days\.<\/b>[\s\S]*Expand volume/);
  assert.match(html, /<b>The disk host-b · disk-1 is full in about 45 days\.<\/b>[\s\S]*nodeDetail\(&quot;host-b&quot;\)[\s\S]*Open host/);
  assert.match(html, /<b>cloud has 1 of 2 copies\.<\/b>[\s\S]*volumeRebuild\(&quot;pvc-cloud&quot;\)[\s\S]*Rebuild now/);
  assert.match(html, /<b>1 old copy is kept from a storage class change\.<\/b>/);
  assert.doesNotMatch(html, /not growing/, "a store that is not filling is not listed");
});

test("a row's labelled action fits the volume, and its ⋯ menu is grouped", () => {
  const c = page();
  const first = x => (c.volumeActions(x).match(/<div class="actionbar"><button[^>]*>(?:<svg[^>]*><\/svg>)?([^<]+)<\/button>/) || [])[1];
  assert.equal(first(volume("web")), "Open snapshots");
  assert.equal(first(volume("full", { filesystem: { used_gb: 19, capacity_gb: 19.6, used_pct: 96 } })), "Expand volume");
  assert.equal(first(volume("cloud", { state: "detached", copies_short: { whole: 1, wanted: 2 } })), "Rebuild now");
  assert.match(c.volumeActions(volume("web")), /actionmenu-group[^>]*>Use<[\s\S]*actionmenu-group[^>]*>Change<[\s\S]*actionmenu-group[^>]*>Remove<[\s\S]*Delete volume…/);
});

test("copies are dots: whole, not running, missing, or unknown when Longhorn could not say", () => {
  const c = page();
  assert.equal((c.volumeCopyDots(volume("web")).match(/copydot ok/g) || []).length, 2);
  assert.match(c.volumeCopyDots(volume("web", { replicas: 3 })), /copydot missing/);
  assert.match(c.volumeCopyDots(volume("web", { copies: [{ node: "host-a", disk: "d", healthy: false }] })), /copydot warn[\s\S]*copydot missing/);
  assert.match(c.volumeCopyDots(volume("web", { copies: null })), />Unknown</);
});

test("a volume's sidebar and hover cards come from the list the page read, escaped", () => {
  const c = page();
  c.STATE.data.vols = [volume("<b>x</b>", { name: "pvc-x" }), volume("cloud", { state: "detached", attached: [], used_by: ["Deployment/nextcloud"], copies_short: { whole: 1, wanted: 2 } })];
  assert.equal(c.DETAIL_KINDS.volume.head("pvc-x").title, "<b>x</b>");
  assert.equal(c.DETAIL_KINDS.volume.head("pvc-cloud").sub, "lab · used by nextcloud, stopped");
  const overview = c.DETAIL_KINDS.volume.tabs[0].html("pvc-cloud");
  assert.match(overview, /cloud has 1 of 2 copies/);
  for (const label of ["State", "Used by", "Files", "Size", "Longhorn footprint", "Copies", "Access", "Engine", "Last used"]) assert.match(overview, new RegExp(`</svg>${label}</dt>`), label);
  assert.equal([...c.DETAIL_KINDS.volume.actions("pvc-cloud")][0].label, "Rebuild now");
  assert.doesNotMatch(c.HOVER_CARDS.volume("pvc-x"), /<b>x<\/b>/);
  assert.match(c.HOVER_CARDS["volume-copies"]("pvc-x"), /host-a[\s\S]*disk-1/);
  assert.match(c.HOVER_CARDS["volume-health"]("pvc-cloud"), /Why/);
  assert.equal(c.DETAIL_KINDS.volume.head("pvc-gone"), null, "a volume that is gone has nothing to show");
});
