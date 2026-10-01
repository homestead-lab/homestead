"use strict";
const test = require("node:test"), assert = require("node:assert/strict");
const fs = require("node:fs"), vm = require("node:vm");

function fixture() {
  const ctx = { STATE: { data: {} }, esc: String, sizeText: n => `${n} GB` };
  ctx.window = ctx;
  vm.createContext(ctx);
  vm.runInContext(fs.readFileSync("web/js/host-os.js", "utf8"), ctx);
  return ctx;
}

test("cached partition maps use current hardware and retain LVM system disks", () => {
  const ctx = fixture();
  const facts = { disks: [{ name: "nvme0n1", size: 512 }, { name: "sda", size: 3000 }, { name: "sdc", size: 80 }] };
  assert.equal(ctx.hostPhysicalLayouts("node-a", facts).length, 3, "do not hide unknown hardware when probe inventory is unavailable");
  ctx.STATE.data.disks = { nodes: { "node-a": [{ device: "nvme0n1" }, { device: "sda" }] } };
  assert.deepEqual(Array.from(ctx.hostPhysicalLayouts("node-a", facts), d => d.name), ["nvme0n1", "sda"]);
});

test("whole-disk filesystems and LVM partitions have useful labels", () => {
  const ctx = fixture();
  const whole = ctx.partitionMap({ name: "sda", size: 1024 ** 3, fstype: "ext4", mount: "/mnt/data", partitions: [] });
  assert.match(whole, /whole-disk ext4 filesystem/);
  assert.doesNotMatch(whole, /no partition table/);
  const lvm = ctx.partitionMap({ name: "nvme0n1", size: 1024 ** 3, table: "gpt", partitions: [
    { name: "nvme0n1p3", size: 1024 ** 3, start: 0, fstype: "LVM2_member", mounts: ["/"], holds: ["lvm"] },
  ] });
  assert.match(lvm, /LVM physical volume/);
  assert.doesNotMatch(lvm, /LVM2_member/);
});
