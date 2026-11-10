// The Timeline's filter bar as a shared helper (#919 PR 1): the same markup as before,
// a filter kept across a refresh of the same page, and a bar another page can drive.
"use strict";
const test = require("node:test"), assert = require("node:assert/strict");
const fs = require("node:fs"), vm = require("node:vm");
const between = require("./helpers/between");

const core = require("./helpers/core-source")();
const entry = (kind, extra = {}) => ({ at: 1, kind, tone: "info", text: "t", app: "", host: "", href: "", ...extra });
const DATA = { since: 0, now: 1, more: 0, unread: [], counts: { restart: 1, host: 1, change: 1 },
  entries: [entry("restart", { app: "apps/game" }), entry("host", { host: "node-2" }), entry("change", { app: "apps/wiki" })] };
// Captured from the page before the bar moved into the helper.
const PLAIN = "<div class=\"tl-filters\"><div class=\"seg\" role=\"group\" aria-label=\"Kind\"><button type=\"button\" class=\"on\" aria-pressed=\"true\" onclick=\"timelineFilter('kind', '')\">All</button><button type=\"button\" class=\"\" aria-pressed=\"false\" onclick=\"timelineFilter('kind', &quot;restart&quot;)\">Restarts 1</button><button type=\"button\" class=\"\" aria-pressed=\"false\" onclick=\"timelineFilter('kind', &quot;change&quot;)\">Changes 1</button><button type=\"button\" class=\"\" aria-pressed=\"false\" onclick=\"timelineFilter('kind', &quot;host&quot;)\">Hosts 1</button></div><label class=\"tl-pick\"><span class=\"dim xs\">App</span><select aria-label=\"App\" onchange=\"timelineFilter(&quot;app&quot;, this.value)\">\n      <option value=\"\">All apps</option><option value=\"apps/game\">apps/game</option><option value=\"apps/wiki\">apps/wiki</option></select></label><label class=\"tl-pick\"><span class=\"dim xs\">Host</span><select aria-label=\"Host\" onchange=\"timelineFilter(&quot;host&quot;, this.value)\">\n      <option value=\"\">All hosts</option><option value=\"node-2\">node-2</option></select></label></div>";
const CHOSEN = "<div class=\"tl-filters\"><div class=\"seg\" role=\"group\" aria-label=\"Kind\"><button type=\"button\" class=\"\" aria-pressed=\"false\" onclick=\"timelineFilter('kind', '')\">All</button><button type=\"button\" class=\"on\" aria-pressed=\"true\" onclick=\"timelineFilter('kind', &quot;restart&quot;)\">Restarts 1</button><button type=\"button\" class=\"\" aria-pressed=\"false\" onclick=\"timelineFilter('kind', &quot;change&quot;)\">Changes 1</button><button type=\"button\" class=\"\" aria-pressed=\"false\" onclick=\"timelineFilter('kind', &quot;host&quot;)\">Hosts 1</button></div><label class=\"tl-pick\"><span class=\"dim xs\">App</span><select aria-label=\"App\" onchange=\"timelineFilter(&quot;app&quot;, this.value)\">\n      <option value=\"\">All apps</option><option value=\"apps/game\" selected>apps/game</option><option value=\"apps/wiki\">apps/wiki</option></select></label><label class=\"tl-pick\"><span class=\"dim xs\">Host</span><select aria-label=\"Host\" onchange=\"timelineFilter(&quot;host&quot;, this.value)\">\n      <option value=\"\">All hosts</option><option value=\"node-2\">node-2</option></select></label></div>";

function setup() {
  const out = {};
  const ctx = { console, Date, Map, Set, Promise, JSON, Math, URLSearchParams, Number, String, Array, Object,
    document: { addEventListener() {}, hidden: false }, STATE: { data: {}, view: "timeline" }, ME: "a",
    esc: x => String(x ?? "").replaceAll("&", "&amp;").replaceAll("<", "&lt;").replaceAll('"', "&quot;"),
    paint: html => { out.html = html; }, toast() {}, $: () => null, $$: () => [], icon: () => "", api: async () => DATA };
  ctx.window = ctx;
  vm.createContext(ctx);
  require("./helpers/load-ui")(ctx);
  ctx.jsArg = s => JSON.stringify(String(s ?? "")); ctx.jsq = s => ctx.esc(ctx.jsArg(s));
  vm.runInContext(between(core, "const fmtUp", "const upChip"), ctx);
  vm.runInContext(fs.readFileSync("web/js/timeline.js", "utf8"), ctx);
  return { ctx, out, T: vm.runInContext("Timeline", ctx) };
}
const bar = html => html.match(/<div class="tl-filters">[\s\S]*?<\/div>(?=\n)/)[0];

test("the page's filter bar is the same markup as before, unfiltered and filtered", async () => {
  const { ctx, out, T } = setup();
  await T.view();
  assert.equal(bar(out.html), PLAIN);
  ctx.timelineFilter("kind", "restart"); ctx.timelineFilter("app", "apps/game");
  assert.equal(bar(out.html), CHOSEN);
});

test("filterBar draws the page's bar from the entries, counts and filter it is given", () => {
  const { T } = setup();
  const html = T.filterBar({ entries: DATA.entries, counts: DATA.counts, filter: { kind: "", app: "", host: "" } });
  assert.equal(html, PLAIN);
});

test("filterBar calls the function it is told to, so another page can drive it", () => {
  const { T } = setup();
  const html = T.filterBar({ entries: DATA.entries, counts: DATA.counts, filter: { kind: "", app: "", host: "" }, call: "weekFilter" });
  assert.match(html, /onclick="weekFilter\('kind', ''\)"/);
  assert.match(html, /onchange="weekFilter\(&quot;app&quot;, this\.value\)"/);
  assert.doesNotMatch(html, /timelineFilter/);
});

test("a filter is kept while the same navigation refreshes, and starts over on a new one", async () => {
  const { ctx, T } = setup();
  ctx.NAV_TOKEN = 1;
  await T.view();
  ctx.timelineFilter("host", "node-2");
  await T.view();
  assert.equal(T.state.filter.host, "node-2", "a refresh keeps it");
  ctx.NAV_TOKEN = 2;
  await T.view();
  assert.equal(T.state.filter.host, "", "opening the page again starts unfiltered");
});

test("a null counts shows the bar with every kind at nothing, not a throw", () => {
  const { ctx, T } = setup();
  const html = T.filterBar({ entries: DATA.entries, counts: null, filter: { kind: "", app: "", host: "" } });
  assert.match(html, /tl-filters/);
});
