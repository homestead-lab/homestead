"use strict";

/* A host a Homestead job cordoned says which job, on its card's chip, its
   page's summary and its "Takes new apps" line; a person's own cordon, or one
   whose job list could not be read, still says only cordoned. */
const test = require("node:test"), assert = require("node:assert/strict");
const fs = require("node:fs"), vm = require("node:vm");
const between = require("./helpers/between");
const core = require("./helpers/core-source")();

function context() {
  const c = { document: { addEventListener() {}, querySelector() { return null; } }, FLEET: { view: {} }, icon: () => "",
    clusterAttr: () => "", clusterTag: () => "", fleetAll: () => false, Diagram: null, worstMetricClass: () => "",
    meter: () => "", sev: () => "", hardwareTags: () => "", remoteRow: () => "", sizePair: (used, cap) => `${used}/${cap} GB`, ratePair: (rx, tx) => [`↓${rx}`, `↑${tx}`] };
  c.window = c; vm.createContext(c);
  vm.runInContext(between(core, null, "/* Put text on the clipboard."), c);
  require("./helpers/core-text")(c);
  for (const file of ["metrics-utils.js", "ui.js", "views-overview.js"]) vm.runInContext(fs.readFileSync(`web/js/${file}`, "utf8"), c);
  return c;
}
const base = { name: "node-a", status: "Ready", schedulable: false, roles: ["worker"], cpu_pct: 12, cpu_cap: 4, mem_pct: 40,
  mem_used_gb: 6, mem_cap_gb: 16, fs_pct: 30, fs_used_gb: 30, fs_cap_gb: 100, rx_mbps: 1, tx_mbps: 1, pods: 9, disks: [],
  temps: { cpu_c: 41, max_c: 44, disks: [] }, workloads: [], cordoned_by: null };
const running = { ...base, cordoned_by: { job: "op-7", kind: "platform-upgrade", title: "Upgrade <k3s>", running: true } };
const ended = { ...base, cordoned_by: { job: "op-3", kind: "longhorn-v2-cleanup", title: "Stop Longhorn V2", running: false } };
const gone = { ...base, cordoned_by: { job: "", kind: "platform-upgrade", title: "", running: false } };
const shutdown = { ...base, cordoned_by: { job: "op-9", kind: "cluster-shutdown", title: "Shut down cluster", running: false } };
const untitled = { ...base, cordoned_by: { job: "op-4", kind: "longhorn-v2-cleanup", title: "", running: true } };

test("a running job's cordon names the job, with a way to open it", () => {
  const c = context();
  assert.equal(c.nodeCordonWords(running), "Cordoned by a job that is still running: Upgrade <k3s>.");
  const html = c.nodeCordonWords(running, true);
  assert.match(html, /^Cordoned by a job that is still running: <button [^>]*onclick="[^"]*openJob\([^)]*op-7[^)]*\)"[^>]*>Upgrade &lt;k3s&gt;<\/button>\.$/);
});

test("a job that ended and left the cordon says to check the job, then uncordon", () => {
  const c = context();
  assert.equal(c.nodeCordonWords(ended), "Cordoned by a job that has ended: Stop Longhorn V2. Check the job, then uncordon the host.");
  assert.match(c.nodeCordonWords(ended, true), /openJob\([^)]*op-3[^)]*\)"[^>]*>Stop Longhorn V2<\/button>\. Check the job/);
  assert.equal(c.nodeCordonWords(gone), "Cordoned by a job that has ended. Check the host, then uncordon it.");
  assert.doesNotMatch(c.nodeCordonWords(gone, true), /openJob/);
});

test("an ended cluster shutdown points to its own recovery, not to Uncordon", () => {
  const c = context();
  assert.equal(c.nodeCordonWords(shutdown), "Cordoned by a job that has ended: Shut down cluster. Use Uncordon hosts in the job.");
  assert.doesNotMatch(c.nodeCordonWords(shutdown, true), /uncordon (the host|it)/i, "never the page's own Uncordon");
});

test("a job with no title is never named by its kind, and still opens", () => {
  const c = context();
  assert.equal(c.nodeCordonWords(untitled), "Cordoned by a job that is still running.");
  assert.match(c.nodeCordonWords(untitled, true), /^Cordoned by a job that is still running\. <button [^>]*openJob\([^)]*op-4[^)]*\)"[^>]*>Open job<\/button>$/);
  assert.doesNotMatch(c.nodeCordonWords(untitled, true), /longhorn-v2-cleanup/);
});

test("a person's own cordon, or an unread job list, says cordoned alone", () => {
  const c = context();
  assert.equal(c.nodeCordonWords(base), "");
  assert.equal(c.nodeCordonWords({ ...running, schedulable: true }), "", "a mark on a host taking apps holds nothing");
  assert.match(c.nodePageSummary(base), />cordoned</);
  assert.doesNotMatch(c.nodePageSummary(base), /job/);
  assert.match(c.nodeCard(base), /<span class="ui-chip warn">cordoned<\/span>/, "no tip on a person's own cordon");
});

test("the card's chip and the page's summary carry the reason", () => {
  const c = context();
  assert.match(c.nodeCard(running), /data-tip="Cordoned by a job that is still running: Upgrade &lt;k3s&gt;\."[^>]*>cordoned</);
  assert.match(c.nodePageSummary(ended), /data-tip="Cordoned by a job that has ended: Stop Longhorn V2\. Check the job, then uncordon the host\."[^>]*>cordoned by a job</);
});

test("maintenance keeps the card's chip", () => {
  const c = context();
  c.hostMaintenanceChip = () => "<span>in maintenance</span>";
  assert.doesNotMatch(c.nodeCard(running), />cordoned</);
});
