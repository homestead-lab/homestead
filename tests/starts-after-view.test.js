"use strict";
// Starts after (#746) in the web UI: Edit → Placement's block, what Edit
// sends, the review line, and what the Apps list and Start say while an app
// waits for another.
const test = require("node:test"), assert = require("node:assert/strict"), fs = require("node:fs"), vm = require("node:vm");

function load(extra = {}) {
  const c = { window: {}, console, URLSearchParams, Map, Set, Date, document: { addEventListener() {} },
    $: () => null, $$: () => [], esc: s => String(s ?? "").replaceAll("&", "&amp;").replaceAll("<", "&lt;").replaceAll('"', "&quot;"),
    STATE: { data: {} }, tip: t => `<span class="tip">${t}</span>`, icon: () => "",
    // core.js's fmtClock takes seconds or ms; local time, here UTC for a fixed answer.
    fmtClock: v => new Date(v < 1e12 ? v * 1000 : v).toISOString().slice(11, 16), ...extra };
  vm.createContext(c);
  c.jsArg = s => JSON.stringify(String(s ?? "")); c.jsq = s => c.esc(c.jsArg(s));
  for (const f of ["web/js/ui.js", "web/js/views-workloads.js", "web/js/views-lifecycle.js", "web/js/views-storage.js"])
    vm.runInContext(fs.readFileSync(f, "utf8"), c);
  return c;
}

const candidates = [
  { ns: "lab", app: "postgres", services: [{ name: "postgres", ports: [5432] }] },
  { ns: "lab", app: "frigate", services: [{ name: "frigate", ports: [5000, 8554] }] },
  { ns: "data", app: "redis", services: [{ name: "redis", ports: [6379] }] },
  { ns: "lab", app: "worker", services: [] },
];
const form = (over = {}) => ({ after: [], timeout: 300, on_timeout: "start", unreadable: false, managed_by: null,
  gate: "none", candidates, git: "", ...over });
const ctx = { ns: "lab", name: "app-1" };

test("the block lists the stored apps, with other namespaces and a port choice where there is one", () => {
  const c = load();
  const html = c.startsAfterBlock(form({ after: [{ ns: "lab", app: "frigate", service: "frigate", port: 8554 }], gate: "rendered" }), ctx);
  assert.match(html, /Starts after/);
  assert.match(html, /<option value="lab\/frigate" selected>frigate<\/option>/);
  assert.match(html, /redis · data/);
  assert.match(html, /<option value="frigate:8554" selected>8554<\/option>/);
  assert.match(html, /value="5"/, "300 s is shown as 5 minutes");
  assert.match(html, /Start anyway/);
  assert.match(html, /Keep waiting/);
  assert.match(html, /<option value="lab\/worker" disabled>worker · no Service with a TCP port<\/option>/);
});

test("an app that has gone stays in its row, marked", () => {
  const html = load().startsAfterBlock(form({ after: [{ ns: "lab", app: "old-db", service: "old-db", port: 5432 }] }), ctx);
  assert.match(html, /<option value="lab\/old-db" selected>old-db \(gone\)<\/option>/);
});

test("a name is escaped", () => {
  const html = load().startsAfterBlock(form({ after: [{ ns: "lab", app: "<b>x", service: "s", port: 1 }] }), ctx);
  assert.doesNotMatch(html, /<b>x/);
  assert.match(html, /&lt;b>x/);
});

test("an app Flux applies shows the lines to commit and offers to show them for a change", () => {
  const html = load().startsAfterBlock(form({ managed_by: "Flux", after: [{ ns: "lab", app: "postgres", service: "postgres", port: 5432 }],
    gate: "not-committed", git: "metadata:\n  annotations:\n    homestead.io/starts-after: x\n" }), ctx);
  assert.match(html, /<pre[^>]*id="e_starts_git"[^>]*>metadata:/);
  assert.match(html, /where Flux applies it/);
  assert.match(html, /Show the lines to commit/);
});

test("a read that failed says so and offers nothing to change", () => {
  const html = load().startsAfterBlock({ error: "the API timed out" }, ctx);
  assert.match(html, /Could not read Starts after: the API timed out/);
  assert.match(html, /Retry/);
  assert.doesNotMatch(html, /id="e_starts_rows"/);
});

test("a setting edited by hand is said, and can be cleared", () => {
  const html = load().startsAfterBlock(form({ unreadable: true }), ctx);
  assert.match(html, /can't be read/);
  assert.match(html, /Clear it/);
});

test("Homestead and its helpers are told they can't wait", () => {
  const html = load().startsAfterBlock(form(), { ...ctx, fixed: true });
  assert.match(html, /can't wait for other apps/);
  assert.doesNotMatch(html, /id="e_starts_rows"/);
});

test("what Edit sends: nothing untouched, minutes as seconds when changed, an empty list when cleared", () => {
  const c = load();
  const base = form({ after: [{ ns: "lab", app: "postgres", service: "postgres", port: 5432 }] });
  const rows = [{ ns: "lab", app: "postgres", service: "postgres", port: 5432 }];
  assert.equal(c.startsAfterValue(base, { dirty: false, rows, minutes: 5, on_timeout: "start" }), undefined);
  assert.equal(c.startsAfterValue(base, { dirty: true, rows, minutes: 5, on_timeout: "start" }), undefined, "the same again is no change");
  assert.deepEqual(JSON.parse(JSON.stringify(c.startsAfterValue(base, { dirty: true, rows, minutes: 2.5, on_timeout: "wait" }))),
    { after: rows, timeout: 150, on_timeout: "wait" });
  assert.deepEqual(JSON.parse(JSON.stringify(c.startsAfterValue(base, { dirty: true, rows: [], minutes: 5, on_timeout: "start" }))),
    { after: [], timeout: 300, on_timeout: "start" });
  assert.equal(c.startsAfterValue({ error: "x" }, { dirty: true, rows, minutes: 5, on_timeout: "start" }), undefined, "never after a failed read");
  assert.equal(c.startsAfterValue(form({ managed_by: "Flux" }), { dirty: true, rows, minutes: 5, on_timeout: "start" }), undefined, "never on a managed app");
  assert.ok(c.startsAfterValue(form({ unreadable: true }), { dirty: true, rows: [], minutes: 5, on_timeout: "start" }), "clearing a hand-edited one is a change");
});

test("the review says what saving does", () => {
  const c = load();
  assert.equal(c.startsAfterReviewLine("app-1", { after: [{ app: "postgres" }, { app: "redis" }] }),
    "Saving restarts app-1 once: its pods wait for postgres and redis before starting.");
  assert.equal(c.startsAfterReviewLine("app-1", { after: [] }), "Saving restarts app-1 once: its pods no longer wait for other apps.");
  assert.equal(c.startsAfterReviewLine("app-1", undefined), "");
});

test("a refusal about Starts after is recognised; others are not", () => {
  const c = load();
  for (const text of ["a cycle: app-1 → app-2 → app-1", "app-2 has no Service with a TCP port, so nothing can tell when it is ready",
    "app-2's Service has no port 9", "app-1 does not use cluster DNS (dnsPolicy Default), so it can't find app-2; set dnsPolicy to ClusterFirst",
    "an app can't start after itself", "wait up to must be between 30 s and 60 min"])
    assert.ok(c.startsAfterRefusal(text), text);
  assert.ok(!c.startsAfterRefusal("Every container needs a name and image"));
});

test("which picked apps are stopped, so a start would wait", () => {
  const c = load();
  const others = [{ ns: "lab", name: "postgres", desired: 0 }, { ns: "lab", name: "frigate", desired: 1 }];
  assert.deepEqual([...c.startsAfterStopped([{ ns: "lab", app: "postgres" }, { ns: "lab", app: "frigate" }, { ns: "lab", app: "gone" }], others)],
    ["postgres", "gone"]);
});

const NOW = Date.parse("2026-10-23T07:10:00Z") / 1000;
test("a row waiting for an app gets the Waiting chip and how long", () => {
  const w = { name: "app-1", start_wait: { waiting_for: ["app-2"], since: NOW - 180 }, start_setting: { timeout: 300, after: [] }, start_gate: "rendered" };
  const r = load().startWaitText(w, NOW);
  assert.equal(r.chip, "Waiting");
  assert.equal(r.tone, "warn");
  assert.equal(r.line, "Waiting for app-2 (3 min of 5 min)");
});

test("a row that started without an app says when and why", () => {
  const w = { name: "app-1", start_wait: { started_without: ["app-2"], at: Date.parse("2026-10-23T07:05:00Z") / 1000 },
    start_setting: { timeout: 300, after: [] }, start_gate: "rendered" };
  const r = load().startWaitText(w, NOW);
  assert.equal(r.chip, "");
  assert.equal(r.line, "Started without app-2 at 07:05: app-2 was not ready after 5 min");
});

test("a gate that doesn't match, one not committed, and a setting that can't be read", () => {
  const c = load();
  assert.equal(c.startWaitText({ start_gate: "differs", start_setting: { after: [], timeout: 300 } }, NOW).line,
    "Its start gate doesn't match its setting: put it back in Edit → Placement");
  assert.equal(c.startWaitText({ start_gate: "missing", start_setting: { managed: true, after: [], timeout: 300 } }, NOW).line,
    "Its start gate isn't committed yet: commit the lines Edit shows");
  assert.equal(c.startWaitText({ start_unreadable: true, start_gate: "differs", start_note: "its Starts after setting could not be read" }, NOW).line,
    "Could not read its Starts after setting");
  assert.equal(c.startWaitText({ start_unreadable: true, start_gate: "differs" }, NOW).line,
    "Its Starts after setting can't be read: set it again in Edit");
  assert.equal(c.startWaitText({ start_gate: "none" }, NOW), null);
  assert.equal(c.startWaitText({}, NOW), null);
});

test("the apps it starts after, with the gone ones marked", () => {
  const c = load();
  assert.equal(c.startsAfterNames({ start_setting: { after: [{ ns: "lab", app: "postgres", gone: false }, { ns: "lab", app: "old", gone: true }] } }),
    "postgres, old (gone)");
  assert.equal(c.startsAfterNames({ starts_after: ["postgres"] }), "postgres");
  assert.equal(c.startsAfterNames({}), "");
});

test("Start says which stopped apps it will wait for", () => {
  const c = load();
  assert.equal(c.startWaitsLine("app-1", ["app-2"], 300), " app-2 is stopped: app-1 will wait for it up to 5 min.");
  assert.equal(c.startWaitsLine("app-1", ["app-2", "app-3"], 90), " app-2 and app-3 are stopped: app-1 will wait for them up to 1.5 min.");
  assert.equal(c.startWaitsLine("app-1", ["app-2"], null), " app-2 is stopped: app-1 will wait for it before it starts.");
  assert.equal(c.startWaitsLine("app-1", [], 300), "");
});

test("the Architecture page links an app to the apps it starts after, and keeps them per cluster", () => {
  const c = load();
  const graph = c.architectureData({ clusters: [{ site: { id: "a" }, workloads: [{ id: "w:lab/x", claims: [], after: [{ id: "w:lab/y", committed: false }] }],
    volumes: [], nodes: [], vips: [], jobs: [] }] });
  assert.deepEqual(JSON.parse(JSON.stringify(graph.workloads[0].after)), [{ id: "w:a/lab/y", committed: false }]);
  const links = c.archStartLinks([{ id: "w:lab/x", after: [{ id: "w:lab/y", committed: true }, { id: "w:lab/z", committed: false }] }, { id: "w:lab/y" }]);
  assert.deepEqual(JSON.parse(JSON.stringify(links)), [["w:lab/x", "w:lab/y", "starts-after"], ["w:lab/x", "w:lab/z", "starts-after pending"]]);
});

// Review fixes.
test("picking an app rebuilds only its port choice, so the app list keeps focus", () => {
  const c = load();
  EDIT(c);
  const port = { innerHTML: "", hidden: true }, row = { removed: false, remove() { this.removed = true; }, q: s => s === ".sa-port" ? port : null };
  const select = { value: "lab/frigate", closest: () => row };
  c.$ = (sel, root) => root?.q ? root.q(sel) : null;
  c.startsAfterChanged = () => {};
  c.window.startsAfterAppPicked(select);
  assert.equal(row.removed, false);
  assert.match(port.innerHTML, /<option value="frigate:5000">5000<\/option><option value="frigate:8554">8554<\/option>/);
  assert.equal(port.hidden, false);
  select.value = "lab/postgres";
  c.window.startsAfterAppPicked(select);
  assert.equal(port.hidden, true, "one port: nothing to choose");
});

function EDIT(c, over = {}) {
  vm.runInContext(`EDIT_STARTS = ${JSON.stringify({ form: form(over), ctx, dirty: false })}`, c);
}

test("rows are labelled for screen readers", () => {
  const html = load().startsAfterBlock(form({ after: [{ ns: "lab", app: "postgres", service: "postgres", port: 5432 }] }), ctx);
  assert.match(html, /aria-label="Remove this app"/);
  assert.match(html, /role="group" aria-labelledby="e_starts_apps"/);
  assert.match(html, /id="e_starts_apps"/);
});

test("a changed Starts after on an app Flux applies is said in the review, not dropped silently", () => {
  const c = load();
  assert.equal(c.startsAfterManagedNote(form({ managed_by: "Flux" }), true),
    "Starts after is not saved here: Flux applies this app, so commit the lines shown under Placement.");
  assert.equal(c.startsAfterManagedNote(form({ managed_by: "Flux" }), false), "");
  assert.equal(c.startsAfterManagedNote(form(), true), "");
});

test("Show the lines to commit asks for the lines only, and never saves", async () => {
  const c = load();
  EDIT(c, { managed_by: "Flux" });
  const pre = { textContent: "", hidden: true }, sent = [];
  c.$ = sel => sel === "#e_starts_git" ? pre : null;
  c.$$ = () => [];
  c.api = async (path, opts) => { sent.push([path, JSON.parse(opts.body)]); return { git: "metadata: x\n" }; };
  c.toast = () => { throw new Error("no toast: nothing was saved"); };
  await c.window.startsAfterLines();
  assert.equal(sent[0][0], "/api/workloads/starts-after");
  assert.equal(sent[0][1].lines, true);
  assert.equal(pre.textContent, "metadata: x\n");
  assert.equal(pre.hidden, false);
});

test("the time is checked only when there are apps to wait for", () => {
  const c = load();
  assert.equal(c.startsAfterProblem({ after: [], timeout: NaN, on_timeout: "start" }), "");
  assert.equal(c.startsAfterProblem({ after: [{ app: "x" }], timeout: 10, on_timeout: "start" }), "Wait up to: between 0.5 and 60 minutes");
  assert.equal(c.startsAfterProblem({ after: [{ app: "x" }], timeout: 300, on_timeout: "start" }), "");
  assert.equal(c.startsAfterProblem(undefined), "");
});

test("a gate changed outside Homestead can be put back, which sends the setting again", () => {
  const c = load();
  const stored = [{ ns: "lab", app: "postgres", service: "postgres", port: 5432 }];
  const base = form({ after: stored, gate: "differs" });
  assert.match(c.startsAfterBlock(base, ctx), /Put the start gate back/);
  assert.doesNotMatch(c.startsAfterBlock(form({ after: stored, gate: "rendered" }), ctx), /Put the start gate back/);
  assert.doesNotMatch(c.startsAfterBlock(form({ after: stored, gate: "not-committed", managed_by: "Flux" }), ctx), /Put the start gate back/);
  assert.deepEqual(JSON.parse(JSON.stringify(c.startsAfterValue(base, { dirty: true, restore: true, rows: stored, minutes: 5, on_timeout: "start" }))),
    { after: stored, timeout: 300, on_timeout: "start", restore_gate: true });
});

test("Start: several apps it keeps waiting for, and a read that failed", () => {
  const c = load();
  assert.equal(c.startWaitsLine("app-1", ["app-2"], 300, "wait"), " app-2 is stopped: app-1 will wait for it until it is up.");
  assert.equal(c.startWaitsLine("app-1", ["app-2", "app-3"], 300, "wait"), " app-2 and app-3 are stopped: app-1 will wait for them until they are up.");
  assert.equal(c.startWaitsLine("app-1", [], 300, "start", "could not check which apps it starts after: API down"),
    " Could not check which apps it starts after: API down.");
});
