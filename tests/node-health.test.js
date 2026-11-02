"use strict";

/* A host's state is worked out once and said the same everywhere, and a host
   that isn't answering shows no current-looking readings. */
const test = require("node:test"), assert = require("node:assert/strict");
const fs = require("node:fs"), vm = require("node:vm");
const between = require("./helpers/between");
const core = require("./helpers/core-source")();

function context() {
  const c = { document: { addEventListener() {}, querySelector() { return null; } }, FLEET: { view: {} }, icon: () => "",
    clusterAttr: () => "", clusterTag: () => "", fleetAll: () => false, Diagram: null,
    meter: () => "", sev: () => "", hardwareTags: () => "", remoteRow: () => "", sizePair: (used, cap) => `${used}/${cap} GB`, ratePair: (rx, tx) => [`↓${rx}`, `↑${tx}`] };
  c.window = c; vm.createContext(c);
  vm.runInContext(between(core, null, "/* Put text on the clipboard."), c);
  require("./helpers/core-text")(c);
  for (const file of ["metrics-utils.js", "ui.js", "views-overview.js"]) vm.runInContext(fs.readFileSync(`web/js/${file}`, "utf8"), c);
  return c;
}
const base = { name: "host-2", status: "Ready", schedulable: true, roles: ["worker"], cpu_pct: 12, cpu_cap: 4, mem_pct: 40,
  mem_used_gb: 6, mem_cap_gb: 16, fs_pct: 30, fs_used_gb: 30, fs_cap_gb: 100, rx_mbps: 1, tx_mbps: 1, pods: 9, disks: [],
  temps: { cpu_c: 41, max_c: 44, disks: [] }, workloads: [] };
const failing = { ...base, temps: { ...base.temps, disks: [{ name: "sda", health: { state: "attention", summary: "24 reallocated sectors" } }] } };
const down = { ...base, name: "host-3", status: "NotReady", cpu_pct: 0, mem_pct: 0, mem_used_gb: 0, fs_pct: undefined,
  fs_used_gb: undefined, fs_cap_gb: undefined, rx_mbps: undefined, tx_mbps: undefined, temps: { disks: [] } };

test("nodeHealth works out a host's state once", () => {
  const c = context();
  assert.deepEqual(JSON.parse(JSON.stringify(c.nodeHealth(base))), { tone: "ok", status: "Ready", issues: [], ready: true });
  const drive = c.nodeHealth(failing);
  assert.equal(drive.status, "Drive warning"); assert.equal(drive.tone, "warn");
  assert.deepEqual([...drive.issues], ["sda: 24 reallocated sectors"]);
  assert.equal(c.nodeHealth(down).status, "Not ready"); assert.equal(c.nodeHealth(down).tone, "bad");
  assert.equal(c.nodeHealth({ ...base, schedulable: false }).status, "Cordoned");
});

test("Nodes and the node's page say Drive warning for a Ready host with a failing drive", () => {
  const c = context();
  const status = vm.runInContext("nodeComparisonRows", c)([failing]).find(row => row.label === "Status").values[0];
  assert.match(status, /Drive warning/);
  assert.match(c.nodePageSummary(failing), /Drive warning/);
});

test("a host that isn't Ready shows no readings, only dashes", () => {
  const c = context();
  const summary = c.nodePageSummary(down);
  assert.doesNotMatch(summary, /undefined|\b0%/);
  assert.match(summary, /CPU <b>—<\/b>/);
  const rows = vm.runInContext("nodeComparisonRows", c)([down]);
  for (const label of ["CPU", "Memory", "Network", "CPU temp"]) assert.doesNotMatch(rows.find(row => row.label === label).values[0], /\b0%|undefined|°C/, label);
  const card = c.dashboardNodes([down], "compact");
  assert.doesNotMatch(card, /<b class="mono">0%<\/b>/);
});

test("the node's Storage line counts drive warnings, and Overview has one callout to Health", () => {
  const c = context();
  assert.equal(c.nodeStorageLine(failing, 2, 0), "2 drives · 1 warning");
  assert.equal(c.nodeStorageLine(base, 2, 1), "2 drives · 1 unused");
  const callout = c.nodeAttention(failing);
  assert.equal((callout.match(/class="ui-callout /g) || []).length, 1);
  assert.match(callout, /sda: 24 reallocated sectors/);
  assert.match(callout, /HealthInsights\.open\(&#39;health&#39;\)|HealthInsights\.open\('health'\)/);
  assert.match(c.nodeAttention(down), /ui-callout bad[\s\S]*not answering/);
  assert.equal(c.nodeAttention(base), "");
});

const warm = { ...base, temps: { cpu_c: 39, max_c: 51, max_source: "NVMe nvme0", disks: [] } };

test("nodeTemp gives the CPU temperature, and the hottest sensor when it runs hotter", () => {
  const c = context();
  const t = c.nodeTemp(warm);
  assert.equal(t.label, "CPU temp"); assert.equal(t.value, 39); assert.equal(t.extra, "max 51°C, NVMe nvme0");
  assert.equal(c.nodeTemp({ ...base, temps: { cpu_c: 45, max_c: 45, disks: [] } }).extra, "");
  assert.equal(c.nodeTemp({ ...base, temps: { max_c: 50, disks: [] } }).label, "Max temp");
  assert.equal(c.nodeTemp({ ...base, temps: { disks: [] } }).value, null);
});

test("the dashboard, Nodes and the node's page give the same temperature and disk labels", () => {
  const c = context();
  const card = c.dashboardNodes([warm], "compact"), summary = c.nodePageSummary(warm);
  const rows = vm.runInContext("nodeComparisonRows", c)([warm]);
  assert.match(card, /CPU temp[\s\S]*39°C/); assert.doesNotMatch(card, /Max temp/);
  assert.ok(rows.some(row => row.label === "CPU temp" && /39°C/.test(row.values[0])), rows.map(r => r.label).join());
  assert.match(summary, /CPU temp <b[^>]*>39°C<\/b>/); assert.doesNotMatch(summary, /CPU <b[^>]*>39°C/);
  assert.match(card, /Node filesystem/); assert.match(summary, /Node filesystem/);
  assert.doesNotMatch(summary, /Disk <b>/);
});

test("shapes a real probe reports: a hottest sensor that is the CPU itself adds nothing", () => {
  const c = context();
  // As a Raspberry Pi's probe reports it: the hottest sensor is the SoC's ADC, at the CPU's temperature.
  assert.deepEqual(JSON.parse(JSON.stringify(c.nodeTemp({ ...base, temps: { cpu_c: 52.6, max_c: 52.6, max_source: "rp1_adc", disks: [] } }))),
    { value: 52.6, label: "CPU temp", text: "52.6°C", extra: "" });
  // A drive running hotter than the CPU is named beside it.
  assert.equal(c.nodeTemp({ ...base, temps: { cpu_c: 30, max_c: 37.9, max_source: "NVMe nvme0", disks: [] } }).extra, "max 37.9°C, NVMe nvme0");
});

test("a host's address chips keep a space between them (#543: .ui-chip has no margin)", () => {
  const c = context();
  const host = { ...base, addresses: { InternalIP: "192.0.2.7" }, duties: { vips: ["192.0.2.14", "192.0.2.15"] } };
  const html = vm.runInContext("nodeAddressTags", c)(host);
  assert.equal((html.match(/class="ui-chip /g) || []).length, 3);
  assert.doesNotMatch(html, /<\/span><\/span><span/, "two address chips touch");
});
