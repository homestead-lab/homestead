"use strict";
// #1290: after a host restart, Hosts says when the cluster is ready for the next one.
const test = require("node:test"), assert = require("node:assert/strict"), fs = require("node:fs"), vm = require("node:vm");
function setup(answer) {
  let html = "";
  const c = { console, Set, Map, Promise, URLSearchParams, location: { search: "" }, document: { addEventListener() {} },
    STATE: { data: {} }, localStorage: { getItem: () => null, setItem() {} },
    // The rest of the page, beside the point here.
    loadHardwareFeatures: async () => [], viewLayout: () => "cards", paint: v => { html = v; }, metricsNotice: () => "",
    layoutSwitch: () => "", moreMenu: () => "", icon: () => "", nodeComparison: () => "",
    $: () => ({ set innerHTML(v) { html = v; }, get innerHTML() { return html; } }), $$: () => [],
    esc: v => String(v ?? "").replaceAll("&", "&amp;").replaceAll("<", "&lt;").replaceAll('"', "&quot;"), toast() {},
    api: async path => path === "/api/nodes/restart-ready" ? (answer instanceof Error ? Promise.reject(answer) : answer)
      : path === "/api/nodes" ? [] : path === "/api/nodes/uptime" ? { nodes: {} } : [] };
  c.window = c; vm.createContext(c);
  c.jsArg = s => JSON.stringify(String(s ?? "")); c.jsq = s => (c.esc || String)(c.jsArg(s));
  for (const f of ["web/js/ui.js", "web/js/views-overview.js"]) vm.runInContext(fs.readFileSync(f, "utf8"), c);
  return { c, html: () => html };
}
const settling = { node: "node-a", ready: false, not_running: ["lab/db", "lab/web"], short_of_copies: ["lab/data"] };

test("while the last restart settles, Hosts says what is still not running and which volumes are short", () => {
  const notice = setup().c.restartReadyNotice(settling);
  assert.match(notice, /Not ready for the next host restart/);
  assert.match(notice, /2 apps moved by the restart of node-a are not running yet: lab\/db, lab\/web/);
  assert.match(notice, /1 volume is short of copies: lab\/data/);
  assert.match(notice, /ui-callout warn/);
});

test("once it has settled, Hosts says it is ready", () => {
  const notice = setup().c.restartReadyNotice({ node: "node-a", ready: true, not_running: [], short_of_copies: [] });
  assert.match(notice, /Ready for the next host restart/);
  assert.doesNotMatch(notice, /Not ready/);
});

test("with no recent restart, or an answer that could not be read, nothing is shown", () => {
  const t = setup();
  assert.equal("", t.c.restartReadyNotice({ node: null }));
  assert.equal("", t.c.restartReadyNotice(null));
  assert.equal("", t.c.restartReadyNotice({ unread: "HTTP 500" }));
});

test("names are escaped", () => {
  const notice = setup().c.restartReadyNotice({ ...settling, not_running: ["lab/<b>"] });
  assert.doesNotMatch(notice, /<b>/);
});

test("the Hosts page reads it and shows it under the header", async () => {
  const t = setup(settling);
  await t.c.viewNodes();
  assert.match(t.html(), /Not ready for the next host restart/);
  const failed = setup(new Error("unreadable"));
  await failed.c.viewNodes();
  assert.doesNotMatch(failed.html(), /next host restart/, "a failed read leaves the page as it was");
});
