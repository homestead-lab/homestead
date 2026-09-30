"use strict";
const test = require("node:test"), assert = require("node:assert/strict"), fs = require("node:fs"), vm = require("node:vm");

function setup(admin = true, confirm = true) {
  const calls = [], toasts = [];
  const ctx = { console, STATE: { data: {} }, $: () => null, $$: () => [], esc: String, can: () => admin,
    ask: async () => confirm, toast: (m, k) => toasts.push([m, k]), addonsPaint: () => {},
    api: async (path, opts) => { calls.push({ path, body: opts ? JSON.parse(opts.body) : null }); return { detail: "being installed" }; } };
  ctx.window = ctx; ctx.jsq = JSON.stringify; vm.createContext(ctx);
  vm.runInContext(fs.readFileSync("web/js/platform.js", "utf8"), ctx);
  ctx.addonsPaint = () => {};
  return { ctx, calls, toasts };
}

const state = (over = {}) => ({ version: "2.8.247", enabled: true, hosts: 2, installed: 2, current: 1, settled: false, harvester: false,
  nodes: [{ name: "node1", ready: true, enabled: true, detail: "Installed 2.8.246; update available" },
          { name: "node2", ready: true, enabled: true, current: true, detail: "Installed 2.8.247; matches this release" },
          { name: "native", ready: true, native: true }], ...over });

test("the host console is one add-on row: its state, each host, one switch", () => {
  const { ctx } = setup();
  const html = ctx.hostConsoleRow(state());
  assert.match(html, /Host console/);
  assert.match(html, /1 of 2 up to date/);
  assert.match(html, /node1.*update available/s);
  assert.doesNotMatch(html, /native/, "Harvester's own console is not a host to manage");
  assert.match(html, /onchange="hostConsoleSet\(this\)"/);
  assert.match(ctx.hostConsoleRow(state({ settled: true, current: 2 })), /pill ok">installed/);
  assert.match(ctx.hostConsoleRow(state({ enabled: false, installed: 1 })), /removing · 1 left/);
  assert.equal(ctx.hostConsoleRow(state({ harvester: true })), "");
  assert.equal(ctx.hostConsoleRow(null), "", "not an administrator, or not readable: no row");
});

test("the switch sets the add-on for every host, and asks before removing it", async () => {
  const on = setup();
  await on.ctx.hostConsoleSet({ checked: true });
  assert.deepEqual(on.calls[0], { path: "/api/host-console", body: { enabled: true } });
  const declined = setup(true, false), input = { checked: false };
  await declined.ctx.hostConsoleSet(input);
  assert.equal(declined.calls.length, 0);
  assert.equal(input.checked, true, "left on");
  const off = setup();
  await off.ctx.hostConsoleSet({ checked: false });
  assert.deepEqual(off.calls[0].body, { enabled: false });
});

test("a viewer sees the row without the switch", () => {
  const { ctx } = setup(false);
  assert.doesNotMatch(ctx.hostConsoleRow(state()), /hostConsoleSet/);
});
