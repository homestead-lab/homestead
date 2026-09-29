"use strict";
const test = require("node:test"), assert = require("node:assert/strict"), fs = require("node:fs"), path = require("node:path"), vm = require("node:vm");

function load() {
  const source = fs.readFileSync(path.join(__dirname, "..", "web", "js", "views-workloads.js"), "utf8");
  const start = source.indexOf("const CAPACITY_ROUTINE"), end = source.indexOf("window.capacityNotes = capacityNotes;");
  const ctx = { window: {} };
  vm.createContext(ctx);
  vm.runInContext(source.slice(start, end) + "\nwindow.capacityNotes = capacityNotes;", ctx);
  return ctx.window.capacityNotes;
}

// What a standard Windows 11 VM's review listed, word for word (k3s-test, 2.8.232).
const W11 = [
  "No persisted TPM/EFI/CBT state was found: KubeVirt would create fresh state (longhorn, ReadWriteOnce). If this VM ran before, recover its original PVC first. Provisioning/size mutation is not guaranteed; the displayed claim name is only a placement placeholder.",
  "PVC persistent-state-for-w11 is not bound; provisioning and topology need review",
  "PVC persistent-state-for-w11 is planned, not provisioned; storage capacity and attachment remain unverified",
  "PVC w11-disk is not bound; provisioning and topology need review",
  "PVC w11-disk is planned, not provisioned; storage capacity and attachment remain unverified",
  "RAM projection includes the larger of 256 MiB, 5% of guest/reserved RAM and a CPU/thread/process memory floor, not KubeVirt's exact launcher overhead or a memory limit",
  "VM disk persistent-state-for-w11 is planned; provisioning, import completion and free storage are not guaranteed",
  "VM disk w11-disk is planned; provisioning, import completion and free storage are not guaranteed",
  "VM launcher memory is not explicitly limited",
  "disk import w11-disk is planned; provisioning, import completion and free storage are not guaranteed",
  "memory is not limited for vm-launcher-estimate",
  "unrecognised/injected helpers, admission defaults and runtime overhead may require more resources than this lower-bound request",
  "Image import/provisioning may start before the guest. Importer and controller overhead is not fully rendered in this estimate.",
  "If a later step fails, created images, claims or Secrets are retained for inspection; do not blindly repeat creation.",
];

test("a standard new VM has nothing that needs review", () => {
  const notes = load()({ warnings: W11, vm: { action: "create" } });
  assert.deepEqual([...notes.concerns], []);
  assert.equal(notes.caveats.length, W11.length, "kept, under How this is estimated");
});

test("what does need someone still warns", () => {
  const capacityNotes = load();
  const tight = "harvester-node1 would be at 93% of its memory";
  assert.deepEqual([...capacityNotes({ warnings: [...W11, tight], vm: { action: "create" } }).concerns], [tight]);
  // Starting a VM that ran before: fresh TPM state would lose BitLocker keys.
  assert.equal(capacityNotes({ warnings: [W11[0]], vm: { action: "start" } }).concerns.length, 1);
  // A claim that is not bound and not being made here is a real problem.
  assert.equal(capacityNotes({ warnings: ["PVC data is not bound; provisioning and topology need review"] }).concerns.length, 1);
});
