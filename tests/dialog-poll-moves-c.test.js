"use strict";
// The last three dialog loops moved onto UI.poll (#1406): the image store
// following its downloads, a StatefulSet "Starting elsewhere" and a host's
// power job. The two job dialogs get the checks every moved loop gets
// (tests/helpers/dialog-loop-tests.js); the image store opens with a read of
// its own on the same path as its poll, so its checks are written out below.
const test = require("node:test");
const assert = require("node:assert/strict");
const dialogLoopCases = require("./helpers/dialog-loop-tests");
const world = require("./helpers/dialog-poll-world");

/* ---- the image store: downloads followed, the list repainted ---- */
const STORE = "/api/vm/store";
const storeImage = (progress, ready = false) => ({ id: "debian-12", name: "Debian 12", distro: "Debian", variant: "generic", about: "", user: "debian",
  size: 1024 ** 3, available: true, kept: true, auto: false, versions: [{ ready, failed: false, progress, added: 1700000000, image: "img-a" }] });
const store = (progress, ready = false) => ({ arch: "amd64", harvester: true, note: "", own: [], images: [storeImage(progress, ready)] });
const storeReads = w => w.reads.filter(r => r.path.startsWith(STORE));

async function storeOpened(first = store(40)) {
  const w = world("views-vms.js", { STATE: { data: {} }, fmtDay: () => "1 Jan", tip: () => "" });
  // The real modal keeps its body in #mbody; the world's modal draws only what it is given.
  const draw = w.ctx.modal;
  w.ctx.modal = (title, body) => draw(title, `<div id="mbody">${body}</div>`);
  const opening = w.ctx.vmStore();
  await w.clock.settle();
  storeReads(w)[0].answer(first);
  await opening; await w.clock.settle();
  return w;
}

test("Image store: downloads under way are read again 5 s after the dialog's read, then 5 s after each answer", async () => {
  const w = await storeOpened();
  await w.clock.advance(4999);
  assert.equal(storeReads(w).length, 1);
  await w.clock.advance(1);
  assert.equal(storeReads(w).length, 2);
  await w.clock.advance(30000 - 1000);
  assert.equal(storeReads(w).length, 2, "no second read beside one still on its way");
  storeReads(w)[1].answer(store(60)); await w.clock.settle();
  await w.clock.advance(4999);
  assert.equal(storeReads(w).length, 2);
  await w.clock.advance(1);
  assert.equal(storeReads(w).length, 3);
});

test("Image store: with nothing downloading, nothing more is read", async () => {
  const w = await storeOpened(store(100, true));
  await w.clock.advance(60000);
  assert.equal(storeReads(w).length, 1);
  assert.equal(w.clock.pending(), 0);
});

test("Image store: a download that finishes ends the reads, and the list shows it", async () => {
  const w = await storeOpened();
  await w.clock.advance(5000);
  storeReads(w)[1].answer(store(100, true)); await w.clock.settle();
  await w.clock.advance(60000);
  assert.equal(storeReads(w).length, 2);
  assert.equal(w.clock.pending(), 0, "no timer is left behind");
  assert.match(w.fields["#vmStoreRows"]?.innerHTML || w.fields["#mbody"].innerHTML, /here · 1 Jan/);
});

test("Image store: a read repaints the list, not the whole dialog", async () => {
  const w = await storeOpened();
  assert.match(w.fields["#vmStoreRows"]?.innerHTML || w.fields["#mbody"].innerHTML, /downloading 40%/);
  // Writing the whole body again makes every element in it anew (as the browser does).
  const box = w.fields["#vmStoreFilter"];
  await w.clock.advance(5000);
  storeReads(w)[1].answer(store(60)); await w.clock.settle();
  assert.equal(w.fields["#vmStoreFilter"], box, "the filter box, and the dialog's text around the list, are left as they are");
  assert.match(w.fields["#vmStoreRows"].innerHTML, /downloading 60%/);
});

test("Image store: closing the dialog reads no more, and it stays closed", async () => {
  const w = await storeOpened();
  w.close();
  await w.clock.advance(60000);
  assert.equal(w.open, false, "it does not open itself again");
  assert.equal(storeReads(w).length, 1, "nothing reads after the dialog closed");
});

test("Image store: closing the dialog cancels the read on its way, and its answer is not painted", async () => {
  const w = await storeOpened();
  await w.clock.advance(5000);
  const read = storeReads(w)[1];
  w.close();
  assert.equal(read.signal?.aborted, true);
  read.answer(store(60)); await w.clock.settle();
  assert.doesNotMatch(w.fields["#vmStoreRows"]?.innerHTML || w.fields["#mbody"].innerHTML, /downloading 60%/);
});

test("Image store: a hidden tab reads nothing, and reads once when shown", async () => {
  const w = await storeOpened();
  w.answers.auto = r => r.answer(store(50));
  w.document.show(false);
  await w.clock.advance(60000);
  assert.equal(storeReads(w).length, 1, "no read while the tab is hidden");
  w.document.show(true); await w.clock.settle();
  assert.equal(storeReads(w).length, 2, "one read when it is shown again");
});

test("Image store: a read with no answer in 30 s is given up and shown with Try again, and following stops", async () => {
  const w = await storeOpened();
  await w.clock.advance(5000);
  const stuck = storeReads(w)[1];
  await w.clock.advance(29999);
  assert.equal(stuck.signal?.aborted, false, "not before its limit");
  await w.clock.advance(1);
  assert.equal(stuck.signal?.aborted, true);
  assert.match(w.fields["#mbody"].innerHTML, /No response in 30 s/);
  assert.match(w.fields["#mbody"].innerHTML, /Try again/);
  await w.clock.advance(60000);
  assert.equal(storeReads(w).length, 2, "Try again starts it again, nothing else");
});

test("Image store: a failed read is shown with Try again, and following stops", async () => {
  const w = await storeOpened();
  await w.clock.advance(5000);
  storeReads(w)[1].fail(new Error("connection refused")); await w.clock.settle();
  assert.match(w.fields["#mbody"].innerHTML, /connection refused/);
  assert.match(w.fields["#mbody"].innerHTML, /Try again/);
  await w.clock.advance(60000);
  assert.equal(storeReads(w).length, 2);
});

test("Image store: drawn again while following (a filter, Keep, Try again), it runs one loop", async () => {
  const w = await storeOpened();
  w.answers.auto = r => r.answer(store(50));
  await w.clock.advance(2000);
  const redraw = w.ctx.vmStore(); await w.clock.settle(); await redraw; await w.clock.settle();
  const before = storeReads(w).length;
  await w.clock.advance(20000);
  assert.equal(storeReads(w).length - before, 4, "one read every 5 s, not two");
});

/* ---- Starting elsewhere ---- */
const restartJob = (status, extra = {}) => [{ id: "job-1", status, progress: status === "succeeded" ? 100 : 40, message: "Waiting for the pod on node-b", ...extra }];
for (const [name, check] of dialogLoopCases([{
  name: "Starting elsewhere", files: "set-restart.js", path: "/api/operations", exact: true, every: 2000, host: "#srProgress",
  open: w => w.ctx.setRestartFollow("job-1", "db"),
  running: () => restartJob("running"),
  done: () => restartJob("succeeded"),
  // Before the first answer a failed read leaves "Starting…" (below: after one, "Reconnecting").
  failed: null, afterFailure: "retry",
}])) test(name, check);

async function restartOpened() {
  const w = world("set-restart.js");
  w.ctx.setRestartFollow("job-1", "db");
  await w.clock.settle();
  return w;
}

test("Starting elsewhere: after an answer, a failed read keeps the last progress, says Reconnecting, and tries again", async () => {
  const w = await restartOpened();
  w.reads[0].answer(restartJob("running")); await w.clock.advance(2000);
  w.reads[1].fail(new Error("connection refused")); await w.clock.settle();
  const shown = w.fields["#srProgress"].innerHTML;
  assert.match(shown, /Reconnecting/);
  assert.match(shown, /Waiting for the pod on node-b/, "the last progress stays");
  await w.clock.advance(2000);
  assert.equal(w.reads.length, 3);
  w.reads[2].answer(restartJob("running", { message: "Pod started" })); await w.clock.settle();
  assert.doesNotMatch(w.fields["#srProgress"].innerHTML, /Reconnecting/);
});

test("Starting elsewhere: a job that is not found says so and ends the reads", async () => {
  const w = await restartOpened();
  w.reads[0].answer([]); await w.clock.settle();
  assert.match(w.fields["#srProgress"].innerHTML, /Job not found/);
  await w.clock.advance(20000);
  assert.equal(w.reads.length, 1);
  assert.equal(w.clock.pending(), 0);
});

test("Starting elsewhere: a job that stopped shows Stopped and ends the reads", async () => {
  const w = await restartOpened();
  w.reads[0].answer(restartJob("failed", { message: "The pod did not start" })); await w.clock.settle();
  assert.match(w.fields["#srProgress"].innerHTML, /Stopped/);
  await w.clock.advance(20000);
  assert.equal(w.reads.length, 1);
});

/* ---- host power job ---- */
const powerJob = (status, extra = {}) => [{ id: "power-1", status, progress: 30, message: "Moving apps",
  power: { action: "reboot", phase: status === "running" ? "draining" : "observing" }, ...extra }];
for (const [name, check] of dialogLoopCases([{
  name: "host power job", files: "views-lifecycle-node-power.js", path: "/api/operations", exact: true, every: 2000, host: "#pwProgress",
  open: w => w.ctx.nodePowerFollow("power-1"),
  running: () => powerJob("running"),
  done: () => powerJob("succeeded"),
  failed: /Waiting for Homestead to reconnect/, failedShowsCause: false, afterFailure: "retry",
  // A host restart or shutdown is followed as it happens, in a hidden tab too (decided 12 Nov for dangerous operations).
  background: true,
}])) test(name, check);

test("host power job: an access error says so and ends the reads", async () => {
  const w = world("views-lifecycle-node-power.js");
  w.ctx.nodePowerFollow("power-1"); await w.clock.settle();
  w.reads[0].fail(Object.assign(new Error("forbidden"), { status: 403 })); await w.clock.settle();
  assert.match(w.fields["#pwProgress"].innerHTML, /Sign in with access to Jobs/);
  await w.clock.advance(20000);
  assert.equal(w.reads.length, 1);
  assert.equal(w.clock.pending(), 0);
});

test("host power job: following another job replaces the first, one read at a time", async () => {
  const w = world("views-lifecycle-node-power.js");
  w.ctx.nodePowerFollow("power-1"); await w.clock.settle();
  const first = w.reads[0];
  w.ctx.nodePowerFollow("power-2"); await w.clock.settle();
  assert.equal(first.signal?.aborted, true, "the first job's read is cancelled");
  w.answers.auto = r => r.answer([{ ...powerJob("running")[0], id: "power-2" }]);
  w.reads[1].answer([{ ...powerJob("running")[0], id: "power-2" }]);
  const before = w.reads.length;
  await w.clock.advance(8000);
  assert.equal(w.reads.length - before, 4, "one read every 2 s, not two");
});

test("host power job: the disconnected minutes count from the first failed read, through a read given up and a hidden tab", async () => {
  const w = world("views-lifecycle-node-power.js");
  w.ctx.Date = { now: () => 1e12 + w.clock.now() };   // the dialog's clock is the test's
  const shown = () => w.fields["#pwProgress"].innerHTML;
  w.ctx.nodePowerFollow("power-1"); await w.clock.settle();
  w.reads[0].answer(powerJob("running")); await w.clock.advance(2000);
  w.reads[1].fail(new Error("connection refused")); await w.clock.settle();      // lost at 2 s
  assert.match(shown(), /Waiting for Homestead to reconnect/);
  assert.doesNotMatch(shown(), /disconnected for/);
  await w.clock.advance(2000);                                                    // the read at 4 s never answers
  await w.clock.advance(30000);                                                   // given up at 34 s: still disconnected
  assert.equal(w.reads[2].signal.aborted, true);
  assert.match(shown(), /Waiting for Homestead to reconnect/);
  w.answers.auto = r => r.fail(new Error("connection refused"));
  w.document.show(false);
  const hiddenFrom = w.reads.length;
  // 962 s is 16 min after the loss at 2 s, but under 16 min after the give-up at 34 s.
  await w.clock.advance(962000 - w.clock.now());
  assert.ok(w.reads.length - hiddenFrom >= 400, "reads go on every 2 s in a hidden tab");
  assert.match(shown(), /disconnected for 16 min/, "counted from the first failed read, hidden time included");
  assert.match(shown(), /Still waiting for Homestead/);
});
