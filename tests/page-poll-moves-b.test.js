"use strict";
// Page loops moved onto UI.poll (#1406, the Pages table), group B: Volumes
// following a rebuild or restore, the Image cache following a scan, and IP
// addresses following a subnet scan. Each is loaded with the real ui.js (so the
// real UI.poll) and its own view file, in tests/helpers/dialog-poll-world.js:
// a clock moved by hand, a tab that can be hidden, and reads held until the
// test answers them. Leaving the page is what go() does to a page's reads and
// polls (NAV_TOKEN, abortPageReads, UI.stopPagePolls); IP addresses can also be
// left by another Networking section, which bumps NAV_TOKEN without go().
// It is NOT a browser: the browser groups open these pages for real.
const test = require("node:test");
const assert = require("node:assert/strict");
const world = require("./helpers/dialog-poll-world");

const LIMIT = 30000;   // UI.poll gives a read up after 5 intervals or 30 s; all three are under 6 s

/* The page's world: core-api's page signal, and go() as far as reads go. */
function pageWorld(files, view, extra = {}) {
  let pageReads = { token: -1, controller: null }, ctx = null;   // ctx: the world's, once made
  const w = world(files, {
    NAV_TOKEN: 1,
    STATE: { view, q: "", data: {} },
    pageReadSignal() {
      if (pageReads.token !== ctx.NAV_TOKEN) { pageReads.controller?.abort(); pageReads = { token: ctx.NAV_TOKEN, controller: new AbortController() }; }
      return pageReads.controller.signal;
    },
    abortPageReads() { if (pageReads.token !== ctx.NAV_TOKEN) { pageReads.controller?.abort(); pageReads = { token: -1, controller: null }; } },
    tip: () => "", fmtAgo: s => `${Math.round(s)} s ago`, paint() {},
    ...extra,
  });
  ctx = w.ctx;
  const held = w.ctx.api;
  // As core-api's api(): a read with no signal of its own takes the page's.
  w.ctx.api = (path, opts = {}) => held(path, { ...opts, signal: opts.signal || w.ctx.pageReadSignal() });
  w.document.querySelector = () => null;
  w.document.querySelectorAll = () => [];
  w.go = page => { w.ctx.NAV_TOKEN++; w.ctx.abortPageReads(); w.ctx.UI.stopPagePolls(); w.ctx.STATE.view = page; };
  return w;
}

const imagesAnswer = scanning => ({ images: [], nodes: [{ node: "node-a", total_gb: 1, count: 2, scanned_at: 0 }], distinct: 2, protected: 0, scanning: scanning ? ["node-a"] : [] });
const ipamAnswer = state => ({ unifi: {}, suggested: [], subnets: [{ id: "lan", name: "LAN", cidr: "192.0.2.0/24", used: 1, usable: 254, free_static: 200, next_free: [],
  dhcp_size: 0, scan: { state, progress: 40, at: 0 }, rows: [] }] });
const volumes = busy => [{ name: "data", namespace: "apps", ...(busy ? { rebuild: { pct: 40 } } : {}) }];

const LOOPS = [
  {
    name: "Volumes following a rebuild", files: "views-storage-volumes.js", view: "storage", path: "/api/volumes", every: 4000,
    extra: { CSS: { escape: String } },
    // storagePaint ends with volumeProgressWatch(): the page drawn with a volume rebuilding.
    draw: async w => { w.ctx.STATE.data.vols = volumes(true); w.run("volumeProgressWatch()"); },
    running: () => volumes(true), done: () => volumes(false),
  },
  {
    name: "Image cache following a scan", files: "views-lifecycle-images.js", view: "images", path: "/api/images", every: 4000,
    side: r => r.path === "/api/images/vm" ? [] : { enabled: false },
    draw: w => w.ctx.viewImages(),
    running: () => imagesAnswer(true), done: () => imagesAnswer(false),
  },
  {
    name: "IP addresses following a subnet scan", files: "views-ipam.js", view: "network", path: "/api/ipam", every: 3000,
    extra: { URLSearchParams, networkPage: (tab, html) => html, location: { search: "?tab=ip" }, localStorage: { getItem: () => "", setItem() {} } },
    draw: w => w.ctx.viewIpam(),
    running: () => ipamAnswer("running"), done: () => ipamAnswer("done"),
  },
];

for (const loop of LOOPS) {
  const pollReads = w => w.reads.filter(r => r.path === loop.path);
  /* The page drawn with the job running: the draw's own read (Volumes has
     none: the page's load read the volumes) answered at once. After it, the
     loop's reads wait for the test unless w.pollAuto answers them. */
  async function drawn() {
    const w = pageWorld(loop.files, loop.view, loop.extra);
    w.pollAuto = r => r.answer(loop.running());
    w.answers.auto = r => r.path === loop.path ? w.pollAuto?.(r) : r.answer(loop.side ? loop.side(r) : {});
    await loop.draw(w); await w.clock.settle();
    w.pollAuto = null;
    w.drawReads = pollReads(w).length;
    return w;
  }
  const loopReads = w => pollReads(w).length - w.drawReads;

  test(`${loop.name}: reads ${loop.every / 1000} s after the page is drawn, then ${loop.every / 1000} s after each answer`, async () => {
    const w = await drawn();
    await w.clock.advance(loop.every - 1);
    assert.equal(loopReads(w), 0);
    await w.clock.advance(1);
    assert.equal(loopReads(w), 1);
    await w.clock.advance(LIMIT - 1000);
    assert.equal(loopReads(w), 1, "no second read beside one still on its way");
    pollReads(w).at(-1).answer(loop.running()); await w.clock.settle();
    await w.clock.advance(loop.every - 1);
    assert.equal(loopReads(w), 1);
    await w.clock.advance(1);
    assert.equal(loopReads(w), 2, `the next read is ${loop.every / 1000} s after the answer`);
  });

  test(`${loop.name}: a hidden tab reads nothing, and reads once when shown`, async () => {
    const w = await drawn();
    w.pollAuto = r => r.answer(loop.running());
    await w.clock.advance(loop.every);
    assert.equal(loopReads(w), 1);
    w.document.show(false);
    await w.clock.advance(20 * loop.every);
    assert.equal(loopReads(w), 1, "no read while the tab is hidden");
    assert.equal(w.clock.pending(), 0, "and no timer is left ticking while it is hidden");
    w.document.show(true); await w.clock.settle();
    assert.equal(loopReads(w), 2, "one read when it is shown again");
    w.document.show(false); w.document.show(true); w.document.show(false); w.document.show(true); await w.clock.settle();
    await w.clock.advance(loop.every);
    assert.equal(loopReads(w), 5, "however often the tab is shown, one loop");
  });

  test(`${loop.name}: a read with no answer in ${LIMIT / 1000} s is given up, and the loop tries again`, async () => {
    const w = await drawn();
    await w.clock.advance(loop.every);
    const stuck = pollReads(w).at(-1);
    await w.clock.advance(LIMIT - 1);
    assert.equal(stuck.signal?.aborted ?? false, false, "not before its limit");
    await w.clock.advance(1);
    assert.equal(stuck.signal?.aborted, true, "the read is cancelled, so it stops holding a connection");
    await w.clock.advance(loop.every);
    assert.equal(loopReads(w), 2, "and the loop reads again an interval later");
  });

  test(`${loop.name}: a failed read is tried again an interval later`, async () => {
    const w = await drawn();
    await w.clock.advance(loop.every);
    pollReads(w).at(-1).fail(new Error("connection refused")); await w.clock.settle();
    await w.clock.advance(loop.every);
    assert.equal(loopReads(w), 2);
  });

  test(`${loop.name}: the job finished ends the reads`, async () => {
    const w = await drawn();
    await w.clock.advance(loop.every);
    pollReads(w).at(-1).answer(loop.done()); await w.clock.settle();
    await w.clock.advance(10 * loop.every);
    assert.equal(loopReads(w), 1);
    assert.equal(w.clock.pending(), 0, "no timer is left behind");
  });

  test(`${loop.name}: leaving the page between reads stops the loop at once`, async () => {
    const w = await drawn();
    w.go("overview");
    assert.equal(w.clock.pending(), 0, "no timer is left waiting for the page left behind");
    await w.clock.advance(20 * loop.every);
    assert.equal(loopReads(w), 0);
  });

  test(`${loop.name}: leaving the page cancels the read on its way and reads no more`, async () => {
    const w = await drawn();
    await w.clock.advance(loop.every);
    const read = pollReads(w).at(-1);
    w.go("overview");
    assert.equal(read.signal?.aborted, true, "the read on its way is cancelled");
    read.answer(loop.running()); await w.clock.settle();
    await w.clock.advance(20 * loop.every);
    assert.equal(loopReads(w), 1, "nothing reads after the page was left");
    assert.equal(w.clock.pending(), 0);
  });

  test(`${loop.name}: a page drawn again while the job runs keeps one loop, not one per draw`, async () => {
    const w = await drawn();
    w.pollAuto = r => r.answer(loop.running());
    for (let i = 0; i < 2; i++) { await loop.draw(w); await w.clock.settle(); }
    const before = pollReads(w).length;
    await w.clock.advance(4 * loop.every);
    assert.equal(pollReads(w).length - before, 4, "one read per interval, not one per draw");
  });

  test(`${loop.name}: left and come back to, it runs one loop`, async () => {
    const w = await drawn();
    w.pollAuto = r => r.answer(loop.running());
    await w.clock.advance(loop.every);
    w.go("overview");
    w.go(loop.view);
    await loop.draw(w); await w.clock.settle();
    const before = pollReads(w).length;
    await w.clock.advance(4 * loop.every);
    assert.equal(pollReads(w).length - before, 4, "one read per interval, not two");
  });
}

/* IP addresses is a section of Networking: another section is shown without
   go(), by NAV_TOKEN and the page's reads (networkTab). */
test("IP addresses: another Networking section ends the scan loop, and cancels its read", async () => {
  const loop = LOOPS[2];
  const w = pageWorld(loop.files, loop.view, loop.extra);
  let first = true;
  w.answers.auto = r => { if (first) { first = false; r.answer(loop.running()); } };
  await w.ctx.viewIpam(); await w.clock.settle();
  await w.clock.advance(loop.every);
  const read = w.reads.at(-1);
  assert.equal(w.reads.length, 2);
  w.ctx.NAV_TOKEN++; w.ctx.abortPageReads();   // networkTab("overview")
  await w.clock.settle();
  assert.equal(read.signal.aborted, true);
  await w.clock.advance(20 * loop.every);
  assert.equal(w.reads.length, 2, "nothing reads for a section nobody is looking at");
  assert.equal(w.clock.pending(), 0);
});

/* Someone half-way through a bulk change keeps the page as it is (viewIpam
   draws nothing); the scan is still followed, and the page catches up after. */
test("IP addresses: a tick while someone uses the bulk bar draws nothing, and the scan is still followed", async () => {
  const loop = LOOPS[2];
  const w = pageWorld(loop.files, loop.view, loop.extra);
  w.answers.auto = r => r.answer(loop.running());
  await w.ctx.viewIpam(); await w.clock.settle();
  w.document.activeElement = { closest: sel => sel === "#ipamBulk" ? {} : null };
  await w.clock.advance(2 * loop.every);
  assert.equal(w.reads.length, 3, "the scan is still followed");
  w.document.activeElement = null;
  await w.clock.advance(loop.every);
  assert.equal(w.reads.length, 4);
});
