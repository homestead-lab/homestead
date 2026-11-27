"use strict";
// Hosts after the Nov 2026 mockup (views-hosts.js): the summary line says
// which hosts can restart without stopping an app, and each card ends with
// that answer and its two main actions.
const test = require("node:test"), assert = require("node:assert/strict"), fs = require("node:fs"), vm = require("node:vm");

function page() {
  const esc = v => String(v ?? "").replace(/[&<>"']/g, ch => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[ch]));
  const c = { console, Set, Map, Promise, URLSearchParams, location: { search: "" }, document: { addEventListener() {} }, STATE: { data: {}, view: "nodes" },
    $: () => null, $$: () => [], toast() {}, icon: () => "", esc, HOVER_CARDS: {} };
  c.window = c; vm.createContext(c);
  c.jsArg = s => JSON.stringify(String(s ?? "")); c.jsq = s => c.esc(c.jsArg(s));
  for (const f of ["web/js/ui.js", "web/js/views-overview.js", "web/js/views-hosts.js"]) vm.runInContext(fs.readFileSync(f, "utf8"), c);
  for (const name of ["HOST_IMPACT", "hostVerdictHtml"]) c[name] = vm.runInContext(name, c);
  return c;
}
const moves = (...names) => names.map(name => ({ name, outcome: "moves", to: ["node-b", "node-c"] }));

test("a host whose apps all move is safe to restart, and says where they go", () => {
  const c = page();
  c.HOST_IMPACT.set("node-a", { apps: moves("web", "db", "cache"), unread: [] });
  assert.deepEqual({ ...c.hostRestartVerdict("node-a") }, { tone: "ok", title: "Safe to restart", line: "Its 3 apps move to the other two hosts." });
  c.HOST_IMPACT.set("node-c", { apps: [{ name: "paperless", outcome: "moves", to: ["node-a"] }] });
  assert.equal(c.hostRestartVerdict("node-c").line, "paperless moves to node-a.");
  assert.equal(c.hostRestartVerdict("node-z"), null, "not read yet");
  assert.match(c.hostVerdictHtml("node-z"), /Checking whether a restart is safe/);
});

test("a host whose restart stops an app says which and why, in words", () => {
  const c = page();
  c.HOST_IMPACT.set("node-b", { apps: [{ name: "frigate", outcome: "stops", why: "no other host can run it: lacks the Google Coral USB" }, ...moves("web")] });
  assert.deepEqual({ ...c.hostRestartVerdict("node-b") }, { tone: "warn", title: "A restart stops frigate", line: "No other host has a Google Coral USB." });
  c.HOST_IMPACT.set("node-x", { unread: "timed out" });
  assert.equal(c.hostRestartVerdict("node-x").title, "Not known whether a restart is safe");
});

test("the summary says how many hosts are ready, then which can restart without stopping an app", () => {
  const c = page();
  const nodes = [{ name: "node-a", status: "Ready" }, { name: "node-b", status: "Ready" }, { name: "node-c", status: "Ready" }];
  assert.match(c.hostsSummary(nodes), /^<span class="sum-wide"><b>All 3 hosts are ready\.<\/b><\/span>/, "nothing about restarts until every host is read");
  c.HOST_IMPACT.set("node-a", { apps: moves("web") });
  c.HOST_IMPACT.set("node-b", { apps: [{ name: "frigate", outcome: "stops", why: "x" }] });
  c.HOST_IMPACT.set("node-c", { apps: [] });
  assert.match(c.hostsSummary(nodes), /<b>All 3 hosts are ready\.<\/b> <span class="sum-note">node-a and node-c can restart without stopping an app\.<\/span>/);
  assert.match(c.hostsSummary([...nodes, { name: "node-d", status: "NotReady" }]), /<b>3 of 4 hosts are ready\.<\/b> <button[^>]*nodeDetail\(&quot;node-d&quot;\)[^>]*>node-d is not ready<\/button>/);
});
