"use strict";
const test = require("node:test"), assert = require("node:assert/strict");
const fs = require("node:fs"), vm = require("node:vm");

function load() {
  const actions = [], callouts = [];
  const ctx = { STATE: { data: {} }, esc: v => String(v ?? "").replace(/[&<>"']/g, c => `&#${c.charCodeAt(0)};`) };
  ctx.window = ctx;
  vm.createContext(ctx);
  // The real UI.chip draws the status chips; the rest of UI is stubbed below.
  vm.runInContext(fs.readFileSync("web/js/ui.js", "utf8"), ctx);
  Object.assign(ctx, { fmtAgo: s => `${Math.round(s)}s ago`,
    serviceRow: (title, pill, detail, bar) => `<row>${title}|${pill}|${detail}|${bar}</row>`,
    actionBar: rows => { const kept = rows.filter(Boolean); actions.push(...kept); return kept.map(r => `[${r.label}]`).join(""); } });
  Object.assign(ctx.UI, { moduleHeader: (t, s) => `<h>${t}</h><p>${s}</p>`, facts: rows => rows.map(([k, v]) => `<f>${k}: ${v}</f>`).join(""),
    callout: (tone, title, body) => { callouts.push([tone, title]); return `<c ${tone}>${title}: ${body}</c>`; } });
  vm.runInContext(fs.readFileSync("web/js/views-stats.js", "utf8"), ctx);
  return { ctx, actions, callouts };
}

test("MQTT sits under Connections, not Monitoring", () => {
  const source = fs.readFileSync("web/js/views-settings.js", "utf8");
  const connections = source.match(/\["connections", "Connections", "([^"]*)", \[([^\]]*)\]/);
  const monitoring = source.match(/\["monitoring", "Monitoring", "([^"]*)", \[([^\]]*)\]/);
  assert.match(connections[1], /MQTT/);
  assert.match(connections[2], /"mqtt"/);
  assert.doesNotMatch(monitoring[1] + monitoring[2], /mqtt/i);
  assert.match(fs.readFileSync("web/style.css", "utf8"), /\.settings-grid\[data-tab="connections"\]>section\[data-tab="mqtt"\]/);
});

test("the card is laid out as UniFi is: the broker, its facts, then power control", () => {
  const { ctx, actions, callouts } = load();
  const html = ctx.mqttCardHtml({ enabled: true, host: "192.0.2.177", port: 1883, tls: false, username: "", base: "harvester",
    discovery: "homeassistant", interval: 60, sensors: { cluster: 15, node: 9 }, control: true, controlled: ["vm:vms/win11", "app:lab/frigate"],
    status: { state: "publishing", last_publish: 980 } }, 1000);
  assert.match(html, /MQTT broker\|<span class="ui-chip ok">publishing/);
  assert.match(html, /Broker: <span class="mono">192\.0\.2\.177:1883/);
  assert.match(html, /Power control from Home Assistant\|<span class="ui-chip ok">on · 2/);
  assert.deepEqual(actions.map(a => a.label).sort(), ["Choose apps", "Edit settings", "Turn off", "What is published"]);
  assert.deepEqual(actions.filter(a => a.need).map(a => a.need), ["admin", "admin", "admin"]);
  // control on a broker with no sign-in and no TLS: a warning
  assert.deepEqual(callouts, [["warn", "Anyone who can publish to this broker can control these"]]);
});

test("with no broker it offers to connect one, and no power control yet", () => {
  const { ctx, actions } = load();
  const html = ctx.mqttCardHtml({ enabled: false, host: "", sensors: { cluster: 15, node: 9 }, status: { state: "off" } });
  assert.match(html, /No broker\|<span class="ui-chip neutral">not connected/);
  assert.doesNotMatch(html, /Power control/);
  assert.deepEqual(actions.map(a => a.label), ["Connect MQTT"]);
});

test("the chooser lists VMs and containers, and what cannot be picked says why", () => {
  const { ctx } = load();
  const html = ctx.mqttControlList([
    { id: "app:lab/homestead", kind: "app", ns: "lab", name: "homestead", refused: "it is part of Homestead" },
    { id: "vm:vms/win11", kind: "vm", ns: "vms", name: "win11", refused: "" },
    { id: "app:lab/frigate", kind: "app", ns: "lab", name: "frigate", refused: "" }], new Set(["vm:vms/win11", "app:lab/homestead"]));
  assert.ok(html.indexOf("Virtual machines") < html.indexOf("Containers"));
  assert.match(html, /value="vm:vms\/win11" checked/);
  assert.match(html, /value="app:lab\/homestead" {2}disabled>/);
  assert.match(html, /it is part of Homestead/);
  assert.match(ctx.mqttControlList([], new Set()), /There are no containers or VMs yet/);
});
