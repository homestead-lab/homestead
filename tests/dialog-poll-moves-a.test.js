"use strict";
// The small dialog loops moved onto UI.poll: storage allocation, V2 disk
// preparation, runtime maintenance, V2 setup and its cleanup, and the V2
// upgrade status. Each keeps its interval, its first read, what it shows on a
// failed read and when it stops; and now, as every UI.poll does, it reads
// nothing in a hidden tab, and closing its dialog cancels the read on its way
// (its answer is not painted) and ends the loop. Reopened, it runs once.
const test = require("node:test");
const assert = require("node:assert/strict");
const world = require("./helpers/dialog-poll-world");

const g = 1073741824;
const LOOPS = [
  {
    name: "storage allocation", files: "disk-allocation.js", path: "/api/disks/allocation/status", every: 4000, host: "#allocationStatus",
    open: w => w.ctx.diskAllocationWatch("allocation-1"),
    running: () => ({ id: "allocation-1", node: "host-1", mountpoint: "/", target_bytes: 80 * g, phase: "growing", status: "running", progress: 50, message: "Growing the filesystem" }),
    done: () => ({ id: "allocation-1", node: "host-1", mountpoint: "/", target_bytes: 80 * g, phase: "complete", status: "succeeded", progress: 100, message: "Grown" }),
    failed: /Status unavailable/, afterFailure: "retry", background: true,
  },
  {
    name: "V2 disk preparation", files: "disk-v2.js", path: "/api/disks/v2/status", every: 4000, host: "#diskV2Status",
    open: w => w.ctx.diskV2Watch("task-1"),
    running: () => ({ id: "task-1", node: "node-1", device: "/dev/sdb", phase: "evacuating", status: "running", progress: 40, message: "Moving volume copies", remaining: 2, volumes: [] }),
    done: () => ({ id: "task-1", node: "node-1", device: "/dev/sdb", phase: "complete", status: "succeeded", progress: 100, message: "Prepared", remaining: 0, volumes: [] }),
    failed: /Task status unavailable/, afterFailure: "retry", background: true,
  },
  {
    name: "runtime maintenance", files: ["disk-v2.js", "longhorn-runtime.js"], path: "/api/disks/runtime/status", every: 5000, host: "#runtimeStatus",
    open: w => w.ctx.runtimeMoveWatch("runtime-1"),
    running: () => ({ id: "runtime-1", node: "node-1", disk: "data", device: "/dev/dm-1", phase: "copying", status: "running", message: "Copying the runtime", destination: "/var/lib/longhorn-runtime/active", retained: "/mnt/old", stable_device: "/dev/disk/by-id/x", size_bytes: g, runtime_bytes: g }),
    done: () => ({ id: "runtime-1", node: "node-1", disk: "data", device: "/dev/dm-1", phase: "retired", status: "succeeded", message: "Done", destination: "/var/lib/longhorn-runtime/active", retained: "/mnt/old", stable_device: "/dev/disk/by-id/x", size_bytes: g, runtime_bytes: g }),
    failed: /Waiting for host maintenance/, failedShowsCause: false, afterFailure: "retry",
  },
  {
    name: "V2 setup", files: "longhorn-v2-setup.js", path: "/api/longhorn/v2/plan", every: 4000, host: "#lhV2Setup",
    open: w => w.ctx.lhV2Setup(),
    running: () => ({ namespace: "lab", nodes: [{ node: "node-1", required_mib: 2048, target_pages: 1024, capacity_mib: 0, can_prepare: true, review_token: "t", problems: [], job: { name: "job", state: "running" } }], required_mib: 2048, blockers: [], can_enable: false }),
    done: null,   // the setup has no end of its own: it follows until it is closed
    firstFailed: /The setup could not be checked/, failed: /Setup status unavailable/, afterFailure: "retry",
  },
  {
    name: "V2 cleanup", files: "longhorn-v2-setup.js", path: "/api/longhorn/v2/cleanup/status", every: 4000, host: "#v2CleanupStatus",
    open: w => w.ctx.lhV2CleanupWatch("cleanup-1"),
    running: () => ({ id: "cleanup-1", status: "running", message: "Releasing memory", nodes: [] }),
    done: () => ({ id: "cleanup-1", status: "succeeded", message: "Released", nodes: [] }),
    failed: /Status unavailable/, afterFailure: "stop",
  },
  {
    name: "V2 upgrade status", files: "longhorn-v2-upgrade.js", path: "/api/longhorn/v2/upgrade", every: 5000, host: "#lhV2UpgradeBody", later: true,
    // The dialog's own first read (the review flow's) comes before the poll's.
    open: async w => { const opening = w.ctx.lhV2Upgrade(); await w.clock.settle(); w.reads.shift().answer(upgradeReport()); await opening; },
    running: () => upgradeReport(),
    done: null,   // it follows Longhorn's upgrade until the dialog is left
    failed: /Status could not be refreshed/, failedAt: "#lhV2UpgradeError", afterFailure: "retry", background: true,
  },
];
function upgradeReport() {
  return { installed: "v1.13.0", target: "v1.13.0", v2_volumes: 1, live_ready: true, offline_ready: false, live_blockers: [], offline_blockers: [],
    upgrade: { enabled: true, timeout: 60, current_node: "node-1", nodes: [{ node: "node-1", stage: "in-progress", state: "in-progress" }] } };
}
const pollReads = (w, loop) => w.reads.filter(r => r.path.startsWith(loop.path));
async function opened(loop) {
  const w = world(loop.files);
  // Not awaited: the opener waits for its first read, which the test answers.
  const opening = loop.open(w);
  if (loop.later) await opening;
  await w.clock.settle();
  return w;
}
const shown = (w, loop) => (loop.failedAt ? w.fields[loop.failedAt]?.textContent : "") + (w.fields[loop.host]?.innerHTML || "");

for (const loop of LOOPS) {
  test(`${loop.name}: reads ${loop.later ? `${loop.every / 1000} s after opening` : "at once"}, then ${loop.every / 1000} s after each answer`, async () => {
    const w = await opened(loop);
    if (loop.later) {
      assert.equal(pollReads(w, loop).length, 0);
      await w.clock.advance(loop.every - 1);
      assert.equal(pollReads(w, loop).length, 0);
      await w.clock.advance(1);
    }
    assert.equal(pollReads(w, loop).length, 1);
    await w.clock.advance(29000);
    assert.equal(pollReads(w, loop).length, 1, "no second read beside one still on its way");
    pollReads(w, loop)[0].answer(loop.running()); await w.clock.settle();
    await w.clock.advance(loop.every - 1);
    assert.equal(pollReads(w, loop).length, 1);
    await w.clock.advance(1);
    assert.equal(pollReads(w, loop).length, 2, `the next read is ${loop.every / 1000} s after the answer`);
  });

  test(`${loop.name}: a failed read is shown, and the loop ${loop.afterFailure === "retry" ? "tries again" : "stops"}`, async () => {
    const w = await opened(loop);
    if (loop.later) await w.clock.advance(loop.every);
    if (loop.firstFailed) {
      pollReads(w, loop)[0].fail(new Error("connection refused")); await w.clock.settle();
      assert.match(shown(w, loop), loop.firstFailed);
      await w.clock.advance(loop.every);
    }
    pollReads(w, loop).at(-1).fail(new Error("connection refused")); await w.clock.settle();
    assert.match(shown(w, loop), loop.failed);
    const before = pollReads(w, loop).length;
    await w.clock.advance(loop.every);
    assert.equal(pollReads(w, loop).length, before + (loop.afterFailure === "retry" ? 1 : 0));
  });

  // New with the move: a read that never answers no longer holds the dialog for good.
  test(`${loop.name}: a read with no answer in 30 s is shown as failed, and the loop ${loop.afterFailure === "retry" ? "tries again" : "stops"}`, async () => {
    const w = await opened(loop);
    if (loop.later) await w.clock.advance(loop.every);
    if (loop.firstFailed) { pollReads(w, loop)[0].answer(loop.running()); await w.clock.advance(loop.every); }
    const stuck = pollReads(w, loop).at(-1), before = pollReads(w, loop).length;
    await w.clock.advance(30000);
    assert.equal(stuck.signal.reason.name, "TimeoutError");
    assert.match(shown(w, loop), loop.failed);
    if (loop.failedShowsCause !== false) assert.match(shown(w, loop), /No response in 30 s/);
    await w.clock.advance(loop.every);
    assert.equal(pollReads(w, loop).length, before + (loop.afterFailure === "retry" ? 1 : 0));
  });

  if (loop.done) test(`${loop.name}: a finished job ends the reads`, async () => {
    const w = await opened(loop);
    pollReads(w, loop)[0].answer(loop.done()); await w.clock.settle();
    await w.clock.advance(10 * loop.every);
    assert.equal(pollReads(w, loop).length, 1);
    assert.equal(w.clock.pending(), 0, "no timer is left behind");
  });

  // Closing the dialog, in three parts, so each says which part an old loop did not do.
  const closedMidRead = async () => {
    const w = await opened(loop);
    if (loop.later) await w.clock.advance(loop.every);
    const read = pollReads(w, loop).at(-1), before = shown(w, loop);
    w.close();
    return { w, read, before };
  };
  test(`${loop.name}: closing the dialog reads no more`, async () => {
    const { w, read } = await closedMidRead();
    read.answer(loop.running()); await w.clock.settle();
    await w.clock.advance(20 * loop.every);
    assert.equal(pollReads(w, loop).length, 1, "nothing reads after the dialog closed");
  });
  test(`${loop.name}: closing the dialog cancels the read on its way`, async () => {
    const { read } = await closedMidRead();
    assert.equal(read.signal?.aborted, true);
  });
  test(`${loop.name}: closing the dialog drops the answer of the read on its way`, async () => {
    const { w, read, before } = await closedMidRead();
    read.answer(loop.running()); await w.clock.settle();
    assert.equal(shown(w, loop), before, "its answer is not painted");
  });

  test(`${loop.name}: closed and opened again, it runs one loop`, async () => {
    const w = await opened(loop);
    w.answers.auto = read => read.answer(read.path.startsWith(loop.path) ? loop.running() : {});
    if (loop.later) await w.clock.advance(loop.every); else pollReads(w, loop)[0].answer(loop.running());
    await w.clock.advance(loop.every);
    w.close();
    const before = pollReads(w, loop).length;
    if (loop.later) { const opening = w.ctx.lhV2Upgrade(); await opening; await w.clock.settle(); } else { loop.open(w); await w.clock.settle(); }
    await w.clock.advance(4 * loop.every);
    // The upgrade dialog's own first read uses the same address; the poll's are the rest.
    const extra = loop.later ? 1 : 0;
    assert.equal(pollReads(w, loop).length - before, extra + (loop.later ? 4 : 5), "one read per interval, not two");
  });

  // The dialogs that follow a dangerous operation read on in a hidden tab (Andreas, 12 Nov); the others pause.
  if (loop.background) test(`${loop.name}: a hidden tab goes on reading every ${loop.every / 1000} s, and showing it adds no read`, async () => {
    const w = await opened(loop);
    w.answers.auto = read => read.answer(loop.running());
    if (loop.later) await w.clock.advance(loop.every); else pollReads(w, loop)[0].answer(loop.running());
    await w.clock.settle();
    const before = pollReads(w, loop).length;
    w.document.show(false);
    await w.clock.advance(3 * loop.every);
    assert.equal(pollReads(w, loop).length, before + 3, "a read every interval while the tab is hidden");
    w.document.show(true); await w.clock.settle();
    assert.equal(pollReads(w, loop).length, before + 3, "no extra read when it is shown again");
  });
  else test(`${loop.name}: a hidden tab reads nothing, and reads once when shown`, async () => {
    const w = await opened(loop);
    w.answers.auto = read => read.answer(loop.running());
    if (loop.later) await w.clock.advance(loop.every); else pollReads(w, loop)[0].answer(loop.running());
    await w.clock.settle();
    const before = pollReads(w, loop).length;
    w.document.show(false);
    await w.clock.advance(20 * loop.every);
    assert.equal(pollReads(w, loop).length, before, "no read while the tab is hidden");
    w.document.show(true); await w.clock.settle();
    assert.equal(pollReads(w, loop).length, before + 1, "one read when it is shown again");
  });
}
