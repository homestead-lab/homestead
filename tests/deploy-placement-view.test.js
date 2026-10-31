"use strict";
// Deploy's Placement (#844): the section Edit has, from web/js/app-form.js,
// set live in the first rollout. Pod copies and the tier sit in it in both forms.
const test = require("node:test"), assert = require("node:assert/strict"), fs = require("node:fs"), vm = require("node:vm");
const between = require("./helpers/between");

function load(fields = {}, rows = {}) {
  const stub = value => ({ value, checked: false, options: [], classList: { add() {}, remove() {} }, dataset: {}, style: {} });
  const c = { window: {}, console, URLSearchParams, Map, Set, document: { addEventListener() {} },
    $: (sel, root) => root ? root[sel] : (sel in fields ? fields[sel] : stub("")),
    $$: sel => rows[sel] || [], esc: s => String(s ?? "").replaceAll("<", "&lt;"),
    STATE: { data: {} }, tip: t => t,
    selectedHardware: () => [], readPrivileges: () => ({}), readVolumeRows: () => [], containerLanRead: () => null,
    monitoringFieldValue: () => "", readEditedContainer: () => ({}) };
  vm.createContext(c);
  c.jsArg = s => JSON.stringify(String(s ?? "")); c.jsq = s => (c.esc || String)(c.jsArg(s));
  for (const f of ["web/js/ui.js", "web/js/app-form.js", "web/js/views-workloads.js", "web/js/views-lifecycle.js"]) vm.runInContext(fs.readFileSync(f, "utf8"), c);
  return c;
}
const NODES = [{ name: "node-1" }, { name: "node-2" }];
const APP = { failover: "move", priority: "normal", start_replicas: 1, node: "" };
const ROW = (target, mode) => ({ ".aff-target": { value: target }, ".aff-mode": { value: mode } });

test("the Placement section takes an id prefix: Deploy's is Edit's with d_ for e_", () => {
  const c = load();
  const p = { spread: "prefer", with: [{ ns: "lab", name: "broker", mode: "prefer" }], apart: [] };
  const edit = c.appFormPlacement("e", p, APP, NODES, [{ name: "web" }]);
  const deploy = c.appFormPlacement("d", p, APP, NODES, [{ name: "web" }]);
  for (const id of ["d_placement", "d_rep", "d_spread", "d_node", "d_with", "d_apart", "d_failover", "d_failover_help", "d_priority", "d_priority_help", "d_place_note", "d_pod_containers"])
    assert.match(deploy, new RegExp(`id="${id}"`), id);
  assert.doesNotMatch(deploy, /id="e_/);
  assert.match(deploy, /placementAdd\((&quot;|")d(&quot;|"),'with'\)/);
  assert.equal(deploy.replace(/\bd_/g, "e_").replaceAll('"d"', '"e"'), edit);
});

test("Edit draws its Placement through the shared section", () => {
  const lifecycle = fs.readFileSync("web/js/views-lifecycle.js", "utf8");
  assert.match(between(lifecycle, "window.wlEdit = async", "\n};\n"), /appFormPlacement\("e",/);
  assert.doesNotMatch(lifecycle, /function placementSection|function placementRow|function readPlacement/);
});

test("Deploy's Placement is live, with pod copies and the tier in it, not in Basics", () => {
  const workloads = fs.readFileSync("web/js/views-workloads.js", "utf8");
  const deploy = between(workloads, "async function viewDeploy", "\n}\n");
  assert.doesNotMatch(deploy, /later:/, "no section is greyed");
  assert.doesNotMatch(deploy, /Set after deploying/);
  assert.match(deploy, /appFormPlacement\("d",/);
  const basics = between(deploy, "basics: `", "hardware: `");
  assert.doesNotMatch(basics, /d_rep|d_priority|deployTierField/);
});

test("a new app starts as Deploy builds it today: moves on host failure, normal tier, one copy", () => {
  const c = load();
  assert.deepEqual(JSON.parse(JSON.stringify(c.deployPlacementApp({ replicas: 1 }))), { failover: "move", priority: "normal", start_replicas: 1, node: "" });
  const html = c.appFormPlacement("d", {}, c.deployPlacementApp({ replicas: 2, priority: "critical" }), NODES, [], { minCopies: 0 });
  assert.match(html, /<option value="move" selected>/);
  assert.match(html, /<option value="critical" selected>/);
  assert.match(html, /id="d_rep" value="2" min="0"/);
  for (const odd of ["group", "system", "bogus", 5]) {
    const shown = c.appFormPlacement("d", {}, c.deployPlacementApp({ priority: odd }), NODES, []);
    assert.match(shown, /<option value="normal" selected>/, String(odd));
    assert.doesNotMatch(shown, /<option value="group"/, String(odd));
  }
});

test("collect sends the host, spread, neighbours and failover chosen", () => {
  const c = load({ "#d_spread": { value: "require" }, "#d_node": { value: "node-1" }, "#d_failover": { value: "wait" },
    "#d_target_mode": { value: "new" }, "#d_rep": { value: "2" } },
  { "#d_with .aff-row": [ROW("lab/broker", "prefer")], "#d_apart .aff-row": [ROW("dns/dns-2", "require"), ROW("", "prefer")] });
  const sent = JSON.parse(JSON.stringify(vm.runInContext("collect()", c)));
  assert.deepEqual(sent.placement, { spread: "require", with: [{ ns: "lab", name: "broker", mode: "prefer" }], apart: [{ ns: "dns", name: "dns-2", mode: "require" }] });
  assert.equal(sent.node, "node-1");
  assert.equal(sent.failover, "wait");
  assert.equal(sent.replicas, 2);
});

test("joining an app sends no placement: it keeps the one it has", () => {
  const c = load({ "#d_spread": { value: "require" }, "#d_node": { value: "node-1" }, "#d_failover": { value: "wait" },
    "#d_target_mode": { value: "existing" } });
  const sent = vm.runInContext("collect()", c);
  for (const key of ["placement", "node", "failover"]) assert.equal(key in sent, false, key);
});

test("the placement notes read Deploy's own fields", () => {
  const note = { hidden: true, innerHTML: "" };
  const c = load({ "#d_place_note": note, "#d_spread": { value: "require" }, "#d_rep": { value: "3" } });
  c.appFormPlacementContext("d", { ns: "lab", others: [], nodes: 2, volumes: () => [], pvcs: () => [] });
  c.window.placementChanged("d");
  assert.equal(note.hidden, false);
  assert.match(note.innerHTML, /3 instances but 2 schedulable hosts/);
});

test("the demo's review echoes the placement chosen in Deploy", async () => {
  const ctx = { URL, URLSearchParams, Response, Date, console, location: { search: "?demo=1", origin: "https://demo.example.com" },
    HOMESTEAD_DEMO: false, document: { body: null, addEventListener() {} },
    fetch: async () => { throw new Error("demo must never reach a real cluster"); } };
  ctx.window = ctx;
  vm.createContext(ctx);
  vm.runInContext(fs.readFileSync("web/js/demo.js", "utf8"), ctx);
  const preview = async body => (await ctx.fetch("/api/preview", { method: "POST", body: JSON.stringify(body) })).json();
  const base = { workload_name: "web", container_name: "web", image: "nginx:alpine", namespace: "lab", target_mode: "new" };
  const nodes = (await (await ctx.fetch("/api/nodes")).json()).map(n => n.name);
  const chosen = await preview({ ...base, node: nodes[0], failover: "wait",
    placement: { spread: "prefer", with: [], apart: [{ ns: "lab", name: "frigate", mode: "require" }] } });
  const spec = chosen.deployment.spec.template.spec;
  assert.equal(spec.affinity.nodeAffinity.preferredDuringSchedulingIgnoredDuringExecution[0].preference.matchExpressions[0].values[0], nodes[0]);
  assert.ok(spec.affinity.podAntiAffinity.requiredDuringSchedulingIgnoredDuringExecution.length);
  assert.ok(spec.affinity.podAntiAffinity.preferredDuringSchedulingIgnoredDuringExecution.length);
  assert.equal(chosen.capacity.placement.preferred, nodes[0]);
  assert.equal(chosen.capacity.candidates.some(c => c.name === nodes[0]), true, "the chosen host is in the review");
  const plain = await preview(base);
  assert.equal(plain.deployment.spec.template.spec.affinity, undefined);
  assert.equal(plain.capacity.placement, undefined);
});
