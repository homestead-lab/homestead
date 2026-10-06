"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const state = require("../web/js/update-state.js");

test("an older cached registry report cannot replace a forced scan", () => {
  const forced = { checked_at: "2026-09-19T19:58:00Z", updates: 1 };
  const cached = { checked_at: "2026-09-19T19:57:00Z", updates: 0 };
  assert.equal(state.isStale(forced, cached), true);
  assert.equal(state.isStale(cached, forced), false);
});

test("missing or equal timestamps remain eligible", () => {
  const report = { checked_at: "2026-09-19T19:58:00Z" };
  assert.equal(state.isStale(report, report), false);
  assert.equal(state.isStale({}, report), false);
});

test("only workloads with available images can be staged", () => {
  const report = { workloads: [
    { ns: "lab", name: "plex", available: true },
    { ns: "lab", name: "frigate", available: false, images: [{ error: "registry unavailable" }] },
    { ns: "lab", name: "samba", available: true },
  ] };
  assert.deepEqual(state.availableWorkloads(report).map(item => item.name), ["plex", "samba"]);
});

test("control-plane updates are applied last without disturbing the selected order", () => {
  const selected = [
    { name: "homestead" },
    { name: "plex" },
    { name: "frigate" },
  ];
  assert.deepEqual(state.orderApply(selected).map(item => item.name), ["plex", "frigate", "homestead"]);
  assert.deepEqual(selected.map(item => item.name), ["homestead", "plex", "frigate"]);
});

test("Homestead and its helpers are updated apart from apps, Homestead first", () => {
  const report = { workloads: [
    { ns: "lab", name: "homestead-nfs", homestead: "nfs", available: true },
    { ns: "lab", name: "plex", homestead: "", available: true },
    { ns: "lab", name: "homestead", homestead: "self", available: true },
    { ns: "lab", name: "homestead-objectstore", homestead: "objectstore", available: false },
  ] };
  assert.deepEqual(state.availableWorkloads(report).map(item => item.name), ["plex"]);
  assert.deepEqual(state.homesteadWorkloads(report).map(item => item.name), ["homestead", "homestead-nfs", "homestead-objectstore"]);
  assert.deepEqual(state.orderApply(state.homesteadWorkloads(report).filter(w => w.available)).map(item => item.name),
    ["homestead-nfs", "homestead"]);
});

test("an app something else updates keeps its notice but is not offered for update (#296)", () => {
  const report = { workloads: [
    { ns: "lab", name: "loki", available: true, managed: { by: "Flux", source: "Kustomization flux-system/apps", detected: true } },
    { ns: "lab", name: "radarr", available: true, managed: { by: "Renovate", source: "", detected: false } },
    { ns: "lab", name: "sonarr", available: true, managed: {} },
  ] };
  assert.deepEqual(state.availableWorkloads(report).map(w => w.name), ["sonarr"]);
});
