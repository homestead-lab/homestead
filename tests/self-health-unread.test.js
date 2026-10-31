"use strict";

/* Homestead's health card: backup storage, MQTT or the node probe that could
   not be read say so, never "none", a missing row or "not installed". */
const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const between = require("./helpers/between");

const source = fs.readFileSync("web/js/views-settings.js", "utf8");
const paint = between(source, "const HEALTH_TONE", "window.selfHealthPaint = selfHealthPaint;");
const BASE = { loops: [], api: { ok: true, ms: 20 }, version: "1.0.0", replicas: { desired: 1, pods: [] }, leader: true };

async function card(health) {
  const host = { innerHTML: "" };
  const ctx = {
    window: {}, STATE: { view: "settings", data: {} }, console,
    $: sel => (sel === "#selfHealthCard" ? host : null),
    api: () => Promise.resolve({ ...BASE, ...health }),
    esc: s => String(s ?? ""), can: () => true, fmtAgo: s => `${s} s`, agoText: s => `${s} s`,
    UI: { moduleHeader: title => `<h3>${title}</h3>`, callout: (tone, title, text) => `<p>${title}: ${text}</p>`,
      chip: (label, tone) => `<span class="tag ${tone}">${label}</span>` },
    setTimeout: () => 0, clearTimeout: () => { }, settingsTab: () => "other",
  };
  ctx.window = ctx;
  vm.createContext(ctx);
  vm.runInContext(`${paint}\nwindow.selfHealthPaint = selfHealthPaint;`, ctx);
  await ctx.selfHealthPaint();
  return host.innerHTML;
}

const rowOf = (html, label) => html.split('<div class="health-row">').find(r => r.includes(`<b>${label}</b>`)) || "";

test("backup storage that could not be read is unknown, with the reason, not none", async () => {
  const row = rowOf(await card({ backups: { deployed: null, ready: null, endpoint: null, error: "Backup storage could not be read: timed out" } }), "Backup storage");
  assert.match(row, /tag neutral">unknown</);
  assert.match(row, /could not be read: timed out/);
  assert.doesNotMatch(row, />none</);
  assert.doesNotMatch(row, /Set up under Data protection/);
});

test("backup storage that was read and is not there still says none", async () => {
  assert.match(rowOf(await card({ backups: { deployed: false, ready: false, endpoint: "" } }), "Backup storage"), />none</);
});

test("MQTT that could not be read keeps its row, as unknown", async () => {
  const row = rowOf(await card({ backups: {}, mqtt: { state: "unknown", error: "MQTT's state could not be read: lock" } }), "MQTT");
  assert.match(row, />unknown</);
  assert.match(row, /could not be read: lock/);
});

test("a node probe that could not be read is unknown, not not installed", async () => {
  const row = rowOf(await card({ backups: {}, probe: { installed: null, desired: 0, ready: 0, error: "The node probe could not be read: 500" } }), "Node probe");
  assert.match(row, /tag neutral">unknown</);
  assert.match(row, /could not be read: 500/);
  assert.doesNotMatch(row, /not installed/);
  assert.match(rowOf(await card({ backups: {}, probe: { installed: false } }), "Node probe"), /not installed/);
});
