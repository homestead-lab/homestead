"use strict";
// The page loops of Import and Linked clusters moved onto UI.poll (#1406): the moves from
// other clusters (Settings > Linked clusters, started by its card or by an action on a move),
// the container imports running (Import), and the backup storage starting on another cluster
// (one loop per cluster). Each keeps its interval and what a tick does; and now, as every
// page poll does, it reads nothing in a hidden tab and once when shown, gives a read that
// never answers up and tries again, and leaving the page cancels the read on its way and ends
// the loop. The world is tests/helpers/dialog-poll-world.js (the real ui.js, a hand-moved
// clock, a tab that can be hidden); leaving a page is go()'s part: NAV_TOKEN moves on and the
// page's polls stop. It is NOT a browser: the browser groups open these pages for real.
const test = require("node:test"), assert = require("node:assert/strict"), fs = require("node:fs");
const world = require("./helpers/dialog-poll-world");
const between = require("./helpers/between");

const IMPORT = "views-lifecycle-import.js";
// go(): the page is left (its elements go with it unless a test keeps them).
function leave(w, { keep = false } = {}) {
  w.ctx.NAV_TOKEN++;
  w.ctx.STATE.view = "dashboard";
  w.ctx.UI.stopPagePolls();
  if (!keep) for (const key of Object.keys(w.fields)) delete w.fields[key];
}
// A page body drawn into the world: its elements (by id) can then be found with $().
const drawPage = (w, html) => w.ctx.modal("", html);
const pageWorld = (files, extra = {}) => world(files, { NAV_TOKEN: 1, STATE: { view: "settings", data: {} }, ...extra });

/* ---------------- the moves from other clusters ---------------- */
const move = status => ({ id: "m1", name: "camera-app", cluster: "staging", namespace: "lab", kind: "container", status,
  phases: ["joining", "restoring", "done"], phase_index: 1, progress: 40, created_at: new Date().toISOString(), message: "Restoring" });
const MOVES = "/api/move/moves";
const moveReads = w => w.reads.filter(r => r.path === MOVES);

function movesWorld() {
  const w = pageWorld(IMPORT, { moveSourceRow: () => "", moveSourceCheck() {}, fleetPendingMove() {} });
  w.run(between(fs.readFileSync("web/js/fleet.js", "utf8"), "window.fleetMovesPaint = async", "\nwindow.fleetSetMode"));
  drawPage(w, '<div id="fleetMovesCard"></div>');
  // The card's other reads answer at once; the moves wait for the test.
  w.answers.auto = r => r.path === "/api/move/clusters" ? r.answer([]) : r.path === "/api/move/holds" ? r.answer({ holds: [], leftovers: [] }) : undefined;
  return w;
}
// The Linked clusters card drawn with one move running: it follows the moves from 4 s later.
async function cardWithMoveRunning() {
  const w = movesWorld();
  const painting = w.ctx.fleetMovesPaint();
  await w.clock.settle();
  moveReads(w)[0].answer([move("running")]);
  await painting; await w.clock.settle();
  return w;
}

test("moves: the Linked clusters card follows a running move, 4 s after it is drawn and 4 s after each answer", async () => {
  const w = await cardWithMoveRunning();
  assert.match(w.fields["#fleetMovesCard"].innerHTML, /camera-app/);
  await w.clock.advance(3999);
  assert.equal(moveReads(w).length, 1);
  await w.clock.advance(1);
  assert.equal(moveReads(w).length, 2);
  await w.clock.advance(29000);
  assert.equal(moveReads(w).length, 2, "no second read beside one still on its way");
  moveReads(w)[1].answer([{ ...move("running"), message: "Restoring volume 2 of 3" }]); await w.clock.settle();
  assert.match(w.fields["#movesList"].innerHTML, /Restoring volume 2 of 3/);
  await w.clock.advance(4000);
  assert.equal(moveReads(w).length, 3);
  moveReads(w)[2].answer([move("succeeded")]); await w.clock.settle();
  await w.clock.advance(40000);
  assert.equal(moveReads(w).length, 3, "a finished move ends the reads");
  assert.equal(w.clock.pending(), 0, "no timer is left behind");
});

test("moves: a card with no move running does not follow them", async () => {
  const w = movesWorld();
  const painting = w.ctx.fleetMovesPaint();
  await w.clock.settle();
  moveReads(w)[0].answer([move("succeeded")]);
  await painting;
  await w.clock.advance(40000);
  assert.equal(moveReads(w).length, 1);
});

test("moves: an action on a move reads them at once, and follows while one runs or is cleaned up", async () => {
  const w = movesWorld();
  drawPage(w, '<div id="movesList"></div>');
  w.ctx.watchMoves(); await w.clock.settle();
  assert.equal(moveReads(w).length, 1);
  moveReads(w)[0].answer([{ ...move("cancelled"), resource_cleanup_pending: true }]); await w.clock.settle();
  await w.clock.advance(4000);
  assert.equal(moveReads(w).length, 2, "a cleanup still running is followed too");
  moveReads(w)[1].answer([move("cancelled")]); await w.clock.settle();
  await w.clock.advance(40000);
  assert.equal(moveReads(w).length, 2);
});

test("moves: the card drawn again and an action on a move run one loop between them", async () => {
  const w = await cardWithMoveRunning();
  w.answers.auto = r => r.path === MOVES ? r.answer([move("running")]) : r.path === "/api/move/clusters" ? r.answer([]) : r.answer({ holds: [], leftovers: [] });
  await w.ctx.fleetMovesPaint(); await w.clock.settle();
  w.ctx.watchMoves(); await w.clock.settle();
  const before = moveReads(w).length;
  await w.clock.advance(4 * 4000);
  assert.equal(moveReads(w).length - before, 4, "one read per interval, not two or three");
});

test("moves: a failed read is shown with Retry, and the loop tries again", async () => {
  const w = await cardWithMoveRunning();
  await w.clock.advance(4000);
  moveReads(w)[1].fail(new Error("HTTP 503")); await w.clock.settle();
  assert.match(w.fields["#movesList"].innerHTML, /could not be read: HTTP 503/);
  assert.match(w.fields["#movesList"].innerHTML, /Retry/);
  await w.clock.advance(4000);
  assert.equal(moveReads(w).length, 3);
});

// The failure is an alert (role="alert"): drawn again every 4 s it would be announced again
// each time, and a focused Retry would lose its focus. A first failure and a recovery paint.
test("moves: a failure already shown is left as it is while reads keep failing, and a recovery paints the list", async () => {
  const w = await cardWithMoveRunning();
  let content = "", paints = 0;
  w.fields["#movesList"] = { dataset: {} };
  Object.defineProperty(w.fields["#movesList"], "innerHTML", { get: () => content, set: value => { content = value; paints++; } });
  await w.clock.advance(4000);
  moveReads(w)[1].fail(new Error("HTTP 503")); await w.clock.settle();
  assert.equal(paints, 1, "the first failure is shown");
  assert.match(content, /could not be read: HTTP 503/);
  await w.clock.advance(4000);
  moveReads(w)[2].fail(new Error("HTTP 503")); await w.clock.settle();
  assert.equal(paints, 1, "the same failure again leaves the alert and its Retry as they are");
  await w.clock.advance(4000);
  moveReads(w)[3].answer([{ ...move("running"), message: "Restoring again" }]); await w.clock.settle();
  assert.equal(paints, 2, "a recovery paints the list again");
  assert.match(content, /Restoring again/);
  await w.clock.advance(4000);
  moveReads(w)[4].fail(new Error("HTTP 503")); await w.clock.settle();
  assert.equal(paints, 3, "a failure after the recovery is shown again");
  assert.match(content, /could not be read: HTTP 503/);
});

test("moves: a read with no answer in 30 s is given up, said, and tried again", async () => {
  const w = await cardWithMoveRunning();
  await w.clock.advance(4000);
  const stuck = moveReads(w)[1];
  await w.clock.advance(29999);
  assert.equal(stuck.signal?.aborted, false, "not before its limit");
  await w.clock.advance(1);
  assert.equal(stuck.signal?.aborted, true);
  assert.match(w.fields["#movesList"].innerHTML, /No response in 30 s/);
  await w.clock.advance(4000);
  assert.equal(moveReads(w).length, 3);
});

test("moves: a hidden tab reads nothing, and reads once when shown", async () => {
  const w = await cardWithMoveRunning();
  w.answers.auto = r => r.path === MOVES ? r.answer([move("running")]) : undefined;
  await w.clock.advance(4000);
  const before = moveReads(w).length;
  w.document.show(false);
  await w.clock.advance(20 * 4000);
  assert.equal(moveReads(w).length, before, "no read while the tab is hidden");
  w.document.show(true); await w.clock.settle();
  assert.equal(moveReads(w).length, before + 1, "one read when it is shown again");
});

test("moves: leaving the page cancels the read on its way and drops its answer", async () => {
  const w = await cardWithMoveRunning();
  await w.clock.advance(4000);
  const read = moveReads(w)[1], before = w.fields["#movesList"].innerHTML;
  leave(w, { keep: true });
  assert.equal(read.signal?.aborted, true, "cancelled at once, not left holding a connection");
  read.answer([{ ...move("running"), message: "late" }]); await w.clock.settle();
  assert.equal(w.fields["#movesList"].innerHTML, before, "its answer is not painted");
});

test("moves: nothing reads after the page is left", async () => {
  const w = await cardWithMoveRunning();
  w.answers.auto = r => r.path === MOVES ? r.answer([move("running")]) : undefined;
  await w.clock.advance(4000);
  leave(w);
  await w.clock.settle();
  const before = moveReads(w).length;
  await w.clock.advance(20 * 4000);
  assert.equal(moveReads(w).length, before);
  assert.equal(w.clock.pending(), 0, "no timer is left waiting for the page left behind");
});

/* ---------------- the container imports running (Import) ---------------- */
const job = state => ({ name: "import-camera-app", app: "camera-app", namespace: "lab", state, start: "2026-11-20T10:00:00Z", percent: 40 });
function importWorld(state = "running", { hangRefresh = false } = {}) {
  const calls = { refresh: 0 };
  const w = pageWorld(IMPORT, {
    loadHardwareFeatures: async () => {}, paint() {}, menuButton: () => "", tip: () => "", defaultNamespace: () => "lab", fmtWhen: () => "",
    // app.js's refresh(): the page drawn again in place.
    refresh: () => { calls.refresh++; return hangRefresh ? new Promise(() => {}) : w.ctx.viewImport(); },
  });
  w.ctx.STATE.view = "imports";
  w.answers.auto = r => r.answer(r.path === "/api/imports" ? [job(state)] : r.path === "/api/storageclasses?facts=1" ? { facts: {} } : []);
  return { w, calls };
}

test("imports: a copy under way draws the page again 5 s after each draw", async () => {
  const { w, calls } = importWorld();
  await w.ctx.viewImport(); await w.clock.settle();
  await w.clock.advance(4999);
  assert.equal(calls.refresh, 0);
  await w.clock.advance(1);
  assert.equal(calls.refresh, 1);
  await w.clock.advance(5000);
  assert.equal(calls.refresh, 2, "each draw starts the next 5 s, and only one loop runs");
  await w.clock.advance(5 * 5000);
  assert.equal(calls.refresh, 7);
});

test("imports: no copy under way, no redraw", async () => {
  const { w, calls } = importWorld("done");
  await w.ctx.viewImport();
  await w.clock.advance(60000);
  assert.equal(calls.refresh, 0);
  assert.equal(w.clock.pending(), 0);
});

test("imports: a hidden tab draws nothing, and draws once when shown", async () => {
  const { w, calls } = importWorld();
  await w.ctx.viewImport();
  await w.clock.advance(5000);
  const before = calls.refresh;
  w.document.show(false);
  await w.clock.advance(20 * 5000);
  assert.equal(calls.refresh, before, "no redraw while the tab is hidden");
  w.document.show(true); await w.clock.settle();
  assert.equal(calls.refresh, before + 1, "one redraw when it is shown again");
});

test("imports: a redraw with no answer in 30 s is given up, and the next one runs 5 s later", async () => {
  const { w, calls } = importWorld("running", { hangRefresh: true });
  await w.ctx.viewImport();
  await w.clock.advance(5000);
  assert.equal(calls.refresh, 1);
  await w.clock.advance(30000 + 4999);
  assert.equal(calls.refresh, 1, "nothing beside the redraw still on its way");
  await w.clock.advance(1);
  assert.equal(calls.refresh, 2);
});

test("imports: leaving the page ends the redraws", async () => {
  const { w, calls } = importWorld();
  await w.ctx.viewImport();
  await w.clock.advance(5000);
  leave(w);
  await w.clock.advance(20 * 5000);
  assert.equal(calls.refresh, 1);
  assert.equal(w.clock.pending(), 0, "no timer is left waiting for the page left behind");
});

/* ---------------- backup storage starting on another cluster ---------------- */
const READY = "/api/move/clusters/readiness";
const starting = () => ({ storage: { deployed: true, ready: false }, target: {} });
const settled = () => ({ ready: true, storage: { deployed: true, ready: true }, target: { configured: true, reachable_off_cluster: true, url: "http://192.0.2.10:9000" } });
function clusterWorld() {
  const held = [];
  // The readiness check is a POST that waits for the test, as a held read does.
  const w = pageWorld(IMPORT, {
    api: (path, opts = {}) => new Promise((resolve, reject) => {
      const read = { path, name: JSON.parse(opts.body || "{}").name, signal: opts.signal, answer: resolve, fail: reject };
      held.push(read);
      opts.signal?.addEventListener?.("abort", () => reject(opts.signal.reason), { once: true });
    }),
  });
  drawPage(w, '<div id="clready_staging"></div><div id="clready_backup"></div>');
  return { w, checks: name => held.filter(r => r.path === READY && (!name || r.name === name)) };
}
async function storageStarting(name = "staging") {
  const t = clusterWorld();
  t.w.ctx.clusterReady(name); await t.w.clock.settle();
  t.checks(name)[0].answer(starting()); await t.w.clock.settle();
  return t;
}

test("backup storage: while it starts it is checked again 15 s after each answer, and no more once ready", async () => {
  const { w, checks } = await storageStarting();
  assert.match(w.fields["#clready_staging"].innerHTML, /is starting/);
  await w.clock.advance(14999);
  assert.equal(checks().length, 1);
  await w.clock.advance(1);
  assert.equal(checks().length, 2);
  checks()[1].answer(starting()); await w.clock.settle();
  await w.clock.advance(15000);
  assert.equal(checks().length, 3);
  checks()[2].answer(settled()); await w.clock.settle();
  await w.clock.advance(150000);
  assert.equal(checks().length, 3);
  assert.equal(w.clock.pending(), 0, "no timer is left behind");
});

test("backup storage: each cluster has its own loop, and checking one again runs one loop for it", async () => {
  const t = await storageStarting("staging");
  t.w.ctx.clusterReady("backup"); await t.w.clock.settle();
  t.checks("backup")[0].answer(starting()); await t.w.clock.settle();
  t.w.ctx.clusterReady("staging"); await t.w.clock.settle();
  t.checks("staging")[1].answer(starting()); await t.w.clock.settle();
  const auto = () => t.checks().forEach(r => r.answered || (r.answered = true, r.answer(starting())));
  for (let i = 0; i < 4; i++) { await t.w.clock.advance(15000); auto(); await t.w.clock.settle(); }
  assert.equal(t.checks("staging").length, 2 + 4, "one check per interval for staging, not two");
  assert.equal(t.checks("backup").length, 1 + 4, "backup goes on beside it");
});

test("backup storage: a hidden tab checks nothing, and checks once when shown", async () => {
  const { w, checks } = await storageStarting();
  w.document.show(false);
  await w.clock.advance(10 * 15000);
  assert.equal(checks().length, 1, "no check while the tab is hidden");
  w.document.show(true); await w.clock.settle();
  assert.equal(checks().length, 2, "one check when it is shown again");
});

test("backup storage: a failed check is shown, and checked again at the next 15 s", async () => {
  const { w, checks } = await storageStarting();
  await w.clock.advance(15000);
  checks()[1].fail(new Error("HTTP 502")); await w.clock.settle();
  assert.match(w.fields["#clready_staging"].innerHTML, /HTTP 502/);
  await w.clock.advance(14999);
  assert.equal(checks().length, 2);
  await w.clock.advance(1);
  assert.equal(checks().length, 3, "the poll goes on after a failed check");
});

test("backup storage: a check with no answer in 75 s is given up, said, and tried again", async () => {
  const { w, checks } = await storageStarting();
  await w.clock.advance(15000);
  const stuck = checks()[1];
  await w.clock.advance(74999);
  assert.equal(stuck.signal?.aborted, false, "not before its limit");
  await w.clock.advance(1);
  assert.equal(stuck.signal?.aborted, true);
  assert.match(w.fields["#clready_staging"].innerHTML, /No response in 75 s/);
  await w.clock.advance(15000);
  assert.equal(checks().length, 3);
});

test("backup storage: leaving the page cancels the check on its way and drops its answer", async () => {
  const { w, checks } = await storageStarting();
  await w.clock.advance(15000);
  const check = checks()[1], before = w.fields["#clready_staging"].innerHTML;
  leave(w, { keep: true });
  assert.equal(check.signal?.aborted, true, "cancelled at once");
  check.answer(settled()); await w.clock.settle();
  assert.equal(w.fields["#clready_staging"].innerHTML, before, "its answer is not painted");
});

test("backup storage: nothing is checked after the page is left", async () => {
  const { w, checks } = await storageStarting();
  leave(w);
  await w.clock.advance(10 * 15000);
  assert.equal(checks().length, 1);
  assert.equal(w.clock.pending(), 0, "no timer is left waiting for the page left behind");
});
