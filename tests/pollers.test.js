"use strict";
// The page's own repeating reads, on UI.poll (#909): the main refresh loop, the
// alert checks and the 15-minute image-update check. Each stops when told to,
// reads once (not twice) when the tab comes back, and the image check reads
// nothing at all on tab return.
const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const between = require("./helpers/between");

function fakeClock() {
  let now = 0, seq = 0;
  const timers = new Map();
  const settle = async () => { for (let i = 0; i < 20; i++) await Promise.resolve(); };
  return {
    setTimeout: (fn, ms = 0) => { const id = ++seq; timers.set(id, { at: now + ms, fn }); return id; },
    clearTimeout: id => { timers.delete(id); },
    pending: () => timers.size,
    async advance(ms) {
      const end = now + ms;
      await settle();
      for (;;) {
        const due = [...timers.entries()].filter(([, t]) => t.at <= end).sort((a, b) => a[1].at - b[1].at)[0];
        if (!due) break;
        timers.delete(due[0]); now = due[1].at; due[1].fn(); await settle();
      }
      now = end; await settle();
    },
    settle,
  };
}
function fakeDocument() {
  const listeners = new Map();
  return {
    visibilityState: "visible", hidden: false,
    addEventListener(type, fn) { if (!listeners.has(type)) listeners.set(type, new Set()); listeners.get(type).add(fn); },
    removeEventListener(type, fn) { listeners.get(type)?.delete(fn); },
    listening: type => listeners.get(type)?.size || 0,
    show(visible) { this.visibilityState = visible ? "visible" : "hidden"; this.hidden = !visible; for (const fn of [...(listeners.get("visibilitychange") || [])]) fn(); },
  };
}
// The real ui.js (so the real UI.poll), a fake clock and document.
function world(extra = {}) {
  const clock = fakeClock(), document = fakeDocument();
  const ctx = { console, document, AbortController, setTimeout: clock.setTimeout, clearTimeout: clock.clearTimeout,
    setInterval: () => { throw new Error("setInterval: a repeating read belongs on UI.poll"); }, clearInterval() {},
    esc: String, jsArg: s => JSON.stringify(String(s ?? "")), icon: () => "", ...extra };
  ctx.window = ctx; ctx.jsq = ctx.jsArg;
  vm.createContext(ctx);
  vm.runInContext(fs.readFileSync("web/js/ui.js", "utf8"), ctx);
  return { ctx, clock, document, run: code => vm.runInContext(code, ctx) };
}
const src = file => fs.readFileSync(`web/js/${file}`, "utf8");

function loopWorld() {
  const modal = { hidden: true, classList: { contains: () => modal.hidden } };
  const w = world({ SET: { refresh: 15 }, STATE: { settingsDirty: new Set() }, $: () => modal, refreshes: 0, refreshResult: true });
  w.modal = modal;
  w.ctx.refresh = async () => { w.ctx.refreshes++; return w.ctx.refreshResult; };
  w.run(between(src("app.js"), "function startLoop()", "/* ---------------- nav"));
  return w;
}

test("the main loop refreshes every setting seconds, and a refresh that was skipped does not end it", async () => {
  const w = loopWorld();
  w.run("startLoop()");
  await w.clock.advance(14999);
  assert.equal(w.ctx.refreshes, 0);
  w.ctx.refreshResult = false;           // refresh() answers false when it skipped
  await w.clock.advance(1);
  assert.equal(w.ctx.refreshes, 1);
  await w.clock.advance(15000);
  assert.equal(w.ctx.refreshes, 2, "still going");
});

test("the main loop's handle really stops it, and restarting leaves one loop", async () => {
  const w = loopWorld();
  w.run("startLoop()");
  w.run("startLoop()");
  await w.clock.advance(15000);
  assert.equal(w.ctx.refreshes, 1, "one loop, not two");
  w.ctx.__loopTimer.stop();
  w.document.show(false); w.document.show(true);
  await w.clock.advance(120000);
  assert.equal(w.ctx.refreshes, 1, "stopped: nothing on a tick or a tab return");
  assert.equal(w.document.listening("visibilitychange"), 0);
});

test("the main loop skips a tick while a dialog is open or settings are being edited, but is not stopped by closing one", async () => {
  const w = loopWorld();
  w.run("startLoop()");
  w.modal.hidden = false;
  w.ctx.$ = () => ({ classList: { contains: () => false } });  // modal open: "hidden" is false
  await w.clock.advance(30000);
  assert.equal(w.ctx.refreshes, 0, "dialog open: no tick refreshes");
  w.ctx.$ = () => ({ classList: { contains: () => true } });
  w.ctx.STATE.settingsDirty.add("x");
  await w.clock.advance(15000);
  assert.equal(w.ctx.refreshes, 0, "settings dirty: no tick refreshes");
  w.ctx.STATE.settingsDirty.clear();
  w.ctx.UI.stopPolls();                  // closeModal does this
  await w.clock.advance(15000);
  assert.equal(w.ctx.refreshes, 1, "closing a dialog does not stop the main loop");
});

test("coming back to the tab refreshes once, not twice", async () => {
  const w = loopWorld();
  w.run("startLoop()");
  w.document.show(false);
  await w.clock.advance(60000);
  assert.equal(w.ctx.refreshes, 0);
  w.document.show(true); await w.clock.settle();
  assert.equal(w.ctx.refreshes, 1);
});

test("coming back to the tab refreshes even with a dialog open, as it always did", async () => {
  const w = loopWorld();
  w.run("startLoop()");
  w.ctx.$ = () => ({ classList: { contains: () => false } });
  w.document.show(false); w.document.show(true); await w.clock.settle();
  assert.equal(w.ctx.refreshes, 1);
});

function alertsWorld() {
  const w = world({ PWA_ALERTS: { timer: null, epoch: 0 }, alertReads: 0 });
  w.ctx.refreshPwaAlerts = () => { w.ctx.alertReads++; };
  w.run(between(src("pwa.js"), "window.stopAlertChecks=", "window.refreshPwaAlerts="));
  return w;
}

test("the alert checks read at once, every 20 s, once on tab return, and stop when told", async () => {
  const w = alertsWorld();
  w.ctx.startAlertChecks();
  assert.equal(w.ctx.alertReads, 1, "at once");
  await w.clock.advance(20000);
  assert.equal(w.ctx.alertReads, 2);
  w.document.show(false);
  await w.clock.advance(60000);
  assert.equal(w.ctx.alertReads, 2, "nothing while hidden");
  w.document.show(true); await w.clock.settle();
  assert.equal(w.ctx.alertReads, 3, "one read on return, not two");
  w.ctx.stopAlertChecks();
  w.document.show(false); w.document.show(true);
  await w.clock.advance(120000);
  assert.equal(w.ctx.alertReads, 3, "stopped");
  assert.equal(w.document.listening("visibilitychange"), 0);
});

test("starting the alert checks twice leaves one loop", async () => {
  const w = alertsWorld();
  w.ctx.startAlertChecks(); w.ctx.startAlertChecks();
  assert.equal(w.ctx.alertReads, 2);
  await w.clock.advance(20000);
  assert.equal(w.ctx.alertReads, 3);
});

function imageWorld() {
  const w = world({ imageReads: 0 });
  w.ctx.loadImageUpdates = async () => { w.ctx.imageReads++; };
  w.run(between(src("views-workloads.js"), "window.startUpdateChecks = ", "/* The release an update moves to"));
  return w;
}

test("the image-update check reads after 3.5 s, then every 15 minutes", async () => {
  const w = imageWorld();
  w.ctx.startUpdateChecks();
  await w.clock.advance(3499);
  assert.equal(w.ctx.imageReads, 0);
  await w.clock.advance(1);
  assert.equal(w.ctx.imageReads, 1);
  await w.clock.advance(15 * 60 * 1000);
  assert.equal(w.ctx.imageReads, 2);
});

test("the image-update check reads nothing when the tab comes back, and its handle stops it", async () => {
  const w = imageWorld();
  w.ctx.startUpdateChecks();
  await w.clock.advance(3500);
  w.document.show(false);
  await w.clock.advance(3600000);
  assert.equal(w.ctx.imageReads, 1, "nothing while hidden");
  w.document.show(true); await w.clock.settle();
  assert.equal(w.ctx.imageReads, 1, "and nothing at once on return");
  await w.clock.advance(15 * 60 * 1000);
  assert.equal(w.ctx.imageReads, 2);
  w.ctx.__imageUpdateLoop.stop();
  await w.clock.advance(3600000);
  assert.equal(w.ctx.imageReads, 2, "stopped");
});

test("the VM console no longer polls for its dialog closing; closing the dialog ends the session", () => {
  const consoleSrc = src("vm-console.js");
  assert.doesNotMatch(consoleSrc, /setInterval|VMC\.watch/);
  const calls = [];
  const ctx = { window: {}, VMC_TOUCH: () => false };
  ctx.window = ctx;
  vm.createContext(ctx);
  vm.runInContext(between(consoleSrc, "const VMC = ", "function vmConsoleUrl") + between(consoleSrc, "function vmConsoleStop()", "/* A VM with a GPU"), ctx);
  vm.runInContext("VMC.rfb = { disconnect() { calls.push('rfb'); } }; VMC.socket = { close() { calls.push('socket'); } };", Object.assign(ctx, { calls }));
  assert.equal(typeof ctx.vmConsoleDismiss, "function");
  ctx.vmConsoleDismiss();
  assert.deepEqual(calls, ["rfb", "socket"]);
  ctx.vmConsoleDismiss();
  assert.deepEqual(calls, ["rfb", "socket"], "a second close has nothing to end");
  const core = require("./helpers/core-source")();
  assert.match(between(core, "function closeModal(", "\n}\n"), /vmConsoleDismiss/);
});
