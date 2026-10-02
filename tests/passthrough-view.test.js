"use strict";
const test = require("node:test"), assert = require("node:assert/strict"), fs = require("node:fs"), vm = require("node:vm");

function setup() {
  const fields = {"#pd_pick": {value: "example.test/gpu"}, "#pd_adds": {insertAdjacentHTML: (_, html) => fields.added = html}};
  const rows = {added: [], all: []};
  const body = {innerHTML: ""};
  fields["#nodeDevices"] = {dataset: {}, querySelector: () => body};
  const requests = [];
  const answers = {};
  const questions = [];

  const ctx = {console, Set, Uint8Array, btoa, document: {},
    jsArg: JSON.stringify,
    api: async (url, opts) => { requests.push({url, opts}); const answer = answers[url]; return typeof answer === "function" ? answer() : answer || {}; },
    ask: async text => { questions.push(text); return false; },
    $: key => fields[key], $$: selector => selector === "#mbody .pd-add" || selector === "#pd_adds .pd-add" ? rows.added : rows.all,
    esc: value => String(value).replaceAll("&", "&amp;").replaceAll('"', "&quot;").replaceAll("<", "&lt;"),
    UI: {lead: String, actions: String, chip: String, section: (label, html) => `${label}${html}`, table: (_, rows) => rows.flat().join(""), button: (label, handler) => `${label} ${handler}`, more: (label, html) => `${label}${html}`, callout: (_, label, detail) => `${label}${detail}`}};
  ctx.window = ctx; vm.createContext(ctx);
  vm.runInContext(fs.readFileSync("web/js/passthrough.js", "utf8"), ctx);
  return {ctx, fields, rows, body, requests, answers, questions};
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


const hostFacts = {node: "node1", iommu: true, inspected_at: 1700000000, pci: [
  {address: "0000:01:00.0", name: "Named GPU", class: "0300", offered: true, group: "12", group_members: ["0000:01:00.1", "0000:00:01.0"]},
  {address: "0000:01:00.1", name: "GPU audio", class: "0403", offered: true, group: "12", group_members: ["0000:01:00.0", "0000:00:01.0"]},
  {address: "0000:00:01.0", name: "PCI bridge", class: "0604", offered: false, group: "12", group_members: ["0000:01:00.0", "0000:01:00.1"]},
  {address: "0000:00:02.0", name: "Integrated GPU", class: "0300", offered: true, group: 0, group_members: []}
], usb: []};

test("reopening the host restores a retained inspection and groups every companion by name", async () => {
  const t = setup();
  t.answers["/api/passthrough/inventory?node=node1"] = {facts: hostFacts};
  await t.ctx.nodeDevicesPaint("node1");
  assert.match(t.body.innerHTML, /IOMMU group 12 · 3 devices/);
  assert.match(t.body.innerHTML, /2 devices move together.*1 PCI bridge stays/);
  for (const name of ["Named GPU", "GPU audio", "PCI bridge", "IOMMU group 0"]) assert.ok(t.body.innerHTML.includes(name));
  assert.match(t.body.innerHTML, /Refresh devices/);
  assert.match(t.body.innerHTML, /Last inspected/);
  await t.ctx.nodeDevicesPaint("node1");
  assert.equal(t.requests.length, 1, "reopening does not rerun a host helper or hide inventory");
  assert.ok(!t.requests.some(r => r.opts), "restoring the inventory performs only a GET");
  await t.ctx.ptPci("node1", "0000:01:00.0", true);
  assert.match(t.questions[0], /GPU audio \(0000:01:00.1\)/);
  assert.ok(!t.questions[0].includes("PCI bridge"), "bridge is not handed over");
});

test("a blocked group companion explains why its GPU cannot be offered", async () => {
  const t = setup();
  const facts = JSON.parse(JSON.stringify(hostFacts));
  facts.pci[1].problems = ["it carries this host's network"];
  t.answers["/api/passthrough/inventory?node=node1"] = {facts};
  await t.ctx.nodeDevicesPaint("node1");
  assert.match(t.body.innerHTML, /GPU audio: it carries this host's network/);
  assert.ok(!t.body.innerHTML.includes('ptPci("node1","0000:01:00.0",true)'), "group protection is visible before offering");
});

test("failed refresh retains the inventory and offers retry", async () => {
  const t = setup();
  t.answers["/api/passthrough/inventory?node=node1"] = {facts: hostFacts};
  await t.ctx.nodeDevicesPaint("node1");
  t.answers["/api/passthrough/inspect"] = () => { throw new Error("host unavailable"); };
  await t.ctx.nodeDevicesLook("node1");
  assert.match(t.body.innerHTML, /Refresh failed; showing the last inspection/);
  assert.match(t.body.innerHTML, /Named GPU/);
  assert.match(t.body.innerHTML, /Refresh devices/);
});

test("inspection completing after navigation cannot overwrite another host", async () => {
  const t = setup();
  t.answers["/api/passthrough/inventory?node=node1"] = {facts: hostFacts};
  await t.ctx.nodeDevicesPaint("node1");
  let finish;
  t.answers["/api/passthrough/inspect"] = () => new Promise(resolve => { finish = resolve; });
  const reading = t.ctx.nodeDevicesLook("node1");
  await t.ctx.nodeDevicesPaint("node2");
  const html = t.body.innerHTML;
  finish(hostFacts); await reading;
  assert.equal(t.body.innerHTML, html);
});

test("VM choices and added cards show model, address, group, selector and live availability", () => {
  const t = setup();
  const inventory = {resources: [{resource: "example.test/gpu", kind: "pci", label: "Named GPU", selector: "10DE:1E87", nodes: ["node1"],
    devices: [{node: "node1", name: "Named GPU", address: "0000:01:00.0", group: 0}]}]};
  const html = t.ctx.vmDevicesPane({}, inventory);
  assert.match(html, /Named GPU \[10DE:1E87\].*node1 0000:01:00.0 · group 0/);
  t.ctx.vmAddHostDevice();
  assert.match(t.fields.added, /Named GPU/);
  assert.match(t.fields.added, /0000:01:00.0 · group 0/);
  inventory.resources[0].nodes = [];
  assert.match(t.ctx.vmDevicesPane({}, inventory), /unavailable/);
});


test("hosts with the same name in different clusters never share their cached devices", async () => {
  const t = setup();
  t.ctx.FLEET = {target: "", view: {self: "home"}};
  t.answers["/api/passthrough/inventory?node=node1"] = {facts: hostFacts};
  await t.ctx.nodeDevicesPaint("node1");
  t.ctx.FLEET.target = "branch";
  t.answers["/api/passthrough/inventory?node=node1"] = {facts: null};
  await t.ctx.nodeDevicesPaint("node1");
  assert.ok(!t.body.innerHTML.includes("Named GPU"));
  assert.match(t.body.innerHTML, /Look at its devices/);
  t.ctx.FLEET.target = "home";
  await t.ctx.nodeDevicesPaint("node1");
  assert.match(t.body.innerHTML, /Named GPU/);
  assert.equal(t.requests.length, 2);
});
