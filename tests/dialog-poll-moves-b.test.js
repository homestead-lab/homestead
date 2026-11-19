"use strict";
// The second group of dialog loops moved onto UI.poll: the snapshot browser
// being prepared, the cluster shutdown, a new host joining, a volume's
// snapshots, Homestead's data preparation and an OS update run. The same
// checks as the first group (tests/helpers/dialog-loop-tests.js): interval,
// first read, a failed read, when each stops, closing the dialog, opening it
// again, and a hidden tab.
const { webcrypto } = require("node:crypto");
const test = require("node:test");
const dialogLoopCases = require("./helpers/dialog-loop-tests");

const SNAPSHOT_PLAN = { namespace: "longhorn-system", volume: "vol-a", snapshot: "snap-1", claim: "data", size: "1073741824", method: "full-copy", review_token: "t" };
const snapshotState = state => ({ state, stage: "clone", percent: 40, source: SNAPSHOT_PLAN, message: "Copying the snapshot" });
const SHUTDOWN = { run: "run-1", phase: "draining", progress: 30, message: "Stopping apps", deadline: 4e9,
  plan: { nodes: [{ name: "node-a" }], own_node: "node-a", own: ["homestead"] }, hosts: [], drain: null };
const preparation = status => ({ preparations: [{ id: "job-1", destination: "data-b", status, progress: 40, message: "Copying", prepared: false }], nodes: [] });
const osReport = status => ({ applies: true, hosts: {}, checking: {}, rollout: { status, nodes: ["node-a"], index: 0, message: "Updating node-a", results: [] } });

const LOOPS = [
  {
    // Before: the first status read 2 s after the start answered, then every 3 s. Now one 3 s
    // interval with the first read at once (the open choice in the recon, its suggested answer).
    name: "snapshot browser preparation", files: "snapshot-files.js", path: "/api/snapshot-files/status", every: 3000, host: "#snapshotFilesProgress",
    open: w => { w.ctx.crypto = webcrypto; w.answers.reply = () => snapshotState("preparing"); w.run(`snapshotFilesReview = ${JSON.stringify(SNAPSHOT_PLAN)}`); return w.ctx.snapshotFilesStart(); },
    running: () => snapshotState("preparing"),
    done: () => ({ ...snapshotState("failed"), message: "The copy failed" }),
    failed: /Progress unavailable/, afterFailure: "retry",
  },
  {
    name: "cluster shutdown", files: "cluster-shutdown.js", path: "/api/cluster/shutdown", exact: true, every: 2000, host: "#shutdownLive", later: true,
    open: async w => { w.ctx.clusterShutdownProgress(SHUTDOWN); },
    running: () => ({ state: { ...SHUTDOWN, progress: 40 } }),
    done: null,   // it follows every phase until another dialog replaces it
    failed: /Connection lost/, failedShowsCause: false, afterFailure: "retry", background: true,
    readsOnClosed: true,   // closed, it reads on and opens again when the shutdown moves on (tests below)
  },
  {
    name: "new host joining", files: "onboard.js", path: "/api/nodes", every: 10000, host: "#guideWatch",
    open: w => { w.run("ONBOARD.known = new Set(['node-a'])"); w.ctx.modal("Add a host", '<div id="guideWatch"></div>'); return w.run("guideWatch()"); },
    running: () => [{ name: "node-a", status: "Ready" }],
    done: null,   // it waits for a host until the dialog is left
    failed: /The hosts could not be read/, afterFailure: "retry",
  },
  {
    name: "volume snapshots", files: "views-protect.js", path: "/api/lh/snapshots?", every: 4000, host: "#snapshot_live",
    open: w => { w.run("SNAPSHOT_REFRESH = 1"); w.ctx.modal("Snapshots · data", '<div id="snapshot_live" data-token="1"></div>'); return w.run("refreshSnapshotDialog(1, 'vol-a', 'data')"); },
    side: () => ({ active: false }),   // the cleanup progress read beside it
    running: () => [],
    done: null,   // it follows the snapshots until the dialog is left
    failed: /The snapshots could not be refreshed/, afterFailure: "retry",
  },
  {
    name: "Homestead data preparation", files: "views-settings.js", path: "/api/self/data/prepare", exact: true, every: 3000, host: "#selfDataFlow",
    open: w => { w.ctx.modal("Move Homestead data", '<div id="selfDataFlow"></div>'); w.run("selfDataDialog = { jobs: [{ id: 'job-1', destination: 'data-b', node: 'node-a' }] }"); return w.ctx.selfDataWatch(0); },
    running: () => preparation("running"),
    done: () => preparation("succeeded"),
    failed: /Check again/, afterFailure: "stop",
  },
  {
    name: "OS update run", files: "host-os.js", path: "/api/os-updates", exact: true, every: 3000, host: "#osu_status", later: true,
    open: async w => { w.ctx.modal("OS updates", '<div id="osu_status"></div><div id="osu_hosts"></div><div id="osu_run"></div>'); w.run(`osUpdatesFollow(${JSON.stringify(osReport("running").rollout)})`); },
    running: () => osReport("running"),
    done: () => ({ ...osReport("succeeded"), rollout: { status: "succeeded", nodes: ["node-a"], results: [] } }),
    failed: null, afterFailure: "retry", background: true,   // a failed read paints nothing and tries again
  },
];
for (const [name, check] of dialogLoopCases(LOOPS)) {
  test(`${name}`, check);
}

// What the shared checks cannot see: each loop's own state between reads.
const assert = require("node:assert/strict");
const world = require("./helpers/dialog-poll-world");

test("snapshot browser: a ready answer hands the session to the file manager and ends the poll", async () => {
  const handed = [];
  const w = world("snapshot-files.js", { crypto: webcrypto, fileManagerSnapshot: state => handed.push(state) });
  w.answers.reply = () => snapshotState("preparing");
  w.run(`snapshotFilesReview = ${JSON.stringify(SNAPSHOT_PLAN)}`);
  w.ctx.snapshotFilesStart(); await w.clock.settle();
  const reads = () => w.reads.filter(r => r.path.startsWith("/api/snapshot-files/status"));
  reads()[0].answer({ ...snapshotState("ready"), path: "/" }); await w.clock.settle();
  assert.equal(handed.length, 1, "the file manager is opened once");
  assert.equal(handed[0].state, "ready");
  assert.equal(w.run("snapshotFilesSession"), null, "the file manager owns the session now");
  assert.equal(w.clock.pending(), 0, "the poll ends at once, with no timer left");
  await w.clock.advance(10 * 3000);
  assert.equal(reads().length, 1, "and reads no more");
});

const shutdownWorld = extra => {
  const w = world("cluster-shutdown.js", extra);
  w.ctx.clusterShutdownProgress(SHUTDOWN);
  return w;
};
const shutdownReads = w => w.reads.filter(r => r.path === "/api/cluster/shutdown");

test("cluster shutdown: \"Connection lost\" shows the last state read, not the one the dialog opened with", async () => {
  const w = shutdownWorld();
  await w.clock.advance(2000);
  shutdownReads(w)[0].answer({ state: { ...SHUTDOWN, progress: 40 } });
  await w.clock.advance(2000);
  shutdownReads(w)[1].fail(new Error("connection refused")); await w.clock.settle();
  const html = w.fields["#shutdownLive"].innerHTML;
  assert.match(html, /Connection lost/);
  assert.match(html, /aria-valuenow="40"/, "the progress last read");
  assert.doesNotMatch(html, /aria-valuenow="30"/);
});

test("cluster shutdown: a read still waiting after 10 s is \"Connection lost\", well before the poll's own 30 s", async () => {
  const limits = [];
  const FakeSignal = { any: signals => AbortSignal.any(signals),
    timeout: ms => { const c = new AbortController(); limits.push({ ms, fire: () => c.abort(new DOMException("signal timed out", "TimeoutError")) }); return c.signal; } };
  const w = shutdownWorld({ AbortSignal: FakeSignal });
  await w.clock.advance(2000);
  assert.equal(shutdownReads(w).length, 1);
  assert.deepEqual(limits.map(l => l.ms), [10000], "each read has a 10 s limit");
  limits[0].fire(); await w.clock.settle();
  assert.match(w.fields["#shutdownLive"].innerHTML, /Connection lost/);
  await w.clock.advance(2000);
  assert.equal(shutdownReads(w).length, 2, "and it reads again 2 s later");
});

// Closed, the shutdown dialog reads on, and opens itself again on a phase change or an overrun,
// as on dev (owner's decision, 14 Nov). Another dialog in its place ends the loop.
const opened = async () => {
  const w = shutdownWorld();
  await w.clock.advance(2000);
  shutdownReads(w)[0].answer({ state: { ...SHUTDOWN, progress: 40 } }); await w.clock.settle();
  w.close();
  assert.equal(w.open, false);
  return w;
};

test("cluster shutdown: closed, it reads on and stays closed while the phase holds", async () => {
  const w = await opened();
  await w.clock.advance(2000);
  assert.equal(shutdownReads(w).length, 2, "it reads on after the dialog closed");
  shutdownReads(w)[1].answer({ state: { ...SHUTDOWN, progress: 45 } }); await w.clock.settle();
  assert.equal(w.open, false, "the same phase does not open it");
  assert.match(w.fields["#shutdownLive"].innerHTML, /aria-valuenow="45"/, "the closed body stays current");
});

test("cluster shutdown: closed, a phase change opens the dialog again with the new phase, and one loop runs", async () => {
  const w = await opened();
  await w.clock.advance(2000);
  shutdownReads(w)[1].answer({ state: { ...SHUTDOWN, phase: "stopping-homestead", progress: 70, message: "Stopping Homestead" } }); await w.clock.settle();
  assert.equal(w.open, true, "the dialog is shown again");
  assert.equal(w.title, "Cluster shutdown progress");
  assert.match(w.fields["#shutdownLive"].innerHTML, /Stopping Homestead/);
  const before = shutdownReads(w).length;
  w.pollAuto = null; w.answers.auto = r => r.answer({ state: { ...SHUTDOWN, phase: "stopping-homestead", progress: 70 } });
  await w.clock.advance(3 * 2000);
  assert.equal(shutdownReads(w).length - before, 3, "one read per interval, not two");
});

test("cluster shutdown: closed, an overrun past the deadline opens the dialog again to uncordon", async () => {
  const w = await opened();
  await w.clock.advance(2000);
  shutdownReads(w)[1].answer({ state: { ...SHUTDOWN, progress: 40, deadline: 1 } }); await w.clock.settle();
  assert.equal(w.open, true, "the dialog is shown again");
  assert.ok(w.fields["#shutdownLive"], "with the progress");
  assert.equal(w.fields["#shutdownCancel"], undefined, "no Cancel once overrun");
});

test("cluster shutdown: another dialog in its place ends the loop, with no timer left", async () => {
  const w = await opened();
  w.ctx.modal("Something else", '<div id="other"></div>');
  await w.clock.advance(10 * 2000);
  assert.equal(shutdownReads(w).length, 1, "no read once its body is gone");
  assert.equal(w.clock.pending(), 0, "no timer is left behind");
});

test("cluster shutdown: another dialog drawn during a read drops its answer and ends the loop", async () => {
  const w = await opened();
  await w.clock.advance(2000);
  w.ctx.modal("Something else", '<div id="other"></div>');
  shutdownReads(w)[1].answer({ state: { ...SHUTDOWN, phase: "stopping-homestead", progress: 70 } }); await w.clock.settle();
  assert.equal(w.title, "Something else", "the other dialog is not replaced");
  await w.clock.advance(10 * 2000);
  assert.equal(shutdownReads(w).length, 2);
  assert.equal(w.clock.pending(), 0, "no timer is left behind");
});

test("OS update run: the buttons are redrawn when the run changes, not on every read", async () => {
  const w = world("host-os.js");
  w.ctx.modal("OS updates", '<div id="osu_status"></div><div id="osu_hosts"></div><div id="osu_run"></div>');
  const rollout = waiting => ({ status: "running", nodes: ["node-a", "node-b"], index: 0, message: "Updating", results: [], ...(waiting === null ? {} : { node: { name: "node-a", waiting } }) });
  w.run(`osUpdatesFollow(${JSON.stringify(rollout(null))})`);
  const reads = () => w.reads.filter(r => r.path === "/api/os-updates");
  await w.clock.advance(3000);
  reads()[0].answer({ applies: true, hosts: {}, checking: {}, rollout: rollout(true) }); await w.clock.settle();
  assert.match(w.fields["#osu_run"].innerHTML, /Stop and leave this host/, "a host now waits: its button is drawn");
  w.fields["#osu_run"].innerHTML = "unchanged";
  await w.clock.advance(3000);
  reads()[1].answer({ applies: true, hosts: {}, checking: {}, rollout: rollout(true) }); await w.clock.settle();
  assert.equal(w.fields["#osu_run"].innerHTML, "unchanged", "the same run is not redrawn");
});
