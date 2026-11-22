"use strict";
// The details sidebar's tabs after the Nov 2026 mockup: what a tab reads from
// the server is read once when it is first shown and kept a while
// (detailsData), drawn by detailsSlot, and built from the shared pieces -
// tiles without icons, an activity list and a log's last lines.
const test = require("node:test"), assert = require("node:assert/strict"), fs = require("node:fs"), vm = require("node:vm");

function panel() {
  const esc = x => String(x ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const c = { window: {}, console, Set, Map, Promise, JSON, Date, esc, icon: n => `<svg data-icon="${n}"></svg>`,
    document: { addEventListener() {}, getElementById: () => null }, jsArg: s => JSON.stringify(String(s ?? "")) };
  c.jsq = s => esc(c.jsArg(s));
  vm.createContext(c);
  for (const f of ["web/js/core-format.js", "web/js/ui.js", "web/js/details-panel.js"]) vm.runInContext(fs.readFileSync(f, "utf8"), c);
  // Top-level consts are not properties of the context: copy out the ones read here.
  for (const name of ["UI", "DETAILS_WANTED", "DETAILS_DATA", "eventWords"]) c[name] = vm.runInContext(name, c);
  return c;
}

test("a tab's data is asked for while it is drawn, read once after, and shown when it comes", async () => {
  const c = panel();
  let reads = 0;
  const read = async () => { reads += 1; return { n: 7 }; };
  vm.runInContext('DETAILS.building = "volume|pvc-a"', c);
  assert.equal(c.detailsData("protect", read).loading, true, "nothing is read until the panel is drawn");
  assert.equal(c.DETAILS_WANTED.size, 1);
  c.detailsReadWanted();
  c.detailsReadWanted();
  await new Promise(r => setTimeout(r, 0));
  assert.equal(reads, 1, "asked twice, read once");
  assert.deepEqual({ ...c.detailsData("protect", read).data }, { n: 7 });
  assert.equal(c.DETAILS_WANTED.size, 0, "fresh data is not read again");
  // Another item reads its own.
  vm.runInContext('DETAILS.building = "volume|pvc-b"', c);
  assert.equal(c.detailsData("protect", read).loading, true);
});

test("a slot says it is reading, says why it could not, or draws what it read", () => {
  const c = panel();
  assert.match(c.detailsSlot({ loading: true }, () => "", { reading: "Reading its log…" }), /details-reading[\s\S]*Reading its log…/);
  assert.match(c.detailsSlot({ error: "<b>timeout</b>" }, () => "", { failed: "Its log could not be read" }), /Its log could not be read[\s\S]*&lt;b&gt;timeout/);
  assert.equal(c.detailsSlot({ data: [1, 2] }, d => `${d.length} rows`), "2 rows");
});

test("the sidebar's buttons are words alone, the first the main one, and the kind says how many show", () => {
  const c = panel();
  c.actionBar = vm.runInContext("actionBar", c);
  vm.runInContext(`DETAIL_KINDS.thing = { head: () => ({ title: "thing" }), shown: 2,
    actions: () => [{ label: "Rebuild copy", run: "a()", icon: "refresh" }, { label: "Take snapshot", run: "b()" }, { label: "Delete", run: "c()" }],
    tabs: [{ id: "overview", label: "Overview", html: () => "" }, { id: "activity", label: "Activity", html: () => "" }] }`, c);
  const html = c.detailsHtml("thing", "k", "");
  assert.match(html, /<button type="button" class="btn sm pri"[^>]*data-plain="true"[\s\S]*Rebuild copy<\/button><button type="button" class="btn sm"[^>]*data-plain="true"[\s\S]*Take snapshot<\/button><details class="actionmenu">/);
  assert.match(html, /aria-label="More actions for thing"/);
  assert.match(html, /details-tabs[\s\S]*>Overview<[\s\S]*>Activity</);
});

test("tiles can leave out their icons, and an activity list is newest first, escaped", () => {
  const c = panel();
  assert.equal(c.UI.facts([["Copy 1", "Whole"]], { icons: false }), '<dl class="ui-facts plain"><div><dt>Copy 1</dt><dd>Whole</dd></div></dl>');
  assert.match(c.UI.facts([["Size", "5 GiB"]]), /<dl class="ui-facts"><div><dt><svg class="ui-fact-icon"/);
  const list = c.UI.activity([
    { at: "2026-11-20T10:00:00Z", title: "Snapshot taken", detail: "48 MiB" },
    { at: "2026-11-21T10:00:00Z", title: "<b>Backup failed</b>", tone: "bad" },
  ]);
  assert.match(list, /<ol class="ui-activity"><li class="bad">[\s\S]*&lt;b&gt;Backup failed[\s\S]*<li><i[\s\S]*Snapshot taken<\/b><span>48 MiB<\/span>/);
  assert.match(c.UI.activity([], { empty: "Nothing lately." }), /ui-empty">Nothing lately\./);
});

test("a log's last lines keep their order, cut Kubernetes' timestamp to the time of day, and escape the text", () => {
  const c = panel();
  const raw = ["2026-11-22T09:41:50.1Z start", "2026-11-22T09:41:58.123456789Z <worker> timeout", "2026-11-22T09:42:30Z redis: connection refused", ""].join("\n");
  const html = c.UI.logTail(raw, 2);
  assert.doesNotMatch(html, /start/, "only the last two lines");
  assert.match(html, /^<pre class="ui-log">\[\d\d:\d\d:58\] &lt;worker&gt; timeout\n\[\d\d:\d\d:30\] redis: connection refused<\/pre>$/);
  assert.equal(c.UI.logTail("plain line"), '<pre class="ui-log">plain line</pre>', "a line without a timestamp is kept as it is");
});

test("an event's reason is said in words", () => {
  const c = panel();
  assert.equal(c.eventWords("ProvisioningFailed"), "Provisioning failed");
  assert.equal(c.eventWords("Pulled"), "Pulled");
  assert.equal(c.eventWords(""), "Event");
});
