"use strict";
// Priority tiers for StatefulSets: the row, and the lead text's counts.
const test = require("node:test"), assert = require("node:assert/strict"), fs = require("node:fs"), vm = require("node:vm");

function load() {
  const c = { window: {}, console, URLSearchParams, Map, Set, document: { addEventListener() {} },
    $: () => null, esc: s => String(s ?? "").replaceAll("&", "&amp;").replaceAll("<", "&lt;").replaceAll(">", "&gt;").replaceAll('"', "&quot;"),
    STATE: { data: {} }, tip: t => t };
  vm.createContext(c);
  c.jsArg = s => JSON.stringify(String(s ?? "")); c.jsq = s => (c.esc || String)(c.jsArg(s));
  for (const f of ["web/js/ui.js", ...require("./helpers/workloads-source").paths(), ...require("./helpers/lifecycle-source").files()]) vm.runInContext(fs.readFileSync(f, "utf8"), c);
  return c;
}

const plan = apps => ({ apps, groups: [], changes: apps.filter(a => a.tier !== a.current).length, max: 50, skipped: [], warnings: [] });
const row = (id, extra) => {
  const [kind, rest] = id.startsWith("sts:") ? ["sts", id.slice(4)] : id.startsWith("vm:") ? ["vm", id.slice(3)] : ["app", id];
  const [ns, name] = rest.split("/");
  return { id, kind, ns, name, current: "normal", tier: "normal", suggested: "", why: "", managed: "", group: "", follows: false,
    wanted: "normal", source: "own", status: "in_line", restarts: kind !== "vm", ...extra };
};

test("a StatefulSet's row shows ns/name and a StatefulSet chip", () => {
  const html = load().priorityHtml(plan([row("sts:lab/postgres")]));
  assert.match(html, /<span class="mono">lab\/postgres<\/span> <span[^>]*>StatefulSet<\/span>/);
  assert.doesNotMatch(html, /<span class="mono">sts:/);
  assert.match(html, /Apps, StatefulSets and VMs · 1/);
});

test("the lead counts restarting StatefulSets apart from apps, and held ones with VMs", () => {
  const html = load().priorityHtml(plan([
    row("lab/web", { tier: "low" }), row("lab/api", { tier: "low" }),
    row("sts:lab/postgres", { tier: "critical" }),
    row("sts:lab/x-db", { tier: "critical", restarts: false }),
    row("vm:lab/win", { tier: "low" })]));
  assert.match(html, /Changes the tier of 2 apps and 1 StatefulSet\. Each restarts once, one at a time; a StatefulSet one pod at a time\./);
  assert.match(html, /1 VM and 1 StatefulSet take their new tier when they next start; none restarts now\./);
  assert.match(html, /Restart these 3 now/);
});

test("a StatefulSet that doesn't restart itself is counted alone with the VMs", () => {
  const html = load().priorityHtml(plan([row("sts:lab/x-db", { tier: "critical", restarts: false })]));
  assert.match(html, /1 StatefulSet takes its new tier when it next starts; none restarts now\./);
  assert.doesNotMatch(html, /Changes the tier of/);
  assert.match(html, /Set the tiers/);
});

test("apps alone read as before", () => {
  const html = load().priorityHtml(plan([row("lab/web", { tier: "low" }), row("vm:lab/win", { tier: "low" })]));
  assert.match(html, /Changes the tier of 1 app\. Each restarts once, one at a time\. 1 VM takes its new tier when it next starts/);
});

test("a StatefulSet's name is escaped", () => {
  const html = load().priorityHtml(plan([row("sts:lab/<img src=x>")]));
  assert.doesNotMatch(html, /<img src=x>/);
  assert.match(html, /lab\/&lt;img src=x&gt;/);
});

test("the toast names StatefulSets apart from apps", async () => {
  const c = load(), toasts = [];
  c.api = async () => ({ operation: { id: "op1" } });
  c.toast = (m, tone) => toasts.push(m); c.closeModal = () => {};
  c.$ = sel => sel === "#prioRestart" ? { checked: true } : null;
  vm.runInContext("PRIORITY", c).plan = plan([row("lab/web", { tier: "low" }), row("lab/api", { tier: "low" }), row("sts:lab/postgres", { tier: "critical" })]);
  await c.window.priorityStart();
  assert.deepEqual(toasts, ["Setting the tier of 2 apps and 1 StatefulSet; follow it in Jobs"]);
});

test("What the tiers do says which partitions wait", () => {
  const html = load().priorityHtml(plan([row("sts:lab/postgres")]));
  assert.match(html, /OnDelete, or a partition at or above its replicas/);
});
