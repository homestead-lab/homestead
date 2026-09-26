"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");

async function review(plan) {
  let html = "";
  const context = { window: {}, console, URLSearchParams, Map, $: () => null, document: { addEventListener() {} },
    api: async () => plan, modal: (_, body) => { html = body; }, toast: message => { throw new Error(message); },
    esc: value => String(value).replaceAll("<", "&lt;").replaceAll(">", "&gt;") };
  vm.createContext(context);
  vm.runInContext(fs.readFileSync("web/js/views-workloads.js", "utf8"), context);
  await context.window.wlScale("lab", "app", 3);
  return html;
}

test("insufficient replica slots cannot be overridden by Start anyway", async () => {
  const html = await review({ blocked: true, additional: 3, warnings: ["3 replicas exceed 2 slots"],
    pod_request_gb: 1, pod_memory_gb: 2, pod_cpu_request_percent: 50, candidates: [{ name: "node", eligible: true,
      metrics_available: true, projected_percent: 60, used_gb: 1, projected_gb: 3, capacity_gb: 5,
      reservations_known: true, reserved_gb: 2, allocatable_gb: 4, reserved_cpu_percent: 100, request_slots: 2 }] });
  assert.match(html, /3 replicas exceed 2 slots/);
  assert.match(html, /Already reserved: 2 \/ 4 GiB/);
  assert.match(html, /50% CPU/);
  assert.match(html, /snapshot, not a reservation/);
  assert.doesNotMatch(html, /Start anyway/);
});

test("unknown reservations are presented as unknown with explicit review", async () => {
  const html = await review({ requires_confirmation: true, additional: 1, warnings: ["unknown <state>"],
    candidates: [{ name: "node", eligible: true, reservations_known: false, metrics_available: false }] });
  assert.match(html, /Scheduler reservations unavailable/);
  assert.match(html, /live RAM unavailable/);
  assert.match(html, /unknown &lt;state&gt;/);
  assert.match(html, /wl_capacity_ok/);
});
