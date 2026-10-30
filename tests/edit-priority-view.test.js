"use strict";
// Edit's tier field (#429): read-only for Kubernetes' own class, a class that
// isn't on the cluster, and apps Flux or Argo CD apply.
const test = require("node:test"), assert = require("node:assert/strict"), fs = require("node:fs"), vm = require("node:vm");

function load() {
  const c = { window: {}, console, URLSearchParams, Map, Set, document: { addEventListener() {} },
    $: () => null, esc: s => String(s ?? "").replaceAll("<", "&lt;"), STATE: { data: {} }, tip: t => t };
  vm.createContext(c);
  c.jsArg = s => JSON.stringify(String(s ?? "")); c.jsq = s => (c.esc || String)(c.jsArg(s));
  for (const f of ["web/js/ui.js", "web/js/app-form.js", "web/js/views-workloads.js", "web/js/views-lifecycle.js"]) vm.runInContext(fs.readFileSync(f, "utf8"), c);
  return c;
}
const field = (c, w) => c.appFormPlacement("e", { apart: [], together: [] }, { failover: "default", ...w }, [], []);

test("a tier Homestead may set is a choice", () => {
  assert.match(field(load(), { priority: "critical" }), /id="e_priority"/);
});

test("Kubernetes' own class is shown, not offered", () => {
  const html = field(load(), { priority: "system", priority_class: "system-node-critical" });
  assert.doesNotMatch(html, /id="e_priority"/);
  assert.match(html, /system-node-critical/);
  assert.match(html, /Kubernetes' own class/);
});

test("a class that isn't on the cluster says its pods are refused", () => {
  const html = field(load(), { priority: "missing", priority_class: "gone" });
  assert.doesNotMatch(html, /id="e_priority"/);
  assert.match(html, /isn't on the cluster/);
});

test("an app Flux applies shows where to set its tier", () => {
  const html = field(load(), { priority: "normal", priority_class: "x-normal", priority_owner: "Flux" });
  assert.doesNotMatch(html, /id="e_priority"/);
  assert.match(html, /where Flux applies it/);
});

test("Priority tiers shows what is wrong in one notice", () => {
  const c = load();
  const html = c.priorityHtml({ apps: [], changes: 0, max: 50, skipped: [],
    warnings: ["lab/ha name gone, which isn't a class on the cluster: their new pods are refused",
               "homestead-critical (1000) is no longer the class for its tier; lab/db still use it"] });
  assert.equal((html.match(/class="ui-callout /g) || []).length, 1);
  assert.match(html, /Check first[^]*lab\/ha name gone[^]*homestead-critical \(1000\)/);
});

test("a class Homestead couldn't check is left as it is", () => {
  const html = field(load(), { priority: "unknown", priority_class: "x-normal" });
  assert.doesNotMatch(html, /id="e_priority"/);
  assert.match(html, /couldn't read the cluster's classes/);
});
