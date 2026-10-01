"use strict";
const test = require("node:test"), assert = require("node:assert/strict");
const fs = require("node:fs"), vm = require("node:vm");
async function fixture(scenario = "", live = false) {
  const ctx = { URL, URLSearchParams, Response, Date, console,
    location: { search: live ? scenario ? `?demo-scenario=${scenario}` : "" : `?demo=1${scenario ? `&demo-scenario=${scenario}` : ""}`, origin: "https://demo.example.com" },
    HOMESTEAD_DEMO: live, document: { body: null, addEventListener() {} },
    fetch: async () => { throw new Error("demo must never reach a real cluster"); } };
  ctx.window = ctx;
  vm.createContext(ctx);
  vm.runInContext(fs.readFileSync("web/js/demo.js", "utf8"), ctx);
  return async (path, body) => (await ctx.fetch(path, body ? { method: "POST", body: JSON.stringify(body) } : undefined)).json();
}
test("public and local demos start with consistent healthy cluster data", async () => {
  for (const live of [false, true]) {
    const api = await fixture("", live);
    const [overview, cluster, setup, storage, volumes, capacity, workloads, operations, events, alerts] = await Promise.all([
      "/api/overview", "/api/cluster", "/api/setup", "/api/storage", "/api/volumes", "/api/longhorn/capacity", "/api/workloads", "/api/operations", "/api/events", "/api/alerts",
    ].map(path => api(path)));
    assert.equal(overview.health, "healthy");
    assert.deepEqual(overview.health_issues, []);
    assert.equal(cluster.state, "healthy");
    assert.equal(cluster.control_plane.etcd_total, 3);
    assert.equal(cluster.nodes.filter(row => row.roles.includes("etcd")).length, 3);
    assert.equal(setup.steps.health.done, true);
    assert.equal(setup.steps.probe, undefined);
    assert.equal(storage.degraded, 0);
    assert.equal(storage.healthy, volumes.filter(row => row.robustness === "healthy").length);
    assert.ok(volumes.every(row => row.state !== "attached" || (row.robustness === "healthy" && row.copies.length === row.replicas)));
    assert.ok(capacity.nodes.every(row => row.level === "ok"));
    assert.ok(workloads.every(row => row.ready === row.desired));
    const imageUpdates = await api("/api/image-updates");
    assert.equal(imageUpdates.errors, 0);
    assert.ok(imageUpdates.workloads.every(row => row.images.every(image => !image.error)));
    assert.equal(imageUpdates.updates, 2, "available updates are compatible with healthy checks");
    assert.ok(operations.every(row => row.status !== "failed"));
    assert.ok(events.every(row => row.type !== "Warning"));
    assert.deepEqual(alerts.active, []);
    await api("/api/setup/complete", { completed: true });
    assert.equal((await api("/api/setup")).completed, true);
  }
});
test("explicit incident and critical scenarios retain unhealthy evaluation cases", async () => {
  for (const scenario of ["incidents", "critical"]) {
    const api = await fixture(scenario, true);
    const overview = await api("/api/overview"), setup = await api("/api/setup");
    assert.equal(overview.health, scenario === "critical" ? "critical" : "degraded");
    assert.ok(overview.health_issues.length > 0);
    assert.equal(setup.steps.health.done, false);
    assert.ok((await api("/api/storage")).degraded > 0);
    assert.ok((await api("/api/events")).some(row => row.type === "Warning"));
    assert.ok((await api("/api/operations")).some(row => row.status === "failed"));
    const imageUpdates = await api("/api/image-updates");
    assert.equal(imageUpdates.errors, 1);
    assert.ok(imageUpdates.workloads.some(row => row.images.some(image => image.error)));
    if (scenario === "critical") assert.equal(overview.nodes_ready, 2);
  }
});
