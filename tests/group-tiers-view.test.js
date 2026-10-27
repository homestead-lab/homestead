"use strict";
// #430: group tiers in Containers, Edit and Priority tiers.
const test = require("node:test"), assert = require("node:assert/strict"), fs = require("node:fs"), vm = require("node:vm");

function load() {
  const c = { window: {}, console, URLSearchParams, Map, Set, document: { addEventListener() {} },
    $: () => null, esc: s => String(s ?? "").replaceAll("<", "&lt;"), STATE: { data: {} }, tip: t => t };
  vm.createContext(c);
  c.jsArg = s => JSON.stringify(String(s ?? "")); c.jsq = s => (c.esc || String)(c.jsArg(s));
  for (const f of ["web/js/ui.js", "web/js/app-form.js", "web/js/views-workloads.js", "web/js/views-lifecycle.js"]) vm.runInContext(fs.readFileSync(f, "utf8"), c);
  return c;
}

test("renaming a group sends its old name, so its tier moves with it", async () => {
  const c = load(), sent = [];
  c.api = async (path, init) => { sent.push([path, JSON.parse(init.body)]); return { detail: "1 workload moved to House", group: "House" }; };
  c.toast = () => {}; c.manageWorkloadGroups = () => {}; c.renderWorkloads = () => {}; c.closeModal = () => {};
  c.saveWorkloadGroup = c.window.saveWorkloadGroup;
  c.$ = sel => sel === "#wg_rename" ? { value: "House" } : null;
  c.window.__wgGroup = { items: [{ ns: "lab", name: "frigate" }], from: "Home" };
  c.window.wgRenameApply();
  await new Promise(resolve => setImmediate(resolve));
  assert.deepEqual(sent, [["/api/workloads/group", { items: [{ ns: "lab", name: "frigate" }], group: "House", renamed_from: "Home" }]]);
});

const plan = (apps, groups = []) => ({ apps, groups, changes: 0, max: 50, skipped: [], warnings: [] });
const row = (id, extra) => ({ id, kind: "app", ns: "lab", name: id.split("/")[1], current: "normal", tier: "normal", suggested: "", why: "",
  managed: "", group: "", follows: false, wanted: "normal", source: "own", status: "in_line", ...extra });

test("a row that follows its group says where its tier comes from, and that it needs a restart", () => {
  const html = load().priorityHtml(plan([row("lab/ha", { group: "Home", follows: true, wanted: "critical", source: "group", status: "restart", tier: "critical" })]));
  assert.match(html, /critical, from Home · runs normal/);
  assert.match(html, /needs a restart/);
});

test("a tier of its own, and the default", () => {
  const html = load().priorityHtml(plan([row("lab/db", { current: "low", tier: "low", wanted: "low" }), row("lab/web", { follows: true, source: "default" })]));
  assert.match(html, /low, its own · in line/);
  assert.match(html, /normal, the default · in line/);
});

test("an app Flux applies shows the line to commit", () => {
  const html = load().priorityHtml(plan([row("lab/web", { managed: "Flux", group: "Home", wanted: "critical", source: "group", status: "git",
    line: "spec.template.spec.priorityClassName: x-critical" })]));
  assert.match(html, /change in git/);
  assert.match(html, /spec\.template\.spec\.priorityClassName: x-critical/);
});

test("a class changed outside Homestead, and a group tier that can't be read, are said and not ticked", () => {
  const html = load().priorityHtml(plan([
    row("lab/ha", { group: "Home", follows: true, current: "low", tier: "low", wanted: "critical", source: "group", status: "outside" }),
    row("lab/fr", { group: "Home", follows: true, wanted: null, source: "group", status: "unknown_group" })]));
  assert.match(html, /changed outside Homestead/);
  assert.match(html, /group tier unknown/);
  assert.doesNotMatch(html, /was low|was normal/);
});

test("the groups are listed with their tiers", () => {
  const html = load().priorityHtml(plan([], [{ name: "Home", tier: "critical", apps: 4 }, { name: "Media", tier: "", apps: 1 }, { name: "Lab", tier: null, apps: 2 }]));
  assert.match(html, /<b>Home<\/b> critical · 4 apps/);
  assert.match(html, /<b>Media<\/b> no tier · 1 app</);
  assert.match(html, /<b>Lab<\/b> tier unknown · 2 apps/);
});

test("a row whose group tier is unknown has its select disabled, says why, and is not ticked", () => {
  const html = load().priorityHtml(plan([row("lab/fr", { group: "Home", follows: true, wanted: null, source: "group", status: "unknown_group" })]));
  assert.match(html, /<select[^>]*disabled/);
  assert.match(html, /Group tiers couldn&#39;t be read|Group tiers couldn't be read/);
  assert.doesNotMatch(html, /was normal/);
});

const field = (c, w) => c.appFormPlacement("e", { apart: [], together: [] }, { failover: "default", ...w }, [], []);

test("Edit offers its group's tier first, for an app in a group", () => {
  const html = field(load(), { priority: "group", priority_group: "Home", priority_group_tier: "critical" });
  assert.match(html, /<option value="group" selected>From its group: critical, from Home<\/option>/);
  assert.match(html, /takes Home's tier/);
});

test("a group with no tier is offered as normal", () => {
  const html = field(load(), { priority: "low", priority_group: "Home", priority_group_tier: "" });
  assert.match(html, /value="group"\s*>From its group: normal, Home has no tier/);
  assert.match(html, /<option value="low" selected>/);
});

test("an app in no group has no group option", () => {
  assert.doesNotMatch(field(load(), { priority: "normal", priority_group: "" }), /value="group"/);
});

test("an app Flux applies stays read-only, in a group too", () => {
  const html = field(load(), { priority: "normal", priority_class: "x-normal", priority_owner: "Flux", priority_group: "Home", priority_group_tier: "critical" });
  assert.doesNotMatch(html, /id="e_priority"/);
});

test("a group's heading offers Tier… and shows its tier", () => {
  const html = load().workloadGroupHead("Home", [{ name: "ha", group: "Home", group_tier: "critical", desired: 1, ready: 1 }], false);
  assert.match(html, />Tier…</);
  assert.match(html, /ui-chip[^>]*>critical</);
});

test("Homestead's own group and Ungrouped offer no tier", () => {
  const c = load();
  assert.doesNotMatch(c.workloadGroupHead("Homestead", [{ name: "homestead", group: "Homestead", homestead: "self", platform: "Homestead", desired: 1, ready: 1 }], false), /Tier…/);
  assert.doesNotMatch(c.workloadGroupHead("", [{ name: "web", group: "", desired: 1, ready: 1 }], false), /Tier…/);
});

test("the Tier… dialog says what a tier would do before it is saved", () => {
  const c = load(), p = plan([row("lab/ha", { group: "Home", follows: true }), row("lab/fr", { group: "Home", follows: true, current: "critical", tier: "critical" }),
    row("lab/db", { group: "Home" }), row("lab/web", { group: "Home", managed: "Flux" })]);
  assert.equal(c.groupTierLine(p, "Home", "critical"),
    "2 apps follow Home; 1 would need a restart. Nothing restarts now. Start it from Priority tiers… 1 is applied by Flux or Argo CD: change it in git.");
  assert.equal(c.groupTierLine(p, "Home", "none"), "2 apps follow Home; 1 would need a restart. Nothing restarts now. Start it from Priority tiers…");
  assert.equal(c.groupTierLine(plan([row("lab/db", { group: "Home" })]), "Home", "low"), "No app follows Home: each has a tier of its own.");
  assert.equal(c.groupTierLine(null, "Home", "low"), "Homestead couldn't read the apps just now; the tier can still be saved.");
});

test("Set tiers sends back whether each app keeps following its group, as reviewed", async () => {
  const c = load(), sent = [];
  c.api = async (path, init) => { sent.push([path, JSON.parse(init.body)]); return {}; };
  c.toast = () => {}; c.closeModal = () => {};
  c.$ = sel => sel === "#prioRestart" ? { checked: true } : null;
  vm.runInContext("PRIORITY", c).plan = { review_token: "t", apps: [row("lab/ha", { follows: true, follow: true, tier: "critical" }), row("lab/db", { follow: false, tier: "low" })] };
  await c.window.priorityStart();
  assert.deepEqual(sent[0][1].changes, [{ id: "lab/ha", current: "normal", tier: "critical", follow: true }, { id: "lab/db", current: "normal", tier: "low", follow: false }]);
});

function tierDialog(c, groupTier) {
  const els = { "#gt_tier": { value: "" }, "#gtGo": { disabled: false }, "#gt_effect": {} };
  let html = "";
  c.modal = (_, body) => { html = body; };
  c.api = async () => { throw new Error("no plan"); };
  c.$ = sel => els[sel] || null; c.groupTierEffect = c.window.groupTierEffect;
  vm.runInContext('WL_GROUP_KEYS = ["Home"]', c);
  c.STATE.data.wl = [{ name: "ha", group: "Home", group_tier: groupTier }];
  return { els, html: () => html };
}

test("the Tier… dialog preselects nothing, and waits for a choice, when the group's tier can't be read", async () => {
  const c = load(), d = tierDialog(c, null);
  await c.window.groupTier(0);
  assert.doesNotMatch(d.html(), /<option value="(critical|normal|low|none)" selected/);
  assert.match(d.html(), /<button[^>]*id="gtGo"[^>]*disabled|<button[^>]*disabled[^>]*id="gtGo"/);
  assert.match(d.html(), /couldn(&#39;|')t read Home(&#39;|')s tier/);
  assert.equal(d.els["#gtGo"].disabled, true, "Save stays off until a tier is chosen");
  const sent = []; c.api = async (path, init) => { sent.push(init.body); return {}; };
  await c.window.groupTierSave();
  assert.deepEqual(sent, [], "nothing is saved without a choice");
  d.els["#gt_tier"].value = "low";
  c.window.groupTierEffect();
  assert.equal(d.els["#gtGo"].disabled, false);
});

test("the Tier… dialog preselects the group's tier, or None, when it is known", async () => {
  for (const [known, picked] of [["critical", "critical"], ["", "none"]]) {
    const c = load(), d = tierDialog(c, known);
    await c.window.groupTier(0);
    assert.match(d.html(), new RegExp(`<option value="${picked}" selected`));
    assert.doesNotMatch(d.html(), /couldn(&#39;|')t read/);
  }
});
