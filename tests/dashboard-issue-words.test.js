"use strict";

/* The Dashboard's summary names each health issue in a few words, as the
   mockup does ("paperless is down"), with the full reason as the link's tip. */
const test = require("node:test"), assert = require("node:assert/strict");
const fs = require("node:fs"), vm = require("node:vm");
const between = require("./helpers/between");
const core = require("./helpers/core-source")();

function context() {
  const c = { document: { addEventListener() {}, querySelector() { return null; } }, FLEET: { view: {} }, icon: () => "",
    clusterAttr: () => "", clusterTag: () => "", fleetAll: () => false, Diagram: null, HOVER_CARDS: {},
    meter: () => "", sev: () => "", hardwareTags: () => "", remoteRow: () => "" };
  c.window = c; vm.createContext(c);
  vm.runInContext(between(core, null, "/* Put text on the clipboard."), c);
  require("./helpers/core-text")(c);
  for (const file of ["metrics-utils.js", "ui.js", "views-overview.js"]) vm.runInContext(fs.readFileSync(`web/js/${file}`, "utf8"), c);
  return c;
}

test("each kind of health issue the server reports reads in a few words", () => {
  const c = context(), words = (kind, name, reason) => c.dashboardIssueWords({ kind, name, reason });
  assert.equal(words("Node", "harvester-node3", "Kubernetes reports NotReady. Review this host."), "harvester-node3 is not ready");
  assert.equal(words("Disk", "harvester-node2/sda", "24 reallocated sectors"), "sda on harvester-node2 has a drive warning");
  assert.equal(words("Volume", "nextcloud-data", "Longhorn is rebuilding or missing a replica"), "nextcloud-data is missing a copy");
  assert.equal(words("Volume", "nextcloud-data", "Longhorn reports the volume faulted"), "nextcloud-data is faulted");
  assert.equal(words("Volume", "nextcloud-data", "still rebuilding a copy after 2h (40% done)"), "nextcloud-data is still rebuilding a copy");
  assert.equal(words("Workload", "lab/paperless", "only 0/1 replicas ready, not ready for 12m"), "paperless is down");
  assert.equal(words("Workload", "lab/edge-tunnel", "only 2/3 replicas ready, not ready for 12m"), "edge-tunnel has 2 of 3 copies ready");
  assert.equal(words("Trim", "frigate-config", "Weekly trim failed: engine is not running."), "frigate-config's weekly trim failed");
  assert.equal(words("Backup", "nightly", "The last backup run failed. Review its job log."), "nightly's last backup failed");
  assert.equal(words("Backup", "hourly", "The last snapshot run failed. Review its job log."), "hourly's last snapshot failed");
  assert.equal(words("Backup", "backup target", "Backup jobs have no backup target. Configure a destination."), "Backups have no target");
  assert.equal(words("Backup", "backup target", "s3://example cannot be reached"), "The backup target cannot be reached");
  // A reason it does not know: as it is when short, "needs attention" when long.
  assert.equal(words("Workload", "lab/app", "is waiting for its image"), "app is waiting for its image");
  assert.equal(words("Workload", "lab/app", "Back-off restarting failed container app in pod app-7d9c, exit code 137 after an out-of-memory kill"), "app needs attention");
});

test("the summary links each issue with its full reason as the tip", () => {
  const c = context();
  const line = c.dashboardSummary({ nodes_total: 3, health_issues: [
    { kind: "Workload", name: "lab/paperless", severity: "degraded", reason: "only 0/1 replicas ready, not ready for 12m" },
    { kind: "Node", name: "harvester-node3", severity: "critical", reason: "Kubernetes reports NotReady. Review this host." }] });
  assert.match(line, /2 items need attention\./);
  assert.match(line, /paperless is down/);
  assert.match(line, /only 0\/1 replicas ready, not ready for 12m/);
  assert.match(line, /harvester-node3 is not ready/);
  assert.doesNotMatch(line, /needs attention/);
});
