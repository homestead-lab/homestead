"use strict";
// A dialog's poll (#983): never two reads of the same thing at once, nothing
// read while the tab is hidden, one read at once when it is shown again, and
// closing the dialog cancels the read still running.
const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const between = require("./helpers/between");

// A clock the test moves by hand. advance() runs every timer that falls due,
// letting the promises each one starts settle before the next.
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
    show(visible) { this.visibilityState = visible ? "visible" : "hidden"; this.hidden = !visible; for (const fn of listeners.get("visibilitychange") || []) fn(); },
  };
}

function loadUI() {
  const clock = fakeClock(), document = fakeDocument();
  const ctx = { console, document, AbortController, setTimeout: clock.setTimeout, clearTimeout: clock.clearTimeout,
    setInterval: () => 0, clearInterval() {}, esc: String, jsArg: s => JSON.stringify(String(s ?? "")), icon: () => "" };
  ctx.window = ctx; ctx.jsq = ctx.jsArg;
  vm.createContext(ctx);
  vm.runInContext(fs.readFileSync("web/js/ui.js", "utf8"), ctx);
  return { UI: ctx.window.UI, clock, document, ctx };
}

test("a poll whose read is still running gets no second read, however long it takes", async () => {
  const { UI, clock } = loadUI();
  let calls = 0, answer;
  UI.poll(() => { calls++; return new Promise(resolve => { answer = resolve; }); }, 2000);
  assert.equal(calls, 1, "the first read is at once");
  await clock.advance(30000);
  assert.equal(calls, 1, "30 s of a slow read: still one in flight, none piled up behind it");
  answer(); await clock.settle();
  assert.equal(calls, 1, "the next read waits its interval after the last one settled");
  await clock.advance(2000);
  assert.equal(calls, 2);
});

test("a hidden tab reads nothing for 60 s, and showing it reads at once", async () => {
  const { UI, clock, document } = loadUI();
  let calls = 0;
  UI.poll(async () => { calls++; }, 2000);
  await clock.settle();
  assert.equal(calls, 1);
  document.show(false);
  await clock.advance(60000);
  assert.equal(calls, 1, "no read while the tab is hidden");
  assert.equal(clock.pending(), 0, "and no timer left ticking");
  document.show(true); await clock.settle();
  assert.equal(calls, 2, "the read is at once when the tab is shown again");
  await clock.advance(2000);
  assert.equal(calls, 3, "then every interval as before");
});

test("a poll started in a hidden tab waits until the tab is shown", async () => {
  const { UI, clock, document } = loadUI();
  document.show(false);
  let calls = 0;
  UI.poll(async () => { calls++; }, 2000);
  await clock.advance(60000);
  assert.equal(calls, 0);
  document.show(true); await clock.settle();
  assert.equal(calls, 1);
});

test("showing the tab during a read does not start a second one beside it", async () => {
  const { UI, clock, document } = loadUI();
  let calls = 0, answer;
  UI.poll(() => { calls++; return new Promise(resolve => { answer = resolve; }); }, 2000);
  document.show(false); document.show(true); await clock.settle();
  assert.equal(calls, 1, "the read still running is the fresh one");
  answer(); await clock.settle();
  assert.equal(calls, 2, "one more, at once, for the visit");
});

test("closing the dialog stops every poll and cancels the read still running", async () => {
  const { UI, clock, document } = loadUI();
  const signals = [];
  let calls = 0;
  UI.poll(signal => { calls++; signals.push(signal); return new Promise(() => {}); }, 2000);
  UI.poll(signal => { calls++; signals.push(signal); return new Promise(() => {}); }, 3000);
  assert.equal(calls, 2);
  UI.stopPolls();
  assert.ok(signals.every(s => s.aborted), "the reads in flight are cancelled");
  assert.equal(document.listening("visibilitychange"), 0, "nothing is left listening for the tab");
  document.show(false); document.show(true);
  await clock.advance(60000);
  assert.equal(calls, 2, "and nothing reads again");
});

test("a read that says false ends its poll; one that fails is tried again", async () => {
  const { UI, clock } = loadUI();
  let calls = 0;
  const poll = UI.poll(async () => { calls++; if (calls === 1) throw new Error("timed out"); return calls < 3; }, 2000);
  await clock.advance(10000);
  assert.equal(calls, 3, "a failed read is retried; the third says it is done");
  assert.equal(poll.stopped, true);
  assert.equal(clock.pending(), 0);
});

test("now() reads at once, or straight after the read still running", async () => {
  const { UI, clock } = loadUI();
  let calls = 0, answer;
  const poll = UI.poll(() => { calls++; return new Promise(resolve => { answer = resolve; }); }, 2000);
  poll.now(); poll.now();
  assert.equal(calls, 1, "not beside the read still running");
  answer(); await clock.settle();
  assert.equal(calls, 2, "but right after it, once");
  answer(); await clock.settle();
  assert.equal(calls, 2);
  await clock.advance(2000);
  assert.equal(calls, 3);
});

test("a read that never answers is given up after its limit, and the poll carries on", async () => {
  const { UI, clock } = loadUI();
  const signals = [];
  let calls = 0;
  // One read honours its signal (api's fetch does), the other never settles at all.
  UI.poll(signal => { calls++; signals.push(signal); return new Promise((_, reject) => signal.addEventListener("abort", () => reject(signal.reason))); }, 2000);
  UI.poll(signal => { calls++; signals.push(signal); return new Promise(() => {}); }, 2000);
  assert.equal(calls, 2);
  await clock.advance(29999);
  assert.equal(calls, 2, "the limit is 30 s (or 5 intervals, if longer)");
  await clock.advance(1);
  assert.ok(signals.slice(0, 2).every(s => s.aborted), "the stuck reads are cancelled");
  assert.equal(signals[0].reason.name, "TimeoutError");
  assert.equal(UI.pollCancelled(signals[0]), false, "a read that timed out is a failed read the dialog shows, not a cancelled one");
  await clock.advance(2000);
  assert.equal(calls, 4, "both polls read again");
  UI.stopPolls();
  assert.equal(UI.pollCancelled(signals[2]), true, "closing the dialog is a cancel the dialog does not show");
});

test("restart() cancels the read running and reads again at once", async () => {
  const { UI, clock } = loadUI();
  const signals = [];
  const poll = UI.poll(signal => { signals.push(signal); return new Promise((_, reject) => signal.addEventListener("abort", () => reject(signal.reason))); }, 2000);
  poll.restart(); await clock.settle();
  assert.equal(signals.length, 2, "the new pod's read starts straight away");
  assert.equal(signals[0].aborted, true, "the old pod's read is cancelled, so its output never paints");
  assert.equal(UI.pollCancelled(signals[0]), true);
  assert.equal(signals[1].aborted, false);
});

test("a poll that starts later waits its interval before the first read", async () => {
  const { UI, clock } = loadUI();
  let calls = 0;
  UI.poll(async () => { calls++; }, 20000, { later: true, dialog: false });
  assert.equal(calls, 0);
  UI.stopPolls();
  await clock.advance(20000);
  assert.equal(calls, 1, "a poll that is not a dialog's outlives closing one");
});

/* The Volumes progress watch reads the whole volume list every 4 s while a
   volume rebuilds or restores, which can be hours. A hidden tab reads none. */
test("a busy volume with a hidden tab makes no /api/volumes read in 60 s", async () => {
  const clock = fakeClock(), document = Object.assign(fakeDocument(), { querySelector: () => null });
  const reads = [];
  const ctx = { console, document, setTimeout: clock.setTimeout, clearTimeout: clock.clearTimeout,
    STATE: { view: "storage", data: { vols: [{ name: "data", rebuilding: true }] } },
    volumeBusy: x => !!x.rebuilding, CSS: { escape: String }, api: async path => { reads.push(path); return [{ name: "data", rebuilding: true }]; } };
  ctx.window = ctx;
  vm.createContext(ctx);
  const src = fs.readFileSync("web/js/views-storage.js", "utf8");
  vm.runInContext(between(src, "function volumeProgressWatch()", "/* Claims on classes that are not Longhorn"), ctx);
  vm.runInContext("volumeProgressWatch()", ctx);
  await clock.advance(8000);
  assert.equal(reads.length, 2, "a visible tab follows the rebuild");
  document.show(false);
  await clock.advance(60000);
  assert.equal(reads.length, 2, "a hidden one reads nothing");
  assert.equal(clock.pending(), 0, "and leaves no timer ticking");
  document.show(true);
  await clock.advance(4000);
  assert.equal(reads.length, 3, "showing the tab starts the watch again");
  document.show(false); document.show(true); document.show(false); document.show(true);
  await clock.advance(4000);
  assert.equal(reads.length, 4, "however often the tab is shown, one watch");
});

/* The ratchet: a repeating timer that reads must be a poll, which waits for
   its last read and stops in a hidden tab. setInterval does neither. A
   callback that is async, or calls api(), fetch() or post(), is flagged; one
   that calls refresh() or a load*() helper is flagged unless it checks
   document.hidden itself (the main refresh loop, which has its own busy
   guard, and the 15-minute image-update check). A member callback
   (setInterval(obj.fn, ...)) cannot be read here, so it is flagged too. */
function intervalOffenders(file, src) {
  const offenders = [];
  for (const m of src.matchAll(/\bsetInterval\(\s*/g)) {
    const start = m.index + m[0].length, line = `${file}:${src.slice(0, m.index).split("\n").length}`;
    if (/^[A-Za-z_$][\w$]*(?:\.[A-Za-z_$][\w$]*)+\s*,/.test(src.slice(start))) { offenders.push(`${line} member callback`); continue; }
    const name = /^[A-Za-z_$][\w$]*(?=\s*,)/.exec(src.slice(start))?.[0];
    let callback;
    if (name) {
      const def = new RegExp(`(?:(?:const|let|var)\\s+${name}\\s*=\\s*(async\\b)?|(async\\s+)?function\\s+${name}\\b)`).exec(src);
      if (!def) continue;
      if (def[1] || def[2]) { offenders.push(`${line} ${name} is async`); continue; }
      callback = between(src.slice(def.index), def[0], /\n(?:\s*\n|\S)/);
    } else {
      // The inline callback: up to the comma that closes it at depth 0.
      let depth = 0, end = start;
      for (; end < src.length; end++) {
        const c = src[end];
        if ("([{".includes(c)) depth++;
        else if (")]}".includes(c)) { if (depth === 0) break; depth--; }
        else if (c === "," && depth === 0) break;
      }
      callback = src.slice(start, end);
    }
    const reads = /(?:^|[^\w$.])(?:api|fetch|post)\(/.test(callback);
    const helper = /(?:^|[^\w$.])(?:refresh|load[A-Z]\w*)\(/.test(callback) && !/document\.hidden/.test(callback);
    if (/^\s*async\b/.test(callback) || reads || helper) offenders.push(line);
  }
  return offenders;
}

test("no setInterval in web/js starts a read (use UI.poll)", () => {
  const offenders = fs.readdirSync("web/js").filter(f => f.endsWith(".js"))
    .flatMap(file => intervalOffenders(file, fs.readFileSync(`web/js/${file}`, "utf8")));
  assert.deepEqual(offenders, []);
});

test("the ratchet catches each shape of a reading interval", () => {
  const cases = {
    "setInterval(async () => { await x(); }, 1000);": 1,
    "setInterval(() => api('/api/x'), 1000);": 1,
    "setInterval(() => { fetch('/x'); }, 1000);": 1,
    "setInterval(() => post('/x', {}), 1000);": 1,
    "setInterval(() => refresh(), 1000);": 1,
    "setInterval(() => { loadThings(true); }, 1000);": 1,
    "setInterval(() => { if (!document.hidden) loadThings(true); }, 1000);": 0,
    "setInterval(Watch.tick, 1000);": 1,
    "const tick = async () => {}; setInterval(tick, 1000);": 1,
    "function sweep() { tidy(); }\nsetInterval(sweep, 1000);": 0,
    "setInterval(() => refreshPwaAlerts(), 1000);": 0,
  };
  for (const [src, n] of Object.entries(cases)) assert.equal(intervalOffenders("x.js", src).length, n, src);
});

test("closing a dialog stops its polls", () => {
  const core = fs.readFileSync("web/js/core.js", "utf8");
  assert.match(between(core, "function closeModal(", "\n}\n"), /UI\??\.stopPolls/);
});
