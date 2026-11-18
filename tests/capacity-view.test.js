"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const between = require("./helpers/between");

async function review(plan) {
  let html = "";
  const context = { window: {}, console, URLSearchParams, Map, $: () => null, document: { addEventListener() {} },
    api: async () => plan, modal: (_, body) => { html = body; }, toast: message => { throw new Error(message); },
    esc: value => String(value).replaceAll("<", "&lt;").replaceAll(">", "&gt;") };
  vm.createContext(context);
  context.jsArg = s => JSON.stringify(String(s ?? "")); context.jsq = s => (context.esc || String)(context.jsArg(s));
  vm.runInContext(fs.readFileSync("web/js/ui.js", "utf8"), context);
  vm.runInContext(require("./helpers/workloads-source")(), context);
  await context.window.wlScale("lab", "app", 3);
  return html;
}

test("insufficient replica slots cannot be overridden by Start anyway", async () => {
  const html = await review({ blocked: true, additional: 3, warnings: ["3 replicas exceed 2 slots"],
    pod_request_gb: 1, pod_memory_gb: 2, pod_cpu_request_percent: 50, candidates: [{ name: "node", eligible: true,
      metrics_available: true, projected_percent: 60, used_gb: 1, projected_gb: 3, capacity_gb: 5,
      reservations_known: true, reserved_gb: 2, allocatable_gb: 4, reserved_cpu_percent: 100, request_slots: 2 }] });
  assert.match(html, /3 replicas exceed 2 slots/);
  assert.match(html, /already reserved 2 of 4 GiB/);
  assert.match(html, /50% CPU/);
  assert.match(html, /It is a snapshot and does not reserve anything/);
  assert.doesNotMatch(html, /Start anyway/);
});

test("unknown reservations are presented as unknown with explicit review", async () => {
  const html = await review({ requires_confirmation: true, additional: 1, warnings: ["unknown <state>"],
    candidates: [{ name: "node", eligible: true, reservations_known: false, metrics_available: false }] });
  assert.match(html, /reservations unavailable/);
  assert.match(html, /Memory use unknown/);
  assert.match(html, /unknown &lt;state&gt;/);
  assert.match(html, /wl_capacity_ok/);
  assert.match(html, /Start it anyway/);
});

test("topology conflicts identify the next pod without claiming all resource slots fit", async () => {
  const html = await review({ blocked: true, additional: 3, topology_status: "blocked", warnings: [],
    candidates: [{ name: "host<1>", eligible: false, reasons: ["required pod anti-affinity conflicts in zone=<west>"] }] });
  assert.match(html, /do not allow all its copies to fit/);
  assert.match(html, /Cannot run here: required pod anti-affinity/);
  assert.match(html, /zone=&lt;west&gt;/);
  assert.doesNotMatch(html, /Start anyway/);
});

test("a VM blocked only by memory says so in one line, and its routine notes wait under Details", async () => {
  const html = await review({ blocked: true, additional: 1, topology_status: "blocked", pod_request_gb: 23.08, unbounded: ["vm-launcher-estimate"],
    warnings: ["LAN network default/lan exists. The host bridge, plugin and physical link must also be ready.",
      "The VM's state is kept on volume persistent-state-for-win-dev. Keep it with the VM's disks when moving or recovering this VM.",
      "no ready host satisfies this workload's placement requirements",
      "no scheduling order fits all requested replicas. The observed pod affinity and topology spread rules rule it out",
      "Power requests address the VM by name. Identity is rechecked immediately before sending, but this is not an atomic scheduler reservation or a cross-resource transaction."],
    candidates: [{ name: "k3s-test", eligible: false, reasons: ["memory request exceeds remaining scheduler capacity"], allocatable_gb: 30.6, reserved_gb: 22.4 }] });
  const notice = between(html, 'class="ui-lead"', "<section");
  assert.match(notice, /until the problem below is resolved/);
  assert.match(notice, /ui-notice-first[^>]*>No host has enough memory left for it\. It reserves 23\.08 GiB\./);
  assert.doesNotMatch(notice, /LAN network|state is kept|Power requests|no ready host|no scheduling order|spread rules|vm-launcher-estimate/);
  assert.match(html, /<h4>Hosts<\/h4>/, "no host has room, so not Hosts with room");
  assert.match(html, /not enough memory: 8\.2 of 30\.6 GiB is left, and it reserves 23\.08 GiB/);
  const details = between(html, "<summary>Details", "</details>");
  assert.match(details, /LAN network default\/lan exists/);
  assert.match(details, /Power requests address the VM by name/);
});

test("inconclusive topology remains an explicit warning", async () => {
  const html = await review({ requires_confirmation: true, additional: 2, topology_status: "unknown", candidates: [] });
  assert.match(html, /placement and spread rules have not been checked/);
  assert.match(html, /wl_capacity_ok/);
});

test("start review distinguishes required preferred and scheduler-selected launch hosts", async () => {
  const candidates = [{ name: "host<a>", eligible: true, projected_pods: 1 }, { name: "host-b", eligible: true, projected_pods: 1 }];
  const base = { requires_confirmation: true, additional: 1, candidates, warnings: [] };
  const pinned = await review({ ...base, placement: { pinned: "host<a>" } });
  assert.match(pinned, /<p class="ui-lead">Starts it on <b>host&lt;a&gt;<\/b>\./);
  assert.match(pinned, /ui-chip info">required/);
  const preferred = await review({ ...base, placement: { preferred: "host<a>" } });
  assert.match(preferred, /on <b>host&lt;a&gt;<\/b> if it can, or another host/);
  const automatic = await review(base);
  assert.match(automatic, /on one of 2 hosts/);
  assert.match(automatic, /host&lt;a&gt;[^]*host-b/);
  assert.match(await review({ ...base, candidates: [candidates[0]] }), /on <b>host&lt;a&gt;<\/b>, the only host that can run it/);
});

test("unavailable preference and blocked launches do not promise a host", async () => {
  const html = await review({ requires_confirmation: true, blocked: true, additional: 1, warnings: [],
    placement: { preferred: "offline" }, candidates: [{ name: "offline", eligible: false }] });
  assert.match(html, /cannot start until/);
  assert.match(html, /Cannot run here/);
  assert.doesNotMatch(html, /on <b>offline/);
  assert.doesNotMatch(html, /Start anyway/);
});
