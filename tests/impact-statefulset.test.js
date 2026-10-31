"use strict";
// If this host goes down (#440): a StatefulSet's row - its chip, its copies
// here, the link to Longhorn's setting - and rows that couldn't be checked.
const test = require("node:test"), assert = require("node:assert/strict"), fs = require("node:fs"), vm = require("node:vm");

function setup(payload = {}, state = {}) {
  const fields = { "#mbody": { innerHTML: "" } }, calls = [];
  const ctx = { console, Map, Set, Promise, encodeURIComponent, document: { addEventListener() {} }, STATE: { data: {}, ...state },
    $: key => fields[key], $$: () => [], can: () => true, tip: t => t, toast: () => {}, jsArg: v => JSON.stringify(String(v)),
    esc: value => String(value ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c])),
    appAvatar: () => '<span class="av"></span>', modal: () => {},
    applyRole: () => calls.push(["applyRole", fields["#mbody"].innerHTML]),
    closeModal: () => calls.push(["closeModal"]), settingsTab: tab => calls.push(["settingsTab", tab]), go: view => calls.push(["go", view]),
    api: async url => (String(url).startsWith("/api/nodes/impact") ? payload : { hosts: [] }) };
  ctx.window = ctx; vm.createContext(ctx); require("./helpers/load-ui")(ctx);
  vm.runInContext(fs.readFileSync("web/js/impact.js", "utf8"), ctx);
  return { ctx, fields, calls, impactApp: vm.runInContext("impactApp", ctx) };
}

const WAITS = "waits until its pod is deleted: Kubernetes doesn't start a StatefulSet's pod elsewhere while the old one might still run. "
  + "Longhorn can do this for you: Settings → Hardware and storage → Longhorn storage → Pods on a failed node.";
const waits = { kind: "set", ns: "data", name: "postgres", icon: "", here: 1, copies: 3, keeps_answering: true,
  outcome: "waits", setting: "node_down", why: WAITS, notes: [] };

test("a StatefulSet's row has its chip, its copies here and a link to Longhorn's setting", () => {
  const html = setup().impactApp(waits);
  assert.match(html, /<span class="ui-chip neutral">StatefulSet<\/span>/);
  assert.match(html, /data · 1 of 3 copies here · keeps answering from its other copies/);
  assert.match(html, /<button class="linkish xs" onclick="ifDownSetting\(\)">Open the setting<\/button>/);
  assert.match(html, /ui-chip warn slim">waits</);
});

test("a row Longhorn's setting can help says it in one line, with the why behind a disclosure", () => {
  const html = setup().impactApp(waits);
  const visible = html.replace(/<details[^]*?<\/details>/g, "");
  assert.match(visible, /<span class="small">waits until its pod is deleted\. <button/);
  assert.doesNotMatch(visible, /Kubernetes doesn|Longhorn can do this/);
  assert.match(html, /<details class="ui-more"[^>]*>\s*<summary>Why it waits<\/summary>[^]*Kubernetes doesn&#39;t start[^]*Pods on a failed node\./);
});

test("one copy reads copy, and a row the setting can't help has no link", () => {
  const html = setup().impactApp({ ...waits, copies: 1, keeps_answering: false, setting: undefined, outcome: "stops",
    why: "its volume data-postgres-0 is stored on node2" });
  assert.match(html, /data · 1 of 1 copy here</);
  assert.doesNotMatch(html, /ifDownSetting/);
});

test("names and reasons are escaped", () => {
  const html = setup().impactApp({ ...waits, name: "<img src=x onerror=alert(1)>", ns: 'x-db"', why: "<b>x</b>",
    notes: ["<i>n</i>"] });
  assert.doesNotMatch(html, /<img src=x|<b>x<\/b>|<i>n<\/i>/);
  assert.match(html, /&lt;img src=x onerror=alert\(1\)&gt;/);
  assert.match(html, /x-db&quot;/);
});

test("the link opens Settings where Longhorn's setting is", () => {
  const t = setup();
  t.ctx.ifDownSetting();
  assert.deepEqual(t.calls, [["closeModal"], ["settingsTab", "cluster"], ["go", "settings"]]);
});

test("rows that couldn't be checked have their own chip and section; unread StatefulSets are said", async () => {
  const t = setup({ node: "node2", others_ready: ["node1"], at_risk: [], fewer_copies: [], addresses: [], unread: ["StatefulSets"],
    counts: { stops: 0, waits: 0, unknown: 1, moves: 0, at_risk: 0, fewer_copies: 0, addresses: 0 },
    apps: [{ ...waits, outcome: "unknown", setting: undefined, why: "couldn't be checked: where its volumes are stored" }] });
  await t.ctx.nodeIfDown("node2");
  const html = t.fields["#mbody"].innerHTML;
  assert.match(html, /<span class="ui-chip neutral">1 unknown<\/span>/);
  // Titles and reasons go through esc, which turns ' into &#39;.
  assert.match(html, /Couldn(?:'|&#39;)t be checked<\/h4>[^]*couldn&#39;t be checked: where its volumes are stored/);
  assert.match(html, /ui-chip neutral slim">unknown</);
  assert.match(html, /Couldn't read StatefulSets, so none of them is listed\./);
});

test("copies Longhorn couldn't read make the volume risk unknown, never nothing at risk (#1002)", async () => {
  const t = setup({ node: "node2", others_ready: ["node1"], at_risk: [], fewer_copies: [], addresses: [],
    unread: ["Longhorn's volume copies"], apps: [],
    counts: { stops: 0, waits: 0, unknown: 0, moves: 0, at_risk: null, fewer_copies: null, addresses: 0 } });
  await t.ctx.nodeIfDown("node2");
  const html = t.fields["#mbody"].innerHTML;
  assert.match(html, /<span class="ui-chip neutral">volume risk unknown<\/span>/);
  assert.doesNotMatch(html, /no volume keeps a copy here/);
  assert.match(html, /Couldn(?:'|&#39;)t read Longhorn(?:'|&#39;)s volume copies/);
});

test("one heading fits apps waiting for their host and StatefulSets waiting for their pod to be deleted", async () => {
  const t = setup({ node: "node2", others_ready: ["node1"], at_risk: [], fewer_copies: [], addresses: [], unread: [],
    counts: { stops: 0, waits: 2, unknown: 0, moves: 0, at_risk: 0, fewer_copies: 0, addresses: 0 },
    apps: [waits, { kind: "app", ns: "lab", name: "zigbee", icon: "", outcome: "waits", why: "set to wait for its host to come back", notes: [] }] });
  await t.ctx.nodeIfDown("node2");
  const html = t.fields["#mbody"].innerHTML;
  assert.doesNotMatch(html, /Waits for it/);
  assert.match(html, />Waits<\/h4>[^]*postgres[^]*zigbee/);
});

// Start on another host (#441): only for a set that waits, on a host that is down.
const chosen = { ...waits, setting: undefined, why: "set to wait for its host to come back" };
const down = (status, extra = {}) => ({ data: { nodes: [{ name: "node2", status }] }, ...extra });

test("a waiting set on a NotReady host offers Start on another host to admins", () => {
  const t = setup({}, down("NotReady"));
  for (const row of [waits, chosen]) {
    const html = t.impactApp(row, "node2");
    assert.match(html, /data-need="admin"/);
    assert.match(html, /setRestartReview\(&quot;data&quot;,&quot;postgres&quot;,&quot;node2&quot;\)/);
    assert.match(html, /Start on another host…/);
  }
});

test("not offered while the host is Ready, or for a row that does not wait", () => {
  assert.doesNotMatch(setup({}, down("Ready")).impactApp(waits, "node2"), /setRestartReview/);
  assert.doesNotMatch(setup({}, down("NotReady")).impactApp({ ...waits, outcome: "stops" }, "node2"), /setRestartReview/);
  assert.doesNotMatch(setup({}, down("NotReady")).impactApp({ ...waits, kind: "app" }, "node2"), /setRestartReview/);
});

test("not offered on Harvester, which handles it itself", () => {
  const html = setup({}, down("NotReady", { platform: { harvester: true } })).impactApp(waits, "node2");
  assert.doesNotMatch(html, /setRestartReview/);
  assert.match(html, /Harvester restarts these itself; see its VM force reset policy\./);
});

test("the impact dialog applies the role after drawing the button, so viewers don't see it", async () => {
  const t = setup({ node: "node2", others_ready: ["node1"], at_risk: [], fewer_copies: [], addresses: [], unread: [],
    counts: { stops: 0, waits: 1, unknown: 0, moves: 0, at_risk: 0, fewer_copies: 0, addresses: 0 }, apps: [waits] }, down("NotReady"));
  await t.ctx.nodeIfDown("node2");
  const applied = t.calls.filter(c => c[0] === "applyRole");
  assert.equal(applied.length, 1);
  assert.match(applied[0][1], /data-need="admin"[^>]*>Start on another host…/);
});
