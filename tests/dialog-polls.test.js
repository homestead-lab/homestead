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

test("a read is told why it runs: the start, a timer tick or the tab being shown", async () => {
  const { UI, clock, document } = loadUI();
  const why = [];
  UI.poll(async (signal, reason) => { why.push(reason); }, 2000);
  await clock.advance(2000);
  document.show(false); document.show(true); await clock.settle();
  assert.deepEqual(why, ["start", "tick", "show"]);
});

test("readOnShow: false waits a whole interval after the tab is shown, and reads nothing before", async () => {
  const { UI, clock, document } = loadUI();
  let calls = 0;
  UI.poll(async () => { calls++; }, 900000, { later: true, dialog: false, readOnShow: false });
  await clock.advance(900000);
  assert.equal(calls, 1);
  document.show(false);
  await clock.advance(3600000);
  assert.equal(calls, 1, "nothing is read while the tab is hidden");
  document.show(true); await clock.settle();
  assert.equal(calls, 1, "showing the tab is not a reason to read");
  await clock.advance(899999);
  assert.equal(calls, 1);
  await clock.advance(1);
  assert.equal(calls, 2, "the next read is an interval after the tab came back");
});

test("a stopped poll that is not a dialog's reads no more, however the tab changes", async () => {
  const { UI, clock, document } = loadUI();
  let calls = 0;
  const poll = UI.poll(async () => { calls++; }, 2000, { later: true, dialog: false });
  await clock.advance(2000);
  assert.equal(calls, 1);
  poll.stop();
  document.show(false); document.show(true);
  await clock.advance(60000);
  assert.equal(calls, 1);
  assert.equal(document.listening("visibilitychange"), 0);
  assert.equal(clock.pending(), 0);
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
  const src = require("./helpers/storage-source")();
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
  const core = require("./helpers/core-source")();
  assert.match(between(core, "function closeModal(", "\n}\n"), /UI\??\.stopPolls/);
});

test("first settles when the first read ends, or when the poll stops before it", async () => {
  const { UI, clock, document } = loadUI();
  let answer, settled = false;
  const poll = UI.poll(() => new Promise(resolve => { answer = resolve; }), 2000);
  poll.first.then(() => { settled = true; });
  await clock.settle();
  assert.equal(settled, false, "not while the first read is on its way");
  answer(); await clock.settle();
  assert.equal(settled, true);
  document.show(false);
  let hiddenSettled = false;
  const waiting = UI.poll(async () => {}, 2000);
  waiting.first.then(() => { hiddenSettled = true; });
  await clock.advance(10000);
  assert.equal(hiddenSettled, false, "a poll started in a hidden tab has not read yet");
  waiting.stop(); await clock.settle();
  assert.equal(hiddenSettled, true, "stopped before reading, it settles too, so its opener is not held for good");
  poll.stop();
});

/* A page's poll (page: true): closing a dialog leaves it running, leaving the
   page stops it and cancels its read at once. UI.poll hands its own signal to
   api(), so the page's own read cancel (abortPageReads) never reaches it: a
   page poll that nothing stopped would go on reading the page left behind. */
test("a page's poll outlives closing a dialog, and leaving the page stops it and cancels its read", async () => {
  const { UI, clock, document } = loadUI();
  const signals = [];
  let calls = 0;
  const poll = UI.poll(signal => { calls++; signals.push(signal); return calls === 1 ? Promise.resolve() : new Promise(() => {}); }, 2000, { page: true });
  await clock.settle();
  UI.stopPolls();
  await clock.advance(2000);
  assert.equal(calls, 2, "closing a dialog is not leaving the page");
  UI.stopPagePolls();
  assert.equal(signals[1].aborted, true, "the read in flight is cancelled at once");
  assert.equal(UI.pollCancelled(signals[1]), true, "as a cancel, so its answer or error is not shown");
  assert.equal(poll.stopped, true);
  document.show(false); document.show(true);
  await clock.advance(60000);
  assert.equal(calls, 2, "nothing reads again, however the tab changes");
  assert.equal(document.listening("visibilitychange"), 0);
  assert.equal(clock.pending(), 0);
});

test("a page's poll whose page reads were abandoned stops on its next turn", async () => {
  const { UI, clock, ctx } = loadUI();
  ctx.NAV_TOKEN = 3;
  let calls = 0;
  const poll = UI.poll(async () => { calls++; }, 2000, { page: true });
  await clock.advance(2000);
  assert.equal(calls, 2);
  ctx.NAV_TOKEN++;            // a section change abandons the page's reads without go()
  await clock.advance(10000);
  assert.equal(calls, 2, "no read for the page left behind");
  assert.equal(poll.stopped, true);
  assert.equal(clock.pending(), 0);
});

// The page's own read cancel, as core-api.js has it: pageReadSignal() and abortPageReads().
function withPageReads(ctx) {
  ctx.AbortSignal = AbortSignal;
  const api = fs.readFileSync("web/js/core-api.js", "utf8");
  vm.runInContext(between(api, "let pageReads =", "async function api("), ctx);
}

test("a page's poll whose page is left while its read runs drops that read's answer", async () => {
  const { UI, clock, ctx } = loadUI();
  ctx.NAV_TOKEN = 1;
  withPageReads(ctx);
  let calls = 0, answer, painted = 0;
  const poll = UI.poll(async signal => { calls++; await new Promise(resolve => { answer = resolve; }); if (!UI.pollCancelled(signal)) painted++; }, 2000, { page: true });
  ctx.NAV_TOKEN++; ctx.abortPageReads();   // a Network section change: no go()
  answer(); await clock.settle();
  assert.equal(painted, 0, "the late answer is not painted");
  assert.equal(poll.stopped, true, "and the poll ends");
  await clock.advance(10000);
  assert.equal(calls, 1);
  assert.equal(clock.pending(), 0);
});

test("stopPagePolls leaves dialog polls and the polls that are neither alone", async () => {
  const { UI, clock } = loadUI();
  let dialogReads = 0, otherReads = 0;
  UI.poll(async () => { dialogReads++; }, 2000);
  UI.poll(async () => { otherReads++; }, 2000, { dialog: false });
  UI.stopPagePolls();
  await clock.advance(4000);
  assert.equal(dialogReads, 3);
  assert.equal(otherReads, 3, "the main refresh loop and the alert checks are not a page's");
});

test("going to another page stops that page's polls at once", async () => {
  const { UI, clock, ctx } = loadUI();
  const app = fs.readFileSync("web/js/app.js", "utf8");
  ctx.NAV_TOKEN = 1;
  withPageReads(ctx);
  ctx.STATE = {};
  const signals = [];
  const waiting = UI.poll(signal => { signals.push(signal); return new Promise(() => {}); }, 2000, { page: true });
  const between_reads = UI.poll(async () => { signals.push(null); }, 2000, { page: true });
  await clock.settle();
  vm.runInContext(between(app, "window.NAV_TOKEN++;", "STATE.busy = false;"), ctx);
  assert.equal(signals[0].aborted, true, "the read in flight is cancelled at once, not 30 s later");
  assert.equal(waiting.stopped && between_reads.stopped, true, "both polls end as the page is left, not at their next turn");
  await clock.settle();
  assert.equal(clock.pending(), 0, "no timer is left waiting for the page left behind");
  await clock.advance(60000);
  assert.equal(signals.length, 2);
});

/* The second ratchet: a function that reads and then sets a timer to run
   itself again is a poll written by hand. It reads on in a hidden tab, and a
   read that never answers stops it for good. New ones go on UI.poll. The
   list is what is left today, by file and function; moving one to UI.poll
   takes it off (the list must match exactly, so a move cannot be forgotten).
   It sees a setTimeout whose callback calls the function it sits in (the
   nearest function before it), where that function or the callback reads:
   api(), mustRead(), fetch(), post(), refresh() or a view*() page. */
const HAND_POLLS_LEFT = [
  "cluster-shutdown.js shutdownPoll",
  "disk-allocation.js poll",
  "disk-v2.js poll",
  "host-os.js osUpdatesFollow",
  "longhorn-runtime.js poll",
  "longhorn-v2-setup.js lhV2Poll",
  "longhorn-v2-setup.js poll",
  "longhorn-v2-upgrade.js lhV2UpgradePoll",
  "onboard.js guideWatch",
  "snapshot-files.js snapshotFilesPoll",
  "views-lifecycle-images.js viewImages",
  "views-lifecycle-import.js watchMoves",
  "views-protect.js refreshSnapshotDialog",
  "views-settings.js paint",
  "views-storage-volumes.js volumeProgressWatch",
];
function handPolls(file, src) {
  const head = /(?:async\s+)?function\s+([A-Za-z_$][\w$]*)\s*\(|(?:const|let|var)?\s*([A-Za-z_$][\w$.]*)\s*=\s*(?:async\s*)?(?:\([^()]*\)|[A-Za-z_$][\w$]*)\s*=>/g;
  const found = [];
  for (const m of src.matchAll(/\bsetTimeout\(\s*/g)) {
    const before = src.slice(0, m.index);
    let name = null, at = 0;
    for (const h of before.matchAll(head)) { name = h[1] || h[2]; at = h.index; }
    if (!name) continue;
    const self = name.split(".").pop().replace(/\$/g, "\\$");
    let depth = 0, end = m.index + m[0].length;
    for (; end < src.length; end++) {
      const c = src[end];
      if ("([{".includes(c)) depth++;
      else if (")]}".includes(c)) { if (depth === 0) break; depth--; }
      else if (c === "," && depth === 0) break;
    }
    const callback = src.slice(m.index + m[0].length, end).trim();
    const again = callback === name.split(".").pop() || new RegExp(`(?:^|[^\\w$.])${self}\\s*\\(`).test(callback);
    const reads = /(?:^|[^\w$.])(?:api|mustRead|fetch|post|refresh|view[A-Z]\w*)\(/.test(src.slice(at, m.index) + callback);
    if (again && reads) found.push(`${file} ${name}`);
  }
  return found;
}

test("no new hand-written read loop of the shapes it can see in web/js (use UI.poll); the list left only shrinks", () => {
  const found = fs.readdirSync("web/js").filter(f => f.endsWith(".js") && f !== "demo.js" && f !== "ui.js").sort()
    .flatMap(file => handPolls(file, fs.readFileSync(`web/js/${file}`, "utf8")));
  assert.deepEqual(found, HAND_POLLS_LEFT);
});

test("the hand-poll ratchet sees each shape of a read that re-arms itself", () => {
  const cases = {
    "async function poll() { await api('/x'); setTimeout(poll, 4000); }": 1,
    "function watch() { refresh(); timer = setTimeout(() => watch(), 4000); }": 1,
    "const tick = async () => { await mustRead('/x', 'x'); setTimeout(tick, 2000); };": 1,
    "function viewThings() { setTimeout(() => { if (ok) viewThings(); }, 3000); }": 1,
    "function step() { send(); setTimeout(step, 30); }": 0,
    "async function save() { await api('/x', { method: 'POST' }); setTimeout(() => refresh(true), 1500); }": 0,
  };
  for (const [src, n] of Object.entries(cases)) assert.equal(handPolls("x.js", src).length, n, src);
});

/* background: true, for a dialog that follows a long operation that can go
   wrong: it reads on in a hidden tab at its own interval, and the tab coming
   back is no reason for an extra read. */
test("a background poll reads on in a hidden tab, and showing the tab adds no read", async () => {
  const { UI, clock, document } = loadUI();
  let calls = 0;
  const poll = UI.poll(async () => { calls++; }, 2000, { background: true });
  await clock.settle();
  assert.equal(calls, 1);
  document.show(false);
  await clock.advance(6000);
  assert.equal(calls, 4, "every interval while hidden");
  document.show(true); await clock.settle();
  assert.equal(calls, 4, "no extra read when the tab comes back");
  await clock.advance(2000);
  assert.equal(calls, 5);
  assert.equal(document.listening("visibilitychange"), 0, "it does not follow the tab at all");
  UI.stopPolls();
  await clock.advance(10000);
  assert.equal(calls, 5, "closing its dialog still stops it");
  assert.equal(poll.stopped, true);
});

test("a background poll started in a hidden tab reads at once", async () => {
  const { UI, clock, document } = loadUI();
  document.show(false);
  let calls = 0;
  UI.poll(async () => { calls++; }, 2000, { background: true });
  await clock.settle();
  assert.equal(calls, 1);
});
