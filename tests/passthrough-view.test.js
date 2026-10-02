"use strict";
const test = require("node:test"), assert = require("node:assert/strict"), fs = require("node:fs"), vm = require("node:vm");

function setup() {
  const fields = {"#pd_pick": {value: "example.test/gpu"}, "#pd_adds": {insertAdjacentHTML: (_, html) => fields.added = html}};
  const rows = {added: [], all: []};
  const ctx = {console, Set, Uint8Array, btoa, document: {},
    $: key => fields[key], $$: selector => selector === "#mbody .pd-add" || selector === "#pd_adds .pd-add" ? rows.added : rows.all,
    esc: value => String(value).replaceAll("&", "&amp;").replaceAll('"', "&quot;").replaceAll("<", "&lt;"),
    UI: {lead: String, actions: String, button: (label, handler) => `${label} ${handler}`, more: (label, html) => `${label}${html}`, callout: (_, label, detail) => `${label}${detail}`}};
  ctx.window = ctx; vm.createContext(ctx);
  vm.runInContext(fs.readFileSync("web/js/passthrough.js", "utf8"), ctx);
  return {ctx, fields, rows};
}
const resources = {resources: [{resource: "example.test/gpu", kind: "pci", label: "GPU", nodes: ["node1"]}]};

test("new device can receive a ROM immediately and gets a stable unused name", () => {
  const t = setup();
  t.ctx.vmDevicesPane({host_devices: [{name: "hostdev-0", resource: "example.test/gpu"}]}, resources);
  t.ctx.vmAddHostDevice();
  assert.match(t.fields.added, /data-hostdev="hostdev-1"/);
  assert.match(t.fields.added, /type="file" class="pd_rom"/);
  assert.match(t.fields.added, /node1/);
});

test("device edits send new ROM, remap, remove, and clear choices independently", async () => {
  const t = setup(), rom = {arrayBuffer: async () => Uint8Array.from([0x55, 0xaa, 0]).buffer};
  const row = (name, resource, selectors) => ({dataset: {hostdev: name, resource}, querySelector: selector => selectors[selector]});
  const added = row("hostdev-3", "example.test/gpu", {".pd_rom": {files: [rom]}});
  t.rows.added = [added];
  t.rows.all = [row("hostdev-0", "old/gpu", {".pd_resource": {value: "example.test/gpu"}}),
    row("hostdev-1", "old/gpu", {".pd_rm": {checked: true}}),
    row("hostdev-2", "old/gpu", {".pd_clear": {checked: true}}), added];
  const changes = JSON.parse(JSON.stringify(await t.ctx.vmDevicesChanges()));
  assert.deepEqual(changes.add, [{name: "hostdev-3", resource: "example.test/gpu"}]);
  assert.deepEqual(changes.map, {"hostdev-0": "example.test/gpu"});
  assert.deepEqual(changes.remove, ["hostdev-1"]);
  assert.deepEqual(changes.roms, {"hostdev-2": "", "hostdev-3": "VaoA"});
});

test("invalid ROM bytes and oversized files are rejected before review", async () => {
  const t = setup();
  for (const bytes of [Uint8Array.from([0x4d, 0x5a]), new Uint8Array(641 * 1024)]) {
    await assert.rejects(t.ctx.vmReadRom({arrayBuffer: async () => bytes.buffer}), /55 AA|640 KiB/);
  }
});

test("empty device inventory explains host setup and read failure is distinct", () => {
  const t = setup();
  const empty = t.ctx.vmDevicesPane({}, {resources: []});
  assert.match(empty, /IOMMU.*vBIOS setup/);
  assert.match(empty, /Hardware → Devices for VMs/);
  assert.match(empty, /target="_blank"/);
  assert.match(t.ctx.vmDevicesPane({}, {resources: [], error: "<unavailable>"}), /Devices could not be loaded.*&lt;unavailable>/);
});
