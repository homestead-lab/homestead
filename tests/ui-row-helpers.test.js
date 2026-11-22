"use strict";
// The shared pieces every list page builds its summary line and rows from
// (design.md "What needs a person is in the summary line", "Resource rows"):
// UI.summary and UI.summaryPart, UI.reason and UI.list.
const test = require("node:test"), assert = require("node:assert/strict"), fs = require("node:fs"), vm = require("node:vm");

function ui() {
  const esc = x => String(x ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const c = { window: {}, console, Set, Map, JSON, esc, icon: n => `<svg data-icon="${n}"></svg>`, document: { addEventListener() {} } };
  vm.createContext(c);
  vm.runInContext(fs.readFileSync("web/js/ui.js", "utf8"), c);
  return vm.runInContext("UI", c);
}
const DOT = ' <span class="dim sum-dot" aria-hidden="true">·</span> ';

test("a summary part is a link with a run, quiet words with note, otherwise words in its tone, all escaped", () => {
  const UI = ui();
  assert.equal(UI.summaryPart({ text: "<b>db</b> is missing a copy", tone: "warn", run: "openDetails('volume',&quot;db&quot;)" }),
    `<button type="button" class="linkish sum-link sum-warn" onclick="openDetails('volume',&quot;db&quot;)">&lt;b&gt;db&lt;/b&gt; is missing a copy</button>`);
  assert.equal(UI.summaryPart({ text: "2 more", note: true, tip: "The rest are in the list" }), '<span class="sum-note" data-tip="The rest are in the list">2 more</span>');
  assert.equal(UI.summaryPart({ text: "All Longhorn storage is full in about 9 days", tone: "warn" }), '<span class="sum-warn">All Longhorn storage is full in about 9 days</span>');
  assert.equal(UI.summaryPart({ text: "Show all", run: "showAll()" }), '<button type="button" class="linkish sum-link" onclick="showAll()">Show all</button>');
  assert.equal(UI.summaryPart(false), "");
});

test("a summary line is the head, then its parts set apart by dots; a phone gets its own when given", () => {
  const UI = ui();
  const line = UI.summary({ head: "<b>4 of 6 apps are running normally.</b>", parts: [{ text: "paperless is down", tone: "bad", run: "a()" }, null, { text: "1 app is stopped", note: true }],
    narrow: { head: "<b>4 of 6 apps are running.</b>", parts: [{ text: "paperless is down", tone: "bad", run: "a()" }] } });
  assert.equal(line, `<span class="sum-wide"><b>4 of 6 apps are running normally.</b> <button type="button" class="linkish sum-link sum-bad" onclick="a()">paperless is down</button>${DOT}<span class="sum-note">1 app is stopped</span></span>`
    + `<span class="sum-narrow"><b>4 of 6 apps are running.</b> <button type="button" class="linkish sum-link sum-bad" onclick="a()">paperless is down</button></span>`);
  assert.equal(UI.summary({ head: "<b>All 3 apps are running normally.</b>" }), '<span class="sum-wide"><b>All 3 apps are running normally.</b></span><span class="sum-narrow"><b>All 3 apps are running normally.</b></span>', "nothing to point to, no trailing space");
});

test("a reason is quiet words beside a chip, or under a value with the hover card it opens", () => {
  const UI = ui();
  assert.equal(UI.reason("Responses take 2.4 seconds."), '<span class="ui-reason xs">Responses take 2.4 seconds.</span>');
  assert.equal(UI.reason("1 copy is missing.", { under: true, card: "volume-health|pvc-a" }), '<div class="ui-reason under xs" data-card="volume-health|pvc-a">1 copy is missing.</div>');
});

test("a list row is a title and a line under it, then its chip and actions; an empty list says so", () => {
  const UI = ui();
  assert.equal(UI.list([{ title: "backup-<1>", mono: true, detail: "1,842.6 MiB · 19 Sept", chipHtml: "<i>chip</i>", actionsHtml: "<i>acts</i>" }]),
    '<div class="ui-list"><div class="ui-list-row"><div class="ui-list-main"><b class="mono small">backup-&lt;1&gt;</b><span class="dim xs">1,842.6 MiB · 19 Sept</span></div><i>chip</i><i>acts</i></div></div>');
  assert.equal(UI.list([], { empty: "No backups yet." }), '<div class="ui-empty">No backups yet.</div>');
});

test("pages build their summary line, reasons and short lists from these, not by hand", () => {
  for (const file of ["web/js/views-workloads-list.js", "web/js/views-storage-volume-details.js", "web/js/views-storage-volumes.js"]) {
    const src = fs.readFileSync(file, "utf8");
    assert.doesNotMatch(src, /class="linkish sum-link|class="sum-note|class="sum-wide|class="(wl|vol)-(reason|note)|class="details-list/, `${file} builds them by hand`);
  }
});
