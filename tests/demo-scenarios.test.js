"use strict";
const test = require("node:test"), assert = require("node:assert/strict");
const fs = require("node:fs"), vm = require("node:vm");
async function fixture(scenario = "", live = false, clock = Date) {
  const ctx = { URL, URLSearchParams, Response, Date: clock, console,
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
    const vms = await api("/api/vms");
    assert.ok(vms.every(row => ["Running", "Stopped"].includes(row.status) && !row.problem && !row.restart_required));
    for (const v of vms) {
      const detail = await api(`/api/vm?ns=${v.ns}&name=${v.name}`);
      assert.equal(detail.status, v.status);
      assert.equal(detail.problem, "");
      assert.ok(detail.disks.every(d => d.made));
      assert.ok(detail.conditions.every(c => !c.reason && !c.message));
      if (!v.running) {
        assert.equal(v.run_strategy, "Halted");
        assert.deepEqual(v.actions, ["start"]);
      }
    }
    const stopped = vms.find(v => v.name === "ubuntu-test");
    assert.equal(stopped.cores, 2);
    assert.equal(stopped.memory, "4Gi");
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
    const broken = await api("/api/vm?ns=lab&name=ubuntu-test");
    assert.equal(broken.status, "ErrorUnschedulable");
    assert.match(broken.problem, /Insufficient memory/);
    assert.equal(broken.disks[0].made, false);
    if (scenario === "critical") assert.equal(overview.nodes_ready, 2);
  }
});

test("disk preparation demo mutations return concrete saved tasks without contacting a cluster", async () => {
  const api = await fixture("", true);
  const plan = await api("/api/disks/v2/plan", {node:"node-1",disk:"data"});
  assert.equal(plan.device,"/dev/sdb");
  const started = await api("/api/disks/v2/start", {capacity_token:plan.capacity_token});
  assert.equal(started.id,"demo-disk-v2");assert.equal(started.phase,"evacuating");
  const prepared = await api("/api/disks/v2/prepare", {operation_id:started.id});
  assert.equal(prepared.id,started.id);assert.equal(prepared.phase,"preparing");
  assert.equal((await api("/api/disks/v2/status?id="+started.id)).phase,"preparing");
});

test("demo long-term history covers each full range with gentle, bounded trends and accurate peaks", async () => {
  for (const live of [false, true]) {
    const api = await fixture("", live);
    for (const [range, span, step] of [["24h", 86400, 300], ["7d", 7 * 86400, 3600],
      ["30d", 30 * 86400, 3600], ["90d", 90 * 86400, 3600]]) {
      const h = await api(`/api/history/long?range=${range}`);
      assert.equal(h.range, range); assert.equal(h.step, step);
      assert.equal(h.samples, span / step);
      assert.equal(h.t.at(-1) - h.t[0] + step, span);
      assert.ok(h.t.every((t, i) => t % step === 0 && (!i || t - h.t[i - 1] === step)));
      for (const field of ["cpu", "mem", "rx", "tx", "pods", "vol_bad", "nodes_ready", "nodes_total"])
        assert.equal(h[field].length, h.samples);
      for (const field of ["cpu", "mem", "rx", "tx"]) {
        const values = h[field], extent = Math.max(...values) - Math.min(...values);
        const change = Math.max(...values.slice(1).map((value, i) => Math.abs(value - values[i])));
        assert.ok(values.every(value => Number.isFinite(value) && value >= 0));
        assert.ok(change < extent * 0.1, `${range} ${field} should not jump between adjacent samples`);
      }
      assert.ok(h.cpu.every(v => v <= 100)); assert.ok(h.mem.every(v => v <= 100));
      assert.equal(h.cpu_max, Math.max(...h.cpu)); assert.equal(h.mem_max, Math.max(...h.mem));
      assert.ok(h.vol_bad.every(v => v === 0));
    }
    assert.equal((await api("/api/history/long?range=unknown")).range, "24h");
  }
});

test("demo history refresh preserves existing buckets and only advances at the sampling cadence", async () => {
  let now = Date.UTC(2026, 9, 3, 12);
  class Clock extends Date { static now() { return now; } }
  const api = await fixture("", true, Clock);
  for (const range of ["24h", "7d", "30d", "90d"]) {
    const before = await api(`/api/history/long?range=${range}`);
    now += 1000;
    assert.deepEqual(await api(`/api/history/long?range=${range}`), before);
    now += before.step * 1000;
    const after = await api(`/api/history/long?range=${range}`);
    assert.deepEqual(after.t.slice(0, -1), before.t.slice(1));
    for (const field of ["cpu", "mem", "rx", "tx"])
      assert.deepEqual(after[field].slice(0, -1), before[field].slice(1));
  }
});
