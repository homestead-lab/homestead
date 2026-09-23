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
