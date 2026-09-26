"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");

function setup(capacity) {
  let config = { name: "demo", container_name: "demo", workload_name: "demo", target_mode: "new",
    image: "demo:1", ports: [], volumes: [] };
  let html = "";
  const sent = [], notices = [];
  const fields = { "#deployGo": {}, "#deployCapacityConfirm": { checked: false }, "#deployConfirm": { checked: false } };
  const context = { window: {}, console, URLSearchParams, Map, document: { addEventListener() {} },
    $: selector => fields[selector], esc: s => String(s).replaceAll("<", "&lt;").replaceAll(">", "&gt;"),
    api: async (path, options) => {
      if (path === "/api/preview") return { capacity, capacity_token: "signed-token", impact: {}, deployment: {}, service: null };
      sent.push(JSON.parse(options.body)); return { ok: true };
    }, modal: (_, body) => { html = body; }, toast: msg => notices.push(msg),
    volumeListIssue: () => "", closeModal() {}, go() {} };
  vm.createContext(context);
  vm.runInContext(fs.readFileSync("web/js/views-workloads.js", "utf8"), context);
  context.collect = () => config;
  return { context, fields, sent, notices, html: () => html, setConfig: c => { config = { ...config, ...c }; } };
}

const warning = { additional: 1, requires_confirmation: true, blocked: false, pod_request_gb: 1,
  pod_cpu_request_percent: 50, pod_memory_gb: 2, warnings: ["planned <volume>"], candidates: [
    { name: "host<a>", eligible: true, metrics_available: false, reservations_known: false }] };

test("deploy review shows unknown capacity and escapes warnings", async () => {
  const t = setup(warning);
  await t.context.window.doDeploy();
  assert.match(t.html(), /planned &lt;volume&gt;/);
  assert.match(t.html(), /Live RAM unavailable/);
  assert.match(t.html(), /Scheduler reservations unavailable/);
  assert.match(t.html(), /snapshot, not a reservation/);
  assert.match(t.html(), /deployCapacityConfirm/);
  assert.match(t.html(), /Proceed despite capacity warnings/);
  assert.match(t.html(), /Capacity warnings can be overridden/);
  await t.context.window.confirmDeploy();
  assert.equal(t.sent.length, 0);
});

test("confirmation sends the reviewed config, not later form edits", async () => {
  const t = setup(warning);
  await t.context.window.doDeploy();
  t.fields["#deployCapacityConfirm"].checked = true;
  t.setConfig({ image: "different:2" });
  await t.context.window.confirmDeploy();
  assert.equal(t.sent.length, 1);
  assert.equal(t.sent[0].image, "demo:1");
  assert.equal(t.sent[0].capacity_token, "signed-token");
  assert.equal(t.sent[0].confirm_capacity, true);
});

test("blocked placement cannot be submitted even with checkbox", async () => {
  const t = setup({ ...warning, blocked: true });
  await t.context.window.doDeploy();
  t.fields["#deployCapacityConfirm"].checked = true;
  await t.context.window.confirmDeploy();
  assert.equal(t.sent.length, 0);
  assert.match(t.html(), /cannot fit/);
  assert.doesNotMatch(t.html(), /id="deployCapacityConfirm"/);
  assert.match(t.html(), /placement blocker, not just a capacity warning/);
});

test("missing capacity response fails closed", async () => {
  const t = setup(null);
  await t.context.window.doDeploy();
  assert.match(t.notices.join(" "), /Capacity preview unavailable/);
  await t.context.window.confirmDeploy();
  assert.equal(t.sent.length, 0);
});

test("shared-pod review distinguishes conditional released RAM and restart consent", async () => {
  const t = setup({ ...warning, rollout: { strategy: "Recreate", replicas: 1, ownership_known: true,
    owned_pods: ["demo-pod"], release_request_gb: 1 } });
  t.setConfig({ target_mode: "existing", target_workload: "shared" });
  await t.context.window.doDeploy();
  assert.match(t.html(), /capacity after old pods stop/);
  assert.match(t.html(), /released only after termination/);
  await t.context.window.confirmDeploy();
  assert.equal(t.sent.length, 0);
  t.fields["#deployConfirm"].checked = true;
  t.fields["#deployCapacityConfirm"].checked = true;
  await t.context.window.confirmDeploy();
  assert.equal(t.sent.length, 1);
});

test("server rejection requires a fresh review rather than a blind retry", async () => {
  const t = setup(warning);
  const api = t.context.api;
  t.context.api = (path, options) => path === "/api/deploy" ? Promise.reject(new Error("review expired")) : api(path, options);
  await t.context.window.doDeploy();
  t.fields["#deployCapacityConfirm"].checked = true;
  await t.context.window.confirmDeploy();
  assert.equal(t.fields["#deployGo"].textContent, "Review again");
  assert.equal(typeof t.fields["#deployGo"].onclick, "function");
  assert.equal(t.context.window.deployReviewReady(), false);
});

test("rolling overlap shortage is distinct from a hard rollout blocker", async () => {
  const t = setup({ ...warning, rollout: { strategy: "RollingUpdate", replicas: 1, ownership_known: true,
    owned_pods: ["demo-pod"], release_request_gb: 1, max_surge: 1, max_unavailable: 1,
    overlap: { ...warning, blocked: true } } });
  t.setConfig({ target_mode: "existing", target_workload: "shared" });
  await t.context.window.doDeploy();
  assert.match(t.html(), /This overlap does not fit while old pods remain/);
  assert.doesNotMatch(t.html(), /This deployment cannot fit/);
  assert.match(t.html(), /Intermediate rollout steps remain unverified/);
});

test("shared pod cannot submit without a rollout capacity response", async () => {
  const t = setup(null);
  t.setConfig({ target_mode: "existing", target_workload: "shared" });
  await t.context.window.doDeploy();
  assert.match(t.notices.join(" "), /Capacity preview unavailable/);
  assert.equal(t.sent.length, 0);
});
