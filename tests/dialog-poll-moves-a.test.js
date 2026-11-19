"use strict";
// The small dialog loops moved onto UI.poll: storage allocation, V2 disk
// preparation, runtime maintenance, V2 setup and its cleanup, and the V2
// upgrade status. Each keeps its interval, its first read, what it shows on a
// failed read and when it stops; and now, as every UI.poll does, it reads
// nothing in a hidden tab, and closing its dialog cancels the read on its way
// (its answer is not painted) and ends the loop. Reopened, it runs once.
const test = require("node:test");
const dialogLoopCases = require("./helpers/dialog-loop-tests");

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
    reopen: w => w.ctx.lhV2Upgrade(), reopenReads: 1,
    failed: /Status could not be refreshed/, failedAt: "#lhV2UpgradeError", afterFailure: "retry", background: true,
  },
];
function upgradeReport() {
  return { installed: "v1.13.0", target: "v1.13.0", v2_volumes: 1, live_ready: true, offline_ready: false, live_blockers: [], offline_blockers: [],
    upgrade: { enabled: true, timeout: 60, current_node: "node-1", nodes: [{ node: "node-1", stage: "in-progress", state: "in-progress" }] } };
}
for (const [name, check] of dialogLoopCases(LOOPS)) {
  test(`${name}`, check);
}
