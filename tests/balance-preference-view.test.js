"use strict";
// A host preference shows on the Containers row, and Balance's review says it leaves one.
const test = require("node:test"), assert = require("node:assert/strict"), fs = require("node:fs"), vm = require("node:vm");
const between = require("./helpers/between");

function load() {
  const c = { window: {}, console, URLSearchParams, Map, Set, document: { addEventListener() {} }, $: () => null, $$: () => [],
    esc: s => String(s ?? "").replaceAll("<", "&lt;"), STATE: { data: {} }, tip: t => t };
  vm.createContext(c);
  c.jsArg = s => JSON.stringify(String(s ?? "")); c.jsq = s => (c.esc || String)(c.jsArg(s));
  for (const f of ["web/js/ui.js", "web/js/autoupdate.js", "web/js/views-workloads.js"]) vm.runInContext(fs.readFileSync(f, "utf8"), c);
  return name => vm.runInContext(name, c);
}

test("the row says which host an app prefers", () => {
  const preferTag = load()("preferTag");
  assert.match(preferTag({ prefers: "host-a" }), /Prefers host-a/);
  assert.equal(preferTag({ prefers: "" }), "");
});

test("it goes on the row, not the card, whose title has no room beside it", () => {
  const tags = between(fs.readFileSync("web/js/views-workloads.js", "utf8"), "function workloadStateTags", "\n}\n");
  assert.match(tags, /row \? preferTag\(w\) : ""/);
});

test("the row's tip says who left it and what clearing it costs", () => {
  const preferTag = load()("preferTag");
  const balance = preferTag({ prefers: "host-a", prefers_balance: true });
  assert.match(balance, /Balance hosts/);
  assert.match(balance, /restarts/);
  assert.doesNotMatch(preferTag({ prefers: "host-a" }), /Balance hosts/);
});

test("both Balance reviews say a move leaves a preference that lasts", () => {
  const source = fs.readFileSync("web/js/views-workloads.js", "utf8");
  const words = load()("BALANCE_LEAVES_PREFERENCE");
  assert.match(words, /stays/);
  assert.match(words, /restarts/);
  assert.match(between(source, "function balanceContainersHtml", "function balanceVolumesHtml"), /BALANCE_LEAVES_PREFERENCE/);
  assert.match(between(source, "window.mvPlan =", "window.mvGo"), /BALANCE_LEAVES_PREFERENCE/);
});
