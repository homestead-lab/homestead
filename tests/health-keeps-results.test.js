"use strict";
// Cluster Health keeps its last results while it checks again, and a tooltip
// whose element a refresh removed does not stay on screen (#834).
const test = require("node:test"), assert = require("node:assert/strict");
const fs = require("node:fs"), vm = require("node:vm");
const between = require("./helpers/between");

const esc = value => String(value ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const PLACEHOLDER = /Loading checks/;

function insights() {
  const el = { dataset: { insight: "full" }, cloneNode: () => ({ innerHTML: "" }) };
  global.window = {};
  global.STATE = { platform: {} };
  global.document = { querySelectorAll: sel => sel === "[data-insight]" ? [el] : [], querySelector: () => null };
  global.morph = () => {};
  global.UI = new Proxy({}, { get: () => () => "" });
  global.esc = esc; global.jsArg = JSON.stringify; global.jsq = JSON.stringify;
  global.fmtAge = String; global.fmtDay = String; global.toast = () => {};
  const answers = { "/api/overview": { health_issues: [] }, "/api/cluster": { control_plane: { etcd_total: 2, etcd_ready: 2 } },
    "/api/lh/overview": { volumes: [] }, "/api/nodes": [], "/api/self/health": { api: { ok: true } },
    "/api/settings": { thresholds: {} }, "/api/alerts": { active: [] } };
  global.api = async path => JSON.parse(JSON.stringify(answers[path] ?? {}));
  delete require.cache[require.resolve("../web/js/insights.js")];
  return require("../web/js/insights.js");
}

test("Cluster Health's body is the placeholder only until the first results", async () => {
  const H = insights();
  assert.match(H.fullBody(), PLACEHOLDER, "nothing read yet: the placeholder");
  await H.load(true);
  const body = H.fullBody();
  assert.doesNotMatch(body, PLACEHOLDER, "results in memory: those, not the placeholder");
  assert.equal(body, H.healthBody(), "the same markup the card shows, so a morph keeps its rows");
  assert.match(body, /0 critical · 1 medium/, "the two-member etcd finding");
  H.reset();
  assert.match(H.fullBody(), PLACEHOLDER, "after a reset (another account) the old results are gone");
});

test("new Health results drop a tooltip whose row they replaced", async () => {
  const H = insights();
  let dropped = 0;
  global.window.dropOrphanTooltip = () => { dropped++; };
  await H.load(true);
  assert.equal(dropped, 1);
  H.liveJobs([], true);
  assert.equal(dropped, 2, "so do the jobs widgets, rewritten as jobs change");
});

test("the Cluster page paints the Health results it already has", async () => {
  let painted = "";
  const ctx = { console, Map, Set, Promise, esc, STATE: { data: {}, platform: { harvester: true } },
    api: path => path === "/api/cluster" ? Promise.resolve({ control_plane: {}, nodes: [] }) : new Promise(() => {}),
    paint: html => { painted = html; }, $: () => null, tip: t => t, icon: () => "", fmtAgo: String, can: () => false,
    moreMenu: () => "", summaryLine: (_k, _p, body) => body, platformName: () => "",
    // Any shared part the page uses draws nothing here: only the Health body matters.
    UI: new Proxy({}, { get: () => () => "" }),
    HealthInsights: { fullBody: () => '<div class="insight-summary">0 critical</div>', load() {} } };
  ctx.window = ctx; vm.createContext(ctx);
  vm.runInContext(fs.readFileSync("web/js/views-cluster.js", "utf8"), ctx);
  // What the page starts after its paint (other cards' own reads) is not this test's.
  await ctx.viewCluster().catch(e => { if (!painted) throw e; });
  assert.match(painted, /<div data-insight="full"><div class="insight-summary">0 critical<\/div><\/div>/);
  assert.doesNotMatch(painted, PLACEHOLDER);
});

// The shared tooltip from core.js, with a document small enough to drive it.
function tooltips() {
  const listeners = {}, bubble = { hidden: true, style: {}, offsetWidth: 80, offsetHeight: 20, textContent: "" };
  class Element {
    constructor() { this.attrs = { title: "node1" }; this.dataset = {}; this.isConnected = true; }
    closest() { return this; } contains(n) { return n === this; } matches() { return false; }
    getAttribute(n) { return this.attrs[n] ?? null; } setAttribute(n, v) { this.attrs[n] = v; } removeAttribute(n) { delete this.attrs[n]; }
    getBoundingClientRect() { return { left: 100, top: 100, width: 50, height: 20, bottom: 120 }; }
  }
  const on = (type, fn) => { (listeners[type] ||= []).push(fn); };
  const ctx = { Element, innerWidth: 1440, innerHeight: 900, Math, $: sel => sel === "#uiTooltip" ? bubble : null,
    document: { addEventListener: on, body: { appendChild() {} } } };
  ctx.window = { addEventListener: on };
  vm.createContext(ctx);
  const core = fs.readFileSync("web/js/core.js", "utf8");
  vm.runInContext(between(core, "/* ---------------- shared, viewport-safe tooltips", "const ACTION_ICONS"), ctx);
  const fire = (type, e) => (listeners[type] || []).forEach(fn => fn(e));
  const owner = new Element();
  fire("pointerover", { target: owner, relatedTarget: null });
  assert.equal(bubble.hidden, false, "the tooltip shows on hover");
  return { ctx, core, bubble, owner, fire };
}

test("a pointer move hides a tooltip whose element is gone", () => {
  const { bubble, owner, fire } = tooltips();
  fire("pointermove", { target: owner });
  assert.equal(bubble.hidden, false, "still there: the tooltip stays");
  owner.isConnected = false;
  fire("pointermove", { target: {} });
  assert.equal(bubble.hidden, true);
});

test("a pointer over another element hides a tooltip whose element is gone", () => {
  const { bubble, owner, fire } = tooltips();
  owner.isConnected = false;
  fire("pointerover", { target: {}, relatedTarget: null });
  assert.equal(bubble.hidden, true);
});

test("a repaint or a new page hides a tooltip whose element is gone, with no pointer event", () => {
  for (const run of ["paint('<p>new</p>')", "resetPaint()"]) {
    const { ctx, core, bubble, owner } = tooltips();
    const host = { dataset: {}, classList: { remove() {} }, innerHTML: "" };
    Object.assign(ctx, { V: () => host, pageTabs: () => "", filterBar: () => "", keepSnapshot() {}, snapshotKey: String,
      STATE: { view: "cluster" }, enhanceActions() {}, loadAppIcons() {} });
    vm.runInContext(between(core, "function paint(html)", "/* ---------------- chart primitives"), ctx);
    host.dataset = {}; vm.runInContext(run, ctx);
    assert.equal(bubble.hidden, false, `${run}: the element is still on the page, so its tooltip stays`);
    owner.isConnected = false;
    host.dataset = {}; vm.runInContext(run, ctx);
    assert.equal(bubble.hidden, true, `${run}: hidden once its element is gone`);
  }
});
